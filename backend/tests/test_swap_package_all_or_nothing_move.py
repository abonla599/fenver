r"""换包脚本整体改名契约：把 2026-10-03 深夜那次 v0.29.2 换包里**脚本自己**闯的
两个祸钉成文本锁（判据文件与惯例同 test_swap_package_contract_v0291.py：读 PS 源码
的文本，CI 不跑 PowerShell）。

事故一（改名会闯祸的一半）：22:42:59 第一次换包，进程已停、8000 端口已清，
`Move-Item -LiteralPath $Live -Destination $Backup` 仍在改名那一瞬死于一个系统还没
放开的句柄。Move-Item 在这种情况下**不是只抛异常**：FileSystem provider 会先把目标
目录建出来，再逐个搬子项，搬到一半死掉。这一次运气好——源目录一个字节都没动，脚本
按 `moved=no` 原样恢复并重启了旧包，但盘上从此多了一个**空的**
`dist\run_backend_old-20261003-224259`，和真正完好的 `dist\run_backend` 并排躺着：
"哪一份是回滚位"这个问题第一次有了两个答案。换包脚本最不该制造的就是这种歧义。
`[System.IO.Directory]::Move` 是一次 rename：要么整个搬走，要么什么都没发生，
而且**目标已存在时直接拒绝**——最后这半个性质同时治了事故二。

事故二（回滚配方自己是个坑）：脚本打印的 ROLLBACK-HOWTO 第 4 步原文是
`Move-Item -LiteralPath '$Backup' -Destination '$Live'`。22:47 手敲这条配方时，
`$Live` 还在（第 3 步没做成），而 Move-Item 遇到"目标目录已存在"**不报错**，它把
备份塞进目标**里面**一层——旧包当场变成
`dist\run_backend\run_backend_old-20261003-224427\`。一条把人往"包套包"上带的回滚
指令，比没有指令更危险（同一族的先例：写死的 12 秒等待让脚本给一个好包打印回滚配方）。

事故三（"没看见进程"被当成"包没起来"）：第二次换包改名与换入都成功，`Start-Process`
也在 +7 秒把进程起来了（pid 58728，事后 /health 直接答 build=v0.29.2），但
started-check 轮询 90 秒一次都没匹配上进程，于是打印了整套回滚配方。真正的事实是
**服务在正常服务**，错的是"我只信进程列表"这一个判据。现在失败前必须再问一次服务
自己：/health 已经答出我们要发的那个 build，就没有任何可回滚的东西（STARTED 打
pid=-1，意思是"health 认证在跑、进程列表看不见"，绝不能再是 0——0 是触发失败的那颗哨兵）。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SWAP_PS1 = REPO_ROOT / "deploy" / "swap-package.ps1"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """剥掉整行 # 注释与 <# ... #> 块注释再判「某个写法绝对不许出现」——本文件的
    注释里合法地写着 Move-Item 的病根说明（那是文档职责），文本判据只管可执行部分。"""
    no_block = re.sub(r"<#[\s\S]*?#>", "", text)
    return re.sub(r"(?m)^[ \t]*#.*$", "", no_block)


PS1 = _read(SWAP_PS1)
CODE = _strip_comments(PS1)
# 回滚配方是**打印出来的字符串**（Show-Rollback 里那些 Write-Output），不属于可执行
# 部分；判它必须用原文，用 CODE 会把整段配方判成"不存在"。
RECIPE = PS1[PS1.index("function Show-Rollback"):]


def _line_of(pattern: str, text: str = None, last: bool = False) -> int:
    src = PS1 if text is None else text
    hits = [i for i, ln in enumerate(src.splitlines()) if re.search(pattern, ln)]
    assert hits, f"契约里找不到这一步：{pattern}"
    return hits[-1] if last else hits[0]


def _between(text: str, start_pattern: str, end_line: int) -> str:
    """从 start_pattern 那一行起到第 end_line 行（含）为止的原文切片。

    行号不能当字符下标用：这里每一步都只有几十行，切错方向的症状是"判据明明在场
    却搜不到"，比写错正则更难查，所以统一走这一个函数。
    """
    lines = text.splitlines()
    hits = [i for i, ln in enumerate(lines) if re.search(start_pattern, ln)]
    assert hits, f"切片起点找不到：{start_pattern}"
    return "\n".join(lines[hits[0]:end_line + 1])


def test_live_package_moves_go_through_the_all_or_nothing_mover():
    """两次换目录（旧包让位、新包换入）都必须走 Invoke-PackageMove。

    形状判据三条，缺一条就可能退回"建目标 + 逐子项搬"那半边：主流程不许再出现
    Move-Item；helper 体必须是 [System.IO.Directory]::Move；helper 必须带**有界**
    重试（句柄没放开是一个两秒钟的状态，不是一次该放弃换包的判决）。
    """
    assert re.search(r"Invoke-PackageMove -From \$Live -To \$Backup", CODE), \
        "旧包让位必须走整体改名：Move-Item 改名失败时会先建目标目录再逐子项搬，半份包就是这么来的"
    assert re.search(r"Invoke-PackageMove -From \$StageDir -To \$Live", CODE), \
        "新包换入同上，必须走同一个改名器"
    assert not re.search(r"^\s*Move-Item ", CODE, re.M), \
        "可执行部分一个 Move-Item 都不许留：两处换目录都已有整体改名的替身"
    body = CODE[CODE.index("function Invoke-PackageMove"):]
    assert re.search(r"\[System\.IO\.Directory\]::Move\(\$From, \$To\)", body), \
        "改名器的心脏必须是一次 rename（要么全动要么全不动，且目标已存在时拒绝）"
    assert re.search(r"param\(\[string\] \$From, \[string\] \$To, \[int\] \$TrySeconds = (\d+)\)", body), \
        "重试窗口必须写成参数：默认值就是'句柄算多久'的口径"
    seconds = int(re.search(r"\$TrySeconds = (\d+)", body).group(1))
    assert 5 <= seconds <= 120, f"重试窗口 {seconds}s 既撑不住句柄释放，也不该把换包拖成事故"
    assert "throw" in body[body.index("catch"):], "到点必须把原始异常抛出去，静默吞掉就是没改名成功还继续走"
    assert "Copy-Item" not in CODE, "复制粘贴式换包会把'半份包'从概率变成必然"


def test_rollback_recipe_cannot_bury_a_backup_inside_the_live_package():
    """打印出来的第 4 步必须用拒绝覆盖的改名器，不许是 Move-Item。

    22:47 手敲老配方时 `$Live` 还在，Move-Item 不报错而是把备份塞进去一层。
    Directory::Move 在这种情况下抛"目标已存在"——报错停在原地，正是回滚指令唯一
    该有的失败方式。判据读 RECIPE（配方是字符串，不在 CODE 里）。
    """
    assert re.search(r"\[System\.IO\.Directory\]::Move\('\$Backup', '\$Live'\)", RECIPE), \
        "回滚第 4 步必须在目标已存在时拒绝，而不是把备份埋进活包里"
    assert re.search(r"\[System\.IO\.Directory\]::Move\('\$Live', '\$Live-failed-", RECIPE), \
        "第 3 步（把半活的包挪开留证据）同样只许整体改名"
    assert not re.search(r"Move-Item -LiteralPath '\\$Backup' -Destination '\$Live'", RECIPE), \
        "打印这条配方的人自己会先踩一遍：目标存在时 Move-Item 静默嵌套，不许回到配方里"


def test_started_check_asks_the_service_before_declaring_failure():
    """"进程列表里没有" 不能单独定罪：先问 /health 是不是已经在答我们要发的 build。

    钉四件：轮询失败分支里必须调 Get-HealthOnce；必须与 $ExpectedVersion 全等比较
    （"有东西在答" 不算——2026-09-19 那次 /health 全程 200 答的是旧包）；health
    认下来的那条路要把 $livePid 置成非 0 的哨兵（0 是触发失败与回滚配房的值）；
    整套判序仍要在 /health 验收断言之前。
    """
    i_fail = _line_of(r"Stop-AfterFailure 'started-check'", PS1)
    i_probe = _line_of(r"\$asked = Get-HealthOnce", PS1)
    assert i_fail > i_probe, "started-check 失败前必须先问一次服务，不问就打印回滚配方是误报"
    block = _between(PS1, r"\$asked = Get-HealthOnce", i_fail)
    assert re.search(r"\$asked\.build -eq \$ExpectedVersion", block), \
        "第二个判据必须与目标 build 全等，光答 200 不作数"
    assert re.search(r"\$livePid = -1", block), \
        "health 认下来时必须离开 0：0 是'宣布失败并打印配方'的哨兵值"
    assert _line_of(r"function Get-HealthOnce", PS1) < i_probe, "探针要先定义后使用"
    assert re.search(r"return Invoke-RestMethod -Uri \('http://127\.0\.0\.1:\{0\}/health' -f \$Port\) -TimeoutSec 5",
                     CODE), "探针打的必须是本机回环上的 /health（换包窗口里外部链路不可信）"
    assert _line_of(r"\$h\.build -ne \$ExpectedVersion", PS1) > i_fail, \
        "第二轮正式验收仍在后面：started-check 只是别把好消息当坏消息报"


def test_volume_mismatch_is_a_precheck_not_a_step_five_surprise():
    """整体改名不能跨卷，所以"暂存与活包不在同一卷"必须在停任何进程之前判掉。

    判据两半：预检本体在场（GetPathRoot 比较 + stage-volume 失败行）；并且它排在
    第一次停用看门狗与第一次 Stop-Process 之前——预检失败时生产须原封不动。
    """
    assert re.search(r"\[System\.IO\.Path\]::GetPathRoot\(\$StageDir\)", CODE) and \
        re.search(r"\[System\.IO\.Path\]::GetPathRoot\(\$Live\)", CODE), \
        "卷比对必须取两条路径各自的根"
    assert re.search(r"PRECHECK fail stage-volume", CODE), "对不上要按预检失败收场（一条都不许动）"
    i_vol = _line_of(r"GetPathRoot\(\$StageDir\)", PS1)
    assert i_vol < _line_of(r"^Disable-ScheduledTask -TaskPath", PS1), \
        "卷检查必须在停用看门狗之前：发现得太晚就等于把生产停在半路"
    assert i_vol < _line_of(r"Stop-Process -Id \$p\.ProcessId -Force", PS1), \
        "更要早于第一次真正停服：预检失败时生产须原封不动"


def test_failure_verdicts_stay_on_one_physical_line():
    """PS 5.1 解析器在换行处收尾一条语句，除非这一行**以运算符结尾**。

    2026-10-03 写第二判据时就把消息拆成了三行（第二行以 `+` 开头），当场
    "表达式缺少 ')'"——一个打印失败原因的句子自己把整个脚本弄死，这比误报还糟。
    这里锁住所有 Stop-AfterFailure 调用都是单行，并把"下一行以运算符开头"这一形状
    在全文件禁绝（PowerShell 只认行尾运算符续行）。
    """
    for ln in CODE.splitlines():
        assert not re.match(r"^\s*[+\-*/]=?\s", ln), \
            f"以运算符开头的续行 PowerShell 5.1 不认（它只认行尾运算符）：{ln[:60]!r}"
    for i, ln in enumerate(PS1.splitlines()):
        # 只判**调用**：函数定义那一行 `function Stop-AfterFailure {` 不是一句判决
        if not re.match(r"^\s*Stop-AfterFailure\s+'", ln):
            continue
        # 一句判决必须在**一行里说完**：判据取"括号在这一行配平"——不配平就是续到了
        # 下一行，而 5.1 只有在行尾是运算符时才肯往下读。
        assert ln.count("(") == ln.count(")"), \
            f"第 {i + 1} 行的失败判决没在一行里说完：判决语句拆行会把脚本本身弄死"
        assert not ln.rstrip().endswith("+"), \
            f"第 {i + 1} 行把一句判决拆成了两行（行尾运算符）：整句请写在一行里"


def test_mutation_removing_the_second_witness_turns_the_lock_red():
    """自证：把 health 第二判据整段删掉（回到"进程列表说了算"），上面那条锁必须红。

    反向也要红：把配方第 4 步退回 Move-Item，配方锁必须指名它。原文必须全绿。
    """
    def started_ok(text: str) -> bool:
        try:
            i_fail = _line_of(r"Stop-AfterFailure 'started-check'", text)
            i_probe = _line_of(r"\$asked = Get-HealthOnce", text)
        except AssertionError:
            return False
        block = _between(text, r"\$asked = Get-HealthOnce", i_fail)
        return (i_fail > i_probe
                and bool(re.search(r"\$asked\.build -eq \$ExpectedVersion", block))
                and bool(re.search(r"\$livePid = -1", block)))

    def recipe_ok(text: str) -> bool:
        recipe = text[text.index("function Show-Rollback"):]
        return bool(re.search(r"\[System\.IO\.Directory\]::Move\('\$Backup', '\$Live'\)", recipe)) \
            and not re.search(r"Move-Item -LiteralPath '\$Backup' -Destination '\$Live'", recipe)

    assert started_ok(PS1) and recipe_ok(PS1), "真文件两条都必须绿"
    assert not started_ok(PS1.replace("$asked = Get-HealthOnce", "$asked = $null", 1)), \
        "把第二判据摘掉，started-check 锁必须红"
    back = PS1.replace("[System.IO.Directory]::Move('$Backup', '$Live')",
                       "Move-Item -LiteralPath '$Backup' -Destination '$Live'", 1)
    assert not recipe_ok(back), "把回滚第 4 步退回 Move-Item，配方锁必须红"
