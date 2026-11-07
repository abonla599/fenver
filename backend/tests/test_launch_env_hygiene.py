r"""ticket #295：启动/换包脚本必须先清除环境代理变量，再拉起后端子进程。

事故形状（#294 的运行时镜像）：从一台带着毒 `NO_PROXY` 的会话里跑 watchdog /
swap-package / start-ai-stack，`Start-Process` 把当前进程的环境原样继承给后端子进程；
#294 给生产四处 httpx 客户端关了 trust_env，那是**子进程侧**的防线，父会话里的毒仍然
在。#295 在**启动这一层**再加一道：拉起后端之前先从当前进程环境里删掉
`HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY`，并响亮打印 token（一枚都没命中也要
打 `names=none`，让日志能证明这步执行过）。裁决 R2 走方案 (c)：清环境 + 响亮 token，
**不留** `-KeepProxyEnv` 之类的旁路开关；回滚指引（Show-Rollback 打印的那条手动
Start-Process）也必须把"先清代理"打给照抄指引的人——那是第 5 条落点，单独成类。

这把锁属于"派生名册"一族（惯例见 test_pathmask.py / test_outbound_trust_env.py）：
- 名册 `_LAUNCH_SITES` 与计数钉 `_ROSTER_SIZES` 是**手抄字面量**，判据用正则从真实脚本
  字节现扫，两个方向分开报（在册未扫到 / 扫到未在册 / 机制 MISSING），不许自我派生；
- 计数钉成精确值且 >0：摘掉一处调用，名册 MISSING 与计数钉双红；
- 机器真相探针（纪律抄 test_swap_package_boot_poll_single_process_machine_truth.py）：
  抽脚本**自己的**清环境表达式（`PROXY-SCRUB-HELPER` 标记之间 / bat 的
  `PROXY-SCRUB-BEGIN/END` 块），原样拼进生成的 .ps1 / .cmd（写在 tmp_path，仓库外），
  注入四枚毒变量后真跑，断言变量确实清零、token 确实打出。阳性对照 = 注入本身必须
  让"动手前"数到 4（不注入成功的绿是空转）；反例 = 同样的注入、不调清环境，变量必须
  还在。探针零后端进程启动、零出网、只碰变量名不碰任何值。
- 没有 powershell.exe / cmd.exe 就只跳过机器那几格，文本锁照跑（惯例同上）。

射程声明（按实测，不许超出）：
- 本锁只认"行首即 Start-Process"的后端拉起语句（$BackendExe / $LiveExe / run_backend），
  隧道（$TunnelExe / cloudflared）不在 #295 射程；Show-Rollback 里**打印**的那条
  Start-Process 文本不是执行语句，走 rollback-print 单独一格；
- 注释与整行 `#` 剥掉再扫（bat 的 rem 同）；块注释 `#>` 续行仍是被声明的盲区；
- helper 调用被写进变量赋值（`$x = Remove-ProxyEnvForChild`）仍在调用判据里，
  被改写成字符串反射之类就出射程——那种形状先过 PS 语法这关再说。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHDOG_PS1 = REPO_ROOT / "deploy" / "watchdog.ps1"
SWAP_PS1 = REPO_ROOT / "deploy" / "swap-package.ps1"
STACK_BAT = REPO_ROOT / "tools" / "start-ai-stack.bat"

# 要清掉的四个环境代理变量名（Windows 环境变量大小写不敏感，判据按大小写不敏感认）。
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
_PS_FILTER = "(?i)^(HTTP|HTTPS|ALL|NO)_PROXY$"

_HELPER_BEGIN = "# PROXY-SCRUB-HELPER-BEGIN"
_HELPER_END = "# PROXY-SCRUB-HELPER-END"
_BAT_BEGIN = "rem PROXY-SCRUB-BEGIN"
_BAT_END = "rem PROXY-SCRUB-END"

# --------------------------------------------------------------------------- 名册
# 手抄落点名册：哪些地方"在拉起后端前必须先清代理环境"。加/删落点必须同批改这张表。
_LAUNCH_SITES = {
    ("deploy/watchdog.ps1", "backend-launch"): "ps-helper",
    ("deploy/swap-package.ps1", "swap-main-start"): "ps-helper",
    ("deploy/swap-package.ps1", "restart-old-package"): "ps-helper",
    ("deploy/swap-package.ps1", "rollback-print"): "ps-printed-instruction",
    ("tools/start-ai-stack.bat", "login-autostart"): "bat-block",
}
# 精确计数钉（>0）：清环境部件的枚数，掏空/复制都会在这里红。
_ROSTER_SIZES = (
    ("ps-helper-defs", 2),        # watchdog 与 swap 的 Remove-ProxyEnvForChild 函数定义
    ("ps-helper-calls", 3),       # watchdog 与 swap 里 Remove-ProxyEnvForChild 的调用点
    ("bat-blocks", 1),
    ("rollback-print-lines", 1),
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig", errors="surrogateescape")


def _strip_line_comments(text: str) -> str:
    r"""剥整行 `#` 注释但**保留行数**（行号尺专用，惯例同 test_swap_package_contract）。"""
    return re.sub(r"(?m)^[ \t]*#.*$", "", text)


def _ps_scrub_def_lines(text: str) -> list:
    return [i for i, ln in enumerate(text.splitlines())
            if re.match(r"^\s*function\s+Remove-ProxyEnvForChild\b", ln)]


def _ps_scrub_call_lines(text: str) -> list:
    code = _strip_line_comments(text)
    out = []
    for i, ln in enumerate(code.splitlines()):
        if re.match(r"^\s*function\s+", ln):
            continue
        if re.search(r"\bRemove-ProxyEnvForChild\b", ln):
            out.append(i)
    return out


def _ps_backend_launch_lines(text: str) -> list:
    """行首即 `Start-Process` 且指向后端（$BackendExe / $LiveExe / run_backend）的执行语句。

    Write-Output 开头的**打印**语句（Show-Rollback 的指引文本）故意不算——它是给人抄的，
    不是这条脚本自己执行的；rollback-print 那格另有判据。隧道不在射程。
    """
    code = _strip_line_comments(text)
    out = []
    for i, ln in enumerate(code.splitlines()):
        if not ln.lstrip().startswith("Start-Process"):
            continue
        if not re.search(r"\$BackendExe|\$LiveExe|run_backend", ln):
            continue
        if re.search(r"TunnelExe|cloudflared", ln):
            continue
        out.append(i)
    return out


def _rollback_printed_launch_line(text: str):
    """Show-Rollback 打印的启动指引行：带引号回显的**文本形态** `'$LiveExe'` 不会是可执行
    语句的形状，靠这一点与真正的 Start-Process 区分（:746 时代实测该行不带 ROLLBACK-HOWTO
    字样，指引编号在上一行）。"""
    for i, ln in enumerate(text.splitlines()):
        if re.search(r"Write-Output \(\".*Start-Process -FilePath '\$LiveExe'", ln):
            return i
    return None


def _rollback_scrub_instruction_line(text: str):
    """Show-Rollback 里打印给操作者的"先清代理"命令行（Remove-Item env:…，四个名字齐）。"""
    for i, ln in enumerate(text.splitlines()):
        if re.search(r"Remove-Item\s+env:", ln) \
                and all(re.search(r"env:" + v, ln) for v in PROXY_VARS):
            return i
    return None


def _bat_block_range(text: str):
    m1 = text.find(_BAT_BEGIN)
    m2 = text.find(_BAT_END)
    if m1 == -1 or m2 == -1 or m2 < m1:
        return None
    return (m1, m2)


# ------------------------------------------------------------------ 名册 <-> 现扫
def _derive_scanned():
    """从真实脚本字节现扫，重建 (路径, 落点) -> 机制 的账。机制未接上的标 *-MISSING。"""
    scanned = {}
    wtext = _read(WATCHDOG_PS1)
    rel_w = WATCHDOG_PS1.relative_to(REPO_ROOT).as_posix()
    wlaunches = _ps_backend_launch_lines(wtext)
    wcalls = _ps_scrub_call_lines(wtext)
    if wlaunches:
        ok = all(any(c < ln for c in wcalls) for ln in wlaunches)
        scanned[(rel_w, "backend-launch")] = "ps-helper" if ok else "ps-helper-MISSING"

    stext = _read(SWAP_PS1)
    rel_s = SWAP_PS1.relative_to(REPO_ROOT).as_posix()
    slaunches = sorted(_ps_backend_launch_lines(stext))
    scalls = _ps_scrub_call_lines(stext)
    # 结构归位（惯例同 test_swap_package_disable_verify._restart_if）：较早那条必须落在
    # Stop-AfterFailure 的配平体内（restart 旧包），较晚那条在主流程。前提不成立就报
    # shape-broken，绝不猜。
    sdef = re.search(r"^function\s+Stop-AfterFailure\s*\{", stext, re.M)
    body_range = None
    if sdef:
        lines = stext.splitlines()
        start = stext.count("\n", 0, sdef.start())
        depth = 0
        for i in range(start, len(lines)):
            depth += lines[i].count("{") - lines[i].count("}")
            if depth <= 0 and i > start:
                body_range = (start, i)
                break
    if len(slaunches) == 2 and body_range and body_range[0] < slaunches[0] <= body_range[1]:
        restart_line, main_line = slaunches
        # 每次拉起必须被**自己那条分支**的清理覆盖：restart 要函数体内、它之前的调用；
        # main 要两条拉起之间的调用。只按"文件里更早某处有一个调用"判，摘掉 main 那处
        # 名册不会红（restart 的调用行号更早）——行序判据在这个形状上不够狠。
        restart_ok = any(body_range[0] <= c < restart_line for c in scalls)
        main_ok = any(restart_line < c < main_line for c in scalls)
        scanned[(rel_s, "restart-old-package")] = "ps-helper" if restart_ok else "ps-helper-MISSING"
        scanned[(rel_s, "swap-main-start")] = "ps-helper" if main_ok else "ps-helper-MISSING"
    else:
        scanned[(rel_s, "__shape__")] = "SWAP-LAUNCH-SHAPE-BROKEN(launches=%d)" % len(slaunches)

    rp = _rollback_printed_launch_line(stext)
    instr = _rollback_scrub_instruction_line(stext)
    if rp is not None:
        ok = instr is not None and instr < rp
        scanned[(rel_s, "rollback-print")] = (
            "ps-printed-instruction" if ok else "ps-printed-instruction-MISSING")

    rel_b = STACK_BAT.relative_to(REPO_ROOT).as_posix()
    btext = _read(STACK_BAT)
    rng = _bat_block_range(btext)
    if rng is not None:
        after = rng[1]
        launch_pos = None
        for m in re.finditer(r"(?m)^\s*powershell.*run_backend", btext):
            launch_pos = m.start()
            break
        ok = launch_pos is not None and after < launch_pos
        scanned[(rel_b, "login-autostart")] = "bat-block" if ok else "bat-block-MISSING"
    return scanned


# ------------------------------------------------------------------------- 名册锁
def test_scanned_launch_sites_nonempty_and_match_roster():
    scanned = _derive_scanned()
    assert scanned, "现扫一条落点都没拿到——判据自己坏了，不许绿。"
    broken_shape = {k: v for k, v in scanned.items() if v.startswith(("SWAP", "BAT"))}
    assert not broken_shape, f"落点识别的前提形状被改坏了（判据先红给人看）：{broken_shape}"
    reg_keys = set(_LAUNCH_SITES)
    missing_mechanism = {k: v for k, v in scanned.items() if v.endswith("-MISSING")}
    in_roster_not_scanned = reg_keys - set(scanned.keys())
    scanned_not_in_roster = set(scanned.keys()) - reg_keys
    assert not in_roster_not_scanned, f"在册却没扫到落点：{sorted(in_roster_not_scanned)}"
    assert not missing_mechanism, f"落点在但清环境没接上（MISSING）：{missing_mechanism}"
    assert not scanned_not_in_roster, f"扫到了但未登记（要同批改名册）：{sorted(scanned_not_in_roster)}"


def test_roster_sizes_pinned_exactly():
    wtext = _read(WATCHDOG_PS1)
    stext = _read(SWAP_PS1)
    btext = _read(STACK_BAT)
    got = {
        "ps-helper-defs": len(_ps_scrub_def_lines(wtext)) + len(_ps_scrub_def_lines(stext)),
        "ps-helper-calls": len(_ps_scrub_call_lines(wtext)) + len(_ps_scrub_call_lines(stext)),
        "bat-blocks": 1 if _bat_block_range(btext) is not None else 0,
        "rollback-print-lines": 1 if _rollback_scrub_instruction_line(stext) is not None else 0,
    }
    expect = dict(_ROSTER_SIZES)
    assert got == expect, f"清环境各部件数量钉对不上（摘掉一处就红在这里）：期望 {expect} 实得 {got}"
    assert all(v > 0 for v in expect.values()), "计数钉必须 >0，0 不构成防线。"


def test_helper_defined_before_every_call():
    for path in (WATCHDOG_PS1, SWAP_PS1):
        text = _read(path)
        defs = _ps_scrub_def_lines(text)
        calls = _ps_scrub_call_lines(text)
        assert defs, f"{path.name} 必须先定义 Remove-ProxyEnvForChild 才能调用。"
        for c in calls:
            assert any(d <= c for d in defs), \
                f"{path.name} 第 {c} 行的调用出现在 helper 定义之前（PS 顺序执行找不到函数）。"


def test_two_ps_helper_copies_are_identical():
    """两份 helper 必须逐字节同形（各自 strip 后比较）：一边修了另一边没修，就是下一场事故。"""
    hw = _extract_ps_helper(WATCHDOG_PS1)
    hs = _extract_ps_helper(SWAP_PS1)
    assert hw is not None and hs is not None, "helper 标记块必须两份都在（缺一边先红在这）。"
    assert hw.strip() == hs.strip(), "watchdog 与 swap 的清环境 helper 已经分叉，必须同批改。"


def test_names_copied_literals_not_derived():
    """名册/计数必须是本文件的手抄字面量——把名册赋成扫描/派生结果等于缴械（惯例 #289）。"""
    src = _read(Path(__file__))
    assert re.search(r"_LAUNCH_SITES\s*=\s*\{", src), "名册得是字面 dict。"
    assert re.search(r"_ROSTER_SIZES\s*=\s*\(", src), "计数钉得是字面 tuple。"
    # 反缴械判据：两处名单的赋值 RHS 一旦变成标识符（`_derive...` 之类）就红。
    assert not re.search(r"_LAUNCH_SITES\s*=\s*_[A-Za-z]", src), "名册 RHS 变成了表达式。"
    assert not re.search(r"_ROSTER_SIZES\s*=\s*_[A-Za-z]", src), "计数钉 RHS 变成了表达式。"


