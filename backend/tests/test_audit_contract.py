"""v0.24 T3.4 契约（D14 拆出的 audit.jsonl）：管理端每一次写都留一条改不得的证据。

判据分四层：
1. 只读路由带 require_admin，且**没有任何删除路由**——能从界面删掉的证据不是证据。
2. 每一次管理端写操作（建号/停用/启用/换发/重置/删号/配置/Provider 增删改默认）都
   往这份只增账里贴进对应的一条 action。
3. 敏感值（口令/令牌/密钥/initial_password）绝不落进盘里那行——审计证明"动过"，
   不需要那格的内容。
4. 写盘炸了不许把被审计的动作一起带崩；坏行读到时标记出来、不假装解析成功。
"""
import json

import pytest
from fastapi.routing import APIRoute

from app.core import audit
from app.core.authz import require_admin
from app.main import app

USERS = "/v1/admin/users"


@pytest.fixture(autouse=True)
def audit_file(tmp_path, monkeypatch):
    """每条用例一份独立的账：conftest 已把 AUDIT_LOG_PATH 指进共享临时目录，
    这里再各指各的，测试之间不串账。"""
    p = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(p))
    yield p


def _actions():
    return [e["action"] for e in audit.read()]


def _all_dependencies(dependant):
    yield dependant
    for sub in dependant.dependencies:
        yield from _all_dependencies(sub)


# ---------- 1. 路由守卫 ----------


def test_audit_read_route_declares_admin_dependency():
    route = next(r for r in app.routes
                 if isinstance(r, APIRoute) and r.path == "/v1/admin/audit")
    tree = [d.call for d in _all_dependencies(route.dependant)]
    assert require_admin in tree, "/v1/admin/audit 没挂管理员守卫"


def test_there_is_no_way_to_delete_the_audit_log():
    """账本只增：不存在能把 /v1/admin/audit 那行删掉的动词。"""
    verbs = {tuple(sorted(r.methods)) for r in app.routes
             if isinstance(r, APIRoute) and r.path == "/v1/admin/audit"}
    assert verbs == {("GET",)}, f"审计面出现了 GET 之外的动词：{verbs}"


def test_read_rejects_a_mere_user(client, enforced):
    hdr = enforced("看热闹的人")
    r = client.get("/v1/admin/audit", headers=hdr)
    assert r.status_code == 403 and r.json()["detail"] == "需要管理员权限"


# ---------- 2. 每次写操作都进账 ----------


def _create(client, username):
    """按主干（fenver T3.1）的形状建号：口令由管理员带在请求体里，响应是公开展望。"""
    return client.post(USERS, json={"username": username,
                                    "password": "Zz9-账面上的口令"}).json()


def test_account_writes_all_land_in_the_ledger(client):
    created = _create(client, "账上要有人")
    uid = created["user_id"]
    client.post(USERS + "/" + uid + "/disable")
    client.post(USERS + "/" + uid + "/enable")
    client.post(USERS + "/" + uid + "/rotate-token")
    client.post(USERS + "/" + uid + "/reset-password",
                 json={"new_password": "Zz9-换过一次的口令"})
    client.post("/v1/admin/config", json={"registration_open": False})
    client.delete(USERS + "/" + uid)

    acts = _actions()
    for want in ("user.create", "user.disable", "user.enable", "user.rotate-token",
                 "user.reset-password", "config.update", "user.delete"):
        assert want in acts, f"少了 {want}：{acts}"


def test_provider_writes_land_in_the_ledger(client):
    p = client.post("/v1/providers", json={
        "label": "账上模型", "base_url": "https://x.invalid/v1",
        "api_key": "sk-abcdef123456", "model": "m"}).json()["provider"]
    pid = p["id"]
    client.put("/v1/providers/" + pid, json={
        "label": "改过名", "base_url": "https://x.invalid/v1",
        "api_key": "sk-abcdef123456", "model": "m"})
    client.post(f"/v1/providers/{pid}/default")
    client.delete("/v1/providers/" + pid)

    acts = _actions()
    for want in ("provider.create", "provider.update",
                 "provider.set-default", "provider.delete"):
        assert want in acts, f"少了 {want}：{acts}"


def test_entries_carry_who_and_when(client):
    _create(client, "带元数据的人")
    e = audit.read()[-1]
    assert e["action"] == "user.create"
    assert e["ts"].endswith("+00:00"), f"时间戳不是带时区的 UTC：{e['ts']}"
    assert e["actor_role"] == "admin" and e["actor_id"], \
        f"审计没落「是谁」：{e}"


# ---------- 3. 秘密不落盘 ----------


def test_passwords_and_keys_never_reach_the_disk(audit_file, client):
    """initial_password / api_key 这类值，即便出现在 after 里也必须被擦成 ***。"""
    audit.log({"user_id": "u_a", "username": "admin", "role": "admin"}, "x.probe",
              after={"api_key": "sk-live-abcdef1234",
                     "initial_password": "Zz9-abcdefghij",
                     "password": "hunter2", "label": "看得见"})
    raw = audit_file.read_text(encoding="utf-8")
    for secret in ("sk-live-abcdef1234", "Zz9-abcdefghij", "hunter2"):
        assert secret not in raw, f"审计把明文 {secret[:4]}… 写进了盘"
    assert "***" in raw and "看得见" in raw


def test_create_user_password_never_leaks_into_the_ledger(audit_file, client):
    """建号口令由管理员带在请求体里（主干 T3.1）——那它就更不能顺着审计漏进账本。

    判据写在"文件里查不到那串字节"这一层，不写在"after 里没这个键"那一层：
    前者才挡得住有人在路由上顺手把请求体整个塞进 after。
    """
    pw = "Zz9-绝不上盘的一次性口令"
    r = client.post(USERS, json={"username": "别漏他密码", "password": pw})
    assert r.status_code == 200, r.text
    raw = audit_file.read_text(encoding="utf-8")
    assert pw not in raw, "审计落盘把管理员设的口令原样抄了进去"
    assert "user.create" in raw, "口令没上盘是因为压根没记这条——那也不行"


# ---------- 4. 写失败不带崩动作 / 坏行可读 ----------


def test_write_failure_does_not_raise_and_still_returns(audit_file, capsys, monkeypatch):
    """磁盘炸了不能把"保存成功"变成 500：账没记上是可容忍的降级，操作崩了不是。"""
    def boom(path):
        raise OSError("disk full")
    monkeypatch.setattr(audit, "ensure_parent", boom)
    rec = audit.log({"user_id": "u", "username": "admin", "role": "admin"},
                    "x.should_not_break_caller")
    assert rec["action"] == "x.should_not_break_caller"   # 返回记录，调用方照常成功
    assert "没能落盘" in capsys.readouterr().err


def test_read_marks_a_corrupt_tail_without_losing_the_rest(audit_file):
    good = {"action": "user.create", "ts": "2026-09-30T00:00:00+00:00"}
    audit_file.write_text(json.dumps(good, ensure_ascii=False) + "\n"
                          + "{ 坏掉的一行\n", encoding="utf-8")
    rows = audit.read()
    assert any(r["action"] == "user.create" for r in rows)
    assert any(r["action"] == "_corrupt_line" for r in rows)
