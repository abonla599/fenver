r"""换包脚本 #42 那 214 行的锁：回滚之前先把 data\tasks.json 复制走，并把判决打在屏幕上。

洞（现网事实，不是假想）：v0.35 把任务停在 `waiting_user` 状态并把断点写进 `resume_cursor`；
**已经发出去的 v0.34.2 包里**那份 `task_store.py` 用 `TaskStatus(data.get("status", ...))` 恢复
文件，而它自己的枚举只有五个成员（pending/running/completed/failed/cancelled），所以这个字面量
一进来就 ValueError；`:206-207` 打印 "skipping one unreadable task record" 把这条记录**从内存里
丢掉**，`:216-217`（或任何一次后续写入，因为 `:156-160` 是整个 dict 落盘）再把内存**写回文件**。
于是 v0.35 -> v0.34.2 的回滚不是"旧 UI 里看不见停着任务"，而是**连停点带游标一起永久抹掉**，
而且动手的是那份改不了的包。修法是运营侧的：`Save-RuntimeDataSnapshot` 在任何一次"能让包启动"
之前把 `data\tasks.json`（有就带上 `data\approvals.json`）复制进 `data\swap-snapshots\<stamp>\`，
打印 `DATA-SAVED ... moved=no`；复制失败打印 `DATA-SAVE-FAIL ...` 然后**按预检失败的方式中止**
（此时什么都没停、看守也没被按住）；读不出来的文件报 `waiting_user=unproven`，**绝不报 0**
（"读不出来"和"读到零条"是两个判决，同 `Clear-BackendBudget` 的 backend-entries=unproven）；
`Show-Rollback` 多出第 7 步，把"会被抹掉"和"往哪个方向恢复"写清楚。

本文件判五件事（一件一组，机器判的居多）：
(1) **顺序问机器**：探针把脚本**自己的** step 0b 整段 + step 1 + 它自己的 dry-run 出口抄进
    生成的 .ps1（`Get-ScheduledTask` / `Get-LiveProcess` 是桩，`Start-Process` 根本不在探针里），
    真跑一遍，判**观测到的行序**——DATA-SAVED 在 `TASK` 之前、`DRYRUN would-save-data` 在
    `DRYRUN would-start` 之前；复制失败那一档连 `TASK` 都到不了。位置锁只是它的影子；
(2) **只复制**：新代码里不许出现 Move/Rename/Remove/Set-Content/Out-File/WriteAllText，
    源与目的是两个不同路径；机器侧真跑一遍后原文件**逐字节不变、还在**，快照目录里多一份同摘要；
(3) **waiting_user 是从 JSON 读出来的**：tmp_path 下手搓 6 条记录（3 条 `waiting_user`、
    1 条大写 `WAITING_USER` 证明用的是 `-ceq`）⇒ 机器报 records=6 waiting_user=3；
    坏 JSON ⇒ `state=unparsable waiting_user=unproven`；文件不存在 ⇒ `state=absent`；
    `{"tasks": {}}` ⇒ `waiting_user=0`。**0 与 unproven 必须是两种判决**，把 unproven 换成 0
    这条就红（牙印二）；
(4) **DATA-SAVE-FAIL 之后不许继续**：句柄把文件按住 -> 这台机器打印恰好一行 DATA-SAVE-FAIL
    （带 state=unreadable waiting_user=unproven error=<真实原文>）、rc 非 0、探针末尾的
    `PROBE_TAIL` 到不了、`DATA-SAVED` 一行都没有；
(5) **ROLLBACK-HOWTO 7)** 存在、说清"抹掉"与"把 tasks.json 拷回去"的方向，而 1-6 步逐行与
    改前那份**一字不差**（对照锚点是字面 SHA `262b1224`，不是 `HEAD`——见 `_PINNED_PRE_CHANGE`）。

安全边界（票面硬约束）：所有路径都是 tmp_path 下**手搓**的假项目根；探针里不出现
`watchdog-state.json` / `data/version.json` / `data\apk` / `127.0.0.1` / `Invoke-RestMethod` /
`Start-Process` / `Stop-Process` / 写计划任务的那批 cmdlet（`test_generated_probes_...` 逐条断言）；
现网计划任务只由桩回答；生成的 .ps1 一律 ASCII + CRLF 且落在仓库外。

尺子从既有锁借（本仓规矩：一把尺子只有一份）：`_target_ps1`（`SWAP_PS1_UNDER_TEST` 通道，
它自己读 `os.environ`）、`_code_lines` / `_idx` / `_func_body` / `_ascii_ok` / `REPO_ROOT` 来自
`test_swap_package_disable_verify.py`，`_write_crlf` / `_run_powershell` 来自
`test_swap_package_post_acceptance_tokens.py`，`_find_powershell` / `PROBE_TIMEOUT_SECONDS` 来自
`test_swap_package_watchdog_enabled_machine_truth.py`。没有 powershell 就只 skip 机器那几条。

**名字漂移已收口（#61）**：机制文件头 `:121` 曾登记
`backend/tests/test_swap_package_rollback_data_snapshot.py`，而票面指定本文件叫
`test_swap_package_data_snapshot.py`；#61 已把头注释改指本文件，并由
`test_machine_read_pointers.py` 的 deploy 头注释存在性锁看住。
"""
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_swap_package_disable_verify import (
    REPO_ROOT,
    _ascii_ok,
    _code_lines,
    _func_body,
    _idx,
    _target_ps1,
)
from tests.test_swap_package_post_acceptance_tokens import _run_powershell, _write_crlf
from tests.test_swap_package_watchdog_enabled_machine_truth import (
    _find_powershell,
)

SWAP_PS1 = _target_ps1()
PS1 = SWAP_PS1.read_text(encoding="utf-8")

# 票面硬约束：锚点必须是**字面 SHA**。写成 HEAD 的那把锁在提交那一秒自毁并整体反向
# （见 test_swap_package_post_acceptance_tokens.py 的红绿分工段）。
_PINNED_PRE_CHANGE = "262b1224"

