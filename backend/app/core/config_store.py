"""运行时配置：一个 data/config.json + 默认值 + 环境变量口子。

v0.24 之前"配置"散在三处：注册永远开放写在路由里没有开关、更新仓库地址硬编码在
releases.py、注册开关的口径只存在于文档。这个模块把它们收进一份能落盘、能改、
能重启后还在的文件——与 providers/usage/schedule 同一套形状：读时合并默认值，
写时公共原子落盘（core/atomic_write），读不懂的记录先备份 .corrupt 再退回默认。

两个环境变量的口径（与其余存储一致：env 优先于文件）：
- CONFIG_DB_PATH：文件落点，测试与多实例靠它指走。
- REGISTRATION_OPEN：显式设了就压过文件里的值。这不是后门，是部署入口——
  docker-compose 里给一颗环境变量，比先起服务再调管理端点更省事；也保证
  文件被误删时"注册关着"这个安全默认不会因为读不到文件而翻成开。
"""
import json
import math
import os
import re
import threading
from typing import Tuple

from app.core.atomic_write import write_json_atomic
from app.core.paths import data_root

# 安全默认：开源版就是"给你自己用的"，注册默认关；想开放，管理端点或 env 说一声。
# 更新仓库默认指向新快照仓；旧仓名由 releases.py 作为回落常量单独管着（老用户
# 的 v0.23.x 更新链在改名后不能断，那是 releases 的事，不是这里的第二真相）。
DEFAULTS = {
    "registration_open": False,
    "update_repo": "abonla599/fenver",
    # v0.25 R3b：流式断线后"没有读者的宽限期"（秒）。单一真相就是这里——
    # 读取统一走 stream_no_reader_grace_seconds()，别处不许再写这个数。
    # 为什么是 120 而不是 30（产品定的口径，别再当省钱旋钮调小）：省钱的是
    # streaming.py 轮次边界上的钱闸——没有活读者就不起飞新一轮，这一条与宽限期
    # 长短无关；宽限期决定的是"你离开多久之后我们还替你接着跑"。接个电话、锁屏
    # 看一眼、页面重进，都是几十秒量级，30 秒会把答案停在半路而一分钱没多省。
    "stream_no_reader_grace_seconds": 120.0,
    # v0.35 A2：一次性审批票据的有效期（秒）。单一真相就是这里，读数走
    # approval_ticket_ttl_seconds()（env APPROVAL_TICKET_TTL_SECONDS 优先）。
    # 60 这个数**没变**——它就是 approval.py 从 v0.31 起一直在用的那个常量，本版只是
    # 把它从"模块里一个字面量"升成"可配的一格"，默认照旧，谁的行为都不因此改变。
    # 为什么值得给一个旋钮（提案 §二 第二行、§四 v0.35）：60 秒是给"弹了没人回"的
    # 移动端定的，而一个跑几分钟的多步任务里第二次确认一定落在 60 秒之外；配的是窗口，
    # 不是判据——到期一律作废，绝不因为"任务还活着"就续。0 是合法值：等于每次都要现批。
    "approval_ticket_ttl_seconds": 60.0,
    # v0.35 A5：一条挂在 `waiting_user` 断点上的任务最多可以挂多久（秒）。单一真相就是
    # 这里，读数走 task_waiting_user_timeout_seconds()（env TASK_WAITING_USER_TIMEOUT_SECONDS 优先）。
    # ⭐ 默认 0 = **永不作废**，而这不是"还没想好"，是这一格的性质：正数打开的那道动作是
    # **销毁用户手里的断点**（作废之后答案递进去也接不回去），所以默认必须停在"不销毁"
    # 那一头，与提案 §四 v0.35「超时语义：用户不响应**不是失败**，是挂起保活」同一方向：
    # 默认形态下挂 5 分钟、挂 5 天都还停在同一个断点上，带答案回来就是第 4 步（判据 1）。
    # 与上面两格不同的地方要说清：那两格的正数是"窗口更长＝更宽"，这一格的正数是
    # "到点作废＝更狠"，所以负数与读不懂的值一律按 0 收（关），而不是按某个默认窗口收。
    # 运维要挡"停点无限堆积"就把这一格设成正数（例：86400 = 一天）：超时的那个停点会被
    # 翻成 cancelled 并**作废游标**（判据与为什么不是 failed 见 task_store.expire_parked_task）。
    "task_waiting_user_timeout_seconds": 0.0,
    # v0.25 R4b T5.8：积分的基准价——「1 积分 = 用基准价跑 1000 个混合 token」的锚。
    # 单一真相就是这一份：展示倍率 = 模型混合单价 ÷ 这里的混合价，结算换算也除这里的
    # 混合价。刻意不用"默认模型 = 1.0x"那种定义——锚跟着 ★ 漂移，管理员点一下按钮
    # 全站倍率集体重排，倍率表就再也不能当长期记忆用（卡片 §2.3b / PRD D25 Q-b）。
    # 示例口径（部署者可改，改的是锚不是公式）：输入 ¥1/百万 + 输出 ¥4/百万 = 1.00x；
    # mixed_input_output_ratio=4.0 是"展示折算的典型输入:输出 = 4:1"，只进倍率折算，
    # 结算一律真实 token × 真实单价（D13：倍率不是合同价）。
    "credit_benchmark": {
        "currency": "CNY",
        "input_per_m": 1.0,
        "output_per_m": 4.0,
        "mixed_input_output_ratio": 4.0,
        "tokens_per_credit": 1000,
    },
    # v0.25 R4b T5.20：每日影子积分护栏。默认 0 = 只算不拦——本批口径是
    # 「算得出、看得见、拦得住（默认关）」，开不开是部署者的决定。填正数后，
    # 某用户当日影子合计到线就停在「等待你确认是否继续」，且不再向模型出网。
    "credit_shadow_daily_limit": 0.0,
    # v0.37.0 http_fetch 的主机白名单：工具只允许抓这些域名（精确匹配主机名，
    # 不含子域）。默认空名单 = 工具对模型不可见——"没人配过"与"配了但都拒了"
    # 必须是同一个可见状态，而不是一个每次调用都失败的摆设。条目形状见
    # _FETCH_HOST_RE：小写 ASCII LDH、≥2 标签、无通配/端口/@/尾点、整表 ≤50 条。
    "fetch_allowed_hosts": [],
}

