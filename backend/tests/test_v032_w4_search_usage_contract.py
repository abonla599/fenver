"""v0.32 W4 搜索成本与用量可见的合同测试。

判据来自 .qoder/planning/v0.32-w4-search-usage.md 的 AC-1..AC-8，一句话验收口径：
**管理员答得出「这个窗口搜了几次、挂在哪个源几次、每源成功率」，而账上没有一个
查询原文，失败归因只能是写死的枚举。**

分工不重叠，别在这里重复钉已经钉过的：
- 链的顺序/换源/异常降级 → test_search_chain_contract.py；
- 源层出站四件套与响应形状 → test_search_api_sources.py、test_web_search.py；
- 探针把可用性筛成什么 → test_tool_availability.py；
- LLM 那本账（谁、多少 token、谁的钱）→ test_usage_ledger.py / test_usage_api.py。
本文件只钉 W4 新增的三件事：记账（core/search_usage.py 的写）、按源聚合的读面
（/v1/admin/search-usage）、以及两条边界（探针不记账 + 浏览器直连不进账）。
"""
import json
import os
import re

import pytest

from app.core import search_usage
from app.tools import availability, builtin_tools, search_chain


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """把搜索用量账本挪进临时目录，并连进程内那份字典一起还原。

    `search_usage._days` 与 `_PATH` 是模块级全局（与 usage/schedule 同形）：只设 env
    不还原进程状态的话，一条用例记的账会留给后面所有用例的断言。
    """
    from app.core import search_usage as su

    prev_days, prev_path = dict(su._days), su._PATH
    path = tmp_path / "search_usage.json"
    monkeypatch.setenv("SEARCH_USAGE_DB_PATH", str(path))
    su.restore(path=str(path))
    try:
        yield path
    finally:
        su._days, su._PATH = prev_days, prev_path


@pytest.fixture
def no_probe_side_effects(monkeypatch):
    """web_search_tool 成功时会刷新可用性缓存——那是另一节的状态，别带出用例。"""
    monkeypatch.setattr(availability, "note_search_ok", lambda: None)
    return availability


def _row(tag: str) -> dict:
    return {"title": f"标题 {tag}", "url": f"https://{tag}.example/x", "snippet": f"摘要 {tag}"}


class _Reasoned:
    """带归因的 mock 源：走链上那条 `search_with_reason` 的口。

    `reason` 缺省时按 rows 推（非空 = ok，空 = no_result），跟链对"只会回空的源"
    的退化口径一致；用例里通常显式给一个，钉的就是"这个归因确实进了账"。
    """

    def __init__(self, name, rows=(), reason=None):
        self.name = name
        self._rows = list(rows)
        self.reason = reason if reason is not None else ("ok" if self._rows else "no_result")

    def search_with_reason(self, query, max_results=None):
        return [dict(r) for r in self._rows], self.reason


class _Raises:
    """违约抛异常的源：链按空处理，账上必须留 source_raised 而不是消失。"""

    def __init__(self, name):
        self.name = name

    def search_with_reason(self, query, max_results=None):
        raise RuntimeError(f"{self.name} 又炸了")


def _win(ledger_path, days=1):
    return search_usage.aggregate(days)


# ---------- AC-1 一次成功搜索 = 一笔账，挂在真正答上话的那个源 ----------

def test_one_successful_search_records_one_call_against_the_answering_source(
        ledger, no_probe_side_effects, monkeypatch):
    monkeypatch.setattr(search_chain, "SOURCES", [
        _Reasoned("bocha", [_row("bocha")]), _Reasoned("tavily", [_row("t")])])
    resp = builtin_tools.web_search_tool("西安 人口")
    assert resp.success and "标题 bocha" in resp.data

    data = _win(ledger)
    assert data["totals"] == {"calls": 1, "ok": 1, "failed": 0}, data["totals"]
    by_name = {r["source"]: r for r in data["sources"]}
    assert list(by_name) == ["bocha"], "备用源没被敲过，账上不该凭空多它一行"
    assert by_name["bocha"]["attempts"] == 1 and by_name["bocha"]["ok"] == 1
    assert by_name["bocha"]["reasons"] == {"ok": 1}
    assert by_name["bocha"]["success_rate"] == 1.0


# ---------- AC-2 主源非 200、备用源答上：一次执行一笔，两个源各记一次尝试 ----------

