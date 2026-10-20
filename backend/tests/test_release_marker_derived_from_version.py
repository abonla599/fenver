"""换包用的 `-Marker` 必须是**版本号推导出来的**，不是人在窗口里挑的（#43）。

事故形状：`deploy/swap-package.ps1` 的预检把标记数两次（暂存包必须有、装机包必须没有），
而**只改后端的一版**里 `backend/app/web/` 一字未动，天然不存在这样的串。v0.34.2 过得去是
因为有人手工在 `app.js` 里补了一行 `release-marker-v0.34.2`——那是补产物，不是补机制
（`ff85ee0f` 补的是上一版的产物）。手工挑串有两种红法，且都在窗口打开之后才被发现：
挑了两边都有的串 = 预检被自己人盖章、换包后的 ACCEPT 空转；挑了新包里没有的串 = 预检
当场拒绝，而看门狗已经停了。

本文件钉的是 `tools/release_marker.py` 那条推导规则：

    「这一版要发的是哪个号、是哪一形态」= docs/releases/<tag>.md 的 scope 行（**不是** versionName）
    -Marker   逐字符等于 release-marker-v<VERSION>
    有安装包那一版（说明没带 scope 或写 installer）：versionName 必须已等于这一版，
            标记在 app.js 里恰好命中一行，且是 app.js 里最高的那枚
    只换服务端那一版（<!-- scope: server-only -->，v0.34.1 就是这一形态）：**不要求 bump
            versionName**（它本来就不动）。#69 把这一支的判据从"app.js 里在不在"换成一条
            **世界事实**：这一枚标记**不得出现在线上那份包里**（dist/run_backend/_internal/
            app/web/static/app.js，与 swap-package.ps1 数的装机侧同一份）。app.js 里在不在
            只说明走到哪一步——没有 ⇒ 补这一行，有了 ⇒ 可以构建，两种都绿；已经在装机包里 ⇒
            无论仓库怎样都红（那两侧就分不开，就是空转验收）。且 app.js 里不得有比它更新的
            标记。装机侧读不到 ⇒ 退 2 fail-closed，**绝不**默认成"里面没有"
    任意两枚标记不得互为子串（`Select-String -SimpleMatch` 数子串，v0.34.1 会命中 v0.34.10）
    docs/releases 里最高的那一版不得跑在目标版本前面 —— **但这一格也吃同一条 scope 判读**
            （#71）：最高那一版自己声明 `scope: server-only` 时，"说明比声明侧新"正是这一形态
            的预期形状（只换服务端的一版不 bump versionName），裸跑必须绿；没声明 / 声明读不出
            ⇒ 照旧红，这条豁免不是"说明领先就不管了"的旁路
    **裸跑（不带 --expect）的 target/scope**：只看 versionName 会把"只换服务端的一版在途"读成
            installer，而那一版的 -Marker 按设计就该领先 versionName（新包里有、线上包里绝没有），
            于是把正确形状判成红、并把上一版那枚已经躺在装机包里的串印成"该用的那一串"。
            下面几条同时成立才换 target：最新那份说明领先 versionName、它自己声明
            `scope: server-only`、app.js 里最新那枚标记也领先 versionName ⇒ target=那份说明的
            版本，判据是「app.js 里最新 marker == 那个领先的版本」（等号右边取自说明，所以对不上
            说明的领先标记照旧红）。任何一条不成立 ⇒ 回到原判据（target=versionName、installer
            那一支原有的判据一字未改），"有一条 server-only 说明"本身不是放行理由。
            **这一支的射程**：只读仓库里的 app.js 与那份说明，**不读装机侧那份包**——裸跑在没有
            dist\\ 的机器（CI、新克隆、清过盘）上也要能给出结论，所以"这枚串线上是不是已经有了"
            只有 `--expect` 那一路判（另有测试钉住同一棵树在那一路上的读法）。判不了的：说明不存在、
            说明不领先、scope 行缺失 / 值不认识 / 读不出文本、标记还没补上、装机侧是否已有同一枚
            串、versionName 该不该追平（只换服务端的那一版不追）
    说明缺 / 读不到 / scope 值不认识 ⇒ 退 2（fail-closed），不放行也不许拿 bump 当替代

**为什么不在 `tools/pre_tag_gate.py` 里加第 6 条**：那把闸的第 3 条要求线上 `/health` 的
build **已经**等于这个 tag，也就是它只有在下**线之后**才可能绿；而标记缺不缺必须在上
线之前说，否则拦住的时刻正是最怕的时刻。发版顺序里排在"构建 staging"之前的只有
`CI 全绿 → 本地全量复跑`，所以常驻判据落在这里。

反例全部是 `tmp_path` 下现造的假树（`android-native/app/build.gradle` +
`backend/app/web/static/app.js` + `docs/releases/` + 一棵**假的**装机包
`dist/run_backend/_internal/app/web/static/app.js`），**不碰仓库里的真文件、不碰真
`dist\\`（这台机器上它就是线上服务）、更不碰 `data\\`（真数据目录）**：判据读的是传进去的
`repo_root`，这一点由 `test_the_tool_reads_only_the_root_it_is_handed` 钉住。
"""
import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "release_marker.py"
GATE = REPO_ROOT / "tools" / "pre_tag_gate.py"
CHANGELOG = REPO_ROOT / "tools" / "gen_changelog.py"
SWAP_PS1 = REPO_ROOT / "deploy" / "swap-package.ps1"
APP_JS = REPO_ROOT / "backend" / "app" / "web" / "static" / "app.js"
SCOPE_SERVER_ONLY = "<!-- scope: server-only -->"
SCOPE_INSTALLER = "<!-- scope: installer -->"
# 测试自己独立读一遍"最新那份说明"，用**字面**正则，不借 `rm` 的那几把尺子：判据与探针同构
# 就等于恒绿自证，所以这里连文件名形状、scope 行、标记串都各抄一份自己的读法。
NOTE_NAME_RE = re.compile(r"^v(\d+\.\d+\.\d+)\.md$")
SCOPE_LINE_RE = re.compile(r"^\s*<!--\s*scope:\s*([A-Za-z0-9._-]+)\s*-->\s*$")
NOTE_MARKER_RE = re.compile(r"release-marker-v(\d+\.\d+\.\d+)")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rm = _load(TOOL, "release_marker_under_test")
gate = _load(GATE, "pre_tag_gate_for_regex_drift")
cl = _load(CHANGELOG, "gen_changelog_for_scope_drift")


# ------------------------------------------------------------------ 假树脚手架
LIVE_APP_JS = Path("dist") / "run_backend" / "_internal" / "app" / "web" / "static" / "app.js"


def _gradle_versionname() -> str:
    """测试自己独立读一遍声明侧：期望值**不许**从要验的那把尺子的输出里拿。

    这里用字面正则、不用 `rm.VERSION_NAME_RE`，所以"工具读错了文件"照样会红；而版本号本身
    是从 `android-native/app/build.gradle` 现读的，不是写死的 `0.34.2`——写死的那行每次发版
    都要人记得改、改漏了就常年红着（#69 就是要拆掉这种会腐烂的字面量）。
    """
    text = (REPO_ROOT / "android-native" / "app" / "build.gradle").read_text(encoding="utf-8")
    match = re.search(r'^\s*versionName\s+"([^"]+)"', text, re.M)
    assert match, "build.gradle 里读不到 versionName，这条锁没有输入"
    return match.group(1)


