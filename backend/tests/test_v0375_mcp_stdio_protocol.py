"""v0.37.5 T2：MCP stdio 协议客户端的行为锁（A1-A10、B1-B10 中属于 T2 的部分）。

覆盖：
- 握手成功 + ``notifications/initialized``（记录目录取证）；协议版本不符；拒绝
  initialize（JSON-RPC code 保留）；
- 两页 tools/list；6 页；70 条；cursor 重复；
- tools/call 正常中文/换行；``isError``（文案来自 text、进程仍活、随后可继续调用）；
  JSON-RPC error（code 保留、不杀）；
- 错 id 被丢弃后正确响应仍匹配；迟到重复只计数；
- 坏 JSON / 批量数组 / ``Content-Length:`` 各自协议错误且进程被杀；
- 600KB 单行 <2s 被杀；256KB 请求写前拒且进程未被杀；
- 慢滴超时杀 + 在途锁释放；EOF ``retryable=True``；
- CRLF / BOM；server 主动请求收到 ``-32601``（读记录文件）；通知洪水杀；
- ``close()`` 幂等。

协议形状收紧（T2 审查补充）：
- 非字符串 ``protocolVersion``、5000 位整数、NaN、孤立代理项、空 ``nextCursor``、
  标量 ``result``、``null`` error、布尔 ``id``、连续 3 次错 id、EOF 但进程仍活、
  回不了的 server 请求 → 全部协议错误并杀；合法转义代理对仍放行；
- 单在途/窄窗口：慢调用期间第二次调用「正忙」、``close()`` 立即唤醒在途调用、
  出站 id 从 1 递增。
"""
import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from app.core.mcp_client import (
    DEFAULT_PROTOCOL_VERSION,
    MCPError,
)
from app.core.mcp_stdio_client import (
    MCPStdioClient,
    _same_id,
    _sanitize_stderr_line,
)

SERVER = Path(__file__).with_name("fake_mcp_stdio_server.py")


class _Spawner:
    """最小 ``subprocess.Popen`` 闭包：客户端自己不做 Popen，T3 才换 Job/env 实现。"""

    def __init__(self, mode: str, record_dir: Path) -> None:
        self.mode = mode
        self.record_dir = record_dir
        self.proc = None
        self.spawns = 0

    def __call__(self):
        self.spawns += 1
        self.record_dir.mkdir(parents=True, exist_ok=True)
        stdout = subprocess.PIPE
        sink = None
        if self.mode == "eof_alive":
            # Windows 上子进程关 stdout 管道句柄不一定让读端看到 EOF（句柄有内部副本）。
            # 用空文件模拟“stdout 已到 EOF、进程仍活着”：读端立刻见底，进程继续 sleep。
            sink_path = self.record_dir / "stdout-empty.log"
            sink = open(sink_path, "wb")
            stdout = sink
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(SERVER), self.mode, str(self.record_dir)],
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=subprocess.PIPE,
            cwd=str(self.record_dir.parent),
        )
        if sink is not None:
            sink.close()
            self.proc.stdout = open(sink_path, "rb")
        return self.proc


@contextmanager
def _client(tmp_path, mode="ok", *, handshake_timeout=5.0, call_timeout=5.0, **kwargs):
    record = tmp_path / f"record-{mode}"
    spawner = _Spawner(mode, record)
    client = MCPStdioClient(
        spawner=spawner,
        handshake_timeout=handshake_timeout,
        call_timeout=call_timeout,
        **kwargs,
    )
    try:
        assert client.start() is not None
        yield client, spawner
    finally:
        with suppress(Exception):
            client.close()
        if spawner.proc is not None and spawner.proc.poll() is None:
            with suppress(Exception):
                spawner.proc.kill()
            with suppress(Exception):
                spawner.proc.wait(timeout=5)


