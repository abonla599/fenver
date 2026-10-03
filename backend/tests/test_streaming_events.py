"""v0.29.0「过程留痕」引擎与契约的测试。

为什么单开这一个文件：过程帧（thinking/tool_call/tool_result/search）是这一版加进
`app.core.streaming` 的新引擎 `stream_chat_events` 的产物，而既有那批流式用例
（test_stream_tools / test_stream_api / v0.25 那几份取消与计费契约）测的都是"只给
正文文本"的旧契约。新引擎的每一条新保证——思考会被合并成帧、工具帧在真正执行**之前**
就发出去、搜索把 URL 还给人、URL 里的危险协议先在服务端抹平、落盘 trace 有预算——
都没有人钉。这里就是那批锁。

conftest 的 autouse 夹具把 `streaming.stream_chat` 整个换成假函数（别真调付费模型），
但它没碰 `stream_chat_events`；而且真身与假身要靠 reload 才分得开。所以每个用例先
用 `real_engine` 夹具 reload 拿回真函数、再把 `build_client` 换成喂假 chunk 的对象。
这套假客户端的形状照 `test_stream_tools.py`（OpenAI 流式：id/name 只在首片、
arguments 分片流回来、delta 里有 reasoning_content），改一处两処都要一起变。
"""
import importlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.core.streaming as streaming
from app.core import stream_events as se
from app.main import app

client = TestClient(app)

# 一个够用的假工具清单：引擎只用它来（a）决定是否进多轮、（b）原样塞进 create() 的
# kwargs。假客户端不看 kwargs，所以这里给什么都行，只要非空——空数组会被引擎当成
# "这轮不带工具"从而只跑一轮，工具回灌后就没有第二轮了。
_TOOLS = [{"type": "function", "function": {"name": "calculator",
                                            "parameters": {"properties": {}}}}]


# ---------- 假流式 chunk ----------

def _chunk(content=None, reasoning=None, tool_calls=None):
    """拼一个 OpenAI 流式 chunk 的壳：delta 上三颗属性都齐（没有的显式给 None）。"""
    delta = SimpleNamespace(content=content, reasoning_content=reasoning,
                            tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)],
                           usage=None)


def _tool_call_delta(index=0, call_id=None, name=None, arguments=None):
    """id/name 只在首片，arguments 分片流回来——真实 API 就是这样，必须按 index 累积。"""
    return SimpleNamespace(index=index, id=call_id,
                           function=SimpleNamespace(name=name, arguments=arguments))


class _RecordingStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        pass


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _RecordingStream(self.responses.pop(0) if self.responses else [])


class _FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_FakeCompletions(responses))


@pytest.fixture
def real_engine(monkeypatch):
    """reload 换回真的 stream_chat / stream_chat_events，并把 build_client 换成假客户端。

    必须先 reload：autouse 的 _stub_llm_calls 已把 streaming.stream_chat 换成假身。
    teardown 再 reload 一次，把模块恢复成别处的桩，免得污染后面的用例。
    """
    importlib.reload(streaming)
    holder = {}

    def build(provider):
        return holder["client"]

    monkeypatch.setattr(streaming, "build_client", build)

    def _install(responses):
        holder["client"] = _FakeClient(responses)
        return holder["client"].chat.completions

    yield _install
    importlib.reload(streaming)


def _run(*, tools=None, trace_out=None, messages=None):
    """抽干 stream_chat_events，返回 (帧列表, 收集到的步)。假客户端已由 real_engine 夹具
    在调用方装好，这里只负责把生成器跑干净并原样收帧——测试绝大多数只看这个。
    """
    frames = list(streaming.stream_chat_events(
        "fake-chat", messages or [{"role": "user", "content": "hi"}],
        provider_id="fake-model", tools=tools, trace_out=trace_out))
    return frames, (trace_out or [])


# ---------- 1. 向后兼容：stream_chat 仍是"只吐正文的字符串"薄过滤器 ----------

