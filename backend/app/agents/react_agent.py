"""
ReAct (Reasoning + Acting) 智能体模块
实现思考-行动-观察循环，支持工具调用和多轮推理

v0.31：工具调用已全面走原生 tool_calls（与 pipeline.py / streaming.py 同一口径）。
旧版从正文解析 JSON 的文本协议路径已删除（_extract_tool_calls），防复活契约锁在
tests/test_v031_r4_text_protocol_removed.py。
"""
import time
import json
from typing import List, Dict, Any, Optional

from app.core.providers import store, build_client, scrub_secrets
from app.core import usage
from app.core.guardrails import RoundGuard
from app.tools.executor import execute_tool
from app.tools.registry import get_available_tools_schema


class ReActAgent:
    """
    ReAct智能体
    核心流程：
    1. Thought（思考）：分析当前状态，决定下一步行动
    2. Action（行动）：调用工具或给出最终答案
    3. Observation（观察）：获取工具执行结果
    4. 重复以上步骤直到得出最终答案或达到最大轮次
    """

    def __init__(
        self,
        model: str = None,
        max_turns: int = 10,
        # 默认关。这些 print 会把模型回复原文、工具入参与工具结果整段写进 stdout，
        # 而桌面版部署里 stdout 就是 EXE 旁的日志文件：里面躺着用户查询的内容和
        # 带路径的参数，却没有人订阅读它。判据是"开着才响"，不是"写着好看"——
        # 需要看的时候 `ReActAgent(verbose=True)` 一行就回来，删掉反而两失。
        verbose: bool = False
    ):
        """
        初始化ReAct智能体
        Args:
            model: provider id（或旧式模型名）；留空 = 用setDefault的那份配置。
            max_turns: 最大思考-行动轮次
            verbose: 是否打印详细日志
        """
        self.model = model
        self.max_turns = max_turns
        self.verbose = verbose
        self.tools_schema = get_available_tools_schema()

    def run(
        self,
        task: str,
        context: Optional[List[Dict[str, str]]] = None,
        max_duration: int = 120,
        user_id: str = None,
        provider_id: str = None
    ) -> str:
        """
        执行任务的主方法
        Args:
            task: 任务描述
            context: 可选的上下文消息列表
            max_duration: 最大执行时间（秒），默认120秒
            user_id: 这次运行属于谁。needs_user 类工具（读日程/读记忆）由执行器
                用这个值覆盖模型给的参数；不传的话这些工具只能落在 default_user 上。
                它同时是账本上那一人（v0.25 R1：/v1/agent/run 对登录用户开放）
            provider_id: 发起人解析出的模型服务；None 时按他的默认与池子走
        Returns:
            最终答案字符串
        """
        provider = store.resolve(provider_id, legacy_model=self.model,
                                 user_id=user_id)
        client = build_client(provider)

        # --- 超时控制 ---
        start_time = time.time()

        # v0.31 护栏：单轮工具护栏。原先这条路径完全没有护栏，于是 max_turns（默认 10）
        # 高于 MAX_TOOL_ROUNDS（5）时，一次请求能连拉 10 轮容器、烧 10 次真钱，
        # 而且没有任何重复检测与失败退避。护栏实例必须是**每次 run 一个**：
        # 跨请求复用会把不同用户/不同问题的计数混在一起。
        guard = RoundGuard()

        # --- 初始化消息列表 ---
        messages = []

        # 系统提示词
        system_prompt = self._build_system_prompt()
        messages.append({"role": "system", "content": system_prompt})

        # 添加上下文（如果有）
        if context:
            messages.extend(context)

        # 添加用户任务
        messages.append({"role": "user", "content": task})

        if self.verbose:
            print(f"[ReActAgent] 开始执行任务: {task}")
            print(f"[ReActAgent] 最大轮次: {self.max_turns}, 最大时长: {max_duration}秒")

        # --- 记账状态（与 pipeline/streaming 同口径） ---
        billed = {k: 0 for k in ("prompt_tokens", "completion_tokens",
                                 "total_tokens", "reasoning_tokens", "cached_tokens")}
        rounds = 0
        ok = True

        try:
            # --- ReAct 主循环 ---
            for turn in range(self.max_turns):
                # ========== 超时检查 ==========
                elapsed_time = time.time() - start_time
                if elapsed_time > max_duration:
                    timeout_msg = f"任务执行超时（已执行{elapsed_time:.1f}秒，超过{max_duration}秒限制）。以下是目前已获得的部分结果。"
                    if self.verbose:
                        print(f"[ReActAgent] ⏰ {timeout_msg}")

                    try:
                        summary = self._force_summarize(client, provider, messages, task)
                        return f"{timeout_msg}\n\n{summary}"
                    except:
                        return timeout_msg

                if self.verbose:
                    remaining_time = max_duration - elapsed_time
                    print(f"[ReActAgent] ====== 第 {turn+1}/{self.max_turns} 轮（剩余{remaining_time:.0f}秒）======")

                # --- 1. 调用LLM获取响应（原生 tool_calls） ---
                try:
                    kwargs: Dict[str, Any] = {
                        "model": provider["model"],
                        "messages": messages,
                        "temperature": 0.7,
                    }
                    if self.tools_schema:
                        kwargs["tools"] = self.tools_schema
                        kwargs["tool_choice"] = "auto"
                    response = client.chat.completions.create(**kwargs)
                    rounds += 1
                    msg = response.choices[0].message
                    # 记账：与 pipeline/streaming 同口径，取上游回传的 usage
                    u = getattr(response, "usage", None)
                    if u is not None:
                        billed["prompt_tokens"] += getattr(u, "prompt_tokens", 0) or 0
                        billed["completion_tokens"] += getattr(u, "completion_tokens", 0) or 0
                        billed["total_tokens"] += getattr(u, "total_tokens", 0) or 0
                        details = getattr(u, "completion_tokens_details", None)
                        billed["reasoning_tokens"] += (getattr(details, "reasoning_tokens", 0) or 0) if details else 0
                        pdetails = getattr(u, "prompt_tokens_details", None)
                        billed["cached_tokens"] += (getattr(pdetails, "cached_tokens", 0) or 0) if pdetails else 0
                except Exception as e:
                    # 这句话会原样进 /v1/agent/run 的响应体：上游把 Authorization
                    # 打印回来不是假设，出口先过 scrub_secrets（约束：错误文案不裸走）。
                    error_msg = f"调用LLM出错: {scrub_secrets(str(e))}"
                    if self.verbose:
                        print(f"[ReActAgent] ❌ {error_msg}")
                    return error_msg

                # --- 2. 检查原生 tool_calls ---
                if msg.tool_calls:
                    # 把助手消息（含 tool_calls）追加进历史
                    msgs_dump = msg.model_dump() if hasattr(msg, "model_dump") else {
                        "role": "assistant", "content": msg.content,
                        "tool_calls": [{"id": tc.id, "type": "function",
                                        "function": {"name": tc.function.name,
                                                     "arguments": tc.function.arguments}}
                                       for tc in msg.tool_calls],
                    }
                    messages.append(msgs_dump)

                    for call_idx, tc in enumerate(msg.tool_calls):
                        tool_name = tc.function.name
                        try:
                            tool_args = json.loads(tc.function.arguments)
                        except (ValueError, TypeError) as e:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": f"工具参数解析失败（不是合法 JSON）: {e}",
                            })
                            continue

                        if self.verbose:
                            print(f"[ReActAgent] 🔧 调用工具: {tool_name}")
                            print(f"[ReActAgent] 参数: {json.dumps(tool_args, ensure_ascii=False)}")

                        # v0.31 护栏：执行前检查（成本上限 + 重复检测 + 失败退避）。
                        # 与两条聊天路径同一口径，接在同一个 RoundGuard 上。
                        guard_blocked = guard.check(
                            tool_name, tool_args if isinstance(tool_args, dict) else {})
                        if guard_blocked:
                            if self.verbose:
                                print(f"[ReActAgent] 🚫 护栏拦下 {tool_name}: {guard_blocked}")
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": guard_blocked,
                            })
                            continue

                        try:
                            # 注意这里**不传 granted_ticket**：审批闸门在执行器里，
                            # 没带票据的 execute_code 会被执行器直接拒掉。ReAct 这条路
                            # 是同步跑完就返回的，装不下"等用户点确认"这个交互，
                            # 所以这里如实告诉模型改用聊天路径，而不是替用户批准。
                            tool_result = execute_tool(tool_name, tool_args, user_id=user_id)
                            # 局部名不叫 ok：run 级的 ok 在 :108 已有含义（这次
                            # 上游调用整体成不成功），在这里覆盖它会让一次工具失败
                            # 被记成整轮 run 失败。护栏只认这一次调用自己的成败。
                            tool_ok = str(tool_result).startswith("✓")
                            guard.record_outcome(tool_name, tool_ok)
                            if self.verbose:
                                print(f"[ReActAgent] ✅ 工具结果: {str(tool_result)[:200]}...")
                        except Exception as e:
                            tool_result = f"工具执行错误: {str(e)}"
                            guard.record_outcome(tool_name, False)
                            if self.verbose:
                                print(f"[ReActAgent] ❌ {tool_result}")

                        # 单轮累计输出预算闸（与 executor 的 per-call 4000 互补）。
                        tool_result, _ = guard.clip_output(str(tool_result))
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": str(tool_result)
                        })
                else:
                    # --- 没有工具调用，视为最终答案 ---
                    if self.verbose:
                        print(f"[ReActAgent] 🎯 获得最终答案")
                    return msg.content or "（模型未返回内容）"

            # --- 达到最大轮次，强制总结 ---
            if self.verbose:
                print(f"[ReActAgent] ⚠️ 达到最大轮次 {self.max_turns}，强制总结")

            try:
                return self._force_summarize(client, provider, messages, task)
            except:
                return "抱歉，任务执行达到最大轮次限制，未能得出完整结论。"
        except BaseException:
            ok = False
            raise
        finally:
            # 与 pipeline/streaming 同口径：记账用上游回传的数，抛出去的异常也要落一行
            usage.record_call(user_id=(user_id or "").strip() or "unattributed",
                              provider_id=provider["id"],
                              paid_by=provider.get("paid_by") or "operator",
                              tool_rounds=max(0, rounds - 1), ok=ok, **billed)

    def _build_system_prompt(self) -> str:
        """构建系统提示词"""
        tool_descriptions = []
        for tool in self.tools_schema:
            tool_descriptions.append(f"- {tool['function']['name']}: {tool['function']['description']}")

        tools_text = "\n".join(tool_descriptions) if tool_descriptions else "无可用工具"

        return f"""你是一个智能助手，能够使用工具来完成复杂任务。

可用工具：
{tools_text}

工作流程：
1. 分析用户的任务，思考需要使用哪些工具
2. 调用相应的工具获取信息或执行操作
3. 根据工具返回的结果，决定下一步行动
4. 当你认为已经收集到足够的信息时，直接给出最终答案

注意事项：
- 每次只调用必要的工具，不要过度调用
- 如果工具返回错误，尝试其他方法或向用户说明
- 最终答案应该清晰、完整，直接回答用户的问题
- 如果任务无法完成，诚实告知用户原因
"""

    def _force_summarize(self, client, provider, messages: List[Dict], task: str) -> str:
        """强制总结当前对话"""
        summary_prompt = f"请基于以上对话历史，对任务「{task}」给出你目前能得出的最佳答案。即使信息不完整，也请尽量提供有价值的内容。"

        summarize_messages = messages.copy()
        summarize_messages.append({"role": "user", "content": summary_prompt})

        response = client.chat.completions.create(
            model=provider["model"],
            messages=summarize_messages,
            temperature=0.5,
        )
        return response.choices[0].message.content or ""
