"""工具护栏三件套：重复调用检测、单轮成本上限、失败退避。

v0.31 新增。挂在现有 usage 账本同轮记录上（guard_events 字段），不建新表。
两条聊天路径（pipeline + streaming）共用这一个模块，ReAct 那条也过这里。

为什么三个放一个模块：它们共享同一份"单轮状态"（RoundGuard 实例），
触发后的回填文案格式也一致（给模型看的一句话 + 给人看的面板标记）。
拆开反而要让三个类互相传同一份计数器。
"""
import json
import threading
from typing import Dict, List, Optional, Tuple


# ---------- 默认阈值（管理员可调，见 config_store） ----------
# 重复检测：同 (tool, 规范化参数) 在单轮内出现 ≥3 次 → 熔断
DUPLICATE_THRESHOLD = 3
# 成本上限：单轮工具轮次上限（5 轮封顶保留为硬底线）
MAX_TOOL_ROUNDS = 5
# 单轮 token 上限（估算口径，不引 tokenizer）
MAX_ROUND_TOKENS = 50000
# 失败退避：同一工具连续失败 2 次 → 熔断
CONSECUTIVE_FAIL_THRESHOLD = 2
# R2 工具输出预算闸：单轮累计工具输出字符上限（与 executor 的 per-call 4000 互补）
# 4000 × 4 次 = 16000，超过就截断并告知模型
MAX_ROUND_OUTPUT_CHARS = 16000


def _normalize_args(args: dict) -> str:
    """参数规范化：排序键后 JSON 序列化，用于重复检测的等价比较。

    模型可能以不同键序给出相同参数（{"q":"x","n":3} vs {"n":3,"q":"x"}），
    不规范化会把同一次搜索判成两次。
    """
    if not isinstance(args, dict):
        return str(args)
    return json.dumps(dict(sorted(args.items())), ensure_ascii=False, sort_keys=True)


