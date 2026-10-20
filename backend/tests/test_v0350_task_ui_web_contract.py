"""v0.35.0 #133（web/PWA 侧任务入口 + 「需要你输入」卡片）的契约锁。

这个文件按代价从大到小管下面这几组判据：

1. **导出的那三个函数真的在，而且打的是那两条端点**。网页这一侧「用户能回答」这件事
   是本票第一次做，此前唯一发起入口在管理页、只发 `{goal}`。"定义写了、导出漏了"在本仓
   付过账（v0.19 的 `API.adopt is not a function`），所以判据拿 node 真跑 api.js，不 grep。
2. **帧名一条都没新造**。卡片状态一律以 `GET /v1/tasks/{id}` 为准，SSE 帧只当"去读一次"
   的提示——`api.js` 的 `onEvent` 那条缝与 unknown 那一臂是"契约之外冒出来的东西"唯一的
   出口，动它们等于把上报通道改成第二份状态表。帧名清单从 `app/core/stream_events.py`
   现读，测试里不写第二份。
3. **卡片骨架与填它的代码互相点名**。HTML 里留一格没人填 = 界面上一个永远不动的空壳；
   JS 问一格 HTML 里没有 = 静默的 `null.textContent`。两侧都判，读法从 UI 语料现解析。
4. **非属主 404 是一句话，不是一次断网**。服务端把"不存在"和"不是你的"收成同一句 404
   （那是归属探测的闸），前端要说成"读不到"。说成断网，人就守着一颗刷新键反复敲一条
   永远不会有的记录——这一页没有任何自动重试，也没有一个自己的定时器。
5. **换身份要把任务面清干净**。答案框里常是短信验证码，换人后旧输入、旧错误条、在途
   标记一个都不许留在 DOM 里；换号之后任何在途请求（列表、详情、提交后的重读）回来
   都不许写进新那个人的页面——epoch 从发请求带到落地前，不符就整段丢掉。
6. **文案只以 textContent 进 DOM，且任务面的字面分发与直接调用为零**。前者用真
   `paintTaskCard` / `renderTaskList` / `setTaskError` 跑注入串（假 DOM 的 innerHTML
   会真的长出节点）来判；后者只认字面形状：任务面源码里没有 `type === "…"` 这类比较、
   `onEvent` 体里没有 `TASK_FACE_FUNCS` 列名函数的直接调用 `name(`。

**这两条"帧 / 调用边"判据的射程只到字面形状，仍漏清单（今天这么写就看不见，不是
已覆盖）**：

- `switch (evt.type) { case "waiting_user": … }`：不是 `===` 比较，扫不到；
- `evt["type"] === "…"`、先 `const t = evt.type` 再比：名字不是 `evt.type`；
- `const { type } = evt`：解构出来的名字不在正则里；
- 把帧转给 helper（`onUnknown` / `onStatus` / 自定义函数）再由它认：只看 `onEvent` 体；
- `list.forEach(paintTaskCard)`、`const f = paintTaskCard; f(...)`：不是 `name(` 直呼；
- 在 `TASK_FACE_FUNCS` 之外新加的任务面函数：表里没列就没人看（今天 `setTaskError`
  就不在这张表里，它只由写入点那条 XSS 锁罩着）；
- 帧名被拼接 / 截断（`"wait" + "_user"`）或写在注释里：前者不是单个字面量，后者剥掉。

倒计时那一句只建立在 `waiting_expires_at` 上，而默认窗口下服务端答的是 None；
"默认显示「等你回复」、既不说数字也不说无限"由下面的默认窗口那条钉，输入取
`app.agents.task_store` 的真函数，不靠测试自己造形状。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.core import stream_events as se

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "backend" / "app" / "web" / "static"
MAIN_PY = REPO_ROOT / "backend" / "app" / "main.py"
APP_PY = REPO_ROOT / "backend" / "app"
# 服务端把帧写到线上的那三个文件（与 test_process_trace_contract 的 EMIT_SIDE 同一份范围，
# 那里判"别手打"，这里判"别认一个发不出来的名字"）。
EMIT_SIDE = (APP_PY / "main.py", APP_PY / "core" / "streaming.py",
             APP_PY / "core" / "stream_runs.py")

# 这一页读的三个函数名。写在这里是因为它是**对外契约的名字**（票面定的），
# 不是因为测试想挑一次实现细节。
TASK_API_FUNCS = ("listTasks", "getTask", "answerTask")

# 任务面这一组函数与 onEvent 之间不许有调用边：卡片状态只从 GET 来，流不是它的第二真源。
TASK_FACE_FUNCS = ("taskStatusText", "taskProgressLine", "taskWaitingLine", "taskStatusLine",
                   "taskCardView", "paintTaskCard", "taskErrorLine", "renderTaskList",
                   "paintTasks", "openTaskDetail", "loadTasks", "submitTaskAnswer")


def _text(path: Path) -> str:
    assert path.is_file(), f"契约的另一端不在仓库里：{path}"
    return path.read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    """去掉 JS 的 // 与 /* */ 注释，尊重字符串字面量（判据不许被注释喂绿）。"""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in ('"', "'", "`"):
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == c:
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_html_comments(src: str) -> str:
    """注释整段抹平成等量换行，保住"按位置判"的部分不挪位。"""
    out, i = [], 0
    while i < len(src):
        if src.startswith("<!--", i):
            end = src.find("-->", i)
            chunk = src[i:] if end < 0 else src[i:end + 3]
            out.append("\n" * chunk.count("\n"))
            i += len(chunk)
            continue
        out.append(src[i])
        i += 1
    return "".join(out)


def _js_function(src: str, name: str) -> str:
    """`[async] function name(...) { ... }` 整段原文（大括号配平收尾）。

    切错了宁可抛：把缺一段的函数交给 node，node 报的是"harness 自己坏了"那种错话。
    """
    at = src.find(f"function {name}(")
    assert at >= 0, f"{name}() 在前端源码里找不到了——改名了还是被删了？"
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


def _js_const(src: str, name: str) -> str:
    """`const NAME = "..."` 的值（字符串常量这一族）。"""
    m = re.search(rf'const {name}\s*=\s*"([^"]*)"', src)
    assert m, f"app.js 里找不到 const {name} = \"...\""
    return m.group(1)


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
    """在 node 里真跑交给它的那段 JS；driver 里可以 await。"""
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


