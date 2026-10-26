"""有界、脱敏的安全事件日志（管理台「安全监控」的数据源）。

三条硬约束：

1. **record() 只做内存操作**。鉴权中间件是 `async` 热路径，本项目禁止在事件循环里
   做同步文件 I/O（判据见 tests/test_event_loop_not_blocked.py）。清洗、合并、入队
   全部在锁内完成；落盘交给后台守护线程每 5 秒批量追加一次，进程退出时 atexit 再
   补一次。写失败只把 pending 留在内存并在 stderr 说一声，绝不连累请求。
2. **原始 IP 永不落盘、永不出现在 API**。只存网络前缀（IPv4 /24、IPv6 /48）与原始
   地址的 sha256 前 8 位；账号去控制字符并截断，路由只取 path 并过白名单正则，
   detail/type 只认固定枚举。
3. **有界**。内存只留最新 2000 条事件，合并键表上限 4096，pending 上限 2000；文件
   超过 2MB 或 2000 行时在 flush 线程里压缩成「内存中的最新 2000 条」（30 天前的
   事件一并丢弃），先写 `<path>.tmp` 再 `os.replace`。

**合并只发生在尚未落盘的同键事件上**（键 = type/source/source_fp/account/route/
status/detail，窗口 60 秒）。已落盘的事件再合并就得重写整份文件，那会把追加写从
O(1) 换成 O(n)，也会让"只增的账"变得可改——所以 flush 之后同键的新事件一定是
新的一行。这条取舍是有意的，不是漏了。

本批接入 `auth.fail` / `auth.rate_limited` / `auth.blocked` 三类。`admin.action` 由
audit.jsonl 负责，不在这里重复记；`tool.blocked` / `approval.denied` /
`provider.error` 本批没有接入点，枚举先固定住（与前端契约一致），等 v0.37 接入。
"""
import atexit
import datetime
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
from collections import OrderedDict, deque

from app.core import audit
from app.core.client_ip import client_ip, redact_ip
from app.core.paths import data_file, ensure_parent

# 与前端契约逐字一致的固定枚举，不得增删。
TYPES = frozenset({"auth.fail", "auth.rate_limited", "auth.blocked", "admin.action",
                   "tool.blocked", "approval.denied", "provider.error",
                   "system.anomaly"})
SEVERITY_BY_TYPE = {"auth.fail": "warn", "auth.rate_limited": "warn",
                    "auth.blocked": "warn", "admin.action": "info",
                    "tool.blocked": "alert", "approval.denied": "info",
                    "provider.error": "warn", "system.anomaly": "alert"}
DETAILS = frozenset({
    "login.failed", "register.rejected", "reset.rejected", "reauth.denied",
    "limit.login", "limit.register", "limit.reset", "limit.reauth", "limit.chat",
    "limit.orchestrate", "token.unknown", "csrf.missing", "unspecified",
})

MAX_EVENTS = 2000
MAX_PENDING = 2000
MERGE_KEY_LIMIT = 4096
MERGE_WINDOW_SECONDS = 60.0
FLUSH_INTERVAL_SECONDS = 5.0
MAX_AGE_SECONDS = 30 * 24 * 3600

# 账号名先截到有界前缀再跑 category()：公开端点会把用户名原样送到这里，2 MB 的
# 串逐字符分类就是一条未鉴权的 CPU 放大路径。64 是最终落盘上限，256 给控制字符
# 留出足够的清洗余量，两者都不是"用户名合法长度"（那是 auth.USERNAME_MAX 的事）。
MAX_ACCOUNT_CHARS = 64
ACCOUNT_SCAN_LIMIT = 256