def _fake_repo(root: Path, version_name="0.34.2", markers=("0.34.2",), notes=("v0.34.2",),
               server_only=(), live_markers=None, installer_only=()):
    """只放判据真正会读的那几样东西，路径形状与仓库一致。

    `server_only` 里点名的那份说明会带上机器读的 `<!-- scope: server-only -->`（写在标题之后、
    第一个小节之前，与仓库里 v0.34.1.md 的位置一致）；`installer_only` 里点名的那份带上
    `<!-- scope: installer -->`（安装包形态那一支的机器读法）；两个名单都不含的说明就是缺省
    形状（没有 scope 行）。

    `live_markers` 是**假日机包**里已有的标记（缺省跟着 `markers`，即"仓库和线上是同一版"那种
    已上线状态）：#69 之后只换服务端那一版要读装机侧那一份，所以假树里必须自己带一棵假的
    `dist\\run_backend\\...`，测试永远不碰真 `dist\\`。
    """
    gradle = root / "android-native" / "app" / "build.gradle"
    gradle.parent.mkdir(parents=True, exist_ok=True)
    gradle.write_text(
        'android {\n    defaultConfig {\n        versionCode 61\n'
        '        versionName "%s"\n    }\n}\n' % version_name,
        encoding="utf-8")
    static = root / "backend" / "app" / "web" / "static"
    static.mkdir(parents=True, exist_ok=True)
    body = "/* Fenver PWA 主逻辑 */\n"
    for one in markers:
        body += "/* release-marker-v%s：本版说明 */\n" % one
    (static / "app.js").write_text(body, encoding="utf-8")
    if live_markers is None:
        live_markers = markers
    live_static = root / LIVE_APP_JS.parent
    live_static.mkdir(parents=True, exist_ok=True)
    live_body = "/* 假日机包（不是真 dist\\） */\n"
    for one in live_markers:
        live_body += "/* release-marker-v%s：装机包 */\n" % one
    (live_static / "app.js").write_text(live_body, encoding="utf-8")
    releases = root / "docs" / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    for note in notes:
        if note in server_only:
            text = "# %s\n\n%s\n\n本版只换服务端，安卓包不动。\n" % (note, SCOPE_SERVER_ONLY)
        elif note in installer_only:
            text = "# %s\n\n%s\n\n本版有可安装的产物。\n" % (note, SCOPE_INSTALLER)
        else:
            text = "# %s\n" % note
        (releases / ("%s.md" % note)).write_text(text, encoding="utf-8")
    return root


def _repo_side_bare_expectation(root: Path):
    """测试自己算一遍"裸跑该给哪一串"：最新那份说明 + 装机无关的仓库文本，字面正则，
    不 import `rm` 的任何函数——否则这条锁只是在复读判据自己的输出。

    返回 `(scope, target)`，其中 target 是不带 v 的三段号。算的过程就是被判据那几条：
    最新说明领先 versionName、那份说明自己写着 server-only、app.js 里最新那枚标记也领先
    versionName；任何一条不成立 ⇒ target=versionName、scope=installer。
    """
    gradle_version = re.search(r'^\s*versionName\s+"([^"]+)"',
                               (root / "android-native" / "app" / "build.gradle").read_text(
                                   encoding="utf-8"), re.M).group(1)
    notes = sorted((NOTE_NAME_RE.match(p.name).group(1)
                    for p in (root / "docs" / "releases").iterdir()
                    if NOTE_NAME_RE.match(p.name)), key=_vkey)
    newest_leads_server_only = False
    if notes and _vkey(notes[-1]) > _vkey(gradle_version):
        for raw in (root / "docs" / "releases" / ("v%s.md" % notes[-1])).read_text(
                encoding="utf-8").splitlines():
            if raw.startswith("## "):
                break
            hit = SCOPE_LINE_RE.match(raw)
            if hit:
                newest_leads_server_only = hit.group(1).strip().lower() == "server-only"
                break
    markers = sorted((m.group(1) for m in NOTE_MARKER_RE.finditer(
        (root / "backend" / "app" / "web" / "static" / "app.js").read_text(
            encoding="utf-8"))), key=_vkey)
    marker_leads = bool(markers) and _vkey(markers[-1]) > _vkey(gradle_version)
    if newest_leads_server_only and marker_leads:
        return "server-only", notes[-1]
    return "installer", gradle_version


def _vkey(text):
    return tuple(int(p) for p in text.split("."))


def _run(root, expect=None, live=None):
    return rm.evaluate(str(root), expect, live)


# ---------------------------------------------------------------------- 正对照
def test_derivation_script_is_in_the_repo_and_runnable():
    assert TOOL.is_file(), "tools/release_marker.py 必须入库：推导是脚本，不是文档里的一句话"
    assert callable(rm.main) and callable(rm.evaluate), "这把锁没有可调的入口，等于没锁"


def test_real_repo_marker_equals_the_derived_declared_version():
    """现网这一版：标记必须由 versionName 算出来，且真在 app.js 里恰好一行。

    原来这里写死的是 `"release-marker-v0.34.2"`（#69 拆掉的那根会腐烂的钉子）：换成测试自己
    从 `build.gradle` 现读的 `versionName`，"尺子读错文件"照样红，而版本号不用再跟着发版改。
    """
    declared = rm.declared_version(str(REPO_ROOT))
    marker = rm.marker_for(declared)
    rc, lines = _run(REPO_ROOT)
    assert rc == 0, "仓库现状必须绿，否则下面所有反例都无从对照：%s" % lines
    text = APP_JS.read_text(encoding="utf-8")
    gradle_version = _gradle_versionname()
    assert re.fullmatch(r"\d+\.\d+\.\d+", gradle_version), (
        "build.gradle 的 versionName 不是三段号：拿到 %r" % gradle_version)
    assert declared == gradle_version, (
        "工具读出来的声明侧 %r 与测试独立读到的 %r 不符：要么 versionName 被改过，"
        "要么这把尺子读错了文件" % (declared, gradle_version))
    assert marker == "release-marker-v" + gradle_version, (
        "标记不是由声明侧那一个版本推出来的：marker=%r declared=v%s" % (marker, gradle_version))
    assert rm.count_marker_lines(text, marker) == 1, (
        "%r 在 backend/app/web/static/app.js 里必须恰好一行，Select-String 数的就是行数" % marker)
    assert any("verdict=PASS" in ln for ln in lines), lines


def test_declaration_regex_has_not_drifted_from_pre_tag_gate():
    """两把闸读同一个声明侧，正则必须是同一条；分叉了就会各绿各的。"""
    assert rm.VERSION_NAME_RE.pattern == gate._VERSION_NAME_RE.pattern, (
        "tools/release_marker.py 与 tools/pre_tag_gate.py 的 versionName 正则分叉了："
        "%r vs %r" % (rm.VERSION_NAME_RE.pattern, gate._VERSION_NAME_RE.pattern))


def test_swap_precheck_still_consumes_the_derived_marker_on_both_sides():
    """推导出来的串必须仍然被那两侧计数闸消费，否则这把锁只是纸面规则（闸本身的锁在
    test_swap_package_marker_gate.py，这里只钉接线）。"""
    body = SWAP_PS1.read_text(encoding="utf-8")
    assert "-Marker required" in body, "swap 脚本不再强制 -Marker，接线断了"
    assert "stage-appjs-marker=absent" in body, "暂存侧那一格不见了"
    assert "live-appjs-marker=present" in body, "装机侧那一格（空转验收的形状）不见了"


def test_the_tool_reads_only_the_root_it_is_handed(tmp_path):
    """判据只认传进去的 repo_root —— 否则反例会在真仓库上跑，绿灯也可能是假的。"""
    _fake_repo(tmp_path, version_name="0.9.9", markers=("0.9.9",), notes=("v0.9.9",))
    rc, lines = _run(tmp_path)
    assert rc == 0, lines
    assert "marker=release-marker-v0.9.9" in lines[0], lines
    assert "release-marker-v0.34.2" not in "".join(lines), "假树里出现了真仓库的标记：它在读真文件"


