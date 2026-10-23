"""v0.36.3 安全监控端点的契约：守卫、参数校验、聚合、游标与真实埋点。

admin 端点走 `RequireAdmin`：匿名 401、普通用户 403、管理员 200 且 no-store。
埋点用例一律通过真实请求产生事件（不直接调 record），证明接线在中间件/端点里。
"""
import time

import pytest

from app.core import security_log

BOOT = {"Authorization": "Bearer boot-token"}
SUMMARY = "/v1/admin/security/summary"
EVENTS = "/v1/admin/security/events"


@pytest.fixture(autouse=True)
def slog(tmp_path):
    security_log.reset_for_tests(path=str(tmp_path / "security_log.jsonl"),
                                 autoflush=False)
    yield
    security_log.reset_for_tests(path=None, autoflush=False)


def _seed(count=5, event_type="auth.fail", detail="login.failed", ip="203.0.113.77"):
    for i in range(count):
        security_log.record(event_type, ip=ip, account=f"user-{i}",
                            route="/v1/auth/login", status=401, detail=detail,
                            now=time.time() - 60 + i)


# ---------- 守卫与响应头 ----------

def test_anonymous_is_rejected(client, enforced):
    enforced("占位")                       # 切到 enforced，这是唯一能演 401 的模式
    assert client.get(SUMMARY).status_code == 401
    assert client.get(EVENTS).status_code == 401


def test_a_mere_user_gets_403(client, enforced):
    headers = enforced("普通用户")
    assert client.get(SUMMARY, headers=headers).status_code == 403
    assert client.get(EVENTS, headers=headers).status_code == 403


def test_admin_gets_200_with_no_store(client, enforced):
    enforced("占位")
    for url in (SUMMARY, EVENTS):
        res = client.get(url, headers=BOOT)
        assert res.status_code == 200, res.text
        assert res.headers.get("cache-control") == "no-store", url


# ---------- 参数校验 ----------

def test_unknown_window_is_400(client):
    res = client.get(SUMMARY, params={"window": "30d"})
    assert res.status_code == 400
    assert res.json()["detail"]


def test_unknown_event_type_is_400(client):
    assert client.get(EVENTS, params={"type": "auth.evil"}).status_code == 400


@pytest.mark.parametrize("limit", ["abc", "0", "201", "-1", "1.5"])
def test_bad_limit_is_400(client, limit):
    assert client.get(EVENTS, params={"limit": limit}).status_code == 400


@pytest.mark.parametrize("before", ["0", "-3", "abc", "1.5"])
def test_bad_before_is_400(client, before):
    assert client.get(EVENTS, params={"before": before}).status_code == 400


def test_limit_defaults_to_fifty_and_accepts_the_boundaries(client):
    _seed(3)
    assert client.get(EVENTS).status_code == 200
    assert client.get(EVENTS, params={"limit": "1"}).json()["events"]
    assert client.get(EVENTS, params={"limit": "200"}).status_code == 200


# ---------- 聚合与游标 ----------

def test_summary_totals_and_top_lists_reflect_the_events(client):
    _seed(3, ip="203.0.113.77")
    security_log.record("auth.blocked", ip="198.51.100.9", account="ghost",
                        route="/v1/admin/users", status=401, detail="token.unknown",
                        now=time.time() - 30)
    security_log.record("auth.blocked", ip="198.51.100.9", account="ghost",
                        route="/v1/admin/users", status=401, detail="token.unknown",
                        now=time.time() - 20)   # 同键合并 → count=2

    body = client.get(SUMMARY, params={"window": "24h"}).json()
    assert body["window"] == "24h"
    assert body["totals"] == {"events": 5, "alert": 0, "warn": 5, "info": 0,
                              "sources": 2, "accounts": 4}
    assert body["by_type"] == [{"type": "auth.fail", "count": 3},
                               {"type": "auth.blocked", "count": 2}]
    assert body["by_source"][0]["count"] == 3
    assert len(body["by_source"]) == 2
    assert body["by_account"][0] == {"account": "ghost", "count": 2}
    assert isinstance(body["generated_at"], str)
    assert body["audit"].keys() >= {"size_bytes", "lines", "entries", "last_ts"}
    assert body["audit"]["entries"] == body["audit"]["lines"]
    assert body["warnings"] == []


def test_events_are_descending_and_the_cursor_pages_backwards(client):
    _seed(5)
    first = client.get(EVENTS, params={"limit": "2"}).json()
    assert [item["id"] for item in first["events"]] == [5, 4]
    assert first["next_before"] == 4
    second = client.get(EVENTS, params={"limit": "2",
                                        "before": str(first["next_before"])}).json()
    assert [item["id"] for item in second["events"]] == [3, 2]
    third = client.get(EVENTS, params={"limit": "2",
                                       "before": str(second["next_before"])}).json()
    assert [item["id"] for item in third["events"]] == [1]
    assert third["next_before"] is None


def test_events_filters_by_type_source_and_account(client):
    _seed(3)
    security_log.record("auth.blocked", ip="198.51.100.9", account="ghost",
                        route="/v1/admin/users", status=401, detail="token.unknown")
    assert len(client.get(EVENTS, params={"type": "auth.blocked"}).json()["events"]) == 1
    assert len(client.get(EVENTS, params={"source": "198.51.100.0/24"}).json()["events"]) == 1
    assert len(client.get(EVENTS, params={"account": "ghost"}).json()["events"]) == 1
    assert client.get(EVENTS, params={"account": "nobody"}).json()["events"] == []


def test_source_and_account_filters_are_truncated_not_rejected(client):
    _seed(1)
    assert client.get(EVENTS, params={"source": "x" * 200}).status_code == 200
    assert client.get(EVENTS, params={"account": "y" * 200}).status_code == 200


# ---------- 真实埋点 ----------

def test_middleware_rejection_really_records_auth_blocked(client, enforced):
    enforced("占位")
    res = client.get("/v1/admin/users", headers={"Authorization": "Bearer wrong"})
    assert res.status_code == 401
    items, _ = security_log.events(type="auth.blocked")
    assert len(items) == 1, "中间件 401 没有产生安全事件"
    event = items[0]
    assert event["detail"] == "token.unknown"
    assert event["status"] == 401
    assert event["route"] == "/v1/admin/users"
    assert event["source"] == "127.0.0.0/24"
    assert "wrong" not in str(event), "凭据原文不许进安全日志"


def test_csrf_rejection_really_records_auth_blocked(client, enforced):
    headers = enforced("csrf-user")
    assert client.post("/v1/auth/adopt", headers=headers).status_code == 200
    res = client.post("/v1/admin/config", json={"registration_open": True})
    assert res.status_code == 403, res.text
    items, _ = security_log.events(type="auth.blocked")
    assert [(item["detail"], item["status"], item["route"]) for item in items] == [
        ("csrf.missing", 403, "/v1/admin/config")]


def test_a_real_login_failure_records_auth_fail(client):
    username = "security-login-probe"
    reg = client.post("/v1/auth/register", json={
        "username": username, "password": "correct-horse-battery",
        "security_answers": ["新市场小学", "hehai2024", "李建国"]})
    assert reg.status_code == 200, reg.text
    res = client.post("/v1/auth/login",
                      json={"username": username, "password": "definitely-wrong"})
    assert res.status_code == 401
    items, _ = security_log.events(type="auth.fail")
    assert len(items) == 1
    event = items[0]
    assert event["detail"] == "login.failed"
    assert event["account"] == username
    assert event["status"] == 401
    assert event["route"] == "/v1/auth/login"
    assert "definitely-wrong" not in str(event), "口令绝不许进安全日志"
