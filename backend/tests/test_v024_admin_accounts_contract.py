"""v0.24 阶段3（T3.1）管理端账号 API 契约测试——先于实现交付。

钉住四件事，每一件事都对应拆解清单里一条会静默复发的边界：
- 管理员建号：初始口令由管理员设定，新账号**必须首登改密**（must_change_password），
  且建号响应里绝不能出现口令摘要、令牌或找回答案（沿用 _public_user 白名单纪律）；
  role 依旧不在任何请求体里——能建出的一定是普通用户。
- 管理员重置口令：该人名下全部会话即刻作废（管理员替他换口令，不该留旧设备在线），
  并且同样置 must_change_password——"重置"不等于"我替他记住了新口令"。
- 停用三连边界：停用自己→400；停用一个管理员（含最后一个管理员）→400 且说得出理由
  （存储层的"管理员禁不掉"是 R2 决策，比 PRD 的"最后一个管理员"更严，路由不许把它
  糊成 404"用户不存在"——那会让人在排查时先去找一个根本没死的进程式的笑话）；
  在线的人被停用→下一个请求 401，启用后可恢复。
- 注册开关的读写面（GET /v1/config + POST /v1/admin/config）已由 T1.4 在本仓库
  钉住（见 test_v024_foundation_contract.py），这里不重复钉，只补管理端点清单：
  新增的 POST /v1/admin/users 与 reset-password 一并进
  test_auth_endpoints.py 的 require_admin 点名表。

用户首登改密（POST /v1/auth/change-password）按"出示当前身份+原口令"走：
换密成功只作废**除调用方这一枚之外**的会话——与三题自助 reset 的"全端掉线"刻意不同，
因为改密的动机通常是"我要换掉口令"而不是"我的口令可能泄露了"。
"""
import pytest

from app.core.auth import AuthError, AuthStore

PW = "correct-horse-battery"
PW2 = "another-horse-battery-9"
ANS = ["新市场小学", "hehai2024", "李建国"]


@pytest.fixture
def isolated_users(tmp_path, monkeypatch):
    db = tmp_path / "users.json"
    monkeypatch.setenv("USERS_DB_PATH", str(db))
    return AuthStore(path=str(db))


# ---------- 存储层：admin_create_user ----------

def test_admin_created_account_carries_the_change_flag_and_no_sessions(isolated_users):
    rec = isolated_users.admin_create_user("新建号", PW)
    assert rec["must_change_password"] is True
    assert rec["tokens"] == [], "管理员建号不该顺手发会话——建号的人没有登录资格"
    assert rec["role"] == "user", "建号端点没有 role 参数：能建出的只能是普通用户"
    assert rec["status"] == "active"
    _principal, tok = isolated_users.login("新建号", PW)
    assert isolated_users.must_change_password(rec["user_id"]) is True
    assert tok


def test_admin_create_rejects_duplicate_name_and_weak_password(isolated_users):
    isolated_users.admin_create_user("撞名的", PW)
    with pytest.raises(AuthError) as dup:
        isolated_users.admin_create_user("撞名的", PW)
    assert dup.value.taken is True
    with pytest.raises(AuthError):
        isolated_users.admin_create_user("口令太短", "abc")


def test_admin_create_accepts_optional_recovery_answers(isolated_users):
    rec = isolated_users.admin_create_user("带答案的", PW, security_answers=ANS)
    assert "answer_hashes" in rec
    bare = isolated_users.admin_create_user("不带答案的", PW)
    assert "answer_hashes" not in bare, "没给就不该造半套凭据"


# ---------- 存储层：admin_reset_password ----------

def test_admin_reset_revokes_every_session_and_sets_the_flag(isolated_users):
    principal, tok_a = isolated_users.register("被重置的人", PW, security_answers=ANS)
    _p2, tok_b = isolated_users.login("被重置的人", PW)
    assert isolated_users.admin_reset_password(principal.user_id, PW2) is True
    assert isolated_users.resolve(tok_a) is None, "旧设备必须立刻掉线"
    assert isolated_users.resolve(tok_b) is None
    with pytest.raises(AuthError):
        isolated_users.login("被重置的人", PW)
    _p3, tok_c = isolated_users.login("被重置的人", PW2)
    assert isolated_users.must_change_password(principal.user_id) is True
    assert isolated_users.resolve(tok_c) is not None


