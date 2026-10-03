"""真 SDK → 真 HTTP → 真端点 → 真磁盘的过程留痕端到端锁（v0.29.0）。

test_streaming_events.py 验的是引擎产帧的形状，test_process_trace_contract.py 验的
是三端源码一致；中间那段"SSE 行到底长什么样、帧序对不对、trace 到底落没落盘"以前
只有接了真模型才走得通——而最要命的一段恰恰在这里：**openai SDK 会不会把上游
delta 里的 reasoning_content 交给读侧**。它要是吞了这个键，单测里那些
SimpleNamespace 假块全都照样绿，线上一接真模型就永远没有思考过程。这一份用真 SDK
读真 HTTP，就是为了让那种"测试绿、产品瞎"的坏法当场红。

做法沿用 test_e2e_tool_loop.py：本地起 OpenAI 兼容 mock 上游，把 conftest 盖住的
替身逐个掀开，跑装配后的真 app、真 SDK、真 HTTP、真会话文件；搜索源打桩（这条链
要验的是命中结果怎么进帧与磁盘，不是第三方 API 此刻好不好用）。

回退必红自查（①②④ 实测过：把 mock 上游的 reasoning_content 三片删掉，这三条一起红，
③不受影响——它验的是另一道闸）：
- 上游不再给 reasoning_content、或 SDK/读侧把它吞了 → ①②④ 红；
- 把端点里 drain_frames 的补发时机改到正文之后 → ②红（thinking 排到 content 之后）；
- 把 search_frame 的 _safe_url 改成原样放行 → ③红（javascript: 上了线）；
- 把 add_message 的 trace 参数删掉 → ④红（磁盘上那份没了）。
"""
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core import stream_events as se
from app.core.providers import build_client as real_build_client
from app.core.providers import store as provider_store

# 够长的一段"思考"：越过 THINKING_FLUSH_CHARS，才逼得出轮内 flush（而不是只在轮末
# 一次性吐出来）——那样断言的就是分批，不是"至少发了一次"。
THINK_R1 = "先把它拆成两半：哪一半要外部资料，哪一半能自己算。" * 6
THINK_R2 = "资料到手，逐条核对标题与来源再落笔，不相干的那几条不用。" * 6

QUERY = "Fenver 发布记录"
ANSWER = "已核对来源，答案在这里。"

