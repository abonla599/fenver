"""v0.37.5 T3：stdio 进程治理（命令 / env / cwd / 进程树 / 资源）的行为锁。

覆盖计划 §7.2 的 C1-C7 与 §3.6：
- 命令判据：相对路径 / ``cmd.exe`` / ``.cmd`` / UNC / 不存在 → ``MCPStdioConfigError``，
  错误串不回显 args；
- ``shell=False``：argv 里的 ``& echo ... > marker.txt`` 原样到达，marker 不生成；
- 环境从空白构造：父进程的 ``DEEPSEEK_API_KEY`` / ``HTTP_PROXY`` 不进子进程，
  allowlist 进；``HOME`` / ``TEMP`` 指向 ``<mcp_root>/<id>/``，目录被创建；
- ``argv_fingerprint`` 只吃命令与 flag，位置参数的值不参与；
- Windows Job Object：5 个 limit flag + 进程/Job 内存 + CPU 时间可读回；
  注入 64MB 单进程上限后分配 128MB 的 ``hog`` 被终止；
- 进程树：``tree`` 模式拉孙进程 + 写心跳，``terminate_tree`` 后 2s 内父子都不在、
  心跳停止增长；
- ``terminate_tree`` 幂等；
- POSIX：``start_new_session`` + ``killpg``（CI Ubuntu 覆盖）。

Windows 专属用例 ``skipif(os.name != "nt")``；POSIX 用例 ``skipif(os.name == "nt")``。
所有临时目录用 ``tmp_path``，每个用例 finally 杀树；Windows 上用 ``is_pid_alive``
（``OpenProcess`` + ``GetExitCodeProcess``）断言无孤儿。
"""
import ast
import functools
import inspect
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from app.core import mcp_stdio_process
from app.core.mcp_stdio_process import (
    DEFAULT_ACTIVE_PROCESSES,
    DEFAULT_CPU_TIME_100NS,
    DEFAULT_JOB_MEMORY_BYTES,
    DEFAULT_PROCESS_MEMORY_BYTES,
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
    JOB_OBJECT_LIMIT_JOB_MEMORY,
    JOB_OBJECT_LIMIT_JOB_TIME,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOB_OBJECT_LIMIT_PROCESS_MEMORY,
    MCPStdioConfigError,
    argv_fingerprint,
    build_env_plan,
    command_basename,
    is_pid_alive,
    query_job_limits,
    spawn_stdio,
    validate_spawn_entry,
)

SERVER = Path(__file__).with_name("fake_mcp_stdio_server.py")
# 本机（Windows Store Python 的 venv）`Scripts\python.exe` 是启动器：Popen 拿到的是
# 启动器的 pid，真正的解释器是它的孩子，且这个孩子跑在 AppX 自己的 Job 里，Job/杀树
# 语义无法验证。此时用 `MCP_STDIO_TEST_EXE` 指向一个冻结好的单进程 helper（同一份
# fake server 的 PyInstaller onedir 产物，仓库外）。CI 的标准解释器不需要它。
_HELPER_EXE = os.environ.get("MCP_STDIO_TEST_EXE", "").strip()

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows Job Object / taskkill")
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX start_new_session / killpg")


@functools.lru_cache(maxsize=1)
def _single_process_host() -> bool:
    """当前解释器是否"Popen pid == 子进程 os.getpid()"的单进程宿主。"""
    if _HELPER_EXE:
        return True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pid.txt"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os,sys;open(sys.argv[1],'w').write(str(os.getpid()))",
                    str(out),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc.wait(timeout=20)
            return out.read_text(encoding="utf-8").strip() == str(proc.pid)
    except (OSError, subprocess.SubprocessError):
        return False


SPAWN_OK = pytest.mark.skipif(
    not _single_process_host(),
    reason=(
        "当前解释器是启动器（Popen pid != 真实进程 pid），Job/杀树/孤儿断言无意义；"
        "设 MCP_STDIO_TEST_EXE 指向单进程 fake server helper"
    ),
)


