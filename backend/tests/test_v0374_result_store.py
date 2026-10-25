"""v0.37.4 T2：搜索结果句柄（`app/tools/result_store.py`）的合同测试。

判据一句话：这枚 `r_...` 是**有界、短期、绑定 owner 的"结果句柄"**，不是正文缓存。
- 只给 http/https + 有主机名 + URL ≤2048 的行签发；其它 scheme/空主机/超长行返回 None；
- 映射里只有 url/title/owner/created_at 四格，没有正文、snippet、query；
- TTL 600 s、全局 512、每 owner 64，超出按插入顺序淘汰最旧；
- owner 不匹配只回原因、不回内容（跨用户重放门）；
- 并发下不重号、不越界、不抛异常。

最后一条是缺牙负控：在用例里做一次"删掉 owner 比较"的内存变异，证明跨用户用例
真的会因此变红。没有这一步，那条锁与一个永远绿的假绿灯没有区别。
"""
import json
import re
import threading

import pytest

from app.tools import result_store


@pytest.fixture(autouse=True)
def _clean_store():
    """进程内全局表：每条用例前后各清一次，断言只看得见自己写进去的东西。"""
    result_store.clear()
    yield
    result_store.clear()


def _row(url="https://example.com/a", title="标题", snippet="摘要"):
    return {"title": title, "url": url, "snippet": snippet}


def _remember(url="https://example.com/a", owner="u", title="标题"):
    ids = result_store.remember([_row(url=url, title=title)], owner=owner)
    assert ids[0] is not None
    return ids[0]


# ---------- ID 形状与唯一性 ----------

def test_the_issued_id_is_r_plus_sixteen_base64url_chars():
    rid = _remember()
    assert re.fullmatch(r"r_[A-Za-z0-9_-]{16}", rid), rid
    assert result_store.RESULT_ID_RE.match(rid), rid
    assert len(rid) == 18, "r_ + 16 字符：长度漂了说明 token 的字节数被改过"


def test_ten_thousand_issuances_never_repeat():
    rows = [_row(url=f"https://example.com/{i}") for i in range(10000)]
    ids = result_store.remember(rows, owner="bulk")
    assert len(ids) == 10000
    assert all(rid and result_store.RESULT_ID_RE.match(rid) for rid in ids)
    assert len(set(ids)) == 10000, "句柄重号了：两枚 ID 会互相覆盖/指到同一条"


# ---------- TTL ----------

