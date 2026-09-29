"""输入栏「短按不冒粘贴、键盘起不触语音」的双端契约（2026-09-28 真机反馈第 8 轮）。

钉四件事：
① voiceHold 的门控必须是"活"的——pointerInput 不重启，enabled 闭包会冻在第一次
   组合的 keyboardUp=false（第 4~8 轮反复复发的真根因）；必须用
   rememberUpdatedState 每次按下现取最新 lambda；
② 语音接管前必须实收键盘：clearFocus 在这 ROM 上不够，要 windowInsetsController
   .hide(ime)——胶囊和键盘不许同屏（第 8 轮真机图二）；setVoiceMode 同样走 stashIme；
③ 粘贴气泡治理（第 9 轮改判：用户钦定「粘贴我说过我不要」，按压时长把关壳被
   真机证伪退役，换成输入框作用域 NoopTextToolbar——判据见下面第三条用例）；
④ 第 3~7 轮判据不许回退（键盘实测、门控在位、6 秒自收、SETTLE_MS、语音模式显式）。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")


def test_voice_hold_gate_reads_latest_state_not_frozen_lambda():
    assert "import androidx.compose.runtime.rememberUpdatedState" in VOICE, \
        "enabled 闭包必须吃 rememberUpdatedState——pointerInput 不随组合重启，" \
        "裸闭包会把 keyboardUp 冻在第一次组合的 false（第 4~8 轮反复复发病根）"
    assert re.search(r"val enabledRef = rememberUpdatedState\(enabled\)", VOICE)
    assert "if (!enabledRef.value()) return@awaitEachGesture" in VOICE, \
        "按下判闸必须现取现用 enabledRef.value()，不许再直接调裸闭包"
    assert re.search(r"enabled = \{[^}]*!keyboardUp", CHAT), \
        "键盘起着不接管语音：这条门控的字样必须还在（语义由上面两条钉活）"


def test_voice_takeover_stashes_ime_before_recording():
    assert "fun stashIme()" in CHAT and "WindowInsetsCompat.Type.ime()" in CHAT, \
        "clearFocus 单打在这 ROM 收不了键盘——必须命令窗口控制器 hide(ime)"
    assert re.search(r"root\.windowInsetsController\?\.hide\(WindowInsetsCompat\.Type\.ime\(\)\)", CHAT)
    assert re.search(r"stashIme\(\)\s*\n\s*voice\.start\(\)", CHAT), \
        "长按判为说话的瞬间先压键盘再开录音：胶囊和键盘不许同屏（第 8 轮真机图二）"
    assert re.search(r"if \(on\) stashIme\(\)", CHAT), \
        "切语音模式也要实收键盘，不许只 clearFocus"


def test_toolbar_governance_follows_round11_override():
    # 本文件第 8/9 轮原判据（时长把关壳/输入框作用域空壳）已被第 11 轮第四次
    # 改判推翻：用户点名「要和deepseek一模一样」，键盘起着时长按输入框要在光标
    # 旁出复制/粘贴/全选——默认工具条彻底恢复，任何覆写整块退役。
    assert "NoopTextToolbar" not in CHAT, \
        "第 11 轮改判：长按要出系统菜单，空壳必须整块退役"
    assert "LocalTextToolbar provides" in CHAT and "PopupTextToolbar" in CHAT, \
        "第 15 轮第五次改判：菜单换成系统 PopupMenu 壳（v02314 契约钉形状）"


def test_earlier_rounds_do_not_regress():
    # 第 4 轮：键盘实测可见区 + 状态条 6 秒自收 + 占位符随键盘
    assert "getWindowVisibleDisplayFrame" in VOICE and "rememberKeyboardVisible()" in CHAT
    assert re.search(r"LaunchedEffect\(status\)[\s\S]{0,200}delay\(6000\)", CHAT)
    assert re.search(r'if \(keyboardUp\) "发消息…" else "发消息或按住说话…"', CHAT)
    # 第 4 轮：拒录音直达入口；第 7 轮：语音模式字样在位
    assert '"按住说话"' in CHAT and "voiceMode || !keyboardUp" in CHAT
    assert "去开语音服务权限" in CHAT
    # 第 7 轮的「不卡死」判据第 15 轮换形态：松手当场收尾（等包闹钟退役），
    # 未知路防挂死仍由会话硬顶闹钟 postDelayed 守着
    assert "SETTLE_MS" not in VOICE and "mainHandler.postDelayed" in VOICE
