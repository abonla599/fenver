# backend/tests/test_v0372_mcp_client.py
"""v0.37.2 MCP HTTP/SSE 传输层测试。

假 MCP server 用标准库 ThreadingHTTPServer 起在 127.0.0.1 随机端口，
全部行为由测试内配置驱动，不依赖任何外部 MCP 实现。
"""
from __future__ import annotations

import gzip
import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.core.mcp_client import (
    DEFAULT_PROTOCOL_VERSION,
    MCPClient,
    MCPConnectionBudget,
    MCPError,
    _effective_origin,
)

# ---------------------------------------------------------------------------
# 假 server（Streamable HTTP）


def _tool(i: int) -> dict:
    return {
        "name": f"tool_{i}",
        "description": f"第 {i} 个工具",
        "inputSchema": {"type": "object"},
    }


class _QuietServer(ThreadingHTTPServer):
    """断连（客户端提前中止、慢滴超时）不算错误，别往 stderr 打 traceback。"""

    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


class StreamConfig:
    """假 server 的行为旋钮。"""

    def __init__(self, **kw) -> None:
        self.protocol_version = kw.get("protocol_version", DEFAULT_PROTOCOL_VERSION)
        self.session_id = kw.get("session_id", "sess-0001")
        self.enforce_session = kw.get("enforce_session", True)
        self.pages = kw.get("pages")  # list[list[tool]]，None → 单页 2 工具
        self.call_mode = kw.get("call_mode", "ok")  # ok | is_error | jsonrpc_error
        self.response_mode = kw.get("response_mode", "json")  # json | sse
        self.sse_extra_events = kw.get("sse_extra_events", False)
        self.delay = kw.get("delay", 0.0)
        self.override_status = kw.get("override_status")  # {method 或 '*': int}
        self.raw_body_bytes = kw.get("raw_body_bytes")  # tools/list 超限响应
        # 该 method 的请求在会话校验前先把 server 侧会话 id 换掉 ⇒ 恒 404
        self.rotate_for = kw.get("rotate_for")
        self.redirect_location = kw.get("redirect_location", "/next")
        self.set_cookie = kw.get("set_cookie")
        # 慢滴：{"bytes": 8, "interval": 0.05, "duration": 1.0}，只作用于 initialize
        self.drip = kw.get("drip")
        # (started_event, release_event)：initialize 请求进来先卡住，便于并发测试定序
        self.init_gate = kw.get("init_gate")
        self.wrong_id_for = kw.get("wrong_id_for")  # 该 method 的响应 id 换成 999999
        self.bad_tools = kw.get("bad_tools", False)  # tools 数组里混非对象
        self.bad_cursor = kw.get("bad_cursor", False)  # nextCursor 非字符串


