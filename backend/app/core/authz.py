"""鉴权接线：把凭据解析成 Principal，并提供两个端点依赖。

与 core/auth.py 分家的原因：身份规则要能离线测，也不该被 web 框架绑住。
"""
import hmac
import os
import threading
import time
from collections import OrderedDict
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.auth import (BOOTSTRAP_TOKEN_ENV, Principal, _as_hash_bytes,
                           auth_store)
# v0.36.3 安全监控：401/403 只往内存里记一条（record 不做盘上 I/O），
# 绝不在这里 open/fsync——这条中间件是 async 热路径。
from app.core import security_log
# 来源地址只认 client_ip 这一份口径；不从 auth_router 导入（那会成环）。
from app.core.client_ip import client_ip
# 导出票据的路径形状只有存储层知道（长度跟着生成器走），这里引用而不是抄第二份。
from app.session.export_store import EXPORT_PATH_PREFIX, TICKET_PATH_RE

BOOTSTRAP_PRINCIPAL = Principal("default_user", "本机管理员", "admin")

# 无需凭据即可到达的端点，精确匹配。注册与登录本来就是给"还没有身份的人"用的，
# 所以它们必然公开——代价是这几个端点自己变成攻击面，防线全部落在 auth_router
# 的真实 IP 限流与"几种失败同一句话、同一份耗时"上（注册/登录/改密都是）。
# 找回只有改密这一条公开端点：三题是全站常量，页面自己渲染，不必问服务器要。
# 新增公开端点必须同时改这里，否则路由契约测试会红。
PUBLIC_PATHS = frozenset({"/v1/auth/register", "/v1/auth/login", "/v1/auth/reset",
                          # v0.34.0：静默重登。免的是**会话凭据**（掉线的人手里没有
                          # 会话），不免"凭据"本身——body 里那把 256 位续命凭据就是
                          # 凭据，且单次使用。防线（限速两本、措辞同形、绝不认会话
                          # 令牌）全在 auth_router.reauth 与 auth.AuthStore.reauth 里。
                          # 加这一条之前请先读 tests/test_route_auth_contract.py 的
                          # test_public_allowlist_is_exactly_the_bootstrap_endpoints。
                          "/v1/auth/reauth",
                          # 公开信息，不含任何用户数据：手机上那张「发现版本更新」的卡片
                          # 要问"最新是哪一版"，而它发生在人还没登录的时候。
                          # 免鉴权不等于没有代价——它会替调用方读一次版本清单，所以那条
                          # 读取带一个缓存期（app/core/releases.py 的 CACHE_SECONDS，今天
                          # 30 秒）：一小时里最多 120 次，与来多少请求无关。
                          "/v1/release/latest",
                          # 壳「检查更新」的透传端点：与上面那条共用同一份快照与同一套
                          # 缓存代价，公开的是本来就公开的发布元数据。它替代的是
                          # "壳自己直连 api.github.com"——那条通道要过各家 ROM 的下载器。
                          "/v1/update/info"})

# 会话 Cookie（方案 C：凭据不进 JS）。名字刻意短且不带语义泄露；值就是存储层
# 签发的那枚令牌原文，服务端不新建第二套凭据体系。
# path=/v1：静态页面与文档路由永远收不到它，能带上它的只有数据端点本身。
# HttpOnly：页面脚本读不到 document.cookie，XSS 拿不走会话。
# SameSite=Lax：跨站 POST 一律不带 Cookie（第一道 CSRF 防线），顶层 GET 跳转仍带。
# 不设 Max-Age = 会话级 Cookie：浏览器/WebView 进程退出即蒸发。
SESSION_COOKIE = "session"

# CSRF 第二道防线：不带凭据请求头、却带会话 Cookie 的不安全方法，必须声明这个
# 自定义头。跨站脚本能诱导浏览器带出 Cookie（同站顶层导航之外其实带不出，Lax
# 已挡 POST），但任何站点的 JS 都发不出"既带该头又能让浏览器附上本站 Cookie"的
# 跨站 POST；而原生 HTTP 客户端从来不受同源约束，也就从来不需要这道门。
# 值不校验内容，只要求非空：它是"我在 JS 里"的声明位，不是秘密。
CSRF_HEADER = "x-csrf"