_SAVED = "DATA-SAVED mode="
_FAIL = "DATA-SAVE-FAIL mode="
_DRY_SAVE = "DRYRUN would-save-data mode="
_WARNING = "DATA-WARNING mode=rollback"
_START_LINE = "DRYRUN would-start "
_OK_LINE = "Write-Output 'DRYRUN ok (no process was stopped, no file was moved)'"
_SWAPMODE_RE = re.compile(r"^\$SwapMode = \$\(if \(\$Rollback\)")
_SAVED_RE = re.compile(r"^\s*Write-Output \('" + _SAVED)
_FAIL_RE = re.compile(r"^\s*Write-Output \('" + _FAIL)
_DRY_SAVE_RE = re.compile(r"^\s*Write-Output \('" + _DRY_SAVE)
_START_RE = re.compile(r"^\s*Write-Output \('" + _START_LINE)
_OK_RE = re.compile(r"^\s*" + re.escape(_OK_LINE) + r"\s*$")
_SNAP_CALL_RE = re.compile(r"^\s*\$snap = Save-RuntimeDataSnapshot -Dir ")
_VERDICT_CALL_RE = re.compile(r"^\s*\$taskVerdict = Get-TaskFileVerdict -TasksPath ")
_TASK_STEP_RE = re.compile(r"^\$task = @\(Get-ScheduledTask \| Where-Object")
_TASK_DONE_RE = re.compile(r"^\s*Write-Output \('TASK name-len=")
_UNPROVEN_STATES = ("unreadable", "empty-file", "unparsable", "no-tasks-key", "tasks-not-object")
_DESTROY = ("Move-Item", "Rename-Item", "Remove-Item", "Set-Content", "Out-File",
            "Clear-Content", "WriteAllText", "WriteAllBytes", "Delete(", "[System.IO.File]::Move")
_TASK_WRITES = ("Disable-ScheduledTask", "Enable-ScheduledTask", "Unregister-ScheduledTask",
                "Register-ScheduledTask", "Set-ScheduledTask", "New-ScheduledTask",
                "Start-ScheduledTask", "Stop-ScheduledTask")
_FORBIDDEN = ("watchdog-state.json", "data/version.json", "data\\version.json", "data\\apk",
              "data/apk", "127.0.0.1", "Invoke-RestMethod", "http://", "Start-Process",
              "Stop-Process", "Move-Item", "Rename-Item", "Remove-Item", "Set-Content",
              "Out-File", "WriteAllText") + _TASK_WRITES
_KEYS_SAVED = ("mode=", "dir=", "tasks=", "approvals=", "state=", "records=", "waiting_user=", "moved=no")
_KEYS_FAIL = ("mode=", "dir=", "state=", "records=", "waiting_user=", "error=")


# ------------------------------------------------------------------ 定位
def _hits(pattern, lines):
    return [i for i, ln in enumerate(lines) if pattern.search(ln)]


def _sites(text: str):
    """step 0b 的各行位置；形状不对就返回 None（判据因此对坏形状恒红，不判空集合）。"""
    lines = _code_lines(text)
    if len(lines) != len(text.splitlines()):
        return None
    sm = _hits(_SWAPMODE_RE, lines)
    saved = _hits(_SAVED_RE, lines)
    fail = _hits(_FAIL_RE, lines)
    dry = _hits(_DRY_SAVE_RE, lines)
    call = _hits(_SNAP_CALL_RE, lines)
    verdict = _hits(_VERDICT_CALL_RE, lines)
    if not (len(sm) == len(saved) == len(fail) == len(dry) == len(call) == 1):
        return None
    if len(verdict) != 1 or not (verdict[0] < call[0] < saved[0]) or not (sm[0] < dry[0] < call[0]):
        return None
    dryexit = _hits(re.compile(r"^if \(\$DryRun\.IsPresent\) \{\s*$"), lines)
    if len(dryexit) != 1 or not (dryexit[0] < dry[0] < call[0]):
        return None
    else_branch = [i for i in range(dryexit[0], call[0]) if re.match(r"^\} else \{\s*$", lines[i])]
    if len(else_branch) != 1:
        return None
    guard = [i for i in range(call[0], saved[0]) if re.match(r"^\s*if \(-not \$snap\.Ok\) \{$", lines[i])]
    if len(guard) != 1 or not (guard[0] < fail[0] < saved[0]):
        return None
    throw = [i for i in range(fail[0], saved[0]) if re.match(r"^\s*throw \(", lines[i])]
    if len(throw) != 1:
        return None
    task = _hits(_TASK_STEP_RE, lines)
    taskdone = _hits(_TASK_DONE_RE, lines)
    start = _hits(_START_RE, lines)
    okline = _hits(_OK_RE, lines)
    if not (len(task) == len(taskdone) == len(start) == len(okline) == 1):
        return None
    return {"lines": lines, "swapmode": sm[0], "dryexit": dryexit[0], "dry_save": dry[0],
            "call": call[0], "saved": saved[0], "fail": fail[0], "guard": guard[0],
            "throw": throw[0], "task": task[0], "taskdone": taskdone[0],
            "start": start[0], "okline": okline[0]}


def _function_spans(lines):
    """所有 `function NAME {` 定义块的 (起, 止) 行号（花括号配平，同 disable_verify 的算法）。

    要判"有没有哪条语句能启动包"，函数**定义体里**那几行不算——它们只在被调用时才执行，
    而调用点本身会被下面的 `Stop-AfterFailure '` 形状抓到。
    """
    spans = []
    i = 0
    while i < len(lines):
        if re.match(r"^function\s+[A-Za-z][\w-]*\s*\{\s*$", lines[i]):
            depth = 0
            j = i
            while j < len(lines):
                depth += lines[j].count("{") - lines[j].count("}")
                if depth <= 0 and j > i:
                    break
                j += 1
            spans.append((i, j))
            i = j + 1
            continue
        i += 1
    return spans


def _first_start_capable(lines):
    """能"让一个包启动"的三行形状：真的 Start-Process、dry-run 的 would-start、
    以及 Stop-AfterFailure 的**调用点**（它 pre-backupDone 那半分支会 Start-Process $LiveExe）。"""
    pats = (re.compile(r"^\s*Start-Process -FilePath \$LiveExe"), _START_RE,
            re.compile(r"^\s*Stop-AfterFailure\s+'"))
    spans = _function_spans(lines)
    hits = []
    for p in pats:
        found = [i for i in _hits(p, lines) if not any(lo <= i <= hi for lo, hi in spans)]
        if found:
            hits.append(found[0])
    return min(hits) if hits else None


def _ordering_ok(text: str) -> bool:
    """(1) 静态那一半：保存点早于**第一条**能启动包的语句，且两种模式各自都有保存/token 行。"""
    s = _sites(text)
    if s is None:
        return False
    lines = s["lines"]
    first_start = _first_start_capable(lines)
    if first_start is None or not (s["saved"] < first_start):
        return False
    if not (s["call"] < s["task"] < s["taskdone"] <= first_start):
        return False
    if not (s["dry_save"] < s["start"]):
        return False
    return s["saved"] < s["okline"]


