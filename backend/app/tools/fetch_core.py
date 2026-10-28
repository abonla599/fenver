"""fetch_core——服务端出站抓取判据的唯一实现（v0.37.4）。

`http_fetch`（管理员白名单）与 `web_read`（搜索结果正文）共用这一份出站判据：
URL 形状、DNS 解析、逐跳重定向、地址闸、响应预算、正文提取、审计。两个工具
只负责各自的**目的地准入策略**（白名单 / 结果映射）与对外文案，不再各留一份
SSRF 判据。隔离锁 `tests/test_v0374_single_source_of_egress_predicates.py`
钉着"全场只有这一份"。

刻意**不 import** 的两样（`tests/test_v0370_isolation_locks.py` 钉着）：
* `app.sandbox`——沙箱容器 `network_disabled=True`，本模块是它的对照面；
* `app.core.providers`——两个出网面共享的只有 `net_guard` 判据，解析器不借用。

DNS rebinding：每一跳只解析一次，判闸与连接用的是同一份地址列表——
`make_client(pins=...)` 把这份列表交给 `PinnedBackend`（定义已搬到
`app.core.pinned_http`，本模块按历史名字 `_PinnedBackend` 再导出），连接层
只连字面量，主机名只用于 Host/SNI。

时间语义（继承 v0.37.0 审查 B3）：
* `total_timeout` 是**墙钟总时限**，每跳开始、每个响应块读取前都由单调时钟检查；
* 每跳新建 client 时把 read/connect 超时夹到剩余时间，单次阻塞也随 deadline 收缩；
* DNS 解析走 daemon 线程 + `queue.Queue.get(timeout=...)`：解析器卡住不会永久占住
  工作线程（不用会 join 线程的 ThreadPoolExecutor）。
"""
import ipaddress
import queue
import re
import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

from app.core import audit
from app.core.net_guard import reject_reason
from app.core.pinned_http import (  # noqa: F401 - PER_ADDRESS_CONNECT_CAP re-export
    PER_ADDRESS_CONNECT_CAP,
    PinnedBackend,
    install_pins,
)
from app.core.tls import system_ssl_context
from app.tools.extract import BODY_CHAR_LIMIT, EMPTY_BODY_TEXT, extract_text

MAX_BODY_BYTES = 1024 * 1024          # 解码后 1 MiB：超过就中止，不进解析器
MAX_REDIRECTS = 3                     # 初始请求之外最多跟 3 跳，每跳重过全部闸
READ_TIMEOUT = 8.0                    # 单次 socket 读/写/池等待的默认上限
CONNECT_TIMEOUT = 3.5                 # 单次连接的上限
HARD_TOTAL_TIMEOUT = 15.0             # 一次 fetch() 的**墙钟硬总时限**（含 DNS/重定向/读体）
DNS_TIMEOUT = 3.0                     # 一次 DNS 解析的等待上限（daemon 线程 + 队列）
MAX_LOCATION_LENGTH = 2048            # 单个 Location 头的长度上限

# Content-Type 白名单：只收"正文可能是给人读的文字"的类型。image/*、
# application/pdf、application/octet-stream 一律拒——不是它们危险，是这里的
# 提取器读不出正文，返回一堆乱码比返回一句"类型不支持"更骗模型。
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml",
                         "text/plain", "application/json")

# 固定请求头四件套。绝不转发 Cookie/Authorization（每次新建 client，也不从
# 任何地方取凭据）；Accept-Encoding: identity 让响应不压回 gzip，字节预算才
# 量在真东西上。
FIXED_HEADERS = {
    "User-Agent": "fenver-ai/1.0 (server-side fetch; validated egress only)",
    "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.1",
    "Accept-Language": "zh-CN,zh,en",
    "Accept-Encoding": "identity",
}

# 默认 URL 契约：v0.37.0 的 https-only/443。`http_fetch` 不传参数，行为逐字不变；
# `web_read` 显式传 ("http", "https") 与 80/443。
DEFAULT_SCHEMES = ("https",)
DEFAULT_PORTS = {"https": {None, 443}}

_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class FetchOutcome:
    """一次抓取的结果。`text` 成功时是正文，失败时是给调用方拼错误文案的人话理由。"""

    ok: bool
    text: str = ""
    status: str = ""
    host: str = ""
    path: str = ""
    bytes: int = 0
    hops: int = 0
    final_url: str = ""
    final_status: int = 0
    network_error: str = ""
    content_type: str = ""


