# v0.39.x 模型驾驶舱 Task 4：网页上下文环 + 账本的接线锁。
#
# 分工沿用 Task 2/3 的口径：预算/校准/分段/整理纯函数由 test_v0391_web_context_meter.py
# 真跑钉死；这里锁的是 UI 那一侧——环与账本的 DOM 存在性、渲染函数走同一口径、
# 动态内容一律 textContent（模型名/消息预览都可能带服务端文本，innerHTML 等于开门）。
# "轻量整理必须真的影响发送"（outbound 与 clientContextK 同一 offset）与
# "done.usage 落账到发送时那个会话"是本任务的两条真功能，各配一条 node 真跑/形状锁。
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.test_web_pwa import (STATIC, _css, _fn_text, _function_body, _html, _js,
                                _strip_html_comments, _strip_js_comments)

REPO = STATIC.parents[3]


# 把文本拼进 HTML 的出口不止 innerHTML 一个。本仓同类锁早就按一组 sink 判
# （test_v0363_admin_console.py:86 test_admin_js_has_no_html_sinks），本文件此前只查
# `"innerHTML" not in X` 这一条——把渲染改成 insertAdjacentHTML("afterbegin", item.name)
# 之后 17+171 全绿、node --check rc=0，模型名走 HTML 拼接这条 XSS 面在测试层完全隐形
# （Task 9 修复轮 I-3）。判据一律走这里，别再用单个词。
_HTML_SINKS = ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function")


def _no_html_sinks(body: str, where: str) -> None:
    """这一段的渲染只许 createElement + textContent；出现任何 HTML sink 即红。"""
    hit = [needle for needle in _HTML_SINKS if needle in body]
    assert not hit, f"{where} 把文本拼进了 HTML：{hit}（模型名/档位标签/服务端报错都可能是别人写的字）"


def test_no_html_sinks_helper_proves_its_own_teeth():
    """这条判据的自证：换一种"不是 innerHTML"的写法，它必须还能报红。

    没有这一段，I-3 的修复就只是"把 innerHTML 这个词换成五个词"——下一个人写成
    `el.insertAdjacentHTML(...)`（旧判据看不见）照样绿。这里同时验一个反例（干净
    文本渲染不许误报），否则"什么都算 sink"也是一种没牙的判据。
    """
    clean = 'function f(){ const n = document.createElement("span"); n.textContent = name; }'
    _no_html_sinks(clean, "自检：干净的纯文本渲染")
    for mutant in ('list.insertAdjacentHTML("afterbegin", item.name)',
                   'box.outerHTML = "<p>" + name + "</p>"',
                   'box.innerHTML = "<p>" + name + "</p>"',
                   'box.appendChild(eval("document"))',
                   "box.appendChild(new Function('return name')())"):
        probe = clean + "\n" + mutant
        try:
            _no_html_sinks(probe, "自检：变异样本")
        except AssertionError:
            continue
        raise AssertionError(f"判据没牙：{mutant} 这种写法它还说绿")


def test_context_ring_and_panel_are_wired_without_innerhtml_for_user_text():
    html = _html()
    js = _js()
    assert 'id="ctxRingBtn"' in html, "输入卡没有上下文环按钮"
    assert 'id="contextPanel"' in html, "没有上下文账本容器"
    assert 'id="ctxBudgetRange"' in html, "账本没有预算滑杆"
    assert 'id="ctxTidyBtn"' in html, "账本没有轻量整理按钮"
    ring = _function_body(js, "renderContextRing")
    assert "contextBudgetK(" in ring and "calibrateUsedK(" in ring, \
        "环没有走本会话预算/校准口径"
    panel = _function_body(js, "renderContextPanel")
    assert "contextSegments(" in panel, "账本没有分段"
    _no_html_sinks(panel, "上下文账本")
    assert "textContent" in panel, "账本没有用 textContent 落文本"
    assert "aria-label" in panel or "setAttribute(\"aria-label\"" in panel, \
        "账本没有可读的无障碍标签"
    tidy = _function_body(js, "applyContextTidy")
    assert "dropOldestToBudget(" in tidy and "contextDropBySession" in tidy, \
        "轻量整理没有按本轮发送丢最早消息"


def test_ring_sits_beside_model_chip_and_the_budget_control_has_one_source():
    """环放在现有 #modelChip 旁边（不硬造第二套模型控件）；预算控件到 Task 9 真的从
    设置页搬走了——现在只剩两处（上下文面板的滑杆与模型卡），两处读同一份 per-model
    map（contextBudgetK/setContextBudget），没有第三份真相。"""
    html = _html()
    js = _js()
    foot = html[html.index('class="input-foot"'):html.index("</form>")]
    assert 'id="ctxRingBtn"' in foot and 'id="modelChip"' in foot, "环或芯片不在输入脚"
    assert foot.index('id="ctxRingBtn"') < foot.index('id="modelChip"'), \
        "环应排在芯片前（点环开账本，点芯片仍开模型菜单）"
    assert 'id="ctxRange"' not in html, \
        "设置页还留着第二份预算滑杆：Task 9 迁走了它，留回来就是两个真相"
    assert 'id="contextPanel"' in html and 'role="dialog"' in html
    assert 'id="ctxBudgetRange"' in html, "预算控件迁移后仍要在上下文面板里"
    detail = _function_body(js, "renderModelDetail")
    panel = _function_body(js, "renderContextPanel")
    sync = _function_body(js, "syncCtxRow")
    for name, body in (("账本", panel), ("模型卡", detail), ("读数行", sync)):
        assert "contextBudgetK(" in body, f"{name}没读那份按模型存的预算 map"
    assert "setContextBudget(" in detail, "模型卡没接本会话预算"
    assert "localStorage.setItem" not in detail and "localStorage.getItem" not in detail, \
        "模型卡自己写了一份预算：与账本滑杆就此各存真相"
    assert "localStorage.setItem" not in panel and "localStorage.getItem" not in panel, \
        "账本自己写了一份预算：与模型卡就此各存真相"


def test_panel_layer_and_ring_style_use_the_established_patterns():
    """开合走 Layers（返回键与历史不漂）；环由 --ctx-pct/--ctx-color 的
    conic-gradient 直接驱动；环刷新绝不能再调 renderMessages（递归）。"""
    js = _js()
    css = _css()
    assert 'Layers.open("contextPanel"' in _function_body(js, "openContextPanel"), \
        "账本没走让位层栈（返回键会对不上界面）"
    assert 'Layers.close("contextPanel"' in _function_body(js, "closeContextPanel")
    ring = _function_body(js, "renderContextRing")
    assert 'setProperty("--ctx-pct"' in ring and 'setProperty("--ctx-color"' in ring
    assert "renderMessages" not in ring, "环刷新里再调 renderMessages 就是递归"
    assert "conic-gradient" in css and "--ctx-pct" in css and "--ctx-color" in css, \
        "环样式没由自定义属性直接驱动"


def test_ring_refresh_hooks_cover_every_changing_path():
    """环与账本的刷新钩子：消息/芯片/预算/整理任何一侧变了环都要跟上；
    队列函数经 queueUiRefresh 守卫调用刷新——Task 3 的纯状态机测试没有 DOM，
    守卫（typeof 判据住在 queueUiRefresh 里）不许被拆掉，拆了那批测试就炸。"""
    js = _js()
    for name in ("renderMessages", "renderModelChip", "syncCtxRow", "applyContextTidy"):
        assert "renderContextRing()" in _function_body(js, name), \
            f"{name} 变了没刷新上下文环"
    assert "renderContextRing()" in _function_body(js, "runStream"), \
        "done 落账后环没有立刻重读校准锚点"
    guard = _function_body(js, "queueUiRefresh")
    assert "typeof renderContextRing" in guard and "renderContextRing()" in guard, \
        "队列刷新没有 DOM 守卫（Task 3 无 DOM 真跑会被拖崩）"
    for name in ("enqueueQueueItem", "nextQueueItem", "finishQueueItem", "pauseQueue"):
        assert "queueUiRefresh()" in _function_body(js, name), \
            f"{name} 之后队列量变了，环却不会刷新"


def test_panel_controls_are_wired_and_state_maps_are_reset_with_identity():
    js = _js()
    for needle in ('$("ctxRingBtn").onclick', '$("ctxTidyBtn").onclick',
                   '$("ctxResetBtn").onclick', '$("ctxBudgetRange").oninput'):
        assert needle in js, f"账本控件没接线：{needle}"
    reset = _function_body(js, "resetViewForIdentity")
    assert "lastUsageBySession" in reset and "contextDropBySession" in reset, \
        "换身份没清掉两张环/账本表：上一个人的用量会画到新会话头上"
    state_block = js[js.index("const state = {"):js.index("};", js.index("const state = {"))]
    assert "lastUsageBySession: {}" in state_block and "contextDropBySession: {}" in state_block, \
        "两张表没在 state 字面量里初始化"


