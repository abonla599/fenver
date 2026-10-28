"""v0.35 A2 行为契约：一次性审批票据活得过一次重启，且 TTL 是可配的。

规划提案 §二 的第二块地基：票据存进程内存（`_tickets`）+ TTL 硬编码 60 秒，于是
"跑三分钟的任务，第二次确认时票据已作废，用户被迫重跑"。§四 v0.35 判据 3 要的是
"一张审批票据在第 4 步仍有效（TTL 已按长任务放宽）"。

这一票把那张表搬进 `data/approvals.json`，并把判据拆成六件事，每件事都有负向控制：

1. **往返**：建票 → 「重启」（同一份文件上再 `restore()`）→ 同一张票仍可被属主消费、
   仍然能向执行器放行。
2. **一次性跟着盘走**：消费过再重放，重启之后仍然拒绝（这是本票最贵的一条——落盘把
   "能力"从进程生命周期里救出来，如果 consumed 不跟着走，救出来的就是一张复活的能力）。
3. **盘上只存摘要**：键与文件都是 `sha256:<hex>`，明文 ticket_id 一个字节都不落盘。
   抄的是 auth.py 的既有纪律（会话令牌与 device_secrets 都以摘要为键，
   `hash_token` / `hash_secret` 定义摘要口径、按摘要查记录、单次使用即从表里弹出），
   且**是第三个名字**而不是把三类凭据塞进同一张表——受众隔离那句理由就写在
   `hash_secret` 的 docstring 里。
4. **扫描不再只挂在建票时**：落盘之后"没人建票就没人扫"会从内存问题变成磁盘问题
   （过期记录带着参数与执行结果长期留在文件里）。现在建/消费/查/回填/装载五个入口都扫，
   仍然不起后台线程。
5. **TTL 走 config_store**：新键 `approval_ticket_ttl_seconds`，默认 60.0 —— **就是
   原先那个数**，没配过的部署行为一字不变。
6. **多租户**：B 读不到、消费不掉、也数不出 A 的票据（跨重启同样成立）。这个项目已经为
   一次跨用户泄露付过账。

另附"阻塞 I/O 不在事件循环上"的那条判据（本仓 524 事故的形状）：A2 之后审批层多了磁盘写，
而这条路上没有任何 `to_thread` 卸载（全仓 grep 不到一处）——挡着的是"同步 def 路由走线程池"
与"工具循环住在生产线程"这两件事。所以钉两层：源码级契约锁
`test_the_disk_touching_paths_are_all_sync_so_the_write_never_sits_on_the_loop`，
加一条现场证据 `test_the_confirm_write_lands_off_the_event_loop`（在落盘那一刻问
`asyncio.get_running_loop()` 要的是"这线程上没有循环"）。扫描"不起后台线程"另有
`test_the_sweep_costs_no_background_thread_and_no_timer` 钉着。
"""
import asyncio
import inspect
import json
import re
import time
from pathlib import Path

import pytest

from app.core import approval
from app.core import config_store
from app.core.auth import hash_token


@pytest.fixture(autouse=True)
def isolated_approvals(tmp_path, monkeypatch):
    """每条用例自己的 approvals.json；进出都把进程级表清空。

    与 test_task_store 那份夹具同一理由：`_tickets` 是进程级 dict，不带进带出的话任何
    一条按 id 断言的用例都可能撞上别人家的票据。真实 data/approvals.json 不是测试的靶子
    （覆盖判据在 test_test_isolation.py，清单从 paths.DATA_PATH_ENV_VARS 派生）。
    """
    path = tmp_path / "approvals.json"
    monkeypatch.setenv("APPROVALS_DB_PATH", str(path))
    approval._tickets.clear()
    approval._round_fused.clear()
    yield path
    approval._tickets.clear()
    approval._round_fused.clear()


def _restart(path):
    """等价于重启进程：内存表清空，一切从那份文件读回来。"""
    approval._tickets.clear()
    return approval.restore(path=str(path))


def _make(user_id="u-keep", code="print(42)"):
    req = approval.create_approval_request("execute_code", {"code": code,
                                                            "language": "python"},
                                           user_id=user_id)
    return req["ticket_id"], req


