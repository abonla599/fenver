"""v0.35 A1 行为契约：任务状态机多出来的那一格，和随它一起落盘的游标。

规划提案 §二 把"任务不能挂起等待用户"列为 v0.35 必须自己铺的第一块地基，判据写在
§四 v0.35 那四条里。这一版只动 `TaskStatus`（**只加一个成员** `waiting_user`）与
`Task` 的三个字段（游标 / 可续布尔 / 等待时刻），所以这里的判据全部对着三件事：

1. **迁移是全量的**：现网那份 `data/tasks.json` 由上一版代码写下，一个键都没有；
   读它不许炸、也不许把缺的键猜成"正在等谁"。反过来，`status` 写成一个认不出的值
   必须**响**（按本仓既有约定：逐条跳过 + 一句人话），而不是悄悄变成 `pending`
   ——一个正在等人的任务被认成待执行，列表里它就永远不再需要任何人。
2. **跳转表是唯一判据**：`waiting_user` 从 `running` 进得来、出得去四个出口，
   表外的跳转一格内存都不许改。提案 §四 判据 4 要的变异锁（"删掉 waiting_user
   状态转换 → 测试红"）落在 `test_the_transition_table_is_the_one_we_designed`
   与 `test_waiting_user_survives_a_restart...` 两条上。
3. **消费方不把新态当结束**：详情端点答得出"在等什么、能不能续、等了多久"，
   取消这一面对 waiting_user 仍然有效且真的落盘。

字面量只加一个、且"可续"是布尔字段而不是状态——理由是 `resumable` 这个词在本仓
已经有两处各自的含义（`core/stream_runs.py` 的函数与 JSON 布尔、安卓壳 Api.kt 的
Boolean），再拿它当状态名等于让下一个人猜。那条纪律由
`test_the_new_member_is_exactly_one_and_no_reserved_word_is_a_status` 钉住。

网页/壳那半边的渲染（SSE 的 waiting_user 帧、确认卡）不在这个票面上，见交付报告里
的 follow-up 清单。
"""
import json

import pytest

from app.agents import task_store as ts

# 上一版 to_dict 写下的全部键。这份"老形状"是从改动前的代码里逐字抄来的，
# 迁移判据要用它，不能用新代码的产物当基准（那等于自己给自己出题）。
OLD_RECORD_KEYS = {"task_id", "user_id", "goal", "provider_id", "legacy", "subtasks",
                   "current_subtask", "results", "status", "created_at", "final_answer",
                   "error", "cancelled", "interrupted_by_restart"}
NEW_RECORD_KEYS = OLD_RECORD_KEYS | {"resume_cursor", "can_resume", "waiting_since"}


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    """每条用例自己的 tasks.json；进出都清空进程级存储再恢复。

    抄的是 test_v025_task_ownership_contract 那份夹具的理由：task_store 是进程级
    dict，不带进带出的话，任何一条按 id 断言的用例都可能撞上别人家的任务。
    """
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    yield path
    for tid in list(ts.task_store):
        del ts.task_store[tid]


def _running(goal: str = "点一份外卖", user_id: str = "u-1") -> ts.Task:
    """建一个已经在跑的任务（waiting_user 的上一状态只能是 running）。"""
    task = ts.Task(goal=goal, subtasks=["选店", "下单", "输验证码", "付钱"],
                   user_id=user_id)
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    return task


# ---------- 状态面：只许多一个成员 ----------

def test_the_new_member_is_exactly_one_and_no_reserved_word_is_a_status():
    """六个状态，比改动前**恰好多一个**，且没有第二个"等待"的同义词。

    负向锁：再加一个 `NEEDS_INPUT` / `SUSPENDED` / `RESUMABLE` 成员，这条立刻红——
    那正是提案 §四 那行字面列表与本票前提冲突的地方，加第二个字面量会让"这个任务是
    不是在等人"变成两个答案。
    """
    assert sorted(m.value for m in ts.TaskStatus) == [
        "cancelled", "completed", "failed", "pending", "running", "waiting_user"]
    banned = ("resum", "suspend", "needs_input", "pause", "hold")
    collisions = [m.value for m in ts.TaskStatus
                  if any(b in m.value for b in banned)]
    assert not collisions, f"状态字面量撞了预留词族（stream_runs/Api.kt 已占用）：{collisions}"
    # "能不能续"是**字段**，不是第二个状态
    task = ts.Task(goal="g", user_id="u-1")
    assert task.can_resume is False
    assert isinstance(task.can_resume, bool)


