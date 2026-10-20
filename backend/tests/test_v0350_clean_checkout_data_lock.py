"""干净检出闸：读仓库 `data/` 的每一处都得先问它在不在。

`data/` 被 .gitignore 排除，所以 `git clone` 出来的那一份（CI 每次都是）里根本没有
`data/tasks.json`。一句无守卫的 `read_bytes()` 在那份检出里就是 `FileNotFoundError`，
而 CI 全绿是发版顺序的第一格——红在这一格，后面每一步都走不到。

本文件量两件事，判据都朝"这份检出里没有 data/"那一句。**两半条认得的形状要正面写清**，
别把它们读成"这个目录里所有读点都审过了"：

1. **静态**只认这一种读点：路径在**模块级**被绑成一个常量——绑法只认 `ast.Assign` 的**单个
   `Name` 目标**（`REPO_DATA_TASKS = REPO_ROOT / "data" / "tasks.json"`），带注解的
   `REPO_DATA_TASKS: Final = …`（AnnAssign）认不出来，把 `data` 那一段拆成另一枚常量再拼进来
   （`DATA_SUBDIR = "data"`）也认不出来，两格都在清单里；认的条件是**值里含 `data` 这个字面量**，
   读点写成 `<那个名字>.read_bytes()` 这种**属性调用**（方法名必须是挂在 `Attribute` 上的**字面量**：
   把方法名递进 `getattr` / `methodcaller` 的那一族两半条都看不见，而**根名字正是被认出的那个
   常量**，那一族也在清单里），而且存在性判据（`exists()` / `is_file()`
   / `is_dir()`）与读点落在**同一个函数作用域**内、守卫的行号**不晚于**读点那条语句——或者就写在
   同一条语句里（三元式 `x.read_bytes() if x.exists() else None` 是本批既有的写法）。缺文件是一种
   取值，不是异常。
2. **动态**只认这一种函数：**模块级的 `def`**（`ast.FunctionDef`；`async def` 根本不枚举，那一格
   在清单里）、**零参数**、函数体里对上面那个常量**既有读点又有存在性判据**。把它绑到的数据路径
   换成一条**不存在**的路径（干净检出的形状），它必须回 `(None, None)`，不许抛。

认不到的形状逐条记在 `_STILL_MISSED_SHAPES` 那份清单里，每条配一枚"今天必须看不见"的钉子
（`test_the_declared_blind_spots_are_actually_blind`；另有一组把钉子的**落点**钉到机制上的用例——
间接格认出的名字、async 格的同步对照、动态派发两族各自的"根名字就是被认出的那个常量"，
防止"看不见"其实是合成源码写坏了）：谁把这些形状收进
判据而没回来改清单，
钉子就红；清单不是"已穷尽"，它只覆盖复验过的那几种形状。清单**两个方向都钉死**：
`_DECLARED_BLIND_SPOT_NAMES` 是手抄的名单，`test_the_blind_spot_list_is_pinned_in_both_directions`
要求元组与它逐条相等——删掉一条（射程悄悄变宽）与加一条（账没跟上）都会红。另有两格与形状无关的
边界：静态按文本顺序判，会放过"问了却没用上答案"那一种，那种由动态这半条兜；两半条都不覆盖 CI
本身——真跑一遍干净检出得在仓库外做克隆（仓库永远带着 `data/`，本文件自己也一样），也不覆盖
`test_v0350_*.py` 之外那些文件读仓库数据的方式。
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


# ---------- 射程外：两半条今天都看不见的形状（清单，不是待办） ----------
# 每条 = (名字, 喂给判据的源码, 为什么算漏 / 现网有没有活例)。活例口径统一是
# "对着 backend/tests/test_v0350_*.py 做过 AST 扫描"，扫的是读点根名字的身份：函数参数、
# 别名、函数内绑定、非属性调用、import 进来的常量——本批一个都不落在这些格里，全部是
# 模块级常量 + 属性调用（`test_the_ruler_actually_sees_this_batch` 逮到的就是它们），
# 所以下面每一条都是**前瞻性缺口**：今天没有活例，写出来是为了不许把射程说成比测量宽。
_HDR = 'import os\nfrom pathlib import Path\nREPO_ROOT = Path(".")\n' \
       'REPO_DATA_TASKS = REPO_ROOT / "data" / "tasks.json"\n'

# 间接格：把 `data` 那一段拆成另一枚模块级常量，真路径常量的值里就不再写着 data。
_DATA_SEGMENT_INDIRECTION = (
    'import os\nfrom pathlib import Path\nREPO_ROOT = Path(".")\nDATA_SUBDIR = "data"\n'
    'REPO_DATA_TASKS = REPO_ROOT / DATA_SUBDIR / "tasks.json"\n'
    'def _repo_data_state():\n'
    '    return REPO_DATA_TASKS.read_bytes(), REPO_DATA_TASKS.stat().st_mtime_ns\n')
# async 格：同款函数体三种写法——同步对照 / async 带守卫（钉"看不见"用）/ async 不带守卫
# （量"静态那半条其实认得 async 体里的读点"用）。
_SYNC_SNAPSHOT_HELPER_TWIN = _HDR + (
    'def _repo_data_state():\n'
    '    return (REPO_DATA_TASKS.read_bytes()\n'
    '            if REPO_DATA_TASKS.exists() else None), None\n')
_ASYNC_SNAPSHOT_HELPER_GUARDED = _HDR + (
    'async def _repo_data_state():\n'
    '    return (REPO_DATA_TASKS.read_bytes()\n'
    '            if REPO_DATA_TASKS.exists() else None), None\n')
_ASYNC_SNAPSHOT_HELPER_UNGUARDED = _HDR + (
    'async def _repo_data_state():\n'
    '    return REPO_DATA_TASKS.read_bytes(), REPO_DATA_TASKS.stat().st_mtime_ns\n')

# 动态派发族：读点的**方法名**是递进 getattr / methodcaller 的字符串，而 `_calls_in` 那一关只吃
# `func` 是 `ast.Attribute` 的调用 ⇒ 静态报不出读点、动态也枚举不到快照函数，未守卫的那条读点在
# 干净检出里照样抛 FileNotFoundError。这一族的几种拼法同一根因；对照组是同款函数体的静态派发写法
# （`_SYNC_SNAPSHOT_HELPER_TWIN`）与同一函数体的 getattr 守卫版，落点由
# `test_the_dynamic_dispatch_family_is_blind_at_the_method_end` 钉住。
_DYNAMIC_DISPATCH_GETATTR = _HDR + (
    'def _repo_data_state():\n'
    '    return getattr(REPO_DATA_TASKS, "read_bytes")(), None\n')
_DYNAMIC_DISPATCH_METHOD_NAME_IN_VAR = _HDR + (
    'def _repo_data_state():\n'
    '    method = "read_bytes"\n'
    '    return getattr(REPO_DATA_TASKS, method)(), None\n')
_DYNAMIC_DISPATCH_METHODCALLER = (
    'import os\nfrom operator import methodcaller\nfrom pathlib import Path\n'
    'REPO_ROOT = Path(".")\nREPO_DATA_TASKS = REPO_ROOT / "data" / "tasks.json"\n'
    'def _repo_data_state():\n'
    '    return methodcaller("read_bytes")(REPO_DATA_TASKS), None\n')
# 同一函数体、只把读点换成 getattr 递方法名的**带守卫**版：动态那半条连"既读又问"都枚举不到。
_DYNAMIC_DISPATCH_GETATTR_GUARDED = _HDR + (
    'def _repo_data_state():\n'
    '    return (getattr(REPO_DATA_TASKS, "read_bytes")()\n'
    '            if REPO_DATA_TASKS.exists() else None), None\n')

# 第四形：**方法本身当值**传（不是方法名的字符串），四种拼法同一根因。这一族与上面那一族的
# 差别要讲准：上面瞎在「方法名是字符串、被 getattr/methodcaller 拿着」；这里方法对象已经是值，
# 外面套的是局部别名、`__getattribute__`、从 `type(x).__dict__` 里取未绑定函数、或 attrgetter。
# 四种写法的读点 func 分别是 Name / Attribute(__getattribute__，不在 READ_METHODS) / Subscript /
# Call，`_calls_in` 一条都吃不到；路径仍是模块级那个被认出的常量。每一条都必须今天看不见、
# 且把常量指向缺失路径时真抛 FileNotFoundError（落点由
# `test_the_dynamic_dispatch_methods_as_values_family_is_blind_at_the_value_end` 钉住）。
_METHOD_VALUE_FORMS = (
    ("bound-method-in-local-var", _HDR + (
        'def _repo_data_state():\n'
        '    read = REPO_DATA_TASKS.read_bytes\n'
        '    return read(), None\n')),
    ("dunder-getattribute", _HDR + (
        'def _repo_data_state():\n'
        '    return REPO_DATA_TASKS.__getattribute__("read_bytes")(), None\n')),
    ("unbound-from-type-dict", _HDR + (
        'def _repo_data_state():\n'
        '    return type(REPO_DATA_TASKS).__mro__[1].__dict__["read_bytes"](REPO_DATA_TASKS), None\n')),
    ("operator-attrgetter", (
        'import os\nfrom operator import attrgetter\nfrom pathlib import Path\n'
        'REPO_ROOT = Path(".")\nREPO_DATA_TASKS = REPO_ROOT / "data" / "tasks.json"\n'
        'def _repo_data_state():\n'
        '    return attrgetter("read_bytes")(REPO_DATA_TASKS)(), None\n')),
)
# 清单那一格只放一条（一种形状），样本把四种拼法都装进去；机制钉逐条单量。
_DYNAMIC_DISPATCH_METHODS_AS_VALUES = "\n".join(
    source for _tag, source in _METHOD_VALUE_FORMS)

_STILL_MISSED_SHAPES = (
    ("param-hand-off",
     _HDR + 'def g(p):\n    return p.read_bytes()\n'
            'def f():\n    return g(REPO_DATA_TASKS)\n',
     "读点的根名字是**函数参数**：判据只跟着模块级那个常量名走，路径一过参数边界就追不下去。"
     "本批的活例检查：把 data 常量当实参递出去的只有 `os.path.realpath(str(...))` / `str(...)` "
     "这几处，传的是路径字符串、当场不读字节 ⇒ 无活例"),
    ("inline-open",
     _HDR + 'def f():\n    return open(REPO_DATA_TASKS).read()\n',
     "`open(path)` 是**非属性调用**（func 是 Name 不是 Attribute），`_calls_in` 那一关就过不去；"
     "READ_METHODS 里的 `open` 只逮得到 `Path.open()` 那种写法。本批 grep `open(` 零命中 ⇒ 无活例"),
    ("unbound-data-literal",
     'from pathlib import Path\ndef f():\n    return Path("data/tasks.json").read_text()\n',
     "路径没绑成模块级常量、直接内联字面量：`_data_bound_names` 认的是赋值的**值里含 `data`**，"
     "内联字面量没有那个名字可认。本批扫过内联的 data 路径字面量（`Path(…` / `open(…` / 裸 "
     "`data/…` 串）零命中 ⇒ 无活例"),
    ("os-path-join",
     _HDR + 'def f():\n    return open(os.path.join(str(REPO_ROOT), "data", "tasks.json")).read()\n',
     "`os.path.join` 拼出来的字符串没有可认的根名字（并且它本身也是非属性读点）。本批唯一的 "
     "`os.path.join` 在 `_abspath(*parts)` 里，喂进去的是 tmp_path 下的假路径，不读仓库数据 ⇒ 无活例"),
    ("imported-constant",
     'from conftest import REPO_DATA_TASKS\ndef f():\n    return REPO_DATA_TASKS.read_bytes()\n',
     "常量从别的模块 import 进来：`_data_bound_names` 只遍历本模块 tree.body 上的 Assign，"
     "import 不产生绑定记录，于是这个名字在读点侧就是陌生名字。本批 `from conftest import` 零命中 ⇒ 无活例"),
    ("module-level-read-guarded-elsewhere",
     _HDR + 'def guard():\n    return REPO_DATA_TASKS.exists()\n'
            'RAW = REPO_DATA_TASKS.read_bytes()\n',
     "读点在模块级、守卫写在另一个函数里：作用域取的是包住读点的**最小函数**，模块级读点因此"
     "在整模块里找守卫——`guard()` 那一句行号在前，就被当成护住了（行号比较分不清作用域）。"
     "本批的模块级读点只有反例字符串常量里的假源码，真读点都在函数里且同函数有守卫 ⇒ 无活例"),
    ("alias-then-read",
     _HDR + 'ALIAS = REPO_DATA_TASKS\n'
            'def _repo_data_state():\n    return ALIAS.read_bytes(), ALIAS.stat().st_mtime_ns\n',
     "别名行把常量换成另一个名字再读：`ALIAS = REPO_DATA_TASKS` 的值里没有 `data` 字面量，"
     "于是 ALIAS 进不了认得的名单；那个函数也因为只对陌生名字动手而不满足快照函数定义。"
     "本批 `X = REPO_DATA_TASKS` 形式的别名行零命中 ⇒ 无活例"),
    ("function-scope-binding",
     _HDR + 'def f():\n    p = REPO_ROOT / "data" / "tasks.json"\n    return p.read_bytes()\n',
     "**函数内**绑定的路径（哪怕值里就写着 data）静态那半条根本看不见——"
     "它只遍历模块级赋值。动态侧同理，`run_snapshot_helper` 往探针命名空间里塞的也只有模块级"
     "那批名字，函数内绑定的读点在两半条里都是隐形。本批唯一的函数内 data 段绑定是本文件自己的"
     "`target_dir = tmp_path / …`，那是 tmp 下的假数据根，不是仓库路径 ⇒ 无活例"),
    ("data-segment-indirection",
     _DATA_SEGMENT_INDIRECTION,
     "`data` 那一段拆成另一枚模块级常量再拼进路径：`_data_bound_names` 认的是赋值的**值里含 `data` "
     "字面量**，而 `REPO_ROOT / DATA_SUBDIR / \"tasks.json\"` 的值里只剩 `tasks.json`，于是真路径"
     "常量整个陌生、名单里躺着的是谁都没被读过的 `DATA_SUBDIR`。两半条同时看不见这一格：静态报不出"
     "读点、动态连快照函数都枚举不到，于是那条无守卫的读点在干净检出里抛 FileNotFoundError——CI "
     "照样红，只是红成一串不报 `文件:行号` 的裸异常，这把锁不响，`test_the_ruler_actually_sees_"
     "this_batch` 也照绿。与 `alias-then-read` 那一格不是同一格：那条是 `ALIAS = "
     "REPO_DATA_TASKS`（值是 Name，真常量仍被认到、只是换了个陌生名字去读），这条是真常量自己进不了"
     "名单，所以 `alias-then-read` 盖不住这一格。本批（乃至整个 tests 目录）扫过模块级 `X = \"data\"` 这种段"
     "常量：零命中 ⇒ 无活例"),
    ("annotated-assignment",
     'import os\nfrom pathlib import Path\nREPO_ROOT = Path(".")\n'
     'REPO_DATA_TASKS: Final = REPO_ROOT / "data" / "tasks.json"\n'
     'def _repo_data_state():\n'
     '    return REPO_DATA_TASKS.read_bytes(), REPO_DATA_TASKS.stat().st_mtime_ns\n',
     "带注解的模块级绑定是 `ast.AnnAssign`，`_data_bound_names` 只吃 `ast.Assign` 且单 `Name` 目标 "
     "⇒ 名单是空的，`unguarded_read_sites` 开头 `if not names: return []` 就直接退了，两半条一起瞎，"
     "那条读点护没护着根本没人问。正面声明里那句\"模块级被绑成一个常量\"讲的就是这个 `ast.Assign` "
     "单目标。本批模块级 AnnAssign 零命中（批内有带注解的绑定，都在函数里，那种本来就在 "
     "`function-scope-binding` 那一格的射程外）⇒ 无活例"),
    ("async-snapshot-helper",
     _ASYNC_SNAPSHOT_HELPER_GUARDED,
     "只漏**动态**那半条：`snapshot_helpers` 只枚举 `ast.FunctionDef`，`async def` 的快照函数进不了"
     "名单 ⇒ `test_snapshot_helpers_answer_an_absent_data_path` 对它一条判据都不跑，它面对不存在的 "
     "data/ 抛不抛、回不回 `(None, None)` 没人问。静态那半条**认得** async 函数体里的读点"
     "（`_enclosing_function` 两种函数都吃），所以这里的合成源码刻意**带守卫**，好让上面那枚钉子只"
     "钉动态那一格——把守卫摘掉静态就会报读点，那不是这一格的漏（对照量在 "
     "`test_the_async_gap_is_the_async_not_the_shape`）。本批有 `async def`，但没有一个 async 函数"
     "同时带 data 读点与存在性判据（= 没有 async 快照函数），识别到的读点全在同步 `def` 里 ⇒ 无活例"),
    ("dynamic-dispatch-getattr",
     _DYNAMIC_DISPATCH_GETATTR,
     "方法名是**递进 getattr 的字符串**：`_calls_in` 只吃 `func` 为 `ast.Attribute` 的调用，"
     "`getattr(REPO_DATA_TASKS, \"read_bytes\")()` 的 func 是 `Name(getattr)` ⇒ 静态报不出读点、"
     "动态也枚举不到快照函数，那条无守卫的读点在干净检出里抛 FileNotFoundError 而两半条都不响。"
     "**根名字正是被认出的那个常量**，所以它与 `inline-open`、`os-path-join` 两格不同源：那两格瞎在"
     "根名字陌生（收进射程得动 `_data_bound_names`），这一族要动的是方法名那一端，那两格的判据一条"
     "也不会跟着变。本批 `getattr(` 的命中都落在 AST 节点、枚举值与模块属性上，没有一枚挂在 data "
     "路径常量的读点上 ⇒ 无活例"),
    ("dynamic-dispatch-method-name-in-var",
     _DYNAMIC_DISPATCH_METHOD_NAME_IN_VAR,
     "同一根因的另一种拼法：方法名先存进**局部变量**再 getattr。字符串常量挪了位置不改变缺口"
     "（判据看不见的方法名仍是方法名）；`function-scope-binding` 那一格讲的是**路径**在函数内绑定，"
     "而这一格的路径仍是模块级那个被认出的常量，那一格盖不住它。本批扫过：没有把方法名存进变量再 "
     "getattr 的读点 ⇒ 无活例"),
    ("dynamic-dispatch-methodcaller",
     _DYNAMIC_DISPATCH_METHODCALLER,
     "`methodcaller(\"read_bytes\")(REPO_DATA_TASKS)`：外层调用的 `func` 是一个 `Call`、内层的 attr "
     "是 `methodcaller`（不在 READ_METHODS）⇒ 两条都过不去 `_calls_in`，而根名字照样是被认出的"
     "常量。本批 `methodcaller` 零命中 ⇒ 无活例"),
    ("dynamic-dispatch-methods-as-values",
     _DYNAMIC_DISPATCH_METHODS_AS_VALUES,
     "**方法本身当值**的四种拼法：局部别名 `read = REPO_DATA_TASKS.read_bytes; read()`、"
     "`__getattribute__(\"read_bytes\")()`、`type(x).__mro__[1].__dict__[\"read_bytes\"](x)`"
     "（具体的 WindowsPath/PosixPath 类不带继承来的 read_bytes，所以取 MRO 上的 Path 那一格；"
     "这正是「从类字典里取出未绑定函数」的写法）、`attrgetter(\"read_bytes\")(x)()`。与上一族的分界是"
     "「方法名还是字符串」——这里方法对象已经"
     "是值，所以改 getattr/methodcaller 那两格的判据不会顺手收掉这一族。四种写法的读点 `func`"
     "分别是 Name / 不在 READ_METHODS 的 Attribute / Subscript / Call，`_calls_in` 一条都吃不到；"
     "根名字仍是被认出的那个常量。本批扫过：没有把方法对象存成值或经 attrgetter/type-dict 派发的"
     "读点 ⇒ 无活例"),
)

# 对照组：尺子对"它说它认得"的那个形状是真认得——没有这一枚，上面那一串"看不见"就可能是
# 尺子在空转（这条文件的反空转规矩）。
_RECOGNIZED_SHAPE = _HDR + 'def f():\n    return REPO_DATA_TASKS.read_bytes()\n'

# 手抄的名单（#126 的"两个方向"）：`_STILL_MISSED_SHAPES` 只有"今天仍然看不见"这一半，
# 删掉一条（把某一格说成已覆盖）或悄悄加一条都不会被上面那枚钉子拦住。这里把名单本身钉成
# 逐条相等：少一条 = 射程声明变宽而没人回来删账；多一条 = 新形状没登记进这份手抄名单。
# 改清单必须同批改这份名单，与 `test_v0350_lock_comment_stripping_scope.py` 的条数钉同一个方向。
_DECLARED_BLIND_SPOT_NAMES = (
    "param-hand-off", "inline-open", "unbound-data-literal", "os-path-join",
    "imported-constant", "module-level-read-guarded-elsewhere", "alias-then-read",
    "function-scope-binding", "data-segment-indirection", "annotated-assignment",
    "async-snapshot-helper", "dynamic-dispatch-getattr",
    "dynamic-dispatch-method-name-in-var", "dynamic-dispatch-methodcaller",
    "dynamic-dispatch-methods-as-values",
)


def _blind_spot_roster_problems(names, roster) -> dict:
    """清单与手抄名单的分叉，两个方向各回一份：`missing` = 名单里有、清单里没了；
    `unledgered` = 清单里有、名单没登记。顺序不同也算分叉（这份名单是给人按序读的账）。"""
    return {
        "missing": [n for n in roster if n not in names],
        "unledgered": [n for n in names if n not in roster],
        "order": names != tuple(n for n in roster if n in names),
    }


def test_the_blind_spot_list_is_pinned_in_both_directions():
    """清单两个方向都钉：删掉一条（射程悄悄变宽）与加一条（账没跟上）都必须红。

    没有这一枚时，`_STILL_MISSED_SHAPES` 是**单向钉**——它只问"列出来的这些今天还看不见吗"，
    把其中三条整行删掉，参数化循环少转三圈，全绿且没人知道射程声明已经比测量宽。反过来加一条
    新形状也不会有人被提醒去核手抄名单。这里比的是元组与 `_DECLARED_BLIND_SPOT_NAMES` 的完整
    等式（含顺序），两边必须同批改。
    """
    names = tuple(name for name, _source, _note in _STILL_MISSED_SHAPES)
    problems = _blind_spot_roster_problems(names, _DECLARED_BLIND_SPOT_NAMES)
    assert not any(problems.values()), (
        "清单与手抄名单分家了：missing（名单里有、清单删了）= %s，unledgered（清单加了、名单没"
        "登记）= %s，order=%s。修清单就把 `_DECLARED_BLIND_SPOT_NAMES` 同批改，别让射程声明"
        "与清单各说各话" % (problems["missing"], problems["unledgered"], problems["order"]))
    # 反例：这枚等式必须有牙——删掉最后一格、改成名单外的名字、打乱顺序，三种分叉都得被点名。
    dropped = _blind_spot_roster_problems(names[:-1], _DECLARED_BLIND_SPOT_NAMES)
    renamed = _blind_spot_roster_problems(names[:-1] + ("dynamic-dispatch-invented",),
                                          _DECLARED_BLIND_SPOT_NAMES)
    reordered = _blind_spot_roster_problems(tuple(reversed(names)), _DECLARED_BLIND_SPOT_NAMES)
    assert dropped["missing"] == [_DECLARED_BLIND_SPOT_NAMES[-1]], dropped
    assert renamed["unledgered"] == ["dynamic-dispatch-invented"], renamed
    assert reordered["order"] is True, reordered


def test_the_declared_blind_spots_are_actually_blind():
    """清单里每一条今天都得**看不见**：钉的是"声明与测量一致"，不是把判据收紧。

    两半条各判一次（静态报不出读点、动态认不出快照函数），谁以后把这些形状收进射程而没回来
    删掉对应那条，这里就红——清单必须越还越少，和本文件的豁免名单同一个方向。
    """
    for name, source, _note in _STILL_MISSED_SHAPES:
        assert unguarded_read_sites(source, "<synth>") == [], (
            "%s 已经看得见，射程声明该改、清单里该删这一条" % name)
        assert not snapshot_helpers(source, "<synth>")[2], (
            "%s 的动态那半条已经覆盖，清单里该删这一条" % name)
    assert len(unguarded_read_sites(_RECOGNIZED_SHAPE, "<control>")) == 1, (
        "连对照组都判不出读点 ⇒ 上面那一串「看不见」是尺子在空转，不是射程外")


def test_the_data_segment_indirection_is_blind_by_name_not_by_accident():
    """间接格钉到机制上：这一格里尺子认出的名字必须恰是那枚装着 `data` 段的中间常量。

    只判"看不见"不够——看不见也可能是合成源码自己写坏了（值里没有 `data` 段、读点拼错名字），
    那枚钉子就是假的、绿得没有意义。这里把**为什么**看不见钉住：名单必须正好是 `DATA_SUBDIR`。
    哪天判据学会顺着名字把段拼回路径（射程收了这一格），名单里就会多出真路径常量、这里红，
    清单里那条该删；名单空了同样红，红的是"这枚钉子不再指着这一格"。
    """
    names = _data_bound_names(ast.parse(_DATA_SEGMENT_INDIRECTION))
    assert names == {"DATA_SUBDIR"}, (
        "间接格里尺子认出的名字是 %s，不再是只有那枚中间常量：认出真路径常量说明射程已经收了这一格、"
        "清单里该删那条；一个都不认说明合成源码与判据脱钩了，这根钉子是假的" % sorted(names))
    assert unguarded_read_sites(_DATA_SEGMENT_INDIRECTION, "<synth>") == [], (
        "data 段间接格已经看得见，清单里该删这一条")
    assert not snapshot_helpers(_DATA_SEGMENT_INDIRECTION, "<synth>")[2], (
        "data 段间接格的动态那半条已经覆盖，清单里该删这一条")


def test_the_async_gap_is_the_async_and_not_the_shape():
    """async 那一格只漏动态半条：同款函数体写成 `def` 就被枚举、写成 `async def` 就不被枚举。

    少了这组对照，那句"看不见"可能瞎在别处（比如函数体根本不像快照函数、零参数没判上），
    也没法说清静态那半条其实是**认得** async 函数体里读点的——声明要讲准，就得当场各量一次。
    """
    assert [n.name for n in snapshot_helpers(_SYNC_SNAPSHOT_HELPER_TWIN, "<control>")[2]] == [
        "_repo_data_state"], (
        "对照组（同步 def 的同款函数体）不再被枚举成快照函数：动态那半条瞎的地方变了，"
        "上面那枚 async 钉子的落点得重挑")
    for source, tag in ((_ASYNC_SNAPSHOT_HELPER_GUARDED, "<synth>"),
                        (_ASYNC_SNAPSHOT_HELPER_UNGUARDED, "<synth-unguarded>")):
        assert not snapshot_helpers(source, tag)[2], (
            "async 快照函数已经被枚举了：射程收了这一格，清单里该删那条")
    assert len(unguarded_read_sites(_ASYNC_SNAPSHOT_HELPER_UNGUARDED, "<synth>")) == 2, (
        "静态那半条对 async 函数体里的读点不再报两枚：清单里那句「静态认得 async 体里的读点」"
        "与测量不一致了，改声明或改判据，别留着")


def test_the_dynamic_dispatch_family_is_blind_at_the_method_end(tmp_path):
    """方法名当字符串那一族：瞎的是**方法名**那一端，不是根名字——这一格必须钉到机制上才敢引用它。

    `inline-open` 与 `os-path-join` 那两格也是"看不见"，但落点完全不同（根名字陌生），所以光靠
    清单里那条通用钉子分不开这一族与那两格。这里各量一次：
    ① 这一族每种拼法的合成源码里，`_data_bound_names` 认出的名字必须**恰好**是那条 data 路径常量——
       名单空了或换成了别的名字都说明源码与判据脱钩，那枚"看不见"是假的；
    ② 对照组：同款函数体写成静态属性派发必须被枚举成快照函数，写成 getattr 递方法名（带守卫那版）
       必须不被枚举 ⇒ 两次的差异只剩派发形态，不是"函数体本来就不像快照函数"；
    ③ 代价当场量一次：把常量指向一条不存在的路径（干净检出的形状）单跑这一族每种写法，必须真抛
       `FileNotFoundError`，而同一份源码在两半条判据里各报空——"CI 会红但不报 文件:行号"这句
       不是相信出来的，是跑出来的。
    """
    for source, tag in ((_DYNAMIC_DISPATCH_GETATTR, "<getattr>"),
                        (_DYNAMIC_DISPATCH_METHOD_NAME_IN_VAR, "<method-in-var>"),
                        (_DYNAMIC_DISPATCH_METHODCALLER, "<methodcaller>")):
        names = _data_bound_names(ast.parse(source))
        assert names == {"REPO_DATA_TASKS"}, (
            "%s 里尺子认出的名字是 %s，不再是那条 data 路径常量：这一族的落点变了（或合成源码与"
            "判据脱钩了，那这枚钉子就是假的）" % (tag, sorted(names)))
        assert unguarded_read_sites(source, tag) == [], (
            "%s 的读点已经看得见，清单里该删这一条" % tag)
        assert not snapshot_helpers(source, tag)[2], (
            "%s 的动态那半条已经覆盖，清单里该删这一条" % tag)

    assert [n.name for n in snapshot_helpers(_SYNC_SNAPSHOT_HELPER_TWIN, "<control>")[2]] == [
        "_repo_data_state"], (
        "对照组（静态属性派发的同款函数体）不再被枚举成快照函数：②里剩下的差异就不是派发形态了，"
        "这枚钉子的落点得重挑")
    assert not snapshot_helpers(_DYNAMIC_DISPATCH_GETATTR_GUARDED, "<synth>")[2], (
        "带守卫的 getattr 版已经被枚举成快照函数：射程收了这一格，清单里该删那条")

    # 单跑这一族每种写法：被 exec 的只有那个函数体，源码里那行 `from operator import methodcaller`
    # 不参与，所以命名空间里补的是标准库同一个对象；REPO_DATA_TASKS 指向父目录都不在的路径 =
    # 干净检出。
    missing = tmp_path / "not-checked-out" / "tasks.json"
    probe_globals = {"__name__": "dispatch_probe", "Path": Path, "os": os,
                     "REPO_DATA_TASKS": missing,
                     "methodcaller": __import__("operator").methodcaller}
    for source, tag in ((_DYNAMIC_DISPATCH_GETATTR, "<getattr>"),
                        (_DYNAMIC_DISPATCH_METHOD_NAME_IN_VAR, "<method-in-var>"),
                        (_DYNAMIC_DISPATCH_METHODCALLER, "<methodcaller>")):
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef))
        namespace = dict(probe_globals)
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<probe>", "exec"), namespace)
        with pytest.raises(FileNotFoundError):
            namespace["_repo_data_state"]()


def test_the_dynamic_dispatch_methods_as_values_family_is_blind_at_the_value_end(tmp_path):
    """第四形（方法本身当值）：落点仍是方法那一端，但**方法名连字符串都不是**了。

    与 `dynamic-dispatch-getattr` 那一族的分界：那里瞎在"字符串被 getattr/methodcaller 拿着"，
    这里方法对象已经是值，套的是局部别名、`__getattribute__`、`type(x).__dict__` 里的未绑定
    函数、或 attrgetter。把上面那族的判据收进射程，这一族照样看不见——所以清单里必须是两条，
    不能合成一条。每种拼法各量：
    ① `_data_bound_names` 认出的名字恰好是那条 data 路径常量（根名字没变，落点在方法那一端）；
    ② 静态报不出读点、动态也枚举不到快照函数；
    ③ 把常量指向缺失路径单跑，必须真抛 `FileNotFoundError`——这四条写法是**真读**，不是死代码。
    对照组仍用静态属性派发的同款函数体：它必须被枚举成快照函数。
    """
    assert [n.name for n in snapshot_helpers(_SYNC_SNAPSHOT_HELPER_TWIN, "<control>")[2]] == [
        "_repo_data_state"], (
        "对照组（静态属性派发的同款函数体）不再被枚举成快照函数：这一族的差异就不是派发形态了")
    missing = tmp_path / "not-checked-out" / "tasks.json"
    probe_globals = {"__name__": "method_value_probe", "Path": Path, "os": os,
                     "REPO_DATA_TASKS": missing,
                     "attrgetter": __import__("operator").attrgetter}
    for tag, source in _METHOD_VALUE_FORMS:
        names = _data_bound_names(ast.parse(source))
        assert names == {"REPO_DATA_TASKS"}, (
            "%s 里尺子认出的名字是 %s，不再是那条 data 路径常量：这一族的落点变了（或合成源码与"
            "判据脱钩了，那这枚钉子就是假的）" % (tag, sorted(names)))
        assert unguarded_read_sites(source, tag) == [], (
            "%s 的读点已经看得见，清单里该删这一条" % tag)
        assert not snapshot_helpers(source, tag)[2], (
            "%s 的动态那半条已经覆盖，清单里该删这一条" % tag)
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef))
        namespace = dict(probe_globals)
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<probe>", "exec"), namespace)
        with pytest.raises(FileNotFoundError):
            namespace["_repo_data_state"]()

