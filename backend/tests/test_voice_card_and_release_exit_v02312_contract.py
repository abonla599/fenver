"""第 13 轮真机反馈的双端契约（v0.23.12）：语音卡收小 + 松手才发送 + 不说话必退场。

用户原话（附三张对标图）：「只有发消息没有长按语音输入。只有键盘收了之后，才有
支持语音输入，还有就是我没说话，松手之后这个语音UI也该退出了……我长按语音输入
时，还在说话的时候，语音输入自己就断了然后发送，明明是松手发送，我还没松手就
发送了，而且这个UI占据了整个屏幕很不美观，太大了，大小比输入框大点就行了，还有
就是模型切换你把它放到加号旁边。」

钉六件事：
① 松手发送是铁律：引擎提前回终包/提前报错而手指还按着 → 结果/半截字暂存进
   pending，onFinal 绝不提前 firing；UI 原地留住，直到 finish() 统一收尾
   （有字发送、没字安静收）；
② 不说话松手必退场：voiceHold 的 onFinish 一次且仅一次——正常抬手走循环出口，
   协程被掐/异常退出走 finally 兜底（settled 位点标记）；再加 MAX_SESSION_MS
   硬顶闹钟，事件彻底丢了也最迟一分钟自己收场；
③ 录音 UI 收小成贴底圆角卡片：BottomCenter 渲染、VoiceScreen 体内无
   fillMaxSize、卡片不挂 pointerInput（手势锚点仍在输入位 Box）；
④ 占位符改判：键盘起着只写「发消息…」（语音只在键盘收起时长按触发）；
⑤ 模型切换钮搬进收起态：挂在 ＋ 左边（展开态下行同钮保留），ModelChip 抽成
   共用组件；
⑥ 第 3~12 轮地基不回退（松手即收尾＋60s 硬顶〔第 15 轮撤等包闹钟〕、闸门、双保
   险、全屏选模型页）＋ v0.23.12
   更新日志简版三段式。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"
RELEASE_DOC = REPO_ROOT / "docs" / "releases" / "v0.23.12.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")


def test_release_send_waits_for_finger_up():
    # 引擎提前判停（说话中自己断了回 onResults）而手指还按着：只许暂存，不许发
    assert re.search(r"private var pending: String\? = null", VOICE), \
        "pending 暂存位：按住期间引擎提前回的终包先攒着"
    assert re.search(r"if \(holding\) \{[\s\S]{0,200}pending = t\n\s*return\s*\n\s*\}"
                     r"[\s\S]{0,80}listening = false", VOICE), \
        "onResults 里 holding 优先：回 pending 后直接 return，onFinal 绝不提前触发"
    # 提前报错（没听清/静音超时）同样暂存半截字等松手；死亡码/权限错照常立刻退
    assert re.search(r"if \(holding && keepSend && error !in ENGINE_DEAD_CODES &&[\s\S]{0,160}pending = heard\n\s*return", VOICE), \
        "onError 的 holding 分支：引擎没松手先报错也不提前退场"
    # finish(send=true) 当场消费手里结果：第 15 轮改判「松手要立刻响应」，
    # 等迟到包的闹钟整个退役——pending（按住期间已回的终包）优先，其次最后半截
    assert re.search(r"val t = \(pending\?\.takeIf \{ it\.isNotBlank\(\) \} \?: heard\)\.orEmpty\(\)\n"
                     r"\s*pending = null\n\s*listening = false", VOICE), \
        "松手收尾先看手里有什么：有字发送、没字提示，当场关闭不等引擎第二次回包"
    # 取消路径必须清掉暂存，不留脏字到下一次的按下
    assert re.search(r"pending = null\n\s*keepSend = false", VOICE), \
        "finish(send=false)：pending 一起清，取消不留残包"


def test_silent_release_always_exits():
    # onFinish 一次且仅一次：循环出口正常走，协程被掐/异常由 finally 兜底
    assert re.search(r"var settled = false", VOICE), \
        "settled 位点：抬手收尾的幂等标记（第 13 轮『不说话松手 UI 依然在』）"
    assert re.search(r"if \(started\) \{ settled = true; onFinish\(cancelling\) \}", VOICE)
    assert re.search(r"else if \(!movedOut\) \{ settled = true; tapRef\.value\(\) \}", VOICE), \
        "没到判定线的正常抬手仍转发给点按（第 10 轮短按语义不许被兜底逻辑吃掉）"
    assert re.search(r"finally \{\s*timer\.cancel\(\)\s*\n\s*if \(started && !settled\) onFinish\(cancelling\)", VOICE), \
        "finally 兜底：手势协程半途被掐也必须把收尾交回状态机，一次都不许漏"
    # 硬顶闹钟：连事件都丢了（系统吞掉 up），最迟 MAX_SESSION_MS 自己收场
    assert "const val MAX_SESSION_MS = 60_000L" in VOICE, \
        "60 秒会话硬顶：任何路径都不许让录音状态无限挂死"
    assert re.search(r"mainHandler\.postDelayed\(wd, MAX_SESSION_MS\)", VOICE) \
        and re.search(r"Runnable \{[\s\S]{0,200}listening = false; holding = false", VOICE), \
        "start 挂 watchdog 闹钟，到点强制退场"
    assert re.search(r"fun finish\(send: Boolean\) \{[\s\S]{0,140}clearWatchdog\(\)", VOICE), \
        "松手即撤硬顶闹钟：收尾交回 pending/settle 常规路径"


def test_voice_screen_is_compact_bottom_card():
    assert re.search(r"if \(voice\.listening\) \{[\s\S]{0,160}VoiceScreen\(voice\.heard, "
                     r"voice\.level, voiceCancel,\s*Modifier\.align\(Alignment\.BottomCenter\)"
                     r"\.padding\(bottom = 14\.dp\)\)", CHAT), \
        "录音 UI 贴底渲染：用户点名『占据整个屏幕很不美观，大小比输入框大点就行』"
    screen = re.search(r"fun VoiceScreen\(heard: String, level: Float, cancelling: Boolean"
                       r"[\s\S]*?\n\}", VOICE)
    assert screen, "VoiceScreen 必须在 VoiceUi 里"
    body = screen.group(0)
    assert "fillMaxSize" not in body, "全屏回魂即违例：卡片不许铺满屏幕"
    assert "RoundedCornerShape(26.dp)" in body and "verticalGradient" in body, \
        "圆角卡片 + 两态渐变（录音绿/取消粉）照豆包配色"
    assert "松手发送，上移取消" in body and "松手取消" in body
    assert "WaveBars(level, waveInk, bars = 24)" in body, "波纹搬进窄卡：24 根够用"
    assert "pointerInput" not in body, \
        "覆盖层绝不挂 pointerInput：吃了事件录音状态机就断（锚点在输入位）"
    assert ".then(voiceMod)" in CHAT and "Box(Modifier.weight(1f).heightIn(min = 42.dp)" in CHAT, \
        "手势锚点仍钉在输入位 Box 上，卡片出现不挪锚点"


def test_keyboard_up_placeholder_says_send_only():
    assert re.search(r'if \(keyboardUp\) "发消息…" else "发消息或按住说话…"', CHAT), \
        "键盘起着只写『发消息…』：长按语音只在键盘收起时可用（第 13 轮对标图）"
    assert '"按住说话"' in CHAT, "语音模式仍给『按住说话』字样"


def test_model_chip_next_to_plus_when_collapsed():
    # 收起态 [声纹][占位文本][模型钮][＋]：不展开输入框也能换模型
    assert re.search(r"if \(!expanded\) \{[\s\S]{0,220}if \(chipVisible\) ModelChip\(chipModel, onToggleChip\)", CHAT), \
        "用户点名『模型切换放到加号旁边，不然我进去了怎么找到』：收起态必须挂"
    assert CHAT.count("ModelChip(chipModel, onToggleChip)") == 2, \
        "收起态与展开态下行共用同一枚 ModelChip（两处调用点）"
    chip = re.search(r"private fun ModelChip\(chipModel: String, onToggleChip: \(\) -> Unit\)"
                     r"[\s\S]*?\n\}", CHAT)
    assert chip and "onToggleChip" in chip.group(0) and "IconChevronDown" in chip.group(0), \
        "ModelChip 是独立组件：点开全屏选模型页的回调原样接线"
    assert "fun ModelPickerScreen(" in CHAT and \
        re.search(r"if \(modelMenuOpen\) \{[\s\S]{0,200}ModelPickerScreen\(", CHAT), \
        "第 12 轮全屏选模型页不回退（它保持 fillMaxSize 整屏盖上）"


def test_earlier_rounds_do_not_regress():
    assert "SETTLE_MS" not in VOICE and "clearSettle" not in VOICE and \
        "const val MAX_SESSION_MS = 60_000L" in VOICE, \
        "第 15 轮改判：等包闹钟整个退役、松手即收尾，防挂死只剩 60s 硬顶一道——不许复活"
    assert re.search(r"val voiceArmed = input\.text\.isEmpty\(\) && !busy", CHAT) and \
        "!fieldPending" in CHAT, "第 10 轮闸门在位不挂框（零键盘闪）"
    assert re.search(r"enabled = \{[^}]*!keyboardUp", CHAT), \
        "键盘起着长按归系统复制/粘贴、语音不触发——活门控"
    assert "NoopTextToolbar" not in CHAT and "PopupTextToolbar" in CHAT, \
        "工具条第 15 轮起是系统 PopupMenu 壳，Noop 空壳不许复活"
    assert "fun stashIme()" in CHAT and "for (i in 1..4)" in CHAT
    assert "getWindowVisibleDisplayFrame" in VOICE and "getLocationOnScreen" in VOICE
    assert "换个语音引擎" in CHAT and "ENGINE_DEAD_CODES" in VOICE
    assert "ACTION_RECOGNIZE_SPEECH" not in CHAT


def test_release_notes_brief_format():
    doc = _read(RELEASE_DOC)
    allowed = {"修复了什么", "优化了什么", "新增了什么"}
    heads = re.findall(r"^##\s*(.+?)\s*$", doc, flags=re.M)
    assert heads and set(heads) <= allowed, \
        "更新日志三段式（第 12 轮钦定延续）：没有的不写，解释性段落禁止"
    assert not re.search(r"怎么修|根因|教训", doc), "简版口径：只报结果，不展开过程"
