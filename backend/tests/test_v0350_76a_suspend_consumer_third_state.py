"""工单 #76a 的行为锁：消费端把"停在断点"记成第三态，而不是记成一次失败。

背景（#29 把护栏的第三格开好了，#76a 才把线接上）：
`record_outcome(..., suspended=True)` 在 `guardrails.py:167-173`，排在 `:175` 的 `if ok:`
之前；调用点此前一律传 `ok=bool(outcome.ok)`，于是"请输入短信验证码"被读成一次工具
失败，两次就把这个工具在本轮熔断（`guardrails.py:177-180`）。#76a 把四个读数点接上
结构化那一格：`pipeline.py:306`、`streaming.py:478`、`react_agent.py:212`、
`task_agent.py:178`。#29 那一份锁（`test_v0350_ticket29_guardrail_third_state.py`）
是**直接对 RoundGuard 说话**的，摘掉调用点的 `suspended=` 它一条都不会红——所以本文件
补的是"走真实代码路"的那一把，摘掉就红。

判据纪律（本仓明文规矩，逐条对着写）：
* 判据一律问**世界事实**：`guard.check()` 在门口给的回话、公开属性 `tool_rounds` /
  `guard_events` / `suspension_counts`。没有一条断言读"被测对象自己说它记了什么"。
* 抓护栏实例的那个钩子**不实现任何被验行为**：它 `return` 真的 `RoundGuard(*args)`，
  只是把"这一条路上用的那一个实例"交出来看（每次请求只有一个实例，实例随调用结束就
  被丢掉，不抓就无从读起）。验证器不顺手做它要验的记账。
* 每条锁都配**同一条路上的反例**：同一路、同一夹具，只差"挂起"与"真失败"。反例必须
  熔断——否则上面那条绿只是因为这条路根本没往护栏里记任何东西。
* D1（不许发明文本前缀判据）在这里为什么不给"前缀 vs 结构化"的反例锁：渲染器
  （`tools/response.py:84-87`）让每一次真挂起的文本都以 `⏸ ` 开头，而成功/失败两支分别是
  `✓ `/`✗ 错误: `——所以在真执行器下面两者今天恒等相关，行为层面造不出"前缀说挂起、
  结构化说不挂起"的输入。**实测过**：把 pipeline 与 react_agent 两处**挂起**判据换成
  `startswith("⏸ ")` 之后，本文件 7 条全绿（rc=0）——**本文件这把行为锁到今天仍然管不了前缀
  判据**，别以为它在管。原因不在测试写得薄，而在等价性由执行器替所有读法兜住
  （`executor.py:251-256` 的 fail-closed + `executor.py:82` 的保头裁剪），这一族回归在行为层面
  **结构上抓不到**。
* 抓得到的是源码形状，而**形状这一侧此前无锁**（只靠人工审那四个读数点）。⭐ 工单 #80 已把它
  变成两把机器锁，就在 `tests/test_v0350_80_d1_shape_and_forbidden_prefix_locks.py`：
  **形状锁**钉四个读数点（`pipeline.py:306`、`streaming.py:478`、`react_agent.py:212`、
  `task_agent.py:178`）的两个关键字实参必须逐字是 `ok=bool(outcome.ok)` 与
  `suspended=bool(outcome.suspended)`；它的**孪生判据**管的是"记账不许被 `startswith(...)` 的结果
  喂进去"，跟随射程按 #83 第二轮实测写：**一跳之内覆盖 `=`/`:` 赋值以及 walrus（`:=`）目标与
  下标/属性实参的基名回溯，两跳以上仍然漏**（仍漏清单在锁文件的边界段里列着）。walrus/下标/
  属性这三枚一跳变异第一轮实测是**绿的**（漏），跟随补强后各红一次；两跳那枚补强后实测仍绿。
  #80 原文那句"藏进一个再套一层的变量……本条就红"是过度声称，已按实测收窄（边界与"剩下那一半
  由逐字形状锁兜"的论证见锁文件 `test_d1_read_points_are_never_fed_by_a_text_prefix`）。**禁字锁**钉 `⏸` / `SUSPEND_PREFIX`
  以及 #83 补的构造写法 `chr(9208)` / `\\u23f8` 在 `backend/app/**` 的 `.py` 里只许出现在
  `tools/response.py` 的定义与渲染两处 —— 文本级判据，只守"源码里看得见的写法"，运行时才拼出来的
  串在射程外，**不声称已穷尽**。硬造一把前缀反例锁仍然不被接受：那只能靠 mock 渲染器，
  等于让被测对象自报，本仓不认。
* ⭐ 工单 #76b（本票的第二波，只换判据、不加锁也不删锁）：`react_agent.py:212` 那一行的**成败**
  判据也已从 `str(tool_result).startswith("✓")` 换成 `ok=bool(outcome.ok)`，与上面四处同形；同一
  口径实测过——把那处摘回文本前缀写法之后**全量仍绿**（passed/skipped/rc 见 #76b 的 commit
  正文）⇒ 落锁之前本文件与 #29 那一份只管 `suspended=`，**当时**没有任何一把锁读得出这一处回归；
  这一处现在由 #80 的形状锁读（协议层 `ToolOutcome` 不校验，手搓 `ok=True/text="t"` 仍合法，
  那一格不在本单射程）。

现网可达性：这一批对现网零行为改变——`ToolSuspended` 在 `backend/app/**` 里零 `raise`，
`registry` 也没有任何一条工具声明 `suspends`，第三态今天到不了。本文件的道具就是一次
`raise ToolSuspended`，只在 pytest 内注册、收尾原样摘掉（`data/` 一个字节都不碰：
所有断言都收在护栏实例上，不落盘）。
"""
import importlib
import json
from types import SimpleNamespace

