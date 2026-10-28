"""管理员凭据失败按来源 IP 计数：第 11 次 429，窗口过后重新放行。

计数的射程就是"管理面 + 带了凭据 + 解析失败"：普通端点的 401 与成功解析都不动
这本账——否则一条正确会话就能替同一个来源把失败次数洗掉。窗口按来源滚动，
所以共享 NAT 下的正常用户会一起等窗口；这是限速按来源计的已知代价。
"""
import pytest

from app.core import authz, security_log
from tests.conftest import peer_client

ADMIN_PATH = "/v1/admin/security/summary"
BOOT_TOKEN = "boot-token"
ADMIN_FAIL_DETAIL = "管理员凭据尝试过多，请稍后再试"
BAD = {"Authorization": "Bearer not-the-bootstrap-token"}


@pytest.fixture(autouse=True)
def _clean_ledgers(tmp_path):
    authz.reset_admin_throttle_for_tests()
    security_log.reset_for_tests(path=tmp_path / "security_log.jsonl", autoflush=False)
    yield
    authz.reset_admin_throttle_for_tests()
    security_log.reset_for_tests(path=None, autoflush=False)


@pytest.fixture
def admin_client(enforced, monkeypatch):
    """enforced 模式 + 已知 bootstrap 口令；对端固定成一个非回环来源。"""
    monkeypatch.setattr(authz, "_bootstrap_token", lambda: BOOT_TOKEN)
    return peer_client(("203.0.113.10", 4001))


def test_eleventh_bad_admin_credential_is_429(admin_client):
    for attempt in range(authz.ADMIN_FAIL_LIMIT):
        res = admin_client.get(ADMIN_PATH, headers=BAD)
        assert res.status_code == 401, (attempt, res.text)

    blocked = admin_client.get(ADMIN_PATH, headers=BAD)
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == ADMIN_FAIL_DETAIL
    assert blocked.headers.get("retry-after") == str(int(authz.ADMIN_FAIL_WINDOW_SECONDS))
    assert "not-the-bootstrap-token" not in blocked.text, "429 回显了凭据"

    items, _ = security_log.events(type="auth.rate_limited")
    assert [item["detail"] for item in items] == ["limit.admin"]


def test_failures_outside_admin_are_not_counted(admin_client):
    for _ in range(authz.ADMIN_FAIL_LIMIT + 1):
        assert admin_client.get(
            "/v1/sessions", headers=BAD).status_code == 401
    assert authz._ADMIN_FAILS == {}, "普通端点的 401 进了管理员失败账"
    # 管理面在自己的账上从零开始，不该继承上面那 11 次
    assert admin_client.get(ADMIN_PATH, headers=BAD).status_code == 401


def test_admin_requests_without_credentials_are_not_counted(admin_client):
    for _ in range(authz.ADMIN_FAIL_LIMIT + 1):
        assert admin_client.get(ADMIN_PATH).status_code == 401
    assert authz._ADMIN_FAILS == {}
    assert admin_client.get(ADMIN_PATH, headers=BAD).status_code == 401


def test_success_does_not_clear_the_window(admin_client):
    for _ in range(authz.ADMIN_FAIL_LIMIT - 1):
        assert admin_client.get(ADMIN_PATH, headers=BAD).status_code == 401

    ok = admin_client.get(ADMIN_PATH,
                          headers={"Authorization": "Bearer " + BOOT_TOKEN})
    assert ok.status_code == 200, ok.text

    # 成功解析不是"洗账"动作：第 10 次失败照常计，第 11 次照常 429
    assert admin_client.get(ADMIN_PATH, headers=BAD).status_code == 401
    assert admin_client.get(ADMIN_PATH, headers=BAD).status_code == 429


def test_saturated_source_blocks_even_the_right_token(admin_client):
    for _ in range(authz.ADMIN_FAIL_LIMIT):
        assert admin_client.get(ADMIN_PATH, headers=BAD).status_code == 401

    # 有意的取舍：限速按来源，不按凭据对不对。来源已被刷满时，正确口令也先等窗口。
    assert admin_client.get(
        ADMIN_PATH, headers={"Authorization": "Bearer " + BOOT_TOKEN}).status_code == 429

    other = peer_client(("203.0.113.11", 4002))
    res = other.get(ADMIN_PATH, headers={"Authorization": "Bearer " + BOOT_TOKEN})
    assert res.status_code == 200, res.text


def test_window_rolls_over(admin_client, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(authz.time, "monotonic", lambda: clock[0])

    for _ in range(authz.ADMIN_FAIL_LIMIT):
        assert admin_client.get(ADMIN_PATH, headers=BAD).status_code == 401
    assert admin_client.get(ADMIN_PATH, headers=BAD).status_code == 429

    clock[0] += authz.ADMIN_FAIL_WINDOW_SECONDS + 1.0
    assert admin_client.get(ADMIN_PATH, headers=BAD).status_code == 401
