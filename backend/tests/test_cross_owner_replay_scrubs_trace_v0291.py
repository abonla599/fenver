"""跨属主续播不许把「过程留痕」一起带出去（v0.29.1 补的隐私边界）。

形状是怎么长出来的：v0.25 R3 给续播留了一条 admin 可跨属主接上别人 run 的口子
（main.py 的 resume 分支，注释原话"与任务面同权同形"）。那时候那条缓冲里只有
start/content/done，跨属主接上去看到的是**回答正文**——与任务面看到的结果同量级，
所以那口子当时不算越界。

v0.29 把思考文本、工具入参与执行结果、搜索命中写进了**同一个**缓冲。于是同一条
早就存在的通道，开始往外送模型对用户输入的复述、用户的私有参数、以及用户查了
什么。这没人批准过：管理端从来不读任何人的会话内容——本文件第 ① 组断言把这件事
钉成证据（/v1/sessions* 全部要属主，非属主拿 404；admin 界面走的 /v1/admin/* 一条
都不返回消息正文）。所以"能不能接上"照旧同权同形，"接上之后能看见什么"必须收窄。

判据四条，缺一条都不算锁住：
① 属主与非属主的读权限形状没被这次改动扩大（回归基线，先钉住"以前就没有"）；
② 出口减法本身：三类过程帧整条不吐、done 的 trace 删掉、骨架帧逐字节原样、
   序号可以留空档但 id 行必须在（旧客户端靠 data 行的 type 分发）；
③ admin 跨属主真打一次端点：拿到的流里没有过程留痕，正文照旧完整；
④ 同一轮由属主续播：过程留痕必须在（防止把新功能一起"修"没了）；
   外加普通用户续播别人的 run 仍是那一句逐字节相同的 410（run_id 不是探测信道）。

回退必红自查：
- 把 main.py 里 `if run.user_id != principal.user_id` 那三行删掉 → test ③ 红
  （admin 又能看见 thinking/tool/search 与 done.trace）；
- 把 stream_runs.scrub_private_trace 的 PRIVATE_TRACE_FRAME_TYPES 改成空集 →
  test ②③ 红；
- 把 scrub 写成"连 done 一起删" → test ② 的终帧断言与 ③ 的正文断言同时红；
- 把 sse_frames 的 owner 路径也接上 scrub → test ④ 红（过程面板从此看不见）。
"""
import json

import pytest

from app.core import authz, stream_events as se, stream_runs
from app.core.authz import Principal
from app.main import app

BOOT = {"Authorization": "Bearer boot-token"}
VICTIM = "u_victim_42"


# ------------------------------------------------------------------ 造一轮带留痕的流 --

def _a_run_with_full_process(owner: str = VICTIM):
    """按生产侧的真实顺序灌一帧一帧：想 → 要去搜 → 搜完 → 正文 → done(带留痕)。"""
    run = stream_runs.create_run(user_id=owner, session_id="s_probe")
    run.append(se.start_frame(run.message_id, "probe-model"))
    run.append(se.thinking_frame("先把问题拆开，看哪一半要外部资料。"))
    run.append(se.tool_call_frame("call_1", "web_search", {"query": "Fenver 发布记录"}))
    run.append(se.tool_result_frame("call_1", "web_search", True, "命中 2 条", 12))
    run.append(se.search_frame("call_1", "Fenver 发布记录", [
        {"title": "Fenver v0.29 发布记录", "url": "https://fenever.example/releases/29",
         "snippet": "这一版加了过程留痕"},
    ]))
    run.append(se.content_frame("正文第一句"))
    run.append(se.content_frame("，正文第二句"))
    trace = [se.step_thinking("先把问题拆开，看哪一半要外部资料。"),
             se.step_tool("call_1", "web_search", "搜索 Fenver 发布记录", True, "命中 2 条", 12),
             se.step_search("call_1", "Fenver 发布记录",
                            [{"title": "Fenver v0.29 发布记录",
                              "url": "https://fenever.example/releases/29"}])]
    run.append(se.done_frame("正文第一句，正文第二句", run.message_id, "probe-model",
                             trace=trace))
    run.finish("completed")
    return run


def _drain(frames):
    """把补帧生成器跑完并 close：close 也测，R3b 的读者计数系在 finally 上。"""
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
        if not sep:
            continue
        try:
            out.append(json.loads(rest.partition("\n\n")[0]).get("type"))
        except ValueError:
            out.append("<unparsable>")
    return out


# ------------------------------------------------------------------ ① 读权限的形状没变 --

