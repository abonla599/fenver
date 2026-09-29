"""第 12 轮真机反馈的双端契约（v0.23.11）：豆包全屏语音接管 + 三态输入区 + 全屏选模型页。

用户原话（附六张对标图）：「打开软件时的对话框和第一个图片一样，单点对话框之后
出现了第二个图片的模样，当我点击图片中的Auto时弹出第三个图片中的界面。我说的取代
是像这样完全看不到对话框，输入框，你这个语音输入明显是嵌入进对话框的，模型切换按钮
都显示出来了。」——第 11 轮的行级覆盖层再被判不够：「取代」= 豆包图 4/5 那种整屏
渐变场景。本轮另收一条交付纪律：更新日志只写「修复了什么/优化了什么/新增了什么」，
没有的不写，不解释怎么修的。

钉五件事：
① 语音卡片：voice.listening 时根层渲染 VoiceScreen——第 13 轮真机判「全屏太大不
   美观，大小比输入框大点就行」，收小成贴底圆角卡片（BottomCenter，判据见 v02312
   契约）——录音绿渐变「松手发送，上移取消」+ 白波纹，上滑粉渐变「松手取消」+
   红波纹；覆盖层不挂 pointerInput，手势锚点留在输入位 Box（摘锚点=掐死录音协程，
   第 11 轮教训）；
② 三态输入区（对标图 1/2）：收起=单胶囊 [声纹][发消息或按住说话…][＋]（第 13 轮
   ＋左边加模型切换钮）；单点展开=上行「发消息…」文本区 + 下行 [声纹][模型钮]
   [＋][发送/停止]；相机钮从输入条退役（＋面板里的相机 tile 照旧）；
③ 全屏「选择模型」页（对标图 3）：点模型钮整屏盖上来——标题居中 + 右上 × +
   模型清单当前项 ✓；输入卡下挂的旧小面板退役；
④ 第 9~11 轮门控不回退：键盘起着长按归系统复制/粘贴菜单（默认工具条、无覆写）、
   闸门在位不挂框（零键盘闪）、短按转发、stashIme 双保险、坐标差检测；
⑤ 更新日志简版纪律：docs/releases 现行版只含 修复/优化/新增 三段式，
   不许再出现「验证方法/注意/怎么修的」这类解释性段落。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"
RELEASE_DOC = REPO_ROOT / "docs" / "releases" / "v0.23.11.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")


def test_voice_takeover_is_bottom_card():
    assert re.search(r"if \(voice\.listening\) \{[\s\S]{0,160}VoiceScreen\(voice\.heard, "
                     r"voice\.level, voiceCancel,\s*Modifier\.align\(Alignment\.BottomCenter\)", CHAT), \
        "录音中根层渲染语音卡：第 13 轮钦定贴底小卡片，不再整屏（太大不美观）"
    screen = re.search(r"fun VoiceScreen\(heard: String, level: Float, cancelling: Boolean"
                       r"[\s\S]*?\n\}", VOICE)
    assert screen, "VoiceScreen 必须在 VoiceUi 里"
    body = screen.group(0)
    assert "松手发送，上移取消" in body and "松手取消" in body, \
        "豆包两态文案原样：录音提示 / 上滑取消"
    assert "fillMaxSize" not in body and "RoundedCornerShape" in body \
        and "verticalGradient" in body, \
        "第 13 轮改判：收小成圆角卡片（大小比输入框大点就行），全屏不许回魂"
    assert "WaveBars(level" in body, "实时波纹搬进全屏场景"
    assert "pointerInput" not in body, \
        "覆盖层绝不挂 pointerInput：吃了事件录音状态机就断（锚点在输入位）"
    assert ".then(voiceMod)" in CHAT and "Box(Modifier.weight(1f).heightIn(min = 42.dp)" in CHAT, \
        "手势锚点仍钉在输入位 Box 上，三态共用同一节点"


def test_doubao_three_state_input_card():
    assert re.search(r"val expanded = keyboardUp \|\| input\.text\.isNotEmpty\(\) \|\| busy", CHAT), \
        "三态判据（对标图 1→2）：键盘起/有字/生成中才展开成两行，收起是单胶囊"
    assert re.search(r'if \(keyboardUp\) "发消息…" else "发消息或按住说话…"', CHAT), \
        "第 13 轮改判：键盘起着只写「发消息…」，不再提示长按语音（语音只在键盘收起时）"
    assert "IconVoiceWave(" in CHAT, "声纹钮（图 1/2 左侧 ·)) 图标）：语音模式开关"
    assert "GhostCircleButton(onClick = onCamera)" not in CHAT, \
        "相机钮从输入条退役（图 1 里没有它）；＋面板的相机 tile 照旧"
    assert '"发消息或按住说话…"' in CHAT and '"按住说话"' in CHAT


def test_model_button_opens_fullscreen_picker():
    assert "fun ModelPickerScreen(" in CHAT, "点模型钮出全屏「选择模型」页（对标图 3）"
    assert re.search(r"if \(modelMenuOpen\) \{[\s\S]{0,200}ModelPickerScreen\(", CHAT), \
        "模型页挂在根层整屏盖上，不是输入卡下挂的小面板"
    page = re.search(r"fun ModelPickerScreen\([\s\S]*?\n\}", CHAT).group(0)
    assert "选择模型" in page and "×" in page and "✓" in page, \
        "页三件套：居中标题、右上 ×、当前项 ✓"
    assert "fillMaxSize" in page and "verticalScroll" in page
    assert "AnimatedVisibility(visible = modelMenuOpen" not in CHAT, \
        "旧下挂小面板退役——同一开关不许有两个出口"


def test_round9_to_11_gates_do_not_regress():
    assert "NoopTextToolbar" not in CHAT and "PopupTextToolbar" in CHAT, \
        "长按必须出菜单（第 11 轮口径）：第 15 轮起接的是系统 PopupMenu 壳，Noop 空壳不许复活"
    assert re.search(r"enabled = \{[^}]*!keyboardUp", CHAT), \
        "键盘起着长按归文本选择、语音不触发——活门控原样在位"
    assert re.search(r"val voiceArmed = input\.text\.isEmpty\(\) && !busy", CHAT) and \
        "!fieldPending" in CHAT, \
        "闸门在位不挂框（零键盘闪）+ 短按转发挂回：第 10 轮地基不动"
    assert "LaunchedEffect(fieldPending, keyboardUp)" in CHAT and "keyboardController?.show()" in CHAT
    assert "fun stashIme()" in CHAT and "for (i in 1..4)" in CHAT
    assert "getWindowVisibleDisplayFrame" in VOICE and "getLocationOnScreen" in VOICE
    assert "SETTLE_MS" not in VOICE and "const val MAX_SESSION_MS = 60_000L" in VOICE, \
        "第 15 轮改判：松手即收尾，等包闹钟退役，防挂死靠 60s 硬顶"
    assert "换个语音引擎" in CHAT and "ACTION_RECOGNIZE_SPEECH" not in CHAT


def test_release_notes_brief_format():
    doc = _read(RELEASE_DOC)
    allowed = {"修复了什么", "优化了什么", "新增了什么"}
    heads = re.findall(r"^##\s*(.+?)\s*$", doc, flags=re.M)
    assert heads and set(heads) <= allowed, \
        "第 12 轮用户钦定：更新日志只写 修复/优化/新增 三段，没有的不写——" \
        "『验证方法/注意/怎么修的』这类解释段落整块禁止"
    assert not re.search(r"怎么修|根因|教训", doc), \
        "简版口径：只报结果，不展开过程"
