"""原生气泡必须真的把 `**加粗**` 画成粗体，而不是把星号原样上屏（v0.29.2）。

用户点名的那件事：「我发现我的软件回复的时候里面有很多'*'号，看着很不舒服。」
截图里是 `**🍜 省事版（30 分钟，一锅出）**` 带着星号出现。

诊断结论先记在这里，免得下一个人又去查正则：**原生从来就没有实现过加粗**。
`RichText` 只切 ``` 代码围栏，围栏之外一律当纯文本交给 `Text()`。所以不是转义
规则写错、不是 emoji 或全角括号不匹配，而是那一层根本没有；网页版（marked +
DOMPurify）一直是好的，于是同一句话在两端长得不一样——这条锁守的就是这个分裂。

CI 里没有 Kotlin 测试运行器（`.github/workflows` 只跑 `assembleDebug`），沿用
`test_process_trace_contract.py` 的做法：把 Kotlin **源码当文本**读，判据钉在函数
体内而不是整文件 grep。而"读源码"最容易自欺的地方是——解析函数写得再对，渲染那
一行忘了用它，屏幕上照样是星号。所以这里除了把 `boldSegments` 的算法逐行搬进
Python 真跑一遍，还有一条**渲染支路**的锁：`RichText` 画正文的那一句必须经过
`boldAnnotated`，且不许再出现把裸 `seg` 交给 `Text()` 的写法。

诚实边界：这套判据证明的是"逻辑与接线都对"，不是"屏幕上真的粗了"。真机效果要看
v0.29.2 的 APK；如果 Compose 侧还有别的样式覆盖（例如 `Text` 上写死 `fontFamily`
或非默认 `fontWeight`），那条只能靠肉眼。已知未修：`StreamResponse` 的自动跟随挂在
`messages.size, streamText` 上，思考面板变高不会把列表带着往下走。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
KOTLIN = (REPO_ROOT / "android-native" / "app" / "src" / "main" / "java"
          / "xyz" / "fenever" / "assistant" / "nativeapp")
CHAT_UI = KOTLIN / "ui" / "ChatUi.kt"


def _text(path: Path) -> str:
    assert path.is_file(), f"契约的另一端不在仓库里：{path}"
    return path.read_text(encoding="utf-8")


def _kotlin_body(src: str, anchor: str, label: str) -> str:
    """从 anchor 起做大括号配平，截出那一块函数体（把搜索面收到真代码上）。"""
    at = src.find(anchor)
    assert at >= 0, f"{label}：找不到锚点 {anchor!r}——那一处结构变了，锁得跟着改读法"
    open_at = src.index("{", at)
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at:i + 1]
    raise AssertionError(f"{label}：大括号没闭合")


# --------------------------------------------------------------------------
# 参考实现：把 Kotlin 的逐行语义搬进 Python，用来真跑几条判据
# --------------------------------------------------------------------------

def _reference_bold_segments(src: str):
    """与 ChatUi.kt `boldSegments` 一行对一行：只认成对的 `**`，未闭合的原样留着。"""
    out = []
    rest = src
    while True:
        open_at = rest.find("**")
        if open_at < 0:
            if rest:
                out.append((rest, False))
            break
        if open_at > 0:
            out.append((rest[:open_at], False))
        rest = rest[open_at + 2:]
        close = rest.find("**")
        if close < 0:
            out.append(("**" + rest, False))
            break
        out.append((rest[:close], True))
        rest = rest[close + 2:]
    return out


def _kotlin_reference_agrees_with_source():
    """反把参考实现的形状对着 Kotlin 函数体核一遍：Kotlin 改了算法，这里要先红。

    不然会出现最坏的一种绿：Python 参考实现跟着断言跑得欢，Kotlin 那侧早已换成
    `Regex("\\*\\*(.+?)\\*\\*")`，屏幕上又是另一回事。
    """
    body = _kotlin_body(_text(CHAT_UI), "internal fun boldSegments", "boldSegments")
    # 三个不可少的动作：找开标记、找闭标记、闭合失败时把 "**" 原样吐回去
    assert body.count('.indexOf("**")') == 2, "开/闭标记应当各查一次 indexOf(\"**\")"
    assert '"**" + rest' in body, "未闭合的 ** 必须原样保留（宁可晚一帧变粗，不许吞字）"
    assert "substring(open + 2)" in body and "substring(close + 2)" in body, \
        "标记本身要跳过，否则星号会被带进正文"
    assert "Pair<String, Boolean>" in body, "分段结果的形状变了：渲染那侧要跟着改"
    return body


def test_closed_pairs_render_bold_and_drop_the_markers():
    """截图里那句话：成对 `**` 之间的内容算粗体，星号本身不许出现在任何一段里。"""
    _kotlin_reference_agrees_with_source()
    line = "**🍜 省事版（30 分钟，一锅出）**：先把鸡腿剁块。"
    segs = _reference_bold_segments(line)
    assert segs == [("🍜 省事版（30 分钟，一锅出）", True), ("：先把鸡腿剁块。", False)], segs
    assert "**" not in "".join(s for s, _ in segs), "星号还挂在屏幕上就是没修"


def test_a_single_asterisk_is_left_alone():
    """单个 `*` 是算术，不是强调："2 * 3 * 4" 不许被切坏，也不许被吞掉。"""
    segs = _reference_bold_segments("先算 2 * 3 * 4，再乘 5")
    assert segs == [("先算 2 * 3 * 4，再乘 5", False)], segs


def test_an_unclosed_pair_keeps_every_character_it_has():
    """流式期间只有开头没有闭合：原样显示，包括那两个星号。

    这条是流式客户端特有的：正文一片一片到，`**省事版` 会真的作为中间态出现在屏幕
    上。任何"等闭合再渲染"的实现都会在这一刻把已经到手的字吞掉，用户看到的是字数
    倒退——比多两颗星号更让人以为程序坏了。
    """
    segs = _reference_bold_segments("下面这句**省事版（30 分")
    assert "".join(s for s, _ in segs) == "下面这句**省事版（30 分", segs
    assert all(not bold for _, bold in segs), f"没闭合的一律不算粗体：{segs}"
    # 补上后半截之后必须翻成粗体：证明"晚一帧"不是"永远不粗"
    segs2 = _reference_bold_segments("下面这句**省事版（30 分）**")
    assert ("省事版（30 分）", True) in segs2, segs2


def test_multiple_pairs_alternate_and_the_whole_text_survives():
    """多对加粗：拼回去必须一个字不少（星号除外），顺序不许被打乱。"""
    src = "开头**甲**中间**乙**结尾"
    segs = _reference_bold_segments(src)
    assert [b for _, b in segs] == [False, True, False, True, False], segs
    assert "".join(s for s, _ in segs) == "开头甲中间乙结尾", segs


def test_the_render_branch_uses_the_bold_helper_not_the_raw_segment():
    """接线锁：`RichText` 画正文的那一句必须经过 `boldAnnotated`。

    整文件 grep 会给出假的安心——`boldSegments` 定义了、单测也能跑，但渲染那行还是
    `Text(seg, ...)`，用户屏幕上一个字都不会变。这里把判据钉在 `RichText` 函数体内，
    并反向要求"裸 seg 交给 Text()"那种写法不再存在。
    """
    src = _text(CHAT_UI)
    body = _kotlin_body(src, "internal fun RichText", "RichText")
    assert "boldAnnotated(" in body, "RichText 没走加粗渲染：那段函数只是挂在仓库里没人调用"
    assert not re.search(r"Text\(\s*seg\s*[,)]", body), \
        "RichText 里还有一处把裸 seg 直接交给 Text()：星号会原样上屏"
    ann = _kotlin_body(src, "private fun boldAnnotated", "boldAnnotated")
    assert "FontWeight.Bold" in ann, "加粗段没上粗体字重：与纯文本没有区别"
    assert "SpanStyle" in ann and "pushStyle" in ann and "pop()" in ann, \
        "样式必须成对 push/pop，否则一段粗体会把后面全部带粗"
    assert "import androidx.compose.ui.text.SpanStyle" in src, \
        "SpanStyle 没 import：Kotlin 编译期就红，CI 的 assembleDebug 会先拦住"


def test_code_fence_content_is_still_verbatim():
    """``` 围栏里不许做加粗：代码中的 `**`（指针、幂运算、Glob）原样保留。

    这一条防的是"顺手把 boldSegments 提到 split 之前"：那样 `int **p` 会变成 int p，
    用户复制到 IDE 里才发现少了两个星号。判据钉在分支位置上——加粗只许出现在
    围栏判定的 else 那一侧，代码那一侧仍然把 seg 直接交给等宽 Text()。
    """
    src = _text(CHAT_UI)
    body = _kotlin_body(src, "internal fun RichText", "RichText")
    assert '"```"' in body, "RichText 不再区分代码围栏：围栏里的星号会被当成加粗吃掉"
    hit = [l.strip() for l in body.splitlines() if "boldAnnotated(" in l]
    assert hit and all("else" in l for l in hit), \
        f"加粗渲染不在 else（非代码）分支里：{hit}"
    assert "Text(seg.trimEnd" in body, \
        "代码分支不再原样上屏：围栏内的 ** 会被吃掉，复制出来的代码是坏的"