# 一条正常命中 + 一条恶意 url 的命中：后者必须原样留在帧里（标题是要给人读的），
# 但 url 必须被裁成空串——这条断言防的是"服务端那道闸哪天被人顺手删了"。
SEARCH_ROWS = [
    {"title": "Fenver v0.29 发布记录", "url": "https://fenever.example/releases/29",
     "snippet": "过程留痕上线：思考、工具、搜索都能看见。"},
    {"title": "诱导标题", "url": "javascript:alert(1)", "snippet": "这条网址不该以链接形态出门。"},
]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        streamed = bool(body.get("stream"))
        tool_results = [m for m in body.get("messages", []) if m.get("role") == "tool"]
        if streamed:
            self._sse(bool(tool_results))
        else:
            self._json(tool_results)

    def _json(self, tool_results):
        if tool_results:
            content = ANSWER
            self._send({"choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant",
                                                 "content": content}}]})
            return
        # 非流式那一趟也要真的走一次工具：⑤ 验的是"兜底路径的过程还在不在"，
        # 上游不调工具的话这条断言就成了空话。
        self._send({"choices": [{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant", "content": None,
                                             "tool_calls": [{
                         "id": "call_trace_1", "type": "function",
                         "function": {"name": "web_search",
                                      "arguments": json.dumps({"query": QUERY},
                                                              ensure_ascii=False)}}]}}]})

    def _send(self, out):
        out.update({"id": "cmpl-1", "object": "chat.completion", "model": "e2e-chat",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                              "total_tokens": 15}})
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _sse(self, has_result):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def ev(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        def chunk(delta, finish=None):
            ev({"id": "c", "object": "chat.completion.chunk", "model": "e2e-chat",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})

        chunk({"role": "assistant"})
        if has_result:
            # 第二轮：拿到搜索结果之后再想一次，然后给正文
            chunk({"reasoning_content": THINK_R2})
            chunk({"content": ANSWER})
            chunk({}, finish="stop")
        else:
            # 分两片到达：和真上游一样，思考文本不是一个整块
            half = len(THINK_R1) // 2
            chunk({"reasoning_content": THINK_R1[:half]})
            chunk({"reasoning_content": THINK_R1[half:]})
            args = json.dumps({"query": QUERY}, ensure_ascii=False)
            chunk({"tool_calls": [{"index": 0, "id": "call_trace_1", "type": "function",
                                   "function": {"name": "web_search",
                                                "arguments": ""}}]})
            chunk({"tool_calls": [{"index": 0,
                                   "function": {"arguments": args}}]})
            chunk({}, finish="tool_calls")
        ev({"id": "c", "object": "chat.completion.chunk", "model": "e2e-chat",
            "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                     "total_tokens": 15}})
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture(scope="module")
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


@pytest.fixture
def real_stack(upstream, monkeypatch):
    """掀开 conftest 的替身跑真链路，并把搜索源钉成 SEARCH_ROWS。

    收尾必须原样盖回去：那三枚替身是全套件的承重墙，带着真函数出门，下一条毫不
    相干的用例会去打网络然后超时，红到离真凶很远的地方。
    """
    import app.core.llm_client as llm_client
    import app.core.streaming as streaming
    import app.pipeline as pipeline
    import app.tools.availability as availability
    import app.tools.web_search as web_search

    fake_stream = streaming.stream_chat
    prev_default = next((p["id"] for p in provider_store.all()
                         if p.get("is_default")), None)
    provider_store.upsert({
        "id": "e2etrace", "label": "E2E 留痕上游", "base_url": upstream,
        "api_key": "sk-e2e-a1b2c3d4", "model": "e2e-chat", "is_default": True,
    })
    monkeypatch.setattr(pipeline, "build_client", real_build_client)
    monkeypatch.setattr(llm_client, "build_client", real_build_client)
    monkeypatch.setattr(web_search, "search", lambda query, max_results=None:
                        [dict(r) for r in SEARCH_ROWS])
    # 可用性只影响"要不要把工具列给模型"；这里模型是我们自己扮的，但清单也得给全，
    # 否则 mock 上游发的 tool_call 会撞在一个没注册的意图上。
    monkeypatch.setattr(availability, "search_reachable", lambda: True)
    importlib.reload(streaming)      # 模块级 import 时抓的替身要一并洗掉
    try:
        yield
    finally:
        importlib.reload(streaming)
        streaming.stream_chat = fake_stream
        if prev_default:
            provider_store.set_default(prev_default)
        provider_store.delete("e2etrace")


@pytest.fixture
def client(real_stack):
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _lines_to_frames(text):
    """线上字节 → 帧。顺带把"SSE 分帧"这条硬约束一起验了。"""
    frames = []
    for line in text.splitlines():
        if not line.strip():
            continue
        # id: 行是 R3 加的游标（旧客户端看不懂就跳过）；除此之外只许有 data: 行。
        assert line.startswith("data: ") or line.startswith("id: "), \
            f"出现了不属于 SSE 骨架的行，说明某帧里混进了裸换行：{line[:80]!r}"
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            continue
        frames.append(json.loads(payload))
    return frames


def _post_stream(client, session_id):
    r = client.post("/v1/chat/stream", json={
        "model": "e2e-chat",
        "messages": [{"role": "user", "content": "查一下 Fenver 的发布记录，再给结论"}],
        "session_id": session_id})
    assert r.status_code == 200, r.text[:200]
    return _lines_to_frames(r.text)


def test_reasoning_from_a_real_sdk_reaches_the_wire(client):
    """① 真 SDK 读到的 reasoning_content 必须变成 thinking 帧——否则产品是瞎的。"""
    sid = client.post("/v1/sessions?model=e2e-chat").json()["session_id"]
    try:
        frames = _post_stream(client, sid)
    finally:
        client.delete(f"/v1/sessions/{sid}")

    types = [f["type"] for f in frames]
    assert se.FRAME_THINKING in types, \
        f"一帧思考都没有：SDK 或读侧把 reasoning_content 吞了（帧序 {types}）"
    think = "".join(f["text"] for f in frames if f["type"] == se.FRAME_THINKING)
    assert THINK_R1 in think, f"第一轮思考不完整：{think[:200]}"
    assert all(len(f["text"]) <= se.THINKING_FLUSH_CHARS * 2
               for f in frames if f["type"] == se.FRAME_THINKING), \
        "单帧思考越过封顶：分批的那道闸没了"


def test_frames_arrive_in_the_order_they_happened(client):
    """② 帧序 = 发生序：想 → 要去搜 → 搜完了 → 结果 → 再想 → 正文 → done。"""
    sid = client.post("/v1/sessions?model=e2e-chat").json()["session_id"]
    try:
        frames = _post_stream(client, sid)
    finally:
        client.delete(f"/v1/sessions/{sid}")

    types = [f["type"] for f in frames]
    assert set(types) <= set(se.CLIENT_FRAMES), f"线上出现了客户端不认识的帧：{types}"
    assert types[0] == se.FRAME_START and types[-1] == se.FRAME_DONE, types[:3] + types[-3:]

    def first(t):
        return types.index(t) if t in types else -1

    call = first(se.FRAME_TOOL_CALL)
    assert call > -1, f"工具调用没上屏：{types}"
    think_before = [i for i, f in enumerate(frames)
                    if f["type"] == se.FRAME_THINKING and i < call]
    assert think_before, "第一轮思考排在 tool_call 之后：补发顺序被改坏了"
    result = first(se.FRAME_TOOL_RESULT)
    search = first(se.FRAME_SEARCH)
    content = first(se.FRAME_CONTENT)
    assert call < result < search < content, \
        f"帧序乱了：tool_call={call} tool_result={result} search={search} content={content}"

    call_id = frames[call]["id"]
    assert frames[result]["id"] == call_id and frames[search]["id"] == call_id, \
        "开始与回执配不上对：客户端无法把一步的两侧接起来"
    assert frames[call]["name"] == "web_search"
    assert frames[call].get("label"), "服务端没算那一行收起态文案（两个客户端各排一次是错的）"
    assert frames[call]["arguments"] == {"query": QUERY}, frames[call]
    assert frames[result]["ok"] is True, frames[result]["summary"]

    body = "".join(f["text"] for f in frames if f["type"] == se.FRAME_CONTENT)
    assert body == ANSWER, body[:120]


def test_a_third_party_url_never_leaves_as_a_link(client):
    """③ 搜索命中的网址在出厂前过闸：非 http(s) 一律裁成空串，标题照留。"""
    sid = client.post("/v1/sessions?model=e2e-chat").json()["session_id"]
    try:
        frames = _post_stream(client, sid)
    finally:
        client.delete(f"/v1/sessions/{sid}")

    search = next(f for f in frames if f["type"] == se.FRAME_SEARCH)
    assert search["query"] == QUERY, search
    rows = search["results"]
    assert len(rows) == len(SEARCH_ROWS), f"命中条数变了：{rows}"
    assert rows[0]["url"] == SEARCH_ROWS[0]["url"], rows[0]
    assert rows[1]["url"] == "", f"危险协议漏到线上了：{rows[1]}"
    assert rows[1]["title"] == SEARCH_ROWS[1]["title"], "裁网址不该顺手把标题也删了"


def test_the_trace_on_the_wire_is_the_trace_on_disk(client):
    """④ done 带的那份留痕与会话里存的那份必须同源同序。"""
    sid = client.post("/v1/sessions?model=e2e-chat").json()["session_id"]
    try:
        frames = _post_stream(client, sid)
        done = next(f for f in frames if f["type"] == se.FRAME_DONE)
        assert done.get("status") == "completed", done
        wire_trace = done.get("trace")
        assert wire_trace, "done 没带 trace：断线刷新之后过程就没了来历"

        got = client.get(f"/v1/sessions/{sid}").json()
        messages = got.get("data", got).get("messages", [])
        assert [m["role"] for m in messages] == ["user", "assistant"], messages
        stored = messages[1]
        assert stored["content"] == ANSWER
        assert stored.get("message_id") == done["message_id"], \
            "落盘消息与 done 的 message_id 对不上：反馈无法关联"
        disk_trace = stored.get("trace")
        assert disk_trace, "会话磁盘上没有 trace：过程只活在连接里"
        assert [s["kind"] for s in disk_trace] == [s["kind"] for s in wire_trace], \
            (disk_trace, wire_trace)
        kinds = {s["kind"] for s in disk_trace}
        assert {"thinking", "tool", "search"} <= kinds, kinds
        tool_step = next(s for s in disk_trace if s["kind"] == "tool")
        assert tool_step["ok"] is True and tool_step["id"]
        search_step = next(s for s in disk_trace if s["kind"] == "search")
        assert search_step["results"][0]["url"] == SEARCH_ROWS[0]["url"]
        assert search_step["results"][1]["url"] == ""
    finally:
        client.delete(f"/v1/sessions/{sid}")


def test_the_non_stream_fallback_keeps_process_steps_but_not_thinking(client):
    """⑤ 流断之后走非流式那次兜底：工具与搜索照留，思考照没有——两边都得是真的。

    更新说明里那句"兜底时过程面板仍有工具调用与搜索命中，但没有思考文本"以前只是
    推断：非流式那条走的是 pipeline._call_model_with_tool_loop，与流式两条不同的路，
    共享的只有 compact_trace 那一层。这里把它钉成断言，下一个只改一条路的人就会红。
    """
    sid = client.post("/v1/sessions?model=e2e-chat").json()["session_id"]
    try:
        r = client.post("/v1/chat", json={
            "model": "e2e-chat",
            "messages": [{"role": "user", "content": "查一下 Fenver 的发布记录"}],
            "session_id": sid})
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert body["reply"] == ANSWER, body["reply"][:120]
        trace = body.get("trace")
        assert trace, "非流式兜底把过程整个丢了：面板在这一趟上是空的"
        kinds = [s["kind"] for s in trace]
        assert "thinking" not in kinds, \
            f"非流式响应里冒出了思考步：那份文本不是上游给的（{kinds}）"
        assert {"tool", "search"} <= set(kinds), kinds
        tool_step = next(s for s in trace if s["kind"] == "tool")
        assert tool_step["ok"] is True and tool_step["name"] == "web_search"
        search_step = next(s for s in trace if s["kind"] == "search")
        assert search_step["query"] == QUERY, search_step
        assert search_step["results"][1]["url"] == "", \
            f"兜底路径绕过了网址那道闸：{search_step['results'][1]}"

        got = client.get(f"/v1/sessions/{sid}").json()
        messages = got.get("data", got).get("messages", [])
        assert [m["role"] for m in messages] == ["user", "assistant"], messages
        assert messages[1].get("trace") == trace, \
            "兜底路径返回的过程与落盘的过程不是同一份"
    finally:
        client.delete(f"/v1/sessions/{sid}")
