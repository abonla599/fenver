"""工单 #36：任务行没有上限——先给一个只读观察口，不自动删任何一行。

缺陷形状：`data/tasks.json` 里的行只会被惰性作废（status → cancelled），作废
**不删行**；唯一的删行路径是 `DELETE /v1/tasks/{id}`。默认等待窗口 0（永不作废）
时行数因此无上界，而 `/health` 只报"十几处可写"，不报任务行数。

本票选的是**非破坏性**那一支：admin 的 `GET /v1/tasks` 多一格只读的
`stored_total`（这份存储此刻握着的行数 = `data/tasks.json` 的条目数），运维要求
写进部署指南；代码里一行运行时行都不删。本文件钉住三件事：

1. 这一格真的等于存储里的行数，而且跟着 DELETE 走（不是常量、不是本次列出的行数）；
2. 非 admin 拿不到全库行数（跨用户信息不外露，与 404 同一口径）；
3. 读它不删行、不改盘——一条 `waiting_user` 的断点原样还在。
"""
import pytest

from app.agents import task_store as ts


ADMIN_HEADERS = {"Authorization": "Bearer boot-token"}


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    """每条用例自己的 tasks.json；进出都把进程级存储清空重恢复。"""
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    yield path
    for tid in list(ts.task_store):
        del ts.task_store[tid]


def _row(user_id: str, goal: str, status=None) -> ts.Task:
    """造一行；给了 status 就从 pending 经 running 走到那一态（状态机只认这些边）。"""
    task = ts.Task(goal=goal, user_id=user_id)
    ts.task_store[task.task_id] = task
    if status is not None:
        task.set_status(ts.TaskStatus.RUNNING)
        task.set_status(status)
    return task


def _uid(client, headers) -> str:
    res = client.get("/v1/auth/me", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()["user_id"]


def test_admin_list_answers_the_physical_row_count_and_it_tracks_deletes(
        client, enforced, isolated_tasks):
    """⭐ 观察口就是 `stored_total`：等于存储行数，删掉一行之后跟着减一。"""
    _row("u-a", "已完成的那条", ts.TaskStatus.COMPLETED)
    _row("u-b", "失败的那条", ts.TaskStatus.FAILED)
    _row("u-b", "取消的那条", ts.TaskStatus.CANCELLED)

    res = client.get("/v1/tasks", headers=ADMIN_HEADERS)
    assert res.status_code == 200, res.text
    body = res.json()
    assert isinstance(body, dict)
    assert isinstance(body["stored_total"], int) and not isinstance(
        body["stored_total"], bool), "stored_total 必须是 JSON 整数"
    assert body["stored_total"] == len(ts.task_store) == 3
    assert body["total"] == 3 and len(body["tasks"]) == 3
    # 只增不改：行上既有的键一个不少（客户端不因这一格换解析方式）。
    assert {"task_id", "goal", "status", "user_id", "legacy",
            "progress", "created_at"} <= set(body["tasks"][0])

    victim = next(t for t in ts.task_store.values() if t.goal == "已完成的那条")
    deleted = client.delete(f"/v1/tasks/{victim.task_id}", headers=ADMIN_HEADERS)
    assert deleted.status_code == 200, deleted.text
    after = client.get("/v1/tasks", headers=ADMIN_HEADERS).json()
    assert after["stored_total"] == 2, "删掉一行之后行数没跟着走：这不是真读数"


def test_a_regular_user_does_not_get_the_global_row_count(client, enforced):
    """非属主连"全库有几条任务"都不该知道——那也是一格跨用户信息。"""
    ha = enforced("行数甲")
    hb = enforced("行数乙")
    ua = _uid(client, ha)
    ub = _uid(client, hb)
    _row(ua, "甲自己的", ts.TaskStatus.COMPLETED)
    _row(ub, "乙的第一条", ts.TaskStatus.COMPLETED)
    _row(ub, "乙的第二条", ts.TaskStatus.FAILED)

    body = client.get("/v1/tasks", headers=ha).json()
    assert body["total"] == 1
    assert [t["goal"] for t in body["tasks"]] == ["甲自己的"]
    assert "stored_total" not in body, (
        "普通用户的列表带出了全库行数：这是非属主不该知道的跨用户信息")


def test_reading_the_count_neither_deletes_nor_rewrites_anything(
        client, enforced, isolated_tasks):
    """⭐ 非破坏性的证据：一次读接口之后，内存行数、盘上字节、断点分布一个不动。"""
    waiting = ts.Task(goal="下单要验证码", user_id="u-w")
    ts.task_store[waiting.task_id] = waiting
    waiting.set_status(ts.TaskStatus.RUNNING)
    waiting.wait_for_user("请输入短信验证码", cursor={"kind": "tool_suspension"})
    _row("u-done", "已经完成的", ts.TaskStatus.COMPLETED)

    on_disk_before = isolated_tasks.read_bytes()
    rows_before = dict(ts.task_store)

    body = client.get("/v1/tasks", headers=ADMIN_HEADERS).json()

    assert body["stored_total"] == 2
    assert dict(ts.task_store) == rows_before, "一次读接口改动了内存里的任务表"
    assert isolated_tasks.read_bytes() == on_disk_before, (
        "一次读接口重写了 tasks.json")
    kept = ts.get_task(waiting.task_id)
    assert kept is waiting and kept.status is ts.TaskStatus.WAITING_USER
    assert kept.can_resume is True and kept.resume_cursor is not None, (
        "读行数把一条还能续的断点删了/作废了")
