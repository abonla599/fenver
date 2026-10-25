"""v0.37.5 T6：MCP stdio 管理端点 + main 接线 + lifespan 的行为锁。

覆盖计划 §4.4 / §7.2 的 E1–E8 与 F1/F2：

* E1 401/403：四条端点匿名 401、普通用户 403、bootstrap 管理员 200/202；
* E2 GET 不回 args/env 值：状态快照只投影白名单字段；
* E3 PUT 非法：未知键 / cwd / env / timeout / id 不一致 / 第 4 条 / 命令不存在
  一律 400，配置不变，错误详情不回显诱饵；
* E4 PUT 合法：202、落盘、supervisor.reload 被调、响应不含 args；
* E5 DELETE：404 / 200、配置少一条、reload 被调、注册表条目被注销；
* E6 settings：非 bool/未知键 400；合法写盘并 reload；
* E7 通用 config 不写 MCP：由 test_v0375_mcp_stdio_config 覆盖（这里不重复）；
* E8 审计无 args：after/before 只有 id/enabled/argv_fingerprint；
* F1/F2 lifespan：默认空配置不 spawn、退出后 state 为 None；必失败配置服务仍 200，
  supervisor 状态 config_error，不抛异常。

所有用例结束恢复 config_store 缓存与 supervisor 单例，避免污染别的文件。
"""
import copy
import json
import os
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.core import audit, config_store
from app.core.config_store import MCP_ENABLED_KEY, MCP_SERVERS_KEY
from app.core.mcp_stdio_admin import _SERVER_STATUS_KEYS
from app.main import app

DECOY_ARG = "DECOY_ARG_VALUE_t6_5d31"
DECOY_ENV_VALUE = "DECOY_ENV_VALUE_t6_9a42"
DECOY_STDERR = "DECOY_STDERR_TEXT_t6_c7e8"


def _entry(server_id="files", **overrides):
    raw = {
        "id": server_id,
        "command": sys.executable,
        "args": [],
        "env_allowlist": [],
        "enabled": False,
    }
    raw.update(overrides)
    return raw


def _ids():
    return [entry["id"] for entry in config_store.read()[MCP_SERVERS_KEY]]


def _missing_command():
    """绝对路径、但保证不存在（spawn 前判据会把它打成 config_error）。"""
    name = "fenver-v0375-no-such.exe" if os.name == "nt" else "fenver-v0375-no-such"
    path = os.path.join(os.path.abspath(os.sep), "fenver-v0375-no-such-dir", name)
    assert not os.path.exists(path)
    return path


class FakeSupervisor:
    """最小 supervisor：status 快照 + reload 计数；reload 后按配置重算状态。"""

    def __init__(self, status=None):
        self.reloads = 0
        self._status = status or {"enabled": False, "servers": []}

    def status(self):
        return copy.deepcopy(self._status)

    def reload(self):
        self.reloads += 1
        cfg = config_store.read()
        global_on = bool(cfg.get(MCP_ENABLED_KEY))
        servers = []
        for entry in cfg.get(MCP_SERVERS_KEY) or []:
            servers.append({
                "id": entry["id"],
                "enabled": bool(entry["enabled"]),
                "state": "starting" if global_on and entry["enabled"] else "disabled",
            })
        self._status = {"enabled": global_on, "servers": servers}


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
def fake_supervisor():
    supervisor = FakeSupervisor()
    app.state.mcp_supervisor = supervisor
    try:
        yield supervisor
    finally:
        app.state.mcp_supervisor = None


@pytest.fixture
def fresh_supervisor(monkeypatch):
    """把模块级单例摘掉，让 lifespan 真的新建一个；结束后停掉并还原。"""
    import app.core.mcp_stdio as mcp_stdio

    previous = mcp_stdio._SUPERVISOR
    monkeypatch.setattr(mcp_stdio, "_SUPERVISOR", None)
    yield
    current = mcp_stdio._SUPERVISOR
    if current is not None:
        try:
            current.shutdown()
        except Exception:                                    # noqa: BLE001 - 清理不掩盖断言
            pass


