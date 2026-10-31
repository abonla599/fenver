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


def test_ring_sits_beside_model_chip_and_settings_slider_survives():
    """环放在现有 #modelChip 旁边（不硬造第二套模型控件）；设置页 #ctxRange
    要到 Task 9 才迁移，这里钉住它仍在；账本是 role=dialog 的层。"""
    html = _html()
    foot = html[html.index('class="input-foot"'):html.index("</form>")]
    assert 'id="ctxRingBtn"' in foot and 'id="modelChip"' in foot, "环或芯片不在输入脚"
    assert foot.index('id="ctxRingBtn"') < foot.index('id="modelChip"'), \
        "环应排在芯片前（点环开账本，点芯片仍开模型菜单）"
    assert 'id="ctxRange"' in html, "设置页预算滑杆被提前拆掉了（Task 9 才迁移）"
    assert 'id="contextPanel"' in html and 'role="dialog"' in html


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
        "clientContextK", "outbound"))
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
