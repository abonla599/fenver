"""
TaskAgent - 单智能体，可在循环中调用工具完成任务
完全独立，不依赖 ReActAgent
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # backend 目录

import json
from app.core.guardrails import RoundGuard
from app.core.llm_client import get_llm_response
from app.tools.executor import execute_tool_detailed
from app.tools.registry import is_available


class TaskNeedsUser(Exception):
    """⚠️ v0.35 A3：这一步不是失败，是**缺用户手里的一个答案**。

    谁抛：`TaskAgent._run_fallback`（工具交出挂起结果时）。谁接：
    `Orchestrator.run` —— 它手里才有 `Task`，能把断点与问题落成
    `TaskStatus.waiting_user`（A1 那份 `resume_cursor`）。

    为什么要**恰好一个**接住它的 except（`except TaskNeedsUser: raise`）：
    TaskAgent 的工具块整个躺在 `except Exception` 里（这份文件的老写法），
    而那条兜底把任何异常都说成"工具调用解析错误，请重试或直接回答"——
    真让它接住这一条，挂起会被模型当成一次坏参数，于是它**重试或直接编一个答案**，
    这就是提案 §二 第一行写的"问了就是整轮失败"。接住点只有编排器那一个，
    是为了"要不要挂起"这件事只有一个地方说了算。
    """

    def __init__(self, question: str, suspension: dict):
        super().__init__(str(question or ""))
        self.question = str(question or "")
        # 执行器那包断点原样带上：tool_name / arguments / prompt / resume_state / can_resume
        self.suspension = dict(suspension or {})

    @property
    def can_resume(self) -> bool:
        """这一挂在协议上接不接得回去（判据只在执行器那一处，这里不另算一遍）。"""
        return bool(self.suspension.get("can_resume"))


class TaskAgent:
    """
    基础任务智能体：
    - 接收任务描述
    - 循环调用 LLM + 工具，直到得到最终答案
    """
    def __init__(self, name: str, model: str, tools_schema: list, tools: dict,
                 system_prompt: str = "", user_id: str = None, provider_id: str = None):
        # user_id 一路从凭据里带下来：工具执行必须知道"是谁在调"，否则 needs_user
        # 那类工具（查某个人自己的数据）只能一律拒绝。缺身份是 fail-closed，
        # 不是"当成没身份的人"。
        # provider_id 是发起人那口池子的门牌：调用统一出口时带上它，
        # 别人的默认、站级的默认都不许顶替这个人选过的模型（v0.25 R1）。
        self.name = name
        self.model = model
        self.tools_schema = tools_schema
        self.tools = tools
        self.system_prompt = system_prompt
        self.user_id = user_id
        self.provider_id = provider_id
        self.max_turns = 8
        # 与 ReActAgent 同一个口径：这两行 print 打的是工具入参与结果原文，
        # 默认关掉；调试时把它翻成 True 即可，不必再往源码里塞临时输出。
        self.verbose = False

    def run(self, task: str, resumed_tool: dict = None) -> str:
        """跑完一个子任务，返回给编排器的那句话。

        `resumed_tool`（⭐ v0.35 A3）是"这个子任务里那个工具已经从断点接回来了"的
        一包：`{"tool_name", "result"}`，由 `Orchestrator` 在带答案续跑时填。
        它被补成对话里**已有的一问一答**（见 _run_fallback 里那段种子），于是模型
        看到的上下文与挂起前接得上——提案 §四 判据 1 那句"全上下文不丢"落在这里。
        不给就是全新的一次，行为与本格加入之前逐字相同。
        """
        return self._run_fallback(task, resumed_tool)

    def _run_fallback(self, task: str, resumed_tool: dict = None) -> str:
        """降级方案：用纯文本提示，让模型决定是否调用工具（基于规则）"""
        # 清单按可用性过滤，和给原生 tool_calls 那条路同一个口径
        # （get_available_tools_schema）。旧写法直接遍历全量注册表：依赖不可用时
        # schema 里没它、提示词里却写着它，模型照样调、照样拿错误，再凭错误硬编答案。
        tool_descriptions = "\n".join([
            f"- {name}: {info['description']}"
            for name, info in self.tools.items()
            if is_available(info)
        ])

        prompt = f"""你是一个智能助手，可以使用以下工具完成任务。

可用工具：
{tool_descriptions}

当你需要使用工具时，请在回复中用以下格式：
<tool_call>
{{"name": "工具名", "arguments": {{"参数名": "值"}}}}
</tool_call>

你会收到工具执行结果，然后继续回答。如果不需要工具，直接给出最终答案。

