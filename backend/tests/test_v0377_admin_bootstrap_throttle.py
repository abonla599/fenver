"""bootstrap 凭据失败按来源 IP 计数：第 11 次 429，窗口过后重新放行。

计数的射程是"任何受保护路径 + 带了请求头凭据 + 解析失败"：resolve_principal 在
所有受保护路径上都会拿请求头凭据去比 bootstrap 口令，只记管理面与会话兑换出口的
话，/v1/sessions、/v1/auth/me 就是不计数的口令比对器。只带 Cookie 的 401 与没带
任何凭据的 401 都不动这本账——正常过期会话不该把来源额度耗光。窗口按来源滚动，
所以共享 NAT 下的正常用户会一起等窗口；这是限速按来源计的已知代价。
"""
import pytest

from app.core import authz, security_log
from tests.conftest import peer_client

ADMIN_PATH = "/v1/admin/security/summary"
ADOPT_PATH = "/v1/auth/adopt"
SESSIONS_PATH = "/v1/sessions"
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


def test_adopt_bad_credentials_share_the_admin_budget(admin_client):
    # adopt 是管理页粘贴 bootstrap 口令后的换会话出口；若不计在这里，它就是一个
    # 不限速的口令比对器。10 次失败走同一本账，第 11 次和管理面一样 429。
    for attempt in range(authz.ADMIN_FAIL_LIMIT):
        res = admin_client.post(ADOPT_PATH, headers=BAD)
        assert res.status_code == 401, (attempt, res.text)

    blocked = admin_client.post(ADOPT_PATH, headers=BAD)
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == ADMIN_FAIL_DETAIL
    assert "not-the-bootstrap-token" not in blocked.text

    items, _ = security_log.events(type="auth.rate_limited")
    assert [item["detail"] for item in items] == ["limit.admin"]

    # 同一本账：这个来源的管理面额度也一起被挡下
    assert admin_client.get(ADMIN_PATH, headers=BAD).status_code == 429


def test_non_admin_path_bad_credentials_share_the_same_budget(admin_client):
    """普通受保护端点也是 bootstrap 口令的比对入口，必须与 admin/adopt 同账。

    resolve_principal 不区分路径：任何 /v1 受保护端点都会把请求头凭据拿去和
    bootstrap 口令比一次。少了这一条，攻击者换到 /v1/sessions 就能无限试。
    """
    for attempt in range(authz.ADMIN_FAIL_LIMIT):
        res = admin_client.get(SESSIONS_PATH, headers=BAD)
        assert res.status_code == 401, (attempt, res.text)

    blocked = admin_client.get(SESSIONS_PATH, headers=BAD)
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == ADMIN_FAIL_DETAIL
    assert blocked.headers.get("retry-after") == str(int(authz.ADMIN_FAIL_WINDOW_SECONDS))
    assert "not-the-bootstrap-token" not in blocked.text
    assert len(authz._ADMIN_FAILS["203.0.113.10"]) == authz.ADMIN_FAIL_LIMIT

    # 同一来源的管理面也在这本账上：额度不会因为换路径而另起一本
    assert admin_client.get(ADMIN_PATH, headers=BAD).status_code == 429


def test_cookie_only_failures_do_not_consume_the_budget(admin_client):
    """Cookie-only 的 401 不计数：正常过期会话不该把来源额度耗光。"""
    admin_client.cookies.set(authz.SESSION_COOKIE, "garbage-not-a-token", path="/v1")
    for _ in range(authz.ADMIN_FAIL_LIMIT + 1):
        assert admin_client.get(SESSIONS_PATH).status_code == 401
    assert authz._ADMIN_FAILS == {}, "Cookie-only 的 401 进了凭据失败账"

    # 这个来源的请求头凭据额度仍从零开始
    assert admin_client.get(SESSIONS_PATH, headers=BAD).status_code == 401
    assert len(authz._ADMIN_FAILS["203.0.113.10"]) == 1


def test_correct_bootstrap_can_still_adopt_from_unsaturated_source(admin_client):
    res = admin_client.post(ADOPT_PATH,
                            headers={"Authorization": "Bearer " + BOOT_TOKEN})
    assert res.status_code == 200, res.text
    assert res.json()["role"] == "admin"
    assert authz._ADMIN_FAILS == {}


def test_successful_header_requests_release_their_own_reservation(admin_client):
    """合法 header 请求只还自己那一条：账不增长，也不替别的失败洗账。"""
    for _ in range(authz.ADMIN_FAIL_LIMIT):
        assert admin_client.get(SESSIONS_PATH,
                                headers={"Authorization": "Bearer " + BOOT_TOKEN}
                                ).status_code == 200
        assert authz._ADMIN_FAILS == {}, "成功请求把自己的预占留在了账上"

    # 先攒 5 次失败，再打 10 次合法请求：成功只释放各自的预占，失败记录不动
    for _ in range(5):
        assert admin_client.get(SESSIONS_PATH, headers=BAD).status_code == 401
    for _ in range(authz.ADMIN_FAIL_LIMIT):
        assert admin_client.get(SESSIONS_PATH,
                                headers={"Authorization": "Bearer " + BOOT_TOKEN}
                                ).status_code == 200
    assert len(authz._ADMIN_FAILS["203.0.113.10"]) == 5, \
        "成功请求把自己的预占还回去了，却顺带洗掉了别的失败"


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


