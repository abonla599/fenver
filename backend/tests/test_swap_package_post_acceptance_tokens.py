r"""换包脚本**验收之后那段尾巴**的两处裸出口：step 8 的确认读 + step 9 的还预算（ticket #47）。

洞在哪（一句话版）：`deploy/swap-package.ps1` 在 `ACCEPT marker-lines=` 通过、看守也**已经**
被这次运行扶回去**之后**，还剩两处没登记的死法——

  SITE 1：`Clear-BackendBudget` 里 `[System.IO.File]::ReadAllBytes($StatePath)` 与
  `[System.IO.File]::WriteAllText($StatePath, $json, $enc)` **都不在 try 里**，而这个函数是在
  主流程最后一行附近**裸调**的（就在 `Write-Output 'SWAP-DONE'` 前一行人读的日志）。它吃的路径
  是 `data\watchdog-state.json`——本仓库自己的注释就写着"看门狗可能正在写这个文件"。这不是假设：
  `deploy/watchdog.ps1:585` 每一轮巡检都调 `Save-State`，巡检一分钟一次
  （`register-watchdog.ps1:145` RepetitionInterval=1min），本机 2026-10-17 实测那份账的 mtime
  09:12:05 -> 09:13:05 -> 09:14:05（delta 60/59 秒，799 字节不变）。撤掉 try 之后这台机器当场
  演示（`test_mutation_unwrapping_the_budget_read_...`）：抛的是
  `Exception calling "ReadAllBytes" with "1" argument(s): The process cannot access the file
  ... because it is being used by another process`，rc=1，**没有 BUDGET、没有 SWAP-DONE**，
  看守这一小时的重启额度还留在打空状态。

  SITE 2：`$finalState = (Get-ScheduledTask -TaskPath ... -TaskName ...).State`——紧接在
  **成功**的 re-enable 之后的一次裸读。这台机器对**手搓假身份**（`\t47-fake-path\` /
  `t47-fake-task`，绝不问现网那个任务）的答案是
  `exception=Microsoft.PowerShell.Cmdletization.Cim.CimJobException`、`bare_state=NOT-REACHED`
  （见 `test_machine_the_bare_task_read_of_a_fake_identity_is_a_real_throw`），所以
  `$ErrorActionPreference='Stop'` 下它一抛，这次运行就没有 `ENABLED`、没有 `BUDGET`、没有
  `SWAP-DONE`：包已经换好、看守已经回来了，日志尾巴上却什么都读不出来。

本文件钉四件事（票面四条判据，一条对应一组函数）：
(a) 两处文件调用**各在自己的 try 里**，catch 把 `$_` 第一时间转成具名变量、打完本票 token
    之后收尾是 `return`（**不是**裸 throw / `exit`，**更不是** `Stop-AfterFailure`）；SITE 2
    必须**复用**既有 helper `Get-WatchdogTaskState`（它自己接住失败并把结果当字段带回来），
    而不是在尾巴上再生出第二把读任务的尺子；
(b) **路由由这台机器判**（票面明令不许只有文本锁）：探针把脚本**自己的**函数体 / step 8 整段
    抄进生成的 .ps1，状态文件换成 tmp_path 下的**手搓假账**，现场回答"读被挡住 -> 一行
    `BUDGET state=read-threw` + 照样走到 `SWAP-DONE`"、"写得回去 -> 照旧清账、别的键不动、
    BOM 保住"、"读答不了 -> `READFAIL stage=final-enabled-read` + `ENABLED state=Unreadable`
    并继续"；
(c) 新 token 存在、形状是一行一个、`state=` 沿用 `BUDGET` 既有那一族（absent/empty-file/
    unparsable 之外多 read-threw / write-threw 两档），且文件头 `:15-70` 那张 token 登记表
    **登记**了它们与"验收之后退化不止步"这条规则；
(d) 摘掉这层保护就必须红：撤读的 wrap、撤写的 wrap、把退化改成中止、尾巴退回裸读、
    token 退回只说"这次没做成"的散文、登记表抹掉、LF 副本、非 ASCII 副本——每种都有独立样本，
    并且**红要归对人**（只动写的话读那半的锁必须照旧绿，反之亦然）。

**为什么两处都是退化而不是中止**（票面第 1 条要求把论证写在站点注释里，这里记锁的那一侧）：
走到这两行时包已验收、`$script:backupDone` 为真、`$script:watchdogDisabled` 也已经被成功启用
那一句置回假。这时 `Stop-AfterFailure` 走的是 `moved=yes` 那条分支，打印的是**字面量**
`watchdog=left-disabled-by-design` 加整套 ROLLBACK 配方——此刻看守明明已经回来，那句话就是假话，
配方还会教人在凌晨三点拆掉一个健康服务。所以本文件把"这两站点不许出现 `Stop-AfterFailure`"
钉死（`_no_stop_route_anywhere` 判**整个函数体**与尾巴整段，不只 catch 前几行），并由
`_restore_ok` 那把公共尺同时证明被禁的那条出口函数**本体**没坏——这里的红只能来自"这两站点用了它"。
`test_machine_the_post_acceptance_abort_would_print_a_lie` 再用这台机器演示一遍：抄脚本**真的**
`Stop-AfterFailure`，在 step 8 走完之后的真状态下调用它，念出来的正是那句字面量假话，而
`Restore-WatchdogTask` 一次都没被调用（开关只有一个主人）。

安全边界（票面硬约束，探针必须配得上）：对现网只读。假账一律是 tmp_path 下**手搓**的路径
（接线锁断言路径由命令行给、`_REAL_PATH_MARK` 在探针源码里根本不出现），探针**从不**打开真的
`data\watchdog-state.json`；**不**动计划任务（SITE 2 用手搓假身份 + 桩掉的 `Get-ScheduledTask`
与 `Enable-ScheduledTask`，写任务的那批 cmdlet 名逐条断言不在探针源码里），**不**碰
`127.0.0.1:8000`（探针里没有 `Invoke-RestMethod` / `127.0.0.1` / `http://`）；**不**碰 `dist\`。
只有一档 `live-identity` 探针**只读**问真任务的属性（票面允许 read-only `Get-ScheduledTask`）。
生成的 .ps1 一律 ASCII + CRLF，跑在 `tmp_path`（仓库外）。牙印样本一律落**仓库外**的副本
（`_write_crlf`），并经 `test_under_test_copy_runs_the_whole_file_locks` 在落盘副本上复跑整文件。

红绿分工（为什么本文件必须存在，而不是往旧锁里加一条 risky）：
`test_pre_fix_reconstruction_is_green_on_the_existing_locks_and_red_on_ours` 用 `git cat-file`
取 `HEAD` 那份 blob、**按 CRLF 重建**再判。本机 `core.autocrlf=true`，`git show HEAD:deploy/...`
给的是 **LF** blob：HEAD 那份实测 63590 字节 / CRLF 0 / LF 1055 / 非 ASCII 0 /
sha256 `a6772ba1cc9a4048abeb1001077fd0c62f1beba7fca0234a35a9bea823cf65a0`；换成 CRLF 之后
64645 字节 / CRLF 1055 / LF 1055 / 非 ASCII 0 / sha256
`59b68cb4a594322b0c3f3f6b9c458fde1a91b6c449c3698fd65272c5ad07e835`，与工作树那份逐字节相同。
**票面写的 60226 不是 HEAD 的 blob**——那是 `HEAD~1`（#45 落地之前那一版）的 LF blob 大小，
本条把三个数都当场取出来放进断言消息，免得下一个人又按 60226 去找。
**另注意 `_no_bare_exit_ok` 绿不等于它扫过尾巴**：那把尺的射程是 disable 到
`$script:backupDone` 那一段，本票两处都在它之后——这台机器用 `Get-Content -Tail 40` 现场念一遍
改前那份的尾巴（裸读与收尾都还在原位），红绿分工才算有据。

尺子从既有锁借（本仓规矩：一把尺子只有一份）：`_code_lines` / `_idx` / `_try_spans` /
`_span_of` / `_func_body` / `_window` / `_target_ps1` / `_ascii_ok` / `_restore_ok` /
`_verdict_ok` / `_no_bare_exit_ok` / `_single_value_helpers_ok` 来自
`test_swap_package_disable_verify.py`（`_restore_ok` 就是"`Stop-AfterFailure` 两条分支各打印
什么"那把尺，本票"不许中止"那一条正是靠它兜住被禁函数的本体），`_block_end` / `_boot_region`
来自 `test_swap_package_boot_poll_threw_handler.py`（本票用它定位"SITE 2 在起进程轮询之后"，
不另刻一把），`_find_powershell` / `_no_write_cmdlets` / `PROBE_TIMEOUT_SECONDS` 来自
`test_swap_package_watchdog_enabled_machine_truth.py`，`_try_wraps_and_routes` /
`_handler_asks_the_machine` / `_branches_and_failsafe` 来自
`test_swap_package_disable_threw_handler.py`，`_enabled_read_site` / `_read_site_ok` 来自
`test_swap_package_watchdog_enabled_machine_truth.py`（后面这些只做**分工证据**：
改前那份对它们照旧绿）。

**不与 #51 抢锚点**：本文件所有定位只看 (1) `function Clear-BackendBudget` 的函数体、
(2) `$finalProbe = Get-WatchdogTaskState -TaskToRead $task` / `Write-Output ('ENABLED state='`
/ `Write-Output 'SWAP-DONE'`、(3) 顶格的 `    Clear-BackendBudget -StatePath`。
#51 那枚 `(Get-LiveProcess).Count -eq 0` 文本锚点在本文件**一次都不出现**（借来的
`_boot_region` 用的是 `$livePid = 0` / `while ((Get-Date) -lt $bootDeadline) {` /
`$running = Get-LiveProcess` / `STARTED pid=` 四个顶格锚点，只看轮询那一段，不看
`Stop-AfterFailure` 里的重启 if），所以 #51 把那处改成 `@(Get-LiveProcess).Count` 不会移动
本文件任何锚点；本文件也不碰 `test_swap_package_disable_verify.py:341` 那条判据的射程
（`$restore = Restore-WatchdogTask` 的缩进层级）。

没有 `powershell.exe` 就 skip 机器那几条，静态那几条照跑照判（惯例同上面几份锁）。
"""
import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from tests.test_swap_package_boot_poll_threw_handler import _block_end, _boot_region
from tests.test_swap_package_disable_threw_handler import (
    _branches_and_failsafe,
    _handler_asks_the_machine,
    _try_wraps_and_routes,
)
from tests.test_swap_package_disable_verify import (
    _ascii_ok,
    _code_lines,
    _func_body,
    _idx,
    _no_bare_exit_ok,
    _restore_ok,
    _single_value_helpers_ok,
    _span_of,
    _target_ps1,
    _try_spans,
    _verdict_ok,
    _window,
)
from tests.test_swap_package_watchdog_enabled_machine_truth import (
    PROBE_TIMEOUT_SECONDS,
    _enabled_read_site,
    _find_powershell,
    _no_write_cmdlets,
    _read_site_ok,
)

SWAP_PS1 = _target_ps1()
PS1 = SWAP_PS1.read_text(encoding="utf-8")

# ------------------------------------------------------------------ 本票认下来的 token
_READ_TOKEN = "BUDGET state=read-threw"
_WRITE_TOKEN = "BUDGET state=write-threw"
_FAIL_TOKENS = (_READ_TOKEN, _WRITE_TOKEN)
_FAIL_KEYS = ("budget-returned=false", "backend-entries=", "error=")
_SUCCESS_TOKEN = "BUDGET backend-entries-cleared="
_ENABLED_READ_STAGE = "final-enabled-read"
_READFAIL_ENABLED = "READFAIL stage=" + _ENABLED_READ_STAGE
_UNREADABLE = "Unreadable"
_PROBE_MARK = "t47-fake-state.json"
_FAKE_TASK_PATH = "\\t47-fake-path\\"
_FAKE_TASK_NAME = "t47-fake-task"
_REAL_PATH_MARK = "watchdog-state.json"
_NETWORK_MARKS = ("Invoke-RestMethod", "127.0.0.1", "http://")
_TASK_WRITES = ("Disable-ScheduledTask", "Unregister-ScheduledTask", "Register-ScheduledTask",
                "Set-ScheduledTask", "New-ScheduledTask", "Start-ScheduledTask",
                "Stop-ScheduledTask")

