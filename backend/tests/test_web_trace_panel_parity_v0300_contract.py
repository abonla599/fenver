"""v0.30 网页版过程面板与原生对齐：同一句话、同一个秒数、同一套画法、同一条实时条。

用户 2026-10-04 的原话：「网页版跟原生对齐」。上一轮只改了 android-native 那侧，
网页这边还写着「思考过程 · 3 步 · 已搜索 5 个网页」，而原生已经是「查看 3 个步骤 ·
5 个网页」——两套客户端两套说法就是这个特性长歪的方式。

读法沿用本仓库判例（test_process_trace_contract.py / test_native_trace_polish_and_icon_v0300_contract.py）：
CI 里没有 JS 与 Kotlin 的测试运行器，所以把 app.js / style.css / index.html / ChatUi.kt
的**源码当文本读**，纯函数（traceSummaryText / fmtTraceSecs / isTouchPrimary）交给 node
真跑。所有文本判据都先剥注释：注释里那句「原来打的是 ✓」不该冒充成"代码里还有 ✓"。

一条口径先说清楚：模型清单里那枚「当前项 ✓」（`app.js` 的当前项标记与 `ChatUi.kt` 的选择模型页）
**不在本轮收口范围内**——双端都还留着同一枚勾，本身是对齐的。这里的禁字范围只到
过程面板（traceStepNode → paintTrace 那一段）与它用的 CSS。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "backend" / "app" / "web" / "static"
APP_JS = STATIC / "app.js"
CSS = STATIC / "style.css"
HTML = STATIC / "index.html"
CHAT = (REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" /
        "assistant" / "nativeapp" / "ui" / "ChatUi.kt")

# 缺字风险最高的几何字形：原生那台设备已经真机判过"没这个字形 → 实心方块"。
# 面板范围内一个都不许留（模型清单那枚 ✓ 见文件头，双端一致，不在这里）。
# 名单里那三个几何字形用 chr() 拼：编辑链路会吃掉 U+258B / U+25B8 这两个字符，
# 写成字面量就等于名单里悄悄多出两个空串——而 "" in anything 恒真，判据当场
# 从"抓得住"退化成"永远红"或"永远看不见问题"。下面那行 assert 是自证。
PANEL_GLYPHS = ["✓", "✗", chr(0x258B), chr(0x25BE), chr(0x25B8)]
assert all(PANEL_GLYPHS) and len(set(PANEL_GLYPHS)) == 5, "那份名单自己坏了：尺子不可信"


def _text(path: Path) -> str:
    assert path.is_file(), f"契约的另一端不在仓库里：{path}"
    return path.read_text(encoding="utf-8")


def _js_code(src: str) -> str:
    """去掉 JS 的 // 与 /* */ 注释，同时尊重字符串字面量与正则字面量。

    正则那一档不是洁癖：app.js 里 `/^https?:\\/\\/\\S+$/i` 这种行含有 `//`，不认正则字面量
    就会把整行当注释吃掉，判据于是"看不见"后面所有代码——那是最坏的一种绿。
    """
    out = []
    i, n = 0, len(src)
    prev = ""
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
            prev = c
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if c == "/" and prev in ("", "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";"):
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == "/":
                    i += 1
                    break
                i += 1
            prev = "/"
            continue
        out.append(c)
        if not c.isspace():
            prev = c
        i += 1
    return "".join(out)


def _kotlin_code(src: str) -> str:
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in ('"', "'"):
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == quote:
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


def _css_code(src: str) -> str:
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


def _html_code(src: str) -> str:
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


def _span(src: str, start: str, end: str) -> str:
    a, b = src.index(start), src.index(end)
    assert a < b, f"锚点顺序变了（{start} 应该在 {end} 之前）：这条判据的范围失效了"
    return src[a:b]


def _function_body(src: str, name: str) -> str:
    """`function name(...) { ... }` 的大括号配平切片。切不出来直接抛，不静默给空串。"""
    at = src.find(f"function {name}(")
    assert at >= 0, f"{name}() 不在了：这条判据的锚点得跟着改"
    open_at = src.index("{", src.index(")", at))
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at:i + 1]
    raise AssertionError(f"{name}() 大括号没闭合")


_NODE_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const fns = fs.readFileSync(process.argv[2], "utf8");
const driver = fs.readFileSync(process.argv[3], "utf8");
const sc = fs.readFileSync(process.argv[4], "utf8");
const scObj = JSON.parse(sc);
const sandbox = { console, JSON, Math, Object, Array, String, Number, Boolean, Error,
                  RegExp, Promise, Date, setInterval, clearInterval,
                  window: scObj.__window, navigator: scObj.__navigator };
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
    d = Path(tempfile.mkdtemp(prefix="web-trace-parity-"))
    (d / "fns.js").write_text(fn_sources, encoding="utf-8")
    (d / "driver.js").write_text(driver, encoding="utf-8")
    (d / "sc.json").write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
    (d / "harness.cjs").write_text(_NODE_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(d / "harness.cjs"), str(d / "fns.js"),
                        str(d / "driver.js"), str(d / "sc.json")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"harness 自己就跑失败了：\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout)


def _js_function(name: str) -> str:
    src = _text(APP_JS)
    at = src.find(f"function {name}(")
    assert at >= 0, f"app.js 里没有 function {name}()：这一节的判据跟着改名一起失效"
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


# ==========================================================================
# 1. 标题行：同一句话
# ==========================================================================

HEADER_TOKENS = ["查看 ", "收起 ", " 个步骤", " 个网页"]


def test_header_copy_is_the_same_sentence_on_both_clients():
    js = _js_code(_text(APP_JS))
    kt = _kotlin_code(_text(CHAT))
    for token in HEADER_TOKENS:
        assert token in js, f"网页标题行不再说 {token!r}：双端说法长岔了"
        assert token in kt, f"原生标题行不再说 {token!r}：双端说法长岔了"
    # 旧文案必须真的没了，不然"两套说法"只是变成了"三套"
    assert "思考过程 · " not in js and "已搜索" not in js, "网页还留着 v0.29 那套旧文案"
    assert "思考过程 · " not in kt and "已搜索" not in kt, "原生又退回旧文案"


def test_web_header_counts_like_the_native_panel():
    """真跑 traceSummaryText：omitted 那条不算一步，网页与原生同判据。

    把省略记账算进"步数"等于虚报进度——用户拿这个数字判断它想到哪一步了。
    """
    trace = [{"kind": "thinking", "text": "先乘后加"},
             {"kind": "tool", "id": "c_1", "label": "计算 1+1", "pending": False, "ok": True},
             {"kind": "search", "id": "c_9", "query": "西安",
              "results": [{"title": "a", "url": "https://e.example/a"},
                          {"title": "b", "url": "https://e.example/b"}]},
             {"kind": "omitted", "count": 5}]
    driver = ("return { closed: traceSummaryText(SC.trace, false),"
              " open: traceSummaryText(SC.trace, true) };")
    out = _run_node(_js_function("traceSummaryText"), driver, {"trace": trace})
    assert out["closed"] == "查看 3 个步骤 · 2 个网页", out
    assert out["open"] == "收起 3 个步骤 · 2 个网页", out
    empty = _run_node(_js_function("traceSummaryText"),
                      "return { closed: traceSummaryText([], false) };", {})
    assert empty["closed"] == "查看 0 个步骤", empty


# ==========================================================================
# 2. 时长：一个口径
# ==========================================================================

def test_seconds_are_one_rule_on_both_clients():
    js = _js_code(_text(APP_JS))
    kt = _kotlin_code(_text(CHAT))
    assert "(n / 1000).toFixed(1) + \"s\"" in js, "网页的秒数口径换了：双端各报各的"
    assert '"%.1fs".format(ms / 1000.0)' in kt, "原生的秒数口径换了：双端各报各的"
    assert '" ms"' not in js, "网页又回到直接甩毫秒：18734 ms 这种数字人读不动"


def test_fmt_trace_secs_agrees_with_the_native_formatter():
    """同一批毫秒数喂给两边的格式化器，字符串必须逐字相同（node 真跑网页那一份）。"""
    out = _run_node(_js_function("fmtTraceSecs"),
                    "return SC.ms.map(function (m) { return fmtTraceSecs(m); });",
                    {"ms": [0, 8, 999, 1000, 18734, 60500]})
    want = ["%.1fs" % (m / 1000.0) for m in [0, 8, 999, 1000, 18734, 60500]]
    assert out == want, f"网页 {out} != 原生 {want}"


# ==========================================================================
# 3. 标记：一律画，不打字形
# ==========================================================================

def test_panel_marks_are_not_font_glyphs():
    js = _js_code(_text(APP_JS))
    span = _span(js, "function traceStepNode", "function paintTrace")
    left = [g for g in PANEL_GLYPHS if g in span]
    assert not left, f"网页过程面板又拿字形当标记了（{left}）：换台设备就是方块"
    assert 'mark.setAttribute("aria-hidden", "true")' in span, \
        "步前三态没标成装饰：读屏软件那里这一步没头没尾"


def test_the_web_draws_the_three_step_states_and_the_caret():
    css = _css_code(_text(CSS))
    for sel in (".trace-tool-status.ok", ".trace-tool-status.bad", ".trace-tool-status.pending"):
        assert sel in css, f"{sel} 不在了：三态里有一态又靠字形"
    assert "trace-spin" in css, "进行中的转弧没有关键帧"
    caret = _span(css, ".trace-caret {", ".trace-steps {")
    assert "border-right" in caret and "rotate(45deg)" in caret, "箭头不是画的"
    assert ".msg-trace.open .trace-caret" in css and "-135deg" in css, "展开时箭头不转了"
    assert "font-style: italic" not in _span(css, ".trace-thinking", ".trace-tool-line"), \
        "思考行又靠斜体区分：中文没有斜体字形，只会把字拉斜"
    assert "border-left" in _span(css, ".trace-thinking", ".trace-tool-line"), \
        "思考行少了那道竖线：与原生 drawBehind 那条线对不上"


def test_the_blinking_cursor_is_drawn_too():
    css = _css_code(_text(CSS))
    rule = _span(css, ".typing .msg-body::after", "@keyframes blink")
    assert chr(0x258B) not in rule, "光标又是打出来的字形：原生那台设备就是这块绿方块"
    assert "background: var(--accent)" in rule and "width: 3px" in rule, "光标不是画的竖条"


# ==========================================================================
# 4. 实时状态条：本地秒表
# ==========================================================================

def test_live_status_bar_is_wired_on_both_clients():
    html = _html_code(_text(HTML))
    for cls in ("trace-status", "trace-status-spin", "trace-status-label", "trace-status-time"):
        assert cls in html, f"模板里少了 .{cls}：状态条没有壳可画"
    js = _js_code(_text(APP_JS))
    assert "setInterval(traceTickOnce, 100)" in js, "秒表不 ticks：那条状态条就是装的"
    assert "dataset.since" in js and "Date.now() - panel._liveSince" in js, \
        "秒数不是本地现算的：抄一个假进度比没有更糟"
    kt = _kotlin_code(_text(CHAT))
    assert "delay(100)" in kt and "elapsedRealtime()" in kt, \
        "原生那侧的本地秒表没了：双端一条对不上"


def test_the_ticker_is_started_and_stopped_in_pairs():
    """心跳必须起停在同一条流里配对：泄漏一条 100ms 的 interval 比多写两行脏得多。"""
    js = _js_code(_text(APP_JS))
    run = _function_body(js, "runStream")
    assert "traceTickStart()" in run and "traceTickStop()" in run, "起停不配对"
    assert run.index("traceTickStart()") < run.index("finally {"), "起点不在流上"
    assert run.index("traceTickStop()") > run.index("finally {"), \
        "停止没进 finally：出错与断线那两条路会把心跳漏在外面"
    assert "trace-empty" in js and "empty && !live" in js, \
        "trace 还空着时整块面板又藏起来了：手机上看到的就是条空气泡"


def test_the_clock_resets_when_the_running_step_changes():
    js = _js_code(_text(APP_JS))
    paint = _function_body(js, "paintTrace")
    assert "panel._liveKey !== key" in paint and "panel._liveSince = Date.now()" in paint, \
        "计时起点不挂在「是哪一步」上：换步不归零，用户看到的秒数是累计的假账"
    assert 'running.id || running.label || running.name || "step"' in paint, \
        "兜底键没了：id 为空串的那一步永远不重置"
    kt = _kotlin_code(_text(CHAT))
    assert "remember(clockKey)" in kt, "原生那侧的换步归零没了"


def test_live_bar_is_hidden_for_settled_messages():
    """落定的历史消息不许在原地自己数秒（原生同判据：liveLabel=null 时不挂状态条）。"""
    js = _js_code(_text(APP_JS))
    paint = _function_body(js, "paintTrace")
    assert re.search(r'panel\.classList\.toggle\("trace-live", live && !empty\)', paint)
    assert 'bar.classList.add("hidden")' in paint, "非 live 那一支没把状态条收起来"
    kt = _kotlin_code(_text(CHAT))
    assert "if (liveLabel != null) TraceLiveBar" in kt


# ==========================================================================
# 5. 一个字都还没有的那几秒
# ==========================================================================

def test_empty_trace_still_says_its_thinking():
    html = _html_code(_text(HTML))
    assert "trace-now" in html and "正在思考" in html, "空 trace 那一行没了"
    css = _css_code(_text(CSS))
    assert ".msg-trace.trace-empty .trace-head" in css, "空态还顶着一条「查看 0 个步骤」"
    kt = _kotlin_code(_text(CHAT))
    assert "TraceThinkingNow()" in kt and "正在思考" in kt, "原生那侧的「正在思考」没了"


# ==========================================================================
# 6. 减弱动效的退路
# ==========================================================================

def test_every_new_beat_has_a_reduced_motion_way_out():
    """这一版加的三处呼吸（步前转弧、状态条转圈、思考三点）+ 光标，全都得在退路清单里。

    判据是**扫出来的**而不是手写名单：以后谁再给面板加一条 animation，这里当场要求登记。
    """
    css = _text(CSS)
    media = re.search(r"@media[^{]*prefers-reduced-motion[^{]*\{(.*?)\n\}", css, re.S)
    assert media, "prefers-reduced-motion 那条媒体查询没写成块，退路无从谈起"
    block = _css_code(media.group(1))
    code = css.replace(media.group(0), "")   # 退路块自己不算被检查对象
    animated = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", code):
        sel = m.group(1).strip().splitlines()[-1].strip()
        if not (sel.startswith(".trace") or sel.startswith(".typing .msg-body")):
            continue
        if "animation:" in m.group(2):
            animated.append(sel)
    assert len(animated) >= 4, f"扫到的动画比预期还少（{animated}）：读法失效了，不是这里干净了"
    missing = [s for s in animated if s not in block]
    assert not missing, f"这些动画没有减弱动效的退路：{missing}"


# ==========================================================================
# 7. 发完问题收键盘（触屏）
# ==========================================================================

def test_sending_blurs_the_input_only_on_touch_devices():
    js = _js_code(_text(APP_JS))
    stash = _function_body(js, "stashKeyboardAfterSend")
    assert "isTouchPrimary()" in stash and ".blur()" in stash, "发送收键盘那一步没了"
    run = _function_body(js, "runStream")
    assert "stashKeyboardAfterSend()" in run, "runStream 不再收键盘：用户又得手动点一下"
    assert 'if (!isTouchPrimary()) $("input").focus()' in run, \
        "流式收尾又把手机上的键盘顶回来了：刚收的就为它收的"


def test_touch_detection_is_a_real_branch():
    """真跑 isTouchPrimary 的三条路：matchMedia 在位、matchMedia 说不是、老 WebView 兜底。

    钉的是行为而不是"函数里有没有那个词"：判据选错（比如认 touch 事件在不在）会让带触屏
    的笔记本也收键盘，而那种设备收掉键盘纯属添乱——这条只有真跑才抓得住。
    """
    fn = _js_function("isTouchPrimary")
    touch = _run_node(
        fn,
        "window.matchMedia = function (q) { SC.q = q; return { matches: true }; };\n"
        "  return { got: isTouchPrimary(), q: SC.q };",
        {"__window": {}})
    assert touch["got"] is True, touch
    assert "hover: none" in touch["q"] and "pointer: coarse" in touch["q"], touch["q"]
    laptop = _run_node(
        fn,
        "window.matchMedia = function () { return { matches: false }; };\n"
        "  return { got: isTouchPrimary() };",
        {"__window": {}, "__navigator": {"maxTouchPoints": 5}})
    assert laptop["got"] is False, "带触屏的笔记本被判成手机：打字时收键盘是添乱"
    legacy = _run_node(fn, "return { got: isTouchPrimary() };",
                       {"__window": None, "__navigator": {"maxTouchPoints": 5}})
    assert legacy["got"] is True, "老 WebView 没有 matchMedia：兜底那一路没接上"


# ==========================================================================
# 8. 检测器自证：别让一条抓不到东西的判据看起来全绿
# ==========================================================================

def test_the_comment_stripper_does_not_blind_the_detector():
    assert "✓" in _js_code('mark.textContent = "✓";')
    assert "✓" not in _js_code('/* 原来填的是 ✓ */\nmark.textContent = "";')
    assert "✗" not in _js_code('// 注释里的 ✗')
    # 正则字面量里那对 \/\/ 不许被当成行注释，把后面的代码一起吃掉
    probe = 'const re = /^https?:\\/\\/\\S+$/i; const keep = "✗";'
    assert "keep" in _js_code(probe) and "✗" in _js_code(probe), \
        "剥注释的尺子把正则字面量当成了注释：这条判据会瞎"
