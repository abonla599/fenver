"""服务端出网对象的地址判据总口径：net_guard。

v0.37.0 把原本写在 providers 里的 CIDR 清单与地址判据（v0.33 初版、v0.34.2 补
"域名必须先解析再判形状"）整体搬进本模块，供多条出站路径共用：provider 的
base_url（本轮）与后续的 http_fetch。刻意**不收**的东西：

* 端口限制、https、userinfo、尾点/IDN、重定向——那些是各调用方自己的 URL 契约，
  且 provider base_url 允许自定义端口，混进来会把配置校验管成访问策略；
* `main.py` 的代理信任判据（ip_network）不并入——那是"这个请求是不是真从
  反向代理来的"，与"服务端该不该替它出网"是两个方向的问题。

拒的理由从 v0.33 起没变过（危害详情见 providers 的 base_url SSRF 一节，这里只摘要）：
  1. SSRF——出站路径能被指着去打内网/云元数据，管理员侧还带回显；
  2. 凭据外泄——全局 provider 的 base_url 被改走后，此后每一个走默认上游的
     用户每轮聊天都把管理员的 api_key 作为 Authorization: Bearer 送给攻击者。

为什么是白名单式的"这段网段不许"而不是 `ip.is_global`：后者会连同
100.64.0.0/10、192.0.2.0/24 这些各版本 Python 语义还在漂的保留段一起拒，
部署者被拒时看不出自己撞了哪一条。这里的判据是显式、可念出来的 CIDR 清单，
外加几条跨版本语义稳定的属性（is_loopback / is_unspecified / is_link_local /
is_multicast）。
"""
import ipaddress

# 网段清单。v0.33 初版只有前八段；v0.37.0 第一批补进"写成 IP 字面量就能绕过"
# 的其余特殊用途段——域名那一支有属性补丁兜底，而 IP 字面量走的是快路，此前
# http://[fe80::10]/、http://224.0.0.1/、http://[ff02::1]/ 就是这么一路放行的。
# fe80::/10、ff00::/12、224.0.0.0/4 与下面的属性判断是**双保险**关系：清单让
# 字面量快路自成完备，属性判断先跑、保住"链路本地/组播"这两类既有文案。
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "127.0.0.0/8",     # 回环：宿主机自己，以及本机所有 127.x 别名
    "::1/128",         # IPv6 回环。**不在**下面 fc00::/7 里——那是唯一本地地址，
                       # 与回环是两段，写漏这一条就是 `http://[::1]/` 一路放行
    "10.0.0.0/8",      # RFC1918
    "172.16.0.0/12",   # RFC1918
    "192.168.0.0/16",  # RFC1918
    "169.254.0.0/16",  # 链路本地：云厂商的元数据地址就在这里（169.254.169.254）
    "0.0.0.0/8",       # 全零：0.0.0.0 与任何 0.x
    "fc00::/7",        # IPv6 唯一本地地址
    "100.64.0.0/10",   # CGNAT（RFC6598）：运营商级 NAT 共享地址池，不是公网端点
    "192.0.2.0/24",    # 文档段 TEST-NET-1（RFC5737），永不路由
    "198.51.100.0/24", # 文档段 TEST-NET-2
    "203.0.113.0/24",  # 文档段 TEST-NET-3
    "198.18.0.0/15",   # 设备基准测试段（RFC2544），公网不路由
    "224.0.0.0/4",     # IPv4 组播（含 IGMP/所有主机 224.0.0.x 这些保留组）
    "ff00::/12",       # IPv6 组播（ff02::1 之类"链路本地全部节点"就在里面）
    "fe80::/10",       # IPv6 链路本地；fc00::/7 管的是唯一本地，不是这一段
    "2001:db8::/32",   # IPv6 文档段（RFC3849）
    "240.0.0.0/4",     # IPv4 保留（旧 class E）：受限广播 255.255.255.255 也在这里
    # ---- v0.37.0 第二批：独立审查点名的五个特殊用途段 ----
    # 共同性质：合法用途要么已废弃、要么本就不是"服务端该去连接的公网端点"，
    # 而它们都能以合法字面量写成，前一张清单（含 is_global 一类属性）都拦不住。
    "2002::/16",       # 6to4（RFC3056，已废弃）：中段 32 位内嵌任意 IPv4——
                       # 2002:7f00:0101:: 就是 127.0.0.1 的另一种写法，属性判断
                       # （is_loopback 等）对它全部失明
    "2001::/32",       # Teredo（RFC4380）：尾 32 位是混淆后的端点地址；注意与
                       # 2001:db8::/32（文档段）不重叠，2001:4860::/32（Google）等
                       # 真实公网前缀都在 /32 边界之外，不误伤
    "192.0.0.0/24",    # IETF 协议分配段（RFC6890）：ias-address/ias-backdoor 等
                       # 保留协议地址，不是普通公网端点；与 192.0.2.0/24（文档段）
                       # 相邻但不同段
    "192.88.99.0/24",  # 6to4 中继 anycast（RFC3068，已废弃）：历史特殊用途段，
                       # 现网任何合法服务都不该指到这里
    "::/96",           # 已废弃的 IPv4-compatible IPv6（RFC4291）：::a.b.c.d 是
                       # IPv4 的另一种写法。`::` 与 `::1` 先被 is_unspecified/
                       # is_loopback 属性判走（文案保持"本机"不变），`::ffff:x` 系
                       # 先折回 v4 再判——这一条只兜其余 compat 写法，如 ::127.0.0.1
    # ---- v0.37.0 第二批修复：审查点名的四个特殊用途段 ----
    # 与上面五段同一性质：能写成合法字面量、属性判断全失明、且不是"服务端该去
    # 连接的公网端点"。
    "100::/64",        # discard-only（RFC6666）：只为丢弃流量而分配，没有服务
    "2001:2::/48",     # 基准测试段（RFC5180）：给网络设备压测用，不是业务端点
    "2001:10::/28",    # ORCHID（RFC4843，已废弃）：曾用于叠加标识，现网不路由
    "3fff::/20",       # IPv6 文档段（RFC9637）：与 2001:db8::/32 同类
))

