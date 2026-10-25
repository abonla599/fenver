"""v0.37.5 T7：MCP stdio 跨层对抗用例。

这份文件刻意**不重复** T1–T6 的单点用例（配置判据、协议帧、Job 限额、桥接撞名、
管理端点形状各自已有锁），只证明“几条防线叠在一起后仍然 fail-closed”：

1. 管理员闸 × schema 清洗 × freeform 丢参：声明 ``user_id`` 的工具注册期就没了，
   带合法票据的普通用户仍进不了 wrapper，``default_user`` 也只能拿到空参数；
2. 审批闸 × 票据属主/工具名：错工具、错属主都 0 spawn；错属主尝试不烧掉真票；
3. 审批闸 × 频控顺序：无票据尝试不占外部 20 次额度，第 21 次带新票也 ``tool_ran=False``；
4. 名字/来源 × 两 server 同名 × 16/32 上限 × 内置撞名：内置仍在、另一 server 照常注册；
5. 深嵌套 schema × 恶意工具名 × 兄弟 server：坏条目只拒自己，拒绝账本可 JSON 序列化；
6. 管理写入口 × env 边界 × 真实子进程：reserved/denied 名字 400，固定注入值胜出，
   父进程 canary 不进子进程；
7. 管理写入口 × 命令判据 × supervisor 真实 argv/cwd：坏命令 400，元字符原样、
   marker 不生成、cwd 固定在 ``<mcp_root>/<id>``；
8. Windows Job × 内存限额 × 祖孙 tree kill（POSIX 明确 skip，由 T3 的 killpg 用例覆盖）；
9. 传输毒帧（batch/大整数/600KB/深嵌套）只杀自己那台 server；错 id 与 NaN 不串响应；
10. 管理面 × 审计 × 状态：state=None 503、响应/审计无 args/env/stderr 诱饵、
    reload 阻塞时 /health 仍答；
11. lifespan：一台配置必坏、一台正常，服务仍 200，退出后子进程与注册表都清干净；
12. crashed/backoff 保留注册但 ``available=False``，disabled 才注销。

所有真实子进程用例 ``finally`` 收尸；Windows 孤儿断言只在单进程宿主（T3 的
``MCP_STDIO_TEST_EXE`` 或原生解释器）上执行，POSIX 分支如实 skip 并写明原因。
"""
import asyncio
import contextlib
import copy
import functools
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

import httpx
import pytest

from app.core import approval
from app.core import config_store
from app.core import mcp_stdio as stdio_mod
from app.core import mcp_stdio_bridge as bridge_mod
from app.core.config_store import MCP_ENABLED_KEY, MCP_SERVERS_KEY
from app.core.mcp_client import DEFAULT_PROTOCOL_VERSION, MCPConnectionBudget, MCPError
from app.core.mcp_stdio import (
    STATE_BACKOFF,
    STATE_CIRCUIT_OPEN,
    STATE_CONFIG_ERROR,
    STATE_CRASHED,
    STATE_DISABLED,
    STATE_READY,
    StdioSupervisor,
    _safe_copy_tools,
)
from app.core.mcp_stdio_bridge import (
    MAX_TOOLS_PER_SERVER,
    MAX_TOOLS_TOTAL,
    SOURCE_PREFIX,
    StdioToolBridge,
)
from app.core.mcp_stdio_client import MCPStdioClient
from app.core.mcp_stdio_process import (
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_JOB_MEMORY,
    JOB_OBJECT_LIMIT_JOB_TIME,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    build_env_plan,
    is_pid_alive,
    query_job_limits,
    spawn_stdio,
)
from app.main import app
from app.tools import executor as ex
from app.tools.registry import (
    is_available,
    register_tool,
    tools_registry,
)

SERVER = Path(__file__).with_name("fake_mcp_stdio_server.py")
_HELPER_EXE = os.environ.get("MCP_STDIO_TEST_EXE", "").strip()
WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows Job Object / taskkill；POSIX killpg 已由 "
           "test_v0375_mcp_stdio_process.py 真跑覆盖",
)

DECOY_ARG = "DECOY_ARG_T7_51bd"
DECOY_ENV_VALUE = "DECOY_ENV_T7_9c2e"
DECOY_STDERR = "DECOY_STDERR_T7_a4d0"
PARENT_KEY = "PARENT_CANARY_T7_7f31"
PARENT_PROXY = "http://proxy-canary-t7.invalid:8080"


# --------------------------------------------------------------------------- 夹具 / 小工具


@pytest.fixture(autouse=True)
def _restore_process_globals():
    """频控账本、注册表、审批票表都是进程级状态：用例之间必须逐字还原。"""
    freq = {key: list(value) for key, value in ex._FREQ_LEDGER.items()}
    registry_before = set(tools_registry)
    with approval._lock:
        tickets_before = dict(approval._tickets)
        fused_before = dict(approval._round_fused)
    yield
    ex._FREQ_LEDGER.clear()
    ex._FREQ_LEDGER.update(freq)
    for name in list(tools_registry):
        if name not in registry_before:
            dict.pop(tools_registry, name, None)
    with approval._lock:
        approval._tickets.clear()
        approval._tickets.update(tickets_before)
        approval._round_fused.clear()
        approval._round_fused.update(fused_before)


