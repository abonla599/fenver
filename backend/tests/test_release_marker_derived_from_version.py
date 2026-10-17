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

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "release_marker.py"
GATE = REPO_ROOT / "tools" / "pre_tag_gate.py"
CHANGELOG = REPO_ROOT / "tools" / "gen_changelog.py"
SWAP_PS1 = REPO_ROOT / "deploy" / "swap-package.ps1"
APP_JS = REPO_ROOT / "backend" / "app" / "web" / "static" / "app.js"
SCOPE_SERVER_ONLY = "<!-- scope: server-only -->"


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
               server_only=(), live_markers=None):
    """只放判据真正会读的那几样东西，路径形状与仓库一致。

    `server_only` 里点名的那份说明会带上机器读的 `<!-- scope: server-only -->`（写在标题之后、
    第一个小节之前，与仓库里 v0.34.1.md 的位置一致）；不在名单里的说明就是缺省形状（有安装包）。

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
        else:
            text = "# %s\n" % note
        (releases / ("%s.md" % note)).write_text(text, encoding="utf-8")
    return root


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


def test_scope_regex_has_not_drifted_from_gen_changelog():
    """scope 行只有**一处**声明、一套读法：两把闸读成同一个值，否则"这一版是不是只换服务端"
    会长出第三种真相（发版说明页与换包闸各点头一次）。与 versionName 正则同源同一条锁法。
    """
    assert rm.SCOPE_RE.pattern == cl._SCOPE_RE.pattern, (
        "tools/release_marker.py 与 tools/gen_changelog.py 的 scope 正则分叉了："
        "%r vs %r" % (rm.SCOPE_RE.pattern, cl._SCOPE_RE.pattern))
    assert rm.SCOPE_SERVER_ONLY == cl.SCOPE_SERVER_ONLY, "server-only 这个值两边读得不一样"
    assert rm.SCOPE_INSTALLABLE == cl.SCOPE_INSTALLABLE, "installer 这个值两边读得不一样"
    assert rm.SECTION_RE.pattern == cl._SECTION_RE.pattern, (
        "引言区到哪里为止两边读得不一样：正文里那句历史叙述会被当成第二次判据")


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