def _raw_file(path):
    return path.read_text(encoding="utf-8")


# ---------- 落点与"没有票据就没有文件" ----------

def test_the_file_lives_where_the_env_says_and_absence_is_not_a_write(isolated_approvals):
    """APPROVALS_DB_PATH 指到哪就写到哪；而**导入期那次 restore 不许建文件**。

    判据同 test_task_store 的"空账不该被凭空造出来"：现网那份 data/approvals.json
    要么是真的有票据，要么根本不存在——不存在却每次启动被建出一个空壳，运维就分不清
    "这功能从没被用过"与"表被清空过"。

    负向锁：把 `restore()` 里 `except FileNotFoundError: return 0` 改成"按空表落一次盘"
    （`_flush_locked(); return 0`），这条红在最后那句 `not exists()`。
    """
    assert Path(approval._default_path()) == isolated_approvals, \
        "环境变量没说话不算：落点必须由 APPROVALS_DB_PATH 决定"
    assert approval._default_path().endswith("approvals.json")
    approval.restore()        # 没有票据：读一次，不该产出任何文件
    with approval._lock:
        assert approval._tickets == {}
    assert not isolated_approvals.exists(), (
        "盘上没有就是没有——读一次就建出个空壳，运维就分不清『从没用过』与『表被清空』")


def test_a_ticket_survives_a_restart_and_still_grants_execution(isolated_approvals):
    """提案 §四 判据 3 的正向一半：建票 → 重启 → 属主仍能消费，且执行器为它放行。

    负向锁：把 create_approval_request 里的 `_flush_locked()` 删掉，这条红在
    `_restart(...) == 0`；把 `_from_record` 的 `expires_at` 读丢，红在 is_granted。
    """
    tid, req = _make()
    assert _restart(isolated_approvals) == 1, "票据没落盘：重启即丢，用户被迫重跑"

    stored = approval.get_ticket(tid)
    assert stored is not None, "重启后按同一个 id 查不到——摘要键没对上"
    assert stored.tool_name == "execute_code" and stored.user_id == "u-keep"
    assert stored.consumed is False and stored.decision is None
    assert stored.arguments == {"code": "print(42)", "language": "python"}, \
        "参数快照没跟着回来：confirm 端点会执行一个空 args"
    assert abs(stored.expires_at - req["expires_at"]) < 1e-6

    ticket = approval.consume_ticket(tid, "allow", "u-keep")
    assert ticket is not None and ticket.decision == "allow"
    assert approval.is_granted(ticket, "execute_code", "u-keep") is True, \
        "重启后放不了行：这一票就白做"
    # 一张票据只放行它自己那次调用，这条判据跨重启同样成立
    assert approval.is_granted(ticket, "web_search", "u-keep") is False


def test_the_get_route_answers_the_plain_id_a_client_already_holds(isolated_approvals,
                                                                  client):
    """盘上只有摘要，所以刚重启完那一张手里没有明文 id——响应必须把它补回来。

    这是 `_present()` 的全部意义：客户端拿的是 uuid，轮询响应里的 `ticket_id` 若变成
    摘要串或空串，双端契约就断了（接口形状那份定稿写的是 uuid-v4）。
    负向锁：把 `_present` 里那句回填删掉，这条红在 `body["ticket_id"] == tid`。
    """
    tid, _ = _make(user_id="default_user")     # disabled 模式下 client 就是本机管理员
    assert _restart(isolated_approvals) == 1

    res = client.get(f"/v1/approval/{tid}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ticket_id"] == tid, "回给客户端的必须是它手里那一枚明文 id"
    assert body["status"] == "pending"
    assert body["tool_name"] == "execute_code"


# ---------- 一次性：重放跨重启仍被拒 ----------

