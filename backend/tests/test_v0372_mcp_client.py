# backend/tests/test_v0372_mcp_client.py
"""v0.37.2 MCP HTTP/SSE 传输层测试。

假 MCP server 用标准库 ThreadingHTTPServer 起在 127.0.0.1 随机端口，
全部行为由测试内配置驱动，不依赖任何外部 MCP 实现。
"""
from __future__ import annotations

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
)

# ---------------------------------------------------------------------------
# 假 server（Streamable HTTP）


def _tool(i: int) -> dict:
    return {
        "name": f"tool_{i}",
        "description": f"第 {i} 个工具",
        "inputSchema": {"type": "object"},
    }


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


class StreamFake:
    def __init__(self, cfg: StreamConfig, tracker: dict | None = None) -> None:
        self.cfg = cfg
        self.tracker = tracker  # 跨多个 fake 共享的并发计数 dict
        self.lock = threading.Lock()
        self.current = 0
        self.max_concurrent = 0
        self.requests: list[dict] = []  # {method, headers, payload}
        self.session = cfg.session_id
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_stream_handler())
        self.httpd.fake = self  # type: ignore[attr-defined]
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

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

        def do_POST(self):
            fake = self.server.fake  # type: ignore[attr-defined]
            cfg = fake.cfg
            tr = fake.tracker
            with fake.lock:
                fake.current += 1
                fake.max_concurrent = max(fake.max_concurrent, fake.current)
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
                    self._send(override, b'{"oops":1}', "application/json")
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
                    self._send_jsonrpc(
                        resp, ctype=ctype, extra_headers={"Mcp-Session-Id": fake.session}
                    )
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
                    self._send_jsonrpc(
                        {"jsonrpc": "2.0", "id": payload.get("id"), "result": result},
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
    fake = fake_server(override_status={"initialize": 302})
    with make_client(fake) as client:
        with pytest.raises(MCPError, match="重定向"):
            client.initialize()


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
    assert secret not in caplog.text
    assert secret not in str(ei.value)
    for record in caplog.records:
        assert secret not in record.getMessage()


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

    srv = ThreadingHTTPServer(("127.0.0.1", 0), BigSSEHandler)
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


# ---------------------------------------------------------------------------
# P1：legacy SSE 兜底


class LegacyFake:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending: dict[str, queue.Queue] = {}
        self.n = 0
        self.post_headers: list[dict] = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.httpd.legacy = self  # type: ignore[attr-defined]
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/sse"

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
                self.wfile.write(
                    f"event: endpoint\ndata: /messages?session={sid}\n\n".encode()
                )
                self.wfile.flush()
                try:
                    resp = q.get(timeout=5.0)
                except queue.Empty:
                    return
                self.wfile.write(f"event: message\ndata: {resp}\n\n".encode())
                self.wfile.flush()

            def do_POST(self):
                legacy = self.server.legacy  # type: ignore[attr-defined]
                sid = self.path.split("session=")[-1]
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length))
                with legacy.lock:
                    legacy.post_headers.append(
                        {k.lower(): v for k, v in self.headers.items()}
                    )
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
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()

        return H


def test_legacy_sse_roundtrip():
    legacy = LegacyFake()
    try:
        with MCPClient(
            legacy.url, transport="legacy-sse", budget=MCPConnectionBudget()
        ) as client:
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