class StreamFake:
    def __init__(self, cfg: StreamConfig, tracker: dict | None = None) -> None:
        self.cfg = cfg
        self.tracker = tracker  # 跨多个 fake 共享的并发计数 dict
        self.lock = threading.Lock()
        self.current = 0
        self.max_concurrent = 0
        self.requests: list[dict] = []  # {method, headers, payload}
        self.visited: list[str] = []  # 所有物理请求的 path（重定向测试用）
        self.session = cfg.session_id
        self.httpd = _QuietServer(("127.0.0.1", 0), _make_stream_handler())
        self.httpd.fake = self  # type: ignore[attr-defined]
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/mcp"

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.httpd.server_address[:2]
        return str(host), int(port)

    def count(self, method: str) -> int:
        with self.lock:
            return sum(1 for r in self.requests if r["method"] == method)

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _make_stream_handler():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静音
            pass

        def _send(self, status: int, body: bytes, ctype: str, extra_headers=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _send_jsonrpc(self, body_dict, status=200, ctype="application/json", extra_headers=None):
            fake = self.server.fake  # type: ignore[attr-defined]
            if ctype == "text/event-stream":
                lines = []
                if fake.cfg.sse_extra_events:
                    lines += ['event: ping', 'data: {"note":"ignore me"}', ""]
                lines += ["event: message", f"data: {json.dumps(body_dict)}", ""]
                body = "\n".join(lines).encode("utf-8")
            else:
                body = json.dumps(body_dict).encode("utf-8")
            self._send(status, body, ctype, extra_headers)

        def do_GET(self):
            fake = self.server.fake  # type: ignore[attr-defined]
            with fake.lock:
                fake.visited.append(self.path)
            self._send(501, b'{"error":"fake has no GET"}', "application/json")

        def do_POST(self):
            fake = self.server.fake  # type: ignore[attr-defined]
            cfg = fake.cfg
            tr = fake.tracker
            with fake.lock:
                fake.current += 1
                fake.max_concurrent = max(fake.max_concurrent, fake.current)
                fake.visited.append(self.path)
            if tr is not None:
                with tr["lock"]:
                    tr["cur"] += 1
                    tr["max"] = max(tr["max"], tr["cur"])
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                header_snapshot = {k.lower(): v for k, v in self.headers.items()}
                try:
                    payload = json.loads(raw)
                except ValueError:
                    payload = {}
                method = payload.get("method")
                with fake.lock:
                    fake.requests.append(
                        {"method": method, "headers": header_snapshot, "payload": payload}
                    )
                if cfg.delay:
                    time.sleep(cfg.delay)
                if cfg.rotate_for and method == cfg.rotate_for:
                    fake.session = f"rot-{len(fake.requests)}"
                override = (cfg.override_status or {}).get(
                    method, (cfg.override_status or {}).get("*")
                )
                if override:
                    extra = {}
                    if 300 <= override < 400:
                        extra["Location"] = cfg.redirect_location
                    self._send(override, b'{"oops":1}', "application/json", extra)
                    return
                if cfg.drip and method == "initialize":
                    # 慢滴：200 + 每 interval 秒 8 字节，持续 duration 秒后收尾。
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    chunk = b"x" * cfg.drip["bytes"]
                    until = time.monotonic() + cfg.drip["duration"]
                    try:
                        while time.monotonic() < until:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            time.sleep(cfg.drip["interval"])
                    except OSError:
                        pass
                    return
                # 会话校验：非 initialize 请求必须带上当前有效会话 id
                if (
                    cfg.enforce_session
                    and method not in (None, "initialize")
                    and self.headers.get("Mcp-Session-Id") != fake.session
                ):
                    self._send(404, b'{"error":"no such session"}', "application/json")
                    return
                if method == "initialize":
                    if cfg.init_gate is not None:
                        started, release = cfg.init_gate
                        started.set()
                        release.wait(5.0)
                    fake.session = cfg.session_id
                    resp = {
                        "jsonrpc": "2.0",
                        "id": payload.get("id"),
                        "result": {
                            "protocolVersion": cfg.protocol_version,
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": "fake-mcp", "version": "0.0.1"},
                        },
                    }
                    ctype = (
                        "text/event-stream"
                        if cfg.response_mode == "sse"
                        else "application/json"
                    )
                    extra = {"Mcp-Session-Id": fake.session}
                    if cfg.set_cookie:
                        extra["Set-Cookie"] = cfg.set_cookie
                    self._send_jsonrpc(resp, ctype=ctype, extra_headers=extra)
                    return
                if method == "notifications/initialized":
                    self._send(202, b"", "application/json")
                    return
                if method == "tools/list":
                    if cfg.raw_body_bytes:
                        pad = b"x" * cfg.raw_body_bytes
                        big = (
                            b'{"jsonrpc":"2.0","id":'
                            + str(payload.get("id")).encode()
                            + b',"result":{"pad":"'
                            + pad
                            + b'"}}'
                        )
                        self._send(200, big, "application/json")
                        return
                    pages = cfg.pages if cfg.pages is not None else [[_tool(1), _tool(2)]]
                    if cfg.bad_tools:
                        pages = [[_tool(1), "not-an-object"]]
                    cursor = (payload.get("params") or {}).get("cursor")
                    idx = 0
                    if cursor is not None:
                        try:
                            idx = int(cursor.split("-")[1])
                        except (ValueError, IndexError):
                            self._send_jsonrpc(
                                {
                                    "jsonrpc": "2.0",
                                    "id": payload.get("id"),
                                    "error": {"code": -32602, "message": "bad cursor"},
                                }
                            )
                            return
                    result: dict = {"tools": pages[idx] if idx < len(pages) else []}
                    if idx + 1 < len(pages):
                        result["nextCursor"] = f"cursor-{idx + 1}"
                    if cfg.bad_cursor:
                        result["nextCursor"] = 5
                    rid = 999999 if cfg.wrong_id_for == "tools/list" else payload.get("id")
                    self._send_jsonrpc(
                        {"jsonrpc": "2.0", "id": rid, "result": result},
                        ctype=(
                            "text/event-stream"
                            if cfg.response_mode == "sse"
                            else "application/json"
                        ),
                    )
                    return
                if method == "tools/call":
                    if cfg.call_mode == "is_error":
                        result = {
                            "isError": True,
                            "content": [{"type": "text", "text": "工具内部炸了"}],
                        }
                    elif cfg.call_mode == "jsonrpc_error":
                        self._send_jsonrpc(
                            {
                                "jsonrpc": "2.0",
                                "id": payload.get("id"),
                                "error": {"code": -32603, "message": "内部错误"},
                            }
                        )
                        return
                    else:
                        result = {"content": [{"type": "text", "text": "42"}], "isError": False}
                    self._send_jsonrpc(
                        {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}
                    )
                    return
                self._send_jsonrpc(
                    {
                        "jsonrpc": "2.0",
                        "id": payload.get("id"),
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )
            finally:
                with fake.lock:
                    fake.current -= 1
                if tr is not None:
                    with tr["lock"]:
                        tr["cur"] -= 1

    return Handler


@pytest.fixture()
def fake_server():
    fakes: list[StreamFake] = []

    def start(tracker=None, **cfg_kw) -> StreamFake:
        f = StreamFake(StreamConfig(**cfg_kw), tracker=tracker)
        fakes.append(f)
        return f

    yield start
    for f in fakes:
        f.stop()


def make_client(fake: StreamFake, *, budget=None, **kw) -> MCPClient:
    kw.setdefault("budget", budget or MCPConnectionBudget())
    return MCPClient(fake.url, **kw)


class RawFake:
    """initialize / notifications 固定行为，其余请求交给 ``raw_fn(handler, payload)``。

    用来构造标准 StreamFake 表达不了的响应体（gzip 炸弹、深层嵌套 JSON、各类 SSE
    帧、不终止的超限流）。
    """

    def __init__(self, raw_fn) -> None:
        self.raw_fn = raw_fn
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.httpd = _QuietServer(("127.0.0.1", 0), self._handler())
        self.httpd.rawfake = self  # type: ignore[attr-defined]
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/mcp"

    def count(self, method: str) -> int:
        with self.lock:
            return sum(1 for r in self.requests if r["method"] == method)

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self):
        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status, body, ctype, extra=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_POST(self):
                fake = self.server.rawfake  # type: ignore[attr-defined]
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                method = payload.get("method")
                with fake.lock:
                    fake.requests.append({"method": method, "payload": payload})
                if method == "initialize":
                    body = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": payload.get("id"),
                            "result": {
                                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                                "capabilities": {},
                                "serverInfo": {"name": "raw", "version": "1"},
                            },
                        }
                    ).encode()
                    self._send(200, body, "application/json", {"Mcp-Session-Id": "raw-1"})
                    return
                if method == "notifications/initialized":
                    self._send(202, b"", "application/json")
                    return
                fake.raw_fn(self, payload)

        return H


