"""极简原生 design tokens 的形状锁：色板只有一份、UI 不再引用旧名、窗口底色同源。

色值本身不在这里抄：官网 ↔ Web ↔ 安卓 的三方逐值对照住在
`test_design_tokens_parity_v0360.py`，它从 site.css 动态读真源。
本文件只管形状——令牌各声明一次、没有第二套浅色板、窗口底色跟 Bg 对得上。
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
THEME = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/theme/Theme.kt"
COLORS = REPO / "android-native/app/src/main/res/values/colors.xml"
THEMES = REPO / "android-native/app/src/main/res/values/themes.xml"


def _text(p):
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_minimal_palette_is_declared_once():
    src = _text(THEME)
    assert "object FenverTokens" in src
    assert "WebTokens" not in src
    # 一份色板：这些名字各只声明一次，值由三方对照那条锁去核。
    for name in ("Bg", "Surface", "Surface2", "Surface3", "Line", "Text",
                 "Text2", "Text3", "Accent", "Ai", "Danger"):
        hits = re.findall(r"val %s = Color\(" % name, src)
        assert len(hits) == 1, f"FenverTokens.{name} 声明了 {len(hits)} 次"


def test_single_theme_has_no_second_palette():
    src = _text(THEME)
    assert not re.search(r"val L[A-Z][A-Za-z0-9]* = Color\(", src), \
        "浅色 L* 令牌又回来了：官网是单主题，安卓只留一套墨绿"
    for forbidden in ("lightColorScheme", "isSystemInDarkTheme",
                      "object ThemeMode", "fun isWebLight"):
        assert forbidden not in src, f"单主题里不该还有 {forbidden}"
    assert "colorScheme = DarkScheme" in src, "AiTheme 不再固定用那一份色板"


def test_no_ui_file_still_says_webtokens():
    ui = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui"
    for path in ui.glob("*.kt"):
        text = _text(path)
        assert "WebTokens" not in text, f"{path.name} 还在用旧 token 名"
        assert "isWebLight" not in text, f"{path.name} 还在按主题分叉取色"
        assert "ThemeMode" not in text, f"{path.name} 还在写外观偏好"


def test_window_background_matches_token():
    """冷启动首帧的窗口底色必须与 Compose 画布逐字节相同（防白闪那条锁）。"""
    m = re.search(r"val Bg = Color\((0x[0-9A-Fa-f]{8})\)", _text(THEME))
    assert m, "Theme.kt 里找不到 FenverTokens.Bg：对照失效就喊人，别改这边"
    bg = m.group(1)[2:].upper()   # 剥掉 0x，与 colors.xml 的 #FFRRGGBB 写法对齐
    assert f"#{bg}" in _text(COLORS).upper(), \
        f"colors.xml 的 window_bg 不是 {bg}：键盘顶起窗口时会露出另一块底色"
    assert "@color/window_bg" in _text(THEMES)
