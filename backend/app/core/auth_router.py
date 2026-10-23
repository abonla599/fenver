"""注册、登录与管理端点。

这里是身份存储（app/core/auth.py）与 HTTP 之间唯一的一层，规则三条：
1. 对外说话保守。登录失败只有一句"用户名或密码不正确"——区分"没这个用户"和
   "密码错"，就把这个免凭据端点变成了用户名探测器；存储层内部也刻意不分开
   （AuthError 只带那一句，见 auth.login）。注册端的"该用户名已存在"是有意
   保留的实话（改名是用户自己能解决的事），但邀请码退役之后它前面再没有闸门，
   所以这句改由**按真实 IP 计费**来限制——见 _note_failure 的口径。
2. 响应按字段白名单出。存储层的记录带着 pw_hash、tokens 和内建的 username_lc，
   顺手 return record 等于把口令摘要与会话令牌摘要交给前端与日志。
3. 凭据明文只在"必须被看见"的那一次出现：令牌见于注册与登录的响应，以及管理员
   轮换的响应。任何端点都不许复述调用方刚提交的密码或令牌。
"""
import os
import re
from collections import defaultdict
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core import authz, usage
# v0.34.0 D1：限流账"住在哪儿"抽成一层后端（默认内存，Redis 那条路不落生产）。
from app.core import ledger_backend
# v0.32 W4：搜索这本账独立一份文件、独立一个读面（口径见 core/search_usage.py 头部）。
from app.core import search_usage
# v0.24 T1.4：注册闸门与配置端点读的是同一份 config_store，不在路由层留第二默认值。
from app.core import config_store
# R4a 个人用量面的 provider 显示名要查这份配置——只查 label 一个字段，见 _my_provider_name。
from app.core import providers
# v0.24 T3.4：管理端每一次账号改动都往 audit.jsonl 追加一条（只增、脱敏在这一层里）。
from app.core import audit
# 只 import AuthError：找回那一支要不要计费看的是 e.charge 这个显式标记，
# 不再需要把 RESET_FAIL 那句文案搬进路由层（终审 F4）。
from app.core.auth import AuthError, DEVICE_SECRET_INACTIVE_SECONDS, hash_secret
from app.core.authz import CurrentPrincipal, Principal, RequireAdmin

router = APIRouter(tags=["身份"])

# 猜密码的代价：同一来源在窗口内失败太多次就拒一拒。进程内计数即可——
# 重启即清零是可接受的，因为真正的凭据是 bcrypt 校验与长密码。
# 账本只记失败，不记格式错（用户名打错字、密码太短）：那是当事人自己能改好的事，
# 把它算进预算只会让唯一的登录入口被自己的手滑锁死。
# **任何成功都不还回预算**（终审 F3 删掉了 register/login 成功路径上的两处
# `_FAILS.pop(ip)`）。旧契约"密码对了就说明来路正当"实测是假的：一台机器、一枚来源 IP，
# 只要穿插"自己的号成功一次"，撞名枚举能跑到 45 次 409 / 0 次 429，口令猜测能跑到
# 36 次错 / 0 次 429——因为"成功"本身就是这个免凭据端点上随手可得的东西（猜对自己的
# 口令、再注册一个小号）。代价如实认：同一个十分钟窗口里连续打错十次的真人要等窗口过去，
# 话术已经是"尝试次数过多，请稍后再试"。判据是两条反转过的锁
# （test_a_correct_login_does_not_pay_back_the_failure_budget 与
# test_a_successful_register_does_not_pay_back_the_failure_budget）。
# day 参数只认这一种写法；放宽=任何垃圾都换回一份 200 空表
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

FAILURE_WINDOW_SECONDS = 600
MAX_FAILURES_PER_WINDOW = 10

# 注册开放之后，"能建多少个号"是唯一的成本闸门：按真实来源限成功数。
# 记成功而不是记失败，因为失败（撞名）本来就是零成本，而一个脚本可以无限撞名
# 却一个号也建不出来；能真正花钱的是"注册成功 + 拿去对话"。
REGISTER_WINDOW_SECONDS = 86400
MAX_REGISTRATIONS_PER_SOURCE = 3

# 自助改密走的是同一套口径，而且比注册更需要它：三题是全站公开的常量，"答对"不再
# 说明对面是本人，只说明他猜中了。连着猜中三次的人是另一个人，而每一次成功都把
# 名下所有会话令牌作废——对被猜的人而言这是真实的伤害（每台设备都掉线）。
# 上限 3 = 本人忘密改一次 + 反悔改回来 + 再出一次意外，之后只能找管理员。
RESET_WINDOW_SECONDS = 86400
MAX_RESETS_PER_SOURCE = 3

# 来源键取自 CF-Connecting-IP：域名必经 Cloudflare，而它会把真实访客 IP 写在
# 这个头上；后端只监听 127.0.0.1:8000、外部唯一入口就是 cloudflared，没有旁路
# 可以伪造这个头。取不到该头时退回 uvicorn 看到的对端地址（本机直连与测试）。
# 以前只用 request.client.host，经过隧道后恒为 127.0.0.1——全网共用一个桶，
# 一个人手滑就能把所有人挡在门外，所以这既是功能也是修 bug。
MAX_TRACKED_SOURCES = 4096


class _Ledger(defaultdict):
    """一本限流账：还是原来那个 `defaultdict(list)`，只是多背一个后端认得的名字。

    为什么仍要是 dict：conftest 与既有用例直接 `_FAILS.clear()`、`sum(len(v) for v in
    _FAILS.values())`——账本对象自己就是内存后端的存储，这不是将就，是"抽一层但不换一套
    事实来源"。名字那半边是给 Redis 后端拼键用的：没有它，两本账的同名来源键会在共享存储
    里撞成一本（`_Ledger.name` 缺失时 `RedisLedgers._key` 直接抛，不静默拼键）。
    """

    def __init__(self, name: str) -> None:
        super().__init__(list)
        self.name = name


# 后端在这一层选，不在每个调用点选：八本账共用一条"记/读/裁"的通路，多进程时只需要
# 换这一个对象；默认内存实现（见 ledger_backend 头上的两条取舍）。
_LEDGER_BACKEND = ledger_backend.select_backend()


def _window_of(ledger) -> float:
    """这本账的窗口，唯一出处是 `_LEDGERS`（"有哪几本账"的同一份清单）。

    记一次也要窗口，是因为共享存储里桶的寿命就是窗口：让调用点各抄一份数字，就等于给
    "两本账窗口不一致"这种错位留了第二个来源——新加一本没登记进 `_LEDGERS` 时会当场炸，
    那正是我们要的症状（不炸就是只胀不收）。
    """
    for known, window in _LEDGERS:
        if known is ledger:
            return window
    raise RuntimeError(f"限流账 {getattr(ledger, 'name', ledger)!r} 没登记进 _LEDGERS")


_FAILS = _Ledger("fails")
_REGISTERS = _Ledger("registers")
_RESETS = _Ledger("resets")
# 猜找回答案的失败单独一册（窗口与上限沿用 FAILURE_WINDOW_SECONDS /
# MAX_FAILURES_PER_WINDOW，换的只是账本）。
# 拆开之后仍然站得住的两条理由：一，猜口令与猜三题是两种不同的猜测面，共用一格预算时
# 前者会把后者顶满，一个人猜错九次之后连自己的密码都不许再试一次；二，429 落在哪本账上
# 要看得出来，否则运维没法从 Retry-After 区分"刚才有人在枚举用户名"还是"有人在猜找回答案"。
# 找回这本账任何成功路径都不许清，改密成功也不行（判据：
# test_no_other_success_pays_off_the_guessing_ledger）。
# 当初拆账的**原始**理由是"_FAILS 会被登录或注册成功清空，猜答案的人随手就能拿到一次
# 那样的成功"——终审 F3 把那两处 pop 删掉之后，那半边理由已经不成立了，留着的是上面这两条。
# 别把这段再读成"那本会被成功清、这本不会"。
_RESET_FAILS = _Ledger("reset_fails")

