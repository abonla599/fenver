"""v0.37.5 T6：MCP stdio 的管理端点（配置面 / 状态面 / 启停面）。

四条路由全部挂在现有 ``/v1/admin/*`` 之下，不新增顶层前缀；每条都声明
``RequireAdmin``。计划 §4.4 的三个"不返回"在这里是硬约束：

* 状态快照只投影固定字段集合——**绝不**回 ``args``、env 值或 stderr 正文；
* 错误 ``detail`` 只用本模块的固定短码，不拼接第三方值（配置里可能写着密钥）；
* 写入口先 ``normalize_entry(..., verify_command=True)`` 再落盘，坏条目整条拒。

读-改-写必须串行：``config_store.apply_update`` 的锁只覆盖它自己那次写入，
不覆盖"读当前表 → 替换/追加 → 写回"这一整段。两个管理员同时 PUT 时，后写的
那份会覆盖前一份（一次静默丢配置），所以这里用模块级 ``_WRITE_LOCK`` 包住整段。
"""
from __future__ import annotations

import copy
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.core import audit, config_store
from app.core.authz import Principal, RequireAdmin
from app.core.config_store import MCP_ENABLED_KEY, MCP_SERVERS_KEY
from app.core.mcp_stdio_config import ID_RE, MAX_SERVERS, normalize_entry
from app.core.mcp_stdio_process import argv_fingerprint

router = APIRouter()

# 读-改-写整段串行：一次覆盖就是静默丢配置，不能靠"单管理员部署应该不会"。
_WRITE_LOCK = threading.Lock()

# 条目允许的键（与 mcp_stdio_config._ALLOWED_ENTRY_KEYS 同一集合）。这里先判一遍
# 是为了不把未知键名拼进错误详情（normalize_entry 的 unsupported_field 会带键名）。
_ENTRY_KEYS = frozenset({"id", "command", "args", "env_allowlist", "enabled"})
_SETTINGS_KEYS = frozenset({"enabled"})

# 状态投影的固定字段（计划 §4.4）。args / env 值 / stderr 正文不在其中，而且哪怕
# 将来 supervisor.status() 多带了别的东西，这道投影也不会把它放出去。
_SERVER_STATUS_KEYS = (
    "id", "enabled", "state", "pid", "restarts", "uptime_s",
    "tools_total", "tools_exposed", "schema_flags",
    "command", "command_basename", "argv_fingerprint",
    "env_names", "cwd", "last_error",
)

DETAIL_INVALID_BODY = "invalid_body"
DETAIL_UNSUPPORTED_FIELD = "unsupported_field"
DETAIL_ID_MISMATCH = "id_mismatch"
DETAIL_TOO_MANY_SERVERS = "too_many_servers"
DETAIL_CONFIG_REJECTED = "config_rejected"
DETAIL_SETTINGS_SHAPE = "settings_must_be_enabled_bool"
DETAIL_SERVER_NOT_FOUND = "server_not_found"
DETAIL_NOT_INITIALIZED = "MCP stdio 未初始化"


# --------------------------------------------------------------------------- 取口


