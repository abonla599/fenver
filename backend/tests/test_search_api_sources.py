"""v0.32 W1 扩范围：博查/Tavily 两个 API 源的合同测试（全 mock，不碰真网络与真 key）。

钉四件事：
1. **无 key 硬验收**——没有凭据时该源不发任何请求、直接回空（链等价于纯 Bing）。
   这是判"未启用"的最强形式：连出站构造函数都不许被碰一下。
2. 请求与响应形状按 2026-10-05 官方文档核对的口径：博查 Bearer 认证、请求体含
   query/count，结果在 data.webPages.value[]（标题字段官方叫 name，title 留作回退）；
   Tavily 请求体含 query/max_results，结果在顶层 results[] 的 title/url/content。
3. 出站四件套对 API 源同样成立：有界超时、不跟跳转、系统信任锚且验主机名、
   边读边判的字节上限；非 200、坏 JSON、超大、坏结构一律回空不抛。
4. key 绝不外泄：不出现在任何输出/日志里，对外只许 mask_key 的「末四位」。

httpx.Client 整个换成双重对象：构造参数、请求行（method/url/json/headers）都记下来，
"未启用不联网"和"出站形状对"两类断言共用这一份证据。
"""
import json
import ssl

import pytest

from app.core import search_keys
from app.tools import search_api_sources
from app.tools.search_api_sources import BochaSource, TavilySource
from app.tools.web_search import TIMEOUT_SECONDS

KEY = "sk-live-ABCDEFGH1234"


class _Resp:
    def __init__(self, status=200, chunks=(b"{}",)):
        self.status_code = status
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        return iter(self._chunks)


class _Double:
    """httpx.Client 的双重对象：记录每一次构造与每一次请求。"""

    def __init__(self):
        self.constructions = []     # httpx.Client(**kwargs) 的 kwargs
        self.requests = []          # {method,url,json,headers}
        self.responses = []          # 依序弹出的 _Resp 队列

    def install(self, monkeypatch):
        monkeypatch.setattr(search_api_sources.httpx, "Client", self._factory)

    def _factory(self, **kwargs):
        self.constructions.append(kwargs)
        resp = self.responses.pop(0) if self.responses else _Resp()
        double = self

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def stream(self, method, url, json=None, headers=None):
                double.requests.append({"method": method, "url": url,
                                        "json": json, "headers": headers})
                return resp
        return _Client()


@pytest.fixture
def double(monkeypatch):
    d = _Double()
    d.install(monkeypatch)
    return d


@pytest.fixture
def keys(tmp_path, monkeypatch):
    """写一份临时 search_keys.json 并把环境变量指过去；返回一个改字典的闭包。"""
    path = tmp_path / "search_keys.json"

    def _write(**values):
        if values:
            path.write_text(json.dumps(values), encoding="utf-8")
        elif path.exists():
            path.unlink()
        monkeypatch.setenv("SEARCH_KEYS_DB_PATH", str(path))
    return _write


# ---------- 1. 无 key 硬验收：未启用 = 零出站 ----------

def test_no_key_source_never_touches_the_network(double, keys):
    """用户此刻没有 key：整条链必须照常工作，而未启用的源连构造都不许发生。

    判据故意苛刻——不是"回空"就算过，而是 `httpx.Client` 一次都没被调用。
    否则"先握手再收 401 回空"的实现也能骗过只断言返回值的测试。
    """
    keys()  # 文件不存在
    assert BochaSource().search("北京 天气") == []
    assert TavilySource().search("北京 天气") == []
    assert double.constructions == [] and double.requests == []


def test_placeholder_key_counts_as_disabled(double, keys):
    """形如占位符的 key 与没有 key 同罪：它骗过 `if not key` 的老毛病不能复发。"""
    keys(bocha="sk-your-key-here", tavily="待填")
    assert search_keys.get("bocha") == ""
    assert BochaSource().search("北京 天气") == []
    assert TavilySource().search("北京 天气") == []
    assert double.constructions == []


def test_blank_query_returns_empty_even_with_a_key(double, keys):
    keys(bocha=KEY)
    assert BochaSource().search("   ") == []
    assert double.constructions == []


# ---------- 2. 请求与响应形状（以官方文档为准） ----------

def test_bocha_request_and_official_response_shape(double, keys):
    keys(bocha=KEY)
    double.responses.append(_Resp(chunks=(json.dumps({
        "data": {"webPages": {"value": [
            {"name": "博查标题", "url": "https://a.example/x", "snippet": "博查摘要"},
            {"title": "回退标题", "url": "https://b.example/y", "summary": "长摘要回退"},
        ]}},
    }, ensure_ascii=False).encode(),)))
    rows = BochaSource().search("西安 人口", 2)

    assert rows == [{"title": "博查标题", "url": "https://a.example/x", "snippet": "博查摘要"},
                    {"title": "回退标题", "url": "https://b.example/y", "snippet": "长摘要回退"}], \
        f"官方字段口径（name/snippet 及其回退）没兑现：{rows}"
    req = double.requests[0]
    assert req["method"] == "POST" and req["url"] == BochaSource.URL
    assert req["headers"]["Authorization"] == f"Bearer {KEY}"
    assert req["json"]["query"] == "西安 人口" and req["json"]["count"] == 2