# 假 DOM 的公共底：textContent 与 innerHTML 分开收，innerHTML 会真的"长出"一颗 <img>。
# 有了这条，把写入改成 innerHTML 才是在行为上被判红，而不是被一次字符串搜索判红。
_FAKE_DOM = r"""
var __created = [];
function __node(tag) {
  const n = {
    tag: tag || "div", className: "", type: "", textContent: "", _html: "",
    _kids: {}, _children: [], _imgs: [],
    classList: {
      _s: new Set(),
      toggle(c, on) { if (on) this._s.add(c); else this._s.delete(c); },
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    appendChild(ch) { this._children.push(ch); return ch; },
    querySelector(sel) {
      if (sel === "img") {
        for (const k in this._kids) if (this._kids[k]._imgs.length) return this._kids[k]._imgs[0];
        return null;
      }
      if (sel.charAt(0) !== ".") return null;
      if (!this._kids[sel]) this._kids[sel] = __node("div");
      return this._kids[sel];
    },
  };
  Object.defineProperty(n, "innerHTML", {
    get() { return this._html; },
    set(v) {
      this._html = String(v);
      const m = String(v).match(/<img\b[^>]*>/i);
      if (m) { const img = __node("img"); img._html = m[0]; this._imgs.push(img); }
    },
  });
  __created.push(n);
  return n;
}
function __findImgs() {
  const out = [];
  __created.forEach((n) => { if (n._imgs && n._imgs.length) out.push.apply(out, n._imgs); });
  return out;
}
var __els = {};
function __el(id) { if (!__els[id]) __els[id] = __node("div"); return __els[id]; }
var document = { createElement: (tag) => __node(tag) };
var $ = (id) => __el(id);
var state = { messages: [], sessions: [], pending: [], memoryQuery: "", filter: "",
              tasks: [], taskDetail: null, taskOpenId: "", taskAnswering: false,
              taskEpoch: 0 };
var renderMessages = () => {};
var renderSessions = () => {};
var NetMinder = { note: () => {} };
var needsAuth = () => false;
var API = {};
"""


def _with_dom(source: str) -> str:
    return _FAKE_DOM + source


def _api_source() -> str:
    return _text(STATIC / "api.js")


def _app_source() -> str:
    return _text(STATIC / "app.js")


def _task_module_source() -> str:
    """app.js 里任务面那一组纯函数 + 它们共用的文案常量（一次切齐，不写第二份清单）。"""
    src = _app_source()
    consts = "\n".join(re.findall(
        r'^const TASK_[A-Z_]+ = \{[^}]*\};|^const TASK_[A-Z_]+ = "[^"]*";',
        src, flags=re.M | re.S))
    assert "TASK_CARD_TITLE" in consts, "任务面的文案常量一条都没扫到——那条正则坏了"
    names = ("taskStatusText", "taskProgressLine", "taskWaitingLine", "taskStatusLine",
             "taskCardView", "paintTaskCard", "taskErrorLine")
    return consts + "\n" + "\n".join(_js_function(src, n) for n in names)


def _task_face_source() -> str:
    """任务面整组函数（纯函数 + 接线函数）：判"一条帧都不读"的射程就是这一整组。"""
    src = _app_source()
    extra = [n for n in TASK_FACE_FUNCS if n not in (
        "taskStatusText", "taskProgressLine", "taskWaitingLine", "taskStatusLine",
        "taskCardView", "paintTaskCard", "taskErrorLine")]
    return _task_module_source() + "\n" + "\n".join(_js_function(src, n) for n in extra)


def _card_block(html: str) -> str:
    """`class="msg-task…"` 那个 div 的整棵子树（按 div 配平切，注释先抹掉）。"""
    at = re.search(r'class="msg-task\b', html)
    assert at, 'index.html 里没有 class="msg-task…" 这张卡片'
    start = html.rindex("<", 0, at.start())
    tag = re.compile(r"<(/?)div\b[^>]*>")
    depth = 0
    end = None
    for m in tag.finditer(html, start):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            end = m.end()
            break
    assert end, ".msg-task 那块 div 没闭合，切不出子树"
    return html[start:end]


def _classes_in(block: str):
    found = []
    for m in re.finditer(r'class="([^"]*)"', block):
        found.extend(m.group(1).split())
    return found


@pytest.fixture()
def isolated_tasks(tmp_path, monkeypatch):
    """任务表进这条用例自己的文件，进出清空进程级存储（照 A1/A3/A5 那份夹具）。

    不落真库是本机这条路的硬规矩：这个仓库目录同时是生产机。
    """
    import app.agents.task_store as ts
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    monkeypatch.delenv("TASK_WAITING_USER_TIMEOUT_SECONDS", raising=False)
    ts.restore(path=str(path))
    for tid in list(ts.task_store):
        del ts.task_store[tid]
    yield ts
    for tid in list(ts.task_store):
        del ts.task_store[tid]


def _parked(ts, user_id="u-lock"):
    """一条真挂在工具断点上等人的任务：游标与那三个数都由生产代码自己产出。"""
    task = ts.Task(goal="买票", subtasks=["a", "b", "c"], user_id=user_id)
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    task.wait_for_user("请输入短信验证码")
    return task


# ==========================================================================
# ① 三个函数在导出表里，而且打的是那两条端点
# ==========================================================================

def test_the_three_task_wrappers_are_exported_and_hit_the_agreed_endpoints():
    """node 真跑仓库那份 api.js：三个函数取到，请求形状与鉴权口径都还是 request() 那一条路。

    判据来自运行时而不是 grep：导出漏一格，页面照常加载、别的按钮照常好用，只有点进
    任务那一页炸一句 not a function（本仓的先例是 v0.19 的 adopt）。顺带钉"没有新写一套
    fetch"——每一条都走 request()，所以 Cookie、CSRF、401 自愈、X-Token-Expires-In 一件都漏不掉。
    """
    out = _run_node(_api_source(), r"""
      const seen = [];
      globalThis.fetch = async (path, init) => {
        seen.push({ path, method: (init.method || "GET"),
                    csrf: !!(init.headers && init.headers["X-CSRF"]),
                    auth: !!(init.headers && init.headers["Authorization"]),
                    credentials: init.credentials,
                    json: init.body ? JSON.parse(init.body) : null });
        return { ok: true, status: 200, headers: { get: () => null },
                 json: async () => ({ total: 0, tasks: [] }) };
      };
      const kinds = SC.TASK_NAMES.map((n) => typeof API[n]);
      for (const n of SC.TASK_NAMES) await API[n]("t-1", "买票", "123456");
      return { kinds, seen };
    """, {"TASK_NAMES": list(TASK_API_FUNCS)})
    assert out["kinds"] == ["function"] * 3, \
        f"这三个名字在 api.js 的导出对象里不齐：{dict(zip(TASK_API_FUNCS, out['kinds']))}"
    assert [s["path"] for s in out["seen"]] == [
        "/v1/tasks", "/v1/tasks/t-1", "/v1/agent/orchestrate"], out["seen"]
    assert [s["method"] for s in out["seen"]] == ["GET", "GET", "POST"], out["seen"]
    assert [s["auth"] for s in out["seen"]] == [False] * 3, "这三个请求不该有 Authorization 头"
    assert [s["credentials"] for s in out["seen"]] == ["same-origin"] * 3
    assert [s["csrf"] for s in out["seen"]] == [False, False, True], "POST 必须带 X-CSRF"
    assert set(out["seen"][2]["json"]) == {"goal", "task_id", "user_answer"}, out["seen"][2]


