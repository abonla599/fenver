"""H-1：管理端高危写操作的 5 分钟二次验证票。"""

from app.core import admin_reauth

BOOT = {"Authorization": "Bearer boot-token"}


def _reset():
    admin_reauth.reset_for_tests()


def test_high_risk_admin_write_needs_a_ticket(client, enforced, monkeypatch):
    monkeypatch.setenv("ADMIN_REAUTH_REQUIRED", "1")
    _reset()
    blocked = client.post("/v1/admin/config", json={"registration_open": False},
                          headers=BOOT)
    assert blocked.status_code == 428
    assert "二次验证" in blocked.json()["detail"]

    wrong = client.post("/v1/auth/admin-reauth", json={"password": "not-the-token"},
                        headers=BOOT)
    assert wrong.status_code == 401

    ok = client.post("/v1/auth/admin-reauth", json={"password": "boot-token"},
                     headers=BOOT)
    assert ok.status_code == 200
    cookie = ok.headers.get("set-cookie", "")
    assert admin_reauth.COOKIE_NAME + "=" in cookie
    assert "HttpOnly" in cookie and "Path=/v1/admin" in cookie

    allowed = client.post("/v1/admin/config", json={"registration_open": False},
                          headers=BOOT)
    assert allowed.status_code == 200, allowed.text


def test_a_plain_user_cannot_get_an_admin_ticket(client, enforced, monkeypatch):
    monkeypatch.setenv("ADMIN_REAUTH_REQUIRED", "1")
    _reset()
    user = enforced("普通用户")
    res = client.post("/v1/auth/admin-reauth", json={"password": "whatever"},
                      headers=user)
    assert res.status_code == 403


def test_ticket_is_bound_to_the_user_and_expires(monkeypatch):
    _reset()
    token = admin_reauth.issue("u_admin")
    assert admin_reauth.valid(token, "u_admin")
    assert not admin_reauth.valid(token, "u_other")
    # 过期：把 monotonic 拨到 TTL 之后
    now = admin_reauth.time.monotonic()
    monkeypatch.setattr(admin_reauth.time, "monotonic",
                        lambda: now + admin_reauth.TICKET_TTL_SECONDS + 1)
    assert not admin_reauth.valid(token, "u_admin")


def test_offline_tool_path_can_create_a_real_admin(tmp_path):
    from app.core.auth import AuthStore, AuthError

    store = AuthStore(path=str(tmp_path / "users.json"))
    record = store.admin_create_user("ops-admin", "correct-horse-battery",
                                     role="admin")
    assert record["role"] == "admin"
    assert store.verify_password(record["user_id"], "correct-horse-battery")
    assert not store.verify_password(record["user_id"], "wrong-password")
    principal, _token = store.login("ops-admin", "correct-horse-battery")
    assert principal.role == "admin"
    try:
        store.admin_create_user("bad-role", "correct-horse-battery", role="owner")
    except AuthError:
        pass
    else:
        raise AssertionError("非法角色没有被拒")
