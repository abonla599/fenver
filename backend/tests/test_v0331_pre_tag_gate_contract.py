"""v0.33.1：把"打 tag 之前先确认线上就是这一版"钉成机制的契约锁。

## 这三条锁各守一次真实事故

发版顺序里 **"换包上线 → 线上验收 → 打 tag → 发 Release"** 的先后不是排版偏好：
tag 一推，`release-apk.yml` 就生成**公开 Release 和固定下载链接**。如果那时候线上
还在跑上一版，就会出现"Release 写着 vX，用户访问到的服务是 vX-1"——事后谁也说不清
装的是哪一版。

**这条顺序连着被违反两次**（v0.29.0、v0.33.0），而且两次的换包执行记录里都写了同一段
检讨："这条偏差已经连着两次出现在同一份记录里：不是提醒不够，是流程没有机制"。
所以这一版不再往记录里加第三条检讨，而是加三样东西：

1. `tools/pre_tag_gate.py` —— 判据取 `/health` 的 `build` 字段（**由正在应答的那个
   进程从自己包里读出来的版本戳**，不是仓库声明、不是人的转述），不符就非零退出；
2. workflow 里同名闸门步骤，排在签名与构建**之前**，红得越早越少半成品；
3. 本文件 —— 锁住"闸门存在、判据 fail-closed、顺序在 Release 之前"。
   少了这一层，前两条哪天被重构掉都没人知道，那又变回一句提醒。

判据形状沿用本仓库对 JS/Kotlin/PS 的文本锁惯例（见 test_ops_scripts.py /
test_swap_package_contract_v0291.py），再加一组**真跑 `main()`** 的行为断言：
闸门自己如果判错方向（把"取不到"当成"通过"），文本锁是锁不出来的。
"""
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "tools" / "pre_tag_gate.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-apk.yml"
GUIDE = REPO_ROOT / "docs" / "安装部署指南.md"


def _load_gate():
    spec = importlib.util.spec_from_file_location("pre_tag_gate", str(GATE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_side_checks(gate, monkeypatch):
    """把"与 build 判据无关的旁证"接掉，让每条断言只测它名义上那件事。

    发版说明是否存在、versionName 涨没涨，都是**另一条**判据（下面各有专门的用例）。
    留着它们，测错位的那条会先撞上"v0.33.1 的说明还没写"，红的理由和断言的理由对不上。
    """
    monkeypatch.setattr(gate, "release_note_exists", lambda root, tag: True)


def test_gate_script_exists_and_is_the_documented_shape():
    assert GATE.is_file(), "tools/pre_tag_gate.py 必须入库：闸门是脚本，不是文档里的一句话"
    src = GATE.read_text(encoding="utf-8")
    assert "argparse" in src and "--expect" in src, "闸门必须可命令行调用，CI 与本机关用同一份"
    assert "verdict=PASS" in src and "verdict=BLOCK" in src, "结论要机读，人能跳过一步不能跳过一行输出"

    # **它在 CI 的那次 checkout 里存在吗**——这条比上一条更要命。
    # `/tools/*` 在 .gitignore 里是整目录默认排除（历史上 _diag_shell_bench.py 就
    # 撞在这条上永远进不了仓库）。闸门文件如果没被 ! 例外放进来，本机测试全绿、
    # 文档照写、workflow 那一步照跑，而 CI 每次都在 `python3 tools/pre_tag_gate.py`
    # 上撞 "No such file or directory"——机制退化回一句提醒，而且退化得悄无声息。
    # 判据走 git 的真实语义（check-ignore 退出码 0 = 被忽略），不搜 .gitignore 文本。
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--stdin"],
        input=b"tools/pre_tag_gate.py\n", capture_output=True, timeout=60)
    assert res.returncode != 0, (
        "tools/pre_tag_gate.py 被 .gitignore 挡住了：CI 那次 checkout 里没有这个文件，"
        "闸门那一步必然跑不到判据。加一行 !/tools/pre_tag_gate.py"
    )
    # input 走 bytes：text=True 时 Windows 上 "\n" 会被换成 "\r\n"，git 于是拿带
    # \r 的路径去匹配，永远"没被忽略"，这条锁就空转了。


def test_gate_blocks_when_live_build_is_the_previous_version(monkeypatch, capsys):
    """核心那条：**线上是上一版 = 不许打 tag**。

    v0.33.0 当天出的就是形状本身：先打了 tag，再换包。这条断言不许有例外分支。
    """
    gate = _load_gate()
    _stub_side_checks(gate, monkeypatch)
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: ("v0.33.0", "ok"))
    code = gate.main(["--expect", "v0.33.1", "--no-versionname-check"])
    out = capsys.readouterr().out
    assert code != 0, "线上还是 v0.33.0，tag v0.33.1 必须被拦住"
    assert "live-build-mismatch" in out, "错误要说清是错位，不是网络"


def test_gate_is_fail_closed_when_the_live_verdict_cannot_be_read(monkeypatch, capsys):
    """**取不到 ≠ 通过**。"网络抖了一下"和"根本没换包"在证据上是同一件事：
    你无法证明线上是这一版，那就别打 tag。

    这一条是闸门自己最容易被写反的地方——写闸门的人直觉上会把"读不到"当环境问题
    放行，于是闸门在最需要它的时候（线上挂了/还没起来）恰好失效。
    """
    gate = _load_gate()
    _stub_side_checks(gate, monkeypatch)
    for why in ("unreachable:URLError", "http-502", "not-json", "no-build-field"):
        monkeypatch.setattr(gate, "fetch_build", lambda url, timeout, _w=why: (None, _w))
        code = gate.main(["--expect", "v0.33.1", "--no-versionname-check"])
        capsys.readouterr()
        assert code != 0, "判据取不到时闸门放行了（%s）——fail-open 的闸门等于没有闸门" % why

    # 连"线上根本没这个字段"也要拦：v0.33 刻意保留 build 字段作为自证，
    # 哪天被删，闸门必须当场红，而不是退化成"200 就是好的"。
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: (None, "no-build-field"))
    assert gate.main(["--expect", "v0.33.1", "--no-versionname-check"]) != 0


