r"""换包脚本「等进程起来那一句轮询自己抛异常」这条路的控制流锁 + 真跑机器的路由锁（ticket #45）。

洞在哪（一句话版）：`deploy/swap-package.ps1` 起进程轮询里的 `$running = Get-LiveProcess`
**不在任何 try 里**。它走的是 CIM/WMI，而本仓库自己就写过两次这类查询会抛（#24 因此包住
了停进程那段，#32 因此包住了停用看门狗那句），脚本跑在 `$ErrorActionPreference = 'Stop'`
下且没有 `trap` / `finally`。于是这一句一抛，脚本就**没有 ABORT stage= token、没有对看守
做任何登记、也没有打印回滚配方**地死掉——而到那一刻为止，旧包早就被改名让位了
（`$script:backupDone` 在 step 4 就置真），看守也早就被这次运行按下去了。这是同一个洞的
第三个入口，且是 before-next-swap 阻塞项：下一次发版就是它的第一次实跑。

本文件钉四件事（票面四条判据，一条对应一组函数）：
(a) 轮询那句读**在**一个 try 里，且那条路最终落在脚本自己的出口上（`Stop-AfterFailure`），
    既不是裸 `throw` / `exit`，也不是「悄悄当没事」；
(b) 那段 handler **不写计划任务**（不许出现 Enable-/Disable-ScheduledTask），也不新起第二条
    恢复路径（不许自己调 `Restore-WatchdogTask`）——开关只有一个主人；
(c) 中止的 stage token 存在且是**一行一个**机器可解析的形状，stage 名沿用 `disable-threw-*`
    那一族（`boot-poll-threw`），与既有 `started-check` 严格分家；
(d) 摘掉这层保护就必须红（`test_mutation_*`），包括把文件退回本票之前那八行裸形状，
    以及证明「改动前那一份对既有三套锁照旧绿」的红绿分工。

**为什么不能只有文本锁**：本票真正要确认的两件事都是控制流事实，文本判不了——
①「抛出去之后实际打印了哪几行」，②「改名之后的中止到底有没有把看守扶回去」。
所以本文件像 `test_swap_package_watchdog_enabled_machine_truth.py` 那样生成探针跑 PowerShell，
但问的不是任务属性而是**路由**：探针把脚本自己的轮询那一段**原文抄进**生成的 .ps1，只把
三个外部依赖换成桩（会抛的 `Get-LiveProcess`、绝不联网的 `Get-HealthOnce`、打印一行
SENTINEL 就收口的 `Stop-AfterFailure`），然后看这台机器实际走到哪一步。六档答案的分工
全部由机器判：一直抛 → `SENTINEL stage=boot-poll-threw`；抛一次之后答出两个进程 → 没有
SENTINEL，`$livePid` 带着 4242 破出循环并一路走到 HARNESS_DONE（保护没有把正常路径吞掉）；一次都没抛但确实没进程 →
走既有的 `stage=started-check`；**先抛、后面答到「没进程」** → 仍然 `started-check`，因为
「看不见进程」与「看不了」是两种判决（这一例正是本票最容易被糊过去的地方）；另有两档答的是
**恰好一个进程**（ticket #50 补的，理由见下面那一节），它们要求轮询第一次读到就破出循环。
第五条探针再往前一步：它抄的是脚本**真的** `Stop-AfterFailure` 与 `Show-Rollback` 函数体，
只把 `Restore-WatchdogTask` 换成会喊一声的桩，于是「改名之后中止打印哪几行、看守到底有没有
被这次运行扶回去」由这台机器回答而不是由注释回答——本票要的正是这个确认：
`$script:backupDone` 为真时打印的是 `moved=yes watchdog=left-disabled-by-design` 加配方，
**不**是 `re-enabled`，且 `Restore-WatchdogTask` 根本不被调用（探针断言那声喊从未出现）。
把恢复硬塞进这一段的「好意」改法，会被 (b) 与本条探针同时咬住。

安全边界（票面硬约束，探针必须配得上）：全程只读。探针不调用任何会动任务的 cmdlet
（接线锁断言 `Enable-ScheduledTask` / `Disable-ScheduledTask` 在探针源码里根本不出现，
路由探针再走一遍 `_no_write_cmdlets`），不碰 `127.0.0.1:8000`（`Get-HealthOnce` 在探针里
是返回 `$null` 的桩，且断言探针源码里既没有 `Invoke-RestMethod` 也没有 `127.0.0.1`——
现网就在这个端口上服务），不碰 `dist\`、不碰 `data\`，跑在 `tmp_path`（仓库外）。
牙印样本一律落**仓库外**的副本并经 `SWAP_PS1_UNDER_TEST` 复跑整文件（作者留这个变量的
本意见 `test_swap_package_disable_verify._target_ps1`）。

**ticket #50 补进来的那一档（原用例集合自己的洞）**：上面那四种答案把进程数只喂过
**0 与 2**（`_CASES` 里 procs 的取值），**从没喂过 1** —— 而 1 才是现网的常态：换包时
起来的就是一个 `run_backend.exe`。这不是顺手的疏漏，是本票的另一半：PowerShell 是
**逐元素枚举函数输出**的，`Get-LiveProcess` 里那句 `@( ... )` 在结果恰好一个时到调用侧
会散成**裸 CimInstance**，而裸 CimInstance 答 `.Count` 给的是 `$null`（本机实测：`int` 答 1、
`$null` 答 0，CimInstance 是例外，所以这事不能靠直觉推）。于是本票之前那句
`if ($running.Count -ge 1)` 在「恰好一个进程」时恒假：轮询永远破不出循环，每次换包烧完
整个 `$StartTimeoutSeconds`，而 `STARTED pid=` 那个数是下面 /health 哨兵给的 -1，不是循环
找到的 pid。所以 `_CASES` 现在多两档：`transient-then-one`（与 #45 作者当年那句
「抛一次之后答出进程就能认出 pid」同形状，只是只有一个进程）和 `answers-one`（一次都不抛、
就一个进程，也就是现网的正常路径）。这两档在**改动前那一份**上都由这台机器判红（livePid 停在
0、判决被送到 `stage=started-check`），改好后判绿；而「那个对象到底答不答 `.Count`」这件
机器形状的事，由 `test_swap_package_boot_poll_single_process_machine_truth.py` 问机器，
不靠本文件的文本判据。少了 1 这一档，#45 那 17 条对坏代码和对本代码同样绿——正是
`test_swap_package_watchdog_enabled_machine_truth.py` 说过的那类「文本锁证不了机器形状」。

CRLF 陷阱（上一次审查员踩过的）：本机 `core.autocrlf=true`，`git show HEAD:deploy/...` 给出
的是 **LF** blob；拿它当「改动前那份」喂给文件自带的 ASCII/CRLF 判据，会先红在格式上，
从而把「改前也全绿」这种假结论坐实。本文件所有副本一律按 CRLF 落盘（`_write_crlf`），
并当场断言 `CRLF 数 == LF 数`。另外注意 `Path.read_text()` 走 universal newlines，
`PS1` 里的行尾是 `\n`，所以样本锚点用 `\n`，落盘时才补 CRLF。

尺子从既有三份锁借（本仓规矩：一把尺子只有一份）：`_code_lines` / `_idx` / `_try_spans` /
_span_of / `_func_body` / `_window` / `_target_ps1` 与 `_verdict_ok` / `_no_bare_exit_ok` /
`_restore_ok` 来自 `test_swap_package_disable_verify.py`（`_restore_ok` 就是「Stop-AfterFailure
两条分支各打印什么」那把尺，本票复用而非另刻），`_find_powershell` / `_no_write_cmdlets` /
`PROBE_TIMEOUT_SECONDS` 来自 `test_swap_package_watchdog_enabled_machine_truth.py`，
`_try_wraps_and_routes` / `_handler_asks_the_machine` / `_branches_and_failsafe` 来自
`test_swap_package_disable_threw_handler.py`（只用它们做**分工证据**）。

没有 `powershell.exe` 就 skip 机器那几条，静态那几条照跑照判（惯例同上面两份锁）。
"""
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_swap_package_disable_threw_handler import (
    _branches_and_failsafe,
    _handler_asks_the_machine,
    _try_wraps_and_routes,
)
from tests.test_swap_package_disable_verify import (
    _code_lines,
    _func_body,
    _idx,
    _no_bare_exit_ok,
    _restore_ok,
    _span_of,
    _target_ps1,
    _try_spans,
    _verdict_ok,
    _window,
)
from tests.test_swap_package_watchdog_enabled_machine_truth import (
    PROBE_TIMEOUT_SECONDS,
    _find_powershell,
    _no_write_cmdlets,
)

