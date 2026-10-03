import threading
import time
from collections import defaultdict
from typing import NamedTuple, Optional

from app.tools.registry import tools_registry
from app.tools.response import ToolResponse

# 按调用者频控的工具闸门。
#
# 为什么必须有：工具的输出会喂回模型，而模型可以被外部文本影响（搜索结果里的网页
# 标题就是别人写的字）。"外部文本 → 模型 → execute_code 连拉容器"是这条注入链的
# 变现方式：每次容器执行都烧真钱，而 ReAct 一轮能自己决定调多少次。出口预算
# （TOOL_OUTPUT_BUDGET）管不了次数，这里补上。
#
# 口径：滑动窗口内计数，**尝试即计数**——失败的代码同样拉起过容器，不计数就等于
# 给重试型攻击留免费额度。键用服务端算出的 user_id（与 needs_user 注入同源），
# 模型碰不到它；无凭据调用归 "anon" 同一个桶，不给匿名调用开平行额度。
# 窗口 10 分钟 / 上限 10 次：正常用户一轮对话用不满，脚本循环与注入驱动的密集
# 调用必然撞上；撞上的回复带"还需等约 N 秒"，让模型有明确出口而不是盲目重试。
_FREQ_LIMITS = {"execute_code": (600.0, 10)}
_FREQ_LEDGER = defaultdict(list)
_FREQ_LEDGER_MAX_KEYS = 4096          # 键数上限：超了顺手清过期键，防无界增长
_FREQ_LOCK = threading.Lock()         # 计数与追加必须同锁：否则并发两条各见 9 次、各放行


def _freq_gate(tool_name: str, user_id: str):
    """放行回 None；超预算回一句给模型看的话（不再执行）。"""
    limit = _FREQ_LIMITS.get(tool_name)
    if limit is None:
        return None
    window, cap = limit
    key = (tool_name, (user_id or "").strip() or "anon")
    now = time.monotonic()
    with _FREQ_LOCK:
        hits = [t for t in _FREQ_LEDGER[key] if now - t < window]
        if len(hits) >= cap:
            retry = int(window - (now - hits[0])) + 1
            _FREQ_LEDGER[key] = hits
            return (f"调用过于频繁：{tool_name} 每 {int(window)} 秒最多 {cap} 次，"
                    f"还需等待约 {retry} 秒。不要把同一件事拆成多次调用，"
                    "可以合并步骤稍后再试，或者如实告诉用户现在做不了。")
        hits.append(now)
        _FREQ_LEDGER[key] = hits
        if len(_FREQ_LEDGER) > _FREQ_LEDGER_MAX_KEYS:
            stale = [k for k, v in _FREQ_LEDGER.items() if not v or now - v[-1] >= window]
            for k in stale:
                del _FREQ_LEDGER[k]
    return None

# 一次工具调用允许发给模型多少字。
#
# 为什么要裁：execute_code 打一万行日志、web_search 回一整页摘要，这些都是**本轮已经
# 付过钱**的内容——超限部分不是"省下来了"，是白付。更实在的代价在后面：上下文窗口是
# 满的，塞进来的工具输出会把同一轮里更早的对话挤出去，用户看到的症状是"它忘了我说过
# 的话"。所以这道闸放在执行器出口这一个地方：两条聊天路径加 ReAct 那条都从这儿过，
# 以后新加的调用方不可能忘（本项目已经因"只有一路径带了 tools"吃过一次产品级缺口）。
TOOL_OUTPUT_BUDGET = 4000


def _omission(omitted: int, total: int, budget: int) -> str:
    return (f"…〔中段已省略 {omitted} 字：工具输出共 {total} 字，超出给模型的 "
            f"{budget} 字预算，这里只保留开头与结尾〕…")


def clip_for_model(text: str, budget: int = TOOL_OUTPUT_BUDGET) -> str:
    """按预算截断，并在文本里写明截了多少。

    静默截断比截断本身更糟：模型会以为它看到的是全貌，然后理直气壮地基于半截日志
    下结论。所以省略号里带字数，让它有机会说"输出被截断了"。
    头 2/3 尾 1/3 —— 说明与参数在开头，而报错与结果的关键行几乎总在结尾。
    """
    total = len(text)
    if total <= budget:
        return text
    # 先按"最坏长度的那句省略说明"（两个数字都按 total 的位数）预留位置，再拿实际
    # 长度拼一次，于是 头+说明+尾 ≤ 预算 是可证的，不需要事后修剪。
    room = max(budget - len(_omission(total, total, budget)), 0)
    tail = room // 3
    head = room - tail
    return text[:head] + _omission(total - room, total, budget) + (text[total - tail:] if tail else "")


class ToolOutcome(NamedTuple):
    """一次工具调用的完整结果：给模型的文本 + 给人看的结构化元数据。

    为什么两个都要：给模型的 `text` 是预算裁剪后那段带 ✓/✗ 前缀的话（与非流式那条
    pipeline 拿到的逐字节一致），而 ok/artifacts/truncated/elapsed_ms 只服务于"过程
    面板"——用户要看到的是"这次算成了没、花了多久、输出有没有被截、搜到哪些网页"，
    这些信息不该、也没被塞进模型的上下文。
    """
    text: str
    ok: bool
    artifacts: Optional[dict]
    truncated: bool
    elapsed_ms: int


