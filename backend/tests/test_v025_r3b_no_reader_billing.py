"""v0.25 R3b 行为契约：关掉页面不许继续开新的付费轮——断线的代价封顶在"已在飞行中的那一轮"。

为什么判据单独成一个文件而不是并进 test_v025_stream_cancel_contract：R3 把
"读者离开"和"生成停止"解耦了（续播的前提，钱账上是对的），副作用是一个只关页面
的客户端谁也没取消，工具循环却可以一轮接一轮地继续叫 create()——每一轮都是真金
白银。R3b 补的就是这一侧：新的付费轮开始前必须有活读者；宽限期（默认 30 秒，
config_store.stream_no_reader_grace_seconds / 环境变量 STREAM_NO_READER_GRACE_SECONDS）
内读者回来（含 Last-Event-ID 续播）一切照常；没回来就走**同一条**取消路径翻标志、
尽力关连接、半句在落盘闩下存会话、账照常结清——不另起第二套"超时"语义。

诚实的边界（与实现处的注释同一句话，判据按这个钉，不多钉）：本机制封顶的是
"已经起飞的那一轮飞完并结账，新的轮次为零"。它**不**承诺断线即刻免费——
close() 一条正阻塞在 read 上的连接在这套 SDK 下不可证明地即时。所以这里的
数字判据是"create() 次数恰好是起飞的那几轮"，而不是"token 增量为 0"。

第二条红线是 R3b-3：今天两个客户端（api.js:160-185）只认 content/done/error，
其余帧**静默丢弃**；流若以一个看不懂的帧收尾，旧客户端会当作"没 done"→
retryable → 把整轮重发进 /v1/chat——R3 的 cancelled 终帧恰好落进这个洞里，
这正是要修的回归。因此：被停掉的流最后一帧**可读**的必须是带新字段
（status/stopped_reason/resumable/run_id/seq）的 done；cancelled 可以给升级后的
客户端留着放在 done 之前。HTTP 410 CANNOT_RESUME 豁免（旧客户端从不发
Last-Event-ID）。

负向验证（这两条摘掉哪条都会红在哪些判据上，实现前先记在这里）：
- 摘掉轮次的读者闸门（streaming.py 边界上的 before_round 调用点）：
  test_disconnect_without_reconnect_charges_no_new_round、
  test_grace_expiry_saves_partial_answer_into_session、
  test_grace_window_comes_from_config_file_and_env、
  test_stopped_streams_end_in_a_frame_old_clients_honor[expired] 全红——
  第二轮 create() 真的发生，账本多一行不算，会话里还多出一段没人看的"第二轮"。
- 让 cancelled 收尾时不再补 done 终帧：
  test_stopped_streams_end_in_a_frame_old_clients_honor 的
  [cancelled]/[expired]/[gap] 三条全红——旧客户端落回"没有 done ⇒ 重发整轮"。
"""
import importlib
import json
import threading
import time
import types

import pytest

from app.core import config_store, stream_runs, usage
from app.core.authz import Principal
from app.core.providers import store as provider_store
from app.main import launch_stream_run

BOOT = {"Authorization": "Bearer boot-token"}


# ---------- 测试替身：形状对齐 openai SDK（与 Lane B 同一套，注释见彼处） ----------

def _chunk(content=None, tool_calls=None):
    delta = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=delta, finish_reason=None)],
        usage=None)


def _usage_chunk(prompt=5, completion=7):
    return types.SimpleNamespace(
        choices=[],
        usage=types.SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion,
            total_tokens=prompt + completion,
            completion_tokens_details=None, prompt_tokens_details=None))


def _tool_call_chunk(call_id="call_1", name="calculator", arguments='{"expression": "1+1"}'):
    fn = types.SimpleNamespace(name=name, arguments=arguments)
    return _chunk(tool_calls=[types.SimpleNamespace(index=0, id=call_id, function=fn)])