# 一个窗口数值能取到的**最大秒数**。下游 `task_store.waiting_deadline()` 会做
# `waiting_since + timedelta(seconds=window)`，而 datetime 的表达区间是
# 0001-01-01 ~ 9999-12-31；`timedelta.max` 自己的上限（999999999 天 ≈ 8.64e13 秒）
# 比日期差宽，所以两条上限里更紧的是日期差：实测
# `(datetime.max - datetime.min).total_seconds()` = 315537897600.0，而
# `timedelta(seconds=315537897600.0)` 从 datetime.min 加过去就抛
# "date value out of range"，315537897599.0 才加得进去。超过这条线的数不是"很长的
# 窗口"，是一次 `OverflowError` / `date value out of range`（详情端点 500）。
# 它同时远小于 2**53：线内的整数都能被 float 精确表示，int→float 不会改值。
#
# 射程（#149 已收窄）：这条线只对**秒**格生效——`stream_no_reader_grace_seconds`、
# `approval_ticket_ttl_seconds`、`task_waiting_user_timeout_seconds`
# （名单是下面的 `_SECONDS_KEYS`；`_type_ok`、`_sanitize_loaded`、
# `_finite_or_close` 都按格判）。别的顶层数值格（今天只有
# `credit_shadow_daily_limit`）是计数不是时长，`1e12` 不是"很长的窗口"而是配得出
# 来的额度，不许拿这条秒线去拒它；它们的取值范围上界 owner 是 IEEE-754 双精度
# 浮点本身（`float()` 不溢出且有限，即 `_type_ok` 的 OverflowError 关与
# `_json_safe` 的 `allow_nan=False` 关），不是 datetime。
#
# 窄带：值本身 ≤ 这条线、但从 `waiting_since` 加过去会越界的那一段**不是拒绝，是
# 封顶**——`task_store.waiting_deadline()` 捕住那次 OverflowError 后返回
# `datetime.max`（9999-12-31T23:59:59.999999），展示语义是"约八千年后到期"。
# 这样详情端点绝不因为一次日期加法 500。
MAX_WINDOW_SECONDS = 315_537_897_599.0

# 秒格的名单：只有会被送进 `timedelta(seconds=...)` / 日期加法的格子才归上面那条线
# 管。新增一个"秒"配置格时要加到这里；`tests/test_v0350_ticket149_seconds_cap_scope.py`
# 钉死这份名单，加格会红——那不是麻烦，是提醒你确认它真的该受 datetime 限制。
_SECONDS_KEYS = frozenset({
    "stream_no_reader_grace_seconds",
    "approval_ticket_ttl_seconds",
    "task_waiting_user_timeout_seconds",
})

# 允许被 POST /v1/admin/config 改写的键。白名单而不是全接收：配置面将来加到
# 会话存储、供应商地址这类东西上时，忘了改这张表的管理端点就等于开了个任意写口子。
MUTABLE_KEYS = frozenset(DEFAULTS)