@pytest.fixture
def config_sandbox(tmp_path, monkeypatch):
    """配置落点与内存缓存都挪进临时域；结束后逐字还原缓存。"""
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CONFIG_DB_PATH", str(path))
    with config_store._lock:
        previous = copy.deepcopy(config_store._config)
        config_store._config.clear()
        config_store._config.update(copy.deepcopy(config_store.DEFAULTS))
    try:
        yield path
    finally:
        with config_store._lock:
            config_store._config.clear()
            config_store._config.update(previous)


@pytest.fixture
def fresh_supervisor(monkeypatch):
    """把模块级单例摘掉，让 lifespan 真的新建一个；结束后停掉并还原。"""
    previous = stdio_mod._SUPERVISOR
    monkeypatch.setattr(stdio_mod, "_SUPERVISOR", None)
    yield
    current = stdio_mod._SUPERVISOR
    if current is not None:
        with contextlib.suppress(Exception):
            current.shutdown()


def _schema(**props):
    return {"type": "object", "properties": dict(props)}


def _tool(name, *, description="演示工具", schema=None):
    item = {"name": name, "description": description}
    item["inputSchema"] = (
        {"type": "object", "properties": {}} if schema is None else schema)
    return item


def _deep_schema(depth=300):
    schema = {"type": "object", "properties": {}}
    node = schema
    for _ in range(depth):
        child = {"type": "object", "properties": {}}
        node["properties"]["n"] = child
        node = child
    return schema


def _hash8(server_id, raw_name):
    return hashlib.sha256((server_id + "\0" + raw_name).encode("utf-8")).hexdigest()[:8]


def _mapped(server_id, raw_name):
    return bridge_mod._mapped_tool_name(server_id, raw_name)


def _ticket(name, user_id):
    request = approval.create_approval_request(name, {}, user_id=user_id)
    ticket = approval.consume_ticket(request["ticket_id"], "allow", user_id)
    assert ticket is not None
    return ticket


def _fake_auth(roles):
    class _Auth:
        def role_of(self, user_id):
            return roles.get(user_id, "user")
    return _Auth()