_TRY_OPEN = re.compile(r"^\s*try\s*\{\s*$")
_CATCH_OPEN = re.compile(r"^\s*\}\s*catch\s*\{\s*$")
_RETURN = re.compile(r"^\s*return\s*$")
_BARE_EXIT = re.compile(r"^\s*(throw|exit)\b")
_NULL_INIT = re.compile(r"^\s*\$\w+ = \$null\s*$")
_CAPTURE_ERR = re.compile(r"^\s*(?P<var>\$\w+) = \$_\.Exception\.Message\s*$")
_READ_STMT = re.compile(r"\$bytes = \[System\.IO\.File\]::ReadAllBytes\(\$StatePath\)\s*$")
_WRITE_STMT = re.compile(r"\[System\.IO\.File\]::WriteAllText\(\$StatePath, \$json, \$enc\)\s*$")
_FILE_TOUCH = re.compile(r"\[System\.IO\.[A-Za-z]+\]::")
_HELPER_READ = re.compile(r"^\$finalProbe = Get-WatchdogTaskState -TaskToRead \$task$")
_ENABLED_TOKEN_LOOSE = re.compile(r"^Write-Output \('ENABLED state='")
_ENABLED_TOKEN = re.compile(r"^Write-Output \('ENABLED state=' \+ \$finalProbe\.State\)$")
_READFAIL_LINE = re.compile(r"^\s*Write-Output \('" + re.escape(_READFAIL_ENABLED) +
                            r" error=' \+ \$finalProbe\.ReadError\)\s*$")
_BARE_ENABLED_BLOCK = re.compile(r"^\$finalState = \(Get-ScheduledTask -TaskPath \$task\.TaskPath"
                                 r" -TaskName \$task\.TaskName\)\.State$")
_ENABLE_CALL = re.compile(r"^\s*Enable-ScheduledTask -TaskPath")
_DONE = re.compile(r"^Write-Output 'SWAP-DONE'$")
_BUDGET_CALL = re.compile(r"^    Clear-BackendBudget -StatePath")
_STOP = re.compile(r"Stop-AfterFailure")
_RETRY = re.compile(r"\bwhile\b|\bfor\b|foreach\s*\(|Start-Sleep|AddSeconds|New-TimeSpan")
_TRUNCATE = re.compile(r"Remove-Item|Set-Content|Clear-Content|Out-File|Add-Content|WriteAllBytes")
_VERDICT_FIELD = re.compile(r"(verdict|aborted|swallowed|handled|branch)=")

# 改动前（HEAD 那份）SITE 2 的两行原文：牙印四与"改前那份"都用它，逐字照抄 blob
PRE_TICKET_ENABLED_BLOCK = """$finalState = (Get-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName).State
Write-Output ('ENABLED state=' + $finalState)"""


# ------------------------------------------------------------------ 定位与"只看这一段"的尺
def _func_span(lines: list, name: str):
    start = _idx(r"^function\s+" + name + r"\s*\{", lines)
    if start is None:
        return None
    end = _block_end(lines, start, len(lines) - 1)
    return None if end is None else (start, end)


def _budget_region(text: str):
    """`Clear-BackendBudget` 的**整段函数原文**（注释已剥、行数与原文对齐）。

    分两层是 boot_poll 那份的先例：探针与牙印需要"撤掉 try 之后仍能抄出原文"，
    而 `_budget_ctx` 对那种形状必须返回 None（判据因此对坏形状恒红）。
    """
    lines = _code_lines(text)
    if len(lines) != len(text.splitlines()):
        return None
    fn = _func_span(lines, "Clear-BackendBudget")
    if fn is None:
        return None
    lo, hi = fn
    body = "\n".join(lines[lo:hi + 1])
    if "[System.IO.File]::ReadAllBytes($StatePath)" not in body:
        return None
    if "[System.IO.File]::WriteAllText($StatePath, $json, $enc)" not in body:
        return None
    return {"lines": lines, "fn": fn, "region_text": body}


def _budget_code(text: str):
    """函数体的**代码**文本（注释已剥）。

    漂移样本的"这一枚 token 不该再出现"必须按这段判，不能按整份文件判：文件头那张登记表里
    合法地写着 `BUDGET state=read-threw` 这些串（boot_poll 那份为同一件事留了同样的话）。
    """
    reg = _budget_region(text)
    if reg is None:
        return ""
    lo, hi = reg["fn"]
    return "\n".join(reg["lines"][lo:hi + 1])


def _enabled_code(text: str):
    """step 8 那一整段（最后一次启用 -> 确认读 -> ENABLED token）的**代码**文本，同上理由。

    两种形状都成立（改动前那份也有启用行与 `ENABLED state=` 那一行），所以牙印能照它判。
    """
    lines = _code_lines(text)
    enables = [i for i, ln in enumerate(lines) if _ENABLE_CALL.match(ln)]
    toks = [i for i, ln in enumerate(lines) if _ENABLED_TOKEN_LOOSE.match(ln)]
    if not enables or not toks:
        return ""
    a = max(enables)
    after = [i for i in toks if i > a]
    if not after:
        return ""
    return "\n".join(lines[a:after[0] + 1])


def _budget_site(text: str, which: str):
    """单独定位**一处**文件调用（读或写）：它在自己的 try 里、catch 里有具名错误变量、
    catch 里打了一行 `BUDGET state=...` 的 token。任何一条不满足就返回 None。

    **一处一处判**是这套锁的关键设计：本票要撤掉任意一半（独立性牙印）时，另一半的锁
    必须照旧绿。若把两处绑在一个定位器里，撤掉读会让写的判据也一起红，红就归错了人——
    那正是 `test_swap_package_disable_verify.py` 里"两条锁必须互相独立"要防的事。
    """
    reg = _budget_region(text)
    if reg is None:
        return None
    lines = reg["lines"]
    lo, hi = reg["fn"]
    pattern, token = (_READ_STMT, _READ_TOKEN) if which == "read" else (_WRITE_STMT, _WRITE_TOKEN)
    hits = [i for i, ln in enumerate(lines) if pattern.search(ln)]
    if len(hits) != 1:
        return None
    idx = hits[0]
    span = _span_of(_try_spans(lines, lo, hi), idx)
    if span is None or not _TRY_OPEN.match(lines[span[0]]):
        return None
    s, e = span
    catches = [i for i in range(s, e + 1) if _CATCH_OPEN.match(lines[i])]
    if len(catches) != 1:
        return None
    handler = lines[catches[0] + 1:e]
    if not handler:
        return None
    caps = [m.group("var") for m in (_CAPTURE_ERR.match(ln) for ln in handler) if m]
    if len(caps) != 1:
        return None
    # catch 里必须有一行 `BUDGET state=` 的 token（**是什么语法由 `_budget_fail_token_ok` 判**，
    # 这里只要求"这一条路留下了一行 BUDGET"，好让"只改那一行文字"的样本红在正确的锁上）
    if not any(re.match(r"^\s*Write-Output \('BUDGET state=", ln) for ln in handler):
        return None
    return {"lines": lines, "fn": (lo, hi), "idx": idx, "span": span, "catch": catches[0],
            "handler": handler, "handler_text": "\n".join(handler), "err_var": caps[0],
            "token_line": next(ln for ln in handler if token in ln) if any(token in ln for ln in handler) else None,
            "token": token}


def _budget_ctx(text: str):
    """两处的**共同**上下文（顺序、整段函数原文）。任何一处定位不到就返回 None。

    只用在你必须同时知道两处形状的地方（前提锁、样本构造）；单条判据一律走 `_budget_site`。
    """
    reg = _budget_region(text)
    if reg is None:
        return None
    out = {"lines": reg["lines"], "fn": reg["fn"], "region_text": reg["region_text"]}
    for tag in ("read", "write"):
        site = _budget_site(text, tag)
        if site is None:
            return None
        out[tag] = site["idx"]
        out[tag + "_span"] = site["span"]
        out[tag + "_catch"] = site["catch"]
        out[tag + "_handler"] = site["handler"]
        out[tag + "_handler_text"] = site["handler_text"]
        out[tag + "_err_var"] = site["err_var"]
        out[tag + "_token_line"] = site["token_line"]
    if not (out["read"] < out["write"]):
        return None
    return out


def _enabled_ctx(text: str):
    """SITE 2 那三行：helper 读 -> READFAIL -> ENABLED token；必须在最后一次启用之后、
    清账调用之前、`SWAP-DONE` 之前，且这几行各自唯一。"""
    lines = _code_lines(text)
    if len(lines) != len(text.splitlines()):
        return None
    boot = _boot_region(text)
    if boot is None:
        return None
    reads = [i for i, ln in enumerate(lines) if _HELPER_READ.match(ln)]
    tokens = [i for i, ln in enumerate(lines) if _ENABLED_TOKEN.match(ln)]
    fails = [i for i, ln in enumerate(lines) if _READFAIL_LINE.match(ln)]
    dones = [i for i, ln in enumerate(lines) if _DONE.match(ln)]
    enables = [i for i, ln in enumerate(lines) if _ENABLE_CALL.match(ln)]
    calls = [i for i, ln in enumerate(lines) if _BUDGET_CALL.match(ln)]
    if not (len(reads) == len(tokens) == len(fails) == len(dones) == len(calls) == 1):
        return None
    r, tok, fail, done, call = reads[0], tokens[0], fails[0], dones[0], calls[0]
    # step 8 启用 -> 确认读 -> READFAIL -> ENABLED -> step 9 清账 -> SWAP-DONE
    if not enables or not (max(enables) < r < fail <= tok < call < done):
        return None
    if not (boot["started"] < r):
        return None
    enable = max(enables)
    if not (enable > 0 and _TRY_OPEN.match(lines[enable - 1])):
        # 启用不在自己的 try 里 = #24 那层保护没了。本票不判这件事（那是别的锁的活），
        # 但区间也拼不出一段合法探针（从 `} catch {` 起抄会留下配不上的花括号）。
        return None
    raw_hits = [i for i, ln in enumerate(lines) if _BARE_ENABLED_BLOCK.match(ln)]
    return {"lines": lines, "read": r, "readfail": fail, "token": tok, "done": done,
            "budget_call": call, "enable": enable, "region_start": enable - 1, "boot": boot,
            "bare_raw_reads": raw_hits, "region_text": "\n".join(lines[enable - 1:tok + 1])}


def _registry_block(text: str):
    """文件头 `<# ... #>` 里那张 token 登记表的**原文**行。

    登记表判据**不吃** `_code_lines`：整张表本身就是注释，剥掉注释它就空了，
    那种"绿"是恒绿的装饰。区间取 `Progress lines are one-token machine-parsable`
    到 `Keep this file 100% ASCII`（顶格散文，各恰好一处）。
    """
    raw = text.splitlines()
    start = _idx(r"^Progress lines are one-token machine-parsable", raw)
    end = _idx(r"^Keep this file 100% ASCII", raw)
    if start is None or end is None or end <= start:
        return None
    return "\n".join(raw[start:end])


# ------------------------------------------------------------------ 判据本体
def _stmt_wrapped_and_degrades(text: str, which: str) -> bool:
    """(a) SITE 1 的一半：那句文件调用在自己的 try 里；catch 记错误 + 打 token +
    `return` 收尾；不许中止、不许重试、不许动别的文件、不许出现第二个写文件的形状。"""
    site = _budget_site(text, which)
    if site is None:
        return False
    s, e = site["span"]
    if not (s < site["idx"] <= e):
        return False
    handler = site["handler"]
    if any(_BARE_EXIT.match(ln) for ln in handler):
        return False
    if not _RETURN.match(handler[-1]):
        return False
    body = site["handler_text"]
    if _STOP.search(body) or _RETRY.search(body) or _TRUNCATE.search(body):
        return False
    if site["err_var"] not in body:
        return False
    _no_write_cmdlets(body, "Clear-BackendBudget 的 " + which + " catch 本体")
    return True


def _budget_fail_token_ok(text: str) -> bool:
    """(c) 两行失败 token 各**恰好一处**、括号在一行里配平、三个键齐、不自报结论，
    且与成功那一行**互不认领**（`backend-entries=` 与 `backend-entries-cleared=` 是两族）。"""
    reg = _budget_region(text)
    if reg is None:
        return False
    lines = reg["lines"]
    for token in _FAIL_TOKENS:
        hits = [i for i, ln in enumerate(lines)
                if re.match(r"^\s*Write-Output \('" + re.escape(token), ln)]
        if len(hits) != 1:
            return False
        line = lines[hits[0]]
        if line.count("(") != line.count(")"):
            return False
        if not all(k in line for k in _FAIL_KEYS):
            return False
        if _VERDICT_FIELD.search(line):
            return False
    success = [ln for ln in lines if _SUCCESS_TOKEN in ln]
    if len(success) != 1:
        return False
    return not any(t in success[0] for t in _FAIL_TOKENS)


def _no_stop_route_anywhere(text: str) -> bool:
    """票面"不许改成 Stop-AfterFailure"，判**整个函数体 + 尾巴整段**，不只 catch 前几行。

    真正的危险不是"有人往 catch 里塞一句"，而是"有人把 `return` 换成中止"——那正好落在
    catch 的最后几行，所以两处都判。`_restore_ok` 那把公共尺保证被禁函数的**本体**没坏，
    于是这里的红只能归因于"这两站点用了它"。step 8 自己那层 catch 里的
    `Stop-AfterFailure 'enable'`（#24 那一族，本票不许一起"统一"掉它）必须还在。
    """
    ctx = _budget_ctx(text)
    en = _enabled_ctx(text)
    if ctx is None or en is None:
        return False
    lo, hi = ctx["fn"]
    if _STOP.search("\n".join(ctx["lines"][lo:hi + 1])):
        return False
    after = "\n".join(en["lines"][en["read"]:en["budget_call"]])
    if _STOP.search(after) or re.search(r"^\s*(exit|throw)\b", after, re.M):
        return False
    step8 = "\n".join(en["lines"][en["enable"]:en["read"]])
    if "Stop-AfterFailure 'enable'" not in step8:
        return False
    return _restore_ok(text)


