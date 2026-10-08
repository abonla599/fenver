"""限流账的存储后端（v0.34.0 · D1）。

为什么要有这一层：`auth_router` 里那八本账（改密/注册/找回/聊天/重登失败……）今天住在
模块级的 `defaultdict(list)` 里。单进程时这就够了；一旦同一条服务多开几个 worker，
每个进程各有一份账，"每 10 分钟最多试 10 次"就变成"最多试 10×worker 次"——**而且没有任何
症状**：限流照样生效，只是阈值悄悄放宽。这类"只对一半"的防线比没有防线更贵。

这一层把"账放在哪儿"这件事收进三个动作（记一次 / 读窗口内的次数 / 按内存上限裁剪），
于是：

- **默认且生产唯一在用的是 `MemoryLedgers`**：它不搬数据，`_FAILS` 那些对象本身就是它的
  存储。conftest 和既有用例直接 `_FAILS.clear()`、`sum(len(v) for v in _FAILS.values())`
  全都照旧有效——这不是将就，是有意让"抽一层"不变成"换一套事实来源"。
- `RedisLedgers` 是**为多进程准备的那条路，本版不落生产**（提案第二节 4 的证伪结论照搬）：
  它的行为必须与内存实现**逐条一致**，这一点由
  `tests/test_v0340_d1_ledger_backends.py` 拿同一组用例跑两遍来钉，不是靠读代码相信。

两处只有后端自己知道的取舍，写在下面而不是藏在实现里：

1. **时钟**：内存实现沿用 `time.monotonic()`（本机时钟跳变不能把窗口拉长或缩短）；
   Redis 实现**必须**用 `time.time()`——monotonic 值是"本进程开机以来的秒数"，两个进程
   给出的数互相不可比，把它们写进同一本共享账里等于把账写废。
2. **键的生命周期**：Redis 侧每次写入都 `EXPIRE window`，空闲的键自己消失，`prune` 只负责
   "键数超过内存上限时把整桶已过期的清掉"这一条与内存实现同形的语义。EXPIRE 让桶比内存
   更早不见，但那一刻桶里每个时间戳都已经出窗了——判据上等价，没有哪一次请求会因此多放行。
"""
import os
import time

# Redis 键的前缀：`前缀:账本名:来源键`。账本名来自 `_Ledger.name`（见 auth_router），
# 没登记名字的账本在这里是配置错误，不许静默拼出一个可能和别的账本撞车的键。
DEFAULT_PREFIX = "fenver:ratelimit"


class LedgerBackend:
    """三件事：记一次、读窗口内、按上限裁剪。子类必须全部实现。"""

    name = "abstract"

    def now(self) -> float:
        raise NotImplementedError

    def note(self, ledger, key: str, window: float, moment: float) -> None:
        raise NotImplementedError

    def recent(self, ledger, key: str, window: float, moment: float) -> list:
        raise NotImplementedError

    def prune(self, ledgers, moment: float, max_sources: int) -> None:
        raise NotImplementedError


class MemoryLedgers(LedgerBackend):
    """默认后端：账本对象自己就是存储，一个字节都不搬。"""

    name = "memory"

    def now(self) -> float:
        return time.monotonic()

    def note(self, ledger, key, window, moment) -> None:
        # defaultdict(list) 的 __missing__ 就是这一行的全部依赖：第一次见某个来源时
        # 不需要任何 if，也不需要一个"顺手把空键塞进字典"的隐藏副作用被改掉。
        ledger[key].append(moment)

    def recent(self, ledger, key, window, moment) -> list:
        # 读的时候就地把这一键的过期前缀清掉：这正是老 `_recent` 那两行，一行语义都没动。
        stamps = [t for t in ledger[key] if moment - t < window]
        ledger[key] = stamps
        return stamps

    def prune(self, ledgers, moment, max_sources) -> None:
        for ledger, window in ledgers:
            if len(ledger) <= max_sources:
                continue
            for key in [k for k, v in ledger.items()
                        if not any(moment - t < window for t in v)]:
                ledger.pop(key, None)


