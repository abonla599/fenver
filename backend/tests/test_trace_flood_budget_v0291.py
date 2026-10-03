"""v0.29.1 加固①：把「帧洪泛」的验收标准从口号变成实测数字（3000 片思考 → 67 帧）。

plan-v0.29-process-trace.md §5 的验收原文是「压测 >2000 帧即回退更粗的分片」，防洪
预算全数住在唯一真源 `app/core/stream_events.py`（THINKING_FLUSH_CHARS /
THINKING_FLUSH_SECONDS / THINKING_TOTAL_MAX / TRACE_STEPS_MAX / TRACE_CHARS_MAX 与
各字段封顶），合并与冲光逻辑在 `app/core/streaming.py` 的 flush_thinking。但 v0.29.0
合并那天起，没有任何一条测试**真的**喂过 >2000 片上游 delta——预算在不在、由谁执行、
裁掉的部分记不记账，全是口头承诺。这个文件就是那把缺席的尺子，三组数字全部实测：

1. 引擎洪泛（①②）：3000 片 4 字思考 delta（共 12000 字，越过 THINKING_TOTAL_MAX），
   实测 thinking 帧 = 67（= 8000//120 + 1，而不是 3000），上屏合计恰好 8000；
2. 落盘洪泛（③）：600 步（500 thinking + 50 tool + 50 search）过 compact_trace，
   实测 29 条真步 + 1 条记账、真步 11915 字——被裁的 571 步里字符裁那刀只碰
   thinking（34→23），工具与搜索零损失；
3. 每步/每命中字段封顶（④）：_cap 与各构造器逐字段实测，外加预算常量快照。

诚实声明（实测出来的现状，不是断言错误）：THINKING_TOTAL_MAX 这道闸是**执行了的**
（恰好停在 8000，正文照流、不抛异常），但被裁的 4000 字**不记账**：
streaming.py:272-275 在 room<=0 时把 think_buf 直接清空、streaming.py:276 在余量
不足时丢掉缓冲区尾巴、streaming.py:314 此后连攒都不攒——帧与步里都没有
omitted/truncated 之类的记号（对比 compact_trace：stream_events.py:243-249 把丢的步
记成 {kind:"omitted",count:N}）。② 把「裁了且不记账」钉成现状断言；将来若补上记账
帧，② 会红——那是缺口被堵上的信号，按新形状改断言即可，不是测试坏了。

另：计划里那句「…中间 N 步已省略…」在**服务端源码里并不存在**——服务端产的是结构化
步（kind=omitted + count），文案是两个客户端各自渲染的（app.js:1415、ChatUi.kt:2102），
双端文案一致性已由 test_process_trace_contract.py:891 那组守着，本文件不重复钉。

THINKING_FLUSH_SECONDS（0.4）这里不测：时间触发只是「面板别长时间没动静」的下限，
洪泛场景下字数触发必然先到（同 test_streaming_events 第 3 组的口径，测试不为它 sleep）。

回退必红自查：
- THINKING_FLUSH_CHARS 从 120 改掉 → ④ 的预算快照红，且 ① 的实测帧数 67 与
  「前 66 帧每帧恰好 120 字」红；
- 合并改成一字一帧 / flush_thinking 被删 → ① 帧数暴涨远离 67，红；
- THINKING_TOTAL_MAX 被抬或被删（flush_thinking 的 room 逻辑）→ ① 的合计 8000 与
  帧数 67 红；给思考溢出补上记账帧 → ② 的「不记账现状」红（提示更新为记账断言）；
- TRACE_STEPS_MAX 被抬 → ③ 的实测 29 真步红；TRACE_CHARS_MAX 被抬或字符裁被删 →
  ③ 的 11915 字红；把字符裁从「删最长的一条」改成「从头删」或「只按步数留前 40」→
  ③ 的「工具 3 + 搜索 3 全存活、被裁的 11 条全是 thinking」红；
- 摘掉 compact_trace 的 omitted 记账 → ③ 的 count==571 红；
- 摘掉任何构造器里的 _cap → ④ 对应字段的实测长度红。
"""
import importlib
from collections import Counter
from types import SimpleNamespace

import pytest

import app.core.streaming as streaming
from app.core import stream_events as se

# ---------- 假流式客户端（形状沿用 test_streaming_events / test_stream_tools） ----------

def _chunk(content=None, reasoning=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning,
                            tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)],
                           usage=None)


class _RecordingStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        pass


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)

    def create(self, **kwargs):
        return _RecordingStream(self.responses.pop(0) if self.responses else [])


