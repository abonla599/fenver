r"""换包脚本「停用看门狗那一句自己抛异常」这条路的控制流锁 + 机器路由锁（ticket #32）。

洞在哪（一句话版）：`deploy/swap-package.ps1` 原先是裸的两句 ——
`Disable-ScheduledTask ... | Out-Null` 与 `$script:watchdogDisabled = $true`，
**不在任何 try 里**，全文件也没有 `trap` / `finally`，而脚本跑在
`$ErrorActionPreference = 'Stop'` 下。于是那次调用自己抛（CIM/Taskschd 抖一下，
或者开关已经按下去了才在输出管道上失败）时，脚本**没有 stage token、没有恢复尝试**
就死了，而崩溃守护的真实状态从此 UNKNOWN：可能已经停用然后被留在停用态，也可能
什么都没动。这与 #24（把停进程那段包进 try→`Stop-AfterFailure`）是同一族缺陷，
只是从另一行进来。下一次发版就是它的第一次实跑，所以是 before-next-swap 阻塞项。

本文件钉四件事：
(i)   disable 那一句**在**一个 try 里，且那个 try 的 catch 走的是脚本自己的出口
      （`Stop-AfterFailure` → 恢复机制），不是裸 throw / exit；
(ii)  那个 handler **只**用现成的只读 helper `Get-WatchdogTaskState` 问「看守此刻在不在」，
      绝不自己写任务（不许出现 `Enable-ScheduledTask`/`Disable-ScheduledTask`：
      重新启用归 `Restore-WatchdogTask` 一处，两处拥有一个开关就是第二个 bug 藏身的地方）；
(iii) 三种答案各归各的效果，而且**判「是哪一种」用的是机器给的那个开关本身**：
      答「已停用」→ 先登记 `$script:watchdogDisabled = $true` 再中止（这次运行确实按下去过，
      恢复路径会把看守还回来）；答「仍在启用」（且是开关亲口答的）→ 不动登记，
      中止 token 老实读作 `watchdog=never-disabled`，不谎称重新启用过；
      **答不了**（read-threw / task-missing / settings-missing / settings-enabled-missing /
      重读整句没返回）→ 走 fail-safe：当作可能已停用，登记照旧设上、恢复照旧尝试。
      这里的取舍是刻意的不对称：多做一次无谓的重新启用只多一行日志，
      而把崩溃守护留在地上要等到有人去找才发现。
      fail-safe 写成**兜底 fallthrough**而不是第三个 if —— 以后有人加分支忘了中止，也仍然落在这里。
(iv)  这条路要留下一行机器可解析的 token（`DISABLE-THREW enabled=… via=… state=… instances=…
      error=…`），沿用文件既有风格：分类由**走到了哪一个 stage token** 说明，
      不由 handler 自报一个 `branch=` 字段（自报就是判据由被测对象自己说）。

为什么这些锁必须新增而不是改 `test_swap_package_disable_verify.py` 里那 13 条：
本票之前那 13 条对这个洞**全绿**（票面把 `Disable-ScheduledTask` 刻意留在「不必包 try」的
名单里，理由是「它自己抛的时候看守根本没被按下去」—— 而这条理由正是本次要堵掉的猜测：
调用抛了并不等于开关没翻）。所以本文件的牙印之一就是把**改动前的那两句裸形状**造出来，
断言旧的判决锁 `_verdict_ok` 与出口锁 `_no_bare_exit_ok` **照旧绿**、而新锁红 ——
红绿分工必须这样才对得上本次的洞。反过来说，不能把 disable 加进 `_no_bare_exit_ok` 的
`risky` 名单：那会让它既有的「半修样本只红判决锁、出口锁须保持绿」那条独立性牙印变红
（半修样本里包住 disable 的 `} catch {` 正好落在被整段替换的范围里）。

尺子从既有两份锁借（本仓规矩：一把尺子只有一份）：`_window`/`_code_lines`/`_try_spans`/
`_span_of`/`_idx`/`_func_body`/`_catch_routes_to_restore`/`_verdict_ok`/`_no_bare_exit_ok` 来自
`test_swap_package_disable_verify.py`，`_find_powershell`/`_no_write_cmdlets`/
`_parse_probe_output`/`_enabled_read_site` 来自 `test_swap_package_watchdog_enabled_machine_truth.py`，
读哪个文件仍然走作者留下的 `SWAP_PS1_UNDER_TEST`（牙印跑在**仓库外的副本**上）。

机器那两条问的是**路由**，不是任务状态：探针把 handler 里那两条 `if` 条件**原文**抄进生成的
.ps1，再喂给它三种「机器能答出来的答案」（用 helper `return` 那一行抽出来的**同名**字段造对象），
看每条条件实际取真假。于是「已停用→支 1」「仍在启用→支 2」「答不了→两条都假、落进 fail-safe」
这三件事是这台机器判的，不是文本判的；顺带证明两条条件互斥、以及 $null 对象不会误进支 1
（`$null.Enabled` 在这台机器上给 $null，`-not $null` 给 True —— 少了 `$disableProbe -and`
这层守卫，fail-safe 就会被一条「什么都没读到」的分支抢走）。最后再问一次真问题：
对本机那个真看门狗任务，helper 那条读法（`Settings.Enabled`）必须**答得出**，
也就是 fail-safe 不是这台机器上的日常路径。

探针全程只读：只调 `Get-ScheduledTask`，绝不 Disable/Enable/Unregister/Register/Start/Stop 任务、
不停进程、不写文件；本票明令不许动现网那个 `AI助手-崩溃守护`。
没有 `powershell.exe` 就 skip 机器那两条，静态那几条照跑照判。
"""
import re
import subprocess
from pathlib import Path

