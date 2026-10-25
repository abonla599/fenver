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