# 账号维度的找回失败账（审查 #6，2026-09-23）：_RESET_FAILS 按来源 IP 记账，
# 而三题是全站固定的低熵常量（手机号后四位只有 10^4 种）——换得起 IP 的人
# （NAT 后、秒拨代理）对**同一个账号**可以无限猜。这本按"被猜的账号"计，
# 窗口与上限沿用 FAILURE_WINDOW_SECONDS / MAX_FAILURES_PER_WINDOW。
# 两条口径说清楚：
# ① 查无此人的失败同样记进它名下那个键——与存储层"同一句话、同一格预算"对齐，
#    于是"这个用户名锁不锁"对任何账号都一样，429 不泄露"这号存在吗"。
# ② 代价如实认： Anyone 用 10 次错答案就能把真号主的自助找回挡 10 分钟
#    （找回锁不碰登录，管理员也能人工解）。低熵三题下"能被猜"比"能被锁"更先
#    发生，所以这笔交换是划算的。
_RESET_FAILS_BY_ACCOUNT = _Ledger("reset_fails_by_account")

# 每本账配自己的窗口：_prune 是内存闸门，拿十分钟那把尺子去过 24 小时那两本，就是
# 在来源数超过 4096 时把整桶有效记录提前丢掉——配额被悄悄放宽，是一条 fail-open。
# 这份元组同时是"账本有哪几本"的唯一清单：新加一本忘了加进来，就是只胀不收。
# 第五本：聊天节流。键是 `IP + 登录身份`，不是单独 IP——同一家共用一个出口的人
# 不该互相挡；而"换个身份就能重来一轮"是这个选择自带的口子，写在这行是为了别把
# 它当成密不透风的防线（真正花钱的那本在 app/core/usage.py，两件事互补）。
CHAT_WINDOW_SECONDS = 60
MAX_CHAT_CALLS_PER_WINDOW = 20
_CHATS = _Ledger("chats")

# ⭐ 工单 #38：`/v1/agent/orchestrate` 的**尝试**账，与付费那本分开。
#
# 为什么必须有第二本：从 #38 起，付费那本只在该端点**真的要开工**时才记（新任务在
# 规划前、续跑在认领成功后），于是"这条答案没被采用"`accepted:false` 的那一族不再扣
# 用户自己的聊天额度——那正是工单要的"没花钱就不记账"。但没有这本的话，一条 retry
# 循环（断点已被别人接走、任务正在飞）可以不花一分钱地把端点敲到天荒地老：每条仍要
# 读一次任务表、打一行日志、走一遍 HTTP。这本只拦"刷"，不拦"钱"：上限 60 秒 60 次，
# 比付费那本（20 次）宽三倍——正常人撞到付费墙之前根本碰不到它。
ORCHESTRATE_ATTEMPT_WINDOW_SECONDS = 60
MAX_ORCHESTRATE_ATTEMPTS_PER_WINDOW = 60
_ORCHESTRATE_ATTEMPTS = _Ledger("orchestrate_attempts")

# v0.34.0：reauth 是全站唯一"不带会话凭据、只带续命凭据"的公开端点，所以它自己
# 需要两本账。为什么是两本而不是复用 _FAILS：
# ① _FAILS 是"猜口令"的账（按来源 IP，十分钟十次）。reauth 猜的是 256 位随机值，
#    爆破在数学上不成立，但**定向**使坏成立：拿到某把 secret 的人可以拿它换会话，
#    而 secret 是一次性的——用错一次就废。给这条路径单开一本，是为了让 429 落在
#    哪本账上仍然读得出来（与 _RESET_FAILS 拆账同一条理由）。
# ② 按 secret 摘要记：换 IP 的人（秒拨、共享 NAT）对同一把 secret 可以无限试，
#    这正是 _RESET_FAILS_BY_ACCOUNT 当年拆出账号维度的同一个形状。
# 查无此 secret 的失败同样记进它自己那个键（键就是摘要本身，不含身份信息），
# 于是 429 不泄露"这把手里有没有对应的账号"。
MAX_REAUTH_FAILURES_PER_WINDOW = 10
_REAUTH_FAILS = _Ledger("reauth_fails")
_REAUTH_BY_SECRET = _Ledger("reauth_by_secret")


def _reauth_secret_key(digest: str) -> str:
    """账本键取摘要前 16 位：够区分（sha256 前 64 bit 碰撞概率可忽略），
    又不把整枚摘要写进内存里那份可被 dump 的键表。"""
    return (digest or "")[:16] or "anon"


def _reauth_throttled(ip: str, digest: str) -> bool:
    moment = _now()
    _prune(moment)
    key = _reauth_secret_key(digest)
    return (len(_recent(_REAUTH_FAILS, ip, FAILURE_WINDOW_SECONDS, moment))
            >= MAX_REAUTH_FAILURES_PER_WINDOW
            or len(_recent(_REAUTH_BY_SECRET, key, FAILURE_WINDOW_SECONDS, moment))
            >= MAX_REAUTH_FAILURES_PER_WINDOW)


def _note_reauth_failure(ip: str, digest: str) -> None:
    moment = _now()
    _note(_REAUTH_FAILS, ip, moment)
    _note(_REAUTH_BY_SECRET, _reauth_secret_key(digest), moment)


def _chat_key(ip: str, user_id: str) -> str:
    return f"{ip}|{user_id or 'anon'}"


_LEDGERS = ((_FAILS, FAILURE_WINDOW_SECONDS),
            (_REGISTERS, REGISTER_WINDOW_SECONDS),
            (_RESETS, RESET_WINDOW_SECONDS),
            (_RESET_FAILS, FAILURE_WINDOW_SECONDS),
            (_RESET_FAILS_BY_ACCOUNT, FAILURE_WINDOW_SECONDS),
            (_CHATS, CHAT_WINDOW_SECONDS),
            (_ORCHESTRATE_ATTEMPTS, ORCHESTRATE_ATTEMPT_WINDOW_SECONDS),
            (_REAUTH_FAILS, FAILURE_WINDOW_SECONDS),
            (_REAUTH_BY_SECRET, FAILURE_WINDOW_SECONDS))


def _store():
    """身份库要从 authz 现取，不能在 import 时绑死。

    中间件读的是 authz.auth_store（测试换库也只换它）。端点若绑住 app.core.auth
    里那个单例，enforced 模式下就会出现"中间件认 A 库、注册端点写 B 库"，
    发出去的令牌连它自己都解不出来——而这恰是路由层最容易悄悄错位的地方。
    """
    return authz.auth_store


def _now() -> float:
    """后端自己的时钟，独立成函数这一条不变：为的是"窗口过期"这一条要测得到。

    内存实现给单调钟（本机时钟跳变不该把窗口拉长或缩短）；共享存储那侧必须给水钟
    （monotonic 是本进程开机以来的秒数，两个 worker 的数互相不可比）。这条差异写在
    ledger_backend 的头，不在这里再抄一份。
    """
    return _LEDGER_BACKEND.now()


def _recent(ledger, ip: str, window: float, moment: float) -> list:
    """读"这一键在窗口内记了几次"。判据一条没变，只是记账本的动作交给后端（D1）。

    留在模块层的理由：它同时是既有用例与 conftest 认的那个入口，而窗口的唯一出处在
    `_LEDGERS`——把这两件事搬进后端会让"哪本账用哪个窗口"再没有一个可读的地方。
    """
    return _LEDGER_BACKEND.recent(ledger, ip, window, moment)


def _note(ledger, key: str, moment: float = None) -> None:
    """记一次（失败/注册/找回/聊天/reauth 共一条通路）。

    窗口不在调用点抄，从 `_LEDGERS` 现取：共享存储里桶的寿命就是这个窗口，抄错一个数字
    在内存后端上毫无症状、在 Redis 后端上就是一本活得比判据长的账。
    `moment` 留给"同一件事要同时记进两本账"的那一处（reauth 的 IP 账与 secret 账）：
    两笔取同一个时刻，注入式假时钟下也不会出现"同一件事在两本账里差一个刻度"。
    """
    _LEDGER_BACKEND.note(ledger, key, _window_of(ledger),
                         _now() if moment is None else moment)


