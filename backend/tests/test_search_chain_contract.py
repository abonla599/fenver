"""v0.32 W1 搜索多源抽象与降级链的合同测试：用两条 mock 源就够。

验收口径是一句话：**拔掉第一源，web_search 仍能出结果，且留痕写明用的是第二源。**
本文件每条用例都对得上 .qoder/planning/v0.32-w1-search-chain.md 里的 AC-1..6。

分工不重叠：源层自身（HTML 解析、出站四件套、判空）钉在 test_web_search.py；
可用性筛清单钉在 test_tool_availability.py；帧的裁剪/封顶钉在
test_process_trace_contract.py。这里只钉"链"这一层的合同——
顺序、判空换源、异常降级、源名留痕，以及"旧帧字节不变"。

mock 源不需要网络也不需要打桩真源：search_chain.search 的 `sources` 参数
就是为这条链的测试留的注入口。
"""
import json

import pytest

from app.core import stream_events as se
from app.tools import availability, builtin_tools, search_chain, web_search
from app.tools.executor import execute_tool


def _row(tag: str) -> dict:
    return {"title": f"标题 {tag}", "url": f"https://{tag}.example/x", "snippet": f"摘要 {tag}"}


class _FakeSource:
    """照源接口合同写的 mock：name + search(query, max_results) → 列表。

    `rows` 给函数就按调用求值（用来做"第一源被拔掉"的开关），给列表就原样回；
    raises=True 模拟违约抛异常的坏源——链必须把它按空处理而不是跟着炸。
    """

    def __init__(self, name, rows=(), raises=False):
        self.name = name
        self.raises = raises
        self._rows = rows
        self.calls = []

    def search(self, query, max_results=None):
        self.calls.append((query, max_results))
        if self.raises:
            raise RuntimeError(f"{self.name} 违约抛出来了")
        rows = self._rows(self.calls) if callable(self._rows) else self._rows
        return [dict(r) for r in rows]


def _good(name: str) -> _FakeSource:
    return _FakeSource(name, [_row(name)])


@pytest.fixture
def _clean_probe_state(monkeypatch):
    """把 availability 的进程级缓存钉回"没测过"，测完原样交还。

    _state 是模块级可变全局（同 test_tool_availability 里的处置），不还原会
    把翻转日志和缓存判定泄漏给后面的用例。
    """
    saved = dict(availability._state)
    availability._state["value"] = None
    availability._state["checked_at"] = 0.0
    yield
    availability._state.update(saved)


# ---------- AC-1 链按优先级问源 ----------

def test_the_chain_asks_the_highest_priority_source_first():
    first, second = _good("first"), _good("second")
    rows, used = search_chain.search("西安 人口", sources=[first, second])
    assert used == "first", f"第一源给得出结果却换了源：{used}"
    assert [r["title"] for r in rows] == ["标题 first"]
    assert second.calls == [], "前一源成功时不该去敲下一源"


def test_bing_source_delegates_to_web_search_by_module_attribute(monkeypatch):
    """第一个源必须**调用期**经模块属性取 web_search.search。

    既有测试（test_tools/test_medium2/test_e2e_process_trace 等）全都
    monkeypatch `web_search.search` 这一个属性；BingHtmlSource 若在导入期
    绑死函数引用，那些桩会在一夜之间静默失效——链照常跑真 HTTP，CI 变网络依赖。
    """
    seen = []

    def fake_search(query, *rest):
        seen.append((query, rest))
        return [_row("bing")]

    monkeypatch.setattr(web_search, "search", fake_search)
    rows, used = search_chain.search("某校 简章")
    assert used == "bing" and rows[0]["title"] == "标题 bing"
    assert seen == [("某校 简章", ())], f"缺省调用形态变了：{seen}"
    search_chain.search("某校 简章", 1)
    assert seen[-1] == ("某校 简章", (1,)), f"探测形态没把 max_results 传下去：{seen}"


# ---------- AC-2 拔掉第一源仍出结果，且留痕第二源（验收主场景） ----------

def test_unplugging_the_first_source_still_returns_rows_attributed_to_the_second():
    first = _FakeSource("first", [])
    second = _good("second")
    rows, used = search_chain.search("西安 人口", sources=[first, second])
    assert used == "second", "第一源回空，留痕却没指到真正给出结果的源"
    assert [r["title"] for r in rows] == ["标题 second"]
    assert len(first.calls) == 1, "回空后必须恰好换一次源，不回头重试"


