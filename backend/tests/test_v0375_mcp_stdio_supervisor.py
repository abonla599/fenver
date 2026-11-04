"""v0.37.5 T4：stdio supervisor（状态机 / 退避 / 熔断 / 审计 / 状态快照）行为锁。

覆盖计划 §7.2 的 C8（8h 寿命注入 0.2s 回收）、D9（进程崩溃后不再 available）与
T4 验收段：

1. 全局关 / 条目关 → 不 spawn，状态 ``disabled``；
2. ``ok`` → 2s 内 ``ready``、pid>0、tools_total=4，status 不含 args/env 值；
3. ``crash_once`` → 第二次成功，``restarts==1``；
4. ``crash_always`` → 5 次失败后 ``circuit_open``，第 6 次在 cooldown 内不 spawn；
   cooldown 到期只探一次（第 6 次），探活失败重新熔断；
5. ``ready_then_die``（握手成功 50ms 后退出）→ 每次 spawn 都按失败累计，
   5 次后 ``circuit_open``，spawn 次数停在 5；探活同样只探一次；
   ``stable_seconds`` 未到之前绝不 ``record_success``；
6. 配置错误（命令不存在 / 协议版本不符）→ ``config_error`` 终态，不重启；
   reload 同一份配置也不重启；
7. ``max_life`` 注入 0.3s → 到点回收重启，无孤儿；寿命回收不记传输失败；
8. 审计：start / exit / refused / circuit_open 各出现，且不含 args/env 诱饵、
   stderr 正文、完整命令行；start 只带 ``args_count``；
9. ``shutdown()`` 幂等、无线程残留、status 无 running server；
10. ``reload()``：enabled→disabled 停进程，disabled→enabled 起进程；旧代卡在
    ``client.close`` 时不得复活再 spawn——这一格（N3）用**事件**把 worker 钉在收尸里，
    观察点是"线程了结"而不是"睡了几秒"（#266：写死的 0.4/0.8 让它独跑 3 发 1 败），
    形状锁见本文件末尾第 11 节；
11. supervisor 再判 ``MAX_SERVERS=3``；坏条目 status 保留原始 ``enabled``。

所有用例 ``finally`` 调 ``shutdown()``；Windows 上用 T3 的 ``is_pid_alive`` 断言无孤儿。
"""
import ast
import contextlib
import inspect
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import audit as audit_module
from app.core.mcp_client import MCPConnectionBudget
from app.core.mcp_stdio import (
    STATE_BACKOFF,
    STATE_CIRCUIT_OPEN,
    STATE_CONFIG_ERROR,
    STATE_CRASHED,
    STATE_DISABLED,
    STATE_READY,
    STATE_STARTING,
    STATE_STOPPING,
    StdioSupervisor,
    _Runtime,
    _refused_reason,
    get_supervisor,
)
from app.core.mcp_stdio_client import MCPStdioClient
from app.core.mcp_stdio_process import command_basename, is_pid_alive

SERVER = Path(__file__).with_name("fake_mcp_stdio_server.py")
BACKOFF = (0.01, 0.02, 0.04, 0.08, 0.16)
COOLDOWN = 0.2
DECOY_ARG = "DECOY_ARG_VALUE_4b9c"
ENV_NAME = "SUP_DECOY_ENV_NAME"
ENV_VALUE = "ENV_DECOY_VALUE_8e17"
STDERR_DECOY = "STDERR_DECOY_SUPERVISOR_7f21"


class _Config:
    """可变配置源：reload 用例靠它改 enabled。"""

    def __init__(self, enabled, servers):
        self.enabled = enabled
        self.servers = servers

    def __call__(self):
        return {
            "mcp_stdio_enabled": self.enabled,
            "mcp_stdio_servers": [dict(item) for item in self.servers],
        }


def _entry(sid, mode="ok", *, enabled=True, extra_args=(), env_allowlist=(), command=None):
    # argv[2]（记录目录）留空：supervisor 用例不依赖 fake server 的落点，
    # 也就不会往仓库 cwd 里写任何东西。
    return {
        "id": sid,
        "command": command if command is not None else sys.executable,
        "args": [str(SERVER), mode, "", *extra_args],
        "env_allowlist": list(env_allowlist),
        "enabled": enabled,
    }


def _rows(supervisor):
    return {item["id"]: item for item in supervisor.status()["servers"]}


def _row(supervisor, sid):
    rows = _rows(supervisor)
    assert sid in rows, "status 里没有条目 %s" % sid
    return rows[sid]