import pytest

# 尺子只有一份：结构与只读性判据全部从既有两份锁借，本文件不复制第二把。
from tests.test_swap_package_disable_verify import (
    _catch_routes_to_restore,
    _code_lines,
    _func_body,
    _idx,
    _no_bare_exit_ok,
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
    _parse_probe_output,
)

SWAP_PS1 = _target_ps1()
PS1 = SWAP_PS1.read_text(encoding="utf-8")

# 包住 disable 的那句 try 最多在它前面这么多行（中间隔着整段理由注释）
_TRY_LOOKBACK = 80
# 本文件认下来的三种答案各自的中止 stage，一人一个，运维能 grep 出是哪一种
_STAGES = ("disable-threw-task-down", "disable-threw-task-up", "disable-threw-guard-state-unproven")
# helper 的 return 行必须带这五个字段，少一个/多一个都算形状变了（探针造的对象要跟它同名）
_HELPER_FIELDS = ("Enabled", "State", "Instances", "ReadError", "EnabledSource")

_IF_LINE = re.compile(r"^\s*if \((?P<pred>.+)\) \{$")
_SAFETY_LINE = re.compile(r"^\s*Stop-AfterFailure\s+'(?P<stage>[A-Za-z0-9-]+)'")
_FLAG_TRUE = re.compile(r"^\s*\$script:watchdogDisabled = \$true\s*$")
_FLAG_LINE_COL0 = r"^\$script:watchdogDisabled = \$true"
_BARE_EXIT = re.compile(r"^\s*(throw|exit)\b")
_TOKEN_LINE = re.compile(r"^\s*Write-Output \('DISABLE-THREW enabled='")
_TRY_OPEN = re.compile(r"^\s*try\s*\{$")
_DISABLE_COL0 = re.compile(r"^Disable-ScheduledTask -TaskPath")
_READ_CALL = r"$disableProbe = Get-WatchdogTaskState -TaskToRead $task"
_ERROR_CAPTURE = r"$disableError = $_.Exception.Message"
# 把脚本里的条件原文拼进生成的探针，这是唯一的护栏，宁可窄：只许这些字符
_PRED_ALLOWED = re.compile(r"^[\w$().=&'\- ]+$")
_TAIL_ALLOWED = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")

# 探针里造的三种答案：名字 / Enabled / EnabledSource / 期望落到哪一支
# 期望值不是猜的：`down` = 支 1（这次运行拥有这次停用，会尝试恢复），`up` = 支 2
# （不拥有、不恢复），`fallthrough` = 兜底 fail-safe（当作可能已停用，尝试恢复）。
# `enabled-null-answer` 这一例 helper 今天造不出来（$enabled 只有 $true 默认与 [bool] 读数两种
# 赋值，见机器真相锁 _fail_closed_named_ok），留着是因为它钉的是**平局的方向**：
# 一个读起来不像答案的答案，必须落在「仍然尝试恢复」那一侧，而不是「不拥有停用」那一侧。
_CASES = (
    ("down", "$false", "'settings-enabled'", "down"),
    ("up", "$true", "'settings-enabled'", "up"),
    ("unreadable-read-threw", "$true", "'read-threw'", "fallthrough"),
    ("unreadable-settings-enabled-missing", "$true", "'settings-enabled-missing'", "fallthrough"),
    ("unreadable-settings-missing", "$true", "'settings-missing'", "fallthrough"),
    ("unreadable-task-missing", "$true", "'task-missing'", "fallthrough"),
    ("unreadable-empty-source", "$true", "''", "fallthrough"),
    ("enabled-null-answer", "$null", "$null", "down"),
)


# ------------------------------------------------------------------ 定位：try 与 handler
def _disable_try_span(lines: list, lo: int, hi: int):
    """包住 disable 那一句的 try 范围 (start, end)；不在任何 try 里就 None。

    起点必须**早于** lo：`_try_spans` 从窗口第一行开始扫，而窗口第一行就是 disable 本身，
    所以从 lo 起扫永远看不见包着它的那颗 try。
    """
    spans = _try_spans(lines, max(0, lo - _TRY_LOOKBACK), hi)
    span = _span_of(spans, lo)
    if span is None:
        return None
    s, e = span
    if not _TRY_OPEN.match(lines[s]) or not (s < lo <= e):
        return None
    return span


def _ctx(text: str):
    """定位本票那一段的全部锚点；任何一环找不到都返回 None（判据随后一律红）。"""
    lines, lo, hi = _window(text)
    if lines is None:
        return None
    span = _disable_try_span(lines, lo, hi)
    if span is None:
        return None
    catch = _idx(r"\}\s*catch\s*\{", lines, span[0], span[1])
    if catch is None or catch >= span[1]:
        return None
    handler_start = catch + 1
    handler = lines[handler_start:span[1]]
    if not handler:
        return None
    return {"lines": lines, "lo": lo, "hi": hi, "span": span, "catch": catch,
            "handler_start": handler_start, "handler": handler,
            "handler_text": "\n".join(handler)}