def test_the_answer_request_shape_matches_the_endpoint_model():
    """answerTask 发的三键必须正好落在 OrchestrateRequest 的字段上，而 goal 那一格缺席不了。

    这几格的权威是 main.py 里那条模型（本票禁改后端，正好逼前端只用现有键）：`goal: str`
    没有默认值 = 必填，续跑判定认的却是 task_id。所以带出去的必须是**任务自己那条 goal**
    ——空串也过校验，却把"这一枪在续哪件事"从服务端日志里抹掉，而那是排查重复付费时
    唯一想看的一行。
    """
    src = _text(MAIN_PY)
    block = re.search(r"class OrchestrateRequest\(BaseModel\):\n((?:[ \t].*\n)*)", src)
    assert block, "main.py 里找不到 OrchestrateRequest——那条模型改名了，这把锁得跟着改读法"
    lines = dict(re.findall(r"^    (\w+)\s*:\s*([^\n]+)$", block.group(1), flags=re.M))
    assert set(lines) == {"goal", "task_id", "user_answer"}, sorted(lines)
    assert lines["goal"].strip() == "str", f"goal 不再是那格必填的 str：{lines['goal']!r}"
    assert lines["task_id"].startswith("Optional"), lines["task_id"]
    assert lines["user_answer"].startswith("Optional"), lines["user_answer"]

    api_src = _strip_js_comments(_api_source())
    shape = re.search(r"answerTask:\s*\(([^)]*)\)\s*=>\s*request\(\s*\"/v1/agent/orchestrate\""
                      r"\s*,\s*\{\s*method:\s*\"POST\",\s*body:\s*\{([^}]*)\}", api_src)
    assert shape, "api.js 里 answerTask 的形状变了：它必须是 request + POST /v1/agent/orchestrate"
    assert [p.strip() for p in shape.group(1).split(",")] == ["goal", "taskId", "userAnswer"]
    sent = shape.group(2)
    for key in ("goal", "task_id", "user_answer"):
        assert re.search(rf"\b{key}\b", sent), f"这一格没发出去：{key} -> {sent!r}"
    assert re.search(r"goal,\s*task_id:", sent), f"goal 不是原样带出去的那一形：{sent!r}"


# ==========================================================================
# ② onEvent 那条缝与 unknown 那一臂一个字没动、也没造新帧名
# ==========================================================================

def _server_frame_names():
    """服务端**真发得出**的帧名全集，来源都不写在测试里：

    ① `stream_events` 的常量（CLIENT_FRAMES + NON_CLIENT_FRAMES——审批那一帧刻意不进
      CLIENT_FRAMES，理由写在模块那段注释里）；
    ② 发帧那三个文件里手打的 `{"type": "..."}`：v0.25 的连接级终帧 `cancelled` /
      `cannot_resume` 就住在这一档（它们是 test_process_trace_contract 那份 ledger 认下的
      既有欠账，不是本票新造的）。
    """
    names = set(se.CLIENT_FRAMES) | set(se.NON_CLIENT_FRAMES)
    for path in EMIT_SIDE:
        names |= set(re.findall(r'"type":\s*"([A-Za-z_]+)"', _text(path)))
    return names


def test_no_frame_name_outside_the_server_contract_appears_in_the_parser():
    """api.js 分发链里出现的每一个帧名，都必须是服务端真发得出的那一个。

    这条是"不造新帧名"的纪律有没有人守的机器判据：卡片状态走 GET，一旦有人图省事在这里
    加一臂 `waiting_user`，帧名就多了第二份真相——而两端各抄一遍 schema 正是本特性烂掉的
    方式。允许的集合从发帧侧现读，测试里不写第二份清单。
    """
    body = _strip_js_comments(_js_function(_api_source(), "runResilientStream"))
    literals = set(re.findall(r'type\s*===\s*"([A-Za-z_]+)"', body))
    assert literals, "这条锁自己空了：分发链里一个帧名都没读到"
    invented = literals - _server_frame_names()
    assert not invented, f"这些帧名服务端一个字都发不出来：{sorted(invented)}"


def test_the_on_event_seam_and_the_unknown_arm_are_still_there_and_untouched():
    """非内容帧仍从 onEvent 那条缝出去；认不出的帧仍只记一笔 unknown + 报一次 onUnknown。

    判据按**顺序 + 链尾 else**来，不按行号：行号会跟着无关改动漂，而"unknown 那一臂是不是
    最后一条"漂不了——往它前面再加一臂，就等于把已知帧记进未知上报那笔账。
    """
    flat = re.sub(r"\s+", " ", _strip_js_comments(
        _js_function(_api_source(), "runResilientStream")))
    seam = re.search(r'if \(type !== "content" && onEvent\) \{ try \{ onEvent\(evt\); \} '
                     r'catch \(_\) \{', flat)
    assert seam, "onEvent 那条缝不是原来那一句了：非内容帧的出口被动过"
    assert flat.index('if (type === "content") { if (onChunk) onChunk(evt.text') > seam.end(), \
        "正文不再是只进 onChunk 的那一条：老调用点的形状动了"
    assert re.search(r'else \{ unknown\.push\(evt\); if \(onUnknown\) onUnknown\(evt\); \} \}',
                     flat), "unknown 那一臂被挪了或往里加了东西：认不出的帧只能记一笔 + 报一次"
    assert '"approval_request"' in flat, "v0.31 的审批帧掉进 unknown 那一臂就是这里该红"


def test_the_app_side_event_handler_still_routes_only_through_the_existing_seam():
    """app.js 交给 streamChat 的 onEvent 仍只做两件事：审批帧画卡片、其余并进 trace。

    任务这一页不挂在这条缝上（卡片状态只认 GET）。这里钉它，是因为"往 onEvent 里塞一个
    waiting_user 分支"是本票最容易顺手做错的一步——那会把一份随进程没的缓冲当成真相，
    而症状正是"重启之后卡片没了"。

    判据是白名单而不是黑名单：这条缝认的帧名必须**恰好**是审批那一枚。但射程只到
    字面形状——只认 `onEvent` 体里直接写出来的 `evt.type === "…"` 比较，以及
    `TASK_FACE_FUNCS` 里列名函数的直接调用 `name(`。`switch (evt.type)`、`evt["type"]`、
    解构 `{ type }`、转给 helper、别名 / `forEach` 传递、表外新增的函数都不在射程；
    逐条列在文件头的「仍漏清单」里，别把这条读成"多认一枚就红 / 零调用边"的全称结论。
    """
    src = _strip_js_comments(_app_source())
    handler = re.search(r"onEvent:\s*\(evt\)\s*=>\s*\{([\s\S]*?)\n      \},", src)
    assert handler, "streamChat 的 onEvent 那一臂形状变了：这条锁要跟着改读法"
    body = handler.group(1)
    assert 'evt.type === "approval_request"' in body and "paintApproval(node, holder)" in body
    assert "accumulateTrace(holder, evt)" in body
    recognised = set(re.findall(r'evt\.type\s*===\s*"([A-Za-z_]+)"', body))
    assert recognised == {"approval_request"}, \
        f"onEvent 多认了帧：{sorted(recognised - {'approval_request'})}"
    for name in TASK_FACE_FUNCS + TASK_API_FUNCS:
        assert not re.search(rf"\b{name}\s*\(", body), f"onEvent 调到了任务面：{name}"


