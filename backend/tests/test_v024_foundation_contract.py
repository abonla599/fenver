"""v0.24 阶段一（T1.1/T1.2/T1.3/T1.4）契约测试。

四条主线，各自钉住一条会静默复发的旧病：
- T1.1：任务存储重启不丢、恢复带"被重启打断"标注、老记录缺新字段按默认容忍；
- T1.2：八家可变数据存储全部走同一个原子落盘函数——谁手写回 tmp+replace 谁红；
- T1.3：status 停用字段生效、没有 status 的旧记录照常认 disabled、管理员禁不了自己人；
- T1.4：注册默认关（闸门在限流账之前）、配置读写鉴权分明、重启后值还在、
  env 压过文件、发布探针"新仓在前、旧仓兜底"。

端点鉴权契约（GET /v1/config 要身份、POST /v1/admin/config 要管理员）按拆解清单
写在实现交付前——同一个文件里，先于绿灯提交。
"""
import json
import os
import urllib.error
from pathlib import Path

import pytest

from app.agents import task_store as ts_mod
from app.agents.task_store import Task, TaskStatus, task_store
from app.core.atomic_write import write_json_atomic
from app.core.auth import AuthError, AuthStore
from tests.conftest import RECOVERY_FIELDS as RECOVERY

_BACKEND_ROOT = Path(__file__).resolve().parents[1]

# 八家收编名单：与《待修复清单-小模型》批次 1 逐字对应，多一家少一家都红。
ATOMIC_CALLERS = [
    "app/session/session_store.py",
    "app/feedback_storage.py",
    "app/core/uploads.py",
    "app/core/providers.py",
    "app/core/usage.py",
    "app/core/schedule.py",
    "app/agents/task_store.py",
    "app/core/auth.py",
]


# ---------- T1.2 ----------