def _prune(moment: float) -> None:
    """来源数超出上限时丢掉"整桶都已过期"的那些键——八本账都要扫，且各按自己的窗口过。

    触发条件是**内存**上限，不是安全窗口，所以两件事都不能凑：漏掉一本就是只胀不收，
    拿统一的最短窗口去过 24 小时的那两本则会在来源数超过 4096 时把整桶有效记录提前丢掉
    （注册与改密的配额被悄悄放宽，那是一条 fail-open）。窗口就在 _LEDGERS 里跟账本绑在
    一起，判据见 test_prune_uses_each_ledgers_own_window。

    怎么扫交给后端：内存侧是 len(dict) 与整桶判空，共享存储侧对应的是键数与整桶出窗
    （两条语义必须一致，由 D1 的成对用例钉，不靠"看着像"）。
    """
    _LEDGER_BACKEND.prune(_LEDGERS, moment, MAX_TRACKED_SOURCES)


def _throttled(ip: str) -> bool:
    """登录与注册共用的那本失败账（猜口令、撞名）。找回流程不看这本，见下。"""
    moment = _now()
    _prune(moment)
    return len(_recent(_FAILS, ip, FAILURE_WINDOW_SECONDS, moment)) >= MAX_FAILURES_PER_WINDOW


def _reset_throttled(ip: str) -> bool:
    """找回流程自己那本失败账：同一个窗口、同一个上限，只是另一本账——
    登录成功、注册成功、改密成功都清不到它（为什么必须分开，见 _RESET_FAILS 上面那段）。
    """
    moment = _now()
    _prune(moment)
    return len(_recent(_RESET_FAILS, ip, FAILURE_WINDOW_SECONDS,
                       moment)) >= MAX_FAILURES_PER_WINDOW


def _note_failure(ip: str) -> None:
    _note(_FAILS, ip)


def _note_reset_failure(ip: str) -> None:
    _note(_RESET_FAILS, ip)


def _reset_account_key(username: str) -> str:
    """账号键与库里 username_lc 同一套归一（strip + casefold），截断到 64 字符：
    键来自免凭据的裸输入，不给它一个尺寸上限，光靠往字典里塞超长键就能把内存吃掉。
    """
    return (username or "").strip().casefold()[:64]


def _reset_account_throttled(username: str) -> bool:
    moment = _now()
    _prune(moment)
    return (len(_recent(_RESET_FAILS_BY_ACCOUNT, _reset_account_key(username),
                        FAILURE_WINDOW_SECONDS, moment))
            >= MAX_FAILURES_PER_WINDOW)


def _note_reset_failure_account(username: str) -> None:
    _note(_RESET_FAILS_BY_ACCOUNT, _reset_account_key(username))


def _registrations_full(ip: str) -> bool:
    moment = _now()
    _prune(moment)
    return (len(_recent(_REGISTERS, ip, REGISTER_WINDOW_SECONDS, moment))
            >= MAX_REGISTRATIONS_PER_SOURCE)


def _note_registration(ip: str) -> None:
    _note(_REGISTERS, ip)


def _resets_full(ip: str) -> bool:
    moment = _now()
    _prune(moment)
    return (len(_recent(_RESETS, ip, RESET_WINDOW_SECONDS, moment))
            >= MAX_RESETS_PER_SOURCE)


def _note_reset(ip: str) -> None:
    _note(_RESETS, ip)


LOOPBACK = ("127.0.0.1", "::1")


def _client_ip(request: Request) -> str:
    # 只信 Cloudflare 那一个头。X-Forwarded-For 是一条可被追加的链，取首项等于
    # 取攻击者写的第一句假话；cf-connecting-ip 由边缘改写，才是可信来源。
    #
    # 但"边缘改写过"这件事得有个判据，否则它只是一句信仰：现网的形状是后端只绑
    # 回环、公网流量必须经 cloudflared 从 127.0.0.1 转进来，所以**对端不是回环**
    # 就意味着这个请求没走隧道——那它带来的 cf-connecting-ip 是客户端自己写的。
    # 少这一道，哪天有人把 PORT/HOST 改成对外监听，五本限流账就同时变成可绕过的。
    peer = request.client.host if request.client else ""
    real = request.headers.get("cf-connecting-ip", "").strip()
    if real and peer in LOOPBACK:
        return real
    return peer or "unknown"




def _sliding_peek(ledger, key: str, window: float, cap: int) -> bool:
    """只看，不记账。所有滑动窗口账共用这一条判据（chat / orchestrate 尝试）。"""
    moment = _now()
    _prune(moment)
    return len(_recent(ledger, key, window, moment)) < cap


def _sliding_note(ledger, key: str) -> None:
    """记一次。窗口从 `_LEDGERS` 现取（见 `_note` 的注释：共享存储里桶的寿命就是它）。"""
    _note(ledger, key)


def _sliding_retry_after(ledger, key: str, window: float) -> int:
    moment = _now()
    stamps = _recent(ledger, key, window, moment)
    if not stamps:
        return int(window)
    return max(1, int(window - (moment - min(stamps))) + 1)


def chat_allowed(ip: str, user_id: str) -> bool:
    """只看，不记账。记账是 note_chat()，两件事分开才能在"挡下时不花钱"上测得出来。"""
    return _sliding_peek(_CHATS, _chat_key(ip, user_id), CHAT_WINDOW_SECONDS,
                         MAX_CHAT_CALLS_PER_WINDOW)


def note_chat(ip: str, user_id: str) -> None:
    """记一次尝试。调用点在**放行之后、叫模型之前**，所以失败的、模型报错的那次
    一样占额度——成功还不还预算是这四本旧账定下来的裁决，第五本沿用。"""
    _sliding_note(_CHATS, _chat_key(ip, user_id))


def chat_retry_after(ip: str, user_id: str) -> int:
    return _sliding_retry_after(_CHATS, _chat_key(ip, user_id), CHAT_WINDOW_SECONDS)


def _too_many(retry_after: int) -> HTTPException:
    return HTTPException(status_code=429, detail="尝试次数过多，请稍后再试",
                         headers={"Retry-After": str(retry_after)})


def throttle_paid_upstream(request: Request, principal) -> None:
    """凡是会真金白银叫一次上游模型的入口，都走这一本账：60 秒 20 次，键 IP + 登录身份。

    顺序必须是**先判断、再记账、才叫模型**——反过来写，被挡下的那一次也会把
    额度花出去，而那正是这本账要挡的事。

    为什么从 `app/main.py` 的那条私有 helper 提到这里：`/v1/chat` 与 `/v1/chat/stream`
    一直挂着这本账，而 `/v1/memory/add?summarize=true`（摘要走 chat-completion）与
    `/v1/memory/search`（查询走云端嵌入）同样出网、同样花钱，却一条都不记账。
    一个注册用户光发记忆接口就能绕开全站唯一的额度闸门，等于限流形同虚设。
    三个公开原语（`chat_allowed`/`note_chat`/`chat_retry_after`）的**行为**一格未动：
    工单 #38 只把它们内部并到同一条 `_sliding_*` 实现上（第二本窗口账要复用同一条
    判据），`backend/tests/test_chat_throttle.py` 钉的还是那三件事。

    ⭐ 工单 #38：`/v1/agent/orchestrate` 不再在**进入编排器之前**调它，而是把一个
    闭包交给编排器，一次 run **至多记一次**（两个候选时刻：新任务在规划之前、续跑在
    认领成功之后；见 `orchestrator.run` 的 `charge_upstream`）。被拒的续跑
    （`accepted:false` 那一族）因此不再吃这本账；防刷由另一本
    `throttle_orchestrate_attempt` 负责。
    """
    ip = _client_ip(request)
    if not chat_allowed(ip, principal.user_id):
        raise _too_many(chat_retry_after(ip, principal.user_id))
    note_chat(ip, principal.user_id)