def _if_blocks(handler: list):
    """handler 里的顶层 if 块 [(start_rel, end_rel, pred_text)]；任何一处不配平就 None。

    花括号是真的配平的（`if (… ) {` 进、独立 `}` 出），不是猜缩进。
    """
    blocks = []
    i = 0
    while i < len(handler):
        m = _IF_LINE.match(handler[i])
        if m:
            depth = 0
            j = i
            while j < len(handler):
                depth += handler[j].count("{") - handler[j].count("}")
                if depth <= 0 and j > i:
                    break
                j += 1
            if j >= len(handler) or depth > 0:
                return None
            blocks.append((i, j, m.group("pred")))
            i = j + 1
        else:
            i += 1
    return blocks


def _predicates(text: str):
    """handler 里那两条判决条件**原文**（去掉 `if (` 与 `) {`）；不是恰好两条就 None。"""
    ctx = _ctx(text)
    if ctx is None:
        return None
    blocks = _if_blocks(ctx["handler"])
    if blocks is None or len(blocks) != 2:
        return None
    pred1, pred2 = blocks[0][2], blocks[1][2]
    for pred in (pred1, pred2):
        if "$disableProbe" not in pred or not _PRED_ALLOWED.match(pred):
            return None
    if pred1 == pred2:
        return None
    return pred1, pred2, blocks


def _helper_fields(text: str):
    """从 helper 的 `return [pscustomobject]@{…}` 那一行抽出字段名（探针对象与它同名）。"""
    body = _func_body(text, "Get-WatchdogTaskState")
    if body is None:
        return None
    joined = "\n".join(body)
    m = re.search(r"return \[pscustomobject\]@\{(?P<fields>.+?)\}", joined, re.S)
    if not m:
        return None
    names = []
    for part in m.group("fields").split(";"):
        mm = re.match(r"\s*(?P<n>[A-Za-z_][A-Za-z0-9_]*)\s*=", part)
        if not mm:
            return None
        names.append(mm.group("n"))
    return names


# ------------------------------------------------------------------ 判据本体
def _try_wraps_and_routes(text: str) -> bool:
    """(i) disable 在 try 里，catch 走 Stop-AfterFailure，且 handler 自己没有裸出口。"""
    ctx = _ctx(text)
    if ctx is None:
        return False
    s, e = ctx["span"]
    lines, lo = ctx["lines"], ctx["lo"]
    # 顶格形状是刻意的（见脚本里那段注释）：三处顺序锁按第 0 列认这一步
    if not _DISABLE_COL0.match(lines[lo]):
        return False
    flag = _idx(_FLAG_LINE_COL0, lines, lo, ctx["hi"])
    if flag is None or flag <= lo or flag > e:
        return False
    if not _catch_routes_to_restore(lines, ctx["span"]):
        return False
    if any(_BARE_EXIT.match(ln) for ln in ctx["handler"]):
        return False
    stages = [_SAFETY_LINE.match(ln).group("stage")
              for ln in ctx["handler"] if _SAFETY_LINE.match(ln)]
    if len(stages) != 3 or set(stages) != set(_STAGES):
        return False
    # 顺序：登记在两条 if 之前不可能成立，所以这里只判 handler 最后一句是中止
    last_safety = max(i for i, ln in enumerate(ctx["handler"]) if _SAFETY_LINE.match(ln))
    trailing = [ln for ln in ctx["handler"][last_safety + 1:] if ln.strip()]
    if trailing:
        return False
    return True


def _handler_asks_the_machine(text: str) -> bool:
    """(ii) handler 只问现成的只读 helper，绝不自己写任务，也不自己再读一遍属性。"""
    ctx = _ctx(text)
    if ctx is None:
        return False
    handler_text = ctx["handler_text"]
    if handler_text.count(_READ_CALL) != 1:
        return False
    if "Get-WatchdogTaskState" not in handler_text:
        return False
    # 不许 handler 自己抄一份读法：那就有两处真相，而本票要钉的正是「问的是哪一处」
    if "Get-ScheduledTask" in handler_text or "[bool]" in handler_text:
        return False
    _no_write_cmdlets(handler_text, "disable 失败 handler 本体")
    return True