# --------------------------------------------------------------------------- E1


@pytest.mark.parametrize("method,path,kwargs", [
    ("get", "/v1/admin/mcp/stdio", {}),
    ("put", "/v1/admin/mcp/stdio/servers/files", {"json": _entry("files")}),
    ("delete", "/v1/admin/mcp/stdio/servers/files", {}),
    ("put", "/v1/admin/mcp/stdio/settings", {"json": {"enabled": False}}),
])
def test_admin_endpoints_reject_anonymous_and_normal_users(
        client, enforced, config_sandbox, fake_supervisor, method, path, kwargs):
    assert client.request(method, path, **kwargs).status_code == 401
    ordinary = enforced("ordinary")
    assert client.request(method, path, headers=ordinary, **kwargs).status_code == 403
    assert config_store.read()[MCP_SERVERS_KEY] == []
    assert config_store.read()[MCP_ENABLED_KEY] is False
    assert fake_supervisor.reloads == 0


def test_bootstrap_admin_gets_200_on_status(client, config_sandbox, fake_supervisor):
    admin = {"Authorization": "Bearer " + os.environ["ACCESS_TOKEN"]}
    res = client.get("/v1/admin/mcp/stdio", headers=admin)
    assert res.status_code == 200


# --------------------------------------------------------------------------- E2


def test_get_never_returns_args_env_values_or_stderr(client, config_sandbox):
    supervisor = FakeSupervisor(status={
        "enabled": True,
        "servers": [{
            "id": "files",
            "enabled": True,
            "state": "ready",
            "pid": 123,
            "restarts": 0,
            "uptime_s": 1.5,
            "tools_total": 2,
            "tools_exposed": 2,
            "schema_flags": {"freeform": 0},
            "command": sys.executable,
            "command_basename": os.path.basename(sys.executable),
            "argv_fingerprint": "f" * 64,
            "env_names": ["MCP_LOG_LEVEL"],
            "cwd": "C:/tmp/files",
            "last_error": "",
            # 下面三格真实 status 不会返回；这里放进来证明端点自己也会剔掉。
            "args": ["--token", DECOY_ARG],
            "env": {"MCP_LOG_LEVEL": DECOY_ENV_VALUE},
            "stderr": DECOY_STDERR,
        }],
    })
    app.state.mcp_supervisor = supervisor
    try:
        res = client.get("/v1/admin/mcp/stdio")
    finally:
        app.state.mcp_supervisor = None

    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    body = res.json()
    assert body["enabled"] is True
    server = body["servers"][0]
    assert set(server) == set(_SERVER_STATUS_KEYS)
    for key in ("id", "enabled", "state", "pid", "restarts", "uptime_s",
                "tools_total", "tools_exposed", "schema_flags", "command",
                "command_basename", "argv_fingerprint", "env_names", "cwd",
                "last_error"):
        assert key in server
    for decoy in (DECOY_ARG, DECOY_ENV_VALUE, DECOY_STDERR):
        assert decoy not in res.text


# --------------------------------------------------------------------------- E4


def test_put_legal_persists_and_reloads(client, config_sandbox, fake_supervisor):
    res = client.put("/v1/admin/mcp/stdio/servers/files",
                     json=_entry("files", args=["--mode", DECOY_ARG]))
    assert res.status_code == 202
    assert res.json()["status"] == "accepted"
    assert res.json()["server"]["id"] == "files"
    assert DECOY_ARG not in res.text
    assert fake_supervisor.reloads == 1
    assert _ids() == ["files"]
    stored = config_store.read()[MCP_SERVERS_KEY][0]
    assert stored["args"] == ["--mode", DECOY_ARG]
    on_disk = json.loads(config_sandbox.read_text(encoding="utf-8"))
    assert on_disk[MCP_SERVERS_KEY][0]["args"] == ["--mode", DECOY_ARG]


