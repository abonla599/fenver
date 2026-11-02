# 模型驾驶舱 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在输入卡周围落地「上下文环 + 模型卡 + 接力队列」：忙时发送不丢、上下文可见可整理、模型能力在选模型处管理；思考参数只在真实核实后启用。

**Architecture:** P0a 先做后端 done usage、网页/原生的上下文计量与内存队列；P0b 再加 provider 能力字段、模型卡/管理页和设置页迁移；P0c 用一次显式授权的真实调用核实思考 profile，再开请求映射。网页纯函数留在 `app.js` 并用 Node 真跑；原生用源码形状锁 + `compileDebugKotlin`。

**Tech Stack:** FastAPI + Pydantic + pytest；原生 JS（无框架，`app.js`/`api.js`/`index.html`/`style.css`）；Android Kotlin + Jetpack Compose + kotlinx.serialization；Node 仅用于纯函数测试。

**Spec:** `docs/superpowers/specs/2026-10-31-model-cockpit-design.md`

## Global Constraints

- 实现基线：`develop`（写作时 `2757d308`）。开始前先把 `fix/m8-export-fallback` 与 `perf/package-a` 按发版顺序收口，再从最新 `develop` 切 `feat/model-cockpit`；不要把代码叠在未合并树上。
- 实现工作树：`C:/Users/34426/ai-assistant/.qoder/worktrees/model-cockpit`（若执行环境禁止新建 worktree，使用分配给你的干净树；不碰 `design-model-cockpit` 和主工作树）。
- 测试解释器：`C:/Users/34426/ai-assistant/venv/Scripts/python.exe`（下称 `$PY`）；不要用全局 python。
- 不碰 `data/`、`chroma_db/`、`venv/`、`version.txt`、`.github/workflows/*`；不 push/tag/发版/重启服务。
- 队列首版只存内存：不落 localStorage/Prefs/服务端；重启丢失；每标签页一份。
- 「轻量整理」= 从本轮发送去掉最早 N 条，历史原文不动；深度摘要压缩不在本计划（P2）。
- 网页不新增内联脚本；所有用户/模型文本走 `textContent`/`createElement`，不拼 `innerHTML`。
- 思考参数只通过服务端白名单 profile 发送；未核实/不支持不发参数；禁止任意 JSON/headers。
- 共享模型能力字段仅管理员可写；私有模型属主可写；`can_edit` 只约束 provider 字段，不约束本机预算/思考偏好。
- 每个任务先写失败测试，再实现，再跑聚焦测试；任务结束提交一次。最后跑全量 + Kotlin 编译。
- 不写死会腐烂的测试数字（通过/跳过数按本次实测报告）；不把未跑过的结论写成已确认。

---

## File Structure

**Backend**
- Modify `backend/app/core/stream_events.py`：`done_frame(..., usage=None)`。
- Modify `backend/app/core/streaming.py`：`stream_chat_events(..., usage_out=None)`；P0c 的思考参数映射。
- Modify `backend/app/main.py`：`ChatRequest.thinking_level`；producer 的 `usage_out`/done usage；`/v1/models` 的 `can_edit`；`ProviderRequest` 新字段。
- Modify `backend/app/core/providers.py`：能力字段、校验、catalog/public。
- Modify `backend/app/pipeline.py`：非流式思考参数映射（P0c）。
- Modify `backend/tests/conftest.py`：`text_as_events` 接受 `usage_out`。
- Create tests：`backend/tests/test_v0391_done_usage.py`、`test_v0391_provider_capabilities.py`、`test_v0391_web_context_meter.py`、`test_v0391_web_queue_model.py`、`test_v0391_web_cockpit_ui.py`、`test_v0391_native_cockpit.py`、`test_v0391_thinking_mapping.py`。

**Web**
- Modify `backend/app/web/static/app.js`：预算 map/校准/分段/整理、队列状态、环/账本/接力条、模型卡/管理。
- Modify `backend/app/web/static/index.html`：`#ctxRingBtn`、`#contextPanel`、`#queueStrip`、模型菜单详情容器、模型管理入口；移除设置页上下文滑杆。
- Modify `backend/app/web/static/style.css`：环、账本、接力条、模型卡/管理；沿用 `--fv-*`。
- Modify `backend/app/web/static/api.js`：`thinking_level` 请求字段、done `usage` 透出、provider 能力写口。
- 不新建静态 JS 文件，避免 SW 清单/CSP 多一套登记。

**Native**
- Modify `android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/Api.kt`：ModelInfo 能力字段、Done usage、`thinking_level` 请求、`outbound` 预算入参。
- Modify `.../Prefs.kt`：per-provider 预算 map + 思考偏好 map + 迁移。
- Modify `.../ui/ChatUi.kt`：模型 chip + 环 + 账本；`sendNow` 忙时入队；接力条；done usage；模型卡。
- Modify `.../ui/SettingsUi.kt`：移除上下文滑杆与 provider 能力输入，保留连接配置。
- Test：`backend/tests/test_v0391_native_cockpit.py`（源码形状锁）。

---

## Task 1: Backend done usage

**Files:**
- Modify: `backend/app/core/stream_events.py`
- Modify: `backend/app/core/streaming.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/test_v0391_done_usage.py`

**Interfaces:**
- Produces: `done_frame(full_text, message_id, model, trace=None, usage=None) -> dict`；`stream_chat_events(..., usage_out=None)`。
- Consumes: existing `billed` dict in `stream_chat_events`；existing producer `_produce_stream`。

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_v0391_done_usage.py
import json
from app.core import stream_events as se


def _data_frames(text: str):
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                yield json.loads(line[6:])


def _done(frames):
    return [d for d in _data_frames(frames) if d.get("type") == "done"][-1]


def test_done_frame_usage_is_optional_and_additive():
    bare = se.done_frame("答", "m1", "model-x")
    assert set(bare) == {"type", "full_text", "message_id", "model"}
    full = se.done_frame("答", "m1", "model-x",
                         usage={"prompt_tokens": 12, "completion_tokens": 3,
                                "total_tokens": 15, "reasoning_tokens": 0,
                                "cached_tokens": 0})
    assert full["usage"]["prompt_tokens"] == 12
    assert {k: v for k, v in full.items() if k != "usage"} == bare


