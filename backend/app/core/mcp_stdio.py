"""v0.37.5 T4：stdio MCP supervisor（状态机 / 退避 / 熔断 / 审计 / 状态快照）。

职责边界：一个 supervisor 管**至多 MAX_SERVERS 条** stdio server，每条一个 worker
线程 + 一个 :class:`MCPStdioClient`（T2）+ 一个 :class:`StdioProcess`（T3）。
工具注册 / 审批 / 频控在 T5，管理端点在 T6，本文件只做"有界、可观测、可停"。

状态机（计划 §3.2）：:

    disabled ──(全局&条目 enabled)──▶ starting ──(spawn+initialize+list)──▶ ready
       ▲                                  │                                    │
       │                                  ├─ config_error（终态，不重启）       │
       │                                  └─ transport_error ─▶ backoff ─▶ starting
       │                                                                       │
       └──(disable/remove)── stopping ◀── crash / max_life ◀──────────────────┘
                                      │
                                      └─ 5 次失败 ──▶ circuit_open（cooldown）

几条刻意的口径（与计划 §2.7 / §3.7 对齐）：

- **配置错不重试**：``MCPStdioConfigError`` 与 ``MCPError(retryable=False)``（版本不符、
  server 拒绝 initialize）都进 ``config_error`` 终态，只等 reload。否则"命令路径写错"
  会变成无限重启。传输类错误（``retryable=True``、EOF、超时、进程崩溃、写管道失败）
  才走退避 + 熔断。
- **熔断计数只在传输失败时发生**：每次成功就绪都 ``record_success``，所以 8h 到点回收
  不会毒化熔断（刚 ready 过就清零）。
- **circuit_open 到期只探一次**：预算里的 ``check`` 只负责闸门；探活失败后由 supervisor
  自己的 ``circuit_until`` 再压一个 cooldown，绝不在 check 松口的瞬间打成热循环。
- **审计只落摘要**：``id`` / ``pid`` / ``argv_fingerprint``（只 hash 命令与 flag）/
  ``command_basename`` / ``env_names``（allowlist 名字）。args 的值、env 的值、完整
  命令行、stderr 正文一律不进审计、不进 status、不进日志。
- **绝不在 import 期读配置或起进程**：``get_supervisor()`` 只做懒创建，``start()`` 才
  读配置；worker 是 daemon，``start()`` 立即返回，不阻塞服务启动。
"""
from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.core import audit as audit_module
from app.core.config_store import read as config_store_read
from app.core.mcp_client import MCPConnectionBudget, MCPError
from app.core.mcp_stdio_client import (
    DEFAULT_CALL_TIMEOUT,
    DEFAULT_HANDSHAKE_TIMEOUT,
    MCPStdioClient,
)
from app.core.mcp_stdio_config import (
    MCP_ENABLED_KEY,
    MCP_SERVERS_KEY,
    normalize_entry,
)
from app.core.mcp_stdio_process import (
    MCPStdioConfigError,
    argv_fingerprint,
    command_basename,
    spawn_stdio,
)
from app.core.paths import data_root

logger = logging.getLogger("app.core.mcp_stdio")

__all__ = ["STATES", "StdioSupervisor", "get_supervisor"]

# 生命周期状态（计划 §3.2）。对外只暴露这八个字符串。
STATE_DISABLED = "disabled"
STATE_STARTING = "starting"
STATE_READY = "ready"
STATE_CRASHED = "crashed"
STATE_BACKOFF = "backoff"
STATE_STOPPING = "stopping"
STATE_CIRCUIT_OPEN = "circuit_open"
STATE_CONFIG_ERROR = "config_error"

STATES = frozenset({
    STATE_DISABLED,
    STATE_STARTING,
    STATE_READY,
    STATE_CRASHED,
    STATE_BACKOFF,
    STATE_STOPPING,
    STATE_CIRCUIT_OPEN,
    STATE_CONFIG_ERROR,
})

# 计划 §2.5：工具曝光 16/server（桥接层在 T5 截断；这里只报数字）。
MAX_TOOLS_EXPOSED = 16

DEFAULT_BACKOFF: Tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0)
DEFAULT_MAX_LIFE = 8 * 3600.0
DEFAULT_MONITOR_INTERVAL = 0.05
DEFAULT_COOLDOWN = 900.0
STOP_JOIN_TIMEOUT = 5.0
TEARDOWN_WAIT_SECONDS = 5.0

