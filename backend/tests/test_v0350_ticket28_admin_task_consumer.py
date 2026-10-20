"""v0.35.0 工单 #28（剩余消费半边）：管理页的任务面。

#133（web/PWA）与 #99（原生）已经把「需要你输入」卡片做完了；这一票只剩 admin。
管理页此前只把 `t.status` 原样摆出来（`waiting_user` 这种机器字面量直接给人看），
任务详情是一坨 JSON——`waiting_user` 的问题原文、`can_resume`、以及 `results`
的可见性都没有被任何界面消费。

本文件钉四件事：

1. **列表把 waiting_user 翻成人话，并把问题原文读出来**。列表接口不带问题（问题
   可能是一枚验证码/收货地址的追问），所以 admin 只给这一种状态的行各补一次
   `GET /v1/tasks/{id}`；其余状态一次详情都不许打。问题读不到时说"没读到"，
   不许拿"没有问题"冒充。
2. **results 的可见性照服务端的口径**：`GET /v1/tasks/{id}` 只在 `completed` 时
   返回 results，admin 页也只在那一态渲染；完成前明说"不返回"，不画空列表。
   先把真任务塞进存储、打真端点，再把响应喂给前端函数——判据不是测试手搓的形状。
3. **管理页保持只读**：没有回答输入框、没有 `user_answer`、没有新帧名/桥方法。
   续跑只认任务属主（orchestrator 的归属校验），管理员代答只会拿到 404；页面
   把问题原文摆出来并明说"只有发起人能回答"。
4. **删除文案不再说任务不落盘**：`DELETE /v1/tasks/{id}` 会 `_flush` 回
   `data/tasks.json`，旧文案"它本来也不落盘，重启即空"会让管理员以为删不删都一样。

跑真函数：从 admin.js 里切出 `taskStatusText` / `taskRow` / `fillTaskQuestion` /
`loadTasks` / `taskDetailView` / `paintTaskDetail`，配一套假 DOM 在 node 里执行。
没有 node 的机器上这几条 skip（与 test_v0350_task_ui_web_contract 同一口径）；
其余静态锁照常跑。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.agents import task_store as ts

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_DIR = REPO_ROOT / "backend" / "app" / "web" / "admin"
ADMIN_JS = ADMIN_DIR / "admin.js"
ADMIN_HTML = ADMIN_DIR / "index.html"
MANUAL = REPO_ROOT / "docs" / "用户手册.md"

# 判据读前端文本前先剥注释（解释性注释里会出现被禁字样，真删掉的文案只要还躺在
# 注释里就仍会命中"不许出现"那一半）。尺子与 test_admin_page.py 同一份。
import sys
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from tests.test_web_pwa import _strip_html_comments, _strip_js_comments  # noqa: E402

# 任务面这一组函数：静态锁与 node 行为锁都用这一份清单。
TASK_FACE_FUNCS = ("cell", "fmtTime", "taskStatusText", "taskRow", "fillTaskQuestion",
                   "loadTasks", "taskDetailView", "paintTaskDetail")


@pytest.fixture(autouse=True)
def isolated_tasks(tmp_path, monkeypatch):
    """每条用例自己的 tasks.json；进出都把进程级存储清空重恢复。"""
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    for tid in list(ts.task_store):
        del ts.task_store[tid]
    yield path
    for tid in list(ts.task_store):
        del ts.task_store[tid]


def _text(path: Path) -> str:
    assert path.is_file(), f"契约的一端不在仓库里：{path}"
    return path.read_text(encoding="utf-8")


def _js() -> str:
    return _strip_js_comments(_text(ADMIN_JS))


def _html() -> str:
    return _strip_html_comments(_text(ADMIN_HTML))


def _js_function(src: str, name: str) -> str:
    """`[async] function name(...) { ... }` 整段原文（大括号配平收尾）。"""
    at = src.find(f"function {name}(")
    assert at >= 0, f"admin.js 里找不到 {name}()——改名了还是被删了？"
    if src[:at].rstrip().endswith("async"):
        at = src.rfind("async", 0, at)
    open_at = src.index("{", src.index(")", at))
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[at:i + 1]
    raise AssertionError(f"{name}() 的大括号没闭合，切不出函数体")


def _task_surface_source() -> str:
    src = _text(ADMIN_JS)
    const = re.search(r"const TASK_STATUS = \{[\s\S]*?\n  \};", src)
    assert const, "TASK_STATUS 这张表不像原来的形状了，这条锁要跟着改读法"
    return const.group(0) + "\n" + "\n".join(_js_function(src, n) for n in TASK_FACE_FUNCS)


_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const driver = fs.readFileSync(process.argv[3], "utf8");
const scenario = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
const run = new Function("SC", src + "\nreturn (async () => {\n" + driver + "\n})();");
Promise.resolve(run(scenario))
  .then((v) => process.stdout.write(JSON.stringify({ ok: true, out: v })))
  .catch((e) => process.stdout.write(JSON.stringify({ ok: false, error: String((e && e.stack) || e) })));
"""


