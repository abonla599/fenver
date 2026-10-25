"""http_fetch——只抓管理员白名单站点的 https 页面正文（v0.37.0；v0.37.4 起是薄壳）。

出站判据（URL 契约、DNS 解析、逐跳重定向、地址闸、1 MiB/墙钟预算、正文提取、
审计）全部住在 `app.tools.fetch_core`，本模块只保留**本工具自己的**三件事：

* 管理员白名单策略 `config_store.fetch_allowed_hosts()`；
* 网络故障后的 600 秒进程内冷却（`web_read` 不能被一条坏结果全局冷却）；
* v0.37.0 的既有错误文案与 re-export（契约测试与隔离锁继续钉这里）。

刻意**不 import** 的两样（隔离锁 `tests/test_v0370_isolation_locks.py` 钉着）：
* `app.sandbox`——沙箱容器 `network_disabled=True`，http_fetch 是它的对照面；
* `app.core.providers`——两个出网面各自独立演进，共享的只有 net_guard 判据。

`_resolve_addresses` 与 `make_client` 在这里保留同名入口，`fetch()` 调用
`fetch_core.fetch` 时从**本模块属性**取它们：v0.37.0 的契约测试 monkeypatch
的就是这两个名字（以及 `socket`/`DNS_TIMEOUT`/`HARD_TOTAL_TIMEOUT`/`urljoin`），
薄壳必须让这些历史注入点继续生效。
"""
import socket
import threading
import time
from urllib.parse import urljoin, urlparse

from app.core import config_store
from app.tools import fetch_core
from app.tools.extract import EXTERNAL_CONTENT_NOTE
from app.tools.fetch_core import (  # noqa: F401 - re-export 给既有测试与调用方
    ALLOWED_CONTENT_TYPES,
    CONNECT_TIMEOUT,
    DNS_TIMEOUT,
    FIXED_HEADERS,
    HARD_TOTAL_TIMEOUT,
    MAX_BODY_BYTES,
    MAX_REDIRECTS,
    PER_ADDRESS_CONNECT_CAP,
    READ_TIMEOUT,
    _PinnedBackend,
    _resolve_addresses as _core_resolve_addresses,
    audit_fetch,
    client_kwargs,
    contract_reject_reason,
    make_client,
)
from app.tools.response import ToolResponse

COOLDOWN_SECONDS = 600.0              # 一次真实网络失败后工具下线 10 分钟
_DEFAULT_HINT = "不要换个写法再试一次；把理由如实告诉用户。"

_lock = threading.Lock()
_cooldown_until = 0.0                 # time.monotonic() 口径；0 = 从未失败


def _resolve_addresses(host: str, timeout=None) -> list:
    """薄壳的 DNS 入口：把本模块的 `socket`/`DNS_TIMEOUT` 交给 fetch_core。

    v0.37.0 的测试 monkeypatch 的是 `http_fetch.socket` 与
    `http_fetch.DNS_TIMEOUT`；判据实现仍只有 fetch_core 一份，这里只做
    运行时取值与转交（在调用时读模块属性，patch 才生效）。
    """
    return _core_resolve_addresses(
        host,
        timeout=DNS_TIMEOUT if timeout is None else timeout,
        socket_module=socket,
    )


def _enter_cooldown() -> None:
    global _cooldown_until
    with _lock:
        _cooldown_until = time.monotonic() + COOLDOWN_SECONDS


def _fetch_failed(reason: str, status: str, *, user_id: str, host: str,
                  path: str, hint: str = "", detail: str = "") -> ToolResponse:
    """薄壳自己的前置拒绝（白名单读取失败/空名单/冷却）落账并返回失败。"""
    audit_fetch("http_fetch", user_id, host, path, status, 0, 0,
                origin_host=host, origin_path=path, detail=detail)
    return ToolResponse(False, error=f"http_fetch 拒绝：{reason}",
                        hint=hint or _DEFAULT_HINT)