def test_put_replaces_same_entry_instead_of_appending(client, config_sandbox,
                                                      fake_supervisor):
    config_store.apply_update({MCP_ENABLED_KEY: True})
    assert client.put("/v1/admin/mcp/stdio/servers/files",
                      json=_entry("files", args=["--one"])).status_code == 202
    res = client.put("/v1/admin/mcp/stdio/servers/files",
                     json=_entry("files", args=["--two"], enabled=True))
    assert res.status_code == 202
    assert _ids() == ["files"]
    stored = config_store.read()[MCP_SERVERS_KEY][0]
    assert stored["args"] == ["--two"] and stored["enabled"] is True
    assert fake_supervisor.reloads == 2
    assert res.json()["server"]["state"] == "starting"


# --------------------------------------------------------------------------- E3


@pytest.mark.parametrize("bad", [
    {"cwd": "C:/tmp"},
    {"env": {"A": "B"}},
    {"timeout": 5},
    {"command": sys.executable, "unknown": DECOY_ARG},
])
def test_put_rejects_unknown_fields_without_echo(client, config_sandbox,
                                                 fake_supervisor, bad):
    body = _entry("files")
    body.update(bad)
    res = client.put("/v1/admin/mcp/stdio/servers/files", json=body)
    assert res.status_code == 400
    assert res.json()["detail"] == "unsupported_field"
    assert DECOY_ARG not in res.text
    assert _ids() == []
    assert fake_supervisor.reloads == 0


def test_put_rejects_id_mismatch(client, config_sandbox, fake_supervisor):
    res = client.put("/v1/admin/mcp/stdio/servers/files",
                     json=_entry("other"))
    assert res.status_code == 400
    assert res.json()["detail"] == "id_mismatch"
    assert _ids() == []
    assert fake_supervisor.reloads == 0


def test_put_accepts_body_id_equal_to_path(client, config_sandbox, fake_supervisor):
    res = client.put("/v1/admin/mcp/stdio/servers/files", json=_entry("files"))
    assert res.status_code == 202
    assert _ids() == ["files"]


def test_put_rejects_invalid_server_id(client, config_sandbox, fake_supervisor):
    res = client.put("/v1/admin/mcp/stdio/servers/BAD", json=_entry("BAD"))
    assert res.status_code == 400
    assert res.json()["detail"] == "invalid_server_id"
    assert _ids() == []
    assert fake_supervisor.reloads == 0


def test_fourth_server_is_rejected_and_config_unchanged(client, config_sandbox,
                                                        fake_supervisor):
    seeded = [_entry("one"), _entry("two"), _entry("three")]
    cfg, rejected = config_store.apply_update({MCP_SERVERS_KEY: seeded})
    assert rejected == [] and cfg[MCP_SERVERS_KEY]

    res = client.put("/v1/admin/mcp/stdio/servers/four", json=_entry("four"))
    assert res.status_code == 400
    assert res.json()["detail"] == "too_many_servers"
    assert _ids() == ["one", "two", "three"]
    assert fake_supervisor.reloads == 0


def test_put_rejects_missing_command_without_echoing_args(client, config_sandbox,
                                                          fake_supervisor):
    body = _entry("files", command=_missing_command(), args=[DECOY_ARG])
    res = client.put("/v1/admin/mcp/stdio/servers/files", json=body)
    assert res.status_code == 400
    assert res.json()["detail"].startswith("invalid_entry:command:")
    assert DECOY_ARG not in res.text
    assert _ids() == []
    assert fake_supervisor.reloads == 0


def test_put_rejects_bad_env_allowlist_without_echo(client, config_sandbox,
                                                    fake_supervisor):
    res = client.put("/v1/admin/mcp/stdio/servers/files",
                     json=_entry("files", env_allowlist=["API_KEY"]))
    assert res.status_code == 400
    assert res.json()["detail"] == "invalid_entry:env_name_denied"
    assert "API_KEY" not in res.text
    assert _ids() == []


# --------------------------------------------------------------------------- E5


def test_delete_missing_server_is_404(client, config_sandbox, fake_supervisor):
    res = client.delete("/v1/admin/mcp/stdio/servers/ghost")
    assert res.status_code == 404
    assert res.json()["detail"] == "server_not_found"
    assert fake_supervisor.reloads == 0


