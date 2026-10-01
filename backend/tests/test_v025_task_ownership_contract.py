"""v0.25 R1 行为契约：任务这一面对登录用户开放，但归属判定先于一切。

test_route_auth_contract 钉的是"声明"（守卫挂在依赖树上、端点体里出现
principal.user_id），那份判据对"摘掉一处归属校验"这种改法只有源码级直觉；
这里补的是**行为**：拿两个真用户、一枚真任务，从 HTTP 边界上把"读不到、
改不动、数不清"三件事各钉一遍。负向验证（故意摘掉 `_task_for_principal` 的
属主比较）时，红必须出现在本文件与契约文件，而不是某条没人读的注释里。

三组判据对应 PRD 三条：
- 跨用户反例（T1.3）：A 读/取消/删除 B 的任务 = 与根本不存在的 id 同一个
  404、同一句话——枚举 uuid 得到的是同一个答案，任务表因此不是探测信道。
- 归属与旧数据（T1.1）：Task 带 user_id 与 provider_id 落盘；无主的旧条目在
  启动恢复时认给部署者主账号并打 legacy 标——老数据不崩、老用户不看空列表。
- 出网走发起人配置（T1.4）：任务里的每一次模型调用都用**发起人**的池子与
  默认模型，账记在发起人头上；没配 provider 的是人话 400，不是 500；错误
  出口过 scrub_secrets；节流与聊天同一本账，被挡下的那一次一个 token 不花。
"""
import json
import os
import uuid

import pytest

from app.agents import task_store as ts
from app.core import usage
from app.core.authz import UNAUTHORIZED_DETAIL
from app.core.providers import store as provider_store
from tests import conftest
from tests.conftest import RECOVERY_FIELDS


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    """每条用例自己的 tasks.json；进与出都把进程级存储清空重恢复。

    task_store 是进程级 dict，端点写进去的任务若不带进带出，后面任何一条按
    id 断言的用例都可能撞上别人家的任务——那种红离真凶隔着整个文件。
    """
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    yield path
    for tid in list(ts.task_store):
        del ts.task_store[tid]


def _whoami(client, headers) -> str:
    res = client.get("/v1/auth/me", headers=headers)
    assert res.status_code == 200, res.text
    return res.json()["user_id"]


@pytest.fixture
def two_users(client, enforced):
    """两个真用户：注册走存储层（enforced 的 as_user），uid 从 /v1/auth/me 现取。"""
    ha = enforced("任务甲")
    hb = enforced("任务乙")
    return ha, _whoami(client, ha), hb, _whoami(client, hb)


ADMIN_HEADERS = {"Authorization": "Bearer boot-token"}


# ---------- T1.2/T1.3：跨用户反例与枚举 ----------


def test_task_endpoints_refuse_anonymous(client, enforced, two_users):
    """摘掉守卫的第一格不是"用户能看别人的任务"，是匿名就能看——先钉 401。

    GET 那半由 test_route_auth_contract 的全家桶扫过；POST cancel 不在 GET 扫描
    范围内，所以这一族里唯一能改状态的方法在这里单独证一次匿名不可达。
    """
    _, uid_b, _, _ = two_users
    task = ts.Task(goal="乙的任务", user_id=uid_b)
    ts.task_store[task.task_id] = task
    res = client.post(f"/v1/tasks/{task.task_id}/cancel")
    assert res.status_code == 401, res.text
    assert res.json()["detail"] == UNAUTHORIZED_DETAIL
    assert ts.get_task(task.task_id).cancelled is False, "被挡下的请求不许改动任务"


