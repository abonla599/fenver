# backend/tests/test_v0340_d1_ledger_backends.py
"""v0.34.0 · D1：限流账的两种存储实现必须**逐条判据一致**，而且生产默认仍是内存。

这一份要钉的是三件事，各自为什么只能这么钉：

1. **同一组用例跑两遍 backend**（判据 D1-3）。"看着像"不算一致：内存侧 `ledger[key] = 过滤后的列表`
   与共享侧的 `LTRIM 掉过期前缀` 是两个完全不同的动作，只有拿同一串"记 / 过窗 / 再记 / 裁剪"
   的剧本在两边各跑一遍，才说得出"第 11 次仍然被挡、第 10 次仍然放行"这条判据没被搬坏。
2. **多 worker 放大预算这件事要能被执行出来**。它是 D1 存在的全部理由，但本机不会真起
   两个 worker：这里用"同一个后端对象、两份账本"演一次进程隔离（内存后端的存储就是账本
   本身，两份账本就是两个进程），再用一份共享存储演一次——前者预算按进程数翻倍、后者不翻。
   这条不是装饰：它就是"为什么不干脆把默认值改成 Redis"的答案（判据第七节 D1 的证伪结论）。
3. **默认与边界**：不设环境变量就是内存；说要用 redis 却给不出连接**必须启动失败**，
   不许悄悄退回内存（那等于把刚说出口的共享账本又收回去）；`Dockerfile` 里那句
   `--workers` 警告必须在，因为它是这条放大器唯一写给下一个接手人的地方。

Redis 那一侧的 client 是本文件里的 `_FakeRedis`：它只实现后端真正用到的六条命令
（RPUSH / LRANGE / LTRIM / EXPIRE / DEL / SCAN）的已发布语义，不是"随便应答的 mock"——
语义写歪，第 1、2 两条就变成自证。真机 Redis 想跑同一组剧本：把 `_FakeRedis()` 换成
`redis.Redis.from_url(...)` 即可（用例不依赖它存在）。
"""
import re
import textwrap
from pathlib import Path

import pytest

from app.core import auth_router, ledger_backend
from app.core.ledger_backend import LedgerBackend, MemoryLedgers, RedisLedgers

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"

WINDOW = 600.0
OTHER_WINDOW = 86_400.0          # 注册/改密那两本的尺度，用来钉"各按自己的窗口过"
CAP = 10                          # 与 MAX_FAILURES_PER_WINDOW 同数，写在用例里更读得懂


class _FakeRedis:
    """六条命令的最小可信实现（含 Redis 的惰性过期语义）。

    切片语义按已发布的 Redis 文档来：LRANGE/LTRIM 的 stop 是**闭区间**，负数从尾部数；
    空列表 = 键不存在。EXPIRE 之后到点就当键已不存在，且是"访问时才消失"——和真 Redis
    的惰性删除一致，因为后端 `prune` 依赖 SCAN 能看见"还在但整桶出窗"的键。
    """

    def __init__(self, clock=None):
        self.data = {}
        self.deadline = {}
        self.clock = clock or (lambda: 0.0)
        self.commands = set()

    def _alive(self, key):
        exp = self.deadline.get(key)
        if exp is not None and self.clock() >= exp:
            self.data.pop(key, None)
            self.deadline.pop(key, None)
        return key in self.data

    def rpush(self, key, *values):
        self.commands.add("rpush")
        self._alive(key)
        lst = self.data.setdefault(key, [])
        lst.extend(str(v) for v in values)
        return len(lst)

    def lrange(self, key, start, stop):
        self.commands.add("lrange")
        if not self._alive(key):
            return []
        lst = self.data[key]
        if stop == -1:
            return list(lst[start:])
        return list(lst[start:stop + 1])

    def ltrim(self, key, start, stop):
        self.commands.add("ltrim")
        if not self._alive(key):
            return True
        lst = self.data[key]
        kept = lst[start:] if stop == -1 else lst[start:stop + 1]
        if kept:
            self.data[key] = kept
        else:
            self.data.pop(key, None)
            self.deadline.pop(key, None)
        return True

    def expire(self, key, seconds):
        self.commands.add("expire")
        if not self._alive(key):
            return False
        self.deadline[key] = self.clock() + seconds
        return True

    def delete(self, *keys):
        self.commands.add("delete")
        gone = 0
        for key in keys:
            if self.data.pop(key, None) is not None:
                gone += 1
            self.deadline.pop(key, None)
        return gone

    def scan(self, cursor=0, match=None, count=1000):
        """单趟返完（游标 0 = 迭代结束）。match 用 fnmatch 的形状，够用且不过度承诺。"""
        self.commands.add("scan")
        from fnmatch import fnmatch
        keys = [k for k in list(self.data) if self._alive(k)]
        if match:
            keys = [k for k in keys if fnmatch(k, match)]
        return 0, keys


