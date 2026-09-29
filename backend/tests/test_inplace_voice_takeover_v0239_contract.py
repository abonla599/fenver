"""第 10 轮真机反馈的双端契约（v0.23.9）：豆包/DeepSeek 式就地语音接管。

用户原话：「我不希望出现键盘闪一下，你不行就学一下豆包和deepseek那样的，
长按对话框之后，语音ui就取代了输入框」。第 9 轮的「按下抢焦点、判长按后再
连发补刀收键盘」被真机判死——收得再快也是闪一下。本轮换成源头根治：
闸门在位的窗口里文本框压根不挂载，键盘没有可闪的载体。

钉五件事：
① 不挂框即不闪：voiceArmed（空文本/不忙/没在录/非语音模式/键盘没起/没在
   等挂回）在位时，输入位渲染占位胶囊而非 BasicTextField——分支条件必须是
   voiceMode || voiceArmed || voiceLive，voiceLive（录音中）必须在分支内，
   否则录音一开始组合切换就把 pointerInput 协程掐死、onFinish 永不回；
② 短按转发：voiceHold 新增 onTap 出口，按下被闸门吃掉（down.consume()），
   没到判定线正常抬手走 tapRef.value()（同样必须 rememberUpdatedState）；
   ChatScreen 侧 fieldPending → LaunchedEffect 挂回文本框并 requestFocus +
   keyboardController.show()——键盘跟着「点按=打字」出现，不跟着长按闪；
③ 语音 UI 就地取代：贴底悬浮的 VoiceOverlay 整块退役；第 11 轮的行内 VoiceBar
   又被第 12 轮判「嵌入对话框不算取代」——录音 UI 最终形态是全屏 VoiceScreen
   （判据见 v02311 契约），本文件只钉「不挂框/短按转发/双保险」的地基；
④ stashIme 连发补刀与第 8/9 轮键盘检测公式原地保留——降级为双保险，
   不再承担「先把键盘顶起来再摁回去」的主责；
⑤ 第 3~9 轮判据不许回退（活门控、SETTLE_MS、Noop 只罩输入框一层、占位符、
   点名开麦、换引擎入口）。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")


def test_field_not_mounted_while_voice_gate_armed():
    assert re.search(r"val voiceArmed = input\.text\.isEmpty\(\) && !busy", CHAT), \
        "闸门谓词必须在：空文本+不忙才可能长按说话（第 10 轮不闪键盘的地基）"
    assert "!fieldPending" in CHAT, "等挂回的窗口里闸门要让位，不许左右互搏"
    assert re.search(r"val gateOn = voiceMode \|\| voiceArmed \|\| voiceLive", CHAT) and \
        re.search(r"if \(gateOn\) \{", CHAT), \
        "接管分支三段式（第 12 轮起收进 gateOn）：语音模式/闸门/录音中都走同一个 " \
        "Box——分支翻面会掐死 pointerInput 协程，onFinish 就永远不回了"
    takeover = re.search(r"if \(gateOn\) \{([\s\S]*?)\n                    \} else \{", CHAT)
    assert takeover and "BasicTextField" not in takeover.group(1), \
        "接管分支里不许出现文本框：没框没焦点，键盘压根没有可闪的载体"
    assert re.search(r"else \{[\s\S]{0,900}BasicTextField\(", CHAT), \
        "非接管态（打字/有内容/键盘在）照常挂文本框"


def test_short_tap_forwards_to_remount_field():
    assert re.search(r"onTap: \(\) -> Unit = \{\}", VOICE), \
        "voiceHold 必须有短按出口：按下没到 260ms 正常抬手 = 用户想打字"
    assert "val tapRef = rememberUpdatedState(onTap)" in VOICE and "tapRef.value()" in VOICE, \
        "onTap 与 enabled 同罪：pointerInput 不重启协程，裸闭包吃旧快照"
    # 第 13 轮：同一分支搬进 settled 幂等位——抬手收尾一次且仅一次
    assert "if (started) { settled = true; onFinish(cancelling) }" in VOICE and \
        "else if (!movedOut) { settled = true; tapRef.value() }" in VOICE
    assert "down.consume()" in VOICE, \
        "闸门生效就把按下在 Initial 段吃掉：把『绝不冒键盘』钉死在事件源头"
    assert re.search(r"onTap = \{ if \(!voiceMode\) fieldPending = true \}", CHAT), \
        "短按记一笔 fieldPending：语音模式里的短按不该抢着挂字"
    assert "LaunchedEffect(fieldPending, keyboardUp)" in CHAT, \
        "挂回走效果：等重组把文本框挂上再聚焦请键盘"
    assert "inputFocus.requestFocus()" in CHAT and "keyboardController?.show()" in CHAT, \
        "点按=打字的键盘由程序化 show 请出，跟着松手来，不跟着按下闪"
    assert "androidx.compose.ui.platform.LocalSoftwareKeyboardController" in CHAT


def test_voice_ui_replaces_input_in_place():
    # 第 12 轮改判：躺进输入位的横条 VoiceBar（含 26dp 胶囊 token、呼吸麦克风点）
    # 整块退役——「嵌在对话框里、模型按钮还露着」不算取代。第 13 轮再把全屏
    # VoiceScreen 收小成贴底圆角卡片（判据见 v02311/v02312 契约），本条只留
    # 「不许第二块屏/不许卡死」的骨架。
    assert "fun VoiceScreen(heard: String, level: Float, cancelling: Boolean" in VOICE, \
        "录音态 UI 是 VoiceScreen 贴底卡片：盖住输入位，只留卡片自己"
    assert "VoiceBar" not in VOICE and "MicPulseDot" not in VOICE, \
        "行内胶囊的残骸不许留在文件里误导下轮"
    assert "VoiceOverlay" not in CHAT and "fun VoiceOverlay" not in VOICE, \
        "贴底悬浮浮层整块退役——用户点名「语音ui就取代了输入框」，不是叠一层屏"
    assert re.search(r"if \(voice\.listening\) \{", CHAT) and \
        re.search(r"VoiceScreen\(voice\.heard, voice\.level, voiceCancel,\s*"
                  r"Modifier\.align\(Alignment\.BottomCenter\)", CHAT), \
        "录音中根层渲染贴底语音卡（第 13 轮钦定：大小比输入框大点就行）"
    assert "bottom = 168.dp" not in VOICE, "旧胶囊沉底贴屏的偏移量不许残留"
    assert re.search(r"fun WaveBars\(level: Float, barColor: Color, bars: Int", VOICE), \
        "波纹条资产原样保留，搬进语音卡（第 13 轮卡片版收到 bars=24）"


def test_round9_ime_gear_kept_as_double_safety():
    # 补刀连发与屏幕坐标检测降级保留：主责换成了「不挂框」，双保险不许拆
    assert "fun stashIme()" in CHAT and "for (i in 1..4)" in CHAT
    assert re.search(r"stashIme\(\)\s*\n\s*voice\.start\(\)", CHAT)
    assert "getWindowVisibleDisplayFrame" in VOICE and "getLocationOnScreen" in VOICE
    assert re.search(r"val imeStash = remember \{ Handler\(Looper\.getMainLooper\(\)\) \}", CHAT)


def test_earlier_rounds_do_not_regress():
    assert "import androidx.compose.runtime.rememberUpdatedState" in VOICE
    assert "if (!enabledRef.value()) return@awaitEachGesture" in VOICE
    assert "voiceMode || !keyboardUp" in CHAT, "第 8 轮活门控语义原样在位"
    assert "SETTLE_MS" not in VOICE and "const val MAX_SESSION_MS = 60_000L" in VOICE, \
        "第 15 轮改判：松手即收尾，等包闹钟退役，防挂死靠 60s 硬顶"
    # 第 11 轮第四次改判：长按必出菜单；第 15 轮起菜单壳换成系统 PopupMenu（v02314 契约）
    assert "PopupTextToolbar" in CHAT and "NoopTextToolbar" not in CHAT
    assert re.search(r'if \(keyboardUp\) "发消息…" else "发消息或按住说话…"', CHAT)
    assert '"按住说话"' in CHAT and "去开语音服务权限" in CHAT
    assert "换个语音引擎" in CHAT and "ENGINE_DEAD_CODES" in VOICE
    assert "ACTION_RECOGNIZE_SPEECH" not in CHAT
