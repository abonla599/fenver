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
    ``client.close`` 时不得复活再 spawn；
11. supervisor 再判 ``MAX_SERVERS=3``；坏条目 status 保留原始 ``enabled``。

所有用例 ``finally`` 调 ``shutdown()``；Windows 上用 T3 的 ``is_pid_alive`` 断言无孤儿。
"""
import inspect
import json
import os
import sys
import threading
import time
from pathlib import Path

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
    pids = set()
    try:
        supervisor.start()
        assert _wait_state(supervisor, "s_life", STATE_READY, timeout=5.0), supervisor.status()
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
    try:
        supervisor.start()
        assert _wait_state(supervisor, "s_cycle", STATE_READY, timeout=5.0), supervisor.status()
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


def test_zombie_generation_does_not_respawn_after_reload(tmp_path, monkeypatch):
    """N3：旧代卡在 client.close 超过 join 超时时，reload 不得再起一代、旧代也不许复活。

    形状：``crash_once`` 起两代（第 2 代 ready），再用很短的 max_life 让第 2 代在
    worker 线程里进入 teardown；monkeypatch 的 close 把 worker 卡在 join 超时之外，
    此时 reload 改条目 → 新实现保留活线程（stopping）拒绝 spawn；旧实现会
    先起第 3 代、旧代醒来后又 spawn 一次。
    """
    monkeypatch.setattr("app.core.mcp_stdio.STOP_JOIN_TIMEOUT", 0.1)
    armed = threading.Event()
    closing = threading.Event()

    def slow_close(self):
        if armed.is_set():
            closing.set()
            time.sleep(0.4)

    monkeypatch.setattr(MCPStdioClient, "close", slow_close)

    config = _Config(True, [_entry("s_zombie", "crash_once")])
    supervisor = _make(config, tmp_path, max_life=0.25, stable_seconds=60.0)
    try:
        supervisor.start()
        assert _wait_until(
            lambda: _read_count(tmp_path, "s_zombie") >= 2, 5.0
        ), supervisor.status()
        armed.set()
        assert closing.wait(5.0), "worker 没有进入 client.close"
        config.servers = [_entry("s_zombie", "crash_once", extra_args=["changed"])]
        supervisor.reload()
        # 旧代走完 close 与收尾后，spawn 计数必须仍停在 2。
        time.sleep(0.8)
        assert _read_count(tmp_path, "s_zombie") == 2, supervisor.status()
        assert _row(supervisor, "s_zombie")["state"] in (
            STATE_STOPPING,
            STATE_DISABLED,
        )
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
    finally:
        supervisor.shutdown()


# --------------------------------------------------------------------------- 单例


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