SWAP_PS1 = _target_ps1()
PS1 = SWAP_PS1.read_text(encoding="utf-8")

# 本票认下来的中止 stage 与每次抛留下的 token（沿用文件既有那把，不新造语法）
_STAGE = "boot-poll-threw"
_READFAIL = "READFAIL stage=boot-poll"

# 轮询那一段的锚点。区间的两端取**顶格**形状：这段在主流程 0 列上，
# 缩进过的写法不算定位到了它（同 disable-verify 用顶格认 disable 那一步的理由）。
_BOOT_OPEN = re.compile(r"^\$livePid = 0$")
_BOOT_DEADLINE = re.compile(r"^\$bootDeadline = \(Get-Date\)\.AddSeconds\(\$StartTimeoutSeconds\)$")
_LOOP = re.compile(r"^while \(\(Get-Date\) -lt \$bootDeadline\) \{$")
_SLEEP = re.compile(r"^\s*Start-Sleep -Seconds 2$")
_READ = re.compile(r"^\s*\$running = (?:@\()?Get-LiveProcess")
_STARTED = re.compile(r"^Write-Output \('STARTED pid='")
_EMPTY_IF = re.compile(r"^if \(\$livePid -eq 0\) \{$")
_TRY_OPEN = re.compile(r"^\s*try\s*\{$")
_CATCH_OPEN = re.compile(r"^\s*\}\s*catch\s*\{$")
_CONTINUE = re.compile(r"^\s*continue\s*$")
_BARE_EXIT = re.compile(r"^\s*(throw|exit)\b")
_SAFETY_LINE = re.compile(r"^\s*Stop-AfterFailure\s+(?P<stage>'[A-Za-z0-9-]+')")
_IF_OPEN = re.compile(r"^\s*if \(?[^{]*\{?\s*$")
_CAPTURE_ERR = re.compile(r"^\s*(?P<var>\$\w+) = \$_.Exception\.Message\s*$")
_CLEAR_ERR = re.compile(r"^\s*(?P<var>\$\w+) = ''\s*$")
_READFAIL_LINE = re.compile(r"^\s*Write-Output \('" + re.escape(_READFAIL) + r" fails='")
_ENABLE_CALL = re.compile(r"^\s*Enable-ScheduledTask -TaskPath")
_TASK_WRITE = re.compile(r"Enable-ScheduledTask|Disable-ScheduledTask")
_NEW_TIMER = re.compile(r"Start-Sleep|AddSeconds|New-TimeSpan|\bwhile\b|\bfor\b")

# 本票之前那八行裸形状（照 HEAD 原文的形状，含顶格与缩进）：牙印一与「改动前那份」都用它
PRE_TICKET_BLOCK = """$livePid = 0
$bootDeadline = (Get-Date).AddSeconds($StartTimeoutSeconds)
while ((Get-Date) -lt $bootDeadline) {
    Start-Sleep -Seconds 2
    $running = Get-LiveProcess
    if ($running.Count -ge 1) {
        $livePid = (@($running)[0]).ProcessId
        break
    }
}"""

# 机器探针的答案档位：case -> (前几次读抛, 答出几个进程, 期望走到的 stage)。
# 期望值不是猜的，理由见模块 docstring 的分工说明。
# **进程数必须包含 1**（ticket #50）：本文件原来只有 0 与 2，而现网换包时起的就是一个
# run_backend.exe——恰好 1 个时函数输出散成裸 CimInstance、`.Count` 是 $null，那句
# `$running.Count -ge 1` 恒假，轮询破不出循环。0 与 2 两档看不出这个形状（0 走 $null
# 答 0、2 走数组答 2），所以原来的四种答案对坏代码同样绿。机器形状本身由
# test_swap_package_boot_poll_single_process_machine_truth.py 问这台机器。
_CASES = {
    "always-throws": (99, 0, "boot-poll-threw"),
    "transient-then-two": (1, 2, None),
    "transient-then-one": (1, 1, None),
    "answers-one": (0, 1, None),
    "answers-empty": (0, 0, "started-check"),
    "throws-then-empty": (1, 0, "started-check"),
}

# 探针一次跑约 4 秒（$StartTimeoutSeconds = 4，循环体里那颗原有的 Start-Sleep 2 定节奏）。
_HARNESS_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
$case = $args[0]
$StartTimeoutSeconds = 4
$LiveExe = 'X:/not-a-real-package/run_backend.exe'
$ExpectedVersion = 'v9.9.9'
$Port = 1
$script:failFor = 0
$script:procs = 0
$script:reads = 0
switch ($case) {
    'always-throws' { $script:failFor = 99; $script:procs = 0 }
    'transient-then-two' { $script:failFor = 1; $script:procs = 2 }
    'transient-then-one' { $script:failFor = 1; $script:procs = 1 }
    'answers-one' { $script:failFor = 0; $script:procs = 1 }
    'answers-empty' { $script:failFor = 0; $script:procs = 0 }
    'throws-then-empty' { $script:failFor = 1; $script:procs = 0 }
    default { 'UNKNOWN-CASE ' + $case; exit 3 }
}
function Get-LiveProcess {
    # Shape note (ticket #50): this returns @(pipeline) exactly like the real
    # Get-LiveProcess, so with procs=1 PowerShell's element-by-element function
    # output leaves the caller a BARE object, and a bare object here answers
    # .Count as $null - measured on this box for both a bare PSCustomObject and a
    # bare Win32_Process CimInstance. The production TYPE is what
    # test_swap_package_boot_poll_single_process_machine_truth.py asks the machine
    # about; this harness only has to reproduce the flattening.
    $script:reads = $script:reads + 1
    if ($script:reads -le $script:failFor) { throw 'CIM-PROBE-FAILURE cannot enumerate Win32_Process' }
    if ($script:procs -le 0) { return @() }
    @(1..$script:procs | ForEach-Object { [pscustomobject]@{ ProcessId = 4241 + $_ } })
}
function Get-HealthOnce { return $null }
function Stop-AfterFailure {
    param([string] $FailedAt, [string] $Message)
    'SENTINEL stage=' + $FailedAt + ' msg=' + $Message
    exit 0
}
__REGION__
'HARNESS_DONE livePid=' + $livePid + ' reads=' + $script:reads
"""

# 第五条探针：抄脚本**真的** Stop-AfterFailure 与 Show-Rollback 函数体，只把
# Restore-WatchdogTask 换成喊一声的桩（真跑它 = 真碰现网那个计划任务，票面明令禁止）。
_REAL_STOP_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
$Live = 'X:/not-a-real-package/dist/run_backend'
$LiveExe = 'X:/not-a-real-package/dist/run_backend/run_backend.exe'
$ProjectRoot = 'X:/not-a-real-package'
$Backup = 'X:/not-a-real-package/dist/run_backend_old-19700101-000000'
$stamp = '19700101-000000'
$Port = 1
$task = [pscustomobject]@{ TaskPath = '\fake\' ; TaskName = 'fake-task' }
$script:backupDone = $true
$script:watchdogDisabled = $true
function Restore-WatchdogTask { 'RESTORE-CALLED' }
__REAL_SHOW_ROLLBACK__
__REAL_STOP_AFTER_FAILURE__
Stop-AfterFailure '__STAGE__' 'probe: the boot poll could not read the process list'
'REAL_MACHINERY_REACHED_END'
"""


