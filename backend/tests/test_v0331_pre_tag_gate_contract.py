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
2. `release-apk.yml` 里的那一步 —— **这里改过，改前/改后写清楚（#90b 换窄）**：
   改前它跑的是**全量闸门**（步骤名 `LIVE BUILD NOT THIS VERSION`），而闸门有两格判据
   在 CI 的结构上根本拿不到——第 3 格要问对岸那个进程，第 5 格要问 `data/version.json`
   与 `data/apk/<版本>.apk`，两处都被 `.gitignore` 排除，checkout 里不可能有；每次 tag
   必撞 rc=1（实测撞的 token 是 `manifest-version-mismatch`，被跟踪的种子清单停在上一版），
   解不开的红灯不是闸门，是噪音（#57）。改后 CI 只留**自己真能判的那一格**：本版发版说明
   `docs/releases/<tag>.md` 在不在，仍然排在签名与发布**之前**；线上 build 与物料顺序那两格
   **只由本机闸门判**——发版人在自己机器上、敲 `git tag` 之前跑它，非零即不许往下敲。
3. 本文件 —— 锁住"闸门存在、判据 fail-closed、CI 那一格在场且顺序在 Release 之前、
   **并且那两格确实只剩本机一道闸**（移交不是删除）"。少了这一层，前两条哪天被重构掉
   都没人知道，那又变回一句提醒。

