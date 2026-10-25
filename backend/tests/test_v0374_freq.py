"""v0.37.4 T4：`web_read` 的频控档（600 秒 / 4 页 / 人）。

判据：
- 档位恰为 `(600.0, 4)`，既有 execute_code / plan_add / http_fetch 档位不变；
- 第 1–4 次真的执行（fake fetch 计数 4），第 5 次在执行器闸门被挡、fetch 仍为 4；
- 被挡的调用不进入工具函数，因此不 lookup、不抓取、不写审计；
- 桶键是服务端 user_id：A 用满不影响 B，模型在参数里塞 user_id 也换不了桶；
- 窗口过后恢复；无身份调用在 needs_user 那道闸门就被拒。
"""
from collections import defaultdict

import pytest

from app.tools import executor as ex
from app.tools import fetch_core, result_store
from app.tools.executor import execute_tool


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """频控账本与结果句柄都是进程级全局：每条用例一份自己的。"""
    monkeypatch.setattr(ex, "_FREQ_LEDGER", defaultdict(list))
    result_store.clear()
    yield
    result_store.clear()


def _mint(owner="u", url="https://example.com/a"):
    ids = result_store.remember(
        [{"title": "标题", "url": url, "snippet": "摘要"}], owner=owner)
    assert ids[0]
    return ids[0]


def _fake_fetch(monkeypatch):
    calls = []

    def fake(url, **kwargs):
        calls.append((url, kwargs))
        return fetch_core.FetchOutcome(
            ok=True, text="正文", status="ok", host="example.com", path="/a",
            bytes=6, final_url=url, final_status=200, content_type="text/plain")

    monkeypatch.setattr(fetch_core, "fetch", fake)
    return calls


def _read(rid, user_id="u", arguments=None):
    return execute_tool("web_read", arguments or {"result_id": rid}, user_id=user_id)


def test_web_read_tier_is_the_decided_window_and_cap():
    assert ex._FREQ_LIMITS["web_read"] == (600.0, 4)
    # 既有档位一个数都不许跟着漂。
    assert ex._FREQ_LIMITS["execute_code"] == (600.0, 10)
    assert ex._FREQ_LIMITS["plan_add"] == (900.0, 6)
    assert ex._FREQ_LIMITS["http_fetch"] == (600.0, 10)


def test_the_fifth_read_is_blocked_before_any_fetch(monkeypatch):
    rid = _mint(owner="u")
    calls = _fake_fetch(monkeypatch)

    for n in range(1, 5):
        out = _read(rid)
        assert out.startswith("✓ "), f"第 {n} 次不该被挡：{out[:80]}"
    assert len(calls) == 4

    fifth = _read(rid)
    assert fifth.startswith("✗ "), fifth[:80]
    assert "调用过于频繁" in fifth
    assert "web_read 每 600 秒最多 4 次" in fifth
    assert len(calls) == 4, "被频控挡下的调用仍然抓取了页面"


def test_a_blocked_call_does_not_touch_the_store(monkeypatch):
    rid = _mint(owner="u")
    _fake_fetch(monkeypatch)
    for _ in range(4):
        _read(rid)
    before = {key: dict(value) for key, value in result_store._store.items()}

    boom_calls = []

    def boom(*_args, **_kwargs):
        boom_calls.append(1)
        raise AssertionError("被频控挡下的调用进了工具函数")

    monkeypatch.setattr(result_store, "lookup", boom)
    out = _read(rid)
    assert "调用过于频繁" in out
    assert boom_calls == [], "第 5 次调用已经 lookup 过了，频控闸门放晚了"
    assert result_store._store == before


def test_buckets_are_per_user_not_per_result(monkeypatch):
    a_id = _mint(owner="A")
    b_id = _mint(owner="B")
    calls = _fake_fetch(monkeypatch)
    for _ in range(4):
        assert _read(a_id, user_id="A").startswith("✓ ")
    assert _read(a_id, user_id="A").startswith("✗ "), "A 的桶没有封顶"

    out = _read(b_id, user_id="B")
    assert out.startswith("✓ "), f"B 被 A 的用量连坐了：{out[:80]}"
    assert len(calls) == 5


def test_the_model_cannot_pick_its_own_freq_bucket(monkeypatch):
    rid = _mint(owner="A")
    _fake_fetch(monkeypatch)
    for _ in range(4):
        _read(rid, user_id="A")
    # 模型在 arguments 里塞 user_id：schema 清洗会丢掉它，频控键仍是服务端的 A。
    out = _read(rid, user_id="A", arguments={"result_id": rid, "user_id": "B"})
    assert "调用过于频繁" in out, "模型参数换掉了频控桶"


def test_the_window_slides_after_ten_minutes(monkeypatch):
    rid = _mint(owner="u")
    calls = _fake_fetch(monkeypatch)
    for _ in range(4):
        _read(rid)
    assert _read(rid).startswith("✗ ")

    # 把这一桶的时间戳整体拨回 601 秒：滑动窗口里一条都不剩，下一次该放行。
    for key, hits in list(ex._FREQ_LEDGER.items()):
        if key[0] == "web_read":
            ex._FREQ_LEDGER[key] = [stamp - 601 for stamp in hits]
    out = _read(rid)
    assert out.startswith("✓ "), f"窗口过后没有恢复：{out[:80]}"
    assert len(calls) == 5


def test_a_read_without_an_identity_is_refused_before_freq(monkeypatch):
    rid = _mint(owner="")
    calls = _fake_fetch(monkeypatch)
    out = execute_tool("web_read", {"result_id": rid}, user_id=None)
    assert "缺少身份" in out
    assert calls == []
    assert not ex._FREQ_LEDGER.get(("web_read", "anon")), \
        "无身份调用不该在频控账上留下一个匿名桶"
