"""v0.37.0 隔离锁：http_fetch 与沙箱/providers 必须是三张独立的面孔。

这一版把"服务端能出网"从两条窄路（provider base_url、搜索源）扩成了三条，
最贵的事故形状是**边界在某个文件里被悄悄焊穿**：http_fetch  import 了 providers
就共享了那枚 ALLOW_LOCALHOST_BASE_URL 开关（fetch 永远不许吃它）；import 了
sandbox 就等于给"沙箱容器无网络"这条隔离补一个带 URL 的旁路。文件名和 import
行是最容易被"顺手复用一下"改掉的东西，所以钉成 AST。

四把锁：
1. `http_fetch.py` 的 import 面（AST 遍历 Import/ImportFrom，正面写明认得的形状：
   绝对导入、`from app.x import y` 都算）里不许出现 `app.sandbox*` 或
   `app.core.providers`；源码文本不许出现 ALLOW_LOCALHOST；
2. `sandbox_manager.py` 里 `*.containers.create` 调用上的 `network_disabled=True`——
   AST 找调用、只在该调用自己的 keywords 里断言；行号不参与判据（v0.37.0 审查
   N12：硬编码行号是会腐烂的散文坐标，上方插一行注释不该让锁红）；
3. execute_code 的 schema 参数恰为 {code, language}、http_fetch 恰为 {url}——
   参数表是模型唯一能碰的契约，多一颗口子（比如 user_id 或 base_url）就是新通道；
4. http_fetch 注册旗标：needs_user=True（审计归因走服务端身份）、
   requires_approval=False、suspends=False，schema 里绝不出现 user_id。

用法：../venv/Scripts/python.exe -m pytest tests/test_v0370_isolation_locks.py -q
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.tools.registry import tools_registry

BACKEND = Path(__file__).resolve().parent.parent
HTTP_FETCH_SRC = (BACKEND / "app" / "tools" / "http_fetch.py").read_text(encoding="utf-8")
SANDBOX_SRC = (BACKEND / "app" / "sandbox" / "sandbox_manager.py").read_text(encoding="utf-8")

_FORBIDDEN_ROOTS = ("app.sandbox", "app.core.providers")


def _imported_modules(source: str, module: str = "app.tools.http_fetch") -> set:
    """AST 认得的形状：`import a.b[ as c]`、`import a.b, d.e`、
    `from a.b import x, y`（每个 alias 与 module 拼接），相对导入按 `level`
    折算回 `app.*` 前缀。

    为什么不能只收 `ImportFrom.module`：`from app.core import providers` 的
    `module` 是 `app.core`，真正被引用的名字在 alias 里；`from .. import sandbox`
    连 module 都是空的。v0.37.0 审查实测这四种形状全部漏网：
    `from app.core import providers`、`from app import sandbox`、
    `from ..core import providers`、`from .. import sandbox`。
    条件导入（函数体里的 import）同样在射程内——遍历整棵语法树，不限顶层。
    """
    package = module.split(".")[:-1]          # app.tools.http_fetch -> ["app","tools"]
    tree = ast.parse(source)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from . import x`（level=1）落在当前包；`from .. import x`
                # （level=2）上一级。够不着的层数按 app 收，宁可多看一层。
                drop = node.level - 1
                base = package[:max(1, len(package) - drop)] if package else ["app"]
                if node.module:
                    base = base + node.module.split(".")
            else:
                base = node.module.split(".") if node.module else []
            if base:
                found.add(".".join(base))
            for alias in node.names:
                if alias.name == "*":
                    continue
                if base:
                    found.add(".".join([*base, alias.name]))
                else:
                    found.add(alias.name)
    return found


@pytest.mark.parametrize("forbidden", _FORBIDDEN_ROOTS)
def test_http_fetch_never_imports_the_other_two_surfaces(forbidden):
    mods = _imported_modules(HTTP_FETCH_SRC)
    hit = [m for m in mods if m == forbidden or m.startswith(forbidden + ".")]
    assert not hit, f"http_fetch 焊穿了隔离面：import 了 {hit}"


def test_http_fetch_source_has_no_localhost_switch():
    """providers 那枚 ALLOW_LOCALHOST_BASE_URL 不许以任何形式被搬进/引用进 fetch 源码。

    fetch 调 net_guard 时 `allow_loopback=False` 是写死的字面量（判据见契约测试
    "名单不是免检通道"一组），这里钉的是另一半：连开关名字都不许出现，防止
    "从别处读开关再传进来"的二手借用。
    """
    assert "ALLOW_LOCALHOST" not in HTTP_FETCH_SRC
    assert "allow_loopback=False" in HTTP_FETCH_SRC


def _dotted_call_name(node) -> str:
    """`self.client.containers.create` -> "self.client.containers.create"。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _container_create_calls(source: str) -> list:
    """AST 找 `*.containers.create(...)` 调用（不再锚行号）。"""
    calls = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = _dotted_call_name(node.func)
            if name == "containers.create" or name.endswith(".containers.create"):
                calls.append(node)
    return calls


