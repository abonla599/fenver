"""M-8（2026-10-27 安全审计）：安卓凭据与通知的源码形状锁。

这几条都是"代码里看着对、摘掉也照样跑"的防线，所以每条都要能在源码里指到
具体形状；测试读的是剥过注释的源码（`_code` 与发版纪律共用一把尺子），
避免注释里写一句"已加密"就把锁喂饱。

真机验收仍另算：本文件只证明代码形状，不证明 Keystore 在某台 ROM 上建得起来。
"""

from pathlib import Path

from tests.test_apk_release_discipline import _code

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"


def _src(path: Path) -> str:
    return _code(path)


def test_session_tokens_live_in_the_encrypted_store():
    secret = _src(PKG / "SecretStore.kt")
    prefs = _src(PKG / "Prefs.kt")
    assert 'TOKEN_PREFIX = "session_token:"' in secret
    for fn in ("fun saveToken(", "fun readToken(", "fun dropToken("):
        assert fn in secret, f"SecretStore 少了 {fn}"
    assert "SecretStore.saveToken(userId, token)" in prefs
    assert "SecretStore.readToken(userId)" in prefs
    # 旧版那条明文路径必须已经拆掉：addIdentity 不能再把 res.token 写进 Identity
    assert "token = res.token" not in prefs


def test_legacy_plaintext_tokens_are_migrated_and_erased():
    prefs = _src(PKG / "Prefs.kt")
    assert "migrateLegacyTokens()" in prefs
    assert "it.copy(token = \"\")" in prefs


def test_evicted_identities_are_logged_out_server_side():
    prefs = _src(PKG / "Prefs.kt")
    auth = _src(PKG / "ui" / "AuthUi.kt")
    settings = _src(PKG / "ui" / "SettingsUi.kt")
    # addIdentity 把被挤掉的令牌交回调用方
    assert "fun addIdentity(res: AuthResult): List<String>" in prefs
    assert "evicted += dead" in prefs
    # 两个调用点都拿这份清单去调服务端 logout
    assert "val evicted = Prefs.addIdentity(res)" in auth
    assert "evicted.forEach { runCatching { Api.logout(it) } }" in auth
    assert "val evicted = Prefs.addIdentity(AuthResult(" in settings
    assert "evicted.forEach { runCatching { Api.logout(it) } }" in settings
    # 退出/删除账号也不再读条目里那个已搬走的 token 字段
    assert "val tok = Prefs.tokenFor(hit.userId)" in settings
    assert "Api.logout(tok)" in settings
    assert "val dead = Prefs.tokenFor(x.userId)" in settings


def test_login_copy_stops_claiming_data_never_leaves_the_phone():
    auth_raw = (PKG / "ui" / "AuthUi.kt").read_text(encoding="utf-8")
    assert "对话与记忆只存在这台机器上" not in auth_raw
    assert "对话与记忆保存在你连接的服务器上" in auth_raw
    assert "只存这台机器上的摘要" not in auth_raw


def test_notifications_hide_content_on_the_lock_screen():
    reminders = _src(PKG / "Reminders.kt")
    export = _src(PKG / "export" / "ExportTracker.kt")
    for src in (reminders, export):
        assert "VISIBILITY_PRIVATE" in src
        assert "setPublicVersion(publicVersion)" in src
    assert "有一条提醒到点了" in reminders
    assert "有一条导出结果通知" in export


def test_compose_backup_path_has_limits_and_a_real_healthcheck():
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "cpus:" in compose and "memory:" in compose and "pids:" in compose
    assert "http://localhost:8000/health" in compose
    assert 'get(\'status\') == \'ok\'' in compose


def test_user_visible_exception_text_goes_through_path_masking():
    # _code 是 Java/Kotlin 的剥注释尺子，对 Python 会把字符串里的 /* 当注释开头；
    # 这里只按行去掉 # 注释，够这条锁用。
    raw = (REPO_ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    main = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("#"))
    assert "detail=str(e)" not in main
    assert main.count("detail=hide_paths(str(e))") >= 15
    # f-string 拼接与 scrub_secrets 这两形同样会把磁盘路径带出去（复核发现）
    assert 'detail=f"反馈保存失败：{e}"' not in main
    assert 'detail=hide_paths(f"反馈保存失败：{e}")' in main
    assert 'detail=f"意见反馈保存失败：{e}"' not in main
    assert 'detail=hide_paths(f"意见反馈保存失败：{e}")' in main
    assert "detail=scrub_secrets(str(e))" not in main
    assert "detail=hide_paths(scrub_secrets(str(e)))" in main
