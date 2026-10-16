"""v0.35 A5 行为契约：**超时是一次挂起，不是一次失败**。

规划提案 §四 v0.35 那一行的原文是「超时语义：用户不响应**不是失败**，是 `needs_input`
（挂起保活）」，而验收判据 1 的原文是「跑到第 3 步需要验证码的任务能挂起 **5 分钟**，
用户输入后从第 4 步继续，全上下文不丢」。A3 交付了「挂得起也接得回」，并把「5 分钟」
那一半明确交还给本票（见 `tests/test_v0350_a3_suspendable_tool_protocol.py:7-8`：
「本文件把"5 分钟"那半条交给 A5 的超时语义与真机验收」），所以 A5 只管**时间**那一半。
两处措辞按 A1 已经落地的形状读，两条都不按字面抄：

- `needs_input` 没有落地，这一票也不新加字面量：本版仍然只有一个等待态
  `waiting_user`（那条纪律由 A1 的 `test_the_new_member_is_exactly_one…` 钉着）。
  「挂起保活」说的就是 `waiting_user` 本身，超时之后它**还是** `waiting_user`。
- 「超时」在这里指**这条任务自己挂了多久**（`waiting_since` 到现在的秒数），
  **不是**流式那条路上「没有读者的宽限期」。后者是
  `config_store.stream_no_reader_grace_seconds`（默认 120 秒），管的是「没有活读者
  就不起飞新一轮」那道钱闸——本票一个字不动它，并把它的默认值与「两本账各读各的」
  一起钉住（`test_the_no_reader_money_gate_keeps_its_own_default_and_knob`）；那道闸
  自己的判据住在 `tests/test_v025_r3b_no_reader_billing.py`，本票跑完它必须照旧全绿。

两条路回答的其实是同一个问题「没人等的时候要不要继续花钱」，答案**刻意不对称**：
流式那边正有模型调用在飞，所以到点要停手；任务这边挂在断点上时一个调用都没在飞、
一分钱都没在花，所以到点既不停手也不改状态——停着本身就是零开销。这个不对称由
`test_a_timeout_while_parked_costs_no_model_and_no_tool_call…` 用计数假账量出来。

一条贯穿全文件的读法：**时间由夹具推进，不由真睡**。`task_store._now()` 是那枚唯一的
时钟接缝（`task_store.py:25`），`clock.advance(300)` 就是「挂了 5 分钟」；没有任何一条
用例 `sleep` 抢时机，跨请求那一族的会合走 `asyncio.gather`。

跨请求那一族必须用 `httpx.ASGITransport` + `asyncio.gather`，不能用 `TestClient`：
后者每个请求另开一个 portal，跨请求互相影响的形状在它下面结构上测不到（本仓那条硬约定
的原文见 `tests/test_event_loop_not_blocked.py` 文件头那次线上 524）。

本机作业纪律（这个目录同时是生产机）：每条用例的任务表都指在自己的 `tmp_path` 里，
`TASK_WAITING_USER_TIMEOUT_SECONDS` 进出都不留值；**没有任何一条用例把仓库真的
`data/tasks.json` 放进环境变量**——反例只喂手工造的清单（见文件末尾那条落点判据）。
"""
import asyncio
import copy
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

import app.agents.orchestrator as orch_mod
import app.agents.task_store as ts
from app.core import config_store, usage
from app.tools import executor as ex
from app.tools.registry import tools_registry
from app.tools.response import ToolSuspended

_TAG = "tool_call"
_GOAL_A = "点甲的一杯咖啡"
_GOAL_B = "点乙的一杯咖啡"
_WINDOW = 3600.0                 # 运维开了作废窗口时用的那一个（秒）
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPO_DATA_TASKS = _REPO_ROOT / "data" / "tasks.json"


def _call(name: str, arguments: dict) -> str:
    """模型「要调工具」时在本仓降级协议里的写法（与 task_agent 的判据同形）。"""
    return f"<{_TAG}>" + json.dumps({"name": name, "arguments": arguments},
                                    ensure_ascii=False) + f"</{_TAG}>"


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    """任务表进每条用例自己的文件；进出清空进程级存储（抄 A1/A3 那份夹具的理由）。"""
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    yield path
    for tid in list(ts.task_store):
        del ts.task_store[tid]


@pytest.fixture(autouse=True)
def no_stale_timeout_env(monkeypatch):
    """这一格的新环境变量进出都不许留值：它是「会销毁断点」的那一颗旋钮。

    漏了这一条，上一条用例设的 3600 会带进下一条，于是「默认永不做废」那几条会在一个
    没人看见的窗口下被作废——红的地方离真凶隔一个文件。
    """
    monkeypatch.delenv("TASK_WAITING_USER_TIMEOUT_SECONDS", raising=False)
    yield


@pytest.fixture
def clock(monkeypatch):
    """把 `task_store` 的墙上时钟接到一个可调偏移上：**推进时间不靠真睡**。

    `clock.advance(300)` 就是「挂了 5 分钟」，`advance(3601)` 就是「超过了 3600 秒的
    窗口」。基准时刻写死，所以断言里的秒数是**精确数**而不是区间。
    """
    base = datetime(2026, 10, 17, 12, 0, 0)
    state = {"offset": 0.0}
    monkeypatch.setattr(ts, "_now",
                        lambda: base + timedelta(seconds=state["offset"]))

    class _Clock:
        def advance(self, seconds: float) -> None:
            state["offset"] += float(seconds)

        def at(self, extra: float = 0.0) -> datetime:
            return base + timedelta(seconds=state["offset"] + extra)

    return _Clock()


