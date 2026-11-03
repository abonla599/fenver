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


def test_cancelled_done_carries_usage(client, monkeypatch):
    """取消路径的 done 帧必须带 usage：引擎 finally 已把 billed 回填进 usage_out，
    producer 的 cancelled 分支（main.py `_produce_stream`）收尾 done 用同一个字典。
    半截消耗是真花出去的钱，取消不能把它从账上抹掉。"""
    from app.core import streaming
    from app.core.stream_events import content_frame
    from app.core.streaming import StreamCancelled

    def cancelling_events(model, messages, **kwargs):
        out = kwargs.get("usage_out")
        assert out is not None, "producer 没把 usage_out 传给引擎"
        # 模拟引擎 finally 的结账：取消抛出之前，已消费的部分先回填
        out.update({"prompt_tokens": 33, "completion_tokens": 7,
                    "total_tokens": 40, "reasoning_tokens": 0, "cached_tokens": 0})
        yield content_frame("读到")
        raise StreamCancelled()

    monkeypatch.setattr(streaming, "stream_chat_events", cancelling_events)
    res = client.post("/v1/chat/stream",
                      json={"messages": [{"role": "user", "content": "hi"}]})
    assert res.status_code == 200, res.text
    frames = list(_data_frames(res.text))
    # 必须先有 cancelled 帧，再以 done 收尾（旧客户端只认 done，取消信息走新字段）
    types = [d.get("type") for d in frames]
    assert "cancelled" in types, res.text
    assert types[-1] == "done", res.text
    done = frames[-1]
    assert done["status"] == "cancelled"
    assert done["usage"]["prompt_tokens"] == 33, "cancelled done 丢了 usage"
    assert done["usage"]["total_tokens"] == 40
    # 旧字段骨架不因取消而缺：full_text 是在取消点前已流出的半句
    assert done["full_text"] == "读到"
    assert done["message_id"] and done["model"]
