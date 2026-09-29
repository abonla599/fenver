"""断网提醒与长按门控的双端契约（2026-09-28 真机反馈第 3 轮）。

钉三件事：
① 原生长按语音只在键盘没弹出时接管——键盘在的时候长按归文本框（选择/粘贴）；
② 不再自动拉起系统语音界面（ACTION_RECOGNIZE_SPEECH 会弹厂商服务自己的
   白框错误「似乎出错了呢(2)」，比不响还吓人），改为一句话状态提示；
③ 断网提醒双端同判据同文案：只认连接层失败（非 HTTP 状态的业务错误不弹），
   带「今日不再显示」且按本地日期持久化，隔天自动恢复。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "backend" / "app" / "web" / "static"
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_voice_longpress_yields_to_keyboard():
    kt = _read(UI / "ui" / "ChatUi.kt")
    assert "WindowInsets.ime.getBottom(density) > 0" in kt, \
        "键盘在不在，必须用 IME 内衬高度判，不许靠焦点猜"
    assert re.search(r"enabled = \{[^}]*!keyboardUp", kt), \
        "voiceHold 的接管条件必须包含『键盘没弹出』——键盘开着长按是文本选择，不是说话"


def test_no_auto_launch_of_system_speech_ui():
    kt = _read(UI / "ui" / "ChatUi.kt")
    assert "ACTION_RECOGNIZE_SPEECH" not in kt, \
        "系统语音界面不许自动拉起：它会弹服务自己的『似乎出错了呢(2)』白框（真机反馈第 3 轮）"
    assert "onMicServiceDenied" in kt and "先打字聊" in kt, \
        "权限误报的兜底必须还在，只是换成一句日常提示"


def test_offline_reminder_both_ends_same_words():
    kt = _read(UI / "ui" / "NetMinder.kt")
    js = _read(STATIC / "api.js")
    assert "连不上网络了，消息暂时发不出去" in kt and "连不上网络了，消息暂时发不出去" in js, \
        "双端同文案，不许各造各的句子"
    assert "今日不再显示" in kt and "今日不再显示" in js
    assert "知道了" in kt and "知道了" in js


def test_offline_judgement_is_connection_layer_only():
    kt = _read(UI / "ui" / "NetMinder.kt")
    js = _read(STATIC / "api.js")
    assert "e !is ApiException" in kt, "原生判据：非 HTTP 状态异常才算断网，4xx/5xx 走状态条"
    assert "e instanceof TypeError" in js, "网页判据：fetch 抛 TypeError = 请求根本没出设备"


def test_mute_today_persists_by_local_date_on_both_ends():
    kt = _read(UI / "ui" / "NetMinder.kt") + _read(UI / "Prefs.kt")
    js = _read(STATIC / "api.js")
    assert "offline_mute_date" in kt and "yyyy-MM-dd" in kt, \
        "原生静音按本地日期存 Prefs，隔天自动恢复"
    assert "netminder_mute" in js and "_today()" in js, \
        "网页静音按本地日期存 localStorage"


def test_offline_card_is_overlay_not_layout():
    kt = _read(UI / "ui" / "ChatUi.kt")
    css = _read(STATIC / "style.css")
    assert "NetMinderCard(Modifier.align(Alignment.TopCenter).statusBarsPadding()" in kt, \
        "原生卡片悬浮在内容之上（Box overlay），不许挤消息列表布局"
    assert "#netMinder" in css and "position: fixed" in css, \
        "网页卡片同理：fixed 悬浮，不占文档流"
