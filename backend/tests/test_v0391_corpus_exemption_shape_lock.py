r"""#267 的 AST 形状锁：语料豁免判据那三件必备，摘掉任何一件必须红在**这里**。

为什么需要这一把（#265 审查实测，不是推测）：
- 把 `assert out.returncode in (0, 1)` 摘掉、或整段包成 `except Exception: return set()`
  以后，CI 与干净克隆**两种树都 74 passed**——这一格完全隐形；只有在生产根那种"恰好有
  六份被忽略文档"的树上它才红，而红话反过来宣称"已扣掉，不是漏扫"，误导性的红比不红更坏。
- 把豁免换成六族字面量名单（git 一次都不问）以后，行为与问 git **在全部可观察场景下不可
  区分**——于是"判据只来自 git"这句话今天没有牙，只有注释在守。

所以这一把盯的是**写法**而不是运行结果：三件必备缺一即红，与跑在哪棵树、磁盘上有没有那
六份文档都无关。

## 必备形状（工单 #267 交付 B 的原文口径）

**条数不写在注释里**：哪些形状是必备的、一共几枚，以下面各枚测试与
`test_the_scope_membership_is_the_declared_roster` 的成员钉为准（#268 的"数只活在钉里"）。
注释里再抄一份数就是第二份真相，它一定比钉先腐烂——先例照抄
`test_ci_history_depth.py` 的 `HISTORY_READING_MARKERS` 那枚条数钉（== 固定值、注释不写数）。

1. 必须出现 `subprocess.run`，且它的参数里有字符串 `"check-ignore"`；
2. 必须有针对 `returncode` 的判定，形状是 `… .returncode in (0, 1)`，而且这一形必须
   **直接是一枚 assert 的 test**——审查缴械 c 写的是 `assert True or (… in (0, 1))`：
   字样还在、判据已死，所以"子树里找得到"不算数；
3. 豁免集合不许来自函数字面量：禁 `startswith`/`endswith` 这一族段排除、禁把
   Set/Tuple/List 的字符串常量名单当作赋值/返回/`in` 比较的**左右两边**、禁判据函数引用
   模块级字符串名单（"git 问答照旧留着、再并一份手抄名单"这一形就是这么抓的）。

外加同样钉写法的加固：**判据不许被 try/except 摘掉**（helper、两把锁的调用点、以及调用点里
把判据包成 wrapper 的那只手——这几格都不许有返回值的 except 分支）；**扣减项必须是
这份判据的返回值**（不许从名字、属性或模块级名单绕回来）；**判据里不许给问法补斜杠**
（`p + "/"` 那一形在 CRLF 检出上会被裸 CR 空模式恒判"已忽略"，见 helper 的 docstring）。

## #268 补的三格（这把锁自己的盲区，逐条写明挡谁、挡不住谁）

- **自扫**：这一把原来不扫自己——`_SCOPE` 不含本文件，而
  `test_the_shape_lock_is_registered_where_the_guarded_locks_live` 只钉"在册 + 磁盘可读"、
  不钉内部形状，所以改它自己正文里的一句断言（塞一枚 `assert True or …`）没人红。
  `test_the_shape_lock_does_not_disarm_its_own_body` 补的就是这一格：照 #266
  `no_tautological_assert` 的形状做，**不另造一套判据**，只对本文件全部顶层函数跑恒真断言并集。
  为什么不把 `SHAPE_LOCK` 塞进 `_SCOPE`（主控人裁决）：`_walks_disk` 把 `ast.walk` 当磁盘遍历，
  自扫会把本文件每个 AST helper 都拖进 `except`-return 那一格——今天绿，但将来任何人在这里写
  `except: return x` 都会被**错理由**判红。
- **成员钉**：`test_the_scope_membership_is_the_declared_roster` 钉 `_SCOPE`/`_CALL_SITES`
  的成员与条数（从 `_SCOPE` 摘掉 helper 那棵 ⇒ 这里先红，不用等"认领不到判据函数"那条间接红话），
  并把"SHAPE_LOCK 不进 `_SCOPE`"这条裁决钉在机器上而不是钉在注释里。
- **条数与清单钉**：`test_the_disarm_roster_is_pinned_and_distinct` 钉 `_DISARM_SAMPLES`
  的条数下限、id 互不相同、kind 必须落在白名单里。它换掉的是原来那枚**恒真**的
  `len(caught) == len(_DISARM_SAMPLES)`（`caught` 在每轮循环里必然 +1，那个等式永真、不含信息）。
  诚实登记：和 #266 那侧一样，**条数钉只挡"删"，挡不住"三处同向一起缩"**——这一把的
  "名单 + 探测器实现 + 语料代表"如果同向一起删，红话仍然只来自条数，所以条数就是唯一的那道闸。

## 射程（从 `git ls-files` 现取，问不到即 fail-closed）

盯的是 `_SCOPE` 名单里的那几棵文件：那份 helper 与两把锁的调用点（成员与条数由
`test_the_scope_membership_is_the_declared_roster` 钉住，注释不重复抄这份数）。射程名单本身也从
`git ls-files backend/tests` 现取：git 问不到、返回空、或名单里任一棵不入库一律红——
"扫不到"与"守住了"不许长得一样（B1 同一条规矩）。

"判据函数"按**结构**认领而不是按名字：全射程里 body 内有一枚**字面量正好等于**
`"check-ignore"` 的那个函数（把这三个字抄进 docstring 或长句子里不算，`"git check-ignore"`
这种整串也不算）——所以把它改名、把问答拆进第二个函数（那里出现同一枚字面量→"有且只有一份"
当场红）都绕不过去。两把锁的 import、调用与减法三项都按 AST 里看得到的别名与实参匹配，
只把调用点改写、不改 helper，一样红。

## 射程与残余（写清边界，不是"已穷尽"）

- 原生 `_tracked_android_kt()` 里那条 `assert not [p for p in tracked if "/build/" in p]`
  **今天合法**：它判的是"版本库里不许躺着 build 产物"（git 侧的镜子），不构造磁盘侧集合、
  不喂给任何减法，所以不在判据函数、磁盘 walk 函数与减法函数三格射程内。别把它读成"段排除
  还在当第二份真相"——磁盘侧那份已经换成问 git（见 `_native_kt_corpus` 的 docstring）。
- 网页 `_disk_text_copy()` 里 `p.suffix in _WEB_COPY_SUFFIXES` 同样今天合法：比较右边是
  **名字**不是字符串常量，而 `_WEB_COPY_SUFFIXES` 判的是"文本表面 vs 图片"那一轴，两侧
  同口径过滤、吃不进真源码，不是 gitignore 豁免那一轴。
- 挡不住的形状（诚实登记）：把名单藏进函数**默认参数**、藏进 `dict` 字面量（`{"a": 1}` 的
  key）、或写成运行时拼接（`"".join([...])`、f-string）——这些都不是"字符串模式容器"。
  要让它们变红得先扩大这一把的语法覆盖面，本票没做；下一票若要收，请连同这三条形状一起
  写进红话，别只挑一条。
- **恒真断言判据两份分家**（#268 主控人裁决：不抽共用 helper，两把锁各留一份）。代价如实登记：
  另一份住在 `backend/tests/test_v0375_mcp_stdio_supervisor.py` 的 `no_tautological_assert`
  （#266 那把）。本票已经把**并集**写全到这一侧（`_tautological_asserts` 现认裸 `assert True` /
  `assert 1` 与 `BoolOp` 里的 `or True` / `and False`），所以今天两份口径一致；但它们是两份
  独立实现、可以被单方面改窄——**"两份会再分叉"是本票登记的已知残余**，不在本票射程里。
  合并成一份要付出的代价（那把锁的射程按"函数名含 zombie"派生，抽 helper 会把这枚判据拖进
  它的派生射程）由主控人拍过，裁决是"各留一份"。

先例：`test_v0374_single_source_of_egress_predicates.py`（单源/方法级 AST 锁 +
`test_scanners_have_teeth` 那枚负控）、`test_v0350_lock_comment_stripping_scope.py`
（正面写明认得什么、认不到的逐条列成清单）。本文件的 `test_the_shape_lock_itself_has_teeth`
就是同一枚负控：缴械样本必须红、干净样本必须不红、而且两把锁那份真源码必须**一条都不报**。
"""
import ast
import inspect
import subprocess
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parents[1]

