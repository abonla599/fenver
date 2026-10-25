"""v0.37.4 T1 抽核后的核心判据：`fetch_core.fetch` 是全场唯一的逐跳出站循环。

v0.37.0 的 110 条契约测试钉的是薄壳（https-only/443 + 管理员白名单），不能替代
core 自己的参数面：`web_read` 传的是 http+https / 80+443 / policy 恒空。T5 派单
点名的九条判据在这里逐条钉住：

1. **每跳重验**：重定向的每一跳重新过 contract / policy / DNS / net_guard，
   第 2 跳被拦时一个字节都不发；
2. **https→http 降级**：先于第二跳的解析与连接拒；
3. **Location**：缺失 / CRLF / 超过 2048 字拒；正好 2048 字放行（边界不误伤）；
4. **1 MiB**：解码后正好 1 MiB 放行，+1 中止；gzip 解压后按同一预算计；
5. **墙钟**：慢滴流按 deadline 中止，不是等单次 read 超时；
6. **DNS 超时**：daemon 线程 + 队列，解析器卡住不占工作线程、fail-closed；
7. **pin**：一次解析，同一份地址既判闸又钉进连接层；DNS 换答案不再生效；
8. **审计**：query/fragment/userinfo 不落账，正文与标题不落账；
9. **policy=None**：是"忘了传"不是"全放行"，在任何 DNS/审计之前抛 TypeError。

所有字节来自 `httpx.MockTransport` 或等价的假 response/client，DNS 一律
monkeypatch；本文件不真出网、不写仓库 `data/`。
"""
import gzip
import time
from pathlib import Path

import httpx
import pytest

from app.core import audit
from app.tools import fetch_core

PUBLIC_IP = "93.184.216.34"
PUBLIC_IP_2 = "93.184.216.35"
PRIVATE_IP = "169.254.169.254"
OK_HOST = "ok.example"
ACTION = "fetch_core_test"
MAX = fetch_core.MAX_BODY_BYTES


def _fetch(url: str, **kwargs):
    """按 `web_read` 的参数面调 core；单个用例只覆盖它要改的那一项。"""
    kwargs.setdefault("policy", lambda _host: "")
    kwargs.setdefault("schemes", ("http", "https"))
    kwargs.setdefault("ports", {"http": {None, 80}, "https": {None, 443}})
    kwargs.setdefault("audit_action", ACTION)
    return fetch_core.fetch(url, **kwargs)


def _forbidden_resolver(host):
    raise AssertionError(f"这条路径不该触发 DNS：{host}")


@pytest.fixture
def resolver(monkeypatch):
    """按主机名给答案并记录调用序；未登记的主机名出现即用例写错。"""
    def _install(answers):
        calls = []

        def fake(host):
            calls.append(host)
            if host not in answers:
                raise AssertionError(f"解析桩没有给 {host} 配答案")
            return list(answers[host])

        monkeypatch.setattr(fetch_core, "_resolve_addresses", fake)
        return calls
    return _install


class _Recorder:
    """按请求序号消费一格脚本；格用完重复最后一格（便于"一直是 302"）。"""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.factory_kwargs = []

    def handler(self, request):
        self.requests.append(request)
        idx = min(len(self.requests) - 1, len(self.script) - 1)
        item = self.script[idx]
        if isinstance(item, Exception):
            raise item
        status, headers, body = item
        return httpx.Response(status, headers=headers, content=body)


@pytest.fixture
def transport(monkeypatch):
    """字节来源桩：协议栈是真的 httpx，只有字节是假的；工厂参数全部记账。"""
    def _install(script):
        rec = _Recorder(script)

        def factory(**kwargs):
            rec.factory_kwargs.append(kwargs)
            return httpx.Client(transport=httpx.MockTransport(rec.handler))

        monkeypatch.setattr(fetch_core, "make_client", factory)
        return rec
    return _install


class _RawResponse:
    """httpx 会在 `next_request` 里先拒掉带控制字符的 Location；要测 core 自己
    那一关，只能绕开协议栈，直接喂响应形状。"""

    def __init__(self, status_code, headers=None, body=b""):
        self.status_code = status_code
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self._body = body
        self.charset_encoding = None

    def iter_bytes(self):
        if isinstance(self._body, Exception):
            raise self._body
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _RawClient:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def stream(self, _method, url, headers=None):
        self.requests.append(url)
        idx = min(len(self.requests) - 1, len(self.script) - 1)
        status, headers_, body = self.script[idx]
        return _RawResponse(status, headers_, body)

    def close(self):
        pass