# ---------------------------------------------------------------------------
# P0：握手 / 列表 / 调用


def test_initialize_and_headers(fake_server):
    fake = fake_server()
    with make_client(fake, headers={"Authorization": "Bearer tok-abc"}) as client:
        result = client.initialize()
        assert result["protocolVersion"] == DEFAULT_PROTOCOL_VERSION
        assert client._session_id == "sess-0001"
        tools = client.list_tools()
        assert [t["name"] for t in tools] == ["tool_1", "tool_2"]
    init_req = next(r for r in fake.requests if r["method"] == "initialize")
    assert init_req["headers"]["authorization"] == "Bearer tok-abc"
    assert "mcp-session-id" not in init_req["headers"]  # 握手请求还没会话
    notif = next(r for r in fake.requests if r["method"] == "notifications/initialized")
    assert "id" not in notif["payload"]  # 通知不带 JSON-RPC id
    assert notif["headers"]["mcp-session-id"] == "sess-0001"
    list_req = next(r for r in fake.requests if r["method"] == "tools/list")
    # 握手后的请求必须带协议版本与会话 id
    assert list_req["headers"]["mcp-protocol-version"] == DEFAULT_PROTOCOL_VERSION
    assert list_req["headers"]["mcp-session-id"] == "sess-0001"
    assert list_req["headers"]["accept"] == "application/json, text/event-stream"
    assert list_req["headers"]["content-type"] == "application/json"


def test_unknown_protocol_version_fail_closed(fake_server):
    fake = fake_server(protocol_version="2024-11-05")
    with make_client(fake) as client:
        with pytest.raises(MCPError, match="不受支持"):
            client.initialize()
        assert not client._ready


def test_list_tools_pagination(fake_server):
    pages = [[_tool(i) for i in range(1, 21)] for _ in range(3)]
    fake = fake_server(pages=pages)
    with make_client(fake) as client:
        tools = client.list_tools()
    assert len(tools) == 60
    assert fake.count("tools/list") == 3


def test_list_tools_page_cap_stops(fake_server):
    pages = [[_tool(1)] for _ in range(7)]
    fake = fake_server(pages=pages)
    with make_client(fake) as client:
        with pytest.raises(MCPError, match="分页超过上限"):
            client.list_tools()
    assert fake.count("tools/list") == 5


def test_list_tools_count_cap(fake_server):
    pages = [[_tool(i) for i in range(1, 41)] for _ in range(2)]  # 40+40 > 64
    fake = fake_server(pages=pages)
    with make_client(fake) as client:
        with pytest.raises(MCPError, match="工具数超过上限"):
            client.list_tools()
    assert fake.count("tools/list") == 2


def test_call_tool_success(fake_server):
    fake = fake_server()
    with make_client(fake) as client:
        result = client.call_tool("tool_1", {"a": 1})
        assert result["content"][0]["text"] == "42"
        call = next(r for r in fake.requests if r["method"] == "tools/call")
        assert call["payload"]["params"] == {"name": "tool_1", "arguments": {"a": 1}}


def test_call_tool_is_error_no_retry(fake_server):
    fake = fake_server(call_mode="is_error")
    with make_client(fake) as client:
        with pytest.raises(MCPError, match="工具内部炸了") as ei:
            client.call_tool("tool_1")
        assert not ei.value.retryable
    assert fake.count("tools/call") == 1


def test_call_tool_jsonrpc_error_no_retry(fake_server):
    fake = fake_server(call_mode="jsonrpc_error")
    with make_client(fake) as client:
        with pytest.raises(MCPError) as ei:
            client.call_tool("tool_1")
        assert ei.value.jsonrpc_code == -32603
        assert not ei.value.retryable
    assert fake.count("tools/call") == 1


def test_jsonrpc_error_code_mapping(fake_server):
    fake = fake_server()
    with make_client(fake) as client:
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client._rpc("bogus/method", {})
        assert ei.value.jsonrpc_code == -32601


def test_client_info_uses_constructor_args(fake_server):
    fake = fake_server()
    with make_client(fake, client_name="my-client", client_version="9.9.9") as client:
        client.initialize()
    init_req = next(r for r in fake.requests if r["method"] == "initialize")
    assert init_req["payload"]["params"]["clientInfo"] == {
        "name": "my-client",
        "version": "9.9.9",
    }


def test_legacy_endpoint_state_removed(fake_server):
    fake = fake_server()
    with make_client(fake) as client:
        assert not hasattr(client, "_legacy_endpoint")


# ---------------------------------------------------------------------------
# B1：URL 凭据与日志


@pytest.mark.parametrize(
    "kind,secret",
    [
        ("userinfo", "supersecret"),
        ("query", "query-secret"),
        ("fragment", "frag-secret"),
    ],
)
def test_url_credentials_rejected(fake_server, caplog, kind, secret):
    fake = fake_server()
    host, port = fake.address
    caplog.set_level("DEBUG")
    if kind == "userinfo":
        url = f"http://alice:{secret}@{host}:{port}/mcp"
    elif kind == "query":
        url = f"http://{host}:{port}/mcp?token={secret}"
    else:
        url = f"http://{host}:{port}/mcp#{secret}"
    with pytest.raises(MCPError) as ei:
        MCPClient(url, budget=MCPConnectionBudget())
    assert not ei.value.retryable
    assert secret not in caplog.text
    assert secret not in str(ei.value)
    assert fake.requests == []