def _run_node(source: str, driver: str, scenario=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "fns.js").write_text(source, encoding="utf-8")
        (d / "driver.js").write_text(driver, encoding="utf-8")
        (d / "harness.cjs").write_text(_HARNESS, encoding="utf-8")
        (d / "scenario.json").write_text(json.dumps(scenario or {}), encoding="utf-8")
        r = subprocess.run([node, str(d / "harness.cjs"), str(d / "fns.js"),
                            str(d / "driver.js"), str(d / "scenario.json")],
                           capture_output=True, text=True, timeout=90)
        assert r.returncode == 0, f"node 自己就挂了：{r.stderr}"
        line = [x for x in r.stdout.strip().splitlines() if x.startswith("{")]
        assert line, f"node 没给出 JSON：stdout={r.stdout!r} stderr={r.stderr!r}"
        out = json.loads(line[-1])
        assert out["ok"], f"这段 JS 跑挂了：{out['error']}"
        return out["out"]


# 假 DOM：只实现本票那组函数用到的面。textContent 与 children 分开收，
# 所以"整棵树的文字"能递归拼出来，而不必猜每个节点落在哪。
_FAKE_DOM = r"""
function __node(tag) {
  const n = {
    tag: tag || "div", className: "", _text: "", value: "", disabled: false,
    children: [],
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { if (on === undefined) on = !this._s.has(c);
                      if (on) this._s.add(c); else this._s.delete(c); },
    },
    appendChild(ch) { this.children.push(ch); return ch; },
    querySelector() { return null; },
  };
  // 真 DOM 里 `textContent = ""` 会把子节点一起清掉；paintTaskDetail 每次重画都靠
  // 这一句。假 DOM 不建模的话，第二次画出来的文字会叠在第一次上面。
  Object.defineProperty(n, "textContent", {
    get() { return this._text; },
    set(v) { this._text = String(v); this.children.length = 0; },
  });
  return n;
}
function __text(n) {
  let out = String((n && n.textContent) || "");
  ((n && n.children) || []).forEach((c) => { out += " " + __text(c); });
  return out;
}
var document = { createElement: (tag) => __node(tag) };
var __els = {};
function __el(id) { if (!__els[id]) __els[id] = __node("div"); return __els[id]; }
var $ = (id) => __el(id);
var nameById = { "u-28": "甲" };
var req = async () => { throw new Error("req 没被这条用例替换"); };
function btn(iconName, label, cls, fn) {
  const b = __node("button");
  b.className = cls || "";
  b.textContent = label || "";
  b.__fn = fn;
  return b;
}
var busy = async (el, fn) => fn();
var ask = async () => false;
var flash = () => {};
"""


# ==========================================================================
# ① 列表：waiting_user 翻成人话，问题原文只为这一种状态补读
# ==========================================================================

def test_waiting_rows_show_human_status_and_the_real_question():
    """列表里出现的是「需要你输入」与问题原文，不是 `waiting_user` 这个机器字。

    行为判据：跑真 `loadTasks`。请求账必须恰好是"列表一次 + 每条 waiting 一次"——
    给所有行都打详情是 N+1 的另一种写法，而问题原文只在详情里，其余状态没有理由读。
    """
    tasks = [
        {"task_id": "t-run", "goal": "在跑的那条", "status": "running",
         "progress": "1/2", "user_id": "u-28", "created_at": "2026-10-21T09:00:00"},
        {"task_id": "t-wait", "goal": "周末买菜", "status": "waiting_user",
         "progress": "1/3", "user_id": "u-28", "created_at": "2026-10-21T09:05:00"},
    ]
    detail = {"task_id": "t-wait", "goal": "周末买菜", "status": "waiting_user",
              "user_id": "u-28", "can_resume": True,
              "waiting_since": "2026-10-21T09:06:00",
              "resume_cursor": {"question": "「周末买菜」要记到哪一天？",
                                "subtask_index": 1, "subtask_total": 3}}
    out = _run_node(_FAKE_DOM + _task_surface_source(), r"""
      const calls = [];
      req = async (path) => {
        calls.push(path);
        if (path === "/v1/tasks") return { tasks: SC.tasks };
        if (path === "/v1/tasks/" + encodeURIComponent(SC.detail.task_id)) return SC.detail;
        throw new Error("unexpected request: " + path);
      };
      await loadTasks();
      return { text: __text(__el("taskList")), calls };
    """, {"tasks": tasks, "detail": detail})

    assert "需要你输入" in out["text"], out["text"]
    assert "问题：「周末买菜」要记到哪一天？" in out["text"], out["text"]
    assert "waiting_user" not in out["text"], "列表把机器状态字直接摆给人看"
    assert "甲" in out["text"], "任务行没有把归属人翻成人名"
    assert out["calls"] == ["/v1/tasks", "/v1/tasks/t-wait"], \
        f"不是只给 waiting 的行补详情：{out['calls']}"