# 匿名拒绝/限流埋点的限额：攻击者能用不同路径或不同用户名刷满 2000 条内存环，
# 把正常事件挤出去。三条上限分别管"全局速率""单来源速率""环内占比"，任一触顶就
# 丢弃这条埋点本身，不丢别的账；环满时先保证非这一类事件有位置。
# 除下面两类外，注册/找回在参数形状不合格时写的 `auth.fail(status=422)` 也算：
# 那是未鉴权、可轮换用户名/路径的匿名埋点，不占预算就能挤掉真实登录失败。
ABUSE_TYPES = frozenset({"auth.blocked", "auth.rate_limited"})
ABUSE_WINDOW_SECONDS = 60.0
MAX_ABUSE_EVENTS = 120
MAX_ABUSE_EVENTS_PER_SOURCE = 12
MAX_ABUSE_IN_EVENTS = 500
# 环内占比只数最近 60 秒的 abuse 事件：把"环里已经有 500 条"当永久状态，会让
# 一次 422 洪水在之后永远压掉同类 auth.blocked（窗口内 500 条才是真占比）。
ABUSE_RING_WINDOW_SECONDS = 60.0
_ABUSE_WINDOW = [0.0, 0]
_ABUSE_SOURCES = {}

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILE_LINES = 2000
AUDIT_LARGE_BYTES = 5 * 1024 * 1024          # summary 里 audit_file_large 的阈值

# 管理台时间线的固定桶：窗口 → (桶宽秒数, 桶数)。窗口与三档一一对应，
# 桶宽 × 桶数 == 窗口；其它窗口值沿用 1 小时口径（24 个 1 小时桶）。
_TIMELINE_SPEC = {3600: (300, 12), 86400: (3600, 24), 7 * 24 * 3600: (86400, 7)}
_DEFAULT_BUCKET_SECONDS = 3600
_DEFAULT_BUCKET_COUNT = 24

_ROUTE_RE = re.compile(r"^/[A-Za-z0-9_./{}-]{0,120}$")

_LOCK = threading.Lock()
_EVENTS = deque(maxlen=MAX_EVENTS)           # 内存中的最新事件（读取面只认它）
_PENDING = []                                # 尚未成功落盘的事件（与 _EVENTS 同序）
_PENDING_IDS = set()
_MERGE = OrderedDict()                       # 合并键 → 尚未落盘的最后一条同键事件
_NEXT_ID = 1
_LOADED = False
_FILE_LINES = 0
_PATH_OVERRIDE = None
_AUTOFLUSH = True
_THREAD = None


def default_path() -> str:
    """落点：$SECURITY_LOG_PATH > <项目根>/data/security_log.jsonl。不缓存。"""
    return data_file("SECURITY_LOG_PATH", "security_log.jsonl")


def _current_path() -> str:
    return _PATH_OVERRIDE or default_path()


def _warn(message: str) -> None:
    """stderr 是唯一的告警面：日志本身坏了，不能再借它报信。"""
    try:
        print(f"[security_log] 安全日志在漏：{message}", file=sys.stderr)
    except Exception:                        # noqa: BLE001 - stderr 都没了只能沉默
        pass


def _clean_account(value) -> str:
    """去控制字符（Cc/Cf：含 NUL、零宽连接符）、strip、截断 64；空 → anonymous。

    只扫有界前缀：2 MB 用户名不该在这里被逐字符分类（公开端点可并发放大）。
    """
    raw = str(value or "")[:ACCOUNT_SCAN_LIMIT]
    text = "".join(ch for ch in raw
                   if unicodedata.category(ch) not in ("Cc", "Cf"))
    text = text.strip()[:MAX_ACCOUNT_CHARS]
    return text or "anonymous"


def _is_abuse_record(event_type: str, status: int) -> bool:
    """哪些事件占用匿名埋点预算（环内计数与速率预算共用的唯一判据）。

    覆盖 `auth.blocked`、`auth.rate_limited`，以及注册/找回在参数形状不合格时写的
    `auth.fail(status=422)`。正常登录失败（401/409）由路由层各自的 `_note_failure`
    计费，不在这里重复收费。
    """
    return event_type in ABUSE_TYPES or (event_type == "auth.fail" and status == 422)


