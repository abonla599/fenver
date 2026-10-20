"""v0.35.0 验收判据 1、2 的端到端复跑：真实任务入口 → 挂起 → 重启 → 回答 → 终态。

这条文件只走**现网那两个 HTTP 面**（网页/PWA 的 #133 用的就是它们）：

- 任务入口与回答入口是同一个 `POST /v1/agent/orchestrate`，回答那一枪带
  `{"goal", "task_id", "user_answer"}`（网页端 `api.js` 的 `answerTask`，main.py:1716）；
- 读侧是 `GET /v1/tasks/{task_id}`（main.py:1753）。

判据 1 的形状：请求命中 `plan_add` 的真挂起点（#153：文本里有日子词、却认不出具体
日期，`suspends=True`）后，`TaskStore` 里留下一条 `waiting_user` 任务；把用户回答从
回答入口递回去，编排器从断点续跑、任务落到 `completed`，并且 `plan_add` 收到的就是
用户答的那个日子（不是今天、也不是没收到答案时重挂一次）。

判据 2 的形状：中途调 `task_store.restore()`——模块导入期（task_store.py:827）跑的
就是这一条无参调用——模拟进程重启；`GET /v1/tasks/{id}` 仍须答得出等待游标与
`can_resume`，重启之后回答仍然能续跑。

不证明什么（交付报告里逐条写）：不证明 PyInstaller 整包里真有这份字节、不证明浏览器
卡片的渲染与真人点击、不证明多进程下同一停点只被接一次（现网单进程，A3 文件头有边界）。
"""
import json
from datetime import date, timedelta

import pytest

import app.tools.builtin_tools  # noqa: F401  导入期注册 plan_add：不导入就没有真工具
from app.agents import task_store as ts
from app.core import schedule

_GOAL = "记得提醒我周末买菜"
_ANSWER = "下周六"


def _next_saturday() -> str:
    """「下周六」的落点：下周一 + 5 天。与 plan_add 的换算同一口径（#153 的测试同款）。"""
    base = date.fromisoformat(schedule.today())
    monday = base - timedelta(days=base.weekday())
    return (monday + timedelta(days=7 + 5)).isoformat()


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    """任务表进这条用例自己的文件；进出清空进程级存储（抄 A1/A3 那份夹具）。"""
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    yield path
    for tid in list(ts.task_store):
        del ts.task_store[tid]


def _install_fake_llm(monkeypatch, calls):
    """确定性替身：规划一步、执行时要求调 plan_add、汇总给一句话。

    执行侧只要对话里已经有「工具执行结果」就收尾——那一行正是编排器从断点续跑时
    补进 TaskAgent 的种子（task_agent.py:112-120）。所以如果续跑没把工具结果接上，
    这个替身会再要一次 plan_add，任务会第二次挂起，用例当场红在 "completed" 上。
    """
    def plan_llm(model, messages, **kw):
        calls.append("plan")
        return json.dumps(["把周末的买菜记进日程"], ensure_ascii=False)

    def task_llm(model, messages, **kw):
        calls.append("task")
        if any(str(m.get("content", "")).startswith("工具执行结果") for m in messages):
            return "日程已经记好了。"
        return ('<tool_call>' + json.dumps(
            {"name": "plan_add", "arguments": {"text": "周末买菜"}},
            ensure_ascii=False) + '</tool_call>')

    def summary_llm(model, messages, **kw):
        calls.append("summary")
        return "汇总：周末买菜的日程已经记好。"

    monkeypatch.setattr("app.agents.planner.get_llm_response", plan_llm)
    monkeypatch.setattr("app.agents.task_agent.get_llm_response", task_llm)
    monkeypatch.setattr("app.agents.orchestrator.get_llm_response", summary_llm)


def _park(client, headers) -> dict:
    res = client.post("/v1/agent/orchestrate", json={"goal": _GOAL}, headers=headers)
    assert res.status_code == 200, res.text
    parked = res.json()
    assert parked["status"] == "waiting_user", parked
    assert parked["can_resume"] is True
    assert "哪一天" in parked["question"], parked
    return parked