# ---------- 跳转表：唯一一份判据 ----------

def test_the_transition_table_is_the_one_we_designed():
    """整张表逐格断言。

    这是提案 §四 判据 4（变异锁）的落点：`ALLOWED_TRANSITIONS[RUNNING]` 里删掉
    `WAITING_USER`，这条与重启那条一起红；给 `COMPLETED` 加一条出边，这条也红。
    """
    S = ts.TaskStatus
    expected = {
        S.PENDING: {S.RUNNING, S.CANCELLED},
        S.RUNNING: {S.RUNNING, S.WAITING_USER, S.COMPLETED, S.FAILED, S.CANCELLED},
        S.WAITING_USER: {S.RUNNING, S.COMPLETED, S.FAILED, S.CANCELLED},
        S.COMPLETED: set(),
        S.FAILED: {S.RUNNING},
        S.CANCELLED: set(),
    }
    got = {k: set(v) for k, v in ts.ALLOWED_TRANSITIONS.items()}
    assert got == expected, f"跳转表与设计不一致：{got ^ expected}"
    # pending → waiting_user 刻意不走：一步都没跑就没有"停在哪"可记
    assert ts.can_transition(S.PENDING, S.WAITING_USER) is False
    assert ts.can_transition(S.RUNNING, S.WAITING_USER) is True
    assert ts.can_transition(S.COMPLETED, S.RUNNING) is False


def test_an_illegal_jump_is_refused_and_touches_nothing(isolated_tasks):
    """非法跳转不是"返回 False"，是抛，而且内存与盘上都不留半个跳转。

    这条测的就是原子性本身：`wait_for_user` 先在局部拼好游标、再走那一跳，判据不过时
    self 一格都不动。负向锁两条，各自对着红的一半：
    - 把 `set_status` 里那句 `raise IllegalTaskTransition(...)` 删掉 → 这条红在
      `pytest.raises`（没抛）与 `status is PENDING`；
    - 把游标两行赋值挪回 `set_status` **之前**（改动前的写法）→ 这条红在
      `resume_cursor is None`：被拒绝的一跳留下了"有游标却没在等人"的半个形状。
    """
    task = ts.Task(goal="g", subtasks=["a", "b"], user_id="u-1")
    ts.task_store[task.task_id] = task
    assert task.status is ts.TaskStatus.PENDING

    with pytest.raises(ts.IllegalTaskTransition):
        task.wait_for_user("请输入验证码")

    assert task.status is ts.TaskStatus.PENDING, "被拒绝的跳转不许留下半个状态"
    assert task.can_resume is False and task.resume_cursor is None
    assert task.waiting_since is None, "没进等待态就不该有等待时刻"
    on_disk = json.loads(isolated_tasks.read_text(encoding="utf-8"))["tasks"]
    assert on_disk[task.task_id]["status"] == "pending"

    # 同一张任务，合法的一跳就过（证明上面那次红不是"waiting_user 永远进不去"）
    task.set_status(ts.TaskStatus.RUNNING)
    task.wait_for_user("请输入验证码")
    assert task.status is ts.TaskStatus.WAITING_USER


