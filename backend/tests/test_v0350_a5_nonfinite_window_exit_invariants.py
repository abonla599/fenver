r"""A5 出口不变量：**非有限值不许从配置读数口径穿出去**，尤其不许穿进 JSON 响应。

这把尺子量的是「付费端点被打成 500」那一条真链路：`data/config.json` 里一个 `Infinity`
（或一颗环境变量）→ `config_store` 的 getter → `task_store.waiting_timeout_info()` →
详情端点的那份 dict → `starlette` 用 `allow_nan=False` 序列化 → 当场抛。
它长的样子不是"算错了个窗口"，是一个 500；而同一枚值在别的格子里更坏：票据窗口拿到
`inf` 就是**永不过期**，钱闸拿到 `inf` 就是**永不落**。

⭐ 为什么还要再写一个文件（既有那把窗口判据的锁已经存在）：`tests/
test_v0350_a5_timeout_is_suspension.py` 里"窗口坏值"那一族**只喂显式参数**
（`is_waiting_timed_out(..., timeout_seconds=...)`），走的是 `_window_seconds` 自己
那道 `isfinite`。环境变量与 `data/config.json` 那两路读数口径它一格没碰，
`_window_seconds(None)` 那一支也没有别的锁——这条文件补的就是这两路。所以本文件里
每一条走停点窗口的用例都**刻意不传 `timeout_seconds`**：传了就退化成既有那把尺子
已经量过的东西，两个文件一起留同一个盲区。

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
- `task_store._window_seconds` 的 `None` 那一支（现读那一条），以及一个故意绕开配置
  出口的假读数口径（验的是"就算上游漏了，这一层也拦得住"）。
- 形状只有这些：`("inf","Inf","Infinity","+inf","1e400","  inf ","nan","-inf","","五分钟")`
  喂 env 臂；`(inf, -inf, nan)` 经 `restore()` 喂 config 臂；同一批字符串另走一支
  绕过 `restore()` 的直写用例。

──────── 仍然漏掉的形状（本文件不声称覆盖）────────
- config 臂上 `""` 与 `"五分钟"` **到不了 getter 的那一次 `float()`**：它们在
  `restore()` 的形状关（`_type_ok`）就被拦下、按默认跑。字符串真被送到 `float()`
  面前只有一次，就是那条直写用例，它模拟的是"上游形状关被跳过"，不是现网通道。
- 下游一格没动，也不由本文件验：`stream_runs.py` 那道钱闸与 `core/approval.py` 的
  票据窗口，各自的判据在它们自己的文件里。**出口折回什么值，它们就按什么值跑**——
  所以"宽限期与 TTL 的坏值折回默认窗口而不是 0"这一格的实际后果在下游，不在这把尺子
  里；要验那两格的真行为，得由持有那两个文件的票接手。
- 不经这些 getter 的读数（直接 `os.getenv`、或拿 `read()` 里的原始值）不在射程。
- HTTP 层不在射程：这里验的是 `waiting_timeout_info()` 吐出的那份 dict——详情端点
  序列化正是这份 dict；把它接进 `TestClient` 只会给这条不变量多两个失败原因。
- 「不变量」那一族只看得到 `inf`：`nan` 与 `-inf` 被 `max` 折成 0.0，而 0.0 也是干净值。
  这两行由"落点必须是该格默认"那句方向断言负责（env 臂与 config 臂各有一条）。剩下
  一种真看不出差别的情形要如实说：默认本来就是 0 的那两格（作废窗口、影子护栏），
  坏值折成 0 与折成默认是同一个数——那里 0 就是它们自己要的那一档，不是被拧过去的。
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
    `nan` 与 `-inf` 这两行是上面两族不变量用例接不住的（坏值被 `max` 折成 0.0 也算
    "干净值"），这一族才问得出"0 是不是那一格想要的 0"。
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