def test_server_id_and_log_exclude_credentials(fake_server, caplog):
    fake = fake_server()
    host, port = fake.address
    caplog.set_level("DEBUG")
    with make_client(fake) as client:
        client.initialize()
        assert client._server_id == f"{host}:{port}/mcp"
        assert "@" not in client._server_id
        assert "?" not in client._server_id
        assert "#" not in client._server_id
    # 正面控制：日志确实产生了，且只出现干净的 server 标识
    assert "MCP 握手完成" in caplog.text
    assert f"MCP 握手完成: {host}:{port}/mcp" in caplog.text


# ---------------------------------------------------------------------------
# P0：会话恢复 / 404


def test_session_404_reinitializes_once(fake_server):
    fake = fake_server()
    with make_client(fake) as client:
        client.initialize()
        assert fake.count("initialize") == 1
        # server 端会话轮换 → 旧会话 id 全部 404 → 客户端只重初始化一次
        fake.session = "sess-rotated"
        tools = client.list_tools()
        assert [t["name"] for t in tools] == ["tool_1", "tool_2"]
        assert fake.count("initialize") == 2
        assert fake.count("tools/list") == 2


def test_persistent_404_gives_up(fake_server):
    fake = fake_server(rotate_for="tools/list")
    with make_client(fake) as client:
        client.initialize()
        with pytest.raises(MCPError, match="持续无效"):
            client.list_tools()
        # 原始一次 + 重初始化后一次，没有第三次
        assert fake.count("tools/list") == 2
        assert fake.count("initialize") == 2


def test_call_tool_does_not_recover_session(fake_server):
    fake = fake_server()
    with make_client(fake) as client:
        client.initialize()
        fake.session = "rotated"
        with pytest.raises(MCPError, match="不允许重初始化"):
            client.call_tool("tool_1")
        assert fake.count("tools/call") == 1
        assert fake.count("initialize") == 1


def test_call_tool_404_resets_session_for_next_call(fake_server):
    """B4：404 后必须复位会话状态，否则该实例的 call_tool 永久失败。"""
    fake = fake_server()
    with make_client(fake) as client:
        client.initialize()
        assert fake.count("initialize") == 1
        fake.session = "rotated"
        with pytest.raises(MCPError, match="不允许重初始化"):
            client.call_tool("tool_1")
        assert fake.count("tools/call") == 1
        assert fake.count("initialize") == 1
        # 第二次调用：先重新握手（init 1→2），再成功；不再永久失败
        result = client.call_tool("tool_1")
        assert result["content"][0]["text"] == "42"
        assert fake.count("initialize") == 2
        assert fake.count("tools/call") == 2
        # tools/call 本身没有被重放（第一次失败后只发过一次）
        assert fake.count("notifications/initialized") == 2


def test_initialized_notification_404_is_bounded(fake_server):
    """B3：initialized 恒 404 时必须抛 MCPError，且请求次数有界（不得递归）。"""
    fake = fake_server(override_status={"notifications/initialized": 404})
    with make_client(fake) as client:
        with pytest.raises(MCPError) as ei:
            client.initialize()
        assert not isinstance(ei.value, RecursionError)
        assert not ei.value.retryable
        # 状态不残留（N6）
        assert client._ready is False
        assert client._session_id is None
        assert client._protocol_version is None
    assert fake.count("initialize") == 2
    assert fake.count("notifications/initialized") == 2


# ---------------------------------------------------------------------------
# B5：初始化线程安全


def test_concurrent_first_list_tools_initializes_once(fake_server):
    fake = fake_server(delay=0.1)
    budget = MCPConnectionBudget(global_limit=4, per_server_limit=2)
    with make_client(fake, budget=budget) as client:
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        results: list[list] = []

        def worker():
            barrier.wait()
            try:
                results.append(client.list_tools())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []
    assert len(results) == 2
    assert fake.count("initialize") == 1
    assert fake.count("notifications/initialized") == 1


def test_explicit_initialize_racing_list_tools_handshakes_once(fake_server):
    started = threading.Event()
    release = threading.Event()
    fake = fake_server(init_gate=(started, release))
    budget = MCPConnectionBudget(global_limit=4, per_server_limit=2)
    with make_client(fake, budget=budget) as client:
        errors: list[BaseException] = []

        def do_init():
            try:
                client.initialize()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def do_list():
            try:
                client.list_tools()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=do_init)
        t1.start()
        assert started.wait(5.0)
        t2 = threading.Thread(target=do_list)
        t2.start()
        time.sleep(0.1)  # 让 list_tools 抵达初始化锁
        release.set()
        t1.join(10)
        t2.join(10)
    assert errors == []
    assert fake.count("initialize") == 1
    assert fake.count("notifications/initialized") == 1


# ---------------------------------------------------------------------------
# B6：墙钟总超时