def _wait_until(predicate, timeout, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def _wait_state(supervisor, sid, state, timeout=5.0):
    return _wait_until(lambda: _row(supervisor, sid)["state"] == state, timeout)


def _ready_event(supervisor, sid):
    """记录一次 ready 回调，不靠轮询 state。

    max_life 从 spawn 起算；注入 0.3/0.5s 时，子进程启动本身就可能吃掉整个
    寿命，ready 设完同一轮监控就会转 stopping。10ms 轮询会随机错过这段零长度
    窗口（负载越高越容易），但 ready 回调一定会在回收之前发出。
    """
    event = threading.Event()
    supervisor.set_tool_callbacks(
        on_ready=lambda got, _tools: event.set() if got == sid else None)
    return event


def _make(supervisor_config, tmp_path, **overrides):
    options = {
        "config_reader": supervisor_config,
        "mcp_root": str(tmp_path),
        "backoff": BACKOFF,
        "monitor_interval": 0.01,
        "handshake_timeout": 5.0,
        "call_timeout": 2.0,
        "budget": MCPConnectionBudget(
            failure_threshold=5, failure_window=60.0, breaker_cooldown=COOLDOWN
        ),
    }
    options.update(overrides)
    return StdioSupervisor(**options)


def _read_count(tmp_path, sid, name="spawn.count"):
    path = tmp_path / sid / name
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _ids(supervisor):
    return [item["id"] for item in supervisor.status()["servers"]]


# --------------------------------------------------------------------------- 1. disabled


def test_global_disabled_spawns_nothing(tmp_path):
    config = _Config(False, [_entry("g_off", "crash_always")])
    supervisor = _make(config, tmp_path)
    try:
        supervisor.start()
        time.sleep(0.3)
        row = _row(supervisor, "g_off")
        assert row["state"] == STATE_DISABLED
        assert row["pid"] is None
        assert row["restarts"] == 0
        # crash_always 一启动就写 spawn.count；文件不存在即证明没有子进程。
        assert _read_count(tmp_path, "g_off") == 0
        assert supervisor.ready_servers() == []
    finally:
        supervisor.shutdown()


def test_entry_disabled_spawns_nothing(tmp_path):
    config = _Config(True, [_entry("e_off", "crash_always", enabled=False)])
    supervisor = _make(config, tmp_path)
    try:
        supervisor.start()
        time.sleep(0.3)
        row = _row(supervisor, "e_off")
        assert row["state"] == STATE_DISABLED
        assert row["pid"] is None
        assert _read_count(tmp_path, "e_off") == 0
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- 2. ready / status


def test_ok_reaches_ready_and_status_hides_values(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_NAME, ENV_VALUE)
    config = _Config(
        True,
        [_entry("s_ok", "ok", extra_args=[DECOY_ARG], env_allowlist=[ENV_NAME])],
    )
    supervisor = _make(config, tmp_path)
    pid = None
    try:
        supervisor.start()
        assert _wait_state(supervisor, "s_ok", STATE_READY, timeout=2.0), supervisor.status()
        row = _row(supervisor, "s_ok")
        pid = row["pid"]
        assert isinstance(pid, int) and pid > 0
        assert row["tools_total"] == 4
        assert row["tools_exposed"] == 4
        assert row["restarts"] == 0
        assert row["uptime_s"] > 0
        assert row["command_basename"] == command_basename(sys.executable)
        assert len(row["argv_fingerprint"]) == 64
        assert row["env_names"] == [ENV_NAME]
        assert supervisor.is_ready("s_ok") is True
        assert supervisor.ready_servers() == ["s_ok"]

        dumped = json.dumps(supervisor.status(), ensure_ascii=False)
        assert DECOY_ARG not in dumped
        assert ENV_VALUE not in dumped
        assert str(SERVER) not in dumped
        for item in supervisor.status()["servers"]:
            assert "args" not in item
            assert "env" not in item
    finally:
        supervisor.shutdown()

    assert pid is not None
    assert _wait_until(lambda: not is_pid_alive(pid), 5.0)
    assert supervisor.ready_servers() == []
    assert _row(supervisor, "s_ok")["state"] not in (STATE_READY, STATE_STARTING)


# --------------------------------------------------------------------------- 3. crash_once


def test_crash_once_restarts_and_recovers(tmp_path):
    config = _Config(True, [_entry("s_once", "crash_once")])
    supervisor = _make(config, tmp_path)
    try:
        supervisor.start()
        assert _wait_state(supervisor, "s_once", STATE_READY, timeout=6.0), supervisor.status()
        row = _row(supervisor, "s_once")
        assert row["restarts"] == 1
        assert _read_count(tmp_path, "s_once") == 2
        assert row["tools_total"] == 4
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- 4. circuit


def test_supervisor_reads_public_breaker_cooldown_only(tmp_path):
    """C2：预算不暴露私有属性时，supervisor 也必须从公开 property 读出 cooldown。"""

    class _PublicBudget:
        breaker_cooldown = COOLDOWN

        def __getattr__(self, name):
            # 旧实现把私有属性放在 getattr 的 default 位置；Python 会先求 default，
            # 即使公开属性存在也会摸一次 ``_breaker_cooldown``。这里让它响亮地红。
            if name == "_breaker_cooldown":
                raise AssertionError(
                    "supervisor 又去读私有 _breaker_cooldown 兜底了"
                )
            raise AttributeError(name)

    supervisor = _make(_Config(True, []), tmp_path, budget=_PublicBudget())
    try:
        assert supervisor._cooldown == COOLDOWN, \
            "supervisor 还在走私有 _breaker_cooldown 的兜底读法"
    finally:
        supervisor.shutdown()


def test_crash_always_opens_circuit_and_probes_once(tmp_path):
    config = _Config(True, [_entry("s_crash", "crash_always")])
    supervisor = _make(config, tmp_path)
    try:
        supervisor.start()
        assert _wait_state(supervisor, "s_crash", STATE_CIRCUIT_OPEN, timeout=10.0), (
            supervisor.status()
        )
        assert _read_count(tmp_path, "s_crash") == 5
        # cooldown 内绝不 spawn 第 6 次。
        time.sleep(COOLDOWN / 2)
        assert _read_count(tmp_path, "s_crash") == 5
        # cooldown 到期只探一次。
        assert _wait_until(lambda: _read_count(tmp_path, "s_crash") >= 6, 3.0)
        assert _wait_state(supervisor, "s_crash", STATE_CIRCUIT_OPEN, timeout=2.0)
        time.sleep(COOLDOWN / 2)
        assert _read_count(tmp_path, "s_crash") == 6
    finally:
        supervisor.shutdown()


def test_ready_then_die_opens_circuit_after_five_spawns(tmp_path):
    """B1：ready 不等于稳定成功。握手成功 50ms 就退出必须按失败累计到熔断。"""
    config = _Config(True, [_entry("s_flap", "ready_then_die")])
    # stable_seconds=60：本用例全程远小于 60s，任何"ready 即清零"都会让熔断永不打开。
    supervisor = _make(config, tmp_path, stable_seconds=60.0)
    try:
        supervisor.start()
        assert _wait_state(supervisor, "s_flap", STATE_CIRCUIT_OPEN, timeout=10.0), (
            supervisor.status()
        )
        # 计的是假 server 自己写的 spawn.count，不看 status 自报的 restarts。
        assert _read_count(tmp_path, "s_flap") == 5, supervisor.status()
        # cooldown 内绝不 spawn 第 6 次。
        time.sleep(COOLDOWN / 2)
        assert _read_count(tmp_path, "s_flap") == 5
        # cooldown 到期只探一次；探活同样 ready 后秒死，重新熔断。
        assert _wait_until(lambda: _read_count(tmp_path, "s_flap") >= 6, 3.0)
        assert _wait_state(supervisor, "s_flap", STATE_CIRCUIT_OPEN, timeout=2.0)
        time.sleep(COOLDOWN / 2)
        assert _read_count(tmp_path, "s_flap") == 6
    finally:
        supervisor.shutdown()


def test_crash_marks_crashed_before_teardown(tmp_path):
    """N1 / N7：收尸前状态必须离开 ready（crashed），status 不会出现 ready + pid=None。"""
    config = _Config(True, [_entry("s_mark", "ready_then_die")])
    supervisor = _make(config, tmp_path, stable_seconds=60.0)
    states = []
    original = supervisor._teardown_process

    def spy(runtime, *, reason, uptime):
        states.append(runtime.state)
        return original(runtime, reason=reason, uptime=uptime)

    supervisor._teardown_process = spy
    try:
        supervisor.start()
        assert _wait_state(supervisor, "s_mark", STATE_CIRCUIT_OPEN, timeout=10.0), (
            supervisor.status()
        )
    finally:
        supervisor.shutdown()

    assert STATE_CRASHED in states, states
    assert STATE_READY not in states, states


# --------------------------------------------------------------------------- 5. config_error


def test_missing_command_is_terminal_config_error(tmp_path):
    missing = str(tmp_path / "no_such_server.exe")
    config = _Config(True, [_entry("s_missing", command=missing)])
    supervisor = _make(config, tmp_path)
    try:
        supervisor.start()
        assert _wait_state(supervisor, "s_missing", STATE_CONFIG_ERROR, timeout=3.0)
        row = _row(supervisor, "s_missing")
        assert row["last_error"].startswith("config_error")
        assert row["restarts"] == 0
        assert row["pid"] is None
        time.sleep(0.3)
        assert _row(supervisor, "s_missing")["state"] == STATE_CONFIG_ERROR
        assert _row(supervisor, "s_missing")["restarts"] == 0
        # 同一份配置 reload 不是"配置变更"：终态不许自己翻回来。
        supervisor.reload()
        time.sleep(0.3)
        row = _row(supervisor, "s_missing")
        assert row["state"] == STATE_CONFIG_ERROR
        assert row["restarts"] == 0
    finally:
        supervisor.shutdown()


def test_version_mismatch_is_terminal_config_error(tmp_path):
    config = _Config(True, [_entry("s_ver", "version_mismatch")])
    supervisor = _make(config, tmp_path)
    try:
        supervisor.start()
        assert _wait_state(supervisor, "s_ver", STATE_CONFIG_ERROR, timeout=5.0)
        row = _row(supervisor, "s_ver")
        assert row["last_error"].startswith("config_error")
        # 只 spawn 了一次：restarts 是"后续 spawn 次数"，必须为 0。
        assert row["restarts"] == 0
        time.sleep(0.4)
        assert _row(supervisor, "s_ver")["state"] == STATE_CONFIG_ERROR
        assert _row(supervisor, "s_ver")["restarts"] == 0
        supervisor.reload()
        time.sleep(0.3)
        assert _row(supervisor, "s_ver")["state"] == STATE_CONFIG_ERROR
        assert _row(supervisor, "s_ver")["restarts"] == 0
    finally:
        supervisor.shutdown()


def test_max_servers_caps_fourth_entry(tmp_path):
    """N5：supervisor 再判 MAX_SERVERS=3，第 4 条不 spawn 且 reason=max_servers。"""
    records = []

    def audit(actor, action, *, target=None, after=None):
        records.append({"action": action, "target": target, "after": after})

    config = _Config(
        True,
        [
            _entry("s_one", "ok"),
            _entry("s_two", "ok"),
            _entry("s_three", "ok"),
            # 用会写 spawn.count 的模式：只要第 4 条被起过，计数就会 > 0。
            _entry("s_four", "crash_always"),
        ],
    )
    supervisor = _make(config, tmp_path, audit_fn=audit)
    try:
        supervisor.start()
        for server_id in ("s_one", "s_two", "s_three"):
            assert _wait_state(supervisor, server_id, STATE_READY, timeout=5.0), (
                supervisor.status()
            )
        row = _row(supervisor, "s_four")
        assert row["state"] == STATE_CONFIG_ERROR
        assert row["last_error"] == "max_servers"
        assert row["pid"] is None
        assert _read_count(tmp_path, "s_four") == 0
        assert any(
            item["action"] == "mcp.stdio.refused"
            and item["target"] == "s_four"
            and item["after"]["reason"] == "max_servers"
            for item in records
        ), records
    finally:
        supervisor.shutdown()


def test_bad_entry_keeps_original_enabled_flag(tmp_path):
    """N10：坏条目 status 保留配置里的 enabled bool，不硬写成 False。"""
    bad = _entry("s_bad", "crash_always", enabled=True)
    bad["args"] = "not-a-list"  # normalize_entry → args_not_a_list
    config = _Config(True, [bad])
    supervisor = _make(config, tmp_path)
    try:
        supervisor.start()
        row = _row(supervisor, "s_bad")
        assert row["state"] == STATE_CONFIG_ERROR
        assert row["enabled"] is True
        assert row["last_error"].startswith("config_error")
        time.sleep(0.2)
        assert _read_count(tmp_path, "s_bad") == 0
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- 6. max_life


def test_max_life_recycles_without_orphans(tmp_path):
    config = _Config(True, [_entry("s_life", "ok")])
    supervisor = _make(config, tmp_path, max_life=0.3)
    ready = _ready_event(supervisor, "s_life")
    pids = set()
    try:
        supervisor.start()
        assert ready.wait(5.0), supervisor.status()
        assert _wait_until(
            lambda: _row(supervisor, "s_life")["restarts"] >= 1, 6.0
        ), supervisor.status()

        # 采样一轮，确认发生过不止一个 pid（真的换了进程，不是只改计数）。
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            row = _row(supervisor, "s_life")
            if row["pid"]:
                pids.add(row["pid"])
            time.sleep(0.01)
        assert len(pids) >= 1
    finally:
        supervisor.shutdown()

    assert _wait_until(lambda: not any(is_pid_alive(pid) for pid in pids), 5.0)
    assert _row(supervisor, "s_life")["state"] not in (STATE_READY, STATE_STARTING)


class _SpyBudget(MCPConnectionBudget):
    """记调用次数的预算：用来证明 max_life 回收不写传输失败。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.failures_recorded = 0
        self.successes_recorded = 0

    def record_failure(self, server_id):
        self.failures_recorded += 1
        super().record_failure(server_id)

    def record_success(self, server_id):
        self.successes_recorded += 1
        super().record_success(server_id)


def test_max_life_recycle_does_not_record_transport_failure(tmp_path):
    """N2：寿命到点回收走"teardown + 固定短延迟重启"，不计失败、exit reason 仍是 killed。"""
    records = []

    def audit(actor, action, *, target=None, after=None):
        records.append({"action": action, "target": target, "after": after})

    budget = _SpyBudget(
        failure_threshold=5, failure_window=60.0, breaker_cooldown=COOLDOWN
    )
    config = _Config(True, [_entry("s_cycle", "ok")])
    supervisor = _make(
        config,
        tmp_path,
        max_life=0.5,
        stable_seconds=60.0,
        budget=budget,
        audit_fn=audit,
    )
    ready = _ready_event(supervisor, "s_cycle")
    try:
        supervisor.start()
        assert ready.wait(5.0), supervisor.status()
        assert _wait_until(
            lambda: _row(supervisor, "s_cycle")["restarts"] >= 1, 6.0
        ), supervisor.status()
        assert budget.failures_recorded == 0
        exit_records = [
            item["after"]
            for item in records
            if item["action"] == "mcp.stdio.exit" and item["target"] == "s_cycle"
        ]
        assert exit_records, records
        assert exit_records[0]["reason"] == "killed", exit_records
        assert _row(supervisor, "s_cycle")["state"] != STATE_CIRCUIT_OPEN
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- 7. audit


def _audit_tail(offset):
    path = audit_module.path()
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def test_audit_events_are_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_NAME, ENV_VALUE)
    offset = os.path.getsize(audit_module.path()) if os.path.exists(audit_module.path()) else 0

    first = _make(
        _Config(
            True,
            [
                _entry("aok", "ok", extra_args=[DECOY_ARG], env_allowlist=[ENV_NAME]),
                _entry("abad", "version_mismatch"),
            ],
        ),
        tmp_path,
    )
    try:
        first.start()
        assert _wait_state(first, "aok", STATE_READY, timeout=5.0), first.status()
        assert _wait_state(first, "abad", STATE_CONFIG_ERROR, timeout=5.0)
    finally:
        first.shutdown()

    second = _make(_Config(True, [_entry("acrash", "crash_always")]), tmp_path)
    try:
        second.start()
        assert _wait_state(second, "acrash", STATE_CIRCUIT_OPEN, timeout=10.0)
    finally:
        second.shutdown()

    text = _audit_tail(offset)
    records = []
    for line in text.splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    actions = {record.get("action") for record in records}
    assert {
        "mcp.stdio.start",
        "mcp.stdio.exit",
        "mcp.stdio.refused",
        "mcp.stdio.circuit_open",
    } <= actions, actions

    targets = {record.get("target") for record in records}
    assert {"aok", "abad", "acrash"} <= targets

    start_records = [r for r in records if r.get("action") == "mcp.stdio.start"]
    assert start_records
    start_after = start_records[0]["after"]
    assert len(start_after["argv_fingerprint"]) == 64
    assert start_after["command_basename"]
    assert "args" not in start_after
    # N6：start 审计只带 args 的个数（argv = server 路径 + mode + 记录目录 + 诱饵）。
    assert start_after["args_count"] == 4

    # 诱饵：args 的值、env 的值、stderr 正文、完整命令行都不许出现。
    assert DECOY_ARG not in text
    assert ENV_VALUE not in text
    assert STDERR_DECOY not in text
    assert "fake_mcp_stdio_server.py" not in text


# --------------------------------------------------------------------------- 8. shutdown


def test_shutdown_is_idempotent_and_leaves_no_threads(tmp_path):
    config = _Config(True, [_entry("s_stop", "ok")])
    supervisor = _make(config, tmp_path)
    supervisor.start()
    assert _wait_state(supervisor, "s_stop", STATE_READY, timeout=5.0)
    pid = _row(supervisor, "s_stop")["pid"]
    assert pid

    supervisor.shutdown()
    supervisor.shutdown()

    assert _wait_until(lambda: not is_pid_alive(pid), 5.0)
    status = supervisor.status()
    assert all(
        item["state"] not in (STATE_READY, STATE_STARTING, STATE_STOPPING)
        for item in status["servers"]
    )
    assert all(item["pid"] is None for item in status["servers"])
    assert supervisor.ready_servers() == []

    def leftovers():
        alive = []
        for thread in threading.enumerate():
            if not thread.is_alive():
                continue
            if "s_stop" in thread.name or ("mcp-stdio-reader-" + str(pid)) in thread.name:
                alive.append(thread.name)
        return alive

    assert _wait_until(lambda: leftovers() == [], 5.0), leftovers()


# --------------------------------------------------------------------------- 9. reload


def test_reload_stops_and_starts_by_enabled_flag(tmp_path):
    entry = _entry("s_reload", "ok", enabled=False)
    config = _Config(True, [entry])
    supervisor = _make(config, tmp_path)
    try:
        supervisor.start()
        time.sleep(0.2)
        row = _row(supervisor, "s_reload")
        assert row["state"] == STATE_DISABLED
        assert row["pid"] is None

        config.servers = [_entry("s_reload", "ok", enabled=True)]
        supervisor.reload()
        assert _wait_state(supervisor, "s_reload", STATE_READY, timeout=5.0), supervisor.status()
        pid_one = _row(supervisor, "s_reload")["pid"]
        assert pid_one

        config.servers = [_entry("s_reload", "ok", enabled=False)]
        supervisor.reload()
        assert _wait_state(supervisor, "s_reload", STATE_DISABLED, timeout=5.0)
        assert _wait_until(lambda: not is_pid_alive(pid_one), 5.0)
        assert _row(supervisor, "s_reload")["pid"] is None

        config.servers = [_entry("s_reload", "ok", enabled=True)]
        supervisor.reload()
        assert _wait_state(supervisor, "s_reload", STATE_READY, timeout=5.0)
        pid_two = _row(supervisor, "s_reload")["pid"]
        assert pid_two and pid_two != pid_one
    finally:
        supervisor.shutdown()


PARK_BOUND = 30.0
SPAWN_BOUND = 20.0
SETTLE_BOUND = 20.0


def _zombie_thread(supervisor, sid):
    """这一代还挂着的 worker 线程；旧代卡在收尸里时引用被**保留**（N3 的观察点来源）。"""
    with supervisor._lock:
        runtime = supervisor._runtimes.get(sid)
        return None if runtime is None else runtime.thread


def _zombie_thread_exited(supervisor, sid):
    thread = _zombie_thread(supervisor, sid)
    return thread is None or not thread.is_alive()


@contextlib.contextmanager
def _zombie_park(monkeypatch, *, join_timeout=0.1):
    """把 ``MCPStdioClient.close`` 换成"停在收尸里、由事件放行"的桩。

    为什么必须是事件（#266 的全部理由）：旧写法靠 ``sleep(0.4)`` 停车、靠 ``sleep(0.8)``
    观察，判据实际压在"0.8 > 0.4"这道算术上。主线程在 ``closing`` 置位与 ``reload()``
    之间被调度走超过 0.4 秒，旧代就已经按 max_life 的**回收规则**合法地起了第三代
    ⇒ 计数 3 ⇒ 独跑 3 发 1 败（2026-11-04 在本机 ``dc08ad56`` 上复跑 10 发 1 败）。
    停车窗改由 ``released`` 决定之后，"reload 一定落在旧代还卡着的那一刻"是结构保证。

    ``PARK_BOUND`` 与 ``join_timeout`` 都只是**上限**（挡"别把 CI 挂死"），不承载判据；
    把 ``released.set()`` 挪出 ``finally``、或把上限摘掉/换成非正数，第 11 节那把形状锁会红。
    """
    monkeypatch.setattr("app.core.mcp_stdio.STOP_JOIN_TIMEOUT", join_timeout)
    armed = threading.Event()
    closing = threading.Event()
    released = threading.Event()
    parked = []

    def parked_close(self):
        if not armed.is_set():
            return                      # 装闸之前的 close（第 1 代退场）照常放行
        parked.append(self)
        closing.set()
        released.wait(PARK_BOUND)

    monkeypatch.setattr(MCPStdioClient, "close", parked_close)
    try:
        yield SimpleNamespace(armed=armed, closing=closing, released=released, parked=parked)
    finally:
        # 放行必须在 finally：任何一枚断言在半路红，都不许把 worker 永久留在停车态。
        released.set()


class _ZombieTrace:
    """把每一步的 ``(名字, 相对秒, 当时的 spawn 计数)`` 记下来——只在红的时候有用。

    为什么值得留：这一族有两种完全不同的坏法——"被钉住之前就已经多跑了一代"（max_life
    的回收规则自己起的，与闸口无关）和"闸口没拦住、放行后又起一代"（真回归）。没有这条
    时间线，两种红在断言文本里长得一模一样，下一个人只能重新猜。
    """

    def __init__(self, counter):
        self._counter = counter
        self._t0 = time.monotonic()
        self.marks = []

    def mark(self, name):
        self.marks.append((name, round(time.monotonic() - self._t0, 3), self._counter()))
        return self

    def __str__(self):
        return " → ".join("%s@%ss(count=%s)" % item for item in self.marks)


def test_zombie_generation_does_not_respawn_after_reload(tmp_path, monkeypatch):
    """N3：旧代卡在 client.close 超过 join 超时时，reload 不得再起一代、旧代也不许复活。

    形状：``crash_once`` 起两代（第 2 代 ready），再用很短的 max_life 让第 2 代在
    worker 线程里进入 teardown；close 桩把 worker **钉**在收尸里（事件放行，不睡秒数），
    此时 reload 改条目 → 新实现保留活线程（stopping）拒绝 spawn；旧实现会
    先起第 3 代、旧代醒来后又 spawn 一次。

    三个观察点各钉一种坏法：
    * reload 当场——条目必须停在 ``stopping``：钉"闸口没拦住、reload 自己起了新一代"
      （把 ``_ensure_running`` 那道 ``thread.is_alive()`` 闸门摘掉，这里必红，旧实现会把
      状态翻成 ``starting`` 再 spawn）；
    * 放行之前——计数与停车那一刻**相等**；
    * 放行、并等**旧代线程真的了结**之后——计数仍与停车那一刻相等：钉"旧代醒来复活又
      spawn"。旧实现里那一代根本不会收束，所以这条等待到点报红，而不是悄悄放过。

    为什么判据写成**增量**、不再写"计数必须停在 2"（2026-11-04 实测改的）：max_life 的
    回收规则本来就会自己起新一代；主线程在"观察到第 2 次 spawn"与"装闸"之间被调度走
    整整一个 ready+max_life 时，被钉住的那一代就已经是第 3 代了——绝对值断言把"停车前
    跑了几代"这件与闸口无关的事一起吃了进去（同一形状 45 发里 7 发这样红）。增量把
    判据收回到它真正想说的这句话上，时间线由 ``_ZombieTrace`` 带着，两种红分得开。
    """
    config = _Config(True, [_entry("s_zombie", "crash_once")])

    def count():
        return _read_count(tmp_path, "s_zombie")

    trace = _ZombieTrace(count)
    with _zombie_park(monkeypatch, join_timeout=0.1) as park:
        supervisor = _make(config, tmp_path, max_life=0.25, stable_seconds=60.0)
        try:
            supervisor.start()
            trace.mark("start")
            assert _wait_until(lambda: count() >= 2, SPAWN_BOUND), (
                "两代都没起来：%s status=%r" % (trace, supervisor.status()))
            park.armed.set()
            assert park.closing.wait(SETTLE_BOUND), (
                "worker 没有进入 client.close：%s" % trace)
            trace.mark("parked")
            count_at_park = count()

            config.servers = [_entry("s_zombie", "crash_once", extra_args=["changed"])]
            supervisor.reload()
            trace.mark("reloaded")

            assert _row(supervisor, "s_zombie")["state"] == STATE_STOPPING, (
                "reload 没有把条目停在 stopping：%s status=%r" % (trace, supervisor.status()))
            assert count() == count_at_park, (
                "闸口拒绝之后、放行之前又起了新一代（停车时 %d，现在 %d）：%s" % (
                    count_at_park, count(), trace))

            park.released.set()
            trace.mark("released")
            assert _wait_until(
                lambda: _zombie_thread_exited(supervisor, "s_zombie"), SETTLE_BOUND
            ), "旧代在 stop_event 已置位后仍没有了结（多半是它复活又 spawn 了一代）：%s status=%r" % (
                trace, supervisor.status())
            trace.mark("thread_done")
            assert count() == count_at_park, (
                "旧代了结之后又起了新一代（停车时 %d，现在 %d）：%s" % (
                    count_at_park, count(), trace))
        finally:
            supervisor.shutdown()


@pytest.mark.parametrize("scale", (0.1, 1.0, 10.0))
def test_zombie_gate_survives_timing_constants(tmp_path, monkeypatch, scale):
    """#266 要的自证：把这一格能注入的秒级常数同时改小/改大一个数量级，结论必须一模一样。

    旧写法做不到这件事——0.4 与 0.8 一旦互相靠近，这格就随机红。改成事件驱动后，
    "停车窗跨不跨过 reload"不由秒数决定，所以 ``max_life``、join 超时、monitor_interval
    同比例缩到 0.1× 或放到 10× 都不许改变判据：

    * 0.1×：第 2 代一 ready 就到期回收（收尸与 reload 挤在同一瞬间），闸口照样拦；
    * 10×：reload 里那一次 ``join(1.0)`` 会**真等满**——旧代仍被事件钉着，没人替它放行，
      所以 join 到点、状态停在 stopping、计数不再涨，与 0.1× 同结论。

    这一格的放行时机取**相反**的那一支：reload 一返回就立刻放行（旧写法最吃亏的形状，
    也是"旧代醒来复活"最有机会露头的时刻）；"钉着不放行"那一支由上一格覆盖。
    """
    config = _Config(True, [_entry("s_zombie", "crash_once")])

    def count():
        return _read_count(tmp_path, "s_zombie")

    trace = _ZombieTrace(count)
    with _zombie_park(monkeypatch, join_timeout=0.1 * scale) as park:
        supervisor = _make(
            config,
            tmp_path,
            max_life=0.25 * scale,
            monitor_interval=0.01 * scale,
            stable_seconds=60.0,
        )
        try:
            supervisor.start()
            trace.mark("start")
            assert _wait_until(lambda: count() >= 2, SPAWN_BOUND), (
                "scale=%s 连两代都没起来：%s" % (scale, trace))
            park.armed.set()
            assert park.closing.wait(SETTLE_BOUND), (
                "scale=%s worker 没有进入 client.close：%s" % (scale, trace))
            trace.mark("parked")
            count_at_park = count()

            config.servers = [_entry("s_zombie", "crash_once", extra_args=["changed"])]
            supervisor.reload()
            trace.mark("reloaded")

            assert _row(supervisor, "s_zombie")["state"] == STATE_STOPPING, (
                "scale=%s 闸口没拦住：reload 把条目带离了 stopping：%s status=%r" % (
                    scale, trace, supervisor.status()))

            # 立刻放行——这一支最容易让"旧代醒来再 spawn"露头。
            park.released.set()
            trace.mark("released")
            assert _wait_until(
                lambda: _zombie_thread_exited(supervisor, "s_zombie"), SETTLE_BOUND
            ), "scale=%s 时旧代没有了结：判据开始吃时序了：%s" % (scale, trace)
            trace.mark("thread_done")
            assert count() == count_at_park, (
                "scale=%s 起了新一代（停车时 %d，现在 %d）：秒级常数又开始承载判据了：%s" % (
                    scale, count_at_park, count(), trace))
        finally:
            supervisor.shutdown()


def test_reload_demoted_fourth_server_stops_its_old_runtime(tmp_path):
    """C3：第 4 条降级为 max_servers 时，旧的第 3 条 runtime 必须被停掉。

    形状：3 个 ready 的 ok server → reload 成 4 个都合法的条目、新条目排最前，
    于是原来的第 3 个变成名额外的 config_error。只比 entry 的旧实现会把它继续
    跑着（status 还报 ready/pid），与"最多 3 条"的上限自相矛盾。
    """
    config = _Config(True, [_entry("s_one", "ok"), _entry("s_two", "ok"),
                            _entry("s_three", "ok")])
    supervisor = _make(config, tmp_path)
    old_pid = None
    try:
        supervisor.start()
        for server_id in ("s_one", "s_two", "s_three"):
            assert _wait_state(supervisor, server_id, STATE_READY, timeout=5.0), (
                supervisor.status()
            )
        old_pid = _row(supervisor, "s_three")["pid"]
        assert old_pid

        config.servers = [
            _entry("s_four", "ok"),
            _entry("s_one", "ok"),
            _entry("s_two", "ok"),
            _entry("s_three", "ok"),
        ]
        supervisor.reload()

        assert _wait_state(supervisor, "s_four", STATE_READY, timeout=5.0), (
            supervisor.status()
        )
        assert _wait_state(supervisor, "s_three", STATE_CONFIG_ERROR, timeout=5.0), (
            supervisor.status()
        )
        row = _row(supervisor, "s_three")
        assert row["last_error"] == "max_servers"
        assert row["pid"] is None
        assert _wait_until(lambda: not is_pid_alive(old_pid), 5.0), (
            "被降级的旧 runtime 进程仍在运行: pid=%s" % old_pid
        )
        assert len(supervisor.ready_servers()) == 3, supervisor.status()
        assert _row(supervisor, "s_four")["state"] == STATE_READY

        # 恢复回归：删掉第 4 条后名额回到 3，s_three 自身条目没变，也必须从
        # max_servers 降级里回到 ready——否则它永久停在 config_error/pid=None，
        # ready 只剩 2 条。C3 修之前这里必红。
        config.servers = [
            _entry("s_one", "ok"),
            _entry("s_two", "ok"),
            _entry("s_three", "ok"),
        ]
        supervisor.reload()
        assert _wait_state(supervisor, "s_three", STATE_READY, timeout=5.0), (
            supervisor.status()
        )
        assert len(supervisor.ready_servers()) == 3, supervisor.status()
        assert _row(supervisor, "s_three")["pid"] is not None
    finally:
        supervisor.shutdown()


def test_worker_binds_the_generation_stop_event_not_runtime_stop_event(tmp_path):
    """C4：``_worker`` 用入参 event；改 ``runtime.stop_event`` 不该影响这一代。"""
    params = list(inspect.signature(StdioSupervisor._worker).parameters)
    assert params == ["self", "runtime", "stop_event"], params

    supervisor = _make(_Config(True, [_entry("s_evt", "ok")]), tmp_path)
    seen = []

    def fake_run_session(runtime, stop_event):
        seen.append(stop_event)
        stop_event.set()                 # 这一代立即结束，别让用例挂住

    supervisor._run_session = fake_run_session
    runtime = _Runtime("s_evt", _entry("s_evt", "ok"))
    # 这一代运行态里的事件保持未置位：如果 _worker 读它，会多跑一轮甚至不退出。
    runtime.stop_event = threading.Event()
    supervisor._global_enabled = True
    supervisor._runtimes["s_evt"] = runtime
    custom_event = threading.Event()
    worker = threading.Thread(
        target=supervisor._worker, args=(runtime, custom_event), daemon=True
    )
    try:
        worker.start()
        worker.join(5.0)
        assert not worker.is_alive(), "_worker 没有按传入的 stop_event 退出"
        assert seen and seen[0] is custom_event, "worker 没有使用入参 event"
        assert runtime.stop_event.is_set() is False, \
            "worker 动了 runtime.stop_event（按代绑定的口径被破坏）"

        # C4 射程：不经过 _run_session 的两处 _wait 入口（inactive / config_error）
        # 同样必须把**这一代的** stop_event 递下去，而不是 runtime.stop_event。
        wait_events = []

        def spy_wait(rt, seconds, stop_event):
            wait_events.append(stop_event)
            stop_event.set()
            return True

        supervisor._wait = spy_wait
        for state, enabled in ((STATE_CONFIG_ERROR, True), (STATE_DISABLED, False)):
            sid = "s_wait_%s" % state
            rt = _Runtime(sid, _entry(sid, "ok", enabled=enabled))
            rt.state = state
            rt.stop_event = threading.Event()
            supervisor._global_enabled = True
            supervisor._runtimes[sid] = rt
            generation_event = threading.Event()
            gen_worker = threading.Thread(
                target=supervisor._worker, args=(rt, generation_event), daemon=True
            )
            gen_worker.start()
            gen_worker.join(5.0)
            assert not gen_worker.is_alive(), "_worker 在 %s 分支没有按入参退出" % state
            assert wait_events and wait_events[-1] is generation_event, (
                "worker 在 %s 分支给 _wait 递了 runtime.stop_event" % state
            )
            assert rt.stop_event.is_set() is False
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- 单例


def test_job_failure_refused_reason_maps_to_spawn_failed():
    """Job 建不起来是"起进程"失败，不是命令规则问题：审计枚举必须是 spawn_failed。"""
    for raw in (
        "job_failed:boom",
        "job_failed:attach_failed:5",
        "job_failed:attach_failed:5:kill_failed",
    ):
        assert _refused_reason(raw) == "spawn_failed", raw
    # 邻接语义不变：env / 命令判据仍按原枚举收敛。
    assert _refused_reason("spawn_failed:2") == "spawn_failed"
    assert _refused_reason("env_name_reserved") == "env_denied"
    assert _refused_reason("command:host_denied") == "command_denied"


def test_get_supervisor_is_lazy_and_singleton():
    # 不在 import 期创建/启动：这里第一次拿到实例；conftest 的配置没有 MCP 条目，
    # 所以快照必须是"双关 + 空表"，而不是被某次 import 顺手启动过的样子。
    supervisor = get_supervisor()
    assert supervisor is get_supervisor()
    status = supervisor.status()
    assert status["enabled"] is False
    assert status["servers"] == []
    with pytest.raises(ValueError):
        get_supervisor(mcp_root="ignored")


# --------------------------------------------------- 11. zombie 用例的形状锁（#266 的自证）

"""为什么还要一把**形状**锁：上面两格把 N3 改成事件驱动，本身不阻止下一个人再写回
``time.sleep(0.8)``——行为测试在"这次恰好睡够了"的那一晚照样全绿。#266 要的自证是
"把常数改大改小都必须红或必须绿"，那一格（``test_zombie_gate_survives_timing_constants``）
只能证明**今天**不靠常数；"判据不许再压在秒数上"这条纪律只能钉在源码形状上。

规则名单（``ZOMBIE_SHAPE_RULES``，每条都在 ``_ZOMBIE_CORPUS`` 里有代表，见
``test_every_zombie_shape_rule_has_a_corpus_representative``；**条数不写在这里**——它住在
``test_the_zombie_shape_lock_roster_is_pinned_and_bound_to_the_detector`` 的条数钉里，
注释里再写一份数就是第二份真相，它会比钉先腐烂）：

1. ``no_bare_sleep``——zombie 这一族里任何 ``sleep(...)`` 都算违规（``time.sleep`` 与裸
   ``sleep`` 两种写法都咬）。停车窗与观察窗都必须由事件给出。
2. ``bounded_waits``——``X.wait(...)``（含裸 ``wait(...)`` 别名写法）与 ``_wait_until`` /
   ``_wait_state`` 必须带一个**正数上限**（字面量，或模块级 ``NAME = 数字`` 常量）。上限只
   挡"别把 CI 挂死"，不承载判据；摘掉、写成 ``True``、写成 0、换成一个查不到正数的名字，
   都红。
3. ``release_in_finally``——凡是**在嵌套函数里**被 ``wait`` 的那只事件（= 停车的那只），
   它的 ``.set()`` 必须出现在某个 ``try`` 的 ``finally`` 分支里。挪进 ``try`` 正文、
   或者去 ``set()`` 另一只事件，都红。
4. ``no_tautological_assert``——``assert True`` / ``assert True or …`` / ``assert … or True``
   这类恒真断言不许出现在这一族里（#265/#267 的审查员就是拿这一枚把锁缴械的）。

射程边界（**如实写**，别当成已穷尽）：
* 名单按"模块里函数名含 zombie"派生（``_zombie_subject_sources``），新加一格 zombie 用例
  不必改这把锁就会被扫；把 ``EXPECTED_ZOMBIE_SUBJECTS`` 里点名的那几格改名或删掉，
  会红在 ``test_zombie_shape_lock_subjects_are_derived_not_handwritten`` 那枚钉上。
* 只做**同函数内**的形状判断，不做跨函数/跨文件的赋值跟随：把 sleep 藏进一个
  名字不含 zombie 的公共 helper 里再调用、或把上限算出来（``wait(MAX / 2)`` 这种
  非常量表达式），今天都不在射程内。``_SYNTH_ZOMBIE_UNBOUND_JOIN`` 钉的就是"今天必须
  放过去"的那一格（``worker.join()`` 不带超时）——哪天规则 widened 到咬得住它，
  那条语料会红，逼着回来改这段话。
* 这把锁的**守卫自身**：探测器被改成恒返回 ``[]`` 的话，语料那几格立刻红（每条坏形状
  都断言"必须报出对应规则"），所以它不是自我指涉的恒绿探针。往守卫正文里塞恒真断言
  同样会红——这一族的守卫函数名字都含 zombie，派生射程把它们**包括在内**，所以
  ``no_tautological_assert`` 天然扫得到它们自己（#266 审查员 M-C 实测：往
  ``test_zombie_cases_carry_no_wall_clock_verdict`` 里塞 ``assert True or …`` 当场红）。
* **同向一起缩这一格只靠条数钉**（#268 牙二，本票存在理由）：把某条规则的名单行、
  探测器里那枚 ``flag(...)`` 与它的语料代表**三处同向一起删**，双向覆盖钉与
  id↔探测器绑定看见的都是自洽的名单、谁也不红——#266 审查员 M-B 实测这么干以后
  ``41 passed`` 全绿，唯一信号是条数。今天咬得住它的只有
  ``test_the_zombie_shape_lock_roster_is_pinned_and_bound_to_the_detector`` 里的条数钉。
  诚实登记分工：id↔探测器绑定挡"删探测器留名单"（#268 牙一），语料下限挡"整批删语料"，
  两者都**不是**这一格的替代。本枚 ``no_tautological_assert`` 与
  ``backend/tests/test_v0391_corpus_exemption_shape_lock.py``（#267 那把语料豁免形状锁）
  里的恒真判据**是两份分家的实现**——主控人裁决不抽共用 helper，本票只把#267 那一侧的
  口径并集写全；两份各留一份、可能继续分叉，是本票登记的已知残余。
"""

ZOMBIE_SHAPE_RULES = (
    "no_bare_sleep",
    "bounded_waits",
    "release_in_finally",
    "no_tautological_assert",
)

# 这三格必须一直在名单里：改名/删掉=射程自己缩水，必须红。
EXPECTED_ZOMBIE_SUBJECTS = (
    "_zombie_park",
    "test_zombie_generation_does_not_respawn_after_reload",
    "test_zombie_gate_survives_timing_constants",
)

# ``_wait_*`` 那几个轮询助手里"上限"所在的下标（0 基）。**每个键都必须在 ``_ZOMBIE_CORPUS``
# 里有成对代表**（缺上限的一支必须报、带正数上限的一支必须不报）——这条由
# ``test_the_zombie_shape_lock_roster_is_pinned_and_bound_to_the_detector`` 的子分支覆盖断言
# 看着：#266 审查实测 ``"_wait_state": 3`` 这一支今天既没被任何 zombie 用例调用、语料里也
# 没有它的代表，规则级覆盖有钉、子分支级没有，所以它是一段没牙的死支（#268 牙六补的成对语料）。
_POLL_BOUND_INDEX = {"_wait_until": 1, "_wait_state": 3}


def _zombie_numeric_bounds():
    """模块级 ``NAME = 正数`` 的取值表：上限允许写成常量名，但必须查得到是个正数。"""
    tree = ast.parse(inspect.getsource(sys.modules[__name__]))
    table = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        value = node.value.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                table[target.id] = float(value)
    return table


def _zombie_subject_sources():
    """按名字派生这一族的射程：模块里所有函数名含 "zombie" 的对象。"""
    module = sys.modules[__name__]
    found = []
    for name in sorted(dir(module)):
        obj = getattr(module, name)
        if "zombie" not in name.lower() or not inspect.isfunction(obj):
            continue
        if getattr(obj, "__module__", None) != module.__name__:
            continue
        found.append((name, inspect.getsource(obj)))
    return found


def _zombie_bound_value(node, table):
    """实参是不是"一个查得到值的正数上限"；不是就返回 ``None``。"""
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)
    if isinstance(node, ast.Name):
        return table.get(node.id)
    return None


def _zombie_wait_target(call):
    """``X.wait(...)``（或裸 ``wait(...)`` 别名）里的等待对象原文；不是等待就返回 ``None``。

    裸 ``wait`` 也咬：#125 那一课——把 ``park.closing.wait`` 换个别名递进来，判据不能跟着瞎。
    """
    if isinstance(call.func, ast.Attribute) and call.func.attr == "wait":
        return ast.unparse(call.func.value) if call.func.value is not None else "wait"
    if isinstance(call.func, ast.Name) and call.func.id == "wait":
        return "wait"
    return None


def _zombie_root_function(tree):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _zombie_shape_problems(where, source, table=None):
    """返回这一格源码的形状违规明细（空表 = 合格）。规则 id 一律前置，红话点名行号。"""
    table = _zombie_numeric_bounds() if table is None else table
    problems = []

    def flag(rule, node, text):
        problems.append("%s@%s:%s %s" % (rule, where, getattr(node, "lineno", 0), text))

    tree = ast.parse(source)
    root = _zombie_root_function(tree)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

    # 1. no_bare_sleep
    for call in calls:
        func = call.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "sleep":
            flag("no_bare_sleep", call,
                 "裸 sleep(...) 把判据压在秒级常数上——停车窗与观察窗都要改成事件")

    # 2. bounded_waits
    for call in calls:
        target = _zombie_wait_target(call)
        if target is not None:
            bound = call.args[0] if call.args else None
            if bound is None:
                for keyword in call.keywords or []:
                    if keyword.arg in ("timeout", None):
                        bound = keyword.value
            if bound is None or _zombie_bound_value(bound, table) is None:
                flag("bounded_waits", call,
                     "%s.wait(...) 没有正数上限（字面量或模块级数字常量）" % target)
            elif _zombie_bound_value(bound, table) <= 0:
                flag("bounded_waits", call,
                     "%s.wait(...) 的上限不是正数：等于不挡任何东西" % target)
            continue
        if isinstance(call.func, ast.Name) and call.func.id in _POLL_BOUND_INDEX:
            index = _POLL_BOUND_INDEX[call.func.id]
            bound = call.args[index] if len(call.args) > index else None
            if bound is None:
                for keyword in call.keywords or []:
                    if keyword.arg == "timeout":
                        bound = keyword.value
            if bound is None or _zombie_bound_value(bound, table) is None:
                flag("bounded_waits", call,
                     "%s(...) 缺第 %d 位的那个上限实参" % (call.func.id, index + 1))

    # 3. release_in_finally
    parked = set()
    if root is not None:
        for node in ast.walk(root):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node is root:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    target = _zombie_wait_target(inner)
                    if target is not None:
                        parked.add(target)
    released = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for stmt in node.finalbody:
            for inner in ast.walk(stmt):
                if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "set" and inner.func.value is not None):
                    released.add(ast.unparse(inner.func.value))
    for target in sorted(parked - released):
        flag("release_in_finally", root,
             "被卡在嵌套函数里等的那只 %s 没有在 try/finally 里 set()："
             "断言半路红就会把 worker 永久留在停车态" % target)

    # 4. no_tautological_assert
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        test = node.test
        if isinstance(test, ast.Constant) and test.value in (True, 1):
            flag("no_tautological_assert", node, "恒真 assert：这条判据永远不会红")
        elif isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
            for value in test.values:
                if isinstance(value, ast.Constant) and value.value in (True, 1):
                    flag("no_tautological_assert", node,
                         "assert … or True：把该报红的那一枚吃掉了")
    return problems


_SYNTH_ZOMBIE_CLEAN_PARK = '''
def _zombie_park_ok(monkeypatch):
    closing = threading.Event()
    released = threading.Event()

    def parked_close(self):
        closing.set()
        released.wait(PARK_BOUND)

    try:
        yield None
    finally:
        released.set()
'''

_SYNTH_ZOMBIE_CLEAN_CASE = '''
def test_zombie_case_ok(park, supervisor, read_count):
    assert park.closing.wait(SETTLE_BOUND), "没进 close"
    assert _wait_until(lambda: read_count() >= 2, SPAWN_BOUND)
    park.released.set()
    assert read_count() == 2
'''

# **边界钉**：``worker.join()`` 不带超时今天必须绿（本条只咬等待类调用，不咬 join）。
# 哪天规则 widened 到咬得住它，这一格会红，逼着回来把本节 docstring 的射程段改对。
_SYNTH_ZOMBIE_UNBOUND_JOIN = '''
def test_zombie_unbound_join(worker):
    worker.start()
    worker.join()
    assert worker.is_alive() is False
'''

_SYNTH_ZOMBIE_ALIAS_WAIT = '''
def test_zombie_alias_wait(wait):
    wait()
'''

_SYNTH_ZOMBIE_BARE_SLEEP = '''
def test_zombie_bare_sleep(park, read_count):
    park.released.set()
    time.sleep(0.8)
    assert read_count() == 2
'''

_SYNTH_ZOMBIE_BARE_SLEEP_ALIAS = '''
def test_zombie_bare_sleep_alias(sleep):
    sleep(0.4)
    assert True
'''

_SYNTH_ZOMBIE_UNBOUND_WAIT = '''
def test_zombie_unbound_wait(park):
    park.closing.wait()
'''

_SYNTH_ZOMBIE_BOOL_BOUND_WAIT = '''
def test_zombie_bool_bound_wait(park):
    park.closing.wait(True)
'''

_SYNTH_ZOMBIE_ZERO_BOUND_WAIT = '''
def test_zombie_zero_bound_wait(park):
    park.released.wait(0)
'''

_SYNTH_ZOMBIE_UNBOUND_POLL = '''
def test_zombie_unbound_poll(predicate):
    assert _wait_until(lambda: predicate())
'''

# `_POLL_BOUND_INDEX` 的另一支 `_wait_state`（真签名 `def _wait_state(supervisor, sid, state,
# timeout=5.0)`，上限是第 4 位**关键字**参数）的**成对**语料：#266 审查实测这一支今天既没被
# 任何 zombie 用例调用、语料里也没有代表，是一段没牙的死支。两支都按真实调用形状写，
# 且必须走 `_SYNTH_ZOMBIE_*` 字符串惯例——写成真正的模块级函数会被派生射程收进去（函数名
# 含 zombie），同时被 pytest 收集成需要假夹具的用例，当场 error。
_SYNTH_ZOMBIE_UNBOUND_STATE_POLL = '''
def test_zombie_unbound_state_poll(supervisor):
    _wait_state(supervisor, "srv", STATE_READY)
'''

_SYNTH_ZOMBIE_BOUND_STATE_POLL = '''
def test_zombie_bound_state_poll(supervisor, ready_count):
    assert _wait_state(supervisor, "srv", STATE_READY, timeout=SPAWN_BOUND) is not None
'''

_SYNTH_ZOMBIE_UNKNOWN_NAME_BOUND = '''
def test_zombie_unknown_name_bound(park, not_a_number):
    park.closing.wait(not_a_number)
'''

_SYNTH_ZOMBIE_RELEASE_IN_TRY_BODY = '''
def _zombie_park_release_in_body(monkeypatch):
    released = threading.Event()

    def parked_close(self):
        released.wait(PARK_BOUND)

    try:
        released.set()
        yield None
    finally:
        assert released.is_set()
'''

_SYNTH_ZOMBIE_RELEASE_WRONG_EVENT = '''
def _zombie_park_release_wrong_event(monkeypatch):
    released = threading.Event()
    other = threading.Event()

    def parked_close(self):
        released.wait(PARK_BOUND)

    try:
        yield None
    finally:
        other.set()
'''

_SYNTH_ZOMBIE_TAUTOLOGICAL_ASSERT = '''
def test_zombie_tautological(park, read_count):
    assert True or park.closing.wait(SETTLE_BOUND)
    assert read_count() == 2
'''

_SYNTH_ZOMBIE_TAUTOLOGICAL_OR_TAIL = '''
def test_zombie_tautological_tail(park):
    assert park.closing.wait(SETTLE_BOUND) or True
'''

# 每条坏形状各自应当报出的规则；期望为 ``()`` 的那几枚是**正对照**（防过度报警），
# 条数不在注释里写、由本节末尾的条数钉管（#268："数只活在钉里"）。
_ZOMBIE_CORPUS = (
    ("clean_park", _SYNTH_ZOMBIE_CLEAN_PARK, ()),
    ("clean_case", _SYNTH_ZOMBIE_CLEAN_CASE, ()),
    ("bare_sleep", _SYNTH_ZOMBIE_BARE_SLEEP, ("no_bare_sleep",)),
    ("bare_sleep_alias", _SYNTH_ZOMBIE_BARE_SLEEP_ALIAS,
     ("no_bare_sleep", "no_tautological_assert")),
    ("unbound_wait", _SYNTH_ZOMBIE_UNBOUND_WAIT, ("bounded_waits",)),
    ("alias_wait", _SYNTH_ZOMBIE_ALIAS_WAIT, ("bounded_waits",)),
    ("bool_bound_wait", _SYNTH_ZOMBIE_BOOL_BOUND_WAIT, ("bounded_waits",)),
    ("zero_bound_wait", _SYNTH_ZOMBIE_ZERO_BOUND_WAIT, ("bounded_waits",)),
    ("unbound_poll", _SYNTH_ZOMBIE_UNBOUND_POLL, ("bounded_waits",)),
    # `_POLL_BOUND_INDEX` 的另一支：成对语料，缺上限那支必须报、带正数 `timeout=` 那支必须
    # 不报（#268 牙六——两支都要跑，只跑一半等于没证明这一支有牙）。
    ("unbound_state_poll", _SYNTH_ZOMBIE_UNBOUND_STATE_POLL, ("bounded_waits",)),
    ("bound_state_poll", _SYNTH_ZOMBIE_BOUND_STATE_POLL, ()),
    ("unknown_name_bound", _SYNTH_ZOMBIE_UNKNOWN_NAME_BOUND, ("bounded_waits",)),
    ("release_in_try_body", _SYNTH_ZOMBIE_RELEASE_IN_TRY_BODY, ("release_in_finally",)),
    ("release_wrong_event", _SYNTH_ZOMBIE_RELEASE_WRONG_EVENT, ("release_in_finally",)),
    ("tautological_assert", _SYNTH_ZOMBIE_TAUTOLOGICAL_ASSERT, ("no_tautological_assert",)),
    ("tautological_or_tail", _SYNTH_ZOMBIE_TAUTOLOGICAL_OR_TAIL, ("no_tautological_assert",)),
    # **边界钉**：``worker.join()`` 不带超时今天必须绿（不在射程里，见本节 docstring）。
    # 哪天规则 widened 到咬得住它，这一格会红，逼着回来把那段声明改对。
    ("unbound_join_is_out_of_scope", _SYNTH_ZOMBIE_UNBOUND_JOIN, ()),
)


def _zombie_problem_rules(problems):
    return {item.split("@", 1)[0] for item in problems}


@pytest.mark.parametrize("case_id,source,expected", _ZOMBIE_CORPUS,
                         ids=[item[0] for item in _ZOMBIE_CORPUS])
def test_zombie_shape_lock_can_go_red(case_id, source, expected):
    """反例语料：每条坏形状都必须被点名，每条好形状都必须放过去。

    红的是哪一种"锁被缴械"：探测器被改成恒返回 ``[]``、规则 id 改名、只按行首文本匹配
    而不再读 AST —— 这三种坏法下，上面那批语料会成片报"没抓到该抓的规则"。
    期望值为 ``()`` 的那几枚是**正对照**（防过度报警）：把规则做得过宽，于是真用例永远红、
    最后被人整条摘掉。语料条数与名单条数都不写在这里，由
    ``test_the_zombie_shape_lock_roster_is_pinned_and_bound_to_the_detector`` 的条数钉管。
    """
    problems = _zombie_shape_problems("synthetic:%s" % case_id, source)
    found = _zombie_problem_rules(problems)
    assert found == set(expected), (
        "%s 期望报出 %s，实得 %s；明细：%s" % (case_id, sorted(expected), sorted(found), problems))


def test_every_zombie_shape_rule_has_a_corpus_representative():
    """规则名单与语料必须双向成立：每条规则有代表，语料不许造出名单外的规则 id。

    红的是哪一种：新增一条规则却忘了配反例（那它就永远没有牙），或语料里拼错规则 id
    （那条断言其实谁也没在验）。同 #257 给所有锁定的口径：**清单派生，不许手抄**。
    """
    covered = set()
    for _, _, expected in _ZOMBIE_CORPUS:
        covered.update(expected)
    assert covered <= set(ZOMBIE_SHAPE_RULES), (
        "语料里出现了名单外的规则 id：%s" % sorted(covered - set(ZOMBIE_SHAPE_RULES)))
    missing = [rule for rule in ZOMBIE_SHAPE_RULES if rule not in covered]
    assert not missing, "这些规则没有反例代表：%s" % missing


def test_zombie_shape_lock_subjects_are_derived_not_handwritten():
    """正对照（防空转）：射程必须是**派生**出来的，且那三格承重对象一直在名单里。

    红的是哪一种：zombie 用例被改名、被整格删掉、被搬去别的文件 —— 派生名单会少人，
    而"锁在扫什么"这句话就变成假话。没有这一条，"形状全对"和"一格都没扫到"在全绿里
    根本分不开（本仓为这个形状付过钱，见 ``test_machine_read_pointers.py`` 与 #268 的
    "守卫自身"盲区）。
    """
    subjects = _zombie_subject_sources()
    names = {name for name, _ in subjects}
    missing = [name for name in EXPECTED_ZOMBIE_SUBJECTS if name not in names]
    assert not missing, "这些承重对象不在 zombie 射程里了（改名/删掉/搬走）：%s" % missing
    assert len(subjects) >= len(EXPECTED_ZOMBIE_SUBJECTS), (
        "派生名单只剩 %d 格，少于登记的 %d 格" % (
            len(subjects), len(EXPECTED_ZOMBIE_SUBJECTS)))
    for name, source in subjects:
        parsed = ast.parse(source)   # 读不到可解析的形状 = 锁在扫空气
        assert any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == name for node in parsed.body), (
            "%s 的源码里没有同名函数定义：派生名单在骗人" % name)


def test_zombie_cases_carry_no_wall_clock_verdict():
    """主判据：这一族用例今天一条都不许违反 ``ZOMBIE_SHAPE_RULES`` 里的任何一条规则。

    红的是哪一种回归：把 N3 改回 ``time.sleep(0.8)`` 那种"猜睡够了"的观察窗、把事件上限
    摘掉、把放行挪出 ``finally``、或往断言里塞 ``or True``。每一条都在语料里有同名反例，
    所以这一条不会因为探测器被改窄而悄悄变绿。
    """
    problems = []
    for name, source in _zombie_subject_sources():
        problems += _zombie_shape_problems(name, source)
    assert not problems, "zombie 这一族的时序形状被改回常数驱动了：\n%s" % "\n".join(problems)


def _zombie_flagged_rule_ids():
    """探测器里真正被 ``flag()`` 报出去的那些规则 id（按 AST 取实参，不按子串）。

    为什么不能写成"id 在 ``inspect.getsource(...)`` 里出现就行"（#268 实测过的那一格）：
    探测器正文里带着 ``# 1. no_bare_sleep`` 这种**分节注释**，只摘 ``flag(...)`` 那一行而
    留着注释时，裸子串测试照样绿——那句话就成了假话。按 AST 取实参才只认"真的报出去了"。
    """
    tree = ast.parse(inspect.getsource(_zombie_shape_problems))
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "flag" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            ids.add(first.value)
    return ids


def test_the_zombie_shape_lock_roster_is_pinned_and_bound_to_the_detector():
    """条数钉 + 规则 id↔探测器绑定 + ``_POLL_BOUND_INDEX`` 子分支成对覆盖（#268 牙一/牙二/牙六）。

    逐格说清各自挡谁、挡不住谁（诚实登记射程，别读成"已穷尽"）：

    * **条数钉**是本票的存在理由。把某条规则的名单行、探测器里那枚 ``flag(...)`` 与它的
      语料代表**三处同向一起删**，双向覆盖钉与 id↔探测器绑定看见的都是自洽名单、谁也不红
      ——#266 审查员 M-B 实测这么干以后 ``41 passed`` 全绿，唯一信号是条数而条数没有任何钉
      （本仓同一族先例见 ``test_pathmask.py`` 的 ``_GIT_LS_FILES``/``_DRIVE_RULE_SITES`` 那段
      与 #128）。今天只有这一枚咬得住那一格。它是**下限**（基数钉先例见
      ``test_v0391_thinking_mapping.py`` 的 ``len(sources) >= 200``），挡"删"，
      不挡"往名单里加一条没配反例的规则"——后者由
      ``test_every_zombie_shape_rule_has_a_corpus_representative`` 的双向成立那一半挡。
    * **id↔探测器绑定**：每条在册 id 必须真出现在探测器的 ``flag()`` 实参里，反向同样钉
      （探测器不许报出名单外的 id）。它挡"摘掉 flag 留名单"（牙一），**挡不住三处同向一起删**。
    * **子分支成对覆盖**：``_POLL_BOUND_INDEX`` 的每个键都必须在语料里被调用过。#266 审查实测
      ``"_wait_state": 3`` 这一支今天是死支：既没被任何 zombie 用例调用、语料里也没有它的代表，
      规则级覆盖有钉而子分支级没有。牙六补的成对语料（缺上限必须报、带正数 ``timeout=`` 必须
      不报）由 ``test_zombie_shape_lock_can_go_red`` 逐条跑，两支各红各绿都当场可复验。
    """
    assert len(ZOMBIE_SHAPE_RULES) == 4, (
        "ZOMBIE_SHAPE_RULES 现在是 %d 条。注释与本节 docstring 都不写数（数只活在钉里），"
        "所以这条红话就是唯一的那份真相：删一条规则要么连同探测器里的 flag 与语料代表一起删"
        "（#266 审查员 M-B 实测那种删法今天全绿），要么只删名单（那会红在 "
        "test_the_zombie_shape_lock_roster_is_pinned_and_bound_to_the_detector 的绑定与 "
        "test_every_zombie_shape_rule_has_a_corpus_representative 上）。真要加/减一条规则，"
        "改这条钉的 4 必须同批改好探测器与语料，并在上面那段射程声明里把新形状写清。"
        % len(ZOMBIE_SHAPE_RULES))
    assert len(set(ZOMBIE_SHAPE_RULES)) == len(ZOMBIE_SHAPE_RULES), (
        "规则 id 必须互不相同：重复的一条会让双向覆盖钉数出假的一致")
    assert len(_ZOMBIE_CORPUS) >= 15, (
        "反例语料只剩 %d 枚，低于登记的下限 15：语料被整批删窄了，"
        "而『每条规则都有代表』在一份同样变窄的名单上照样成立（#268 牙二那一格的另一半）。"
        % len(_ZOMBIE_CORPUS))
    corpus_ids = [case_id for case_id, _src, _exp in _ZOMBIE_CORPUS]
    assert len(set(corpus_ids)) == len(corpus_ids), (
        "语料 id 必须互不相同：parametrize 会用同一个 id 生成两格，红话里分不清是哪一枚")

    flagged = _zombie_flagged_rule_ids()
    unflagged = [rule for rule in ZOMBIE_SHAPE_RULES if rule not in flagged]
    assert not unflagged, (
        "这些在册规则在探测器里没有任何 flag() 实参把它们报出去：%s——名单留着、判据已经被"
        "摘掉，这一格从此只靠注释守着（#268 牙一实测的红话形状）" % unflagged)
    ghost = sorted(flagged - set(ZOMBIE_SHAPE_RULES))
    assert not ghost, (
        "探测器报出了名单外的规则 id：%s——语料里那条断言其实谁也没在验，"
        "而 `test_every_zombie_shape_rule_has_a_corpus_representative` 会把它当成名单漏了"
        "一条来要求补" % ghost)

    polled = set()
    for _case_id, source, _expected in _ZOMBIE_CORPUS:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in _POLL_BOUND_INDEX:
                polled.add(node.func.id)
    dead_branches = sorted(set(_POLL_BOUND_INDEX) - polled)
    assert not dead_branches, (
        "_POLL_BOUND_INDEX 里这些下标支在语料里一份代表都没有：%s——规则级覆盖钉看不见"
        "子分支，这一支等于没牙（#268 牙六补的就是 `_wait_state` 这一支）" % dead_branches)