# ---------------------------------------------------------------- token / 形状
def test_loud_token_shape_in_helper():
    """helper 必须打印 PROXY-ENV-SCRUBBED token，且没命中时打 names=none（响亮判据）。"""
    for path in (WATCHDOG_PS1, SWAP_PS1):
        text = _read(path)
        m = re.search(r"function\s+Remove-ProxyEnvForChild\s*\{(?P<body>[\s\S]*?)\n\}", text)
        assert m, f"{path.name} 里没解析到 Remove-ProxyEnvForChild 函数体。"
        body = m.group("body")
        assert "PROXY-ENV-SCRUBBED" in body, f"{path.name} helper 没打印响亮 token。"
        assert re.search(r"'none'", body), f"{path.name} 命中 0 枚时不许沉默——必须打 names=none。"


def test_ps_helper_regex_targets_exactly_four_proxy_vars():
    """过滤器只认这四枚代理变量名：不许误伤 PATH/TEMP，也不许漏 NO_PROXY。"""
    for path in (WATCHDOG_PS1, SWAP_PS1):
        text = _read(path)
        m = re.search(r"function\s+Remove-ProxyEnvForChild\s*\{(?P<body>[\s\S]*?)\n\}", text)
        assert m, path.name
        fx = re.search(r"-match\s+'(?P<rx>[^']+)'", m.group("body"))
        assert fx, f"{path.name} helper 里没找到过滤器正则字面量。"
        rx = fx.group("rx")
        for v in PROXY_VARS:
            assert re.match(rx, v), f"{rx} 认不出该被清的 {v}"
        for innocent in ("PATH", "TEMP", "HOME", "NO_PROXYY", "XHTTP_PROXY", "PYTHONPATH",
                         "GOPROXY", "npm_config_proxy"):
            assert not re.match(rx, innocent), f"{rx} 误伤了不该动的 {innocent}"


