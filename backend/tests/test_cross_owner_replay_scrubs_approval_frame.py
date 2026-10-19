"""跨属主续播不许把审批请求帧一起带出去（补 v0.29.1 那道边界漏掉的那一种帧）。

形状是怎么漏的：这一帧不是 stream_events 的构造器造的，是 streaming.py 里手写的字面量。
`PRIVATE_TRACE_FRAME_TYPES` 按类型名逐条命中，而这个名字从来没被登记过——于是它在
admin 跨属主补帧时逐字节透出，而 `plan_summary` 是用户那句话的衍生物。
"不是匿名可取"要说清楚：能取到它的是持有 admin 令牌的人，被背弃的承诺是"连管理端也看不见"。

判据都是"整串输出里一次都不出现"，不写上下界（v0.29.1 那份用 `>=`/`<=` 的形状正是它
没抓住这一帧的原因：断言只数了已经登记的那几种）：
① 出口减法：这一帧整条不吐，连它的 `id:` 行都不留，序号因此留空档；
② admin 跨属主真打一次端点：响应体里搜不到类型名、票据号、计划摘要里的私有串，
   正文照旧完整——收回的是"顺手看见别人的计划"，不是接上别人 run 这个能力；
③ 同一轮由发起者本人续播：这一帧必须在，且内容与属主视图一致——少了这条，"收窄"会
   一路收成功能消失，审批卡片从此不再出现；
④ 结构锁：注册帧要先声明立场；豁免名单钉死到具体名字；手写帧字面量的扫描面是
   `backend/app/` 下所有 *.py，并且**不按已知帧名筛**（按帧名筛正是这一帧当初隐形的原因）。

回退必红自查：
- 把 FRAME_APPROVAL_REQUEST 从 PRIVATE_TRACE_FRAME_TYPES 摘掉 → ①② 同时红；
- 把 sse_frames 的属主路径也接上 scrub → ③ 红；
- 把构造处改回手写字面量 → test_approval_frame_is_registered_by_constant… 红；
- 动线上形状（键序、增键、删键、类型名）→ test_approval_frame_wire_bytes… 红：它读的是
  stream_chat_events 在审批分支上真造出来的那一帧，不是这份文件手抄的字典；
- 只改手抄夹具的键集/键序、不动基准 → 同一把里的对齐钉红（两份手抄必须同键同序，值故意不同）；
- 把"取真身"那一段换成手抄一个字典 → 真跑那几枚判据红（假 LLM 的份数没被抽干、流里没有
  tool_result 与 content）；
- 往 NON_CLIENT_FRAMES 里加名字（哪怕同时补进隐私名单）→ 名单钉子红；
- 豁免登记里留一条空理由 → 结构锁红（机器只判非空，判不了理由对不对）。
"""
import importlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import approval, authz, stream_events as se, stream_runs, streaming
from app.core.authz import Principal
from app.main import app

BOOT = {"Authorization": "Bearer boot-token"}
VICTIM = "u_victim_approval"
TICKET = "tk-0123456789abcdef"
# 计划摘要里刻意混一个 ASCII 片段：帧走 ensure_ascii，纯中文串在线上会变成 \u 转义，
# 拿它当"整串里不许出现"的判据会恒真（这正是这一类锁最容易空转的地方）。
PLAN_SUMMARY = "删除 last-month-recordings 目录里的 3 个文件"
PLAN_MARK = "last-month-recordings"
BACKEND = Path(__file__).resolve().parents[1]

# 喂 run 缓冲用的夹具帧，形状照构造处写；它**不是**线上字节的基准——基准由下面那把
# 从引擎里取真身的锁判（抄一份字典当基准，正是键序与增删键能一路绿过去的方式）。
# 键集与键序由 test_approval_frame_wire_bytes_and_client_whitelists_unchanged 里那条判据对齐
# 到基准；两边的值故意不同，别拿这一份当基准的取值底稿。
APPROVAL_FRAME = {
    "type": se.FRAME_APPROVAL_REQUEST,
    "ticket_id": TICKET,
    "tool_name": "delete_files",
    "plan_summary": PLAN_SUMMARY,
    "expires_at": 1730000000.0,
}


def _a_run_with_approval(owner: str = VICTIM):
    run = stream_runs.create_run(user_id=owner, session_id="s_approval")
    run.append(se.start_frame(run.message_id, "probe-model"))
    run.append(se.content_frame("正文第一句"))
    run.append(dict(APPROVAL_FRAME))
    run.append(se.tool_result_frame("call_1", "delete_files", False, "〔审批待确认〕", 0))
    run.append(se.content_frame("，正文第二句"))
    run.append(se.done_frame("正文第一句，正文第二句", run.message_id, "probe-model"))
    run.finish("completed")
    return run


