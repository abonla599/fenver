"""v0.39.x 性能 A 包 · 服务端读时合帧的行为契约（stream_runs.sse_frames）。

为什么要这一层：模型逐 token 吐字时，一轮回答会在缓冲里堆成千上万帧
content；旧行为是读一帧发一帧——每帧都带一次 yield、一次网络写、客户端
一次回调。合帧把相邻的纯 content 帧在**读出口**拼成一帧，SSE 输出量与
客户端渲染次数都塌缩一个量级，而缓冲（run.events）与生产侧一个字都不动。

判据六条，缺一条都不算锁住：
1. 相邻纯 content 合并成一帧：text 按顺序拼接、seq 取被合并的最后一帧、
   run_id 不变；
2. 中间夹着 thinking/tool_call 等非 content 帧时切成多段，段间顺序不乱；
3. 非 content 帧的输出逐字节（逐字典）不变——合帧只碰 content；
4. 合帧是**读时**行为：消费前后 run.events 不被改写（deepcopy 对照），
   append/缓冲预算/生产侧语义与本改动无关；
5. 续播边界：客户端拿着合并帧的末 seq 断线重连，只补后续帧，
   不重发已合并的正文、不跳过未发的帧；
6. 合帧窗口真的在等：窗口常量 COALESCE_SECONDS 临时调宽到 0.2s 后，
   晚到的相邻 content 会被并入同一帧（跑完必须恢复常量）。

反向锁（回退必红，实现落地后实测）：把 _merge_content 摘掉或让
sse_frames 直接逐帧透出 → 判据 1/2/5/6 同时红；把合并帧的 seq 改成
首帧 → 判据 1/5 红；只在 payload 键集合恰好是 {type,text,seq,run_id}
时合并——未来 content 帧长出第三个字段（如 omitted 记号）就该自动
不合并，这条由 test_only_plain_content_payloads_merge 钉住。
"""
import copy
import json
import threading
import time

import pytest

from app.core import stream_runs


@pytest.fixture(autouse=True)
def _clean_runs():
    stream_runs.clear_all_for_tests()
    yield
    stream_runs.clear_all_for_tests()


def _parse(chunk: str):
    """把一帧 SSE 文本拆成 (id行里的 seq 或 None, payload 字典)。"""
    rid_seq = None
    data = None
    for line in chunk.split("\n"):
        if line.startswith("id: "):
            rid_seq = int(line[4:].rsplit(":", 1)[1])
        elif line.startswith("data: "):
            data = json.loads(line[6:])
    return rid_seq, data


def _drain(gen):
    out = []
    try:
        out.extend(gen)
    finally:
        gen.close()
    return out


def _merge(batch):
    """直测纯函数：入参形状与 sse_frames 内部的 batch 一致。"""
    return stream_runs._merge_content(batch)


def _ev(payload):
    return {"seq": payload["seq"], "payload": payload}


# ------------------------------------------------ 1. 相邻纯 content 合并 --

def test_adjacent_plain_content_frames_merge_with_last_seq():
    run = stream_runs.create_run(user_id="u1")
    run.append({"type": "content", "text": "甲"})
    run.append({"type": "content", "text": "乙"})
    run.append({"type": "content", "text": "丙"})
    run.append({"type": "done", "full_text": "甲乙丙"})
    run.finish("completed")

    frames = _drain(stream_runs.sse_frames(run, 0))
    parsed = [_parse(c) for c in frames]
    types = [p["type"] for _, p in parsed]
    assert types == ["content", "done"], types
    seq_content = parsed[0][0]
    assert seq_content == 3, "合并帧的 seq 必须取被合并的最后一帧"
    assert parsed[0][1]["seq"] == 3
    assert parsed[0][1]["text"] == "甲乙丙", "text 必须按缓冲顺序拼接"
    assert parsed[0][1]["run_id"] == run.run_id


def test_merge_content_is_pure_and_batch_shaped():
    """纯函数面：同批输入两次调用结果一致，且不改写传入的 batch。"""
    batch = [{"seq": 1, "payload": {"type": "content", "text": "a",
                                    "seq": 1, "run_id": "r"}},
             {"seq": 2, "payload": {"type": "content", "text": "b",
                                    "seq": 2, "run_id": "r"}}]
    snapshot = copy.deepcopy(batch)
    merged = _merge(batch)
    assert batch == snapshot, "_merge_content 不许就地改写输入"
    assert len(merged) == 1
    assert merged[0]["seq"] == 2
    assert merged[0]["payload"]["text"] == "ab"
    assert _merge(batch) == merged, "同一批两次调用结果必须一致"