def _redis_backend(clock=None):
    client = _FakeRedis(clock=clock or (lambda: 0.0))
    return RedisLedgers(client), client


def _ledger(name):
    """一个最小可用的账本句柄：真身就是 auth_router._Ledger（带 name 的那一个）。"""
    return auth_router._Ledger(name)


# ---------- 1. 同一组剧本，两个后端各跑一遍 ----------

CASES = {}


def _make(kind):
    """(后端, 数桶器, 数条目器)。两个探针都必须两边各自成立：内存侧的桶就是 dict 的键、
    条目就是那个 list；共享侧的桶是带账本名前缀的键、条目是那把 list 的长度。

    拿 `len(dict)` 去问共享存储会永远读到 0，prune 那两条在 redis 分支上就成了空转——
    这正是"同一组剧本跑两遍"要防的自证。
    """
    if kind == "memory":
        backend = MemoryLedgers()
        return (backend,
                lambda led: len(led),
                lambda led, key: len(led.get(key, ())))
    client = _FakeRedis()
    backend = RedisLedgers(client)

    def buckets(led):
        return len(client.scan(match=f"{backend.prefix}:{led.name}:*")[1])

    def entries(led, key):
        return len(client.lrange(f"{backend.prefix}:{led.name}:{key}", 0, -1))

    return backend, buckets, entries


CASES = {"memory": lambda: _make("memory"), "redis": lambda: _make("redis")}
BACKENDS = list(CASES)


@pytest.mark.parametrize("kind", BACKENDS)
def test_notes_count_only_within_the_window(kind):
    """判据最基础的一条：记 3 次读 3 次；把时钟推过窗口，读回 0 次。

    两个后端都必须"过窗的条目不再计入"，而且**不是靠读的时候数**——内存侧那行
    `ledger[key] = 过滤后的列表`、共享侧那句 LTRIM，作用是把账本本身瘦下来。
    """
    backend, buckets, entries = CASES[kind]()
    led = _ledger("fails")
    for i in range(3):
        backend.note(led, "1.2.3.4", WINDOW, 100.0 + i)
    assert len(backend.recent(led, "1.2.3.4", WINDOW, 100.0 + 2)) == 3
    assert backend.recent(led, "1.2.3.4", WINDOW, 100.0 + 3 + WINDOW) == []


@pytest.mark.parametrize("kind", BACKENDS)
def test_the_window_boundary_is_exclusive_on_both_backends(kind):
    """出窗的那一刻必须两边同一个答案：`moment - t < window` 是严格小于。

    边界写成 <= 的话，两侧都会多挡一次；这条把它钉成"刚好等于窗口 = 已经不算了"。
    """
    backend, buckets, entries = CASES[kind]()
    led = _ledger("fails")
    backend.note(led, "ip", WINDOW, 1000.0)
    assert len(backend.recent(led, "ip", WINDOW, 1000.0 + WINDOW - 0.001)) == 1
    assert backend.recent(led, "ip", WINDOW, 1000.0 + WINDOW) == []