class _FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_FakeCompletions(responses))


@pytest.fixture
def real_engine(monkeypatch):
    """掀开 conftest 对 streaming.stream_chat 的桩（reload 拿回真身），build_client
    换成喂假 chunk 的对象。teardown 再 reload 一次，把模块恢复成别的用例期待的桩。"""
    importlib.reload(streaming)
    holder = {}

    def build(provider):
        return holder["client"]

    monkeypatch.setattr(streaming, "build_client", build)

    def _install(responses):
        holder["client"] = _FakeClient(responses)

    yield _install
    importlib.reload(streaming)


# ---------- ① 引擎洪泛：3000 片 delta 必须被合并成几十帧，而不是 3000 帧 ----------

def test_three_thousand_reasoning_deltas_merge_into_sixty_seven_frames(real_engine):
    """验收「压测 >2000 帧」的那把尺子：喂 3000 片思考 delta，实测线上只有 67 帧。

    每片 4 字、共 12000 字——刻意越过 THINKING_TOTAL_MAX(8000)，一趟同时把两条闸
    都走掉：按字数合并（每攒够 120 字一帧）与单轮总量封顶。判据分两层：公式上界
    （预算的函数，随常量伸缩）与实测数字（把 2026-10-03 量到的形状钉死）。
    """
    n_deltas = 3000
    piece = "思考一二"  # 4 字一片，3000 片整除 120，让 flush 触发点可精确推算
    total_in = n_deltas * len(piece)  # 12000
    assert total_in > se.THINKING_TOTAL_MAX, "洪泛样本必须越过总量闸，否则测不到 (b)"

    real_engine([[_chunk(reasoning=piece) for _ in range(n_deltas)]
                 + [_chunk(content="收尾")]])
    trace_out = []
    frames = list(streaming.stream_chat_events(
        "fake-chat", [{"role": "user", "content": "想点什么"}],
        provider_id="fake-model", trace_out=trace_out))

    thinking = [f for f in frames if f["type"] == se.FRAME_THINKING]

    # (a) 帧数：公式上界 = 封顶总量 / 每帧预算 + 1 帧尾差；实测 = 67。
    assert len(thinking) <= se.THINKING_TOTAL_MAX // se.THINKING_FLUSH_CHARS + 1, \
        f"合并失效：{len(thinking)} 帧（输入 {n_deltas} 片）"
    assert len(thinking) == 67, \
        f"实测基线是 67 帧（66×120 + 1×80）；现在是 {len(thinking)}，防洪形状变了"
    assert all(len(f["text"]) <= se.THINKING_FLUSH_CHARS * 2 for f in thinking), \
        "单帧思考越过 thinking_frame 的封顶"
    # 实测形状：前 66 帧恰好攒满 120 字（字数触发），第 67 帧只剩总量余量 80 字。
    assert [len(f["text"]) for f in thinking[:66]] == [120] * 66
    assert len(thinking[66]["text"]) == se.THINKING_TOTAL_MAX - 66 * se.THINKING_FLUSH_CHARS

    # (b) 上屏合计恰好停在总量闸，一个字不多。
    emitted = "".join(f["text"] for f in thinking)
    assert len(emitted) == se.THINKING_TOTAL_MAX == 8000, \
        f"总量封顶没停在预算上：{len(emitted)}"

    # (c) 保真 + 记账的诚实边界：上屏的必须是输入的真前缀（不重排、不掺假），
    # 而差额 12000-8000=4000 字被裁——现状是帧与步都**没有**任何 omitted/truncated
    # 记号（见模块 docstring 的缺口声明）。哪天补上记账，下面两条红，更新断言即可。
    assert emitted == (piece * n_deltas)[:se.THINKING_TOTAL_MAX], "裁的是哪段不能含糊"
    dropped = total_in - len(emitted)
    assert dropped == 4000
    assert all(set(f.keys()) == {"type", "text"} for f in thinking), \
        "thinking 帧长出了记账字段：源码改了裁量记账方式，请连同缺口声明一起更新本用例"
    steps = [s for s in trace_out if s["kind"] == "thinking"]
    assert len(steps) == 1 and set(steps[0].keys()) == {"kind", "text"}
    assert len(steps[0]["text"]) == se.THINKING_TOTAL_MAX, "落盘的那一份也必须恰好贴闸"

    # 封顶只掐展示，不掐流程：正文照流，帧总数 = 67 thinking + 1 content。
    content = "".join(f["text"] for f in frames if f["type"] == se.FRAME_CONTENT)
    assert content == "收尾", "思考封顶不该把正文一起掐掉"
    assert len(frames) == 68, f"线上帧总数实测 68（67+1），实得 {len(frames)}"