def _branches_and_failsafe(text: str) -> bool:
    """(iii) 三种答案 → 三种归属，且每条路都以自己的 stage token 中止。"""
    ctx = _ctx(text)
    if ctx is None:
        return False
    preds = _predicates(text)
    if preds is None:
        return False
    pred1, pred2, blocks = preds
    b1, b2 = blocks[0], blocks[1]
    body1, body2 = ctx["handler"][b1[0] + 1:b1[1]], ctx["handler"][b2[0] + 1:b2[1]]
    tail = ctx["handler"][b2[1] + 1:]
    joined_t = "\n".join(tail)

    # 支 1：开关答「已停用」。必须有 $disableProbe 本体守卫（$null 不许抢走 fail-safe），
    # 必须**先登记再中止**——登记就是「这次运行按下去过」，恢复路径靠它把看守还回来。
    if "-not $disableProbe.Enabled" not in pred1 or "$disableProbe -and" not in pred1:
        return False
    i_flag1 = _idx(_FLAG_TRUE.pattern, body1)
    i_abort1 = _idx(r"^\s*Stop-AfterFailure\s+'disable-threw-task-down'", body1)
    if i_flag1 is None or i_abort1 is None or i_flag1 > i_abort1:
        return False

    # 支 2：开关亲口答「仍在启用」。这条**不许**登记——没按下去过就不谎称重新启用。
    if "$disableProbe.Enabled" not in pred2 or "EnabledSource -eq 'settings-enabled'" not in pred2:
        return False
    if _idx(_FLAG_TRUE.pattern, body2) is not None:
        return False
    if _idx(r"^\s*Stop-AfterFailure\s+'disable-threw-task-up'", body2) is None:
        return False

    # 支 3：答不了 = 兜底 fallthrough，必须真的尝试恢复（登记 + 自己的 stage 中止）。
    if _idx(_FLAG_TRUE.pattern, tail) is None:
        return False
    i_abort3 = _idx(r"^\s*Stop-AfterFailure\s+'disable-threw-guard-state-unproven'", tail)
    if i_abort3 is None:
        return False
    if any(_BARE_EXIT.match(ln) for ln in tail):
        return False
    # 兜底写在两条 if 之后，而不是第三条 if：以后有人加分支忘了中止也仍然落在这里
    if "if (" in joined_t:
        return False
    return True


def _token_line_ok(text: str) -> bool:
    """(iv) 这条路留下一行 token，且它只报读到的原始答案、不自报分类。"""
    ctx = _ctx(text)
    if ctx is None:
        return False
    hits = [i for i, ln in enumerate(ctx["handler"]) if _TOKEN_LINE.match(ln)]
    if len(hits) != 1:
        return False
    i = hits[0]
    token = ctx["handler"][i]
    # $_ 要转成具名变量，且排在 token 那一行之前。不要求它是进 catch 的第一句：
    # 本机实测 PS 5.1 的 $_ 能穿过下面那个嵌套 try 的 catch（#46），真正的约束
    # 只是拼 token 时读的是抓下来的那份。
    if _idx(re.escape(_ERROR_CAPTURE), ctx["handler"]) is None:
        return False
    if _idx(re.escape(_ERROR_CAPTURE), ctx["handler"]) > i:
        return False
    for field in ("' via=' + $disableProbe.EnabledSource", "' state=' + $disableProbe.State",
                  "' instances=' + $disableProbe.Instances", "' error=' + $disableError"):
        if field not in token:
            return False
    # 分类不许自报（判据由被测对象自己说 = 它可以不说）
    if re.search(r"(guard|branch|owns|verdict)=", token):
        return False
    # 必须在两条判决之前：先说话再分支，三种答案都留得下同一行证据
    blocks = _if_blocks(ctx["handler"])
    if blocks is None or i > blocks[0][0]:
        return False
    return True


# ------------------------------------------------------------------ 生成的只读探针
def _probe_case_lines() -> str:
    rows = []
    for name, enabled, source, _expect in _CASES:
        rows.append(f"    [pscustomobject]@{{ Name='{name}'; Enabled={enabled}; "
                    f"EnabledSource={source} }},")
    # 「整句重读什么都没返回」不是 Enabled 为空，而是对象本身为 $null：形状不同，判据也不同
    rows.append("    [pscustomobject]@{ Name='probe-null-object'; Enabled=$null; EnabledSource=$null }")
    return "\n".join(rows)


def _build_probe_text(pred1: str, pred2: str, var: str, tail: str, fields) -> str:
    """把脚本自己那两条条件与 helper 自己的字段名抄进探针源码；校验不过一律 None。"""
    if not pred1 or not pred2 or not _PRED_ALLOWED.match(pred1) or not _PRED_ALLOWED.match(pred2):
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var) or not _TAIL_ALLOWED.match(tail):
        return None
    if tuple(fields or ()) != _HELPER_FIELDS:
        return None
    value_of = {"Enabled": "$c.Enabled", "State": "'Ready'", "Instances": "0",
                "ReadError": "''", "EnabledSource": "$c.EnabledSource"}
    if any(n not in value_of for n in fields):
        return None
    obj = "[pscustomobject]@{ " + "; ".join(f"{n}={value_of[n]}" for n in fields) + " }"
    real_obj = ("[pscustomobject]@{ " + "; ".join(
        f"{n}=" + {"Enabled": "$enabled", "State": "[string] $fresh.State",
                   "Instances": "$(if ($fresh.State -eq 'Running') { 1 } else { 0 })",
                   "ReadError": "''", "EnabledSource": "$enabledSource"}[n] for n in fields) + " }")
    text = f"""$ErrorActionPreference = 'Stop'
'psversion=' + $PSVersionTable.PSVersion.ToString()
$cases = @(
{_probe_case_lines()}
)
foreach ($c in $cases) {{
    if ($c.Name -eq 'probe-null-object') {{
        $disableProbe = $null
    }} else {{
        $disableProbe = {obj}
    }}
    $p1 = 'False'
    $p2 = 'False'
    if ({pred1}) {{ $p1 = 'True' }}
    if ({pred2}) {{ $p2 = 'True' }}
    'case name=' + $c.Name + ' p1=' + $p1 + ' p2=' + $p2
}}

$wd = @(Get-ScheduledTask | Where-Object {{
    $_.Actions.Execute -eq 'wscript.exe' -and $_.Actions.Arguments -like '*watchdog-hidden.vbs*'
}})
'watchdog_count=' + $wd.Count
if ($wd.Count -eq 1) {{
    $fresh = Get-ScheduledTask -TaskPath $wd[0].TaskPath -TaskName $wd[0].TaskName
    $enabled = $true
    $enabledSource = 'unread'
    if ($null -eq $fresh.Settings) {{ $enabledSource = 'settings-missing' }}
    elseif ($null -eq $fresh.{tail}) {{ $enabledSource = 'settings-enabled-missing' }}
    else {{ $enabled = [bool] $fresh.{tail}; $enabledSource = 'settings-enabled' }}
    $disableProbe = {real_obj}
    'real_enabled=' + [string] $enabled
    'real_source=' + $enabledSource
    'real_state=' + [string] $fresh.State
    $p1 = 'False'
    $p2 = 'False'
    if ({pred1}) {{ $p1 = 'True' }}
    if ({pred2}) {{ $p2 = 'True' }}
    'real_p1=' + $p1
    'real_p2=' + $p2
}}
'probe_done=yes'
"""
    # 接线证明：探针里跑的确实是抄出来的那两条条件与那个字段集，不是「另一条也行的写法」
    if text.count(f"if ({pred1}) {{ $p1 = 'True' }}") != 2 or \
       text.count(f"if ({pred2}) {{ $p2 = 'True' }}") != 2:
        return None
    if f"$fresh.{tail}" not in text or f"$enabled = [bool] $fresh.{tail}" not in text:
        return None
    for name in fields:
        if f"{name}=" not in text:
            return None
    if not text.isascii():
        return None
    return text


