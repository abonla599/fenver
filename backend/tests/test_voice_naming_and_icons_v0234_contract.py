"""第 5 轮真机反馈的双端契约（v0.23.4）：语音入口点名 + 输入栏自绘图标。

钉四件事：
① 输入栏不许再用 emoji 当按钮（用户原话「看着太简陋」）：ChatUi.kt 里不得再出现
   📷/🎙/⌨/＋/↑/⌄/ 这些字形，全部换成 Icons.kt 的自绘线性图标；
② 语音服务拒录音时，入口必须点名是哪个 App（用户拿本 App「AI 助手」的麦克风页
   对答案，不说名字分不清开的是谁的权限），且识别服务查不到时要有枚举兜底；
③ 「用系统语音输入试试」是手动备用通路：只在 VoiceUi 里经 Activity Result 发起，
   ChatUi 依旧不得出现 ACTION_RECOGNIZE_SPEECH（第 3 轮「不自动弹」判据不变）；
④ 旧版四轮的判据（键盘实测门控、提示 6 秒自收、占位符随键盘）不许回退；
   「粘贴气泡空壳」一条判据史：第 4 轮整屏装→第 7 轮用户要长按出粘贴、退役→
   第 9 轮用户钦定「粘贴我说过我不要」，空壳回位但只许罩输入框一层。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")
ICONS = _read(UI / "ui" / "Icons.kt")


def test_composer_buttons_no_longer_use_emoji_glyphs():
    for glyph in ('"📷"', '"🎙"', '"⌨"', '"＋"', '"↑"', '"⌄"', '"☰"'):
        assert glyph not in CHAT, f"输入栏还留着 emoji 字形按钮 {glyph}——第 5 轮已换自绘图标"
    for icon in ("IconCamera", "IconMic", "IconKeyboard", "IconPlus", "IconArrowUp",
                 "IconStop", "IconImage", "IconFile", "IconChevronDown", "IconMenu"):
        assert f"fun {icon}(" in ICONS, f"Icons.kt 缺自绘图标 {icon}"
        assert f"{icon}(" in CHAT, f"ChatUi 没在用自绘图标 {icon}"
    assert "Canvas" in ICONS and "StrokeCap.Round" in ICONS, \
        "图标族是 Canvas 直绘的圆头描边线性图标（统一手筋），不是贴图"


def test_voice_permission_entry_names_the_target_app():
    assert "去给「$voiceHintLabel」开麦克风" in CHAT, \
        "入口按钮必须点名要开麦克风的是哪个 App——用户会拿本 App 的设置页对答案（第 5 轮）"
    assert "不是本助手" in CHAT, "提示语要说清楚：拒录音的不是 AI 助手自己"
    assert "去开语音服务权限" in CHAT, "查不到应用名时的兜底文案还在（第 4 轮判据）"
    assert "fun speechServiceCandidates(" in VOICE and "queryIntentServices" in VOICE, \
        "默认识别服务查不到时要枚举全部候选语音服务兜底"
    assert "?: speechServiceCandidates(context).firstOrNull()?.pkg" in VOICE, \
        "speechServicePackage 必须有枚举兜底，不许再静默返回 null 让入口消失"


def test_system_voice_fallback_is_manual_and_lives_in_voiceui():
    assert "rememberSystemVoiceFallback" in VOICE and "StartActivityForResult" in VOICE, \
        "系统语音输入备用通路：走 Activity Result 手动拉起"
    assert "EXTRA_RESULTS" in VOICE, "识别结果要从 RecognizerIntent 标准回包取"
    assert "rememberSystemVoiceFallback(" in CHAT and "用系统语音输入试试" in CHAT, \
        "输入卡上要挂这枚手动按钮"
    assert "ACTION_RECOGNIZE_SPEECH" not in CHAT, \
        "第 3 轮判据不回退：ChatUi 里不得出现自动拉起系统语音界面的动作"


def test_round4_gates_do_not_regress():
    assert "WindowInsets.ime.getBottom(density) > 0 || frameKeyboardUp" in CHAT
    assert re.search(r"enabled = \{[^}]*!keyboardUp", CHAT)
    assert "NoopTextToolbar" not in CHAT and "PopupTextToolbar" in CHAT, \
        "长按必出菜单（第 11 轮第四次改判的口径）：第 15 轮起菜单壳换成系统 PopupMenu；" \
        "第 9 轮的空壳覆写与 Compose 自绘气泡都不许回来"
    assert re.search(r"LaunchedEffect\(status\)[\s\S]{0,200}delay\(6000\)", CHAT)
    assert re.search(r'if \(keyboardUp\) "发消息…" else "发消息或按住说话…"', CHAT)
    assert '"按住说话"' in CHAT and "voiceMode || !keyboardUp" in CHAT
