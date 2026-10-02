"""v0.28.1 修复的形状锁：一条 401 只作废当前身份，绝不抹掉全机身份清单。

背景（用户真机反馈，2026-10-02）：升级到 v0.28.0 后报「模型丢失、无法对话、
之前账号的会话丢失」。根因链：服务端每账户令牌封顶 8 条、超了静默挤最旧
（auth.py MAX_SESSION_TOKENS，属设计）；壳里任何一次 401 都走 Prefs.clearAuth()
把 SharedPreferences 里**所有**身份连 token/providerId/lastSessionId 一起清光，
把一次可恢复的令牌过期放大成"账户全没"。修复：401 路径改 Prefs.forgetCurrent()
（只忘当前条目）。另修误导文案「重新注册」→「重新登录」——让人去注册新号才是
「会话丢失」观感的最后一公里。

本机跑不了 :app:testDebugUnitTest，沿用 test_bottom_clamp_*_v0280_contract
的读源文本判据（尺子先剥 Kotlin 行注释，注释里提 clearAuth 不算违规）。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SHELL = REPO_ROOT / "android-native" / "app" / "src" / "main"
CHATUI_KT = SHELL / "java" / "xyz" / "fenever" / "assistant" / "nativeapp" / "ui" / "ChatUi.kt"
SESSION_LIST_KT = SHELL / "java" / "xyz" / "fenever" / "assistant" / "nativeapp" / "ui" / "SessionListUi.kt"
SETTINGS_UI_KT = SHELL / "java" / "xyz" / "fenever" / "assistant" / "nativeapp" / "ui" / "SettingsUi.kt"
APP_JS = REPO_ROOT / "backend" / "app" / "web" / "static" / "app.js"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_kotlin_line_comments(src: str) -> str:
    src = "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)   # 块注释（注释里提 clearAuth 不算违规）


def test_no_clearauth_on_401_paths():
    """两条 401 登出路径只许 forgetCurrent，不许 clearAuth（注释除外）。"""
    for f in (CHATUI_KT, SESSION_LIST_KT):
        code = _strip_kotlin_line_comments(_read(f))
        assert "Prefs.clearAuth()" not in code, f.name + "：401 路径仍在清空全机身份"
    assert "Prefs.forgetCurrent()" in _strip_kotlin_line_comments(_read(CHATUI_KT))
    assert "Prefs.forgetCurrent()" in _strip_kotlin_line_comments(_read(SESSION_LIST_KT))


def test_clearauth_kept_only_for_base_url_switch():
    """换服务器=换身份世界，那里全清是对的——全仓唯一 clearAuth() 调用点只剩它。"""
    prefs = _read(SHELL / "java" / "xyz" / "fenever" / "assistant" / "nativeapp" / "Prefs.kt")
    assert "fun clearAuth()" in prefs          # 能力保留，不给 401 路径用
    assert "baseUrl = url\n        clearAuth()" in prefs
    callers = [f for f in (SHELL / "java").rglob("*.kt")
               if "clearAuth()" in _strip_kotlin_line_comments(_read(f))]
    assert [f.name for f in callers] == ["Prefs.kt"], f"401 外的全机清空调用点: {callers}"


def test_401_copy_says_relogin_on_both_surfaces_verbatim():
    """双端逐字纪律：401 那句必须是「重新登录」，且两端完全同文。"""
    native = re.search(r'e\.status == 401\) return "([^"]+)"', _read(SETTINGS_UI_KT))
    web = re.search(r'e\.status === 401\) return "([^"]+)"', _read(APP_JS))
    assert native and web, "找不到 401 文案行（两端正逐字成对）"
    assert "重新登录" in native.group(1) and "重新注册" not in native.group(1)
    assert native.group(1) == web.group(1), "双端文案漂移"
