"""
任务编排器模块
负责将复杂目标分解为子任务，并协调ReAct Agent逐步执行
支持任务状态持久化，可断点续传，支持任务取消
"""
from typing import List, Optional, Dict, Any, Tuple
from .task_store import (IllegalTaskTransition, Task, TaskStatus, can_transition,
                         claim_execution, expire_parked_task, is_executing,
                         is_waiting_timed_out, release_execution, save,
                         sweep_timed_out_parks, waiting_timeout_info, task_store)
# 认领那一跳要用的锁就是那份存储自己那把（改表与落盘同锁，task_store.py:274）。
# 跨模块引一个下划线开头的名字在本仓有先例（`app/web/web_router.py:24` 引
# `auth_router._client_ip`），而这里的理由更硬：判"这条任务在不在等人"与写它的那一跳
# 必须在**同一把**锁里，另起一把只是把窗口从一处搬到两处。
from .task_store import _lock as _store_lock
# `_EXECUTING` 这个名字留给既有消费方（A3 的并发用例直接往这张表里塞 id）：它现在是
# task_store 里那一张表的**别名**，不是第二份。生产代码判占位请走公共函数。
from .task_store import _EXECUTING as _EXECUTING  # noqa: F401
from .planner import Planner
from .executor import Executor
from .task_agent import TaskNeedsUser
from ..core.llm_client import get_llm_response
from ..core.providers import scrub_secrets
from app.tools.executor import execute_tool_detailed, _can_resume
from app.tools.registry import tools_registry

# ⭐ v0.35 A3：工具挂起时写进 A1 那份 `Task.resume_cursor` 的游标**种类标记**。
# 为什么要有这个键：`resume_cursor` 是一张自由 dict（A1 刻意如此，见 task_store.py 里
# `wait_for_user` 的注释——"要往里加键不该每加一个就回来改 to_dict"），所以读侧必须
# 能认出"这一张是工具断点"还是别的什么，认不出就什么都不接（fail-closed）。
TOOL_SUSPENSION_KIND = "tool_suspension"

# ⭐⭐ 本进程里"已经有人在跑"的任务清单（独立审查员复现出来的那一格，见 A3 测试文件
# 末尾那族用例）。工单 #34 起这张表搬进了 `task_store`（公共 API：`claim_execution` /
# `release_execution` / `is_executing`），因为它不再只归编排器看：`/v1/tasks/{id}/cancel`
# 必须问同一张表，否则一条取消能在答案正被接回去的那一秒落下来，把用户刚递进去的
# 验证码/收货地址连同任务一起丢掉。上面引的那个 `_EXECUTING` 只是同一张表的兼容别名。
#
# 为什么状态不够、必须另有一张表：一个 RUNNING 的任务有两种截然不同的下场——
#   ① 上一次进程被打断留下的（`restore()` 给它 `interrupted_by_restart`）：再进来的人
#      续跑它是**设计如此**，跳转表那条 `running → running` 自环就是为它开的
#      （task_store.py:69 的注释）；
#   ② 本进程另一条请求**正跑在这一秒**：再进来的人做什么都是错的。
# 审查员复现出来的正是 ② 被当成 ①：第一条答案把任务从 waiting_user 翻成 RUNNING，
# 第二条因此看不见 waiting_user，掉进那句通用的"恢复任务 … 进度: 0/1"，把整条子任务
# 从第 1 步重跑——第 1~3 步（查店、加购、发验证码）又烧一遍，那正是 A3 承诺省掉的
# 那一笔钱，而两个响应还互相矛盾（一个说还在等人，一个说办完了）。
# 把 ① 与 ② 分开的唯一诚实信息是"这条任务在我这个进程里有没有人占着"，那就是一张
# 进程内的表，不是状态字段：字段会随 tasks.json 落盘，一次崩溃就留下一个永远"有人在跑"
# 的假案（谁也叫不动它），而这张表跟着进程一起清空。边界（只在本进程内有效）与
# 为什么换包脚本单进程起得来，见 `task_store` 里那张表自己的注释。


def _suspension_cursor(suspension: Dict[str, Any], subtask_index: int) -> Dict[str, Any]:
    """把执行器那包断点折成一份游标 dict。键在这里定死，`task_store` 一个字不动。

    `question` 不在这里写：`Task.wait_for_user()` 会用问题兜住那一格，而 GET
    `/v1/tasks/{id}` 的读侧（A1 已交付）认的就是 `resume_cursor["question"]`。
    `arguments` 存的是**服务端这次调用真正用的那一份**，续跑时照原样交回同一个工具——
    与审批那条同一条纪律：不重新采纳任何客户端事后回传的东西，免得"批准的是 A、
    执行的是 B"。
    """
    return {
        "kind": TOOL_SUSPENSION_KIND,
        "tool_name": suspension.get("tool_name"),
        "arguments": suspension.get("arguments") or {},
        "resume_state": suspension.get("resume_state"),
        "subtask_index": subtask_index,
    }


def _tool_suspension(task: Task) -> Optional[Dict[str, Any]]:
    """这条任务的游标是不是一次**工具断点**。认不出的一律回 None（= 不当断点接）。

    逐条判据都朝"宁可什么都不接"那一侧：形状不对就回到 A1 既有那条"按子任务续跑"，
    绝不拿半个游标去调工具——一次没有断点的续跑等于从头再跑，那是这一版要省掉的
    那一笔钱，而且没人会知道它又跑了一遍。
    """
    cursor = getattr(task, "resume_cursor", None)
    if not isinstance(cursor, dict) or cursor.get("kind") != TOOL_SUSPENSION_KIND:
        return None
    tool_name = str(cursor.get("tool_name") or "").strip()
    arguments = cursor.get("arguments")
    state = cursor.get("resume_state")
    if not tool_name or not isinstance(arguments, dict) or not isinstance(state, dict):
        return None
    return {"tool_name": tool_name, "arguments": arguments, "resume_state": state,
            "question": str(cursor.get("question") or "")}


# ⭐⭐ 工单 #33：一条 `failed` 记录还带不带**可信**的工具断点。
#
# 缺陷原文：编排器先认领（`_claim_execution`）、这一趟可能抛，而 `_record_failure` 写
# `FAILED` 时 `set_status` **刻意留着**那份游标（它是"上次停在哪儿"的证据，撕了它只会让
# 下一个接手的人更瞎）。留下来的状态就是 `failed` + `kind=="tool_suspension"`，而改动前
# 的 FAILED 分支（原 `orchestrator.py:164-165`）**一眼都不看游标**就进通用续跑——
# `can_resume=False` 只是个标签、不是一道闸，于是已经付过钱的第 1~3 步（查店、加购、
# 发验证码）再烧一遍，验证码再发一遍。审查员量到的就是那两行：`SPENT before=1 after=2`。
#
# 这里的判据与 `waiting_user` 那一族**同向**（宁可什么都不接）：判不出 = 不接 = 一句
# 机器可读的实话 + 要求一次刻意的重新开始。为什么"拒"是对的方向而"重跑"是错的：重跑
# 花的是用户的真钱，而且没人会知道它又跑了一遍；拒只是这一趟办不成，盘上一格未动。
#
# `_can_resume` 是从 `app/tools/executor.py` 引来的**那一份**判据（声明过 suspends +
# 断点带得过重启），不在这里抄第二份——引跨模块的下划线名在本仓有先例，见文件头
# `_store_lock` 那条注释的理由。registry 那一眼只为把"哪一半没过"说清楚（拒的理由要
# 答得出为什么），判决本身仍然出自 `_can_resume`。


