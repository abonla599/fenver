"""本机运维脚本（deploy/）里那些"只有出事那天才看得出来"的源码锁。

这些脚本没有可注入的接口、也不该被 import（它们一跑就往计划任务/进程表上伸手），
所以这里全是读源码的形状锁 —— 和 test_native_android_discipline.py 同一类：它们不证明功能对，
只守住"别再让注释变成第二个事实来源"。下面每条都对应一次真实的静默失效。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHDOG = REPO_ROOT / "deploy" / "watchdog.ps1"


def _code(src: str) -> str:
    """去掉 <# #> 块注释、整行 # 注释和行尾 # 注释，只留代码。

    为什么要先剥注释：这个文件里解释"为什么这么写"的行数快赶上代码了。
    拿原文去数 `if`、`return`，注释里那句"上一版这里漏了这个开关"就会参与计数。
    """
    src = re.sub(r"<#.*?#>", "", src, flags=re.S)
    out = []
    for line in src.splitlines():
        stripped = line.split("#", 1)[0].rstrip()
        if stripped.strip():
            out.append(stripped)
    return "\n".join(out)


def _region(code: str, start: str, end: str, label: str) -> str:
    """取 start 之后、end 之前的那段。任一标记不存在就 ValueError 当场报错——
    这正是正向对照要的：路径写错、改名没同步时，锁不许安静地"通过"。"""
    i = code.index(start)
    j = code.index(end, i)
    assert j > i, f"{label}：结束标记在起始标记之前，取到的区间不可信"
    return code[i:j]


def test_the_tunnel_guard_asks_whether_the_tunnel_is_serving():
    """2026-09-20 那次 Error 1033：cloudflared 进程一直活着，看门狗每分钟全绿。

    「进程在不在」量不出「链路通不通」，所以隧道那一条必须带第二判据。少传任何一个
    参数都会让它退回只看进程：LivenessKey 漏了就没处记连续次数，阈值漏了虽然还有
    默认值 3 能跑，但「改一处就把守护悄悄降级」的形状必须当场红。
    """
    code = _code(WATCHDOG.read_text(encoding="utf-8"))
    block = _region(code, "-Key 'tunnel'", "-Launch", "隧道那条 Invoke-Guard")

    for param in ("-LivenessProbe", "-LivenessKey", "-LivenessThreshold"):
        assert param in block, f"隧道守护少了 {param}：它会退回只看进程在不在，1033 那种故障再次无人知晓"
    assert "tunnel-unready" in block, "连续不通的次数没存进状态文件，重启就清零、永远攒不到门槛"


def test_the_readiness_probe_covers_both_connectors():
    """补起来的那个 connector 用的是另一个端口。

    只问主端口的话：升级动作真的成功了，看门狗却仍然认为「不服务」，于是每小时烧掉
    三次预算去起第四、第五个进程 —— 检测对了，动作变成了新的故障。
    """
    code = _code(WATCHDOG.read_text(encoding="utf-8"))
    served = _region(code, "function Test-TunnelServed", "function Test-TunnelReady",
                     "Test-TunnelServed")
    assert "Test-TunnelReady" in served, "Test-TunnelServed 没去问 /ready，那它量的就不是「对外可服务」"

    block = _region(code, "-Key 'tunnel'", "-Launch", "隧道那条 Invoke-Guard")
    assert "$TunnelMetricsPort" in block and "$TunnelAltMetricsPort" in block, (
        "判据必须同时问主端口和补位端口：补的那个不能抢主端口（旧的还占着，抢了就当场退出）"
    )

    probe = _region(code, "function Test-TunnelReady", "function Get-BackendProcess",
                    "Test-TunnelReady")
    assert "/ready" in probe, "问的不是 /ready —— 只有它表示「至少一条 connector 连上了边缘」"
    assert "200" in probe, "只看请求没抛异常的话，非 200 也会被当成服务中"


def _unguarded_returns(lines):
    """返回那些【直接父层不是 if】的裸 `return` 所在行（0 起）。

    为什么不是"往前看 6 行有没有 if"：试过一版那样判的，结果上一轮那个真实 bug
    （把 `return` 加在 `} else {` 的末尾）逃得干干净净 —— 它前 6 行里全是 `if`，
    可那些 `if` 早就闭合了，这条 return 相对 else 分支仍然是无条件的。
    所以这里维护一个括号栈，只看【当前还开着的最内层那个块】是谁开的。

    字符串字面量先抹掉再数括号。仍然是源码形状的近似，不是编译器。
    """
    stack = []
    offenders = []
    for idx, line in enumerate(lines):
        code = re.sub(r"'[^']*'", "''", line)
        if re.fullmatch(r"\s*return\s*", line):
            parent = stack[-1] if stack else ""
            if "if (" not in parent:
                offenders.append(idx)
        for pos, ch in enumerate(code):
            if ch == "{":
                stack.append(code[:pos])
            elif ch == "}" and stack:
                stack.pop()
    return offenders


def test_detecting_unready_actually_reaches_the_launch_code():
    """上一版最贵的一条：WARN 打出来了，函数却照样 return。

    活着的分支里那句无条件 `return` 让升级路径根本到不了拉起代码 —— 日志写着
    「按【隧道对外不可用】处理」，进程表里一个都没多。这类「效果没了但不报错」
    正是这个文件要守的形状。
    """
    code = _code(WATCHDOG.read_text(encoding="utf-8"))
    block = _region(code, "$escalate = $false", "if (-not $escalate)", "存活分支的判据段")

    lines = block.splitlines()
    returns = [i for i, ln in enumerate(lines) if re.fullmatch(r"\s*return\s*", ln)]
    assert returns, "这段里一条 return 都没有 —— 要么改形状了，要么这条锁在空转"

    offenders = _unguarded_returns(lines)
    assert not offenders, (
        f"这段第 {offenders} 行的 return 直接父层不是 if（多半挂在某个分支末尾），"
        "它会把存活分支无条件收尾，于是「检测到不服务」永远不会变成「再起一个 connector」"
    )

    assert "$escalate = $true" in block, "没有置真这一步，检测与动作就是两套各跑各的"



def test_budget_counters_never_cast_a_wrapped_array():
    """Read-State 给每个值套了 @()（那是给拉起记录数组准备的）。

    于是 `[int]$State[$Key]` 直接抛「无法将 System.Object[] 转换为 System.Int32」，
    整轮巡检被 catch 掉：后端守得好好的，隧道那条从此再没被执行过。
    """
    code = _code(WATCHDOG.read_text(encoding="utf-8"))
    bad = re.findall(r"\[int\]\s*\$State", code)
    assert not bad, f"这几处把状态值直接强转 int，撞上 @() 就整轮崩：{bad}"
    assert "function Get-Counter" in code and "function Set-Counter" in code, (
        "计数要走这两个函数 —— 它们替 @() 兜了一层，别拆了包装只留调用方"
    )


def test_the_replacement_connector_is_launched_with_usable_arguments():
    """三处实测踩出来的形状错误，每一条都能让「已拉起」变成一句谎：

    1) `tunnel run <名> --metrics ...` 会被判成「accepts only one argument」，进程立刻退出；
    2) 补位的 connector 若沿用主端口，会在旧进程还占着时 bind 失败退出；
    3) Start-Process -WindowStyle Hidden 丢掉子进程 stderr，于是前两种失败连一行痕迹
       都不留（第 1 条就是这么藏了一整轮：日志写着已拉起，进程表里只有原来那一个）。
    """
    code = _code(WATCHDOG.read_text(encoding="utf-8"))

    args_line = [ln for ln in code.splitlines() if "$tunnelArgs" in ln and "=" in ln]
    assert len(args_line) == 1, f"参数数组应当只赋值一次，读到 {len(args_line)} 行"
    pos = [args_line[0].index(t) for t in ("'tunnel'", "'--metrics'", "'run'")]
    assert pos == sorted(pos), "顺序必须是 tunnel → --metrics → run <名>；挂在 run 后面会被拒"
    assert "$TunnelAltMetricsPort" in args_line[0], "补位的用了主端口：旧进程没死它就 bind 失败退出"

    starts = [ln for ln in code.splitlines()
              if ln.strip().startswith("Start-Process") and "$TunnelExe" in ln]
    assert len(starts) == 1, f"隧道拉起应当只有一句 Start-Process，读到 {len(starts)} 行"
    assert "-RedirectStandardError" in starts[0], (
        "不接住 stderr，「用法错误」「端口被占」这类启动即失败就完全无痕"
    )


def _launch_command_line(code: str, tunnel_name: str) -> str:
    """把 -Launch 里那个参数数组还原成真实会出现在进程表里的命令行。

    判据用的是脚本**自己的**拉起参数，不是我在测试里手抄的一份：手抄的那版会和
    脚本各自漂移，而这条锁要防的正是"两边不一致"。
    """
    args_line = [ln for ln in code.splitlines() if "$tunnelArgs" in ln and "=" in ln]
    assert len(args_line) == 1, f"参数数组应当只赋值一次，读到 {len(args_line)} 行"
    # 逐元素解析整个 @(...)：里面有**裸变量** `$TunnelName` 和带变量的双引号串
    # `"127.0.0.1:$TunnelAltMetricsPort"`。只抓单引号字面量的那版会把隧道名整个漏掉，
    # 于是拼出来的"命令行"根本没有 run 的宾语——测的还是个假形状。
    arr = re.search(r"@\((.*)\)", args_line[0])
    assert arr, f"没能取回 @(...) 参数数组：{args_line[0]}"
    parts = []
    # 用 finditer 取 **group(0)**：findall 在有捕获组时只返回组内容，裸变量那一条
    # 分支没有捕获组，findall 会把它变成 ('','') —— 于是 `$TunnelName` 整个消失，
    # 拼出的"命令行"没有 run 的宾语（正是这条锁要防的形状，却先骗过了自己）。
    for m in re.finditer(r"'([^']*)'|\"([^\"]*)\"|(\$[A-Za-z_][A-Za-z0-9_]*)",
                         arr.group(1)):
        tok = m.group(1) or m.group(2) or ""
        if not tok:                                   # 裸变量项
            if m.group(3) == "$TunnelName":
                tok = tunnel_name
            elif m.group(3) == "$TunnelAltMetricsPort":
                tok = "127.0.0.1:34568"
            else:
                tok = m.group(3)                      # 其它变量原样留着，不影响判据
        if tok.startswith("127.0.0.1:"):
            tok = "127.0.0.1:34568"                   # 端口值不重要，形状才重要
        parts.append(tok)
    assert parts[:1] == ["tunnel"], f"参数数组第一元应是 'tunnel'，实际 {parts}"
    assert tunnel_name in parts, f"参数数组里没有隧道名 {tunnel_name}：{parts}"
    return " ".join(["cloudflared.exe"] + parts)


def _probe_pattern(code: str, tunnel_name: str) -> str:
    """从 Get-TunnelProcess 里取回它真正拿去 -match 的那个模式串。

    `[regex]::Escape($Name)` 按字面替换成转义后的隧道名，字符串字面量原样取。
    """
    fn = _region(code, "function Get-TunnelProcess", "function Invoke-Guard",
                 "Get-TunnelProcess")
    # 不锚行尾：那一行以 `) }` 收尾，锚 `$` 会永远取不到。贪婪到**最后一个**右括号
    # 正好取回整个 -match 表达式（字符串字面量里那个 `(?:\s|$)` 的括号不是候选终点，
    # 因为后面还有闭合括号）。
    m = re.search(r"-match\s*\((.+)\)", fn)
    assert m, "没找到 Get-TunnelProcess 里的 -match 表达式，这条锁在空转"
    expr = m.group(1)
    esc_call = "[regex]::Escape($Name)"
    pieces = []
    pos = 0
    while True:
        lit = re.search(r"'([^']*)'", expr[pos:])
        esc = expr.find(esc_call, pos)
        if esc == -1:
            if lit:
                pieces.append(lit.group(1))
            break
        if lit and pos + lit.start() < esc:
            pieces.append(lit.group(1))
            pos = pos + lit.end()
            continue
        pieces.append(re.escape(tunnel_name))
        pos = esc + len(esc_call)
    pattern = "".join(pieces)
    assert pattern.strip(), "取回的匹配模式是空的"
    return pattern


def test_the_tunnel_probe_recognizes_the_tunnel_this_script_launches():
    r"""**探针必须认得自己拉起的那条隧道**——判据和对象之间不许有缝。

    v0.33.0 上线后的实测形状：`-Launch` 用的是
    `tunnel --metrics 127.0.0.1:<port> run <名>`（--metrics 必须挂在 run 前面，
    否则 cloudflared 判 "accepts only one argument" 当场退出，脚本自己的注释就写着
    这条），而 `Get-TunnelProcess` 当年搜的是 `tunnel\s+run\s+<名>`——中间插了
    `--metrics …` 之后那个模式**永远匹配不上**。于是看门狗每分钟把自己正在服务的
    隧道判成死的：再拉一个、烧完预算、日志写着"已拉起"，而真正在跑的 connector
    从来没被数过。

    这条锁不是搜关键字，是**把脚本自己的参数数组拼成命令行，再拿脚本自己的模式去
    匹配**。任何一边改了参数顺序或匹配写法而没同步另一边，当场红。

    负向锁：把 `-Launch` 的 `--metrics` 挪回 `run <名>` 之后 → 拉起那行红；
    把探针模式改回 `tunnel\s+run\s+<名>` → 这条红。
    """
    code = _code(WATCHDOG.read_text(encoding="utf-8"))
    # 隧道名从脚本自己的定义里取，取不到就当场报错——不许在测试里写死一个值，
    # 那等于又造了第二个事实来源。
    m = re.search(r"\$TunnelName\s*=\s*'([^']*)'", code)
    assert m, "没读到 $TunnelName 的字面值，这条锁在空转"
    name = m.group(1)

    pattern = _probe_pattern(code, name)
    cmdline = _launch_command_line(code, name)

    assert re.search(pattern, cmdline), (
        "Get-TunnelProcess 认不出本脚本 -Launch 起来的隧道：\n"
        "  命令行 = %s\n  模式   = %s\n"
        "探针判死 → 反复拉起 → 预算烧光，而真正在服务的 connector 从没被数过。"
        % (cmdline, pattern)
    )
    # 反向：quick 隧道（没有 run <名> 这一段）不许被当成命名隧道的持有者
    quick = "cloudflared.exe tunnel --url http://127.0.0.1:8000"
    assert not re.search(pattern, quick), (
        "探针把 quick 隧道也认成了命名隧道：残留的 --url 进程会给死掉的命名隧道"
        "顶缸，看门狗从此不再拉起"
    )
    # 名字是别的命名隧道时也不许命中（前缀不算命中）
    other = "cloudflared.exe tunnel --metrics 127.0.0.1:34568 run %s-other" % name
    assert not re.search(pattern, other), (
        "模式没在隧道名后收口：另一条同名前缀的隧道会被误判成本条活着"
    )


def test_the_watchdog_asks_whether_the_backend_is_complete():
    """后端那条守的是「进程在不在」，而这一轮加的东西全都能在进程活着时坏掉。

    密钥库读不动、账本写不进、嵌入降级成全零伪嵌入、新限流账没登记——四种都没有
    任何进程级症状。这和 2026-09-20 那次 Error 1033 是同一个形状：巡检每分钟全绿，
    外面早就不对了。所以这里两头都要锁：它得去问，而且问到了也不许动手拉起重启。
    """
    code = _code(WATCHDOG.read_text(encoding="utf-8"))

    probe = _region(code, "function Get-BackendSelfCheck", "function Get-Counter", "自检探测函数")
    assert "/health" in probe, "没问 /health，问的还是端口"
    assert "ConvertFrom-Json" in probe, "只看了状态码：后端刻意永远回 200，判据在 checks 里"
    assert "-TimeoutSec" in probe, "没有超时的话，一个卡住的后端会把整轮巡检一起拖住"

    verdict = _region(code, "$health = Get-BackendSelfCheck", "$SkipTunnel", "自检判决那一段")
    assert "Test-ShouldLog" in verdict, "不节流的话，一个坏掉的配置每分钟刷一条 ERROR，真告警就淹了"
    assert "'broken'" in verdict and "'ok'" in verdict, "没有按状态分级，就退化成一句「好像不对」"
    for forbidden in ("Start-Process", "Invoke-Guard"):
        assert forbidden not in verdict, (
            f"自检报 broken 就 {forbidden}：坏的是配置，重启修不好它，"
            f"只会把正在进行的对话每分钟带走一次"
        )


def test_save_state_creates_the_data_dir_before_writing():
    """日志与状态分家之后，Write-Log 只建 logs/，谁负责建 data/？

    新机器上（或 data/ 被移走时）没有任何人建过它，`Set-Content` 会以"找不到路径"
    失败，而 Save-State 的设计是**只留一条 WARN 继续跑**。于是预算与告警节流每轮
    都从空账开始：守护看着在巡检，熔断却永远不会触发——这正是本文件要守的
    "效果没了但不报错"的形状。判据是顺序，不只是存在：目录必须先于 Set-Content。
    """
    code = _code(WATCHDOG.read_text(encoding="utf-8"))
    save = _region(code, "function Save-State", "function Get-LaunchCount", "Save-State")
    assert "if (-not (Test-Path -LiteralPath $DataDir))" in save, (
        "Save-State 没先确认 data/ 存在：状态写不进去只留一条 WARN，重启预算静默失效")
    assert "New-Item -ItemType Directory -Path $DataDir" in save, "没有把 data/ 建出来"
    assert save.index("New-Item") < save.index("Set-Content"), (
        "先写状态再建目录：全新机器上的第一次写照样失败")


def test_current_log_paths_point_at_logs_not_data():
    """日志搬家之后，看门狗里"现在去看哪份日志"的话必须指向 logs\\。

    判据只看剥掉注释后的代码：文件头 2026-09-19 事故叙述里的 `data/backend.log` 是
    历史事实（当时日志确实在 data\\），不能被这条锁逼着改写；而真正会落进
    logs\\watchdog.log 的提示语是字符串，剥不掉。反过来，谁把提示语改回 data\\，
    这里当场红——照着它去翻，只会看到一份不再增长的历史文件。
    """
    code = _code(WATCHDOG.read_text(encoding="utf-8"))
    for stale in (r"data\cloudflared.log", "data/cloudflared.log",
                  r"data\backend.log", "data/backend.log"):
        assert stale not in code, (
            f"现行提示语还在指 {stale}：日志已迁到 logs\\，按它去翻是空的")
    assert r"logs\cloudflared.log" in code, (
        "隧道那条的提示语没指向 logs\\cloudflared.log：日志搬家后它得说清新位置")
