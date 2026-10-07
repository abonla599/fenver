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
    # 批准后**真的执行**的结果。原先这里只有一个 decision 字段，于是 confirm 端点
    # 消费掉票据就返回"已允许执行"，而代码从未被执行——用户在App 上点允许什么都不会
    # 发生，v0.31 的审批闭环只有"计划-确认"没有"执行"。执行发生在 confirm 端点内，
    # 结果存在这里，客户端轮GET /v1/approval/{ticket_id} 取。
    result: Optional[str] = None
    ok: Optional[bool] = None         # 执行成功与否，显式存：不去 parse 结果文本里的 ✓
    executed: bool = False            # 是否已执行过（"allow 但执行炸了"与"allow 且跑完"要分得开）


# ---------- 全局票据存储 ----------
_lock = threading.Lock()
_tickets: Dict[str, ApprovalTicket] = {}
# 单轮熔断：(user_id, tool_name) → 本轮拒绝后不再弹确认
_round_fused: Dict[tuple, float] = {}  # value = 过期时间戳

# ---------- 执行器放行令牌 ----------
# 为什么需要：审批判据原先只活在两条聊天路径的**调用侧**（pipeline/streaming 里
# `if approval.needs_approval(name): ... continue`），而真正执行工具的 _dispatch
# 完全不知情。后果是 react_agent 与 task_agent 直接 execute_tool 就拿到了无审批的
# 代码执行能力——它们不经过那两个分支。审批是执行前的闸门，闸门必须挂在执行器上，
# 不能挂在某几个调用方的记忆里。
#
# 契约：调用 execute_tool 时把"用户已确认"这件事**证明**给执行器看，而不是让执行器
# 替调用方判断。证明物就是上面那张 ApprovalTicket——服务端自己产的，客户端碰不到。
# 默认 None = 未获批准 = 需要审批的工具一律拒执行。


def is_granted(ticket: Optional[ApprovalTicket], tool_name: str,
               user_id: str) -> bool:
    """这张票据能不能为这次调用放行。

    三道判据缺一不可，任何一道不过都按"未获批准"处理（fail-closed）：
    - decision 已是 allow（reject/timeout/None 一律不算）
    - 工具名一致（一张票据只放行它自己那次调用，不能拿去放行别的工具）
    - 属主一致（跨属主不成立）

    **不查consumed**：consume_ticket 消费时就会把它置True，而执行恰恰发生在消费之后。
    防重放的职责在 consume_ticket 本身——同一张票据第二次消费直接返回 None，
    所以 confirm 端点最多为一张票据执行一次。这里再查一次会把正常路径全堵死。
    """
    if ticket is None:
        return False
    if ticket.decision != "allow":
        return False
    if ticket.tool_name != tool_name:
        return False
    if ticket.user_id != (user_id or ""):
        return False
    return True


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

    顺带清理过期票据：cleanup_expired 原本定义了却全项目无调用点，于是 _tickets
    只增不减。更要紧的是票据现在还存着执行结果（工具输出可能上千字），长期滞留
    既是内存增长也是留痕。这里在建票时顺手扫一遍——不引定时器依赖，代价O(n) 但
    n 只跟"最近两倍票据有效期内的活跃票据数"同阶，可以忽略。
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
        # 清理判据：已超时的（含超时后消费的那些）。executed=True 的执行结果也
        # 会随超时一起被清掉——客户端轮询窗口就是票据有效期，窗口外取不到本就
        # 是预期行为（GET 会返回 404，与"票据不存在"同一形状，不泄露存在性）。
        # 不去区分"消费了但没执行"：那也只是多留 60 秒，换不来任何东西。
        stale = [tid for tid, t in _tickets.items()
                 if now > t.expires_at + APPROVAL_TIMEOUT_SECONDS]
        for tid in stale:
            del _tickets[tid]
        for k in [k for k, v in _round_fused.items() if now > v]:
            del _round_fused[k]
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


def record_execution(ticket_id: str, result: str, ok: bool) -> None:
    """回填"批准后真的执行了"的结果，供客户端轮询 GET /v1/approval/{id}。

    为什么结果存在票据上而不是返回值里：confirm 端点是 POST/响应即走，真正要给用户
    看执行结果得能轮询（代码执行最长10 秒，客户端不适合同步等）。票据是服务端持有、
    客户端只有 id 的那个对象，天然就是这个角色。

    `ok` 显式存而不是让读侧去 `result.startswith("✓")`：成败是执行器的判定，读侧从
    文本里猜等于把"✓ 前缀是契约"这件事复制一份到第二个文件——改了 ToolResponse 的
    措辞这里就悄悄说反了。
    """
    with _lock:
        ticket = _tickets.get(ticket_id)
        if ticket is None:
            return
        ticket.result = result
        ticket.ok = bool(ok)
        ticket.executed = True


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