def _trusted_failed_breakpoint(task: Task) -> Tuple[Optional[Dict[str, Any]], str]:
    """这份游标能不能当"接着上一步的花钱买的断点"来续。返回 (断点 | None, 代码)。

    代码取值（`resumable` 之外一律**什么都不许跑**）：
    - `no_breakpoint`         失败发生在任何断点存在之前（连游标都没有）；
    - `wrong_kind`            有游标，但不是工具断点（A1 那张 dict 是自由的，别的用途
                              的键不能顶着一枚"看起来还能接"的断点骗走一次付费续跑）；
    - `consumed_breakpoint`   这一枚断点**已经接回去过**了（见 `_consume_breakpoint`）：
                              工具真的收到过答案，再递一次就是把同一个外部副作用落两遍；
    - `stale_subtask_index`   游标记的那一步与 `current_subtask` 不是同一步：断点说的
                              位置已经不代表这条任务的现在，按它续跑就是猜；
    - `unreadable_state`      形状读不出工具名/参数/断点，或断点带不过一次重启；
    - `tool_not_suspendable`  这条工具现在没在注册表里声明 `suspends` ——答案递不进去，
                              许诺"能接"就是撒谎（与 `_can_resume` 那一份同源）。
    """
    cursor = getattr(task, "resume_cursor", None)
    if not isinstance(cursor, dict) or not cursor:
        return None, "no_breakpoint"
    if cursor.get("kind") != TOOL_SUSPENSION_KIND:
        return None, "wrong_kind"
    if cursor.get("consumed"):
        return None, "consumed_breakpoint"
    if cursor.get("subtask_index") != task.current_subtask:
        return None, "stale_subtask_index"
    suspension = _tool_suspension(task)
    if suspension is None:
        return None, "unreadable_state"
    if not _can_resume(suspension["tool_name"], suspension["resume_state"]):
        declared = bool((tools_registry.get(suspension["tool_name"]) or {})
                        .get("suspends"))
        return None, ("tool_not_suspendable" if not declared else "unreadable_state")
    return suspension, "resumable"