def test_total_timeout_aborts_slow_drip_and_releases_slot(fake_server):
    fake = fake_server(drip={"bytes": 8, "interval": 0.05, "duration": 1.0})
    budget = MCPConnectionBudget(global_limit=1, per_server_limit=1)
    with make_client(
        fake,
        budget=budget,
        total_timeout=0.3,
        timeout=httpx.Timeout(0.2, connect=0.2, read=0.1),
    ) as client:
        started = time.monotonic()
        with pytest.raises(MCPError, match="总时限") as ei:
            client.initialize()
        elapsed = time.monotonic() - started
        assert ei.value.retryable
        assert elapsed < 0.9, f"慢滴没有在总时限附近中止: {elapsed:.3f}s"
        assert elapsed >= 0.25, f"不该在 deadline 之前中止: {elapsed:.3f}s"
        # 槽位已释放（否则非阻塞取不到）
        assert budget._global_sem.acquire(blocking=False) is True
        budget._global_sem.release()
        # 同一实例恢复正常响应后还能继续用（不被占死）
        fake.cfg.drip = None
        assert client.initialize()["protocolVersion"] == DEFAULT_PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# P0：超时 / 超限 / 守卫 / 重定向 / 环境凭据 / 日志脱敏


def test_timeout_is_retryable(fake_server):
    fake = fake_server(delay=1.0)
    with make_client(
        fake, timeout=httpx.Timeout(2.0, connect=1.0, read=0.25)
    ) as client:
        with pytest.raises(MCPError, match="超时") as ei:
            client.initialize()
    assert ei.value.retryable


def test_oversize_response_rejected(fake_server):
    fake = fake_server(raw_body_bytes=600 * 1024)
    with make_client(fake) as client:
        client.initialize()
        with pytest.raises(MCPError, match="超过字节上限"):
            client.list_tools()


def test_gzip_bomb_rejected_after_decompression():
    captured: dict = {}

    def raw_fn(handler, payload):
        raw = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "result": {"pad": "x" * (600 * 1024)},
            }
        ).encode()
        comp = gzip.compress(raw, 9)
        captured["comp"] = len(comp)
        captured["raw"] = len(raw)
        handler._send(200, comp, "application/json", {"Content-Encoding": "gzip"})

    fake = RawFake(raw_fn)
    try:
        with MCPClient(fake.url, budget=MCPConnectionBudget()) as client:
            with pytest.raises(MCPError, match="超过字节上限"):
                client.list_tools()
    finally:
        fake.stop()
    # 压缩后 <1KB、解压 600KB：限额必须在解压后、逐块生效
    assert captured["comp"] < 1024
    assert captured["raw"] > 512 * 1024


def test_unterminated_oversize_stream_aborted_early():
    """区分"逐块中断"与"读完再判"：服务端声明 1MB 却只发 640KB 就断开。

    逐块读取在 512KB 处就抛"超过字节上限"；``resp.read()`` 实现会先撞上
    连接提前关闭（RemoteProtocolError → 传输层错误），从而被这条测试判红。
    """

    def raw_fn(handler, payload):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(1024 * 1024))
        handler.end_headers()
        chunk = b"x" * (64 * 1024)
        try:
            for _ in range(10):  # 640KB 后直接断开
                handler.wfile.write(chunk)
                handler.wfile.flush()
                time.sleep(0.015)
        except OSError:
            pass
        handler.close_connection = True

    fake = RawFake(raw_fn)
    try:
        with MCPClient(fake.url, budget=MCPConnectionBudget()) as client:
            with pytest.raises(MCPError, match="超过字节上限"):
                client.list_tools()
    finally:
        fake.stop()


def test_url_guard_blocks_before_any_request(fake_server):
    fake = fake_server()
    with pytest.raises(MCPError, match="私网地址") as ei:
        MCPClient(fake.url, url_guard=lambda url: "私网地址不在白名单")
    assert not ei.value.retryable
    assert fake.requests == []

    # 构造放行、请求前再问一遍：策略中途收紧也必须停
    calls = {"n": 0}

    def guard(url):
        calls["n"] += 1
        return None if calls["n"] == 1 else "策略收紧"

    fake2 = fake_server()
    with make_client(fake2, url_guard=guard) as client:
        with pytest.raises(MCPError, match="被 URL 守卫拒绝"):
            client.initialize()
    assert fake2.requests == []


def test_redirect_not_followed(fake_server):
    fake = fake_server(override_status={"initialize": 302}, redirect_location="/next")
    with make_client(fake) as client:
        with pytest.raises(MCPError, match="重定向"):
            client.initialize()
    # 302 必须带 Location，且目标绝不能被访问（变异 follow_redirects=True 会红）
    assert "/next" not in fake.visited
    assert fake.visited == ["/mcp"]


def test_env_proxy_not_used(fake_server, monkeypatch):
    # trust_env=False：哪怕 HTTP_PROXY 指向黑洞端口也直连成功
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    fake = fake_server()
    with make_client(fake) as client:
        assert client.list_tools()


def test_token_never_logged(fake_server, caplog):
    secret = "super-secret-token-9f3a"
    fake = fake_server(call_mode="jsonrpc_error")
    caplog.set_level("DEBUG")
    with make_client(fake, headers={"Authorization": f"Bearer {secret}"}) as client:
        client.initialize()
        with pytest.raises(MCPError) as ei:
            client.call_tool("tool_1")
    # 正面控制：先证明日志真的产生了，再断言秘密不在里面
    assert "MCP 握手完成" in caplog.text
    assert secret not in caplog.text
    assert secret not in str(ei.value)
    for record in caplog.records:
        assert secret not in record.getMessage()


def test_server_set_cookie_not_echoed(fake_server):
    fake = fake_server(set_cookie="sid=from-server; Path=/")
    with make_client(fake) as client:
        client.initialize()
        client.list_tools()
    list_req = next(r for r in fake.requests if r["method"] == "tools/list")
    assert "cookie" not in list_req["headers"]
    # 对照：服务端确实下发过 Set-Cookie
    init_req = next(r for r in fake.requests if r["method"] == "initialize")
    assert init_req["headers"].get("cookie") is None