def _cookie_secure() -> bool:
    """Cookie 的 Secure 开关：默认开，只有显式 AUTH_COOKIE_SECURE=0 才降级。

    会话 Cookie 与续命凭据 Cookie 共用这一个判据，理由与 _attach_session 相同：
    忘配 env 的方向必须是 Secure=on，降级只能是本机/测试主动做的事。
    """
    return os.getenv("AUTH_COOKIE_SECURE", "1").strip() != "0"


def throttle_orchestrate_attempt(request: Request, principal) -> None:
    """`/v1/agent/orchestrate` 每一条**尝试**都过的闸：60 秒 60 次，键 IP + 登录身份。

    它记的不是钱（钱那一半在 `throttle_paid_upstream`，只在真开工时记），而是"敲了
    几次门"。两条账分开的理由见 `_ORCHESTRATE_ATTEMPTS` 上面那段：被拒的续跑不该扣
    用户自己的聊天额度，但也不能因此变成一条不限次的免费信道。顺序同付费那本：
    先判断、再记账、才进编排器。
    """
    ip = _client_ip(request)
    key = _chat_key(ip, principal.user_id)
    if not _sliding_peek(_ORCHESTRATE_ATTEMPTS, key,
                         ORCHESTRATE_ATTEMPT_WINDOW_SECONDS,
                         MAX_ORCHESTRATE_ATTEMPTS_PER_WINDOW):
        raise _too_many(_sliding_retry_after(_ORCHESTRATE_ATTEMPTS, key,
                                             ORCHESTRATE_ATTEMPT_WINDOW_SECONDS))
    _sliding_note(_ORCHESTRATE_ATTEMPTS, key)


def _attach_session(response: Response, token: str) -> None:
    """把一枚刚被请求头出示过的凭据写进 httpOnly 会话 Cookie。

    全服务端**只有 /v1/auth/adopt 一个调用方**——登录与注册刻意不设**会话**
    Cookie：
    ① 那几个公开端点的响应体契约（token 字段）一个字都不动，限流/枚举那套
       按来源计费的判据也不跟着漂移；
    ② 更要紧的是挡会话固定：若跨站的 login 响应能直接种会话 Cookie，攻击者就能
       在自己页面上用**他自己的**账号把受害者浏览器钉在攻击者的会话上。会话 Cookie
       只从 adopt 出来，而 adopt 要求请求头里那枚凭据——跨站表单发不出
       Authorization 头，这条缝就是死的。（v0.35.0 #143② 的续命凭据 Cookie
       走 _store_device_secret，那是另一类凭据、另一道生命周期，不在这里。）

    secure 默认开（现网只走 cloudflared HTTPS）；本机 http 直连与测试用
    AUTH_COOKIE_SECURE=0 显式降级——降级必须是主动行为，忘配不会 fail-open。
    """
    response.set_cookie(
        key=authz.SESSION_COOKIE, value=token,
        httponly=True, samesite="lax", path="/v1",
        secure=_cookie_secure())


def _store_device_secret(response: Response, secret: str) -> None:
    """把一枚刚签发的续命凭据种进 httpOnly Cookie（v0.35.0 #143②）。

    与 _attach_session 的会话固定论证不冲突：这里种的不是会话，攻击者把自己的
    device_secret 钉到受害者浏览器上，只能让受害者在下次自愈时登进**攻击者的**
    账号——而这把凭据本身来自 register/login 的成功响应，跨站发不出 JSON 登录
    请求（也没有 CORS），所以"别人的登录响应替我种 Cookie"这条信道不成立。

    调用点只有三处：注册、登录、reauth 成功（轮换）。adopt/renew 不碰这一格——
    前者已经由登录/注册种过，后者不换凭据，只换会话。
    """
    response.set_cookie(
        key=authz.DEVICE_SECRET_COOKIE, value=secret,
        httponly=True, samesite="strict",
        path=authz.DEVICE_SECRET_COOKIE_PATH,
        secure=_cookie_secure(),
        max_age=DEVICE_SECRET_INACTIVE_SECONDS)


def _clear_device_secret(response: Response) -> None:
    """刮掉浏览器里的续命凭据 Cookie。

    删除必须按 域名+path 定位，path 与 _store_device_secret 用同一个常量；
    写死字面量会在下次改路径时留下一条删不掉的死 Cookie。
    """
    response.delete_cookie(
        key=authz.DEVICE_SECRET_COOKIE, path=authz.DEVICE_SECRET_COOKIE_PATH,
        samesite="strict", secure=_cookie_secure())


class RegisterRequest(BaseModel):
    username: str
    password: str
    # 三条固定问题的答案，顺序与 auth.RECOVERY_QUESTIONS 对齐。这里是必填：存储层
    # 允许记录没有找回凭据（真实库里就有那之前的老账号），但经 API 新注册的人必须
    # 留下它，否则"忘记密码"这条路对新人永远走不通。少一条就是 422。
    security_answers: List[str]


class LoginRequest(BaseModel):
    username: str
    password: str


class ResetRequest(BaseModel):
    username: str
    # 条数不在这里判，和下面的 new_answers 同一层：存储层那道闸门（auth._check_answer_shapes）
    # 在读库与比对之前就会抛出，落到 HTTP 还是那句指着题数的中文 422、照样不计费（判据见
    # tests/test_auth.py 里那条零次 bcrypt 锁与 tests/test_auth_endpoints.py 里那条 free_typo 锁）。
    answers: List[str]
    new_password: str
    # 给了就连找回答案一起轮换（固定问题不等于固定答案），不给就是原样留着。
    # "不给"本身合法，所以条数只能由存储层判：它同样在读库与比对之前抛出，落到 HTTP
    # 还是那句 422、照样不计费（判据见 test_auth.py 里那条对称的零次 bcrypt 锁）。
    new_answers: Optional[List[str]] = None


class AdminCreateUserRequest(BaseModel):
    username: str
    password: str
    # 与注册同一套纪律：不给就空着（新账号没有自助找回，靠管理员重置），
    # 给就必须满三条（半套凭据更糟）。Optional 区分"没带"与"带了三条"。
    security_answers: Optional[List[str]] = None


class AdminResetPasswordRequest(BaseModel):
    new_password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/v1/auth/register")
def register(req: RegisterRequest, request: Request, response: Response):
    """开放注册：用户名 + 自设密码，成功即发一枚会话令牌（注册即登录）。

    路由挂在精确路径上——authz.PUBLIC_PATHS 也是精确匹配，带斜杠的变体在
    enforced 下先被中间件挡在凭据之外，不会成为第二个入口。

    v0.24 T1.4：注册受 data/config.json 的 registration_open 管，默认关闭
    （开源版是"给你自己用的"，管理员建号）。闸门放在限流检查**之前**：
    关着的门口不收失败预算——不然探测一句"注册关了"也要攒账，攒满之后
    重新开放注册的头一个人会莫名其妙吃 429。

    v0.35.0 #143②：响应里同时种续命凭据 Cookie。它与会话 Cookie 是两回事：
    这里仍然不种 session（会话固定论证见 _attach_session），只种那把 400 天寿命、
    Path 收口在 reauth 上的钥匙；老客户端无视它、读响应体里的字段，照样能用。
    """
    if not config_store.registration_open():
        raise HTTPException(status_code=403,
                            detail="注册当前关闭：本助理默认仅由管理员建号")
    ip = _client_ip(request)
    if _throttled(ip):
        # 与登录共用同一份失败预算。少了这一句，"撞名要计费"就是空话：格子照扣、
        # 谁也不收，枚举用户名依然是免费的。
        raise _too_many(FAILURE_WINDOW_SECONDS)
    if _registrations_full(ip):
        raise _too_many(REGISTER_WINDOW_SECONDS)
    try:
        principal, token, device_secret = _store().register_for_new_session(
            username=req.username, password=req.password,
            security_answers=req.security_answers)
    except AuthError as e:
        raise _register_error(e, ip)
    _note_registration(ip)
    # 成功**不**清失败账（终审 F3）：理由见 _FAILS 上面那段。
    _store_device_secret(response, device_secret)
    return {"token": token, "user_id": principal.user_id,
            "username": principal.username, "role": principal.role,
            # v0.34.0：续命凭据与会话令牌一起发——明文只在这一次出现（与 token
            # 同一条纪律，见文件头第 3 条）。库里只留 sha256 摘要。浏览器侧由
            # 上面的 Set-Cookie 收编；原生/老 web 仍读这个字段。
            "device_secret": device_secret}


