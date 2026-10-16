"""
任务编排器模块
负责将复杂目标分解为子任务，并协调ReAct Agent逐步执行
支持任务状态持久化，可断点续传，支持任务取消
"""
from typing import List, Optional, Dict, Any
from .task_store import (IllegalTaskTransition, Task, TaskStatus, can_transition,
                         task_store)
# 认领那一跳要用的锁就是那份存储自己那把（改表与落盘同锁，task_store.py:274）。
# 跨模块引一个下划线开头的名字在本仓有先例（`app/web/web_router.py:24` 引
# `auth_router._client_ip`），而这里的理由更硬：判"这条任务在不在等人"与写它的那一跳
# 必须在**同一把**锁里，另起一把只是把窗口从一处搬到两处。
from .task_store import _lock as _store_lock
from .planner import Planner
from .executor import Executor
from .task_agent import TaskNeedsUser
from ..core.llm_client import get_llm_response
from ..core.providers import scrub_secrets
from app.tools.executor import execute_tool_detailed

# ⭐ v0.35 A3：工具挂起时写进 A1 那份 `Task.resume_cursor` 的游标**种类标记**。
# 为什么要有这个键：`resume_cursor` 是一张自由 dict（A1 刻意如此，见 task_store.py 里
# `wait_for_user` 的注释——"要往里加键不该每加一个就回来改 to_dict"），所以读侧必须
# 能认出"这一张是工具断点"还是别的什么，认不出就什么都不接（fail-closed）。
TOOL_SUSPENSION_KIND = "tool_suspension"

