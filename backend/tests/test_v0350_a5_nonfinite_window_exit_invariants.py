r"""A5 出口不变量：**非有限值不许从配置读数口径穿出去**，尤其不许穿进 JSON 响应。

这把尺子量的是「付费端点被打成 500」那一条真链路：`data/config.json` 里一个 `Infinity`
（或一颗环境变量）→ `config_store` 的 getter → `task_store.waiting_timeout_info()` →
详情端点的那份 dict → `starlette` 用 `allow_nan=False` 序列化 → 当场抛。
它长的样子不是"算错了个窗口"，是一个 500；而同一枚值在别的格子里更坏：票据窗口拿到
`inf` 就是**永不过期**，钱闸拿到 `inf` 就是**永不落**。

⭐ 为什么还要再写一个文件（既有那把窗口判据的锁已经存在）：`tests/
test_v0350_a5_timeout_is_suspension.py` 里"窗口坏值"那一族**只喂显式参数**
（`is_waiting_timed_out(..., timeout_seconds=...)`），走的是 `_window_seconds` 自己那
道 `isfinite`。环境变量与 `data/config.json` 那两路读数口径它一格没碰，
`_window_seconds(None)` 那一支也没有别的锁——这条文件补的就是这两路。所以本文件里
走 env 与 config 两路的停点窗口用例都**刻意不传 `timeout_seconds`**：传了就退化成既有
那把尺子已经量过的东西。唯一一条显式传参的是
`test_停点窗口吃到400位整数时不抛`，它量的不是窗口语义，而是既有那把尺子没量过的
异常类型（`10**400` 的 OverflowError），两个文件合起来仍不留同一个盲区。

判的是**不变量**，不是某个期望值：`math.isfinite(v) and v >= 0`，外加把 `v` 灌进
响应之后 `json.dumps(..., allow_nan=False)` 不抛。只有"折回哪一档"那一格按各格自己的
默认值断言，且参照的是 `config_store.DEFAULTS` 本身而不是抄下来的字面量。

落盘放大器是真实存在的：`core/atomic_write.py` 的 `json.dump` 用默认 `allow_nan=True`，
所以手改一次 `Infinity` 能写进盘、重启之后还在。本文件喂 config 那一路就是照这个写法
造的文件（`_write_config`），不是假设。

──────── 射程（不许说得更宽）────────
- 数值 getter 的 env 臂与 config 臂，射程就是下面的 `CELLS` 表：宽限期、审批票据
  有效期、停点作废窗口、影子护栏。表里新增一行，这批判据自动跟着扩。基准价（`credit_benchmark`）单独一条，它的坏值落点是**逐键各自的默认**，
  不是某个统一数。
- `restore()` / `apply_update()` **写缓存之前**的消毒：盘上（或管理端 JSON 里）的
  `Infinity` / `NaN` / `-Infinity` 收进来之前就折回该格默认，判据是把 `read()` 整份
  交给 `json.dumps(..., allow_nan=False)`——`GET /v1/config` 与 `POST /v1/admin/config`
  的响应形状就是这一份字典。`credit_benchmark` 按**整棵子字典**收：已知数值键折回
  各自的默认、`currency` 类型不对回默认、额外键只要含非有限值或超出 float 范围的数
  （含嵌套容器里的）就丢掉那一键。
- 超出 float 范围的整数（`10**400`），以及**超出 datetime 可用范围**的数值
  （`10**13` 以上；边界是 `config_store.MAX_WINDOW_SECONDS`，取
  `timedelta` 天数上限与 datetime 日期差里更紧的那个）：形状关拒收、导入期子进程、
  `credit_benchmark` 的 `float()`、`_window_seconds` 的显式参数与现读两支。
- `task_store._window_seconds` 的 `None` 那一支（现读那一条），以及一个故意绕开配置
  出口的假读数口径（验的是"就算上游漏了，这一层也拦得住"）。
- 形状只有这些：`("inf","Inf","Infinity","+inf","1e400","  inf ","nan","-inf","","五分钟")`
  喂 env 臂；`(inf, -inf, nan)` 经 `restore()` 喂 config 臂；同一批字符串另走一支
  绕过 `restore()` 的直写用例；`10**400` 走盘、基准价、管理端与导入期这些通道；
  `10**13` / `10**14` / `10**18` / `10**18+1` 走盘与形状关，`1e18` 走 env 臂；
  `credit_benchmark` 的 `currency=Infinity`、`junk=Infinity`、`junk=NaN`、
  `junk=10**400`、`junk=[Infinity]` 各走 `restore()` 与 `apply_update()` 两条路。

──────── 仍然漏掉的形状（本文件不声称覆盖）────────
- 直写 `config_store._config` 的路径只有 getter 层那一条锁（`test_绕过形状关直写内存态时
  读数口径自己也拦得住`）：`read()` 不叠第二层消毒，所以同一种坏值被直写进缓存之后，
  `read()` 仍会把它交出去。现网写入口只有 `restore()` 与 `apply_update()` 两处（都在
  写缓存前消毒，见上面射程），"以后新增的第三条绕过它们的写入口"不在本文件射程。
- config 臂上 `""` 与 `"五分钟"` **到不了 `_finite_or_close` 的 `float()`**：它们在
  `restore()` 的形状关（`_type_ok`）就被拦下、按默认跑。字符串真被送到数值转换面前
  只有一次，就是那条直写用例，它模拟的是"上游形状关被跳过"，不是现网通道。
- 下游一格没动，也不由本文件验：`stream_runs.py` 那道钱闸与 `core/approval.py` 的
  票据窗口，各自的判据在它们自己的文件里。**出口折回什么值，它们就按什么值跑**——
  所以"宽限期与 TTL 的坏值折回默认窗口而不是 0"这一格的实际后果在下游，不在这把尺子
  里；要验那两格的真行为，得由持有那两个文件的票接手。
- 不经这些 getter 的读数（直接 `os.getenv`、或拿 `read()` 里的原始值）不在射程。
- HTTP 层不在射程：这里验的是 `read()` / `waiting_timeout_info()` 交出来的字典能否过
  `allow_nan=False`——那正是响应序列化唯一会抛的地方；把端点接进 `TestClient` 只会给
  这条不变量多两个失败原因（鉴权、路由）。
- 「不变量」那一族只看得到"出口干不干净"：`nan` 与 `-inf` 都被折回**该格自己的默认**
  （实跑：宽限期 `NaN → 120.0`、审批 TTL `-Infinity → 60.0`），干净值，所以这一族
  不红。折回哪一档由"落点必须是该格默认"那句方向断言负责（env 臂与 config 臂各有
  一条）。一种真看不出差别的情形要如实说：默认本来就是 0 的那两格（作废窗口、影子
  护栏），坏值折回默认就是 0——那里 0 就是它们自己要的那一档，不是被拧过去的。
- 真链路那条（不经显式参数）只在**两层同时**漏收时才红——两层互为纵深，一把尺子量不到
  第二层，第二层的锁是 `test_读数口径本身漏收时…`。
"""
import datetime
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