def _register_error(e: AuthError, ip: str) -> HTTPException:
    """把存储层的失败原因翻译成状态码，并给唯一那条可被滥用的信道计费。"""
    if e.taken:
        # 实话保留，但它现在是免凭据的用户名枚举信道：每问一次扣一格登录预算。
        # 真人改名一次就过了，脚本则要每 10 次换一枚真实访客 IP——**这句从终审 F3 起才算数**：
        # 那时删掉了 register/login 成功路径上的 _FAILS.pop(ip)，它再也拿不到"自己的号
        # 成功一次"来洗账。换 IP 意味着它背后真有一张分布式网络，那时限流本来也挡不住，
        # 只是把成本抬上去。
        _note_failure(ip)
        return HTTPException(status_code=409, detail=e.reason)
    # 用户名、密码、三条找回答案本身不合格：都是当事人自己能改好的，不计费也不该挡别人的路。
    return HTTPException(status_code=422, detail=e.reason)


@router.post("/v1/auth/login")
def login(req: LoginRequest, request: Request, response: Response):
    """用户名 + 密码换一枚新的会话令牌；旧令牌继续有效（多设备并存）。

    v0.35.0 #143②：登录也种续命凭据 Cookie（Path 收口在 reauth），与会话 Cookie
    的签发门仍然分着——这里不种 session，adopt 仍是唯一会话 Cookie 出口。
    """
    ip = _client_ip(request)
    if _throttled(ip):
        raise _too_many(FAILURE_WINDOW_SECONDS)
    try:
        principal, token, device_secret = _store().login_for_new_session(
            username=req.username, password=req.password)
    except AuthError as e:
        _note_failure(ip)
        # 401 而不是 403：这里没有"身份是真的但角色不够"这一说，只有"没认出来"。
        raise HTTPException(status_code=401, detail=e.reason)
    # 成功**不**清失败账（终审 F3）：理由见 _FAILS 上面那段。
    _store_device_secret(response, device_secret)
    return {"token": token, "user_id": principal.user_id,
            "username": principal.username, "role": principal.role,
            # v0.34.0：登录是"发续命凭据"的两个时刻之一（另一个是 reauth 的轮换）。
            # 存量已登录的设备一把 secret 都没有——它们的降级路径是 renew 撑到
            # 绝对上限、然后老老实实登录一次，从此才有静默重登（PRD 第四节）。
            "device_secret": device_secret,
            # v0.24 T1.3：管理员建号/重置后要求首登改密；老记录缺字段读到 False。
            "must_change_password": _store().must_change_password(principal.user_id)}


@router.post("/v1/auth/renew")
def renew(request: Request, principal: Principal = CurrentPrincipal):
    """换一枚新令牌，**不换会话**（v0.34.0，PRD 第三节）。

    三条形状约束各自挡的东西：
    ① **无请求体**：不接受任何"期望有效期"入参。让客户端自己许愿要多久，等于把
       服务端唯一的到期判据交给最不可信的那一端。
    ② 要正常会话凭据：这条**绝不进** authz.PUBLIC_PATHS（判据 10 的负向锁）。
    ③ 不种 Cookie：全服务端发**会话** Cookie 的门仍然只有 /v1/auth/adopt 一扇
       （tests/test_cookie_auth.py 钉着）。浏览器侧要接手轮换后的新令牌，走
       "拿着新 token 再 adopt 一次"这条既有路，不另开一条 Set-Cookie 信道。续命
       凭据 Cookie 是 v0.35.0 #143② 的另一扇门，renew 不碰它。

    失败一律 401 且不签发任何东西；措辞只有一句（见 auth.RENEW_FAIL）。
    """
    token = authz._credential(request)
    try:
        _, new_token, payload = _store().renew(token)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=e.reason)
    return {"token": new_token, **payload}


class ReauthRequest(BaseModel):
    # 字段名就是它的语义：这里只接受续命凭据，不接受会话令牌，也不接受口令。
    # v0.35.0 #143②：body 变成可选——浏览器把凭据放在 httpOnly Cookie 里，
    # 它的请求体是空的；原生与老 web 仍走这条 JSON 回落。
    device_secret: Optional[str] = None


@router.post("/v1/auth/reauth")
def reauth(request: Request, response: Response,
           req: Optional[ReauthRequest] = None):
    """用续命凭据开一段全新会话：免会话凭据，但**不是**免凭据（PRD 第三节）。

    这条端点在 authz.PUBLIC_PATHS 里点了名——它必须免**会话**凭据才能被一个已经
    掉线的人用到，这正是它存在的理由；代价是它自己变成攻击面，防线全部落在这里：
    ① 256 位随机值不可爆破；② 单次使用，用错一次就废（猜中的那一次也一并废掉，
       所以"猜"没有累积收益）；③ 两本限速账（来源 IP + 按 secret），见 _REAUTH_FAILS；
    ④ 绝对不认会话令牌：会话令牌打进来算不出 device_secrets 里的命中，401
       （判据 5 的反向半边）。

    v0.35.0 #143②：凭据来源先看 Cookie，再看 JSON body（原生无 Cookie 通道，
    老 web 也仍带着 body）。两条一起出现时 **Cookie 优先**，并且只要 Cookie 在，
    就必须声明 X-CSRF——否则跨站请求可以拿受害者的 Cookie 换会话，body 是不是
    攻击者自己写的都改变不了这一点。这条端点在公开名单里，authz 中间件的 CSRF
    闸门不会经过它，所以这道检查必须在这里自己做（用的是同一套头与文案）。

    状态码走存储层带上来的显式标记，不靠 e.reason 等于哪句文案判（与 taken/
    charge/reauth 同一条纪律，终审 F4）：401 = 不认这把，410 = 太久没用了。
    410 同时把浏览器里的死 Cookie 刮掉；401 不刮（账号停用/服务端抖动都可能是
    暂时的，客户端历史上也刻意保留这一格）。
    """
    cookie_secret = request.cookies.get(authz.DEVICE_SECRET_COOKIE, "").strip()
    body_secret = ((req.device_secret if req else None) or "").strip()
    if cookie_secret and not request.headers.get(authz.CSRF_HEADER, "").strip():
        raise HTTPException(status_code=403, detail=authz.CSRF_DETAIL)
    secret = cookie_secret or body_secret
    ip = _client_ip(request)
    digest = hash_secret(secret)
    if _reauth_throttled(ip, digest):
        raise _too_many(FAILURE_WINDOW_SECONDS)
    try:
        principal, token, new_secret, payload = _store().reauth(secret)
    except AuthError as e:
        _note_reauth_failure(ip, digest)
        # 审计只记"有人拿续命凭据试过并被拒"，不记那把值：audit.jsonl 是明文日志。
        audit.log(None, "auth.reauth.denied", target=_reauth_secret_key(digest))
        if getattr(e, "gone", False):
            # 410 要同时刮掉浏览器里的死 Cookie。不能先往注入的 Response 上
            # delete_cookie 再 raise：异常路径会另建一个 JSONResponse，注入
            # Response 上的头会被丢掉（实测过，Set-Cookie 不见了）。
            gone = JSONResponse(status_code=410, content={"detail": e.reason})
            _clear_device_secret(gone)
            return gone
        raise HTTPException(status_code=401, detail=e.reason)
    audit.log(principal, "auth.reauth", target=principal.user_id)
    _store_device_secret(response, new_secret)     # 单次使用：每次 reauth 都轮换 Cookie
    return {"token": token, "device_secret": new_secret, **payload}