def test_the_task_face_consumes_no_frame_type_at_all():
    """任务面整组函数不认任何帧：不认识事件对象，也没有一处按 type 分发。

    判据是字面形状：任务面源码里没有 evt / onEvent / streamChat 这几个词，也没有
    `.type ===/== "…"` 或 `["type"] ===/== "…"` 这种直接比较。`switch (type)`、解构
    `{ type }`、把 type 交给别人再比都不在射程（见文件头「仍漏清单」）。反控是往
    onEvent 塞一个直接写 `evt.type === "task_refresh"` 的分支：上面那条白名单会红。
    """
    code = _strip_js_comments(_task_face_source())
    assert not re.search(r"\b(onEvent|evt|streamChat)\b", code), \
        "任务面挂到流事件上了：它的状态只该从 GET 来"
    dispatched = set(re.findall(r'(?:\.type|\["type"\])\s*===?\s*"([A-Za-z_]+)"', code))
    assert not dispatched, f"任务面在按帧名分发：{sorted(dispatched)}"


# ==========================================================================
# ③ 卡片与回答入口在 HTML/JS 里真存在且互相引用
# ==========================================================================

def test_the_card_skeleton_and_the_painter_point_at_each_other():
    """paintTaskCard 问的每一格都在 HTML 里；HTML 里每一格要么被填、要么有画法。

    思路照「解析出的东西必须真得被 UI 引用」那条既有判例，但只判本票这一块：留一格没人填
    = 界面上一个永远不动的空壳；JS 问一格不存在 = 静默失灵。跑的是真函数（假 DOM 只负责
    记下问了哪些选择器），所以"注释里写了这格"喂不绿它。
    """
    html = _strip_html_comments(_text(STATIC / "index.html"))
    skeleton = set(_classes_in(_card_block(html)))
    assert "msg-task" in skeleton, "卡片骨架不在 HTML 里"
    css = _text(STATIC / "style.css")

    driver = r"""
      const asked = [];
      const make = () => {
        const kids = {};
        const set = new Set();
        const node = {
          textContent: "",
          classes: set,
          classList: {
            toggle: (c, on) => { if (on) set.add(c); else set.delete(c); },
            add: (c) => set.add(c),
            contains: (c) => set.has(c),
          },
          querySelector: (sel) => {
            asked.push(sel);
            if (!kids[sel]) kids[sel] = make();
            return kids[sel];
          },
        };
        return node;
      };
      const pane = make();
      paintTaskCard(pane, SC.view);
      return { asked };
    """
    view = {"visible": True, "title": "需要你输入", "question": "请输入短信验证码",
            "progress": "第 2 / 3 步", "waiting": "等你回复", "canAnswer": True,
            "state": "需要你输入", "busy": False}
    out = _run_node(_task_module_source(), driver, {"view": view})
    queried = {s.lstrip(".") for s in out["asked"]}
    assert queried <= skeleton, f"卡片画了一格 HTML 里没有的：{sorted(queried - skeleton)}"
    dead = sorted(c for c in (skeleton - queried) if f".{c}" not in css)
    assert not dead, f"这些格子既没人填也没画法（死骨架）：{dead}"
    assert {"task-question", "task-answer", "task-card-head"} <= queried, \
        f"问题原文、回答入口或标题没被画：{sorted(queried)}"


XSS_PAYLOAD = '<img src=x onerror=alert(1)>'


def test_task_text_enters_the_dom_as_text_not_markup():
    """问题原文、列表里的 goal / meta 与错误条只走 textContent：写进去的是字符串，不是节点。

    假 DOM 的 innerHTML 会真的长出一颗 <img>，所以把任一写入点换成 innerHTML，这里会
    同时丢掉原文、多出一颗 img。射程只到本票新增的这几个写入点，不是全站 XSS 证明。
    meta 的恰等断言把"状态字 + 进度"的拼接也算进去：只数 img 的话，一个把文字拼丢的
    写入点会照样绿。
    """
    payload = XSS_PAYLOAD
    src = _with_dom(_task_module_source() + "\n" + "\n".join(
        _js_function(_app_source(), n) for n in ("setTaskError", "renderTaskList")))
    driver = r"""
      const pane = __node("div");
      paintTaskCard(pane, SC.view);
      const card = pane.querySelector(".msg-task");
      state.tasks = [{ task_id: "T1", goal: SC.payload, status: "running", progress: SC.payload }];
      state.taskOpenId = "";
      __els.taskList = __node("ul");
      __els.taskEmpty = __node("div");
      renderTaskList();
      setTaskError(SC.payload);
      const goal = __created.filter((n) => n.className === "task-goal")[0];
      const meta = __created.filter((n) => n.className === "task-meta-line")[0];
      return {
        imgs: __findImgs().length,
        head: card.querySelector(".task-card-head").textContent,
        question: card.querySelector(".task-question").textContent,
        progress: card.querySelector(".task-progress").textContent,
        waiting: card.querySelector(".task-waiting").textContent,
        state: card.querySelector(".task-state").textContent,
        goal: goal ? goal.textContent : null,
        goal_html: goal ? goal._html : null,
        meta: meta ? meta.textContent : null,
        meta_html: meta ? meta._html : null,
        meta_expected: taskStatusText("running") + " · " + SC.payload,
        err: __el("taskError").textContent,
        err_html: __el("taskError")._html,
      };
    """
    view = {"visible": True, "title": payload, "question": payload, "progress": payload,
            "waiting": payload, "canAnswer": True, "state": payload, "busy": False}
    out = _run_node(src, driver, {"payload": payload, "view": view})
    assert out["imgs"] == 0, "有写入点走了 innerHTML：注入串在 DOM 里长成了节点"
    for field in ("head", "question", "progress", "waiting", "state", "goal", "err"):
        assert out[field] == payload, f"{field} 写入后不是原文：{out[field]!r}"
    assert out["meta"] == out["meta_expected"], \
        f"meta 不是「状态字 · 进度」的原文：{out['meta']!r}"
    assert out["goal_html"] == "" and out["meta_html"] == "" and out["err_html"] == "", \
        f"列表或错误条按 markup 写进去了：{out['goal_html']!r} / {out['meta_html']!r} / {out['err_html']!r}"


def test_the_state_line_is_visible_whenever_it_has_something_to_say():
    """状态句有内容时必须摘掉 #taskState 的 hidden；.busy 不许把回答表单收走。

    HTML 里这一格默认 hidden、style.css 又是 display:none，只写 textContent 等于没写。
    跑真 paintTaskCard：有句子时无 hidden，空句子时仍有 hidden；busy 那张卡的表单也
    必须在场（可以压暗/禁用，但不能消失）。
    """
    css = _text(STATIC / "style.css")
    assert not re.search(r"\.msg-task\.busy[^{]*\{[^}]*display:\s*none", css), \
        ".busy 又把回答表单 display:none 收走了：提交后输入框会凭空消失"
    driver = r"""
      const pane = __node("div");
      paintTaskCard(pane, SC.with_state);
      const card = pane.querySelector(".msg-task");
      const stateEl = card.querySelector(".task-state");
      const withState = { hidden: stateEl.classList.contains("hidden"),
                          text: stateEl.textContent,
                          card_hidden: card.classList.contains("hidden") };
      const busyPane = __node("div");
      paintTaskCard(busyPane, SC.busy);
      const busyCard = busyPane.querySelector(".msg-task");
      const busy = { form_hidden: busyCard.querySelector(".task-answer").classList.contains("hidden"),
                     state_hidden: busyCard.querySelector(".task-state").classList.contains("hidden"),
                     state_text: busyCard.querySelector(".task-state").textContent,
                     busy_class: busyCard.classList.contains("busy") };
      paintTaskCard(pane, SC.empty_view);
      const empty = { hidden: stateEl.classList.contains("hidden"), text: stateEl.textContent };
      return { withState, busy, empty };
    """
    base = {"visible": True, "title": "需要你输入", "question": "验证码", "progress": "",
            "waiting": "", "canAnswer": False}
    scenario = {
        "with_state": dict(base, state="执行中", busy=False),
        "busy": dict(base, state="执行中", busy=True),
        "empty_view": {"visible": False, "title": "", "question": "", "progress": "",
                       "waiting": "", "canAnswer": False, "state": "", "busy": False},
    }
    out = _run_node(_with_dom(_task_module_source()), driver, scenario)
    assert out["withState"] == {"hidden": False, "text": "执行中", "card_hidden": False}, out["withState"]
    assert out["busy"]["state_hidden"] is False and out["busy"]["state_text"] == "执行中", out["busy"]
    assert out["busy"]["form_hidden"] is False, "提交在途时回答表单被收起了"
    assert out["busy"]["busy_class"] is True
    assert out["empty"] == {"hidden": True, "text": ""}, out["empty"]