class GatedStream:
    """前 free 块照常给，之后每拉一次卡 gate_seconds——用来把生产线程钉在"轮中"。

    close() 会把闸门立刻放开并抛 ConnectionAbortedError，与真机上 abort_upstream
    对阻塞读的效果同形；不 close 则每拉一次到点放行剩余块、耗尽后 StopIteration，
    也就是"这一轮自然飞完"。R3b 的判据靠这两种收尾分别钉「取消」与「轮已起飞」。
    """

    def __init__(self, chunks, free=2, gate_seconds=0.5):
        self._chunks = list(chunks)
        self._free = free
        self._pulled = 0
        self._cv = threading.Condition()
        self.closed = False
        self.gate_seconds = gate_seconds

    def __iter__(self):
        return self

    def __next__(self):
        if self._pulled < self._free and self._pulled < len(self._chunks):
            self._pulled += 1
            return self._chunks[self._pulled - 1]
        deadline = time.monotonic() + self.gate_seconds
        with self._cv:
            while not self.closed and time.monotonic() < deadline:
                self._cv.wait(0.05)
        if self.closed:
            raise ConnectionAbortedError("闸门：连接被关闭（取消/超时同一条路）")
        self._pulled += 1
        if self._pulled <= len(self._chunks):
            return self._chunks[self._pulled - 1]
        raise StopIteration

    def close(self):
        with self._cv:
            self.closed = True
            self._cv.notify_all()


class FakeCompletions:
    """每次 create() 消费一个预排的"轮"；弹药没了还来 = 不该发生的付费调用，当场炸。"""

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._rounds:
            raise AssertionError("不该发生的付费轮次发生了：没有读者/已取消之后仍发起了 create()")
        item = self._rounds.pop(0)
        return iter(item) if isinstance(item, list) else item


class FakeClient:
    def __init__(self, rounds):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(rounds))


# ---------- 夹具 ----------

@pytest.fixture(autouse=True)
def _clean_runs():
    stream_runs.clear_all_for_tests()
    yield
    stream_runs.clear_all_for_tests()


@pytest.fixture
def clean_usage(tmp_path, monkeypatch):
    prev_days, prev_path = usage._days, usage._PATH
    path = str(tmp_path / "usage.json")
    usage._days = {}
    usage._PATH = path
    try:
        yield path
    finally:
        usage._days, usage._PATH = prev_days, prev_path


@pytest.fixture
def real_stream(monkeypatch):
    """真 stream_chat，上游换成 FakeClient（reload 模式同 Lane B/test_stream_tools）。"""
    import app.core.streaming as streaming
    importlib.reload(streaming)
    holder = {}
    monkeypatch.setattr(streaming, "build_client", lambda provider: holder["client"])

    def install(rounds):
        client_ = FakeClient(rounds)
        holder["client"] = client_
        return client_.chat.completions

    yield install
    importlib.reload(streaming)


@pytest.fixture
def grace_env(monkeypatch):
    """把宽限期设成用例要的值（环境变量是既有配置通道里的最高优先级口径）。"""
    def set_grace(seconds):
        monkeypatch.setenv("STREAM_NO_READER_GRACE_SECONDS", str(seconds))
    return set_grace


@pytest.fixture
def config_file_grace(tmp_path, monkeypatch):
    """走 data/config.json 那条通道：写文件 → restore() 读回 → 用例后原样还回内存。"""
    prev = dict(config_store._config)
    path = tmp_path / "config.json"
    monkeypatch.delenv("STREAM_NO_READER_GRACE_SECONDS", raising=False)
    monkeypatch.setenv("CONFIG_DB_PATH", str(path))

    def write(values: dict):
        path.write_text(json.dumps(values), encoding="utf-8")
        config_store.restore()
        return config_store.stream_no_reader_grace_seconds()

    yield write
    with config_store._lock:
        config_store._config.clear()
        config_store._config.update(prev)


# ---------- 工具函数 ----------

def _frames(body: str):
    out = []
    for block in body.split("\n\n"):
        rid, data = None, None
        for line in block.split("\n"):
            if line.startswith("id: "):
                rid = line[4:].strip()
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                except ValueError:
                    data = None
        if data is not None:
            out.append((rid, data))
    return out