@pytest.mark.parametrize("kind", BACKENDS)
def test_one_source_never_spends_another_source_s_budget(kind):
    """按来源分桶这件事本身：A 打完 10 次，B 的第 1 次仍然算 1 次。"""
    backend, buckets, entries = CASES[kind]()
    led = _ledger("fails")
    for _ in range(CAP):
        backend.note(led, "a", WINDOW, 10.0)
    assert len(backend.recent(led, "a", WINDOW, 10.0)) == CAP
    assert len(backend.recent(led, "b", WINDOW, 10.0)) == 0
    backend.note(led, "b", WINDOW, 10.0)
    assert len(backend.recent(led, "b", WINDOW, 10.0)) == 1


@pytest.mark.parametrize("kind", BACKENDS)
def test_prune_uses_each_ledger_s_own_window_and_only_drops_whole_buckets(kind):
    """裁剪的两个方向都要有：出窗的整桶要收得回去，没出窗的一律不许动。

    这是 test_prune_uses_each_ledgers_own_window 在存储层的对应物，方向反过来钉：
    ① 24 小时那本里"三小时前"的记录，拿十分钟的尺子量就成了"已出窗"——真裁掉就是把注册
       与改密的配额悄悄放宽（fail-open，什么症状都没有）；
    ② 同一个键里"昨天更早"的那一段确实出窗了，不裁就是只胀不收。
    两个方向在同一条用例里，是因为只钉一边时把另一边写反也能绿。
    """
    backend, buckets, entries = CASES[kind]()
    moment = 1_000_000.0
    long_way, short_way = _ledger("registers"), _ledger("chats")
    # 这本账的窗口是 24 小时：三小时前 = 仍在窗内；二十五小时前 = 整桶出窗。
    backend.note(long_way, "three-hours-ago", OTHER_WINDOW, moment - 3_000)
    backend.note(long_way, "twenty-five-hours-ago", OTHER_WINDOW, moment - 90_000)
    # 这本账的窗口是十分钟：刚刚那一次必须还在。
    backend.note(short_way, "just-now", WINDOW, moment)
    backend.prune([(long_way, OTHER_WINDOW), (short_way, WINDOW)], moment, 0)
    assert entries(long_way, "three-hours-ago") == 1,         "还在自己 24 小时窗口里的桶被裁了：注册配额被一把短尺子悄悄放宽"
    assert entries(long_way, "twenty-five-hours-ago") == 0,         "整桶出窗却没收回去：共享存储里的键只胀不收"
    assert entries(short_way, "just-now") == 1, "十分钟那本里刚记的一次被裁：预算被凭空放宽"
    assert buckets(long_way) == 1 and buckets(short_way) == 1,         f"桶数不对：registers={buckets(long_way)} chats={buckets(short_way)}"


@pytest.mark.parametrize("kind", BACKENDS)
def test_prune_drops_a_bucket_whose_every_entry_is_out_of_window(kind):
    """整桶出窗就必须收回去——否则共享存储里的键只增不减（内存闸门那一半）。"""
    backend, buckets, entries = CASES[kind]()
    led = _ledger("fails")
    for i in range(3):
        backend.note(led, f"ip{i}", WINDOW, 0.0)
    assert buckets(led) == 3
    backend.prune([(led, WINDOW)], WINDOW + 1.0, 0)
    assert buckets(led) == 0, "整桶出窗却没被裁：只胀不收"


@pytest.mark.parametrize("kind", BACKENDS)
def test_prune_respects_the_cap_and_leaves_a_small_table_alone(kind):
    """上限之下不许裁剪：那条路是内存闸门，不是安全窗口。"""
    backend, buckets, entries = CASES[kind]()
    led = _ledger("fails")
    for i in range(3):
        backend.note(led, f"ip{i}", WINDOW, 0.0)
    backend.prune([(led, WINDOW)], 10_000.0, 4096)
    assert buckets(led) == 3, "没到上限就动手：等于把 prune 变成第二把窗口尺"


