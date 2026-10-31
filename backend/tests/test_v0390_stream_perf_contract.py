"""v0.39 性能 A 包 · 网页流式渲染的静态契约锁（app.js / style.css）。

改的是什么：runStream 的 paint() 原来每拍都 `MD.render(body, holder.content, true)`
——整段正文重解析 + innerHTML 重建，长回答是 O(n²)，逐 token 洪泛时主线程被重排
吃满。这一版把**流式期间**降成一行 `body.textContent = holder.content`（屏幕上
短暂看到原始 Markdown 标记，是明说的取舍），重画闸门从 rAF 换成固定 50ms 的
setTimeout 节流；终稿仍由 finally 之后的 persistCurrent() + renderMessages()
走 messageNode 里那条完整 MD.render——排版一个字不少，只是不再在流里做。

判据五条：
1. paint 体内没有 MD.render、没有 innerHTML，有 textContent = holder.content；
   贴底 stick 判断与 paintTrace 仍在（它们不属于这次降级）；
2. STREAM_PAINT_MS 是 40..60 的常量，paint 经 setTimeout(paint, STREAM_PAINT_MS)
   节流——chunk 回调里不许每帧直接调 paint，rAF 闸门整条撤下；
3. 起流给 body 挂 is-streaming、finally 摘除；data-live 的设置/摘除原样保留
   （markdown.js 的高亮补扫靠它跳过半截容器）；style.css 里
   .msg.assistant .msg-body.is-streaming 带 white-space: pre-wrap——
   纯文本期不换行等于把整段回答压成一行，这条必须有；
4. 终稿路径完整：messageNode 里 MD.render(body, m.content) 仍在，
   finally 之后 persistCurrent() 与 renderMessages() 仍成对——holder 早在
   发送时进了 state.messages，取消未决 timer 不会丢字；
5. finally 取消未决 timer（迟到的 paint 会打到已被重建的旧节点上）。

反向锁（回退必红，本批实现落地前实测）：把 paint 改回 MD.render(...) 或去掉
节流直接每 chunk 调 paint → 判据 1/2 红；摘掉 is-streaming/pre-wrap → 判据 3 红。
"""
import re

from tests.test_web_trace_panel_parity_v0300_contract import (  # noqa: E402
    APP_JS, CSS, _function_body, _js_code, _text,
)


def _js():
    return _js_code(_text(APP_JS))


def _paint_body(js: str) -> str:
    """`const paint = () => { ... };` 的大括号配平切片（它不是 function 声明，
    _function_body 那把尺子量不到箭头函数）。"""
    at = js.find("const paint = () => {")
    assert at >= 0, "paint 不再是 runStream 里那条箭头函数：锚点得跟着改"
    open_at = js.index("{", at)
    depth = 0
    for i in range(open_at, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[open_at:i + 1]
    raise AssertionError("paint 箭头函数大括号没闭合")


def test_paint_writes_plain_text_only_no_markdown_no_innerhtml():
    js = _js()
    paint = _paint_body(js)
    assert "MD.render" not in paint, "流式 paint 又拿整段正文重跑 Markdown 了：O(n²) 回归"
    assert "innerHTML" not in paint, "paint 里出现 innerHTML：流式重绘面回到全量重建"
    assert "body.textContent = holder.content" in paint, \
        "paint 不再是一行 textContent：流式期渲染面变了"
    # 贴底与过程面板不属于降级，原样在
    assert "host.scrollHeight - host.scrollTop - host.clientHeight" in paint, \
        "贴底 stick 判断没了：流式期会把上翻阅读的人拽回底部"
    assert "paintTrace(node, holder)" in paint, "paint 不再带过程面板重画"


def test_stream_paint_is_settimeout_throttled_not_raf_or_per_chunk():
    js = _js()
    m = re.search(r"const STREAM_PAINT_MS\s*=\s*(\d+)\s*;", js)
    assert m, "STREAM_PAINT_MS 常量没了：节流间隔必须可审计"
    assert 40 <= int(m.group(1)) <= 60, f"节流窗口 {m.group(1)}ms 超出 40..60 的批准区间"
    run = _function_body(js, "runStream")
    assert "setTimeout(paint, STREAM_PAINT_MS)" in run, \
        "paint 不经固定窗口节流：闸门形状变了"
    assert "requestAnimationFrame" not in run and "raf(paint)" not in run, \
        "rAF 闸门没撤干净：两条节流并存等于没有节流"
    # chunk 回调只许"挂起一次重画"，不许直接调 paint
    chunk_arm = run[run.index("(chunk) =>"):run.index("});", run.index("(chunk) =>"))]
    assert "paint()" not in chunk_arm, "每个 chunk 直接调 paint：每 chunk 一次重排回来了"
    assert "schedulePaint()" in chunk_arm or "pending = setTimeout(paint" in chunk_arm, \
        "chunk 回调不再经节流闸门"


def test_streaming_class_and_css_prewrap():
    js = _js()
    run = _function_body(js, "runStream")
    assert 'body.classList.add("is-streaming")' in run, "起流没挂 is-streaming：CSS 那侧没有作用面"
    assert 'body.classList.remove("is-streaming")' in run, "finally 没摘 is-streaming：终稿被 pre-wrap 钉死"
    assert run.index('body.classList.remove("is-streaming")') > run.index("finally {"), \
        "摘 is-streaming 不在 finally：出错/断线那条路会把 class 漏在外面"
    # data-live 的设置/摘除语义原样保留（高亮跳过靠它）
    assert 'body.setAttribute("data-live", "1")' in run and 'body.removeAttribute("data-live")' in run
    css = _text(CSS)
    sel = re.search(r"\.msg\.assistant\s+\.msg-body\.is-streaming\s*\{[^}]*\}", css)
    assert sel, "style.css 没有 .msg.assistant .msg-body.is-streaming 这条规则"
    assert "white-space: pre-wrap" in sel.group(0), \
        "流式期纯文本没有 pre-wrap：整段回答会被压成一行"


def test_final_markdown_render_still_on_message_path():
    js = _js()
    node = _function_body(js, "messageNode")
    assert "MD.render(body, m.content)" in node, "终稿的完整 Markdown 渲染被一起删了：排版回归"
    run = _function_body(js, "runStream")
    assert "persistCurrent()" in run and "renderMessages()" in run, \
        "finally 之后的终稿重渲染链路断了：流式降级就没人补排版了"


def test_finally_cancels_pending_paint_timer():
    js = _js()
    run = _function_body(js, "runStream")
    fin = run[run.index("finally {"):]
    assert "clearTimeout(pending)" in fin, \
        "finally 不取消未决 timer：renderMessages 重建之后迟到的 paint 会打到旧节点"