def _file_touches_all_wrapped(text: str) -> bool:
    """射程棘轮：函数体里**每一处** `System.IO.*` 调用都必须在自己的 try 里，且总数恰好 2。

    数字本身就是登记点：将来谁再加第三处文件调用（"顺手验一下文件大小"那种），必须连
    catch、本票那一族 token 和这个数字一起改，否则这条红。
    """
    reg = _budget_region(text)
    if reg is None:
        return False
    lines = reg["lines"]
    lo, hi = reg["fn"]
    spans = _try_spans(lines, lo, hi)
    touches = [i for i in range(lo, hi + 1) if _FILE_TOUCH.search(lines[i])]
    if len(touches) != 2:
        return False
    return all(_span_of(spans, i) is not None for i in touches)


def _reaches_the_tail_token(text: str) -> bool:
    """(b) 的静态那一半：两处站点都在 `SWAP-DONE` **之前**，收尾那行与清账调用各恰好一处。"""
    en = _enabled_ctx(text)
    if en is None:
        return False
    lines = en["lines"]
    dones = [i for i, ln in enumerate(lines) if _DONE.match(ln)]
    calls = [i for i, ln in enumerate(lines) if _BUDGET_CALL.match(ln)]
    if len(dones) != 1 or len(calls) != 1:
        return False
    return en["read"] < en["token"] < en["budget_call"] < dones[0]


def _enabled_read_goes_through_the_helper(text: str) -> bool:
    """(a) SITE 2：读**复用**既有 helper；尾巴上不许留第二把裸任务读，
    也不许在站点上再套一层 try（"读这个开关只许有一条路"）。"""
    en = _enabled_ctx(text)
    if en is None:
        return False
    if en["bare_raw_reads"]:
        return False
    spans = _try_spans(en["lines"], 0, len(en["lines"]) - 1)
    if _span_of(spans, en["read"]) is not None:
        return False
    if not _HELPER_READ.match(en["lines"][en["read"]]):
        return False
    helper = _func_body(text, "Get-WatchdogTaskState")
    if helper is None:
        return False
    joined = "\n".join(helper)
    if "$enabled = [bool] $fresh.Settings.Enabled" not in joined:
        return False
    # helper 的 catch 把失败当**字段**带出，绝不自己打印（调用点是被赋值捕获的）
    if "Write-Output" in joined:
        return False
    return _single_value_helpers_ok(text)


def _enabled_token_stays_honest(text: str) -> bool:
    """(b)/(c) SITE 2 的形状：答不了就 `READFAIL stage=final-enabled-read` +
    `ENABLED state=Unreadable`，并且不许把 fail-CLOSED 的 `enabled=` 顺手打进 step 8 的 token。"""
    en = _enabled_ctx(text)
    if en is None:
        return False
    helper = "\n".join(_func_body(text, "Get-WatchdogTaskState") or [])
    if "$state = 'Unreadable'" not in helper or "$enabled = $true" not in helper:
        return False
    if "return [pscustomobject]@{ Enabled = $enabled" not in helper:
        return False
    if "$finalProbe.State" not in en["lines"][en["token"]]:
        return False
    tail = "\n".join(en["lines"][en["read"]:en["token"] + 1])
    if "$finalState" in tail or "enabled=" in tail or "via=" in tail:
        return False
    if "$finalProbe.ReadError" not in en["lines"][en["readfail"]]:
        return False
    return _READFAIL_ENABLED in en["lines"][en["readfail"]]


def _registry_documents_the_tail(text: str) -> bool:
    """(c) 后半：登记表必须写明两枚新 token、`budget-returned=false` 的语义、
    `ENABLED state=Unreadable`，以及"验收之后退化不止步"这条规则。"""
    block = _registry_block(text)
    if block is None:
        return False
    for needle in ("read-threw", "write-threw", "budget-returned=false",
                   _READFAIL_ENABLED, "ENABLED state=Unreadable",
                   "POST-ACCEPTANCE TAIL DEGRADES INTO TOKENS INSTEAD OF DYING"):
        if needle not in block:
            return False
    return not re.search(r"ABORT stage=(budget|final-enabled-read)", block)


