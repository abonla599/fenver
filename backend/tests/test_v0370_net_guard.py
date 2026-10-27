"""v0.37.0 第一批：net_guard 抽取 + IP 字面量缺口加固。

背景：providers 的 base_url 闸门里，IP 字面量走的是快路，域名那一支才有的
`is_link_local` / `is_multicast` 属性补丁对快路不可见，于是
`http://[fe80::10]/`、`http://224.0.0.1/`、`http://[ff02::1]/` 这三类**写法**
能直接过关；`100.64.0.0/10`（CGNAT）、三个文档段、`198.18.0.0/15`（基准段）
连清单里都没有，两支写法一起漏。判据抽到 `app.core.net_guard` 之后，清单补齐，
字面量与域名共用同一个 `_address_reject_reason`。

本文件钉四件事：
1. 每一类新补的地址段，字面量写法必须真的被拒（缺任何一条 CIDR/属性判据红）；
2. 公网地址不许误伤——每个新段都配了边界外一例（负控的另一半：判据过宽也红）；
3. 抽取本身不换口径：既有的 reason 句式逐字保持（"指向" vs "的主机名…解析到"，
   链路本地/组播/内网的分类词），providers 侧仍**调用时**取注入点；
4. 负控变异：把清单里某一段在**内存里**摘掉，对应字面量必须转绿为放行——
   证明那条 CIDR 是真的判据来源，不是一句注释。等价性自陈：monkeypatch 换的是
   `net_guard` 模块属性本身，`reject_reason` 在调用时才读它，走的正是生产同一条
   代码路径；不落盘、teardown 自动还原，提交的清单文件从未被改过。

第二批（审查放行前置）在同一张钉板上追加：6to4 `2002::/16`、Teredo `2001::/32`、
`192.0.0.0/24`、`192.88.99.0/24`、IPv4-compatible `::/96` 五段各有字面量正控、
边界外负控与"摘掉这一段必须转绿"的变异；zone-id 写法（`fe80::1%eth0` 这类）
fail-closed 且理由点名 zone-id，而 `::1%lo`/`::%lo` 的既有"本机"语义不被挪走。

用法（本目录惯例）：../venv/Scripts/python.exe -m pytest tests/test_v0370_net_guard.py -q
"""
import ipaddress
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.core import net_guard, providers


def _forbidden_resolver(host):
    """字面量/localhost 快路必须**不调用**解析器：调用了就是这里红。"""
    raise AssertionError(f"不该触发 DNS 解析，却调用了：{host}")


def _reject(literal, *, allow_loopback=False):
    return net_guard.reject_reason(literal, resolve=_forbidden_resolver,
                                   allow_loopback=allow_loopback, where="base_url")


# ---------- 缺口加固：任务点名的三个 URL 现在必须被拒 ----------

@pytest.mark.parametrize("url", [
    "http://[fe80::10]/",
    "http://224.0.0.1/",
    "http://[ff02::1]/",
])
def test_v0370_named_gap_urls_are_now_rejected(url):
    """修复前这三个在真码里一路放行（规划员复现、事实核查确认）。走整条 URL 路径。

    负向锁：把 net_guard 里 fe80::/10、ff00::/12、224.0.0.0/4 三段与对应的属性
    判断一起删掉，本条红。
    """
    why = providers._base_url_reject_reason(url)
    assert why, f"v0.37.0 要补的缺口居然还放行：{url}"
    assert any(k in why for k in ("链路本地", "组播", "内网")), f"理由要说清类别：{why!r}"


# ---------- 全类别字面量清单：私网/链路本地/组播/CGNAT/文档/基准/保留 ----------