def test_swap_helper_and_edits_are_pure_ascii():
    """swap-package.ps1 整文件（含 helper 与回滚指引新行）一个 >=0x80 字节都不许有——
    沿用 contract 那把 ASCII 钉的形状门；watchdog 允许 BOM+中文，但 helper 段本身
    要 ASCII（探针把它拼进生成的 .ps1，生成文件是全 ASCII 的）。"""
    raw = SWAP_PS1.read_bytes()
    bad = [(i, b) for i, b in enumerate(raw) if b >= 0x80]
    assert not bad, f"swap-package.ps1 出现非 ASCII 字节（前 3 处）：{bad[:3]}"
    helper = _extract_ps_helper(WATCHDOG_PS1)
    assert helper is not None, "watchdog 没有可抽取的 helper 段"
    assert all(ord(c) < 128 for c in helper), "watchdog 的 helper 段必须 ASCII（探针生成物全 ASCII）。"


def test_bat_scrub_clears_four_and_echoes_token():
    btext = _read(STACK_BAT)
    rng = _bat_block_range(btext)
    assert rng, "start-ai-stack.bat 缺 PROXY-SCRUB-BEGIN/END 标记块。"
    block = btext[rng[0]:rng[1]]
    for v in PROXY_VARS:
        assert re.search(r'set\s+"' + v + r'="', block), f"bat 块没清 {v}"
    assert "PROXY-ENV-SCRUBBED names=none" in block, "bat 全空时没打 names=none（沉默=没执行过）。"
    assert re.search(r"PROXY-ENV-SCRUBBED names=%", block), "bat 命中时没打真实名字 token。"
    assert all(ord(c) < 128 for c in block), "bat 清环境块必须 ASCII——OEM 936 错解码事故见文件头。"


