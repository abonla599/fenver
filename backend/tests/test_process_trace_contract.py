"""v0.29.0「过程留痕」（思考 / 工具调用 / 搜索到的网页）的跨端契约锁。

这个文件只管三件事，按代价从大到小排：

1. **单一真源真的只有一处**。帧名、字段、预算、构造全在 `app/core/stream_events.py`；
   本文件的判据也一律**从这个模块现读**（`se.CLIENT_FRAMES`、各 `*_frame`/`step_*` 的产出、
   `se._URL_SAFE_SCHEMES`、`se.TRACE_STEPS_MAX`/`TRACE_CHARS_MAX`），不写第二份字面量。
   写死的副本本身就是这个特性最贵的失败模式：四处各抄一遍 schema，改一处漏三处，而绿着
   的测试会替漂移背书。
2. **双端齐认**。同一帧名必须在服务端构造、网页分发、原生分发三处对得上；一边认得、另一边
   不认得是静默的体验分裂。本仓库的先例（`test_netminder_contract.py`）就是拿 Python 把
   JS/Kotlin **源文本当判据**读——CI 里没有 JS 与 Kotlin 测试运行器。能真跑的我们真跑：
   网页侧的 `runResilientStream`/`accumulateTrace`/`traceSafeUrl` 都是纯函数，交给 node
   执行仓库里那一份（先例见 `test_web_pwa.py` 的 `_run_stream_js`）。
3. **这条特性把攻击者可控的文本送进了界面**。搜索结果的标题/摘要/网址出自别人写的网页，
   思考文本可能复述用户隐私，过程步会落进 `sessions.json`。于是 URL 白名单、步数/字数预算、
   "客户端不互信服务端那道过滤"、"谁都不许把过程原文打进日志"都得有可红的判据。

一条例外先说清楚：`main.py` 里 `run.append({"type": "start"/"content"/"error", ...})` 是本
特性之前就存在的字面量，`test_frame_names_are_never_hand_written_on_the_emit_side` 现在会红
在那里。**这是有意的**：那三行正是"四处各抄一遍 schema"的现场；红着，比写一份 xfail 或把
判据放宽成"只查新增帧"诚实。
"""
import ast
import inspect
import json
import re
import shutil
import subprocess
import tempfile
import tokenize
from pathlib import Path

import pytest

from app.core import stream_events as se
from app.session.session_store import SessionStore

REPO_ROOT = Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "backend" / "app"
EMIT_SIDE = (APP / "main.py", APP / "core" / "streaming.py")   # 服务端发帧的两处
STATIC = APP / "web" / "static"
KOTLIN = (REPO_ROOT / "android-native" / "app" / "src" / "main" / "java"
          / "xyz" / "fenever" / "assistant" / "nativeapp")

FRAME_NAMES = set(se.CLIENT_FRAMES)


# --------------------------------------------------------------------------
# 语料读法（先把尺子定清楚，再写判据）
# --------------------------------------------------------------------------

def _text(path: Path) -> str:
    assert path.is_file(), f"契约的另一端不在仓库里：{path}"
    return path.read_text(encoding="utf-8")


def _string_token(tok):
    """tokenize 的 STRING token → Python 值；f-string 之类读不出常量就回 None（本就不算字面量）。"""
    try:
        return ast.literal_eval(tok.string)
    except Exception:
        return None


def _frame_type_literals_in(path: Path):
    """这个文件里**手写成字面量**的帧 `type` 值：[(行号, 帧名)]。

    只认真实代码里的两种形状：`{"type": "start"}` 与 `d["type"] = "start"`。走 tokenize
    而不是一来：① 注释与 docstring 天然是整块 STRING/COMMENT token，不会被误判成代码
    （否则"解释为什么不该写字面量"的那句注释会自己把锁喂绿）；② 变量拼出来的值不是字面量。
    """
    with path.open("rb") as fh:
        toks = [t for t in tokenize.tokenize(fh.readline)
                if t.type not in (tokenize.NEWLINE, tokenize.NL, tokenize.COMMENT,
                                  tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER)]
    hits = []
    for i in range(len(toks) - 1):
        a, b = toks[i], toks[i + 1]
        if a.type != tokenize.STRING or _string_token(a) != "type":
            continue
        if b.type == tokenize.OP and b.string == ":":
            # {"type": "start"}：值必须在同一行、且是个字符串字面量
            if i + 2 < len(toks) and toks[i + 2].type == tokenize.STRING:
                value = _string_token(toks[i + 2])
                if value in FRAME_NAMES:
                    hits.append((toks[i + 2].start[0], value))
        elif (b.type == tokenize.OP and b.string == "]"
              and i + 3 < len(toks) and toks[i + 2].type == tokenize.OP
              and toks[i + 2].string == "=" and toks[i + 3].type == tokenize.STRING):
            # d["type"] = "start"
            value = _string_token(toks[i + 3])
            if value in FRAME_NAMES:
                hits.append((toks[i + 3].start[0], value))
    return hits


def _js_function(src: str, name: str) -> str:
    """`[async] function name(...) { ... }` 整段原文（含 async 前缀，大括号配平收尾）。

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


def _kotlin_body(src: str, anchor: str, label: str) -> str:
    """从 anchor 起做大括号配平，截出那一块**代码**（把搜索面收到真分支上）。

    不整文件 grep 的理由：整文件里 `"start" ->` 这种串也可能出现在注释里；限定在函数体内，
    冒充成"处理过了"的成本就高到没人愿意付。
    """
    at = src.find(anchor)
    assert at >= 0, f"{label}：找不到锚点 {anchor!r}——那一处结构变了，锁得跟着改读法"
    open_at = src.index("{", at)
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at:i + 1]
    raise AssertionError(f"{label}：大括号没闭合")


def _kotlin_header(src: str, anchor: str, label: str) -> str:
    """data class / 函数签名那一截：按**圆括号**配平（这类声明体里没有大括号，用不上上一条）。"""
    at = src.find(anchor)
    assert at >= 0, f"{label}：找不到锚点 {anchor!r}"
    open_at = src.index("(", at)
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[at:i + 1]
    raise AssertionError(f"{label}：圆括号没闭合")


# --------------------------------------------------------------------------
# node 执行器：跑的是仓库里那一份 JS，不是抄写的副本
# --------------------------------------------------------------------------

_NODE_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const fns = fs.readFileSync(process.argv[2], "utf8");
const driver = fs.readFileSync(process.argv[3], "utf8");
const sc = fs.readFileSync(process.argv[4], "utf8");
const sandbox = { console, JSON, Math, Object, Array, String, Number, Boolean, Error,
                  Symbol, Set, Map, RegExp, Promise, TextDecoder, TextEncoder, setTimeout };
vm.createContext(sandbox);
const script = "const SC = " + sc + ";\n" + fns
  + "\nconst __drive = async () => {\n" + driver + "\n};\n__drive();";
const p = vm.runInContext(script, sandbox);
p.then((r) => process.stdout.write(JSON.stringify(r)),
       (e) => { console.error(e); process.exit(2); });
"""