def client_kwargs(*, hop_timeout=None, connect_timeout=None) -> dict:
    """出站客户端的参数表——唯一构造点，独立成纯函数给测试钉形状。

    `verify=system_ssl_context()` 与模型调用同一份信任锚（系统根证书库，见
    core/tls.py 的卡巴斯基备注）；`trust_env=False` 是不让 HTTP(S)_PROXY 环境
    变量把"已判过地址闸的连接"改道去任何中间人——判据与连接必须是同一条路。
    `follow_redirects=False`：重定向由本模块手动逐跳走，每一跳重过全部闸，
    httpx 自带的跟随会绕过目的地策略与 net_guard。
    """
    connect_cap = CONNECT_TIMEOUT if connect_timeout is None else float(connect_timeout)
    if hop_timeout is None:
        read_timeout, connect_limit = READ_TIMEOUT, connect_cap
    else:
        remaining = max(0.001, float(hop_timeout))
        read_timeout = min(READ_TIMEOUT, remaining)
        connect_limit = min(connect_cap, remaining)
    return {"verify": system_ssl_context(), "trust_env": False,
            "follow_redirects": False,
            "timeout": httpx.Timeout(read_timeout, connect=connect_limit)}


# 历史名字：`http_fetch._PinnedBackend` 与既有测试按这个类名引用；实现只有
# core/pinned_http.py 那一份。
_PinnedBackend = PinnedBackend


def make_client(*, hop_timeout=None, pins=None, connect_timeout=None) -> httpx.Client:
    """每跳新建、绝不复用：httpx 的 client 会攒 Set-Cookie，复用就把上一跳的
    cookie 带给了下一跳，那是"不转发 Cookie"的反面。

    `pins` 是 `{hostname: [已由 net_guard 校验过的地址]}`：把传输层的
    `connect_tcp` 换成只连校验过的字面量；TLS 的 SNI 与 Host 头仍由 httpcore
    用原主机名设置，证书校验不受影响。钉不住（空 pins / 传输层形状变了）时
    关掉刚建的 client 再抛——与旧实现同一行为。"""
    client = httpx.Client(**client_kwargs(hop_timeout=hop_timeout,
                                          connect_timeout=connect_timeout))
    if pins is not None:
        try:
            install_pins(client, pins)
        except Exception:
            client.close()
            raise
    return client


def _resolve_addresses(host: str, timeout=None, *, socket_module=None) -> list:
    """主机名 → 地址字面量列表（去重、保留系统解析的原序）。

    原序不是装饰：双栈机器上系统通常把可用的 IPv6 排在前面，排序会把 IPv4
    顶到前面，白白耗掉连接预算。端口固定问 443 只为取地址，地址判据与端口无关。

    DNS 解析没有超时参数，慢解析会先于 net_guard 把工作线程占住。这里用
    daemon 线程跑解析、主线程只等队列：超时抛 `TimeoutError`（可读文案），由
    net_guard 折成 fail-closed 的拒绝理由并进审计。刻意不用
    ThreadPoolExecutor——它的 `__exit__` 会 join 工作线程，等于把超时又等回去。

    `socket_module` 是给薄壳的注入点：`http_fetch` 的历史测试 monkeypatch 的是
    它自己模块上的 `socket` 与 `DNS_TIMEOUT`，薄壳把这些转交进来，判据仍只有
    这一份。
    """
    mod = socket_module if socket_module is not None else socket
    limit = DNS_TIMEOUT if timeout is None else float(timeout)
    result = queue.Queue(maxsize=1)
    # 在起线程前把解析函数与常量抓成局部名：线程跑起来后即使调用方
    # monkeypatch 已还原，工作线程看到的仍是发起这次解析时的那一枚，
    # 不会"超时之后又去做一次真实 DNS"。
    resolve = mod.getaddrinfo
    family, socktype = mod.AF_UNSPEC, mod.SOCK_STREAM

    def _worker():
        try:
            infos = resolve(host, 443, family, socktype)
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
    ordered, seen = [], set()
    for info in payload:
        ip = info[4][0]
        if ip not in seen:
            seen.add(ip)
            ordered.append(ip)
    return ordered


