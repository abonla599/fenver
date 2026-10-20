"""工单 #38：被拒的续跑不再扣用户自己的付费上游额度，但也没变成免费无限信道。

缺陷形状：`POST /v1/agent/orchestrate` 一进门就 `throttle_paid_upstream`（60 秒 20 次，
与聊天共用），**然后**才进编排器认领。于是 `accepted:false` 的那一族（断点已被另一条
请求接走、任务正在飞）——没跑工具、没叫模型、一分钱没花——每次重试都吃掉一枚本该留给
聊天的令牌。一个客户端照着 `message` 里的"稍后再试"重试几轮，用户自己的对话就先 429 了。

本票的裁决（写在 `orchestrator.run` 的 `charge_upstream` 与
`auth_router.throttle_orchestrate_attempt` 两处）：
1. **付费那本账只在"真要开工"时记**，且由编排器在两种时刻各调一次：新任务在
   `planner.plan()` 之前，恢复/续跑在认领成功之后、翻 running 之前。被拒的路径一次
   都走不到那里——于是"没花钱就不记账"不是一句承诺，是调用点位置本身。
2. **防刷另开一本**：每条尝试（含注定被拒的）记 `_ORCHESTRATE_ATTEMPTS`，60 秒 60 次。
   它不扣钱，只拦"无限敲"。

本文件要证明的四件事：
- 反面：被拒的一次，付费账本与模型调用账都纹丝不动（**no money spent, no debit**）；
- 正面：同一枚答案在阻因消失后重试会被接受，且只记**一笔**（legitimate retry）；
- 刷：连续被拒到第 61 次才 429，付费账本仍然一格未动；正常人的 20 次聊天预算还在；
- 钱闸没被搬软：付费账本满时，新建任务在规划**之前** 429（不建任务、不叫模型），
  续跑在认领**之后** 429（状态还是 waiting_user，执行权已还，稍后能再答）。
"""
import json

import pytest

import app.agents.orchestrator as orch_mod
from app.agents import task_store as ts
from app.core import auth_router as ar
from app.tools.registry import tools_registry
from app.tools.response import ToolSuspended

_TAG = "tool_call"
_GOAL = "点一杯咖啡"
_IP = "127.0.0.1"           # conftest 的 client 对端写成回环，与现网同形
_UID = "default_user"       # AUTH_MODE=disabled 下的本机管理员


def _call(name: str, arguments: dict) -> str:
    return f"<{_TAG}>" + json.dumps({"name": name, "arguments": arguments},
                                   ensure_ascii=False) + f"</{_TAG}>"


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    """任务表进每条用例自己的文件；进出清空进程级存储（抄 A3/A5 那份夹具）。"""
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    yield path
    for tid in list(ts.task_store):
        del ts.task_store[tid]


@pytest.fixture
def t38_tools():
    """一条真会挂起的工具：`spent` 每加一笔 = 第 1~3 步被真金白银跑过一次。"""
    spent, resumed, calls = [], [], []

    def t38_gate(item: str = "拿铁", resume: dict = None):
        calls.append(resume)
        if resume is None:
            spent.append("step1-3")
            raise ToolSuspended("请输入短信验证码", {"step": 3, "item": item})
        resumed.append(resume.get("answer"))
        return f"已提交（答案={resume.get('answer')}）"

    entries = {
        "t38_gate": {"function": t38_gate, "description": "下一杯咖啡（要验证码）",
                     "parameters": {"type": "object",
                                    "properties": {"item": {"type": "string"}}},
                     "available": None, "needs_user": False,
                     "requires_approval": False, "suspends": True},
    }
    saved = {name: tools_registry.get(name) for name in entries}
    for name, info in entries.items():
        tools_registry[name] = info
    try:
        yield {"spent": spent, "resumed": resumed, "calls": calls}
    finally:
        for name, previous in saved.items():
            if previous is None:
                tools_registry.pop(name, None)
            else:
                tools_registry[name] = previous