def test_the_nav_entry_and_the_answer_input_are_wired():
    """侧栏那颗「任务」与卡片里那张表单都得既有元素、又有绑定——缺一边就是点了没反应。"""
    html = _strip_html_comments(_text(STATIC / "index.html"))
    js = _strip_js_comments(_app_source())
    assert 'data-page="tasks"' in html, "任务那一页不在设置二级页的清单里"
    for el in ("navTask", "taskAnswerForm", "taskAnswerInput", "taskAnswerBtn", "taskRefresh"):
        assert f'id="{el}"' in html, f"index.html 里没有 {el}"
        assert f'$("{el}")' in js, f"app.js 里没有 $({el})：这个元素要么没接线要么已删"
    assert re.search(r'\$\("taskAnswerForm"\)\.onsubmit\s*=', js), "答案表单没绑提交"
    assert re.search(r'\$\("navTask"\)\.onclick\s*=\s*\(\)\s*=>\s*\{\s*openSettings\("tasks"\)',
                     js), "侧栏那颗没落到 tasks 这一页"
    assert re.search(r'const SET_PAGES = \{[^}]*tasks: "[^"]+"', js, flags=re.S), \
        "SET_PAGES 少了 tasks 这一条：openSetPage 会把标题写成 undefined"


def test_the_card_reads_keys_the_server_actually_answers(isolated_tasks):
    """渲染读的键必须真是 GET /v1/tasks/{id} 答得出的那些，以及游标里那些。

    本票禁改后端，而 `waiting_*` 那一族被 A5 做 == 全等断言、加不了键——于是前端只能用
    现有键。这条把"用现有键"从口头承诺变成判据：详情端点的顶层键从 main.py 现读，
    游标键与那三个数由 task_store 的真函数产出，不是测试手搓的形状。
    """
    ts = isolated_tasks
    task = _parked(ts)
    cursor_keys = set(task.resume_cursor)
    info_keys = set(ts.waiting_timeout_info(task))

    src = _text(MAIN_PY)
    endpoint = _python_block(src, "@app.get(\"/v1/tasks/{task_id}\")")
    top_keys = set(re.findall(r'^\s{8}"(\w+)":', endpoint, flags=re.M))
    assert "resume_cursor" in top_keys, f"详情端点不再回游标了：{sorted(top_keys)}"

    js_src = _app_source()
    rendered = "\n".join(_js_function(js_src, n) for n in
                         ("taskProgressLine", "taskWaitingLine", "taskStatusLine", "taskCardView"))
    detail_keys = set(re.findall(r"detail\.(\w+)", rendered))
    cursor_read = set(re.findall(r"cursor\.(\w+)", rendered))
    assert {"question", "subtask_index", "subtask_total"} <= cursor_keys, sorted(cursor_keys)
    assert {"waiting_expires_at", "waiting_timeout_seconds", "waiting_elapsed_seconds"} <= info_keys
    assert {"status", "resume_cursor", "waiting_expires_at"} <= detail_keys, sorted(detail_keys)
    assert cursor_read <= cursor_keys, f"渲染读了游标里没有的键：{sorted(cursor_read - cursor_keys)}"
    allowed = top_keys | info_keys | {"progress_percent", "subtask_index", "subtask_total",
                                      "waiting_since"}
    assert detail_keys <= allowed, f"渲染读了这些服务端没答的顶层键：{sorted(detail_keys - allowed)}"


def _python_block(src: str, decorator: str) -> str:
    """从装饰器那一行起，截出那个函数体（按缩进走到下一颗顶格行为止）。"""
    at = src.index(decorator)
    body_start = src.index("\n", src.index("):", at))
    end = body_start + 1
    while end < len(src):
        nl = src.find("\n", end)
        line = src[end:nl if nl > 0 else len(src)]
        if line.strip() and not line.startswith((" ", "\t", ")")):
            break
        end = nl + 1
    return src[body_start:end]


# ==========================================================================
# ④ 倒计时默认形态 / 回答在途 / 非属主 404
# ==========================================================================

def test_default_window_renders_no_countdown_at_all(isolated_tasks):
    """默认窗口无可数之物：不说数字、不说"无限"，只说「等你回复」。

    Infinity 与 0 走同一条渲染路径——这件事在服务端已经归好了（`_window_seconds` 把非有限
    值与坏值收成 0，于是 `waiting_expires_at` 恒为 None），前端只需认那一格，不许自己再造
    一个"无限"状态。非正的剩余同理：坏窗口只在前端自造输入时可达，而这一页判不了窗口坏
    没坏、服务端还在等，就不替它说「时限已过」。阳性对照：真给了到期时刻就必须说出还剩
    多久，否则这条只是恒真。
    """
    src = _app_source()
    waiting_text = _js_const(src, "TASK_WAITING_TEXT")
    driver = ("return { none: taskWaitingLine(SC.no_window, SC.t0), "
              "past: taskWaitingLine(SC.past, SC.t0), "
              "neg5: taskWaitingLine(SC.neg5, SC.t0), "
              "soon: taskWaitingLine(SC.soon, SC.t0) };")
    t0 = 1_700_000_000_000          # 2023-11-14T22:13:20Z
    scenario = {
        "no_window": {"status": "waiting_user", "waiting_expires_at": None,
                      "waiting_timeout_seconds": 0.0},
        "past": {"status": "waiting_user", "waiting_expires_at": "2023-11-14T22:12:20Z"},
        "neg5": {"status": "waiting_user", "waiting_expires_at": "2023-11-14T22:13:15Z"},
        "soon": {"status": "waiting_user", "waiting_expires_at": "2023-11-14T22:16:20Z"},
        "t0": t0,
    }
    out = _run_node(_task_module_source(), driver, scenario)
    assert out["none"] == waiting_text, out["none"]
    assert not re.search(r"\d", out["none"]), f"默认那一格说出了数字：{out['none']!r}"
    assert "无限" not in out["none"] and "∞" not in out["none"], out["none"]
    assert out["past"] == waiting_text and out["neg5"] == waiting_text, out
    assert not re.search(r"\d", out["neg5"]), f"负窗口说出了数字或旧文案：{out['neg5']!r}"
    assert out["soon"] != waiting_text, out
    assert re.search(r"\d", out["soon"]), f"真给了到期时刻却不说还剩多久（阳性对照）：{out['soon']!r}"

    ts = isolated_tasks
    task = _parked(ts)
    assert ts.waiting_deadline(task) is None, "默认窗口下服务端该答不出到期时刻"
    for bad in (0, -5, float("inf"), "五分钟"):
        assert ts.waiting_deadline(task, timeout_seconds=bad) is None, bad
    assert ts.waiting_deadline(task, timeout_seconds=600) is not None, "阳性对照：正窗口该算得出"