def _old_client_parse(payloads):
    """逐字复刻旧 api.js 的分发纪律（160-185 行）：只认 content/done/error。

    返回值就是旧客户端的行为分叉："done" 正常收尾；"error" 报错且不重发；
    "RESEND_WHOLE_TURN" —— 那是洞：finished 为空 → throw retryable=true →
    app.js:1588 把整轮重发进 /v1/chat，再付一次钱。R3b-3 的全部意义在
    任何被停掉的流都不许走到这个返回值上。
    """
    finished = None
    for evt in payloads:
        t = evt.get("type")
        if t == "content":
            continue
        if t == "done":
            finished = evt
        elif t == "error":
            return ("error", evt)
    if finished is None:
        return ("RESEND_WHOLE_TURN", None)
    return ("done", finished)


def _wait_until(pred, timeout=5.0, why=""):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    raise AssertionError(f"等待超时（{timeout}s）：{why}")


def _usage_row(user_id, provider_id="fake-model"):
    rows = [r for r in usage.snapshot()
            if r["user_id"] == user_id and r["provider_id"] == provider_id]
    assert len(rows) <= 1, f"同一人同一 provider 出现多行：{rows}"
    return rows[0] if rows else None


def _session_rows(http_client, sid):
    return http_client.get(f"/v1/sessions/{sid}", headers=BOOT).json()["messages"]


def _tool_pipe():
    return types.SimpleNamespace(
        tools_schema=[{"type": "function",
                       "function": {"name": "calculator", "description": "",
                                    "parameters": {"type": "object"}}}],
        save_interaction=lambda *a: None)


def _launch(run, user_text="帮我算"):
    """直启生产线程（不开 HTTP 首轮），时机确定——同 Lane B 的做法。"""
    return launch_stream_run(run, provider_store.resolve("fake-model"),
                             [{"role": "user", "content": user_text}], user_text,
                             Principal("default_user", "本机管理员", "admin"),
                             _tool_pipe(), [])


class _Reader:
    """用真 sse_frames 生成器握着一个"活读者"：next 进场即 attach，close 即 detach。

    读者计数必须经真路径进出——直接调 attach_reader/detach_reader 只能证明计数器
    会加减，证明不了"SSE 连接持有即在场、断开（含 GeneratorExit）即离场"。
    """

    def __init__(self, run, cursor=0):
        self.gen = stream_runs.sse_frames(run, cursor)
        self.exhausted = False

    def next_frame(self):
        try:
            return next(self.gen)
        except StopIteration:
            self.exhausted = True
            return None

    def drain_text(self):
        parts = []
        while True:
            f = self.next_frame()
            if f is None:
                return "".join(parts)
            parts.append(f)

    def close(self):
        self.gen.close()


def _expired_run(client, real_stream, sid, gate_seconds=0.5):
    """开一轮"读者中途离场、再也不回来"的流：第一轮起飞并结账，第二轮被闸门掐死。

    返回 (run, completions, t_detach)。宽限期由调用方通过 env/config 设好。
    """
    gated = GatedStream([_chunk(content="前半句"), _usage_chunk(5, 7), _tool_call_chunk()],
                        free=2, gate_seconds=gate_seconds)
    completions = real_stream([gated, [_chunk(content="不该出现的第二轮")]])
    run = stream_runs.create_run(user_id="default_user", session_id=sid)
    _launch(run)
    reader = _Reader(run)
    assert reader.next_frame() is not None, "首帧都没拿到，读者夹具坏了"
    _wait_until(lambda: run.last_seq >= 2, why="第一轮正文没进缓冲")
    t_detach = time.monotonic()
    reader.close()
    assert not run.has_reader(), "生成器 close 之后读者必须离场"
    _wait_until(lambda: run.finished, timeout=8.0, why="宽限到期后没有按取消收尾")
    return run, completions, t_detach


# ---------- 1. 主判据：断线不复连，钱只付到"已起飞的那一轮" ----------

