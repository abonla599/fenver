"""v0.34.2 SSRF 补口：域名必须先解析、再判地址形状。

v0.33 那道闸门（`providers._base_url_reject_reason`）只看了两样东西：主机名是不是
IP 字面量、以及名字是不是 `localhost`/`*.localhost`。剩下所有**域名**走最后一行
`return ""` ——也就是"只要这串字符不像内网，就放行"。缺口于是长这样：

    https://good-model.example/   → A 记录 127.0.0.1        → 过关
    https://tenant-a.example/v1   → A 记录 169.254.169.254  → 过关（云元数据）

而过关之后的代价不是"扫一下内网"这么轻：全局 provider（owner 为空、站级默认那条）
的 base_url 被指走后，此后每一个走默认上游的用户每轮聊天都会把管理员的 api_key
作为 `Authorization: Bearer` 送出去（见本模块顶部 v0.33 那段注释的第 2 条危害）。

这一轮把域名那一支补成：**解析之后，任一地址踩线就拒**；解析失败也拒（fail closed）。
判据继续复用 `_PRIVATE_NETWORKS` 那一份网段清单，不另写 CIDR——两处清单迟早对不上。

`ALLOW_LOCALHOST_BASE_URL` 的语义一位都不动：它只管"本机"这一类（回环/全零/
localhost 这个名字），私有与链路本地在任何开关状态下都拒。这个开关的理由是
"把模型自建在本机"（Ollama 那种），不是"允许扫内网"。

**这一轮没有关掉的东西**（写在这里免得下一个人以为已经关完）：闸门只在配置**写入时**
跑一次（唯一调用点是 `ProviderStore._validate`），真正出站在 `build_client` 里由
SDK 重新解析一次主机名——写入时验过的 IP 和连接时用的 IP 之间隔着一次重新解析，
DNS rebinding / TOCTOU 窗口仍然在。要钉死它得把已校验的 IP 传进连接层（自定
transport 按 IP 连、Host/SNI 另给），那是改客户端路径的另一件事。本文件里
`test_toctou_window_is_documented_not_closed` 就是钉"别把它写成已覆盖"的。

判据的形状照本目录既有风格写成负向锁：每条都注明"把哪一段删掉，这条会红"。
每条断言都收在"理由是一句**非空**的话"上——只断 `is not None` 是本项目产假绿的
老办法：空串照样不是 None，而 `if blocked:` 会把空串当放行。

用法（本目录惯例）：../venv/Scripts/python.exe -m pytest tests/test_v0342_ssrf_resolve_guards.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import socket

import pytest

from app.core import net_guard


def _stub(monkeypatch, *addresses, raises=None):
    """把 DNS 解析换成固定答案（生产走真 getaddrinfo，测试一次都不许出网）。"""
    from app.core import providers

    def fake_resolve(host):
        if raises is not None:
            raise raises
        return list(addresses)

    monkeypatch.setattr(providers, "_resolve_addresses", fake_resolve)
    return providers


def _record(**over):
    # secret-scan:allow —— 这颗是测试造的假密钥，这台机器上没有。标记必须与
    # 密钥**同一行**（判据与原因见 tests/test_v033_ssrf_guards.py 的 _record）。
    base = {"label": "公网样子", "api_key": "sk-real-looking-key-123456",  # secret-scan:allow
            "model": "qwen-plus"}
    base.update(over)
    return base


# ---------- 域名解析到内网：必须拒，对外理由只给类别 ----------

PRIVATE_ANSWERS = {
    "回环 v4": "127.0.0.1",
    "回环 v4 另一个别名": "127.5.5.5",
    "云元数据": "169.254.169.254",
    "链路本地": "169.254.1.1",
    "私网 A": "10.0.0.5",
    "私网 B": "172.16.9.9",
    "私网 C": "192.168.1.1",
    "全零": "0.0.0.0",
    "回环 v6": "::1",
    "唯一本地 v6": "fd12::34",
    "IPv6 链路本地": "fe80::1",
    "组播 v4": "224.0.0.1",
    "组播 v6": "ff02::1",
}


@pytest.mark.parametrize("literal", PRIVATE_ANSWERS.values(), ids=list(PRIVATE_ANSWERS))
def test_domain_resolving_to_private_range_is_rejected(monkeypatch, literal):
    """一个像公网的名字解析到内网/回环/链路本地/组播 → 必须是拒，不是放行。

    对外理由只给类别（M-4：解析地址是内网 DNS oracle），但仍须说清属于哪一类。

    负向锁：把域名那一支解析+判段的整段删掉（恢复成 v0.34.2 之前的 `return ""`），
    本条 13 个参数全部红——理由那一句变成空串，断言当场抓到。
    """
    providers = _stub(monkeypatch, literal)

    why = providers._base_url_reject_reason("https://good-model.example/v1")
    assert isinstance(why, str), f"理由必须是字符串，拿到 {type(why).__name__}"
    assert why, f"解析到 {literal} 居然放行了（返回空串=放行）"
    assert any(k in why for k in ("本机", "链路本地", "组播", "内网")), why


@pytest.mark.parametrize("literal", PRIVATE_ANSWERS.values(), ids=list(PRIVATE_ANSWERS))
def test_domain_resolving_to_private_range_blocks_the_write_path(monkeypatch, literal):
    """同一个判据必须挡在**唯一写路径**上：_validate 要抛出 ProviderError。

    只测纯函数是半条命：闸门挂在 `_validate` 里才挡得住管理员点保存和
    POST /v1/providers/test 的草稿试连（调用点见 providers.py 的 `_validate`）。
    抛出的文案同样只含类别，不含这个地址。

    负向锁：把 `_validate` 里 `blocked = _base_url_reject_reason(base_url)` 那三行
    删掉，本条红（异常不发生）。
    """
    providers = _stub(monkeypatch, literal)

    with pytest.raises(providers.ProviderError) as caught:
        providers.ProviderStore._validate(_record(base_url="https://good-model.example/v1"))
    assert any(k in str(caught.value) for k in ("本机", "链路本地", "组播", "内网")), \
        f"抛出来的得是类别理由：{caught.value!r}"
    assert literal not in str(caught.value), f"异常文案回显了解析地址：{caught.value!r}"


def test_any_address_of_a_multi_answer_name_rejects_it(monkeypatch):
    """A/AAAA 多条记录：只要**有一条**踩线就拒——全称判据，不是看第一条。

    对外文案只给「内网」类别；详细理由（点名踩线的那条地址）仍可从
    net_guard.reject_reason 拿到。

    负向锁：把 `for literal in addresses` 改成只看 `addresses[0]`，本条红
    （公网排在前，内网那条就被忽略了）。
    """
    providers = _stub(monkeypatch, "93.184.216.34", "10.0.0.5")

    why = providers._base_url_reject_reason("https://good-model.example/v1")
    assert why and "内网" in why, why
    assert "10.0.0.5" not in why, why
    detail = net_guard.reject_reason(
        "good-model.example",
        resolve=lambda host: ["93.184.216.34", "10.0.0.5"],
        allow_loopback=False,
        where="base_url",
    )
    assert "10.0.0.5" in detail, f"内部详细理由要点名踩线的地址：{detail!r}"


def test_resolving_to_public_ip_is_allowed(monkeypatch):
    """公网答案照旧放行——闸门拦的是内网，不是"所有域名"。

    负向锁：把域名那一支改成无条件返回一句理由（"一律不许"），或把
    `_PRIVATE_NETWORKS` 写成 0.0.0.0/0，本条立刻红。
    """
    providers = _stub(monkeypatch, "93.184.216.34")

    assert providers._base_url_reject_reason("https://good-model.example/v1") == ""
    cleaned = providers.ProviderStore._validate(
        _record(base_url="https://good-model.example/v1"))
    assert cleaned["base_url"] == "https://good-model.example/v1"


def test_dual_stack_public_answers_both_pass(monkeypatch):
    """v4+v6 双公网记录不许被误伤（把 v6 那条判错就会拒掉真上游）。"""
    providers = _stub(monkeypatch, "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")

    assert providers._base_url_reject_reason("https://good-model.example/v1") == ""


# ---------- M-4：对外文案不回显解析地址 / CIDR / host ----------

def test_provider_facing_reason_hides_resolved_address_cidr_and_host(monkeypatch):
    """普通用户能看到的拒因只能是类别：地址、CIDR、host 三者一个都不许出现。

    闸门仍先在内部拿详细理由（排障与未来审计要它），再经 public_reject_reason
    收成类别；把详细理由直接回显就是一台内网 DNS oracle。

    负向锁：把 providers 的返回值改回未公开化的 `blocked`，本条红。
    """
    providers = _stub(monkeypatch, "10.1.2.3")

    why = providers._base_url_reject_reason("https://intranet.corp.example")
    assert why, "解析到内网必须拒"
    assert "10.1.2.3" not in why, f"回显了解析地址：{why!r}"
    assert "10.0.0.0/8" not in why, f"回显了命中 CIDR：{why!r}"
    assert "intranet.corp.example" not in why, f"回显了主机名：{why!r}"
    assert "内网" in why and "不被允许" in why, why


def test_provider_facing_reason_still_allows_public_answers(monkeypatch):
    """公开化不许把放行改成拒绝：公网答案仍返回空串（=放行）。"""
    providers = _stub(monkeypatch, "93.184.216.34")

    assert providers._base_url_reject_reason("https://good-model.example") == ""


def test_provider_facing_reason_hides_zone_id_value():
    """zone-id 只报类别，`%eth0` 这类接口名与地址本身都不回显。"""
    from app.core import providers

    why = providers._base_url_reject_reason("http://[fe80::1%eth0]/v1")
    assert why, "zone-id 写法必须拒"
    assert "zone-id" in why, why
    assert "eth0" not in why and "fe80" not in why, f"回显了 zone-id 值/地址：{why!r}"


def test_detailed_reason_survives_in_net_guard_for_troubleshooting():
    """内部排障没被削掉：直接调 net_guard.reject_reason 仍点名地址与命中 CIDR。"""
    why = net_guard.reject_reason(
        "intranet.corp.example",
        resolve=lambda host: ["10.1.2.3"],
        allow_loopback=False,
        where="base_url",
    )
    assert "10.1.2.3" in why, why
    assert "10.0.0.0/8" in why, why
    assert "intranet.corp.example" in why, why


def test_public_reject_reason_is_a_pure_category_mapping():
    """纯函数口径：放行回空串，未知类别回通用文案，已知类别只由固定句子组成。"""
    assert net_guard.public_reject_reason("") == ""
    assert net_guard.public_reject_reason("别的东西") == net_guard.PUBLIC_REJECT_GENERIC
    assert net_guard.PUBLIC_REJECT_GENERIC == "该地址不被允许"
    public = net_guard.public_reject_reason(
        "base_url 的主机名 x 解析到内网地址 10.1.2.3（10.0.0.0/8），服务端不做内网请求")
    assert public == "该地址属于内网，不被允许"
    assert "10.1.2.3" not in public and "10.0.0.0/8" not in public


# ---------- 解析失败：fail closed ----------

@pytest.mark.parametrize("exc", [
    OSError(-2, "Name or service not known"),
    OSError(-5, "No address associated with hostname"),
    socket.gaierror("Temporary failure in name resolution"),
])
def test_unresolvable_name_is_rejected_not_allowed(monkeypatch, exc):
    """解析不出来 = 拒。放行它的代价是把"下一次连接去猜指向哪儿"留给运行时。

    负向锁：把 `except Exception` 那一段改成 `return ""`（当年那种"解析不了就算了"），
    本条 3 个参数全红。
    """
    providers = _stub(monkeypatch, raises=exc)

    why = providers._base_url_reject_reason("https://good-model.example/v1")
    assert isinstance(why, str), "必须返回一句字符串理由，不许是 None"
    assert why, f"解析失败居然放行了：{why!r}"
    assert "解析" in why, f"理由要说清是解析这一环出的问题：{why!r}"


def test_empty_resolution_result_is_rejected(monkeypatch):
    """解析"成功"但一条地址都没有：同样不许放行，形状不明的名字没法定罪也没法定无罪。

    负向锁：把 `if not addresses: return ...` 那两行删掉，本条红（空列表走 for
    循环一次都不进，最后 return "" 把它放行了）。
    """
    providers = _stub(monkeypatch)  # 零条记录

    why = providers._base_url_reject_reason("https://good-model.example/v1")
    assert why, "解析结果为空必须拒"
    assert "解析" in why, f"理由要说清是解析环节：{why!r}"


def test_resolver_returning_junk_is_rejected(monkeypatch):
    """解析返回一个不像地址的串（真 getaddrinfo 不该给，但桩/上游可能给）→ 拒。

    负向锁：把 `except ValueError` 那个分支改成 `continue`，本条红。
    """
    providers = _stub(monkeypatch, "not-an-ip")

    why = providers._base_url_reject_reason("https://good-model.example/v1")
    assert why, "无法识别的解析结果必须拒"


# ---------- 开关语义一位都不许动 ----------

def test_localhost_switch_releases_a_name_resolving_to_loopback(monkeypatch):
    """ALLOW_LOCALHOST_BASE_URL=True 时，解析到回环的**域名**也该被放行。

    这个开关从 v0.33 起管的就是"本机"这一类，而"名字解析到 127.0.0.1"与
    "直接写 127.0.0.1"是同一件事的两种写法（见 _base_url_reject_reason 里 IP
    字面量那一段的注释）。开关打开后只放行前者、拦住后者才是错的。

    负向锁：把域名那一支的回环判断写成"不看开关、一律拒"，本条红。
    """
    providers = _stub(monkeypatch, "127.0.0.1")
    assert providers._base_url_reject_reason("https://mine.example/v1")  # 默认状态是拒
    monkeypatch.setattr(providers, "ALLOW_LOCALHOST_BASE_URL", True)
    assert providers._base_url_reject_reason("https://mine.example/v1") == ""


def test_localhost_switch_still_does_not_release_private_answers(monkeypatch):
    """开关只管本机：解析到私网/链路本地/组播时，开关打开也照拒。

    这条是 v0.33 那条 `test_localhost_switch_does_not_release_the_other_private_ranges`
    的域名版——那边把 IP 字面量管住了，这一轮补的是名字这一支。

    负向锁：把开关那个 if 改成 `if True: continue`（提前跳过全部网段判断），本条红。
    """
    providers = _stub(monkeypatch, "10.0.0.5")
    monkeypatch.setattr(providers, "ALLOW_LOCALHOST_BASE_URL", True)
    why = providers._base_url_reject_reason("https://mine.example/v1")
    assert why, "开关打开不等于允许扫内网"
    assert "内网" in why and "10.0.0.5" not in why, why

    for literal in ("169.254.169.254", "192.168.1.1", "fe80::1"):
        _stub(monkeypatch, literal)
        assert providers._base_url_reject_reason("https://mine.example/v1"), literal


def test_localhost_name_branch_still_short_circuits_without_dns(monkeypatch):
    """`localhost`/`*.localhost` 这两个名字保持老行为：走开关、不去解析。

    刻意不把它们也塞进解析那一段：`.localhost` 按 RFC 6761 是特殊用途域，恒指回环，
    多问一次 DNS 只会让"开关打开就能存的自建模型"变成"还要看本机 hosts 有没有配"。
    这一条钉的是"别改过头"。

    负向锁：把 localhost 那一段挪到解析之后（或删掉那段提前返回），本条红——
    桩里那个会抛异常/记调用的解析器会被叫到。
    """
    from app.core import providers

    calls = []

    def _boom(host):
        calls.append(host)
        raise AssertionError("localhost 这一支被拖去解析了")

    monkeypatch.setattr(providers, "_resolve_addresses", _boom)
    assert providers._base_url_reject_reason("http://localhost:11434/v1")
    assert providers._base_url_reject_reason("http://api.localhost/v1")
    assert calls == [], f"localhost 这一支不该碰解析器：{calls}"

    monkeypatch.setattr(providers, "ALLOW_LOCALHOST_BASE_URL", True)
    assert providers._base_url_reject_reason("http://localhost:11434/v1") == ""
    assert calls == []


def test_ip_literal_hosts_never_reach_the_resolver(monkeypatch):
    """IP 字面量继续走原来那条快路：不再解析一次（否则是把 127.0.0.1 也送去查 DNS）。

    负向锁：在解析那段前面无条件调用 `_resolve_addresses`（把字面量也拖进解析），
    本条红。
    """
    from app.core import providers

    calls = []
    monkeypatch.setattr(providers, "_resolve_addresses",
                        lambda host: calls.append(host) or ["93.184.216.34"])
    assert providers._base_url_reject_reason("http://10.1.2.3/v1")
    assert providers._base_url_reject_reason("https://93.184.216.34/v1") == ""
    assert calls == [], f"字面量地址不该被送去解析：{calls}"


# ---------- 别牵连：这一轮只动了域名那一支 ----------

def test_no_hostname_cases_are_unchanged(monkeypatch):
    """没有主机名的空壳仍然是原来那句理由，且不该走到解析器。

    负向锁：把 `if not host: return "..."` 删掉或挪到解析之后，本条红。
    """
    providers = _stub(monkeypatch, "93.184.216.34")
    seen = []
    monkeypatch.setattr(providers, "_resolve_addresses",
                        lambda host: seen.append(host) or ["93.184.216.34"])

    for shell in ("http:///v1", "http://:80/v1"):
        why = providers._base_url_reject_reason(shell)
        assert why and "主机名" in why, f"{shell} 应当因缺主机名被拒：{why!r}"
    assert seen == [], f"空壳 URL 不该触发解析：{seen}"


def test_resolver_indirection_is_module_level_and_used(monkeypatch):
    """解析必须是模块级注入点，而不是藏在函数里的裸 getaddrinfo。

    存在的理由写在 `_resolve_addresses` 的注释里：整套测试不许真出网查 DNS，
    而"能不能被换掉"就是这件事唯一的实现前提。这里用 monkeypatch 换掉它并断言
    被调用——如果哪天有人把它内联回 `_base_url_reject_reason`（socket.getaddrinfo
    直接写进判据里），本条立刻红，测试就会开始真打 DNS 并随网络状态乱红。

    负向锁：把 `_base_url_reject_reason` 里的调用改成 `socket.getaddrinfo(...)`，
    本条红（桩没被叫到）。
    """
    providers = _stub(monkeypatch, "93.184.216.34")
    seen = []

    def spy(host):
        seen.append(host)
        return ["93.184.216.34"]

    monkeypatch.setattr(providers, "_resolve_addresses", spy)
    assert providers._base_url_reject_reason("https://good-model.example/v1") == ""
    assert seen == ["good-model.example"], f"解析注入点没被用上：{seen}"


def test_toctou_window_is_documented_not_closed():
    """把"这一轮没关掉重新解析的窗口"钉成一条测试，防止下一个人在文档里吹圆。

    判据是源码形状：`build_client` 直接把记录里的 `base_url`（一个**名字**）交给
    SDK，中间没有任何"钉住已校验 IP"的动作；因此写入时验过的地址与连接时用的
    地址之间隔着第二次解析。真要关闭它需要改客户端构造路径（见本文件顶部）。

    负向锁：如果哪天连接层真的把 IP 钉住了（base_url 在出站前被换成校验过的
    字面量），本条会红——那时该做的不是删掉这条，而是把它改成钉"必须钉住"。
    """
    import inspect

    from app.core import providers

    src = inspect.getsource(providers.build_client)
    assert 'base_url=provider["base_url"]' in src, (
        "build_client 的形状变了：请确认 TOCTOU 是否已关，并同步本文件与 "
        "_base_url_reject_reason 的注释——注释现在写的是「仍然有窗口」")