def _wait_until(predicate, timeout, interval=0.02, message="条件未满足"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    value = predicate()
    assert value, message
    return value


def _wait_state(supervisor, server_id, states, timeout=8.0):
    states = (states,) if isinstance(states, str) else tuple(states)

    def _match():
        for row in supervisor.status()["servers"]:
            if row.get("id") == server_id:
                return row.get("state") in states
        return False

    return _wait_until(_match, timeout, message="%s 未进入 %s" % (server_id, states))


def _missing_command():
    name = "fenver-v0375-no-such.exe" if os.name == "nt" else "fenver-v0375-no-such"
    path = os.path.join(os.path.abspath(os.sep), "fenver-v0375-no-such-dir", name)
    assert not os.path.exists(path)
    return path


def _read_json(path, timeout=5.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            last = exc
            time.sleep(0.02)
    raise AssertionError("未读到有效 JSON: %s (%s)" % (path, last))


def _invocation(mode, record_dir, extra=()):
    if _HELPER_EXE:
        return _HELPER_EXE, [mode, str(record_dir), *extra]
    return sys.executable, [str(SERVER), mode, str(record_dir), *extra]


@functools.lru_cache(maxsize=1)
def _single_process_host():
    if _HELPER_EXE:
        return True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pid.txt"
            proc = subprocess.Popen(
                [sys.executable, "-c",
                 "import os,sys;open(sys.argv[1],'w').write(str(os.getpid()))",
                 str(out)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc.wait(timeout=20)
            return out.read_text(encoding="utf-8").strip() == str(proc.pid)
    except (OSError, subprocess.SubprocessError):
        return False


SPAWN_OK = pytest.mark.skipif(
    not _single_process_host(),
    reason="当前解释器是启动器（Popen pid != 真实进程 pid）；"
           "设 MCP_STDIO_TEST_EXE 指向单进程 fake server helper",
)


class _Config:
    def __init__(self, enabled, servers):
        self.enabled = enabled
        self.servers = servers

    def __call__(self):
        return {
            MCP_ENABLED_KEY: self.enabled,
            MCP_SERVERS_KEY: [dict(item) for item in self.servers],
        }


def _script_entry(mode, record_dir, server_id, *, extra_args=()):
    """强制走仓库里**当前这份** fake server（helper 可能早于某个新模式构建）。"""
    return {
        "id": server_id,
        "command": sys.executable,
        "args": [str(SERVER), mode, str(record_dir), *extra_args],
        "env_allowlist": [],
        "enabled": True,
    }


def _config_entry(mode, record_dir, server_id, *, extra_args=(), command=None,
                  env_allowlist=(), enabled=True):
    default_command, default_args = _invocation(mode, record_dir, extra_args)
    return {
        "id": server_id,
        "command": command or default_command,
        "args": default_args,
        "env_allowlist": list(env_allowlist),
        "enabled": enabled,
    }


def _make_supervisor(config, tmp_path, **overrides):
    options = {
        "config_reader": config,
        "mcp_root": str(tmp_path / "mcp"),
        "backoff": (0.01, 0.02, 0.04),
        "monitor_interval": 0.01,
        "handshake_timeout": 5.0,
        "call_timeout": 2.0,
        "budget": MCPConnectionBudget(
            global_limit=4, per_server_limit=1, failure_threshold=5,
            failure_window=60.0, breaker_cooldown=0.5,
        ),
    }
    options.update(overrides)
    return StdioSupervisor(**options)


class BridgeSupervisor:
    """桥接层最小替身：只实现 T5 依赖的接口，并记录每次 wrapper 调用。"""

    def __init__(self, tools=None, ready=None):
        self.tools = {sid: list(value) for sid, value in (tools or {}).items()}
        self.ready = set(self.tools) if ready is None else set(ready)
        self.callbacks = {}
        self.exposed = {}
        self.calls = []

    def set_tool_callbacks(self, on_ready=None, on_disabled=None):
        self.callbacks = {"ready": on_ready, "disabled": on_disabled}

    def ready_tools(self):
        return {sid: _safe_copy_tools(sid, self.tools[sid])
                for sid in self.ready if sid in self.tools}

    def is_ready(self, sid):
        return sid in self.ready

    def set_tools_exposed(self, sid, count):
        self.exposed[sid] = count

    def call_tool(self, sid, name, arguments=None):
        self.calls.append({"id": sid, "name": name,
                           "arguments": dict(arguments or {})})
        return {"content": [{"type": "text", "text": "T7:%s|%s" % (sid, name)}]}

    def fire_ready(self, sid, tools=None):
        if tools is not None:
            self.tools[sid] = list(tools)
        self.ready.add(sid)
        self.callbacks["ready"](sid, _safe_copy_tools(sid, self.tools[sid]))

    def fire_disabled(self, sid):
        self.ready.discard(sid)
        self.callbacks["disabled"](sid)


class EndpointSupervisor:
    """管理端点最小替身：status 深拷贝 + reload 计数/可注入阻塞。"""

    def __init__(self, status=None, reload_delay=0.0):
        self.reloads = 0
        self.reload_delay = reload_delay
        self._status = status or {"enabled": False, "servers": []}

    def status(self):
        return copy.deepcopy(self._status)

    def reload(self):
        self.reloads += 1
        if self.reload_delay:
            time.sleep(self.reload_delay)


def _admin_entry(server_id="files", **overrides):
    body = {
        "id": server_id,
        "command": sys.executable,
        "args": [],
        "enabled": False,
    }
    body.update(overrides)
    return body


def _assert_report_name_safe(name):
    assert isinstance(name, str) and name
    assert len(name) <= stdio_mod.REPORT_NAME_LIMIT
    assert all(unicodedata.category(ch) not in ("Cc", "Cf") for ch in name)
    name.encode("utf-8")


# --------------------------------------------------------------------------- 1. 管理员闸纵深


def test_admin_gate_combines_schema_rejection_freeform_drop_and_identity(monkeypatch):
    """声明 user_id 的工具注册期消失；freeform 的 user_id 参数执行期丢弃。

    组合判据：普通用户拿到**有效票据**仍 0 spawn（管理员闸在审批闸之后）；
    default_user 带票据只执行一次，且 wrapper 收到的是空参数——模型既不能借
    schema 声明 user_id，也不能借 freeform 参数夹带身份。
    """
    sid = "srvcombo"
    supervisor = BridgeSupervisor({sid: [
        _tool("id_pinned", schema=_schema(user_id={"type": "string"},
                                          q={"type": "string"})),
        _tool("free", schema={"type": "object", "additionalProperties": True}),
    ]})
    bridge = StdioToolBridge(supervisor)
    bridge.attach()
    supervisor.fire_ready(sid)

    assert _mapped(sid, "id_pinned") not in tools_registry
    assert any(item["name"] == "id_pinned" and "user_id" in item["reason"]
               for item in bridge.rejection_report()[sid])
    free_name = _mapped(sid, "free")
    assert free_name in tools_registry

    monkeypatch.setattr(
        bridge_mod, "_current_auth_store",
        lambda: _fake_auth({"u_admin": "admin", "u_plain": "user"}))

    plain_ticket = _ticket(free_name, "u_plain")
    out = ex.execute_tool_detailed(
        free_name, {"user_id": "default_user", "q": "ignored"},
        user_id="u_plain", granted_ticket=plain_ticket)
    assert out.ok is False
    assert "仅管理员可执行" in out.text
    # tool_ran=True 是诚实的：进的是桥接闭包，不是第三方 wrapper；判据是 0 次
    # supervisor.call_tool（子进程零调用），不能拿 tool_ran 冒充 spawn 计数。
    assert supervisor.calls == []

    default_ticket = _ticket(free_name, "default_user")
    out = ex.execute_tool_detailed(
        free_name, {"user_id": "u_admin", "q": "ignored"},
        user_id="default_user", granted_ticket=default_ticket)
    assert out.ok is True, out.text
    assert out.tool_ran is True
    assert len(supervisor.calls) == 1
    assert supervisor.calls[0]["arguments"] == {}
    assert "user_id" not in supervisor.calls[0]["arguments"]


# --------------------------------------------------------------------------- 2. 审批闸


def test_ticket_tool_and_owner_mismatch_never_reach_wrapper_and_do_not_burn_ticket():
    """错工具/错属主都 0 spawn；错属主消费不烧掉真票，真属主随后仍执行一次。"""
    sid = "srvticket"
    supervisor = BridgeSupervisor({sid: [
        _tool("alpha", schema=_schema(q={"type": "string"})),
        _tool("beta", schema=_schema(q={"type": "string"})),
    ]})
    bridge = StdioToolBridge(supervisor)
    bridge.attach()
    supervisor.fire_ready(sid)
    alpha = _mapped(sid, "alpha")
    beta = _mapped(sid, "beta")

    alpha_ticket = _ticket(alpha, "default_user")
    out = ex.execute_tool_detailed(
        beta, {"q": "cross-tool"}, user_id="default_user",
        granted_ticket=alpha_ticket)
    assert out.ok is False and out.tool_ran is False
    assert supervisor.calls == []

    request = approval.create_approval_request(alpha, {}, user_id="default_user")
    assert approval.consume_ticket(request["ticket_id"], "allow", "u_other") is None
    ticket = approval.consume_ticket(request["ticket_id"], "allow", "default_user")
    assert ticket is not None, "错属主的尝试不许把真票烧掉"

    out = ex.execute_tool_detailed(
        alpha, {"q": "cross-owner"}, user_id="u_other", granted_ticket=ticket)
    assert out.ok is False and out.tool_ran is False
    assert supervisor.calls == []

    out = ex.execute_tool_detailed(
        alpha, {"q": "right-owner"}, user_id="default_user",
        granted_ticket=ticket)
    assert out.ok is True, out.text
    assert out.tool_ran is True
    assert len(supervisor.calls) == 1
    assert supervisor.calls[0]["arguments"] == {"q": "right-owner"}


# --------------------------------------------------------------------------- 3. 审批 × 频控


def test_rejected_tickets_do_not_spend_frequency_but_21st_with_ticket_does():
    """无票据尝试不占额度；外部第 21 次即使带新票也 tool_ran=False。"""
    sid = "srvfreqcombo"
    supervisor = BridgeSupervisor(
        {sid: [_tool("freq", schema=_schema(q={"type": "string"}))]})
    bridge = StdioToolBridge(supervisor)
    bridge.attach()
    supervisor.fire_ready(sid)
    name = _mapped(sid, "freq")

    for _ in range(5):
        out = ex.execute_tool_detailed(name, {"q": "no-ticket"},
                                       user_id="default_user")
        assert out.ok is False and out.tool_ran is False
    assert supervisor.calls == []

    for index in range(20):
        ticket = _ticket(name, "default_user")
        out = ex.execute_tool_detailed(
            name, {"q": "run-%d" % index}, user_id="default_user",
            granted_ticket=ticket)
        assert out.ok is True and out.tool_ran is True, (index, out.text)
    assert len(supervisor.calls) == 20

    ticket = _ticket(name, "default_user")
    out = ex.execute_tool_detailed(name, {"q": "21st"}, user_id="default_user",
                                   granted_ticket=ticket)
    assert out.ok is False
    assert out.tool_ran is False, "被频控挡下的调用不许声称进过 wrapper"
    assert "调用过于频繁" in out.text
    assert len(supervisor.calls) == 20

    assert ex._EXTERNAL_FREQ == {"mcp-stdio:": (600.0, 20)}
    assert ex._FREQ_LIMITS["execute_code"] == (600.0, 10)
    assert ex._FREQ_LIMITS["web_read"] == (600.0, 4)


# --------------------------------------------------------------------------- 4. 名字/来源/额度


def test_builtin_collision_and_same_raw_name_across_servers_stay_isolated():
    """同一个原始名落在两个 server 的不同注册键上；内置撞名只拒那一个外部条目。"""
    a, b = "srvcol_a", "srvcol_b"
    builtin_name = _mapped(a, "shared")
    register_tool(builtin_name, "内置占位", _schema())(
        lambda: "builtin-still-here")

    supervisor = BridgeSupervisor({
        a: [_tool("shared", schema=_schema(q={"type": "string"}))],
        b: [_tool("shared", schema=_schema(q={"type": "string"}))],
    }, ready=set())
    bridge = StdioToolBridge(supervisor)
    bridge.attach()
    supervisor.fire_ready(a)
    supervisor.fire_ready(b)

    assert tools_registry[builtin_name]["source"] == "builtin"
    assert tools_registry[builtin_name]["function"]() == "builtin-still-here"
    assert _mapped(b, "shared") in tools_registry
    assert _mapped(b, "shared") != builtin_name
    assert supervisor.exposed[a] == 0
    assert supervisor.exposed[b] == 1

    report = bridge.rejection_report()[a]
    assert any(item["name"] == _mapped(a, "shared") and "占用" in item["reason"]
               for item in report)
    json.dumps(bridge.rejection_report(), ensure_ascii=False).encode("utf-8")


def test_per_server_and_global_caps_count_only_registered_tools():
    """每 server 16、全局 32：三个 server 各 20 条，只注册前 16 + 16 + 0。"""
    ids = ("srvcap_a", "srvcap_b", "srvcap_c")
    tools = {sid: [_tool("%s_t%02d" % (sid[-1], index)) for index in range(20)]
             for sid in ids}
    supervisor = BridgeSupervisor(tools, ready=set())
    bridge = StdioToolBridge(supervisor)
    bridge.attach()
    for sid in ids:
        supervisor.fire_ready(sid)

    assert MAX_TOOLS_PER_SERVER == 16
    assert MAX_TOOLS_TOTAL == 32
    assert [supervisor.exposed[sid] for sid in ids] == [16, 16, 0]
    external = [name for name, info in tools_registry.items()
                if str(info.get("source", "")).startswith(SOURCE_PREFIX)]
    assert len(external) == 32
    for sid in ids[:2]:
        for index in range(16):
            assert _mapped(sid, "%s_t%02d" % (sid[-1], index)) in tools_registry
    assert not any(str(info.get("source", "")) == SOURCE_PREFIX + ids[2]
                   for info in tools_registry.values())


# --------------------------------------------------------------------------- 5. 深嵌套 × 恶意名


def test_deep_schema_and_hostile_names_do_not_poison_siblings_or_report():
    """生产者真实复制失败 + 桥接拒绝账本：坏名只进账，兄弟 server 照常注册。"""
    a, b = "srvdeep_a", "srvdeep_b"
    hostile = ("tool\u202e\x00x", "tool\ud800", "N" * 5006)
    raw_a = [_tool("good_a")]
    raw_a.append(_tool("deep_tool", schema=_deep_schema()))
    # 这三个名字要的是“进拒绝账本且账本仍可序列化”，所以故意给缺 schema 的形状。
    raw_a.extend({"name": name} for name in hostile)
    raw_a.append(_tool("banned", schema=_schema(user_id={"type": "string"})))
    raw_a.append(_tool("no_schema", schema="not-a-schema"))

    copied = _safe_copy_tools(a, raw_a)
    supervisor = BridgeSupervisor({}, ready=set())
    bridge = StdioToolBridge(supervisor)
    bridge.on_ready(a, copied)
    bridge.on_ready(b, [_tool("good_b")])

    assert _mapped(a, "good_a") in tools_registry
    assert _mapped(b, "good_b") in tools_registry
    assert _mapped(a, "deep_tool") not in tools_registry
    assert _mapped(a, "banned") not in tools_registry

    report = bridge.rejection_report()
    assert set(report) == {a}
    for item in report[a]:
        _assert_report_name_safe(item["name"])
    assert any("嵌套过深" in item["reason"] for item in report[a])
    assert any(item["name"] == "N" * 127 + "…" for item in report[a])
    json.dumps(report, ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------------------- 6. env 纵深


def test_env_allowlist_rejected_at_write_and_fixed_values_win_in_real_child(
        client, config_sandbox, tmp_path, monkeypatch):
    """管理 PUT 拒绝 reserved/denied 名称；绕过写入口时固定注入值仍胜出且
    父进程 canary 不进真实子进程。"""
    supervisor = EndpointSupervisor()
    app.state.mcp_supervisor = supervisor
    try:
        for name in ("DEEPSEEK_API_KEY", "HTTP_PROXY", "PATH", "Path", "TEMP", "HOME"):
            body = _admin_entry("envsrv", env_allowlist=[name])
            res = client.put("/v1/admin/mcp/stdio/servers/envsrv", json=body)
            assert res.status_code == 400, (name, res.text)
            assert DECOY_ENV_VALUE not in res.text
            assert config_store.read()[MCP_SERVERS_KEY] == []
        assert supervisor.reloads == 0
    finally:
        app.state.mcp_supervisor = None

    monkeypatch.setenv("DEEPSEEK_API_KEY", PARENT_KEY)
    monkeypatch.setenv("HTTP_PROXY", PARENT_PROXY)
    monkeypatch.setenv("HOME", "C:/parent-home-canary-t7")
    monkeypatch.setenv("TEMP", "C:/parent-temp-canary-t7")
    monkeypatch.setenv("Path", "C:/parent-path-canary-t7")

    root = tmp_path / "envroot"
    command, _args = _invocation("envdump", tmp_path / "envrec")
    plan = build_env_plan(
        "envsrv",
        ["PATH", "Path", "TEMP", "HOME", "DEEPSEEK_API_KEY", "HTTP_PROXY"],
        str(root), command=command)
    assert "DEEPSEEK_API_KEY" not in plan
    assert "HTTP_PROXY" not in plan
    assert plan["HOME"] == str(root / "envsrv" / "home")
    assert plan["TEMP"] == str(root / "envsrv" / "tmp")
    assert "parent-path-canary" not in plan["PATH"]

    record = tmp_path / "envrec"
    entry = _config_entry("envdump", record, "envsrv")
    process = spawn_stdio(entry, str(root))
    try:
        env = _read_json(record / "env.json", timeout=8.0)
    finally:
        process.terminate_tree()
    assert env.get("DEEPSEEK_API_KEY") != PARENT_KEY
    assert "DEEPSEEK_API_KEY" not in env
    assert "HTTP_PROXY" not in env
    assert env["HOME"] == str(root / "envsrv" / "home")
    assert env["TEMP"] == str(root / "envsrv" / "tmp")
    assert "parent-path-canary" not in env.get("PATH", "")
    assert not any(PARENT_KEY in value for value in env.values())


# --------------------------------------------------------------------------- 7. 命令/cwd


def test_bad_commands_rejected_and_metacharacters_stay_argv_with_fixed_cwd(
        client, config_sandbox, tmp_path, monkeypatch):
    """管理写入口拒坏命令；supervisor 真跑时元字符原样、marker 不生成、cwd 固定。"""
    supervisor = EndpointSupervisor()
    app.state.mcp_supervisor = supervisor
    evil_cmd = tmp_path / "evil.cmd"
    evil_cmd.write_text("echo pwned\n", encoding="utf-8")
    try:
        for command in ("cmd.exe", ".\\evil.exe",
                        r"\\server\share\evil.exe", "//server/share/evil.exe",
                        str(evil_cmd)):
            body = _admin_entry("cmd", command=command,
                                args=["--token", DECOY_ARG])
            res = client.put("/v1/admin/mcp/stdio/servers/cmd", json=body)
            assert res.status_code == 400, (command, res.text)
            assert DECOY_ARG not in res.text
            assert config_store.read()[MCP_SERVERS_KEY] == []
        assert supervisor.reloads == 0
    finally:
        app.state.mcp_supervisor = None

    monkeypatch.setenv("DEEPSEEK_API_KEY", PARENT_KEY)
    marker = tmp_path / "shell-marker.txt"
    inject_one = '& echo pwned > "%s"' % marker
    inject_two = '; echo pwned >> "%s"' % marker
    record = tmp_path / "cmdrec"
    config = _Config(True, [
        _script_entry("record_argv", record, "cmdsrv",
                      extra_args=(inject_one, inject_two))])
    supervisor = _make_supervisor(config, tmp_path)
    bridge = StdioToolBridge(supervisor)
    bridge.attach()
    try:
        supervisor.start()
        _wait_state(supervisor, "cmdsrv", STATE_READY)
        argv = _read_json(record / "argv.json")
        cwd = _read_json(record / "cwd.json")
    finally:
        supervisor.shutdown()

    assert not marker.exists(), "元字符被 shell 执行了"
    tail = argv[argv.index("record_argv") + 2:]
    assert tail == [inject_one, inject_two]
    assert os.path.normcase(os.path.abspath(cwd)) == os.path.normcase(
        os.path.abspath(str(tmp_path / "mcp" / "cmdsrv")))


# --------------------------------------------------------------------------- 8. Job / tree


@WINDOWS_ONLY
@SPAWN_OK
def test_windows_job_flags_memory_limit_and_grandchild_tree_kill(tmp_path):
    """Job 五个 flag + 64MB 进程限额杀 hog + supervisor 退出杀孙进程。"""
    root = tmp_path / "jobroot"
    hog = _config_entry("hog", tmp_path / "hogrec", "hog")
    hog_process = spawn_stdio(
        hog, str(root),
        process_memory_bytes=64 * 1024 * 1024,
        job_memory_bytes=128 * 1024 * 1024,
        active_processes=2,
        cpu_time_100ns=10_000_000,
    )
    try:
        # hog 先回 initialize 才分配内存：不触发握手它只会等 stdin。
        hog_process.proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        }).encode("utf-8") + b"\n")
        hog_process.proc.stdin.flush()
        _wait_until(lambda: not is_pid_alive(hog_process.pid), 20.0,
                    message="64MB 进程限额没有终止 hog")
    finally:
        hog_process.terminate_tree()

    record = tmp_path / "treerec"
    config = _Config(True, [_config_entry("tree", record, "treesrv")])
    supervisor = _make_supervisor(config, tmp_path)
    bridge = StdioToolBridge(supervisor)
    bridge.attach()
    try:
        supervisor.start()
        _wait_state(supervisor, "treesrv", STATE_READY)
        tree = _read_json(record / "tree.json")
        parent, grandchild = tree["parent"], tree["grandchild"]
        assert is_pid_alive(parent) and is_pid_alive(grandchild)
        wrapper = supervisor._runtimes["treesrv"].wrapper
        assert wrapper.job_attached is True
        limits = query_job_limits(wrapper.job)
        expected = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                    | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                    | JOB_OBJECT_LIMIT_PROCESS_MEMORY
                    | JOB_OBJECT_LIMIT_JOB_MEMORY
                    | JOB_OBJECT_LIMIT_JOB_TIME)
        assert limits["limit_flags"] & expected == expected
        heartbeat = record / "heartbeat.txt"
        size_before = heartbeat.stat().st_size
    finally:
        supervisor.shutdown()

    _wait_until(lambda: not is_pid_alive(parent), 5.0,
                message="tree kill 后父进程仍活")
    _wait_until(lambda: not is_pid_alive(grandchild), 5.0,
                message="tree kill 后孙进程仍活")
    time.sleep(0.6)
    assert heartbeat.stat().st_size == size_before, "心跳仍在增长，孙进程未死"


# --------------------------------------------------------------------------- 9. 传输毒帧


@pytest.mark.parametrize("mode", ["batch", "hugeint", "bigline", "deepnest"])
def test_handshake_poison_in_one_server_cannot_cross_into_another(tmp_path, mode):
    """坏帧只杀自己那台；另一台照常 ready 并可调用，注册表不出现坏源。"""
    good, bad = "aaa_good", "zzz_bad"
    config = _Config(True, [
        _config_entry("ok", tmp_path / "goodrec", good),
        _config_entry(mode, tmp_path / "badrec", bad),
    ])
    supervisor = _make_supervisor(
        config, tmp_path,
        budget=MCPConnectionBudget(
            global_limit=4, per_server_limit=1, failure_threshold=2,
            failure_window=30.0, breaker_cooldown=1.0,
        ),
        backoff=(0.01, 0.02, 0.04),
    )
    bridge = StdioToolBridge(supervisor)
    bridge.attach()
    try:
        supervisor.start()
        _wait_state(supervisor, good, STATE_READY)
        # 协议类坏帧在 supervisor 里落 config_error（终态），传输类落 circuit_open；
        # 两者都是 fail-closed，关键是它不再 ready、另一台不受影响。
        _wait_state(supervisor, bad,
                    (STATE_CIRCUIT_OPEN, STATE_CONFIG_ERROR), timeout=15.0)

        name = _mapped(good, "echo")
        _wait_until(lambda: name in tools_registry, 5.0,
                    message="好 server ready 后工具没有进注册表")
        assert is_available(tools_registry[name]) is True
        result = tools_registry[name]["function"](
            user_id="default_user", text="跨服务器隔离")
        assert result.success is True, result.to_string()
        assert "跨服务器隔离" in result.to_string()
        assert not any(
            str(info.get("source", "")) == SOURCE_PREFIX + bad
            for info in tools_registry.values())
        assert any(
            str(info.get("source", "")) == SOURCE_PREFIX + good
            for info in tools_registry.values())
    finally:
        supervisor.shutdown()
    assert not any(
        str(info.get("source", "")).startswith(SOURCE_PREFIX)
        for info in tools_registry.values())


def _spawner_for(mode, record_dir):
    record_dir.mkdir(parents=True, exist_ok=True)
    command, args = _invocation(mode, record_dir)
    argv = [command, *args]
    if not _HELPER_EXE:
        argv = [sys.executable, "-u", *args]

    def _spawn():
        return subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=str(record_dir.parent))
    return _spawn