def test_delete_removes_config_reloads_and_unregisters(client, config_sandbox,
                                                       fake_supervisor):
    from app.tools.registry import tools_registry

    config_store.apply_update({MCP_SERVERS_KEY: [_entry("files"),
                                                _entry("other")]})
    tools_registry["mcp_files__ghost_tool"] = {"source": "mcp-stdio:files"}
    try:
        res = client.delete("/v1/admin/mcp/stdio/servers/files")
        assert res.status_code == 200
        assert res.json()["status"] == "deleted"
        assert res.json()["server"]["state"] == "disabled"
        assert _ids() == ["other"], "删除后配置表必须少一条"
        assert fake_supervisor.reloads == 1
        assert "mcp_files__ghost_tool" not in tools_registry, "DELETE 必须显式注销工具"
    finally:
        tools_registry.pop("mcp_files__ghost_tool", None)


# --------------------------------------------------------------------------- E6


@pytest.mark.parametrize("body", [
    {"enabled": 1},
    {"enabled": "true"},
    {"enabled": None},
    {},
    {"enabled": True, "extra": DECOY_ARG},
])
def test_settings_rejects_bad_shape(client, config_sandbox, fake_supervisor, body):
    res = client.put("/v1/admin/mcp/stdio/settings", json=body)
    assert res.status_code == 400
    assert res.json()["detail"] == "settings_must_be_enabled_bool"
    assert DECOY_ARG not in res.text
    assert config_store.read()[MCP_ENABLED_KEY] is False
    assert fake_supervisor.reloads == 0


def test_settings_writes_and_reloads(client, config_sandbox, fake_supervisor):
    config_store.apply_update({MCP_SERVERS_KEY: [_entry("files", enabled=True)]})
    res = client.put("/v1/admin/mcp/stdio/settings", json={"enabled": True})
    assert res.status_code == 200
    assert res.json() == {"enabled": True}
    assert config_store.read()[MCP_ENABLED_KEY] is True
    assert fake_supervisor.reloads == 1
    on_disk = json.loads(config_sandbox.read_text(encoding="utf-8"))
    assert on_disk[MCP_ENABLED_KEY] is True

    res = client.put("/v1/admin/mcp/stdio/settings", json={"enabled": False})
    assert res.status_code == 200
    assert res.json() == {"enabled": False}
    assert config_store.read()[MCP_ENABLED_KEY] is False
    assert fake_supervisor.reloads == 2


# --------------------------------------------------------------------------- E8


def _last_audit():
    entries = audit.read(limit=5)
    assert entries
    return entries[-1]


def test_audit_has_no_args_env_decoys(client, config_sandbox, fake_supervisor):
    res = client.put("/v1/admin/mcp/stdio/servers/files",
                     json=_entry("files", args=["--token", DECOY_ARG],
                                 env_allowlist=["MCP_LOG_LEVEL"], enabled=True))
    assert res.status_code == 202
    last = _last_audit()
    assert last["action"] == "mcp.stdio.config"
    assert last["target"] == "files"
    assert DECOY_ARG not in json.dumps(last, ensure_ascii=False)
    assert DECOY_ENV_VALUE not in json.dumps(last, ensure_ascii=False)
    assert last["after"] == [{
        "id": "files",
        "enabled": True,
        "argv_fingerprint": last["after"][0]["argv_fingerprint"],
    }]
    assert set(last["after"][0]) == {"id", "enabled", "argv_fingerprint"}

    assert client.delete("/v1/admin/mcp/stdio/servers/files").status_code == 200
    last = _last_audit()
    assert last["action"] == "mcp.stdio.config"
    assert last["target"] == "files"
    assert last["after"] == []
    assert DECOY_ARG not in json.dumps(last, ensure_ascii=False)

    assert client.put("/v1/admin/mcp/stdio/settings",
                      json={"enabled": True}).status_code == 200
    last = _last_audit()
    assert last["action"] == "mcp.stdio.config"
    assert last["target"] == "settings"
    assert last["before"] == {"enabled": False}
    assert last["after"] == {"enabled": True}
    assert "args" not in json.dumps(last, ensure_ascii=False)