def _drain(frames):
    gen = iter(frames)
    out = []
    try:
        out.extend(gen)
    finally:
        gen.close()
    return out


def _types(chunks):
    out = []
    for chunk in chunks:
        _, sep, rest = chunk.partition("data: ")
        if sep:
            out.append(json.loads(rest.partition("\n\n")[0]).get("type"))
    return out


def _seq(chunk):
    return int(chunk.partition("\n")[0].rsplit(":", 1)[1])


def _payload(chunk):
    return json.loads(chunk.partition("data: ")[2].partition("\n\n")[0])


# ------------------------------------------------------------------ ① 出口的减法 --

def test_scrub_drops_the_approval_frame_whole_including_its_id_line():
    run = _a_run_with_approval()
    owner_frames = _drain(stream_runs.sse_frames(run, 0))
    scrubbed = _drain(stream_runs.scrub_private_trace(stream_runs.sse_frames(run, 0)))

    approval_seq = next(_seq(c) for c in owner_frames if '"approval_request"' in c)
    body = "".join(scrubbed)
    assert '"approval_request"' not in body, "审批帧仍然整条透出：跨属主能看见别人的计划"
    assert se.FRAME_APPROVAL_REQUEST not in _types(scrubbed), _types(scrubbed)
    assert approval_seq not in [_seq(c) for c in scrubbed if c.startswith("id: ")], \
        "帧被摘掉了却漏了它的 id: 行：这一帧的游标还留在补帧里"
    assert TICKET not in body and PLAN_MARK not in body, body
    for chunk in owner_frames:
        if '"approval_request"' not in chunk and '"tool_result"' not in chunk:
            assert chunk in scrubbed, "非隐私帧被重写过：形状不该变"


def test_approval_frame_is_registered_by_constant_not_by_hand_written_literal():
    """构造处不许再手写类型名字面量：这一帧之所以漏，正是因为没人登记过它。"""
    source = (BACKEND / "app" / "core" / "streaming.py").read_text(encoding="utf-8")
    assert "se.FRAME_APPROVAL_REQUEST" in source, "构造处回退成手写字面量了"
    assert '"type": "approval_request"' not in source, "手写字面量仍在：名单外的帧就是裸帧"


# ------------------------------------------------------------------ ② admin 跨属主 --

def test_admin_cross_owner_replay_hides_the_approval_frame(client):
    run = _a_run_with_approval()
    r = client.post("/v1/chat/stream", headers={**BOOT, "Last-Event-ID": f"{run.run_id}:0"},
                    json={"messages": [{"role": "user", "content": "不该被调用"}]})
    assert r.status_code == 200, r.text[:200]
    assert "approval_request" not in r.text, "admin 接上别人的 run 又看见审批卡片了"
    assert TICKET not in r.text and PLAN_MARK not in r.text, r.text[:400]
    done = next(_payload(l) for l in r.text.splitlines()
                if l.startswith("data: ") and '"type": "done"' in l)
    assert done["full_text"] == "正文第一句，正文第二句", done


# ------------------------------------------------------------------ ③ 发起者本人照旧 --

def test_originating_owner_still_receives_the_approval_frame(client):
    run = _a_run_with_approval(owner="u_owner_approval")
    app.dependency_overrides[authz.current_principal] = \
        lambda: Principal("u_owner_approval", "属主本人", "user")
    try:
        r = client.post("/v1/chat/stream", headers={"Last-Event-ID": f"{run.run_id}:0"},
                        json={"messages": [{"role": "user", "content": "不该被调用"}]})
    finally:
        app.dependency_overrides.pop(authz.current_principal, None)
    assert r.status_code == 200, r.text[:200]
    assert "approval_request" in r.text, "属主也收不到审批帧了：确认卡片被一起修没了"
    assert TICKET in r.text and PLAN_MARK in r.text, r.text[:400]


# ------------------------------------------------------------------ 线上字节没动 --

_APPROVAL_TOOL = "execute_code"   # 注册表里唯一标了 requires_approval 的工具（直通的不算）
_WIRE_USER = "u_wire_probe"       # 引擎那一轮的属主：熔断判据与 stream_chat_events 用同一个名字
_TICKET_FIXTURE = {"ticket_id": TICKET, "tool_name": _APPROVAL_TOOL, "plan_summary": PLAN_SUMMARY,
                   "created_at": 1730000000.0, "expires_at": 1730000000.0, "user_id": "u_wire"}


