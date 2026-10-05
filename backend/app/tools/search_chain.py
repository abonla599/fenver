"""多源搜索的降级链：按优先级逐源尝试，前一源回空就换下一源。

统一源接口（链上所有源实现都必须遵守的合同）：
- `name: str` —— 源名，进留痕（artifacts/trace）供用户侧核对"这次用的是谁"；
- `search(query, max_results) -> list` —— 每项含 {title, url, snippet}；
  失败一律回空列表、绝不抛——沿用 web_search.search 的判空口径（非 200、超时、
  解析不出全是 []），空列表是 availability 判定"这个源现在不好用"的唯一信号。
- （v0.32 W4 可选）`search_with_reason(query, max_results) -> (list, str)` —— 同一个
  调用多带一个写死归因。链能取就取、取不到就退化到 ok/no_result，所以**加了它不
  破坏源合同**：`search` 仍然是唯一必须实现的那个，返回值一个字没变。

为什么要链而不是单源：web_search 的可用性是测出来的（availability 用同一个函数
真查询），单源时源站一改版工具就整体从清单消失。链让"某个源坏了"降级成
"换下一个源"，而不是"整个功能没了"。

默认优先级（2026-10-05 用户拍板）：**bocha → tavily → bing**。
可用环境变量 SEARCH_SOURCE_ORDER（逗号分隔的源名）重排；未知源名会被丢弃并打一行
日志，整份配置没有一个已知源时退回 bing——链不许被一次配置笔误清空成零个源。
API 源（博查/Tavily，客户端在 search_api_sources.py）未配 key 时判"未启用"：
不发请求、直接回空换源，所以没有 key 的机器上这条链的行为与只有爬取源时完全一致。
"""
import os

from app.tools import web_search
from app.tools.search_api_sources import BochaSource, TavilySource

DEFAULT_ORDER = "bocha,tavily,bing"


class BingHtmlSource:
    """爬取源（兜底）：现有 Bing 爬取原样收编。

    只是一层接口壳——解析器、出站四件套、判空口径全部留在 web_search，
    且必须经模块属性在调用期取函数：既有测试都 monkeypatch
    `web_search.search`，导入期绑死引用会让那条链上的桩全部失效。
    它是唯一不需要凭据的源，也因此是配置坏掉时的那个保底。
    """

    name = "bing"

    def search_with_reason(self, query, max_results=None):
        """爬取源的归因只能到「回空、说不清为什么」这一档，这是代码决定的不是偷懒：
        `web_search.search` 的合同就是"非 200 / 超时 / 超大 / 解析不出——全是 []"，
        而链必须**调用期**经模块属性取那一个函数（既有测试全部 monkeypatch 它，
        改去调别的函数会让那些桩一夜失效、CI 变成真打外网）。拿不到细分支就不编一个。
        """
        rows = (web_search.search(query) if max_results is None
                else web_search.search(query, max_results))
        return rows, ("ok" if rows else "no_result")

    def search(self, query, max_results=None):
        return self.search_with_reason(query, max_results)[0]


_REG = {src.name: src for src in (BochaSource(), TavilySource(), BingHtmlSource())}


def _build_sources():
    """按 SEARCH_SOURCE_ORDER 装配链。位置即优先级：前一源回空才轮到下一源。"""
    names = [n.strip().lower()
             for n in os.getenv("SEARCH_SOURCE_ORDER", DEFAULT_ORDER).split(",")]
    ordered, seen = [], set()
    for name in names:
        if not name:
            continue
        source = _REG.get(name)
        if source is None:
            print(f"⚠️ 搜索链配置里有未知源名 {name!r}，已忽略")
            continue
        if name not in seen:
            ordered.append(source)
            seen.add(name)
    return ordered or [_REG["bing"]]


SOURCES = _build_sources()


def _ask_source(source, query, max_results):
    """问一个源，带回 (rows, reason)。

    有 `search_with_reason` 就用它（API 源给得出非 200/网络/超大/解析空这些细归因）；
    只有合同那个 `search` 的源（链上注入的 mock 源、任何后来的第三方源）就退化到
    ok / no_result——**退化而不是猜**：归因枚举里 `no_result` 那一档的存在理由就是
    "这个源说不清自己为什么空"。名字取自 core/search_usage.py 的写死枚举。
    """
    ask = getattr(source, "search_with_reason", None)
    if ask is not None:
        return ask(query) if max_results is None else ask(query, max_results)
    rows = (source.search(query) if max_results is None
            else source.search(query, max_results))
    return rows, ("ok" if rows else "no_result")


def search(query, max_results=None, sources=None, attempts=None):
    """跑一次降级链，返回 (rows, source_name)；全链皆空返回 ([], None)。

    `sources` 参数是给合同测试注入 mock 源的口子，缺省用模块级 SOURCES。
    `attempts` 是 v0.32 W4 给用量账本留的**出参**：调用方传一个 list 进来，链把每一
    次逐源尝试以 {"source", "reason"} 追加进去。它默认 None，所以既有调用方
    （含 availability 那个每 15 分钟一次的探针）一个字节都不用改，也不会在没人问的
    时候攒出一份没人读的数据。
    捕获源抛出的异常是防御性冗余：源合同是"失败回空不抛"，但一个坏源抛出来
    不该把整条链和它上游的可用性判定一起拖成噪音——处理方式与回空一致：
    打一行日志，换下一源。
    """
    for source in SOURCES if sources is None else sources:
        try:
            rows, reason = _ask_source(source, query, max_results)
        except Exception as e:
            print(f"⚠️ 搜索源 {getattr(source, 'name', '?')} 抛异常，按空处理并换下一源：{e}")
            rows, reason = [], "source_raised"
        if attempts is not None:
            attempts.append({"source": getattr(source, "name", "?"), "reason": reason})
        if rows:
            return rows, source.name
    return [], None