@contextlib.contextmanager
def _stdio_client(tmp_path, mode):
    record = tmp_path / ("rec-%s" % mode)
    proc_holder = {}
    raw_spawn = _spawner_for(mode, record)

    def _spawn():
        proc = raw_spawn()
        proc_holder["proc"] = proc
        return proc

    client = MCPStdioClient(spawner=_spawn, handshake_timeout=5.0,
                            call_timeout=5.0)
    try:
        assert client.start() is not None
        yield client
    finally:
        with contextlib.suppress(Exception):
            client.close()
        proc = proc_holder.get("proc")
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)


def test_wrong_id_and_nan_are_contained_within_their_own_client(tmp_path):
    """错 id 的诱饵不进结果；NaN 杀自己；并发的好 client 不受影响。"""
    with _stdio_client(tmp_path, "wrongid") as bad:
        with _stdio_client(tmp_path, "ok") as good:
            bad_init = bad.initialize()
            good_init = good.initialize()
            assert bad_init["protocolVersion"] == DEFAULT_PROTOCOL_VERSION
            assert good_init["protocolVersion"] == DEFAULT_PROTOCOL_VERSION
            assert bad.stats["stray_responses"] == 1
            good_text = good.call_tool("echo", {"text": "good-only"})
            assert good_text["content"][0]["text"] == "good-only"
            bad_sum = bad.call_tool("add", {"a": 1, "b": 2})
            assert bad_sum["content"][0]["text"] == "3"

    with _stdio_client(tmp_path, "nanresult") as nan_client:
        with _stdio_client(tmp_path, "ok") as good:
            nan_client.initialize()
            nan_client.list_tools()
            with pytest.raises(MCPError):
                nan_client.call_tool("echo", {"text": "nan"})
            assert nan_client.alive is False
            still = good.call_tool("echo", {"text": "after-nan"})
            assert still["content"][0]["text"] == "after-nan"


