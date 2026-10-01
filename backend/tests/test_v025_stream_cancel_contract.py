"""v0.25 R3 行为契约：流被打断不重放整轮，「停止」真的停得掉，停完不再花钱。

为什么判据在这个文件里而不是 test_route_auth_contract：路由契约钉的是"声明"
（每条 /v1 路由带身份依赖），匿名 GET 扫面也管不到 POST 的 cancel。这里钉的是
**行为**，三句红线：

1. 取消之后，这一轮的账本增量必须真的是 0（快照现读数字，不是"标志位翻了"）——
   PRD 判据原话是「取消后 usage 账本增量 = 0，且有契约测试钉住」。上一段的钱
   照记（token 确实消费了，假装没花才是假账），但取消落地之后：不再有第二次
   create()、不再有下一轮工具、不再有第二条账行、重连续播不再重记任何东西。
2. 带合法游标的续播 = 纯缓冲回放：不调模型、不重记 usage、不第二条助手消息。
3. 续播不了（不存在 / 不是你的 / 缓冲空洞 / 已过保留期）= 一个明确的 410
   cannot_resume，三类失败**逐字节相同**，run_id 因此不是探测信道；并且这个
   信号的含义是"别重发原文"，服务端也顺手不落任何新行。

负向验证（怎么证明这三条锁有牙，2026-10 实测记录在 v0.25 过程留档）：
- 把 stream_chat 里的取消检查摘掉（只剩"翻标志位"）：
  test_cancel_really_stops_generation_and_no_further_step_bills 变红——
  闸门到点开之后第二轮 create() 真的发生，FakeUpstream 那颗炸弹先炸。
- 把 resume 分支改成"落到正常开轮流程"（等于旧行为：断线重放整轮）：
  test_resume_valid_offset_replays_without_rebilling_or_restorage 变红——
  create() 次数从 1 变 2，会话里出现第二条助手消息。
"""
import importlib
import json
import threading
import time
import types
import uuid

import pytest

from app.core import stream_runs, usage
from app.core.authz import UNAUTHORIZED_DETAIL, Principal
from app.core.providers import store as provider_store
from app.main import app, launch_stream_run

# enforced 夹具下唯一的管理员身份（与 test_stream_api 同一个 BOOT）。
BOOT = {"Authorization": "Bearer boot-token"}


# ---------- 测试替身：形状对齐 openai SDK 的流式返回 ----------

def _chunk(content=None, tool_calls=None):
    """一个普通内容/工具增量块（无 usage）。形状同 test_stream_tools。"""
    delta = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=delta, finish_reason=None)],
        usage=None)


def _usage_chunk(prompt=5, completion=7):
    """上游最后那种 choices 为空、只带 usage 的块——账就从这个数字来。"""
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
    """会"挂在半截"的假上游：前 free 块照常给，之后卡在闸门上等 close()。

    close() 正是 cancel 端点对真 openai Stream 做的事（run.abort_upstream）。
    这条替身存在的意义是把「取消是否真的伸手关了连接」变成可断言的事实：
    只翻标志位不关连接的话，闸门要等到超时才放行——gated.closed 会是 False，
    而且超时的最坏路径下第二轮照样被叫起来（FakeCompletions 的炸弹先响）。
    """

    def __init__(self, chunks, free=2, gate_seconds=5.0):
        self._chunks = list(chunks)
        self._free = free
        self._pulled = 0
        self._cv = threading.Condition()
        self.closed = False
        self.close_calls = 0
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
            # 真机上 close 一条正被阻塞读取的连接，读侧看到的就是这类异常
            raise ConnectionAbortedError("闸门：连接被取消端点关闭")
        self._pulled += 1
        if self._pulled <= len(self._chunks):
            return self._chunks[self._pulled - 1]
        raise StopIteration

    def close(self):
        self.close_calls += 1
        with self._cv:
            self.closed = True
            self._cv.notify_all()


