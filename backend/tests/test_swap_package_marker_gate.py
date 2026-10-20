"""`-Marker` 必须是判据，不能是仪式（v0.34.2 收口第一批 item 1）。

事故形状（现网兑现过两次，最近一次就是 v0.34.1 那种「只换服务端」的版）：换包脚本只数
**暂存包**里有没有那个串，从来不数**在装机**里有没有。前端逐字节相同的时候两边都命中
（那次用的是 `/changelog`，两边各 2 次），验收照样打印一行绿的，而它证明的只是
「什么都没变」。ff85ee0f 给 v0.34.1 补了一行 `release-marker-v0.34.1` 注释——那是补
**产物**，不是补机制：下一个人照样能挑一个两边都有的串，脚本不会说一个字。

本文件钉的是那把闸的三件事：

1. **两侧计数**（`deploy/swap-package.ps1` step 0）：暂存包必须命中、在装机必须不命中，
   任一侧不对就是一条 `PRECHECK fail`。它排在停用看门狗与第一次 Stop-Process 之前——
   预检失败时生产须原封不动（那条顺序本身的锁在 test_swap_package_contract_v0291.py）。
2. **`-DryRun` 走的是同一条闸**：预检不过就 `exit 1`，所以彩排与正式判定共用同一份代码，
   不会出现「彩排绿、正式才发现」。行为层就是真跑 `-DryRun`（不停进程、不动文件）。
3. **唯一合法的「没有判据」是往后退**：`-Rollback` 必须与 `-RollbackEvidence` 同时出现，
   它把两条预检闸与换包后的验收**一起翻面**（串指的是「被退掉的那一版」），并把那句
   理由原样打进换包记录。只带 `-Rollback` 不带证据，脚本当场拒。

行为层怎么跑（`_dryrun`）：造一个假的「仓库根」（`dist\\run_backend` 当在装机、
`dist_new\\run_backend` 当暂存包，形状照 onedir 布局），把 `-ProjectRoot` 指过去，于是
脚本读的全是 tmp_path 下的东西：真 `dist\\`、真 `data\\`、真进程都不在射程里。
非 Windows 上行为层跳过、文本层照跑（惯例与理由同
test_v0340_swap_path_normalization.py:68——CI 是 ubuntu，硬断言会把锁变成 CI 红）。

工单 #63 的旁注（skip 数不是判据）：整套用例的 skipped 数跟着**本机产物**走，不跟代码走——
`data/apk/` 里有没有那一版包（test_update_channel 的种子摘要锁；同一件事另由
`tools/pre_tag_gate.py` 在打 tag 前 fail-closed 把关，见那里的「发版物料顺序」）、`chroma_db/`
在不在（test_test_isolation 的真库只读锁）、`docs-internal/` 的日程设计稿在不在
（test_schedule_ui_contract 的两条文案/色值锁）、`dist/run_backend` 构建产物在不在
（test_build_vc_runtime 的产物锁，参数化 ×3）。这四处缺的都是**被测的那个东西本身**：缺了就没有
可判的字节，跳过不等于放行。所以 14→15 这类计数漂移是本机快照差异，**单看总数不是回归**；判回归
只认「哪一条从 passed 变成 skipped」。反面形状是本文件原来的 `_dryrun_reported`：缺的 TASK 行不是
被测对象而是跑彩排的前提，skip 咽掉的是在真机器上本会执行的 DRYRUN 断言——#56 已改成红。
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# 剥注释的尺子与行号尺子都从 v0.29.1 那份契约借（本仓规矩：一把尺子只有一份）
from tests.test_swap_package_contract_v0291 import _line_of, _strip_comments

REPO_ROOT = Path(__file__).resolve().parents[2]
SWAP_PS1 = REPO_ROOT / "deploy" / "swap-package.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")

PS1 = SWAP_PS1.read_text(encoding="utf-8")
CODE = _strip_comments(PS1)

STAMP = "v9.9.9"
# 现网那份 app.js 里真有这一行（ff85ee0f 补的）。拿它当「两边都有」的那个串，
# 测的就是真发生过的那次假绿，而不是一个编出来的形状。
BOTH_SIDES = "release-marker-v0.34.1"
ONLY_NEW = "STUB-MARKER-ONLY-NEW-BUILD-9f3c"
ONLY_OLD = "STUB-MARKER-ONLY-OLD-BUILD-4a71"

LIVE_JS = "line one\n" + BOTH_SIDES + " and " + ONLY_OLD + "\n"
STAGE_JS = "line one\n" + BOTH_SIDES + " and " + ONLY_NEW + "\n"


# ------------------------------------------------------------------ 行为层脚手架
def _one_pkg(base: Path, app_js):
    static = base / "_internal" / "app" / "web" / "static"
    static.mkdir(parents=True, exist_ok=True)
    (base / "run_backend.exe").write_bytes(b"")
    (base / "_internal" / "version.txt").write_text(STAMP, encoding="utf-8")
    if app_js is not None:
        (static / "app.js").write_text(app_js, encoding="utf-8")


def _make_project(root: Path, live_js: str, stage_js: str) -> Path:
    """一个够脚本读到的假仓库根：在装机 dist\\run_backend + 暂存包 dist_new\\run_backend。

    传 None 就是「那一侧的 app.js 根本不存在」——这是形状的一部分，不是测试自己删文件。
    """
    _one_pkg(root / "dist" / "run_backend", live_js)
    _one_pkg(root / "dist_new" / "run_backend", stage_js)
    return root


def _dryrun(root: Path, marker: str, *extra: str):
    """真跑一次 `-DryRun`，返回 (退出码, 结论行, 全文)。

    `-DryRun` 在预检失败时 exit 1 并把这些行原样打出来，所以「红」也是拿得到的证据。
    """
    if os.name != "nt" or not POWERSHELL:
        pytest.skip("行为层要实跑 powershell.exe：非 Windows 上没有这个解释器，"
                    "换包脚本本身也不会在那儿跑")
    p = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SWAP_PS1),
         "-DryRun", "-ProjectRoot", str(root), "-ExpectedVersion", STAMP, "-Marker", marker,
         *extra],
        cwd=str(root), capture_output=True, timeout=180)
    # 按字节收，再 errors="replace" 解：PowerShell 5.1 把 `throw` 的整段错误横幅写到
    # **stderr**，而那个横幅里带着控制台代码页（这台机器是 GBK）的中文。用 text=True 严格
    # 按 utf-8 解会抛 UnicodeDecodeError，异常发生在 capture_output 的读线程里、被 pytest
    # 记成一条 warning，stdout/stderr 双双变成空串——于是"脚本说了什么"这条判据变成
    # "什么都没读到"（实测踩过：正对照的 rc 非 0 断言过了，内容断言拿到空串）。
    out = ((p.stdout or b"").decode("utf-8", "replace")
           + (p.stderr or b"").decode("utf-8", "replace"))
    lines = [ln.strip() for ln in out.splitlines()
             if ln.strip().startswith(("PRECHECK", "DRYRUN", "ROLLBACK-MODE", "TASK"))]
    return p.returncode, lines, out


def _fails(lines):
    return [ln for ln in lines if ln.startswith("PRECHECK fail")]


def _gate_passed(rc, lines, out, expect_contains=()):
    """「闸过了」的判据，写成一条是因为它的退出码有一个与本锁无关的分支：

    彩排在预检之后还要去找看门狗计划任务（脚本 step 1），那一步要求任务**唯一**。
    如果这台机器上那个任务被注册了两次（或还没注册），脚本会在那里 throw、退出码非 0，
    而那与"-Marker 判别不判别"没有任何关系。所以这里的判据是"预检行干净 +
    退出码为 0，或者红在闸之外的那一步"，绝不写 rc == 0 这种会把环境问题算成锁红的断言。
    """
    reds = _fails(lines)
    assert not reds, f"这条串两侧都能分辨，却被预检拦了：{reds}"
    ok = [ln for ln in lines if ln.startswith("PRECHECK ok")]
    assert ok, f"预检没有一条通过行（多半是桩包形状与脚本读法不一致）：{lines}"
    for needle in expect_contains:
        assert needle in ok[0], f"通过行少了 {needle}：{ok[0]}"
    if rc != 0:
        assert "watchdog task not uniquely found" in out, (
            "退出码非 0 又不红在「任务不唯一」那一步，红处没有解释："
            f"{lines}\n{out[:300]}")
    return ok[0]


def _dryrun_reported(lines, *needles):
    """彩排的结论行里必须出现这些片段；彩排没走到 TASK 行**红，不 skip**（工单 #56）。

    `-DryRun` 在预检之后还要去找看门狗计划任务（脚本 step 1），那一步要求任务**唯一**。
    以前这里在缺 TASK 行时 `pytest.skip`：一台没注册（或注册了两遍）守护的机器上，DRYRUN
    那几行断言整段消失，报告里只多一个没人看的 "1 skipped"。但缺的 TASK 行不是被测对象
    （被测对象是换包脚本那把闸），而是这台机器能跑彩排的前提；这台机器又恰是唯一会跑正式
    换包的那台（AGENTS.md：开机自启与崩溃守护就是计划任务）。前提没满足时该红在前提上，
    不是把问题当成没问过。

    两侧计数那条闸仍由 `PRECHECK ok` 行证明（那是别的用例的判据，这里不放宽也不重复）；
    `_gate_passed` 对「任务不唯一」那把退出码的容忍照旧——兄弟计数锁一个字不动。
    """
    if not any("TASK name-len=" in ln for ln in lines):
        pytest.fail(
            "彩排没有走到 `TASK name-len=` 那一行，所以 DRYRUN 的结论行一条都没打出来——"
            "这条锁要判的东西根本没跑。两种形状：换包脚本 step 1 按 action 找到的看门狗任务"
            "不是恰好 1 个（0 个=守护没注册，2 个以上=重复注册），或者预检就没过（那会先在"
            " PRECHECK fail 上红）。先跑 `Get-ScheduledTask | Where-Object { $_.Actions.Execute "
            "-eq 'wscript.exe' -and $_.Actions.Arguments -like '*watchdog-hidden.vbs*' }` 看"
            "个数，修回唯一一个再跑。这里不再用 skip 把 DRYRUN 断言静默咽掉（#56）。"
            "拿到的行：" + repr(lines))
    for needle in needles:
        assert any(needle in ln for ln in lines), f"彩排行里少了 {needle}：{lines}"


# ------------------------------------------------------------------ 1. 两侧计数
def test_marker_present_on_both_sides_is_a_precheck_failure(tmp_path):
    """就是那两次假绿的形状：串在暂存包有、在在装机也有 ⇒ 预检必须点名判死。"""
    root = _make_project(tmp_path, live_js=LIVE_JS, stage_js=STAGE_JS)
    rc, lines, _ = _dryrun(root, BOTH_SIDES)
    reds = "\n".join(_fails(lines))
    assert rc != 0, f"两边都命中却被放行了（这就是那两次假绿）：{lines}"
    assert "live-appjs-marker=present" in reds, (
        f"没有一条指着在装机那一侧的红：{reds}\n"
        "期望 PRECHECK fail live-appjs-marker=present count=N expected=0 marker=...")
    assert "expected=0" in reds and "marker=" + BOTH_SIDES in reds, (
        f"那条红没把判据写全（count / expected / marker 三者都要在，运维才知道该换哪个串）：{reds}")
    assert not any("DRYRUN ok" in ln for ln in lines), f"预检判死了彩排还说 ok：{lines}"
    assert any("DRYRUN precheck-failures=" in ln for ln in lines), \
        f"预检失败没有留下可机读的行：{lines}"


def test_marker_only_in_the_new_package_passes_the_gate(tmp_path):
    """正对照：只在新包里的串必须放行——否则这条闸只是另一台「永远拦」的机器。"""
    root = _make_project(tmp_path, live_js=LIVE_JS, stage_js=STAGE_JS)
    rc, lines, out = _dryrun(root, ONLY_NEW)
    # 两侧计数都要出现在通过行里：只报一边，就等于把这条闸退回它刚被修掉的那一半
    ok = _gate_passed(rc, lines, out,
                      expect_contains=("marker-lines=1", "live-marker-lines=0",
                                       "marker-mode=forward"))
    _dryrun_reported(lines, "DRYRUN marker-gate mode=forward", "live-lines=0", "expect=present")
    assert ok


def test_missing_live_appjs_is_a_failure_not_a_vacuous_pass(tmp_path):
    """在装机读不到 app.js 时不许「因为读不到所以零命中所以通过」——那是第三种假绿。"""
    root = _make_project(tmp_path, live_js=None, stage_js=STAGE_JS)
    rc, lines, _ = _dryrun(root, ONLY_NEW)
    reds = _fails(lines)
    assert rc != 0, f"在装机的 app.js 不存在却被放行：{lines}"
    assert any("live-appjs=missing" in ln for ln in reds), \
        f"没点名是在装机那一侧读不到：{reds}"


# ------------------------------------------------------------------ 2. 唯一合法的例外
def test_rollback_needs_stated_evidence_and_refuses_without_it(tmp_path):
    """`-Rollback` 不许被顺手打出来：没有证据串就当场拒，并把「要什么证据」说清楚。"""
    root = _make_project(tmp_path, live_js=LIVE_JS, stage_js=STAGE_JS)
    rc, _, out = _dryrun(root, ONLY_OLD, "-Rollback")
    assert rc != 0, f"只带 -Rollback 就翻了面（证据都不用写）：{out[:400]}"
    assert "RollbackEvidence" in out, f"拒绝的理由没说要什么证据：{out[:400]}"


def test_rollback_flips_the_gate_and_still_demands_discrimination(tmp_path):
    """往后退：串指「被退掉的那一版」⇒ 必须在装机有、在暂存包没有，两侧仍须能分辨。"""
    root = _make_project(tmp_path, live_js=LIVE_JS, stage_js=STAGE_JS)
    # 正确方向：装机有 ONLY_OLD、要换上身的旧包没有它
    rc, lines, out = _dryrun(root, ONLY_OLD, "-Rollback",
                             "-RollbackEvidence", "v9.9.9 broke the tunnel, back to previous build")
    _gate_passed(rc, lines, out,
                 expect_contains=("marker-mode=rollback", "marker-lines=0",
                                  "live-marker-lines=1"))
    assert any(ln.startswith("ROLLBACK-MODE") and "broke the tunnel" in ln for ln in lines), \
        f"证据没被打进换包记录（记录里只看得见判决、看不见那句人话）：{lines}"
    _dryrun_reported(lines, "DRYRUN marker-gate mode=rollback", "live-lines=1",
                     "DRYRUN would-accept", "expect=absent")

    # 错方向一：拿新包的串说回滚（在装机里没有它）
    rc2, lines2, _ = _dryrun(root, ONLY_NEW, "-Rollback", "-RollbackEvidence", "whatever")
    assert rc2 != 0 and any("rollback-live-appjs-marker=absent" in ln for ln in lines2), \
        f"回滚方向搞反却没被点名：{lines2}"

    # 错方向二：串两边都有（回滚也不许绕过两侧计数）
    rc3, lines3, _ = _dryrun(root, BOTH_SIDES, "-Rollback", "-RollbackEvidence", "whatever")
    assert rc3 != 0 and any("rollback-stage-appjs-marker=present" in ln for ln in lines3), \
        f"往后退时用了一个两边都有的串却被放行：{lines3}"


# ------------------------------------------------------------------ 3. 文本锁 + 牙印
# 行为层在非 Windows 上会 skip，所以下面这几条是那条闸在 CI 里唯一的存在证明：
# 判「闸在不在」必须真判可执行部分，判「排在哪」必须真用行号，不许写成一句 `in 全文`。
GATE_ABSENT = r"live-appjs-marker=present"
GATE_COUNT = r"\$liveMarkerLines\s*=\s*@\(\s*Select-String\s+-LiteralPath\s+\$LiveAppJs"
# 那条判死是一整句三行（`if … {` / 一行 Add-Fail / `}`）。切它必须 `(?ms)`：
# 只有 m 的话 `.` 不能跨行，`[ ]*\}` 就会去要求 Add-Fail 那一行是个花括号，整条正则
# 在真文件上一条都不匹配 —— 症状是"变异样本没生效"，比红更难查。
LIVE_GATE_STMT = (r"(?ms)^[ ]*if \(\$liveAppJsPresent -and \$liveMarkerLines -ge 1\)"
                  r" \{.*?\n[ ]*\}\n")


def _marker_gate_ok(text: str) -> bool:
    """这条闸的判据本体：两侧都数、两侧判据都在、且全部排在任何「动生产」的动作之前。"""
    code = _strip_comments(text)
    try:
        i_live_count = _line_of(GATE_COUNT, text)
        i_absent_gate = _line_of(GATE_ABSENT, text)
        i_stage_gate = _line_of(r"stage-appjs-marker=absent", text)
        i_disable = _line_of(r"^Disable-ScheduledTask -TaskPath", text)
        i_stop = _line_of(r"Stop-Process -Id \$p\.ProcessId -Force", text)
        i_precheck_ok = _line_of(r"PRECHECK ok", text)
        i_dryrun = _line_of(r"^if \(\$DryRun\)", text)
    except AssertionError:
        return False
    return (
        # 在装机那一侧真的去数了（读的是 $LiveAppJs，不是 $StageAppJs）
        i_live_count < i_absent_gate
        # 老那条「暂存包必须有」还在（只加不减，否则正向换包丢了验收）
        and i_stage_gate < i_precheck_ok
        # 两侧判据都先于预检通过行，因而也先于停用看门狗 / 停进程 / -DryRun 出口
        and i_absent_gate < i_precheck_ok
        and i_absent_gate < i_disable and i_absent_gate < i_stop
        and i_absent_gate < i_dryrun
        # 两侧计数吃的是同一个 -Marker：两个不同串就不是「判别」，是两次无关的搜索
        and bool(re.search(r"Select-String -LiteralPath \$LiveAppJs -Pattern \$Marker -SimpleMatch",
                           code))
        and bool(re.search(r"Select-String -LiteralPath \$StageAppJs -Pattern \$Marker -SimpleMatch",
                           code)))


def test_the_live_side_absence_gate_is_in_the_precheck():
    assert _marker_gate_ok(PS1), (
        "两侧计数这条闸不在了（或排到了动生产之后）：换包又会回到「只数暂存包」的仪式——"
        "v0.34.1 那次 -Marker 两边各 2 命中却全绿的假绿就是这么来的")
    assert re.search(r"PRECHECK fail live-appjs=missing", CODE), \
        "在装机读不到 app.js 必须是一条 fail，不许「读不到所以判通过」"
    assert re.search(r"-Rollback requires -RollbackEvidence", CODE), \
        "-Rollback 的证据要求没了：往后退这条路就变成谁都能顺手打的开关"
    assert re.search(r"\$LiveAppJs\s*=\s*Join-Path\s+\$Live", CODE), \
        "在装机那一侧的 app.js 路径必须是 $Live 拼出来的，不是又抄一份常量"


def test_dryrun_path_reports_the_gate_it_just_passed():
    """彩排不许只报「我会搬哪些目录」：它得把刚过的那条闸的结果原样打出来。"""
    at = _line_of(r"^if \(\$DryRun\)", PS1)
    body = "\n".join(PS1.splitlines()[at:])
    assert re.search(r"DRYRUN marker-gate mode=", body), "-DryRun 的打印里没有这条闸的结果"
    assert "$stageMarkerLines" in body and "$liveMarkerLines" in body, \
        "彩排行没带两侧计数（彩排绿而正式才发现 = 同一条闸的两份口径）"
    assert re.search(r"DRYRUN would-accept .*expect=", body), \
        "-DryRun 没报出验收的方向（正向要有 / 回滚要无），彩排就看不出自己在验什么"
    assert "DRYRUN ok" in body, "-DryRun 的收尾行不见了，行为层正对照的前提没了"


def test_acceptance_inverts_only_in_rollback_mode():
    """换包后的验收也跟着翻面，但正向那条判据一个字不许少。"""
    after_swap = PS1[PS1.index("Write-Output 'SWAPPED'"):]
    assert re.search(r"\$markerLines -lt 1", after_swap), \
        "正向验收（在被服务的 app.js 必须命中）不见了：那才是「新代码确实在被服务」"
    assert re.search(r"if \(\$Rollback\)[\s\S]*?\$markerLines -ge 1", after_swap), \
        "回滚方向没有反向验收：换上旧包之后还要求新包才有的串，等于回滚必红"
    assert re.search(r"ACCEPT marker-lines=", after_swap), "验收结论仍须可机读"


def test_mutation_removing_the_live_side_gate_turns_the_lock_red():
    """自证这条锁真会红：只在内存里删掉在装机那一侧那一整句判死。

    行为层在非 Windows 上会 skip，所以这里留一个平台无关的牙印（写法同
    test_swap_package_all_or_nothing_move 最后那条：变异样本必须**真的生效**，
    否则这条负向锁形同虚设）。真删文件跑一遍红/绿的取证记在提交说明里。
    """
    assert _marker_gate_ok(PS1), "真文件必须是绿的，否则下面的变异证明不了任何事"

    mutated = re.sub(LIVE_GATE_STMT, "", PS1, count=1)
    assert mutated != PS1, "变异样本没生效（那条闸的形状与 LIVE_GATE_STMT 不一致了）"
    assert "live-appjs-marker=present" not in mutated, \
        "变异没把判死那一整句删掉，样本还是在场的那条闸"
    assert not _marker_gate_ok(mutated), \
        "把在装机那一侧的判死摘掉，这条锁必须红——它就是为这一次而写的"

    # 变异二：两侧改成读同一个文件（只剩一边计数，回到仪式）
    same_side = PS1.replace("-LiteralPath $LiveAppJs -Pattern $Marker",
                            "-LiteralPath $StageAppJs -Pattern $Marker")
    assert same_side != PS1, "变异二没生效：$LiveAppJs 那一次 Select-String 的形状变了"
    assert not _marker_gate_ok(same_side), \
        "两侧都去数暂存包时锁仍绿：那说明判据没真的看在装机那一侧"

    # 变异三：判据一字不改，只把它搬到停用看门狗**之后**（发现了也已经把生产停了）
    m = re.search(LIVE_GATE_STMT, PS1)
    assert m, "先决条件：那条闸必须是「一句可整段切出来」的形状，否则这个样本无从构造"
    anchor = "Disable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null"
    assert PS1.count(anchor) == 1, "变异三的锚点不唯一，搬完可能插错了地方"
    moved = PS1.replace(m.group(0), "", 1).replace(anchor, anchor + "\n" + m.group(0), 1)
    assert m.group(0) in moved, "变异三没生效：搬走之后没能插回锚点之后"
    assert not _marker_gate_ok(moved), \
        "把两侧计数挪到停用看门狗之后，锁必须红：预检失败时生产须原封不动，位置就是判据的一部分"


# ------------------------------------------------------------------ 4. 缺 TASK 行必须红（#56 反例）
def test_a_rehearsal_that_never_reached_the_task_line_is_a_loud_failure():
    """反例：彩排停在任务查找那一步时，`_dryrun_reported` 必须红，不许 skip。

    这就是 #56 那个洞的形状：缺 TASK 行时它只 skip，DRYRUN 那几行的断言整段消失，报告里
    多一个 "1 skipped"，和"这台机器上根本没跑过这条判据"在报告里长得一模一样。这里造的是
    当年触发 skip 的那个线集（有 `PRECHECK ok`、没有 `TASK`），证明现在它红，而且红在
    "任务查找"这个原因上。

    抓 `BaseException` 是有意的：`pytest.skip` 抛的 `Skipped` 也是 `BaseException`。万一哪天
    被改回 skip，这条反例会**红**，而不是自己被记成 skipped、再从报告里消失一次。
    """
    lines_without_task = [
        "PRECHECK ok stage-version=v9.9.9 marker-lines=1 live-marker-lines=0 "
        "live-appjs=present marker-mode=forward",
    ]
    raised = None
    try:
        _dryrun_reported(lines_without_task, "DRYRUN ok")
    except BaseException as exc:          # 见 docstring：Skipped 也要抓得住
        raised = exc
    assert raised is not None, (
        "缺 TASK 行的线集进来，_dryrun_reported 一声不吭地返回了：DRYRUN 断言被放过，"
        "报告里只会多一个没人看的 skipped")
    assert not isinstance(raised, pytest.skip.Exception), (
        "缺 TASK 行时仍在 pytest.skip：这台机器上 DRYRUN 那几行断言整段消失，而报告看起来"
        "和'根本没有这条判据'完全一样——#56 要堵的就是这个")
    assert isinstance(raised, pytest.fail.Exception), (
        f"红的方式不对（{type(raised).__name__}）：要的是 pytest.fail 那种点名原因的 loud "
        f"failure，不是后面断言顺手撞上的红：{raised}")
    msg = str(raised)
    assert "TASK" in msg and "watchdog" in msg, (
        f"红了但没说清是任务查找那一步停的，运维从这条红里读不出该修什么：{msg}")

    # 正对照：TASK 行在场时同一个函数必须不红，否则"永远红"不是锁
    _dryrun_reported(["TASK name-len=12 state=Ready",
                      "DRYRUN ok (no process was stopped, no file was moved)"], "DRYRUN ok")
