"""思考帧必须在"想到的那一刻"上线，而不是等正文把它带出来（v0.29.2）。

用户实测到的两件事是同一个 bug：
  「为什么要等半天，思考过程和答案一起蹦出来，那万一一个任务很复杂处理的时间
    非常长，我在不知道它思考的情况下就停止生成了怎么办。」

以前有两处会让思考迟到：
  1) 端点走 stream_chat（文本过滤器），过程帧只留在本线程 sink 里，端点每吐出
     一段正文才 drain 一次——只想不说的整段时间里一个字节都不上屏；
  2) 时间触发（THINKING_FLUSH_SECONDS）写在正文分支里，于是"上游一个字一个字
     地想、每片都不够 THINKING_FLUSH_CHARS"时，两条触发一条都不满足。

本文件的锁是**时序**锁，不是顺序锁：上游发完思考之后把连接冻住（阻塞在一个
Event 上），只有读者真的在线上看到了 thinking 帧才会把它解开。
回退必红自查（三条都实测过，不是推测）：
- 把 main.py 端点改回"过程帧先攒着、等正文到了再补发" → ①② 红（冻到超时）。同时
  test_e2e_process_trace.py 五条**全绿**——这就是为什么光有顺序锁不够；
- 把 streaming.py 思考分支里的时间触发条件删掉，只留字数触发 → ① 红（②仍绿）；
- 把字数触发那条判断改成恒假 → ② 红（①仍绿，两条触发各自有主）。
③ 数的是帧数，不看时刻，所以它对"迟到"免疫：它防的是另一个方向（把阈值改成 1 字
把连接刷爆），那种改法 ①② 都看不出来。

为了让"读者中途读到的"真的是中途，这里走真 TCP：起一个 uvicorn 端口，用
http.client 逐行读，不经过任何测试客户端的缓冲语义。

诚实边界（写在这里，免得下一个人把 ① 改成"上游完全不出字"然后困惑）：时间触发的
检查点在**读到下一片 delta 的那一刻**。连接彻底静默时读侧是阻塞的，谁也冲不出缓冲
里那几个字——所以 ① 的碎片之间留了一个节流周期以上的间隔，那才是真上游的形状。
唯一真正长的静默是"模型决定去调工具、工具在跑"，那一段由 tool_call 帧负责：它在
工具执行**之前**就发出去（streaming.py 的轮末 flush 也在它之前）。
"""
import importlib
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.core import stream_events as se
from app.core.providers import build_client as real_build_client
from app.core.providers import store as provider_store

# ①用的一小段思考：越不过 THINKING_FLUSH_CHARS，只能靠时间触发上路——正是要验的那条。
SMALL_THINK = "先把问题拆开：哪一半要查资料。"
assert len(SMALL_THINK) < se.THINKING_FLUSH_CHARS, "样本失效：这段思考本就该走字数触发"
# ②用的一整段思考：一次到位越过字数触发，且**不给它任何时间触发可用的间隔**。
BIG_THINK = ("逐条核对来源与标题，不相干的那几条不用；剩下的按时间排一遍，"
             "再把结论写成一句话。") * 4
assert len(BIG_THINK) >= se.THINKING_FLUSH_CHARS, "样本失效：这段思考本就该走时间触发"

# 冻住上游的等待上限：时间触发是 0.4 秒，给足真机抖动余量，仍远短于用户抱怨的"半天"。
GATE_SECONDS = 20.0
# 读者判定"迟到"的红线：比时间触发宽 enough 容纳调度，比任何等正文的实现严。
LIVE_CEILING_SECONDS = 3.0
# ①里思考碎片之间的间隔：比节流窗口长一点，时间触发才有东西可冲。
THINK_GAP_SECONDS = se.THINKING_FLUSH_SECONDS + 0.2