class RoundGuard:
    """单轮工具调用的护栏状态。每条聊天路径在一次用户请求中创建一个实例。

    线程安全：护栏在同步生成器/同步循环里调用，但 cancel_event 那条路上
    生产线程与主线程可能同时读计数器，加一把锁防竞态。
    """

    def __init__(self, max_tool_rounds: int = MAX_TOOL_ROUNDS,
                 max_tokens: int = MAX_ROUND_TOKENS,
                 duplicate_threshold: int = DUPLICATE_THRESHOLD,
                 fail_threshold: int = CONSECUTIVE_FAIL_THRESHOLD,
                 max_output_chars: int = MAX_ROUND_OUTPUT_CHARS):
        self.max_tool_rounds = max_tool_rounds
        self.max_tokens = max_tokens
        self.duplicate_threshold = duplicate_threshold
        self.fail_threshold = fail_threshold
        self.max_output_chars = max_output_chars

        self._lock = threading.Lock()
        # (tool_name, normalized_args) → 出现次数
        self._call_counts: Dict[Tuple[str, str], int] = {}
        # tool_name → 已熔断
        self._fused_tools: Dict[str, str] = {}  # tool_name → 熔断原因
        # tool_name → 连续失败次数
        self._consecutive_fails: Dict[str, int] = {}
        # 总工具轮次
        self._tool_rounds = 0
        # 总 token（估算）
        self._total_tokens = 0
        # R2：单轮累计工具输出字符数
        self._output_chars = 0
        # 护栏事件记录（挂账本 guard_events）
        self.events: List[Dict] = []

    def check(self, tool_name: str, args: dict) -> Optional[str]:
        """执行前检查。返回 None = 放行；返回字符串 = 熔断原因（给模型看的文案）。

        检查顺序：成本上限 → 重复检测 → 失败退避。任一条触发即熔断该工具。
        """
        norm = _normalize_args(args)
        key = (tool_name, norm)

        with self._lock:
            # 已被熔断的工具直接拒绝
            if tool_name in self._fused_tools:
                return self._fused_tools[tool_name]

            # 成本上限：工具轮次
            if self._tool_rounds >= self.max_tool_rounds:
                reason = (f"本轮工具调用已达上限（{self.max_tool_rounds} 轮），"
                          "请基于已有结果直接回答用户，不要再调用工具。")
                self._fused_tools[tool_name] = reason
                self.events.append({"type": "cost_cap_rounds",
                                    "tool": tool_name,
                                    "rounds": self._tool_rounds})
                return reason

            # 重复检测
            count = self._call_counts.get(key, 0) + 1
            self._call_counts[key] = count
            if count >= self.duplicate_threshold:
                reason = (f"已拦截重复调用：{tool_name} 用相同参数已调用 {count} 次，"
                          "请换一种方式或如实告诉用户当前结果。")
                self._fused_tools[tool_name] = reason
                self.events.append({"type": "duplicate", "tool": tool_name,
                                    "args": norm, "count": count})
                return reason

            return None

    def record_outcome(self, tool_name: str, ok: bool,
                       estimated_tokens: int = 0) -> None:
        """记录一次工具调用的结果（成功/失败 + 估算 token）。

        失败退避的计数在这里更新：成功时清零，失败时累加。
        可用性探测已剔除的工具不进熔断计数（它们根本不会被调用到这里）。
        """
        with self._lock:
            self._tool_rounds += 1
            self._total_tokens += estimated_tokens

            if ok:
                self._consecutive_fails[tool_name] = 0
            else:
                fails = self._consecutive_fails.get(tool_name, 0) + 1
                self._consecutive_fails[tool_name] = fails
                if fails >= self.fail_threshold:
                    reason = (f"{tool_name} 连续失败 {fails} 次，本轮不再重试；"
                              "请基于已有信息回答，或告诉用户该工具暂时不可用。")
                    self._fused_tools[tool_name] = reason
                    self.events.append({"type": "fail_backoff", "tool": tool_name,
                                        "fails": fails})

    def record_tokens(self, tokens: int) -> None:
        """追加 token 计数（来自上游 usage）。超限时熔断所有工具。"""
        with self._lock:
            self._total_tokens += tokens
            if self._total_tokens >= self.max_tokens:
                reason = (f"本轮 token 已达上限（约 {self.max_tokens}），"
                          "请基于已有结果直接回答用户。")
                # 熔断所有工具（用通配符标记）
                self._fused_tools["__all__"] = reason
                self.events.append({"type": "cost_cap_tokens",
                                    "tokens": self._total_tokens})

    def is_all_fused(self) -> bool:
        """是否所有工具都被熔断（token 上限触发）。"""
        with self._lock:
            return "__all__" in self._fused_tools

    @property
    def tool_rounds(self) -> int:
        with self._lock:
            return self._tool_rounds

    @property
    def guard_events(self) -> List[Dict]:
        with self._lock:
            return list(self.events)

    def clip_output(self, text: str, already_truncated: bool = False) -> Tuple[str, bool]:
        """R2 工具输出预算闸：单轮累计字符超限时截断。

        返回 (截断后的文本, 是否被本层截断)。
        与执行器的 truncated 标记合并：如果执行器已经截过（already_truncated=True），
        本层不再重复标记，避免双重计费。

        超限行为是截断并告知（在文本里追加说明），不是静默丢或 502。
        """
        with self._lock:
            remaining = self.max_output_chars - self._output_chars
            if remaining <= 0:
                # 已经超了，整段截成一句说明
                self.events.append({"type": "output_budget_exhausted",
                                    "chars": self._output_chars})
                return ("〔本轮工具输出预算已用尽，请基于已有结果回答用户〕", True)

            if len(text) <= remaining:
                # 放得下，全收
                self._output_chars += len(text)
                return (text, False)

            # 超了：截取剩余预算，追加说明
            truncated_text = text[:max(remaining - 60, 0)]
            omission = (f"…〔本轮工具输出预算剩余 {remaining} 字，"
                        f"已截断 {len(text) - remaining} 字〕…")
            result = truncated_text + omission if truncated_text else omission
            self._output_chars = self.max_output_chars
            self.events.append({"type": "output_truncated",
                                "original": len(text), "kept": len(truncated_text)})
            return (result, not already_truncated)

    @property
    def output_chars(self) -> int:
        with self._lock:
            return self._output_chars
