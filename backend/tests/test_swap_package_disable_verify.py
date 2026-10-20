r"""换包脚本「停用看门狗」那一步的三条契约（v0.34.2 收口第二批，2026-10-16 现网事故）。

事故一（判据读错了属性）：`deploy/swap-package.ps1` disable 之后拿 `(Get-ScheduledTask).State`
当判决，不等于 `'Disabled'` 就 throw。可是**任务正有一次实例在跑时，即使停用成功，State 照样
报 Running**——停用只拦今后的触发器，不停已经在跑的那一次。而崩溃守护每隔一分钟就醒一次
（计划任务的 RepetitionInterval=1min；那个注册脚本是本机专属、刻意被 .gitignore 排除，
不是仓库文件），deploy/watchdog-hidden.vbs:35
又是 `waitOnReturn=True`（wscript 等 watchdog.ps1 跑完才退出，实例的存活时间就是这一次检查的
时长），于是"换包正好撞在醒来之后那一秒"根本不是小概率。现网真就这么红的：
`DISABLED state=Running` → `throw 'watchdog did not actually disable ...'` → PSEXIT=1，
包一个字节没动（fail-safe 是对的，判据是错的），几分钟后没有实例在跑时重试就
`DISABLED state=Disabled` 一路走完。这两次对照就是复现证据。

事故二（那条 throw 是裸的，比事故一更贵）：它没走脚本自己的失败出口 `Stop-AfterFailure`
（那里会在旧包还没被改名时重启完好包并重新启用任务），所以中止**把看门狗留在了停用态**——
这台机器当晚从此没有崩溃守护，直到人手动 `Get-ScheduledTask | Where-Object {...vbs...}`
找到它、`Enable-ScheduledTask` 才回到 Ready。也就是说同一次中止里有**两个**缺陷，
第二个还是静默的：任何后来在那一行中止的换包都会再丢一次守护。

本文件钉五件事（前三条是 2026-10-16 事故的原判据，#155 补 (iv)/(v)）：
(i)  disable 之后的判决读的是任务的 `Settings.Enabled`（真问题：这段窗口里它还能不能把 exe 拉起来；
     2026-10-18 又红过一次：这一句原本写「读 Enabled」，而本机任务对象上**没有** Enabled 成员，
     真值在 Settings 那一层——见 tests/test_swap_package_watchdog_enabled_machine_truth.py），
     正在跑的实例单独打 token 供人看，只在**有界的**等待到点后按自己的 stage 中止；
(ii) 从 disable 那一句到「旧包让位」（`$script:backupDone = $true`）之间**不存在任何绕过
     恢复机制的出口**：裸 `throw` / `exit` 一律不许；那几句会抛异常的 cmdlet 必须落在
     「catch 里调用 Stop-AfterFailure」的 try 块内（用花括号配平真算出 try 的范围，
     不是拿正则碰运气）；
(iii) 恢复路径真的重新启用任务，而且那次启用**不嵌**在「要不要重启旧包」的分支里
     （嵌进去就等于"进程还没停的那条中止路上看守不回来"——恰好是事故二那条路）。
(iv) 恢复路径的 `re-enabled` 只在**启用后的回读**证明开关真上来之后才准出现（ticket #155）：
     `Enable-ScheduledTask` 不抛不等于开关动了——它可以正常返回而任务仍停用，旧恢复路径
     就是拿"没抛"当成功打了绿灯。所以启用之后必须经 `Get-WatchdogTaskState` 再读一次
     `Settings.Enabled`；回读答不出、via 不是 `settings-enabled`、或 Enabled=false 一律
     `re-enable-FAILED`，并把回读的原始字段写进错误详情。`_restore_readback_ok` 判这条。
(v)  看守**从没被按下去**时不许报 `re-enabled`（同一张票的另一半）：`disable-did-not-take`
     中止会带着"这次运行拥有停用"的登记进入恢复函数，可开关明明还开着。恢复函数在启用前
     先读一次；开关亲口答 Enabled=true（via=settings-enabled）时报 `already-enabled`，
     不碰任务。`_restore_already_enabled_ok` 判这条。

判位置用行号、判可执行部分先剥整行注释，尺子从 test_swap_package_contract_v0291.py 借
（本仓规矩：一把尺子只有一份）。`_code_lines()` 是它的一个变体：只剥「整行 #」，
**行数与原文一一对应**，因为 `_strip_comments` 会把 `<# ... #>` 整段连同其中换行一起删掉，
行号随之漂移，判顺序与判块都不能用它。

为什么没有行为层：这一步只有真把生产任务停用下来才跑得进去，而本次票面明令禁止
（不许对现网任务动 Disable/Enable/Unregister/Register，也不许跑 swap-package.ps1，带
-DryRun 也不行——DryRun 在 disable 之前就 return 了，证明不了任何事）。所以这些判据**全是
静态锁，下一次换包就是它们的第一次实跑**：盯 `DISABLED enabled=... via=... state=... instances=`、
`WAIT`、`READFAIL`、`ABORT stage=... moved=... watchdog=...` 四行 token。ticket #155 另加了
一层**可跑的行为排练**（`deploy/rehearse-watchdog-restore.ps1`）：用脚本自己创建的一次性任务
`\AIAssistantSwapRehearsal-*` 真跑 disable → `Stop-AfterFailure` → `Restore-WatchdogTask`，
并造一个"启用调用静默无效"的回读失败样本；排练全程不碰现网 `AI助手-崩溃守护`，结束时注销
一次性任务并核对真任务的 Enabled 未变。pytest 里不放这条行为测试（测试进程不该创建计划
任务），行为证据由那次排练的输出承担。牙印在本文件末尾：旧判据回来、裸 throw 回来、恢复
被删、恢复被嵌进 if、被赋值的 helper 里漏一行日志、塞进一个非 ASCII 字节，再加上 #155
的两条（摘掉启用后回读、把从没停过的看守说成 re-enabled）——每一种都必须红。

静态锁的射程要说在前面（这是 2026-10-18 那次红的直接教训）：本文件判的是「代码里写了什么」，
判不了「那样写在这台机器上读不读得到」。`a5d37bde` 把判决从 `State` 换成裸 `$fresh.Enabled`，
上面 13 条**一条都没红**——文本上它确实"读 Enabled、有界等待、走恢复机制"，全对；
可本机 `Get-ScheduledTask` 回来的对象没有 Enabled 成员，读它安静地给 $null，fail-closed
默认从此永久，通过条件永不成立。补这一层射程的是同目录的
`test_swap_package_watchdog_enabled_machine_truth.py`（只读问机器 + 真停用任务阳性对照 + 
裸写法牙印）。以后动这一段的判决，两个文件要一起绿。
"""
import os
import re
from pathlib import Path