# ----------------------------------------- 2. 非 content 隔断：切成多段 --

def test_interleaved_frames_split_segments_and_keep_order():
    run = stream_runs.create_run(user_id="u1")
    run.append({"type": "start", "message_id": "m", "model": "fake"})
    run.append({"type": "content", "text": "一"})
    run.append({"type": "content", "text": "二"})
    run.append({"type": "thinking", "text": "想一下"})
    run.append({"type": "tool_call", "call_id": "c1", "name": "calc", "args": {}})
    run.append({"type": "content", "text": "三"})
    run.append({"type": "content", "text": "四"})
    run.append({"type": "done", "full_text": "一二三四"})
    run.finish("completed")

    parsed = [_parse(c) for c in _drain(stream_runs.sse_frames(run, 0))]
    types = [p["type"] for _, p in parsed]
    assert types == ["start", "content", "thinking", "tool_call", "content", "done"], types
    contents = [p for _, p in parsed if p["type"] == "content"]
    assert [c["text"] for c in contents] == ["一二", "三四"]
    assert [c["seq"] for c in contents] == [3, 7]
    assert [p["seq"] for _, p in parsed if p["type"] != "content"] == [1, 4, 5, 8], \
        "非 content 帧的序号与顺序不许被动过"


# ------------------------------------------------ 3. 非 content 逐字节不变 --

def test_non_content_frames_are_byte_identical():
    run = stream_runs.create_run(user_id="u1")
    run.append({"type": "start", "message_id": "m1", "model": "fake-model"})
    run.append({"type": "thinking", "text": "私密思考"})
    run.append({"type": "done", "full_text": "答", "message_id": "m1",
                "model": "fake-model"})
    run.finish("completed")

    frames = _drain(stream_runs.sse_frames(run, 0))
    for chunk in frames:
        _, payload = _parse(chunk)
        assert payload["type"] != "content"
        # 逐字节：id 行 + data 行原样来自 _frame(缓冲里的那个 payload)
        expected = stream_runs._frame(run.run_id, payload["seq"],
                                      run.events[payload["seq"] - 1]["payload"])
        assert chunk == expected


# ------------------------------------------------ 4. 缓冲不被改写（读时） --

def test_consumption_does_not_rewrite_run_events():
    run = stream_runs.create_run(user_id="u1")
    for t in ("甲", "乙", "丙"):
        run.append({"type": "content", "text": t})
    run.append({"type": "done", "full_text": "甲乙丙"})
    run.finish("completed")

    before = copy.deepcopy(run.events)
    _drain(stream_runs.sse_frames(run, 0))
    assert run.events == before, "读时合帧把改写漏进了缓冲：生产侧对象不许被动"


# ------------------------------------------------------ 5. 续播不重发正文 --

def test_resume_after_merged_frame_replays_only_the_tail():
    """合并帧发出后断线：cursor=合并帧末 seq，重连只补它之后的帧。"""
    run = stream_runs.create_run(user_id="u1")
    run.append({"type": "content", "text": "甲"})
    run.append({"type": "content", "text": "乙"})
    run.append({"type": "content", "text": "丙"})
    run.finish("completed")  # 先不 finish 也行：这里走"已终态"支路

    gen = stream_runs.sse_frames(run, 0)
    first = _parse(next(gen))
    assert first[1]["text"] == "甲乙丙" and first[0] == 3, first
    gen.close()  # 假装断线：客户端拿着 id 行里的 run:3 重连

    rejoin = [_parse(c) for c in _drain(stream_runs.sse_frames(run, 3))]
    assert not any(p["type"] == "content" for _, p in rejoin), \
        f"续播把已合并的正文重发了一遍：{rejoin}"


