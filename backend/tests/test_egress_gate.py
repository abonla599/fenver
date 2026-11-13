"""会话级出网闸的自证：先证明它会拦，再证明它不误伤正常判据。

纪律（本仓「负控先于绿灯」）：一条判据的绿灯只有在它**有能力报红**时才可信。所以这里
不是「装闸后跑一遍看绿」，而是逐条演给闸看——非环回的 DNS/连接必须当场 EgressBlocked
（拦截发生在任何 DNS 包 / SYN 之前 ⇒ 自证本身零出网），环回的真 TCP 往返与 localhost
解析必须照常成功（证明闸不是「看谁都拦」的空转）。

fail-closed：每条会触发出站尝试的用例**先断言闸已装上**（_MARKER），再发起那次调用。
闸若没装，第一条断言就红，根本走不到那次调用——绝不给「闸不在时真出网」留窗口。
目标只用任务真实主机（api.github.com）与文档占位（example.com / 1.1.1.1 / 8.8.8.8），
它们只作为**被拦的输入**出现，从不被真正联系。
"""
import socket
import threading

import pytest

from tests._egress_gate import BLOCKS, EgressBlocked, _MARKER


def _gate_active():
    assert getattr(socket, _MARKER, False), \
        "出网闸没装上——先装闸再跑这条（fail-closed：闸不在时不许发起任何出站尝试）"


def test_the_egress_gate_is_active():
    """闸真的装在这个进程里（标记在）。拆掉会话级夹具，本条当场红。"""
    _gate_active()


def test_non_loopback_dns_is_blocked():
    """DNS 查询本身就是出站：非环回名字解析必须先于任何 DNS 包被拦。"""
    _gate_active()
    with pytest.raises(EgressBlocked):
        socket.getaddrinfo("api.github.com", 443)


def test_non_loopback_gethostbyname_is_blocked():
    _gate_active()
    with pytest.raises(EgressBlocked):
        socket.gethostbyname("example.com")


def test_non_loopback_connect_is_blocked():
    """主动连接非环回目标必须先于任何 SYN 被拦。"""
    _gate_active()
    with pytest.raises(EgressBlocked):
        socket.create_connection(("api.github.com", 443), timeout=5)


def test_non_loopback_udp_sendto_is_blocked():
    _gate_active()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(EgressBlocked):
            s.sendto(b"x", ("8.8.8.8", 53))
    finally:
        s.close()


def test_loopback_tcp_roundtrip_is_allowed():
    """阳性对照：环回真 TCP 往返必须成功——证明闸不误伤本地真服务器用例。"""
    _gate_active()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        conn.sendall(b"ok")
        conn.close()
        srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    data = c.recv(8)
    c.close()
    t.join(timeout=5)
    assert data == b"ok", f"环回往返被闸误伤了：{data!r}"


def test_localhost_resolution_is_allowed():
    """阳性对照：localhost 解析照常——本地名字不该被当成出站。"""
    _gate_active()
    assert socket.getaddrinfo("localhost", 80), "localhost 解析被闸误伤了"


def test_blocks_are_recorded_for_audit():
    """拦截要留痕（审计「闸真的拦了什么」）：上面几条非环回尝试都进了清单。"""
    _gate_active()
    with pytest.raises(EgressBlocked):
        socket.getaddrinfo("api.github.com", 443)
    assert any("api.github.com" in detail for _kind, detail in BLOCKS), \
        f"拦截没留痕：{BLOCKS}"