def test_single_use_semantics_survive_the_restart(isolated_approvals):
    """判据 5（本票最贵的一条）：消费过 → 重启 → 再消费同一个 id，直接 None。

    落盘把"能力"从进程生命周期里救出来；如果 consumed 不跟着走，救出来的就是一张
    能再执行一次的复活凭据。

    负向锁：`consume_ticket` 里那句 `if ticket.consumed: return None` 一删，这条红在
    第二次 consume；把 `dirty = True` 那行删掉（改了内存不落盘），红在重启之后的第二次。
    """
    tid, _ = _make()
    first = approval.consume_ticket(tid, "allow", "u-keep")
    assert first is not None and first.consumed is True

    assert _restart(isolated_approvals) == 1
    replay = approval.consume_ticket(tid, "allow", "u-keep")
    assert replay is None, "已消费的票据跨重启复活了——那是一次可重放的执行凭据"

    on_disk = json.loads(_raw_file(isolated_approvals))["tickets"][approval.hash_ticket(tid)]
    assert on_disk["consumed"] is True and on_disk["decision"] == "allow", \
        "一次性这件事必须写在盘上，而不是只写在这一进程的内存里"


def test_a_timeout_written_before_the_restart_is_not_granted_after(isolated_approvals):
    """到期作废那条也落盘：重启后那张仍是 timeout，不是"再看一次时间就放行"。

    负向锁：把超时分支的 `dirty = True` 删掉，这条红在重启后读回的 decision。
    """
    tid, _ = _make()
    with approval._lock:
        approval._tickets[approval.hash_ticket(tid)].expires_at = time.time() - 1
    assert approval.consume_ticket(tid, "allow", "u-keep") is None

    assert _restart(isolated_approvals) == 1
    back = approval.get_ticket(tid)
    assert back is not None and back.decision == "timeout" and back.consumed is True
    assert approval.is_granted(back, "execute_code", "u-keep") is False


# ---------- 盘上格式：摘要，不是明文 ----------

def test_the_disk_holds_the_digest_never_the_plain_id(isolated_approvals):
    """文件里不许出现明文 ticket_id；表键就是 `sha256:<64 hex>`。

    抄的是 auth.py 的 `hash_token` / `hash_secret` 两个函数与它们各自的表：
    凭据只存摘要，查找时把呈现方带来的原文算一次再比。这里复用同一个**原语**
    （`hash_ticket` 调 hash_token），但坚持是第三个名字——受众隔离。

    有了这个文件能读到什么、读不到什么，交付报告里逐条写了；这条锁只钉"能读到的东西
    里不含可重放的凭据"。

    负向锁：把 `_to_record` 里 `record.pop("ticket_id", None)` 删掉，这条红在第一句。
    """
    tid, _ = _make(user_id="u-disk")
    text = _raw_file(isolated_approvals)
    assert tid not in text, "明文票据 id 落盘了：拿到文件就能拿去confirm（还差属主登录态）"
    payload = json.loads(text)["tickets"]
    digest = approval.hash_ticket(tid)
    assert digest in payload, "表键不是摘要——那内存与盘上就是两个形状"
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", k) for k in payload), \
        f"键的形状与 auth 那两张凭据表不一致：{list(payload)}"
    assert digest == hash_token(tid), "摘要算法必须是 auth.py 那个原语，不许另起一套"
    assert "ticket_id" not in payload[digest], "序列化漏了一格明文"


def test_an_unreadable_file_is_backed_up_and_grants_nothing(isolated_approvals, capsys):
    """整份文件读不懂：备份成 .corrupt 后按"没有票据"跑（fail-closed，且响）。

    与 task_store/providers/config 同一形状——坏掉的东西要看得见，不能被当成空。
    方向在这里是安全的：按空表跑等于一张票据都不放行，用户重批一次；反过来"猜着恢复"
    才是造出鬼凭据。

    负向锁：把那句 os.replace 备份删掉，这条红在 `.corrupt` 不存在。
    """
    isolated_approvals.write_text("{这不是 JSON", encoding="utf-8")
    assert _restart(isolated_approvals) == 0
    out = capsys.readouterr().out
    assert "审批票据读不了" in out, f"读不懂要说出来：{out!r}"
    assert isolated_approvals.with_suffix(".json.corrupt").exists(), \
        "坏文件必须留在原地可查，而不是被下一次写入覆盖"
    # 一张都不许放行：拿任何 id 来都当不存在
    assert approval.consume_ticket("whatever", "allow", "u-x") is None
    assert approval.get_ticket("whatever") is None


