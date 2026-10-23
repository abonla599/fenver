"""http_fetch——只抓管理员白名单站点的 https 页面正文（v0.37.0）。

与 providers 的 base_url 闸门共用 `app.core.net_guard` 的地址判据，但这一条路径
的输入是**模型给的 URL**（可被外部网页文本间接影响），所以 URL 契约比 base_url
更窄：仅 https、仅默认端口、无 userinfo、无 IP 字面量/IDN/尾点、主机必须先命中
`config_store.fetch_allowed_hosts()` 再解析、重定向手动跟且每一跳重过全部闸。

刻意**不 import** 的两样（隔离锁 tests/test_v0370_isolation_locks.py 钉着）：
* `app.sandbox`——沙箱容器 `network_disabled=True`，http_fetch 是它的对照面：
  出网只走这一条窄路，绝不给沙箱代码开一条"看起来受控"的取料通道；
* `app.core.providers`——两个出网面各自独立演进，共享的只有 net_guard 判据。
  解析器因此是本模块自己的 `_resolve_addresses`，不借 providers 的那一枚。

审计（判据 tests/test_v0370_http_fetch_contract.py）：每次调用（含拒绝与网络
失败）落一条 `audit.log`，只记 user_id、host、path、status、bytes、hops——
query/fragment/userinfo 一律剥掉（`?token=SECRET` 不许进账），正文、头、密钥
都不落。`host`/`path` 是**失败/成功发生在哪一跳**的目标（重定向后指向新目标），
原始锚点另记在 `origin_host`/`origin_path`；解析出的内网 IP 等敏感细节只进
`detail`，不进给模型看的错误文案。

时间语义（v0.37.0 审查 B3）：
* `HARD_TOTAL_TIMEOUT` 是**墙钟总时限**（一次 fetch() 从入口到出口的硬上限），
  每跳开始、每个响应块读取前都由单调时钟检查；
* 每跳新建 client 时把 read/connect 超时夹到剩余时间（`min(READ_TIMEOUT, 剩余)`），
  让单次阻塞也随 deadline 收缩。最坏情况是 `HARD_TOTAL_TIMEOUT` 加上一次
  read 阻塞（read 已被夹到剩余时间，超那个值的阻塞会按网络故障进冷却）；
* DNS 解析走 daemon 线程 + `queue.Queue.get(timeout=DNS_TIMEOUT)`：解析器卡住
  不会永久占住工作线程（不用会 join 线程的 ThreadPoolExecutor）。
"""
import ipaddress
import queue
import re
import socket
import threading
import time
from urllib.parse import urljoin, urlparse

import httpx

from app.core import audit, config_store
from app.core.net_guard import reject_reason
from app.core.tls import system_ssl_context
from app.tools.extract import EXTERNAL_CONTENT_NOTE, extract_text
from app.tools.response import ToolResponse

MAX_BODY_BYTES = 1024 * 1024          # 解码后 1 MiB：超过就中止，不进解析器
MAX_REDIRECTS = 3                     # 初始请求之外最多跟 3 跳，每跳重过全部闸
READ_TIMEOUT = 8.0                    # 单次 socket 读/写/池等待的默认上限
CONNECT_TIMEOUT = 3.5                 # 单次连接的上限
HARD_TOTAL_TIMEOUT = 15.0             # 一次 fetch() 的**墙钟硬总时限**（含 DNS/重定向/读体）
DNS_TIMEOUT = 3.0                     # 一次 getaddrinfo 的等待上限（daemon 线程 + 队列）
COOLDOWN_SECONDS = 600.0              # 一次真实网络失败后工具下线 10 分钟

# Content-Type 白名单：只收"正文可能是给人读的文字"的类型。image/*、
# application/pdf、application/octet-stream 一律拒——不是它们危险，是这里的
# 提取器读不出正文，返回一堆乱码比返回一句"类型不支持"更骗模型。
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml",
                         "text/plain", "application/json")