def _copy_only_ok(text: str) -> bool:
    """(2) 静态那一半：两个 helper 里只有 File::Copy，且源/目的两个不同路径。"""
    out = {}
    for name in ("Get-TaskFileVerdict", "Save-RuntimeDataSnapshot"):
        body = _func_body(text, name)
        if body is None:
            return False
        out[name] = "\n".join(body)
    joined = out["Get-TaskFileVerdict"] + "\n" + out["Save-RuntimeDataSnapshot"]
    if any(d in joined for d in _DESTROY):
        return False
    copies = re.findall(r"\[System\.IO\.File\]::Copy\(([^)]*)\)", joined)
    if len(copies) != 2:
        return False
    for arg in copies:
        parts = [p.strip() for p in arg.split(",")]
        if len(parts) != 3 or parts[0] == parts[1]:
            return False
        if parts[0] not in ("$TasksPath", "$ApprovalsPath"):
            return False
        if not parts[1].startswith("$dest"):
            return False
    snap_dir = [ln for ln in _code_lines(text) if re.match(r"^\$DataSnapshotDir = Join-Path ", ln)]
    if len(snap_dir) != 1 or "data\\swap-snapshots" not in snap_dir[0]:
        return False
    if re.search(r"Join-Path \$StageDir[^\n]*swap-snapshots|dist[\\\\/]+run_backend_old[^\n]*swap", text):
        return False
    return True


def _verdict_honest_ok(text: str) -> bool:
    """(3) 静态那一半：计数是从 JSON 读出来的（ConvertFrom-Json + -ceq），
    每一种读不出来/形状不认识的状态都报 unproven，**没有一处**把 0 当默认值；
    文件不存在单独是一档 absent。"""
    body = _func_body(text, "Get-TaskFileVerdict")
    if body is None:
        return False
    joined = "\n".join(body)
    for need in ("ConvertFrom-Json", "-ceq 'waiting_user'", "[System.IO.File]::ReadAllText($TasksPath)",
                 "Test-Path -LiteralPath $TasksPath", '$props = @($data.tasks.PSObject.Properties)',
                 'Records = [string] $props.Count', 'WaitingUser = [string] $waiting.Count'):
        if need not in joined:
            return False
    if "-eq 'waiting_user'" in joined.replace("-ceq 'waiting_user'", ""):
        return False
    returns = re.findall(r"return \[pscustomobject\]@\{.*?\}\n", joined + "\n")
    if len(returns) < 7:
        return False
    ok_rows = [r for r in returns if "State = 'ok'" in r]
    absent_rows = [r for r in returns if "State = 'absent'" in r]
    bad_rows = [r for r in returns if any("State = '" + s + "'" in r for s in _UNPROVEN_STATES)]
    if len(ok_rows) != 1 or len(absent_rows) != 1 or len(bad_rows) != 5:
        return False
    for r in bad_rows:
        if "Records = 'unproven'" not in r or "WaitingUser = 'unproven'" not in r:
            return False
    if any("'0'" in r for r in bad_rows + absent_rows):
        return False
    for r in absent_rows:
        if "Records = 'absent'" not in r or "WaitingUser = 'absent'" not in r:
            return False
    # 打印那一行也不许把 unproven 兜成 0：DATA-SAVED / DATA-SAVE-FAIL 只转发字段
    s = _sites(text)
    if s is None:
        return False
    for idx in (s["saved"], s["fail"]):
        ln = s["lines"][idx]
        if "$taskVerdict.WaitingUser" not in ln or "$taskVerdict.Records" not in ln:
            return False
        if re.search(r"WaitingUser, '0'|\|\| '0'|\) \+ '0'", ln):
            return False
    if not all(k in s["lines"][s["saved"]] for k in _KEYS_SAVED):
        return False
    if not all(k in s["lines"][s["fail"]] for k in _KEYS_FAIL):
        return False
    if s["lines"][s["saved"]].count("(") != s["lines"][s["saved"]].count(")"):
        return False
    return True


_HOWTO_HEAD = re.compile(r"^\s*Write-Output \(?'ROLLBACK-HOWTO ([0-9a-z]+)([):])")


def _howto_map(text: str):
    """`Show-Rollback` 里 ROLLBACK-HOWTO 每一步的原文块，按步骤号（`1)`…`7)`、`ref:`）归档。

    分组靠"标题行"，续行归到它上面那一步，所以第 4 步那种跨两行的句子也整块拿到。
    """
    body = _func_body(text, "Show-Rollback")
    if body is None:
        return None
    out, cur, key = {}, None, None
    for ln in body:
        m = _HOWTO_HEAD.match(ln)
        if m:
            if cur is not None:
                out[key] = tuple(cur)
            key, cur = m.group(1) + m.group(2), [ln]
        elif cur is not None:
            cur.append(ln)
    if cur is not None:
        out[key] = tuple(cur)
    return out or None


_HOWTO_SEVEN = ("DATA-SAVED dir=", "waiting_user", "erases", "data\\tasks.json",
                "on its first flush", "stop the backend",
                "copy tasks.json back over data\\tasks.json", "then start it again",
                "DATA-SAVE-FAIL")


def _howto_ok(text: str, pre_text: str) -> bool:
    """(5)：1-6 步（含结尾那行 ref:）逐行与改前那份**完全相同**，第 7 步是新增的，
    且写明"旧包解析不了 waiting_user -> 第一次落盘就抹掉记录"与"停服务 -> 把 tasks.json
    拷回去 -> 再启动"的恢复方向。"""
    now = _howto_map(text)
    if now is None:
        return False
    for key in ("1)", "2)", "3)", "4)", "5)", "6)", "7)", "ref:"):
        if key not in now:
            return False
    if len(now["7)"]) != 1:
        return False
    line = now["7)"][0]
    for need in _HOWTO_SEVEN:
        if need not in line:
            return False
    if any("ROLLBACK-HOWTO 8)" in ln for grp in now.values() for ln in grp):
        return False
    if pre_text is None:
        return True
    pre = _howto_map(pre_text)
    if pre is None:
        return False
    for key in ("1)", "2)", "3)", "4)", "5)", "6)", "ref:"):
        if pre.get(key) != now.get(key):
            return False
    if "7)" in pre:
        return False
    return not any("DATA-SAVED" in "\n".join(pre[k]) for k in pre)