def _supervisor_for(request: Request):
    """只认 lifespan 装上的实例。

    这里**不再**回退 ``get_supervisor()``：lifespan 构造失败时管理面若替它造一个
    单例，这个实例没人 attach/start，退出时也没人 shutdown——后续启服还能读到
    它，跨重启残留。没装上就是 503，把"未初始化"如实回给管理员。
    """
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    supervisor = getattr(state, "mcp_supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail=DETAIL_NOT_INITIALIZED)
    return supervisor


def _public_status(supervisor) -> Dict[str, Any]:
    """把 supervisor 快照收成固定字段集合；异常类型名之外不带任何第三方文本。"""
    status = supervisor.status()
    if not isinstance(status, dict):
        status = {}
    servers: List[Dict[str, Any]] = []
    raw_servers = status.get("servers")
    if isinstance(raw_servers, list):
        for item in raw_servers:
            if isinstance(item, dict):
                servers.append(copy.deepcopy(
                    {key: item.get(key) for key in _SERVER_STATUS_KEYS}))
    return {"enabled": bool(status.get("enabled")), "servers": servers}


def _server_status(supervisor, server_id: str,
                   entry: Optional[dict] = None,
                   fallback_state: str = "unknown") -> Dict[str, Any]:
    """该条目的状态；supervisor 还没建运行态时给一个诚实的合成快照。"""
    try:
        status = _public_status(supervisor)
    except Exception as exc:                                 # noqa: BLE001 - 状态不连累写结果
        print(f"⚠️ MCP stdio 状态读取失败: {type(exc).__name__}")
        status = {}
    for item in status.get("servers") or []:
        if item.get("id") == server_id:
            return item
    # 运行态还没建时也返回与 GET 相同的键集合：PUT / DELETE 的响应形状不该因为
    # "supervisor 还没看见这条"而少键，管理台才能用同一份渲染。
    fallback = {key: None for key in _SERVER_STATUS_KEYS}
    fallback.update({
        "id": server_id,
        "enabled": bool((entry or {}).get("enabled")),
        "state": fallback_state,
    })
    return fallback


# --------------------------------------------------------------------------- 小工具


def _reload(supervisor) -> None:
    """重读配置；失败只打印类型名，绝不把异常正文回给客户端。"""
    try:
        supervisor.reload()
    except Exception as exc:                                 # noqa: BLE001 - 启动失败不连累响应
        print(f"⚠️ MCP stdio reload 失败: {type(exc).__name__}")


def _safe_entry_reason(reason: Any) -> str:
    """错误详情的短码：白名单形状，未知键名一律折叠成 unsupported_field。"""
    text = str(reason or "").strip()
    if not text or len(text) > 64:
        return "invalid_entry"
    if text.startswith("unsupported_field"):
        return DETAIL_UNSUPPORTED_FIELD
    return text


def _current_entries() -> List[dict]:
    """当前配置表（逐条浅拷贝；read() 已保证是深副本）。"""
    current = config_store.read()
    raw = current.get(MCP_SERVERS_KEY)
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _summary(entries: List[dict]) -> List[dict]:
    """审计摘要：id / enabled / argv 指纹，绝不带 args、env 值或完整命令行。"""
    out = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        args = item.get("args")
        out.append({
            "id": item.get("id") if isinstance(item.get("id"), str) else "?",
            "enabled": bool(item.get("enabled")),
            "argv_fingerprint": argv_fingerprint(
                command if isinstance(command, str) else "",
                args if isinstance(args, list) else [],
            ),
        })
    return out


def _content_type_is_json(request: Request) -> bool:
    """Content-Type 必须是 application/json（允许 ``; charset=...`` 参数）。

    只看媒体类型本身：``application/json-patch+json`` 这类不算，``text/plain``
    也不是——之前直接 ``request.json()`` 会照收，等于把"发错格式却写进配置"的
    请求当成了合法写入。
    """
    raw = request.headers.get("content-type", "")
    media_type = raw.split(";", 1)[0].strip().lower()
    return media_type == "application/json"


async def _json_object(request: Request) -> Dict[str, Any]:
    """请求体必须是 JSON 对象；格式不对、形状不对都回同一句 400。"""
    if not _content_type_is_json(request):
        raise HTTPException(status_code=400, detail=DETAIL_INVALID_BODY)
    try:
        body = await request.json()
    except Exception:                                        # noqa: BLE001 - 客户端输入
        raise HTTPException(status_code=400, detail=DETAIL_INVALID_BODY)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail=DETAIL_INVALID_BODY)
    return body


# --------------------------------------------------------------- 同步的写事务
# 加锁→读-改-写→审计→reload 整段放在同步函数里，由端点用 run_in_threadpool 调。
# reload 会停子进程（每台最多 5s+5s），直接跑在事件循环里哪怕只有一台也会把
# /health 和聊天一起冻住；这里是 I/O 与等待，属于线程池。


def _apply_server_update(actor: Principal, server_id: str, entry: dict,
                         supervisor) -> Dict[str, Any]:
    """整条替换并落盘；返回该条目在 reload 之后的公开状态。"""
    with _WRITE_LOCK:
        entries = _current_entries()
        before = _summary(entries)
        replaced = False
        for index, item in enumerate(entries):
            if item.get("id") == server_id:
                entries[index] = entry
                replaced = True
                break
        if not replaced:
            if len(entries) >= MAX_SERVERS:
                raise HTTPException(status_code=400,
                                    detail=DETAIL_TOO_MANY_SERVERS)
            entries.append(entry)

        cfg, rejected = config_store.apply_update({MCP_SERVERS_KEY: entries})
        if MCP_SERVERS_KEY in rejected:
            # apply_update 是整键拒，坏表不会写一半；配置仍是写前的样子。
            raise HTTPException(status_code=400, detail=DETAIL_CONFIG_REJECTED)
        after = _summary(cfg.get(MCP_SERVERS_KEY) or [])
        # server_id 可以是 "settings"（合法 id），与全局开关的审计 target 同名；
        # 加上前缀才能分开记。
        audit.log(actor, "mcp.stdio.config", target=f"server:{server_id}",
                  before=before, after=after)
        _reload(supervisor)
        return _server_status(supervisor, server_id, entry)


def _apply_settings_update(actor: Principal, enabled: bool, supervisor) -> bool:
    """写全局开关并 reload；返回落盘后的实际值。"""
    with _WRITE_LOCK:
        try:
            before_enabled = bool(config_store.read().get(MCP_ENABLED_KEY, False))
        except Exception:                                    # noqa: BLE001 - 审计摘要尽力
            before_enabled = False
        cfg, rejected = config_store.apply_update({MCP_ENABLED_KEY: enabled})
        if MCP_ENABLED_KEY in rejected:
            raise HTTPException(status_code=400, detail=DETAIL_CONFIG_REJECTED)
        after_enabled = bool(cfg.get(MCP_ENABLED_KEY, enabled))
        audit.log(actor, "mcp.stdio.config", target="global",
                  before={"enabled": before_enabled},
                  after={"enabled": after_enabled})
        _reload(supervisor)
        return after_enabled


