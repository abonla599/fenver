"""流式输出模块 - 基于 Server-Sent Events (SSE)。

模型配置统一来自 app.core.providers，不再自带一份模型清单——此前
streaming 与 llm_client 各有一份 MODEL_CONFIG，改一处不生效；且未知模型会
静默回落到 deepseek-chat，造成"界面显示 GPT-4o、实际是 DeepSeek 在答"。

v0.29.0 起这里是"过程留痕"的引擎：thinking（模型在想）/tool_call（要去调哪个工具）/
tool_result（这一次成没成、多久、截没截）/search（搜到哪些网页）这些帧在同一条同步
生成器里产出。但对外仍然只有一个 `stream_chat`——把上面这些都吃掉的"只给正文文本"
薄过滤器。为什么这么分（而不是让端点直接抽干帧流）见下面 ambient sink 那段注释。
"""
import json
import threading
import time
from typing import Generator, List, Dict, Any, Optional

from app.core.providers import store, build_client
from app.core import usage   # 账本：两条聊天路径共用一个口径，别各记一套
from app.core import stream_events as se
from app.tools.executor import execute_tool, execute_tool_detailed, ToolOutcome


class StreamCancelled(Exception):
    """用户按了"停止"（cancel 端点翻标志并尽力关连接）：生成线在此收口。

    与"读者断开"是两回事——v0.25 R3 起断线不再打断生产（续播靠这个前提），
    所以这个异常只有一个成因：显式取消。调用方（main.py 的生产线程）据此发
    cancelled 终帧而不是 error，账照已消费的部分结清，下一轮 create() 不发生。
    """


def _close_quietly(stream) -> None:
    """尽力关闭上游流：close 可能不存在（测试假流）、可能已关、可能层里报错。
    取消方不该因为"关连接"这一下再炸出第二个异常——标志位才是主判据。"""
    try:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    except Exception:
        pass


# ---------- 过程留痕的环境接收器（ambient sink） ----------
# 为什么用线程局部，而不是把 on_frame/trace_out 一层层透传进 stream_chat：
#   1. conftest 与好几条不可改的用例（test_event_loop_not_blocked、test_env_context、
#      test_today_context、test_chat_throttle、test_isolation、test_stream_api）都把
#      `streaming.stream_chat` 整个换成签名固定的桩，且要求"打桩不许比真身更能收"
#      （test_stream_tools 那条签名锁）。于是 stream_chat 的对外形状一个字都不能加。
#   2. 而端点这一头要把 thinking/tool_call/search 帧逐块 append 进 run 缓冲、
#      还要在最后把步收拢成 trace 落盘——这些非内容帧被 stream_chat 这个文本过滤器
#      吃掉了，得有另一条路交出去。
# 解法：端点（与生产线程同线程）在抽干 stream_chat 前 begin_trace()，引擎把非内容帧
# 与步就地塞进这根线程的 sink；端点每收到一段正文就 drain_frames() 把攒下的帧补发，
# 流抽干后再 collect_steps() 拿全程留痕。stream_chat 仍是 stream_chat_events 的薄过滤器，
# 签名不变、桩不受影响；直连 stream_chat_events 的调用方（含本模块新测试）可改用显式
# trace_out。生产线程是每轮一条独立线程，线程局部天然按 run 隔离，不串味。
_trace_local = threading.local()


class _TraceSink:
    __slots__ = ("frames", "steps")

    def __init__(self):
        self.frames: List[Dict[str, Any]] = []
        self.steps: List[Dict[str, Any]] = []


def begin_trace() -> None:
    """在当前（生产）线程开一份过程留痕接收器；与 end_trace 成对使用。"""
    _trace_local.sink = _TraceSink()


def _sink() -> Optional[_TraceSink]:
    return getattr(_trace_local, "sink", None)


def drain_frames() -> List[Dict[str, Any]]:
    """取走自上次以来累积的非内容帧并清空。

    返回的是引擎产出的帧 dict（thinking/tool_call/tool_result/search），顺序即产出
    顺序。端点把它们连同自己发的 content 帧一起 append 进 run 缓冲；这里只负责
    "把过滤器吃不掉的那部分交出去"。
    """
    s = _sink()
    if s is None or not s.frames:
        return []
    out = s.frames
    s.frames = []
    return out