def test_a_degraded_search_is_one_call_but_two_source_attempts(
        ledger, no_probe_side_effects, monkeypatch):
    monkeypatch.setattr(search_chain, "SOURCES", [
        _Reasoned("bocha", reason="non_200"), _Reasoned("tavily", [_row("t")])])
    resp = builtin_tools.web_search_tool("西安 人口")
    assert resp.success and "标题 t" in resp.data

    data = _win(ledger)
    assert data["totals"]["calls"] == 1, "一次工具执行记一笔：换源不许把它算成两次搜索"
    by_name = {r["source"]: r for r in data["sources"]}
    assert by_name["bocha"]["failed"] == 1 and by_name["bocha"]["reasons"] == {"non_200": 1}
    assert by_name["tavily"]["ok"] == 1
    # 全链口径：这次执行对用户是成功的
    assert data["totals"]["ok"] == 1


# ---------- AC-3 全链空 + 未启用不进成功率分母 ----------

def test_an_all_empty_chain_records_a_failed_call_with_per_source_reasons(
        ledger, no_probe_side_effects, monkeypatch):
    monkeypatch.setattr(search_chain, "SOURCES", [
        _Reasoned("bocha", reason="not_enabled"), _Raises("tavily"),
        _Reasoned("bing", reason="no_result")])
    resp = builtin_tools.web_search_tool("某件查不到的事")
    assert "这次搜索没有返回结果" in resp.data

    data = _win(ledger)
    assert data["totals"] == {"calls": 1, "ok": 0, "failed": 1}
    by_name = {r["source"]: r for r in data["sources"]}
    assert by_name["bocha"]["skipped"] == 1 and by_name["bocha"]["attempts"] == 0
    assert by_name["bocha"]["success_rate"] is None, \
        "没配 key 的源不能报 0% 成功率：那是把「没用它」说成「用它全败」"
    assert by_name["tavily"]["reasons"] == {"source_raised": 1}
    assert by_name["bing"]["failed"] == 1 and by_name["bing"]["reasons"] == {"no_result": 1}


def test_a_source_that_never_went_out_net_contributes_no_denominator(ledger):
    """成功率 = 给结果 / 真出网的尝试；skipped 只在它自己那一栏里。"""
    search_usage.record_search([{"source": "bocha", "reason": "not_enabled"},
                                {"source": "bing", "reason": "ok"}], ok=True)
    row = {r["source"]: r for r in _win(ledger)["sources"]}
    assert row["bocha"]["attempts"] == 0 and row["bocha"]["skipped"] == 1
    assert row["bing"]["success_rate"] == 1.0


# ---------- AC-4 归因是写死的枚举，自由文本进不了账 ----------

FROZEN_REASONS = {"ok", "not_enabled", "empty_query", "network", "non_200", "oversize",
                  "parse_empty", "no_result", "source_raised"}


def test_the_reason_enum_is_the_frozen_set_and_nothing_else(ledger):
    """加一个归因值必须是刻意的动作：这条会红在那里，逼来人对着链代码说清它来自哪一支。"""
    assert set(search_usage.ALL_REASONS) == FROZEN_REASONS
    assert search_usage.REASON_OK == "ok"
    # 未出网的那两档不能混进失败里：它们不进成功率的分母（AC-3 判的就是这个）
    assert set(search_usage.SKIPPED_REASONS) == {"not_enabled", "empty_query"}
    assert set(search_usage.FAILED_REASONS) == FROZEN_REASONS - {"ok"} - set(
        search_usage.SKIPPED_REASONS)


@pytest.mark.parametrize("bad", [
    "源站挂了", "网络问题，可能是DNS", "", None, "OK", "non-200", 0])
def test_free_text_reason_is_rejected_and_the_ledger_stays_untouched(ledger, bad):
    before = json.dumps(_win(ledger, 30))
    with pytest.raises(ValueError):
        search_usage.record_search([{"source": "bocha", "reason": bad}], ok=False)
    assert json.dumps(_win(ledger, 30)) == before, "拒收之外还得保证一行都没写进去"


def test_an_attempt_without_a_source_name_is_rejected(ledger):
    with pytest.raises(ValueError):
        search_usage.record_search([{"reason": "non_200"}], ok=False)
    with pytest.raises(ValueError):
        search_usage.record_search("不是对象", ok=False)