# --------------------------------------------------------------------------- 10. 端点/审计


def test_admin_state_none_then_runtime_decoys_never_reach_response_or_audit(
        client, config_sandbox):
    """state=None 503 且不造单例；装上 supervisor 后响应/审计都不回 args/env/stderr。"""
    before = stdio_mod._SUPERVISOR
    app.state.mcp_supervisor = None
    try:
        for method, path, kwargs in (
            ("get", "/v1/admin/mcp/stdio", {}),
            ("put", "/v1/admin/mcp/stdio/servers/files",
             {"json": _admin_entry("files")}),
            ("delete", "/v1/admin/mcp/stdio/servers/files", {}),
            ("put", "/v1/admin/mcp/stdio/settings", {"json": {"enabled": False}}),
        ):
            res = client.request(method, path, **kwargs)
            assert res.status_code == 503
            assert res.json()["detail"] == "MCP stdio 未初始化"
        assert stdio_mod._SUPERVISOR is before, "state=None 时偷偷造了单例"
        assert config_store.read()[MCP_SERVERS_KEY] == []
    finally:
        app.state.mcp_supervisor = None

    supervisor = EndpointSupervisor(status={
        "enabled": True,
        "servers": [{
            "id": "files", "enabled": True, "state": "ready", "pid": 1,
            "restarts": 0, "uptime_s": 0.1, "tools_total": 1,
            "tools_exposed": 1, "schema_flags": {"freeform": 0},
            "command": sys.executable, "command_basename": "python.exe",
            "argv_fingerprint": "f" * 64, "env_names": ["MCP_LOG_LEVEL"],
            "cwd": "C:/tmp/files", "last_error": "",
            "args": [DECOY_ARG], "env": {"MCP_LOG_LEVEL": DECOY_ENV_VALUE},
            "stderr": DECOY_STDERR,
        }],
    })
    app.state.mcp_supervisor = supervisor
    try:
        res = client.get("/v1/admin/mcp/stdio")
        assert res.status_code == 200
        for decoy in (DECOY_ARG, DECOY_ENV_VALUE, DECOY_STDERR):
            assert decoy not in res.text

        body = _admin_entry("files", cwd=DECOY_ARG)
        res = client.put("/v1/admin/mcp/stdio/servers/files", json=body)
        assert res.status_code == 400
        assert DECOY_ARG not in res.text
        assert config_store.read()[MCP_SERVERS_KEY] == []
        assert supervisor.reloads == 0

        body = _admin_entry("files", args=["--token", DECOY_ARG],
                            env_allowlist=["MCP_LOG_LEVEL"])
        res = client.put("/v1/admin/mcp/stdio/servers/files", json=body)
        assert res.status_code == 202, res.text
        assert DECOY_ARG not in res.text
        assert supervisor.reloads == 1
    finally:
        app.state.mcp_supervisor = None

    audit_text = Path(os.environ["AUDIT_LOG_PATH"]).read_text(encoding="utf-8")
    for decoy in (DECOY_ARG, DECOY_ENV_VALUE, DECOY_STDERR):
        assert decoy not in audit_text
    assert "argv_fingerprint" in audit_text