import pytest

import app.agents.react_agent as react_agent_mod
import app.core.streaming as streaming
import app.pipeline as pipeline_mod
from app.agents.react_agent import ReActAgent
from app.core.guardrails import RoundGuard as RealRoundGuard
from app.tools.registry import tools_registry
from app.tools.response import ToolResponse, ToolSuspended

SUSPENDING = "t76a_code"       # 每次都停在断点问用户要验证码
FAILING = "t76a_broken"        # 每次都真失败（反例用的道具）


@pytest.fixture
def t76a_tools():
    """往全局注册表插两条道具（一条必挂起、一条必失败），收尾原样摘掉。

    为什么不借 `test_v0350_a3_*` 那份 `fake_tools`：那份夹具带着 task_store 落盘和八个
    本单用不到的道具；跨文件借夹具等于把"道具长什么样"交给别人决定。这里只要两件事：
    `suspends: True`（`executor.py:225-232` 的 `_can_resume` 要它，才给得出可续的挂起）
    与"每次调用都走到那一步"。
    """
    calls = []

    def ask_for_code(item: str = "拿铁"):
        calls.append((SUSPENDING, item))
        raise ToolSuspended("请输入短信验证码", {"step": 1, "item": item})

    def always_broken(item: str = "拿铁"):
        calls.append((FAILING, item))
        return ToolResponse(False, error="上游没应答", hint="稍后再试")

    entries = {
        SUSPENDING: {"function": ask_for_code, "description": "要验证码的道具",
                     "parameters": {"type": "object",
                                    "properties": {"item": {"type": "string"}}},
                     "available": None, "needs_user": False,
                     "requires_approval": False, "suspends": True},
        FAILING: {"function": always_broken, "description": "一直失败的工具",
                  "parameters": {"type": "object",
                                 "properties": {"item": {"type": "string"}}},
                  "available": None, "needs_user": False,
                  "requires_approval": False, "suspends": False},
    }
    saved = {name: tools_registry.get(name) for name in entries}
    for name, info in entries.items():
        tools_registry[name] = info
    try:
        yield calls
    finally:
        for name, previous in saved.items():
            if previous is None:
                tools_registry.pop(name, None)
            else:
                tools_registry[name] = previous