# ------------------------------------------------------------------ 生成的探针
_PROBE = r"""$ErrorActionPreference = 'Stop'
# t42 data-snapshot probe. The script's OWN step 0b + its OWN step 1 + its OWN dry-run
# exit, against a HAND-BUILT fake project root handed in on the command line. Nothing here
# can reach production: the task reader is a stub returning a fake identity, the process
# reader is a stub answering empty, and no cmdlet that starts or stops a process exists in
# this file at all. case=locked holds the fake tasks.json with FileShare=None so the
# script's own read and copy both throw, exactly as a live writer would.
$case = $args[0]
$ProjectRoot = $args[1]
$stamp = '19700101-000000'
$TasksJson = Join-Path $ProjectRoot 'data\tasks.json'
$ApprovalsJson = Join-Path $ProjectRoot 'data\approvals.json'
$DataSnapshotDir = Join-Path $ProjectRoot ('data\swap-snapshots\' + $stamp)
$Live = Join-Path $ProjectRoot 'dist\run_backend'
$LiveExe = Join-Path $Live 'run_backend.exe'
$Backup = Join-Path $ProjectRoot ('dist\run_backend_old-' + $stamp)
$Port = 1
$ExpectedVersion = 'v0.0.0-probe'
$Marker = 't42-probe-marker'
$stageMarkerLines = 1
$liveMarkerLines = 0
$liveAppJsPresent = $true
$Rollback = ($case -eq 'rollback')
$DryRun = $(if ($case -eq 'dryrun') { [pscustomobject]@{ IsPresent = $true } } else { $null })
$script:t42Holder = $null
if ($case -eq 'locked') {
    $script:t42Holder = [System.IO.File]::Open($TasksJson, 'Open', 'ReadWrite', 'None')
}
'PROBE_MODE case=' + $case
'PROBE_TASKS_PRESENT=' + (Test-Path -LiteralPath $TasksJson)
function Get-ScheduledTask {
    return @([pscustomobject]@{ TaskPath = '\t42-fake-path\'; TaskName = 't42-fake-task'
        State = 'Ready'
        Actions = @([pscustomobject]@{ Execute = 'wscript.exe'; Arguments = 't42-fake watchdog-hidden.vbs' }) })
}
function Get-LiveProcess { return @() }
__FUNCS__
__REGION__
'PROBE_TAIL'
"""


def _region(text: str, lo: int, hi: int = None):
    """step 0b -> dry-run 出口那一段**原文**（注释已剥，行号与原文对齐）。"""
    lines = _code_lines(text)
    end = len(lines) - 1 if hi is None else hi
    chunk = lines[lo:end + 1]
    return "\n".join(chunk)


def _probe_source(text: str, region: str = None):
    """拼探针并**硬校验**（任何一条不过就返回 None，绝不静默换跑"另一段更好看的代码"）。"""
    s = _sites(text)
    if s is None:
        return None
    end = s["okline"] + 2
    if end >= len(s["lines"]) or s["lines"][end].rstrip() != "}":
        return None
    body = _region(text, s["swapmode"], end) if region is None else region
    funcs = []
    for name in ("Get-TaskFileVerdict", "Save-RuntimeDataSnapshot"):
        fb = _func_body(text, name)
        if fb is None:
            return None
        funcs.append("\n".join(fb))
    if region is None:
        for needle in (_SAVED, _FAIL, _DRY_SAVE, "moved=no", "Write-Output ('TASK name-len=",
                       "DRYRUN would-start "):
            if needle not in body:
                return None
    out = _PROBE.replace("__FUNCS__", "\n".join(funcs)).replace("__REGION__", body)
    if "__" in out:
        return None
    if body not in out or not all(f in out for f in funcs):
        return None
    if not out.isascii():
        return None
    return out


# ------------------------------------------------------------------ 假根与跑法
_HAPPY = {"tasks": {
    "t1": {"id": "t1", "status": "waiting_user", "resume_cursor": "cur-1"},
    "t2": {"id": "t2", "status": "waiting_user", "resume_cursor": "cur-2"},
    "t3": {"id": "t3", "status": "waiting_user", "resume_cursor": "cur-3"},
    "t4": {"id": "t4", "status": "WAITING_USER", "resume_cursor": "cur-4"},
    "t5": {"id": "t5", "status": "pending", "resume_cursor": ""},
    "t6": {"id": "t6", "status": "completed", "resume_cursor": ""},
}}
_ROLLBACK_JSON = {"tasks": {
    "a1": {"id": "a1", "status": "waiting_user", "resume_cursor": "cur-a"},
    "a2": {"id": "a2", "status": "running", "resume_cursor": ""},
}}
_CASES = ("happy", "zero", "unparsable", "absent", "locked", "dryrun", "rollback")


def _seed(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(obj, indent=2) + "\n").encode("utf-8"))


def _build_root(work: Path, case: str):
    r"""手搓假项目根：data\ 与 dist\ 都在 tmp_path 下，真仓库一个字节都不涉及。"""
    proj = work / "proj"
    data = proj / "data"
    data.mkdir(parents=True, exist_ok=True)
    (proj / "dist").mkdir(parents=True, exist_ok=True)
    tasks = data / "tasks.json"
    approvals = data / "approvals.json"
    if case == "absent":
        pass
    elif case == "zero":
        _seed(tasks, {"tasks": {}})
    elif case == "unparsable":
        tasks.parent.mkdir(parents=True, exist_ok=True)
        tasks.write_bytes(b'{"tasks": {"t1": {"status": "waiting_user"')
    elif case == "rollback":
        _seed(tasks, _ROLLBACK_JSON)
        _seed(approvals, {"approvals": {"z1": {"status": "waiting_user"}}})
    else:
        _seed(tasks, _HAPPY)
        if case == "happy":
            _seed(approvals, {"approvals": {"z1": {"status": "waiting_user"}}})
    return proj, tasks, approvals


def _digest(path: Path):
    return None if not path.is_file() else hashlib.sha256(path.read_bytes()).hexdigest()


def _run_case(text: str, case: str, work: Path, region: str = None):
    src = _probe_source(text, region=region)
    assert src is not None, "探针源码生不出来（step 0b / 两个 helper 抄不出原文，见 _probe_source）"
    path = _write_crlf(work, "probe-t42-" + case + ".ps1", src)
    assert path.read_text(encoding="utf-8") == src, "探针没落盘：牙齿咬的是内存而不是文件"
    proj, tasks, approvals = _build_root(work, case)
    before = (_digest(tasks), _digest(approvals))
    rc, out, err = _run_powershell(path, case, str(proj))
    return {"rc": rc, "lines": [ln.strip() for ln in out.splitlines() if ln.strip()],
            "out": out, "err": err, "proj": proj, "tasks": tasks, "approvals": approvals,
            "before": before, "after": (_digest(tasks), _digest(approvals)), "src": src}


def _pick(lines, prefix):
    return [ln for ln in lines if ln.startswith(prefix)]


_POWERSHELL = _find_powershell()