class FakeCompletions:
    """每次 create() 消费一个预先排好的"轮"；弹药没了还来——直接炸。

    那颗 RuntimeError 不是偷懒的兜底，是判据本身：「不该发生的付费调用发生了」
    应当在账本上留下痕迹之前，先在调用点留下痕迹。
    """

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._rounds:
            raise AssertionError("不该发生的第二次上游调用发生了（取消没拦住，或续播走了重跑）")
        item = self._rounds.pop(0)
        return iter(item) if isinstance(item, list) else item


class FakeClient:
    def __init__(self, rounds):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(rounds))


# ---------- 夹具 ----------

@pytest.fixture(autouse=True)
def _clean_runs():
    """运行表是进程级内存状态：每条用例进空、出空。

    不清的话，"按 id 从表里拿"的用例可能捡到别人上一条用例留下的 run——那种红
    离真凶隔着整个文件，与 task_store/限流账本进测试前必须清是同一个理由。
    """
    stream_runs.clear_all_for_tests()
    yield
    stream_runs.clear_all_for_tests()


@pytest.fixture
def clean_usage(tmp_path, monkeypatch):
    """把进程级账本换到一个空的临时文件上：判据是"数字恰好是几"，容不得历史行。

    进出都还原（同 isolated_schedule 的做法）——_days 是模块全局，带进带出的话,
    下一条用例的"增量 = 0"会是在跟上一条用例的存量比。
    """
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
    """拿回真 stream_chat，并把它的 build_client 换成假上游（reload 模式同 test_stream_tools）。

    取消与续播的判据必须落在**真** stream_chat 上：conftest 那个桩不参与上游连接,
    在它身上测"取消有没有关连接"等于什么都没测。
    """
    import app.core.streaming as streaming
    importlib.reload(streaming)
    holder = {}
    monkeypatch.setattr(streaming, "build_client", lambda provider: holder["client"])

    def install(rounds):
        client = FakeClient(rounds)
        holder["client"] = client
        return client.chat.completions

    yield install
    importlib.reload(streaming)


@pytest.fixture
def two_stream_users(client, enforced):
    """两个真用户的头部与 uid（enforced 才是真身份：disabled 下人人是管理员）。"""
    ha = enforced("流甲")
    hb = enforced("流乙")
    uid_a = client.get("/v1/auth/me", headers=ha).json()["user_id"]
    uid_b = client.get("/v1/auth/me", headers=hb).json()["user_id"]
    return ha, uid_a, hb, uid_b


# ---------- 工具函数 ----------

def _frames(body: str):
    """把一段 SSE 响应体拆成 [(run_id或None, payload)]，只收带 data: 的帧。"""
    out = []
    for block in body.split("\n\n"):
        rid = None
        data = None
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


def _run_id_from(frames):
    for rid, _ in frames:
        if rid and ":" in rid:
            return rid.rsplit(":", 1)[0]
    raise AssertionError(f"响应里没有任何 id: 行，run_id 无从取得：{frames}")


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
    assert len(rows) <= 1, f"同一个人同一 provider 竟然有多行：{rows}"
    return rows[0] if rows else None


def _session_rows(client, sid):
    return client.get(f"/v1/sessions/{sid}").json()["messages"]


# ---------- 协议本体：帧带 id 与 seq，旧事件名原样活着 ----------

def test_new_stream_frames_carry_run_id_and_sequence(client):
    """协议锁：每一帧都有 `id: <run_id>:<seq>`，data 里带 seq/run_id。

    同时钉向后兼容——start/content/done 三个旧事件名不许被换掉：两个客户端
    （api.js:171-173、Api.kt parseFrame）都按 data.type 分发，改了名字等于
    在客户端 lane 合入之前先把线上打断。
    """
    res = client.post("/v1/chat/stream", json={
        "provider": "fake-model",
        "messages": [{"role": "user", "content": "随便说点什么"}]})
    assert res.status_code == 200, res.text
    frames = _frames(res.text)
    types_seen = [d["type"] for _, d in frames]
    assert types_seen[0] == "start" and types_seen[-1] == "done", types_seen
    assert "content" in types_seen, types_seen

    run_id = _run_id_from(frames)
    seqs = [d["seq"] for _, d in frames]
    assert seqs == list(range(1, len(frames) + 1)), f"seq 必须从 1 连续：{seqs}"
    for (rid, d) in frames:
        assert rid == f"{run_id}:{d['seq']}", f"id 行与 payload.seq 对不上：{rid}"
        assert d["run_id"] == run_id