def test_admin_reset_unknown_user_is_false(isolated_users):
    assert isolated_users.admin_reset_password("u_ghost", PW2) is False
    with pytest.raises(AuthError):
        isolated_users.admin_reset_password("u_ghost", "abc")


# ---------- 存储层：本人首登改密 ----------

def test_change_password_clears_the_flag_and_keeps_the_callers_session(isolated_users):
    rec = isolated_users.admin_create_user("待改密的人", PW)
    _p, login_tok = isolated_users.login("待改密的人", PW)
    _p2, other_tok = isolated_users.login("待改密的人", PW)
    isolated_users.change_password(rec["user_id"], PW, PW2, keep_token=login_tok)
    assert isolated_users.must_change_password(rec["user_id"]) is False
    assert isolated_users.resolve(login_tok) is not None, "改密的这台设备不该被踢下线"
    assert isolated_users.resolve(other_tok) is None, "另一枚会话应随之作废"
    with pytest.raises(AuthError):
        isolated_users.login("待改密的人", PW)


def test_change_password_refuses_a_wrong_current_one(isolated_users):
    rec = isolated_users.admin_create_user("口令记错的", PW)
    with pytest.raises(AuthError) as e:
        isolated_users.change_password(rec["user_id"], "根本不是这个", PW2)
    assert "原口令" in str(e.value) or "不正确" in str(e.value)


def test_change_password_rejects_a_new_password_that_is_too_weak(isolated_users):
    rec = isolated_users.admin_create_user("改坏了的", PW)
    with pytest.raises(AuthError):
        isolated_users.change_password(rec["user_id"], PW, "abc")


# ---------- HTTP 面（disabled：人人都是本机管理员） ----------

def test_admin_can_create_an_account_over_http(client):
    res = client.post("/v1/admin/users", json={"username": "小赵", "password": PW})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["role"] == "user" and body["must_change_password"] is True
    assert body["sessions"] == 0
    assert PW not in res.text and "pw_hash" not in res.text


def test_created_account_logins_and_is_told_to_change_its_password(client):
    res = client.post("/v1/admin/users", json={"username": "首登的人", "password": PW})
    login = client.post("/v1/auth/login", json={"username": "首登的人", "password": PW})
    assert login.status_code == 200
    assert login.json()["must_change_password"] is True


def test_http_create_edge_codes(client):
    ok = client.post("/v1/admin/users", json={"username": "占位的", "password": PW})
    assert ok.status_code == 200
    dup = client.post("/v1/admin/users", json={"username": "占位的", "password": PW2})
    assert dup.status_code == 409, "撞名在管理端也是实话"
    weak = client.post("/v1/admin/users", json={"username": "口令短的", "password": "abc"})
    assert weak.status_code == 422


def test_http_create_wants_an_admin(client, enforced):
    hdrs = enforced("普通用户甲")
    assert client.post("/v1/admin/users", json={"username": "塞不进来", "password": PW},
                       headers=hdrs).status_code == 403
    assert client.post("/v1/admin/users",
                       json={"username": "匿名塞", "password": PW}).status_code == 401


def test_admin_reset_password_over_http_kicks_everyone_off(client, enforced):
    """必须走 enforced：disabled 模式下人人都是引导管理员，旧令牌照样解得出 200，
    "被踢下线"这件事根本测不到。"""
    hdrs_admin = enforced("执法管理员")
    from app.core import authz
    admin_id = client.get("/v1/auth/me", headers=hdrs_admin).json()["user_id"]
    authz.auth_store._users[admin_id]["role"] = "admin"
    rec = authz.auth_store.admin_create_user("被踢的人", PW)
    authz.auth_store._flush()
    tok = client.post("/v1/auth/login",
                      json={"username": "被踢的人", "password": PW}).json()["token"]
    hdrs = {"Authorization": "Bearer " + tok}
    assert client.get("/v1/auth/me", headers=hdrs).status_code == 200
    res = client.post(f"/v1/admin/users/{rec['user_id']}/reset-password",
                      json={"new_password": PW2}, headers=hdrs_admin)
    assert res.status_code == 200, res.text
    assert client.get("/v1/auth/me", headers=hdrs).status_code == 401
    again = client.post("/v1/auth/login",
                        json={"username": "被踢的人", "password": PW2})
    assert again.status_code == 200 and again.json()["must_change_password"] is True


def test_admin_reset_unknown_user_is_404(client):
    res = client.post("/v1/admin/users/u_ghost/reset-password",
                      json={"new_password": PW2})
    assert res.status_code == 404