用户任务: {task}"""

        messages = [
            {"role": "system", "content": "你是一个有能力使用工具的助手。"},
            {"role": "user", "content": prompt}
        ]

        # v0.31 护栏：与 ReAct/两条聊天路径同口径。原先这条路径没有护栏，
        # max_turns 循环里能连续拉起 N 次容器，且无重复检测与失败退避。
        # 每次 execute 建一个实例：跨请求复用会把不同人的计数混在一起。
        #
        # ⭐ 工单 #31 边角 N5：实例必须先于续跑种子建。种子是**本轮真实吃进去的一份
        # 工具结果**，在原先的位置（种子之后建）它绕过了 `clip_output`——于是同一轮
        # 里"续跑结果 + 后续工具输出"的累计字数比别的路径多出一整份（上限 16000），
        # 而那句"与执行器 per-call 4000 互补"的预算只对后者生效。执行器那一步已经把
        # `outcome.text` 裁到 4000，这里再记一次不是重复计费，是让**轮预算**看见它。
        guard = RoundGuard()

        # ⭐ v0.35 A3：断点续跑的种子。形状照抄本文件自己那一问一答（assistant 那行带
        # 工具调用标记、user 那行是"工具执行结果: …"），不另发明第三种形状——模型在
        # 挂起前看到的就是这个形状，续上来才接得上（提案 §四 判据 1「全上下文不丢」）。
        if resumed_tool:
            _tag = "tool_call"        # 与本文件那条降级协议同一个标记，不写第二份拼法
            messages.append({
                "role": "assistant",
                "content": f"<{_tag}>" + json.dumps(
                    {"name": str(resumed_tool.get("tool_name", "")), "resumed": True},
                    ensure_ascii=False) + f"</{_tag}>"})
            seed, _ = guard.clip_output(str(resumed_tool.get("result", "")))
            messages.append({"role": "user",
                             "content": f"工具执行结果: {seed}"})

        for turn in range(self.max_turns):
            response = get_llm_response(self.model, messages,
                                        provider_id=self.provider_id,
                                        user_id=self.user_id)
            
            # 检查是否有工具调用
            if "<tool_call>" in response:
                # 提取工具调用
                start = response.index("<tool_call>") + len("<tool_call>")
                end = response.index("</tool_call>")
                tool_json = response[start:end].strip()
                try:
                    tool_call = json.loads(tool_json)
                    tool_name = tool_call["name"]
                    tool_args = tool_call.get("arguments", {})
                    
                    if self.verbose:
                        print(f"    [{self.name}] 调用工具: {tool_name}({tool_args})")

                    # v0.31 护栏：执行前检查（成本上限 + 重复检测 + 失败退避）
                    guard_blocked = guard.check(
                        tool_name, tool_args if isinstance(tool_args, dict) else {})
                    if guard_blocked:
                        if self.verbose:
                            print(f"    [{self.name}] 🚫 护栏拦下 {tool_name}: {guard_blocked}")
                        messages.append({"role": "assistant", "content": response})
                        messages.append({"role": "user",
                                         "content": f"工具执行结果: {guard_blocked}"})
                        continue

                    # 执行工具走执行器，不直接 call 注册表里的函数对象。差三件事：
                    # ① needs_user 的工具必须由服务端补身份，模型自己填的 user_id 作废；
                    # ② 输出要按上下文预算截断；③ 未知工具/参数错误要回一句模型读得懂的话。
                    # 绕过它，这三条同时失效。
                    #
                    # 第④件：审批。执行器那道闸门在没带票据时直接拒掉需要审批的工具
                    # （这里不传 granted_ticket），所以这条路径同样拿不到无审批的代码执行。
                    #
                    # 第⑤件（⭐ v0.35 A3）：挂起。这一行换成 detailed 那一条不是为了多拿
                    # 什么，是因为**只读文本分不出"失败"和"停在断点问用户"**：老写法
                    # `str(result).startswith("✓")` 会把"请输入短信验证码"记成一次工具
                    # 失败，而失败退避两次就熔断（`guardrails.record_outcome` 里
                    # `fails >= fail_threshold` 那一支）——挂起的任务
                    # 于是永远续不上。结构化那一格（outcome.suspended）才是判据。
                    outcome = execute_tool_detailed(tool_name, tool_args,
                                                    user_id=self.user_id)
                    # 护栏：挂起这一格**照旧记这一轮**（轮次是真花掉的，不记就是给
                    # 重试留免费额度，与频控那份"尝试即计数"同一个口径）。⭐ 工单 #76a：
                    # 第三态由 `suspended=` 自己说，读的是 outcome.suspended 那格世界事实
                    # （执行器在 `ToolOutcome` 上统一赋值的那一格）——不是文本前缀（D1），也不从 ok 反猜：
                    # 挂起时 ok 同样是 False，只传 ok 会让同一轮里第二次挂起把该工具熔断。
                    # #29 已在 `guardrails.record_outcome` 的 `suspended=` 入口开好这一格，这里接上它。
                    guard.record_outcome(tool_name, ok=bool(outcome.ok),
                                         suspended=bool(outcome.suspended))
                    if outcome.suspended:
                        # 不回填给模型、不重试、不编答案：整条子任务在这里交回编排器，
                        # 由它把断点落成 waiting_user。顺序要紧——必须在 clip_output
                        # 之前，否则那句问话还会占用本轮输出预算（它压根不给模型看）。
                        raise TaskNeedsUser(outcome.suspension.get("prompt", ""),
                                            outcome.suspension)
                    result, _ = guard.clip_output(str(outcome.text))

                    # 将结果反馈给模型
                    messages.append({"role": "assistant", "content": response})
                    messages.append({"role": "user", "content": f"工具执行结果: {result}"})
                    
                    if self.verbose:
                        print(f"    [{self.name}] 结果: {result[:100]}...")
                except TaskNeedsUser:
                    # ⭐ v0.35 A3 的生死线，位置就是判据：必须排在下面那条兜底 except
                    # **之前**。删掉这两行，挂起会被兜底那句折成"工具调用解析错误: …，
                    # 请重试或直接回答"回灌给模型 —— 第三态当场退化回失败态，模型会
                    # 照着"直接回答"替用户把验证码编出来。负向锁见
                    # tests/test_v0350_a3_suspendable_tool_protocol.py。
                    raise
                except Exception as e:
                    messages.append({"role": "assistant", "content": response})
                    messages.append({"role": "user", "content": f"工具调用解析错误: {e}，请重试或直接回答。"})
            else:
                # 没有工具调用，视为最终答案
                return response.strip()

        return "达到最大循环次数，任务未完成。"