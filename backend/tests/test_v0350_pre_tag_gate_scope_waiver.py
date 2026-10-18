"""#67：`pre_tag_gate.py` 的第 1 条判据必须按**这一版自己声明的形态**判，不再一视同仁。

## 这条锁守的是哪一种事故

`tools/pre_tag_gate.py` 的第 1 条判据原先只问一句：`build.gradle` 的 versionName 等不等于
这个 tag？不等就 `versionname-mismatch`，而且它**跑在第 2 条判据（发版说明在不在）之前**，
所以连那份写着「只换服务端」的说明都没被读过一眼。

**只换服务端的一版根本不 bump versionName**——实测 `git show v0.34.1:android-native/app/build.gradle`
是 `versionCode 60 / versionName "0.34.0"`，而同一时刻 `docs/releases/v0.34.1.md` 第 3 行写着
`<!-- scope: server-only -->`。于是下一版纯后端的 v0.35.0 会被这把锁拦住，唯一的出路是
`--no-versionname-check`——而那面旗的 help 自己写明只给"这份 checkout 里没有 android-native/"用
（CI 那一格）。台账里 v0.34.2 是**没带这个旗**过的，v0.34.1 那次带旗被记为"绕道，本版该跟着撤"：
用过一次，从未被追认。同一个脚本的第 5 条判据却早就接受形态声明（`--backend-only`，help 里还
写着"纯后端版，v0.35.x 就可能是"）——**一把锁对"纯后端版合不合法"给了两种答案**。

这正是 #65/#69/#71 在 `release_marker.py` 上刚修掉的同一个混用（把"要切哪一版"的权威当成
versionName），只不过长在第二把锁上。

## 这套锁钉住四件事

1. **豁免的判据是世界事实**（那份说明里那一行），不是操作者手边的旗；`--no-versionname-check`
   的语义一字未动（`test_no_versionname_check_flag_keeps_its_original_meaning`）；
2. **读不出形态就不豁免**：缺说明 / 说明读不动 / scope 值不认识 ⇒ 照旧要求
   `versionName == tag`，退 1。默认成 server-only 等于让这把闸在任何一份坏说明面前静默放绿
   （`test_gate_fails_closed_when_the_scope_verdict_cannot_be_read`）；
3. **逆命题也要核**：说明写着只换服务端、versionName 却已经等于这个 tag ⇒ 拒绝
   （`versionname-bumped-without-package`）——那是在对外宣布一个装不上的安卓版本；
4. **第 2/3/4/5 条判据与退出码分档一字未改**：可安装那一版匹配时照旧全绿、不匹配时照旧退 1
   （`test_installable_release_*` 两条）。

另外钉一条**实现方式**：scope 的读法只有 `tools/release_marker.py` 那一份（#67 的原始要求是
"不许抄第三份正则"——`gen_changelog.py` 与 `release_marker.py` 已经各有一份，两份的合并归 #68）。
连带钉住这条 import 的前提：`tools/release_marker.py` 没被 .gitignore 挡住，否则 CI 那次
checkout 里 import 直接炸（手法照 `test_v0331_pre_tag_gate_contract.py` 对闸门自己做的事）。
"""
import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "tools" / "pre_tag_gate.py"
SCOPE_READER = REPO_ROOT / "tools" / "release_marker.py"
TAG = "v0.35.0"