def test_stream_chat_still_joins_to_plain_str(real_engine):
    """正文之外的一切实在（思考、工具帧）都不许从 stream_chat 漏进字符串里。

    这一条钉的是整场重构的地基：老代码到处 `"".join(stream_chat(...))`，conftest 的
    桩、test_stream_api 的会话拼接都建立在这个形状上。把 stream_chat 改成吐帧 dict
    会一次性打断它们。真身在有 reasoning + 工具调用时也必须只回正文。
    """
    real_engine([
        [_chunk(reasoning="先想一下"),
         _chunk(tool_calls=[_tool_call_delta(call_id="c1", name="calculator",
                                             arguments='{"expression": "1+1"}')])],
        [_chunk(content="结果是 "), _chunk(content="2")],
    ])

    out = "".join(streaming.stream_chat("fake-chat", [{"role": "user", "content": "算"}],
                                        provider_id="fake-model", tools=_TOOLS))

    assert out == "结果是 2", f"只该流出正文，实得 {out!r}"


# ---------- 2. reasoning → thinking 帧，且在工具帧之前冲光 ----------

def test_reasoning_becomes_thinking_frames_flushed_before_tool_call(real_engine):
    """模型在想什么要能上屏；而"想完"必须排在"去调工具"之前。

    如果只在攒够字数时才发思考帧，一句短思考会永远卡在缓冲区里不上屏——所以本轮
    读完、发工具帧之前必须把残留思考冲光（顺序错了面板就会"先开始算、后补一句它
    刚才在想什么"）。同时留一份 step_thinking 进 trace，供回看。
    """
    trace_out = []
    real_engine([
        [_chunk(reasoning="这题得"), _chunk(reasoning="先算乘法"),
         _chunk(tool_calls=[_tool_call_delta(call_id="c1", name="calculator",
                                             arguments='{"expression": "2*3"}')])],
        [_chunk(content="等于六")],
    ])

    frames, steps = _run(tools=_TOOLS, trace_out=trace_out)
    types = [f["type"] for f in frames]

    thinking_idx = [i for i, t in enumerate(types) if t == se.FRAME_THINKING]
    tool_call_idx = types.index(se.FRAME_TOOL_CALL)
    assert thinking_idx, f"没发出任何思考帧：{types}"
    assert max(thinking_idx) < tool_call_idx, \
        f"思考必须排在工具帧之前：{[(f['type'], f.get('text', f.get('name'))) for f in frames]}"
    joined = "".join(f["text"] for f in frames if f["type"] == se.FRAME_THINKING)
    assert joined == "这题得先算乘法", joined
    assert steps[0]["kind"] == "thinking" and steps[0]["text"] == "这题得先算乘法", steps


# ---------- 3. 防洪：逐字思考合并成段，不一字一帧 ----------

def test_thinking_is_batched_not_one_frame_per_token(real_engine):
    """上游一个 token 一个 token 来，不合并就一秒几十帧把连接刷爆。

    这条把"按字数合并"写成判据：喂 500 个单字 delta，帧数必须是 O(500/120) 而不是
    O(500)，且拼回去一个字不少。THINKING_FLUSH_SECONDS 只是"面板别长时间没动静"的
    下限，测试不为它去 sleep——它由上面那条的"本轮读完必冲光"从结果上保证。
    """
    trace_out = []
    reasoning_deltas = [_chunk(reasoning="字") for _ in range(500)]
    real_engine([reasoning_deltas + [_chunk(content="完了")], []])

    frames, steps = _run(tools=None, trace_out=trace_out)
    thinking = [f for f in frames if f["type"] == se.FRAME_THINKING]

    assert len(thinking) > 1, "思考帧被合并成了一整块，连接还是会一次性收到 500 字"
    assert len(thinking) <= 8, f"没做防洪，发了 {len(thinking)} 帧"
    assert all(len(f["text"]) <= se.THINKING_FLUSH_CHARS * 2 for f in thinking)
    assert "".join(f["text"] for f in thinking) == "字" * 500