def test_disconnect_without_reconnect_charges_no_new_round(client, real_stream,
                                                           clean_usage, grace_env):
    """判据的正身：关页面（不取消、不复连）→ 宽限后停在轮次边界。

    钉四个数：create() 次数=1（第二轮没发生）、账本 calls=1（只有起飞那轮结账）、
    tool_rounds=0（没有第二轮的钱）、total_tokens=12（取上游回传，不是估算）。
    """
    grace_env(0.2)
    sid = client.post("/v1/sessions", headers=BOOT).json()["session_id"]
    run, completions, t_detach = _expired_run(client, real_stream, sid)

    assert len(completions.calls) == 1, \
        f"没有读者仍发起了新付费轮：create() {len(completions.calls)} 次"
    assert run.status == "cancelled" and run.cancel_event.is_set(), \
        "宽限到期必须走既有取消路径，不另起第二套'超时'语义"
    row = _usage_row("default_user")
    assert row["calls"] == 1, f"账本增量必须恰好是起飞的那一轮：{row}"
    assert row["tool_rounds"] == 0, f"多出来的轮次被结了账：{row}"
    assert row["total_tokens"] == 12, f"结的是上游回传的真账：{row}"
    # 计时器是从 1→0 起算的：到点才停，不提前（提前=旧客户端没赶上重开窗口）
    assert time.monotonic() - t_detach >= 0.15, "窗格没等满就掐，grace 语义没钉住"

    # 停完之后任何后续步骤（这里用一次合法续播模拟"回来晚了"）不再动账
    before = usage.snapshot()
    late = client.post("/v1/chat/stream",
                       headers={**BOOT, "Last-Event-ID": f"{run.run_id}:1"},
                       json={"messages": []})
    assert late.status_code == 200, late.text
    after = usage.snapshot()
    assert sum(r["calls"] for r in after) == sum(r["calls"] for r in before), \
        "终态之后的续播重记了账"


# ---------- 2. 宽限内回来（含 Last-Event-ID 续播）= 取消计时器，一切照常 ----------

def test_reconnect_within_grace_replays_and_finishes_without_extra_charge(
        client, real_stream, clean_usage, grace_env):
    """断线重开的正路：续播本身算"读者回来了"——轮次边界放行第二轮，账不重复。

    钉的四个数与 R3 的"续播不加钱"同源而这里多钉一面：正因为续播重连把计时器
    取消了，第二轮是**该**发生的合法付费轮——calls 仍是 1（一条流一个 stream_chat
    一笔总账），助手消息仍只有一条。
    """
    grace_env(10)
    gated = GatedStream([_chunk(content="甲"), _usage_chunk(5, 7), _tool_call_chunk()],
                        free=1, gate_seconds=0.3)
    completions = real_stream([gated, [_chunk(content="记下了"), _usage_chunk(3, 4)]])
    sid = client.post("/v1/sessions", headers=BOOT).json()["session_id"]
    run = stream_runs.create_run(user_id="default_user", session_id=sid)
    _launch(run)

    reader = _Reader(run)
    reader.next_frame()                       # attach：SSE 连接进场
    _wait_until(lambda: run.last_seq >= 2, why="第一轮正文没进缓冲")
    reader.close()                            # 断线：1→0，计时开始

    # 宽限内用真 HTTP 的 Last-Event-ID 续播回来。这个请求会阻塞到整条流出完
    # （生产线程还要跑第二轮），所以放到线程里，主测试只管时间窗。
    box = {}
    def _resume():
        box["res"] = client.post("/v1/chat/stream",
                                 headers={**BOOT, "Last-Event-ID": f"{run.run_id}:2"},
                                 json={"messages": []})
    t = threading.Thread(target=_resume)
    t.start()
    _wait_until(lambda: run.has_reader(), timeout=4.0, why="续播没有把读者带回来")
    assert run.no_reader_since is None, "读者回来必须把 1→0 计时器清掉"
    t.join(timeout=20)

    assert run.finished and run.status == "completed", run.status
    assert len(completions.calls) == 2, "读者在场时第二轮照常发生——闸门不是来杀正常流程的"
    row = _usage_row("default_user")
    assert row["calls"] == 1, f"一整条流只结一笔总账：{row}"
    assert row["tool_rounds"] == 1, row
    msgs = _session_rows(client, sid)
    assert [m["role"] for m in msgs] == ["user", "assistant"], msgs
    assert msgs[1]["content"] == "甲记下了"
    assert msgs[1]["message_id"] == run.message_id

    verdict, done_evt = _old_client_parse([d for _, d in _frames(box["res"].text)])
    assert verdict == "done", "续播重放的最后一帧可读的必须是 done"
    assert done_evt["message_id"] == run.message_id


