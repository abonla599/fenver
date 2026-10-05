"""v0.32 W3「搜索引用上屏 · 来源留痕可见」的双端契约锁。

范围（工作包只动展示层，帧字节一个不改）：
1. 来源显示口径三端逐字锁：SEARCH_SOURCE_PREFIX 与 SEARCH_SOURCE_LABELS 的真源是
   `app/core/stream_events.py` 的常量（先例：approval.py 的 APPROVAL_*），PWA app.js
   与原生 ChatUi.kt 各定义一次，本文件把三份互等——谁漂一个字就红。
2. 搜索步标题行的句子：「搜索：{query} · 来自 {源名}」；没有 source 的步（W1 之前的
   历史数据）必须与 v0.30 已锁文案逐字一致，连前缀一起省。
3. 双端引用条数与 url 逐项一致：真源 search_frame 裁出的 rows 喂给两端各自的 URL 闸
   （node 跑网页的 traceSafeUrl、Python 复刻原生 safeTraceUrl 的判据），href 列表
   逐项相等；渲染侧再锁「每条 results 一行」。
4. 点击可达（跳转意图）：PWA 用 mock url 在 node 里真跑 traceResultNode 断言
   href/target=_blank/rel；原生锁 clickable { onOpenUrl(url) } 只挂在过了闸的 url 上，
   且 MainActivity 的出口是 ACTION_VIEW。

读法沿用本仓库判例：源码当文本读、剥注释后断言字面量；纯函数交给 node 真跑。
不动 v0.29/v0.30/v0.31 任何既有锁的文案。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.core import stream_events as se

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_JS = REPO_ROOT / "backend" / "app" / "web" / "static" / "app.js"
CHAT_KT = (REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" /
           "fenever" / "assistant" / "nativeapp" / "ui" / "ChatUi.kt")
API_KT = (REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" /
          "fenever" / "assistant" / "nativeapp" / "Api.kt")
MAIN_KT = (REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" /
           "fenever" / "assistant" / "nativeapp" / "MainActivity.kt")


def _text(path: Path) -> str:
    assert path.is_file(), f"契约的另一端不在仓库里：{path}"
    return path.read_text(encoding="utf-8")


def _js_code(src: str) -> str:
    """去 JS 注释（// 与 /* */），尊重字符串字面量——注释里的旧文案不许替漂移背书。"""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in ('"', "'", "`"):
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == c:
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _kt_code(src: str) -> str:
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _js_function(name: str) -> str:
    src = _text(APP_JS)
    at = src.find(f"function {name}(")
    assert at >= 0, f"app.js 里没有 function {name}()：这条判据的锚点得跟着改"
    open_at = src.index("{", src.index(")", at))
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[at:i + 1]
    raise AssertionError(f"{name}() 大括号没闭合")


def _js_const(name: str) -> str:
    """`const NAME = ...;`（对象/字符串字面量，单句）。"""
    src = _text(APP_JS)
    m = re.search(rf"const {name} = .*?;", src, re.S)
    assert m, f"app.js 里找不到 const {name}"
    return m.group(0)


_NODE_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const fns = fs.readFileSync(process.argv[2], "utf8");
const driver = fs.readFileSync(process.argv[3], "utf8");
const sc = fs.readFileSync(process.argv[4], "utf8");
const scObj = JSON.parse(sc);
const sandbox = { console, JSON, Math, Object, Array, String, Number, Boolean, Error,
                  RegExp, Promise };
vm.createContext(sandbox);
const script = "const SC = " + sc + ";\n" + fns
  + "\nconst __drive = async () => {\n" + driver + "\n};\n__drive();";
const p = vm.runInContext(script, sandbox);
p.then((r) => process.stdout.write(JSON.stringify(r)),
       (e) => { console.error(e); process.exit(2); });
