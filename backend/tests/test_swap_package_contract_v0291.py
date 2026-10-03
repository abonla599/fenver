"""v0.29.1 换包加固契约：deploy/swap-package.ps1 把《安装部署指南》「换入正在跑的服务：
三步顺序不能换」那一节的每一条血泪教训钉成可执行的文本锁。

这一版不新增功能，新增的是"别再手动敲一遍事故现场"：2026-10-03 那次 v0.29.0 换包
能一次成功，靠的是当场把文档里每一条坑都绕过去了；这个脚本把绕行固化成顺序本身。
每条断言对应一个真实事故：

- ASCII 纯净性 ← UTF-8 无 BOM 的中文注释 .ps1 被 PowerShell 5.1 错解码成语法错误，
  生产静默跑了九天旧包；
- 禁 Rename-Item ← 2026-09-20 上午 Rename-Item -NewName 收到整条目标路径不报错、
  原地不动，脚本照样打印"换好了"；
- 禁 Get-Process ← 它的属性是 .Id，.ProcessId 是 Win32_Process 的；写错不报错，
  只会静默地什么都没停；
- 按 action（watchdog-hidden.vbs）找计划任务 ← 任务名是中文，ASCII 脚本不能硬编码；
- 顺序锁 ← 看门狗只启动从不杀，disable 若在 stop/rename 之后，它会在换包窗口里
  把旧 exe 拉回来，文件句柄让 rename 当场 PermissionError，"换包换了一半"；
  re-enable 若在 health 断言之前，守住的可能是个半份包；
- 只改名不删除 ← 回滚全靠旧包还在盘上；
- 暂存污染闸门 ← dist\\run_backend 每次重建被整体删掉，2026-09-16 真丢过一次数据；
- 正向验收（build 相等 + app.js 标记）← 2026-09-19 第一次换包失败时 /health 照样
  200——"服务起来了"从来不等于"新代码在被服务"。

本测试读的是 PS 文件的【文本】（本仓库用 Python 文本断言锁 JS/Kotlin/PS 行为的惯例，
见 test_pill_anchor_to_handle_v02317_contract.py / test_empty_field_paste_backstop_v0283_contract.py）。
最后一个用例自证"真的在解析真文件"：把 disable 行与 stop 行对调后喂给同一个顺序
判据，它必须变红——即任何人调换步骤或删除步骤，test_step_order_* 会立刻指名哪一步
漂了（例：把 ENABLED 段挪到 HEALTH 断言之前 → test_enable_only_after_both_acceptance_checks；
删掉暂存 version.txt 校验 → test_stage_version_stamp_checked；把 .ProcessId 写进
Get-Process → test_no_rename_item_and_no_get_process）。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SWAP_PS1 = REPO_ROOT / "deploy" / "swap-package.ps1"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _read_bytes(p: Path) -> bytes:
    return p.read_bytes()


def _strip_comments(text: str) -> str:
    """剥掉整行 # 注释与 <# ... #> 块注释再判「某个写法绝对不许出现」——
    脚本的注释里合法地写着 Rename-Item / Get-Process 的病根说明（那是文档职责），
    文本判据只能管可执行的部分。"""
    no_block = re.sub(r"<#[\s\S]*?#>", "", text)
    return re.sub(r"(?m)^[ \t]*#.*$", "", no_block)


PS1 = _read(SWAP_PS1)
CODE = _strip_comments(PS1)


def _line_of(pattern: str, text: str = None, last: bool = False) -> int:
    """返回第一个（或最后一个）匹配行号（0 起）；匹配不到直接断言失败——
    「删掉某一步」与「把某一步挪走」是两种红法，都要红。"""
    src = PS1 if text is None else text
    hits = [i for i, ln in enumerate(src.splitlines()) if re.search(pattern, ln)]
    assert hits, f"契约里找不到这一步：{pattern}"
    return hits[-1] if last else hits[0]


def _steps_in_order(text: str) -> dict:
    """主路径各步骤的行号。注意每条正则都只命中主流程那一行：恢复/回滚用的
    Enable-ScheduledTask、回滚提示里打印的 Stop-Process 字符串都刻意与这些
    模式不同形（函数在文件里先定义，按 first-match 判序会被带偏）。"""
    return {
        "preflight": _line_of(r"PRECHECK ok", text),
        "disable": _line_of(r"Disable-ScheduledTask -TaskPath", text),
        "stop": _line_of(r"Stop-Process -Id \$p\.ProcessId -Force", text),
        "backup": _line_of(r"\$script:backupDone = \$true", text),
        "swap": _line_of(r"^Write-Output 'SWAPPED'$", text),
        "health": _line_of(r"\$h\.build -ne \$ExpectedVersion", text),
        "accept": _line_of(r"\$markerLines -lt 1", text),
        "enable": _line_of(r"Enable-ScheduledTask -TaskPath \$task\.TaskPath -TaskName \$task\.TaskName \| Out-Null",
                           text, last=True),
    }


def test_script_exists_and_is_pure_ascii():
    """九天静默旧包事故的锁：整个文件一个非 ASCII 字节都不许有（>=0x80 即红）。
    PowerShell 5.1 把 UTF-8 无 BOM 的中文注释错解码成语法错误——脚本死在半路，
    生产继续跑旧包，而且没有任何人被告知。中文任务名也因此不许出现在文件里，
    只能按 action 找任务。"""
    assert SWAP_PS1.is_file(), "deploy/swap-package.ps1 必须存在且入库（换包流程的机器可读形态）"
    raw = _read_bytes(SWAP_PS1)
    bad = [(i, b) for i, b in enumerate(raw) if b >= 0x80]
    assert not bad, f"出现非 ASCII 字节（PowerShell 5.1 错解码事故）：前 3 处 {bad[:3]}"


def test_no_rename_item_and_no_get_process():
    """两个「不报错但什么都没做」的写法在可执行部分一律禁绝：
    - Rename-Item：-NewName 只收叶子名，传整条目标路径它会静默原地不动
      （2026-09-20 上午事故：脚本打印"换好了"，两份包都在原位）；换目录必须 Move-Item。
    - Get-Process：它的属性是 .Id，.ProcessId 是 Win32_Process 的——写错不报错，
      只会静默地什么都没停。本脚本停进程全走 Win32_Process（Get-LiveProcess），
      所以连 Get-Process 这个 cmdlet 都不许出现，从根上没有拿错属性的可能。

    v0.29.2 换包当晚改判：换目录从"必须 Move-Item"收紧成"必须走整体改名器"。判据还是同一条
    ——**不许打印一句成功而文件没动**——只是当年的解法（Move-Item）自己漏了另一种
    静默形状：改名失败时它会先建目标目录再逐子项搬，2026-10-03 22:42 那次就在盘上
    留下一个空的 dist\run_backend_old-20261003-224259 与完好源目录并排。判据与理由
    见 test_swap_package_all_or_nothing_move.py。"""
    assert "Rename-Item" not in CODE, "换目录只许整体改名；Rename-Item 收整路径时静默不动"
    assert "Get-Process" not in CODE, "不许 Get-Process：.Id/.ProcessId 属性之争只留给 Win32_Process 一处"
    assert re.search(r"Invoke-PackageMove -From \$Live -To \$Backup", CODE), \
        "旧包必须先整体改名让位（这就是那条不许删的备份步）"
    assert re.search(r"Invoke-PackageMove -From \$StageDir -To \$Live", CODE), \
        "新包必须整体改名换入到位"


def test_watchdog_task_found_by_action_not_by_name():
    """任务名是中文（AI助手-崩溃守护），ASCII 脚本硬编码不了它——按 action 匹配
    wscript.exe + watchdog-hidden.vbs 找任务，这才是 2026-10-03 真跑通过的那条路。"""
    assert re.search(r"\$_.Actions\.Execute -eq 'wscript\.exe'", CODE), "按 action 宿主找任务"
    assert re.search(r"\$_.Actions\.Arguments -like '\*watchdog-hidden\.vbs\*'", CODE), \
        "按 vbs 包装器参数钉死唯一性，任务改名也不怕"
    assert "Get-ScheduledTask" in CODE and "watchdog-hidden.vbs" in PS1


def test_disable_watchdog_comes_before_first_stop_or_move():
    """顺序锁·前半：Disable-ScheduledTask 必须出现在第一次 Stop-Process 和第一次
    动 dist\\run_backend 之前——看门狗只启动从不杀，晚停用它，它就在停服与改名
    之间把旧 exe 拉回来，文件句柄让 rename 报 PermissionError，"换包换了一半"。
    预检（PRECHECK ok）还必须先于 disable：预检失败时生产连停都没停。"""
    st = _steps_in_order(PS1)
    assert st["preflight"] < st["disable"], "预检必须在停用看门狗之前——失败时生产须原封不动"
    assert st["disable"] < st["stop"], "停用看门狗必须先于停进程"
    assert st["disable"] < st["backup"], "停用看门狗必须先于旧包让位（rename）"


def test_enable_watchdog_only_after_both_acceptance_checks():
    """顺序锁·后半：re-enable 必须排在 build 相等断言与 app.js 标记断言都通过之后
    ——整段换包窗口保持停用正是这套顺序的意义；提前启用等于让看门狗去"守护"
    一个可能还没验收（甚至半份）的包。取主流程最后一次 Enable-ScheduledTask 判序：
    失败恢复路径里那一次位置在前，是合法的（它恢复的是原封未动的旧包）。"""
    st = _steps_in_order(PS1)
    assert st["health"] < st["enable"], "build 断言在启用回看门狗之前"
    assert st["accept"] < st["enable"], "app.js 标记断言在启用回看门狗之前"
    assert re.search(r"\$afterState -ne 'Disabled'", CODE), "disable 之后必须复查 State 真Disabled，不复查等于没停"


def test_old_package_renamed_never_deleted():
    """回滚的唯一依赖是旧包还在盘上：全脚本不许出现任何 Remove-Item（也不许
    rd/del/erase）；备份名带时间戳，重复换包不许互相覆盖。"""
    assert "Remove-Item" not in CODE, "旧包只改名不删除——删了就没有回滚位了"
    assert not re.search(r"\b(rd|del|erase)\b", CODE, re.I), "同上，换个别名也不行"
    assert re.search(r"run_backend_old-", PS1), "备份目录名必须带 run_backend_old- 前缀"
    assert re.search(r"yyyyMMdd-HHmmss", PS1), "备份名带时间戳：每次换包的备份互不覆盖"


def test_preflight_checks_stage_purity_and_version_stamp():
    """换包前的两条闸门，各钉一个已兑现的事故：
    - 暂存里冒出 .env / data\\ / chroma_db\\ 就拒换——dist\\run_backend 每次重建被
      整体删除，2026-09-16 因此真丢过一次运行时数据；
    - 暂存包里 _internal\\version.txt 必须等于 -ExpectedVersion——冻结版认随包构建
      那份戳（4d39daa9 修的就是根上旧戳压过包内新戳、把 v0.24.1 报成 v0.24.0）。"""
    assert re.search(r"\.env', 'data', 'chroma_db", CODE), "三件套污染闸门都要在"
    assert re.search(r"Join-Path \$StageDir \$poison", CODE), "污染闸门查的是暂存目录"
    assert re.search(r"PRECHECK fail stage-contaminant", CODE)
    assert "_internal\\version.txt" in PS1.replace("\\\\", "\\"), "暂存包内版本戳路径要钉死在 _internal 下"
    assert re.search(r"\$stageVersion -ne \$ExpectedVersion", CODE), "戳内容必须与 -ExpectedVersion 全等比较"
    assert re.search(r"stage-version=", PS1)


def test_positive_acceptance_not_merely_port_answering():
    """验收判据是「新代码确实在被服务」，不是「端口有东西应答」：
    2026-09-19 第一次换包失败时 /health 全程 200 应答旧包。所以必须
    ① /health 的 build 字段与 -ExpectedVersion 全等，
    ② 拉取在被服务的 /app/app.js、数 -Marker 命中行数且须 >0，
    ③ 换包中途（备份改名之后）任何失败都打印带真实备份路径的手工回滚配方——
       生产机上自作主张的自动回滚比一条清楚的指令更危险。"""
    assert re.search(r"/health", CODE) and re.search(r"\$h\.build -ne \$ExpectedVersion", CODE), \
        "build 全等断言必须在"
    assert re.search(r"/app/app\.js", CODE), "必须真的去拉在被服务的 app.js"
    assert re.search(r"\$markerLines -lt 1", CODE), "标记行数必须判 >0，不判等于没验收"
    assert re.search(r"ROLLBACK backup-package=", CODE), "回滚配方必须把真实备份路径打出来"
    assert re.search(r"STOPPED pid=", CODE), "停进程结果必须可机读（静默没停就是事故重演）"


def test_mutation_reordering_a_step_turns_the_order_lock_red():
    """自证契约真在解析真文件：只改内存里的文本——把 disable 行与 stop 行对调
    （即"先停进程、后停用看门狗"，2026-10-03 文档里记的那次半换包事故的形状）——
    顺序判据必须立刻变红；原文必须立刻变绿。这条就是"任何人调换步骤会红"的证据。"""
    def ordered(text: str) -> bool:
        try:
            st = _steps_in_order(text)
        except AssertionError:
            return False
        return (st["preflight"] < st["disable"] < st["stop"]
                and st["disable"] < st["backup"] < st["swap"] < st["health"] < st["accept"] < st["enable"])

    assert ordered(PS1), "真文件的步骤顺序必须全绿"

    lines = PS1.splitlines()
    i_dis = _line_of(r"Disable-ScheduledTask -TaskPath", PS1)
    i_stop = _line_of(r"Stop-Process -Id \$p\.ProcessId -Force", PS1)
    lines[i_dis], lines[i_stop] = lines[i_stop], lines[i_dis]
    assert not ordered("\n".join(lines)), "把停用看门狗挪到停进程之后，顺序锁必须红"


def test_startup_window_is_polled_with_a_knob_not_a_hardcoded_sleep():
    """v0.29.1 换包当晚实测到的脚本自身缺陷：冻结版起进程要过 chromadb 导入与
    服务商探测，**进程活着但 /health 还没应答**是常态（20:44:42 起、20 秒后才通）。
    当时写死的 `Start-Sleep -Seconds 12` 于是把一个**好包**判死，打印了整套回滚
    配方，而生产只是慢了十几秒——误报的回滚指令比没有指令更危险，因为它让人去动
    一个本来正常的东西。锁的形状：等待必须是轮询 + 可调上限，两处上限同一个旋钮。"""

    def polled(text: str) -> bool:
        return bool(re.search(r"\[int\] \$StartTimeoutSeconds = (\d+)", text)
                    and re.search(r"\$bootDeadline = \(Get-Date\)\.AddSeconds\(\$StartTimeoutSeconds\)", text)
                    and re.search(r"while \(\(Get-Date\) -lt \$bootDeadline\)", text)
                    and re.search(r"\$deadline = \(Get-Date\)\.AddSeconds\(\$StartTimeoutSeconds\)", text))

    assert polled(PS1), "起进程与 /health 两处等待都必须轮询、都吃 -StartTimeoutSeconds"
    default = int(re.search(r"\[int\] \$StartTimeoutSeconds = (\d+)", PS1).group(1))
    assert default >= 60, f"默认上限 {default}s 撑不住一次冷启动（实测约 20s，留一倍余量）"
    assert not re.search(r"Start-Sleep -Seconds (1[0-9]|[2-9][0-9])\r?\n\$running = Get-LiveProcess", PS1), \
        "写死的固定秒数不许回到起进程检查前面"
    # STARTED 必须在轮询循环之后、/health 判定之前：顺序反了就是"还没起来就验收"
    assert _line_of(r"\$bootDeadline = ", PS1) < _line_of(r"^Write-Output \('STARTED pid='", PS1) \
        < _line_of(r"\$h\.build -ne \$ExpectedVersion", PS1)

    mutated = PS1.replace("while ((Get-Date) -lt $bootDeadline) {",
                          "if ((Get-Date) -lt $bootDeadline) {", 1)
    assert not polled(mutated), "把轮询改成一次性判断，这条锁必须红"
