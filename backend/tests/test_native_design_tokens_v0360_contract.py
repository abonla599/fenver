"""v0.36.0 极简原生 design tokens 的形状锁：色板只有一份、UI 不再引用旧名、窗口底色同源。"""
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
    for needle in (
        "val Bg = Color(0xFF0A0B0E)",
        "val Surface = Color(0xFF121317)",
        "val Surface2 = Color(0xFF181A20)",
        "val Line = Color(0xFF262A31)",
        "val Text = Color(0xFFF2F3F5)",
        "val Accent = Color(0xFF3DD598)",
        "val Ai = Color(0xFF7C83FF)",
        "val LBg = Color(0xFFF7F8FA)",
        "val LAccent = Color(0xFF0A7D5E)",
    ):
        assert needle in src, "缺少 token：" + needle


def test_no_ui_file_still_says_webtokens():
    ui = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui"
    for path in ui.glob("*.kt"):
        assert "WebTokens" not in _text(path), f"{path.name} 还在用旧 token 名"


def test_window_background_matches_token():
    assert "#0A0B0E" in _text(COLORS).upper()
    assert "@color/window_bg" in _text(THEMES)
