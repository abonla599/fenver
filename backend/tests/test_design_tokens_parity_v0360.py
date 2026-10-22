import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
THEME = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/theme/Theme.kt"
CSS = REPO / "backend/app/web/static/style.css"

PAIRS = {
    "Bg": ("--fv-bg", "#0a0b0e"),
    "Surface": ("--fv-surface", "#121317"),
    "Surface2": ("--fv-surface-2", "#181a20"),
    "Line": ("--fv-line", "#262a31"),
    "Text": ("--fv-text", "#f2f3f5"),
    "Text2": ("--fv-text-2", "#a1a6b0"),
    "Text3": ("--fv-text-3", "#6f747e"),
    "Accent": ("--fv-accent", "#3dd598"),
    "Ai": ("--fv-ai", "#7c83ff"),
    "Danger": ("--fv-danger", "#f87171"),
}


def _hex(value):
    return "#" + value[4:].lower() if value.startswith("0x") else value.lower()


def test_native_and_web_share_the_same_tokens():
    theme = THEME.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    for name, (var, expected) in PAIRS.items():
        m = re.search(r"val %s = Color\(0x([0-9A-Fa-f]{8})\)" % name, theme)
        assert m, name
        native = "#" + m.group(1)[2:].lower()
        assert native == expected, (name, native, expected)
        assert re.search(re.escape(var) + r"\s*:\s*" + re.escape(expected), css, re.I), var
    assert "--fv-accent-soft: rgba(61, 213, 152, .12)" in css
    assert "val AccentSoft = Color(0x1F3DD598)" in theme