# 可内嵌 IPv4 的 IPv6 段：**不整段拒**，而是把低 32 位提出来按 v4 清单判。
# `::ffff:0:0:0/96` 是 IPv4-translated（SIIT/RFC2765）写法，Python 的
# `ipv4_mapped` 只折叠 `::ffff:a.b.c.d`（/96 的另一种布局），不认它；
# `64:ff9b::/96` 是 NAT64 知名前缀（RFC6052）。两者都能在 SIIT/NAT64 网络里
# 指向 169.254.169.254 或私网；但内嵌公网地址（如 64:ff9b::8.8.8.8）本身没有
# 危害，所以按 v4 清单判，而不是把整段打成内网。
_V4_EMBEDDED_NETWORKS = (
    ipaddress.ip_network("::ffff:0:0:0/96"),
    ipaddress.ip_network("64:ff9b::/96"),
)

# 回环/localhost 理由句的既有尾巴（v0.33 原文）。providers 走默认值一字不动；
# 不吃这枚开关的调用方（http_fetch 永远 allow_loopback=False）传 "" 去掉它，
# 免得向用户指一条对本路径无效的开关。
_LOOPBACK_HINT = ("；确实要把模型自建在本机的话，"
                  "把 providers.ALLOW_LOCALHOST_BASE_URL 打开")