@pytest.fixture
def raw_client(monkeypatch):
    """不做 httpx 协议校验的假 client：专测 core 对 Location 头本身的判据。"""
    def _install(script):
        client = _RawClient(script)
        monkeypatch.setattr(fetch_core, "make_client", lambda **_kwargs: client)
        return client
    return _install


class _ChunkFeed:
    """逐块产出并记账的响应体：证明实现是边读边停，而不是先读完再判。"""

    def __init__(self, chunk: bytes, count: int, delay: float = 0.0):
        self.chunk = chunk
        self.count = count
        self.delay = delay
        self.sent = 0

    def __iter__(self):
        for _ in range(self.count):
            if self.delay:
                time.sleep(self.delay)
            self.sent += len(self.chunk)
            yield self.chunk


def _records():
    return [rec for rec in audit.read(limit=10000) if rec.get("action") == ACTION]


def _last_target():
    return _records()[-1]["target"]


def _audit_text():
    return Path(audit.path()).read_text(encoding="utf-8")


def _ok(body="<html><body><p>正文</p></body></html>", ctype="text/html"):
    return (200, {"content-type": f"{ctype}; charset=utf-8"},
            body.encode("utf-8") if isinstance(body, str) else body)


# ---------- 1. 每跳重验：contract / policy / DNS / net_guard ----------

def test_redirect_to_a_private_address_is_blocked_before_the_second_connection(
        resolver, transport):
    calls = resolver({OK_HOST: [PUBLIC_IP], "metadata.example": [PRIVATE_IP]})
    rec = transport([(302, {"location": "https://metadata.example/latest/meta-data"}, b"")])
    out = _fetch(f"https://{OK_HOST}/start")
    assert out.ok is False
    assert out.status == "blocked_address", out
    assert "内网" in out.text and PRIVATE_IP not in out.text, "内网 IP 只许进审计"
    assert calls == [OK_HOST, "metadata.example"], "第二跳没有重新解析就下了结论"
    assert len(rec.requests) == 1, "第二跳已被拒，却仍然发出了请求"
    target = _last_target()
    assert target["status"] == "blocked_address"
    assert target["host"] == "metadata.example" and target["path"] == "/latest/meta-data"
    assert target["hops"] == 1
    assert PRIVATE_IP in target.get("detail", ""), "拒绝理由的 IP 只进审计"


def test_policy_is_reapplied_on_every_hop_before_dns(resolver, transport):
    calls = resolver({OK_HOST: [PUBLIC_IP]})

    def policy(host):
        return "" if host == OK_HOST else f"主机 {host} 不在白名单"

    rec = transport([(302, {"location": "https://other.example/next"}, b"")])
    out = _fetch(f"https://{OK_HOST}/start", policy=policy)
    assert out.ok is False and out.status == "off_allowlist", out
    assert calls == [OK_HOST], "策略拒的主机不该被解析"
    assert len(rec.requests) == 1
    target = _last_target()
    assert target["host"] == "other.example" and target["hops"] == 1


def test_a_cross_host_https_redirect_is_revalidated_and_followed(resolver, transport):
    calls = resolver({OK_HOST: [PUBLIC_IP], "next.example": [PUBLIC_IP_2]})
    rec = transport([
        (302, {"location": "https://next.example/final"}, b""),
        _ok("<html><body><p>第二跳正文</p></body></html>"),
    ])
    out = _fetch(f"https://{OK_HOST}/start")
    assert out.ok is True, out.text
    assert "第二跳正文" in out.text
    assert calls == [OK_HOST, "next.example"]
    assert len(rec.requests) == 2
    assert rec.factory_kwargs[0]["pins"] == {OK_HOST: [PUBLIC_IP]}
    assert rec.factory_kwargs[1]["pins"] == {"next.example": [PUBLIC_IP_2]}, \
        "第二跳用了第一跳的 pin（或根本没 pin）"
    assert out.hops == 1 and out.host == "next.example"


def test_a_mixed_dns_answer_is_rejected_before_any_connection(resolver, transport):
    resolver({OK_HOST: [PUBLIC_IP, PRIVATE_IP]})
    rec = transport([_ok()])
    out = _fetch(f"https://{OK_HOST}/a")
    assert out.ok is False and out.status == "blocked_address", out
    assert rec.requests == [], "多答案里有一个内网，仍然连了公网那个"