def _invocation(mode: str, record_dir: Path, extra=()):
    """返回 (command, args)：有 helper 用 helper，否则 sys.executable + 脚本。"""
    if _HELPER_EXE:
        return _HELPER_EXE, [mode, str(record_dir), *extra]
    return sys.executable, [str(SERVER), mode, str(record_dir), *extra]


# --------------------------------------------------------------------------- helpers


def _entry(
    mode: str,
    record_dir: Path,
    *,
    sid: str = "srv1",
    extra_args=(),
    allowlist=None,
    command=None,
    enabled: bool = True,
):
    default_command, default_args = _invocation(mode, record_dir, extra_args)
    return {
        "id": sid,
        "command": command or default_command,
        "args": default_args,
        "env_allowlist": list(allowlist or []),
        "enabled": enabled,
    }


def _wait_until(predicate, timeout: float, message: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message)


def _read_json(path: Path, timeout: float = 5.0):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"未等到有效 JSON: {path} ({last_error})")


def _assert_gone(pid: int, timeout: float = 2.0) -> None:
    _wait_until(
        lambda: not is_pid_alive(pid),
        timeout,
        f"pid {pid} 在 {timeout}s 内仍存活",
    )


# --------------------------------------------------------------------------- C4 命令判据


@WINDOWS_ONLY
def test_windows_command_policy_rejects_before_spawn(tmp_path):
    root = tmp_path / "mcp"
    not_exe = tmp_path / "evil.cmd"
    not_exe.write_text("echo hi\n", encoding="utf-8")
    cases = [
        "cmd.exe",                                  # 相对路径
        "python.exe",                               # 相对路径
        str(not_exe),                               # .cmd 不是 .exe
        r"\\server\share\evil.exe",                 # UNC
        r"\\?\C:\Windows\System32\cmd.exe",         # 设备命名空间
        r"C:\definitely-no-such-dir\nope.exe",      # 不存在
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",  # 宿主黑名单
    ]
    canary = "CANARY-ARG-MUST-NOT-LEAK"
    for command in cases:
        entry = _entry("ok", tmp_path / "rec", extra_args=(canary,), command=command)
        with pytest.raises(MCPStdioConfigError) as excinfo:
            spawn_stdio(entry, str(root))
        assert canary not in str(excinfo.value)
        assert command not in str(excinfo.value)

    # 配置里带 cwd：T1 整条拒，spawn 也不接受（未知键）
    entry = _entry("ok", tmp_path / "rec")
    entry["cwd"] = str(tmp_path)
    with pytest.raises(MCPStdioConfigError):
        spawn_stdio(entry, str(root))

    # validate_spawn_entry 是同一个入口
    with pytest.raises(MCPStdioConfigError):
        validate_spawn_entry(_entry("ok", tmp_path / "rec", command="cmd.exe"))


# --------------------------------------------------------------------------- C3 shell 注入