# ---------- 3. 宽限到期不许把已产出的半句弄丢 ----------

def test_grace_expiry_saves_partial_answer_into_session(client, real_stream, clean_usage,
                                                        grace_env):
    """取消路径的落盘纪律原样复用：已流出的部分在 save 闩下进会话，恰好一条。"""
    grace_env(0.2)
    sid = client.post("/v1/sessions", headers=BOOT).json()["session_id"]
    run, completions, _ = _expired_run(client, real_stream, sid)

    msgs = _session_rows(client, sid)
    assistants = [m for m in msgs if m["role"] == "assistant"]
    assert len(assistants) == 1, f"助手消息必须恰好落一条（闩只响一次）：{msgs}"
    assert assistants[0]["content"] == "前半句", \
        "超时不许静默丢数据：屏幕上到过的字要进历史"
    assert assistants[0]["message_id"] == run.message_id
    assert [m["role"] for m in msgs] == ["user", "assistant"], msgs


# ---------- 4. 终帧的旧客户端兼容性：被停掉的流必须以 done 收尾 ----------

def test_stopped_streams_end_in_a_frame_old_clients_honor(client, real_stream,
                                                          clean_usage, grace_env):
    """R3b-3 的三种"停下"全过一遍旧解析器：任何一种掉回 RESEND_WHOLE_TURN 都是再烧一轮。

    - [cancelled] 显式取消（R3 的 cancelled 帧今天正是洞本身）；
    - [expired]   宽限超时停；
    - [gap]       在线读者被缓冲裁减追上：in-band cannot_resume 也是旧客户端不认识的类型；
    - [completed] 成功路径回归守卫：别把一直合格的这条改坏了。
    """
    # --- [cancelled]：显式取消，停在轮中 ---
    grace_env(30)
    gated = GatedStream([_chunk(content="半句就停"), _usage_chunk(1, 1)], free=1,
                        gate_seconds=8.0)
    real_stream([gated])
    sid = client.post("/v1/sessions", headers=BOOT).json()["session_id"]
    run = stream_runs.create_run(user_id="default_user", session_id=sid)
    _launch(run)
    reader = _Reader(run)
    reader.next_frame()
    _wait_until(lambda: run.last_seq >= 2, why="正文没进缓冲")
    res = client.post(f"/v1/chat/stream/{run.run_id}/cancel", headers=BOOT)
    assert res.status_code == 200 and res.json()["status"] == "cancelled"
    _wait_until(lambda: run.finished, timeout=8.0)
    reader.close()

    payloads = [e["payload"] for e in run.events]
    types_seen = [p["type"] for p in payloads]
    assert "cancelled" in types_seen, "升级客户端的 cancelled 帧照旧在（在 done 之前）"
    verdict, done_evt = _old_client_parse(payloads)
    assert verdict == "done", "旧客户端必须读到 done 收尾，不许落回重发整轮"
    assert done_evt["message_id"] == run.message_id
    assert done_evt["status"] == "cancelled"
    assert done_evt["stopped_reason"] == "user_cancel"
    assert done_evt["resumable"] is False
    assert done_evt["seq"] == run.last_seq and done_evt["run_id"] == run.run_id
    assert types_seen[-1] == "done", "缓冲里的最后一帧也得是 done"

    # --- [expired]：宽限超时停 ---
    grace_env(0.2)
    sid2 = client.post("/v1/sessions", headers=BOOT).json()["session_id"]
    run2, _, _ = _expired_run(client, real_stream, sid2)
    verdict2, done2 = _old_client_parse([e["payload"] for e in run2.events])
    assert verdict2 == "done", verdict2
    assert done2["message_id"] == run2.message_id
    assert done2["status"] == "cancelled" and done2["stopped_reason"] == "no_reader"

    # --- [gap]：缓冲空洞，in-band cannot_resume 之后旧客户端仍要有 done 可读 ---
    # 手改手还而不是 monkeypatch：本条用例后面还有真流要跑，上限压在 monkeypatch
    # 里要到用例结束才回弹，会把后面的正常流裁成假空洞。
    prev_max = stream_runs.MAX_EVENTS_PER_RUN
    stream_runs.MAX_EVENTS_PER_RUN = 2
    try:
        run3 = stream_runs.create_run(user_id="default_user")
        for i in range(1, 6):
            run3.append({"type": "content", "text": f"块{i}"})
        run3.append({"type": "cancelled", "full_text": "块1..5"})
        run3.finish("cancelled")
        body = "".join(stream_runs.sse_frames(run3, cursor=2))
    finally:
        stream_runs.MAX_EVENTS_PER_RUN = prev_max
    verdict3, done3 = _old_client_parse([d for _, d in _frames(body)])
    assert "cannot_resume" in body, "临时帧照给升级客户端（无 seq，不进缓冲）"
    assert verdict3 == "done", "cannot_resume 也不许成为旧客户端读到的最后一帧"
    assert done3["status"] == "cannot_resume" and done3["resumable"] is False
    assert "id: " not in body.split("cannot_resume")[1], "补的 done 是连接级帧，不占序号"

    # --- [completed]：成功路径 ---
    stream_runs.clear_all_for_tests()
    res4 = client.post("/v1/chat/stream", headers=BOOT, json={
        "provider": "fake-model",
        "messages": [{"role": "user", "content": "正常说一句"}]})
    assert res4.status_code == 200, res4.text
    payloads4 = [d for _, d in _frames(res4.text)]
    verdict4, done4 = _old_client_parse(payloads4)
    assert verdict4 == "done" and done4["status"] == "completed", payloads4[-1]


