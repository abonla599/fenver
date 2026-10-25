"""v0.37.4 单源出站判据锁（T1 最小版）。

v0.37.4 的安全故事只有一句：`http_fetch` 与 `web_read` 共用**同一份**出站判据。
最便宜的事故形状是有人为了"就多一个参数"把 `contract_reject_reason` 或 DNS
解析复制进调用方——两份判据从复制那一秒开始各自腐烂。这里用 AST/源码扫描钉住
两条最小事实：

1. `def contract_reject_reason` 在 `app/tools/` 下只有 `fetch_core.py` 一份；
2. `getaddrinfo` 在 `app/tools/` 下只出现在 `fetch_core.py`，且只有一行。

扫描器本身配负控：喂一段合成副本必须报红，否则"取不到就静默绿"的锁没有牙。
"""
import ast
from pathlib import Path

import pytest

from app.core import net_guard

BACKEND = Path(__file__).resolve().parent.parent
TOOLS = BACKEND / "app" / "tools"


def _tool_sources() -> dict:
    return {path.name: path.read_text(encoding="utf-8")
            for path in sorted(TOOLS.glob("*.py"))}


def _function_def_lines(source: str, name: str) -> list:
    """AST 找同名函数定义的行号（不只认 `def` 文本，防注释/字符串误报）。"""
    tree = ast.parse(source)
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name]


def _lines_containing(source: str, needle: str) -> list:
    return [number for number, line in enumerate(source.splitlines(), 1)
            if needle in line]


def test_contract_reject_reason_is_defined_only_in_fetch_core():
    hits = {name: lines for name, source in _tool_sources().items()
            if (lines := _function_def_lines(source, "contract_reject_reason"))}
    assert set(hits) == {"fetch_core.py"}, \
        f"contract_reject_reason 出现了第二份定义：{hits}"
    http_source = (TOOLS / "http_fetch.py").read_text(encoding="utf-8")
    assert "def contract_reject_reason" not in http_source, \
        "http_fetch 不许自己定义契约判据：只能从 fetch_core import/re-export"
    assert "contract_reject_reason" in http_source, \
        "http_fetch 应保留 contract_reject_reason 的 re-export"


def test_getaddrinfo_appears_only_in_fetch_core():
    hits = {}
    for name, source in _tool_sources().items():
        lines = _lines_containing(source, "getaddrinfo")
        if lines:
            hits[name] = lines
    assert set(hits) == {"fetch_core.py"}, \
        f"DNS 解析器在 fetch_core 之外出现：{ {k: len(v) for k, v in hits.items()} }"
    assert len(hits["fetch_core.py"]) == 1, \
        f"fetch_core 里的 getaddrinfo 应只有一处调用点，实际行号：{hits['fetch_core.py']}"


def test_scanners_have_teeth():
    """负控：合成一份"复制到调用方"的源码，两条扫描都必须报红。"""
    duplicated = ("def contract_reject_reason(url):\n"
                  "    return ''\n\n"
                  "resolve = socket.getaddrinfo\n")
    assert _function_def_lines(duplicated, "contract_reject_reason") == [1]
    assert _lines_containing(duplicated, "getaddrinfo") == [4]
    assert _function_def_lines("def other():\n    pass\n",
                               "contract_reject_reason") == []
    assert _lines_containing("def other():\n    pass\n", "getaddrinfo") == []


def _imported_modules(source: str) -> set:
    """AST 取 import 的完整模块名，包含 `from ... import name` 与相对导入。

    只取顶层根不够：`from app.core import net_guard` 的根是 `app`，
    `from ..core import net_guard` 的根连 `app` 都不是；两种写法都会把第二份
    出站判据带进来而骗过只看根的扫描。
    """
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base:
                modules.add(base)
            for alias in node.names:
                modules.add(f"{base}.{alias.name}" if base else alias.name)
    return modules


def test_web_read_never_imports_httpx_or_a_second_egress_gate():
    """`web_read` 只选目的地、不自己发请求：它不该碰 httpx/net_guard/ipaddress。

    负控：把 `import httpx`、`from app.core import net_guard`、
    `from ..core import net_guard` 塞进合成源码，扫描器都必须报红。
    """
    source = (TOOLS / "web_read.py").read_text(encoding="utf-8")
    modules = _imported_modules(source)
    assert not any(m == "httpx" or m.startswith("httpx.") for m in modules), \
        "web_read 自己发请求就是第二份出站面"
    assert not any(m == "net_guard" or m.endswith(".net_guard") for m in modules), \
        "出站地址判据只许 fetch_core 持有"
    assert "app.core.net_guard" not in source, "出站地址判据只许 fetch_core 持有"
    assert "getaddrinfo" not in source, "DNS 解析只许 fetch_core 持有"
    assert "ipaddress" not in source, "地址判据只许 net_guard/fetch_core 持有"
    assert any(m.endswith(".net_guard") for m in _imported_modules(
        "from app.core import net_guard\n")), "扫描器没有牙（绝对 import）"
    assert any(m.endswith(".net_guard") for m in _imported_modules(
        "from ..core import net_guard\n")), "扫描器没有牙（相对 import）"
    assert "httpx" in _imported_modules("import httpx\n"), "扫描器没有牙（httpx）"


def test_fetch_core_refuses_a_missing_policy():
    """policy=None 是"忘了传"，不是"全放行"；必须在任何解析之前炸。"""
    from app.tools import fetch_core

    with pytest.raises(TypeError):
        fetch_core.fetch("https://e.example/a", policy=None)