# ---------------------------------------------------------------------- 反 例
def test_stale_marker_is_refused(tmp_path):
    """versionName 已 bump 到 0.35.0、app.js 里还留着上一版的标记：窗口里才发现就没意义了。"""
    _fake_repo(tmp_path, version_name="0.35.0", markers=("0.34.1", "0.34.2"), notes=("v0.34.2",))
    rc, lines = _run(tmp_path)
    assert rc == 1, "落后一枚标记却被放行，这就是那次空转验收：%s" % lines
    assert any("token=marker-newest=mismatch" in ln and "newest=v0.34.2" in ln for ln in lines), lines
    assert any("use-this-marker=release-marker-v0.35.0" in ln for ln in lines), (
        "红的时候必须直接把该用的那一串打出来，否则下一个人还是只能挑：%s" % lines)


def test_marker_ahead_of_the_declaration_is_refused(tmp_path):
    """标记指向一个还没被 versionName 声明的版本：两侧计数会对，tag 却对不上号。"""
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2", "0.35.0"), notes=("v0.34.2",))
    rc, lines = _run(tmp_path)
    assert rc == 1, lines
    assert any("token=marker-newest=mismatch" in ln and "newest=v0.35.0" in ln for ln in lines), lines


def test_missing_marker_line_is_refused(tmp_path):
    """只改后端的一版忘了补标记：合法 marker 根本不存在，必须在构建之前就说。"""
    _fake_repo(tmp_path, version_name="0.35.0", markers=(), notes=("v0.34.2",))
    rc, lines = _run(tmp_path)
    assert rc == 1, "app.js 一枚标记都没有却被放行：%s" % lines
    assert any("token=marker=absent" in ln for ln in lines), lines


def test_duplicate_marker_line_is_refused(tmp_path):
    """同一版的两行标记让"命中几行"失去意义，两侧计数也就失去意义。"""
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2", "0.34.2"), notes=("v0.34.2",))
    rc, lines = _run(tmp_path)
    assert rc == 1, "标记重复却被放行：%s" % lines
    assert any("token=marker-lines=count" in ln and "got=2" in ln for ln in lines), lines


def test_prefix_collision_between_markers_is_refused(tmp_path):
    """两边都有的假判据：-SimpleMatch 数的是子串，v0.34.1 会命中 v0.34.10 那一行。"""
    _fake_repo(tmp_path, version_name="0.34.10", markers=("0.34.1", "0.34.10"), notes=("v0.34.2",))
    rc, lines = _run(tmp_path)
    assert rc == 1, "互相命中的两枚标记被当成判据放行了：%s" % lines
    assert any("token=marker=prefix-collision" in ln for ln in lines), lines
    assert not any("verdict=PASS" in ln for ln in lines), lines


def test_release_note_running_ahead_of_the_declaration_is_refused(tmp_path):
    """docs/releases 已经写了指向更高版本的说明，而声明侧没跟上：三件套错位。"""
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2",), notes=("v0.34.2", "v0.35.0"))
    rc, lines = _run(tmp_path)
    assert rc == 1, lines
    assert any("token=release-note=ahead" in ln for ln in lines), lines


def test_expect_flag_refuses_a_target_the_declaration_has_not_reached(tmp_path):
    """构建之前按目标版本核：versionName 没 bump 就该红，而不是顺着操作者的嘴。

    这份 fixture 补了 `docs/releases/v0.35.0.md`——**不带 scope 行**，也就是仓库缺省的那一形态
    （有安装包）。补它是因为 #65 之后"这一版是什么形态"改由说明决定，缺说明会先撞上
    `no-release-note-for-target`（另有测试钉住），本条要钉的那条红仍然是同一条、同一个 rc：
    可安装的一版而 versionName 没追平 ⇒ versionname-not-bumped。断言一字未改。

    后两个子判据挪到 `caughtup` 那棵同形状的树上跑：那棵树里没有领先于目标版本的说明，否则
    `release-note=ahead`（另一条测试钉着的既有判据）会先红——子判据要量的是"expect 这一路放行"，
    不是"说明领先"，两件事不该混在同一棵树里。
    """
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2",),
               notes=("v0.34.2", "v0.35.0"))
    rc, lines = _run(tmp_path, expect="v0.35.0")
    assert rc == 1, "操作者说要发 0.35.0、声明侧还是 0.34.2，却被放行：%s" % lines
    assert any("token=versionname-not-bumped" in ln for ln in lines), lines
    caughtup = _fake_repo(tmp_path / "caughtup", version_name="0.34.2", markers=("0.34.2",),
                          notes=("v0.34.2",))
    ok_rc, ok_lines = _run(caughtup, expect="v0.34.2")
    assert ok_rc == 0, ok_lines
    bad_rc, bad_lines = _run(caughtup, expect="0.34.2")
    assert bad_rc == 1 and any("expect-must-be-full-tag" in ln for ln in bad_lines), bad_lines


# ------------------------------------------------------------------ #65：只换服务端的一版
def test_server_only_release_is_cut_without_touching_versionname(tmp_path):
    """仓库里真实存在的那一形态（v0.34.1）：versionName 一动不动、说明带 scope、标记还没进
    app.js —— 这一版必须**放行**并给出该用的那一串。#43 的判据在这里退成了 v0.35.0 那次实测的
    `versionname-not-bumped`，照着它做就等于把一个装不上的安卓版本声明出去。
    """
    _fake_repo(tmp_path, version_name="0.34.0", markers=(),
               notes=("v0.34.0", "v0.34.1"), server_only=("v0.34.1",))
    rc, lines = _run(tmp_path, expect="v0.34.1")
    assert rc == 0, "只换服务端的一版被 versionName 拦住了：%s" % lines
    assert any("scope=server-only" in ln for ln in lines), lines
    assert any("marker=release-marker-v0.34.1" in ln for ln in lines), lines
    assert lines[-1].startswith("RELEASE-MARKER verdict=PASS") \
        and "release-marker-v0.34.1" in lines[-1], lines
    assert not any("versionname-not-bumped" in ln for ln in lines), lines
    # 声明侧一个字节没动：这把闸不许逼着人去 bump 它
    assert rm.declared_version(str(tmp_path)) == "0.34.0", lines


def test_server_only_marker_already_in_app_js_is_refused(tmp_path):
    """说明写着只换服务端，而 `release-marker-v0.34.1` 已经在 app.js 里了 ⇒ 拒绝。
    已经在了就是说线上那份包里也有这一串，两侧计数会"两边都有"——那正是空转验收的形状，
    也是 #43 手工补行留下的坑：补早了比不补更危险。

    #69 之后这棵假树的 `live_markers` 缺省跟着 `markers`，也就是"仓库和线上是同一版"，
    被拒的理由落在**装机侧那一份**上（`already-present` 这个 token 一字未改，断言也未改）。
    """
    _fake_repo(tmp_path, version_name="0.34.0", markers=("0.34.0", "0.34.1"),
               notes=("v0.34.0", "v0.34.1"), server_only=("v0.34.1",))
    rc, lines = _run(tmp_path, expect="v0.34.1")
    assert rc == 1, "标记已经在线上却被放行：%s" % lines
    assert any("token=server-only-marker=already-present" in ln and "release-marker-v0.34.1" in ln
               for ln in lines), lines
    assert not any("verdict=PASS" in ln for ln in lines), lines