def _run_node(fn_sources: str, driver: str, scenario: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    d = Path(tempfile.mkdtemp(prefix="trace-contract-"))
    (d / "fns.js").write_text(fn_sources, encoding="utf-8")
    (d / "driver.js").write_text(driver, encoding="utf-8")
    (d / "sc.json").write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
    (d / "harness.cjs").write_text(_NODE_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(d / "harness.cjs"), str(d / "fns.js"),
                        str(d / "driver.js"), str(d / "sc.json")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"harness 自己就跑失败了：\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout)


def _full_flow_frames():
    """八类帧各来一帧，帧体全部由契约模块现场构造（不抄字面量）。"""
    rows = [{"title": "西安 百科", "url": "https://baike.example/xian", "snippet": "常住人口一千多万"}]
    return [
        se.start_frame("m-1", "fake-chat"),
        se.thinking_frame("先乘后加"),
        se.tool_call_frame("c_1", "calculator", {"expression": "17*24+12"}, "计算 17*24+12"),
        se.tool_result_frame("c_1", "calculator", True, "✓ 420", elapsed_ms=12),
        se.search_frame("c_1", "西安 人口", rows),
        se.content_frame("答案是 420"),
        se.done_frame("答案是 420", "m-1", "fake-chat", [se.step_thinking("先乘后加")]),
    ]


_JS_STREAM_DRIVER = r"""
const enc = new TextEncoder();
const NL = String.fromCharCode(10);
function blockText(b) {
  let s = "id: " + b.id + NL;
  if (b.data !== undefined && b.data !== null) s += "data: " + JSON.stringify(b.data) + NL;
  return s + NL;
}
const eventTypes = [], chunks = [], unknownTypes = [];
function open() {
  const blocks = SC.frames.map(blockText);
  let i = 0;
  return Promise.resolve({ ok: true, status: 200, body: { getReader() {
    return { read() {
      if (i < blocks.length) {
        const v = enc.encode(blocks[i]); i += 1;
        return Promise.resolve({ value: v, done: false });
      }
      return Promise.resolve({ value: undefined, done: true });
    } };
  } } });
}
let threw = null, doneType = null, doneTraceLen = null;
try {
  const r = await runResilientStream({
    open: open, sleep: () => Promise.resolve(), maxAttempts: 1, resumeStatus: "rs",
    onChunk: (t) => { chunks.push(t); },
    onEvent: (e) => { eventTypes.push(e && e.type); },
    onUnknown: (e) => { unknownTypes.push(e && e.type); },
  });
  doneType = (r && r.done) ? r.done.type : null;
  doneTraceLen = (r && r.done && Array.isArray(r.done.trace)) ? r.done.trace.length : null;
} catch (e) {
  threw = { message: String((e && e.message) || e), kind: (e && e.kind) || null,
            retryable: (e && e.retryable) === undefined ? null : e.retryable };
}
return { eventTypes: eventTypes, chunks: chunks.join(""), unknownTypes: unknownTypes,
         doneType: doneType, doneTraceLen: doneTraceLen, threw: threw };
"""


def _run_web_stream(frames):
    """把 frames 当成 SSE 字节流喂给 api.js 里那一份 `runResilientStream`。"""
    api_src = _text(STATIC / "api.js")
    scenario = {"frames": [{"id": f"R:{i + 1}", "data": f} for i, f in enumerate(frames)]}
    return _run_node(_js_function(api_src, "runResilientStream"), _JS_STREAM_DRIVER, scenario)


def _run_web_app_js(names, driver, scenario):
    """在 node 里跑 app.js 里的那几个纯函数（accumulateTrace / traceSafeUrl …）。"""
    app_src = _text(STATIC / "app.js")
    return _run_node("\n".join(_js_function(app_src, n) for n in names), driver, scenario)


# --------------------------------------------------------------------------
# 契约自身的形状（下面几条锁共用的读法）
# --------------------------------------------------------------------------

def _contract_constructors():
    """扫出契约模块里所有 `*_frame` 构造器并各调一次（不手写清单：漏一条就少钉一条帧）。"""
    out = []
    for name in sorted(dir(se)):
        obj = getattr(se, name)
        if not callable(obj) or name.startswith("_") or not name.endswith("_frame"):
            continue
        params = inspect.signature(obj).parameters
        required = [p for p in params.values() if p.default is inspect.Parameter.empty]
        try:
            frame = obj(*["x"] * len(required))
        except TypeError:
            continue
        if isinstance(frame, dict):
            out.append((name, frame))
    return out


def _trace_key() -> str:
    """历史里"过程留痕"的字段名：从**存储与 done 帧两边各现读一次并要求相等**。

    这个名字的唯一出处就是这一段：下面所有锁都拿它去比客户端，不在测试里写第二遍 "trace"。
    """
    plain = SessionStore._entry("assistant", "正文")
    stored = SessionStore._entry("assistant", "正文", trace=[{"kind": "thinking", "text": "想"}])
    keys = set(stored) - set(plain)
    assert len(keys) == 1, f"_entry 收了 trace 却说不清多出来的是哪颗键：{sorted(keys)}"
    persisted = keys.pop()
    step = [{"kind": "thinking", "text": "想"}]
    done_keys = set(se.done_frame("x", "m", "mm", step)) - set(se.done_frame("x", "m", "mm"))
    assert done_keys == {persisted}, \
        f"done 里带回过程的字段名 {sorted(done_keys)} 与落盘字段 {persisted!r} 不是同一个词"
    return persisted


def _omitted_kind() -> str:
    """compact_trace 记账用的那个 kind：从一次真的溢出里读出来，不写死 "omitted"。"""
    steps = [se.step_tool(f"c{i}", "calculator", f"调用 {i}", True, "结果")
             for i in range(se.TRACE_STEPS_MAX + 30)]
    kinds_in = {s["kind"] for s in steps}
    kinds_out = {s["kind"] for s in se.compact_trace(steps)}
    extra = kinds_out - kinds_in
    assert len(extra) == 1, f"被丢的步没记进任何一个新 kind：{sorted(extra)}"
    return extra.pop()


# ==========================================================================
# 1. 单一真源
# ==========================================================================

def test_frame_names_are_never_hand_written_on_the_emit_side():
    """服务端发帧的这两处只许用 stream_events 的常量当帧名，一个字面量都不许手打。

    为什么这条值得为它红一次：帧的形状以前只以字面量散在端点里，客户端两边各抄一份
    （api.js 的 if 链、Api.kt 的 when 臂），于是加一帧要同时改四处。**四处各抄一遍 schema
    就是这个特性烂掉的方式**：改一处漏三处，症状不是报错而是"某一端看不见"，而绿着的
    测试会替它背书。这一条把"唯一真源"从模块文档里的口号变成可执行的判据。
    """
    offenders = []
    for path in EMIT_SIDE:
        for line, value in _frame_type_literals_in(path):
            offenders.append(f'{path.relative_to(REPO_ROOT)}:{line} 手打了 {{"type": "{value}"}}')
    assert not offenders, (
        "帧名只能来自 backend/app/core/stream_events.py 的常量，别处写死一份就是第二份真相。\n"
        + "\n".join(offenders)
        + '\n改法：se.start_frame(...) / se.content_frame(...) / se.error_frame(...)；'
          "要加字段就 {**se.done_frame(...), \"status\": ...} 这样并上去。")


def test_new_hand_written_frame_literals_have_to_be_declared_in_the_ledger():
    """发帧侧之外的手打帧名是一份**只能缩短**的欠账清单，新冒出来的当场红。

    上面那条只扫 `main.py`/`streaming.py`（本特性的发帧侧）。这条扫整个 `app/`：任何人在
    别的模块再手打一个帧名（续播那条连接级终帧就是现成的例子），都得先在这里登记一行并
    写清为什么忍——把"又多抄了一份 schema"变成一次显式表态。
    """
    ledger = {
        # 既有欠账（v0.25 R3b 留的，不属于本特性）：读者追不上服务端缓冲时补发的终帧。
        # 修好了请把这行删掉；清单只该越还越少。
        ("core/stream_runs.py", se.FRAME_DONE),
    }
    found = set()
    for path in sorted(APP.rglob("*.py")):
        if path.name == "stream_events.py" or path in EMIT_SIDE:
            continue
        for _line, value in _frame_type_literals_in(path):
            found.add((path.relative_to(APP).as_posix(), value))
    extra = found - ledger
    assert not extra, (
        f"这些位置手打了帧名而没登记：{sorted(extra)}。登记请写清为什么忍，"
        "否则改成 stream_events 的构造器。")
    missing = ledger - found
    assert not missing, (
        f"清单里这几条已经不在了（{sorted(missing)}）——好事，把 ledger 里对应那行删掉。")


def test_client_frames_is_exactly_the_names_the_contract_can_build():
    """FRAME_* 常量、`CLIENT_FRAMES` 清单、各 `*_frame` 构造器的产出，三者必须一模一样。

    钉两个方向：① 加了 FRAME_X 常量却忘了列进 CLIENT_FRAMES——客户端契约测试读的是
    CLIENT_FRAMES，于是新帧永远不会被钉到；② 写了构造器但它的 type 不在清单里——服务端
    会发出一条"两端都不认的帧"，轻则死重量，重则把两端"未知帧上报"刷满噪声。
    """
    constant_values = {getattr(se, name) for name in dir(se)
                       if name.startswith("FRAME_") and isinstance(getattr(se, name), str)}
    assert constant_values == FRAME_NAMES, (
        f"FRAME_* 常量与 CLIENT_FRAMES 对不上：常量侧 {sorted(constant_values)}，"
        f"清单侧 {sorted(FRAME_NAMES)}")
    assert len(se.CLIENT_FRAMES) == len(FRAME_NAMES), f"CLIENT_FRAMES 里有重复项：{se.CLIENT_FRAMES}"

    built = _contract_constructors()
    assert built, "一个 *_frame 构造器都没扫到——这条锁自己空了"
    produced = {frame["type"] for _name, frame in built}
    assert produced <= FRAME_NAMES, \
        f"这些构造器发出的 type 不在 CLIENT_FRAMES 里：{sorted(produced - FRAME_NAMES)}"
    assert FRAME_NAMES <= produced, \
        f"CLIENT_FRAMES 里这几条帧没有对应构造器：{sorted(FRAME_NAMES - produced)}"


def test_the_four_pre_existing_frame_skeletons_are_untouched():
    """start/content/done/error 的既有键一个不许动，`trace` 只能是**新增的可选键**。

    老客户端就按这几颗键取数据（api.js 的 onChunk 只吃 content.text，原生 Done 读
    full_text/message_id/model）。改名字不是"新客户端跟着改就完事"，是线上老包当场读空。
    """
    assert set(se.start_frame("m", "mm")) == {"type", "message_id", "model"}
    assert set(se.content_frame("t")) == {"type", "text"}
    assert set(se.error_frame("m")) == {"type", "message"}
    plain_done = se.done_frame("full", "m", "mm")
    assert set(plain_done) == {"type", "full_text", "message_id", "model"}, \
        f"done 的既有骨架动了：{sorted(plain_done)}"
    assert _trace_key() not in plain_done, "没有留痕的时候不该凭空多挂一个空 trace 键"
    rich_done = se.done_frame("full", "m", "mm", [se.step_thinking("想")])
    assert set(plain_done) <= set(rich_done), "带 trace 的 done 不该改掉任何旧键"


def test_sse_framing_survives_newline_in_attacker_controlled_text():
    """搜索标题/摘要是别人写的网页内容，里面带换行也不许把 SSE 的分帧顶穿。

    一帧里只要出现裸换行，读侧就会把后半截当成另一帧（或直接断流）：第三方文本于是能
    伪造协议边界、凭空造出一条 `done` 来。序列化靠 `sse_data` 那一份默认 ensure_ascii 的
    json.dumps 兜住，这里钉的是**结果**，不是"记得转义"那句口头承诺。
    """
    nasty = se.search_frame("c", "q", [{"title": '标题\n\ndata: {"type":"done"}',
                                       "url": "https://e.example/a",
                                       "snippet": "摘要\r\n再来一行"}])
    wire = se.sse_data(nasty)
    assert wire.startswith("data: ") and wire.endswith("\n\n"), wire
    assert "\n" not in wire[:-2], "帧体里有裸换行：第三方文本能伪造 SSE 分帧"
    assert "\r" not in wire[:-2]
    # 分隔符口径也是契约：tests/test_stream_api.py 锁的是冒号后带空格的那个形状
    assert '"type": ' in se.sse_data(se.error_frame("m")), se.sse_data(se.error_frame("m"))


# ==========================================================================
# 2. 双端齐认
# ==========================================================================

def test_the_web_parser_acks_every_frame_the_server_can_send():
    """真跑仓库里那份 api.js：八类帧一类都不许掉进"看不懂的帧"那一堆。

    判据来自运行时而不是 grep。把契约模块现场构造的帧按线上字节形态喂进去，要求
    ①非内容帧全部从 onEvent 那条缝交出去（那是过程面板的唯一入口），②正文只进 onChunk
    （老调用点的形状不许变），③unknown 为空——api.js 的 else 分支收的是"契约之外冒出来
    的东西"，已知帧掉进去等于这一端其实不认它，只是没崩而已。
    """
    frames = _full_flow_frames()
    out = _run_web_stream(frames)
    expect_events = [f["type"] for f in frames if f["type"] != se.FRAME_CONTENT]
    assert out["eventTypes"] == expect_events, out
    # error 不在这一份"走得完的流程"里：它一出现就把流掐断（下一条锁单独钉它），
    # 于是这里的"全帧覆盖"只能拿除 error 之外的帧来比——少了哪一帧照样红。
    assert set(out["eventTypes"]) | {se.FRAME_CONTENT, se.FRAME_ERROR} == FRAME_NAMES, \
        f"网页这一侧有帧没送到 onEvent：{sorted(FRAME_NAMES - set(out['eventTypes']))}"
    assert out["unknownTypes"] == [], f"这些帧被网页当成契约之外的东西：{out['unknownTypes']}"
    assert out["chunks"] == "答案是 420", out
    assert out["doneType"] == se.FRAME_DONE, out
    assert out["doneTraceLen"] == 1, f"done 带回来的 trace 没原样交出去：{out}"


def test_the_web_parser_treats_error_as_a_failure_not_a_silent_frame():
    """error 帧仍然要"这一轮失败了"：抛出去、且不标 retryable。

    retryable=true 的老写法会让上层把整轮原文重发回 /v1/chat 再付一次钱（v0.25 R3 的判据）。
    同时钉 error 也走 onEvent：过程面板得知道流是在哪一步断的。
    """
    frame = se.error_frame("模型调用失败：上游 502")
    out = _run_web_stream([se.start_frame("m", "mm"), frame])
    assert out["threw"], f"error 帧没被当成失败：{out}"
    assert out["threw"]["message"] == frame["message"], out["threw"]
    assert out["threw"]["retryable"] is False, out["threw"]
    assert se.FRAME_ERROR in out["eventTypes"], out


def test_the_native_parser_has_one_branch_per_frame_and_each_maps_to_its_own_event():
    """原生侧八条帧一条一臂，且各自落到**不同的** ChatEvent 子类。

    sealed class 的穷尽性是这条链唯一的编译器护栏（加帧不加臂编不过）；这一条钉护栏管不到的
    另一半：臂加了、却和别的帧共用一个事件类——那是静默的 UX 分裂，服务端发了、界面按另一种
    东西画，症状是"过程看着在动但说不清是哪一步"。
    """
    body = _kotlin_body(_text(KOTLIN / "Api.kt"), "when (val type =", "Api.kt emitFrame 的 when")
    emitted = {}
    for name in se.CLIENT_FRAMES:
        branch = re.search(r'"%s"\s*->' % re.escape(name), body)
        assert branch, f'原生解析器没有 "{name}" 这一臂：服务端发出去它就掉进 else'
        tail = body[branch.end():]
        nxt = re.search(r'\n\s*"[^"]+"[^\n]*->|\n\s*else\s*->', tail)
        arm = tail[:nxt.start()] if nxt else tail
        classes = re.findall(r"ChatEvent\.(\w+)", arm)
        assert classes, f'"{name}" 那一臂没产出任何 ChatEvent（只剩注释或空实现）'
        emitted[name] = classes[0]
    dupes = sorted(n for n, c in emitted.items() if list(emitted.values()).count(c) > 1)
    assert not dupes, f"这些帧共用了同一个事件类型，界面上分不出彼此：{dupes}"
    assert "ChatEvent.Unknown" in body, \
        "未知帧的兜底臂没了：以后加帧时老包会静默丢掉而不是上报一次"


def test_the_process_frames_are_actually_consumed_by_both_ui_layers():
    """三条新帧不止要能被解析，还得真的进面板：一端接住、另一端丢掉就是体验分裂。

    原生那侧的事件类名**从 Api.kt 的 when 臂现读**（不写死 Thinking/ToolCall 这份清单），
    再要求 ChatUi.kt 引用它——于是"解析器造了个没人画的事件"也会红，而那正是半成品最常见
    的形状。网页侧对着 accumulateTrace 的分支判：帧并进不了步，面板上就没有这一步。
    """
    api_src = _text(KOTLIN / "Api.kt")
    body = _kotlin_body(api_src, "when (val type =", "Api.kt emitFrame 的 when")
    ui_src = _text(KOTLIN / "ui" / "ChatUi.kt")
    for name in (se.FRAME_THINKING, se.FRAME_TOOL_CALL, se.FRAME_TOOL_RESULT, se.FRAME_SEARCH):
        branch = re.search(r'"%s"\s*->' % re.escape(name), body)
        assert branch, f"原生没有 {name} 臂"
        cls = re.findall(r"ChatEvent\.(\w+)", body[branch.end():])[0]
        assert f"ChatEvent.{cls}" in ui_src, \
            f"原生把 {name} 解成了 ChatEvent.{cls}，但界面层从不读它：这一帧白发"

    reducer = _js_function(_text(STATIC / "app.js"), "accumulateTrace")
    for name in (se.FRAME_THINKING, se.FRAME_TOOL_CALL, se.FRAME_TOOL_RESULT, se.FRAME_SEARCH):
        assert f'evt.type === "{name}"' in reducer, \
            f"网页 accumulateTrace 不认 {name} 帧：过程面板上不会有这一步"


def test_the_web_reducer_builds_exactly_the_steps_the_server_persists():
    """网页把帧并成"步"，步的字段名要与服务端落盘那一份逐一对齐——回放成立的前提。

    真跑 accumulateTrace：连续 thinking 并成一条、tool_call 与 tool_result 靠 id 并成同一条、
    search 一条。然后拿契约模块的 `step_*` 当尺子比字段名：谁改了名（summary→text 之类）而
    另一边没改，客户端 PUT 回写的历史就再也认不出服务端那一版，"重开会话还看得见过程"当场
    失效——而且不报错，只是过程没了。最后把这批步交给真存储 `_entry` 走一遍，确认一条都不丢。
    """
    frames = [se.thinking_frame("先乘"), se.thinking_frame("后加"),
              se.tool_call_frame("c_1", "calculator", {"expression": "1+1"}, "计算 1+1"),
              se.tool_result_frame("c_1", "calculator", True, "✓ 2", elapsed_ms=8),
              se.search_frame("c_9", "西安", [{"title": "百科", "url": "https://e.example/a",
                                              "snippet": "人口"}])]
    driver = r"""
      const holder = { trace: [] };
      const changed = [];
      for (const f of SC.frames) changed.push(accumulateTrace(holder, f));
      return { steps: holder.trace, changed: changed };
    """
    out = _run_web_app_js(["accumulateTrace"], driver, {"frames": frames})
    steps = out["steps"]
    want_kinds = [se.step_thinking("x")["kind"],
                  se.step_tool("i", "n", "l", True, "s")["kind"],
                  se.step_search("i", "q", [{"title": "t", "url": "https://e.example/a"}])["kind"]]
    assert [s["kind"] for s in steps] == want_kinds, \
        f"帧并成步的形状变了：{[s['kind'] for s in steps]} != {want_kinds}"
    assert all(out["changed"]), f"每条过程帧都该改动 trace（含并段那两条 thinking）：{out['changed']}"

    assert set(steps[0]) == set(se.step_thinking("x")), steps[0]
    assert set(steps[1]) == set(se.step_tool("c_1", "calculator", "l", True, "s")), steps[1]
    assert set(steps[2]) == set(se.step_search("c_9", "q", None)), steps[2]
    assert set(steps[2]["results"][0]) == {"title", "url", "snippet"}, steps[2]

    key = _trace_key()
    entry = SessionStore._entry("assistant", "正文", trace=steps)
    assert entry is not None and len(entry[key]) == len(steps), \
        f"网页自己并出来的步服务端不认（存进去就少几条）：{entry}"
    assert entry[key][1]["id"] == "c_1" and entry[key][1]["ok"] is True, entry[key][1]


def test_the_history_field_name_is_the_same_on_server_and_both_clients():
    """历史里"过程"的字段名三端必须是一个词。对不上的后果不是报错，是每次保存都把过程擦掉。

    三处各读各的：服务端 `_entry` 多出来的那颗键（`_trace_key()` 已钉过它与 done 一致）、
    网页整份回写时挂上的键、原生 StoredMessage 的字段与 PUT 时 put 的键。
    """
    key = _trace_key()

    write_back = _js_function(_text(STATIC / "app.js"), "replaceMessages")
    assert f"out.{key} = trace" in write_back or f'out["{key}"]' in write_back, \
        f"网页整份回写没带 {key}：PUT 是全量替换，不带等于用「没有过程」覆盖服务端那一份"

    api_src = _text(KOTLIN / "Api.kt")
    stored_header = _kotlin_header(api_src, "data class StoredMessage", "Api.kt StoredMessage")
    assert re.search(r"\bval\s+%s\b" % re.escape(key), stored_header), \
        f"原生 StoredMessage 没有 {key} 字段：重开会话时服务端落好的过程读不回来"
    put_region = _kotlin_body(api_src, "suspend fun replaceMessages", "Api.kt replaceMessages")
    assert f'put("{key}"' in put_region, \
        f"原生回写没带 {key}：同一个坑，只是换了个客户端"

    params = inspect.signature(SessionStore.add_message).parameters
    assert key in params, f"add_message 的入参名不是 {key}，落盘这条链在服务端就断了"


# ==========================================================================
# 3. 安全：第三方文本与无界磁盘
# ==========================================================================

_UNSAFE_URLS = [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "JAVASCRIPT:alert(document.cookie)",
    "  javascript:alert(1)  ",
    "\tjavascript:alert(1)",
    "\njeScriPt:alert(1)",
    "java\tscript:alert(1)",
    "javascript&colon;alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "DATA:text/html;base64,PHN2Zz4=",
    "  data:,hello",
    "file:///etc/passwd",
    "FILE:///C:/Windows/win.ini",
    "vbscript:msgbox(1)",
    "mailto:someone@example.com",
    "//protocol-relative.example/a",
    "",
    "   ",
]


def test_the_contract_blanks_every_url_that_is_not_plain_http():
    """`_safe_url` 只放行白名单协议，大小写/空白/编码变体一律抹成空串。

    表驱动，因为这一层的价值全在"变体也想到了"：`JAVASCRIPT:`、前导空白、中间一个制表符，
    任何一个绕过 `startswith` 都等于把第三方文本升格成可执行入口。放行侧从
    `se._URL_SAFE_SCHEMES` 现读，不复制清单。
    """
    for url in _UNSAFE_URLS:
        assert se._safe_url(url) == "", f"这条不该被当成链接：{url!r}"
    assert se._safe_url(None) == "", "None 得能过（服务端字段缺失是常态）"
    for scheme in se._URL_SAFE_SCHEMES:
        kept = se._safe_url(scheme + "example.com/a")
        assert kept == scheme + "example.com/a", f"正常来源被误杀：{kept!r}"
    # 白名单本身也要有闸：往 _URL_SAFE_SCHEMES 里塞 javascript:// 会一路绿到线上
    assert set(se._URL_SAFE_SCHEMES) <= {"http://", "https://"}, se._URL_SAFE_SCHEMES


def test_search_frame_caps_count_and_length_and_drops_empty_rows():
    """一帧里最多 SEARCH_RESULTS_MAX 条、网址按 SEARCH_URL_MAX 封顶、标题与网址都空的行丢掉。

    第三方可以一次给几百条命中、每条一个超长 URL；不封顶就是让别人的一句话撑爆连接与磁盘。
    空行那条是可用性判据：面板上出现一行能点却没有内容的东西，比少一条结果更可疑。
    """
    rows = [{"title": f"标题{i}", "url": "https://e.example/a", "snippet": "摘要"}
            for i in range(se.SEARCH_RESULTS_MAX + 9)]
    rows.append({"title": "", "url": "", "snippet": "什么可点的都没有"})
    frame = se.search_frame("c", "q", rows)
    assert len(frame["results"]) <= se.SEARCH_RESULTS_MAX, len(frame["results"])
    assert all(len(r["url"]) <= se.SEARCH_URL_MAX for r in frame["results"])
    assert all(not (r["title"] == "" and r["url"] == "") for r in frame["results"]), \
        "标题与网址都空的行留在了帧里：面板上就是一行死空白"
    huge = se._safe_url("https://e.example/" + "a" * (se.SEARCH_URL_MAX * 3))
    assert 0 < len(huge) <= se.SEARCH_URL_MAX, "超长正常网址要封顶而不是整条抹掉"


def test_the_web_client_checks_search_urls_itself():
    """网页自己判一遍网址协议：服务端那道过滤不算信任前提（纵深防御）。

    真跑 app.js 的 `traceSafeUrl`，判据是"服务端抹掉的东西客户端也必须判成空串"（单向包含：
    两边写法本来不同，服务端还封顶 500 字符）。哪天有人删掉服务端那一层，这里不替它背书；
    哪天客户端退回"给什么就当链接画"，这里当场红。
    """
    cases = _UNSAFE_URLS + ["https://e.example/a", "http://e.example/b"]
    out = _run_web_app_js(["traceSafeUrl"],
                          "return { urls: SC.urls.map(traceSafeUrl) };", {"urls": cases})
    assert len(out["urls"]) == len(cases), out
    for url, js_result in zip(cases, out["urls"]):
        server = se._safe_url(url)
        if server == "":
            assert js_result == "", f"服务端判死的链接网页又当链接画了：{url!r} → {js_result!r}"
        else:
            assert js_result != "", f"正常来源被客户端误杀成纯文本：{url!r}"


def test_the_native_client_checks_search_urls_itself():
    """原生同样自校验，而且放行清单与服务端白名单是同一组协议（从模块现读）。

    Kotlin 在 CI 里没有测试运行器，只能读源码：判据钉在 `safeTraceUrl` 这一段函数体里（不是
    整文件 grep），并要求它落到"其余一律回空串"——`else ""` 那半句才是这条闸门的本体，少写
    就等于 `javascript:` 一路走到 Intent。
    """
    body = _kotlin_body(_text(KOTLIN / "ui" / "ChatUi.kt"),
                        "private fun safeTraceUrl", "ChatUi.kt safeTraceUrl")
    for scheme in se._URL_SAFE_SCHEMES:
        assert f'"{scheme}"' in body, f"原生这一侧没放行 {scheme}：与服务端白名单长岔了"
    assert re.search(r'else\s*""', body), "不放行的协议必须回空串，而不是原样交给 onOpenUrl"
    assert "lowercase()" in body or "toLowerCase()" in body, \
        "只比原样前缀会被 JAVASCRIPT: 绕过（大小写不敏感是判据的一部分）"
    assert "trim()" in body, "前导空白的变体会绕过 startsWith"


def test_omitted_steps_are_persisted_as_the_omitted_kind_and_counted():
    """被丢的步一律并成 `{"kind": 那个记账 kind, "count": N}`，而且只有一条。

    判据从模块现读：kind 由一次真溢出反推（`_omitted_kind()`），预算数字取
    `TRACE_STEPS_MAX`。这条钉的是"省略要说明省略了多少"——静默裁剪的过程留痕比没有留痕更坏，
    用户会以为那就是全过程。
    """
    omitted = _omitted_kind()
    steps = [se.step_tool(f"c{i}", "calculator", f"调用 {i}", True, "结果")
             for i in range(se.TRACE_STEPS_MAX + 30)]
    out = se.compact_trace(steps)
    marks = [s for s in out if s.get("kind") == omitted]
    assert len(marks) == 1, f"省略标记应当只有一条：{marks}"
    kept_real = [s for s in out if s.get("kind") != omitted]
    assert marks[0]["count"] == len(steps) - len(kept_real), \
        f"记的账对不上：标了 {marks[0]['count']}，实际丢了 {len(steps) - len(kept_real)}"
    assert out[0] == steps[0] and out[-1] == steps[-1], \
        "被裁的不是中间步：首尾两条（开始与收尾那一步）必须照旧在"
    assert len(kept_real) <= se.TRACE_STEPS_MAX


def test_compact_trace_never_returns_fewer_than_two_real_steps():
    """喂得再多再长，落盘的 trace 至少留住两条**真过程**：留痕不能变成"什么都不剩"。

    三条用例各钉一种溢出：步数溢出、字数溢出、以及畸形输入（不是字典 / 没有 kind 的步）。
    前两条要求裁完之后还剩至少两条真步；最后一条要求畸形项一个都不落到 sessions.json——
    裁到什么程度是策略，一条都不剩是事故，而"剩没剩真东西"只能靠排除记账标记来判。
    """
    omitted = _omitted_kind()
    cases = {
        "步数溢出": [se.step_tool(f"c{i}", "calculator", f"调用 {i}", True, "结果")
                     for i in range(se.TRACE_STEPS_MAX * 3)],
        "字数溢出": [se.step_thinking("长" * se.THINKING_TOTAL_MAX) for _ in range(12)],
        "只有垃圾": [{"kind": ""}, "不是字典", {"没有 kind": 1},
                     se.step_tool("c1", "calculator", "调用 1", True, "结果"),
                     se.step_tool("c2", "calculator", "调用 2", True, "结果"),
                     se.step_thinking("收尾想一想")],
    }
    for label, steps in cases.items():
        valid = [s for s in steps if isinstance(s, dict) and s.get("kind")]
        out = se.compact_trace(steps)
        assert all(isinstance(s, dict) and s.get("kind") for s in out), \
            f"{label}：畸形步没被挡住，直接落盘：{out}"
        real = [s for s in out if s.get("kind") != omitted]
        assert all(s in valid for s in real), f"{label}：产出的是没见过的步：{real}"
        assert len(real) >= 2, f"{label}：compact_trace 只剩 {out}，两条真过程都没留住"
        assert len(real) >= min(len(valid), 2), f"{label}：连喂进去的少量真步都裁没了"
        assert len(out) - len(real) <= 1, f"{label}：记账标记不止一条：{out}"


def test_oversized_thinking_is_trimmed_before_the_evidence_steps():
    """字数超预算时先牺牲最长的那条（大段独白），留住工具与搜索的证据。

    这条钉的是 compact_trace 那一刀**切在哪**：按"排在前面"裁会留下最没信息量的部分，
    而用户回看时真正要核对的是"它查了什么、算没算成"。所以构造一份"5 条满额思考 + 少量
    工具/搜索步"的 trace，要求证据一条不少、思考先被丢。
    """
    omitted = _omitted_kind()
    think_kind = se.step_thinking("x")["kind"]
    tool_step = se.step_tool("c_1", "calculator", "计算 1+1", True, "✓ 2", elapsed_ms=5)
    search_step = se.step_search("c_2", "西安 人口",
                                 [{"title": "百科", "url": "https://e.example/a", "snippet": "人口"}])
    tool_kind, search_kind = tool_step["kind"], search_step["kind"]
    steps = ([se.step_thinking("想" * se.THINKING_TOTAL_MAX) for _ in range(5)]
             + [tool_step, search_step])
    out = se.compact_trace(steps)
    kinds = [s["kind"] for s in out]
    assert kinds.count(tool_kind) == 1 and kinds.count(search_kind) == 1, \
        f"证据步被裁掉了：{kinds}"
    assert kinds.count(think_kind) < 5, f"先该丢的是长思考：{kinds}"
    assert se._trace_chars(out) <= se.TRACE_CHARS_MAX or len(out) <= 2, \
        f"裁完还在预算之外（{se._trace_chars(out)} > {se.TRACE_CHARS_MAX}）：磁盘预算是空的"
    assert any(s["kind"] == omitted for s in out), "丢了步却不记账，等于静默裁剪"


def test_the_client_cannot_pump_more_trace_onto_disk_than_the_budget_allows():
    """客户端 PUT 回写的 trace 要再裁一次：磁盘不是对面想写多大就多大。

    `_entry` 这条路径同时吃服务端算出的 trace 和客户端整份回写来的 trace；不重裁就等于把
    "一条消息能在 sessions.json 里塞多少字"交给客户端。
    """
    key = _trace_key()
    omitted = _omitted_kind()
    # 每条摘要都顶到服务端封顶后的上限：这样一份 trace 同时越过步数预算与字数预算，
    # 两刀都得落下——只钉一条的话，另一条预算被删掉也照样绿。
    steps = [se.step_tool(f"c{i}", "calculator", f"调用 {i}", True, "结果" * 240)
             for i in range(se.TRACE_STEPS_MAX + 80)]
    assert se._trace_chars(steps) > se.TRACE_CHARS_MAX, "用例本身没构造出溢出，判据是空的"
    entry = SessionStore._entry("assistant", "正文", trace=steps)
    real = [s for s in entry[key] if s.get("kind") != omitted]
    assert len(real) <= se.TRACE_STEPS_MAX, len(real)
    assert se._trace_chars(entry[key]) <= se.TRACE_CHARS_MAX or len(real) <= 2, \
        se._trace_chars(entry[key])
    assert any(s.get("kind") == omitted for s in entry[key]), "裁剪没记账"


def test_the_persisted_history_round_trips_through_a_real_store(tmp_path):
    """落盘回放这条链真的走得通：存进去的 trace 重开一次还在，整份回写也不把它抹平。

    这是本特性对用户的承诺（重开会话还看得见过程），而它靠三段字段名一致才成立——
    拿真 SessionStore（临时文件）跑一遍 add_message → get → replace → get。
    """
    key = _trace_key()
    store = SessionStore(path=str(tmp_path / "sessions.json"))
    created = store.create("fake-chat", owner="someone")
    sid = created["session_id"]
    steps = [se.step_thinking("先乘后加"),
             se.step_tool("c_1", "calculator", "计算 17*24+12", True, "✓ 420", elapsed_ms=9),
             se.step_search("c_2", "西安 人口",
                            [{"title": "百科", "url": "https://e.example/a", "snippet": "人口"}])]
    assert store.add_message(sid, "someone", "assistant", "答案是 420",
                             "m-1", [], **{key: steps})
    reloaded = store.get(sid, "someone")
    assert [s["kind"] for s in reloaded["messages"][0][key]] == [s["kind"] for s in steps], \
        reloaded["messages"][0]

    # 客户端整份回写（PUT 全量替换）：带着 trace 回去，过程必须还在
    outbound = [{"role": "assistant", "content": "答案是 420", "message_id": "m-1",
                 key: reloaded["messages"][0][key]}]
    assert store.replace(sid, "someone", outbound)
    again = store.get(sid, "someone")
    assert again["messages"][0].get(key), f"整份回写之后过程被抹平了：{again['messages'][0]}"
    # 回写时不带 trace：老客户端/老会话不许因此报错，只是没有过程
    assert store.replace(sid, "someone", [{"role": "assistant", "content": "只有正文"}])
    assert key not in store.get(sid, "someone")["messages"][0]
    store.delete(sid, "someone")


def test_nobody_writes_process_text_into_a_log():
    """思考原文与搜索命中只进界面与会话，不进日志：日志的可见面比会话归属宽得多。

    风险登记里那句"思考文本可能复述用户隐私"的前提是它只落在 owner 读得到的地方；而
    `data/backend.log` 既没有 owner 概念也常常被再抄一份出去。判据是**日志调用的实参**里
    出现过程内容（帧名、reasoning、trace、过程类标识），而不是"禁止一切 print"——发帧侧
    那句既有诊断 `[Stream] 调用工具: name(args)` 保留，它给的是工具名与参数，不是思考全文。

    两头成色不同，说清楚：服务端今天真的有 18 行日志语句（`server_sites` 那条断言就是
    自证扫到了东西），这一半是在管现成的代码；四个前端文件里 `console.*` / `Log.x()` /
    `println()` 一个都没有（原生只用 Compose 画、网页只改 DOM），所以客户端那一半现在是
    一条**空集的判据**——它今天抓不到任何违规，因为压根没有日志可查；它值钱的成分是
    "以后谁在前端加一行 console.log 打 trace，这里当场红"。别把它读成"已经证明前端不泄"。
    """
    banned = [se.FRAME_THINKING, se.FRAME_SEARCH, "reasoning", "trace", "TraceItem",
              "TracePanel", "accumulateTrace", "streamTrace"]
    log_call = re.compile(r"console\.(?:log|info|debug)\s*\(|Log\.[divwe]\s*\(|"
                          r"println[!]?\s*\(|System\.out\.print|logging\.|logger\.")
    clients = [STATIC / "app.js", STATIC / "api.js", KOTLIN / "Api.kt", KOTLIN / "ui" / "ChatUi.kt"]
    for path in clients:
        src = _text(path)
        for m in log_call.finditer(src):
            site = src[m.start():m.start() + 200]
            hit = [token for token in banned if token in site]
            assert not hit, (f"{path.relative_to(REPO_ROOT)} 把过程内容打进了日志"
                             f"（{hit}）：{site[:120]!r}")

    banned_server = ["reasoning", "think_buf", "round_think", "think_emitted"]
    server_sites = 0
    for path in EMIT_SIDE:
        for line in _text(path).splitlines():
            if not re.search(r"\bprint\(|logging\.|logger\.|\.warn\(|\.info\(", line):
                continue
            server_sites += 1
            hit = [token for token in banned_server if token in line]
            assert not hit, \
                f"{path.relative_to(REPO_ROOT)} 这一行把思考内容写进了日志：{line.strip()[:120]}"
    # 自证判据没空转：服务端这一半真的扫到了日志语句（今天是 18 行），客户端那一半
    # 一个日志调用都还没有——那是更强的状态，但要说破，别让 0 命中看起来像"查过了"。
    assert server_sites >= 5, f"发帧侧只扫到 {server_sites} 行日志：读法失效了，不是这里干净了"
    assert "reasoning_content" in _text(EMIT_SIDE[1]), \
        "streaming.py 里已经没有 reasoning_content 了——上面那份 banned_server 该跟着改"


def test_the_process_panel_only_ever_renders_text_nodes():
    """过程面板里第三方文本一律走 textContent，不许拼进 innerHTML。

    标题/摘要/网址是别人写的网页内容；一处 `innerHTML = ...` 就把它变成可执行入口，而那时
    服务端 `javascript:` 那道过滤救不了（被注入的可以是标签而不是协议）。原生侧对应的是
    Compose `Text(...)`：把 html() / AndroidHtmlConversionPolicy 引进这个过程面板就该红。
    """
    app_src = _text(STATIC / "app.js")
    span = app_src[app_src.index("function traceStepNode"):app_src.index("function paintTrace")]
    assert "innerHTML" not in span and "insertAdjacentHTML" not in span, \
        "过程面板开始拼 HTML 了：第三方标题/摘要于是成了可执行入口"
    assert "textContent" in span, "过程面板不再走文本节点，判据本身要重写"
    kt = _text(KOTLIN / "ui" / "ChatUi.kt")
    region = _kotlin_body(kt, "private fun TraceStepRow", "ChatUi.kt TraceStepRow")
    assert "fromHtml" not in region and "HtmlCompat" not in region and "html(" not in region, \
        "原生过程面板把字符串按 HTML 解了：同一份注入在壳上照样成立"


# ==========================================================================
# 4. 文案单点（两条聊天路径不许各长一套）
# ==========================================================================

def test_tool_label_is_the_same_rule_on_both_chat_paths():
    """工具那一行收起态文案：流式与非流式两条路必须给出逐字相同的句子。

    过程面板的标题是用户判断"它到底去算了什么"的唯一一句人话，同一个工具在两条路径上写出
    两种句子（流式说「搜索「西安 人口」」、非流式说「调用 web_search」）就是 UX 分裂，而且
    流断兜底到 /v1/chat 那一刻人会看见标题变。两份实现的理由写在 pipeline.py 的注释里
    （不跨模块引私有 helper）——那就用行为把它们钉在一起：判据不同表就红。
    """
    import app.core.streaming as streaming
    import app.pipeline as pipeline

    table = [
        ("web_search", {"query": "西安 人口"}),
        ("web_search", {"query": "   "}),
        ("web_search", {}),
        ("calculator", {"expression": "17*24+12"}),
        ("calculator", {"expression": ""}),
        ("execute_code", {"code": "print(1)"}),
        ("unknown_tool", {"a": 1}),
        ("web_search", "不是字典"),
        ("web_search", None),
    ]
    for name, args in table:
        got = streaming._tool_label(name, args)      # noqa: SLF001 —— 钉的就是这两份实现
        want = pipeline._tool_label(name, args)      # noqa: SLF001
        assert got == want, f"{name}({args!r})：流式说 {got!r}，非流式说 {want!r}"
    # 而真正出门的那一句一定被封顶过（长度在 stream_events 里算，不在两处各写一遍）
    long_label = streaming._tool_label("web_search", {"query": "词" * 500})
    assert len(se.tool_call_frame("c", "web_search", {"query": "词" * 500},
                                  long_label)["label"]) <= se.TOOL_LABEL_MAX


def test_the_omitted_marker_says_the_same_thing_on_both_clients():
    """被省略的那一行双端同文案：这是用户判断"过程被动过刀"的唯一提示。

    与本文件开头那条 netminder 的先例同理（同判据同文案），只是这里钉的是省略标记。句子
    由测试锚定（它不是契约模块给的，是两端各自写的用户可见文案），钉的是**同一个说法**：
    一端「…中间 3 步已省略…」另一端「(略过 3 步)」就是"悄悄裁了还不说"的另一种写法。
    """
    js = _text(STATIC / "app.js")
    kt = _text(KOTLIN / "ui" / "ChatUi.kt")
    for src, label in ((js, "网页"), (kt, "原生")):
        assert "…中间 " in src and " 步已省略…" in src, \
            f"{label}那侧的省略标记不再是「…中间 N 步已省略…」：双端文案长岔了"
    assert re.search(r'"…中间 \$\{(\w+)\.omitted\} 步已省略…"', kt), \
        "原生那句没把条数放进去：只剩一句「中间省略了」，用户不知道被裁掉多少"
    assert re.search(r'"…中间 " \+ \(Number\(s\.count\) \|\| 0\) \+ " 步已省略…"', js), \
        "网页那句没把服务端给的 count 放进去"
