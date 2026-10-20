"""v0.35 #82/#98：挂起在**既有出口**上长什么样——任务面的真相在读侧，帧只认值。

两条边界是拍定的，这份文件不重新论证、只钉住它们：
- **不造新帧名**：`stream_events.CLIENT_FRAMES` 一个都不加（`test_process_trace_contract.py` 的
  `test_client_frames_is_exactly_the_names_the_contract_can_build` 对常量与构造器做双向
  比对，`test_no_new_sse_frame_shape_for_old_clients` 另有一句"waiting_user 不许进"）。
- **不给帧加键**：`tool_result` 是七键 `==` 全等
  （`test_no_new_sse_frame_shape_for_old_clients`），`waiting_*` 那一族被
  `test_the_window_boundary_is_gross_and_every_uncertainty_means_do_not_void` 做 `==` 全等。
  于是第三态只能借**既有字段的取值**表达。

补的是既有锁没有覆盖的三件事：

1. 挂起那一支在 `tool_result` 帧里只借 `summary` 的 `⏸ ` 前缀表达（`ok` 仍是 False），
   且帧里**没有任何任务身份**（task_id / 游标 / 问题 / waiting_user）。这不是洁癖：
   帧上既然拿不到任务身份，卡片就不可能从帧推出任务状态——那条路结构上不存在。
2. 卡片的真相来自 `GET /v1/tasks/{id}`，而且**只来自盘上那条记录**。钉法不是"读一遍
   GET 看一眼对不对"，而是 `task_store.restore()`（本仓认定"重启过一次进程"的做法，
   A1/A3 同款）之后再读一遍，**除随墙上时钟变的 `waiting_elapsed_seconds` 外逐键相同**
   ——任何把状态只放在进程内存里的实现（SSE 缓冲就是现成的那一份）都会在这一步露出空白。
3. "这个停点还活着"只认 `status == "waiting_user"`（等价于 `can_resume is True`）。
   `cancelled` 与 `failed` 都会**留着**游标（A1 的"上次停在哪儿"，
  由 `test_waiting_user_exits_to_every_allowed_state` 钉着），
   拿游标在不在当判据，就会给一条已经定论的任务画输入框。

本笔的保证只有两条：① 真挂起时帧与模型文本是**同一个串**、不重复加前缀（模型会看到
`⏸`——那是 A3 起 `response.to_string()` 的既有前缀；要"模型绝不看到"是 A3 的独立票，
不在本笔）；② 帧不带任何任务身份（task_id / 问题 / 游标），卡片的真相读 GET。

`pipeline.py` 那条非流式 trace 的形与流式同构；这里只跑流式那一支（`stream_chat_events`），
因为它是 `tool_result` 帧的唯一生产者，另一支产的是落盘步，不产帧。
"""
import importlib
import json
from types import SimpleNamespace

import pytest

import app.agents.task_store as ts
from app.core import stream_events as se
from app.core import streaming
from app.tools import executor as ex
from app.tools.executor import ToolOutcome
from app.tools.registry import tools_registry
from app.tools.response import SUSPEND_PREFIX, ToolSuspended

PARK_TOOL = "t82_park"

# 七键形状：与 `test_no_new_sse_frame_shape_for_old_clients` 同一份，这里再写一遍是为了让"多一个键"的漂移在**本文件**
# 就地报红，而不是只靠另一份文件的上下文。两处都由 a3 那份锁兜底，这里不替代它。
_TOOL_RESULT_KEYS = {"type", "id", "name", "ok", "summary", "elapsed_ms", "truncated"}

# 帧上不许出现的任务面字段：出现任何一个，帧就成了卡片的第二份真相来源。
_TASK_IDENTITY_KEYS = {"task_id", "question", "resume_state", "resume_cursor",
                       "waiting_user", "can_resume", "waiting_since"}


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    """任务表进每条用例自己的文件（A1/A3/A5 同款夹具：绝不让真库路径进环境）。"""
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    yield path
    for tid in list(ts.task_store):
        del ts.task_store[tid]


@pytest.fixture
def park_tool():
    """注册一条真会挂起的工具：#79 之后 `suspends=True` 还必须配一档频控，
    缺了执行器会在 `_dispatch` 处以 "缺配套" 拒掉——这里把两样都摆齐，测的是帧，
    不是那条派发闸门。"""
    def fn(item: str = "", resume: dict = None):
        raise ToolSuspended("请输入短信验证码", {"step": 3, "item": item})

    info = {"function": fn, "description": "停一下问用户", "parameters": {"type": "object",
            "properties": {}}, "available": None, "needs_user": False,
            "requires_approval": False, "suspends": True}
    saved = tools_registry.get(PARK_TOOL)
    saved_limit = ex._FREQ_LIMITS.get(PARK_TOOL)
    tools_registry[PARK_TOOL] = info
    ex._FREQ_LIMITS[PARK_TOOL] = (600.0, 10)
    yield
    if saved is None:
        tools_registry.pop(PARK_TOOL, None)
    else:
        tools_registry[PARK_TOOL] = saved
    if saved_limit is None:
        ex._FREQ_LIMITS.pop(PARK_TOOL, None)
    else:
        ex._FREQ_LIMITS[PARK_TOOL] = saved_limit