def _answer(client, headers, task_id: str, answer: str) -> dict:
    res = client.post("/v1/agent/orchestrate",
                      json={"goal": _GOAL, "task_id": task_id, "user_answer": answer},
                      headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def test_c1_suspend_then_answer_through_endpoints_reaches_completed(
        client, enforced, isolated_tasks, isolated_schedule, monkeypatch):
    """判据 1：挂起真的落成 waiting_user；从回答入口递答案，任务真的续跑完。

    会红的三处（都是续跑这半条被弄坏的形状）：
    - 回答没过 `user_answer` 进编排器 → plan_add 无 resume 再挂一次，`status` 不是
      completed，日程里也没有这一条；
    - 编排器没把断点递给工具 → 同上；
    - 编排器把用户的答案丢了/自己编了 → `schedule.plan(uid, 下周六)` 为空。
    """
    _install_fake_llm(monkeypatch, [])
    headers = enforced("验收判据一")
    uid = client.get("/v1/auth/me", headers=headers).json()["user_id"]

    parked = _park(client, headers)
    tid = parked["task_id"]

    # 落盘的是任务，不只是 HTTP 回话：盘上那条就是 waiting_user + 工具断点。
    on_disk = json.loads(isolated_tasks.read_text(encoding="utf-8"))["tasks"][tid]
    assert on_disk["status"] == "waiting_user", on_disk["status"]
    assert on_disk["can_resume"] is True
    assert on_disk["waiting_since"]
    assert on_disk["resume_cursor"]["kind"] == "tool_suspension"
    assert on_disk["resume_cursor"]["tool_name"] == "plan_add"
    assert on_disk["resume_cursor"]["arguments"] == {"text": "周末买菜"}
    assert on_disk["resume_cursor"]["resume_state"] == {
        "text": "周末买菜", "at": "", "day": ""}
    assert on_disk["interrupted_by_restart"] is False

    detail = client.get(f"/v1/tasks/{tid}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["status"] == "waiting_user"
    assert body["can_resume"] is True
    assert body["resume_cursor"]["question"] == parked["question"]
    assert body["waiting_since"] == on_disk["waiting_since"]
    # 没回答之前，日程里一条都不许有：问之前就把事办了是把日子记错的前一步。
    assert schedule.plan(uid) == []
    assert schedule.plan(uid, _next_saturday()) == []

    done = _answer(client, headers, tid, _ANSWER)
    assert done["status"] == "completed", done
    assert done["final_answer"] == "汇总：周末买菜的日程已经记好。"
    assert done["results"], done

    final = client.get(f"/v1/tasks/{tid}", headers=headers).json()
    assert final["status"] == "completed", final
    assert final["can_resume"] is False
    assert final["waiting_since"] is None
    assert final["error"] is None

    # 用户答的那个日子真的进了 plan_add：不是今天，也不是被吞掉的答案。
    rows = schedule.plan(uid, _next_saturday())
    assert [r["text"] for r in rows] == ["周末买菜"], rows
    assert schedule.plan(uid) == [], "回答给了具体日子却还落在今天"


def test_c2_restore_keeps_waiting_cursor_and_the_answer_after_restart_completes(
        client, enforced, isolated_tasks, isolated_schedule, monkeypatch):
    """判据 2：`task_store.restore()`（重启读盘那一路）之后，GET 仍返回等待游标。

    重启后还会再答一次并重新读盘：这样"重启前落下的 waiting_user 记录能不能被运行期
    真的接上"也被钉住，而不只是读侧看见了一个字符串。
    """
    _install_fake_llm(monkeypatch, [])
    headers = enforced("验收判据二")
    uid = client.get("/v1/auth/me", headers=headers).json()["user_id"]

    parked = _park(client, headers)
    tid = parked["task_id"]
    before = client.get(f"/v1/tasks/{tid}", headers=headers).json()
    assert before["status"] == "waiting_user"

    # 进程重启：模块导入期调用的是同一句无参 restore()（认 TASKS_DB_PATH）。
    assert ts.restore() == 1
    restored = ts.get_task(tid)
    assert restored is not None, "重启后任务整条丢了"
    assert restored.status is ts.TaskStatus.WAITING_USER
    assert restored.interrupted_by_restart is False, "等人不等于被重启打断"

    after = client.get(f"/v1/tasks/{tid}", headers=headers)
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["status"] == "waiting_user", "重启后 waiting 态丢了"
    assert body["can_resume"] is True
    assert body["waiting_since"] == before["waiting_since"]
    assert body["resume_cursor"] == before["resume_cursor"], \
        "重启后 GET 拿到的等待游标与重启前不是同一份"
    assert body["resume_cursor"]["tool_name"] == "plan_add"
    assert body["resume_cursor"]["question"] == parked["question"]

    done = _answer(client, headers, tid, _ANSWER)
    assert done["status"] == "completed", done
    rows = schedule.plan(uid, _next_saturday())
    assert [r["text"] for r in rows] == ["周末买菜"], rows

    # 终态也过一遍重启读盘：完成不是"只活在内存里"的那一格。
    assert ts.restore() == 1
    final = client.get(f"/v1/tasks/{tid}", headers=headers).json()
    assert final["status"] == "completed", final
    assert final["can_resume"] is False
    assert final["final_answer"] == "汇总：周末买菜的日程已经记好。"
