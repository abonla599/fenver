r"""ticket #295：启动/换包脚本必须先清除环境代理变量，再拉起后端子进程。

事故形状（#294 的运行时镜像）：从一台带着毒 `NO_PROXY` 的会话里跑 watchdog /
swap-package / start-ai-stack，`Start-Process` 把当前进程的环境原样继承给后端子进程；
#294 给生产四处 httpx 客户端关了 trust_env，那是**子进程侧**的防线，父会话里的毒仍然
在。#295 在**启动这一层**再加一道：拉起后端之前先从当前进程环境里删掉
`HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY`，并响亮打印 token（一枚都没命中也要
打 `names=none`，让日志能证明这步执行过）。名单另含 `SSL_CERT_FILE / SSL_CERT_DIR`：
这两个名字是 OpenSSL 自己读的验证位置环境变量（`ssl.get_default_verify_paths()` 的
`openssl_cafile_env` / `openssl_capath_env`），继承它们等于让父会话替子进程追加信任锚。
裁决 R2 走方案 (c)：清环境 + 响亮 token，
**不留** `-KeepProxyEnv` 之类的旁路开关；回滚指引（Show-Rollback 打印的那条手动
Start-Process）也必须把"先清代理"打给照抄指引的人——那是第 5 条落点，单独成类。

这把锁属于"派生名册"一族（惯例见 test_pathmask.py / test_outbound_trust_env.py）：
- 名册 `_LAUNCH_SITES` 与计数钉 `_ROSTER_SIZES` 是**手抄字面量**（"是不是字面量"由
  `_literal_roster_problems` 走 AST 判赋值节点，正则看 RHS 开头挡不住括号包裹的派生），
  判据用正则从真实脚本字节现扫，两个方向分开报（在册未扫到 / 扫到未在册 / 机制 MISSING），
  不许自我派生；
- 计数钉成精确值且 >0：摘掉一处调用，名册 MISSING 与计数钉双红；
- 机器真相探针（纪律抄 test_swap_package_boot_poll_single_process_machine_truth.py）：
  抽脚本**自己的**清环境表达式（`PROXY-SCRUB-HELPER` 标记之间 / bat 的
  `PROXY-SCRUB-BEGIN/END` 块），原样拼进生成的 .ps1 / .cmd（写在 tmp_path，仓库外），
  注入六枚毒变量后真跑，断言变量确实清零、token 确实打出。阳性对照 = 注入本身必须
  让"动手前"数到 6（不注入成功的绿是空转）；反例 = 同样的注入、不调清环境，变量必须
  还在。探针零后端进程启动、零出网、只碰变量名不碰任何值；两枚 TLS 名注入的是不存在
  的假路径，探针不加载 TLS，那些值不会被任何库读到。
- 没有 powershell.exe / cmd.exe 就只跳过机器那几格，文本锁照跑（惯例同上）。

射程声明（按实测，不许超出）：
- 本锁只认"行首即 Start-Process"的后端拉起语句（$BackendExe / $LiveExe / run_backend），
  隧道（$TunnelExe / cloudflared）不在 #295 射程；Show-Rollback 里**打印**的那条
  Start-Process 文本不是执行语句，走 rollback-print 单独一格；
- 注释与整行 `#` 剥掉再扫（bat 的 rem 同）；块注释 `#>` 续行仍是被声明的盲区；
- helper 调用被写进变量赋值（`$x = Remove-ProxyEnvForChild`）仍在调用判据里，
  被改写成字符串反射之类就出射程——那种形状先过 PS 语法这关再说。

仍漏清单（名单只关闭"子进程启动时的环境继承"这一条路，下面三种它管不到）：
- `.env`：`paths.load_project_env()` 用 `load_dotenv(override=False)` 把值灌进
  `os.environ`，调用点在 `providers.py` / `memory_manager.py` 的 **import 期**——
  那发生在进程内部，启动前删环境删不到它；
- 操作者手工启动（自己 `python run_backend.py`、双击 exe）：没有父脚本，也就没有这一步；
- 隧道（`cloudflared.exe`）不在射程，沿用上面那条声明，不扩大也不下结论。

`SSL_CERT_DIR` 的判据口径（已实测，不许换回根数）：把它指向 `<subject_hash>.0` 形式的
哈希目录，验链从 CERTIFICATE_VERIFY_FAILED 变成成功，而 `len(ctx.get_ca_certs())`
**不变**——OpenSSL 的 hash-dir 查找是惰性的，只在链构建时按 hash 去取文件，证书不进
store 的对象列表，所以"根数上涨"这个量具对 capath 天生是盲的。真握手探针要 openssl
二进制 + 自签 CA + socketpair，在 CI 上必然 skip，因此不移植进本套件；这里承担回归的是
文本层名单锁 + 扩到六名的机器环境探针。
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

# 四个**逻辑**代理变量名。2026-11-09 起落点必须把每个名字的大小写两版都列进名单：
# 宿主可以把 http_proxy 与 HTTP_PROXY 当成两条独立条目交给子进程，而 `if defined` /
# `set "NAME="` 都只认一个大小写不敏感的"名字"，删掉一条、另一条照样往下传。
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
_PS_FILTER = "(?i)^(HTTP|HTTPS|ALL|NO)_PROXY$"

# 两枚 TLS 信任集变量。OpenSSL 自己读这两个名字（ssl.get_default_verify_paths() 的
# openssl_cafile_env / openssl_capath_env）：SSL_CERT_FILE 往系统根库上**追加**锚，
# SSL_CERT_DIR 要 <subject_hash>.0 哈希目录形式才生效。手抄字面量，不许从脚本或
# PROXY_VARS 派生（test_names_copied_literals_not_derived 盯着这一条）。
_TRUST_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR")
_TRUST_FILTER = "(?i)^SSL_CERT_(FILE|DIR)$"
# 名单里两版大小写都要在（理由同代理那八枚：宿主可以把同名两条交给子进程）。
_TRUST_ROSTER = ("SSL_CERT_FILE", "SSL_CERT_DIR", "ssl_cert_file", "ssl_cert_dir")
# 探针注入值：不存在的那个驱动器上的假路径，全 ASCII（生成物要 .encode("ascii")）。
# 探针只跑 powershell/cmd、不加载 TLS、不出网，所以这两个值不会被任何库读到。
_TRUST_POISON = (("SSL_CERT_FILE", r"X:\probe-trust\ca.pem"),
                 ("SSL_CERT_DIR", r"X:\probe-trust\certs"))

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
    """打印给操作者的"先清环境"命令行（Remove-Item env:…，六枚名字齐）。

    这一行是给人照抄的、脚本自己不执行：漏一枚名字，照抄的人就把那枚带进旧包，
    所以判据要求代理四枚与 TLS 两枚同时在场（反例见
    test_rollback_instruction_lock_can_go_red）。名字带右边界，口径与 _TRUST_FILTER
    一致：`env:NO_PROXYY` / `env:SSL_CERT_FILE_BAK` 是**别的**变量名，清不到真正那枚。

    同一把尺子也量 docs/安装部署指南.md 的手动换包配方（锁在 test_paths.py）：
    两处都是抄给人看的名单，判据只留一份。
    """
    for i, ln in enumerate(text.splitlines()):
        if re.search(r"Remove-Item\s+env:", ln) \
                and all(re.search(r"env:" + v + r"(?![A-Za-z0-9_])", ln)
                        for v in PROXY_VARS + _TRUST_VARS):
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


def test_rollback_instruction_lock_can_go_red():
    """回滚指引那行的变异自证（纯文本，CI 上也有牙）：只列四枚代理名 ⇒ 判据必须认不出它。

    那一行是打印给操作者照抄的、脚本自己不执行：判据若只要求代理四枚，TLS 两枚被摘掉
    时它照样绿，第四处落点就成了"改了没人守"。正控 = 六枚齐的那行 + 真脚本那一行。
    超串那几格钉的是右边界：名字被换成 `…_BAK` / `…X` 时清的是另一个变量，不许算在场。
    """
    def _line(names):
        return ("Remove-Item " + ", ".join("env:" + v for v in names)
                + " -ErrorAction SilentlyContinue\n")

    assert _rollback_scrub_instruction_line(_line(PROXY_VARS)) is None, \
        "只有四枚代理名时判据竟认了这一行——TLS 两枚被摘掉它不会红"
    for missing in _TRUST_VARS:
        names = tuple(v for v in PROXY_VARS + _TRUST_VARS if v != missing)
        assert _rollback_scrub_instruction_line(_line(names)) is None, \
            f"缺 {missing} 时判据竟认了这一行（逐枚都要有牙）"
    assert _rollback_scrub_instruction_line(_line(PROXY_VARS + _TRUST_VARS)) == 0, \
        "六枚齐时判据必须认出这一行（正控）"

    # 超串不算在场：逐枚把它换成"同名前缀的另一个变量"，判据都必须认不出这一行。
    # 没有右边界时 `env:NO_PROXYY` 会被当成 `env:NO_PROXY` 在场，六枚全换也照样绿。
    for suffix in ("_BAK", "X"):
        for victim in PROXY_VARS + _TRUST_VARS:
            names = tuple(v + suffix if v == victim else v
                          for v in PROXY_VARS + _TRUST_VARS)
            assert _rollback_scrub_instruction_line(_line(names)) is None, \
                f"{victim} 被换成超串 {victim + suffix} 时判据竟认了这一行（右边界没了）"
    all_super = tuple(v + "_BAK" for v in PROXY_VARS + _TRUST_VARS)
    assert _rollback_scrub_instruction_line(_line(all_super)) is None, \
        "六枚全是超串时判据竟认了这一行——它一个真名字都没要求"

    stext = _read(SWAP_PS1)
    instr = _rollback_scrub_instruction_line(stext)
    rp = _rollback_printed_launch_line(stext)
    assert instr is not None and rp is not None and instr < rp, \
        f"真脚本的清理指引行缺失、或排在启动指引之后（照抄的人会先起进程）：instr={instr} start={rp}"


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


def _literal_roster_problems(source: str) -> list:
    """名册/计数钉必须是模块顶层的**字面量**赋值（AST 判节点类型，不是正则看 RHS 开头）。

    正则那版只要求 RHS 以 `(` / `{` 开头、且不以 `_标识符` 开头：把派生表达式用括号
    包一层就同时满足两条，连"从脚本字节自派生"（#289 明令禁的形状）都照样绿。
    AST 直接看节点：推导式 / 生成器 / 星号展开 / 调用一律不是字面量。
    纯函数吃源码字符串，所以能喂合成坏源码自证有牙（口径同 test_providers.py 的
    信任锚反向锁）。找不到赋值、或同名赋值不止一次，都 fail-closed 先红给人看。

    射程：只钉"是不是字面量"，不钉"字面量内容与脚本一致"——后者由现扫名册锁与
    计数钉两个方向分开判。仍漏：import 之后用 `globals()[…] = …` / 动态执行改写这
    四个名字，模块顶层的 Assign 里看不见。
    """
    import ast

    wanted = {"_LAUNCH_SITES": ast.Dict, "_ROSTER_SIZES": ast.Tuple,
              "_TRUST_VARS": ast.Tuple, "_TRUST_ROSTER": ast.Tuple}
    string_tuples = ("_TRUST_VARS", "_TRUST_ROSTER")

    def literal(node):
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, (ast.Tuple, ast.List)):
            return all(literal(e) for e in node.elts)
        if isinstance(node, ast.Dict):
            return all(k is not None and literal(k) and literal(v)
                       for k, v in zip(node.keys, node.values))
        return False

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"本文件解析不了，判据无从下手：{exc}"]
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in wanted:
                    found.setdefault(t.id, []).append(node)
    problems = []
    for name, kind in wanted.items():
        nodes = found.get(name, [])
        if len(nodes) != 1:
            problems.append(f"{name} 在模块顶层不是恰好一次赋值（实得 {len(nodes)} 次）")
            continue
        value = nodes[0].value
        if not isinstance(value, kind):
            problems.append(f"{name} 不是字面 {kind.__name__}：RHS 是派生表达式")
        elif not literal(value):
            problems.append(f"{name} 含非字面量元素：名单被派生出来了")
        elif name in string_tuples and not all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts):
            problems.append(f"{name} 的元素必须全是字符串字面量")
    return problems


def test_names_copied_literals_not_derived():
    """名册/计数必须是本文件的手抄字面量——把名册赋成扫描/派生结果等于缴械（惯例 #289）。

    两族名字是两份独立的手抄事实：TLS 名单不许从 PROXY_VARS 派生，也不许从脚本派生。
    反例见 test_literal_roster_lock_can_go_red。
    """
    problems = _literal_roster_problems(_read(Path(__file__)))
    assert not problems, "名册/计数钉不再是手抄字面量（派生 = 缴械）：" + "; ".join(problems)


def _roster_assignment_segment(src: str, name: str):
    """按 AST 取某份名单的赋值原文：判据的反例不必手抄第二份本文件文本（抄的会腐烂）。"""
    import ast

    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.get_source_segment(src, node)
    return None


# 每格都是"把名单改成派生"的缴械形状；第一格是括号包裹的自派生——正则那版对它全绿。
_LITERAL_ROSTER_MUTANTS = (
    ("trust-vars-self-derived-in-parens", "_TRUST_VARS",
     '_TRUST_VARS = (tuple(dict.fromkeys(re.findall('
     'r"\'(SSL_CERT_FILE|SSL_CERT_DIR)\'", SWAP_PS1.read_text(encoding="utf-8-sig")))))'),
    ("trust-roster-starred-from-trust-vars", "_TRUST_ROSTER",
     "_TRUST_ROSTER = (*_TRUST_VARS, *(v.lower() for v in _TRUST_VARS))"),
    ("trust-vars-from-proxy-vars", "_TRUST_VARS", "_TRUST_VARS = PROXY_VARS"),
    ("launch-sites-comprehension", "_LAUNCH_SITES", "_LAUNCH_SITES = {k: v for k, v in ()}"),
    ("roster-sizes-generator", "_ROSTER_SIZES",
     '_ROSTER_SIZES = (tuple((k, v) for k, v in (("ps-helper-defs", 2),)))'),
)


@pytest.mark.parametrize("label,name,replacement", _LITERAL_ROSTER_MUTANTS,
                         ids=[m[0] for m in _LITERAL_ROSTER_MUTANTS])
def test_literal_roster_lock_can_go_red(label, name, replacement):
    """变异自证：任一份名单被改成派生表达式，同一条判据必须红。正控是本文件自己。"""
    src = _read(Path(__file__))
    assert not _literal_roster_problems(src), "正控先绿：本文件自己就该过判据"
    seg = _roster_assignment_segment(src, name)
    assert seg is not None and src.count(seg) == 1, \
        f"变异前提：{name} 的赋值原文在本文件里应恰好一处，实得 {seg!r}"
    problems = _literal_roster_problems(src.replace(seg, replacement))
    assert problems, f"[{label}] 名单被改成派生形状，判据竟不红"


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


def _ps_trust_problems(helper: str) -> list:
    """helper 的 TLS 名单判据本体：四枚字面量（两名 × 大小写两版）+ 第二条过滤器，
    且那条过滤器必须排在代理过滤器**后面**。

    抽成纯函数是为了能喂合成 helper 自证有牙：CI 是 ubuntu，四格机器探针在那边 skip，
    文本层这一格就是这条修复在 CI 上唯一的牙。
    """
    problems = []
    for name in _TRUST_ROSTER:
        if "'" + name + "'" not in helper:
            problems.append(f"固定名单缺 {name}")
    if _TRUST_FILTER not in helper:
        problems.append(f"缺 TLS 过滤器字面量 {_TRUST_FILTER}")
    proxy_at = helper.find(_PS_FILTER)
    trust_at = helper.find(_TRUST_FILTER)
    if proxy_at == -1:
        problems.append("代理过滤器不在了：它必须留在前面（第一个 -match 字面量是它的形状）")
    elif trust_at != -1 and trust_at < proxy_at:
        problems.append("TLS 过滤器排在代理过滤器前面：第一个 -match 字面量会被读成 TLS 那条")
    return problems


def test_ps_helper_roster_and_second_filter_cover_the_trust_vars():
    """两份 helper 都必须把两枚 TLS 名（大小写两版）列进固定名单，并追加第二条过滤器。

    只改一份 = 下一场事故（逐字同形那把锁另有格）；只列大写 = 宿主给的小写那条照样
    往下传。代理那条过滤器不许被替换掉，也不许被挤到后面去。
    """
    for path in (WATCHDOG_PS1, SWAP_PS1):
        helper = _extract_ps_helper(path)
        assert helper, f"{path.name} 没有可抽取的 helper"
        problems = _ps_trust_problems(helper)
        assert not problems, f"{path.name} 的 TLS 信任集名单不全：{problems}"


def test_ps_trust_filter_targets_exactly_the_two_openssl_vars():
    """第二条过滤器只认这两枚名字（大小写两版），不许误伤别的 CA 相关变量或 PATH。"""
    for v in _TRUST_VARS:
        for name in (v, v.lower()):
            assert re.match(_TRUST_FILTER, name), f"{_TRUST_FILTER} 认不出该被清的 {name}"
    for innocent in ("PATH", "TEMP", "HOME", "HTTP_PROXY", "SSL_CERT_FILEX", "XSSL_CERT_FILE",
                     "SSL_CERT_DIRTY", "SSL_CERT_FILE_BAK", "REQUESTS_CA_BUNDLE",
                     "CURL_CA_BUNDLE", "PYTHONPATH"):
        assert not re.match(_TRUST_FILTER, innocent), f"{_TRUST_FILTER} 误伤了不该动的 {innocent}"


def test_ps_trust_lock_can_go_red():
    """变异自证（纯文本、不跑机器）：摘掉第二条过滤器 / 名单只留大写 / 把它挪到代理前面，
    同一条判据必须分别报红。正控是真 helper 自己。

    顺序那一格同时证明 R3 的陷阱是真的：挪到前面之后，helper 里**第一个** `-match '…'`
    字面量就变成了 TLS 那条，代理过滤器那把锁会跟着红。
    """
    helper = _extract_ps_helper(SWAP_PS1)
    assert helper is not None, "正控的前提：swap 有可抽取的 helper"
    assert not _ps_trust_problems(helper), f"正控先绿：真 helper 自己就该过判据 {_ps_trust_problems(helper)}"

    lines = helper.splitlines(keepends=True)
    trust_lines = [ln for ln in lines if _TRUST_FILTER in ln]
    assert len(trust_lines) == 1, f"变异前提：真 helper 里应恰好一条 TLS 过滤器，实得 {len(trust_lines)}"

    dropped = "".join(ln for ln in lines if _TRUST_FILTER not in ln)
    assert any("缺 TLS 过滤器" in p for p in _ps_trust_problems(dropped)), \
        f"摘掉第二条过滤器判据竟不红：{_ps_trust_problems(dropped)}"

    upper_only = helper.replace(", 'ssl_cert_file', 'ssl_cert_dir'", "")
    assert upper_only != helper, "变异没生效：名单里找不到小写那两枚（形状变了，先修判据）"
    problems = _ps_trust_problems(upper_only)
    assert problems and all("ssl_cert_" in p for p in problems), \
        f"名单只留大写两枚时判据应只报小写那两枚缺失，实得 {problems}"

    rest = [ln for ln in lines if _TRUST_FILTER not in ln]
    proxy_idx = next(i for i, ln in enumerate(rest) if _PS_FILTER in ln)
    reversed_order = "".join(rest[:proxy_idx] + trust_lines + rest[proxy_idx:])
    problems = _ps_trust_problems(reversed_order)
    assert any("排在代理过滤器前面" in p for p in problems), \
        f"把 TLS 过滤器挪到前面判据竟不红：{problems}"
    first_rx = re.search(r"-match\s+'(?P<rx>[^']+)'", reversed_order)
    assert first_rx and first_rx.group("rx") == _TRUST_FILTER, \
        "顺序陷阱的前提变了：挪到前面后第一个 -match 字面量竟不是 TLS 那条"


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


def _bat_roster_problems(block: str) -> list:
    """bat 清环境块的名单判据本体：十二枚名字（六变量 × 大小写两版）都要有对应的删除语句。

    抽出来是为了能自证有牙——喂一段只有大写四枚代理名的合成块，它必须报出
    「代理小写四枚 + TLS 两版四枚 + 行数」共九条问题（见 test_bat_roster_lock_can_go_red）。
    """
    problems = []
    code = re.sub(r"(?mi)^\s*rem\b.*$", "", block)  # 注释里也会写到 `if defined`，只数真语句
    for v in PROXY_VARS + _TRUST_VARS:
        if not re.search(r'set\s+"' + v + r'="', block):
            problems.append(f"没清 {v}")
        if not re.search(r'set\s+"' + v.lower() + r'="', block):
            problems.append(f"没清小写那版 {v.lower()}：同名两条会留下一条带进子进程")
    want = 2 * (len(PROXY_VARS) + len(_TRUST_VARS))
    if code.count("if defined ") != want:
        problems.append(f"if defined 的行数不是 {want} 行：名单被收窄了（只列一版就会少一半）")
    return problems


def test_bat_scrub_clears_both_cases_and_echoes_token():
    """bat 块必须把十二枚名字（六个变量 × 大小写两版）都列出来，不许只列大写。

    2026-11-09 实测：同一个工具宿主把 http_proxy / HTTP_PROXY 与 https_proxy /
    HTTPS_PROXY 当成**四条独立条目**交给子进程。`if defined` 大小写不敏感（看得到
    任意一版），但 `set "NAME="` 只删掉其中一条，于是小写那条带着死代理进到
    run_backend。list 两版是把"只删一条"堵死的唯一写法；cmd 侧造不出同名两条，
    所以这条只能由名单本身钉住（机器探针那格只判"清的动作是真的"）。
    """
    btext = _read(STACK_BAT)
    rng = _bat_block_range(btext)
    assert rng, "start-ai-stack.bat 缺 PROXY-SCRUB-BEGIN/END 标记块。"
    block = btext[rng[0]:rng[1]]
    assert not _bat_roster_problems(block), \
        "bat 清环境块名单不全：%s" % _bat_roster_problems(block)
    assert "PROXY-ENV-SCRUBBED names=none" in block, "bat 全空时没打 names=none（沉默=没执行过）。"
    assert re.search(r"PROXY-ENV-SCRUBBED names=%", block), "bat 命中时没打真实名字 token。"
    assert all(ord(c) < 128 for c in block), "bat 清环境块必须 ASCII——OEM 936 错解码事故见文件头。"


def test_bat_roster_lock_can_go_red():
    """变异自证：把名单砍回事故前那四行大写，同一条判据必须报红。

    没有这一格，上面那条绿可能只是"正则永远匹配不上"的空转；有这一格，判据的射程
    就钉在"两版都得列"上。正控是同一段文本补回十二行后立刻转绿。
    中间那一格（代理八行齐、TLS 四条缺）证明判据分得清缺的是哪一族，而不是只数行数。
    """
    four = "".join(
        'if defined ' + v + ' (set "_PSCRUB=%_PSCRUB%' + v + '," & set "' + v + '=")\r\n'
        for v in PROXY_VARS)
    problems = _bat_roster_problems(four)
    assert len(problems) == len(PROXY_VARS) + 1 + 2 * len(_TRUST_VARS), \
        f"砍回四行大写时判据只报了 {problems}——它认不出名单被收窄"
    assert any("if defined 的行数" in p for p in problems), problems

    eight = "".join(
        'if defined ' + n + ' (set "_PSCRUB=%_PSCRUB%' + n + '," & set "' + n + '=")\r\n'
        for n in _FIXED_ROSTER)
    problems = _bat_roster_problems(eight)
    name_problems = [p for p in problems if "if defined 的行数" not in p]
    assert len(name_problems) == 2 * len(_TRUST_VARS), \
        f"代理八行齐、TLS 四条缺时，名字问题应恰好是 TLS 那四条，实得 {name_problems}"
    assert all(any(t in p for t in _TRUST_ROSTER) for p in name_problems), name_problems
    assert not any(any(v in p for v in _FIXED_ROSTER) for p in name_problems), \
        f"代理八行齐却报出了代理族的问题（两族分不清）：{name_problems}"
    # 行数钉同时在场：8 != 12。它是另一条判据，不许拿它冒充"名字族分得清"。
    assert any("if defined 的行数" in p for p in problems), problems

    twelve = "".join(
        'if defined ' + n + ' (set "_PSCRUB=%_PSCRUB%' + n + '," & set "' + n + '=")\r\n'
        for n in _FIXED_ROSTER + _TRUST_ROSTER)
    assert not _bat_roster_problems(twelve), \
        f"补回十二行后判据仍报红：{_bat_roster_problems(twelve)}"


# ------------------------------------------------------------ 机器真相（PS）
def _extract_ps_helper(path: Path):
    """按 BEGIN/END 标记从脚本真实字节里抽出**它自己的** helper 源码。"""
    text = _read(path)
    m = re.search(re.escape(_HELPER_BEGIN) + r"(?P<body>[\s\S]*?)" + re.escape(_HELPER_END), text)
    if not m:
        return None
    return m.group("body")


def _assert_probe_is_duplicate_safe(probe: str) -> None:
    """探针生成物自己也不许用 `Get-ChildItem env:` 数条目。

    2026-11-09 实测：宿主同时给了 http_proxy 与 HTTP_PROXY 两条独立条目时，这个
    cmdlet 本身就抛「已添加了具有相同键的项」，探针先死在一句与被测脚本无关的异常
    上——那种红会被读成"脚本坏了"。取数一律逐名 GetEnvironmentVariable。
    """
    assert "Get-ChildItem env:" not in probe, \
        "探针又用 env: drive 枚举了：同名两条会让它先炸，红的原因就不是被测代码"
    assert "GetEnvironmentVariable($n, 'Process')" in probe, \
        "探针没有逐名取值：大小写两版是否都清掉就判不出来"


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
    """把脚本自己的 helper 拼进生成的 .ps1 真跑：注入 6 枚毒 -> 清 -> 必须剩 0，且 token 响亮。

    取数一律**逐名 `[Environment]::GetEnvironmentVariable`**，不许用 `Get-ChildItem env:`
    数条目——宿主同时注入 `http_proxy` 与 `HTTP_PROXY` 时，枚举那本字典自己就抛
    「已添加了具有相同键的项」（2026-11-09 换包事故的同一个异常），探针会替被测对象先
    死在这儿，报出来的红与被测代码无关。按名字取值是大小写不敏感的：任一大小写变体还
    活着都会数到它，所以 `AFTER=0` 同时证明了大小写两版都清干净了。
    """
    if (shutil.which("powershell.exe") or shutil.which("powershell")) is None:
        pytest.skip("powershell.exe 不可用，机器那格跳过（文本锁照跑）")
    helper = _extract_ps_helper(path)
    assert helper is not None and "Remove-ProxyEnvForChild" in helper, \
        f"{path.name} 的 PROXY-SCRUB-HELPER 标记块缺失——探针没有可跑的东西"
    assert "Remove-Item" in helper, f"{path.name} helper 里没有删变量的动作"
    probe = (
        "$ErrorActionPreference='Stop'\r\n"
        "$CheckNames = @('" + "','".join(PROXY_VARS + _TRUST_VARS) + "')\r\n"
        "$env:HTTP_PROXY='http://127.0.0.1:1'\r\n"
        "$env:HTTPS_PROXY='http://127.0.0.1:1'\r\n"
        "$env:ALL_PROXY='socks5://127.0.0.1:1'\r\n"
        "$env:NO_PROXY='localhost,127.0.0.1,[::1]'\r\n"
        + "".join("$env:" + n + "='" + v + "'\r\n" for n, v in _TRUST_POISON) +
        "$before = 0\r\n"
        "foreach ($n in $CheckNames) { if ($null -ne [Environment]::GetEnvironmentVariable($n, 'Process')) { $before++ } }\r\n"
        "Write-Output ('BEFORE=' + $before)\r\n"
        + helper + "\r\n"
        "Remove-ProxyEnvForChild\r\n"
        "$after = 0\r\n"
        "foreach ($n in $CheckNames) { if ($null -ne [Environment]::GetEnvironmentVariable($n, 'Process')) { $after++ } }\r\n"
        "Write-Output ('AFTER=' + $after)\r\n"
    )
    _assert_probe_is_duplicate_safe(probe)
    res = _run_ps_file(probe, tmp_path)
    out = (res.stdout or "") + (res.stderr or "")
    assert res.returncode == 0, f"探针跑失败 rc={res.returncode}：{out}"
    before = int(re.search(r"BEFORE=(\d+)", out).group(1))
    after = int(re.search(r"AFTER=(\d+)", out).group(1))
    assert before == 6, f"阳性对照失败：注入就没生效（BEFORE={before}≠6）——下面的绿是空转"
    assert after == 0, f"脚本自己的清环境表达式没把毒清掉：AFTER={after}（应为 0）"
    assert "PROXY-ENV-SCRUBBED names=" in out, f"helper 没打印响亮 token：{out}"


def test_machine_truth_ps_counterexample_no_scrub_leaves_vars(tmp_path):
    """反例：同样注入六枚毒、**不**调清环境，机器必须答"还在"——证明上面那条绿是清环境挣来的。"""
    if (shutil.which("powershell.exe") or shutil.which("powershell")) is None:
        pytest.skip("powershell.exe 不可用")
    probe = (
        "$CheckNames = @('" + "','".join(PROXY_VARS + _TRUST_VARS) + "')\r\n"
        "$env:HTTP_PROXY='http://127.0.0.1:1'\r\n"
        "$env:HTTPS_PROXY='http://127.0.0.1:1'\r\n"
        "$env:ALL_PROXY='socks5://127.0.0.1:1'\r\n"
        "$env:NO_PROXY='localhost,127.0.0.1,[::1]'\r\n"
        + "".join("$env:" + n + "='" + v + "'\r\n" for n, v in _TRUST_POISON) +
        "$after = 0\r\n"
        "foreach ($n in $CheckNames) { if ($null -ne [Environment]::GetEnvironmentVariable($n, 'Process')) { $after++ } }\r\n"
        "Write-Output ('AFTER=' + $after)\r\n"
    )
    _assert_probe_is_duplicate_safe(probe)
    res = _run_ps_file(probe, tmp_path)
    after = int(re.search(r"AFTER=(\d+)", res.stdout).group(1))
    assert after == 6, f"反例坏了：不清环境竟也答 {after}（应 6），上面那条绿就没有牙"


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
    "if defined SSL_CERT_FILE set AFTER=STILL_SSLFILE\r\n"
    "if defined SSL_CERT_DIR set AFTER=STILL_SSLDIR\r\n"
    "echo PROBE_STATE=%AFTER%\r\n"
    "set | findstr /I /R \"^HTTP_PROXY= ^HTTPS_PROXY= ^ALL_PROXY= ^NO_PROXY= "
    "^SSL_CERT_FILE= ^SSL_CERT_DIR=\"\r\n"
    "echo FINDSTR_RC=%ERRORLEVEL%\r\n"
)
# 注入前先把十二枚名字（大小写两版）清空：宿主可能替这个进程备好了 http_proxy 与
# HTTP_PROXY 两条同名不同大小的条目，那样 BEFORE 就不是探针注入的那六枚，探针本身
# 变成一次靠宿主运气的测量。归一化之后，"六枚注入 -> 清零 -> CLEAN" 才是这台机器上
# 可复现的结论。注：cmd 的 `set` 是大小写不敏感的，它**造不出**同名两条这种形状，
# 所以"两版都得清"这条只能由文本锁钉名单来判，机器探针判的是清的动作为真。
_BAT_NORMALIZE = "".join(
    'set "' + n + '="\r\n'
    for n in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
              "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
              "SSL_CERT_FILE", "ssl_cert_file", "SSL_CERT_DIR", "ssl_cert_dir"))
_BAT_POISON = (
    "@echo off\r\n"
    + _BAT_NORMALIZE +
    "set HTTP_PROXY=http://127.0.0.1:1\r\n"
    "set HTTPS_PROXY=http://127.0.0.1:1\r\n"
    "set ALL_PROXY=socks5://127.0.0.1:1\r\n"
    "set NO_PROXY=localhost,127.0.0.1\r\n"
    + "".join("set " + n + "=" + v + "\r\n" for n, v in _TRUST_POISON)
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
    assert "names=none" not in out, f"注入了 6 枚却报 names=none——累加器坏了：{out}"
    assert "PROBE_STATE=STILL_" not in out, f"清环境后仍有变量存活：{out}"
    assert "PROBE_STATE=CLEAN" in out, f"探针自己没跑到判定行：{out}"
    assert "FINDSTR_RC=1" in out, f"set|findstr 还能看到代理变量（rc 不是 1）：{out}"


def test_machine_truth_bat_counterexample_no_scrub_leaves_vars(tmp_path):
    """反例：同样注入、**不**跑清环境块，六枚变量必须还在（AFTER 不是 CLEAN、findstr 命中）。"""
    res = _run_cmd_file(_BAT_POISON + _BAT_CHECK_TAIL, tmp_path)
    if res is None:
        pytest.skip("cmd.exe 不可用")
    out = res.stdout or ""
    assert "PROBE_STATE=STILL_" in out, f"反例坏了：不跑清环境块变量就不在了？{out}"
    assert "FINDSTR_RC=0" in out, f"findstr 抓不到注入的变量，正面那格的 rc=1 就没有意义：{out}"


# ---------------------------------------------------- 2026-11-09 换包事故形状锁
_FIXED_ROSTER = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def test_ps_helper_uses_fixed_roster_not_env_drive_enumeration():
    """helper 不许再靠 `Get-ChildItem env:` 枚举来取删除名单。

    2026-11-09 真实事故：工具宿主同时注入了 http_proxy / HTTP_PROXY 和
    https_proxy / HTTPS_PROXY，枚举 env: drive 时 .NET 字典抛「已添加了具有相同键
    的项」；helper 排在 Start-Process 前面且同在一个 try，于是换包在 start 阶段中止
    ——包已经移进 live，服务没起来，公网 502。固定八枚名字（大小写两版）+ 逐枚
    SetEnvironmentVariable 兜底，才不会碰重复键那本字典。
    """
    for path in (WATCHDOG_PS1, SWAP_PS1):
        helper = _extract_ps_helper(path)
        assert helper, f"{path.name} 没有可抽取的 helper"
        assert "Get-ChildItem env:" not in helper, \
            f"{path.name} 又用 env: drive 枚举取名单了：重复键那场事故会重演"
        assert "SetEnvironmentVariable" in helper, \
            f"{path.name} 没有按名字清的兜底动作"
        assert "PROXY-SCRUB-WARN" in helper, \
            f"{path.name} 单枚清理失败会往外抛：防御动作不许中断换包"
        for name in _FIXED_ROSTER:
            assert "'" + name + "'" in helper, f"{path.name} 的固定名单缺 {name}"
        assert _PS_FILTER in helper, (
            f"{path.name} 的名单过滤器不是「四个前缀、大小写不敏感」这条：{_PS_FILTER}"
            "——收窄成只认某几个名字，剩下那几枚会被静默漏清")


def test_swap_scrub_calls_are_wrapped_so_they_cannot_abort_start():
    """swap 里两处 helper 调用都得被自己的 try/catch 包住。

    清理只是纵深防御；它失败时最多打一行 token，不许落到 step 6 的外层 catch
    变成 Stop-AfterFailure 'start'（那是「包已换、服务没起」的最坏失败形状）。
    """
    text = _read(SWAP_PS1)
    calls = [ln for ln in text.splitlines()
             if re.search(r"\bRemove-ProxyEnvForChild\b", ln)
             and not re.match(r"^\s*function\s", ln)]
    assert len(calls) == 2, f"swap 里应是两处调用，实际 {len(calls)}：{calls}"
    for ln in calls:
        assert re.search(r"\btry\s*\{[^}]*Remove-ProxyEnvForChild[^}]*\}\s*catch", ln), \
            f"调用没有单独兜底：{ln.strip()}"
