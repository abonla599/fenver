"""#322(b)：安卓专用版（scope: android-only）的闸门第 3 关豁免只在物料齐时生效。

## 这条锁守的是哪一种事故

`tools/pre_tag_gate.py` 第 3 关要求线上 `/health` 的 build == 待打 tag。安卓专用版
只出 APK、不换服务端包，线上 build 永远停在服务端线 ⇒ 对安卓 tag 结构性必挂，
没有任何开关可绕。分家后新增第三个被显式承认的 scope 值 `android-only`：第 3 关
对它不核「线上 build == tag」，安卓侧的现实判据由**既有第 5 关**
`check_announcement_materials` 承担（清单宣告该版 + 包落盘且摘要相符）。

豁免必须由发版说明**显式声明**，闸门绝不推断：缺说明 / 读不动 / 值不认识 ⇒
拿不到原始 scope 值 ⇒ 第 3 关照旧核。物料不齐时豁免不生效——一个开关能授权
事故形状的闸门不是机制。

## 判据源

`tmp_path` 假树 + 真跑 `main()`：第 5 关**不 stub**，读的是假清单与假 APK 字节；
`RELEASE_MANIFEST_PATH` / `APK_CACHE_DIR` 钉在 tmp_path（手法照
test_v0350_release_materials_gate.py）。只接掉与本格无关的旁证：`fetch_build`
（不触网，返回固定的服务端线 build）与泄露隔离闸（假树不是 git 仓库，那一格
自己的牙在 test_v0350_leak_quarantine_gate.py）。

漂移两枚（实跑记录在实现报告，不在这里恒绿自证）：
- 拆掉第 3 关的 android-only 豁免 ⇒ 正控格当场红（live-build-mismatch）；
- 拆掉第 5 关对 android-only 的物料核 ⇒ 三个物料反例格当场红（被放行）。
"""
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from tests.test_machine_read_pointers import strip_py_prose

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "tools" / "pre_tag_gate.py"
MARKER_TOOL = REPO_ROOT / "tools" / "release_marker.py"

TAG = "v0.99.0"
VERSION = "0.99.0"
SERVER_BUILD = "v0.39.2"          # 线上停在服务端线：安卓 tag 永不等于它
SCOPE_ANDROID_ONLY_LINE = "<!-- scope: android-only -->"


def _load_gate():
    spec = importlib.util.spec_from_file_location("pre_tag_gate_android_only", str(GATE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_marker():
    spec = importlib.util.spec_from_file_location("release_marker_android_only", str(MARKER_TOOL))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _android_tree(tmp_path, scope_line=SCOPE_ANDROID_ONLY_LINE):
    """只放闸门真正会读的那几样：声明侧、发版说明。清单与 APK 由各格自己摆。"""
    gradle = tmp_path / "android-native" / "app" / "build.gradle"
    gradle.parent.mkdir(parents=True, exist_ok=True)
    gradle.write_text('versionName "%s"\n' % VERSION, encoding="utf-8")
    note = tmp_path / "docs" / "releases" / (TAG + ".md")
    note.parent.mkdir(parents=True, exist_ok=True)
    body = "# %s 更新说明\n\n" % TAG
    if scope_line:
        body += scope_line + "\n\n"
    body += "## 修复了什么\n\n- 安卓侧改动。\n"
    note.write_text(body, encoding="utf-8")
    return tmp_path


def _write_manifest(path, version=VERSION, sha=None, apk_bytes=b"fake apk bytes"):
    if sha is None:
        sha = hashlib.sha256(apk_bytes).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": version,
        "apk_name": "fenver-%s.apk" % version,
        "sha256": sha,
        "size": len(apk_bytes),
        "url": "https://ai.fenever.xyz/site/android.apk",
    }), encoding="utf-8")
    return sha