class _RoundStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        pass


class _FakeLLM:
    """一轮一份假 chunk 列表；顺手记下引擎每一轮**真正交给模型**的 messages。"""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.messages_per_round = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.messages_per_round.append(list(kwargs.get("messages") or []))
        return _RoundStream(self.rounds.pop(0) if self.rounds else [])


def _chunk(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, reasoning_content=None,
                            tool_calls=tool_calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def _call_chunk(call_id="c_1", name=PARK_TOOL, arguments='{"item": "拿铁"}'):
    return _chunk(tool_calls=[SimpleNamespace(
        index=0, id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments))])


def _run_engine(monkeypatch, llm, frames_out=None):
    """真跑一轮 `stream_chat_events`：conftest 的 autouse 把整个函数换成了假身，
    要先 reload 才拿得到真引擎（先例：test_cross_owner_replay_scrubs_approval_frame.py）。"""
    importlib.reload(streaming)
    try:
        monkeypatch.setattr(streaming, "build_client", lambda provider: llm)
        return list(streaming.stream_chat_events(
            "fake-chat", [{"role": "user", "content": "点一杯咖啡"}],
            provider_id="fake-model", user_id="u-t82",
            tools=[{"type": "function",
                    "function": {"name": PARK_TOOL, "parameters": {"properties": {}}}}],
            trace_out=frames_out if frames_out is not None else []))
    finally:
        importlib.reload(streaming)


# ---------- 1. 第三态在帧里只借值表达 ----------