# ⭐⭐ 本进程里"已经有人在跑"的任务清单（独立审查员复现出来的那一格，见 A3 测试文件
# 末尾那族用例）。
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
# 的假案（谁也叫不动它），而这张表跟着进程一起清空。
#
# 边界要说实话：这张表管的是**本进程**。多个 worker 各持一份，跨进程的同一枚停点还抢
# 得动（现网是本机 `127.0.0.1:8000` 单进程：`run_backend.py:63` 那句
# `uvicorn.run(app, host="127.0.0.1", port=8000)` 不带 workers，换包脚本也按这一份起）；
# 真要多进程，得换一把跨进程的锁（文件锁或 sqlite），不在这一格里，已作为 follow-up 上报。
_EXECUTING: set = set()


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

            # 如果任务失败，询问是否重新执行
            if task.status == TaskStatus.FAILED:
                print(f"[Orchestrator] 任务 {task_id} 之前失败，从断点重新执行")
                # 从当前中断的子任务继续
            elif task.status == TaskStatus.WAITING_USER:
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
                            "message": (f"任务停在第 {task.current_subtask + 1} 步等用户"
                                        "给一个答案。带上 user_answer 才会从断点继续；"
                                        "这一次没有答案，所以什么也没跑、也没再花钱。"),
                        }
                    pending_resume = (suspension, answer)
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

        # --- 2. 原子认领这一趟执行（顺带把状态翻成 running）---
        # ⭐ v0.35 A1：走状态机而不是字段赋值。到这里合法的上一状态只有 pending /
        # running（被打断后续跑）/ failed（断点重跑，上面那句 print 就是它）/
        # waiting_user（等到了人，从游标记的那一步接）。表外的跳转直接抛，
        # 不再让"半个状态"悄悄写进盘。
        # ⭐ 这一跳从原来的裸 `task.set_status(...)` 挪进了 `_claim_execution`：
        # "读到 waiting_user"与"翻成 running"之间原来没有任何东西挡着，两条答案因此
        # 都能读到你都能动手，或者后到的那条读不到你、掉进通用续跑那一路把前三步重跑。
        # 判与跳现在同锁同批，见那份方法的注释。
        refused = self._claim_execution(task, claims_breakpoint=pending_resume is not None)
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
                         claims_breakpoint: bool = False) -> Optional[Dict[str, Any]]:
        """原子地占住这条任务的执行权。占到回 `None`；占不到回的就是要给调用方的那一格。

        判"有没有人占着"、判"这一趟是不是来接工具断点的"、以及那一跳
        `→ running`，全在 `task_store` 那把锁里一次做完——这正是审查员要的那一句
        "claim the cursor atomically"。为什么必须连那一跳一起做：只加上锁而不把状态
        一起翻，第二条请求照样能在锁外读到 waiting_user、照样各自算出一份
        `pending_resume`。

        `claims_breakpoint=True` 的那一路多一条判据：**断点只许被一个答案接走**。
        这一趟在锁外读到 waiting_user、进锁时却发现状态已经不是它了（并发的那一条
        刚接走 / 已经跑完 / 被取消），那就不能顺着下面那句通用的"恢复任务"把整条
        子任务从第 1 步重跑——那笔钱与那枚重复的验证码就是这一格要挡的东西。
        `claims_breakpoint=False` 的那几路（pending / 被重启打断的 running / failed 断点
        重跑 / 游标不是工具断点的 waiting_user）照旧进来占位并跑，与改动前逐字同形：
        "被打断过、可以由人接着跑"本来就是跳转表给 running → running 那条自环留的口子
        （task_store.py:69），这一格不改它，只把"另一条请求正跑着"这一种从里面分出去。
        不带答案停在工具断点上的那一路更早就 return 了（`run` 里那一格），压根不进这里。

        回 None 之外还顺手把 `_EXECUTING` 那一格留着；调用方必须在 finally 里还。
        """
        with _store_lock:
            if task.task_id in _EXECUTING:
                return self._answer_not_accepted(task, "already_in_flight")
            if claims_breakpoint and task.status is not TaskStatus.WAITING_USER:
                return self._answer_not_accepted(task, "breakpoint_taken")
            _EXECUTING.add(task.task_id)
            try:
                task.set_status(TaskStatus.RUNNING)
            except Exception as e:
                # 跳转被拒（终态在这一瞬间被 /cancel 抢先落下来）或落盘坏了（OSError）：
                # 占位当场还回去，并把话回给调用方而不是抛出去。抛的症状是 500，
                # 而这件事实本可以是一句"这条任务此刻是什么"。
                _EXECUTING.discard(task.task_id)
                return self._answer_not_accepted(task, "transition_refused", detail=e)
        return None

    @staticmethod
    def _release_execution(task_id: str) -> None:
        """还执行权。与认领同一把锁，免得认领那一方正读表时这里正在改。"""
        with _store_lock:
            _EXECUTING.discard(task_id)

    def _answer_not_accepted(self, task: Task, reason: str,
                             detail: Optional[BaseException] = None) -> Dict[str, Any]:
        """落败者拿到的那一格：一句实话。不跑任何东西、不花一分钱、不改一个字节。

        要说清"这一格是设计出来的答案，不是漏掉的"：
        - `status` 报的是任务**此刻真实**的状态（多半是 running；断点已被接走时可能是
          completed）。不另造一个状态字面量，也不把"我没做成"藏进 200 里当成功——
          响应里没有 `final_answer`，也没有把任务改成 failed。
        - `accepted: False` 就是判据本身：这一趟给的答案没有被采用。它替代了"两条都
          说自己办完了"那种自相矛盾的形状。
        - `reason` 给机器读：`already_in_flight`（本进程已有一趟正在跑它）/
          `breakpoint_taken`（断点已被另一枚答案接走）/ `transition_refused`
          （进锁之后状态机不收这一跳）。
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
        }
        message = messages[reason]
        if detail is not None:
            message += f"（{type(detail).__name__}: {scrub_secrets(str(detail))}）"
        print(f"[Orchestrator] 任务 {task.task_id} 的这一趟没有被认领：{reason}"
              f"（此刻状态 {status}）——没跑工具、没叫模型、任务一格未改")
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
                # 上面那一问与这一跳之间的窗口：另一条线程（`/v1/tasks/{id}/cancel`
                # 的判据在锁外）抢先把终态落了进来。同一句纪律——半个跳转不许留下，
                # 刚写的那一格 error 也吐回去。
                task.error = previous_error
                print(f"[Orchestrator] 任务 {task.task_id} 的 failed 那一跳被状态机拒了"
                      f"（{type(jump).__name__}：{jump}）：任务此刻是"
                      f" {getattr(task.status, 'value', task.status)}，一格都不改；"
                      f"这一轮的异常只回出口：{error_msg}")
                return getattr(task.status, "value", str(task.status))

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