HELPER = "backend/tests/_corpus_exemption.py"
WEB_LOCK = "backend/tests/test_v0391_web_cockpit_ui.py"
NATIVE_LOCK = "backend/tests/test_v0391_native_cockpit.py"
# 形状锁自己。**如实口径（#268 改的，原文是假话）**：牙六"摘掉这一把必须有人红"今天只由
# 网页那把锁里的一枚 `test_the_exemption_shape_lock_is_present_and_tracked`
# （`backend/tests/test_v0391_web_cockpit_ui.py` 末尾）接住；**原生那把锁里没有这一枚**——
# 只在网页那侧红，就是本票登记的已知残余形状（见下面
# `test_the_shape_lock_is_registered_where_the_guarded_locks_live` 的 docstring）。
# 本票按"改注释"收这一格，不往原生那把再加一枚重复的在册钉（主控人裁决）。
# `SHAPE_LOCK` 也**不进** `_SCOPE`（理由与这条裁决一起钉在
# `test_the_scope_membership_is_the_declared_roster` 里）；这一把自己由上面那枚在册钉 +
# 本文件的自扫测试两路看着。
SHAPE_LOCK = "backend/tests/test_v0391_corpus_exemption_shape_lock.py"
_SCOPE = (HELPER, WEB_LOCK, NATIVE_LOCK)
_CALL_SITES = (WEB_LOCK, NATIVE_LOCK)
# 缴械样本的 kind 白名单：条数、id 唯一性、kind 合法性三件都由这一枚名单自己钉住
# （`test_the_disarm_roster_is_pinned_and_distinct`），注释里不写样本条数。
_DISARM_KINDS = ("predicate", "site", "helper")

GIT_FLAG = "check-ignore"
_WALK_METHODS = frozenset({"rglob", "glob", "iterdir", "listdir", "walk"})
_SEGMENT_METHODS = frozenset({"startswith", "endswith", "removeprefix", "removesuffix"})


# ---------------- 射程：从 git ls-files 现取 ----------------

def _scope_sources() -> dict:
    out = subprocess.run(["git", "ls-files", "-z", "backend/tests"], cwd=str(REPO),
                         capture_output=True, timeout=60)
    assert out.returncode == 0, \
        "问不到 `git ls-files backend/tests`：形状锁的射程读不到时不许 skip 后当绿"
    tracked = {p.decode("utf-8").replace("\\", "/") for p in out.stdout.split(b"\0") if p}
    assert tracked, "`git ls-files backend/tests` 返回 0 份：这一把的射程读歪了"
    missing = sorted(rel for rel in _SCOPE if rel not in tracked)
    assert not missing, (
        f"形状锁射程里的文件不在 `git ls-files` 里：{missing}——`_SCOPE` 名单（成员与条数由 "
        f"test_the_scope_membership_is_the_declared_roster 钉住）里任何一棵被挪走/改名/没入库，"
        f"这一把就等于不判，必须响亮红而不是安静绿")
    sources = {}
    for rel in _SCOPE:
        path = REPO / rel
        assert path.is_file(), f"{rel} 在 `git ls-files` 里却读不到：射程与磁盘不同"
        sources[rel] = path.read_text(encoding="utf-8")
    return sources


# ---------------- AST 小尺子 ----------------

