"""原生列表必须跟着**长高的思考面板**走（v0.29.2）。

服务端把思考帧改成即产即发之后，客户端这一侧还剩半件事没做对：贴底跟随那条
`LaunchedEffect` 的 key 只有 `messages.size, streamText`。而"模型只想不说"的那几十
秒里 `streamText` 一直是空串——面板在一行一行变高，列表却一动不动，最新那几行思考
始终在视口外。用户抱怨的那句「等半天，思考过程和答案一起蹦出来」在这一层还有第二
个来源，只是它在客户端。

v0.29.0 当时把这件事写成了"顺风车"注释（面板长高搭 content 帧重跑的便车），并逐字
钉死 key 不许动；那次权衡守的是 v0.28 真机磨出来的贴底判据，不是"永远不跟面板"。
这一版把面板的变化量加进 key，**贴底/夹边界/近底闸门三条判据一个字都没动**（它们
仍由 test_settled_bottom_and_title_ink_v0252_contract.py 钉着）。

为什么必须挂**变化量**而不是列表本身：连续思考是就地并进最后一步的（30 个分片不许
变 30 行），`publishTrace()` 虽然发布新列表，但只在新建那一步或末步被替换时才有变化
——判据要落在"面板真的长高了"这件事上，所以既看步数也看思考总字数。

CI 里没有 Kotlin 测试运行器（只跑 `assembleDebug`），沿用本仓库做法：读 Kotlin 源码
文本当判据，先把注释剥掉，免得注释里的字样冒充成代码。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHAT_UI = (REPO_ROOT / "android-native" / "app" / "src" / "main" / "java"
           / "xyz" / "fenever" / "assistant" / "nativeapp" / "ui" / "ChatUi.kt")


def _strip_kotlin_comments(src: str) -> str:
    """去掉 // 与 /* */ 注释，且尊重字符串字面量。

    不处理引号的话，"https://…" 里的 `//` 会把整行代码吃掉——那会让判据看起来"没
    找到"，红到离真凶很远的地方。同一把尺子在 test_v025_android_resume_contract.py
    与 test_settled_bottom_and_title_ink_v0252_contract.py 已有先例。
    """
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


def _code() -> str:
    assert CHAT_UI.is_file(), f"契约的另一端不在仓库里：{CHAT_UI}"
    return _strip_kotlin_comments(CHAT_UI.read_text(encoding="utf-8"))


def _settle_effect_keys(code: str) -> str:
    m = re.search(r"LaunchedEffect\((messages\.size[^)]*)\)\s*\{", code)
    assert m, "找不到贴底跟随那条 LaunchedEffect(messages.size, …)：整块被删了？"
    return m.group(1)


def test_the_follow_effect_wakes_up_when_the_panel_grows():
    """key 里必须有过程面板的变化量：只有 streamText 的那份在"只想不说"时永不触发。"""
    keys = _settle_effect_keys(_code())
    assert "streamText" in keys, "正文不再触发跟随：答案流式期间会脱离底部"
    assert "streamTrace.size" in keys, \
        "面板步数没进 key：新增一步（工具/搜索）不会把列表带到底部"
    assert re.search(r"streamTrace\w*Chars", keys), \
        "思考总字数没进 key：连续思考并进末步时列表身份不变，等于没修"


def test_the_trace_char_signal_is_a_real_derivation_not_a_constant():
    """那个变化量必须是从 streamTrace 现算出来的，不许是颗永远不变的常量。"""
    code = _code()
    m = re.search(r"val\s+(streamTrace\w*)\s*=\s*streamTrace\.sumOf\s*\{([^}]*)\}", code)
    assert m, "找不到 streamTrace.sumOf { … } 这条派生：跟随的信号是假的"
    assert re.search(r"\.text\.length", m.group(2)), \
        f"派生没在量思考文本长度：{m.group(2).strip()}"
    assert m.group(1) in _settle_effect_keys(code), "算出来的信号没被挂进 LaunchedEffect 的 key"


def test_the_generate_branch_still_only_follows_when_near_bottom():
    """加 key 不许把"上翻阅读不被拽回底部"这道闸一起改掉（判据与 v0.25.2 同源）。"""
    code = _code()
    m = re.search(r"LaunchedEffect\(messages\.size[^)]*\)\s*\{", code)
    assert m, "贴底效果不见了"
    i = m.end() - 1
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                body = code[i:j + 1]
                break
    else:
        raise AssertionError("LaunchedEffect 体大括号不配对")
    assert re.search(r"lastVisible\s*<\s*total\s*-\s*2", body), \
        "近底闸门没了：每来一步思考都会把正在上翻阅读的人拽回底部"
    assert "withFrameNanos" in body, "落定随帧重贴被删：v0.28 那个'条目事后长高'的 bug 会回来"


def test_the_follow_effect_is_still_a_single_one():
    """反向锁：贴底跟随全场只许有这一条挂 messages.size 的效果。

    第二份"看见面板变高就滚到底"会绕过近底闸门，把上翻阅读的人反复拽回底部——
    多写一份比不写更坏，所以这里数条数。
    """
    code = _code()
    hits = re.findall(r"LaunchedEffect\(\s*messages\.size", code)
    assert len(hits) == 1, f"贴底效果出现了 {len(hits)} 份：跟随判据被复制成了两处真相"