def test_resume_mid_stream_gets_coalesced_tail_without_loss():
    """未终态时断线在合并帧上：cursor=末 seq，重连补上后到的正文，一字不丢。"""
    run = stream_runs.create_run(user_id="u1")
    run.append({"type": "content", "text": "前半"})
    run.append({"type": "content", "text": "句"})

    gen = stream_runs.sse_frames(run, 0)
    t = threading.Timer(0.05, lambda: (run.append({"type": "content", "text": "后半"}),
                                       run.append({"type": "done", "full_text": "前半句后半"}),
                                       run.finish("completed")))
    t.start()
    try:
        head = _parse(next(gen))
    finally:
        t.join(2.0)
    assert head[0] >= 2 and head[1]["type"] == "content", head
    got = head[1]["text"]
    gen.close()  # 假装断线：游标就是刚拿到的合并帧 id 行里的 seq
    tail = [_parse(c) for c in _drain(stream_runs.sse_frames(run, head[0]))]
    tail_text = "".join(p["text"] for _, p in tail if p["type"] == "content")
    assert got + tail_text == "前半句后半", (got, tail_text)
    assert "前半" not in tail_text, "续播把已发正文重发了一遍"


# --------------------------------------------- 6. 合帧窗口真的在等（0.2s） --

def test_coalesce_window_waits_and_merges_late_content():
    """把窗口临时调宽到 0.2s：晚 0.05s 到的相邻 content 必须被并入同一帧。

    这条同时钉住"只等一次"的反面——旧实现（不等、逐帧透出）里 first 只会
    拿到 seq=1 的"来"；而若无窗口重读，后到的"字"要单独成帧。
    """
    prev = stream_runs.COALESCE_SECONDS
    stream_runs.COALESCE_SECONDS = 0.2
    try:
        run = stream_runs.create_run(user_id="u1")
        run.append({"type": "content", "text": "来"})
        gen = stream_runs.sse_frames(run, 0)

        t = threading.Timer(0.05, lambda: run.append({"type": "content", "text": "字"}))
        t.start()
        t0 = time.monotonic()
        try:
            first = _parse(next(gen))
        finally:
            elapsed = time.monotonic() - t0
            t.join(2.0)

        assert first[1]["text"] == "来字", \
            f"窗口没等到晚到的 content（first={first}）：合帧失效或没在等"
        assert first[0] == 2, "合并帧 seq 应取晚到那帧的序号"
        assert elapsed >= 0.04, f"窗口没真的等（{elapsed:.3f}s 就返回了）"
        # 终帧补一条 done 收尾，保证上面 close 生成器前缓冲是自洽的一轮
        run.append({"type": "done", "full_text": "来字"})
        run.finish("completed")
        tail = [_parse(c) for c in _drain(gen)]
        assert [p["type"] for _, p in tail] == ["done"], tail
    finally:
        stream_runs.COALESCE_SECONDS = prev


def test_coalesce_does_not_wait_for_non_content_tail():
    """非 content 收尾不等待：批尾是 done 时首帧立刻出（时间下界由窗口=0 验证不了，
    这里用窗口=0.2 + 已终态双重保证等待根本不该发生，elapsed 远小于窗口）。"""
    prev = stream_runs.COALESCE_SECONDS
    stream_runs.COALESCE_SECONDS = 0.2
    try:
        run = stream_runs.create_run(user_id="u1")
        run.append({"type": "content", "text": "甲"})
        run.append({"type": "done", "full_text": "甲"})
        run.finish("completed")
        t0 = time.monotonic()
        frames = _drain(stream_runs.sse_frames(run, 0))
        assert time.monotonic() - t0 < 0.15, "已终态/非 content 收尾不该吃合帧窗口"
        assert [p["type"] for _, p in map(_parse, frames)] == ["content", "done"]
    finally:
        stream_runs.COALESCE_SECONDS = prev


# ------------------------------------------- payload 键集合收紧：未来字段不并 --

def test_only_plain_content_payloads_merge():
    """content 帧一旦长出新字段（键集合不再是 {type,text,seq,run_id}）就不许合并。

    这是给"未来加字段"上的保险：合帧的语义前提是帧里除正文与序号外没有别的
    可丢失信息；哪天 content 带上记账字段，逐帧形状必须先由语义审查再谈合并。
    """
    batch = [{"seq": 1, "payload": {"type": "content", "text": "a",
                                    "seq": 1, "run_id": "r", "omitted": 3}},
             {"seq": 2, "payload": {"type": "content", "text": "b",
                                    "seq": 2, "run_id": "r"}}]
    merged = _merge(batch)
    assert [m["seq"] for m in merged] == [1, 2], "带额外键的帧被并掉了：保险失效"
    # 反向对照：同样两帧去掉额外键就必须合并（证明红在键集合，不是别的）
    batch2 = [dict(batch[0], payload={k: v for k, v in batch[0]["payload"].items()
                                      if k != "omitted"}), batch[1]]
    assert [m["seq"] for m in _merge(batch2)] == [2]