def _delta(line: str) -> int:
    return line.count("{") - line.count("}")


def _block_end(lines: list, start: int, hi: int):
    """从 lines[start]（一颗 `{` 的开行）走到与它配平的那一行；不配平返回 None。"""
    depth = 0
    for i in range(start, hi + 1):
        depth += _delta(lines[i])
        if depth <= 0 and i > start:
            return i
    return None


def _boot_region(text: str):
    """只按**顶格锚点**切出轮询那一段（不要求里面有 try）：机器探针与牙印样本都用它。

    分成两层是有原因的：改动前那一份没有 try，`_poll_ctx` 对它返回 None 是对的
    （判据因此一律红），但「把裸形状喂给真探针、看它怎么死」那条牙印仍需要它的原文段。
    """
    lines = _code_lines(text)
    if len(lines) != len(text.splitlines()):
        return None
    opens = [i for i, ln in enumerate(lines) if _BOOT_OPEN.match(ln)]
    deadlines = [i for i, ln in enumerate(lines) if _BOOT_DEADLINE.match(ln)]
    loops = [i for i, ln in enumerate(lines) if _LOOP.match(ln)]
    reads = [i for i, ln in enumerate(lines) if _READ.match(ln)]
    ends = [i for i, ln in enumerate(lines) if _STARTED.match(ln)]
    if not (len(opens) == len(deadlines) == len(loops) == len(reads) == len(ends) == 1):
        return None
    lo, deadline, loop, read, started = (opens[0], deadlines[0], loops[0], reads[0], ends[0])
    if not (lo < deadline < loop < read < started):
        return None
    return {"lines": lines, "lo": lo, "deadline": deadline, "loop": loop, "read": read,
            "started": started, "region_text": "\n".join(lines[lo:started])}


def _poll_ctx(text: str):
    """定位本票那一段的全部锚点；任何一环不唯一 / 不配平 / 顺序不对就返回 None（判据一律红）。"""
    reg = _boot_region(text)
    if reg is None:
        return None
    lines = reg["lines"]
    lo, deadline, loop, read, started = (reg["lo"], reg["deadline"], reg["loop"],
                                         reg["read"], reg["started"])
    sleeps = [i for i, ln in enumerate(lines) if _SLEEP.match(ln)]
    span = _span_of(_try_spans(lines, lo, started), read)
    if span is None or not _TRY_OPEN.match(lines[span[0]]) or not (span[0] < read <= span[1]):
        return None
    s, e = span
    catch = _idx(_CATCH_OPEN.pattern, lines, s, e)
    if catch is None or catch >= e:
        return None
    handler = lines[catch + 1:e]
    if not handler:
        return None
    loop_end = _block_end(lines, loop, started)
    if loop_end is None or read > loop_end or catch > loop_end:
        return None
    emptyifs = [i for i, ln in enumerate(lines) if _EMPTY_IF.match(ln)]
    if not emptyifs or not (loop_end < emptyifs[0] < started):
        return None
    return {"lines": lines, "lo": lo, "deadline": deadline, "loop": loop, "loop_end": loop_end,
            "sleeps": sleeps, "read": read, "span": span, "catch": catch, "handler": handler,
            "handler_text": "\n".join(handler), "empty_if": emptyifs[0], "started": started,
            "region_text": reg["region_text"],
            "try_body": lines[s + 1:catch],
            "post_text": "\n".join(lines[loop_end + 1:started])}


def _error_var(text: str):
    """catch 里那句「第一时间把 `$_` 转成具名变量」的变量名（不写死 `$bootPollError`：
    名字改了判据仍然认，**没有**这一句才必须红）。"""
    ctx = _poll_ctx(text)
    if ctx is None:
        return None
    hits = [m.group("var") for m in (_CAPTURE_ERR.match(ln) for ln in ctx["handler"]) if m]
    return hits[0] if len(hits) == 1 else None


def _giveup_block(text: str):
    """轮询之后那段「窗口最后一次的读仍然没答话就中止」的 if 块。

    条件必须同时看 `$livePid` 与 catch 记下错误的那个变量；块内必须**恰好**一句本票
    stage 的 `Stop-AfterFailure`；那句必须摆在块自己的 0 层嵌套里（嵌进任何依赖 /health
    读数的子块里都算不合格——那会把票面不许的「静默吞掉失败」伪装成判决）。
    """
    ctx = _poll_ctx(text)
    err = _error_var(text)
    if ctx is None or err is None:
        return None
    lines = ctx["lines"]
    safety = [i for i in range(ctx["loop_end"], ctx["started"])
              if _SAFETY_LINE.match(lines[i])
              and _SAFETY_LINE.match(lines[i]).group("stage") == "'" + _STAGE + "'"]
    if len(safety) != 1:
        return None
    i_abort = safety[0]
    cond = None
    for i in range(ctx["loop_end"] + 1, i_abort):
        if _IF_OPEN.match(lines[i]) and "$livePid" in lines[i] and err in lines[i] \
                and _delta(lines[i]) > 0:
            cond = i
            break
    if cond is None:
        return None
    end = _block_end(lines, cond, ctx["started"])
    if end is None or i_abort > end:
        return None
    depth = 0
    for ln in lines[cond + 1:i_abort]:
        depth += _delta(ln)
    return {"start": cond, "end": end, "abort": i_abort, "err": err,
            "cond_line": lines[cond], "inner_depth": depth,
            "text": "\n".join(lines[cond:end + 1]),
            "body_text": "\n".join(lines[cond + 1:end])}


# ------------------------------------------------------------------ 判据本体
def _read_wrapped_and_recorded(text: str) -> bool:
    """(a) 那句读在 try 里；catch 把错误记成具名变量、收尾是 continue、不裸出口，
    也不自己再开一个计时器（上界只能是这个轮询本来就有的 $bootDeadline）。"""
    ctx = _poll_ctx(text)
    if ctx is None:
        return False
    if _error_var(text) is None:
        return False
    handler = ctx["handler"]
    if any(_BARE_EXIT.match(ln) for ln in handler):
        return False
    if not _CONTINUE.match(handler[-1]):
        return False
    if _NEW_TIMER.search(ctx["handler_text"]):
        return False
    _no_write_cmdlets(ctx["handler_text"], "轮询 handler 本体")
    return True