# 固定请求头四件套。绝不转发 Cookie/Authorization（每次新建 client，也不从
# 任何地方取凭据）；Accept-Encoding: identity 让响应不压回 gzip，字节预算才
# 量在真东西上。
FIXED_HEADERS = {
    "User-Agent": "fenver-ai/1.0 (server-side http_fetch; single admin-allowlisted fetch)",
    "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.1",
    "Accept-Language": "zh-CN,zh,en",
    "Accept-Encoding": "identity",
}

_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

_lock = threading.Lock()
_cooldown_until = 0.0                 # time.monotonic() 口径；0 = 从未失败


def client_kwargs(*, hop_timeout=None) -> dict:
    """出站客户端的参数表——唯一构造点，独立成纯函数给测试钉形状。

    `verify=system_ssl_context()` 与模型调用同一份信任锚（系统根证书库，见
    core/tls.py 的卡巴斯基备注）；`trust_env=False` 是不让 HTTP(S)_PROXY 环境
    变量把"已判过地址闸的连接"改道去任何中间人——判据与连接必须是同一条路。
    `follow_redirects=False`：重定向由 fetch 手动逐跳走，每一跳重过全部闸，
    httpx 自带的跟随会绕过白名单与 net_guard。

    `hop_timeout` 是一次 fetch() 剩余的墙钟时间：read/connect 都取
    `min(默认值, 剩余)`，让单次阻塞随 deadline 收缩；不传则用默认值。
    """
    if hop_timeout is None:
        read_timeout, connect_timeout = READ_TIMEOUT, CONNECT_TIMEOUT
    else:
        remaining = max(0.001, float(hop_timeout))
        read_timeout = min(READ_TIMEOUT, remaining)
        connect_timeout = min(CONNECT_TIMEOUT, remaining)
    return {"verify": system_ssl_context(), "trust_env": False,
            "follow_redirects": False,
            "timeout": httpx.Timeout(read_timeout, connect=connect_timeout)}


def make_client(*, hop_timeout=None) -> httpx.Client:
    """每跳新建、绝不复用：httpx 的 client 会攒 Set-Cookie，复用就把上一跳的
    cookie 带给了下一跳，那是"不转发 Cookie"的反面。"""
    return httpx.Client(**client_kwargs(hop_timeout=hop_timeout))


def _resolve_addresses(host: str, timeout=None) -> list:
    """主机名 → 地址字面量列表（去重、稳定排序）。本模块自带的注入点。

    与 providers 同形但**不 import 它**（见模块 docstring）：conftest 的 autouse
    DNS 桩钉的是 providers 那一枚，本模块的测试各自 monkeypatch 这里；真实出站
    解析走 socket.getaddrinfo，端口固定问 443（这一条路只走 https）。

    `getaddrinfo` 没有超时参数，慢 DNS 会先于 net_guard 把工作线程占住。这里用
    daemon 线程跑解析、主线程只 `Queue.get(timeout=DNS_TIMEOUT)`：超时抛
    `TimeoutError`（可读文案），由 net_guard 折成 fail-closed 的拒绝理由并进审计。
    刻意不用 ThreadPoolExecutor——它的 `__exit__` 会 join 工作线程，等于把超时
    又等回去。
    """
    limit = DNS_TIMEOUT if timeout is None else float(timeout)
    result = queue.Queue(maxsize=1)
    # 在起线程前把解析函数与常量抓成局部名：线程跑起来后即使调用方
    # monkeypatch 已还原，工作线程看到的仍是发起这次解析时的那一枚，
    # 不会"超时之后又去做一次真实 DNS"。
    getaddrinfo = socket.getaddrinfo
    family, socktype = socket.AF_UNSPEC, socket.SOCK_STREAM

    def _worker():
        try:
            infos = getaddrinfo(host, 443, family, socktype)
            result.put_nowait(("ok", infos))
        except Exception as exc:                     # noqa: BLE001 - 交给主线程决定
            try:
                result.put_nowait(("err", exc))
            except queue.Full:                       # 主线程已经超时走了
                pass

    worker = threading.Thread(target=_worker, name=f"dns-{host[:24]}", daemon=True)
    worker.start()
    try:
        kind, payload = result.get(timeout=limit)
    except queue.Empty:
        raise TimeoutError(f"DNS 解析超时（{limit:.1f} 秒）") from None
    if kind == "err":
        raise payload
    return sorted({info[4][0] for info in payload})


