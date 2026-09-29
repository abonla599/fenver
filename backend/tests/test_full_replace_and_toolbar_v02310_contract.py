"""第 11 轮真机反馈的双端契约（v0.23.10）：完全取代输入框 + 默认文字工具条回归。

用户原话（附 v0.23.9 真机图：录音条只占了输入位中间一格，相机/麦克风/＋还露着）：
「要和deepseek还有豆包的一模一样，你现在还没有取代输入框……单点会触发键盘并且
（键盘起着时）长按语音识别不再触发，此时长按输入框会在光标旁边出现复制粘贴等ui；
刚打开软件时长按对话框，键盘不再弹出，语音识别UI完全取代输入框」。

钉四件事：
① 完全取代：本轮回音行级 matchParentSize 覆盖层（第 12 轮再改判为全屏
   VoiceScreen，判据搬家到 v02311 契约）——但手势锚点必须还挂在输入位 Box 上
   （覆盖层不挂 pointerInput 不抢事件；若把侧键 if(!voiceLive) 摘掉，slot 翻面
   会掐死录音中的 pointerInput）；
② 默认工具条回归（第四次改判）：NoopTextToolbar 空壳整块退役，键盘起着时长按
   输入框出系统复制/粘贴/全选；防「点一下冒气泡」改由「键盘没起不挂框」承担；
③ 键盘态门控不回退：enabled 仍吃 rememberUpdatedState + voiceMode || !keyboardUp
   ——键盘起着长按归文本选择，语音不触发（v0.23.9 真机已验证此条成立）；
④ 第 3~10 轮判据不回退（不挂框闸门、短按转发、SETTLE_MS、stashIme 双保险、
   坐标差检测、点名开麦/换引擎）。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")


def test_recording_bar_fully_replaces_input_row():
    # 第 12 轮改判：行级 matchParentSize 覆盖层被真机判「明显是嵌入进对话框的，
    # 模型切换按钮都显示出来了」——录音 UI 升级为全屏 VoiceScreen（判据搬家，
    # 见 v02311 契约）。本条只保留还活着的锚点纪律与「覆写不许复活」。
    assert "matchParentSize" not in CHAT and "VoiceBar(" not in CHAT, \
        "行内覆盖层已退役：只盖输入行不算完全取代（第 12 轮截图控诉），不许回魂"
    assert ".then(voiceMod)" in CHAT, \
        "手势锚点必须留在输入位 Box：覆盖层没挂 pointerInput 不抢事件，" \
        "把锚点摘进 if 分支会挪 slot 掐死录音中的 pointerInput——onFinish 永不回"


def test_default_text_toolbar_restored_round11():
    assert "NoopTextToolbar" not in CHAT and "PopupTextToolbar" in CHAT, \
        "第 11 轮点名「要和deepseek一模一样」：键盘起着长按要出复制/粘贴/全选。" \
        "第 15 轮第五次改判：菜单换成系统 PopupMenu 壳（长相原生、外面点即收），" \
        "Noop 空壳与自绘气泡都不许回来"
    assert "TapAwareTextToolbar" not in CHAT and "PressLedger" not in CHAT
    assert "import androidx.compose.ui.platform.TextToolbar" in CHAT, \
        "第 15 轮起覆写合法：PopupTextToolbar 必须 import 平台接口——第 9~10 轮不许留的" \
        "是 Noop 空壳残骸，不是这个在干活的系统菜单壳"


def test_keyboard_up_longpress_goes_to_selection_not_voice():
    assert "voiceMode || !keyboardUp" in CHAT and \
        re.search(r"val enabledRef = rememberUpdatedState\(enabled\)", VOICE), \
        "键盘起着长按归文本选择（系统菜单）、语音不触发：活门控原样在位"
    assert "down.consume()" in VOICE, "键盘没起时按下仍被语音闸门源头吃掉"


def test_earlier_rounds_do_not_regress():
    assert re.search(r"val voiceArmed = input\.text\.isEmpty\(\) && !busy", CHAT), \
        "第 10 轮地基：闸门在位不挂文本框——刚打开软件长按键盘不弹出的根因解法"
    assert "LaunchedEffect(fieldPending, keyboardUp)" in CHAT and "keyboardController?.show()" in CHAT, \
        "单点=打字：挂回文本框、聚焦、键盘随松手出现"
    assert "SETTLE_MS" not in VOICE and "const val MAX_SESSION_MS = 60_000L" in VOICE, \
        "第 15 轮改判：松手即收尾，等包闹钟退役，防挂死靠 60s 硬顶"
    assert "fun stashIme()" in CHAT and "for (i in 1..4)" in CHAT
    assert "getWindowVisibleDisplayFrame" in VOICE and "getLocationOnScreen" in VOICE
    # 第 12 轮：胶囊 token（26dp/珊瑚/呼吸点）随行内 VoiceBar 退役，录音态文案
    # 换成全屏两态「松手发送，上移取消 / 松手取消」（判据在 v02311 契约）
    assert '"正在听你说…"' in CHAT, "录音中锚点位的提示还在输入卡上"
    assert re.search(r'if \(keyboardUp\) "发消息…" else "发消息或按住说话…"', CHAT)
    assert '"按住说话"' in CHAT and "换个语音引擎" in CHAT
    assert "ACTION_RECOGNIZE_SPEECH" not in CHAT
