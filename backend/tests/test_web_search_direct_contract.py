"""服务端搜索代理的契约锁（v0.39 L-1 收口后）。

旧「本机直连」把源站 key 放在 localStorage：任意 XSS 都能读走，审计 L-1 记的就是
这一条。现在浏览器不再存 key，搜索走 `POST /v1/search/source`，key 只在服务端由
`search_api_sources` 取用。这份文件钉三件事：

1. 设置页不再出现 `sdkey.` / `localStorage` / `searchDirect` 这一套；
2. 前端只调 `/v1/search/source`，源名与后端白名单同源；
3. 探测页（probe.html）仍是人工直连、key 只进 Authorization 头——它是诊断页，
   不读 localStorage、不写 cookie，和设置页不是同一条路。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.search_keys import ALLOWED_SOURCES
from app.tools.search_api_sources import BochaSource, TavilySource

WEB = Path(__file__).resolve().parent.parent / "app" / "web"
APP_JS = (WEB / "static" / "app.js").read_text(encoding="utf-8")
API_JS = (WEB / "static" / "api.js").read_text(encoding="utf-8")
INDEX = (WEB / "static" / "index.html").read_text(encoding="utf-8")
PROBE = (WEB / "site" / "probe.html").read_text(encoding="utf-8")
MAIN = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text(encoding="utf-8")
SOURCES = (Path(__file__).resolve().parent.parent
           / "app" / "tools" / "search_api_sources.py").read_text(encoding="utf-8")


def _region(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


def test_index_html_labels_are_the_backend_registry_verbatim():
    """源名→显示名仍是同一份名单；URL 已经不再进前端（查询走服务端）。"""
    raw = re.search(r"data-search-endpoints='([^']+)'", INDEX).group(1)
    eps = json.loads(raw)
    assert set(eps) == set(ALLOWED_SOURCES), "可显示的源与后端白名单必须是同一份"
    assert all((cfg.get("label") or "").strip() for cfg in eps.values())
    assert all("url" not in cfg for cfg in eps.values()), \
        "前端不再直连源站，data 属性里不该再留源站 URL"


def test_probe_page_carries_the_same_two_urls():
    """probe.html 是人工诊断页，仍是直连；它的 URL 与后端常量逐字同源。"""
    assert BochaSource.URL in PROBE and TavilySource.URL in PROBE
    for ln in (l for l in PROBE.splitlines() if "fetch(" in l):
        assert "endpoint()" in ln, "有 fetch 没走 endpoint()/searchendpoint()：" + ln


def test_settings_page_has_no_local_search_key_storage():
    region = _region(APP_JS, "/* ---------------- 搜索源", "/* ---------------- 设置弹层")
    assert "sdkey." not in region, "设置页又出现本机 key 前缀了"
    assert "localStorage" not in region, "搜索 key 又回 localStorage 了"
    assert "searchDirect" not in region, "浏览器直连函数又回来了"
    assert "$(\"sdKey\")" not in region and "$(\"sdSaveBtn\")" not in region


def test_settings_page_uses_the_server_proxy():
    assert "API.searchSource(source, query, SD_COUNT)" in APP_JS
    assert 'request("/v1/search/source", { method: "POST"' in API_JS
    assert "body: { source, query, max_results }" in API_JS


def test_server_proxy_uses_the_same_source_registry():
    assert "_search_key_gate(req.source)" in MAIN, "代理没走源名白名单"
    assert "search_api_sources.search_named(req.source" in MAIN, "代理没落到共享源实现"
    assert "_BY_NAME = {source.name: source for source in (BochaSource(), TavilySource())}" \
        in SOURCES, "源名注册表漂了"


def test_probe_key_only_ever_rides_in_the_authorization_header():
    run = _region(PROBE, "async function runSearchDirect", "$(\"run\").onclick")
    assert '"Bearer " + key' in run
    assert PROBE.count('$("sdkey").value') == 1, "探测页读 key 的地方多于一处"
    assert "res.text()" not in run, "错误分支不读响应体：上游可能回显请求头"
    assert "localStorage." not in PROBE and "document.cookie" not in PROBE


def test_api_js_search_key_calls_are_relative_and_never_url_the_key():
    assert 'request("/v1/search-keys")' in API_JS
    assert 'request("/v1/search-keys/" + encodeURIComponent(source), { method: "PUT", body: { api_key } })' \
        in API_JS, "登记走 body，不进 URL——这条就是 key 红线的 api.js 侧"
    assert 'request("/v1/search-keys/" + encodeURIComponent(source), { method: "DELETE" })' in API_JS