# 退避抖动的幅度：±20%。单点抖动避免多个 server 在同一秒一起重启。
_JITTER = 0.2

# 配置错误原因里允许出现在 last_error / 审计里的形状：只留字母数字与 :._-，
# 长度封顶。宁可截断也不把整段异常文本（可能含路径/参数）带出去。
_REASON_CLEAN = re.compile(r"[^A-Za-z0-9_.:-]+")

# ``mcp.stdio.refused`` 的 reason 枚举（计划 §2.7）。把 T3 的短码收敛到这张表，
# 表外的值退化成 ``command_problem`` / ``protocol_error``，不兜底原始异常文本。
_REFUSED_BY_COMMAND_PROBLEM = {
    "not_absolute": "command_not_absolute",
    "unc_or_device_path": "command_not_absolute",
    "missing_or_not_file": "command_missing",
    "not_executable": "command_missing",
    "host_denied": "command_denied",
    "not_exe": "command_denied",
}


def _default_mcp_root() -> str:
    """工作目录根：``<data_root()>/data/mcp``（计划 §2.6）。"""
    return os.path.join(data_root(), "data", "mcp")


def _clean_reason(text: Any, *, limit: int = 64) -> str:
    """把异常文本收成短码：只留安全字符，截断。绝不回显整条命令。"""
    raw = str(text or "").strip() or "error"
    return _REASON_CLEAN.sub("_", raw)[:limit]


def _refused_reason(text: str) -> str:
    """把 T3 的短码映射成审计枚举。"""
    raw = str(text or "")
    for token in raw.split(":"):
        mapped = _REFUSED_BY_COMMAND_PROBLEM.get(token)
        if mapped:
            return mapped
    if raw.startswith("spawn_failed"):
        return "spawn_failed"
    if raw.startswith("env_"):
        return "env_denied"
    return "command_problem"


def _transport_error(exc: MCPError, client: Optional[MCPStdioClient]) -> bool:
    """这条 ``MCPError`` 算传输失败（可退避）还是配置错（终态）。

    T2 把"响应超时"与"不读 stdin"标成 ``retryable=False``（重试会让结果双花或
    无意义），但它们在 supervisor 眼里是传输问题，必须退避 + 计熔断。用客户端
    统计里的计数器与固定文案判，不解析完整异常消息。
    """
    if getattr(exc, "retryable", False):
        return True
    stats: Dict[str, Any] = {}
    if client is not None:
        try:
            stats = client.stats
        except Exception:                                    # noqa: BLE001 - 只读快照
            stats = {}
    if stats.get("timeouts") or stats.get("write_failures"):
        return True
    text = str(exc)
    return any(marker in text for marker in ("不读 stdin", "写入 stdin 失败", "已退出"))


def _was_timeout(client: Optional[MCPStdioClient]) -> bool:
    if client is None:
        return False
    try:
        return bool(client.stats.get("timeouts"))
    except Exception:                                        # noqa: BLE001
        return False