def _delta(content=None, tool_calls=None):
    piece = SimpleNamespace(content=content, reasoning_content=None, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=piece, finish_reason=None)], usage=None)


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(index=0, id=call_id,
                           function=SimpleNamespace(name=name, arguments=arguments))


class _RoundStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __iter__(self):
        return iter(self._chunks)

    def close(self):
        pass


class _FakeLLM:
    """一轮一份假 chunk 列表；引擎要的只是 chat.completions.create(**kwargs)。"""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **_kwargs):
        return _RoundStream(self.rounds.pop(0) if self.rounds else [])


def _frame_the_engine_itself_builds(monkeypatch):
    """真跑一轮带审批的 stream_chat_events，取回**产品代码自己写出来**的那一帧。

    先 reload：autouse 的 `_stub_llm_calls` 把 `stream_chat_events` 换成了假身，真引擎要从
    模块里换回来（先例见 test_streaming_events.py 的 real_engine）。票据工厂也给桩——要钉的
    是 streaming.py 写出那颗字典的手，ticket_id 与 TTL 的取值不是它；给成常量才谈得上"逐字节
    相同"，顺带不落盘、不碰 `_tickets`。审批分支本身走真判据（needs_approval / is_round_fused
    两条都在下面各钉了一枚前置断言，判不过就没有"真身"可读）。

    "真跑"这一句靠下面那几枚判据撑：假 LLM 手里的 chunk 份数被抽干（引擎走到了轮次边界）、
    审批帧之外还有 tool_result 与 content（引擎把这一轮走完了）、带 ticket_id 的帧只有一枚。
    把这里整段换成手抄一个字典，这几枚里总有一枚红——光数 ticket_id 那一枚是拦不住造假的。
    """
    monkeypatch.setattr(approval, "create_approval_request",
                        lambda name, args, user_id: dict(_TICKET_FIXTURE))
    assert approval.needs_approval(_APPROVAL_TOOL), \
        "%s 不再需要审批：审批分支走不到，这一帧就没有真身可读了" % _APPROVAL_TOOL
    assert not approval.is_round_fused(_WIRE_USER, _APPROVAL_TOOL), \
        "%s 对 %s 已被本轮熔断：走的是不再重试那条分支，审批帧根本不会被造出来" % (_APPROVAL_TOOL, _WIRE_USER)
    rounds = [[_delta(tool_calls=[_tool_call("c_1", _APPROVAL_TOOL, '{"code": "print(1)"}')])],
              [_delta(content="已停在确认卡片前")]]
    llm = _FakeLLM(rounds)
    importlib.reload(streaming)
    monkeypatch.setattr(streaming, "build_client", lambda provider: llm)
    frames = list(streaming.stream_chat_events(
        "fake-chat", [{"role": "user", "content": "跑一段代码"}], provider_id="fake-model",
        user_id=_WIRE_USER,
        tools=[{"type": "function", "function": {"name": _APPROVAL_TOOL,
                                                 "parameters": {"properties": {}}}}],
        trace_out=[]))
    importlib.reload(streaming)          # 把模块还给下一个用例
    seq = [f.get("type") for f in frames]
    assert not llm.rounds, "假 LLM 手里还剩 %d 份没被取走：引擎没走到轮次边界，读回来的不是跑完一轮的帧（%s）" % (
        len(llm.rounds), seq)
    assert se.FRAME_TOOL_RESULT in seq and se.FRAME_CONTENT in seq, (
        "整条流里只有审批帧 = 手抄一个字典就能给出的形状，不是引擎跑完一轮的形状：%s" % seq)
    built = [f for f in frames if "ticket_id" in f]
    assert len(built) == 1, "审批分支该造且只造一帧，实际 %d 帧：%s" % (
        len(built), [f.get("type") for f in frames])
    return built[0]