@SPAWN_OK
def test_shell_metacharacters_stay_argv(tmp_path):
    record_dir = tmp_path / "rec"
    root = tmp_path / "mcp"
    injected = (
        "& echo marker > marker.txt"
        if os.name == "nt"
        else "; echo marker > marker.txt"
    )
    # 不含空格/引号：Windows 的 list2cmdline 不会给它加引号，shell=True 时 cmd 真的
    # 会解释 `&` 与 `>`（上一行带空格的字符串会被加引号，单靠它验不出 shell=True）。
    unquoted = "&echo.>marker2.txt" if os.name == "nt" else ";echo.>marker2.txt"
    # 重定向会在**启动 server 之前**由 shell 求值：server 是阻塞进程，`&` 后面的命令
    # 永远不会轮到执行，只有这种形状能在 server 还活着时留下 shell 解释的铁证。
    redirect = ">marker3.txt"
    marker = root / "sh1" / "marker.txt"
    marker2 = root / "sh1" / "marker2.txt"
    marker3 = root / "sh1" / "marker3.txt"
    wrapper = spawn_stdio(
        _entry(
            "record_argv",
            record_dir,
            sid="sh1",
            # 重定向放最前：带空格的参数会被 list2cmdline 加引号，若它在前，cmd 的
            # 引号状态会把后面的 `>` 吞掉；先出现才能证明"重定向被 shell 求值"。
            extra_args=(redirect, injected, unquoted),
        ),
        str(root),
    )
    try:
        # shell=False：没有解释器，三个 marker 都不该出现（先断言这个：shell=True
        # 会把参数当运算符吃掉，argv 断言可能先红，但 marker 才是"解释执行"的铁证）。
        # 留 0.2s 让 shell 的重定向落盘；无 shell 时这里什么也不会多出来。
        time.sleep(0.2)
        assert not marker3.exists(), "重定向被 shell 求值了"
        assert not marker.exists(), "带空格的参数被 shell 解释执行了"
        assert not marker2.exists(), "不带空格的参数被 shell 解释执行了"
        argv = _read_json(record_dir / "argv.json")
        assert argv[-3] == redirect
        assert argv[-2] == injected
        assert argv[-1] == unquoted
        # record_argv 顺带记 cwd：子进程实际工作目录必须是 <mcp_root>/<id>，
        # 不是服务进程的 cwd，也不是配置里能写的任何路径（cwd 键由 T1 整条拒）。
        # helper 是仓库外的预构建产物，字节可能早于这条埋点；这种情况由下面
        # 不依赖 helper 的 test_child_cwd_is_mcp_root_server_id 兜底。
        if not _HELPER_EXE:
            assert os.path.samefile(_read_json(record_dir / "cwd.json"), root / "sh1")
    finally:
        assert wrapper.terminate_tree() is True


# --------------------------------------------------------------------------- C2 env


