"""v0.28 安卓（Kotlin 壳）三处体验交付的形状锁：贴底边界、悬浮回底箭头、窗口底色。

本机跑不了 :app:testDebugUnitTest，全是"读源文本"的形状锁（尺子沿用
test_settled_bottom_and_title_ink_v0252_contract 的 _strip_kotlin_comments）。
判据对着当前 Kotlin 是绿的：实现装回去之后才该红。

背景（用户真机反馈，2026-10-02）：
① 「输入框老是挡着上面的输出」——落定贴底拿旧一帧的高度做减法，条目事后长高
   （操作行晚一拍挂出）就把尾巴推进输入卡；改顶到滚动边界（MAX_SCROLL_OFFSET）。
② 「加一个像图片中的小箭头，点击之后直接跳转到最下方」——豆包同款悬浮回底钮，
   按 atBottom（最后一项底边在不在视口里）出现/收起。
③ 「点输入框时，键盘出现之前会出现白色闪屏」——adjustResize 顶起窗口时露的是
   **窗口底色**，此前吃 Material.Light 的白底；底色钉进资源（冷启动首帧）+
   AiTheme 随换肤重设（之后的一切帧）。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SHELL = REPO_ROOT / "android-native" / "app" / "src" / "main"
CHATUI_KT = SHELL / "java" / "xyz" / "fenever" / "assistant" / "nativeapp" / "ui" / "ChatUi.kt"
THEME_KT = SHELL / "java" / "xyz" / "fenever" / "assistant" / "nativeapp" / "theme" / "Theme.kt"
MANIFEST = SHELL / "AndroidManifest.xml"
COLORS = SHELL / "res" / "values" / "colors.xml"
THEMES = SHELL / "res" / "values" / "themes.xml"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_kotlin_comments(src: str) -> str:
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"' or c == "'":
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _kt_code() -> str:
    return _strip_kotlin_comments(_read(CHATUI_KT))


# ---------- ① 贴底顶到边界：越界 offset 夹底，不再拿旧高度做减法 ----------

def test_bottom_stick_clamps_to_the_scroll_boundary():
    code = _kt_code()
    assert re.search(r"MAX_SCROLL_OFFSET\s*=\s*Int\.MAX_VALUE\s*/\s*2", code), \
        "越界夹底的常量形状变了：LazyList 把它当【再往下也没有了】用，值必须远大于任何条目"
    assert "scrollToItem(t - 1, MAX_SCROLL_OFFSET)" in code \
        and "scrollToItem(total - 1, MAX_SCROLL_OFFSET)" in code, \
        "落定或流式有一处不再夹到滚动边界：条目事后长高时尾巴又会藏进输入卡"


def test_at_bottom_is_measured_from_the_layout_not_guessed():
    """atBottom 的判据是"最后一项底边进了视口"——与落定收工、箭头显隐同一份真相。"""
    code = _kt_code()
    assert "snapshotFlow" in code and "atBottom" in code, "atBottom 不再随布局流更新"
    assert re.search(r"(last|it)\.offset\s*\+\s*(last|it)\.size\s*<=\s*[\w.]*viewportEndOffset", code), \
        "「在不在底部」的判据漂了：应当是最后一项底边（offset+size，1.6.8 的 ListItemInfo 没有 .end）≤ 视口底"


# ---------- ② 悬浮回底箭头：!atBottom 出现，点一下夹到边界 ----------

def test_floating_jump_to_bottom_button_exists_and_acts():
    code = _kt_code()
    m = re.search(r"AnimatedVisibility\(\s*visible\s*=\s*!atBottom[\s\S]*?IconArrowDown[\s\S]*?\n            \}",
                  code)
    assert m, "找不到 !atBottom 门控、内含 IconArrowDown 的 AnimatedVisibility：悬浮回底箭头没了"
    block = m.group(0)
    assert "Alignment.BottomCenter" in block, "箭头不再悬在输入卡上方居中（参考图豆包同款）"
    assert "composerPx" in block, "箭头高度不再按 composer 实测高度让位——会骑在输入卡上"
    assert "MAX_SCROLL_OFFSET" in block, "点击动作不再是【夹到滚动边界】"
    assert "IconArrowDown" in block, "箭头图标丢了"


def test_arrow_down_icon_is_the_mirrored_up_arrow():
    icons = _strip_kotlin_comments(
        _read(SHELL / "java" / "xyz" / "fenever" / "assistant" / "nativeapp" / "ui" / "Icons.kt"))
    m = re.search(r"fun IconArrowDown\(tint: Color[^\n]*\n([\s\S]*?)\n    \}", icons)
    assert m, "Icons.kt 里没有 IconArrowDown：ChatUi 的箭头钮编不过"
    body = m.group(1)
    assert "seg(" in body and "poly(" in body, "下箭头应当是杆 + 人字头（与 IconArrowUp 同构）"


# ---------- ③ 窗口底色：资源钉首帧，Compose 跟换肤 ----------

def test_manifest_theme_pins_the_window_background():
    manifest = _read(MANIFEST)
    assert 'android:theme="@style/Theme.Fenver"' in manifest, \
        "活动又吃系统主题的白底：键盘顶起窗口时输入卡下方会再露出白板"
    assert 'android:windowSoftInputMode="adjustResize"' in manifest, \
        "adjustResize 是「键盘与输入框紧贴」的前提，不许被动过"
    themes = _read(THEMES)
    assert 'name="Theme.Fenver"' in themes and "windowBackground" in themes, \
        "Theme.Fenver 或它的 windowBackground 没了"
    colors = _read(COLORS)
    xml_bg = re.search(r'name="window_bg">#([0-9A-Fa-f]{8})<', colors)
    assert xml_bg, "colors.xml 里找不到 window_bg"
    kt_bg = re.search(r"val Bg = Color\((0x[0-9A-Fa-f]{8})\)", _read(THEME_KT))
    assert kt_bg, "Theme.kt 里找不到 WebTokens.Bg——对照失效就喊人，别改这边"
    assert xml_bg.group(1).upper() == kt_bg.group(1)[2:].upper(), \
        f"资源底色 {xml_bg.group(1)} 与 Compose 深色画布 {kt_bg.group(1)} 不是同一个颜色"


def test_ai_theme_resyncs_window_background_on_theme_switch():
    """换肤（深色↔浅色/跟随系统）时窗口底色必须跟着改：资源只管冷启动首帧。"""
    theme = _strip_kotlin_comments(_read(THEME_KT))
    m = re.search(r"fun AiTheme\([\s\S]*?\n\}", theme)
    assert m, "找不到 AiTheme 函数体"
    body = m.group(0)
    assert "setBackgroundDrawable(ColorDrawable(bg.toArgb()))" in body, \
        "AiTheme 不再随外观重设窗口底色：浅色用户会等到一块深色闪屏（对称的旧 bug）"
    assert "DisposableEffect(bg)" in body, "底色重设没有挂在对 bg 的 effect 上：换肤不生效"