def test_a_waiting_row_says_so_when_the_question_cannot_be_read():
    """问题读不到时不许沉默：不写"读取中…"、不冒充"没有问题"，而是说没读到。"""
    out = _run_node(_FAKE_DOM + _task_surface_source(), r"""
      req = async (path) => {
        if (path === "/v1/tasks") return { tasks: [SC.task] };
        throw new Error("详情接口 500");
      };
      await loadTasks();
      return { text: __text(__el("taskList")) };
    """, {"task": {"task_id": "t-wait", "goal": "买菜", "status": "waiting_user",
                   "user_id": "u-28"}})
    assert "问题没读到" in out["text"] and "详情接口 500" in out["text"], out["text"]
    assert "问题读取中" not in out["text"], "占位一直没被换掉：这一行永远读不出问题"


# ==========================================================================
# ② results 的可见性：只认 completed，完成前明说不返回
# ==========================================================================

def test_results_are_rendered_only_for_completed_tasks():
    """服务端的口径是 `results` 只在 completed 时非空；admin 页不许把它放宽。

    三个输入都故意带着 results：waiting / running 那两份在现实里到不了前端，这里
    仍然塞进去，是为了证明"前端的门"自己也在守——将来谁把端点改成提前回 results，
    管理页也不会把中间结果当最终结果摆出来。
    """
    waiting = {"task_id": "t-wait", "goal": "买票", "status": "waiting_user",
               "user_id": "u-28", "can_resume": True,
               "waiting_since": "2026-10-21T09:06:00",
               "subtasks": ["选店", "下单"], "current_subtask": 1,
               "resume_cursor": {"question": "请输入短信验证码"},
               "results": ["完成前不该露出的结果"]}
    running = dict(waiting, status="running", resume_cursor=None,
                   results=["运行中也不该露出的结果"])
    completed = {"task_id": "t-done", "goal": "写周报", "status": "completed",
                 "user_id": "u-28", "subtasks": ["草稿", "提交"], "current_subtask": 2,
                 "results": ["草稿写好了", "提交了"], "final_answer": "周报已交"}
    out = _run_node(_FAKE_DOM + _task_surface_source(), r"""
      paintTaskDetail(taskDetailView(SC.waiting));
      const waitingText = __text(__el("detailText"));
      paintTaskDetail(taskDetailView(SC.running));
      const runningText = __text(__el("detailText"));
      paintTaskDetail(taskDetailView(SC.completed));
      const completedText = __text(__el("detailText"));
      return { waiting: waitingText, running: runningText, completed: completedText };
    """, {"waiting": waiting, "running": running, "completed": completed})

    assert "需要你输入" in out["waiting"] and "请输入短信验证码" in out["waiting"], out
    assert "只有这条任务的发起人" in out["waiting"], \
        f"waiting 详情没说明管理员只读：{out['waiting']!r}"
    assert "完成前不该露出的结果" not in out["waiting"], \
        f"完成前的中间结果被摆出来了：{out['waiting']!r}"
    assert "运行中也不该露出的结果" not in out["running"], out["running"]
    assert "只在「办完了」的任务详情里返回" in out["waiting"] \
        and "只在「办完了」的任务详情里返回" in out["running"], out
    assert "草稿写好了" in out["completed"] and "提交了" in out["completed"], out
    assert "周报已交" in out["completed"], out["completed"]
    assert "办完了" in out["completed"] \
        and "只在「办完了」的任务详情里返回" not in out["completed"], out