# --------------------------------------------------------------------------- 端点


@router.get("/v1/admin/mcp/stdio")
async def get_mcp_stdio(request: Request, response: Response,
                        _: Principal = RequireAdmin):
    """当前全局开关与每条 server 的状态摘要。

    ``Cache-Control: no-store``：这份快照会随子进程起落变化，缓存住只会让管理员
    对着一份过期状态排障。
    """
    response.headers["Cache-Control"] = "no-store"
    return _public_status(_supervisor_for(request))


@router.put("/v1/admin/mcp/stdio/servers/{server_id}")
async def put_mcp_stdio_server(server_id: str, request: Request,
                               actor: Principal = RequireAdmin):
    """整条替换（不是 PATCH）：body 不允许 id 以外的新键，也不允许 cwd/env/timeout。

    返回 202：reload 只负责起线程，ready 是异步结果，管理员用 GET 看状态。
    """
    if not isinstance(server_id, str) or not ID_RE.fullmatch(server_id):
        raise HTTPException(status_code=400, detail="invalid_server_id")
    body = await _json_object(request)

    if any(key not in _ENTRY_KEYS for key in body):
        raise HTTPException(status_code=400, detail=DETAIL_UNSUPPORTED_FIELD)
    if "id" in body and body.get("id") != server_id:
        raise HTTPException(status_code=400, detail=DETAIL_ID_MISMATCH)

    candidate = dict(body)
    candidate["id"] = server_id
    entry, reason = normalize_entry(candidate, verify_command=True)
    if entry is None:
        # reason 全部来自 mcp_stdio_config 的固定枚举；不含任何 args/env 的值。
        raise HTTPException(status_code=400,
                            detail="invalid_entry:" + _safe_entry_reason(reason))

    supervisor = _supervisor_for(request)
    # 写事务整体进线程池：reload 的停进程等待不能占着事件循环。
    payload = await run_in_threadpool(
        _apply_server_update, actor, server_id, entry, supervisor)
    return JSONResponse(status_code=202,
                        content={"status": "accepted", "server": payload})


@router.delete("/v1/admin/mcp/stdio/servers/{server_id}")
def delete_mcp_stdio_server(server_id: str, request: Request,
                            actor: Principal = RequireAdmin):
    """删除一条配置；停进程、审计、重新加载配置，再显式兜一次注销。

    ``def`` 而不是 ``async def``：Starlette 本来就把同步端点丢进线程池，reload
    的停进程等待不会占着事件循环。
    """
    supervisor = _supervisor_for(request)
    with _WRITE_LOCK:
        entries = _current_entries()
        remaining = [item for item in entries if item.get("id") != server_id]
        if len(remaining) == len(entries):
            raise HTTPException(status_code=404, detail=DETAIL_SERVER_NOT_FOUND)
        before = _summary(entries)
        cfg, rejected = config_store.apply_update({MCP_SERVERS_KEY: remaining})
        if MCP_SERVERS_KEY in rejected:
            raise HTTPException(status_code=400, detail=DETAIL_CONFIG_REJECTED)
        after = _summary(cfg.get(MCP_SERVERS_KEY) or [])
        audit.log(actor, "mcp.stdio.config", target=f"server:{server_id}",
                  before=before, after=after)
        _reload(supervisor)
        payload = _server_status(supervisor, server_id,
                                fallback_state="disabled")

    # reload 的 disabled 回调与桥接层应已注销；这里显式再兜一次（幂等）。整段
    # 放在锁外：注销只是注册表内的一次遍历，不该拖住其它配置写入。
    try:
        from app.core.mcp_stdio_bridge import SOURCE_PREFIX
        from app.tools.registry import unregister_source
        unregister_source(SOURCE_PREFIX + server_id)
    except Exception as exc:                                 # noqa: BLE001 - 注销不连累删除结果
        print(f"⚠️ MCP stdio 注销工具失败: {type(exc).__name__}")

    return {"status": "deleted", "id": server_id, "server": payload}


@router.put("/v1/admin/mcp/stdio/settings")
async def put_mcp_stdio_settings(request: Request,
                                 actor: Principal = RequireAdmin):
    """全局启停开关。只收 ``{"enabled": bool}``，多一个键都算形状不对。"""
    body = await _json_object(request)
    if set(body) != _SETTINGS_KEYS or not isinstance(body.get("enabled"), bool):
        raise HTTPException(status_code=400, detail=DETAIL_SETTINGS_SHAPE)

    enabled = bool(body["enabled"])
    supervisor = _supervisor_for(request)
    after_enabled = await run_in_threadpool(
        _apply_settings_update, actor, enabled, supervisor)
    return {"enabled": after_enabled}