def contract_reject_reason(url: str, *, schemes=DEFAULT_SCHEMES, ports=None) -> str:
    """URL 形状契约。返回 "" = 过，否则是拒的理由（给模型看的人话）。

    判序：控制字符 → scheme → userinfo → 端口 → 主机名形状。主机名一律用
    urlparse 的 `.hostname`（已小写、已去方括号），非 ASCII/IDN、IP 字面量、
    **整数/八进制/十六进制 IP 形状**、尾点、单标签、通配、下划线全在形状关拒掉——
    **不给解析器任何机会**去"看看它指着哪"。

    控制字符必须在 `urlparse` **之前**拒：`urlparse` 会静默删掉 `\\t\\r\\n`，
    清洗后的结果过了契约、原始串再交给 httpx，就会变成一条绕过审计与冷却的
    `InvalidURL`（v0.37.0 审查 B2 的实测路径）。

    `schemes`/`ports` 是目的地策略的一部分：默认保持 v0.37.0 的 https-only/443；
    `web_read` 传 http+https 与 80/443。判据本身只有这一份。
    """
    allowed_schemes = tuple(str(s).lower() for s in schemes)
    allowed_ports = DEFAULT_PORTS if ports is None else ports
    text = str(url or "").strip()
    if not text:
        return "URL 为空"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text):
        return "URL 含控制字符（\\r、\\n、\\t、\\x00 等），一律拒绝"
    try:
        parsed = urlparse(text)
    except ValueError:
        return "URL 解析失败"
    scheme = (parsed.scheme or "").lower()
    if scheme not in allowed_schemes:
        if allowed_schemes == DEFAULT_SCHEMES:
            return "只允许 https 方案（http、其它方案与无方案一律拒）"
        return f"只允许 {'/'.join(allowed_schemes)} 方案（其它方案与无方案一律拒）"
    if "@" in (parsed.netloc or ""):
        return "URL 不允许携带 userinfo（user:pass@ 或 @ 都不行）"
    try:
        port = parsed.port
    except ValueError:
        return "URL 端口写法不合法"
    allowed_for_scheme = allowed_ports.get(scheme, set())
    if port not in allowed_for_scheme:
        if scheme == "https" and allowed_for_scheme == {None, 443}:
            return f"只允许默认端口/443，收到 {port}"
        shown = "、".join(str(p) for p in sorted(
            p for p in allowed_for_scheme if p is not None)) or "无"
        return f"只允许默认端口/{shown}，收到 {port}"
    host = parsed.hostname or ""
    if not host:
        return "URL 里没有主机名"
    try:
        ipaddress.ip_address(host.strip("[]"))
        return "不接受 IP 字面量——这里只抓域名，URL 也请写域名"
    except ValueError:
        pass
    if "%" in host:
        return "URL 主机名带 zone-id（%接口），链路作用域地址不是可抓取对象"
    if any(ord(ch) > 127 for ch in host):
        return "非 ASCII/IDN 主机名一律拒"
    if host.endswith("."):
        return "主机名尾点拒：比较不做 DNS 根别名"
    labels = host.split(".")
    if len(labels) < 2:
        return "主机名至少两段（裸单标签不受理）"
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


def _scrub_audit_detail(detail: str) -> str:
    """审计 detail 里出现 URL 时整段替换：异常原文可能带 query/fragment/userinfo。"""
    text = re.sub(r"(?i)\b(?:https?://|//)[^\s'\"<>]+", "<url>", str(detail))
    text = re.sub(r"(?i)\b[A-Za-z0-9._%+-]+:[^/\s@'\"<>]+@[^\s'\"<>]+", "<url>", text)
    text = re.sub(r"\?[^\s'\"<>]*", "?<redacted>", text)
    text = re.sub(r"#[^\s'\"<>]*", "#<redacted>", text)
    return text[:400]


def audit_fetch(action: str, user_id: str, host: str, path: str, status: str,
                nbytes: int, hops: int, *, origin_host: str = "",
                origin_path: str = "", detail: str = "") -> None:
    """一次调用一笔账，成败都记。写账失败绝不把一次好抓取变成失败（抄 core/audit）。

    `target` 只落 host/path/status/bytes/hops（另加 origin_* 与已剥 URL 的 detail）；
    query/fragment/userinfo 在调用方由 urlparse 剥掉，正文、标题、头部值不传进来。
    """
    try:
        target = {"host": host, "path": path, "status": status,
                  "bytes": nbytes, "hops": hops}
        if origin_host or origin_path:
            target["origin_host"] = origin_host
            target["origin_path"] = origin_path
        if detail:
            # 细节（解析出的内网 IP、DNS 超时原文、content-type）只进审计，
            # 不进给模型看的错误文案。
            target["detail"] = _scrub_audit_detail(detail)
        audit.log({"user_id": user_id or "anon"}, action, target=target)
    except Exception as e:                       # noqa: BLE001 - 审计不许带崩抓取
        print(f"⚠️ {action} 的审计这本账在漏（这次记不上）：{e}")


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