@router.post("/v1/auth/reset")
def reset(req: ResetRequest, request: Request, response: Response):
    """三题全答对就换密码；该人名下所有会话令牌同时作废（每一台设备都掉线）。

    这里没有"先把问题念给你听"那一步：三题是全站常量，前端自己渲染，服务器不为
    一句抄来的话开一条免凭据信道。答案与新密码**一次提交**——分开验答案就等于给
    外人一个 oracle。

    这个端点看**三本**账，顺序是先猜错、后猜中：_RESET_FAILS（按 IP）挡
    "同一来源一直在猜"，_RESET_FAILS_BY_ACCOUNT（按被猜的账号）挡"换个来源接着猜
    同一个人"——三题低熵，IP 维度的预算对秒拨/共享 NAT 不构成上限（2026-09-23 审查 #6）；
    _RESETS 挡"已经猜中过几次"。第四本 _FAILS（登录与注册共用的失败账）它既不看不写，
    猜错的格子也刻意不记到那本上：猜口令与猜三题是两种不同的猜测面，共用一格预算时前者
    会把后者顶满，而 429 落在哪本账上得能从 Retry-After 里读出来（为什么单独一册，见
    _RESET_FAILS 上面那一段；三本找回账的 Retry-After 同为十分钟，不互相泄露信息）。
    反过来同样不通融：改密成功只往 _RESETS 记一格，**不清**任何失败账——三题的文本和
    常见答案组合本来就是公开的，猜中一次恰恰说明来路不明的那一面还没排除。
    （这一段以前还写着"因为 _FAILS 会在登录或注册成功时被清空"：终审 F3 把那两处
    `_FAILS.pop(ip)` 删了，现在两本账任何成功都不还。）
    """
    ip = _client_ip(request)
    if _reset_throttled(ip) or _reset_account_throttled(req.username):
        raise _too_many(FAILURE_WINDOW_SECONDS)
    if _resets_full(ip):
        raise _too_many(RESET_WINDOW_SECONDS)
    try:
        _store().reset_password(req.username, req.answers, req.new_password,
                                req.new_answers)
    except AuthError as e:
        if e.charge:
            # 答案错、没留找回答案、查无此人、已停用：同一句、同一格预算、同一个 401。
            # 判的是存储层带上来的显式标记，不是 e.reason 等于哪句文案——文案是会说、
            # 会换的，预算不该挂在它上面（锁：
            # test_the_reset_billing_follows_the_flag_not_the_wording）。
            # 计费同时记两本：来源一本、被猜的账号一本（查无此人记在它名下那个键上，
            # 锁不锁对任何用户名一律，429 就不泄露"这号存在吗"）。
            _note_reset_failure(ip)
            _note_reset_failure_account(req.username)
            raise HTTPException(status_code=401, detail=e.reason)
        # 新密码或轮换答案列表本身不合格（太短、太长、条数不对）：那是当事人自己能
        # 改好的，不计费
        raise HTTPException(status_code=422, detail=e.reason)
    _note_reset(ip)
    # 改密会清掉这个人全部 device_secrets（auth.reset_password）；这台浏览器里的
    # 那一枚 Cookie 也就成了死钥匙，响应顺手刮掉（v0.35.0 #143②）。
    _clear_device_secret(response)
    return {"status": "password_reset"}


@router.post("/v1/auth/change-password")
def change_password(req: ChangePasswordRequest, request: Request, response: Response,
                    principal: Principal = CurrentPrincipal):
    """本人改密（v0.24 T3.1 首登闭环）：出示当前会话 + 原口令，换掉口令并清旗标。

    与三题自助 reset 的两处刻意不同：
    ① reset 是"口令可能泄露"的自救，名下**全部**设备掉线；这里把正在用的这一枚
       留下（keep_token 就是调用方自己出示的凭据），其余作废——动机不同，惩罚面不同。
    ② reset 对免凭据访客说话，措辞必须同形同耗时；这里的失败措辞可以说实话
       （"原口令不正确"），因为对面已经出示过有效会话，不构成新的探测信道。
       状态码走 reauth 标记而不是文案相等——与 taken/charge 同一纪律（终审 F4）。
    """
    try:
        record = _store().change_password(
            principal.user_id, req.current_password, req.new_password,
            keep_token=authz._credential(request))
    except AuthError as e:
        raise HTTPException(status_code=401 if e.reauth else 422, detail=e.reason)
    # 改密会清掉这个人全部 device_secrets（判据 6）；这台浏览器里的 Cookie 跟着刮掉。
    _clear_device_secret(response)
    return {"status": "password_changed",
            "must_change_password": bool(record.get("must_change_password"))}


@router.get("/v1/auth/me")
def me(principal: Principal = CurrentPrincipal):
    """前端用它确认"我到底是谁"——凭据被解析成谁，只有这里说得准。"""
    return {"user_id": principal.user_id, "username": principal.username,
            "role": principal.role}


@router.post("/v1/auth/adopt")
def adopt(request: Request, response: Response,
          principal: Principal = CurrentPrincipal):
    """把请求头里出示的那枚凭据收编为 httpOnly 会话 Cookie（凭据 → 浏览器会话）。

    前端拿到登录/注册响应体里的 token 后调用它一次，此后浏览器侧一切请求只靠
    Cookie，token 明文在页面里存都不存；管理页粘贴 bootstrap 口令、手动录令牌
    走的也是这同一条路。它同时是服务端**唯一发会话 Cookie**的地方（见
    _attach_session 那一段的会话固定论证）；续命凭据 Cookie 由 register/login/
    reauth 三处走 _store_device_secret，与本端点无关。

    只认请求头凭据：一个只剩 Cookie 的会话没有可收编的新东西，再 adopt 一次
    只是把同一枚 Cookie 原样重写，没有意义还多一个岔路，所以直接 400。
    这也让本端点对 CSRF 天然免疫——跨站请求带不出 Authorization 头。
    响应体与 /v1/auth/me 同形：adopt 成功的第一个用处就是当场确认身份。
    """
    token = authz._header_credential(request)
    if not token:
        raise HTTPException(status_code=400, detail="adopt 需要请求头里出示凭据")
    _attach_session(response, token)
    return {"user_id": principal.user_id, "username": principal.username,
            "role": principal.role}


# ---------- 管理端 ----------
# role 不在任何请求体里：管理员只来自 ACCESS_TOKEN bootstrap，或来自运维手改
# users.json。给 API 开一个写 role 的口子，等于把整套身份体系作废。

def _public_user(record: dict) -> dict:
    """用户记录的对外视图。逐字段列出，是为了让 pw_hash 与 tokens 无处可藏。"""
    return {
        "user_id": record.get("user_id"),
        "username": record.get("username"),
        "role": record.get("role"),
        "disabled": record.get("disabled"),
        # v0.24 T3.1：管理端要看得出"这个号还在用别人设定的口令"——旗标本身
        # 不是秘密，它由首登改密流程负责消掉。
        "must_change_password": bool(record.get("must_change_password")),
        "created_at": record.get("created_at"),
        # last_seen 粗粒度是设计使然：存储层为不把鉴权变成热路径写盘，把落盘
        # 节流到一小时以上，所以它读作"上次看见它至少是一小时前"，不是在线状态。
        "last_seen": record.get("last_used_at"),
        # 有几台设备在线是运维要知道的，但只报数量：令牌摘要本身是凭据。
        "sessions": len(record.get("tokens") or []),
    }


