"""运行时配置：一个 data/config.json + 默认值 + 环境变量口子。

v0.24 之前"配置"散在三处：注册永远开放写在路由里没有开关、更新仓库地址硬编码在
releases.py、注册开关的口径只存在于文档。这个模块把它们收进一份能落盘、能改、
能重启后还在的文件——与 providers/usage/schedule 同一套形状：读时合并默认值，
写时公共原子落盘（core/atomic_write），读不懂的记录先备份 .corrupt 再退回默认。

两个环境变量的口径（与其余存储一致：env 优先于文件）：
- CONFIG_DB_PATH：文件落点，测试与多实例靠它指走。
- REGISTRATION_OPEN：显式设了就压过文件里的值。这不是后门，是部署入口——
  docker-compose 里给一颗环境变量，比先起服务再调管理端点更省事；也保证
  文件被误删时"注册关着"这个安全默认不会因为读不到文件而翻成开。
"""
import json
import os
import threading
from typing import Tuple

from app.core.atomic_write import write_json_atomic
from app.core.paths import data_root

# 安全默认：开源版就是"给你自己用的"，注册默认关；想开放，管理端点或 env 说一声。
# 更新仓库默认指向新快照仓；旧仓名由 releases.py 作为回落常量单独管着（老用户
# 的 v0.23.x 更新链在改名后不能断，那是 releases 的事，不是这里的第二真相）。
DEFAULTS = {
    "registration_open": False,
    "update_repo": "abonla599/fenver",
}

# 允许被 POST /v1/admin/config 改写的键。白名单而不是全接收：配置面将来加到
# 会话存储、供应商地址这类东西上时，忘了改这张表的管理端点就等于开了个任意写口子。
MUTABLE_KEYS = frozenset(DEFAULTS)

_lock = threading.RLock()
_config = dict(DEFAULTS)


def _default_path() -> str:
    env_path = os.getenv("CONFIG_DB_PATH")
    if env_path:
        return os.path.abspath(env_path)
    return os.path.join(data_root(), "data", "config.json")


def _env_registration_open():
    """REGISTRATION_OPEN 设了就返回布尔，没设返回 None（表示"文件说了算"）。"""
    raw = os.getenv("REGISTRATION_OPEN", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def restore(path: str = None) -> int:
    """从盘上读回配置（启动时一次；测试用它等价于"重启进程"）。返回读到的条数。"""
    target = os.path.abspath(path or _default_path())
    with _lock:
        _config.clear()
        _config.update(DEFAULTS)
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return 0
        except (ValueError, OSError) as e:
            backup = target + ".corrupt"
            try:
                os.replace(target, backup)
                print(f"⚠️ 配置文件读不了（{e}），已备份为 {backup}，按默认值跑")
            except OSError:
                print(f"⚠️ 配置文件读不了且无法备份（{e}），按默认值跑")
            return 0
        if not isinstance(data, dict):
            return 0
        count = 0
        for key in DEFAULTS:
            if key in data and isinstance(data[key], type(DEFAULTS[key])):
                _config[key] = data[key]
                count += 1
            elif key in data:
                # 形状不对的一条不入库，但要说出来：静默吞掉会让人以为
                # "我改过了怎么没生效"，其实是那行 JSON 写坏了类型。
                print(f"⚠️ 配置项 {key} 的类型不对，按默认值跑")
        return count


def _write() -> None:
    write_json_atomic(_default_path(), _config)


def read() -> dict:
    """当前生效的配置（env 已合并）。每次现读 env，改了不用重启——与 AUTH_MODE 同口径。"""
    with _lock:
        out = dict(_config)
    env_open = _env_registration_open()
    if env_open is not None:
        out["registration_open"] = env_open
    return out


def registration_open() -> bool:
    return bool(read()["registration_open"])


def update_repo() -> str:
    repo = str(read()["update_repo"] or "").strip()
    return repo or DEFAULTS["update_repo"]


def apply_update(partial: dict) -> Tuple[dict, list]:
    """合并管理端的改动并落盘。返回 (改后的完整配置, 被拒的键列表)。

    未知键不写入也不报错——回一份"哪些没被接受"比 422 整单拒绝友好，因为
    界面上"保存"的人想知道的是改成了什么；类型不对同样拒，但只拒那一个键。
    """
    rejected = []
    with _lock:
        for key, value in (partial or {}).items():
            if key not in MUTABLE_KEYS:
                rejected.append(key)
                continue
            if not isinstance(value, type(DEFAULTS[key])):
                rejected.append(key)
                continue
            _config[key] = value
        _write()
        out = dict(_config)
    env_open = _env_registration_open()
    if env_open is not None:
        out["registration_open"] = env_open
    return out, rejected


def log_config_summary() -> None:
    """启动时一行，说人话：注册开没开、更新去哪儿找。出事的人在看日志，不用猜。"""
    cfg = read()
    reg = "开放" if cfg["registration_open"] else "关闭（默认，管理员建号）"
    env_hit = _env_registration_open() is not None
    note = "，由环境变量 REGISTRATION_OPEN 决定" if env_hit else ""
    print(f"⚙️ 运行时配置：注册{reg}{note}；更新仓库 {cfg['update_repo']}"
          f"（文件 {_default_path()}）")


restore()