class RedisLedgers(LedgerBackend):
    """共享账本：一把 list 装一个 (账本, 来源)，时间戳按写入顺序排在尾巴上。

    用到的命令一共六条：RPUSH / LRANGE / LTRIM / EXPIRE / DEL / SCAN。
    `client` 只要实现这六条的**已发布语义**即可（`redis.Redis` 满足；测试里那个假实现
    也满足，见 tests/test_v0340_d1_ledger_backends.py）。
    """

    name = "redis"

    def __init__(self, client, prefix: str = DEFAULT_PREFIX) -> None:
        self.client = client
        self.prefix = prefix

    def now(self) -> float:
        # 见模块头的第 1 条：跨进程只能用水钟。
        return time.time()

    def _key(self, ledger, key: str) -> str:
        name = getattr(ledger, "name", None)
        if not name:
            raise ValueError("这本限流账没有登记名字，共享存储里拼不出不会撞车的键")
        return f"{self.prefix}:{name}:{key}"

    def note(self, ledger, key, window, moment) -> None:
        rk = self._key(ledger, key)
        self.client.rpush(rk, repr(float(moment)))
        # 桶的寿命 = 距离最后一次写入一个窗口；与内存实现"整桶出窗才可被裁"判据等价。
        self.client.expire(rk, max(1, int(window)))

    def recent(self, ledger, key, window, moment) -> list:
        rk = self._key(ledger, key)
        raw = self.client.lrange(rk, 0, -1)
        stamps = [float(x) for x in raw]
        fresh = [t for t in stamps if moment - t < window]
        # RPUSH 保证升序，所以过期的一定排在前面一段：LTRIM 掉那一段，桶自己瘦下来。
        if len(fresh) != len(stamps):
            self.client.ltrim(rk, len(stamps) - len(fresh), -1)
        return fresh

    def prune(self, ledgers, moment, max_sources) -> None:
        for ledger, window in ledgers:
            pattern = f"{self.prefix}:{getattr(ledger, 'name', '')}:*"
            keys = self._scan(pattern)
            if len(keys) <= max_sources:
                continue
            for rk in keys:
                raw = self.client.lrange(rk, 0, -1)
                if not any(moment - float(t) < window for t in raw):
                    self.client.delete(rk)

    def _scan(self, pattern: str) -> list:
        """SCAN 全量收齐（游标为 0 表示迭代结束）。"""
        found, cursor = [], 0
        while True:
            cursor, batch = self.client.scan(cursor=cursor, match=pattern, count=1000)
            found.extend(batch)
            if not cursor:
                return found


def select_backend(client=None) -> LedgerBackend:
    """按环境变量选后端；**不设变量就是内存实现**（生产形态）。

    显式写了 `FENVER_RATE_LIMIT_LEDGER=redis` 却给不出客户端时一律**当场炸**，不悄悄退回
    内存：那句"多开 worker 之前先把账迁到共享存储"要的就是这个失败——静默降级等于把
    放宽的阈值重新发明一遍。
    """
    choice = (os.environ.get("FENVER_RATE_LIMIT_LEDGER") or "memory").strip().lower()
    if choice in ("", "memory"):
        return MemoryLedgers()
    if choice == "redis":
        if client is None:
            url = os.environ.get("FENVER_RATE_LIMIT_REDIS_URL")
            if not url:
                raise RuntimeError(
                    "FENVER_RATE_LIMIT_LEDGER=redis 但没给 FENVER_RATE_LIMIT_REDIS_URL："
                    "宁可启动失败，也不要各 worker 一本账、把阈值悄悄乘以进程数")
            try:
                import redis                  # 可选依赖：只有真要走这条路才装它
            except ImportError as exc:
                # 没装 = 启动失败，这里**不许**退回内存实现：那句"多开 worker 之前先把账
                # 迁到共享存储"要的就是失败本身；退回内存等于把阈值乘以进程数还看不出来。
                raise RuntimeError(
                    "FENVER_RATE_LIMIT_LEDGER=redis 但本机没装 redis 包："
                    "要么装上它，要么别开共享账本——不许悄悄退回每 worker 一本账") from exc
            client = redis.Redis.from_url(url)
        return RedisLedgers(client)
    raise RuntimeError(f"不认识的限制账后端 {choice!r}（可选：memory / redis）")
