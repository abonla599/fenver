"""取客户端 IP 的唯一口径（auth_router、authz 与安全日志共用这一份）。

只信 Cloudflare 的 `cf-connecting-ip`，且**只在对端是回环时才信**：现网后端只绑
127.0.0.1，公网流量必经 cloudflared 从回环转进来；对端不是回环说明这条请求没走
隧道，那个头就是客户端自己写的。全仓不读 `X-Forwarded-For`——它是可被追加的链，
取首项等于取攻击者写的第一句假话。

`redact_ip` 是安全日志的脱敏入口：原始地址只用来算 /24、/48 前缀与 sha256 指纹，
本身永远不落盘、也不出现在任何 API 响应里。
"""
import hashlib
import ipaddress

LOOPBACK = ("127.0.0.1", "::1")


def client_ip(request) -> str:
    """真实来源地址：回环对端才认 CF 头，否则用 socket 对端；都没有时 "unknown"。"""
    peer = request.client.host if request.client else ""
    real = request.headers.get("cf-connecting-ip", "").strip()
    if real and peer in LOOPBACK:
        return real
    return peer or "unknown"


def parse_ip(raw):
    """把来源字符串解成 ipaddress 对象；解不出返回 None。

    允许的噪声形状：首尾空白、IPv6 方括号、`host:port`。端口只在"恰有一个冒号
    且右侧全是数字"时才剥——IPv6 本身就有多个冒号，不能按冒号盲切。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("[") and "]" in text:
        text = text[1:text.index("]")]
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
        if port.isdigit():
            text = host
    # zone id（fe80::1%eth0）不属于地址本身，且会让前缀运算失败
    text = text.split("%", 1)[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def redact_ip(raw) -> tuple:
    """原始地址 → (网络前缀, sha256 前 8 位)。解析失败给 ("unknown", "")。

    前缀只到 /24（IPv4）与 /48（IPv6）：足够做"同一来源"的聚合，又不指向某台主机。
    指纹取规范化后的地址串，于是 `1.2.3.4` 与 `1.2.3.4:5678` 是同一个来源。
    """
    parsed = parse_ip(raw)
    if parsed is None:
        return "unknown", ""
    bits = 24 if parsed.version == 4 else 48
    prefix = ipaddress.ip_network(f"{parsed}/{bits}", strict=False)
    fingerprint = hashlib.sha256(str(parsed).encode("utf-8")).hexdigest()[:8]
    return str(prefix), fingerprint
