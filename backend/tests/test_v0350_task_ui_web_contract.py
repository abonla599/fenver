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
    """
    src = _strip_js_comments(_app_source())
    handler = re.search(r"onEvent:\s*\(evt\)\s*=>\s*\{([\s\S]*?)\n      \},", src)
    assert handler, "streamChat 的 onEvent 那一臂形状变了：这条锁要跟着改读法"
    body = handler.group(1)
    assert 'evt.type === "approval_request"' in body and "paintApproval(node, holder)" in body
    assert "accumulateTrace(holder, evt)" in body
    for not_here in ("waiting_user", "getTask", "resume_cursor", "orchestrate", "answerTask"):
        assert not_here not in body, f"任务状态被塞进流的那条缝里了：{not_here}"


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
    一个"无限"状态。阳性对照：真给了到期时刻就必须说出还剩多久，否则这条只是恒真。
    """
    src = _app_source()
    waiting_text = _js_const(src, "TASK_WAITING_TEXT")
    driver = ("return { none: taskWaitingLine(SC.no_window, SC.t0), "
              "past: taskWaitingLine(SC.past, SC.t0), "
              "soon: taskWaitingLine(SC.soon, SC.t0) };")
    t0 = 1_700_000_000_000          # 2023-11-14T22:13:20Z
    scenario = {
        "no_window": {"status": "waiting_user", "waiting_expires_at": None,
                      "waiting_timeout_seconds": 0.0},
        "past": {"status": "waiting_user", "waiting_expires_at": "2023-11-14T22:12:20Z"},
        "soon": {"status": "waiting_user", "waiting_expires_at": "2023-11-14T22:16:20Z"},
        "t0": t0,
    }
    out = _run_node(_task_module_source(), driver, scenario)
    assert out["none"] == waiting_text, out["none"]
    assert not re.search(r"\d", out["none"]), f"默认那一格说出了数字：{out['none']!r}"
    assert "无限" not in out["none"] and "∞" not in out["none"], out["none"]
    assert out["past"] != waiting_text and out["soon"] != waiting_text, out
    assert re.search(r"\d", out["soon"]), f"真给了到期时刻却不说还剩多久（阳性对照）：{out['soon']!r}"

    ts = isolated_tasks
    task = _parked(ts)
    assert ts.waiting_deadline(task) is None, "默认窗口下服务端该答不出到期时刻"
    for bad in (0, -5, float("inf"), "五分钟"):
        assert ts.waiting_deadline(task, timeout_seconds=bad) is None, bad
    assert ts.waiting_deadline(task, timeout_seconds=600) is not None, "阳性对照：正窗口该算得出"


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