def test_the_scope_line_is_not_a_global_bypass(tmp_path):
    """反面对照：说明**没带** scope（=有安装包那一版）而 versionName 没追平 ⇒ 照旧红。
    少了这一条，"权威换成说明"就退成了"谁都能随口报一个版本号"。
    """
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2",),
               notes=("v0.34.2", "v0.35.0"))
    rc, lines = _run(tmp_path, expect="v0.35.0")
    assert rc == 1, "可安装的一版没 bump versionName 却被放行：%s" % lines
    assert any("token=versionname-not-bumped" in ln for ln in lines), lines
    assert any("scope=installer" in ln for ln in _run(tmp_path, expect="v0.34.2")[1]), \
        "同一棵树按声明侧核必须是 installer 形态"


# ------------------------------------------------ #68：scope 的读法只有一把尺子
# 事故形状（#68）：这里原先只比 `SCOPE_RE.pattern` 两个字符串。往 `gen_changelog._VALID_SCOPES`
# 里加第三个值（`hotfix`），正则一字未动、四把锁全绿，而换包闸照样对同一份说明退 2
# `release-note-scope-unknown`——发版说明页说"有安装包"，换包闸说"判据读不出"。字符串相等
# 证明不了两边读的是同一套值，更证明不了同一份说明会得到同一个结论。
#
# 现在分两层钉：
# 1. 结构层：`gen_changelog` 直接 import `release_marker` 的常量与 `scope_from_text`，
#    合法值只有**一个对象**（`test_the_scope_value_set_has_exactly_one_source`）；
# 2. 行为层：同一份语料写进假 `docs/releases/`，分别喂给两个**生产入口**
#    （`gen_changelog.load_releases` / `release_marker.note_scope`），逐样本断言两边结论
#    一致、且等于下面这张**冻结**的期望表。期望表是人工观测值，不从任何一侧现算——
#    两边一起漂移时，"互相一致"本身没有意义。
SERVER_ONLY = "server-only"
INSTALLER = "installer"
UNKNOWN = "unknown-scope"
NOTE_VERSION = "0.35.0"

# 27 条完整说明：3 条正/负对照 + 21 条形状对抗（reviewer 实测过的 CRLF、BOM、大小写、
# 空格、全角、位置、两行……都在里面）+ 3 条认不出的值（两边必须同时判失败）。
# 写盘用 bytes，不用 write_text：Windows 会把 "\n" 换成 "\r\n"，CRLF 那一格就被洗成
# 普通换行，再也不是它声称的形状了。
_SCOPE_CORPUS = [
    # ---- 正/负对照：两种合法形态 + 历史各版的缺省形状（没有 scope 行） ----
    ("canonical-server-only",
     "# v0.35.0\n\n<!-- scope: server-only -->\n\n## 修复\n\n- x\n", SERVER_ONLY),
    ("canonical-installer", "# v0.35.0\n\n<!-- scope: installer -->\n", INSTALLER),
    ("no-scope-line", "# v0.35.0\n\n## 修复\n\n- x\n", INSTALLER),
    # ---- reviewer 的对抗形状 ----
    ("crlf", "# v0.35.0\r\n\r\n<!-- scope: server-only -->\r\n", SERVER_ONLY),
    ("cr-only", "# v0.35.0\r\r<!-- scope: server-only -->\r", SERVER_ONLY),
    ("bom-first-line", "\ufeff# v0.35.0\n\n<!-- scope: server-only -->\n", SERVER_ONLY),
    ("bom-on-scope-line", "# v0.35.0\n\n\ufeff<!-- scope: server-only -->\n", INSTALLER),
    ("uppercase-key", "# v0.35.0\n\n<!-- SCOPE: server-only -->\n", INSTALLER),
    ("mixed-case-key", "# v0.35.0\n\n<!-- Scope: server-only -->\n", INSTALLER),
    ("space-before-colon", "# v0.35.0\n\n<!-- scope : server-only -->\n", INSTALLER),
    ("space-inside-value", "# v0.35.0\n\n<!-- scope: server only -->\n", INSTALLER),
    ("missing-space-before-arrow", "# v0.35.0\n\n<!-- scope: server-only-->\n", SERVER_ONLY),
    ("trailing-tab", "# v0.35.0\n\n<!-- scope: server-only -->\t\n", SERVER_ONLY),
    ("fullwidth-spaces",
     "# v0.35.0\n\n<!--\u3000scope:\u3000server-only\u3000-->\u3000\n", SERVER_ONLY),
    ("nbsp", "# v0.35.0\n\n<!--\u00a0scope:\u00a0server-only\u00a0-->\n", SERVER_ONLY),
    ("scope-before-title",
     "<!-- scope: server-only -->\n\n# v0.35.0\n\n## 修复\n", SERVER_ONLY),
    ("two-scope-lines-server-first",
     "# v0.35.0\n\n<!-- scope: server-only -->\n<!-- scope: installer -->\n", SERVER_ONLY),
    ("two-scope-lines-installer-first",
     "# v0.35.0\n\n<!-- scope: installer -->\n<!-- scope: server-only -->\n", INSTALLER),
    ("scope-after-first-section",
     "# v0.35.0\n\n## 修复\n\n<!-- scope: server-only -->\n", INSTALLER),
    ("uppercase-value", "# v0.35.0\n\n<!-- scope: SERVER-ONLY -->\n", SERVER_ONLY),
    ("no-spaces-at-all", "# v0.35.0\n\n<!--scope:server-only-->\n", SERVER_ONLY),
    ("indented", "# v0.35.0\n\n   <!-- scope: server-only -->\n", SERVER_ONLY),
    ("trailing-comment",
     "# v0.35.0\n\n<!-- scope: server-only --> <!-- x -->\n", INSTALLER),
    ("empty-value", "# v0.35.0\n\n<!-- scope: -->\n", INSTALLER),
    # ---- 认不出的值：两边必须同时判**失败**（#68 的旁路值 hotfix 就在第一条） ----
    ("unknown-hotfix", "# v0.35.0\n\n<!-- scope: hotfix -->\n", UNKNOWN),
    ("unknown-camelcase", "# v0.35.0\n\n<!-- scope: serverOnly -->\n", UNKNOWN),
    ("unknown-backend-only", "# v0.35.0\n\n<!-- scope: backend-only -->\n", UNKNOWN),
]


def _write_scope_note(root: Path, body: str) -> Path:
    folder = root / "docs" / "releases"
    folder.mkdir(parents=True, exist_ok=True)
    note = folder / ("v%s.md" % NOTE_VERSION)
    note.write_bytes(body.encode("utf-8"))
    return note


def _gen_changelog_scope_verdict(root: Path) -> str:
    """走生成器的生产入口（读同一棵假树），返回它读出的形态；认不出的值折成 UNKNOWN。"""
    try:
        releases = cl.load_releases(str(root / "docs" / "releases"))
    except ValueError as exc:
        assert "scope" in str(exc), "生成器抛的不是 scope 判读的错：%r" % (exc,)
        return UNKNOWN
    assert len(releases) == 1, releases
    return SERVER_ONLY if releases[0]["server_only"] else INSTALLER


def _release_marker_scope_verdict(root: Path) -> str:
    """走换包闸的生产入口（同一个 note_scope），返回它读出的形态。"""
    server_only, reason = rm.note_scope(str(root), NOTE_VERSION)
    if reason is None:
        return SERVER_ONLY if server_only else INSTALLER
    assert reason.startswith("release-note-scope-unknown:"), reason
    return UNKNOWN


@pytest.mark.parametrize("name,body,expected", _SCOPE_CORPUS,
                         ids=[sample[0] for sample in _SCOPE_CORPUS])
