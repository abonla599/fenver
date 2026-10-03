"""第 16 轮真机反馈的键盘信号契约（v0.23.15）：减法翻正 + 自校准第三路 + 僵尸监听自愈。

用户原话（附键盘全开、占位符却写「发消息或按住说话…」的真机截图）：「我再说一遍，
这个界面我是要复制粘贴的，不能复制粘贴我想粘贴的内容怎么弄到对话框啊，为什么还有
按住说话，为什么按住还能说话。」

现场结案：v0.23.14 装到那台真机上，键盘明明全开着，keyboardUp 仍判「没键盘」——
占位符照写「按住说话」、长按被语音抢走，系统粘贴菜单根本出不来。两路几何信号
全哑的账记在两处：
① 减法方向：这台 ROM 是全窗口 + IME 悬浮，DecorView 恒占满屏、frame 底边被抬到
   键盘上沿——第 9 轮那路 frame.bottom − 窗底 算出来是负的（= −键盘高），永远够
   不着阈值；翻正成 窗底 − frame.bottom。顶扁式机型该差≈0 不误报，由 maxH 路兜底。
② 监听器活性：ViewTreeObserver 随窗口树重建整个作废，旧实例上的监听器从此一声
   不吭——「两路全哑」和监听器死透的现场无法区分，必须自愈：回调里验 isAlive
   换挂新 OIV，视图重新入窗时补挂一遍。
另加第三路自校准信号：frame 底边自己的历史最低点 maxFB 当基线，现值比它高
1/5 屏判键盘在——不依赖窗口缩不缩，只要键盘起落可见区底边会动（第 14 轮截图
已实证）就扳得响。

钉五件事：
① 减法翻正在位，旧负方向公式不许复活；
② maxFB 第三路 + 宽度变化重立基线在位；maxH 路保留；
③ OIV 僵尸自愈（isAlive 换挂）+ 入窗补挂（OnAttachStateChangeListener）在位；
④ 250ms 轮询兜底在位——悬浮式 IME 不触发布局、监听器一次不回调时，信号照样
   最迟四分之一秒必刷新，不存在"卡死在旧值"；
⑤ 键盘起着长按归文本选择（系统粘贴菜单）、语音不触发的活门控原样在位；
   焦点代理（第 14 轮旧药）不许借尸回来。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"
BUILD = REPO_ROOT / "android-native" / "app" / "build.gradle"
RELEASE_DOC = REPO_ROOT / "docs" / "releases" / "v0.23.15.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")


def test_frame_gap_subtraction_flipped_to_positive():
    kb = re.search(r"fun rememberKeyboardVisible\(\)[\s\S]*?\n\}", VOICE)
    body = kb.group(0)
    assert re.search(r"\(loc\[1\] \+ root\.height\) - frame\.bottom > root\.height / 5", body), \
        "第 16 轮翻正：窗底−frame.bottom 才是键盘高（悬浮窗机型上旧式算出来是负的）"
    assert "frame.bottom - (loc[1]" not in body, \
        "旧负方向公式（frame.bottom − 窗底）在悬浮窗上恒为负、永远哑火，不许复活"


def test_third_self_calibrated_frame_bottom_baseline():
    kb = re.search(r"fun rememberKeyboardVisible\(\)[\s\S]*?\n\}", VOICE)
    body = kb.group(0)
    assert "var maxFB = 0" in body and \
        re.search(r"else if \(frame\.bottom > maxFB\) maxFB = frame\.bottom", body), \
        "第三路基线：frame 底边历史最低点（键盘收着时采得）"
    assert re.search(r"maxFB - frame\.bottom > maxFB / 5", body), \
        "现值比基线高 1/5 屏判键盘在——窗口缩不缩都扳得响"
    assert re.search(r"if \(w != lastFBW\) \{ lastFBW = w; maxFB = frame\.bottom \}", body), \
        "宽度变化（旋转/分屏）旧基线作废重立"
    assert re.search(r"maxH - h > maxH / 5", body), "第 15 轮窗高缩水路保留并联"


def test_dead_observer_self_heal_in_place():
    kb = re.search(r"fun rememberKeyboardVisible\(\)[\s\S]*?\n\}", VOICE)
    body = kb.group(0)
    assert "var oiv = root.viewTreeObserver" in body, \
        "OIV 必须是可换绑的 var——僵尸实例换挂自愈的前提"
    assert re.search(r"if \(!oiv\.isAlive\) \{[\s\S]{0,120}oiv = root\.viewTreeObserver[\s\S]{0,120}addOnGlobalLayoutListener\(listener\)", body), \
        "回调里验 isAlive，死了抓新 OIV 重挂——监听器哑火自愈"
    assert "OnAttachStateChangeListener" in body and "onViewAttachedToWindow" in body, \
        "视图重新入窗时补挂一遍——僵尸期不漏挂"
    assert re.search(r"if \(oiv\.isAlive\) oiv\.removeOnGlobalLayoutListener\(listener\)", body), \
        "退场清理也验活：对死观察者 remove 会直接抛"


def test_polling_safety_net_no_stale_signal():
    # 悬浮式 IME 可能压根不触发布局——监听器一次不回调时公式再对也是死的。
    # 轮询自己量，是「两路全哑」最后的保险丝。
    kb = re.search(r"fun rememberKeyboardVisible\(\)[\s\S]*?\n\}", VOICE)
    body = kb.group(0)
    assert re.search(r"fun measure\(\) \{", body), \
        "量取逻辑抽成局部函数：监听器和轮询共用同一个量法，不分叉"
    assert re.search(r"polled\[0\] = \{ measure\(\) \}", body), \
        "DisposableEffect 把量法交给轮询；退场置 null 防悬空"
    assert re.search(r"LaunchedEffect\(view\) \{\s*\n\s*while \(true\) \{\s*\n\s*delay\(250\)\s*\n\s*polled\[0\]\?\.invoke\(\)", body), \
        "250ms 轮询循环在位：信号最迟四分之一秒必刷新"
    assert "polled[0] = null" in body, "退场先断轮询引用，effect 重挂不叠加"


def test_keyboard_up_still_gates_voice_and_placeholder():
    # 用户本轮的真痛点：键盘起着要能长按粘贴，不能被语音抢
    assert "voiceMode || !keyboardUp" in CHAT, \
        "键盘起着长按归文本选择（系统粘贴菜单）、语音不触发：活门控原样在位"
    assert re.search(r'if \(keyboardUp\) "发消息…" else "发消息或按住说话…"', CHAT), \
        "占位符判据原样在位：键盘一起只写『发消息…』"
    assert "fieldFocused" not in CHAT and "onFieldFocus" not in CHAT, \
        "焦点代理（收键盘不收焦点→卡死）不许借尸回来"
    assert "PopupTextToolbar" in CHAT and CHAT.count("LocalTextToolbar provides") == 2, \
        "原生粘贴菜单壳两处挂载原样在位——信号修通后长按弹的就是它"


def test_version_bumped_and_release_notes_brief():
    gradle = _read(BUILD)
    # 版本号只有一份真相（gradle），所以每一轮的契约文件都跟着钉**当前**那一格：
    # 第 15 轮那一格早被后面几版推过去了，v0.24.0 是 41、v0.24.1 是 42、v0.25.0 是 43、v0.25.1 是 44、v0.25.2 是 45、v0.26.0 是 46、v0.27.0 是 47、v0.28.0 是 48、v0.28.1 是 49、v0.28.2 是 50。下面断的是现在那一格。
    assert "versionCode 50" in gradle and 'versionName "0.28.2"' in gradle, \
        "gradle 那一格漂了：这一版应是 50 / 0.28.2，且必须与 docs/releases/v0.28.2.md 同时存在"
    doc = _read(RELEASE_DOC)
    allowed = {"修复了什么", "优化了什么", "新增了什么"}
    heads = re.findall(r"^##\s*(.+?)\s*$", doc, flags=re.M)
    assert heads and set(heads) <= allowed, \
        "更新日志三段式（第 12 轮钦定延续）：没有的不写，解释性段落禁止"
    assert not re.search(r"怎么修|根因|教训|自愈|自校准", doc), "简版口径：只报结果，不展开过程"
