"""出站 trust_env 名册锁：生产出站的每个 httpx 客户端都必须关掉环境代理读取。

判据全部是 AST 的 keyword.arg 判定，不是子串匹配：`trust_env` / `http_client`
这些词出现在注释或字符串字面量里**不算数**。子串判据会把这类锁变成假绿——
旧锁 `test_providers.py::test_no_openai_client_is_built_without_an_explicit_http_client`
管的是「每处 OpenAI 构造必带 http_client」（TLS 信任锚），与本锁「每个客户端必带
trust_env=False」（环境代理）是两个不相干的谓词，两把并存、互相引用；旧锁那侧的
子串盲区已另登 #296，本锁不重复兑现那边的债。

为什么 trust_env=False 是承重的：httpx 在 **trust_env=True 的构造期**就解析
NO_PROXY 等环境代理（不发请求也会崩）。带方括号的 `[::1]` 会让解析落进
`all://*[::1]` 分支、构造期抛 InvalidURL（机理见 httpx/_utils.py 的
`_parse_no_proxy` 一族，行为反例与活性阳性对照钉在
`test_embed_key_source.py::test_bracket_no_proxy_poison_does_not_degrade_cloud_embed`）。
生产客户端一旦被这种毒打崩，各站点的 except 降级分支会把异常吞成静默故障：
嵌入降级伪嵌入、APK 取包失败、搜索回空。

射程声明（与实测一致，由下面的射程锁兑现，不一致即报红）：
- 只扫 `backend/app/` 下 httpx.Client / httpx.AsyncClient 的构造点，含
  `import httpx`、`import httpx as 别名`、`from httpx import Client` 三种绑定
  形状；覆盖形状两种：调用上直接 `trust_env=False`，或经
  `**client_kwargs(...)` 展开（其返回值由运行时断言钉住为 False）。
- `backend/tests/` 的构造点豁免。机理：httpx/_client.py:685（同步）与
  httpx/_client.py:1399（异步）`allow_env_proxies = trust_env and transport is
  None`——显式传了 transport 的客户端根本不读环境代理，对毒 NO_PROXY 免疫；
  实测当前测试构造点全部带 transport=，豁免名单以该机理为凭而不是以文件位置为凭，
  任何**不带 transport** 的新测试构造点出现时上面的行为反例与毒环境全量运行会先红。
- urllib 出站（core/releases.py 的 _fetch_repo）不在本锁射程：本锁只数 httpx 客户端
  构造点。它已改成显式 build_opener(ProxyHandler({}), HTTPSHandler(context=...))，
  不再读环境/注册表代理（#297 已落地）；那条性质由 test_release_probe.py 与
  test_v024_foundation_contract.py 两把 opener 形状锁钉着，不在本 httpx 名册里重复登记。
- 不传 http_client 时 openai SDK 自建的客户端默认读环境代理，所以本锁同时钉
  每一处 `OpenAI(...)` / `AsyncOpenAI(...)` 构造点：必须显式传 http_client，
  且所传客户端必须是名册里已覆盖的站点（防未来冒出一处隐式客户端）。

仍漏清单（声明与实测一致，判据对这些形状是盲的）：
- 动态派发：`getattr(httpx, "Cli" + "ent")(...)`、把 Client 当值传递再调用等
  非字面构造形状。
- `**` 展开的 dict 来源不是 client_kwargs 风格 helper：那会落进「未登记」方向
  报红（好过静默），但 dict 的赋值流不追踪。
- `.py` 之外（模板、动态 exec 源码）的构造。
"""
import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REPO_ROOT = Path(__file__).resolve().parents[2]

# 手抄名册（射程锁不许它等于扫描派生，见
# test_the_rosters_are_hand_copied_literals_not_derived）。
# 键 = (仓库相对路径 posix, 宿主 qualname, 该宿主内第几次构造)，值 = 覆盖形状。
# 新增/删除站点必须人肉改这张表，射程锁会盯住扫描结果与它的双向差。
_CLIENT_SITES = {
    ("backend/app/core/mcp_client.py", "MCPClient.__init__", 0): "direct-false",
    ("backend/app/core/providers.py", "build_client", 0): "direct-false",
    ("backend/app/core/releases.py", "_open_asset", 0): "direct-false",
    ("backend/app/memory/memory_manager.py", "MemoryManager.__init__", 0): "direct-false",
    ("backend/app/tools/fetch_core.py", "make_client", 0): "unpack:client_kwargs",
    ("backend/app/tools/search_api_sources.py", "_ask", 0): "direct-false",
    ("backend/app/tools/web_search.py", "search", 0): "direct-false",
}