# ---------- 4. 单轮思考封顶：超了就停，绝不升级成故障 ----------

def test_thinking_capped_per_round_never_raises(real_engine):
    """展示用的思考有上限（THINKING_TOTAL_MAX），超了就停止外发而不是抛异常。

    为封顶去抛异常 = 把一个展示问题升级成整轮失败。超过上限之后，思考不再进帧，
    但正文照流、流程照走。这里喂 1.1 倍上限，断恰好停在 8000，且没有异常。
    """
    n = se.THINKING_TOTAL_MAX + 1000
    batch = 100
    reasoning_deltas = [_chunk(reasoning="思" * batch)
                        for _ in range(n // batch)]
    real_engine([reasoning_deltas + [_chunk(content="收尾")], []])

    trace_out = []
    frames, _ = _run(tools=None, trace_out=trace_out)
    total = sum(len(f["text"]) for f in frames if f["type"] == se.FRAME_THINKING)
    content = "".join(f["text"] for f in frames if f["type"] == se.FRAME_CONTENT)

    assert total == se.THINKING_TOTAL_MAX, f"封顶没停在预算上：{total}"
    assert content == "收尾", "封顶不该把正文也一起掐掉"


# ---------- 5. 工具帧：执行前发出，参数结构化，带服务端算出的中文标题 ----------

def test_tool_call_frame_before_execution_with_label_and_args(real_engine):
    """用户要看到"它现在要去算什么"——等算完再发就失去意义。

    锁定三件事：tool_call 帧在 tool_result 帧之前；arguments 是结构化的 dict（客户端
    按行渲染最好看）；label 是服务端算的一句人话（两个客户端不各排一遍）。同时
    tool_result 帧带 ok/elapsed_ms/truncated，并留一份 step_tool。
    """
    trace_out = []
    real_engine([
        [_chunk(tool_calls=[_tool_call_delta(call_id="c_9", name="calculator",
                                             arguments='{"expression": "17*24+12"}')])],
        [_chunk(content="420")],
    ])

    frames, steps = _run(tools=_TOOLS, trace_out=trace_out)
    call = next(f for f in frames if f["type"] == se.FRAME_TOOL_CALL)
    result = next(f for f in frames if f["type"] == se.FRAME_TOOL_RESULT)

    assert call["id"] == "c_9" and call["name"] == "calculator"
    assert call["arguments"] == {"expression": "17*24+12"}, call
    assert call["label"] == "计算 17*24+12", call
    assert result["ok"] is True and result["id"] == "c_9"
    assert result["elapsed_ms"] >= 0 and result["truncated"] is False
    assert "420" in result["summary"]

    tool_step = next(s for s in steps if s["kind"] == "tool")
    assert tool_step["label"] == "计算 17*24+12" and tool_step["ok"] is True, tool_step


# ---------- 6. 参数不是合法 JSON：退回 arguments_text，且这一帧仍有用 ----------

def test_non_json_arguments_fall_back_to_text_and_result_fails(real_engine):
    """模型吐出残缺 JSON 参数很常见。参数不是 JSON 这件事本身就该显示出来。

    tool_call 帧带 arguments_text（原样），而不是硬凑一个 dict；tool_result 帧 ok=False
    并带一句原因；流不能因此断掉（下面还有第二轮正文）。
    """
    trace_out = []
    real_engine([
        [_chunk(tool_calls=[_tool_call_delta(call_id="c_bad", name="calculator",
                                             arguments='{"expression": "1+')])],  # 缺收尾
        [_chunk(content="我换个写法")],
    ])

    frames, _ = _run(tools=_TOOLS, trace_out=trace_out)
    call = next(f for f in frames if f["type"] == se.FRAME_TOOL_CALL)
    result = next(f for f in frames if f["type"] == se.FRAME_TOOL_RESULT)

    assert "arguments" not in call and "arguments_text" in call, call
    assert result["ok"] is False, result
    content = "".join(f["text"] for f in frames if f["type"] == se.FRAME_CONTENT)
    assert content == "我换个写法", "坏参数不该把整条流打断"


# ---------- 7. 一次搜索的网页命中 → search 帧，把 URL 还给人 ----------

def test_web_search_artifacts_become_search_frame(real_engine, monkeypatch):
    """以前搜索的 {title,url,snippet} 被压成给模型的文本之后就丢了，用户永远看不见
    "它到底搜了哪些网页"，也没法核对来源。artifacts 走另一条通道进过程面板。

    打桩搜索源（CI 无公网），断 search 帧带 query 与命中行，并留一份 step_search。
    """
    from app.tools import web_search as search_source

    rows = [{"title": "西安 百科", "url": "https://baike.example/xian",
             "snippet": "常住人口一千多万"},
            {"title": "必打卡景点", "url": "https://zhihu.example/p/1",
             "snippet": "给我一天还你千年"}]
    monkeypatch.setattr(search_source, "search", lambda q, **k: rows)

    trace_out = []
    real_engine([
        [_chunk(tool_calls=[_tool_call_delta(call_id="c_s", name="web_search",
                                             arguments='{"query": "西安 人口"}')])],
        [_chunk(content="查到了")],
    ])

    frames, steps = _run(tools=_TOOLS, trace_out=trace_out)
    sframe = next(f for f in frames if f["type"] == se.FRAME_SEARCH)
    assert sframe["query"] == "西安 人口"
    assert [r["url"] for r in sframe["results"]] == ["https://baike.example/xian",
                                                     "https://zhihu.example/p/1"]
    search_step = next(s for s in steps if s["kind"] == "search")
    assert search_step["results"][0]["title"] == "西安 百科", search_step


# ---------- 8. 危险协议在服务端先抹平：URL 不出 http(s) ----------

def test_unsafe_urls_are_blanked_in_search_frame():
    """搜索结果里的 URL 出自别人写的网页。`javascript:` 或 `data:` 一旦被客户端当成
    链接，就等于把第三方内容升格成可执行入口——这一层必须在服务端挡掉。

    直接打契约层的 search_frame：危险协议变空串、https 原样保留。两个客户端少犯一次
    错的概率，比"各自记得校验"高，所以判据钉在这里而不是各端。
    """
    frame = se.search_frame("id1", "q", [
        {"title": "会执行的链接", "url": "javascript:alert(1)", "snippet": "s"},
        {"title": "内联数据", "url": "data:text/html,<script>", "snippet": "s"},
        {"title": "正常来源", "url": "https://ok.example/a", "snippet": "s"},
    ])
    urls = {r["title"]: r["url"] for r in frame["results"]}

    assert urls["会执行的链接"] == "", urls
    assert urls["内联数据"] == "", urls
    assert urls["正常来源"] == "https://ok.example/a", urls


# ---------- 9. 工具真炸了：折成 ok=False 的 ToolOutcome，流照走 ----------

def test_raising_tool_yields_failed_result_without_breaking_stream(real_engine, monkeypatch):
    """工具内部任何异常都只该影响这一次调用，不该把整条 SSE 打断。

    那正是 _run_tool_detailed 外层兜底的职责：execute_tool_detailed 万一抛出来，也要
    折成 ok=False 的 ToolOutcome（而不是向上炸）。这里故意把执行器换成会抛异常的桩，
    验 tool_result 帧 ok=False、流仍走到第二轮正文。
    """
    def boom(name, args, user_id=None):
        raise RuntimeError("沙箱没了")

    monkeypatch.setattr(streaming, "execute_tool_detailed", boom)

    trace_out = []
    real_engine([
        [_chunk(tool_calls=[_tool_call_delta(call_id="c_e", name="calculator",
                                             arguments='{"expression": "1+1"}')])],
        [_chunk(content="工具不可用，我口算")],
    ])

    frames, steps = _run(tools=_TOOLS, trace_out=trace_out)
    result = next(f for f in frames if f["type"] == se.FRAME_TOOL_RESULT)
    content = "".join(f["text"] for f in frames if f["type"] == se.FRAME_CONTENT)

    assert result["ok"] is False, result
    assert content == "工具不可用，我口算", content
    assert next(s for s in steps if s["kind"] == "tool")["ok"] is False


# ---------- 10. compact_trace：封顶步数、至少留住首尾 ----------

def test_compact_trace_caps_steps_and_never_empties():
    """落盘的 trace 是一条能无凭据一直涨的磁盘文件，必须封顶步数与字数；
    但被裁成"什么都不剩"就失去了"留痕"的意义，所以首尾两条一定留住。

    喂远超上限的步，结果里的真实步必须 <=TRACE_STEPS_MAX 且带一条 omitted 记账；再喂一
    堆超字数预算的长步，验证裁到贴着预算为止、但绝不裁到少于两条（首尾留不住是次要的，
    "什么都不剩"才是这条要堵的）。
    """
    many = [se.step_tool(f"id{i}", "calculator", f"调用 {i}", True, "x")
            for i in range(se.TRACE_STEPS_MAX + 60)]
    compacted = se.compact_trace(many)
    real_steps = [s for s in compacted if s.get("kind") != "omitted"]
    assert len(real_steps) <= se.TRACE_STEPS_MAX, len(real_steps)
    assert any(s.get("kind") == "omitted" for s in compacted), "被丢的步没记账"

    big = "长" * 4000
    heavy = [se.step_thinking(big) for _ in range(10)]
    kept = se.compact_trace(heavy)
    real = [s for s in kept if s.get("kind") != "omitted"]
    assert len(real) >= 2, f"一份 trace 至少留住两条，实得 {len(real)} 条"
    assert se._trace_chars(real) <= se.TRACE_CHARS_MAX or len(real) == 2, len(real)
    assert all(s["kind"] == "thinking" for s in real), real


# ---------- 11. 端点全链路：SSE 上出过程帧，落盘 trace 能回看 ----------

def test_sse_endpoint_emits_process_frames_and_persists_trace(real_engine):
    """真用户走的是 /v1/chat/stream，不是引擎函数。这一条钉端点接线：

    - thinking/tool_call/tool_result 这些被 stream_chat 文本过滤器吃掉的帧，必须靠
      ambient sink（begin_trace/drain_frames）补发进 run 缓冲、原样上屏；
    - 助手消息落盘时带 trace（回看时才有来历），done 帧也带 trace；
    - 旧的 content/done 骨架不受影响（老客户端只认 content/done）。
    """
    real_engine([
        [_chunk(reasoning="先乘后加"),
         _chunk(tool_calls=[_tool_call_delta(call_id="c_1", name="calculator",
                                             arguments='{"expression": "17*24+12"}')])],
        [_chunk(content="答案是 420")],
    ])

    sid = client.post("/v1/sessions?model=fake-chat").json()["session_id"]
    res = client.post("/v1/chat/stream", json={
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "算 17×24+12"}],
        "session_id": sid,
    })

    assert res.status_code == 200, res.text
    body = res.text
    assert '"type": "thinking"' in body, body[:400]
    assert '"type": "tool_call"' in body, body[:400]
    assert '"type": "tool_result"' in body, body[:400]
    assert '"type": "content"' in body, "正文骨架不许被动到（老客户端只认它）"
    assert '"type": "done"' in body, body[-400:]

    messages = client.get(f"/v1/sessions/{sid}").json()["messages"]
    assistant = [m for m in messages if m["role"] == "assistant"][-1]
    assert assistant["content"] == "答案是 420"
    kinds = [s["kind"] for s in assistant.get("trace", [])]
    assert "thinking" in kinds and "tool" in kinds, assistant.get("trace")

    client.delete(f"/v1/sessions/{sid}")