def test_public_calls_after_close_raise_mcp_error(fake_server):
    fake = fake_server()
    client = make_client(fake)
    client.close()
    for call in (
        client.initialize,
        client.list_tools,
        lambda: client.call_tool("tool_1"),
    ):
        with pytest.raises(MCPError, match="客户端已关闭"):
            call()
    assert fake.requests == []


# ---------------------------------------------------------------------------
# P1：SSE 响应（Streamable HTTP）


def test_sse_json_message_response(fake_server):
    fake = fake_server(response_mode="sse")
    with make_client(fake) as client:
        client.initialize()  # initialize 本身走 SSE 回包
        tools = client.list_tools()
        assert [t["name"] for t in tools] == ["tool_1", "tool_2"]


def test_sse_unknown_events_ignored(fake_server):
    fake = fake_server(response_mode="sse", sse_extra_events=True)
    with make_client(fake) as client:
        client.initialize()
        assert len(client.list_tools()) == 2


def test_json_response_id_mismatch_rejected(fake_server):
    fake = fake_server(wrong_id_for="tools/list")
    with make_client(fake) as client:
        client.initialize()
        with pytest.raises(MCPError, match="id 与请求不匹配"):
            client.list_tools()


def _sse_frame(kind: str, rid) -> bytes:
    msg = json.dumps({"jsonrpc": "2.0", "id": rid, "result": {"tools": [_tool(1)]}})
    if kind == "lf":
        return f"event: message\ndata: {msg}\n\n".encode()
    if kind == "crlf":
        return f"event: message\r\ndata: {msg}\r\n\r\n".encode()
    if kind == "cr":
        return f"event: message\rdata: {msg}\r\r".encode()
    if kind == "multiline":
        head, tail = msg.split('"id"', 1)
        return f'event: message\ndata: {head}\ndata: "id"{tail}\n\n'.encode()
    if kind == "comment_empty":
        return f": keepalive\n\ndata:\n\nevent: message\ndata: {msg}\n\n".encode()
    if kind == "event_error":
        return (
            'event: error\ndata: {"code":-32000}\n\n'
            f"event: message\ndata: {msg}\n\n"
        ).encode()
    raise AssertionError(f"unknown kind {kind}")


@pytest.mark.parametrize(
    "kind", ["lf", "crlf", "cr", "multiline", "comment_empty", "event_error"]
)
def test_sse_line_endings_and_frames(kind):
    def raw_fn(handler, payload):
        handler._send(
            200, _sse_frame(kind, payload.get("id")), "text/event-stream"
        )

    fake = RawFake(raw_fn)
    try:
        with MCPClient(fake.url, budget=MCPConnectionBudget()) as client:
            tools = client.list_tools()
            assert [t["name"] for t in tools] == ["tool_1"]
    finally:
        fake.stop()


def test_sse_event_too_large():
    class BigSSEHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length))
            method = payload.get("method")
            if method == "initialize":
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": payload.get("id"),
                        "result": {
                            "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                            "capabilities": {},
                            "serverInfo": {"name": "big", "version": "1"},
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Mcp-Session-Id", "s1")
                self.end_headers()
                self.wfile.write(body)
                return
            if method and method.startswith("notifications/"):
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            pad = "y" * (70 * 1024)  # > 64KB 单事件上限
            body = (
                'event: message\ndata: {"jsonrpc":"2.0","id":'
                + str(payload.get("id"))
                + ',"result":{"pad":"'
                + pad
                + '"}}\n\n'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = _QuietServer(("127.0.0.1", 0), BigSSEHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://{srv.server_address[0]}:{srv.server_address[1]}/mcp"
        with MCPClient(url, budget=MCPConnectionBudget()) as client:
            client.initialize()
            with pytest.raises(MCPError, match="单个 SSE 事件"):
                client.list_tools()
    finally:
        srv.shutdown()
        srv.server_close()


def test_deeply_nested_json_is_mcp_error():
    def raw_fn(handler, payload):
        body = (
            '{"jsonrpc":"2.0","id":'
            + json.dumps(payload.get("id"))
            + ',"result":'
            + "[" * 20000
            + "]" * 20000
            + "}"
        ).encode()
        handler._send(200, body, "application/json")

    fake = RawFake(raw_fn)
    try:
        with MCPClient(fake.url, budget=MCPConnectionBudget()) as client:
            with pytest.raises(MCPError, match="不是合法 JSON"):
                client.list_tools()
    finally:
        fake.stop()


# ---------------------------------------------------------------------------
# P1：legacy SSE 兜底


class LegacyFake:
    def __init__(self, *, endpoint="/messages/{sid}", silent_get=False, post_status=202):
        self.lock = threading.Lock()
        self.pending: dict[str, queue.Queue] = {}
        self.n = 0
        self.post_headers: list[dict] = []
        self.endpoint = endpoint
        self.silent_get = silent_get
        self.post_status = post_status
        self.httpd = _QuietServer(("127.0.0.1", 0), self._handler())
        self.httpd.legacy = self  # type: ignore[attr-defined]
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/sse"

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.httpd.server_address[:2]
        return str(host), int(port)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self):
        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):
                legacy = self.server.legacy  # type: ignore[attr-defined]
                with legacy.lock:
                    legacy.n += 1
                    sid = f"legacy-{legacy.n}"
                    q: queue.Queue = queue.Queue()
                    legacy.pending[sid] = q
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if legacy.silent_get:
                    # 只发头不发事件：客户端必须在读 endpoint 时超时
                    time.sleep(0.6)
                    return
                payload = legacy.endpoint.format(sid=sid)
                self.wfile.write(f"event: endpoint\ndata: {payload}\n\n".encode())
                self.wfile.flush()
                try:
                    resp = q.get(timeout=5.0)
                except queue.Empty:
                    return
                self.wfile.write(f"event: message\ndata: {resp}\n\n".encode())
                self.wfile.flush()

            def do_POST(self):
                legacy = self.server.legacy  # type: ignore[attr-defined]
                sid = self.path.rsplit("/", 1)[-1]
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length))
                with legacy.lock:
                    legacy.post_headers.append(
                        {k.lower(): v for k, v in self.headers.items()}
                    )
                if legacy.post_status == 302:
                    self.send_response(302)
                    self.send_header("Location", "/messages/other")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                method = payload.get("method")
                if method == "initialize":
                    result = {
                        "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                        "capabilities": {},
                        "serverInfo": {"name": "legacy", "version": "1"},
                    }
                elif method == "tools/list":
                    result = {"tools": [_tool(1)]}
                elif method == "tools/call":
                    result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
                else:
                    result = {}
                if method and not method.startswith("notifications/"):
                    q = legacy.pending.get(sid)
                    if q is not None:
                        q.put(json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result}))
                # 202 + application/json + 空 body：回音在同一条 GET 流上
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "0")
                self.end_headers()

        return H