@pytest.mark.parametrize("kind", BACKENDS)
def test_a_stale_bucket_restarts_from_one_not_from_history(kind):
    """过窗之后再记一次，账本从 1 开始——不能把出窗的旧条目也数进去。

    内存侧靠 `ledger[key] = 过滤后的列表`，共享侧靠 LTRIM；少任何一侧，症状都是
    "窗口早过了但配额还是满的"，而且只在长跑的进程里出现。
    """
    backend, buckets, entries = CASES[kind]()
    led = _ledger("fails")
    for _ in range(CAP):
        backend.note(led, "ip", WINDOW, 0.0)
    assert backend.recent(led, "ip", WINDOW, 5_000.0) == []      # 读一次 = 顺手瘦下来
    backend.note(led, "ip", WINDOW, 5_001.0)
    assert len(backend.recent(led, "ip", WINDOW, 5_001.0)) == 1


@pytest.mark.parametrize("kind", BACKENDS)
def test_reading_a_window_shrinks_the_ledger_itself_on_both_backends(kind):
    """`recent` 不只是"返回过滤后的数"，它必须把账本本身瘦下来。

    这条存在的全部理由是探针测不到的那一半：少写回（内存侧）或少 LTRIM（共享侧）时
    `len(recent(...))` 两边都还是对的——出窗的条目永远留在桶里，长跑进程的共享存储
    只增不减，而现网没人会看见，直到某个键攒了几十万个时间戳。
    判据：把 MemoryLedgers.recent 里 `ledger[key] = stamps` 删掉，或把 RedisLedgers.recent
    里那句 ltrim 删掉，本用例两边各红一条。
    """
    backend, buckets, entries = CASES[kind]()
    led = _ledger("fails")
    for _ in range(CAP):
        backend.note(led, "ip", WINDOW, 0.0)
    assert entries(led, "ip") == CAP
    assert backend.recent(led, "ip", WINDOW, 5_000.0) == []
    assert entries(led, "ip") == 0, \
        f"{kind} 后端读完窗口却没瘦账本：出窗的条目永远留在桶里"


def test_the_shared_backend_gives_every_bucket_a_ttl():
    """共享侧每次写入都挂 EXPIRE：空闲的键自己消失，不等人来 prune。

    这不是装饰——SCAN 全表扫是有代价的，prune 只在键数超上限时才动手。少了这一句，
    共享存储里的空闲键要等到下一次超上限才可能被清，而那之前它们一直是扫描成本。
    """
    clock = {"t": 100.0}
    backend, client = _redis_backend(clock=lambda: clock["t"])
    led = _ledger("fails")
    backend.note(led, "ip", WINDOW, clock["t"])
    assert "expire" in client.commands, "写入没挂 TTL：空闲键只能靠 prune 收"
    assert client.deadline[f"{backend.prefix}:fails:ip"] == 100.0 + WINDOW, \
        "TTL 不是这本账自己的窗口：桶比窗口先没或后没都算判据漂移"
    # 24 小时那本必须挂 24 小时的 TTL：把窗口写死成"十分钟"在内存后端毫无症状，
    # 在共享存储里却是注册/改密的配额被悄悄放宽（桶十分钟就没了）。
    long_led = _ledger("registers")
    backend.note(long_led, "ip", OTHER_WINDOW, clock["t"])
    assert client.deadline[f"{backend.prefix}:registers:ip"] == 100.0 + OTHER_WINDOW, \
        "TTL 用的是别本账的尺子：长窗口那本在共享存储里提前消失"



# ---------- 2. 多 worker 放大预算：D1 存在的全部理由 ----------