# --------------------------------------------------------------------------- 并发


def test_concurrent_put_cannot_overwrite_a_newer_entry(
        client, config_sandbox, fake_supervisor, monkeypatch):
    """去掉 _WRITE_LOCK 时这条会红：第二个写入会被第一个的旧表覆盖掉。

    用包装后的 apply_update 制造确定性交错：第一个请求在写盘前挂起，等第二个
    请求写完后才继续。锁在则第二个请求根本进不来（等到第一个释放），两个条目
    都在；锁没了则第一个带着"读时的空表"把第二个条目覆盖掉。
    """
    real_apply = config_store.apply_update
    state_lock = threading.Lock()
    calls = {"n": 0}
    first_at_apply = threading.Event()
    second_done = threading.Event()

    def slow_apply(partial):
        with state_lock:
            calls["n"] += 1
            order = calls["n"]
        if order == 1:
            first_at_apply.set()
            second_done.wait(timeout=0.5)
        else:
            result = real_apply(partial)
            second_done.set()
            return result
        return real_apply(partial)

    monkeypatch.setattr(config_store, "apply_update", slow_apply)

    outcomes = {}

    def put(server_id):
        outcomes[server_id] = client.put(
            f"/v1/admin/mcp/stdio/servers/{server_id}",
            json=_entry(server_id)).status_code

    first = threading.Thread(target=put, args=("alpha",))
    first.start()
    assert first_at_apply.wait(2), "第一个请求没有进入写盘点"
    second = threading.Thread(target=put, args=("beta",))
    second.start()
    first.join(5)
    second.join(5)

    assert outcomes == {"alpha": 202, "beta": 202}
    assert set(_ids()) == {"alpha", "beta"}, (
        "并发 PUT 丢了一条配置：读-改-写没有被 _WRITE_LOCK 串行")


# --------------------------------------------------------------------------- F1/F2


def test_lifespan_default_config_does_not_spawn_and_clears_state(
        config_sandbox, fresh_supervisor):
    config_sandbox.write_text("{}", encoding="utf-8")
    config_store.restore()

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        supervisor = app.state.mcp_supervisor
        assert supervisor is not None, "lifespan 必须装上 supervisor"
        assert supervisor.status() == {"enabled": False, "servers": []}
        assert getattr(supervisor, "_runtimes", {}) == {}, "默认空配置不许 spawn"
        res = client.get("/v1/admin/mcp/stdio")
        assert res.status_code == 200
        assert res.json()["servers"] == []

    assert app.state.mcp_supervisor is None
    assert supervisor._started is False


def test_lifespan_survives_a_failing_mcp_config(config_sandbox, fresh_supervisor):
    config_sandbox.write_text(json.dumps({
        MCP_ENABLED_KEY: True,
        MCP_SERVERS_KEY: [_entry("broken", command=_missing_command(), enabled=True)],
    }), encoding="utf-8")
    config_store.restore()

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200, "MCP 配置坏不许打死服务"
        deadline = time.time() + 3.0
        state = None
        while time.time() < deadline:
            body = client.get("/v1/admin/mcp/stdio").json()
            servers = body.get("servers") or []
            if servers:
                state = servers[0].get("state")
                if state == "config_error":
                    break
            time.sleep(0.05)
        assert state == "config_error", f"必失败配置应停在 config_error，实际 {state!r}"
        assert app.state.mcp_supervisor is not None

    assert app.state.mcp_supervisor is None


def test_lifespan_survives_supervisor_construction_failure(
        config_sandbox, fresh_supervisor, monkeypatch):
    """lifespan 的 try/except 是承重的：supervisor 造不出来时服务必须照常启动。"""
    import app.core.mcp_stdio as mcp_stdio

    def boom(**_kwargs):
        raise RuntimeError("supervisor boom")

    monkeypatch.setattr(mcp_stdio, "get_supervisor", boom)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.mcp_supervisor is None
    assert app.state.mcp_supervisor is None