BLOCKED_LITERALS = {
    "回环 v4": ("127.0.0.1", "本机"),
    "回环 v4 边缘": ("127.255.255.255", "本机"),
    "回环 v6": ("::1", "本机"),
    "全零 v4": ("0.0.0.0", "本机"),
    "零段非全零": ("0.1.2.3", "内网"),
    "IPv6 未指定": ("::", "本机"),
    "私网 A": ("10.9.8.7", "内网"),
    "私网 B": ("172.20.0.1", "内网"),
    "私网 C": ("192.168.30.40", "内网"),
    "链路本地 v4（云元数据）": ("169.254.169.254", "内网"),
    "唯一本地 v6": ("fd12::34", "内网"),
    "fc00 v6": ("fc01::2", "内网"),
    "链路本地 v6": ("fe80::10", "链路本地"),
    "链路本地 v6 边缘": ("febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff", "链路本地"),
    "组播 v4": ("224.0.0.1", "组播"),
    "组播 v4 边缘": ("239.255.255.255", "组播"),
    "组播 v6": ("ff02::1", "组播"),
    "组播 v6 上沿": ("ff0f::8", "组播"),
    "CGNAT 下沿": ("100.64.0.0", "内网"),
    "CGNAT 上沿": ("100.127.255.255", "内网"),
    "文档段 1": ("192.0.2.5", "内网"),
    "文档段 2": ("198.51.100.7", "内网"),
    "文档段 3": ("203.0.113.200", "内网"),
    "基准段": ("198.18.9.9", "内网"),
    "基准段上沿": ("198.19.255.255", "内网"),
    "v6 文档段": ("2001:db8::1", "内网"),
    "保留段 class E": ("240.0.0.1", "内网"),
    "受限广播": ("255.255.255.255", "内网"),
    "v4 映射私网": ("::ffff:10.0.0.1", "内网"),
    "v4 映射元数据": ("::ffff:169.254.169.254", "内网"),
    "v4 映射回环": ("::ffff:127.0.0.1", "本机"),
    # ---- v0.37.0 第二批：审查点名的五个特殊用途段 ----
    "6to4 内嵌回环": ("2002:7f00:1::1", "内网"),
    "6to4 内嵌私网": ("2002:0a01:203::", "内网"),
    "6to4 内嵌公网": ("2002:0808:808::", "内网"),
    "Teredo 前缀": ("2001::1234", "内网"),
    "Teredo 满址": ("2001:0:ffff:ffff::", "内网"),
    "IETF 协议分配段": ("192.0.0.1", "内网"),
    "IETF 段尾": ("192.0.0.255", "内网"),
    "6to4 中继 anycast": ("192.88.99.1", "内网"),
    "compat 内嵌回环": ("::127.0.0.1", "内网"),
    "compat 内嵌私网": ("::10.0.0.1", "内网"),
    "compat 内嵌公网": ("::8.8.8.8", "内网"),
    # ---- v0.37.0 第二批修复：残留可编码 v4 与四个特殊用途段 ----
    "IPv4-translated 元数据": ("::ffff:0:169.254.169.254", "内网"),
    "IPv4-translated 私网": ("::ffff:0:10.0.0.1", "内网"),
    "NAT64 元数据": ("64:ff9b::a9fe:a9fe", "内网"),
    "NAT64 私网": ("64:ff9b::a00:1", "内网"),
    "local-use NAT64": ("64:ff9b:1::7f00:1", "内网"),
    "discard-only": ("100::1", "内网"),
    "IPv6 基准测试段": ("2001:2::1", "内网"),
    "ORCHID": ("2001:10::1", "内网"),
    "IPv6 文档段 3fff": ("3fff::1", "内网"),
}


@pytest.mark.parametrize("literal,keyword",
                         list(BLOCKED_LITERALS.values()),
                         ids=list(BLOCKED_LITERALS))
def test_every_blocked_category_rejects_the_ip_literal(literal, keyword):
    """IPv4/IPv6 全部私网/链路本地/组播/CGNAT/文档/基准/保留段——**字面量写法**。

    这是本轮的主修复：v0.33/v0.34.2 的旧闸门里，链路本地 v6、组播（两支）、
    CGNAT、文档段、基准段、保留段、v4 映射写法都能以字面量过关。

    负向锁：删掉 net_guard 清单里对应那一段（属性类还得连属性判断一起删），
    本条红；把 `_address_reject_reason` 里的映射折叠删掉，三条 v4 映射参数红。
    """
    why = _reject(literal)
    assert why, f"字面量 {literal} 居然放行"
    assert keyword in why, f"{literal} 的理由应含「{keyword}」：{why!r}"


