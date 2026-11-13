"""会话级出网闸：测试进程一律不许真出网（含 DNS 查询本身）。

为什么需要它（不是客套的警告）：独立复审在出网闸下跑全量时，闸自证会拦之后**拦下了
套件既有的 5 次出站尝试**（搜索降级路径打 cn.bing.com、假解析返回 example.com 的公网
IP 后真连）。那些尝试先于任何一批改动存在、对应用例靠优雅降级照绿——但「跑测试不出网」
这句话因此不成立：CI 与本机的绿都可能在偷偷依赖外部可达性。逐处补桩挡不住这一类，因为
触发它们的是全量的顺序/时序（后台探测线程、跨用例的状态），不是某个文件单独跑就会犯。
所以这道闸装在会话级，覆盖整套，且只拦非环回——环回真服务器（uvicorn / ThreadingHTTPServer）
与本地解析照常放行，bind/listen/accept 不拦。

形状与复审那把已验证的闸同源：拦 getaddrinfo / gethostbyname* / gethostbyaddr / getnameinfo
的非环回目标（DNS 查询本身就是出站，先于连接拦），拦 socket.connect / connect_ex / sendto /
sendmsg（若有）与 create_connection 的非环回目标；放行环回（127.0.0.0/8、::1、localhost）、
被动 hint（None/""）、AF_UNIX。每次拦截发生在任何 DNS 包 / SYN 之前 ⇒ 装闸本身零出网。

自证在 tests/test_egress_gate.py：先证明它会拦（非环回 DNS/连接一律 EgressBlocked），
再证明它不误伤正常判据（环回真 TCP 往返、localhost 解析照常）。审计日志可选：设了
FENVER_EGRESS_GATE_LOG 就追加到那个文件（运行期路径，不写死本机地址），否则只留进程内清单。
"""
import ipaddress
import os
import socket

# 进程级标记：任何用例都能据此证明闸真的装上了（自证先查它，fail-closed）。
_MARKER = "_FENVER_EGRESS_GATE"

# 进程内拦截清单（审计用）：每条 (kind, detail)。设了日志环境变量时同时追加到文件。
BLOCKS = []


class EgressBlocked(RuntimeError):
    """在任何可能把字节送上线路的 syscall 之前抛出，替代那次调用。"""


def _log(kind, detail):
    BLOCKS.append((kind, detail))
    path = os.environ.get("FENVER_EGRESS_GATE_LOG", "")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("%s %s\n" % (kind, detail))
        except OSError:
            pass


def _is_local_host(host):
    if host is None or host == "":
        return True
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    if host.lower() in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_address(address):
    # AF_UNIX：str/bytes 路径是本机，放行。
    if isinstance(address, (str, bytes)):
        return
    if isinstance(address, tuple) and address:
        if not _is_local_host(address[0]):
            _log("BLOCKED", "connect/send to non-loopback %r" % (address,))
            raise EgressBlocked("connect/send to non-loopback %r" % (address,))
        return
    _log("BLOCKED", "unrecognized address shape %r" % (address,))
    raise EgressBlocked("unrecognized address shape %r" % (address,))


def install(mp):
    """把闸装到一个 pytest MonkeyPatch 实例上（会话级夹具用它，收尾 mp.undo() 还原）。

    只 patch 名字解析与主动连接/发送；bind/listen/accept 不动（本地真服务器合法）。
    sendmsg 是 Unix-only：Windows 没有就不 patch（hasattr 判，避免装载即炸）。
    """
    orig_getaddrinfo = socket.getaddrinfo
    orig_gethostbyname = socket.gethostbyname
    orig_gethostbyname_ex = socket.gethostbyname_ex
    orig_gethostbyaddr = socket.gethostbyaddr
    orig_getnameinfo = socket.getnameinfo
    orig_create_connection = socket.create_connection
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_sendto = socket.socket.sendto
    orig_sendmsg = getattr(socket.socket, "sendmsg", None)

    def guarded_getaddrinfo(host, port, *a, **k):
        if not _is_local_host(host):
            _log("BLOCKED", "getaddrinfo(%r, %r)" % (host, port))
            raise EgressBlocked("DNS egress blocked: getaddrinfo(%r)" % (host,))
        return orig_getaddrinfo(host, port, *a, **k)

    def guarded_gethostbyname(host):
        if not _is_local_host(host):
            _log("BLOCKED", "gethostbyname(%r)" % (host,))
            raise EgressBlocked("DNS egress blocked: gethostbyname(%r)" % (host,))
        return orig_gethostbyname(host)

    def guarded_gethostbyname_ex(host):
        if not _is_local_host(host):
            _log("BLOCKED", "gethostbyname_ex(%r)" % (host,))
            raise EgressBlocked("DNS egress blocked: gethostbyname_ex(%r)" % (host,))
        return orig_gethostbyname_ex(host)

    def guarded_gethostbyaddr(addr):
        if not _is_local_host(addr):
            _log("BLOCKED", "gethostbyaddr(%r)" % (addr,))
            raise EgressBlocked("reverse-DNS egress blocked: gethostbyaddr(%r)" % (addr,))
        return orig_gethostbyaddr(addr)

    def guarded_getnameinfo(sockaddr, *a, **k):
        if isinstance(sockaddr, tuple) and sockaddr and not _is_local_host(sockaddr[0]):
            _log("BLOCKED", "getnameinfo(%r)" % (sockaddr,))
            raise EgressBlocked("reverse-DNS egress blocked: getnameinfo(%r)" % (sockaddr,))
        return orig_getnameinfo(sockaddr, *a, **k)

    def guarded_create_connection(address, *a, **k):
        _check_address(address)
        return orig_create_connection(address, *a, **k)

    def guarded_connect(self, address):
        _check_address(address)
        return orig_connect(self, address)

    def guarded_connect_ex(self, address):
        _check_address(address)
        return orig_connect_ex(self, address)

    def guarded_sendto(self, data, *a, **k):
        addr = k.get("address")
        if addr is None:
            if len(a) >= 2:
                addr = a[1]
            elif len(a) == 1 and not isinstance(a[0], int):
                addr = a[0]
        if addr is not None:
            _check_address(addr)
        return orig_sendto(self, data, *a, **k)

    def guarded_sendmsg(self, buffers, *a, **k):
        addr = k.get("address") or (a[0] if a else None)
        if addr is not None:
            _check_address(addr)
        return orig_sendmsg(self, buffers, *a, **k)

    mp.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    mp.setattr(socket, "gethostbyname", guarded_gethostbyname)
    mp.setattr(socket, "gethostbyname_ex", guarded_gethostbyname_ex)
    mp.setattr(socket, "gethostbyaddr", guarded_gethostbyaddr)
    mp.setattr(socket, "getnameinfo", guarded_getnameinfo)
    mp.setattr(socket, "create_connection", guarded_create_connection)
    mp.setattr(socket.socket, "connect", guarded_connect)
    mp.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    mp.setattr(socket.socket, "sendto", guarded_sendto)
    if orig_sendmsg is not None:
        mp.setattr(socket.socket, "sendmsg", guarded_sendmsg)
    mp.setattr(socket, _MARKER, True, raising=False)