def test_the_fourth_redirect_stops_at_the_limit(resolver, transport):
    resolver({OK_HOST: [PUBLIC_IP]})
    rec = transport([(302, {"location": f"https://{OK_HOST}/n{i}"}, b"")
                     for i in range(5)])
    out = _fetch(f"https://{OK_HOST}/start")
    assert out.ok is False and out.status == "too_many_redirects", out
    assert len(rec.requests) == 4, f"初始 + 3 跳就该停，实际发了 {len(rec.requests)} 次"


# ---------- 2. https→http 降级 ----------

def test_https_to_http_downgrade_is_rejected_before_the_next_hop(resolver, transport):
    calls = resolver({OK_HOST: [PUBLIC_IP]})
    rec = transport([(302, {"location": f"http://{OK_HOST}/plain"}, b"")])
    out = _fetch(f"https://{OK_HOST}/start")
    assert out.ok is False and out.status == "redirect_downgrade", out
    assert "降级" in out.text
    assert len(rec.requests) == 1, "降级目标已经被请求过了"
    assert calls == [OK_HOST], "降级目标不该进入解析"
    assert _last_target()["hops"] == 1, "账上要能看出是第几跳被降级拦下"


def test_http_to_https_upgrade_is_still_allowed(resolver, transport):
    resolver({OK_HOST: [PUBLIC_IP]})
    rec = transport([
        (302, {"location": f"https://{OK_HOST}/secure"}, b""),
        _ok("<html><body><p>升级后的正文</p></body></html>"),
    ])
    out = _fetch(f"http://{OK_HOST}/start")
    assert out.ok is True, out.text
    assert len(rec.requests) == 2


# ---------- 3. Location 缺失 / CRLF / 超长 ----------

def test_missing_location_is_rejected(resolver, transport):
    resolver({OK_HOST: [PUBLIC_IP]})
    rec = transport([(302, {}, b"")])
    out = _fetch(f"https://{OK_HOST}/a")
    assert out.ok is False and out.status == "redirect_no_location", out
    assert len(rec.requests) == 1


def test_location_with_crlf_is_rejected_by_core_not_httpx(resolver, raw_client):
    resolver({OK_HOST: [PUBLIC_IP]})
    rec = raw_client([(302, {"location": f"https://{OK_HOST}/next\r\nX-Injected: 1"}, b"")])
    out = _fetch(f"https://{OK_HOST}/a")
    assert out.ok is False and out.status == "redirect_invalid_location", out
    assert "控制字符" in out.text
    assert "X-Injected" not in out.text
    assert len(rec.requests) == 1


def test_overlong_location_is_rejected(resolver, transport):
    resolver({OK_HOST: [PUBLIC_IP]})
    location = f"https://{OK_HOST}/" + "a" * 2048   # 2067 > MAX_LOCATION_LENGTH
    rec = transport([(302, {"location": location}, b"")])
    out = _fetch(f"https://{OK_HOST}/a")
    assert out.ok is False and out.status == "redirect_invalid_location", out
    assert "2048" in out.text
    assert len(rec.requests) == 1


def test_location_at_exactly_the_limit_is_followed(resolver, transport):
    resolver({OK_HOST: [PUBLIC_IP]})
    location = f"https://{OK_HOST}/" + "a" * (2048 - len(f"https://{OK_HOST}/"))
    assert len(location) == 2048
    rec = transport([(302, {"location": location}, b""), _ok()])
    out = _fetch(f"https://{OK_HOST}/a")
    assert out.ok is True, out.text
    assert len(rec.requests) == 2, "边界长度被误拒了"


# ---------- 4. 解码后 1 MiB ----------

def test_exactly_one_mib_is_accepted(resolver, transport):
    resolver({OK_HOST: [PUBLIC_IP]})
    transport([(200, {"content-type": "text/plain"}, b"x" * MAX)])
    out = _fetch(f"https://{OK_HOST}/big")
    assert out.ok is True, out.text
    assert out.bytes == MAX


def test_one_byte_over_one_mib_is_aborted(resolver, transport):
    resolver({OK_HOST: [PUBLIC_IP]})
    transport([(200, {"content-type": "text/plain"}, b"x" * (MAX + 1))])
    out = _fetch(f"https://{OK_HOST}/big")
    assert out.ok is False and out.status == "too_large", out
    assert out.bytes > MAX