def _network_disabled_true(call) -> list:
    """只在该 `containers.create` 调用自己的 keywords 里找 `network_disabled=True`。"""
    return [kw for kw in call.keywords
            if kw.arg == "network_disabled"
            and isinstance(kw.value, ast.Constant) and kw.value.value is True]


def _sandbox_lock_holds(source: str) -> bool:
    """判据本体：至少一个 containers.create 调用带着 network_disabled=True。"""
    return any(_network_disabled_true(call) for call in _container_create_calls(source))


def test_sandbox_container_create_keeps_network_disabled():
    """AST 找 docker 容器 create 调用上的 `network_disabled=True`。

    判据是"这个 kwargs 必须挂在那次 create 调用上且值为 True"；行号只是观测值，
    只在失败信息里出现——上方插一行注释不该让这条锁红（v0.37.0 审查 N12）。
    """
    calls = _container_create_calls(SANDBOX_SRC)
    assert calls, "sandbox_manager 里找不到 *.containers.create 调用——沙箱隔离被拆了"
    hits = [(call, kw) for call in calls for kw in _network_disabled_true(call)]
    assert hits, ("sandbox_manager 的 containers.create 上找不到 network_disabled=True——"
                  f"沙箱隔离被拆了（create 调用在第 "
                  f"{', '.join(str(c.lineno) for c in calls)} 行）")


def test_sandbox_lock_survives_a_line_inserted_above():
    """行号不是判据：上方插一行（注释/空行）后同一条锁照样成立。"""
    mutated = "# 上方随手插一行注释\n" + SANDBOX_SRC
    assert _sandbox_lock_holds(mutated), "插一行就让锁红了：判据还在锚行号"


def test_sandbox_lock_ignores_network_disabled_on_other_calls():
    """`network_disabled=True` 挂在别的调用（run/其它对象）上不算数。

    负向锁：把 `_container_create_calls` 放宽成"任意带该关键字的 Call"，本条红。
    """
    source = (
        "def f(self):\n"
        "    self.client.containers.create(image='x')\n"
        "    self.client.containers.run(image='x', network_disabled=True)\n"
        "    other.run(network_disabled=True)\n"
    )
    assert not _sandbox_lock_holds(source)


def test_import_scanner_catches_from_import_shapes():
    """`from app.x import y` 与相对导入的四种绕法必须落进扫描结果。

    负向锁：把 `_imported_modules` 退回"只收 ImportFrom.module"，这四条全红
    （v0.37.0 审查实测的四个漏网形状）。
    """
    cases = {
        "from app.core import providers": "app.core.providers",
        "from app import sandbox": "app.sandbox",
        "from ..core import providers": "app.core.providers",
        "from .. import sandbox": "app.sandbox",
    }
    for source, needle in cases.items():
        mods = _imported_modules(source)
        assert needle in mods, f"{source!r} 没扫出 {needle}，扫到的是 {sorted(mods)}"


def test_import_scanner_does_not_invent_hits():
    """正常相邻导入不许被误报成隔离面焊穿。"""
    mods = _imported_modules("from app.core import audit, config_store\n"
                             "from app.tools import extract\nimport httpx\n")
    assert not any(m == root or m.startswith(root + ".") for m in mods
                   for root in _FORBIDDEN_ROOTS), sorted(mods)


def test_tool_schemas_are_exactly_the_declared_parameters():
    """execute_code 恰 {code, language}，http_fetch 恰 {url}。"""
    exec_props = set(tools_registry["execute_code"]["parameters"]["properties"])
    fetch_props = set(tools_registry["http_fetch"]["parameters"]["properties"])
    assert exec_props == {"code", "language"}, exec_props
    assert fetch_props == {"url"}, fetch_props


def test_http_fetch_registration_flags():
    info = tools_registry["http_fetch"]
    assert info["needs_user"] is True
    assert info["requires_approval"] is False
    assert info["suspends"] is False
    # 审计归因的纪律：user_id 由服务端注入，schema 里连名字都不许出现。
    assert "user_id" not in str(info["parameters"])