def _funcs(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _is_str(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _call_str_args(node: ast.Call) -> set:
    """这个调用的**实参**里出现过的字符串常量（含 `["git", "check-ignore", …]` 那种
    列表实参——`subprocess.run` 的 argv 就是一个列表，只看顶层实参会永远取不到）。"""
    out = set()
    for arg in list(node.args) + [kw.value for kw in node.keywords]:
        for sub in ast.walk(arg):
            if _is_str(sub):
                out.add(sub.value)
    return out


def _asks_git(func) -> list:
    """`subprocess.run(...)` 且实参里带着 `"check-ignore"` 的那些调用。"""
    return [n for n in ast.walk(func)
            if isinstance(n, ast.Call) and _call_name(n) == "run"
            and GIT_FLAG in _call_str_args(n)]


def _is_rc_compare(node) -> bool:
    """`….returncode in (0, 1)` 这一形**本身**（不是包在 or/and 里的那一份）。"""
    return (isinstance(node, ast.Compare) and len(node.ops) == 1
            and isinstance(node.ops[0], ast.In)
            and isinstance(node.left, ast.Attribute) and node.left.attr == "returncode"
            and isinstance(node.comparators[0], ast.Tuple)
            and [x.value for x in node.comparators[0].elts] == [0, 1])


def _rc_whitelist(func) -> list:
    """要求存在一枚 **test 直接就是** `returncode in (0, 1)` 的 assert。

    为什么必须"直接是"而不是"子树里有"：#265 审查的缴械 c 写的就是
    `assert True or (out.returncode in (0, 1))`——形状还在、判据已经死了。只认顶层 test
    才咬得住这一形，摘掉整句同样红。
    """
    return [n for n in ast.walk(func)
            if isinstance(n, ast.Assert) and _is_rc_compare(n.test)]


def _is_bool_constant(node) -> bool:
    """这枚常量是不是"布尔味"的字面量：`True` / `False` / `1` / `0`。

    `1` 与 `0` 是 #266 那份 `no_tautological_assert` 也认的形状（`assert 1`、`assert x or 1`
    与 `assert True` 同构）。#268 把两份并集写全，所以这里只**放宽**不收窄。
    """
    return isinstance(node, ast.Constant) and isinstance(node.value, (bool, int)) \
        and not isinstance(node.value, (str, bytes))


def _tautological_asserts(func) -> list:
    """恒真/恒假断言的**并集**：断言自己宣布自己不判。

    认这几形（#268 把原来两份分家的口径写全在这里）：
    * 裸 `assert True` / `assert False` / `assert 1` / `assert 0` —— **旧版这一把漏的就是
      裸 `assert True`**，而 #266 那份从一开始就认；两份分家是本票登记的已知残余（见模块
      docstring 的"恒真断言判据两份分家"）。
    * `assert True or …` / `assert … or True` —— #265 审查缴械 c 的写法：字样还在、判据已死。
    * `assert False and …` / `assert … and False` —— 恒假，同属"自己宣布自己不判"。

    `BoolOp` 那一支维持"里面出现任何布尔味常量就算"的宽口径（比只认 `or True` 更宽），
    本票只做并集，不做收窄。
    """
    hits = []
    for n in ast.walk(func):
        if not isinstance(n, ast.Assert):
            continue
        if _is_bool_constant(n.test) or (isinstance(n.test, ast.BoolOp)
                                         and any(_is_bool_constant(x) for x in n.test.values)):
            hits.append(n.lineno)
    return hits


def _container_has_str(node) -> bool:
    """Set/Tuple/List **字面量**里（穿过同类容器）有字符串常量 ⇒ 这是一份手抄的模式名单。

    只看字面量容器：集合推导式、函数调用、f-string 都不是"名单"，否则会误伤
    `_WEB_COPY_SUFFIXES` 之外那一整批既有牙（本文件"射程与残余"写清了这一格的边界）。
    """
    if not isinstance(node, (ast.Set, ast.Tuple, ast.List)):
        return False
    stack = list(node.elts)
    while stack:
        el = stack.pop()
        if _is_str(el):
            return True
        if isinstance(el, (ast.Set, ast.Tuple, ast.List)):
            stack.extend(el.elts)
    return False


def _literal_exemption_shapes(func, predicate_only: bool = False) -> list:
    """这个函数里"豁免集合来自字面量"的形状：[(说明, lineno), ...]。

    `predicate_only=True` 多加一条只适用于判据函数的形状：给问法**补斜杠**
    （`p + "/"`）。带尾斜杠问目录会被 CRLF 检出里那枚裸 CR 空模式恒判"已忽略"
    （#267 起底的那枚假阳性，`_corpus_exemption` 的 docstring 有实测行号），
    所以那份 helper 里连这个写法都不许出现；调用点不判这条（那边 `surface + "/"`
    是射程成员判据，与 git 问答无关）。
    """
    hits = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _SEGMENT_METHODS \
                and any(_is_str(arg) for arg in node.args):
            hits.append((f"`{node.func.attr}()` 带字符串字面量＝段排除", node.lineno))
        elif isinstance(node, ast.Compare):
            # `"/build/" not in p` 与 `p in ("docs/", …)` 是同一枚段排除的两面：字符串常量
            # 可能写在 `in` 的**左边**，也可能写在右边，两边都要认。
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)) \
                        and (_is_str(comp) or _container_has_str(comp)):
                    hits.append(("`in` 比较的右边是字符串字面量/模式容器", node.lineno))
            if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops) \
                    and _is_str(node.left):
                hits.append(("`in` 比较的左边是字符串字面量＝段排除", node.lineno))
        elif predicate_only and isinstance(node, ast.BinOp) \
                and isinstance(node.op, ast.Add) \
                and any(_is_str(x) and "/" in x.value
                        for x in (node.left, node.right)):
            hits.append(("给问法补了斜杠（`p + \"/\"` 这一形）：带尾斜杠问目录会被"
                         "CRLF 检出里那枚裸 CR 空模式恒判『已忽略』", node.lineno))
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Return)):
            if node.value is not None and _container_has_str(node.value):
                hits.append(("赋值/返回的值是字符串模式容器（Set/Tuple/List 字面量）",
                             node.lineno))
    return hits


def _walks_disk(func) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr in _WALK_METHODS for n in ast.walk(func))


def _subtraction_targets(func) -> list:
    """这个函数里"结果被赋值的**集合**减法"：[(右侧节点, [目标名, ...]), ...]。

    只认 `X = A - B` 这一形，且右侧不能是数字常量——`d -= 1` 那种计数器递减在原生那把
    里有四处（`_block`/`_call`/`_call_inner_args`/`_split_top_level_args`），把它们当
    "语料扣减"就是误报。集合减法的右侧只会是名字/调用/推导式/切片，不会是数字。
    """
    out = []
    for n in ast.walk(func):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.BinOp) \
                and isinstance(n.value.op, ast.Sub) \
                and not (isinstance(n.value.right, ast.Constant)
                         and isinstance(n.value.right.value, (int, float))):
            out.append((n.value.right, [t.id for t in n.targets if isinstance(t, ast.Name)]))
    return out


def _name_bindings(func) -> dict:
    """函数内 `{名字: [赋值右侧节点, ...]}`（只认普通赋值与增强赋值的单个 Name 目标）。"""
    bindings = {}
    for n in ast.walk(func):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            bindings.setdefault(n.targets[0].id, []).append(n.value)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)) and isinstance(n.target, ast.Name) \
                and n.value is not None:
            bindings.setdefault(n.target.id, []).append(n.value)
    return bindings


def _exemption_alias(func) -> str:
    """这个函数用来接豁免判据的那个**别名**（从实际调用里取），取不到就空串。"""
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id.endswith("deliberately_ignored"):
                return f.id
            if isinstance(f, ast.Attribute) and f.attr.endswith("deliberately_ignored"):
                return f.attr
    return ""


def _alias_bindings(func, alias: str) -> list:
    """`{这个函数里由"对那份判据的调用"绑到的变量名}`，只认**带实参**的调用。

    返回值是 `[(变量名, 调用节点), ...]`：`ignored = deliberately_ignored(...)` 与
    `ignored = _IGNORED_ROSTER` 在这里能分开，而只看变量名或只看调用名都分不开。
    """
    out = []
    if not alias:
        return out
    for name, values in _name_bindings(func).items():
        for value in values:
            if isinstance(value, ast.Call) and _call_name(value) == alias and value.args:
                out.append((name, value))
    return out