def test_a_typo_in_a_companion_field_is_refused_not_silently_added(isolated_tasks):
    """`set_status(..., resume_curosr=...)` 这种拼写错误必须响，不要静默挂个新属性。

    静默挂上去的症状是"写了但读不回来"：to_dict 不认识那个键，下一次 restore 凭空少
    一格——正是这份文件 :81 那段注释在防的同一类错，只是换了个入口。

    负向锁：删掉 `if not hasattr(self, name)` 那三行，这条红在 pytest.raises。
    """
    task = _running()
    with pytest.raises(AttributeError):
        task.set_status(ts.TaskStatus.WAITING_USER, resume_curosr={"question": "打错了"})
    assert not hasattr(task, "resume_curosr"), "拼错的键被当成新属性挂上了"
    assert task.status is ts.TaskStatus.RUNNING, "报错之前不许改状态"


@pytest.mark.parametrize("target", ["running", "completed", "failed", "cancelled"])
def test_waiting_user_exits_to_every_allowed_state(isolated_tasks, target):
    """进得来也出得去：四个出口逐个真走一遍，并断言离开后 can_resume 归位。

    负向锁：把 `WAITING_USER` 那一行的某个出口删掉，这条对着那个 param 红。
    """
    task = _running()
    task.wait_for_user("请输入验证码")
    task.set_status(ts.TaskStatus(target))

    assert task.status.value == target
    assert task.can_resume is False, "离开等待态还标着可续，等于两个真相"
    assert task.waiting_since is None
    assert task.resume_cursor is not None, "游标记的是「上次停在哪儿」，离开时不必抹"
    back = ts.Task.from_dict(task.to_dict())
    assert back.status.value == target and back.can_resume is False


def test_completed_and_cancelled_are_terminal(isolated_tasks):
    """终态不出边：给一个已完成的任务再挂起 = 抛。

    负向锁：`ALLOWED_TRANSITIONS[COMPLETED]` 一旦不是空集，这条红。
    """
    task = _running()
    task.set_status(ts.TaskStatus.COMPLETED)
    with pytest.raises(ts.IllegalTaskTransition):
        task.set_status(ts.TaskStatus.WAITING_USER)
    with pytest.raises(ts.IllegalTaskTransition):
        task.mark_cancelled()
    assert task.status is ts.TaskStatus.COMPLETED


def test_mark_cancelled_from_waiting_user_is_legal_and_persists(isolated_tasks):
    """取消一个正在等人的任务：状态机允许，而且**这次真的落盘**。

    改动前 `mark_cancelled()` 只改内存字段（唯一写盘点是"放进存储"那一次），于是
    取消过的任务重启后会以 pending/running 复活。这一条把修好的那侧钉住。

    负向锁：把 `set_status` 的 `persist=True` 默认改成 False，这条红在盘上那句
    `waiting_user` 上。
    """
    task = _running()
    task.wait_for_user("请输入验证码")
    task.mark_cancelled()

    assert task.status is ts.TaskStatus.CANCELLED and task.cancelled is True
    ts.restore(path=str(isolated_tasks))
    back = ts.get_task(task.task_id)
    assert back.status is ts.TaskStatus.CANCELLED, "取消只活在内存：重启就诈尸"


# ---------- 落盘：新键要真的在盘上，且写成 .value ----------

def test_the_new_state_lands_as_its_value_not_its_repr(isolated_tasks):
    """盘上那格必须是 `"waiting_user"`。

    `class TaskStatus(str, Enum)` 的 `str()` 给的是 `"TaskStatus.WAITING_USER"`，
    写进去就再也读不回来（`TaskStatus(...)` 认不出它）——这份文件 :81 那段注释讲的
    就是同一个坑，新状态必须重新踩一遍验证它没被踩。
    """
    task = _running()
    task.wait_for_user("请输入验证码")
    record = json.loads(isolated_tasks.read_text(encoding="utf-8"))["tasks"][task.task_id]
    assert record["status"] == "waiting_user"
    assert "TaskStatus." not in json.dumps(record), "写成了 repr：写进去的东西读不回来"
    assert record["can_resume"] is True
    assert isinstance(record["resume_cursor"], dict)
    assert record["resume_cursor"]["question"] == "请输入验证码"
    assert record["resume_cursor"]["subtask_index"] == task.current_subtask
    assert record["waiting_since"], "等待时刻没落盘，「挂起保活多久」就无从判"