# v0.35.0 #143②：Web 端的续命凭据（device_secret）从 localStorage 收编进 Cookie。
# 它与 SESSION_COOKIE 是两类凭据、两道生命周期：会话 30 天滑动 / 180 天绝对上限；
# 续命凭据 400 天不活动才失效（auth.DEVICE_SECRET_INACTIVE_SECONDS），换来的是
# "网页在绝对上限之后也能免口令爬起来"。属性逐条都是有理由的：
# - HttpOnly：JS 读不到这把 400 天寿命的钥匙，XSS 能重放会话也偷不走它；
# - Secure 默认开：现网只走 cloudflared HTTPS，本机 http 与测试要显式
#   AUTH_COOKIE_SECURE=0 降级（与 SESSION_COOKIE 同一个开关、同一条 fail-closed 方向）；
# - SameSite=Strict：页面与 API 同源，唯一消费方是同源 fetch；这把钥匙不服务任何
#   顶层跳转，比 Lax 更紧，跨站请求连带都带不出去；
# - Path 精确到 /v1/auth/reauth：它只在这一条端点上被需要。指向业务面（/v1）会让
#   一枚 400 天凭据随每一次请求出门，多一份代理/日志暴露面；
# - Max-Age 由 auth_router 写成 400 天：与会话 Cookie 不同，它必须跨浏览器重启活着，
#   否则"绝对上限之后自愈"在用户关一次浏览器后就失效了。
# 原生没有 Cookie 通道（Prefs.kt / SecretStore.kt），服务端保留 JSON body 回落；
# 浏览器这一侧不再往 body 里放它。
DEVICE_SECRET_COOKIE = "device_secret"
DEVICE_SECRET_COOKIE_PATH = "/v1/auth/reauth"

# v0.34.0：这枚会话令牌还剩几秒（滑动期与会话绝对上限里更近的那个）。
# 名字带 Expires-In 而不叫 Expires-At：值是**秒数**不是时刻，客户端不需要拿
# 服务器时钟和自己的时钟做减法——那正是"提前 5 天续期"和"晚 5 天才发现要续"
# 两种 bug 的产地。
TOKEN_EXPIRES_HEADER = "X-Token-Expires-In"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_DETAIL = "缺少 CSRF 校验头"

# 免凭据的第二种形状：带变量段的公开路由。精确匹配的门今天只有票据兑换这一条
# 需要跨过去——链接本身就是凭据（128 位随机、5 分钟过期、一次作废，见
# app/session/export_store.py），壳 APK 的下载请求带不出 Authorization 头，
# 不匿名就没有任何文件能落进手机。
# 放行的是"整条路径恰好等于前缀+票据形状"（锚定的正则，不是前缀匹配）：
# /v1/exports/ 、/v1/exports/short、/v1/exports/x/y 都仍然要凭据。key 是路由
# 模板（给路由契约测试核对挂载表用），value 是中间件匹配具体请求用的正则。
# 新增一条带变量的公开路由 = 在这里点名 + 改 tests/test_route_auth_contract.py，
# 与 PUBLIC_PATHS 同一套"多一条就红"的规矩。
PUBLIC_ROUTE_TEMPLATES = {f"{EXPORT_PATH_PREFIX}{{ticket_id}}": TICKET_PATH_RE}

_PROTECTED_PREFIXES = ("/v1/", "/docs", "/redoc", "/openapi.json")

# 401 文案只有一份：中间件与依赖各写一遍迟早会漂移，而它是对客户端的语义承诺
# （"没有身份"，区别于 403 的"有身份但不够"）。
UNAUTHORIZED_DETAIL = "缺少或错误的访问凭据"