def test_stream_done_carries_usage_without_touching_old_fields(client, monkeypatch):
    from app.core import streaming
    from app.core import stream_events as se

    def fake_events(model, messages, **kwargs):
        out = kwargs.get("usage_out")
        assert out is not None, "producer 没把 usage_out 传给引擎"
        out.update({"prompt_tokens": 21, "completion_tokens": 4,
                    "total_tokens": 25, "reasoning_tokens": 0, "cached_tokens": 0})
        yield se.content_frame("好")

    monkeypatch.setattr(streaming, "stream_chat_events", fake_events)
    res = client.post("/v1/chat/stream",
                      json={"messages": [{"role": "user", "content": "hi"}]})
    assert res.status_code == 200, res.text
    done = _done(res.text)
    assert done["usage"]["prompt_tokens"] == 21
    assert done["full_text"] == "好"
    assert done["message_id"] and done["model"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
"$PY" -m pytest backend/tests/test_v0391_done_usage.py -q
```
Expected: FAIL — `done_frame` 不接受 `usage` / `stream_chat_events` 不接受 `usage_out` / producer 没传。

- [ ] **Step 3: Implement the minimal backend change**

`stream_events.py`:
```python
def done_frame(full_text: str, message_id: str, model: str,
               trace: Optional[List[Dict[str, Any]]] = None,
               usage: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    frame: Dict[str, Any] = {"type": FRAME_DONE, "full_text": full_text,
                             "message_id": message_id, "model": model}
    if trace:
        frame["trace"] = trace
    if usage:
        frame["usage"] = {k: int(v or 0) for k, v in usage.items()}
    return frame
```

`streaming.py`:
```python
def stream_chat_events(
    ...,
    trace_out: Optional[List[Dict[str, Any]]] = None,
    usage_out: Optional[Dict[str, int]] = None,
) -> Generator[Dict[str, Any], None, None]:
    ...
    finally:
        if usage_out is not None:
            usage_out.update(billed)
        usage.record_call(...)
```

`main.py` producer：`usage_out` 必须定义在 `_produce_stream` 的 `try:` 之前（cancelled 分支也要用），两处 done 都带：
```python
        usage_out: dict = {}
        for frame in stream_chat_events(..., usage_out=usage_out):
            ...
        run.append({**se.done_frame(full_text, run.message_id, provider["model"],
                                    trace or None, usage=usage_out or None),
                    "status": "completed", "resumable": False})
```
cancelled 分支同理：
```python
        run.append({**se.done_frame(full_text, run.message_id, provider["model"],
                                    trace or None, usage=usage_out or None),
                    "status": "cancelled", ...})
```

`conftest.py` 的 `text_as_events.events` 增加 `usage_out=None` 形参（忽略即可；它只给不接受 usage 的旧桩兜底）。

- [ ] **Step 4: Run focused tests**

Run:
```bash
"$PY" -m pytest backend/tests/test_v0391_done_usage.py backend/tests/test_stream_api.py backend/tests/test_streaming_events.py -q
```
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/stream_events.py backend/app/core/streaming.py backend/app/main.py backend/tests/conftest.py backend/tests/test_v0391_done_usage.py
git commit -m "feat(chat): done 帧回传 usage"
```

---

## Task 2: Web context/budget pure functions

**Files:**
- Modify: `backend/app/web/static/app.js`
- Test: `backend/tests/test_v0391_web_context_meter.py`

**Interfaces:**
- Produces: `contextBudgetStorageKey()`, `readContextBudgets()`, `contextBudgetK(providerId)`, `setContextBudget(providerId, k)`, `migrateContextBudget()`, `calibrateUsedK(clientK, lastPromptK, lastClientK)`, `contextSegments(usedK, parts)`, `dropOldestToBudget(list, budgetTokens)`.
- Consumes: `state.providers` (`max_context_k`), `pref.userId`, `pref.contextTokensK`（只用于迁移）, `estimateTokens`, `truncateWithin`.

- [ ] **Step 1: Write the failing Node test**

```python
# backend/tests/test_v0391_web_context_meter.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
"$PY" -m pytest backend/tests/test_v0391_web_context_meter.py -q
```
Expected: FAIL — functions not defined.

- [ ] **Step 3: Implement the pure functions in `app.js`**

```js
/* v0.39.x 模型驾驶舱：本会话预算按「用户 + provider」存；旧 contextTokensK 只用于迁移。 */
function contextBudgetStorageKey() {
  return "fv.contextBudget." + (pref.userId || "anon") + ".v1";
}

function readContextBudgets() {
  try {
    const raw = JSON.parse(localStorage.getItem(contextBudgetStorageKey()) || "{}");
    return raw && typeof raw === "object" ? raw : {};
  } catch (_) { return {}; }
}

function contextBudgetK(providerId) {
  const p = state.providers.find((x) => x.id === providerId);
  const cap = Math.max(1, Number(p && p.max_context_k) || 64);
  const map = readContextBudgets();
  const raw = map[providerId];
  const fallback = Number(pref.contextTokensK) || 8;
  const wanted = Number.isFinite(Number(raw)) ? Number(raw) : fallback;
  return Math.max(1, Math.min(cap, Math.round(wanted)));
}

function setContextBudget(providerId, k) {
  const p = state.providers.find((x) => x.id === providerId);
  const cap = Math.max(1, Number(p && p.max_context_k) || 64);
  const map = readContextBudgets();
  map[providerId] = Math.max(1, Math.min(cap, Math.round(Number(k) || 8)));
  localStorage.setItem(contextBudgetStorageKey(), JSON.stringify(map));
}

function migrateContextBudget() {
  if (localStorage.getItem(contextBudgetStorageKey()) !== null) return;
  const pid = pref.provider;
  if (!pid) return;
  setContextBudget(pid, Number(pref.contextTokensK) || 8);
  localStorage.removeItem("contextTokensK");
}

function calibrateUsedK(clientK, lastPromptK, lastClientK) {
  const c = Math.max(0, Number(clientK) || 0);
  const lp = Number(lastPromptK) || 0;
  const lc = Number(lastClientK) || 0;
  if (lp <= 0 || lc <= 0) return Math.round(c);
  const ratio = Math.max(1, Math.min(4, lp / lc));
  return Math.round(c * ratio);
}

function contextSegments(usedK, parts) {
  const known = Math.max(0, Number(parts.persona) || 0) +
    Math.max(0, Number(parts.history) || 0) +
    Math.max(0, Number(parts.input) || 0);
  const residual = Math.max(0, Math.round(Number(usedK) || 0) - Math.round(known));
  return { known, residual, parts: { ...parts, residual } };
}

function dropOldestToBudget(list, budgetTokens) {
  const before = (list || []).reduce((n, m) => n + estimateTokens(m.content) + 4, 0);
  const kept = truncateWithin(list || [], Math.max(500, Number(budgetTokens) || 0));
  const droppedCount = Math.max(0, (list || []).length - kept.length);
  return {
    kept, droppedCount,
    keptRoles: kept.map((m) => m.role),
    freedK: Math.max(0, Math.round((before - kept.reduce((n, m) => n + estimateTokens(m.content) + 4, 0)) / 1000)),
  };
}
```

把 `outbound()` 的 `pref.contextTokensK` 改成 `contextBudgetK(pref.provider)`；`syncCtxRow()` 的读/写改成 `contextBudgetK`/`setContextBudget`；`boot()` 里在 `migrateContextPref()` 之后调用 `migrateContextBudget()`。旧 `contextTokensK` 只留给迁移，不再作为运行时读数。

- [ ] **Step 4: Run focused test**

Run:
```bash
"$PY" -m pytest backend/tests/test_v0391_web_context_meter.py backend/tests/test_web_pwa.py -q
```
Expected: PASS（`test_web_pwa` 里旧的 `contextTokensK` 断言若因迁移改写而红，按新口径改成 `contextBudgetK` + 迁移测试，不许删断言）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/web/static/app.js backend/tests/test_v0391_web_context_meter.py
git commit -m "feat(web): 上下文预算按模型存储与校准"
```

---

## Task 3: Web queue state machine

**Files:**
- Modify: `backend/app/web/static/app.js`
- Test: `backend/tests/test_v0391_web_queue_model.py`

**Interfaces:**
- Produces: `queueFor(sessionId)`, `enqueueQueueItem(sessionId, item)`, `nextQueueItem(sessionId)`, `finishQueueItem(sessionId, id)`, `pauseQueue(sessionId, paused)`, `canRelay(sessionId, currentSessionId, streaming, visibility)`, `resetQueuesForIdentity()`.
- Queue item: `{ id, text, attachments, k }`（attachments 是已上传附件的元数据数组）。

- [ ] **Step 1: Write the failing Node test**

```python
# backend/tests/test_v0391_web_queue_model.py
import json, shutil, subprocess, tempfile
from pathlib import Path
import pytest
from tests.test_web_pwa import _fn_text, _js

HARNESS = r"""
const fs=require("fs"), vm=require("vm");
const ctx={}; vm.createContext(ctx);
ctx.state={queues:{}};
vm.runInContext(fs.readFileSync(process.argv[2],"utf8"), ctx);
const q1="s1", q2="s2";
const added = ctx.enqueueQueueItem(q1,{id:"a",text:"A",attachments:[],k:1});
const added2 = ctx.enqueueQueueItem(q1,{id:"b",text:"B",attachments:[],k:2});
const addedOther = ctx.enqueueQueueItem(q2,{id:"c",text:"C",attachments:[],k:1});
const first = ctx.nextQueueItem(q1);
const guardHidden = ctx.canRelay(q1,q1,false,"hidden");
const guardStreaming = ctx.canRelay(q1,q1,true,"visible");
const guardOther = ctx.canRelay(q1,q2,false,"visible");
const guardOk = ctx.canRelay(q1,q1,false,"visible");
ctx.finishQueueItem(q1,"a");
ctx.pauseQueue(q1,true);
const paused = ctx.queueFor(q1).paused;
ctx.resetQueuesForIdentity();
process.stdout.write(JSON.stringify({added,added2,addedOther,first,guardHidden,guardStreaming,guardOther,guardOk,paused,left:ctx.queueFor(q1).items.length}));
"""


def test_queue_state_machine():
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node")
    src = "\n".join(_fn_text(_js(), n) for n in (
        "queueFor","enqueueQueueItem","nextQueueItem","finishQueueItem",
        "pauseQueue","canRelay","resetQueuesForIdentity"))
    d = Path(tempfile.mkdtemp(prefix="model-cockpit-q-"))
    (d/"fns.js").write_text(src, encoding="utf-8")
    (d/"harness.cjs").write_text(HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(d/"harness.cjs"), str(d/"fns.js")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    out = json.loads(r.stdout)
    assert out["added"] and out["added2"] and out["addedOther"]
    assert out["first"]["id"] == "a"
    assert out["guardHidden"] is False and out["guardStreaming"] is False and out["guardOther"] is False
    assert out["guardOk"] is True
    assert out["paused"] is True and out["left"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
"$PY" -m pytest backend/tests/test_v0391_web_queue_model.py -q
```
Expected: FAIL — queue functions missing.

- [ ] **Step 3: Implement the state machine**

```js
/* 首版队列只在内存：state.queues[sessionId] = {items, paused, sendingId}。
   不落 localStorage；每标签页一份；切身份由 resetQueuesForIdentity 清空。 */
function queueFor(sessionId) {
  const key = sessionId || "draft";
  if (!state.queues) state.queues = {};
  if (!state.queues[key]) state.queues[key] = { items: [], paused: false, sendingId: null };
  return state.queues[key];
}

function enqueueQueueItem(sessionId, item) {
  const q = queueFor(sessionId);
  const chars = q.items.reduce((n, x) => n + String(x.text || "").length, 0);
  if (q.items.length >= 20 || chars + String(item.text || "").length > 20000) return false;
  q.items.push(item);
  q.paused = false;
  return true;
}

function nextQueueItem(sessionId) {
  const q = queueFor(sessionId);
  if (q.sendingId) return null;
  const item = q.items.shift() || null;
  if (item) q.sendingId = item.id;
  return item;
}

function finishQueueItem(sessionId, id) {
  const q = queueFor(sessionId);
  if (q.sendingId === id) q.sendingId = null;
}

function pauseQueue(sessionId, paused) {
  queueFor(sessionId).paused = !!paused;
}

function canRelay(sessionId, currentSessionId, streaming, visibility) {
  const q = queueFor(sessionId);
  return !!q.items.length && !q.paused && !streaming &&
    sessionId === currentSessionId && visibility === "visible";
}

function resetQueuesForIdentity() {
  state.queues = {};
}
```

- [ ] **Step 4: Run focused test**

Run:
```bash
"$PY" -m pytest backend/tests/test_v0391_web_queue_model.py -q
```
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/web/static/app.js backend/tests/test_v0391_web_queue_model.py
git commit -m "feat(web): 接力队列内存状态机"
```

---

## Task 4: Web context ring and ledger

**Files:**
- Modify: `backend/app/web/static/index.html`
- Modify: `backend/app/web/static/app.js`
- Modify: `backend/app/web/static/style.css`
- Test: `backend/tests/test_v0391_web_cockpit_ui.py`

**Interfaces:**
- DOM: `#ctxRingBtn`（环按钮）、`#contextPanel`（账本）、`#ctxBudgetRange`、`#ctxTidyBtn`、`#ctxResetBtn`。
- JS: `clientContextK()`, `renderContextRing()`, `renderContextPanel()`, `openContextPanel()`, `closeContextPanel()`, `applyContextTidy()`.
- State: `state.contextDropBySession = {}`（每个会话丢最早几条）；`state.lastUsageBySession = {}`（`{prompt_tokens, clientK, providerId}`）。

- [ ] **Step 1: Write the failing static test**

```python
# backend/tests/test_v0391_web_cockpit_ui.py
import re
from tests.test_web_pwa import _function_body, _html, _js


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
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
"$PY" -m pytest backend/tests/test_v0391_web_cockpit_ui.py -q
```
Expected: FAIL — elements/functions missing.

- [ ] **Step 3: Add markup, styles, and renderers**

`index.html`（把现有 `#modelChip` 包进容器；位置以现有输入卡为准）：
```html
<span class="model-cockpit" id="modelCockpit">
  <button type="button" id="ctxRingBtn" class="ctx-ring-btn" aria-label="上下文用量"></button>
  <button type="button" class="model-chip hidden" id="modelChip" aria-label="切换模型" title="切换模型">
    <span class="chip-name" id="modelChipName"></span><span class="chip-caret">⌄</span>
  </button>
</span>
...
<div id="contextPanel" class="context-panel hidden" role="dialog" aria-label="上下文账本">
  <div id="ctxPanelBody"></div>
</div>
```
`app.js`：
```js
function clientContextK() {
  const drop = state.contextDropBySession[pref.sessionId] || 0;
  const msgs = state.messages.slice(drop).filter((m) => m.content && !m.transient);
  const persona = pref.persona(pref.sessionId) || "";
  const known = (persona ? estimateTokens(persona) + 4 : 0) +
    msgs.reduce((n, m) => n + estimateTokens(m.content) + 4, 0);
  return { known, msgs, persona };
}

function renderContextRing() {
  const cm = currentProvider();
  if (!cm) return;
  const { known } = clientContextK();
  const last = state.lastUsageBySession[pref.sessionId];
  const used = calibrateUsedK(known, last && last.prompt_tokens, last && last.clientK);
  const budget = contextBudgetK(cm.id);
  const pct = Math.min(100, used / Math.max(1, budget) * 100);
  const qK = (state.queues && state.queues[pref.sessionId] || { items: [] }).items
    .reduce((n, x) => n + (Number(x.k) || 0), 0);
  const btn = $("ctxRingBtn");
  const color = used > budget ? "var(--danger)" : pct >= 70 ? "var(--warn)" : "var(--accent)";
  btn.style.setProperty("--ctx-pct", String(pct));
  btn.style.setProperty("--ctx-color", color);
  btn.setAttribute("aria-label", `上下文 ${pct.toFixed(1)}%，${Math.round(used / 1000)}K / ${budget}K；待发 +${qK}K`);
  btn.classList.toggle("warn", pct >= 70 || used > budget);
  btn.classList.toggle("danger", used > budget);
}
```
账本 renderer 只使用 `createElement`/`textContent`；分段用 `contextSegments(used, parts)`；`#ctxTidyBtn` 调 `applyContextTidy()`：
```js
function applyContextTidy() {
  const { known, msgs } = clientContextK();
  const cm = currentProvider();
  if (!cm) return;
  const plan = dropOldestToBudget(msgs, contextBudgetK(cm.id) * 1000);
  state.contextDropBySession[pref.sessionId] =
    (state.contextDropBySession[pref.sessionId] || 0) + plan.droppedCount;
  renderContextPanel();
  renderContextRing();
  setStatus(`已从本轮发送移除最早 ${plan.droppedCount} 条：省 ${plan.freedK}K；历史原文未动`, false);
}
```
`#ctxResetBtn` 清掉 `state.contextDropBySession[pref.sessionId]`。`openContextPanel`/`closeContextPanel` 用现有 `Layers` 模式（参考 `setModelMenu`）或直接 `hidden` + 点击外部关闭；两处保持同一套。

`style.css`：环 24dp，用 conic-gradient 让 `--ctx-pct`/`--ctx-color` 直接驱动：

```css
.ctx-ring-btn{width:24px;height:24px;border-radius:50%;position:relative;padding:0;
  background:conic-gradient(var(--ctx-color,var(--fv-accent)) calc(var(--ctx-pct,0)*1%),
                             var(--fv-surface-3) 0);}
.ctx-ring-btn::after{content:"";position:absolute;inset:4px;border-radius:50%;
  background:var(--fv-surface);}
.ctx-ring-btn.warn{--ctx-color:var(--fv-warn,var(--fv-danger));}
.ctx-ring-btn.danger{--ctx-color:var(--fv-danger);}
```
账本沿用 `.fv-card` 的 surface/line/16 圆角；分段条 10px 高、5 段；移动端全宽。不要新增第三强调色。

- [ ] **Step 4: Run focused tests + manual check**

Run:
```bash
"$PY" -m pytest backend/tests/test_v0391_web_cockpit_ui.py backend/tests/test_web_pwa.py -q
node --check backend/app/web/static/app.js
```
Expected: PASS。手动：打开 `/app`，点环，确认账本出现；调低预算看到警示；点整理后环与分段下降、历史消息仍在。

- [ ] **Step 5: Commit**

```bash
git add backend/app/web/static/index.html backend/app/web/static/app.js backend/app/web/static/style.css backend/tests/test_v0391_web_cockpit_ui.py
git commit -m "feat(web): 上下文环与账本"
```

---

## Task 5: Web relay queue UI and auto-send

**Files:**
- Modify: `backend/app/web/static/index.html`
- Modify: `backend/app/web/static/app.js`
- Modify: `backend/app/web/static/style.css`
- Test: `backend/tests/test_v0391_web_cockpit_ui.py`（追加用例）

**Interfaces:**
- DOM: `#queueStrip`、`#queueList`、`#queuePauseBtn`、`#queueCount`。
- JS: `renderQueueStrip()`, `queueCardNode(item)`, `maybeRelay(result, sidAtSend, userAtSend)`, `queueShowNow(item)`, `clearQueue()`。
- `send(text)` 在 `state.streaming` 时改为入队；`streamInto` 捕获 `sidAtSend`/`epochAtSend`，`await runStream` 后调 `maybeRelay`；`stop()` 暂停；`visibilitychange` 暂停；身份切换清空。

- [ ] **Step 1: Write failing tests (append to Task 4 test file)**

```python
def test_relay_queue_ui_and_auto_send_guards():
    html = _html()
    js = _js()
    assert 'id="queueStrip"' in html and 'id="queueList"' in html
    assert 'id="queuePauseBtn"' in html
    send = _function_body(js, "send")
    assert "enqueueQueueItem(" in send and "已加入接力队列" in send, \
        "忙时发送没有入队"
    relay = _function_body(js, "maybeRelay")
    assert "canRelay(" in relay, "自动接力没有会话/前台/暂停闸"
    assert "setTimeout(" in relay, "自动接力没有给落定一拍留时间"
    assert "finishQueueItem(" in relay, "发送中条目没有释放"
    assert "resetQueuesForIdentity()" in _function_body(js, "resetViewForIdentity"), \
        "换身份没清队列"
    assert "pauseQueue(" in _function_body(js, "stop"), "停止没有暂停队列"
    assert "visibilitychange" in js, "没有前台/后台队列闸"
    card = _function_body(js, "queueCardNode")
    assert "textContent" in card and "innerHTML" not in card, \
        "队列卡对用户文本拼了 innerHTML"
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
"$PY" -m pytest backend/tests/test_v0391_web_cockpit_ui.py -q
```
Expected: FAIL — queue DOM/functions missing.

- [ ] **Step 3: Implement queue DOM + send/relay hooks**

`index.html` 在输入卡上方插入：
```html
<div id="queueStrip" class="queue-strip hidden" aria-label="接力队列">
  <div class="queue-head"><span id="queueCount">0 条待发送</span>
    <button type="button" id="queuePauseBtn">暂停</button></div>
  <div id="queueList"></div>
</div>
```
`app.js`：
```js
function queueCardNode(item) {
  const row = document.createElement("div");
  row.className = "queue-card";
  const text = document.createElement("div");
  text.className = "queue-text";
  text.textContent = item.text;          // 用户原文只进 textContent
  const meta = document.createElement("div");
  meta.className = "queue-meta";
  meta.textContent = `+${Number(item.k) || 0}K`;
  const edit = document.createElement("button"); edit.textContent = "编辑";
  edit.onclick = () => {
    const ta = document.createElement("textarea");
    ta.className = "queue-edit";
    ta.value = item.text;
    const save = document.createElement("button"); save.textContent = "保存";
    save.onclick = () => {
      item.text = ta.value.trim() || item.text;
      renderQueueStrip(); renderContextRing();
    };
    const cancel = document.createElement("button"); cancel.textContent = "取消";
    cancel.onclick = renderQueueStrip;
    text.replaceWith(ta);
    meta.after(save, cancel);
  };
  const del = document.createElement("button"); del.textContent = "删除";
  del.onclick = () => { const q = queueFor(pref.sessionId); q.items = q.items.filter((x) => x.id !== item.id); renderQueueStrip(); renderContextRing(); };
  const now = document.createElement("button"); now.textContent = "立即插话";
  now.onclick = () => queueShowNow(item);
  row.append(text, meta, edit, del, now);
  return row;
}

function renderQueueStrip() {
  const q = queueFor(pref.sessionId);
  $("#queueStrip").classList.toggle("hidden", q.items.length === 0);
  $("#queueCount").textContent = `${q.items.length} 条待发送`;
  $("#queuePauseBtn").textContent = q.paused ? "继续" : "暂停";
  const host = $("#queueList"); host.textContent = "";
  q.items.forEach((it) => host.appendChild(queueCardNode(it)));
}

function maybeRelay(result, sidAtSend, userAtSend) {
  const q = queueFor(sidAtSend);
  if (!result || !result.normal || pref.sessionId !== sidAtSend ||
      pref.userId !== userAtSend || document.visibilityState !== "visible") {
    q.paused = true; renderQueueStrip(); return;
  }
  setTimeout(() => {
    if (state.streaming || pref.sessionId !== sidAtSend || pref.userId !== userAtSend ||
        !canRelay(sidAtSend, pref.sessionId, state.streaming, document.visibilityState)) return;
    const item = nextQueueItem(sidAtSend);
    if (!item) return;
    state.pending = item.attachments || [];
    send(item.text).finally(() => finishQueueItem(sidAtSend, item.id));
  }, 300);
}
```
`send()` 忙时：
```js
  if (state.streaming) {
    const item = { id: `q_${Date.now()}_${Math.random().toString(16).slice(2)}`,
                   text, attachments: state.pending.slice(), k: Math.round(estimateTokens(text) / 1000) + 1 };
    if (!enqueueQueueItem(pref.sessionId, item)) { setStatus("接力队列已满，先发掉一条或清空", true); return; }
    clearPending(); $("input").value = ""; updateSendEnabled();
    renderQueueStrip(); renderContextRing(); setStatus("已加入接力队列");
    return;
  }
```
`streamInto`：
```js
  const sidAtSend = pref.sessionId;
  const userAtSend = pref.userId;
  const result = await runStream(holder);
  maybeRelay(result, sidAtSend, userAtSend);
```
`runStream` 末尾返回 `{ normal: !failed && !aborted && !resumed }`；`cannot_resume` 分支置 `resumed=true`；error/abort 分支 normal=false。`stop()` 里 `pauseQueue(pref.sessionId, true)`；`window.addEventListener("visibilitychange", ...)` 在 hidden 时暂停并 render；`resetViewForIdentity` 调 `resetQueuesForIdentity()`。

立即插话：
```js
function queueShowNow(item) {
  if (!window.confirm("会停止当前回答（已生成部分保留），并立即发送这条消息。")) return;
  const q = queueFor(pref.sessionId);
  q.items = q.items.filter((x) => x.id !== item.id);
  state.forceNext = { sid: pref.sessionId, user: pref.userId, item };
  renderQueueStrip();
  stop();
}
```
`streamInto` 在 `runStream` 返回后先消费 `forceNext`，否则走 `maybeRelay`：
```js
if (state.forceNext && state.forceNext.sid === sidAtSend &&
    state.forceNext.user === userAtSend) {
  const f = state.forceNext;
  state.forceNext = null;
  state.pending = f.item.attachments || [];
  await send(f.item.text);
} else {
  maybeRelay(result, sidAtSend, userAtSend);
}
```
剩余队列保持 `paused=true`（`stop()` 已置）。

- [ ] **Step 4: Run focused tests + Node syntax**

Run:
```bash
"$PY" -m pytest backend/tests/test_v0391_web_cockpit_ui.py backend/tests/test_v0391_web_queue_model.py backend/tests/test_web_pwa.py -q
node --check backend/app/web/static/app.js
```
Expected: PASS。手动：忙时发送入队；模拟/等当前回答结束后自动发；点停止队列暂停；切会话队列不串；刷新页面队列消失。

- [ ] **Step 5: Commit**

```bash
git add backend/app/web/static/index.html backend/app/web/static/app.js backend/app/web/static/style.css backend/tests/test_v0391_web_cockpit_ui.py
git commit -m "feat(web): 接力队列与自动发送"
```

---

## Task 6: Native context budget, done usage, ring/panel

**Files:**
- Modify: `android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/Prefs.kt`
- Modify: `.../Api.kt`
- Modify: `.../ui/ChatUi.kt`
- Test: `backend/tests/test_v0391_native_cockpit.py`

**Interfaces:**
- `Prefs.contextBudgetK(providerId): Int`、`Prefs.setContextBudget(providerId, k)`、`Prefs.migrateContextBudget()`。
- `Api.ModelInfo` 新增 `max_context_k` 已有；`Api.ChatEvent.Done` 新增 `usage: Usage?`；`Api.outbound(history, persona, budgetK, modelCapK)`。
- `ChatUi`：`lastUsage`/`lastClientK`（按会话+provider）、`clientContextK()`、`ContextRing`/`ContextPanel`。

- [ ] **Step 1: Write failing source-shape tests**

```python
# backend/tests/test_v0391_native_cockpit.py
from pathlib import Path
from tests.test_apk_release_discipline import _code

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp"


def _t(rel): return (NATIVE / rel).read_text(encoding="utf-8")
def _c(rel): return _code(NATIVE / rel)
def _block(code, anchor):
    at = code.index(anchor); i = code.index("{", at); d = 0
    for j in range(i, len(code)):
        if code[j] == "{": d += 1
        elif code[j] == "}":
            d -= 1
            if d == 0: return code[i:j + 1]
    raise AssertionError(anchor)


def test_prefs_budget_map_and_migration():
    c = _c("Prefs.kt")
    assert "fun contextBudgetK(providerId: String)" in c
    assert "fun setContextBudget(providerId: String" in c
    assert "fun migrateContextBudget()" in c
    assert "context_tokens_k" in c, "旧键迁移没有读 source"


def test_done_usage_and_budget_reach_outbound():
    api = _c("Api.kt")
    assert "data class Usage(" in api
    assert "val usage: Usage? = null" in _block(api, "data class Done(")
    ob = _block(api, "fun outbound(")
    assert "budgetK" in ob
    chat = _c("ui/ChatUi.kt")
    assert "Prefs.contextBudgetK(" in chat, "原生发送没走 per-model 预算"
    assert "calibrateUsedK" in chat, "原生没做 usage 校准"
```

- [ ] **Step 2: Run to verify they fail**

```bash
"$PY" -m pytest backend/tests/test_v0391_native_cockpit.py -q
```
Expected: FAIL — symbols missing.

- [ ] **Step 3: Implement `Prefs`, `Api`, `ChatUi`**

`Prefs.kt`：
```kotlin
private fun budgetMapKey() = "context_budget_map"
private fun budgetMap(): MutableMap<String, Int> =
    runCatching { json.decodeFromString<Map<String, Int>>(sp.getString(budgetMapKey(), "{}") ?: "{}") }
        .getOrNull()?.toMutableMap() ?: mutableMapOf()

fun contextBudgetK(providerId: String): Int {
    val cap = 10000 // 由调用方再与 ModelInfo.max_context_k 取 min
    return budgetMap()[providerId]?.coerceIn(1, cap) ?: contextTokensK.coerceIn(1, cap)
}

fun setContextBudget(providerId: String, k: Int) {
    if (providerId.isEmpty()) return
    val map = budgetMap(); map[providerId] = k.coerceIn(1, 10000)
    sp.edit().putString(budgetMapKey(), json.encodeToString(map)).apply()
}

fun migrateContextBudget() {
    if (sp.contains(budgetMapKey())) return
    val pid = defaultProviderId
    if (pid.isNotEmpty()) setContextBudget(pid, contextTokensK)
    sp.edit().remove("context_tokens_k").apply()
}
```
`Api.kt`：
```kotlin
@Serializable data class Usage(val prompt_tokens: Int = 0, val completion_tokens: Int = 0,
                               val total_tokens: Int = 0, val reasoning_tokens: Int = 0,
                               val cached_tokens: Int = 0)
data class Done(..., val usage: Usage? = null) : ChatEvent()
```
解析 done 的 `usage` 对象（`Api.kt` 的 Done 分支）；`outbound` 的形参名改 `budgetK`（语义不变）。
`ChatUi.kt`：
- `val lastUsageByKey = mutableStateMapOf<String, Usage>()`；key `${sessionId}:${providerId}`。
- `clientContextK`：用 `Api.outbound` 的同一套 `estimateTokens`（把 `estimateTokens` 在 `Api` 内 public/internal 或复制一份；优先复用）。
- `outboundMsgs()` 用 `Prefs.contextBudgetK(pid)` 且 `modelCapK()`；在发送前记录 `lastClientK`。
- `ChatEvent.Done` 分支：`ev.usage?.let { lastUsageByKey[key] = it }`。
- 输入卡模型按钮旁加 `ContextRing`（Canvas 画弧，24dp，`contentDescription`）；点开 `ContextPanel`（Dialog/底部弹层）。面板展示百分比、客户端估算/实测来源、分段（角色/历史/输入/待发）、预算滑杆、`轻量整理`（把 `contextDropBySession[sessionId]` 增加最早 N 条，历史不动）、`恢复`。
- 环与面板的百分比使用 `calibrateUsedK(clientK, last.prompt_tokens, lastClientK)` 的等价 Kotlin 纯函数；把它放在 `Api` 或 `ChatUi` 可测的 internal 函数里。

- [ ] **Step 4: Run focused tests + Kotlin compile**

```bash
"$PY" -m pytest backend/tests/test_v0391_native_cockpit.py -q
```
Kotlin（用本机已备的 JDK17/gradle）：
```bash
JAVA_HOME=C:/Users/34426/dl/perf-package-a/jdk17x/jdk-17.0.20.1+1 \
  C:/Users/34426/dl/perf-package-a/gradlex/gradle-8.7/bin/gradle.bat \
  -p android-native :app:compileDebugKotlin
```
Expected: tests PASS；`BUILD SUCCESSFUL`。若工具链不可用，报告「无法证实编译」，不许写绿。

- [ ] **Step 5: Commit**

```bash
git add android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/Prefs.kt android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/Api.kt android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/ChatUi.kt backend/tests/test_v0391_native_cockpit.py
git commit -m "feat(android): 上下文预算、usage 与上下文环"
```

---

## Task 7: Native relay queue

**Files:**
- Modify: `.../ui/ChatUi.kt`
- Test: `backend/tests/test_v0391_native_cockpit.py`（追加）

**Interfaces:**
- `data class QueuedMsg(id, text, attachments, k)`。
- `queues: Map<String, MutableList<QueuedMsg>>`、`queuePaused: Map<String, Boolean>`、`sendingQueueId`。
- `sendNow` 忙时入队；`streamInto` 末尾 `maybeRelay(sidAtSend, epochAtSend, normal)`；`stop` 暂停；`openSession`/换身份按会话/身份隔离。

- [ ] **Step 1: Write failing source-shape test**

```python
def test_native_relay_queue_state_and_guards():
    c = _c("ui/ChatUi.kt")
    assert "data class QueuedMsg(" in c
    send = _block(c, "fun sendNow(text: String) {")
    assert "if (busy) {" in send and "queues[" in send, "忙时发送没入队"
    assert "queuePaused[" in c and "canRelay" in c, "队列没有暂停/会话/前台闸"
    assert "maybeRelay(" in c, "流结束没有接力入口"
    assert "resetQueuesForIdentity" in c or "queues.clear()" in c, "换身份没清队列"
```

- [ ] **Step 2: Run to verify fail**

```bash
"$PY" -m pytest backend/tests/test_v0391_native_cockpit.py -q
```
Expected: FAIL — queue symbols missing.

- [ ] **Step 3: Implement native queue**

- `sendNow` 首行从 `if (busy) return` 改为：忙时把 `t` + `pending.toList()` 入当前会话队列，清空输入/pending，刷新队列条并返回；队列满 20 条/20k 字时给状态提示。
- `streamInto` 在 `finally` 之后、同一个 job 内：`maybeRelay(sidAtSend, epochAtSend, normal)`；`normal` 只在正常 completed 时为真，stop/error/cannot_resume/断线取回为假；`maybeRelay` 检查 `mine()`、前台、队列未暂停、无 `sendingQueueId`，然后 `delay(300)`，取队首、设 `sendingQueueId`，调用 `sendNow(item.text)`（先把 `pending = item.attachments`），最后清 `sendingQueueId`。
- 接力条：输入卡上方 `QueueStrip` composable，显示预览（`Text`，不拼 HTML）、模型/`+XK`、编辑（行内 `BasicTextField`）、删除、立即插话（确认后 `stop()` + 设 `forceNext`）。`stop()` 设 `queuePaused[currentSession]=true`。
- 失败/停止：`maybeRelay` 设 paused；UI 显示「继续队列」；恢复时若空闲再接力。
- 刷新/重启：队列只在 `remember` 的 map 中，不落盘；不新增持久化。

- [ ] **Step 4: Run focused tests + compile**

```bash
"$PY" -m pytest backend/tests/test_v0391_native_cockpit.py -q
JAVA_HOME=C:/Users/34426/dl/perf-package-a/jdk17x/jdk-17.0.20.1+1 \
  C:/Users/34426/dl/perf-package-a/gradlex/gradle-8.7/bin/gradle.bat \
  -p android-native :app:compileDebugKotlin
```
Expected: PASS + BUILD SUCCESSFUL（或如实报告编译未证实）。

- [ ] **Step 5: Commit**

```bash
git add android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/ChatUi.kt backend/tests/test_v0391_native_cockpit.py
git commit -m "feat(android): 接力队列"
```

---

## Task 8: Provider capability schema (backend)

**Files:**
- Modify: `backend/app/core/providers.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_v0391_provider_capabilities.py`

**Interfaces:**
- Provider record new fields: `supports_thinking: bool`, `thinking_levels: list[str]`, `thinking_default: str`, `thinking_profile: str`.
- `ProviderRequest` new optional fields; `_validate` clamps; `catalog`/`_public` add fields + `thinking_status`; `/v1/models` adds `can_edit`.
- `_can_edit_provider(principal, item) -> bool`（admin 或私有属主）。
- `THINKING_PROFILES` whitelist initially only `"none"`; P0c adds verified profiles.

- [ ] **Step 1: Write failing tests**

```python
# backend/tests/test_v0391_provider_capabilities.py
import json
import pytest
from app.core.providers import ProviderStore, ProviderError


def _store(tmp_path, items):
    p = tmp_path / "providers.json"
    p.write_text(json.dumps(items), encoding="utf-8")
    return ProviderStore(path=str(p))


def _old(**over):
    base = {"id": "p1", "label": "旧模型", "base_url": "https://1.1.1.1/v1",
            "api_key": "sk-test-123456",  # secret-scan:allow 测试假密钥
            "model": "m", "supports_vision": False, "is_default": True}
    base.update(over)
    return base


def test_old_records_get_safe_defaults_and_unverified_status(tmp_path):
    s = _store(tmp_path, [_old()])
    e = s.catalog()[0]
    assert e["supports_thinking"] is False and e["thinking_levels"] == []
    assert e["thinking_default"] == "" and e["thinking_status"] == "unsupported"


def test_thinking_fields_validate_and_old_client_put_preserves_them(tmp_path):
    s = _store(tmp_path, [_old()])
    s.upsert({**_old(), "supports_thinking": True,
              "thinking_levels": ["快速", "均衡"], "thinking_default": "均衡"})
    saved = s.get("p1")
    assert saved["thinking_levels"] == ["快速", "均衡"]
    # 旧客户端 PUT 不带新字段：必须保留，不许静默洗成 false
    s.upsert({**_old(), "label": "改名"})
    assert s.get("p1")["supports_thinking"] is True
    assert s.get("p1")["thinking_levels"] == ["快速", "均衡"]


@pytest.mark.parametrize("bad", [
    {"thinking_levels": ["a", "b", "c", "d"]},
    {"supports_thinking": True, "thinking_levels": ["快速"], "thinking_default": "均衡"},
    {"supports_thinking": False, "thinking_profile": "nope"},
])
def test_invalid_capabilities_are_rejected(tmp_path, bad):
    s = _store(tmp_path, [_old()])
    with pytest.raises(ProviderError):
        s.upsert({**_old(), **bad})
```

- [ ] **Step 2: Run to verify fail**

```bash
"$PY" -m pytest backend/tests/test_v0391_provider_capabilities.py -q
```
Expected: FAIL — fields/defaults/validation missing.

- [ ] **Step 3: Implement provider fields**

`providers.py`：
```python
DEFAULT_THINKING_PROFILE = "none"
# P0b 只开 none；P0c 核实后把 verified profile 加进来。
THINKING_PROFILES = {"none": {"label": "不使用思考", "verified": True}}


def _clean_levels(value) -> list:
    if value is None: return []
    seq = value if isinstance(value, (list, tuple)) else []
    out = []
    for x in seq[:3]:
        t = str(x or "").strip()[:16]
        if t and t not in out: out.append(t)
    return out


def _thinking_status(supports: bool, profile: str) -> str:
    if not supports: return "unsupported"
    return "verified" if profile != DEFAULT_THINKING_PROFILE else "unverified"
```
在 `_validate` 的返回 dict 加：
```python
supports_thinking = bool(record.get("supports_thinking"))
levels = _clean_levels(record.get("thinking_levels"))
default = str(record.get("thinking_default") or "").strip()
if default not in levels: default = levels[0] if levels else ""
profile = str(record.get("thinking_profile") or DEFAULT_THINKING_PROFILE).strip()
if profile not in THINKING_PROFILES: raise ProviderError("未知的思考参数配置")
if not supports_thinking and profile != DEFAULT_THINKING_PROFILE:
    raise ProviderError("不支持思考的模型不能配置思考参数")
...
"supports_thinking": supports_thinking,
"thinking_levels": levels,
"thinking_default": default,
"thinking_profile": profile,
```
`upsert` 更新分支保留未提供的新字段（与 pricing 同一条纪律）：
```python
for key in ("supports_thinking", "thinking_levels", "thinking_default", "thinking_profile"):
    if key not in record:
        cleaned[key] = self._items[existing].get(key, cleaned[key])
```
**PUT 路由必须用 `req.model_dump(exclude_none=True)`**（与现有 pricing 同一条）；否则旧客户端把 Optional 字段序列化成 `None`，上面这段保留逻辑会被绕过，能力被静默洗掉。
`catalog`/`_public` 加：
```python
"supports_thinking": bool(p.get("supports_thinking")),
"thinking_levels": _clean_levels(p.get("thinking_levels")),
"thinking_default": str(p.get("thinking_default") or ""),
"thinking_status": _thinking_status(bool(p.get("supports_thinking")),
                                    str(p.get("thinking_profile") or "none")),
```
`_public` 还加 `thinking_profile`（管理表单需要）；普通 `/v1/models` 不加 profile。

`main.py`：
```python
class ProviderRequest(BaseModel):
    ...
    supports_thinking: Optional[bool] = None
    thinking_levels: Optional[list[str]] = None
    thinking_default: Optional[str] = None
    thinking_profile: Optional[str] = None
```
`/v1/models`：
```python
def _can_edit_provider(principal, item):
    return principal.role == "admin" or not item.get("shared")

models = []
for item in provider_store.catalog(principal.user_id):
    item["can_edit"] = _can_edit_provider(principal, item)
    models.append(item)
return {"models": models, ...}
```

- [ ] **Step 4: Run focused tests**

```bash
"$PY" -m pytest backend/tests/test_v0391_provider_capabilities.py backend/tests/test_providers.py -q
```
Expected: PASS；旧的 `/v1/models` 精确字段断言若红，按新增字段更新，不许删断言。

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/providers.py backend/app/main.py backend/tests/test_v0391_provider_capabilities.py
git commit -m "feat(providers): 思考能力字段与 can_edit"
```

---

## Task 9: Web model card and management + settings migration

**Files:**
- Modify: `backend/app/web/static/index.html`
- Modify: `backend/app/web/static/app.js`
- Modify: `backend/app/web/static/style.css`
- Modify: `backend/app/web/static/api.js`
- Test: `backend/tests/test_v0391_web_cockpit_ui.py`（追加）

**Interfaces:**
- Model menu row shows `thinking_status` chip, `max_context_k`, `can_edit`.
- `#modelMenu` becomes two-pane container; detail panel `#modelDetail`; footer `#modelManageBtn`.
- `API.updateProviderCapabilities(scope, id, patch)` (web helper) PUTs the existing provider payload plus fields.
- Settings page removes `#ctxRange` row and `#provCtxK` input; model service form keeps connection fields.
- Context panel budget slider is the only user-facing budget control (plus card).

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run to verify fail**

```bash
"$PY" -m pytest backend/tests/test_v0391_web_cockpit_ui.py -q
```
Expected: FAIL.

- [ ] **Step 3: Implement model menu/card and move settings**

- `index.html`：`#modelMenu` 内加 `<div id="modelList"></div><div id="modelDetail"></div><button id="modelManageBtn">模型管理</button>`；删除设置页 `ctxRange` 行与 `provCtxK` 字段。模型详情用 `document.createElement`/`textContent` 渲染，不用 `innerHTML`。
- `app.js`：`renderModelChip()` 改成分组并渲染 usable 模型行（名字/共享/`thinking_status`/`maxK`/倍率），点击 `switchModel`；新增 `renderModelDetail()` 显示模型卡（思考开关/档位、模型上限、本会话预算、设为本轮、管理此模型）；桌面用 CSS 两栏 + hover，手机用 `@media` 展开。
- 每用户思考偏好：`state.thinkingPref` 不再另开一份内存真相；用下面两个函数读/写 localStorage（键 `fv.thinking.<userId>.v1`），默认 `thinking_default`，不可用时回落第一个档；`unsupported/unverified` 时开关禁用。
```js
function thinkingPrefsKey() {
  return "fv.thinking." + (pref.userId || "anon") + ".v1";
}
function readThinkingPrefs() {
  try { return JSON.parse(localStorage.getItem(thinkingPrefsKey()) || "{}") || {}; }
  catch (_) { return {}; }
}
function writeThinkingPref(providerId, patch) {
  const all = readThinkingPrefs();
  all[providerId] = { ...(all[providerId] || {}), ...patch };
  localStorage.setItem(thinkingPrefsKey(), JSON.stringify(all));
}
```
- `api.js`：`updateProviderCapabilities(scope, id, patch)` 复用现有 `/v1/providers/{id}` 或 `/v1/me/providers/{id}` PUT；只允许 admin/`can_edit`。
- 设置页模型服务表单保留 label/base_url/api_key/model/supports_vision/paid_by/pricing/测试/默认；能力/上限提示「在模型选择里管理」。
- 样式：模型行 52dp、详情卡 16 圆角、能力 chip 用 `--fv-*`；桌面 hover 只换详情，不重新渲染整页；手机点行展开。

- [ ] **Step 4: Run focused tests + Node syntax**

```bash
"$PY" -m pytest backend/tests/test_v0391_web_cockpit_ui.py backend/tests/test_v0391_web_context_meter.py backend/tests/test_web_pwa.py -q
node --check backend/app/web/static/app.js
```
Expected: PASS。手动：桌面 hover 模型行右侧详情切换；共享模型能力只读（普通用户）；自己的模型可改；设置页不再有上下文滑杆/上限输入。

- [ ] **Step 5: Commit**

```bash
git add backend/app/web/static/index.html backend/app/web/static/app.js backend/app/web/static/style.css backend/app/web/static/api.js backend/tests/test_v0391_web_cockpit_ui.py
git commit -m "feat(web): 模型卡/管理与设置页迁移"
```

---

## Task 10: Native model card/management + settings migration

**Files:**
- Modify: `.../Api.kt`
- Modify: `.../ui/ChatUi.kt`
- Modify: `.../ui/SettingsUi.kt`
- Test: `backend/tests/test_v0391_native_cockpit.py`（追加）

**Interfaces:**
- `Api.ModelInfo` 加 `supports_thinking`, `thinking_levels: List<String>`, `thinking_default`, `thinking_status`, `can_edit`.
- `ModelPickerScreen` 接收 `List<ModelInfo>`；详情卡显示三态/档位/上限/预算；共享普通用户只读。
- `Prefs` 思考偏好 map（per user+provider）。
- `SettingsUi` 移除 `CtxRow`、`provCtxK` 字段；模型服务保留连接配置。

- [ ] **Step 1: Write failing source-shape test**

```python
def test_native_model_card_and_settings_move():
    api = _c("Api.kt")
    mi = _block(api, "data class ModelInfo(")
    for key in ("supports_thinking", "thinking_levels", "thinking_default",
                "thinking_status", "can_edit"):
        assert key in mi, f"ModelInfo 缺 {key}"
    chat = _c("ui/ChatUi.kt")
    assert "ModelPickerScreen(" in chat
    assert "thinking_status" in _block(chat, "private fun ModelPickerScreen("), \
        "模型卡没渲染思考三态"
    assert "Prefs.contextBudgetK(" in chat
    settings = _c("ui/SettingsUi.kt")
    assert "private fun CtxRow(" not in settings, "设置页还在重复上下文滑杆"
    assert "fCtxK" not in settings, "模型服务表单还在重复模型上限"
```

- [ ] **Step 2: Run to verify fail**

```bash
"$PY" -m pytest backend/tests/test_v0391_native_cockpit.py -q
```
Expected: FAIL.

- [ ] **Step 3: Implement native model card and move settings**

- `Api.ModelInfo` 加字段（`@Serializable` 默认值；旧服务端缺字段不崩）。
- `ChatUi.kt` 的 `ModelPickerScreen` 改为接收 `List<ModelInfo>`；调用处传 `providers.filter { it.usable }` 而不是 `Pair`。行内显示名字/共享/思考状态/上限/倍率；点行展开详情卡（思考开关+档位、模型上限滑杆（`can_edit` 才可改）、本会话预算滑杆、`设为本轮模型`、`管理此模型`）。桌面/手机同一 Compose 布局，手机用底部弹层/全屏卡。
- 思考偏好：`Prefs.thinkingEnabled(providerId)`/`thinkingLevel(providerId)`/setters，默认 `thinking_default`，不可用时回落第一档；只有 `thinking_status=="verified"` 可操作。
- `SettingsUi.kt` 删除 `CtxRow` 与模型服务表单的 `fCtxK`；连接配置保留；上下文/能力改由模型卡管理。
- 能力写口：使用现有 `Api` provider PUT 路径；`can_edit` 为 false 时 UI 只读，服务端仍二次校验。

- [ ] **Step 4: Run focused tests + compile**

```bash
"$PY" -m pytest backend/tests/test_v0391_native_cockpit.py -q
JAVA_HOME=C:/Users/34426/dl/perf-package-a/jdk17x/jdk-17.0.20.1+1 \
  C:/Users/34426/dl/perf-package-a/gradlex/gradle-8.7/bin/gradle.bat \
  -p android-native :app:compileDebugKotlin
```
Expected: PASS + BUILD SUCCESSFUL（或如实报告编译未证实）。

- [ ] **Step 5: Commit**

```bash
git add android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/Api.kt android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/Prefs.kt android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/ChatUi.kt android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/SettingsUi.kt backend/tests/test_v0391_native_cockpit.py
git commit -m "feat(android): 模型卡/管理与设置页迁移"
```

---

## Task 11 (GATED): Live thinking-profile verification

> **DO NOT RUN WITHOUT EXPLICIT USER AUTHORIZATION FOR ONE SMALL PAID CALL.**
> This task is blocked until the user says something equivalent to「授权核实思考参数」.
> If not authorized: leave profiles as `none`, UI shows `unverified`, no parameter is sent. Skip to Task 12? No — Task 12 depends on a verified profile; it stays blocked too.

**Files:**
- Create: `tools/probe_thinking_profile.py`
- Create: `backend/app/core/thinking_profiles.py`（只放候选探针映射；生产白名单仍是 Task 12）
- Test: `backend/tests/test_v0391_thinking_mapping.py` (only the non-live parts)

**Interfaces:**
- Probe prints only: `provider_id`, `profile`, `level`, `accepted`, `has_reasoning_content`, `reasoning_tokens`, `error_class`; never prints keys, Authorization, user content, or full URLs with query.
- Exit 0 only when accepted and (reasoning_content or reasoning_tokens > 0); otherwise exit 1 with a safe reason.

- [ ] **Step 1: Write the probe skeleton + a dry-run test**

```python
# tools/probe_thinking_profile.py
"""核实用一次极小调用验证思考参数；不打印密钥/用户内容。
用法：python tools/probe_thinking_profile.py --provider <id> --profile <name> --level <label>
"""
import argparse, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from app.core import providers, streaming

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--level", required=True)
    args = ap.parse_args()
    p = providers.store.resolve(args.provider)
    # 只发最小 prompt；候选映射住在 probe_kwargs，生产白名单要到 Task 12 才开。
    from app.core.thinking_profiles import probe_kwargs
    kwargs = probe_kwargs(args.profile, args.level)
    try:
        client = providers.build_client(p, timeout=30.0, max_retries=0)
        r = client.chat.completions.create(
            model=p["model"], messages=[{"role": "user", "content": "ping"}],
            max_tokens=8, stream=False, **kwargs)
        text = (r.choices[0].message.content or "") if r.choices else ""
        usage = getattr(r, "usage", None)
        rt = getattr(usage, "completion_tokens_details", None)
        reasoning = getattr(rt, "reasoning_tokens", 0) if rt else 0
        has_reason = bool(getattr(r.choices[0].message, "reasoning_content", None)) if r.choices else False
        print(json.dumps({"provider_id": p["id"], "profile": args.profile,
                          "level": args.level, "accepted": True,
                          "has_reasoning_content": has_reason,
                          "reasoning_tokens": int(reasoning or 0),
                          "error_class": ""}, ensure_ascii=False))
        return 0 if (has_reason or int(reasoning or 0) > 0) else 1
    except Exception as e:
        print(json.dumps({"provider_id": p["id"], "profile": args.profile,
                          "level": args.level, "accepted": False,
                          "has_reasoning_content": False, "reasoning_tokens": 0,
                          "error_class": type(e).__name__}, ensure_ascii=False))
        return 1

if __name__ == "__main__":
    sys.exit(main())
```
`backend/app/core/thinking_profiles.py`（本任务只建候选探针表；生产 `PROFILES` 在 Task 12）：
```python
PROBE_PROFILES = {
    "openai_reasoning_effort": {
        "levels": ["快速", "均衡", "深入"],
        "kwargs": lambda level: {"reasoning_effort":
            {"快速": "low", "均衡": "medium", "深入": "high"}[level]},
    },
    "dashscope_enable_thinking": {
        "levels": ["快速", "均衡", "深入"],
        "kwargs": lambda level: {"extra_body": {"enable_thinking": True}},
    },
}

def probe_kwargs(profile: str, level: str) -> dict:
    p = PROBE_PROFILES.get(profile)
    if not p:
        raise ValueError("未知的候选 profile")
    if level not in p["levels"]:
        raise ValueError("未知的思考档位")
    return dict(p["kwargs"](level))
```
Dry-run test（不联网）：monkeypatch `build_client` 返回假 client，断言输出字段与退出码；**不**调用真实 provider。

- [ ] **Step 2: Run dry-run test**

```bash
"$PY" -m pytest backend/tests/test_v0391_thinking_mapping.py -q -k probe
```
Expected: PASS (fake client).

- [ ] **Step 3: STOP and ask the user**

Ask exactly: “是否授权对 provider `<id>` 发起一次 `max_tokens=8` 的真实调用核实思考参数？只打印 accepted/reasoning 计数，不打印密钥或内容。” Do not run the live probe until yes.

- [ ] **Step 4 (after authorization): Run the probe for each candidate**

> 这一段是修复轮 1（M-6）重写的。旧示例少 `--data-dir`、`--grant`、`--ledger`、`--attempt`
> 四样，还拿 `openai_reasoning_effort` 举例——它不在两轮授权的任何 `allowed_shapes` 里。
> 照抄旧示例的结果是 `rc=2` 响亮拒发（**不会烧钱**），但会被读成"探针坏了"。
> live 模式现在有四样必填，缺一即 rc=2、零出站：`--data-dir`、`--grant <轮次 id>`、
> `--ledger`（basename 必须等于那一轮表里的 `ledger_name`，且三次调用同一路径）、
> `--attempt`（"我以为这是这一轮的第几格"，与账本序号对不上就拒发）。

```bash
# 先 dry-run 演**两侧各一格**（假 client、零网络、不记账），才许真发。
# 修复轮 2（F3）改了这一段：旧示例只写"先 dry-run 绿"，而 `--fake` 缺省是 `reasoning`
# （上游仍在出推理），拿它演 OFF 那一形按退出码表就是 `switch_off_not_observed` ⇒ **rc=1**。
# 照旧示例走永远等不到"绿"。dry-run 这一格真正要看的不是 rc，而是
# `transport=fake_client` + `scope.simulated=true`（那是"零出站的机械凭据"）；
# rc 由"这一格演的是哪一侧、结果对不对"决定。所以两侧各演一格：
# 第 ① 格演 OFF 那一侧：假上游"痕迹消失"才算对 ⇒ `--fake quiet`，期望 rc=0 / `switch_off_observed`。
"$PY" tools/probe_thinking_profile.py --dry-run --fake quiet \
    --data-dir <现网 data 目录> \
    --provider deepseek-chat --profile deepseek_thinking_disabled --level 均衡 \
    --grant g2-20261102 --ledger <仓库外固定目录>/call-ledger-g2-20261102.jsonl --attempt 1
# 第 ② 格演 ON 那一侧：假上游"仍在出推理"才算对 ⇒ `--fake reasoning`，期望 rc=0 / `verified`。
"$PY" tools/probe_thinking_profile.py --dry-run --fake reasoning \
    --data-dir <现网 data 目录> \
    --provider deepseek-chat --profile deepseek_thinking_type --level 均衡 \
    --grant g2-20261102 --ledger <仓库外固定目录>/call-ledger-g2-20261102.jsonl --attempt 1
# 两格配反了（quiet 演 ON、reasoning 演 OFF）就会 rc=1，那是**预期中的红**，不是通路坏了；
# 配对了却 rc=2 才是预检闸在说话，按 payload 的 notes 读那一句。

# §R5 预登记的三格（OFF 方向）；--ledger 三次同一路径，--attempt 依次 1 / 2 / 3。
"$PY" tools/probe_thinking_profile.py --data-dir <现网 data 目录> \
    --provider deepseek-chat --profile deepseek_thinking_disabled --level 均衡 \
    --grant g2-20261102 --ledger <仓库外固定目录>/call-ledger-g2-20261102.jsonl --attempt 1
"$PY" tools/probe_thinking_profile.py --data-dir <现网 data 目录> \
    --provider p_55a83a43 --profile dashscope_enable_thinking_off --level 均衡 \
    --grant g2-20261102 --ledger <仓库外固定目录>/call-ledger-g2-20261102.jsonl --attempt 2
# 第 3 格按 §R5 三选一（同轮 ON 对照 / 或 none 基线），仍必须带 --attempt 3 与同一份 --ledger。
```
Record the safe JSON outputs in the plan execution log. A rejected/unknown parameter is **not** 「不支持」; it stays unverified.

- [ ] **Step 5: Commit only the probe + dry-run test**

```bash
git add tools/probe_thinking_profile.py backend/app/core/thinking_profiles.py backend/tests/test_v0391_thinking_mapping.py
git commit -m "test(thinking): 思考参数核实探针与 dry-run"
```

---

## Task 12: Thinking profile registry and request mapping

> **生效范围（Task 12 落地时补的一行，不改写下面的任务正文）**：本轮走 `none-only` 那一支——
> 探针 g1+g2 共 7 次真调用之后没有任何配方够格进生产白名单，所以 Task 12 交付的是**边界本身**
> （未核实就不发），不是"思考强度能用"。裁决与三处范围改动（M1/M2/M3）见
> `.superpowers/sdd/2026-10-31-model-cockpit/task-12-amendment.md`；它把上面那句
> "把探针确认通过的那一条复制进生产白名单"改写成"今天一条都不复制"。
> 拆单与验收：`task-12a-brief.md`（登记表机制）与 `task-12b-brief.md`（请求侧通道）。

**Files:**
- Modify: `backend/app/core/thinking_profiles.py`（Task 11 已建；这里只加 `PROFILES`/`build_thinking_kwargs`，并把探针确认通过的那一条复制进生产白名单）
- Modify: `backend/app/core/providers.py`（把核实的 profile 加进 `THINKING_PROFILES`）
- Modify: `backend/app/main.py`（`ChatRequest.thinking_level`）
- Modify: `backend/app/core/streaming.py`
- Modify: `backend/app/pipeline.py`
- Test: `backend/tests/test_v0391_thinking_mapping.py`

**Interfaces:**
- `build_thinking_kwargs(profile: str, level: str) -> dict`：白名单映射；`none` 返回 `{}`。
- `ChatRequest.thinking_level: Optional[str]`；`_prepare_chat` 验证 provider 的 `thinking_status=="verified"` 且 level 在 `thinking_levels`；否则 400。
- `stream_chat_events(..., thinking_level=None)`；`pipeline.chat(..., thinking_level=None)`；两者调用同一个 `build_thinking_kwargs`。
- `THINKING_PROFILES` only contains `none` + profiles that passed Task 11.

- [ ] **Step 1: Write failing mapping tests (fake client, no network)**

```python
import pytest
import app.core.thinking_profiles as tp
from app.core.thinking_profiles import build_thinking_kwargs
from app.core.providers import ProviderError


def test_none_emits_no_parameter():
    assert build_thinking_kwargs("none", "均衡") == {}


def test_verified_profile_maps_level_by_index_and_rejects_unknown(monkeypatch):
    # 用测试专用 profile 验证机制；生产白名单只加 Task 11 真实核实过的那一条。
    monkeypatch.setitem(tp.PROFILES, "test_verified", {
        "verified": True, "levels": ["快速", "均衡", "深入"],
        "kwargs": lambda level: {"reasoning_effort":
            {"快速": "low", "均衡": "medium", "深入": "high"}[level]},
    })
    kwargs = tp.build_thinking_kwargs("test_verified", "均衡")
    assert kwargs["reasoning_effort"] in {"low", "medium", "high"}
    with pytest.raises(ValueError):
        tp.build_thinking_kwargs("test_verified", "不存在")


def test_unverified_profile_never_reaches_upstream(client, monkeypatch):
    from app.core import streaming
    called = {}
    def fake_events(model, messages, **kwargs):
        called.update(kwargs)
        yield __import__("app.core.stream_events", fromlist=["content_frame"]).content_frame("好")
    monkeypatch.setattr(streaming, "stream_chat_events", fake_events)
    res = client.post("/v1/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "thinking_level": "均衡"})
    assert res.status_code == 400
    assert "thinking_level" not in called or called.get("thinking_level") is None
```

- [ ] **Step 2: Run to verify fail**

```bash
"$PY" -m pytest backend/tests/test_v0391_thinking_mapping.py -q
```
Expected: FAIL — module/field missing.

- [ ] **Step 3: Implement registry + validation + two request paths**

`thinking_profiles.py`：
```python
PROFILES = {
    "none": {"verified": True, "levels": [], "kwargs": lambda level: {}},
    # Task 11 通过后才加；示例形状，未核实前不要取消注释：
    # "openai_reasoning_effort": {"verified": True,
    #     "levels": ["快速", "均衡", "深入"],
    #     "kwargs": lambda level: {"reasoning_effort": {"快速": "low", "均衡": "medium", "深入": "high"}[level]}},
}

def build_thinking_kwargs(profile: str, level: str) -> dict:
    p = PROFILES.get(profile)
    if not p or not p.get("verified"):
        raise ValueError("思考参数未核实")
    if profile == "none":
        return {}
    if level not in p["levels"]:
        raise ValueError("该模型不支持这个思考强度")
    return dict(p["kwargs"](level))
```
`providers.THINKING_PROFILES` 由 `thinking_profiles.PROFILES` 派生，避免两份白名单。
`main.py` `ChatRequest` 加 `thinking_level: Optional[str] = None`；`_prepare_chat` 里：
```python
from app.core import providers as providers_mod  # 或沿用 main.py 已有 import

if request.thinking_level:
    status = providers_mod._thinking_status(
        bool(provider.get("supports_thinking")),
        str(provider.get("thinking_profile") or "none"))
    if status != "verified" or request.thinking_level not in provider.get("thinking_levels", []):
        raise HTTPException(400, "该模型不支持这个思考强度")
```
把 `request.thinking_level` 传给 producer/pipeline；`stream_chat_events` 的 `kwargs` 在 `temperature`/`max_tokens` 后：
```python
kwargs.update(build_thinking_kwargs(provider.get("thinking_profile") or "none",
                                    thinking_level or ""))
```
`pipeline.chat` 同样在 `create` 前 update；没有 verified profile 时 `thinking_level` 为 None，不发参数。

- [ ] **Step 4: Run focused tests + full backend?**

```bash
"$PY" -m pytest backend/tests/test_v0391_thinking_mapping.py backend/tests/test_v0391_provider_capabilities.py backend/tests/test_stream_api.py -q
```
Expected: PASS。若 Task 11 未授权，profile 只有 none；测试只验证 none/拒绝，不伪造 verified。

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/thinking_profiles.py backend/app/core/providers.py backend/app/main.py backend/app/core/streaming.py backend/app/pipeline.py backend/tests/test_v0391_thinking_mapping.py
git commit -m "feat(thinking): 白名单 profile 与请求映射"
```

---

## Task 13: Clients send thinking_level

**Files:**
- Modify: `backend/app/web/static/api.js`
- Modify: `backend/app/web/static/app.js`
- Modify: `.../Api.kt`
- Modify: `.../ui/ChatUi.kt`
- Test: `backend/tests/test_v0391_web_cockpit_ui.py`, `test_v0391_native_cockpit.py`

**Interfaces:**
- Web `API.streamChat` payload includes optional `thinking_level`; `send`/`regenerate` compute it from the local per-provider preference only when `thinking_status=="verified"` and enabled.
- Native `Api.chatPayload`/`streamChat` payload includes optional `thinking_level`; `ChatUi` computes it from `Prefs.thinkingEnabled/Level` only when verified.
- Off/unverified/unsupported → omit the key entirely (not `""`).

- [ ] **Step 1: Write failing tests**

```python
def test_clients_send_thinking_level_only_when_verified():
    js = _js()
    assert "thinking_level" in _function_body(js, "thinkingLevelForSend"), \
        "网页没有发送思考档位"
    assert "thinking_status" in _function_body(js, "thinkingLevelForSend"), \
        "网页没先看能力状态"
    api = _c("Api.kt")
    assert "thinking_level" in _block(api, "fun chatPayload("), "原生 payload 没带档位"
    chat = _c("ui/ChatUi.kt")
    assert "thinkingLevelForSend" in chat, "原生发送没接偏好"
```

- [ ] **Step 2: Run to verify fail**

```bash
"$PY" -m pytest backend/tests/test_v0391_web_cockpit_ui.py backend/tests/test_v0391_native_cockpit.py -q
```
Expected: FAIL.

- [ ] **Step 3: Implement client payloads**

Web `app.js`：
```js
function thinkingLevelForSend(provider) {
  if (!provider || provider.thinking_status !== "verified") return null;
  const pref = readThinkingPrefs()[provider.id];
  if (!pref || !pref.enabled) return null;
  const level = provider.thinking_levels.includes(pref.level)
    ? pref.level : (provider.thinking_default || provider.thinking_levels[0] || "");
  return level || null;
}
```
`api.js` 的 stream payload 用 `obj`/条件键：只有非空才加 `thinking_level`。`send`/`regenerate` 把 `currentProvider()` 传进去。
Native `Api.kt` `chatPayload(..., thinkingLevel: String? = null)`：仅在非空时 `put("thinking_level", ...)`；`ChatUi` 用同一逻辑从 `Prefs.thinkingEnabled/Level` + `ModelInfo.thinking_status` 计算。

- [ ] **Step 4: Run focused tests + compile**

```bash
"$PY" -m pytest backend/tests/test_v0391_web_cockpit_ui.py backend/tests/test_v0391_native_cockpit.py -q
node --check backend/app/web/static/app.js
JAVA_HOME=C:/Users/34426/dl/perf-package-a/jdk17x/jdk-17.0.20.1+1 \
  C:/Users/34426/dl/perf-package-a/gradlex/gradle-8.7/bin/gradle.bat \
  -p android-native :app:compileDebugKotlin
```
Expected: PASS + BUILD SUCCESSFUL（或如实报告）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/web/static/api.js backend/app/web/static/app.js android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/Api.kt android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/ChatUi.kt backend/tests/test_v0391_web_cockpit_ui.py backend/tests/test_v0391_native_cockpit.py
git commit -m "feat(thinking): 客户端按核实能力发送思考档位"
```

---

## Task 14: Full verification and handoff

**Files:** none (verification only).

- [ ] **Step 1: Run the full backend suite**

```bash
"$PY" -m pytest backend/tests -q
```
Expected: `0 failed, 0 errors`; report exact passed/skipped. If it exceeds the 10-minute shell limit, split the test files into 4 groups, run each with logs outside the repo, and ensure every group rc=0. Do not report a subset as full.

- [ ] **Step 2: Run JS syntax + Kotlin compile + focused UI tests**

```bash
node --check backend/app/web/static/app.js
"$PY" -m pytest backend/tests/test_v0391_web_cockpit_ui.py backend/tests/test_v0391_native_cockpit.py -q
JAVA_HOME=C:/Users/34426/dl/perf-package-a/jdk17x/jdk-17.0.20.1+1 \
  C:/Users/34426/dl/perf-package-a/gradlex/gradle-8.7/bin/gradle.bat \
  -p android-native :app:compileDebugKotlin
```
Expected: syntax OK；tests PASS；BUILD SUCCESSFUL（或明确「无法证实」）。

- [ ] **Step 3: Manual acceptance checklist**

- 桌面：模型名/环开两个面板；hover 模型行右侧详情切换；共享模型能力只读；本会话预算滑杆独立。
- 手机：两个面板从底部展开；环 44dp 触控；队列卡动作可用。
- 忙时发送入队；正常完成后自动发队首；停止/报错暂停；切会话不串；切身份清空；刷新后队列消失（首版预期）。
- 上下文：调低预算出现超预算警示与丢弃预览；轻量整理只减本轮发送，历史原文仍在；环/分段/百分比同步。
- 思考：未授权时所有思考开关禁用并显示未验证；Task 11 授权且核实后，只有 verified 的模型可开，请求里才出现 `thinking_level`。
- 不出现：模型/队列文本被 `innerHTML` 拼接；队列落盘；设置页重复上下文/上限控件；共享模型普通用户可写。

- [ ] **Step 4: Report**

Report:
- 已确认：全量 passed/skipped、0 failed/0 errors；Kotlin 编译；Node；手动项（实际做了什么）。
- 待确认：真机帧率/观感、浏览器实测、APK 构建、CI Linux、Task 11 是否授权核实。
- 残余：队列不持久化、多标签页各自一份、上下文估算/校准误差、思考 profile 仅覆盖已核实 provider。
- 不 push/tag/发版；交独立审查。

---

## Self-Review (plan author)

**Spec coverage:**
- done usage → Task 1/6/13；上下文环/账本 → Task 2/4/6；接力队列 → Task 3/5/7；模型卡/管理/设置迁移 → Task 8/9/10；思考 profile/核实/映射 → Task 11/12/13；全量/边界 → Task 14。
- 队列首版内存、轻量整理只丢最早消息、深度压缩 P2、无新内联脚本、文本渲染、白名单参数、权限/`can_edit` 均在 Global Constraints / tasks 中写明。

**Placeholder scan:** 无 TBD/TODO；Task 11 是显式授权闸，不是占位；未核实的 profile 保持 `none`，不伪造。

**Type consistency:** `usage_out` 在 Task 1/6 一致；`contextBudgetK`/`setContextBudget` 在 Task 2/4/6/9/10 一致；`queueFor`/`nextQueueItem`/`canRelay` 在 Task 3/5/7 一致；`thinking_level` 在 Task 12/13 一致；`can_edit` 在 Task 8/9/10 一致。