def test_a_suspended_tool_result_speaks_by_value_and_carries_no_task_identity(
        monkeypatch, park_tool):
    llm = _FakeLLM([[_call_chunk()], []])
    frames = _run_engine(monkeypatch, llm)

    results = [f for f in frames if f.get("type") == se.FRAME_TOOL_RESULT]
    assert len(results) == 1, f"一轮挂起该且只该产出一条 tool_result：{len(results)}"
    frame = results[0]
    assert set(frame) == _TOOL_RESULT_KEYS, f"tool_result 的形状漂了：{sorted(frame)}"
    assert frame["ok"] is False, "挂起冒充成功：面板会把它画成对勾"
    assert frame["summary"].startswith(SUSPEND_PREFIX), (
        f"挂起那一支没带第三态记号：{frame['summary']!r}")
    assert not (set(frame) & _TASK_IDENTITY_KEYS), (
        f"帧里冒出了任务面字段：{sorted(set(frame) & _TASK_IDENTITY_KEYS)}")
    # 第三态是**值**不是帧名：帧型仍是既有的 tool_result，waiting_user 不在发放清单里
    assert frame["type"] in se.CLIENT_FRAMES
    assert "waiting_user" not in se.CLIENT_FRAMES
    # 模型拿到的是工具自己的那句话（同一份文本投影），不是卡片文案
    tool_msgs = [m for m in llm.messages_per_round[1] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1 and tool_msgs[0]["content"].startswith(SUSPEND_PREFIX)

    # 这条锁自己有没有牙：同一份判据对"多一个键"的帧必须说不
    assert not (set({**frame, "task_id": "T"}) == _TOOL_RESULT_KEYS), \
        "七键判据没牙：多一个键它也说相等"


def test_the_marker_is_added_only_when_the_outcome_text_lacks_it(
        monkeypatch, park_tool):
    """`⏸ ` 自 A3 起就在 `response.to_string()` 里，帧与模型看到的是**同一个串**。

    实跑（`c68b711c` 与 `cd1e0522` 各一次）：帧的 `summary` 与回灌给模型的 tool content
    都是 `'⏸ 需要用户输入：…〔执行已经停在断点：…〕'`，各 215 bytes、两版逐字节相同
    ——本笔没新增污染，本笔新增的只有"outcome 文本缺前缀时补上"这一件事。

    做法是把执行器那一层换成一份"挂着但文本没带前缀"的结果（将来某条工具或中间层
    改了文本投影，就是这个形状）：帧必须仍按 `outcome.suspended` 补回记号，而回灌给
    模型的 content 与 outcome 给的文本一字不差——不加第二道前缀。
    """
    importlib.reload(streaming)
    plain = "需要用户输入：请输入短信验证码"
    monkeypatch.setattr(
        streaming, "_run_tool_detailed",
        lambda name, raw, user_id=None: ToolOutcome(
            plain, False, None, False, 1, suspended=True,
            suspension={"tool_name": name, "prompt": "请输入短信验证码"}))
    llm = _FakeLLM([[_call_chunk()], []])
    monkeypatch.setattr(streaming, "build_client", lambda provider: llm)
    try:
        frames = list(streaming.stream_chat_events(
            "fake-chat", [{"role": "user", "content": "点一杯咖啡"}],
            provider_id="fake-model", user_id="u-t82",
            tools=[{"type": "function",
                    "function": {"name": PARK_TOOL, "parameters": {"properties": {}}}}],
            trace_out=[]))
    finally:
        importlib.reload(streaming)

    frame = [f for f in frames if f.get("type") == se.FRAME_TOOL_RESULT][0]
    assert frame["summary"] == SUSPEND_PREFIX + plain, frame["summary"]
    tool_msgs = [m for m in llm.messages_per_round[1] if m.get("role") == "tool"]
    assert tool_msgs[0]["content"] == plain, (
        "显示前缀被写进了模型输入：挂起那一支的原文被改了")


# ---------- 2. 卡片的真相在 GET，且只在盘上那条记录里 ----------

def _parked(ts_mod, user_id, goal="下单要验证码"):
    task = ts_mod.Task(goal=goal, subtasks=["选店", "下单", "输验证码"], user_id=user_id)
    ts_mod.task_store[task.task_id] = task
    task.set_status(ts_mod.TaskStatus.RUNNING)
    task.wait_for_user("请输入短信验证码", cursor={
        "kind": "tool_suspension", "tool_name": PARK_TOOL,
        "arguments": {"item": "拿铁"}, "resume_state": {"step": 3}})
    return task


def test_the_get_surface_answers_the_park_from_the_disk_record(
        client, enforced, isolated_tasks):
    headers = enforced("停点甲")
    uid = client.get("/v1/auth/me", headers=headers).json()["user_id"]
    task = _parked(ts, uid)

    first = client.get(f"/v1/tasks/{task.task_id}", headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "waiting_user" and body["can_resume"] is True
    assert body["resume_cursor"]["question"] == "请输入短信验证码"
    assert body["resume_cursor"]["subtask_index"] == 0
    assert body["resume_cursor"]["subtask_total"] == 3
    assert body["waiting_since"] == task.waiting_since
    assert body["waiting_expires_at"] is None, "默认窗口 0 该是永不作废"
    assert body["progress_percent"] == 0.0

    on_disk = json.loads(isolated_tasks.read_text(encoding="utf-8"))["tasks"][task.task_id]
    assert on_disk["status"] == "waiting_user"
    assert on_disk["resume_cursor"]["question"] == "请输入短信验证码"

    # 重启过一次进程：内存里那份（以及任何随进程没的缓冲）全没了，只剩盘。
    # 只放过“现在挂了多久”那一格：它本来就是拿墙上时钟算的，两次读不是同一个数。
    stable = {k: v for k, v in body.items() if k != "waiting_elapsed_seconds"}
    ts.restore(path=str(isolated_tasks))
    second = client.get(f"/v1/tasks/{task.task_id}", headers=headers).json()
    second_stable = {k: v for k, v in second.items() if k != "waiting_elapsed_seconds"}
    assert second_stable == stable, (
        f"重启之后详情变了：{sorted(set(second_stable) ^ set(stable))} 或取值不同")
    assert second["resume_cursor"]["question"] == "请输入短信验证码", "问题只活在进程内存里"


# ---------- 3. 还活着只认 waiting_user，不认游标在不在 ----------

def test_the_answer_box_is_offered_only_while_the_status_is_waiting_user(
        client, enforced):
    headers = enforced("停点乙")
    uid = client.get("/v1/auth/me", headers=headers).json()["user_id"]

    live = _parked(ts, uid, "还在等")
    cancelled = _parked(ts, uid, "被取消")
    cancelled.mark_cancelled()
    failed = _parked(ts, uid, "失败了")
    failed.set_status(ts.TaskStatus.FAILED)

    bodies = {
        name: client.get(f"/v1/tasks/{t.task_id}", headers=headers).json()
        for name, t in (("live", live), ("cancelled", cancelled), ("failed", failed))
    }

    def offers_an_answer(b):
        # 卡片那条判据的机器形状：状态是 waiting_user 且不变式说可续。
        return b["status"] == "waiting_user" and b["can_resume"] is True

    assert offers_an_answer(bodies["live"]) is True
    for name in ("cancelled", "failed"):
        b = bodies[name]
        # A1 刻意留着游标（"上次停在哪儿"），`test_waiting_user_exits_to_every_allowed_state`
        # 钉的就是这一条；本票不推翻它。
        assert b["resume_cursor"] is not None, f"{name} 的游标被抹了：A1 的语义变了"
        assert b["can_resume"] is False
        assert offers_an_answer(b) is False, (
            f"{name} 还画着输入框：把「游标在」当成了「还活着」")
        # 反面：如果判据换成"游标在不在"，这一行就会与上一行矛盾——锁在这里有牙。
        assert (b["resume_cursor"] is not None) != offers_an_answer(b)