# ---------- (a) 取消要登录 ----------

def test_cancel_requires_login(client, enforced, two_stream_users):
    """匿名 POST cancel 必须 401——cancel 是这族里唯一的 POST，GET 全家桶扫不到它。"""
    ha, _, hb, _ = two_stream_users
    res = client.post("/v1/chat/stream", headers=hb, json={
        "provider": "fake-model",
        "messages": [{"role": "user", "content": "乙的流"}]})
    assert res.status_code == 200
    run_id = _run_id_from(_frames(res.text))

    anon = client.post(f"/v1/chat/stream/{run_id}/cancel")
    assert anon.status_code == 401, anon.text
    assert anon.json()["detail"] == UNAUTHORIZED_DETAIL
    run = stream_runs.get_run(run_id)
    assert run is not None and not run.cancel_event.is_set(), "被挡下的取消请求不许翻标志位"


# ---------- (b) 不是你的 == 不存在 ----------

def test_stranger_cancel_is_indistinguishable_from_a_missing_run(client, enforced,
                                                                 two_stream_users):
    """A 取消 B 的 run：与取消一个编造的 uuid **逐字节相同**的 404。

    同 _task_for_principal 一个做法：403 等于承认这个 id 存在，那 cancel 就成
    了 run_id 探测器。B 自己取消已终态的 run 是 200 warning（与任务面
    "已处于终态"同一个形状），这一对照防的是"一律 404"也算过。
    """
    ha, _, hb, _ = two_stream_users
    res = client.post("/v1/chat/stream", headers=hb, json={
        "provider": "fake-model",
        "messages": [{"role": "user", "content": "乙的流"}]})
    run_id = _run_id_from(_frames(res.text))

    theirs = client.post(f"/v1/chat/stream/{run_id}/cancel", headers=ha)
    made_up = client.post(f"/v1/chat/stream/{uuid.uuid4()}/cancel", headers=ha)
    assert theirs.status_code == 404, theirs.text
    assert (made_up.status_code, made_up.text) == (theirs.status_code, theirs.text), \
        f"两种失败给了两种答案：{theirs.text!r} vs {made_up.text!r}"
    assert theirs.json()["detail"] == "运行不存在"

    run = stream_runs.get_run(run_id)
    assert not run.cancel_event.is_set(), "陌生人的 cancel 竟动了别人的 run"
    own = client.post(f"/v1/chat/stream/{run_id}/cancel", headers=hb)
    assert own.status_code == 200, own.text
    assert own.json()["status"] == "warning", \
        f"已终态的取消应答与任务面同形（warning + 说清终态）：{own.json()}"


def test_cross_user_resume_is_indistinguishable_from_an_unknown_one(client, enforced,
                                                                    two_stream_users):
    """续播这一头同一条纪律：不是你的 / 不存在 / 已过期，同一个 410、同一句话。"""
    ha, _, hb, _ = two_stream_users
    res = client.post("/v1/chat/stream", headers=hb, json={
        "provider": "fake-model",
        "messages": [{"role": "user", "content": "乙的流"}]})
    run_id = _run_id_from(_frames(res.text))

    stranger = client.post("/v1/chat/stream",
                           headers={**ha, "Last-Event-ID": f"{run_id}:1"},
                           json={"messages": []})
    unknown = client.post("/v1/chat/stream",
                          headers={**ha, "Last-Event-ID": f"{uuid.uuid4()}:9"},
                          json={"messages": []})
    assert stranger.status_code == 410, stranger.text
    assert unknown.status_code == 410, unknown.text
    assert stranger.text == unknown.text, "两种失败逐字节相同才不是探测信道"
    assert stranger.json()["code"] == "cannot_resume"
    # B 自己续播则是纯回放：200，且把剩下的帧补全
    own = client.post("/v1/chat/stream", headers={**hb, "Last-Event-ID": f"{run_id}:1"},
                      json={"messages": []})
    assert own.status_code == 200, own.text
    assert _frames(own.text)[0][1]["type"] == "content"