import app.agents.task_store as ts
from app.core import config_store

BACKEND_DIR = Path(__file__).resolve().parent.parent

# (格名, config.json 的键, 环境变量名, 读数口径)。收的都是"数值 + 非负 + env/config 两路"
# 这一族；每条判据都对表里的每一行跑一遍，以后新增一行就自动进射程。
CELLS = (
    ("宽限期", "stream_no_reader_grace_seconds", "STREAM_NO_READER_GRACE_SECONDS",
     lambda: config_store.stream_no_reader_grace_seconds()),
    ("审批票据有效期", "approval_ticket_ttl_seconds", "APPROVAL_TICKET_TTL_SECONDS",
     lambda: config_store.approval_ticket_ttl_seconds()),
    ("停点作废窗口", "task_waiting_user_timeout_seconds", "TASK_WAITING_USER_TIMEOUT_SECONDS",
     lambda: config_store.task_waiting_user_timeout_seconds()),
    ("影子护栏", "credit_shadow_daily_limit", "CREDIT_SHADOW_DAILY_LIMIT",
     lambda: config_store.credit_shadow_daily_limit()),
)
ENV_NAMES = [cell[2] for cell in CELLS]

# 矩阵一字不加：`""` 走的是「env 没设」那一条路（落到 config 臂），`"五分钟"` 走的是
# 「不是数」那一条路；两者都算这一族，因为 500 的入口不该只有一种拼法。
BAD_ENV_TEXTS = ("inf", "Inf", "Infinity", "+inf", "1e400", "  inf ",
                 "nan", "-inf", "", "五分钟")
# 能真的躺在 config.json 上的非有限数字：`Infinity` / `-Infinity` / `NaN` 正是
# `allow_nan=True` 那次落盘写出来的东西。
BAD_CONFIG_NUMBERS = (float("inf"), float("-inf"), float("nan"))
BAD_CONFIG_STRINGS = ("", "五分钟")