@pytest.fixture
def stub(monkeypatch):
    """三条模型路都记在闭包账上：被拒的那一趟必须一条都不多。"""
    llm = []

    def plan_llm(model, messages, **kw):
        llm.append("plan")
        return json.dumps(["选店并下单"], ensure_ascii=False)

    def task_llm(model, messages, **kw):
        llm.append("task")
        already = any(str(m.get("content", "")).startswith("工具执行结果")
                      for m in messages)
        return "拿到了，继续" if already else _call("t38_gate", {"item": "拿铁"})

    def summary_llm(model, messages, **kw):
        llm.append("summary")
        return "最终汇总"

    monkeypatch.setattr("app.agents.planner.get_llm_response", plan_llm)
    monkeypatch.setattr("app.agents.task_agent.get_llm_response", task_llm)
    monkeypatch.setattr("app.agents.orchestrator.get_llm_response", summary_llm)
    yield llm


def _paid():
    """付费那本账：这本账的键是 IP|身份，只数窗口内的时间戳。"""
    return len(ar._CHATS.get(ar._chat_key(_IP, _UID), []))


def _attempts():
    return len(ar._ORCHESTRATE_ATTEMPTS.get(ar._chat_key(_IP, _UID), []))


def _park(client) -> str:
    """把一条任务跑到"停在工具断点上等人"。这一步本身是付费路径，账上会有 1 笔。"""
    res = client.post("/v1/agent/orchestrate", json={"goal": _GOAL})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "waiting_user", body
    return body["task_id"]


def _answer(client, tid: str, answer: str = "8888"):
    return client.post("/v1/agent/orchestrate",
                       json={"goal": _GOAL, "task_id": tid, "user_answer": answer})


# ---------- 反面 + 正面：同一枚答案，先被拒（不记账），后成功（只记一笔） ----------

def test_a_rejected_resume_spends_nothing_and_debits_nothing_then_the_retry_pays_once(
        client, t38_tools, stub):
    """⭐ 本票的主判据。四个数在"被拒"前后必须逐字相同：付费额度、尝试额度以外的
    一切、模型调用账、世界那本 `spent`；重试成功之后付费额度恰好 +1。

    删掉 `charge_upstream` 那套（退回端点进门就 `throttle_paid_upstream`），本条在
    `_paid() == 1` 那一句红（第 2 笔就是被拒那次花掉的）。把记账改成"每趟都记"也一样红。
    """
    tid = _park(client)
    assert _paid() == 1, "前提没成立：停车这一趟本该在付费账上记一笔"
    assert _attempts() == 1

    # 模拟"另一条请求正跑着这条任务"：占住执行权（与编排器同一张表，不碰状态）。
    assert ts.claim_execution(tid) is True
    llm_before = list(stub)
    res = _answer(client, tid)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["accepted"] is False and body["reason"] == "already_in_flight", body
    assert body["status"] == "waiting_user", body

    # ⭐ no money spent, no debit：四条一起看才是这句话。
    assert _paid() == 1, "没跑、没花钱的拒绝也扣了用户自己的付费额度（#38 的原始症状）"
    assert stub == llm_before, f"被拒的续跑叫了模型：{stub}"
    assert t38_tools["spent"] == ["step1-3"], t38_tools["spent"]
    assert t38_tools["resumed"] == [], "被拒的续跑把答案递进工具了"
    assert ts.get_task(tid).status is ts.TaskStatus.WAITING_USER
    # 尝试账照记：这一格证明"不扣钱"没有顺手把计数也关掉。
    assert _attempts() == 2, "尝试账必须照记（防刷靠它）"

    # 阻因消失之后，同一枚答案就是合法重试：接受、只记一笔、钱只花在续跑那一步。
    ts.release_execution(tid)
    ok = _answer(client, tid)
    assert ok.status_code == 200, ok.text
    done = ok.json()
    assert done.get("accepted") is not False, done
    assert done["status"] == "completed", done
    assert done["final_answer"] == "最终汇总"
    assert t38_tools["resumed"] == ["8888"], t38_tools["resumed"]
    assert t38_tools["spent"] == ["step1-3"], "重试把第 1~3 步重烧了一遍"
    assert _paid() == 2, "真有付费动作的那一趟必须记一笔（否则这条的绿灯只是账本坏了）"
    assert stub == llm_before + ["task", "summary"], stub