def collect_steps() -> List[Dict[str, Any]]:
    """取走全程过程步（未压缩）：端点拿它去 se.compact_trace 后落盘。"""
    s = _sink()
    return list(s.steps) if s is not None else []


def end_trace() -> None:
    """关闭并清空当前线程的接收器：无论正常、取消还是异常都必须在 finally 里调到。"""
    _trace_local.sink = None


def _tool_label(name: str, args: Any) -> str:
    """服务端算一句短的中文一行文案（收起态标题）。

    为什么在服务端算、不在两个客户端各排一遍：客户端各写一份"看到工具名拼什么话"
    迟早漂移，而这条文案是过程面板的标题，必须跨端一致。搜什么、算什么各给一句人话，
    其余工具退回"调用 <name>"。真正的裁剪（封顶 TOOL_LABEL_MAX 字）在 stream_events。
    """
    if isinstance(args, dict):
        if name == "web_search":
            q = str(args.get("query", "")).strip()
            if q:
                return f"搜索「{q}」"
        if name == "calculator":
            expr = str(args.get("expression", "")).strip()
            if expr:
                return f"计算 {expr}"
    return f"调用 {name}"


def stream_chat(
    model: str,
    messages: List[Dict[str, Any]],
    provider_id: str = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    tools: Optional[List[Dict]] = None,
    max_tool_turns: int = 5,
    user_id: str = None,
    cancel_event=None,
    on_upstream_start=None,
    before_round=None,
) -> Generator[str, None, None]:
    """流式生成 AI 回复的**文本**薄过滤器：只把正文 piece 透出去，其余帧吞掉。

    形状与改造前完全一致（同步生成器、逐块产出 str、参数不变），因此既有的
    `"".join(stream_chat(...))` 断言、conftest 的桩、以及所有以 stream_chat 为测试
    切面的端点用例照旧成立。真正干活的是 stream_chat_events——这里只是它的一个投影。

    非内容帧（thinking/tool_call/tool_result/search）与过程步不在这里透传：需要它们的
    调用方要么用 begin_trace()/drain_frames()/collect_steps() 这对环境接口（端点走的
    就是这条），要么直接抽干 stream_chat_events 并传 trace_out。
    """
    for ev in stream_chat_events(
        model, messages, provider_id=provider_id, temperature=temperature,
        max_tokens=max_tokens, tools=tools, max_tool_turns=max_tool_turns,
        user_id=user_id, cancel_event=cancel_event,
        on_upstream_start=on_upstream_start, before_round=before_round,
    ):
        if ev["type"] == se.FRAME_CONTENT:
            yield ev["text"]