def test_change_password_endpoint_completes_the_first_login_loop(client, enforced):
    """同上一条的理由：改密闭环要在 enforced 里走，disabled 的身份不是库里的人。"""
    enforced("引路人")
    from app.core import authz
    rec = authz.auth_store.admin_create_user("闭环的人", PW)
    authz.auth_store._flush()
    tok = client.post("/v1/auth/login",
                      json={"username": "闭环的人", "password": PW}).json()["token"]
    hdrs = {"Authorization": "Bearer " + tok}
    assert client.get("/v1/auth/me", headers=hdrs).json()["user_id"] == rec["user_id"]
    res = client.post("/v1/auth/change-password",
                      json={"current_password": PW, "new_password": PW2}, headers=hdrs)
    assert res.status_code == 200, res.text
    assert res.json()["must_change_password"] is False
    wrong = client.post("/v1/auth/change-password",
                        json={"current_password": PW, "new_password": PW}, headers=hdrs)
    assert wrong.status_code == 401, "旧口令已换掉，再拿它当'当前口令'必须被拒"


def test_disable_self_returns_400_with_a_reason(client, enforced):
    """停用自己→400。这条必须在 enforced 下测：disabled 模式的操作者是引导身份，
    库里根本没有它的记录，只会得到与"停用自己"无关的 404。"""
    hdrs = enforced("自锁管理员")
    me = client.get("/v1/auth/me", headers=hdrs).json()
    # 提升为管理员——与 T1.4 测试同一手法：直接改库，API 里没有写 role 的口子。
    from app.core import authz
    authz.auth_store._users[me["user_id"]]["role"] = "admin"
    authz.auth_store._flush()
    res = client.post(f"/v1/admin/users/{me['user_id']}/disable", headers=hdrs)
    assert res.status_code == 400, f"停用自己不该是 404/403，应是 400 一句实话：{res.text}"
    assert "自己" in res.json()["detail"]


def test_disabling_any_admin_is_400_not_404(client, enforced):
    """R2 决策比"最后一个管理员"更严：任何管理员都禁不掉。
    路由必须把这句理由说出来——404"用户不存在"会把人引向一个根本没死的人。"""
    hdrs_a = enforced("管理员甲")
    hdrs_b = enforced("管理员乙")
    from app.core import authz
    for h in (hdrs_a, hdrs_b):
        uid = client.get("/v1/auth/me", headers=h).json()["user_id"]
        authz.auth_store._users[uid]["role"] = "admin"
    authz.auth_store._flush()
    other = client.get("/v1/auth/me", headers=hdrs_b).json()["user_id"]
    res = client.post(f"/v1/admin/users/{other}/disable", headers=hdrs_a)
    assert res.status_code == 400, res.text
    assert "管理员" in res.json()["detail"]
    assert client.get("/v1/auth/me", headers=hdrs_b).status_code == 200, "被拒的停用不该真的停用"


def test_disable_online_user_then_next_request_is_401_then_enable_restores(client, enforced):
    hdrs_admin = enforced("执法管理员")
    from app.core import authz
    admin_id = client.get("/v1/auth/me", headers=hdrs_admin).json()["user_id"]
    authz.auth_store._users[admin_id]["role"] = "admin"
    authz.auth_store._flush()

    victim = enforced("在线的人")
    uid = client.get("/v1/auth/me", headers=victim).json()["user_id"]
    assert client.post(f"/v1/admin/users/{uid}/disable", headers=hdrs_admin).status_code == 200
    assert client.get("/v1/auth/me", headers=victim).status_code == 401, "停用必须下一个请求就生效"
    assert client.post(f"/v1/admin/users/{uid}/enable", headers=hdrs_admin).status_code == 200
    assert client.get("/v1/auth/me", headers=victim).status_code == 200, "启用要真的把门打开"


def test_users_list_row_gains_must_change_password_and_nothing_else(client):
    """管理列表的字段白名单逐字钉死（PRD 的 注册时间/最近活跃/会话数 + T3.1 的改密旗标）。"""
    client.post("/v1/admin/users", json={"username": "看列表的", "password": PW})
    rows = client.get("/v1/admin/users").json()["users"]
    row = [u for u in rows if u["username"] == "看列表的"][0]
    assert set(row) == {"user_id", "username", "role", "disabled", "must_change_password",
                        "created_at", "last_seen", "sessions"}
