"""v0.37.5 T1：MCP stdio 配置面（两个键、条目消毒、可见性、响应/审计清洗）。

判据：
1. 默认双关：`mcp_stdio_enabled=False`、`mcp_stdio_servers=[]`；
2. restore 路径纯形状消毒：坏条目丢、重复 id 丢后者、截断到 3 条、绝对但
   **不存在**的命令保留（证明 restore 不碰磁盘），全程零 raise；
3. apply_update 整表判：一条坏 → 整个键进 rejected，旧值不变；
4. `/v1/config` 对 admin 与普通用户都不含两个键；
5. 通用 `POST /v1/admin/config` 写不动 MCP 键（Pydantic 忽略未声明字段）；
6. 泄漏锁：通用配置写一次之后，**响应体**与 `audit.jsonl` 都不含 args 诱饵，
   也不含两个键名；
7. 命令闸（相对/UNC/未知键/宿主黑名单/平台可执行判据）与 env 闸（密钥/代理名
   硬拒，普通名通过）、args/整表上限。
"""
import copy
import json
import os
import sys
from pathlib import Path

import pytest

from app.core import config_store, mcp_stdio_config
from app.core.config_store import MCP_ENABLED_KEY, MCP_SERVERS_KEY
from app.core.mcp_stdio_config import (
    ARG_MAX_CHARS,
    MAX_ARGS,
    MAX_ENV_ALLOWLIST,
    MAX_SERVERS,
    command_problem,
    env_name_denied,
    normalize_entry,
    normalize_servers,
)


def _entry(server_id="files", **overrides):
    raw = {
        "id": server_id,
        "command": sys.executable,
        "args": ["-m", "mcp_server"],
        "env_allowlist": ["MCP_LOG_LEVEL"],
        "enabled": False,
    }
    raw.update(overrides)
    return raw


def _abs_missing_tool():
    """绝对路径、但保证不存在：用来证明 restore 不判存在性。"""
    name = "tool.exe" if os.name == "nt" else "tool"
    path = os.path.join(os.path.abspath(os.sep), "fenver-v0375-no-such-dir", name)
    assert not os.path.exists(path)
    return path


def _write_cfg(path, servers, enabled=False):
    path.write_text(json.dumps({MCP_ENABLED_KEY: enabled,
                                MCP_SERVERS_KEY: servers}), encoding="utf-8")


@pytest.fixture
def config_sandbox(tmp_path, monkeypatch):
    """把配置文件的落点与内存缓存都挪进临时域，用例结束原样还原。"""
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CONFIG_DB_PATH", str(path))
    with config_store._lock:
        previous = copy.deepcopy(config_store._config)
        config_store._config.clear()
        config_store._config.update(copy.deepcopy(config_store.DEFAULTS))
    yield path
    with config_store._lock:
        config_store._config.clear()
        config_store._config.update(previous)


def _ids():
    return [entry["id"] for entry in config_store.read()[MCP_SERVERS_KEY]]


# ---------- 1. 默认值 ----------

def test_defaults_are_off_and_empty(config_sandbox):
    assert config_store.DEFAULTS[MCP_ENABLED_KEY] is False
    assert config_store.DEFAULTS[MCP_SERVERS_KEY] == []
    cfg = config_store.read()
    assert cfg[MCP_ENABLED_KEY] is False
    assert cfg[MCP_SERVERS_KEY] == []


# ---------- 2. restore：坏条目丢、好条目留、不碰盘、零 raise ----------

def test_restore_drops_bad_entries_and_truncates(config_sandbox):
    rel = {"id": "rel", "command": "mcp_server.exe"}
    with_cwd = {"id": "cwd", "command": sys.executable, "cwd": "."}
    _write_cfg(config_sandbox, [_entry("good"), rel, with_cwd])
    config_store.restore(path=str(config_sandbox))
    assert _ids() == ["good"]

    too_many_args = {"id": "many", "command": sys.executable,
                     "args": ["x"] * (MAX_ARGS + 1)}
    bad_id = {"id": "BADID", "command": sys.executable}
    denied_env = {"id": "env", "command": sys.executable,
                  "env_allowlist": ["GITHUB_TOKEN"]}
    _write_cfg(config_sandbox, [too_many_args, bad_id, denied_env])
    config_store.restore(path=str(config_sandbox))
    assert _ids() == []

    _write_cfg(config_sandbox, [_entry("a"), _entry("b"), _entry("c"), _entry("d")])
    config_store.restore(path=str(config_sandbox))
    assert _ids() == ["a", "b", "c"], "restore 必须截断到 MAX_SERVERS 条"

    _write_cfg(config_sandbox, [_entry("dup"), _entry("dup", args=["-v"])])
    config_store.restore(path=str(config_sandbox))
    assert _ids() == ["dup"], "重复 id 丢后者"
    assert config_store.read()[MCP_SERVERS_KEY][0]["args"] == ["-m", "mcp_server"]


