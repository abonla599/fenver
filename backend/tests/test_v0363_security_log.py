"""v0.36.3 安全日志的行为锁：脱敏、合并、有界、懒加载、压缩、热路径零 I/O。

这些用例只碰 `tmp_path` 下的文件（conftest 已把 SECURITY_LOG_PATH 指进测试临时根，
这里再各指各的，双保险）。每条用例自带 reset，不把进程内的事件环带出用例。
"""
import builtins
import datetime
import hashlib
import json
import os
import re
import time
from types import SimpleNamespace

import pytest

from app.core import audit, security_log


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


# ---------- 时间线：固定桶、零桶、夹边界 ----------

@pytest.mark.parametrize("window, bucket_seconds, bucket_count", [
    (3600, 300, 12),
    (24 * 3600, 3600, 24),
    (7 * 24 * 3600, 86400, 7),
])
def test_summary_timeline_has_fixed_zero_filled_buckets(window, bucket_seconds,
                                                         bucket_count):
    now = 1_700_000_000.0
    # 窗口起点那一刻 → 首桶（边界算在窗口内）。
    security_log.record("auth.fail", ip="203.0.113.77", account="edge-start",
                        route="/v1/auth/login", status=401, detail="login.failed",
                        now=now - window)
    # 晚于 now 的事件 → 夹到末桶，不能凭空多出一个桶。
    security_log.record("auth.fail", ip="203.0.113.77", account="edge-end",
                        route="/v1/auth/login", status=401, detail="login.failed",
                        now=now + 120)
    # 高危事件落在末桶（window 内）。
    security_log.record("system.anomaly", ip="198.51.100.9", account="boom",
                        route="/v1/admin/users", status=500, detail="unspecified",
                        now=now - 30)

    data = security_log.summary(window, now=now)
    timeline = data["timeline"]
    assert data["bucket_seconds"] == bucket_seconds
    assert len(timeline) == bucket_count, "桶数不是固定值"
    assert sum(bucket["count"] for bucket in timeline) == data["totals"]["events"]
    assert sum(bucket["alert"] for bucket in timeline) == data["totals"]["alert"]

    starts = [datetime.datetime.fromisoformat(bucket["start"]) for bucket in timeline]
    assert all(start.tzinfo is not None for start in starts), "start 不是带时区的 ISO 8601"
    assert starts == sorted(starts), "桶起点必须按时间递增"
    for earlier, later in zip(starts, starts[1:]):
        assert (later - earlier).total_seconds() == bucket_seconds

    assert any(bucket["count"] == 0 for bucket in timeline), "零桶没有输出"
    assert timeline[0]["count"] == 1, "窗口起点的事件没有落进首桶"
    assert timeline[-1]["count"] >= 2, "晚于 now 的事件没有夹进末桶"
    assert timeline[-1]["alert"] == 1


def test_summary_unknown_window_uses_hourly_buckets():
    """窗口只认三档；其它值仍要有时间线，桶宽按 3600 兜底。"""
    data = security_log.summary(2 * 3600, now=1_700_000_000.0)
    assert data["bucket_seconds"] == 3600
    assert sum(bucket["count"] for bucket in data["timeline"]) == data["totals"]["events"]


# ---------- 级别过滤 ----------

def test_events_filter_by_severity_only_returns_that_level():
    security_log.record("auth.fail", ip="203.0.113.77", account="warn-user",
                        route="/v1/auth/login", status=401, detail="login.failed",
                        now=1000.0)
    security_log.record("system.anomaly", ip="198.51.100.9", account="alert-user",
                        route="/v1/admin/users", status=500, detail="unspecified",
                        now=1001.0)
    security_log.record("admin.action", ip="203.0.113.77", account="info-user",
                        route="/v1/admin/users", status=200, detail="unspecified",
                        now=1002.0)

    for severity, account in (("alert", "alert-user"), ("warn", "warn-user"),
                              ("info", "info-user")):
        items, _ = security_log.events(severity=severity)
        assert [item["account"] for item in items] == [account], severity
        assert all(item["severity"] == severity for item in items)

    everything, _ = security_log.events()
    assert len(everything) == 3, "None 不该过滤任何级别"