"""


def _run_node(fn_sources: str, driver: str, scenario: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    d = Path(tempfile.mkdtemp(prefix="w3-citation-"))
    (d / "fns.js").write_text(fn_sources, encoding="utf-8")
    (d / "driver.js").write_text(driver, encoding="utf-8")
    (d / "sc.json").write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
    (d / "harness.cjs").write_text(_NODE_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(d / "harness.cjs"), str(d / "fns.js"),
                        str(d / "driver.js"), str(d / "sc.json")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"harness 自己就跑失败了：\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout)


# 给 node 的最小 DOM 替身：只兜住 traceStepNode/traceResultNode 用到的那几个操作。
_DOM_SHIM = r"""
const URL = function (u) {
  const m = /^https?:\/\/([^\/?#]+)/i.exec(String(u));
  if (!m) throw new Error("bad url");
  this.host = m[1];
};
const document = {
  createElement(tag) {
    const el = { tag, className: "", textContent: "", children: [], attrs: {}, classes: [] };
    el.classList = { add: (c) => el.classes.push(c) };
    el.appendChild = (c) => { el.children.push(c); return c; };
    el.append = (...cs) => cs.forEach((c) => el.children.push(c));
    el.setAttribute = (k, v) => { el.attrs[k] = v; };
    return el;
  },
  createTextNode(t) { return { text: String(t) }; },
};
"""


# ==========================================================================
# 1. 来源显示口径：三端逐字（真源 = stream_events 常量）
# ==========================================================================

def test_server_owns_the_canonical_copy():
    assert se.SEARCH_SOURCE_PREFIX == " · 来自 "
    assert se.SEARCH_SOURCE_LABELS == {"bocha": "博查", "tavily": "Tavily",
                                       "bing": "必应"}


def test_pwa_prefix_and_labels_match_server():
    js = _js_code(_text(APP_JS))
    m = re.search(r'const SEARCH_SOURCE_PREFIX = "([^"]*)"', js)
    assert m, "app.js 不再定义 SEARCH_SOURCE_PREFIX：双端措辞要长岔了"
    assert m.group(1) == se.SEARCH_SOURCE_PREFIX
    table = re.search(r"const SEARCH_SOURCE_LABELS = \{([^}]*)\}", js)
    assert table, "app.js 不再定义 SEARCH_SOURCE_LABELS"
    pairs = dict(re.findall(r'(\w+):\s*"([^"]+)"', table.group(1)))
    assert pairs == se.SEARCH_SOURCE_LABELS


def test_native_prefix_and_labels_match_server():
    kt = _kt_code(_text(CHAT_KT))
    m = re.search(r'const val SEARCH_SOURCE_PREFIX = "([^"]*)"', kt)
    assert m, "ChatUi.kt 不再定义 SEARCH_SOURCE_PREFIX：双端措辞要长岔了"
    assert m.group(1) == se.SEARCH_SOURCE_PREFIX
    table = re.search(r"SEARCH_SOURCE_LABELS =\s*mapOf\(([^)]*)\)", kt)
    assert table, "ChatUi.kt 不再定义 SEARCH_SOURCE_LABELS"
    pairs = dict(re.findall(r'"([^"]+)" to "([^"]+)"', table.group(1)))
    assert pairs == se.SEARCH_SOURCE_LABELS


def test_prefix_is_written_out_only_once_per_client():
    """连接词只许活在常量定义里一次；第二处手写就是「各端各写各的」的开端。"""
    js = _js_code(_text(APP_JS))
    kt = _kt_code(_text(CHAT_KT))
    assert js.count(se.SEARCH_SOURCE_PREFIX) == 1, "app.js 里手写副本出现了第二处"
    assert kt.count(se.SEARCH_SOURCE_PREFIX) == 1, "ChatUi.kt 里手写副本出现了第二处"


# ==========================================================================
# 2. 显示名规则：已知源中文名、未知源原样、空回空（node 真跑网页那一份）
# ==========================================================================

_CASES = ["bocha", "tavily", "bing", "duckduckgo", "  bocha  ", "", "   ", None]


def test_search_source_name_matches_the_server_rule():
    js_src = _js_const("SEARCH_SOURCE_LABELS") + "\n" + _js_const("SEARCH_SOURCE_PREFIX") \
        + "\n" + _js_function("searchSourceName")
    out = _run_node(js_src, "return { got: SC.cases.map((c) => searchSourceName(c)) };",
                    {"cases": _CASES})
    assert out["got"] == [se.source_display_name(c) for c in _CASES], out


def test_native_display_function_follows_the_same_rule():
    kt = _kt_code(_text(CHAT_KT))
    assert "fun searchSourceName(source: String): String" in kt
    body = _span(kt, "fun searchSourceName(source: String): String",
                 "private fun TraceStepRow")
    for token in (".trim()", "if (s.isEmpty()) return \"\"",
                  "SEARCH_SOURCE_LABELS[s] ?: s"):
        assert token in body, f"原生的显示名规则换了（缺 {token!r}）：三端要长岔"


def _span(src: str, start: str, end: str) -> str:
    a, b = src.index(start), src.index(end)
    assert a < b, f"锚点顺序变了（{start} 应该在 {end} 之前）"
    return src[a:b]


# ==========================================================================
# 3. 搜索步标题行：同一句话，双端逐字
# ==========================================================================

_SENTENCES = [
    # (query, source, 期望整句)
    ("西安 人口", "bing", "搜索：西安 人口 · 来自 必应"),
    ("x", "bocha", "搜索：x · 来自 博查"),
    ("x", "tavily", "搜索：x · 来自 Tavily"),
    ("x", "searx", "搜索：x · 来自 searx"),          # 未知源：原样，不编不吞
    ("x", "", "搜索：x"),                              # 没有 source：与 v0.30 锁逐字一致
    ("x", None, "搜索：x"),
]


def _step_label_from_node():
    fns = (_DOM_SHIM + _js_const("TRACE_KIND_CLASS") + "\n"
           + _js_const("SEARCH_SOURCE_LABELS") + "\n"
           + _js_const("SEARCH_SOURCE_PREFIX") + "\n"
           + _js_function("searchSourceName") + "\n"
           + _js_function("traceSafeUrl") + "\n"
           + _js_function("traceResultNode") + "\n"
           + _js_function("traceStepNode"))
    driver = ("return { labels: SC.steps.map((s) => {\n"
              "  const n = traceStepNode(s);\n"
              "  const q = n.children[0];\n"
              "  const t = q.children.find((c) => typeof c.text === \"string\");\n"
              "  return t ? t.text : null;\n"
              "}) };")
    return fns, driver


def test_the_search_title_line_is_the_same_sentence_on_the_pwa():
    fns, driver = _step_label_from_node()
    steps = [{"kind": "search", "id": "c", "query": q,
              "results": [], **({"source": s} if s else {})}
             for q, s, _ in _SENTENCES]
    out = _run_node(fns, driver, {"steps": steps})
    assert out["labels"] == [want for _, _, want in _SENTENCES], out


def test_the_native_title_line_builds_the_same_sentence():
    kt = _kt_code(_text(CHAT_KT))
    m = re.search(r'"搜索：\$\{t\.query\}" \+ if \(src\.isEmpty\(\)\) "" else '
                  r'SEARCH_SOURCE_PREFIX \+ src', kt)
    assert m, "原生的搜索步标题不再是「搜索：q + 前缀 + 源名」这一句：双端措辞长岔了"
    js = _js_code(_text(APP_JS))
    assert '"搜索：" + String(s.query || "")' in js and \
        "(src ? SEARCH_SOURCE_PREFIX + src : \"\")" in js, \
        "网页的搜索步标题不再是同一句拼接：双端措辞长岔了"


# ==========================================================================
# 4. accumulateTrace：source 并进步里、且「没有就不出现这个键」（与服务端 step_search 同先例）
# ==========================================================================

def test_accumulate_keeps_source_and_omits_the_key_when_absent():
    fns = _js_function("accumulateTrace")
    driver = ("const a = { trace: [] }, b = { trace: [] };\n"
              "accumulateTrace(a, SC.with);\naccumulateTrace(b, SC.without);\n"
              "return { with: a.trace[0], without: b.trace[0] };")
    out = _run_node(fns, driver, {
        "with": {"type": "search", "id": "c1", "query": "x", "results": [], "source": "bing"},
        "without": {"type": "search", "id": "c2", "query": "x", "results": []}})
    assert out["with"]["source"] == "bing"
    assert "source" not in out["without"], "空 source 不许以空键的形式被回写进会话"


def test_native_search_pipes_the_source_through():
    api = _kt_code(_text(API_KT))
    kt = _kt_code(_text(CHAT_KT))
    assert 'ChatEvent.Search(sOf(o, "id"), sOf(o, "query"), hits,' in api \
        and 'sOf(o, "source")' in api, "Api.kt 不读 search 帧的 source 键了"
    assert "source = ev.source" in kt, "原生的流内累积丢了 source"
    assert 'source = jtStr(o, "source")' in kt, "原生的历史回放丢了 source"
    assert 'if (t.source.isNotEmpty()) put("source", t.source)' in kt, \
        "原生回写会话的键纪律变了（应与服务端 step_search 同：没有就不写）"


# ==========================================================================
# 5. 引用条数与 url：双端逐项一致（真源 search_frame 的 rows 过两端各自的闸）
# ==========================================================================

_ROWS = [
    {"title": "A", "url": "https://zhuanlan.zhihu.com/p/1", "snippet": "s1"},
    {"title": "B", "url": "http://plain.example/a?x=1&y=2", "snippet": "s2"},
    {"title": "JS", "url": "javascript:alert(1)", "snippet": "坏"},
    {"title": "DATA", "url": "data:text/html,<h1>hi</h1>", "snippet": "坏"},
    {"title": "", "url": "https://no-title.example/x", "snippet": ""},
    {"title": "ONLY-TITLE", "url": "", "snippet": "s"},
] + [{"title": f"N{i}", "url": f"https://n.example/{i}", "snippet": ""}
     for i in range(10)]


def _native_gate(url: str) -> str:
    """ChatUi.kt safeTraceUrl 判据的 Python 复刻（与 test_process_trace_contract 同一先例：
    规则本身由文本锁钉死在原文件里，这里只按钉死的规则算结果）。"""
    u = url.strip()
    low = u.lower()
    return u if low.startswith("http://") or low.startswith("https://") else ""


def test_citation_rows_match_item_by_item_across_both_clients():
    frame = se.search_frame("c1", "西安", _ROWS)
    rows = frame["results"]
    assert 0 < len(rows) <= se.SEARCH_RESULTS_MAX, "真源的封顶规则变了：这条锁的范围要跟着改"
    fns = _js_function("traceSafeUrl")
    out = _run_node(fns, "return { hrefs: SC.urls.map((u) => traceSafeUrl(u)) };",
                    {"urls": [r["url"] for r in rows]})
    native = [_native_gate(r["url"]) for r in rows]
    assert out["hrefs"] == native, "同一份 rows，双端闸给出的可点 url 逐项不一致"
    assert len(out["hrefs"]) == len(rows), "引用条数不是逐条来自 rows"


def test_both_clients_render_one_row_per_hit():
    js = _js_code(_text(APP_JS))
    kt = _kt_code(_text(CHAT_KT))
    assert "(Array.isArray(s.results) ? s.results : []).forEach((r) => {\n" \
        "      list.appendChild(traceResultNode(r));\n" \
        "    });" in js, "网页不再给每条命中画一行"
    assert "t.results.forEach { TraceResultRow(it, onOpenUrl) }" in kt, \
        "原生不再给每条命中画一行"


# ==========================================================================
# 6. 点击可达：mock url 断言跳转意图
# ==========================================================================

def test_pwa_citation_link_carries_the_jump_intent():
    fns, _ = _step_label_from_node()
    driver = ("const step = traceStepNode({ kind: \"search\", id: \"c\", query: \"q\",\n"
              "  results: [SC.hit, { title: \"坏\", url: \"javascript:alert(1)\" }]});\n"
              "const rows = step.children[1].children.map((item) => item.children[0].children[0]);\n"
              "return { okTag: rows[0].tag, href: rows[0].href || null, target: rows[0].target || null,\n"
              "         rel: rows[0].rel || null, badTag: rows[1].tag, badHref: rows[1].href || null,\n"
              "         badClasses: rows[1].classes };")
    out = _run_node(fns, driver, {"hit": {"title": "标题",
                                          "url": "https://mock.example/article"}})
    assert out["okTag"] == "a", "mock url 的引用条标题不是链接：点了不会走"
    assert out["href"] == "https://mock.example/article"
    assert out["target"] == "_blank", "引用链接不开新标签：会把会话整个换掉"
    assert out["rel"] == "noopener noreferrer"
    assert out["badTag"] == "span" and out["badHref"] is None, "非 http url 被升格成了可点链接"
    assert "trace-result-plain" in out["badClasses"]


def test_pwa_link_attributes_are_written_once_in_source():
    js = _js_code(_text(APP_JS))
    span = _span(js, "function traceResultNode", "function setTraceOpen")
    for token in ('title.href = url', 'title.target = "_blank"',
                  'title.rel = "noopener noreferrer"'):
        assert token in span, f"网页引用条的跳转属性不再显式挂出（缺 {token!r}）"


def test_native_citation_click_goes_through_the_gate_to_the_browser():
    kt = _kt_code(_text(CHAT_KT))
    at = kt.index("private fun TraceResultRow")
    body = kt[at:at + 2600]
    assert 'val url = safeTraceUrl(r.url)' in body, "原生引用条不再对每条 url 过本地闸"
    assert "Modifier.weight(1f).clickable { onOpenUrl(url) }" in body, \
        "原生的跳转意图不只在过了闸的分支上：要么丢了点击，要么放了坏链"
    main = _kt_code(_text(MAIN_KT))
    assert "Intent(Intent.ACTION_VIEW, Uri.parse(u))" in main, \
        "原生 onOpenUrl 的出口不再是外部浏览器：引用条点了到不了原站"