def test_waiting_user_survives_a_restart_and_is_not_called_interrupted(isolated_tasks):
    """提案 §四 v0.35 判据 2：重启后 waiting_user 仍在，且能继续。

    "被重启打断"那一族只有 pending/running —— 正在等用户的任务不该被说成打断，
    它的停点本来就随游标落盘了。

    负向锁：把 restore() 里那句 `t.status in (PENDING, RUNNING)` 改成包含
    WAITING_USER，这条红在 `interrupted_by_restart is False`；把 WAITING_USER 从
    枚举里删掉，这条红在读不回来那一步。
    """
    task = _running(user_id="u-keep")
    task.current_subtask = 2
    task.results = ["选了店", "下了单"]
    task.wait_for_user("请输入短信验证码", cursor={"collected": {"shop": "甲"}})
    tid = task.task_id

    ts.restore(path=str(isolated_tasks))            # 等价于重启进程
    back = ts.get_task(tid)
    assert back is not None, "waiting_user 任务没落盘：重启即丢"
    assert back.status is ts.TaskStatus.WAITING_USER
    assert back.can_resume is True, "恢复回来却认不出「可以续」，游标就白落了"
    assert back.interrupted_by_restart is False, "等人不是被打断"
    assert back.current_subtask == 2, "断点位置丢了"
    assert back.resume_cursor["collected"] == {"shop": "甲"}
    assert back.resume_cursor["question"] == "请输入短信验证码"
    assert back.results == ["选了店", "下了单"], "已收集的结果必须跟着回来"
    assert back.user_id == "u-keep" and back.waiting_since == task.waiting_since


def test_leaving_the_state_and_coming_back_is_not_a_one_way_door(isolated_tasks):
    """挂起 → 用户答了（回 running）→ 再挂起：第二次也要带得出游标。

    长任务里"要验证码"不会只有一次；提案 §二 那一行写的正是"第二次确认时票据已作废"
    这一类反复。这条钉状态机可反复走，不落"只能挂一次"的坑。
    """
    task = _running()
    task.wait_for_user("第一次：验证码")
    task.set_status(ts.TaskStatus.RUNNING)
    task.wait_for_user("第二次：收货地址")

    assert task.status is ts.TaskStatus.WAITING_USER and task.can_resume is True
    assert task.resume_cursor["question"] == "第二次：收货地址"
    ts.restore(path=str(isolated_tasks))
    assert ts.get_task(task.task_id).resume_cursor["question"] == "第二次：收货地址"


# ---------- 迁移：老库全量可读，坏值响 ----------

def test_an_old_tasks_json_loads_and_gets_the_documented_defaults(isolated_tasks):
    """现网那份文件由**改动前**的代码写下：一个 A1 键都没有。读它不许炸。

    缺省必须正好是"没在等任何人"：resume_cursor=None / can_resume=False /
    waiting_since=None。把 default 写成 True 的话，每条历史任务都会伪装成在等人，
    而列表里没有人会去答复它。

    负向锁：把 from_dict 里那句 `task.can_resume = (status is WAITING_USER)` 换成
    `bool(data.get("can_resume", True))`，这条对着 legacy 那条红。
    """
    def old_record(**over):
        rec = {"task_id": None, "user_id": "u-old", "goal": "历史任务",
               "provider_id": None, "legacy": False, "subtasks": ["一", "二"],
               "current_subtask": 1, "results": ["做完了第一个"],
               "status": "completed", "created_at": "2026-10-01T09:00:00",
               "final_answer": "都办好了", "error": None,
               "cancelled": False, "interrupted_by_restart": False}
        rec.update(over)
        return rec

    records = {
        "t_done": old_record(task_id="t_done"),
        "t_pending": old_record(task_id="t_pending", status="pending",
                               current_subtask=0, results=[], final_answer=None),
        "t_running": old_record(task_id="t_running", status="running",
                                current_subtask=1),
        "t_legacy": old_record(task_id="t_legacy", user_id=None, status="completed"),
    }
    isolated_tasks.write_text(json.dumps({"tasks": records}), encoding="utf-8")

    n = ts.restore(path=str(isolated_tasks))
    assert n == 4, f"老库里四条都该读回来，实际 {n} 条——迁移不许丢条目"

    done = ts.get_task("t_done")
    assert done.status is ts.TaskStatus.COMPLETED
    assert done.resume_cursor is None and done.can_resume is False
    assert done.waiting_since is None
    assert done.final_answer == "都办好了"

    # running/pending 照旧被认成"被重启打断"，waiting_user 不在这一族（见下一条）
    assert ts.get_task("t_pending").interrupted_by_restart is True
    assert ts.get_task("t_running").interrupted_by_restart is True
    assert ts.get_task("t_running").can_resume is False
    legacy = ts.get_task("t_legacy")
    assert legacy.user_id == ts.LEGACY_OWNER and legacy.legacy is True
    assert legacy.can_resume is False

    # 新写盘是**超集**：老键一个不能少、一个不能改名，否则下一次落盘就在丢历史数据
    assert set(done.to_dict().keys()) == NEW_RECORD_KEYS
    assert OLD_RECORD_KEYS.issubset(set(done.to_dict().keys()))