def _probe_source_for(text: str):
    """从一份 .ps1 文本（真文件或副本）抽出条件并生成探针源码。"""
    preds = _predicates(text)
    site = _enabled_read_site(text)
    fields = _helper_fields(text)
    if preds is None or site is None or fields is None:
        return None
    return _build_probe_text(preds[0], preds[1], site[0], site[1], fields)


def _parse_cases(stdout: str) -> dict:
    out = {}
    for ln in (x.strip() for x in stdout.splitlines()):
        m = re.fullmatch(r"case name=(\S+) p1=(True|False) p2=(True|False)", ln)
        if m:
            out[m.group(1)] = (m.group(2) == "True", m.group(3) == "True")
    return out


def _classify(p1: bool, p2: bool) -> str:
    if p1 and p2:
        return "BOTH"
    if p1:
        return "down"
    if p2:
        return "up"
    return "fallthrough"


def _classify_all(cases: dict) -> dict:
    return {name: _classify(p1, p2) for name, (p1, p2) in cases.items()}


_POWERSHELL = _find_powershell()


def _skip_machine() -> None:
    pytest.skip("这台机器没有 powershell.exe，机器路由那两条跑不了。静态那几条不受影响、照判；"
                "但本票「三种答案各归各的效果」是这台机器判的，放行复跑必须在 Windows 现网机上做。")


def _exec_probe(probe_path: Path):
    return subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(probe_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=PROBE_TIMEOUT_SECONDS)


def _run_probe(text_source: str, work_dir: Path):
    """在**仓库外**的 work_dir 里生成并只读跑一次探针，返回 (kv, cases, stdout)。"""
    if _POWERSHELL is None:
        _skip_machine()
    probe_path = work_dir / "probe-disable-threw-routing.ps1"
    probe_path.write_text(text_source, encoding="ascii")
    proc = _exec_probe(probe_path)
    assert proc.returncode == 0, (
        f"探针不是只读跑通的（rc={proc.returncode}）：{proc.stderr.strip()[:400]}\n"
        f"探针源码：{probe_path}")
    kv, _samples = _parse_probe_output(proc.stdout)
    assert kv.get("probe_done") == "yes", f"探针没走到最后一行：{proc.stdout[:400]!r}"
    return kv, _parse_cases(proc.stdout), proc.stdout


@pytest.fixture(scope="module")
def routing_probe(tmp_path_factory):
    source = _probe_source_for(PS1)
    assert source is not None, "探针源码生不出来：条件、字段或读法抽不出来（见 _probe_source_for）"
    work = tmp_path_factory.mktemp("disable-threw-routing")
    return _run_probe(source, work)


# ------------------------------------------------------------------ 静态锁
def test_handler_is_locatable_and_locks_are_not_judging_an_empty_set():
    """前提锁：try / catch / 两条判决 / 三个 stage / 字段集各自必须**恰好**定位得到。

    「锚点找不到」与「锚点找到了但内容不对」必须是两种红法，所以先单独钉这一条。
    """
    ctx = _ctx(PS1)
    assert ctx is not None, (
        "disable 那一句没有被 try 包住（或 try 的 catch 里没有 Stop-AfterFailure）："
        "本票要堵的就是这个——裸的两句在 $ErrorActionPreference='Stop' 下会把这次换包"
        "判死在没有 stage token、没有恢复尝试的地方")
    s, e = ctx["span"]
    assert s < ctx["lo"] <= e, f"try 范围没有包住 disable 那一行：{_span_dump(ctx)}"
    assert _predicates(PS1) is not None, "handler 里必须恰好有两条判决 if（见 _predicates）"
    assert _helper_fields(PS1) == list(_HELPER_FIELDS), (
        f"helper 的 return 字段集变了（实得 {_helper_fields(PS1)}）：探针造的对象与它不同名，"
        "机器路由那两条就无从接线")
    site = _enabled_read_site(PS1)
    assert site is not None and site[1] != "Enabled", "读法抽不出来或回到了裸 Enabled 成员"


