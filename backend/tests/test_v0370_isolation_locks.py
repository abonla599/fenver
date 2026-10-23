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
2. `sandbox_manager.py:77` 的 `network_disabled=True`——AST 找 docker 容器
   create 调用上的关键字参数，同时钉当前行号 77：行号漂了本条红并教人重新锚定，
   参数被摘掉更是直接把"代码执行不出网"这条隔离拆掉；
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


def _imported_modules(source: str) -> set:
    """AST 认得的形状：`import a.b[ as c]`、`import a.b, d.e`、`from a.b import x`。

    绝对与相对导入都收（相对导入把 `from .. import sandbox` 这类绕法也算进来：
    按层数折算回 app.* 前缀比对）。条件导入（函数体里的 import）同样在射程内——
    遍历的是整棵语法树，不限模块顶层。
    """
    tree = ast.parse(source)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:                        # 相对导入：够不着的层数按 0 收进 "app."
                found.add("." * node.level)
            if node.module:
                found.add(node.module)
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


def test_sandbox_container_create_keeps_network_disabled():
    """AST 找 docker 容器 create 调用上的 `network_disabled=True`，并钉当前行号 77。

    行号是**观测值**不是判据本体：判据是"这个 kwargs 必须还在且值为 True"。
    哪天上面插了代码让行号漂了，第二句会红并给出真实行号——那是要重新锚定，
    不是要删掉这条锁。
    """
    tree = ast.parse(SANDBOX_SRC)
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "network_disabled" and isinstance(kw.value, ast.Constant) \
                        and kw.value.value is True:
                    found = kw
    assert found is not None, "sandbox_manager 里找不到 network_disabled=True——沙箱隔离被拆了"
    lines = SANDBOX_SRC.splitlines()
    actual = found.lineno
    assert "network_disabled=True" in lines[actual - 1]
    if actual != 77:
        pytest.fail(f"network_disabled=True 从第 77 行漂到了第 {actual} 行："
                    "确认没有别的改动后把锚更新成这个数，锁本身不许删")


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
