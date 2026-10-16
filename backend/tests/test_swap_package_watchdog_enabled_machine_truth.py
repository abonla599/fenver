r"""看门狗开关的**机器真相锁**：判「脚本读的那个属性位置在这台机器上到底存不存在」。

为什么本文件存在（一句话版）：`deploy/swap-package.ps1` 停用看门狗之后的判决，先被
`a5d37bde` 之前的 `State` 误判坑过一次（实例在跑时它恒报 Running），同一次提交把判决换成
读 `Enabled` 之后，**换成了一个在这台机器上不存在的成员**。文本锁对这两次都毫无反应——
它判的是「代码里写了 Enabled」，而「写了」与「读得到」是两件事。

2026-10-18 在本机（PowerShell 5.1.26100.9549）只读探针实测：

  has-Enabled-member(PSBase.Properties.Name -contains Enabled) = False
  Get-Member -Name Enabled count = 0
  bare-Enabled-non-null-count = 0            （202 个任务里 0 个答话）
  Settings.Enabled-eq-false-count = 31       （31 个真 State=Disabled 的全答 False）
  disabled-and-SettingsEnabled-false = 31    （阳性对照：31/31）
  Get-ScheduledTaskInfo has-Enabled-member = False
  MSFT_ScheduledTask has-Enabled-member = False（它也只是把开关放在 Settings 里）

也就是说 `(Get-ScheduledTask …)` 回来的是 `MSFT_ScheduledTask` 这个 CIM 实例，它的顶层成员是
TaskName / TaskPath / State / Actions / Triggers / Principal / **Settings**，`Enabled` 住在
Settings 那一层里。CIM 实例读一个不存在的成员**不报错、不抛异常，安静地给 $null**，于是脚本里
`if ($null -ne $fresh.Enabled) { … }` 这条「读到了就用、读不到就 fail closed」的写法，
**永远走「读不到」那一支**：默认值 $true 成了永久状态，通过条件
`-not $verify.Enabled -and $verify.Instances -eq 0` 永远不成立，于是每一次换包都会烧完那 15 秒
窗口再以 `disable-did-not-take` 中止——安全（一个字节都不动，看守照原样还回去），
但 100% 挡住发版。

方向要说清楚：`a5d37bde` 修的那个缺陷是真的（读 State 判据确实误判在跑的实例），
它换来换去换的是**同一个位置的另一半**——把「读错的属性」换成「读一个没有的属性」。
而它配的那 13 条锁全是文本/控制流判据（`test_swap_package_disable_verify.py`），
一条都看不见机器形状：那 13 条对坏代码和对本代码同样绿。本文件就是补那一层——
**判据的答案由这台机器给，不由文本给**。

本文件怎么做到「不是同义反复」：
1. `_enabled_read_site()` 从脚本 `Get-WatchdogTaskState` 的函数体里**抽出判决那一行实际读的
   属性路径**（今天抽出来是 `Settings.Enabled`）；
2. 探针把**抽出来的那条表达式**原样写进一份生成的 .ps1 里跑
   （`test_probe_evaluates_the_very_expression_the_script_reads` 钉这次接线：跑的就是
   `$fv = $fresh.<抽出来的路径>` 与 `$vTest = $a.<抽出来的路径>` 两行）；
3. **阳性对照**：这台机器上真有一个 `State=Disabled` 的任务，用脚本自己那条表达式读出来必须是
   `False` 且类型是 `Boolean`——不是「读到了东西」，是「读对了东西」。这一条在表达式是裸
   `.Enabled` 时不可能成立（31 个全给 $null），所以它不是恒真；
4. **阴性对照**：同一次探针里**固定**再读一次历史坏写法（裸 `$a.Enabled`），断言它在这台机器上
   一个都不答话。这条不读脚本，纯讲机器事实，它存在的意义是：将来有人「简化」回裸写法时，
   红的不是本条，而是第 2/3 条——那条红的消息会直接把原因说给他听。

牙印（`test_mutation_bare_enabled_read_turns_the_machine_truth_lock_red`）：把判决那一行改成
`$enabled = [bool] $fresh.Enabled` 造一份**仓库外**的副本（`tmp_path`，理由与
`SWAP_PS1_UNDER_TEST` 同源：本仓库目录同时是生产机），然后
- 静态那条红（读的位置回到了不存在的成员）；
- **机器那几条也红**（探针拿的是**副本自己**抽出来的表达式，于是那 31 个真停用任务一个都不答
  False，阳性对照塌）。
第二条才是本票的意义：机器参与了判决，红是机器判的，不是文本判的。
同一次实验也可以用 `SWAP_PS1_UNDER_TEST=<副本>` 跑整个文件复现（作者留这个变量的本意），
两份证据在票面回执里都贴了。

平台闸门与「不静默跳过」：机器探针需要 `powershell.exe`（Windows）。没有就 `pytest.skip`，
且 skip 理由里明说**静态那几条不受影响、照跑照判**——本文件不是「整块跳过」的跨平台测试：
Linux/CI 上「读的位置 / 接线 / fail-closed / via= token / 只读性」这五条仍然真跑，
只有「问机器」的三条跳。在那台机器上本文件就退化成 `test_swap_package_disable_verify.py`
的文本判据水平，而那正是它挡不住本次 bug 的原因，所以这道门的复跑必须在 Windows 现网机上做。

探针全程只读：只调 `Get-ScheduledTask`（枚举 + 两次按 TaskPath/TaskName 的新读，其中一次正是
脚本 helper 用的那一句），**绝不**调用 Disable/Enable/Unregister/Register/Start/Stop/Set/New-
ScheduledTask、Stop-Process，也不写任何任务状态；被拿来做阳性对照的那个真停用任务是**从机器
现存的 State=Disabled 名单里挑的**，不是本文件造出来的，也绝不许是 `AI助手-崩溃守护`
（探针按 TaskPath+TaskName 排除，Python 侧再核一次名字不同）。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# 尺子从既有那份锁借（本仓规矩：一把尺子只有一份）：_func_body 来自 disable-verify 那份，
# _target_ps1 顺带把 SWAP_PS1_UNDER_TEST 的接线也继承过来（仓库外副本靠它跑牙印）。
from tests.test_swap_package_disable_verify import _func_body, _target_ps1

SWAP_PS1 = _target_ps1()
PS1 = SWAP_PS1.read_text(encoding="utf-8")

# 探针一次约 6 秒（202 个任务的枚举 + 两次按名新读），超时给到 240 秒足够宽松。
PROBE_TIMEOUT_SECONDS = 240

# 允许出现在属性路径里的形状：标识符（可含点分层），除此之外一律拒绝——这条白名单是
# 「把脚本里的文本片段拼进 .ps1 源码」这件事唯一的护栏，宁可窄。
_TAIL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)*$")

# 判决那一行的形状：$enabled = [bool] $<var>.<path>
_ENABLE_READ_RE = re.compile(
    r"^\s*\$enabled = \[bool\] (\$[A-Za-z_][A-Za-z0-9_]*)((?:\.[A-Za-z_][A-Za-z0-9_]*)+)\s*$")

# 历史坏写法（本文件固定复现的阴性对照）：任务对象上的裸 Enabled
_BARE_REF = "Enabled"
# 实测好位置（本文件固定参照读数，用来和「抽出来的表达式」对账）
_SETTINGS_REF = "Settings.Enabled"

# 只读探针的模板。__VAR__ / __TAIL__ 由 _build_probe_text() 校验之后替换。
# 计数器一律一行一个 key=value（SAMPLE 行除外），Python 侧按行解析，不拿正则猜整行。
_PROBE_TEMPLATE = r"""$ErrorActionPreference = 'Stop'
'tail=__TAIL__'
'psversion=' + $PSVersionTable.PSVersion.ToString()
$all = @(Get-ScheduledTask)
'total=' + $all.Count
$enumClass = ''
if ($all.Count -gt 0) { $enumClass = [string] $all[0].CimClass.CimClassName }
'enum_cimclass=' + $enumClass

