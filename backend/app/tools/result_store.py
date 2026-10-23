"""搜索结果句柄：`result_id -> URL` 的**短期映射**，不是正文缓存。

v0.37.4 的正文读取需要一个"模型能指、但指不到任意目的地"的选择面：搜索结果里的
URL 是别人写的网页字段（不可信输入），把它原样交回工具当参数，等于把服务端变成
任意公网 URL 的抓取代理。所以 `web_search` 每返回一条结果，就为它签发一枚随机
句柄；后续读取只认这枚句柄，URL 由服务端从这张表里取。

这张表里**只有** `{url, title, owner, created_at}` 四格：
- 不存正文、snippet、query、请求头、Cookie——它不是内容缓存，正文每次现抓；
- 不落盘：进程内字典，重启即全部失效（这正是一枚 10 分钟句柄该有的寿命）；
- `owner` 是服务端从凭据注入的 user_id：跨用户猜中/泄露的 ID 不能重放。

为什么不是签名 token：签名要引入服务端密钥、token 长约 200 字（会占模型输出预算），
而且无法提前撤销；一张有界的内存表已经满足"短期、可撤销、无盘"。

容量与寿命：TTL 600 s（`time.monotonic`，`_now` 可被测试 monkeypatch）；全局 512 条、
每 owner 64 条，超出按插入顺序淘汰最旧；每次 `remember`/`lookup` 顺带清理过期条目。
一把 `threading.RLock` 护住整张表，临界区只做 dict/list 操作，n ≤ 512 所以扫描有界。
"""
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

ID_PREFIX = "r_"
# secrets.token_urlsafe(12) = 12 字节 → 16 个 base64url 字符（约 96 bit 随机）。
RESULT_ID_RE = re.compile(r"^r_[A-Za-z0-9_-]{16}$")

TTL_SECONDS = 600.0
MAX_TOTAL = 512
MAX_PER_OWNER = 64
MAX_URL_CHARS = 2048
MAX_TITLE_CHARS = 120

_lock = threading.RLock()
# dict 保持插入顺序：淘汰"最旧"就是取最前面那几条。
_store: dict = {}


def _now() -> float:
    """模块级时钟：测试 monkeypatch 这一处就能推进 TTL，不用睡 10 分钟。"""
    return time.monotonic()


def _new_id() -> str:
    return ID_PREFIX + secrets.token_urlsafe(12)


def _owner_key(owner) -> str:
    """owner 的唯一归一化点：写入与查询必须走同一处，否则两侧各有一套等于没有绑定。"""
    return str(owner or "")


def _row_str(row, key: str) -> str:
    if not isinstance(row, dict):
        return ""
    value = row.get(key)
    return "" if value is None else str(value)


def _signable(url: str) -> bool:
    """只有 https、主机名非空、URL ≤ 2048 字的行才配拿句柄。

    http 行不签：读取侧最终只允许 https/443，给一条注定读不了的记录发 ID，比不发
    更糟（模型会拿着它反复失败）。空主机与超长 URL 同理，在进表之前就拒掉。
    """
    if not url or len(url) > MAX_URL_CHARS:
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(host)


def _clip_title(title: str) -> str:
    """存储侧也封顶：映射是有界的，不能靠"来源层会给短标题"这种假设。"""
    if len(title) <= MAX_TITLE_CHARS:
        return title
    return title[:MAX_TITLE_CHARS - 1] + "…"


def _purge_locked(now: float) -> None:
    """清掉已过 TTL 的条目；调用方必须已持有 `_lock`。"""
    expired = [rid for rid, record in _store.items()
               if now - record["created_at"] > TTL_SECONDS]
    for rid in expired:
        del _store[rid]


def _evict_locked() -> None:
    """先按 owner 淘汰、再按全局淘汰；两边都取插入顺序最旧的。"""
    by_owner: dict = {}
    for rid, record in list(_store.items()):
        by_owner.setdefault(record["owner"], []).append(rid)
    for rids in by_owner.values():
        for rid in rids[:max(0, len(rids) - MAX_PER_OWNER)]:
            del _store[rid]
    overflow = len(_store) - MAX_TOTAL
    if overflow > 0:
        for rid in list(_store.keys())[:overflow]:
            del _store[rid]


def remember(rows, owner: str = "") -> list:
    """给这一批搜索结果签发句柄，返回与 `rows` **一一对应**的 ID 列表。

    不可签的行返回 `None`——该行仍会被调用方照常显示，只是没有句柄。签发是纯内存
    动作，不发起任何网络请求，也不读 rows 里的 snippet（只取 url/title）。
    """
    owner_key = _owner_key(owner)
    now = _now()
    issued: list = []
    with _lock:
        _purge_locked(now)
        for row in rows or []:
            url = _row_str(row, "url")
            if not _signable(url):
                issued.append(None)
                continue
            rid = _new_id()
            while rid in _store:            # 96 bit 下的碰撞实际不会发生：撞了就重摇
                rid = _new_id()
            _store[rid] = {
                "url": url,
                "title": _clip_title(_row_str(row, "title")),
                "owner": owner_key,
                "created_at": now,
            }
            issued.append(rid)
        _evict_locked()
    return issued


def lookup(result_id, owner: str = ""):
    """查一枚句柄，返回 `(record | None, reason)`。

    reason ∈ `ok / unknown / expired / owner_mismatch`。owner 不匹配时**只回原因**，
    绝不带出 url/title——调用方因此也没有可泄露的内容。TTL 过期的条目就地删除，
    不让"查过但过期"变成表继续变长的理由。
    """
    rid = str(result_id or "").strip()
    owner_key = _owner_key(owner)
    now = _now()
    with _lock:
        record = _store.get(rid)
        if record is None:
            _purge_locked(now)
            return None, "unknown"
        if now - record["created_at"] > TTL_SECONDS:
            del _store[rid]
            _purge_locked(now)
            return None, "expired"
        _purge_locked(now)
        if record["owner"] != owner_key:
            return None, "owner_mismatch"
        return dict(record), "ok"


def clear() -> None:
    """清空整张表。只给测试做隔离用；生产路径没有调用方。"""
    with _lock:
        _store.clear()