def _abuse_budget_locked(source: str, now: float) -> bool:
    """匿名埋点预算；调用方已持有 _LOCK。

    覆盖 `auth.blocked`、`auth.rate_limited` 与 `auth.fail(status=422)`——这是
    "匿名形状错误/拒绝"的记账额度，不是登录失败计费：401/409 的 `auth.fail` 走
    路由层自己的失败限流，不经这里。
    """
    start, count = _ABUSE_WINDOW
    if now - start >= ABUSE_WINDOW_SECONDS:
        _ABUSE_WINDOW[0], _ABUSE_WINDOW[1] = now, 0
        _ABUSE_SOURCES.clear()
    if _ABUSE_WINDOW[1] >= MAX_ABUSE_EVENTS:
        return False
    entry = _ABUSE_SOURCES.get(source)
    if entry is None or now - entry[0] >= ABUSE_WINDOW_SECONDS:
        entry = [now, 0]
        _ABUSE_SOURCES[source] = entry
    if entry[1] >= MAX_ABUSE_EVENTS_PER_SOURCE:
        return False
    _ABUSE_WINDOW[1] += 1
    entry[1] += 1
    if len(_ABUSE_SOURCES) > 4096:
        # 来源前缀本身已被限到 /24、/48；这里再兜一层内存上限。
        _ABUSE_SOURCES.clear()
    return True


def _clean_route(value) -> str:
    """只接受 path 形状；query string 永远不会走到这里（调用方只传 url.path）。"""
    if value is None:
        return ""
    text = str(value)
    return text if _ROUTE_RE.fullmatch(text) else "<invalid>"