def test_disable_call_is_inside_a_try_whose_catch_routes_to_the_abort_machinery():
    assert _try_wraps_and_routes(PS1), (
        "停用看门狗那一句不在「catch 里调用 Stop-AfterFailure」的 try 里，或 handler 自己"
        "留了裸出口/中止 stage 不再是那三个：这条路一抛就把脚本带走，"
        "看守状态从此无人登记（ticket #32 的洞）")


def test_handler_asks_the_existing_read_helper_and_never_writes_the_task_itself():
    assert _handler_asks_the_machine(PS1), (
        "disable 失败的 handler 不再只问 Get-WatchdogTaskState，或自己动手写任务/自己抄一份"
        "属性读法：重新启用只许住在 Restore-WatchdogTask 一处，两处拥有一个开关就是第二个洞")


def test_three_answers_route_to_three_different_ownership_effects():
    assert _branches_and_failsafe(PS1), (
        "三种答案（开关答已停用 / 开关答仍在启用 / 答不了）不再各归各的效果："
        "已停用必须先登记 watchdogDisabled 再中止，仍在启用必须**不**登记，答不了必须落进"
        "兜底 fail-safe 并登记——少一条就有下一次换包把崩溃守护留在地上")


def test_disable_threw_token_is_printed_once_before_the_branches_and_claims_no_outcome():
    assert _token_line_ok(PS1), (
        "DISABLE-THREW 那行不在、不止一处、排到了分支之后、自己报了分类结论，"
        "或者 $disableError 不是进 catch 第一句抓下来的：运维从换包那一行输出里"
        "就分不出「看守还在」与「开关没答话」")


def test_probe_is_wired_to_the_scripts_own_predicates_and_stays_read_only():
    """接线锁 + 只读锁：探针跑的就是脚本抄出来的那两条条件、那个字段集、那条读法。

    没有这一条，机器那两条完全可以靠「另写一条更好看的条件」恒绿。
    """
    preds = _predicates(PS1)
    assert preds is not None
    pred1, pred2, _blocks = preds
    source = _probe_source_for(PS1)
    assert source is not None, "探针源码生不出来（_build_probe_text 的校验没过）"
    assert f"if ({pred1}) {{ $p1 = 'True' }}" in source, "支 1 的条件原文没有落到探针里"
    assert f"if ({pred2}) {{ $p2 = 'True' }}" in source, "支 2 的条件原文没有落到探针里"
    site = _enabled_read_site(PS1)
    assert f"$enabled = [bool] $fresh.{site[1]}" in source, "helper 的读法没有落到真任务那次按名新读"
    assert source.isascii(), "生成的探针必须纯 ASCII：PS 5.1 会把带中文的无 BOM .ps1 错解码成语法错误"
    _no_write_cmdlets(source, "生成的探针")
    assert "Get-ScheduledTask" in source, "探针没问机器，问的是空气"


# ------------------------------------------------------------------ 机器两条
def test_machine_routes_each_answerable_case_through_the_scripts_own_predicates(routing_probe):
    """机器判据：三种答案在**这台机器**上真的各落到各自的分支。

    - 开关答 False → 只有支 1 真（这次运行拥有这次停用）；
    - 开关答 True  → 只有支 2 真；
    - 答不了（四种失败出口 + EnabledSource 空串）→ 两条都假，落进兜底 fail-safe；
    - 重读整句没返回对象（`$disableProbe` 是 $null）→ 也必须两条都假：少了 `$disableProbe -and`
      这层守卫，`$null.Enabled` 给 $null、`-not $null` 给 True，fail-safe 会被支 1 抢走；
    - 而 helper 造不出来的「Enabled 读起来是空」这一例，钉的是**平局方向**：它落进支 1，
      也就是「仍然尝试恢复」那一边，不是「这次停用不归我」那一边（这台机器实测，不是文本推断）。
    任何一例出现「两条同时真」都算红：那说明分支互斥不成立，谁登记了停用就不确定了。
    """
    kv, cases, stdout = routing_probe
    assert kv.get("watchdog_count") == "1", f"看门狗任务没唯一找到（探针与脚本脱钩）：{kv}"
    expected = {name: expect for name, _e, _s, expect in _CASES}
    expected["probe-null-object"] = "fallthrough"
    assert cases and set(cases) == set(expected), f"探针的 CASE 行缺例或多例：{stdout[:400]!r}"
    got = _classify_all(cases)
    for name, want in expected.items():
        assert got.get(name) == want, (
            f"用例 {name} 本该落进 {want}，这台机器按脚本自己的条件判成了 {got.get(name)}"
            f"（p1={cases.get(name)}）——handler 的归属判据变了，本票的洞会重新打开")