@pytest.fixture
def android_gate(tmp_path, monkeypatch):
    """假树 + 钉住两只物料口子 + 接掉无关旁证；第 5 关真跑。"""
    root = _android_tree(tmp_path)
    manifest = tmp_path / "manifest" / "version.json"
    apk_dir = tmp_path / "apk-cache"
    apk_dir.mkdir()
    monkeypatch.setenv("RELEASE_MANIFEST_PATH", str(manifest))
    monkeypatch.setenv("APK_CACHE_DIR", str(apk_dir))
    mod = _load_gate()
    # root 从模块 __file__ 推出来：挪到假树上，闸门就只读 tmp_path（不碰真仓库）。
    (root / "tools").mkdir()
    monkeypatch.setattr(mod, "__file__", str(root / "tools" / "pre_tag_gate.py"))
    monkeypatch.setattr(mod, "fetch_build", lambda url, timeout: (SERVER_BUILD, "ok"))
    monkeypatch.setattr(mod, "check_leak_quarantine",
                        lambda root_: (True, "ok", "stubbed：本用例只测第 3/5 关"))
    return {"mod": mod, "root": root, "manifest": manifest, "apk_dir": apk_dir}


def _run_gate(android_gate):
    return android_gate["mod"].main(["--expect", TAG])


# ---------------------------------------------------------------- 四格物料判据

def test_android_only_with_complete_materials_passes_gate3(android_gate, capsys):
    """正控：清单宣告 + 包落盘 + 摘要相符 ⇒ 第 3 关不红、verdict=PASS。

    线上 build 停在服务端线（≠ 安卓 tag），这正是安卓专用版的预期形状；
    豁免必须留痕（waived-scope），不许安静跳过。
    """
    apk = android_gate["apk_dir"] / (VERSION + ".apk")
    apk.write_bytes(b"fake apk bytes")
    _write_manifest(android_gate["manifest"])
    code = _run_gate(android_gate)
    out = capsys.readouterr().out
    assert code == 0, "物料齐的安卓专用版被第 3 关结构性拦死：%s" % out
    assert "live-build=waived-scope" in out and "android-only" in out, (
        "豁免没有留痕：%s" % out)
    assert "live-build-mismatch" not in out
    assert "apk-materials=ok" in out, "第 5 关没有真跑物料核：%s" % out
    assert "verdict=PASS" in out


def test_android_only_without_announcement_is_refused(android_gate, capsys):
    """格 1：清单没宣告该版 ⇒ 非零 + manifest-version-mismatch。豁免不授权缺物料。"""
    apk = android_gate["apk_dir"] / (VERSION + ".apk")
    apk.write_bytes(b"fake apk bytes")
    _write_manifest(android_gate["manifest"], version="0.36.5")
    code = _run_gate(android_gate)
    out = capsys.readouterr().out
    assert code != 0, "清单没宣告这一版却放行了：%s" % out
    assert "apk-materials-manifest-version-mismatch" in out
    assert "verdict=PASS" not in out


def test_android_only_with_missing_package_is_refused(android_gate, capsys):
    """格 2：清单宣告了但盘上没有 APK ⇒ 非零 + package-missing。"""
    _write_manifest(android_gate["manifest"])
    code = _run_gate(android_gate)
    out = capsys.readouterr().out
    assert code != 0, "盘上没有那串字节却放行了：%s" % out
    assert "apk-materials-package-missing" in out
    assert "verdict=PASS" not in out


def test_android_only_with_digest_mismatch_is_refused(android_gate, capsys):
    """格 3：摘要不符 ⇒ 非零 + package-digest-mismatch。"""
    apk = android_gate["apk_dir"] / (VERSION + ".apk")
    apk.write_bytes(b"tampered bytes")
    _write_manifest(android_gate["manifest"], sha=hashlib.sha256(b"fake apk bytes").hexdigest())
    code = _run_gate(android_gate)
    out = capsys.readouterr().out
    assert code != 0, "摘要对不上却放行了：%s" % out
    assert "apk-materials-package-digest-mismatch" in out
    assert "verdict=PASS" not in out


def test_the_waiver_is_not_inferred_without_an_explicit_declaration(android_gate, capsys):
    """反面对照：同一棵树、说明没有 scope 行 ⇒ 第 3 关照旧 live-build-mismatch。

    豁免只在说明显式声明 android-only 时生效；"线上 build 不等于 tag"本身不是
    推断安卓版的理由。物料这一格摆齐，证明红的是第 3 关而不是第 5 关。
    """
    _android_tree(android_gate["root"], scope_line="")
    apk = android_gate["apk_dir"] / (VERSION + ".apk")
    apk.write_bytes(b"fake apk bytes")
    _write_manifest(android_gate["manifest"])
    code = _run_gate(android_gate)
    out = capsys.readouterr().out
    assert code != 0 and "live-build-mismatch" in out, (
        "没声明 android-only 却拿到了豁免：%s" % out)
    assert "waived-scope" not in out


