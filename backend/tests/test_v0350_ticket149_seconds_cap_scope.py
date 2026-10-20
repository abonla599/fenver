r"""#149：秒上限只归**秒三格**管，计数格不因为"大过八千年"被判非法。

`MAX_WINDOW_SECONDS` 的由来是 `waiting_since + timedelta(seconds=...)` 装不下——它是
**时长**的上界。`_type_ok` / `_sanitize_loaded` / `_finite_or_close` 曾经不分格地把它
套在每一个数值格上，于是 `credit_shadow_daily_limit=1e12`（一个每日积分额度，不是
时长）在盘上被拒收、在 env 上被折回默认 0——一次把护栏**关掉**的静默改值，正是这张
票说的范畴错误。修法不是把线放宽，而是按格判：秒三格（宽限期、审批票据有效期、停点
作废窗口）继续受这条线管，计数格只受"有限且 float 装得下"约束。

本文件钉三件事：
1. 计数格 `credit_shadow_daily_limit=1e12` 经**盘**（`restore()` / `apply_update()`）
   与 **env** 两路都原样活下来，不拒收、不折零；
2. 秒三格的上界行为没变：`10**13 / 10**14 / 10**18` 仍然折回各自那一格的默认
   （停点窗口那一格就是 0.0，既有 #35 用例的判据）；
3. **反向控制**：把秒格的"超额"判据去掉、或把名单改成空/改成不分格，第 2 类判据与
   下面 `_finite_or_close` 的按格对照必红——这条锁有能力报红，不是恒绿。

──────── 射程（不许说得更宽）────────
- 判的是 `config_store` 里这批顶层数值格的形状关、写缓存消毒与 getter 出口；HTTP 层
  不在射程（`apply_update` 的返回值就是管理端响应体，形状等价）。
- 计数格只钉 `1e12`（票面值）与它附近的合法额度；"额度该配多大"是产品口径，不是
  本文件能判的事，所以只断言"不许因为秒线被拒/折零"，不发明一个更小的业务上界。
- 秒格的窄带（值在上限内、起点太晚时封顶到 `datetime.max`）由既有 A5 文件守着，
  这里不重复。
"""
import json
import math

import pytest

from app.core import config_store

SECONDS_KEYS = (
    "stream_no_reader_grace_seconds",
    "approval_ticket_ttl_seconds",
    "task_waiting_user_timeout_seconds",
)
SHADOW_KEY = "credit_shadow_daily_limit"

ENV_BY_KEY = {
    "stream_no_reader_grace_seconds": "STREAM_NO_READER_GRACE_SECONDS",
    "approval_ticket_ttl_seconds": "APPROVAL_TICKET_TTL_SECONDS",
    "task_waiting_user_timeout_seconds": "TASK_WAITING_USER_TIMEOUT_SECONDS",
    SHADOW_KEY: "CREDIT_SHADOW_DAILY_LIMIT",
}
GETTERS = {
    "stream_no_reader_grace_seconds": config_store.stream_no_reader_grace_seconds,
    "approval_ticket_ttl_seconds": config_store.approval_ticket_ttl_seconds,
    "task_waiting_user_timeout_seconds": config_store.task_waiting_user_timeout_seconds,
    SHADOW_KEY: config_store.credit_shadow_daily_limit,
}

# 每天 1e12 积分：远大于 0.0 的默认，也远大于秒线（≈3.16e11），但在 float 的
# 精确整数范围内——它必须是"配得出来的额度"，不是"很长的窗口"。
SHADOW_AMOUNT = 10 ** 12
# 票面那三个超线值：对秒格来说是 datetime 装不下的秒数，对计数格只是普通的大数。
OVERRANGE = (10 ** 13, 10 ** 14, 10 ** 18)