def test_a_record_without_an_owner_is_dropped_not_loaded(isolated_approvals, capsys):
    """手改出一条没有属主的票据：丢掉并点名，不带着它进 is_granted。

    这条不是洁癖：is_granted 比的是 `ticket.user_id != (user_id or "")`，一张属主为空
    的记录会跟"没带身份"的调用对上——那正是一次跨用户执行能力的形状。

    负向锁：把 `_from_record` 里那句属主/工具名的空值检查去掉，这条红。
    """
    digest = approval.hash_ticket("fake-id-1")
    isolated_approvals.write_text(json.dumps({"tickets": {digest: {
        "tool_name": "execute_code", "arguments": {"code": "print(1)"},
        "plan_summary": "s", "user_id": "", "created_at": time.time(),
        "expires_at": time.time() + 60, "consumed": False}}}), encoding="utf-8")
    assert _restart(isolated_approvals) == 0
    assert "跳过一张读不懂的审批票据" in capsys.readouterr().out
    assert approval.get_ticket("fake-id-1") is None, "无主票据被读回来了"


# ---------- 扫描：五个入口都扫，且不起线程 ----------

@pytest.mark.parametrize("entry", ["get", "consume", "record"])
def test_the_sweep_now_runs_at_every_entry_point_not_only_on_create(isolated_approvals,
                                                                    entry):
    """A2 补的那个洞：票据落盘后，"只在建票时扫"等于过期能力长期躺在文件里。

    这条**一次新票据都不建**：先把 a 做旧到超出保留窗口、回写一次盘（内存与盘上同一条
    旧票），然后只走一个读/写入口（get / consume / record_execution 之一），
    断言 a 从内存与文件里一起消失。

    负向锁：把那个入口里的 `_sweep_locked(...)` 调用删掉，这条对着那个 param 红。
    改动前的代码只有 create_approval_request 扫，所以三条都红——这就是原洞的现场。
    """
    stale_id, _ = _make(user_id="u-old")
    fresh_id, _ = _make(user_id="u-new")
    with approval._lock:
        old = approval._tickets[approval.hash_ticket(stale_id)]
        old.expires_at = time.time() - 10 * approval.ticket_ttl_seconds()
        # 做旧之后**必须回写一次盘**：只改内存的话盘上仍是那个新鲜的到期时刻，
        # 这条测试就退化成"在验内存自己"。上一版正是这么写的（先做旧再 `_restart`），
        # 于是 `_restart` 从盘上读回来两张活票、断言"这条入口没扫"当场红——红得对，
        # 但红在夹具没setup好，而不是红在判据上。这里把前置钉成"内存与盘上同一条旧票"。
        # 也不走 `_restart`：超出保留窗口的那一张在装载时就被 restore 拒了（那是另一条
        # 测试 test_a_ticket_past_the_grace_window_is_never_resurrected 钉的东西），
        # 本条要复现的是原洞本身——一个**不重启的长命进程**里没人建票。
        approval._flush_locked()
    keys_before = json.loads(_raw_file(isolated_approvals))["tickets"]
    assert approval.hash_ticket(stale_id) in keys_before, "前置：旧票得先在盘上"
    assert approval.hash_ticket(fresh_id) in keys_before, "前置：新票也得在盘上"

    if entry == "get":
        assert approval.get_ticket("not-a-ticket") is None
    elif entry == "consume":
        assert approval.consume_ticket("not-a-ticket", "allow", "u-x") is None
    else:
        approval.record_execution("not-a-ticket", "no such ticket", False)

    # 校验必须**不走这张表的读接口**：拿 `get_ticket` 去验"record_execution 那条入口扫了
    # 没有"，get_ticket 自己会扫一次并回写，坏掉的那条入口就被下一行掩盖了——本条最初
    # 就是这么空转的（把 record_execution 里的扫描删掉，它照样绿）。所以这里直接看表。
    stale_key, fresh_key = approval.hash_ticket(stale_id), approval.hash_ticket(fresh_id)
    with approval._lock:
        assert stale_key not in approval._tickets, f"{entry} 这条入口没扫：过期票据留在内存"
        assert fresh_key in approval._tickets, "扫描不许误删还在窗口里的"
    keys = json.loads(_raw_file(isolated_approvals))["tickets"]
    assert stale_key not in keys, \
        f"内存扫掉了、盘上 {entry} 之后还留着——那才是文件只增不减的根因"
    assert fresh_key in keys