def test_a_positive_sub_second_window_still_says_one_second_not_no_deadline():
    """正数剩余一律向上取整到秒：0.4 / 0.5 / 0.9 / 1.0 秒都说「1 秒内回复」。

    round 在 0.5 秒处换口径：0.4 秒被舍成 0、说成「等你回复」（那是"没有时限"那句话），
    0.5 秒却被进成「1 秒内回复」。同一个倒计时不许在 0.4/0.5 两边各说一套，所以取整
    统一向上：只有剩余真到 0 才说「等你回复」。
    """
    waiting_text = _js_const(_app_source(), "TASK_WAITING_TEXT")
    t0 = 1_700_000_000_000          # 2023-11-14T22:13:20Z
    scenario = {
        "t0": t0,
        "zero": {"status": "waiting_user", "waiting_expires_at": "2023-11-14T22:13:20.000Z"},
        "s04": {"status": "waiting_user", "waiting_expires_at": "2023-11-14T22:13:20.400Z"},
        "s05": {"status": "waiting_user", "waiting_expires_at": "2023-11-14T22:13:20.500Z"},
        "s09": {"status": "waiting_user", "waiting_expires_at": "2023-11-14T22:13:20.900Z"},
        "s10": {"status": "waiting_user", "waiting_expires_at": "2023-11-14T22:13:21.000Z"},
    }
    driver = ("return { zero: taskWaitingLine(SC.zero, SC.t0), p4: taskWaitingLine(SC.s04, SC.t0),"
              " p5: taskWaitingLine(SC.s05, SC.t0), p9: taskWaitingLine(SC.s09, SC.t0),"
              " one: taskWaitingLine(SC.s10, SC.t0) };")
    out = _run_node(_task_module_source(), driver, scenario)
    assert out["zero"] == waiting_text, f"真到点了却还在倒计时：{out['zero']!r}"
    for key in ("p4", "p5", "p9", "one"):
        assert out[key] == "1 秒内回复", f"{key} 的剩余不足或恰为 1 秒，却说了 {out[key]!r}"


def test_the_card_goes_executing_and_advances_only_by_reading():
    """提交之后先落「执行中」，推进靠再读一次 GET，不靠那条连接活着。

    这一枪是同步 def 的长阻塞 POST：跑完（或再次挂回断点）才回话，断连时服务端照跑。
    所以「执行中」只能是界面此刻的一句话（taskCardView 的 busy 入参），它**不许**盖掉
    卡片其余几格的内容来源——那些仍来自 detail。顺带钉在途时答案键收起来：同一枚断点
    只许一个答案接走，双击送出去的是两枪。
    """
    driver = "return { idle: taskCardView(SC.detail, SC.idle), busy: taskCardView(SC.detail, SC.opts) };"
    detail = {"task_id": "T1", "status": "waiting_user", "goal": "买票", "can_resume": True,
              "resume_cursor": {"question": "请输入短信验证码",
                                "subtask_index": 1, "subtask_total": 3}}
    out = _run_node(_task_module_source(), driver,
                    {"detail": detail, "idle": {"now": 1_700_000_000_000},
                     "opts": {"busy": True, "now": 1_700_000_000_000}})
    executing = _js_const(_app_source(), "TASK_EXECUTING_TEXT")
    assert out["idle"]["state"] != executing, out["idle"]
    assert out["busy"]["state"] == executing, out["busy"]
    assert out["busy"]["question"] == out["idle"]["question"] == "请输入短信验证码", out
    assert out["busy"]["visible"] and out["idle"]["visible"], "waiting_user 就该有这张卡片"
    assert out["idle"]["progress"] == "第 2 / 3 步", out["idle"]
    assert out["idle"]["canAnswer"] is True and out["busy"]["canAnswer"] is False, out

    # 在途那一句之后必须"再读一次"：提交函数的收尾是 openTaskDetail，不是本地猜状态。
    body = _js_function(_app_source(), "submitTaskAnswer")
    assert re.search(r"finally \{[\s\S]*openTaskDetail\(", body), \
        "提交完没有再读 GET：那等于把卡片的状态交给这条连接的死活"
    assert "API.answerTask" in body and "goal" in body


def test_a_404_is_not_a_network_error_and_nothing_retries_it():
    """非属主与不存在的 id 是同一句 404：说成"这条打不开"，不弹断网卡片，也不重试。

    下面几件一起做：① `taskErrorLine` 在 404 与"请求根本没出设备"（fetch 抛 TypeError）上给
    两句不同的话；② 任务面每一处 `NetMinder.note(e)` 都被 404 挡在前面；③ 这一页没有定时
    器、没有递归重打——断网那句话由 api.js 的 NetMinder 说一次，剩下的都等人再点一次。
    最后真跑一次 api.js：404 必须带着 status 抛出来，否则上面那句"说成读不到"没有输入。
    """
    gone = _js_const(_app_source(), "TASK_GONE_TEXT")
    driver = ("return { gone: taskErrorLine(SC.not_found), "
              "net: taskErrorLine(new TypeError(\"Failed to fetch\")), "
              "other: taskErrorLine(SC.ratelimited), none: taskErrorLine(null) };")
    out = _run_node(_task_module_source(), driver, {
        "not_found": {"status": 404, "message": "任务不存在"},
        "ratelimited": {"status": 429, "message": "慢一点再说"},
    })
    assert out["gone"] == gone, out["gone"]
    assert "连不上" not in out["gone"], f"404 被说成断网：{out['gone']!r}"
    assert "连不上" in out["net"], f"真的连不上设备时该说连不上：{out['net']!r}"
    assert out["net"] != out["gone"], "404 与断网共用一句话，人就分不清该等还是该敲"
    assert "慢一点" in out["other"], out["other"]
    assert out["none"] == "", out["none"]

    for name in ("loadTasks", "openTaskDetail", "submitTaskAnswer"):
        body = _js_function(_app_source(), name)
        guarded = re.search(r"if \(!\(e && e\.status === 404\)[\s\S]{0,60}?NetMinder\.note\(e\)",
                            body)
        assert guarded or "NetMinder.note" not in body, f"{name} 里有没被 404 挡住的断网提醒"
        for forbidden in ("setTimeout", "setInterval", "location.reload", "for (;;)"):
            assert forbidden not in body, f"{name} 里有 {forbidden}：这一页不许自己造心跳或重试"

    out2 = _run_node(_api_source(), r"""
      globalThis.fetch = async () => ({
        ok: false, status: 404, statusText: "Not Found",
        headers: { get: () => null },
        json: async () => ({ detail: "任务不存在" }),
      });
      try { await API.getTask("someone-elses-task"); return { threw: null }; }
      catch (e) { return { threw: { status: e.status, message: e.message } }; }
    """)
    assert out2["threw"] and out2["threw"]["status"] == 404, out2
    assert out2["threw"]["message"] == "任务不存在", out2


