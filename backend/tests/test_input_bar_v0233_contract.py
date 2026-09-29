"""输入栏重做与语音可修通路的双端契约（2026-09-28 真机反馈第 4 轮）。

钉五件事：
① 键盘在不在必须"实测窗口可见区"——上一版只读 IME 内衬，而本 App 是 adjustResize
   非 edge-to-edge，内衬恒为 0，门控在真机上形同虚设（长按仍闪录音的根因）；
② 文字工具条判据已被第 7 轮推翻：当年为赶"粘贴气泡"换的空壳工具条把长按粘贴
   一起枪掉了，用户钦定恢复系统默认工具条（见 test_voice_capsule_and_toolbar_v0236_contract.py）；
③ 状态条提示 6 秒自收，不许常驻；语音服务拒录音的提示是中性色不是红色；
④ 语音模式是显式按钮（🎙 ↔ ⌨），输入区变「按住说话」，不依赖碰运气的手势判定；
   语音服务自己拒录音时给「去开语音服务权限」直达入口，且失败后重绑识别器；
⑤ 输入栏对齐参考图：占位符随键盘切换（发消息或按住说话… / 发消息…）。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")


def test_keyboard_gate_measures_window_not_only_ime_insets():
    assert "rememberKeyboardVisible()" in CHAT, \
        "键盘检测必须走窗口可见区实测：adjustResize 下 IME 内衬恒为 0（第 4 轮根因）"
    assert re.search(r"WindowInsets\.ime\.getBottom\(density\) > 0 \|\| frameKeyboardUp", CHAT), \
        "keyboardUp 要双保险：内衬有值吃内衬，另外吃实测"
    assert "getWindowVisibleDisplayFrame" in VOICE, \
        "实测的实现（可见区缩水超 1/5 屏判键盘在）必须留在 VoiceUi 里"


def test_paste_toolbar_default_restored_round11():
    # 判据史：第 4 轮装整屏空壳→第 7 轮空壳退役恢复默认→第 8 轮按时长把关→
    # 第 9 轮用户钦定「粘贴我说过我不要」空壳回牌桌（只罩输入框）→第 11 轮用户
    # 点名「要和deepseek一模一样：键盘起着时长按输入框在光标旁出复制粘贴等ui」
    # ——第四次改判：默认工具条彻底恢复，Noop 空壳整块退役。键盘没起时输入位
    # 是占位条（不挂框），单点不冒菜单；长按归语音，两头不冲突。
    assert "NoopTextToolbar" not in CHAT, \
        "第 11 轮改判：长按要出系统复制/粘贴菜单，空壳必须整块退役"
    assert "TapAwareTextToolbar" not in CHAT and "PressLedger" not in CHAT, \
        "第 8 轮的按时长把关壳已被真机证伪（气泡照样冒），不许复活"


def test_status_banners_self_dismiss():
    assert re.search(r"LaunchedEffect\(status\)[\s\S]{0,200}delay\(6000\)", CHAT), \
        "状态条要 6 秒自收：红色提示常驻被真机点名（第 4 轮反馈）"
    denied = re.search(r"voice\.onMicServiceDenied = \{[\s\S]*?\n    \}", CHAT)
    assert denied and ", true)" not in denied.group(0), \
        "语音服务拒录音的提示是提醒不是报错，不许用红色档"


def test_voice_mode_is_explicit_and_service_hint_reachable():
    assert '"按住说话"' in CHAT, "语音模式：输入区必须能整块变成「按住说话」大按钮"
    assert "voiceMode || !keyboardUp" in CHAT, \
        "语音模式下无条件接管长按；文本模式下仍要让着键盘"
    assert "speechServicePackage(ctx)" in CHAT and "ACTION_APPLICATION_DETAILS_SETTINGS" in CHAT, \
        "拒录音时给直达入口：跳语音服务 App 的系统详情页开麦克风"
    assert "去开语音服务权限" in CHAT
    assert re.search(r"runCatching \{ rec\?\.destroy\(\) \}\s*\n\s*rec = null\s*\n\s*onMicServiceDenied", VOICE), \
        "拒权后必须拆掉旧识别器：授权态可能被旧 binder 缓存，下次长按要重绑新实例"


def test_placeholder_follows_keyboard_like_the_reference():
    assert re.search(r'if \(keyboardUp\) "发消息…" else "发消息或按住说话…"', CHAT), \
        "占位符随键盘切换（第 13 轮改判：键盘起着只写「发消息…」）：键盘没起才提示可以长按说话"
