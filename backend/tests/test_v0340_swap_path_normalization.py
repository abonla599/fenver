"""换包脚本的路径归一化契约（v0.34.0，2026-10-09 加）。

血泪来源就一条，是出包预演当天撞上的：`-StageDir dist_new\\run_backend`（相对路径，从仓库根
敲下去，天经地义）让预检吐出了 **两条** 红，第二条是

    PRECHECK fail stage-volume stage= live=C:\\

同卷校验用 `[IO.Path]::GetPathRoot()` 取两边的盘符，而**相对路径的盘符是空串**——于是一台
根本没问题的机器被判"暂存包在别的卷上"。为什么这值得单独一条锁：换包当天最贵的不是红，是
**假的红**。人会开始怀疑预检、去找"绕过预检"的开关，而那条路径的尽头就是 v0.29.0 那两次
"提醒不是机制"的事故。真红（版本戳不是目标版）必须照旧留在那儿，所以这条锁判的是
"相对路径下只剩真红，一条不多"。

判据分两层，缺一层都不算：
1. **行为层**：拿一个版本戳故意做错的桩包，用**相对路径**跑 `-DryRun`，要求
   出现 `stage-version=`（真红还在）且**不出现** `stage-volume`（假红没了）；再用绝对路径
   跑同一个桩，要求两条 `PRECHECK fail` 完全一致——归一化不该改变判定，只该去掉凭空多出来的那条。
2. **文本层**：归一化必须发生在 `$StageExe/$StageAppJs/$StageStamp` 拼出来**之前**
   （否则版本预检读的是相对目录、重命名读的是绝对目录，两边看的不是同一个包）；且
   不许改用 `Resolve-Path`（目录不存在时它**抛异常**，会把"包还没构建"这种正常红变成
   脚本崩，一个 token 一行的机器可读输出就没了），也不许用 `[Environment]::CurrentDirectory`
   （PowerShell 里它和会话所在目录可以不一致，那正是这台机器上最容易骗过自己的那种不一致）。

`-DryRun` 的语义（读脚本 step 0 与 312-345 行确认）：预检不过就直接 `exit 1`，**不停进程、
不动文件、不碰计划任务**——所以行为层可以放心实跑，不需要 mock。
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

# 读法与行号尺子都从 v0.29.1 那份契约借（本仓的规矩：剥注释的尺子只有一份）
from tests.test_swap_package_contract_v0291 import _line_of, _strip_comments

REPO_ROOT = Path(__file__).resolve().parents[2]
SWAP_PS1 = REPO_ROOT / "deploy" / "swap-package.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")

CODE = _strip_comments(SWAP_PS1.read_text(encoding="utf-8"))

STUB_STAMP = "v0.0.0-stub"
STUB_MARKER = "STUB-MARKER-ONLY-IN-THIS-TEST"


def _make_stub(root: Path) -> Path:
    """一个形状正确、版本戳故意不是目标版的桩包（只用于预检失败的路径）。"""
    stage = root / "stage" / "run_backend"
    internal = stage / "_internal" / "app" / "web" / "static"
    internal.mkdir(parents=True, exist_ok=True)
    (stage / "run_backend.exe").write_bytes(b"")
    (stage / "_internal" / "version.txt").write_text(STUB_STAMP, encoding="utf-8")
    (internal / "app.js").write_text("x = '" + STUB_MARKER + "'\n", encoding="utf-8")
    return stage


def _run_dryrun(stage_dir: str, cwd: Path) -> list:
    """-DryRun 实跑一次，返回所有 PRECHECK 行（失败也拿得到，退出码由调用方不管）。"""
    assert POWERSHELL, "这台机器上没有 powershell.exe，行为层锁就无从跑起"
    p = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(SWAP_PS1), "-DryRun",
         "-StageDir", stage_dir, "-ExpectedVersion", "v9.9.9", "-Marker", STUB_MARKER],
        cwd=str(cwd), capture_output=True, text=True, timeout=180)
    out = (p.stdout or "") + (p.stderr or "")
    return [ln.strip() for ln in out.splitlines() if ln.startswith("PRECHECK")]


def test_relative_stage_dir_loses_only_the_fabricated_failure(tmp_path):
    """相对路径：真红（版本戳）留着，假红（同卷）必须消失。"""
    stub = _make_stub(tmp_path)
    rel = (Path("stage") / stub.name).as_posix().replace("/", "\\")
    lines = _run_dryrun(rel, tmp_path)
    assert any("stage-version=" in ln and STUB_STAMP in ln for ln in lines), (
        f"相对路径下版本预检没有红（{lines}）——要么桩包形状坏了，要么归一化把包指错了地方；"
        "这条锁的意义是'真红照旧'，它不在就等于什么都没判")
    assert not any("stage-volume" in ln for ln in lines), (
        f"相对路径又造出了假红（{lines}）：GetPathRoot 对相对路径返回空串，"
        "换包当天人会对着一个不存在的卷问题找开关")


def test_relative_and_absolute_agree_on_the_verdict(tmp_path):
    """同一个桩包，相对与绝对敲法必须给出**逐条一致**的预检结果。"""
    stub = _make_stub(tmp_path)
    rel = _run_dryrun((Path("stage") / stub.name).as_posix().replace("/", "\\"), tmp_path)
    abs_ = _run_dryrun(str(stub), tmp_path)
    assert rel and abs_, f"两边一条预检都没有（rel={rel} abs={abs_}）：实跑没跑起来"
    assert rel == abs_, f"相对与绝对判定不一致：rel={rel} abs={abs_}"


# ------------------------------------------------------------------ 文本层
def test_normalizer_is_defined_and_applied_to_both_roots():
    assert re.search(r"function\s+ConvertTo-AbsolutePath\b", CODE), \
        "归一化函数没了：相对路径的假红会原样回来"
    assert "$ProjectRoot = ConvertTo-AbsolutePath" in CODE, \
        "-ProjectRoot 由调用方传相对路径时同样会造假红，这一处也得归一化"
    assert "$StageDir = ConvertTo-AbsolutePath" in CODE, \
        "$StageDir 没有过归一化：这就是 2026-10-09 那条假红的直接来源"


def test_normalization_comes_before_the_stage_joins():
    """归一化必须排在 $StageExe/$StageAppJs/$StageStamp 之前。

    顺序反了会怎样：预检读的是相对目录（靠进程当前目录碰巧对上），而重命名用的是另一个值。
    现网那台机器上"碰巧对上"是常态，所以这类错位不会红，只会把**上一版**换上去。
    """
    at = _line_of(r"^\s*\$StageDir\s*=\s*ConvertTo-AbsolutePath", CODE)
    for var in ("StageExe", "StageAppJs", "StageStamp"):
        joined = _line_of(r"^\s*\$" + var + r"\s*=\s*Join-Path\s+\$StageDir", CODE)
        assert at < joined, \
            f"${var} 在归一化之前就用 $StageDir 拼出来了：预检与重命名读的不是同一个目录"


def test_normalizer_does_not_rely_on_resolve_path_or_current_directory():
    """禁用两种"看着更地道"的写法，理由写进行为层 docstring；这里判的是它们不许回来。"""
    body = CODE[CODE.index("function ConvertTo-AbsolutePath"):]
    body = body[:body.index("\nif (-not $ProjectRoot)")]
    assert "Resolve-Path" not in body, \
        "Resolve-Path 对不存在的目录抛异常：'包还没构建'会从一条 PRECHECK 红变成脚本崩"
    assert "Environment]::CurrentDirectory" not in body, \
        "CurrentDirectory 与 PowerShell 会话目录可以不一致，相对路径会解析到别处"
    assert "IsPathRooted" in body and "GetFullPath" in body, \
        "归一化的两条腿（已绝对的直过、相对的用会话目录补全）缺了任一，判定就会漂"


def test_positive_control_the_stub_actually_fails_the_version_check(tmp_path):
    """正对照：桩包必须真的能让版本预检红。

    桩包形状一变（比如 _internal 的相对结构改了），两条行为层锁会因为"压根没跑到版本预检"
    而一起变绿——那种绿和"归一化坏了"长得一模一样，所以这里单独把"桩包必然触发真红"钉住。
    """
    stub = _make_stub(tmp_path)
    lines = _run_dryrun(str(stub), tmp_path)
    assert any("stage-version=" in ln for ln in lines), \
        f"桩包没有触发版本预检（{lines}）：行为层两条锁正在空转"
    # 桩包里 marker 是真的存在：那条预检不该红，红了说明形状错了
    assert not any("stage-appjs-marker=absent" in ln for ln in lines), \
        f"桩包的验收标记没被读到（{lines}）：_internal 的相对形状与脚本读法不一致"