# 行为锁：整理必须真的影响发送（node 真跑，不是文本断言）。
# "只改显示不改实际发送 = 假功能"是这一整段要防的形状：outbound 与 clientContextK
# 必须读同一个 contextDropBySession offset，丢 N 条之后实发条数与环读数一起降。
_DROP_SEND_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {};
vm.createContext(ctx);
const mem = new Map();
ctx.localStorage = {
  getItem: (k) => mem.has(k) ? mem.get(k) : null,
  setItem: (k, v) => mem.set(k, String(v)),
  removeItem: (k) => mem.delete(k),
};
ctx.state = { providers: [{ id: "m1", max_context_k: 64 }], messages: [
  { role: "user", content: "一".repeat(100) },
  { role: "assistant", content: "二".repeat(100) },
  { role: "user", content: "三".repeat(100) },
  { role: "assistant", content: "四".repeat(100) },
], contextDropBySession: {}, lastUsageBySession: {} };
ctx.pref = { userId: "u1", provider: "m1", contextTokensK: 8,
  sessionId: "s1", persona: () => "" };
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const before = ctx.outbound().length;
const ck0 = ctx.clientContextK();
// 走 applyContextTidy 的同一条算式：按预算吐掉最早的，条数记进表里。
// 每条 cost=104：预算 250 留最后 2 条（208≤250，再加一条 312>250 即停），丢最早 2 条。
const plan = ctx.dropOldestToBudget(ck0.msgs, 250);
ctx.state.contextDropBySession.s1 = (ctx.state.contextDropBySession.s1 || 0) + plan.droppedCount;
const out = ctx.outbound();
const ck1 = ctx.clientContextK();
process.stdout.write(JSON.stringify({
  before, beforeKnown: ck0.known,
  droppedCount: plan.droppedCount,
  afterOut: out.length, afterFirst: out[0] && out[0].content.slice(0, 1),
  afterKnown: ck1.known,
}));
"""


def test_tidy_offset_reaches_the_real_outbound_not_just_the_display():
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    src = "\n".join(_fn_text(_js(), n) for n in (
        "estimateTokens", "truncateWithin", "contextBudgetStorageKey",
        "readContextBudgets", "contextBudgetK", "dropOldestToBudget",
        "clientContextK", "outbound",
        "serverDefaultProvider", "currentProvider", "budgetProviderId"))
    d = Path(tempfile.mkdtemp(prefix="model-cockpit-ring-"))
    (d / "fns.js").write_text(src, encoding="utf-8")
    (d / "harness.cjs").write_text(_DROP_SEND_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(d / "harness.cjs"), str(d / "fns.js")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"harness 自己跑失败：\n{r.stdout}\n{r.stderr}"
    out = json.loads(r.stdout)
    assert out["before"] == 4 and out["droppedCount"] == 2, \
        f"预算 250 token 下应丢最早的 2 条：{out}"
    # 关键差分：显示降了、实发也 must 降——slice(drop) 之后 outbound 只剩最后 2 条。
    assert out["afterOut"] == 2 and out["afterFirst"] == "三", \
        f"整理没作用到真实发送：{out}"
    assert out["afterKnown"] == out["beforeKnown"] - \
        (out["before"] - out["afterOut"]) * 104, \
        f"环读数与发送口径的 offset 不同源：{out}"


def test_done_usage_is_booked_to_the_session_that_sent():
    """done.usage 落账：发送时捕获 sid 与 clientK，回来只写给"出发时那个会话"，
    会话已切换就不落（防把 A 轮的用量记到 B 会话头上）。形状锁——真跑要起整条流。"""
    js = _js()
    rs = _function_body(js, "runStream")
    assert "sidAtSend" in rs and "clientKAtSend" in rs, "发送时没捕获会话/估算锚点"
    assert "done.usage" in rs and "prompt_tokens" in rs, "done 的 usage 没落账"
    assert re.search(r"pref\.sessionId === sidAtSend", rs), \
        "落账没有核对会话仍是出发时那一个"
    assert "providerId" in rs, "账本没记当时用的模型"


def test_budget_provider_id_is_the_single_source_for_ring_outbound_and_send():
    """Fix Round 1：环/账本/整理/滑杆原来读 currentProvider().id，而 outbound 截断与
    runStream 实发 payload 读裸 pref.provider——pref.provider 不在清单、回落到服务端
    默认时，环按默认模型的预算显示、实发按 pref 口径，显示≠实发。
    现在两侧都必须经 budgetProviderId()（currentProvider 优先、pref 兜底），
    旧的两处裸读法钉死不许回来。"""
    js = _js()
    assert "function budgetProviderId(" in js, "没有统一的预算/发送 provider helper"
    assert "budgetProviderId(" in _function_body(js, "outbound"), \
        "outbound 截断预算还读裸 pref.provider"
    assert "const providerId = budgetProviderId()" in _function_body(js, "runStream"), \
        "runStream 实发 payload 的 provider 与显示口径不同源"
    for name in ("syncCtxRow", "renderContextRing", "renderContextPanel", "applyContextTidy"):
        assert "budgetProviderId(" in _function_body(js, name), \
            f"{name} 的预算没走统一口径"
    assert "setContextBudget(budgetProviderId(" in js, "预算滑杆的写键没走统一口径"
    assert "contextBudgetK(pref.provider)" not in js, \
        "旧读法 contextBudgetK(pref.provider) 还在：显示与实发又能分叉"
    assert "const providerId = pref.provider" not in js, \
        "旧发送口径 const providerId = pref.provider 还在：payload 与环不同源"
    ring = _function_body(js, "renderContextRing")
    assert 'setProperty("--ctx-pct", "0")' in ring and "无可用模型" in ring, \
        "清单为空时环必须归零并如实说'无可用模型'，不许 retain 上一次的填充"


# ---------------- Task 5：接力队列 UI 与自动发送 ----------------
# 队列纯状态机由 test_v0391_web_queue_model.py 真跑钉死（Task 3）；这里锁 UI 接线：
# 忙时入队、正常完成自动接力、停止/后台/换身份暂停、失败回队不丢条目、带图条目
# 在没有视觉闸的模型前不发、排队条对用户原文只走 textContent。


def test_relay_queue_ui_and_auto_send_guards():
    html = _html()
    js = _js()
    assert 'id="queueStrip"' in html and 'id="queueList"' in html
    assert 'id="queuePauseBtn"' in html
    composer = html[html.index('class="composer"'):html.index('id="chatForm"')]
    assert 'id="queueStrip"' in composer, "排队条没放输入卡上方"
    send = _function_body(js, "send")
    assert "enqueueQueueItem(" in send and "已加入接力队列" in send, \
        "忙时发送没有入队"
    assert "return false" in send and "return true" in send, \
        "send 没有把'接住/没接住'回传给调用方（自动接力失败会丢条目）"
    relay = _function_body(js, "maybeRelay")
    assert "canRelay(" in relay, "自动接力没有会话/前台/暂停闸"
    assert "setTimeout(" in relay, "自动接力没有给落定一拍留时间"
    assert "finishQueueItem(" in relay, "发送中条目没有释放"
    assert "started === false" in relay, "自动接力发送失败没有把条目放回队列"
    assert "relayImageBlocked(" in relay, "自动接力缺视觉闸：带图条目会打到非视觉模型"
    assert "resetQueuesForIdentity()" in _function_body(js, "resetViewForIdentity"), \
        "换身份没清队列"
    assert "forceNext" in _function_body(js, "resetViewForIdentity"), \
        "换身份没清待发的立即插话条目"
    assert "pauseQueue(" in _function_body(js, "stop"), "停止没有暂停队列"
    assert re.search(r'addEventListener\("visibilitychange"[\s\S]{0,220}pauseQueue\(', js), \
        "切后台没有暂停当前会话队列"
    card = _function_body(js, "queueCardNode")
    assert "textContent" in card, "队列卡没有用 textContent 落用户原文"
    _no_html_sinks(card, "接力排队条")
    assert "queueShowNow(" in card, "队列卡没挂立即插话"
    strip = _function_body(js, "renderQueueStrip")
    assert "renderMessages" not in strip, "排队条刷新里递归重画消息"
    into = _function_body(js, "streamInto")
    assert "sidAtSend" in into and "userAtSend" in into, "没捕获出发时的会话/身份锚点"
    assert "maybeRelay(" in into and "forceNext" in into, "流结束没接接力/立即插话出口"
    assert "relayImageBlocked(" in into, "立即插话缺视觉闸"
    rs = _function_body(js, "runStream")
    assert "return { normal:" in rs, "runStream 没有返回 {normal} 结局"
    assert re.search(r"\bresumed\b", rs), "断线取回/cannot_resume 没算非正常结束"
    assert "function clearQueue(" in js, "接口要求的 clearQueue() 不存在"
    # 插话发送失败同样回队暂停（streamInto 的 forceNext 分支）。
    assert "started === false" in into, "forceNext 发送失败没有回队"


# ---------------- Task 5 Fix Round 1：插话视觉闸 + forceNext 悬空回队 ----------------
# 两条 Important：①queueShowNow 的"无在途流直接发"分支不过视觉闸，带图条目在无流时
# 点「立即插话」会直发非视觉模型；②在 A 点插话后切到 B，forceNext={sid:A} 谁都不消费，
# 条目从任何排队条上消失。修复口径：失配时按 forceNext.sid 回队原队列并暂停；
# 三条 promise 链（queueShowNow 直发 / maybeRelay / forceNext 发送）异常一律回队，
# 不留悬挂 sendingId；#queueCount 补 aria-live。


def test_shownow_vision_gate_and_force_next_requeue_guards():
    js = _js()
    html = _html()
    # ①直接发分支的视觉闸：必须排在 send( 之前，闸放在 send 后等于没闸。
    show = _function_body(js, "queueShowNow")
    assert "relayImageBlocked(" in show, \
        "queueShowNow 缺视觉闸：无在途流时带图条目会直发非视觉模型"
    assert show.index("relayImageBlocked(") < show.index("send("), \
        "queueShowNow 的视觉闸判在 send 之后：拦不住直发"
    assert "putBackQueueItem(" in show, "插话被视觉闸拦下时条目没有放回队列并暂停"
    assert ".catch(" in show, "queueShowNow 直发链没有 catch：异常会静默吞掉条目"
    # ②forceNext 失配回队：streamInto 里必须引用 forceNext.sid 作回队桶键、经 queueFor 落桶。
    into = _function_body(js, "streamInto")
    assert "requeueForceNext(" in into, "streamInto 没有过期 forceNext 的回队闸"
    assert "forceNext.sid" in into and "queueFor(" in into, \
        "forceNext 失配分支没有按 forceNext.sid 经 queueFor( 回队"
    assert ".catch(" in into, "forceNext 发送链没有 catch：异常留下悬空条目"
    relay = _function_body(js, "maybeRelay")
    assert ".catch(" in relay, "maybeRelay 发送链没有 catch：异常悬挂 sendingId"
    # ③待发计数对读屏可播报。
    assert re.search(r'id="queueCount"[^>]*aria-live="polite"', html), \
        "#queueCount 没有 aria-live=\"polite\"：待发条数变化不会播报"
    assert "function requeueForceNext(" in js, "回队判据纯函数 requeueForceNext 不存在"


# 行为锁（Task 3 的 Node vm 口径）：requeueForceNext 是注入 putBack 的纯判据——
# 无 forceNext/锚点仍匹配 → 不动作；会话或身份任一切走 → 按 forceNext.sid 回队一次。
_REQUEUE_FORCE_NEXT_HARNESS = r"""
const fs=require("fs"), vm=require("vm");
const ctx={}; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2],"utf8"), ctx);
const calls=[];
const putBack=(sid,item)=>calls.push(sid+":"+item.id);
const out={
  nullCase: ctx.requeueForceNext(null,"s1","u1",putBack),
  match: ctx.requeueForceNext({sid:"s1",user:"u1",item:{id:"a"}},"s1","u1",putBack),
  userSwitched: ctx.requeueForceNext({sid:"s1",user:"u1",item:{id:"b"}},"s1","u2",putBack),
  sessionSwitched: ctx.requeueForceNext({sid:"s1",user:"u1",item:{id:"c"}},"s2","u1",putBack),
  calls,
};
process.stdout.write(JSON.stringify(out));
"""


def test_requeue_force_next_pure_function_requeues_only_on_mismatch():
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    src = _fn_text(_js(), "requeueForceNext")
    d = Path(tempfile.mkdtemp(prefix="model-cockpit-requeue-"))
    (d / "fns.js").write_text(src, encoding="utf-8")
    (d / "harness.cjs").write_text(_REQUEUE_FORCE_NEXT_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(d / "harness.cjs"), str(d / "fns.js")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"harness 自己跑失败：\n{r.stdout}\n{r.stderr}"
    out = json.loads(r.stdout)
    assert out["nullCase"] is False and out["match"] is False, \
        f"没有 forceNext / 锚点仍成立时不许回队：{out}"
    assert out["userSwitched"] is True and out["sessionSwitched"] is True, \
        f"身份或会话任一切走都必须回队：{out}"
    assert out["calls"] == ["s1:b", "s1:c"], \
        f"回队桶键必须是 forceNext.sid（原队列），不是当前会话：{out}"


# ---------------- Task 9：模型卡 / 模型管理 / 设置页迁移 ----------------
# 判据源：task-9-brief.md Step 1 逐字，外加本轮拍定的口径（两栏 DOM、三态文案不说谎、
# 预算两处同源、能力写口的 scope 分流、PUT 的唯一记录来源、动态文本不拼 innerHTML）。
# "点了开关到底发不发得出去""偏好是不是随身份分键"这类读源码读不出结论的，
# 交给下面那段 node 真跑。


def test_web_model_card_management_and_settings_move():
    html = _html()
    js = _js()
    assert 'id="modelDetail"' in html and 'id="modelManageBtn"' in html
    assert 'id="ctxRange"' not in html, "设置页还在重复上下文滑杆：两个真相"
    assert 'id="provCtxK"' not in html, "模型服务表单还在重复模型上限"
    render = _function_body(js, "renderModelChip")
    assert "thinking_status" in render and "can_edit" in render, \
        "模型行没有能力/权限状态"
    detail = _function_body(js, "renderModelDetail")
    assert "contextBudgetK(" in detail and "setContextBudget(" in detail, \
        "模型卡没接本会话预算"
    _no_html_sinks(detail, "模型卡")
    assert "data-thinking" in js and "thinking_levels" in js, "思考三态/档位没有接线"
    assert "updateProviderCapabilities" in _js(), "能力写口没有 API 封装"
    # 本单补锁：行是模型名（服务端文本）的第一个展示面，同样不许拼 HTML
    _no_html_sinks(render, "模型行")


def test_model_menu_is_a_two_pane_container_with_one_manage_entry():
    html = _html()
    js = _js()
    at = html.index('id="modelMenu"')
    menu = html[at:html.index('id="contextPanel"', at)]
    for el in ('id="modelList"', 'id="modelDetail"', 'id="modelManageBtn"'):
        assert el in menu, f"模型菜单的两栏容器缺 {el}：清单/详情/管理必须同在这一层"
    assert '$("modelManageBtn")' in js, "「模型管理」按钮没接线"
    manage = _function_body(js, "openModelManage")
    assert "state.modelManage" in manage and "showModelDetail(" in manage, \
        "模型管理没有落点：点一下只换个 class 等于没这个功能"
    # 行上的管理入口只在 can_edit 时给（服务端说不许改，就别留一个必然 403 的键）
    assert "openModelManage(" in _function_body(js, "renderModelChip")
    assert "openModelManage(" in _function_body(js, "renderModelDetail")


def test_capability_write_path_is_scoped_and_reads_one_record_source():
    api = _js("api.js")
    js = _js()
    m = re.search(r"updateProviderCapabilities:\s*\(([^)]*)\)\s*=>", api)
    assert m, "api.js 里 updateProviderCapabilities 不是箭头函数（形状变了，锁要跟着改）"
    assert [p.strip() for p in m.group(1).split(",")] == ["scope", "id", "patch"], \
        "能力写口的形参必须是 (scope, id, patch)"
    assert 'scope === "admin"' in api, "scope 没分流：管理员改共享条目会打到个人面上"
    assert "/v1/providers/" in api and "/v1/me/providers/" in api, "两面 PUT 少了一面"

    save = _function_body(js, "saveModelCapabilities")
    assert "state.providerRecords[" in save and "API.updateProviderCapabilities(" in save
    assert "state.providers" not in save, \
        "PUT 的记录来自 /v1/models 目录项：那里没有 base_url，保存会把上游洗掉"
    assert "await loadModels()" in save or "loadModels()" in save, \
        "保存后没重读清单：界面继续拿一份旧真值"
    assert "loadProviders()" in save, "保存后没重读记录：卡片下一次编辑会拿旧的 PUT 上去"
    assert "alert(" not in save, "服务端那句人话该写在卡上，不是弹窗"
    cache = _function_body(js, "loadProviders")
    assert "state.providerRecords = {}" in cache and "state.providerRecords[p.id] = p" in cache, \
        "完整记录没在 loadProviders 里整表重建：PUT 就没有唯一来源（或留着上一条旧记录）"
    assert "data.shared" in cache and "data.mine" in cache, \
        "记录缓存只建了一面：管理员改共享条目时拿不到那份完整记录"
    assert "state.providerRecords = {}" in _function_body(js, "resetViewForIdentity"), \
        "换身份没清记录缓存：上一个人的 base_url 会跟着下一次 PUT 发出去"

    body = _function_body(js, "providerCapabilityBody")
    for keep in ("api_key", "is_default", "pricing", "thinking_profile",
                 "base_url", "model", "supports_vision"):
        assert keep in body, f"全量 PUT 少了 {keep}：一次保存会把这一格洗掉"
    assert 'api_key: ""' in body, "api_key 不传空串就带不回原密钥（服务端按占位符保留）"
    # Minor 4（修复轮）：这一格以前写在 body 里、配一句"少了 paid_by 就会被洗掉"的红话，
    # 那是假承诺——`main.ProviderRequest` 没有 paid_by，Pydantic 直接把客户端发的丢掉，
    # 删了 body 里那行也不会红。真正保住计费归属的是服务端那份保留，它另有自己的锁
    # （test_review_fixes_20260923.py::test_upsert_keeps_paid_by_when_update_omits_it）。
    # 所以这里判的是**反向**：body 不许出现 paid_by（别留下一个改了也没用的输入口）。
    assert "paid_by" not in body, \
        "body 还发 paid_by：请求模型没这一格，发出去会被 Pydantic 丢掉，留着只让下一个人以为界面改得动"
    # 每一格都必须**从那份记录里取**：写死一个值等于把"这次没改"演成"改成这个值"
    assert "max_context_k: rec.max_context_k" in body, \
        "上限不从完整记录里取：改一次思考档位就把库里的上限写死"
    assert "base_url: rec.base_url" in body and "label: rec.label" in body, \
        "连接字段不从完整记录里取：全量 PUT 会把它洗掉"
    assert "id:" not in body, "PUT body 不带 id：路由按 path 钉这一条"


def test_thinking_switch_is_only_clickable_when_the_profile_is_verified():
    js = _js()
    ctl = _function_body(js, "thinkingControlState")
    assert "thinking_status" in ctl and '"verified"' in ctl, "三态判据没落在一个函数里"
    detail = _function_body(js, "renderModelDetail")
    assert "thinkingControlState(" in detail and ".disabled" in detail, \
        "思考开关没按三态禁用：会做出一个点了写偏好却发不出去的状态"
    assert "thinkingChoice(" in detail and "writeThinkingPref(" in detail, \
        "开关/档位没接到本机偏好"
    label = _function_body(js, "thinkingStatusLabel")
    for text in ("不支持思考", "支持思考（参数未核实，暂时不能开）", "可切换思考强度"):
        assert text in label, f"三态文案少了「{text}」"
    # 能力编辑区（服务端字段）与偏好开关（本机）是两件事，只有 can_edit 才给写口，
    # 而写口全页只有一个：卡片与管理区不许各发一份 PUT（分流散成两处是漂移的起点）。
    assert "can_edit" in detail and "modelManageSection(" in detail
    section = _function_body(js, "modelManageSection")
    assert "saveModelCapabilities(" in section and "max_context_k" in section
    _no_html_sinks(section, "能力编辑区")
    assert js.count("API.updateProviderCapabilities(") == 1, \
        "能力写口开了第二处：scope 分流就不止一个出处了"


_THINKING_PREF_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {};
vm.createContext(ctx);
const mem = new Map();
ctx.localStorage = {
  getItem: (k) => mem.has(k) ? mem.get(k) : null,
  setItem: (k, v) => mem.set(k, String(v)),
};
ctx.pref = { userId: "u1" };
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const verified = { thinking_status: "verified", thinking_levels: ["快速", "均衡"],
                   thinking_default: "均衡" };
const out = {
  keyU1: ctx.thinkingPrefsKey(),
  fresh: ctx.thinkingChoice("p1", verified),
  otherProvider: ctx.thinkingChoice("p2", verified),
};
ctx.writeThinkingPref("p1", { enabled: true, level: "快速" });
out.afterWrite = ctx.thinkingChoice("p1", verified);
ctx.pref.userId = "u2";
out.newIdentity = ctx.thinkingChoice("p1", verified);
out.keyU2 = ctx.thinkingPrefsKey();
ctx.pref.userId = "u1";
out.defaultNotInLevels = ctx.thinkingChoice("p8",
  { thinking_status: "verified", thinking_levels: ["甲", "乙"], thinking_default: "丙" });
out.noLevels = ctx.thinkingChoice("p9",
  { thinking_status: "unsupported", thinking_levels: [], thinking_default: "" });
for (const st of ["unsupported", "unverified", "verified"]) {
  out[st] = ctx.thinkingControlState(
    { thinking_status: st, thinking_levels: ["甲"], thinking_default: "甲" });
}
process.stdout.write(JSON.stringify(out));
"""