# v0.33 SSRF：`update_repo` 是唯一一个"管理员能改、但值会被拼进服务端出站 URL"的键
# （releases.latest_url_for = _API_HOST + repo + "/releases/latest"）。只靠 _type_ok
# 的话任何字符串都过，于是 `../../../../169.254.169.254/latest/meta-data/` 会被
# HTTP 客户端路径归一化吃掉前缀，让服务端替管理员去读云元数据。
#
# 白名单而不是黑名单：这个值唯一的合法形状就是 GitHub 的 owner/repo——两段、
# 各由字母数字与 _ . - 组成。白名单同时顺手挡掉了 `../`、绝对 URL、`\`、
# 端口与 userinfo（`@`）这些形状，而它们各自都能拼出别的目标主机。
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# v0.37.0 http_fetch 白名单的条目形状。一个正则收齐全部要求：
# * 只允许小写字母/数字/连字符，且标签首尾不许是连字符（LDH，RFC3696 口径）——
#   大写、下划线、`*`（通配）、`:`（端口）、`@`（userinfo）、非 ASCII 全部落不进；
# * 至少两段（`[label](.label)+`）——裸单标签名不许上表，与 URL 契约同一口径，
#   否则"配了个 `localhost`"会以"看着像域名"的形状混进来；
# * 尾点被空标签天然拒死（`example.com.` 会要求最后一段匹配 LDH，空串不匹配）；
# * 单标签 ≤63、总长 ≤253（正则里 1+62 与 4+63*4 的组合在 253 内再叠一条硬检查）。
# 为什么这么严：这份名单是 http_fetch 唯一的"允许出网到哪"的事实来源，条目形状
# 一松（通配、前缀、URL），匹配语义就会在不同代码路径里各长成不同样子。
_FETCH_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                            r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9]))+$")
_FETCH_HOST_MAX = 50


def _fetch_host_ok(entry) -> bool:
    return (isinstance(entry, str) and len(entry) <= 253
            and bool(_FETCH_HOST_RE.match(entry)))


def _normalize_fetch_hosts(value) -> list:
    """把任意 JSON 形状收成"合法条目、去重、保序、≤50 条"的名单。

    与 `_finite_or_close` 同一族纪律：零 raise、坏条目 print 一句就丢——这一支
    跑在 `restore()`（导入期）里，抛出去就是现网起不来。管理端 POST 的"整单拒收"
    由 `_VALUE_CHECKS` 在写入口判，两关的分工与其余格子的"形状关+消毒关"一致。
    """
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for item in value:
        entry = item.strip().lower() if isinstance(item, str) else item
        if not _fetch_host_ok(entry):
            print(f"⚠️ 配置项 fetch_allowed_hosts 的条目 {item!r} 不是"
                  "「小写、≥2 段、无通配/端口/@/尾点」的 ASCII 主机名，丢弃这一条")
            continue
        if entry in seen:
            continue
        if len(out) >= _FETCH_HOST_MAX:
            print(f"⚠️ 配置项 fetch_allowed_hosts 超过 {_FETCH_HOST_MAX} 条上限，"
                  f"其余条目丢弃：名单是访问策略不是爬虫清单")
            break
        seen.add(entry)
        out.append(entry)
    return out


# 逐键的值域校验。与 _type_ok（形状）并列的另一道闸门，两者都过才写入。
# 拒的键与类型不对、未知键走同一条路：进 rejected，不抛异常也不静默吞——
# 界面上"保存"的人有权知道这一格没被接受。
_VALUE_CHECKS = {
    "update_repo": lambda v: bool(_REPO_RE.match(str(v).strip())),
    # 管理端写入整表判：任何一条形状不对或超 50 条就整个键拒收（进 rejected），
    # 而不是悄悄收一半——管理员要的是"这份名单没被接受"，不是"哪一条没进"。
    "fetch_allowed_hosts": lambda v: (isinstance(v, list) and len(v) <= _FETCH_HOST_MAX
                                      and all(_fetch_host_ok(e) for e in v)),
}


def _value_ok(key: str, value) -> bool:
    check = _VALUE_CHECKS.get(key)
    return check is None or check(value)

_lock = threading.RLock()
_config = dict(DEFAULTS)


def _default_path() -> str:
    env_path = os.getenv("CONFIG_DB_PATH")
    if env_path:
        return os.path.abspath(env_path)
    return os.path.join(data_root(), "data", "config.json")