def _spy_guard(monkeypatch, module, seen):
    """把某个模块里 `RoundGuard()` 那一句换成"造真实例并交出来看"的钩子。

    钩子自己不做任何记账（它只是构造函数），被验的行为全部由真护栏完成。
    """
    def factory(*args, **kwargs):
        guard = RealRoundGuard(*args, **kwargs)
        seen.append(guard)
        return guard

    monkeypatch.setattr(module, "RoundGuard", factory)
    return seen


# ---------- 非流式 / ReAct 那条用的假上游（一次 create 回一条完整消息） ----------

def _tool_call_msg(name: str, arguments: dict, call_id: str):
    tc = SimpleNamespace(
        id=call_id, type="function",
        function=SimpleNamespace(name=name,
                                arguments=json.dumps(arguments, ensure_ascii=False)))
    return SimpleNamespace(content=None, tool_calls=[tc],
                           model_dump=lambda: {"role": "assistant", "content": None,
                                               "tool_calls": [tc.id]})


def _text_msg(content: str):
    return SimpleNamespace(content=content, tool_calls=None,
                           model_dump=lambda: {"role": "assistant", "content": content})


def _resp(msg):
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])


class _Completions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class _Client:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_Completions(responses))


# ---------- 流式那条用的假上游（一次 create 回一串 chunk） ----------

def _chunk(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)])


def _tool_call_delta(call_id, name, arguments):
    return SimpleNamespace(index=0, id=call_id,
                           function=SimpleNamespace(name=name, arguments=arguments))


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)


class _StreamCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Stream(self.responses.pop(0) if self.responses else [])


class _StreamClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_StreamCompletions(responses))


@pytest.fixture
def real_stream_chat(monkeypatch):
    """拿回真的 stream_chat 并接上假客户端（conftest 的 autouse 桩把函数换掉了，先 reload）。

    没有 begin_trace()，`_sink()` 为空 ⇒ `rich=False` ⇒ 走的是本单改过的那一支
    `_run_tool`（现网不可达、只有测试可达，所以这一支必须自己在 pytest 里有牙）。
    """
    importlib.reload(streaming)
    holder = {}

    def build(provider):
        return holder["client"]

    monkeypatch.setattr(streaming, "build_client", build)

    def install(responses):
        holder["client"] = _StreamClient(responses)
        return holder["client"].chat.completions

    yield install
    importlib.reload(streaming)


def _client_for(module, monkeypatch, responses):
    """给 pipeline / react_agent 装一个按脚本回消息的假上游，并交回它的 completions（可查 calls）。"""
    client = _Client(responses)
    monkeypatch.setattr(module, "build_client", lambda provider: client)
    return client.chat.completions


def _tool_msgs_of(calls):
    """从最后一次 create 的 messages 里取回灌给模型的 tool 消息。"""
    return [m for m in calls[-1]["messages"] if m.get("role") == "tool"]


def _assert_two_suspensions_recorded_as_third_state(guard, seen_calls):
    """三条锁共用的判据：全部问护栏这台机器，不看任何自报。"""
    types = [e["type"] for e in guard.guard_events]
    assert types == ["suspend", "suspend"], (
        f"两次挂起只许产出两条 suspend 事件（挂起既不是失败也不是成功）：{guard.guard_events}")
    assert guard.suspension_counts == {SUSPENDING: 2}, (
        f"摘掉 suspended= 之后这一格是空的：{guard.suspension_counts!r}")
    assert guard.tool_rounds == 2, (
        f"挂起照记轮次（不记就是给重试留免费额度）：{guard.tool_rounds}")
    assert seen_calls == [(SUSPENDING, "拿铁"), (SUSPENDING, "美式")], (
        f"两次挂起其实没跑到工具里：{seen_calls}")
    # 门口那一句是唯一的熔断判据：摘掉 suspended= 时这里会给出"连续失败 2 次"。
    blocked = guard.check(SUSPENDING, {"probe": "第三次还要不要问"})
    assert blocked is None, f"两次挂起把这个工具熔断了：{blocked!r}"