def test_a_record_that_claims_to_wait_without_a_status_is_not_invented(isolated_tasks):
    """手改盘上那格 can_resume=true 而状态是 completed：不认。

    "可续"由状态决定（不变式在 from_dict 这一侧同样成立），盘上单独一格的 true
    是坏数据，不是指令。

    负向锁：把那句改成 `task.can_resume = bool(data.get("can_resume", ...))`，这条红。
    """
    isolated_tasks.write_text(json.dumps({"tasks": {
        "t_x": {"task_id": "t_x", "user_id": "u-1", "goal": "g", "subtasks": [],
                "status": "completed", "can_resume": True,
                "resume_cursor": {"question": "还等吗"}, "created_at": "2026-10-01T09:00:00"},
    }}), encoding="utf-8")
    ts.restore(path=str(isolated_tasks))
    assert ts.get_task("t_x").can_resume is False, "已完成的任务不许自称能续"


def test_unknown_status_fails_loudly_instead_of_becoming_pending(isolated_tasks, capsys):
    """认不出的 status = 逐条跳过 + 一句人话（本仓既有约定），**绝不**猜成 pending。

    这正是 requirement "migration must be total, not partial" 的反面：坏记录被兜成
    pending，等于把一个未知状态伪装成"还没开始跑"，列表里它谁都不必负责。

    负向锁：把 from_dict 里 `TaskStatus(data.get("status", PENDING))` 换成
    `TaskStatus(data.get("status") or PENDING)` 之类的兜底（即吞掉 ValueError），
    这条红在第一句——它会"读回来一条 pending"。
    """
    good = {"task_id": "t_good", "user_id": "u-1", "goal": "好的", "subtasks": [],
            "status": "waiting_user", "can_resume": True,
            "resume_cursor": {"question": "在等你"}, "created_at": "2026-10-01T09:00:00"}
    bad = {"task_id": "t_bad", "user_id": "u-1", "goal": "坏的状态", "subtasks": [],
           "status": "teleporting", "created_at": "2026-10-01T09:00:00"}
    isolated_tasks.write_text(json.dumps({"tasks": {"t_good": good, "t_bad": bad}}),
                              encoding="utf-8")

    ts.restore(path=str(isolated_tasks))
    out = capsys.readouterr().out
    assert "t_bad" not in ts.task_store, "认不出的状态被兜成了某个已知值"
    assert ts.get_task("t_bad") is None
    assert "跳过一条读不懂的任务记录" in out, f"坏记录不许静悄悄：实际输出 {out!r}"
    assert "t_bad" in out, "那句人话要点名是哪一条，否则运维无法定位"
    # 一条坏记录不许带走同库的好记录（既有约定，新状态同样受它保护）
    assert ts.get_task("t_good").status is ts.TaskStatus.WAITING_USER