def _skip_machine() -> None:
    pytest.skip("这台机器没有 powershell.exe，本文件的顺序/计数/中止三条机器判据跑不了。"
                "静态那几条照判；但「这台机器实际先打哪一行、原文件还剩不剩」是机器判的，"
                "放行复跑必须在 Windows 现网机上做（惯例同 disable_verify / post_acceptance）。")


@pytest.fixture(scope="module")
def routes(tmp_path_factory):
    """七档假根各跑一次**真** step 0b（模块共用）：happy / zero / unparsable / absent /
    locked（复制失败）/ dryrun / rollback。"""
    if _POWERSHELL is None:
        _skip_machine()
    got = {}
    for case in _CASES:
        work = tmp_path_factory.mktemp("t42-" + case)
        got[case] = _run_case(PS1, case, work)
    return got


# ------------------------------------------------------------------ 静态锁
def test_the_snapshot_step_is_locatable_and_first_among_start_capable_lines():
    """(1) 静态那一半 + 前提锁：step 0b 形状唯一、两种模式各打各的 token，
    而且**全脚本第一条能让包启动的语句**在保存点之后（连 -DryRun 的 would-start 也是）。"""
    s = _sites(PS1)
    assert s is not None, (
        "step 0b 没被唯一地定位到：$SwapMode / DRYRUN would-save-data / Get-TaskFileVerdict / "
        "$snap = Save-RuntimeDataSnapshot / DATA-SAVE-FAIL + throw / DATA-SAVED 任意一行不在、"
        "不止一处，或先后不对——本票要钉的就是这一段，定位不到等于判一个空集合")
    assert _ordering_ok(PS1), (
        "保存点不再早于第一条能启动包的语句（Start-Process $LiveExe / DRYRUN would-start / "
        "Stop-AfterFailure 调用点），或 step 0b 被搬到了 step 1 之后：Stop-AfterFailure 的 "
        "pre-backupDone 分支会 Start-Process，所以它的**第一个调用点**之前就必须有副本")
    assert s["dry_save"] < s["start"] and s["call"] < s["task"], (
        "dry-run 的 would-save-data 不在 would-start 之前，或复制调用不在 step 1 之前")
    assert not _hits(_SAVED_RE, PS1.splitlines()[:s["saved"]]), "DATA-SAVED 那一行出现了两次"
    assert _ordering_ok(PS1) and s["saved"] < s["okline"]


def test_the_snapshot_helpers_copy_only_and_write_to_data_not_dist():
    r"""(2) 静态那一半：两个 helper 里只有 [System.IO.File]::Copy（源 != 目的、目的在
    $dest* 新路径），没有 Move/Rename/Remove/Set-Content/Out-File/WriteAllText；
    快照目录钉在 data\swap-snapshots\ 而不是 dist\。"""
    assert _copy_only_ok(PS1), (
        "复制那一步不再「只复制」：出现了 _DESTROY 里的任何一种写法、Copy 的源与目的同名、"
        "或者快照目录不在 data\\swap-snapshots\\ 下。原文件是**还在应答请求的服务**正在读写的"
        "那两个文件；而 dist\\run_backend_old-* 正是「清理构建垃圾」那一刀会吃掉的位置，"
        "一份要活过回滚的副本必须放在清理的人不去的地方")


def test_the_verdict_is_json_derived_and_never_answers_zero_for_cannot_read():
    """(3) 静态那一半：计数来自 ConvertFrom-Json + `-ceq`（区分大小写），
    五种"读不出来/形状不认识"的状态一律 unproven，absent 单独一档，**没有 0 当默认值**；
    DATA-SAVED / DATA-SAVE-FAIL 两行只转发字段且键齐。"""
    assert _verdict_honest_ok(PS1), (
        "waiting_user / records 不再是「读 JSON 得来的、读不出来就 unproven」：少了 "
        "ConvertFrom-Json / -ceq / PSObject.Properties 计数，或某个非 ok 状态报了 0/absent，"
        "或打印那行把 unproven 兜成了 0——0 是唯一会让运维相信「回滚安全」的数，"
        "它只能来自真读到的零条（同 Clear-BackendBudget 的 backend-entries=unproven 那条规则）")


def test_rollback_howto_step_7_names_the_erasure_and_1_to_6_are_word_for_word_unchanged():
    """(5)：ROLLBACK-HOWTO 1)-6) 逐行与锚点 `262b1224` 那份**一字不差**，7) 是新增的，
    并且写明"旧包解析不了 waiting_user 会在第一次落盘时抹掉记录"与"停服务->把 tasks.json
    拷回去->再启动"的方向。"""
    pre = _pre_change_text()
    assert _howto_ok(PS1, pre), (
        "Show-Rollback 的 1-6 步与改前那份不再逐行相同（改配方就是改既有契约），"
        "或 7) 少了：DATA-SAVED dir= / waiting_user / erases / copy tasks.json back over "
        "data\\tasks.json / stop the backend / DATA-SAVE-FAIL 六个要素之一")


def test_generated_probes_run_the_scripts_own_region_and_cannot_touch_production():
    """接线锁 + 只读锁：探针跑的就是脚本自己的 step 0b + 两个 helper 的**原文**，
    而且它问不到现网头上——票面点名的六个禁止形状逐条断言，写任务/写进程的 cmdlet 逐条断言。"""
    src = _probe_source(PS1)
    assert src is not None, "探针生不出来（见 _probe_source 的校验）"
    s = _sites(PS1)
    body = _region(PS1, s["swapmode"], s["okline"] + 2)
    assert body in src, "探针里不是脚本 step 0b..dry-run 出口的原文"
    for name in ("Get-TaskFileVerdict", "Save-RuntimeDataSnapshot"):
        fb = "\n".join(_func_body(PS1, name) or [])
        assert fb in src, "脚本自己的 " + name + " 没有原样落到探针里"
    for needle in (_SAVED, _FAIL, _DRY_SAVE, "moved=no", "waiting_user=' + $taskVerdict.WaitingUser"):
        assert needle in src, "那一行没有原样落到探针里：" + needle
    for mark in _FORBIDDEN:
        assert mark not in src, "探针里出现了能碰到现网的形状：" + mark
    for name in _TASK_WRITES:
        assert name not in src
    assert "$args[1]" in src, "假项目根必须由命令行给，不许写死在探针源码里"
    assert src.isascii(), "生成的探针必须纯 ASCII：PS 5.1 会把带中文的无 BOM .ps1 错解码成语法错误"
    raw = SWAP_PS1.read_bytes()
    assert _ascii_ok(raw) and raw.count(b"\r\n") == raw.count(b"\n") > 0, "仓库这份已经坏了：先修它"