def test_bracketed_and_uppercase_v6_literals_are_rejected():
    """`[FE80::10]` 这种带括号大写写法与裸串同判——strip("[]")/lower 的口径没丢。"""
    assert _reject("[FE80::10]")
    assert _reject("[FF02::1]")


# ---------- zone-id（审查前置第 5 条）：fail-closed 且理由可读 ----------

@pytest.mark.parametrize("literal", [
    "fe80::1%eth0",              # 链路本地 + zone：属性/zone 两条都拒得住，这里钉 zone 文案
    "2001:4860:4860::8888%eth0", # 公网地址 + zone 一样拒：zone 只对"本机某条链路"有意义
    "::ffff:8.8.8.8%eth0",       # v4 映射写法 + zone：折叠前捕获 scope，公网兜底也算链路寻址
])
def test_zone_id_literals_fail_closed_with_readable_reason(literal):
    """带 `%接口` 的地址一律 fail closed，理由点名 zone-id，读的人知道该改什么。"""
    why = _reject(literal)
    assert why, f"zone-id 写法居然放行：{literal}"
    assert "zone-id" in why, f"理由应点名 zone-id：{why!r}"


@pytest.mark.parametrize("literal,keyword", [
    ("::1%lo", "本机"),          # 既有语义保住：::1 带 lo 仍按回环类走"本机"文案与开关
    ("::%lo", "本机"),           # :: 带 lo 仍按全零类
    ("fe80::10", "链路本地"),    # 不带 zone 的链路本地文案不变（scope 分支排在属性前只影响带 % 的）
])
def test_zone_id_does_not_disturb_existing_categories(literal, keyword):
    """`::1%lo`/`::%lo` 的回环语义与不带 zone 的既有分类词都不许被 zone 分支挪走。

    负向锁：把 zone-id 分支挪到回环分支之前，前两条参数会变成 zone-id 文案而红——
    开关打开时 `::1%lo` 甚至会从"可放行"变成"恒拒"。
    """
    why = _reject(literal)
    assert keyword in why, f"{literal} 的理由应含「{keyword}」：{why!r}"
    assert "zone-id" not in why, why


def test_loopback_hint_switch_for_non_providers_callers():
    """loopback_hint 只影响尾巴句：providers 默认逐字不动，别的调用方可去掉那句指向。

    负向锁：把 _LOOPBACK_HINT 的文本改掉或让默认值变成 ""，第一条红（管理员文案漂移）；
    把 "" 分支写死回默认尾巴，第二条红（http_fetch 拿到对它无效的开关指引）。
    """
    assert net_guard.reject_reason(
        "127.0.0.1", resolve=_forbidden_resolver, allow_loopback=False,
        where="base_url") == (
        "base_url 指向本机地址 127.0.0.1，服务端不做本机请求；"
        "确实要把模型自建在本机的话，把 providers.ALLOW_LOCALHOST_BASE_URL 打开")
    why = net_guard.reject_reason(
        "127.0.0.1", resolve=_forbidden_resolver, allow_loopback=False,
        where="url", loopback_hint="")
    assert why == "url 指向本机地址 127.0.0.1，服务端不做本机请求", why

    assert net_guard.reject_reason(
        "localhost", resolve=_forbidden_resolver, allow_loopback=False,
        where="url", loopback_hint="") == "url 指向 localhost，服务端不做本机请求"


# ---------- 公网不误伤：每个新段的边界外一例 ----------