# ---------- 锁 1：非流式（pipeline，rich=True 那一支，读数点 pipeline.py:306） ----------

def test_lock1_pipeline_suspensions_never_fuse_the_tool(monkeypatch, t76a_tools):
    """走 pipeline 的真实工具循环：连续两次挂起 → 不熔断、挂起账是 2。

    摘掉 `suspended=` 会红在哪：`pipeline.py:306-307` 退回只传 ok 之后，两次挂起就是两次
    ok=False → `guardrails.py:177-180` 跳退避 → 本条最后一句 `check()` 拿到"连续失败 2 次"
    （并且 `suspension_counts` 变空、事件变成 fail_backoff），红在 `_assert_...` 那三条里。
    """
    seen = _spy_guard(monkeypatch, pipeline_mod, [])
    calls = _client_for(pipeline_mod, monkeypatch, [
        _resp(_tool_call_msg(SUSPENDING, {"item": "拿铁"}, "c1")),
        _resp(_tool_call_msg(SUSPENDING, {"item": "美式"}, "c2")),
        _resp(_text_msg("好的，先把验证码发我")),
    ])

    reply = pipeline_mod.ChatPipeline("u_76a_pipe")._call_model_with_tool_loop(
        "fake-chat", [{"role": "user", "content": "点一杯咖啡"}],
        provider_id="fake-model", trace_out=[])

    assert reply == "好的，先把验证码发我", reply
    assert len(seen) == 1, f"一条请求应当只有一个护栏实例，实得 {len(seen)}"
    _assert_two_suspensions_recorded_as_third_state(seen[0], t76a_tools)
    # 第三态没被当成功：模型拿到的仍然是那句问话（⏸ 那一支），不是 ✓、也不是 ✗
    tool_msgs = _tool_msgs_of(calls.calls)
    assert tool_msgs and tool_msgs[-1]["content"].startswith("⏸ "), tool_msgs


def test_lock1_control_pipeline_real_failures_still_fuse(monkeypatch, t76a_tools):
    """同一条路上的反例：两次真失败必须熔断——否则锁 1 的绿只是因为什么都没记。"""
    seen = _spy_guard(monkeypatch, pipeline_mod, [])
    _client_for(pipeline_mod, monkeypatch, [
        _resp(_tool_call_msg(FAILING, {"item": "拿铁"}, "f1")),
        _resp(_tool_call_msg(FAILING, {"item": "美式"}, "f2")),
        _resp(_text_msg("这台机器暂时不可用")),
    ])

    pipeline_mod.ChatPipeline("u_76a_fail")._call_model_with_tool_loop(
        "fake-chat", [{"role": "user", "content": "点一杯咖啡"}],
        provider_id="fake-model", trace_out=[])

    guard = seen[0]
    assert guard.suspension_counts == {}, guard.suspension_counts
    assert {"fail_backoff"} <= {e["type"] for e in guard.guard_events}, guard.guard_events
    blocked = guard.check(FAILING, {"probe": "还要不要试"})
    assert blocked and "连续失败 2 次" in blocked, (
        f"两次真失败必须熔断（#29 没放宽这一条）：{blocked!r}")


# ---------- 锁 2：ReAct（react_agent.py:212，#76a 换 detailed、#76b 把成败判据换成 outcome.ok） ----------