# ---------- 3. 间接 import 形状：两跳 / 星号 / 动态导入 ----------
#
# 审查 D2 实测：`from app import core` + `core.net_guard...`、`from app.core import *`
# + `net_guard...`、`importlib.import_module("app.core." + "net_guard")` 三种写法都能
# 骗过只解析 `ImportFrom.module` 的扫描器。`web_read` 自己发请求或自建判据都是
# "第二份出站面"，这里把这三条路一起收口。

_DYNAMIC_ENTRY_NAMES = frozenset({"importlib", "__import__", "eval", "exec", "compile"})


def _egress_name_escapes(source: str) -> list:
    """扫描 `web_read` 可能自建第二份出站判据的名字形状，返回 `[(kind, line)]`。

    正面写明认得的形状：
    1. 源码文本里出现 `net_guard`（含注释、字符串、两跳 `from app import core`
       之后的 `core.net_guard`，以及 `from app.core import *` 之后直接用的名字）；
    2. AST 里任何叫 `importlib` / `__import__` / `eval` / `exec` / `compile` 的
       `Name`、`Attribute` 或 import alias——`web_read` 没有任何动态导入/动态执行
       的正当用途，出现即拒；
    3. `from x import *`——星号导入把哪些名字带进命名空间读不出来，一律拒。

    **射程到此为止**：完全不出现 `net_guard` 字面、也不用上述动态入口，纯靠
    `globals()`/`getattr()` 在运行时拼出第二份判据的写法不在本锁射程内（它需要
    先拿到 `globals`/`builtins`；`web_read` 当前源码里没有这类调用，见下一条用例
    明写的残余）。这条残余不静默：它被单独钉成一条用例。
    """
    hits = []
    if "net_guard" in source:
        hits.append(("net_guard-literal", 0))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _DYNAMIC_ENTRY_NAMES:
            hits.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute) and node.attr in _DYNAMIC_ENTRY_NAMES:
            hits.append((node.attr, node.lineno))
        elif isinstance(node, ast.alias) and node.name.split(".")[0] in _DYNAMIC_ENTRY_NAMES:
            hits.append((node.name, node.lineno))
        elif isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            hits.append(("star-import", node.lineno))
    return hits


def test_web_read_has_no_indirect_egress_gate_in_any_import_shape():
    source = (TOOLS / "web_read.py").read_text(encoding="utf-8")
    assert _egress_name_escapes(source) == [], \
        f"web_read 出现了第二份出站判据/动态入口：{_egress_name_escapes(source)}"
    # 负控：三种已知逃逸形状都必须报红，否则这条锁只会"取不到就静默绿"。
    assert _egress_name_escapes(
        "from app import core\ncore.net_guard.reject_reason('x', resolve=None)\n")
    assert _egress_name_escapes(
        "from app.core import *\nnet_guard.reject_reason('x', resolve=None)\n")
    assert _egress_name_escapes(
        "import importlib\nm = importlib.import_module('app.core.' + 'net_guard')\n")
    # 动态入口即使不带 net_guard 字样也要拒：拼串绕开文本扫描的那一半由它兜住。
    assert _egress_name_escapes(
        "import importlib\nm = importlib.import_module('app.core.net' + '_guard')\n")
    assert _egress_name_escapes(
        "m = __import__('app.core.net' + '_guard')\n")
    assert _egress_name_escapes("from app.tools import fetch_core\n") == []


def test_the_lock_scope_ends_at_fully_runtime_name_construction():
    """明写射程：完全运行时拼名不在锁内；当前源码也没有拼名所需的入口。

    这不是"漏测"而是"写清边界"：本锁的承诺是"静态可读的 import 形状 + 动态导入
    入口"，不是"能挡住任何 Python 反射"。要让残余变成可执行路径，先得在
    `web_read` 里拿到 `globals`/`builtins`/`getattr` 一类名字；当前源码一个都没有。
    """
    residual = "m = globals()['net_' + 'guard']\n"
    assert _egress_name_escapes(residual) == [], "射程被悄悄扩大了"
    source = (TOOLS / "web_read.py").read_text(encoding="utf-8")
    for entry in ("globals(", "getattr(", "builtins"):
        assert entry not in source, f"web_read 出现了运行时拼名入口：{entry}"


# ---------- 4. local-use NAT64（64:ff9b:1::/48）的边界负控 ----------

def _no_dns(_host):
    raise AssertionError("IP 字面量不该触发 DNS")


@pytest.mark.parametrize("literal", [
    "64:ff9b:1::",                        # /48 的基址
    "64:ff9b:1::7f00:1",                  # 内嵌 127.0.0.1
    "64:ff9b:1::a9fe:a9fe",               # 内嵌 169.254.169.254
    "64:ff9b:1:ffff:ffff:ffff:ffff:ffff", # /48 内最远端
])
def test_local_use_nat64_prefix_is_rejected(literal):
    reason = net_guard.reject_reason(literal, resolve=_no_dns,
                                     allow_loopback=False, where="url",
                                     loopback_hint="")
    assert "64:ff9b:1::/48" in reason, f"{literal} 没被 /48 拦下：{reason!r}"


@pytest.mark.parametrize("literal", [
    "64:ff9b::808:808",     # 知名 NAT64 前缀、内嵌 8.8.8.8：既有放行不变
    "64:ff9b:2::808:808",   # /48 之外
    "64:ff9b:ffff::1",      # /48 之外的另一端
])
def test_nat64_outside_the_local_use_prefix_is_allowed(literal):
    assert net_guard.reject_reason(literal, resolve=_no_dns,
                                   allow_loopback=False, where="url",
                                   loopback_hint="") == "", \
        f"{literal} 被误伤了：放行集被扩到了 /48 之外"
