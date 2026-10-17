import json
import threading
import time
from collections import defaultdict
from typing import NamedTuple, Optional

from app.tools.registry import tools_registry
from app.tools.response import ToolResponse, ToolSuspended

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

    ⭐ v0.35 A3 两格（提案 §四"可挂起 → 等外部输入 → 恢复"的落点）：
    - `suspended`：这一次调用**没跑完也没失败**，它停在断点等用户给一个答案。
      读法纪律：`ok=False` 只说明"没产出结果"，**不**说明"失败了"；想知道是哪一种，
      只看 `suspended`。把挂起读成失败，代价是把"问用户要验证码"记进失败退避
      （`guardrails.py:177-180`），两次就把这个工具在本轮熔断，用户永远续不上。
    - `suspension`：续跑要的整包状态，只在 suspended 时非空：
      `{"tool_name", "arguments", "prompt", "resume_state", "can_resume"}`。
      `arguments` 是**服务端这一侧**的调用参数（不是模型事后能改的那一份），续跑时
      照原样交给同一个工具；`resume_state` 是工具自己记的断点；`can_resume` 是
      "这一挂到底接不接得回去"的唯一答案（判据见 `_settle_suspension`）。
      为什么是一包 dict 而不是三个字段：状态本身是一张自由 dict（同 A1 那份
      `Task.resume_cursor` 的理由——工具每多带一格断点信息，不该回来改本协议的骨架）。

    ⭐ 工单 #37 `tool_ran`：这一次调用**有没有真的进到工具函数体**（`func(**kwargs)` 到底
    被调用没有）。它是"这个答案递没递进工具"这件事的**世界事实**，由 `_dispatch` 里那一处
    唯一赋值点给出——不是从 `ok`/`success` 猜的（被审批闸门、频控、缺身份挡下的续跑，
    `ok` 同样是 False，却压根没进函数体）。编排器据此决定"要不要把这枚断点标成已消费"：
    只有真的把答案交给了工具（副作用可能已落地）才许标，被闸门挡下的那种必须留着能再递。
    默认 False = 没走到函数体。除 `orchestrator.py:386` 那一处消费点外，既有读者（`pipeline.py`、
    `streaming.py`、`react_agent.py`、`task_agent.py`、`main.py:974`）都不读这一格，与它们无关。

    两个新格都带默认值：`tool_ran` 加在 `suspension` 之前而不是最末尾，是因为 `suspended`
    与 `suspension` 必须保持是 `_fields` 的最后两格（那条形状锁见
    `tests/test_v0350_a3_suspendable_tool_protocol.py`）；前五格（text/ok/artifacts/
    truncated/elapsed_ms）的位置与含义一字未动，既有构造点（本文件与 `streaming.py:549/:554`）
    全是关键字写法。
    """
    text: str
    ok: bool
    artifacts: Optional[dict]
    truncated: bool
    elapsed_ms: int
    tool_ran: bool = False
    suspended: bool = False
    suspension: Optional[dict] = None


def execute_tool(tool_name: str, arguments: dict, user_id: str = None,
                 granted_ticket=None, resume: dict = None) -> str:
    """执行一条工具，返回**已在上下文预算内**的结果文本。

    这是全站对外稳定的文本契约（签名与返回类型 str 一律不动）：真正干活的是
    execute_tool_detailed，这里只投影它的 `.text`，于是"唯一实现"只有一份，两侧不可能
    各长出一套截断/频控逻辑。⭐ 工单 #76a 起三条消费路都改吃 detailed 那一格，这里留给只要文本的调用方。

    `user_id` 由调用方从**凭据**里算出来传进来（两条聊天路径都是这么做的），它不进
    模型可见的参数表：标了 needs_user 的工具，执行器用服务端那份覆盖掉模型可能传上
    来的同名参数。方向反过来（让模型说了算）就是"读谁的日程"由模型编。

    `granted_ticket` 是审批放行证明，只在用户**真的点了允许**之后由confirm 端点
    传进来（一张服务端 ApprovalTicket）。默认 None = 未获批准。这道闸门必须挂在
    执行器而不是调用方：原先判据只写在两条聊天路径的调用侧，于是 react_agent 与
    task_agent 绕开它直接拿到无审批的代码执行。见_dispatch 里的注释。

    `resume`（⭐ v0.35 A3）是"从断点接上"的那一包：`{"state": 工具上次交出的
    resume_state, "answer": 用户给的那句话}`，取自上一次结果里的
    `ToolOutcome.suspension`。默认 None = 一次全新的调用，行为与加这一格之前逐字相同。
    它**不**新增"调用成功/失败"的含义，只是把答案递回给同一个工具继续往下跑；
    递不递得进去由 registry 的 `suspends` 声明决定（`_resume_refusal`），判据不合
    就一句错误文本，绝不静默从头再跑一遍。

    ⚠️ 文本投影读不出第三态的完整形状：挂起时这里返回的是 `⏸ 需要用户输入：…`
    那句话（`✓`/`✗` 之外的第三个前缀），而 `can_resume` 这类结构化信息只在
    `execute_tool_detailed` 里有。⭐ 工单 #76b 之后 `backend/app/**` 已无"只拿文本"的读数点（各处
    都取 detailed，文本只是它投影出的那一格），这层投影今天只剩测试在吃：它至少能分清"这不是失败"。
    """
    return execute_tool_detailed(tool_name, arguments, user_id=user_id,
                                 granted_ticket=granted_ticket, resume=resume).text


def execute_tool_detailed(tool_name: str, arguments: dict,
                          user_id: str = None, granted_ticket=None,
                          resume: dict = None) -> ToolOutcome:
    """执行一条工具，返回文本 + 结构化元数据。唯一实现，两条路共用。

    流式那条要拿 artifacts/truncated 发帧，非流式与 ReAct 只要 `.text`——它们各自
    从这一个函数取自己要的那几项，绝不允许出现"另一份执行器"。计时贴着真正执行的
    `_dispatch` 与出口裁剪，量到的是这一次调用的人为感知耗时（不含上游排队）。
    """
    start = time.monotonic()
    response = _dispatch(tool_name, arguments, user_id, granted_ticket, resume)
    raw = response.to_string()
    clipped = clip_for_model(raw)
    truncated = len(clipped) != len(raw)
    elapsed_ms = int((time.monotonic() - start) * 1000)
    if truncated:
        print(f"[Tools] {tool_name} 输出 {len(raw)} 字，超出 {TOOL_OUTPUT_BUDGET} 字预算，"
              f"已截断后再发给模型")
    suspended = bool(getattr(response, "suspended", False))
    suspension = None
    if suspended:
        # 断点整包在这里拼，只有这一处。arguments 取**调用方传进来的那一份**（服务端
        # 已经用它算过 kwargs，模型事后改不动 needs_user 那格），resume_state 取工具
        # 自己交出来的那一份。
        suspension = {
            "tool_name": tool_name,
            "arguments": dict(arguments) if isinstance(arguments, dict) else {},
            "prompt": str(getattr(response, "prompt", "") or ""),
            "resume_state": getattr(response, "resume_state", None),
            "can_resume": _can_resume(tool_name, getattr(response, "resume_state", None)),
        }
    # 工单 #37：这个答案有没有真的递进工具——读 `_dispatch` 在唯一赋值点贴上的那格，
    # 不是从 ok/success 反推（见 ToolOutcome.tool_ran 的文档）。默认 False = 没进函数体。
    tool_ran = bool(getattr(response, "tool_ran", False))
    return ToolOutcome(text=clipped, ok=bool(response.success),
                       artifacts=getattr(response, "artifacts", None),
                       truncated=truncated, elapsed_ms=elapsed_ms,
                       tool_ran=tool_ran,
                       suspended=suspended, suspension=suspension)


def _state_carries(state) -> bool:
    """这份断点带不带得过一次重启。

    判据只有一条：**JSON 往返得回来的 dict**。为什么在这里判而不是等落盘时炸：
    断点随后要进 `data/tasks.json`（A1 那份 `resume_cursor` 是自由 dict，正是为它留的
    位置），而落盘那一侧坏掉形状的代价是"任务挂在那里，谁也接不回去"。现在判，
    消费方拿到的 `can_resume` 就是一句可以照办的事实。

    `None` 不算可续：一个连"停在哪"都没交出来的挂起，续跑只能从头再跑一遍——
    从头再跑就是**再花一次钱**，而这一版的全部意义是不花那次钱。空 dict 算：
    那是工具在明说"我这儿不需要带任何断点"（例如只差一个"要不要继续"）。
    这里只做纯 CPU 的序列化试探，不碰磁盘、不起线程。
    """
    if not isinstance(state, dict):
        return False
    try:
        return json.loads(json.dumps(state)) == state
    except (TypeError, ValueError):
        return False


def _can_resume(tool_name: str, state) -> bool:
    """这一挂接不接得回去——**全场唯一一份**判据。两条都要成立：
    ① registry 声明了这条工具可挂起（`suspends=True`）：没声明就没有把答案递回工具
       的那条通道，`_resume_refusal` 会拒掉任何续跑，此时许诺"可续"是撒谎；
    ② 断点形状带得过重启（`_state_carries`）。
    """
    info = tools_registry.get(tool_name) or {}
    return bool(info.get("suspends")) and _state_carries(state)


def _settle_suspension(tool_name: str, response: ToolResponse) -> ToolResponse:
    """把"工具说它在等人"这句话验一遍，不合格就地折成失败——**唯一的验证点**。

    为什么验在执行器而不是调用侧：与审批闸门同一个道理（见 `_dispatch` 里 v0.31 那段）。
    挂在调用方等于赌"所有未来的调用方都记得判 prompt 非空"，而本仓已经为这个赌输过两次。

    三种形状算"判不出是在等还是已经跑完"，一律 fail-closed 成失败（绝不自动继续）：
    - `suspended=True` 同时 `success=True`：自相矛盾。把它当挂起，用户永远等不到那一步
      的结果；把它当成功，模型会替用户把后面编完。两边都不能选，所以明说失败。
    - prompt 是空的：挂起的意思就是"要问人"，问不出话就等于没人能把它接回去。
    - resume_state 不是 dict：接不回去的挂起仍然可以**问**（这一跑确实停了），所以
      不折成失败，只由 `_can_resume` 把 `can_resume` 说成 False，交给消费方决定。
    """
    if not getattr(response, "suspended", False):
        return response
    prompt = str(getattr(response, "prompt", "") or "").strip()
    if response.success:
        return ToolResponse(False,
                            error=(f"{tool_name} 同时声明了「已完成」与「等用户输入」，"
                                   "协议无法判断这一次到底跑完没有。按失败处理：既不自动"
                                   "继续，也不替你假设结果。"),
                            hint="如实告诉用户这一步没做成，需要人工确认后再来一次")
    if not prompt:
        return ToolResponse(False,
                            error=(f"{tool_name} 声明要挂起等用户，却没给出要问的那句话。"
                                   "没人能接上这一步，所以不把它当成还在等。"),
                            hint="如实告诉用户这一步没做成")
    response.prompt = prompt
    return response


def _resume_refusal(tool_name: str, info: dict, resume) -> Optional[str]:
    """这次"从断点接上"能不能成立。回 None = 可以，回字符串 = 拒它的理由（给模型看）。

    四条判据全在 fail-closed 那一侧，方向是一致的：**判不出就不往下跑**，
    而不是"先跑了再说"——跑一次真工具是要花钱的，而一次没有答案的续跑等于
    让工具自己替用户编一个答案。
    """
    if not isinstance(resume, dict):
        return (f"续跑 {tool_name} 需要一包状态（{{state, answer}}，取自上一次结果的 "
                "suspension），现在给的不是这个形状。不要改成重新调用一次这个工具——"
                "重新调用等于把已经跑过的步骤再烧一遍。")
    if not info.get("suspends"):
        return (f"{tool_name} 没有声明可挂起（注册表 suspends），执行器不往里递断点。"
                "请把它上一次交出的那句问题原样问用户，再用一次全新的调用继续。")
    state = resume.get("state")
    if not isinstance(state, dict):
        return (f"续跑 {tool_name} 缺断点：resume.state 必须是上一次 suspension 里那份 "
                "resume_state，收到的是空或别的形状。宁可不动，也不从头再跑一遍。")
    answer = resume.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return (f"续跑 {tool_name} 必须带上用户给的答案，且答案不能是空的。"
                "没有答案就继续 = 替用户编一个，这里绝不那么做。")
    return None


def _dispatch(tool_name: str, arguments: dict, user_id: str = None,
              granted_ticket=None, resume: dict = None) -> ToolResponse:
    """把一次调用收敛成一个 ToolResponse（含 success/artifacts），不再吐裸字符串。

    所有分支都回 ToolResponse：未知工具、参数非对象、缺身份、schema 外参数、
    **未获审批**、频控闸门、正常执行、**挂起等用户**、执行炸了——每个出口都带正确的
    success 位与（搜索这类工具的）artifacts，好让上层既能原样拿到给模型的文本，
    又能拿到结构化元数据去发过程帧。

    ⭐ 工单 #37：每一个出口都另外贴一格 `tool_ran`——**`func(**kwargs)` 到底被调用没有**。
    这是"答案递没递进工具"的唯一真相来源，写在派发处而不是让上层从 `ok` 猜：`_settle_suspension`
    与兜底 `except Exception` 把"被审批闸门/频控挡下（压根没跑）"与"跑了一半才坏（副作用可能
    已落地）"都折成同一句 `ok=False` 的话，从外面分不开。判"要不要把断点标成已消费"必须由这
    一处的世界事实说了算——见 `ToolOutcome.tool_ran` 与 `orchestrator` 里那一处消费点。
    """
    def _ret(resp: ToolResponse, *, ran: bool) -> ToolResponse:
        resp.tool_ran = ran          # 唯一赋值点：ran=True 只在真调用 func 的那一段给
        return resp

    if tool_name not in tools_registry:
        return _ret(ToolResponse(False, error=f"未知工具: {tool_name}",
                                 hint="使用 help 工具查看可用工具列表"), ran=False)
    info = tools_registry[tool_name]
    func = info["function"]
    # 模型偶尔会给出数组/字符串当参数。原写法 dict(arguments) 在这一支会直接炸
    # 出 TypeError（还在 try 之外）；这里按同样的口径给出结构化的"参数错误"。
    if not isinstance(arguments, dict):
        return _ret(ToolResponse(False, error="参数错误: arguments 必须是对象",
                                 hint="请检查工具参数是否正确"), ran=False)
    # ⭐⭐ 工单 #37 选项 b：既声明可挂起（suspends）又声明需审批（requires_approval）的工具，
    # 现在**不被支持**，无论有没有票据，都在派发处响亮拒绝。理由（写死在这里，别让下一个
    # 接手的人再推一遍）：审批票据接不过一次挂起——
    #   ① 任务面（TaskAgent/Orchestrator）从不带 granted_ticket（见 task_agent.py:162 那段），
    #      所以第一次调用就会被下面那道审批闸门挡在函数体之前，工具压根挂不起来，永远拿不到
    #      断点；② 就算退一步、由带票据的 confirm 那条路让它挂起，续跑时那张票要么已被消费
    #      （一次性，A2 判据钉着）、要么明文 id 不落盘（A2 只存 sha256 摘要，服务端事后取不回
    #      原文），带不进下一次 `_dispatch`，于是必然停在"被拒 → 又被烧"。两个下场都指向同一
    #      句实话：这一组合今天没法工作。宁可在这里明说一句"不支持、请二选一"，也不让 A4 把一
    #      个"批准的是 A、执行的可能是 B、断点还接不回去"的死局悄悄上线。真要解锁这一组合，
    #      得先让审批票据能跨请求跟着断点走（动 approval.py 与游标形状），那是独立的一票。
    # 判据放在执行器而不是调用侧（与下面 v0.31 那道闸门同一条纪律）。
    if info.get("suspends") and info.get("requires_approval"):
        return _ret(ToolResponse(
            False,
            error=(f"{tool_name} 同时声明了可挂起（suspends）与需审批（requires_approval），"
                   "工单 #37 起这一组合不被支持：审批票据接不过一次挂起，"
                   "结果要么是先被审批闸门挡下（工具压根挂不起来），要么是挂起后续跑时票据"
                   "已消费或取不回而必然接不回去。请二选一——要么去掉 suspends（一次批准一次"
                   "执行），要么去掉 requires_approval（改成能带断点续跑的工具），要么先把审批"
                   "票据做成能跨请求随断点走（那是另一票）。"),
            hint="这条工具既挂起又需审批，暂不支持"), ran=False)
    # v0.31 审批闸门，挂在这里而不是调用侧。
    #
    # 为什么必须在执行器：审批是"这件事有副作用、执行前要用户点头"，那就必须由
    # **真正执行的那一层**来校验。只写在 pipeline/streaming 的调用侧，等于假设
    # 所有未来的调用方都记得检查——而 react_agent 与 task_agent 已经证明这个假设
    # 不成立，它们直接 execute_tool 就拿到了无审批的代码执行。
    #
    # 为什么两条聊天路径不会双弹卡片：它们在调用侧已经拦下并continue 了，根本走不到
    # 这里；真正走到这里又没有票据的，只可能是绕开调用侧的那两条路。
    if info.get("requires_approval"):
        from app.core import approval
        if not approval.is_granted(granted_ticket, tool_name, user_id):
            return _ret(ToolResponse(
                False,
                error=(f"{tool_name} 需要用户确认后才能执行，当前这次调用没有获得批准。"
                       "请改为直接回答用户，或提示用户在确认卡片上点允许。"),
                hint="这条工具需要审批"), ran=False)
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
            return _ret(ToolResponse(False, error="缺少身份：这条工具只查得到某个具体人的数据",
                                     hint="请从已登录的会话里调用"), ran=False)
        kwargs["user_id"] = user_id
    # ⭐ v0.35 A3：续跑（"从断点接上"）的判据放在频控**之前**。
    # 理由与下面那句"被闸门挡下的调用不该已经拉起过容器"同源：一次形状不对的续跑
    # 根本没资格消耗这个人的频控额度，更不该已经把容器拉起来了。
    # ⭐ 工单 #37 之后这一段的审批那一半：上面那道"既挂起又需审批"的派发拒绝已经替
    # 它把最坏的情形（A4 那个必然被拒又被烧的组合）关掉了；纯需审批（不挂起）的工具
    # 仍要在续跑时重新过一遍 granted_ticket（不放宽），而**这些被闸门挡在函数体之前的
    # 出口现在都回 `tool_ran=False`**——编排器因此不再把"被拒"误记成"已消费"，同一枚
    # 答案在闸门放行后仍接得回去（见 orchestrator 消费点那一份表格）。
    if resume is not None:
        refusal = _resume_refusal(tool_name, info, resume)
        if refusal:
            return _ret(ToolResponse(False, error=refusal,
                                     hint="按上面那句的理由办，不要重跑已跑过的步骤"), ran=False)
        kwargs["resume"] = {"state": resume["state"], "answer": resume["answer"]}
    # 频控放在真正执行之前、参数清洗之后：被闸门挡下的调用不该已经拉起过容器。
    if blocked := _freq_gate(tool_name, user_id):
        return _ret(ToolResponse(False, error=blocked,
                                 hint="等待后用一次调用完成剩余步骤"), ran=False)
    try:
        # ⭐ 工单 #37：下面这一段是**唯一**给 `tool_ran=True` 的地方——一旦进到 `func`，
        # 答案就已经交到工具手里了（无论是正常返回、还是跑了一半才抛，副作用都可能已经落地）。
        result = func(**kwargs)
        # 如果函数本身返回 ToolResponse，则保持（连同它的 artifacts），否则包装
        if isinstance(result, ToolResponse):
            return _ret(_settle_suspension(tool_name, result), ran=True)
        return _ret(ToolResponse(True, data=result), ran=True)
    except ToolSuspended as s:
        # ⭐ 第三态的入口：必须排在 `except Exception` **之前**。
        # 顺序就是这一版的生死线——Exception 在前，挂起会被兜底那句折成
        # `✗ 错误: <prompt>`，于是"问用户要验证码"在护栏里记成一次失败，
        # 两次就把这个工具在本轮熔断（负向锁见 tests/test_v0350_a3_*.py）。
        # 走到这里 = 函数体已经执行（它自己 raise 出来的）→ ran=True。
        return _ret(_settle_suspension(
            tool_name, ToolResponse.suspend(s.prompt, s.state, s.hint)), ran=True)
    except TypeError as e:
        # `func(**kwargs)` 已经**被调用**（Python 已把控制权交给它；TypeError 既可能是
        # 形参不匹配也可能是函数体内部抛的），从外面分不开 → 保守侧记 ran=True，
        # 与"跑了一半才坏"同一处置：宁可让人多点一次"重来"，也不能替他把可能已落地的
        # 副作用再落一遍（工单 #33 那道门守的就是这个）。
        return _ret(ToolResponse(False, error=f"参数错误: {e}",
                                 hint="请检查工具参数是否正确"), ran=True)
    except Exception as e:
        return _ret(ToolResponse(False, error=str(e), hint="重试或使用其他方法"), ran=True)