def fetch(url: str, *, user_id: str = ""):
    """抓一页白名单内容，返回 ToolResponse（正文带外部内容标注）。

    这里只做：读白名单 → 冷却检查 → 调 `fetch_core.fetch` → 把 outcome 映射回
    v0.37.0 的文案与冷却语义。逐跳闸、预算与审计都在 fetch_core 里。
    """
    raw_url = str(url or "").strip()
    # 审计锚点：前置拒绝时也要能看见"谁想抓哪里"；query/fragment/userinfo 由
    # urlparse 天然剥掉，坏 URL 解析不动就留空。
    try:
        _p = urlparse(raw_url)
        origin_host = _p.hostname or ""
        origin_path = _p.path or ""
    except ValueError:
        origin_host, origin_path = "", ""

    try:
        allow = set(config_store.fetch_allowed_hosts())
    except Exception as e:                       # noqa: BLE001 - 不许把失败漏给执行器
        return _fetch_failed(
            "读取 fetch_allowed_hosts 白名单配置失败，本工具暂不可用",
            "failed", user_id=user_id, host=origin_host, path=origin_path,
            detail=f"{type(e).__name__}: {str(e)[:200]}")
    if not allow:
        return _fetch_failed(
            "管理员还没有配置 fetch_allowed_hosts 白名单，本工具不可用",
            "no_allowlist", user_id=user_id, host=origin_host, path=origin_path)

    with _lock:
        cooling = _cooldown_until - time.monotonic()
    if cooling > 0:
        return _fetch_failed(
            f"上一次抓取遇到网络故障，本工具进入 {int(COOLDOWN_SECONDS)} 秒冷却，"
            f"还需等待约 {int(cooling) + 1} 秒", "cooldown",
            user_id=user_id, host=origin_host, path=origin_path,
            hint="不要重试；稍后再来或如实告诉用户现在抓不了。")

    def policy(host: str) -> str:
        if host in allow:
            return ""
        return f"主机 {host} 不在 fetch_allowed_hosts 白名单里"

    # 从本模块属性取（不是 fetch_core 的全局）：既有 monkeypatch 继续生效。
    outcome = fetch_core.fetch(
        raw_url,
        policy=policy,
        user_id=user_id,
        audit_action="http_fetch",
        total_timeout=HARD_TOTAL_TIMEOUT,
        connect_timeout=CONNECT_TIMEOUT,
        max_redirects=MAX_REDIRECTS,
        client_factory=make_client,
        resolver=_resolve_addresses,
        urljoin_func=urljoin,
    )

    if outcome.ok:
        final = urlparse(outcome.final_url)
        final_host = final.hostname or outcome.host
        final_path = final.path or "/"
        scheme = final.scheme or "https"
        return ToolResponse(True, data=(
            EXTERNAL_CONTENT_NOTE + f"来源：{scheme}://{final_host}{final_path}"
            f"（HTTP {outcome.final_status}，{outcome.bytes} 字节，正文如下）\n\n"
            f"{outcome.text}"))

    if outcome.network_error:
        # 真实网络故障才进冷却；web_read 不复用这个全局状态。
        _enter_cooldown()
        return ToolResponse(False, error=(
            f"http_fetch 失败：网络连接没有完成（{outcome.network_error}）"),
            hint="站点没接通，工具已进入冷却；不要重试，稍后再来"
                 "或如实告诉用户现在抓不了。")

    hint = ""
    if outcome.status == "off_allowlist":
        hint = "只有管理员配置的站点抓得到；别换写法绕，告诉用户站点没被允许。"
    elif outcome.status.startswith("http_"):
        hint = "站点自己不给，别再敲它。"
    if outcome.status == "failed":
        # 非 TransportError 的逃逸（DecodingError/ValueError…）在 v0.37.0 里
        # 也进冷却：那是实现面出了问题，不该让注入驱动的路径继续敲。
        _enter_cooldown()
    return ToolResponse(False, error=f"http_fetch 拒绝：{outcome.text}",
                        hint=hint or _DEFAULT_HINT)


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