def test_gate_allow_unreachable_never_lets_a_wrong_version_through(monkeypatch, capsys):
    """`--allow-unreachable` 只降级"取不到"，**不许降级"取到了但不是这一版"**。

    有这个开关是因为离线环境确实要能放行，但它的边界必须锁死：一旦它能顺带放过
    错位，它就是那条事故的重演入口。
    """
    gate = _load_gate()
    _stub_side_checks(gate, monkeypatch)
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: ("v0.33.0", "ok"))
    code = gate.main(["--expect", "v0.33.1", "--no-versionname-check", "--allow-unreachable"])
    assert code != 0, "--allow-unreachable 把版本错位也放过了：这个开关只能管可达性"

    # 而真的取不到时，它必须放行并且**留下 WARNING 痕迹**（悄悄放行等于没有交代）
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: (None, "unreachable:URLError"))
    capsys.readouterr()
    code = gate.main(["--expect", "v0.33.1", "--no-versionname-check", "--allow-unreachable"])
    out = capsys.readouterr().out
    assert code == 0, "离线逃生门没了：运维只能在『证明不了』和『绕过整把锁』之间选"
    assert "WARNING" in out, "走了逃生门必须打印留痕，不许安静放行"


def test_gate_passes_when_live_reports_the_target(monkeypatch, capsys):
    """全绿的那条路也要有断言：只测红路，会把闸门写成恒返回非零也能"通过"。"""
    gate = _load_gate()
    _stub_side_checks(gate, monkeypatch)
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: ("v0.33.1", "ok"))
    code = gate.main(["--expect", "v0.33.1", "--no-versionname-check"])
    assert code == 0, "线上已经是目标版本时闸门必须放行（红绿都要对，只红不对的锁没用）"
    assert "verdict=PASS" in capsys.readouterr().out


def test_gate_requires_the_release_note_for_this_tag(monkeypatch, capsys):
    """发版说明必须**先于 tag 存在**：`docs/releases/<tag>.md` 是这个版本对外说了什么
    的唯一记录，事后补写等于替旧版本说话（test_release_notes.py 管的是"versionName
    涨了要有这一份"，这里管的是"这份必须跟着 tag 一起在场"）。
    """
    gate = _load_gate()
    monkeypatch.setattr(gate, "release_note_exists", lambda root, tag: False)
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: ("v0.33.1", "ok"))
    assert gate.main(["--expect", "v0.33.1", "--no-versionname-check"]) != 0
    assert "release-note-missing" in capsys.readouterr().out


def test_gate_checks_declared_versionname_against_the_tag(monkeypatch, capsys):
    """声明侧：versionName 还没涨就打上 tag，Release 名字和 APK 里的不一致。

    CI 里另有一条步骤管这件事；本机关的是"人还没推就已经知道自己错了"。
    """
    gate = _load_gate()
    _stub_side_checks(gate, monkeypatch)
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: ("v0.33.1", "ok"))
    monkeypatch.setattr(gate, "declared_version_name", lambda root: ("0.33.0", "ok"))
    code = gate.main(["--expect", "v0.33.1"])
    assert code != 0, "versionName=0.33.0 却要打 v0.33.1，闸门没拦"
    assert "versionname-mismatch" in capsys.readouterr().out


def test_workflow_runs_the_gate_before_anything_is_published():
    """CI 侧的第二道锁：闸门步骤必须存在、只在 tag 事件跑、并且排在 `gh release create`
    **之前**——排在后面就等于 Release 已经发出去了才告诉你线上不是这一版。
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "tools/pre_tag_gate.py" in text, "workflow 没调用闸门：本机的锁只有本机那一道"

    steps = re.split(r"\n\s{6}- (?:name:|uses:)", text)
    idx = [i for i, s in enumerate(steps) if "tools/pre_tag_gate.py" in s]
    assert len(idx) == 1, f"闸门步骤应当只有一处，读到 {len(idx)} 处"
    gate_step = steps[idx[0]]

    create = [i for i, s in enumerate(steps) if "gh release create" in s]
    assert create, "没在 workflow 里找到发布步骤，这条顺序判据不可信"
    assert idx[0] < create[0], "闸门在 gh release create 之后：Red 已经发出去了，拦不住"

    sign = [i for i, s in enumerate(steps) if "SIGNING KEY MISSING" in s]
    assert not sign or idx[0] < sign[0], "闸门排在签名步骤之后：红得不够早，白烧 runner"

    assert "startsWith(github.ref, 'refs/tags/')" in gate_step, \
        "闸门没有 tag 事件限定：workflow_dispatch 手动验签名链时线上本来就不该等于任何 tag"


def test_guide_tells_the_operator_to_run_it_before_tagging():
    """文档必须教人**在打 tag 之前**跑它。

    闸门只在 CI 有而文档没有，结果是本机和 CI 两套流程：人在本机照样先打后换。
    """
    text = GUIDE.read_text(encoding="utf-8")
    assert "tools/pre_tag_gate.py" in text, "部署指南里找不到闸门，本机会照旧手敲 git tag"
    i_cmd = text.index("tools/pre_tag_gate.py")
    i_tag = text.index("git tag v0.15", i_cmd - 2000 if i_cmd > 2000 else 0)
    assert i_cmd < i_tag, "指南里闸门出现在 git tag 命令之后：顺序又写反了"