def test_stranger_cannot_read_or_cancel(client, enforced, two_users):
    """A 读、取消、删除 B 的任务，得到的必须是"不存在"——403 也算泄密。"""
    ha, uid_a, hb, uid_b = two_users
    mine = ts.Task(goal="甲自己的", user_id=uid_a)
    theirs = ts.Task(goal="乙的账单分析", user_id=uid_b)
    ts.task_store[mine.task_id] = mine
    ts.task_store[theirs.task_id] = theirs

    # 属主自己：读得到、取消得动（对照，防止"一律 404"也过这条）
    assert client.get(f"/v1/tasks/{mine.task_id}", headers=ha).status_code == 200
    res = client.post(f"/v1/tasks/{mine.task_id}/cancel", headers=ha)
    assert res.status_code == 200, res.text
    assert ts.get_task(mine.task_id).cancelled is True

    # 陌生人：三种方法、同一句话、同一个 404；不许有 403
    for headers, method, path in (
            (hb, "get", f"/v1/tasks/{theirs.task_id}"),
            (ha, "post", f"/v1/tasks/{theirs.task_id}/cancel"),
            (ha, "delete", f"/v1/tasks/{theirs.task_id}")):
        res = getattr(client, method)(path, headers=headers) if headers else \
            getattr(client, method)(path)
        assert res.status_code == 404, f"{method} {path} -> {res.status_code} {res.text}"

    assert ts.get_task(theirs.task_id).cancelled is False, "陌生人的 cancel 竟改动了任务"
    assert ts.get_task(theirs.task_id) is not None, "陌生人的 delete 竟删掉了任务"


def test_id_enumeration_gets_one_indistinguishable_answer(client, enforced, two_users):
    """拿着 B 的真 id 和三个随机 uuid 挨个问，得到的响应**逐字节相同**。

    "非属主与不存在返回同一个 404"是这么钉的：不是各自断言一下 404，而是把
    两者的响应体放在一起比。谁哪天给非属主单独换一句话（哪怕"无权访问"），
    任务表立刻变成 uuid 探测器，这条当场红。
    """
    ha, _, hb, uid_b = two_users
    theirs = ts.Task(goal="乙的秘密", user_id=uid_b)
    ts.task_store[theirs.task_id] = theirs

    answers = set()
    for probe in (theirs.task_id, str(uuid.uuid4()), str(uuid.uuid4())):
        res = client.get(f"/v1/tasks/{probe}", headers=ha)
        assert res.status_code == 404
        answers.add((res.status_code, res.text))
    assert len(answers) == 1, f"不存在与非属主给出了不同答案：{answers}"

    # A 的列表里既没有 B 的 id，也没有 B 的目标原文——goal 是泄露面最大的一格
    res = client.get("/v1/tasks", headers=ha)
    assert res.status_code == 200, res.text
    body = res.json()
    assert all(t["task_id"] != theirs.task_id for t in body["tasks"])
    assert "乙的秘密" not in res.text
    # B 自己看得到自己（防止"一律过滤成空"过这条）
    res = client.get("/v1/tasks", headers=hb)
    assert [t["task_id"] for t in res.json()["tasks"]] == [theirs.task_id]


def test_admin_sees_everything_and_says_whose_it_is(client, enforced, two_users):
    """admin 仍见全量，且每行带着"属于谁"——这是 v0.24 管理员面的既有承诺。

    响应里的 user_id 就是可辨识归属信息；缺了它，管理页排障只能靠猜。
    """
    _, uid_a, hb, uid_b = two_users
    ta = ts.Task(goal="甲的", user_id=uid_a)
    tb = ts.Task(goal="乙的", user_id=uid_b)
    ts.task_store[ta.task_id] = ta
    ts.task_store[tb.task_id] = tb

    res = client.get("/v1/tasks", headers=ADMIN_HEADERS)
    assert res.status_code == 200, res.text
    rows = {t["task_id"]: t for t in res.json()["tasks"]}
    assert {ta.task_id, tb.task_id} <= set(rows), "admin 的全量列表少了任务"
    assert rows[ta.task_id]["user_id"] == uid_a
    assert rows[tb.task_id]["user_id"] == uid_b
    # admin 读单条也给 200（"严格按属主"不等于把管理员锁在自己那一份里）
    assert client.get(f"/v1/tasks/{tb.task_id}",
                      headers=ADMIN_HEADERS).status_code == 200


