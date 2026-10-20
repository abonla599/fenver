r"""**机器形状锁**（ticket #50）：问这台机器——「换包轮询实际拿到的那个对象，答不答 `.Count`」。

洞在哪（一句话版）：`deploy/swap-package.ps1` 起进程轮询里那句判决，本票之前是
`if ($running.Count -ge 1)`，而 `$running = Get-LiveProcess`。`Get-LiveProcess` 自己把流水线
包在 `@( ... )` 里，看着像「一定给回一个数组」——但 **PowerShell 是逐元素枚举函数输出的**：
结果恰好只有一个元素时，调用侧拿到的是**裸 CimInstance**，而**裸 CimInstance 答 `.Count`
给的是 `$null`**。于是「恰好一个 `run_backend.exe`」——换包时的常态——那条判决恒假：
轮询永远破不出循环、每次换包烧完整个 `$StartTimeoutSeconds`，最后 `STARTED pid=` 那个数是
/health 哨兵给的 -1，而不是这个循环本该找到的 pid。循环声称的职责（找到 pid）是**静默死的**。

本机实测（2026-10-17，PS 5.1.26100.9549，现网 pid 126256 一个进程在跑，只读探针）：

    一个进程 : is_array=False  count=[]   ($running.Count -ge 1)=False  @($running) 有 1 个元素
    零个进程 : is_null=True    count=[0]  ($running.Count -ge 1)=False
    两个进程 : is_array=True   count=[2]  ($running.Count -ge 1)=True

**这不是「标量行为」，别按直觉推**：同一个位置，`int` 的 `.Count` 答 1、`$null` 的 `.Count`
答 0（也都实测过），**CimInstance 是那个例外**。这正是 #45 的实现员与审查员都读不出问题、
而只有拿一个进程去问机器才暴露的原因。

为什么必须问机器、文本锁不够：`test_swap_package_boot_poll_threw_handler.py` 的 `_CASES`
原来只喂 **0 与 2** 个进程，从没喂过 **1**——而 0 与 2 两档上「裸写法」和「再包一层 `@()`
的写法」判决**完全一样**（0 走 $null 答 0、2 走数组答 2），只有 1 会散成裸对象。所以那 17 条
锁对坏代码和对本代码同样绿：它锁的是**形状之外的东西**（try 包没包、抛出去走到哪个 stage、
看守归谁）。这半个洞只能由机器回答：把脚本**自己那句判决表达式**抽出来，喂给一个真的
 CimInstance 形状的对象，问它「这次答话了吗」。本文件照抄
`test_swap_package_watchdog_enabled_machine_truth.py` 的三条纪律：
① 用文本/AST 定位**脚本自己的那条表达式**（`_poll_predicate` / `_stop_predicate`），
② 把**抽出来的那一条**原样拼进生成的 .ps1 跑（`_build_probe_text` 校验拼进去的就是它，
   校验不过一律 None，绝不退回跑另一条「看起来也对」的表达式），
③ 判决的期望值写死在本文件里（这台机器的事实），**不读脚本内容**——判决绝不由被测对象自报。
另外还带**阳性对照**（固定在探针里的 `(@($running).Count) -ge 1`：一个进程时必须真）与
**反例**（固定在探针里的裸 `$running.Count -ge 1`：一个进程时必须假，且它的 `.Count` 是
$null 而 `@($running).Count` 是 1）——反例不读脚本，讲的全是机器事实，作用与那份
「裸 `.Enabled`」对照一样：有人把写法改回去时，红的是判决锁，而这条把为什么红当场说给他听。

**#52（第二轮）**：`@($running).Count` 对**字面量 `$null`** 也数成 1；今天安全仅仅因为
`$running` 每轮都重新赋值。修法是把 `@()` 移到**赋值点**并过滤 `$null` 元素
（`$running = @(Get-LiveProcess | Where-Object { $null -ne $_ })`），判决改读
`$running.Count`。于是本文件同时抄**赋值表达式**与判决表达式进探针，并新增 literal-null
三档：字面 `$null` -> FALSE、空数组 -> FALSE、`@($null)` -> TRUE（机器实测表
FALSE/FALSE/TRUE）。**不许退回 `-not` 写法**：它在 0/1/2 进程上答 FALSE/FALSE/TRUE
（零进程不再拉起、两个在跑还再拉一个），那是 #51 已经量过的危险方向。

顺带钉住 `Stop-AfterFailure` 里那句重启判决（票面写作 `:493`，本文件从 `:474` 时代起盯它）：
#51 之前它是裸 `.Count` 写法，三种进程数**碰巧**都对（0/1/2 → True/False/False）——一个进程时
`.Count` 是 $null，而 `$null -eq 0` 是 False，于是分支不开。#51 把它改成
`@(Get-LiveProcess).Count` 的 honest 形式（行为逐形状不变），同时把
`test_swap_package_disable_verify.py` 的定位改成了结构锚点（锚在 if 体内的 `Start-Process`
上），所以文本形状不再牵动别人的锁。本文件继续抽**那一行自己的条件**，在三种形状下必须得
True/False/False，并且与固定在探针里的裸写法逐形状相同；而
`if (-not (Get-LiveProcess).Count -eq 0 ...)` 这种「顺手简化」必须被抓住——这台机器答的是
FALSE/FALSE/TRUE：零进程时不再重启（包静默躺在地上），两个进程时反倒再拉一个（两个后端写
同一批 data\\ 文件）。这张表是探针答给本文件的，不是本文件推出来的：写这份锁时按运算符结合
性猜的那一版（FALSE/TRUE/TRUE）被这台机器当场拒了，所以文件里留下的是读数。

安全边界（全部只读，探针必须配得上）：探针**不查进程表**——它把脚本 `Get-LiveProcess` 的
函数体原文抄进生成的 .ps1，只把 `Get-CimInstance` 换成一个用 `New-CimInstance -ClientOnly`
造哑对象的桩（`-ClientOnly` 不访问 WMI 仓储，实测类型就是
`Microsoft.Management.Infrastructure.CimInstance`、`CimClass.CimClassName` 就是
`Win32_Process`，与现网那个对象同形），于是「一个/零个/两个进程」三种形状全在内存里造出来。
`Get-CimInstance` 在探针源码里**只许出现两次**（桩的定义 + 抄进来的那一次调用），这条由
`test_probe_cannot_touch_the_live_process_list_or_the_network` 断言；同样断言探针里没有
`127.0.0.1` / `Invoke-RestMethod`（现网就在这台机器 8000 端口服务）、没有
`Get-ScheduledTask` 与任何会写任务/杀进程/写文件的 cmdlet（复用那份锁的 `_no_write_cmdlets`），
`$LiveExe` 指向 `tmp_path`（仓库外）里一个假的 exe 文件，仓库绝对路径绝不进探针。
Python 侧也绝不枚举真进程表、绝不动任何计划任务。

`Test-Path` 那一半也要真的成立：探针先打印 `live_exe_present=True`，否则 `:493` 那条条件会
因为文件不存在而三种形状全 False，锁就成了恒真的空判据（这一条本身也被断言）。

没有 `powershell.exe` 就 skip 机器那四条，静态那四条照跑照判（惯例同上面两份锁）。
"""
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_swap_package_boot_poll_threw_handler import _block_end, _boot_region
from tests.test_swap_package_disable_verify import _code_lines, _func_body, _target_ps1
from tests.test_swap_package_watchdog_enabled_machine_truth import (
    PROBE_TIMEOUT_SECONDS,
    _find_powershell,
    _no_write_cmdlets,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SWAP_PS1 = _target_ps1()
PS1 = SWAP_PS1.read_text(encoding="utf-8")

_CIM_TYPE = "Microsoft.Management.Infrastructure.CimInstance"
_CIM_CLASS = "Win32_Process"

# 抽 `if (<cond>) {` 那一行的 <cond>：贪婪匹配到行尾那颗 `) {`，所以嵌套括号留在 cond 里。
_IF_LINE = re.compile(r"^\s*if \((?P<cond>.*)\) \{$")

# 拼进生成 .ps1 之前的唯一护栏：只许是「一条裸表达式」的形状。宁可窄——
# 出现 ; { } 反引号 换行 重定向 与引号一律拒绝（拒绝即 None，调用方判红）。
_EXPR_SAFE = re.compile(r"^[A-Za-z0-9_$@()\[\].,\- ]+$")

# 脚本自己那条读法的函数体必须长这样，否则探针跑的就不是生产路径
_BODY_HEAD = "function Get-LiveProcess {"
_BODY_NEEDLE = "Get-CimInstance Win32_Process -Filter"

# 三种形状的机器事实（2026-10-17 本机实测，见模块 docstring；**不读脚本内容**）。
# #52 起另加 assigned_*：赋值点把结果变成数组并过滤 $null，raw 那几格仍描述
# Get-LiveProcess 的原始返回值（#50 的展平事实不随赋值写法改变）。
_SHAPE_FACTS = {
    "zero": {"is_array": "False", "is_null": "True", "count_is_null": "False",
             "count_value": "0", "wrapped_count": "0", "type_name": "NULL",
             "first_pid": "NONE", "assigned_is_array": "True", "assigned_count": "0"},
    "one": {"is_array": "False", "is_null": "False", "count_is_null": "True",
            "count_value": "NULL", "wrapped_count": "1", "type_name": _CIM_TYPE,
            "cimclass": _CIM_CLASS, "first_pid": "4242",
            "assigned_is_array": "True", "assigned_count": "1"},
    "two": {"is_array": "True", "is_null": "False", "count_is_null": "False",
            "count_value": "2", "wrapped_count": "2", "type_name": "System.Object[]",
            "first_pid": "4242", "assigned_is_array": "True", "assigned_count": "2"},
}

# #52 的 literal-null 三档（机器判，不读脚本）：判决表达式只读 $running.Count。
# 字面 $null 与空数组都是 FALSE；@($null) 是 TRUE —— 所以 "@() 包一层"不是恒等，
# 赋值点必须把 $null 元素滤掉。
_LITERAL_NULL_EXPECT = {"literal": "FALSE", "empty": "FALSE", "wrapped": "TRUE"}

# 判决的机器事实：一个进程只有「再包一层」才看得见；裸写法在 0/2 上与它同答（所以旧用例
# 集合 {0,2} 永远分不出来），只在 1 上分道。
_POLL_EXPECT = {
    "extracted": {"zero": "FALSE", "one": "TRUE", "two": "TRUE"},
    "bare": {"zero": "FALSE", "one": "FALSE", "two": "TRUE"},
    "wrapped": {"zero": "FALSE", "one": "TRUE", "two": "TRUE"},
}
# 重启判决：只许「真的一个进程都没有」时开门。#51 已把脚本那一行改成 @() 形式；
# 裸 .Count 与 @() 在 0/1/2 上逐形状相同（所以那次改动行为中性），而 `-not` 那种
# 写法在**两个**进程时会开门（危险方向：双起后端，写同一批 data\ 文件）。
_STOP_EXPECT = {
    "extracted": {"zero": "TRUE", "one": "FALSE", "two": "FALSE"},
    "bare": {"zero": "TRUE", "one": "FALSE", "two": "FALSE"},
    "wrapped": {"zero": "TRUE", "one": "FALSE", "two": "FALSE"},
}

_PROBE_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
$LiveExe = '__LIVE_EXE__'
$script:fakeCount = 0
$script:stubCalls = 0
function Get-CimInstance {
    param([string] $ClassName, [string] $Filter)
    $script:stubCalls = $script:stubCalls + 1
    $n = $script:fakeCount
    if ($n -le 0) { return }
    foreach ($i in 1..$n) { New-CimInstance -ClassName Win32_Process -Property @{ ProcessId = (4241 + $i); ExecutablePath = $LiveExe } -ClientOnly }
}
__GET_LIVE_PROCESS__
function Eval-Shape([string] $label, [int] $n) {
    $script:fakeCount = $n
    $raw = Get-LiveProcess
    # __ASSIGN__ is the script's OWN assignment expression (ticket #52 moved the
    # @() and the $null filter here). $raw keeps describing what the reader itself
    # returned, which is what the #50 shape facts are about.
    $running = __ASSIGN__
    'shape=' + $label
    'is_array=' + ($raw -is [array])
    'is_null=' + ($null -eq $raw)
    'count_is_null=' + ($null -eq $raw.Count)
    'count_value=' + $(if ($null -eq $raw.Count) { 'NULL' } else { [string] $raw.Count })
    'wrapped_count=' + (@($raw).Count)
    'type_name=' + $(if ($null -eq $raw) { 'NULL' } else { $raw.GetType().FullName })
    'cimclass=' + $(if ($null -eq $raw) { 'NULL' } else { [string] $raw.CimClass.CimClassName })
    'first_pid=' + $(if (@($raw).Count -ge 1) { [string] (@($raw)[0]).ProcessId } else { 'NONE' })
    'assigned_is_array=' + ($running -is [array])
    'assigned_count=' + $running.Count
    'poll_bare=' + $(if ($raw.Count -ge 1) { 'TRUE' } else { 'FALSE' })
    'poll_wrapped=' + $(if ((@($raw).Count) -ge 1) { 'TRUE' } else { 'FALSE' })
    'poll_extracted=' + $(if __POLL_COND__ { 'TRUE' } else { 'FALSE' })
    'stop_bare=' + $(if ((Get-LiveProcess).Count -eq 0) { 'TRUE' } else { 'FALSE' })
    'stop_wrapped=' + $(if (@(Get-LiveProcess).Count -eq 0) { 'TRUE' } else { 'FALSE' })
    'stop_extracted=' + $(if __STOP_COND__ { 'TRUE' } else { 'FALSE' })
}
'live_exe_present=' + (Test-Path -LiteralPath $LiveExe)
Eval-Shape 'zero' 0
Eval-Shape 'one' 1
Eval-Shape 'two' 2
# #52 literal-null truth table: the PREDICATE alone, fed values fixed here (not read
# from the script). Expected on this machine: FALSE / FALSE / TRUE.
$running = $null
'nullcase_literal=' + $(if __POLL_COND__ { 'TRUE' } else { 'FALSE' })
$running = @()
'nullcase_empty=' + $(if __POLL_COND__ { 'TRUE' } else { 'FALSE' })
$running = @($null)
'nullcase_wrapped=' + $(if __POLL_COND__ { 'TRUE' } else { 'FALSE' })
# Counterexample fixed here: the OLD wrapping on a literal $null counts 1.
$running = $null
'old_wrap_literal=' + $(if ((@($running).Count) -ge 1) { 'TRUE' } else { 'FALSE' })
'stub_calls=' + $script:stubCalls
'psversion=' + $PSVersionTable.PSVersion.ToString()
'probe_done=yes'
"""

_POWERSHELL = _find_powershell()
_CACHE: dict = {}


def _skip_machine() -> None:
    pytest.skip("这台机器没有 powershell.exe，四条机器锁跑不了（跳过的就是这四条）。静态那四条"
                "——两处判决可定位、接线、拼不进非法表达式、只读性——不受影响、照判。注意：没有"
                "探针时本文件只剩文本判据水平，而文本判据正是挡不住「恰好一个进程」这一档的东西，"
                "所以这道门的放行复跑必须在 Windows 现网机上做。")


# ------------------------------------------------------------------ 抽脚本自己的赋值与两条判决
def _cond_at_if_line(line: str):
    m = _IF_LINE.match(line)
    return None if m is None else m.group("cond").strip()


# #52 认下来的赋值形状：@() 必须在赋值点，且过滤字面 $null。只放行这三个原文，
# 别的写法一律 None（调用方判红）——探针里拼的是生产代码，不放行就没有第二把尺子。
_ALLOWED_ASSIGNMENTS = (
    "@(Get-LiveProcess | Where-Object { $null -ne $_ })",
    "@(Get-LiveProcess)",
    "Get-LiveProcess",
)


def _poll_assignment(text: str):
    """轮询里 `$running = ...` 的**右侧表达式原文**（脚本自己写的赋值）。

    定位复用 `_boot_region` 的 read 行，只剥 `$running = ` 前缀。形状不在
    `_ALLOWED_ASSIGNMENTS` 里就返回 None：探针拼的必须是生产那一句，宁可红。
    """
    reg = _boot_region(text)
    if reg is None:
        return None
    line = reg["lines"][reg["read"]]
    m = re.match(r"^\s*\$running = (?P<rhs>.+?)\s*$", line)
    if m is None:
        return None
    rhs = m.group("rhs")
    if rhs not in _ALLOWED_ASSIGNMENTS:
        return None
    return {"line": reg["read"], "expr": rhs}


def _poll_predicate(text: str):
    """轮询里那句「看见进程没有」的**条件原文**（脚本自己写的表达式）。

    定位：`_boot_region` 的顶格锚点给出 `$running = @(Get-LiveProcess ...)` 那一行，
    再在循环体内（读到该行之后、与 `while` 配平的那一行之前）找**恰好一条**条件里带
    `$running` 的 `if (...) {`。零条、两条、或者条件里根本不带 `$running`，一律 None——
    两条就是两份真相，本文件不知道该问哪一条，宁可不判。
    """
    reg = _boot_region(text)
    if reg is None:
        return None
    lines = reg["lines"]
    loop_end = _block_end(lines, reg["loop"], reg["started"])
    if loop_end is None:
        return None
    hits = []
    for i in range(reg["read"] + 1, loop_end + 1):
        cond = _cond_at_if_line(lines[i])
        if cond is not None and "$running" in cond:
            hits.append((i, cond))
    if len(hits) != 1:
        return None
    idx, cond = hits[0]
    if ".Count" not in cond:
        return None
    return {"line": idx, "cond": cond}


def _stop_predicate(text: str):
    """"旧包要不要重新拉起来"那句判决的**条件原文**（`Stop-AfterFailure` 里那条 if）。"""
    lines = _code_lines(text)
    hits = []
    for i, ln in enumerate(lines):
        cond = _cond_at_if_line(ln)
        if cond is not None and "Get-LiveProcess" in cond:
            hits.append((i, cond))
    if len(hits) != 1:
        return None
    idx, cond = hits[0]
    if ".Count" not in cond:
        return None
    return {"line": idx, "cond": cond}


def _reader_body(text: str):
    """脚本 `Get-LiveProcess` 的函数体原文（注释已被 `_func_body` 清空，行数与花括号照旧）。"""
    body = _func_body(text, "Get-LiveProcess")
    if body is None:
        return None
    joined = "\n".join(body)
    if not joined.startswith(_BODY_HEAD):
        return None
    if _BODY_NEEDLE not in joined:
        return None
    return joined


def _expr_is_safe(cond: str) -> bool:
    """护栏：一条裸表达式才准拼进 .ps1（括号还得配平）。"""
    if not cond or not _EXPR_SAFE.match(cond):
        return False
    if cond.count("(") != cond.count(")") or cond.count("[") != cond.count("]"):
        return False
    return "$running" in cond or "Get-LiveProcess" in cond


def _build_probe_text(live_exe: str, assign_expr: str, poll_cond: str, stop_cond: str, body: str):
    """把**脚本自己的赋值 + 两条判决 + 那句读法**拼进探针，并证明拼进去的就是它们。

    这一步是「探针跑的不是另一条表达式 / 另一段代码」的接线证明；任何一环不过就返回 None，
    调用方判红——绝不退回去跑一条更好写的表达式。
    拼接时给条件补上 PowerShell 的 `if ()` 那层括号（本机实测：少了它 PS 5.1 直接报
    「if 中缺少 '('」，见 2026-10-17 第一次跑），补出来的形状与脚本原文逐字相同：
    脚本那句本来也是 `if (<cond>) {`，cond 自己不带外层括号。
    """
    if body is None or assign_expr not in _ALLOWED_ASSIGNMENTS:
        return None
    if not _expr_is_safe(poll_cond) or not _expr_is_safe(stop_cond):
        return None
    text = (_PROBE_TEMPLATE
            .replace("__LIVE_EXE__", live_exe)
            .replace("__ASSIGN__", assign_expr)
            .replace("__POLL_COND__", "(" + poll_cond + ")")
            .replace("__STOP_COND__", "(" + stop_cond + ")")
            .replace("__GET_LIVE_PROCESS__", body))
    if "__" in text:
        return None
    if ("$running = " + assign_expr) not in text:
        return None
    for tag, cond in (("poll_extracted", poll_cond), ("stop_extracted", stop_cond)):
        needle = "'" + tag + "=' + $(if (" + cond + ") { 'TRUE' } else { 'FALSE' })"
        if needle not in text:
            return None
    if body not in text:
        return None
    if "function Get-CimInstance {" not in text:
        return None
    return text


# 每个 shape 段里的那 14 个键；其余键（probe_done / stub_calls / psversion /
# live_exe_present）是**全局**读数——按键名归属，不按出现顺序，否则最后那几行会被
# 归进最后一个 shape 段里（2026-10-17 第一次跑就是这么丢了 probe_done 的）。
_SHAPE_KEYS = frozenset((
    "is_array", "is_null", "count_is_null", "count_value", "wrapped_count", "type_name",
    "cimclass", "first_pid", "assigned_is_array", "assigned_count",
    "poll_bare", "poll_wrapped", "poll_extracted",
    "stop_bare", "stop_wrapped", "stop_extracted",
))


def _parse_shapes(stdout: str):
    """把 key=value 输出切成 {shape: {...}} + 全局 kv。SAMPLE 式分段，不拿正则猜整行。"""
    shapes, kv, cur = {}, {}, None
    for line in (ln.strip() for ln in stdout.splitlines()):
        if not line:
            continue
        m = re.fullmatch(r"([a-z_][a-z0-9_]*)=(\S*)", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if key == "shape":
            cur = {}
            shapes[val] = cur
            continue
        if key in _SHAPE_KEYS:
            assert cur is not None, f"形状键 {key} 出现在任何 shape 段之前：探针输出顺序变了"
            cur[key] = val
        else:
            kv[key] = val
    return shapes, kv


def _verdicts(shapes: dict):
    """三张判决表：{poll|stop} x {extracted|bare|wrapped} x {zero|one|two}。"""
    out = {}
    for kind in ("poll", "stop"):
        out[kind] = {}
        for how in ("extracted", "bare", "wrapped"):
            key = kind + "_" + how
            out[kind][how] = {shape: shapes[shape].get(key) for shape in ("zero", "one", "two")}
    return out


def _run_shape_probe(text: str, work: Path, tag: str = "script"):
    """在**仓库外**的 work 目录里生成并只读跑一次形状探针，返回 (shapes, kv, 探针源码)。"""
    if _POWERSHELL is None:
        _skip_machine()
    assign = _poll_assignment(text)
    poll = _poll_predicate(text)
    stop = _stop_predicate(text)
    body = _reader_body(text)
    if assign is None or poll is None or stop is None or body is None:
        return None
    key = (assign["expr"], poll["cond"], stop["cond"], body)
    if key in _CACHE:
        return _CACHE[key]
    work.mkdir(parents=True, exist_ok=True)
    live = work / ("fake-live-exe-" + tag + ".bin")
    # 只是个占位文件，让 :493 那条条件的 Test-Path 那半边真的成立；探针从不执行它。
    live.write_bytes(b"MZ-fake-not-an-executable")
    src = _build_probe_text(live.as_posix(), assign["expr"], poll["cond"], stop["cond"], body)
    if src is None:
        return None
    path = work / ("probe-shape-" + tag + ".ps1")
    path.write_bytes(src.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii"))
    proc = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
        capture_output=True, timeout=PROBE_TIMEOUT_SECONDS, cwd=str(work))
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    result = {"rc": proc.returncode, "out": out, "err": err, "path": str(path),
              "src": src, "assign": assign, "poll": poll, "stop": stop, "body": body}
    _CACHE[key] = result
    return result


def _machine_tables(text: str, work: Path, tag: str = "script"):
    """跑探针并把它**变成判决用的东西**：三张表 + 形状事实。任何一环不成立就返回 None。"""
    ran = _run_shape_probe(text, work, tag)
    if ran is None:
        return None
    assert ran["rc"] == 0, (
        f"形状探针不是只读跑通的（rc={ran['rc']}）：{ran['err'].strip()[:300]}\n探针：{ran['path']}")
    shapes, kv = _parse_shapes(ran["out"])
    assert kv.get("probe_done") == "yes", f"探针没走到最后一行，输出半路断了：{ran['out'][:300]!r}"
    assert kv.get("live_exe_present") == "True", (
        "假 exe 文件不在，:493 那条条件的 Test-Path 那半边恒假，三条判决会一起变成假真：")
    assert set(shapes) == {"zero", "one", "two"}, f"三种形状没都问到机器：{sorted(shapes)}"
    calls = int(kv.get("stub_calls") or 0)
    assert calls >= 9, f"桩没被叫到（stub_calls={calls}）——探针根本没经过脚本那句读法：{kv}"
    return {"shapes": shapes, "kv": kv, "v": _verdicts(shapes), "ran": ran}


# ------------------------------------------------------------------ 判据本体（可喂任意文本）
def _poll_sees_one_process(text: str, work: Path, tag: str = "script") -> bool:
    """核心判据：**脚本自己那条轮询判决**，在恰好一个进程的形状上必须为真。"""
    got = _machine_tables(text, work, tag)
    if got is None:
        return False
    return all(got["v"]["poll"][how][shape] == want
               for how in ("extracted",)
               for shape, want in _POLL_EXPECT["extracted"].items())


def _stop_fires_only_when_empty(text: str, work: Path, tag: str = "script") -> bool:
    """:493 那条重启判决的真值表必须正好是「只有零进程时开门」，且与包一层 @() 逐形状相同。"""
    got = _machine_tables(text, work, tag)
    if got is None:
        return False
    if any(got["v"]["stop"][how][shape] != want
           for how in ("extracted",) for shape, want in _STOP_EXPECT["extracted"].items()):
        return False
    return got["v"]["stop"]["extracted"] == got["v"]["stop"]["wrapped"]


def _write_crlf_copy(work: Path, name: str, text: str) -> Path:
    """变异样本落盘到**仓库外**，且按 CRLF：本机 core.autocrlf=true，LF 副本会先红在
    文件自带的 ASCII/CRLF 门上，把「改前那份也全绿」这类假结论坐实（同 #45 那份的教训）。"""
    body = text.replace("\r\n", "\n").replace("\n", "\r\n")
    path = work / name
    path.write_bytes(body.encode("utf-8"))
    raw = path.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n"), "副本必须是纯 CRLF"
    assert not any(b >= 0x80 for b in raw), "副本必须保持纯 ASCII：PS 5.1 的解码坑"
    return path


@pytest.fixture(scope="module")
def script_tables(tmp_path_factory):
    """用**当前被锁的那份脚本**自己的赋值与两条判决跑一次探针，结果整个模块共用。"""
    if _POWERSHELL is None:
        _skip_machine()
    work = tmp_path_factory.mktemp("boot-poll-shape")
    got = _machine_tables(PS1, work, "script")
    assert got is not None, (
        "探针生不出来：赋值/两处判决有一句定位不到，或那句读法的函数体形状不对（判法见 "
        "_poll_assignment / _poll_predicate / _stop_predicate / _reader_body）")
    return got


# ------------------------------------------------------------------ 静态四条
def test_both_predicates_are_locatable_and_each_is_the_only_one():
    """前提锁：赋值、轮询判决、重启判决都必须**恰好一条**，且判决都读 `.Count`。

    「锚点找不到」与「同一段里出现两条真相」是两种红法，都要红：任何一环抽不出来，
    本文件的机器锁就无从接线。
    """
    assign = _poll_assignment(PS1)
    assert assign is not None, (
        "轮询里 `$running = ...` 的赋值表达式定位不到，或形状不在 _ALLOWED_ASSIGNMENTS 里"
        "（#52：@() 必须在赋值点并过滤 $null）")
    assert assign["expr"] == "@(Get-LiveProcess | Where-Object { $null -ne $_ })", (
        f"赋值形状退回危险写法：{assign['expr']!r}——@($null) 会数成 1，"
        "字面 $null 必须在这里被过滤掉")
    poll = _poll_predicate(PS1)
    assert poll is not None, (
        "轮询里那句「看见进程没有」的 if 定位不到（判法见 _poll_predicate：顶格 while 锚点 + "
        "$running 那行之后、循环体内恰好一条带 $running 的 if）。本票要问机器的就是这一条")
    stop = _stop_predicate(PS1)
    assert stop is not None, (
        "Stop-AfterFailure 里那句「旧包要不要重新拉起来」的 if 定位不到（_stop_predicate："
        "全文件恰好一条条件里带 Get-LiveProcess 的 if）")
    assert "$running" in poll["cond"] and "Get-LiveProcess" in stop["cond"], (
        f"抽出来的不是那两条判决：poll={poll['cond']!r} stop={stop['cond']!r}")
    assert "$running.Count -ge 1" == poll["cond"], (
        "轮询判决必须只读赋值点保证过的数组的 .Count；回到 @($running) 包裹会把字面 $null 数成 1")
    body = _reader_body(PS1)
    assert body is not None, (
        "Get-LiveProcess 的函数体抄不出来（必须以 `function Get-LiveProcess {` 开头、"
        "且含那句 CIM 查询）——探针要跑的就是脚本自己那句读法")


def test_probe_splices_the_scripts_own_reader_and_both_its_own_predicates():
    """接线锁：探针跑的**就是脚本抽出来的赋值、两条表达式和那段读法**，不是另一条好看的。

    没有这一条，机器那四条完全可以靠「另拼一条恒成立的表达式」恒绿。
    """
    assign = _poll_assignment(PS1)
    poll = _poll_predicate(PS1)
    stop = _stop_predicate(PS1)
    body = _reader_body(PS1)
    assert assign is not None and poll is not None and stop is not None and body is not None
    src = _build_probe_text("C:/fake/dir/run_backend.exe", assign["expr"],
                            poll["cond"], stop["cond"], body)
    assert src is not None, "探针源码生不出来（_build_probe_text 的校验没过）"
    assert "$running = " + assign["expr"] in src, "脚本自己那句赋值没有原样落到探针里"
    for needle in ((lambda tag, cond:
                    "'" + tag + "=' + $(if (" + cond + ") { 'TRUE' } else { 'FALSE' })")(t, c)
                   for t, c in (("poll_extracted", poll["cond"]), ("stop_extracted", stop["cond"]))):
        assert needle in src, f"脚本自己那条判决没有原样落到探针里：{needle!r}"
    assert body in src, "探针里跑的不是脚本 Get-LiveProcess 那一段原文"
    assert "@(Get-CimInstance Win32_Process" in src, (
        "脚本那句读法的 @(pipeline) 形状没进探针——那条形状正是本票的洞所在")
    assert "function Get-CimInstance {" in src, "桩必须先把 Get-CimInstance 换成哑对象生成器"
    assert src.count("Get-CimInstance") == 2, (
        f"探针里 Get-CimInstance 出现 {src.count('Get-CimInstance')} 次（应为桩定义 1 + "
        "抄进来的调用 1）：多出来的就是又在真查进程表")
    assert ("if ($running.Count -ge 1) { 'TRUE' }" in src) is True, (
        "反例（裸 $running.Count -ge 1）必须固定在探针里，不随脚本改")
    assert "@(@($running).Count) -ge 1" not in src and "(@($running).Count) -ge 1" in src, (
        "阳性对照（包一层再比）必须固定在探针里，且不许被二次包裹改变形状")
    assert "live_exe_present=" in src, "Test-Path 那半边必须自己报告，否则三条判决可能同时假真"


def test_generated_probe_refuses_an_expression_that_is_not_bare():
    """护栏自检（正控 + 反控）：拼进 .ps1 的只许是一条裸表达式。

    正控：脚本今天那两条必须过。反例：带 `;` / `{` / 反引号 / 换行 / 括号的非法形状必须被
    拒（返回 None）——少了这一条，「把脚本里的文本片段拼进生成的 .ps1」就是在拿生产机冒险。
    """
    assign = _poll_assignment(PS1)
    poll = _poll_predicate(PS1)
    stop = _stop_predicate(PS1)
    body = _reader_body(PS1)
    assert assign and poll and stop and body
    assert _build_probe_text("C:/f/a.bin", assign["expr"], poll["cond"], stop["cond"], body) is not None, (
        "正控：今天那句赋值与两条表达式必须能拼进探针，否则护栏是在拒一切")
    for bad in ("$running.Count -ge 1; Remove-Everything",
                "$running.Count -ge 1 } else { $x",
                "`$running.Count -ge 1",
                "$running.Count -ge (`r`n 1)",
                "$running.Count -ge 1 > out.txt",
                "$running.Count -ge 1 # comment"):
        assert _EXPR_SAFE.match(bad) is None, f"反例被放进来了：{bad!r}"
        assert _build_probe_text("C:/f/a.bin", assign["expr"], bad, stop["cond"], body) is None, (
            f"非法表达式竟然拼得出探针：{bad!r}")
    assert _build_probe_text("C:/f/a.bin", "$running = $null", poll["cond"], stop["cond"], body) is None, (
        "不在 _ALLOWED_ASSIGNMENTS 里的赋值形状必须被拒（句子里带不认识的赋值 = 换了生产代码）")
    assert _expr_is_safe("SomeStringWithoutAnyReference") is False, (
        "连 $running / Get-LiveProcess 都不带的表达式不是本票要问的那一条，必须拒")


def test_probe_cannot_touch_the_live_process_list_or_the_network():
    """只读锁（静态，不启动 PowerShell）：探针问的是**内存里造的 CimInstance**，
    碰不到现网、碰不到任务、也碰不到进程表。

    现网此刻就在这台机器的 8000 端口上服务，`dist\\run_backend\\run_backend.exe` 正是
    `Get-LiveProcess` 按全路径匹配的那个对象；本文件一次都不许真去查它。
    """
    assign = _poll_assignment(PS1)
    poll = _poll_predicate(PS1)
    stop = _stop_predicate(PS1)
    body = _reader_body(PS1)
    assert assign and poll and stop and body
    fake = "C:/fake/outside-the-repo/run_backend-probe-only.bin"
    src = _build_probe_text(fake, assign["expr"], poll["cond"], stop["cond"], body)
    assert src is not None, "探针源码生不出来"
    assert src.isascii(), "生成的探针必须纯 ASCII：PS 5.1 会把带中文的无 BOM .ps1 错解码成语法错误"
    assert "Invoke-RestMethod" not in src and "127.0.0.1" not in src, (
        "探针里出现了真 /health 探针的形状：现网就在这台机器的 8000 端口上")
    assert "ScheduledTask" not in src, "探针里出现了计划任务：本票一条都不许碰任务"
    assert "Get-Process" not in src and "Stop-Process" not in src, (
        "探针里出现了进程 cmdlet：桩之外一个都不许读")
    assert "Get-CimInstance" in src and src.count("Get-CimInstance") == 2, (
        f"Get-CimInstance 出现 {src.count('Get-CimInstance')} 次（只许桩定义 1 + 抄进来的调用 1）："
        "多出来的就是又在真查进程表")
    assert "New-CimInstance" in src and "-ClientOnly" in src, (
        "哑对象必须由 New-CimInstance -ClientOnly 在内存里造：它不访问 WMI 仓储")
    assert str(REPO_ROOT) not in src, "仓库绝对路径进了探针（本仓库目录同时是生产机）"
    assert "dist/run_backend" not in src and "dist\\run_backend" not in src, (
        "现网包路径进了探针：$LiveExe 必须指向 tmp_path 里的假文件")
    assert fake in src, "$LiveExe 没有指向探针目录里的那个假文件"
    _no_write_cmdlets(src, "形状探针")
    assert SWAP_PS1.read_text(encoding="utf-8") == PS1, "仓库里那份被改了：本文件全程只读"


# ------------------------------------------------------------------ 机器四条
def test_machine_the_object_the_poll_actually_received_is_the_production_shape(script_tables):
    """机器判据·形状：一个进程时调用侧拿到的**不是数组**、`.Count` 是 `$null`、
    而 `@(...)` 之后是 1 个元素；类型就是生产那个 CIM 类型。

    这一条是给后面三条「续保」的：如果探针造出来的对象其实是数组（或类型不对），
    那「判决对不对」就问的不是现网那个形状，整个文件会静默变成空判据。
    """
    shapes = script_tables["shapes"]
    for shape, facts in _SHAPE_FACTS.items():
        got = shapes[shape]
        for key, want in facts.items():
            assert got.get(key) == want, (
                f"{shape} 个进程时机器答的是 {key}={got.get(key)!r}，本机实测应为 {want!r}"
                f"（整段形状：{got}）")
    assert shapes["one"]["type_name"] == _CIM_TYPE, (
        "一个进程时拿到的类型不是 CimInstance——探针喂的不是生产形状，后面三条判决全部作废")
    assert shapes["one"]["count_is_null"] == "True", ".Count 竟然不是 $null：这台机器的形状变了"
    assert shapes["one"]["wrapped_count"] == "1", "包一层 @() 之后不是 1 个元素：形状对不上账"


def test_machine_the_scripts_own_poll_predicate_sees_exactly_one_process(script_tables):
    """机器判据·本票本体：**脚本自己那条轮询判决**，在 0/1/2 三种形状上必须分别是
    FALSE / TRUE / TRUE。

    一个进程（现网常态）那一档就是 #50 的洞；#52 之后判决读的是赋值点保证过的数组，
    赋值表达式来自脚本自己。判决的期望值写死在本文件（机器事实），不是从脚本里读出来的。
    """
    v = script_tables["v"]
    for shape, want in _POLL_EXPECT["extracted"].items():
        got = v["poll"]["extracted"][shape]
        assert got == want, (
            f"脚本自己那句 {script_tables['ran']['poll']['cond']!r} 在 {shape} 个进程时"
            f"这台机器判成 {got}，应为 {want}。一个进程判成 FALSE 就是 ticket #50："
            "裸 CimInstance 的 .Count 是 $null，-ge 1 恒假，轮询永远破不出循环")
    assert script_tables["ran"]["assign"]["expr"] == "@(Get-LiveProcess | Where-Object { $null -ne $_ })", (
        "探针跑的赋值表达式不是脚本今天那一句：接线断了")
    assert v["poll"]["extracted"] == v["poll"]["wrapped"], (
        f"脚本那条判决与「包一层 @()」的写法不同答（{v['poll']}）：判决没在数元素，在猜形状")
    assert "-not" not in script_tables["ran"]["poll"]["cond"], (
        "判决退回 -not 写法：机器实测它在 0/1/2 上答 FALSE/FALSE/TRUE，"
        "零进程不拉起、两个在跑还再拉一个")


def test_machine_literal_null_is_not_counted_as_one(script_tables):
    """#52 的机器判据：字面 $null / 空数组 / @($null) 分别是 FALSE / FALSE / TRUE。

    这条表讲的是「@() 包一层不是恒等」：`@($null).Count` 是 1，所以赋值点必须把
    `$null` 元素滤掉；而判决读 `$running.Count` 后，字面 `$null` 答 0（FALSE），
    不会让轮询带着 `$livePid = $null` break。改前那种 `@($running)` 包裹在同一份
    探针里作为反例固定下来（old_wrap_literal=TRUE），红绿两侧都是这台机器答的。
    """
    kv = script_tables["kv"]
    for key, want in (("nullcase_literal", _LITERAL_NULL_EXPECT["literal"]),
                      ("nullcase_empty", _LITERAL_NULL_EXPECT["empty"]),
                      ("nullcase_wrapped", _LITERAL_NULL_EXPECT["wrapped"])):
        got = kv.get(key)
        assert got == want, (
            f"{key} 在这台机器上答 {got}，应为 {want}（字面 $null/空数组/@($null) = "
            "FALSE/FALSE/TRUE）：判决对字面 $null 的语义变了，先重跑探针再动判据")
    assert kv.get("old_wrap_literal") == "TRUE", (
        "旧的 @($running) 包裹对字面 $null 竟然不数 1：那 #52 的前提不成立，"
        "本文件与赋值点的过滤都要重写")


def test_machine_positive_control_and_the_counter_example_show_why_zero_and_two_cannot_tell(
        script_tables):
    """机器判据·对照与反例（两条都不读脚本内容，讲机器事实）。

    阳性对照：`(@($running).Count) -ge 1` 在 0/1/2 上 FALSE/TRUE/TRUE——它真的数得出一个。
    反例：固定在探针里的裸 `$running.Count -ge 1` 在 0/1/2 上 FALSE/FALSE/TRUE——
    它只在**恰好一个**时错。于是「旧用例集合只喂 0 与 2」为什么看不见这个洞，是这台机器
    当场回答的：那两档上两种写法同答，唯一分道的形状没被喂过。
    """
    v = script_tables["v"]
    for shape, want in _POLL_EXPECT["wrapped"].items():
        assert v["poll"]["wrapped"][shape] == want, (
            f"阳性对照（包一层再比）在 {shape} 个进程时答 {v['poll']['wrapped'][shape]}，"
            f"应为 {want}：这台机器的 @() 形状和实测不一致，先修机器再动判据")
    for shape, want in _POLL_EXPECT["bare"].items():
        assert v["poll"]["bare"][shape] == want, (
            f"反例（裸 $running.Count -ge 1）在 {shape} 个进程时答 {v['poll']['bare'][shape]}，"
            f"应为 {want}。一个进程竟然答 TRUE 的话，本票的理由（裸 CimInstance 的 .Count "
            "是 $null）就不成立了，模块 docstring 那份实测要带日期重写")
    agree = [s for s in ("zero", "two")
             if v["poll"]["bare"][s] == v["poll"]["wrapped"][s]]
    assert agree == ["zero", "two"], (
        f"0 与 2 两档上裸写法与包一层竟然分道（{v['poll']}）：那 #45 的用例集合就不是盲点，"
        "本文件的理由要重写")
    assert v["poll"]["bare"]["one"] != v["poll"]["wrapped"]["one"], (
        "一个进程时两种写法同答：那本票的洞在这台机器上不存在，别改代码，先重跑探针")


def test_machine_the_restart_condition_fires_only_when_nothing_runs(script_tables):
    """机器判据（票面 `:493`）：那条重启判决只在零进程时开门，且裸写法与 @() 逐形状同答。

    #51 把那一行从裸 `.Count` 改成了 `@(...)` 的 honest 形式：旧写法的真值表是对的，
    但靠的是"一个进程时 .Count 是 $null、$null -eq 0 为 False"这个巧合；新写法真的在数
    元素。两种拼法在 0/1/2 上逐形状相同，所以这次改动行为中性；而 `test_mutation_*` 会证明
    `-not` 那种「顺手简化」在同一个位置开门（真危险：两个后端写同一批 data\\ 文件），锁抓得住。
    """
    v = script_tables["v"]
    for shape, want in _STOP_EXPECT["extracted"].items():
        got = v["stop"]["extracted"][shape]
        assert got == want, (
            f"脚本自己那句 {script_tables['ran']['stop']['cond']!r} 在 {shape} 个进程时答 "
            f"{got}，应为 {want}。一个进程答 TRUE 就是「明明有后端还要再拉一个」；"
            "零进程答 FALSE 就是中止后把包留在地上")
    for how in ("bare", "wrapped"):
        for shape, want in _STOP_EXPECT[how].items():
            assert v["stop"][how][shape] == want, (
                f"固定在探针里的参照写法（{how}）在 {shape} 个进程时答 "
                f"{v['stop'][how][shape]}，应为 {want}：参照本身坏了，别拿它对照脚本那一行")
    assert v["stop"]["extracted"] == v["stop"]["wrapped"], (
        f"那一行与包一层 @() 不同答（{v['stop']}）：那「不改它」就不再是行为中性的决定，"
        "本票的论证作废，得回来改这行并同步 disable-verify 那份锁")


# ------------------------------------------------------------------ 牙印三条
def test_mutation_unwrapping_the_poll_assignment_goes_red_on_the_machine(tmp_path):
    """牙印一（#52 的形状-only 变异）：只把**赋值点**的 `@(...)` 摘掉，判决一个字节不动。

    与「退回改动前那一份」的区别在这里：注释、try、stage 全都还在，**只有赋值回到裸读**。
    红必须是这台机器判的：探针拿的是**副本自己**抽出来的赋值表达式，于是 `one` 那一档
    判决答 FALSE（裸 CimInstance 的 .Count 是 $null），阳性对照仍答 TRUE。
    """
    assign = _poll_assignment(PS1)
    assert assign is not None
    assert assign["expr"] == "@(Get-LiveProcess | Where-Object { $null -ne $_ })", (
        f"真文件那句赋值应当是包了一层并过滤 $null 的形状，实得 {assign['expr']!r}：这条牙印没有对象")
    wrapped_line = "$running = " + assign["expr"]
    drifted = PS1.replace(wrapped_line, "$running = Get-LiveProcess", 1)
    assert drifted != PS1, "变异样本没生效：replace 一个字节都没改"
    copy = _write_crlf_copy(tmp_path, "swap-package-assignment-unwrapped.ps1", drifted)
    on_disk = copy.read_text(encoding="utf-8")
    assert on_disk.replace("\r\n", "\n") == drifted.replace("\r\n", "\n"), "副本没落盘"
    assert SWAP_PS1.read_text(encoding="utf-8") == PS1, "仓库里那份被改了"

    got = _poll_assignment(on_disk)
    assert got is not None and got["expr"] == "Get-LiveProcess", (
        f"副本里抽出来的赋值不对：{got}")
    tables = _machine_tables(on_disk, tmp_path, "unwrapped")
    assert tables is not None, "副本的探针生不出来"
    v = tables["v"]
    assert v["poll"]["extracted"]["one"] == "FALSE", (
        "把赋值的 @() 摘掉之后这台机器仍然认出一个进程？那这条锁是空的：" + str(v["poll"]))
    assert tables["shapes"]["one"]["assigned_count"] != "1", (
        f"裸读下来的 $running.Count 竟然还是 1：{tables['shapes']['one']}")
    assert v["poll"]["extracted"] == v["poll"]["bare"], (
        "摘掉赋值那一层之后判决竟然不与「裸写法」同答：探针跑的不是副本那条表达式（接线断了）")
    assert v["poll"]["wrapped"]["one"] == "TRUE", "阳性对照必须照旧为真，否则探针自己坏了"
    assert not _poll_sees_one_process(on_disk, tmp_path, "unwrapped-judgement"), (
        "摘掉赋值那一层，本票的核心判据必须红")
    assert _poll_sees_one_process(PS1, tmp_path, "control-judgement"), (
        "真文件必须先绿，否则上一条的红证明不了任何事")

    unfiltered = PS1.replace(assign["expr"], "@(Get-LiveProcess)", 1)
    assert unfiltered != PS1, "过滤样本没生效"
    got_unfiltered = _poll_assignment(unfiltered)
    assert got_unfiltered is not None and got_unfiltered["expr"] == "@(Get-LiveProcess)", (
        "@() 但不过滤 $null 的样本应当仍可被抽出来（它是文本锁的反例，不是探针反例）")
    assert "@(Get-LiveProcess | Where-Object { $null -ne $_ })" not in unfiltered, (
        "把 $null 过滤器摘掉后，@() 仍会把 @($null) 数成 1——静态锁必须能看见")


def test_mutation_the_restart_site_spellings_are_neutral_while_the_not_spelling_is_caught(tmp_path):
    """牙印二（#51 之后的两面）：`@()` 与裸 `.Count` 行为**一样**，`-not` 那种「简化」**不一样**。

    左半边：脚本那一行现在是 `@(Get-LiveProcess).Count`（#51 的 honest 形式）。把它反向摘掉
    那层 `@()` 去问机器，三种形状逐档相同——这就是那次改动行为中性的证据。右半边是这条锁的
    牙齿：`if (-not (Get-LiveProcess).Count -eq 0 -and ...)` 这种顺手简化，在这台机器上答的是
    FALSE/FALSE/TRUE（`-not` 先结合，见下面那条断言里的逐档推算）——零进程时不再重启（包静默
    躺在地上），两个进程时反倒再拉一个（两个后端写同一批 data\\ 文件）。这条真值表是这台机器
    答的，不是本文件推的：本文件原本按 `-eq` 先结合猜的是 FALSE/TRUE/TRUE，探针当场拒了那一次，
    所以这里记的是读数而不是想法。
    """
    stop = _stop_predicate(PS1)
    assert stop is not None
    wrapped_line = "if (" + stop["cond"] + ") {"
    assert stop["cond"].startswith("@(Get-LiveProcess).Count"), (
        f"#51 之后这句应当是包了一层 @() 的写法，实得 {stop['cond']!r}：这条牙印没有对象")
    bare_line = "if ((Get-LiveProcess).Count -eq 0 -and (Test-Path -LiteralPath $LiveExe)) {"
    bare = PS1.replace(wrapped_line, bare_line, 1)
    assert bare != PS1, "摘掉 @() 的样本没生效"
    copy_b = _write_crlf_copy(tmp_path, "swap-package-stop-bare-spelling.ps1", bare)
    tables_b = _machine_tables(copy_b.read_text(encoding="utf-8"), tmp_path, "stop-bare")
    assert tables_b is not None
    assert tables_b["v"]["stop"]["extracted"] == _STOP_EXPECT["extracted"], (
        f"摘掉 @() 之后这句的行为不再是 True/False/False（{tables_b['v']['stop']}）："
        "那 #51「两种拼法等价」的论证需要重写")
    here = _machine_tables(PS1, tmp_path, "stop-control-tables")
    assert here is not None
    assert tables_b["v"]["stop"]["extracted"] == here["v"]["stop"]["extracted"], (
        "裸写法与仓库里那一行不同答：@() 的改动就不是行为中性的")

    negated = PS1.replace(wrapped_line,
                          "if (-not (Get-LiveProcess).Count -eq 0 -and (Test-Path -LiteralPath $LiveExe)) {",
                          1)
    assert negated != PS1, "反例样本没生效"
    copy_n = _write_crlf_copy(tmp_path, "swap-package-stop-not-spelling.ps1", negated)
    text_n = copy_n.read_text(encoding="utf-8")
    assert not _stop_fires_only_when_empty(text_n, tmp_path, "stop-not"), (
        "`-not ...Count -eq 0` 这种「顺手简化」竟然过了锁：这台机器的判据就是空的")
    tables_n = _machine_tables(text_n, tmp_path, "stop-not-machine")
    # 这台机器当场答出来的表（2026-10-17，本文件原本按「-eq 比 -not 结合得紧」猜的是
    # FALSE/TRUE/TRUE，机器拒了；实测这台 PS 5.1 上 -not 先结合，于是它是
    # ((-not .Count) -eq 0)）：
    #   zero：.Count=0 → -not 0 = True → True -eq 0 = False  → 包在地上**不再重启**（静默）
    #   one  ：.Count=$null → -not $null = True → True -eq 0 = False → 与今天那行同答（侥幸）
    #   two  ：.Count=2 → -not 2 = False → False -eq 0 = True → 有后端还再拉一个（双起）
    # 判决与真值表都由这台机器答，不由本文件推：跑出来不一样就改这里，别改代码。
    assert tables_n["v"]["stop"]["extracted"] == {
        "zero": "FALSE", "one": "FALSE", "two": "TRUE"}, (
        f"`-not` 那种写法在三种形状上答的是 {tables_n['v']['stop']['extracted']}，"
        "与本文件记录的实测不符：先重跑探针确认这台机器，再改这里的表")
    assert _stop_fires_only_when_empty(PS1, tmp_path, "stop-control"), "真文件那行必须先绿"


def test_mutation_a_predicate_that_always_counts_in_turns_the_machine_lock_red(tmp_path):
    """牙印三：把判决改成「什么都算看见」（`-ge 0`），零进程那档必须判红。

    少了这条，本票的核心判据可能只是把判决换成了另一个恒真的东西：`@($running).Count -ge 0`
    照样在 1 个进程时为真，但零进程时也成真——轮询会带着 `$livePid = (@($running)[0])` 去索引
    一个根本没有对象的数组。这台机器当场把它抓出来。
    """
    poll = _poll_predicate(PS1)
    assert poll is not None
    old_line = "if (" + poll["cond"] + ") {"
    always = PS1.replace(old_line, "if (@($running).Count -ge 0) {", 1)
    assert always != PS1, "样本没生效"
    copy = _write_crlf_copy(tmp_path, "swap-package-predicate-always-true.ps1", always)
    text = copy.read_text(encoding="utf-8")
    tables = _machine_tables(text, tmp_path, "always")
    assert tables is not None
    assert tables["v"]["poll"]["extracted"]["zero"] == "TRUE", (
        "零进程时这条判决竟然还是假：探针问的不是副本那条表达式（接线断了）")
    assert not _poll_sees_one_process(text, tmp_path, "always-judgement"), (
        "把判决换成恒真（零进程也算看见）必须红：那 STARTED pid= 会去索引一个不存在的元素")
    assert _poll_sees_one_process(PS1, tmp_path, "always-control"), "真文件必须先绿"
