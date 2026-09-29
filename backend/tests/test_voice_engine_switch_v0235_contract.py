"""第 6 轮真机反馈的双端契约（v0.23.5）：语音引擎可切换，别再拿开麦克风当万能钥匙。

真机证据链：默认「系统语音引擎」一长按就回 11（ERROR_SERVER_TERMINATED），
系统语音界面弹白框「似乎出错了呢(2)」——那是引擎连不上**它自己的**服务器。
麦克风权限第 5 轮就给对了，问题从来不在权限。钉四件事：
① 引擎死亡类错码（4/5/8/10/11）必须从"报错重试"里拆出来走 onEngineDead，
   并拆掉旧 binder；普通错码的报错路径不许被牵连；
② 识别器要能按 ComponentName(pkg, cls) 直绑指定引擎，选择持久化到 Prefs，
   冷启动装回；连续两次引擎死且机器上不止一个候选时自动轮换；
③ 输入栏修复行第一位必须是「换个语音引擎」（FlowRow 摊开），弹窗列全部候选
   + 跟随系统默认 + 系统语音设置直达；备用通路失败文案不许再喊"去开麦克风"；
④ 第 4/5 轮的门控（长按让键盘、粘贴空壳、点名开麦、不自动弹系统语音）不许回退。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")
PREFS = _read(UI / "Prefs.kt")


def test_engine_dead_codes_route_to_switch_not_retry():
    assert "ENGINE_DEAD_CODES" in VOICE and "ERROR_SERVER_TERMINATED" in VOICE, \
        "11=SERVER_TERMINATED 这类引擎死亡码必须单列（第 6 轮真机：长按即回 11）"
    assert re.search(r"if \(error in ENGINE_DEAD_CODES\)[\s\S]{0,160}onEngineDead\?\.invoke", VOICE), \
        "死亡码要走 onEngineDead 换引擎，不许再落进普通报错文案里让用户重试"
    assert "onMicServiceDenied?.invoke()" in VOICE, "第 4/5 轮的权限误报通路必须还在"


def test_recognizer_can_bind_chosen_engine():
    assert "createSpeechRecognizer(appCtx, e)" in VOICE, \
        "要用 ComponentName 直绑用户/自动轮换选中的引擎"
    assert "var engine: ComponentName? = null" in VOICE
    assert "val cls: String" in VOICE or "cls: String" in VOICE, \
        "候选必须带服务类名，光包名绑不了 ComponentName"
    assert "voice_engine_pkg" in PREFS and "voice_engine_cls" in PREFS, \
        "引擎选择要持久化，冷启动装回"
    assert "ComponentName(p, c)" in CHAT, "ChatUi 冷启动要把存的引擎装回 voice.engine"


def test_auto_rotation_and_manual_picker():
    assert re.search(r"engineDeadStreak >= 2[\s\S]{0,200}applyEngine\(nxt\)", CHAT), \
        "连续两次引擎死且不止一个候选：自动轮换到下一个，别让用户干按"
    assert "Prefs.voiceEngineCls.isNotEmpty()" in CHAT, \
        "用户手动锁过引擎就不许再自作主张自动换"
    assert '"换个语音引擎"' in CHAT, "修复行第一枚必须是换引擎（病根是引擎不是麦克风）"
    assert "选一个语音识别引擎" in CHAT and "跟随系统默认" in CHAT, \
        "手动弹窗：列候选 + 可切回系统默认"
    assert "android.settings.VOICE_INPUT_SETTINGS" in CHAT, \
        "弹窗里给系统语音设置直达（用字符串动作，兼容低版本 API）"
    assert "已自动切到「${nxt.label}」" in CHAT, "自动换引擎要说人话：换到哪个、下一步做什么"


def test_fallback_words_no_longer_blame_microphone():
    assert "它和长按用的是同一个引擎" in VOICE, \
        "备用通路失败不能再喊去开麦克风——它俩用的是同一个死引擎（第 6 轮误导）"
    assert "还是得去给语音服务开麦克风" not in VOICE, "旧的误导文案必须清除"
    assert "ACTION_RECOGNIZE_SPEECH" not in CHAT, "第 3 轮判据不回退：不自动弹系统语音界面"


def test_round45_gates_do_not_regress():
    assert "先打字聊" in CHAT and "不是本助手" in CHAT
    assert '去给「$voiceHintLabel」开麦克风' in CHAT and "去开语音服务权限" in CHAT
    assert "PopupTextToolbar" in CHAT and "NoopTextToolbar" not in CHAT, \
        "长按必出菜单（第 11 轮口径）：第 15 轮起是系统 PopupMenu 壳，空壳不许复活"
    assert re.search(r"enabled = \{[^}]*!keyboardUp", CHAT)
    assert "voiceHintOn" in CHAT and "FlowRow" in CHAT, \
        "修复行三枚胶囊用 FlowRow，窄屏换行不挤没"