def _clean_status(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _key_of(event: dict) -> tuple:
    return (event["type"], event["source"], event["source_fp"], event["account"],
            event["route"], event["status"], event["detail"])


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(float(ts),
                                           tz=datetime.timezone.utc).isoformat()


def _ensure_thread_locked() -> None:
    """首次 record 时懒启动后台线程；导入期不建线程、不读盘。"""
    global _THREAD
    if not _AUTOFLUSH:
        return
    if _THREAD is not None and _THREAD.is_alive():
        return
    _THREAD = threading.Thread(target=_flush_loop, name="security-log-flush",
                               daemon=True)
    _THREAD.start()


def _flush_loop() -> None:
    while True:
        time.sleep(FLUSH_INTERVAL_SECONDS)
        try:
            if _AUTOFLUSH:
                flush()
        except Exception as e:               # noqa: BLE001 - 线程不许因一次失败死掉
            _warn(f"后台 flush 异常：{e}")


def _coerce_loaded(raw) -> dict:
    """把盘上一行收成内存事件；形状不认的行返回 None（坏行跳过）。"""
    if not isinstance(raw, dict):
        return None
    event_type = raw.get("type")
    if event_type not in TYPES:
        return None
    try:
        event_id = int(raw["id"])
        ts = float(raw["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(ts):
        return None
    try:
        count = int(raw.get("count", 1))
    except (TypeError, ValueError):
        count = 1
    count = max(1, count)
    detail = raw.get("detail")
    return {
        "id": event_id,
        "ts": ts,
        "type": event_type,
        "severity": SEVERITY_BY_TYPE[event_type],
        "source": str(raw.get("source") or "unknown")[:80],
        "source_fp": str(raw.get("source_fp") or "")[:16],
        "account": _clean_account(raw.get("account")),
        "route": _clean_route(raw.get("route")),
        "status": _clean_status(raw.get("status", 0)),
        "count": count,
        "detail": detail if detail in DETAILS else "unspecified",
    }


def _ensure_loaded_locked() -> None:
    """首次使用时读一次文件：坏行跳过、30 天前的事件丢弃、只留最新 2000 条。

    导入期绝不调用它；读失败只报警，内存从空开始（安全日志坏了不能把服务带崩）。
    """
    global _LOADED, _NEXT_ID, _FILE_LINES
    if _LOADED:
        return
    _LOADED = True
    path = _current_path()
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:                   # noqa: BLE001 - 读不出就当下没有
        _warn(f"读取 {path} 失败，从空状态开始：{e}")
        return
    _FILE_LINES = len(lines)
    cutoff = time.time() - MAX_AGE_SECONDS
    max_id = 0
    kept = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            event_id = int(raw["id"])
        except (ValueError, TypeError, KeyError):
            continue                          # 坏行不假装是好行
        max_id = max(max_id, event_id)
        event = _coerce_loaded(raw)
        if event is not None and event["ts"] >= cutoff:
            kept.append(event)
    _EVENTS.extend(kept[-MAX_EVENTS:])
    _NEXT_ID = max(_NEXT_ID, max_id + 1)


def record(event_type, *, request=None, ip=None, account="", route=None, status=0,
           detail="unspecified", now=None):
    """记一条安全事件。只做内存操作，任何异常都吞掉并打 stderr。

    `request` 只用来取 path 与（未显式给 ip 时的）来源地址；绝不读 body/query。
    `now` 仅供测试注入。未知 type 一律忽略（不落盘，也不抛给调用方）；未知 detail
    归一为 "unspecified"。返回落进内存的事件（合并时是同一条），被忽略时返回 None。
    """
    try:
        if event_type not in TYPES:
            return None
        if detail not in DETAILS:
            detail = "unspecified"
        moment = float(now) if now is not None else time.time()
        raw_ip = ip
        if raw_ip is None and request is not None:
            raw_ip = client_ip(request)
        if raw_ip:
            source, source_fp = redact_ip(raw_ip)
        else:
            source, source_fp = "unknown", ""
        clean_account = _clean_account(account)
        path = route
        if path is None and request is not None:
            try:
                path = request.url.path
            except Exception:                # noqa: BLE001 - 取不到就当没给
                path = None
        clean_route = _clean_route(path)
        clean_status = _clean_status(status)

        with _LOCK:
            _ensure_loaded_locked()
            if _is_abuse_record(event_type, clean_status):
                abuse_in_ring = sum(
                    1 for existing in _EVENTS
                    if moment - existing["ts"] < ABUSE_RING_WINDOW_SECONDS
                    and _is_abuse_record(existing["type"], existing["status"]))
                if abuse_in_ring >= MAX_ABUSE_IN_EVENTS:
                    return None
                if not _abuse_budget_locked(source, moment):
                    return None
            key = (event_type, source, source_fp, clean_account, clean_route,
                   clean_status, detail)
            last = _MERGE.get(key)
            if (last is not None and last["id"] in _PENDING_IDS
                    and moment - last["ts"] <= MERGE_WINDOW_SECONDS):
                last["count"] = int(last.get("count") or 0) + 1
                last["ts"] = moment
                _MERGE.move_to_end(key)
                return last

            global _NEXT_ID
            event = {
                "id": _NEXT_ID,
                "ts": moment,
                "type": event_type,
                "severity": SEVERITY_BY_TYPE[event_type],
                "source": source,
                "source_fp": source_fp,
                "account": clean_account,
                "route": clean_route,
                "status": clean_status,
                "count": 1,
                "detail": detail,
            }
            _NEXT_ID += 1
            _EVENTS.append(event)
            _PENDING.append(event)
            _PENDING_IDS.add(event["id"])
            while len(_PENDING) > MAX_PENDING:
                dropped = _PENDING.pop(0)
                _PENDING_IDS.discard(dropped["id"])
            _MERGE[key] = event
            _MERGE.move_to_end(key)
            while len(_MERGE) > MERGE_KEY_LIMIT:
                _MERGE.popitem(last=False)
            _ensure_thread_locked()
            return event
    except Exception as e:                   # noqa: BLE001 - 安全日志绝不连累请求
        _warn(f"record 异常：{e}")
        return None


def flush() -> int:
    """把 pending 追加到文件，返回写出的条数。生产由后台线程与 atexit 调用。

    一次 open("a") 批量写，不 fsync：这份账的价值在"有"，不在"掉电也一字不丢"；
    每 5 秒一次 fsync 反而会把磁盘抖动带进服务进程。写失败保留 pending。
    """
    global _FILE_LINES
    with _LOCK:
        _ensure_loaded_locked()
        if not _PENDING:
            return 0
        path = _current_path()
        payload = "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            for event in _PENDING)
        try:
            ensure_parent(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(payload)
        except Exception as e:               # noqa: BLE001 - 留在内存下次再试
            while len(_PENDING) > MAX_PENDING:
                dropped = _PENDING.pop(0)
                _PENDING_IDS.discard(dropped["id"])
            _warn(f"落盘失败（{len(_PENDING)} 条留在内存，稍后重试）：{e}")
            return 0
        written = len(_PENDING)
        for event in _PENDING:
            _PENDING_IDS.discard(event["id"])
            key = _key_of(event)
            if _MERGE.get(key) is event:
                del _MERGE[key]
        _PENDING.clear()
        _FILE_LINES += written
        _maybe_compress_locked(path)
        return written


def _maybe_compress_locked(path: str) -> None:
    """文件过大/行数过多时，用内存里的最新事件整体重写（tmp + replace）。"""
    global _FILE_LINES
    try:
        too_big = os.path.getsize(path) > MAX_FILE_BYTES
    except OSError:
        too_big = False
    if not too_big and _FILE_LINES <= MAX_FILE_LINES:
        return
    cutoff = time.time() - MAX_AGE_SECONDS
    kept = [dict(event) for event in _EVENTS if event["ts"] >= cutoff]
    tmp = path + ".tmp"
    try:
        ensure_parent(tmp)
        with open(tmp, "w", encoding="utf-8") as f:
            for event in kept:
                f.write(json.dumps(event, ensure_ascii=False,
                                   separators=(",", ":")) + "\n")
        os.replace(tmp, path)
        _FILE_LINES = len(kept)
    except Exception as e:                   # noqa: BLE001 - 保留旧文件，下次再试
        try:
            os.remove(tmp)
        except OSError:
            pass
        _warn(f"压缩失败，保留旧文件：{e}")


def snapshot(window_seconds) -> list:
    """窗口内事件的副本（ts 保持 epoch 秒），按写入顺序。"""
    cutoff = time.time() - float(window_seconds)
    with _LOCK:
        _ensure_loaded_locked()
        return [dict(event) for event in _EVENTS if event["ts"] >= cutoff]


def summary(window_seconds, now=None) -> dict:
    """窗口聚合。`window` 标签由路由补（它才知道用户传的是 1h/24h/7d）。

    同时给出固定桶数的时间线（含零桶）：`bucket_seconds` 是桶宽，`timeline` 的
    `count`/`alert` 合计分别等于 `totals.events`/`totals.alert`。桶边界从 `now`
    向前推一个窗口；`now` 仅供测试注入，生产调用不传。
    """
    moment = float(now) if now is not None else time.time()
    span = float(window_seconds)
    cutoff = moment - span
    with _LOCK:
        _ensure_loaded_locked()
        rows = [dict(event) for event in _EVENTS if event["ts"] >= cutoff]

    totals = {"events": 0, "alert": 0, "warn": 0, "info": 0,
              "sources": 0, "accounts": 0}
    type_counts, source_counts, account_counts = {}, {}, {}
    for event in rows:
        count = int(event.get("count") or 1)
        totals["events"] += count
        severity = event.get("severity")
        if severity in ("alert", "warn", "info"):
            totals[severity] += count
        type_counts[event["type"]] = type_counts.get(event["type"], 0) + count
        source_key = (event["source"], event["source_fp"])
        source_counts[source_key] = source_counts.get(source_key, 0) + count
        account_counts[event["account"]] = account_counts.get(event["account"], 0) + count
    totals["sources"] = len(source_counts)
    totals["accounts"] = len(account_counts)

    def top(mapping):
        ordered = sorted(mapping.items(), key=lambda kv: (-kv[1], str(kv[0])))
        return ordered[:10]

    by_type = [{"type": name, "count": count}
               for name, count in top(type_counts)]
    by_source = [{"source": source, "source_fp": fp, "count": count}
                 for (source, fp), count in top(source_counts)]
    by_account = [{"account": name, "count": count}
                  for name, count in top(account_counts)]

    bucket_seconds, bucket_count = _TIMELINE_SPEC.get(
        int(span), (_DEFAULT_BUCKET_SECONDS, _DEFAULT_BUCKET_COUNT))
    timeline = [{"start": _iso(cutoff + index * bucket_seconds), "count": 0, "alert": 0}
                for index in range(bucket_count)]
    for event in rows:
        index = int((float(event["ts"]) - cutoff) // bucket_seconds)
        index = max(0, min(index, bucket_count - 1))   # 越界事件夹进首/尾桶
        count = int(event.get("count") or 1)
        timeline[index]["count"] += count
        if event.get("severity") == "alert":
            timeline[index]["alert"] += count

    audit_stats = audit.stats()
    warnings = (["audit_file_large"]
                if int(audit_stats.get("size_bytes") or 0) > AUDIT_LARGE_BYTES else [])
    return {
        "generated_at": _iso(moment),
        "totals": totals,
        "bucket_seconds": bucket_seconds,
        "timeline": timeline,
        "by_type": by_type,
        "by_source": by_source,
        "by_account": by_account,
        "audit": audit_stats,
        "warnings": warnings,
    }


def events(before=None, limit=50, type=None, source=None, account=None,
           severity=None):
    """倒序取一页事件，返回 `(items, next_before)`。

    `limit + 1` 只用来判断"还有没有更旧的"：`next_before` 是本页最后一条的 id，
    下一页传回来即可。`ts` 在这里转成 ISO 8601（UTC，带 +00:00），其余字段原样。
    `severity` 只与事件的 severity 字段比较；取值白名单由路由负责，None 不过滤。
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))
    with _LOCK:
        _ensure_loaded_locked()
        rows = sorted(_EVENTS, key=lambda event: int(event["id"]), reverse=True)

    picked = []
    for event in rows:
        if before is not None and int(event["id"]) >= int(before):
            continue
        if type is not None and event["type"] != type:
            continue
        if severity is not None and event["severity"] != severity:
            continue
        if source is not None and event["source"] != source:
            continue
        if account is not None and event["account"] != account:
            continue
        picked.append(dict(event))
        if len(picked) >= limit + 1:
            break

    next_before = picked[limit - 1]["id"] if len(picked) > limit else None
    items = picked[:limit]
    for event in items:
        event["ts"] = _iso(event["ts"])
    return items, next_before


def reset_for_tests(path=None, autoflush=False) -> None:
    """只给测试用：清空进程内状态，可把落点指到临时文件并关掉后台线程。

    生产代码永远不调用它——"清空安全日志"不是运维动作（这份账与 audit.jsonl 一样
    只增）。写成这个签名是为了让误用一眼可见，也为了让每条用例从确定的空状态出发。
    """
    global _NEXT_ID, _LOADED, _FILE_LINES, _PATH_OVERRIDE, _AUTOFLUSH
    with _LOCK:
        _EVENTS.clear()
        _PENDING.clear()
        _PENDING_IDS.clear()
        _MERGE.clear()
        _NEXT_ID = 1
        _FILE_LINES = 0
        _PATH_OVERRIDE = path
        _AUTOFLUSH = bool(autoflush)
        _ABUSE_WINDOW[0], _ABUSE_WINDOW[1] = 0.0, 0
        _ABUSE_SOURCES.clear()        # 测试从空状态出发，不读盘；要演懒加载的用例显式把 _LOADED 置回 False。
        _LOADED = True


atexit.register(flush)