def test_ttl_is_six_hundred_seconds(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(result_store, "_now", lambda: clock["t"])
    rid = _remember(owner="A")

    clock["t"] += 599
    record, reason = result_store.lookup(rid, owner="A")
    assert reason == "ok" and record["url"] == "https://example.com/a"

    clock["t"] += 2                     # 601 s
    record, reason = result_store.lookup(rid, owner="A")
    assert record is None and reason == "expired"
    assert rid not in result_store._store, "过期条目没被就地清掉：表会一直长"


def test_remember_sweeps_entries_that_already_expired(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(result_store, "_now", lambda: clock["t"])
    stale = _remember(owner="A")

    clock["t"] += 601
    fresh = _remember(url="https://example.com/b", owner="B")
    assert stale not in result_store._store, "签新条目时没有顺带清理过期的旧条目"
    assert fresh in result_store._store


# ---------- 容量：全局 512、每 owner 64 ----------

def test_the_513th_entry_evicts_the_oldest_globally():
    # 每条一个 owner：把 per-owner 上限排除在外，测的才是全局 512。
    ids = [result_store.remember([_row(url=f"https://example.com/{i}")],
                                 owner=f"u{i}")[0] for i in range(513)]
    assert len(result_store._store) == result_store.MAX_TOTAL == 512
    assert result_store.lookup(ids[0], owner="u0")[1] == "unknown", "最旧的没有被淘汰"
    assert result_store.lookup(ids[1], owner="u1")[1] == "ok"
    assert result_store.lookup(ids[-1], owner="u512")[1] == "ok", "新条目录不进来"


def test_the_65th_entry_only_evicts_that_owners_oldest():
    a_ids = [result_store.remember([_row(url=f"https://a.example/{i}")], owner="A")[0]
             for i in range(65)]
    b_id = _remember(url="https://b.example/1", owner="B")

    assert len([r for r in result_store._store.values() if r["owner"] == "A"]) == 64
    assert result_store.lookup(a_ids[0], owner="A")[1] == "unknown", "A 的最旧条目没被淘汰"
    assert result_store.lookup(a_ids[-1], owner="A")[1] == "ok"
    assert result_store.lookup(b_id, owner="B")[1] == "ok", "B 的条目被 A 的容量连坐了"


# ---------- owner 绑定 ----------

def test_owner_mismatch_returns_no_record_content():
    rid = _remember(url="https://secret.example/a", owner="A", title="A 的标题")

    record, reason = result_store.lookup(rid, owner="B")
    assert record is None and reason == "owner_mismatch"
    # 不泄露：B 拿不到 URL/标题，只能拿到原因
    assert result_store.lookup(rid, owner="")[1] == "owner_mismatch"

    record, reason = result_store.lookup(rid, owner="A")
    assert reason == "ok" and record["url"] == "https://secret.example/a"


def test_an_anonymous_direct_call_can_read_its_own_entries():
    rid = _remember(owner="")
    record, reason = result_store.lookup(rid, owner="")
    assert reason == "ok" and record["owner"] == ""


def test_clear_invalidates_everything():
    ids = [result_store.remember([_row(url=f"https://e.example/{i}")], owner="u")[0]
           for i in range(3)]
    result_store.clear()
    assert result_store._store == {}
    for rid in ids:
        assert result_store.lookup(rid, owner="u")[1] == "unknown"


# ---------- 并发 ----------

def test_twenty_threads_remember_and_lookup_without_duplicates_or_unbounded_growth():
    errors, seen_ids = [], []
    ids_lock = threading.Lock()

    def worker(n):
        try:
            owner = f"thread-{n}"
            for i in range(100):
                rid = result_store.remember(
                    [_row(url=f"https://e.example/{n}/{i}", title=f"t{n}-{i}")],
                    owner=owner)[0]
                record, reason = result_store.lookup(rid, owner)
                # 全局 512 在上限附近时，刚签的条目理论上可能已被别的线程挤出；
                # 但"别人的/过期的"两种原因在这里都不该出现。
                assert reason in ("ok", "unknown"), (rid, reason)
                if reason == "ok":
                    assert record["owner"] == owner
                with ids_lock:
                    seen_ids.append(rid)
        except BaseException as e:      # 线程里的断言也要带出来，不能静默吞掉
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    assert len(seen_ids) == 2000 and len(set(seen_ids)) == 2000, "并发下出现了重号"
    assert len(result_store._store) <= result_store.MAX_TOTAL
    per_owner = {}
    for record in result_store._store.values():
        per_owner[record["owner"]] = per_owner.get(record["owner"], 0) + 1
    assert all(count <= result_store.MAX_PER_OWNER for count in per_owner.values()), per_owner


# ---------- 只存四格：不是正文缓存 ----------

def test_the_map_carries_only_url_title_owner_and_created_at():
    result_store.remember(
        [_row(url="https://e.example/a", title="标题", snippet="摘要不该被存")],
        owner="A")
    dumped = json.loads(json.dumps(result_store._store, ensure_ascii=False))
    assert len(dumped) == 1
    record = next(iter(dumped.values()))
    assert set(record) == {"url", "title", "owner", "created_at"}, sorted(record)
    raw = json.dumps(dumped, ensure_ascii=False)
    assert "摘要不该被存" not in raw, "snippet 被存进了映射：这就变正文缓存了"
    assert "正文" not in raw
    assert "query" not in raw


# ---------- 签发条件 ----------

def test_only_http_and_https_with_a_host_and_a_bounded_url_get_an_id():
    overlong = "https://e.example/" + "a" * result_store.MAX_URL_CHARS
    ids = result_store.remember([
        _row(url="https://e.example/ok"),
        _row(url="http://e.example/plain"),
        _row(url=""),
        _row(url="https://"),
        _row(url=overlong),
        _row(url="ftp://e.example/file"),
    ], owner="u")
    assert ids[0] is not None and result_store.RESULT_ID_RE.match(ids[0])
    assert ids[1] is not None, "http 行也应签发：读取侧允许 http/443 之外的 80"
    assert ids[2:] == [None] * 4, "不满足 http(s)/host/长度的行不该拿到 ID"
    assert result_store.lookup(ids[0], owner="u")[1] == "ok"
    assert result_store.lookup(ids[1], owner="u")[1] == "ok"
    assert len(result_store._store) == 2, "不可签的行不该在表里留下任何东西"


def test_the_stored_title_is_capped_at_120_chars():
    rid = result_store.remember([_row(title="长" * 5000)], owner="u")[0]
    record, reason = result_store.lookup(rid, owner="u")
    assert reason == "ok"
    assert len(record["title"]) == result_store.MAX_TITLE_CHARS
    assert record["title"].endswith("…")


def test_an_unknown_or_forged_id_is_unknown_not_an_error():
    assert result_store.lookup("r_" + "A" * 16, owner="A") == (None, "unknown")
    assert result_store.lookup("https://e.example/", owner="A") == (None, "unknown")


# ---------- 缺牙负控：删掉 owner 比较，跨用户用例必须红 ----------

def test_negative_control_removing_the_owner_check_leaks_the_other_users_result(monkeypatch):
    """内存变异：把 lookup 里那行 owner 比较删掉，`test_owner_mismatch_...` 就会红。

    共享工作树只跑测试、不落盘变异；这里把"删掉比较"的那份 lookup 克隆一遍，
    先证真身判 owner_mismatch，再证变异版让 B 读到 A 的 URL——也就是那条跨用户
    用例在变异下会红在 `assert record is None and reason == "owner_mismatch"`。
    """
    rid = _remember(url="https://secret.example/a", owner="A", title="A 的标题")
    assert result_store.lookup(rid, owner="B") == (None, "owner_mismatch")

    def lookup_without_owner_check(result_id, owner=""):
        record = result_store._store.get(str(result_id or "").strip())
        if record is None:
            return None, "unknown"
        return dict(record), "ok"

    monkeypatch.setattr(result_store, "lookup", lookup_without_owner_check)
    record, reason = result_store.lookup(rid, owner="B")
    assert reason == "ok" and record["url"] == "https://secret.example/a", (
        "删掉 owner 比较后 B 竟没读到 A 的结果——那条跨用户锁是假的")