# ---------- ② compact_trace 洪泛：600 步进、贴预算出、裁的是思考、丢的记账 ----------

def _flood_steps():
    """10 thinking + 1 tool + 1 search 一轮，共 50 轮：600 步（500/50/50）。

    轮转而不是「思考全排前面」：真实过程就是想完一段调一次工具；而且只有混排
    才能让步数头尾窗同时吃到 thinking/tool/search，字符那一刀「删最长的」才有戏。
    """
    steps = []
    for i in range(50):
        steps += [se.step_thinking("想" * 500) for _ in range(10)]
        steps.append(se.step_tool(f"c{i}", "calculator", f"计算 {i}", True, "算完了"))
        steps.append(se.step_search(
            f"c{i}", "西安 人口",
            [{"title": "西安 百科", "url": "https://baike.example/xian",
              "snippet": "常住人口一千多万"}]))
    return steps


def test_compact_trace_flood_cuts_thinking_keeps_evidence_and_accounts_omitted():
    """600 步洪泛实测：真步 29（≤40 闸）、11915 字（≤12000 闸）、被丢 571 步全记账。

    这条把 §5「保首尾、先裁大段独白、证据留到最后」从注释变成数字：
    - 步数闸先把 600 裁成头 20 + 尾 20 = 40（34 thinking + 3 tool + 3 search）；
    - 字符闸再把 34 条 thinking 从最长者下手裁到 23（11 条），tool/search 零损失；
    - 记账步实测落在 index 14（中间），count = 571 = 600 - 29。
    """
    steps = _flood_steps()
    assert len(steps) == 600
    out = se.compact_trace(steps)

    real = [s for s in out if s.get("kind") != "omitted"]
    marks = [s for s in out if s.get("kind") == "omitted"]

    # 步数闸：封顶的是**真步**；记账步是账、不占预算，实测总长 30 = 29 + 1。
    assert len(real) <= se.TRACE_STEPS_MAX, f"步数闸失效：{len(real)}"
    assert len(real) == 29 and len(out) == 30, \
        f"实测基线 29 真步 + 1 记账，实得 {Counter(s['kind'] for s in out)}"

    # 字数闸：_trace_chars 按字符串叶子求和（不按 JSON 长度），实测贴着预算之内。
    chars_real = se._trace_chars(real)
    assert chars_real <= se.TRACE_CHARS_MAX, f"字数闸失效：{chars_real}"
    assert chars_real == 11915, f"实测基线 11915 字，现在是 {chars_real}——裁刀位置变了"
    # 记账步只贡献它的 kind 字符串（count 是整数，不进字数口径），实测 11922。
    assert se._trace_chars(out) == 11915 + len("omitted")

    # 裁谁保谁：被裁的 11 条（40-29）全是 thinking；进窗的 3 tool + 3 search 全存活。
    kinds = Counter(s["kind"] for s in out)
    assert kinds == Counter({"thinking": 23, "omitted": 1, "tool": 3, "search": 3}), kinds
    assert all(s["name"] == "calculator" and s["ok"] is True for s in real
               if s["kind"] == "tool")
    search_steps = [s for s in real if s["kind"] == "search"]
    assert all(s["results"][0]["url"] == "https://baike.example/xian"
               for s in search_steps), "证据（网址）必须在落盘的那一份里活着"

    # 丢的步记账：恰好一条、count 精确对账、位置在中间而不是首尾。
    assert len(marks) == 1 and marks[0]["count"] == 600 - 29 == 571, marks
    assert 0 < out.index(marks[0]) < len(out) - 1

    # 首尾保底 + 实测的裁刀落点：并列最长时 max 取**第一个**（stream_events.py:239），
    # 于是先被删的是头窗里最前面的大段思考——11 条全落在思考上，头两条反而提前成了
    # 工具/搜索（证据往前顶），尾仍是搜索。「什么都不剩」才是这里要堵的死法。
    assert out[0]["kind"] == "tool" and out[-1]["kind"] == "search"
    assert len(real) >= 2


# ---------- ③ 每步 / 每命中字段的封顶：_cap 与各构造器逐字段实测 ----------