def _legacy_client(legacy: LegacyFake, **kw) -> MCPClient:
    kw.setdefault("budget", MCPConnectionBudget())
    return MCPClient(legacy.url, transport="legacy-sse", **kw)


def test_legacy_sse_roundtrip():
    legacy = LegacyFake()
    try:
        with _legacy_client(legacy) as client:
            client.initialize()
            tools = client.list_tools()
            assert [t["name"] for t in tools] == ["tool_1"]
            result = client.call_tool("tool_1", {})
            assert result["content"][0]["text"] == "ok"
        # 握手之后的请求必须带协议版本头（initialize 自身协商前没有，属预期）
        assert "mcp-protocol-version" not in legacy.post_headers[0]
        for h in legacy.post_headers[-2:]:
            assert h["mcp-protocol-version"] == DEFAULT_PROTOCOL_VERSION
    finally:
        legacy.stop()


def test_legacy_bad_transport_rejected():
    with pytest.raises(MCPError, match="未知传输类型"):
        MCPClient("https://mcp.example.invalid/mcp", transport="carrier-pigeon")


def test_legacy_scheme_downgrade_rejected():
    """B2：https→http 与 http→https 都必须拒绝（同 host、默认端口）。"""
    https_client = MCPClient(
        "https://mcp.example.invalid/sse",
        transport="legacy-sse",
        budget=MCPConnectionBudget(),
    )
    try:
        with pytest.raises(MCPError, match="异源"):
            https_client._check_legacy_target("http://mcp.example.invalid/messages")
        # 正控：同 scheme/host/端口（默认 443）通过
        https_client._check_legacy_target("https://mcp.example.invalid/messages")
    finally:
        https_client.close()

    http_client = MCPClient(
        "http://mcp.example.invalid/sse",
        transport="legacy-sse",
        budget=MCPConnectionBudget(),
    )
    try:
        with pytest.raises(MCPError, match="异源"):
            http_client._check_legacy_target("https://mcp.example.invalid/messages")
        http_client._check_legacy_target("http://mcp.example.invalid/other")
    finally:
        http_client.close()


def test_effective_origin_uses_effective_port():
    assert _effective_origin("https://h:8443/x") == ("https", "h", 8443)
    assert _effective_origin("http://h:8443/x") == ("http", "h", 8443)
    assert _effective_origin("https://h/x") == ("https", "h", 443)
    assert _effective_origin("http://h/x") == ("http", "h", 80)
    assert _effective_origin("http://H:8080/x") == _effective_origin("http://h:8080/y")
    # https→http 降级：同 host、同端口，scheme 不同 ⇒ 不同源
    assert _effective_origin("https://h:8443/x") != _effective_origin("http://h:8443/y")


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("cross_port", "异源"),
        ("userinfo", "userinfo"),
        ("query", "query/fragment"),
        ("fragment", "query/fragment"),
    ],
)
def test_legacy_endpoint_rejections(kind, expected):
    legacy = LegacyFake()
    host, port = legacy.address
    if kind == "cross_port":
        legacy.endpoint = "http://127.0.0.1:1/messages"
    elif kind == "userinfo":
        legacy.endpoint = f"http://u:p@{host}:{port}/messages"
    elif kind == "query":
        legacy.endpoint = "/messages?token=abc"
    else:
        legacy.endpoint = "/messages#frag"
    try:
        with _legacy_client(legacy) as client:
            with pytest.raises(MCPError, match=expected):
                client.initialize()
    finally:
        legacy.stop()


def test_legacy_post_redirect_rejected():
    legacy = LegacyFake(post_status=302)
    try:
        with _legacy_client(legacy) as client:
            with pytest.raises(MCPError, match="重定向"):
                client.initialize()
    finally:
        legacy.stop()