def test_middleware_reserves_before_resolving(admin_client, monkeypatch):
    """中间件先预占、再解凭据：reserve 与 resolve 的顺序不许对调。

    顺序对调（先 resolve 记结果、再补记账）在顺序请求下看不出差别，并发突发
    却正是靠"解析之前就已经入账"关住的。这里在 resolve 入口读一眼账：第 N 次
    解析发生时，账上已经躺着第 N 条预占。
    """
    seen = []
    real = authz.resolve_principal

    def spy(request):
        seen.append(len(authz._ADMIN_FAILS.get("203.0.113.10", [])))
        return real(request)

    monkeypatch.setattr(authz, "resolve_principal", spy)
    for _ in range(authz.ADMIN_FAIL_LIMIT):
        assert admin_client.get(SESSIONS_PATH, headers=BAD).status_code == 401
    assert seen == list(range(1, authz.ADMIN_FAIL_LIMIT + 1)), \
        "resolve 时账上还没有本次预占：判定与解析之间存在 check-then-act 窗口"


def test_attempt_reservation_is_atomic():
    """判定与记账是同一个动作：第 11 次预占在判定那一刻就不可能成功。

    并发窗口里的两个请求都能看到"还没满"才算旧实现；这里直接连调 11 次
    （同一来源、同一时刻），前 10 次必须各自入账，第 11 次必须拒绝。
    """
    ip = "198.51.100.9"
    now = 5000.0
    granted = [authz._admin_attempt_start(ip, now)
               for _ in range(authz.ADMIN_FAIL_LIMIT + 1)]
    assert granted == [True] * authz.ADMIN_FAIL_LIMIT + [False]
    assert len(authz._ADMIN_FAILS[ip]) == authz.ADMIN_FAIL_LIMIT

    # 另一个来源互不牵连
    assert authz._admin_attempt_start("198.51.100.10", now) is True

    # 成功释放的正是本次预占的那一枚：删一条，之后又能再进一次，再下一次又满
    authz._admin_attempt_success(ip, now)
    assert len(authz._ADMIN_FAILS[ip]) == authz.ADMIN_FAIL_LIMIT - 1
    assert authz._admin_attempt_start(ip, now) is True
    assert authz._admin_attempt_start(ip, now) is False

    # 找不到对应时间戳时 no-op：不新建来源，也不误删别人的记录
    authz._admin_attempt_success("198.51.100.99", now)
    assert "198.51.100.99" not in authz._ADMIN_FAILS
    authz._admin_attempt_success(ip, now + 1.0)
    assert len(authz._ADMIN_FAILS[ip]) == authz.ADMIN_FAIL_LIMIT


def test_hard_key_limit_evicts_the_oldest_source(monkeypatch):
    """窗口里全是新来源时，表也不能无限长：按最后尝试时间 LRU，绝不整表清空。"""
    monkeypatch.setattr(authz, "_ADMIN_FAILS_MAX_KEYS", 3)
    base = 7000.0
    for offset in range(3):
        assert authz._admin_attempt_start(f"198.51.100.{offset}", base + offset)
    assert set(authz._ADMIN_FAILS) == {"198.51.100.0", "198.51.100.1", "198.51.100.2"}

    # 刷新最旧的那个来源：它的"最后尝试"变成最新，淘汰顺序必须跟着变（LRU）
    assert authz._admin_attempt_start("198.51.100.0", base + 3)
    assert authz._admin_attempt_start("198.51.100.9", base + 4)

    assert len(authz._ADMIN_FAILS) == 3
    assert "198.51.100.1" not in authz._ADMIN_FAILS, "淘汰的不是最后尝试最旧的来源"
    assert set(authz._ADMIN_FAILS) == {"198.51.100.0", "198.51.100.2", "198.51.100.9"}
    # 被淘汰的是键位，不是整表：最近还在尝试的来源预算原样保留
    assert len(authz._ADMIN_FAILS["198.51.100.0"]) == 2


def test_non_loopback_cf_header_is_not_trusted_for_the_ledger(enforced):
    """对端不是回环时，cf-connecting-ip 是客户端自己写的：账要落在真实对端上。"""
    client = peer_client(("203.0.113.12", 4010))
    forged = {**BAD, "cf-connecting-ip": "198.51.100.7"}
    for _ in range(authz.ADMIN_FAIL_LIMIT):
        assert client.get(ADMIN_PATH, headers=forged).status_code == 401
    assert client.get(ADMIN_PATH, headers=forged).status_code == 429

    assert set(authz._ADMIN_FAILS) == {"203.0.113.12"}