def test_a_ticket_past_the_grace_window_is_never_resurrected(isolated_approvals):
    """装载时就该拒：到期太久的条目根本不复活。

    落盘换来的必须是"活得更久而合法"，不是"过期凭据跨重启诈尸"。
    负向锁：把 restore() 里那句 `now > ticket.expires_at + grace` 的 continue 删掉，
    这条红在 loaded == 0。
    """
    dead_id, _ = _make(user_id="u-dead")
    alive_id, _ = _make(user_id="u-alive")
    with approval._lock:
        approval._tickets[approval.hash_ticket(dead_id)].expires_at = (
            time.time() - 5 * approval.ticket_ttl_seconds())
        approval._tickets[approval.hash_ticket(alive_id)].expires_at = (
            time.time() - 1)              # 刚过期，还在保留窗口内（客户端还能取结果）
        # 同上一条的理由：做旧只留在内存里，`_restart` 读回来的就是盘上那两个新鲜时刻，
        # "装载时按盘上的到期时间拒绝"这条判据根本没被喂到。
        approval._flush_locked()

    assert _restart(isolated_approvals) == 1
    assert approval.get_ticket(dead_id) is None, "过期的能力记录被复活了"
    kept = approval.get_ticket(alive_id)
    assert kept is not None and kept.expires_at < time.time(), \
        "刚过期的那张仍要读得回来（消费它的那一次会照直判 timeout）"


# ---------- 周期清理：只清窗口外的，刚消费的留给轮询 ----------

def test_sweep_stale_removes_a_ticket_past_one_extra_ttl_and_flushes(isolated_approvals):
    """到期之后又过一个 TTL 的票据：内存删掉，盘上也要删掉。

    审计 M-5 的清理面：票据落盘后只增不减，过期记录带着参数与执行结果长期躺在
    data/approvals.json 上。周期清理必须把盘上那一份一起带走。

    负向锁：把 `sweep_stale` 里的 `_flush_locked()` 删掉，红在盘上那条断言。
    """
    tid, _ = _make(user_id="u-stale")
    key = approval.hash_ticket(tid)
    with approval._lock:
        approval._tickets[key].expires_at = (
            time.time() - approval.ticket_ttl_seconds() - 1)
        # 做旧必须回写：只改内存的话，"盘上也删掉了"就成了在验内存自己
        approval._flush_locked()
    assert key in json.loads(_raw_file(isolated_approvals))["tickets"], \
        "前置：旧票得先在盘上"

    assert approval.sweep_stale() == 1
    with approval._lock:
        assert key not in approval._tickets
    assert key not in json.loads(_raw_file(isolated_approvals))["tickets"], \
        "内存删了、盘上还留着——过期票据会带着参数与结果一直躺在文件里"


def test_sweep_stale_keeps_a_just_consumed_ticket_for_the_polling_window(
        isolated_approvals):
    """与 `cleanup_expired` 的关键区别：刚消费的票在窗口内不许被周期清掉。

    confirm 之后客户端紧跟着 GET 取执行结果；周期清理若按 `cleanup_expired` 的判据
    （consumed 立刻删），那次轮询就是 404。这正是"把 cleanup_expired 挂上周期"会
    长出来的回归，本条钉住周期那头用的是惰性同口径。

    负向锁：把 `sweep_stale` 的判据换成 `t.consumed or now > t.expires_at`，这条
    红在 `sweep_stale() == 0` 之后的取票断言。
    """
    tid, _ = _make(user_id="u-poll")
    assert approval.consume_ticket(tid, "allow", "u-poll") is not None
    approval.record_execution(tid, "✓ ok", True)

    assert approval.sweep_stale() == 0, \
        "刚消费的票被周期清理删了：confirm 之后紧接的那次 GET 会 404"
    back = approval.get_ticket(tid)
    assert back is not None and back.consumed is True and back.result == "✓ ok", \
        "轮询窗口内取不到执行结果"
    assert approval.hash_ticket(tid) in json.loads(
        _raw_file(isolated_approvals))["tickets"], "盘上那份也要留着，否则重启后轮询断掉"