def execute_tool(tool_name: str, arguments: dict, user_id: str = None) -> str:
    """执行一条工具，返回**已在上下文预算内**的结果文本。

    这是全站对外稳定的文本契约：两条聊天路径与 ReAct 那条都靠它，签名与返回类型
    （str）一律不动。真正干活的是 execute_tool_detailed——这里只是把它结果里的
    `.text` 投影出来，于是"唯一实现"只有一份，两侧不可能各长出一套截断/频控逻辑。

    `user_id` 由调用方从**凭据**里算出来传进来（两条聊天路径都是这么做的），它不进
    模型可见的参数表：标了 needs_user 的工具，执行器用服务端那份覆盖掉模型可能传上
    来的同名参数。方向反过来（让模型说了算）就是"读谁的日程"由模型编。
    """
    return execute_tool_detailed(tool_name, arguments, user_id=user_id).text


def execute_tool_detailed(tool_name: str, arguments: dict,
                          user_id: str = None) -> ToolOutcome:
    """执行一条工具，返回文本 + 结构化元数据。唯一实现，两条路共用。

    流式那条要拿 artifacts/truncated 发帧，非流式与 ReAct 只要 `.text`——它们各自
    从这一个函数取自己要的那几项，绝不允许出现"另一份执行器"。计时贴着真正执行的
    `_dispatch` 与出口裁剪，量到的是这一次调用的人为感知耗时（不含上游排队）。
    """
    start = time.monotonic()
    response = _dispatch(tool_name, arguments, user_id)
    raw = response.to_string()
    clipped = clip_for_model(raw)
    truncated = len(clipped) != len(raw)
    elapsed_ms = int((time.monotonic() - start) * 1000)
    if truncated:
        print(f"[Tools] {tool_name} 输出 {len(raw)} 字，超出 {TOOL_OUTPUT_BUDGET} 字预算，"
              f"已截断后再发给模型")
    return ToolOutcome(text=clipped, ok=bool(response.success),
                       artifacts=getattr(response, "artifacts", None),
                       truncated=truncated, elapsed_ms=elapsed_ms)


def _dispatch(tool_name: str, arguments: dict, user_id: str = None) -> ToolResponse:
    """把一次调用收敛成一个 ToolResponse（含 success/artifacts），不再吐裸字符串。

    所有分支都回 ToolResponse：未知工具、参数非对象、缺身份、schema 外参数、频控
    闸门、正常执行、执行炸了——每个出口都带正确的 success 位与（搜索这类工具的）
    artifacts，好让上层既能原样拿到给模型的文本，又能拿到结构化元数据去发过程帧。
    """
    if tool_name not in tools_registry:
        return ToolResponse(False, error=f"未知工具: {tool_name}", hint="使用 help 工具查看可用工具列表")
    info = tools_registry[tool_name]
    func = info["function"]
    # 模型偶尔会给出数组/字符串当参数。原写法 dict(arguments) 在这一支会直接炸
    # 出 TypeError（还在 try 之外）；这里按同样的口径给出结构化的"参数错误"。
    if not isinstance(arguments, dict):
        return ToolResponse(False, error="参数错误: arguments 必须是对象",
                            hint="请检查工具参数是否正确")
    # 只放行 schema 里声明过的参数。函数签名上的形参（比如 execute_code 的
    # max_retries）模型一律不许给——否则传个 max_retries=100 就能串行拉起
    # 上百个容器。schema 是模型能碰的唯一契约。
    schema_props = (info.get("parameters") or {}).get("properties") or {}
    kwargs = {k: v for k, v in arguments.items() if k in schema_props}
    dropped = [k for k in arguments if k not in schema_props]
    if dropped:
        print(f"[Tools] {tool_name} 收到 schema 外的参数 {dropped}，已丢弃")
    if info.get("needs_user"):
        if not (user_id or "").strip():
            return ToolResponse(False, error="缺少身份：这条工具只查得到某个具体人的数据",
                                hint="请从已登录的会话里调用")
        kwargs["user_id"] = user_id
    # 频控放在真正执行之前、参数清洗之后：被闸门挡下的调用不该已经拉起过容器。
    if blocked := _freq_gate(tool_name, user_id):
        return ToolResponse(False, error=blocked,
                            hint="等待后用一次调用完成剩余步骤")
    try:
        result = func(**kwargs)
        # 如果函数本身返回 ToolResponse，则保持（连同它的 artifacts），否则包装
        if isinstance(result, ToolResponse):
            return result
        return ToolResponse(True, data=result)
    except TypeError as e:
        return ToolResponse(False, error=f"参数错误: {e}", hint="请检查工具参数是否正确")
    except Exception as e:
        return ToolResponse(False, error=str(e), hint="重试或使用其他方法")