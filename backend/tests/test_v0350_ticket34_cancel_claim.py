"""工单 #34：取消要看见执行权，判与落必须是同一跳。

缺陷形状（审查员量到的）：一条 `waiting_user` 任务正在把用户递进来的答案接回去时，
另一个标签页 / 管理员 / 清扫线程打 `POST /v1/tasks/{id}/cancel` 依然回 200。端点只看
终态名单，然后 `mark_cancelled()`；编排器此刻已经把任务翻成 RUNNING 并把执行权记在
进程内的表里，但那张表住在 `orchestrator` 模块，端点看不见。取消落下后编排循环在
下一步读到 `task.cancelled` 就整条收工——刚递进去的验证码/收货地址随任务一起被丢掉，
按既有口径还不退钱。

修法（本票）：执行权搬进 `task_store` 成为公共 API（`claim_execution` /
`release_execution` / `is_executing`），并新增 `cancel_task`：终态与执行权在同一把
`_lock` 里判、状态也在同一把锁里落。端口因此只剩三种回答：cancelled / warning /
409。本文件钉住这三格与"取消失败后还权还能再取消"（409 不是永久拒绝）。
"""
import pytest

import app.agents.orchestrator as orch_mod
from app.agents import task_store as ts
from app.agents.orchestrator import Orchestrator


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    """任务表进每条用例自己的文件；进出都清空，执行权也一并还干净。

    执行权是**进程级**集合，泄漏一条就会让后面任何按 id 断言的用例莫名 409——
    本仓那种"红离真凶隔着整个文件"的形状，必须在夹具里先堵掉。
    """
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    yield path
    for tid in list(ts.task_store):
        del ts.task_store[tid]
    for tid in list(ts._EXECUTING):
        ts.release_execution(tid)


def _parked(user_id: str = "u-34") -> ts.Task:
    """造一条停在断点上等人的任务（不跑模型，直接从状态机走过去）。"""
    task = ts.Task(goal="下单要验证码", subtasks=["选店并下单"], user_id=user_id)
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    task.wait_for_user("请输入短信验证码")
    return task


def test_the_execution_claim_is_a_public_task_store_api(isolated_tasks):
    """占位/归还/查询是公共函数，占两次只许成功一次。"""
    task = ts.Task(goal="任意", user_id="u-34")
    ts.task_store[task.task_id] = task

    assert ts.is_executing(task.task_id) is False
    assert ts.claim_execution(task.task_id) is True
    assert ts.is_executing(task.task_id) is True
    assert ts.claim_execution(task.task_id) is False, "同一枚任务被占了两份执行权"

    ts.release_execution(task.task_id)
    assert ts.is_executing(task.task_id) is False
    ts.release_execution(task.task_id)          # 幂等：多还一次不许炸
    assert ts.is_executing(task.task_id) is False


def test_cancel_refuses_an_execution_in_flight_and_leaves_the_task_untouched(
        isolated_tasks):
    """⭐ 本票的核心：有执行权时取消一格不改；还权之后同一句调用真的能落。

    "一格不改"要连着三格一起断言（status / cancelled 标志 / can_resume）：只判状态
    的话，先置 `cancelled=True` 再被拒的实现也能过——那正是编排循环会读到的那一格。
    """
    task = _parked()
    assert ts.claim_execution(task.task_id) is True
    try:
        assert ts.cancel_task(task) == "executing"
    finally:
        ts.release_execution(task.task_id)

    assert task.status is ts.TaskStatus.WAITING_USER
    assert task.cancelled is False
    assert task.can_resume is True and task.waiting_since is not None
    assert task.resume_cursor is not None and task.resume_cursor["question"] == "请输入短信验证码"

    # 反例的另一侧：409 不是"永远拒绝"。还权之后同一个答案照旧能取消。
    assert ts.cancel_task(task) == "cancelled"
    assert task.status is ts.TaskStatus.CANCELLED and task.cancelled is True


def test_terminal_tasks_keep_the_existing_no_op_answer(isolated_tasks):
    """终态优先于执行权：已定论的任务仍然拿到"无需取消"，不新造 409。"""
    task = ts.Task(goal="任意", user_id="u-34")
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    task.set_status(ts.TaskStatus.COMPLETED)

    assert ts.cancel_task(task) == "terminal"
    assert task.status is ts.TaskStatus.COMPLETED
    assert task.cancelled is False


def test_the_orchestrator_claims_through_the_same_public_table(isolated_tasks):
    """编排器的认领必须写进 /cancel 看的那张表，否则这道闸就是两本账。"""
    task = ts.Task(goal="任意", user_id="u-34")
    ts.task_store[task.task_id] = task
    orch = Orchestrator()

    assert orch._claim_execution(task) is None
    try:
        assert ts.is_executing(task.task_id) is True, (
            "编排器认领后 task_store 看不见：/cancel 会照旧把答案丢掉")
        assert task.status is ts.TaskStatus.RUNNING
        assert ts.cancel_task(task) == "executing"
    finally:
        orch._release_execution(task.task_id)

    assert ts.is_executing(task.task_id) is False
    assert ts.cancel_task(task) == "cancelled"
    # 既有消费方的旧名字仍是同一张表（A3 的并发用例直接往里面塞 id）。
    assert orch_mod._EXECUTING is ts._EXECUTING


def test_the_cancel_route_returns_409_while_a_claim_is_held(
        client, enforced, isolated_tasks):
    """HTTP 边界：在飞时 409 + 任务不动；还权后端点走既有 200/cancelled 那条路。

    旧实现的第一句就红：它回 200，响应体 `status=cancelled`，任务当场被取消。
    """
    headers = enforced("取消乙")
    uid = client.get("/v1/auth/me", headers=headers).json()["user_id"]
    task = _parked(uid)

    assert ts.claim_execution(task.task_id) is True
    try:
        res = client.post(f"/v1/tasks/{task.task_id}/cancel", headers=headers)
    finally:
        ts.release_execution(task.task_id)

    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert "执行中" in detail and "取消未被接受" in detail, detail
    kept = ts.get_task(task.task_id)
    assert kept.status is ts.TaskStatus.WAITING_USER, "被拒的取消改动了任务"
    assert kept.cancelled is False
    assert kept.resume_cursor is not None, "答案的停点被一次没落下的取消弄没了"

    ok = client.post(f"/v1/tasks/{task.task_id}/cancel", headers=headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "cancelled", ok.json()
    assert ts.get_task(task.task_id).status is ts.TaskStatus.CANCELLED