def contract_reject_reason(url: str) -> str:
    """URL 形状契约。返回 "" = 过，否则是拒的理由（给模型看的人话）。

    判序：控制字符 → scheme → userinfo → 端口 → 主机名形状。主机名一律用
    urlparse 的 `.hostname`（已小写、已去方括号），非 ASCII/IDN、IP 字面量、
    **整数/八进制/十六进制 IP 形状**、尾点、单标签、通配、下划线全在形状关拒掉——
    **不给解析器任何机会**去"看看它指着哪"。

    控制字符必须在 `urlparse` **之前**拒：`urlparse` 会静默删掉 `\\t\\r\\n`，
    清洗后的结果过了契约、原始串再交给 httpx，就会变成一条绕过审计与冷却的
    `InvalidURL`（v0.37.0 审查 B2 的实测路径）。
    """
    text = str(url or "").strip()
    if not text:
        return "URL 为空"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text):
        return "URL 含控制字符（\\r、\\n、\\t、\\x00 等），一律拒绝"
    try:
        parsed = urlparse(text)
    except ValueError:
        return "URL 解析失败"
    if parsed.scheme != "https":
        return "只允许 https 方案（http、其它方案与无方案一律拒）"
    if "@" in (parsed.netloc or ""):
        return "URL 不允许携带 userinfo（user:pass@ 或 @ 都不行）"
    try:
        port = parsed.port
    except ValueError:
        return "URL 端口写法不合法"
    if port not in (None, 443):
        return f"只允许默认端口/443，收到 {port}"
    host = parsed.hostname or ""
    if not host:
        return "URL 里没有主机名"
    try:
        ipaddress.ip_address(host.strip("[]"))
        return "不接受 IP 字面量——白名单配的是域名，URL 也请写域名"
    except ValueError:
        pass
    if "%" in host:
        return "URL 主机名带 zone-id（%接口），链路作用域地址不是可抓取对象"
    if any(ord(ch) > 127 for ch in host):
        return "非 ASCII/IDN 主机名一律拒——白名单里没有这种写法"
    if host.endswith("."):
        return "主机名尾点拒：白名单比较不做 DNS 根别名"
    labels = host.split(".")
    if len(labels) < 2:
        return "主机名至少两段（裸单标签不进白名单）"
    for label in labels:
        # 这些形状都会被 inet_aton/浏览器解释成 IPv4，但 `urlparse`/`ipaddress`
        # 不认。形状关就要拒，别把"是不是地址"的解释权留给下游解析器。
        if len(label) > 1 and label[0] == "0" and label[1:].isdigit():
            return "主机名里出现前导零数字标签——八进制 IP 写法一律拒"
        if label.startswith("0x"):
            return "主机名里出现 0x 标签——十六进制 IP 写法一律拒"
        if label.isdigit():
            return "主机名里出现纯数字标签——整数形式的 IP 一律拒"
    if not all(_LABEL_RE.match(label) for label in labels):
        return "主机名不是合法的小写 ASCII LDH 标签序列"
    if any(label.startswith("xn--") for label in labels):
        return "punycode（xn-- 标签）是 IDN 的另一种写法，同样拒"
    if len(host) > 253:
        return "主机名超过 253 字符"
    return ""