def test_the_tool_artifact_names_the_source_that_actually_answered(monkeypatch):
    """全链到 artifacts 为止：用户侧的留痕通道里必须有"这次用的是谁"。"""
    monkeypatch.setattr(search_chain, "SOURCES",
                        [_FakeSource("坏的", []), _good("好的")])
    monkeypatch.setattr(availability, "note_search_ok", lambda: None)
    resp = builtin_tools.web_search_tool("标题 好的")
    assert resp.success and "标题 好的" in resp.data, resp.data
    art = resp.artifacts
    assert art["kind"] == "web_search" and art["source"] == "好的", art
    assert [r["title"] for r in art["results"]] == ["标题 好的"]


# ---------- AC-3 坏源抛异常只降级，不炸链 ----------

def test_a_source_that_raises_degrades_like_an_empty_one(monkeypatch):
    breaker = _FakeSource("breaker", raises=True)
    keeper = _good("keeper")
    rows, used = search_chain.search("q", sources=[breaker, keeper])
    assert used == "keeper" and rows, "违约抛异常的源把整条链带崩了"


def test_a_raising_source_still_ages_nothing_and_the_tool_survives(monkeypatch, capsys):
    monkeypatch.setattr(search_chain, "SOURCES", [_FakeSource("only", raises=True)])
    monkeypatch.setattr(availability, "note_search_ok", lambda: pytest.fail("空结果不许刷新可用性"))
    out = execute_tool("web_search", {"query": "x"})
    assert "没有返回结果" in out, out
    logged = capsys.readouterr().out
    assert "only" in logged and "抛异常" in logged, f"降级没留一行可查的日志：{logged!r}"


# ---------- AC-4 全链皆空 ----------

def test_an_all_empty_chain_reports_no_rows_and_no_source():
    rows, used = search_chain.search("q", sources=[_FakeSource("a"), _FakeSource("b")])
    assert rows == [] and used is None, "全空时不该硬指一个源"


def test_the_empty_chain_keeps_the_existing_empty_answer_wording(monkeypatch):
    """空结果那句人话一字不动：它是双端行为锁的一部分（"不要据此断定不存在"）。"""
    monkeypatch.setattr(search_chain, "SOURCES", [_FakeSource("a"), _FakeSource("b")])
    monkeypatch.setattr(availability, "note_search_ok", lambda: pytest.fail("空结果不许刷新可用性"))
    resp = builtin_tools.web_search_tool("x")
    assert "这次搜索没有返回结果" in resp.data, resp.data
    assert not resp.artifacts, "没结果就不该有留痕载荷"


# ---------- AC-5 可用性探测是链路维度 ----------

def test_the_probe_is_green_when_any_source_in_the_chain_answers(monkeypatch, capsys,
                                                                 _clean_probe_state):
    """第一源坏、第二源好 → web_search 仍可用，且翻转日志指名给出结果的源。"""
    good = _good("替补")
    monkeypatch.setattr(search_chain, "SOURCES", [_FakeSource("主力", []), good])
    availability._refresh()
    assert availability._state["value"] is True, "链上任一源给得出结果就该判可用"
    logged = capsys.readouterr().out
    assert "替补" in logged, f"翻转日志没指明是哪个源：{logged!r}"


def test_the_probe_goes_red_only_when_every_source_is_empty(monkeypatch, capsys,
                                                            _clean_probe_state):
    bad_a, bad_b = _FakeSource("甲", []), _FakeSource("乙", [])
    monkeypatch.setattr(search_chain, "SOURCES", [bad_a, bad_b])
    availability._refresh()
    assert availability._state["value"] is False
    logged = capsys.readouterr().out
    assert "甲" in logged and "乙" in logged, f"全灭日志该点名整条链：{logged!r}"


# ---------- AC-6 帧与步的既有字节形状不变 ----------

def test_search_frames_without_a_source_keep_their_old_byte_shape():
    rows = [_row("x")]
    legacy = {"type": "search", "id": "c", "query": "q", "results": [
        {"title": "标题 x", "url": "https://x.example/x", "snippet": "摘要 x"}]}
    assert json.loads(json.dumps(se.search_frame("c", "q", rows))) == legacy
    step_legacy = {"kind": "search", "id": "c", "query": "q", "results": legacy["results"]}
    assert json.loads(json.dumps(se.step_search("c", "q", rows))) == step_legacy