def test_lock2_react_agent_suspensions_never_fuse_the_tool(monkeypatch, t76a_tools):
    """走 ReActAgent.run 的真实循环：证明 :212 传的是 `outcome.suspended` 那格。

    这一条路 #76a 之前只有纯文本（`execute_tool` 的投影）+ `startswith("✓")` 猜成败，
    挂起必然被记成失败；#76a 换成 detailed 后文本仍取 `.text`，#76b 已把那处成败判据换成
    `ok=bool(outcome.ok)`（**本文件**读不出那一步 —— 行为锁结构上抓不到，那一种回归今天由
    #80 的形状锁 + 禁字锁读，见文件头 D1 那一族）。摘掉 `suspended=` →
    两记失败 → 本条最后一句红。
    """
    seen = _spy_guard(monkeypatch, react_agent_mod, [])
    calls = _client_for(react_agent_mod, monkeypatch, [
        _resp(_tool_call_msg(SUSPENDING, {"item": "拿铁"}, "c1")),
        _resp(_tool_call_msg(SUSPENDING, {"item": "美式"}, "c2")),
        _resp(_text_msg("收到，我等你的验证码")),
    ])

    answer = ReActAgent(model="fake-chat", max_turns=3).run(
        "点一杯咖啡", user_id="u_76a_react", provider_id="fake-model")

    assert answer == "收到，我等你的验证码", answer
    assert len(seen) == 1, f"一次 run 应当只有一个护栏实例，实得 {len(seen)}"
    _assert_two_suspensions_recorded_as_third_state(seen[0], t76a_tools)
    # ReAct 这条给模型的是 `outcome.text`（投影没丢）：那句问话原样进 tool 消息
    tool_msgs = _tool_msgs_of(calls.calls)
    assert tool_msgs and tool_msgs[-1]["content"].startswith("⏸ "), tool_msgs


def test_lock2_control_react_agent_real_failures_still_fuse(monkeypatch, t76a_tools):
    """同一条路上的反例：ReAct 这条两次真失败照样熔断，兜底异常支（:217）也不许被顺手改成挂起。"""
    seen = _spy_guard(monkeypatch, react_agent_mod, [])
    _client_for(react_agent_mod, monkeypatch, [
        _resp(_tool_call_msg(FAILING, {"item": "拿铁"}, "f1")),
        _resp(_tool_call_msg(FAILING, {"item": "美式"}, "f2")),
        _resp(_text_msg("这个工具坏了")),
    ])

    ReActAgent(model="fake-chat", max_turns=3).run(
        "点一杯咖啡", user_id="u_76a_react2", provider_id="fake-model")

    guard = seen[0]
    assert guard.suspension_counts == {}, guard.suspension_counts
    blocked = guard.check(FAILING, {"probe": "再来一次"})
    assert blocked and "连续失败 2 次" in blocked, (
        f"ReAct 这条路的失败退避不许被放宽：{blocked!r}")


# ---------- 锁 3：流式非富那一支（streaming.py:478 + 本单重写的 _run_tool） ----------

def test_lock3_streaming_non_rich_suspensions_never_fuse(monkeypatch, real_stream_chat,
                                                          t76a_tools):
    """`stream_chat`（无 sink ⇒ rich=False）也要产出第三态：证明 `_run_tool` 换形状没换错。

    这一支现网不可达（生产流式走 `stream_chat_events` + sink ⇒ rich=True），改动前它连
    outcome 都没有、`ok` 恒 True；现在两支同形，读数点只剩 `streaming.py:478` 一处。
    摘掉 `suspended=` → 两记失败 → `check()` 给出"连续失败 2 次"，本条红。
    """
    completions = real_stream_chat([
        [_chunk(tool_calls=[_tool_call_delta("c1", SUSPENDING,
                                             json.dumps({"item": "拿铁"}, ensure_ascii=False))])],
        [_chunk(tool_calls=[_tool_call_delta("c2", SUSPENDING,
                                             json.dumps({"item": "美式"}, ensure_ascii=False))])],
        [_chunk(content="好的，把验证码发我"), _chunk(content="就行")],
    ])
    seen = _spy_guard(monkeypatch, streaming, [])

    text = "".join(streaming.stream_chat(
        "fake-chat", [{"role": "user", "content": "点一杯咖啡"}],
        provider_id="fake-model", tools=[{"type": "function", "function": {"name": SUSPENDING}}],
        user_id="u_76a_stream"))

    assert text == "好的，把验证码发我就行", text
    assert len(seen) == 1, f"一次请求应当只有一个护栏实例，实得 {len(seen)}"
    _assert_two_suspensions_recorded_as_third_state(seen[0], t76a_tools)
    # 文本投影仍在：模型拿到的那句必须还是以 ⏸ 开头的那句话（不是 ✓、也不是 ✗）
    tool_msgs = _tool_msgs_of(completions.calls)
    assert tool_msgs and tool_msgs[-1]["content"].startswith("⏸ "), tool_msgs