def test_tavily_request_and_response_shape(double, keys):
    keys(tavily=KEY)
    double.responses.append(_Resp(chunks=(json.dumps({
        "results": [
            {"title": "Tavily标题", "url": "https://t.example/x", "content": "正文摘要"},
            {"title": "没有网址", "url": ""},
        ],
    }, ensure_ascii=False).encode(),)))
    rows = TavilySource().search("西安 人口", 4)

    assert rows == [{"title": "Tavily标题", "url": "https://t.example/x",
                     "snippet": "正文摘要"}], \
        f"results[] 的 title/url/content 没映射成同一形状：{rows}"
    req = double.requests[0]
    assert req["url"] == TavilySource.URL
    assert req["headers"]["Authorization"] == f"Bearer {KEY}"
    assert req["json"] == {"query": "西安 人口", "max_results": 4}


def test_max_results_is_clamped_like_the_crawl_source(double, keys):
    """max_results 是模型张嘴要的数字，_clamp 这道闸对 API 源同样必须生效。"""
    keys(bocha=KEY)
    from app.tools import web_search
    value = json.dumps({"data": {"webPages": {"value": [
        {"name": f"标题{i}", "url": f"https://i.example/{i}", "snippet": "s"}
        for i in range(9)]}}}).encode()
    double.responses.append(_Resp(chunks=(value,)))
    rows = BochaSource().search("q", 999)
    assert len(rows) == web_search.MAX_RESULTS
    assert double.requests[0]["json"]["count"] == web_search.MAX_RESULTS


def test_rows_without_title_or_http_url_are_dropped(double, keys):
    """缺标题或缺网址的行丢掉：与爬取源"认不出就不给"同一个判空口径。"""
    keys(tavily=KEY)
    double.responses.append(_Resp(chunks=(json.dumps({
        "results": [
            {"title": "", "url": "https://x.example"},
            {"title": "没有网址", "url": "ftp://x.example"},
            "不是字典",
            {"title": "好行", "url": "https://ok.example", "content": "ok"},
        ],
    }).encode(),)))
    rows = TavilySource().search("q")
    assert [r["title"] for r in rows] == ["好行"]


# ---------- 3. 出站四件套与全量吞错 ----------

def test_egress_locks_apply_to_api_posts(double, keys):
    """有界超时、不跟跳转、系统信任锚 + 验主机名——爬取源的四件套一件不少。"""
    keys(bocha=KEY)
    BochaSource().search("q", 1)
    kw = double.constructions[0]
    assert kw["timeout"] == TIMEOUT_SECONDS
    assert kw["follow_redirects"] is False
    ctx = kw["verify"]
    assert isinstance(ctx, ssl.SSLContext), f"信任锚不是系统 SSLContext：{ctx!r}"
    assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("resp", [
    _Resp(status=401),
    _Resp(status=200, chunks=(b"<html>not json</html>",)),
    _Resp(status=200, chunks=(b"{}",)),                       # 坏结构：没有 webPages
    _Resp(status=200, chunks=(b'{"data":', b'null}')),        # 分块 + null 中段
], ids=["非200", "坏JSON", "缺字段", "分块坏结构"])
def test_every_upstream_failure_shape_degrades_to_empty(double, keys, resp):
    keys(bocha=KEY)
    double.responses.append(resp)
    assert BochaSource().search("q", 1) == []


def test_oversized_response_is_treated_as_failure(double, keys, monkeypatch):
    """字节上限边读边判：超了就不拼完整、直接回空——与爬取源同一条纪律。"""
    monkeypatch.setattr(search_api_sources, "API_MAX_BYTES", 16)
    keys(bocha=KEY)
    double.responses.append(_Resp(chunks=(b"x" * 64,)))
    assert BochaSource().search("q", 1) == []


def test_a_raising_transport_degrades_to_empty(double, keys, monkeypatch):
    """源合同的最后一道：任何异常都不许逃出源层（这里连构造都抛）。"""
    keys(bocha=KEY)

    def boom(**kwargs):
        raise OSError("transport died")
    monkeypatch.setattr(search_api_sources.httpx, "Client", boom)
    assert BochaSource().search("q", 1) == []


# ---------- 4. key 绝不外泄 ----------

def test_the_key_appears_nowhere_but_the_masked_last_four(double, keys, capsys):
    keys(bocha=KEY)
    double.responses.append(_Resp(chunks=(json.dumps({
        "data": {"webPages": {"value": [
            {"name": "标题", "url": "https://a.example", "snippet": "s"}]}}}).encode(),)))
    rows = BochaSource().search("q", 1)
    assert KEY not in json.dumps(rows, ensure_ascii=False)
    assert KEY not in capsys.readouterr().out, "本模块承诺过一行输出都没有：key 有了顺风车"
    assert search_keys.masked("bocha") == "末四位 1234"


def test_registered_keys_get_redacted_from_sanitized_logs(keys):
    """读一次密钥存储就顺手把值登记进 logsanitizer：万一 key 蹭进别的日志行也有兜底。

    只断言"登记过 + redact 打码"，不还原全局词表：登记的是这把假 key，
    泄漏到别的用例也只是多一条永远匹配不上的掩码词。
    """
    from app.core import logsanitizer
    keys(bocha=KEY)
    search_keys.get("bocha")
    assert KEY in logsanitizer.registered_terms()
    assert KEY not in logsanitizer.redact(f"上游报错说 {KEY} 无效")