def _exemption_violations(rel: str, func, predicate: str, import_map: dict) -> list:
    """对一棵"参与豁免的文件"里的函数，返回 [(说明, lineno), ...]。"""
    hits = list(_literal_exemption_shapes(func))
    for line in _tautological_asserts(func):
        hits.append(("断言被写成恒真/恒假（裸 `assert True`、`assert True or …`、"
                     "`assert … or True`、`assert … and False` 都算）", line))
    alias = _exemption_alias(func)
    if alias and alias not in import_map.get(predicate, set()):
        hits.append((f"调用的豁免函数 `{alias}` 不是从 `{_MODULE_OF_HELPER}` import 进来的那份",
                     func.lineno))
    alias_calls = _alias_bindings(func, alias)
    good = {name for name, _call in alias_calls}
    for right, targets in _subtraction_targets(func):
        if isinstance(right, ast.Call) and alias and _call_name(right) == alias and right.args:
            continue                       # 减法右边明摆着就是那份判据的调用
        if isinstance(right, ast.Name):
            if right.id in good:
                continue                   # 名字接的就是判据的返回值
            hits.append((f"扣减项 `{right.id}` 不是那份共用 helper 的返回值（目标 "
                         f"{targets}）：手抄名单/模块级常量/别处的 git 问答都能从这里绕进来"
                         f"（合法形状只有 `{alias}(…)` 或它绑到的名字）", right.lineno))
        else:
            hits.append((f"扣减项是 {type(right).__name__} 形状（不是对共用判据的调用）："
                         f"字面量名单/推导式/切片/属性都从这里溜进来", right.lineno))
    for _name, value in alias_calls:
        arg = value.args[0]
        if not isinstance(arg, (ast.Name, ast.Set, ast.List, ast.Tuple, ast.Attribute)):
            hits.append((f"喂给豁免判据的第一份参数是 {type(arg).__name__}：磁盘侧全集必须"
                         f"整份交出去问 git，不许先自己筛过一遍再问", value.lineno))
    return hits


# ---------------- 三件必备：helper 的形状 ----------------

def _predicate_function(trees: dict):
    """全射程里 body 含 `"check-ignore"` 字面量的那一个函数：[(rel, func), ...]。"""
    owners = []
    for rel, tree in trees.items():
        for func in _funcs(tree):
            if any(_is_str(n) and n.value == GIT_FLAG for n in ast.walk(func)):
                owners.append((rel, func))
    return owners


def test_the_predicate_is_one_function_in_one_file():
    sources = _scope_sources()
    trees = {rel: ast.parse(src) for rel, src in sources.items()}
    owners = _predicate_function(trees)
    assert len(owners) == 1, (
        f"问 `git check-ignore` 的函数应有且只有一份（在 helper 里），实际 "
        f"{[(rel, f.name, f.lineno) for rel, f in owners]}：两份判据从复制那一秒开始各自腐烂")
    rel, func = owners[0]
    assert rel == HELPER, f"豁免判据不住在 helper 里，跑到 {rel} 去了：两把锁又要各抄一份"
    for site in _CALL_SITES:
        assert not any(_is_str(n) and n.value == GIT_FLAG for n in ast.walk(trees[site])), (
            f"{site} 里还出现了 `\"check-ignore\"` 字样：这一问就是第二份判据，"
            f"#267 抽一份共享 helper 的意义正在被抵消")
    # helper 里别给豁免开一条字面量近路（模块级名单被函数引用也算）。
    module_lists = {t.id for n in ast.walk(trees[HELPER]) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)
                    and _container_has_str(n.value)}
    reads = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
    assert not (module_lists & reads), (
        f"helper 里那份豁免函数引用了模块级字符串名单 {sorted(module_lists & reads)}："
        f"名单是第二份真相，比 `.gitignore` 先腐烂")


def test_the_predicate_asks_git_and_whitelists_rc():
    sources = _scope_sources()
    trees = {rel: ast.parse(src) for rel, src in sources.items()}
    owners = _predicate_function(trees)
    assert len(owners) == 1, f"认领不到那唯一一份豁免判据：{[(r, f.name) for r, f in owners]}"
    rel, func = owners[0]

    assert _asks_git(func), (
        f"{rel}:{func.lineno} `{func.name}` 里没有一次把 `\"check-ignore\"` 当参数交给 "
        f"`subprocess.run` 的调用：豁免判据不再问 git，"
        f"『只问 git』这句注释当场成假话")

    assert _rc_whitelist(func), (
        f"{rel}:{func.name} 没有一枚 test 直接就是 `….returncode in (0, 1)` 的 assert"
        f"（{rel}:{func.lineno}）："
        f"摘掉它，rc=128/超时/git 不在 PATH 都会退化『没有豁免』还继续比集合，"
        f"而红话反过来宣称『已扣掉，不是漏扫』——#265 审查缴械 c 实测在 CI 与干净克隆里"
        f"都是 74 passed，所以这条判据必须钉形状而不是钉运行结果")
    tauto = _tautological_asserts(func)
    assert not tauto, (
        f"{rel}:{func.name} 第 {tauto} 行把断言写成恒真/恒假（裸 `assert True`、"
        f"`assert True or …`、`assert … and False` 都算）这一形："
        f"形状还在、判据已经死了，这正是 #265 审查缴械 c 的写法")


def test_the_predicate_takes_no_literal_shortcut():
    """第三件必备：豁免集合在 helper 里**只**由那次 git 问答成形。

    段排除（`startswith("docs/")`、`"/build/" in p`）、模式 tuple/list 常量、给问法补斜杠
    这三种写法都会让"只问 git"变成半句真话——它们与真去问 git 在今天的六种模式下行为
    完全相同（#265 审查缴械 d 实测），所以只能钉写法。
    """
    sources = _scope_sources()
    trees = {rel: ast.parse(src) for rel, src in sources.items()}
    owners = _predicate_function(trees)
    assert len(owners) == 1, f"认领不到那唯一一份豁免判据：{[(r, f.name) for r, f in owners]}"
    rel, func = owners[0]
    hits = _literal_exemption_shapes(func, predicate_only=True)
    assert not hits, (
        f"{rel}:{func.name} 里豁免集合正在从字面量成形：{hits}。这一份判据的唯一来源必须是"
        f"`.gitignore` 自己（现问 git）——手抄名单比 `.gitignore` 先腐烂，而且绿得毫无痕迹")


def test_no_disarmed_wrapper_around_the_exemption():
    """三格都不许把豁免包进 try/except 后返回一个值（#265 审查缴械 c 的另一半形状）。"""
    sources = _scope_sources()
    trees = {rel: ast.parse(src) for rel, src in sources.items()}
    owners = _predicate_function(trees)
    assert len(owners) == 1, f"认领不到那唯一一份豁免判据：{[(r, f.name) for r, f in owners]}"
    predicate_rel, predicate_func = owners[0]

    offenders = []
    for rel, tree in trees.items():
        for func in _funcs(tree):
            if (rel, func.name) == (predicate_rel, predicate_func.name):
                targets = [func]
            elif _walks_disk(func) or _subtraction_targets(func) or _exemption_alias(func):
                targets = [func, *_chain(tree, func)]
            else:
                continue
            for target in targets:
                for handler in [n for n in ast.walk(target) if isinstance(n, ast.ExceptHandler)]:
                    if any(isinstance(n, ast.Return) for n in ast.walk(handler)):
                        offenders.append(f"{rel}:{handler.lineno} 的 except 分支里 return 了"
                                         f"（{target.name}）")
    assert not offenders, (
        f"豁免判据被包成『出错就返回一个值』：{'；'.join(sorted(set(offenders)))}。"
        f"这一形读不到判据时不炸而是继续往下比集合，正是 #265 审查缴械 c 的形状")