def test_two_in_process_ledgers_do_double_the_budget_and_a_shared_one_does_not():
    """内存后端的存储就是账本本身：两份账本 = 两个 worker，预算按进程数翻倍。

    这不是假想：`--workers 2` 之后"每窗口 10 次"变成 20 次，而且限流照样生效、日志里
    什么都看不见。共享存储那侧两份"进程"打的是同一个键，第 11 次就挡得住。
    这条同时回答"为什么不把默认值直接改成 redis"——本版不落生产（PRD 第二节 4 已证伪），
    但放大器要能被执行出来，否则 Dockerfile 那句警告只是装饰。
    """
    memory = MemoryLedgers()
    worker_a, worker_b = _ledger("fails"), _ledger("fails")
    for _ in range(CAP):
        memory.note(worker_a, "attacker", WINDOW, 1.0)
        memory.note(worker_b, "attacker", WINDOW, 1.0)
    assert len(memory.recent(worker_a, "attacker", WINDOW, 1.0)) == CAP
    assert len(memory.recent(worker_b, "attacker", WINDOW, 1.0)) == CAP
    assert all(len(memory.recent(x, "attacker", WINDOW, 1.0)) < CAP + 1 for x in (worker_a, worker_b)), \
        "两个进程各记各的，谁都挡不住：这就是共享账本要解决的问题"

    clock = {"t": 1.0}
    shared, client = _redis_backend(clock=lambda: clock["t"])
    a_view, b_view = _ledger("fails"), _ledger("fails")     # 同一份存储，两个调用方
    for _ in range(CAP):
        shared.note(a_view, "attacker", WINDOW, clock["t"])
        shared.note(b_view, "attacker", WINDOW, clock["t"])
    assert len(shared.recent(a_view, "attacker", WINDOW, clock["t"])) == CAP + CAP
    assert len(shared.recent(b_view, "attacker", WINDOW, clock["t"])) == CAP + CAP, \
        "共享存储里两个调用方读到的不是同一本账：那还是各记各的"


# ---------- 3. 时钟与后端的对应关系 ----------

def test_the_shared_backend_uses_a_wall_clock_because_monotonic_is_per_process():
    """`RedisLedgers.now()` 必须是水钟：monotonic 是两个进程之间不可比的数。

    写进共享账本就等于把账写废（新 worker 的 monotonic 可能比旧的还小）。
    """
    import time as _time

    wall = _time.time()
    assert abs(RedisLedgers(_FakeRedis()).now() - wall) < 60
    assert MemoryLedgers().now() < wall, "内存实现该用单调钟：本机时钟跳变不该拉长窗口"


def test_the_router_reads_its_clock_from_the_selected_backend():
    """路由层的 `_now()` 只是后端时钟的转发：换后端就换钟，不在调用点各拿一次。"""
    seen = []
    probe = MemoryLedgers()
    probe.now = lambda: (seen.append(1) or 1234.5)   # 只改这一个对象的实例属性
    original = auth_router._LEDGER_BACKEND
    try:
        auth_router._LEDGER_BACKEND = probe
        assert auth_router._now() == 1234.5
    finally:
        auth_router._LEDGER_BACKEND = original
    assert seen == [1]


class _SpyBackend(MemoryLedgers):
    """只为了"记账时递出去的是哪一把尺子"而存在：写进共享存储的桶寿命就是这个数。"""

    def __init__(self):
        self.seen = []

    def note(self, ledger, key, window, moment):
        self.seen.append((getattr(ledger, "name", "?"), window))
        return super().note(ledger, key, window, moment)


def test_the_router_notes_each_ledger_with_its_own_registered_window():
    """写账时那条窗口必须从 `_LEDGERS` 现取，而不是在通路上抄一个常数。

    内存后端上抄错数字毫无症状（`recent` 读的时候另有窗口），到 Redis 那侧就变成
    "24 小时那两本的桶十分钟就没了"——注册/改密配额被悄悄放宽。这条钉的就是 `_window_of`。
    """
    spy = _SpyBackend()
    original = auth_router._LEDGER_BACKEND
    try:
        auth_router._LEDGER_BACKEND = spy
        auth_router._note_failure("192.0.2.9")
        auth_router._note_registration("192.0.2.9")
        auth_router._note_reset("192.0.2.9")
    finally:
        auth_router._LEDGER_BACKEND = original
    windows = dict(spy.seen)
    assert windows["fails"] == auth_router.FAILURE_WINDOW_SECONDS
    assert windows["registers"] == auth_router.REGISTER_WINDOW_SECONDS, \
        "注册那本拿到的不是它登记的窗口：共享存储里桶会比判据先死"
    assert windows["resets"] == auth_router.RESET_WINDOW_SECONDS
    assert windows["registers"] != windows["fails"], \
        "两本账共用一把尺子：`_window_of` 退化成常数了"