# ---------- 5. regenerate：同会话第二次开问必须真付钱（R3b-4：不做跨请求内容去重） ----------

def test_regenerate_style_second_ask_is_a_real_paid_call(client, real_stream,
                                                         clean_usage):
    """app.js:1464 的"重新生成"= PUT 截断会话 + 原样再问一次。不许任何机制把它"优化"掉。

    形状与线上一致：第一轮流完，把会话替换回只剩那条用户消息，再发 /v1/chat/stream。
    钉三点：create() 两次、账本两行钱（calls 增量 1 不是 0）、落盘是两轮各自的助手消息。
    """
    completions = real_stream([[_chunk(content="第一次的答案")],
                               [_chunk(content="重新生成的答案")]])
    sid = client.post("/v1/sessions", headers=BOOT).json()["session_id"]
    body = {"provider": "fake-model", "session_id": sid,
            "messages": [{"role": "user", "content": "再说一遍"}]}

    res1 = client.post("/v1/chat/stream", headers=BOOT, json=body)
    assert res1.status_code == 200, res1.text
    rows = _session_rows(client, sid)
    keep = [{"role": m["role"], "content": m["content"]} for m in rows if m["role"] == "user"]
    rep = client.put(f"/v1/sessions/{sid}/messages", headers=BOOT, json={"messages": keep})
    assert rep.status_code == 200, rep.text

    res2 = client.post("/v1/chat/stream", headers=BOOT, json=body)
    assert res2.status_code == 200, res2.text

    assert len(completions.calls) == 2, \
        "第二次开问是新的付费轮，任何去重/缓存都不许吞掉它"
    assert _usage_row("default_user")["calls"] == 2, "两次问，两笔账"
    # 会话尾形与基线一致（这条判据防的是 R3b 的任何机制把第二轮"优化"掉）：
    # 客户端的 PUT 截掉了旧答案，第二轮的流照常把 user+assistant 写回来——
    # 最后一条必须是第二轮的真答案，且带第二轮自己的 message_id。
    msgs = _session_rows(client, sid)
    assert msgs[-1]["role"] == "assistant" and \
        msgs[-1]["content"] == "重新生成的答案", msgs
    done2 = [d for _, d in _frames(res2.text)][-1]
    assert done2["type"] == "done" and done2["message_id"] == msgs[-1]["message_id"], \
        "done 里的 message_id 要指得回落盘的那条助手消息"
    assert done2["message_id"] != \
        [d for _, d in _frames(res1.text)][-1]["message_id"], "每个 run 自己的 message_id"


