"""v0.37.5 T2：MCP stdio 传输客户端（换行分帧 JSON-RPC，自研，只用 stdlib）。

本文件只管**一个** stdio MCP server 的一段管道：spawn 由调用方注入的 ``spawner``
完成（T3 换成带 Job Object / 最小环境 / 固定 cwd 的实现，本文件不做 ``Popen``），
这里负责：

- 换行分帧：一行一个 JSON 对象，UTF-8 strict；容忍 ``\\r\\n`` 与首行 BOM；
  拒绝 JSON 数组；``Content-Length:``（LSP 分帧）出现即协议错误，不做双识别。
- 上限：单行 512KB（``readline(limit)`` 在分配前判，超限或缺换行即协议错误）、
  请求体 256KB（写前拒，``retryable=False``，不杀进程）。
- 单在途：``initialize`` / ``list_tools`` / ``call_tool`` 共用一把非阻塞锁；忙 →
  ``MCPError(retryable=True, "该 MCP server 正忙")``。stdio 没有多路复用，排队只会
  把延迟叠成雪崩。出站 id 每进程从 1 递增；:meth:`start` 重新计数。
- 响应匹配：只接受 ``id`` 全等且形状合法的响应；错 id / 未知 id / 迟到重复一律
  丢弃并计数，**不串给下一次**；连续 3 次判协议失步并杀进程。单次请求期间通知
  >1000 条判洪水并杀。
- 入站 JSON 严格化：``parse_constant`` 拒绝 NaN / Infinity；合法代理对合成为真码点，
  孤立代理项与任何无法严格回写的值一律协议错误并杀。``result`` / ``error`` 形状
  不对同样杀，不给下游留半截形状。
- server 主动请求（``id`` + ``method``）：立即回 JSON-RPC ``-32601``；绝不反向执行
  sampling / roots / elicitation。通知只计数，绝不进模型。
- 握手：``initialize`` 后校验 ``result.protocolVersion`` 白名单，成功后发
  ``notifications/initialized``；握手墙钟默认 20s。
- ``tools/list`` 分页 ≤5 页 / ≤64 条；cursor 重复判死循环；超页超条杀进程。
- ``tools/call`` 默认墙钟 30s；``result.isError`` 与 JSON-RPC ``error`` 都**不杀**
  进程（对端正常回了话，只是工具自己报错）；超时先尽力发
  ``notifications/cancelled``（非阻塞，最多等 100ms）再 ``terminate``，文案固定
  写明"结果未知，不要自动重试"。
- EOF：在途请求立刻收到 ``MCPError(retryable=True, "server 已退出")`` 并
  ``wait(timeout=5)`` 收尸；EOF 但进程仍活（stdout 关了）→ 协议错误并杀。
- 写侧：专用 writer 线程 + 有界队列（16）；队列满 / 写失败判服务器不读 stdin 并杀。
  服务线程绝不直接阻塞在 ``write()`` 上。
- 读侧：独立 reader 线程始终排空 stdout；stderr 独立线程，逐行去控制字符（保留
  ``\\t``）、单行 4KB、每分钟 256KB、环形 20 行，脱敏后只进
  ``logging.getLogger("app.core.mcp_stdio")``。stdout 原文绝不进日志 / 异常 / 模型，
  错误只记 ``len`` 与 ``sha256[:16]``（hex 转储可逆，等于写回原文，不用）。
- ``close()`` 幂等：注销在途错误 → 关 stdin → 等 3s → ``terminate`` → join 线程 →
  关闭句柄；重复调用不抛。

工具注册、审批、频控、进程隔离、supervisor 都不在这里。
"""
from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import logging
import queue
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from app.core.mcp_client import (
    DEFAULT_PROTOCOL_VERSION,
    MAX_LIST_PAGES,
    MAX_LIST_TOOLS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MCPError,
    SUPPORTED_PROTOCOL_VERSIONS,
)

logger = logging.getLogger("app.core.mcp_stdio")

__all__ = ["MCPStdioClient"]