def test_sweep_stale_keeps_unexpired_and_just_expired_tickets(isolated_approvals):
    """未过期的、刚过期但还在一个 TTL 宽限里的，都留着。

    负向锁：把 `_sweep_locked` 的判据从 `expires_at + grace` 改成 `expires_at`，
    这条红在"刚过期"那张上。
    """
    fresh_id, _ = _make(user_id="u-fresh")
    just_expired_id, _ = _make(user_id="u-grace")
    with approval._lock:
        approval._tickets[approval.hash_ticket(just_expired_id)].expires_at = (
            time.time() - 1)
        approval._flush_locked()
    fresh_key = approval.hash_ticket(fresh_id)
    grace_key = approval.hash_ticket(just_expired_id)

    assert approval.sweep_stale() == 0
    with approval._lock:
        assert fresh_key in approval._tickets, "周期清理误删了未过期的票"
        assert grace_key in approval._tickets, "刚过期的票还在轮询宽限里，不许清"
    on_disk = json.loads(_raw_file(isolated_approvals))["tickets"]
    assert fresh_key in on_disk and grace_key in on_disk


# ---------- TTL：可配，且默认值没变 ----------

def test_ttl_default_is_unchanged_and_the_knob_is_config_store_only(monkeypatch):
    """默认必须还是 60 秒——没配过的部署行为不许有任何变化。

    单一真相在 config_store 那一格，approval.py 只是读数；不许有第二个 60。
    负向锁：把 DEFAULTS 那格改成 120，这条红；把建票改成读常量而不是
    `ticket_ttl_seconds()`，第二条红。
    """
    assert config_store.DEFAULTS["approval_ticket_ttl_seconds"] == 60.0
    assert approval.ticket_ttl_seconds() == 60.0
    monkeypatch.setenv("APPROVAL_TICKET_TTL_SECONDS", "300")
    assert approval.ticket_ttl_seconds() == 300.0, "环境变量必须每次现读，改了不必重启"


def test_a_long_task_window_comes_from_the_config(monkeypatch, isolated_approvals):
    """把 TTL 调成 300 秒：新票据的到期时刻与重启后的保留窗口都跟着走。

    这条就是提案 §四 判据 3 的"第 4 步仍有效"：60 秒给不了，5 分钟给得了。
    负向锁：create_approval_request 里把 `ttl = ticket_ttl_seconds()` 换回常量，红。
    """
    monkeypatch.setenv("APPROVAL_TICKET_TTL_SECONDS", "300")
    tid, req = _make()
    assert req["expires_at"] - req["created_at"] == 300.0
    assert _restart(isolated_approvals) == 1
    assert approval.consume_ticket(tid, "allow", "u-keep") is not None, \
        "配好的窗口没跟着落盘"


def test_a_garbage_ttl_falls_back_and_says_so(monkeypatch, capsys):
    """TTL 配成鬼话（不是数）：退回那一格的真值并说一句，不炸也不静。

    负向锁：把 except ValueError 那支的 print 删掉，红在第二句。
    """
    monkeypatch.setenv("APPROVAL_TICKET_TTL_SECONDS", "五分钟")
    assert approval.ticket_ttl_seconds() == 60.0
    assert "APPROVAL_TICKET_TTL_SECONDS 不是数" in capsys.readouterr().out


# ---------- 多租户：B 读不到、消费不掉、数不清 ----------