# ---------- 账号栏：凭据形状不进盘 ----------
#
# account 来自登录/注册请求体，攻击者可以把 `password=…` 或 `sk-…` 直接填进用户名。
# 判据写在「盘上查不到那串字节」这一层，不写在「清洗函数返回什么」那一层。

def test_account_credential_shapes_are_scrubbed_before_disk(slog):
    password = "Zz9" + "accountpw"
    key = "sk" + "-" + "C" * 24
    _event(account="password=" + password)
    _event(account=key)
    assert security_log.flush() == 2
    raw = slog.read_text(encoding="utf-8")
    assert password not in raw, "账号里的口令值原样落盘"
    assert key not in raw, "账号里的 sk- 密钥原样落盘"
    assert "password=***" in raw, "口令标签被一起吃掉了"
    assert '"account":"***"' in raw, "sk- 形状没有整体隐去"


def test_ordinary_accounts_survive_cleaning():
    for name in ("张三", "alice", "secretary"):
        assert _event(account=name)["account"] == name


def test_account_scrubbing_runs_before_the_length_cap():
    secret = "SECRET" + "123456"
    event = _event(account="x" * 50 + " password=" + secret)
    assert event["account"] == "x" * 50 + " password=***"
    assert secret[:4] not in event["account"], "截断把凭据值的片段留了下来"
    assert len(event["account"]) <= security_log.MAX_ACCOUNT_CHARS


def test_account_scrubbing_actually_has_teeth(tmp_path, monkeypatch):
    """负控：把 audit._scrub_text 换成恒等函数，同一条「盘上查不到」必须变红。"""
    password = "Zz9" + "accountpw"

    def check(path):
        security_log.reset_for_tests(path=str(path), autoflush=False)
        security_log.record("auth.fail", ip="203.0.113.77",
                            account="password=" + password,
                            route="/v1/auth/login", status=401,
                            detail="login.failed")
        assert security_log.flush() == 1
        raw = path.read_text(encoding="utf-8")
        assert password not in raw, "安全日志账号里的口令值原样落盘"

    check(tmp_path / "green.jsonl")           # 正常实现：红不了
    real = audit._scrub_text
    monkeypatch.setattr(audit, "_scrub_text", lambda s: s)
    with pytest.raises(AssertionError, match="安全日志账号里的口令值原样落盘"):
        check(tmp_path / "red.jsonl")         # 脱敏被拆：必须红
    monkeypatch.setattr(audit, "_scrub_text", real)
    check(tmp_path / "green-again.jsonl")     # 还原：复绿


# ---------- 环内占用：拒绝类事件最多 MAX_REJECTION_IN_RING 条 ----------
#
# 401 不占 120/min 的 abuse 预算（那会压掉真实暴力破解证据），但未鉴权、可轮换
# 账号/来源，不设环内上限同样能把 system.anomaly 等非拒绝事件挤出去。下面的
# 场景一律用唯一账号 + 唯一 /24，避免合并与匿名埋点预算干扰判据。

def _record_rejections(start, count, now=1000.0):
    ids = []
    for i in range(start, start + count):
        event = security_log.record(
            "auth.fail", ip=f"10.{(i // 256) % 200}.{i % 256}.1",
            account=f"u{i}", route="/v1/auth/login", status=401,
            detail="login.failed", now=now)
        assert event is not None
        ids.append(event["id"])
    return ids


def _record_anomalies(count, now=1000.0):
    ids = []
    for i in range(count):
        event = security_log.record(
            "system.anomaly", ip=f"172.16.{(i // 256) % 200}.{i % 256}",
            account=f"a{i}", route="/v1/admin/users", status=500,
            detail="unspecified", now=now)
        assert event is not None
        ids.append(event["id"])
    return ids


