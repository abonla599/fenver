"""浏览器/WebView 直连搜索的前后端同构锁——v0.32 W2。

直连这一路没有任何运行时中介：前端自己 POST 源站。后端（W1 的
search_api_sources）与前端（设置页 app.js、探测页 probe.html）是三份各写各的代码，
一旦形状漂移——字段名差一个、URL 差一段、判空少一条——症状是"手机上直连永远红"，
而两边各自的测试全都绿。这个文件就是不让那件事发生：

· URL、请求体字段、结果提取路径、判空规则：逐字与后端常量/实现锁成同字；
· key 的三条红线在源码层面可静态检查（只进 Bearer 头、不拼 URL、错误分支不读响应体）；
· api.js 那三个调用只许走本站相对路径——本机 key 与本站服务器，两边永不见面。

改这份文件里任何一条断言前，先想清楚你动的是**锁**还是**被锁住的东西**。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.providers import PLACEHOLDER_HINTS
from app.core.search_keys import ALLOWED_SOURCES
from app.tools import web_search
from app.tools.search_api_sources import BochaSource, TavilySource

WEB = Path(__file__).resolve().parent.parent / "app" / "web"
APP_JS = (WEB / "static" / "app.js").read_text(encoding="utf-8")
API_JS = (WEB / "static" / "api.js").read_text(encoding="utf-8")
INDEX = (WEB / "static" / "index.html").read_text(encoding="utf-8")
PROBE = (WEB / "site" / "probe.html").read_text(encoding="utf-8")


def _region(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


# ---------------------------------------------------------------- 端点同字 --

def test_index_html_endpoints_are_the_backend_registry_verbatim():
    """data-search-endpoints 住在 HTML 而不是 JS：JS_FILES 有一条"只许相对路径"的
    既有锁（test_web_pwa），源站绝对地址进了 app.js 就会撞上去——而撞了之后的
    坏修法是把锁放宽。放在属性里，两边都体面。"""
    raw = re.search(r"data-search-endpoints='([^']+)'", INDEX).group(1)
    eps = json.loads(raw)
    assert set(eps) == set(ALLOWED_SOURCES), "可登记的源与可直连的源必须是同一份名单"
    assert eps["bocha"]["url"] == BochaSource.URL
    assert eps["tavily"]["url"] == TavilySource.URL
    assert all(cfg.get("label") for cfg in eps.values())


def test_probe_page_carries_the_same_two_urls():
    assert BochaSource.URL in PROBE and TavilySource.URL in PROBE
    # 旧锁的形状不松动：probe 的每一行 fetch 都打到页面上定义的 endpoint 取值器
    for ln in (l for l in PROBE.splitlines() if "fetch(" in l):
        assert "endpoint()" in ln, "有 fetch 没走 endpoint()/searchendpoint()：" + ln


# ---------------------------------------------------------------- 形状同构 --

def test_settings_page_body_shape_matches_the_backend():
    build = _region(APP_JS, "function sdBuildBody", "function sdPick")
    assert '{ query, max_results: count }' in build, "Tavily 请求体形状漂了（后端：{query, max_results}）"
    assert ('{ query, count, freshness: "noLimit", summary: false }' in build
            or 'query, count, freshness: "noLimit", summary: false' in build), \
        "博查请求体形状漂了（后端：{query, count, freshness:noLimit, summary:false}）"


def test_probe_body_shape_matches_the_backend():
    assert re.search(r"\(\s*q,\s*n\s*\)\s*=>\s*\(\{ query: q, max_results: n \}\)", PROBE), \
        "probe 的 Tavily 请求体形状漂了"
    assert re.search(r"\(\s*q,\s*n\s*\)\s*=>\s*\(\{ query: q, count: n, "
                     r'freshness: "noLimit", summary: false \}\)', PROBE), \
        "probe 的博查请求体形状漂了"


def test_result_extraction_paths_match_the_backend():
    """提取路径与判空规则：后端是 data.webPages.value / results，判空是
    「缺标题或缺 http 网址的行不给」——前端两处各抄一份，钉在这里。"""
    direct = _region(APP_JS, "async function searchDirect", "function sdSetStatus")
    assert ".data" in direct and ".webPages" in direct and ".value" in direct
    assert "data.results" in direct
    assert '"name", "title"' in direct and '"content", "snippet"' in direct
    rows = _region(APP_JS, "function sdRows", "async function searchDirect")
    assert 'url.startsWith("http")' in rows, "判空规则漂了：后端 _rows 只收 http 网址的行"
    probe_bocha = re.search(r"rows:\s*\(j\)\s*=>[^\n]*", PROBE).group(0)
    assert "webPages" in probe_bocha and "value" in probe_bocha


def test_direct_count_is_the_backend_cap():
    assert int(re.search(r"const SD_COUNT = (\d+)", APP_JS).group(1)) == web_search.MAX_RESULTS


def test_direct_timeout_is_the_probe_watchdog():
    ms = int(re.search(r"const SD_TIMEOUT_MS = (\d+)", APP_JS).group(1))
    probe = int(re.search(r"IDLE_LIMIT_MS\s*=\s*(\d+)", PROBE).group(1))
    assert ms == probe, "设置页与探测页的耐心不该是两个数"


def test_placeholder_hints_are_one_list_not_two():
    js = re.search(r"const SD_PLACEHOLDER_HINTS = \[([^\]]+)\]", APP_JS).group(1)
    hints = tuple(re.findall(r'"([^"]+)"', js))
    assert set(hints) == set(PLACEHOLDER_HINTS), "本机闸门与后端闸门必须同一组形状"


# ---------------------------------------------------------------- key 红线 --

def test_key_only_ever_rides_in_the_authorization_header():
    direct = _region(APP_JS, "async function searchDirect", "function sdSetStatus")
    assert '"Authorization": "Bearer " + key' in direct
    assert direct.count("+ key") == 1, "key 第二次参与拼接：查一查是不是拼进 URL 了"
    assert "credentials: \"omit\"" in direct, "不许带着本站 cookie 去敲第三方"
    assert "res.text()" not in direct, "错误分支读响应体：上游回显请求头时 key 搭顺风车"
    assert "console." not in direct, "直连函数里不许把任何东西打进控制台"


def test_local_key_never_meets_the_own_server():
    """本机 key（localStorage）与本站 API（API.*）在同一行相遇 = 有人开始
    把私钥上送本站——正是 W2「只留在用户输入的那一侧」的反面。"""
    for ln in APP_JS.splitlines():
        if "sdLocalKey(" in ln:
            assert "API." not in ln, "本机 key 被喂给了本站接口：" + ln.strip()
    for ln in PROBE.splitlines():
        if '$("sdkey")' in ln:
            assert ln.count('$("sdkey")') == 1
    assert PROBE.count('$("sdkey").value') == 1, "探测页读这把 key 的地方多于一处"
    run = _region(PROBE, "async function runSearchDirect", "$(\"run\").onclick")
    assert '$("sdkey").value' in run, "唯一那处读取应当只在 runSearchDirect 里"
    assert "res.text()" not in run, "第 7 项也不读错误响应体：报告是要贴到公开处的"
    assert '"Bearer " + key' in run


def test_api_js_search_key_calls_are_relative_and_never_url_the_key():
    assert 'request("/v1/search-keys")' in API_JS
    assert 'request("/v1/search-keys/" + encodeURIComponent(source), { method: "PUT", body: { api_key } })' \
        in API_JS, "登记走 body，不进 URL——这条就是 key 红线的 api.js 侧"
    assert 'request("/v1/search-keys/" + encodeURIComponent(source), { method: "DELETE" })' in API_JS
