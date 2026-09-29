"""第 14 轮真机反馈的契约（v0.23.13）：焦点判键盘 + 录音中锁抽屉。

用户原话（附豆包键盘态两图）：「还是没改好，这个时候图片中的输入框应该只有发消息
没有按住说话，这个时候长按应该出现下图所示的选项（全选/粘贴/选择），我发现一个问题
就是当我按住语音是向右滑动时，会出现如图所示的现象。非常影响观感。」

钉四件事（第 15 轮锁口径已改判，见 ① 注）：
① 键盘检测信号：第 14 轮装的焦点代理被第 15 轮真机证伪（收键盘不收焦点，信号
   卡死在"有键盘"），整个撤下换成窗口绝对高度缩水（adjustResize 物理事实，
   ROM 抹不平）——keyboardUp 必须吃它，占位符、展开态、voiceHold 门控、
   fieldPending 清挂账全部自动归位；
② 语音通路不受换信号源牵连：stashIme 的 clearFocus 首步保留；
③ 录音中锁抽屉：ModalNavigationDrawer 的 gesturesEnabled 吃 !voice.listening——
   按住语音右滑不再把整页拖开、录音卡错位；
④ 第 3~13 轮地基不回退（双保险几何信号、闸门、松手铁律、贴底卡片、收起态模型钮、
   v0.23.13 简版留档）。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"
RELEASE_DOC = REPO_ROOT / "docs" / "releases" / "v0.23.13.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")


def test_keyboard_signal_is_height_shrink_not_focus():
    # 第 15 轮把焦点代理整个撤下（用户真机：点键盘下箭头收了键盘，焦点不走，
    # 信号永远卡在"有键盘"，胶囊回不去）。换窗口绝对高度缩水：adjustResize 下
    # 键盘把窗顶扁是物理事实，个别 ROM 抹不平；收窗即弹回，跟焦点无关。
    assert "fieldFocused" not in CHAT and "onFieldFocus" not in CHAT, \
        "焦点接线不许复活：收键盘不收焦点，这路信号天生测不到「键盘已收」"
    assert re.search(r"val keyboardUp = WindowInsets\.ime\.getBottom\(density\) > 0"
                     r" \|\| frameKeyboardUp", CHAT), \
        "keyboardUp 两路取或：ime 内衬兜底 + rememberKeyboardVisible（可见区差 || 窗高缩水）"
    assert re.search(r"var maxH = 0", VOICE) and re.search(r"else if \(h > maxH\) maxH = h", VOICE), \
        "历史最高窗高基线在 rememberKeyboardVisible 里"
    assert re.search(r"maxH - h > maxH / 5", VOICE), "现高比最矮窗高基线矮 1/5 判键盘在"
    assert re.search(r"if \(w != lastW\) \{ lastW = w; maxH = h \}", VOICE), \
        "宽度一变（旋转/分屏）旧基线作废重立——别把换窗当起键盘"
    # 门控吃 keyboardUp：键盘起着 voiceHold 让位给系统工具条（豆包图 2：全选/粘贴/选择）
    assert re.search(r"enabled = \{[^}]*!keyboardUp", CHAT), \
        "键盘起着长按归文本框自己（选字+系统工具条），语音不抢"
    assert re.search(r"val voiceArmed = input\.text\.isEmpty\(\) && !busy"
                     r" && !voice\.listening &&[\s\S]{0,80}!keyboardUp", CHAT), \
        "闸门同样吃键盘信号：键盘起着必挂文本框（没框就没光标，豆包图 1 口径）"


def test_voice_paths_unaffected_by_signal_swap():
    assert "onFocusEvent" not in CHAT and ".onFocusEvent" not in CHAT, \
        "聊天页的焦点接线第 15 轮整个拆干净，不留残骸误导下轮"
    assert "focusManager.clearFocus()" in CHAT, \
        "stashIme 先 clearFocus——语音按下照常把焦点压掉，换信号源后这条不许丢"
    assert re.search(r"fun stashIme\(\) \{[\s\S]{0,80}clearFocus", CHAT), \
        "clearFocus 必须是 stashIme 的第一步：先丢焦点再压键盘"


def test_drawer_swipe_locked_while_recording():
    assert re.search(r"ModalNavigationDrawer\(\s*\n\s*drawerState = drawerState,\s*\n"
                     r"[\s\S]{0,400}gesturesEnabled = !voice\.listening", CHAT), \
        "录音中锁死抽屉滑动：右滑不再把整页拖开、录音卡错位（第 14 轮真机截图）"
    assert "gesturesEnabled = !voice.listening" in CHAT, \
        "判据只吃 listening——松手退场即恢复，不留永久锁"
    # CI 红过一次：swipeEnabled 是 material3 新版参数名，本仓 BOM 2024.06.00 只有 gesturesEnabled
    assert "swipeEnabled" not in CHAT, \
        "不许回退成 swipeEnabled——这个参数名在当前 BOM 下编译不过"


def test_earlier_rounds_do_not_regress():
    assert "getWindowVisibleDisplayFrame" in VOICE, "第 9 轮几何双保险原样保留（多一路不亏）"
    assert re.search(r'if \(keyboardUp\) "发消息…" else "发消息或按住说话…"', CHAT), \
        "第 13 轮占位符判据：键盘起着只写「发消息…」"
    assert re.search(r"VoiceScreen\(voice\.heard, voice\.level, voiceCancel,\s*"
                     r"Modifier\.align\(Alignment\.BottomCenter\)", CHAT), \
        "第 13 轮贴底卡片不回退"
    assert "const val MAX_SESSION_MS = 60_000L" in VOICE and \
        re.search(r"var settled = false", VOICE), \
        "第 13 轮必退场双保险（settled 一次收尾 + 60s 硬顶）在位"
    assert re.search(r"pending = t\n\s*return", VOICE), \
        "第 13 轮松手发送铁律（引擎提前回包先暂存）在位"
    assert re.search(r"if \(!expanded\) \{[\s\S]{0,220}if \(chipVisible\) ModelChip", CHAT), \
        "第 13 轮收起态模型钮在位"
    assert "NoopTextToolbar" not in CHAT and "LocalTextToolbar provides" in CHAT, \
        "用户第 14 轮点名要长按菜单：第 15 轮起给它接的是系统 PopupMenu 壳（v02314 契约钉形状），Noop 空壳不许复活"


def test_release_notes_brief_format():
    doc = _read(RELEASE_DOC)
    allowed = {"修复了什么", "优化了什么", "新增了什么"}
    heads = re.findall(r"^##\s*(.+?)\s*$", doc, flags=re.M)
    assert heads and set(heads) <= allowed, "简版三段式：没有的不写，解释性段落禁止"
    assert not re.search(r"怎么修|根因|教训", doc), "只报结果，不展开过程"
