# v0.39.x 模型驾驶舱 Task 2：网页上下文预算/校准/分段/轻量整理的纯函数锁。
#
# 判据一律走 node 真跑（同 test_token_estimate_and_budget_truncation_behavior 的理由）：
# 校准比例、残差口径、"丢哪条"这类行为读源码读不出对错，文本断言只能钉形状不能钉语义。
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.test_web_pwa import _fn_text, _js


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
  { id: "m1", max_context_k: 300 },
  { id: "m2", max_context_k: 16 },
], messages: [] };
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
process.stdout.write(JSON.stringify({ before, after, other, cal, segs, tidy }));
"""


def test_context_budget_map_cap_calibration_and_tidy():
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    src = "\n".join(_fn_text(_js(), n) for n in (
        "estimateTokens", "truncateWithin", "contextBudgetStorageKey",
        "readContextBudgets", "contextBudgetK", "setContextBudget",
        "calibrateUsedK", "contextSegments", "dropOldestToBudget"))
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
