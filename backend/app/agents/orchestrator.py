"""
任务编排器模块
负责将复杂目标分解为子任务，并协调ReAct Agent逐步执行
支持任务状态持久化，可断点续传，支持任务取消
"""
from typing import List, Optional, Dict, Any
from .task_store import Task, TaskStatus, task_store
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

        # --- 2. 更新任务状态为执行中 ---
        # ⭐ v0.35 A1：走状态机而不是字段赋值。到这里合法的上一状态只有 pending /
        # running（被打断后续跑）/ failed（断点重跑，上面那句 print 就是它）/
        # waiting_user（等到了人，从游标记的那一步接）。表外的跳转直接抛，
        # 不再让"半个状态"悄悄写进盘。
        task.set_status(TaskStatus.RUNNING)

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

            # 先记 error 再跳状态：set_status 顺带落盘，顺序反过来的话盘上那条
            # failed 记录会带着一格空的 error（"失败了但不知道为什么"最难查）。
            task.error = error_msg
            task.set_status(TaskStatus.FAILED)

            return {
                "task_id": task.task_id,
                "status": "failed",
                "goal": task.goal,
                "error": error_msg,
                "current_subtask": task.current_subtask,
                "total_subtasks": len(task.subtasks)
            }

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