def _give_up_routes_to_the_abort_machinery(text: str) -> bool:
    """(a) 的后半 + (c)：give-up 走的是那个**打印 ABORT/FAIL/ROLLBACK 并拥有看守归属**的
    出口函数，消息带上记下的错误，判决条件里不许出现 `$_`。"""
    blk = _giveup_block(text)
    if blk is None:
        return False
    if "$_" in blk["cond_line"]:
        return False
    if blk["err"] not in blk["text"]:
        return False
    if "Stop-AfterFailure '" + _STAGE + "'" not in blk["text"]:
        return False
    if any(_BARE_EXIT.match(ln) for ln in blk["text"].splitlines()):
        return False
    # 复用既有那把尺，而不是另刻一把：被调用的那个函数本体仍然打印两条分支各自的 token
    # （moved=yes 那条明说看守是**有意**留在停用态的），并且恢复只住在 Restore-WatchdogTask。
    return _restore_ok(text)


def _handler_scope(text: str):
    """本票射程内的两段可执行文本：轮询 try 的 catch 本体 + give-up 中止块。"""
    ctx = _poll_ctx(text)
    blk = _giveup_block(text)
    if ctx is None or blk is None:
        return None
    return ctx["handler_text"] + "\n" + blk["text"]


def _handler_never_writes_the_task(text: str) -> bool:
    """(b) 开关只有一个主人：本票这段既不写任务，也不新起第二条恢复路径；
    全文件的 `Enable-ScheduledTask` 仍然恰好两处（恢复机制一处 + step 8 计划内启用一处）。"""
    ctx = _poll_ctx(text)
    scope = _handler_scope(text)
    if ctx is None or scope is None:
        return False
    if _TASK_WRITE.search(scope):
        return False
    if "Restore-WatchdogTask" in scope:
        return False
    lines = ctx["lines"]
    sites = [i for i, ln in enumerate(lines) if _ENABLE_CALL.match(ln)]
    if len(sites) != 2:
        return False
    restore = _func_body(text, "Restore-WatchdogTask")
    if restore is None or not any(_ENABLE_CALL.match(ln) for ln in restore):
        return False
    r_start = _idx(r"^function\s+Restore-WatchdogTask\s*\{", lines)
    if r_start is None:
        return False
    r_end = _block_end(lines, r_start, len(lines) - 1)
    if r_end is None:
        return False
    for i in sites:
        if r_start <= i <= r_end:
            continue
        # 另一处必须是计划内的 step 8 启用，位置在本票这段之后（早于它就是提前启用）
        if i <= ctx["started"]:
            return False
    # 兜底那把公共尺也过一遍（红的理由由它说）
    _no_write_cmdlets(scope, "轮询 handler 与中止块")
    return True


def _retry_is_bounded_by_the_existing_window(text: str) -> bool:
    """(a) 的另一半：一次抛当「再问一次」，但上界与节奏都用这个轮询**本来就有**的东西。

    钉四件：循环条件仍是那句 `while ((Get-Date) -lt $bootDeadline)`；上限仍来自
    `-StartTimeoutSeconds`；抛之后靠循环体里那颗原有的 `Start-Sleep -Seconds 2` 定节奏
    （catch 自己不许再睡、再开窗、再嵌套循环）；答到一次就把错误清掉——否则
    「看不见进程」会被本票误判成「看不了」，把既有 started-check 那条判决抢走。
    """
    ctx = _poll_ctx(text)
    if ctx is None:
        return False
    err = _error_var(text)
    if err is None:
        return False
    if len([ln for ln in ctx["lines"] if _LOOP.match(ln)]) != 1:
        return False
    if not _BOOT_DEADLINE.match(ctx["lines"][ctx["deadline"]]):
        return False
    if not any(ctx["loop"] < i <= ctx["loop_end"] for i in ctx["sleeps"]):
        return False
    if _NEW_TIMER.search(ctx["handler_text"]):
        return False
    body = ctx["try_body"]
    cleared = [m.group("var") for m in (_CLEAR_ERR.match(ln) for ln in body) if m]
    if cleared != [err]:
        return False
    return True


def _readfail_token_ok(text: str) -> bool:
    """(c) 每一次抛留**一行** READFAIL（括号在一行里配平、带上错误变量、不自报结论），
    且「看不了」与「没看见」两条判决各用自己的 stage。"""
    ctx = _poll_ctx(text)
    blk = _giveup_block(text)
    if ctx is None or blk is None:
        return False
    hits = [i for i, ln in enumerate(ctx["handler"]) if _READFAIL_LINE.match(ln)]
    if len(hits) != 1:
        return False
    line = ctx["handler"][hits[0]]
    if line.count("(") != line.count(")"):
        return False
    if _error_var(text) not in line or "fails=" not in line:
        return False
    if re.search(r"(verdict|aborted|swallow|branch)=", line):
        return False
    stages = [m.group("stage") for m in
              (_SAFETY_LINE.match(ln) for ln in ctx["lines"] if _SAFETY_LINE.match(ln))]
    if stages.count("'" + _STAGE + "'") != 1:
        return False
    if "'started-check'" not in stages:
        return False
    return True


def _witness_is_report_only(text: str) -> bool:
    """票面第 4 条要求的反面教材由这条钉住：中止前那一次 /health 只是**报告字段**。

    钉三件：整段里 `Get-HealthOnce` 至多调一次；give-up 的条件行里不许出现那个读数；
    中止调用摆在块自己的 0 层嵌套里（判决不依赖那次读数）。少了这条，
    「顺手让 /health 救一下」就会把本票变成静默吞掉失败的那一类——票面明令不许。
    """
    blk = _giveup_block(text)
    if blk is None:
        return False
    if blk["text"].count("Get-HealthOnce") > 1:
        return False
    if "$pollWitness" in blk["cond_line"]:
        return False
    if blk["inner_depth"] != 0:
        return False
    return True


# ------------------------------------------------------------------ 生成的探针
def _build_harness(region_text: str, stage_required: bool = True):
    """把脚本自己那段轮询原文塞进探针；校验不过一律 None（绝不退回去跑「另一段」）。"""
    if not region_text or not region_text.strip():
        return None
    if "$running = Get-LiveProcess" not in region_text and "$running = @(Get-LiveProcess" not in region_text:
        return None
    if "STARTED pid=" in region_text:
        return None
    if stage_required and ("Stop-AfterFailure '" + _STAGE + "'") not in region_text:
        return None
    text = _HARNESS_TEMPLATE.replace("__REGION__", region_text)
    if region_text not in text or "__REGION__" in text or not text.isascii():
        return None
    # 探针绝不联网：现网就服务在这台机器的 127.0.0.1:8000 上
    if "Invoke-RestMethod" in text or "127.0.0.1" in text:
        return None
    if _TASK_WRITE.search(text):
        return None
    _no_write_cmdlets(text, "轮询路由探针")
    return text


def _harness_for(text: str, stage_required: bool = True):
    reg = _boot_region(text)
    if reg is None:
        return None
    return _build_harness(reg["region_text"], stage_required)


def _build_real_stop_text(text: str):
    """抄脚本**真的** Stop-AfterFailure 与 Show-Rollback 函数体（Restore 换成喊桩）。"""
    stop = _func_body(text, "Stop-AfterFailure")
    show = _func_body(text, "Show-Rollback")
    if stop is None or show is None:
        return None
    stop_text = "\n".join(stop)
    show_text = "\n".join(show)
    if "ABORT stage=" not in stop_text or "ROLLBACK backup-package=" not in show_text:
        return None
    out = (_REAL_STOP_TEMPLATE
           .replace("__REAL_STOP_AFTER_FAILURE__", stop_text)
           .replace("__REAL_SHOW_ROLLBACK__", show_text)
           .replace("__STAGE__", _STAGE))
    if stop_text not in out or show_text not in out or "__" in out or not out.isascii():
        return None
    if _TASK_WRITE.search(out):
        return None
    return out