@router.post("/v1/auth/logout")
def logout(request: Request, response: Response,
           principal: Principal = CurrentPrincipal):
    """退出这台机器：只作废**调用方这一枚**令牌。

    刻意不清这个人的整张令牌表——那是管理员 rotate 的语义（怀疑口令泄露）。
    它还要能替"本机清单里的另一个人"退出：前端直接带上他那一枚来调就行，不必
    先把他切成当前身份、把他的会话加载到屏幕上（共用设备上没这个必要）。

    取凭据复用 authz._credential：它已经管好了方案名大小写、x-access-token
    兜底、以及"请求头没有就看会话 Cookie"——这里再写一份 split 就是第二个事实
    来源，两边一漂移就会出现"中间件认得这枚、退出说不认识"，而表现是退出没反应。
    """
    _store().revoke(authz._credential(request))
    # Cookie 里的会话已随上面那枚作废；把壳也刮掉，浏览器侧不留一条死凭据。
    # path 必须与 _attach_session 一致，否则删不掉（Cookie 按 域名+path 定位）。
    response.delete_cookie(key=authz.SESSION_COOKIE, path="/v1",
                           samesite="lax")
    # v0.35.0 #143②：退出会清掉这个人**全部** device_secrets，浏览器里这一枚
    # 也成了死钥匙。只在"退的是本浏览器自己的会话"时刮：带 Authorization 的
    # 那一形是替清单里的另一个人撤销，他的凭据不在这台浏览器的 Cookie 里，
    # 顺手刮掉只会把当前用户的静默重登能力一起误伤。
    if not authz._header_credential(request):
        _clear_device_secret(response)
    return {"status": "logged_out"}


def _blank_totals() -> dict:
    return {k: 0 for k in ("calls", "ok", "failed", "prompt_tokens", "completion_tokens",
                           "total_tokens", "reasoning_tokens", "cached_tokens", "unknown_usage")}


def _today() -> str:
    from datetime import datetime
    return datetime.now().astimezone().strftime("%Y-%m-%d")


@router.get("/v1/admin/usage")
def admin_usage(day: str = None, _: Principal = RequireAdmin):
    """账本的读取面：谁、几次、多少 token、谁的钱。

    `day` 只收 `YYYY-MM-DD`：这个值会直接当键去查字典，宽松一点就是"随便传什么
    都能拿到一份 200 空表"，那既不报错也看不出自己问错了。
    """
    if day is not None and not _DAY_RE.match(day.strip()):
        raise HTTPException(status_code=400, detail="day 要写成 YYYY-MM-DD")
    target = day.strip() if day else None
    rows = usage.snapshot(target)
    totals = {"operator": _blank_totals(), "user": _blank_totals()}
    for row in rows:
        bucket = totals.setdefault(row.get("paid_by") or "operator", _blank_totals())
        for field in ("calls", "ok", "failed", "prompt_tokens", "completion_tokens",
                      "total_tokens", "reasoning_tokens", "cached_tokens", "unknown_usage"):
            bucket[field] += row.get(field, 0)
    # v0.25 T5.20 管理端读面：影子积分按天、按 provider 的合计（管理员可见，
    # 用户端不出现）。语义是「免费期已垫付 N 积分」——0.00x 免费期照常算出来的
    # 那个数是垫资额的唯一来源；算不出的（未定价/per_call）如实列成 gap，
    # 绝不溜进总和。派生即算：当日账行 × 当前单价现算，不改 usage 写口径。
    from app.core import credits
    return {"day": target or _today(), "rows": rows, "totals": totals,
            "days": usage.days(), "credits": credits.shadow_breakdown(target)}


@router.get("/v1/admin/search-usage")
def admin_search_usage(days: int = search_usage.DEFAULT_WINDOW_DAYS,
                       _: Principal = RequireAdmin):
    """搜索用量：这个窗口里搜了几次、挂在哪个源几次、每源成功率。

    与 `/v1/admin/usage` 的分工：那本账答"谁用了多少 token、谁的钱"，这本答
    "搜索这门外部依赖的现状"。两本各存各的文件，`usage.py` 的写口径一个字没动
    （这里只是多一个读面）。

    `days` 由 FastAPI 收成整数（非整数 422），范围在这一层闸死成 1–90：读面是按天
    枚举再查字典，不给它一个可以喊"读一整年"的无界参数。窗口按服务端本地时区切天，
    与 `/v1/admin/usage` 的 `day` 同一把尺子。缺账的那天如实列成零——"这周没搜过"
    是合法答案，不是错误（v0.25 R4a 同一条判据）。

    三条必须说清的边界，都在 `boundaries` 里原样带出去，界面不重述第二套口径：
    1. 只记本站服务端真跑过降级链的那几次（`web_search` 工具每次执行一笔）；
    2. 用户在「设置 → 搜索源」用自己的 key 走浏览器/WebView 直连的搜索**不在这本账上**
       ——那是用户设备上的出站请求，本站一个字节都看不见，也不拿 0 冒充"用户没用";
    3. 每 15 分钟一次的可用性探针不记账（那是运维检查，记了就等于每小时虚增 4 次）。
    """
    if not search_usage.MIN_WINDOW_DAYS <= days <= search_usage.MAX_WINDOW_DAYS:
        raise HTTPException(status_code=400,
                            detail=f"days 要在 {search_usage.MIN_WINDOW_DAYS}–"
                                   f"{search_usage.MAX_WINDOW_DAYS} 之间")
    data = search_usage.aggregate(days)
    data["boundaries"] = {
        "server_only": "只含本站服务端跑降级链的真实搜索（web_search 工具每次执行记一笔，按天×源聚合）。",
        "no_query_text": "账上没有查询原文，也没有哈希：失败归因是写死的枚举，不存自由文本。",
        "direct_browser": "用户自带 key 的浏览器/WebView 直连不计：那些请求从用户设备直接出站，本站看不见。",
        "probe": "每 15 分钟一次的可用性探针不计：它搜的是固定的「北京 天气」，不是有人搜了东西。",
        "success_rate": "成功率 = 给出结果的次数 ÷ 真出网的尝试次数；没出过网回 null（不是 0%）。",
    }
    return data


def _my_provider_name(provider_id: str) -> str:
    """显示名只取 label 这一个字段：配置整个对象（api_key、base_url 都在里面）
    不出了这个函数，路由层想"顺手把整个 provider 倒出去"都没机会。
    条目被删过或从来不是配置里的 id（unknown-provider），名字回落成 id 本身——
    账上的行不能因为配置被改就凭空蒸发（那是改历史），名字也不该编造第二个事实。
    """
    provider = providers.store.get(provider_id)
    return (provider or {}).get("label") or provider_id


@router.get("/v1/me/usage")
def my_usage(day: str = None, principal: Principal = CurrentPrincipal):
    """v0.25 R4a：每个人读自己的用量，只有这一面答"我自己"。

    与 /v1/admin/usage 的分工：那边答"所有人"（require_admin 原样保留），这边
    只认调用者自己——所以这里没有能填别人 id 的参数，"别人的数字"在这张面上
    根本问不出来；不存在与不是你的同一种回答（200 空表），也就没有拿 403/404
    的差别当探测器的那种缝。day 的规矩与管理面同一条（只认 YYYY-MM-DD），
    两边不许漂移。没账的人读成零 + 齐的键："还没用过"是合法状态，不是错误。

    诚实的边界，两句都要说：这里只有次数与 token，钱要等 R4b（没有价格表，
    本地估算拿去对账是错的，见 usage.py 头部）；`unknown_usage` > 0 的行 token
    是**下限**——上游没回 usage 的那几次记的是 0，别把它读成"没用过"。
    """
    if day is not None and not _DAY_RE.match(day.strip()):
        raise HTTPException(status_code=400, detail="day 要写成 YYYY-MM-DD")
    target = day.strip() if day else _today()
    rows = []
    for row in usage.snapshot_for_user(principal.user_id, target):
        item = {"provider": {"id": row["provider_id"],
                             "name": _my_provider_name(row["provider_id"])},
                "paid_by": row.get("paid_by", "operator")}
        for field in usage.FIELDS:
            item[field] = row.get(field, 0)
        item["tokens_are_floor"] = item["unknown_usage"] > 0
        rows.append(item)
    totals = {field: sum(row[field] for row in rows) for field in usage.FIELDS}
    totals["tokens_are_floor"] = totals["unknown_usage"] > 0
    return {"day": target, "days": usage.days_for_user(principal.user_id),
            "rows": rows, "totals": totals,
            "note": "token 数一律取上游回传的 usage；tokens_are_floor 为真的行是下限，"
                    "不是 0。这里没有钱：价格估算随 R4b 才接入。"}