def _schema_flags(tools: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """status 里的 schema 概览：只报计数，绝不回显第三方 schema 原文。"""
    with_schema = 0
    object_schema = 0
    no_properties = 0
    for tool in tools:
        schema = tool.get("inputSchema") if isinstance(tool, dict) else None
        if isinstance(schema, dict):
            with_schema += 1
            if schema.get("type") == "object":
                object_schema += 1
            if not schema.get("properties"):
                no_properties += 1
    return {
        "total": len(tools),
        "input_schema": with_schema,
        "object_schema": object_schema,
        "no_properties": no_properties,
    }


class _Runtime:
    """一个 server 条目的运行态。所有字段由 supervisor 的 ``_lock`` 保护。"""

    def __init__(self, server_id: str, entry: Dict[str, Any]) -> None:
        self.server_id = server_id
        self.entry: Dict[str, Any] = dict(entry)
        self.state = STATE_DISABLED
        self.last_error = ""
        self.client: Optional[MCPStdioClient] = None
        self.wrapper: Any = None
        self.pid: Optional[int] = None
        self.spawned_at: Optional[float] = None
        self.spawns = 0
        self.failures = 0
        self.tools: List[Dict[str, Any]] = []
        self.circuit_until = 0.0
        self.circuit_audited = False
        self.probe_mode = False
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None


class StdioSupervisor:
    """stdio server 的守护者：起停、退避、熔断、审计与状态快照。

    所有可测点都从构造参数注入（配置读取 / 工作目录 / 退避表 / 寿命 / 轮询间隔 /
    握手与调用超时 / 预算 / 审计 / 时钟 / sleep），默认值就是生产值。
    """

    def __init__(
        self,
        *,
        config_reader: Optional[Callable[[], Dict[str, Any]]] = None,
        mcp_root: Optional[str] = None,
        backoff: Sequence[float] = DEFAULT_BACKOFF,
        max_life: float = DEFAULT_MAX_LIFE,
        monitor_interval: float = DEFAULT_MONITOR_INTERVAL,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        budget: Optional[MCPConnectionBudget] = None,
        audit_fn: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        intervals = tuple(float(item) for item in backoff)
        if not intervals or any(item < 0 for item in intervals):
            raise ValueError("backoff 必须是非空的非负数序列")
        if monitor_interval <= 0:
            raise ValueError("monitor_interval 必须为正数")
        if max_life < 0:
            raise ValueError("max_life 不能为负数")
        if handshake_timeout <= 0 or call_timeout <= 0:
            raise ValueError("握手/调用超时必须为正数")

        self._config_reader = config_reader if config_reader is not None else config_store_read
        self._mcp_root = os.path.abspath(mcp_root) if mcp_root else _default_mcp_root()
        self._backoff = intervals
        self._max_life = float(max_life)
        self._monitor_interval = float(monitor_interval)
        self._handshake_timeout = float(handshake_timeout)
        self._call_timeout = float(call_timeout)
        self._budget = budget if budget is not None else MCPConnectionBudget(
            global_limit=3,
            per_server_limit=1,
            failure_threshold=5,
            failure_window=600.0,
            breaker_cooldown=DEFAULT_COOLDOWN,
        )
        self._audit_fn = audit_fn if audit_fn is not None else audit_module.log
        self._clock = clock
        self._sleep = sleep
        # 预算的 cooldown 是熔断语义的一部分；预算类没有公开 getter，按属性读并给
        # 一个与默认值一致的兜底（测试注入的小 cooldown 因此自动生效）。
        self._cooldown = float(
            getattr(
                self._budget,
                "breaker_cooldown",
                getattr(self._budget, "_breaker_cooldown", DEFAULT_COOLDOWN),
            )
        )

        self._lock = threading.RLock()
        self._reload_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._runtimes: Dict[str, _Runtime] = {}
        self._global_enabled = False
        self._started = False

    # ------------------------------------------------------------------ 对外 API

    def start(self) -> Dict[str, Any]:
        """读配置并起 worker；立即返回（不 spawn、不阻塞启动）。幂等。"""
        with self._reload_lock:
            self._apply_config(self._read_config())
            self._started = True
        return self.status()

    def shutdown(self) -> None:
        """置停、收尸、join 线程。幂等；之后 ``status()`` 里没有 running server。"""
        with self._reload_lock:
            self._shutdown.set()
            with self._lock:
                runtimes = list(self._runtimes.values())
            for runtime in runtimes:
                self._stop_runtime(runtime)
            with self._lock:
                for runtime in runtimes:
                    runtime.state = STATE_DISABLED
                    runtime.thread = None
            self._started = False

    def reload(self) -> Dict[str, Any]:
        """重读配置：停掉被删/被禁/变更的条目，起新增/重启变更的条目。"""
        with self._reload_lock:
            self._apply_config(self._read_config())
        return self.status()

    def status(self) -> Dict[str, Any]:
        """状态快照。**绝不**返回 args/env 的值、stderr 正文或完整命令行。"""
        with self._lock:
            enabled = bool(self._global_enabled)
            runtimes = [self._runtimes[key] for key in sorted(self._runtimes)]
            servers = [self._snapshot(runtime) for runtime in runtimes]
        return {"enabled": enabled, "servers": servers}

    def is_ready(self, server_id: str) -> bool:
        with self._lock:
            runtime = self._runtimes.get(server_id)
            if runtime is None or runtime.state != STATE_READY:
                return False
            client = runtime.client
        return bool(client is not None and client.alive)

    def ready_servers(self) -> List[str]:
        with self._lock:
            candidates = [
                (runtime.server_id, runtime.client)
                for runtime in self._runtimes.values()
                if runtime.state == STATE_READY
            ]
        return sorted(
            server_id
            for server_id, client in candidates
            if client is not None and client.alive
        )

    def client_for(self, server_id: str) -> Optional[MCPStdioClient]:
        """给 T5 用的读口：只在 ready 且进程活着时返回客户端，否则 ``None``。"""
        with self._lock:
            runtime = self._runtimes.get(server_id)
            if runtime is None or runtime.state != STATE_READY:
                return None
            client = runtime.client
        if client is None or not client.alive:
            return None
        return client

    def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """T5 的调用口：未就绪给一条明确的、可重试的错误，绝不空等。"""
        client = self.client_for(server_id)
        if client is None:
            raise MCPError(
                "该 MCP stdio server 未就绪（可能正在重启或已配置错误）",
                retryable=True,
            )
        return client.call_tool(tool_name, arguments)

    @property
    def budget(self) -> MCPConnectionBudget:
        return self._budget

    @property
    def mcp_root(self) -> str:
        return self._mcp_root

    # ------------------------------------------------------------------ 配置应用

    def _read_config(self) -> Optional[Dict[str, Any]]:
        try:
            config = self._config_reader()
        except Exception as exc:                             # noqa: BLE001 - 配置读取不许炸
            logger.warning("读取 MCP stdio 配置失败: %s", type(exc).__name__)
            return None
        return config if isinstance(config, dict) else None

    def _parse_entries(
        self, config: Dict[str, Any]
    ) -> List[Tuple[Dict[str, Any], str]]:
        """把配置表收成 ``[(entry, config_error_reason)]``。

        形状判断复用 T1 的 ``normalize_entry(verify_command=False)``（不碰磁盘：
        命令存在性留到 spawn 前判，避免慢盘/网络盘拖住 start）。坏条目保留 id、
        进 ``config_error``，这样管理端看得见"这条没起来"而不是凭空消失。
        """
        raw = config.get(MCP_SERVERS_KEY)
        if not isinstance(raw, list):
            return []
        out: List[Tuple[Dict[str, Any], str]] = []
        seen = set()
        for item in raw:
            server_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(server_id, str) or not server_id or server_id in seen:
                continue
            seen.add(server_id)
            normalized, reason = normalize_entry(item, verify_command=False)
            if normalized is None:
                command = item.get("command")
                entry = {
                    "id": server_id,
                    "command": command if isinstance(command, str) else "",
                    "args": [],
                    "env_allowlist": [],
                    "enabled": False,
                }
                out.append((entry, "config_error:" + _clean_reason(reason or "invalid_entry")))
                continue
            out.append((normalized, ""))
        return out

    def _apply_config(self, config: Optional[Dict[str, Any]]) -> None:
        """把配置差异落到运行态。调用方必须持有 ``_reload_lock``。"""
        if config is None:
            # 读配置失败：保持现状（不因为一次读盘抖动把所有 server 停掉）。
            return
        entries = self._parse_entries(config)
        global_enabled = bool(config.get(MCP_ENABLED_KEY, False))
        desired = {entry["id"]: (entry, reason) for entry, reason in entries}

        with self._lock:
            self._global_enabled = global_enabled
            existing = dict(self._runtimes)

        # 先停：被删的、全局关的、条目内容变了的。
        to_stop: List[_Runtime] = []
        for server_id, runtime in existing.items():
            if not global_enabled or server_id not in desired:
                to_stop.append(runtime)
                continue
            entry, _reason = desired[server_id]
            if runtime.entry != entry:
                to_stop.append(runtime)
        for runtime in to_stop:
            self._stop_runtime(runtime)
            with self._lock:
                if runtime.server_id not in desired:
                    self._runtimes.pop(runtime.server_id, None)

        # 再落新配置并决定哪些要起线程。
        planned: List[_Runtime] = []
        with self._lock:
            for server_id, (entry, reason) in desired.items():
                runtime = self._runtimes.get(server_id)
                if runtime is None:
                    runtime = _Runtime(server_id, entry)
                    self._runtimes[server_id] = runtime
                runtime.entry = dict(entry)
                if not global_enabled:
                    runtime.state = STATE_DISABLED
                    runtime.last_error = ""
                    continue
                if reason:
                    runtime.state = STATE_CONFIG_ERROR
                    runtime.last_error = reason
                    continue
                # 注意：``config_error`` 只在**条目变更**后重试。条目没变时 reload
                # 不是"配置变更"，绝不能把终态翻回 disabled 再 spawn 一次。
                if entry.get("enabled"):
                    planned.append(runtime)
                else:
                    runtime.state = STATE_DISABLED

        for runtime in planned:
            self._ensure_running(runtime)

    # ------------------------------------------------------------------ 线程起停

    def _ensure_running(self, runtime: _Runtime) -> None:
        with self._lock:
            if self._shutdown.is_set():
                return
            if runtime.thread is not None and runtime.thread.is_alive():
                return
            if not (self._global_enabled and runtime.entry.get("enabled")):
                runtime.state = STATE_DISABLED
                return
            if runtime.state == STATE_CONFIG_ERROR:
                return
            runtime.stop_event = threading.Event()
            runtime.state = STATE_STARTING
            thread = threading.Thread(
                target=self._worker,
                args=(runtime,),
                name="mcp-stdio-sup-%s" % runtime.server_id,
                daemon=True,
            )
            runtime.thread = thread
        thread.start()

    def _stop_runtime(self, runtime: _Runtime) -> None:
        """置停并收尸；可在任意线程调用（不持 ``_lock`` 做 join）。"""
        runtime.stop_event.set()
        self._set_state(runtime, STATE_STOPPING)
        uptime = self._uptime(runtime)
        self._teardown_process(runtime, reason="killed", uptime=uptime)
        thread = runtime.thread
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=STOP_JOIN_TIMEOUT)
        runtime.thread = None
        self._set_state(runtime, STATE_DISABLED)

    # ------------------------------------------------------------------ worker

    def _worker(self, runtime: _Runtime) -> None:
        try:
            while not self._shutdown.is_set() and not runtime.stop_event.is_set():
                if not self._active(runtime):
                    self._set_state(runtime, STATE_DISABLED)
                    self._wait(runtime, self._monitor_interval)
                    continue
                if runtime.state == STATE_CONFIG_ERROR:
                    # 终态：只等 reload 换掉 entry / 停线程。
                    self._wait(runtime, self._monitor_interval)
                    continue

                now = self._clock()
                if runtime.circuit_until > now:
                    self._set_state(runtime, STATE_CIRCUIT_OPEN)
                    self._wait(
                        runtime,
                        min(self._monitor_interval, runtime.circuit_until - now),
                    )
                    continue
                try:
                    self._budget.check(runtime.server_id)
                except MCPError:
                    # 熔断开着（首次由预算触发）：压一个本地 cooldown，到期只探一次。
                    self._open_circuit(runtime, failures=max(runtime.failures, 1))
                    continue
                if runtime.circuit_audited:
                    # 刚从 circuit_open 出来：这一次 spawn 是探活，成功才清零。
                    runtime.circuit_audited = False
                    runtime.probe_mode = True
                    self._set_state(runtime, STATE_STARTING)

                self._run_session(runtime)
        except Exception as exc:                             # noqa: BLE001 - worker 不许炸穿
            logger.warning(
                "MCP stdio supervisor worker 异常退出: %s %s",
                runtime.server_id,
                type(exc).__name__,
            )
            with self._lock:
                runtime.last_error = "worker_error:" + type(exc).__name__
            self._stop_runtime(runtime)
        finally:
            if runtime.stop_event.is_set() or self._shutdown.is_set():
                with self._lock:
                    if runtime.state in (STATE_STARTING, STATE_READY, STATE_BACKOFF,
                                         STATE_CRASHED, STATE_CIRCUIT_OPEN):
                        runtime.state = STATE_STOPPING

    def _active(self, runtime: _Runtime) -> bool:
        return bool(self._global_enabled and runtime.entry.get("enabled"))

    def _run_session(self, runtime: _Runtime) -> None:
        """一次完整生命周期：spawn → 握手 → list → 监控；返回即代表这一代已死。"""
        runtime.spawns += 1
        runtime.spawned_at = self._clock()
        self._set_state(runtime, STATE_STARTING)

        try:
            wrapper = spawn_stdio(runtime.entry, self._mcp_root)
        except MCPStdioConfigError as exc:
            reason = _clean_reason(exc)
            with self._lock:
                runtime.spawned_at = None
                runtime.last_error = "config_error:" + reason
                runtime.state = STATE_CONFIG_ERROR
            self._audit(
                "mcp.stdio.refused",
                runtime,
                {"id": runtime.server_id, "reason": _refused_reason(reason)},
            )
            return
        except OSError as exc:
            with self._lock:
                runtime.spawned_at = None
                runtime.last_error = "spawn_failed:" + type(exc).__name__
            self._transport_failure(runtime, failures_delta=1)
            return
        except Exception as exc:                             # noqa: BLE001 - 类型名足够
            with self._lock:
                runtime.spawned_at = None
                runtime.last_error = "spawn_failed:" + type(exc).__name__
            self._transport_failure(runtime, failures_delta=1)
            return

        with self._lock:
            runtime.wrapper = wrapper
            runtime.pid = getattr(wrapper, "pid", None)

        if runtime.stop_event.is_set() or self._shutdown.is_set():
            self._teardown_process(runtime, reason="killed", uptime=self._uptime(runtime))
            return

        client = MCPStdioClient(
            spawner=lambda: wrapper,
            terminate=lambda proc: proc.terminate_tree(),
            handshake_timeout=self._handshake_timeout,
            call_timeout=self._call_timeout,
        )
        with self._lock:
            runtime.client = client

        phase = "initialize"
        try:
            client.start()
            client.initialize()
            phase = "list_tools"
            tools = client.list_tools()
        except MCPError as exc:
            self._failed_start(runtime, client, exc, phase)
            return
        except Exception as exc:                             # noqa: BLE001 - 类型名足够
            with self._lock:
                runtime.last_error = "start_failed:" + type(exc).__name__
            self._teardown_process(
                runtime, reason="error", uptime=self._uptime(runtime)
            )
            self._transport_failure(runtime, failures_delta=1)
            return

        if runtime.stop_event.is_set() or self._shutdown.is_set():
            self._teardown_process(runtime, reason="killed", uptime=self._uptime(runtime))
            return

        with self._lock:
            runtime.tools = list(tools)
            runtime.state = STATE_READY
            runtime.last_error = ""
            runtime.failures = 0
            runtime.probe_mode = False
            runtime.circuit_audited = False
            pid = runtime.pid
        self._safely(self._budget.record_success, runtime.server_id)
        self._audit(
            "mcp.stdio.start",
            runtime,
            {
                "id": runtime.server_id,
                "pid": pid,
                "argv_fingerprint": argv_fingerprint(
                    runtime.entry.get("command", ""), runtime.entry.get("args") or []
                ),
                "command_basename": command_basename(runtime.entry.get("command", "")),
                "restarts": max(0, runtime.spawns - 1),
            },
        )
        self._monitor_ready(runtime, client, wrapper)

    def _failed_start(
        self,
        runtime: _Runtime,
        client: MCPStdioClient,
        exc: MCPError,
        phase: str,
    ) -> None:
        """握手 / list 阶段失败：传输类走退避，配置类进终态。"""
        uptime = self._uptime(runtime)
        if _transport_error(exc, client):
            timed_out = _was_timeout(client)
            with self._lock:
                runtime.last_error = ("timeout:" if timed_out else "error:") + type(exc).__name__
            self._teardown_process(
                runtime,
                reason="timeout" if timed_out else "error",
                uptime=uptime,
            )
            self._transport_failure(runtime, failures_delta=1)
            return

        reason = "protocol_version" if phase == "initialize" else "protocol_error"
        with self._lock:
            runtime.last_error = "config_error:" + type(exc).__name__
            runtime.state = STATE_CONFIG_ERROR
        self._teardown_process(runtime, reason="config", uptime=uptime)
        self._audit(
            "mcp.stdio.refused",
            runtime,
            {"id": runtime.server_id, "reason": reason},
        )

    def _monitor_ready(
        self,
        runtime: _Runtime,
        client: MCPStdioClient,
        wrapper: Any,
    ) -> None:
        """ready 期间：收敛崩溃、EOF 与 max_life。返回即代表这一代已收尸。"""
        while not self._shutdown.is_set() and not runtime.stop_event.is_set():
            uptime = self._uptime(runtime)
            exit_code = self._poll(wrapper)
            if exit_code is not None:
                with self._lock:
                    runtime.last_error = "crashed:%s" % exit_code
                self._teardown_process(
                    runtime,
                    reason="error" if exit_code else "eof",
                    uptime=uptime,
                )
                self._transport_failure(runtime, failures_delta=1)
                return
            if not client.alive:
                with self._lock:
                    runtime.last_error = "crashed:" + (
                        str(client.stats.get("exit_code"))
                        if client.stats.get("exit_code") is not None
                        else "unknown"
                    )
                self._teardown_process(runtime, reason="error", uptime=uptime)
                self._transport_failure(runtime, failures_delta=1)
                return
            if self._max_life and uptime >= self._max_life:
                with self._lock:
                    runtime.last_error = "max_life"
                # 到点回收：先审计 exit（reason=killed），再按传输失败退避重启。
                # 刚 ready 过已 record_success，所以寿命回收不会累积熔断。
                self._teardown_process(runtime, reason="killed", uptime=uptime)
                self._transport_failure(runtime, failures_delta=1)
                return
            self._wait(runtime, self._monitor_interval)

    # ------------------------------------------------------------------ 失败 / 熔断

    def _transport_failure(self, runtime: _Runtime, *, failures_delta: int = 1) -> None:
        """传输失败：计数 → 熔断或退避。停线程期间不再改状态。"""
        if self._shutdown.is_set() or runtime.stop_event.is_set():
            self._set_state(runtime, STATE_STOPPING)
            return
        with self._lock:
            runtime.failures += max(1, int(failures_delta))
            failures = runtime.failures
            probe = runtime.probe_mode
            runtime.probe_mode = False
        self._safely(self._budget.record_failure, runtime.server_id)

        if probe:
            # 熔断到期后的唯一一次探活失败：立刻重新压 cooldown，不放开热循环。
            self._open_circuit(runtime, failures=failures)
            return

        try:
            self._budget.check(runtime.server_id)
        except MCPError:
            self._open_circuit(runtime, failures=failures)
            return

        delay = self._backoff_delay(failures)
        self._set_state(runtime, STATE_BACKOFF)
        self._wait(runtime, delay)
        if self._shutdown.is_set() or runtime.stop_event.is_set():
            self._set_state(runtime, STATE_STOPPING)
        else:
            self._set_state(runtime, STATE_STARTING)

    def _backoff_delay(self, failures: int) -> float:
        index = min(max(0, failures - 1), len(self._backoff) - 1)
        base = self._backoff[index]
        return max(0.0, base * (1.0 + random.uniform(-_JITTER, _JITTER)))

    def _open_circuit(self, runtime: _Runtime, *, failures: int) -> None:
        with self._lock:
            runtime.circuit_until = self._clock() + self._cooldown
            runtime.state = STATE_CIRCUIT_OPEN
            runtime.last_error = "circuit_open"
            first = not runtime.circuit_audited
            runtime.circuit_audited = True
        if first:
            self._audit(
                "mcp.stdio.circuit_open",
                runtime,
                {
                    "id": runtime.server_id,
                    "failures": failures,
                    "cooldown_s": self._cooldown,
                },
            )

    # ------------------------------------------------------------------ 收尸 / 审计 / 快照

    def _teardown_process(self, runtime: _Runtime, *, reason: str, uptime: float) -> None:
        """关客户端、杀树、收尸、记一条 exit。幂等：谁先摘走引用谁记账。"""
        with self._lock:
            client = runtime.client
            wrapper = runtime.wrapper
            pid = runtime.pid
            runtime.client = None
            runtime.wrapper = None
            runtime.pid = None
            runtime.spawned_at = None
        if client is not None:
            try:
                client.close()
            except Exception as exc:                         # noqa: BLE001 - 收尸不许炸
                logger.debug("MCP stdio client.close 失败: %s", type(exc).__name__)
        if wrapper is not None:
            code = self._poll(wrapper)
            if code is None:
                try:
                    wrapper.terminate_tree()
                except Exception as exc:                     # noqa: BLE001
                    logger.debug("terminate_tree 失败: %s", type(exc).__name__)
            try:
                wrapper.wait(timeout=TEARDOWN_WAIT_SECONDS)
            except Exception:                                # noqa: BLE001 - 超时也继续
                pass
        if pid is None:
            return
        exit_code = self._poll(wrapper) if wrapper is not None else None
        self._audit(
            "mcp.stdio.exit",
            runtime,
            {
                "id": runtime.server_id,
                "pid": pid,
                "exit_code": exit_code,
                "uptime_s": round(max(0.0, float(uptime)), 3),
                "reason": reason,
            },
        )

    def _audit(self, action: str, runtime: _Runtime, after: Dict[str, Any]) -> None:
        """审计落摘要；写失败绝不连累 supervisor。actor 用固定服务身份。"""
        try:
            self._audit_fn(
                "stdio-supervisor",
                action,
                target=runtime.server_id,
                after=after,
            )
        except Exception as exc:                             # noqa: BLE001 - 审计不连累
            logger.warning("MCP stdio 审计写入失败: %s", type(exc).__name__)

    def _snapshot(self, runtime: _Runtime) -> Dict[str, Any]:
        entry = runtime.entry
        command = entry.get("command") or ""
        tools_total = len(runtime.tools)
        return {
            "id": runtime.server_id,
            "enabled": bool(entry.get("enabled")),
            "state": runtime.state,
            "pid": runtime.pid,
            "restarts": max(0, runtime.spawns - 1),
            "uptime_s": round(self._uptime(runtime), 3),
            "tools_total": tools_total,
            "tools_exposed": min(tools_total, MAX_TOOLS_EXPOSED),
            "schema_flags": _schema_flags(runtime.tools),
            "command": command,
            "command_basename": command_basename(command),
            "argv_fingerprint": argv_fingerprint(command, entry.get("args") or []),
            "env_names": list(entry.get("env_allowlist") or []),
            "cwd": os.path.join(self._mcp_root, runtime.server_id),
            "last_error": runtime.last_error,
        }

    # ------------------------------------------------------------------ 小工具

    def _set_state(self, runtime: _Runtime, state: str) -> None:
        with self._lock:
            runtime.state = state

    def _uptime(self, runtime: _Runtime) -> float:
        spawned_at = runtime.spawned_at
        if spawned_at is None:
            return 0.0
        return max(0.0, self._clock() - spawned_at)

    @staticmethod
    def _poll(wrapper: Any) -> Optional[int]:
        if wrapper is None:
            return None
        try:
            return wrapper.poll()
        except Exception:                                    # noqa: BLE001
            return None

    def _wait(self, runtime: _Runtime, seconds: float) -> bool:
        """可中断等待：返回 True 表示收到停止信号。按 monitor_interval 切片，
        这样 shutdown 不必等完整个 16s 退避。"""
        end = self._clock() + max(0.0, float(seconds))
        while True:
            if self._shutdown.is_set() or runtime.stop_event.is_set():
                return True
            remaining = end - self._clock()
            if remaining <= 0:
                return False
            self._sleep(min(self._monitor_interval, remaining))

    @staticmethod
    def _safely(func: Callable[..., Any], *args: Any) -> None:
        try:
            func(*args)
        except Exception as exc:                             # noqa: BLE001 - 预算是旁路
            logger.warning("MCP stdio 预算调用失败: %s", type(exc).__name__)


# --------------------------------------------------------------------------- 单例

_SUPERVISOR_LOCK = threading.Lock()
_SUPERVISOR: Optional[StdioSupervisor] = None


def get_supervisor(**overrides: Any) -> StdioSupervisor:
    """模块级懒加载单例。**不在 import 期读配置、不在 import 期 spawn。**

    无参数调用拿到默认实例；T6 接线时应调用 ``get_supervisor().start()``。
    带参数只允许在第一次创建时注入（测试便利），已存在时再传参会显式报错，
    避免"以为换了配置其实用的是旧实例"。
    """
    global _SUPERVISOR
    with _SUPERVISOR_LOCK:
        if _SUPERVISOR is None:
            _SUPERVISOR = StdioSupervisor(**overrides)
        elif overrides:
            raise ValueError("StdioSupervisor 单例已存在，不能再用参数替换")
        return _SUPERVISOR