def test_orchestrate_under_a_foreign_task_id_does_not_hijack(client, enforced,
                                                             two_users):
    """带别人的 task_id 来编排：既读不到也改不动，落到的是一条自己的新任务。

    orchestrator 对"不是你的 task_id"的既有语义是当作不存在、新建一条——这条
    钉的是那半句话的另一侧：新建出来的那条必须属于**调用者**，B 的原任务状态
    一个字不许变。
    """
    ha, uid_a, _, uid_b = two_users
    theirs = ts.Task(goal="乙的旧任务", subtasks=["第一步"], user_id=uid_b)
    ts.task_store[theirs.task_id] = theirs
    before = theirs.to_dict()

    res = client.post("/v1/agent/orchestrate",
                      json={"goal": "甲的新目标", "task_id": theirs.task_id},
                      headers=ha)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["task_id"] != theirs.task_id, "竟以 B 的任务 id 往下跑了"
    created = ts.get_task(body["task_id"])
    assert created.user_id == uid_a
    assert theirs.to_dict() == before, "B 的任务被甲的请求改动了"


# ---------- T1.1：归属字段与旧数据兼容 ----------


def test_task_carries_provider_id_through_the_file(isolated_tasks):
    """provider_id 与 user_id 一样要过一遍落盘：账与配置不能只活在内存那一晚。"""
    t = ts.Task(goal="g", user_id="u-1", provider_id="p_abc")
    ts.task_store[t.task_id] = t
    ts.restore(path=str(isolated_tasks))
    back = ts.get_task(t.task_id)
    assert back.provider_id == "p_abc"
    record = json.loads(isolated_tasks.read_text(encoding="utf-8"))["tasks"][t.task_id]
    assert record["provider_id"] == "p_abc"


def test_ownerless_legacy_records_are_claimed_by_the_deployer(isolated_tasks):
    """老 tasks.json 里没有 user_id 的那批：认给部署者主账号并打 legacy 标。

    判据三件套：不崩（restore 正常返回）、不归零（老用户在 default_user 名下
    仍看得到自己的历史）、落盘跟上（标注入盘，下次恢复不重复猜）。
    """
    isolated_tasks.write_text(json.dumps({"tasks": {
        "t_old": {"goal": "老任务", "subtasks": [], "status": "completed",
                  "created_at": "2026-01-01T00:00:00"},
    }}), encoding="utf-8")
    n = ts.restore(path=str(isolated_tasks))
    assert n == 1, "一条都读不回来——老数据不许被当成坏记录扔掉"
    back = ts.get_task("t_old")
    assert back.user_id == ts.LEGACY_OWNER
    assert back.legacy is True
    assert [t.task_id for t in ts.tasks_of(ts.LEGACY_OWNER)] == ["t_old"]
    on_disk = json.loads(isolated_tasks.read_text(encoding="utf-8"))["tasks"]["t_old"]
    assert on_disk["user_id"] == ts.LEGACY_OWNER and on_disk["legacy"] is True, \
        "回填只活在内存：下次重启又是一批无主孤儿"


def test_legacy_backfill_coexists_with_broken_and_good_records(isolated_tasks):
    """混合库：好记录照读、坏记录照跳过、无主的老记录照认领——一条坏数据不许带走整库。"""
    isolated_tasks.write_text(json.dumps({"tasks": {
        "t_good": {"goal": "有主的", "user_id": "u-1", "status": "completed",
                   "created_at": "2026-01-01T00:00:00"},
        "t_legacy": {"goal": "无主的", "status": "pending",
                     "created_at": "2025-12-31T23:00:00"},
        "t_broken": "这不是一条任务",
    }}), encoding="utf-8")
    ts.restore(path=str(isolated_tasks))
    assert ts.get_task("t_good").user_id == "u-1"
    assert ts.get_task("t_legacy").user_id == ts.LEGACY_OWNER
    assert ts.get_task("t_legacy").interrupted_by_restart is True, \
        "PENDING 的旧任务同时该有「被打断」与 legacy 两个标，一个不能顶掉另一个"
    assert ts.get_task("t_broken") is None


