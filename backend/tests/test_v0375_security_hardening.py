"""2026-10-25 安全复核：三条低成本、可实测的放大面。

每一条都先在**未修**的树上证明会红（见提交说明里的红/绿日志），再收口：

1. 用户名脱敏原先对整串逐字符跑 `unicodedata.category`，2 MB 用户名约 0.15 s，
   公开端点可并发放大；且三个请求模型没有 `max_length`，超长串会走到这一层。
2. `extract_text` 的结束标签匹配从栈尾向前整段扫描，深栈 + 大量不配对结束标签
   是二次复杂度；`http_fetch` 的墙钟总时限不覆盖解析阶段。
3. 未鉴权 401/403 每个请求都写一条 `auth.blocked`，2000 条的内存环会被洪水挤满，
   正常事件随后被压缩一起丢掉。
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
    ("<script>", "</style>"),     # 扫 _stack（丢弃容器栈）
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
        security_log.record("auth.blocked", ip=f"198.51.{i // 256}.{i % 256}",
                            status=401, route=f"/v1/x/{i}", detail="token.unknown")

    blocked, _ = security_log.events(type="auth.blocked", limit=500)
    assert len(blocked) <= 150, f"跨来源写了 {len(blocked)} 条 auth.blocked，没有全局限额"