def test_real_api_sources_report_only_enum_values(tmp_path, monkeypatch):
    """跨层漂移锁：真源给出的归因必须在这份枚举里，否则记账当场 ValueError 而漏账。

    这里跑的是博查/Tavily 的真实代码（没配 key 那一支），不是 mock。
    """
    from app.tools.search_api_sources import BochaSource, TavilySource

    monkeypatch.setenv("SEARCH_KEYS_DB_PATH", str(tmp_path / "absent.json"))
    for src in (BochaSource(), TavilySource()):
        rows, reason = src.search_with_reason("北京 天气")
        assert rows == [] and reason in search_usage.ALL_REASONS, (src.name, reason)


def test_the_admin_page_label_map_covers_exactly_the_frozen_enum():
    """`admin.js` 里那句"枚举多一个值而这里少一条会由合同测试钉住"不许是空话。

    界面那一格用 `SEARCH_REASON[k] || k` 兜底，所以少一条不会报错——它会把
    `parse_empty` 这样没翻译的键名直接摊给管理员看。账本不许自由文本，但界面
    编得出第二套说法，这条锁把两边钉成同一份。
    """
    js = _admin_js().read_text(encoding="utf-8")
    block = re.search(r"const SEARCH_REASON = \{(.*?)\};", js, re.S)
    assert block, "admin.js 里找不到 SEARCH_REASON 这张表"
    labels = dict(re.findall(r'^\s*(\w+):\s*"([^"]*)"', block.group(1), re.M))
    assert set(labels) == set(search_usage.ALL_REASONS), (
        set(search_usage.ALL_REASONS) ^ set(labels))
    # 标签不许是空的或跟键名同名：那就等于没翻译
    assert all(v.strip() and v != k for k, v in labels.items()), labels


def _admin_js():
    from pathlib import Path
    return (Path(__file__).resolve().parents[1]
            / "app" / "web" / "admin" / "admin.js")


# ---------- AC-5 隐私口径：账上没有查询原文，也没有哈希 ----------

def test_the_query_never_reaches_the_ledger(ledger, no_probe_side_effects, monkeypatch):
    marker = "鲸落试验场-QX7"
    monkeypatch.setattr(search_chain, "SOURCES", [_Reasoned("bing", [_row("bing")])])
    builtin_tools.web_search_tool(marker + " 是什么")

    raw = ledger.read_text(encoding="utf-8")
    assert marker not in raw, "查询原文落盘了"
    assert "鲸落" not in raw
    # 连"哈希一格"都不许有：2–4 个词的短查询，哈希穷举一下就还原成原文
    assert "hash" not in raw.lower()

    def keys_of(node):
        if isinstance(node, dict):
            return set(node) | set().union(*(keys_of(v) for v in node.values())) if node else set()
        if isinstance(node, list):
            return set().union(*(keys_of(v) for v in node)) if node else set()
        return set()

    assert not any(k in keys_of(json.loads(raw)) for k in ("query", "text", "q")), keys_of(json.loads(raw))


# ---------- AC-6 两条边界：探针不记账，浏览器直连不进账 ----------

def test_the_availability_probe_ages_nothing_in_the_search_ledger(
        ledger, monkeypatch, _clean_probe_state):
    """探针每 15 分钟拿固定的「北京 天气」真跑一次链：记进去就是每小时虚增 4 次。"""
    monkeypatch.setattr(search_chain, "SOURCES", [_Reasoned("bing", [_row("bing")])])
    availability._refresh()
    assert _win(ledger, 30)["totals"]["calls"] == 0, "探针被记成了一次真实搜索"


def test_boundaries_are_spelled_out_by_the_read_surface(ledger):
    """边界由后端说，界面不另写一套：两份措辞迟早有一份先说谎。"""
    data = _win(ledger, 7)
    # aggregate 本身不带 boundaries（那是路由层的话），但窗口/成功率口径必须先齐
    assert data["totals"] == {"calls": 0, "ok": 0, "failed": 0}
    assert data["window"]["days"] == 7


def test_the_direct_browser_boundary_is_written_down_where_operators_read():
    """W2 那条浏览器/WebView 直连面板的用量不进这本账——这条边界必须落在文档里。

    不造假数据的意思是：既不把用户设备上的出站算成本站的，也不拿 0 冒充"用户没用"。
    """
    doc = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       "docs", "搜索源浏览器直连矩阵.md")
    text = open(doc, encoding="utf-8").read()
    assert re.search(r"不进这本账|不计入这本账", text), "矩阵文档没写这条边界"
    assert "search_usage" in text


