"""工单 #30：审批确认端点不许把"挂在断点上等人"报成执行失败。

缺陷原文（`docs-internal` 台账 2026-10-16 第 4 条）：`POST /v1/approval/confirm` 的
allow 分支只认 `outcome.ok`，于是执行器交出的第三态（`suspended=True, ok=False`）
被回成 `executed_with_error`，`approval.record_execution(..., ok=False)` 还把票据
标成"执行失败"。现网到不了这一格（`executor.py` 的派发拒绝不让"又挂起又需审批"的
工具存在），所以这是**契约正确性**：一旦有人给一条 requires_approval 工具加上挂起行为
（或者像本文件这样只声明审批、函数体自己 raise），客户端看到的就是一张失败卡。

本票选定的最小**只增**契约（老客户端一字不用改）：
- confirm：`status` 多一个取值 `suspended`；`ok` 仍是 False（没有产出结果）；新增
  `suspended: True` 与 `question` 两格。`executed` / `executed_with_error` 两个老形状
  原样保留，只多一格 `suspended: False`。
- GET /v1/approval/{id}：同样多一格 `suspended`，且 `status` 答 `suspended`。
- 双端不需要跟着改：PWA 的 `confirmApproval`（api.js）与原生 `Api.confirmApproval`
  （Api.kt）都只读状态码、扔掉响应体——这一条由 `test_v031_approval_ui_contract.py`
  的调用点形状锁间接钉着，本文件只钉服务端契约。
"""
import json

import pytest

from app.core import approval
from app.tools.registry import tools_registry
from app.tools.response import ToolSuspended


@pytest.fixture(autouse=True)
def isolated_approvals(tmp_path, monkeypatch):
    """每条用例自己的 approvals.json；进出都把进程级表清空（抄 A2 那份夹具）。"""
    path = tmp_path / "approvals.json"
    monkeypatch.setenv("APPROVALS_DB_PATH", str(path))
    approval._tickets.clear()
    approval._round_fused.clear()
    yield path
    approval._tickets.clear()
    approval._round_fused.clear()


@pytest.fixture
def t30_tools():
    """一条"只声明审批、函数体却挂起"的工具 + 一条正常工具 + 一条失败工具。

    只声明 requires_approval 不声明 suspends 是刻意的：执行器那条"两者兼有就拒绝
    派发"的闸（工单 #37）因此不会先把它挡掉，这一格才是真正测 confirm 端点的第三态。
    """
    calls = []

    def t30_suspender(note: str = "x"):
        calls.append(("suspender", note))
        raise ToolSuspended("请输入短信验证码", {"step": 3})

    def t30_ok(note: str = "x"):
        calls.append(("ok", note))
        return "STDOUT: 2\n"

    def t30_fail(note: str = "x"):
        calls.append(("fail", note))
        raise RuntimeError("沙箱没起来")

    entries = {
        "t30_suspender": {"function": t30_suspender, "description": "要验证码",
                          "parameters": {"type": "object",
                                         "properties": {"note": {"type": "string"}}},
                          "available": None, "needs_user": False,
                          "requires_approval": True, "suspends": False},
        "t30_ok": {"function": t30_ok, "description": "正常跑完",
                   "parameters": {"type": "object",
                                  "properties": {"note": {"type": "string"}}},
                   "available": None, "needs_user": False,
                   "requires_approval": True, "suspends": False},
        "t30_fail": {"function": t30_fail, "description": "执行炸了",
                     "parameters": {"type": "object",
                                    "properties": {"note": {"type": "string"}}},
                     "available": None, "needs_user": False,
                     "requires_approval": True, "suspends": False},
    }
    saved = {name: tools_registry.get(name) for name in entries}
    for name, info in entries.items():
        tools_registry[name] = info
    try:
        yield calls
    finally:
        for name, previous in saved.items():
            if previous is None:
                tools_registry.pop(name, None)
            else:
                tools_registry[name] = previous


def _ticket(tool_name: str, user_id: str = "default_user") -> str:
    """建一张票。disabled 模式的 client 就是本机管理员（user_id=default_user）。"""
    return approval.create_approval_request(tool_name, {}, user_id=user_id)["ticket_id"]


def _confirm(client, tid: str, decision: str = "allow"):
    return client.post("/v1/approval/confirm",
                       json={"ticket_id": tid, "decision": decision})


def test_an_allow_that_lands_on_a_breakpoint_is_the_third_state_not_a_failure(
        client, t30_tools, isolated_approvals):
    """⭐ 本票的主判据：挂起 → `status="suspended"`，不是 executed_with_error。

    删掉 confirm 端点里 `if outcome.suspended` 那一支（退回只看 outcome.ok），
    本条红在三处：status、suspended、question。
    """
    tid = _ticket("t30_suspender")
    res = _confirm(client, tid)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "suspended", (
        f"挂起被报成了执行结果：{body}——第三态必须有自己的 status")
    assert body["ok"] is False, "没有产出结果仍然是 False（ok 的含义一个字没改）"
    assert body["suspended"] is True
    assert body["question"] == "请输入短信验证码", body
    assert body["result"].startswith("⏸ "), body["result"]
    assert "不是执行失败" in body["message"], body["message"]
    assert t30_tools == [("suspender", "x")], f"工具该被真跑一次：{t30_tools}"

    # 票据侧：ok=False 但 suspended=True，且已经 executed（确实跑到了断点）。
    stored = approval.get_ticket(tid)
    assert stored.executed is True and stored.ok is False and stored.suspended is True

    # 落盘要带着这一格：重启后轮询必须还是同一句话，不许退回"执行过"。
    assert json.loads(isolated_approvals.read_text(encoding="utf-8"))["tickets"]
    approval.restore(path=str(isolated_approvals))
    polled = client.get(f"/v1/approval/{tid}")
    assert polled.status_code == 200, polled.text
    got = polled.json()
    assert got["status"] == "suspended", got
    assert got["suspended"] is True and got["ok"] is False, got


def test_a_normal_run_keeps_the_old_status_and_answers_suspended_false(
        client, t30_tools):
    """反方向：老形状一格不动（status/ok/result/message），只多一格 `suspended: False`。"""
    tid = _ticket("t30_ok")
    body = _confirm(client, tid).json()
    assert body["status"] == "executed", body
    assert body["ok"] is True and body["suspended"] is False, body
    assert "STDOUT: 2" in body["result"]
    assert body["message"] == "已执行"
    assert "question" not in body, "没有问句的工具不该多出这一格"

    tid2 = _ticket("t30_fail")
    body2 = _confirm(client, tid2).json()
    assert body2["status"] == "executed_with_error", body2
    assert body2["ok"] is False and body2["suspended"] is False, body2

    polled = client.get(f"/v1/approval/{tid2}").json()
    assert polled["status"] == "executed" and polled["suspended"] is False, polled


def test_reject_still_consumes_the_ticket_without_executing_or_a_third_state(
        client, t30_tools):
    """拒绝这一格不被本票改动：不执行、`status="rejected"`、失败两格都不出现。"""
    tid = _ticket("t30_suspender")
    body = _confirm(client, tid, "reject").json()
    assert body["status"] == "rejected", body
    assert "suspended" not in body, "拒绝不是第三态，别把第三态的键乱发"
    assert t30_tools == [], f"拒绝了还跑工具：{t30_tools}"
    assert approval.get_ticket(tid).executed is False