def test_session_reads_are_owner_only_and_admin_reads_no_message_body(client):
    """① 先钉住"以前就没有"：admin 面（/v1/admin/*）一条消息正文都不返回。

    这条不是新功能的装饰——它是第 ③ 组判据的前提。若 admin 本来就能从别处读到
    别人的 messages，那"续播不送留痕"只是遮住半扇门。判据取"字段名"而不是"内容"：
    管理员面拿到的任何回包里不许出现 messages / trace / full_text 这三个键，
    它们只在属主自己的会话读路径上存在（main.py 的 GET /v1/sessions/{id}）。
    """
    sid = client.post("/v1/sessions", headers=BOOT).json()["session_id"]
    mine = client.get(f"/v1/sessions/{sid}", headers=BOOT)
    assert mine.status_code == 200, mine.text[:200]
    assert isinstance(mine.json().get("data", mine.json()).get("messages"), list)

    admin_paths = ("/v1/admin/users", "/v1/admin/usage", "/v1/admin/user-feedback",
                   "/v1/admin/audit")
    for path in admin_paths:
        r = client.get(path, headers=BOOT)
        assert r.status_code in (200, 403, 404), (path, r.status_code)
        if r.status_code != 200:
            continue
        for banned in ('"messages"', '"trace"', '"full_text"'):
            assert banned not in r.text, f"{path} 开始往外送消息体：{banned}"


# ------------------------------------------------------------------ ② 出口的减法 --

def test_scrub_removes_process_frames_and_keeps_the_skeleton_byte_identical():
    """② 摘哪些、留哪些，逐条对账；留下的必须**逐字节**没被动过。

    "只删该删的"和"删完别把别的碰坏"是两件事：done 是重新序列化的（少了 trace），
    其余帧必须原样。旧客户端只看 data 行里的 type 分发、看 id 行记游标，所以
    空档可以、id 行不能少。
    """
    run = _a_run_with_full_process()
    owner_frames = _drain(stream_runs.sse_frames(run, 0))
    scrubbed = _drain(stream_runs.scrub_private_trace(
        stream_runs.sse_frames(run, 0)))

    assert set(_types(owner_frames)) >= {se.FRAME_THINKING, se.FRAME_TOOL_CALL,
                                         se.FRAME_TOOL_RESULT, se.FRAME_SEARCH}, owner_frames
    gone = {se.FRAME_THINKING, se.FRAME_TOOL_CALL, se.FRAME_TOOL_RESULT, se.FRAME_SEARCH}
    kept_types = _types(scrubbed)
    assert not (set(kept_types) & gone), kept_types
    assert kept_types.count(se.FRAME_START) == 1 and kept_types.count(se.FRAME_DONE) == 1
    assert kept_types.count(se.FRAME_CONTENT) == 2, kept_types

    # 骨架帧原样：start/content 两条在两个视图里逐字节相同
    same = [c for c in owner_frames if '"type": "' + se.FRAME_START in c
            or '"type": "' + se.FRAME_CONTENT in c]
    for chunk in same:
        assert chunk in scrubbed, "非留痕帧被重写过：形状不该变"

    # done：只少 trace，其余字段原样；id 行必须在
    done = next(c for c in scrubbed if '"type": "' + se.FRAME_DONE in c)
    assert done.startswith("id: " + run.run_id + ":"), done[:40]
    payload = json.loads(done.partition("data: ")[2].partition("\n\n")[0])
    assert "trace" not in payload, payload
    assert payload["full_text"] == "正文第一句，正文第二句", payload
    assert payload["message_id"] == run.message_id and payload["model"] == "probe-model", payload
    assert payload["seq"] == json.loads(
        next(c for c in owner_frames if '"type": "' + se.FRAME_DONE in c)
        .partition("data: ")[2].partition("\n\n")[0])["seq"], "序号被顺手改了"

    # 空档合规：scrub 后每个 id 的 seq 仍严格递增，且不超过原始最大 seq
    def _seq(chunk):
        return int(chunk.partition("\n")[0].rsplit(":", 1)[1])
    seqs = [_seq(c) for c in scrubbed if c.startswith("id: ")]
    assert seqs == sorted(set(seqs)) and seqs[-1] == max(_seq(c) for c in owner_frames)


def test_scrub_closes_the_inner_generator_so_readers_detach():
    """②-b 外层被 close 时内层必须一起关掉：detach_reader 挂在 sse_frames 的 finally。

    少了这一手，读者计数的"归零"要等 GC，R3b 那套"没有活读者就停宽限"会晚醒，
    而这条没有任何用户可见症状——正是要靠断言钉住的形状。
    """
    run = _a_run_with_full_process()
    assert run._readers == 0
    gen = stream_runs.scrub_private_trace(stream_runs.sse_frames(run, 0))
    assert next(gen)                      # 一进就 attach
    assert run._readers == 1, run._readers
    gen.close()
    assert run._readers == 0, "close 没传到内层：读者一直在场，宽限计时永不启动"