_OPENAI_SITES = {
    ("backend/app/memory/memory_manager.py", "MemoryManager.__init__", 0): "bound-client",
    ("backend/app/core/providers.py", "build_client", 0): "bound-client",
}

_ROSTER_SIZES = (("CLIENT", 7), ("OPENAI", 2))

_ROSTER_MUTATORS = ("update", "setdefault", "pop", "popitem", "clear", "discard", "add")

_CLIENT_METHODS = frozenset({"Client", "AsyncClient"})
_OPENAI_METHODS = frozenset({"OpenAI", "AsyncOpenAI"})


def _app_python_files():
    import app as app_pkg

    root = Path(app_pkg.__file__).parent
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(p: Path) -> str:
    return p.resolve().relative_to(_REPO_ROOT).as_posix()


def _bound_names(tree):
    """返回 (httpx 模块别名集合, 直接绑定的客户端名->方法名, openai 模块别名, openai 类名绑定)。"""
    hx_bases = set()
    client_direct = {}
    oa_bases = set()
    openai_direct = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "httpx":
                    hx_bases.add(alias.asname or "httpx")
                elif alias.name == "openai":
                    oa_bases.add(alias.asname or "openai")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "httpx":
                for alias in node.names:
                    if alias.name in _CLIENT_METHODS:
                        client_direct[alias.asname or alias.name] = alias.name
            elif node.module == "openai":
                for alias in node.names:
                    if alias.name in _OPENAI_METHODS:
                        openai_direct[alias.asname or alias.name] = alias.name
    return hx_bases, client_direct, oa_bases, openai_direct


def _nodes_with_qualname(tree):
    """yield (宿主 def/class 栈, 任意节点)。"""

    def walk(node, stack):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stack = stack + [node.name]
        yield tuple(stack), node
        for child in ast.iter_child_nodes(node):
            yield from walk(child, stack)

    yield from walk(tree, [])


def _callsites_with_qualname(tree):
    """yield (宿主 qualname 栈, Call 节点)，宿主按 def/class 栈拼名字。"""
    for stack, node in _nodes_with_qualname(tree):
        if isinstance(node, ast.Call):
            yield stack, node


def _client_shape(call):
    """调用点的覆盖形状：direct-false / unpack:client_kwargs / 其它如实报。"""
    for kw in call.keywords:
        if kw.arg == "trust_env":
            if isinstance(kw.value, ast.Constant) and kw.value.value is False:
                return "direct-false"
            return "direct-" + ast.dump(kw.value)[:40]
    for kw in call.keywords:
        if kw.arg is None:
            func = kw.value.func if isinstance(kw.value, ast.Call) else None
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "client_kwargs":
                return "unpack:client_kwargs"
    return "uncovered"


def _scan_all():
    """单次 parse 同时产出两册现扫结果与共享节点表（跨 parse 的节点身份不可比）。

    返回 (client_shapes, client_nodes, openai_shapes, openai_nodes, trees)。
    """
    client_shapes, client_nodes = {}, {}
    openai_shapes, openai_nodes = {}, {}
    trees = {}
    for path in _app_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        trees[_rel(path)] = tree
        hx_bases, client_direct, oa_bases, openai_direct = _bound_names(tree)
        per_host_client, per_host_openai = {}, {}
        for stack, call in _callsites_with_qualname(tree):
            qual = ".".join(stack)
            func = call.func
            method = None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
                    and func.value.id in hx_bases and func.attr in _CLIENT_METHODS:
                method = func.attr
            elif isinstance(func, ast.Name) and func.id in client_direct:
                method = client_direct[func.id]
            if method is not None:
                idx = per_host_client.get(qual, 0)
                per_host_client[qual] = idx + 1
                key = (_rel(path), qual, idx)
                client_shapes[key] = _client_shape(call)
                client_nodes[key] = call
                continue
            hit = (isinstance(func, ast.Name) and func.id in openai_direct) or \
                  (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                   and func.value.id in oa_bases and func.attr in _OPENAI_METHODS)
            if hit:
                idx = per_host_openai.get(qual, 0)
                per_host_openai[qual] = idx + 1
                key = (_rel(path), qual, idx)
                openai_shapes[key] = call
                openai_nodes[key] = call
    return client_shapes, client_nodes, openai_shapes, openai_nodes, trees


def _scan_client_sites():
    return _scan_all()[0], _scan_all()[1]


def _scan_openai_sites():
    return _scan_all()[2]