mode = {"kind": "small"}
gate_open = threading.Event()      # 读者在线上看见了 thinking 帧
upstream_frozen = threading.Event()  # 上游确实把连接冻住了（放行之前不许置位）


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()

        def chunk(delta, finish=None):
            self.wfile.write(b"data: " + json.dumps({
                "id": "c", "object": "chat.completion.chunk", "model": "e2e-live",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }).encode() + b"\n\n")
            self.wfile.flush()

        chunk({"role": "assistant"})
        if mode["kind"] == "small":
            # 分三片、片间隔一个节流周期以上：真实上游就是这样一个 token 一个 token
            # 往外蹦的。这个间隔是给时间触发留的活口——见文件头"诚实边界"。
            n = len(SMALL_THINK) // 3
            for i, piece in enumerate((SMALL_THINK[:n], SMALL_THINK[n:2 * n],
                                       SMALL_THINK[2 * n:])):
                if i:
                    time.sleep(THINK_GAP_SECONDS)
                chunk({"reasoning_content": piece})
        else:
            chunk({"reasoning_content": BIG_THINK})

        # 关键一步：把连接冻在这里。只有读者真的把 thinking 帧收下了才放行——
        # 冻住的这段时间正是用户过去看到的"等半天"，区别是这次它必须被证明很短。
        upstream_frozen.set()
        gate_open.wait(timeout=GATE_SECONDS)
        chunk({"content": "答案在这里。"})
        chunk({}, finish="stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture(scope="module")
def upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


@pytest.fixture
def real_stack(upstream, monkeypatch):
    """掀开 conftest 的替身跑真链路；收尾必须原样盖回去，理由同 test_e2e_process_trace。"""
    import app.core.llm_client as llm_client
    import app.core.streaming as streaming
    import app.pipeline as pipeline

    prev_default = next((p["id"] for p in provider_store.all()
                         if p.get("is_default")), None)
    provider_store.upsert({
        "id": "e2elive", "label": "E2E 实时留痕上游", "base_url": upstream,
        "api_key": "sk-e2e-a1b2c3d4", "model": "e2e-live", "is_default": True,
    })
    monkeypatch.setattr(pipeline, "build_client", real_build_client)
    monkeypatch.setattr(llm_client, "build_client", real_build_client)
    importlib.reload(streaming)      # 模块级 import 时抓的替身要一并洗掉
    try:
        yield
    finally:
        importlib.reload(streaming)
        if prev_default:
            provider_store.set_default(prev_default)
        provider_store.delete("e2elive")


@pytest.fixture
def live_server(real_stack):
    """真 uvicorn + 真 TCP：读者要能在整条流结束之前就读到帧。"""
    import uvicorn
    from app.main import app as fastapi_app

    class _Server(uvicorn.Server):
        def install_signal_handlers(self):
            pass

    # 端口自己先占一个再放开：uvicorn 收到 port=0 不会把随机端口写回 cfg.port，
    # 那样读者连的是 0 端口，红会红成"地址无效"，离真凶十万八千里。
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    # log_config=None **挡不住**这件事：实测 uvicorn.Server.run() 一起来，
    # uvicorn / uvicorn.access 两个 logger 的 propagate 就被置成 False（默认日志配置
    # 是 dictConfig 里写死的 "propagate": false）。caplog 的抓手挂在 root 上，
    # propagate 一断，跑在后面的 test_log_sanitizer 就收到零条记录——红成"脱敏坏了"，
    # 而真凶在另一个文件里。所以这里不是"配一个安静点的 log_config"，而是**进出各存
    # 一份还原一份**：起服务是借用全局日志状态，用完必须原样还回去。
    watched = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {n: (logging.getLogger(n).propagate, list(logging.getLogger(n).handlers),
                 logging.getLogger(n).level) for n in watched}
    cfg = uvicorn.Config(fastapi_app, host="127.0.0.1", port=port, log_level="warning",
                         log_config=None, access_log=False)
    server = _Server(cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(400):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn 没起来：这条锁就成了空话"
    try:
        yield port
    finally:
        server.should_exit = True
        gate_open.set()
        thread.join(timeout=15)
        for n, (prop, handlers, level) in saved.items():
            lg = logging.getLogger(n)
            lg.propagate, lg.level = prop, level
            lg.handlers[:] = handlers


def _connect(port):
    from http.client import HTTPConnection
    return HTTPConnection("127.0.0.1", port, timeout=GATE_SECONDS + 20)


def _new_session(port):
    conn = _connect(port)
    conn.request("POST", "/v1/sessions?model=e2e-live")
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    sid = body.get("session_id")
    assert sid, f"建会话失败：{body}"
    return sid


def _read_stream_live(port, session_id):
    """发起流式请求并逐行读；返回 [(帧类型, 相对到达秒, 帧)]，读到思考帧立刻放行上游。"""
    body = json.dumps({
        "model": "e2e-live",
        "messages": [{"role": "user", "content": "先把问题拆开再回答"}],
        "session_id": session_id,
    }).encode()
    conn = _connect(port)
    conn.putrequest("POST", "/v1/chat/stream")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Accept", "text/event-stream")
    conn.putheader("Content-Length", str(len(body)))
    conn.endheaders()
    conn.send(body)
    resp = conn.getresponse()
    assert resp.status == 200, f"端点回了 {resp.status}：这条路根本没走通"
    seen = []
    t0 = time.monotonic()
    try:
        while True:
            line = resp.readline()   # 逐行读：SSE 一帧一行 data:，块与块之间不攒缓冲
            if not line:
                break
            if not line.startswith(b"data: "):
                continue
            payload = line[6:]
            if payload == b"[DONE]":
                break
            frame = json.loads(payload)
            ftype = frame.get("type")
            seen.append((ftype, time.monotonic() - t0, frame))
            if ftype == se.FRAME_THINKING:
                gate_open.set()   # 读者收到了：允许上游继续
    finally:
        conn.close()
    return seen


def _run_live_case(port, kind):
    mode["kind"] = kind
    gate_open.clear()
    upstream_frozen.clear()
    sid = _new_session(port)
    try:
        return _read_stream_live(port, sid)
    finally:
        mode["kind"] = "small"
        try:
            conn = _connect(port)
            conn.request("DELETE", f"/v1/sessions/{sid}")
            conn.getresponse().read()
            conn.close()
        except OSError:
            pass


def test_time_triggered_thinking_reaches_the_wire_while_upstream_is_frozen(live_server):
    """① 碎片式思考（够不上字数触发）也必须在上游冻住之前上线：时间触发在场且即时转发。"""
    seen = _run_live_case(live_server, "small")
    types = [t for t, _, _ in seen]
    assert se.FRAME_THINKING in types, (
        f"读到流结束都没有思考帧：端点又回到按正文节奏补发，或时间触发离开了思考分支（{types}）")
    first_at = next(t for ty, t, _ in seen if ty == se.FRAME_THINKING)
    assert first_at < LIVE_CEILING_SECONDS, \
        f"第一帧思考迟到 {first_at:.2f}s：它等的是正文，不是自己"
    assert upstream_frozen.is_set(), "上游没被冻住过：这条断言什么都没测"
    content_at = [t for ty, t, _ in seen if ty == se.FRAME_CONTENT]
    assert content_at and first_at < min(content_at), \
        "思考帧没有排在正文之前：面板会先出答案再倒着想过的话"


def test_char_triggered_thinking_reaches_the_wire_while_upstream_is_frozen(live_server):
    """② 一整段思考（越过字数触发）同样不许等正文：批成一段可以，攒到正文不行。"""
    seen = _run_live_case(live_server, "big")
    think = [(t, f) for ty, t, f in seen if ty == se.FRAME_THINKING]
    assert think, f"整段思考没上屏：{[t for t, _, _ in seen]}"
    assert think[0][0] < LIVE_CEILING_SECONDS, (
        f"第一帧思考迟到 {think[0][0]:.2f}s：字数触发被挪走了或端点不再即产即发")
    assert upstream_frozen.is_set(), "上游没被冻住过：这条断言什么都没测"
    # 迟到修好了不许顺手把内容改坏：拼回来必须还是上游给的那句话，且不超单轮封顶
    joined = "".join(f["text"] for _, f in think)
    assert joined == BIG_THINK[:len(joined)] and joined, f"思考文本被截断了：{joined[:80]}"
    assert len(joined) <= se.THINKING_TOTAL_MAX


def test_small_pieces_are_batched_not_spammed_one_frame_per_character(live_server):
    """③ 即时不等于逐字刷屏：三片思考到的帧数仍要落在封顶骨架里。

    这一条守的是"为了实时把 flush 阈值改成 1 字"这种反向回退——那会把连接刷爆，
    而 ①② 都看不出问题。
    """
    seen = _run_live_case(live_server, "small")
    think = [f for ty, _, f in seen if ty == se.FRAME_THINKING]
    # 即时不等于逐字刷屏：三片思考最多三帧。这条守的是"为了实时把阈值改成 1 字"
    # 那种反向回退——①② 都看不出它把连接刷爆。
    assert 0 < len(think) <= 3, [len(f["text"]) for f in think]
    assert all(len(f["text"]) <= se.THINKING_FLUSH_CHARS * 2 for f in think), \
        [len(f["text"]) for f in think]

