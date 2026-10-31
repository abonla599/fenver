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
