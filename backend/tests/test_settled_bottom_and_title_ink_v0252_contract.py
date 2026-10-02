"""v0.25.2 安卓（Kotlin 壳）两处体验回归锁：落定贴底 + 顶栏标题的墨色。

背景（都是真机用户反馈，不是假想敌）：
① v0.25.1 把"生成期间贴底"算对了，但【回答落定】那一拍仍是旧的单发公式——
   streamText 刚置 null 时 layoutInfo 里还是流式气泡的旧高度，算出的 offset
   偏小，贴的是已经消失的那只气泡的底；长回答的结尾悬在视口外，用户"想看刚
   回复的答案还得往下扒"。网页的权威语义是两条分开的：流式 paint() 本来贴底
   才跟随（app.js:1556），落定 renderMessages 无条件 scrollTop=scrollHeight
   （app.js:1323）。安卓落定必须"随帧重贴直到高矮量实"，单发一次不算贴底。
② 顶栏 .topbar 标题 Text 没写 color，Compose 落到默认纯黑，深色主题下黑字压
   在深蓝黑玻璃底上几乎看不见。网页 h1 走 --text（主题墨色），安卓必须显式跟
   MaterialTheme.colorScheme.onSurface。

本机没有 Android SDK，跑不了 :app:testDebugUnitTest，所以全是"读源文本"的
形状锁（先例见 test_v025_android_resume_contract.py 的 _strip_kotlin_comments）；
assembleDebug 兜编译，真实手感仍需真机。

判据对着当前 Kotlin 是红的：实现把这两处装回去之后才该绿。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHATUI_KT = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / \
    "fenever" / "assistant" / "nativeapp" / "ui" / "ChatUi.kt"
STATIC = REPO_ROOT / "backend" / "app" / "web" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_kotlin_comments(src: str) -> str:
    """去掉 // 与 /* */ 注释，尊重字符串字面量（照 test_v025_android_resume_contract 的尺子）。"""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"' or c == "'":
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
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _kt_code() -> str:
    return _strip_kotlin_comments(_read(CHATUI_KT))


def _settle_effect_body(code: str) -> str:
    """取 LaunchedEffect(messages.size, streamText) 的函数体（大括号配对截断）。"""
    m = re.search(r"LaunchedEffect\(messages\.size,\s*streamText\)\s*\{", code)
    assert m, "找不到贴底跟随的 LaunchedEffect(messages.size, streamText)：这一整块被删了？"
    i = m.end() - 1
    depth = 0
    while i < len(code):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[m.end() - 1:i + 1]
        i += 1
    raise AssertionError("LaunchedEffect 体大括号不配对，测试的截断器读不下去了")


# ---------- ① 落定贴底：随帧重贴，不许再用单发旧高度 ----------

def test_native_settled_reply_resticks_after_layout_is_measured():
    """回答落定必须等新一帧量出真实高度后再贴：withFrameNanos 随帧重贴是唯一正解。

    钉三件：落定判据在场（streamText == null && !busy）、随帧等待在场
    （withFrameNanos）、贴底滚动顶到**滚动边界**（scrollToItem 到最后一项 +
    MAX_SCROLL_OFFSET 越界夹底）。
    v0.28 改判：贴底手段从"上一帧量到的高度做减法（lastH - vh）"换成"越界 offset
    夹到最大滚动位置"——落定那一拍条目还会事后长高（操作行晚一拍才挂出来），
    按旧高度贴的底会把条目尾巴推进输入卡后面（真机「输入框老是挡着上面的输出」）。
    高度会撒谎，边界不会。退出判据也换成结果导向：最后一项底边进了视口才收工。
    """
    body = _settle_effect_body(_kt_code())
    assert "streamText == null" in body and "!busy" in body, \
        "落定拍没有单独分支：v0.25.1 的单发公式还会用流式旧高度骗人"
    assert "withFrameNanos" in body, \
        "落定没有随帧重贴：新一帧量到真实高度之前滚的 offset 是旧气泡的，结尾仍悬在视口外"
    assert "scrollToItem" in body and "MAX_SCROLL_OFFSET" in body, \
        "贴底不再是【顶到滚动边界】：v0.28 修的就是条目事后长高把尾巴藏进输入卡，" \
        "退回拿旧高度做减法就回退成那个 bug"
    assert re.search(r"last\.offset\s*\+\s*last\.size\s*<=\s*[\w.]*viewportEndOffset", body), \
        "落定的收工判据不再是【最后一项底边进了视口】——没贴实就停表等于没修"


def test_native_streaming_follow_still_gated_on_already_at_bottom():
    """生成期间仍是"本来贴底才跟随"：上翻阅读不被拽回（网页 paint() 的 stick 闸，不许丢）。"""
    body = _settle_effect_body(_kt_code())
    assert re.search(r"lastVisible\s*<\s*total\s*-\s*2", body), \
        "流式贴底闸门没了：每个 token 都把上翻阅读的人拽回底部（网页 app.js:1556 不这样）"


# ---------- ② 顶栏标题：显式主题墨色，不许落成默认纯黑 ----------

def test_native_topbar_title_uses_theme_ink_not_default_black():
    """顶栏对话标题必须显式 color = onSurface（网页 .topbar h1 走 --text 的同义）。

    Text(topTitle 从起点到闭合括号之间要找得到 color 且是 onSurface——Compose 不写
    color 落的是默认黑，深色主题下"标题纯黑不好辨认"就是这条没写。
    """
    code = _kt_code()
    m = re.search(r"Text\(\s*topTitle[\s\S]{0,400}?\)\s*\n", code)
    assert m, "顶栏标题 Text(topTitle 不见了"
    block = m.group(0)
    assert re.search(r"color\s*=\s*MaterialTheme\.colorScheme\.onSurface\b", block), \
        "顶栏标题没显式 onSurface：默认纯黑压在深色玻璃底上，用户认不出标题"


# ---------- 网页权威出处仍在（对照失效就喊人，不许安卓自创语义） ----------

def test_web_reference_semantics_are_still_there():
    """两条对照都还在网页源码里：paint 的 stick 闸与 renderMessages 的无条件滚底。"""
    js = _read(STATIC / "app.js")
    assert "stick = host.scrollHeight - host.scrollTop - host.clientHeight" in js, \
        "网页流式贴底闸读不到了：安卓这条锁的对照失效"
    assert re.search(r"function renderMessages[\s\S]{0,900}?host\.scrollTop\s*=\s*host\.scrollHeight", js), \
        "网页 renderMessages 结尾的无条件滚底读不到了：落定语义的对照失效"