_POWERSHELL = _find_powershell()


def _skip_machine() -> None:
    pytest.skip("这台机器没有 powershell.exe，路由那几条跑不了。静态那几条不受影响、照判；"
                "但本票「摘掉保护之后脚本到底怎么死」与「改名之后的中止打印哪几行」是这台"
                "机器判的，放行复跑必须在 Windows 现网机上做（理由同机器真相锁那一份）。")


def _run_powershell(path: Path, arg=None):
    cmd = [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]
    if arg is not None:
        cmd.append(arg)
    proc = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS,
                          cwd=str(path.parent))
    # 按字节收再 errors="replace" 解：PowerShell 5.1 把 throw 的整段横幅写进 stderr，
    # 那里面带控制台代码页（本机 GBK）的中文字节，严格 utf-8 会把两路都读成空串。
    return (proc.returncode, (proc.stdout or b"").decode("utf-8", "replace"),
            (proc.stderr or b"").decode("utf-8", "replace"))


def _parse(out: str):
    lines = [ln.strip() for ln in out.splitlines()]
    return {"sentinel": [ln for ln in lines if ln.startswith("SENTINEL stage=")],
            "readfail": [ln for ln in lines if ln.startswith(_READFAIL)],
            "started": [ln for ln in lines if ln.startswith("STARTED pid=")],
            "done": [ln for ln in lines if ln.startswith("HARNESS_DONE")],
            "abort": [ln for ln in lines if ln.startswith("ABORT stage=")],
            "fail": [ln for ln in lines if ln.startswith("FAIL stage=")],
            "rollback": [ln for ln in lines if ln.startswith("ROLLBACK")]}