# ------------------------------------------------------------ 机器真相（PS）
def _extract_ps_helper(path: Path):
    """按 BEGIN/END 标记从脚本真实字节里抽出**它自己的** helper 源码。"""
    text = _read(path)
    m = re.search(re.escape(_HELPER_BEGIN) + r"(?P<body>[\s\S]*?)" + re.escape(_HELPER_END), text)
    if not m:
        return None
    return m.group("body")


def _run_ps_file(script_text: str, tmp_path: Path):
    ps = shutil.which("powershell.exe") or shutil.which("powershell")
    if ps is None:
        return None
    f = tmp_path / "probe_scrub.ps1"
    f.write_bytes(script_text.replace("\n", "\r\n").encode("ascii"))
    return subprocess.run(
        [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(f)],
        capture_output=True, text=True, timeout=90)


@pytest.mark.parametrize("path", [WATCHDOG_PS1, SWAP_PS1], ids=["watchdog", "swap"])
def test_machine_truth_ps_scrubber_clears_poison(path, tmp_path):
    """把脚本自己的 helper 拼进生成的 .ps1 真跑：注入 4 枚毒 -> 清 -> 必须剩 0，且 token 响亮。"""
    if (shutil.which("powershell.exe") or shutil.which("powershell")) is None:
        pytest.skip("powershell.exe 不可用，机器那格跳过（文本锁照跑）")
    helper = _extract_ps_helper(path)
    assert helper is not None and "Remove-ProxyEnvForChild" in helper, \
        f"{path.name} 的 PROXY-SCRUB-HELPER 标记块缺失——探针没有可跑的东西"
    assert "Remove-Item" in helper, f"{path.name} helper 里没有删变量的动作"
    probe = (
        "$ErrorActionPreference='Stop'\r\n"
        "$env:HTTP_PROXY='http://127.0.0.1:1'\r\n"
        "$env:HTTPS_PROXY='http://127.0.0.1:1'\r\n"
        "$env:ALL_PROXY='socks5://127.0.0.1:1'\r\n"
        "$env:NO_PROXY='localhost,127.0.0.1,[::1]'\r\n"
        "$before = @(Get-ChildItem env: | Where-Object { $_.Name -match '" + _PS_FILTER + "' })\r\n"
        "Write-Output ('BEFORE=' + $before.Count)\r\n"
        + helper + "\r\n"
        "Remove-ProxyEnvForChild\r\n"
        "$after = @(Get-ChildItem env: | Where-Object { $_.Name -match '" + _PS_FILTER + "' })\r\n"
        "Write-Output ('AFTER=' + $after.Count)\r\n"
    )
    res = _run_ps_file(probe, tmp_path)
    out = (res.stdout or "") + (res.stderr or "")
    assert res.returncode == 0, f"探针跑失败 rc={res.returncode}：{out}"
    before = int(re.search(r"BEFORE=(\d+)", out).group(1))
    after = int(re.search(r"AFTER=(\d+)", out).group(1))
    assert before == 4, f"阳性对照失败：注入就没生效（BEFORE={before}≠4）——下面的绿是空转"
    assert after == 0, f"脚本自己的清环境表达式没把毒清掉：AFTER={after}（应为 0）"
    assert "PROXY-ENV-SCRUBBED names=" in out, f"helper 没打印响亮 token：{out}"