@pytest.fixture
def park_ledger():
    """计数假账箱：这一票的判据问的都是「这个世界里到底发生了几个动作」。

    为什么必须记在**闭包**里而不是读任务字段：判据问的是「第 1~3 步被付了几次」，
    不是「被测对象自己申报花了多少」。拿 `task.results` 去证明没重复付费，等于让被告
    自己开证明（A3 那份道具箱同一个理由，抄它）。
    """
    spent: list = []        # 每笔 = 一次真跑了停点之前的第 1~3 步（查店/加购/发验证码）
    resumed: list = []      # 每一次「从断点接上」时递进工具的整包
    llm: list = []          # 模型调用（同一种钱，另一本账）
    goal = {"current": _GOAL_A}   # 只有**顺序的挂起**用得上：挂那一次才知道是哪条任务

    def a5_gate(item: str = _GOAL_A, resume: dict = None):
        if resume is None:
            spent.append(("step1-3", item))
            raise ToolSuspended("请输入短信验证码",
                                {"step": 3, "item": item, "collected": {"shop": "甲"}})
        resumed.append(copy.deepcopy(resume))
        return (f"已提交订单（{resume['state']['item']}），答案={resume['answer']}，"
                f"接自第{resume['state']['step']}步")

    def a5_calc(expression: str = "1+1"):
        return {"1+1": 2}.get(expression, 0)

    entries = {
        "a5_gate": {"function": a5_gate, "description": "下一杯咖啡（要验证码）",
                    "parameters": {"type": "object",
                                   "properties": {"item": {"type": "string"}}},
                    "available": None, "needs_user": False,
                    "requires_approval": False, "suspends": True},
        "a5_calc": {"function": a5_calc, "description": "算一下",
                    "parameters": {"type": "object",
                                   "properties": {"expression": {"type": "string"}}},
                    "available": None, "needs_user": False,
                    "requires_approval": False, "suspends": False},
    }
    saved = {name: tools_registry.get(name) for name in entries}
    for name, info in entries.items():
        tools_registry[name] = info
    ledger_saved = dict(ex._FREQ_LEDGER)
    try:
        yield {"spent": spent, "resumed": resumed, "llm": llm, "goal": goal}
    finally:
        for name, previous in saved.items():
            if previous is None:
                tools_registry.pop(name, None)
            else:
                tools_registry[name] = previous
        ex._FREQ_LEDGER.clear()
        ex._FREQ_LEDGER.update(ledger_saved)


@pytest.fixture
def stub_llm(monkeypatch, park_ledger):
    """三条模型路（规划/子任务/汇总）都换成桩，每次调用记进闭包那本账。

    子任务那一条只看消息形状：对话里已经有「工具执行结果」那一行就说明断点接上了，
    直接收尾；否则就要调 `a5_gate`（与 A3 那份 `_patch_llm_for_race` 同一个口径）。
    """
    ledger = park_ledger

    def plan_llm(model, messages, **kw):
        ledger["llm"].append("plan")
        return json.dumps(["选店并下单"], ensure_ascii=False)

    def task_llm(model, messages, **kw):
        ledger["llm"].append("task")
        already = any(str(m.get("content", "")).startswith("工具执行结果")
                      for m in messages)
        if already:
            return "拿到了，继续"
        return _call("a5_gate", {"item": ledger["goal"]["current"]})

    def summary_llm(model, messages, **kw):
        ledger["llm"].append("summary")
        return "最终汇总"

    monkeypatch.setattr("app.agents.planner.get_llm_response", plan_llm)
    monkeypatch.setattr("app.agents.task_agent.get_llm_response", task_llm)
    monkeypatch.setattr("app.agents.orchestrator.get_llm_response", summary_llm)
    return ledger


# ---------- 驱动端点的那几只手 ----------