# ---------- 4. 默认、失败方向、与那条写给接手人的警告 ----------

def test_the_default_backend_is_memory_and_production_stays_on_it(monkeypatch):
    """不设任何环境变量 = 内存实现（本版生产形态），选后端这件事只有一处读环境。"""
    monkeypatch.delenv("FENVER_RATE_LIMIT_LEDGER", raising=False)
    assert isinstance(ledger_backend.select_backend(), MemoryLedgers)
    monkeypatch.setenv("FENVER_RATE_LIMIT_LEDGER", "memory")
    assert isinstance(ledger_backend.select_backend(), MemoryLedgers)
    # redis 那一支也要真的被走一遍：只测"默认是内存"的话，把 choice 的判断抄成
    # `if choice in ("memory", "redis")` 也照样绿，而那条路的意义是"多进程共用一本账"。
    monkeypatch.setenv("FENVER_RATE_LIMIT_LEDGER", "REDIS")
    chosen = ledger_backend.select_backend(client=_FakeRedis())
    assert isinstance(chosen, RedisLedgers), "写了 redis 却选不出共享后端：这条路是装饰"



def test_the_router_module_starts_on_the_memory_backend():
    """auth_router 在 import 期选的那一个也必须是内存：这条钉的是"今天上线的东西"。"""
    assert isinstance(auth_router._LEDGER_BACKEND, MemoryLedgers)


def test_asking_for_redis_without_a_connection_fails_instead_of_quietly_degrading(monkeypatch):
    """说要用共享账本却给不出连接：启动必须炸。

    悄悄退回内存 = 把刚说出口的"多 worker 共一本账"收回成"每 worker 一本"，而且没有任何
    症状——这条锁盯的就是这个 fail-open 方向。
    """
    monkeypatch.setenv("FENVER_RATE_LIMIT_LEDGER", "redis")
    monkeypatch.delenv("FENVER_RATE_LIMIT_REDIS_URL", raising=False)
    with pytest.raises(RuntimeError):
        ledger_backend.select_backend()
    monkeypatch.setenv("FENVER_RATE_LIMIT_LEDGER", "carrier-pigeon")
    with pytest.raises(RuntimeError):
        ledger_backend.select_backend()


def test_ledgers_whose_names_share_a_prefix_still_get_their_own_buckets():
    """共享存储的键里有账本名：名字只差后缀也必须分桶。

    这条看着像挑刺，实际钉的是一个真实形状：`reset_fails` 与 `reset_fails_by_account`
    只差后缀，而它们按不同的维度记（IP / 用户名）。如果后端把名字截断或省掉分隔符，
    两本账会在 Redis 里撞成一本——症状是"猜找回答案的人顺便把登录预算也烧了"，
    在内存后端上完全看不出来（两个 dict 各是各的）。
    """
    backend, buckets, _entries = _make("redis")
    a, b = _ledger("reset_fails"), _ledger("reset_fails_by_account")
    for i in range(CAP):
        backend.note(a, "same-key", WINDOW, 10.0)
    backend.note(b, "same-key", WINDOW, 10.0)
    assert len(backend.recent(a, "same-key", WINDOW, 10.0)) == CAP, \
        "两本账在共享存储里撞成了同一本：按账号那本的预算漏进了按 IP 那本"
    assert len(backend.recent(b, "same-key", WINDOW, 10.0)) == 1, \
        "按账号的那本被按 IP 的那本灌满了：撞键"
    assert buckets(a) == 1 and buckets(b) == 1, \
        "键的分隔形状不对：SCAN 的 `reset_fails:*` 会顺路把 `reset_fails_by_account` 那本也扫进来"


