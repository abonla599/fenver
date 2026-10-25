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

全部落点用 pytest 的 ``tmp_path``；每个用例在 ``finally`` 里 ``close()`` 并补杀进程。
"""
import json
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from app.core.mcp_client import (
    DEFAULT_PROTOCOL_VERSION,
    MCPError,
)
from app.core.mcp_stdio_client import MCPStdioClient

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
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(SERVER), self.mode, str(self.record_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(self.record_dir.parent),
        )
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