判据形状沿用本仓库对 JS/Kotlin/PS 的文本锁惯例（见 test_ops_scripts.py /
test_swap_package_contract_v0291.py），再加一组**真跑 `main()`** 的行为断言：
闸门自己如果判错方向（把"取不到"当成"通过"），文本锁是锁不出来的。
"""
import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "tools" / "pre_tag_gate.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-apk.yml"
GUIDE = REPO_ROOT / "docs" / "安装部署指南.md"

# #90b 换窄之后 CI 侧只剩的那一格，认它的步骤名前缀（步骤名就是结论本身，
# 与签名三步同一手法：未鉴权只能读到名字，读不到正文）。
CI_NARROW_CHECK = "RELEASE NOTES MISSING"
# 本机那道全量闸门的 token。它自本单起**只**该出现在 YAML 注释与文档里，
# 不许再出现在任何 `run:` 正文里——那是 #57 记的结构错位。
GATE_TOKEN = "tools/pre_tag_gate.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("pre_tag_gate", str(GATE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_side_checks(gate, monkeypatch):
    """把"与 build 判据无关的旁证"接掉，让每条断言只测它名义上那件事。

    发版说明是否存在、versionName 涨没涨，都是**另一条**判据（下面各有专门的用例）。
    留着它们，测错位的那条会先撞上"v0.33.1 的说明还没写"，红的理由和断言的理由对不上。

    #22 起多一条同级的旁证：发版物料顺序闸（清单宣告 vs 盘上的包）。它自己有专门的
    用例文件（test_v0350_release_materials_gate.py，真的跑闸门、真的拿 tmp 物料），
    这里照上面那条的同一手法接掉——否则这些用例会先撞上"v0.33.1 的包不在盘上"，
    红的理由和断言的理由又对不上了。**接掉的只是这条判据在本文件里的噪声，
    判据本身与它的顺序一个字没动。**

    泄露隔离闸（任何可推送 ref 能走到被隔离的提交就不许封版）是同一条路上的第 6 格，
    也照同一手法接掉，理由比第 5 格更硬：本文件的 `root` 是**真仓库**，而这一格判的
    恰恰是真仓库的 ref 状态——留着它，这些用例测到的就不是自己名义上那件事，而是
    "本机此刻有没有脏 ref"。它自己的牙在 test_v0350_leak_quarantine_gate.py，那里用
    pytest `tmp_path` 现造一次性仓库，红／绿／判不出来三档都真跑，不借本文件的地盘。
    这里接掉的同样是本文件的噪声，不是那条判据。
    """
    monkeypatch.setattr(gate, "release_note_exists", lambda root, tag: True)
    monkeypatch.setattr(gate, "check_announcement_materials",
                        lambda root, ver: (True, "ok", "stubbed：本用例只测 build 判据"))
    monkeypatch.setattr(gate, "check_leak_quarantine",
                        lambda root: (True, "ok", "stubbed：本用例只测 build 判据"))


def test_gate_script_exists_and_is_the_documented_shape():
    assert GATE.is_file(), "tools/pre_tag_gate.py 必须入库：闸门是脚本，不是文档里的一句话"
    src = GATE.read_text(encoding="utf-8")
    assert "argparse" in src and "--expect" in src, "闸门必须可命令行调用，CI 与本机关用同一份"
    assert "verdict=PASS" in src and "verdict=BLOCK" in src, "结论要机读，人能跳过一步不能跳过一行输出"

    # **它在跑回归的那次 CI checkout（tests.yml）里存在吗**——这条比上一条更要命。
    # `/tools/*` 在 .gitignore 里是整目录默认排除（历史上 _diag_shell_bench.py 就
    # 撞在这条上永远进不了仓库）。闸门文件如果没被 ! 例外放进来，本机测试全绿、
    # 文档照写，而 CI 每次都在本文件那句 importlib 加载上撞 "No such file or directory"
    # ——机制退化回一句提醒，而且退化得悄无声息。#90b 起 `release-apk.yml` 不再跑它，
    # 读它的是 backend/tests 这批行为锁（它们真的 exec `main()`），这条前提一个字没变。
    # 判据走 git 的真实语义（check-ignore 退出码 0 = 被忽略），不搜 .gitignore 文本。
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--stdin"],
        input=b"tools/pre_tag_gate.py\n", capture_output=True, timeout=60)
    assert res.returncode != 0, (
        "tools/pre_tag_gate.py 被 .gitignore 挡住了：tests.yml 那次 checkout 里没有这个文件，"
        "本文件这些行为锁连闸门都加载不到，闸门退化回一句提醒。加一行 !/tools/pre_tag_gate.py"
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


def _workflow_release_steps() -> list:
    """按 YAML 结构取 release 作业的步骤清单（不切原文，理由见下面第一条用例的 docstring）。"""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["release"]["steps"]
    assert steps, "release-apk.yml 里没有步骤：下面的判据全部在空转"
    return steps


def test_ci_step_is_narrowed_to_what_ci_can_actually_judge():
    """**改前钉什么**：CI 里有一步在跑全量闸门 `tools/pre_tag_gate.py`，只在 tag 事件跑，
    并且排在 `gh release create` 与签名三步**之前**。

    **改后钉什么**（#90b 换窄，两格归本机）：
    · 正面对称——CI 侧那一格换成"本版发版说明在不在"：这一步必须在场、按 tag 拼文件名、
      缺了就 `exit 1`（不许退成 warning 或恒真）、只在 tag 事件跑、顺序照旧早于签名三步与
      `gh release create`；
    · 反面对称——`run:` 正文里**不许再出现** `tools/pre_tag_gate.py`：闸门又被抄回 CI，
      就是 #57 那个"每次 tag 必撞 rc=1"的重演入口。

    **红的是哪一种回归**：这一步被删（CI 对缺本版说明重新变回静默）、被挪到发布之后
    （Release 已经公开了才告诉你正文是空的）、被改成恒真（存在性判据或 `exit 1` 没了）、
    tag 事件限定被动过（手工 dispatch 验签名链会被这一格堵死）、以及 CI 又去判本机才判得
    得了的东西。

    为什么不沿用旧写法（`re.split` 切原文 + "整份文本（含注释）里有没有 token"）：实测那把
    尺子**拦不住这一步被删**。#90b 换窄后 CI 已经不跑闸门，而注释里"从前这一步跑的是什么"
    那段历史叙述就带着 `tools/pre_tag_gate.py` 这个串——文本判据照样绿；按原文切"步"时，
    注释又跟着**上一步**走，切出来的那一格是别人的正文。判据从此只按 YAML 结构读 Actions
    真执行的东西，注释一概不算数（与 test_apk_release_discipline.py 的 `_workflow_step`、
    test_ci_history_depth.py 的"注释不参与判据"同一个口径）。

    **射程（#92a 交付项 ③：只把这句话写清楚，判据一个字没扩）**：末尾那条反向钉
    （`assert GATE_TOKEN not in runs`）读的是 `WORKFLOW` 这一个常量指着的那份文件，也就是
    **只有 `release-apk.yml` 的 `run` 正文**在射程内。把 `tools/pre_tag_gate.py` 原样抄进
    `tests.yml` 或任何别的 workflow，今天**不会红**——这是判据的边界而不是它坏了；#90b 换窄
    治的是"闸门被搬回 CI 的这一步"，抄到别处属于另一个洞。扩到全 `.github/workflows/*.yml`
    的 `run:` 集合归 **#92b**，别在本单里顺手做掉（本单只泛化浅克隆哨那一把）。
    """
    steps = _workflow_release_steps()
    names = [s.get("name") or "" for s in steps]

    hits = [i for i, n in enumerate(names) if CI_NARROW_CHECK in n]
    assert len(hits) == 1, (
        f"CI 侧的窄检查应当恰好一步，读到 {len(hits)} 处 {[names[i] for i in hits]}："
        "零步 = 这一格被删了，缺本版发版说明不再有任何东西拦；多于一处 = 判据长出第二份真相")
    at = hits[0]
    step = steps[at]

    body = step.get("run") or ""
    assert 'release_note="docs/releases/${GITHUB_REF_NAME}.md"' in body, (
        "窄检查不再按 tag 拼本版说明的文件名：这一格的判据被改写了。文件名必须跟着 tag 走"
        "（带那个 v），跟着剥过 v 的 ver 走会去找一个永远不存在的名字——那个坑见 Publish 那步")
    assert '[ ! -f "$release_note" ]' in body, (
        '存在性判据 `[ ! -f "$release_note" ]` 不在 run 正文里了（多半被改成恒真或换了形状）：'
        "这一步会永远绿，CI 对『缺本版说明』重新变回静默")
    assert "exit 1" in body, "缺说明时没停下来：响亮检查退化成一句 echo"

    assert "startsWith(github.ref, 'refs/tags/')" in (step.get("if") or ""), (
        "窄检查没有 tag 事件限定：dispatch 事件里 GITHUB_REF_NAME 是分支名，这一步会先红在"
        "签名三步之前，把『重贴完 secret 自己点一下看红不红』那条路堵死")

    create = [i for i, s in enumerate(steps) if "gh release create" in (s.get("run") or "")]
    assert create, "没在 workflow 里找到发布步骤，这条顺序判据不可信"
    assert at < create[0], "窄检查排在 gh release create 之后：Release 已经发出去了才说说明没写"

    sign = [i for i, n in enumerate(names) if "SIGNING KEY MISSING" in n]
    assert not sign or at < sign[0], "窄检查排在签名步骤之后：红得不够早，白烧 runner"

    runs = "\n".join((s.get("run") or "") for s in steps)
    assert GATE_TOKEN not in runs, (
        f"全量发版闸门又被抄回 CI 的 run 正文：{GATE_TOKEN} 在那次 checkout 里判不到世界事实"
        "（/health 是对岸进程，data/ 与 version.json 被 .gitignore 排除），每次 tag 必撞 rc=1"
        " —— #57 记的就是这件事。线上 build 与物料顺序归本机，别在这里复述")


def test_the_two_hand_off_checks_still_run_in_the_local_gate(monkeypatch, capsys):
    """移交的另一半：CI 不再判的那两格必须**仍在本地闸门里**，这里各把它跑红一次。

    **改前**不需要这一条——同一个 `main()` 在 CI 里也被跑一遍，谁把判据削掉 CI 当场红。
    **改后**（#90b）CI 只剩"本版发版说明在不在"那一格，所以这两格在整个仓库里**只剩这一道
    闸**：削掉任何一格都不再有别的东西变红，于是这里按行为钉而不是按文本钉——

    · 第 3 格：线上 `/health` 自报的 build ≠ 本次 tag → `main()` 非零 + `live-build-mismatch`。
      v0.29.0 / v0.33.0 那两次"先打 tag 后换包"就是这个形状；
    · 第 5 格：发版物料顺序（包先落盘并验过摘要、清单才许宣告这一版）→ `main()` 非零 +
      `apk-materials-<token>`。v0.34.2 那次"清单宣告跑在盘上的包前面"就是这个形状（#22）。

    **红的是哪一种回归**：有人以为"CI 会兜底"而把这两格从闸门里删掉或放成放行；也包含把
    `main()` 里的调用顺序改掉、让某一格再也轮不到执行。旁证（本版说明、versionName）按本
    文件既有手法接掉，让每条断言只测它名义上那件事。
    """
    gate = _load_gate()
    monkeypatch.setattr(gate, "release_note_exists", lambda root, tag: True)
    argv = ["--expect", "v0.35.0", "--no-versionname-check"]

    monkeypatch.setattr(gate, "check_announcement_materials",
                        lambda root, ver: (True, "ok", "stubbed：本用例只核线上那一格"))
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: ("v0.34.2", "ok"))
    assert gate.main(argv) != 0, "线上还是 v0.34.2 却要打 v0.35.0，本机闸门放行了"
    assert "live-build-mismatch" in capsys.readouterr().out

    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: ("v0.35.0", "ok"))
    monkeypatch.setattr(gate, "check_announcement_materials",
                        lambda root, ver: (False, "package-missing",
                                           "stubbed：本用例只核物料顺序那一格"))
    assert gate.main(argv) != 0, "清单已宣告这一版而盘上没有那串字节，本机闸门放行了"
    assert "apk-materials-package-missing" in capsys.readouterr().out, (
        "顺序那一格不再让闸门非零：#22 补上的那一格又被放回去了")

    src = GATE.read_text(encoding="utf-8")
    assert "live-build-mismatch" in src and "check_announcement_materials" in src, (
        "两格的判据从闸门源码里掉了——上面两条是靠 monkeypatch 跑出来的，这一条兜住『整格删掉』")


def test_guide_tells_the_operator_to_run_it_before_tagging():
    """文档必须教人**在打 tag 之前**跑它。

    改前 CI 里也跑一遍，文档写漏了还有那道闸兜着；#90b 换窄之后 CI 只核本版发版说明，
    这两格**只剩本机这一道**，而本机这一道是靠发版那个人照着指南敲的——闸门只在脚本里
    而文档没有，人在本机照样先打后换，机制又退化成一句提醒。
    """
    text = GUIDE.read_text(encoding="utf-8")
    assert "tools/pre_tag_gate.py" in text, "部署指南里找不到闸门，本机会照旧手敲 git tag"
    i_cmd = text.index("tools/pre_tag_gate.py")
    i_tag = text.index("git tag v0.15", i_cmd - 2000 if i_cmd > 2000 else 0)
    assert i_cmd < i_tag, "指南里闸门出现在 git tag 命令之后：顺序又写反了"