# ---------- AC-7 读面：按源聚合的数、零要读成零、鉴权与参数闸 ----------

def test_the_window_sums_days_and_lists_missing_days_as_zero(ledger):
    from datetime import datetime, timedelta
    today = datetime.now().astimezone()
    two_ago = (today - timedelta(2)).strftime("%Y-%m-%d")
    search_usage._days[two_ago] = {
        "calls": 3, "ok": 2, "failed": 1,
        "sources": {"bocha": {"attempts": 2, "ok": 2, "failed": 0, "skipped": 0,
                             "reasons": {"ok": 2}},
                    "tavily": {"attempts": 2, "ok": 0, "failed": 2, "skipped": 0,
                              "reasons": {"non_200": 2}}}}
    data = _win(ledger, 7)
    assert data["totals"] == {"calls": 3, "ok": 2, "failed": 1}
    by_name = {r["source"]: r for r in data["sources"]}
    assert by_name["bocha"]["success_rate"] == 1.0
    assert by_name["tavily"]["success_rate"] == 0.0, "真出过网且全败，那就是 0%，不是 null"
    # 缺账的六天必须逐日列出零：否则"这周没搜过"和"读不到"长成同一个样子
    assert len(data["by_day"]) == 7
    assert sum(1 for d in data["by_day"] if d["calls"] == 0) == 6


def test_the_ledger_survives_a_restart(ledger):
    search_usage.record_search([{"source": "bing", "reason": "ok"}], ok=True)
    before = _win(ledger)["totals"]
    search_usage.restore(path=str(ledger))
    assert _win(ledger)["totals"] == before == {"calls": 1, "ok": 1, "failed": 0}


def test_a_type_corrupted_ledger_degrades_to_zero_instead_of_killing_import(ledger):
    """合法 JSON、值类型不对（有人手工改过数字）：读成 0，不能把进程拖崩。

    `int("abc")` 的 ValueError 落在 restore 那个 try/except 外面，而 restore 在模块
    导入时就跑——那时崩的不是"这一页读不了"，是整个后端起不来。
    """
    from datetime import datetime
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    ledger.write_text(json.dumps({"days": {today: {
        "calls": "很多", "ok": None, "failed": 1,
        "sources": {"bocha": {"attempts": 1, "ok": "abc", "skipped": None,
                              "reasons": {"ok": "x"}}}}}}),
        encoding="utf-8")
    assert search_usage.restore(path=str(ledger)) == 1
    data = _win(ledger)
    assert data["totals"] == {"calls": 0, "ok": 0, "failed": 1}, data["totals"]
    row = {r["source"]: r for r in data["sources"]}
    assert row["bocha"]["attempts"] == 1, "没改坏的那一格不该被连坐"
    assert row["bocha"]["ok"] == 0 and row["bocha"]["reasons"] == {"ok": 0}
    # 顶层 days 不是字典时也照样起步：宁可读出零，不要读出一个栈
    ledger.write_text(json.dumps({"days": []}), encoding="utf-8")
    assert search_usage.restore(path=str(ledger)) == 0


def test_admin_endpoint_reports_the_same_numbers_as_the_ledger(client, ledger):
    search_usage.record_search([{"source": "bocha", "reason": "parse_empty"},
                               {"source": "bing", "reason": "ok"}], ok=True)
    admin = {"Authorization": "Bearer " + os.environ["ACCESS_TOKEN"]}
    body = client.get("/v1/admin/search-usage", headers=admin).json()
    assert body["totals"] == {"calls": 1, "ok": 1, "failed": 0}
    by_name = {r["source"]: r for r in body["sources"]}
    assert by_name["bocha"]["reasons"] == {"parse_empty": 1}
    assert by_name["bing"]["success_rate"] == 1.0
    assert body["boundaries"]["direct_browser"], "边界那句话必须由后端回，不是界面现编"
    assert "不计" in body["boundaries"]["probe"]


def test_a_mere_user_cannot_read_the_search_ledger(client, enforced, ledger):
    hdr = enforced("search-usage-outsider")
    assert client.get("/v1/admin/search-usage", headers=hdr).status_code == 403
    admin = {"Authorization": "Bearer " + os.environ["ACCESS_TOKEN"]}
    assert client.get("/v1/admin/search-usage", headers=admin).status_code == 200


