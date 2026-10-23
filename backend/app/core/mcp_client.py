"""v0.37.2 MCP over HTTP 传输层（server-only，自研，不依赖 ``mcp`` 包）。

本文件只管一件事：把一个 MCP server 的 Streamable HTTP（含可选 legacy SSE 兜底）
会话跑起来——``initialize`` / ``notifications/initialized`` / ``tools/list`` /
``tools/call``，外加安全外壳：

- URL 判据**不在这里**：私网/白名单等由调用方注入 ``url_guard(url) -> 拒绝理由|None``
  （后续接 net_guard）。本文件只在构造与每次物理请求前调用它，拒绝即停。
- 凭据只走调用方显式传入的 headers（如 Authorization）；``trust_env=False``
  挡住环境代理与凭据，不发 Cookie，不跟随重定向。
- 任何路径都不把 headers / token / 响应体写进日志。

工具适配、审批、频控属于后续波次，不在本文件。
"""
from __future__ import annotations

import contextlib
import itertools
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import httpx

from app.core.tls import system_ssl_context

logger = logging.getLogger("app.core.mcp_client")

__all__ = [
    "MCPError",
    "MCPClient",
    "MCPConnectionBudget",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "DEFAULT_PROTOCOL_VERSION",
]

# 显式钉死支持的协议版本；server 协商出集合外的版本一律 fail-closed。
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-06-18"})
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

MAX_RESPONSE_BYTES = 512 * 1024
MAX_SSE_EVENT_BYTES = 64 * 1024
MAX_LIST_PAGES = 5
MAX_LIST_TOOLS = 64

_RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class MCPError(RuntimeError):
    """MCP 传输层错误。``retryable`` 只代表"换个时间再试可能不同"，
    不代表本客户端会自动重试——本客户端除会话恢复外不重试任何东西。"""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        jsonrpc_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)
        self.jsonrpc_code = jsonrpc_code


class _SessionLost(MCPError):
    """内部信号：HTTP 404 且携带过会话 id，值得做一次重初始化。"""


