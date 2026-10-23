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


# ---------- 5. 键名之外：完整记录与自由文本里的凭据 ----------
#
# 上面的第 3 节只锁住了「键名撞上名单」这一条路。调用方把完整用户记录递进来时，
# device_secrets / security_answers 这些格子不在旧名单里；detail 与 before/after
# 的字符串更是只截断不过滤。这一节把这两处缺口钉住，并且都是判「盘上查不到那串
# 字节」，不判「返回值里没这个键」——前者才挡得住有人顺手把请求体塞进 after。

_DEVICE_AND_RECOVERY_FIELDS = (
    "device_secret", "device_secrets", "security_answer", "security_answers",
    "recovery_code", "backup_code", "private_key", "session_token",
)


def test_stats_reports_entries_and_last_ts_without_trusting_corrupt_tail(audit_file):
    """管理台那张「审计文件」卡读 entries/last_ts，后端必须真的给出这两个字段。

    坏行计入物理行数与 entries（文件体量口径），但 last_ts 只取最后一条可解析记录：
    被截断的尾巴不能假装成「最近写入」。
    """
    audit.log({"user_id": "u_a", "username": "admin", "role": "admin"}, "x.one")
    audit.log({"user_id": "u_a", "username": "admin", "role": "admin"}, "x.two")
    with audit_file.open("a", encoding="utf-8") as f:
        f.write("{not json}\n")
    got = audit.stats()
    lines = audit_file.read_text(encoding="utf-8").splitlines()
    assert got["size_bytes"] == audit_file.stat().st_size
    assert got["lines"] == 3 and got["entries"] == 3
    assert got["last_ts"] == json.loads(lines[1])["ts"]


def test_stats_is_zero_when_the_ledger_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "missing.jsonl"))
    assert audit.stats() == {"size_bytes": 0, "lines": 0,
                             "entries": 0, "last_ts": None}


def _probe(audit_file, action="x.probe", **kw):
    """贴一条探针再把盘上原文读回来：判据只认落盘的字节。"""
    audit.log({"user_id": "u_a", "username": "admin", "role": "admin"},
              action, **kw)
    return audit_file.read_text(encoding="utf-8")


def test_secret_key_names_cover_device_and_recovery_fields():
    missing = [k for k in _DEVICE_AND_RECOVERY_FIELDS
               if k not in audit._SECRET_KEYS]
    assert not missing, f"这些键名还没进脱敏名单，值会原样落盘：{missing}"


def test_a_full_user_record_lands_as_stars_not_values(audit_file):
    """整条用户记录原样递进 after：敏感格一个值都不许出现在盘上。"""
    record = {
        "user_id": "u_device_1",
        "username": "带了两台设备的人",
        "device_secrets": ["DEV-SECRET-AAAABBBB", "DEV-SECRET-CCCCDDDD"],
        "device_secret": "DEV-SECRET-EEEEFFFF",
        "security_answers": {"pet_name": "小蓝", "city": "西安"},
        "security_answer": "小蓝",
        "recovery_code": "RECOVERY-1234-5678",
        "backup_code": "BACKUP-8765-4321",
        "private_key": "PRIVATEKEYMATERIAL-must-not-land",
        "session_token": "SESSIONTOKEN-must-not-land",
    }
    raw = _probe(audit_file, action="user.create", after=record)
    for value in ("DEV-SECRET-AAAABBBB", "DEV-SECRET-CCCCDDDD",
                  "DEV-SECRET-EEEEFFFF", "小蓝", "西安",
                  "RECOVERY-1234-5678", "BACKUP-8765-4321",
                  "PRIVATEKEYMATERIAL-must-not-land",
                  "SESSIONTOKEN-must-not-land"):
        assert value not in raw, f"完整用户记录里的 {value[:8]}… 原样落了盘"
    assert raw.count("***") >= len(_DEVICE_AND_RECOVERY_FIELDS), \
        f"敏感格没有逐格变成 ***：{raw}"
    assert "带了两台设备的人" in raw, "脱敏把非敏感字段也吃掉了"


def test_bearer_token_in_detail_is_hidden_but_the_label_stays(audit_file):
    raw = _probe(audit_file, detail="上游 401：Bearer abcdefghijklmnop 已过期")
    assert "abcdefghijklmnop" not in raw, "detail 里的 Bearer token 原样落盘"
    assert "Bearer" in raw and "***" in raw, \
        f"凭据被隐了，但标签/遮蔽记号也一起没了：{raw}"