# ---------- T1.4：出网走发起人的配置、账记在发起人头上 ----------


def _add_private_provider(uid: str, paid_by: str = "user") -> str:
    rec = {"label": "甲自带模型", "base_url": "https://a.invalid/v1",
           "api_key": "sk-priv-a-0123456789", "model": "priv-chat",
           "paid_by": paid_by, "owner": uid}
    out = provider_store.upsert(rec)
    return out["id"]


def test_llm_calls_in_a_task_use_the_initiators_pool_and_default(client, enforced,
                                                                 two_users,
                                                                 monkeypatch):
    """发起人设了自己的默认模型，任务里的每次调用都必须用**他的**——
    全局默认顶替用户选择，正是这条禁令要防的"配置没生效"的根因形状。
    """
    ha, uid_a, _, _ = two_users
    pid = _add_private_provider(uid_a)
    try:
        provider_store.set_pref(uid_a, pid)

        seen = []
        fake = conftest._FakeClient()

        def spy_build(provider):
            seen.append(dict(provider))
            return fake

        from app.core import llm_client
        monkeypatch.setattr(llm_client, "build_client", spy_build)

        res = client.post("/v1/agent/orchestrate", json={"goal": "排一下今天"},
                          headers=ha)
        assert res.status_code == 200, res.text
        assert seen, "任务一次模型都没调——那账也没人记"
        assert all(p["id"] == pid for p in seen), \
            f"任务里出现了非发起人默认模型的调用：{[p['id'] for p in seen]}"
        # 任务本身也要带上这条 provider：续跑与排障都认这条记录，不认现场猜测
        assert ts.get_task(res.json()["task_id"]).provider_id == pid

        rows = [r for r in usage.snapshot() if r["user_id"] == uid_a]
        assert rows and all(r["provider_id"] == pid for r in rows), \
            f"账没记到发起人的这次调用上：{rows}"
        assert all(r["paid_by"] == "user" for r in rows), "自带 key 的账记成了 operator 垫钱"
    finally:
        provider_store.delete(pid)      # delete 顺手清掉指向它的偏好


def test_task_llm_falls_back_to_shared_default_without_a_pref(client, enforced,
                                                              two_users, monkeypatch):
    """没设过默认的人用站级共享默认——回落要有，但回落的是**他的池子**里的默认。"""
    ha, uid_a, _, _ = two_users
    seen = []
    fake = conftest._FakeClient()

    def spy_build(provider):
        seen.append(dict(provider))
        return fake

    from app.core import llm_client
    monkeypatch.setattr(llm_client, "build_client", spy_build)
    res = client.post("/v1/agent/orchestrate", json={"goal": "排一下"}, headers=ha)
    assert res.status_code == 200, res.text
    assert seen and all(p["id"] == "fake-model" for p in seen)
    rows = [r for r in usage.snapshot() if r["user_id"] == uid_a]
    assert rows and rows[0]["paid_by"] == "operator", "共享默认的账是 operator 垫的"


def test_no_provider_says_human_words_not_a_500(client, enforced, two_users):
    """一台什么都没配过的机器上点"编排"，要拿到一句人话，而不是内部错误。

    判据选 400 而不是 500/502：这是**调用方的配置状态**，不是服务故障；
    而文案必须过 scrub——"尚未配置任何模型服务，请在「设置 → 模型服务」中添加"
    这类实话可以长话短说，底层异常原文一个字都不许出现。
    """
    ha, uid_a, _, _ = two_users
    saved = provider_store.all()
    try:
        for p in saved:
            provider_store.delete(p["id"])
        res = client.post("/v1/agent/orchestrate", json={"goal": "排一下"}, headers=ha)
        assert res.status_code == 400, f"没配 provider 竟给出 {res.status_code}: {res.text}"
        assert "模型" in res.json()["detail"], res.text
        assert "Traceback" not in res.text and "Exception" not in res.text
        res2 = client.post("/v1/agent/run", json={"task": "算一下"}, headers=ha)
        assert res2.status_code == 400, res2.text
    finally:
        for p in saved:
            provider_store.upsert(p)