def test_lock3_control_streaming_non_rich_real_failures_still_fuse(real_stream_chat,
                                                                   monkeypatch,
                                                                   t76a_tools):
    """同一条路上的反例：非富那支两次真失败必须熔断（改动前这一支 ok 恒 True，从不熔断）。"""
    real_stream_chat([
        [_chunk(tool_calls=[_tool_call_delta("f1", FAILING,
                                             json.dumps({"item": "拿铁"}, ensure_ascii=False))])],
        [_chunk(tool_calls=[_tool_call_delta("f2", FAILING,
                                             json.dumps({"item": "美式"}, ensure_ascii=False))])],
        [_chunk(content="它坏了")],
    ])
    seen = _spy_guard(monkeypatch, streaming, [])

    "".join(streaming.stream_chat(
        "fake-chat", [{"role": "user", "content": "点一杯咖啡"}],
        provider_id="fake-model", tools=[{"type": "function", "function": {"name": FAILING}}],
        user_id="u_76a_stream2"))

    guard = seen[0]
    assert guard.suspension_counts == {}, guard.suspension_counts
    blocked = guard.check(FAILING, {"probe": "再试一次"})
    assert blocked and "连续失败 2 次" in blocked, blocked


def test_lock3_streaming_non_rich_keeps_the_exception_and_json_fallbacks(real_stream_chat,
                                                                        monkeypatch):
    """`_run_tool` 换形状之后，它自己那两道兜底一个字都不许丢。

    改动前 `_run_tool` 里写着坏 JSON 与异常折文本两支；换成交给 `_run_tool_detailed` 之后
    文案必须逐字相同（模型读的那句不能变），而且这一次调用要记成**失败**而不是挂起。
    这里把执行器换成必然抛异常的桩：红的两种坏法是①异常外飘把整条 SSE 打断（正文出不来），
    ②兜底文案丢了，③把异常记成挂起（suspension_counts 非空）。
    """
    def boom(name, args, user_id=None):
        raise RuntimeError("沙箱没了")

    monkeypatch.setattr(streaming, "execute_tool_detailed", boom)
    bad_args = json.dumps({"expression": "1+"})[:-1]      # 故意留一段模型常吐的残缺 JSON
    completions = real_stream_chat([
        [_chunk(tool_calls=[_tool_call_delta("c_boom", "calculator",
                                             json.dumps({"expression": "1+1"}))])],
        [_chunk(tool_calls=[_tool_call_delta("c_bad", "calculator", bad_args)])],
        [_chunk(content="我没能算成")],
    ])
    seen = _spy_guard(monkeypatch, streaming, [])

    text = "".join(streaming.stream_chat(
        "fake-chat", [{"role": "user", "content": "算一下"}],
        provider_id="fake-model", tools=[{"type": "function", "function": {"name": "calculator"}}],
        user_id="u_76a_boom"))

    assert text == "我没能算成", text
    guard = seen[0]
    assert guard.suspension_counts == {}, (
        f"异常不是挂起，不许记进第三态：{guard.suspension_counts!r}")
    assert guard.tool_rounds == 2, guard.tool_rounds
    contents = [m["content"] for m in _tool_msgs_of(completions.calls)]
    assert any("工具执行错误" in c for c in contents), contents
    assert any("工具参数不是合法 JSON" in c for c in contents), contents