def _write_config(path: Path, values: dict) -> None:
    """照放大器的原样写法落盘：默认 `allow_nan=True`，inf 在盘上就是 `Infinity`。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(values, f, ensure_ascii=False)


def _assert_gate_output(value, cell_name: str) -> None:
    """出口不变量：有限、非负，而且进得了 JSON 响应。"""
    assert isinstance(value, float), f"{cell_name} 的读数口径不再返回浮点：{value!r}"
    assert math.isfinite(value), (
        f"{cell_name} 把非有限值 {value!r} 吐出了出口——下游把它当窗口用就是永不过期，"
        f"当数字进响应就是序列化当场抛")
    assert value >= 0, f"{cell_name} 吐出负窗口：{value!r}"
    try:
        json.dumps({"waiting_timeout_seconds": value}, allow_nan=False)
    except ValueError as exc:
        pytest.fail(f"{cell_name} 的值 {value!r} 进不了 allow_nan=False 的响应：{exc}")


def _parked_task():
    task = ts.Task(goal="判据", subtasks=["a", "b"], user_id="u-nonfinite")
    ts.task_store[task.task_id] = task
    task.set_status(ts.TaskStatus.RUNNING)
    task.wait_for_user("请输入短信验证码")
    return task


@pytest.fixture(autouse=True)
def no_stale_knobs(monkeypatch):
    """表里每一支的旋钮进出都不留值，进程级内存态也原样还回去。

    `config_store._config` 是**进程级单例**：漏了还原，下一格（乃至下一个文件）就在一
    个没人配过的窗口上跑，红点离真凶隔一个文件。
    """
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    prev = dict(config_store._config)
    yield
    with config_store._lock:
        config_store._config.clear()
        config_store._config.update(prev)
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def isolated_tasks(tmp_path, monkeypatch):
    """任务表指在自己的文件里，用例后清进程级存储（与既有 A5 那份夹具同口径）。"""
    path = tmp_path / "tasks.json"
    monkeypatch.setenv("TASKS_DB_PATH", str(path))
    ts.restore(path=str(path))
    yield path
    for tid in list(ts.task_store):
        del ts.task_store[tid]


@pytest.fixture
def config_channel(tmp_path, monkeypatch):
    """把某一格的值经**盘**送进去：写文件 → `restore()` → 现读那支 getter。"""
    def feed(key, value, read):
        path = tmp_path / "config.json"
        _write_config(path, {key: value})
        monkeypatch.setenv("CONFIG_DB_PATH", str(path))
        for name in ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        config_store.restore()
        return read()
    return feed


# ---------- env 臂 ----------

@pytest.mark.parametrize("cell", CELLS, ids=[c[0] for c in CELLS])
@pytest.mark.parametrize("text", BAD_ENV_TEXTS, ids=[repr(t) for t in BAD_ENV_TEXTS])
def test_环境变量臂的任何坏形状都吐不出非有限值(cell, text, monkeypatch):
    """"不经显式参数"的第一条路：只设环境变量，然后现读。

    负向锁：把这一支出口上的非有限判据摘回 `max(0.0, float(raw))`，`inf`、`Inf`、
    `Infinity`、`+inf`、`1e400`、`"  inf "` 这六行会红在「吐出了非有限值」那一句。
    ⚠️ 同一摘法下 `nan` 与 `-inf` 两行**不红**——它们都被 `max` 折成 0.0，而 0.0 也满足
    这里这条不变量。"悄悄变 0" 由 `test_坏值折回的是自己那一格的默认…` 那一族负责；
    只看这一条就宣布收口验完了，是这把尺子的盲区。
    """
    _, _, env_name, read = cell
    monkeypatch.setenv(env_name, text)
    _assert_gate_output(read(), cell[0])


@pytest.mark.parametrize("cell", CELLS, ids=[c[0] for c in CELLS])
def test_环境变量臂的合法正数照旧原样穿过(cell, monkeypatch):
    """阳性对照：收坏值不许顺手把配得出来的好值也收了。

    负向锁：把出口改成恒返回默认值（"反正坏值都折回默认"），这一条立刻红。
    """
    _, _, env_name, read = cell
    monkeypatch.setenv(env_name, "3600")
    assert read() == 3600.0, "配了 3600 却没读到 3600——出口在替运维做决定"


# ---------- config 臂（走盘，走 restore）----------

@pytest.mark.parametrize("cell", CELLS, ids=[c[0] for c in CELLS])
@pytest.mark.parametrize("number", BAD_CONFIG_NUMBERS, ids=["inf", "-inf", "nan"])
def test_配置臂的非有限数字经盘读回来也吐不出去(cell, number, config_channel):
    """放大器那条真通道：手改一次 `Infinity` 落盘、重启还在——重启后读回来必须仍干净。

    负向锁（实跑）：把这一支出口摘回 `max(0.0, float(value))`，`inf` 那一行红在
    「非有限值」。`-inf` 与 `nan` 两行红的是**下面那句方向断言**——它们被 `max` 折成
    0.0，而 0.0 在宽限期与 TTL 两格里是"不留窗口 / 每次现批"（最严档），运维一个字
    都没配过；只有默认本来就是 0 的那两格（作废窗口、影子护栏）才看不出区别，
    因为那里 0 就是它们自己要的档。
    """
    name, key, _, read = cell
    got = config_channel(key, number, read)
    _assert_gate_output(got, name)
    assert got == float(config_store.DEFAULTS[key]), (
        f"{name} 的 config 臂把坏值折成了 {got!r}，而它自己那一格的默认是 "
        f"{config_store.DEFAULTS[key]!r}——0 不是全文件通用的兜底")


@pytest.mark.parametrize("cell", CELLS, ids=[c[0] for c in CELLS])
@pytest.mark.parametrize("text", BAD_CONFIG_STRINGS, ids=[repr(t) for t in BAD_CONFIG_STRINGS])
def test_配置臂的字符串形状被形状关拦下也照样吐得出干净值(cell, text, config_channel):
    """这一条钉的是"整条 config 通道的出口一定是干净值"，不是"字符串喂进了 getter"。

    `restore()` 的 `_type_ok` 会先把字符串拦下（见文件头「仍然漏掉的形状」第一条），
    把字符串真的送到 `float()` 面前，是下面那条绕过 `restore()` 的直写用例在做的事。
    """
    name, key, _, read = cell
    _assert_gate_output(config_channel(key, text, read), name)


@pytest.mark.parametrize("cell", CELLS, ids=[c[0] for c in CELLS])
@pytest.mark.parametrize("text", BAD_ENV_TEXTS, ids=[repr(t) for t in BAD_ENV_TEXTS])
def test_绕过形状关直写内存态时读数口径自己也拦得住(cell, text, monkeypatch):
    """纵深防御：`_config` 被一条跳过 `restore()` 的路径写脏时，getter 照旧收口。

    现网两条写入路径（`restore()` 与 `apply_update()`）都有 `_type_ok`，所以这一支
    模拟的不是运维能走到的路，而是"以后有人新加一条不判形状的写入口"。摘掉 getter
    里那次 try/except、或那次非有限判据，这里都会红。
    """
    name, key, _, read = cell
    for other in ENV_NAMES:
        monkeypatch.delenv(other, raising=False)
    with config_store._lock:
        prev = config_store._config.get(key)
        config_store._config[key] = text
    try:
        _assert_gate_output(read(), name)
    finally:
        with config_store._lock:
            if prev is None:
                config_store._config.pop(key, None)
            else:
                config_store._config[key] = prev


# ---------- 折回的是"该格自己的那一档"，不是一个统一的 0 ----------

@pytest.mark.parametrize("cell", CELLS, ids=[c[0] for c in CELLS])
@pytest.mark.parametrize("text", ("inf", "-inf", "nan"), ids=["inf", "-inf", "nan"])
def test_坏值折回的是自己那一格的默认而不是全文件统一的数(cell, text, monkeypatch):
    """0 在这一族里**不是同一个方向**：作废窗口与影子护栏的 0 是"关"，宽限期与审批
    TTL 的 0 是"不留窗口 / 每次现批"（最严档）。拿后者去兜坏值，等于凭空执行一次运维
    从来没做过的收紧——用户那边表现为票据一弹就得立刻点、流式一断线就没人接。

    参照的是 `DEFAULTS` 本身：默认改了，两头一起动，不存在第二份真相。
    `nan` 与 `-inf` 这两行是上面两族不变量用例接不住的：坏值折回该格默认（或即便被
    `max` 折成 0.0）都是"干净值"，这一族才问得出"0 是不是那一格想要的 0"。
    负向锁：把出口改成恒 `return 0.0`，只有宽限期与审批票据有效期这两格红（它们的
    默认不是 0），另两格照旧绿——那两格的默认本来就是关。
    """
    name, key, env_name, read = cell
    monkeypatch.setenv(env_name, text)
    default = float(config_store.DEFAULTS[key])
    got = read()
    assert got == default, (
        f"{name} 的坏值 {text} 没折回它自己那一格的默认（期望 {default!r}，实得 {got!r}）；"
        f"这一格的兜底档由 DEFAULTS 说了算，不许由收口那一行说了算")
    if default > 0:
        # 只有"默认不是关"的那几格才谈得上方向反了：0 对它们是策略，不是兜底。
        assert got != 0.0, f"{name} 的坏值被兜成了 0，而这一格的 0 是「最严」不是「关」"


# ---------- 真链路：不经显式参数，一路灌进响应 ----------

@pytest.mark.parametrize("number", BAD_CONFIG_NUMBERS, ids=["inf", "-inf", "nan"])
def test_停点窗口经盘喂进来时详情响应照样序列化得出来(number, isolated_tasks,
                                                       config_channel):
    """⭐ 这条就是那个 500 的形状，而且**一个显式参数都没喂**。

    `waiting_timeout_info(task)` 走的是 `_window_seconds(None)` → 现读 config_store；
    它出来的那份 dict 正是详情端点 `response.update(...)` 塞进去的内容。既有那把窗口
    锁只喂 `timeout_seconds=`，这条路一直是盲区。

    负向锁按实跑写，不按"应该都会红"写：只把 `config_store` 那一支出口摘回
    `max(0.0, float(value))`，这条**不红**——`_window_seconds` 的判据在第二层接着；
    两层同时摘掉（再把 `None` 那一支改回提前 return）它才红，红话是
    「响应键里的窗口是 inf」。两层各自另有独立的锁：出口层是上面那条 config 用例，
    第二层是 `test_读数口径本身漏收时窗口函数仍然不把非有限值送进响应`。
    """
    config_channel("task_waiting_user_timeout_seconds", number,
                   config_store.task_waiting_user_timeout_seconds)
    task = _parked_task()
    info = ts.waiting_timeout_info(task)          # 不传 timeout_seconds：全部现读
    window = info["waiting_timeout_seconds"]
    assert math.isfinite(window) and window >= 0, f"响应键里的窗口是 {window!r}"
    # 前提自证：这条判据有能力报红——inf 进 allow_nan=False 真的会抛。
    with pytest.raises(ValueError):
        json.dumps({"waiting_timeout_seconds": float("inf")}, allow_nan=False)
    json.dumps(info, allow_nan=False)             # 修复在场：这里不许抛


@pytest.mark.parametrize("text", BAD_ENV_TEXTS, ids=[repr(t) for t in BAD_ENV_TEXTS])
def test_停点窗口经环境变量喂进来时详情响应照样序列化得出来(text, isolated_tasks,
                                                             monkeypatch):
    """同一条链路的 env 臂（盘上那一份读不到时，环境变量说了算）。"""
    monkeypatch.setenv("TASK_WAITING_USER_TIMEOUT_SECONDS", text)
    task = _parked_task()
    info = ts.waiting_timeout_info(task)
    window = info["waiting_timeout_seconds"]
    assert math.isfinite(window) and window >= 0, f"响应键里的窗口是 {window!r}"
    json.dumps(info, allow_nan=False)


def test_读数口径本身漏收时窗口函数仍然不把非有限值送进响应(isolated_tasks, monkeypatch):
    """把"配置出口"这一层整个换掉，验 `_window_seconds` 自己那道判据还在。

    假读数口径返回 `inf`，等于模拟"以后新增了一条没收口的读数路径"。这一条过，说明
    响应键那一格不依赖上游唯一那道闸。
    负向锁：把 `None` 那一支改回提前 `return task_waiting_user_timeout_seconds()`，红。
    """
    monkeypatch.setattr(ts, "task_waiting_user_timeout_seconds", lambda: float("inf"))
    task = _parked_task()
    assert ts._window_seconds() == 0.0, "现读那一支没跟显式参数那一支共用同一道判据"
    info = ts.waiting_timeout_info(task)
    assert math.isfinite(info["waiting_timeout_seconds"])
    json.dumps(info, allow_nan=False)
    # 判据方向：坏值折回 0 之后窗口是关着的，就不许有任何一次作废。
    stale = _parked_task()
    stale.waiting_since = (ts._now() - datetime.timedelta(seconds=99999)).isoformat()
    assert ts.is_waiting_timed_out(stale) is False, "坏值被拧成了「到点销毁断点」"


# ---------- 基准价：逐键退回它自己的默认，不是整表重排 ----------

def test_基准价的坏键只折回它那一键的默认其余键照旧():
    """这一格的坏值落点与前面那些 getter 不同：**逐键各自的默认**。

    整包信任会怎样：一个 `Infinity` 的 `input_per_m` 把展示倍率表整张算成鬼话，而
    `float(nan) <= 0` 是 False——`nan` 与 `inf` 都能从"是不是正数"这一关放行到出口。
    负向锁：把这一支的非有限判据摘掉，第一句就红（坏值穿进了 `out`）。
    """
    defaults = config_store.DEFAULTS["credit_benchmark"]
    numeric_keys = ("input_per_m", "output_per_m", "mixed_input_output_ratio",
                    "tokens_per_credit")
    for bad in (float("inf"), float("-inf"), float("nan")):
        for key in numeric_keys:
            raw = dict(defaults)
            raw[key] = bad
            with config_store._lock:
                prev = config_store._config.get("credit_benchmark")
                config_store._config["credit_benchmark"] = raw
            try:
                out = config_store.credit_benchmark()
            finally:
                with config_store._lock:
                    config_store._config["credit_benchmark"] = prev
            for name, value in out.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    assert math.isfinite(value) and value > 0, (
                        f"{key} 灌进 {bad!r} 之后 {name} 成了 {value!r}")
            assert float(out[key]) == float(defaults[key]), (
                f"{key} 没折回它自己的默认")
            for other in numeric_keys:
                if other != key:
                    assert float(out[other]) == float(defaults[other]), (
                        f"一键坏了却动了 {other}：整表重排是另一件事，不是消毒")


# ---------- 导入期：坏值绝对不许把进程拦在 ImportError ----------

@pytest.mark.parametrize("text", ("inf", "nan", "Infinity"))
def test_坏TTL在导入期只降级不抛(text, tmp_path):
    """`core/approval.py` 在**模块层**就取 TTL。这里抛一下，现网就是一次起不来。

    用子进程跑真导入，不在本文件里 mock：要判的是"导入这条路上有没有 raise"，而在本
    进程里 import 过一次就再也证不了。这台机器的守护每小时只有三次重启额度，一次导入
    期异常就是一段没人看着的停机。
    """
    script = ("import app.core.approval as a;"
              "print('OK', a.ticket_ttl_seconds(), a.APPROVAL_TIMEOUT_SECONDS)")
    env = dict(os.environ)
    env["APPROVAL_TICKET_TTL_SECONDS"] = text
    env["DATA_ROOT"] = str(tmp_path)
    env["CONFIG_DB_PATH"] = str(tmp_path / "config.json")
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(BACKEND_DIR),
                          env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"导入期抛了：{proc.stderr[-600:]}"
    # 子进程的 stdout 上先响过的那几行就是降级本身——它是这一条要的证据，不是噪声。
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert any(ln.startswith("OK ") for ln in lines), proc.stdout[-300:]
    assert any("不是有限值" in ln for ln in lines), (
        f"坏 TTL 静悄悄地过了导入期，没人说出来：{proc.stdout[-300:]}")
    report = [ln for ln in lines if ln.startswith("OK ")][-1].split()
    for got in (float(report[1]), float(report[2])):
        assert math.isfinite(got) and got >= 0, f"导入期拿到 {got!r}"


# ---------- BLOCK ①：read() / 管理端响应也不许把非有限值交出去 ----------
#
# 复核员实跑：`restore()` 把盘上的 `Infinity` 收进 `_config`，`read()` 原样返回，
# `GET /v1/config` 直接 `return config_store.read()`，一个 `inf` 就是一次 500。
# 修在**缓存写入层**（`restore()` 与 `apply_update()` 写 `_config` 之前消毒），
# 所以这里判的就是 `read()` 那一份字典本身。

@pytest.mark.parametrize("number", BAD_CONFIG_NUMBERS, ids=["inf", "-inf", "nan"])
def test_盘上每种非有限值经restore后read都过得了allow_nan序列化(number, tmp_path,
                                                              monkeypatch):
    """复核员那句原话的复现：`json.dumps(cs.read(), allow_nan=False)` 不许抛。

    `CELLS` 表里的每一格与基准价的每一个数值键都喂同一个坏值，落点必须仍是各格自己的默认。
    负向锁：把 `restore()` 里 `_sanitize_loaded(key, data[key])` 摘回 `data[key]`，
    每个坏值形状那一行都会红，红话是「restore() 把盘上的非有限值收进了缓存，GET /v1/config 会在这里抛」。
    """
    path = tmp_path / "config.json"
    raw = {key: number for _, key, _, _ in CELLS}
    raw["credit_benchmark"] = {
        "currency": "CNY", "input_per_m": number, "output_per_m": number,
        "mixed_input_output_ratio": number, "tokens_per_credit": number,
    }
    _write_config(path, raw)
    monkeypatch.setenv("CONFIG_DB_PATH", str(path))
    config_store.restore()
    try:
        json.dumps(config_store.read(), allow_nan=False)
    except ValueError as exc:
        pytest.fail(f"restore() 把盘上的非有限值收进了缓存，GET /v1/config 会在这里抛：{exc}")
    cfg = config_store.read()
    for _, key, _, _ in CELLS:
        assert cfg[key] == float(config_store.DEFAULTS[key]), (
            f"{key} 没在写缓存前折回自己那一格的默认：{cfg[key]!r}")
    for sub, default in config_store.DEFAULTS["credit_benchmark"].items():
        if sub == "currency":
            continue
        assert cfg["credit_benchmark"][sub] == default, (
            f"credit_benchmark.{sub} 没折回默认：{cfg['credit_benchmark'][sub]!r}")


@pytest.mark.parametrize("cell", CELLS, ids=[c[0] for c in CELLS])
def test_管理端写入非有限值后的响应也过得了allow_nan序列化(cell, tmp_path, monkeypatch):
    """`apply_update` 的返回值就是 `POST /v1/admin/config` 的响应体，同样原样回给前端。

    真实形状：请求 JSON 里写 `1e400`，Python 的 `json.loads` 把它解析成 `inf`
    （不是拒绝），`_type_ok` 只看形状、会放行。负向锁：把 `apply_update` 里
    `_sanitize_loaded(...)` 摘回 `value`，这一条红在 dumps；落盘那份也会带着
    `Infinity`，下一次启动又回到复核员那条链路。
    """
    _, key, _, _ = cell
    monkeypatch.setenv("CONFIG_DB_PATH", str(tmp_path / "config.json"))
    config_store.restore()
    out, rejected = config_store.apply_update({key: float("inf")})
    assert rejected == [], "形状关收下它了，消毒层就该给出默认值而不是把它拒掉"
    assert out[key] == float(config_store.DEFAULTS[key]), (
        f"{key} 把 inf 原样交给了管理端响应：{out[key]!r}")
    json.dumps(out, allow_nan=False)
    json.dumps(config_store.read(), allow_nan=False)
    with open(tmp_path / "config.json", encoding="utf-8") as f:
        json.dumps(json.load(f), allow_nan=False)


# ---------- BLOCK ②：超出 float 范围的整数 ----------

# `float(HUGE_INT)` 抛 OverflowError（"int too large to convert to float"），
# 而 `json.dump` 写它毫不费力——这就是复核员那个 400 位整数的形状。
HUGE_INT = 10 ** 400


@pytest.mark.parametrize("bad", (HUGE_INT, "五分钟", None, [1], {"a": 1}),
                         ids=["10^400", "中文", "null", "list", "dict"])
def test_数值归一函数对每种JSON形状都零raise(bad):
    """`_finite_or_close` 的 docstring 写着"零 raise"，这一条替那句话作证。

    捕获面就是 `float()` 对 JSON 值会抛的三类：TypeError（null/列表/字典）、
    ValueError（中文）、OverflowError（400 位整数）。负向锁：把 `except` 里的
    `OverflowError` 摘掉，`10^400` 那一行红在异常上。
    """
    assert config_store._finite_or_close(bad, 7.5, "判据") == 7.5


def test_超出float范围的整数被形状关拒收():
    """`_type_ok` 是缓存写入前判"收不收"的地方：它拒，restore/apply_update 就不会收。

    负向锁：把 `_type_ok` 里那次 `float(value)` 的 try 摘掉，前两句变成 OverflowError
    （不是 False），这条红在异常而不是断言——同样是红。
    """
    assert config_store._type_ok(HUGE_INT, 0.0) is False
    assert config_store._type_ok(HUGE_INT, 0) is False
    assert config_store._type_ok(1000, 0.0) is True
    assert config_store._type_ok(float("inf"), 0.0) is True, (
        "inf 是 float，形状关收它；非有限值由写缓存前的消毒层折回默认")


def test_盘上是400位整数时read仍能过allow_nan序列化(tmp_path, monkeypatch):
    """复核员的实跑：`restore()` 收下 400 位整数，`read()` 把它原样交给序列化。

    修法有两层：`_type_ok` 先拒收（这个数根本进不了缓存），`_finite_or_close` 的捕获面
    兜住任何绕过形状关的路径。只摘一层时另一层仍兜得住——两层是纵深，不是互斥。
    """
    path = tmp_path / "config.json"
    raw = {key: HUGE_INT for _, key, _, _ in CELLS}
    raw["credit_benchmark"] = {"tokens_per_credit": HUGE_INT}
    _write_config(path, raw)
    monkeypatch.setenv("CONFIG_DB_PATH", str(path))
    config_store.restore()
    try:
        json.dumps(config_store.read(), allow_nan=False)
    except ValueError as exc:
        pytest.fail(f"read() 把 10**400 交给了序列化（GET /v1/config 会 500）：{exc}")
    cfg = config_store.read()
    for _, key, _, _ in CELLS:
        assert cfg[key] == float(config_store.DEFAULTS[key]), (
            f"{key} 没折回自己那一格的默认：{cfg[key]!r}")
    assert cfg["credit_benchmark"]["tokens_per_credit"] == 1000


def test_管理端写入400位整数被拒且响应可序列化(tmp_path, monkeypatch):
    """超范围整数在管理端是"这一键没被接受"，不是 500，也不是静默改写成别的数。"""
    monkeypatch.setenv("CONFIG_DB_PATH", str(tmp_path / "config.json"))
    config_store.restore()
    out, rejected = config_store.apply_update({"approval_ticket_ttl_seconds": HUGE_INT})
    assert rejected == ["approval_ticket_ttl_seconds"]
    assert out["approval_ticket_ttl_seconds"] == float(
        config_store.DEFAULTS["approval_ticket_ttl_seconds"])
    json.dumps(out, allow_nan=False)


def test_基准价喂400位整数不抛且只折回那一键():
    """`credit_benchmark` 里那次 `float(value)` 曾在判据外：400 位整数直接 500。

    负向锁：把 `float(value)` 挪回 if 条件里（没有 try），任一键一行都红在
    OverflowError 上。判据：该键回默认，其余键**一个都不动**（不是整表重排）。
    """
    defaults = config_store.DEFAULTS["credit_benchmark"]
    numeric_keys = ("input_per_m", "output_per_m", "mixed_input_output_ratio",
                    "tokens_per_credit")
    for key in numeric_keys:
        raw = dict(defaults)
        raw[key] = HUGE_INT
        with config_store._lock:
            prev = config_store._config.get("credit_benchmark")
            config_store._config["credit_benchmark"] = raw
        try:
            out = config_store.credit_benchmark()
        finally:
            with config_store._lock:
                config_store._config["credit_benchmark"] = prev
        assert out[key] == defaults[key], f"{key} 没折回它自己的默认"
        for other in numeric_keys:
            if other != key:
                assert out[other] == defaults[other], (
                    f"一键坏了却动了 {other}：整表重排是另一件事，不是消毒")


def test_停点窗口吃到400位整数时不抛(isolated_tasks, tmp_path, monkeypatch):
    """`_window_seconds` 两条支各喂一次 400 位整数：显式参数、现读配置（getter）。

    负向锁：把 `_window_seconds` 里包 getter 的那个 try 摘掉、同时把 `_finite_or_close`
    的 `OverflowError` 捕获摘掉，这一条红在 OverflowError；只摘 `_finite_or_close`
    那一处时形状关先拒收（盘上那份进不了缓存），只有直写缓存那半段会红。
    """
    assert ts._window_seconds(HUGE_INT) == 0.0, "显式参数那一支没接住 OverflowError"
    path = tmp_path / "config.json"
    _write_config(path, {"task_waiting_user_timeout_seconds": HUGE_INT})
    monkeypatch.setenv("CONFIG_DB_PATH", str(path))
    monkeypatch.delenv("TASK_WAITING_USER_TIMEOUT_SECONDS", raising=False)
    config_store.restore()
    assert ts._window_seconds() == 0.0, "现读那一支在形状关之后仍然抛了"
    # 绕过形状关直写缓存：getter 的 `_finite_or_close` 自己也要接得住。
    with config_store._lock:
        prev = config_store._config.get("task_waiting_user_timeout_seconds")
        config_store._config["task_waiting_user_timeout_seconds"] = HUGE_INT
    try:
        assert ts._window_seconds() == 0.0, "直写缓存后 getter 没接住 OverflowError"
    finally:
        with config_store._lock:
            config_store._config["task_waiting_user_timeout_seconds"] = prev
    info = ts.waiting_timeout_info(_parked_task())
    json.dumps(info, allow_nan=False)


def test_400位整数的TTL在导入期只降级不抛(tmp_path):
    """复核员的实跑：子进程 `import app.core.approval` 曾 rc=1（现网启动会炸）。

    盘上那份 config.json 用 `json.dump` 原样写 400 位整数；判据是 rc=0、OK 行仍在、
    而且**有一行说出来**（点名 `approval_ticket_ttl_seconds`、说按默认跑）。

    负向锁：把 `_type_ok` 里 `float(value)` 的 try 与 `_finite_or_close` 里
    `OverflowError` 的捕获同时摘掉，`restore()` 在 config_store 导入期抛，rc=1，
    红话是「导入期抛了：...OverflowError...」。只摘 `_finite_or_close` 那一处**实测
    不红**（`_type_ok` 先把这个数拒在缓存外）——两层互为纵深，这条锁量的是"入口对
    OverflowError 的全部防御"，不是其中一层。
    """
    path = tmp_path / "config.json"
    _write_config(path, {"approval_ticket_ttl_seconds": HUGE_INT})
    script = ("import app.core.approval as a;"
              "print('OK', a.ticket_ttl_seconds(), a.APPROVAL_TIMEOUT_SECONDS)")
    env = dict(os.environ)
    env.pop("APPROVAL_TICKET_TTL_SECONDS", None)
    env["DATA_ROOT"] = str(tmp_path)
    env["CONFIG_DB_PATH"] = str(path)
    proc = subprocess.run([sys.executable, "-c", script], cwd=str(BACKEND_DIR),
                          env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"导入期抛了：{proc.stderr[-600:]}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert any(ln.startswith("OK ") for ln in lines), proc.stdout[-300:]
    assert any("approval_ticket_ttl_seconds" in ln and "按默认值跑" in ln for ln in lines), (
        f"400 位整数静悄悄地过了导入期，没人说出来：{proc.stdout[-400:]}")
    report = [ln for ln in lines if ln.startswith("OK ")][-1].split()
    default = float(config_store.DEFAULTS["approval_ticket_ttl_seconds"])
    assert (float(report[1]), float(report[2])) == (default, default)


# ---------- BLOCK ①（二轮）：基准价要消毒**整棵子字典** ----------
#
# 复核员实跑：`{"credit_benchmark": {"currency": Infinity}}` 或
# `{"credit_benchmark": {"junk": Infinity}}` 经 `restore()` 后
# `json.dumps(cs.read(), allow_nan=False)` 抛；`apply_update` 的返回体同形状。
# 旧消毒只遍历已知数值键、跳过 currency，且 `out = dict(value)` 保留额外键。

BENCHMARK_BAD_SHAPES = ("currency=Infinity", "junk=Infinity", "junk=NaN",
                        "junk=10**400", "junk=[Infinity]")


def _benchmark_payload(shape: str) -> dict:
    """在"全默认"的基础上只坏一处，用来证明消毒没有顺手重排其余键。"""
    payload = dict(config_store.DEFAULTS["credit_benchmark"])
    if shape == "currency=Infinity":
        payload["currency"] = float("inf")
    elif shape == "junk=Infinity":
        payload["junk"] = float("inf")
    elif shape == "junk=NaN":
        payload["junk"] = float("nan")
    elif shape == "junk=10**400":
        payload["junk"] = HUGE_INT
    else:
        payload["junk"] = [float("inf")]
    return payload


def _assert_benchmark_clean(bench: dict, shape: str) -> None:
    defaults = config_store.DEFAULTS["credit_benchmark"]
    for sub, default in defaults.items():
        if sub == "currency":
            continue
        assert bench[sub] == default, f"坏值 {shape} 顺手动了已知键 {sub}：{bench[sub]!r}"
    if shape.startswith("currency="):
        assert bench["currency"] == defaults["currency"], (
            f"currency 喂 {shape} 之后成了 {bench['currency']!r}")
    else:
        assert "junk" not in bench, (
            f"额外键（{shape}）没被丢掉，它会跟着 read() 进响应：{bench.get('junk')!r}")


@pytest.mark.parametrize("shape", BENCHMARK_BAD_SHAPES,
                         ids=[s.replace("**", "^") for s in BENCHMARK_BAD_SHAPES])
def test_基准价子字典的坏值经盘进来后read与响应都过得了序列化(shape, tmp_path, monkeypatch):
    """整棵子字典都要消毒，不只是"已知数值键里的非有限数"。

    `currency` 是非数值键（旧消毒跳过它），`junk` 是额外键（旧消毒用 `dict(value)`
    原样保留）。`junk=10**400` 那一行要单独说：`json.dumps` 对任意大的整数**不抛**，
    所以只断言"dumps 不抛"会漏掉它——必须断言这一键被丢掉。嵌套 `[Infinity]` 证明
    判据是递归的，不是只看顶层标量。

    负向锁（实跑）：把 `_sanitize_loaded` 的 credit_benchmark 分支摘回 `dict(value)`，
    `currency=Infinity` / `junk=Infinity` / `junk=NaN` / `junk=[Infinity]` 红在 dumps，
    `junk=10**400` 红在"额外键没被丢掉"——五种形状没有一种能混过去。
    """
    path = tmp_path / "config.json"
    _write_config(path, {"credit_benchmark": _benchmark_payload(shape)})
    monkeypatch.setenv("CONFIG_DB_PATH", str(path))
    config_store.restore()
    cfg = config_store.read()
    try:
        json.dumps(cfg, allow_nan=False)
    except ValueError as exc:
        pytest.fail(f"restore() 把基准价子字典里的坏值收进了缓存，GET /v1/config 会抛：{exc}")
    _assert_benchmark_clean(cfg["credit_benchmark"], shape)


@pytest.mark.parametrize("shape", BENCHMARK_BAD_SHAPES,
                         ids=[s.replace("**", "^") for s in BENCHMARK_BAD_SHAPES])
def test_基准价子字典的坏值经管理端进来后响应与落盘都过得了序列化(shape, tmp_path,
                                                              monkeypatch):
    """`apply_update` 的返回体就是 `POST /v1/admin/config` 的响应，落盘那份是下一次
    `restore()` 的输入——两份都要干净，且 `rejected` 里不许出现这个键（形状关收下了
    整棵子树，消毒层负责折值/丢键，不是把整键拒掉）。

    负向锁：同上（摘回 `dict(value)`），返回体与落盘那份各红在 dumps 或额外键断言。
    """
    monkeypatch.setenv("CONFIG_DB_PATH", str(tmp_path / "config.json"))
    config_store.restore()
    out, rejected = config_store.apply_update(
        {"credit_benchmark": _benchmark_payload(shape)})
    assert rejected == [], f"子字典的形状关收下了它，消毒层该折值/丢键：{rejected!r}"
    try:
        json.dumps(out, allow_nan=False)
    except ValueError as exc:
        pytest.fail(f"apply_update 的返回体带着坏值，管理端响应会抛：{exc}")
    _assert_benchmark_clean(out["credit_benchmark"], shape)
    with open(tmp_path / "config.json", encoding="utf-8") as f:
        disk = json.load(f)
    try:
        json.dumps(disk, allow_nan=False)
    except ValueError as exc:
        pytest.fail(f"消毒后的字典仍把坏值落了盘，下一次 GET /v1/config 会抛：{exc}")
    _assert_benchmark_clean(disk["credit_benchmark"], shape)


# ---------- BLOCK ②（二轮）：float 范围内、datetime 范围外的数 ----------
#
# 复核员实跑：10**18 过得了 `_type_ok`（read 可序列化），却让
# `timedelta(seconds=...)` 抛 "Python int too large to convert to C int"；
# 10**13 抛 "date value out of range"；10**14 抛
# "days=...; must have magnitude <= 999999999"。边界常量是实测出来的，
# 两条上限（timedelta 自己的天数上限、datetime 的日期差）里取更紧的那个。

DATETIME_OVERRANGE_INTS = (10 ** 13, 10 ** 14, 10 ** 18, 10 ** 18 + 1)


def test_窗口上限常量卡在datetime能表达的最大边界上():
    """常量不是拍的：上限那一秒从 datetime.min 加得进去，再加一秒就出界。

    负向锁（实跑）：把 `MAX_WINDOW_SECONDS` 放宽到 `sys.float_info.max`
    （1.7976931348623157e308），**首句**就先抛 `OverflowError: Python int too large
    to convert to C int`——`timedelta(seconds=...)` 自己都构不出来，轮不到"第二句
    不再抛"。同一变异下 10**13 那一族也全红——这正是复核员说的"只挡 float 范围不够"。
    """
    edge = config_store.MAX_WINDOW_SECONDS
    assert datetime.datetime.min + datetime.timedelta(seconds=edge) <= datetime.datetime.max
    with pytest.raises(OverflowError):
        datetime.datetime.min + datetime.timedelta(seconds=edge + 1)
    assert datetime.timedelta.max.total_seconds() > edge, (
        "边界取了更紧的那条：datetime 的日期差，不是 timedelta 自己的天数上限")


def test_超出datetime范围的数被形状关拒收而inf仍然收():
    """形状关是唯一能在写缓存前说"这一键没被接受"的地方。

    inf 不在这一关拒：它是 float 的形状，由写缓存前的消毒层折回该格默认（既有用例
    `test_管理端写入非有限值后的响应…` 钉着 `rejected == []`）。

    负向锁：把 `_type_ok` 里那条上界判据摘掉，第一族断言红；只放宽常量也一样。
    """
    for value in DATETIME_OVERRANGE_INTS:
        assert config_store._type_ok(value, 0.0) is False, (
            f"{value} 过得了形状关——它会在 timedelta 里炸")
    assert config_store._type_ok(3600, 0.0) is True
    assert config_store._type_ok(float("inf"), 0.0) is True


@pytest.mark.parametrize("value", DATETIME_OVERRANGE_INTS,
                         ids=[str(v) for v in DATETIME_OVERRANGE_INTS])
def test_超出datetime范围的整数经盘进来时详情响应不炸(value, isolated_tasks,
                                                    tmp_path, monkeypatch):
    """走盘的那一族：形状关拒收 → 缓存里是该格默认 0 → 详情 dict 可序列化。

    这里**不传** `timeout_seconds`：复现的就是 `waiting_timeout_info` 自己现读那一支。

    负向锁（实跑）：把 `MAX_WINDOW_SECONDS` 放宽到 `sys.float_info.max`
    （1.7976931348623157e308，两层上界一起失效），四个 id **全红在同一条断言**上，
    红话是 `AssertionError: 10000000000000 没在写缓存前折回默认：10000000000000`
    （每个 id 报它自己的数）——`waiting_deadline` 已经把到期时刻封顶到
    `datetime.max`，`waiting_timeout_info` 这一层不再抛 OverflowError。只摘
    `_type_ok` 一层不红（`_sanitize_loaded` 的上界分支接着折回默认）——两层是纵深。
    """
    path = tmp_path / "config.json"
    _write_config(path, {"task_waiting_user_timeout_seconds": value})
    monkeypatch.setenv("CONFIG_DB_PATH", str(path))
    monkeypatch.delenv("TASK_WAITING_USER_TIMEOUT_SECONDS", raising=False)
    config_store.restore()
    cfg = config_store.read()
    json.dumps(cfg, allow_nan=False)
    assert cfg["task_waiting_user_timeout_seconds"] == 0.0, (
        f"{value} 没在写缓存前折回默认：{cfg['task_waiting_user_timeout_seconds']!r}")
    info = ts.waiting_timeout_info(_parked_task())
    assert info["waiting_timeout_seconds"] == 0.0, (
        f"{value} 被收成了一个会炸的窗口：{info['waiting_timeout_seconds']!r}")
    json.dumps(info, allow_nan=False)


@pytest.mark.parametrize("text", ("1e13", "1e14", "1e18"), ids=["1e13", "1e14", "1e18"])
def test_环境变量里的超范围窗口也不让详情响应炸(text, isolated_tasks, monkeypatch):
    """env 臂不经过 `_type_ok` / `_sanitize_loaded`：`1e18` 是有限浮点，只能在
    getter 出口（以及 `_window_seconds` 的第二层）折回 0。

    负向锁：把 `_finite_or_close` 与 `_window_seconds` 两处上界判据**同时**摘掉，
    这条红在 `waiting_timeout_info` 里的 OverflowError；只摘一处不红（另一层接着）。
    """
    monkeypatch.setenv("TASK_WAITING_USER_TIMEOUT_SECONDS", text)
    info = ts.waiting_timeout_info(_parked_task())
    assert info["waiting_timeout_seconds"] == 0.0, (
        f"env {text} 被收成了一个会炸的窗口：{info['waiting_timeout_seconds']!r}")
    json.dumps(info, allow_nan=False)


def test_起点本身已经很晚时到期时刻封顶在datetimemax(isolated_tasks, monkeypatch):
    """窗口在上限以内，起点却晚到"再加就出界"（tasks.json 被手改 / 时钟跳到 9999 年）。

    `waiting_deadline` 那次加法同样不许变成详情端点的 500：封顶到 `datetime.max`
    仍然表达"远到看不见"，而不是抛 `date value out of range`。
    负向锁：把 `waiting_deadline` 里那次 OverflowError 捕获摘掉，这条红在加法上。
    """
    monkeypatch.setenv("TASK_WAITING_USER_TIMEOUT_SECONDS", "3600")
    task = _parked_task()
    task.waiting_since = datetime.datetime.max.isoformat()
    deadline = ts.waiting_deadline(task, timeout_seconds=3600.0)
    assert deadline == datetime.datetime.max, f"到期时刻没封顶：{deadline!r}"
    info = ts.waiting_timeout_info(task)
    assert info["waiting_expires_at"] == datetime.datetime.max.isoformat()
    json.dumps(info, allow_nan=False)


# ---------- BLOCK ③（二轮）：合法 int 保型；超范围整数明确拒绝 ----------

def test_合法整数经restore后read原样保型(tmp_path, monkeypatch):
    """`3600` 进、`3600` 出，类型也是 int——不统一浮点化。

    值没变只是类型变了也算一种静默改写（`10**18+1` 那种是真改值，另有一条）。
    getter 出口的形状不变：仍是 float。

    负向锁：把 `_sanitize_loaded` 里保型那一支摘掉（回到 `_finite_or_close`），
    `type(...) is int` 这一句红，拿到的是 3600.0。
    """
    path = tmp_path / "config.json"
    _write_config(path, {"stream_no_reader_grace_seconds": 3600})
    monkeypatch.setenv("CONFIG_DB_PATH", str(path))
    config_store.restore()
    got = config_store.read()["stream_no_reader_grace_seconds"]
    assert got == 3600 and type(got) is int, (
        f"restore 把整数 3600 改写成了 {got!r}（{type(got).__name__}）")
    assert config_store.stream_no_reader_grace_seconds() == 3600.0


def test_管理端写入超出datetime范围的整数被明确拒绝而不是静默取整(tmp_path, monkeypatch):
    """复核员实跑：`10**18+1` 被收下并静默落成 `1e18`、`rejected=[]`。

    判据三处：`rejected` 点名这一键、返回值与**落盘值**都是该格默认（不是 1e18）、
    响应可序列化。负向锁：把 `_type_ok` 的上界判据摘掉（常量保持），
    `rejected == []` 红——它会走 `_sanitize_loaded` 的上界分支静默折成默认；
    两层都摘掉时落盘值变成 1e18，还是红。
    """
    monkeypatch.setenv("CONFIG_DB_PATH", str(tmp_path / "config.json"))
    config_store.restore()
    out, rejected = config_store.apply_update(
        {"stream_no_reader_grace_seconds": 10 ** 18 + 1})
    assert rejected == ["stream_no_reader_grace_seconds"], (
        f"超范围整数被静默收下了：rejected={rejected!r}")
    default = float(config_store.DEFAULTS["stream_no_reader_grace_seconds"])
    assert out["stream_no_reader_grace_seconds"] == default, (
        f"返回值被静默改成了 {out['stream_no_reader_grace_seconds']!r}")
    with open(tmp_path / "config.json", encoding="utf-8") as f:
        disk = json.load(f)
    assert disk["stream_no_reader_grace_seconds"] == default, (
        f"落盘值被静默改成了 {disk['stream_no_reader_grace_seconds']!r}")
    json.dumps(out, allow_nan=False)