ALLOWED_LITERALS = [
    "8.8.8.8", "1.1.1.1", "93.184.216.34", "104.16.0.1",
    "11.0.0.1",              # 10/8 边界外
    "172.15.255.255", "172.32.0.1",   # 172.16/12 边界外
    "192.167.255.255", "192.169.0.1",  # 192.168/16 边界外
    "169.253.0.1", "169.255.0.1",      # 169.254/16 边界外
    "100.63.255.255", "100.128.0.0",   # CGNAT 边界外
    "192.0.1.255", "192.0.3.0",        # TEST-NET-1 边界外
    "198.17.255.255", "198.20.0.0",    # 基准段边界外
    "198.51.99.255", "198.51.101.0",   # TEST-NET-2 边界外
    "203.0.112.255", "203.0.114.0",    # TEST-NET-3 边界外
    "223.255.255.255",                 # 组播段下边界外
    "2003::",                          # 2002::/16（6to4）上边界外
    "2001:1::",                        # 2001::/32（Teredo）上边界外（下一整个 /32）
    "192.0.1.0",                       # 192.0.0.0/24（IETF 协议分配）上边界外
    "192.88.98.255", "192.88.100.0",   # 192.88.99.0/24（6to4 中继）两侧边界外
    "2606:2800:220:1:248:1893:25c8:1946",
    "2001:4860:4860::8888",            # 2001:db8::/32 同 prefix 长邻居，不是文档段
    "fe00::",                          # fe80::/10 边界外
    "::ffff:93.184.216.34",            # v4 映射到公网仍算公网
    # ---- 可编码 v4 段按内嵌 v4 判：内嵌公网不误伤 ----
    "::ffff:0:8.8.8.8",                # IPv4-translated 内嵌公网
    "64:ff9b::808:808",                # NAT64 内嵌 8.8.8.8
    # ---- 四个特殊用途段的边界外一例 ----
    "100:0:0:1::",                     # 100::/64 边界外
    "2001:1:ffff::",                   # 2001:2::/48 边界外（也在 Teredo 2001::/32 之外）
    "2001:20::",                       # 2001:10::/28 边界外
    "3fff:1000::",                     # 3fff::/20 边界外
]


@pytest.mark.parametrize("literal", ALLOWED_LITERALS)
def test_public_and_boundary_outside_literals_stay_allowed(literal):
    """闸门拦的是特殊用途段，不是"所有地址"——边界外一例都不许误伤。

    负向锁：把清单里任何一段写宽（如 100.64.0.0/10 写成 /9），对应边界参数红。
    """
    assert _reject(literal) == "", f"公网/边界外地址被误拒：{literal}"


# ---------- 抽取不换口径：既有句式逐字钉住 ----------

def test_detailed_reason_sentences_are_unchanged():
    """net_guard 的详细文案一位都不动（v0.33 立的原话）。

    M-4 之后 providers 的出口只回类别，所以逐字句式改为直接对 net_guard 断言；
    这同时证明内部排障信息没被削掉。

    负向锁：改动 net_guard 里任何一条 f-string 模板，本条红。
    """
    assert net_guard.reject_reason(
        "10.1.2.3", resolve=_forbidden_resolver, allow_loopback=False,
        where="base_url") == (
        "base_url 指向内网地址 10.1.2.3（10.0.0.0/8），服务端不做内网请求")
    assert net_guard.reject_reason(
        "127.0.0.1", resolve=_forbidden_resolver, allow_loopback=False,
        where="base_url") == (
        "base_url 指向本机地址 127.0.0.1，服务端不做本机请求；"
        "确实要把模型自建在本机的话，把 providers.ALLOW_LOCALHOST_BASE_URL 打开")
    assert providers._base_url_reject_reason("http:///v1") == "base_url 里没有主机名"


def test_provider_outlet_returns_public_categories_only():
    """经 ProviderError 回显的文案只有类别：具体地址/CIDR 不出 providers 这一层。

    负向锁：把 `_base_url_reject_reason` 的返回改回未公开化的 `detail`，本条红。
    """
    why = providers._base_url_reject_reason("http://10.1.2.3/v1")
    assert why == "该地址属于内网，不被允许", why
    assert "10.1.2.3" not in why and "10.0.0.0/8" not in why

    why = providers._base_url_reject_reason("http://127.0.0.1:8000/v1")
    assert why == "该地址指向本机，不被允许", why
    assert "127.0.0.1" not in why and "ALLOW_LOCALHOST_BASE_URL" not in why