def stream_chat_events(
    model: str,
    messages: List[Dict[str, Any]],
    provider_id: str = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    tools: Optional[List[Dict]] = None,
    max_tool_turns: int = 5,
    user_id: str = None,
    cancel_event=None,
    on_upstream_start=None,
    before_round=None,
    trace_out: Optional[List[Dict[str, Any]]] = None,
) -> Generator[Dict[str, Any], None, None]:
    """唯一的流式引擎：产出**帧 dict**（类型一律来自 stream_events，绝不在这里手写字面量）。

    给调用方的承诺与 stream_chat 同一条，只是产出从 str 变成帧：
    - 必须是同步生成器而不是 async：`for chunk in stream` 走的是阻塞 socket，async
      生成器会被事件循环直接驱动，一个卡住的上游就冻住整个进程（连 /health 都不响应，
      Cloudflare 报 524）；同步生成器才会被 StreamingResponse 的 iterate_in_threadpool
      放进线程池。
    - 异常一律向上抛出：若在此转成文本 yield，调用方无法区分正常回答与故障，错误文本
      还会被写进会话历史。
    - tools 默认 None 而不是自动装配：要不要给工具由调用方决定（历史上流式这条一个
      tools 形参都没有，界面上工具等于不存在）。
    - cancel_event / on_upstream_start / before_round 三件套的语义、计费与检查点
      同 stream_chat（见各段内联注释），此处不再重述。
    - usage 在 finally 里结：半截消耗同样要记账，取消/故障也不例外。

    过程留痕：非内容帧与步除了产出/回填 trace_out，还会就地塞进本线程的 ambient sink
    （若端点 begin_trace 过）。rich 判定 = 有 trace_out 或有 sink：只有需要发帧/留痕时
    才走 execute_tool_detailed 那条重一点的路；纯文本消费者（如 stream_chat 过滤器在
    无 sink 时）退回 execute_tool 的公开文本契约——两条都是真跑、都带 user_id，绝不双跑。
    """
    provider = store.resolve(provider_id, legacy_model=model)
    client = build_client(provider)

    sink = _sink()
    rich = (trace_out is not None) or (sink is not None)

    # 复制一份：工具轮次要往里追加 assistant/tool 消息，不能改调用方的列表
    msgs = list(messages)
    turns = max_tool_turns if tools else 1

    billed: Dict[str, int] = {k: 0 for k in ("prompt_tokens", "completion_tokens",
                                             "total_tokens", "reasoning_tokens", "cached_tokens")}
    rounds = 0
    ok = True

    def _push_frame(frame: Dict[str, Any]) -> None:
        # 非内容帧交进 ambient sink：stream_chat 这层文本过滤器吃不掉它们，
        # 端点靠 drain_frames() 逐块补发。content 帧不进 sink（端点自己发 content）。
        if sink is not None and frame.get("type") != se.FRAME_CONTENT:
            sink.frames.append(frame)

    def _record_step(step: Dict[str, Any]) -> None:
        if trace_out is not None:
            trace_out.append(step)
        if sink is not None:
            sink.steps.append(step)

    try:
        for _ in range(turns):
            # 轮首先查再 create：取消之后不许再有新一轮付费调用。这一句是
            # "取消后账本增量 = 0"里被数学上保证的那一半。
            if cancel_event is not None and cancel_event.is_set():
                raise StreamCancelled()
            # R3b-1 的钱闸（检查点在此、判据在调用方）：第一个付费轮不查——用户刚
            # POST 过，首轮本就是被同意发起的，且读者的 attach 是端点返回之后的异步
            # 事件，拿首轮去等它是把协议竞态当产品前提。此后每一轮都要过闸门；闸门等待
            # 尊重 1→0 的宽限窗（不忙轮询，挂 cond）。封顶："已在飞行中的那一流完、
            # 结账，新的轮次为零"——不承诺断线即刻免费。
            if rounds > 0 and before_round is not None and not before_round():
                raise StreamCancelled()
            kwargs: Dict[str, Any] = {
                "model": provider["model"],
                "messages": msgs,
                "stream": True,
                "temperature": temperature,
                "max_tokens": max_tokens,
                # 没有这一行，流式响应里永远不会有 usage，账本只能记"不知道"。
                # 网关若因此回 4xx，它会以 failed 出现在账上——那正是这本账要看得见的事。
                "stream_options": {"include_usage": True},
            }
            if tools:
                # 空数组不传：有些兼容网关对 tools=[] 直接 400
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"

            stream = client.chat.completions.create(**kwargs)
            # 把手里的连接交给要停它的人（交接失败不影响本轮内容，吞掉即可）；
            # create 与首块之间也可能正好被取消，交完句柄立刻查一次。
            if on_upstream_start is not None:
                try:
                    on_upstream_start(stream)
                except Exception:
                    pass
            if cancel_event is not None and cancel_event.is_set():
                _close_quietly(stream)
                raise StreamCancelled()

            text = ""
            # 工具调用在流式里是按 index 分片回来的：id/name 通常只在第一片，
            # arguments 一串 JSON 被切成任意多片，所以必须按 index 累积再解析。
            calls: Dict[int, Dict[str, str]] = {}
            # 思考（reasoning_content）逐字来，一个字一帧会把连接刷爆；按"攒够字数"
            # 或"过了时间没发"两种触发合并成段发出。单轮累计封顶，超了就停止外发
            # （绝不为封顶去抛异常——那是把展示问题升级成故障）。
            think_buf = ""
            think_emitted = 0
            think_last_flush = time.monotonic()
            round_think: List[str] = []

            def flush_thinking() -> Generator[Dict[str, Any], None, None]:
                nonlocal think_buf, think_emitted, think_last_flush
                if not think_buf:
                    return
                room = se.THINKING_TOTAL_MAX - think_emitted
                if room <= 0:
                    think_buf = ""
                    return
                emit = think_buf[:room]
                think_buf = ""
                think_emitted += len(emit)
                think_last_flush = time.monotonic()
                round_think.append(emit)
                frame = se.thinking_frame(emit)
                _push_frame(frame)
                yield frame

            rounds += 1
            try:
                for chunk in stream:
                    # 逐块检查取消标志——诚实边界：这里的"立刻"是"下一个块到达
                    # 时立刻"；close() 常常（不保证）让读侧当场报错，那条路走下面
                    # 的 except 归类。
                    if cancel_event is not None and cancel_event.is_set():
                        _close_quietly(stream)
                        raise StreamCancelled()
                    # usage 通常在最后一个块里，而那个块的 choices 是空数组——
                    # 先读账再 continue，否则永远读不到。
                    u = getattr(chunk, "usage", None)
                    if u is not None:
                        billed["prompt_tokens"] += getattr(u, "prompt_tokens", 0) or 0
                        billed["completion_tokens"] += getattr(u, "completion_tokens", 0) or 0
                        billed["total_tokens"] += getattr(u, "total_tokens", 0) or 0
                        details = getattr(u, "completion_tokens_details", None)
                        billed["reasoning_tokens"] += (getattr(details, "reasoning_tokens", 0) or 0) if details else 0
                        pdetails = getattr(u, "prompt_tokens_details", None)
                        billed["cached_tokens"] += (getattr(pdetails, "cached_tokens", 0) or 0) if pdetails else 0
                    if not getattr(chunk, "choices", None):
                        continue
                    delta = getattr(chunk.choices[0], "delta", None)
                    if delta is None:
                        continue
                    # 先收思考再收正文：思考帧排在它引出的正文之前，面板读起来才是
                    # "先想后说"。上游没有 reasoning_content 这颗属性时 getattr 回
                    # None，整段静默跳过（不是所有模型都回思考）。
                    rp = getattr(delta, "reasoning_content", None)
                    if rp and rich and think_emitted < se.THINKING_TOTAL_MAX:
                        think_buf += rp
                        if len(think_buf) >= se.THINKING_FLUSH_CHARS:
                            yield from flush_thinking()
                    piece = getattr(delta, "content", None)
                    if piece:
                        # 正文来了，先把还没发完的思考按时间触发冲一次——但不冲
                        # 光：THINKING_FLUSH_SECONDS 只是"别让面板长时间没动静"的下限，
                        # 攒够字数那条已在上面即时冲过。
                        if think_buf and (time.monotonic() - think_last_flush) >= se.THINKING_FLUSH_SECONDS:
                            yield from flush_thinking()
                        text += piece
                        frame = se.content_frame(piece)
                        yield frame
                    for tc in (getattr(delta, "tool_calls", None) or []):
                        slot = calls.setdefault(getattr(tc, "index", 0) or 0,
                                                {"id": "", "name": "", "arguments": ""})
                        if getattr(tc, "id", None):
                            slot["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            if getattr(fn, "name", None):
                                slot["name"] += fn.name
                            if getattr(fn, "arguments", None):
                                slot["arguments"] += fn.arguments
            except StreamCancelled:
                raise
            except BaseException:
                # 取消时 close 一条正在读的连接，在 read 侧多半炸出一个底层异常
                # （httpx 的 StreamClosed 之类）：标志位翻着的时候炸的，算取消，
                # 不算模型故障——账仍然在下面 finally 里结，但调用方发的是
                # cancelled 终帧，不是 error。
                if cancel_event is not None and cancel_event.is_set():
                    raise StreamCancelled() from None
                raise
            finally:
                # 正常读干、取消、异常三条路都过这一遍：连接不该靠 GC 收场。
                _close_quietly(stream)

            # 本轮读完：把残留的思考在工具帧之前冲光——顺序必须是"想完→再去调工具"，
            # 否则面板上会出现"先开始算、后面才补一句它刚才在想什么"。
            if rich:
                yield from flush_thinking()
                if round_think:
                    _record_step(se.step_thinking("".join(round_think)))

            # 自然读完但取消恰好在这之后到达：工具回灌前再查一次。跑工具本身
            # 不再花上游的钱，但回灌之后必进下一轮 create()——在这里掐死最干净。
            if cancel_event is not None and cancel_event.is_set():
                raise StreamCancelled()

            if not calls:
                return

            msgs.append({
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {"id": slot["id"], "type": "function",
                     "function": {"name": slot["name"], "arguments": slot["arguments"]}}
                    for _, slot in sorted(calls.items())
                ],
            })

            for _, slot in sorted(calls.items()):
                call_id = slot["id"]
                name = slot["name"]
                raw_args = slot["arguments"]
                # 与 pipeline.py 那行 [Pipeline] 调用工具 同一用途：这条路上曾经静默了
                # 几个月，出事只能靠猜"到底有没有执行"。
                print(f"[Stream] 调用工具: {name}({raw_args})", flush=True)

                # 先解析一份参数用于帧的展示：合法 JSON → 结构化 arguments（客户端按行
                # 渲染最好看）；不合法 → 原样 raw 串走 arguments_text，那一帧仍有用，
                # 因为"参数不是 JSON"本身就是接下来失败的原因。
                try:
                    parsed_args = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    parsed_args = raw_args
                label = _tool_label(name, parsed_args)

                # 工具开始执行**之前**先把 tool_call 帧发出去：用户要看到"它现在要去
                # 算什么"，等算完再发就失去意义了。
                if rich:
                    frame = se.tool_call_frame(call_id, name, parsed_args, label)
                    _push_frame(frame)
                    yield frame

                if rich:
                    outcome = _run_tool_detailed(name, raw_args, user_id=user_id)
                    result = outcome.text
                else:
                    result = _run_tool(name, raw_args, user_id=user_id)
                    outcome = None
                msgs.append({"role": "tool", "tool_call_id": call_id,
                             "content": result})

                if rich and outcome is not None:
                    summary = result
                    rframe = se.tool_result_frame(call_id, name, outcome.ok, summary,
                                                  elapsed_ms=outcome.elapsed_ms,
                                                  truncated=outcome.truncated)
                    _push_frame(rframe)
                    yield rframe
                    _record_step(se.step_tool(call_id, name, label, outcome.ok, summary,
                                              elapsed_ms=outcome.elapsed_ms,
                                              truncated=outcome.truncated))
                    art = outcome.artifacts
                    if isinstance(art, dict) and art.get("kind") == "web_search":
                        sframe = se.search_frame(call_id, art.get("query", ""),
                                                 art.get("results"))
                        _push_frame(sframe)
                        yield sframe
                        _record_step(se.step_search(call_id, art.get("query", ""),
                                                    art.get("results")))


    except BaseException:
        ok = False
        raise
    finally:
        # v0.25 R3 起这条路上的"读者断开"不再终结生成（生产线程抽干为止），
        # 落到 failed 的成因改为取消与真故障。半截消耗同样要记账：那一半的 token
        # 是真花出去了，只记跑完的那一次会系统性低估，也永远看不见故障率。
        usage.record_call(user_id=user_id or "unattributed",
                          provider_id=provider["id"],
                          paid_by=provider.get("paid_by") or "operator",
                          tool_rounds=max(0, rounds - 1), ok=ok, **billed)