def test_the_card_title_and_the_waiting_gate_are_the_agreed_words():
    """卡片标题那句是「需要你输入」，而这张卡片只在 waiting_user 那一态出现。

    这不是文案洁癖：`can_resume` 是布尔字段、不是状态字面量（服务端 A1 的不变式），
    所以"这一态"与"这一步真能接住一句话"是两件事——非等待态不该出现一个按得动的答案键。
    """
    assert _js_const(_app_source(), "TASK_CARD_TITLE") == "需要你输入"
    driver = "return { rows: SC.cases.map((c) => taskCardView(c, { now: 1700000000000 })) };"
    cases = [
        {"status": "waiting_user", "can_resume": True,
         "resume_cursor": {"question": "要收货地址", "subtask_index": 0, "subtask_total": 2}},
        {"status": "running"},
        {"status": "cancelled", "end_reason": "cancelled_by_timeout"},
        {"status": "completed", "final_answer": "办好了"},
    ]
    out = _run_node(_task_module_source(), driver, {"cases": cases})
    rows = out["rows"]
    assert rows[0]["visible"] is True and rows[0]["title"] == "需要你输入", rows[0]
    assert [r["visible"] for r in rows[1:]] == [False] * 3, rows[1:]
    assert [r["canAnswer"] for r in rows[1:]] == [False] * 3, rows[1:]
    assert "等回复" in rows[2]["state"], f"超时作废被说成用户自己取消：{rows[2]}"


# ==========================================================================
# ⑤ 换身份：上一个人的答案、错误与在途标记都不许留在这一页
# ==========================================================================

def _identity_source() -> str:
    """换身份那条路上真正会跑的几段：任务渲染 + 错误条 + resetViewForIdentity。"""
    app = _app_source()
    return _with_dom(_task_module_source() + "\n" + "\n".join(
        _js_function(app, n) for n in ("renderTaskList", "setTaskError", "resetViewForIdentity")))


def test_switching_identity_leaves_none_of_the_previous_answer_in_the_dom():
    """换人时输入串、错误条、在途标记必须当场复位，并重画成干净的空卡。

    答案框里常是短信验证码。这里先铺一个"上一个人正在回答"的现场（输入串 + 错误条 +
    busy 卡 + 在途标记），跑真 resetViewForIdentity，再把整棵假 DOM 的文本与 value 扫一遍。
    """
    driver = r"""
      state.messages = ["上一个人的对话"];
      state.tasks = [{ task_id: "T-A", goal: "上一个人的任务" }];
      state.taskDetail = { task_id: "T-A", status: "waiting_user", can_resume: true,
                           resume_cursor: { question: "上一个人的问题",
                                            subtask_index: 1, subtask_total: 2 } };
      state.taskOpenId = "T-A";
      state.taskAnswering = true;
      state.taskEpoch = 7;
      __el("taskAnswerInput").value = SC.secret;
      __el("taskAnswerInput").disabled = true;
      __el("taskAnswerBtn").disabled = true;
      __el("taskError").textContent = SC.err;
      __el("taskError").classList.toggle("hidden", false);
      const pane = __el("paneTasks");
      paintTaskCard(pane, taskCardView(state.taskDetail, { busy: true }));
      const before = pane.querySelector(".msg-task").classList.contains("busy");
      resetViewForIdentity();
      const card = pane.querySelector(".msg-task");
      const seen = [];
      __created.forEach((n) => { seen.push(String(n.textContent || ""), String(n.value || "")); });
      return {
        before,
        input: __el("taskAnswerInput").value,
        input_disabled: __el("taskAnswerInput").disabled,
        err: __el("taskError").textContent,
        err_hidden: __el("taskError").classList.contains("hidden"),
        answering: state.taskAnswering,
        epoch: state.taskEpoch,
        card_hidden: card.classList.contains("hidden"),
        card_busy: card.classList.contains("busy"),
        state_hidden: card.querySelector(".task-state").classList.contains("hidden"),
        leak: seen.filter((t) => t.includes(SC.secret) || t.includes(SC.err)),
      };
    """
    out = _run_node(_identity_source(), driver, {"secret": "554433", "err": "慢一点再说"})
    assert out["before"] is True, "现场没铺成 busy 卡，这条锁等于没跑"
    assert out["input"] == "" and out["input_disabled"] is False, out
    assert out["err"] == "" and out["err_hidden"] is True, out
    assert out["answering"] is False and out["epoch"] == 8, out
    assert out["card_hidden"] is True and out["card_busy"] is False, out
    assert out["state_hidden"] is True, out
    assert out["leak"] == [], f"换人后 DOM 里还找得到上一个人的输入/错误：{out['leak']}"


def test_an_answer_in_flight_cannot_write_into_the_new_identity_page():
    """长阻塞的那一枪在换人之后回来：错误条、重读、输入框清空都不许发生。

    这是复核现场的另一半——旧请求回来会把新用户的卡片重新置忙，或把旧任务的报错写进
    新页面。resetViewForIdentity 把 taskEpoch +1，收尾对不上就整段丢掉。
    """
    driver = r"""
      let rejectIt;
      const pending = new Promise((_, reject) => { rejectIt = reject; });
      const calls = [];
      API.answerTask = () => pending;
      API.getTask = async () => { calls.push("getTask"); return null; };
      API.listTasks = async () => { calls.push("listTasks"); return { tasks: [] }; };
      state.taskDetail = { task_id: "T-A", goal: "买票", status: "waiting_user", can_resume: true,
                           resume_cursor: { question: "验证码", subtask_index: 0, subtask_total: 1 } };
      state.taskEpoch = 3;
      __el("taskAnswerInput").value = "123456";
      const run = submitTaskAnswer();
      resetViewForIdentity();
      rejectIt({ status: 429, message: "慢一点再说" });
      await run;
      return { err: __el("taskError").textContent,
               err_hidden: __el("taskError").classList.contains("hidden"),
               answering: state.taskAnswering, calls,
               input: __el("taskAnswerInput").value,
               card_busy: __el("paneTasks").querySelector(".msg-task").classList.contains("busy") };
    """
    app = _app_source()
    tail = "\n".join(_js_function(app, n) for n in
                     ("paintTasks", "openTaskDetail", "loadTasks", "submitTaskAnswer"))
    out = _run_node(_identity_source() + "\n" + tail, driver)
    assert out["err"] == "" and out["err_hidden"] is True, out
    assert out["calls"] == [], f"换人后旧请求还去重读了任务：{out['calls']}"
    assert out["answering"] is False and out["card_busy"] is False, out
    assert out["input"] == "", out