def _audit(user_id: str, host: str, path: str, status: str,
           nbytes: int, hops: int, *, origin_host: str = "",
           origin_path: str = "", detail: str = "") -> None:
    """一次调用一笔账，成败都记。写账失败绝不把一次好抓取变成失败（抄 core/audit）。"""
    try:
        target = {"host": host, "path": path, "status": status,
                  "bytes": nbytes, "hops": hops}
        if origin_host or origin_path:
            target["origin_host"] = origin_host
            target["origin_path"] = origin_path
        if detail:
            # 细节（解析出的内网 IP、DNS 超时原文、content-type）只进审计，
            # 不进给模型看的错误文案。
            target["detail"] = str(detail)[:400]
        audit.log({"user_id": user_id or "anon"}, "http_fetch", target=target)
    except Exception as e:
        print(f"⚠️ http_fetch 的审计这本账在漏（这次记不上）：{e}")


def _fail(reason: str, status_tag: str, *, hint: str = "",
          user="", host="", path="", nbytes=0, hops=0,
          origin_host="", origin_path="", detail=""):
    _audit(user, host, path, status_tag, nbytes, hops,
           origin_host=origin_host, origin_path=origin_path, detail=detail)
    return ToolResponse(False, error=f"http_fetch 拒绝：{reason}",
                        hint=hint or "不要换个写法再试一次；把理由如实告诉用户。")


def _public_block_reason(host: str, why: str) -> str:
    """把 net_guard 的详细拒绝理由收成给模型看的版本：只留类别与主机名。

    net_guard 的理由句要同时服务 providers 的管理端排障，里面会带解析出的
    169.254.169.254/10.x 这类地址；那些细节只进审计的 `detail`，模型看到的
    只需要"这类地址不许出网"。
    """
    if "zone-id" in why:
        kind = "带 zone-id 的地址"
    elif "本机" in why:
        kind = "本机地址"
    elif "链路本地" in why:
        kind = "链路本地地址"
    elif "组播" in why:
        kind = "组播地址"
    elif "内网" in why:
        kind = "内网地址"
    elif "解析失败" in why:
        return f"主机 {host} 解析失败，服务端不放行解析不出来的地址"
    elif "解析结果为空" in why:
        return f"主机 {host} 解析结果为空，服务端不放行"
    else:
        kind = "受限地址"
    return f"主机 {host} 指向{kind}，服务端不替它出网"


