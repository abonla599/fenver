"""v0.39 性能 A 包 · 原生流式路径的静态契约锁（ChatUi.kt）。

改的是什么：流式期间组合根上到处**直读** `streamText` 这个 mutableStateOf，
而每个 token 都写它一次——整棵消息列表每个 token 重组一遍；RichText 又对
全文重跑围栏解析 + 加粗注释。这一版把三件事拆开：
1. **发布节流**：content 事件只 `acc.append(ev.text)`，屏上由一条 40ms 的
   ticker 子协程发布快照（`STREAM_PUBLISH_MS`），且 `mine()` 不对号立刻停——
   A 的孤儿流不许把字画到 B；
2. **组合期只读派生量**：`streamVisible`（derivedStateOf，只认"有没有正文"），
   `streamText` 的字符串读取挪进 `item {}` 体内与 effect 协程内；父级条件
   （空态英雄、isLastAssistant、streaming 标记、气泡挂载）一律吃
   `streamVisible/busy`，不再逐 token 翻整页；
3. **流式期纯文本**：RichText 加 `streaming` 参数，true 时一条裸 Text 早退，
   不跑 parseRichTextParts/boldAnnotated——流式期屏幕上是原始 Markdown 标记，
   终稿落进 messages 后照旧走完整解析（取舍与网页 paint 同一句话）。

贴底拆两条：落定 effect 的 key 换成 `messages.size, streamActive,
streamTrace.size, streamTraceChars`（落定判据仍在协程内读 streamText）；
流式跟随独立成 `LaunchedEffect(streamActive)` 的 80ms 循环，近底闸门
（lastVisible >= total-2 才跟）原样搬过去。

判据全部读**剥注释后**的源码文本（沿用 _code/_block 那把既有尺子）；
反向锁见各条 assert 的措辞——把 ticker 撤掉、把 streamVisible 换回直读、
把早退拆掉，对应判据当场红。
"""
import re
from pathlib import Path

from tests.test_apk_release_discipline import _code  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CHATUI = REPO / "android-native" / "app" / "src" / "main" / "java" / "xyz" / \
    "fenever" / "assistant" / "nativeapp" / "ui" / "ChatUi.kt"


def _chat() -> str:
    return _code(CHATUI)