@pytest.mark.parametrize("bad", ["0", "-3", "91", "999"])
def test_the_window_param_is_gated_not_clamped(client, ledger, bad):
    """越界给 400 而不是悄悄夹到 90：宽松一点就是"随便传什么都拿到一份 200"，
    调用方看不出自己问错了（与 /v1/admin/usage 的 day 同一条纪律）。"""
    admin = {"Authorization": "Bearer " + os.environ["ACCESS_TOKEN"]}
    res = client.get(f"/v1/admin/search-usage?days={bad}", headers=admin)
    assert res.status_code == 400, res.text


def test_a_non_integer_window_is_a_shape_error(client, ledger):
    admin = {"Authorization": "Bearer " + os.environ["ACCESS_TOKEN"]}
    assert client.get("/v1/admin/search-usage?days=一周", headers=admin).status_code == 422


def test_empty_ledger_reads_as_zero_not_as_an_error(client, ledger):
    admin = {"Authorization": "Bearer " + os.environ["ACCESS_TOKEN"]}
    body = client.get("/v1/admin/search-usage?days=7", headers=admin).json()
    assert body["totals"]["calls"] == 0 and body["sources"] == []
    assert len(body["by_day"]) == 7


@pytest.mark.parametrize("field", ["attempts", "ok", "failed", "skipped", "success_rate",
                                   "reasons", "source"])
def test_every_source_row_carries_the_fields_the_page_renders(client, ledger, field):
    """响应里少一个键，界面对应那一格就永远空白而不是报错——这条锁先把键名钉住。"""
    search_usage.record_search([{"source": "bing", "reason": "ok"}], ok=True)
    admin = {"Authorization": "Bearer " + os.environ["ACCESS_TOKEN"]}
    row = client.get("/v1/admin/search-usage", headers=admin).json()["sources"][0]
    assert field in row, sorted(row)


# ---------- AC-8 不动别人钉过的东西：源合同与 W2/W3 的文案 ----------

def test_the_plain_source_contract_still_returns_a_list_only(monkeypatch, tmp_path):
    """W1 那条源合同一个字没改：`search()` 仍然只回列表（新归因走另一个方法）。"""
    from app.tools.search_api_sources import BochaSource

    monkeypatch.setenv("SEARCH_KEYS_DB_PATH", str(tmp_path / "absent.json"))
    out = BochaSource().search("北京 天气")
    assert isinstance(out, list) and out == []


def test_the_crawl_source_still_knocks_web_search_search_by_module_attribute(monkeypatch):
    """W4 加了 search_with_reason，但爬取源必须仍旧调用期取 `web_search.search`。

    既有测试全部 monkeypatch 那一个属性；改成调别的函数（哪怕是同模块的
    `search_detailed`）就会让那些桩一夜失效，链照常打真 HTTP。
    """
    from app.tools import web_search

    seen = []
    monkeypatch.setattr(web_search, "search",
                        lambda q, *r: seen.append((q, r)) or [_row("bing")])
    rows, reason = search_chain.BingHtmlSource().search_with_reason("某校 简章")
    assert rows and reason == "ok"
    assert seen == [("某校 简章", ())], f"调用形态变了：{seen}"


def test_a_source_without_a_reason_method_still_gets_booked(ledger, monkeypatch,
                                                           no_probe_side_effects):
    """只实现合同 `search` 的源（第三方/mock）不退化成"漏记"：归因落成 no_result。"""
    class Plain:
        name = "plain"

        def search(self, query, max_results=None):
            return [dict(_row("plain"))]

    monkeypatch.setattr(search_chain, "SOURCES", [Plain()])
    builtin_tools.web_search_tool("某件事")
    row = {r["source"]: r for r in _win(ledger)["sources"]}
    assert row["plain"]["ok"] == 1 and row["plain"]["reasons"] == {"ok": 1}


@pytest.fixture
def _clean_probe_state(monkeypatch):
    """把 availability 的进程级缓存钉回"没测过"，测完原样交还（与 W1 用例同一条处置）。"""
    saved = dict(availability._state)
    availability._state["value"] = None
    availability._state["checked_at"] = 0.0
    yield
    availability._state.update(saved)