# ------------------------------------------------------------------ 机器锁
def test_machine_data_saved_is_observed_before_any_start_step_and_the_originals_survive(routes):
    """(1)+(2) 问机器：happy 档真跑，观测到的行序是 DATA-SAVED -> TASK -> PROBE_TAIL，
    moved=no；原文件**还在且逐字节不变**（tasks 与 approvals 都是），快照目录里多一份同摘要。"""
    got = routes["happy"]
    assert got["rc"] == 0, f"happy 档没跑完（rc={got['rc']}）：{got['err'][:300]} | {got['out'][:300]}"
    saved = _pick(got["lines"], _SAVED)
    task = _pick(got["lines"], "TASK name-len=")
    tail = _pick(got["lines"], "PROBE_TAIL")
    assert len(saved) == 1, f"DATA-SAVED 应当恰好一行：{got['out'][:400]}"
    assert _pick(got["lines"], _FAIL) == [], f"复制成功却打了失败 token：{got['lines']}"
    assert task and tail, f"没走到 step 1 / 探针末尾：{got['lines']}"
    assert got["lines"].index(saved[0]) < got["lines"].index(task[0]) < got["lines"].index(tail[0]), \
        f"观测到的行序不是 保存 -> step 1 -> 末尾：{got['lines']}"
    line = saved[0]
    for key in _KEYS_SAVED:
        assert key in line, f"DATA-SAVED 少了 {key}（运维照它决定回滚能不能救回来）：{line[:260]}"
    assert "moved=no" in line, f"只复制不搬动这件事必须自己说：{line[:200]}"
    assert "state=ok" in line and "records=6" in line and "waiting_user=3" in line, \
        f"6 条记录里 3 条 waiting_user（大写那条不算）：{line[:260]}"
    assert got["before"] == got["after"] and all(got["before"]), \
        f"原文件被动了：before={got['before']} after={got['after']}"
    assert got["tasks"].is_file() and got["approvals"].is_file(), "原文件必须还在（不许是搬走）"
    snap = Path(line.split("dir=", 1)[1].split(" ", 1)[0])
    for name, src in (("tasks.json", got["tasks"]), ("approvals.json", got["approvals"])):
        copy = snap / name
        assert copy.is_file(), f"快照目录里没有 {name}：{list(snap.iterdir()) if snap.is_dir() else snap}"
        assert copy.read_bytes() == src.read_bytes(), f"{name} 的副本与原件不同字节"
    assert "tasks=" + str(snap / "tasks.json") in line and "approvals=" + str(snap / "approvals.json") in line, \
        f"那一行必须把副本落在哪里说出来：{line[:300]}"
    assert _pick(got["lines"], _WARNING) == [], "forward 档不许打 rollback 专属的 DATA-WARNING"


def test_machine_dryrun_rehearses_would_save_data_before_would_start(routes):
    """(1) 问机器（-DryRun 那条路）：真跑 dry 档，观测到的**行序**是
    DRYRUN would-save-data 在 DRYRUN would-start 之前，且没有 DATA-SAVED（dry 不写盘）。"""
    got = routes["dryrun"]
    assert got["rc"] == 0, f"dryrun 档 rc 非 0：{got['err'][:300]} | {got['out'][:300]}"
    idx = {ln: i for i, ln in enumerate(got["lines"])}
    dry = _pick(got["lines"], _DRY_SAVE)
    start = _pick(got["lines"], _START_LINE)
    assert len(dry) == 1 and len(start) == 1, f"dry 档两行 token 各应恰好一行：{got['lines']}"
    assert idx[dry[0]] < idx[start[0]], f"would-save-data 排到了 would-start 之后：{got['lines']}"
    seq = ("DRYRUN would-save-data", "DRYRUN would-disable", "DRYRUN would-check-port",
           "DRYRUN would-backup", "DRYRUN would-swap-in", "DRYRUN would-start", "DRYRUN ok")
    seen = [got["lines"].index(ln) for ln in got["lines"] if ln.startswith(seq[:-1])]
    assert len(seen) == len(seq) - 1, f"dry-run 那几行没有全打出来：{got['lines']}"
    assert seen == sorted(seen), f"DRYRUN 计划行的观测顺序乱了：{got['lines']}"
    assert "tasks=present" in dry[0] and "mode=forward" in dry[0], f"dry 行形状不对：{dry[0][:240]}"
    assert _pick(got["lines"], _SAVED) == [] and _pick(got["lines"], _FAIL) == [], \
        "dry 档不该写真副本，也不该写真失败"
    assert got["before"] == got["after"], f"dry 档动了原文件：{got['before']} -> {got['after']}"
    assert not (got["proj"] / "data" / "swap-snapshots").exists(), "dry 档竟然建了快照目录"
    assert any(ln.startswith("DRYRUN ok") for ln in got["lines"]), \
        f"dry 档没走到脚本自己的收尾：{got['lines'][-3:]}"
    assert "PROBE_TAIL" not in got["out"], "脚本自己的 exit 0 应当在探针末尾之前就把运行收口"


def test_machine_the_waiting_user_number_comes_from_the_file(routes):
    """(3) 问机器：records / waiting_user 是真从 JSON 读出来的——6 条里 3 条停点
    （大写 WAITING_USER 不算，证明是 `-ceq`）；rollback 档另有 2 条里 1 条 + DATA-WARNING；
    approvals.json 存在时副本一起打出来。"""
    happy = routes["happy"]
    line = _pick(happy["lines"], _SAVED)[0]
    assert "records=6" in line and "waiting_user=3" in line, line[:260]
    rb = routes["rollback"]
    saved = _pick(rb["lines"], _SAVED)
    assert len(saved) == 1, f"rollback 档应打 DATA-SAVED：{rb['out'][:300]}"
    assert "mode=rollback" in saved[0] and "records=2" in saved[0] and "waiting_user=1" in saved[0], saved[0][:260]
    warn = _pick(rb["lines"], _WARNING)
    assert len(warn) == 1 and "hazard=" in warn[0] and "copies=" in warn[0], \
        f"回滚档必须单独警告这次要起的是会抹记录的包：{rb['lines']}"
    assert rb["before"] == rb["after"] and rb["before"][1] is not None, \
        "approvals.json 也必须只是被复制"