# 凭据失败账：按来源 IP 计，窗口滚动，只在本进程内存里。目的不是"锁号"
# （bootstrap 没有可锁的账号名），而是让猜 bootstrap 口令从"每次一往返"变成有预算的
# 慢动作。计数射程分两半，共用同一本账：
# - 请求头凭据在**解析之前**预占（任何受保护路径）：resolve_principal 在所有受保护
#   路径上都会拿请求头凭据去比 bootstrap 口令，只记管理面与会话兑换出口的话，
#   /v1/auth/me、/v1/sessions 就是不计数的口令比对器。预占把并发突发也关进预算。
# - 只带 Cookie 的请求在**解析失败之后**补记：有效 Cookie 不计数、更不 429。这里
#   不能预占——adopt 会把 bootstrap 原样写进会话 Cookie，正常管理台浏览器每个请求
#   都带着它，预占会让饱和来源上的合法管理台会话一起被 429。代价是并发/猜测各自
#   仍会先做一次常量时间比较；这是低危取舍（比较之后不会服务任何业务逻辑）。
# 没带任何凭据的 401 不计数。进程重启清零是接受的边界：持久化会把这份可被匿名
# 填充的表带上磁盘。
ADMIN_FAIL_WINDOW_SECONDS = 600.0
ADMIN_FAIL_LIMIT = 10
ADMIN_THROTTLED_DETAIL = "管理员凭据尝试过多，请稍后再试"
_ADMIN_FAILS = OrderedDict()         # ip -> [monotonic 时间戳]，升序，最多 LIMIT 个
_ADMIN_FAILS_LOCK = threading.Lock()
# 硬上限，与 security_log 的来源表同量级。窗口内全是新来源时"只清过期项"清不掉
# 任何东西，表会无限增长；到达上限后先清完全过期项，再按"最久未触碰"淘汰
# （OrderedDict 的触碰序，append 后 move_to_end）。不整表清空：那会把攻击者的失败
# 预算一并重置，也误伤最近还在尝试的来源。
_ADMIN_FAILS_MAX_KEYS = 4096


def is_public_path(path: str) -> bool:
    """这条**具体请求路径**免不免凭据：精确名单，或恰好整条命中票据形状。

    判定收在这一个函数里，中间件与测试共用同一份口径；写成两处各来一遍的
    话，改天漂移的那一半就是没人知道的免凭据门。
    """
    return path in PUBLIC_PATHS or any(p.match(path)
                                       for p in PUBLIC_ROUTE_TEMPLATES.values())


def _auth_mode() -> str:
    """每次请求现读。做成 import 期常量的话，测试就得 reload 模块才能切模式。"""
    return os.getenv("AUTH_MODE", "enforced").strip().lower()


# disabled 模式的远程放行开关：运维显式设 1 才承认"我知道我在裸奔"。
DISABLED_REMOTE_ENV = "ALLOW_DISABLED_REMOTE"


def _bind_host() -> str:
    """进程对外绑定的网卡地址。约定读 HOST（与 uvicorn --host / docker 传参同名）。

    缺省按 127.0.0.1 算：run_backend.py 与桌面 EXE 都硬编码回环，不设 env 的
    部署形态本来就是本机单机；把缺省判成 0.0.0.0 会误伤全部本地安装。
    """
    return (os.getenv("HOST", "").strip() or "127.0.0.1").lower()


