"""干净检出闸：读仓库 `data/` 的每一处都得先问它在不在。

`data/` 被 .gitignore 排除，所以 `git clone` 出来的那一份（CI 每次都是）里根本没有
`data/tasks.json`。一句无守卫的 `read_bytes()` 在那份检出里就是 `FileNotFoundError`，
而 CI 全绿是发版顺序的第一格——红在这一格，后面每一步都走不到。

本文件量两件事，判据都朝"这份检出里没有 data/"那一句：

1. **静态**：`backend/tests/test_v0350_*.py` 里每一个读仓库数据路径的调用点，都要有一条
   存在性判据（`exists()` / `is_file()` / `is_dir()`）走在它前面，或者写在同一条语句里
   （三元式 `x.read_bytes() if x.exists() else None` 是这一批既有的写法）。缺文件是一种
   取值，不是异常。
2. **动态**：把这批模块里那些"零参数、既问存在又读字节"的快照函数摘出来单跑，把它绑到
   的数据路径换成一条**不存在**的路径——那正是干净检出的形状。它必须回 `(None, None)`，
   不许抛。

射程说清楚：静态那半条按文本顺序判，会放过"问了却没用上答案"那一种；那种由动态这半条
兜。两半条都不覆盖 CI 本身——真跑一遍干净检出得在仓库外做克隆（仓库永远带着 `data/`，
本文件自己也一样），也不覆盖 `test_v0350_*.py` 之外那些文件读仓库数据的方式。
"""
import ast
import os
import tempfile
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
BATCH_GLOB = "test_v0350_*.py"

READ_METHODS = {"read_bytes", "read_text", "stat", "open", "iterdir", "glob", "touch",
                "unlink", "write_bytes", "write_text", "listdir", "scandir"}
PREDICATE_METHODS = {"exists", "is_file", "is_dir"}

# 这两份是被这条锁逮住过的那一族；名字钉在这儿，别的文件漏判也不会让它们再退回无守卫。
EXPECTED_SNAPSHOT_HELPERS = {
    ("test_v0350_ticket33_failed_breakpoint.py", "_repo_data_state"),
    ("test_v0350_ticket37_gate_refused_breakpoint.py", "_repo_data_state"),
}


def _root_name(node):
    """`REPO_DATA_TASKS.read_bytes()` 这条链最底下那个名字。"""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _parents(tree):
    map_ = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            map_[child] = node
    return map_


def _enclosing_stmt(node, parents):
    while node is not None and not isinstance(node, ast.stmt):
        node = parents.get(node)
    return node


def _data_bound_names(tree):
    """模块级被绑成 `… / "data" / …` 的那几个名字（认值的形状，不认名字）。"""
    names = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        literals = [c.value for c in ast.walk(node.value)
                    if isinstance(c, ast.Constant) and isinstance(c.value, str)]
        if "data" in literals:
            names.add(target.id)
    return names


def _calls_in(scope, names, attrs):
    """`scope` 里那些底下挂着 `names` 之一、方法名落在 `attrs` 里的调用。"""
    return [node for node in ast.walk(scope)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in attrs and _root_name(node.func.value) in names]


def _enclosing_function(tree, lineno):
    """包住这一行的最小函数作用域；不在任何函数里就是模块作用域。"""
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.lineno <= lineno <= node.end_lineno \
                and (best is None or node.end_lineno - node.lineno
                     < best.end_lineno - best.lineno):
            best = node
    return best or tree


def unguarded_read_sites(source, filename="<source>"):
    """[(行号, 源码行)]：这一行所在语句既没被前面的存在性判据护着、也不在同一条语句里问。"""
    tree = ast.parse(source, filename=filename)
    names = _data_bound_names(tree)
    if not names:
        return []
    parents = _parents(tree)
    lines = source.splitlines()
    out = []
    for read in _calls_in(tree, names, READ_METHODS):
        stmt = _enclosing_stmt(read, parents)
        scope = _enclosing_function(tree, read.lineno)
        guarded = any(pred.lineno <= stmt.lineno                      # 前面的语句问过了
                      or stmt.lineno <= pred.lineno <= stmt.end_lineno  # 就写在这条语句里
                      for pred in _calls_in(scope, names, PREDICATE_METHODS))
        if not guarded:
            out.append((read.lineno, lines[read.lineno - 1].strip()))
    return sorted(out)


def snapshot_helpers(source, filename="<source>"):
    """模块级、零参数、既问存在又读字节的函数——这一批的快照约定。"""
    tree = ast.parse(source, filename=filename)
    names = _data_bound_names(tree)
    found = [node for node in tree.body
             if isinstance(node, ast.FunctionDef)
             and not (node.args.args or node.args.posonlyargs or node.args.kwonlyargs
                      or node.args.vararg or node.args.kwarg)
             and _calls_in(node, names, READ_METHODS)
             and _calls_in(node, names, PREDICATE_METHODS)]
    return tree, names, found


