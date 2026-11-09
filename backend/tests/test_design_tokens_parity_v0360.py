"""官网 ↔ 网页版 ↔ 安卓：同一份墨绿色板的三方对照。

v0.36.0 那份契约钉的是「Web 与安卓同为近黑」，并把十个十六进制值抄在测试里。
色板要跟官网统一时，抄值就成了第二个真源：改官网的人不会想到来改测试，
改测试的人也未必同步三处。现在真源收归 `site/site.css` 的 :root —— 本文件
逐项从官网读出期望值，再去核 `/app/style.css` 的 --fv-* 与安卓 FenverTokens。
改了官网配色却漏改任意一端，这里当场红，不会等用户看出来。

顺带把「单主题」也钉住：官网没有明暗切换，两端都不许再分出第二套色板。
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SITE = REPO / "backend/app/web/site/site.css"
CSS = REPO / "backend/app/web/static/style.css"
ADMIN_CSS = REPO / "backend/app/web/admin/admin.css"
ADMIN_HTML = REPO / "backend/app/web/admin/index.html"
THEME = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/theme/Theme.kt"
HTML = REPO / "backend/app/web/static/index.html"

# (官网变量, Web 变量, 安卓字段)。派生色也列进来，防止某一端偷偷回到旧近黑板。
TRIPLES = [
    ("--bg", "--fv-bg", "Bg"),
    ("--surface", "--fv-surface", "Surface"),
    ("--surface-2", "--fv-surface-2", "Surface2"),
    ("--surface-3", "--fv-surface-3", "Surface3"),
    ("--line", "--fv-line", "Line"),
    ("--ink", "--fv-text", "Text"),
    ("--ink-soft", "--fv-text-2", "Text2"),
    ("--ink-faint", "--fv-text-3", "Text3"),
    ("--brand", "--fv-accent", "Accent"),
    ("--brand-bright", "--fv-accent-pressed", "AccentPressed"),
    ("--brand-soft", "--fv-accent-soft", "AccentSoft"),
    ("--brand-ink", "--fv-accent-ink", "BrandInk"),
    ("--ai", "--fv-ai", "Ai"),
    ("--ai-soft", "--fv-ai-soft", "AiSoft"),
    ("--danger", "--fv-danger", "Danger"),
    ("--bg", "--fv-code-bg", "CodeBg"),
]


def _root(path):
    m = re.search(r":root\s*\{([^}]*)\}", path.read_text(encoding="utf-8"))
    assert m, f"{path.name} 里找不到 :root"
    return m.group(1)


def _css_hex(block, name):
    m = re.search(re.escape(name) + r"\s*:\s*(#[0-9a-fA-F]{6})\s*;", block)
    assert m, f":root 里找不到 {name} 的十六进制色值"
    return m.group(1).lower()


def test_site_palette_is_the_single_source_for_web_and_android():
    site, web = _root(SITE), _root(CSS)
    theme = THEME.read_text(encoding="utf-8")
    for site_var, web_var, native in TRIPLES:
        expected = _css_hex(site, site_var)
        got_web = _css_hex(web, web_var)
        assert got_web == expected, (
            f"{web_var} 是 {got_web}，官网 {site_var} 是 {expected}：网页版没跟上官网")

        m = re.search(r"val %s = Color\(0x([0-9A-Fa-f]{8})\)" % native, theme)
        assert m, f"安卓 Theme.kt 里找不到 FenverTokens.{native}"
        assert m.group(1).upper() == "FF" + expected[1:].upper(), (
            f"FenverTokens.{native}=0x{m.group(1)}，官网 {site_var}={expected}：安卓没跟上官网")


def test_web_aliases_stay_mapped_to_the_shared_tokens():
    """旧名（--accent/--text/…）只允许做 var() 映射，不许再写死一份色值。"""
    web = _root(CSS)
    for alias, target in (("--accent", "--fv-accent"), ("--bg", "--fv-bg"),
                          ("--surface", "--fv-surface"), ("--text", "--fv-text"),
                          ("--line", "--fv-line"), ("--danger", "--fv-danger")):
        assert f"{alias}: var({target})" in web, f"{alias} 不再指向 {target}"
    # 官网是实色表面。这里只禁装饰性的线性渐变；驾驶舱那圈 conic-gradient
    # 是环形进度本身，由 test_v0391_web_cockpit_ui.py 另钉着，不在这一条的射程里。
    assert "linear-gradient" not in CSS.read_text(encoding="utf-8"), \
        "应用里又混进线性渐变：官网全站实色表面"


def test_typography_and_shapes_use_the_site_scale():
    """字体族与圆角档位跟官网同源；字号阶梯也搬过来了。"""
    site, web = _root(SITE), _root(CSS)
    for token in ("--step-0", "--step-1", "--step-2", "--step--1"):
        assert token in site and token in web, f"{token} 没在两端同时定义"
    assert "--fv-radius-sm: 14px" in web and "--fv-radius-lg: 24px" in web, \
        "圆角档位没对上官网的 14 / 24"
    assert "--fv-sans:" in web and "--fv-mono:" in web, "字体族没搬官网那一套"
    # 管理台的「安全来源指纹」等几处等宽小字改走这个令牌，字体栈的真值得在这守住。
    assert re.search(r"--fv-mono:\s*[^;]*ui-monospace", web), \
        "--fv-mono 不再是等宽字体：换了令牌名却丢了字体"
    assert "font: 400 var(--step-0)/1.68 var(--fv-sans)" in CSS.read_text(encoding="utf-8"), \
        "body 不再吃官网的字号/行高/字体族"


def test_admin_page_does_not_declare_a_second_palette():
    """管理台与 App 共用同一份 style.css，自己不许再定义色板。"""
    admin = ADMIN_CSS.read_text(encoding="utf-8")
    assert 'href="/app/style.css"' in ADMIN_HTML.read_text(encoding="utf-8"), \
        "管理台不再引用 /app/style.css：它得和 App 读同一份色板"
    assert not re.search(r"^\s*--fv-(bg|surface|text|accent|line)\s*:", admin, re.M), \
        "admin.css 又定义了一份 --fv-* 色板"
    assert 'data-theme="light"' not in admin


def test_single_theme_across_the_three_surfaces():
    css = CSS.read_text(encoding="utf-8")
    theme = THEME.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    assert 'data-theme="light"' not in css and "prefers-color-scheme" not in css, \
        "网页版又按主题分套了：官网是单主题"
    for forbidden in ("lightColorScheme", "isSystemInDarkTheme", "ThemeMode", "isWebLight"):
        assert forbidden not in theme, f"安卓又出现了 {forbidden}：单主题不该有第二套"
    assert 'id="themeBtn"' not in html and 'id="themeVal"' not in html, \
        "网页版的外观切换入口又回来了"
    assert 'name="theme-color" content="#2b3d34"' in html, \
        "theme-color 没钉成官网 --bg，系统 UI 会和页面差一个色"