def test_both_scope_readers_agree_on_the_frozen_corpus(tmp_path, name, body, expected):
    _write_scope_note(tmp_path, body)
    from_gen = _gen_changelog_scope_verdict(tmp_path)
    from_marker = _release_marker_scope_verdict(tmp_path)
    assert from_gen == from_marker, (
        "%s：同一份说明两个读者读出两种结论（gen_changelog=%s / release_marker=%s）"
        % (name, from_gen, from_marker))
    assert from_marker == expected, (
        "%s：读者给出 %s，冻结期望是 %s——语义变了要么是有意的（改期望并说明），"
        "要么是 scope 的读法漂了" % (name, from_marker, expected))


def test_the_frozen_corpus_still_covers_every_verdict():
    """语料本身不许缩水：删样本能让上面那条恒绿，等于把锁拆了。"""
    outcomes = [expected for _, _, expected in _SCOPE_CORPUS]
    assert len(_SCOPE_CORPUS) >= 17, "对抗语料少于 17 条：reviewer 实测的形状被删了"
    assert SERVER_ONLY in outcomes and INSTALLER in outcomes, "两种合法形态必须都有对照"
    assert outcomes.count(UNKNOWN) >= 3, "认不出的值至少要有三个样本，失败方向才不是单点"
    names = [name for name, _, _ in _SCOPE_CORPUS]
    assert len(names) == len(set(names)), "样本名重复：参数化 id 会撞在一起"


def test_the_scope_value_set_has_exactly_one_source():
    """值集合只有一份：`gen_changelog` 必须 import `release_marker` 的元组**本身**。

    这里断的是对象同一，不是字符串/元素相等：`_VALID_SCOPES = VALID_SCOPES + ("hotfix",)`
    这种"在本模块里加一个值"正是 #68 的旁路，它会让集合在这里当场不等。往共享元组里加值
    则两边一起变——结构上合法，但下面那条 `unknown-hotfix` 的冻结样本会红，所以也不是
    一条能悄悄走通的旁路。
    """
    shared = importlib.import_module("release_marker")   # gen_changelog 刚刚 import 的那一份
    assert cl._VALID_SCOPES is shared.VALID_SCOPES, (
        "gen_changelog._VALID_SCOPES 不是 release_marker.VALID_SCOPES 本身："
        "%r vs %r" % (cl._VALID_SCOPES, shared.VALID_SCOPES))
    assert cl._SCOPE_RE is shared.SCOPE_RE, "gen_changelog 又抄了一条 scope 正则"
    assert cl._SECTION_RE is shared.SECTION_RE, "引言区的边界两边不是同一条正则"
    assert cl._VALID_SCOPES == rm.VALID_SCOPES, (
        "两个模块认的 scope 值分叉了：%r vs %r" % (cl._VALID_SCOPES, rm.VALID_SCOPES))
    assert cl.SCOPE_SERVER_ONLY == rm.SCOPE_SERVER_ONLY == "server-only"
    assert cl.SCOPE_INSTALLABLE == rm.SCOPE_INSTALLABLE == "installer"


def test_unreadable_sources_fail_closed_not_green(tmp_path):
    """判据读不到＝不许（与 pre_tag_gate 的 fail-closed 同一档），不是"当它过了"。

    #65 之后"这一版是什么形态"的权威换成了那份发版说明，所以说明这一侧的三种拿不到——没有这份
    文件、文件读不出文本、scope 值不认识——必须一并落进同一档：缺文件**不许**回落到"那就去
    bump versionName"（那是把同一个错误换个写法），值不认识**不许**回落到"那就当有安装包"。
    """
    empty = tmp_path / "nothing"
    empty.mkdir()
    rc, lines = _run(empty)
    assert rc == 2, "读不到 build.gradle 却给了绿灯：%s" % lines
    assert any("verdict=UNKNOWN" in ln for ln in lines), lines

    partial = _fake_repo(tmp_path / "noappjs", version_name="0.34.2", notes=("v0.34.2",))
    (partial / "backend" / "app" / "web" / "static" / "app.js").unlink()
    rc2, lines2 = _run(partial)
    assert rc2 == 2 and any("appjs-missing" in ln for ln in lines2), lines2

    # 目标版本的说明还不存在：拦，但拦的原因是"没有说明"，不是"没 bump versionName"
    missing = _fake_repo(tmp_path / "nonote", version_name="0.34.2", notes=("v0.34.2",))
    rc3, lines3 = _run(missing, expect="v0.35.0")
    assert rc3 == 2 and any("token=no-release-note-for-target" in ln for ln in lines3), lines3
    assert not any("versionname-not-bumped" in ln for ln in lines3), (
        "缺说明却把人推去 bump versionName：那正是 #65 要修的错：%s" % lines3)
    assert not any("verdict=PASS" in ln for ln in lines3), lines3

    # scope 值不认识：报错，不当"没标"处理
    bogus = _fake_repo(tmp_path / "badscope", version_name="0.34.2", notes=("v0.34.2", "v0.35.0"))
    (bogus / "docs" / "releases" / "v0.35.0.md").write_text(
        "# v0.35.0\n\n<!-- scope: only-server -->\n\n## 修复了什么\n", encoding="utf-8")
    rc4, lines4 = _run(bogus, expect="v0.35.0")
    assert rc4 == 2 and any("release-note-scope-unknown" in ln for ln in lines4), lines4

    # 说明读不出文本（不是 utf-8）
    binary = _fake_repo(tmp_path / "binarynote", version_name="0.34.2", notes=("v0.34.2", "v0.35.0"))
    (binary / "docs" / "releases" / "v0.35.0.md").write_bytes(b"# v0.35.0 \xff\xfe\x00\n")
    rc5, lines5 = _run(binary, expect="v0.35.0")
    assert rc5 == 2 and any("release-note-unreadable" in ln for ln in lines5), lines5
    for lines_x in (lines4, lines5):
        assert not any("verdict=PASS" in ln for ln in lines_x), lines_x


# ---------------------------------------------------- #69：装机侧那一份事实才是判据
def test_server_only_marker_added_to_app_js_but_not_live_is_green(tmp_path):
    """这一条就是 #69 的全部要点：发版提交把 `release-marker-v0.35.0` 补进 app.js **之后**
    那一棵树必须是绿的。旧判据（"app.js 里不许有"）在这个形状下恒红——只换服务端的一版在
    "标记必须存在"的那一刻根本不存在绿色状态，操作者在换包窗口里没有可抄的绿灯。
    """
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2", "0.35.0"),
               notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",), live_markers=("0.34.2",))
    rc, lines = _run(tmp_path, expect="v0.35.0")
    assert rc == 0, "标记已补进 app.js、线上那份里没有，却被拦住了：%s" % lines
    assert any("token=server-only-moment=verified-ready-to-build" in ln
               and "release-marker-v0.35.0" in ln and "live-lines=0" in ln
               and "repo-lines=1" in ln for ln in lines), \
        "绿了但没说清「这一行已经在仓库里、可以构建」：%s" % lines
    assert lines[-1].startswith("RELEASE-MARKER verdict=PASS") \
        and "release-marker-v0.35.0" in lines[-1], lines
    assert not any("already-present" in ln for ln in lines), lines
    assert not any("versionname-not-bumped" in ln for ln in lines), lines


def test_server_only_marker_absent_on_both_sides_is_green_and_says_add_this_line(tmp_path):
    """同一条判据的前一个时刻：仓库里没有、线上也没有 ⇒ 一样绿，但 token 必须说"还差一行"，
    并把该抄的那一串原样给出来（两个时刻都绿，靠 token 分辨，不靠让其中一个红）。
    """
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2",),
               notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",), live_markers=("0.34.2",))
    rc, lines = _run(tmp_path, expect="v0.35.0")
    assert rc == 0, lines
    assert any("token=server-only-moment=add-this-line" in ln and "repo-lines=0" in ln
               and "live-lines=0" in ln and "release-marker-v0.35.0" in ln for ln in lines), lines
    assert not any("already-present" in ln for ln in lines), lines
    assert sum(1 for ln in lines if "token=server-only-moment=" in ln) == 1, \
        "两个时刻的 token 同时报了，等于没说：%s" % lines