def test_both_call_sites_take_the_exemption_from_the_helper():
    sources = _scope_sources()
    trees = {rel: ast.parse(src) for rel, src in sources.items()}
    owners = _predicate_function(trees)
    assert len(owners) == 1, f"认领不到那唯一一份豁免判据：{[(r, f.name) for r, f in owners]}"
    _, predicate_func = owners[0]
    predicate_name = predicate_func.name

    for rel in _CALL_SITES:
        tree = trees[rel]
        import_map = _imports_of(tree, predicate_name)
        assert import_map.get(predicate_name), (
            f"{rel} 没有从 `{_MODULE_OF_HELPER}` import 那份豁免判据：两把锁共用一份"
            f"这件事（#267 交付 A）不成立了")
        corpus = [f for f in _funcs(tree) if _subtraction_targets(f)]
        assert corpus, f"{rel} 里再没有『磁盘侧扣掉一份再比对』的减法：语料射程的形状变了"
        hits = []
        for func in _funcs(tree):
            if _walks_disk(func) or _subtraction_targets(func) or _exemption_alias(func):
                hits += [(rel, func.name, msg, line)
                         for msg, line in _exemption_violations(
                             rel, func, predicate_name, import_map)]
        assert not hits, (
            "豁免集合正在从字面量/别处来源成形：" + "；".join(
                f"{r}:{name} 第 {line} 行 {msg}" for r, name, msg, line in sorted(set(hits))))


def _imports_of(tree, name: str) -> dict:
    """`{判据函数名: {本模块里可用的别名}}`，只认 `from … import …` 这一形。"""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if not (module == "tests._corpus_exemption" or module.endswith("_corpus_exemption")):
                continue
            for alias in node.names:
                out.setdefault(alias.name, set()).add(alias.asname or alias.name)
    return out


_MODULE_OF_HELPER = "tests._corpus_exemption"


def _chain(tree, func):
    """返回**调用** `func` 的那些函数（判据被包成 wrapper 时，except 那一格要一起看）。"""
    parents = []
    for other in _funcs(tree):
        if other is func:
            continue
        for node in ast.walk(other):
            if isinstance(node, ast.Call) and _call_name(node) == func.name:
                parents.append(other)
                break
    return parents


# ---------------- 牙六：这一把自己也得有"摘了就红"的证据 ----------------

def test_the_shape_lock_is_registered_where_the_guarded_locks_live():
    """反向钉：这一把形状锁必须**在册**（入库 + 磁盘可读）。

    **如实口径（#268 改的；这一句原来是假话）**：被守卫的两把锁里**只有网页那把**有
    `test_the_exemption_shape_lock_is_present_and_tracked`
    （`backend/tests/test_v0391_web_cockpit_ui.py` 末尾），**原生那把里没有**。
    所以"删掉这一把会红在被守卫的那一侧"今天只对网页那一半成立：把这一把改名或删除，
    红的是网页那枚在册钉与下面那枚自扫，而不是"两把锁各有一枚"。
    残余形状（照实登记，不假装没有）：
    ① 原生那侧没有在册钉 ⇒ 一只手同时删掉网页那枚与本文件，还是会安静滑过——那是任何
    "守卫的守卫"都躲不掉的最后一格，本仓的处置办法一直是把它登记出来（见
    `test_v0374_single_source_of_egress_predicates.py` 的"仍漏"一节）；
    ② 本票按主控人裁决**改注释**收这一格，不往原生那把再加一枚重复的在册钉。
    本文件内部形状那一半由 `test_the_shape_lock_does_not_disarm_its_own_body` 接住
    （在册钉挡"整把被删/改名"，自扫挡"改它自己正文里的一句断言"）。
    """
    out = subprocess.run(["git", "ls-files", "-z", SHAPE_LOCK], cwd=str(REPO),
                         capture_output=True, timeout=60)
    assert out.returncode == 0, "问不到 `git ls-files`：这一把的在册状态读不到时不许当绿"
    listed = {p.decode("utf-8").replace("\\", "/") for p in out.stdout.split(b"\0") if p}
    assert listed == {SHAPE_LOCK}, (
        f"形状锁 {SHAPE_LOCK} 不在 `git ls-files` 里（读到 {sorted(listed)}）："
        f"守卫自己没入库，被守卫的那两把锁等于没人守")
    assert (REPO / SHAPE_LOCK).is_file(), f"{SHAPE_LOCK} 在册却读不到：射程与磁盘不同"

    # 在册钉**只**钉在册，不钉内部形状——这正是 #267 那把锁不扫自己的那格。这里顺手把它
    # 钉成一句话：这一枚里不许出现"改内部形状也红在这里"的假声明（红话不许替别人作保）。
    guard_src = inspect.getsource(test_the_shape_lock_is_registered_where_the_guarded_locks_live)
    assert "test_the_shape_lock_does_not_disarm_its_own_body" in guard_src, (
        "在册钉的 docstring 不再写明『内部形状那一半由自扫接住』：下一位读这一枚的人会把它"
        "当成两件事都管，那就是把 #267 的假话注释换个地方再写一遍")


# ---------------- 自扫：这一把自己也得受同一条恒真判据管（#268 牙四） ----------------

def test_the_shape_lock_does_not_disarm_its_own_body():
    """照 #266 的 `no_tautological_assert` 形状补的自扫——**不另造一套判据**。

    红的是哪一种隐身（#267 审查实测，本票存在理由之一）：`_SCOPE` 不含本文件、在册钉只钉
    "在册 + 磁盘可读"不钉内部形状 ⇒ 往这一把自己的正文里塞一句 `assert True or …` 或裸
    `assert True`，今天没有任何一条判据会红（守卫自己被人缴械而无人报警）。

    为什么是自扫而不是把 `SHAPE_LOCK` 塞进 `_SCOPE`（主控人裁决，实测理由成立）：
    `_walks_disk` 把 `ast.walk` 当磁盘遍历，自扫会把本文件每个 AST helper 都拖进
    `except`-return 那一格——今天绿，但将来任何人在这里写 `except: return x` 都会被
    **错理由**判红。所以这一枚只对本文件的全部**顶层**函数跑"恒真断言并集"这一条规则，
    别的规则一条都不跑。射程 `Path(__file__)`：读的就是正在被执行的那一份，改名/搬走
    都会先红在册面的名字上，而这里绝不从 `_SCOPE` 取路径（否则改一个常量就顺带把自扫
    的射程也改了）。
    """
    source = Path(__file__).resolve().read_text(encoding="utf-8")
    tree = ast.parse(source)
    top = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert top, "本文件顶层一个函数都没有：这一枚自扫等于在扫空气（恒绿的摆设）"
    offenders = []
    for func in top:
        for line in _tautological_asserts(func):
            offenders.append(f"{func.name}:{line}")
    assert not offenders, (
        f"这一把形状锁自己的正文里有恒真/恒假断言：{offenders}。守卫自己被缴械而全量无一红，"
        f"就是 #267 审查点名的那一格——判据的字样还在、判据已经不判了")


