"""API-key 搜索源的 HTTP 客户端（v0.32 W1：博查主源、Tavily 备用源）。

源合同与爬取源一致（见 search_chain）：回 [{title,url,snippet}]，失败一律回空、
不抛；max_results 走同一个 _clamp 口径，空查询直接回空。出站四件套与
web_search.py 一一对应——有界超时、不跟跳转、系统信任锚且验主机名、边读边判的
字节上限——唯一区别是 GET 回 HTML 换成 POST 回 JSON。

密钥来自 core.search_keys（缺失/占位 = 未启用：不发任何请求就回空，链自动换源）。
本模块不打任何日志、不 print 异常：这是全站第一次把 API key 放进出站请求，
「key 绝不落日志」最稳的实现就是这里根本没有任何一行输出——httpx 的异常串
理论上只含请求行，但我们不押注在"理论上"。

字段口径（2026-10-05 按官方文档核对；两家都是 Bearer 认证）：
- 博查  POST https://api.bochaai.com/v1/web-search
  请求体 {query, count, freshness:"noLimit", summary:false}；
  结果在 data.webPages.value[]，标题字段在官方响应里叫 name、摘要叫 snippet
  （开了 summary 才会另有一份长 summary——留作回退，别赌对面默认值不变）。
- Tavily POST https://api.tavily.com/search
  请求体 {query, max_results}；结果在顶层 results[]，字段 title/url/content。

v0.32 W4 多了 `search_with_reason`：源合同那个 `search` 原样保留（回列表），它只是
把带归因的那一份投影成列表。归因值必须与 core/search_usage.py 的写死枚举同名——
枚举漂移的代价由合同测试钉（test_v032_w4_search_usage_contract.py），不靠人记忆。
"""
import json

import httpx

from app.core import search_keys
from app.core.tls import system_ssl_context
from app.tools.web_search import TIMEOUT_SECONDS, _clamp, _clean

API_MAX_BYTES = 1 * 1024 * 1024    # JSON 结果比 HTML 页小一个量级；超了照样当失败


def _ask(url: str, key: str, payload: dict, extract):
    """POST 一次问上游，回 `(rows, reason)`。

    rows 的判空口径与原来逐字一致：非 200、超时、TLS 失败、坏 JSON、超大、坏结构
    ——全是 []。v0.32 W4 多带回来的那个 reason 只服务用量账本（core/search_usage.py
    的写死枚举），不参与任何判空判断：链仍然只看 rows。
    """
    try:
        with httpx.Client(verify=system_ssl_context(), timeout=TIMEOUT_SECONDS,
                          follow_redirects=False) as client:
            with client.stream("POST", url, json=payload,
                               headers={"Authorization": f"Bearer {key}",
                                        "Content-Type": "application/json"}) as response:
                if response.status_code != 200:
                    return [], "non_200"
                chunks, total = [], 0
                for part in response.iter_bytes():
                    total += len(part)
                    if total > API_MAX_BYTES:
                        return [], "oversize"
                    chunks.append(part)
    except Exception:
        return [], "network"
    try:
        rows = extract(json.loads(b"".join(chunks).decode("utf-8", "replace")))
    except Exception:
        # 坏 JSON / 结构不是预期的形状：与"200 但一条也认不出"归到同一档，
        # 因为对上游而言这两件事是同一句话——回来的东西我们用不了。
        return [], "parse_empty"
    return rows, ("ok" if rows else "parse_empty")


def _first(item: dict, fields) -> str:
    """按候选字段顺序取第一个清洗后非空的值（上游字段名两家常不一致）。"""
    for field in fields:
        text = _clean(item.get(field) or "")
        if text:
            return text
    return ""


def _rows(items, title_fields, snippet_fields):
    """把上游的行洗成 {title,url,snippet}：缺标题或缺网址的行丢掉——与爬取源
    "认不出就不给"同一个判空口径，宁缺毋滥，空是链换源的唯一信号。"""
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _first(item, title_fields)
        url = str(item.get("url") or "").strip()
        if title and url.startswith("http"):
            rows.append({"title": title, "url": url,
                         "snippet": _first(item, snippet_fields)})
    return rows


class BochaSource:
    """博查 AI 搜索（主源）。未配 key 即未启用：不联网，直接回空给链换源。

    `search` 是源合同（回列表），`search_with_reason` 是它的多带一栏版：同一个实现，
    前者只是把后者投影成列表——与 tools/executor.py 的 `execute_tool` /
    `execute_tool_detailed` 同一套"唯一实现 + 薄投影"，不允许两份逻辑各跑各的。
    """

    name = "bocha"
    URL = "https://api.bochaai.com/v1/web-search"

    def search_with_reason(self, query, max_results=None):
        want = _clamp(max_results)
        if not str(query or "").strip():
            return [], "empty_query"
        key = search_keys.get(self.name)
        if not key:
            return [], "not_enabled"
        rows, reason = _ask(self.URL, key,
                           {"query": query, "count": want, "freshness": "noLimit",
                            "summary": False},
                           lambda data: _rows(
                               ((data.get("data") or {}).get("webPages") or {}).get("value") or [],
                               ("name", "title"), ("snippet", "summary")))
        return rows[:want], reason

    def search(self, query, max_results=None):
        return self.search_with_reason(query, max_results)[0]


class TavilySource:
    """Tavily（备用源）。同上，未启用不发请求；归因口径与 BochaSource 一份实现。"""

    name = "tavily"
    URL = "https://api.tavily.com/search"

    def search_with_reason(self, query, max_results=None):
        want = _clamp(max_results)
        if not str(query or "").strip():
            return [], "empty_query"
        key = search_keys.get(self.name)
        if not key:
            return [], "not_enabled"
        rows, reason = _ask(self.URL, key, {"query": query, "max_results": want},
                            lambda data: _rows(data.get("results") or [],
                                               ("title",), ("content", "snippet")))
        return rows[:want], reason

    def search(self, query, max_results=None):
        return self.search_with_reason(query, max_results)[0]