def test_restore_keeps_missing_command_and_never_touches_disk(config_sandbox,
                                                             monkeypatch):
    ghost = _abs_missing_tool()
    _write_cfg(config_sandbox, [{"id": "ghost", "command": ghost}])
    config_store.restore(path=str(config_sandbox))
    servers = config_store.read()[MCP_SERVERS_KEY]
    assert [entry["id"] for entry in servers] == ["ghost"]
    assert servers[0]["command"] == ghost

    # 把存在性判据全部换成会炸的桩：restore 只要碰一次盘（isfile 换成 exists /
    # access 也一样）就红，而不是靠"这个路径恰好不存在"这种弱证据。
    def boom(*args, **kwargs):
        raise AssertionError("restore 路径不许碰磁盘")

    monkeypatch.setattr(mcp_stdio_config.os.path, "isfile", boom)
    monkeypatch.setattr(mcp_stdio_config.os.path, "exists", boom)
    monkeypatch.setattr(mcp_stdio_config.os, "access", boom)
    config_store.restore(path=str(config_sandbox))
    servers = config_store.read()[MCP_SERVERS_KEY]
    assert [entry["id"] for entry in servers] == ["ghost"]
    assert servers[0]["command"] == ghost, "摘掉磁盘探针后 ghost 仍必须原样保留"
    direct = mcp_stdio_config.sanitize_servers(
        [{"id": "ghost", "command": ghost}])
    assert [entry["id"] for entry in direct] == ["ghost"]


def test_restore_never_echoes_forged_id_from_log(capsys):
    """手改配置里的 id 带 CR/LF 时不能伪造一行 stdout。"""
    out = mcp_stdio_config.sanitize_servers([
        {"id": "bad\nFAKE", "command": sys.executable},
    ])
    assert out == []
    captured = capsys.readouterr().out
    assert "FAKE" not in captured, "id 里的换行把伪造内容写进了日志"
    assert "丢弃条目 ?: bad_id" in captured, "不可信的 id 必须以 ? 落日志"


# ---------- 3. apply_update：一条坏 → 整键拒，旧值不变 ----------

def test_apply_update_rejects_whole_table_on_one_bad_entry(config_sandbox):
    cfg, rejected = config_store.apply_update({MCP_SERVERS_KEY: [_entry("keep")]})
    assert rejected == []
    before = config_store.read()[MCP_SERVERS_KEY]
    assert [entry["id"] for entry in before] == ["keep"]

    bad_table = [_entry("keep"), {"id": "bad", "command": "relative-tool"}]
    cfg, rejected = config_store.apply_update({MCP_SERVERS_KEY: bad_table})
    assert rejected == [MCP_SERVERS_KEY], "一条坏必须整键拒，不许写一半"
    assert config_store.read()[MCP_SERVERS_KEY] == before

    good_table = [_entry("one"), _entry("two", enabled=True)]
    cfg, rejected = config_store.apply_update({MCP_SERVERS_KEY: good_table})
    assert rejected == []
    assert [entry["id"] for entry in cfg[MCP_SERVERS_KEY]] == ["one", "two"]


def test_apply_update_rejects_bare_api_key_env(config_sandbox):
    """裸 `API_KEY` 也是凭据名：T3 会按 allowlist 从服务进程环境复制值。"""
    cfg, rejected = config_store.apply_update({MCP_SERVERS_KEY: [_entry("keep")]})
    assert rejected == []
    before = config_store.read()[MCP_SERVERS_KEY]

    poisoned = [_entry("leak", env_allowlist=["API_KEY"])]
    cfg, rejected = config_store.apply_update({MCP_SERVERS_KEY: poisoned})
    assert rejected == [MCP_SERVERS_KEY], "裸 API_KEY 必须让整条配置进 rejected"
    assert config_store.read()[MCP_SERVERS_KEY] == before