def test_android_only_with_matching_live_build_still_reports_ok(android_gate, capsys):
    """线上 build 恰好等于 tag 时走原来的 ok 分支：豁免不改变既有绿灯的形状。"""
    android_gate["mod"].fetch_build = lambda url, timeout: (TAG, "ok")
    apk = android_gate["apk_dir"] / (VERSION + ".apk")
    apk.write_bytes(b"fake apk bytes")
    _write_manifest(android_gate["manifest"])
    code = _run_gate(android_gate)
    out = capsys.readouterr().out
    assert code == 0 and "live-build=ok" in out, out
    assert "waived-scope" not in out


# ------------------------------------------------ scope 模型：三值单源 + bool 不动

@pytest.mark.parametrize("scope,expected", [
    ("server-only", "server-only"),
    ("installer", "installer"),
    ("android-only", "android-only"),
])
def test_note_scope_value_returns_the_raw_value(tmp_path, scope, expected):
    rm = _load_marker()
    note = tmp_path / "docs" / "releases" / "v0.9.9.md"
    note.parent.mkdir(parents=True)
    note.write_text("# v0.9.9\n\n<!-- scope: %s -->\n" % scope, encoding="utf-8")
    value, reason = rm.note_scope_value(str(tmp_path), "0.9.9")
    assert (value, reason) == (expected, None)
    server_only, bool_reason = rm.note_scope(str(tmp_path), "0.9.9")
    assert bool_reason is None
    assert server_only is (scope == "server-only"), (
        "note_scope 的 bool 语义变了：android-only 与 installer 都出安装包，"
        "对『是不是只换服务端』必须同为 False")


def test_note_scope_value_fail_closed_shapes(tmp_path):
    """缺说明 / 没有 scope 行 / 值不认识：原始值访问器与 bool 读法同一套 fail-closed。"""
    rm = _load_marker()
    value, reason = rm.note_scope_value(str(tmp_path), "0.9.9")
    assert value is None and reason == rm.NOTE_MISSING
    note = tmp_path / "docs" / "releases" / "v0.9.9.md"
    note.parent.mkdir(parents=True)
    note.write_text("# v0.9.9\n", encoding="utf-8")
    assert rm.note_scope_value(str(tmp_path), "0.9.9") == (None, None)
    note.write_text("# v0.9.9\n\n<!-- scope: hotfix -->\n", encoding="utf-8")
    value2, reason2 = rm.note_scope_value(str(tmp_path), "0.9.9")
    assert value2 is None and reason2.startswith("release-note-scope-unknown:"), reason2
    assert rm.note_scope(str(tmp_path), "0.9.9") == (False, reason2)


def test_gate_delegates_the_android_scope_and_copies_no_literal():
    """闸门里不许出现 scope 字面量：常量与访问器都从 release_marker import。

    与 test_v0350_pre_tag_gate_scope_waiver 的委托锁同一把尺子（strip_py_prose）：
    剥掉注释与 docstring 之后，整份源码都在射程里。
    """
    src = strip_py_prose(GATE.read_text(encoding="utf-8"))
    assert "SCOPE_ANDROID_ONLY" in src and "note_scope_value" in src, (
        "第 3 关必须吃 release_marker 的常量与原始值访问器")
    assert "android-only" not in src, "闸门代码里出现了 android-only 字面量"
    assert "<!--" not in src and "scope:" not in src, (
        "闸门代码里不许出现 scope 行的正则或字面量——读法只有 release_marker 那一份")
    gate = _load_gate()
    # 对象同一要对着闸门真正 import 的那一份（sys.modules 里的规范模块）比：
    # 用 importlib 另起炉灶加载的副本与它值相等但不是同一个对象（#68 单源锁同一手法）。
    shared = importlib.import_module("release_marker")
    assert gate.SCOPE_ANDROID_ONLY is shared.SCOPE_ANDROID_ONLY, "常量不是同一个对象"
    assert gate.note_scope_value is shared.note_scope_value, "访问器不是同一份实现"
    assert shared.SCOPE_ANDROID_ONLY in shared.VALID_SCOPES
    assert gate.note_scope_value(str(REPO_ROOT), "0.34.1") == ("server-only", None), (
        "真树的 v0.34.1 就是 server-only：委托必须是活的而不是恒 None")