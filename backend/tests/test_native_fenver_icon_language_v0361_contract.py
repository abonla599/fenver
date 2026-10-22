"""v0.36.1 图标语言批的契约锁：Fenver 原创图标语言（统一入口 + 签名记号 + 无字形图标）。

用户 2026-10 的原话口径：「不是换一套 Material Icons，而是让整套图标有可识别的自家语言」。
这一把锁钉四件事：
① Icons.kt 的语言常量存在**且被绘制代码引用**：24 格网、1.8 描边、2.6 右上角切角、
   2 单位末端折角——常量只声明不引用等于没有语言；
② 统一入口：`enum class FenverIcon` + `@Composable fun FenverIcon(kind, tint, modifier)`，
   每个枚举臂都有分发几何；旧 IconXxx 名字留作**薄包装**（不许复制第二份几何）；
③ 四个 UI 文件（ChatUi/SettingsUi/SessionListUi/ScheduleUi）不再拿 Unicode 字形当图标：
   被钉死的字形名单只收"图标位"用过的那些；正文语义符号（· … ⚠ ⚠️ ✅ ❌ 以及
   「设置 → 模型服务」箭头路径文案、· 分隔符）明确豁免，不许误伤；
④ 没引入 Material Icons 或任何外部图标库（零依赖是这族的来由：Canvas 直绘）。
读法沿用本仓库判例：把 Kotlin 源码当文本读（剥注释，注释不上屏），负控自证在先。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_native_trace_polish_and_icon_v0300_contract import (  # noqa: E402
    _strip_kotlin_comments, _text)

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / \
     "assistant" / "nativeapp" / "ui"
ICONS = UI / "Icons.kt"
FOUR = [UI / name for name in ("ChatUi.kt", "SettingsUi.kt", "SessionListUi.kt", "ScheduleUi.kt")]
GRADLE = REPO_ROOT / "android-native" / "app" / "build.gradle"

# 图标位用过的字形名单：这一批全部换成了画出来的 FenverIcon，字符串字面量里一个都不许留。
# 注意 ⚠/⚠️/✅/❌/·/… 与「设置 → 账户」里的 → **不在名单**——那是正文语义符号。
# ● 同样豁免：它在「● 有未保存修改 · 保存将整日替换」里是正文状态记号，且这一句被
# test_schedule_ui_contract 的 SHARED_COPY 双端逐字同值钉死（网页同一串带 ●），
# 动它 = 双端文案漂移，比"少一枚画出来的点"代价大。
ICON_GLYPHS = ["×", "✓", "✗", "▋", "▾", "▸", "⚙", "✎", "🗑", "👍", "👎", "📄",
               "▤", "◈", "⌗", "«", "‹", "›", "»", "⚑", "✦", "⊙", "◫", "ⓘ", "↻",
               "☾", "✉", "↗", "◉", "⇄", "∗", "↓", "↑", "◆", "⚡", "★", "◎",
               "＋", "≡", "⌨", "📷", "🎙", "⌄", "☰", "🔍"]

# 语言常量：值 + 必须在绘制代码里被引用的名字
LANGUAGE_CONSTANTS = {
    "GRID": "24f",
    "STROKE": "1.8f",
    "BEVEL": "2.6f",
    "FOLD": "2f",
}

# 被旧契约钉着名字的薄包装（v0.23.4 / v0.23.11 / v0.23.12 / v0.28.0 / v0.30.0 读的是名字）
WRAPPERS = ["IconCamera", "IconMic", "IconKeyboard", "IconPlus", "IconArrowUp",
            "IconArrowDown", "IconStop", "IconImage", "IconFile", "IconChevronDown",
            "IconVoiceWave", "IconMenu", "IconTraceEye", "IconStepDone",
            "IconStepFailed", "IconStepBusy", "IconThinkingDots"]


def _icon_glyph_literals(src: str, banned=ICON_GLYPHS):
    """只挑字符串字面量里的图标字形（注释必须先剥——注释不上屏）。"""
    return [g for g in banned if re.search(r'"[^"\n]*%s[^"\n]*"' % re.escape(g), src)]


def test_the_detector_itself_works():
    """负控自证：这把尺子真的抓得到旧写法，不是又一条"检不到东西的检查看起来全绿"。"""
    assert _icon_glyph_literals('Text("⚙", fontSize = 15.sp)') == ["⚙"]
    assert _icon_glyph_literals('SetRow("↻", "检查更新"') == ["↻"]
    assert _icon_glyph_literals('CalNav("«", "上一年"') == ["«"]
    assert _icon_glyph_literals('Text("👍", on)') == ["👍"]
    # 正文语义符号必须放过（误伤一条好文案比漏一枚图标更糟）：
    assert _icon_glyph_literals('"⚠️ " + msg') == []
    assert _icon_glyph_literals('"当前没有可用模型，请在「设置 → 模型服务」中配置"') == []
    assert _icon_glyph_literals('Text("有未保存修改 · 保存将整日替换")') == []
    assert _icon_glyph_literals('Text("…中间 ${t.omitted} 步已省略…")') == []
    # 注释里提一句"原来这里打的是 ⚙"不该被判成代码里还有 ⚙：
    assert _icon_glyph_literals(_strip_kotlin_comments('/* 原来是 ⚙ */\nText("设置")')) == []


def test_language_constants_exist_and_are_referenced():
    """24/1.8/2.6/2 是这门语言的文法：常量存在，且绘制代码真在引用它们。"""
    src = _strip_kotlin_comments(_text(ICONS))
    for name, value in LANGUAGE_CONSTANTS.items():
        assert re.search(r"const val %s = %s\b" % (name, value), src), \
            f"语言常量 {name}={value} 漂了：整套图标的位置基准就没了"
    # 存在≠生效：逐个证明被引用（声明之外还有使用点）。
    for name in LANGUAGE_CONSTANTS:
        uses = len(re.findall(r"\b%s\b" % name, src))
        assert uses >= 2, f"常量 {name} 只被声明没被引用：语言文法成了摆设"
    assert "size.width / GRID" in src, "几何没有按 24 格网缩放（k=size/GRID）"
    assert "STROKE * k" in src, "描边没有按 1.8 设计单位换算"
    assert "bevel: Float = BEVEL" in src, "闭合外框的右上角切角没有默认吃 BEVEL"
    assert "FOLD / sqrt(" in src, "末端折角没有从 FOLD 一处真相算出来"


def test_unified_entry_dispatches_every_kind():
    """统一入口：enum 每一枚都有分发臂 + 几何；入口只有一个 Canvas 绘制核。"""
    src = _strip_kotlin_comments(_text(ICONS))
    m = re.search(r"enum class FenverIcon \{([\s\S]*?)\n\}", src)
    assert m, "Icons.kt 没有 enum class FenverIcon：同一语义同一枚图标没有名册"
    kinds = [k.strip() for k in re.split(r"[,\n]", m.group(1)) if k.strip()]
    assert kinds, "枚举名册是空的"
    assert "@Composable\nfun FenverIcon(kind: FenverIcon" in src or \
        re.search(r"@Composable\s+fun FenverIcon\(kind: FenverIcon", src), \
        "统一入口 @Composable fun FenverIcon(kind, tint, modifier) 不在了"
    for kind in kinds:
        assert re.search(r"FenverIcon\.%s ->" % re.escape(kind), src), \
            f"{kind} 在名册里没有分发臂：画出来是空的一格"
    assert src.count("Canvas(") == 1, \
        "Canvas 入口不止一枚：几何又各画各的，统一入口就不是唯一真相了"


def test_wrappers_are_thin_and_geometrylives_once():
    """旧名字的契约价值在"名字"，不在第二份几何：包装体只许转发给 FenverIcon。"""
    src = _strip_kotlin_comments(_text(ICONS))
    for name in WRAPPERS:
        m = re.search(r"fun %s\(([\s\S]*?)\n(@Composable|/|\n\n|$)" % name, src)
        assert m, f"薄包装 {name} 没了：v0.23.4/v0.30.0 的名字契约当场编不过"
        body = m.group(1)
        assert "FenverIcon(" in body, f"{name} 不再是薄包装：没有转发给统一入口"
        for geom in ("poly(", "seg(", "drawRoundRect", "drawCircle", "drawArc", "Canvas("):
            assert geom not in body, f"{name} 里又出现第二份 {geom} 几何：语言开始分家"


def test_four_ui_files_hold_no_unicode_icon_glyphs():
    """四个 UI 文件的字符串字面量里不再拿图标字形当按钮/行首/行尾；图标一律走 FenverIcon。"""
    legacy_wrapper = re.compile(r"(?<![A-Za-z0-9_])Icon[A-Z][A-Za-z0-9_]*\(")
    for f in FOUR:
        src = _strip_kotlin_comments(_text(f))
        left = _icon_glyph_literals(src)
        assert not left, f"{f.name} 又用 Unicode 字形当图标了（{left}）：换成 FenverIcon 那一枚"
        assert not legacy_wrapper.search(src), \
            f"{f.name} 还有绕过统一入口的 IconXxx(...) 直调：调用必须走 FenverIcon(…)"
        assert "FenverIcon(FenverIcon." in src, f"{f.name} 一枚图标都不走统一入口了？"


def test_no_material_icons_or_external_icon_library():
    """这族的存在理由就是"不拿现成图标库"：Material Icons / 外部图标依赖出现即红。"""
    assert "androidx.compose.material.icons" not in _text(ICONS), "Icons.kt 引了 Material Icons"
    src_root = REPO_ROOT / "android-native" / "app" / "src"
    for kt in src_root.rglob("*.kt"):
        text = _text(kt)
        assert "androidx.compose.material.icons" not in text, \
            f"{kt.name} 引了 Material Icons：自家语言不许外包"
        assert "Icons.Default" not in text, f"{kt.name} 用了 Icons.Default.*"
    gradle = _text(GRADLE)
    for dep in ("material-icons", "iconloader", "coil-svg"):
        assert dep not in gradle, f"build.gradle 混进外部图标依赖 {dep}"
