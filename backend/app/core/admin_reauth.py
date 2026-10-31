"""管理员二次验证票据（H-1）：高危写操作前要重新输一次口令。

票据只在进程内存里，默认 5 分钟；不落盘、不进日志。它解决的是"管理员会话被
别人拿到后可以直接改配置/开 MCP stdio"，不是替代登录——登录仍然决定"你是谁"，
这张票只决定"这次高危动作是不是本人刚点头"。

Cookie 路径必须是 `/v1`：高危管理写口不只在 `/v1/admin/` 前缀下，管理员专属的
provider / search-key / memory-decay 写口也要同一张票；收在 `/v1/admin` 会让这些
端点永远收不到票，等于留一条绕过路。票据只活 5 分钟、HttpOnly + SameSite=Strict，
且单独一张票不构成任何身份，没它会话 Cookie 也做不了写操作。前端拿到 428 后弹一次
口令框，换到 Cookie 再重试原请求。票据与 user_id 绑定，换身份不会继承。
"""
import hashlib
import secrets
import threading
import time

TICKET_TTL_SECONDS = 300
COOKIE_NAME = "admin_reauth"
COOKIE_PATH = "/v1"
_MAX_TICKETS = 4096

_TICKETS = {}                       # digest -> (user_id, expires_monotonic)
_LOCK = threading.Lock()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _purge_locked(now: float) -> None:
    for key in [k for k, (_, exp) in _TICKETS.items() if exp <= now]:
        _TICKETS.pop(key, None)


def issue(user_id: str) -> str:
    if not user_id:
        raise ValueError("user_id 不能为空")
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _LOCK:
        _purge_locked(now)
        if len(_TICKETS) >= _MAX_TICKETS:
            # 票据表只增不减才会成为内存问题；过期清完还满就丢掉最早的一枚。
            _TICKETS.pop(next(iter(_TICKETS)), None)
        _TICKETS[_digest(token)] = (user_id, now + TICKET_TTL_SECONDS)
    return token


def valid(token: str, user_id: str) -> bool:
    if not token or not user_id:
        return False
    now = time.monotonic()
    with _LOCK:
        hit = _TICKETS.get(_digest(token))
        if hit is None:
            return False
        owner, expires = hit
        if expires <= now or owner != user_id:
            _TICKETS.pop(_digest(token), None)
            return False
        return True


def reset_for_tests() -> None:
    with _LOCK:
        _TICKETS.clear()