# ---------- 6. 宽限期从配置通道来：改它，观察得到的行为就跟着改 ----------

def test_grace_window_comes_from_config_file_and_env(client, real_stream, clean_usage,
                                                     grace_env, config_file_grace):
    """默认 120；config.json 的值说了算；env 压过文件——三层都是既有通道的口径。

    行为面钉两侧：文件给 0.2 → 真的很快停；文件给 300 → 1.5 秒时还活着（计时器
    在等，不是装样子），随后手动取消收尾。用假的"读盘成功"糊弄不算数。
    """
    assert config_store.stream_no_reader_grace_seconds() == 120.0, "默认必须是 120 秒：宽限期不是省钱旋钮，省钱的是轮次边界钱闸"

    # 文件通道（顺带钉形状的宽容度：JSON 里的 int 30 也是合法数字）
    v = config_file_grace({"registration_open": True,
                           "stream_no_reader_grace_seconds": 30})
    assert v == 30.0, f"config.json 里的整数秒必须被接受，不是按类型不对悄悄回默认：{v}"
    assert config_file_grace({"registration_open": True,
                              "stream_no_reader_grace_seconds": 0.2}) == 0.2

    sid = client.post("/v1/sessions", headers=BOOT).json()["session_id"]
    run, completions, _ = _expired_run(client, real_stream, sid)
    assert run.status == "cancelled" and len(completions.calls) == 1

    # env 压过文件：设 300 之后同一个流在 1.5 秒时还该活着（窗口在等，没提前掐）
    config_file_grace({"registration_open": True,
                       "stream_no_reader_grace_seconds": 1})
    grace_env(300)
    gated = GatedStream([_chunk(content="慢慢说"), _usage_chunk(1, 1), _tool_call_chunk()],
                        free=2, gate_seconds=0.3)
    completions2 = real_stream([gated, [_chunk(content="第二轮")]])
    sid2 = client.post("/v1/sessions", headers=BOOT).json()["session_id"]
    run2 = stream_runs.create_run(user_id="default_user", session_id=sid2)
    _launch(run2)
    reader = _Reader(run2)
    reader.next_frame()
    _wait_until(lambda: run2.last_seq >= 2, why="正文没进缓冲")
    reader.close()
    time.sleep(1.5)
    assert not run2.finished, "env=300 时 1.5 秒就停说明没读配置，只读了自己的默认值"
    assert not run2.cancel_event.is_set()
    run2.cancel()                      # 收尾：显式取消这条测试性的流
    _wait_until(lambda: run2.finished, timeout=8.0)
    assert len(completions2.calls) == 1, "取消之后的轮次边界照旧拦新付费轮"


# ---------- 7. 既有行为不许坏：显式取消仍旧是"立刻"，不吃宽限 ----------

def test_explicit_cancel_stops_instantly_even_with_long_grace(client, real_stream,
                                                               clean_usage, grace_env):
    """宽限 300 秒也拦不住用户按停止：取消走的是标志位，与计时器互不依赖。"""
    grace_env(300)
    gated = GatedStream([_chunk(content="还在说"), _usage_chunk(2, 2)], free=1,
                        gate_seconds=8.0)
    completions = real_stream([gated])
    run = stream_runs.create_run(user_id="default_user")
    _launch(run)
    reader = _Reader(run)
    reader.next_frame()
    _wait_until(lambda: run.last_seq >= 2, why="正文没进缓冲")

    t0 = time.monotonic()
    res = client.post(f"/v1/chat/stream/{run.run_id}/cancel", headers=BOOT)
    assert res.status_code == 200 and res.json()["status"] == "cancelled"
    _wait_until(lambda: run.finished, timeout=3.0,
                why="显式取消被宽限期拖住了——cancel 必须是即时动作")
    assert time.monotonic() - t0 < 3.0
    assert gated.closed is True, "取消仍要伸手尽力关连接（R3 的原判据不许退化）"
    assert len(completions.calls) == 1
    reader.close()
    verdict, done = _old_client_parse([e["payload"] for e in run.events])
    assert verdict == "done" and done["stopped_reason"] == "user_cancel"