def test_a_missing_redis_package_fails_loudly_rather_than_falling_back(monkeypatch):
    """`redis` 是可选依赖（不落生产、不进冻结包），但"没装"不许变成"那就用内存"。

    这条与上一条同一个方向：环境变量已经明说了要共享账本，包却不在，静默降级就是把
    阈值乘以进程数并且没有任何症状。判据：把 except ImportError 里的 raise 改成
    `return MemoryLedgers()`，本用例红。
    """
    import sys

    monkeypatch.setenv("FENVER_RATE_LIMIT_LEDGER", "redis")
    monkeypatch.setenv("FENVER_RATE_LIMIT_REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setitem(sys.modules, "redis", None)   # import redis -> ImportError
    with pytest.raises(RuntimeError):
        ledger_backend.select_backend()


def test_an_unnamed_ledger_is_refused_by_the_shared_backend():
    """没登记名字的账本不许进共享存储：拼得出的键会跟别的账本撞。"""
    backend = _redis_backend()[0]
    with pytest.raises(ValueError):
        backend.note({}, "ip", WINDOW, 1.0)


def test_every_ledger_used_by_the_router_is_named_and_registered():
    """`_LEDGERS` 是唯一清单，这一条同时钉"有哪几本"和"每本都有后端认得的名字"。

    新加一本忘了登记 = `_window_of` 当场抛（记不进去），忘了起名 = 共享侧拼不出键。
    两个方向都要在导入期就能被这条看见，而不是等线上第一次撞上限。
    """
    assert len(auth_router._LEDGERS) >= 8
    for ledger, window in auth_router._LEDGERS:
        assert window > 0, f"账本 {getattr(ledger, 'name', '?')} 的窗口不是正数"
        assert getattr(ledger, "name", ""), "这本账没名字：它在共享存储里无处安放"
    names = [ledger.name for ledger, _ in auth_router._LEDGERS]
    assert len(set(names)) == len(names), f"账本名重复，共享存储里会撞键：{names}"


def test_the_router_touches_ledgers_only_through_the_backend():
    """路由层不许再直接往账本里 append：那会绕过窗口（共享侧的 TTL）与后端选择。

    读侧同理：`_recent` 是唯一入口。这条是结构锁，盯的是"第二个写入口又长出来"。
    """
    src = (REPO_ROOT / "backend" / "app" / "core" / "auth_router.py").read_text(encoding="utf-8")
    body = re.sub(r"#.*", "", src)
    for name in ("_FAILS", "_REGISTERS", "_RESETS", "_RESET_FAILS",
                 "_RESET_FAILS_BY_ACCOUNT", "_CHATS", "_REAUTH_FAILS", "_REAUTH_BY_SECRET"):
        assert not re.search(rf"{name}\[[^\]]*\]\s*\.\s*append\(", body), \
            f"{name} 又被直接 append：那一笔记载不进共享账本"
    assert "_LEDGER_BACKEND.note(" in body and "_LEDGER_BACKEND.recent(" in body, \
        "写与读没走后端：这一层抽象就是空的"


def test_the_dockerfile_warns_before_anyone_adds_workers():
    """`--workers` 那句警告必须在 CMD 旁边（判据 D1-2，原文措辞）。

    以前 `grep workers Dockerfile` 零命中：唯一能让限流悄悄放宽一倍的开关，写在镜像里却
    没有任何一句提醒。这条锁盯的是那句提醒本身。
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    idx = text.index('CMD ["uvicorn"')
    block = text[max(0, idx - 600):idx]
    assert "--workers" in block and "限流账" in block and "共享存储" in block, \
        "CMD 上方没有那句警告：下一个人加 --workers 时看不见这台放大器"
    assert "FENVER_RATE_LIMIT_LEDGER" not in text, \
        "镜像里就设了共享账本开关：PRD 明写 Redis 实现不落生产"


# ---------- 5. 路由层只经后端动账（接线 + 端到端同一条门槛） ----------

def test_the_router_routes_every_ledger_write_through_one_helper():
    """八个写账点全部走 `_note`，模块里不许再出现 `X[key].append(...)` 那种直接写。

    为什么只钉写侧：直接 append 在内存后端上**照样工作**（症状为零），但那一笔永远进不了
    共享存储——于是"迁到共享账本"这件事会变成只有读侧搬了、写侧各留一半。
    """
    src = (REPO_ROOT / "backend" / "app" / "core" / "auth_router.py").read_text(encoding="utf-8")
    body = re.sub(r"#.*", "", src)
    assert not re.search(r"_[A-Z_]+\[[^\]]*\]\.append\(", body), \
        "还有一处在直接往账本里 append：那一笔不会进共享存储"
    assert body.count("_note(") >= 8, "写账点没收拢到一条通路：八个入口各有各的窗口抄法"


def test_the_router_functions_are_still_the_single_door_for_reads():
    """`_recent` 与 `_prune` 各自只转一次发：判据留在路由层，存储动作留在后端。

    反向也要：`_throttled` 那几家不许绕过 `_recent` 直接读后端，否则窗口口径会分两处漂移。
    """
    src = (REPO_ROOT / "backend" / "app" / "core" / "auth_router.py").read_text(encoding="utf-8")
    assert "_LEDGER_BACKEND.recent(ledger, ip, window, moment)" in src
    assert "_LEDGER_BACKEND.prune(_LEDGERS, moment, MAX_TRACKED_SOURCES)" in src
    assert src.count("_LEDGER_BACKEND.recent(") == 1, \
        "读侧长出第二个后端调用点：窗口与裁剪的口径就有了两份"


@pytest.mark.parametrize("kind", BACKENDS)
def test_the_http_gate_opens_on_the_same_attempt_whatever_is_behind_it(kind, monkeypatch):
    """端到端：换后端不改判据——第十次坏码仍然放行，第十一次才 429。

    前两条测的是后端的算术，这一条测的是"路由层接上去之后算术还是不是那个数"：
    窗口、上限、键的来源（CF-Connecting-IP）都从端点那侧走一遍。
    """
    from tests.conftest import RECOVERY_FIELDS, peer_client

    backend, buckets, entries = CASES[kind]()
    monkeypatch.setattr(auth_router, "_LEDGER_BACKEND", backend)
    client = peer_client(("127.0.0.1", 54321))
    ip = "192.0.2.77"
    # 两条参数各用一个名字：撞上一条 409 就等于先烧掉一格失败预算，
    # 那条"第 limit + 1 次才 429"的门槛会被自己挪早一格。
    who = f"共享账本的人-{kind}"
    res = client.post("/v1/auth/register",
                      json={"username": who, "password": "correct-horse-battery",
                            **RECOVERY_FIELDS},
                      headers={"CF-Connecting-IP": ip})
    assert res.status_code == 200, res.text
    limit = auth_router.MAX_FAILURES_PER_WINDOW
    codes = []
    for _ in range(limit):
        codes.append(client.post("/v1/auth/login",
                                 json={"username": who, "password": "错的口令"},
                                 headers={"CF-Connecting-IP": ip}).status_code)
    assert codes == [401] * limit, f"预算内就该一直 401：{set(codes)}"
    last = client.post("/v1/auth/login",
                       json={"username": who, "password": "错的口令"},
                       headers={"CF-Connecting-IP": ip})
    assert last.status_code == 429, f"{kind} 后端在第 {limit + 1} 次没挡住：门槛随实现漂移了"
    assert entries(auth_router._FAILS, ip) >= limit, \
        f"{kind} 后端记账的键不是 CF-Connecting-IP 那一个来源"