# 剥「整行注释」的尺子与行号尺子都从 v0.29.1 那份契约借（本仓规矩：一把尺子只有一份）
from tests.test_swap_package_contract_v0291 import _line_of

REPO_ROOT = Path(__file__).resolve().parents[2]


def _target_ps1() -> Path:
    """本文件读的那份 .ps1。默认是仓库里的真文件。

    `SWAP_PS1_UNDER_TEST` 只为一件事存在：把「塞一个非 ASCII 字节，这道门必须红」那次
    取证跑在**仓库外的副本**上。本仓库目录同时是生产机，往 `deploy/swap-package.ps1`
    里真塞进中文字节，等于给任何一次落在几秒窗口里的换包埋一颗 PS 5.1 解码雷，
    而这条判据根本不需要拿生产文件去冒险（票面也是这么要求的）。
    设了环境变量就必须指着一个真实存在的文件——绝不静默退回默认路径，否则那条"绿"
    可能来自一个根本没读到的位置。
    """
    override = os.environ.get("SWAP_PS1_UNDER_TEST")
    if not override:
        return REPO_ROOT / "deploy" / "swap-package.ps1"
    path = Path(override)
    if not path.is_file():
        raise AssertionError(f"SWAP_PS1_UNDER_TEST 不是一个真实文件：{override}")
    return path


SWAP_PS1 = _target_ps1()

PS1 = SWAP_PS1.read_text(encoding="utf-8")

# ticket #155 的行为排练脚本（只动一次性任务；本文件只判它的形状与安全门）
REHEARSAL_PS1 = REPO_ROOT / "deploy" / "rehearse-watchdog-restore.ps1"


# ------------------------------------------------------------------ 读法（行号保真）
def _code_lines(text: str) -> list:
    """把「整行是 # 注释」的那几行替换成空串，**行数与原文一一对应**。

    为什么不直接用借来的 _strip_comments：它连 `<# ... #>` 整段删掉，段里的换行也一起没了，
    行号就漂了——本文件判的是"哪一句在哪一句之前/之内"，漂一行全盘皆错。
    反过来，判"可执行部分里绝不许出现某写法"仍然该用 CODE（注释里合法地写着旧判据的
    病根说明，那是文档职责）。
    """
    return re.sub(r"(?m)^[ \t]*#.*$", "", text).splitlines()


def _idx(pattern: str, lines: list, lo: int = 0, hi: int = None, last: bool = False):
    """在 lines[lo:hi] 里找第一个（或最后一个）命中行；找不到返回 None。

    这里刻意不学 `_line_of` 那样"找不到就 assert 炸掉"：判据本体必须是**能返回 False
    的谓词**，这样同一条判据既能对真文件跑绿、也能对漂移样本跑红。
    """
    end = len(lines) if hi is None else hi
    hits = [i for i in range(lo, min(end + 1, len(lines))) if re.search(pattern, lines[i])]
    if not hits:
        return None
    return hits[-1] if last else hits[0]


DISABLE_RE = r"^\s*Disable-ScheduledTask -TaskPath"
BACKUP_FLAG_RE = r"^\s*\$script:backupDone = \$true"
RESTORE_CALL_RE = r"^\s*\$restore = Restore-WatchdogTask\b"
SAFETY_RE = r"^\s*Stop-AfterFailure\s+'"
# 启用后的回读：`$<var> = Get-WatchdogTaskState -TaskToRead $task`（#155）
READBACK_RE = re.compile(
    r"^\s*\$(?P<var>[A-Za-z_][A-Za-z0-9_]*) = Get-WatchdogTaskState -TaskToRead \$task\s*$")


def _window(text: str):
    r"""返回 (code_lines, disable 行, backupDone 行)；找不到或找到多于一处都算不合格。

    disable 那一句必须**恰好一处**：marker gate 与 all-or-nothing 两份锁也按命令文本
    判序，三处都允许行首缩进（`^\s*`），只认那句命令本身。
    """
    lines = _code_lines(text)
    if len(lines) != len(text.splitlines()):
        return None, None, None
    starts = [i for i, ln in enumerate(lines) if re.search(DISABLE_RE, ln)]
    ends = [i for i, ln in enumerate(lines) if re.search(BACKUP_FLAG_RE, ln)]
    if len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
        return None, None, None
    return lines, starts[0], ends[0]


def _try_spans(lines: list, lo: int, hi: int) -> list:
    """lo..hi 里每个 `try {` 的行范围 (start, end)（含 catch 块，到它与 try 配平的那行为止）。

    数花括号净增减，不是猜缩进：`foreach (...) {` / `} else {` / `@{ ... }` / `@(... { })`
    这些形状在窗口里都出现，任何"看第几列"的写法都会把它们判错。注释已在 _code_lines 里
    清空，所以注释里的括号不会掺进来。
    """
    spans = []
    i = lo
    while i <= hi:
        if re.search(r"\btry\s*\{", lines[i]):
            depth = 0
            j = i
            while j <= hi:
                depth += lines[j].count("{") - lines[j].count("}")
                if depth <= 0 and j > i:
                    break
                j += 1
            spans.append((i, min(j, hi)))
            i = j + 1
        else:
            i += 1
    return spans


def _span_of(spans: list, idx: int):
    for s, e in spans:
        if s <= idx <= e:
            return (s, e)
    return None


