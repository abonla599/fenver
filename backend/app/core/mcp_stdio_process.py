"""v0.37.5 T3：stdio MCP server 的进程治理（命令 / env / cwd / 进程树 / 资源）。

这一层只做"把进程起对、把进程杀干净"，不做协议（T2）、不做状态机/审计（T4）、
不注册工具、不 import registry/executor。公开接口是 T4 supervisor 直接调用的那一组：

- :func:`build_env_plan`：从**空白**构造子进程环境，不继承 ``os.environ``；
- :func:`argv_fingerprint` / :func:`command_basename`：审计摘要（§2.7）；
- :func:`validate_spawn_entry`：写入口判据的启动时复查；
- :func:`spawn_stdio`：``Popen`` + Windows Job Object；
- :class:`StdioProcess`：``poll`` / ``wait`` / ``terminate_tree``；
- :func:`query_job_limits` / :func:`is_pid_alive`：测试与运维观测。

安全口径（计划 §2.3–§2.6、§3.6）：

- ``shell=False`` 永远；argv 是列表，元字符没有解释器；
- cwd 固定为 ``<mcp_root>/<id>``，配置里的 ``cwd`` 键由 T1 整条拒，这里不再接受参数；
- 子进程环境只有固定注入项 + 管理员 allowlist，密钥片段（``env_name_denied``）一律丢；
- Windows 上 ``KILL_ON_JOB_CLOSE`` 保证服务进程被看守 ``TerminateProcess`` 时整棵子树
  跟着死；``taskkill /F /T`` 是第二道，兜 Job 分配与子进程首次 ``CreateProcess``
  之间毫秒级竞态逃逸的孙进程；
- 错误串只放短原因，绝不回显 command/args/env 的值。

Job 失败（嵌套 Job 被拒、权限不足等）**不拒绝启动**：``job_attached=False`` 记下来，
靠 ``taskkill`` 收树。状态必须可观测，但不该因为一个观测/兜底设施起不来就让管理员
配置的 server 全站不可用。
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
from typing import Any, Dict, List, Optional, Sequence

from app.core.mcp_stdio_config import (
    ID_RE,
    command_problem,
    env_name_denied,
    normalize_entry,
)

# Windows Job Object 常量（winnt.h）
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# 默认资源极限（计划 §2.5）；测试注入小值。
DEFAULT_PROCESS_MEMORY_BYTES = 512 * 1024 * 1024
DEFAULT_JOB_MEMORY_BYTES = 1024 * 1024 * 1024
DEFAULT_ACTIVE_PROCESSES = 8
DEFAULT_CPU_TIME_100NS = 15 * 60 * 10_000_000  # 15 分钟

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200

_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"


class MCPStdioConfigError(Exception):
    """配置类错误：终态，不重启、不重试（计划 §3.2 的 ``config_error``）。

    只带短原因（如 ``command:host_denied``），绝不带 command/args/env 的值。
    """


# --------------------------------------------------------------------------- 平台单例


if os.name == "nt":  # pragma: no cover - 仅在 Windows 上执行
    import ctypes
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),  # ULONG_PTR
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
else:  # pragma: no cover - POSIX 分支没有 Job Object
    ctypes = None  # type: ignore[assignment]
    wintypes = None  # type: ignore[assignment]
    _kernel32 = None


# --------------------------------------------------------------------------- 小工具


def _system_root() -> str:
    """Windows 系统目录：优先真实 ``SystemRoot``，否则按盘符推默认值。

    只读这两个固定名字，不是为了继承环境，而是为了让 ``SystemRoot`` /
    ``COMSPEC`` / ``PATH`` 指向真实系统目录（写死 ``C:\\Windows`` 在系统盘不是 C
    的机器上会起不来任何东西）。
    """
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or ""
    if root and os.path.isabs(root):
        return root.rstrip("\\/")
    drive = os.environ.get("SystemDrive") or "C:"
    return drive.rstrip("\\/") + "\\Windows"


def _system_drive(system_root: str) -> str:
    drive = os.environ.get("SystemDrive") or ""
    if drive:
        return drive
    head, _tail = os.path.splitdrive(system_root)
    return head or "C:"


def _config_error(reason: str) -> MCPStdioConfigError:
    return MCPStdioConfigError(reason)


def _makedirs_private(path: str) -> None:
    """建目录；POSIX 收紧到 0700（Windows 继承数据根 ACL）。"""
    os.makedirs(path, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            # 目录已存在且属主不同时 chmod 会失败；不该因此拒绝启动。
            pass


def _dedupe_paths(paths: Sequence[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in paths:
        if not item:
            continue
        key = os.path.normcase(os.path.normpath(item))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# --------------------------------------------------------------------------- env / 审计


def build_env_plan(server_id: str, allowlist, mcp_root: str, *, command: Optional[str] = None) -> Dict[str, str]:
    """构造子进程环境：**从空白开始**，只放固定注入项 + allowlist。

    - 不继承 ``os.environ``（除了按名复制 allowlist 里的安全项）；
    - ``HOME`` / ``USERPROFILE`` / ``APPDATA`` / ``LOCALAPPDATA`` 指到
      ``<mcp_root>/<id>/home``，``TEMP`` / ``TMP`` 指到 ``<mcp_root>/<id>/tmp``；
    - ``PATH`` 固定为系统目录 + ``command`` 所在目录，不继承服务进程 PATH；
    - POSIX 不注入 Windows 专属变量，``PATH`` 固定为 ``/usr/local/bin:/usr/bin:/bin``。

    allowlist 里被 ``env_name_denied`` 命中的名字、非 str 名字、服务进程里不存在的
    名字一律丢。返回值只含字符串键值；**值不写日志**。
    """
    if not isinstance(server_id, str) or not ID_RE.fullmatch(server_id):
        raise _config_error("bad_server_id")

    root = os.path.abspath(os.fspath(mcp_root))
    server_dir = os.path.join(root, server_id)
    home_dir = os.path.join(server_dir, "home")
    tmp_dir = os.path.join(server_dir, "tmp")
    for directory in (server_dir, home_dir, tmp_dir):
        _makedirs_private(directory)

    env: Dict[str, str] = {}
    command_dir = ""
    if isinstance(command, str) and os.path.isabs(command):
        command_dir = os.path.dirname(command)

    if os.name == "nt":
        system_root = _system_root()
        system32 = os.path.join(system_root, "System32")
        path_dirs = [
            system32,
            system_root,
            os.path.join(system32, "Wbem"),
        ]
        if command_dir:
            path_dirs.append(command_dir)
        env.update(
            {
                "SystemRoot": system_root,
                "SystemDrive": _system_drive(system_root),
                "windir": system_root,
                "COMSPEC": os.path.join(system32, "cmd.exe"),
                "PATHEXT": _DEFAULT_PATHEXT,
                "PATH": ";".join(_dedupe_paths(path_dirs)),
            }
        )
    else:
        path_dirs = ["/usr/local/bin", "/usr/bin", "/bin"]
        if command_dir:
            path_dirs.append(command_dir)
        env["PATH"] = ":".join(_dedupe_paths(path_dirs))

    env.update(
        {
            "HOME": home_dir,
            "TEMP": tmp_dir,
            "TMP": tmp_dir,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
    )
    if os.name == "nt":
        env["USERPROFILE"] = home_dir
        env["APPDATA"] = home_dir
        env["LOCALAPPDATA"] = home_dir
    else:
        env["TMPDIR"] = tmp_dir

    if isinstance(allowlist, (list, tuple)):
        for name in allowlist:
            if not isinstance(name, str) or env_name_denied(name):
                continue
            value = os.environ.get(name)
            if isinstance(value, str):
                env[name] = value

    # 只留字符串键值：调用方（Popen）不接受别的类型。
    return {
        key: value
        for key, value in env.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def argv_fingerprint(command: str, args: Sequence[Any]) -> str:
    """审计用的 argv 摘要：**只 hash 命令与 flag，不含 flag 的值/位置参数**。

    整串 argv 的哈希对低熵密钥（如 4 位 PIN）可穷举，等于把秘密写成可离线爆破的
    摘要；只取 flag 足以发现"配置换过参数形态"，又不把值带进只增账（§2.7）。
    """
    flags = [arg for arg in (args or []) if str(arg).startswith("-")]
    payload = json.dumps([command, *flags], ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def command_basename(command: str) -> str:
    """审计字段用：命令的文件名部分；拿不到就返回空串。"""
    if not isinstance(command, str):
        return ""
    return os.path.basename(command)


# --------------------------------------------------------------------------- 启动前判据


def _validated_entry(entry: Any) -> Dict[str, Any]:
    """跑一遍写入口的全部判据（含文件存在性），返回规范化五键 dict。

    先单独判 command 是为了让错误原因稳定为 ``command:<problem>``；随后
    ``normalize_entry`` 再收 args / env_allowlist / enabled 的形状与未知键
    （``cwd``、``env``、``timeout`` 都在这里整条拒）。
    """
    if not isinstance(entry, dict):
        raise _config_error("not_an_object")
    problem = command_problem(entry.get("command"), verify_file=True)
    if problem is not None:
        raise _config_error("command:" + problem)
    normalized, reason = normalize_entry(entry, verify_command=True)
    if normalized is None:
        raise _config_error(reason or "invalid_entry")
    return normalized


def validate_spawn_entry(entry) -> None:
    """spawn 前的复查：命令判据 + 文件存在性 + args/env/enabled 形状。

    失败抛 :class:`MCPStdioConfigError`（终态，不重试），原因只带短码。
    """
    _validated_entry(entry)


# --------------------------------------------------------------------------- Job Object


def _apply_job_limits(
    job: Any,
    *,
    process_memory_bytes: int,
    job_memory_bytes: int,
    active_processes: int,
    cpu_time_100ns: int,
) -> None:
    """设置 Job 限额；失败抛 OSError（由调用方降级为 ``job_attached=False``）。"""
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    basic = info.BasicLimitInformation
    basic.LimitFlags = (
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | JOB_OBJECT_LIMIT_JOB_MEMORY
        | JOB_OBJECT_LIMIT_JOB_TIME
    )
    basic.PerJobUserTimeLimit = int(cpu_time_100ns)
    basic.ActiveProcessLimit = int(active_processes)
    info.ProcessMemoryLimit = int(process_memory_bytes)
    info.JobMemoryLimit = int(job_memory_bytes)
    if not _kernel32.SetInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise OSError(0, "SetInformationJobObject failed", ctypes.get_last_error())


def _create_job_for(
    proc: subprocess.Popen,
    *,
    process_memory_bytes: int,
    job_memory_bytes: int,
    active_processes: int,
    cpu_time_100ns: int,
) -> tuple:
    """创建并分配 Job；成功返回 ``(handle, None)``，失败 ``(None, 短错误)``。"""
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        return None, "create_failed:%d" % ctypes.get_last_error()
    try:
        _apply_job_limits(
            job,
            process_memory_bytes=process_memory_bytes,
            job_memory_bytes=job_memory_bytes,
            active_processes=active_processes,
            cpu_time_100ns=cpu_time_100ns,
        )
        if not _kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))):
            raise OSError(0, "AssignProcessToJobObject failed", ctypes.get_last_error())
    except OSError as exc:
        code = exc.errno or ctypes.get_last_error()
        _kernel32.CloseHandle(job)
        return None, "attach_failed:%d" % int(code or 0)
    return job, None


def query_job_limits(job) -> Dict[str, int]:
    """读回 Job 限额（测试/状态用）；``job is None`` 或非 Windows 返回空 dict。"""
    if job is None or os.name != "nt":
        return {}
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    if not _kernel32.QueryInformationJobObject(
        job,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
        None,
    ):
        return {}
    basic = info.BasicLimitInformation
    return {
        "limit_flags": int(basic.LimitFlags),
        "active_process_limit": int(basic.ActiveProcessLimit),
        "process_memory_limit": int(info.ProcessMemoryLimit),
        "job_memory_limit": int(info.JobMemoryLimit),
        "per_job_user_time_limit": int(basic.PerJobUserTimeLimit),
        "per_process_user_time_limit": int(basic.PerProcessUserTimeLimit),
    }


def is_pid_alive(pid) -> bool:
    """PID 是否还活着（测试/运维用）。

    Windows 不用 ``os.kill(pid, 0)``：它在 Windows 上对任意 pid 都可能返回成功或
    直接杀进程。改用 ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`` +
    ``GetExitCodeProcess``；POSIX 用 ``os.kill(pid, 0)`` 并区分
    ``ProcessLookupError`` / ``PermissionError``。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # 拒绝访问说明进程存在但不归我们查：按活处理，宁可多等一轮。
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