$wd = @($all | Where-Object {
    $_.Actions.Execute -eq 'wscript.exe' -and $_.Actions.Arguments -like '*watchdog-hidden.vbs*'
})
'watchdog_count=' + $wd.Count
$wdPath = ''
$wdName = ''
if ($wd.Count -ge 1) { $wdPath = [string] $wd[0].TaskPath; $wdName = [string] $wd[0].TaskName }
'watchdog_name_ascii=' + ((($wdName -replace '[^\x20-\x7E]', '?') -replace '\s', '_'))

$testNonNull = 0
$testNull = 0
$testBool = 0
$testTrue = 0
$testFalse = 0
$setNonNull = 0
$setBool = 0
$bareNonNull = 0
$disabledTotal = 0
$disTestFalse = 0
$disTestTrue = 0
$disTestNull = 0
$disSetFalse = 0
$samplesShown = 0

foreach ($a in $all) {
    $vTest = $a.__TAIL__
    if ($null -eq $vTest) { $testNull = $testNull + 1 }
    else {
        $testNonNull = $testNonNull + 1
        if ($vTest -is [bool]) {
            $testBool = $testBool + 1
            if ($vTest) { $testTrue = $testTrue + 1 } else { $testFalse = $testFalse + 1 }
        }
    }
    $vSet = $a.Settings.Enabled
    if ($null -ne $vSet) {
        $setNonNull = $setNonNull + 1
        if ($vSet -is [bool]) { $setBool = $setBool + 1 }
    }
    $vBare = $a.Enabled
    if ($null -ne $vBare) { $bareNonNull = $bareNonNull + 1 }
    if ($a.State -eq 'Disabled') {
        $disabledTotal = $disabledTotal + 1
        if ($null -eq $vTest) { $disTestNull = $disTestNull + 1 }
        elseif ($vTest -is [bool]) {
            if ($vTest) { $disTestTrue = $disTestTrue + 1 } else { $disTestFalse = $disTestFalse + 1 }
        }
        if ($vSet -is [bool] -and $vSet -eq $false) { $disSetFalse = $disSetFalse + 1 }
        $isWd = (([string] $a.TaskPath -eq $wdPath) -and ([string] $a.TaskName -eq $wdName))
        if ((-not $isWd) -and ($vTest -is [bool]) -and ($vTest -eq $false) -and $samplesShown -lt 4) {
            $samplesShown = $samplesShown + 1
            'SAMPLE name=' + ((($a.TaskName -replace '[^\x20-\x7E]', '?') -replace '\s', '_')) +
                ' path=' + ((($a.TaskPath -replace '[^\x20-\x7E]', '?') -replace '\s', '_')) +
                ' state=' + $a.State + ' value=' + [string] $vTest +
                ' type=' + $vTest.GetType().Name +
                ' bare=' + $(if ($null -eq $a.Enabled) { 'NULL' } else { [string] $a.Enabled })
        }
    }
}
'test_non_null=' + $testNonNull
'test_null=' + $testNull
'test_bool=' + $testBool
'test_true=' + $testTrue
'test_false=' + $testFalse
'set_non_null=' + $setNonNull
'set_bool=' + $setBool
'bare_non_null=' + $bareNonNull
'disabled_total=' + $disabledTotal
'dis_test_false=' + $disTestFalse
'dis_test_true=' + $disTestTrue
'dis_test_null=' + $disTestNull
'dis_set_false=' + $disSetFalse
'sample_count=' + $samplesShown

