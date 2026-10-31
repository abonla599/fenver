"""`POST /v1/search/source` 的行为锁（v0.39 L-1）。"""

from app.tools import search_api_sources


def test_proxy_returns_rows_and_reason(client, enforced, monkeypatch):
    rows = [{"title": "结果", "url": "https://example.com/a", "snippet": "摘要"}]
    monkeypatch.setattr(search_api_sources, "search_named",
                        lambda name, query, n: (rows, "ok"))
    hdr = enforced("搜索用户")
    res = client.post("/v1/search/source",
                      json={"source": "bocha", "query": "测试", "max_results": 3},
                      headers=hdr)
    assert res.status_code == 200, res.text
    assert res.json() == {"rows": rows, "reason": "ok"}


def test_proxy_rejects_unknown_source_and_blank_query(client, enforced):
    hdr = enforced("搜索用户2")
    bad = client.post("/v1/search/source",
                      json={"source": "definitely-not-a-source", "query": "x"},
                      headers=hdr)
    assert bad.status_code == 404
    blank = client.post("/v1/search/source",
                        json={"source": "bocha", "query": "   "}, headers=hdr)
    assert blank.status_code == 400


def test_proxy_reports_not_enabled_without_leaking_a_key(client, enforced, monkeypatch):
    monkeypatch.setattr(search_api_sources, "search_named",
                        lambda name, query, n: ([], "not_enabled"))
    hdr = enforced("搜索用户3")
    res = client.post("/v1/search/source",
                      json={"source": "tavily", "query": "x"}, headers=hdr)
    assert res.status_code == 200
    assert res.json() == {"rows": [], "reason": "not_enabled"}
    assert "sk-" not in res.text


def test_proxy_requires_authentication(client, enforced):
    hdr = enforced("搜索用户4")
    res = client.post("/v1/search/source",
                      json={"source": "bocha", "query": "x"})
    assert res.status_code == 401
    # 带正常凭据仍在同一本付费上游账内可过
    assert hdr