def test_body_over_the_limit_stops_reading_while_streaming(resolver, transport):
    """边读边停：超过上限后不再从流里取字节，不是先读完整段再判。"""
    resolver({OK_HOST: [PUBLIC_IP]})
    feed = _ChunkFeed(b"x" * 32768, count=80)       # 2.5 MiB
    transport([(200, {"content-type": "text/plain"}, feed)])
    out = _fetch(f"https://{OK_HOST}/big")
    assert out.ok is False and out.status == "too_large", out
    assert feed.sent <= MAX + 32768, f"超过上限后还在读：已读 {feed.sent} 字节"
    assert feed.sent < 2 * MAX, f"整段被读完了（{feed.sent} 字节），不是流式中止"


def test_gzip_expansion_is_counted_after_decoding(resolver, transport):
    resolver({OK_HOST: [PUBLIC_IP]})
    bomb = gzip.compress(b"x" * (MAX + 1))           # 压缩后很小，解压后超限
    transport([(200, {"content-type": "text/plain", "content-encoding": "gzip"}, bomb)])
    out = _fetch(f"https://{OK_HOST}/bomb")
    assert out.ok is False and out.status == "too_large", out
    assert out.bytes > MAX, "字节预算量在了压缩后的长度上"


# ---------- 5. 墙钟总时限 ----------

def test_wall_clock_deadline_aborts_a_slow_trickle(resolver, transport):
    """40 × 50ms 的慢滴流：0.3s 的硬总时限必须在远小于 2s 内中止。

    负向锁：删掉读块前的 deadline 检查（只靠 read 超时），本条 elapsed 会到 2s。
    """
    resolver({OK_HOST: [PUBLIC_IP]})
    feed = _ChunkFeed(b"x", count=40, delay=0.05)
    transport([(200, {"content-type": "text/plain"}, feed)])
    started = time.monotonic()
    out = _fetch(f"https://{OK_HOST}/slow", total_timeout=0.3)
    elapsed = time.monotonic() - started
    assert out.ok is False and out.status == "timeout", out
    assert "总时限" in out.text
    assert elapsed < 1.5, f"慢滴流没有被墙钟总时限截住：{elapsed:.2f}s"
    assert _last_target()["status"] == "timeout"


# ---------- 6. DNS 超时 ----------

def test_dns_timeout_fails_closed_without_hanging(monkeypatch):
    """getaddrinfo 卡住时按 DNS_TIMEOUT 失败：daemon 线程不占住调用方。"""
    monkeypatch.setattr(fetch_core, "DNS_TIMEOUT", 0.15)

    def slow_getaddrinfo(*_args, **_kwargs):
        time.sleep(0.8)
        return []

    import socket as real_socket
    monkeypatch.setattr(fetch_core, "socket", type("S", (), {
        "getaddrinfo": staticmethod(slow_getaddrinfo),
        "AF_UNSPEC": real_socket.AF_UNSPEC,
        "SOCK_STREAM": real_socket.SOCK_STREAM,
    })())
    started = time.monotonic()
    out = _fetch(f"https://{OK_HOST}/a")
    elapsed = time.monotonic() - started
    assert out.ok is False and out.status == "blocked_address", out
    assert "解析失败" in out.text
    assert elapsed < 0.65, f"DNS 超时没生效，等了 {elapsed:.2f}s"
    target = _last_target()
    assert "DNS 解析超时" in target.get("detail", ""), target
    assert "DNS 解析超时" not in out.text, "细节只许进审计"


def test_resolver_exception_and_empty_answer_are_both_fail_closed(monkeypatch):
    def boom(_host):
        raise OSError("dns down")

    monkeypatch.setattr(fetch_core, "_resolve_addresses", boom)
    out = _fetch(f"https://{OK_HOST}/a")
    assert out.ok is False and out.status == "blocked_address", out
    assert "解析失败" in out.text
    assert _last_target()["detail"].startswith("resolve_error=OSError"), _last_target()

    monkeypatch.setattr(fetch_core, "_resolve_addresses", lambda _host: [])
    out = _fetch(f"https://{OK_HOST}/a")
    assert out.ok is False and out.status == "blocked_address", out
    assert "解析结果为空" in out.text
    assert _last_target()["detail"] == "resolve_empty"


# ---------- 7. pin：一次解析，判闸与连接用同一份地址 ----------