def _catch_routes_to_restore(lines: list, span) -> bool:
    """那个 try 的范围里必须真有 `} catch {`，而且 catch 走的是恢复机制那一句话。"""
    s, e = span
    body = lines[s:e + 1]
    if not any(re.search(r"\}\s*catch\s*\{", ln) for ln in body):
        return False
    return any(re.match(SAFETY_RE, ln) for ln in body)


def _func_body(text: str, name: str):
    """函数体的行（从 `function NAME {` 到它与它配平的那一行），注释已清空。"""
    lines = _code_lines(text)
    start = _idx(r"^function\s+" + name + r"\s*\{", lines)
    if start is None:
        return None
    depth = 0
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if depth <= 0 and i > start:
            return lines[start:i + 1]
    return None


def _indent(lines: list, i: int) -> int:
    return len(lines[i]) - len(lines[i].lstrip())


# ------------------------------------------------------------------ 判据本体
def _verdict_ok(text: str) -> bool:
    """(i) 停用之后的判决读 Enabled；实例只报告 + 有界等待；不许杀任何东西。"""
    lines, lo, hi = _window(text)
    if lines is None:
        return False
    win = lines[lo:hi + 1]
    body = "\n".join(win)

    # 通过条件必须是"确实停用，且此刻没有实例在跑"——两条都在 token 里
    if not re.search(r"if \(-not \$verify\.Enabled -and \$verify\.Instances -eq 0\) \{ break \}", body):
        return False
    if not re.search(r"'DISABLED enabled=' \+ \$verify\.Enabled", body):
        return False
    if not re.search(r"' state=' \+ \$verify\.State \+ ' instances=' \+ \$verify\.Instances", body):
        return False
    # State 从此不再是判决：窗口里任何一处把某个值和 'Disabled' 比都算旧判据回来
    if re.search(r"(-ne|-eq)\s+'Disabled'", body):
        return False
    if re.search(r"\$afterState", body):
        return False

    # 等待必须有界、固定秒数、到点按各自 stage 中止，两种原因必须分得开
    m = re.search(r"\$DisableVerifyWindowSeconds = (\d+)", body)
    if not m or not (1 <= int(m.group(1)) <= 60):
        return False
    if not re.search(r"\$verifyDeadline = \(Get-Date\)\.AddSeconds\(\$DisableVerifyWindowSeconds\)", body):
        return False
    if not re.search(r"if \(\(Get-Date\) -ge \$verifyDeadline\)", body):
        return False
    if "while ($true)" not in body:
        return False
    if not re.search(r"Stop-AfterFailure 'disable-did-not-take'", body):
        return False
    if not re.search(r"Stop-AfterFailure 'disable-instance-running'", body):
        return False
    if not re.search(r"Enabled is still true", body):
        return False
    if not re.search(r"instance is still underway", body):
        return False

    # disable 成功之后才登记"这台看守是我按下去的"
    i_flag = _idx(r"^\s*\$script:watchdogDisabled = \$true", lines, lo, hi)
    if i_flag is None or i_flag <= lo:
        return False

    # 验环本体（disable -> 第一次取进程列表）绝不许动刀杀实例
    stop_at = _idx(r"\$procs = Get-LiveProcess", lines, lo, hi)
    if stop_at is None:
        return False
    loop = "\n".join(lines[lo:stop_at])
    if re.search(r"Stop-(Process|ScheduledTask)|Unregister-ScheduledTask", loop):
        return False
    return True


def _no_bare_exit_ok(text: str) -> bool:
    """(ii) disable 到 backupDone 之间没有任何绕过恢复机制的出口。

    三条都算出口：裸 `throw`、`exit`、以及会抛异常却没被 catch 住的 cmdlet 调用。
    前两条用行首匹配判（`throw` / `exit` 出现在这一行的第一个词），第三条用
    try 范围包含关系判（_try_spans 真数花括号）。

    `Disable-ScheduledTask` **不在这条判据的 `risky` 名单里，但它如今确实被包在 try 里**
    （2026-10-17 ticket #32 改判）。这一处原本写的是"它自己抛异常的时候看守根本没被按下去，
    没有需要恢复的东西"——那是一句猜测而不是读数：调用抛了并不等于开关没翻（CIM 可以先翻
    开关、再在输出管道上失败），而猜错的那一侧是把崩溃守护留在地上。现在这一段由
    `test_swap_package_disable_threw_handler.py` 钉：try 包住那一句、catch 走
    `Stop-AfterFailure` 的恢复机制、handler 只用只读 helper 问"看守此刻在不在"、
    三种答案各自归属。本函数不把它加进 `risky` 名单也是有原因的，别顺手合并：
    那会让"半修样本只红判决锁、出口锁必须保持绿"那条独立性牙印变红（半修样本里包住
    disable 的 `} catch {` 正落在被整段替换的范围里），两条锁各判各的范围。
    """
    lines, lo, hi = _window(text)
    if lines is None:
        return False
    win = lines[lo:hi + 1]
    if any(re.match(r"^\s*(throw|exit)\b", ln) for ln in win):
        return False
    aborts = [ln for ln in win if re.match(SAFETY_RE, ln)]
    if not aborts:
        return False

    spans = _try_spans(lines, lo, hi)
    risky = [r"\$procs = Get-LiveProcess", r"Stop-Process -Id \$p\.ProcessId -Force", r"netstat -ano"]
    for pattern in risky:
        idx = _idx(pattern, lines, lo, hi)
        if idx is None:
            return False
        span = _span_of(spans, idx)
        if span is None or not _catch_routes_to_restore(lines, span):
            return False
    return True