def test_machine_absent_zero_and_unproven_are_three_different_verdicts(routes):
    """(3) 问机器（票面硬约束）：`0` 与 `unproven` 与 `absent` 是三档不同判决。
    `{"tasks": {}}` -> waiting_user=0；坏 JSON -> state=unparsable waiting_user=unproven；
    文件不存在 -> state=absent。把 unproven 写成 0 的改动在这里当场红。"""
    zero = _pick(routes["zero"]["lines"], _SAVED)[0]
    unp = _pick(routes["unparsable"]["lines"], _SAVED)[0]
    absent = _pick(routes["absent"]["lines"], _SAVED)[0]
    assert "state=ok" in zero and "records=0" in zero and "waiting_user=0" in zero, zero[:240]
    assert "state=unparsable" in unp and "records=unproven" in unp and "waiting_user=unproven" in unp, unp[:240]
    assert "state=absent" in absent and "records=absent" in absent and "waiting_user=absent" in absent, absent[:240]
    assert "waiting_user=0" not in unp and "waiting_user=0" not in absent, \
        f"读不出来/没有文件却被报成 0 条：{unp[:200]} | {absent[:200]}"
    assert {zero.split("state=")[1].split(" ")[0], unp.split("state=")[1].split(" ")[0],
            absent.split("state=")[1].split(" ")[0]} == {"ok", "unparsable", "absent"}
    for case in ("unparsable", "absent"):
        assert _pick(routes[case]["lines"], _FAIL) == [], f"{case} 档只是读不出数，不该中止：{routes[case]['lines']}"
    assert routes["absent"]["before"][0] is None
    snap = Path(absent.split("dir=", 1)[1].split(" ", 1)[0])
    assert snap.is_dir() and not (snap / "tasks.json").exists(), "文件不存在时不该凭空造一份副本"
    assert "tasks=absent" in absent, absent[:240]


def test_machine_a_failed_copy_prints_data_save_fail_and_the_run_never_proceeds(routes):
    """(4)+(2) 问机器：句柄把假 tasks.json 按住 -> 复制抛 -> 这台机器**先**打印恰好一行
    DATA-SAVE-FAIL（state=unreadable、waiting_user=unproven、error=真实原文），随后按预检
    方式中止：rc 非 0、没有 DATA-SAVED、没有 step 1 的 TASK 行、探针末尾到不了——
    也就是"没有副本"这一步之后谁也走不到启动包。"""
    got = routes["locked"]
    fail = _pick(got["lines"], _FAIL)
    assert len(fail) == 1, f"DATA-SAVE-FAIL 应当恰好一行：{got['out'][:400]}"
    assert _pick(got["lines"], _SAVED) == [], f"复制失败却还声称保存了：{got['lines']}"
    assert got["rc"] != 0, f"复制失败竟然按 0 收口（rc={got['rc']}）：{got['out'][:300]}"
    assert "PROBE_TAIL" not in got["out"], f"中止之后还走到了探针末尾（= 还能继续往下跑）：{got['lines'][-3:]}"
    assert _pick(got["lines"], "TASK name-len=") == [], \
        f"失败档竟然走到了 step 1（disable 就在它后面）：{got['lines']}"
    line = fail[0]
    for key in _KEYS_FAIL:
        assert key in line, f"DATA-SAVE-FAIL 少了 {key}：{line[:240]}"
    assert "state=unreadable" in line and "waiting_user=unproven" in line, \
        f"读不出来必须报 unproven，绝不报 0：{line[:240]}"
    assert "error=" in line and line.split("error=", 1)[1].strip() != ""
    assert "Open" in line or "ReadAllText" in line or "Copy" in line, f"没有真实错误原文：{line[:240]}"
    assert got["tasks"].is_file() and got["before"] == got["after"], \
        f"复制失败也不许动原件（原件还在应答请求的服务手里）：{got['before']} -> {got['after']}"
    assert "refusing to continue" in (got["err"] + got["out"]), f"中止的理由没说：{got['err'][:300]}"


# ------------------------------------------------------------------ 牙印
def test_mutation_teeth_moving_the_copy_and_zeroing_the_unproven_both_redden(tmp_path):
    """牙印一（把复制改成搬走）与牙印二（把 unproven 换成 0）：两枚样本各造一份仓库外
    CRLF 副本，**红归对人**——另一枚样本上对方的锁照旧绿。"""
    # --- 牙印一：Copy -> Move ---
    moved = PS1.replace("            [System.IO.File]::Copy($TasksPath, $destTasks, $true)",
                        "            [System.IO.File]::Move($TasksPath, $destTasks)", 1)
    assert moved != PS1, "样本构造失败：那行 Copy 的形状与本票假设不一致"
    assert not _copy_only_ok(moved), "把复制改成搬走，只复制这条必须红"
    assert _verdict_honest_ok(moved), "这枚样本只动复制那一句，诚实性那条锁该保持绿（独立性）"
    assert _ordering_ok(moved), "这枚样本没动顺序，顺序锁该保持绿（独立性）"
    copy1 = _write_crlf(tmp_path, "swap-package-moves-instead-of-copies.ps1", moved)
    assert not _copy_only_ok(copy1.read_text(encoding="utf-8")), "落盘之后照样要红"
    if _POWERSHELL is None:
        _skip_machine()
    work = tmp_path / "move-teeth"
    work.mkdir(exist_ok=True)
    got = _run_case(moved, "happy", work, region=_region(PS1, _sites(PS1)["swapmode"],
                                                         _sites(PS1)["okline"] + 2))
    assert not got["tasks"].is_file(), "改动后那份竟然还留着原件（说明牙齿没咬到机器）"
    assert got["before"] != got["after"] and got["after"][0] is None, \
        f"机器侧原文件被搬走了却没红：before={got['before']} after={got['after']}"
    saved = _pick(got["lines"], _SAVED)
    assert saved and "moved=no" in saved[0], f"搬走之后那一行仍然自称 moved=no：{saved}"
    assert not _machine_originals_survive(got), "机器锁（原件逐字节不变）必须为红"
    # --- 牙印二：unproven -> 0（unreadable 那一档）---
    zeroed = PS1.replace(
        "return [pscustomobject]@{ State = 'unreadable'; Records = 'unproven'; WaitingUser = 'unproven'; Detail = $verdictReadError }",
        "return [pscustomobject]@{ State = 'unreadable'; Records = '0'; WaitingUser = '0'; Detail = $verdictReadError }", 1)
    assert zeroed != PS1, "样本构造失败：unreadable 那一档的形状与本票假设不一致"
    assert not _verdict_honest_ok(zeroed), "把读不出来报成 0，诚实性这条必须红"
    assert _copy_only_ok(zeroed), "这枚样本只动判决那一行，只复制那条该保持绿（独立性）"
    assert _ordering_ok(zeroed), "同上，顺序锁该保持绿"
    copy2 = _write_crlf(tmp_path, "swap-package-unreadable-answers-zero.ps1", zeroed)
    assert not _verdict_honest_ok(copy2.read_text(encoding="utf-8")), "落盘之后照样要红"
    work2 = tmp_path / "zero-teeth"
    work2.mkdir(exist_ok=True)
    got2 = _run_case(zeroed, "locked", work2, region=_region(PS1, _sites(PS1)["swapmode"],
                                                             _sites(PS1)["okline"] + 2))
    fail2 = _pick(got2["lines"], _FAIL)
    assert fail2 and "waiting_user=0" in fail2[0], \
        f"样本没生效（机器侧仍报 unproven）：{fail2}"
    assert not _machine_honest_state_line(fail2[0], "unreadable"), \
        "机器锁（非 ok 状态必须 unproven）必须为红"