def test_machine_switch_answers_for_the_real_watchdog_task_so_the_failsafe_is_not_the_norm(
        routing_probe):
    """机器判据·真任务：helper 那条读法对本机那个看门狗任务**答得出**，
    于是支 1/支 2 才是日常路径，fail-safe 只是兜底（答不出就是机器形状变了，见机器真相锁）。

    这条不判看守现在到底开着还是关着——那是现网状态，归 /health 与计划任务看；
    本条只判「问得出来」，且全程只读。
    """
    kv, _cases, stdout = routing_probe
    assert kv.get("real_source") == "settings-enabled", (
        f"真看门狗任务按脚本那条读法没答出开关本体（real_source={kv.get('real_source')}）："
        f"那么本次换包只要 disable 一抛就会落进 fail-safe，{stdout[:200]!r}")
    assert kv.get("real_enabled") in ("True", "False"), f"读到的不是布尔字面量：{kv}"
    p1, p2 = kv.get("real_p1"), kv.get("real_p2")
    assert (p1, p2) in (("True", "False"), ("False", "True")), (
        f"真任务的读数在脚本自己的两条条件下落到 p1={p1} p2={p2}——三种答案的划分在这台机器上判不出来了")


# ------------------------------------------------------------------ 漂移样本
_BARE_DISABLE = "Disable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null"
_BARE_FLAG = "$script:watchdogDisabled = $true"


def _drift_to_pre_ticket_bare_shape(text: str):
    """把 try 与整个 handler 一起摘掉，还原成**本票之前那两句裸的**（顶格、注释保留）。"""
    ctx = _ctx(text)
    if ctx is None:
        return None
    lines = _code_lines(text)
    s, e = ctx["span"]
    if lines[ctx["lo"]] != _BARE_DISABLE or lines[ctx["lo"] + 1] != _BARE_FLAG:
        return None
    kept = lines[:s] + [_BARE_DISABLE, _BARE_FLAG] + lines[e + 1:]
    return "\n".join(kept)


def _drift_replace(text: str, old: str, new: str, label: str) -> str:
    assert old in text, f"漂移样本没生效：{label} 的锚点不在文件里"
    drifted = text.replace(old, new, 1)
    assert drifted != text, f"漂移样本没生效：{label} 一个字节都没改"
    return drifted


def test_mutation_unwrapping_the_try_turns_the_new_locks_red(tmp_path):
    """牙印一：回到本票之前那两句裸的（去掉 try 与 handler）。

    关键分工：旧的两条锁对这份样本**照旧绿**——那正是这个洞能藏到下一次发版的原因，
    也是本文件必须存在而不是给旧文件加一条 risky 的原因。
    样本另落一份盘（tmp_path，仓库外），证明判据吃的是文件而不是内存里的幻想。
    """
    bare = _drift_to_pre_ticket_bare_shape(PS1)
    assert bare is not None, "样本构造失败：try 里的两句不再是预期的顶格形状"
    copy = tmp_path / "swap-package-pre-ticket-bare-disable.ps1"
    copy.write_text(bare, encoding="utf-8")
    assert copy.read_text(encoding="utf-8") == bare, "副本没落盘：牙齿咬的是内存而不是文件"

    assert not _try_wraps_and_routes(bare), "把 try 摘掉，包裹+路由这条锁必须红"
    assert not _handler_asks_the_machine(bare), "handler 没了，问机器这条必须红"
    assert not _branches_and_failsafe(bare), "三种答案没了，归属这条必须红"
    assert not _token_line_ok(bare), "DISABLE-THREW 那行没了，token 这条必须红"
    assert _probe_source_for(bare) is None, "样本里连条件都抽不出来时，机器路由的接线锁必须生不出探针"
    # 分工的证据：本票之前那 13 条锁对这份样本全绿——洞不在它们的射程里
    assert _window(bare)[0] is not None, "样本必须仍能定位窗口，否则下面两条绿得没有意义"
    assert _verdict_ok(bare), "判决锁在裸形状上应当照旧绿（它判的是停用之后，不是这一句自己抛）"
    assert _no_bare_exit_ok(bare), (
        "出口锁在裸形状上应当照旧绿：它管的是 disable **之后**到 backupDone 之间的裸出口，"
        "红绿分工错了就说明本文件与旧锁在重复计同一件事")


def test_mutation_handler_that_stops_asking_the_machine_turns_the_read_lock_red():
    """牙印二：把重读换成「先假定什么都没动」——handler 不再问机器，读一条红。

    这正是那条会静默丢守护的写法：`$disableProbe = $null` 之后，三种答案塌成一种，
    而脚本看起来仍然「有 try、有 catch、有 Stop-AfterFailure」。
    """
    ctx = _ctx(PS1)
    assert ctx is not None, "真文件必须先绿"
    drifted = _drift_replace(
        PS1,
        "    $disableProbe = $null\n    try { $disableProbe = Get-WatchdogTaskState -TaskToRead $task } catch { $disableProbe = $null }",
        "    $disableProbe = $null",
        "把只读重读摘掉")
    assert _try_wraps_and_routes(drifted), (
        "摘掉重读之后 try/路由这条应当照旧绿：红必须归给「handler 不再问机器」那条，不是两条混在一起")
    assert not _handler_asks_the_machine(drifted), "handler 不再问现成的只读 helper，这条必须红"
    assert _predicates(drifted) is not None and _probe_source_for(drifted) is not None, \
        "样本里条件仍应抽得出（这条牙印只摘重读）"


