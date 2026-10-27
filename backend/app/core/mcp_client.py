"""v0.37.2 MCP over HTTP 传输层（server-only，自研，不依赖 ``mcp`` 包）。

本文件只管一件事：把一个 MCP server 的 Streamable HTTP（含可选 legacy SSE 兜底）
会话跑起来——``initialize`` / ``notifications/initialized`` / ``tools/list`` /
``tools/call``，外加安全外壳：

- URL 判据**不在这里**：私网/白名单等由调用方注入 ``url_guard(url) -> 拒绝理由|None``
  （后续接 net_guard）。该参数**必填**；未配置时构造与请求都拒绝（fail-closed），
  本文件在构造与每次物理请求前调用它，拒绝即停。
- 凭据只走调用方显式传入的 headers（如 Authorization）。URL 里出现 userinfo /
  query / fragment 一律在构造期拒绝（fail-closed）：
  * userinfo 会被 httpx 自动转成 ``Authorization: Basic``，凭据并非只走显式 headers，
    且 ``netloc`` 原样进 ``_server_id`` 就会把口令带进 debug / 熔断日志；
  * query 常被拿来塞 token，httpx 自己的 INFO logger 会把完整 URL（含 query）打出来。
    因此本版直接拒绝 URL query——这条 httpx INFO 泄漏面随之消失，代价是不支持
    "URL 里带 token 的部署形态"，凭据必须改走显式 headers。
- 不保留服务端下发的 Cookie：每个物理响应之后清空 httpx 的 cookie jar，后续请求
  不会回显 ``Cookie``（httpx 默认会自动回填）。
- 不跟随重定向（3xx 显式拒绝），``trust_env=False`` 挡住环境代理与机器级凭据。
- 任何路径都不把 headers / token / 响应体写进日志。
- **总超时**：每个逻辑操作（``initialize`` / ``list_tools`` 整体 / ``call_tool`` /
  legacy exchange）建立一条墙钟 ``deadline``，在每个 chunk / SSE 事件之间检查。
  httpx 的 ``Timeout`` 只有 connect/read/write/pool，没有"整次请求总时长"字段，
  单靠 read timeout 挡不住"每 14 秒发一个字节"的慢滴 server（那会永久占住并发槽）。
  注意 deadline 只在 chunk 之间检查，最坏情况下一次操作真正返回的时刻是
  ``deadline + read_timeout``（阻塞在单次 read 上时）。
- **线程安全**：会话状态由 ``_init_lock``（RLock）保护，``_ensure_initialized``
  双检（快路径读 ``_ready``，慢路径持锁再查并握手），并发首次调用只握手一次。
- **熔断口径**：401/403 鉴权类失败不计入；一次操作完整成功（拿到并校验通过响应）
  清零连续失败；404 / 3xx / 协议层错误（SSE 未找到响应、超限、JSON 非法）不计数。
- ``client_name`` / ``client_version`` 会原样进 ``initialize`` 的 ``clientInfo``。

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
MAX_REQUEST_BYTES = 256 * 1024

# 每个逻辑操作默认的墙钟总时限（秒）。
DEFAULT_TOTAL_TIMEOUT = 30.0

_RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
# 鉴权类失败说明"配置/凭据不对"，不是"对端坏了"，不计入熔断。
_AUTH_HTTP_STATUS = frozenset({401, 403})


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
    """全局 / 每 server 并发信号量 + 失败熔断（默认连续 3 次失败 / 60 秒窗口）。

    模块级单例 :data:`DEFAULT_BUDGET` 按 server 维度共享；测试可注入独立实例。

    口径：``record_failure`` 记的是**连续**失败（窗口内钳掉过期项后再判阈值），
    ``record_success`` 清零；401/403 由调用方豁免，不进这里。
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

    @property
    def breaker_cooldown(self) -> float:
        """熔断 cooldown（秒）。只读：supervisor 用它压本地 circuit_until。"""
        return self._breaker_cooldown

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
            cutoff = now - self._failure_window
            while dq and dq[0] < cutoff:
                dq.popleft()
            dq.append(now)
            if len(dq) >= self._failure_threshold:
                self._breaker_until[server_id] = now + self._breaker_cooldown
                dq.clear()
                logger.warning("MCP server 熔断打开（连续失败达阈值）: %s", server_id)

    def record_success(self, server_id: str) -> None:
        with self._lock:
            self._failures.pop(server_id, None)