@router.get("/v1/admin/users")
def list_users(_: Principal = RequireAdmin):
    return {"users": [_public_user(u) for u in _store().list_users()]}


@router.post("/v1/admin/users")
def create_user(req: AdminCreateUserRequest, actor: Principal = RequireAdmin):
    """管理员建号（v0.24 T3.1）。role 不在请求体里——与停用/轮换同一套纪律。

    撞名给 409 一句实话且不计费：这不是免凭据端点，能走到这里的人已经出示了
    管理员身份，"用户名探测器"的攻击面在这里不存在。形状问题一律 422，说的
    都是调用方自己改得好的事。
    """
    try:
        record = _store().admin_create_user(
            req.username, req.password, req.security_answers)
    except AuthError as e:
        raise HTTPException(status_code=409 if e.taken else 422, detail=e.reason)
    # 只记"谁建了哪个 id"：口令是调用方带上来的，一次都不上盘——after 走的是公开展望，
    # 里面本来就没有凭据字段，audit._redact 那把键名黑名单是第二道，不是第一道。
    audit.log(actor, "user.create", target=record["user_id"], after=_public_user(record))
    return _public_user(record)


@router.post("/v1/admin/users/{user_id}/disable")
def disable_user(user_id: str, actor: Principal = RequireAdmin):
    """停用一个人。三种"不"各有各的话，不许糊成一句。

    路由层先看记录，为的是把 404/400 说得准：存储层的闸门（管理员禁不掉，R2
    决策——比 PRD 的"最后一个管理员不许停用"更严）返回的是同一个 False，
    照旧翻译成"用户不存在"就会把人引向一个根本没消失的人。自己停用自己也一样：
    那是操作者手滑，400 一句"不能停用自己"，不是 404。
    真停用的生效形状（下一个请求即 401）由存储层的停用判定保证，
    判据在 test_disable_online_user_then_next_request_is_401_then_enable_restores。
    """
    record = next((u for u in _store().list_users() if u.get("user_id") == user_id), None)
    if record is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user_id == actor.user_id:
        raise HTTPException(status_code=400, detail="不能停用自己")
    if record.get("role") == "admin":
        raise HTTPException(status_code=400, detail=(
            "管理员账号不允许停用：全站能恢复访问的身份只有这一个，"
            "要换管理员请改 users.json，不走停用"))
    if not _store().disable_user(user_id):
        # 服务层闸门（test_admin_cannot_be_disabled_at_the_service_layer）之外的
        # 唯一 False：记录恰好在两次读之间被删了。说实情，不装成功。
        raise HTTPException(status_code=404, detail="用户不存在")
    audit.log(actor, "user.disable", target=user_id)
    return {"status": "disabled", "user_id": user_id}


@router.post("/v1/admin/users/{user_id}/enable")
def enable_user(user_id: str, actor: Principal = RequireAdmin):
    # 轮换令牌不再顺手解除停用，因此撤销必须有对称的还原动作。
    if not _store().enable_user(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    audit.log(actor, "user.enable", target=user_id)
    return {"status": "enabled", "user_id": user_id}


@router.post("/v1/admin/users/{user_id}/reset-password")
def reset_user_password(user_id: str, req: AdminResetPasswordRequest,
                        actor: Principal = RequireAdmin):
    """管理员替某人重置口令：名下令牌全部作废 + 首登必须改密。

    响应里不回显新口令，也不发新令牌——重置的语义是"他得自己设一遍"，
    管理员拿到他的会话等于又造了一个"口令经了别人手还一直用下去"的账号。
    """
    try:
        ok = _store().admin_reset_password(user_id, req.new_password)
    except AuthError as e:
        raise HTTPException(status_code=422, detail=e.reason)
    if not ok:
        raise HTTPException(status_code=404, detail="用户不存在")
    # after 只写"这一格被改过"这件事的形状，不写内容：新口令是请求体带上来的。
    audit.log(actor, "user.reset-password", target=user_id,
              after={"must_change_password": True})
    return {"status": "password_reset", "user_id": user_id,
            "must_change_password": True}


@router.post("/v1/admin/users/{user_id}/rotate-token")
def rotate_user_token(user_id: str, actor: Principal = RequireAdmin):
    """强制全端重登：旧令牌全部作废，且停用状态原样保留。"""
    try:
        token = _store().rotate_token(user_id)
    except AuthError:
        raise HTTPException(status_code=404, detail="用户不存在")
    # 记"发生了换发"，绝不记那枚新令牌——明文只该出现在这一次 HTTP 响应里。
    audit.log(actor, "user.rotate-token", target=user_id)
    return {"token": token}


@router.delete("/v1/admin/users/{user_id}")
def remove_user(user_id: str, actor: Principal = RequireAdmin):
    if user_id == actor.user_id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    if not _store().delete_user(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")
    # 删号是不可逆的：审计是它唯一的存照，这里必须落一条，哪怕只是"谁删了哪个 id"。
    audit.log(actor, "user.delete", target=user_id)
    return {"status": "deleted", "user_id": user_id}


# ---------- v0.24 T1.4：运行时配置 ----------

class ConfigUpdateRequest(BaseModel):
    """管理端只收这三个键，形状与 config_store.MUTABLE_KEYS 对齐。

    字段写成 Optional：PATCH 语义，"没带的那个键不动"。全接收一个 dict 当然更省
    一行代码，但那等于让 pydantic 之外的世界替我们决定什么能写——白名单就白名单。
    """
    registration_open: Optional[bool] = None
    update_repo: Optional[str] = None
    # v0.37.0 http_fetch 主机白名单：只有这一个写入口（RequireAdmin），普通用户
    # 既读不到（GET /v1/config 剔除）也写不了（POST 在依赖处就 403）。空列表是
    # 合法值（= 工具下线）；条目形状由 config_store 整表判，坏的进 rejected。
    fetch_allowed_hosts: Optional[List[str]] = None


@router.get("/v1/config")
def get_config(principal: Principal = CurrentPrincipal):
    """登录用户可见的配置面：注册开没开、更新从哪个仓库找。

    不给匿名：这两个值本身不算秘密，但"管理面长什么样"是侦察材料，与 /health
    不同，配置没有替匿名访客答一句的必要。

    v0.37.0：`fetch_allowed_hosts` 只给管理员——它是"服务端被允许出网到哪"的
    策略原样，普通用户多知道一个白名单域名，就多一枚社工话术素材（"去抓这个
    站点吧"，而那站点真的会被服务端替他去抓）。普通用户拿到的响应里这个键
    整个不出现，而不是给个空表——空表会引导用户去配一个他写不动的格子。
    """
    cfg = config_store.read()
    if principal.role != "admin":
        cfg.pop("fetch_allowed_hosts", None)
    return cfg


@router.post("/v1/admin/config")
def update_config(req: ConfigUpdateRequest, actor: Principal = RequireAdmin):
    """改配置并落盘。未知/类型不对的键原样退回，不静默吞。"""
    before = config_store.read()
    partial = {k: v for k, v in req.model_dump().items() if v is not None}
    cfg, rejected = config_store.apply_update(partial)
    # 每一次配置写都进账，不因为"值恰好没变"而消音：排障要知道的是"谁在什么时候试图
    # 动过全站闸门"，改前改后各是什么——即便这次按下没改变结果，按下本身就是该留痕的
    # 运营动作。被拒的键一并记下，那正是"有人想改改不动"的证据。
    audit.log(actor, "config.update", before=before, after=cfg,
              detail="被拒的键：" + ", ".join(sorted(rejected)) if rejected else "")
    return {"config": cfg, "rejected": rejected}


@router.get("/v1/admin/audit")
def read_audit(limit: int = 200, _: Principal = RequireAdmin):
    """只读地看这份只增账：最新在下（read() 已按正序返回）。没有删除路由——
    能从界面删掉的证据就不是证据。"""
    return {"entries": audit.read(limit=min(max(1, limit), 1000))}