def run_snapshot_helper(source, func_name, tmp_path, absent=True):
    """把那一个函数从模块里摘出来单跑；`absent` 决定它面对的数据路径存不存在。"""
    tree, names, found = snapshot_helpers(source)
    node = next((n for n in found if n.name == func_name), None)
    assert node is not None, f"这份源码里没有零参数的快照函数 {func_name}"
    target_dir = tmp_path / "data"
    if absent:
        target = tmp_path / "not-checked-out" / "tasks.json"      # 父目录都不在
    else:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "tasks.json"
        target.write_bytes(b'{"tasks": {}}')
    namespace = {"__name__": "clean_checkout_probe", "Path": Path, "os": os,
                 "tempfile": tempfile}
    for name in names:
        namespace[name] = target
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<probe>", "exec"), namespace)
    return namespace[func_name]()


def _batch():
    return [(p, p.read_text(encoding="utf-8")) for p in sorted(TESTS_DIR.glob(BATCH_GLOB))]


# 反例（喂判据用，绝不落到仓库数据路径上）：老写法——不问就开。
HOSTILE_UNGUARDED = '''from pathlib import Path
REPO_ROOT = Path(".")
REPO_DATA_TASKS = REPO_ROOT / "data" / "tasks.json"
def _repo_data_state():
    return REPO_DATA_TASKS.read_bytes(), REPO_DATA_TASKS.stat().st_mtime_ns
'''
# 反例二：问了，但没把答案用在该用的地方——静态那半条放过它，动态那半条必须逮住。
HOSTILE_ASKED_BUT_UNUSED = '''from pathlib import Path
REPO_ROOT = Path(".")
REPO_DATA_TASKS = REPO_ROOT / "data" / "tasks.json"
def _repo_data_state():
    REPO_DATA_TASKS.exists()
    return REPO_DATA_TASKS.read_bytes(), REPO_DATA_TASKS.stat().st_mtime_ns
'''

# ---------- 静态：整个 v0.35 批次都没有无守卫的读点 ----------


def test_the_ruler_actually_sees_this_batch():
    """扫不到任何读点 = 尺子在空转。先证明它看得见这一批里那些真读点。"""
    seen = 0
    for _, source in _batch():
        tree = ast.parse(source)
        seen += len(_calls_in(tree, _data_bound_names(tree), READ_METHODS))
    assert seen > 0, f"{BATCH_GLOB} 里一个仓库数据读点都没扫到，本文件的判据没牙"


def test_no_test_in_the_batch_reads_repo_data_without_asking_first():
    """每个读仓库数据路径的点都被存在性判据护着——干净检出里没有 data/，红就红在这。"""
    offenders = [f"{path.name}:{lineno}: {line}"
                 for path, source in _batch()
                 for lineno, line in unguarded_read_sites(source, str(path))]
    assert not offenders, \
        "无守卫地读仓库 data/，干净检出（CI）必红：\n" + "\n".join(offenders)


def test_static_ruler_bites_on_the_old_shape():
    """反例：把读点写回"不问就开"那一种形状，上面那条判据必须报红（恒绿的锁付过账）。"""
    offenders = unguarded_read_sites(HOSTILE_UNGUARDED, "<hostile>")
    assert len(offenders) == 2, f"尺子没牙，反例只报出 {offenders}"
    assert {lineno for lineno, _ in offenders} == {5}
    assert all("read_bytes" in line or "stat" in line for _, line in offenders)


# ---------- 动态：快照函数面对"数据路径不存在"必须回 (None, None) ----------


def test_snapshot_helpers_answer_an_absent_data_path(tmp_path):
    """把每个快照函数指向一条不存在的路径（干净检出的形状）：它得答 (None, None)。"""
    covered = set()
    for path, source in _batch():
        for node in snapshot_helpers(source, str(path))[2]:
            covered.add((path.name, node.name))
            try:
                got = run_snapshot_helper(source, node.name, tmp_path)
            except Exception as exc:
                raise AssertionError(
                    f"{path.name}::{node.name}() 在数据路径不存在时抛了 {exc!r}"
                    "——干净检出里没有 data/，这一条就是 CI 里的那一次红") from exc
            assert got == (None, None), f"{path.name}::{node.name}() 回了 {got!r}"
    assert EXPECTED_SNAPSHOT_HELPERS <= covered, (
        f"这一族的快照函数没被量到：缺 {sorted(EXPECTED_SNAPSHOT_HELPERS - covered)}，"
        f"实际量到 {sorted(covered)}")


def test_snapshot_helpers_still_answer_when_the_path_exists(tmp_path):
    """同一条判据的正对照：路径真在的时候，快照必须给出字节和 mtime，不是 (None, None)。"""
    for path, source in _batch():
        for node in snapshot_helpers(source, str(path))[2]:
            got = run_snapshot_helper(source, node.name, tmp_path, absent=False)
            assert isinstance(got[0], bytes) and isinstance(got[1], int), (
                f"{path.name}::{node.name}() 在文件存在时回了 {got!r}"
                "——它对'有没有'根本没反应，上面那条缺席判据也就是恒真")


def test_dynamic_ruler_bites_when_the_answer_is_not_used(tmp_path):
    """反例二：问了存在性却没把答案用在分支上，静态放过，动态这一半条必须抛出来。"""
    with pytest.raises(FileNotFoundError):
        run_snapshot_helper(HOSTILE_ASKED_BUT_UNUSED, "_repo_data_state", tmp_path)
    assert unguarded_read_sites(HOSTILE_ASKED_BUT_UNUSED) == [], (
        "静态那半条按文本顺序判，本来就该放过这一种；这里记下分工，别把它当成漏判")