def _restore_ok(text: str) -> bool:
    """(iii) 恢复路径真的重新启用任务，并把结果打进一条 ABORT token 里。"""
    stop = _func_body(text, "Stop-AfterFailure")
    restore = _func_body(text, "Restore-WatchdogTask")
    if stop is None or restore is None:
        return False
    stop_body = "\n".join(stop)
    restore_body = "\n".join(restore)

    # 中止那一行必须把"看守回来了没有"写在同一个 token 里（moved=no watchdog=...）
    if not re.search(r"ABORT stage=' \+ \$FailedAt \+ ' moved=no watchdog=' \+ \$restore\.Status", stop_body):
        return False
    # moved=yes（旧包已经让位）不得自动撤销改名，也必须明说看守是**有意**留在停用态
    if not re.search(r"ABORT stage=' \+ \$FailedAt \+ ' moved=yes watchdog=left-disabled-by-design", stop_body):
        return False
    if not re.search(r"if \(\$script:backupDone\)", stop_body):
        return False

    # 恢复本体：按 action 找到的那个任务、按 TaskPath/TaskName 启用回来
    if not re.search(r"Enable-ScheduledTask -TaskPath \$task\.TaskPath -TaskName \$task\.TaskName \| Out-Null",
                     restore_body):
        return False
    if not re.search(r"\$script:watchdogDisabled = \$false", restore_body):
        return False
    # 启用必须由"这次运行真把看守按下去了"那个登记来放行：
    # 正向门（if ($script:watchdogDisabled)）写在前面，取反的写法一律算不合格——
    # 取反之后"从没停用过的中止"会声称自己重新启用了任务，而 disable 失败的那条路
    # 会不去启用，两处都是假话。
    if not re.search(r"if \(\$script:watchdogDisabled\) \{", restore_body):
        return False
    if re.search(r"if \(-not \$script:watchdogDisabled\)", restore_body):
        return False
    # 启用自己也要包在 try 里：它抛异常时更要紧的是别把"中止"变成"没中止"
    if not re.search(r"\btry\s*\{", restore_body) or not re.search(r"\}\s*catch\s*\{", restore_body):
        return False
    if "re-enabled" not in restore_body or "re-enable-FAILED" not in restore_body:
        return False
    return True


def _restart_if(lines: list):
    """「旧包要不要重新拉起来」那条 if 的下标；锚在重启动作上，不认条件写法（#51）。

    先框住 `function Stop-AfterFailure` 的体，再找体内恰好那一句
    `Start-Process -FilePath $LiveExe`。然后在函数体里从前往后扫每一条带块的
    `if (`，按花括号配平取**最内层**、块范围包含重启动作的那些 if 的最后一条。
    条件的拼法（`(Get-LiveProcess).Count` / `@(Get-LiveProcess).Count` / 等价改写）
    完全不参与定位。找不到函数、找不到重启动作、或它不在任何 if 块里，一律 None：
    调用方按"判据前提不成立"判红，绝不退回一条字面匹配。
    """
    start = _idx(r"^function\s+Stop-AfterFailure\s*\{", lines)
    if start is None:
        return None
    depth = 0
    end = None
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if depth <= 0 and i > start:
            end = i
            break
    if end is None:
        return None
    i_start = _idx(r"^\s*Start-Process -FilePath \$LiveExe\b", lines, start, end)
    if i_start is None:
        return None
    owner = None
    for i in range(start, i_start):
        if not re.match(r"^\s*if\s*\(", lines[i]) or "{" not in lines[i]:
            continue
        depth = 0
        j = i
        while j <= end:
            depth += lines[j].count("{") - lines[j].count("}")
            if depth <= 0 and j > i:
                break
            j += 1
        if j > i and i < i_start <= j:
            owner = i
    return owner


def _restore_not_nested(text: str) -> bool:
    """(iii) 的另一半，也是这套锁最容易糊过去的地方：恢复代码**在**、却跑不到。

    事故二就是这一族的形状：恢复机制整个存在，只是那句 throw 不在它射程里。
    这里判的是"重新启用不嵌在重启旧包那条 if 里"：
      - `$restore = Restore-WatchdogTask` 的缩进必须不深于那条 if 的开行；
      - 而且它不能落在包住那次重启的 try/catch 范围内。
    那条 if 由 `_restart_if` **结构定位**（#51）：锚在它体内的 `Start-Process
    -FilePath $LiveExe` 上，不认条件的字面拼法，所以把条件改成 `@(...)` 或调换
    两个判据的顺序都不会让这条锁以"锚点找不到"的方式误红。
    用缩进层级而不是花括号，是因为 Stop-AfterFailure 里那两句是兄弟语句，一旦被嵌进 if
    体内缩进必然变深——而"嵌进 if"恰好就是那条会静默丢守护的漂移。
    """
    lines, lo, hi = _window(text)
    if lines is None:
        return False
    i_restart = _restart_if(lines)
    i_call = _idx(RESTORE_CALL_RE, lines)
    if i_restart is None or i_call is None:
        return False
    if _indent(lines, i_call) > _indent(lines, i_restart):
        return False
    # 同一条恢复里，启用不能排在"读不到进程列表就整体放弃"后面
    spans = _try_spans(lines, 0, len(lines) - 1)
    span = _span_of(spans, i_restart)
    if span is not None and span[0] <= i_call <= span[1]:
        return False
    return True