class Orchestrator:
    def __init__(self, model: str = None):
        # model 留空 = 走 setDefault 的 provider（口径同 ReActAgent）；服务商名
        # 不刻进源码。planner/executor 同源透传，一条解析规则管三个类。
        self.model = model
        self.planner = Planner(model)
        self.executor = Executor(model)

    def run(self, goal: str, task_id: Optional[str] = None,
            user_id: Optional[str] = None,
            provider_id: Optional[str] = None,
            user_answer: Optional[str] = None) -> Dict[str, Any]:
        """
        执行任务的主方法
        Args:
            goal: 用户目标描述
            task_id: 可选，如果提供则尝试恢复已有任务
            user_id: 这个任务属于谁。必填（Task 会拒收空归属）——恢复已有任务时
                不新建设象，所以只有新建那一路用得上，但签名上不给默认值才是真话
            provider_id: 发起人在端点里解析出的模型服务。建任务时随 Task 落盘；
                恢复已有任务时认**任务自己记的那条**——续跑别人的当下默认，
                等于让下一次调用花在一个没点头的人身上
            user_answer: ⭐ v0.35 A3。用户给回的那句话（验证码、收货地址……）。
                **只**在恢复一条"工具挂在断点上等人"的任务时有意义：给了才接、
                不给就不接（原样回 waiting_user，绝不自动继续）。不带这一格的老调用
                与改动前逐字同形——它们碰不到这条新路，因为现网里在此之前没有任何
                一条任务是 waiting_user（`wait_for_user` 在生产代码里零调用点，
                这一版是第一个把它用起来的人）。
                ⭐ 同一枚停点**只许一个答案接走**（v0.35 A3 收口，见 `_claim_execution`）：
                两条答案一起进来时，赢的那条从断点接上，输的那条拿到
                `{"accepted": False, "reason": …}` 与任务此刻真实的状态——它不重跑子任务、
                不叫模型、不花一分钱，也不许看到另一条路给出的那个终态。
        Returns:
            包含任务状态和结果的字典
        """
        task = None
        # ⭐ v0.35 A3：(断点, 答案)。只有从 waiting_user 带着答案回来才非空。
        # 放在局部而不是 self 上：`orchestrator` 在 main.py 是**进程级单例**，
        # 挂在实例上就是两个用户的请求互相踩脚。
        pending_resume = None
        # ⭐ 工单 #33：这一趟是在**哪一个状态**上读到断点的（waiting_user 或 failed）。
        # 认领那一步只认这一个值，不认"反正两种都是断点态"——见 _claim_execution 的文档。
        breakpoint_status: Optional[TaskStatus] = None

        # --- 1. 尝试恢复任务或创建新任务 ---
        # 归属校验下沉在这里而不是端点里（审查 #13）：这一句拿 uuid 恢复任务时是
        # 以**原属主**的身份往下跑的（executor 用 task.user_id 注入 needs_user 工具），
        # 所以"是不是你的"必须由真正要跑它的人来判，不能指望每个调用方记得判。
        # 不是自己的就当不存在——不报错、不解释：一句"这任务不是你的"就把任务表
        # 变成了 uuid 探测器。落到新建分支后 Task 会因空 user_id 拒收，fail-closed。
        if task_id and task_id in task_store and \
                task_store[task_id].user_id == (user_id or "").strip():
            task = task_store[task_id]
            # 恢复的是那条任务自己记的 provider：发起人事后换了默认模型，
            # 不该让半途续跑的调用改花在一个没点头的人/配置上
            provider_id = task.provider_id or provider_id

            # ⭐ 检查是否已被取消
            if task.cancelled or task.status == TaskStatus.CANCELLED:
                return {
                    "task_id": task.task_id,
                    "status": "cancelled",
                    "goal": task.goal,
                    # ⭐ 工单 #33（A4 卡片要的那一格）：cancelled 这一态在 `status` 上分不出
                    # "用户自己取消的"与"停在断点上挂太久被作废的"，而这两件事对用户说的
                    # 是两句不同的话（后者手里那枚断点是**被系统**没掉的）。以前只有
                    # `error` 里一句中文能读，审查员量到的形状就是 `{"status":"cancelled",
                    # "reason":null,"accepted":null}`。这一格是**只增**的键、且与墙上时钟
                    # 无关，所以 A3 那条"非属主与不存在逐字节相同"的判据不受影响。
                    "end_reason": getattr(task, "end_reason", None),
                    "message": "任务已被取消"
                }

            # 如果任务已完成，直接返回结果
            if task.status == TaskStatus.COMPLETED:
                print(f"[Orchestrator] 任务 {task_id} 已完成，直接返回结果")
                return {
                    "task_id": task.task_id,
                    "status": "completed",
                    "goal": task.goal,
                    "final_answer": task.final_answer,
                    "subtasks": task.subtasks,
                    "results": task.results
                }

            # ⭐⭐ 工单 #33：failed 这一支以前只有一句 print 就掉进下面的通用续跑，
            # 于是"带着一枚可信断点失败"的任务被从第 1 步重烧一遍。现在先问游标可信不可信：
            #   可信 + 有答案 → 从断点接（与 waiting_user 那一族同一套判据、同一个认领）；
            #   可信 + 没答案 → 一句实话，什么都不跑（与 waiting_user 那一格同一个方向）；
            #   不可信       → 拒，并报出**为什么**不可信，要求一次刻意的重新开始。
            # 两条都不许"先跑了再说"：跑一次是真花钱的，而钱花在了第 1~3 步那一笔
            # 已经付过的账上，症状还不是报错——是账单比用户以为的多一倍。
            if task.status == TaskStatus.FAILED:
                suspension, code = _trusted_failed_breakpoint(task)
                if code != "resumable":
                    return self._refuse_rerun(task, code)
                answer = (user_answer or "").strip()
                if not answer:
                    return self._refuse_rerun(task, "answer_required",
                                              suspension=suspension)
                pending_resume = (suspension, answer)
                breakpoint_status = TaskStatus.FAILED
                print(f"[Orchestrator] 任务 {task_id} 带着可信的工具断点续跑："
                      f"第 {task.current_subtask + 1} 步的那个工具（不重跑已付费的步骤）")
            elif task.status == TaskStatus.WAITING_USER:
                # ⭐ v0.35 A5：先问"挂得太久了吗"，再问"有没有答案"。顺序是有意的：
                # 默认窗口 0 = 永不做废，这一格直接往下走（今天的形状一字不变）；运维
                # 配了正数才可能在这里停下。停下的下场是 cancelled + 游标清空，
                # **不是** failed——为什么，见 task_store.expire_parked_task 里那一段
                # （failed 留着 failed → running 那条出边，正是工单 #33 的重复付费门）。
                expired = self._expire_if_timed_out(task)
                if expired is not None:
                    return expired
                # ⭐ v0.35 A1：等到了人 → 从游标那一步接。这一支单独成句是有原因的：
                # 掉进下面那句 else 的话，日志里"被重启打断后续跑"与"挂起等输入后
                # 续跑"混成同一行，而两种恢复的原因完全不同。游标本体**不进日志**
                # ——里面可能装着用户刚填的验证码，同"目标原文不落地"那份纪律一条线。
                suspension = _tool_suspension(task)
                if suspension is None:
                    print(f"[Orchestrator] 任务 {task_id} 从等待用户输入中恢复，"
                          f"进度: {task.current_subtask}/{len(task.subtasks)}")
                else:
                    answer = (user_answer or "").strip()
                    if not answer:
                        # ⭐ v0.35 A3 fail-closed 的那一格：**没有答案就什么都不做**。
                        # 状态一格都不改（不假跳 RUNNING）、不调工具、不叫模型。
                        # 这一格改的是既有形状：以前这条分支会往下走、把整条子任务
                        # 从头再跑一遍——那是再烧一次钱，且等于替用户把答案编了。
                        # 对不带 user_answer 的旧调用方，这也是安全方向：它收到的
                        # 是"还在等"，不是"跑坏了"，更不是"假装跑完了"。
                        print(f"[Orchestrator] 任务 {task_id} 仍在等用户输入："
                              f"没有答案不续跑（第 {task.current_subtask + 1} 步的工具断点）")
                        return {
                            "task_id": task.task_id,
                            "status": "waiting_user",
                            "goal": task.goal,
                            "question": suspension["question"],
                            "subtask_index": task.current_subtask,
                            "total_subtasks": len(task.subtasks),
                            "can_resume": True,
                            # ⭐ v0.35 A5：窗口多长 / 哪一刻作废（`waiting_expires_at`
                            # 为 None = 运维没开作废窗口，永不做废——默认形态）。这两格是
                            # **政策**不是"现在"：本端点的响应被 A3 那条"非属主与不存在
                            # 给出逐字节相同的同一句话"判着，"挂了多久"因此只归 GET 详情。
                            **waiting_timeout_info(task, elapsed=False),
                            "message": (f"任务停在第 {task.current_subtask + 1} 步等用户"
                                        "给一个答案。带上 user_answer 才会从断点继续；"
                                        "这一次没有答案，所以什么也没跑、也没再花钱。"),
                        }
                    pending_resume = (suspension, answer)
                    breakpoint_status = TaskStatus.WAITING_USER
                    print(f"[Orchestrator] 任务 {task_id} 带用户答案续跑："
                          f"第 {task.current_subtask + 1} 步的那个工具")
            else:
                print(f"[Orchestrator] 恢复任务: {task.task_id}, 进度: {task.current_subtask}/{len(task.subtasks)}")
        else:
            # 创建新任务。计划这一步也是真金白银的调用：出网按发起人的池子走
            # （user_id/provider_id 往下传），配不成人话错误由端点层兜在
            # ProviderError→400 之前（v0.25 R1）。规划这一步坏掉时**没有任务**
            # 可标 failed——原先它会裸着抛出端点变 500，这里收成一个带 scrub
            # 的失败形状。
            try:
                plan = self.planner.plan(goal, user_id=user_id, provider_id=provider_id)
            except Exception as e:
                error_msg = scrub_secrets(f"{type(e).__name__}: {e}")
                print(f"[Orchestrator] 计划生成失败: {error_msg}")
                return {
                    "task_id": None,
                    "status": "failed",
                    "goal": goal,
                    "error": error_msg,
                }
            task = Task(goal=goal, subtasks=plan, user_id=user_id,
                        provider_id=provider_id)
            task_store[task.task_id] = task
            print(f"[Orchestrator] 计划生成完毕，共 {len(plan)} 个子任务")

        # --- 1.9 ⭐ v0.35 A5：顺手把"挂太久"的停点收一收（惰性清扫，没有定时器）---
        # 排在归属判完、认领之前：`skip_task_ids` 要的是**这一趟真的在处理的这一条**
        # （别人家的 task_id 不该被当成"本趟的那一条"而跳过清扫）。
        self._sweep_timed_out_parks(task.task_id)

        # --- 2. 原子认领这一趟执行（顺带把状态翻成 running）---
        # ⭐ v0.35 A1：走状态机而不是字段赋值。到这里合法的上一状态只有 pending /
        # running（被打断后续跑）/ failed（断点重跑，上面那句 print 就是它）/
        # waiting_user（等到了人，从游标记的那一步接）。表外的跳转直接抛，
        # 不再让"半个状态"悄悄写进盘。
        # ⭐ 这一跳从原来的裸 `task.set_status(...)` 挪进了 `_claim_execution`：
        # "读到 waiting_user"与"翻成 running"之间原来没有任何东西挡着，两条答案因此
        # 都能读到你都能动手，或者后到的那条读不到你、掉进通用续跑那一路把前三步重跑。
        # 判与跳现在同锁同批，见那份方法的注释。
        refused = self._claim_execution(
            task, claims_breakpoint=pending_resume is not None,
            # 认领时**只认这一趟在锁外读到的那一个**断点态：waiting_user 读到的那趟不许
            # 因为"#33 之后 failed 也是一枚断点"就顺着接走一条它从没判过的 failed 记录
            # （那条游标可能已经被别人消费过了，再递一次就是第二次副作用）。
            breakpoint_status=breakpoint_status)
        if refused is not None:
            return refused          # 一个字节都没改、一次调用都没发、一分钱都没花

        try:
            # --- 2.5 ⭐ v0.35 A3：从工具断点接上（只在带着用户答案回来时发生）---
            # 答案只递给**同一个工具、同一份参数快照**：参数取游标里服务端记的那一份，
            # 这里不重新采纳调用方回传的任何东西（与审批那条同一条纪律）。"能不能接、
            # 怎么算不合法"的判据一处在执行器（`_resume_refusal`），这里不复制第二份。
            resumed_tool = None
            if pending_resume is not None:
                suspension, answer = pending_resume
                outcome = execute_tool_detailed(
                    suspension["tool_name"], suspension["arguments"],
                    user_id=task.user_id,
                    resume={"state": suspension["resume_state"], "answer": answer})
                if outcome.suspended:
                    # 接上去又差一个答案（先要验证码、再要收货地址）：再挂一次，
                    # 断点换成新的那一份。running → waiting_user 是跳转表里的合法边。
                    return self._park_suspension(task, outcome.suspension,
                                                 task.current_subtask)
                # 没挂起就把它当这一步的结果往下走，**包括 ok=False**：续跑被闸门
                # 拒了（缺身份/超频控/需审批）也是一句要交给模型看的话，与两条聊天
                # 路径同一条纪律——"绝不因为一次工具没跑成就把整条流打断"
                # （streaming.py:544 那句）。打断它的是"替用户编一个答案"，不是
                # "如实说这一步没成"。
                #
                # ⭐ 工单 #33 第三句 + 工单 #37 收口：这一包断点**真的递进工具了**才就地
                # 记一笔"已消费"。判据不是"这一句返回了"（#33 旧写法，会把被闸门挡下的续跑
                # 也一起烧掉），也不是"这一句成功了"，而是执行器给出的那格来自世界的
                # `outcome.tool_ran`——`func(**kwargs)` 到底被调用没有。被闸门挡下 = 答案从没
                # 进工具 = 必须还能再递 = 不标 consumed。
                #
                # 从"把包递给 execute_tool_detailed"到"工具真的跑了"之间的下场，与各自的
                # consumed 取值（完整表见 `_consume_breakpoint` 的文档）：
                #   审批拒 / 审批待确认   tool_ran=False → 不消费（工单 #37：答案没进工具）
                #   频控挡下（case5）     tool_ran=False → 不消费
                #   缺身份 needs_user     tool_ran=False → 不消费
                #   断点形状不对          tool_ran=False → 不消费
                #   进 execute 之前就抛   走 except→failed，压根到不了这一行 → 不消费
                #   工具正常返回          tool_ran=True  → 消费（答案已交付，副作用可能已落地）
                #   工具跑一半才抛        tool_ran=True  → 消费（分不开"没跑成"与"跑了一半"，
                #                                          保守侧，守 #33 不重放副作用）
                #   工具又挂起一次        outcome.suspended，上面已 return 走 _park_suspension
                #                                          （写一份**新**游标，旧的 consumed 与否
                #                                          都已不代表此刻）→ 不在此处消费
                # 不变式：**工具没收到答案，答案就必须还能再递进去**（#37）；而"真的收到过
                # 答案"这一枚必须只许被接一次（#33）。两条都靠 `tool_ran` 这同一个世界事实区分。
                if outcome.tool_ran:
                    self._consume_breakpoint(task)
                resumed_tool = {"tool_name": suspension["tool_name"],
                                "result": outcome.text}

            # --- 3. 逐步执行子任务 ---
            while task.current_subtask < len(task.subtasks):
                # ⭐⭐⭐ 每个子任务执行前检查取消标志 ⭐⭐⭐
                if task.cancelled:
                    task.mark_cancelled()
                    print(f"[Orchestrator] 任务 {task.task_id} 在第 {task.current_subtask} 步被取消")
                    return {
                        "task_id": task.task_id,
                        "status": "cancelled",
                        "goal": task.goal,
                        "message": f"任务在第 {task.current_subtask + 1} 步被取消",
                        "completed_subtasks": task.current_subtask,
                        "total_subtasks": len(task.subtasks),
                        "results": task.results
                    }

                idx = task.current_subtask
                subtask = task.subtasks[idx]

                # 这里原来每步还打四行：`===== 执行子任务 i/N =====`、子任务原文、
                # "完成"、"当前进度 i/N"。它们要么把用户的目标原文抄进日志，要么是
                # 返回值里本来就有的同一个进度（final 的 results/subtasks/current_subtask）
                # 再印一遍。留下的几行只报 id 与状态变化——那是返回值里没有的东西。
                # 使用Executor（内部调用ReAct Agent）执行单个子任务
                # user_id 往下传：子任务里的工具要知道是谁在调（needs_user 那类不给身份就拒绝）
                # provider_id 认任务自己那一条：整趟任务的钱都记在发起人的账上
                #
                # ⭐ v0.35 A3：这一层是**唯一**接得住 TaskNeedsUser 的地方，因为只有这里
                # 手里有 Task。接住的两种下场见 _park_suspension；无论哪一种都不许落到
                # 下面那句兜底 `except Exception → failed` 里去——那正是提案 §二 第一行
                # 写的"问了就是整轮失败"。
                try:
                    # 只有真带着一包续跑结果时才把 `resumed_tool` 递下去：这一层被
                    # 测试与将来的调用方用鸭子签名替过（`def boom(task, user_id,
                    # provider_id)`），无条件多一个关键字参数会当场 TypeError，
                    # 而症状是"任务跑坏了"，不是"某个道具没跟上"。v0.25 归属契约里
                    # 那条 test_failure_message_is_scrubbed_before_it_leaves 就是抓到
                    # 这一点的现成回归锁（本票第一次就是这么红的）。
                    _seed = {"resumed_tool": resumed_tool} if resumed_tool else {}
                    result = self.executor.execute_task(subtask, user_id=task.user_id,
                                                        provider_id=task.provider_id,
                                                        **_seed)
                except TaskNeedsUser as need:
                    return self._park_suspension(task, need.suspension, idx)
                resumed_tool = None          # 种子只喂第一步（就是它续跑的那一步）

                # 保存结果
                task.results.append(result)
                task.current_subtask += 1

            # --- 4. 所有子任务完成，生成最终汇总 ---

            # 构造汇总提示词
            summary_prompt = self._build_summary_prompt(task)

            # 调用LLM生成最终答案
            final_answer = get_llm_response(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一个善于总结的助手，请将以下子任务的执行结果整合成一个完整、清晰的回答。"},
                    {"role": "user", "content": summary_prompt}
                ],
                provider_id=task.provider_id,
                user_id=task.user_id,
            )

            # 保存最终答案
            task.final_answer = final_answer
            task.set_status(TaskStatus.COMPLETED)

            print(f"[Orchestrator] 任务 {task.task_id} 完成！")

            return {
                "task_id": task.task_id,
                "status": "completed",
                "goal": task.goal,
                "final_answer": final_answer,
                "subtasks": task.subtasks,
                "results": task.results
            }

        except Exception as e:
            # --- 5. 异常处理 ---
            # 出口与落盘共用同一句 scrub 过的话：底层异常原文可能带着上游回显的
            # Authorization，而这个 dict 会直接进 HTTP 响应体（约束：错误出口
            # 不许原文回显）。
            error_msg = scrub_secrets(f"{type(e).__name__}: {e}")
            print(f"[Orchestrator] 任务执行出错: {error_msg}")

            # ⭐ 这一句原来不是一个方法，而是两行裸写：`task.error = error_msg` +
            # `task.set_status(TaskStatus.FAILED)`。后一行就是审查员那枚 500 的抛点
            # （改动前的 :317）——`except` 块里再抛一次 `IllegalTaskTransition`，外面
            # 没有第二层 try，它直接飘出 run()、飘出 /v1/agent/orchestrate。任务已经
            # 被并发的那一路跑完、或被 /v1/tasks/{id}/cancel 取消之后，终态不出边，
            # 这一跳必然被拒；拒了要说得出话，不能崩。判据与"为什么一格都不改"写在
            # `_record_failure` 那一份里。
            reported = self._record_failure(task, error_msg)
            body = {
                "task_id": task.task_id,
                "status": reported,
                "goal": task.goal,
                # ⭐ 工单 #33（A4 卡片要的判别器）：定论原因是**状态机那一跳**给的，不是
                # 这句中文。跳转被拒时它报的是任务此刻真的那一态的原因（可能是
                # cancelled_by_user），与 `status` 同源、同一个值，不许漂。
                "end_reason": getattr(task, "end_reason", None),
                # 报出去的那句 error 与盘上那一条同一个值（v0.25 那条
                # test_failure_message_is_scrubbed_before_it_leaves 钉的就是这个等式）：
                # 跳转被拒时 task.error 没被改，这里也就不能凭空多出一格。
                "error": task.error,
                "current_subtask": task.current_subtask,
                "total_subtasks": len(task.subtasks)
            }
            if reported != TaskStatus.FAILED.value:
                body["message"] = (
                    f"这一轮想把任务写成 failed，被状态机拒了：它此刻是 {reported}。"
                    "另一趟请求（或 /v1/tasks/{id}/cancel）已经先把它定论了。"
                    "终态记录与已经花掉的钱一格都不改，这一轮的异常只回在这里："
                    f"{error_msg}")
            return body

        finally:
            # 执行权必须跟着这一趟一起还，不管它是跑完了、跑坏了、还是半路停在新的
            # 问题上——漏还的症状不是报错，是这条任务在本进程里**永远**"有人在跑"，
            # 于是谁也续不动它（见 _claim_execution 那份表）。
            self._release_execution(task.task_id)

    def _claim_execution(self, task: Task,
                         claims_breakpoint: bool = False,
                         breakpoint_status: Optional[TaskStatus] = None
                         ) -> Optional[Dict[str, Any]]:
        """原子地占住这条任务的执行权。占到回 `None`；占不到回的就是要给调用方的那一格。

        判"有没有人占着"、判"这一趟是不是来接工具断点的"、以及那一跳
        `→ running`，全在 `task_store` 那把锁里一次做完——这正是审查员要的那一句
        "claim the cursor atomically"。为什么必须连那一跳一起做：只加上锁而不把状态
        一起翻，第二条请求照样能在锁外读到 waiting_user、照样各自算出一份
        `pending_resume`。

        `claims_breakpoint=True` 的那一路多一条判据：**断点只许被一个答案接走**。
        这一趟在锁外读到"停在断点上"（`waiting_user`，或工单 #33 之后那条
        `failed` + 可信游标）、进锁时却发现状态**不是它读到的那一个**（并发的那一条刚
        接走 / 已经跑完 / 被取消 / 已经被别人接过去又跑坏了），那就不能顺着下面那句通用的
        "恢复任务"把整条子任务从第 1 步重跑——那笔钱与那枚重复的验证码就是这一格要挡的
        东西。所以这里认的是 `breakpoint_status` 那**一个**上一状态：#33 让 failed 也成
        了一枚等着被接走的断点，但"在 waiting_user 上读到的答案"绝不该拿去接一条它从没
        判过的 failed 记录（那份读是旧的，断点很可能已经被别人消费掉了）。
        `claims_breakpoint=False` 的那几路（pending / 被重启打断的 running / 游标不是
        工具断点的 waiting_user）照旧进来占位并跑，与改动前逐字同形：
        "被打断过、可以由人接着跑"本来就是跳转表给 running → running 那条自环留的口子
        （task_store.py:69），这一格不改它，只把"另一条请求正跑着"这一种从里面分出去。
        ⭐ `failed` 不在 `claims_breakpoint=False` 那几路里了：改动前它会走进这一路、
        然后从第 1 步重烧（工单 #33）；现在它在 `run` 里就被
        `_trusted_failed_breakpoint` 分成"可信 → 带答案走 claims_breakpoint=True"与
        "不可信 → 一句实话拒掉"，两条都不再从这里进通用续跑。
        不带答案停在工具断点上的那一路更早就 return 了（`run` 里那一格），压根不进这里。

        回 None 之外还顺手把 `_EXECUTING` 那一格留着；调用方必须在 finally 里还。
        """
        with _store_lock:
            if is_executing(task.task_id):
                return self._answer_not_accepted(task, "already_in_flight")
            if claims_breakpoint:
                # 只认**这一趟在锁外读到的那一个**断点态。#33 之后带可信游标的 failed 也
                # 是一枚等着被接走的断点，但"failed 也算断点态"绝不许放宽成"两种都算"：
                # 在 waiting_user 上读到答案的那一趟，进锁时发现状态已经变成 failed，
                # 说明另一条请求已经把断点接走过一轮（很可能还把它消费掉了）——手里那份
                # pending_resume 是**旧读**，按它再接一次就是把同一个外部副作用落两遍。
                expected = breakpoint_status or TaskStatus.WAITING_USER
                if task.status is not expected:
                    return self._answer_not_accepted(task, "breakpoint_taken")
                if task.status is TaskStatus.FAILED:
                    # 锁外读到的可信，不等于锁里仍然可信：这一枚断点可能就在刚才被别人
                    # 接过（consumed）、那条工具可能刚不再声明可挂起、或进度格子被改写过。
                    # 判据在锁里**再跑一遍同一个函数**（不抄第二份），跑不过就一句实话退回。
                    _, code = _trusted_failed_breakpoint(task)
                    if code != "resumable":
                        return self._refuse_rerun(task, code)
            # 公共占位 API（工单 #34）：判占位与占位本身都在 task_store 那把锁里，
            # 而外层这一把是同一个 RLock——判断点、占位、`→ running` 仍是同一批。
            if not claim_execution(task.task_id):
                return self._answer_not_accepted(task, "already_in_flight")
            try:
                task.set_status(TaskStatus.RUNNING)
            except Exception as e:
                # 跳转被拒（终态在这一瞬间被 /cancel 抢先落下来——工单 #34 之后取消
                # 也要先看这张表，所以这条是纵深防御）或落盘坏了（OSError）：
                # 占位当场还回去，并把话回给调用方而不是抛出去。抛的症状是 500，
                # 而这件事实本可以是一句"这条任务此刻是什么"。
                release_execution(task.task_id)
                return self._answer_not_accepted(task, "transition_refused", detail=e)
        return None

    @staticmethod
    def _release_execution(task_id: str) -> None:
        """还执行权。与认领同一把锁，免得认领那一方正读表时这里正在改。"""
        release_execution(task_id)

    def _answer_not_accepted(self, task: Task, reason: str,
                             detail: Optional[BaseException] = None) -> Dict[str, Any]:
        """落败者拿到的那一格：一句实话。不跑任何东西、不花一分钱。

        ⭐ v0.35 A5 之后，这一族里前三种落败是"**任务一格未改**"，而 `waiting_timeout`
        是唯一的例外：它在回话之前已经把状态与游标同批作废了（那一跳的判据与为什么
        是 cancelled 不是 failed，见 `task_store.expire_parked_task`）。两种都遵守同一句
        更强的纪律——**没有任何一次付费动作**，而且报出去的状态都是任务此刻真的状态。

        要说清"这一格是设计出来的答案，不是漏掉的"：
        - `status` 报的是任务**此刻真实**的状态（多半是 running；断点已被接走时可能是
          completed）。不另造一个状态字面量，也不把"我没做成"藏进 200 里当成功——
          响应里没有 `final_answer`，也没有把任务改成 failed。
        - `accepted: False` 就是判据本身：这一趟给的答案没有被采用。它替代了"两条都
          说自己办完了"那种自相矛盾的形状。
        - `reason` 给机器读：`already_in_flight`（本进程已有一趟正在跑它）/
          `breakpoint_taken`（断点已被另一枚答案接走）/ `transition_refused`
          （进锁之后状态机不收这一跳）/ `waiting_timeout`（v0.35 A5：停点挂太久已被
          作废，游标清空所以这个答案不可能再接回去）。
        - 如果此刻确实还停在一个问题上（游标认得出是工具断点），把 `question` 原样
          带上——落败的人该知道自己要不要重答；重答要等那一趟跑完、看新的问题，
          而不是拿同一个答案再砸一次。
        - 钱与预算：这一路一次工具都没调、一次模型都没叫、护栏与频控一本都不动，
          所以它既没花钱，也**没拿到**任何免费的重试额度；而它已经花掉的那一枚
          上游限流令牌（`throttle_paid_upstream` 在进这里之前就记过账）照旧不还，
          与本仓"被挡下的调用也不退款"那条口径同一条线。
        """
        status = getattr(task.status, "value", str(task.status))
        waiting = bool(getattr(task, "can_resume", False))
        suspension = _tool_suspension(task) if waiting else None
        messages = {
            "already_in_flight": (
                f"这条任务已经有一趟请求正在执行（此刻 {status}）。这一次什么都没跑、"
                "也没再花钱，你给的答案没有被采用。稍后用 GET /v1/tasks/{id} 看结果；"
                "如果它又停在新的问题上，带着那个新问题再答一次。"),
            "breakpoint_taken": (
                f"这一步的断点已经被另一趟请求接走了（此刻 {status}）。这一次没有"
                "重跑子任务、也没有再花钱，你给的答案没有被采用。"),
            "transition_refused": (
                f"这一趟没能开工：状态机不收 → running 那一跳，任务此刻是 {status}。"
                "什么都没跑、也没再花钱。"),
            # ⭐ v0.35 A5：挂太久的那一格。话要说清三件事：超时**不是**失败（作废落的是
            # cancelled）、断点已经不可挽回（游标清空了，同一个答案再砸也没用）、
            # 以及已经跑过的那几步一次都没重跑。
            "waiting_timeout": (
                f"这条任务停在断点上等人已经超时，停点刚才被作废了（此刻 {status}，"
                "游标已清空）：再带答案进来也接不回去，要接着办请重新发起一次任务。"
                "作废不是失败——这一步没有重跑、这一趟也没有叫模型、没花一分钱。"),
        }
        message = messages[reason]
        if detail is not None:
            message += f"（{type(detail).__name__}: {scrub_secrets(str(detail))}）"
        # 那句"任务一格未改"对前三种落败是真的（判完就return，谁都没动过任务），而
        # v0.35 A5 的 `waiting_timeout` 是唯一一个**先作废再回话**的：状态与游标在刚才
        # 那一跳里一起改了。日志不许在这一格说假话，所以按 reason 分岔——而不是把
        # "改了"藏起来，也不是给超时另写一份响应形状（那两处都会漂）。
        untouched = ("任务一格未改" if reason != "waiting_timeout"
                     else "任务的状态与游标已在那一跳里一起作废（已花掉的一格未动）")
        print(f"[Orchestrator] 任务 {task.task_id} 的这一趟没有被认领：{reason}"
              f"（此刻状态 {status}）——没跑工具、没叫模型、{untouched}")
        body = {
            "task_id": task.task_id,
            "status": status,
            "goal": task.goal,
            "accepted": False,
            "reason": reason,
            "can_resume": waiting,
            "current_subtask": task.current_subtask,
            "total_subtasks": len(task.subtasks),
            "message": message,
        }
        if suspension is not None:
            # 只在"此刻真的还停在一个认得出的工具断点上"时把那句问题带回去（判据就是
            # A1 的不变式 can_resume ⟺ waiting_user，加游标种类认得出）。症状对得上：
            # 赢家又差了一个答案（验证码之后要收货地址），输家手里握着的是旧答案——
            # 不给它新问题，它就只能拿旧的一遍遍砸。问题原文不写日志，只回这一格。
            body["question"] = suspension["question"]
        return body

    def _consume_breakpoint(self, task: Task) -> None:
        """把这一枚工具断点标成**已经接回去过**（工单 #33 第三句；工单 #37 收窄了调用条件）。

        为什么必须有这一格：`_trusted_failed_breakpoint` 判的是"游标可不可信"，而一枚
        已经被递过答案的断点在形状上**依然**完全可信（同 kind、同 tool_name、同
        subtask_index）。不标它，下一次带答案进来的请求就会把同一个包再递一次给同一个
        工具——订单再提交一次、验证码再发一次。那是比重复付费更难发现的一类钱：付了两遍
        不说，外部世界还多了第二次副作用。

        ⭐ 工单 #37——**调用条件**收窄了：本方法只在续跑**真的把答案递进了工具**
        （`outcome.tool_ran` 为真，即 `func(**kwargs)` 被调用过）时，才由 `run` 里那一处
        调用它。改动前它无条件落笔，于是"被闸门拒掉的续跑"（频控/审批/缺身份/断点形状不对
        ——工具一次都没被执行）也被标成 consumed，审查员量到的 case5 就是这么来的：用户答了、
        闸门说不、断点当场烧了，接下来只能重新开始。从"递给 execute_tool_detailed"到"工具真的
        跑了"之间的六种下场，各自的 consumed 取值：

            审批拒 / 审批待确认   → 不消费   tool_ran=False，答案从没进工具
            频控挡下（case5）     → 不消费   tool_ran=False
            缺身份 needs_user     → 不消费   tool_ran=False
            断点形状不对          → 不消费   tool_ran=False（`_resume_refusal` 先于函数体）
            进 execute 之前就抛   → 不消费   异常直接走 except→failed，根本到不了这一处
            工具正常返回          → 消费     tool_ran=True，答案已交付、副作用可能已落地
            工具跑一半才抛        → 消费     tool_ran=True：`_settle_suspension`/兜底 except 把
                                             "没跑成"与"跑了一半才坏"折成同一句 ok=False，外面
                                             分不开；分不清时宁可让人多点一次"重来"，也不能替
                                             他把可能已落地的副作用再落一遍（#33 那道门）
            工具又挂起一次        → 不在此处消费：`run` 里 `outcome.suspended` 一支已先 return
                                             走 `_park_suspension`，写的是一份**新**游标，旧的那枚
                                             consumed 与否都已不代表此刻

        不变式两面同一条线：**工具没收到答案 → 答案必须还能再递进去**（#37，上面那四行 False）；
        **工具真的收到过答案 → 这一枚只许被接一次**（#33，上面那两行 True）。两者都由同一个来自
        世界的信号 `tool_ran` 区分，不从 `ok`/`success` 猜。

        只在游标**还是这一枚**断点时落这一笔（`kind` 认得出、且没标过）：如果那一趟已经
        停在了新的断点上（`_park_suspension` 写过一份新游标），把新的标成"已消费"等于
        把用户刚要回答的那个问题判死。`consumed` 这个键住在那张**自由的** dict 里（A1 留
        它的理由就是"要往里加键不该每加一个就回来改 to_dict"），本票一个字不动存储骨架。
        """
        with _store_lock:
            cursor = getattr(task, "resume_cursor", None)
            if not isinstance(cursor, dict) or cursor.get("consumed"):
                return
            if cursor.get("kind") != TOOL_SUSPENSION_KIND:
                return
            task.resume_cursor = {**cursor, "consumed": True}
            save(task)
        print(f"[Orchestrator] 任务 {task.task_id} 的第 {task.current_subtask + 1} 步那个"
              "断点已被接过一次，同一枚答案不再递第二遍")

    def _refuse_rerun(self, task: Task, code: str,
                      suspension: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """工单 #33 的那一句实话：这一趟**什么都不跑**，并且说清为什么不跑。

        与 `_answer_not_accepted` 同一族纪律（没叫模型、没调工具、盘上一格未动、报出去的
        状态就是任务此刻真的状态），两问不一样：那一份说的是"这一枚断点没抢到"（并发），
        这一份说的是"这一枚断点不可信 / 还差你一个答案"（判据）。键名沿用同一族
        （`accepted` / `reason`），客户端只认一种形状。

        `refuse_code` 是给机器读的第二层：为什么不可信。取值与
        `_trusted_failed_breakpoint` 那份文档一一对应；`answer_required` 是唯一的
        "再答一次就行"，其余都要求一次刻意的重新开始（`fresh_start_required`）。

        ⚠️ 给 A4 的一句话，别说反：`answer_required` 这一格里 `can_resume` 报的是
        **False**，因为 A1 的不变式是"`can_resume` 为真 ⟺ 此刻状态是 `waiting_user``"
        （`task_store.py:214`），而这条任务是 `failed`。**能不能带答案来接**读的是
        `reason == "answer_required"` 与 `fresh_start_required is False` 那两格，不是
        `can_resume`。把 `can_resume` 在这里翻成 True 才是真出事——那等于让 GET
        `/v1/tasks/{id}` 对一条 `failed` 记录许诺"它在等人"，而盘上那格与状态机从此
        两个答案。

        ⚠️ 不许在这里加任何"挂了多久"那种墙上时钟读数：`/v1/agent/orchestrate` 的响应被
        A3 那条"非属主与不存在的 id 拿到逐字节相同的同一句话"判着（见
        `tests/test_v0350_a3_suspendable_tool_protocol.py`），窗口与到期时刻那两格走的
        也是既有那条 `waiting_timeout_info(..., elapsed=False)`。本方法一把都不用。
        """
        answer_required = code == "answer_required"
        reason = "answer_required" if answer_required else "breakpoint_untrusted"
        messages = {
            "answer_required": (
                f"这条任务之前失败，但它的工具断点还在、也接得回去（第 "
                f"{task.current_subtask + 1} 步）。带上 user_answer 才会从断点接上；"
                "这一次没有答案，所以什么也没跑、也没再花钱。不要另起一次任务——"
                "那会把已经付过钱的第 1~3 步再烧一遍。"),
            "no_breakpoint": (
                "这条任务在任何一个工具断点存在**之前**就失败了，手里没有「上次停在哪儿」"
                "的证据。要接着办请重新发起一次任务：这一趟什么都不跑、也不花一分钱，"
                "因为「从头再跑」与「重新发起」花的是同一笔钱，而前者会让人以为钱没多花。"),
            "wrong_kind": (
                "这条任务留着的游标不是一次工具断点（kind 认不出是 tool_suspension），"
                "所以不能按它续跑。要接着办请重新发起一次任务。"),
            "consumed_breakpoint": (
                "这一步的断点**已经接回去过一次**了（工具真的收到过答案），同一个答案再递"
                "一次等于把同一个外部副作用再落一遍，所以这里不再递。要接着办请重新发起"
                "一次任务；如果那一步其实没办成，请先人工确认它到底落没落地。"),
            "stale_subtask_index": (
                "这条任务留着的断点写的第几步与它此刻的进度不是同一步（游标飘了），"
                "按它续跑就是在猜。要接着办请重新发起一次任务。"),
            "unreadable_state": (
                "这条任务留着的断点读不出可用的形状（工具名/参数/续跑状态缺一样，"
                "或那份状态带不过一次重启），所以答案递不进去。要接着办请重新发起一次任务。"),
            "tool_not_suspendable": (
                "这条任务留着的断点指向的工具现在没有在注册表里声明可挂起（suspends），"
                "答案递不进去，许诺「能接」是撒谎。要接着办请重新发起一次任务。"),
        }
        status = getattr(task.status, "value", str(task.status))
        print(f"[Orchestrator] 任务 {task.task_id} 的这一趟想把 failed 接着跑，被拒："
              f"{code}（此刻状态 {status}）——没跑工具、没叫模型、任务一格未改")
        body = {
            "task_id": task.task_id,
            "status": status,
            "goal": task.goal,
            "accepted": False,
            "reason": reason,
            "refuse_code": code,
            "fresh_start_required": not answer_required,
            "end_reason": getattr(task, "end_reason", None),
            "can_resume": bool(getattr(task, "can_resume", False)),
            "current_subtask": task.current_subtask,
            "total_subtasks": len(task.subtasks),
            "error": task.error,
            "message": messages[code],
        }
        if suspension is not None:
            # 与 `_answer_not_accepted` 同一条口径：只在"此刻真的还停在一个认得出的工具
            # 断点上"时把那句问题带回去。不可信 / 已消费的那几种**不许**带问题——那等于
            # 给用户留一条"拿旧答案再砸一次"的路。问题原文不写日志，只回这一格。
            body["question"] = suspension["question"]
        return body

    def _record_failure(self, task: Task, error_msg: str) -> str:
        """把失败写进状态机，并且**自己绝不抛**。返回这一轮该报出去的状态值。

        改动前的两行裸写（`task.error = ...` + `task.set_status(FAILED)`）在任务已被
        别的路上定论时必抛 `IllegalTaskTransition`，而它就在 `except` 块里 → 端点 500。
        现在先问跳转表再跳，问与跳同锁（与认领同一把 `_store_lock`）。

        收不下时**一格都不改**，两条理由都是本仓付过账的那种：
        - 已经花掉的钱与已经定论的结论不许被一次没跑成的续写抹掉——一个 completed 的
          任务被改成 failed，或反过来带着 error 文案，都是在制造第二个真相；钱仍然在
          账上（`usage` 那本记的是每一次真发生过的调用，这一路一个字节都不碰），
          状态却从"办成了"退成"失败了"，等于把付过的账说成没付。
        - `error` 与 `status` 必须同批写：`set_status` 才是写盘点。先改 `error` 再被拒
          = 内存里有错误、盘上没有（与本文件既有那条"先记 error 再跳状态"的纪律同一个
          理由，反过来也成立）。所以那一格在真跳之前不落、跳一半失败还要吐回去。
        """
        with _store_lock:
            current = getattr(task.status, "value", str(task.status))
            if not can_transition(task.status, TaskStatus.FAILED):
                print(f"[Orchestrator] 任务 {task.task_id} 的 failed 那一跳被状态机拒了"
                      f"（{IllegalTaskTransition.__name__}：{current} → failed 不在合法"
                      f"出口里）：任务此刻仍是 {current}，终态记录一格都不改；这一轮的"
                      f"异常只回出口：{error_msg}")
                return current
            previous_error = task.error
            task.error = error_msg
            try:
                task.set_status(TaskStatus.FAILED)
                return TaskStatus.FAILED.value
            except IllegalTaskTransition as jump:
                # 上面那一问与这一跳之间的窗口：另一条线程抢先把终态落了进来
                # （工单 #34 之后 `/cancel` 也走同一把锁、且先看执行权，所以这一格
                # 现在是纵深防御；直接改 store 的测试与将来的写入方仍走得到）。
                # 同一句纪律——半个跳转不许留下，刚写的那一格 error 也吐回去。
                task.error = previous_error
                print(f"[Orchestrator] 任务 {task.task_id} 的 failed 那一跳被状态机拒了"
                      f"（{type(jump).__name__}：{jump}）：任务此刻是"
                      f" {getattr(task.status, 'value', task.status)}，一格都不改；"
                      f"这一轮的异常只回出口：{error_msg}")
                return getattr(task.status, "value", str(task.status))

    def _sweep_timed_out_parks(self, current_task_id: Optional[str] = None) -> None:
        """惰性清扫：把挂太久的停点作废。返回值没有意义——它不许影响这一趟任务。

        为什么挂在 `run` 里（第 1.9 步，认领之前）而不是挂在定时器上：本仓不起后台线程
        也不起定时器（判据见 A3 测试文件末尾那条 AST 锁），既有先例是
        `approval._sweep_locked` 的"每个入口都扫"与 `stream_runs` 的"清扫挂在新 run
        创建时"。这一格的入口就是 `/v1/agent/orchestrate`（同步 def，写盘本来就在线程池里）。

        `current_task_id` 交给清扫器当"这一条别动"：本趟要处理的那一条由
        `_expire_if_timed_out` 自己判，它说得出"你的答案没被采用，因为停点超时作废了"
        那一句；被清扫抢先的话，调用方只会拿到通用的"任务已被取消"（同一件事实，
        前者更准，而客户端要的正是那句可判的话）。

        ⭐ 默认窗口 0 时 `sweep_timed_out_parks` 头一句就返回，**一次遍历都不做、
        一个字节都不写**——所以"没配这一格的部署"与改动前逐字同形。

        整段裹在 try 里是有意的：清扫是家务，不是这一趟请求的一部分。它坏了（落盘
        OSError、某条记录形状怪）最坏的结果是"停点还挂着"，绝不能是这一次调用变 500。
        """
        try:
            voided = sweep_timed_out_parks(skip_task_ids=(current_task_id,)
                                           if current_task_id else ())
        except Exception as e:
            print(f"[Orchestrator] 停点超时清扫没跑动（{type(e).__name__}: "
                  f"{scrub_secrets(str(e))}）：跳过这一轮家务，所有挂着的断点照旧有效")
            return
        if voided:
            print(f"[Orchestrator] 已作废 {len(voided)} 条超时停点：{voided}"
                  f"（只是状态与游标，没有任何一条被重跑）")

    def _expire_if_timed_out(self, task: Task) -> Optional[Dict[str, Any]]:
        """这条挂起的任务是否已经超时；超时就把停点作废并回一句实话，否则回 None。

        回 None = 照旧按 `waiting_user` 走（默认窗口 0 恒走这一支，今天的形状一字不改）。
        回一个 dict = 这一趟的答案不接了，而它**必须**是"什么都不做"那一族：
        没叫模型、没调工具、没重跑子任务，与 `_answer_not_accepted` 同一条纪律。

        两处不许含糊的地方：
        - 「超时不是失败」：作废落的是 `cancelled`，`failed` 在这一族里一次都没出现
          （为什么，见 `task_store.expire_parked_task` 里工单 #33 那一段）。
        - 作废写不下去时（终态被并发抢先 / OSError）回 None：任务照旧挂在断点上，
          宁可让它继续等人，也不把"还没作废"说成"已作废"。
        """
        if not is_waiting_timed_out(task):
            return None
        if not expire_parked_task(task):
            return None
        # 话由 `_answer_not_accepted` 那一份说：同一个"你的答案没生效"的形状只许有一处
        # 定义，抄第二份就是漂移的开始（那句日志也在那里按 reason 分岔，不说假话）。
        return self._answer_not_accepted(task, "waiting_timeout")

    def _park_suspension(self, task: Task, suspension: Dict[str, Any],
                         subtask_index: int) -> Dict[str, Any]:
        """把一次工具挂起落到任务上。能接回去 → waiting_user；接不回去 → 如实 failed。

        为什么必须有这个分叉，而不是"凡挂起一律 waiting_user"：A1 那份存储有条不变式
        `can_resume ⟺ status 是 waiting_user`（task_store.py:190 与 :263 两处同一判据）。
        把一次**没人接得住**的挂起写成 waiting_user，等于让 GET /v1/tasks 对用户许诺
        "给个答案就能续"，而那个答案递进去只会被执行器拒掉——任务就永远挂在里面，
        比一句"这一步没做成"更坑人。failed 带原因才是实话（提案 §四 判据 4 反向的那一半：
        状态机不许说出兑现不了的话）。

        两处调用：TaskAgent 抛上来的那一次、以及带答案续跑后**又**差一个答案的那一次。
        """
        question = str(suspension.get("prompt") or "").strip()
        tool_name = str(suspension.get("tool_name") or "")
        if not suspension.get("can_resume"):
            # 先记 error 再跳状态：set_status 顺带落盘，反过来的话盘上那条 failed 会
            # 带着一格空的 error（与本文件失败出口同一条纪律）。
            task.error = (f"{tool_name} 想挂起等用户，但这次挂起接不回去："
                          "要么它没在注册表里声明可挂起（suspends），要么它交出的断点"
                          "不是一份能带过重启的 dict。问题原样留在这里，供人工接着办："
                          f"{question or '（工具没说要问什么）'}")
            task.set_status(TaskStatus.FAILED)
            print(f"[Orchestrator] 任务 {task.task_id} 的挂起接不回去，按失败落地")
            return {
                "task_id": task.task_id,
                "status": "failed",
                "goal": task.goal,
                "error": task.error,
                # ⭐ 工单 #33：这一格也是"定论"，判别器同一个来源（状态机那一跳给的值）。
                "end_reason": getattr(task, "end_reason", None),
                "question": question,
                "can_resume": False,
                "current_subtask": subtask_index,
                "total_subtasks": len(task.subtasks),
                "results": task.results,
            }
        task.wait_for_user(question=question, cursor=_suspension_cursor(
            suspension, subtask_index))
        print(f"[Orchestrator] 任务 {task.task_id} 挂在第 {subtask_index + 1} 步等用户输入")
        return {
            "task_id": task.task_id,
            "status": "waiting_user",
            "goal": task.goal,
            "question": question,
            "subtask_index": subtask_index,
            "total_subtasks": len(task.subtasks),
            "can_resume": True,
            # ⭐ v0.35 A5：挂起的那一刻就把"窗口多长、哪一刻作废"说出去
            # （`waiting_expires_at` 为 None = 运维没开作废窗口，永不做废——默认形态）。
            # UI 那半边的超时倒计时读的就是这一格，本票只提供数据不提供渲染；
            # "挂了多久"不在这里报，理由见上面 run() 里那一格（同一句"逐字节相同"的判据）。
            **waiting_timeout_info(task, elapsed=False),
            "results": task.results,
            "message": (f"任务停在第 {subtask_index + 1} 步：{tool_name} 需要用户给一个"
                        "答案才能继续。带上 user_answer 再调一次本端点，"
                        "会从断点接上，不重跑已经跑过的步骤。"),
        }

    def _build_summary_prompt(self, task: Task) -> str:
        """构造汇总提示词"""
        parts = [f"用户原始目标：{task.goal}\n"]
        parts.append("以下是各子任务及其执行结果：\n")

        for i, (subtask, result) in enumerate(zip(task.subtasks, task.results)):
            parts.append(f"--- 子任务 {i+1} ---")
            parts.append(f"任务内容：{subtask}")
            parts.append(f"执行结果：{result}")
            parts.append("")

        parts.append("请将以上所有子任务的结果整合成一个完整、连贯的回答，直接呈现给用户。")

        return "\n".join(parts)

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        查询任务状态
        Args:
            task_id: 任务ID
        Returns:
            任务状态信息字典，如果任务不存在则返回None
        """
        task = task_store.get(task_id)
        if not task:
            return None

        return {
            "task_id": task.task_id,
            "goal": task.goal,
            "status": task.status,
            "current_subtask": task.current_subtask,
            "total_subtasks": len(task.subtasks),
            "final_answer": task.final_answer,
            "error": task.error,
            "created_at": task.created_at,
            "cancelled": task.cancelled  # ⭐ 新增
        }