def test_mutation_dropping_the_failsafe_or_the_ownership_flag_turns_the_branch_lock_red():
    """牙印三/四：删掉兜底的登记，或让支 2 也登记「这次停用归我」，归属这条必须红。

    前者 = 答不了就不恢复（看守可能留在地上）；后者 = 从没按下去的中止谎称重新启用过。
    """
    assert _branches_and_failsafe(PS1), "真文件必须先绿"
    no_failsafe = _drift_replace(
        PS1,
        "    $script:watchdogDisabled = $true\n"
        "    Stop-AfterFailure 'disable-threw-guard-state-unproven'",
        "    Stop-AfterFailure 'disable-threw-guard-state-unproven'",
        "删掉兜底登记")
    assert not _branches_and_failsafe(no_failsafe), "答不了却不尝试恢复，归属这条锁必须红"

    lying_up = _drift_replace(
        PS1,
        "        Stop-AfterFailure 'disable-threw-task-up'",
        "        $script:watchdogDisabled = $true\n"
        "        Stop-AfterFailure 'disable-threw-task-up'",
        "让支 2 也登记停用")
    assert not _branches_and_failsafe(lying_up), (
        "「开关说仍在启用」那条也登记了停用归属，token 就会声称重新启用一个从没按下去的开关")


def test_mutation_handler_that_enables_the_task_directly_turns_the_write_lock_red():
    """牙印五：handler 自己动手 Enable-ScheduledTask「把看守扶回来」——只读这条必须红。

    看起来更直接、更负责，实际是把「谁拥有这个开关」变成两处，恢复机制自己那份登记
    （watchdogDisabled）与它互不知情；这正是本仓反复吃过的那类「两处真相」。
    """
    assert _handler_asks_the_machine(PS1), "真文件必须先绿"
    direct = _drift_replace(
        PS1,
        "    $script:watchdogDisabled = $true\n    Stop-AfterFailure 'disable-threw-guard-state-unproven'",
        "    Enable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null\n"
        "    Stop-AfterFailure 'disable-threw-guard-state-unproven'",
        "改成 handler 直接启用任务")
    with pytest.raises(AssertionError) as exc:
        _handler_asks_the_machine(direct)
    assert "Enable-ScheduledTask" in str(exc.value), f"红的理由必须是「自己动手写任务」：{exc.value}"
    assert not _branches_and_failsafe(direct), "兜底不再登记停用归属，归属这条也必须一起红"


def _weakened(pred2: str) -> str:
    """削弱样本：去掉「且这是开关亲口答的」那一半——本票最容易被「顺手简化」掉的正是它。"""
    return pred2.replace(" -and $disableProbe.EnabledSource -eq 'settings-enabled'", "", 1)


def test_mutation_dropping_the_enabledsource_answer_reds_the_machine_routing(tmp_path):
    """牙印六（机器侧）：把支 2 的条件里「是开关亲口答的」那一半摘掉，机器必须判出差别。

    文本侧也红，但这条要的是：**这台机器**告诉你 `read-threw` / `task-missing` 这类
    「答不了」的答案会被削弱后的条件认成「仍在启用」，于是兜底 fail-safe 变成死支、
    看守被留在地上。对照跑的是**同一份**探针模板、只把条件换成削弱版，
    所以红是机器判的，不是文本判的。
    """
    preds = _predicates(PS1)
    assert preds is not None
    pred1, pred2, _blocks = preds
    assert _branches_and_failsafe(PS1), "真文件必须先绿"
    weak = _weakened(pred2)
    assert weak != pred2, "样本没生效：支 2 的条件形状变了，请把 _weakened 跟上真实代码"

    # 文本侧：削弱版过不了归属锁
    weakened_text = _drift_replace(PS1, pred2, weak, "削弱支 2 的条件")
    assert not _branches_and_failsafe(weakened_text), "削弱条件必须过不了归属锁"

    site = _enabled_read_site(PS1)
    source = _build_probe_text(pred1, weak, site[0], site[1], _helper_fields(PS1))
    assert source is not None, "削弱后的条件生不出探针源码"
    assert f"if ({weak}) {{ $p2 = 'True' }}" in source, "探针跑的条件与削弱样本不一致"
    _no_write_cmdlets(source, "削弱样本的探针")
    _kv, cases, stdout = _run_probe(source, tmp_path)
    got = _classify_all(cases)
    assert got.get("down") == "down", f"削弱样本里「已停用」那一例仍应落支 1：{got}"
    assert got.get("up") == "up", f"「仍在启用」那一例仍应落支 2：{got}"
    assert got.get("unreadable-read-threw") == "up", (
        "把「是开关亲口答的」摘掉后，read-threw 这类答不了竟然没被认成「仍在启用」——"
        f"机器没有在判脚本的条件，本条牙印是假的：{stdout[:200]!r}")
    assert got.get("unreadable-task-missing") == "up", f"task-missing 同样被抢走：{got}"
    assert got.get("unreadable-empty-source") == "up", f"空 via= 同样被抢走：{got}"
    assert got.get("probe-null-object") == "fallthrough", (
        f"连 $null 对象都被削弱版认成「仍在启用」，那本体守卫也没了：{got}")


# ------------------------------------------------------------------ 辅助打印（只给断言消息用）
def _span_dump(ctx) -> str:
    s, e = ctx["span"]
    return f"try@{s + 1}..{e + 1} disable@{ctx['lo'] + 1} catch@{ctx['catch'] + 1}"