def _roster_diff_reported(scanned, roster, label):
    """三方向分开报：在册文件里新长出的 / 新文件 / 在册却扫不到。"""
    new = scanned.keys() - roster.keys()
    new_in_registered_file = sorted(k for k in new if k[0] in {s[0] for s in roster})
    new_file = sorted({k[0] for k in new if k[0] not in {s[0] for s in roster}})
    stale = sorted(roster.keys() - scanned.keys())
    assert not new_in_registered_file, (
        f"{label} 名册所在文件里冒出没登记的构造点 {new_in_registered_file}：新增一处出站"
        f"客户端就把它人肉登记进名册（带覆盖形状），别指望锁认得你的字典。")
    assert not new_file, (
        f"{label} 现扫到名册之外的文件 {new_file}：要么新站没登记，要么有人把名册行摘短了"
        "而构造点还在原地——两种都正面回来把那一行加回去。")
    assert not stale, (
        f"{label} 名册登记了、现扫点不到 {stale}：那一站的构造被删、改名或换成了判据认不到的"
        "形状（见模块 docstring 仍漏清单）。真删了就连名册一起删，换形状就改判据。")


def _literal_only(node):
    """名册右侧只许是字面量容器（键为 str/str/int 元组、值为 str 常量）。"""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, int))
    if isinstance(node, ast.Tuple):
        return all(_literal_only(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(_literal_only(k) and _literal_only(v)
                   for k, v in zip(node.keys, node.values))
    if isinstance(node, (ast.Set, ast.List)):
        return all(_literal_only(e) for e in node.elts)
    return False


def _roster_assignments(name):
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"), filename=__file__)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            hits.append((node, name))
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            hits.append((node, name))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Attribute) \
                and isinstance(node.value.func.value, ast.Name) \
                and node.value.func.value.id == name \
                and node.value.func.attr in _ROSTER_MUTATORS:
            hits.append((node, name))
    return hits


# ---------- 条数钉 ----------

@pytest.mark.parametrize("label,size", _ROSTER_SIZES)
def test_rosters_are_pinned_at_measured_sizes(label, size):
    """条数钉：名册恰好 N 条且 N>0——扫描器空转、名册被掏空都会在这一格撞数字。"""
    roster = _CLIENT_SITES if label == "CLIENT" else _OPENAI_SITES
    assert len(roster) == size, (
        f"{label} 名册条数是 {len(roster)}，钉的是 {size}：改站点必须同时改这一格数字，"
        "数字对不上说明有一边被悄悄动了。")
    scanned = _scan_client_sites()[0] if label == "CLIENT" else _scan_openai_sites()
    assert len(scanned) == size, (
        f"{label} 现扫结果 {len(scanned)} 条，名册 {size} 条：条数不等时射程锁也会红，"
        "这一格是它的前置——先报条数再报方向。")


# ---------- 射程锁（三方向） ----------

def test_client_roster_is_the_whole_scanned_range():
    """名册==现扫：backend/app/ 里每一处 httpx 客户端构造都必须在册，反之亦然。"""
    scanned, _ = _scan_client_sites()
    assert scanned, "现扫一无所获：判据或射程被改坏，名册无从对照（扫描器空转即红）。"
    _roster_diff_reported(scanned, _CLIENT_SITES, "CLIENT")


def test_openai_roster_is_the_whole_scanned_range():
    _roster_diff_reported(_scan_openai_sites(), _OPENAI_SITES, "OPENAI")


# ---------- 逐站点覆盖 ----------

def test_every_registered_client_site_closes_trust_env():
    """在册站点逐个核对覆盖形状：直接 trust_env=False，或经 client_kwargs 展开。"""
    scanned, _ = _scan_client_sites()
    offenders = []
    for key, shape in sorted(_CLIENT_SITES.items()):
        got = scanned.get(key)
        if got != shape:
            offenders.append((key, f"名册期望 {shape}", f"实扫 {got!r}"))
    assert not offenders, (
        "以下站点的 trust_env 覆盖与名册不符（摘掉一处 trust_env=False 就红在这里）："
        + str(offenders))


def test_fetch_core_client_kwargs_closes_trust_env_at_runtime():
    """fetch_core 的构造点靠 **client_kwargs(...) 展开覆盖：AST 看不到值，运行时钉。"""
    from app.tools.fetch_core import client_kwargs

    kwargs = client_kwargs()
    assert kwargs.get("trust_env") is False, (
        f"client_kwargs() 返回的 dict 不再含 trust_env=False（实得 {kwargs!r}）："
        "fetch_core 的构造点靠这份展开免疫毒 NO_PROXY，掏空它等于重开事故缺口。")


def test_build_client_feeds_trust_env_false_at_runtime():
    """providers 的 OpenAI 靠 build_client 内部客户端覆盖：与名册互为两腿。

    与 test_providers.py::test_build_client_feeds_that_context_to_httpx 并存——那把管
    信任锚接线，这把管环境代理接线；摘掉 build_client 里那枚 trust_env=False 时，
    名册（AST 腿）与这里（运行时腿）都会红。
    """
    from app.core import providers

    seen = {}

    class Recorder(Exception):
        pass

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            raise Recorder()

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(providers.httpx, "Client", FakeClient)
        # 解析在构造之前，桩掉它：本锁零出网，DNS 也不真打。返回 TEST-NET 地址，
        # 判闸不参与（整个 _resolve_and_check 被替）。
        monkey.setattr(providers, "_resolve_and_check",
                       lambda url: ("api.invalid.example", ["203.0.113.7"], None))
        providers.invalidate_client_cache()
        with pytest.raises(Recorder):
            providers.build_client({"id": "trustenv-lock", "api_key": "sk-x",
                                    "base_url": "https://api.invalid.example"})
    finally:
        providers.invalidate_client_cache()
        monkey.undo()
    assert seen.get("trust_env") is False, (
        f"build_client 构造 httpx 客户端时没关环境代理（实得 {seen!r}）："
        "毒 NO_PROXY 会让它在构造期崩，聊天面整条断。")


def test_openai_sites_bind_a_rostered_http_client():
    """每处 OpenAI 构造：显式 http_client kwarg（AST keyword.arg，非子串），且所传
    客户端解析到在册已覆盖的构造点——防「传了 http_client 但那客户端没关 trust_env」
    与「新冒出一处不传 http_client 的隐式构造」两种形状。"""
    scanned, nodes, _, openai_nodes, trees = _scan_all()
    offenders = []
    for key in sorted(openai_nodes):
        rel, qual, _idx = key
        call = openai_nodes[key]
        if key not in _OPENAI_SITES:
            offenders.append((key, "不在 OPENAI 名册（射程锁已红，这里只是陪跑）"))
            continue
        kwargs = [kw for kw in call.keywords if kw.arg == "http_client"]
        if not kwargs:
            offenders.append((key, "OpenAI 构造没显式传 http_client"))
            continue
        value = kwargs[0].value
        bound = None
        if isinstance(value, ast.Call):
            # 内联构造：http_client=httpx.Client(...)，节点就是名册里那一条。
            for site_key, site_node in nodes.items():
                if site_node is value:
                    bound = site_key
                    break
        elif isinstance(value, ast.Name):
            for host, node in _nodes_with_qualname(trees[rel]):
                if ".".join(host) != qual or not isinstance(node, ast.Assign):
                    continue
                if any(isinstance(t, ast.Name) and t.id == value.id for t in node.targets) \
                        and isinstance(node.value, ast.Call):
                    for site_key, site_node in nodes.items():
                        if site_node is node.value:
                            bound = site_key
                            break
        if bound is None:
            offenders.append((key, "http_client 传的不是可解析到名册的客户端构造"))
        elif scanned.get(bound) not in ("direct-false", "unpack:client_kwargs"):
            offenders.append((key, f"所传客户端 {bound} 的覆盖形状是 {scanned.get(bound)!r}"))
    assert not offenders, "OpenAI 接线不合名册：" + str(offenders)


# ---------- 反派生守卫 ----------

@pytest.mark.parametrize("roster_name", ("_CLIENT_SITES", "_OPENAI_SITES"))
def test_the_rosters_are_hand_copied_literals_not_derived(roster_name):
    """名册右侧写成扫描结果（Call/Name/推导式）⇒ 自比恒真 ⇒ 这一格红。

    「每一条都是人登记的」是射程锁有意义的全部前提：派生出来的名册等于让扫描器
    跟自己比。第二处赋值或原地 .update() 同样失真。
    """
    hits = _roster_assignments(roster_name)
    assert len(hits) == 1, (
        f"`{roster_name}` 在本文件被赋值/原地改写了 {len(hits)} 处（行号 "
        f"{sorted({n.lineno for n, _ in hits})}）：只许人手写一次。")
    node = hits[0][0]
    assert isinstance(node, ast.Assign), (
        f"`{roster_name}` 不是裸赋值（{type(node).__name__}）：带注解/增强的写法会绕开"
        "右侧形状检查，先恢复成 NAME = ...。")
    assert all(isinstance(t, ast.Name) and t.id == roster_name for t in node.targets)
    assert _literal_only(node.value), (
        f"`{roster_name}` 右侧不是纯字面量容器（{type(node.value).__name__}）：Call、"
        "Name、推导式、* / ** 展开一律红——派生的名册恒真，不含「人登记过」的信息。")