def test_marker_already_in_the_live_package_is_refused_even_when_repo_is_clean(tmp_path):
    """空转验收的形状，而且旧判据看不见它：仓库的 app.js 里**没有**这一枚串（干净），
    线上那份包里却已经躺着 `release-marker-v0.35.0` ⇒ 必须红。删掉"线上已经有了就拒绝"这一格
    就是把 v0.34.1 拿 /changelog 撞上的那次盖章重新放成绿灯（变异 (i) 量的是这一条 + 上面
    那条 already-present）。
    """
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2",),
               notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",),
               live_markers=("0.34.2", "0.35.0"))
    rc, lines = _run(tmp_path, expect="v0.35.0")
    assert rc == 1, "标记已经在线上那份包里却被放行：%s" % lines
    assert any("token=server-only-marker=already-present" in ln and "release-marker-v0.35.0" in ln
               and "live-lines=1" in ln and "repo-lines=0" in ln for ln in lines), lines
    assert not any("verdict=PASS" in ln for ln in lines), lines
    assert not any("server-only-moment" in ln for ln in lines), \
        "已经判红了还报「可以构建/该补行」的时刻：%s" % lines


def test_server_only_newest_marker_above_target_is_refused(tmp_path):
    """:266-271 那条 `newest-above` 今天零测试（reviewer 把它删掉，17 条全绿 = 这条判据没牙）。
    树：目标 v0.34.1（只换服务端），仓库 app.js 里却躺着 v0.34.2 那枚更新的标记，装机侧干净。
    """
    _fake_repo(tmp_path, version_name="0.34.0", markers=("0.34.0", "0.34.2"),
               notes=("v0.34.0", "v0.34.1"), server_only=("v0.34.1",), live_markers=("0.34.0",))
    rc, lines = _run(tmp_path, expect="v0.34.1")
    assert rc == 1, "app.js 里有比目标更新的标记却被放行：%s" % lines
    assert any("token=server-only-marker=newest-above" in ln and "newest=v0.34.2" in ln
               and "target=v0.34.1" in ln for ln in lines), lines
    assert not any("verdict=PASS" in ln for ln in lines), lines
    assert not any("server-only-moment" in ln for ln in lines), lines


def test_unreadable_live_package_fails_closed_instead_of_defaulting_to_absent(tmp_path):
    """新克隆 / CI / `dist\\` 被清掉：装机侧读不到 ⇒ 退 2，**绝不**当成"里面没有"。
    默认成 absent 的话，这台机器之外的每一份克隆都会把两侧都有的事故静默放成绿灯——判据自己
    造出它要防的那次事故（变异 (ii) 量的是这一条）。
    """
    root = _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2", "0.35.0"),
                      notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",),
                      live_markers=("0.34.2",))
    ok_rc, ok_lines = _run(root, expect="v0.35.0")
    assert ok_rc == 0, ok_lines                       # 同一个形状，假日机包读得到就是绿的
    live_js = root / LIVE_APP_JS
    live_js.unlink()
    rc, lines = _run(root, expect="v0.35.0")
    assert rc == 2, "装机侧读不到却给了绿/普通红灯：%s" % lines
    assert any("verdict=UNKNOWN" in ln and "token=live-appjs=unreadable" in ln
               and "live-appjs-missing" in ln for ln in lines), lines
    assert not any("verdict=PASS" in ln for ln in lines), lines
    assert not any("live-lines=0" in ln for ln in lines), \
        "读不到被当成「里面没有」了：%s" % lines
    live_js.parent.mkdir(parents=True, exist_ok=True)
    live_js.write_bytes(b"\xff\xfe\x00\x01 not utf-8 at all\n")   # 路径在、读不出文本
    rc2, lines2 = _run(root, expect="v0.35.0")
    assert rc2 == 2 and any("live-appjs-unreadable" in ln for ln in lines2), lines2
    assert not any("verdict=PASS" in ln for ln in lines2), lines2


def test_live_appjs_override_names_a_path_and_never_skips_the_check(tmp_path):
    """`--live-appjs` 是**精确路径覆盖**（测试/别的机器指认另一份包用），不是"跳过这一格"的
    布尔开关：指到不存在的文件照样退 2，指到手工造的假日机包就按那份的内容判（这里让它**带**
    本版那枚串 ⇒ 仍然红）。走 `main()` 是为了把命令行这一层也钉住。
    """
    root = _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2", "0.35.0"),
                      notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",),
                      live_markers=("0.34.2",))
    (root / LIVE_APP_JS).unlink()
    base = ["--repo-root", str(root), "--expect", "v0.35.0"]
    assert rm.main(base + ["--live-appjs", str(root / "nowhere" / "app.js")]) == 2, \
        "覆盖成一个不存在的路径却过了：这一格被当成开关用了"
    installed = tmp_path / "hand-built-package" / "live_app.js"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text("/* 手工造的假日机包：还没有本版那枚串 */\n", encoding="utf-8")
    assert rm.main(base + ["--live-appjs", str(installed)]) == 0, \
        "覆盖路径读得到、里面干净，却没放行"
    installed.write_text("/* 手工造的假日机包 */\n/* release-marker-v0.35.0 */\n", encoding="utf-8")
    assert rm.main(base + ["--live-appjs", str(installed)]) == 1, \
        "覆盖路径里已经有这一枚串还被放行：装机侧这一格被绕过了"


# --------------------------------- #71：说明领先这一条也要吃同一把 scope 尺子
def test_ahead_server_only_note_leaves_the_bare_run_green(tmp_path):
    """发版准备提交 `docs/releases/v0.35.0.md`（带 `scope: server-only`）的那一刻，**裸跑**
    （不带 `--expect`，target=versionName=0.34.2）必须是绿的。

    这一棵树就是 #69 那两支测试**看不见**的形状：它们都带 `--expect v0.35.0`，于是 target 直接
    落在领先的那一版上，`:384-388` 那条"说明比目标版本新"根本不会被触发。裸跑才是常驻锁
    (`test_real_repo_marker_equals_the_derived_declared_version`) 与换包窗口里 `python
    tools/release_marker.py` 抄一行用的那一条命令 —— 只换服务端的一版**本来就不 bump**
    versionName（实测 `v0.34.1` 的 build.gradle 停在 0.34.0/60，而 `docs/releases/v0.34.1.md`
    存在），所以"说明跑在声明侧前面"正是这一形态的预期形状；照 `:384-388` 的老写法它会红，
    并且从有人提交这一版说明的那一刻起**常年红**（同一版说明一直领先，直到换包把版本号追平——
    而这一版根本不追）。它印的"先核 versionName"又恰是 #65/#67 禁止的动作。
    """
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2",),
               notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",))
    rc, lines = _run(tmp_path)                      # 裸跑：判据自己从 versionName 推 target
    assert rc == 0, "只换服务端的一版把说明提交上去之后裸跑就红了：%s" % lines
    assert any("scope=installer" in ln and "target=v0.34.2" in ln for ln in lines), \
        "target 仍然是声明侧那一版（本条只放行说明领先这一格，不放宽形态判读）：%s" % lines
    assert not any("release-note=ahead" in ln for ln in lines), lines
    assert lines[-1].startswith("RELEASE-MARKER verdict=PASS") \
        and "release-marker-v0.34.2" in lines[-1], lines