def test_thinking_prefs_are_local_per_identity_and_honest_about_state():
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    src = "\n".join(_fn_text(_js(), n) for n in (
        "thinkingPrefsKey", "readThinkingPrefs", "writeThinkingPref",
        "thinkingChoice", "thinkingStatusLabel", "thinkingStateReason",
        "thinkingControlState"))
    d = Path(tempfile.mkdtemp(prefix="model-cockpit-thinking-"))
    (d / "fns.js").write_text(src, encoding="utf-8")
    (d / "harness.cjs").write_text(_THINKING_PREF_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(d / "harness.cjs"), str(d / "fns.js")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"harness 自己跑失败：\n{r.stdout}\n{r.stderr}"
    out = json.loads(r.stdout)
    assert out["keyU1"] == "fv.thinking.u1.v1", out["keyU1"]
    assert out["keyU2"] == "fv.thinking.u2.v1", out["keyU2"]
    # enabled 缺省 false：不给全站悄悄改计费行为
    assert out["fresh"]["enabled"] is False and out["fresh"]["level"] == "均衡", out["fresh"]
    assert out["otherProvider"]["enabled"] is False, "偏好按 provider 分格：不能串到别的模型上"
    assert out["afterWrite"]["enabled"] is True and out["afterWrite"]["level"] == "快速", \
        out["afterWrite"]
    assert out["newIdentity"]["enabled"] is False, \
        "换身份读到了上一个人的开关：偏好必须按 userId 分键"
    # 默认档不在候选里 → 落 levels[0]（与服务端 _thinking_view 同一口径，界面不另造状态）
    assert out["defaultNotInLevels"]["level"] == "甲", out["defaultNotInLevels"]
    assert out["noLevels"]["level"] == "", out["noLevels"]
    # 三态：只有 verified 可点，其余两态禁用且给出原因
    assert out["unsupported"]["enabled"] is False and out["unsupported"]["reason"], \
        out["unsupported"]
    assert out["unverified"]["enabled"] is False and "未核实" in out["unverified"]["reason"], \
        out["unverified"]
    assert out["verified"]["enabled"] is True and out["verified"]["reason"] == "", \
        out["verified"]


def test_settings_form_keeps_connection_fields_and_gives_up_capability_inputs():
    html = _html()
    js = _js()
    prov = html[html.index('data-page="providers"'):html.index('data-page="accounts"')]
    for keep in ("provLabel", "provBase", "provKey", "provModel", "provVision",
                 "provTestBtn"):
        assert f'id="{keep}"' in prov, f"连接配置字段被误删：{keep}"
    assert 'id="provCtxK"' not in prov, "表单还在收模型上限：与模型卡两个真相"
    assert "模型管理" in prov and "上限" in prov, \
        "表单没留一句「上限与思考能力到模型卡里配」的指路，改完的人只会找不到入口"
    draft = _function_body(js, "providerDraftFromForm")
    assert "provCtxK" not in draft, "表单还读那个已删掉的输入框：null.value 空指针"
    # 修复轮 I-2 重钉：原判据是"草稿里根本不许出现 max_context_k"。放开的只有**新建**
    # 那一次（预置带着写好的 1000K 来，不发就落进服务端缺省 64）；编辑路径仍一字不许发，
    # 因为那一格的真相在模型卡。两侧都由下面的 node 真跑判行为，不靠读源码。
    assert re.search(r"if \(!state\.editingProvider && state\.presetMaxContextK !== null\)\s*\{\s*"
                     r"draft\.max_context_k = state\.presetMaxContextK;", draft), \
        "上限从表单发出去的条件不是「新建 + 点过预置」：编辑路径会盖掉模型卡刚写的那一格"
    assert draft.count("max_context_k") == 1, "上限在这份草稿里出现两次：出处不止一个"
    for name in ("openProviderForm", "openBlankProviderForm", "fillFormFromPreset"):
        assert "provCtxK" not in _function_body(js, name), \
            f"{name} 还读 provCtxK：字段删掉后就是 null.value 空指针"
    assert "provCtxK" not in js, "app.js 里还留着 provCtxK 的引用"


# ---------------- Task 9 修复轮 1：审查员判 Changes requested 的四条 ----------------
# I-1（换身份守卫补牙）落在 test_v0350_task_ui_web_contract.py，那里是唯一的 node 真跑现场；
# 这里补齐 I-2（预置上限）、I-3（HTML sink 判据，见文件头那组）、I-4（ARIA 归属），
# 以及一并收掉的 Minor 2/3/4/5/6/7。判据一律优先真跑：这一批改动的形状是
# "读源码读着都对、跑起来是错的"（审查员实测的三条都是这个形状）。


def _run_js(names, harness, prefix, extra=""):
    """把 app.js 里点名的那几个函数摘进 vm 真跑一遍，返回 JSON 结果。

    `extra` 给的是**从同一份 app.js 里现抓的常量源码**（见 _app_const）：被摘的函数
    引用了模块级 const 时，测试不许自己再造一份值——那等于把被测数字换成测试的。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    src = "\n".join(_fn_text(_js(), n) for n in names)
    if extra:
        src = extra + "\n" + src
    d = Path(tempfile.mkdtemp(prefix=f"model-cockpit-{prefix}-"))
    (d / "fns.js").write_text(src, encoding="utf-8")
    (d / "harness.cjs").write_text(harness, encoding="utf-8")
    r = subprocess.run([node, str(d / "harness.cjs"), str(d / "fns.js")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"harness 自己跑失败：\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout)


# 假 DOM 的最小一颗：只给表单那几个输入框，够跑就行（拿不到节点=当场抛，不静默跳过）。
_FAKE_FIELDS = r"""
const nodes = {};
const mk = (id) => (nodes[id] || (nodes[id] = {
  id, value: "", checked: false, placeholder: "", textContent: "",
  focus() {}, blur() {}, setAttribute() {}, appendChild() {},
  classList: { _s: new Set(["hidden"]),
    add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
    contains(c) { return this._s.has(c); },
    toggle(c, on) { if (on) { this._s.add(c); } else { this._s.delete(c); } } },
}));
"""

_PRESET_CAP_HARNESS = _FAKE_FIELDS + r"""
const fs = require("fs"), vm = require("vm");
const ctx = {};
vm.createContext(ctx);
ctx.document = { getElementById: mk };
ctx.$ = (id) => mk(id);
ctx.state = { editingProvider: null, editingScope: null, presetMaxContextK: null };
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
// 与后端 providers.PRESETS 同形状的两份：一份带 max_context_k（deepseek=1000），一份不带。
const deepseek = { label: "DeepSeek", base_url: "https://api.deepseek.com/v1",
                   model: "deepseek-flash", supports_vision: true, max_context_k: 1000 };
const plain = { label: "OpenAI", base_url: "https://api.openai.com/v1",
                model: "gpt-4o", supports_vision: true };
const out = {};
ctx.openBlankProviderForm("mine");
ctx.fillFormFromPreset(deepseek);
out.create = ctx.providerDraftFromForm();
out.createNote = mk("provPresetCap").textContent;
out.createNoteHidden = mk("provPresetCap").classList.contains("hidden");

ctx.openBlankProviderForm("mine");
ctx.fillFormFromPreset(plain);
out.createPlain = ctx.providerDraftFromForm();
out.plainNote = mk("provPresetCap").textContent;

// 编辑已有条目：哪怕刚点过预置换 base_url，也不许带上上限（卡片单源）。
ctx.fillFormFromPreset(deepseek);
ctx.openProviderForm({ id: "p1", label: "已配的", base_url: "https://old/v1", model: "m" }, "mine");
out.edit = ctx.providerDraftFromForm();
out.editNote = mk("provPresetCap").textContent;

// 关单再开：上一单点过的预置不许跟着走下一单。
ctx.openBlankProviderForm("mine");
ctx.fillFormFromPreset(deepseek);
ctx.closeProviderForm();
ctx.openBlankProviderForm("mine");
out.afterClose = ctx.providerDraftFromForm();
process.stdout.write(JSON.stringify(out));
"""


def test_preset_cap_travels_only_with_a_new_provider_draft():
    """I-2：选 DeepSeek 预置再保存，上限不能从 1000K 静默掉到 64K。

    链路（审查员实测）：loadProviders 把 data.presets 原样给前端 → fillFormFromPreset
    不回填上限 → providerDraftFromForm 也不发 → POST 落 providers._validate 的缺省 64。
    而 PRESETS["deepseek"]["max_context_k"] = 1000（providers.py，官方 v4 系 1M token）。
    修的是主控人定的口径：**预置上限只在新建时随表单走一次**，编辑路径继续由模型卡单源持有。
    路由层端到端（不带这一格的一次 PUT 读回来仍是 1000）在
    test_v0391_provider_capabilities.py::test_route_put_without_cap_keeps_the_preset_cap。
    """
    out = _run_js(("openProviderForm", "openBlankProviderForm", "fillFormFromPreset",
                   "showPresetCapNote", "closeProviderForm", "providerDraftFromForm"),
                  _PRESET_CAP_HARNESS, "preset-cap")
    assert out["create"].get("max_context_k") == 1000, \
        f"新建 + 预置却没带上限：POST 会落进 _validate 的缺省 64，管理员拿到 64K 闸门：{out['create']}"
    assert out["create"]["label"] == "DeepSeek" and out["create"]["base_url"], out["create"]
    assert "max_context_k" not in out["createPlain"], \
        "没带上限的预置也被发了一份：那是界面在替服务端编缺省"
    assert "max_context_k" not in out["edit"], \
        f"编辑路径发了上限：会把模型卡刚写的那一格盖回预置值：{out['edit']}"
    assert "max_context_k" not in out["afterClose"], \
        "关单后上一单的预置还跟着走：新建第二条会被莫名放大"
    # 表单里那句说明：有预置上限才出现，且说清"只在初始一次、之后归模型卡"。
    assert "1000" in out["createNote"] and "模型管理" in out["createNote"], out["createNote"]
    assert out["createNoteHidden"] is False
    assert out["plainNote"] == "" and out["editNote"] == "", \
        "没带上限（或走编辑）时那句「预置带来的初始上限」还在说亮话"


def test_preset_cap_note_exists_in_the_settings_form():
    """HTML 侧的落点：#provPresetCap 真的在那张表单里（否则 showPresetCapNote 永远空转）。"""
    html = _html()
    prov = html[html.index('data-page="providers"'):html.index('data-page="accounts"')]
    assert 'id="provPresetCap"' in prov, "表单里没有那句话的落点：管理员看不见初始上限从哪来"
    assert re.search(r'<p class="pane-note hidden" id="provPresetCap"></p>', prov), \
        "那句话不该默认就显示出来（没点预置时它是无关的）"


# ---------------- Minor 2：上限调低把预算夹下来必须吭一声 ----------------

_BUDGET_CLAMP_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {};
vm.createContext(ctx);
const mem = new Map();
ctx.localStorage = {
  getItem: (k) => (mem.has(k) ? mem.get(k) : null),
  setItem: (k, v) => mem.set(k, String(v)),
};
ctx.pref = { userId: "u1", contextTokensK: 8 };
ctx.state = { providers: [{ id: "m1", max_context_k: 512 }], budgetClamped: null };
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const out = {};
const key = "fv.contextBudget.u1.v1";
const stored = () => JSON.parse(mem.get(key) || "{}").m1;
ctx.setContextBudget("m1", 512);          // 这台设备记着 512K
out.wantedBefore = ctx.contextBudgetWantedK("m1");
ctx.state.budgetClamped = null;           // 上一句写下的账不算这一次的
// 管理员把上限从 512K 调到 64K：syncCtxRow 走的就是这两行（夹值 + 记账）。
ctx.state.providers = [{ id: "m1", max_context_k: 64 }];
ctx.noteBudgetClamp("m1", ctx.contextBudgetWantedK("m1"), 64);
ctx.setContextBudget("m1", 64);
out.clamped = ctx.budgetClampNoteK("m1");
out.storedAfter = stored();
out.readAfter = ctx.contextBudgetK("m1");
// 印一次就清（渲染侧清的那一行）：清掉之后不该再冒出来。
ctx.state.budgetClamped = null;
out.afterPrint = ctx.budgetClampNoteK("m1");
// 没夹过的情形：不越界不出现；为别的模型记的账也不许说给这一张卡听。
ctx.state.providers = [{ id: "m1", max_context_k: 64 }, { id: "m2", max_context_k: 1000 }];
ctx.noteBudgetClamp("m2", 8, 1000);
out.notClamped = ctx.budgetClampNoteK("m2");
ctx.state.budgetClamped = { id: "m1", from: 512, to: 64 };
out.wrongCard = ctx.budgetClampNoteK("m2");
out.rightCard = ctx.budgetClampNoteK("m1");
process.stdout.write(JSON.stringify(out));
"""


def test_lowering_the_cap_clamps_the_budget_and_says_so_once():
    """Minor 2（spec §3.2）：夹到新上限并提示一次，不静默保留越界值。

    三条各自都要成立：① 越界那份**被写回**上限（原来只在读数时夹，本机 map 里那份
    越界值一直躺着，syncCtxRow 那句 `if (budget > cap)` 因为拿的是夹过的数而恒不成立）；
    ② 卡片说得出"从多少夹到多少"；③ 只在被夹那张卡上出现一次，没夹过就不出现。
    """
    out = _run_js(("contextBudgetStorageKey", "readContextBudgets", "contextBudgetK",
                   "setContextBudget", "contextBudgetWantedK", "noteBudgetClamp",
                   "budgetClampNoteK"), _BUDGET_CLAMP_HARNESS, "clamp")
    assert out["wantedBefore"] == 512 and out["readAfter"] == 64, out
    assert out["storedAfter"] == 64, "越界那份没被写回：界面夹了、本机还留着 512，下次换个上限又冒出来"
    assert "512" in out["clamped"] and "64" in out["clamped"] and "夹" in out["clamped"], out["clamped"]
    assert out["afterPrint"] == "", "印过一次还在说：一句提示变成常驻警告"
    assert out["notClamped"] == "" and out["wrongCard"] == "", \
        "没被夹（或不是这张卡）时也冒出提示：界面在编一个没发生过的动作"
    assert out["rightCard"] == out["clamped"]

    # 接线：提示的唯一落点是卡片（mm-note），记夹的地方是 syncCtxRow 与 setContextBudget；
    # 顺序也钉住——syncCtxRow 必须排在 renderModelChip 之前，否则同一次渲染里卡片先画完、
    # 账还没记，那句提示就永远迟到一拍。
    js = _js()
    detail = _function_body(js, "renderModelDetail")
    assert "budgetClampNoteK(" in detail and 'fvEl("p", "mm-note"' in detail, \
        "夹完没人说：spec §3.2 那句「提示一次」落不了地"
    assert "state.budgetClamped = null" in detail, "提示印完不清：下一次渲染还在说同一句"
    sync = _function_body(js, "syncCtxRow")
    assert "contextBudgetWantedK(" in sync and "noteBudgetClamp(" in sync, \
        "syncCtxRow 还拿夹过的读数比上限：那个分支恒不成立，越界值从来没被真正夹回去过"
    order = _function_body(js, "renderModelSelect")
    assert order.index("syncCtxRow()") < order.index("renderModelChip()"), \
        "先画卡片后夹值：同一次保存里那句提示永远迟到一拍"
    assert "budgetClamped: null" in js[js.index("const state = {"):js.index("};", js.index("const state = {"))], \
        "这笔账没在 state 字面量里初始化：第一次读它时是 undefined，行为随渲染顺序变"
    reset = _function_body(js, "resetViewForIdentity")
    assert "state.budgetClamped = null" in reset, \
        "换身份没清这笔账：B 会看到一句关于 A 的预算被夹小的话"


# ---------------- Minor 3：成功句绑卡片 id ----------------

def test_saved_note_is_bound_to_the_card_it_belongs_to():
    """保存期间 hover 到别的行，成功句不许印在别人家（也不许被顺手销毁）。"""
    js = _js()
    save = _function_body(js, "saveModelCapabilities")
    assert "state.modelSaved = { id: recordId" in save, \
        "成功句又变回裸字符串：它不认得自己属于哪张卡"
    detail = _function_body(js, "renderModelDetail")
    assert "state.modelSaved.id === pid" in detail, \
        "渲染时没比对卡片 id：hover 到别的行就印错地方"
    assert "state.modelSaved = null" in detail, "印过没清：下一次渲染再印一遍"
    show = _function_body(js, "showModelDetail")
    assert "state.modelSaved" not in show, \
        "切走时把成功句清了：用户 hover 出去看一眼再回来，那句「已保存」就凭空消失"
    reset = _function_body(js, "resetViewForIdentity")
    assert "state.modelSaved = null" in reset, "换身份没清成功句：B 看见 A 的「已保存」"


# ---------------- Minor 5：hideModelMenu 的注释与代码对齐 ----------------

_HIDE_MENU_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const nodes = {};
const mk = (id) => (nodes[id] || (nodes[id] = {
  id,
  // 一开始既没有 hidden 也没有 open/card-open：判据看的是 hideModelMenu 跑完之后的状态，
  // 预置成"已经收起来"会让"忘了 add(hidden)"这种写法照样绿。
  classList: { _s: new Set(),
    add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
    contains(c) { return this._s.has(c); },
    toggle(c, on) { if (on) { this._s.add(c); } else { this._s.delete(c); } } },
}));
const ctx = {};
vm.createContext(ctx);
ctx.$ = (id) => mk(id);
ctx.state = { modelPinned: "p1", modelCardOpen: true, modelManage: true, modelDetailId: "p1" };
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
ctx.hideModelMenu();
process.stdout.write(JSON.stringify({
  pinned: ctx.state.modelPinned,
  cardOpen: ctx.state.modelCardOpen,
  manage: ctx.state.modelManage,
  menuHidden: mk("modelMenu").classList.contains("hidden"),
  cardOpenClass: mk("modelMenu").classList.contains("card-open"),
  chipOpen: mk("modelChip").classList.contains("open"),
}));
"""


def test_hiding_the_model_menu_keeps_the_manage_section():
    """Minor 5：注释说清两个本机态、代码实际清了哪几个——按行为对齐，不打反向。

    定下来的行为是「下次打开还接着上次的编辑区」：收起弹单不等于退出管理（改一条模型
    中途去看清单/切设置页，回来还得接着改）。所以 modelManage 故意不清，而
    「收起管理」按钮与换身份两处该清的都在。这条真跑把三个本机态各自的去向钉住，
    下一次"顺手补一行清理"会当场红。
    """
    out = _run_js(("hideModelMenu",), _HIDE_MENU_HARNESS, "hide-menu")
    assert out["pinned"] == "" and out["cardOpen"] is False, out
    assert out["menuHidden"] is True and out["cardOpenClass"] is False and out["chipOpen"] is False, out
    assert out["manage"] is True, \
        "管理区被收起时清了：注释与行为又打反向（本轮定的口径是留着，见 hideModelMenu 那段）"
    js = _js()
    assert "state.modelManage = false" in _function_body(js, "closeModelManage"), \
        "「收起管理」不再真的收起：留着这一格它就不是收的入口"
    assert "state.modelManage = false" in _function_body(js, "resetViewForIdentity"), \
        "换身份没清管理区：B 打开卡片直接落在 A 的编辑区里"


# ---------------- Minor 6：data-thinking 属性必须有 CSS 消费 ----------------

def _thinking_style_pairs(js: str, css: str):
    """(挂载点, 没人消费的孤儿)。射程见下面那条用例的 docstring。"""
    styled = set(re.findall(r"\.([A-Za-z][\w-]*)\[data-thinking", css))
    stamped = []
    for var in sorted(set(re.findall(r"const ([A-Za-z_$][\w$]*) = fvEl\(", js))):
        if f'{var}.setAttribute("data-thinking"' not in js:
            continue
        m = re.search(rf'const {re.escape(var)} = fvEl\("[a-z]+", "([^"]*)"', js)
        assert m, f"{var} 挂了 data-thinking 却找不到它的 fvEl 类名——这条锁要跟着改读法"
        stamped.append((var, m.group(1)))
    orphans = [(var, cls) for var, cls in stamped if not set(cls.split()) & styled]
    return stamped, orphans


def test_every_thinking_state_attribute_is_actually_styled():
    """Minor 6（派生锁）：app.js 里挂 data-thinking 的那些元素，样式表必须真的认它。

    射程：只认 `const X = fvEl("tag", "类名")` 之后紧跟 `X.setAttribute("data-thinking", ...)`
    这一种形状（模型卡那批都这么写）。把属性挂在别的来源（querySelector 拿到的节点、
    dataset 直接赋值）不在这条锁眼里——出现那种写法时要么改这条的读法，要么别指望它挡。
    """
    js = _js()
    css = _css()
    stamped, orphans = _thinking_style_pairs(js, css)
    assert len(stamped) >= 4, f"只扫到 {len(stamped)} 处挂载点，判据等于没跑：{stamped}"
    assert not orphans, (
        f"这些元素挂了 data-thinking 却没有任何 CSS 消费：{orphans}——"
        "要么补一条规则（禁用态该看得出是禁用的），要么把装饰删掉；"
        "白挂的属性会让下一个人以为样式在管它")
    # 自证有牙（在内存里做，树上不留红）：把样式表里那条"思考行降饱和"的选择器换个名字，
    # 孤儿名单必须立刻点名 mm-thinking——这正是本轮之前那两处白挂的形状。
    _, mutants = _thinking_style_pairs(js, css.replace(".mm-thinking[", ".mm-thinking-renamed["))
    assert [cls for _, cls in mutants] == ["mm-row mm-thinking"], \
        f"判据没牙：换掉 CSS 选择器后它还不知道谁变成了孤儿：{mutants}"


# ---------------- Minor 7：档位标签里的空格不许被劈开 ----------------

_THINKING_LEVELS_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
process.stdout.write(JSON.stringify({
  spaced: ctx.parseThinkingLevels("快速 思考， 均衡 、深入"),
  four: ctx.parseThinkingLevels("a,b,c,d"),
  blank: ctx.parseThinkingLevels("   "),
  none: ctx.parseThinkingLevels(null),
  dup: ctx.parseThinkingLevels("均衡，均衡，快速"),
}));
"""


def test_thinking_level_labels_survive_spaces_and_dedup():
    """Minor 7：分隔判据与后端 _clean_levels 同一口径——后端只 strip，从不按空格劈开一项。"""
    out = _run_js(("parseThinkingLevels",), _THINKING_LEVELS_HARNESS, "levels")
    assert out["spaced"] == ["快速 思考", "均衡", "深入"], \
        "含空格的档位标签被劈开了：填 3 个、库里存 4 个，撞上「最多 3 个」拒收的却是没填错的那格：" \
        f"{out['spaced']}"
    assert out["four"] == ["a", "b", "c", "d"], "界面悄悄砍掉第 4 档 = 把「没配上」演成「配上了」"
    assert out["blank"] == [] and out["none"] == [], out
    assert out["dup"] == ["均衡", "快速"], out


# ---------------- I-4：role="menu" 的 ARIA 归属 ----------------

def test_model_menu_uses_group_semantics_not_menu_semantics():
    """I-4：#modelMenu 里住着详情卡（滑杆/输入框/「模型管理」按钮），menu 语义罩不住。

    menu 的合法子元素只有 menuitem / menuitemcheckbox / menuitemradio / group / separator，
    而方向键契约（↑↓ + Home/End）没实现。改法取"去掉不适用的语义"那一支：容器换
    role="group"（仍接受 aria-label，读屏会说"选择模型 分组"），行回到普通 button——
    半套 ARIA 比不给语义更糟。附件菜单那颗 role="menu" 不在本单射程，一字未动。
    """
    html = _html()
    js = _js()
    tag = re.search(r'<div class="model-menu[^"]*" id="modelMenu"[^>]*>', html)
    assert tag, "#modelMenu 那一行的形状变了，这条锁要跟着改读法"
    assert 'role="group"' in tag.group(0), f"模型菜单还挂着 menu 语义（或干脆没角色）：{tag.group(0)}"
    assert 'role="menu"' not in tag.group(0), tag.group(0)
    assert 'aria-label="选择模型"' in tag.group(0), \
        "去掉 menu 时把 aria-label 一起删了：读屏连「选择模型」都不报了（可见行为：分组名仍播报）"
    assert "menuitem" not in _function_body(js, "renderModelChip"), \
        "行上还挂着 menuitem：它只在 menu/menubar/tree 之下才是合法角色"
    attach = html[html.index('id="attachMenu"'):html.index('id="modelMenu"')]
    assert 'role="menu"' in attach and 'role="menuitem"' in attach, \
        "附件菜单那套合法的 menu/menuitem 被顺手改掉了：本单射程只有模型菜单"


# ==================== Task 13A：档位只在「这条记录已核实」时发送 ====================
#
# 今天的事实基线（brief §0）：生产登记表只有 none ⇒ 两条现网记录都是 unverified ⇒ 客户端
# 今天没有任何一条路能把档位发出去。本单不是"以后再说"，判据现在就钉住三件事：
#   1. 只有 verified 才产出这一格，其余一律**整格省略**（不是 null、不是空串、不是 0）；
#   2. 发不发由服务端那条记录说了算，本机偏好只是必要条件（prefs 是历史值）；
#   3. 界面不许替服务端做主——>3 档只说不裁（Task 8 已否过"演成配上了"那种形状）。
# 判据一律优先 node 真跑：这一批改动的失败形状是"读源码都对、跑起来把 null 发出去了"。

_SEND_GATE_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {};
vm.createContext(ctx);
const mem = new Map();
ctx.localStorage = {
  getItem: (k) => mem.has(k) ? mem.get(k) : null,
  setItem: (k, v) => mem.set(k, String(v)),
};
ctx.pref = { userId: "u1" };
ctx.state = {};
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const verified = { id: "p1", thinking_status: "verified",
                   thinking_levels: ["快速", "均衡"], thinking_default: "均衡" };
const unverified = { id: "p1", thinking_status: "unverified",
                     thinking_levels: ["快速", "均衡"], thinking_default: "均衡" };
const unsupported = { id: "p1", thinking_status: "unsupported",
                      thinking_levels: [], thinking_default: "" };
const out = {};
out.freshVerified = ctx.thinkingLevelForSend(verified);
ctx.writeThinkingPref("p1", { enabled: true, level: "快速" });
out.verifiedOn = ctx.thinkingLevelForSend(verified);
out.unverifiedOn = ctx.thinkingLevelForSend(unverified);
out.unsupportedOn = ctx.thinkingLevelForSend(unsupported);
out.noStatus = ctx.thinkingLevelForSend({ id: "p1" });
out.nullItem = ctx.thinkingLevelForSend(null);
ctx.writeThinkingPref("p1", { enabled: false });
out.verifiedOff = ctx.thinkingLevelForSend(verified);
ctx.writeThinkingPref("p1", { enabled: true, level: "深度" });
out.staleLabel = ctx.thinkingLevelForSend(verified);
out.verifiedNoLevels = ctx.thinkingLevelForSend(
  { id: "p9", thinking_status: "verified", thinking_levels: [], thinking_default: "" });
process.stdout.write(JSON.stringify(out));
"""


def test_the_level_is_produced_only_for_a_verified_record():
    """§1.1 + §0：发送侧唯一判据的完整矩阵。unverified 那一格是本单的主判据——
    服务端说没核实，本机偏好记着"开"也不能产出档位标签。"""
    out = _run_js(("thinkingPrefsKey", "readThinkingPrefs", "writeThinkingPref",
                   "thinkingChoice", "thinkingLevelForSend"), _SEND_GATE_HARNESS, "gate")
    assert out["freshVerified"] == "", "开关没打开就不该发档位（默认不发）"
    assert out["verifiedOn"] == "快速", out["verifiedOn"]
    assert out["unverifiedOn"] == "", \
        "unverified 产出了档位：今天两条现网记录都会被发出一格服务端换不出参数的请求"
    assert out["unsupportedOn"] == "", out["unsupportedOn"]
    assert out["noStatus"] == "" and out["nullItem"] == "", \
        "状态缺失/没有记录时按最保守的一支走（省略），不许猜 verified"
    assert out["verifiedOff"] == "", "开关关掉仍产出档位：prefs 盖过了本机意图"
    assert out["staleLabel"] == "均衡", \
        f"偏好里的标签已不在清单里，必须回到默认档而不是把旧标签发出去：{out['staleLabel']}"
    assert out["verifiedNoLevels"] == "", "清单为空时没有可发的标签，只能省略"


_API_STUB = r"""
var __bodies = [];
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
globalThis.document = {
  body: { appendChild() {}, contains: () => false },
  createElement: () => ({ style: {}, classList: { add() {}, remove() {}, toggle() {} },
    appendChild() {}, setAttribute() {}, addEventListener() {}, remove() {},
    querySelector: () => null }),
  getElementById: () => null, addEventListener() {},
  documentElement: { style: { setProperty() {} } },
};
globalThis.navigator = { onLine: true };
globalThis.location = { href: "http://127.0.0.1:8000/", pathname: "/" };
globalThis.fetch = async (path, init) => {
  if (path === "/v1/chat/stream") __bodies.push(JSON.parse(init.body));
  const bytes = new TextEncoder().encode(
    'data: {"type":"done","status":"completed","message_id":"m-1"}\n\n');
  let sent = false;
  return {
    ok: true, status: 200, headers: { get: () => null }, json: async () => ({}),
    body: { getReader: () => ({ read: async () => {
      if (sent) return { done: true };
      sent = true;
      return { value: bytes, done: false };
    } }) },
  };
};
"""

_API_DRIVER = r"""
const call = async (level) => {
  __bodies.length = 0;
  const opts = { model: "p1", provider: "p1", sessionId: "s-1",
                 messages: [{ role: "user", content: "hi" }] };
  if (level !== undefined) opts.thinkingLevel = level;
  await API.streamChat(opts, () => {});
  return __bodies[0] || null;
};
return {
  label: await call("快速"),
  empty: await call(""),
  nul: await call(null),
  zero: await call(0),
  omitted: await call(undefined),
};
"""


def _run_api_js(driver: str):
    """node 真跑仓库那份 api.js：假 fetch 收下的请求体是判据，不是 grep。

    照 test_v0350_task_ui_web_contract 的口径（"导出漏一格，页面照常加载、只有点进去
    才炸"）——发不发这一格同样是"读源码都对、跑起来发错"的形状。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "fns.js").write_text(_API_STUB + (STATIC / "api.js").read_text(encoding="utf-8"),
                                  encoding="utf-8")
        (d / "driver.js").write_text(driver, encoding="utf-8")
        (d / "harness.cjs").write_text(
            "const fs = require('fs');\n"
            "const run = new Function(fs.readFileSync(process.argv[2], 'utf8')"
            " + '\\nreturn (async () => {\\n' + fs.readFileSync(process.argv[3], 'utf8')"
            " + '\\n})();');\n"
            "Promise.resolve(run())"
            ".then((v) => process.stdout.write(JSON.stringify({ ok: true, out: v })))"
            ".catch((e) => process.stdout.write(JSON.stringify("
            "{ ok: false, error: String((e && e.stack) || e) })));\n",
            encoding="utf-8")
        r = subprocess.run([node, str(d / "harness.cjs"), str(d / "fns.js"),
                            str(d / "driver.js")],
                           capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert r.returncode == 0, f"node 自己就挂了：{r.stderr}"
        line = [x for x in r.stdout.strip().splitlines() if x.startswith("{")]
        assert line, f"node 没给出 JSON：stdout={r.stdout!r} stderr={r.stderr!r}"
        out = json.loads(line[-1])
        assert out["ok"], f"这段 JS 跑挂了：{out['error']}"
        return out["out"]


def test_the_request_body_carries_the_key_only_with_a_real_label():
    """§1.1 的行为半边：请求体里这一格要么是非空标签，要么**整格不存在**。

    服务端把"没带这一格"和"带了一格但值是空的"当两件事，省略才是"这一轮不参与"。
    所以 null / "" / 0 / 不传 四种输入都必须让 `thinking_level` 这个键消失，
    而其余各格（model/messages/session_id）照常——别把省略做成顺手少发别的。
    """
    out = _run_api_js(_API_DRIVER)
    assert out["label"]["thinking_level"] == "快速", out["label"]
    for case in ("empty", "nul", "zero", "omitted"):
        body = out[case]
        assert body is not None, f"{case}：这一枪根本没发出去"
        assert "thinking_level" not in body, \
            f"{case} 把「整格省略」写成了「发一个空值」：{body}"
        for keep in ("model", "provider", "messages", "session_id", "attachments"):
            assert keep in body, f"{case}：为了省略档位把 {keep} 也弄丢了"


def test_no_site_can_write_a_null_or_empty_thinking_level():
    """§1.1 的形状半边 + §1.2：禁用态在请求构造路径上不可能产出这一格。

    判"没有 `thinking_level: null` / `= \"\"` 这种形状"用源码形状：这一格在 api.js 里
    一次都不作为对象字面量的键出现（`thinking_level:` 这个写法在本仓之后不存在），
    唯一出处是 `body.thinking_level = ...` 那一条被 typeof + 非空双闸包着的赋值。
    """
    js = _js()
    api = _js("api.js")
    assert re.search(r"thinking_level\b", js) is None, \
        "app.js 里出现了 thinking_level 这个单数格：请求体的键只在 api.js 那一处写，两处写就两处判"
    assert re.search(r"thinking_level\s*:", api) is None, \
        'api.js 里以对象字面量键的写法出现 thinking_level：那等于给 null/"" 留了一个能写的地方'
    assert api.count("body.thinking_level") == 1, \
        f"这一格的写口不止一处（{api.count('body.thinking_level')}）：省略语义就没有唯一出处"
    guard = _function_body(api, "streamChat")
    assert "typeof opts.thinkingLevel === \"string\"" in guard \
        and "opts.thinkingLevel !== \"\"" in guard, \
        "写这一格的闸不是「非空字符串」：0 / null / 空串里总有一种能漏过去"
    run = _function_body(js, "runStream")
    assert "thinkingLevel: thinkingLevelForSend(" in run, \
        "发送路径没走那一个判据：谁都能在 runStream 里再算一遍发不发"
    assert re.search(r"thinking_level\b", run) is None, \
        "runStream 自己拼请求体的单数那一格：发/不发就散成两处了"
    gate = _function_body(js, "thinkingLevelForSend")
    assert '!== "verified"' in gate, "判据里没有 verified 那道闸：unverified 也能产出标签"
    assert "enabled !== true" in gate, "判据没看开关：禁用态也能产出标签（§1.2）"
    assert "indexOf(" in gate and "thinking_levels" in gate, \
        "标签没跟这条记录**当前**的清单对一遍：会发一个清单外的标签（§5.3）"


_CAP_NOTE_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
process.stdout.write(JSON.stringify({
  none: ctx.thinkingLevelsCapNote(null),
  empty: ctx.thinkingLevelsCapNote([]),
  three: ctx.thinkingLevelsCapNote(["甲", "乙", "丙"]),
  four: ctx.thinkingLevelsCapNote(["甲", "乙", "丙", "丁"]),
}));
"""


def _app_const(name: str) -> str:
    """从 app.js 现抓一行模块级 `const name = <数字>;`，喂给 node 真跑的提取体。

    抓不到就红：数字留在测试里、不留在被测代码里，是本单最不能要的那种"锁跟着改名
    一起失效"的形状（同一口径见 test_v0350_task_ui_web_contract._task_module_source）。
    """
    hit = re.findall(rf"^const {name} = (\d+);", _js(), flags=re.M)
    assert hit, f"app.js 里没有 `const {name} = <数字>;` 这一行：常量改名或数字写进了函数里"
    assert len(hit) == 1, f"app.js 里 {name} 声明了 {len(hit)} 次：上限不止一个出处"
    return f"const {name} = {hit[0]};"


def test_over_cap_levels_are_announced_and_never_trimmed_in_the_browser():
    """§5.1（12B 复审点名留给本单的）：上限在界面先说话，服务端那句拒绝原样显示。

    今天管理员打四个标签 ⇒ 送出 4 档 ⇒ 收到 400。修法是把上限和那句拒绝**说出来**，
    不是前端悄悄砍成 3——砍掉等于把"没配上"演成"配上了"（Task 8 已否过一次）。
    既有那条 `four == ["a","b","c","d"]` 守着"不截断"，这条守着"要说话"。
    """
    out = _run_js(("thinkingLevelsCapNote",), _CAP_NOTE_HARNESS, "cap",
                  extra=_app_const("THINKING_LEVELS_CAP"))
    assert out["none"] == "" and out["empty"] == "" and out["three"] == "", \
        "没越界也要闭嘴：常驻一句警告等于没有警告"
    assert "思考档位最多 3 个" in out["four"], \
        f"那句拒绝要与服务端那一条原样对上，别自己改写：{out['four']}"
    assert "4" in out["four"], f"要说清现在有几个：{out['four']}"
    js = _js()
    parse = _function_body(js, "parseThinkingLevels")
    for trim in ("slice(", "splice(", "substring("):
        assert trim not in parse, f"清洗档位时把第 4 档砍了（{trim}）：演成配上了"
    manage = _function_body(js, "modelManageSection")
    assert "thinkingLevelsCapNote(" in manage, "编辑区没把这句上限话说出来：只能等 400"
    assert "textContent" in manage, "编辑区不走 textContent"
    _no_html_sinks(manage, "能力编辑区（含档位上限提示）")
    # 那个数字必须与服务端同一出处（静态对照，不 import providers：本批的隔离规矩）。
    server = (Path(__file__).resolve().parents[1] / "app" / "core" / "providers.py")
    hit = re.search(r"^MAX_THINKING_LEVELS = (\d+)$", server.read_text(encoding="utf-8"), re.M)
    assert hit, "服务端那句上限没扫到：providers.MAX_THINKING_LEVELS 的形状变了，界面这句话该跟着改读法"
    assert hit.group(1) == re.findall(r"^const THINKING_LEVELS_CAP = (\d+);", js, re.M)[0], \
        "界面说好的上限与服务端存不下的那个数不是同一个：警告会说谎，400 照样来"


_FALLBACK_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {};
vm.createContext(ctx);
const mem = new Map();
ctx.localStorage = {
  getItem: (k) => mem.has(k) ? mem.get(k) : null,
  setItem: (k, v) => mem.set(k, String(v)),
};
ctx.pref = { userId: "u1" };
ctx.state = {};
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const unverified = { id: "p1", thinking_status: "unverified",
                     thinking_levels: ["快速", "均衡"], thinking_default: "均衡" };
const unsupported = { id: "p1", thinking_status: "unsupported",
                      thinking_levels: [], thinking_default: "" };
const verified = { id: "p1", thinking_status: "verified",
                   thinking_levels: ["快速", "均衡"], thinking_default: "均衡" };
ctx.writeThinkingPref("p1", { enabled: true, level: "快速" });
out = {};
out.onUnverified = ctx.thinkingInactiveNote(unverified, ctx.thinkingChoice("p1", unverified));
out.onUnsupported = ctx.thinkingInactiveNote(
  unsupported, ctx.thinkingChoice("p1", unsupported));
out.onVerified = ctx.thinkingInactiveNote(verified, ctx.thinkingChoice("p1", verified));
ctx.writeThinkingPref("p1", { enabled: false });
out.offUnverified = ctx.thinkingInactiveNote(unverified, ctx.thinkingChoice("p1", unverified));
ctx.writeThinkingPref("p1", { enabled: true, level: "深度" });
out.staleFirst = ctx.thinkingStaleLevelNote(verified, ctx.thinkingChoice("p1", verified));
out.staleAgain = ctx.thinkingStaleLevelNote(verified, ctx.thinkingChoice("p1", verified));
ctx.writeThinkingPref("p1", { enabled: true, level: "快速" });
out.notStale = ctx.thinkingStaleLevelNote(verified, ctx.thinkingChoice("p1", verified));
process.stdout.write(JSON.stringify(out));
"""


def test_a_fall_back_says_why_the_click_did_nothing():
    """§5.2 + §5.3：回落要在卡上说得出口，而且同一句只说一次。

    12B 的留痕是服务端一条 debug，客户端看不见——"点了档位没生效"必须在这台设备上
    自己讲得清：① 记录未核实 ⇒ 参数不生效；② 偏好里的标签不在当前清单 ⇒ 回到默认档，
    并且显示一次（照 state.budgetClamped 那一组"印完就清"的口径，不每帧刷一句）。
    """
    out = _run_js(("thinkingPrefsKey", "readThinkingPrefs", "writeThinkingPref",
                   "thinkingChoice", "thinkingInactiveNote", "thinkingStaleLevelNote"),
                  _FALLBACK_HARNESS, "fallback")
    assert "未核实" in out["onUnverified"] and "参数不生效" in out["onUnverified"], \
        out["onUnverified"]
    assert "参数不生效" in out["onUnsupported"], out["onUnsupported"]
    assert out["onVerified"] == "", "已核实时别再说不生效：那是说谎"
    assert out["offUnverified"] == "", "开关本来就没开，没有「没生效」要解释"
    assert "深度" in out["staleFirst"] and "均衡" in out["staleFirst"], out["staleFirst"]
    assert out["staleAgain"] == "", "同一句重复刷：每渲染一次就多一行"
    assert out["notStale"] == "", out["notStale"]
    detail = _function_body(_js(), "renderModelDetail")
    assert "thinkingInactiveNote(" in detail and "thinkingStaleLevelNote(" in detail, \
        "两句回落说明没接到卡片渲染：判据只在函数里活着，界面上看不见"
    _no_html_sinks(detail, "模型卡（含回落说明）")


# 13A 复审 Finding 2 的现场：同一张卡上两句相邻说明自相矛盾。这里把
# thinkingStaleLevelNote 在**三态**下各跑一次（旧标签「深度」都不在当前清单里，所以三态
# 都会触发那句回落说明），逐格问它「现在到底发不发」：
#   verified+开关开 → thinkingLevelForSend 真会产出一格 ⇒ 允许承诺「这一轮按默认档…发」；
#   unverified+开关开 → 一整格都不出网 ⇒ 同一张卡的 thinkingInactiveNote 正在说「不会带上档位」；
#   verified+开关关 → 同样不发，而 thinkingInactiveNote 这一格是空的 ⇒ 全靠这句讲真话。
# 每格前把 state.thinkingStaleLevel 复位，否则「只说一次」那把锁会把后两格吞成空串。
_STALE_TRUTH_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {};
vm.createContext(ctx);
const mem = new Map();
ctx.localStorage = {
  getItem: (k) => mem.has(k) ? mem.get(k) : null,
  setItem: (k, v) => mem.set(k, String(v)),
};
ctx.pref = { userId: "u1" };
ctx.state = {};
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const unverified = { id: "p1", thinking_status: "unverified",
                     thinking_levels: ["快速", "均衡"], thinking_default: "均衡" };
const verified = { id: "p1", thinking_status: "verified",
                   thinking_levels: ["快速", "均衡"], thinking_default: "均衡" };
const arm = (patch) => {
  ctx.state.thinkingStaleLevel = null;
  ctx.writeThinkingPref("p1", { enabled: true, level: "深度" });
  if (patch) ctx.writeThinkingPref("p1", patch);
};
out = {};
arm();
out.verifiedOn = ctx.thinkingStaleLevelNote(verified, ctx.thinkingChoice("p1", verified));
arm();
out.unverifiedOn = ctx.thinkingStaleLevelNote(unverified, ctx.thinkingChoice("p1", unverified));
arm({ enabled: false });
out.verifiedOff = ctx.thinkingStaleLevelNote(verified, ctx.thinkingChoice("p1", verified));
arm();
out.unverifiedOnInactive = ctx.thinkingInactiveNote(unverified, ctx.thinkingChoice("p1", unverified));
arm({ enabled: false });
out.verifiedOffInactive = ctx.thinkingInactiveNote(verified, ctx.thinkingChoice("p1", verified));
process.stdout.write(JSON.stringify(out));
"""


def test_the_stale_note_promises_a_send_only_when_a_level_is_sent():
    """13A 复审 Finding 2：那句「这一轮按默认档发」不许在**根本不发**的两格里出现。

    §5.2 的红线是"界面说的话与请求体一致"。今天未核实的记录一整格都不出网、开关关着同样
    不出网，而原句把这两种都说成要发——同一张卡上 `thinkingInactiveNote` 还在说「不会带上
    档位」，两句当面打架。修法口径：只有 `choice.enabled === true && 状态 verified`
    才许说"按默认档发"，其余分支改成"回到默认档、本轮不发这一格"。
    """
    out = _run_js(("thinkingPrefsKey", "readThinkingPrefs", "writeThinkingPref",
                   "thinkingChoice", "thinkingInactiveNote", "thinkingStaleLevelNote"),
                  _STALE_TRUTH_HARNESS, "stale-truth")
    promise = "这一轮按默认档"
    # ① 真会发：承诺可以存在，而且要讲清发的是回落后的那个默认档
    assert promise in out["verifiedOn"], out["verifiedOn"]
    assert "深度" in out["verifiedOn"] and "均衡" in out["verifiedOn"], out["verifiedOn"]
    # ②③ 不发的那两态：同一句承诺必须消失，并老实说这一轮不发
    for key in ("unverifiedOn", "verifiedOff"):
        assert out[key] != "", f"{key}：旧标签回落一句都不说了，用户只看到档位自己变了"
        assert promise not in out[key], \
            f"{key} 根本不发这一格，却仍然承诺「{promise}发」：界面与请求体不一致（§5.2）"
        assert "不发" in out[key], f"{key}：不发就要说不发，别留一句含糊的归回：{out[key]}"
    # 未核实那格由既有那句「不生效」一起说，两句不得互相矛盾
    assert "不会带上档位" in out["unverifiedOnInactive"], out["unverifiedOnInactive"]
    # 开关关着时既有那句「不生效」今天不出场（enabled !== true 直接返回空），
    # 所以这一格卡上**只有**回落那一句可说——它必须自己讲真话。
    assert out["verifiedOffInactive"] == "", \
        "开关本来没开，没有「没生效」要解释；此时卡上只剩回落那一句，它更不许承诺要发"
    for key in ("verifiedOn", "unverifiedOn", "verifiedOff"):
        for banned in BANNED_THINKING_PHRASES:
            assert banned not in out[key], f"{key} 里出现 §2 禁句「{banned}」"


# ---------------- 13A 复审 Finding 1：禁句清单本身少一味 ----------------
# 清单的唯一出处是 `task-12-amendment.md` §2 那一条，点名的句子共**四味**：
# 「可关闭思考」「思考已关闭」「支持关闭」「三档强度」。原先这里是一份手抄 tuple，
# 少抄了「三档强度」——审查员往 app.js 注入 `return "这款模型支持三档强度。"` 后
# 跑整份文件得到 32 passed / 0 failed，等于这句话今天写进界面没有任何一把锁会红。
# 修法不是再手抄一遍（手抄的东西会再次悄悄变短），而是把 §2 那一行原文抄进锁里、
# 由它现抠「」内容当清单来源，再用**句数 + 集合**两道比对钉住扫描实际吃的那份：
# 少一味、多一味、抄错一味，当场红。
# （那份 amendment 文档被 `.superpowers/sdd/.gitignore:1` 整目录排除，CI 上读不到，
# 所以抄原文而不是运行时去读它——读不到就 skip 的锁等于没有锁。）
_AMENDMENT_S2_BANNED_LINE = (
    "界面/发版说明/文档里不许出现「可关闭思考」「思考已关闭」「支持关闭」「三档强度」"
    "这类今天没有对照支持的句子"
)
# §2 那一条点名的句数（裁决原文里就是四味；这个数字是清单的体检尺，见锁里第一道断言）。
_AMENDMENT_S2_BANNED_COUNT = 4
# 裁决 §2 现抠出来的那份（清单来源）
_AMENDMENT_S2_BANNED = tuple(re.findall(r"「([^「」]+)」", _AMENDMENT_S2_BANNED_LINE))
# 扫描实际吃的那份（谁被改窄了都会撞上下面两道断言）
BANNED_THINKING_PHRASES = ("可关闭思考", "思考已关闭", "支持关闭", "三档强度")

# ---------------- 禁句语料的**射程**：从仓库事实派生，不写死文件名（整枝审查 B1） ----------------
# §2 那句裁决声称的是「界面/发版说明/文档」，而这里此前写死 `app.js`/`api.js`/`index.html`
# 三份——只罩得住 /app 那一面：管理台（admin/）、官网（site/）、发版说明（docs/releases/）
# 与 changelog.json 全都不判。审查员的演绿证据：把「这款模型可关闭思考」写进
# `admin/admin.js` 与 `site/site.js`，相关六份测试 235 passed / rc=0，没有任何一把锁红。
# 与原生那把（`67e76585` 里的 `_native_kt_corpus`）同一形状：磁盘扫到的一份必须与
# `git ls-files` 记的那一份**同一个集合**——少一份（改名/挪走/走空目录）多一份（没入库的
# 游离文件）都红；git 问不到或返回空一律红，不 skip 后当绿。
_WEB_COPY_ROOTS = ("backend/app/web/static", "backend/app/web/admin",
                   "backend/app/web/site", "docs/releases")
# 只判文本表面：`site/img/*.jpg` 那种截图不是"文案"。两侧用同一个后缀过滤，
# 所以这层排除面吃不进真源码（与原生那把按 `/build/` 段排除同理）。
_WEB_COPY_SUFFIXES = (".js", ".html", ".css", ".json", ".md", ".webmanifest")
# 点名在册的四味表面各留一份代表：任何一份被挪出射程 ⇒ 语料被改窄了。
_WEB_COPY_MINIMUM = {"backend/app/web/static/app.js", "backend/app/web/static/api.js",
                     "backend/app/web/static/index.html", "backend/app/web/admin/admin.js",
                     "backend/app/web/site/site.js", "backend/app/web/site/changelog.json"}


def _tracked_web_copy() -> set:
    """`git ls-files` 在 §2 那四个表面上记的文本文件集合——基数钉的**仓库事实**一侧。"""
    out = subprocess.run(["git", "ls-files", "-z", *_WEB_COPY_ROOTS], cwd=str(REPO),
                         capture_output=True, timeout=60)
    assert out.returncode == 0, "问不到 `git ls-files`：同集合比对不许 skip 后当绿"
    tracked = {p.decode().replace("\\", "/") for p in out.stdout.split(b"\0") if p}
    tracked = {p for p in tracked if Path(p).suffix in _WEB_COPY_SUFFIXES}
    assert tracked, "`git ls-files` 在这四个表面上返回 0 份文本：基数钉读不到仓库事实"
    return tracked


def _web_copy_text(rel: str) -> str:
    """按表面选尺子：.js 剥 JS 注释、.html 剥 HTML 注释，其余（.md/.json/.css）原文。

    与 `_js()`/`_html()` 同口径——注释里出现同词不判（`app.js` 那条"不许写成…"的说明
    就是形状本身）。CSS 与 `_css()` 一样刻意不剥：注释掉的文案仍是给下一个接手的人看的。
    """
    src = (REPO / rel).read_text(encoding="utf-8")
    if rel.endswith(".js"):
        return _strip_js_comments(src)
    if rel.endswith(".html"):
        return _strip_html_comments(src)
    return src


def _web_copy_corpus() -> dict:
    """§2 声称的全部表面：磁盘扫到的一份与 git 记的一份必须**同一个集合**。"""
    on_disk = set()
    for root in _WEB_COPY_ROOTS:
        base = REPO / root
        assert base.is_dir(), f"§2 声称的表面 {root} 读不到：『扫不到』不许当成『守住了』"
        on_disk |= {p.relative_to(REPO).as_posix() for p in base.rglob("*")
                    if p.is_file() and p.suffix in _WEB_COPY_SUFFIXES}
    tracked = _tracked_web_copy()
    assert on_disk == tracked, (
        f"禁句语料的射程与仓库事实不同：磁盘扫到 {len(on_disk)} 份 / git 记 {len(tracked)} 份，"
        f"只在磁盘={sorted(on_disk - tracked)} 只在 git={sorted(tracked - on_disk)}")
    assert _WEB_COPY_MINIMUM <= on_disk, \
        f"§2 点名的表面已不在语料里：缺 {sorted(_WEB_COPY_MINIMUM - on_disk)}"
    assert len(on_disk) >= 60, \
        f"全表面只扫到 {len(on_disk)} 份：walk 进空目录也算『没有违规』，这一份读歪了"
    return {rel: _web_copy_text(rel) for rel in sorted(on_disk)}


def test_one_pref_source_no_cached_level_count_and_no_banned_copy():
    """§1.3 + §5.3 + §0：偏好一份真相、不缓存"这条记录有几个档"、禁句不出现在两端文案里。

    清单一律取服务端这一条记录的**当前**那份（thinking_levels），界面不留副本：留一份
    条数副本就会出现"清单改了、界面还按旧条数决定发不发"那种漂移（服务端发送点现在
    会重判序号上界，客户端猜不得）。换身份那一路：一次性说明的本机态跟着清，
    照 Task 5 resetViewForIdentity 的对称口径补一条。
    """
    js = _js()
    assert js.count('"fv.thinking.') == 1, \
        "本机思考偏好开了第二个键：两份真相迟早不一致（§1.3）"
    assert "state.thinkingPref" not in js, "新开了一份 state.thinkingPref：localStorage 那份是唯一真相"
    for cache in ("thinkingLevelCount", "levelCount", "cachedLevels", "thinkingLevelsCache"):
        assert cache not in js, f"界面缓存了「这条记录有几个档」的副本：{cache}"
    gate = _function_body(js, "thinkingLevelForSend")
    assert "item.thinking_levels" in gate, "判据没读这条记录当前的清单"
    reset = _function_body(js, "resetViewForIdentity")
    assert "state.thinkingStaleLevel" in reset, \
        "换身份没清那句一次性回落说明：B 会看到一句关于 A 的偏好的话（§1.3 对称锁）"
    state_block = js[js.index("const state = {"):js.index("const state = {") + 4000]
    assert "thinkingStaleLevel: null" in state_block, \
        "那次性说明的本机态没在 state 里声明：换身份清它就成了清一个不存在的键"
    # 射程 = §2 声称的全部表面（界面 + 发版说明 + 文档），由 `git ls-files` 现取并与磁盘
    # 扫到的一份同集合比对；写死三份是整枝审查的 B1（管理台/官网/发版说明/changelog 全不判）。
    corpora = _web_copy_corpus()
    assert set(corpora) >= _WEB_COPY_MINIMUM, "语料集合里没有那六份代表：射程被改窄了"
    # 两道体检：先确认清单来源本身是完整的（§2 那一行被改窄就红），再确认扫描吃的那份
    # 与它**同基数同内容**——手写 tuple 少一味不再是"安静地少扫一句"，而是当场红。
    assert len(_AMENDMENT_S2_BANNED) == _AMENDMENT_S2_BANNED_COUNT, \
        f"裁决 §2 那一行只抠出 {len(_AMENDMENT_S2_BANNED)} 味，点名应是 " \
        f"{_AMENDMENT_S2_BANNED_COUNT} 味：清单来源本身被改窄了（{_AMENDMENT_S2_BANNED}）"
    assert len(BANNED_THINKING_PHRASES) == len(_AMENDMENT_S2_BANNED), \
        f"扫描只吃了 {len(BANNED_THINKING_PHRASES)} 味，裁决 §2 点名 " \
        f"{len(_AMENDMENT_S2_BANNED)} 味：漏掉的那句写进界面没有任何锁会红"
    assert set(BANNED_THINKING_PHRASES) == set(_AMENDMENT_S2_BANNED), \
        f"扫描清单与裁决 §2 不同：只扫 {set(BANNED_THINKING_PHRASES)} / " \
        f"§2 点名 {set(_AMENDMENT_S2_BANNED)}"
    for banned in BANNED_THINKING_PHRASES:
        for name, text in corpora.items():
            assert banned not in text, f"{name} 里出现禁句「{banned}」：今天没有一条记录能说关闭"
