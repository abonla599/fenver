"""搜索用量账本：这台机器的降级链跑了几次、挂在哪个源上、每个源成功率多少。

和 `usage.py` 是两本账，不是同一本的两页：那本答"谁用了多少 token、谁的钱"，按
(天, 人, provider) 一行；这本答"搜索这门外部依赖的现状"，按 (天, 源) 一行，
**没有人这一维**——账本按 (天, 源) 聚合，不记是谁搜的，也不该记：搜索词是用户内容，
这本账连 query 都不落，再加一个归属列只会把它变成第二本用量账。v0.37.4 起
`web_search_tool` 声明了 `needs_user`，工具层确实拿得到服务端注入的 `user_id`，
但那个身份只用于搜索结果句柄的归属与审计（`tools/result_store.py` 的 owner、
读取工具那一路的跨用户校验），**不进这本账**。

为什么按天聚合而不是逐条流水：照抄 `usage.py` 的理由——`data/` 没有任何清理机制，
留着流水等于留一个迟早要人手工删的文件。按 (天, 源) 一行，行数被「天数 × 源数」
框住，不需要轮转。

隐私口径（这条是这个文件最重要的话）：**账本里没有任何原文，连哈希都不落。**
- 不记 query：`usage.py` 那本账连 prompt 都没有一个字，这本跟着同一条纪律走；
- 也不记 query 哈希：搜索词是 2–4 个词的短串，字典一跑就能把哈希还原成原文，
  「存了哈希」在这种长度上等价于「存了原文还多一层假装脱敏」，是更差的取舍。
  要问"哪句话搜不出东西"，靠 `stream_events` 的留痕通道（那是用户侧可见的自己的
  搜索，本来就该看得到），不靠这本账。

失败归因是**写死的枚举**，不是自由文本：下面每一个值都对着 `search_api_sources` /
`search_chain` / `web_search` 里一条真实存在的分支。枚举之外的值进不了账
（`record_search` 直接 `ValueError`），因为一旦允许写"为什么失败"的那句话，
账本就变成日志的第二个副本——聚合不出来，还随时会长出新的说法。

两处明确不进这本账的（判据在 tests/test_v032_w4_search_usage_contract.py）：
1. **可用性探针**：`availability._refresh()` 每 15 分钟拿「北京 天气」真跑一次链，
   那是运维检查，不是有人搜了东西。记进去就等于每小时虚增 4 次。
2. **浏览器/WebView 直连**（v0.32 W2）：用户自己的 key 从用户设备直接出站，
   本站一个字节都看不见。那部分用量记不进这本账，也不拿 0 冒充"用户没用"——
   边界写在 docs/搜索源浏览器直连矩阵.md。
"""
import json
import os
import threading
from datetime import datetime, timedelta

from app.core.atomic_write import write_json_atomic
from app.core.paths import data_root

_lock = threading.RLock()
_days: dict = {}
_PATH: str = ""

# ---------- 归因枚举（每个值都能指回一条真实分支，不许自由文本） ----------

REASON_OK = "ok"                  # 该源给出了非空结果（链在这里短路返回）

# 没出网就回空的两种：不算该源「失败」，也就不进成功率的分母。
# 一台没配 key 的机器上，bocha 每次都是 not_enabled；把它记成失败，报出来的
# 就是「bocha 成功率 0%」这句假话——它根本没被试过。
SKIPPED_REASONS = (
    "not_enabled",                # search_api_sources：`if not key: return []`
    "empty_query",                # 源层 `if not str(query or "").strip(): return []`
)

# 出网了、没拿到结果的四种，对着 _ask 的真实分支逐个写死。
FAILED_REASONS = (
    "network",                    # httpx 异常：连不上 / 超时 / TLS 失败
    "non_200",                    # 上游回了非 200
    "oversize",                   # 响应体越过字节上限（MAX_BYTES / API_MAX_BYTES）
    "parse_empty",                # 200 但一条也认不出，或回来的 JSON 解析不动
    "no_result",                  # 合同是"什么坏都回空列表"的源说不清为什么：
                                  # 爬取源（web_search.search 把四类失败全压成 []）
                                  # 与链上注入的 mock 源只能归到这一档
    "source_raised",              # 源违约抛出异常，search_chain 按空处理并换下一源
)

