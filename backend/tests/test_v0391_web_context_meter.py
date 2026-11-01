# v0.39.x 模型驾驶舱 Task 2：网页上下文预算/校准/分段/轻量整理的纯函数锁。
#
# 判据一律走 node 真跑（同 test_token_estimate_and_budget_truncation_behavior 的理由）：
# 校准比例、残差口径、"丢哪条"这类行为读源码读不出对错，文本断言只能钉形状不能钉语义。
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.test_web_pwa import _fn_text, _function_body, _js


HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const ctx = {};
vm.createContext(ctx);
const mem = new Map();
ctx.localStorage = {
  getItem: (k) => mem.has(k) ? mem.get(k) : null,
  setItem: (k, v) => mem.set(k, String(v)),
  removeItem: (k) => mem.delete(k),
};
ctx.state = { providers: [
  { id: "m1", max_context_k: 300, usable: true },
  { id: "m2", max_context_k: 16, usable: true },
], messages: [], serverDefault: "m1" };
ctx.pref = { userId: "u1", provider: "m1", contextTokensK: 8 };
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const before = ctx.contextBudgetK("m1");
ctx.setContextBudget("m1", 500);
const after = ctx.contextBudgetK("m1");
const other = ctx.contextBudgetK("m2");
const cal = ctx.calibrateUsedK(90000, 224000, 82000);
const segs = ctx.contextSegments(cal, { persona: 4000, history: 58000, input: 8000 });
const tidy = ctx.dropOldestToBudget([
  { role: "user", content: "开场" },
  { role: "assistant", content: "答" },
  { role: "user", content: "最后一条" },
], 18);
// Fix Round 1 口径单源：环/账本/outbound/runStream 都该读 budgetProviderId()——
// currentProvider()（含"清单命中且可用，否则回落服务端默认"）优先，pref.provider 兜底。
// 三个形状都要真跑一遍：回落、命中、清单与 pref 全空。
ctx.pref.provider = "missing";
const pidFallback = ctx.budgetProviderId();
ctx.pref.provider = "m1";
const pidOwn = ctx.budgetProviderId();
ctx.state.providers = [];
ctx.state.serverDefault = "";
ctx.pref.provider = "";
const pidEmpty = ctx.budgetProviderId();
process.stdout.write(JSON.stringify({ before, after, other, cal, segs, tidy, pidFallback, pidOwn, pidEmpty }));
"""


# 摘进 vm 真跑的那批纯函数（一份名单，两处消费：切源码 + 派生锁核对）。
# 名单必须对调用边闭合——见文件末 test_the_meter_slice_carries_every_function_it_calls：
# 往 app.js 那组函数里加一个新调用而名单没跟上时，node 抛 ReferenceError，
# 错话只会说"harness 自己跑失败"（Task 9 修复轮给 setContextBudget 加 noteBudgetClamp
# 时就是这样撞上一次红）。
METER_SLICE_FUNCS = (
    "estimateTokens", "truncateWithin", "contextBudgetStorageKey",
    "readContextBudgets", "contextBudgetK", "setContextBudget", "noteBudgetClamp",
    "calibrateUsedK", "contextSegments", "dropOldestToBudget",
    "serverDefaultProvider", "currentProvider", "budgetProviderId")


def test_context_budget_map_cap_calibration_and_tidy():
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    src = "\n".join(_fn_text(_js(), n) for n in METER_SLICE_FUNCS)
    d = Path(tempfile.mkdtemp(prefix="model-cockpit-ctx-"))
    (d / "fns.js").write_text(src, encoding="utf-8")
    (d / "harness.cjs").write_text(HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(d / "harness.cjs"), str(d / "fns.js")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"harness 自己跑失败：\n{r.stdout}\n{r.stderr}"
    out = json.loads(r.stdout)
    assert out["before"] == 8, "未迁移时没沿用旧 8K 缺省"
    assert out["after"] == 300, "预算没被模型上限 300 夹住"
    assert out["other"] == 8, "另一个模型没有独立 8K 缺省"
    assert out["cal"] == 245854, f"校准公式变了：{out['cal']}"
    assert out["segs"]["residual"] == 175854, f"服务端注入差额不对：{out['segs']}"
    assert out["tidy"]["droppedCount"] == 1 and out["tidy"]["keptRoles"] == ["assistant", "user"], \
        f"轻量整理丢错消息：{out['tidy']}"
    assert out["pidFallback"] == "m1", \
        "pref.provider 不在清单时预算口径没回落到服务端默认：环与实发会分叉"
    assert out["pidOwn"] == "m1", "pref.provider 命中清单时 budgetProviderId 不该换模型"
    assert out["pidEmpty"] == "", "清单与 pref 全空时应给空串，不该抛或留旧值"


def test_the_meter_slice_carries_every_function_it_calls():
    """派生锁（同 Task 9 修复轮 I-1 那一条的形状）：这份名单对调用边必须闭合。

    判据三侧现读：名单里每个函数体调到的 `name(`（排掉属性调用与语句关键字），
    凡是 app.js 里有 `function X(` 定义的，就必须自己也在名单里。差集非空即红。
    它挡的是这一类："往 setContextBudget 里加一行调用，名单没跟上"——那时 node 抛
    ReferenceError，错话说成"harness 自己跑失败"，看代码的人以为是测试坏了。
    仍漏的形状：经变量间接调用（`const f = g; f()`）、eval 出来的名字、注释里的调用。
    """
    js = _js()
    defined = set(re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", js, re.M))
    assert defined, "app.js 里一个 `function X(` 都没扫到——这条锁的读法坏了"
    stmt = {"if", "for", "while", "switch", "catch", "return", "typeof", "function", "new"}

    def gap_of(kept_names):
        kept = set(kept_names)
        out = set()
        for name in METER_SLICE_FUNCS:
            body = _function_body(js, name)
            calls = {c for c in re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", body)} - stmt
            out |= (calls & defined) - kept
        return out

    missing = gap_of(METER_SLICE_FUNCS)
    assert not missing, (
        f"这些函数调用了名单外的 app.js 函数，vm 里必抛 ReferenceError：{sorted(missing)}"
        "——把缺的名字加进 METER_SLICE_FUNCS（或让调用侧自己守卫），别等 node 报"
        "「harness 自己跑失败」")
    # 自证有牙：从名单里删掉一个真被调用的名字，这条判据必须点名它。
    for probe in ("readContextBudgets", "noteBudgetClamp", "currentProvider"):
        assert gap_of([n for n in METER_SLICE_FUNCS if n != probe]) == {probe}, \
            f"判据没牙：从名单里删掉 {probe} 它还不红"