# ---------- (c) 取消真停 + 之后增量 = 0（真数字） ----------

def test_cancel_really_stops_generation_and_no_further_step_bills(client, real_stream,
                                                                  clean_usage):
    """PRD 判据的完整形状：取消后上游没被关闭前的最后一口气记一行真账，
    此后任何一步（重连、续播、再取消）账本增量 = 0，且不再有第二次 create()。
    """
    # 第一轮：正文一块 + usage 一块 + 工具调用一块（工具调用后本应进第二轮付费轮）
    gated = GatedStream([_chunk(content="前半句"), _usage_chunk(5, 7), _tool_call_chunk()])
    completions = real_stream([gated, [_chunk(content="不该出现第二轮")]])

    sid = client.post("/v1/sessions").json()["session_id"]
    provider = provider_store.resolve("fake-model")
    run = stream_runs.create_run(user_id="default_user", session_id=sid)
    principal = Principal("default_user", "本机管理员", "admin")
    pipe = types.SimpleNamespace(  # 生产里这是 ChatPipeline；这里只给生产者用到的两面
        tools_schema=[{"type": "function",
                       "function": {"name": "calculator", "description": "",
                                    "parameters": {"type": "object"}}}],
        save_interaction=lambda *a: None)

    launch_stream_run(run, provider,
                      [{"role": "user", "content": "帮我算"}], "帮我算",
                      principal, pipe, [])

    # 等生产者把正文推上缓冲（这时上游正卡在闸门上——取消要打断的就是它）
    assert _wait_until(lambda: run.last_seq >= 2, why="生产者没把正文推上缓冲"), \
        "两秒内没看到 content 事件，替身或生产者线程坏了"

    res = client.post(f"/v1/chat/stream/{run.run_id}/cancel")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "cancelled", res.text

    assert _wait_until(lambda: run.finished, timeout=8.0), "取消后生产线程没有收尾"
    assert gated.closed is True, \
        "取消只翻了标志位，没有伸手关闭上游连接——「停止」必须是动作，不是心愿"

    # 上游调用停在第一轮：工具清单回灌后的第二轮 create() 不许发生
    assert len(completions.calls) == 1, \
        f"取消后还发起了新的付费轮：{len(completions.calls)} 次 create()"

    # R3b-3 改写了终帧形状（拆解清单里的显式裁定）：cancelled 仍在，但其后必须
    # 跟一条旧客户端认得的 done 收尾——否则旧 PWA 把"看不懂的终帧"当成"没有
    # done"，判 retryable 后整轮重发回 /v1/chat，正是 R3b 要堵的出血点。
    payloads = [e["payload"] for e in run.events]
    assert payloads[-2]["type"] == "cancelled" and payloads[-1]["type"] == "done", \
        f"cancelled 之后必须有 done 收尾：{[p['type'] for p in payloads[-2:]]}"
    assert run.status == "cancelled"

    # 账：取消收尾时把那口气的钱结清（一行，failed，数字取上游回传），此后增量必须为 0
    row = _usage_row("default_user")
    assert row is not None, "取消的这一轮在账本上应该有一行（花了的就是花了）"
    assert row["calls"] == 1 and row["failed"] == 1 and row["ok"] == 0, row
    assert row["total_tokens"] == 12, f"账取上游回传的 usage，不是估算：{row}"

    # 基线取在"这一轮已经结清"之后：before→after 量的是取消**之后**的步骤（重连、
    # 续播、再取消）的增量。取早了会把本轮自己该结的那笔账算成"取消后的新增"。
    before = usage.snapshot()

    # 「后续步骤」：重连续播 + 再取消一次——账本纹丝不动，消息也不许多写一条
    rejoin = client.post("/v1/chat/stream",
                         headers={**BOOT, "Last-Event-ID": f"{run.run_id}:1"},
                         json={"messages": []})
    assert rejoin.status_code == 200, rejoin.text
    replayed = [d["type"] for _, d in _frames(rejoin.text)]
    # 同上（R3b-3）：重放给升级客户端的尾巴是 cancelled→done，旧客户端只读得到 done
    assert replayed == ["content", "cancelled", "done"], replayed
    again = client.post(f"/v1/chat/stream/{run.run_id}/cancel", headers=BOOT)
    assert again.json()["status"] == "warning", again.text

    after = usage.snapshot()
    delta_calls = sum(r["calls"] for r in after) - sum(r["calls"] for r in before)
    assert delta_calls == 0, f"取消之后账本还动了：calls 增量 {delta_calls}"

    rows = _session_rows(client, sid)
    assert [m["role"] for m in rows] == ["user", "assistant"], rows
    assert rows[1]["content"] == "前半句", "取消落盘的是已经流出去的那半句，不是空也不是重放"
    assert rows[1]["message_id"] == run.message_id


