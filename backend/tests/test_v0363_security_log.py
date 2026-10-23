"""v0.36.3 安全日志的行为锁：脱敏、合并、有界、懒加载、压缩、热路径零 I/O。

这些用例只碰 `tmp_path` 下的文件（conftest 已把 SECURITY_LOG_PATH 指进测试临时根，
这里再各指各的，双保险）。每条用例自带 reset，不把进程内的事件环带出用例。
"""
import builtins
import hashlib
import json
import os
import re
import time
from types import SimpleNamespace

import pytest

from app.core import security_log


@pytest.fixture(autouse=True)
def slog(tmp_path):
    path = tmp_path / "security_log.jsonl"
    security_log.reset_for_tests(path=str(path), autoflush=False)
    yield path
    security_log.reset_for_tests(path=None, autoflush=False)


def _event(**overrides):
    base = {"ip": "203.0.113.77", "account": "someone",
            "route": "/v1/auth/login", "status": 401, "detail": "login.failed"}
    base.update(overrides)
    return security_log.record("auth.fail", **base)


def _lines(path):
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------- 脱敏：原始 IP 永不落盘 ----------

def test_raw_ip_never_reaches_disk_only_prefix_and_fingerprint(slog):
    event = _event()
    assert event is not None
    assert security_log.flush() == 1
    raw = slog.read_text(encoding="utf-8")
    assert "203.0.113.77" not in raw, "原始 IP 落盘了"
    assert event["source"] == "203.0.113.0/24"
    assert "203.0.113.0/24" in raw
    assert re.fullmatch(r"[0-9a-f]{8}", event["source_fp"])
    expected = hashlib.sha256(b"203.0.113.77").hexdigest()[:8]
    assert event["source_fp"] == expected
    assert "203.0.113.77" not in json.dumps(
        [dict(item) for item in security_log.snapshot(3600)], ensure_ascii=False)


def test_the_raw_ip_assertion_has_teeth(tmp_path, monkeypatch):
    """拆闸对照：把脱敏换成原样，上面那条"原始 IP 不在盘上"必须能变红。"""
    monkeypatch.setattr(security_log, "redact_ip",
                        lambda raw: (str(raw), "deadbeef"))
    path = tmp_path / "control.jsonl"
    security_log.reset_for_tests(path=str(path), autoflush=False)
    _event()
    security_log.flush()
    assert "203.0.113.77" in path.read_text(encoding="utf-8"), (
        "脱敏被拆掉后检查仍看不见原始 IP——那条断言是空转的")


def test_ipv6_keeps_only_a_48_bit_prefix():
    event = _event(ip="[2001:db8:abcd:12::9]:443")
    assert event["source"] == "2001:db8:abcd::/48"
    assert re.fullmatch(r"[0-9a-f]{8}", event["source_fp"])


def test_unparseable_ip_becomes_unknown(slog):
    event = _event(ip="not-an-ip")
    assert (event["source"], event["source_fp"]) == ("unknown", "")
    assert "not-an-ip" not in json.dumps(event)


def test_request_supplies_path_and_cf_ip_but_never_query():
    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        headers={"cf-connecting-ip": "198.51.100.9"},
        url=SimpleNamespace(path="/v1/chat"))
    event = security_log.record("auth.fail", request=request)
    assert event["route"] == "/v1/chat"
    assert event["source"] == "198.51.100.0/24"


def test_route_with_invalid_characters_is_replaced():
    event = _event(route="/v1/auth/login?token=leak")
    assert event["route"] == "<invalid>"


# ---------- 白名单与账号清洗 ----------

def test_unknown_detail_falls_back_to_unspecified():
    assert _event(detail="evil.payload")["detail"] == "unspecified"


def test_unknown_type_is_ignored_and_never_lands():
    before = len(security_log._EVENTS)
    assert security_log.record("auth.evil", ip="203.0.113.77") is None
    assert len(security_log._EVENTS) == before


def test_account_control_chars_are_stripped_and_length_capped():
    event = _event(account="\x00ab\u200bcd" + "x" * 100)
    assert event["account"].startswith("abcd")
    assert len(event["account"]) == 64
    assert _event(account="   ")["account"] == "anonymous"


# ---------- 合并：只合并尚未落盘的同键事件 ----------

def test_same_key_within_a_minute_merges_into_one_pending_event(slog):
    first = _event(now=1000.0)
    second = _event(now=1030.0)
    assert first is second and first["count"] == 2 and first["ts"] == 1030.0
    security_log.flush()
    assert len(_lines(slog)) == 1
    assert json.loads(_lines(slog)[0])["count"] == 2


def test_a_persisted_event_is_never_merged_again(slog):
    _event(now=1000.0)
    security_log.flush()
    later = _event(now=1001.0)          # 同一合并窗口，但上一条已经落盘
    assert later["id"] != 1 and later["count"] == 1
    security_log.flush()
    rows = [json.loads(ln) for ln in _lines(slog)]
    assert [row["id"] for row in rows] == [1, 2]
    assert [row["count"] for row in rows] == [1, 1]


def test_different_keys_never_merge():
    a = _event(account="alice", now=1000.0)
    b = _event(account="bob", now=1000.0)
    assert a["id"] != b["id"]
    assert (a["count"], b["count"]) == (1, 1)


# ---------- 有界 ----------

