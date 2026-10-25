"""MCP stdio 配置面的纯逻辑：常量、条目消毒、命令/环境判据。

这一层刻意不碰 `config_store`（避免循环导入），也不在导入期读盘：
- `normalize_entry` / `normalize_servers` 是**写入口**的判据，`verify_command=True`
  时才用 `os.path.isfile` 判命令存在性；
- `sanitize_servers` 是**还原路径**（`config_store.restore()` 在 import 期调用）
  的消毒：零 raise、绝不碰磁盘，只做形状判断，坏条目逐条丢弃并打印 id + 原因。

安全口径：stdio server 是本机子进程，等于"管理员授权以服务账号权限运行的代码"。
所以这里的每一个判据都是 fail-closed——拿不准就当坏，宁可不启动。错误串只放短码，
绝不回显 command/args/env 的值（配置里可能写着密钥）。
"""
import json
import os
import re
import unicodedata

MCP_ENABLED_KEY = "mcp_stdio_enabled"
MCP_SERVERS_KEY = "mcp_stdio_servers"

MAX_SERVERS = 3
MAX_ARGS = 32
ARG_MAX_CHARS = 1024
ARGS_TOTAL_MAX = 8192
MAX_ENV_ALLOWLIST = 16
MAX_SERVERS_BYTES = 32 * 1024
COMMAND_MAX_CHARS = 260

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# 计划 §2.4 的硬拒清单：名字里出现这些片段就不许从服务进程环境里复制值过去。
# 大小写不敏感；`DYLD_` 是前缀，其余按子串匹配（`_TOKEN` 同时覆盖 `GITHUB_TOKEN`，
# `AUTH` 同时覆盖 `OAUTH_*`——宁可误伤也不把凭据递给第三方代码）。
_ENV_DENY_PARTS = (
    "_API_KEY", "APIKEY", "ACCESS_TOKEN", "_TOKEN", "TOKEN_",
    "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "COOKIE", "SESSION",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "_PROXY",
    "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "DYLD_",
)

# 宿主解释器/外壳：配置里写了也不启动。Windows 名单按 basename 小写精确匹配；
# POSIX 名单没有扩展名（`sh` 等），同一份判据在两条平台上各用各的表。
_WINDOWS_HOST_DENY = frozenset({
    "cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe", "cscript.exe",
    "mshta.exe", "rundll32.exe", "regsvr32.exe", "bash.exe", "sh.exe",
    "wsl.exe", "conhost.exe", "explorer.exe",
})
_POSIX_HOST_DENY = frozenset({"sh", "bash", "dash", "zsh", "cmd", "powershell", "pwsh"})

_ALLOWED_ENTRY_KEYS = frozenset({"id", "command", "args", "env_allowlist", "enabled"})


def env_name_denied(name) -> bool:
    """这个名字是不是被硬拒的密钥/代理/注入点（大小写不敏感）。"""
    if not isinstance(name, str):
        return True
    upper = name.upper()
    return any(part in upper for part in _ENV_DENY_PARTS)


def _has_control_chars(text: str) -> bool:
    """Cc（含 NUL/CR/LF/TAB）与 Cf（零宽、双向覆盖）都算控制字符。

    TAB 也拒：命令与参数里出现它没有合法的 argv 语义，只会是粘贴事故或注入尝试。
    """
    return any(unicodedata.category(ch) in ("Cc", "Cf") for ch in text)


def command_problem(command, *, verify_file: bool) -> str | None:
    """命令判据：返回短错误码，合法返回 None。绝不回显参数值。

    `verify_file=False` 是 restore 路径，**绝不碰磁盘**：`config_store.restore()`
    跑在 import 期，一个指向慢盘/网络盘的路径不该把启动拖死；存在性由 spawn 前
    的写入口/启动复查判。
    """
    if not isinstance(command, str):
        return "not_a_string"
    if not command.strip():
        return "empty"
    if len(command) > COMMAND_MAX_CHARS:
        return "too_long"
    if _has_control_chars(command):
        return "control_chars"
    if not os.path.isabs(command):
        return "not_absolute"
    # UNC（\\server\share）与设备命名空间（\\?\、\\.\）都以两个分隔符开头；
    # 双斜杠写法在 POSIX 上同样拒——它不是"绝对路径的另一种写法"，是另一台机器。
    if command.startswith("\\\\") or command.startswith("//"):
        return "unc_or_device_path"
    name = os.path.basename(command).lower()
    if os.name == "nt":
        if not name.endswith(".exe"):
            return "not_exe"
        if name in _WINDOWS_HOST_DENY:
            return "host_denied"
    else:
        if name in _POSIX_HOST_DENY:
            return "host_denied"
    if verify_file:
        if not os.path.isfile(command):
            return "missing_or_not_file"
        if os.name != "nt" and not os.access(command, os.X_OK):
            return "not_executable"
    return None