if ($wd.Count -ge 1) {
    $fresh = Get-ScheduledTask -TaskPath $wdPath -TaskName $wdName
    'watchdog_fresh_cimclass=' + [string] $fresh.CimClass.CimClassName
    'watchdog_state=' + [string] $fresh.State
    'watchdog_settings_null=' + ($null -eq $fresh.Settings)
    $fv = $__VAR__.__TAIL__
    'watchdog_test_null=' + ($null -eq $fv)
    'watchdog_test_type=' + $(if ($null -eq $fv) { 'none' } else { $fv.GetType().Name })
    'watchdog_test_value=' + $(if ($null -eq $fv) { 'NULL' } else { [string] $fv })
    'watchdog_bare_null=' + ($null -eq $fresh.Enabled)
}

$pick = @($all | Where-Object {
    ($_.State -eq 'Disabled') -and -not (([string] $_.TaskPath -eq $wdPath) -and ([string] $_.TaskName -eq $wdName))
} | Select-Object -First 1)
'disabled_pool_excluding_watchdog=' + $pick.Count
if ($pick.Count -ge 1) {
    $pf = Get-ScheduledTask -TaskPath $pick[0].TaskPath -TaskName $pick[0].TaskName
    'disfresh_state=' + [string] $pf.State
    'disfresh_name_ascii=' + ((($pf.TaskName -replace '[^\x20-\x7E]', '?') -replace '\s', '_'))
    $pv = $pf.__TAIL__
    'disfresh_test_null=' + ($null -eq $pv)
    'disfresh_test_is_bool=' + ($pv -is [bool])
    'disfresh_test_type=' + $(if ($null -eq $pv) { 'none' } else { $pv.GetType().Name })
    'disfresh_test_value=' + $(if ($null -eq $pv) { 'NULL' } else { [string] $pv })
    'disfresh_bare_null=' + ($null -eq $pf.Enabled)
}
'probe_done=yes'
"""


def _find_powershell():
    for name in ("powershell.exe", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    fixed = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    return str(fixed) if fixed.is_file() else None


_POWERSHELL = _find_powershell()
_CACHE: dict = {}


def _skip_machine() -> None:
    pytest.skip("这台机器上没有 powershell.exe，机器真相探针这三条跑不了（跳过的就是这三条）。"
                "静态那五条——读的位置、接线、fail-closed、via= token、只读性——不受影响、照判。"
                "注意：没有探针时本文件只剩文本判据水平，而那正是挡不住 a5d37bde 那次误读的东西，"
                "所以这道门的放行复跑必须在 Windows 现网机上做。")


def _int(kv: dict, key: str):
    """计数键取值：缺失或非数字一律判红，绝不给个 0 让断言「恰好」通过。"""
    raw = kv.get(key)
    assert raw is not None and re.fullmatch(r"-?\d+", raw), f"探针输出里没有可解析的 {key}（实得 {raw!r}）：{kv}"
    return int(raw)


# ------------------------------------------------------------------ 抽脚本自己的读法
def _enabled_read_site(ps1_text: str):
    """返回 (变量名, 属性路径) —— `Get-WatchdogTaskState` 判决那一行实际读的哪个位置。

    找不到、或找到多于一处，都返回 None：多于一处就是有两份真相，
    本文件的机器对照不知道该验哪一条，宁可不判。
    """
    body = _func_body(ps1_text, "Get-WatchdogTaskState")
    if body is None:
        return None
    hits = [m for m in (_ENABLE_READ_RE.match(ln) for ln in body) if m]
    if len(hits) != 1:
        return None
    return hits[0].group(1).lstrip("$"), hits[0].group(2).lstrip(".")


def _build_probe_text(var: str, tail: str):
    """把脚本抽出来的表达式拼进探针源码，并**证明**拼进去的就是那一句。

    这一步是「探针跑的不是另一条表达式」的接线证明。校验不过就返回 None，
    调用方判红——绝不退回去跑一条更好看的表达式。
    """
    if not _TAIL_RE.match(tail) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
        return None
    text = _PROBE_TEMPLATE.replace("__VAR__", var).replace("__TAIL__", tail)
    if f"$fv = ${var}.{tail}" not in text or f"$vTest = $a.{tail}" not in text:
        return None
    if "__VAR__" in text or "__TAIL__" in text:
        return None
    return text


def _parse_probe_output(stdout: str):
    kv, samples = {}, []
    for line in (ln.strip() for ln in stdout.splitlines()):
        if not line:
            continue
        if line.startswith("SAMPLE "):
            samples.append(line)
            continue
        m = re.fullmatch(r"([a-z][a-z0-9_]*)=(\S*)", line)
        if m:
            kv[m.group(1)] = m.group(2)
    return kv, samples


def _run_probe(var: str, tail: str, work_dir: Path, cache: bool = True):
    """在**仓库外**的 work_dir 里生成并只读跑一次探针，返回 (kv, samples, 探针源码)。"""
    if _POWERSHELL is None:
        _skip_machine()
    key = (var, tail)
    if cache and key in _CACHE:
        return _CACHE[key]
    text = _build_probe_text(var, tail)
    assert text is not None, (
        f"从脚本抽出来的读法没拼进探针（var={var!r} tail={tail!r}）：形状不合法或占位符漏了")
    probe_path = work_dir / ("probe-" + re.sub(r"[^A-Za-z0-9_]", "_", tail) + ".ps1")
    probe_path.write_text(text, encoding="ascii")
    try:
        proc = subprocess.run(
            [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(probe_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        pytest.fail(f"机器真相探针 {PROBE_TIMEOUT_SECONDS}s 没跑完（任务枚举卡住了？先手工只读复跑再动锁）")
    assert proc.returncode == 0, (
        f"探针不是只读跑通的（rc={proc.returncode}）：{proc.stderr.strip()[:400]}\n"
        f"探针源码：{probe_path}")
    kv, samples = _parse_probe_output(proc.stdout)
    assert kv.get("probe_done") == "yes", (
        f"探针没走到最后一行 probe_done=yes，输出半路断了：{proc.stdout[:400]!r}")
    result = (kv, samples, text)
    if cache:
        _CACHE[key] = result
    return result


@pytest.fixture(scope="module")
def probe_for_script_expression(tmp_path_factory):
    """用脚本今天那条表达式跑一次探针，结果整个模块共用（一次 powershell 启动，6 秒级）。"""
    site = _enabled_read_site(PS1)
    assert site is not None, (
        "Get-WatchdogTaskState 里的判决赋值行不见了或不止一处：本文件全部判据的前提是"
        "「脚本自己说它读哪儿」，判法见 _ENABLE_READ_RE")
    work = tmp_path_factory.mktemp("wd-enabled-probe")
    return _run_probe(site[0], site[1], work)


# ------------------------------------------------------------------ 静态判据本体
def _read_site_ok(ps1_text: str) -> bool:
    """判决读的是一个**带层的路径**（`$var.A.B`），不是任务对象上的一个成员名。"""
    site = _enabled_read_site(ps1_text)
    if site is None:
        return False
    tail = site[1]
    return bool(_TAIL_RE.match(tail)) and "." in tail


def _not_the_member_that_is_not_here(ps1_text: str) -> bool:
    """抽出来的属性路径不许是裸 `Enabled`——这台机器上任务对象没有那个成员（实测见模块 docstring）。"""
    site = _enabled_read_site(ps1_text)
    if site is None:
        return False
    return site[1] != _BARE_REF


def _fail_closed_named_ok(ps1_text: str) -> bool:
    """读不到仍然算「还在启用」，而且每一种读不到都登记自己的 via= 取值。"""
    body = _func_body(ps1_text, "Get-WatchdogTaskState")
    if body is None:
        return False
    joined = "\n".join(body)
    if not re.search(r"^\s*\$enabled = \$true\s*$", joined, re.M):
        return False
    if re.search(r"\$enabled\s*=\s*\$false", joined):
        return False
    # 唯一能让 $enabled 变假的，就是那句 [bool] 属性读（别的写法一律算多了一份真相）
    if len([ln for ln in body if _ENABLE_READ_RE.match(ln)]) != 1:
        return False
    for token in ("settings-missing", "settings-enabled-missing", "task-missing", "read-threw"):
        if f"'{token}'" not in joined:
            return False
    if "'settings-enabled'" not in joined:
        return False
    # 每种出口都得真的登记：$enabledSource 至少被赋值 5 次（1 成功 + 4 失败）
    if len([ln for ln in body if re.search(r"^\s*\$enabledSource = '", ln)]) < 5:
        return False
    return True


def _disabled_token_ok(ps1_text: str) -> bool:
    """DISABLED 那一行必须同时带「值」和「是哪个读答的话」，且顺序就是 enabled= 然后 via=。"""
    return re.search(
        r"Write-Output \('DISABLED enabled=' \+ \$verify\.Enabled \+ ' via=' \+ "
        r"\$verify\.EnabledSource \+ ' state=' \+ \$verify\.State \+ ' instances=' \+ "
        r"\$verify\.Instances\)", ps1_text) is not None


_FORBIDDEN_WRITE_CMDETS = (
    "Disable-ScheduledTask", "Enable-ScheduledTask", "Unregister-ScheduledTask",
    "Register-ScheduledTask", "Set-ScheduledTask", "New-ScheduledTask",
    "Start-ScheduledTask", "Stop-ScheduledTask", "Stop-Process", "Remove-Item",
    "Move-Item", "Rename-Item", "New-Item", "Set-Content", "Out-File", "Add-Content",
)


def _no_write_cmdlets(text: str, label: str) -> None:
    """只读性：这段文本里不许出现会改任务/改进程/写文件的 cmdlet。"""
    low = text.lower()
    hits = [name for name in _FORBIDDEN_WRITE_CMDETS if name.lower() in low]
    assert not hits, f"{label} 里出现了会动东西的 cmdlet：{hits}——本文件全程只读，这条必须拦"


# ------------------------------------------------------------------ 静态锁（不需要 PowerShell）
def test_enabled_read_site_is_locatable_and_carries_a_property_path():
    """前提锁：判决那一行必须定位得到，且读的是一个带层的路径。

    定位不到 / 多处命中 / 写成不带 [bool] 的形状，后面所有机器对照都无从谈起。
    """
    site = _enabled_read_site(PS1)
    assert site is not None, (
        "Get-WatchdogTaskState 里那句 `$enabled = [bool] <属性路径>` 不见了或成了多处："
        "机器真相锁的前提是脚本自己说清它读哪儿（判法见 _ENABLE_READ_RE）")
    _var, tail = site
    assert _read_site_ok(PS1), (
        f"抽出来的读法不是一个带层的路径（tail={tail!r}）：裸成员名在这台机器上就是 $null 的来源")


def test_switch_read_is_not_the_member_that_does_not_exist_on_this_box():
    """文本侧护栏（另一半是机器那三条）：不许把判决换成任务对象上的裸 `Enabled`。

    这条不是洁癖，是 a5d37bde 的现场：`Get-ScheduledTask` 给的 MSFT_ScheduledTask 实例顶层成员是
    TaskName/TaskPath/State/Actions/Triggers/Principal/Settings，本机实测 `Get-Member -Name
    Enabled` 计数 0、202 个任务里裸 `.Enabled` 非 $null 的 0 个。读它不报错，只给 $null；
    $null 落在 fail-closed 默认上，判决从此永远不通过。真开关在 `Settings.Enabled`：
    本机 31 个 State=Disabled 的任务全在那里答 False。
    """
    assert _not_the_member_that_is_not_here(PS1), (
        "判决回到了裸 $task.Enabled：那是 a5d37bde 引入的误读（成员不存在 → 恒 $null → "
        "每次换包都 disable-did-not-take）。机器侧的同一件事由本文件那三条探针判定，不是由这条。")


def test_probe_evaluates_the_very_expression_the_script_reads():
    """接线锁：探针跑的就是脚本抽出来的那一句，不是另一条「看起来也对」的表达式。"""
    site = _enabled_read_site(PS1)
    assert site is not None, "先修上一条：定位不到判决读法，这条判不了"
    var, tail = site
    text = _build_probe_text(var, tail)
    assert text is not None, f"表达式拼不进探针源码（var={var!r} tail={tail!r}）"
    assert f"$fv = ${var}.{tail}" in text, "helper 里那句读法没有原样落到探针的按名新读上"
    assert f"$vTest = $a.{tail}" in text, "helper 里那句读法没有原样落到探针的枚举读数上"
    assert "$vBare = $a.Enabled" in text, "阴性对照（历史裸写法）必须固定在探针里，不随脚本变"
    assert f"$vSet = $a.{_SETTINGS_REF}" in text, "参照读数（实测好位置）必须固定在探针里"
    assert text.isascii(), "生成的探针必须纯 ASCII：PS 5.1 会把带中文的无 BOM .ps1 错解码成语法错误"
    _no_write_cmdlets(text, "生成的探针")


def test_helper_stays_fail_closed_and_names_which_read_failed():
    """读不到仍然算「还在启用」，并且每一种读不到都有自己的 via= 取值。

    票面硬要求：不许让「属性缺失」变成绿灯。默认 $true + 不许出现把 $enabled 直接置假的写法 +
    四种失败出口各自登记，缺一个都算这条洞被重新打开。
    """
    assert _fail_closed_named_ok(PS1), (
        "Get-WatchdogTaskState 不再是「读不到 = 当作仍启用 + 登记是哪一种读不到」："
        "settings-missing / settings-enabled-missing / task-missing / read-threw 四种失败出口，"
        "外加 settings-enabled 一种成功出口，必须齐；$enabled 的默认必须是 $true")


def test_disabled_token_carries_the_value_and_the_source_that_answered():
    """DISABLED 那一行必须同时打 enabled= 与 via=（一条里看出是「仍启用」还是「没答话」）。"""
    assert _disabled_token_ok(PS1), (
        "DISABLED token 少了 via=：enabled=True 有两种完全不同的含义（看守真的还在 / 开关根本没答话），"
        "只看 enabled= 分不出来，这正是本次 bug 能藏住的形状")
    assert "EnabledSource" in PS1, "helper 不再往外带 EnabledSource 了：via= 会打成空串"


def test_watchdog_helper_body_issues_no_task_mutation():
    """只读性：判决 helper 本体不碰任何会改任务/进程/文件的 cmdlet。

    它在 15 秒窗口里被反复调用，一次调用多一个副作用，就是 15 次副作用。
    """
    body = _func_body(PS1, "Get-WatchdogTaskState")
    assert body is not None, "helper 不在了"
    _no_write_cmdlets("\n".join(body), "Get-WatchdogTaskState 函数体")


# ------------------------------------------------------------------ 机器三条（需要 PowerShell）
def test_machine_switch_resolves_to_a_real_boolean_through_the_scripts_expression(
        probe_for_script_expression):
    """机器判据·本体：脚本自己那条表达式，在这台机器上真的读得到一个 Boolean。"""
    kv, _samples, _text = probe_for_script_expression
    total = _int(kv, "total")
    assert total > 0, f"任务枚举是空的，探针没问到机器：{kv}"
    assert _int(kv, "watchdog_count") == 1, f"看门狗任务没唯一找到（探针与脚本同步失败）：{kv}"
    assert kv.get("enum_cimclass") == "MSFT_ScheduledTask", f"枚举回来的不是预期的 CIM 类：{kv}"
    assert kv.get("watchdog_fresh_cimclass") == kv.get("enum_cimclass"), (
        f"helper 用的按名新读与枚举回来的是两种对象（{kv.get('watchdog_fresh_cimclass')} vs "
        f"{kv.get('enum_cimclass')}），枚举对账不再成立")
    assert _int(kv, "test_bool") == total, (
        f"按脚本自己的读法（tail={kv.get('tail')}）问 {total} 个任务，只有 {kv.get('test_bool')} 个"
        f"得到 Boolean（非 null {kv.get('test_non_null')} 个）——这条读法拿到的不是开关本体。"
        f"参照：Settings.Enabled 布尔数={kv.get('set_bool')}，裸 Enabled 非 null 数={kv.get('bare_non_null')}")
    assert _int(kv, "set_bool") == total, f"连参照位置 Settings.Enabled 都不全（机器形状变了？）：{kv}"
    assert kv.get("watchdog_test_type") == "Boolean", (
        f"看门狗任务按脚本的读法拿不到 Boolean：null={kv.get('watchdog_test_null')} "
        f"type={kv.get('watchdog_test_type')}——判决会退回 fail-closed 默认，等于永远停不掉")
    assert kv.get("watchdog_test_value") in ("True", "False"), (
        f"判决读到的值不是布尔字面量：{kv.get('watchdog_test_value')!r}")


def test_machine_positive_control_a_genuinely_disabled_task_answers_false(
        probe_for_script_expression):
    """机器判据·阳性对照：真有一个 State=Disabled 的任务，用脚本那条表达式读出 False。

    这一条是本文件不是同义反复的地方：它要的是「读对了」，不是「读到了」。
    样本从机器现存的 State=Disabled 名单里挑（本文件不改任何任务状态），探针按
    TaskPath+TaskName 排除看门狗本身，Python 侧再核一次名字不同。
    """
    kv, samples, _text = probe_for_script_expression
    disabled_total = _int(kv, "disabled_total")
    assert disabled_total >= 1, f"这台机器上一个 State=Disabled 的任务都没有，阳性对照无从建立：{kv}"
    assert _int(kv, "dis_test_false") >= 1, (
        f"按脚本的读法（tail={kv.get('tail')}）问 {disabled_total} 个真停用任务，没有一个答 False"
        f"（null={kv.get('dis_test_null')} true={kv.get('dis_test_true')}）——那条表达式读到的"
        f"不是开关；裸 $task.Enabled 在这台机器上正是这个形状（0/202 答话）")
    assert kv.get("disfresh_state") == "Disabled", f"挑来做按名新读对照的任务不是真停用：{kv}"
    assert kv.get("disfresh_test_is_bool") == "True", (
        f"用 helper 原句（Get-ScheduledTask -TaskPath -TaskName）+ 脚本表达式读不到 Boolean："
        f"type={kv.get('disfresh_test_type')} value={kv.get('disfresh_test_value')}")
    assert kv.get("disfresh_test_value") == "False", (
        f"真停用任务按脚本表达式读出来不是 False：{kv.get('disfresh_test_value')!r}")
    assert kv.get("disfresh_name_ascii") != kv.get("watchdog_name_ascii"), (
        "阳性对照用到了看门狗任务本身——那会掩盖问题，本票也明令不许拿它当样本")
    assert _int(kv, "sample_count") >= 1 and samples, f"没有一条 SAMPLE 证据可看：{kv.get('sample_count')}"


def test_machine_negative_control_the_bare_member_is_not_a_member_here(
        probe_for_script_expression):
    """机器判据·阴性对照：历史坏写法（裸 $task.Enabled）在这台机器上一个都不答话。

    这条**不读脚本内容**，讲的是机器事实，所以它不会因为脚本改好而变绿或变红；它的作用是：
    有人把判决改回裸写法时，红的是上面两条，而这条把「为什么」当场说给他听。
    """
    kv, _samples, _text = probe_for_script_expression
    total = _int(kv, "total")
    assert total > 0, f"枚举没跑成：{kv}"
    assert _int(kv, "bare_non_null") == 0, (
        f"裸 .Enabled 竟然在 {kv.get('bare_non_null')}/{total} 个任务上答话了——这台机器的形状变了，"
        f"那么 a5d37bde 那次「改读 Enabled」就不算纯误读，本文件的理由要重写成带日期的新实测")
    assert kv.get("watchdog_bare_null") == "True", f"看门狗任务的裸 .Enabled 竟然答话了：{kv}"
    assert _int(kv, "dis_set_false") >= 1, (
        f"参照位置 Settings.Enabled 在真停用任务上一个 False 都没有：{kv}——"
        f"连开关都不答话的话先修机器，别改判据")


# ------------------------------------------------------------------ 牙印
_BARE_READ_LINE = "                $enabled = [bool] $fresh.Settings.Enabled"
_BARE_READ_MUTATED = "                $enabled = [bool] $fresh.Enabled"


def test_mutation_bare_enabled_read_turns_the_machine_truth_lock_red(tmp_path):
    """牙印：把判决读法改回 a5d37bde 的裸写法（**仓库外副本**），静态与机器两侧都必须红。

    机器侧的红是本票的意义所在：探针拿的是**副本自己**抽出来的表达式，于是那 31 个真停用任务
    一个都不答 False，阳性对照塌——这不是「文本没匹配上」，是这台机器判它不通过。
    """
    assert _read_site_ok(PS1) and _not_the_member_that_is_not_here(PS1), \
        "真文件两条静态都必须绿，否则变异证明不了任何事"
    assert _BARE_READ_LINE in PS1, (
        "变异样本构造失败：判决那一行不再是预期的形状，请把 _BARE_READ_LINE 跟上真实代码。\n"
        f"当前形状：{[ln for ln in (_func_body(PS1, 'Get-WatchdogTaskState') or []) if '$enabled =' in ln]}")
    drifted = PS1.replace(_BARE_READ_LINE, _BARE_READ_MUTATED, 1)
    assert drifted != PS1, "漂移样本没生效：replace 一个字节都没改"

    copy = tmp_path / "swap-package-bare-enabled-read.ps1"
    copy.write_text(drifted, encoding="utf-8")
    assert copy.read_text(encoding="utf-8") == drifted, "副本没落盘：牙齿咬的是内存而不是文件"
    assert SWAP_PS1.read_text(encoding="utf-8") == PS1, "仓库里那份被改了：本文件全程只读，绝不该写它"

    assert not _read_site_ok(drifted), "裸写法还能过静态判据：_read_site_ok 是在看字符串而不是看层"
    assert not _not_the_member_that_is_not_here(drifted), (
        "裸写法没被抓住，a5d37bde 那次误读随时能回来")

    site = _enabled_read_site(drifted)
    assert site is not None, "副本里连判决赋值行都定位不到：样本形状与 _ENABLE_READ_RE 假设不符"
    assert site[1] == _BARE_REF, f"副本抽出来的读法应当是裸 Enabled，实得 {site[1]!r}"
    kv, samples, text = _run_probe(site[0], site[1], tmp_path)
    assert f"$fv = ${site[0]}.{_BARE_REF}" in text, "探针跑的表达式与副本抽出来的不一致"
    assert _int(kv, "test_bool") == 0, (
        f"用裸写法问这台机器，竟然还有 {kv.get('test_bool')} 个任务答了 Boolean——"
        f"机器真相锁在这里没有牙齿，先确认探针跑的确实是 tail={kv.get('tail')}")
    assert _int(kv, "dis_test_false") == 0, (
        f"裸写法在 {kv.get('disabled_total')} 个真停用任务上拿到了 {kv.get('dis_test_false')} 个 False："
        f"阳性塌了这条锁才算立住，现在它没塌")
    assert kv.get("watchdog_test_type") != "Boolean", f"裸写法读看门狗竟然得到了 Boolean：{kv}"
    assert kv.get("disfresh_test_value") == "NULL", f"按名新读用裸写法却没得到 $null：{kv}"
    assert samples == [], f"裸写法还能造出 SAMPLE 证据：{samples[:2]}"