def reject_reason(host: str, *, resolve, allow_loopback: bool, where: str,
                  loopback_hint: str | None = None) -> str:
    """“这个主机名/地址能不能让服务端替它出网”。返回 "" = 放行，否则是拒的理由。

    三个关键字参数都必填、不给默认值，各有各的存在理由：

    * `resolve`——主机名 → 地址字面量列表的**注入点**（真实现见 providers.
      `_resolve_addresses`，测试用 monkeypatch 换固定答案）。调用方必须在
      **调用时**从模块属性取它再传进来：写成默认参数会在 def 时冻结引用，
      conftest 的 autouse DNS 桩与逐用例的 monkeypatch 全部失效。IP 字面量与
      localhost 名字这两条快路不调用它。
    * `allow_loopback`——只管"本机"这一类（回环 127.0.0.0/8 与 ::1、全零
      0.0.0.0 与 ::、localhost 及其子域）。私有/链路本地/组播/保留段在任何
      开关状态下都拒——这个开关存在的理由是"允许自建模型"，不是"允许扫内网"。
    * `where`——理由句的主语。provider 传 "base_url"，理由就逐字保持既有的
      "`base_url` 指向…"（字面量/名字）与"`base_url` 的主机名 X 解析到…"
      （域名）两种句式，管理员看到的文案一位都不动。
    * `loopback_hint`——回环/localhost 理由句的尾巴（指向 providers 那枚开关的话）。
      providers 不传（默认）保持逐字原文；不吃这枚开关的路径传 "" 去掉误导指向。

    判定顺序（对单个地址）：回环/全零（受开关）→ zone-id fail-closed →
    IPv6 链路本地 → 组播 → CIDR 清单。属性判断排在清单前是为了保住既有文案；
    清单里同时登记了
    fe80::/10、ff00::/12、224.0.0.0/4，删掉属性判断也不会重新放行。
    IPv4 映射的 IPv6 地址（::ffff:a.b.c.d）先折回内嵌的 v4 再判——否则
    `http://[::ffff:10.0.0.1]/` 会因为版本不同绕开整张 v4 清单。
    """
    lowered = host.strip().lower().strip("[]")

    try:
        addr = ipaddress.ip_address(lowered)
    except ValueError:
        addr = None

    if addr is not None:
        return _address_reject_reason(addr, host=host, resolved_via=None,
                                      allow_loopback=allow_loopback, where=where,
                                      loopback_hint=loopback_hint)

    # 主机名而不是 IP：只对"人人都知道指向本机"的那个名字（及其子域，RFC 6761
    # 的 .localhost 特殊用途域恒指回环）留口子，与回环/全零受同一个开关控制，
    # 且不送去解析——开关打开就能存的自建模型，不该变成"还要看本机 hosts"。
    if lowered == "localhost" or lowered.endswith(".localhost"):
        if allow_loopback:
            return ""
        return (f"{where} 指向 localhost，服务端不做本机请求"
                + (_LOOPBACK_HINT if loopback_hint is None else loopback_hint))

    # ---------- 其余域名：解析之后再逐个判形状 ----------
    # 解析失败一律拒（fail closed）：一个连服务端都解析不出来的名字，放行它
    # 等于让下一次连接去猜它指向哪儿。多个地址里只要有一个踩线就拒——闸门要的
    # 是"这一个名字可能被指到哪儿"的全称判据，不是"第一个答案看起来对不对"。
    try:
        addresses = resolve(lowered)
    except Exception as e:
        return (f"{where} 的主机名 {host} 解析失败（{type(e).__name__}: {str(e)[:80]}），"
                "服务端不放行解析不出来的地址")
    if not addresses:
        return f"{where} 的主机名 {host} 解析结果为空，服务端不放行"

    for literal in addresses:
        try:
            addr = ipaddress.ip_address(literal)
        except ValueError:
            return (f"{where} 的主机名 {host} 解析出无法识别的地址 {literal}，"
                    "服务端不放行")
        reason = _address_reject_reason(addr, host=host, resolved_via=literal,
                                        allow_loopback=allow_loopback, where=where,
                                        loopback_hint=loopback_hint)
        if reason:
            return reason
    return ""


def _address_reject_reason(addr, *, host, resolved_via, allow_loopback, where,
                           loopback_hint=None):
    """对**单个地址字面量**给理由。resolved_via=None 表示 host 本身就是字面量。

    两套句式（"指向" vs "的主机名 … 解析到"）与 providers 里 v0.33/v0.34.2
    的原文逐字对齐；开关打开时回环/全零返回 ""（调用方对域名分支据此继续查
    下一个答案，等价于旧代码的 continue）。
    """
    # scope_id 在映射折叠之前捕获：`::ffff:8.8.8.8%eth0` 折回 v4 后 scope 会丢，
    # 但它仍是"链路寻址"的写法，一样不许过关。
    scope_id = getattr(addr, "scope_id", None)
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is None and addr.version == 6:
        # IPv4-translated 与 NAT64：低 32 位就是内嵌的 v4，按 v4 清单判。
        for network in _V4_EMBEDDED_NETWORKS:
            if addr in network:
                mapped = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
                break
    if mapped is not None:
        addr = mapped

    if resolved_via is not None:
        prefix = f"{where} 的主机名 {host} 解析到"
        display = resolved_via
    else:
        prefix = f"{where} 指向"
        display = host

    if addr.is_loopback or addr.is_unspecified:
        if allow_loopback:
            return ""
        return (f"{prefix}本机地址 {display}，服务端不做本机请求"
                + (_LOOPBACK_HINT if loopback_hint is None else loopback_hint))
    # zone-id（`fe80::1%eth0` 这类 `%接口`）在**任何**类别之前 fail closed：
    # 作用域标识只对"本机某条链路"有意义，服务端出网没有链路作用域这个概念，
    # 判不出指向就拒——理由点名 zone-id，读的人不必懂 RFC 也知道该改什么。
    # 排在回环之后是刻意的：`::1%lo` 的既有语义（本机类、受开关管）不许误伤。
    if scope_id:
        return (f"{prefix}带 zone-id（作用域标识）的地址 {display}，"
                "zone-id 只在本机链路上有意义，服务端不做链路作用域寻址")
    if addr.version == 6 and addr.is_link_local:
        return f"{prefix}链路本地地址 {display}，服务端不做内网请求"
    if addr.is_multicast:
        return f"{prefix}组播地址 {display}，服务端不向组播目标发请求"
    for network in PRIVATE_NETWORKS:
        if addr in network:
            return f"{prefix}内网地址 {display}（{network}），服务端不做内网请求"
    return ""