@SPAWN_OK
def test_env_plan_is_blank_slate_and_allowlist_only(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "CANARY-DEEPSEEK-KEY")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("MCP_LOG_LEVEL", "debug")
    evil_path = str(tmp_path / "evil-path")
    monkeypatch.setenv("PATH", evil_path + os.pathsep + os.environ.get("PATH", ""))

    root = tmp_path / "mcp"
    # 保留名（含大小写变体）与普通名一起递进来：allowlist 只能"额外允许"，
    # 绝不能覆盖固定注入的 HOME/TEMP/TMP/PATH。
    allowlist = ["MCP_LOG_LEVEL", "PATH", "TEMP", "HOME", "Path", "path"]
    plan = build_env_plan("env1", allowlist, root, command=sys.executable)

    assert "DEEPSEEK_API_KEY" not in plan
    assert "HTTP_PROXY" not in plan
    assert plan["MCP_LOG_LEVEL"] == "debug"
    assert plan["HOME"] == str(root / "env1" / "home")
    assert plan["TEMP"] == str(root / "env1" / "tmp")
    assert plan["TMP"] == str(root / "env1" / "tmp")
    assert evil_path not in plan["PATH"]
    assert "Path" not in plan and "path" not in plan, "保留名的大小写变体漏进了 env"
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in plan.items())
    if os.name == "nt":
        for name in ("SystemRoot", "SystemDrive", "windir", "COMSPEC", "PATHEXT", "PATH",
                     "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            assert plan.get(name), name
    else:
        assert plan["PATH"].startswith("/usr/local/bin:/usr/bin:/bin")
        assert "SystemRoot" not in plan and "COMSPEC" not in plan
    # 就算 allowlist 写了敏感名 / 非 str，也不复制
    denied = build_env_plan("env1", ["DEEPSEEK_API_KEY", "HTTP_PROXY", 3], root, command=sys.executable)
    assert "DEEPSEEK_API_KEY" not in denied
    assert "HTTP_PROXY" not in denied

    # 子进程实际拿到的 env：这里故意绕过 `normalize_entry` 直接喂 `build_env_plan`。
    # 写入口（T1）已经会拒保留名，本用例要证明的是**第二层（构造层）自己也能
    # 挡住**——否则任何绕过写入口的调用方都能改边界。
    record_dir = tmp_path / "rec"
    command, child_args = _invocation("envdump", record_dir)
    child_cwd = root / "env1"
    child_cwd.mkdir(parents=True, exist_ok=True)
    child_env = build_env_plan("env1", allowlist, root, command=command)
    completed = subprocess.run(
        [command, *child_args],
        cwd=str(child_cwd),
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    dumped = _read_json(record_dir / "env.json")
    assert "DEEPSEEK_API_KEY" not in dumped
    assert "HTTP_PROXY" not in dumped
    assert dumped["MCP_LOG_LEVEL"] == "debug"
    assert Path(dumped["HOME"]) == root / "env1" / "home"
    assert Path(dumped["TEMP"]) == root / "env1" / "tmp"
    assert Path(dumped["TMP"]) == root / "env1" / "tmp"
    assert dumped["PATH"] != os.environ.get("PATH")
    assert evil_path not in dumped["PATH"]
    assert "Path" not in dumped and "path" not in dumped

    # 写入口那一层：带保留名的条目必须整条拒（T1 锁在 T3 侧再钉一次）。
    with pytest.raises(MCPStdioConfigError) as excinfo:
        spawn_stdio(
            _entry("envdump", tmp_path / "rec2", sid="env1", allowlist=["PATH"]),
            str(root),
        )
    assert "env_name_reserved" in str(excinfo.value)


def test_home_and_tmp_directories_created(tmp_path):
    root = tmp_path / "mcp"
    home = root / "dir1" / "home"
    temp = root / "dir1" / "tmp"
    assert not home.exists()
    build_env_plan("dir1", [], root, command=sys.executable)
    assert home.is_dir()
    assert temp.is_dir()
    if os.name != "nt":
        assert (home.stat().st_mode & 0o777) == 0o700
        assert (temp.stat().st_mode & 0o777) == 0o700


# --------------------------------------------------------------------------- 审计摘要


def test_argv_fingerprint_ignores_argument_values_but_tracks_flags():
    command = str(SERVER)
    base = argv_fingerprint(command, ["srv.py", "--port", "1", "data.txt"])
    same = argv_fingerprint(command, ["srv.py", "--port", "2", "other.txt"])
    assert base == same
    assert "data.txt" not in base and "other.txt" not in base
    assert len(base) == 64 and all(ch in "0123456789abcdef" for ch in base)

    with_flag = argv_fingerprint(command, ["srv.py", "--port", "1", "--verbose", "data.txt"])
    assert with_flag != base
    assert argv_fingerprint(command + ".other", ["srv.py", "--port", "1"]) != base

    # --flag=value：值同样可能是低熵密钥，只 hash flag 名。
    equal_a = argv_fingerprint(command, ["srv.py", "--token=AAA", "pos-a"])
    equal_b = argv_fingerprint(command, ["srv.py", "--token=BBB", "pos-b"])
    assert equal_a == equal_b, "--flag=value 的值进了摘要"
    assert argv_fingerprint(command, ["srv.py", "--other=AAA"]) != equal_a

    assert command_basename(command) == "fake_mcp_stdio_server.py"


# --------------------------------------------------------------------------- C6 Job flag


@WINDOWS_ONLY
@SPAWN_OK
def test_job_limits_readback(tmp_path):
    root = tmp_path / "mcp"
    wrapper = spawn_stdio(_entry("ok", tmp_path / "rec"), str(root))
    try:
        assert wrapper.job_attached is True, wrapper.job_error
        assert wrapper.job is not None
        limits = query_job_limits(wrapper.job)
        flags = limits["limit_flags"]
        for flag in (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
            JOB_OBJECT_LIMIT_PROCESS_MEMORY,
            JOB_OBJECT_LIMIT_JOB_MEMORY,
            JOB_OBJECT_LIMIT_JOB_TIME,
        ):
            assert flags & flag == flag, hex(flag)
        assert limits["active_process_limit"] == DEFAULT_ACTIVE_PROCESSES == 8
        assert limits["process_memory_limit"] == DEFAULT_PROCESS_MEMORY_BYTES == 512 * 1024 * 1024
        assert limits["job_memory_limit"] == DEFAULT_JOB_MEMORY_BYTES == 1024 * 1024 * 1024
        assert limits["per_job_user_time_limit"] == DEFAULT_CPU_TIME_100NS == 15 * 60 * 10_000_000

        # 独立 ctypes 结构体回读：不调实现的 query_job_limits，也不 import 它的
        # 结构体。setter/reader 同错（例如两边一起改错字段偏移）在这里会红。
        import ctypes
        from ctypes import wintypes as wt

        class IndependentIoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class IndependentBasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IndependentExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", IndependentBasicLimits),
                ("IoInfo", IndependentIoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.QueryInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        k32.QueryInformationJobObject.restype = ctypes.c_int
        raw = IndependentExtendedLimits()
        assert k32.QueryInformationJobObject(
            int(wrapper.job), 9, ctypes.byref(raw), ctypes.sizeof(raw), None
        ), "独立结构体回读失败"
        raw_flags = int(raw.BasicLimitInformation.LimitFlags)
        for flag in (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
            JOB_OBJECT_LIMIT_PROCESS_MEMORY,
            JOB_OBJECT_LIMIT_JOB_MEMORY,
            JOB_OBJECT_LIMIT_JOB_TIME,
        ):
            assert raw_flags & flag == flag, "独立回读缺 flag " + hex(flag)
        assert int(raw.BasicLimitInformation.ActiveProcessLimit) == 8
        assert int(raw.ProcessMemoryLimit) == 512 * 1024 * 1024
        assert int(raw.JobMemoryLimit) == 1024 * 1024 * 1024
        assert int(raw.BasicLimitInformation.PerJobUserTimeLimit) == 15 * 60 * 10_000_000
    finally:
        assert wrapper.terminate_tree() is True
    _assert_gone(wrapper.pid)


@WINDOWS_ONLY
@SPAWN_OK
def test_job_creation_failure_kills_spawned_tree(tmp_path, monkeypatch):
    """Job 建不起来 = 没有限额：已启动的进程必须被杀，spawn 必须拒绝启动。"""
    spawned_pids = []

    def failing_create_job(proc, **_kwargs):
        spawned_pids.append(proc.pid)
        return None, "boom"

    monkeypatch.setattr(mcp_stdio_process, "_create_job_for", failing_create_job)
    root = tmp_path / "mcp"
    with pytest.raises(MCPStdioConfigError) as excinfo:
        spawn_stdio(_entry("ok", tmp_path / "rec", sid="jobfail"), str(root))
    assert "job_failed" in str(excinfo.value)
    assert "boom" in str(excinfo.value)
    assert spawned_pids, "用例前提：_create_job_for 被调用，子进程已经启动"
    _assert_gone(spawned_pids[0], timeout=5.0)


@WINDOWS_ONLY
@SPAWN_OK
def test_job_failure_path_kills_tree_while_stdin_still_open(tmp_path, monkeypatch):
    """Job 建不起来时，杀树必须是承重防线，不是"关 stdin 让子进程读到 EOF"。

    spy 记录 ``terminate_tree`` 被调用、调用时子进程仍活着、且 stdin 尚未关闭。
    只删失败分支里的 ``wrapper.terminate_tree()`` 时本用例必须红（只留 wait +
    关管道，``ok`` 子进程会被 stdin EOF 收掉，旧用例因此抓不住）。
    """
    observed = []
    original = mcp_stdio_process.StdioProcess.terminate_tree

    def spy(self, *args, **kwargs):
        stdin = self.proc.stdin
        observed.append(
            {
                "pid": self.pid,
                "alive": is_pid_alive(self.pid),
                "stdin_closed": stdin is None or stdin.closed,
            }
        )
        return original(self, *args, **kwargs)

    monkeypatch.setattr(
        mcp_stdio_process, "_create_job_for", lambda proc, **_kwargs: (None, "boom")
    )
    monkeypatch.setattr(mcp_stdio_process.StdioProcess, "terminate_tree", spy)

    root = tmp_path / "mcp"
    with pytest.raises(MCPStdioConfigError) as excinfo:
        spawn_stdio(_entry("ok", tmp_path / "rec", sid="jobkill"), str(root))

    assert "job_failed:boom" in str(excinfo.value)
    assert observed, "Job 失败路径没有调用 terminate_tree：子进程只是被 stdin EOF 收掉"
    assert observed[0]["alive"] is True, "杀树时子进程已退出，承重防线没有被钉住"
    assert observed[0]["stdin_closed"] is False, "关管道发生在杀树之前"
    _assert_gone(observed[0]["pid"], timeout=5.0)


@WINDOWS_ONLY
@SPAWN_OK
def test_job_failure_kill_failure_is_reported_in_short_code(tmp_path, monkeypatch):
    """杀树没能确认进程已死时，终态短码必须带 ``:kill_failed``。"""
    original = mcp_stdio_process.StdioProcess.terminate_tree

    def kill_but_report_failure(self, *args, **kwargs):
        original(self, *args, **kwargs)  # 仍真杀，别把子进程留成孤儿
        return False

    monkeypatch.setattr(
        mcp_stdio_process, "_create_job_for", lambda proc, **_kwargs: (None, "attach_failed:5")
    )
    monkeypatch.setattr(
        mcp_stdio_process.StdioProcess, "terminate_tree", kill_but_report_failure
    )

    root = tmp_path / "mcp"
    with pytest.raises(MCPStdioConfigError) as excinfo:
        spawn_stdio(_entry("ok", tmp_path / "rec", sid="jobkillfail"), str(root))

    message = str(excinfo.value)
    assert message == "job_failed:attach_failed:5:kill_failed", message
    assert str(tmp_path) not in message, "短码不许回显配置值"


def test_spawn_stdio_job_failure_branch_raises_not_returns():
    """平台无关：Job 创建失败必须抛配置错，且该分支不许返回半启动的 runtime。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(mcp_stdio_process.spawn_stdio)))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)

    branches = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If)
        and any(
            isinstance(call, ast.Call)
            and getattr(call.func, "id", "") == "_create_job_for"
            for call in ast.walk(node)
        )
    ]
    assert len(branches) == 1, "spawn_stdio 里找不到唯一的 Job 创建分支"

    branch = branches[0]
    raises = [node for node in ast.walk(branch) if isinstance(node, ast.Raise)]
    assert raises, "Job 创建失败只记 job_error/返回错误串，没有 fail-closed"
    raised = {
        getattr(node.exc.func, "id", getattr(node.exc.func, "attr", ""))
        for node in raises
        if isinstance(node.exc, ast.Call)
    }
    assert raised & {"_config_error", "MCPStdioConfigError"}, raised
    assert not [node for node in ast.walk(branch) if isinstance(node, ast.Return)], (
        "Job 创建失败的分支里出现了 return"
    )

    # 杀树是失败路径的承重防线，且必须先于关管道：只留 wait + 关 stdin 时，子进程
    # 会被 EOF 收掉，行为用例会误绿（helper 忽略不了 EOF 时尤其如此）。
    tree_calls = [
        node
        for node in ast.walk(branch)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "terminate_tree"
    ]
    closes = [
        node
        for node in ast.walk(branch)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "close"
    ]
    assert tree_calls, "Job 失败分支没有调用 terminate_tree"
    assert closes, "前提：Job 失败分支有关管道"
    assert tree_calls[0].lineno < closes[0].lineno, "关管道发生在杀树之前"

    # 整个函数只有一处 runtime 构造，且在 Job 分支之后：失败路径交不出 runtime。
    runtime_returns = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", "") == "StdioProcess"
    ]
    assert len(runtime_returns) == 1
    assert runtime_returns[0].lineno > branch.end_lineno


# --------------------------------------------------------------------------- C7 内存限额


@WINDOWS_ONLY
@SPAWN_OK
def test_process_memory_limit_kills_hog(tmp_path):
    record_dir = tmp_path / "rec"
    root = tmp_path / "mcp"
    wrapper = spawn_stdio(
        _entry("hog", record_dir, sid="hog1"),
        str(root),
        process_memory_bytes=64 * 1024 * 1024,
    )
    try:
        # hog 先正常回 initialize，再分配约 128MB；这里手动发一帧触发。
        frame = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            ensure_ascii=False,
        ).encode("utf-8") + b"\n"
        wrapper.proc.stdin.write(frame)
        wrapper.proc.stdin.flush()

        deadline = time.time() + 10.0
        while time.time() < deadline and wrapper.poll() is None:
            time.sleep(0.05)
        assert wrapper.poll() is not None, "128MB 分配在 64MB 单进程上限下没有被终止"
        assert wrapper.poll() != 0
        assert is_pid_alive(wrapper.pid) is False
    finally:
        wrapper.terminate_tree()


# --------------------------------------------------------------------------- C1 进程树


@WINDOWS_ONLY
@SPAWN_OK
def test_terminate_tree_kills_grandchild_and_stops_heartbeat(tmp_path):
    record_dir = tmp_path / "rec"
    root = tmp_path / "mcp"
    wrapper = spawn_stdio(_entry("tree", record_dir, sid="tree1"), str(root))
    try:
        tree = _read_json(record_dir / "tree.json")
        parent = tree["parent"]
        grandchild = tree["grandchild"]
        assert parent == wrapper.pid
        assert is_pid_alive(grandchild) is True

        heartbeat = record_dir / "heartbeat.txt"
        _wait_until(
            lambda: heartbeat.exists() and heartbeat.stat().st_size > 0,
            5.0,
            "心跳文件没有出现",
        )
        size_before = heartbeat.stat().st_size
        time.sleep(0.5)
        assert heartbeat.stat().st_size > size_before

        assert wrapper.terminate_tree(timeout=5.0) is True
        _assert_gone(parent, timeout=2.0)
        _assert_gone(grandchild, timeout=2.0)

        size_after = heartbeat.stat().st_size
        time.sleep(0.5)
        assert heartbeat.stat().st_size == size_after, "进程树被杀后心跳仍在增长"
    finally:
        wrapper.terminate_tree()


# --------------------------------------------------------------------------- 幂等


@SPAWN_OK
def test_child_cwd_is_mcp_root_server_id(tmp_path):
    """不依赖 helper 字节：子进程自己报告 cwd，必须是 ``<mcp_root>/<id>``。"""
    root = tmp_path / "mcp"
    out = tmp_path / "cwd.txt"
    code = (
        "import os,sys;"
        "open(sys.argv[1], 'w', encoding='utf-8').write(os.getcwd())"
    )
    entry = {
        "id": "cwd1",
        "command": sys.executable,
        "args": ["-c", code, str(out)],
        "env_allowlist": [],
        "enabled": True,
    }
    wrapper = spawn_stdio(entry, str(root))
    try:
        _wait_until(
            lambda: out.exists() and out.read_text(encoding="utf-8").strip(),
            10.0,
            "子进程没有写出 cwd",
        )
        reported = out.read_text(encoding="utf-8").strip()
        assert os.path.samefile(reported, root / "cwd1"), reported
    finally:
        wrapper.terminate_tree()


@WINDOWS_ONLY
@SPAWN_OK
def test_terminate_tree_never_taskkills_a_reaped_pid(tmp_path, monkeypatch):
    """wait()/poll() 收尸后 PID 可能被复用：terminate_tree 只许碰 Job，不许 taskkill。"""
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        raise AssertionError("已收尸的 PID 上仍发了 taskkill")

    monkeypatch.setattr(mcp_stdio_process.subprocess, "run", fake_run)
    root = tmp_path / "mcp"

    waited = spawn_stdio(_entry("ok", tmp_path / "rec1", sid="reap1"), str(root))
    assert waited.job is not None, "用例前提：Job 已附加（否则测不到句柄关闭）"
    waited.proc.stdin.close()
    assert waited.wait(timeout=15) == 0
    assert waited.job is None, "wait() 收尸后必须关 Job 句柄"
    assert waited.terminate_tree() is True
    assert waited.terminate_tree() is True

    polled = spawn_stdio(_entry("ok", tmp_path / "rec2", sid="reap2"), str(root))
    assert polled.job is not None, "用例前提：Job 已附加"
    polled.proc.stdin.close()
    _wait_until(lambda: polled.poll() is not None, 15, "stdin EOF 后进程没有退出")
    assert polled.job is None, "poll() 发现退出后也必须关 Job 句柄"
    assert polled.terminate_tree() is True
    assert calls == [], f"taskkill 被调用: {calls}"


@WINDOWS_ONLY
@SPAWN_OK
def test_system_paths_come_from_windows_api_not_env(tmp_path, monkeypatch):
    """SystemRoot/windir/SystemDrive 被污染时，子进程仍拿到真实系统目录。"""
    fake = tmp_path / "fake-windows"
    (fake / "System32").mkdir(parents=True)
    monkeypatch.setenv("SystemRoot", str(fake))
    monkeypatch.setenv("windir", str(fake))
    monkeypatch.setenv("SystemDrive", "Q:")

    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    assert ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)) > 0
    real_root = buffer.value
    system_buffer = ctypes.create_unicode_buffer(32768)
    assert ctypes.windll.kernel32.GetSystemDirectoryW(
        system_buffer, len(system_buffer)) > 0
    real_system32 = system_buffer.value

    root = tmp_path / "mcp"
    wrapper = spawn_stdio(_entry("envdump", tmp_path / "rec", sid="sys1"), str(root))
    try:
        dumped = _read_json(tmp_path / "rec" / "env.json")
    finally:
        wrapper.terminate_tree()

    # Windows 的 os.environ 会归一化键名大小写；按大写比较，别依赖存储形态。
    dumped = {str(key).upper(): value for key, value in dumped.items()}
    assert os.path.normcase(dumped["SYSTEMROOT"]) == os.path.normcase(real_root)
    assert os.path.normcase(dumped["WINDIR"]) == os.path.normcase(real_root)
    assert os.path.normcase(dumped["COMSPEC"]) == os.path.normcase(
        os.path.join(real_system32, "cmd.exe"))
    assert os.path.normcase(dumped["PATH"].split(";")[0]) == os.path.normcase(
        real_system32)
    assert str(fake).lower() not in dumped["PATH"].lower()
    assert os.path.normcase(dumped["SYSTEMDRIVE"]) == os.path.normcase(
        os.path.splitdrive(real_root)[0])
    taskkill = mcp_stdio_process._taskkill_path()
    assert os.path.isfile(taskkill)
    assert os.path.normcase(os.path.dirname(taskkill)) == os.path.normcase(
        real_system32)


@SPAWN_OK
def test_terminate_tree_is_idempotent(tmp_path):
    root = tmp_path / "mcp"
    wrapper = spawn_stdio(_entry("ok", tmp_path / "rec", sid="idem1"), str(root))
    try:
        assert wrapper.terminate_tree() is True
        assert wrapper.terminate_tree() is True
    finally:
        wrapper.terminate_tree()

    # 已自然退出的进程：wait 收尸后再调两次都不抛
    exited = spawn_stdio(_entry("eof", tmp_path / "rec2", sid="eof1"), str(root))
    try:
        assert exited.wait(timeout=10) == 0
        assert exited.terminate_tree() is True
        assert exited.terminate_tree() is True
    finally:
        exited.terminate_tree()


# --------------------------------------------------------------------------- POSIX


@POSIX_ONLY
@SPAWN_OK
def test_posix_new_session_and_killpg(tmp_path):
    record_dir = tmp_path / "rec"
    root = tmp_path / "mcp"
    wrapper = spawn_stdio(_entry("tree", record_dir, sid="tree1"), str(root))
    try:
        assert os.getsid(wrapper.pid) == wrapper.pid
        assert os.getpgid(wrapper.pid) == wrapper.pid
        tree = _read_json(record_dir / "tree.json")
        grandchild = tree["grandchild"]

        heartbeat = record_dir / "heartbeat.txt"
        _wait_until(
            lambda: heartbeat.exists() and heartbeat.stat().st_size > 0,
            5.0,
            "心跳文件没有出现",
        )
        assert wrapper.terminate_tree(timeout=5.0) is True
        _assert_gone(wrapper.pid, timeout=2.0)
        _assert_gone(grandchild, timeout=2.0)
    finally:
        wrapper.terminate_tree()


@POSIX_ONLY
def test_posix_command_policy_rejects_hosts(tmp_path):
    root = tmp_path / "mcp"
    for command in ("/bin/sh", "/bin/bash", "python3", "//tmp/x"):
        with pytest.raises(MCPStdioConfigError):
            spawn_stdio(_entry("ok", tmp_path / "rec", command=command), str(root))