def test_approval_frame_wire_bytes_and_client_whitelists_unchanged(monkeypatch):
    """换常量不许改一个比特：两个客户端早就各自认这个名字，改字节就是让老包解析崩。

    这份文件里有**两处**手抄的审批帧，分工要分清：`APPROVAL_FRAME` 喂 run 缓冲（上面的
    `_a_run_with_approval`），frozen 是"改之前这一帧在线上长什么样"的字节基准。两份的**键集与
    键序**由下面那枚钉子对齐，**值故意不同**——夹具走的是删文件那条工具名，基准读的是引擎在
    `execute_code` 审批分支上真造出来的那一帧；拿夹具的字典当 frozen 的取值底稿会把 tool_name
    一起漂掉。基准喂给它的必须是引擎真造的帧，否则这份副本改不改都和线上无关。
    """
    assert se.FRAME_APPROVAL_REQUEST == "approval_request"
    # 改动前的形状，含键序与冒号后的空格：json.dumps 按插入顺序出字节，键序变了就是字节变了。
    frozen = ('{"type": "approval_request", "ticket_id": "%s", "tool_name": "%s", '
              '"plan_summary": %s, "expires_at": 1730000000.0}'
              % (TICKET, _APPROVAL_TOOL, json.dumps(PLAN_SUMMARY)))
    assert tuple(APPROVAL_FRAME) == tuple(re.findall(r'"([a-z_]+)":', frozen)), (
        "两份手抄的审批帧键集/键序分叉了：夹具 %s vs 基准 %s——键序变了就是线上字节变了"
        % (tuple(APPROVAL_FRAME), tuple(re.findall(r'"([a-z_]+)":', frozen))))
    assert se.sse_data(_frame_the_engine_itself_builds(monkeypatch)) == "data: " + frozen + "\n\n"
    js = (BACKEND / "app" / "web" / "static" / "api.js").read_text(encoding="utf-8")
    kt = (BACKEND.parent / "android-native" / "app" / "src" / "main" / "java"
          / "xyz" / "fenever" / "assistant" / "nativeapp" / "Api.kt").read_text(encoding="utf-8")
    assert '"approval_request"' in js, "网页侧不再认这一帧：老客户端会解析不到"
    assert '"approval_request" ->' in kt, "原生侧不再认这一帧：同上"


# ------------------------------------------------------------------ ④ 结构锁 --

# 手写 `"type": "…"` 的既成事实与它们的立场，键是 (相对 backend/app/ 的路径, 类型名)——换
# 个模块再手打同一个名字照样红。新增一条必须同时写清为什么忍；位置空了就删掉那行。
_SCHEMA_SHAPE_KEYS = {("tools/builtin_tools.py", "string"), ("tools/builtin_tools.py", "object"),
                      ("tools/registry.py", "function"), ("core/streaming.py", "function"),
                      ("agents/react_agent.py", "function"), ("core/uploads.py", "text"),
                      ("core/uploads.py", "image_url")}
_GUARD_INTERNAL_EVENTS = ("cost_cap_rounds", "cost_cap_tokens", "output_budget_exhausted",
                          "output_truncated", "duplicate", "suspend", "fail_backoff")

DECLARED_HAND_WRITTEN = {
    ("core/stream_runs.py", "cannot_resume"):
        "连接级帧，不占缓冲序号，只带 run_id 与固定话术；它不是终止帧——R3b-3 要求其后必须"
        "再补一帧 done（stream_runs.py 的 cannot_resume 分支自己就 yield 了那条 done）",
    ("main.py", "cancelled"):
        "带的是本轮正文，与 done 同一立场（跨属主本来就看得见回答本身）",
    **{k: "给上游 provider 的 schema / 消息分片，不是流上的帧"
       for k in _SCHEMA_SHAPE_KEYS},
    **{("core/guardrails.py", n): "RoundGuard 的内部事件记录（护栏的账），进程内读，不上流"
       for n in _GUARD_INTERNAL_EVENTS},
}


def test_the_exemption_roster_is_pinned_to_the_name_that_earned_it():
    """钉的是等式而不是 subset：NON_CLIENT_FRAMES 里只许有 FRAME_APPROVAL_REQUEST 这一个名字。

    为什么 subset 判据不够：那条只问"豁免的有没有进隐私名单"，于是**同时**往两张表里加一个
    名字它就沉默了——"用豁免跳过两个客户端各多一条分发臂"变成零成本。跳过的代价要说准：那样
    一帧永远到不了该看它的人手里（客户端没有分发臂），是一个**功能缺口**；不是隐私漏口——这一
    层只会多剥一帧，不会把别人的内容送出去。要加名字就在这儿一起登记，并答清谁该看见它。
    """
    assert set(se.NON_CLIENT_FRAMES) == {se.FRAME_APPROVAL_REQUEST}, (
        "豁免名单动了：%s。除了补进隐私名单，还要说清这一帧谁该看见、两个客户端为什么不用改。"
        % sorted(se.NON_CLIENT_FRAMES))


