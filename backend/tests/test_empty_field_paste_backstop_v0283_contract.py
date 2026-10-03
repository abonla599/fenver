"""v0.28.3 真机反馈的兜底契约：空输入框长按弹不出「粘贴」，补一层不抢事件的观察器。

用户原话（附真机截图：聊天页、键盘已起、输入框空着显示「发消息…」）：
「我在这个界面长按怎么没有粘贴了？0.28.3修复一下」

结案：粘贴胶囊的壳与锚点公式和 v0.23.17（用户验收过的版本）逐字相同，病不在
这层壳。Compose 1.6.8 的长按链里「粘贴」这一项由 clipboardManager.hasText()
把关，而 hasText() 只认 text/plain 一种 MIME；国产 ROM 上不少 App 复制出来的
剪贴板带自定义 MIME（内容确实是文字），闸门说不算，showMenu 收到四个全 null
的回调，胶囊静默不弹。另一条可疑路径是整条长按链在个别 ROM 上压根没触发。
两条都在输入框这一层补同一个观察器：文本框自己的链照常跑，只有抬手之后工具条
仍未弹起才补位——直接读平台剪贴板（coerceToText 收非标准 MIME 的文字），
复用同一只 PopupTextToolbar 弹「粘贴」。

钉五件事：
① 观察器挂在输入位锚点 Box 上，只在「空框 + 键盘起 + 语音闸门不在位」时上班；
② 补位判据是自家工具条没弹起（status 非 Shown），且全程 Final 段、一口不吃；
③ 闭包全部 rememberUpdatedState 现取（第 8 轮冻快照那个病根不许复发）；
④ 剪贴板直读用 coerceToText，并拒掉图片/文件项回落的 content:// 与 file://；
⑤ 壳与锚点公式零改动（PopupTextToolbar 类体、两处挂载、1.6.8 接口形状），
   版本 51/0.28.3 + docs/releases/v0.28.3.md 三段式简版。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"
BUILD = REPO_ROOT / "android-native" / "app" / "build.gradle"
RELEASE_DOC = REPO_ROOT / "docs" / "releases" / "v0.28.3.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
BACKSTOP = re.search(r"@Composable\nprivate fun Modifier\.pasteBackstop\([\s\S]*?\n\}", CHAT)
CLIP = re.search(r"private fun readClipText\([\s\S]*?\n\}", CHAT)


def test_backstop_exists_and_mounted_on_anchor_box():
    assert BACKSTOP, "空框长按兜底观察器（Modifier.pasteBackstop）必须在位"
    assert ".then(voiceMod)\n                    .then(pasteGuardMod)" in CHAT, \
        "兜底挂在输入位锚点 Box 上（与语音状态机同一个节点），三态切换不销毁它"
    assert "val pasteGuardMod = Modifier.pasteBackstop(" in CHAT, "调用点就在 InputCard 组合里"
    elig = re.search(r"eligible = \{([^}]*?)\},", CHAT)
    assert elig and "gateOn" in elig.group(1) and "keyboardUp" in elig.group(1) \
        and "latestInput.text.isEmpty()" in elig.group(1), \
        "上班闸门三条件：语音闸门不在位 + 键盘起 + 框是空的（只兜被点名的场景）"
    assert "anchorRect = { fieldRect }" in CHAT and \
        ".onGloballyPositioned { fieldRect = it.boundsInRoot() }" in CHAT, \
        "补位弹的胶囊要贴着输入框：框的根坐标量下来递给 showMenu"


def test_backstop_only_fires_when_native_chain_stayed_silent():
    body = BACKSTOP.group(0)
    assert re.search(r"if \(toolbar\.status == TextToolbarStatus\.Shown\) return@awaitEachGesture", body), \
        "只在自家工具条没弹起时补位——文本框的链跑通了就一声不吭"
    assert body.count("PointerEventPass.Final") >= 2 and "PointerEventPass.Main" not in body, \
        "与 voiceHold 同款姿势：Final 段收事件，子节点 Main 段先处理完才轮到判定"
    assert ".consume(" not in body and "consumeChange" not in body, \
        "一口不吃：兜底观察器不许抢文本框的事件"
    assert "awaitFirstDown(requireUnconsumed = false)" in body, \
        "连 down 都不要求未消费——抢不过文本框时照常收得到"
    assert "withTimeoutOrNull(viewConfiguration.longPressTimeoutMillis)" in body, \
        "长按时限内抬手算普通点按，不放光标也要让路"


def test_backstop_closures_read_latest_state():
    body = BACKSTOP.group(0)
    assert "rememberUpdatedState" in body, \
        "pointerInput 只在 key 变化时重启协程，闸门/坐标/回调必须现取最新那份"
    assert "val latestInput by rememberUpdatedState(input)" in CHAT and \
        "var fieldRect by remember { mutableStateOf<Rect?>(null) }" in CHAT, \
        "调用侧同样备好消息源：input 与框坐标各挂一个可观察的最新值"
    assert re.search(r"onInput\(TextFieldValue\(cur\.text\.replaceRange\(start, end, text\)", CHAT), \
        "补位插入走 onInput，落在当前选区、光标停在粘贴文字之后"


def test_clipboard_read_accepts_nonstandard_mime_text_only():
    body = CLIP.group(0)
    assert "coerceToText(ctx)" in body, \
        "绕开 hasText() 的 text/plain 闸门：coerceToText 能把自定义 MIME 里的文字取出来"
    assert 't.startsWith("content://")' in body and 't.startsWith("file://")' in body, \
        "图片/文件项 coerceToText 吐回 URI，那不算可粘贴的文字，拒掉"
    assert "if (t.isEmpty()" in body, "空剪贴板不弹空胶囊"


def test_pill_shell_and_mounts_untouched():
    pill = re.search(r"private class PopupTextToolbar\([\s\S]*?\n\}", CHAT)
    body = pill.group(0)
    assert re.search(r"override fun showMenu\(rect: Rect, onCopyRequested", body), \
        "1.6.8 接口形状照旧（BOM 2024.06.00），本轮零改动"
    assert re.search(r"val x = \(rect\.right - w - 6f \* dp\)", body) and \
        re.search(r"val above = rect\.bottom - h - 6f \* dp", body), \
        "第 18 轮的选区末端落点公式不回退"
    assert re.search(r"setTextSize\(TypedValue\.COMPLEX_UNIT_SP, 13f\)", body), "13sp 小胶囊不回退"
    assert CHAT.count("LocalTextToolbar provides") == 2, "两处挂载数不变：只加观察器，没加第三个壳"
    assert re.search(r"toolbar\.showMenu\(rect, null, \{ pasteRef\.value\(\)\(text\) \}, null, null\)", CHAT), \
        "补位复用同一只壳、只递粘贴一项（第三参正是 onPasteRequested）"


def test_version_bumped_and_release_notes_brief():
    gradle = _read(BUILD)
    # 版本号只有一份真相（gradle），所以每一轮的契约文件都跟着钉**当前**那一格：
    # v0.28.0 推到 48，v0.28.1 推到 49，v0.28.2 推到 50，v0.28.3 推到 51。下面断的是现在那一格。
    assert "versionCode 51" in gradle and 'versionName "0.28.3"' in gradle, \
        "gradle 那一格漂了：这一版应是 51 / 0.28.3，且必须与 docs/releases/v0.28.3.md 同时存在"
    doc = _read(RELEASE_DOC)
    allowed = {"修复了什么", "优化了什么", "新增了什么"}
    heads = re.findall(r"^##\s*(.+?)\s*$", doc, flags=re.M)
    assert heads and set(heads) <= allowed, \
        "更新日志三段式（第 12 轮钦定延续）：没有的不写，解释性段落禁止"
    assert not re.search(r"怎么修|根因|教训|改判|逆向|把手", doc), "简版口径：只报结果，不展开过程"