# ---------- 刷：被拒可以很多次，但由它自己的那本账兜住 ----------

def test_rejections_are_bounded_by_their_own_ledger_and_never_touch_the_paid_one(
        client, t38_tools, stub):
    """连续被拒到第 61 次才 429；付费账本始终 1 笔，聊天预算原封不动。

    删掉 `throttle_orchestrate_attempt`（"反正被拒不扣钱就不用管"），本条在最后那句
    429 上红——那时返回的是 200，一条脚本可以无限敲。
    """
    tid = _park(client)                       # 用掉 1 次尝试、1 笔付费
    assert ts.claim_execution(tid) is True

    for i in range(ar.MAX_ORCHESTRATE_ATTEMPTS_PER_WINDOW - 1):
        res = _answer(client, tid)
        assert res.status_code == 200, f"第 {i + 2} 次尝试不该被挡：{res.text}"
        assert res.json()["accepted"] is False, res.json()

    blocked = _answer(client, tid)
    assert blocked.status_code == 429, (
        f"被拒了 {ar.MAX_ORCHESTRATE_ATTEMPTS_PER_WINDOW} 次还放行：免费侧没有闸")
    assert "Retry-After" in blocked.headers
    assert _paid() == 1, "刷被拒把付费账本也刷动了"
    assert ar.chat_allowed(_IP, _UID) is True, (
        "19 次聊天预算被一堆没花钱的拒绝顶掉了")
    assert stub == ["plan", "task"], f"刷的过程中有人叫了模型：{stub}"


# ---------- 钱闸没被搬软：两处 429 都发生在花钱之前 ----------

def test_a_throttled_fresh_run_429s_before_the_planner_and_creates_no_task(
        client, t38_tools, stub):
    """付费账本是满的 → 新建任务必须 429，且"规划"一次都没发生、任务一条都没建。

    这条钉的是 `_charge_once()` 的位置：它若被放进下面那个 `try`，429 会被折成
    `status=failed` 的 200，用户看到"任务坏了"而真相是"没被允许花钱"。
    """
    for _ in range(ar.MAX_CHAT_CALLS_PER_WINDOW):
        ar.note_chat(_IP, _UID)
    before = list(stub)
    res = client.post("/v1/agent/orchestrate", json={"goal": "第 21 遍"})
    assert res.status_code == 429, res.text
    assert stub == before, "被挡下的那一趟还是叫了规划模型——记账顺序倒了"
    assert len(ts.task_store) == 0, "被挡下的那一趟留下了任务记录"


def test_a_throttled_resume_keeps_the_task_parked_and_gives_back_the_claim(
        client, t38_tools, stub):
    """续跑那处 429：认领失败方向必须 fail-closed——状态不动、执行权还回去、钱不记。

    删掉 `_claim_execution` 里 `except BaseException: release_execution(...)` 那两行，
    本条红在"执行权没还"：这条任务在本进程里永远"有人在跑"，之后谁也答不了。
    """
    tid = _park(client)
    for _ in range(ar.MAX_CHAT_CALLS_PER_WINDOW - 1):   # 停车已经用掉 1 枚
        ar.note_chat(_IP, _UID)
    before = list(stub)

    res = _answer(client, tid)
    assert res.status_code == 429, res.text
    assert ts.get_task(tid).status is ts.TaskStatus.WAITING_USER, (
        "被 429 挡下的续跑改动了任务状态")
    assert tid not in orch_mod._EXECUTING, "执行权漏还：这条任务以后谁也答不了"
    assert stub == before, "被 429 挡下的续跑已经叫了模型"
    assert t38_tools["resumed"] == [] and t38_tools["spent"] == ["step1-3"]

    # 预算恢复（窗口过去）之后同一枚答案仍然接得回去：漏还的另一种症状是这里 409/拒。
    ar._CHATS.clear()
    ok = _answer(client, tid)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "completed", ok.json()
    assert t38_tools["resumed"] == ["8888"]