DEFAULT_BUDGET = MCPConnectionBudget()


class MCPClient:
    """一个 MCP server 一个实例。

    ``url_guard`` 是**必填安全参数**：构造时必须显式传入
    ``url_guard(url) -> 拒绝理由 | None``；``None`` 不再表示"关闭检查"，
    未配置守卫会在构造期直接抛 :class:`MCPError`（否则等于默认放行 SSRF）。

    ``client_name`` / ``client_version`` 原样进 ``clientInfo``。
    线程安全：初始化与会话状态由内部 RLock 保护；并发槽位由
    :class:`MCPConnectionBudget` 约束。
    """

    def __init__(
        self,
        server_url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        url_guard: Optional[Callable[[str], Optional[str]]] = None,
        timeout: Optional[httpx.Timeout] = None,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        transport: str = "streamable-http",
        budget: Optional[MCPConnectionBudget] = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_sse_event_bytes: int = MAX_SSE_EVENT_BYTES,
        max_list_pages: int = MAX_LIST_PAGES,
        max_list_tools: int = MAX_LIST_TOOLS,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        client_name: str = "ai-assistant",
        client_version: str = "0.37.2",
    ) -> None:
        """``url_guard`` 为必填安全参数：``None`` 在构造期直接拒绝，不表示关闭检查。"""
        if transport not in ("streamable-http", "legacy-sse"):
            raise MCPError(f"未知传输类型: {transport!r}")
        if total_timeout <= 0:
            raise MCPError("total_timeout 必须为正数秒")
        if url_guard is None:
            raise MCPError("url_guard 必填：URL 守卫未配置时拒绝构造 MCPClient")
        parsed = urlparse(server_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise MCPError("MCP server URL 必须是 http(s) 绝对地址")
        if parsed.username is not None or parsed.password is not None:
            raise MCPError(
                "MCP server URL 不得包含 userinfo：凭据只走显式 headers，"
                "URL 不许带 userinfo/query/fragment"
            )
        if parsed.query or parsed.fragment or "?" in server_url or "#" in server_url:
            raise MCPError(
                "MCP server URL 不得包含 query/fragment：凭据只走显式 headers，"
                "URL 不许带 userinfo/query/fragment（query 还会被 httpx 的 INFO 日志打出）"
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise MCPError("MCP server URL 端口非法") from exc
        self._transport = transport
        self._url_guard = url_guard
        self._check_url(server_url, what="服务器地址")
        self._url = server_url
        # server 标识只由 hostname + 显式端口 + path 组成：绝不拼 netloc（含 userinfo），
        # 也不含 query/fragment，所以它出现在 debug / 熔断日志里是安全的。
        host = (parsed.hostname or "").lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # IPv6
        port_part = f":{port}" if port is not None else ""
        self._server_id = f"{host}{port_part}{parsed.path or '/'}"

        self._max_response_bytes = max_response_bytes
        self._max_sse_event_bytes = max_sse_event_bytes
        self._max_list_pages = max_list_pages
        self._max_list_tools = max_list_tools
        self._max_request_bytes = max_request_bytes
        self._total_timeout = float(total_timeout)
        self._client_info_name = client_name
        self._client_info_version = client_version
        self._budget = budget if budget is not None else DEFAULT_BUDGET
        self._local = threading.local()
        self._init_lock = threading.RLock()

        # 只带调用方显式给的 header（Authorization 之类）；不带 Cookie，
        # trust_env=False 挡住代理与机器级凭据，cookie jar 每个响应后清空。
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
        """显式握手。

        入口先复位本地会话状态（避免上一次失败的 ``_session_id`` 被带进新请求），
        再在 ``_init_lock`` 内重新协商；协议版本不在支持集合内一律 fail-closed。
        """
        with self._budgeted():
            deadline = self._deadline()
            with self._init_lock:
                self._reset_session()
                return self._do_initialize(deadline=deadline)

    def list_tools(self) -> List[Dict[str, Any]]:
        with self._budgeted():
            deadline = self._deadline()
            self._ensure_initialized(deadline)
            tools: List[Dict[str, Any]] = []
            cursor: Optional[str] = None
            for _page in range(1, self._max_list_pages + 1):
                params: Dict[str, Any] = {}
                if cursor is not None:
                    params["cursor"] = cursor
                result = self._rpc("tools/list", params, deadline=deadline)
                page_tools = result.get("tools") if isinstance(result, dict) else None
                if not isinstance(page_tools, list):
                    raise MCPError("tools/list 响应缺少 tools 数组")
                for item in page_tools:
                    if not isinstance(item, dict):
                        raise MCPError(
                            "tools/list 中存在非对象的工具条目，fail-closed 丢弃"
                        )
                tools.extend(page_tools)
                if len(tools) > self._max_list_tools:
                    raise MCPError(
                        f"工具数超过上限 {self._max_list_tools}，fail-closed 丢弃"
                    )
                cursor = result.get("nextCursor") if isinstance(result, dict) else None
                if cursor is not None and not isinstance(cursor, str):
                    raise MCPError("tools/list 的 nextCursor 不是字符串，fail-closed 丢弃")
                if cursor in (None, ""):
                    return tools
            raise MCPError(f"tools/list 分页超过上限 {self._max_list_pages} 页")

    def call_tool(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """``tools/call``。失败（含 isError 与 JSON-RPC error）一律不重试。"""
        with self._budgeted():
            deadline = self._deadline()
            self._ensure_initialized(deadline)
            result = self._rpc(
                "tools/call",
                {"name": name, "arguments": arguments or {}},
                allow_session_recovery=False,
                deadline=deadline,
            )
            if isinstance(result, dict) and result.get("isError"):
                raise MCPError(
                    f"工具 {name} 返回错误: {_tool_error_text(result)}",
                    retryable=False,
                )
            return result if isinstance(result, dict) else {"result": result}

    # ------------------------------------------------------------------ 握手内部

    def _deadline(self) -> float:
        return time.monotonic() + self._total_timeout

    @staticmethod
    def _check_deadline(deadline: Optional[float]) -> None:
        if deadline is not None and time.monotonic() > deadline:
            raise MCPError("请求超过总时限", retryable=True)

    def _ensure_initialized(self, deadline: Optional[float] = None) -> None:
        if self._ready:  # 快路径：热态直接放行
            return
        with self._init_lock:  # 慢路径：持锁双检，并发首次调用只握手一次
            if not self._ready:
                self._do_initialize(deadline=deadline)

    def _do_initialize(
        self,
        *,
        send_initialized: bool = True,
        recovery_done: bool = False,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        """一次握手。

        ``initialized`` 通知 404 时做**有界**恢复：复位会话后重握手一次，同一次
        握手内最多再发一次通知；第二次仍 404 直接 ``MCPError``，绝不递归放大成
        请求风暴（旧实现会在 ``_post_notification`` 里无限递归直到 RecursionError）。
        """
        if self._transport == "legacy-sse":
            return self._legacy_initialize(deadline=deadline)
        deadline = deadline if deadline is not None else self._deadline()
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
        msg, resp_headers = self._raw_post(payload, deadline=deadline)
        result = _extract_result(msg, payload["id"])
        served = result.get("protocolVersion") if isinstance(result, dict) else None
        if served not in SUPPORTED_PROTOCOL_VERSIONS:
            raise MCPError(f"服务器协议版本不受支持: {served!r}")
        self._protocol_version = served
        session = resp_headers.get("mcp-session-id")
        self._session_id = session or None
        # 握手完成后必须发 initialized 通知，否则 server 可能拒绝后续请求。
        try:
            if send_initialized:
                self._post_notification("notifications/initialized", deadline=deadline)
        except _SessionLost:
            # 通知 404：这次握手的 session/protocol 刚写进去，必须一并清掉。
            self._reset_session()
            if recovery_done:
                raise MCPError(
                    "会话恢复后 initialized 通知仍被拒绝（HTTP 404），放弃",
                    retryable=False,
                ) from None
            return self._do_initialize(
                send_initialized=True, recovery_done=True, deadline=deadline
            )
        except BaseException:
            self._reset_session()
            raise
        self._ready = True
        logger.debug("MCP 握手完成: %s", self._server_id)
        return result

    def _client_name(self) -> str:
        return self._client_info_name

    def _version_tag(self) -> str:
        return self._client_info_version

    # ------------------------------------------------------------------ RPC 核心

    def _rpc(
        self,
        method: str,
        params: Dict[str, Any],
        *,
        allow_session_recovery: bool = True,
        deadline: Optional[float] = None,
    ) -> Any:
        deadline = deadline if deadline is not None else self._deadline()
        if self._transport == "legacy-sse":
            request_id = next(self._ids)
            msg = self._legacy_rpc(
                method, params, request_id=request_id, deadline=deadline
            )
            return _extract_result(msg, request_id)
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params,
        }
        try:
            msg, _headers = self._raw_post(payload, deadline=deadline)
        except _SessionLost:
            if not allow_session_recovery:
                # 本次调用不重放（避免副作用），但必须复位：否则 _ready/_session_id
                # 停留在失效值上，之后每次 call_tool 都会永久失败。
                self._reset_session()
                raise MCPError("会话失效且本请求不允许重初始化", retryable=False) from None
            with self._init_lock:
                self._reset_session()
                self._do_initialize(deadline=deadline)  # 只重初始化这一次
            try:
                msg, _headers = self._raw_post(
                    payload, recovery_done=True, deadline=deadline
                )
            except _SessionLost:
                raise MCPError("重初始化后会话仍无效（HTTP 404）", retryable=False) from None
        return _extract_result(msg, payload["id"])

    def _post_notification(
        self, method: str, *, deadline: Optional[float] = None
    ) -> None:
        """一条通知只发一次；404 只抛 ``_SessionLost``，恢复由调用方有界处理。"""
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        self._raw_post(payload, notification=True, deadline=deadline)

    def _reset_session(self) -> None:
        with self._init_lock:
            self._ready = False
            self._session_id = None
            self._protocol_version = None

    def _raw_post(
        self,
        payload: Dict[str, Any],
        *,
        notification: bool = False,
        recovery_done: bool = False,
        deadline: Optional[float] = None,
    ) -> Tuple[Dict[str, Any], Any]:
        """一次物理 POST。会话外的失败都不重试；网络类失败计入熔断。"""
        if self._closed:
            raise MCPError("客户端已关闭", retryable=False)
        deadline = deadline if deadline is not None else self._deadline()
        self._check_deadline(deadline)
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
        if len(body) > self._max_request_bytes:
            raise MCPError(
                f"请求体超过上限 {self._max_request_bytes} 字节，拒绝发送",
                retryable=False,
            )
        request_id = payload.get("id")
        try:
            with self._http.stream(
                "POST", self._url, content=body, headers=headers
            ) as resp:
                try:
                    status = resp.status_code
                    if status == 404:
                        if recovery_done:
                            raise MCPError(
                                "HTTP 404：端点或会话持续无效", retryable=False
                            )
                        raise _SessionLost("HTTP 404：会话失效", retryable=False)
                    if 300 <= status < 400:
                        raise MCPError("服务器返回重定向，已拒绝跟随", retryable=False)
                    ctype = resp.headers.get("content-type", "")
                    if notification:
                        if status in (200, 202, 204):
                            self._budget.record_success(self._server_id)
                            return {}, resp.headers
                        if status not in _AUTH_HTTP_STATUS:
                            self._budget.record_failure(self._server_id)
                        raise MCPError(
                            f"通知被拒绝: HTTP {status}",
                            retryable=status in _RETRYABLE_HTTP_STATUS,
                        )
                    if status >= 400:
                        if status not in _AUTH_HTTP_STATUS:
                            self._budget.record_failure(self._server_id)
                        raise MCPError(
                            f"MCP server 返回 HTTP {status}",
                            retryable=status in _RETRYABLE_HTTP_STATUS,
                        )
                    if "text/event-stream" in ctype:
                        msg = self._rpc_over_sse(resp, request_id, deadline=deadline)
                    elif "application/json" in ctype:
                        raw = self._read_capped(resp, deadline=deadline)
                        try:
                            msg = json.loads(raw)
                        except (ValueError, RecursionError) as exc:
                            raise MCPError(
                                f"响应不是合法 JSON: {type(exc).__name__}"
                            ) from exc
                    else:
                        raise MCPError(f"不支持的响应类型: {ctype or '缺失'}")
                    if not isinstance(msg, dict):
                        raise MCPError("JSON-RPC 响应不是对象")
                    if request_id is not None and msg.get("id") != request_id:
                        raise MCPError(
                            "JSON-RPC 响应 id 与请求不匹配，丢弃", retryable=False
                        )
                    self._budget.record_success(self._server_id)
                    return msg, resp.headers
                finally:
                    # 不保留服务端下发的 Cookie：否则后续请求会自动回显 Cookie 头。
                    self._http.cookies.clear()
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

    def _sse_lines(
        self, resp: httpx.Response, *, deadline: Optional[float] = None
    ) -> Iterator[bytes]:
        """按 SSE 规范切行：支持 ``\\r\\n``、``\\n`` 与裸 ``\\r``。

        逐 chunk 检查总字节上限与 deadline；关闭流由调用方的 ``with`` 负责。
        """
        total_bytes = 0
        pending = b""
        for chunk in resp.iter_bytes():
            self._check_deadline(deadline)
            total_bytes += len(chunk)
            if total_bytes > self._max_response_bytes:
                raise MCPError("SSE 响应超过字节上限", retryable=False)
            pending += chunk
            while True:
                idx, sep = _split_sse_line(pending)
                if idx < 0:
                    break
                line, pending = pending[:idx], pending[idx + sep:]
                yield line
        if pending:
            # 流结束：末尾裸 CR 当行尾；否则把没有行尾的最后一行也交出去。
            if pending.endswith(b"\r"):
                pending = pending[:-1]
            if pending:
                yield pending

    def _iter_sse_events(
        self, resp: httpx.Response, *, deadline: Optional[float] = None
    ) -> Iterator[Tuple[Optional[str], str]]:
        data_lines: List[str] = []
        event_type: Optional[str] = None
        event_bytes = 0
        for line in self._sse_lines(resp, deadline=deadline):
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
        self,
        events: Iterator[Tuple[Optional[str], str]],
        request_id: Optional[int],
        *,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        for event_type, data in events:
            self._check_deadline(deadline)
            if event_type not in (None, "message"):
                # 未知事件类型不许崩，跳过继续等 message。
                logger.debug("忽略未知 SSE 事件类型: %s", event_type)
                continue
            try:
                msg = json.loads(data)
            except RecursionError as exc:
                raise MCPError("SSE 数据嵌套过深，不是合法 JSON") from exc
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if request_id is not None and msg.get("id") != request_id:
                continue
            return msg
        raise MCPError("SSE 流中未找到本请求的响应", retryable=True)

    def _rpc_over_sse(
        self,
        resp: httpx.Response,
        request_id: Optional[int],
        *,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self._first_rpc_message(
            self._iter_sse_events(resp, deadline=deadline),
            request_id,
            deadline=deadline,
        )

    # ------------------------------------------------------------------ legacy SSE

    def _legacy_initialize(self, *, deadline: Optional[float] = None) -> Dict[str, Any]:
        deadline = deadline if deadline is not None else self._deadline()
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
        msg = self._legacy_rpc(
            payload["method"],
            payload["params"],
            request_id=payload["id"],
            deadline=deadline,
        )
        result = _extract_result(msg, payload["id"])
        served = result.get("protocolVersion") if isinstance(result, dict) else None
        if served not in SUPPORTED_PROTOCOL_VERSIONS:
            raise MCPError(f"服务器协议版本不受支持: {served!r}")
        self._protocol_version = served
        with contextlib.suppress(MCPError):
            # legacy 通道对通知常常不回话；发不出去也不影响后续 POST。
            self._legacy_notification("notifications/initialized", deadline=deadline)
        self._ready = True
        return result

    def _legacy_rpc(
        self,
        method: str,
        params: Dict[str, Any],
        *,
        request_id: Optional[int] = None,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id if request_id is not None else next(self._ids),
            "method": method,
            "params": params,
        }
        return self._legacy_exchange(payload, deadline=deadline)

    def _legacy_notification(
        self, method: str, *, deadline: Optional[float] = None
    ) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        self._legacy_exchange(payload, notification=True, deadline=deadline)

    def _legacy_exchange(
        self,
        payload: Dict[str, Any],
        *,
        notification: bool = False,
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        """打开 SSE GET 流 → 等 endpoint 事件 → 向该同源地址 POST → 等 message 响应。

        最简兼容实现：每个逻辑请求一条流。endpoint 必须与配置地址同源
        （scheme + hostname + 有效端口 三元组全等），且不得带 userinfo/query/fragment
        ——本版不支持 "endpoint 里带 session token" 的部署形态，凭据只走显式 headers。
        """
        deadline = deadline if deadline is not None else self._deadline()
        self._check_deadline(deadline)
        self._check_url(self._url, what="请求地址")
        request_id = payload.get("id")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > self._max_request_bytes:
            raise MCPError(
                f"请求体超过上限 {self._max_request_bytes} 字节，拒绝发送",
                retryable=False,
            )
        get_headers = {"Accept": "text/event-stream"}
        if self._protocol_version:
            get_headers["MCP-Protocol-Version"] = self._protocol_version
        try:
            with self._http.stream("GET", self._url, headers=get_headers) as stream:
                try:
                    gstatus = stream.status_code
                    if 300 <= gstatus < 400:
                        raise MCPError("服务器返回重定向，已拒绝跟随", retryable=False)
                    if gstatus >= 400:
                        if gstatus not in _AUTH_HTTP_STATUS:
                            self._budget.record_failure(self._server_id)
                        raise MCPError(
                            f"legacy SSE 连接失败: HTTP {gstatus}",
                            retryable=gstatus in _RETRYABLE_HTTP_STATUS,
                        )
                    endpoint: Optional[str] = None
                    # 同一条 GET 流上的事件生成器只许建一次，POST 之后继续从它取响应。
                    events = self._iter_sse_events(stream, deadline=deadline)
                    for event_type, data in events:
                        if event_type == "endpoint":
                            endpoint = data.strip()
                            break
                        if event_type not in (None, "message"):
                            logger.debug("忽略未知 SSE 事件类型: %s", event_type)
                    if not endpoint:
                        raise MCPError("legacy SSE 流缺少 endpoint 事件")
                    target = urljoin(self._url, endpoint)
                    self._check_legacy_target(target)
                    self._check_url(target, what="endpoint 地址")
                    post_headers = {
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    }
                    if self._protocol_version:
                        post_headers["MCP-Protocol-Version"] = self._protocol_version
                    if self._session_id:
                        post_headers["Mcp-Session-Id"] = self._session_id
                    with self._http.stream(
                        "POST", target, content=body, headers=post_headers
                    ) as post_resp:
                        try:
                            pstatus = post_resp.status_code
                            if 300 <= pstatus < 400:
                                raise MCPError(
                                    "服务器返回重定向，已拒绝跟随", retryable=False
                                )
                            if pstatus >= 400:
                                if pstatus not in _AUTH_HTTP_STATUS:
                                    self._budget.record_failure(self._server_id)
                                raise MCPError(
                                    f"legacy POST 失败: HTTP {pstatus}",
                                    retryable=pstatus in _RETRYABLE_HTTP_STATUS,
                                )
                            ctype = post_resp.headers.get("content-type", "")
                            if notification and pstatus in (200, 202, 204):
                                self._budget.record_success(self._server_id)
                                return {}
                            if pstatus in (202, 204):
                                # 202/204 是"先受理、回音在 GET 流上"：空 body 不许
                                # 先当 JSON 解析报"不是合法 JSON"。
                                return self._complete_legacy(
                                    events, request_id, deadline
                                )
                            if "application/json" in ctype:
                                raw = self._read_capped(post_resp, deadline=deadline)
                                if raw.strip():
                                    try:
                                        msg = json.loads(raw)
                                    except (ValueError, RecursionError) as exc:
                                        raise MCPError(
                                            "legacy POST 响应不是合法 JSON"
                                        ) from exc
                                    if not isinstance(msg, dict):
                                        raise MCPError("legacy POST 响应不是 JSON 对象")
                                    if (
                                        request_id is not None
                                        and msg.get("id") != request_id
                                    ):
                                        raise MCPError(
                                            "JSON-RPC 响应 id 与请求不匹配，丢弃",
                                            retryable=False,
                                        )
                                    self._budget.record_success(self._server_id)
                                    return msg
                            return self._complete_legacy(events, request_id, deadline)
                        finally:
                            self._http.cookies.clear()
                finally:
                    self._http.cookies.clear()
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

    def _complete_legacy(
        self,
        events: Iterator[Tuple[Optional[str], str]],
        request_id: Optional[int],
        deadline: float,
    ) -> Dict[str, Any]:
        msg = self._first_rpc_message(events, request_id, deadline=deadline)
        self._budget.record_success(self._server_id)
        return msg

    def _check_legacy_target(self, target: str) -> None:
        parsed = urlparse(target)
        if parsed.username is not None or parsed.password is not None:
            raise MCPError("legacy endpoint 含 userinfo，拒绝", retryable=False)
        if parsed.query or parsed.fragment:
            raise MCPError(
                "legacy endpoint 含 query/fragment，拒绝"
                "（凭据只走显式 headers，query 还会被 httpx 的 INFO 日志打出）",
                retryable=False,
            )
        if _effective_origin(target) != _effective_origin(self._url):
            raise MCPError(
                "legacy endpoint 指向异源地址（scheme/host/端口任一不同），拒绝",
                retryable=False,
            )

    # ------------------------------------------------------------------ 外壳工具

    def _check_url(self, url: str, *, what: str) -> None:
        # 构造期已拒绝 None；这里再挡一次，防止绕过构造或改私有属性后 fail-open。
        if self._url_guard is None:
            raise MCPError("URL 守卫未配置，拒绝请求", retryable=False)
        reason = self._url_guard(url)
        if reason:
            raise MCPError(f"{what} 被 URL 守卫拒绝: {reason}", retryable=False)

    def _read_capped(
        self, resp: httpx.Response, *, deadline: Optional[float] = None
    ) -> bytes:
        buf = bytearray()
        for chunk in resp.iter_bytes():
            self._check_deadline(deadline)
            buf += chunk
            if len(buf) > self._max_response_bytes:
                raise MCPError("响应超过字节上限", retryable=False)
        return bytes(buf)

    @contextlib.contextmanager
    def _budgeted(self) -> Iterator[None]:
        """公开入口取一个并发槽位；嵌套调用（会话恢复里的重初始化）不重复取。"""
        if self._closed:
            raise MCPError("客户端已关闭", retryable=False)
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


def _split_sse_line(buf: bytes) -> Tuple[int, int]:
    """在缓冲里找第一个行尾，返回 ``(行内容结束下标, 行尾字节数)``。

    ``(-1, 0)`` 表示还不够（无行尾，或末尾是可能是跨块 CRLF 的 ``\\r``）。
    """
    nl = buf.find(b"\n")
    cr = buf.find(b"\r")
    if nl < 0 and cr < 0:
        return -1, 0
    if cr >= 0 and (nl < 0 or cr < nl):
        if cr == len(buf) - 1:
            return -1, 0  # 末尾 \r：等下一块确认是否 CRLF
        return (cr, 2) if buf[cr + 1 : cr + 2] == b"\n" else (cr, 1)
    return nl, 1


def _effective_origin(url: str) -> Tuple[str, str, int]:
    """``(scheme, hostname.lower(), effective_port)``。

    显式端口优先；没有显式端口时 https=443、http=80，其余 scheme 记 -1。
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else -1
    return scheme, host, port


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