# ---------- 4. enabled 必须是 bool ----------

def test_enabled_must_be_bool(config_sandbox):
    for value in ("true", 1, 0, None, [True]):
        cfg, rejected = config_store.apply_update({MCP_ENABLED_KEY: value})
        assert rejected == [MCP_ENABLED_KEY], value
        assert config_store.read()[MCP_ENABLED_KEY] is False
    cfg, rejected = config_store.apply_update({MCP_ENABLED_KEY: True})
    assert rejected == [] and cfg[MCP_ENABLED_KEY] is True


# ---------- 5. read()/apply_update() 深拷贝 ----------

def test_read_and_apply_update_return_deep_copies_for_servers(config_sandbox):
    good = _entry("copy")
    cfg, rejected = config_store.apply_update({MCP_SERVERS_KEY: [good]})
    assert rejected == []
    # apply_update 的返回值同样是副本：改它不许污染下一次 read()。
    cfg[MCP_SERVERS_KEY][0]["args"].append("MUTATED_UPDATE")
    cfg[MCP_SERVERS_KEY][0]["env_allowlist"].append("MUTATED_UPDATE_ENV")
    cfg[MCP_SERVERS_KEY][0]["id"] = "mutated_update"
    cfg[MCP_SERVERS_KEY].append(_entry("extra_update"))

    first = config_store.read()
    first[MCP_SERVERS_KEY][0]["args"].append("MUTATED")
    first[MCP_SERVERS_KEY][0]["env_allowlist"].append("MUTATED_ENV")
    first[MCP_SERVERS_KEY][0]["id"] = "mutated"
    first[MCP_SERVERS_KEY].append(_entry("extra"))

    second = config_store.read()
    assert second[MCP_SERVERS_KEY] == [good]
    assert config_store.DEFAULTS[MCP_SERVERS_KEY] == []
    assert second[MCP_SERVERS_KEY] is not first[MCP_SERVERS_KEY]


# ---------- 6. /v1/config 对所有角色剔除两个键 ----------

def test_get_config_hides_mcp_keys_for_admin_and_user(client, enforced, config_sandbox):
    config_store.apply_update({
        MCP_ENABLED_KEY: True,
        MCP_SERVERS_KEY: [_entry("files")],
    })
    admin = client.get("/v1/config",
                       headers={"Authorization": "Bearer boot-token"}).json()
    assert MCP_SERVERS_KEY not in admin and MCP_ENABLED_KEY not in admin
    assert admin["registration_open"] is True
    # 既有可见性不受影响：管理员仍能看见 fetch 白名单，普通用户仍看不到。
    assert "fetch_allowed_hosts" in admin

    user = client.get("/v1/config", headers=enforced("mcp_reader")).json()
    assert MCP_SERVERS_KEY not in user and MCP_ENABLED_KEY not in user
    assert "fetch_allowed_hosts" not in user
    assert user["registration_open"] is True


# ---------- 7. 通用 POST 写不动 MCP 键 ----------