@pytest.mark.asyncio
async def test_admin_reload_off_loop_keeps_health_answer_during_block(
        config_sandbox):
    """slow reload 占满 0.8s 时 /health 仍能在 0.4s 内答出，且不回 args。"""
    supervisor = EndpointSupervisor(reload_delay=0.8)
    app.state.mcp_supervisor = supervisor
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as async_client:
            task = asyncio.create_task(async_client.put(
                "/v1/admin/mcp/stdio/servers/files",
                json=_admin_entry("files", args=["--token", DECOY_ARG])))
            await asyncio.sleep(0.05)
            started = time.monotonic()
            health = await async_client.get("/health")
            elapsed = time.monotonic() - started
            put = await task
    finally:
        app.state.mcp_supervisor = None

    assert put.status_code == 202
    assert health.status_code == 200
    assert elapsed < 0.4, "reload 占住事件循环，/health 被拖住 %.2fs" % elapsed
    assert DECOY_ARG not in health.text
    assert DECOY_ARG not in put.text


# --------------------------------------------------------------------------- 11. lifespan


@SPAWN_OK
def test_lifespan_keeps_serving_with_one_broken_server_and_leaves_no_child(
        config_sandbox, fresh_supervisor, tmp_path):
    """一台必失败 + 一台正常：服务仍 200；退出后子进程和注册表都清干净。"""
    live_record = tmp_path / "liferec"
    config_sandbox.write_text(json.dumps({
        MCP_ENABLED_KEY: True,
        MCP_SERVERS_KEY: [
            {"id": "broken", "command": _missing_command(), "args": [],
             "env_allowlist": [], "enabled": True},
            _config_entry("ok", live_record, "live"),
        ],
    }), encoding="utf-8")
    config_store.restore()

    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        supervisor = app.state.mcp_supervisor
        assert supervisor is not None
        deadline = time.monotonic() + 10.0
        states = {}
        live_pid = None
        while time.monotonic() < deadline:
            body = client.get("/v1/admin/mcp/stdio").json()
            states = {item["id"]: item["state"] for item in body["servers"]}
            live_pid = next((item.get("pid") for item in body["servers"]
                             if item["id"] == "live"), None)
            if states.get("broken") == STATE_CONFIG_ERROR and \
                    states.get("live") == STATE_READY and live_pid:
                break
            time.sleep(0.05)
        assert states.get("broken") == STATE_CONFIG_ERROR, states
        assert states.get("live") == STATE_READY, states
        assert live_pid and is_pid_alive(live_pid)

    assert app.state.mcp_supervisor is None
    assert not is_pid_alive(live_pid)
    assert supervisor.status()["servers"]
    assert all(item["state"] == STATE_DISABLED
               for item in supervisor.status()["servers"])
    assert not any(str(info.get("source", "")).startswith(SOURCE_PREFIX)
                   for info in tools_registry.values())