def test_budget_constants_snapshot():
    """预算数字本身也是被测物：改任何一个数，先让本文件和上面两组实测红。

    §5 的验收是拿这些常数当防洪方案的「实现」来写的；没有快照，把 120 抬到 12000
    也能让「按公式伸缩」的上界断言照样绿——那等于把尺子交给要测的人。
    """
    assert (se.THINKING_FLUSH_CHARS, se.THINKING_FLUSH_SECONDS,
            se.THINKING_TOTAL_MAX) == (120, 0.4, 8000)
    assert (se.TRACE_STEPS_MAX, se.TRACE_CHARS_MAX) == (40, 12000)
    assert (se.TOOL_NAME_MAX, se.TOOL_ARGS_MAX, se.TOOL_LABEL_MAX,
            se.TOOL_SUMMARY_MAX) == (64, 600, 160, 500)
    assert (se.SEARCH_RESULTS_MAX, se.SEARCH_QUERY_MAX, se.SEARCH_TITLE_MAX,
            se.SEARCH_SNIPPET_MAX, se.SEARCH_URL_MAX) == (8, 200, 200, 300, 500)


def test_every_builder_field_is_capped_where_it_is_enforced():
    """封顶全部由构造器现场执行（_cap 的语义：超长 = limit-1 字 + 一个省略号）。

    逐字段打一遍，两个口径都要：长度恰好等于预算（闸在、且贴着闸），以及
    帧/步两条路同源（step_search 复用 search_frame，改一处两处的形状不能劈叉）。
    """
    # thinking：帧按 2×FLUSH 封顶（stream_events.py:87），步按 TOTAL_MAX 封顶（:184）。
    f = se.thinking_frame("x" * (se.THINKING_FLUSH_CHARS * 2 + 37))
    assert len(f["text"]) == se.THINKING_FLUSH_CHARS * 2 == 240
    assert f["text"].endswith("…")
    s = se.step_thinking("想" * (se.THINKING_TOTAL_MAX + 7))
    assert len(s["text"]) == se.THINKING_TOTAL_MAX and s["text"].endswith("…")

    # 工具：name/label/arguments 各自贴闸；dict 参数最多 12 键、每值 150 字。
    long = "名" * 100
    call = se.tool_call_frame("id1", long, "y" * 900, label="标" * 200)
    assert len(call["id"]) <= 64
    assert len(call["name"]) == se.TOOL_NAME_MAX == 64
    assert len(call["label"]) == se.TOOL_LABEL_MAX == 160
    assert len(call["arguments_text"]) == se.TOOL_ARGS_MAX == 600
    slim = se.tool_call_frame("id2", "calculator",
                              {f"k{i}": "v" * 400 for i in range(20)})["arguments"]
    assert len(slim) == 12, f"dict 参数只该留前 12 键，实得 {len(slim)}"
    assert all(len(k) <= 40 for k in slim)
    assert all(len(v) == se.TOOL_ARGS_MAX // 4 == 150 for v in slim.values())
    res = se.tool_result_frame("id1", long, True, "结" * 600)
    assert len(res["summary"]) == se.TOOL_SUMMARY_MAX == 500
    ts = se.step_tool("id1", long, "标" * 200, True, "结" * 600)
    assert (len(ts["name"]), len(ts["label"]), len(ts["summary"])) == (64, 160, 500)

    # 搜索：命中条数、query、title、snippet、url 五个维度同时打满。
    hits = [{"title": "标" * 300, "url": "https://e.example/" + "p" * 600,
             "snippet": "摘" * 400} for _ in range(20)]
    frame = se.search_frame("id1", "Q" * 300, hits)
    assert len(frame["query"]) == se.SEARCH_QUERY_MAX == 200
    assert len(frame["results"]) == se.SEARCH_RESULTS_MAX == 8, "喂 20 条只该放行 8 条"
    for r in frame["results"]:
        assert len(r["title"]) == se.SEARCH_TITLE_MAX == 200
        assert len(r["snippet"]) == se.SEARCH_SNIPPET_MAX == 300
        assert len(r["url"]) == se.SEARCH_URL_MAX == 500
    # 没标题又没合法网址的行直接不放行（8 条预算不白占）；危险协议照旧裁空。
    ghost = se.search_frame("id", "q", [
        {"title": "", "url": "javascript:alert(1)", "snippet": "s"},
        {"title": "", "url": "", "snippet": "只有碎语"}])
    assert ghost["results"] == []
    # 帧与步两条路必须同一个口径（step_search 内部复用 search_frame）。
    st = se.step_search("id1", "Q" * 300, hits)
    assert st["results"] == frame["results"] and st["query"] == frame["query"]