def _run_case(text: str, case: str, work: Path):
    harness = _harness_for(text)
    assert harness is not None, "探针源码生不出来：轮询那段抄不出原文（见 _build_harness）"
    path = work / ("probe-boot-poll-" + case + ".ps1")
    path.write_bytes(harness.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii"))
    assert path.read_text(encoding="ascii") == harness, "探针没落盘：牙齿咬的是内存而不是文件"
    rc, out, err = _run_powershell(path, case)
    assert rc == 0, f"探针 {case} 没有按桩收口跑完（rc={rc}）：{err.strip()[:300]} | {out[:300]}"
    return _parse(out)


def _write_crlf(tmp_path: Path, name: str, text: str) -> Path:
    """副本按 **CRLF** 落盘：本机 core.autocrlf=true，`git show` 给的是 LF blob，
    拿它喂文件自带的格式门会先红在格式上，把「改前也全绿」这种假结论坐实。"""
    body = text.replace("\r\n", "\n").replace("\n", "\r\n")
    path = tmp_path / name
    path.write_bytes(body.encode("utf-8"))
    raw = path.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n"), "副本必须是纯 CRLF：见函数说明"
    assert not any(b >= 0x80 for b in raw), "副本必须保持纯 ASCII：PS 5.1 的解码坑"
    return path


def _to_pre_ticket_shape(text: str):
    """把本票那一段整块换回**改动前**那八行裸形状。

    切的是**原文行**（`_code_lines` 与原文行数一一对应，所以同一套下标两边都成立），
    这样除这一段之外文件其余部分与仓库那份逐字节一致——牙印要证明的是「摘掉这一层的
    保护」，不是「顺便注释也没了」。
    """
    ctx = _poll_ctx(text)
    if ctx is None:
        return None
    lines = ctx["lines"]
    end = ctx["empty_if"]
    if end <= ctx["lo"]:
        return None
    raw = text.splitlines()
    kept = raw[:ctx["lo"]] + PRE_TICKET_BLOCK.splitlines() + raw[end:]
    out = "\n".join(kept)
    region = "\n".join(kept[ctx["lo"]:ctx["lo"] + len(PRE_TICKET_BLOCK.splitlines())])
    # 判据只看这一段：文件头的 token 清单是散文，摘掉代码之后它仍然在场，
    # 拿它当「样本没生效」的判据会把这个构造永久卡死。
    if _STAGE in region or _READFAIL in region or "try {" in region:
        return None
    if "$running = Get-LiveProcess" not in region:
        return None
    return out


@pytest.fixture(scope="module")
def routed(tmp_path_factory):
    """`_CASES` 每档各跑一次真探针，结果整个模块共用（生成一次、六次 powershell 启动，
    约 24 秒；每档约 4 秒，就是 $StartTimeoutSeconds 那颗窗口）。"""
    if _POWERSHELL is None:
        _skip_machine()
    work = tmp_path_factory.mktemp("boot-poll-routing")
    return {case: _run_case(PS1, case, work) for case in _CASES}


# ------------------------------------------------------------------ 静态锁
def test_poll_read_is_locatable_and_the_locks_are_not_judging_an_empty_set():
    """前提锁：try / catch / give-up 块 / stage 各自必须**恰好**定位得到——
    「锚点找不到」与「锚点找到了但内容不对」必须是两种红法。"""
    ctx = _poll_ctx(PS1)
    assert ctx is not None, (
        "轮询那句 `$running = @(Get-LiveProcess ...)` 没有被 try 包住（或 try 与循环、循环与 STARTED "
        "的相对位置不对）：本票要堵的就是这个——裸的那一句在 $ErrorActionPreference='Stop' "
        "下会把一次已经改完名的换包判死在没有 ABORT stage=、没有看守登记、没有回滚配方的地方")
    s, e = ctx["span"]
    assert s < ctx["read"] <= e, f"try 范围没有包住那句读：try@{s + 1}..{e + 1} read@{ctx['read'] + 1}"
    assert _error_var(PS1) is not None, "catch 里没有「第一时间把 $_ 转成具名变量」那一句"
    blk = _giveup_block(PS1)
    assert blk is not None, "轮询之后的 give-up 块定位不到（判法见 _giveup_block）"
    assert blk["inner_depth"] == 0, "中止调用被嵌进了子块里：判决就不再是无条件的了"
    assert ctx["loop_end"] < blk["start"] < blk["end"] < ctx["started"], "give-up 不在轮询与 STARTED 之间"


def test_the_boot_poll_read_sits_in_a_try_that_records_the_error_and_never_bare_exits():
    assert _read_wrapped_and_recorded(PS1), (
        "轮询那句读不在 try 里，或 catch 留了裸 throw/exit、自己又开了计时器/嵌套循环、"
        "或者没把错误记成具名变量：一抛就又回到本票要堵的那条静默死法（#24/#32 同族）")


def test_the_give_up_aborts_through_the_function_that_prints_the_abort_and_owns_the_guard():
    assert _give_up_routes_to_the_abort_machinery(PS1), (
        f"窗口最后一次的读仍然没答话时没有走 Stop-AfterFailure '{_STAGE}'，或消息里没带上"
        "记下的错误变量，或 give-up 条件里还在用 $_，或被调的那条出口函数本体已经不再打印"
        "ABORT/FAIL/ROLLBACK 两条分支（复用 _restore_ok 那把尺）")


def test_the_handler_never_writes_the_task_and_no_second_restore_path_appears():
    assert _handler_never_writes_the_task(PS1), (
        "轮询的 handler 或它的中止块自己动手写了计划任务，或新起了一条 Restore-WatchdogTask "
        "之外的恢复路径，或全文件的 Enable-ScheduledTask 不再恰好两处（恢复机制一处 + step 8 "
        "计划内启用一处）——两处拥有一个开关就是第二个洞")


def test_the_retry_uses_the_window_the_loop_already_owns_and_clears_it_on_an_answer():
    assert _retry_is_bounded_by_the_existing_window(PS1), (
        "把一次抛当「再问一次」的那层保护不再吃这个轮询本来就有的 $bootDeadline 与那句 "
        "Start-Sleep -Seconds 2（新计时器 / 嵌套循环 / while ($true) 都算不合格），"
        "或者答到一次之后没有把错误清掉——后者会把「看不见进程」误判成「看不了」")


def test_each_raise_prints_one_readfail_line_and_the_two_verdicts_keep_their_own_stage():
    assert _readfail_token_ok(PS1), (
        f"{_READFAIL} 那行不在、不止一处、括号没在一行里配平、没带错误变量或 fails=、"
        f"自己报了结论，或者 {_STAGE} 与 started-check 两条判决不再各用自己的 stage"
        "（一抛就变成「打印一句其实没说清是看不了还是没看见」的中止）")


def test_the_health_witness_on_the_give_up_is_a_field_and_never_the_verdict():
    assert _witness_is_report_only(PS1), (
        "中止前那一次 /health 变成了判决的一部分（give-up 条件引用了它，或中止调用被嵌进"
        "依赖它的子块里）：本票要的是「两个事实都说出来」，不是「顺手让 /health 把这次失败"
        "抹掉」——抹掉就是票面明令不许的静默吞失败")


# ------------------------------------------------------------------ 机器锁
def test_probe_is_wired_to_the_scripts_own_poll_lines_and_cannot_touch_production():
    """接线锁 + 只读锁：探针跑的就是脚本抄出来的那一段，而且它问不到现网头上。

    没有这一条，机器那几条完全可以靠「另抄一段更好看的代码」恒绿；而真脚本里的
    `Get-HealthOnce` 打的就是 `http://127.0.0.1:8000/health`——那台进程此刻正在服务。
    """
    harness = _harness_for(PS1)
    assert harness is not None, "探针源码生不出来（_build_harness 的校验没过）"
    ctx = _poll_ctx(PS1)
    assert ctx is not None
    for needle in ("$running = @(Get-LiveProcess", "} catch {", "$_.Exception.Message",
                   _READFAIL, "Stop-AfterFailure '" + _STAGE + "'",
                   "while ((Get-Date) -lt $bootDeadline) {", "$bootPollError = ''"):
        assert needle in harness, f"脚本自己的那一行没有原样落到探针里：{needle!r}"
    assert ctx["region_text"] in harness, "探针跑的不是脚本那一段原文"
    assert "function Get-HealthOnce { return $null }" in harness, "探针必须用桩替换 /health 那次真读"
    assert "Invoke-RestMethod" not in harness and "127.0.0.1" not in harness, \
        "探针里出现了真 /health 探针的形状：现网就在这台机器的 8000 端口上"
    assert harness.isascii(), "生成的探针必须纯 ASCII：PS 5.1 会把带中文的无 BOM .ps1 错解码成语法错误"
    _no_write_cmdlets(harness, "轮询路由探针")
    assert not _TASK_WRITE.search(harness), "探针里出现了写计划任务的 cmdlet"


def test_machine_routes_the_six_answers_the_way_the_stages_claim(routed):
    """机器判据：`_CASES` 那六档在**这台机器**上真的各走各的路（分工见模块 docstring）。

    「认出进程」那一档的 READFAIL 行数不再写死 1，而是等于该档设定前几次抛（fail_for）——
    新增的 `answers-one` 一次都不抛，写死 1 只会让这条锁在好代码上假红。
    """
    for case, (fail_for, procs, expect_stage) in _CASES.items():
        got = routed[case]
        if expect_stage is None:
            assert got["sentinel"] == [], f"{case} 不该中止，却拿到了 {got['sentinel']}"
            assert len(got["done"]) == 1 and got["done"][0].startswith("HARNESS_DONE livePid=4242"), (
                f"{case}（{procs} 个进程）应当由轮询认出进程、破出循环并一路走到头"
                f"（livePid=4242）；这台机器判成 {got['done']}——轮询没认出那一个进程的话，"
                "pid 就只剩 /health 哨兵那个 -1，而本档的桩根本不联网")
            assert len(got["readfail"]) == fail_for, \
                f"{case} 应当留下恰好 {fail_for} 行 READFAIL：{got}"
            assert len(got["started"]) == 0, "探针的区间止于 STARTED 那一行之前，这里只该看到 livePid"
            continue
        assert len(got["sentinel"]) == 1, f"{case} 应当恰好一次中止收口：{got}"
        assert got["sentinel"][0].startswith("SENTINEL stage=" + expect_stage + " "), \
            f"{case} 本该走 stage={expect_stage}，这台机器判成了 {got['sentinel'][0][:140]!r}"
        assert got["started"] == [], f"{case} 中止了还打印 STARTED，说明桩没接住：{got}"


def test_machine_the_single_process_answer_breaks_the_poll_instead_of_burning_the_window(routed):
    """机器判据（ticket #50 的那一档）：**恰好一个进程**时轮询必须当场破出循环。

    窗口 4 秒、循环体里那颗原有的 `Start-Sleep -Seconds 2` 定节奏，所以整个窗口最多问两次：
      * `answers-one`（一次都不抛、就一个进程 = 现网换包的正常路径）读到**第一次**就破出，
        reads=1；改动前那份（`$running.Count -ge 1`，裸 CimInstance 的 `.Count` 是 $null）
        在这里永远破不出，两次都问完、livePid 停在 0、判决被送到 `stage=started-check`。
      * `transient-then-one`（先抛一次再答出一个进程，正是 #45 作者那句「抛一次之后答出
        进程就能认出 pid」的形状）reads=2：第一次抛留一行 READFAIL，第二次认出进程。
    这一条是「进程数只喂 0 与 2」那套用例看不见的洞的机器证据：0 走 $null 答 0、2 走数组
    答 2，两档对坏代码同样绿；只有 1 会散成裸对象。对象形状为什么是 $null 由
    test_swap_package_boot_poll_single_process_machine_truth.py 问机器。
    """
    one = routed["answers-one"]
    assert one["done"] == ["HARNESS_DONE livePid=4242 reads=1"], (
        "一次都不抛、恰好一个进程时，轮询应当在第一次读到就认出 pid 并破出循环；"
        f"reads=2 就是没破出、把 4 秒窗口烧完（ticket #50 那个洞的形状）：{one}")
    assert one["sentinel"] == [], f"轮询认出了进程却还走中止出口，判决被抢走了：{one['sentinel']}"
    assert one["readfail"] == [], f"这一档一次都没抛却打出 READFAIL：{one}"

    transient = routed["transient-then-one"]
    assert transient["done"] == ["HARNESS_DONE livePid=4242 reads=2"], (
        "先抛一次再答出一个进程：应当是第二次读到就破出（抛的那次不算读到）："
        f"{transient}")
    assert len(transient["readfail"]) == 1, f"那一次抛要留下一行证据：{transient}"
    assert transient["sentinel"] == [], (
        f"#45 作者那句「抛一次之后答出进程就能认出 pid」的说法，在**一个进程**的形状上必须"
        f"成立，否则本票的洞又回来了：{transient['sentinel']}")



def test_machine_the_give_up_carries_the_last_real_error_and_says_cannot_look(routed):
    """机器判据：give-up 那一行带上最后一次真实错误原文，并说清是「看不了」不是「没看见」。"""
    got = routed["always-throws"]
    assert len(got["sentinel"]) == 1
    msg = got["sentinel"][0]
    assert msg.startswith("SENTINEL stage=boot-poll-threw ")
    assert "CIM-PROBE-FAILURE" in msg, f"中止消息里没有最后一次错误原文：{msg[:220]}"
    assert "cannot look" in msg, f"中止消息没把「看不了」说出来（那就是把两种判决混成一种）：{msg[:220]}"
    assert "health=silent" in msg, f"中止消息少了 /health 那个报告字段：{msg[:220]}"
    n = len(got["readfail"])
    assert n >= 2, f"整个窗口每次都抛，READFAIL 应当至少两行，实得 {n}：{got}"
    assert f"{n} failed reads" in msg, f"中止消息里的抛的次数与 READFAIL 行数对不上账：{msg[:220]}"
    assert got["readfail"][-1].startswith(_READFAIL + " fails=" + str(n))


def test_machine_an_answered_read_keeps_the_existing_started_check_verdict(routed):
    """机器判据：先抛后答（答的是「没有进程」）仍由既有 started-check 收口——本票不抢判决。

    这条是本票最容易被人糊过去的地方：错误不清掉的话「看不见进程」就被误报成「看不了」，
    而这两条判决的回滚含义完全不同（前者还有 /health 第二判据兜着，后者没有）。
    """
    got = routed["throws-then-empty"]
    assert len(got["sentinel"]) == 1 and got["sentinel"][0].startswith("SENTINEL stage=started-check "), \
        f"先抛后答却被本票抢走了判决：{got['sentinel']}"
    assert len(got["readfail"]) == 1, f"那一次抛仍然要留一行证据，但不能改变判决：{got}"
    assert "boot-poll-threw" not in got["sentinel"][0]
    assert routed["answers-empty"]["sentinel"][0].startswith("SENTINEL stage=started-check ")
    assert routed["answers-empty"]["readfail"] == [], "一次都没抛却打出 READFAIL：读数是在编的"


def test_machine_the_post_backup_abort_prints_the_recipe_and_deliberately_leaves_the_guard_down(
        tmp_path):
    """机器判据·真出口函数本体：改名之后中止**实际**打印的那几行，由这台机器念出来。

    抄的是脚本真的 `Stop-AfterFailure` 与 `Show-Rollback`，只把 `Restore-WatchdogTask`
    换成一声 `RESTORE-CALLED` 的桩（真跑它就要真动现网那个任务，票面明令禁止）。
    断言四件：ABORT 那一行正是 `moved=yes watchdog=left-disabled-by-design`（不是
    `re-enabled`）；FAIL 与 ROLLBACK 配方各自成行；那次恢复**根本没被调用**；脚本以 throw
    收场（rc 非 0）。这就是票面「确认你到底会拿到哪几个 token」的机器证据。
    """
    if _POWERSHELL is None:
        _skip_machine()
    src = _build_real_stop_text(PS1)
    assert src is not None, "真的 Stop-AfterFailure / Show-Rollback 函数体抄不进探针"
    assert "function Restore-WatchdogTask { 'RESTORE-CALLED' }" in src, \
        "恢复本体必须换成喊一声的桩：真跑它就是真动现网计划任务"
    assert not _TASK_WRITE.search(src), "探针里出现了写计划任务的 cmdlet"
    assert src.isascii()
    path = tmp_path / "probe-boot-poll-real-stop.ps1"
    path.write_bytes(src.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii"))
    rc, out, err = _run_powershell(path)
    got = _parse(out)
    assert rc != 0, f"Stop-AfterFailure 收尾是 throw，rc 不该为 0：{out[:200]}"
    assert "RESTORE-CALLED" not in out, (
        "backupDone 为真那条分支竟然调了恢复：本票的 stage 会「假装」看守已经回来了，"
        "而配方第一步明明白白写着先别启用")
    assert got["abort"] == ["ABORT stage=boot-poll-threw moved=yes watchdog=left-disabled-by-design"], \
        f"ABORT 那一行不是预期的形状：{got['abort']} / {out[:300]}"
    assert got["fail"] == ["FAIL stage=boot-poll-threw"], f"FAIL 那一行不对：{got['fail']}"
    assert any(ln.startswith("ROLLBACK backup-package=X:/not-a-real-package") for ln in got["rollback"]), \
        f"没有打印带真实备份路径的 ROLLBACK 配方：{got['rollback'][:3]}"
    assert "REAL_MACHINERY_REACHED_END" not in out, "throw 没收尾，中止之后还继续往下跑了"


# ------------------------------------------------------------------ 牙印
def _drift_replace(text: str, old: str, new: str, label: str) -> str:
    assert old in text, f"漂移样本没生效：{label} 的锚点不在文件里"
    drifted = text.replace(old, new, 1)
    assert drifted != text, f"漂移样本没生效：{label} 一个字节都没改"
    return drifted


def test_mutation_unwrapping_the_poll_read_turns_the_new_locks_red(tmp_path):
    """牙印一（票面要求的「摘掉保护就必须红」+ 红绿分工）：回到本票之前那八行裸的。

    分工的证据同样在这一条里：**改动前那一份对既有三套锁全绿**（#24 的出口锁、#32 的
    disable handler 三条、恢复机制锁都看不见这个洞），只有本文件的判据红。少了这半条，
    就没人能说明为什么本文件必须存在，而不是往旧文件里加一条 risky。
    样本按 CRLF 落盘到 tmp_path（仓库外），证明判据吃的是文件而不是内存里的幻想。
    """
    bare = _to_pre_ticket_shape(PS1)
    assert bare is not None, "样本构造失败：本票那一段的形状与 PRE_TICKET_BLOCK 假设不一致了"
    copy = _write_crlf(tmp_path, "swap-package-pre-ticket-bare-boot-poll.ps1", bare)
    on_disk = copy.read_text(encoding="utf-8")
    assert on_disk.replace("\r\n", "\n") == bare, "副本没落盘：牙齿咬的是内存而不是文件"
    reg = _boot_region(bare)
    assert reg is not None, "样本必须仍能按顶格锚点定位到轮询那一段"
    assert _STAGE not in reg["region_text"] and _READFAIL not in reg["region_text"], \
        "可执行的轮询那一段里本票的 stage 与 token 都该没了（文件头那段散文留着是对的，" \
        "它是 token 清单，不是判据吃的那段代码）"
    assert "try {" not in reg["region_text"], "样本里那句读竟然还被 try 包着：漂移没生效"

    assert not _read_wrapped_and_recorded(on_disk), "把 try 摘掉，包裹+记录这条锁必须红"
    assert not _give_up_routes_to_the_abort_machinery(on_disk), "give-up 没了，路由这条必须红"
    assert not _handler_never_writes_the_task(on_disk), "handler 都不在了，只读这条也必须红"
    assert not _retry_is_bounded_by_the_existing_window(on_disk), "重试层没了，上界这条必须红"
    assert not _readfail_token_ok(on_disk), f"{_READFAIL} 那行没了，token 这条必须红"
    assert not _witness_is_report_only(on_disk), "give-up 块没了，报告字段这条也必须红"
    assert _harness_for(on_disk) is None, "样本里本票 stage 都不在了，路由探针应当生不出来（不许静默跑另一段）"

    # 红绿分工：改动前那一份在既有三套锁上照旧绿——洞不在它们的射程里
    lines, lo, hi = _window(bare)
    assert lines is not None, "样本必须仍能定位 disable..backupDone 窗口，否则下面三条绿得没有意义"
    assert _verdict_ok(bare), "判决锁在裸形状上应当照旧绿"
    assert _no_bare_exit_ok(bare), (
        "出口锁在裸形状上应当照旧绿：它管的是 disable 到 backupDone 那一段，而本票的洞在"
        " backupDone 之后——红绿分工错了就说明本文件与旧锁在重复计同一件事")
    assert _restore_ok(bare), "恢复机制本体在样本里完好无损：缺的只是有人调用它"
    assert _try_wraps_and_routes(bare) and _handler_asks_the_machine(bare) and _branches_and_failsafe(bare), \
        "#32 那三条在裸形状上应当照旧绿：本票是从第三行进来的同一个洞"


def test_machine_the_pre_fix_copy_dies_bare_with_no_token_at_all(tmp_path):
    """牙印一的另一半（**机器**证据）：把改动前那八行原样喂给真探针，它死得无声无息。

    这条才是本票的分量所在：不是「文本没匹配上」，而是这台机器当场演示——读一抛，
    没有 SENTINEL、没有 ABORT、没有 STARTED，只剩一个非 0 的退出码。
    """
    if _POWERSHELL is None:
        _skip_machine()
    bare = _to_pre_ticket_shape(PS1)
    assert bare is not None, "样本构造失败"
    harness = _harness_for(bare, stage_required=False)
    assert harness is not None, "改动前那段应当仍能抄进探针（它没有本票的 stage，探针照跑）"
    assert "$running = Get-LiveProcess" in harness and "} catch {" not in harness
    path = _write_crlf(tmp_path, "probe-boot-poll-pre-fix.ps1", harness)
    assert path.read_text(encoding="utf-8").replace("\r\n", "\n") == harness
    rc, out, err = _run_powershell(path, "always-throws")
    assert rc != 0, f"改动前那份竟然「跑成功」了：探针没真的把读抛出去（rc={rc}）：{out[:300]}"
    got = _parse(out)
    assert got["sentinel"] == [], f"改动前那份竟然走到了中止出口：{got['sentinel']}"
    assert got["abort"] == [] and got["rollback"] == [], "改动前那份竟然打印了 ABORT/ROLLBACK"
    assert got["started"] == [], f"改动前那份竟然认出了进程：{got['started']}"
    assert "CIM-PROBE-FAILURE" in (out + err), f"抛出去的错误没留下任何痕迹：{err[:200]}"


def test_mutation_that_enables_the_task_in_the_handler_turns_the_write_lock_red(tmp_path):
    """牙印二：handler 自己动手 `Enable-ScheduledTask`「把看守扶回来」——只读这条必须红。

    这正是本票最容易被写错的「好意」改法：改名之后的中止里硬启用看守，既跟配方第一步冲突
    （先别启用，等验收），又把开关的主人从一处变成两处。
    """
    assert _handler_never_writes_the_task(PS1), "真文件必须先绿"
    blk = _giveup_block(PS1)
    assert blk is not None
    direct = _drift_replace(
        PS1, "    Stop-AfterFailure '" + _STAGE + "' $pollWhy",
        "    Enable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null\n"
        "    Stop-AfterFailure '" + _STAGE + "' $pollWhy",
        "改成 handler 直接启用任务")
    assert not _handler_never_writes_the_task(direct), "自己动手启用任务的样本必须红"
    assert _read_wrapped_and_recorded(direct), (
        "样本只动中止块，包裹那条锁应当照旧绿：红要归给「自己动手写任务」，不能两条混在一起")
    copy = _write_crlf(tmp_path, "swap-package-handler-writes-the-task.ps1", direct)
    assert not _handler_never_writes_the_task(copy.read_text(encoding="utf-8")), \
        "落盘之后仍然必须红：判据吃的是文件"
    scope = _handler_scope(direct)
    assert scope is not None, "样本里 give-up 块定位不到了：牙印的红的理由说不清"
    with pytest.raises(AssertionError) as exc:
        _no_write_cmdlets(scope, "动手启用任务的样本")
    assert "Enable-ScheduledTask" in str(exc.value), f"红的理由必须点名那个 cmdlet：{exc.value}"


def test_mutation_silent_catch_or_bare_exit_in_the_handler_turns_the_locks_red(tmp_path):
    """牙印三/四：catch 改成「悄悄当没事」或「就地 throw」，记录与 token 两条各自红。

    前者 = 票面明令不许的静默吞失败（看守照样被留在地上，屏幕上还一片绿）；
    后者 = 本票要堵的那条裸死法换了个位置重演一遍。
    """
    silent = _drift_replace(
        PS1,
        "        $bootPollFails = $bootPollFails + 1\n"
        "        $bootPollError = $_.Exception.Message\n"
        "        Write-Output ('READFAIL stage=boot-poll fails=' + $bootPollFails + ' error=' + $bootPollError)\n"
        "        continue",
        "        continue",
        "把 catch 改成静默 continue")
    assert not _read_wrapped_and_recorded(silent), "catch 不再记录错误，这条锁必须红"
    assert not _readfail_token_ok(silent), "catch 不再留一行 token，token 这条必须红"
    copy = _write_crlf(tmp_path, "swap-package-silent-catch.ps1", silent)
    assert not _read_wrapped_and_recorded(copy.read_text(encoding="utf-8")), \
        "静默样本落盘后判据应当照样红：红必须是吃文件的"

    bare_throw = _drift_replace(PS1, "        continue", "        throw $bootPollError",
                                "把 catch 改成裸 throw")
    assert _poll_ctx(bare_throw) is not None, "样本仍要定位得到（这条牙印只换最后一句）"
    assert not _read_wrapped_and_recorded(bare_throw), \
        "catch 里留裸 throw：本票堵的就是裸出口，换个位置重演也必须红"


def test_mutation_unbounded_retry_or_a_stolen_verdict_turns_the_bound_locks_red(tmp_path):
    """牙印五/六：把重试变成无限轮询，或者把「答到就清错误」那句摘掉，上界与分工各自红。

    前者就是「不要变成 unbounded retry」这条硬要求的反面；后者让「看不见进程」被误报成
    「看不了」——本票不许抢走既有 started-check 的判决。
    """
    unbounded = _drift_replace(PS1, "while ((Get-Date) -lt $bootDeadline) {",
                               "while ($true) {", "把轮询改成无限")
    assert not _retry_is_bounded_by_the_existing_window(unbounded), "无限轮询必须红"
    copy = _write_crlf(tmp_path, "swap-package-unbounded-retry.ps1", unbounded)
    assert not _retry_is_bounded_by_the_existing_window(copy.read_text(encoding="utf-8")), \
        "无限轮询样本落盘后应当照样红"

    never_clear = _drift_replace(PS1, "        $bootPollError = ''",
                                 "        $bootPollFails = $bootPollFails",
                                 "答到之后不清错误")
    assert not _retry_is_bounded_by_the_existing_window(never_clear), \
        "答到一次之后不清错误，重试与分工这条必须红（否则 started-check 会被抢走）"
    assert _read_wrapped_and_recorded(never_clear), \
        "这条牙印只动 try 体内那句清零，包裹锁该保持绿：红要归给上界与分工那条"