def _load_gate():
    spec = importlib.util.spec_from_file_location("pre_tag_gate", str(GATE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def gate(monkeypatch):
    """闸门本体 + 把与第 1 条判据无关的旁证接掉（手法照 test_v0331_pre_tag_gate_contract.py）。

    第 2 条（说明在不在）、第 3/4 条（`/health`）、第 5 条（发版物料顺序）、第 6 条
    （泄露隔离闸）**都不归本文件管**：留着它们，测错位的那条会先撞上别的理由，
    红的理由和断言的理由对不上。

    第 6 格尤其必须接掉：本文件的 `root` 是**真仓库**，而那一格判的就是真仓库此刻的
    ref 状态——不接的话这些用例断言的就不再是"第 1 条怎么豁免"，而是"本机有没有脏
    tag"（那种断言一删 tag 就变，方向还是反的）。它自己的牙在
    test_v0350_leak_quarantine_gate.py，用 `tmp_path` 现造的一次性仓库跑红／绿／
    判不出三档，不借这里的地盘。
    """
    mod = _load_gate()
    monkeypatch.setattr(mod, "release_note_exists", lambda root, tag: True)
    monkeypatch.setattr(mod, "check_announcement_materials",
                        lambda root, ver: (True, "ok", "stubbed：本用例只测第 1 条判据"))
    monkeypatch.setattr(mod, "check_leak_quarantine",
                        lambda root: (True, "ok", "stubbed：本用例只测第 1 条判据"))
    monkeypatch.setattr(mod, "fetch_build", lambda url, timeout: (TAG, "ok"))
    return mod


def _shape(gate, monkeypatch, scope_verdict, declared):
    """把第 1 条判据的两个输入钉成指定形状：(是不是 server-only, 读不出的理由) 与声明侧。"""
    monkeypatch.setattr(gate, "note_declares_server_only",
                        lambda root, tag: scope_verdict)
    monkeypatch.setattr(gate, "declared_version_name", lambda root: declared)


def _write_note(root, body_bytes):
    folder = root / "docs" / "releases"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (TAG + ".md")).write_bytes(body_bytes)


# --------------------------------------------------------------------------
# 1) 豁免只在真的读到 scope: server-only 时生效
# --------------------------------------------------------------------------

def test_server_only_note_waives_the_versionname_equality(gate, monkeypatch, capsys):
    """本版说明声明只换服务端 ⇒ 第 1 条**不许**要求 versionName == tag，且要打印自己的记号。

    这就是 v0.35.0（纯后端版）的形状：声明侧停在 0.34.2，tag 是 v0.35.0。改动前这里
    `rc=1 versionname-mismatch`（reviewer 实测），修完必须绿，并且日志里看得见为什么跳过。
    """
    _shape(gate, monkeypatch, (True, None), ("0.34.2", "ok"))
    code = gate.main(["--expect", TAG])
    out = capsys.readouterr().out
    assert code == 0, "纯后端版被第 1 条判据拦住了：那正是 #67 要修的形状"
    assert "versionname=waived-scope" in out, "跳过等式必须留下自己的记号，不许安静跳过"
    assert "scope=server-only" in out and TAG in out
    assert "versionname-mismatch" not in out
    assert "verdict=PASS" in out


def test_gate_does_not_demand_the_flag_for_a_server_only_release(gate, monkeypatch, capsys):
    """豁免**不许**通过 `--no-versionname-check` 那条路走：带旗时第 1 条整条跳过，
    打印的是"没有这一格"而不是 `waived-scope`——旗的语义仍然是"这份 checkout 没有
    android-native/"（`release-apk.yml` 那一格），不是"这一版是纯后端版"。
    """
    _shape(gate, monkeypatch, (True, None), ("0.34.2", "ok"))
    assert gate.main(["--expect", TAG, "--no-versionname-check"]) == 0
    assert "versionname=waived-scope" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# 2) 读不出形态 ⇒ 不豁免（fail-closed，绝不默认成 server-only）
# --------------------------------------------------------------------------

def test_gate_fails_closed_when_the_scope_verdict_cannot_be_read(gate, monkeypatch, capsys):
    """缺说明 / 说明读不动 / scope 值不认识 ⇒ **照旧**要求 versionName == tag，退 1。

    这三个形状就是 #67 验收里的 (b)(c)(d)：可安装那一版不匹配必须照旧红；说明缺席或坏掉
    的时候默认成 server-only 会把这把闸在坏说明面前静默放成绿灯——那是事故的重演入口。
    """
    for reason in (gate.NOTE_MISSING,
                   "release-note-unreadable:...: not-utf8(UnicodeDecodeError)",
                   "release-note-scope-unknown:...=backend-only（只认 server-only / installer）"):
        _shape(gate, monkeypatch, (False, reason), ("0.34.2", "ok"))
        capsys.readouterr()
        code = gate.main(["--expect", TAG])
        out = capsys.readouterr().out
        assert code == 1, "读不出形态（%s）却放行了：豁免必须是读到的结论，不是猜的" % reason
        assert "versionname-mismatch" in out
        assert "waived-scope" not in out


def test_unknown_scope_value_refuses_instead_of_waiving(tmp_path, gate):
    """`<!-- scope: backend-only -->` 这种不认识的取值 ⇒ 判据回 (False, 理由)——不是 True。

    真跑读法（不 monkeypatch），钉住"不豁免"这条方向是由 `note_scope` 的返回值决定的。
    """
    _write_note(tmp_path, ("# %s\n\n<!-- scope: backend-only -->\n\n## 内容\n" % TAG).encode("utf-8"))
    assert gate.note_declares_server_only(str(tmp_path), TAG)[0] is False


def test_missing_note_is_not_treated_as_server_only(tmp_path, gate):
    """没有那份说明 ⇒ 判据回 (False, NOTE_MISSING)：**默认值是可安装那一版**，不是纯后端。"""
    (tmp_path / "docs" / "releases").mkdir(parents=True)
    server_only, reason = gate.note_declares_server_only(str(tmp_path), TAG)
    assert server_only is False and reason == gate.NOTE_MISSING


# --------------------------------------------------------------------------
# 3) 逆命题：只换服务端那一版把 versionName 顶上来也是事故
# --------------------------------------------------------------------------

def test_bumped_versionname_under_a_server_only_note_is_refused(gate, monkeypatch, capsys):
    """说明写只换服务端、versionName 却已经等于这个 tag ⇒ `versionname-bumped-without-package`。

    这一格声明的是一个盘上根本不存在的可安装安卓版本，违反"只播最新**装得上**的那一版"
    （#17/#18）。改动前这个形状是**绿灯放行**（假树 f 实测 rc=0），修完必须红。
    """
    _shape(gate, monkeypatch, (True, None), ("0.35.0", "ok"))
    code = gate.main(["--expect", TAG])
    out = capsys.readouterr().out
    assert code == 1, "只换服务端的一版把 versionName 顶到这个号却放行 = 播一个装不上的版本"
    assert "versionname-bumped-without-package" in out


def test_unreadable_gradle_still_exits_2_under_the_waiver(gate, monkeypatch, capsys):
    """豁免不改变"声明侧读不到 = 退 2"这一档：逆命题那一格也要读得到声明侧才核得动。"""
    _shape(gate, monkeypatch, (True, None), (None, "gradle-missing"))
    assert gate.main(["--expect", TAG]) == 2
    assert "versionname=unknown" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 4) 其余形状一字未改（可安装那一版：匹配绿 / 不匹配红）