def test_only_a_server_only_declaration_suppresses_the_ahead_token(tmp_path):
    """反面对照（同形状、只差那一行 scope）：这一格不是"说明领先就不管了"的全局旁路。
    说明没带 `scope: server-only` ⇒ 有安装包那一版而声明侧没跟上，那才是文档真的跑在前面，
    照旧红、照旧同一个 token、同一个 rc。少了这一条，:294 那条 `the_scope_line_is_not_a_global_bypass`
    就只覆盖了 versionName 一格。
    """
    green = _fake_repo(tmp_path / "server-only", version_name="0.34.2", markers=("0.34.2",),
                       notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",))
    red = _fake_repo(tmp_path / "installer", version_name="0.34.2", markers=("0.34.2",),
                     notes=("v0.34.2", "v0.35.0"))          # 同一棵树，少那一行 scope
    assert _run(green)[0] == 0, _run(green)[1]
    rc, lines = _run(red)
    assert rc == 1, "说明没声明只换服务端、却领先声明侧，被放行了：%s" % lines
    assert any("token=release-note=ahead" in ln and "v0.35.0" in ln and "v0.34.2" in ln
               for ln in lines), lines
    assert not any("verdict=PASS" in ln for ln in lines), lines


def test_ahead_check_reads_the_newest_note_only(tmp_path):
    """被 scope 豁免的必须是**最高**那一版说明：判据问的是"领先声明侧的那些说明是不是都只换
    服务端"，而领先的那一版恰好就是最高的那一版。中间的说明带 scope、最高的那版不带 ⇒ 照旧红
    （否则"某一版写过 server-only"会永久把这一格关掉，又一个全局旁路）。
    """
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2",),
               notes=("v0.34.2", "v0.35.0", "v0.36.0"), server_only=("v0.35.0",))
    rc, lines = _run(tmp_path)
    assert rc == 1, "最高的说明 v0.36.0 没声明只换服务端却被放行：%s" % lines
    assert any("token=release-note=ahead" in ln and "docs/releases/v0.36.0.md" in ln
               for ln in lines), lines
    assert not any("verdict=PASS" in ln for ln in lines), lines


def test_unreadable_or_bogus_scope_on_the_newest_note_still_fires(tmp_path):
    """豁免的前提是**读得出**"只换服务端"这个结论：scope 值不认识、或说明根本读不出文本时，
    照旧红（沿用 #69 那条纪律——读不到就不当成"没问题"），免得一个坏 scope 值顺手把这一格
    变成全局旁路。两棵树都只改最高那一版说明。
    """
    for name, body in (("unknown-scope", "# v0.35.0\n\n<!-- scope: only-server -->\n"),
                       ("binary-note", None)):
        root = _fake_repo(tmp_path / name, version_name="0.34.2", markers=("0.34.2",),
                          notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",))
        note = root / "docs" / "releases" / "v0.35.0.md"
        if body is None:
            note.write_bytes(b"# v0.35.0 \xff\xfe\x00\n")
        else:
            note.write_text(body, encoding="utf-8")
        rc, lines = _run(root)
        assert rc == 1, "%s：读不出 server-only 这个结论却把说明领先放行了：%s" % (name, lines)
        assert any("token=release-note=ahead" in ln for ln in lines), lines
        assert not any("verdict=PASS" in ln for ln in lines), lines


# ---------------------- 裸跑的「标记领先 + 只换服务端」形状（v0.34.3 落地时的那一棵树）
def test_bare_run_marker_ahead_with_server_only_note_is_green_and_names_it(tmp_path):
    """这一版该用的是**领先的那一枚**：说明领先 versionName、它自己声明 server-only、app.js 里
    最新标记也领先 —— 裸跑（不带 --expect）必须绿，并把那一串原样给出来。
    """
    root = _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2", "0.35.0"),
                      notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",),
                      live_markers=("0.34.2",))
    rc, lines = _run(root)
    assert rc == 0, "标记领先 + 只换服务端是这一形态的正确形状，却被判红：%s" % lines
    assert "scope=server-only" in lines[0] and "target=v0.35.0" in lines[0] \
        and "marker=release-marker-v0.35.0" in lines[0], lines
    assert any("token=bare-shape=server-only-marker-ahead" in ln and "note=v0.35.0" in ln
               and "declared=v0.34.2" in ln for ln in lines), \
        "换了 target 却没在输出里说清为什么换：%s" % lines
    assert lines[-1].startswith("RELEASE-MARKER verdict=PASS") \
        and "release-marker-v0.35.0" in lines[-1], lines
    assert not any("versionname-not-bumped" in ln for ln in lines), lines
    assert rm.declared_version(str(root)) == "0.34.2", "这一支不许动 versionName"
    # 这一支只核仓库里的文本：装机侧的计数与时刻 token 都不该出现在裸跑的输出里
    assert not any("live-lines" in ln for ln in lines), lines
    assert not any("server-only-moment" in ln for ln in lines), lines


def test_bare_run_marker_ahead_beyond_the_note_is_refused(tmp_path):
    """等号右边取自**说明**而不是取自标记：标记领先到另一枚版本号（说明只写到 v0.35.0，app.js
    里却躺着 v0.36.0）⇒ 红，并给出说明那一版该用的串。放行它就是把换包标记交给手抖的人。
    """
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2", "0.35.0", "0.36.0"),
               notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",), live_markers=("0.34.2",))
    rc, lines = _run(tmp_path)
    assert rc == 1, "领先却对不上说明的标记被当成这一版放行了：%s" % lines
    assert any("token=marker-newest=mismatch" in ln and "newest=v0.36.0" in ln
               and "target=v0.35.0" in ln for ln in lines), lines
    assert any("use-this-marker=release-marker-v0.35.0" in ln for ln in lines), lines
    assert not any("verdict=PASS" in ln for ln in lines), lines


def test_marker_ahead_without_any_leading_note_is_refused(tmp_path):
    """缺件之一「说明不存在」：app.js 里领先的那枚标记没有任何发版说明撑着 ⇒ 裸跑不许跟着标记走，
    照旧按声明侧判，红。
    """
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2", "0.35.0"), notes=("v0.34.2",))
    rc, lines = _run(tmp_path)
    assert rc == 1, lines
    assert "scope=installer" in lines[0] and "target=v0.34.2" in lines[0], lines
    assert any("token=marker-newest=mismatch" in ln and "newest=v0.35.0" in ln
               and "target=v0.34.2" in ln for ln in lines), lines
    assert not any("bare-shape=server-only-marker-ahead" in ln for ln in lines), lines
    assert not any("verdict=PASS" in ln for ln in lines), lines


def test_marker_ahead_with_leading_note_lacking_scope_line_is_refused(tmp_path):
    """缺件之二「说明领先但**没有** scope 行」：那一版是有安装包的形态，标记不许越过还没被声明
    的 versionName ⇒ 同一个 rc=1，且两条判据（标记错位 + 说明跑在前面）同时报。
    """
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2", "0.35.0"),
               notes=("v0.34.2", "v0.35.0"))
    rc, lines = _run(tmp_path)
    assert rc == 1, lines
    assert "scope=installer" in lines[0] and "target=v0.34.2" in lines[0], lines
    assert any("token=marker-newest=mismatch" in ln and "newest=v0.35.0" in ln
               and "target=v0.34.2" in ln for ln in lines), lines
    assert any("token=release-note=ahead" in ln and "v0.35.0" in ln for ln in lines), lines
    assert not any("bare-shape=server-only-marker-ahead" in ln for ln in lines), lines