def test_cancel_during_grace_wait_does_not_wait_for_the_window(client, real_stream,
                                                                clean_usage, grace_env):
    """读者不在场、生产线程正挂在宽限等待里时按下取消：cond 要把它立刻叫醒。

    等的是 grace=20 的窗，取消在 ~1 秒处到达——收尾必须远早于窗口本身，
    证明等待挂在条件变量上而不是睡固定时长，也证明两条停止路径共用一个终局。
    """
    grace_env(20)
    gated = GatedStream([_chunk(content="等风来"), _usage_chunk(1, 1), _tool_call_chunk()],
                        free=2, gate_seconds=0.2)
    real_stream([gated, [_chunk(content="第二轮")]])
    sid = client.post("/v1/sessions", headers=BOOT).json()["session_id"]
    run = stream_runs.create_run(user_id="default_user", session_id=sid)
    _launch(run)
    reader = _Reader(run)
    reader.next_frame()
    _wait_until(lambda: run.last_seq >= 2, why="正文没进缓冲")
    reader.close()
    time.sleep(1.0)   # 让生产线程先抵达轮次边界、真的挂进宽限等待里
    t0 = time.monotonic()
    res = client.post(f"/v1/chat/stream/{run.run_id}/cancel", headers=BOOT)
    assert res.status_code == 200, res.text
    _wait_until(lambda: run.finished, timeout=3.0, why="取消没能叫醒正在等宽限的生产线程")
    assert time.monotonic() - t0 < 2.5
    assert run.stop_cause != "no_reader", "先到的是显式取消，成因就记取消，不许张冠李戴"


# ---------- 8. 读者计数的状态机（单元面，先把"1→0 起表、0→1 停表"钉死） ----------

def test_reader_attach_detach_clock_semantics(client, real_stream, clean_usage):
    """attach/detach/has_reader/计时器四件套的原子语义，全部经真 sse_frames 进出。"""
    run = stream_runs.create_run(user_id="default_user")
    run.append({"type": "content", "text": "在的"})           # 让首帧 next() 不等就返回

    assert not run.has_reader() and run.no_reader_since is None

    g1 = _Reader(run)
    assert g1.next_frame() is not None
    assert run.has_reader() and run.no_reader_since is None

    g2 = _Reader(run)
    assert g2.next_frame() is not None                          # 两个读者
    g1.close()
    assert run.has_reader(), "2→1 不是 1→0，计时器不该起表"
    assert run.no_reader_since is None

    g2.close()
    assert not run.has_reader()
    since = run.no_reader_since
    assert since is not None, "1→0 必须起表"

    g3 = _Reader(run)
    assert g3.next_frame() is not None
    assert run.no_reader_since is None, "读者回来必须把表停掉（续播取消计时器的根据）"

    # wait_for_reader：有读者 = 立刻真；无读者 = 最多等 grace，不忙轮询
    t0 = time.monotonic()
    assert run.wait_for_reader(0.0) is True
    assert time.monotonic() - t0 < 0.5
    g3.close()
    assert run.wait_for_reader(0.0) is False, "grace=0 就是立刻判定超时"
    # 超时判定把窗口起点又钉回了当下？——不：1→0 的表只起不挪，这里靠重新进场验证
    woken = {}
    def _later():
        time.sleep(0.2)
        woken["g"] = _Reader(run)
        woken["g"].next_frame()
    th = threading.Thread(target=_later)
    th.start()
    assert run.wait_for_reader(3.0) is True, "窗口里读者回来必须把等待变成真"
    th.join()
    woken["g"].close()