# ---------------- 成员钉：`_SCOPE` / `_CALL_SITES` 不许悄悄变窄（#268 牙五） ----------------

def test_the_scope_membership_is_the_declared_roster():
    """射程名单钉成员与条数，并把"SHAPE_LOCK 不进 `_SCOPE`"这条裁决钉在机器上。

    红的是哪一种：从 `_SCOPE` 摘掉 `HELPER`（或任一棵）、把某棵改名、把两把锁的调用点
    少写一棵 —— 这一枚**先红**，不等"认领不到判据函数"那条间接红话。今天若没有这一枚，
    摘掉 helper 那棵只会让 `_predicate_function` 数出 0 份、红话说的是"两份判据各自腐烂"，
    那是**错理由**（真发生的动作是射程缩了一棵）。

    为什么不许图省事把 `SHAPE_LOCK` 塞进 `_SCOPE`：见 `test_the_shape_lock_does_not_disarm_its_own_body`
    的 docstring（`_walks_disk` 把 `ast.walk` 当磁盘遍历）。这条裁决由下面那一枚反向钉住，
    谁改谁必须同时改判据与那段理由。
    """
    assert len(_SCOPE) == 3, (
        f"_SCOPE 现在是 {len(_SCOPE)} 棵。注释不写数（数只活在钉里），这条红话就是唯一那份"
        f"真相：helper 与两把锁的调用点少了任何一棵，这一把就少扫一棵；真要改射程，改这个数"
        f"必须同批改 _CALL_SITES、_SCOPE 的成员钉与模块 docstring 的射程段。")
    assert len(set(_SCOPE)) == len(_SCOPE), f"_SCOPE 里有重复的树：{list(_SCOPE)}"
    assert set(_SCOPE) == {HELPER, WEB_LOCK, NATIVE_LOCK}, (
        f"_SCOPE 的成员不再是『那份 helper + 两把锁的调用点』：{sorted(_SCOPE)}"
        f"——名单与常量分家就是第二份真相的开始")
    assert set(_CALL_SITES) == {WEB_LOCK, NATIVE_LOCK} and len(_CALL_SITES) == 2, (
        f"_CALL_SITES 现在是 {list(_CALL_SITES)}：调用点名单与 _SCOPE 里的两把锁分家，"
        f"test_both_call_sites_take_the_exemption_from_the_helper 就不知道自己在扫谁")
    assert SHAPE_LOCK not in _SCOPE, (
        f"SHAPE_LOCK 被塞进 _SCOPE 了：主控人裁决是自扫另开一枚，不把它并进射程"
        f"（_walks_disk 把 ast.walk 当磁盘遍历，自扫会误伤本文件每个 AST helper）。"
        f"要推翻这条裁决请先在 test_the_shape_lock_does_not_disarm_its_own_body 的 docstring "
        f"里把新理由写清，再改这一枚。")
    for rel in _SCOPE:
        assert rel.endswith(".py") and rel.startswith("backend/tests/") and "\\" not in rel, (
            f"射程名单里的 {rel} 不是『仓库相对 + posix 分隔符 + .py』的形状：{rel}"
            f"——`git ls-files` 报的就是这个口径，写成别的形状永远问不到")


# ---------------- 条数与清单钉：缴械样本不许悄悄变窄（#268 牙三） ----------------

def test_the_disarm_roster_is_pinned_and_distinct():
    """钉 `_DISARM_SAMPLES` 这份清单**本身**——换掉原来那枚恒真的计数钉。

    原来那枚是 `len(caught) == len(_DISARM_SAMPLES)`，住在 `test_the_shape_lock_itself_has_teeth`
    末尾：`caught` 在每轮循环里必然 +1（同一条循环里前面已经 `assert problems` 过），所以那个
    等式**永真**、不含任何信息——删一枚样本，它跟着变小，照样绿。规划员点名的这一格与
    `test_pathmask.py` 的 `_DRIVE_RULE_SITES`、#128 登记的是同族。

    这一枚改钉清单自己：条数下限（基数钉先例照 `test_v0391_thinking_mapping.py` 的
    `len(sources) >= 200`）、id 互不相同、kind 必须落在白名单里且每格都有样本。
    诚实登记：条数是**下限**，挡"删样本"，挡不住"加一枚没有牙的样本"——后者由
    `test_the_shape_lock_itself_has_teeth` 的 `assert problems` 逐条挡（每枚都必须真被抓到）。
    """
    assert len(_DISARM_SAMPLES) >= 13, (
        f"缴械样本只剩 {len(_DISARM_SAMPLES)} 枚，低于登记的下限 13：这一把的自证面被删窄了，"
        f"而『每条样本都被抓到』在一份同样变窄的清单上照样成立（M-B 那一格的 #267 版本）。")
    ids = [name for name, _kind, _src in _DISARM_SAMPLES]
    assert len(set(ids)) == len(ids), (
        f"缴械样本的 id 必须互不相同（重复：{sorted(i for i in ids if ids.count(i) > 1)}）："
        f"两枚同名会让人以为其中一枚已经不在清单上")
    kinds = {kind for _name, kind, _src in _DISARM_SAMPLES}
    unknown = sorted(kinds - set(_DISARM_KINDS))
    assert not unknown, (
        f"这些样本的 kind 不在白名单 {list(_DISARM_KINDS)} 里：{unknown}。"
        f"`test_the_shape_lock_itself_has_teeth` 按 kind 分流跑尺子，白名单外的 kind 会被"
        f"当成 `site` 那一格糊里糊涂地跑过去，样本抓到什么就没人知道了")
    missing = sorted(set(_DISARM_KINDS) - kinds)
    assert not missing, (
        f"这些 kind 一枚样本都没有：{missing}——`helper` 那一格本来是"
        f"『只 import 不调用』的形状，没了它这一把就不知道那种形状抓不抓得住")



# ---------------- 负控：这一把判据自己有没有牙 ----------------

_CLEAN_HELPER = '''
import subprocess


def deliberately_ignored(paths, tracked, repo):
    payload = b"\\0".join(p.encode("utf-8") for p in sorted(paths)) + b"\\0"
    out = subprocess.run(["git", "check-ignore", "--stdin", "-z"],
                         cwd=str(repo), input=payload, capture_output=True, timeout=60)
    assert out.returncode in (0, 1), "只认 0/1"
    ignored = {p.decode("utf-8") for p in out.stdout.split(b"\\0") if p}
    assert ignored <= set(paths), "报了一份没问过的"
    assert not (ignored & set(tracked)), "把已跟踪的说成已忽略"
    return ignored
'''