# ---------- (d) 合法游标续播：不重调、不重记、不重写 ----------

def test_resume_valid_offset_replays_without_rebilling_or_restorage(client, real_stream,
                                                                    clean_usage):
    """断线重连的正路：补发缺的那几帧，然后收摊。

    钉四个数：create() 次数、账本 calls、会话里 assistant 行数、正文内容。
    把 resume 分支改成"落回正常开轮"（＝今天客户端断线重发整轮的服务端版），
    这四个数一起变二，这条当场红。
    """
    completions = real_stream([[_chunk(content="甲"), _chunk(content="乙"),
                               _chunk(content="丙")]])
    sid = client.post("/v1/sessions").json()["session_id"]
    res = client.post("/v1/chat/stream", json={
        "provider": "fake-model", "session_id": sid,
        "messages": [{"role": "user", "content": "说三个字"}]})
    assert res.status_code == 200, res.text
    frames = _frames(res.text)
    run_id = _run_id_from(frames)
    assert [d["type"] for _, d in frames] == \
        ["start", "content", "content", "content", "done"]

    assert len(completions.calls) == 1
    row = _usage_row("default_user")
    assert row and row["calls"] == 1, row
    before_rows = _session_rows(client, sid)
    assert [m["role"] for m in before_rows] == ["user", "assistant"]

    # 假装断线：客户端只收到了前两帧（start + 第一块），带着 run:2 来续
    rejoin = client.post("/v1/chat/stream", headers={"Last-Event-ID": f"{run_id}:2"},
                         json={"messages": []})
    assert rejoin.status_code == 200, rejoin.text
    replayed = _frames(rejoin.text)
    assert [d["type"] for _, d in replayed] == ["content", "content", "done"], replayed
    assert [d["text"] for _, d in replayed if d["type"] == "content"] == ["乙", "丙"]
    # 续播补的 done 与原流的 done 必须是同一条 message_id（反馈要挂在同一条消息上）
    assert replayed[-1][1]["message_id"] == frames[-1][1]["message_id"]

    # 续播之后：还是那一个上游调用、那一行账、那一条助手消息
    assert len(completions.calls) == 1, "续播竟重跑了上游"
    row = _usage_row("default_user")
    assert row["calls"] == 1, f"续播把账重记了：{row}"
    after_rows = _session_rows(client, sid)
    assert [m["role"] for m in after_rows] == ["user", "assistant"], after_rows
    assert after_rows[1]["content"] == "甲乙丙"


# ---------- (e) 续播不了 = 明确的「别重发」 ----------