# --------------------------------------------------------------------------

def test_installable_release_with_matching_versionname_still_passes(gate, monkeypatch, capsys):
    """(a) 有安装包 + versionName 已追平 ⇒ `versionname=ok`、退 0，与改动前逐字一致。"""
    _shape(gate, monkeypatch, (False, None), ("0.35.0", "ok"))
    assert gate.main(["--expect", TAG]) == 0
    out = capsys.readouterr().out
    assert "versionname=ok declared=0.35.0" in out and "verdict=PASS" in out


def test_installable_release_with_stale_versionname_still_refuses(gate, monkeypatch, capsys):
    """(b) 有安装包（说明没标 scope）+ versionName 没追平 ⇒ 照旧退 1 `versionname-mismatch`。"""
    _shape(gate, monkeypatch, (False, None), ("0.34.2", "ok"))
    assert gate.main(["--expect", TAG]) == 1
    assert "versionname-mismatch" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 5) 实现方式：只有一把尺子，且它在 CI 的 checkout 里
# --------------------------------------------------------------------------

END_TO_END = [
    # (说明形态, build.gradle 的 versionName, 期望记号, 期望退出码)
    ("server-only", "0.34.2", "versionname=waived-scope", 0),
    ("installer", "0.35.0", "versionname=ok", 0),
    ("installer", "0.34.2", "versionname-mismatch", 1),
    ("unknown", "0.34.2", "versionname-mismatch", 1),
    ("garbage", "0.34.2", "versionname-mismatch", 1),
    ("missing", "0.34.2", "versionname-mismatch", 1),
    ("server-only", "0.35.0", "versionname-bumped-without-package", 1),
]