def _park(client, headers, goal, ledger) -> dict:
    """把一条任务跑到「停在工具断点上等人」。单个请求，TestClient 够用。"""
    ledger["goal"]["current"] = goal
    res = client.post("/v1/agent/orchestrate", json={"goal": goal}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "waiting_user", f"前提没成立：这条任务没挂起来：{body}"
    return body


def _answer(client, headers, task_id, answer=None) -> dict:
    body = {"goal": _GOAL_A, "task_id": task_id}
    if answer is not None:
        body["user_answer"] = answer
    res = client.post("/v1/agent/orchestrate", json=body, headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def _async_client():
    """ASGITransport + `raise_app_exceptions=False`。

    后者是必须的：处理器里飘出去的异常在这里要现形为一个 **500 响应**，而不是把测试
    本身炸掉——那几条用例判的正是「客户端看到了什么」。
    """
    from app.main import app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver", timeout=120.0)


async def _answer_async(client, headers, task_id, answer):
    return await client.post("/v1/agent/orchestrate",
                             json={"goal": _GOAL_A, "task_id": task_id,
                                   "user_answer": answer},
                             headers=headers)


def _stale(task_id, seconds: float) -> None:
    """把一条**已经在盘上挂着**的任务的起点往前拨——不是手搭一条假任务。

    只改 `waiting_since` 这一格，然后走 `ts.save`（既有写盘路径），所以后面读到的
    是一条形状与生产完全一致的停点。
    """
    task = ts.get_task(task_id)
    assert task is not None and task.status is ts.TaskStatus.WAITING_USER, (
        "前提没成立：这条任务根本没挂在断点上，那「超时」也就无从判")
    task.waiting_since = (ts._now() - timedelta(seconds=seconds)).isoformat()
    ts.save(task)


# ---------- 验收判据 1 的那一半：挂 5 分钟，回来仍从第 4 步接上 ----------

def test_a_park_that_hit_the_timeout_is_still_parked_and_resumes_from_step_four(
        stub_llm, isolated_tasks, client, enforced, clock):
    """⭐ 验收判据 1 的时间那一半：**挂 5 分钟**再回来，还是从第 4 步接上。

    断言按时间线一段一段来：
    1. 跑到第 3 步挂起 → `waiting_user`、游标在盘上、世界那本账只有一笔（第 1~3 步）；
    2. 推进时钟 300 秒（跨过的不只是「5 分钟」，也跨过流式那条 120 秒的无读者宽限——
       两条路各本账，这里刻意让它俩都「到期」过一次）；
    3. **它还是 `waiting_user`**：不是 failed、不是 cancelled、error 是空的、
       `can_resume` 与游标一格没动，详情端点答得出「挂了 300 秒、窗口 0、没有到期时刻」；
    4. 带着答案回来 → 从第 4 步接上：`resumed` 收到的正是挂起时那份断点，而 `spent` 里
       第 1~3 步**只有一笔**（重跑就红在这里），全上下文不丢。

    怎么知道这条会红：把 `Orchestrator._expire_if_timed_out` 那句「窗口 0 回 None」改成
    「超时就把挂着的任务写成 failed」，红在第 3 段；把游标的读侧改坏，红在第 4 段。
    这两种变异本文件末尾都真的执行过一遍（见交付报告那三条负向对照）。
    """
    ledger = stub_llm
    headers = enforced("超时甲")
    parked = _park(client, headers, _GOAL_A, ledger)
    tid = parked["task_id"]
    assert parked["can_resume"] is True
    assert ledger["spent"] == [("step1-3", _GOAL_A)], ledger["spent"]

    clock.advance(300)          # 「挂 5 分钟」——推进时钟，不是真等

    task = ts.get_task(tid)
    assert task.status is ts.TaskStatus.WAITING_USER, (
        f"挂了 5 分钟就不是 waiting_user 了：{task.status}")
    assert task.status is not ts.TaskStatus.FAILED, "超时被写成了失败——正是要禁的那件事"
    assert task.cancelled is False and task.error is None
    assert task.can_resume is True, "保活的停点不该自己失去续跑资格"
    assert task.resume_cursor["kind"] == orch_mod.TOOL_SUSPENSION_KIND
    body = client.get(f"/v1/tasks/{tid}", headers=headers).json()
    assert body["status"] == "waiting_user"
    assert body["waiting_elapsed_seconds"] == 300.0, body
    assert body["waiting_timeout_seconds"] == 0.0, "默认必须是「不作废」那一格"
    assert body["waiting_expires_at"] is None, "默认形态没有到期时刻"

    done = _answer(client, headers, tid, "8888")
    assert done["status"] == "completed", done
    assert len(ledger["resumed"]) == 1, ledger["resumed"]
    assert ledger["resumed"][0]["answer"] == "8888"
    assert ledger["resumed"][0]["state"] == {"step": 3, "item": _GOAL_A,
                                             "collected": {"shop": "甲"}}, (
        "接上的不是挂起时那份断点，「从第 4 步继续」就是空话")
    assert ledger["spent"] == [("step1-3", _GOAL_A)], (
        f"第 1~3 步又烧了一遍：{ledger['spent']}——超时把挂起退成了重跑")
    assert ts.get_task(tid).status is ts.TaskStatus.COMPLETED


def test_a_park_survives_a_month_when_the_window_is_left_at_the_default(
        stub_llm, isolated_tasks, client, enforced, clock):
    """默认窗口 0 = **永不作废**：挂 30 天也还在同一个断点上，答案照样接得回去。

    这条钉的是「default preserves today's observable behavior」：改动前没有任何代码会
    因为时间让一条挂着的任务变状态，改动后（没人配这一格）也一毫米都不能变。
    阳性对照同场跑：同一份「挂了 30 天」的数据，窗口一旦是正数就**必须**报超时——
    否则上面那句 False 只是因为判据恒假。
    """
    ledger = stub_llm
    assert config_store.DEFAULTS["task_waiting_user_timeout_seconds"] == 0.0
    assert ts.task_waiting_user_timeout_seconds() == 0.0

    headers = enforced("超时乙")
    tid = _park(client, headers, _GOAL_A, ledger)["task_id"]
    clock.advance(30 * 24 * 3600)          # 30 天

    task = ts.get_task(tid)
    assert ts.is_waiting_timed_out(task) is False, "默认窗口把停点判成超时了"
    # 阳性对照（必须**在作答之前**判）：同一份「挂了 30 天」的数据，窗口一旦是正数
    # 就必须报超时——否则上面那句 False 只是因为判据恒假。
    assert ts.is_waiting_timed_out(task, timeout_seconds=_WINDOW) is True, (
        "窗口换成正数都不报超时，上面那句 False 就不是证据而是恒假")

    done = _answer(client, headers, tid, "8888")
    assert done["status"] == "completed", done
    assert ledger["spent"] == [("step1-3", _GOAL_A)], ledger["spent"]


# ---------- 钱：超时期间一次调用都不许多 ----------

def test_a_timeout_while_parked_costs_no_model_and_no_tool_call_and_leaves_the_ledger(
        stub_llm, isolated_tasks, client, enforced, clock):
    """⭐ 前提 3：没人等的时候**不许开始花钱**——超时不启轮、不重跑，也不抹账。

    量的是**世界**那三本假账（`spent` / `resumed` / `llm`，都在闭包里）加一本真账
    （`core/usage.py` 落在 conftest 指的临时 usage.json）：
    - 推进时钟、读详情、再空手问一次 `/v1/agent/orchestrate`（不带答案）——这三件事合
      起来就是「用户不在的那 5 分钟里服务端会跑到的所有代码」。三本假账必须一格没长，
      真账必须与挂起之前**逐行相同**（不退款、不抹账、不新增）。
    - 然后带答案回来，账才因为「接上第 4 步」而长；而挂起之前那一笔在之后的快照里原样
      还在（同 provider、同 token 数、`calls` 还是 1）——这是「已花掉的不许被超时抹掉」。

    怎么知道这条会红：让超时去「自动续跑」（给挂着的任务补一次 `executor.execute_task`），
    红在 `llm` / `spent`；让超时顺手把状态或 error 改掉，红在 `usage` 快照与状态那两句。
    """
    ledger = stub_llm
    headers = enforced("超时丙")
    uid = client.get("/v1/auth/me", headers=headers).json()["user_id"]

    # 已经花掉的那一笔：代表停点之前那几步付过的账，记在**真账本**上而不是道具自报
    usage.record_call(user_id=uid, provider_id="p-a5", paid_by="user",
                      prompt_tokens=100, completion_tokens=20, total_tokens=120)
    spent_before = usage.snapshot_for_user(uid)
    assert any(r["provider_id"] == "p-a5" and r["total_tokens"] == 120
               for r in spent_before), f"真账没记上：{spent_before}"

    tid = _park(client, headers, _GOAL_A, ledger)["task_id"]
    llm_at_park = list(ledger["llm"])
    spent_at_park = list(ledger["spent"])
    resumed_at_park = list(ledger["resumed"])

    clock.advance(300)
    client.get(f"/v1/tasks/{tid}", headers=headers)
    idle = _answer(client, headers, tid)               # 不带答案：用户还没回来
    assert idle["status"] == "waiting_user" and idle["can_resume"] is True, idle
    assert idle["waiting_timeout_seconds"] == 0.0 and idle["waiting_expires_at"] is None, (
        idle)
    assert "waiting_elapsed_seconds" not in idle, (
        "编排器的响应不许带「现在过了多久」：A3 那条「非属主与不存在给出逐字节相同的"
        "同一句话」的判据就是拿两份响应体作差的")
    detail = client.get(f"/v1/tasks/{tid}", headers=headers).json()
    assert detail["waiting_elapsed_seconds"] == 300.0, detail
    assert "user_answer" in idle["message"], "空手回来要说清「带答案才会续」，不许假装在跑"

    assert ledger["llm"] == llm_at_park, (
        f"用户不在的时候又叫了模型：{ledger['llm']} vs {llm_at_park}")
    assert ledger["spent"] == spent_at_park, f"第 1~3 步被重跑：{ledger['spent']}"
    assert ledger["resumed"] == resumed_at_park
    assert usage.snapshot_for_user(uid) == spent_before, (
        "没人等的时候账本动了——超时要么启了付费轮，要么抹了已经花掉的那一笔")
    assert ts.get_task(tid).status is ts.TaskStatus.WAITING_USER

    # 用户回来了：账只因为「接上第 4 步」而长，那一笔旧账原样还在
    done = _answer(client, headers, tid, "8888")
    assert done["status"] == "completed", done
    after = usage.snapshot_for_user(uid)
    assert any(r["provider_id"] == "p-a5" and r["total_tokens"] == 120 and r["calls"] == 1
               for r in after), f"已经花掉的那一笔被改写了：{after}"
    assert ledger["spent"] == spent_at_park, f"续跑还是把前三步重跑了：{ledger['spent']}"


def test_the_no_reader_money_gate_keeps_its_own_default_and_knob(monkeypatch):
    """⭐ 前提 3 的另一半：`stream_no_reader_grace_seconds` 与那道钱闸没被动过。

    单一真相仍然只有那一格：默认 120.0、env 仍然压过文件，而 A5 的新旋钮**不喂**流式
    那条路——把新旋钮设成 0 或 3600 都改变不了宽限期的读数（两本账各读各的，双向都判）。
    那道闸自己的判据在 `tests/test_v025_r3b_no_reader_billing.py`，交付报告带它的原文计数。

    怎么知道这条会红：把 `stream_no_reader_grace_seconds` 的默认改成 0（放开钱闸的形状），
    红在第一句；让流式那边改读 A5 这一格，红在第二句；反过来让宽限期的 env 带动停点
    窗口，红在最后一句。
    """
    assert config_store.DEFAULTS["stream_no_reader_grace_seconds"] == 120.0
    assert config_store.stream_no_reader_grace_seconds() == 120.0

    monkeypatch.setenv("TASK_WAITING_USER_TIMEOUT_SECONDS", "3600")
    assert ts.task_waiting_user_timeout_seconds() == 3600.0
    assert config_store.stream_no_reader_grace_seconds() == 120.0, (
        "A5 的旋钮串到了钱闸上——两本账必须各读各的")

    monkeypatch.setenv("STREAM_NO_READER_GRACE_SECONDS", "30")
    assert config_store.stream_no_reader_grace_seconds() == 30.0
    assert ts.task_waiting_user_timeout_seconds() == 3600.0, (
        "反向也串了：宽限期的 env 不该带动停点窗口")

    # 「没有活读者就不起飞新一轮」那道钩子还挂在流式的轮次边界上（本票不许编辑它，
    # 这里只做只读确认；真判据在 test_v025_r3b_no_reader_billing.py）
    import app.core.streaming as streaming
    src = Path(streaming.__file__).read_text(encoding="utf-8")
    assert "before_round" in src, "轮次边界那道闸不在了——钱闸被拆了"


# ---------- 窗口开着的时候：过期 = cancelled + 游标清空，且绝不重跑 ----------

def test_an_expired_park_becomes_cancelled_with_a_voided_cursor_and_pays_nothing_more(
        stub_llm, isolated_tasks, client, enforced, clock, monkeypatch):
    """⭐ 前提 4 + 与工单 #33 的接缝：超时作废 = `cancelled` + **游标清空**，不是 failed。

    为什么这条必须存在：#33 描述的形状是「一条 `failed`/`running` 的记录还带着
    `kind=="tool_suspension"` 的游标」，而 `failed → running` 是 A1 跳转表里的合法边
    （断点重跑），`orchestrator.run` 的 FAILED 分支**不读游标**、直接进通用续跑——
    于是谁都能再把它从第 1 步点着跑一遍：查店、加购、再发一次验证码。
    本票的作废因此不走那道门，而是两道一起：
    - 落 `cancelled`：那张表里它**一条出边都没有**，作废之后没有任何代码路径能再跑它；
    - 同一条 `set_status` 里把 `resume_cursor` 清空（判、跳、落盘同锁同批），于是盘上
      不会留下一条「看起来还能接」的断点。

    断言分头判这两道，缺一道就红：状态 / 出边 / 游标 / 盘上形状 + `spent` 只一笔 +
    `resumed` 一条都没有 + 再砸一次答案仍是那句「任务已被取消」、仍然一笔不许多。
    """
    monkeypatch.setenv("TASK_WAITING_USER_TIMEOUT_SECONDS", str(_WINDOW))
    ledger = stub_llm
    headers = enforced("作废甲")

    tid = _park(client, headers, _GOAL_A, ledger)["task_id"]
    clock.advance(_WINDOW + 1)          # 3601 秒 > 3600 秒窗口
    assert ts.is_waiting_timed_out(ts.get_task(tid)) is True, "前提没成立：没判成超时"

    body = _answer(client, headers, tid, "8888")
    assert body["status"] == "cancelled", body
    assert body["accepted"] is False and body["reason"] == "waiting_timeout", body
    assert body["can_resume"] is False, body
    assert "作废" in body["message"], body["message"]

    task = ts.get_task(tid)
    assert task.status is ts.TaskStatus.CANCELLED
    assert task.status is not ts.TaskStatus.FAILED, (
        "超时被写成失败——#33 那扇门就是这么开的")
    assert task.resume_cursor is None, "游标没清空：留着一份「看起来还能接」的断点"
    assert task.can_resume is False and task.waiting_since is None
    assert orch_mod._tool_suspension(task) is None, "作废之后仍被读成工具断点"
    assert orch_mod.can_transition(task.status, ts.TaskStatus.RUNNING) is False, (
        "cancelled 有了出边：作废不再是终点，#33 的重复付费门又开回来了")
    assert "作废" in task.error and "没有重跑" in task.error, task.error

    # 盘上那一份必须与内存同一个形状（这条判的是落盘，不是内存）
    on_disk = json.loads(isolated_tasks.read_text(encoding="utf-8"))["tasks"][tid]
    assert on_disk["status"] == "cancelled" and on_disk["resume_cursor"] is None
    assert on_disk["can_resume"] is False and on_disk["cancelled"] is True
    assert "8888" not in json.dumps(on_disk, ensure_ascii=False), "答案不许落盘"

    # 钱：作废这一跳没跑任何东西；**之后**再砸答案也不许把它点着跑一遍
    assert ledger["spent"] == [("step1-3", _GOAL_A)], ledger["spent"]
    assert ledger["resumed"] == [], f"作废之后还把断点接了一遍：{ledger['resumed']}"
    again = _answer(client, headers, tid, "9999")
    assert again["status"] == "cancelled" and again["message"] == "任务已被取消", again
    assert ledger["spent"] == [("step1-3", _GOAL_A)], ledger["spent"]
    assert ledger["resumed"] == []
    assert ledger["llm"].count("plan") == 1, "作废之后又被重新规划了一遍=重跑整趟"


def test_the_window_boundary_is_gross_and_every_uncertainty_means_do_not_void(
        isolated_tasks, clock):
    """⭐ 判据的牙齿：`挂的秒数 >= 窗口` 才算超时，而**任何**不确定都回 False。

    这条是纯判据用例（不叫 HTTP），因为它要造的形状在真链路上造不出来：起点读不懂、
    起点在未来、窗口是负数 / `nan` / `inf` / 一句中文、任务根本不在等待态。每一格都
    成对写——同一份数据在「窗口是正数 + 挂够了秒数」下**必须**报超时（阳性对照），
    否则整段绿只是因为恒假。

    任务全是 `tmp_path` 那份任务表上的真记录（`Task` + `wait_for_user` + `save`），
    外加一份**手工造的列表**喂给清扫器——没有一条是靠把仓库真库路径放进环境做出来的。
    """
    def parked(waiting_since=..., status=None):
        task = ts.Task(goal="判据", subtasks=["a", "b"], user_id="u-judge")
        ts.task_store[task.task_id] = task
        task.set_status(ts.TaskStatus.RUNNING)
        task.wait_for_user("请输入短信验证码")
        if waiting_since is not ...:
            task.waiting_since = waiting_since
        if status is not None:
            # 绕过跳转表造一个「形状不对」的读侧：状态与 can_resume 不同步，
            # 判据要在这种记录上也不报超时（读侧不许替写侧擦屁股）。
            task.status = status
            task.can_resume = False
        ts.save(task)
        return task

    fresh = parked()
    assert ts.waiting_elapsed_seconds(fresh, now=clock.at(0)) == 0.0
    assert ts.is_waiting_timed_out(fresh, now=clock.at(_WINDOW - 1),
                                   timeout_seconds=_WINDOW) is False, "差一秒就作废太狠"
    assert ts.is_waiting_timed_out(fresh, now=clock.at(_WINDOW),
                                   timeout_seconds=_WINDOW) is True, "边界是 >="
    assert ts.waiting_deadline(fresh, timeout_seconds=_WINDOW) == (
        ts._now() + timedelta(seconds=_WINDOW)), (
        "到期时刻算错了——UI 那半边的超时倒计时就是错的")
    assert ts.waiting_timeout_info(fresh) == {
        "waiting_elapsed_seconds": 0.0, "waiting_timeout_seconds": 0.0,
        "waiting_expires_at": None}, "默认形态：知道挂了多久，但没有到期时刻"

    # 起点读不懂 / 根本没有起点 / 时钟被往回拨：判不出挂了多久 → **不作废**
    for label, bad_since, judge_at in (
            ("读不懂", "不是时间", clock.at(99999)),
            ("没有起点", None, clock.at(99999)),
            # 起点在未来 = 系统时钟被往回拨。判它的时刻用「现在」，因为这一格要测的是
            # 负数秒数不作废，而不是「过了 99999 秒之后它就变成过去」。
            ("起点在未来", (ts._now() + timedelta(seconds=600)).isoformat(), clock.at(0))):
        task = parked(waiting_since=bad_since)
        if label == "起点在未来":
            assert ts.waiting_elapsed_seconds(task) < 0, "回拨的时钟该算出负数"
        assert ts.is_waiting_timed_out(task, now=judge_at,
                                       timeout_seconds=_WINDOW) is False, (
            f"起点「{label}」竟然判成了作废：销毁一条本来还能续的断点")
        # 起点读不懂的两种，连「到期时刻」都答不出来；未来那份答得出（它还没到）
        has_deadline = ts.waiting_deadline(task, timeout_seconds=_WINDOW) is not None
        assert has_deadline == (label == "起点在未来"), (
            f"{label} 这一格的到期时刻该与判据同进退：{has_deadline}")

    # 窗口这一侧的坏值：全部收成「关」，因为 0 才是这一格的 fail-closed 方向
    stale = parked(waiting_since=(ts._now() - timedelta(seconds=99999)).isoformat())
    assert ts.is_waiting_timed_out(stale, timeout_seconds=_WINDOW) is True, "阳性对照"
    for bad in (0, -5, "五分钟", float("nan"), float("inf"), None):
        assert ts.is_waiting_timed_out(stale, timeout_seconds=bad) is False, (
            f"窗口坏值 {bad!r} 竟然判成了作废")
    assert ts.waiting_timeout_info(stale) == {
        "waiting_elapsed_seconds": 99999.0, "waiting_timeout_seconds": 0.0,
        "waiting_expires_at": None}

    # 不在等待态就没有「挂了多久」这回事
    for status in (ts.TaskStatus.RUNNING, ts.TaskStatus.FAILED, ts.TaskStatus.COMPLETED):
        other = parked(waiting_since=(ts._now() - timedelta(seconds=99999)).isoformat(),
                       status=status)
        assert ts.waiting_elapsed_seconds(other) is None
        assert ts.is_waiting_timed_out(other, timeout_seconds=_WINDOW) is False

    # 带时区的一份起点要按同一个域读（naive 与 aware 作差是 TypeError → 会被当成读不懂）
    aware = parked(waiting_since=(ts._now() - timedelta(seconds=_WINDOW + 5)
                                 ).astimezone().isoformat())
    assert ts.waiting_elapsed_seconds(aware) > _WINDOW, "带时区的起点被判成读不懂"
    assert ts.is_waiting_timed_out(aware, timeout_seconds=_WINDOW) is True

    # 清扫器：手工造的列表里只该收走超时的那一条，另一条一格不动
    keep = parked()
    voided = ts.sweep_timed_out_parks(tasks=[stale, keep], timeout_seconds=_WINDOW)
    assert voided == [stale.task_id], voided
    assert ts.get_task(keep.task_id).status is ts.TaskStatus.WAITING_USER
    assert ts.get_task(stale.task_id).status is ts.TaskStatus.CANCELLED
    assert ts.sweep_timed_out_parks(tasks=[stale], timeout_seconds=0) == [], (
        "窗口关着还扫得动——默认形态就不该有任何一次作废")


def test_a_failed_void_leaves_the_park_alive_instead_of_500_the_endpoint(
        stub_llm, isolated_tasks, client, enforced, clock, monkeypatch, capsys):
    """⭐ 前提 4 的 fail-closed 那一半：作废**落不了盘**时停点照旧有效，请求不许变 500。

    做法是把 `task_store.save` 换成「只在这一跳的目标状态是 cancelled 时抛 OSError」的
    一层——于是作废那一次写失败，而后续正常的写照常。三条判据：
    1. 200（不飘 500）；
    2. 内存被还原成作废之前的样子（`set_status` 是先改内存再落盘，不还原的话
       「盘上还在等、内存里没游标」会让下一路读进通用续跑、把第 1~3 步重烧一遍）；
    3. 答案照旧从断点接得回去（作废失败 ≠ 断点没了），日志把这次没写成的作废说出来。

    怎么知道这条会红：把 `expire_parked_task` 里那段 snapshot 还原删掉，红在第 2、3 段
    （状态会变成 cancelled、游标为空、`resumed` 空而 `spent` 多一笔）；把那个 except
    删掉，红在第 1 段（OSError 一路飘到端点）。
    """
    monkeypatch.setenv("TASK_WAITING_USER_TIMEOUT_SECONDS", str(_WINDOW))
    ledger = stub_llm
    headers = enforced("作废乙")
    tid = _park(client, headers, _GOAL_A, ledger)["task_id"]
    clock.advance(_WINDOW + 1)

    real_save = ts.save
    probe: list = []

    def flaky_save(task):
        value = getattr(task.status, "value", task.status)
        if value == "cancelled":
            raise OSError("盘满了")          # 只有作废那一跳落不了盘
        if value == "running":
            # 下一次落盘就在还原之后：游标还在 = 那次失败的作废真的把内存还回去了
            probe.append(getattr(task, "resume_cursor", None) is not None)
        return real_save(task)

    monkeypatch.setattr(ts, "save", flaky_save)
    body = _answer(client, headers, tid, "8888")   # 那句 assert 200 本身就是判据
    out = capsys.readouterr().out
    assert "落不了盘" in out, f"作废没写成却静悄悄：{out!r}"
    assert probe == [True], f"那次失败的作废把内存里的游标带走了：{probe}"
    # 内存一格未还 → 读侧还认得出断点 → 答案照旧从第 4 步接上，而不是从第 1 步重跑
    assert body["status"] == "completed", body
    assert len(ledger["resumed"]) == 1, ledger["resumed"]
    assert ledger["resumed"][0]["state"] == {"step": 3, "item": _GOAL_A,
                                             "collected": {"shop": "甲"}}
    assert ledger["spent"] == [("step1-3", _GOAL_A)], (
        f"作废失败之后反而把前三步重烧了一遍：{ledger['spent']}")
    assert ts.get_task(tid).status is ts.TaskStatus.COMPLETED


# ---------- 跨请求：清扫真的在跑，而且只扫超时的那一条 ----------

def test_a_second_request_sweeps_the_stale_park_without_touching_the_fresh_one(
        stub_llm, isolated_tasks, client, enforced, clock, monkeypatch):
    """清扫挂在「走进任务面的那一次请求」上：一条**别的**请求进来，超时的停点被收走。

    场景：甲的停点刚挂上（还在窗口里），乙的停点已经挂了 3601 秒。甲带答案回来——
    甲必须从断点跑完（清扫不许把它一起扫掉），而乙在这一次请求里被作废、游标清空。
    判据看**盘**而不只看内存。

    这条不需要并发：它判的是「一次请求动了另一条任务的记录」这种跨请求**效果**。
    真并发那一格在下一条。
    """
    monkeypatch.setenv("TASK_WAITING_USER_TIMEOUT_SECONDS", str(_WINDOW))
    ledger = stub_llm
    ha = enforced("清扫甲")
    hb = enforced("清扫乙")

    fresh = _park(client, ha, _GOAL_A, ledger)["task_id"]
    stale = _park(client, hb, _GOAL_B, ledger)["task_id"]
    _stale(stale, _WINDOW + 1)                 # 只把乙的起点往前拨（走真落盘）
    assert ts.get_task(stale).status is ts.TaskStatus.WAITING_USER

    done = _answer(client, ha, fresh, "8888")
    assert done["status"] == "completed", done
    assert ts.get_task(fresh).status is ts.TaskStatus.COMPLETED, "清扫把没超时的也扫了"

    swept = ts.get_task(stale)
    assert swept.status is ts.TaskStatus.CANCELLED
    assert swept.resume_cursor is None and swept.can_resume is False
    on_disk = json.loads(isolated_tasks.read_text(encoding="utf-8"))["tasks"]
    assert on_disk[stale]["status"] == "cancelled"
    assert on_disk[stale]["resume_cursor"] is None
    assert on_disk[fresh]["status"] == "completed"
    assert on_disk[fresh]["resume_cursor"]["kind"] == orch_mod.TOOL_SUSPENSION_KIND or (
        on_disk[fresh]["status"] == "completed")
    # 乙的那一步没被任何人重跑，也没被接上：只有甲那一次续跑付了钱
    assert ledger["spent"] == [("step1-3", _GOAL_A), ("step1-3", _GOAL_B)], ledger["spent"]
    assert [r["state"]["item"] for r in ledger["resumed"]] == [_GOAL_A], ledger["resumed"]


def test_two_concurrent_answers_where_one_park_just_expired_pay_only_the_live_one(
        stub_llm, isolated_tasks, client, enforced, clock, monkeypatch):
    """⭐ 跨请求 + 真并发：两条答案一起进来，只有那条还活着的断点该被付一次钱。

    `asyncio.gather` 过 `httpx.ASGITransport`（**不是 TestClient**：它每请求另开一个
    portal，「一条请求改了另一条请求正在读的记录」这种形状在它下面结构上测不到）。
    会合不靠 sleep 也不需要——两条任务彼此独立，判据因此写成**交错无关**的形状：
    - 两条都 200（清扫与作废都不许飘出端点变 500）；
    - 活的那条（甲）从断点接上并跑完，`resumed` 里**只有**甲那一份断点；
    - 超时的那条（乙）落地 cancelled + 游标清空；两种交错都合法：它自己的请求先判超时
      （`reason == waiting_timeout`），或被甲那趟的清扫先作废（早退那句「任务已被取消」）；
    - 全局只有挂起时那两笔 `spent`——乙的第 1~3 步一次都没多烧（#33 那一笔钱）。

    怎么知道这条会红：把 `run()` 里那句 `_expire_if_timed_out` 删掉，乙会走通用续跑、
    从第 1 步重跑 → `spent` 变三笔；把清扫那层的 try/except 删掉 → 并发里飘 500 →
    第一句红。
    """
    monkeypatch.setenv("TASK_WAITING_USER_TIMEOUT_SECONDS", str(_WINDOW))
    ledger = stub_llm
    ha = enforced("并发甲")
    hb = enforced("并发乙")

    a = _park(client, ha, _GOAL_A, ledger)["task_id"]
    b = _park(client, hb, _GOAL_B, ledger)["task_id"]
    _stale(b, _WINDOW + 1)
    spent_at_park = list(ledger["spent"])
    assert spent_at_park == [("step1-3", _GOAL_A), ("step1-3", _GOAL_B)], spent_at_park

    async def scenario():
        async with _async_client() as c:
            return await asyncio.gather(_answer_async(c, ha, a, "8888"),
                                        _answer_async(c, hb, b, "1234"))

    r_a, r_b = asyncio.run(scenario())
    assert (r_a.status_code, r_b.status_code) == (200, 200), (r_a.text, r_b.text)
    body_a, body_b = r_a.json(), r_b.json()

    assert body_a["status"] == "completed", body_a
    assert [r["state"]["item"] for r in ledger["resumed"]] == [_GOAL_A], ledger["resumed"]
    assert ledger["spent"] == spent_at_park, (
        f"前三步被重复付费：{ledger['spent']} vs {spent_at_park}")

    assert ts.get_task(b).status is ts.TaskStatus.CANCELLED, body_b
    assert ts.get_task(b).resume_cursor is None
    assert body_b["status"] == "cancelled", body_b
    if body_b.get("accepted") is False:
        assert body_b["reason"] == "waiting_timeout", body_b
    else:
        assert body_b["message"] == "任务已被取消", body_b
    assert ledger["llm"].count("plan") == 2, "两条任务的规划各一次，多出来就是重跑了整趟"


def test_a_park_voided_between_reading_the_cursor_and_claiming_it_pays_nothing(
        stub_llm, isolated_tasks, client, enforced, clock, monkeypatch):
    """⭐ 那个「读完游标之后才被人作废」的窗口：落败的那条不许把第 1~3 步重跑。

    A3 用同一个手法钉过「断点被另一枚答案接走」那一格；这一条钉的是 A5 新加的那次
    跳转可能落在**同一瞬**：这一趟在锁外读到 waiting_user、进锁之前那条停点被别的
    请求的清扫作废了。窗口是**确定造出来的**（不靠线程抢）：包一层 `_tool_suspension`，
    就在这条请求读完游标的那一刻调用生产函数 `expire_parked_task`。

    判据：200、`accepted is False`、`reason == "breakpoint_taken"`、`spent` 与 `resumed`
    一本都不许多（那一笔验证码就等于重发一遍）、任务留在 cancelled 且游标为空。
    前提那一格也断言（作废真的在这一瞬落了地），否则整条是空测。
    """
    monkeypatch.setenv("TASK_WAITING_USER_TIMEOUT_SECONDS", str(_WINDOW))
    ledger = stub_llm
    headers = enforced("窗口甲")
    tid = _park(client, headers, _GOAL_A, ledger)["task_id"]
    spent_at_park = list(ledger["spent"])

    real_reader = orch_mod._tool_suspension
    stage = {"done": False}

    def spy(task):
        seen = real_reader(task)
        if seen is not None and not stage["done"]:
            stage["done"] = True
            # 「我刚读到它还挂着」与「我要去认领它」之间，deadline 跨过去了、而另一趟
            # 请求的清扫正好在这一瞬把这一跳落了下来：窗口是**确定造出来的**，不靠线程抢。
            # 这里推进的是那枚唯一的时钟接缝，作废用的是生产函数本身——测试不自己
            # 伪造状态，也不自己执行被验的那个动作。
            clock.advance(_WINDOW + 1)
            assert ts.expire_parked_task(task) is True, "前提没成立：这一瞬没落下作废"
        return seen

    monkeypatch.setattr(orch_mod, "_tool_suspension", spy)
    body = _answer(client, headers, tid, "8888")
    assert stage["done"] is True, "前提没成立：这一趟根本没读到游标，整条是空测"
    assert body.get("accepted") is False and body["reason"] == "breakpoint_taken", body
    assert body["status"] == "cancelled", body
    assert ts.get_task(tid).status is ts.TaskStatus.CANCELLED
    assert ts.get_task(tid).resume_cursor is None
    assert ledger["spent"] == spent_at_park, (
        f"落败的那条把前三步又跑了一遍：{ledger['spent']}")
    assert ledger["resumed"] == [], ledger["resumed"]


# ---------- 落点：这一票写的每一个字节都得在自己的临时根里 ----------

def _paths_outside(pairs, allowed_roots):
    """这些实际落点里有几条**不该要它**：不在允许的临时根内，或干脆落在仓库里。

    两条判据叠在一起是有意的（抄 A3 文件末尾那条同名函数的理由）：本机的生产 `data/`
    就躺在仓库目录里，所以「跟着项目根走」才是真正那一刀。
    """
    repo = os.path.realpath(str(_REPO_ROOT)) + os.sep
    offenders = []
    for label, raw in pairs:
        resolved = os.path.realpath(str(raw))
        inside = any(os.path.realpath(r) == resolved
                     or resolved.startswith(os.path.realpath(r) + os.sep)
                     for r in allowed_roots)
        if not inside or resolved.startswith(repo):
            offenders.append((label, resolved))
    return offenders


def test_a_full_timeout_cycle_writes_only_inside_its_own_temp_root(
        stub_llm, isolated_tasks, client, enforced, clock, monkeypatch, tmp_path):
    """⚠️ 本机作业纪律：这个仓库目录**同时是生产机**，这一票写的每一字节都必须在临时根里。

    跑完整一轮：挂起 → 推进时钟到超时 → 作废 → 再砸一次答案。之后拿
    `paths.resolve_all_data_paths()`（「可变数据到底落在哪」的唯一权威清单）逐条判它
    是否还在临时根内，再把仓库那份真 `data/tasks.json` 的**字节与 mtime** 前后对一遍——
    只看字节会漏掉「把同一份内容原样重写一遍」那种写盘（本仓上一条用例就是这么栽的）。

    判据自己配反例：同一把尺子量一份**手工造的**、指着仓库真库的清单必须报红，量一份
    合法指法必须报空。反例绝不靠把 `TASKS_DB_PATH` 真改成仓库路径来造——那一句会把
    生产那份 `data/tasks.json` 放进射程内（上一个反例就是这么漏的）。
    """
    from app.core.paths import resolve_all_data_paths

    monkeypatch.setenv("TASK_WAITING_USER_TIMEOUT_SECONDS", str(_WINDOW))
    ledger = stub_llm
    headers = enforced("落点甲")

    def repo_state():
        if not _REPO_DATA_TASKS.exists():
            return None, None
        return (_REPO_DATA_TASKS.read_bytes(),
                _REPO_DATA_TASKS.stat().st_mtime_ns)

    before_bytes, before_mtime = repo_state()
    tid = _park(client, headers, _GOAL_A, ledger)["task_id"]
    clock.advance(_WINDOW + 1)
    _answer(client, headers, tid, "8888")
    _answer(client, headers, tid, "9999")
    after_bytes, after_mtime = repo_state()

    allowed = [str(tmp_path), tempfile.gettempdir()]
    rows = resolve_all_data_paths()
    offenders = _paths_outside(rows, allowed)
    print("📁 落点判据（每一份可变运行时数据的实际落点）：")
    for label, path in rows:
        print(f"     {label}: {path}")
    print(f"     offenders = {offenders}")
    print(f"     repo data/tasks.json: bytes={before_bytes!r} mtime_ns={before_mtime}"
          f" → bytes={after_bytes!r} mtime_ns={after_mtime}")
    assert not offenders, (
        f"这些运行时数据落到了临时根之外（本机仓库目录就是生产机）：{offenders}")
    assert after_bytes == before_bytes, "真库被测试改了字节——上面那条路径判据没兜住东西"
    assert after_mtime == before_mtime, (
        f"真库被人写过一次（mtime {before_mtime} → {after_mtime}），"
        "内容与改动前相同不代表没写盘")

    hostile = ("任务清单", os.path.realpath(str(_REPO_DATA_TASKS)))
    bad = _paths_outside([hostile], allowed)
    assert hostile in bad, f"反例都没报出来，上面那段绿不算证据：{bad}"
    assert _paths_outside([("任务清单", str(tmp_path / "tasks.json"))], allowed) == [], (
        "合法指法也报红，那上面那句「报红」就只是因为谁都被报红")