ALL_REASONS = (REASON_OK,) + SKIPPED_REASONS + FAILED_REASONS
DEFAULT_WINDOW_DAYS = 7           # 管理页那块默认回答"这周"
MIN_WINDOW_DAYS = 1
MAX_WINDOW_DAYS = 90              # 窗口上限：读面是按天枚举再查字典，不给它无界参数


def _default_path() -> str:
    env_path = os.getenv("SEARCH_USAGE_DB_PATH")
    if env_path:
        return os.path.abspath(env_path)
    return os.path.join(data_root(), "data", "search_usage.json")


def _blank_source() -> dict:
    return {"attempts": 0, "ok": 0, "failed": 0, "skipped": 0, "reasons": {}}


def _blank_day() -> dict:
    # calls/ok/failed 是**工具执行**维度（一次 web_search 一笔）；
    # sources 是**源尝试**维度（一次执行在链上可能留下好几笔）。两个维度都会
    # 被问，而且问的是不同的数：一次主源非 200、备用源答上的搜索,
    # calls=1 但同时有两个源各 +1 尝试。
    return {"calls": 0, "ok": 0, "failed": 0, "sources": {}}


def _num(value) -> int:
    """盘上读来的计数压成整数，读不动就当 0——注意这条不是防御性冗余。

    `int("abc")` 抛的 ValueError 落在 restore 那个 try/except **外面**（那里只兜
    open/json.load），而 restore 在模块导入时就跑一次：那时崩的不是"这一页读不了"，
    是整个后端起不来。可观测性不能变成单点故障——同一条纪律见 _note_search_usage。
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def restore(path: str = None) -> int:
    """从盘上读回来（启动时一次；测试用它等价于"重启进程"）。返回已有天数。"""
    global _days, _PATH
    target = os.path.abspath(path or _default_path())
    with _lock:
        _PATH = target
        _days = {}
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return 0
        except (ValueError, OSError) as e:
            # 与 usage.restore 同一条处置：读不动就先备份再空账起步，
            # 别让一份坏 JSON 变成搜索功能起不来。
            backup = target + ".corrupt"
            try:
                os.replace(target, backup)
                print(f"⚠️ 搜索用量账本读不了（{e}），已备份为 {backup}，本轮从空账开始")
            except OSError:
                print(f"⚠️ 搜索用量账本读不了且无法备份（{e}），本轮从空账开始")
            return 0
        days = (data or {}).get("days") or {}
        if not isinstance(days, dict):
            return 0
        for day, block in days.items():
            merged = _blank_day()
            for field in ("calls", "ok", "failed"):
                merged[field] = _num((block or {}).get(field))
            for name, row in ((block or {}).get("sources") or {}).items():
                blank = _blank_source()
                for field in ("attempts", "ok", "failed", "skipped"):
                    blank[field] = _num((row or {}).get(field))
                blank["reasons"] = {k: _num(v)
                                    for k, v in ((row or {}).get("reasons") or {}).items()}
                merged["sources"][name] = blank
            _days[day] = merged
        return len(_days)


def _write() -> None:
    path = _PATH or _default_path()
    write_json_atomic(path, {"days": _days})


def _check_attempt(attempt) -> tuple:
    """一条尝试的形状与枚举闸门；不合格一律 ValueError，且发生在任何写之前。"""
    if not isinstance(attempt, dict):
        raise ValueError(f"搜索尝试必须是对象，收到 {type(attempt).__name__}")
    source = str(attempt.get("source") or "").strip()
    if not source:
        raise ValueError("记账必须指明是哪个源：source 不能为空")
    reason = attempt.get("reason")
    if reason not in ALL_REASONS:
        raise ValueError(f"搜索失败归因只许写死的枚举，收到 {reason!r}（可用：{ALL_REASONS}）")
    return source, reason


def record_search(attempts, ok: bool) -> None:
    """记一次真实的 web_search 执行。

    `attempts` 是 `search_chain.search(..., attempts=[])` 带出来的那份逐源尝试
    （`[{"source", "reason"}]`）；`ok` 是这一次执行最终有没有给用户拿出结果
    （= 链有没有回非空），由调用方判，不由这里反推——两侧各说各的口径早晚会漂。

    一次执行 = calls +1，无论成敗：只记成功的那本账会系统性低估调用量，
    而"这周搜了几次"问的就是次数。
    """
    checked = [_check_attempt(a) for a in (attempts or [])]
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    with _lock:
        day = _days.setdefault(today, _blank_day())
        day["calls"] += 1
        day["ok" if bool(ok) else "failed"] += 1
        for source, reason in checked:
            row = day["sources"].setdefault(source, _blank_source())
            if reason == REASON_OK:
                row["attempts"] += 1
                row["ok"] += 1
            elif reason in SKIPPED_REASONS:
                row["skipped"] += 1
            else:
                row["attempts"] += 1
                row["failed"] += 1
            row["reasons"][reason] = row["reasons"].get(reason, 0) + 1
        _write()


def _window(days: int) -> list:
    """回溯 N 个日历天（含今天）的日期串，升序。

    按日期枚举而不是"账上有哪些天"：缺账的那天必须读成零。只列有账的天，
    "这周一次都没搜"和"这周没读到账"就长成同一个样子——v0.25 R4a 已经把这条
    判据钉过一次（零要读成零，不是读成错）。
    """
    today = datetime.now().astimezone()
    n = min(max(int(days), MIN_WINDOW_DAYS), MAX_WINDOW_DAYS)
    return [(today - timedelta(k)).strftime("%Y-%m-%d") for k in reversed(range(n))]


def _rate(row: dict):
    """该源成功率 = 给结果的次数 / 真出网的尝试次数。

    没试过回 None，不回 0.0：一台没配 key 的机器上 bocha 的 attempts 是 0，
    报 0% 是把"没用它"说成"用它全败"。前端把 None 读成「—」。
    """
    attempts = row["attempts"]
    return None if attempts <= 0 else round(row["ok"] / attempts, 4)


def aggregate(days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """窗口内的按源聚合：这周搜了几次、挂在哪个源几次、每源成功率。

    只读不动写：这份数据由 `record_search` 落，三个字段（calls/ok/failed）与
    按源计数都在同一把锁里取快照，读不到半条更新。
    """
    window = _window(days)
    with _lock:
        totals = {"calls": 0, "ok": 0, "failed": 0}
        per_day, sources = [], {}
        for day in window:
            block = _days.get(day) or _blank_day()
            totals["calls"] += block["calls"]
            totals["ok"] += block["ok"]
            totals["failed"] += block["failed"]
            per_day.append({"day": day, "calls": block["calls"], "ok": block["ok"],
                            "failed": block["failed"]})
            for name, row in block["sources"].items():
                acc = sources.setdefault(name, _blank_source())
                for field in ("attempts", "ok", "failed", "skipped"):
                    acc[field] += row[field]
                for reason, count in row["reasons"].items():
                    acc["reasons"][reason] = acc["reasons"].get(reason, 0) + count
        rows = [{"source": name, **row, "success_rate": _rate(row)}
                for name, row in sorted(sources.items(),
                                        # 先按量排（挂得最多的源排在前面），同量按名字
                                        key=lambda kv: (-kv[1]["attempts"] - kv[1]["skipped"],
                                                        kv[0]))]
        return {
            "window": {"days": len(window), "from": window[0], "to": window[-1]},
            "totals": totals,
            "sources": rows,
            "by_day": per_day,
        }


def days() -> list:
    """账上有哪几天（含零天的窗口之外那些历史），给日期选择器与排障用。"""
    with _lock:
        return sorted(_days)


restore()