def test_the_real_endpoint_supplies_exactly_the_fields_the_admin_view_reads(
        client, isolated_tasks):
    """真任务 + 真端点：waiting 的 results 是 None、问题在游标里；completed 才给 results。

    前面那条锁读的是前端源码形状，这一条读**服务端此刻的响应**。两边一起跑，
    才叫"前端只用现有键"而不是"测试自己造了一份响应"。列表端点也顺带钉住：
    它不带 question / results——这正是 admin 必须为 waiting 行补一次详情的原因。
    """
    task = ts.Task(goal="下单", subtasks=["选店", "下单"], user_id="u-28")
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    task.wait_for_user("请输入短信验证码")

    parked = client.get(f"/v1/tasks/{task.task_id}").json()
    assert parked["status"] == "waiting_user", parked
    assert parked["results"] is None, "waiting 的中间结果被提前放出来了"
    assert parked["resume_cursor"]["question"] == "请输入短信验证码", parked
    assert parked["can_resume"] is True, parked

    rows = client.get("/v1/tasks").json()["tasks"]
    row = next(r for r in rows if r["task_id"] == task.task_id)
    assert "question" not in row and "results" not in row, \
        f"列表开始带问题/结果了：admin 的补读范围要跟着重新审：{row}"

    done = ts.Task(goal="写周报", user_id="u-28")
    ts.task_store[done.task_id] = done
    done.set_status(ts.TaskStatus.RUNNING)
    done.results = ["草稿写好了"]
    done.final_answer = "周报已交"
    done.set_status(ts.TaskStatus.COMPLETED)
    finished = client.get(f"/v1/tasks/{done.task_id}").json()
    assert finished["results"] == ["草稿写好了"], finished
    assert finished["final_answer"] == "周报已交", finished

    out = _run_node(_FAKE_DOM + _task_surface_source(), r"""
      paintTaskDetail(taskDetailView(SC.parked));
      const parkedText = __text(__el("detailText"));
      paintTaskDetail(taskDetailView(SC.finished));
      const finishedText = __text(__el("detailText"));
      return { parked: parkedText, finished: finishedText };
    """, {"parked": parked, "finished": finished})
    assert "请输入短信验证码" in out["parked"], out["parked"]
    assert "草稿写好了" not in out["parked"], out["parked"]
    assert "草稿写好了" in out["finished"] and "周报已交" in out["finished"], out


# ==========================================================================
# ③ 管理页保持只读：没有回答入口、没有新帧名/桥方法
# ==========================================================================

def test_the_admin_task_surface_gains_no_answer_entry_or_stream_bridge():
    """管理员代答在服务端走不通（续跑只认属主），页面也不许摆一个必然失败的输入框。

    同时钉"没有新帧名/桥方法"：这一页本来就不挂 SSE，任务状态只从 GET 来；
    如果谁把 PWA 那张卡片整套抄过来接上 `EventSource` / `onEvent`，这条会红。
    """
    js, html = _js(), _html()
    for banned in ("user_answer", "answerTask", "taskAnswerInput", "taskAnswerBtn",
                   "task-answer", "EventSource", "onEvent"):
        assert banned not in js, f"管理页出现了回答入口/流桥：{banned}"
    assert "task-answer" not in html and "taskAnswerInput" not in html
    assert "只有这条任务的发起人" in js, \
        "管理员看得见问题，却没被告知只有属主能回答"
    assert "不提供代答入口" in js


# ==========================================================================
# ④ 删除文案：任务会落盘，删了不会在重启后回来
# ==========================================================================

def test_delete_task_copy_matches_the_persisted_store():
    """旧文案「它本来也不落盘，重启即空」在 v0.24 之后就是假的。

    `_TaskStore.__delitem__` 删完立刻 `_flush()`，也就是写回 `data/tasks.json`。
    说成"重启即空"会让管理员把挂起中的断点当垃圾删掉——那是不可再生的。
    """
    js = _js()
    assert "它本来也不落盘，重启即空" not in js, "删除文案还在说任务不落盘"
    assert "data/tasks.json" in js, "删除文案没说它到底落到哪"
    assert "断点" in js and "先确认" in js, "删除文案没提醒挂起中的断点会一起没"


# ==========================================================================
# ⑤ 用户手册：回答任务这一节必须真的存在
# ==========================================================================

def test_the_user_manual_explains_how_to_answer_a_waiting_task():
    manual = _text(MANUAL)
    at = manual.find("### 回答任务")
    assert at >= 0, "用户手册里没有「回答任务」一节"
    section = manual[at:at + 1200]
    for needle in ("需要你输入", "断点", "只有任务发起人本人能回答"):
        assert needle in section, f"「回答任务」一节缺了：{needle}"
