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
import functools
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

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
    plan = build_env_plan("env1", ["MCP_LOG_LEVEL"], root, command=sys.executable)

    assert "DEEPSEEK_API_KEY" not in plan
    assert "HTTP_PROXY" not in plan
    assert plan["MCP_LOG_LEVEL"] == "debug"
    assert plan["HOME"] == str(root / "env1" / "home")
    assert plan["TEMP"] == str(root / "env1" / "tmp")
    assert evil_path not in plan["PATH"]
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

    # 子进程实际拿到的 env：用 envdump 从 child 侧取证
    record_dir = tmp_path / "rec"
    wrapper = spawn_stdio(
        _entry("envdump", record_dir, sid="env1", allowlist=["MCP_LOG_LEVEL"]), str(root)
    )
    try:
        dumped = _read_json(record_dir / "env.json")
        assert "DEEPSEEK_API_KEY" not in dumped
        assert "HTTP_PROXY" not in dumped
        assert dumped["MCP_LOG_LEVEL"] == "debug"
        assert Path(dumped["HOME"]) == root / "env1" / "home"
        assert Path(dumped["TEMP"]) == root / "env1" / "tmp"
    finally:
        assert wrapper.terminate_tree() is True


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
    finally:
        assert wrapper.terminate_tree() is True
    _assert_gone(wrapper.pid)


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