def test_memory_and_merge_table_are_bounded_under_a_flood():
    for i in range(4200):
        security_log.record("auth.fail", ip="203.0.113.77",
                            account=f"user-{i}", route="/v1/auth/login",
                            status=401, detail="login.failed", now=1000.0 + i)
    assert len(security_log._EVENTS) <= security_log.MAX_EVENTS == 2000
    assert len(security_log._MERGE) <= security_log.MERGE_KEY_LIMIT == 4096
    assert len(security_log._PENDING) <= security_log.MAX_PENDING == 2000


# ---------- 懒加载 / 坏行 / 30 天过期 ----------

def test_lazy_load_skips_bad_lines_and_drops_events_older_than_30_days(slog):
    now = time.time()
    good = {"id": 3, "ts": now - 60, "type": "auth.fail", "severity": "warn",
            "source": "203.0.113.0/24", "source_fp": "abcdef01",
            "account": "old", "route": "/v1/auth/login", "status": 401,
            "count": 1, "detail": "login.failed"}
    stale = dict(good, id=5, ts=now - 31 * 86400, account="stale")
    slog.write_text("\n".join([json.dumps(good), "{broken", json.dumps(stale)]),
                    encoding="utf-8")
    security_log._LOADED = False          # 模拟"进程第一次真的用到它"
    items, _ = security_log.events()
    assert [item["id"] for item in items] == [3], "坏行放过了、30 天前的没丢"
    fresh = _event(account="new")
    assert fresh["id"] == 6, "新 id 必须接盘上最大值（含已过期那行）+1"


# ---------- 压缩 ----------

def test_file_is_compacted_when_it_exceeds_the_size_limit(slog, monkeypatch):
    monkeypatch.setattr(security_log, "MAX_FILE_BYTES", 200)
    monkeypatch.setattr(security_log, "MAX_FILE_LINES", 10 ** 9)
    # 盘上先放一段垃圾撑过阈值；flush 追加后触发压缩，垃圾必须被整体重写掉。
    slog.write_text("padding\n" * 60, encoding="utf-8")
    assert slog.stat().st_size > 200
    _event(account="u" + "x" * 60, now=time.time())
    assert security_log.flush() == 1
    rows = [json.loads(ln) for ln in _lines(slog)]
    assert len(rows) == 1, "压缩后应等于内存里的事件数"
    assert security_log._FILE_LINES == 1
    assert "padding" not in slog.read_text(encoding="utf-8")
    assert not os.path.exists(str(slog) + ".tmp"), "压缩后不许留下 .tmp"


def test_compaction_drops_events_older_than_30_days(slog, monkeypatch):
    now = time.time()
    stale = {"id": 1, "ts": now - 31 * 86400, "type": "auth.fail",
             "severity": "warn", "source": "203.0.113.0/24", "source_fp": "abcdef01",
             "account": "stale", "route": "/v1/auth/login", "status": 401,
             "count": 1, "detail": "login.failed"}
    fresh = dict(stale, id=2, ts=now - 30, account="fresh")
    slog.write_text(json.dumps(stale) + "\n" + json.dumps(fresh) + "\n",
                    encoding="utf-8")
    security_log._LOADED = False
    monkeypatch.setattr(security_log, "MAX_FILE_LINES", 1)
    monkeypatch.setattr(security_log, "MAX_FILE_BYTES", 10 ** 9)
    _event(account="new", now=now)
    security_log.flush()
    text = slog.read_text(encoding="utf-8")
    assert '"account":"stale"' not in text, "压缩没丢 30 天前的事件"
    assert '"account":"fresh"' in text
    assert len(_lines(slog)) == 2


# ---------- 热路径零文件 I/O ----------

def test_record_never_touches_the_filesystem(monkeypatch):
    opened = []
    real_open = builtins.open

    def guarded(*args, **kwargs):
        opened.append(args)
        raise AssertionError("record() 在请求热路径上打开了文件")

    monkeypatch.setattr(builtins, "open", guarded)
    try:
        event = _event()
    finally:
        monkeypatch.setattr(builtins, "open", real_open)
    assert event is not None
    assert len(security_log._PENDING) == 1, "事件没有进 pending"
    assert opened == []


def test_record_swallows_its_own_exceptions(monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise RuntimeError("redaction exploded")

    monkeypatch.setattr(security_log, "redact_ip", boom)
    assert _event() is None
    err = capsys.readouterr().err
    assert "安全日志在漏" in err, "异常必须留下可看见的 stderr 一行"


# ---------- 读取面 ----------

def test_events_are_newest_first_and_next_before_is_the_cursor():
    for i in range(3):
        _event(account=f"u{i}", now=1000.0 + i)
    first, cursor = security_log.events(limit=2)
    assert [item["id"] for item in first] == [3, 2]
    assert cursor == 2
    rest, cursor2 = security_log.events(before=cursor, limit=2)
    assert [item["id"] for item in rest] == [1]
    assert cursor2 is None
    assert first[0]["ts"].endswith("+00:00"), "ts 必须是带 +00:00 的 ISO 8601"


def test_snapshot_filters_by_the_window():
    _event(account="old", now=time.time() - 7200)
    _event(account="new", now=time.time())
    assert [item["account"] for item in security_log.snapshot(3600)] == ["new"]
    assert len(security_log.snapshot(86400)) == 2