class MCPConnectionBudget:
    """全局 / 每 server 并发信号量 + 失败熔断（3 次失败 / 60 秒窗口）。

    模块级单例 :data:`DEFAULT_BUDGET` 按 server 维度共享；测试可注入独立实例。
    """

    def __init__(
        self,
        *,
        global_limit: int = 4,
        per_server_limit: int = 2,
        failure_threshold: int = 3,
        failure_window: float = 60.0,
        breaker_cooldown: float = 60.0,
    ) -> None:
        self._global_limit = global_limit
        self._per_server_limit = per_server_limit
        self._failure_threshold = failure_threshold
        self._failure_window = failure_window
        self._breaker_cooldown = breaker_cooldown
        self._global_sem = threading.BoundedSemaphore(global_limit)
        self._per_server: Dict[str, threading.BoundedSemaphore] = {}
        self._failures: Dict[str, Deque[float]] = {}
        self._breaker_until: Dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, server_id: str) -> None:
        with self._lock:
            until = self._breaker_until.get(server_id, 0.0)
        if until and time.monotonic() < until:
            raise MCPError("该 MCP server 已触发熔断，稍后再试", retryable=True)

    def acquire(self, server_id: str) -> None:
        self._global_sem.acquire()
        try:
            with self._lock:
                sem = self._per_server.get(server_id)
                if sem is None:
                    sem = threading.BoundedSemaphore(self._per_server_limit)
                    self._per_server[server_id] = sem
            sem.acquire()
        except BaseException:
            self._global_sem.release()
            raise

    def release(self, server_id: str) -> None:
        with self._lock:
            sem = self._per_server.get(server_id)
        if sem is not None:
            sem.release()
        self._global_sem.release()

    def record_failure(self, server_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            dq = self._failures.setdefault(server_id, deque())
            dq.append(now)
            cutoff = now - self._failure_window
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self._failure_threshold:
                self._breaker_until[server_id] = now + self._breaker_cooldown
                dq.clear()
                logger.warning("MCP server 熔断打开（连续失败达阈值）: %s", server_id)


DEFAULT_BUDGET = MCPConnectionBudget()


class MCPClient:
    """一个 MCP server 一个实例；线程安全受 :class:`MCPConnectionBudget` 约束。"""

    def __init__(
        self,
        server_url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        url_guard: Optional[Callable[[str], Optional[str]]] = None,
        timeout: Optional[httpx.Timeout] = None,
        transport: str = "streamable-http",
        budget: Optional[MCPConnectionBudget] = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_sse_event_bytes: int = MAX_SSE_EVENT_BYTES,
        max_list_pages: int = MAX_LIST_PAGES,
        max_list_tools: int = MAX_LIST_TOOLS,
        client_name: str = "ai-assistant",
        client_version: str = "0.37.2",
    ) -> None:
        if transport not in ("streamable-http", "legacy-sse"):
            raise MCPError(f"未知传输类型: {transport!r}")
        self._transport = transport
        self._url_guard = url_guard
        self._check_url(server_url, what="服务器地址")
        self._url = server_url
        parsed = urlparse(server_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise MCPError("MCP server URL 必须是 http(s) 绝对地址")
        self._server_id = f"{parsed.netloc}{parsed.path or '/'}"

        self._max_response_bytes = max_response_bytes
        self._max_sse_event_bytes = max_sse_event_bytes
        self._max_list_pages = max_list_pages
        self._max_list_tools = max_list_tools
        self._budget = budget if budget is not None else DEFAULT_BUDGET
        self._local = threading.local()

        # 只带调用方显式给的 header（Authorization 之类）；不带 Cookie，
        # trust_env=False 挡住代理与机器级凭据。
        self._http = httpx.Client(
            verify=system_ssl_context(),
            trust_env=False,
            follow_redirects=False,
            timeout=timeout or httpx.Timeout(20.0, connect=5.0, read=15.0),
            headers=dict(headers or {}),
        )
        self._ids = itertools.count(1)
        self._session_id: Optional[str] = None
        self._protocol_version: Optional[str] = None
        self._ready = False
        self._legacy_endpoint: Optional[str] = None
        self._closed = False

    # ------------------------------------------------------------------ 生命周期

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._http.close()

    def __enter__(self) -> "MCPClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ 公开 API

    def initialize(self) -> Dict[str, Any]:
        """显式握手。协议版本不在支持集合内一律 fail-closed。"""
        with self._budgeted():
            return self._do_initialize()

    def list_tools(self) -> List[Dict[str, Any]]:
        with self._budgeted():
            self._ensure_initialized()
            tools: List[Dict[str, Any]] = []
            cursor: Optional[str] = None
            for page in range(1, self._max_list_pages + 1):
                params: Dict[str, Any] = {}
                if cursor is not None:
                    params["cursor"] = cursor
                result = self._rpc("tools/list", params)
                page_tools = result.get("tools") if isinstance(result, dict) else None
                if not isinstance(page_tools, list):
                    raise MCPError("tools/list 响应缺少 tools 数组")
                tools.extend(page_tools)
                if len(tools) > self._max_list_tools:
                    raise MCPError(
                        f"工具数超过上限 {self._max_list_tools}，fail-closed 丢弃"
                    )
                cursor = result.get("nextCursor") if isinstance(result, dict) else None
                if cursor in (None, ""):
                    return tools
            raise MCPError(f"tools/list 分页超过上限 {self._max_list_pages} 页")

    def call_tool(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """``tools/call``。失败（含 isError 与 JSON-RPC error）一律不重试。"""
        with self._budgeted():
            self._ensure_initialized()
            result = self._rpc(
                "tools/call",
                {"name": name, "arguments": arguments or {}},
                allow_session_recovery=False,
            )
            if isinstance(result, dict) and result.get("isError"):
                raise MCPError(
                    f"工具 {name} 返回错误: {_tool_error_text(result)}",
                    retryable=False,
                )
            return result if isinstance(result, dict) else {"result": result}

    # ------------------------------------------------------------------ 握手内部

    def _ensure_initialized(self) -> None:
        if not self._ready:
            self._do_initialize()

    def _do_initialize(self) -> Dict[str, Any]:
        if self._transport == "legacy-sse":
            return self._legacy_initialize()
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "initialize",
            "params": {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self._client_name(), "version": self._version_tag()},
            },
        }
        msg, resp_headers = self._raw_post(payload)
        result = _extract_result(msg, payload["id"])
        served = result.get("protocolVersion") if isinstance(result, dict) else None
        if served not in SUPPORTED_PROTOCOL_VERSIONS:
            raise MCPError(f"服务器协议版本不受支持: {served!r}")
        self._protocol_version = served
        session = resp_headers.get("mcp-session-id")
        self._session_id = session or None
        # 握手完成后必须发 initialized 通知，否则 server 可能拒绝后续请求。
        self._post_notification("notifications/initialized")
        self._ready = True
        logger.debug("MCP 握手完成: %s", self._server_id)
        return result

    def _client_name(self) -> str:
        return "ai-assistant"

    def _version_tag(self) -> str:
        return "0.37.2"

    # ------------------------------------------------------------------ RPC 核心

    def _rpc(
        self,
        method: str,
        params: Dict[str, Any],
        *,
        allow_session_recovery: bool = True,
    ) -> Any:
        if self._transport == "legacy-sse":
            request_id = next(self._ids)
            msg = self._legacy_rpc(
                method, params, request_id=request_id, allow_session_recovery=False
            )
            return _extract_result(msg, request_id)
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params,
        }
        try:
            msg, _headers = self._raw_post(payload)
        except _SessionLost:
            if not allow_session_recovery:
                raise MCPError("会话失效且本请求不允许重初始化", retryable=False)
            self._reset_session()
            self._do_initialize()  # 只重初始化这一次
            try:
                msg, _headers = self._raw_post(payload, recovery_done=True)
            except _SessionLost:
                raise MCPError("重初始化后会话仍无效（HTTP 404）", retryable=False)
        return _extract_result(msg, payload["id"])

    def _post_notification(self, method: str) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        try:
            self._raw_post(payload, notification=True)
        except _SessionLost:
            self._reset_session()
            self._do_initialize()
            self._raw_post(payload, notification=True, recovery_done=True)

    def _reset_session(self) -> None:
        self._ready = False
        self._session_id = None
        self._protocol_version = None

    def _raw_post(
        self,
        payload: Dict[str, Any],
        *,
        notification: bool = False,
        recovery_done: bool = False,
    ) -> Tuple[Dict[str, Any], Any]:
        """一次物理 POST。会话外的失败都不重试；网络类失败计入熔断。"""
        self._check_url(self._url, what="请求地址")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_id = payload.get("id")
        try:
            with self._http.stream(
                "POST", self._url, content=body, headers=headers
            ) as resp:
                status = resp.status_code
                if status == 404:
                    if recovery_done:
                        raise MCPError("HTTP 404：端点或会话持续无效", retryable=False)
                    raise _SessionLost("HTTP 404：会话失效", retryable=False)
                if 300 <= status < 400:
                    raise MCPError("服务器返回重定向，已拒绝跟随", retryable=False)
                ctype = resp.headers.get("content-type", "")
                if notification:
                    if status in (200, 202, 204):
                        return {}, resp.headers
                    self._budget.record_failure(self._server_id)
                    raise MCPError(
                        f"通知被拒绝: HTTP {status}",
                        retryable=status in _RETRYABLE_HTTP_STATUS,
                    )
                if status >= 400:
                    self._budget.record_failure(self._server_id)
                    raise MCPError(
                        f"MCP server 返回 HTTP {status}",
                        retryable=status in _RETRYABLE_HTTP_STATUS,
                    )
                if "text/event-stream" in ctype:
                    msg = self._rpc_over_sse(resp, request_id)
                elif "application/json" in ctype:
                    raw = self._read_capped(resp)
                    try:
                        msg = json.loads(raw)
                    except ValueError as exc:
                        raise MCPError(
                            f"响应不是合法 JSON: {type(exc).__name__}"
                        ) from exc
                else:
                    raise MCPError(f"不支持的响应类型: {ctype or '缺失'}")
                if not isinstance(msg, dict):
                    raise MCPError("JSON-RPC 响应不是对象")
                return msg, resp.headers
        except MCPError:
            raise
        except httpx.TimeoutException as exc:
            self._budget.record_failure(self._server_id)
            raise MCPError("请求超时", retryable=True) from exc
        except httpx.HTTPError as exc:
            self._budget.record_failure(self._server_id)
            raise MCPError(
                f"传输层错误: {type(exc).__name__}", retryable=True
            ) from exc

    # ------------------------------------------------------------------ SSE 解析

    def _iter_sse_events(self, resp: httpx.Response) -> Iterator[Tuple[Optional[str], str]]:
        data_lines: List[str] = []
        event_type: Optional[str] = None
        event_bytes = 0
        total_bytes = 0
        pending = b""
        for chunk in resp.iter_bytes():
            total_bytes += len(chunk)
            if total_bytes > self._max_response_bytes:
                raise MCPError("SSE 响应超过字节上限", retryable=False)
            pending += chunk
            while True:
                nl = pending.find(b"\n")
                if nl < 0:
                    break
                line, pending = pending[:nl], pending[nl + 1:]
                if line.endswith(b"\r"):
                    line = line[:-1]
                event_bytes += len(line) + 1
                if event_bytes > self._max_sse_event_bytes:
                    raise MCPError("单个 SSE 事件超过字节上限", retryable=False)
                if not line:
                    if data_lines or event_type is not None:
                        yield event_type, "\n".join(data_lines)
                    data_lines = []
                    event_type = None
                    event_bytes = 0
                elif line.startswith(b"data:"):
                    value = line[5:]
                    if value.startswith(b" "):
                        value = value[1:]
                    data_lines.append(value.decode("utf-8", "replace"))
                elif line.startswith(b"event:"):
                    event_type = line[6:].lstrip(b" ").decode("utf-8", "replace")
                # id: / retry: / ':' 注释行按规范忽略
        if data_lines or event_type is not None:
            yield event_type, "\n".join(data_lines)

    def _first_rpc_message(
        self, events: Iterator[Tuple[Optional[str], str]], request_id: Optional[int]
    ) -> Dict[str, Any]:
        for event_type, data in events:
            if event_type not in (None, "message"):
                # 未知事件类型不许崩，跳过继续等 message。
                logger.debug("忽略未知 SSE 事件类型: %s", event_type)
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if request_id is not None and msg.get("id") != request_id:
                continue
            return msg
        raise MCPError("SSE 流中未找到本请求的响应", retryable=True)

    def _rpc_over_sse(
        self, resp: httpx.Response, request_id: Optional[int]
    ) -> Dict[str, Any]:
        return self._first_rpc_message(self._iter_sse_events(resp), request_id)

    # ------------------------------------------------------------------ legacy SSE

    def _legacy_initialize(self) -> Dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "initialize",
            "params": {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self._client_name(), "version": self._version_tag()},
            },
        }
        msg = self._legacy_rpc(payload["method"], payload["params"], request_id=payload["id"])
        result = _extract_result(msg, payload["id"])
        served = result.get("protocolVersion") if isinstance(result, dict) else None
        if served not in SUPPORTED_PROTOCOL_VERSIONS:
            raise MCPError(f"服务器协议版本不受支持: {served!r}")
        self._protocol_version = served
        with contextlib.suppress(MCPError):
            # legacy 通道对通知常常不回话；发不出去也不影响后续 POST。
            self._legacy_notification("notifications/initialized")
        self._ready = True
        return result

    def _legacy_rpc(
        self,
        method: str,
        params: Dict[str, Any],
        *,
        request_id: Optional[int] = None,
        allow_session_recovery: bool = True,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id if request_id is not None else next(self._ids),
            "method": method,
            "params": params,
        }
        return self._legacy_exchange(payload)

    def _legacy_notification(self, method: str) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        self._legacy_exchange(payload, notification=True)

    def _legacy_exchange(
        self, payload: Dict[str, Any], *, notification: bool = False
    ) -> Dict[str, Any]:
        """打开 SSE GET 流 → 等 endpoint 事件 → 向该同源地址 POST → 等 message 响应。

        最简兼容实现：每个逻辑请求一条流。endpoint 必须与配置地址同源。
        """
        self._check_url(self._url, what="请求地址")
        request_id = payload.get("id")
        get_headers = {"Accept": "text/event-stream"}
        if self._protocol_version:
            get_headers["MCP-Protocol-Version"] = self._protocol_version
        with self._http.stream("GET", self._url, headers=get_headers) as stream:
            if stream.status_code >= 400:
                self._budget.record_failure(self._server_id)
                raise MCPError(
                    f"legacy SSE 连接失败: HTTP {stream.status_code}",
                    retryable=stream.status_code in _RETRYABLE_HTTP_STATUS,
                )
            if 300 <= stream.status_code < 400:
                raise MCPError("服务器返回重定向，已拒绝跟随", retryable=False)
            endpoint: Optional[str] = None
            # 同一条 GET 流上的事件生成器只许建一次，POST 之后继续从它取响应。
            events = self._iter_sse_events(stream)
            for event_type, data in events:
                if event_type == "endpoint":
                    endpoint = data.strip()
                    break
                if event_type not in (None, "message"):
                    logger.debug("忽略未知 SSE 事件类型: %s", event_type)
            if not endpoint:
                raise MCPError("legacy SSE 流缺少 endpoint 事件")
            target = urljoin(self._url, endpoint)
            if urlparse(target).netloc != urlparse(self._url).netloc:
                raise MCPError("legacy endpoint 指向异源地址，拒绝")
            self._check_url(target, what="endpoint 地址")
            post_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            }
            if self._protocol_version:
                post_headers["MCP-Protocol-Version"] = self._protocol_version
            if self._session_id:
                post_headers["Mcp-Session-Id"] = self._session_id
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                with self._http.stream(
                    "POST", target, content=body, headers=post_headers
                ) as post_resp:
                    if post_resp.status_code >= 400:
                        self._budget.record_failure(self._server_id)
                        raise MCPError(
                            f"legacy POST 失败: HTTP {post_resp.status_code}",
                            retryable=post_resp.status_code in _RETRYABLE_HTTP_STATUS,
                        )
                    ctype = post_resp.headers.get("content-type", "")
                    if notification and post_resp.status_code in (200, 202, 204):
                        return {}
                    if "application/json" in ctype:
                        raw = self._read_capped(post_resp)
                        try:
                            msg = json.loads(raw)
                        except ValueError as exc:
                            raise MCPError("legacy POST 响应不是合法 JSON") from exc
                        if isinstance(msg, dict):
                            return msg
                    # 202 + 空响应：真正的回音在同一条 GET 流上。
                    return self._first_rpc_message(events, request_id)
            except MCPError:
                raise
            except httpx.TimeoutException as exc:
                self._budget.record_failure(self._server_id)
                raise MCPError("请求超时", retryable=True) from exc
            except httpx.HTTPError as exc:
                self._budget.record_failure(self._server_id)
                raise MCPError(
                    f"传输层错误: {type(exc).__name__}", retryable=True
                ) from exc

    # ------------------------------------------------------------------ 外壳工具

    def _check_url(self, url: str, *, what: str) -> None:
        if self._url_guard is None:
            return
        reason = self._url_guard(url)
        if reason:
            raise MCPError(f"{what} 被 URL 守卫拒绝: {reason}", retryable=False)

    def _read_capped(self, resp: httpx.Response) -> bytes:
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf += chunk
            if len(buf) > self._max_response_bytes:
                raise MCPError("响应超过字节上限", retryable=False)
        return bytes(buf)

    @contextlib.contextmanager
    def _budgeted(self) -> Iterator[None]:
        """公开入口取一个并发槽位；嵌套调用（会话恢复里的重初始化）不重复取。"""
        depth = getattr(self._local, "depth", 0)
        if depth > 0:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        self._budget.check(self._server_id)
        self._budget.acquire(self._server_id)
        self._local.depth = 1
        try:
            yield
        finally:
            self._local.depth = 0
            self._budget.release(self._server_id)


def _extract_result(msg: Dict[str, Any], request_id: Any) -> Any:
    err = msg.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        text = err.get("message") or "未知错误"
        raise MCPError(
            f"JSON-RPC 错误 {code}: {text}",
            retryable=False,
            jsonrpc_code=code if isinstance(code, int) else None,
        )
    if "result" not in msg:
        raise MCPError("JSON-RPC 响应缺少 result 字段")
    return msg["result"]


def _tool_error_text(result: Dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    return text[:500]
    return "工具执行失败（无错误文本）"