_CORPUS_SITE = '''
from tests._corpus_exemption import deliberately_ignored


def _disk_all(root):
    return {p.as_posix() for p in root.rglob("*.kt")}


def _corpus(root, tracked):
    on_disk_all = _disk_all(root)
    ignored = deliberately_ignored(on_disk_all, tracked, root)
    on_disk = on_disk_all - ignored
    assert on_disk == tracked, "射程与仓库事实不同"
    return on_disk
'''

# 每格：(名字, 该被哪个检查抓到, 样本源码)。
_DISARM_SAMPLES = (
    ("no-git-ask", "predicate",
     _CLEAN_HELPER.replace('"check-ignore", ', "").replace('"--stdin", ', "")),
    ("no-rc-whitelist", "predicate",
     'import subprocess\n\n\ndef deliberately_ignored(paths, tracked, repo):\n'
     '    out = subprocess.run(["git", "check-ignore", "--stdin", "-z"], input=b"a",\n'
     '                         capture_output=True, timeout=60)\n'
     '    return {p.decode() for p in out.stdout.split(b"\\0") if p}\n'),
    ("except-returns-empty", "predicate",
     'import subprocess\n\n\ndef deliberately_ignored(paths, tracked, repo):\n'
     '    try:\n'
     '        out = subprocess.run(["git", "check-ignore", "--stdin", "-z"], input=b"a",\n'
     '                             capture_output=True, timeout=60)\n'
     '        assert out.returncode in (0, 1), "只认 0/1"\n'
     '    except Exception:\n'
     '        return set()\n'
     '    return {p.decode() for p in out.stdout.split(b"\\0") if p}\n'),
    ("literal-roster", "predicate",
     'import subprocess\n\n\ndef deliberately_ignored(paths, tracked, repo):\n'
     '    roster = ("docs/a.md", "docs/b.md", "docs/c.md")\n'
     '    return {p for p in paths if p.startswith("docs/")}\n'),
    ("trailing-slash-segment", "predicate",
     'import subprocess\n\n\ndef deliberately_ignored(paths, tracked, repo):\n'
     '    out = subprocess.run(["git", "check-ignore", "--stdin", "-z"], input=b"a",\n'
     '                         capture_output=True, timeout=60)\n'
     '    assert out.returncode in (0, 1), "只认 0/1"\n'
     '    return {p for p in paths if "/" + p + "/" in p}\n'),
    ("tautological-rc", "predicate",
     'import subprocess\n\n\ndef deliberately_ignored(paths, tracked, repo):\n'
     '    out = subprocess.run(["git", "check-ignore", "--stdin", "-z"], input=b"a",\n'
     '                         capture_output=True, timeout=60)\n'
     '    assert True or (out.returncode in (0, 1)), "只认 0/1"\n'
     '    return {p.decode() for p in out.stdout.split(b"\\0") if p}\n'),
    ("roster-union-keeps-git", "predicate",
     'import subprocess\n\n\n'
     '_SIX_FAMILIES = ("docs/v0.24-", "docs/代码体检报告-", "docs/待修复清单-")\n\n\n'
     'def deliberately_ignored(paths, tracked, repo):\n'
     '    out = subprocess.run(["git", "check-ignore", "--stdin", "-z"], input=b"a",\n'
     '                         capture_output=True, timeout=60)\n'
     '    assert out.returncode in (0, 1), "只认 0/1"\n'
     '    ignored = {p.decode() for p in out.stdout.split(b"\\0") if p}\n'
     '    return ignored | {p for p in paths if p in _SIX_FAMILIES}\n'),
    ("site-build-segment-filter", "site",
     'from tests._corpus_exemption import deliberately_ignored\n\n\n'
     'def _corpus(root, tracked):\n'
     '    on_disk_all = {p.as_posix() for p in root.rglob("*.kt")\n'
     '                  if "/build/" not in p.as_posix()}\n'
     '    ignored = deliberately_ignored(on_disk_all, tracked, root)\n'
     '    on_disk = on_disk_all - ignored\n'
     '    assert on_disk == tracked, "不同"\n'
     '    return on_disk\n'),
    ("site-own-roster", "site",
     _CORPUS_SITE.replace("    ignored = deliberately_ignored(on_disk_all, tracked, root)\n",
                          "    ignored = _IGNORED_ROSTER & on_disk_all\n")),
    ("site-attribute-exemption", "site",
     _CORPUS_SITE.replace("    on_disk = on_disk_all - ignored\n",
                          "    on_disk = on_disk_all - self.roster\n")),
    ("site-indirection", "site",
     _CORPUS_SITE.replace("ignored = deliberately_ignored(on_disk_all, tracked, root)",
                          "ignored = _lookup(on_disk_all, tracked, root)")),
    ("site-wrapper-except", "site",
     'from tests._corpus_exemption import deliberately_ignored\n\n\n'
     'def _safe(paths, tracked, root):\n'
     '    try:\n'
     '        return deliberately_ignored(paths, tracked, root)\n'
     '    except Exception:\n'
     '        return set()\n'
     '\n'
     '\n'
     'def _corpus(root, tracked):\n'
     '    on_disk_all = {p.as_posix() for p in root.rglob("*.kt")}\n'
     '    ignored = _safe(on_disk_all, tracked, root)\n'
     '    on_disk = on_disk_all - ignored\n'
     '    assert on_disk == tracked, "不同"\n'
     '    return on_disk\n'),
    ("helper-name-only", "helper",
     'from tests._corpus_exemption import deliberately_ignored\n\n\n'
     'def _corpus(root, tracked):\n'
     '    assert deliberately_ignored\n'),
)


def _predicate_findings(src: str) -> list:
    """对一个 helper 形状的样本，跑三件必备 + 字面量形状，返回问题清单。"""
    tree = ast.parse(src)
    owners = [(HELPER, f) for f in _funcs(tree)
              if any(_is_str(n) and n.value == GIT_FLAG for n in ast.walk(f))]
    problems = []
    if len(owners) != 1:
        problems.append("判据函数不止一份或一份都没有")
        return problems
    _, func = owners[0]
    if not _asks_git(func):
        problems.append("没有 subprocess.run(..., \"check-ignore\")")
    if not _rc_whitelist(func):
        problems.append("没有 test 直接就是 returncode in (0, 1) 的断言（被 or/and 包住也算没有）")
    if _tautological_asserts(func):
        problems.append(f"恒真断言 {_tautological_asserts(func)}")
    if any(isinstance(n, ast.ExceptHandler)
           and any(isinstance(r, ast.Return) for r in ast.walk(n))
           for n in ast.walk(func)):
        problems.append("except 分支里 return")
    if _literal_exemption_shapes(func, predicate_only=True):
        problems.append(f"字面量形状 {len(_literal_exemption_shapes(func, predicate_only=True))} 处")
    module_lists = {t.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                    for t in node.targets if isinstance(t, ast.Name)
                    and _container_has_str(node.value)}
    if module_lists & {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}:
        problems.append("判据函数引用了模块级字符串名单")
    return problems


