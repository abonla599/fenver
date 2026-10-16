"""v0.34.2：原生「设置 → 关于 → 版本」这一行只报一个号码——能跑的那一版。

判什么
------
用户 2026-10 的口径是「只显示可运行的最新版本，不要两个相加什么的」。这一行从前是
`壳 v0.34.0 · 服务端 v0.34.1` 两段 join 出来的，第二段取自 GET /health 的 build 字段；
那个字段的形状本身就是 `git describe` 出来的 vX.Y.Z，在一部手机上会被读成
"这一版我也能装"，而服务端那一版装不到手机上——能装的只有壳，
且由紧邻的下一行「检查更新」报（那一行是可安装版本的唯一出口，本次没碰它）。

于是钉四件事，彼此分开：
1. 「版本」那一行的值槽里只有壳那一个号，且它的唯一来源是 BuildConfig.VERSION_NAME；
2. 整份 SettingsUi.kt 里第二个号的**来源**也没了（serverBuild / Api.health() 都不许回来）——
   只判第 1 条的话，"照样拉、拉来不用"也算绿，那是白跑一次网络；
3. 下一行「检查更新」照旧报可安装版本（防止有人为了"只有一个号"把它也删掉）；
4. 服务端 /health 的 build 字段**没被删**（AGENTS.md §1/§5：换包验收与
   tools/pre_tag_gate.py 都读它）。第 2 条与第 4 条是一对：少了第 4 条，
   这一屏的干净就是靠拆掉别人的判据换来的。

语料用的是 `_code()`（剥注释那把尺子，住在 test_apk_release_discipline.py）：
本次新增的说明注释里原样写着「壳 vX · 服务端 vY」那句旧文案，
判"某串不存在"的条目读原文会当场假红，读剥掉注释的就不会。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_apk_release_discipline import _code                    # noqa: E402
from tests.test_schedule_ui_contract import _native                   # noqa: E402

REPO = Path(__file__).resolve().parents[2]

VERSION_ROW = 'SetRow("ⓘ", "版本"'
UPDATE_ROW = 'SetRow("↻", "检查更新"'
NEXT_ROW = 'SetRow("☾", "外观"'


def _ui() -> str:
    return _code(_native("nativeapp/ui/SettingsUi.kt"))


def _slice(text: str, start: str, stop: str) -> str:
    at = text.index(start)
    return text[at:text.index(stop, at)]


def test_the_corpus_is_the_real_settings_screen():
    """正对照：两处切片都真切到了东西，否则下面每条都在空串上判、恒绿。"""
    ui = _ui()
    assert ui.count(VERSION_ROW) == 1, "「版本」那一行不再是恰好一处（改名/复制都会让切片失真）"
    assert ui.count(UPDATE_ROW) == 1, "「检查更新」那一行不再是恰好一处"
    ver = _slice(ui, VERSION_ROW, UPDATE_ROW)
    assert len(ver) > 20, f"「版本」那一行的切片短得不像话：{ver!r}"
    assert len(_slice(ui, UPDATE_ROW, NEXT_ROW)) > 200, "「检查更新」那一行被切空了"


def test_the_version_row_shows_the_shell_version_alone():
    """核心那条：值槽里只有壳那一个号，没有任何第二个来源参与。"""
    ui = _ui()
    ver = _slice(ui, VERSION_ROW, UPDATE_ROW)
    assert "SetValText(shellVer)" in ver, \
        "「版本」那一行的值槽不再是直接给壳的版本号（SetValText(shellVer)）：" + ver.strip()
    # shellVer 本身也只能有一个来源：出包那份 gradle 烘进 BuildConfig 的 versionName
    assert re.search(r'val shellVer = "v" \+ BuildConfig\.VERSION_NAME', ui), \
        "shellVer 不再只由 BuildConfig.VERSION_NAME 拼出来——版本号出现了第二个来源"
    for forbidden in ("serverBuild", "Api.health", "服务端", "joinToString", "listOfNotNull", '"壳 '):
        assert forbidden not in ver, \
            f"「版本」那一行又掺进了第二个号（命中 {forbidden!r}）：{ver.strip()}"


def test_the_second_number_has_no_source_left_on_the_settings_page():
    """整份文件里第二个号的来源一并没了：留着"拉来不用"等于白跑一次网络还留着坑。"""
    ui = _ui()
    assert "serverBuild" not in ui, "serverBuild 那份状态还在（值不许显示，来源也不该留着）"
    assert "Api.health" not in ui, "设置页还在拉 /health 的 build"
    assert "versionVal" not in ui, "两号相加那个中间变量还在，说明拼接的逻辑没走远"


def test_the_update_row_still_names_the_installable_version():
    """「检查更新」是可安装版本的唯一出口，这一行必须原样还在（本次改动不该碰它）。"""
    ui = _ui()
    upd = _slice(ui, UPDATE_ROW, NEXT_ROW)
    assert "已是最新版本 v${d.version}" in upd, "「检查更新」不再报可安装的那一版"
    assert "发现新版本 ${d!!.version}" in upd and "Updater.startCheck()" in upd, \
        "「检查更新」的三态或触发动作被一起删掉了"


def test_the_server_build_field_is_still_served():
    """反向一半：/health 的 build 字段没被删（AGENTS.md §1/§5 的判据依赖它）。"""
    main = (REPO / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    assert '"build": build_version()' in main, \
        "/health 不再回 build 字段——换包验收与 tools/pre_tag_gate.py 读的就是它"
    buildinfo = (REPO / "backend" / "app" / "core" / "buildinfo.py").read_text(encoding="utf-8")
    assert "def build_version() -> str:" in buildinfo, "构建戳的取法被删了"


def test_the_native_health_client_is_untouched():
    """客户端那条 health() 也留着：删它不属于本次范围，且它是公开端点的对位。"""
    api = _code(_native("nativeapp/Api.kt"))
    assert 'suspend fun health(): JsonObject' in api, "Api.health() 被删了"
    assert 'call("/health")' in api, "Api.health() 不再打 /health"