# ------------------------------------------------------------------ 生成的探针
_BUDGET_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
# t47 budget routing probe. The script's OWN Clear-BackendBudget body runs against a
# hand-built fake ledger whose path arrives on the command line, so the live production
# ledger is never opened here. read-throws simulates the watchdog being mid-write by
# holding the fake file with FileShare=None inside this very process; write-throws gets
# a file it CAN read but cannot write.
$case = $args[0]
$statePath = $args[1]
$script:t47Holder = $null
if ($case -eq 'read-throws') {
    $script:t47Holder = [System.IO.File]::Open($statePath, 'Open', 'ReadWrite', 'None')
}
$script:backupDone = $true
$script:watchdogDisabled = $false
$Backup = 'C:/t47-fake-dir/run_backend_old-19700101-000000'
$Live = 'C:/t47-fake-dir/run_backend'
$LiveExe = 'C:/t47-fake-dir/run_backend/run_backend.exe'
$ProjectRoot = 'C:/t47-fake-dir'
$stamp = '19700101-000000'
$task = [pscustomobject]@{ TaskPath = '\t47-fake-path\'; TaskName = 't47-fake-task' }
function Get-LiveProcess { return @() }
function Show-Rollback { return 'RECIPE-LINE' }
function Restore-WatchdogTask { return 'RESTORE-STUB-CALLED' }
# The script's REAL exit machinery (whole function, spliced as-is): if the bookkeeping
# step ever routes through it, this prints the post-backupDone branch verbatim - including
# the line that would be a lie at this point in the run - and throws, so SWAP-DONE and
# PROBE_END disappear, which is the red we want.
__STOP__
__REGION__
Clear-BackendBudget -StatePath $statePath
'CALL_RETURNED'
# The script's own next line: the run must still land here after a degraded budget step.
Write-Output 'SWAP-DONE'
if ($script:t47Holder) { $script:t47Holder.Dispose() }
if ($case -eq 'happy') {
    $obj = [System.IO.File]::ReadAllText($statePath) | ConvertFrom-Json
    'AFTER keys=' + (($obj.PSObject.Properties | ForEach-Object { $_.Name }) -join ',')
    'AFTER bom=' + ([System.IO.File]::ReadAllBytes($statePath)[0] -eq 0xEF)
}
'PROBE_END'
"""

_ENABLED_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
# t47 enabled-read routing probe. The script's OWN step 8 tail plus its OWN
# Get-WatchdogTaskState body, against a hand-built FAKE task identity. Both task cmdlets
# are STUBS here, so nothing is read from or written to the live watchdog task, no file
# is touched and nothing goes near the port the running service listens on.
$case = $args[0]
$task = [pscustomobject]@{ TaskPath = '__FAKE_TASK_PATH__'; TaskName = '__FAKE_TASK_NAME__' }
function Get-ScheduledTask {
    param($TaskPath, $TaskName)
    switch ($case) {
        'answers' { return [pscustomobject]@{ State = 'Ready'; Settings = [pscustomobject]@{ Enabled = $false } } }
        'throws' { throw 'CIM-PROBE-FAILURE MSFT_ScheduledTask cannot answer' }
        'empty' { return $null }
        default { 'UNKNOWN-CASE ' + $case; exit 3 }
    }
}
function Enable-ScheduledTask {
    param($TaskPath, $TaskName)
    $script:t47EnableStubRan = 'yes'
    return [pscustomobject]@{ StubOnly = 'ENABLE-STUB' }
}
__HELPER__
__REGION__
'CALL_RETURNED'
'ENABLE_STUB_RAN=' + $(if ($script:t47EnableStubRan) { $script:t47EnableStubRan } else { 'no' })
'PROBE_END'
"""

# "这一站不在中止机器的射程里"用**真** Stop-AfterFailure 本体跑：只有真函数体才会打印那句
# 字面量 moved=yes watchdog=left-disabled-by-design，而它正是本票不许在这里出现的假话。
# 中止路径可能摸到的一切都是假路径 / 喊一声的桩：真任务、真包、网络一律问不到。
_STOP_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
# t47 real-exit probe: the script's OWN Stop-AfterFailure + the script's OWN step 8 tail,
# in the state step 8 actually leaves behind (backupDone=True, watchdogDisabled=False).
$task = [pscustomobject]@{ TaskPath = '__FAKE_TASK_PATH__'; TaskName = '__FAKE_TASK_NAME__' }
$script:backupDone = $true
$script:watchdogDisabled = $false
$Backup = 'C:/t47-fake-dir/run_backend_old-19700101-000000'
$Live = 'C:/t47-fake-dir/run_backend'
$LiveExe = 'C:/t47-fake-dir/run_backend/run_backend.exe'
$ProjectRoot = 'C:/t47-fake-dir'
$stamp = '19700101-000000'
$Port = 1
function Get-LiveProcess { return @() }
function Show-Rollback { return ('RECIPE-LINE backup-package=' + $Backup) }
function Restore-WatchdogTask { return 'RESTORE-STUB-CALLED' }
function Enable-ScheduledTask {
    param($TaskPath, $TaskName)
    $script:t47EnableStubRan = 'yes'
    return [pscustomobject]@{ StubOnly = 'ENABLE-STUB' }
}
function Get-ScheduledTask {
    param($TaskPath, $TaskName)
    throw 'CIM-PROBE-FAILURE MSFT_ScheduledTask cannot answer'
}
__HELPER__
__STOP__
__REGION__
'CALL_RETURNED'
Stop-AfterFailure 'enable-read-threw' 'what an abort at this site would print'
'PROBE_END'
"""

_SHAPE_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
# t47 helper-shape probe. #50's lesson applied to this site: measure the object the
# expression ACTUALLY receives, do not read the intent off the source text. The fake
# identity below is never the live task; the stub throws so the shape reported is the
# one this run gets when the reader cannot answer.
$task = [pscustomobject]@{ TaskPath = '__FAKE_TASK_PATH__'; TaskName = '__FAKE_TASK_NAME__' }
function Get-ScheduledTask {
    param($TaskPath, $TaskName)
    throw 'CIM-PROBE-FAILURE shape probe'
}
__HELPER__
$rawOut = @(Get-WatchdogTaskState -TaskToRead $task)
'items_from_helper=' + $rawOut.Count
$finalProbe = $rawOut[0]
'shape_is_array=' + ($finalProbe -is [array])
'shape_count=' + $(if ($null -eq $finalProbe.Count) { '$null' } else { [string] $finalProbe.Count })
'shape_type=' + $(if ($null -eq $finalProbe) { 'Null' } else { $finalProbe.GetType().Name })
$members = ''
if ($finalProbe) {
    $members = (($finalProbe | Get-Member -MemberType NoteProperty | ForEach-Object { $_.Name }) -join ',')
}
'shape_members=' + $members
'state=' + [string] $finalProbe.State
'enabled=' + [string] $finalProbe.Enabled
'enabled_source=' + [string] $finalProbe.EnabledSource
'read_error_len=' + ([string] $finalProbe.ReadError).Length
'probe_done=yes'
"""

_LIVE_READ_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
# t47 live-identity READ probe. Read-only: it finds the watchdog task BY ACTION (the name
# is Chinese on this box, which is exactly why the production script searches by action
# too) and asks it through the script's own reader. Nothing below writes a task, a process
# or a file: the only cmdlet used is a plain enumeration, and the reader itself only reads.
$all = @(Get-ScheduledTask)
'enumerated=' + $all.Count
$wd = @($all | Where-Object {
    $_.Actions.Execute -eq 'wscript.exe' -and $_.Actions.Arguments -like '*watchdog-hidden.vbs*'
})
'watchdog_count=' + $wd.Count
if ($wd.Count -lt 1) { 'NO-WATCHDOG-TASK'; exit 0 }
$task = [pscustomobject]@{ TaskPath = $wd[0].TaskPath; TaskName = $wd[0].TaskName }
__HELPER__
$finalProbe = Get-WatchdogTaskState -TaskToRead $task
'state=' + [string] $finalProbe.State
'enabled=' + [string] $finalProbe.Enabled
'enabled_source=' + [string] $finalProbe.EnabledSource
'read_error_len=' + ([string] $finalProbe.ReadError).Length
'probe_done=yes'
"""


def _splice(template: str, helper: str, region: str = None, stop: str = None):
    """把脚本自己的文本拼进探针模板，并对拼好的源码做硬校验。

    任何一条不过就返回 None（绝不静默改跑"另一段更好看的代码"）：哨兵必须换尽、抄进去的
    区域必须逐字在场、纯 ASCII、不含真账本路径、不含网络形状、不含写任务的 cmdlet 名。
    `helper` 是 `_func_body()` 给的**完整函数**（含签名与收尾花括号），所以模板里只放
    `__HELPER__`，不许再套一层 `function {}`——套了会变成函数体里再定义函数，调用侧拿到
    的是空输出（这个形状在本票开发时真踩过一次，接线锁把它判红才算钉住）。
    """
    if helper is None:
        return None
    out = template.replace("__HELPER__", helper)
    if stop is not None:
        out = out.replace("__STOP__", stop)
    if region is None:
        if "__REGION__" in out:
            return None
    else:
        if not region.strip() or "__REGION__" not in out:
            return None
        out = out.replace("__REGION__", region)
    out = out.replace("__FAKE_TASK_PATH__", _FAKE_TASK_PATH)
    out = out.replace("__FAKE_TASK_NAME__", _FAKE_TASK_NAME)
    if "__" in out:
        return None
    if helper not in out:
        return None
    if region is not None and region not in out:
        return None
    if not out.isascii():
        return None
    if _REAL_PATH_MARK in out:
        return None
    if any(m in out for m in _NETWORK_MARKS):
        return None
    for name in _TASK_WRITES:
        if name in out:
            return None
    return out


def _helper_text(text: str):
    body = _func_body(text, "Get-WatchdogTaskState")
    if body is None:
        return None
    joined = "\n".join(body)
    if "$enabled = $true" not in joined:
        return None
    if "$enabled = [bool] $fresh.Settings.Enabled" not in joined:
        return None
    if "Write-Output" in joined:
        return None
    return joined


def _stop_body(text: str):
    body = _func_body(text, "Stop-AfterFailure")
    if body is None:
        return None
    joined = "\n".join(body)
    if "if ($script:backupDone) {" not in joined or "Show-Rollback" not in joined:
        return None
    if "ABORT stage=" not in joined:
        return None
    return joined


def _budget_probe_text(text: str, region: str = None):
    reg = _budget_region(text)
    stop = _stop_body(text)
    if reg is None or stop is None:
        return None
    body = reg["region_text"] if region is None else region
    out = _splice(_BUDGET_TEMPLATE, "", region=body, stop=stop)
    if out is None:
        return None
    for needle in ("Get-ScheduledTask", "Enable-ScheduledTask", "Disable-ScheduledTask"):
        if needle in out:
            return None
    return out


def _enabled_probe_text(text: str, region: str = None, template: str = _ENABLED_TEMPLATE,
                        stage_required: bool = True):
    helper = _helper_text(text)
    if helper is None:
        return None
    if region is None:
        en = _enabled_ctx(text)
        if en is None:
            return None
        region = en["region_text"]
    if "$finalProbe = Get-WatchdogTaskState -TaskToRead $task" not in region:
        if stage_required or "$finalState" not in region:
            return None
    if stage_required and ("$finalProbe.State" not in region or _READFAIL_ENABLED not in region):
        return None
    stop = None
    if template is _STOP_TEMPLATE:
        stop = _stop_body(text)
        if stop is None:
            return None
    return _splice(template, helper, region=region, stop=stop)


def _shape_probe_text(text: str, live: bool = False):
    helper = _helper_text(text)
    if helper is None:
        return None
    return _splice(_LIVE_READ_TEMPLATE if live else _SHAPE_TEMPLATE, helper)


# ------------------------------------------------------------------ 漂移样本构造
def _unwrap_budget_site(text: str, which: str):
    """撤掉某一处的 wrap：try + catch 一起撤（读那半连本票加的 `$bytes = $null` 初始化一起撤），
    只留那句裸调用。另一半一字不动，文件其余部分（含注释）逐行保留。"""
    site = _budget_site(text, which)
    if site is None:
        return None
    s, e = site["span"]
    raw = text.splitlines()
    if which == "read" and _NULL_INIT.match(raw[s - 1]):
        s = s - 1
    stmt = raw[site["idx"]].strip()
    return "\n".join(raw[:s] + ["    " + stmt] + raw[e + 1:])


def _abort_budget_site(text: str, which: str):
    """把 catch 收尾的 `return` 换成 `Stop-AfterFailure`（票面明令的反面）。"""
    site = _budget_site(text, which)
    if site is None:
        return None
    raw = text.splitlines()
    s, e = site["span"]
    rets = [i for i in range(s, e + 1) if _RETURN.match(raw[i])]
    if len(rets) != 1:
        return None
    i = rets[0]
    indent = raw[i][:len(raw[i]) - len(raw[i].lstrip())]
    out = raw[:]
    out[i] = (indent + "Stop-AfterFailure 'budget-threw' ('the ledger could not be handled, "
              "error=' + " + site["err_var"] + ")")
    return "\n".join(out)


def _to_pre_ticket_enabled(text: str):
    """SITE 2 退回改动前那两行裸读（HEAD 那份的形状）。"""
    en = _enabled_ctx(text)
    if en is None:
        return None
    raw = text.splitlines()
    out = raw[:en["region_start"]] + PRE_TICKET_ENABLED_BLOCK.splitlines() + raw[en["token"] + 1:]
    tail = _enabled_code("\n".join(out))
    if PRE_TICKET_ENABLED_BLOCK not in tail:
        return None
    return "\n".join(out)


def _weaken_token(text: str, which: str):
    """把失败 token 退回到只说"这次没做成"的散文形状（票面明令不许的那种：
    要说清**预算没还**，不能只说 read/write failed）。"""
    site = _budget_site(text, which)
    if site is None or site["token_line"] is None:
        return None
    old = site["token_line"]
    token = _READ_TOKEN if which == "read" else _WRITE_TOKEN
    new = ("        Write-Output ('BUDGET state=" + which + "-skipped not-overwritten: ' + "
           + site["err_var"] + ")")
    if old not in text or token in new:
        return None
    return text.replace(old, new, 1)


def _drop_registry_lines(text: str, needle: str):
    """只抹**登记表区间内**含 needle 的那几行（代码里那一行 token 必须原样留着，
    否则这枚样本会同时红两条，独立性就没法判）。"""
    raw = text.splitlines()
    block = _registry_block(text)
    if block is None:
        return None
    head = block.splitlines()[0]
    if head not in raw:
        return None
    start = raw.index(head)
    end = start + len(block.splitlines())
    hits = [i for i in range(start, min(end, len(raw))) if needle in raw[i]]
    if not hits:
        return None
    out = raw[:]
    for i in hits:
        out[i] = ""
    return "\n".join(out)


# ------------------------------------------------------------------ PowerShell 跑法
_POWERSHELL = _find_powershell()
_BUDGET_CASES = ("happy", "read-throws", "write-throws")
_ENABLED_CASES = ("answers", "throws", "empty")


def _skip_machine() -> None:
    pytest.skip("这台机器没有 powershell.exe，路由那几条跑不了。静态那几条不受影响、照判；"
                "但本票「账本被锁住时这台机器实际打印哪几行、运行走不走得到 SWAP-DONE」与"
                "「裸读一抛怎么死」是这台机器判的，放行复跑必须在 Windows 现网机上做"
                "（理由同机器真相锁那一份）。")


def _run_powershell(path: Path, *args):
    cmd = [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]
    cmd.extend(str(a) for a in args)
    proc = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS,
                          cwd=str(path.parent))
    # 按字节收再 errors="replace" 解：PS 5.1 把 throw 的横幅写进 stderr，那里面带控制台代码页
    # （本机 GBK）的中文字节，严格 utf-8 会把两路都读成空串（同 boot_poll 那一份的说明）。
    return (proc.returncode, (proc.stdout or b"").decode("utf-8", "replace"),
            (proc.stderr or b"").decode("utf-8", "replace"))


def _write_crlf(tmp_path: Path, name: str, text: str) -> Path:
    """副本按 **CRLF** 落盘（本机 core.autocrlf=true，`git show` 给的是 LF blob，拿它当
    "改动前那份"喂给文件自带的格式门会先红在格式上，把"改前也全绿"这种假结论坐实）。"""
    body = text.replace("\r\n", "\n").replace("\n", "\r\n")
    path = tmp_path / name
    path.write_bytes(body.encode("utf-8"))
    raw = path.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n"), "副本必须是纯 CRLF：见函数说明"
    assert not any(b >= 0x80 for b in raw), "副本必须保持纯 ASCII：PS 5.1 的解码坑"
    return path


def _parse(out: str):
    lines = [ln.strip() for ln in out.splitlines()]
    kv = {}
    for ln in lines:
        k, _, v = ln.partition("=")
        if k and v and k != ln and k not in kv:
            kv[k] = v
    return {"kv": kv,
            "budget": [ln for ln in lines if ln.startswith("BUDGET")],
            "read_threw": [ln for ln in lines if ln.startswith(_READ_TOKEN)],
            "write_threw": [ln for ln in lines if ln.startswith(_WRITE_TOKEN)],
            "success": [ln for ln in lines if ln.startswith(_SUCCESS_TOKEN)],
            "readfail": [ln for ln in lines if ln.startswith(_READFAIL_ENABLED)],
            "enabled": [ln for ln in lines if ln.startswith("ENABLED state=")],
            "abort": [ln for ln in lines if ln.startswith("ABORT stage=")],
            "fail": [ln for ln in lines if ln.startswith("FAIL stage=")],
            "recipe": [ln for ln in lines if ln.startswith("RECIPE-LINE")],
            "restore": [ln for ln in lines if ln == "RESTORE-STUB-CALLED"],
            "returned": [ln for ln in lines if ln == "CALL_RETURNED"],
            "done": [ln for ln in lines if ln == "SWAP-DONE"],
            "end": [ln for ln in lines if ln == "PROBE_END"]}


def _make_fake_ledger(work: Path, case: str) -> Path:
    """手搓假账：tmp_path 下一份带 BOM、带 backend/tunnel/seen 三种键的假 JSON。

    票面卫生硬约束：真实 store 路径绝不进这里，被清的对象永远是 `t47-fake-state.json`。
    """
    path = work / _PROBE_MARK
    _unlock(path)
    body = ('{"backend":["2026-01-01T00:00:00+08:00","2026-01-01T00:05:00+08:00"],'
            '"tunnel":["2026-01-01T00:00:00+08:00"],'
            '"seen:port:backend":["2026-01-01T00:00:00+08:00"]}')
    path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    assert path.read_bytes().startswith(b"\xef\xbb\xbf"), "假账必须带 BOM：真账就是 watchdog 带 BOM 写的"
    if case == "write-throws":
        os.chmod(path, stat.S_IREAD)
    return path


def _unlock(path: Path) -> None:
    if path.exists():
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)


def _run_budget(text: str, case: str, work: Path, region: str = None, name: str = None):
    src = _budget_probe_text(text, region=region)
    assert src is not None, "探针源码生不出来：Clear-BackendBudget 或 Stop-AfterFailure 抄不出原文"
    path = _write_crlf(work, name or ("probe-budget-" + case + ".ps1"), src)
    assert path.read_text(encoding="utf-8") == src, "探针没落盘：牙齿咬的是内存而不是文件"
    ledger = _make_fake_ledger(work, case)
    rc, out, err = _run_powershell(path, case, ledger)
    _unlock(ledger)
    return rc, _parse(out), err, out


def _run_enabled(text: str, case: str, work: Path, region: str = None,
                 template: str = _ENABLED_TEMPLATE, stage_required: bool = True,
                 name: str = None):
    src = _enabled_probe_text(text, region=region, template=template, stage_required=stage_required)
    assert src is not None, "探针源码生不出来：SITE 2 那段抄不出原文（见 _enabled_probe_text）"
    path = _write_crlf(work, name or ("probe-enabled-" + case + ".ps1"), src)
    assert path.read_text(encoding="utf-8") == src, "探针没落盘：牙齿咬的是内存而不是文件"
    rc, out, err = _run_powershell(path, case)
    return rc, _parse(out), err, out


@pytest.fixture(scope="module")
def budget_routes(tmp_path_factory):
    """三档账本形状（写得回去 / 读被挡住 / 写被拒）各跑一次**真**函数体，模块共用。"""
    if _POWERSHELL is None:
        _skip_machine()
    work = tmp_path_factory.mktemp("budget-routing")
    return {case: _run_budget(PS1, case, work) for case in _BUDGET_CASES}


@pytest.fixture(scope="module")
def enabled_routes(tmp_path_factory):
    """SITE 2 三档答案（答得出 / 抛 / 答出空）各跑一次脚本自己的 step 8 整段。"""
    if _POWERSHELL is None:
        _skip_machine()
    work = tmp_path_factory.mktemp("enabled-routing")
    return {case: _run_enabled(PS1, case, work) for case in _ENABLED_CASES}


# ------------------------------------------------------------------ 静态锁
def test_both_post_acceptance_sites_are_locatable_and_the_locks_are_not_judging_an_empty_set():
    """前提锁：两处站点必须各自唯一地定位到，否则下面所有判据都是在判一个空集合。"""
    ctx = _budget_ctx(PS1)
    assert ctx is not None, (
        "Clear-BackendBudget 里那两处文件调用没有被各自的 try 包住（或区间不配平、catch 里没有"
        "把 `$_` 第一时间转成具名变量）：本票要堵的就是这两处——裸的一句在 "
        "$ErrorActionPreference='Stop' 下会把一次**已验收**的换包判死在没有 BUDGET、"
        "没有 SWAP-DONE、预算还留在打空状态的地方")
    assert ctx["read"] < ctx["write"], "读与写的先后不对：清账那一步被搬动了"
    en = _enabled_ctx(PS1)
    assert en is not None, (
        "SITE 2 没定位到（helper 读 / READFAIL / ENABLED / SWAP-DONE / 清账调用任意一行不唯一，"
        "或相对顺序不对，或 step 8 那层 try 不在了）")
    assert en["enable"] < en["read"] < en["token"] < en["budget_call"] < en["done"], \
        "SITE 2 与启用 / 清账 / SWAP-DONE 的相对位置不对：step 8 与 step 9 的顺序是既有契约"
    assert _registry_block(PS1) is not None, "文件头那张 token 登记表定位不到（判法见 _registry_block）"


def test_the_budget_read_and_write_each_degrade_into_a_token_instead_of_killing_the_run():
    for which in ("read", "write"):
        assert _stmt_wrapped_and_degrades(PS1, which), (
            f"Clear-BackendBudget 的 {which} 那半不合格：不在 try 里、catch 留了裸 throw/exit、"
            "收尾不是 return、自己开了重试或计时器、动了别的文件，或者没把错误记成具名变量"
            "——一抛就又回到本票要堵的那条静默死法（#24/#32/#45 同族）")


def test_every_file_touch_in_the_budget_function_is_wrapped_and_the_count_is_registered():
    """射程棘轮：函数体里**每一处** `System.IO.*` 调用都必须在自己的 try 里，且总数恰好 2。"""
    assert _file_touches_all_wrapped(PS1), (
        "Clear-BackendBudget 里的 [System.IO.*]:: 调用不再是「恰好两处、且各自在 try 里」："
        "本票的原则是这段尾巴上任何失败都必须留下机器可读的 token，多一处裸文件调用就是少一枚 token")


def test_neither_post_acceptance_site_may_route_through_the_abort_machinery():
    assert _no_stop_route_anywhere(PS1), (
        "验收之后的这两站点了 Stop-AfterFailure（或尾巴上出现了 exit / 裸 throw），"
        "或 step 8 自己那层 catch 被一起「统一」掉了，或被禁的那条出口函数本体已经不再"
        "打印两条分支各自的 token（_restore_ok 那把尺）：此刻包已验收、看守已扶回去，"
        "那条出口的字面量 watchdog=left-disabled-by-design 与整套 ROLLBACK 配方在这一刻"
        "两句都是假话")


def test_the_budget_failure_tokens_stay_single_line_and_in_the_existing_grammar():
    assert _budget_fail_token_ok(PS1), (
        f"{_READ_TOKEN} / {_WRITE_TOKEN} 有一行不在、不止一处、括号没在一行里配平、没带 "
        "error=/budget-returned=false/backend-entries=、自己报了结论，或与成功那一行 "
        "（BUDGET backend-entries-cleared=）混用了键名")


def test_the_tail_still_finishes_at_swap_done_after_both_sites():
    assert _reaches_the_tail_token(PS1), (
        "两处站点不在 SWAP-DONE 之前，或主流程那句顶格 Clear-BackendBudget -StatePath 被搬走/"
        "被包进条件里，或收尾那行不再唯一：本票要的是「账本处理不了也照样把这次换包收尾」，"
        "收尾走不到就是倒退")


def test_the_enabled_confirmation_reuses_the_one_existing_reader_and_leaves_no_bare_read():
    assert _enabled_read_goes_through_the_helper(PS1), (
        "SITE 2 不再走 Get-WatchdogTaskState（或尾巴上还剩第二把裸 (Get-ScheduledTask ...).State，"
        "或站点上又被套了一层 try，或 helper 自己开始打印东西）：读这个开关只许有一条路，"
        "a5d37bde 那次误读就是多了第二条才没人看见")


def test_the_enabled_token_stays_honest_when_the_read_cannot_answer():
    assert _enabled_token_stays_honest(PS1), (
        f"{_READFAIL_ENABLED} 或 ENABLED state= 的形状不合格：READFAIL 不恰好一处、没带 "
        "$finalProbe.ReadError、ENABLED 读的不是 helper 的 State 字段，或 helper 里那条"
        "诚实默认值 '$state = Unreadable' 不见了")


def test_the_header_registry_documents_both_new_tokens_and_the_degrade_rule():
    assert _registry_documents_the_tail(PS1), (
        "文件头那张 token 登记表没有登记 read-threw / write-threw / budget-returned=false / "
        "READFAIL stage=final-enabled-read / ENABLED state=Unreadable，或没写下"
        "「POST-ACCEPTANCE TAIL DEGRADES INTO TOKENS INSTEAD OF DYING」这条规则本身："
        "登记表不在，下一版换包就没人知道该盯哪几行")


def test_the_two_catches_never_delete_truncate_or_retry_the_live_ledger():
    """票面硬约束：不许"用重试兜住"、不许删/截 live runtime state、不许把真路径写进探针。"""
    ctx = _budget_ctx(PS1)
    assert ctx is not None
    for which in ("read", "write"):
        body = ctx[which + "_handler_text"]
        assert not _RETRY.search(body), f"{which} 的 catch 里有重试/计时器：票面明令不许无限重试"
        assert not _TRUNCATE.search(body), \
            f"{which} 的 catch 自己动手删/截文件：那份账一分钟就被 watchdog 重写一次"
        _no_write_cmdlets(body, "Clear-BackendBudget 的 " + which + " catch 本体")
    assert "Remove-Item" not in _budget_code(PS1), "整个清账函数都不许出现删除：这是全脚本的不删规则"
    probe = _budget_probe_text(PS1)
    assert probe is not None
    assert "$args[1]" in probe and _REAL_PATH_MARK not in probe, \
        "假账路径必须由命令行给，探针源码里不许出现真账本路径"


def test_the_under_test_file_stays_ascii_crlf_and_bom_free():
    """文件级门（票面第 2 条）：纯 ASCII + 无 BOM + CRLF 与 LF 同数；尺子借 `_ascii_ok`。

    两路解析检查（legacy `PSParser::Tokenize` 与 `Language.Parser` AST）由这台机器判，
    在 `test_machine_both_parsers_agree_the_file_still_parses` 里，这里只判字节形状——
    没有 PowerShell 的机器上这道门照样跑。
    """
    raw = SWAP_PS1.read_bytes()
    bad = [(i, b) for i, b in enumerate(raw) if b >= 0x80]
    assert _ascii_ok(raw), f"出现非 ASCII 字节或 BOM（前 3 处 {bad[:3]}）：PS 5.1 会把整脚本错解码成语法错误"
    assert raw.count(b"\r\n") > 0 and raw.count(b"\r\n") == raw.count(b"\n"), \
        "CRLF 与 LF 不同数（或 CRLF 计数为 0）：这份 .ps1 被重编码了"


# ------------------------------------------------------------------ 机器锁
def test_budget_probe_is_wired_to_the_scripts_own_function_and_cannot_touch_production():
    """接线锁 + 只读锁：探针跑的就是脚本抄出来的那个函数体**和那条真出口函数**，
    而且它问不到现网头上。没有这一条，机器那几条完全可以靠"另抄一段更好看的代码"恒绿。"""
    reg = _budget_region(PS1)
    assert reg is not None
    probe = _budget_probe_text(PS1)
    assert probe is not None, "探针源码生不出来（_budget_probe_text 的校验没过）"
    assert reg["region_text"] in probe, "探针跑的不是脚本那个函数体的原文"
    assert "if ($script:backupDone) {" in probe, "探针里的 Stop-AfterFailure 不是脚本那份真本体"
    for needle in ("[System.IO.File]::ReadAllBytes($StatePath)",
                   "[System.IO.File]::WriteAllText($StatePath, $json, $enc)",
                   _READ_TOKEN, _WRITE_TOKEN, _SUCCESS_TOKEN, "$hadBom",
                   "New-Object System.Text.UTF8Encoding($hadBom)", "state=empty-file skipped",
                   "state=unparsable skipped", "state=absent", "$bytes = $null",
                   "watchdog=left-disabled-by-design"):
        assert needle in probe, f"脚本自己的那一行没有原样落到探针里：{needle!r}"
    assert probe.isascii(), "生成的探针必须纯 ASCII：PS 5.1 会把带中文的无 BOM .ps1 错解码成语法错误"
    assert "$args[1]" in probe, "假账路径由命令行给，绝不写死在探针源码里"
    for needle in (_REAL_PATH_MARK, "Invoke-RestMethod", "127.0.0.1", "http://",
                   "Get-ScheduledTask", "Enable-ScheduledTask", "Disable-ScheduledTask",
                   "data"):
        assert needle not in probe, f"探针里出现了能碰到现网的形状：{needle!r}"
    for name in _TASK_WRITES:
        assert name not in probe


def test_machine_a_writable_ledger_still_clears_and_reaches_swap_done(budget_routes):
    """正控：账本写得回去时，行为与本票之前**完全一致**——清 backend 一个键、别的键原样留着、
    BOM 形状保住、打成功 token、走到 `SWAP-DONE`。

    少了这条，"包进 try" 那份改动可以靠永远走退化分支假绿（票面"接线锁不让机器锁恒真"那一半）。
    """
    rc, got, err, out = budget_routes["happy"]
    assert rc == 0, f"happy 档没跑完（rc={rc}）：{err[:300]} | {out[:300]}"
    assert len(got["success"]) == 1, f"没有打印成功 token：{out[:400]}"
    line = got["success"][0]
    assert "backend-entries-cleared=2" in line, f"backend 那一项没被清掉（假账里有 2 条）：{line}"
    assert "bom=True" in line, f"BOM 形状没保住（watchdog 用 Set-Content -Encoding UTF8 写它）：{line}"
    assert "other-keys-kept=true" in line
    assert got["read_threw"] == [] and got["write_threw"] == [], f"正常档竟然打了失败 token：{got['budget']}"
    keys = got["kv"].get("AFTER keys", "").split(",")
    assert "backend" not in keys, f"backend 那一项还留在盘上：{keys}"
    assert "tunnel" in keys and "seen:port:backend" in keys, \
        f"清账动了别的键（票面不许：那是既有故障的证据与日志节流标记）：{keys}"
    assert got["kv"].get("AFTER bom") == "True", f"写回去时 BOM 丢了：{got['kv']}"
    assert got["returned"] == ["CALL_RETURNED"] and got["done"] == ["SWAP-DONE"] and got["end"]
    assert got["abort"] == [], f"正常档竟然走了中止出口：{got['abort']}"


def test_machine_a_locked_ledger_degrades_to_one_token_and_still_reaches_swap_done(budget_routes):
    """机器判据（票面 (a) 的路由证据）：读被别的句柄挡住（模拟 watchdog 正在写它）时，
    这台机器打印**恰好一行** `BUDGET state=read-threw budget-returned=false
    backend-entries=unproven error=<真实错误原文>`，函数正常返回，**SWAP-DONE 照样打印**。

    探针用 tmp_path 下的手搓假文件，绝不碰 `data\\watchdog-state.json`。
    """
    rc, got, err, out = budget_routes["read-throws"]
    assert rc == 0, f"读被挡住时竟然把运行打死（rc={rc}）：{err[:400]} | {out[:400]}"
    assert len(got["read_threw"]) == 1, f"read-threw token 应当恰好一行：{out[:400]}"
    assert got["write_threw"] == [] and got["success"] == [], \
        f"读都没读到却声称清了账或走了别档：{got['budget']}"
    line = got["read_threw"][0]
    for key in _FAIL_KEYS:
        assert key in line, f"那一行少了 {key}（运维照它决定要不要手工补账）：{line[:220]}"
    assert "backend-entries=unproven" in line, f"读失败时不许假装知道账上有几条：{line[:220]}"
    # 本机控制台消息是 GBK 中文（"文件正由另一进程使用"），所以这条**不**按英文措辞判：
    # 判的是错误原文里带着 ReadAllBytes 这个方法名、带着那枚手搓假账的路径。
    assert "ReadAllBytes" in line, f"那一行没带上真实的错误原文：{line[:260]}"
    assert _PROBE_MARK in line, f"错误原文里少了那枚假账路径（说明抛的不是它）：{line[:300]}"
    assert line.split("error=", 1)[1].strip() != "", f"error= 是空的：{line[:200]}"
    assert got["returned"] == ["CALL_RETURNED"], "函数没有返回：调用点被中止了"
    assert got["done"] == ["SWAP-DONE"], f"退化之后没走到 SWAP-DONE（票面要求必须走到）：{out[:400]}"
    assert got["end"] == ["PROBE_END"]
    assert got["abort"] == [], f"清账这一步自己走了中止出口：{got['abort']}"


def test_machine_an_unwritable_ledger_says_the_budget_was_not_returned_and_keeps_going(budget_routes):
    """机器判据（SITE 1 的另一半）：读答得出来、写不下去（假账被置成只读）时打
    `state=write-threw` + `backend-entries=2`（那两条还留在账上，这次没能清掉），
    仍然走到 `SWAP-DONE`。这一档的 `backend-entries` 必须是**真数**而不是 0——
    把两种情况混成一个 token 就是票面不许的"只说 write failed"。"""
    rc, got, err, out = budget_routes["write-throws"]
    assert rc == 0, f"写被拒时竟然把运行打死（rc={rc}）：{err[:400]} | {out[:400]}"
    assert len(got["write_threw"]) == 1 and got["read_threw"] == [] and got["success"] == [], \
        f"token 档位不对：{got['budget']}"
    line = got["write_threw"][0]
    for key in ("budget-returned=false", "backend-entries=2", "error="):
        assert key in line, f"那一行少了 {key}（backend-entries=2 说的是那两条还在账上）：{line[:240]}"
    assert got["done"] == ["SWAP-DONE"], f"退化之后没走到 SWAP-DONE：{out[:400]}"
    assert got["end"] == ["PROBE_END"]


def test_machine_the_throwing_read_degrades_to_two_lines_and_never_exits_the_run(enabled_routes):
    """机器判据（票面 (b)：SITE 2 不是裸出口）：那一读一抛，这台机器打印
    `READFAIL stage=final-enabled-read error=CIM-PROBE-FAILURE...` +
    `ENABLED state=Unreadable`，并走到运行末尾（`CALL_RETURNED` / `PROBE_END`）。"""
    rc, got, err, out = enabled_routes["throws"]
    assert rc == 0, f"读一抛就把运行打死了（rc={rc}）：{err[:400]} | {out[:400]}"
    assert len(got["readfail"]) == 1, f"READFAIL 应当恰好一行：{out[:400]}"
    assert "CIM-PROBE-FAILURE" in got["readfail"][0], f"那一行没带真实错误原文：{got['readfail']}"
    assert len(got["enabled"]) == 1 and got["enabled"][0] == "ENABLED state=" + _UNREADABLE, \
        f"读答不了时 ENABLED 不是诚实的 Unreadable：{got['enabled']}"
    assert got["abort"] == [] and got["fail"] == [] and got["recipe"] == []
    assert got["returned"] == ["CALL_RETURNED"] and got["end"] == ["PROBE_END"]


def test_machine_the_answering_read_reports_ready_and_prints_no_readfail(enabled_routes):
    """正控（机器）：读答得出（桩给出 State=Ready）时 ENABLED 照旧打印、没有 READFAIL，
    且 step 8 那句启用确实在探针里跑过（接线证据）。"""
    rc, got, err, out = enabled_routes["answers"]
    assert rc == 0, f"answers 档 rc 非 0：{err[:300]} | {out[:300]}"
    assert got["readfail"] == [], f"读答出来了却打 READFAIL：{got['readfail']}"
    assert got["enabled"] == ["ENABLED state=Ready"], f"ENABLED 不再是脚本自己打的状态：{got['enabled']}"
    assert got["kv"].get("ENABLE_STUB_RAN") == "yes", \
        f"探针里 step 8 那句启用没跑到，等于没抄那段尾巴：{got['kv']}"
    assert got["returned"] == ["CALL_RETURNED"] and got["end"] == ["PROBE_END"]


def test_machine_a_read_that_answers_empty_still_produces_a_token(enabled_routes):
    """机器判据（"测表达式实际收到的那个对象"那一课的第二半）：`Get-ScheduledTask`
    **不抛而回空**（本机对假身份是抛 CimJobException，所以这一档由桩给）时，helper 走
    `task-missing` 档：`ENABLED state=Unreadable` 且没有 READFAIL（没有异常就没有 error=），
    运行继续走到末尾——**两种"看不了"都不许是裸出口**。"""
    rc, got, err, out = enabled_routes["empty"]
    assert rc == 0, f"empty 档 rc 非 0：{err[:300]} | {out[:300]}"
    assert got["readfail"] == [], f"这一档 helper 没抛，不该有 READFAIL：{got['readfail']}"
    assert got["enabled"] == ["ENABLED state=" + _UNREADABLE], \
        f"读到空对象时 ENABLED 不再是 Unreadable：{got['enabled']} / {out[:300]}"
    assert got["returned"] == ["CALL_RETURNED"] and got["end"] == ["PROBE_END"]


def test_machine_the_object_the_enabled_read_receives_is_one_value_not_a_count(tmp_path):
    """机器判据（#50 那一族的形状，量在**本票这一站**上）：helper 被赋值捕获时只交回
    **一个**对象（`items_from_helper=1`），那个对象 `is_array=False`、`type=PSCustomObject`、
    NoteProperty 齐 Enabled/State/Instances/ReadError/EnabledSource，而它的 `.Count` 在本机是
    `$null`——所以这一站的判决永远不许写成 count 形状，只许读 NoteProperty。"""
    if _POWERSHELL is None:
        _skip_machine()
    src = _shape_probe_text(PS1)
    assert src is not None, "helper 本体抄不进探针"
    path = _write_crlf(tmp_path, "probe-helper-shape.ps1", src)
    rc, out, err = _run_powershell(path)
    assert rc == 0, f"形状探针没跑完（rc={rc}）：{err[:400]} | {out[:400]}"
    kv = _parse(out)["kv"]
    assert kv.get("probe_done") == "yes", out[:300]
    assert kv["items_from_helper"] == "1", \
        f"helper 交回的对象数不是 1（多一行日志就会把属性读取变成猜）：{kv}"
    assert kv["shape_is_array"] == "False", f"helper 的输出散成了数组：{kv}"
    assert kv["shape_count"] == "$null", (
        f"本机裸 PSCustomObject 的 .Count 不再是 $null（今天实测就是 $null）：{kv}——"
        "这一档变了就说明有人把判决写成了 count 形状，那条判据会恒假")
    assert kv["shape_type"] == "PSCustomObject"
    for member in ("State", "ReadError", "EnabledSource", "Enabled", "Instances"):
        assert member in kv["shape_members"], f"helper 少带了 {member} 字段：{kv}"
    assert kv["state"] == _UNREADABLE and kv["enabled_source"] == "read-threw", \
        f"读一抛时 helper 的字段不再是 fail-closed 那一档：{kv}"
    assert int(kv["read_error_len"]) > 0, f"ReadError 是空的，那 error= 打什么：{kv}"


def test_machine_the_live_identity_answers_through_the_same_reader(tmp_path):
    """只读问这台机器一次：用**现网那个任务的真实身份**（按 ACTION 找，名字是中文）走
    脚本自己的 helper，确认这条读路对真任务答得出（`state` 不是 Unreadable、
    `enabled_source=settings-enabled`）——也就是本票换上的这条路**不会**把一次健康的换包
    判成 unreadable。全程只读，写任务/写文件/停进程的那批名字逐条断言不在探针源码里。"""
    if _POWERSHELL is None:
        _skip_machine()
    src = _shape_probe_text(PS1, live=True)
    assert src is not None, "只读真身份探针生不出来"
    for name in _TASK_WRITES + ("Enable-ScheduledTask", "Stop-Process", "Remove-Item",
                                "New-Item", "Set-Content", "Invoke-RestMethod"):
        assert name not in src, f"只读探针里出现了写形状：{name}"
    path = _write_crlf(tmp_path, "probe-live-identity-read.ps1", src)
    rc, out, err = _run_powershell(path)
    assert rc == 0, f"只读探针没跑通（rc={rc}）：{err[:300]} | {out[:300]}"
    kv = _parse(out)["kv"]
    assert kv.get("probe_done") == "yes", out[:300]
    assert kv["watchdog_count"] == "1", f"按 ACTION 找到的看门狗任务数不是 1：{kv}"
    assert int(kv["enumerated"]) > 50, f"任务枚举数少得不像真的枚举了：{kv}"
    assert kv["state"] != _UNREADABLE, f"真任务这一读答不了（helper 走了 read-threw 档）：{kv}"
    assert kv["enabled_source"] == "settings-enabled", \
        f"真任务上 Settings.Enabled 不再答话（a5d37bde 那一族回来了）：{kv}"
    assert kv["read_error_len"] == "0", f"真任务这一读竟然有错误：{kv}"


def test_machine_the_bare_task_read_of_a_fake_identity_is_a_real_throw(tmp_path):
    """票面"写的是测得的事实"：这台机器上
    `Get-ScheduledTask -TaskPath <假> -TaskName <假>` 抛的是
    `Microsoft.PowerShell.Cmdletization.Cim.CimJobException`——所以 SITE 2 那处洞**不是**假想。
    本条不跑脚本，只跑一枚最小只读表达式（手搓假身份，绝不问现网那个任务）。"""
    if _POWERSHELL is None:
        _skip_machine()
    src = (
        "$ErrorActionPreference = 'Stop'\r\n"
        "$TaskPath = '" + _FAKE_TASK_PATH + "'\r\n"
        "$TaskName = '" + _FAKE_TASK_NAME + "'\r\n"
        "$state = 'NOT-REACHED'\r\n"
        "$ex = ''\r\n"
        "try {\r\n"
        "    $state = [string] (Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName).State\r\n"
        "} catch { $ex = $_.Exception.GetType().FullName }\r\n"
        "'bare_state=' + $state\r\n"
        "'exception=' + $ex\r\n"
        "'probe_done=yes'\r\n")
    assert src.isascii()
    path = tmp_path / "probe-bare-task-read.ps1"
    path.write_bytes(src.encode("ascii"))
    assert path.read_bytes().count(b"\r\n") == src.count("\r\n"), "取证探针没按 CRLF 落盘"
    rc, out, err = _run_powershell(path)
    assert rc == 0 and out.count("probe_done=yes") == 1, f"只读取证探针没跑通（rc={rc}）：{err[:300]}"
    kv = _parse(out)["kv"]
    assert kv["bare_state"] == "NOT-REACHED", \
        f"这台机器上那一句裸读竟然答出了状态：{kv}——那本票这条取证要重写，先测再说"
    assert "CimJobException" in kv["exception"], f"抛的不是 CIM 那个异常类型，形状变了：{kv}"


def test_machine_both_parsers_agree_the_file_still_parses(tmp_path):
    """票面第 2 条要求的两路解析检查，由这台机器做：legacy `PSParser::Tokenize` 与
    `Language.Parser` AST 各 0 错；再喂一枚语法坏样本，两路都必须报红（否则这道门是装饰）。"""
    if _POWERSHELL is None:
        _skip_machine()
    good = _write_crlf(tmp_path, "parse-target-good.ps1", PS1)
    broken = PS1.replace("        return\n    }\n    $hadBom", "        return\n    $hadBom", 1)
    assert broken != PS1, "语法坏样本没造出来（读 try 的收尾花括号形状变了）"
    bad = _write_crlf(tmp_path, "parse-target-broken.ps1", broken)
    for label, path, expect in (("good", good, 0), ("broken", bad, 1)):
        src = (
            "$ErrorActionPreference = 'Continue'\r\n"
            "$toks = $null\r\n"
            "$perrs = $null\r\n"
            "[void][System.Management.Automation.Language.Parser]::ParseFile('"
            + path.as_posix() + "', [ref] $toks, [ref] $perrs)\r\n"
            "'ast_errors=' + @($perrs).Count\r\n"
            "'ast_tokens=' + @($toks).Count\r\n"
            "$errs = $null\r\n"
            "$lt = [System.Management.Automation.PSParser]::Tokenize("
            "(Get-Content -LiteralPath '" + path.as_posix() + "' -Raw), [ref] $errs)\r\n"
            "'legacy_errors=' + @($errs).Count\r\n"
            "'legacy_tokens=' + @($lt).Count\r\n"
            "'parse_done=yes'\r\n")
        assert src.isascii(), "解析门必须吃 ASCII 路径：本机 tmp 路径带中文就先失败在这里"
        runner = _write_crlf(tmp_path, "probe-parse-" + label + ".ps1", src)
        rc, out, err = _run_powershell(runner)
        assert rc == 0 and out.count("parse_done=yes") == 1, \
            f"{label} 解析探针跑不完：{err[:300]} | {out[:300]}"
        kv = _parse(out)["kv"]
        for key in ("ast_errors", "legacy_errors"):
            if expect == 0:
                assert kv[key] == "0", f"{label} 的 {key} 不是 0：{out[:400]}"
            else:
                assert int(kv[key]) >= 1, \
                    f"语法坏样本在 {key} 上竟然全绿：这道门是恒绿的装饰：{kv}"
        if expect == 0:
            assert int(kv["ast_tokens"]) > 1000 and int(kv["legacy_tokens"]) > 1000, \
                f"token 数少得不像解析了整份脚本：{kv}"


def test_machine_the_post_acceptance_abort_would_print_a_lie(tmp_path):
    """机器判据（票面第 1 条"不许改成中止"的证据本体）：抄脚本**真的** `Stop-AfterFailure`，
    在 step 8 走完之后的真状态（`backupDone=True`、`watchdogDisabled=False`）下调用它，
    这台机器念出来的正是 `ABORT stage=... moved=yes watchdog=left-disabled-by-design`
    + `FAIL stage=` + 配方，且 `Restore-WatchdogTask` 一次都没被调用。

    那一刻看守明明已经回来，`left-disabled-by-design` 就是假话——这就是本票两处站点一律退化、
    绝不中止的理由，由这台机器而不是由注释说出来。同一次探针还顺手证明那段尾巴在抛出之前
    就走到了 `CALL_RETURNED`：它自己没中止。
    """
    if _POWERSHELL is None:
        _skip_machine()
    src = _enabled_probe_text(PS1, template=_STOP_TEMPLATE)
    assert src is not None, "真 Stop-AfterFailure 抄不进探针（见 _enabled_probe_text）"
    assert "moved=yes watchdog=left-disabled-by-design" in src, \
        "探针里的 Stop-AfterFailure 不是脚本那份真本体（字面量对照没过）"
    assert "if ($script:backupDone) {" in src, "探针里缺了分支判据，等于没跑真函数"
    assert "RESTORE-STUB-CALLED" in src and "function Restore-WatchdogTask" in src, \
        "恢复路径必须换成喊一声的桩：真跑它就是真动现网计划任务"
    path = _write_crlf(tmp_path, "probe-real-stop-at-step-8.ps1", src)
    rc, out, err = _run_powershell(path)
    got = _parse(out)
    assert got["returned"] == ["CALL_RETURNED"], \
        f"那段尾巴竟然没先走到 CALL_RETURNED（本条的前提变了）：{out[:400]}"
    assert len(got["enabled"]) == 1, f"那段尾巴应先打印 ENABLED 再演示中止：{got['enabled']}"
    assert rc != 0, f"Stop-AfterFailure 的收尾是 throw，rc 不该为 0：{out[:200]}"
    assert len(got["abort"]) == 1 and "moved=yes watchdog=left-disabled-by-design" in got["abort"][0], \
        f"真函数体竟然没打印那句假话（本条的取证前提变了）：{got['abort']}"
    assert got["fail"] and got["recipe"], f"中止竟然没打印 FAIL/配方：{out[:300]}"
    assert got["restore"] == [] and "RESTORE-STUB-CALLED" not in out, \
        "backupDone 为真那条分支不该调恢复：调了就是开关有了第二个主人"
    assert got["end"] == [], f"throw 之后还走到了 PROBE_END，说明中止没收尾：{out[:300]}"


# ------------------------------------------------------------------ 牙印
def test_mutation_unwrapping_the_budget_read_turns_the_locks_red_and_the_machine_dies(tmp_path):
    """牙印一（票面"摘掉 budget try 就必须红"）：只撤**读**那半。

    静态侧：读那半的锁红、写那半的锁**照旧绿**（红要归对人）；路由侧这台机器当场演示：
    句柄挡住读 -> rc 非 0、没有任何 BUDGET token、没有 SWAP-DONE。
    """
    bare = _unwrap_budget_site(PS1, "read")
    assert bare is not None, "样本构造失败：读那半的 try 形状与本票假设不一致"
    code = _budget_code(bare)
    assert "    $bytes = [System.IO.File]::ReadAllBytes($StatePath)" in code
    assert _READ_TOKEN not in code, "本票的读失败 token 竟然还在函数体里：漂移没生效"
    assert _WRITE_TOKEN in code, "写那半必须保持原样"
    assert not _stmt_wrapped_and_degrades(bare, "read"), "撤掉读的 try，包裹+退化这条必须红"
    assert _stmt_wrapped_and_degrades(bare, "write"), "只动读的话，写那半的锁该保持绿"
    assert not _budget_fail_token_ok(bare), "那一行 read-threw token 没了，token 这条必须红"
    assert not _file_touches_all_wrapped(bare), "射程棘轮也必须红"
    assert not _no_stop_route_anywhere(bare) or not _reaches_the_tail_token(bare), \
        "撤掉那层保护后收尾两条锁居然全绿：说明它们没在判这两站点"
    copy = _write_crlf(tmp_path, "swap-package-bare-budget-read.ps1", bare)
    on_disk = copy.read_text(encoding="utf-8")
    assert on_disk.replace("\r\n", "\n") == bare, "副本没落盘：牙齿咬的是内存而不是文件"
    assert not _stmt_wrapped_and_degrades(on_disk, "read"), "坏样本落盘后判据应当照样红：红必须吃文件"
    if _POWERSHELL is None:
        _skip_machine()
    rc, got, err, out = _run_budget(bare, "read-throws", tmp_path, name="probe-budget-pre-fix-read.ps1")
    assert rc != 0, f"改动前那份竟然跑赢了被锁住的账本（rc={rc}）：{out[:300]}"
    assert got["budget"] == [], f"撤掉 try 之后这台机器竟然打了 BUDGET：{got['budget']}"
    assert got["done"] == [] and got["end"] == [], f"撤掉 try 之后竟然走到了 SWAP-DONE：{out[:300]}"
    assert "ReadAllBytes" in (out + err), f"抛出去的错误没留下痕迹：{err[:300]}"


def test_mutation_unwrapping_the_budget_write_turns_only_the_write_locks_red(tmp_path):
    """牙印二：只撤**写**那半（读那半保持绿）——两处洞各自独立可撤，
    这正是本票把两处分开包的第二条理由（第一条是失败原因不同，论证写在站点注释）。"""
    bare = _unwrap_budget_site(PS1, "write")
    assert bare is not None, "样本构造失败：写那半的 try 形状与本票假设不一致"
    code = _budget_code(bare)
    assert "    [System.IO.File]::WriteAllText($StatePath, $json, $enc)" in code
    assert _WRITE_TOKEN not in code and _READ_TOKEN in code
    assert not _stmt_wrapped_and_degrades(bare, "write"), "撤掉写的 try，那一半必须红"
    assert _stmt_wrapped_and_degrades(bare, "read"), "只动写的话，读那半的锁该保持绿"
    assert not _budget_fail_token_ok(bare), "write-threw 那行没了，token 这条必须红"
    assert not _file_touches_all_wrapped(bare), "射程棘轮也必须红"
    copy = _write_crlf(tmp_path, "swap-package-bare-budget-write.ps1", bare)
    assert not _stmt_wrapped_and_degrades(copy.read_text(encoding="utf-8"), "write"), "落盘之后照样要红"
    if _POWERSHELL is None:
        _skip_machine()
    rc, got, err, out = _run_budget(bare, "write-throws", tmp_path, name="probe-budget-pre-fix-write.ps1")
    assert rc != 0, f"改动前那份竟然扛住了只读属性的假账（rc={rc}）：{out[:300]}"
    assert got["budget"] == [] and got["end"] == [], \
        f"撤掉写的 try 之后这台机器却走完了：{out[:300]}"


def test_mutation_turning_the_budget_failure_into_an_abort_turns_the_no_abort_locks_red(tmp_path):
    """牙印三（票面明令的反面）：把 catch 收尾的 `return` 换成 `Stop-AfterFailure`。

    静态侧：退化锁与"不许中止"锁红，`_restore_ok` 照旧绿（被禁函数本体没坏），
    token 锁照旧绿（这枚样本只换收尾那一句）。路由侧这台机器再演示一遍：一次锁住的账本
    把这次**已验收**的换包按中止处理，屏幕上出现的正是那句假话，`SWAP-DONE` 没了。
    """
    abort = _abort_budget_site(PS1, "read")
    assert abort is not None, "中止样本构造失败：catch 收尾不是那一句 return"
    assert "Stop-AfterFailure 'budget-threw'" in _budget_code(abort)
    assert not _stmt_wrapped_and_degrades(abort, "read"), "catch 走中止，退化这条必须红"
    assert _stmt_wrapped_and_degrades(abort, "write"), "另一处没动，它的锁该保持绿"
    assert not _no_stop_route_anywhere(abort), "验收之后的站点了中止机器，这条必须红"
    assert _restore_ok(abort), "被调的那条出口函数本体没坏：红只能来自这两站点"
    assert _budget_fail_token_ok(abort), "这枚样本只换了收尾那一句，token 锁该保持绿（独立性）"
    copy = _write_crlf(tmp_path, "swap-package-budget-failure-aborts.ps1", abort)
    assert not _no_stop_route_anywhere(copy.read_text(encoding="utf-8")), "落盘之后照样要红"
    if _POWERSHELL is None:
        _skip_machine()
    rc, got, err, out = _run_budget(abort, "read-throws", tmp_path,
                                    name="probe-budget-aborts-instead-of-degrading.ps1")
    assert rc != 0, f"改成中止后探针竟然按 0 收口（rc={rc}）：{out[:300]}"
    assert got["end"] == [], f"改成中止却还走到末尾，说明本票的要求没被执行：{out[:300]}"
    assert got["abort"] and "moved=yes watchdog=left-disabled-by-design" in got["abort"][0], \
        f"中止样本没演示出那句假话：{out[:400]}"
    assert got["fail"] == ["FAIL stage=budget-threw"], f"FAIL 那一行不对：{got['fail']}"
    # 这枚样本只把收尾的 `return` 换成中止，所以那一行 token 仍然**先**打出去（catch 里就是
    # 先报后走）——丢的不是 token，是后面整条运行。于是这里判三件：成功那一行绝不许出现、
    # `SWAP-DONE` 不许出现、运行末尾不许出现。把"token 有没有"当判据的是上一条静态锁。
    assert got["success"] == [], f"走了中止却还声称清了账：{got['budget']}"
    assert got["done"] == [], f"走了中止却还走到 SWAP-DONE：{out[:300]}"


def test_mutation_reverting_the_enabled_read_to_the_bare_cmdlet_turns_the_locks_red(tmp_path):
    """牙印四（票面"revert SITE 2 to the bare read"）：尾巴退回
    `$finalState = (Get-ScheduledTask ...).State`。定位锁、helper 复用锁、诚实 token 锁、
    收尾锁各自红；这台机器演示那枚裸读一抛就把运行打死（没有 ENABLED、没有末尾）。"""
    bare = _to_pre_ticket_enabled(PS1)
    assert bare is not None, "样本构造失败：SITE 2 那一段的形状与本票假设不一致"
    tail = _enabled_code(bare)
    assert PRE_TICKET_ENABLED_BLOCK in tail and "$finalState" in tail
    assert _READFAIL_ENABLED not in tail, "READFAIL 那行还在 step 8 那段里：漂移没生效"
    assert _enabled_ctx(bare) is None, "裸读样本竟然还能被定位成合规：定位锁在判空集合"
    assert not _enabled_read_goes_through_the_helper(bare), "尾巴上有裸读，helper 复用这条必须红"
    assert not _enabled_token_stays_honest(bare), "READFAIL/Unreadable 都不在了，诚实 token 这条必须红"
    assert not _reaches_the_tail_token(bare), "SITE 2 不在位，收尾这条也必须红"
    assert not _no_stop_route_anywhere(bare), "同上：尾巴定位不到，收尾这条也该红"
    assert _registry_documents_the_tail(bare), \
        "这枚样本只动代码、不动文件头散文，登记表那条锁该保持绿（红要归给代码侧）"
    assert _enabled_read_site(bare) is not None and _read_site_ok(bare), \
        "helper 本体没被这枚样本碰过：那两把尺在样本上照旧绿（分工）"
    assert _single_value_helpers_ok(bare), "被赋值 helper 的输出形状在样本上照旧绿（分工）"
    copy = _write_crlf(tmp_path, "swap-package-bare-enabled-read.ps1", bare)
    assert not _enabled_read_goes_through_the_helper(copy.read_text(encoding="utf-8")), "落盘之后照样要红"
    if _POWERSHELL is None:
        _skip_machine()
    rc, got, err, out = _run_enabled(bare, "throws", tmp_path, region=PRE_TICKET_ENABLED_BLOCK,
                                     stage_required=False, name="probe-enabled-pre-fix.ps1")
    assert rc != 0, f"改动前那份竟然跑赢了这枚探针（rc={rc}）：{out[:300]}"
    assert got["enabled"] == [], f"裸读一抛竟然还打印了 ENABLED：{got['enabled']}"
    assert got["readfail"] == [] and got["returned"] == [] and got["end"] == [], \
        f"裸出口那一份应当什么都不剩：{out[:300]}"
    assert "CIM-PROBE-FAILURE" in (out + err), f"抛出去的错误没留下痕迹：{err[:300]}"


def test_mutation_token_without_the_budget_claim_turns_the_token_lock_red(tmp_path):
    """牙印五：把失败 token 退回到只说"这次没做成"的散文形状（票面明令不许的那种：
    要说清**预算没还**，不能只说 read/write failed）。包裹+退化那条锁必须照旧绿。"""
    for which in ("read", "write"):
        weaker = _weaken_token(PS1, which)
        assert weaker is not None, f"{which} 的 token 样本构造失败"
        # 只判**这一处**的 catch：另一处（write / read）那一行里合法地带着同一个键，
        # 拿整段函数体比会把没动过的那一半也算进漂移，红的就不是这一枚样本。
        assert "budget-returned=false" not in _budget_site(weaker, which)["handler_text"], \
            "漂移没生效：那一处 catch 里 budget-returned=false 还在"
        assert not _budget_fail_token_ok(weaker), f"{which} 那行不再说预算没还，token 锁必须红"
        assert _stmt_wrapped_and_degrades(weaker, which), \
            "这一枚样本只改了那一行的文字，包裹+退化那条锁该保持绿"
        assert _file_touches_all_wrapped(weaker)
        copy = _write_crlf(tmp_path, "swap-package-token-without-claim-" + which + ".ps1", weaker)
        assert not _budget_fail_token_ok(copy.read_text(encoding="utf-8")), "落盘之后照样要红"


def test_mutation_emptying_the_registry_lines_turns_the_registry_lock_red(tmp_path):
    """牙印六：登记表（文件头散文）里把这几条抹掉，登记表锁红、代码侧锁绿——
    证明两组判据各判各的范围，没有互相顶着。"""
    registry = _drop_registry_lines(PS1, "budget-returned=false")
    assert registry is not None, "登记表样本构造失败：那一行不在登记表区间里"
    assert not _registry_documents_the_tail(registry), "登记表里少了这两条新 token，必须红"
    assert _budget_fail_token_ok(registry), "只抹了散文，代码侧的 token 锁该保持绿"
    assert _stmt_wrapped_and_degrades(registry, "read") and _stmt_wrapped_and_degrades(registry, "write")
    stage = _drop_registry_lines(PS1, _READFAIL_ENABLED)
    assert stage is not None and not _registry_documents_the_tail(stage), \
        "登记表里没有 READFAIL stage=final-enabled-read 也应当红"
    assert _enabled_token_stays_honest(stage), "只抹散文，代码侧的 ENABLED 锁该保持绿"
    copy = _write_crlf(tmp_path, "swap-package-registry-emptied.ps1", registry)
    assert not _registry_documents_the_tail(copy.read_text(encoding="utf-8")), "落盘之后照样要红"


def test_mutation_encoding_gates_bit_on_written_copies(tmp_path):
    """牙印七：本文件的文件级门（票面第 2 条）必须**吃文件**。
    LF-only 副本、带中文注释的副本、带 BOM 的副本——三种都必须红；仓库那份必须绿。"""
    raw = SWAP_PS1.read_bytes()
    assert _ascii_ok(raw) and raw.count(b"\r\n") == raw.count(b"\n") > 0, "仓库这份已经坏了：先修它"
    lf = tmp_path / "copy-lf-only.ps1"
    lf.write_bytes(raw.replace(b"\r\n", b"\n"))
    body = lf.read_bytes()
    assert not (body.count(b"\r\n") > 0 and body.count(b"\r\n") == body.count(b"\n")), \
        "LF-only 副本竟然过了 CRLF 门：这道门没有吃文件"
    p2 = tmp_path / "copy-chinese-comment.ps1"
    p2.write_bytes(raw.replace(b"Write-Output 'SWAP-DONE'",
                               b"# \xe8\xbf\x90\xe7\xbb\xb4\xe5\xb7\xb2\xe7\x9c\x8b\r\nWrite-Output 'SWAP-DONE'", 1))
    assert p2.read_bytes() != raw, "中文注释样本没生效：SWAP-DONE 那一行不在文件里"
    assert not _ascii_ok(p2.read_bytes()), "塞进一行中文注释，ASCII 门必须红"
    assert not _ascii_ok(b"\xef\xbb\xbf" + raw), "BOM 也要门住：PS 5.1 的解码坑与 BOM 有关"
    assert not _ascii_ok(raw.replace(b"Write-Output 'SWAP-DONE'", b"Write-Output '\xe8\xbf\x90'", 1)), \
        "单个非 ASCII 字节也必须红"


# ------------------------------------------------------------------ 红绿分工
def _head_blob(ref: str = "HEAD"):
    """`git cat-file blob <ref>:deploy/swap-package.ps1` -> (LF blob, 按 CRLF 重建的同一份)。"""
    repo = str(SWAP_PS1.parents[1])
    if subprocess.run(["git", "cat-file", "-e", ref + ":deploy/swap-package.ps1"], cwd=repo).returncode != 0:
        pytest.skip(ref + " 里没有 deploy/swap-package.ps1 这一版")
    blob = subprocess.run(["git", "cat-file", "blob", ref + ":deploy/swap-package.ps1"],
                          cwd=repo, capture_output=True, check=True).stdout
    return blob, blob.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def test_pre_fix_reconstruction_is_green_on_the_existing_locks_and_red_on_ours(tmp_path):
    """红绿分工（票面第 4 条要求可复跑）：**按 CRLF 重建**的 HEAD 那份。

    取证：LF blob 的 字节/CRLF/LF/非 ASCII/sha256 与 CRLF 重建后的同样五项，外加 `HEAD~1`
    （上一版）blob 的大小——票面那个 60226 就是它，不是 HEAD 的。判据两路：本文件每条判据
    对它都红（洞不在改前那份里就不必修），既有六把尺照旧绿——少了这半条，就没人能说明
    本文件为什么必须存在，而不是往旧锁里加一条 risky。
    """
    lf, crlf = _head_blob()
    prev, _prev_crlf = _head_blob("HEAD~1")
    facts = {
        "HEAD_lf_bytes": len(lf), "HEAD_lf_crlf": lf.count(b"\r\n"), "HEAD_lf_lf": lf.count(b"\n"),
        "HEAD_lf_nonascii": sum(1 for b in lf if b >= 0x80),
        "HEAD_lf_sha": hashlib.sha256(lf).hexdigest(),
        "HEAD_crlf_bytes": len(crlf), "HEAD_crlf_crlf": crlf.count(b"\r\n"),
        "HEAD_crlf_lf": crlf.count(b"\n"), "HEAD_crlf_nonascii": sum(1 for b in crlf if b >= 0x80),
        "HEAD_crlf_sha": hashlib.sha256(crlf).hexdigest(),
        "HEAD1_lf_bytes": len(prev), "HEAD1_lf_crlf": prev.count(b"\r\n")}
    msg = "\n".join(f"{k}={v}" for k, v in facts.items())
    assert facts["HEAD_lf_crlf"] == 0, "改前那份 blob 竟然带 CRLF：autocrlf 前提变了，本函数要重写\n" + msg
    assert facts["HEAD_lf_lf"] == facts["HEAD_crlf_crlf"] == facts["HEAD_crlf_lf"], \
        "LF 行数与 CRLF 重建对不上\n" + msg
    assert facts["HEAD_crlf_nonascii"] == 0 and facts["HEAD_lf_nonascii"] == 0, msg
    assert not crlf.startswith(b"\xef\xbb\xbf"), msg
    copy = _write_crlf(tmp_path, "swap-package-head-crlf.ps1", crlf.decode("utf-8"))
    assert copy.read_bytes() == crlf, "CRLF 重建没落到盘上"
    assert hashlib.sha256(copy.read_bytes()).hexdigest() == facts["HEAD_crlf_sha"], msg
    pre = crlf.decode("utf-8")
    # 本票的判据：改前那份每条都红
    assert _budget_ctx(pre) is None, "改前那份的读/写没被包住，定位锁必须红（红才对）\n" + msg
    assert _budget_region(pre) is not None, "改前那份仍要能抄出函数体（机器样本靠它）\n" + msg
    assert not _stmt_wrapped_and_degrades(pre, "read"), msg
    assert not _stmt_wrapped_and_degrades(pre, "write"), msg
    assert not _budget_fail_token_ok(pre), "改前那份没有这两行 token，必须红\n" + msg
    assert not _file_touches_all_wrapped(pre), "改前那份两处文件调用都是裸的，射程棘轮必须红\n" + msg
    assert not _no_stop_route_anywhere(pre), "改前那份：收尾这条也必须红\n" + msg
    assert not _reaches_the_tail_token(pre), "改前那份：SWAP-DONE 收尾这条必须红\n" + msg
    assert _enabled_ctx(pre) is None, "改前那份没有 helper 读，SITE 2 定位锁必须红\n" + msg
    assert not _enabled_read_goes_through_the_helper(pre), "改前那份是裸读，必须红\n" + msg
    assert not _enabled_token_stays_honest(pre), "改前那份没有 READFAIL/Unreadable，必须红\n" + msg
    assert not _registry_documents_the_tail(pre), "改前那份的登记表没写这些，必须红\n" + msg
    # 既有锁看不见这条尾巴：照旧绿
    lines, lo, hi = _window(pre)
    assert lines is not None, "改前那份必须仍能定位 disable..backupDone 窗口，否则下面几条绿得没意义\n" + msg
    assert _verdict_ok(pre), "判决锁在改前那份上应当照旧绿\n" + msg
    assert _no_bare_exit_ok(pre), (
        "出口锁在改前那份上应当照旧绿：它管 disable..backupDone 那一段，本票两处都在那之后——"
        "分工错了就说明本文件在重复计同一件事\n" + msg)
    assert _restore_ok(pre), "恢复机制本体在改前那份里完好无损：缺的只是有人退化而不是中止\n" + msg
    assert _single_value_helpers_ok(pre), "被赋值 helper 的输出形状在改前那份上照旧绿\n" + msg
    assert _try_wraps_and_routes(pre) and _handler_asks_the_machine(pre) and _branches_and_failsafe(pre), \
        "#32 那三条在改前那份上应当照旧绿\n" + msg
    assert _enabled_read_site(pre) is not None and _read_site_ok(pre), \
        "helper 的判决属性路径在改前那份上还在（分工）\n" + msg
    # 这台机器看一眼改前那份的尾巴：裸读与收尾都还在原位
    if _POWERSHELL is None:
        _skip_machine()
    proc = subprocess.run([_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                           "Get-Content -LiteralPath '" + copy.as_posix() + "' -Tail 40"],
                          capture_output=True, timeout=PROBE_TIMEOUT_SECONDS)
    tail = (proc.stdout or b"").decode("utf-8", "replace")
    assert PRE_TICKET_ENABLED_BLOCK.splitlines()[0] in tail, \
        "改前那份的尾巴里那枚裸读不在了？本条取证的前提变了：\n" + tail[-900:]
    assert "Write-Output ('ENABLED state=' + $finalState)" in tail, \
        "改前那份的 ENABLED 那一行不再读 $finalState：\n" + tail[-900:]
    assert _READ_TOKEN not in tail and _READFAIL_ENABLED not in tail, \
        "改前那份的尾巴里竟然有了本票的 token：\n" + tail[-900:]
    assert "SWAP-DONE" in tail


def test_under_test_copy_runs_the_whole_file_locks(tmp_path):
    """票面第 4 条：牙印一律走**仓库外**副本（`SWAP_PS1_UNDER_TEST` 这条通道由
    `test_swap_package_disable_verify._target_ps1` 提供，本文件所有判据都吃文本参数，
    所以同一把尺在落盘副本上重跑即可）。

    本条不重开解释器（模块级 `PS1` 在 import 时就绑好了），而是把整套判据在**落盘副本**的
    文本上重跑一遍，并断言副本与工作树那份逐字节相同——既证明这条通道吃的是文件，
    也证明"还原"是真还原（动过的样本一律在 tmp_path，仓库那份的 `git diff` 由报告侧另证）。
    """
    copy = _write_crlf(tmp_path, "swap-package-under-test-restored.ps1", PS1)
    raw_copy = copy.read_bytes()
    assert raw_copy == SWAP_PS1.read_bytes(), "副本与工作树那份逐字节不同：还原没做干净"
    assert hashlib.sha256(raw_copy).hexdigest() == hashlib.sha256(SWAP_PS1.read_bytes()).hexdigest()
    text = copy.read_text(encoding="utf-8")
    assert _budget_ctx(text) and _enabled_ctx(text)
    for which in ("read", "write"):
        assert _stmt_wrapped_and_degrades(text, which)
    assert _budget_fail_token_ok(text) and _registry_documents_the_tail(text)
    assert _enabled_read_goes_through_the_helper(text) and _enabled_token_stays_honest(text)
    assert _no_stop_route_anywhere(text) and _reaches_the_tail_token(text)
    assert _file_touches_all_wrapped(text)
    assert _ascii_ok(raw_copy) and raw_copy.count(b"\r\n") == raw_copy.count(b"\n") > 0