def test_the_shared_writer_is_actually_atomic(tmp_path):
    """公共函数自己得先是对的：同目录 .tmp、fsync 在 replace 之前。"""
    import inspect
    src = inspect.getsource(write_json_atomic)
    assert ".tmp" in src and "os.fsync" in src and "os.replace" in src
    target = tmp_path / "nested" / "x.json"
    write_json_atomic(str(target), {"ok": "在"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": "在"}
    assert not list(tmp_path.rglob("*.tmp")), "写完不该留中间文件"


def test_all_eight_stores_route_through_the_shared_writer():
    """八家存储逐一验：源码里必须调用公共函数，而不是各留一份手写替换。"""
    for rel in ATOMIC_CALLERS:
        src = (_BACKEND_ROOT / rel).read_text(encoding="utf-8")
        assert "write_json_atomic(" in src, f"{rel} 没有走公共原子落盘"
        assert "import write_json_atomic" in src or "atomic_write import" in src, \
            f"{rel} 调用了公共函数却没 import 它"


# ---------- T1.1 ----------

@pytest.fixture
def isolated_tasks(tmp_path, monkeypatch):
    db = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(db))
    task_store.clear()
    yield db
    task_store.clear()


def _mk(goal="写一首诗", status=TaskStatus.PENDING) -> Task:
    t = Task(goal=goal, subtasks=["起稿"], user_id="u_alice")
    t.status = status
    return t


def test_putting_a_task_in_the_store_hits_disk(isolated_tasks):
    t = _mk()
    task_store[t.task_id] = t
    on_disk = json.loads(isolated_tasks.read_text(encoding="utf-8"))
    assert t.task_id in on_disk["tasks"], "放进存储没落盘：重启就静悄悄少了这条"


def test_restore_bring_tasks_back_with_their_ids_and_owners(isolated_tasks):
    t = _mk(goal="明早七点半叫我")
    task_store[t.task_id] = t
    task_store.clear()                      # 等价于进程死掉
    n = ts_mod.restore()
    assert n == 1
    back = task_store[t.task_id]
    assert back.user_id == "u_alice" and back.goal == "明早七点半叫我"


def test_a_task_that_was_running_comes_back_flagged_not_resumed(isolated_tasks):
    """进行中的任务恢复后必须带 interrupted_by_restart 标注，且标注要写回盘。"""
    t = _mk(status=TaskStatus.RUNNING)
    task_store[t.task_id] = t
    task_store.clear()
    ts_mod.restore()
    back = task_store[t.task_id]
    assert back.interrupted_by_restart is True
    again = json.loads(isolated_tasks.read_text(encoding="utf-8"))
    assert again["tasks"][t.task_id]["interrupted_by_restart"] is True, \
        "标注只活在内存里：下次重启又会冒出一批假装还在跑的僵尸"


def test_finished_tasks_come_back_clean(isolated_tasks):
    t = _mk(status=TaskStatus.COMPLETED)
    task_store[t.task_id] = t
    task_store.clear()
    ts_mod.restore()
    assert task_store[t.task_id].interrupted_by_restart is False


def test_old_records_without_the_new_fields_still_load(isolated_tasks):
    """旧任务 JSON 缺 cancelled/interrupted 字段：按默认值容忍，不整库拒读。"""
    isolated_tasks.parent.mkdir(parents=True, exist_ok=True)
    bare = {"tasks": {"t_old": {"task_id": "t_old", "user_id": "u_bob",
                                "goal": "老任务", "status": "completed"}}}
    isolated_tasks.write_text(json.dumps(bare, ensure_ascii=False), encoding="utf-8")
    assert ts_mod.restore() == 1
    t = task_store["t_old"]
    assert t.cancelled is False and t.interrupted_by_restart is False


# ---------- T1.3 ----------

@pytest.fixture
def isolated_users(tmp_path, monkeypatch):
    db = tmp_path / "users.json"
    monkeypatch.setenv("USERS_DB_PATH", str(db))
    return AuthStore(path=str(db))


def test_new_account_carries_the_v024_fields(isolated_users):
    principal, _tok = isolated_users.register("阿测", "abcdefg123")
    rec = next(u for u in isolated_users.list_users() if u["user_id"] == principal.user_id)
    assert rec["status"] == "active"
    assert rec["must_change_password"] is False


def test_disabled_status_blocks_login_and_resolve(isolated_users):
    principal, _tok = isolated_users.register("阿停", "abcdefg123")
    assert isolated_users.disable_user(principal.user_id) is True
    with pytest.raises(AuthError):
        isolated_users.login("阿停", "abcdefg123")


def test_legacy_record_without_status_field_is_still_honored(tmp_path, monkeypatch):
    """旧库只有 disabled 布尔、没有 status：读侧照样认，登录照样拒。"""
    db = tmp_path / "users.json"
    monkeypatch.setenv("USERS_DB_PATH", str(db))
    s = AuthStore(path=str(db))
    principal, _tok = s.register("旧格式", "abcdefg123")
    raw = json.loads(db.read_text(encoding="utf-8"))
    rec = raw[principal.user_id]
    rec.pop("status", None)                 # 模拟 v0.23 及以前的记录
    rec["disabled"] = True
    db.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    again = AuthStore(path=str(db))
    with pytest.raises(AuthError):
        again.login("旧格式", "abcdefg123")


def test_record_missing_both_fields_reads_as_active(tmp_path, monkeypatch):
    """status 与 disabled 都没有的远古记录：按 active 兼容，不许误拒真人。"""
    db = tmp_path / "users.json"
    monkeypatch.setenv("USERS_DB_PATH", str(db))
    s = AuthStore(path=str(db))
    principal, _tok = s.register("远古号", "abcdefg123")
    raw = json.loads(db.read_text(encoding="utf-8"))
    raw[principal.user_id].pop("status", None)
    raw[principal.user_id].pop("disabled", None)
    db.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    again = AuthStore(path=str(db))
    _p, tok = again.login("远古号", "abcdefg123")
    assert tok


def test_admin_cannot_be_disabled_at_the_service_layer(isolated_users):
    """R2 验收前置：管理员账号在服务层就禁不掉，路由怎么写都绕不过来。"""
    principal, _tok = isolated_users.register("管理员甲", "abcdefg123")
    isolated_users._users[principal.user_id]["role"] = "admin"
    isolated_users._flush()
    assert isolated_users.disable_user(principal.user_id) is False
    assert isolated_users.list_users()[0].get("status") != "disabled"


def test_must_change_password_round_trip(isolated_users):
    principal, _tok = isolated_users.register("待改密", "abcdefg123")
    assert isolated_users.must_change_password(principal.user_id) is False
    assert isolated_users.set_must_change_password(principal.user_id, True) is True
    again = AuthStore(path=str(isolated_users.path))   # 新进程读盘
    assert again.must_change_password(principal.user_id) is True


# ---------- T1.4 ----------

@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """把 data/config.json 挪进临时目录并还原进程内那份。

    conftest 全局钉了 REGISTRATION_OPEN=1（既有注册用例都从"注册可用"出发），
    这里必须把它摘掉才测得出"文件说了算"的默认关闭。收尾连内存配置一起还原，
    不然一条用例改的 registration_open 会留给全场。
    """
    from app.core import config_store as cs

    prev = dict(cs._config)
    db = tmp_path / "config.json"
    monkeypatch.setenv("CONFIG_DB_PATH", str(db))
    monkeypatch.delenv("REGISTRATION_OPEN", raising=False)
    cs.restore(str(db))
    try:
        yield db
    finally:
        cs._config.clear()
        cs._config.update(prev)


def test_registration_is_closed_until_someone_opens_it(isolated_config, client):
    """默认关闭就是 403 一句实话（文件在不在都一样——默认值就是关）。"""
    res = client.post("/v1/auth/register",
                      json={"username": "路人甲", "password": "abcdefg123",
                            **RECOVERY})
    assert res.status_code == 403
    assert "注册当前关闭" in res.json()["detail"]


def test_the_gate_runs_before_the_failure_ledger(isolated_config, client):
    """关着的门口不收预算：连吃 12 次 403 之后，重新开放注册的第一个人不该莫名其妙 429。"""
    for _ in range(12):
        assert client.post("/v1/auth/register",
                           json={"username": "路人乙", "password": "abcdefg123",
                                 **RECOVERY}).status_code == 403
    from app.core import config_store as cs
    cs.apply_update({"registration_open": True})
    res = client.post("/v1/auth/register",
                      json={"username": "路人乙", "password": "abcdefg123", **RECOVERY})
    assert res.status_code == 200, "闸门把限流账当了挡箭牌——顺序写反了就会这样"


def test_config_writes_survive_a_restart(isolated_config):
    from app.core import config_store as cs
    cfg, rejected = cs.apply_update({"update_repo": "someone/else"})
    assert rejected == [] and cfg["update_repo"] == "someone/else"
    assert isolated_config.exists(), "apply_update 没落盘：重启后改动就蒸发"
    cs._config.clear()
    cs.restore(str(isolated_config))
    assert cs.read()["update_repo"] == "someone/else"


def test_unknown_and_wrong_typed_keys_are_rejected(isolated_config):
    from app.core import config_store as cs
    cfg, rejected = cs.apply_update({"session_secret": "x", "registration_open": "yes"})
    assert set(rejected) == {"session_secret", "registration_open"}
    assert cfg["registration_open"] is False, "被拒的键不能顺手把值也改了"


def test_env_flag_outranks_the_file(isolated_config, monkeypatch):
    """REGISTRATION_OPEN 显式设了就压过文件——部署入口，也保证文件误删时默认不翻。"""
    from app.core import config_store as cs
    cs.apply_update({"registration_open": True})
    monkeypatch.setenv("REGISTRATION_OPEN", "0")
    assert cs.registration_open() is False
    monkeypatch.setenv("REGISTRATION_OPEN", "1")
    assert cs.registration_open() is True


def test_get_config_wants_a_login_and_write_wants_an_admin(isolated_config, client, enforced):
    """鉴权契约（先于实现交付的那两条）：401/403 分明，管理面不向下兼容"只是登录"。"""
    from app.core import authz, config_store as cs
    assert client.get("/v1/config").status_code == 401, "enforced 下匿名就能看配置"

    headers = enforced("配置读者")
    res = client.get("/v1/config", headers=headers)
    assert res.status_code == 200
    assert res.json()["registration_open"] is False

    res = client.post("/v1/admin/config", json={"registration_open": True}, headers=headers)
    assert res.status_code == 403, "普通用户改得动全局注册开关"

    rec = list(authz.auth_store._users.values())[0]
    rec["role"] = "admin"
    authz.auth_store._flush()
    res = client.post("/v1/admin/config", json={"registration_open": True}, headers=headers)
    assert res.status_code == 200
    assert res.json()["config"]["registration_open"] is True
    assert cs.registration_open() is True


class _Resp:
    def __init__(self, data):
        self._data = data

    def read(self, max_bytes):
        return self._data[:max_bytes]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_the_probe_asks_the_configured_repo_first_and_legacy_falls_behind(
        isolated_config, monkeypatch):
    """T1.4 的更新链承诺：默认问新仓（fenver），新仓没发版时旧仓兜底——
    v0.23.x 的老用户不会因为改名收不到更新。"""
    from app.core import releases

    good = {"tag_name": "v0.23.15",
            "html_url": "https://github.com/abonla599/ai-assistant/releases/tag/v0.23.15",
            "assets": []}
    requested = []

    def fake_urlopen(request, timeout=None, context=None):
        url = request.full_url
        requested.append(url)
        if "fenver" in url:                    # 新仓：还没发版，404
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _Resp(json.dumps(good).encode("utf-8"))

    releases.reset_for_tests()
    monkeypatch.setattr(releases.urllib.request, "urlopen", fake_urlopen)
    out = releases.probe(have="0.20")
    assert out["ok"] is True and out["latest"] == "0.23.15", "旧仓兜底没生效"
    assert "abonla599/fenver" in requested[0], "第一趟问的不是配置里的新仓"
    assert "abonla599/ai-assistant" in requested[1]
    # 发布页跳转跟着"这份货来自哪个仓"走，而不是跟着配置走——配置可以被人改，
    # 手里快照的出处不能事后追认。
    assert releases.releases_page() == \
        "https://github.com/abonla599/ai-assistant/releases/latest"
    releases.reset_for_tests()


def test_config_is_listed_in_the_data_locations_table():
    """启动日志那张"可变数据都在哪"的表必须带上 config——漏一家，运维就找不到它。"""
    from app.core.config_store import _default_path
    from app.core.paths import DATA_PATH_ENV_VARS, resolve_all_data_paths

    assert DATA_PATH_ENV_VARS.get("运行时配置") == "CONFIG_DB_PATH"
    rows = dict(resolve_all_data_paths())
    assert os.path.abspath(_default_path()) == os.path.abspath(rows["运行时配置"])