def test_search_frames_and_steps_gain_only_a_source_key(monkeypatch):
    frame = se.search_frame("c", "q", [_row("x")], source="second")
    assert frame["source"] == "second"
    assert set(frame) == {"type", "id", "query", "results", "source"}
    step = se.step_search("c", "q", [_row("x")], source="second")
    assert step["source"] == "second" and set(step) == {
        "kind", "id", "query", "results", "source"}


def test_search_source_name_is_capped_like_a_tool_name():
    """源名出自我们自己的代码，封顶仍要有：留痕通道的对端是别人的解析器。"""
    frame = se.search_frame("c", "q", [], source="源" * 500)
    assert len(frame["source"]) <= se.TOOL_NAME_MAX


# ---------- AC-7 定源拍板后的默认链与可配置性（2026-10-05 扩范围） ----------

def test_default_chain_order_is_bocha_then_tavily_then_bing(monkeypatch):
    """用户拍板的优先级：博查主源、Tavily 备用、Bing 爬取兜底。

    不设 SEARCH_SOURCE_ORDER 直接跑 _build_sources()，而不是断言模块级 SOURCES：
    后者取决于本机有没有配环境变量，会把产品默认和部署配置搅在一条断言里。
    """
    monkeypatch.delenv("SEARCH_SOURCE_ORDER", raising=False)
    assert [s.name for s in search_chain._build_sources()] == ["bocha", "tavily", "bing"]


def test_unknown_or_empty_order_never_empties_the_chain(monkeypatch, capsys):
    monkeypatch.setenv("SEARCH_SOURCE_ORDER", "google, bocha ,bogus")
    assert [s.name for s in search_chain._build_sources()] == ["bocha"]
    assert "google" in capsys.readouterr().out, "未知源名要留下可查的一行"

    monkeypatch.setenv("SEARCH_SOURCE_ORDER", ",,")
    assert [s.name for s in search_chain._build_sources()] == ["bing"], \
        "链不许被一次配置笔误清空成零个源"

    monkeypatch.setenv("SEARCH_SOURCE_ORDER", "tavily,bing,tavily")
    assert [s.name for s in search_chain._build_sources()] == ["tavily", "bing"]


def test_no_key_machine_behaves_exactly_like_bing_only(monkeypatch):
    """无 key 硬验收的链端形态：默认三源链在没配凭据时由 bing 给结果。

    conftest 把 SEARCH_KEYS_DB_PATH 指到不存在的文件（= 全链无 key），博查/Tavily
    各自回空换源；这条经由真源对象跑完整链，证明"没 key"不是只测了单源。
    """
    monkeypatch.setattr(web_search, "search", lambda q, *r: [_row("bing")])
    rows, used = search_chain.search("北京 天气")
    assert used == "bing" and rows[0]["title"] == "标题 bing"


# ---------- AC-8 三源全 mock：逐级拔线的降级矩阵（用户点名的验收） ----------

def test_unplug_bocha_tavily_answers_and_bing_is_not_knocked(monkeypatch):
    bing = _FakeSource("bing", [_row("bing")])
    rows, used = search_chain.search(
        "q", sources=[_FakeSource("bocha", []), _good("tavily"), bing])
    assert used == "tavily" and rows[0]["title"] == "标题 tavily"
    assert bing.calls == [], "备用源给得出时不该继续敲兜底源"


def test_unplug_two_levels_lands_on_bing():
    rows, used = search_chain.search(
        "q", sources=[_FakeSource("bocha", raises=True), _FakeSource("tavily", []),
                      _good("bing")])
    assert used == "bing", "拔掉两级后兜底源必须接住整条链"
    assert rows[0]["title"] == "标题 bing"


def test_all_three_mock_sources_dead_ages_availability_to_unavailable(monkeypatch, capsys,
                                                                      _clean_probe_state):
    """全挂 → 可用性判不可用，且翻转日志点名整条链（不依赖真实网络与真实 key）。"""
    monkeypatch.setattr(search_chain, "SOURCES", [
        _FakeSource("bocha", raises=True), _FakeSource("tavily", []),
        _FakeSource("bing", raises=True)])
    availability._refresh()
    assert availability._state["value"] is False
    logged = capsys.readouterr().out
    assert all(n in logged for n in ("bocha", "tavily", "bing")), \
        f"全灭日志该把整条链点名：{logged!r}"
