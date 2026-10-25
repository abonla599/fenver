"""v0.37.5 T4：stdio supervisor（状态机 / 退避 / 熔断 / 审计 / 状态快照）行为锁。

覆盖计划 §7.2 的 C8（8h 寿命注入 0.2s 回收）、D9（进程崩溃后不再 available）与
T4 验收段：

1. 全局关 / 条目关 → 不 spawn，状态 ``disabled``；
2. ``ok`` → 2s 内 ``ready``、pid>0、tools_total=4，status 不含 args/env 值；
3. ``crash_once`` → 第二次成功，``restarts==1``；
4. ``crash_always`` → 5 次失败后 ``circuit_open``，第 6 次在 cooldown 内不 spawn；
   cooldown 到期只探一次（第 6 次），探活失败重新熔断；
5. 配置错误（命令不存在 / 协议版本不符）→ ``config_error`` 终态，不重启；
   reload 同一份配置也不重启；
6. ``max_life`` 注入 0.3s → 到点回收重启，无孤儿；
7. 审计：start / exit / refused / circuit_open 各出现，且不含 args/env 诱饵、
   stderr 正文、完整命令行；
8. ``shutdown()`` 幂等、无线程残留、status 无 running server；
9. ``reload()``：enabled→disabled 停进程，disabled→enabled 起进程。

所有用例 ``finally`` 调 ``shutdown()``；Windows 上用 T3 的 ``is_pid_alive`` 断言无孤儿。
"""
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
    STATE_DISABLED,
    STATE_READY,
    STATE_STARTING,
    STATE_STOPPING,
    StdioSupervisor,
    get_supervisor,
)
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