def _assert_rejection_ring_invariants(cap, anomaly_ids, oldest_id):
    rows = list(security_log._EVENTS)
    ring_ids = {event["id"] for event in rows}
    assert set(anomaly_ids) <= ring_ids, "非拒绝事件被拒绝类洪水挤出了内存环"
    rejections = [event for event in rows
                  if security_log._is_rejection_record(event["type"],
                                                       event["status"])]
    assert len(rejections) <= cap, (
        f"环内拒绝类事件 {len(rejections)} 条，超过上限 {cap}")
    assert len(rows) <= security_log.MAX_EVENTS
    assert oldest_id not in ring_ids, "最旧的拒绝类事件没有被淘汰"


def test_rejection_records_cap_their_share_and_spare_non_rejection_events():
    cap = security_log.MAX_REJECTION_IN_RING
    anomalies = security_log.MAX_EVENTS - cap
    oldest_id = _record_rejections(0, cap)[0]
    anomaly_ids = _record_anomalies(anomalies)
    _record_rejections(cap, 500)

    _assert_rejection_ring_invariants(cap, anomaly_ids, oldest_id)
    newest = security_log._EVENTS[-1]
    assert newest["type"] == "auth.fail" and newest["account"] == f"u{cap + 499}", \
        "最新写入的 401 应当仍在环里"

    # 只多写 500 条时，旧的 FIFO 恰好也只淘汰最老的 401；再灌满一轮才能证明
    # 上限真的在拦，而不是队尾恰好都是拒绝类。
    _record_rejections(cap + 500, cap)
    _assert_rejection_ring_invariants(cap, anomaly_ids, oldest_id)


def test_rejections_below_the_ring_cap_are_all_kept():
    ids = _record_rejections(0, 100)
    assert {event["id"] for event in security_log._EVENTS} == set(ids), \
        "低于环内上限时拒绝类事件被提前丢弃"


def test_evicted_rejections_leave_pending_before_flush(slog):
    cap = security_log.MAX_REJECTION_IN_RING
    anomalies = security_log.MAX_EVENTS - cap
    oldest_id = _record_rejections(0, cap)[0]
    anomaly_ids = _record_anomalies(anomalies)
    _record_rejections(cap, 500)

    pending_ids = {event["id"] for event in security_log._PENDING}
    assert security_log._PENDING_IDS == pending_ids, "pending 的 id 簿记与队列不一致"
    assert oldest_id not in security_log._PENDING_IDS, "被淘汰的 id 仍留在 pending 簿记里"

    assert security_log.flush() == security_log.MAX_EVENTS
    rows = [json.loads(line) for line in _lines(slog)]
    assert len(rows) <= security_log.MAX_EVENTS
    assert set(anomaly_ids) <= {row["id"] for row in rows}
    assert oldest_id not in {row["id"] for row in rows}, \
        "已淘汰且未落盘的拒绝事件仍被写进文件"
    assert security_log._PENDING_IDS == set()


def test_the_rejection_ring_cap_actually_has_teeth(monkeypatch):
    """负控：跳过「淘汰最旧拒绝类」这一支，核心场景必须红。"""
    monkeypatch.setattr(security_log, "_is_rejection_record", lambda *_: False)
    cap = security_log.MAX_REJECTION_IN_RING
    anomalies = security_log.MAX_EVENTS - cap
    oldest_id = _record_rejections(0, cap)[0]
    anomaly_ids = _record_anomalies(anomalies)
    _record_rejections(cap, 500)
    _record_rejections(cap + 500, cap)
    with pytest.raises(AssertionError, match="非拒绝事件被拒绝类洪水挤出了内存环"):
        _assert_rejection_ring_invariants(cap, anomaly_ids, oldest_id)


