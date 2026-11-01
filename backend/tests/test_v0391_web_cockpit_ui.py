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

from tests.test_web_pwa import _css, _fn_text, _function_body, _html, _js


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
    assert "innerHTML" not in panel, "账本对用户/模型文本拼了 innerHTML"
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
    assert "textContent" in card and "innerHTML" not in card, \
        "队列卡对用户文本拼了 innerHTML"
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
    assert "innerHTML" not in detail, "模型卡对模型文本拼了 innerHTML"
    assert "data-thinking" in js and "thinking_levels" in js, "思考三态/档位没有接线"
    assert "updateProviderCapabilities" in _js(), "能力写口没有 API 封装"
    # 本单补锁：行是模型名（服务端文本）的第一个展示面，同样不许拼 innerHTML
    assert "innerHTML" not in render, "模型行对模型名拼了 innerHTML"


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
    for keep in ("api_key", "is_default", "paid_by", "pricing", "thinking_profile",
                 "base_url", "model", "supports_vision"):
        assert keep in body, f"全量 PUT 少了 {keep}：一次保存会把这一格洗掉"
    assert 'api_key: ""' in body, "api_key 不传空串就带不回原密钥（服务端按占位符保留）"
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
    assert "innerHTML" not in section, "管理区对模型文本拼了 innerHTML"
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
    assert "max_context_k" not in draft and "provCtxK" not in draft, \
        "表单还在发上限：这一格从此只由模型卡写"
    for name in ("openProviderForm", "openBlankProviderForm", "fillFormFromPreset"):
        assert "provCtxK" not in _function_body(js, name), \
            f"{name} 还读 provCtxK：字段删掉后就是 null.value 空指针"
    assert "provCtxK" not in js, "app.js 里还留着 provCtxK 的引用"
