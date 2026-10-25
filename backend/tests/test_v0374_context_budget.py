"""v0.37.4 模型侧上下文预算：一页正文 + 来源行必须在 3998 字以内。

`executor.clip_for_model` 是**出口兜底**，不是配额：被它截断时正文中段整段消失，
模型却可能仍然自信地引用。本版的设计目标是工具层就把长度定死（计划 §2.3 的
3716 字上限），让执行器的「中段已省略」永不出现。这里用最坏构造复算：

1. host 253 字（DNS 名字上限）+ path 500 字（来源行截到 300）+ 正文 10000 字
   （`extract_text` 截到 3000 并带截断标记）→ `to_string()` 仍 ≤3716；
2. 正文正好 3000 字没有截断标记、3001 字必须有；两条都 ≤3716；
3. 上面每一条都过一遍 `clip_for_model`，必须原样返回（不触发第 2 次省略）。
"""
import pytest

from app.tools import executor, extract, fetch_core, result_store, web_read


@pytest.fixture(autouse=True)
def _clean_store():
    result_store.clear()
    yield
    result_store.clear()


def _mint(url="https://example.com/a", owner="u"):
    ids = result_store.remember(
        [{"title": "标题", "url": url, "snippet": "摘要"}], owner=owner)
    assert ids[0], f"构造的 URL 本该可签发：{url}"
    return ids[0]


def _fake_fetch(monkeypatch, outcome):
    def fake(_url, **_kwargs):
        return outcome
    monkeypatch.setattr(fetch_core, "fetch", fake)


def _ok_outcome(url, text, nbytes):
    return fetch_core.FetchOutcome(
        ok=True, text=text, status="ok", host="example.com", path="/a",
        bytes=nbytes, hops=0, final_url=url, final_status=200,
        content_type="text/plain")


def test_worst_case_page_fits_without_the_executor_clipping(monkeypatch):
    host = ".".join(["a" * 63, "a" * 63, "a" * 63, "a" * 61])
    assert len(host) == 253
    path = "/" + "p" * 499                       # 来源行按 300 字截断
    text = extract.extract_text("text/plain", "长" * 10000)
    assert "〔正文截断：共 10000 字，此处只保留前 3000 字〕" in text
    url = f"https://{host}{path}"
    rid = _mint(url=url, owner="u")
    _fake_fetch(monkeypatch, _ok_outcome(url, text, 30000))

    resp = web_read.read(rid, user_id="u")
    assert resp.success is True, resp.error
    rendered = resp.to_string()
    assert len(rendered) <= 3716, f"最坏构造 {len(rendered)} 字，超计划上限"
    assert "中段已省略" not in rendered, "执行器出口的省略说明出现了，预算设计失败"
    assert executor.clip_for_model(rendered) == rendered, "执行器仍在裁剪 web_read 的输出"


@pytest.mark.parametrize("body_len,has_marker", [(3000, False), (3001, True)])
def test_body_boundary_3000_has_no_marker_and_3001_has_one(
        monkeypatch, body_len, has_marker):
    rid = _mint()
    text = extract.extract_text("text/plain", "字" * body_len)
    assert ("正文截断" in text) is has_marker
    _fake_fetch(monkeypatch, _ok_outcome("https://example.com/a", text, body_len))

    resp = web_read.read(rid, user_id="u")
    assert resp.success is True, resp.error
    assert ("正文截断" in resp.data) is has_marker
    rendered = resp.to_string()
    assert len(rendered) <= 3716
    assert executor.clip_for_model(rendered) == rendered
