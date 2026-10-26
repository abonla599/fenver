"""2026-10-25 安全复核：四条低成本、可实测的放大面。

每一条都先在**未修**的树上证明会红（见提交说明里的红/绿日志），再收口：

1. 用户名脱敏原先对整串逐字符跑 `unicodedata.category`，2 MB 用户名约 0.15 s，
   公开端点可并发放大；且三个请求模型没有 `max_length`，超长串会走到这一层。
2. `extract_text` 的结束标签匹配从栈尾向前整段扫描，深栈 + 大量不配对结束标签
   是二次复杂度；`http_fetch` 的墙钟总时限不覆盖解析阶段。
3. 未鉴权 401/403 每个请求都写一条 `auth.blocked`，2000 条的内存环会被洪水挤满，
   正常事件随后被压缩一起丢掉。
4. 注册/找回的 422 形状错误原先不占 abuse 预算：匿名客户端轮换用户名就能写满
   2000 条内存环，把真实登录失败挤出去；现在它们与 `auth.blocked` /
   `auth.rate_limited` 共用同一份预算。
"""
import time
import unicodedata

import pytest

from app.core import security_log
from app.tools.extract import extract_text


@pytest.fixture(autouse=True)
def clean_security_log(tmp_path):
    security_log.reset_for_tests(path=tmp_path / "security_log.jsonl", autoflush=False)
    yield
    security_log.reset_for_tests(path=None, autoflush=False)


# ---------- 1. 用户名脱敏：只扫有界前缀 ----------

def test_clean_account_scans_only_a_bounded_prefix(monkeypatch):
    calls = {"n": 0}
    real = unicodedata.category

    def counting(ch):
        calls["n"] += 1
        return real(ch)

    monkeypatch.setattr(security_log.unicodedata, "category", counting)
    cleaned = security_log._clean_account("a" * 2_000_000)

    assert cleaned == "a" * 64
    assert calls["n"] <= 256, (
        f"2 MB 用户名触发了 {calls['n']} 次 category()：脱敏仍在全串扫描，"
        "公开端点可并发放大 CPU")


def test_username_fields_are_capped_before_the_store(client):
    """超长用户名应在 pydantic 层 422，而不是进存储层再判（更不该进安全日志）。"""
    res = client.post("/v1/auth/login",
                      json={"username": "x" * 1000, "password": "whatever"})
    assert res.status_code == 422, res.text
    assert security_log.events(type="auth.fail")[0] == [], \
        "超长用户名在进路由之前就该被拒，不该写一条登录失败"


# ---------- 2. extract_text：深栈 + 不配对结束标签不能二次复杂度 ----------

@pytest.mark.parametrize("open_tag,close_tag", [
    ("<main>", "</article>"),     # 扫 _open（正文候选栈）
    ("<nav>", "</style>"),        # 扫 _stack（非 CDATA 的丢弃容器栈）
])
def test_unmatched_end_tag_storm_is_bounded(open_tag, close_tag):
    n = 10_000
    page = open_tag * n + close_tag * n
    started = time.monotonic()
    text = extract_text("text/html", page)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, (
        f"{open_tag}/{close_tag} 各 {n} 个耗时 {elapsed:.2f}s："
        "结束标签匹配仍是二次复杂度，解析阶段没有预算")
    assert isinstance(text, str) and len(text) <= 3000 + 200


def test_deeply_nested_regions_still_return_text():
    page = "<main>" * 500 + "<p>正文还在</p>" + "</main>" * 500
    text = extract_text("text/html", page)
    assert "正文还在" in text


def test_tag_budget_exhaustion_does_not_leak_script_content():
    page = ("<b>" * 200_005
            + "<script>SECRET_LEAK_XYZ</script><p>VISIBLE_OK</p>")
    text = extract_text("text/html", page)
    assert "SECRET_LEAK_XYZ" not in text, "标签预算耗尽后脚本内容进了正文"


@pytest.mark.parametrize("tag", ["script", "style", "nav"])
def test_self_closing_drop_tag_does_not_leak(tag):
    page = f"<{tag}/>SECRET_SELF_CLOSE</{tag}><p>ok</p>"
    text = extract_text("text/html", page)
    assert "SECRET_SELF_CLOSE" not in text, f"<{tag}/> 绕过了丢弃容器"
    assert "ok" in text


def test_self_closing_svg_does_not_swallow_following_text():
    text = extract_text("text/html", '<svg class="i"/><p>Hello</p>')
    assert text == "Hello"


# ---------- 3. auth.blocked 洪水不能挤掉正常事件 ----------