# ------------------------------------------------------------------ ③ admin 跨属主实测 --

def test_admin_resume_of_another_users_run_carries_no_process_trace(client):
    """③ 真打一次端点：admin 用 Last-Event-ID 接上别人的 run，留痕必须不在流里。

    这条是本文件的正题。admin 的合法性没被收回（仍能接上、仍拿 200 与正文），
    收回的只是"顺手看见别人在想什么、查了什么、工具带了什么参数"。
    """
    run = _a_run_with_full_process()
    r = client.post("/v1/chat/stream", headers={**BOOT, "Last-Event-ID": f"{run.run_id}:0"},
                    json={"messages": [{"role": "user", "content": "不该被调用"}]})
    assert r.status_code == 200, r.text[:200]
    chunks = [line for line in r.text.splitlines() if line.startswith("data: ")]
    types = [json.loads(c[6:]).get("type") for c in chunks]
    assert se.FRAME_THINKING not in types and se.FRAME_TOOL_CALL not in types \
        and se.FRAME_TOOL_RESULT not in types and se.FRAME_SEARCH not in types, types
    done = next(json.loads(c[6:]) for c in chunks
                if json.loads(c[6:]).get("type") == se.FRAME_DONE)
    assert "trace" not in done, done
    assert done["full_text"] == "正文第一句，正文第二句", done
    # 流里连一个私有串都不该出现：它们只长在留痕帧与 done.trace 里
    for leak in ("先把问题拆开", "命中 2 条", "Fenver 发布记录", "fenever.example"):
        assert leak not in r.text, leak


# ------------------------------------------------------------------ ④ 属主与新功能没被误伤 --

def test_owner_resume_still_sees_the_full_process(client):
    """④ 同一轮由属主续播：思考/工具/搜索与 done.trace 必须在。

    少了这条，"收窄"很容易一路收成功能消失——而过程面板正是 v0.29 的全部内容。
    """
    run = _a_run_with_full_process(owner="u_owner_here")
    app.dependency_overrides[authz.current_principal] = \
        lambda: Principal("u_owner_here", "属主本人", "user")
    try:
        r = client.post("/v1/chat/stream",
                        headers={"Last-Event-ID": f"{run.run_id}:0"},
                        json={"messages": [{"role": "user", "content": "不该被调用"}]})
    finally:
        app.dependency_overrides.pop(authz.current_principal, None)
    assert r.status_code == 200, r.text[:200]
    types = [json.loads(l[6:]).get("type") for l in r.text.splitlines()
             if l.startswith("data: ")]
    assert {se.FRAME_THINKING, se.FRAME_TOOL_CALL, se.FRAME_TOOL_RESULT,
            se.FRAME_SEARCH} <= set(types), types
    done = next(json.loads(l[6:]) for l in r.text.splitlines()
                if l.startswith("data: ") and json.loads(l[6:]).get("type") == se.FRAME_DONE)
    assert done.get("trace"), "属主也拿不到留痕了：新功能被修没了"


def test_plain_user_resume_of_another_run_is_still_the_same_410(client):
    """④-b 普通用户续播别人的 run：还是那一句逐字节相同的 410，探测面没变大。

    这次改动只在"已经允许接上"之后做减法；归属判定一个字都没动。若有人把
    mine 改成"存在即放行"，这条会红。
    """
    run = _a_run_with_full_process(owner="u_other_side")
    app.dependency_overrides[authz.current_principal] = \
        lambda: Principal("u_intruder", "旁人", "user")
    try:
        r = client.post("/v1/chat/stream",
                        headers={"Last-Event-ID": f"{run.run_id}:0"},
                        json={"messages": [{"role": "user", "content": "x"}]})
        missing = client.post("/v1/chat/stream",
                              headers={"Last-Event-ID": "00000000-0000-4000-8000-000000000000:0"},
                              json={"messages": [{"role": "user", "content": "x"}]})
    finally:
        app.dependency_overrides.pop(authz.current_principal, None)
    assert r.status_code == 410 and missing.status_code == 410, (r.status_code, missing.status_code)
    assert r.json() == missing.json(), "两类失败开始不一样：run_id 变成探测信道了"


@pytest.fixture(autouse=True)
def _clean_runs():
    stream_runs.clear_all_for_tests()
    yield
    stream_runs.clear_all_for_tests()