def test_legacy_get_timeout_wrapped_and_counted():
    """N1：legacy GET 的建流/读 endpoint 超时也要变成 MCPError 并计熔断。"""
    legacy = LegacyFake(silent_get=True)
    budget = MCPConnectionBudget()
    client = _legacy_client(
        legacy,
        budget=budget,
        timeout=httpx.Timeout(2.0, connect=0.5, read=0.2),
        total_timeout=2.0,
    )
    try:
        for _ in range(3):
            with pytest.raises(MCPError, match="超时") as ei:
                client.initialize()
            assert ei.value.retryable
        with pytest.raises(MCPError, match="熔断"):
            client.initialize()
    finally:
        client.close()
        legacy.stop()


# ---------------------------------------------------------------------------
# P1：熔断与并发上限


def test_circuit_breaker_opens_after_three_failures(fake_server):
    fake = fake_server(override_status={"*": 500})
    budget = MCPConnectionBudget()
    with make_client(fake, budget=budget) as client:
        for _ in range(3):
            with pytest.raises(MCPError, match="HTTP 500"):
                client.initialize()
        requests_before = len(fake.requests)
        with pytest.raises(MCPError, match="熔断") as ei:
            client.initialize()
        assert ei.value.retryable
        assert len(fake.requests) == requests_before  # 熔断后不再发物理请求


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_do_not_trip_breaker(fake_server, status):
    """N13：401/403 是配置/凭据问题，不计入熔断。"""
    fake = fake_server(override_status={"*": status})
    budget = MCPConnectionBudget()
    with make_client(fake, budget=budget) as client:
        for _ in range(4):
            with pytest.raises(MCPError, match=f"HTTP {status}"):
                client.initialize()
    assert fake.count("initialize") == 4  # 第 4 次仍然真的发了请求


def test_success_resets_consecutive_failures(fake_server):
    """N13：一次成功清零连续失败。"""
    fake = fake_server(override_status={"tools/list": 500})
    budget = MCPConnectionBudget()
    with make_client(fake, budget=budget) as client:
        client.initialize()
        for _ in range(2):
            with pytest.raises(MCPError, match="HTTP 500"):
                client.list_tools()
        fake.cfg.override_status = {}
        assert [t["name"] for t in client.list_tools()] == ["tool_1", "tool_2"]
        fake.cfg.override_status = {"tools/list": 500}
        for _ in range(2):
            with pytest.raises(MCPError, match="HTTP 500"):
                client.list_tools()
        # 第三次连续失败才会打开熔断；该次本身仍是 HTTP 500
        with pytest.raises(MCPError, match="HTTP 500"):
            client.list_tools()
        with pytest.raises(MCPError, match="熔断"):
            client.list_tools()


def test_failure_window_expiry():
    budget = MCPConnectionBudget(
        failure_threshold=3, failure_window=0.1, breaker_cooldown=60.0
    )
    budget.record_failure("srv")
    budget.record_failure("srv")
    time.sleep(0.15)  # 前两次已出窗口
    budget.record_failure("srv")
    budget.check("srv")  # 窗口内只剩 1 次，不熔断
    budget.record_failure("srv")
    budget.record_failure("srv")
    with pytest.raises(MCPError, match="熔断"):
        budget.check("srv")


def test_request_body_limit(fake_server):
    fake = fake_server()
    with make_client(fake) as client:
        client.initialize()
        with pytest.raises(MCPError, match="请求体超过上限"):
            client.call_tool("tool_1", {"blob": "a" * (256 * 1024)})
    assert fake.count("tools/call") == 0


def test_bad_next_cursor_rejected(fake_server):
    fake = fake_server(bad_cursor=True)
    with make_client(fake) as client:
        with pytest.raises(MCPError, match="nextCursor"):
            client.list_tools()


def test_non_dict_tool_entry_rejected(fake_server):
    fake = fake_server(bad_tools=True)
    with make_client(fake) as client:
        with pytest.raises(MCPError, match="非对象"):
            client.list_tools()


def test_killed_server_raises_not_crash(fake_server):
    fake = fake_server()
    client = make_client(fake, budget=MCPConnectionBudget())
    client.initialize()
    fake.stop()
    with pytest.raises(MCPError) as ei:
        client.list_tools()
    assert ei.value.retryable
    client.close()


def test_per_server_concurrency_cap(fake_server):
    fake = fake_server(delay=0.2)
    budget = MCPConnectionBudget(global_limit=8, per_server_limit=2)
    errors: list[BaseException] = []
    with make_client(fake, budget=budget) as client:
        client.initialize()

        def worker():
            try:
                client.call_tool("tool_1")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []
    assert fake.max_concurrent <= 2


def test_global_concurrency_cap_across_servers(fake_server):
    tracker = {"cur": 0, "max": 0, "lock": threading.Lock()}
    budget = MCPConnectionBudget(global_limit=4, per_server_limit=2)
    fakes = [fake_server(tracker=tracker, delay=0.2) for _ in range(3)]
    clients = [make_client(f, budget=budget) for f in fakes]
    errors: list[BaseException] = []
    try:
        for c in clients:
            c.initialize()

        def worker(client):
            try:
                client.call_tool("tool_1")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = []
        for c in clients:
            for _ in range(2):
                threads.append(threading.Thread(target=worker, args=(c,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert tracker["max"] <= 4
    finally:
        for c in clients:
            c.close()


# ---------------------------------------------------------------------------
# 杂项


def test_close_is_idempotent(fake_server):
    fake = fake_server()
    client = make_client(fake)
    client.close()
    client.close()


def test_https_construction_does_not_touch_network():
    # 构造本身不发请求；guard 先行；URL 合法即可
    c = MCPClient(
        "https://mcp.example.invalid/mcp",
        url_guard=lambda u: None,
        budget=MCPConnectionBudget(),
    )
    c.close()