def _restore_readback_ok(text: str) -> bool:
    """(iv) `re-enabled` 只许由**启用后的回读**放行；回读不成立一律 FAILED。

    判的是顺序与内容，不是"代码里出现过 Get-WatchdogTaskState"：
      1. 启用那一句之后恰好一次 `$<var> = Get-WatchdogTaskState -TaskToRead $task`；
      2. 回读之后有一条 `if (` 同时看 `.ReadError` / `.EnabledSource` / `-not .Enabled`；
      3. 失败分支先写 `$status = 're-enable-FAILED'`，并让 `$failDetail` 带上回读字段；
      4. `$status = 're-enabled'` 排在失败分支之后，且回读（含失败分支）之前不许
         把 `$script:watchdogDisabled` 放下——开关还没证明回来就宣布完成，正是旧形状。
    旧锁 `_restore_ok` 只判"有没有 Enable 那一句"，对"启用后有没有回读"无感；牙印会
    证明摘掉回读后旧锁照旧绿、本条红。
    """
    restore = _func_body(text, "Restore-WatchdogTask")
    if restore is None:
        return False
    lines = restore
    i_enable = _idx(
        r"^\s*Enable-ScheduledTask -TaskPath \$task\.TaskPath -TaskName \$task\.TaskName \| Out-Null\s*$",
        lines)
    if i_enable is None:
        return False
    reads = [(i, m.group("var")) for i, m in
             ((i, READBACK_RE.match(lines[i])) for i in range(i_enable + 1, len(lines))) if m]
    if len(reads) != 1:
        return False
    i_read, var = reads[0]
    i_fail = _idx(r"^\s*\$status = 're-enable-FAILED'\s*$", lines, i_read + 1)
    if i_fail is None:
        return False
    i_ok = _idx(r"^\s*\$status = 're-enabled'\s*$", lines, i_fail + 1)
    if i_ok is None:
        return False

    cond = "\n".join(lines[i_read + 1:i_fail])
    if not re.search(r"(?m)^\s*if \(", cond):
        return False
    for piece in ("$" + var + ".ReadError", "$" + var + ".EnabledSource",
                  "-not $" + var + ".Enabled"):
        if piece not in cond:
            return False

    detail = "\n".join(lines[i_fail:i_ok])
    if "$failDetail = " not in detail:
        return False
    for piece in ("$" + var + ".Enabled", "$" + var + ".EnabledSource"):
        if piece not in detail:
            return False

    # 回读之前（含失败分支）把归属放开 = 还没证明开关回来就宣布完成
    if any(re.match(r"^\s*\$script:watchdogDisabled = \$false\s*$", ln)
           for ln in lines[i_enable:i_ok]):
        return False
    if _idx(r"^\s*\$script:watchdogDisabled = \$false\s*$", lines, i_ok + 1) is None:
        return False
    return True


def _restore_already_enabled_ok(text: str) -> bool:
    """(v) 开关亲口答"看守从没被按下去"时，恢复路径不许碰任务、也不许报 re-enabled。

    `disable-did-not-take` 中止到达恢复函数时登记还挂着，但开关明明 Enabled=true：
    再调一次 Enable 是空动作，随后按"没抛 = 成功"打 `re-enabled` 就是在说一次从未发生的
    重新启用。恢复函数必须先读一次，且只在 `EnabledSource -eq 'settings-enabled'` 时
    认这个答案（fail-closed 的 Enabled=true 不算回答，仍要走启用+回读）。
    """
    restore = _func_body(text, "Restore-WatchdogTask")
    if restore is None:
        return False
    lines = restore
    i_flag = _idx(r"^\s*if \(\$script:watchdogDisabled\) \{\s*$", lines)
    if i_flag is None:
        return False
    i_enable = _idx(
        r"^\s*Enable-ScheduledTask -TaskPath \$task\.TaskPath -TaskName \$task\.TaskName \| Out-Null\s*$",
        lines, i_flag)
    if i_enable is None:
        return False
    reads = [(i, m.group("var")) for i, m in
             ((i, READBACK_RE.match(lines[i])) for i in range(i_flag + 1, i_enable)) if m]
    if len(reads) != 1:
        return False
    i_before, var = reads[0]
    pre = "\n".join(lines[i_before + 1:i_enable])
    if not re.search(r"(?m)^\s*if \(" + re.escape("$" + var + ".Enabled") + r" -and ", pre):
        return False
    if "$" + var + ".EnabledSource -eq 'settings-enabled'" not in pre:
        return False
    if _idx(r"^\s*\$status = 'already-enabled'\s*$", lines, i_before, i_enable) is None:
        return False
    # 从没停过的那条恢复同样要把归属放开，否则下一条恢复路径会再“启用”一次
    if _idx(r"^\s*\$script:watchdogDisabled = \$false\s*$", lines, i_before, i_enable) is None:
        return False
    # 预读结论之前已经写过后面的成功 token = 又回到“先打绿灯再看机器”
    if _idx(r"^\s*\$status = 're-enabled'\s*$", lines, i_flag, i_enable) is not None:
        return False
    return True


def _single_value_helpers_ok(text: str) -> bool:
    """被赋值捕获的两个 helper 必须**只**产出那一个对象。

    `$verify = Get-WatchdogTaskState ...` 接的是整条输出流：helper 里多写一行
    Write-Output，$verify 就成了数组，后面每次属性读取都是在猜——而 disable-verify
    的判决正靠这两个值。恢复路径同理。所以这两个函数体里不许出现 Write-Output，
    要报的信息一律当字段带出去，由调用方打 token。
    """
    for name in ("Get-WatchdogTaskState", "Restore-WatchdogTask"):
        body = _func_body(text, name)
        if body is None or any("Write-Output" in ln for ln in body):
            return False
    read_body = "\n".join(_func_body(text, "Get-WatchdogTaskState") or [])
    # 读不到就**当作还在启用**（fail closed）：默认值必须是 $true，且不许有
    # "读失败就把 Enabled 记成 false" 的写法——那会把这条洞重新打开，还伪装成绿行。
    if not re.search(r"\$enabled = \$true", read_body):
        return False
    if re.search(r"\$enabled\s*=\s*\$false", read_body):
        return False
    if not re.search(r"return \[pscustomobject\]@\{ Enabled = \$enabled", read_body):
        return False
    return True


