"""流式输出模块 - 基于 Server-Sent Events (SSE)。

模型配置统一来自 app.core.providers，不再自带一份模型清单——此前
streaming 与 llm_client 各有一份 MODEL_CONFIG，改一处不生效；且未知模型会
静默回落到 deepseek-chat，造成"界面显示 GPT-4o、实际是 DeepSeek 在答"。
"""
import json
from typing import Generator, List, Dict, Any, Optional

from app.core.providers import store, build_client
from app.core import usage   # 账本：两条聊天路径共用一个口径，别各记一套
from app.tools.executor import execute_tool


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
    """流式生成 AI 回复，逐块产出文本；给了 tools 就带上工具循环。

    异常一律向上抛出：若在此转成文本 yield，调用方无法区分正常回答与故障，
    错误文本还会被写进会话历史。

    这里必须是同步生成器而不是 async：`for chunk in stream` 走的是阻塞 socket。
    async 生成器会被事件循环直接驱动，一个卡住的上游就冻住整个进程（连 /health
    都不响应，Cloudflare 报 524）；同步生成器才会被
    StreamingResponse 的 iterate_in_threadpool 放进线程池。

    tools 默认 None 而不是自动装配：非流式那条路（ChatPipeline）一直是把工具传给
    模型的，这里曾经一个 tools 形参都没有，于是只走流式的界面上，工具等于不存在——
    模型如实回答"我无法调用外部计算器工具"。要不要给工具由调用方决定。

    cancel_event / on_upstream_start（v0.25 R3，都默认 None，直连调用方不受影响）：
    取消要能真的停下来，靠两件事——轮首与逐块检查 cancel_event（保证**下一个**
    付费 create() 不发生），以及 on_upstream_start 把手里正在读的上游连接交出去
    （交给 stream_runs.StreamRun，取消端点从另一个线程尽力 close 它）。
    诚实的边界：close 一条正被阻塞读取的连接不保证立刻打断那次读，所以取消最迟
    在下一个块到达或读超时（build_client 默认 120s）生效；被保证的是不再新增
    一次付费调用。工具回灌后的下一轮在 create() 之前就被拦下——那才是"重复计费"
    的真正来源。

    before_round（v0.25 R3b，默认 None，直连调用方不受影响）：第一个付费轮之后、
    每一次新一轮 create() 之前的闸门，返回 False 即按取消收场。判据归调用方
    （main.py 用它查"这轮还有没有活读者"），检查点必须放在这里——因为这个循环
    的结构就是"付费边界"本身，放在别处都隔着一次 create()。它与 cancel_event
    共用同一条 StreamCancelled 出口：宽限到点不是第二套"超时"语义，就是把既有
    取消路径按下去的手。诚实边界与上面同一句：能保证的是"已在飞行中的那一流完、
    结账，新的轮次为零"，不能承诺断线即刻免费。
    """
    provider = store.resolve(provider_id, legacy_model=model)
    client = build_client(provider)

    # 复制一份：工具轮次要往里追加 assistant/tool 消息，不能改调用方的列表
    msgs = list(messages)
    turns = max_tool_turns if tools else 1

    billed: Dict[str, int] = {k: 0 for k in ("prompt_tokens", "completion_tokens",
                                             "total_tokens", "reasoning_tokens", "cached_tokens")}
    rounds = 0
    ok = True
    try:
        for _ in range(turns):
            # 轮首先查再 create：取消之后不许再有新一轮付费调用。这一句是
            # "取消后账本增量 = 0"里被数学上保证的那一半。
            if cancel_event is not None and cancel_event.is_set():
                raise StreamCancelled()
            # R3b-1 的钱闸（检查点在此、判据在调用方）：第一个付费轮不查——
            # 用户刚 POST 过，首轮本就是被同意发起的，且读者的 attach 是端点
            # 返回之后的异步事件，拿首轮去等它是把协议竞态当产品前提。此后
            # 每一轮都要过闸门；闸门等待尊重 1→0 的宽限窗（不忙轮询，挂 cond）。
            # 封顶："已在飞行中的那一轮流完并结账，新的轮次为零"——不承诺即刻
            # 掐断阻塞读，断线不免费。
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
                    piece = getattr(delta, "content", None)
                    if piece:
                        text += piece
                        yield piece
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
                # 与 pipeline.py 那行 [Pipeline] 调用工具 同一用途：这条路上曾经静默了
                # 几个月，出事只能靠猜"到底有没有执行"。
                print(f"[Stream] 调用工具: {slot['name']}({slot['arguments']})", flush=True)
                result = _run_tool(slot["name"], slot["arguments"], user_id=user_id)
                msgs.append({"role": "tool", "tool_call_id": slot["id"],
                             "content": result})


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
    """执行一个工具调用，永远回一个字符串。

    工具炸了不能把整条 SSE 打断：那时用户看到的是半句话加一个断流，而模型永远
    不知道自己哪里没算成。把错误当作工具结果回灌，模型才能解释它。
    """
    try:
        args = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as e:
        return f"工具参数不是合法 JSON，没能执行：{e}"
    try:
        return str(execute_tool(name, args, user_id=user_id))
    except Exception as e:  # 工具内部任何异常都只影响这一次调用
        return f"工具执行错误: {e}"