def fetch(url: str, *, user_id: str = ""):
    """抓一页白名单内容，返回 ToolResponse（正文带外部内容标注）。

    失败路径的纪律（审查 B2）：契约/名单/地址闸之外的一切——建 client、
    `urljoin`、读响应、解码、`extract_text`——都在同一个 try 里；任何异常都
    走 `_fail` 落一条审计并进冷却，error 只给固定人话 + 异常类名。
    """
    raw_url = str(url or "").strip()
    # 审计锚点：从**原始** URL 尽力取 host/path；query/fragment/userinfo 在这一步
    # 就被 urlparse 天然剥掉，坏 URL 解析不动就留空，账宁可少一栏不落秘密。
    try:
        _p = urlparse(raw_url)
        origin_host = _p.hostname or ""
        origin_path = _p.path or ""
    except ValueError:
        origin_host, origin_path = "", ""
    audit_host, audit_path = origin_host, origin_path

    def fail(reason, tag, nbytes=0, hops=0, hint="", detail=""):
        return _fail(reason, tag, user=user_id, host=audit_host, path=audit_path,
                     origin_host=origin_host, origin_path=origin_path,
                     nbytes=nbytes, hops=hops, hint=hint, detail=detail)

    deadline = time.monotonic() + HARD_TOTAL_TIMEOUT

    try:
        allow = set(config_store.fetch_allowed_hosts())
    except Exception as e:                       # noqa: BLE001 - 不许把失败漏给执行器
        return fail("读取 fetch_allowed_hosts 白名单配置失败，本工具暂不可用",
                    "failed", detail=f"{type(e).__name__}: {str(e)[:200]}")

    if not allow:
        return fail("管理员还没有配置 fetch_allowed_hosts 白名单，本工具不可用",
                    "no_allowlist")
    with _lock:
        cooling = _cooldown_until - time.monotonic()
    if cooling > 0:
        return fail(f"上一次抓取遇到网络故障，本工具进入 {int(COOLDOWN_SECONDS)} 秒冷却，"
                    f"还需等待约 {int(cooling) + 1} 秒", "cooldown",
                    hint="不要重试；稍后再来或如实告诉用户现在抓不了。")

    current = raw_url
    hops = 0
    while hops <= MAX_REDIRECTS:
        if time.monotonic() >= deadline:
            return fail(f"抓取超过 {HARD_TOTAL_TIMEOUT:g} 秒硬总时限，已中止",
                        "timeout", hops=hops, detail="hard_total_timeout_at_hop")
        reason = contract_reject_reason(current)
        if reason:
            return fail(f"URL 契约不过：{reason}", "contract_rejected", hops=hops)
        try:
            host = urlparse(current).hostname or ""
        except ValueError:
            return fail("URL 契约不过：URL 解析失败", "contract_rejected", hops=hops)
        # 先查白名单、再解析：名单外的名字一次 DNS 都不配拥有——探测一个
        # "管理员没准过"的域名本身就是在替别人扫。
        if host not in allow:
            return fail(f"主机 {host} 不在 fetch_allowed_hosts 白名单里",
                        "off_allowlist", hops=hops,
                        hint="只有管理员配置的站点抓得到；别换写法绕，告诉用户站点没被允许。")
        # resolve 在**调用时**从模块属性取（裸全局名每次现查）——monkeypatch 打桩
        # 才生效，与 providers 同一条注入点纪律。
        why = reject_reason(host, resolve=_resolve_addresses, allow_loopback=False,
                            where="url", loopback_hint="")
        if why:
            # 详细理由（含解析出的内网 IP）只进审计；给模型的只留类别与主机名。
            return fail(_public_block_reason(host, why), "blocked_address",
                        hops=hops, detail=why)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return fail(f"抓取超过 {HARD_TOTAL_TIMEOUT:g} 秒硬总时限，已中止",
                        "timeout", hops=hops, detail="hard_total_timeout_before_request")
        client = None
        total = 0
        try:
            client = make_client(hop_timeout=remaining)
            with client.stream("GET", current, headers=FIXED_HEADERS) as resp:
                status_code = resp.status_code
                if 300 <= status_code < 400:
                    location = resp.headers.get("location") or ""
                    if not location:
                        return fail("重定向响应没有 Location，没地方可跟",
                                    "redirect_no_location", hops=hops)
                    redirect = urljoin(current, location)
                    # 审计目标跟着跳：第二跳被拦/被拒时，账上要能看见目标
                    # host/path（query/fragment/userinfo 仍由 urlparse 剥掉）。
                    try:
                        _rp = urlparse(redirect)
                        audit_host = _rp.hostname or audit_host
                        audit_path = _rp.path or "/"
                    except ValueError:
                        pass
                else:
                    redirect = ""
                    if status_code >= 400:
                        return fail(f"远端返回 HTTP {status_code}", f"http_{status_code}",
                                    hops=hops, hint="站点自己不给，别再敲它。")
                    raw_ctype = (resp.headers.get("content-type") or "")
                    ctype = raw_ctype.split(";")[0].strip().lower()
                    if ctype not in ALLOWED_CONTENT_TYPES:
                        # 服务端控制的 header 不回显：只报类别，原文截断后进审计。
                        return fail("内容类型不在支持的清单里（只收 html/xhtml/text/json）",
                                    "content_type", hops=hops,
                                    detail=f"content_type={raw_ctype[:64]!r}")
                    chunks = []
                    iterator = resp.iter_bytes()
                    while True:
                        if time.monotonic() >= deadline:
                            return fail(f"抓取超过 {HARD_TOTAL_TIMEOUT:g} 秒硬总时限，已中止",
                                        "timeout", nbytes=total, hops=hops,
                                        detail="hard_total_timeout_while_reading")
                        try:
                            chunk = next(iterator)
                        except StopIteration:
                            break
                        total += len(chunk)
                        if total > MAX_BODY_BYTES:
                            return fail("页面体超过 1 MiB，已中止读取", "too_large",
                                        nbytes=total, hops=hops)
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    charset = resp.charset_encoding or "utf-8"
                    try:
                        text = body.decode(charset, errors="replace")
                    except LookupError:
                        # 站点声明了 Python 没有的 codec（`x-gbk` 这类历史别名）：
                        # 坏声明不配把一次能读的页面变成失败，按 utf-8 + replace 读。
                        text = body.decode("utf-8", errors="replace")
            if redirect:
                if hops >= MAX_REDIRECTS:
                    return fail(f"重定向已超过 {MAX_REDIRECTS} 跳上限，停止跟随",
                                "too_many_redirects", hops=hops)
                current = redirect
                hops += 1
                continue
            article = extract_text(ctype, text)
            _audit(user_id, audit_host, audit_path or "/", "ok", total, hops,
                   origin_host=origin_host, origin_path=origin_path)
            # 来源行用**最后一跳**的 host+path（正文确实来自那个地址），审计仍记
            # 同一跳的目标并另存原始锚点。
            final_path = urlparse(current).path or "/"
            return ToolResponse(True, data=(
                EXTERNAL_CONTENT_NOTE + f"来源：https://{host}{final_path}"
                f"（HTTP {status_code}，{len(body)} 字节，正文如下）\n\n{article}"))
        except httpx.TransportError as e:
            # 真实网络故障（超时/连不上/协议错）：进冷却，别让注入驱动的路径
            # 把"反复敲一个挂掉的站点"变成免费重试。
            _enter_cooldown()
            _audit(user_id, audit_host, audit_path,
                   f"network_error:{type(e).__name__}", 0, hops,
                   origin_host=origin_host, origin_path=origin_path,
                   detail=f"{type(e).__name__}: {str(e)[:200]}")
            return ToolResponse(False, error=(
                f"http_fetch 失败：网络连接没有完成（{type(e).__name__}）"),
                hint="站点没接通，工具已进入冷却；不要重试，稍后再来"
                     "或如实告诉用户现在抓不了。")
        except Exception as e:                   # noqa: BLE001 - 审查 B2 的兜底
            # 非 TransportError 的逃逸（InvalidURL/DecodingError/urljoin 的
            # ValueError/解析器异常…）：必须落审计、进冷却、文案可控。原始异常
            # 串可能带上恶意 URL/header，只把类名给模型，全文进审计。
            _enter_cooldown()
            return fail(f"抓取过程遇到内部错误（{type(e).__name__}），已中止",
                        "failed", hops=hops,
                        detail=f"{type(e).__name__}: {str(e)[:200]}")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:                # noqa: BLE001 - 关不上不改变结果
                    pass
    return fail(f"重定向已超过 {MAX_REDIRECTS} 跳上限，停止跟随",
                "too_many_redirects", hops=hops)


def _enter_cooldown() -> None:
    global _cooldown_until
    with _lock:
        _cooldown_until = time.monotonic() + COOLDOWN_SECONDS


def fetch_available() -> bool:
    """给 registry `available=` 的那一句：名单非空且不在网络故障冷却期。

    与 web_search 的探测同一取向——模型不该看见一个必然失败的工具；冷却期
    就是"这台机器现在到那批站点的路上有毛病"的实测结论。

    v0.37.0 审查 N14：配置读取本身失败时**自己 catch 并返回 False**（fail-closed）。
    registry 的兜底是"检查炸了按可用处理"，这条工具不能借用那个方向——名单都
    读不出来时，坏掉的配置面不该被模型看见成一个可点的按钮。
    """
    try:
        if not config_store.fetch_allowed_hosts():
            return False
    except Exception as e:                       # noqa: BLE001 - 宁可藏起来
        print(f"⚠️ http_fetch 读取白名单失败，按不可用处理：{e}")
        return False
    with _lock:
        return time.monotonic() >= _cooldown_until