def _ascii_ok(raw: bytes) -> bool:
    """整个 .ps1 一个非 ASCII 字节都不许有，也不许有 BOM（票面要求的门）。

    PowerShell 5.1 把 UTF-8 无 BOM 的中文 .ps1 错解码成语法错误，脚本死在半路，
    生产继续静默跑旧包九天（本仓 test_swap_package_contract_v0291.py 第一条锁记的那次）。
    这一类门没人加中文就永远不红，永远不红就没人知道它还在不在，所以本文件把它
    **和上面三条判据放在一起**重跑一遍，并配一条真的往里塞非 ASCII 字节的牙印。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return False
    return not any(b >= 0x80 for b in raw)


# ------------------------------------------------------------------ 契约断言（真文件）
def test_pre_move_window_is_located_at_all():
    """前提锁：窗口两端必须各自唯一，否则后面所有判据都是在判一个空集合。

    "判据找不到锚点"与"判据判过了"必须是两种红法，所以这里先单独把它们钉死。
    """
    lines, lo, hi = _window(PS1)
    assert lines is not None, "disable 那一句或 backupDone 那一句已经不唯一了（见 _window 的判法）"
    assert len(lines) == len(PS1.splitlines())
    assert 0 < lo < hi < len(lines)
    # 顺序本身仍归 v0.29.1 那份契约管，这里只补一条：预检与 -DryRun 出口都在 disable 之前
    assert _line_of(r"PRECHECK ok", PS1) < lo, "预检必须排在停用看门狗之前"
    assert _line_of(r"^if \(\$DryRun\)", PS1) < lo, "-DryRun 的出口必须排在停用之前（它不该按看守）"


def test_post_disable_verdict_reads_enabled_and_reports_instance_as_its_own_token():
    assert _verdict_ok(PS1), (
        "disable 之后的判决不再读 Enabled，或正在跑的实例又被当成了判决本体"
        "（2026-10-16 那次红就是因为要求 State 字面等于 Disabled——实例在跑时它恒报 Running），"
        "或等待失去了上界/两种中止原因不再分得开")
    # 停用一个正在跑的实例时，Task Scheduler 报的是 Running 而不是 Disabled：
    # 这条常识就是判据本体，写在这里免得下一个人把它当"可以恢复成 State 判决"的旧写法。
    assert "State=Running" in PS1, "为什么不能读 State 的那段说明被删了：那是本次事故的全部理由"


def test_no_bare_exit_between_disable_and_the_restore_machinery():
    assert _no_bare_exit_ok(PS1), (
        "停用看门狗之后、旧包让位之前出现了绕过 Stop-AfterFailure 的出口"
        "（裸 throw / exit / 没被 catch 住的 cmdlet 调用）——那条路会把看门狗留在停用态，"
        "就是 2026-10-16 事故二")
    # 全脚本再核一遍：disable 之后到主流程末尾，不许有任何一句裸 throw
    lines, lo, _ = _window(PS1)
    bare_after = [ln for ln in lines[lo:] if re.match(r"^\s*throw\b", ln)]
    assert not bare_after, f"disable 之后主流程里还有裸 throw：{bare_after}"


def test_restore_path_re_enables_the_task_and_says_so_in_one_token():
    assert _restore_ok(PS1), (
        "恢复路径不再重新启用任务，或 ABORT token 不再报 watchdog=（moved=no 与 moved=yes "
        "两条分支各自要求什么，见 _restore_ok）")


def test_guard_restore_is_not_nested_behind_the_package_restart():
    assert _restore_not_nested(PS1), (
        "重新启用被嵌进了「要不要重启旧包」那条 if 里：disable-verify 那条中止路上进程还没停，"
        "于是看守不回来——这正是事故二的形状")


def test_restore_re_enabled_requires_a_fresh_enabled_readback():
    assert _restore_readback_ok(PS1), (
        "恢复路径又在 Enable-ScheduledTask 没抛之后直接打 re-enabled，或回读失败没有走 "
        "re-enable-FAILED：#155 的现场就是「命令没抛 = 开关动了」，而任务可以正常返回却仍停用")


def test_restore_does_not_claim_re_enabled_when_the_guard_was_never_down():
    assert _restore_already_enabled_ok(PS1), (
        "disable-did-not-take 中止会把「这次运行拥有停用」的登记带进恢复函数，但开关还开着；"
        "恢复路径没有先读一次（或没要求 via=settings-enabled）就 Enable，ABORT token 会把"
        "一次从没发生的停用说成 re-enabled")


def test_restore_rehearsal_harness_is_disposable_and_uses_the_real_helpers():
    """#155 的行为证据必须是一份"只动一次性任务"的可跑脚本，不是一句口述。

    这里只判形状与安全门（不需要 PowerShell）：脚本存在、纯 ASCII/CRLF、从 swap-package.ps1
    里抽的是**真函数**、会创建/注销 `AIAssistantSwapRehearsal-*` 一次性任务、按 action 认
    现网看守、并且包含回读失败的样本。真跑一次的证据由排练脚本自己的输出承担。
    """
    assert REHEARSAL_PS1.is_file(), (
        "deploy/rehearse-watchdog-restore.ps1 不在：ticket #155 要求的行为证据（一次性任务"
        "真跑 disable → abort → restore）没有可复跑的载体")
    raw = REHEARSAL_PS1.read_bytes()
    assert _ascii_ok(raw), "排练脚本出现非 ASCII 字节或 BOM：PS 5.1 会把整脚本错解码成语法错误"
    assert raw.count(b"\r\n") == raw.count(b"\n"), "排练脚本必须是 CRLF（与本仓 .ps1 一致）"
    text = raw.decode("ascii")
    for needle in (
        "AIAssistantSwapRehearsal-",
        "FunctionDefinitionAst",
        "Restore-WatchdogTask",
        "Get-WatchdogTaskState",
        "Stop-AfterFailure",
        "Unregister-ScheduledTask",
        "watchdog-hidden.vbs",
        "re-enable-FAILED",
        "already-enabled",
    ):
        assert needle in text, f"排练脚本缺 {needle!r}：安全门或回读样本不在，证据不可信"
    assert "finally" in text, "一次性任务必须在 finally 里注销"
    assert "X:\\qoder-155-rehearsal-no-such-package" in text, (
        "排练必须把 LiveExe 指向不存在的路径：Stop-AfterFailure 的旧包重启分支不许真跑")


def test_assigned_helpers_cannot_leak_extra_output():
    assert _single_value_helpers_ok(PS1), (
        "被赋值的 helper 里漏了输出，或「读不到就当已停用」回到了判据里："
        "$verify / $restore 会变成数组，属性读取从此是在猜")


def test_swap_package_ps1_is_pure_ascii_without_bom():
    raw = SWAP_PS1.read_bytes()
    bad = [(i, b) for i, b in enumerate(raw) if b >= 0x80]
    assert _ascii_ok(raw), f"出现非 ASCII 字节或 BOM（前 3 处 {bad[:3]}）：PS 5.1 会把整脚本错解码成语法错误"
    assert raw.count(b"\r\n") == raw.count(b"\n"), "混进了裸 LF：本仓 .ps1 是 CRLF"


def test_ascii_gate_bits_on_a_written_copy(tmp_path):
    """把这道门的牙印落到"真写盘的一份副本"上（仓库外，理由见 _target_ps1）。

    两侧都判：副本必须红、仓库那份必须照旧绿。只判内存里的字符串的话，"门在读文件"
    这件事本身就没被验过——而这正是它烂掉的方式（有人加了一行中文日志，门还在，
    只是没人再确认它读的是哪个路径）。
    """
    raw = SWAP_PS1.read_bytes()
    assert _ascii_ok(raw), "仓库里这份已经非 ASCII 了：先修它，牙印不作数"
    copy = tmp_path / "swap-package-with-a-chinese-logline.ps1"
    copy.write_bytes(raw.replace(b"Write-Output 'SWAP-DONE'",
                                 b"Write-Output '\xe8\xbf\x90\xe7\xbb\xb4\xe5\xb7\xb2\xe7\x9c\x8b'", 1))
    drifted = copy.read_bytes()
    assert drifted != raw, "副本没造出来：SWAP-DONE 那一行不在文件里了"
    assert not _ascii_ok(drifted), "非 ASCII 字节写进盘上副本而门还绿：这道门读的不是文件"


# ------------------------------------------------------------------ 漂移样本构造
LEGACY_BLOCK = """Disable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null
$afterState = (Get-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName).State
Write-Output ('DISABLED state=' + $afterState)
if ($afterState -ne 'Disabled') {
    throw 'watchdog did not actually disable - aborting BEFORE touching the running package'
}"""

# 事故一只修一半的样本：判决照旧读 State，只是中止改走恢复机制（看守不再被丢）
HALF_FIXED_BLOCK = """Disable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null
$script:watchdogDisabled = $true
$afterState = (Get-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName).State
Write-Output ('DISABLED state=' + $afterState)
if ($afterState -ne 'Disabled') {
    Stop-AfterFailure 'disable-did-not-take' ('watchdog did not actually disable, state=' + $afterState)
}"""


def _drift_disable_block(new_block: str) -> str:
    """把 disable 那一行到验环收尾（顶格 `}`）整段换成 new_block。

    收尾按"disable 之后第一处顶格 `}`"取：验环的花括号一律带缩进，只有 while 的那颗
    在 0 列，所以这个取法与内容无关。真文件必须是绿的，否则样本什么也证明不了。
    """
    lines, lo, hi = _window(PS1)
    assert lines is not None
    end = None
    for i in range(lo + 1, hi + 1):
        if lines[i] == "}":
            end = i
            break
    assert end is not None, "样本构造失败：disable 之后找不到顶格的收尾花括号"
    return "\n".join(lines[:lo] + new_block.splitlines() + lines[end + 1:])


def test_mutation_restoring_the_state_verdict_and_bare_throw_turns_the_locks_red():
    """牙印一：把 2026-10-16 现场原样放回去（读 State + 裸 throw），两条都必须红。"""
    assert _verdict_ok(PS1) and _no_bare_exit_ok(PS1), "真文件两条都必须绿，否则变异证明不了任何事"
    drifted = _drift_disable_block(LEGACY_BLOCK)
    assert drifted != PS1, "漂移样本没生效：disable 那一段的形状与 LEGACY_BLOCK 假设不一致了"
    assert "if ($afterState -ne 'Disabled') {" in drifted, "漂移没把旧判据放回去"
    assert not _verdict_ok(drifted), "把旧判据放回来，判决这条锁必须红"
    assert not _no_bare_exit_ok(drifted), "把裸 throw 放回来，出口这条锁必须红"
    # 分工的证据：恢复机制的函数体在漂移样本里**完好无损**，缺的只是有人调用它。
    # 所以这条必须仍绿，红的是出口锁——谁的活谁干，两条判据不许互相顶。
    assert _restore_ok(drifted), (
        "恢复判据在样本里也一起红了：说明它其实是在判调用点而不是判机制本体，"
        "那它就和出口锁重复，事故二那种「机制在、没人调」会同时漏过两条")


def test_mutation_state_verdict_routed_through_the_restore_reds_only_the_verdict_lock():
    """牙印二：只修事故二（中止走恢复机制）、事故一照旧读 State ⇒ 判决红、出口绿。

    两条锁必须是**互相独立**的判据，否则一条绿会替另一条说谎（本次票面要求"不是恰好
    能过的正则"，这条就是那条要求的直接证据：同一段文本，一条红一条绿）。
    """
    half = _drift_disable_block(HALF_FIXED_BLOCK)
    assert half != PS1, "半修样本没生效"
    assert not _verdict_ok(half), "判决仍读 State，_verdict_ok 必须红"
    assert _no_bare_exit_ok(half), "半修样本已经没有裸 throw 了，出口锁该继续绿（红就是判过头）"


def test_mutation_removing_the_re_enable_or_nesting_it_turns_the_restore_red():
    """牙印三/四：删掉启用、或把启用嵌进重启那条 if 里，都必须红。"""
    no_enable = PS1.replace(
        "            Enable-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName | Out-Null\n",
        "", 1)
    assert no_enable != PS1, "删启用的样本没生效：Enable-ScheduledTask 那一行的形状变了"
    assert not _restore_ok(no_enable), "恢复路径不再启用任务，恢复这条锁必须红"

    nested = PS1.replace("    $restore = Restore-WatchdogTask",
                         "            $restore = Restore-WatchdogTask", 1)
    assert nested != PS1, "嵌套样本没生效：$restore = Restore-WatchdogTask 那一行不是一句独立语句"
    assert not _restore_not_nested(nested), (
        "启用被嵌进重启那条 if 体内还能过——那条判据是在看字符串而不是看层级")

    # #51：锚点换成结构定位之后，条件换一种等价拼法不该让定位失效。不嵌套时这条
    # 锁必须照旧绿（旧的字面正则会在这里以"找不到锚点"误红），嵌套时照旧红。
    respelled = PS1.replace(
        "if (@(Get-LiveProcess).Count -eq 0 -and (Test-Path -LiteralPath $LiveExe)) {",
        "if ((Test-Path -LiteralPath $LiveExe) -and @(Get-LiveProcess).Count -eq 0) {",
        1)
    assert respelled != PS1, "条件改写样本没生效：重启那条 if 的文本形状变了"
    assert _restore_not_nested(respelled), (
        "条件换成等价写法后结构锚点找不到了：定位又回到了字面匹配（#51）")
    nested_respelled = respelled.replace("    $restore = Restore-WatchdogTask",
                                         "            $restore = Restore-WatchdogTask", 1)
    assert nested_respelled != respelled, "改写样本上的嵌套没生效"
    assert not _restore_not_nested(nested_respelled), (
        "条件改写后把恢复嵌进 if 体内却判绿了：结构锚点没有跟着重启动作走")


def test_mutation_logging_inside_an_assigned_helper_turns_it_red():
    """牙印五：往被赋值捕获的 helper 里塞一行 Write-Output，必须红。

    这条就是"恢复路径自己也可以是坏的"那一族：$restore 一旦变成数组，
    `ABORT ... watchdog=` 里打出来的是整条日志拼串，运维看到的是绿的、机器读到的是错的。
    """
    leaked = PS1.replace("        $failDetail = $_.Exception.Message",
                         "        $failDetail = $_.Exception.Message\n"
                         "        Write-Output 'RECOVERED watchdog-re-enabled'", 1)
    assert leaked != PS1, "漏输出样本没生效：Restore-WatchdogTask 的 catch 体形状变了"
    assert not _single_value_helpers_ok(leaked), "被赋值的 helper 里漏了输出，这条锁必须红"


def test_mutation_non_ascii_byte_turns_the_ascii_gate_red():
    """牙印六：真塞一个非 ASCII 字节进副本（票面要求的"门会自己烂掉"那一类）。"""
    raw = SWAP_PS1.read_bytes()
    assert _ascii_ok(raw)
    with_bad = raw.replace(b"Write-Output 'SWAP-DONE'",
                           b"Write-Output '\xe6\x9c\x8b\xe5\x85\x9b\xe5\xb7\xb2\xe5\x81\x9c\xe7\x94\xa8'", 1)
    assert with_bad != raw, "样本没生效：SWAP-DONE 那一行不在文件里"
    assert not _ascii_ok(with_bad), "加了中文日志行，ASCII 门必须红"
    with_bom = b"\xef\xbb\xbf" + raw
    assert not _ascii_ok(with_bom), "BOM 也要门住：PS 5.1 的解码坑与 BOM 有关"


def _drop_restore_readback(text: str):
    """把启用后的回读与失败分支整段摘掉，还原成 #155 之前"没抛就是成功"的形状。"""
    lines = text.splitlines()
    start = _idx(r"^function\s+Restore-WatchdogTask\s*\{", lines)
    if start is None:
        return None
    depth = 0
    end = None
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if depth <= 0 and i > start:
            end = i
            break
    if end is None:
        return None

    i_enable = None
    for i in range(start, end + 1):
        if re.match(r"^\s*Enable-ScheduledTask -TaskPath \$task\.TaskPath -TaskName \$task\.TaskName "
                    r"\| Out-Null\s*$", lines[i]):
            i_enable = i
            break
    if i_enable is None:
        return None
    i_read = None
    for i in range(i_enable + 1, end + 1):
        if READBACK_RE.match(lines[i]):
            i_read = i
            break
    if i_read is None:
        return None
    i_catch = None
    for i in range(i_read + 1, end + 1):
        if re.match(r"^\s*\}\s*catch\s*\{\s*$", lines[i]):
            i_catch = i
            break
    if i_catch is None:
        return None
    # 旧形状：启用、放下归属、直接打 re-enabled；catch 保留（旧锁要求 re-enable-FAILED 还在）
    old = ["            $script:watchdogDisabled = $false",
           "            $status = 're-enabled'"]
    return "\n".join(lines[:i_read] + old + lines[i_catch:])