def _env_registration_open():
    """REGISTRATION_OPEN 设了就返回布尔，没设返回 None（表示"文件说了算"）。"""
    raw = os.getenv("REGISTRATION_OPEN", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def _seconds_limited(key) -> bool:
    """这一格受不受 `MAX_WINDOW_SECONDS`（datetime 能表达的秒数上限）管。

    `key is None` 是"没点名格子"的直接探针走的那一支，按秒格收，也就是严的那一支：
    现网两个写入口（`restore()` / `apply_update()`）与全部 getter 都点名传 key，不会
    落到这里；两条 A5 用例（`_type_ok(value, 0.0)`、`_finite_or_close(bad, 7.5, ...)`）
    就是这么直接探的，它们的判据仍然有效。
    """
    return key is None or key in _SECONDS_KEYS


def _type_ok(value, default, key: str = None) -> bool:
    """键的形状判定。数字对数字是宽容的：JSON 里 30 与 30.0 是同一个东西的
    两种写法——按老的 isinstance 口径，手写的整数秒会被判"类型不对"悄悄回
    默认，正是"我明明改过怎么没生效"那一类配置事故。bool 仍然只收 bool。

    数值还要多两关。一关是**超出 float 范围的 int**（`float(10**400)` 抛
    OverflowError）：`restore()` 跑在导入期、审批 TTL 又在 `app/core/approval.py`
    的模块层被取走，收下这种数就是让现网起不来（不是 500，是 ImportError）；"等读到
    的时候再兜"也挡不住 `read()`——它会把盘上原值直接交给 `GET /v1/config`。
    另一关是**超出 datetime 可用范围**的值，但只对秒格（`key in _SECONDS_KEYS`）
    生效：`10**13` 过得了 float，过不了下游 `waiting_since + timedelta(seconds=...)`
    （见 `MAX_WINDOW_SECONDS`）。它不是秒格（例如影子护栏那种计数）时不受这条线管，
    否则 `credit_shadow_daily_limit=1e12` 会被一次范畴错误拒掉。形状关是
    唯一能在写缓存前说"这一键没被接受"的地方，放它进去就只剩 500 或静默折值两条
    坏路。inf 仍然收——它是 float 的形状，非有限值由写缓存前的消毒层折回该格默认。"""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            number = float(value)
        except OverflowError:
            return False
        if _seconds_limited(key) and math.isfinite(number) and number > MAX_WINDOW_SECONDS:
            return False
        return True
    return isinstance(value, type(default))


def restore(path: str = None) -> int:
    """从盘上读回配置（启动时一次；测试用它等价于"重启进程"）。返回读到的条数。"""
    target = os.path.abspath(path or _default_path())
    with _lock:
        _config.clear()
        _config.update(DEFAULTS)
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return 0
        except (ValueError, OSError) as e:
            backup = target + ".corrupt"
            try:
                os.replace(target, backup)
                print(f"⚠️ 配置文件读不了（{e}），已备份为 {backup}，按默认值跑")
            except OSError:
                print(f"⚠️ 配置文件读不了且无法备份（{e}），按默认值跑")
            return 0
        if not isinstance(data, dict):
            return 0
        count = 0
        for key in DEFAULTS:
            if key in data and _type_ok(data[key], DEFAULTS[key], key=key):
                _config[key] = _sanitize_loaded(key, data[key])
                count += 1
            elif key in data:
                # 形状不对的一条不入库，但要说出来：静默吞掉会让人以为
                # "我改过了怎么没生效"，其实是那行 JSON 写坏了类型或数值范围。
                print(f"⚠️ 配置项 {key} 的类型或数值范围不对，按默认值跑")
        return count


def _write() -> None:
    write_json_atomic(_default_path(), _config)


def read() -> dict:
    """当前生效的配置（env 已合并）。每次现读 env，改了不用重启——与 AUTH_MODE 同口径。

    `fetch_allowed_hosts` 必须**返回副本**：`_config` 初始化时是 `dict(DEFAULTS)`
    的浅拷贝，`DEFAULTS` 里那个 list 会被 `read()` 的调用方直接拿到；一次
    `read()["fetch_allowed_hosts"].append(...)` 就会改掉全局默认值（v0.37.0
    审查 N5 的实测路径）。其余键是标量/不可变子树，浅拷贝即可。
    """
    with _lock:
        out = dict(_config)
        out["fetch_allowed_hosts"] = list(out.get("fetch_allowed_hosts") or [])
    env_open = _env_registration_open()
    if env_open is not None:
        out["registration_open"] = env_open
    return out


def registration_open() -> bool:
    return bool(read()["registration_open"])


def update_repo() -> str:
    repo = str(read()["update_repo"] or "").strip()
    return repo or DEFAULTS["update_repo"]


def _finite_or_close(value, fallback: float, where: str, key: str = None) -> float:
    """把任意 JSON 形状归一成**非负、有限**的浮点；归不出来就折回 `fallback` 并说一句。

    **上界按格判**（#149）：`key` 是秒格（`_SECONDS_KEYS`）时再加一条不超过
    `MAX_WINDOW_SECONDS` 的上界——那条线管的是 datetime 装不下的秒数，不是"所有数
    字"。计数格（影子护栏）只受有限性约束，`1e12` 这种超秒线但完全合法的额度原样
    穿过；`key=None` 是直接探针，按秒格收（严的那一支，见 `_seconds_limited`）。

    为什么 `max(0.0, x)` 不够：它只挡负数，挡不住非有限值。`inf` 比 0 大，会**原样
    穿到出口**；而 `nan` 与任何数比较都是 False，于是被 `max` 吃掉、悄悄落回 0.0。
    两种穿出都不是"有人配过的值"，而下游每一处都当真：`inf` 进 JSON 响应会让序列化
    当场抛（`allow_nan=False`），进窗口判据则让那一格永不过期；`nan` 悄悄变 0 更是把
    "读不懂"伪装成一个有语义的策略值——配置面里 0 是配得出、且有含义的。

    折回哪一档由**调用点那一格自己说**（`fallback`），这里不替全文件发号施令：本文件
    每一支 getter 出口的坏值落点都是它那一格的默认值，也就是"读不懂就按默认跑"这条
    既有纪律的同一档。必须逐个给、不许统一折成 0.0，因为这些数字里 0 的方向不一致：
    作废窗口与影子护栏的 0 是"关"，宽限期与审批 TTL 的 0 是"不留窗口"（最严档），
    拿后者兜坏值等于凭空执行一次运维没做过的收紧。

    零 raise，只 print 一行：这一族的读数在**导入期**就会被取用（审批 TTL 住在
    `app/core/approval.py` 的模块层），在这里抛出去就是现网起不来的 ImportError。

    射程就是下面这条捕获面，不写裸 `except Exception`：`float()` 对 JSON 能表示的
    每一种形状只会抛三类——`TypeError`（`null`、列表、字典这类没有数值语义的）、
    `ValueError`（字符串不是数）、`OverflowError`（整数超出 float 范围，例如盘上一个
    400 位整数）。裸 `except Exception` 会把调用点自己的 bug（拼错的变量、属性名写错）
    也一起吞成"按默认跑"，那才是真正的静默。同一句话的另一半：它不承诺接住任意对象
    `__float__` 里抛出的自定义异常——配置面只装 JSON 值，没有那种东西。
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        print(f"⚠️ {where} 读不出一个数（{value!r}，{type(exc).__name__}），"
              f"按这一格的默认 {fallback!r} 跑")
        return fallback
    if not math.isfinite(number):
        print(f"⚠️ {where} 读出来不是有限值（{value!r}），按这一格的默认 {fallback!r} 跑；"
              f"非有限值既不能当窗口用，也不能进响应")
        return fallback
    if _seconds_limited(key) and number > MAX_WINDOW_SECONDS:
        # 秒格的上界必须在这里判：环境变量臂不经过 `_type_ok` / `_sanitize_loaded`，
        # `TASK_WAITING_USER_TIMEOUT_SECONDS=1e18` 是能现读出来的。
        print(f"⚠️ {where} 读出来超出可用上限（{value!r} > {MAX_WINDOW_SECONDS} 秒）："
              f"datetime 装不下这么大的窗口，按这一格的默认 {fallback!r} 跑")
        return fallback
    return max(0.0, number)


def _json_safe(value) -> bool:
    """这个值能不能原样进 `allow_nan=False` 的响应，且不给下游留 `float()` 溢出的雷。

    递归判而不是只看顶层：`{"junk": [Infinity]}` 这种嵌套形状同样会让
    `json.dumps(..., allow_nan=False)` 当场抛。两件事一起判——`json.dumps` 自己只拦
    inf/nan，**不拦**任意大的整数（`10**400` 序列化得好好的），而那种整数一进
    `float()` 就抛 OverflowError。
    """
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, int):
        try:
            float(value)
        except OverflowError:
            return False
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, (list, tuple)):
        return all(_json_safe(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _json_safe(v) for k, v in value.items())
    return False


def _sanitize_credit_benchmark(value) -> dict:
    """把基准价整棵子字典收成"任何一键都进得了 `allow_nan=False` 响应"的形状。

    只修数值子键不够：`currency` 被写成 `Infinity`、或额外多出个 `"junk": Infinity`，
    同样让 `read()` 整份字典在 `GET /v1/config` 序列化时抛。三条口径：
    - 字符串键（`currency`）：类型对就留，不对回默认；
    - 已知数值键：不是有限正数（含 `float()` 溢出）就折回**它自己的**默认，别的键不动；
    - 额外/未知键：`_json_safe` 不过就**丢掉这一键**并说一句。丢单键而不是整表回默认，
      因为这些键本模块不读，回默认会把管理员配好的已知键一起抹掉；留下来的额外键则
      必须证明自己进得了响应、也不会在某天被 `float()` 撞上 OverflowError。
    """
    defaults = DEFAULTS["credit_benchmark"]
    if not isinstance(value, dict):
        return dict(defaults)
    out = {}
    for sub, item in value.items():
        if sub in defaults:
            default = defaults[sub]
            if isinstance(default, str):
                if isinstance(item, str):
                    out[sub] = item
                else:
                    print(f"⚠️ 配置项 credit_benchmark.{sub} 得是字符串（收到 {item!r}），"
                          f"落回默认 {default!r}")
                    out[sub] = default
                continue
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                print(f"⚠️ 配置项 credit_benchmark.{sub} 不是数（收到 {item!r}），"
                      f"落回默认 {default!r}")
                out[sub] = default
                continue
            try:
                number = float(item)
            except OverflowError:
                print(f"⚠️ 配置项 credit_benchmark.{sub} 超出 float 范围（{item!r}），"
                      f"落回默认 {default!r}")
                out[sub] = default
                continue
            if not math.isfinite(number) or number <= 0:
                print(f"⚠️ 配置项 credit_benchmark.{sub} 得是有限的正数（{item!r}），"
                      f"落回默认 {default!r}")
                out[sub] = default
                continue
            # 合法 int 原样保型，与顶层数值格同一口径。
            out[sub] = item
            continue
        if _json_safe(item):
            out[sub] = item
        else:
            print(f"⚠️ 配置项 credit_benchmark 的额外键 {sub!r} 里含非有限值或超出 float "
                  f"范围的数，丢弃这一键：它进不了 allow_nan=False 的响应")
    return out


def _sanitize_loaded(key: str, value):
    """盘上/管理端送来的值**写进缓存之前**的最后一道：坏值折回该格默认、坏子树丢掉。

    为什么修在缓存写入层而不是 `read()`：`read()` 在详情端点的热路径上（每次 GET 都经
    `waiting_timeout_info` 现读一次），在它上面再叠一遍消毒，等于每个请求都重算同一件
    在写入时就该定下来的事；而且 `read()` 返回的本来就是 `_config` 的浅副本（env 覆盖
    与默认值合并都发生在读侧），调用方对它的期待是"当前生效的配置"而不是"盘上原值"，
    在写入层收口不改变任何调用方拿到的语义。真正的写入口只有两个——`restore()` 与
    `apply_update()`——都在这里过一遍，于是 `read()`、`GET /v1/config`、
    `POST /v1/admin/config` 的响应以及全部下游读者天然拿到干净字典。

    两条数值口径：
    - **合法整数原样保型**：`3600` 经 `restore()` 后 `read()` 仍是 int 3600，不统一
      浮点化（getter 出口仍是 float，形状没变）。非负的整数都是精确值；秒格
      （`_SECONDS_KEYS`）里超过 `MAX_WINDOW_SECONDS` 的整数过不了 `_type_ok`，走不到
      这里，真走到了也按默认落，绝不交给 `float()` 静默取整；非秒格（影子护栏）不受
      这条线管，`1e12` 这种额度原样留下。
    - 负数的收口沿用 `_finite_or_close` 的同一条 `max(0.0, ...)`：这些数值格的 getter
      本来就把负数读成 0，缓存里存 0 只是让 `read()` 与 getter 看到同一个数。
    """
    if key == "credit_benchmark":
        return _sanitize_credit_benchmark(value)
    if key == "fetch_allowed_hosts":
        return _normalize_fetch_hosts(value)
    default = DEFAULTS[key]
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        if isinstance(value, int) and not isinstance(value, bool):
            if value < 0:
                return 0.0
            if _seconds_limited(key) and value > MAX_WINDOW_SECONDS:
                print(f"⚠️ 配置项 {key} 的整数 {value!r} 超出 datetime 可用上限"
                      f"（{MAX_WINDOW_SECONDS} 秒），落回默认 {default!r}")
                return default
            return value
        return _finite_or_close(value, float(default), f"配置项 {key}", key=key)
    return value


def stream_no_reader_grace_seconds() -> float:
    """流式"没有活读者"的宽限期（秒）——全场唯一读数口径，默认 120（省钱的是轮次边界钱闸，不是这个窗口）。

    优先级与注册开关同一套路：环境变量 STREAM_NO_READER_GRACE_SECONDS 显式设了
    说话（每次现读，改了不必重启），否则 data/config.json 的
    stream_no_reader_grace_seconds 说了算。读不懂的值退回默认，和"配置文件坏了
    先备份再按默认跑"是同一条纪律。0 是合法值：等于"轮次边界上不留窗口"。
    """
    raw = os.getenv("STREAM_NO_READER_GRACE_SECONDS", "").strip()
    if raw:
        try:
            return _finite_or_close(float(raw),
                                    float(DEFAULTS["stream_no_reader_grace_seconds"]),
                                    "宽限期（环境变量 STREAM_NO_READER_GRACE_SECONDS）",
                                    key="stream_no_reader_grace_seconds")
        except ValueError:
            print(f"⚠️ 环境变量 STREAM_NO_READER_GRACE_SECONDS 不是数（{raw!r}），按下面的口径跑")
    with _lock:
        value = _config.get("stream_no_reader_grace_seconds",
                            DEFAULTS["stream_no_reader_grace_seconds"])
    return _finite_or_close(value, float(DEFAULTS["stream_no_reader_grace_seconds"]),
                            "宽限期（data/config.json）",
                            key="stream_no_reader_grace_seconds")


def approval_ticket_ttl_seconds() -> float:
    """一次性审批票据的有效期（秒）——全场唯一读数口径，默认 60（与 v0.31 起的常量同值）。

    优先级与宽限期同一套路：环境变量 APPROVAL_TICKET_TTL_SECONDS 显式设了说话
    （每次现读，改了不必重启），否则 data/config.json 的 approval_ticket_ttl_seconds
    说了算。读不懂的值退回那一格默认，和"配置文件坏了先备份再按默认跑"同纪律。
    0 是合法值（每次都要现批）；负数按 0 收——"倒着走的窗口"不是策略，是写错了。
    """
    raw = os.getenv("APPROVAL_TICKET_TTL_SECONDS", "").strip()
    if raw:
        try:
            return _finite_or_close(float(raw),
                                    float(DEFAULTS["approval_ticket_ttl_seconds"]),
                                    "审批票据有效期（环境变量 APPROVAL_TICKET_TTL_SECONDS）",
                                    key="approval_ticket_ttl_seconds")
        except ValueError:
            print(f"⚠️ 环境变量 APPROVAL_TICKET_TTL_SECONDS 不是数（{raw!r}），按下面的口径跑")
    with _lock:
        value = _config.get("approval_ticket_ttl_seconds",
                            DEFAULTS["approval_ticket_ttl_seconds"])
    return _finite_or_close(value, float(DEFAULTS["approval_ticket_ttl_seconds"]),
                            "审批票据有效期（data/config.json）",
                            key="approval_ticket_ttl_seconds")


def task_waiting_user_timeout_seconds() -> float:
    """一条 `waiting_user` 停点的作废窗口（秒）——全场唯一读数口径，**默认 0 = 永不作废**。

    读数优先级与 TTL 那两格同一套路：环境变量 TASK_WAITING_USER_TIMEOUT_SECONDS 显式设了
    说话（每次现读，改了不必重启），否则 data/config.json 的
    task_waiting_user_timeout_seconds 说了算。读不懂的值退回 0 并说一句，与"配置文件坏了
    先备份再按默认跑"同纪律。

    ⭐ 这一格里 **0 是"关"**，而它上面两格的 0 是"不留窗口"——同一个数字两种方向，
    因为这一格管的是销毁（作废掉用户手里那个还能续的断点），不是宽限：负数与坏值一律
    收成 0（关），绝不许"读不懂就按一个正数作废"。要开启就设正数（例：86400 = 一天）。
    判据与落点见 `app/agents/task_store.py` 的 `is_waiting_timed_out` / `expire_parked_task`。
    """
    raw = os.getenv("TASK_WAITING_USER_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            # 兜回 0.0 = 这一格的"关"：坏值绝不许倒向"到点销毁用户手里那个还能续的断点"。
            return _finite_or_close(float(raw),
                                    float(DEFAULTS["task_waiting_user_timeout_seconds"]),
                                    "停点作废窗口（环境变量 TASK_WAITING_USER_TIMEOUT_SECONDS）",
                                    key="task_waiting_user_timeout_seconds")
        except ValueError:
            print(f"⚠️ 环境变量 TASK_WAITING_USER_TIMEOUT_SECONDS 不是数（{raw!r}），按下面的口径跑")
    with _lock:
        value = _config.get("task_waiting_user_timeout_seconds",
                            DEFAULTS["task_waiting_user_timeout_seconds"])
    return _finite_or_close(value, float(DEFAULTS["task_waiting_user_timeout_seconds"]),
                            "停点作废窗口（data/config.json）",
                            key="task_waiting_user_timeout_seconds")


def credit_benchmark() -> dict:
    """积分基准价（¥→积分的锚）——全场唯一读数口径，credits.py 只从这里取。

    逐键消毒而不是整包信任：data/config.json 是可能被手改的盘上文件，一个坏值
    （字符串价、负价、0 比例）会把倍率与影子整张表算成鬼话。任何一键读不懂就
    单独退回默认并 print 一句人话——与"配置文件坏了先备份再按默认跑"同纪律。
    """
    raw = read().get("credit_benchmark")
    defaults = DEFAULTS["credit_benchmark"]
    if not isinstance(raw, dict):
        return dict(defaults)
    out = dict(defaults)
    for key, default in defaults.items():
        value = raw.get(key)
        if key == "currency":
            text = str(value or "").strip().upper()
            if text:
                out["currency"] = text
            continue
        # 这一支的坏值落点与前面那些 getter 不同：逐键各自退回**它自己的默认**（下面那句
        # `continue`），不是某个全表统一的数。判据也要比"是不是正数"多一格——
        # `float(nan) <= 0` 是 False，nan 与 inf 都能从"正数"这一关放行到出口，
        # 而它们进锚价等于把整张倍率表算成不可复现的鬼话（inf 还会把响应带崩）。
        # `float(10**400)` 抛的 OverflowError 必须进 except：盘上一个 400 位整数就能
        # 让 `GET /v1/config` 与这段读数一起 500，而它不是"小于 0"也不是"非有限"。
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if value is not None and value != default:
                print(f"⚠️ 基准价 {key} 得是有限的正数（收到 {value!r}），按默认 {default} 跑")
            continue
        try:
            number = float(value)
        except OverflowError:
            if value != default:
                print(f"⚠️ 基准价 {key} 超出 float 范围（整数过大），按默认 {default} 跑")
            continue
        if not math.isfinite(number) or number <= 0:
            if value is not None and value != default:
                print(f"⚠️ 基准价 {key} 得是有限的正数（收到 {value!r}），按默认 {default} 跑")
            continue
        out[key] = number if not isinstance(default, int) else value
    return out


def credit_shadow_daily_limit() -> float:
    """每日影子积分护栏（0 = 不拦截，默认）。

    优先级与宽限期同一套路：环境变量 CREDIT_SHADOW_DAILY_LIMIT 显式设了说话
    （每次现读，改了不必重启），否则 data/config.json 说了算。负数按 0 收——
    "负的额度"不是"倒贴"，是写错了，别让它把每一轮都拦死。非有限值也收进 0：
    这一格的 0 同时是默认与"关"（只算不拦），坏值落在这里不新造第二个方向；
    换成别的数就不行，`inf` 穿出去是一扇永远合不上的闸，`nan` 穿出去会崩响应。

    #149：这一格是**计数**不是秒，不归 `MAX_WINDOW_SECONDS` 管——`1e12` 是配得出来
    的额度，必须原样读到（盘上、环境变量两路都是）。它的取值范围上界 owner 是
    IEEE-754 双精度浮点本身：`float()` 不溢出、且有限，就是合法值；`10**400` 这种
    超出 float 的整数仍由形状关 / 捕获面挡在默认外。
    """
    raw = os.getenv("CREDIT_SHADOW_DAILY_LIMIT", "").strip()
    if raw:
        try:
            return _finite_or_close(float(raw),
                                    float(DEFAULTS["credit_shadow_daily_limit"]),
                                    "影子护栏（环境变量 CREDIT_SHADOW_DAILY_LIMIT）",
                                    key="credit_shadow_daily_limit")
        except ValueError:
            print(f"⚠️ 环境变量 CREDIT_SHADOW_DAILY_LIMIT 不是数（{raw!r}），按下面的口径跑")
    with _lock:
        value = _config.get("credit_shadow_daily_limit",
                            DEFAULTS["credit_shadow_daily_limit"])
    return _finite_or_close(value, float(DEFAULTS["credit_shadow_daily_limit"]),
                            "影子护栏（data/config.json）",
                            key="credit_shadow_daily_limit")


def fetch_allowed_hosts() -> list:
    """http_fetch 的主机白名单（全场唯一读数口径）——归一化、去重、保序。

    缓存里的值在写入口已经过 `_normalize_fetch_hosts`，这里再走同一支是防御
    手改盘上文件绕过校验的那条路（restore 的消毒与 getter 的消毒是同一函数、
    同一判据，不可能漂）。返回副本：调用方不许拿到 `_config` 里的活列表。
    """
    with _lock:
        value = _config.get("fetch_allowed_hosts", DEFAULTS["fetch_allowed_hosts"])
    return _normalize_fetch_hosts(value)


def apply_update(partial: dict) -> Tuple[dict, list]:
    """合并管理端的改动并落盘。返回 (改后的完整配置, 被拒的键列表)。

    未知键不写入也不报错——回一份"哪些没被接受"比 422 整单拒绝友好，因为
    界面上"保存"的人想知道的是改成了什么；类型不对同样拒，但只拒那一个键。
    值域不对（_VALUE_CHECKS，目前只有 update_repo 的 owner/repo 白名单）也一样：
    它出站时会拼进服务端要请求的 URL，放行任意字符串等于开一条 SSRF。
    秒格（`_SECONDS_KEYS`）里超出 `MAX_WINDOW_SECONDS` 的数同样拒：`10**18+1` 交给
    `float()` 会被静默取整成 `1e18`，那是**改值**不是校验，运维在管理端看到的必须是
    "这一键没被接受"。非秒格（影子护栏）不按这条秒线拒，`1e12` 这类额度照收；超出
    float 范围的整数（`10**400`）仍然是 `_type_ok` 的 OverflowError 关拒收。
    """
    rejected = []
    with _lock:
        for key, value in (partial or {}).items():
            if key not in MUTABLE_KEYS:
                rejected.append(key)
                continue
            if not _type_ok(value, DEFAULTS[key], key=key):
                rejected.append(key)
                continue
            if not _value_ok(key, value):
                rejected.append(key)
                continue
            # 校验过了也要过消毒：JSON 的 `1e400` / `Infinity` 会被解析成 inf，
            # `_type_ok` 收（它判的是形状，inf 是 float），原样存进缓存就是让
            # `POST /v1/admin/config` 的响应与下一次 `GET /v1/config` 一起 500。
            _config[key] = _sanitize_loaded(key, value)
        _write()
        out = dict(_config)
    env_open = _env_registration_open()
    if env_open is not None:
        out["registration_open"] = env_open
    return out, rejected


def log_config_summary() -> None:
    """启动时一行，说人话：注册开没开、更新去哪儿找。出事的人在看日志，不用猜。"""
    cfg = read()
    reg = "开放" if cfg["registration_open"] else "关闭（默认，管理员建号）"
    env_hit = _env_registration_open() is not None
    note = "，由环境变量 REGISTRATION_OPEN 决定" if env_hit else ""
    print(f"⚙️ 运行时配置：注册{reg}{note}；更新仓库 {cfg['update_repo']}"
          f"（文件 {_default_path()}）")


restore()