def test_machine_truth_ps_counterexample_no_scrub_leaves_vars(tmp_path):
    """反例：同样注入四枚毒、**不**调清环境，机器必须答"还在"——证明上面那条绿是清环境挣来的。"""
    if (shutil.which("powershell.exe") or shutil.which("powershell")) is None:
        pytest.skip("powershell.exe 不可用")
    probe = (
        "$env:HTTP_PROXY='http://127.0.0.1:1'\r\n"
        "$env:HTTPS_PROXY='http://127.0.0.1:1'\r\n"
        "$env:ALL_PROXY='socks5://127.0.0.1:1'\r\n"
        "$env:NO_PROXY='localhost,127.0.0.1,[::1]'\r\n"
        "$after = @(Get-ChildItem env: | Where-Object { $_.Name -match '" + _PS_FILTER + "' })\r\n"
        "Write-Output ('AFTER=' + $after.Count)\r\n"
    )
    res = _run_ps_file(probe, tmp_path)
    after = int(re.search(r"AFTER=(\d+)", res.stdout).group(1))
    assert after == 4, f"反例坏了：不清环境竟也答 {after}（应 4），上面那条绿就没有牙"


# ------------------------------------------------------------ 机器真相（BAT）
def _run_cmd_file(script_text: str, tmp_path: Path):
    if shutil.which("cmd.exe") is None and not Path(r"Windows\System32\cmd.exe").exists():
        return None
    f = tmp_path / "probe_scrub.cmd"
    f.write_bytes(script_text.replace("\n", "\r\n").encode("ascii"))
    return subprocess.run(["cmd", "/c", str(f)], capture_output=True, text=True, timeout=90)