def _site_findings(src: str) -> list:
    """对一个调用点形状的样本，跑减法来源 + 字面量形状，返回问题清单。"""
    tree = ast.parse(src)
    import_map = _imports_of(tree, "deliberately_ignored")
    aliases = import_map.get("deliberately_ignored", set())
    problems = []
    corpus = [f for f in _funcs(tree) if _subtraction_targets(f)]
    if not corpus:
        problems.append("没有减法比对")
    for func in _funcs(tree):
        if _walks_disk(func) or _subtraction_targets(func) or _exemption_alias(func):
            for msg, line in _exemption_violations(
                    WEB_LOCK, func, "deliberately_ignored", import_map):
                problems.append(f"{func.name}:{line} {msg}")
            for handler in [n for n in ast.walk(func) if isinstance(n, ast.ExceptHandler)]:
                if any(isinstance(r, ast.Return) for r in ast.walk(handler)):
                    problems.append(f"{func.name} 的 except 分支里 return")
            for holder in _chain(tree, func):
                for handler in [n for n in ast.walk(holder) if isinstance(n, ast.ExceptHandler)]:
                    if any(isinstance(r, ast.Return) for r in ast.walk(handler)):
                        problems.append(f"{holder.name} 的 except 分支里 return")
    return problems


def test_the_shape_lock_itself_has_teeth():
    """每一枚缴械样本都必须被抓到，干净样本必须一条都不报——否则这一把没有牙。

    清单本身的条数/id/kind 三件不在这里判：`caught` 在这个循环里必然每轮 +1，所以原来那枚
    `len(caught) == len(_DISARM_SAMPLES)` **永真**、不含信息（#268 规划员点名换掉的那格）。
    它现在住在 `test_the_disarm_roster_is_pinned_and_distinct` 里，钉的是清单自己。
    """
    for name, kind, src in _DISARM_SAMPLES:
        try:
            ast.parse(src)
        except SyntaxError as exc:                       # 样本写歪了是本文件的错，不是判据的功劳
            raise AssertionError(f"缴械样本 {name} 连语法都不成立：{exc}") from exc
        problems = _predicate_findings(src) if kind == "predicate" else _site_findings(src)
        assert problems, (
            f"缴械样本 {name}（该被 `{kind}` 那一格抓到）没被抓到：这一把形状锁没有牙")

    assert _predicate_findings(_CLEAN_HELPER) == [], \
        f"干净 helper 样本被误报：{_predicate_findings(_CLEAN_HELPER)}"
    assert _site_findings(_CORPUS_SITE) == [], \
        f"干净调用点样本被误报：{_site_findings(_CORPUS_SITE)}"

    # 最后一格：两把锁与 helper 那份**真源码**必须一条都不报——否则这一把是恒真的摆设。
    sources = _scope_sources()
    trees = {rel: ast.parse(src) for rel, src in sources.items()}
    owners = _predicate_function(trees)
    assert len(owners) == 1, f"真源码里认领不到判据函数：{[(r, f.name) for r, f in owners]}"
    predicate_rel, predicate_func = owners[0]
    real = list(_literal_exemption_shapes(predicate_func))
    for rel in _CALL_SITES:
        import_map = _imports_of(trees[rel], predicate_func.name)
        for func in _funcs(trees[rel]):
            if _walks_disk(func) or _subtraction_targets(func) or _exemption_alias(func):
                real += _exemption_violations(rel, func, predicate_func.name, import_map)
    assert not real, f"真源码被这一把误报（它就是不会红的摆设）：{real}"


# ---------------- 正对照：加宽后的恒真判据在真源码上必须 0 命中（#268 牙七） ----------------

# 这一枚恒真判据今天扫得到的全部真源码。名单是**派生射程之外**的一张显式表（同 #257 的
# 口径：表与它的用法住在同一个文件里，改窄的人会同时改掉用它的断言），列进来的必须是
# "被守卫的 + 守卫自己的"两侧：helper、本文件（#267 那把）、#266 那把形状锁所在的那棵树、
# 两把语料锁。少列一棵 ⇒ 下面那枚在册断言红。
_TAUTO_SCAN_TARGETS = (
    HELPER,
    SHAPE_LOCK,
    "backend/tests/test_v0375_mcp_stdio_supervisor.py",
    WEB_LOCK,
    NATIVE_LOCK,
)


def _tracked_tests() -> set:
    out = subprocess.run(["git", "ls-files", "-z", "backend/tests"], cwd=str(REPO),
                         capture_output=True, timeout=60)
    assert out.returncode == 0, \
        "问不到 `git ls-files backend/tests`：正对照的射程读不到时不许当绿"
    tracked = {p.decode("utf-8").replace("\\", "/") for p in out.stdout.split(b"\0") if p}
    assert tracked, "`git ls-files backend/tests` 返回 0 份：正对照的基数钉读不到仓库事实"
    return tracked


def test_the_widened_tautology_predicate_is_quiet_on_real_sources():
    """#268 牙七（正对照，不许漏）：并集写全之后的恒真判据，在真源码上必须**一条都不报**。

    规划员静态扫过说全绿——**这一格不接受转述**，所以把它做成常驻测试而不是一次性脚本：
    五棵真源码（helper、本文件、#266 那把形状锁那棵树、两把语料锁）的全部顶层函数，
    一条都不许命中。

    红的是哪一种坏法：把判据做得**过宽**（比如连 `assert x == 1` 那种比较也算进去、或者
    把"任何带常量的断言"当成恒真），于是真源码成片红，而一片红的守卫活不过下一位接手的
    人——他会整条摘掉它。#266 那把的语料里期望值为 `()` 的那几枚挡的是同一个方向，
    这一枚是它在本把这边的对应物。反向同样有用：本票把裸 `assert True` 并进并集之后，
    真源码里如果原本就有裸 `assert True`，这里会立刻告诉我是哪一棵哪一格。
    """
    tracked = _tracked_tests()
    missing = sorted(rel for rel in _TAUTO_SCAN_TARGETS if rel not in tracked)
    assert not missing, (
        f"正对照名单里有不入库的树：{missing}——扫不到的那一棵等于没被对照过，"
        f"『真源码 0 命中』这句话就只剩一半证据")
    offenders = []
    for rel in _TAUTO_SCAN_TARGETS:
        path = REPO / rel
        assert path.is_file(), f"{rel} 在册却读不到：正对照的射程与磁盘不同"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        top = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        assert top, f"{rel} 顶层一个函数都没有：这一枚正对照在扫空气"
        for func in top:
            for line in _tautological_asserts(func):
                offenders.append(f"{rel}:{func.name}:{line}")
    assert not offenders, (
        f"加宽后的恒真判据在真源码上命中 {len(offenders)} 处：{offenders[:8]}。要么判据宽过头"
        f"（过度报警的守卫会被整条摘掉），要么真源码里真的混进了恒真断言（那就是本票要挡的"
        f"那一格）——两种都得当场处理，不许留着当噪声")