def test_openai_style_key_in_detail_is_hidden(audit_file):
    raw = _probe(audit_file, detail="调用失败：sk-live-abcdef123456")
    assert "abcdef123456" not in raw, "detail 里的 sk- 密钥原样落盘"
    assert "***" in raw
    assert "调用失败" in raw, "脱敏只该吃凭据形状，不该吃文案"


def test_labeled_secret_keeps_the_label_and_hides_the_value(audit_file):
    # 拼出来而不是写成一行字面量：整行的 `api_key=<16 位不透明串>` 会被仓库自带的
    # secret_scan 当成真凭据（opaque-assignment）。拼起来喂给 log 的仍是同一个串。
    detail = "请求体 " + "api_key=" + "supersecret12345"
    raw = _probe(audit_file, detail=detail)
    assert "supersecret12345" not in raw, "带标签的 api_key 值原样落盘"
    assert "api_key" in raw, "把标签也隐掉等于把线索一起抹了"
    assert "***" in raw


def test_snake_case_event_names_survive_scrubbing(audit_file):
    """反向锁：谁把这里换成 providers.scrub_secrets 那一套，这条就会红——那套的
    第一条正则会连 auth_login_failed 整段吃掉，审计就再也认不出发生过什么。"""
    raw = _probe(audit_file, detail="auth_login_failed")
    assert "auth_login_failed" in raw, "脱敏器把下划线事件名当成密钥吃掉了"
    assert "***" not in raw


def test_nested_bearer_tokens_in_before_and_after_are_hidden(audit_file):
    before = {"headers": [{"name": "Authorization",
                           "value": "Bearer abcdefghijklmnop"}]}
    after = {"outer": {"inner": ["ok", "回包 Bearer zyxwvutsrqponmlk"]}}
    raw = _probe(audit_file, before=before, after=after)
    assert "abcdefghijklmnop" not in raw, "嵌套 list/dict 里的 Bearer token 漏了"
    assert "zyxwvutsrqponmlk" not in raw, "嵌套 list/dict 里的 Bearer token 漏了"
    assert raw.count("Bearer") == 2, f"标签被一起吃掉了：{raw}"
    assert "ok" in raw, "脱敏把非敏感值也吃掉了"


def test_commit_shas_and_digests_survive_scrubbing(audit_file):
    """不认裸十六进制：40 位 SHA 与摘要会进审计，它们是证据，不能被当密码隐掉。"""
    sha = "82318778deadbeef" + "0" * 24
    digest = "a1b2c3d4" * 8
    raw = _probe(audit_file, detail=f"prev={sha} digest={digest}")
    assert sha in raw and digest in raw, "脱敏把 commit SHA / 摘要吃掉了"
    assert "***" not in raw


def test_text_scrubber_actually_has_teeth(audit_file, monkeypatch):
    """负控：同一条「盘上查不到」判据，拆掉 _scrub_text 之后必须当场报红。

    只跑正常路径等于照镜子——断言永远绿，因为你从没见过它红。这里先跑绿，再把
    _scrub_text 换成恒等函数，要求同一条断言抛 AssertionError（monkeypatch 在用例
    结束时自动还原）。
    """
    def check():
        audit_file.write_text("", encoding="utf-8")
        raw = _probe(audit_file, detail="Authorization: Bearer abcdefghijklmnop")
        assert "abcdefghijklmnop" not in raw, "Bearer token 原样落盘"

    check()                                   # 正常实现：红不了
    monkeypatch.setattr(audit, "_scrub_text", lambda s: s)
    with pytest.raises(AssertionError, match="Bearer token 原样落盘"):
        check()                               # 拆掉脱敏器：必须红


def test_key_blacklist_actually_has_teeth(audit_file, monkeypatch):
    """键名名单的负控：把 device_secrets 从名单里摘掉，同一判据必须报红。"""
    def check():
        audit_file.write_text("", encoding="utf-8")
        raw = _probe(audit_file, after={"device_secrets": ["DEV-SECRET-AAAABBBB"]})
        assert "DEV-SECRET-AAAABBBB" not in raw, "device_secrets 的值原样落盘"

    check()                                   # 名单在：红不了
    monkeypatch.setattr(audit, "_SECRET_KEYS",
                        audit._SECRET_KEYS - {"device_secrets"})
    with pytest.raises(AssertionError, match="device_secrets 的值原样落盘"):
        check()                               # 名单被摘：必须红