def test_failure_message_is_scrubbed_before_it_leaves(client, enforced, two_users,
                                                      monkeypatch):
    """执行失败的那句话出门前必须过 scrub_secrets：上游把 Authorization 原样
    打印回来不是假设，是网关常见做法——而这里的返回值会进界面、也会进落盘。
    """
    ha, uid_a, _, _ = two_users
    from app import main as main_module

    def boom(subtask, user_id=None, provider_id=None):
        raise RuntimeError("上游拒绝: api_key=sk-abcdefgh1234567890")

    monkeypatch.setattr(main_module.orchestrator.executor, "execute_task", boom)
    res = client.post("/v1/agent/orchestrate", json={"goal": "坏掉的目标"}, headers=ha)
    assert res.status_code == 200, res.text          # 失败是任务状态，不是 HTTP 500
    body = res.json()
    assert body["status"] == "failed"
    assert "sk-abcdefgh1234567890" not in res.text, f"密钥原文回了出口：{body['error']}"
    from app.core.providers import REDACTED
    assert REDACTED in body["error"], body["error"]
    assert ts.get_task(body["task_id"]).error == body["error"]


def test_agent_run_is_open_to_a_signed_user_and_bills_him(client, enforced, two_users):
    """/v1/agent/run 从管理员专享变成登录可用：账必须落在调用者头上。"""
    ha, uid_a, _, _ = two_users
    res = client.post("/v1/agent/run", json={"task": "算 2+2"}, headers=ha)
    assert res.status_code == 200, res.text
    rows = [r for r in usage.snapshot() if r["user_id"] == uid_a]
    assert rows and sum(r["calls"] for r in rows) >= 1, \
        "跑了一轮 ReAct 账本上却没有这个人——那又是一条不计费的旁路"


def test_orchestrate_shares_the_chat_throttle_and_blocks_for_free(client, enforced,
                                                                  two_users):
    """先判断、才叫模型：攒满 60 秒 20 次的预算后被挡下的那一次，账本纹丝不动。

    这条是"新增端点不许成为第二条不限流的旁路"的反例锁：把端点里那句
    throttle_paid_upstream 摘掉，第 21 次就从 429 变成 200，当场红。
    """
    ha, uid_a, _, _ = two_users
    from app.core import auth_router as ar
    for i in range(ar.MAX_CHAT_CALLS_PER_WINDOW):
        res = client.post("/v1/agent/orchestrate", json={"goal": f"刷第{i}遍"},
                          headers=ha)
        assert res.status_code == 200, res.text
    before = sum(r["calls"] for r in usage.snapshot() if r["user_id"] == uid_a)
    res = client.post("/v1/agent/orchestrate", json={"goal": "第21遍"}, headers=ha)
    assert res.status_code == 429, res.text
    after = sum(r["calls"] for r in usage.snapshot() if r["user_id"] == uid_a)
    assert after == before, "被挡下的请求还是叫了模型——记账顺序倒了"


# ---------- 落盘形状不回退（约束 4 的现行为锁） ----------


def test_tasks_file_keeps_owner_and_carries_legacy_fields(isolated_tasks):
    t = ts.Task(goal="g", user_id="u-1", provider_id="p_1")
    ts.task_store[t.task_id] = t
    payload = json.loads(isolated_tasks.read_text(encoding="utf-8"))
    record = payload["tasks"][t.task_id]
    assert record["user_id"] == "u-1"
    assert record["provider_id"] == "p_1"
    assert record["legacy"] is False
    if os.name == "nt":
        pytest.skip("权限位判据在 POSIX；Windows 上由 write_json_atomic 自己的用例钉")
    assert (isolated_tasks.stat().st_mode & 0o777) == 0o600