def test_domain_branch_wordings_survive_the_move(monkeypatch):
    """域名支的"链路本地/组播/内网"三种分类词不因补 CIDR 而改写：
    fe80/组播走属性判断（排在清单前），169.254 仍走 CIDR 文案。

    详细句式直接对 net_guard 断言（providers 出口已公开化，见上一测试）。

    负向锁：把属性判断挪到 CIDR 循环之后，前两条参数会变成"内网地址"文案而红。
    """
    cases = [
        ("fe80::1", "解析到链路本地地址 fe80::1"),
        ("224.0.0.1", "解析到组播地址 224.0.0.1"),
        ("169.254.1.1", "解析到内网地址 169.254.1.1（169.254.0.0/16）"),
        ("100.64.1.2", "解析到内网地址 100.64.1.2（100.64.0.0/10）"),
    ]
    for answer, snippet in cases:
        why = net_guard.reject_reason(
            "mine.example", resolve=lambda host, a=answer: [a],
            allow_loopback=False, where="base_url")
        assert snippet in why, why

    monkeypatch.setattr(providers, "_resolve_addresses", lambda h: ["fe80::1"])
    why = providers._base_url_reject_reason("https://mine.example/v1")
    assert "链路本地" in why and "fe80::1" not in why, why


def test_providers_still_owns_the_injection_points():
    """薄包装的三件事：别名同源、URL 层留在 providers、开关节关仍在 providers。

    负向锁：`_PRIVATE_NETWORKS` 若被复制成另一份 tuple（两处清单迟早对不上），
    第一条红；开关被搬去 net_guard（providers 读不到），第二条红。
    """
    assert providers._PRIVATE_NETWORKS is net_guard.PRIVATE_NETWORKS
    assert hasattr(providers, "ALLOW_LOCALHOST_BASE_URL")
    assert hasattr(providers, "_resolve_addresses")
    import inspect
    sig = inspect.signature(providers._base_url_reject_reason)
    assert list(sig.parameters) == ["base_url"], \
        f"包装函数只许吃 base_url，注入点必须留在函数体里取：{sig}"
    src = inspect.getsource(providers._base_url_reject_reason)
    assert "resolve=_resolve_addresses," in src
    assert "allow_loopback=ALLOW_LOCALHOST_BASE_URL," in src


# ---------- 多答案 / 解析失败 / 空 / 垃圾 ----------

def test_multi_answer_rejects_when_any_one_crosses(monkeypatch):
    """全称判据：公网排前也不许只看第一条；对外只报「内网」类别。"""
    monkeypatch.setattr(providers, "_resolve_addresses",
                        lambda h: ["93.184.216.34", "100.64.5.6"])
    why = providers._base_url_reject_reason("https://mixed.example/v1")
    assert why and "内网" in why, why
    assert "100.64.5.6" not in why, why

    detail = net_guard.reject_reason(
        "mixed.example", resolve=lambda h: ["93.184.216.34", "100.64.5.6"],
        allow_loopback=False, where="base_url")
    assert "100.64.5.6" in detail, f"内部理由要点名踩线的那条：{detail!r}"


def test_multi_answer_all_public_is_allowed(monkeypatch):
    monkeypatch.setattr(providers, "_resolve_addresses",
                        lambda h: ["93.184.216.34", "2001:4860:4860::8888"])
    assert providers._base_url_reject_reason("https://dual.example/v1") == ""


@pytest.mark.parametrize("answers,detail_keyword,public_keyword", [
    (None, "解析失败", "解析失败"),
    ([], "解析结果为空", "解析结果为空"),
    (["not-an-ip"], "无法识别", "不被允许"),
])
def test_resolution_failure_modes_reject_fail_closed(monkeypatch, answers,
                                                     detail_keyword, public_keyword):
    """解析炸/空/垃圾三类都拒（fail closed）；内部句式不变，对外只给类别。"""
    def fake(host):
        if answers is None:
            raise OSError(-2, "Name or service not known")
        return list(answers)

    monkeypatch.setattr(providers, "_resolve_addresses", fake)
    why = providers._base_url_reject_reason("https://bad.example/v1")
    assert why and public_keyword in why, f"{public_keyword}: {why!r}"
    assert "bad.example" not in why, f"对外文案回了主机名：{why!r}"

    detail = net_guard.reject_reason(
        "bad.example", resolve=fake, allow_loopback=False, where="base_url")
    assert detail_keyword in detail, f"{detail_keyword}: {detail!r}"