def _block(code: str, anchor: str) -> str:
    at = code.find(anchor)
    assert at >= 0, f"找不到锚点 {anchor!r}"
    open_at = code.index("{", at)
    depth = 0
    for i in range(open_at, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[open_at:i + 1]
    raise AssertionError(f"{anchor!r} 的大括号没闭合")


# ------------------------------------------------ 发布节流：ticker + mine() --

def test_stream_publish_constant_in_approved_range():
    code = _chat()
    m = re.search(r"private const val STREAM_PUBLISH_MS = (\d+)L", code)
    assert m, "STREAM_PUBLISH_MS 文件级常量没了：发布节拍必须可审计"
    assert 30 <= int(m.group(1)) <= 50, f"发布窗口 {m.group(1)}ms 超出 30..50 批准区间"


def test_content_event_only_appends_and_ticker_publishes_with_guard():
    code = _chat()
    body = _block(code, "fun streamInto() {")
    assert "is ChatEvent.Content -> acc.append(ev.text)" in body, \
        "content 事件又在逐帧写 streamText：每 token 一次整页重组回来了"
    assert "streamText = acc.toString()" not in body, \
        "旧发布形状残留：帧回调里还在直接发布整段快照"
    assert "delay(STREAM_PUBLISH_MS)" in body, "ticker 不按固定窗口跑"
    assert re.search(r"if \(!mine\(\)\) break", body), \
        "ticker 没有 mine() 停笔守卫：A 的孤儿流会把字画到 B"
    assert "if (snapshot != streamText) streamText = snapshot" in body, \
        "ticker 的发布行变了形状：无变化不写、对号才写是这条设计的本体"


def test_finally_cancels_ticker_before_clearing_slots():
    code = _chat()
    fin = _block(code, "fun streamInto() {")
    at = fin.index("finally {")
    tail = fin[at:]
    assert "publishJob?.cancel()" in tail, "finally 没先撤 ticker"
    assert tail.index("publishJob?.cancel()") < tail.index("streamText = null"), \
        "先清槽后撤表：被撤的 ticker 可能又写回一份孤儿快照"
    assert "streamActive = false" in tail and "busy = false" in tail, \
        "finally 没落 streamActive/busy：is-streaming 语义与 busy 闸一起漏"


def test_stream_active_and_derived_visibility():
    code = _chat()
    assert "var streamActive by remember { mutableStateOf(false) }" in code, \
        "streamActive 状态位没建：起流/收尾两条路无从翻转"
    assert "val streamVisible by remember { derivedStateOf { streamText != null } }" in code, \
        "streamVisible 派生没建：父级组合又要直读 streamText"
    assert "import androidx.compose.runtime.derivedStateOf" in code, \
        "derivedStateOf 没 import：Kotlin 编译期就红"
    body = _block(code, "fun streamInto() {")
    assert "streamActive = true" in body, "起流没翻 streamActive"
    assert body.index("streamActive = true") < body.index("Api.streamChat("), \
        "streamActive 晚于起流才置真：跟随循环起跑晚一拍"


# -------------------------------------------- 组合期读取全部换成派生量 --

def test_parent_composition_reads_never_touch_stream_text_directly():
    code = _chat()
    assert "if (live && streamVisible) item {" in code, \
        "流式气泡的挂载条件没吃派生量：?.let 的直读形状回来了"
    assert "streamText?.let" not in code, \
        "组合期还在 streamText?.let：每个 token 整列表重组的病根回来了"
    assert "StreamingBubble(streamText.orEmpty()" in code, \
        "streamText 的读取没挪进 item 体内：气泡拿不到最新快照"
    assert "(!busy && !streamVisible)" in code, "空态英雄的闸门没换派生量"
    assert "(!live || !streamVisible)" in code, "isLastAssistant 的判据没换派生量"
    assert "busy || streamVisible" in code, "streaming 标记没换派生量"
    # streamText == null 这一读法只许活在两条 effect 的协程体内（落定判据/跟随条件），
    # 把这两块摘掉后，剩下的（组合期）文本里不许再出现这一读。
    settle = _block(code, "LaunchedEffect(messages.size, streamActive, "
                          "streamTrace.size, streamTraceChars) {")
    follow = _block(code, "LaunchedEffect(streamActive) {")
    rest = code.replace(settle, "").replace(follow, "")
    assert "streamText == null" not in rest, \
        "父级组合里仍有 streamText == null 的直读（落定判据只许在 effect 协程内）"


# ------------------------------------------------ 贴底两条：落定 + 80ms 跟随 --

def test_settled_effect_keys_swapped_to_stream_active():
    code = _chat()
    body = _block(code, "LaunchedEffect(messages.size, streamActive, "
                       "streamTrace.size, streamTraceChars) {")
    assert "streamText == null && !busy" in body, \
        "落定判据（effect 协程内读）没了：v0.25.1 的旧高度单发公式会回来"
    assert "withFrameNanos" in body and "repeat(MAX_BOTTOM_CORRECTIONS)" in body, \
        "落定的随帧量高与有界校正被一起删了"
    assert "while (" not in body and "while(" not in body, \
        "落定效果里冒出 while：跟随独立成环之后这里更不许有循环"


def test_follow_loop_is_standalone_eighty_ms_with_same_gate():
    code = _chat()
    m = re.search(r"private const val STREAM_FOLLOW_MS = (\d+)L", code)
    assert m, "STREAM_FOLLOW_MS 文件级常量没了：跟随节拍必须可审计"
    assert int(m.group(1)) == 80, f"跟随窗口实测应为 80ms，实得 {m.group(1)}"
    body = _block(code, "LaunchedEffect(streamActive) {")
    assert "while (streamActive && streamText != null)" in body, \
        "跟随循环的退出条件缺 streamActive 或 streamText（协程内读，不是组合键）"
    assert "delay(STREAM_FOLLOW_MS)" in body, "每轮先 delay：不许忙等"
    assert re.search(r"lastVisible\s*<\s*total\s*-\s*2", body), \
        "近底闸门没搬进跟随环：上翻阅读会被每个 token 拽回底部"
    assert "scrollToItem(total - 1, MAX_SCROLL_OFFSET)" in body, \
        "跟随不再是顶到滚动边界：v0.28 的贴底手段被换掉了"


# ------------------------------------------------ RichText 流式早退与终稿 --

def test_rich_text_streaming_early_exit_skips_parsing():
    code = _chat()
    assert "internal fun RichText(text: String, color: Color, streaming: Boolean = false)" in code, \
        "RichText 的 streaming 参数没了：终稿默认全解析的形状变了"
    body = _block(code, "internal fun RichText(text: String, color: Color, streaming: Boolean = false)")
    at_guard = body.find("if (streaming)")
    at_parse = body.find("parseRichTextParts(")
    assert 0 <= at_guard < at_parse, "早退不在解析之前：流式期还要重切围栏就是没改"
    assert re.search(r"if \(streaming\) \{\s*Text\(text, fontSize = 16\.sp, lineHeight = 24\.sp, color = color\)\s*return", body), \
        "流式那一支不是单条裸 Text + return：加粗注释仍在流式期烧 CPU"
    assert body.count("boldAnnotated(") == 1, "非流式那侧的加粗调用数了数变了：终稿渲染面被动过"


def test_streaming_bubble_passes_true_and_final_rows_stay_default_false():
    code = _chat()
    bubble = _block(code, "private fun StreamingBubble")
    assert "RichText(text, scheme.onSurface, streaming = true)" in bubble, \
        "流式气泡没传 streaming=true：它还在逐帧重解析整段正文"
    assert "RichText(m.content, scheme.onSurface)" in code, \
        "终稿消息行不再走默认全解析：排版被流式早退一起短路了"