def _is_loopback_host(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


def _disabled_mode_allowed() -> bool:
    """disabled 只允许出现在回环绑定上；绑到 0.0.0.0 等对外网卡时必须拒绝。

    这就是"仅打印警告"缺的那道技术护栏：警告是给自觉的人看的，护栏是给误配的人
    兜底的。判定每次现读 env，与 _auth_mode 同一套口径，测试 monkeypatch 即生效。
    """
    host = _bind_host()
    if _is_loopback_host(host):
        return True
    return os.getenv(DISABLED_REMOTE_ENV, "").strip() == "1"


def _bootstrap_token() -> str:
    return os.getenv(BOOTSTRAP_TOKEN_ENV, "").strip()


def docs_kwargs_for_mode(mode: str) -> dict:
    """按模式给出 FastAPI 构造参数——纯函数，不读 env，因此两种模式都断言得到。

    文档路由在 app 构造期就定死，请求期改不了。这个判定原先写在 main.py 里，
    测试要覆盖另一半就只能 importlib.reload(app.main)：reload 会把 sessions_store
    等模块级对象重新绑定到别处，而全局 client 早已抓住旧 app，后续任务给
    app.main 打补丁时就会静默错位。抽成函数后 main.py 只留一行传参。
    """
    if mode == "disabled":
        return {}
    # 路由表本身就是侦察材料，对外一律不给 openapi
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


def _header_credential(request: Request) -> str:
    supplied = request.headers.get("authorization", "").strip()
    scheme, _, credential = supplied.partition(" ")
    # 认证方案名大小写不敏感（RFC 7235）；未写方案名时整值即凭据
    if not credential and scheme:
        credential = scheme
    elif scheme.lower() not in ("bearer", "token"):
        credential = ""
    return credential or request.headers.get("x-access-token", "").strip()


def _cookie_credential(request: Request) -> str:
    """httpOnly 会话 Cookie 里的那一枚；页面 JS 永远看不见它的值。"""
    return request.cookies.get(SESSION_COOKIE, "").strip()


def _credential(request: Request) -> str:
    """请求携带的凭据原文：请求头优先，其次是会话 Cookie。

    保持这个函数名不为别的——logout 与一切"把调用方刚用的那枚作废/复述回去"的
    语义都要走同一份口径，否则会出现"中间件认得这枚、退出说不认识"。
    """
    return _header_credential(request) or _cookie_credential(request)


def _secrets_match(supplied: str, secret: str) -> bool:
    """常量时间比较，且不让畸形请求头变成 500。

    bootstrap 口令是外部可比对的秘密，所以不能图省事用 ==；但请求头是任意
    UTF-8，hmac.compare_digest 收到非 ASCII str 会抛 TypeError——那等于任何人
    用一个乱码令牌就能把鉴权打成 500。编码规则不在这里重写，直接沿用
    auth._as_hash_bytes：同一条"先按字节、非 ASCII 不崩"的规则只有一份权威，
    否则两处各自的边角情况迟早对不上。
    """
    return hmac.compare_digest(_as_hash_bytes(supplied), _as_hash_bytes(secret))


def resolve_principal(request: Request) -> Optional[Principal]:
    """解析请求身份。返回 None 表示"没有身份"，由调用方决定 401/403。"""
    credential = _credential(request)
    if not credential:
        return None
    bootstrap = _bootstrap_token()
    if bootstrap and _secrets_match(credential, bootstrap):
        return BOOTSTRAP_PRINCIPAL
    return auth_store.resolve(credential)


def _has_any_identity() -> bool:
    """库里没有任何管理员、也没配 bootstrap 时，服务端就没有可服务的身份。

    这里的 admin 检查只认运维手工写进 users.json 的逃生口：角色永不通过请求
    产生，否则任何人都能给自己提权。

    判定走 has_role 而不是 list_users()：这条在每个受保护请求上都要跑一遍，
    把整个身份库连口令散列一起拷一份只为读一个字段，纯属白付。拷贝省掉了，
    实时性一点没省——has_role 在同一把锁里现读，所以删号/停用/手工把某个人的
    role 改回 user，下一句请求就按新状态回答。这里**不要**加缓存：那道门是
    fail-closed 的判据，读到过期身份等于把 503 变成放行。
    """
    return bool(_bootstrap_token()) or auth_store.has_role("admin")


def _live_admin_failures_locked(ip: str, now: float) -> list:
    """该来源在窗口内的失败时刻；顺带把过期项从表里删掉（调用方持锁）。"""
    times = _ADMIN_FAILS.get(ip)
    if not times:
        return []
    cutoff = now - ADMIN_FAIL_WINDOW_SECONDS
    times = [stamp for stamp in times if stamp > cutoff]
    if times:
        _ADMIN_FAILS[ip] = times
    else:
        _ADMIN_FAILS.pop(ip, None)
    return times


def _purge_expired_admin_failures_locked(now: float) -> None:
    """把整张表里已经滑出窗口的来源删掉（调用方持锁）。

    每个来源的时刻列表升序，所以看最后一项就知道整条是否过期：最后一项都过了
    cutoff，前面只可能更旧。彻底过期的来源留着只会占键位，硬上限淘汰时还要替它
    做一次比较。部分过期的条目不必在这里裁——单来源的读取路径
    （_live_admin_failures_locked）每次都会把自己裁干净，而每个列表本身最多
    ADMIN_FAIL_LIMIT 项。全表扫描只在超限那一刻发生，正常请求不付这份成本。
    """
    cutoff = now - ADMIN_FAIL_WINDOW_SECONDS
    for ip in [key for key, stamps in _ADMIN_FAILS.items() if stamps[-1] <= cutoff]:
        del _ADMIN_FAILS[ip]


def _evict_oldest_admin_failures_locked(now: float) -> None:
    """超出硬上限时先清完全过期项，再淘汰最久未触碰的来源（调用方持锁）。

    用 OrderedDict 的触碰序而不是每次现扫时间戳取 min：16ms 时钟粒度下先后两次
    失败的 monotonic 值可以相同，旧的普通 dict 实现里 min() 同值按插入序挑，
    刚刷新过的来源会被当成"最旧"淘汰掉。append 后 move_to_end 才是确定且 O(1)
    的 LRU。只淘汰到上限为止，不整表清空：整表清空既会重置攻击者的失败预算
    （等于把限速变成"撑满就松闸"），也会误伤最近还在尝试的来源。
    """
    if len(_ADMIN_FAILS) <= _ADMIN_FAILS_MAX_KEYS:
        return
    _purge_expired_admin_failures_locked(now)
    while len(_ADMIN_FAILS) > _ADMIN_FAILS_MAX_KEYS:
        _ADMIN_FAILS.popitem(last=False)


def _record_admin_failure_locked(ip: str, now: float) -> bool:
    """判定与记账同一个动作（调用方持 _ADMIN_FAILS_LOCK）。

    返回 False 表示该来源在窗口内已经没有预算；返回 True 表示这一条已经入账。
    判定和记账分成两步的话，同一瞬间的 N 个请求都能在任何人记账之前看到"还没满"。
    """
    times = _live_admin_failures_locked(ip, now)
    if len(times) >= ADMIN_FAIL_LIMIT:
        return False
    times.append(now)
    _ADMIN_FAILS[ip] = times
    _ADMIN_FAILS.move_to_end(ip)
    _evict_oldest_admin_failures_locked(now)
    return True


def _admin_attempt_start(ip: str, now: float) -> bool:
    """为一次"带了请求头凭据的受保护请求"预占额度；判定与记账在同一把锁里。

    返回 False 表示该来源在窗口内已经没有预算，调用方直接 429；返回 True 表示
    这一次尝试已经入账，解析失败时保留这条记录（它就是一次失败），解析成功则
    必须用同一个 now 调 _admin_attempt_success 把**这一条**还回去。

    预占把并发突发也关进了预算：判定和记账分成两步的话，同一瞬间的 N 个请求都能
    在任何人记账之前看到"还没满"。代价是同一时刻超过 ADMIN_FAIL_LIMIT 个带请求头
    凭据的合法请求，多出来的会被 429；只带 Cookie 的流量不经过这里，正常网页会话
    的**解析失败**走 _note_cookie_failure 补记，有效会话不受影响。
    """
    with _ADMIN_FAILS_LOCK:
        return _record_admin_failure_locked(ip, now)


def _note_cookie_failure(ip: str, now: float) -> bool:
    """记一次"只带 Cookie 的请求解析失败"；返回 False 表示该来源预算已满。

    与请求头共用一个预算，但走**失败后计数**而不是解析前预占：adopt 会把
    bootstrap 口令原样写进会话 Cookie，饱和来源上的正常管理台浏览器每个请求都
    带着它；预占会把这些合法会话一起 429。有效 Cookie 在调用方已经解析成功、
    根本走不到这里，所以不计数；猜测请求的代价是各自先做一次常量时间比较再吃
    429（低危取舍：比较之后不再服务任何业务逻辑）。
    """
    with _ADMIN_FAILS_LOCK:
        return _record_admin_failure_locked(ip, now)


def _admin_attempt_success(ip: str, now: float) -> None:
    """把本次预占的那一枚时间戳还回去：成功的请求不消耗失败预算。

    只删与 now 相等的那一条，绝不整本清空——否则一个正确口令就能替同一来源把
    窗口里的失败洗掉（管理员口令可以被撞对，失败记录不该跟着消失）。找不到就
    no-op：那条预占可能已被 LRU 淘汰或随来源过期清走，这时什么都不做比误删
    别人的记录安全。
    """
    with _ADMIN_FAILS_LOCK:
        times = _ADMIN_FAILS.get(ip)
        if not times:
            return
        try:
            times.remove(now)
        except ValueError:
            return
        if times:
            _ADMIN_FAILS[ip] = times
        else:
            _ADMIN_FAILS.pop(ip, None)


def reset_admin_throttle_for_tests() -> None:
    """只给测试用：清空按来源的 bootstrap 凭据失败账。生产没有"清零"这条运维动作。"""
    with _ADMIN_FAILS_LOCK:
        _ADMIN_FAILS.clear()


def install_auth(app) -> None:
    if _auth_mode() == "disabled":
        if _disabled_mode_allowed():
            print("⚠️ AUTH_MODE=disabled：所有请求均以本机管理员身份运行（当前绑定回环，仅限本机）")
        else:
            print(f"⛔ AUTH_MODE=disabled 但 HOST={_bind_host()} 绑到了对外网卡："
                  f"受保护请求将一律 503，直到改回回环或显式 {DISABLED_REMOTE_ENV}=1")

    @app.middleware("http")
    async def authenticate(request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or not path.startswith(_PROTECTED_PREFIXES):
            return await call_next(request)
        if is_public_path(path):
            return await call_next(request)

        if _auth_mode() == "disabled":
            # 技术护栏：disabled + 非回环绑定 = 把管理员身份挂在公网门把手上。
            # 请求期再判一次而不只靠启动打印：绑定参数可能在启动打印之后才被
            # 容器/脚本改环境覆盖，而这条 503 是唯一不依赖"有人看了日志"的防线。
            if not _disabled_mode_allowed():
                return JSONResponse(status_code=503, content={
                    "detail": "AUTH_MODE=disabled 不允许在对外网卡上运行："
                              "请改绑回环，或确认风险后设置 ALLOW_DISABLED_REMOTE=1"})
            request.state.principal = BOOTSTRAP_PRINCIPAL
            return await call_next(request)

        # CSRF 闸门只可能由"凭据出自 Cookie"触发：带请求头凭据的请求不是浏览器
        # 自动附带的东西（没有 CORS 就没有跨站自定义头，而受同源约束的自动信道
        # 恰恰只有 Cookie 一条），本身对 CSRF 免疫。顺序放在身份判定之前：一个
        # 既没登录又跨站伪造的 POST 该先被 CSRF 挡下，而不是替攻击者免费试探
        # "这台服务器配没配身份"。
        if (request.method in _UNSAFE_METHODS
                and not _header_credential(request)
                and _cookie_credential(request)
                and not request.headers.get(CSRF_HEADER, "").strip()):
            security_log.record("auth.blocked", request=request,
                                ip=client_ip(request), status=403,
                                detail="csrf.missing")
            return JSONResponse(status_code=403, content={"detail": CSRF_DETAIL})

        if not _has_any_identity():
            return JSONResponse(status_code=503,
                                content={"detail": "服务端未配置访问凭据"})

        ip = client_ip(request)
        # 任何受保护路径上的请求头凭据都进同一本账：resolve_principal 会拿它去比
        # bootstrap 口令，口径收窄到管理面/adopt 就等于给 /v1/auth/me、/v1/sessions
        # 留了一条不计数的比对器。只带 Cookie 的请求不能预占（adopt 把 bootstrap
        # 原样写进 Cookie，会让饱和来源上的正常管理台会话 429），改在解析失败后
        # 由 _note_cookie_failure 补记；没带任何凭据的 401 不计数（disabled 模式在
        # 上面的分支已整体放行）。
        attempt_now = None
        if _header_credential(request):
            attempt_now = time.monotonic()
            if not _admin_attempt_start(ip, attempt_now):
                # detail 沿用 limit.admin：管理台对它有现成映射。它现在表示
                # bootstrap 凭据尝试（任何受保护路径），不再是"管理面尝试"。
                security_log.record("auth.rate_limited", request=request, ip=ip,
                                    status=429, detail="limit.admin")
                # 文案固定、不回显凭据；Retry-After 用满窗口，宁可让客户端多等，
                # 不泄漏"这个来源还差几秒松闸"。
                return JSONResponse(
                    status_code=429,
                    content={"detail": ADMIN_THROTTLED_DETAIL},
                    headers={"Retry-After": str(int(ADMIN_FAIL_WINDOW_SECONDS))})

        principal = resolve_principal(request)
        if principal is None:
            # 解析失败：请求头预占留着，它已经是一笔失败（中间不再另记一次）。
            # 只带 Cookie 的失败在这里补记同一本账：有效 Cookie 在上一行已经解析
            # 成功、走不到这里；错 Cookie 每次落一笔，第 11 次起 429。
            if attempt_now is None and _cookie_credential(request):
                if not _note_cookie_failure(ip, time.monotonic()):
                    security_log.record("auth.rate_limited", request=request,
                                        ip=ip, status=429, detail="limit.admin")
                    return JSONResponse(
                        status_code=429,
                        content={"detail": ADMIN_THROTTLED_DETAIL},
                        headers={"Retry-After": str(int(ADMIN_FAIL_WINDOW_SECONDS))})
            security_log.record("auth.blocked", request=request,
                                ip=ip, status=401,
                                detail="token.unknown")
            return JSONResponse(status_code=401, content={"detail": UNAUTHORIZED_DETAIL})
        if attempt_now is not None:
            # 解析成功：释放本次预占，成功请求不消耗失败预算。
            _admin_attempt_success(ip, attempt_now)
        request.state.principal = principal
        response = await call_next(request)
        # v0.34.0：把"这枚会话还剩几秒"贴在每一个已鉴权的响应上。
        # 客户端据此决定要不要 renew（剩余 < 7 天才发一次，判据 1），所以这条头
        # 必须走**响应**而不是新端点：新端点要么免凭据（多一条探测面），要么要
        # 凭据（等于每个周期多一次往返）。
        # 只报不写：resolve 已经把到期时间算好了，这里不再查一次库、更不落盘。
        # bootstrap 管理员口令没有"到期"这回事，expires_in 是 None —— 头就不发，
        # 客户端拿不到时间就不会对着一个不存在的时钟去续期。
        remaining = getattr(principal, "expires_in", None)
        if remaining is not None and 200 <= response.status_code < 300:
            response.headers[TOKEN_EXPIRES_HEADER] = str(max(0, int(remaining)))
        return response


def current_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail=UNAUTHORIZED_DETAIL)
    return principal


def require_admin(request: Request) -> Principal:
    principal = current_principal(request)
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return principal


CurrentPrincipal = Depends(current_principal)
RequireAdmin = Depends(require_admin)
