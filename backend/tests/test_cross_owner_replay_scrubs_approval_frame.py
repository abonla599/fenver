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
④ 两枚结构锁，防的是"下一帧又漏"：任何在流上出现的类型名都必须先声明立场。

回退必红自查：
- 把 FRAME_APPROVAL_REQUEST 从 PRIVATE_TRACE_FRAME_TYPES 摘掉 → ①② 同时红；
- 把 sse_frames 的属主路径也接上 scrub → ③ 红；
- 把构造处改回手写字面量 → test_approval_frame_is_registered_by_constant… 红；
- 动一字节线上形状（键序或类型名）→ test_approval_frame_wire_bytes… 红。
"""
import json
import re
from pathlib import Path

import pytest

from app.core import authz, stream_events as se, stream_runs
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

# 与 streaming.py 构造处逐键同序：json.dumps 按插入顺序出字节，键序变了就是线上字节变了。
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

def test_approval_frame_wire_bytes_and_client_whitelists_unchanged():
    """换常量不许改一个比特：两个客户端早就各自认这个名字，改字节就是让老包解析崩。"""
    assert se.FRAME_APPROVAL_REQUEST == "approval_request"
    # 改动前的形状，按改动手写在这儿当基准（含键序与冒号后的空格）
    frozen = ('{"type": "approval_request", "ticket_id": "%s", "tool_name": "delete_files", '
              '"plan_summary": %s, "expires_at": 1730000000.0}'
              % (TICKET, json.dumps(PLAN_SUMMARY)))
    assert se.sse_data(dict(APPROVAL_FRAME)) == "data: " + frozen + "\n\n"
    js = (BACKEND / "app" / "web" / "static" / "api.js").read_text(encoding="utf-8")
    kt = (BACKEND.parent / "android-native" / "app" / "src" / "main" / "java"
          / "xyz" / "fenever" / "assistant" / "nativeapp" / "Api.kt").read_text(encoding="utf-8")
    assert '"approval_request"' in js, "网页侧不再认这一帧：老客户端会解析不到"
    assert '"approval_request" ->' in kt, "原生侧不再认这一帧：同上"


# ------------------------------------------------------------------ ④ 结构锁 --

# 手写帧的既成事实与它们的立场；新增一条必须同时写清"为什么可以裸着进补帧"。
DECLARED_HAND_WRITTEN = {
    "function": "这不是帧：同一处构造的是给上游 provider 的工具 schema",
    "cancelled": "带的是本轮正文，与 done 同一立场（跨属主本来就看得见回答本身）",
}


def test_every_frame_type_on_the_stream_declares_a_privacy_posture():
    """任何出现在流上的类型名都必须二选一：客户端注册帧，或隐私名单。

    漏了任何一格都不是"少删一帧"，而是跨属主逐字节透出，所以这里判的是**有没有立场**，
    不是立场对不对（对不对由上面三组行为断言判）。
    """
    registered = {v for k, v in vars(se).items() if k.startswith("FRAME_")}
    declared = set(se.CLIENT_FRAMES) | set(stream_runs.PRIVATE_TRACE_FRAME_TYPES)
    naked = registered - declared
    assert not naked, "这些注册帧既不在 CLIENT_FRAMES 也不在隐私名单：%s" % sorted(naked)
    # 豁免名单不能当后门用：不在两端的帧更不该跨属主送出去
    assert set(se.NON_CLIENT_FRAMES) <= set(stream_runs.PRIVATE_TRACE_FRAME_TYPES), \
        "NON_CLIENT_FRAMES 里有帧没进隐私名单：豁免变成了漏帧的通道"

    handwritten = set()
    for rel in ("app/core/streaming.py", "app/main.py"):
        src = (BACKEND / rel).read_text(encoding="utf-8")
        for found in re.findall(r'"type":\s*"([^"]+)"', src):
            handwritten.add((rel, found))
    undeclared = sorted((rel, t) for rel, t in handwritten
                        if t not in declared and t not in DECLARED_HAND_WRITTEN)
    assert not undeclared, (
        "%s 里手写的类型名 %s 没有任何立场：带用户内容的帧必须在引入它的那一笔里进 "
        "PRIVATE_TRACE_FRAME_TYPES" % (undeclared[0][0] if undeclared else "?",
                                       sorted(t for _, t in undeclared)))


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