@pytest.fixture(autouse=True)
def clean_config(tmp_path, monkeypatch):
    """每个用例自带一份 config.json，跑完把进程级缓存原样还回去。"""
    for name in ENV_BY_KEY.values():
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.json"
    monkeypatch.setenv("CONFIG_DB_PATH", str(path))
    prev = dict(config_store._config)
    config_store.restore()
    yield path
    with config_store._lock:
        config_store._config.clear()
        config_store._config.update(prev)


def _write_config(path, values):
    """照落盘放大器（`atomic_write` 的默认 allow_nan=True）的写法。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(values, f, ensure_ascii=False)


# ---------- 名单本身就是判据：秒格只有这三格 ----------

def test_秒格名单恰好是那三格且计数格不在其中():
    """加一个"秒"配置格会红在这里：不是拦你，是逼你确认它真的会被送进 timedelta。

    `credit_shadow_daily_limit` 不在名单里这件事本身要被钉死——把名单写成"所有
    数值格"（修法前的形状）时，最后一句红，而上一类计数格用例也会全红。
    """
    assert config_store._SECONDS_KEYS == frozenset(SECONDS_KEYS)
    assert SHADOW_KEY not in config_store._SECONDS_KEYS
    assert set(SECONDS_KEYS) <= set(config_store.DEFAULTS)


# ---------- 计数格：盘 / 管理端 / env 三路都原样活下来 ----------

@pytest.mark.parametrize("value", (SHADOW_AMOUNT, float(SHADOW_AMOUNT)),
                         ids=["int-1e12", "float-1e12"])
def test_计数格经盘读回来原样活下来(value, clean_config):
    """盘上写 1e12 → `restore()` → `read()` 与 getter 都还是 1e12。

    负向锁（修法前实跑）：`_type_ok` 不分格地拒收超秒线的数，`restore()` 打一行
    "类型或数值范围不对"就按默认 0 跑——这条红在"盘子里的额度被折成 0"。
    """
    _write_config(clean_config, {SHADOW_KEY: value})
    config_store.restore()
    cfg = config_store.read()
    assert cfg[SHADOW_KEY] == SHADOW_AMOUNT, (
        f"盘上的额度 {value!r} 被写缓存层改成了 {cfg[SHADOW_KEY]!r}")
    assert config_store.credit_shadow_daily_limit() == float(SHADOW_AMOUNT), (
        "getter 把配得出来的额度折成了默认值——护栏被静默关掉")
    json.dumps(cfg, allow_nan=False)


def test_计数格经管理端写进来不被拒也不折零(clean_config):
    """`POST /v1/admin/config` 那条路：`rejected` 里不许出现它，响应与落盘都是 1e12。

    负向锁：`apply_update` 里 `_type_ok` 一旦重新不分格，这条红在
    `rejected == []` 上（管理端会说"这一键没被接受"）。
    """
    out, rejected = config_store.apply_update({SHADOW_KEY: float(SHADOW_AMOUNT)})
    assert rejected == [], f"合法额度被形状关拒了：{rejected!r}"
    assert out[SHADOW_KEY] == float(SHADOW_AMOUNT), (
        f"管理端响应把额度折成了 {out[SHADOW_KEY]!r}")
    json.dumps(out, allow_nan=False)
    with open(clean_config, encoding="utf-8") as f:
        disk = json.load(f)
    assert disk[SHADOW_KEY] == float(SHADOW_AMOUNT), f"落盘被改写：{disk[SHADOW_KEY]!r}"
    config_store.restore()
    assert config_store.credit_shadow_daily_limit() == float(SHADOW_AMOUNT)


@pytest.mark.parametrize("text", (str(SHADOW_AMOUNT), "1e12"),
                         ids=["plain-digits", "exponent"])
def test_计数格的env臂也原样穿给读数口径(text, monkeypatch):
    """env 臂不经过形状关/消毒层，只在 getter 出口判——那条判据也必须按格。

    负向锁：`_finite_or_close` 里那次上界判断若不分格，这条红在 `== 1e12` 上
    （实得 0.0，护栏默认关）。
    """
    monkeypatch.setenv(ENV_BY_KEY[SHADOW_KEY], text)
    assert config_store.credit_shadow_daily_limit() == float(SHADOW_AMOUNT)


# ---------- 反向控制：秒格的上界还在，而且只对秒格生效 ----------

@pytest.mark.parametrize("key", SECONDS_KEYS, ids=list(SECONDS_KEYS))
@pytest.mark.parametrize("value", OVERRANGE, ids=[str(v) for v in OVERRANGE])
def test_秒格_env超线值仍然折回自己那一格的默认(key, value, monkeypatch):
    """票面判据：`10**13 / 10**14 / 10**18` 对秒格仍然到不了下游。

    折回档按 `DEFAULTS` 现取（宽限期 120、TTL 60、作废窗口 0），不抄字面量。
    停点窗口那一格就是票面说的 0.0。负向锁：摘掉 `_finite_or_close` 的秒格上界，
    三格六行全部红——返回的是 1e13/1e14/1e18 而不是该格默认。
    """
    monkeypatch.setenv(ENV_BY_KEY[key], str(value))
    got = GETTERS[key]()
    assert got == float(config_store.DEFAULTS[key]), (
        f"{key} 的 env {value} 没折回该格默认 {config_store.DEFAULTS[key]!r}，实得 {got!r}")


@pytest.mark.parametrize("value", OVERRANGE, ids=[str(v) for v in OVERRANGE])
def test_停点窗口的票面三值仍然是0点0(value, monkeypatch):
    """票面原话单独钉一遍：这三个数落在**停点作废窗口**上必须是 0.0（关）。"""
    monkeypatch.setenv("TASK_WAITING_USER_TIMEOUT_SECONDS", str(value))
    assert config_store.task_waiting_user_timeout_seconds() == 0.0


@pytest.mark.parametrize("key", SECONDS_KEYS, ids=list(SECONDS_KEYS))
def test_秒格的形状关仍然拒收超线值(key):
    """写缓存前那一关也不能漏：`_type_ok` 一旦不分格，这里红。

    两参探针（不传 key）按秒格收的历史口径另有既有 #35 用例守着，这里只钉点名形式。
    """
    for value in OVERRANGE:
        assert config_store._type_ok(value, config_store.DEFAULTS[key], key=key) is False
    assert config_store._type_ok(SHADOW_AMOUNT, config_store.DEFAULTS[SHADOW_KEY],
                                 key=SHADOW_KEY) is True, (
        "计数格被形状关按秒线拒了——这正是 #149 要拆掉的范畴错误")


def test_同一个超线值按格判出两种结果():
    """最小对照：同一个数，key 是秒格 → 折回；key 是计数格 → 原样。

    这是反向控制的核心：把 `_seconds_limited` 改成恒 False（等于摘掉秒上限）或恒
    True（等于回到修法前的不分格），这条各有一半红；名单清空时上一类用例红。
    """
    edge = config_store.MAX_WINDOW_SECONDS
    above = edge + 1.0
    for key in SECONDS_KEYS:
        assert config_store._finite_or_close(above, 7.5, "判据", key=key) == 7.5, (
            f"{key} 是秒格，超线值 {above!r} 却没折回")
    assert config_store._finite_or_close(above, 7.5, "判据", key=SHADOW_KEY) == above, (
        "计数格被秒线折回了——范畴错误又回来了")


def test_前提自证_秒线真被放宽时同一批值会穿出去(monkeypatch):
    """证明上面那些断言不是恒真：把常量放宽到 inf，1e13 立刻穿到出口。

    这一条只作前提自证（它本身在修法前后都绿）；上面按格用例的"红的能力"由上面
    各自的负向锁说明。
    """
    monkeypatch.setattr(config_store, "MAX_WINDOW_SECONDS", math.inf)
    monkeypatch.setenv("STREAM_NO_READER_GRACE_SECONDS", str(OVERRANGE[0]))
    assert config_store.stream_no_reader_grace_seconds() == float(OVERRANGE[0])
