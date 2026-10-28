"""把已校验的地址钉进 httpx 连接层：两条出网路径共用同一份实现。

`app.tools.fetch_core`（逐跳抓取）与 `app.core.providers.build_client`（模型
上游）在连接前都会先解析主机名、用 `net_guard` 判一遍地址。判完到连上之间如果
让 httpx/httpcore 再解析一次，一个 TTL=0 的域名就能在两次解析之间换答案——这就是
DNS rebinding / TOCTOU。这里的做法是：把**判过的那一份地址列表**交给
`PinnedBackend`，连接层只连这些字面量，主机名只用于 Host 头与 TLS SNI。

`install_pins` 触碰的是 httpx 的私有结构（`_transport._pool._network_backend`）。
升级后形状一旦变了必须 fail-closed 抛错，绝不静默退回"按主机名重新解析"的连接
路径——那正是这个模块要关掉的窗口。
"""
import time

import httpcore

PER_ADDRESS_CONNECT_CAP = 1.0         # 多地址时单个地址的尝试上限：别让前两个黑洞吃光预算


class PinnedBackend(httpcore.SyncBackend):
    """只连 net_guard 校验过的地址字面量；主机名只用于 Host/SNI。

    一个主机名可能解析出多个公网地址；按传入顺序逐个尝试，全部失败才把
    最后一个异常抛出去。总尝试时间不超过单跳 timeout。TLS 握手发生在
    httpcore 拿到 stream 之后，不在这层回退（httpcore retries=0）。
    任何情况下都不把原主机名交回系统解析器——那正是 rebinding 窗口的入口。
    """

    def __init__(self, pins):
        self._pins = {str(host).lower(): tuple(str(ip) for ip in ips)
                      for host, ips in pins.items()}

    def connect_tcp(self, host, port, timeout=None, local_address=None,
                    socket_options=None):
        targets = self._pins.get(str(host).lower(), ())
        if not targets:
            raise RuntimeError(f"没有为 {host} 钉住已校验地址，拒绝连接")
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        last_exc = None
        for target in targets:
            if deadline is None:
                attempt_timeout = None
            else:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                attempt_timeout = (min(left, PER_ADDRESS_CONNECT_CAP)
                                   if len(targets) > 1 else left)
            try:
                return super().connect_tcp(
                    target, port, timeout=attempt_timeout,
                    local_address=local_address, socket_options=socket_options)
            except Exception as exc:             # noqa: BLE001 - 换下一个已校验地址
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise TimeoutError(f"连接 {host} 的所有已校验地址超过单跳超时")


def install_pins(client, pins) -> None:
    """把 `PinnedBackend(pins)` 挂到该 client 默认连接池的传输层上。

    `pins` 是 `{hostname: [已由 net_guard 校验过的地址]}`。空 dict 是"没有已
    校验地址"，不是"随便连"——fail-closed 抛 `ValueError`；httpx 传输层不再
    长成 `_transport._pool._network_backend` 时抛 `RuntimeError`。

    失败时**不**关 `client`：所有权在调用方，由它在自己的 except 里 close。
    """
    if not pins:
        raise ValueError("pins 不能为空：没有已校验地址就不该建客户端")
    transport = getattr(client, "_transport", None)
    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        raise RuntimeError("httpx 传输层结构变化，无法把已校验地址钉进连接")
    pool._network_backend = PinnedBackend(pins)