_BAT_CHECK_TAIL = (
    "set AFTER=CLEAN\r\n"
    "if defined HTTP_PROXY set AFTER=STILL_HTTP\r\n"
    "if defined HTTPS_PROXY set AFTER=STILL_HTTPS\r\n"
    "if defined ALL_PROXY set AFTER=STILL_ALL\r\n"
    "if defined NO_PROXY set AFTER=STILL_NO\r\n"
    "echo PROBE_STATE=%AFTER%\r\n"
    "set | findstr /I /R \"^HTTP_PROXY= ^HTTPS_PROXY= ^ALL_PROXY= ^NO_PROXY=\"\r\n"
    "echo FINDSTR_RC=%ERRORLEVEL%\r\n"
)
_BAT_POISON = (
    "@echo off\r\n"
    "set HTTP_PROXY=http://127.0.0.1:1\r\n"
    "set HTTPS_PROXY=http://127.0.0.1:1\r\n"
    "set ALL_PROXY=socks5://127.0.0.1:1\r\n"
    "set NO_PROXY=localhost,127.0.0.1\r\n"
)


def test_machine_truth_bat_scrub_clears_poison(tmp_path):
    rng = _bat_block_range(_read(STACK_BAT))
    assert rng, "bat 标记块缺失——探针没有可跑的东西"
    block = _read(STACK_BAT)[rng[0]:rng[1]]
    assert all(ord(c) < 128 for c in _BAT_POISON + block + _BAT_CHECK_TAIL)
    res = _run_cmd_file(_BAT_POISON + block.replace("\r\n", "\n").replace("\n", "\r\n")
                        + "\r\n" + _BAT_CHECK_TAIL, tmp_path)
    if res is None:
        pytest.skip("cmd.exe 不可用")
    out = (res.stdout or "") + (res.stderr or "")
    assert "PROXY-ENV-SCRUBBED names=" in out, f"bat 块没打出响亮 token：{out}"
    assert "names=none" not in out, f"注入了 4 枚却报 names=none——累加器坏了：{out}"
    assert "PROBE_STATE=STILL_" not in out, f"清环境后仍有变量存活：{out}"
    assert "PROBE_STATE=CLEAN" in out, f"探针自己没跑到判定行：{out}"
    assert "FINDSTR_RC=1" in out, f"set|findstr 还能看到代理变量（rc 不是 1）：{out}"


def test_machine_truth_bat_counterexample_no_scrub_leaves_vars(tmp_path):
    """反例：同样注入、**不**跑清环境块，四枚变量必须还在（AFTER 不是 CLEAN、findstr 命中）。"""
    res = _run_cmd_file(_BAT_POISON + _BAT_CHECK_TAIL, tmp_path)
    if res is None:
        pytest.skip("cmd.exe 不可用")
    out = res.stdout or ""
    assert "PROBE_STATE=STILL_" in out, f"反例坏了：不跑清环境块变量就不在了？{out}"
    assert "FINDSTR_RC=0" in out, f"findstr 抓不到注入的变量，正面那格的 rc=1 就没有意义：{out}"