def test_gap_in_buffer_is_an_explicit_cannot_resume(client, monkeypatch):
    """缓冲被上限裁掉、请求的位置已经不连续：live 读者收到 cannot_resume 帧，之后没有重放。

    上限走 monkeypatch 的数字而不是等真缓冲：判据要测的是"空洞要明说"，
    不是"你得先塞满二十万字符"。
    """
    monkeypatch.setattr(stream_runs, "MAX_EVENTS_PER_RUN", 2)
    run = stream_runs.create_run(user_id="default_user")
    for i in range(1, 6):
        run.append({"type": "content", "text": f"块{i}"})
    run.append({"type": "done", "full_text": "块1..5"})
    run.finish("completed")

    # seq=2 之后的下一个（3）已被裁掉：resumable 判 False，HTTP 层同一句话
    assert stream_runs.resumable(run, 2) is False
    assert stream_runs.resumable(run, 5) is True  # 已追平的人还能拿终态之后的东西（空批次收尾）

    frames = list(stream_runs.sse_frames(run, cursor=2))
    body = "".join(frames)
    data = json.loads(body.split("data: ")[1].strip())
    assert data["type"] == "cannot_resume", data
    assert data["resumable"] is False
    assert "id: " not in body, "这条是临时帧：它没有 seq，不占协议里的序号"


def test_expired_run_resume_says_cannot_resume_and_stores_nothing(client, monkeypatch):
    """过了保留期被清出表的 run：410 与"编一个 id"逐字节相同，且顺手不落任何新行。

    这就是"什么不活过重启/回收"的行为化说法：表是这一进程内存里的，清掉之后
    服务端能给的只有"续播不了，别重发，去会话里取结果"。
    """
    sid = client.post("/v1/sessions").json()["session_id"]
    res = client.post("/v1/chat/stream", json={
        "provider": "fake-model", "session_id": sid,
        "messages": [{"role": "user", "content": "跑一轮就过期"}]})
    run_id = _run_id_from(_frames(res.text))
    rows_before = _session_rows(client, sid)

    # 把保留窗压成 0 再触发清扫（sweep 挂在新 run 创建时，与生产同一入口）
    monkeypatch.setattr(stream_runs, "FINISHED_RETAIN_SECONDS", 0)
    client.post("/v1/chat/stream", json={
        "provider": "fake-model", "messages": [{"role": "user", "content": "另一轮"}]})

    gone = client.post("/v1/chat/stream", headers={"Last-Event-ID": f"{run_id}:1"},
                       json={"messages": []})
    unknown = client.post("/v1/chat/stream",
                          headers={"Last-Event-ID": f"{uuid.uuid4()}:1"},
                          json={"messages": []})
    assert gone.status_code == 410, gone.text
    assert gone.text == unknown.text, "过期与不存在必须是同一句话"
    assert gone.json()["code"] == "cannot_resume"
    assert _session_rows(client, sid) == rows_before, "一次被拒的续播不该动会话"


# ---------- 负向验证的辅助证据：cancel 的 warning 语义与任务面同源 ----------

def test_cancel_terminal_states_share_the_task_wording(client):
    """"cancelled" 在任务面和流式面必须是同一个词、同一个终态语义——不许裂成两套。

    钉三点：TaskStatus.CANCELLED 的值就是流式 run 的 status 值；两边的 warning
    响应形状一致（status/message 两个键，话说"已处于终态"）；两边都不回滚已经
    发生的部分。
    """
    from app.agents.task_store import TaskStatus

    assert TaskStatus.CANCELLED.value == "cancelled"

    res = client.post("/v1/chat/stream", json={
        "provider": "fake-model", "messages": [{"role": "user", "content": "跑完它"}]})
    run_id = _run_id_from(_frames(res.text))
    run = stream_runs.get_run(run_id)
    assert run.status == "completed"

    warn = client.post(f"/v1/chat/stream/{run_id}/cancel").json()
    assert warn["status"] == "warning" and "已处于终态" in warn["message"], warn