def test_every_frame_type_on_the_stream_declares_a_privacy_posture():
    """任何出现在流上的类型名都必须二选一：客户端注册帧，或隐私名单。

    漏了任何一格都不是"少删一帧"，而是跨属主逐字节透出，所以这里判的是**有没有立场**，
    不是立场对不对（对不对由上面三组行为断言判）。

    手写那一半的扫描面是 `backend/app/` 下所有 *.py，且**不按已知帧名筛**：契约那边那份
    整仓扫描器（test_process_trace_contract.py 的 `_frame_type_literals_in`）拿
    `value in FRAME_NAMES` 当筛子，全新的名字对它天然隐形——这一帧当初就是这么漏的。
    判据只认 `"type": "<字面量>"` 这一种形状：变量拼出来的类型名它看不见，那一半没有机器
    判据（别说成"整个 app/ 都审过了"）。

    豁免登记的**理由**这里只判非空：机器判得了"这一行有没有写字"，判不了"写的那句对不对"，
    后一半归独立审查。别把它读成"登记过了就审过了"。
    """
    registered = {v for k, v in vars(se).items() if k.startswith("FRAME_")}
    declared = set(se.CLIENT_FRAMES) | set(stream_runs.PRIVATE_TRACE_FRAME_TYPES)
    naked = registered - declared
    assert not naked, "这些注册帧既不在 CLIENT_FRAMES 也不在隐私名单：%s" % sorted(naked)
    # 豁免名单不能当后门用：不在两端的帧更不该跨属主送出去
    assert set(se.NON_CLIENT_FRAMES) <= set(stream_runs.PRIVATE_TRACE_FRAME_TYPES), \
        "NON_CLIENT_FRAMES 里有帧没进隐私名单：豁免变成了漏帧的通道"
    # 登记的代价是那句话：空串也算登记过，所以这里判非空（多条豁免共用同一句是合法的，
    # 一组同族键本来就同一个理由，别改成"互不相同"）
    blank = sorted(k for k, v in DECLARED_HAND_WRITTEN.items() if not str(v).strip())
    assert not blank, "豁免登记里这些位置的理由是空的：%s——登记一行必须写清为什么忍" % blank

    app_root = BACKEND / "app"
    handwritten = set()
    for path in sorted(app_root.rglob("*.py")):
        rel = path.relative_to(app_root).as_posix()
        for found in re.findall(r'"type":\s*"([^"]+)"', path.read_text(encoding="utf-8")):
            handwritten.add((rel, found))
    undeclared = sorted((rel, t) for rel, t in handwritten
                        if t not in declared and (rel, t) not in DECLARED_HAND_WRITTEN)
    assert not undeclared, (
        "%s 里手写的类型名 %s 没有任何立场：带用户内容的帧必须在引入它的那一笔里进 "
        "PRIVATE_TRACE_FRAME_TYPES；不是帧的就在 DECLARED_HAND_WRITTEN 登记一行并写清为什么忍"
        % (undeclared[0][0] if undeclared else "?", sorted(t for _, t in undeclared)))
    stale = sorted(set(DECLARED_HAND_WRITTEN) - handwritten)
    assert not stale, ("豁免登记里这些位置已经没有手打的类型名了：%s——删掉对应那行，"
                       "清单只该越还越少" % stale)


def test_approval_frame_is_deliberately_not_a_client_frame():
    """不注册进 CLIENT_FRAMES 是刻意的，不是遗漏：注册即要求两个客户端各多一条分发臂。

    「发起者本人要看」与「跨属主不许看」都不取决于在不在 CLIENT_FRAMES——真要注册就连
    api.js 的 if 链与 Api.kt 的 when 臂一起改，别只动这一行（改一半会被
    test_process_trace_contract.py 的逐臂判据当场拦下）。走豁免登记也得进
    NON_CLIENT_FRAMES，而那张表由上面那条结构锁钉成"必须在隐私名单里"。

    这也解释了为什么这一帧能漏过去这么久：契约那边的字面量扫描器只认 CLIENT_FRAMES 里的
    名字（`_frame_type_literals_in` 拿 `value in FRAME_NAMES` 当筛子），不在清单上的帧名
    对它天然隐形。
    """
    assert se.FRAME_APPROVAL_REQUEST not in se.CLIENT_FRAMES
    assert se.FRAME_APPROVAL_REQUEST in se.NON_CLIENT_FRAMES
    assert se.FRAME_APPROVAL_REQUEST in stream_runs.PRIVATE_TRACE_FRAME_TYPES


@pytest.fixture(autouse=True)
def _clean_runs():
    stream_runs.clear_all_for_tests()
    yield
    stream_runs.clear_all_for_tests()