def test_user_b_cannot_consume_read_or_enumerate_user_as_ticket(isolated_approvals):
    """跨用户反例（模块层 + 跨重启）：B 的每一次尝试都是 None，而且**不烧掉** A 那张票。

    "读不到"与"数不清"在这里是同一件事的两面：表键是摘要，B 手里没有 A 的明文 id 就
    无从构造一个键；而 A 的票据即使被 B 猜到了 id，属主比对也先于任何改动。
    这个项目已经为一次跨用户泄露付过账（记忆泄露那回），所以这一条三条断言都要在。

    负向锁：把 consume_ticket 里那句 `if ticket.user_id != requester_user_id` 删掉，
    这条红在第一句（B 会拿到一张票据）；把 `if ticket.consumed` 顺序挪到属主检查之前
    也红不了这里——所以这里钉的是"跨属主不许把别人的能力打码成 404 的 DoS"。
    """
    tid, _ = _make(user_id="u-alice")

    assert approval.consume_ticket(tid, "allow", "u-bob") is None, "跨属主消费成功"
    assert approval.is_round_fused("u-bob", "execute_code") is False, \
        "B 的失败尝试不该改写 A 的熔断账"

    _restart(isolated_approvals)
    assert approval.consume_ticket(tid, "allow", "u-bob") is None, "跨重启后跨属主消费成功"
    # A 自己还能消费：B 来过一次就把这张好票烧掉了，那是一条 DoS 信道
    still = approval.consume_ticket(tid, "allow", "u-alice")
    assert still is not None and still.user_id == "u-alice"

    # "数不清"：文件里没有任何可枚举的明文 id，键全是摘要
    keys = list(json.loads(_raw_file(isolated_approvals))["tickets"])
    assert all(k.startswith("sha256:") for k in keys)
    assert tid not in _raw_file(isolated_approvals)


def test_the_http_read_refuses_a_non_owner(client, enforced, isolated_approvals):
    """GET /v1/approval/{id} 对非属主是 404，与"根本没有这张票"同一句话。

    模块层的 get_ticket 不做属主比对（判据在调用点），所以这条从 HTTP 边界钉住它——
    否则"轮询别人的票据"是一条探测信道。
    负向锁：把 main.py 那句 `ticket.user_id != principal.user_id` 删掉，这条红在 404。
    """
    headers = enforced("审批乙")
    tid, _ = _make(user_id="u-not-bob")
    res = client.get(f"/v1/approval/{tid}", headers=headers)
    assert res.status_code == 404, res.text
    assert res.json()["detail"] == "审批票据不存在或已失效", "跨属主与不存在必须同一句话"


# ---------- 阻塞 I/O 不许坐在事件循环上（本仓 524 事故的形状） ----------

def test_the_disk_touching_paths_are_all_sync_so_the_write_never_sits_on_the_loop():
    """源码级契约锁：每一个会碰这张表的入口都必须是同步的、跑在事件循环之外。

    A2 给审批层新增了磁盘写。写一旦落在事件循环上，一个慢盘（或一次 AV 扫描）就会
    冻住整台服务——那正是本仓为流式端点记过的那笔账：`main.py` 里 stream 端点的
    docstring 写着「端点与生成侧都必须是同步的：模型 token 是从阻塞 socket 上读出来的，
    留在事件循环里会让一个慢请求冻住整台服务（线上 524）」。同一条纪律这里照抄一遍，
    因为票据表现在也要落盘。本仓**没有** `to_thread`/`run_in_threadpool` 那套显式卸载
    （全仓 grep 不到一处），靠的是"同步 def 路由 + 专用生产线程"这两件事：
    - FastAPI 对 `def`（非 async）路由的处理是丢进 anyio 线程池，写发生在 worker 线程；
    - 建票的流式调用点 `streaming.stream_chat_events` 是同步生成器，它的函数体只在
      `main.launch_stream_run` 起的 `threading.Thread(target=_produce_stream)` 里跑。

    钉四个入口 + 生产者线程 + 落盘原语本身：
    - `approval` 的五个读写函数（create/consume/get/record/restore）不是协程；
    - `atomic_write.write_json_atomic` 不是协程（它是 os 级阻塞写）；
    - 两条 HTTP 入口 `confirm_approval`、`get_approval_status` 是 `def` 不是 `async def`；
    - 生成侧 `stream_chat_events` 仍是**同步生成器**、`_produce_stream` 是同步函数、
      `launch_stream_run` 真的起了线程；同步聊天那条 `ChatPipeline._call_model_with_tool_loop`
      与 `chat` 路由也是 `def`。

    负向锁：把 main.py 的 `def get_approval_status` 改成 `async def`，第二条断言红；
    把 `launch_stream_run` 里的 `threading.Thread(target=_produce_stream` 换成同步直调，
    最后一条红。运行时那一半另有 `test_the_confirm_write_lands_off_the_event_loop`。
    """
    import asyncio
    import app.main as main
    from app.core import atomic_write, streaming
    from app.pipeline import ChatPipeline
    from app.core import approval as _ap

    source = inspect.getsource(_ap)
    assert "await " not in source, "审批层里出现了 await——这张表的写要在协程里跑了"
    for fn in (_ap.create_approval_request, _ap.consume_ticket, _ap.get_ticket,
               _ap.record_execution, _ap.restore):
        assert not asyncio.iscoroutinefunction(fn), f"{fn.__name__} 变成了协程：写盘会坐在循环上"

    assert not asyncio.iscoroutinefunction(atomic_write.write_json_atomic)
    for route in (main.confirm_approval, main.get_approval_status):
        assert not asyncio.iscoroutinefunction(route), (
            f"{route.__name__} 成了 async def——同步路由才会被丢进线程池，"
            "async 路由里的阻塞写就是那个 524")

    assert inspect.isgeneratorfunction(streaming.stream_chat_events), (
        "stream_chat_events 不再是同步生成器：生产线程之外没人替它挡住事件循环")
    assert not asyncio.iscoroutinefunction(main._produce_stream)
    assert not asyncio.iscoroutinefunction(ChatPipeline._call_model_with_tool_loop)
    assert not asyncio.iscoroutinefunction(main.chat)
    assert "threading.Thread(target=_produce_stream" in inspect.getsource(main.launch_stream_run), (
        "生产线程被去掉了：工具循环（含建票落盘）会回到调用方的线程上")