def _run_tool(name: str, raw_arguments: str, user_id: str = None) -> str:
    """执行一个工具调用，永远回一个字符串（非富路径：只需要给模型的文本）。

    工具炸了不能把整条 SSE 打断：那时用户看到的是半句话加一个断流，而模型永远
    不知道自己哪里没算成。把错误当作工具结果回灌，模型才能解释它。

    与 _run_tool_detailed 是同一条执行的两种投影（execute_tool 就是
    execute_tool_detailed(...).text），两条都把服务端算出的 user_id 递到底。
    """
    try:
        args = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as e:
        return f"工具参数不是合法 JSON，没能执行：{e}"
    try:
        return str(execute_tool(name, args, user_id=user_id))
    except Exception as e:  # 工具内部任何异常都只影响这一次调用
        return f"工具执行错误: {e}"


def _run_tool_detailed(name: str, raw_arguments: str, user_id: str = None) -> ToolOutcome:
    """执行一个工具调用并取回**结构化元数据**（富路径：要发帧、要留痕时用）。

    与非富路径同一处单一实现（executor.execute_tool_detailed），一次执行同时给出
    给模型的文本与 ok/truncated/elapsed_ms/artifacts。任何异常、坏 JSON 都折成
    ok=False 的 ToolOutcome——绝不因为一次工具没跑成就把整条流打断。
    """
    try:
        args = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as e:
        return ToolOutcome(text=f"工具参数不是合法 JSON，没能执行：{e}",
                           ok=False, artifacts=None, truncated=False, elapsed_ms=0)
    try:
        return execute_tool_detailed(name, args, user_id=user_id)
    except Exception as e:  # 工具内部任何异常都只影响这一次调用
        return ToolOutcome(text=f"工具执行错误: {e}", ok=False,
                           artifacts=None, truncated=False, elapsed_ms=0)
