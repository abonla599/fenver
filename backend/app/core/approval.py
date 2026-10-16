"""审批层最小闭环：计划-确认-执行。

v0.31 新增。仅 `execute_code` 标了 `requires_approval=True`，其他工具直通。
确认凭据走服务端一次性票据，客户端不回传凭据（票据 ID 由端点持有）。
超时/断连 → 票据作废，绝不默认放行。

v0.35 A2 起票据**落盘**（`data/approvals.json`）：提案 §二 把"TTL 60s + 存进程内存
`_tickets`"列为第二块地基缺口——跑三分钟的任务，第二次确认时票据早就没了，用户被迫
重跑。落盘解决的是"活得过一次重启"，可配置的 TTL（见 `ticket_ttl_seconds()`）解决的
是"活得过一次长任务"。**接口形状一个字没改**：客户端拿到的仍是一枚 uuid v4。

盘上放的是**摘要不是明文**：表的键与文件里的键都是 `sha256:<hex>`（`hash_ticket`，
用的就是 `auth.py:141` 那个原语），明文 `ticket_id` 不落盘。抄的是本仓已有的那一条
纪律——会话令牌与续命凭据都只存摘要（`auth.py:141-152`、查找 `auth.py:975`、单次使用
`auth.py:989`），理由与那条注释一样：**受众隔离**，`hash_ticket` 是第三个名字而不是
把三类凭据塞进同一张表。代价如实写在这里：重启后服务端无法再报出明文 id（客户端手里
那一枚才是），所以每次按 id 查都得先算摘要——这正是 `consume_ticket` / `get_ticket` /
`record_execution` 现在做的事。

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
import os
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Dict, Optional

from app.core.atomic_write import write_json_atomic
from app.core.auth import hash_token
from app.core.config_store import approval_ticket_ttl_seconds
from app.core.paths import data_root
from app.core.providers import scrub_secrets
from app.tools.registry import tools_registry


# 票据的有效期（秒）。**单一真相在 core/config_store.py 那一格**（键
# approval_ticket_ttl_seconds），生效路径只走 ticket_ttl_seconds() 现读。
# 下面这个常量是**启动时**从同一处读来的快照，留着只因为既有测试与运维文案拿它做
# "把票据做旧"的算术（生产没配就是 60 秒）。不在这里再写一遍 60：那两个数字会漂。
APPROVAL_TIMEOUT_SECONDS = approval_ticket_ttl_seconds()


def ticket_ttl_seconds() -> float:
    """一张票据现在生效的有效期（秒）。全场唯一读数口径。

    为什么这一版要把它做成可配的（提案 §四 v0.35 第三条、§二 第二行）：60 秒这个数是
    给"弹了没人回"的移动端定的，而一个跑三分钟的多步任务里，第二次确认一定落在 60 秒
    之外——那不是安全，那是逼用户重跑（重跑就是要再烧一次钱）。配的是窗口，不是判据：
    到期一律作废、绝不因为"任务还活着"就续。想放宽请显式配
    `data/config.json` 的 approval_ticket_ttl_seconds 或环境变量
    APPROVAL_TICKET_TTL_SECONDS（口径与 STREAM_NO_READER_GRACE_SECONDS 同一条路）。
    """
    return approval_ticket_ttl_seconds()



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
# 键是**摘要**（`sha256:<hex>`），不是明文 ticket_id：与 auth.py 那两张凭据表同一套
# 纪律（会话令牌表与 device_secrets 都以摘要为键，见 auth.py:141-152 与 :975）。
# 内存与盘上认同一个形状，是有意的：一处明文一处摘要，就会长出"重启前后行为不一样"
# 那种只在现网出现的 bug。明文 id 只活在两头的传输途中——建票时现造、按 id 查时先算
# 摘要，所以 `ApprovalTicket.ticket_id` 这一格**不进文件**（见 _to_record）。
_lock = threading.Lock()      # 普通 Lock，**不可重入**：下面的 _flush_locked 要求持锁调用
_tickets: Dict[str, ApprovalTicket] = {}
# 单轮熔断：(user_id, tool_name) → 本轮拒绝后不再弹确认
_round_fused: Dict[tuple, float] = {}  # value = 过期时间戳


def hash_ticket(ticket_id: str) -> str:
    """票据 id 的摘要——落盘与查表用的那个键。

    复用 auth.py 的 `hash_token` 原语（同一个 "sha256:" 前缀、同一个算法），但**是另
    一个名字**：抄的正是 auth.py:145-152 给 `hash_secret` 写的那条理由——受众隔离要
    靠读代码也看得出这条路径算的是哪一类凭据，三类凭据不许共用一个调用点。
    高熵输入（uuid v4，122 位随机）才谈得上"摘要不可反推"；这与令牌那边同一前提。
    """
    return hash_token(ticket_id)


def _default_path() -> str:
    """票据表落在 data/approvals.json（`data/` 那条 .gitignore 规则盖住它）。"""
    env_path = os.getenv("APPROVALS_DB_PATH")
    if env_path:
        return os.path.abspath(env_path)
    return os.path.join(data_root(), "data", "approvals.json")


def _to_record(ticket: ApprovalTicket) -> dict:
    """序列化：整张票据，**除了明文 ticket_id**。

    arguments 与 result 是真的会进文件（前者是批准后要执行的参数快照——confirm 端点
    只认它，不重解析客户端回传的东西；后者是给客户端轮询的执行结果）。这一点是本票
    面新开的磁盘面，交付报告里如实登记：一个能读到这个文件的攻击者现在看得到
    "谁、哪个工具、参数与输出、批准与到期时刻"，但**拿不到可以继续用的凭据**——
    键是摘要，明文 id 不在盘上，而 confirm 还要过属主自己的登录态。
    """
    record = asdict(ticket)
    record.pop("ticket_id", None)
    return record


def _from_record(record: dict) -> Optional[ApprovalTicket]:
    """反序列化一张票据；形状不对就返回 None（由调用方说人话并丢掉那一条）。

    `ticket_id` 填的是**摘要**：盘上没有明文可还原。这不破坏任何功能——查询一律先过
    hash_ticket，而回给客户端的那一格由调用点带（见 consume_ticket 里的回填）。
    缺 user_id 或 tool_name 的一条直接丢：没有属主的能力记录不许存在，is_granted 的
    属主比对就会拿空串去匹配一个空 user_id 的调用。fail-closed，不是洁癖。
    """
    if not isinstance(record, dict):
        return None
    if not str(record.get("user_id") or "").strip() or not str(
            record.get("tool_name") or "").strip():
        return None
    try:
        created = float(record["created_at"])
        expires = float(record["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None
    arguments = record.get("arguments")
    return ApprovalTicket(
        ticket_id="",                       # 明文不落盘；调用点按呈现的 id 回填
        tool_name=str(record["tool_name"]),
        arguments=arguments if isinstance(arguments, dict) else {},
        plan_summary=str(record.get("plan_summary") or ""),
        user_id=str(record["user_id"]),
        created_at=created,
        expires_at=expires,
        consumed=bool(record.get("consumed", False)),
        decision=record.get("decision"),
        result=record.get("result"),
        ok=record.get("ok"),
        executed=bool(record.get("executed", False)),
    )


def _flush_locked() -> None:
    """把整表快照原子落盘。**调用方必须已经持着 _lock**。

    这把是 threading.Lock（不可重入），所以这里绝不再进一次锁——task_store 那把是
    RLock，两处的写法不能互相抄。改表与落盘在同一次持锁里做完，判据同 task_store
    的审查 #12：一边遍历一边有人改表就是"dictionary changed size during iteration"。
    写法复用 core/atomic_write（mkstemp + fsync + os.replace，权限在建文件那一次就
    收成 0600），不自己写第二套。
    """
    payload = {"tickets": {tid: _to_record(t) for tid, t in _tickets.items()}}
    write_json_atomic(_default_path(), payload)


def _sweep_locked(now: float) -> int:
    """剔除"到期之后又过了一个 TTL 窗口"的票据与到期的熔断标记。调用方持锁。

    这段判据本来就是 create_approval_request 里那几行（判据 11 修的是"只增不减"）。
    A2 把它抽出来并**在每个会碰这张表的入口都跑**，因为落盘之后"只在建票时扫"留下了
    一个新的洞：一张没人再建票的空闲会话里，过期的能力记录会一直躺在
    data/approvals.json 上——文件只增不减，而里面装着参数与执行结果。

    不起后台线程、不加定时器（本机已有外部看门狗，而 session/export_store.py:10-11 那句"为一张
    几行的小表养一条常驻线程，换来的是每个测试进程里多一个关不掉的幽灵"在这里同样
    成立）。保留窗口 = 一个 TTL：客户端的轮询窗口就是票据有效期，窗口外取不到本就是
    预期行为（GET 回 404，与"票据不存在"同一形状，不泄露存在性）。
    """
    grace = ticket_ttl_seconds()
    stale = [tid for tid, t in _tickets.items() if now > t.expires_at + grace]
    for tid in stale:
        del _tickets[tid]
    for k in [k for k, v in _round_fused.items() if now > v]:
        del _round_fused[k]
    return len(stale)


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

    v0.35 A2 之后这句话仍然成立，包括跨重启那一半：盘上读回来的那一张带着
    consumed=True 回来，而这张表上唯一的执行入口（POST /v1/approval/confirm）必须先
    过一次 consume_ticket，那里第二次直接 None。少绕这一道就没有"执行"这回事。
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


def _present(digest: str, presented_id: str) -> Optional[ApprovalTicket]:
    """按摘要键取出票据，并把**这一次调用方呈现的明文 id** 回填到内存对象上。

    为什么需要这一步：盘上只有摘要（这是 A2 抄 auth.py 的那条纪律），所以刚
    `restore()` 回来的那一张手里没有明文 id；而 GET 的响应体和 `record_execution`
    的入参都要它。命中就意味着 `sha256(presented_id) == digest`，即这就是同一张票——
    回填的不是猜测。进程内新建的那一张本来就带着明文，这一步对它是个 no-op。
    """
    ticket = _tickets.get(digest)
    if ticket is not None:
        ticket.ticket_id = presented_id
    return ticket


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

    票据是服务端的凭据，客户端只拿到 ticket_id；v0.35 A2 起它**随建即落盘**，
    所以一次长任务里的第二次确认不再因为进程重启而作废（提案 §四 判据 3）。
    有效期现读 `ticket_ttl_seconds()`（默认仍是 60 秒，见那一格的注释）。

    顺带清理过期票据：`_sweep_locked` 在这里跑，也在消费/查询/回填结果与装载时跑
    （A2 补的就是"只在建票时扫"这个洞——没人建票时，过期的能力记录会一直躺在
    data/approvals.json 上，而里面装着参数与执行结果。不起后台线程，理由见那份函数）。
    """
    now = time.time()
    ttl = ticket_ttl_seconds()
    ticket_id = _generate_ticket_id()
    ticket = ApprovalTicket(
        ticket_id=ticket_id,
        tool_name=tool_name,
        arguments=arguments,
        plan_summary=generate_plan_summary(tool_name, arguments),
        user_id=user_id,
        created_at=now,
        expires_at=now + ttl,
    )
    with _lock:
        _tickets[hash_ticket(ticket_id)] = ticket
        # 清理判据：已超时的（含超时后消费的那些）。executed=True 的执行结果也会
        # 随超时一起被清掉——客户端轮询窗口就是票据有效期，窗口外取不到本就是
        # 预期行为（GET 会返回 404，与"票据不存在"同一形状，不泄露存在性）。
        # 不去区分"消费了但没执行"：那也只是多留一个窗口，换不来任何东西。
        _sweep_locked(now)
        _flush_locked()
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

    一次性这件事现在**跟着盘走**（v0.35 A2 判据 5）：consumed 与 decision 的改动在同
    一次持锁里落盘，所以重启之后再拿同一个 id 来，命中的是那条已经 consumed=True 的
    记录，第二次直接 None——重放不会因为"进程换了内存"而复活。
    """
    if decision not in ("allow", "reject"):
        return None
    now = time.time()
    with _lock:
        # 每个入口都扫：见 _sweep_locked 那段——落盘之后"只在建票时扫"会留过期记录在文件里
        dirty = bool(_sweep_locked(now))
        try:
            ticket = _present(hash_ticket(ticket_id), ticket_id)
            if ticket is None:
                return None
            if ticket.consumed:
                return None
            if time.time() > ticket.expires_at:
                # 超时作废，绝不默认放行
                ticket.consumed = True
                ticket.decision = "timeout"
                dirty = True
                return None
            if ticket.user_id != requester_user_id:
                # 跨属主：不泄露票据内容，也**不许把它消费掉**——B 的一次尝试不能把 A
                # 那张好票烧掉（那是一条把别人的能力打码成 404 的 DoS 信道）。
                return None
            ticket.consumed = True
            ticket.decision = decision
            dirty = True
            if decision == "reject":
                _round_fused[(ticket.user_id, ticket.tool_name)] = time.time() + 300
            return ticket
        finally:
            # 一次持锁里"改表 + 落盘"，出口只有一处：中途任何一条 return 都不会把
            # 刚改的那格留在内存里而盘上还是旧的（判据 5 说的就是这件事）。
            if dirty:
                _flush_locked()


def get_ticket(ticket_id: str) -> Optional[ApprovalTicket]:
    """查询票据状态（不消费）。客户端轮询用；属主比对在调用点（main.py 那个 GET）。

    这个入口也扫一遍：轮询是"没人建新票据时唯一还在发生的事"，A2 之前那正是过期记录
    留在盘上的原因。
    """
    with _lock:
        dirty = bool(_sweep_locked(time.time()))
        ticket = _present(hash_ticket(ticket_id), ticket_id)
        if dirty:
            _flush_locked()
        return ticket


def record_execution(ticket_id: str, result: str, ok: bool) -> None:
    """回填"批准后真的执行了"的结果，供客户端轮询 GET /v1/approval/{id}。

    为什么结果存在票据上而不是返回值里：confirm 端点是 POST/响应即走，真正要给用户
    看执行结果得能轮询（代码执行最长10 秒，客户端不适合同步等）。票据是服务端持有、
    客户端只有 id 的那个对象，天然就是这个角色。

    `ok` 显式存而不是让读侧去 `result.startswith("✓")`：成败是执行器的判定，读侧从
    文本里猜等于把"✓ 前缀是契约"这件事复制一份到第二个文件——改了 ToolResponse 的
    措辞这里就悄悄说反了。

    回填同样落盘（v0.35 A2）：执行结果落在内存而没落在盘上时，人在任务跑到一半重启后
    轮询会读到 `executed=False`，那是一句谎话。
    """
    with _lock:
        dirty = bool(_sweep_locked(time.time()))
        ticket = _present(hash_ticket(ticket_id), ticket_id)
        if ticket is not None:
            ticket.result = result
            ticket.ok = bool(ok)
            ticket.executed = True
            dirty = True
        if dirty:
            _flush_locked()


def cleanup_expired() -> int:
    """清理过期票据。返回清理数量。可由定时任务调用。

    与 `_sweep_locked` 的差别是有意的、别抄混：这条把**已消费的**也立刻删（判据
    `t.consumed or now > t.expires_at`），而惰性那条保留到"到期之后再过一个 TTL 窗口"，
    为的是客户端还能轮询到执行结果。所以这条适合"会话彻底收尾时清场"，不适合挂在每次
    查询上——那会让 confirm 之后紧跟着的那一次 GET 直接 404。
    全项目仍然没有定时调用点（本票刻意不起后台线程），它现在也会落盘。
    """
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
        if expired or fused_expired:
            _flush_locked()
    return cleaned


def restore(path: str = None) -> int:
    """从盘上把票据读回来（启动时一次；测试用它等价于"重启进程"）。返回读到的条数。

    这是本票的存在理由：提案 §四 v0.35 判据 3——一张审批票据在第 4 步仍有效。

    三条纪律：
    - **到期之后又过一个 TTL 窗口的条目根本不复活**（与 _sweep_locked 同一个判据）。
      落盘换来的必须是"活得更久而合法"，不是"过期凭据跨重启诈尸"。
    - 整份文件读不懂 → 备份成 `.corrupt` 后按空表跑（与 task_store/providers/config
      同一形状：坏掉的东西要看得见，不能被当成空）。按空表跑等于**一张票据都不放行**，
      fail-closed 的方向是对的：宁可让用户重批一次。
    - 单条形状不对（缺属主、缺工具名、时间戳不是数）→ 跳过并点名，不带崩其余条目。
    """
    target = os.path.abspath(path or _default_path())
    with _lock:
        _tickets.clear()
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return 0
        except (ValueError, OSError) as e:
            backup = target + ".corrupt"
            try:
                os.replace(target, backup)
                print(f"⚠️ 审批票据读不了（{e}），已备份为 {backup}，按没有票据跑")
            except OSError:
                print(f"⚠️ 审批票据读不了且无法备份（{e}），按没有票据跑")
            return 0
        items = (data or {}).get("tickets") or {}
        now = time.time()
        grace = ticket_ttl_seconds()
        loaded = dropped = 0
        for digest, record in items.items():
            ticket = _from_record(record)
            if ticket is None:
                dropped += 1
                print(f"⚠️ 跳过一张读不懂的审批票据（{str(digest)[:16]}…）")
                continue
            if now > ticket.expires_at + grace:
                dropped += 1            # 到期太久：不复活（见上面第一条纪律）
                continue
            _tickets[digest] = ticket
            loaded += 1
        # 丢过东西就回写一次，否则那份文件会一直带着死条目——落盘之后的"只增不减"
        # 比原先只在内存里发生要贵得多。
        if dropped:
            _flush_locked()
        return loaded


# ---------- 双端契约文案（漂一个字就红） ----------
APPROVAL_TITLE = "执行代码确认"
APPROVAL_ALLOW_TEXT = "允许"
APPROVAL_REJECT_TEXT = "拒绝"


def approval_card_text(tool_name: str, language: str = "python") -> str:
    """给客户端显示的一句话正文。双端契约测试钉死。"""
    return f"模型想要执行一段 {language} 代码"


# 启动时读回未过期的票据（与 task_store.py 末尾同一形状：一次导入期读盘，
# 不在事件循环里，也不建文件——盘上没有就是没有）。
restore()