DEFAULT_HANDSHAKE_TIMEOUT = 20.0
DEFAULT_CALL_TIMEOUT = 30.0
# 写侧有界队列：服务线程只投递，不直接写管道。
WRITE_QUEUE_MAX = 16
# 单次请求期间的通知上限：超过即判异常 server。
MAX_NOTIFICATIONS_PER_REQUEST = 1000
# 连续未知 / 错配 id 的响应上限：超过即判协议失步。
MAX_STRAY_RESPONSES = 3
CLOSE_GRACE_SECONDS = 3.0
EOF_GRACE_SECONDS = 0.25
CANCEL_WAIT_SECONDS = 0.1
STDERR_LINE_LIMIT = 4096
STDERR_BYTES_PER_MINUTE = 256 * 1024
STDERR_RING = 20
STDERR_WINDOW_SECONDS = 60.0

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_REDACTIONS: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
    (re.compile(r"ghp_[A-Za-z0-9]{10,}"), "ghp_***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), "Bearer ***"),
    (
        re.compile(
            r"(?i)\b(token|secret|password|passwd|api[_-]?key)\s*[=:]\s*[^\s,;]+"
        ),
        r"\1=***",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{10,}\b"), "AKIA***"),
)


def _fingerprint(raw: bytes) -> str:
    """stdout 行的取证指纹：只留长度与截断哈希，原文绝不进日志 / 异常。"""
    return f"len={len(raw)} sha256={hashlib.sha256(raw).hexdigest()[:16]}"


def _reject_json_constant(token: str) -> Any:
    """``json.loads`` 的 ``parse_constant``：拒绝 NaN / Infinity 这类非标准 JSON。"""
    raise ValueError(f"非标准 JSON 常量: {token}")


def _normalize_surrogates(value: Any) -> Any:
    """把 JSON 转义解出的合法代理对合成真码点，孤立代理项原样保留。

    严格回写（``ensure_ascii=False`` + UTF-8）只拒绝孤立代理项；合法代理对若不合成就
    会被误杀，所以先归一化。非字符串 / 不含代理项的字符串原样返回。
    """
    if isinstance(value, str):
        if not any("\ud800" <= char <= "\udfff" for char in value):
            return value
        try:
            return value.encode("utf-16-le", "surrogatepass").decode(
                "utf-16-le", "surrogatepass"
            )
        except (UnicodeEncodeError, UnicodeDecodeError):
            return value
    if isinstance(value, dict):
        return {
            _normalize_surrogates(key): _normalize_surrogates(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_surrogates(item) for item in value]
    return value


def _same_id(left: Any, right: Any) -> bool:
    """id 全等：类型也必须一致（``True == 1`` 不算同一个 id）。"""
    return type(left) is type(right) and left == right


def _default_terminate(proc: Any) -> None:
    proc.kill()


def _client_version() -> str:
    """``clientInfo.version``：build_version 去前导 v；取不到用 ``dev``。"""
    try:
        from app.core.buildinfo import build_version

        value = (build_version() or "").strip()
    except Exception:  # 版本函数异常绝不能打死客户端
        return "dev"
    if value.startswith("v"):
        value = value[1:]
    return value or "dev"


def _sanitize_stderr_line(text: str) -> str:
    cleaned = _CONTROL_CHARS.sub("", text.replace("\r", ""))
    cleaned = cleaned.strip(" \t")
    for pattern, replacement in _REDACTIONS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned[:STDERR_LINE_LIMIT]


def _extract_result(message: Dict[str, Any]) -> Any:
    if "error" in message:
        data = message.get("error")
        if isinstance(data, dict):
            code = data.get("code")
            text = data.get("message") or "未知错误"
        else:
            code = None
            text = "错误对象形状非法"
        raise MCPError(
            f"JSON-RPC 错误 {code}: {text}",
            retryable=False,
            jsonrpc_code=code if isinstance(code, int) and not isinstance(code, bool) else None,
        )
    if "result" not in message:
        raise MCPError("JSON-RPC 响应缺少 result 字段", retryable=False)
    return message["result"]


def _tool_error_text(result: Dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    return text[:500]
    return "工具执行失败（无错误文本）"


class _Pending:
    """一条在途请求：reader 线程交付，服务线程消费。"""

    __slots__ = ("request_id", "event", "message", "error", "notifications")

    def __init__(self, request_id: Any) -> None:
        self.request_id = request_id
        self.event = threading.Event()
        self.message: Optional[Dict[str, Any]] = None
        self.error: Optional[MCPError] = None
        self.notifications = 0


class MCPStdioClient:
    """一个 stdio MCP server 的协议客户端（进程由注入的 ``spawner`` 创建）。"""

    def __init__(
        self,
        *,
        spawner: Callable[[], Any],
        terminate: Optional[Callable[[Any], None]] = None,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        line_limit: int = MAX_RESPONSE_BYTES,
        request_limit: int = MAX_REQUEST_BYTES,
        max_pages: int = MAX_LIST_PAGES,
        max_tools: int = MAX_LIST_TOOLS,
        client_name: str = "ai-assistant",
        client_version: Optional[str] = None,
    ) -> None:
        if not callable(spawner):
            raise MCPError("spawner 必须是可调用对象", retryable=False)
        if handshake_timeout <= 0 or call_timeout <= 0:
            raise MCPError("超时必须是正数", retryable=False)
        if line_limit <= 0 or request_limit <= 0:
            raise MCPError("限额必须是正数", retryable=False)
        if max_pages <= 0 or max_tools <= 0:
            raise MCPError("分页/工具上限必须是正数", retryable=False)
        self._spawner = spawner
        self._terminate_fn = terminate if terminate is not None else _default_terminate
        self._handshake_timeout = float(handshake_timeout)
        self._call_timeout = float(call_timeout)
        self._line_limit = int(line_limit)
        self._request_limit = int(request_limit)
        self._max_pages = int(max_pages)
        self._max_tools = int(max_tools)
        self._client_name = client_name
        self._client_version = client_version if client_version is not None else _client_version()

        self._proc: Any = None
        self._started = False
        self._closed = False
        self._ready = False
        self._protocol_version: Optional[str] = None
        self._initialize_result: Optional[Dict[str, Any]] = None
        self._ids = itertools.count(1)
        self._busy = threading.Lock()
        self._state_lock = threading.Lock()
        self._kill_lock = threading.Lock()
        self._pending: Optional[_Pending] = None
        self._fatal_error: Optional[MCPError] = None
        self._stray_streak = 0
        self._first_line = True
        self._writer_stop = threading.Event()
        self._write_q: "queue.Queue[bytes]" = queue.Queue(maxsize=WRITE_QUEUE_MAX)
        self._threads: List[threading.Thread] = []
        self._stderr_ring: Deque[str] = deque(maxlen=STDERR_RING)
        self._stderr_window_start = 0.0
        self._stderr_window_bytes = 0
        self._stats: Dict[str, Any] = {
            "pid": None,
            "requests_sent": 0,
            "responses": 0,
            "stray_responses": 0,
            "notifications": 0,
            "server_requests": 0,
            "protocol_errors": 0,
            "timeouts": 0,
            "kills": 0,
            "write_failures": 0,
            "stderr_lines": 0,
            "stderr_dropped_bytes": 0,
            "exit_code": None,
        }

    # ------------------------------------------------------------------ 只读状态

    @property
    def pid(self) -> Optional[int]:
        return self._stats["pid"]

    @property
    def alive(self) -> bool:
        proc = self._proc
        return bool(
            self._started
            and not self._closed
            and self._fatal_error is None
            and proc is not None
            and proc.poll() is None
        )

    @property
    def stats(self) -> Dict[str, Any]:
        data = dict(self._stats)
        data["ready"] = self._ready
        data["alive"] = self.alive
        return data

    @property
    def protocol_version(self) -> Optional[str]:
        return self._protocol_version

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> Optional[int]:
        """调用 spawner 拿进程并起三个 daemon 线程；本方法自己不做 ``Popen``。"""
        if self._closed:
            raise MCPError("MCP stdio 客户端已关闭", retryable=False)
        if self._started:
            raise MCPError("MCP stdio 客户端已启动", retryable=False)
        try:
            proc = self._spawner()
        except Exception as exc:  # spawner 的异常类型不可控，只留类型名
            raise MCPError(
                f"启动 MCP server 进程失败: {type(exc).__name__}", retryable=False
            ) from exc
        if proc is None or any(
            getattr(proc, attr, None) is None
            for attr in ("stdin", "stdout", "stderr", "poll", "wait", "kill")
        ):
            self._safe_kill(proc)
            raise MCPError("spawner 返回的进程对象形状不合法", retryable=False)
        self._proc = proc
        self._started = True
        self._stats["pid"] = getattr(proc, "pid", None)
        self._ids = itertools.count(1)
        self._first_line = True
        self._writer_stop.clear()
        for target, label in (
            (self._reader_main, "reader"),
            (self._stderr_main, "stderr"),
            (self._writer_main, "writer"),
        ):
            thread = threading.Thread(
                target=target,
                name=f"mcp-stdio-{label}-{getattr(proc, 'pid', '?')}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        return getattr(proc, "pid", None)

    def initialize(self) -> Dict[str, Any]:
        with self._busy_guard():
            self._check_usable()
            if self._ready:
                return dict(self._initialize_result or {})
            return self._initialize_locked()

    def list_tools(self) -> List[Dict[str, Any]]:
        with self._busy_guard():
            self._check_usable()
            self._ensure_ready_locked()
            deadline = time.monotonic() + self._call_timeout
            tools: List[Dict[str, Any]] = []
            cursor: Optional[str] = None
            seen_cursors: set = set()
            for _page in range(1, self._max_pages + 1):
                params: Dict[str, Any] = {}
                if cursor is not None:
                    params["cursor"] = cursor
                result = self._rpc("tools/list", params, deadline=deadline)
                if not isinstance(result, dict):
                    error = MCPError(
                        "MCP stdio 协议错误：tools/list 的 result 不是对象",
                        retryable=False,
                    )
                    self._violate(error)
                    raise error
                page_tools = result.get("tools")
                if not isinstance(page_tools, list):
                    error = MCPError(
                        "MCP stdio 协议错误：tools/list 响应缺少 tools 数组",
                        retryable=False,
                    )
                    self._violate(error)
                    raise error
                for item in page_tools:
                    if not isinstance(item, dict):
                        error = MCPError(
                            "MCP stdio 协议错误：tools/list 中存在非对象的工具条目",
                            retryable=False,
                        )
                        self._violate(error)
                        raise error
                tools.extend(page_tools)
                if len(tools) > self._max_tools:
                    error = MCPError(
                        f"MCP stdio 协议错误：工具数超过上限 {self._max_tools}，fail-closed",
                        retryable=False,
                    )
                    self._violate(error)
                    raise error
                next_cursor = result.get("nextCursor")
                if next_cursor is None:
                    return tools
                if not isinstance(next_cursor, str):
                    error = MCPError(
                        "MCP stdio 协议错误：tools/list 的 nextCursor 不是字符串",
                        retryable=False,
                    )
                    self._violate(error)
                    raise error
                if next_cursor == "":
                    error = MCPError(
                        "MCP stdio 协议错误：tools/list 的 nextCursor 是空字符串"
                        "（只有 null/缺省才表示结束）",
                        retryable=False,
                    )
                    self._violate(error)
                    raise error
                if next_cursor in seen_cursors:
                    error = MCPError(
                        "MCP stdio 协议错误：tools/list 的 cursor 重复，判定死循环",
                        retryable=False,
                    )
                    self._violate(error)
                    raise error
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            error = MCPError(
                f"MCP stdio 协议错误：tools/list 分页超过上限 {self._max_pages} 页",
                retryable=False,
            )
            self._violate(error)
            raise error

    def call_tool(
        self, name: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        with self._busy_guard():
            self._check_usable()
            self._ensure_ready_locked()
            deadline = time.monotonic() + self._call_timeout
            result = self._rpc(
                "tools/call",
                {"name": name, "arguments": arguments or {}},
                deadline=deadline,
            )
            if not isinstance(result, dict):
                error = MCPError(
                    "MCP stdio 协议错误：tools/call 的 result 不是对象",
                    retryable=False,
                )
                self._violate(error)
                raise error
            if result.get("isError") is True:
                # 对端正常回了话，只是工具自己报错：不杀进程、算传输成功。
                raise MCPError(
                    f"工具 {name} 返回错误: {_tool_error_text(result)}",
                    retryable=False,
                )
            return result

    def close(self) -> None:
        """幂等关闭：注销在途 → 关 stdin → 等 3s → terminate → join → 关句柄。"""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._ready = False
            pending = self._pending
            self._pending = None
        if pending is not None:
            pending.error = MCPError("MCP stdio 客户端已关闭", retryable=True)
            pending.event.set()
        self._writer_stop.set()
        proc = self._proc
        if proc is not None:
            stdin = getattr(proc, "stdin", None)
            if stdin is not None:
                with contextlib.suppress(Exception):
                    stdin.close()
            try:
                proc.wait(timeout=CLOSE_GRACE_SECONDS)
            except Exception:
                self._terminate()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2.0)
        current = threading.current_thread()
        for thread in list(self._threads):
            if thread is current:
                continue
            with contextlib.suppress(Exception):
                thread.join(timeout=2.0)
        if proc is not None:
            for attr in ("stdout", "stderr", "stdin"):
                stream = getattr(proc, attr, None)
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()

    def __enter__(self) -> "MCPStdioClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ 握手/调用内部

    @contextlib.contextmanager
    def _busy_guard(self):
        if not self._busy.acquire(blocking=False):
            raise MCPError("该 MCP server 正忙", retryable=True)
        try:
            yield
        finally:
            self._busy.release()

    def _check_usable(self) -> None:
        if not self._started:
            raise MCPError("MCP stdio 客户端尚未启动", retryable=False)
        if self._closed:
            raise MCPError("MCP stdio 客户端已关闭", retryable=False)
        fatal = self._fatal_error
        if fatal is not None:
            raise MCPError(
                str(fatal), retryable=fatal.retryable, jsonrpc_code=fatal.jsonrpc_code
            )
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise MCPError("server 已退出", retryable=True)

    def _ensure_ready_locked(self) -> None:
        if not self._ready:
            self._initialize_locked()

    def _initialize_locked(self) -> Dict[str, Any]:
        deadline = time.monotonic() + self._handshake_timeout
        params = {
            "protocolVersion": DEFAULT_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": self._client_name,
                "version": self._client_version,
            },
        }
        result = self._rpc("initialize", params, deadline=deadline)
        if not isinstance(result, dict):
            error = MCPError(
                "MCP stdio 协议错误：initialize 的 result 不是对象", retryable=False
            )
            self._violate(error)
            raise error
        version = result.get("protocolVersion")
        if not isinstance(version, str) or version not in SUPPORTED_PROTOCOL_VERSIONS:
            error = MCPError(
                f"MCP stdio 协议错误：server 协商的协议版本不受支持（{str(version)[:64]}）",
                retryable=False,
            )
            self._violate(error)
            raise error
        self._initialize_result = result
        self._protocol_version = version
        self._ready = True
        try:
            self._send(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                deadline=deadline,
            )
        except MCPError:
            self._ready = False
            raise
        return result

    def _rpc(
        self, method: str, params: Dict[str, Any], *, deadline: float
    ) -> Any:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise MCPError("server 已退出", retryable=True)
        request_id = next(self._ids)
        payload = self._encode(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        pending = _Pending(request_id)
        with self._state_lock:
            if self._pending is not None:
                raise MCPError("该 MCP server 正忙", retryable=True)
            # close() / EOF / 协议失败与注册 pending 之间存在窄窗口：在同一把锁内
            # 复查一次，命中就给 pending 塞明确错误并放弃注册，绝不空等整段超时。
            if self._closed:
                pending.error = MCPError("MCP stdio 客户端已关闭", retryable=True)
            elif self._fatal_error is not None:
                fatal = self._fatal_error
                pending.error = MCPError(
                    str(fatal),
                    retryable=fatal.retryable,
                    jsonrpc_code=fatal.jsonrpc_code,
                )
            elif self._writer_stop.is_set():
                pending.error = MCPError("server 已退出", retryable=True)
            else:
                self._pending = pending
        if pending.error is not None:
            raise pending.error
        try:
            self._put_payload(payload, deadline)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not pending.event.wait(remaining):
                self._on_timeout(method, request_id)
            if pending.error is not None:
                raise pending.error
            if pending.message is None:
                raise MCPError(
                    "MCP stdio 协议错误：响应丢失", retryable=False
                )
            return _extract_result(pending.message)
        finally:
            with self._state_lock:
                if self._pending is pending:
                    self._pending = None

    def _on_timeout(self, method: str, request_id: Any) -> None:
        self._stats["timeouts"] += 1
        # 尽力发取消通知：非阻塞入队，最多等 100ms 让 writer 取走。
        with contextlib.suppress(Exception):
            payload = self._encode(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": request_id, "reason": "timeout"},
                }
            )
            self._write_q.put_nowait(payload)
            end = time.monotonic() + CANCEL_WAIT_SECONDS
            while time.monotonic() < end and not self._write_q.empty():
                time.sleep(0.005)
        error = MCPError(
            f"MCP server 响应超时（{method}），已终止该进程；结果未知，不要自动重试",
            retryable=False,
        )
        with self._state_lock:
            if self._fatal_error is None:
                self._fatal_error = error
            self._writer_stop.set()
        self._terminate()
        proc = self._proc
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.wait(timeout=5.0)
        raise error

    def _send(self, message: Dict[str, Any], *, deadline: float) -> None:
        self._put_payload(self._encode(message), deadline)

    def _encode(self, message: Dict[str, Any]) -> bytes:
        try:
            text = json.dumps(message, ensure_ascii=False, allow_nan=False)
            payload = text.encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise MCPError(
                f"请求参数无法序列化为 JSON: {type(exc).__name__}", retryable=False
            ) from exc
        if len(payload) > self._request_limit:
            raise MCPError(
                f"请求体 {len(payload)} 字节超过上限 {self._request_limit} 字节，未发送",
                retryable=False,
            )
        return payload

    def _put_payload(self, payload: bytes, deadline: float) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise MCPError("server 已退出", retryable=True)
        remaining = max(0.0, deadline - time.monotonic())
        try:
            self._write_q.put(payload, timeout=remaining)
        except queue.Full:
            error = MCPError(
                "MCP server 不读 stdin（写队列已满），已终止该进程", retryable=False
            )
            self._violate(error)
            raise error
        self._stats["requests_sent"] += 1

    # ------------------------------------------------------------------ reader

    def _reader_main(self) -> None:
        stream = getattr(self._proc, "stdout", None)
        if stream is None:
            return
        try:
            while not self._closed:
                try:
                    raw = stream.readline(self._line_limit + 1)
                except (ValueError, OSError):
                    return
                if not raw:
                    self._handle_eof()
                    return
                if not raw.endswith(b"\n"):
                    self._violate(
                        MCPError(
                            "MCP stdio 协议错误：单行超过上限 "
                            f"{self._line_limit} 字节或缺少换行（{_fingerprint(raw)}）",
                            retryable=False,
                        )
                    )
                    return
                body = raw[:-1]
                if body.endswith(b"\r"):
                    body = body[:-1]
                if not body:
                    continue
                try:
                    text = body.decode("utf-8")
                except UnicodeDecodeError:
                    self._violate(
                        MCPError(
                            "MCP stdio 协议错误：stdout 行不是严格 UTF-8"
                            f"（{_fingerprint(raw)}）",
                            retryable=False,
                        )
                    )
                    return
                if self._first_line:
                    self._first_line = False
                    if text.startswith("\ufeff"):
                        text = text[1:]
                if not text.strip():
                    continue
                if text.strip().lower().startswith("content-length:"):
                    self._violate(
                        MCPError(
                            "MCP stdio 协议错误：收到 Content-Length 分帧"
                            "（LSP 样式），本客户端只支持换行分帧",
                            retryable=False,
                        )
                    )
                    return
                try:
                    message = json.loads(text, parse_constant=_reject_json_constant)
                except (ValueError, RecursionError):
                    # JSONDecodeError 是 ValueError 子类；而超长整数等会让 int() 抛
                    # 普通 ValueError，同样必须走协议错误，不能让 reader 线程带
                    # traceback 死掉、在途请求只剩超时。
                    self._violate(
                        MCPError(
                            "MCP stdio 协议错误：stdout 行不是合法 JSON"
                            f"（{_fingerprint(raw)}）",
                            retryable=False,
                        )
                    )
                    return
                message = _normalize_surrogates(message)
                try:
                    # 严格回写一次：NaN / Infinity / 孤立代理项等 T4 无法再序列化的
                    # 值必须在这里就判协议错误，不能带进下游。
                    json.dumps(message, ensure_ascii=False, allow_nan=False).encode(
                        "utf-8"
                    )
                except (ValueError, UnicodeEncodeError, RecursionError):
                    self._violate(
                        MCPError(
                            "MCP stdio 协议错误：stdout 行含无法严格回写的内容"
                            "（NaN/Infinity/孤立代理项）",
                            retryable=False,
                        )
                    )
                    return
                if not self._handle_message(message):
                    return
        finally:
            pass

    def _handle_message(self, message: Any) -> bool:
        if isinstance(message, list):
            self._violate(
                MCPError(
                    "MCP stdio 协议错误：拒绝 JSON 批量数组，只收单个对象",
                    retryable=False,
                )
            )
            return False
        if not isinstance(message, dict):
            self._violate(
                MCPError(
                    "MCP stdio 协议错误：JSON-RPC 顶层不是对象", retryable=False
                )
            )
            return False
        if message.get("jsonrpc") != "2.0":
            self._violate(
                MCPError(
                    'MCP stdio 协议错误：jsonrpc 字段不是 "2.0"', retryable=False
                )
            )
            return False
        if "method" in message:
            if "id" in message:
                # server 主动请求：立即拒绝，绝不反向执行。
                self._stats["server_requests"] += 1
                self._reply_unsupported(message.get("id"))
                return not self._closed
            self._stats["notifications"] += 1
            pending = self._pending
            if pending is not None:
                pending.notifications += 1
                if pending.notifications > MAX_NOTIFICATIONS_PER_REQUEST:
                    self._violate(
                        MCPError(
                            "MCP server 通知洪水（单次请求期间超过 "
                            f"{MAX_NOTIFICATIONS_PER_REQUEST} 条），已终止该进程",
                            retryable=False,
                        )
                    )
                    return False
            return True
        if "id" not in message:
            self._violate(
                MCPError(
                    "MCP stdio 协议错误：JSON-RPC 消息既无 method 也无 id",
                    retryable=False,
                )
            )
            return False
        has_result = "result" in message
        has_error = "error" in message
        if has_result == has_error:
            self._violate(
                MCPError(
                    "MCP stdio 协议错误：响应必须且只能有 result 或 error 之一",
                    retryable=False,
                )
            )
            return False
        if has_error and not isinstance(message.get("error"), dict):
            # error 为 null / list 等形状：不是 JSON-RPC 错误对象，判协议错误并杀，
            # 不能只抛 MCPError 却把进程留下来。
            self._violate(
                MCPError(
                    "MCP stdio 协议错误：JSON-RPC error 字段不是对象",
                    retryable=False,
                )
            )
            return False
        pending = self._pending
        if pending is None or not _same_id(pending.request_id, message.get("id")):
            # 错 id / 未知 id / 迟到重复：丢弃并计数，绝不串给下一次调用。
            self._stats["stray_responses"] += 1
            self._stray_streak += 1
            if self._stray_streak >= MAX_STRAY_RESPONSES:
                self._violate(
                    MCPError(
                        "MCP stdio 协议错误：连续 "
                        f"{MAX_STRAY_RESPONSES} 次收到未知/错配 id 的响应，判定协议失步",
                        retryable=False,
                    )
                )
                return False
            return True
        self._stray_streak = 0
        self._stats["responses"] += 1
        with self._state_lock:
            if self._pending is not pending:
                return True
            self._pending = None
            pending.message = message
        pending.event.set()
        return True

    def _reply_unsupported(self, request_id: Any) -> None:
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": "client does not support server-initiated requests",
            },
        }
        try:
            payload = self._encode(message)
        except MCPError as exc:
            # server 请求的 id 不可回写（不可序列化 / 回包超过请求上限）：不能静默
            # 吞掉，否则对端永远等不到 -32601、客户端也毫无记录。
            self._violate(
                MCPError(
                    f"MCP stdio 协议错误：无法回复 server 请求（{exc}）",
                    retryable=False,
                )
            )
            return
        try:
            self._write_q.put_nowait(payload)
        except queue.Full:
            self._violate(
                MCPError(
                    "MCP server 不读 stdin（写队列已满），已终止该进程",
                    retryable=False,
                )
            )

    def _handle_eof(self) -> None:
        proc = self._proc
        if proc is None:
            return
        code = proc.poll()
        if code is None:
            # 进程退出与 stdout EOF 之间有毫秒级时序差，给一个短宽限。
            try:
                code = proc.wait(timeout=EOF_GRACE_SECONDS)
            except Exception:
                code = proc.poll()
        if code is None:
            self._violate(
                MCPError(
                    "MCP stdio 协议错误：stdout 已关闭但进程仍在运行",
                    retryable=False,
                )
            )
            return
        self._stats["exit_code"] = code
        self._writer_stop.set()
        self._fail_pending(MCPError("server 已退出", retryable=True))

    # ------------------------------------------------------------------ writer / stderr

    def _writer_main(self) -> None:
        stdin = getattr(self._proc, "stdin", None)
        if stdin is None:
            return
        while not self._writer_stop.is_set():
            try:
                payload = self._write_q.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                stdin.write(payload)
                stdin.flush()
            except Exception:
                self._stats["write_failures"] += 1
                self._writer_stop.set()
                self._fail_pending(
                    MCPError("server 已退出（写入 stdin 失败）", retryable=True)
                )
                self._terminate()
                return

    def _stderr_main(self) -> None:
        stream = getattr(self._proc, "stderr", None)
        if stream is None:
            return
        while not self._closed:
            try:
                raw = stream.readline(STDERR_LINE_LIMIT + 1)
            except (ValueError, OSError):
                return
            if not raw:
                return
            # 先脱敏再截断：截断放在 _sanitize_stderr_line 末尾，避免 4KB 边界把
            # ``sk-...`` 截成不足 6 字符后绕过打码。
            text = _sanitize_stderr_line(raw.decode("utf-8", "replace"))
            if not text:
                continue
            now = time.monotonic()
            if now - self._stderr_window_start >= STDERR_WINDOW_SECONDS:
                self._stderr_window_start = now
                self._stderr_window_bytes = 0
            size = len(text.encode("utf-8"))
            if self._stderr_window_bytes + size > STDERR_BYTES_PER_MINUTE:
                self._stats["stderr_dropped_bytes"] += size
                continue
            self._stderr_window_bytes += size
            self._stats["stderr_lines"] += 1
            self._stderr_ring.append(text)
            with contextlib.suppress(Exception):
                logger.info("mcp stdio stderr: %s", text)

    # ------------------------------------------------------------------ 终止/失败

    def _fail_pending(self, error: MCPError) -> None:
        with self._state_lock:
            pending = self._pending
            self._pending = None
        if pending is not None:
            pending.error = error
            pending.event.set()

    def _violate(self, error: MCPError) -> None:
        """协议失败：记 fatal、唤醒在途、杀进程；调用方决定是否 raise。"""
        with self._state_lock:
            if self._fatal_error is None:
                self._fatal_error = error
            self._stats["protocol_errors"] += 1
            self._writer_stop.set()
        self._fail_pending(error)
        self._terminate()
        proc = self._proc
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.wait(timeout=5.0)
            with contextlib.suppress(Exception):
                self._stats["exit_code"] = proc.poll()

    def _terminate(self) -> None:
        with self._kill_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return
            try:
                self._terminate_fn(proc)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
            self._stats["kills"] += 1

    @staticmethod
    def _safe_kill(proc: Any) -> None:
        if proc is None:
            return
        with contextlib.suppress(Exception):
            proc.kill()