def normalize_entry(raw, *, verify_command: bool):
    """把一条 server 配置收成规范化的五键 dict；坏条目返回 (None, 短原因)。

    未知键**整条拒**（包括 `cwd`/`env`/`timeout`）：固定 cwd、最小环境是安全边界，
    静默忽略一个"看起来生效了"的键比拒绝更危险。
    """
    if not isinstance(raw, dict):
        return None, "not_an_object"

    unknown = sorted(str(key) for key in raw if key not in _ALLOWED_ENTRY_KEYS)
    if unknown:
        return None, "unsupported_field:" + ",".join(unknown)

    server_id = raw.get("id")
    if not isinstance(server_id, str) or not ID_RE.match(server_id):
        return None, "bad_id"

    command = raw.get("command")
    problem = command_problem(command, verify_file=verify_command)
    if problem is not None:
        return None, "command:" + problem

    args = raw.get("args", [])
    if not isinstance(args, list):
        return None, "args_not_a_list"
    if len(args) > MAX_ARGS:
        return None, "args_too_many"
    total_chars = 0
    for arg in args:
        if not isinstance(arg, str):
            return None, "arg_not_a_string"
        if len(arg) > ARG_MAX_CHARS:
            return None, "arg_too_long"
        if _has_control_chars(arg):
            return None, "arg_control_chars"
        total_chars += len(arg)
    if total_chars > ARGS_TOTAL_MAX:
        return None, "args_total_too_long"

    env_allowlist = raw.get("env_allowlist", [])
    if not isinstance(env_allowlist, list):
        return None, "env_allowlist_not_a_list"
    if len(env_allowlist) > MAX_ENV_ALLOWLIST:
        return None, "env_allowlist_too_many"
    for env_name in env_allowlist:
        if not isinstance(env_name, str) or not ENV_NAME_RE.match(env_name):
            return None, "env_name_invalid"
        if env_name_denied(env_name):
            return None, "env_name_denied"

    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        return None, "enabled_not_bool"

    return {
        "id": server_id,
        "command": command,
        "args": list(args),
        "env_allowlist": list(env_allowlist),
        "enabled": enabled,
    }, None


def _table_size_error(raw: list) -> str | None:
    try:
        encoded = json.dumps(raw, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return "not_serializable"
    if len(encoded) > MAX_SERVERS_BYTES:
        return "table_too_large"
    return None


def normalize_servers(raw, *, verify_command: bool):
    """整表判：任何一条坏 → 返回 ([], 短原因列表)，**不做逐条过滤**。

    管理端写入要的是"这份清单没被接受"，而不是"哪一条没进"——配了一半的启动
    清单没有可解释的运行语义。restore 路径的逐条丢弃由 `sanitize_servers` 负责。
    """
    if not isinstance(raw, list):
        return [], ["not_a_list"]
    if len(raw) > MAX_SERVERS:
        return [], ["too_many_servers"]
    size_error = _table_size_error(raw)
    if size_error is not None:
        return [], [size_error]

    out, errors, seen = [], [], set()
    for index, item in enumerate(raw):
        entry, reason = normalize_entry(item, verify_command=verify_command)
        if entry is None:
            errors.append(f"#{index}:{reason}")
            continue
        if entry["id"] in seen:
            errors.append(f"#{index}:duplicate_id")
            continue
        seen.add(entry["id"])
        out.append(entry)
    if errors:
        return [], errors
    return out, []


def sanitize_servers(raw) -> list:
    """还原路径的消毒：零 raise、不碰磁盘，坏条目逐条丢弃、重复 id 丢后者、截断到 3 条。

    丢弃时只打印 id（拿不到写 `?`）与原因短码；**绝不打印 command/args/env 的值**。
    这一支跑在 `config_store.restore()`（导入期）里，抛出去就是现网起不来，所以
    整个循环兜住 Exception——它不是"吞掉实现 bug"，而是这条路径的硬要求。
    """
    if not isinstance(raw, list):
        print("⚠️ 配置项 mcp_stdio_servers 不是列表，按空表跑")
        return []

    out, seen = [], set()
    for item in raw:
        entry_id = item.get("id") if isinstance(item, dict) else None
        label = entry_id if isinstance(entry_id, str) and entry_id else "?"
        if len(out) >= MAX_SERVERS:
            print(f"⚠️ 配置项 mcp_stdio_servers 超过 {MAX_SERVERS} 条上限，"
                  f"丢弃条目 {label}: too_many_servers")
            continue
        try:
            entry, reason = normalize_entry(item, verify_command=False)
        except Exception as exc:                       # noqa: BLE001 - 导入期零 raise
            entry, reason = None, "normalize_error:" + type(exc).__name__
        if entry is None:
            print(f"⚠️ 配置项 mcp_stdio_servers 丢弃条目 {label}: {reason}")
            continue
        if entry["id"] in seen:
            print(f"⚠️ 配置项 mcp_stdio_servers 丢弃重复条目 {label}: duplicate_id")
            continue
        seen.add(entry["id"])
        out.append(entry)
    return out