def test_marker_ahead_with_installer_scope_note_is_refused(tmp_path):
    """缺件之三「领先、但 scope 写的是安装包形态」：那一版必须先把 versionName 追平，标记领先
    就是错位 ⇒ 照旧红，target 不跟着标记走。
    """
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2", "0.35.0"),
               notes=("v0.34.2", "v0.35.0"), installer_only=("v0.35.0",))
    rc, lines = _run(tmp_path)
    assert rc == 1, lines
    assert "scope=installer" in lines[0] and "target=v0.34.2" in lines[0], lines
    assert any("token=marker-newest=mismatch" in ln and "newest=v0.35.0" in ln
               and "target=v0.34.2" in ln for ln in lines), lines
    assert not any("bare-shape=server-only-marker-ahead" in ln for ln in lines), lines
    assert not any("verdict=PASS" in ln for ln in lines), lines


def test_marker_ahead_with_bogus_or_unreadable_leading_note_is_refused(tmp_path):
    """领先那份说明的 scope **读不出结论**（值不认识 / 不是 utf-8）时，不许当成 server-only 换
    target：读不出就不豁免（沿用装机侧那一条的纪律），红在同一个 token 上。
    """
    for name, body in (("unknown-scope", "# v0.35.0\n\n<!-- scope: only-server -->\n"),
                       ("binary-note", None)):
        root = _fake_repo(tmp_path / name, version_name="0.34.2", markers=("0.34.2", "0.35.0"),
                          notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",))
        note = root / "docs" / "releases" / "v0.35.0.md"
        if body is None:
            note.write_bytes(b"# v0.35.0 \xff\xfe\x00\n")
        else:
            note.write_text(body, encoding="utf-8")
        rc, lines = _run(root)
        assert rc == 1, "%s：%s" % (name, lines)
        assert "scope=installer" in lines[0] and "target=v0.34.2" in lines[0], (name, lines)
        assert any("token=marker-newest=mismatch" in ln and "newest=v0.35.0" in ln
                   for ln in lines), (name, lines)
        assert not any("bare-shape=server-only-marker-ahead" in ln for ln in lines), (name, lines)
        assert not any("verdict=PASS" in ln for ln in lines), (name, lines)


def test_ahead_server_only_note_does_not_exempt_the_installer_checks(tmp_path):
    """反过来的那一格：标记**没领先**（还停在 versionName 上）而领先的那份说明写着 server-only ⇒
    按原 installer 支照常判，不许因为"有 scope 行"就一律放行。同一形状里换掉标记的内容，原有的
    判据各自还咬得动。
    """
    on_declared = _fake_repo(tmp_path / "on-declared", version_name="0.34.2", markers=("0.34.2",),
                             notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",))
    rc, lines = _run(on_declared)
    assert rc == 0, "标记还没补行的那一刻（=versionName）判据不该被这条推导挪走：%s" % lines
    assert "scope=installer" in lines[0] and "target=v0.34.2" in lines[0], lines
    assert not any("bare-shape=server-only-marker-ahead" in ln for ln in lines), lines

    stale = _fake_repo(tmp_path / "stale", version_name="0.34.2", markers=("0.34.1",),
                       notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",))
    rc2, lines2 = _run(stale)
    assert rc2 == 1, "标记落后于声明侧却被放行：%s" % lines2
    assert any("token=marker-newest=mismatch" in ln and "newest=v0.34.1" in ln
               and "target=v0.34.2" in ln for ln in lines2), lines2

    dup = _fake_repo(tmp_path / "dup", version_name="0.34.2", markers=("0.34.2", "0.34.2"),
                     notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",))
    rc3, lines3 = _run(dup)
    assert rc3 == 1, "标记重复却被放行：%s" % lines3
    assert any("token=marker-lines=count" in ln and "got=2" in ln for ln in lines3), lines3


def test_bare_marker_ahead_shape_never_reads_the_live_package(tmp_path):
    """射程自证：这一支只核仓库里的文本，**没有**读装机侧那份包。
    假日机包删掉 ⇒ 裸跑仍然绿（否则 CI / 新克隆 / 清过盘这些没有 dist\\ 的机器上，常驻锁会当场
    退 2，而这一格恰恰是发版顺序里"本地全量复跑"要跑的那一格）。
    同一棵树里把本版那枚串放进假日机包 ⇒ 裸跑看不见它（红不了也绿不了，射程之外），而带
    `--expect` 的那一路照样拒绝——裸跑与 --expect 的分工在此钉死，谁也不许替谁放行。
    """
    root = _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2", "0.35.0"),
                      notes=("v0.34.2", "v0.35.0"), server_only=("v0.35.0",),
                      live_markers=("0.34.2",))
    ok_rc, ok_lines = _run(root)
    assert ok_rc == 0, ok_lines
    live_js = root / LIVE_APP_JS
    live_js.unlink()
    rc, lines = _run(root)
    assert rc == 0, "裸跑这一格去读了装机侧，没有 dist\\ 的机器上会当场退 2：%s" % lines
    assert not any("live-appjs" in ln for ln in lines), lines
    assert not any("verdict=UNKNOWN" in ln for ln in lines), lines
    live_js.parent.mkdir(parents=True, exist_ok=True)
    live_js.write_text("/* 假日机包 */\n/* release-marker-v0.35.0 */\n", encoding="utf-8")
    blind_rc, blind_lines = _run(root)
    assert not any("already-present" in ln for ln in blind_lines), \
        "裸跑这一格读到了装机侧，射程声明就成了假话：%s" % blind_lines
    assert blind_rc == 0, blind_lines
    expect_rc, expect_lines = _run(root, expect="v0.35.0")
    assert expect_rc == 1 and any("token=server-only-marker=already-present" in ln
                                 and "release-marker-v0.35.0" in ln
                                 and "live-lines=1" in ln for ln in expect_lines), expect_lines


def test_real_repo_bare_run_names_the_marker_of_the_release_in_flight():
    """真仓库上裸跑给的那一串，必须等于测试**自己独立读一遍**（字面正则、不借判据的尺子）算出来
    的结果，并且逐字符等于 app.js 里最新那枚标记、在文件里恰好一行。
    写死的版本号会腐烂，所以这里两个方向都由现场推导：只换服务端的一版在途时报那一版，
    否则报声明侧。
    """
    scope, target = _repo_side_bare_expectation(REPO_ROOT)
    expected = "release-marker-v%s" % target
    text = APP_JS.read_text(encoding="utf-8")
    newest_in_app_js = max((m.group(1) for m in NOTE_MARKER_RE.finditer(text)), key=_vkey)
    rc, lines = _run(REPO_ROOT)
    assert rc == 0, "真仓库现状必须绿（这条是常驻锁，红着就是拦路）：%s" % lines
    assert "scope=%s" % scope in lines[0] and "marker=%s" % expected in lines[0], (
        "裸跑的 target/scope 与独立推导不符：%s（独立推导=%s/%s）" % (lines[0], scope, target))
    assert any("verdict=PASS" in ln and expected in ln for ln in lines), lines
    # 判据与探针同构就是恒绿自证，所以这一格反过来问：**独立推导要是算错版本，这条必须红**。
    # 现场 v0.34.3 与 app.js 里最新那枚逐字符相同，由下面这句钉住（"恰好一行"那格由
    # test_real_repo_marker_equals_the_derived_declared_version 钉，那里数过 marker-lines）。
    assert expected == "release-marker-v%s" % newest_in_app_js, (
        "独立推导要的是 %r，app.js 里最新那枚却是 v%s：三件套在这里已经错位" % (expected, newest_in_app_js))
    assert text.count(expected) == 1, "%r 在 app.js 里必须恰好一行" % expected