# ------------------------------------------------------------------ 机器判据的裸形状版（牙印复用）
def _machine_originals_survive(got) -> bool:
    return bool(got["tasks"].is_file()) and got["before"] == got["after"]


def _machine_honest_state_line(line: str, state: str) -> bool:
    if "state=" + state not in line:
        return False
    return "waiting_user=unproven" in line and "records=unproven" in line


# ------------------------------------------------------------------ 红绿分工
def _pin_blob(ref: str):
    """`git cat-file blob <ref>:deploy/swap-package.ps1` -> (LF blob, CRLF 重建)。
    仓库根取 REPO_ROOT，**不**跟 SWAP_PS1_UNDER_TEST 走；取不到就响亮失败，绝不 skip
    （skip 会让红绿分工的证据静默消失，报告里只多出一个没人看的 1 skipped）。"""
    probe = subprocess.run(["git", "cat-file", "-e", ref + ":deploy/swap-package.ps1"],
                           cwd=str(REPO_ROOT), capture_output=True)
    assert probe.returncode == 0, (
        "锚点 " + ref + " 在这份工作树里解析不出 deploy/swap-package.ps1（cwd=" + str(REPO_ROOT) +
        "，git 说：" + (probe.stderr or b"").decode("utf-8", "replace").strip() + "）。这条锁判的就是"
        "仓库历史本身，跳过等于把证据扔掉。三查：①是不是在 checkout 里跑的；②锚点有没有被 "
        "rebase/squash/gc 换掉（`git log --oneline -- deploy/swap-package.ps1` 重挑 SHA）；"
        "③设了 SWAP_PS1_UNDER_TEST 与本锁无关，unset 再跑。")
    blob = subprocess.run(["git", "cat-file", "blob", ref + ":deploy/swap-package.ps1"],
                          cwd=str(REPO_ROOT), capture_output=True, check=True).stdout
    for token in (_SAVED, _FAIL, _DRY_SAVE, "ROLLBACK-HOWTO 7)", "Get-TaskFileVerdict",
                  "Save-RuntimeDataSnapshot"):
        assert token.encode() not in blob, (
            "锚点 " + ref + " 那份里已经有本票的形状「" + token + "」——锚点被指到了修复之后，"
            "它不是「改前那一份」。写成 HEAD 的那把锁正是在提交那一秒自毁的同一个坑。")
    return blob, blob.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


_PRE_CACHE = {}


def _pre_change_text():
    if "text" not in _PRE_CACHE:
        lf, crlf = _pin_blob(_PINNED_PRE_CHANGE)
        _PRE_CACHE.update(lf=len(lf), crlf=len(crlf), lf_crlf=lf.count(b"\r\n"),
                          lf_lf=lf.count(b"\n"), crlf_crlf=crlf.count(b"\r\n"),
                          nonascii=sum(1 for b in crlf if b >= 0x80),
                          sha=hashlib.sha256(crlf).hexdigest(), text=crlf.decode("utf-8"))
    return _PRE_CACHE["text"]


def test_pre_change_copy_at_the_pinned_sha_is_red_on_every_lock_here(tmp_path):
    """红绿分工（票面 TASK 3(i)）：CRLF 重建的 `262b1224` 那份（= 本票那 214 行之前）
    对本文件每条判据都红，既有那把尺（disable 窗口）照旧绿——顺带把 LF/CRLF 的字节数、
    CRLF 数、非 ASCII 数、sha256 当场取出来钉住，免得下一个人又拿 LF blob 喂文件自带门。
    锚点是字面 SHA 而不是 HEAD：HEAD 在提交那一秒就变成修复本身。"""
    pre = _pre_change_text()
    facts = "\n".join(f"{k}={v}" for k, v in _PRE_CACHE.items() if k != "text")
    assert _PRE_CACHE["lf_crlf"] == 0, "锚点那份 blob 竟然带 CRLF（autocrlf 前提变了）：\n" + facts
    assert _PRE_CACHE["lf_lf"] == _PRE_CACHE["crlf_crlf"], "LF 行数与 CRLF 重建对不上：\n" + facts
    assert _PRE_CACHE["nonascii"] == 0 and _PRE_CACHE["crlf"] > 0, facts
    copy = _write_crlf(tmp_path, "swap-package-pre-change-crlf.ps1", pre)
    assert copy.read_bytes().decode("utf-8") == pre, "CRLF 重建没落到盘上"
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == _PRE_CACHE["sha"], facts
    assert not _sites(pre), "改前那份竟然能定位到 step 0b：锚点指错到修复之后了\n" + facts
    assert not _ordering_ok(pre), "改前那份没有保存点，顺序锁必须红\n" + facts
    assert not _copy_only_ok(pre), "改前那份没有只复制这一步，必须红\n" + facts
    assert not _verdict_honest_ok(pre), "改前那份没有 JSON 判决，必须红\n" + facts
    assert not _howto_ok(pre, pre), "改前那份没有第 7 步，必须红\n" + facts
    assert _probe_source(pre) is None, "改前那份生不出探针（定位不到 step 0b）\n" + facts
    assert not _machine_originals_survive({"tasks": Path("nope"), "before": ("x",), "after": ("x",)}), \
        "机器裸判据对不存在的前置条件不许恒真"
    assert _sites(PS1) and _ordering_ok(PS1) and _copy_only_ok(PS1) and _verdict_honest_ok(PS1), \
        "同一批尺子在改后这份上必须绿，否则红不是来自锚点：" + facts
    from tests.test_swap_package_disable_verify import _verdict_ok, _no_bare_exit_ok, _window
    lines, lo, hi = _window(pre)
    assert lines is not None and _verdict_ok(pre) and _no_bare_exit_ok(pre), \
        "disable 窗口那两把尺在锚点那份上照旧绿：本文件不重复计同一件事\n" + facts