def test_the_confirm_write_lands_off_the_event_loop(client, isolated_approvals,
                                                    monkeypatch):
    """运行时的同一件事：HTTP 确认这一次落盘，发生在**没有事件循环的线程**上。

    上一条是源码形状，这一条是现场。判据用 `asyncio.get_running_loop()`：worker 线程里
    它必然抛 RuntimeError（这线程上没有循环），而在协程上下文里它会返回那个循环。
    所以"写盘时拿不到运行中的循环"就是"写不在循环上"的直接证据，不依赖 anyio 的线程命名。

    用 reject 而不是 allow：这条要的是那一次写，不必真的执行代码。
    负向锁：把 main.py 的 `def confirm_approval` 改成 `async def`，这条红在
    `loop_was_running`（循环就在那一层上）。
    """
    from app.core import approval as _ap

    seen = []
    real_flush = _ap._flush_locked

    def spy():
        try:
            asyncio.get_running_loop()
            seen.append("loop_was_running")
        except RuntimeError:
            seen.append("no_loop_here")
        real_flush()

    monkeypatch.setattr(_ap, "_flush_locked", spy)
    tid, _ = _make(user_id="default_user")
    before = list(seen)                     # 建票那一次写也在线程池/调用线程上，不该有循环
    res = client.post("/v1/approval/confirm",
                      json={"ticket_id": tid, "decision": "reject"})
    assert res.status_code == 200, res.text
    assert seen[len(before):], "confirm 这趟一次盘都没落：那 A2 的落盘判据根本没跑到"
    assert seen == ["no_loop_here"] * len(seen), (
        f"审批落盘出现在了事件循环线程上：{seen}")


def test_the_sweep_costs_no_background_thread_and_no_timer():
    """扫这个洞是用"每个入口顺手一次"补的，不是用常驻线程/定时器补的。

    提案与本仓的既有结论一致（`session/export_store.py` 模块 docstring 里那句
    "为一张几行的小表养一条常驻线程，
    换来的是每个测试进程里多一个关不掉的幽灵"）：不起线程。这条锁钉的是"以后别图省事加一条"。

    负向锁：把 `_sweep_locked` 换成 `threading.Timer(60, ...)` 常驻，第一条红；
    把文件末尾那行 `restore()` 注释掉，红在最后一条。
    """
    source = inspect.getsource(approval)
    for banned in ("threading.Thread", "threading.Timer", "create_task", "asyncio.ensure_future"):
        assert banned not in source, f"审批层里出现了 {banned}：扫描要靠惰性入口，不是常驻任务"
    assert re.search(r"^restore\(\)$", source, re.M), (
        "导入期那次装载没了（或被注释掉了）：票据表从此活不过重启")