def test_blocked_flood_from_one_source_is_capped_and_keeps_normal_events():
    security_log.record("auth.fail", ip="203.0.113.9", account="alice",
                        route="/v1/auth/login", status=401, detail="login.failed")
    for i in range(3000):
        security_log.record("auth.blocked", ip="203.0.113.9", status=401,
                            route=f"/v1/x/{i}", detail="token.unknown")

    blocked, _ = security_log.events(type="auth.blocked", limit=500)
    fails, _ = security_log.events(type="auth.fail", limit=500)
    assert len(blocked) <= 20, f"同一来源写了 {len(blocked)} 条 auth.blocked，没有上限"
    assert len(fails) == 1, "正常登录失败事件被匿名洪水挤出了内存环"


def test_blocked_flood_across_sources_has_a_global_cap():
    for i in range(600):
        security_log.record("auth.blocked", ip=f"10.{i // 256}.{i % 256}.1",
                            status=401, route=f"/v1/x/{i}", detail="token.unknown")

    blocked, _ = security_log.events(type="auth.blocked", limit=500)
    assert len(blocked) <= 150, f"跨来源写了 {len(blocked)} 条 auth.blocked，没有全局限额"


def test_rate_limited_flood_with_rotating_accounts_keeps_normal_events():
    security_log.record("auth.fail", ip="203.0.113.9", account="alice",
                        route="/v1/auth/login", status=401, detail="login.failed")
    for i in range(3000):
        security_log.record("auth.rate_limited", ip="203.0.113.9",
                            account=f"u{i}", route="/v1/auth/login", status=429,
                            detail="limit.login")

    limited, _ = security_log.events(type="auth.rate_limited", limit=500)
    fails, _ = security_log.events(type="auth.fail", limit=500)
    assert len(limited) <= 20, f"轮换用户名写了 {len(limited)} 条限流事件"
    assert len(fails) == 1, "正常登录失败事件被限流洪水挤出了内存环"


def test_register_422_shape_errors_share_the_abuse_budget():
    """注册/找回的 422 形状错误是匿名埋点：同一来源轮换账号也必须被预算截住。"""
    security_log.record("auth.fail", ip="203.0.113.9", account="alice",
                        route="/v1/auth/login", status=401, detail="login.failed")
    for i in range(3000):
        security_log.record("auth.fail", ip="203.0.113.9", account=f"u{i}",
                            route="/v1/auth/register", status=422,
                            detail="register.rejected")

    fails, _ = security_log.events(type="auth.fail", limit=500)
    shape_errors = [e for e in fails if e["status"] == 422]
    normal = [e for e in fails if e["status"] == 401]
    assert len(shape_errors) <= 20, (
        f"同一来源写了 {len(shape_errors)} 条 422 形状错误，没占 abuse 预算")
    assert len(normal) == 1, "正常登录失败被匿名 422 洪水挤出了内存环"
    total = [e for e in security_log.snapshot(3600) if e["type"] == "auth.fail"]
    assert len(total) <= 30, f"auth.fail 总数 {len(total)}，没有上限"


def test_register_422_shape_errors_across_sources_have_a_global_cap():
    """跨来源轮换 IP 也要受全局 120 条/分钟预算约束。"""
    security_log.record("auth.fail", ip="203.0.113.9", account="alice",
                        route="/v1/auth/login", status=401, detail="login.failed")
    for i in range(600):
        security_log.record("auth.fail", ip=f"10.{i // 256}.{i % 256}.1",
                            account=f"u{i}", route="/v1/auth/register",
                            status=422, detail="register.rejected")

    rows = security_log.snapshot(3600)
    shape_errors = [e for e in rows
                    if e["type"] == "auth.fail" and e["status"] == 422]
    normal = [e for e in rows
              if e["type"] == "auth.fail" and e["status"] == 401]
    assert len(shape_errors) <= 150, (
        f"跨来源写了 {len(shape_errors)} 条 422 形状错误，没有全局预算")
    assert len(normal) == 1, "正常登录失败被跨来源 422 洪水挤出了内存环"


def test_abuse_ring_cap_leaves_room_for_normal_events(monkeypatch):
    monkeypatch.setattr(security_log, "MAX_ABUSE_EVENTS", 10**9)
    monkeypatch.setattr(security_log, "MAX_ABUSE_EVENTS_PER_SOURCE", 10**9)
    for i in range(600):
        security_log.record("auth.blocked", ip=f"10.{i // 256}.{i % 256}.1",
                            status=401, route=f"/v1/x/{i}", detail="token.unknown")

    blocked = [e for e in security_log._EVENTS if e["type"] == "auth.blocked"]
    assert len(blocked) == security_log.MAX_ABUSE_IN_EVENTS, \
        "环内滥用事件没有被 500 条上限截住"

    security_log.record("auth.fail", ip="203.0.113.9", account="alice",
                        route="/v1/auth/login", status=401, detail="login.failed")
    fails, _ = security_log.events(type="auth.fail", limit=500)
    assert len(fails) == 1, "环内滥用事件占满后，正常事件仍应能写入"