@pytest.mark.parametrize("kind,gradle,token,code", END_TO_END)
def test_check_one_actually_reads_that_note_file(tmp_path, monkeypatch, capsys,
                                                 kind, gradle, token, code):
    """第 1 条判据**真的去读那一份文件**：上面几格是 monkeypatch 过的分支，这里**第 1 条的
    输入**一个都不 stub——说明文件是现场写进假树的，读的就是那几行字节。

    没有这一组，"豁免"可以只靠 stub 活着——把判读改成"说明文件存在就豁免"（#67 变异 i）或
    "读不出就默认 server-only"（变异 ii），前面那几条都不会红，而真树上一切照旧。

    接掉的仍然只有旁证：`/health`（第 3 条）与物料顺序（第 5 条）照旧 stub；泄露隔离闸
    （第 6 条）也接掉——这份假树**故意不是 git 仓库**，那一格在这儿只能列不出 ref、
    按 fail-closed 报它自己那一档红，而红的是它的理由不是本用例的理由。它不 stub 的那些
    牙在 test_v0350_leak_quarantine_gate.py，那里跑的是真 git 仓库。
    """
    mod = _load_gate()
    tools = tmp_path / "tools"
    tools.mkdir()
    gradle_dir = tmp_path / "android-native" / "app"
    gradle_dir.mkdir(parents=True)
    (gradle_dir / "build.gradle").write_text('versionName "%s"\n' % gradle, encoding="utf-8")
    if kind != "missing":
        body = (b"\xff\xfe\x00\x01 not utf-8" if kind == "garbage" else
                ("# %s\n\n<!-- scope: %s -->\n\n## 内容\n" % (TAG, kind)).encode("utf-8"))
        _write_note(tmp_path, body)
    # root 是从模块的 __file__ 推出来的：把它挪到这份假树上，闸门就只读 tmp_path（不碰真仓库）。
    monkeypatch.setattr(mod, "__file__", str(tools / "pre_tag_gate.py"))
    monkeypatch.setattr(mod, "fetch_build", lambda url, timeout: (TAG, "ok"))
    monkeypatch.setattr(mod, "check_announcement_materials",
                        lambda root_, ver: (True, "ok", "stubbed：本用例只测第 1 条判据"))
    monkeypatch.setattr(mod, "check_leak_quarantine",
                        lambda root_: (True, "ok", "stubbed：本用例只测第 1 条判据"))
    assert mod.main(["--expect", TAG]) == code, (kind, gradle)
    out = capsys.readouterr().out
    assert token in out, "形状 (%s, versionName=%s) 停在 %r，不是 %r" % (kind, gradle, out, token)


def test_gate_delegates_the_scope_read_and_copies_no_third_regex():
    """scope 的读法**不许在这把锁里再抄一份**（#67 原始要求）。

    仓库里已经有两份：`tools/gen_changelog.py` 与 `tools/release_marker.py`（#68 负责合并）。
    第三份一旦长出来，"这一版到底是不是只换服务端"就有了第三种真相——#65/#69/#71 修的正是这个。
    """
    src = GATE.read_text(encoding="utf-8")
    assert "from release_marker import" in src and "note_scope" in src, \
        "第 1 条判据必须吃 release_marker.note_scope 那把尺子"
    code = src.split('"""')[2]          # 模块 docstring 之后的那一截（docstring 里引用那一行是给人读的）
    assert "<!--" not in code and "scope:" not in code, \
        "闸门代码里不许出现 scope 行的正则或字面量——读法只有 release_marker 那一份"
    mod = _load_gate()
    assert not hasattr(mod, "SCOPE_RE"), "闸门自己不许再定义一条 scope 正则"
    assert mod.note_declares_server_only(str(REPO_ROOT), "v0.34.1")[0] is True, \
        "真树的 v0.34.1 就是 server-only： delegation 必须是活的而不是恒 False"


def test_scope_reader_is_present_in_a_ci_checkout():
    """import 的前提必须钉住：`tools/release_marker.py` 没被 .gitignore 挡住。

    `/tools/*` 默认整目录排除；闸门被 `!` 例外放进来才有 CI 那一步，被 import 的那份同理——
    少了这条，本机全绿而 CI 每次都在 import 上炸（机制退化回一句提醒，而且退化得悄无声息）。
    """
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--stdin"],
        input=b"tools/release_marker.py\n", capture_output=True, timeout=60)
    assert res.returncode != 0, (
        "tools/release_marker.py 被 .gitignore 挡住了：pre_tag_gate.py import 它会在 CI "
        "那次 checkout 里当场炸。加一行 !/tools/release_marker.py")
    # input 走 bytes：text=True 时 Windows 上 "\n" 会被换成 "\r\n"，git 拿带 \r 的路径去
    # 匹配，永远"没被忽略"，这条锁就空转了。