def test_the_validated_addresses_are_pinned_once_per_hop(monkeypatch):
    """校验后模拟 DNS 换答案：连接层拿到的必须仍是校验时的地址列表。"""
    calls = []

    def fake_resolve(host):
        calls.append(host)
        if len(calls) == 1:
            return [PUBLIC_IP, PUBLIC_IP_2]
        return [PRIVATE_IP]                  # rebinding：第二次解析换答案

    monkeypatch.setattr(fetch_core, "_resolve_addresses", fake_resolve)
    seen = []

    def factory(**kwargs):
        seen.append(kwargs)
        return httpx.Client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"pinned")))

    monkeypatch.setattr(fetch_core, "make_client", factory)
    out = _fetch(f"https://{OK_HOST}/a", total_timeout=8.0)
    assert out.ok is True, out.text
    assert calls == [OK_HOST], f"一次请求解析了 {len(calls)} 次：{calls}"
    assert seen[0]["pins"] == {OK_HOST: [PUBLIC_IP, PUBLIC_IP_2]}, \
        "连接层没有拿到校验时的地址，DNS 换答案仍可生效"
    assert 0 < seen[0]["hop_timeout"] <= 8.0
    assert seen[0]["connect_timeout"] == fetch_core.CONNECT_TIMEOUT


def test_make_client_refuses_empty_pins():
    with pytest.raises(ValueError):
        fetch_core.make_client(pins={})
    with pytest.raises(RuntimeError):
        fetch_core._PinnedBackend({OK_HOST: []}).connect_tcp(OK_HOST, 443)


def test_make_client_installs_the_pinned_backend():
    client = fetch_core.make_client(pins={OK_HOST: [PUBLIC_IP]})
    try:
        backend = client._transport._pool._network_backend
        assert isinstance(backend, fetch_core._PinnedBackend)
    finally:
        client.close()


# ---------- 8. 审计剥离 ----------

def test_audit_strips_query_and_fragment(resolver, transport):
    resolver({OK_HOST: [PUBLIC_IP]})
    transport([_ok("<html><body><p>审计正文</p></body></html>")])
    out = _fetch(f"https://{OK_HOST}/p?needle=QUERY_LEAK_CANARY#frag-CANARY")
    assert out.ok is True, out.text
    target = _last_target()
    assert target["host"] == OK_HOST and target["path"] == "/p"
    assert target["status"] == "ok" and target["hops"] == 0
    assert "url" not in target, f"审计落了整条 URL：{target}"
    raw = _audit_text()
    for canary in ("QUERY_LEAK_CANARY", "frag-CANARY", "needle="):
        assert canary not in raw, f"审计泄露了 query/fragment：{canary}"
    assert "审计正文" not in raw, "正文进了审计"


def test_contract_rejection_does_not_leak_userinfo_into_audit(monkeypatch):
    monkeypatch.setattr(fetch_core, "_resolve_addresses", _forbidden_resolver)
    before = len(_records())
    out = _fetch(f"https://user:pass@example.com/p?needle=USERINFO_CANARY")
    assert out.ok is False and out.status == "contract_rejected", out
    assert len(_records()) == before + 1, "契约拒绝也必须落一条审计"
    target = _last_target()
    assert target["host"] == "example.com" and target["path"] == "/p"
    raw = _audit_text()
    for canary in ("user:pass", "USERINFO_CANARY", "needle="):
        assert canary not in raw, f"审计泄露了 userinfo/query：{canary}"


# ---------- 9. policy=None 是遗忘，不是放行 ----------

def test_policy_none_raises_before_dns_or_audit(monkeypatch):
    monkeypatch.setattr(fetch_core, "_resolve_addresses", _forbidden_resolver)
    before = len(_records())
    with pytest.raises(TypeError):
        fetch_core.fetch(f"https://{OK_HOST}/a", policy=None)
    with pytest.raises(TypeError):
        fetch_core.fetch(f"https://{OK_HOST}/a", policy="")
    assert len(_records()) == before, "policy 判据炸之前不该落审计"


def test_a_policy_reason_is_audited_as_off_allowlist(monkeypatch):
    monkeypatch.setattr(fetch_core, "_resolve_addresses", _forbidden_resolver)
    out = _fetch(f"https://blocked.example/a", policy=lambda _host: "不在名单")
    assert out.ok is False and out.status == "off_allowlist", out
    target = _last_target()
    assert target["host"] == "blocked.example" and target["path"] == "/a"


# ---------- 默认参数保持 v0.37.0 的 https-only/443 ----------

def test_default_schemes_stay_https_only(monkeypatch):
    monkeypatch.setattr(fetch_core, "_resolve_addresses", _forbidden_resolver)
    out = fetch_core.fetch(f"http://{OK_HOST}/a", policy=lambda _host: "",
                           audit_action=ACTION)
    assert out.ok is False and out.status == "contract_rejected", out
    assert "只允许 https" in out.text
