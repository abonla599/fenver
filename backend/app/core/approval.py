"""审批层最小闭环：计划-确认-执行。

v0.31 新增。仅 `execute_code` 标了 `requires_approval=True`，其他工具直通。
确认凭据走服务端一次性票据，客户端不回传凭据（票据 ID 由端点持有）。
超时/断连 → 票据作废，绝不默认放行。

接口形状（安卓与网页端等这份定稿）：

确认请求（服务端 → 客户端）：
{
    "ticket_id": "uuid-v4",
    "tool_name": "execute_code",
    "plan_summary": "执行一段 python 代码（前 80 字预览）",
    "created_at": 1728000000.0,
    "expires_at": 1728000060.0,
    "user_id": "u_xxx"
}

确认动作（客户端 → 服务端）：
POST /v1/approval/confirm
Body: {"ticket_id": "uuid-v4", "decision": "allow" | "reject"}

双端文案（契约测试钉死，漂一个字就红）：
- 标题：「执行代码确认」
- 正文：「模型想要执行一段 {language} 代码」
- 代码预览：代码前 80 字（超出省略）
- 按钮：「允许」/「拒绝」
"""
import json
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Dict, Optional

from app.core.providers import scrub_secrets
from app.tools.registry import tools_registry


# 票据默认有效期（秒）。弱网移动端「弹了没人回」的最坏情况：60 秒后作废，不默认放行。
APPROVAL_TIMEOUT_SECONDS = 60


@dataclass
class ApprovalTicket:
    """一次性审批票据。服务端持有，客户端只拿到 ticket_id。"""
    ticket_id: str
    tool_name: str
    arguments: dict
    plan_summary: str
    user_id: str
    created_at: float
    expires_at: float
    consumed: bool = False
    decision: Optional[str] = None  # "allow" | "reject" | None (pending)


# ---------- 全局票据存储 ----------
_lock = threading.Lock()
_tickets: Dict[str, ApprovalTicket] = {}
# 单轮熔断：(user_id, tool_name) → 本轮拒绝后不再弹确认
_round_fused: Dict[tuple, float] = {}  # value = 过期时间戳


def _generate_ticket_id() -> str:
    return str(uuid.uuid4())


def needs_approval(tool_name: str) -> bool:
    """该工具是否需要审批。从注册表读 requires_approval 标记。"""
    info = tools_registry.get(tool_name)
    if not info:
        return False
    return bool(info.get("requires_approval"))


def generate_plan_summary(tool_name: str, arguments: dict) -> str:
    """服务端生成计划摘要（不信任模型自述）。

    摘要只取关键参数的头 80 字，过 scrub_secrets 出口（与 _fail_reason 同口径）。
    """
    safe_args = scrub_secrets(json.dumps(arguments, ensure_ascii=False, sort_keys=True))
    if tool_name == "execute_code":
        code = str(arguments.get("code", ""))[:80]
        lang = arguments.get("language", "python")
        code_preview = scrub_secrets(code)
        if len(str(arguments.get("code", ""))) > 80:
            code_preview += "…"
        return f"执行一段 {lang} 代码：{code_preview}"
    # 兜底：工具名 + 参数摘要
    return f"调用 {tool_name}（{safe_args[:120]}）"


def create_approval_request(tool_name: str, arguments: dict,
                            user_id: str) -> Dict:
    """创建一条审批请求。返回给客户端的确认卡片数据。

    票据在服务端内存里持有，客户端只拿到 ticket_id。超时后票据作废。
    """
    now = time.time()
    ticket_id = _generate_ticket_id()
    ticket = ApprovalTicket(
        ticket_id=ticket_id,
        tool_name=tool_name,
        arguments=arguments,
        plan_summary=generate_plan_summary(tool_name, arguments),
        user_id=user_id,
        created_at=now,
        expires_at=now + APPROVAL_TIMEOUT_SECONDS,
    )
    with _lock:
        _tickets[ticket_id] = ticket
    return {
        "ticket_id": ticket_id,
        "tool_name": tool_name,
        "plan_summary": ticket.plan_summary,
        "created_at": ticket.created_at,
        "expires_at": ticket.expires_at,
        "user_id": user_id,
    }


def is_round_fused(user_id: str, tool_name: str) -> bool:
    """该用户本轮是否已拒绝过该工具。拒绝后本轮不再弹确认。"""
    key = (user_id, tool_name)
    with _lock:
        expires = _round_fused.get(key)
        if expires is None:
            return False
        if time.time() > expires:
            del _round_fused[key]
            return False
        return True


def fuse_round(user_id: str, tool_name: str) -> None:
    """用户拒绝后，本轮熔断该工具。"""
    # 熔断到本轮结束（简化为 5 分钟后过期，足够覆盖一次对话）
    with _lock:
        _round_fused[(user_id, tool_name)] = time.time() + 300


def consume_ticket(ticket_id: str, decision: str,
                   requester_user_id: str) -> Optional[ApprovalTicket]:
    """消费一张票据。返回票据（含完整参数）或 None。

    判据：
    - 票据存在且未消费
    - 未超时
    - 请求人是票据属主（跨属主隐私：过程留痕不出当前属主视野）
    - decision 是 "allow" 或 "reject"
    """
    if decision not in ("allow", "reject"):
        return None
    with _lock:
        ticket = _tickets.get(ticket_id)
        if ticket is None:
            return None
        if ticket.consumed:
            return None
        if time.time() > ticket.expires_at:
            # 超时作废，绝不默认放行
            ticket.consumed = True
            ticket.decision = "timeout"
            return None
        if ticket.user_id != requester_user_id:
            # 跨属主：不泄露票据内容
            return None
        ticket.consumed = True
        ticket.decision = decision
        if decision == "reject":
            _round_fused[(ticket.user_id, ticket.tool_name)] = time.time() + 300
        return ticket


def get_ticket(ticket_id: str) -> Optional[ApprovalTicket]:
    """查询票据状态（不消费）。"""
    with _lock:
        return _tickets.get(ticket_id)


def cleanup_expired() -> int:
    """清理过期票据。返回清理数量。可由定时任务调用。"""
    now = time.time()
    cleaned = 0
    with _lock:
        expired = [tid for tid, t in _tickets.items()
                   if t.consumed or now > t.expires_at]
        for tid in expired:
            del _tickets[tid]
            cleaned += 1
        fused_expired = [k for k, v in _round_fused.items() if now > v]
        for k in fused_expired:
            del _round_fused[k]
    return cleaned


# ---------- 双端契约文案（漂一个字就红） ----------
APPROVAL_TITLE = "执行代码确认"
APPROVAL_ALLOW_TEXT = "允许"
APPROVAL_REJECT_TEXT = "拒绝"


def approval_card_text(tool_name: str, language: str = "python") -> str:
    """给客户端显示的一句话正文。双端契约测试钉死。"""
    return f"模型想要执行一段 {language} 代码"