def test_no_in_flight_reply_can_land_on_the_new_identity_page():
    """换身份之后，任何在途请求的结果都不许写进新页面——复核现场的三枪各跑一遍。

    ① 回答 POST 已返回、finally 里那次重读 GET 在途时换号：A 的详情与问题一个字都不许
      出现，答案表单必须还收着；② 列表 GET 在途时换号：state.tasks 保持空，也不许接着
      去打开任何一条；③ A→B→A：A 的第一枪回来时 epoch 已经 +2，同样不许写。判据落在
      落地结果上（state 与 DOM），不是"代码里有没有 await"。
    """
    app = _app_source()
    source = _identity_source() + "\n" + "\n".join(
        _js_function(app, n) for n in
        ("paintTasks", "openTaskDetail", "loadTasks", "submitTaskAnswer"))
    gate = ("const gate = () => { let resolve, reject;"
            " const p = new Promise((res, rej) => { resolve = res; reject = rej; });"
            " return { p, resolve, reject }; };")
    detail = {"task_id": "T-A", "goal": "买票", "status": "waiting_user", "can_resume": True,
              "resume_cursor": {"question": "A 的验证码问题",
                                "subtask_index": 0, "subtask_total": 1}}
    row = {"task_id": "T-A", "goal": "A 的买票任务", "status": "waiting_user"}

    out1 = _run_node(source, gate + r"""
      const post = gate(), reread = gate();
      const calls = [];
      API.answerTask = () => { calls.push("answer"); return post.p; };
      API.getTask = (id) => { calls.push("get:" + id); return reread.p; };
      API.listTasks = async () => { calls.push("list"); return { tasks: [] }; };
      state.taskDetail = SC.detail;
      __el("taskAnswerInput").value = "123456";
      paintTaskCard(__el("paneTasks"), taskCardView(state.taskDetail, {}));
      const run = submitTaskAnswer();
      post.resolve({ ok: true });
      await new Promise((r) => setTimeout(r, 0));
      const inFlight = calls.slice();
      resetViewForIdentity();
      reread.resolve(SC.detail);
      await run;
      await new Promise((r) => setTimeout(r, 0));
      const card = __el("paneTasks").querySelector(".msg-task");
      const seen = [];
      __created.forEach((n) => seen.push(String(n.textContent || "")));
      return { in_flight: inFlight, calls, detail: state.taskDetail, tasks: state.tasks,
               open_id: state.taskOpenId, epoch: state.taskEpoch,
               question: card.querySelector(".task-question").textContent,
               card_hidden: card.classList.contains("hidden"),
               form_hidden: card.querySelector(".task-answer").classList.contains("hidden"),
               input: __el("taskAnswerInput").value, err: __el("taskError").textContent,
               leak: seen.filter((t) => t.includes(SC.question)) };
    """, {"detail": detail, "question": detail["resume_cursor"]["question"]})

    out2 = _run_node(source, gate + r"""
      const list = gate();
      const calls = [];
      API.listTasks = () => { calls.push("list"); return list.p; };
      API.getTask = async (id) => { calls.push("get:" + id); return null; };
      const run = loadTasks();
      await new Promise((r) => setTimeout(r, 0));
      const inFlight = calls.slice();
      resetViewForIdentity();
      list.resolve({ tasks: [SC.row] });
      await run;
      await new Promise((r) => setTimeout(r, 0));
      return { in_flight: inFlight, calls, tasks: state.tasks, detail: state.taskDetail,
               open_id: state.taskOpenId, epoch: state.taskEpoch };
    """, {"row": row})

    out3 = _run_node(source, gate + r"""
      const list = gate();
      const calls = [];
      API.listTasks = () => { calls.push("list"); return list.p; };
      API.getTask = async (id) => { calls.push("get:" + id); return null; };
      const run = loadTasks();
      await new Promise((r) => setTimeout(r, 0));
      resetViewForIdentity();
      resetViewForIdentity();
      list.resolve({ tasks: [SC.row] });
      await run;
      await new Promise((r) => setTimeout(r, 0));
      return { calls, tasks: state.tasks, detail: state.taskDetail,
               open_id: state.taskOpenId, epoch: state.taskEpoch };
    """, {"row": row})

    problems = []
    if out1["in_flight"] != ["answer", "get:T-A"]:
        problems.append(f"① 现场没铺成：换号时重读 GET 不在途（calls={out1['in_flight']}）")
    if out1["detail"] is not None or out1["question"] or out1["leak"]:
        problems.append(f"① 旧详情写进了新页面：detail={out1['detail']!r} "
                        f"question={out1['question']!r} leak={out1['leak']!r}")
    if not out1["card_hidden"] or not out1["form_hidden"]:
        problems.append(f"① 换号后卡片 / 答案表单还露着：card_hidden={out1['card_hidden']} "
                        f"form_hidden={out1['form_hidden']}")
    if out1["calls"] != ["answer", "get:T-A"]:
        problems.append(f"① 换号后旧请求还接着发：{out1['calls']}")
    if [out1["tasks"], out1["open_id"], out1["epoch"], out1["input"], out1["err"]] != \
            [[], "", 1, "", ""]:
        problems.append(f"① 换号后的本机状态没停干净：tasks={out1['tasks']!r} "
                        f"open_id={out1['open_id']!r} epoch={out1['epoch']} "
                        f"input={out1['input']!r} err={out1['err']!r}")

    if out2["in_flight"] != ["list"]:
        problems.append(f"② 现场没铺成：列表 GET 不在途（calls={out2['in_flight']}）")
    if out2["tasks"] != []:
        problems.append(f"② 换号后旧列表写进了 state.tasks：{out2['tasks']!r}")
    if [out2["detail"], out2["open_id"], out2["epoch"]] != [None, "", 1]:
        problems.append(f"② 换号后旧列表还去挑了一条：detail={out2['detail']!r} "
                        f"open_id={out2['open_id']!r} epoch={out2['epoch']}")
    if out2["calls"] != ["list"]:
        problems.append(f"② 换号后旧请求还接着打开详情：{out2['calls']}")

    if [out3["tasks"], out3["detail"], out3["open_id"], out3["epoch"]] != [[], None, "", 2]:
        problems.append(f"③ A→B→A 之后旧请求仍写进了页面：tasks={out3['tasks']!r} "
                        f"detail={out3['detail']!r} open_id={out3['open_id']!r} "
                        f"epoch={out3['epoch']}")
    if out3["calls"] != ["list"]:
        problems.append(f"③ A→B→A 之后旧请求还接着发：{out3['calls']}")

    assert not problems, "换号后旧请求仍写进了新页面：\n" + "\n".join(problems)


def test_this_feature_grew_no_new_static_file():
    """这一页用到的四份文件都还在 SW 的外壳清单里 —— 新功能一律长在既有文件里。

    两份预缓存清单的既有锁是**单向**的（只判"清单里每条都取得到"），新增一个静态文件而不
    写进 sw.js 时两边都不红，那是 09-17 新旧混发的复现形状。反向核对归 #138，本票只做一件
    力所能及的事：证明自己这一页没有把内容搬进一个没被清单罩着的文件。
    """
    sw = _text(STATIC / "sw.js")
    listed = set(re.findall(r'"([^"]+)"', sw.split("const NAV = [", 1)[1].split("]", 1)[0]))
    listed |= set(re.findall(r'"([^"]+)"', sw.split("const ASSETS = [", 1)[1].split("]", 1)[0]))
    for name in ("index.html", "app.js", "api.js", "style.css"):
        assert name in listed, f"{name} 不在 SW 的外壳清单里：离线时它要回源，新旧混发"
        assert (STATIC / name).is_file()