# ---------- allow_loopback 只放回环类 ----------

@pytest.mark.parametrize("target", [
    "127.0.0.1", "::1", "0.0.0.0", "::", "localhost", "api.localhost",
])
def test_allow_loopback_releases_only_the_local_machine_class(target):
    """开关打开：回环/全零/localhost 名字放行。

    负向锁：把 `_address_reject_reason` 开关分支写死 return，本条红。
    """
    assert _reject(target, allow_loopback=True) == ""


@pytest.mark.parametrize("target", [
    "10.0.0.5", "169.254.169.254", "fe80::10", "224.0.0.1", "ff02::1",
    "100.64.0.1", "fd12::34",
])
def test_allow_loopback_never_releases_private_link_local_or_multicast(target):
    """开关的理由是"允许自建模型"，不是"允许扫内网"——其余类别打开也照拒。"""
    why = _reject(target, allow_loopback=True)
    assert why, f"开关打开居然放行了 {target}"


def test_allow_loopback_continues_past_loopback_to_the_next_answer(monkeypatch):
    """域名多答案：开关打开跳过回环那条，继续查出后面踩线的地址。

    对外只报「内网」；详细理由仍点名 203.0.113.9（TEST-NET-3）。

    负向锁：把开关分支的 return "" 写成"整个函数放行"，本条红。
    """
    monkeypatch.setattr(providers, "_resolve_addresses",
                        lambda h: ["127.0.0.1", "203.0.113.9"])
    monkeypatch.setattr(providers, "ALLOW_LOCALHOST_BASE_URL", True)
    why = providers._base_url_reject_reason("https://mine.example/v1")
    assert why and "内网" in why, why
    assert "203.0.113.9" not in why, why

    detail = net_guard.reject_reason(
        "mine.example", resolve=lambda h: ["127.0.0.1", "203.0.113.9"],
        allow_loopback=True, where="base_url")
    assert "203.0.113.9" in detail, f"内部理由要点名踩线的那条：{detail!r}"


def test_literals_and_localhost_never_reach_the_resolver():
    """快路不碰 DNS：字面量（含新补的类别）与 localhost 名直接判定。

    `_reject` 携带的解析器一旦被调用就抛——放行/拒绝之外，"没被调用"也是断言。
    """
    for target in ("224.0.0.1", "fe80::1", "100.64.0.1", "localhost", "x.localhost"):
        assert _reject(target), target


# ---------- 负控：缺一类地址，判据必须报红（内存变异） ----------

CIDR_ONLY_RANGES = [
    ("100.64.0.0/10", "100.64.5.6"),
    ("192.0.2.0/24", "192.0.2.5"),
    ("198.51.100.0/24", "198.51.100.1"),
    ("203.0.113.0/24", "203.0.113.42"),
    ("198.18.0.0/15", "198.18.9.9"),
    ("2001:db8::/32", "2001:db8::5"),
    ("240.0.0.0/4", "240.0.0.1"),
    ("0.0.0.0/8", "0.1.2.3"),
    ("fc00::/7", "fd12::34"),
    ("10.0.0.0/8", "10.1.2.3"),
    ("169.254.0.0/16", "169.254.169.254"),
    # ---- v0.37.0 第二批：五个特殊用途段各自独立把关 ----
    ("2002::/16", "2002:7f00:1::1"),
    ("2001::/32", "2001::1234"),
    ("192.0.0.0/24", "192.0.0.1"),
    ("192.88.99.0/24", "192.88.99.1"),
    # compat 的负控判据尤其要紧：摘掉 ::/96 后 `::127.0.0.1` 必须转绿（放行），
    # 若仍被拒说明别的分支在替它把关，这条 CIDR 就是死配置。
    ("::/96", "::127.0.0.1"),
    # ---- v0.37.0 第二批修复：四个新补特殊用途段 ----
    ("100::/64", "100::1"),
    ("2001:2::/48", "2001:2::1"),
    ("2001:10::/28", "2001:10::1"),
    ("3fff::/20", "3fff::1"),
]