def test_mutation_dropping_the_restore_readback_turns_only_the_new_lock_red():
    """牙印七（#155）：把启用后的回读摘掉，旧锁照旧绿、回读锁必须红。

    这条就是本次 review 指出的洞：`_restore_ok` 只判"有没有 Enable 那一句"，于是
    "Enable 没抛 → 打 re-enabled"能一路绿到下一次换包。红绿分工在这里是证据本身：
    旧锁看不见的射程，才需要新锁。
    """
    assert _restore_readback_ok(PS1), "真文件必须先绿"
    old = _drop_restore_readback(PS1)
    assert old is not None, "样本构造失败：Restore-WatchdogTask 的形状与摘取假设不一致了"
    assert old != PS1, "漂移样本没生效"
    assert _restore_ok(old), (
        "旧锁对摘掉回读的样本应当照旧绿：它管的是“有没有启用”，管不了“启用有没有生效”——"
        "这正是 #155 的洞能活下来的原因")
    assert not _restore_readback_ok(old), "回读被摘掉，新锁必须红"


def test_mutation_claiming_re_enabled_for_an_already_enabled_guard_turns_it_red():
    """牙印八（#155）：把从没停过的看守说成 re-enabled，归属锁必须红。"""
    assert _restore_already_enabled_ok(PS1), "真文件必须先绿"
    drifted = PS1.replace("$status = 'already-enabled'", "$status = 're-enabled'", 1)
    assert drifted != PS1, "样本没生效：already-enabled 这个 token 不在文件里"
    assert _restore_ok(drifted), "旧锁对这条语义无感，应当照旧绿"
    assert not _restore_already_enabled_ok(drifted), (
        "开关亲口答 Enabled=true 时仍报 re-enabled：那是一次从未发生的重新启用，新锁必须红")