def fetch(url: str, *, policy, schemes=DEFAULT_SCHEMES, ports=None,
          user_id: str = "", audit_action: str = "fetch",
          require_nonempty: bool = False, char_limit=None,
          max_bytes: int = MAX_BODY_BYTES, total_timeout: float = HARD_TOTAL_TIMEOUT,
          connect_timeout: float = CONNECT_TIMEOUT,
          max_redirects: int = MAX_REDIRECTS, client_factory=None,
          resolver=None, urljoin_func=None) -> FetchOutcome:
    """抓取一页并返回 `FetchOutcome`；**唯一**的逐跳出站循环。

    `policy(host) -> str` 是目的地准入：返回 "" 放行，否则是拒的理由（审计状态
    记为 `off_allowlist`；`web_read` 传恒空策略，`http_fetch` 传管理员白名单）。
    其余闸（契约/地址/逐跳重验/预算/审计）不因调用方而变。

    `client_factory` / `resolver` / `urljoin_func` 是测试注入点；生产调用方从
    自己模块的属性取（`http_fetch` 这样做，保证既有 monkeypatch 继续生效）。
    """
    factory = make_client if client_factory is None else client_factory
    resolve_host = _resolve_addresses if resolver is None else resolver
    joiner = urljoin if urljoin_func is None else urljoin_func
    # policy 是目的地准入的唯一入口；None/空值不是"全放行"，而是调用方忘了传。
    # 静默放行会把"少写一个参数"变成"所有地址都能抓"，所以这里直接炸。
    if not callable(policy):
        raise TypeError("fetch_core.fetch 需要 policy(host) callable，None 不是全放行")
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

    deadline = time.monotonic() + float(total_timeout)

    def fail(reason, tag, nbytes=0, hops=0, detail=""):
        audit_fetch(audit_action, user_id, audit_host, audit_path, tag, nbytes,
                    hops, origin_host=origin_host, origin_path=origin_path,
                    detail=detail)
        return FetchOutcome(ok=False, text=reason, status=tag, host=audit_host,
                            path=audit_path, bytes=nbytes, hops=hops)

    current = raw_url
    hops = 0
    prev_scheme = ""
    while hops <= max_redirects:
        if time.monotonic() >= deadline:
            return fail(f"抓取超过 {total_timeout:g} 秒硬总时限，已中止",
                        "timeout", hops=hops, detail="hard_total_timeout_at_hop")
        reason = contract_reject_reason(current, schemes=schemes, ports=ports)
        if reason:
            return fail(f"URL 契约不过：{reason}", "contract_rejected", hops=hops)
        try:
            parsed_current = urlparse(current)
            host = parsed_current.hostname or ""
            scheme = (parsed_current.scheme or "").lower()
        except ValueError:
            return fail("URL 契约不过：URL 解析失败", "contract_rejected", hops=hops)
        # https 之后只许 https：web_read 允许 http，但一条 https 结果被 302 到
        # http 就是明文降级，必须在解析/连接之前拒。http_fetch 的契约先拒 http，
        # 所以这条对它是第二道同向的闸。
        if prev_scheme == "https" and scheme != "https":
            return fail("重定向把 https 降级成 http，拒绝跟随",
                        "redirect_downgrade", hops=hops)
        policy_reason = policy(host) if policy else ""
        if policy_reason:
            return fail(policy_reason, "off_allowlist", hops=hops)
        # 解析只做一次：这一份地址列表既交给 net_guard 判，也交给连接层钉住。
        # 两次解析之间的 DNS 换答案就是 rebinding 窗口，不能留。
        try:
            addresses = resolve_host(host)
        except Exception as e:                   # noqa: BLE001 - 解析失败一律不放行
            return fail(f"主机 {host} 解析失败，服务端不放行", "blocked_address",
                        hops=hops,
                        detail=f"resolve_error={type(e).__name__}: {str(e)[:120]}")
        if not addresses:
            return fail(f"主机 {host} 解析结果为空，服务端不放行",
                        "blocked_address", hops=hops, detail="resolve_empty")
        why = reject_reason(host, resolve=lambda _h: addresses,
                            allow_loopback=False, where="url", loopback_hint="")
        if why:
            # 详细理由（含解析出的内网 IP）只进审计；给模型的只留类别与主机名。
            return fail(_public_block_reason(host, why), "blocked_address",
                        hops=hops, detail=why)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return fail(f"抓取超过 {total_timeout:g} 秒硬总时限，已中止",
                        "timeout", hops=hops,
                        detail="hard_total_timeout_before_request")
        client = None
        total = 0
        try:
            client = factory(hop_timeout=remaining, pins={host: addresses},
                             connect_timeout=connect_timeout)
            with client.stream("GET", current, headers=FIXED_HEADERS) as resp:
                status_code = resp.status_code
                raw_ctype = resp.headers.get("content-type") or ""
                ctype = raw_ctype.split(";")[0].strip().lower()
                if 300 <= status_code < 400:
                    location = resp.headers.get("location") or ""
                    if not location:
                        return fail("重定向响应没有 Location，没地方可跟",
                                    "redirect_no_location", hops=hops)
                    if len(location) > MAX_LOCATION_LENGTH:
                        return fail(
                            f"重定向 Location 超过 {MAX_LOCATION_LENGTH} 字符，拒绝跟随",
                            "redirect_invalid_location", hops=hops)
                    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in location):
                        return fail("重定向 Location 含控制字符，拒绝跟随",
                                    "redirect_invalid_location", hops=hops)
                    redirect = joiner(current, location)
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
                        return fail(f"远端返回 HTTP {status_code}",
                                    f"http_{status_code}", hops=hops)
                    if ctype not in ALLOWED_CONTENT_TYPES:
                        # 服务端控制的 header 不回显：只报类别，原文截断后进审计。
                        return fail(
                            "内容类型不在支持的清单里（只收 html/xhtml/text/json）",
                            "content_type", hops=hops,
                            detail=f"content_type={raw_ctype[:64]!r}")
                    chunks = []
                    iterator = resp.iter_bytes()
                    while True:
                        if time.monotonic() >= deadline:
                            return fail(
                                f"抓取超过 {total_timeout:g} 秒硬总时限，已中止",
                                "timeout", nbytes=total, hops=hops,
                                detail="hard_total_timeout_while_reading")
                        try:
                            chunk = next(iterator)
                        except StopIteration:
                            break
                        total += len(chunk)
                        if total > max_bytes:
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
                if hops >= max_redirects:
                    return fail(f"重定向已超过 {max_redirects} 跳上限，停止跟随",
                                "too_many_redirects", hops=hops)
                prev_scheme = scheme
                current = redirect
                hops += 1
                continue
            article = extract_text(ctype, text,
                                   limit=BODY_CHAR_LIMIT if char_limit is None
                                   else int(char_limit))
            if require_nonempty and (not article.strip()
                                     or article.strip() == EMPTY_BODY_TEXT):
                return fail("页面没有可读正文", "empty_body", nbytes=total,
                            hops=hops)
            audit_fetch(audit_action, user_id, audit_host, audit_path or "/",
                        "ok", total, hops, origin_host=origin_host,
                        origin_path=origin_path)
            return FetchOutcome(ok=True, text=article, status="ok",
                                host=audit_host, path=audit_path or "/",
                                bytes=total, hops=hops, final_url=current,
                                final_status=status_code, content_type=ctype)
        except httpx.TransportError as e:
            # 真实网络故障（超时/连不上/协议错）：本模块只如实记账；是否全局
            # 冷却由调用方决定（`web_read` 不能被一条坏结果冷却 10 分钟）。
            class_name = type(e).__name__
            audit_fetch(audit_action, user_id, audit_host, audit_path,
                        f"network_error:{class_name}", 0, hops,
                        origin_host=origin_host, origin_path=origin_path,
                        detail=f"{class_name}: {str(e)[:200]}")
            return FetchOutcome(ok=False, text="网络连接没有完成",
                                status=f"network_error:{class_name}",
                                host=audit_host, path=audit_path, hops=hops,
                                network_error=class_name)
        except Exception as e:                   # noqa: BLE001 - 审查 B2 的兜底
            # 非 TransportError 的逃逸（InvalidURL/DecodingError/urljoin 的
            # ValueError/解析器异常…）：必须落审计、文案可控。原始异常串可能
            # 带上恶意 URL/header，只把类名给调用方，全文进审计。
            class_name = type(e).__name__
            return fail(f"抓取过程遇到内部错误（{class_name}），已中止",
                        "failed", hops=hops,
                        detail=f"{class_name}: {str(e)[:200]}")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:                # noqa: BLE001 - 关不上不改变结果
                    pass
    return fail(f"重定向已超过 {max_redirects} 跳上限，停止跟随",
                "too_many_redirects", hops=hops)
