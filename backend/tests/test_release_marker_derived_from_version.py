"""换包用的 `-Marker` 必须是**版本号推导出来的**，不是人在窗口里挑的（#43）。

事故形状：`deploy/swap-package.ps1` 的预检把标记数两次（暂存包必须有、装机包必须没有），
而**只改后端的一版**里 `backend/app/web/` 一字未动，天然不存在这样的串。v0.34.2 过得去是
因为有人手工在 `app.js` 里补了一行 `release-marker-v0.34.2`——那是补产物，不是补机制
（`ff85ee0f` 补的是上一版的产物）。手工挑串有两种红法，且都在窗口打开之后才被发现：
挑了两边都有的串 = 预检被自己人盖章、换包后的 ACCEPT 空转；挑了新包里没有的串 = 预检
当场拒绝，而看门狗已经停了。

本文件钉的是 `tools/release_marker.py` 那条推导规则：

    <VERSION> = android-native/app/build.gradle 的 versionName（pre_tag_gate 第 1 条同源）
    -Marker   逐字符等于 release-marker-v<VERSION>，且在 app.js 里恰好命中一行
    app.js 里最高的那枚标记 == 这一版（落后＝空转验收，跑在前面＝指向未声明的版本）
    任意两枚标记不得互为子串（`Select-String -SimpleMatch` 数子串，v0.34.1 会命中 v0.34.10）
    docs/releases 里最高的那一版不得跑在目标版本前面

**为什么不在 `tools/pre_tag_gate.py` 里加第 6 条**：那把闸的第 3 条要求线上 `/health` 的
build **已经**等于这个 tag，也就是它只有在下**线之后**才可能绿；而标记缺不缺必须在上
线之前说，否则拦住的时刻正是最怕的时刻。发版顺序里排在"构建 staging"之前的只有
`CI 全绿 → 本地全量复跑`，所以常驻判据落在这里。

反例全部是 `tmp_path` 下现造的假树（`android-native/app/build.gradle` +
`backend/app/web/static/app.js` + `docs/releases/`），**不碰仓库里的真文件、更不碰
`data\\`（真数据目录）**：判据读的是传进去的 `repo_root`，这一点由
`test_the_tool_reads_only_the_root_it_is_handed` 钉住。
"""
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "release_marker.py"
GATE = REPO_ROOT / "tools" / "pre_tag_gate.py"
SWAP_PS1 = REPO_ROOT / "deploy" / "swap-package.ps1"
APP_JS = REPO_ROOT / "backend" / "app" / "web" / "static" / "app.js"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rm = _load(TOOL, "release_marker_under_test")
gate = _load(GATE, "pre_tag_gate_for_regex_drift")


# ------------------------------------------------------------------ 假树脚手架
def _fake_repo(root: Path, version_name="0.34.2", markers=("0.34.2",), notes=("v0.34.2",)):
    """只放判据真正会读的三样东西，路径形状与仓库一致。"""
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
    releases = root / "docs" / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    for note in notes:
        (releases / ("%s.md" % note)).write_text("# %s\n" % note, encoding="utf-8")
    return root


def _run(root, expect=None):
    return rm.evaluate(str(root), expect)


# ---------------------------------------------------------------------- 正对照
def test_derivation_script_is_in_the_repo_and_runnable():
    assert TOOL.is_file(), "tools/release_marker.py 必须入库：推导是脚本，不是文档里的一句话"
    assert callable(rm.main) and callable(rm.evaluate), "这把锁没有可调的入口，等于没锁"


def test_real_repo_marker_equals_the_derived_declared_version():
    """现网这一版（v0.34.2）：标记必须由 versionName 算出来，且真在 app.js 里恰好一行。"""
    declared = rm.declared_version(str(REPO_ROOT))
    marker = rm.marker_for(declared)
    rc, lines = _run(REPO_ROOT)
    assert rc == 0, "仓库现状必须绿，否则下面所有反例都无从对照：%s" % lines
    text = APP_JS.read_text(encoding="utf-8")
    assert marker == "release-marker-v0.34.2", (
        "声明侧读出来的版本与现网不符：拿到 %r。要么 versionName 被改过，要么这把尺子读错了文件" % marker)
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
    """构建之前按目标版本核：versionName 没 bump 就该红，而不是顺着操作者的嘴。"""
    _fake_repo(tmp_path, version_name="0.34.2", markers=("0.34.2",), notes=("v0.34.2",))
    rc, lines = _run(tmp_path, expect="v0.35.0")
    assert rc == 1, "操作者说要发 0.35.0、声明侧还是 0.34.2，却被放行：%s" % lines
    assert any("token=versionname-not-bumped" in ln for ln in lines), lines
    ok_rc, ok_lines = _run(tmp_path, expect="v0.34.2")
    assert ok_rc == 0, ok_lines
    bad_rc, bad_lines = _run(tmp_path, expect="0.34.2")
    assert bad_rc == 1 and any("expect-must-be-full-tag" in ln for ln in bad_lines), bad_lines


def test_unreadable_sources_fail_closed_not_green(tmp_path):
    """判据读不到＝不许（与 pre_tag_gate 的 fail-closed 同一档），不是"当它过了"。"""
    empty = tmp_path / "nothing"
    empty.mkdir()
    rc, lines = _run(empty)
    assert rc == 2, "读不到 build.gradle 却给了绿灯：%s" % lines
    assert any("verdict=UNKNOWN" in ln for ln in lines), lines

    partial = _fake_repo(tmp_path / "noappjs", version_name="0.34.2", notes=("v0.34.2",))
    (partial / "backend" / "app" / "web" / "static" / "app.js").unlink()
    rc2, lines2 = _run(partial)
    assert rc2 == 2 and any("appjs-missing" in ln for ln in lines2), lines2