def test_a_full_ring_of_non_rejection_events_drops_new_rejections(slog):
    """环里已经全是非拒绝事件时，401 洪水不得按 FIFO 把它们挤掉。

    旧实现只在「拒绝类数触顶」时淘汰，前 1200 条 401 会走 deque 自动 FIFO，
    把 2000 条 anomaly 挤掉 1200 条——这条锁在旧实现下必须红。
    """
    anomaly_ids = _record_anomalies(security_log.MAX_EVENTS)
    dropped = 0
    for i in range(security_log.MAX_REJECTION_IN_RING):
        event = security_log.record(
            "auth.fail", ip=f"10.{(i // 256) % 200}.{i % 256}.1",
            account=f"drop{i}", route="/v1/auth/login", status=401,
            detail="login.failed", now=1001.0)
        dropped += event is None

    rows = list(security_log._EVENTS)
    assert len(rows) == security_log.MAX_EVENTS == 2000, "环总数不是 2000"
    assert {event["id"] for event in rows} == set(anomaly_ids), \
        "非拒绝事件被拒绝类洪水挤出了内存环"
    assert not [event for event in rows
                if security_log._is_rejection_record(event["type"], event["status"])], \
        "环满且全是非拒绝事件时不应再收拒绝类事件"
    assert dropped == security_log.MAX_REJECTION_IN_RING, "新拒绝事件必须整条丢弃"

    assert security_log.flush() == security_log.MAX_EVENTS
    disk = [json.loads(line) for line in _lines(slog)]
    assert len(disk) == security_log.MAX_EVENTS, "flush 后磁盘行数不等于环里的 2000 条"
    assert {row["id"] for row in disk} == set(anomaly_ids), \
        "flush 后磁盘上只剩部分非拒绝事件"


def test_eviction_keeps_pending_and_merge_books_in_step(slog, monkeypatch):
    """淘汰拒绝类时 pending 与 _MERGE 必须同步摘除。

    1200 + 700 + 500 = 1900 条，低于 MAX_PENDING=2000 与 MAX_FILE_LINES=2000；
    这里再把压缩阈值抬走，让簿记错误直接落在行数上，而不是被 pending 上限或
    flush 后的压缩掩盖。
    """
    monkeypatch.setattr(security_log, "MAX_FILE_LINES", 10 ** 9)
    monkeypatch.setattr(security_log, "MAX_FILE_BYTES", 10 ** 9)
    cap = security_log.MAX_REJECTION_IN_RING
    anomaly_count = 700
    _record_rejections(0, cap)
    anomaly_ids = _record_anomalies(anomaly_count)
    _record_rejections(cap, 500)

    assert len(security_log._EVENTS) == 1900
    assert {event["id"] for event in security_log._EVENTS} >= set(anomaly_ids)

    pending_ids = {event["id"] for event in security_log._PENDING}
    assert security_log._PENDING_IDS == pending_ids, "pending 的 id 簿记与队列不一致"
    merge_ids = {event["id"] for event in security_log._MERGE.values()}
    assert merge_ids <= security_log._PENDING_IDS, "_MERGE 指向了非 pending 的事件"

    assert security_log.flush() == 1900, "flush 写出的条数不等于环内事件数"
    assert len(_lines(slog)) == 1900, "被淘汰的拒绝事件仍被写进文件"
    assert security_log._PENDING_IDS == set()
    assert all(event["id"] in security_log._PENDING_IDS
               for event in security_log._MERGE.values()), "_MERGE 留下悬空条目"


def test_rejection_definition_covers_every_auth_failure_but_not_other_types():
    """宽口径锁：只留 auth.fail 或只留 abuse 都会漏掉另一半拒绝事件。"""
    for event_type, status in (("auth.blocked", 0), ("auth.blocked", 403),
                               ("auth.rate_limited", 0), ("auth.rate_limited", 429),
                               ("auth.fail", 401), ("auth.fail", 409),
                               ("auth.fail", 422)):
        assert security_log._is_rejection_record(event_type, status) is True, \
            (event_type, status)
    for event_type in ("system.anomaly", "admin.action"):
        assert security_log._is_rejection_record(event_type, 0) is False, event_type