@pytest.mark.parametrize("cidr,literal", CIDR_ONLY_RANGES)
def test_gate_goes_red_only_because_of_the_declared_range(monkeypatch, cidr, literal):
    """每条 CIDR-only 判据的负控：原清单下拒；在内存里摘掉**这一段**后放行。

    绿灯必须能归因到清单里那一行——摘掉还拒，说明判据在别处（本参数就是错的
    "CIDR-only"断言）；摘掉变放行，说明这一段真的在把关。

    等价性自陈：变异是把 `net_guard.PRIVATE_NETWORKS` 换成过滤后的**新 tuple**
    （模块属性、调用时读取，与生产写入路径同一入口），不改任何落盘文件；
    monkeypatch 在 teardown 还原原对象，本文件之后的用例拿到的仍是原清单。
    """
    pristine = net_guard.PRIVATE_NETWORKS
    assert _reject(literal), f"原清单下 {literal} 就该被拒（前置态不对，变异无从谈起）"
    mutated = tuple(n for n in pristine if str(n) != cidr)
    assert len(mutated) == len(pristine) - 1, f"清单里找不到 {cidr}，这一参数本身是假的"
    monkeypatch.setattr(net_guard, "PRIVATE_NETWORKS", mutated)
    assert _reject(literal) == "", f"摘掉 {cidr} 仍拒 {literal}：判据另有出处，此负控无效"


@pytest.mark.parametrize("cidr,literal", [
    ("fe80::/10", "fe80::10"),
    ("224.0.0.0/4", "224.0.0.1"),
    ("ff00::/12", "ff02::1"),
])
def test_link_local_and_multicast_are_double_covered(monkeypatch, cidr, literal):
    """链路本地/组播是"清单 + 属性判断"双保险：摘掉 CIDR 仍被属性拦住。

    钉的是加固的冗余度（文案靠属性、完备性靠清单）；负向锁：连同
    `_address_reject_reason` 里的属性分支一起删才可能放行。
    """
    pristine = net_guard.PRIVATE_NETWORKS
    mutated = tuple(n for n in pristine if str(n) != cidr)
    monkeypatch.setattr(net_guard, "PRIVATE_NETWORKS", mutated)
    why = _reject(literal)
    assert why, f"{literal} 摘掉 {cidr} 后放行——属性判断不成立，双保险是假的"
    assert any(k in why for k in ("链路本地", "组播")), why


def test_mutations_do_not_leak():
    """负控之后清单必须原样：条数与成员逐一等于首拍快照（证明 monkeypatch 还原了）。

    若变异泄漏进后续用例，本文件乃至全套的绿灯都不再可信。
    """
    expected = tuple(ipaddress.ip_network(c) for c in (
        "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        "169.254.0.0/16", "0.0.0.0/8", "fc00::/7", "100.64.0.0/10", "192.0.2.0/24",
        "198.51.100.0/24", "203.0.113.0/24", "198.18.0.0/15", "224.0.0.0/4",
        "ff00::/12", "fe80::/10", "2001:db8::/32", "240.0.0.0/4",
        "2002::/16", "2001::/32", "192.0.0.0/24", "192.88.99.0/24", "::/96",
        "100::/64", "2001:2::/48", "2001:10::/28", "3fff::/20", "64:ff9b:1::/48"))
    assert net_guard.PRIVATE_NETWORKS == expected


def test_translated_and_nat64_fold_to_embedded_v4(monkeypatch):
    """`::ffff:0:0:0/96` 与 `64:ff9b::/96` 的判据是"折叠内嵌 v4 后按 v4 清单判"。

    负向锁：把 `_V4_EMBEDDED_NETWORKS` 换成空 tuple，两条元数据地址会转绿——
    证明这两段不是被别的分支顺手拦住的，判据真的挂在折叠这一步。
    """
    assert _reject("::ffff:0:169.254.169.254")
    assert _reject("64:ff9b::a9fe:a9fe")
    monkeypatch.setattr(net_guard, "_V4_EMBEDDED_NETWORKS", ())
    assert _reject("::ffff:0:169.254.169.254") == ""
    assert _reject("64:ff9b::a9fe:a9fe") == ""