# --------------------------------------------------------------------------- 进程包装


class StdioProcess:
    """一个 stdio server 进程 + 它的 Job 句柄。

    属性：
    - ``pid`` / ``proc``：``Popen`` 的 pid 与对象；
    - ``job``：Job 句柄（未附加/已关闭为 ``None``）；
    - ``job_attached``：spawn 时是否成功分配进 Job；
    - ``job_error``：Job 失败时的短原因（可观测，不含路径/参数）。

    方法：``poll`` / ``wait`` / ``terminate_tree``（幂等）/ ``kill``（= ``terminate_tree``，
    满足 T2 客户端对进程对象的形状要求）。不启动任何 reader 线程。
    """

    def __init__(self, proc: subprocess.Popen, job=None, job_error: Optional[str] = None):
        self.proc = proc
        self.pid = proc.pid
        self.job = job
        self.job_attached = job is not None
        self.job_error = job_error
        self._lock = threading.Lock()
        self._terminated = False

    # T2 客户端直接读这些属性；包装层不复制管道。
    @property
    def stdin(self):
        return self.proc.stdin

    @property
    def stdout(self):
        return self.proc.stdout

    @property
    def stderr(self):
        return self.proc.stderr

    def poll(self):
        return self.proc.poll()

    def wait(self, timeout: Optional[float] = None):
        """等进程退出；成功收尸后关 Job 句柄（``KILL_ON_JOB_CLOSE`` 再清一次子树）。"""
        result = self.proc.wait(timeout=timeout)
        self._close_job()
        return result

    def kill(self) -> bool:
        """T2 客户端的 ``proc.kill()`` 入口：等价于 ``terminate_tree``（杀整棵树）。"""
        return self.terminate_tree()

    def _close_job(self) -> None:
        with self._lock:
            self._close_job_locked()

    def _close_job_locked(self) -> None:
        job = self.job
        if job is None:
            return
        self.job = None
        if os.name == "nt":
            try:
                _kernel32.CloseHandle(job)
            except Exception:  # noqa: BLE001 - 收尾不抛
                pass

    def terminate_tree(self, timeout: float = 5.0) -> bool:
        """杀整棵进程树；幂等，绝不把异常抛给调用方。

        顺序（计划 §3.6）：``TerminateJobObject`` → ``taskkill /F /T /PID`` →
        ``proc.kill()`` → ``proc.wait(timeout)`` → 关 Job 句柄。POSIX 用
        ``os.killpg(os.getpgid(pid), SIGKILL)``。返回进程是否已不在。
        """
        with self._lock:
            proc = self.proc
            if os.name == "nt":
                if self.job is not None:
                    try:
                        _kernel32.TerminateJobObject(self.job, 1)
                    except Exception:  # noqa: BLE001 - 兜底
                        pass
                self._taskkill_locked()
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
                self._close_job_locked()
            else:
                try:
                    os.killpg(os.getpgid(self.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
            self._terminated = True
            return proc.poll() is not None

    def _taskkill_locked(self) -> None:
        """第二道：固定路径的 taskkill，不经过 shell，不把输出带回来。"""
        system_root = _system_root()
        taskkill = os.path.join(system_root, "System32", "taskkill.exe")
        try:
            subprocess.run(
                [taskkill, "/F", "/T", "/PID", str(self.pid)],
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass


# --------------------------------------------------------------------------- spawn


def spawn_stdio(
    entry,
    mcp_root: str,
    *,
    process_memory_bytes: int = DEFAULT_PROCESS_MEMORY_BYTES,
    job_memory_bytes: int = DEFAULT_JOB_MEMORY_BYTES,
    active_processes: int = DEFAULT_ACTIVE_PROCESSES,
    cpu_time_100ns: int = DEFAULT_CPU_TIME_100NS,
    create_job: bool = True,
) -> StdioProcess:
    """校验配置并启动一个 stdio server 进程（不启动 reader 线程）。

    - 先 :func:`validate_spawn_entry`（含文件存在性）；失败抛
      :class:`MCPStdioConfigError`；
    - ``cwd`` 固定 ``<mcp_root>/<id>``；**不接受 cwd 参数**（T1 已整条拒该键）；
    - Windows：``CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP``，随后建 Job 并分配；
      Job 失败只记 ``job_attached=False``，不拒绝启动；
    - POSIX：``start_new_session=True``，杀树走 ``killpg``。
    """
    normalized = _validated_entry(entry)
    server_id = normalized["id"]
    command = normalized["command"]
    args = normalized["args"]

    root = os.path.abspath(os.fspath(mcp_root))
    cwd = os.path.join(root, server_id)
    _makedirs_private(cwd)

    env_plan = build_env_plan(server_id, normalized["env_allowlist"], root, command=command)

    kwargs: Dict[str, Any] = {
        "shell": False,
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": cwd,
        "env": env_plan,
        "bufsize": -1,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen([command, *args], **kwargs)
    except OSError as exc:
        # WinError 2/3/5（找不到/路径/拒绝访问）是配置错误，终态；其余（资源耗尽等）
        # 交给 supervisor 按 transport_error 退避（§3.2）。
        winerror = getattr(exc, "winerror", None)
        if os.name == "nt" and winerror in (2, 3, 5):
            raise _config_error("spawn_failed:%d" % int(winerror)) from exc
        raise

    job = None
    job_error = None
    if os.name == "nt" and create_job:
        job, job_error = _create_job_for(
            proc,
            process_memory_bytes=int(process_memory_bytes),
            job_memory_bytes=int(job_memory_bytes),
            active_processes=int(active_processes),
            cpu_time_100ns=int(cpu_time_100ns),
        )
    return StdioProcess(proc, job=job, job_error=job_error)