def test_generic_admin_config_cannot_write_mcp_keys(client, config_sandbox):
    resp = client.post("/v1/admin/config", json={
        MCP_ENABLED_KEY: True,
        MCP_SERVERS_KEY: [_entry("files")],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["rejected"] == []
    assert MCP_ENABLED_KEY not in body["config"]
    assert MCP_SERVERS_KEY not in body["config"]
    cfg = config_store.read()
    assert cfg[MCP_ENABLED_KEY] is False and cfg[MCP_SERVERS_KEY] == []
    seen = client.get("/v1/config").json()
    assert MCP_SERVERS_KEY not in seen and MCP_ENABLED_KEY not in seen


# ---------- 8. 泄漏锁：响应体与审计都不含 args 诱饵 ----------

DECOY = "DECOY-MCP-ARG-9f3a"


def test_generic_config_response_and_audit_do_not_leak_mcp_args(
        client, config_sandbox):
    seeded = _entry("files", args=["-m", "server", DECOY], enabled=True)
    _write_cfg(config_sandbox, [seeded], enabled=True)
    config_store.restore(path=str(config_sandbox))
    assert config_store.read()[MCP_SERVERS_KEY][0]["args"][-1] == DECOY

    resp = client.post("/v1/admin/config", json={"registration_open": True})
    assert resp.status_code == 200
    payload = json.dumps(resp.json(), ensure_ascii=False)
    assert DECOY not in payload, "通用配置响应回显了 MCP args"
    assert MCP_SERVERS_KEY not in payload and MCP_ENABLED_KEY not in payload

    audit_path = Path(os.environ["AUDIT_LOG_PATH"])
    lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert lines, "config.update 必须落一条审计"
    last = json.loads(lines[-1])
    assert last["action"] == "config.update", last
    recorded = json.dumps(last, ensure_ascii=False)
    assert DECOY not in recorded, "审计文件里出现了 MCP args 诱饵"
    assert MCP_SERVERS_KEY not in recorded and MCP_ENABLED_KEY not in recorded


# ---------- 9. 命令闸 ----------

def test_command_shape_rejects_relative_unc_and_control_chars():
    assert command_problem("mcp_server.exe", verify_file=False)
    assert command_problem(os.path.join("subdir", "mcp_server.exe"), verify_file=False)
    assert command_problem("\\\\server\\share\\tool.exe", verify_file=False)
    assert command_problem("//server/share/tool", verify_file=False)
    assert command_problem(os.path.join(os.path.abspath(os.sep), "tool\x00.exe"),
                           verify_file=False)
    assert command_problem("", verify_file=False)
    assert command_problem(os.path.join(os.path.abspath(os.sep), "x" * 300),
                           verify_file=False)
    assert command_problem(sys.executable, verify_file=False) is None


def test_unknown_entry_fields_reject_the_entry():
    for extra in ({"cwd": "."}, {"env": {"A": "B"}}, {"timeout": 5}):
        raw = _entry("files", **extra)
        entry, reason = normalize_entry(raw, verify_command=False)
        assert entry is None and reason


@pytest.mark.skipif(os.name != "nt", reason="Windows 可执行判据")
@pytest.mark.parametrize("name", [
    "cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe",
    "mshta.exe", "rundll32.exe", "regsvr32.exe", "bash.exe", "sh.exe",
    "wsl.exe", "conhost.exe", "explorer.exe",
])
def test_windows_host_blacklist(name):
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    assert command_problem(os.path.join(root, "System32", name), verify_file=False)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 宿主黑名单")
@pytest.mark.parametrize("name", ["sh", "bash", "dash", "zsh", "cmd",
                                  "powershell", "pwsh"])
def test_posix_host_blacklist(name):
    assert command_problem("/usr/bin/" + name, verify_file=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows 只认真实 .exe 文件")
def test_windows_requires_a_real_exe_file(tmp_path):
    exe = tmp_path / "mcp-ok.exe"
    exe.write_bytes(b"")
    assert command_problem(str(exe), verify_file=True) is None
    assert command_problem(str(exe), verify_file=False) is None

    folder = tmp_path / "dir.exe"
    folder.mkdir()
    assert command_problem(str(folder), verify_file=True)

    script = tmp_path / "tool.py"
    script.write_bytes(b"")
    assert command_problem(str(script), verify_file=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 按 X_OK 判可执行")
def test_posix_requires_the_executable_bit(tmp_path):
    tool = tmp_path / "mcp-ok"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    assert command_problem(str(tool), verify_file=True) is None

    plain = tmp_path / "plain.txt"
    plain.write_text("not executable", encoding="utf-8")
    plain.chmod(0o644)
    assert command_problem(str(plain), verify_file=True)


# ---------- 10. env 闸 ----------

@pytest.mark.parametrize("name", [
    "GITHUB_TOKEN", "DEEPSEEK_API_KEY", "HTTPS_PROXY", "PYTHONPATH",
    "github_token", "SOME_SECRET", "DYLD_INSERT_LIBRARIES",
    "API_KEY", "api_key",
])
def test_env_denylist_covers_secret_and_proxy_shapes(name):
    assert env_name_denied(name) is True


def test_env_allowlist_accepts_plain_names():
    assert env_name_denied("MCP_LOG_LEVEL") is False
    entry, reason = normalize_entry(
        _entry("files", env_allowlist=["MCP_LOG_LEVEL", "LANG"]),
        verify_command=False)
    assert entry is not None, reason
    assert entry["env_allowlist"] == ["MCP_LOG_LEVEL", "LANG"]

    entry, _ = normalize_entry(
        _entry("files", env_allowlist=["GITHUB_TOKEN"]), verify_command=False)
    assert entry is None, "被硬拒的 env 名必须让整条配置失效"


def test_env_allowlist_boundary_is_16_with_literal_counts():
    """16 个合法名通过、17 个拒：计数用字面量，别拿实现常量当预期。"""
    assert MAX_ENV_ALLOWLIST == 16
    names = [f"MCP_VAR_{index:02d}" for index in range(17)]
    assert len(names) == 17

    entry, reason = normalize_entry(
        _entry("files", env_allowlist=names[:16]), verify_command=False)
    assert entry is not None, reason
    assert len(entry["env_allowlist"]) == 16

    entry, reason = normalize_entry(
        _entry("files", env_allowlist=names), verify_command=False)
    assert entry is None and reason == "env_allowlist_too_many"


# ---------- 11. args 与整表上限 ----------

def test_args_limits_and_control_chars():
    base = {"id": "args", "command": sys.executable}
    assert normalize_entry({**base, "args": ["x"] * (MAX_ARGS + 1)},
                           verify_command=False)[0] is None
    assert normalize_entry({**base, "args": ["x" * (ARG_MAX_CHARS + 1)]},
                           verify_command=False)[0] is None
    assert normalize_entry({**base, "args": ["ok", "bad\x07"]},
                           verify_command=False)[0] is None
    assert normalize_entry({**base, "args": ["x" * 300] * MAX_ARGS},
                           verify_command=False)[0] is None, "总长超 8192 必须拒"
    assert normalize_entry({**base, "args": [123]},
                           verify_command=False)[0] is None
    ok = normalize_entry({**base, "args": ["x" * 256] * MAX_ARGS},
                         verify_command=False)[0]
    assert ok is not None and len(ok["args"]) == MAX_ARGS


def test_normalize_entry_returns_fixed_key_order():
    entry, reason = normalize_entry(_entry("files"), verify_command=False)
    assert entry is not None, reason
    assert list(entry) == ["id", "command", "args", "env_allowlist", "enabled"]


def test_servers_table_limits():
    four = [_entry(f"s{i}") for i in range(MAX_SERVERS + 1)]
    assert normalize_servers(four, verify_command=False) == ([], ["too_many_servers"])

    duplicate = [_entry("dup"), _entry("dup", args=["-v"])]
    got, errors = normalize_servers(duplicate, verify_command=False)
    assert got == [] and any("duplicate_id" in error for error in errors)

    huge = [{"id": f"s{i}", "command": sys.executable,
             "args": ["x" * 1000] * 32} for i in range(MAX_SERVERS)]
    got, errors = normalize_servers(huge, verify_command=False)
    assert got == [] and errors, "整表序列化超过 32KB 必须被拒"


def test_hard_caps_are_pinned_to_literal_boundaries():
    """条目/参数/参数长度的硬上限各钉一处字面量，避免实现常量同源恒绿。"""
    assert MAX_SERVERS == 3
    assert MAX_ARGS == 32
    assert ARG_MAX_CHARS == 1024

    base = {"id": "cap", "command": sys.executable}
    entry, reason = normalize_entry({**base, "args": ["x"] * 32},
                                    verify_command=False)
    assert entry is not None, reason
    entry, reason = normalize_entry({**base, "args": ["x"] * 33},
                                    verify_command=False)
    assert entry is None and reason == "args_too_many"

    entry, reason = normalize_entry({**base, "args": ["x" * 1024]},
                                    verify_command=False)
    assert entry is not None, reason
    entry, reason = normalize_entry({**base, "args": ["x" * 1025]},
                                    verify_command=False)
    assert entry is None and reason == "arg_too_long"

    three = [{"id": f"cap{i}", "command": sys.executable} for i in range(3)]
    got, errors = normalize_servers(three, verify_command=False)
    assert errors == [] and len(got) == 3
    four = [{"id": f"cap{i}", "command": sys.executable} for i in range(4)]
    got, errors = normalize_servers(four, verify_command=False)
    assert got == [] and errors == ["too_many_servers"]