def _wait_for(predicate, timeout=3.0, interval=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    items = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return items


def _methods(path: Path):
    return [m.get("method") for m in _read_jsonl(path) if isinstance(m, dict)]


def _wait_dead(client, spawner, timeout=3.0):
    assert _wait_for(
        lambda: client.alive is False and spawner.proc.poll() is not None,
        timeout,
    ), "进程应已被终止并收尸"


def test_a1_handshake_and_initialized(tmp_path):
    with _client(tmp_path, "ok") as (client, spawner):
        result = client.initialize()
        assert result["protocolVersion"] == DEFAULT_PROTOCOL_VERSION
        assert client.alive is True
        assert client.pid == spawner.proc.pid

        path = spawner.record_dir / "messages.jsonl"
        assert _wait_for(
            lambda: {"initialize", "notifications/initialized"}
            <= set(_methods(path))
        ), f"server 未记录到 initialize/initialized: {_methods(path)}"

        messages = _read_jsonl(path)
        init = next(m for m in messages if m.get("method") == "initialize")
        params = init["params"]
        assert params["protocolVersion"] == DEFAULT_PROTOCOL_VERSION
        assert params["capabilities"] == {}
        assert params["clientInfo"]["name"] == "ai-assistant"
        version = params["clientInfo"]["version"]
        assert isinstance(version, str) and version
        assert not version.startswith("v")  # 前导 v 必须去掉

        initialized = next(
            m for m in messages if m.get("method") == "notifications/initialized"
        )
        assert "id" not in initialized


def test_a2_version_mismatch_kills(tmp_path):
    with _client(tmp_path, "version_mismatch") as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        err = ei.value
        assert err.retryable is False
        assert "协议版本" in str(err)
        _wait_dead(client, spawner)


def test_a3_refused_initialize_keeps_code(tmp_path):
    with _client(tmp_path, "refuse_init") as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        err = ei.value
        assert err.jsonrpc_code == -32000
        assert "refuses initialize" in str(err)
        assert err.retryable is False
        # JSON-RPC 拒绝不杀进程（由 supervisor 决定是否重启）。
        assert client.alive is True


def test_a4_two_pages_of_tools(tmp_path):
    with _client(tmp_path, "ok") as (client, _spawner):
        client.initialize()
        tools = client.list_tools()
        assert [t["name"] for t in tools] == ["echo", "add", "fail", "boom"]
        assert all(isinstance(t, dict) for t in tools)


def test_a5_six_pages_rejected(tmp_path):
    with _client(tmp_path, "paginate_more") as (client, spawner):
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.list_tools()
        assert "分页超过上限" in str(ei.value)
        _wait_dead(client, spawner)


def test_a6_too_many_tools_rejected(tmp_path):
    with _client(tmp_path, "toomany") as (client, spawner):
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.list_tools()
        assert "工具数超过上限" in str(ei.value)
        _wait_dead(client, spawner)


def test_a7_cursor_repeat_is_loop_error(tmp_path):
    with _client(tmp_path, "cursor_repeat") as (client, spawner):
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.list_tools()
        assert "cursor 重复" in str(ei.value)
        _wait_dead(client, spawner)


def test_a8_call_echo_chinese_and_newlines(tmp_path):
    with _client(tmp_path, "ok") as (client, _spawner):
        client.initialize()
        payload = "中文第一行\n第二行\t制表"
        result = client.call_tool("echo", {"text": payload})
        assert result["content"][0]["text"] == payload
        assert client.call_tool("add", {"a": 2, "b": 40})["content"][0]["text"] == "42"


def test_a9_iserror_keeps_process_alive(tmp_path):
    with _client(tmp_path, "ok") as (client, _spawner):
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.call_tool("fail", {"reason": "示例失败"})
        err = ei.value
        assert err.retryable is False
        assert "示例失败" in str(err)
        assert client.alive is True
        # 进程仍 ready：随后一次正常调用必须成功。
        assert (
            client.call_tool("echo", {"text": "still alive"})["content"][0]["text"]
            == "still alive"
        )


def test_a10_jsonrpc_error_keeps_code_and_process(tmp_path):
    with _client(tmp_path, "ok") as (client, _spawner):
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.call_tool("boom", {})
        err = ei.value
        assert err.jsonrpc_code == -32000
        assert err.retryable is False
        assert client.alive is True
        assert (
            client.call_tool("echo", {"text": "after boom"})["content"][0]["text"]
            == "after boom"
        )


def test_b1_wrong_id_discarded_then_matched(tmp_path):
    with _client(tmp_path, "wrongid") as (client, _spawner):
        result = client.initialize()
        # 错 id 的响应携带坏协议版本；若被当成 initialize 的结果就会在这里红。
        assert result["protocolVersion"] == DEFAULT_PROTOCOL_VERSION
        assert client.stats["stray_responses"] >= 1
        tools = client.list_tools()
        assert len(tools) == 4


def test_b1b_late_duplicate_counted_as_stray(tmp_path):
    with _client(tmp_path, "dup") as (client, _spawner):
        client.initialize()
        assert _wait_for(lambda: client.stats["stray_responses"] >= 1, 2.0)
        assert len(client.list_tools()) == 4


def test_b2_bad_json_protocol_error_and_kill(tmp_path):
    with _client(tmp_path, "badjson") as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        assert "协议错误" in str(ei.value)
        _wait_dead(client, spawner)


def test_b3_batch_array_protocol_error_and_kill(tmp_path):
    with _client(tmp_path, "batch") as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        assert "批量" in str(ei.value)
        _wait_dead(client, spawner)


def test_b4_content_length_protocol_error_and_kill(tmp_path):
    with _client(tmp_path, "content_length") as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        assert "Content-Length" in str(ei.value)
        _wait_dead(client, spawner)


def test_b4b_600kb_line_killed_under_two_seconds(tmp_path):
    with _client(tmp_path, "bigline") as (client, spawner):
        started = time.monotonic()
        with pytest.raises(MCPError) as ei:
            client.initialize()
        elapsed = time.monotonic() - started
        assert "上限" in str(ei.value)
        assert elapsed < 2.0, f"600KB 行应在 2s 内被杀，实际 {elapsed:.2f}s"
        _wait_dead(client, spawner, timeout=2.0)


def test_b5_oversize_request_rejected_before_write(tmp_path):
    with _client(tmp_path, "ok") as (client, _spawner):
        client.initialize()
        huge = "a" * 300_000
        with pytest.raises(MCPError) as ei:
            client.call_tool("echo", {"text": huge})
        assert "超过上限" in str(ei.value)
        assert ei.value.retryable is False
        # 写前拒：进程没有被杀，随后的小请求照样可用。
        assert client.alive is True
        assert (
            client.call_tool("echo", {"text": "small"})["content"][0]["text"]
            == "small"
        )


def test_b6_slow_call_timeout_kills_and_releases_lock(tmp_path):
    with _client(tmp_path, "slow_call", call_timeout=0.3) as (client, spawner):
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.call_tool("add", {"a": 1, "b": 1})
        err = ei.value
        assert "结果未知，不要自动重试" in str(err)
        assert err.retryable is False
        _wait_dead(client, spawner)
        # 在途锁必须已释放：再次调用得到「已终止」而不是「正忙」。
        with pytest.raises(MCPError) as second:
            client.call_tool("add", {"a": 1, "b": 1})
        assert "正忙" not in str(second.value)


def test_b6b_slow_init_timeout(tmp_path):
    with _client(tmp_path, "slow_init", handshake_timeout=0.3) as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        assert "结果未知，不要自动重试" in str(ei.value)
        _wait_dead(client, spawner)


def test_b7_eof_is_retryable(tmp_path):
    with _client(tmp_path, "eof") as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        err = ei.value
        assert err.retryable is True
        assert "server 已退出" in str(err)
        assert _wait_for(lambda: spawner.proc.poll() is not None, 3.0)


def test_b10_crlf_is_tolerated(tmp_path):
    with _client(tmp_path, "crlf") as (client, _spawner):
        client.initialize()
        assert len(client.list_tools()) == 4
        assert (
            client.call_tool("echo", {"text": "crlf 中文"})["content"][0]["text"]
            == "crlf 中文"
        )


def test_b10_bom_on_first_line_is_tolerated(tmp_path):
    with _client(tmp_path, "bom") as (client, _spawner):
        assert client.initialize()["protocolVersion"] == DEFAULT_PROTOCOL_VERSION
        assert len(client.list_tools()) == 4


def test_b10b_server_request_gets_32601(tmp_path):
    with _client(tmp_path, "serverreq") as (client, spawner):
        client.initialize()
        answer = spawner.record_dir / "serverreq.json"
        assert _wait_for(lambda: answer.exists() and bool(_read_jsonl(answer)), 3.0)
        entries = _read_jsonl(answer)
        response = entries[0]["response"]
        assert response["id"] == "srv-1"
        assert response["error"]["code"] == -32601
        # 拒绝 server 请求后，正常响应仍能到达。
        assert len(client.list_tools()) == 4


def test_b9_notification_flood_kills(tmp_path):
    with _client(tmp_path, "flood", call_timeout=5.0) as (client, spawner):
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.call_tool("add", {"a": 1, "b": 2})
        assert "通知洪水" in str(ei.value)
        _wait_dead(client, spawner)


def test_close_is_idempotent(tmp_path):
    with _client(tmp_path, "ok") as (client, _spawner):
        client.initialize()
        client.close()
        client.close()
        assert client.alive is False
        with pytest.raises(MCPError):
            client.call_tool("echo", {"text": "after close"})


def test_close_without_start_is_safe(tmp_path):
    client = MCPStdioClient(spawner=lambda: None)
    client.close()
    client.close()
    assert client.alive is False
    with pytest.raises(MCPError):
        client.initialize()


# ---------------------------------------------------------------- 审查补充用例


def _reader_thread(client):
    for thread in client._threads:
        if thread.name.startswith("mcp-stdio-reader-"):
            return thread
    return None


def test_same_id_rejects_equal_values_of_other_types():
    # True == 1、1.0 == 1 在 Python 里都成立，但 JSON-RPC 的 id 必须类型一致。
    assert _same_id(1, 1) is True
    assert _same_id("x", "x") is True
    assert _same_id(True, 1) is False
    assert _same_id(1, True) is False
    assert _same_id(1.0, 1) is False


def test_a2b_non_string_protocol_version_kills(tmp_path):
    with _client(tmp_path, "bad_version_type") as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        err = ei.value
        assert isinstance(err, MCPError)  # 不是 TypeError 逃逸
        assert err.retryable is False
        assert "协议版本" in str(err)
        assert client.stats["protocol_errors"] >= 1
        _wait_dead(client, spawner)


def test_a2c_huge_integer_json_value_kills_and_stops_reader(tmp_path):
    with _client(tmp_path, "hugeint") as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        assert ei.value.retryable is False
        assert "协议错误" in str(ei.value)
        _wait_dead(client, spawner)
        reader = _reader_thread(client)
        assert reader is not None
        assert _wait_for(lambda: not reader.is_alive(), 3.0), "reader 线程应已退出"


def test_a2d_nan_in_result_is_protocol_error(tmp_path):
    with _client(tmp_path, "nanresult") as (client, spawner):
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.call_tool("echo", {"text": "x"})
        assert ei.value.retryable is False
        assert "协议错误" in str(ei.value)
        _wait_dead(client, spawner)


def test_a2e_lone_surrogate_is_protocol_error(tmp_path):
    with _client(tmp_path, "surrogate") as (client, spawner):
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.call_tool("echo", {"text": "x"})
        assert "代理项" in str(ei.value)
        _wait_dead(client, spawner)


def test_a2f_valid_escaped_surrogate_pair_is_still_accepted(tmp_path):
    # ensure_ascii=True 的 server 会把 emoji 转义成合法代理对，不能误杀。
    with _client(tmp_path, "escapedpair") as (client, _spawner):
        client.initialize()
        result = client.call_tool("echo", {"text": "x"})
        assert result["content"][0]["text"] == "😀"
        assert client.alive is True


def test_a2g_deeply_nested_json_kills_instead_of_dead_reader(tmp_path):
    # 1200 层数组（约 2.4KB，远低于 512KB 行限）：json.loads 在 C 层能过，Python 层
    # 递归归一化会 RecursionError。若它逃出 reader 线程，进程还活着但没有读端，
    # 下一次调用只会白等到超时；必须在 <2s 内判协议错误、杀进程、writer 正常退出。
    with _client(tmp_path, "deepnest", handshake_timeout=5.0) as (client, spawner):
        started = time.monotonic()
        with pytest.raises(MCPError) as ei:
            client.initialize()
        elapsed = time.monotonic() - started
        err = ei.value
        assert err.retryable is False
        assert "嵌套" in str(err) and "不可序列化" in str(err)
        assert elapsed < 2.0, f"深层嵌套应在 2s 内判死，实际 {elapsed:.2f}s"
        assert client.stats["protocol_errors"] >= 1
        _wait_dead(client, spawner)
        reader = _reader_thread(client)
        assert reader is not None
        assert _wait_for(lambda: not reader.is_alive(), 3.0), "reader 线程应已退出"


def test_a2h_exponent_overflow_number_is_protocol_error(tmp_path):
    # 1e999 是合法 JSON 数字字面量，json.loads 解析成 inf 且不经过 parse_constant；
    # 只有 allow_nan=False 的严格回写能拦下它。
    with _client(tmp_path, "infresult") as (client, spawner):
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.call_tool("echo", {"text": "x"})
        assert ei.value.retryable is False
        assert "无法严格回写" in str(ei.value)
        assert client.stats["protocol_errors"] >= 1
        _wait_dead(client, spawner)


def test_a4b_empty_next_cursor_is_protocol_error(tmp_path):
    with _client(tmp_path, "empty_cursor") as (client, spawner):
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.list_tools()
        assert "nextCursor" in str(ei.value)
        _wait_dead(client, spawner)


def test_a8b_scalar_call_result_is_protocol_error(tmp_path):
    with _client(tmp_path, "scalarresult") as (client, spawner):
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.call_tool("echo", {"text": "x"})
        assert "result 不是对象" in str(ei.value)
        assert client.stats["protocol_errors"] >= 1
        _wait_dead(client, spawner)


def test_a10b_null_error_is_protocol_error_and_kill(tmp_path):
    with _client(tmp_path, "nullerror") as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        assert "error 字段不是对象" in str(ei.value)
        assert client.stats["protocol_errors"] >= 1
        _wait_dead(client, spawner)


def test_a9b_string_iserror_is_not_tool_error(tmp_path):
    with _client(tmp_path, "str_iserror") as (client, _spawner):
        client.initialize()
        result = client.call_tool("echo", {"text": "x"})
        # 只有严格的布尔 True 才算工具报错；"false" 是真值字符串，但不能误判。
        assert result["isError"] == "false"
        assert result["content"][0]["text"] == "still ok"
        assert client.alive is True


def test_a9c_iserror_text_truncated_to_500(tmp_path):
    with _client(tmp_path, "ok") as (client, _spawner):
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.call_tool("fail", {"reason": "长" * 600})
        text = str(ei.value)
        prefix = "工具 fail 返回错误: "
        assert text.startswith(prefix)
        assert len(text) - len(prefix) <= 500
        assert len(text) - len(prefix) > 400


def test_a9d_integer_iserror_is_not_tool_error(tmp_path):
    # ``isError: 1`` 是真值但不是 True；只有严格的 ``is True`` 才算工具报错，
    # 否则会把成功结果误判成失败。
    with _client(tmp_path, "int_iserror") as (client, _spawner):
        client.initialize()
        result = client.call_tool("fail_int", {"reason": "int"})
        assert result["isError"] == 1
        assert result["content"][0]["text"] == "int isError: int"
        assert client.alive is True


def test_b1c_bool_id_is_stray_not_integer(tmp_path):
    with _client(tmp_path, "trueid", handshake_timeout=1.0) as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        # id: true 不能被当成 id=1 配对；无正确响应，最终走超时并杀进程。
        assert client.stats["stray_responses"] >= 1
        assert client.stats["responses"] == 0
        assert ei.value.retryable is False
        _wait_dead(client, spawner)


def test_b1d_three_stray_responses_are_protocol_error(tmp_path):
    with _client(tmp_path, "stray3") as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        assert "协议失步" in str(ei.value)
        assert client.stats["stray_responses"] >= 3
        _wait_dead(client, spawner)


def test_b6c_close_during_call_wakes_with_closed_error(tmp_path):
    with _client(tmp_path, "slow_call", call_timeout=10.0) as (client, _spawner):
        client.initialize()
        errors = []
        marks = []
        started = [0.0]

        def worker():
            try:
                client.call_tool("add", {"a": 1, "b": 2})
            except MCPError as exc:
                errors.append(exc)
                marks.append(time.monotonic() - started[0])

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert _wait_for(lambda: client.stats["requests_sent"] >= 2, 2.0)
        started[0] = time.monotonic()
        client.close()
        thread.join(timeout=2.0)
        assert not thread.is_alive(), "close() 必须立即唤醒在途调用"
        assert errors and "关闭" in str(errors[0])
        assert marks and marks[0] < 2.0, f"在途调用唤醒耗时 {marks}"
        assert client.alive is False
        # 唤醒之后 close() 已经跑完；再关一次不抛、不二次杀崩。
        client.close()
        assert client.alive is False
        # kills 只钉上限，不钉“必须杀一次”：0 = server 读到 stdin EOF 后在宽限期内
        # 自行退出，close() 的 wait 直接收尸；1 = 进程没退、需要 terminate 收尸。
        # 两种都是正确关闭；强凑 kills==1 就得对已退出进程再动手，会重新引入
        # PID 复用/二次杀的风险。
        assert client.stats["kills"] in (0, 1)


def test_b6d_second_call_while_busy_is_retryable(tmp_path):
    with _client(tmp_path, "slow_call", call_timeout=10.0) as (client, _spawner):
        client.initialize()
        errors = []

        def worker():
            try:
                client.call_tool("add", {"a": 1, "b": 2})
            except MCPError as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        assert _wait_for(lambda: client.stats["requests_sent"] >= 2, 2.0)
        with pytest.raises(MCPError) as ei:
            client.call_tool("echo", {"text": "busy"})
        assert ei.value.retryable is True
        assert "正忙" in str(ei.value)
        client.close()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
        assert errors and "关闭" in str(errors[0])


def test_b7b_eof_with_live_process_kills(tmp_path):
    with _client(tmp_path, "eof_alive") as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        assert "stdout 已关闭但进程仍在运行" in str(ei.value)
        _wait_dead(client, spawner)


def test_b10c_unanswerable_server_request_is_not_silent(tmp_path):
    with _client(tmp_path, "bigreqid") as (client, spawner):
        with pytest.raises(MCPError) as ei:
            client.initialize()
        assert "无法回复 server 请求" in str(ei.value)
        assert client.stats["protocol_errors"] >= 1
        _wait_dead(client, spawner)


def test_a1b_outbound_ids_start_at_one_and_increment(tmp_path):
    with _client(tmp_path, "record") as (client, spawner):
        client.initialize()
        client.list_tools()
        path = spawner.record_dir / "messages.jsonl"
        assert _wait_for(
            lambda: {
                m.get("method")
                for m in _read_jsonl(path)
                if isinstance(m, dict) and m.get("method")
            }
            >= {"initialize", "tools/list"},
            3.0,
        )
        requests = [
            m
            for m in _read_jsonl(path)
            if isinstance(m, dict) and m.get("method") and m.get("id") is not None
        ]
        init = next(m for m in requests if m["method"] == "initialize")
        first_list = next(m for m in requests if m["method"] == "tools/list")
        assert init["id"] == 1
        assert first_list["id"] == 2


# ---------- stderr 脱敏：值不出现、标签保留 ----------
#
# 假密钥一律运行时拼接：整颗字面量会让 tools/secret_scan.py 在自己的测试文件上红。

_FAKE_STDERR_SECRETS = [
    ("sk-", "sk" + "-" + "A" * 24),
    ("ghp_", "ghp" + "_" + "B" * 24),
    ("Bearer ", "Bearer " + "C" * 24),
    ("password=", "password" + "=" + "D" * 16),
    ("AKIA", "AKIA" + "E" * 16),
]


@pytest.mark.parametrize("label, token", _FAKE_STDERR_SECRETS,
                         ids=[label.strip() for label, _ in _FAKE_STDERR_SECRETS])
def test_stderr_sanitizer_hides_each_credential_shape_and_keeps_the_label(label, token):
    cleaned = _sanitize_stderr_line("server 报错：" + token + " 请检查配置")
    value = token[len(label):]
    assert value not in cleaned, f"{label} 的密钥值没有被打码"
    assert label in cleaned, f"{label} 标签被一起吃掉了"