def test_a_misshapen_cursor_is_scrubbed_not_fatal(isolated_tasks, capsys):
    """游标那一格坏了（不是 dict）：那一键退回默认，其余键照读。

    与 config_store.credit_benchmark() 的逐键消毒同纪律。
    负向锁：把 from_dict 里那个 isinstance 分支删掉，这条红——resume_cursor 会变成
    一个字符串，读侧（详情端点/UI）拿去取键就 500。
    """
    isolated_tasks.write_text(json.dumps({"tasks": {
        "t_c": {"task_id": "t_c", "user_id": "u-1", "goal": "g", "subtasks": ["a"],
                "status": "waiting_user", "resume_cursor": "不是表",
                "waiting_since": "2026-10-07T10:00:00",
                "created_at": "2026-10-01T09:00:00"},
    }}), encoding="utf-8")
    ts.restore(path=str(isolated_tasks))
    out = capsys.readouterr().out
    back = ts.get_task("t_c")
    assert back.resume_cursor is None, "坏游标被原样收下，读侧取键就炸"
    assert back.status is ts.TaskStatus.WAITING_USER and back.can_resume is True
    assert back.waiting_since == "2026-10-07T10:00:00", "坏一键不许带走同条其余好键"
    assert "resume_cursor" in out, "退回了默认要说出来，否则没人知道为什么没有游标"


# ---------- 消费方：新态不被当成结束 ----------

def test_the_detail_endpoint_answers_what_is_being_waited_for(client, enforced,
                                                              isolated_tasks):
    """GET /v1/tasks/{id} 必须把 waiting_user 原样说出去，并带三个新字段。

    负向锁：把状态在端点里映射成 "running"（前端看不出来在等人），这条红在
    `status`；删掉三行里的任意一行，红在对应那句断言。
    """
    headers = enforced("等待甲")
    me = client.get("/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    uid = me.json()["user_id"]

    task = ts.Task(goal="下单要验证码", subtasks=["选店", "下单", "输验证码"],
                   user_id=uid)
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    task.wait_for_user("请输入短信验证码")

    res = client.get(f"/v1/tasks/{task.task_id}", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "waiting_user", f"新态被端点吞了：{body['status']!r}"
    assert body["can_resume"] is True
    assert body["resume_cursor"]["question"] == "请输入短信验证码"
    assert body["waiting_since"] == task.waiting_since


def test_a_waiting_task_is_cancellable_through_the_route_and_it_reaches_disk(
        client, enforced, isolated_tasks):
    """/v1/tasks/{id}/cancel 对 waiting_user 有效：它不是终态。

    这一条同时钉两件事：端点那句"已处于终态"的名单**不含** waiting_user
    （把它加进去就等于让等着的人连放弃都做不到），以及取消真的写进了盘。

    负向锁：往 main.py 那个 `[COMPLETED, FAILED, CANCELLED]` 里加一个
    TaskStatus.WAITING_USER，这条红在第一句（返回 status=warning）。
    """
    headers = enforced("等待乙")
    uid = client.get("/v1/auth/me", headers=headers).json()["user_id"]

    task = ts.Task(goal="下单", subtasks=["a", "b"], user_id=uid)
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    task.wait_for_user("请输入验证码")

    res = client.post(f"/v1/tasks/{task.task_id}/cancel", headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "cancelled", res.json()
    ts.restore(path=str(isolated_tasks))
    assert ts.get_task(task.task_id).status is ts.TaskStatus.CANCELLED


def test_the_table_is_reachable_from_the_module_for_other_consumers():
    """跳转表与 `can_transition` 是模块级公共判据，不是藏在方法里的 if。

    下一个消费方（可挂起的工具协议、UI 那半边）要问"这一步能不能走"时，读的是
    同一张表；抄第二份就是漂移的开始。
    """
    assert ts.can_transition("running", "waiting_user") is True
    assert ts.can_transition("pending", "waiting_user") is False
    assert set(ts.ALLOWED_TRANSITIONS) == set(ts.TaskStatus), "每个状态都得有一行"
    assert ts.save is not None and callable(ts.save)