# --------------------------------------------------------------------------- 12. crashed/backoff


def test_crashed_keeps_registered_but_unavailable_and_disable_unregisters(
        client, tmp_path):
    """ready 后死亡：注册保留、available=False、管理面如实报 backoff/crashed；
    全局停用后才注销。"""
    record = tmp_path / "dierec"
    config = _Config(True, [_script_entry("ready_then_die", record, "diesrv")])
    supervisor = _make_supervisor(
        config, tmp_path, backoff=(5.0,), monitor_interval=0.01)
    bridge = StdioToolBridge(supervisor)
    bridge.attach()
    try:
        supervisor.start()
        name = _mapped("diesrv", "echo")
        _wait_until(lambda: name in tools_registry, 8.0,
                    message="ready 后工具没有注册")
        _wait_state(supervisor, "diesrv", (STATE_CRASHED, STATE_BACKOFF))
        info = tools_registry[name]
        assert info["source"] == SOURCE_PREFIX + "diesrv"
        assert is_available(info) is False, "进程已死却对模型仍可用"

        app.state.mcp_supervisor = supervisor
        try:
            body = client.get("/v1/admin/mcp/stdio").json()
        finally:
            app.state.mcp_supervisor = None
        row = next(item for item in body["servers"] if item["id"] == "diesrv")
        assert row["state"] in (STATE_CRASHED, STATE_BACKOFF)
        assert row["tools_total"] >= 1

        config.enabled = False
        supervisor.reload()
        assert name not in tools_registry
        assert supervisor.status()["servers"][0]["state"] == STATE_DISABLED
    finally:
        supervisor.shutdown()
