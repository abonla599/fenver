"""工单 #80：给已经落地的 D1 各补一把**会红的锁**（读数点形状锁 + `⏸`/`SUSPEND_PREFIX` 禁字锁）。

D1 是这条纪律：**全站只许一种"停在断点 / 成败"的判据 = 读结构化那两格**
（`outcome.ok` / `outcome.suspended`，唯一赋值点 `executor.py:197` 与 `:181`）。代码侧 #76a/#76b
已经把四个读数点改对了；本文件**不改任何判据**，只负责"改回去的那一刻必须红"。

为什么必须是形状锁 + 禁字锁，而不是再补一把行为测试（两轮独立实跑，结论都是
2417 passed / 14 skipped / rc=0）：
* #76a 的审查员把四处判据换成 `startswith("⏸ ")` —— 全量绿；
* #76b 的审查员把 `react_agent.py` 那一处摘回父版的 `startswith("✓")` —— 全量照样绿。
根因不在测试写得薄，而在**等价性是执行器替所有读法兜住的**：`executor.py:251-256` 那道
fail-closed 先把"挂起却自报 success=True"的矛盾形状折成失败，`executor.py:82` 的
`clip_for_model` 又是**保头**裁剪（预算 4000、省略说明约 57 字 ⇒ head≈2600），前缀那两三个
字符永远裁不掉。于是在真执行器下面"文本前缀判据"与"结构化判据"今天恒等 ——
**这一族回归在行为层面结构上不可见**，只有读源码形状的锁读得到。审查员那句
"造不出锁"只对行为锁成立，形状锁与禁字锁造得出来，就是本文件这两把。

两把锁各自红的是哪一种回归：
1. **形状锁**（`test_d1_read_point_*` 那三条）：四个读数点里任何一处把 `ok=` / `suspended=`
   摘掉、换成位置写法、裸读 `outcome.ok`（少了 `bool(...)` 那一层）、换成**别的变量**
   （`ok=tool_ok`）或任何从文本前缀算出来的值 —— 立刻红，文案给出 `文件:行号` 与
   "应为 `ok=bool(outcome.ok)`、实得 …"。
2. **禁字锁**（`test_d1_pause_prefix_*` 那三条）：`backend/app/**` 里除 `tools/response.py`
   之外任何 `.py` 出现 `⏸` 或 `SUSPEND_PREFIX` —— 立刻红。它备的是"下一处新违规"：
   今天这一枚前缀只有一处合法落点，明天有人在 registry.py 里 `startswith("⏸ ")`、或到别处
   import 那个常量，本条就拦住。

两把锁都配**同一套判据本体的合成反例**（两条 `*_can_go_red` 测试）：判据先在现造的源码上
被证明报得出红，才许采信它对真源码的绿灯（本仓规矩：一条从没红过的锁不算交付，参照
`test_machine_read_pointers.py` 的"豁免引擎自己也要有牙"那一族）。反例走 `ast.parse` 现造的
字符串，**不改任何源文件**。真实文件的四枚变异实验另在 #80 的 commit 正文里逐条记录。

不许碰的两条承重（动了上面那段"恒等"的论证就得重写）：`executor.py:251-256` 的 fail-closed、
`executor.py:82` 的保头裁剪。本文件只读源码、不 import 任何 app 模块，所以它与 `data/` 之间
没有可写路径。

范围与口径上的四个取舍，写死在这里免得下一个人猜：
* **行号只是实测记录，不是锚点。** 四个读数点与 `response.py` 那两处（定义 / 渲染）在 #80
  落锁时逐一实读复核过（无漂移，见各条断言文案），但定位一律走 AST —— 本仓有 #44/#51 那一族
  "按列 0 字面锚导致 try 内两行无法常规缩进"的排版债，行号锚会把一次无害的缩进改动判成红。
* **只管 `backend/app/**` 的 `.py`。** 测试侧那三处 `startswith("⏸ ")`
  （`test_v0350_76a_suspend_consumer_third_state.py`，实读在 :262/:311/:362）断的是**回灌给
  模型的文本**、不是记账判据，合法，不在射程内。壳侧 `backend/app/web/**` 的 js/html 属于
  渲染消费者（第三态的帧形状归 A4 与工单 #82），落锁时实测那批文件里一枚 `⏸` 都没有，本条
  也不把它们算进集合 —— 把渲染层判红等于替 #82 预先决定帧形状。
* **docstring/注释里的"提及"走显式豁免名单，不许用 `pytest.skip` 糊。** 名单
  （`NON_CODE_EXEMPTIONS`）每条必须写明理由，而且只豁免**非代码位置**：一行的代码段里出现
  禁字，名单救不了它（`test_d1_pause_prefix_lock_can_go_red` 的第四枚反例专门钉这一点）。
  为什么不 skip：#56/#63 那一族已经付过钱 —— skip 让覆盖静默消失，而"没有违规"和"什么都没
  扫到"在全绿里长得一模一样。
* **第 ③ 项那两条注释（挂起这一支往帧里报 `ok:false`、第三态帧形状属 #82）不在本文件的锁
  范围内**：#82 一落地就要重写那两句的取值，把它们钉成锁只会让 #82 必须先改锁。那一处仍只由
  复核保证，写在这里而不是假装锁覆盖了它。
"""
import ast
import io
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "backend" / "app"

# ---------------------------------------------------------------- 锁一：四个读数点的形状
# 落锁时实读复核（无漂移）：pipeline.py:306-307、streaming.py:478-479、react_agent.py:212、
# task_agent.py:178-179。这份名单是**清单**而不是锚点：条数与每处形状一律由 AST 核对。
READ_POINT_FILES = (
    "backend/app/pipeline.py",
    "backend/app/core/streaming.py",
    "backend/app/agents/react_agent.py",
    "backend/app/agents/task_agent.py",
)
OUTCOME_VAR = "outcome"
RECORD_METHOD = "record_outcome"
PREFIX_BUILDER = "startswith"
# (关键字名, 该读的那一格) —— 两个关键字实参必须逐字是 bool(outcome.ok) / bool(outcome.suspended)
STRUCTURED_SLOTS = (("ok", "ok"), ("suspended", "suspended"))
EXPECTED_READ_POINT_COUNT = 4


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _tree(rel: str, source: str) -> ast.Module:
    return ast.parse(source, filename=rel)


def _record_outcome_calls(tree: ast.Module) -> list:
    """这个文件里所有 `…record_outcome(…)` 调用节点（`guard.record_outcome` 与裸调用都收）。"""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == RECORD_METHOD:
            out.append(node)
        elif isinstance(func, ast.Name) and func.id == RECORD_METHOD:
            out.append(node)
    return out


def _is_structured_read_point(call: ast.Call) -> bool:
    """这一处调用是不是"记账读数点"。

    判据是两条任一成立：带了 `ok=` / `suspended=` 任一关键字；或实参子树里直接读了
    `outcome.ok` / `outcome.suspended`（把结构化那格改成位置写法也算读数点，照样锁死）。

    **故意不收**的那一处：`react_agent.py:217` 的兜底异常支 `guard.record_outcome(tool_name, False)`。
    那里压根没有 outcome 这个对象（异常不是挂起），而 `guardrails.py:156-159` 写明了
    `suspended` 是关键字限定参数、位置第三位留给 `estimated_tokens` —— 把它算进读数点等于逼
    那条支传一个它没有的东西。它仍然受下面"不许把前缀喂进记账"那一条的管束。
    """
    if {kw.arg for kw in call.keywords if kw.arg} & {"ok", "suspended"}:
        return True
    for sub in ast.walk(call):
        if isinstance(sub, ast.Attribute) and sub.attr in ("ok", "suspended") \
                and isinstance(sub.value, ast.Name) and sub.value.id == OUTCOME_VAR:
            return True
    return False


def _slot_shape_ok(node: ast.expr, slot_attr: str) -> bool:
    """值必须逐字是 `bool(outcome.<那一格>)`：bool(...) 只有一层、单参、无关键字、属性名对上。"""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == "bool"
            and not node.keywords and len(node.args) == 1
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == slot_attr
            and isinstance(node.args[0].value, ast.Name)
            and node.args[0].value.id == OUTCOME_VAR)


def _read_point_shape_problems(rel: str, source: str) -> list:
    """形状锁的判据本体：回问题清单，空清单 = 这份源码过关。"""
    tree = _tree(rel, source)
    problems = []
    for call in _record_outcome_calls(tree):
        if not _is_structured_read_point(call):
            continue
        kws = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        for kw_name, slot_attr in STRUCTURED_SLOTS:
            expected = f"{kw_name}=bool({OUTCOME_VAR}.{slot_attr})"
            if kw_name not in kws:
                problems.append(
                    f"{rel}:{call.lineno}-{call.end_lineno} 读数点少了关键字实参 {expected}"
                    f"（整调用实得 `{ast.unparse(call)}`）—— 红的就是『把这一格摘掉、退回"
                    "两参 / 纯 ok 写法』那一种回归")
                continue
            if not _slot_shape_ok(kws[kw_name], slot_attr):
                problems.append(
                    f"{rel}:{call.lineno} 读数点的 {kw_name}= 不再是结构化那一格："
                    f"应为 {expected}，实得 {kw_name}={ast.unparse(kws[kw_name])}"
                    " —— 红的就是『换成文本前缀判据 / 换成别的变量 / 裸读 outcome.ok』那一种回归")
    return problems


def _prefix_builder_used(node: ast.AST):
    """这棵子树里有没有 `startswith(...)` 的调用。回 None 或那次调用的源码。"""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Attribute) and func.attr == PREFIX_BUILDER:
            return ast.unparse(sub)
        if isinstance(func, ast.Name) and func.id == PREFIX_BUILDER:
            return ast.unparse(sub)
    return None


def _assignments_from_prefix(tree: ast.Module, rel: str, name: str) -> list:
    """这个文件里把 `name` 赋成"前缀判据"的那几处（`tool_ok = str(x).startswith("✓")`）。

    只做**一跳**：#76b 那枚真实反例就是"先算一个变量、再把它喂进记账"，一跳足够抓到它。
    再往上的数据流分析属于过度工程，这层边界写在这里，而不是假装它不存在。
    """
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        used = _prefix_builder_used(node.value)
        if used:
            hits.append(f"{rel}:{node.lineno} {name} = {used}")
    return hits


def _prefix_feeding_problems(rel: str, source: str) -> list:
    """形状锁的第二半（禁字锁在记账这一侧的孪生判据）：这四个读数点文件里，任何
    `record_outcome(...)` 都不许被文本前缀的布尔结果喂进去 —— 直接喂，或经由一跳变量赋值喂。"""
    tree = _tree(rel, source)
    problems = []
    for call in _record_outcome_calls(tree):
        args = list(call.args) + [kw.value for kw in call.keywords]
        for arg in args:
            used = _prefix_builder_used(arg)
            if used:
                problems.append(
                    f"{rel}:{call.lineno} 直接把 `{used}` 的结果喂进 guard.record_outcome(...) "
                    f"（整调用 `{ast.unparse(call)}`）—— D1 要禁的正是这个形状")
            elif isinstance(arg, ast.Name):
                hops = _assignments_from_prefix(tree, rel, arg.id)
                if hops:
                    problems.append(
                        f"{rel}:{call.lineno} 喂进 guard.record_outcome(...) 的变量 {arg.id} "
                        f"来自文本前缀判据：{'；'.join(hops)}")
    return problems


def _structured_read_points() -> list:
    """真源码上的读数点普查：回 [(rel, call)]，供"条数 / 每文件都有"那条正对照用。"""
    found = []
    for rel in READ_POINT_FILES:
        tree = _tree(rel, _read(rel))
        for call in _record_outcome_calls(tree):
            if _is_structured_read_point(call):
                found.append((rel, call))
    return found


# 形状锁的合成反例：#76b 审查员实测过的那一枚变异（ok 摘回 `startswith("✓")`）、#76a 那一族的
# "摘掉 suspended="、以及"结构化两格改成位置写法"。判据必须先对它们报得出红，才许采信绿灯。
_SYNTH_PREFIX_READ = '''
def run(guard, name, outcome, tool_result):
    tool_ok = str(tool_result).startswith("\u2713")
    guard.record_outcome(name, ok=tool_ok, suspended=bool(outcome.suspended))
'''
_SYNTH_MISSING_SUSPENDED = '''
def run(guard, name, outcome):
    guard.record_outcome(name, ok=bool(outcome.ok),)
'''
_SYNTH_POSITIONAL_STRUCTURED = '''
def run(guard, name, outcome):
    guard.record_outcome(name, outcome.ok, outcome.suspended)
'''
_SYNTH_CORRECT_SHAPE = '''
def run(guard, name, outcome):
    guard.record_outcome(name, ok=bool(outcome.ok), suspended=bool(outcome.suspended))
'''


def test_d1_read_point_lock_finds_the_four_reading_points():
    """正对照（防空转）：四个读数点文件里必须恰好扫出四处结构化读数点，每个文件各一处。

    红的是哪一种回归：读数点被**整行删掉**、被搬去一个新文件、或形状改成"既没关键字也不读
    outcome.*"从而连分类都不进了。这三种坏法都会让条数离开 4 —— 那时本条必须红，否则
    "形状全对"和"一处都没扫到"在全绿里根本分不开（本仓为这个形状付过钱，见
    `test_machine_read_pointers.py::test_the_pointer_scanner_reads_enough_real_pointers`）。
    第五处读数点真出现时本条也会红：那是设计 —— 加读数点必须同时改 `READ_POINT_FILES` 与
    期望条数，改判据就必须动锁。
    """
    found = _structured_read_points()
    per_file = {}
    for rel, call in found:
        per_file.setdefault(rel, []).append(call.lineno)
    assert len(found) == EXPECTED_READ_POINT_COUNT, (
        f"D1 读数点应当恰好 {EXPECTED_READ_POINT_COUNT} 处，实得 {len(found)} 处，实得明细 "
        f"{per_file}。要么有人摘掉/删了一处结构化读数点，要么新增的第五处没登记 —— "
        "新增读数点必须同时改 READ_POINT_FILES 与本条期望值")
    missing = [rel for rel in READ_POINT_FILES if rel not in per_file]
    assert not missing, f"这些文件一处读数点都没扫到（判据被删或被搬走）：{missing}"
    for rel, lines in per_file.items():
        assert len(lines) == 1, f"{rel} 扫出 {len(lines)} 处读数点（行号 {lines}），应为 1 处"


def test_d1_read_points_are_the_two_structured_slots():
    """形状锁主判据：四处读数点的两个关键字实参必须逐字是 `ok=bool(outcome.ok)` 与
    `suspended=bool(outcome.suspended)`。

    红的是哪一种回归：摘掉 `suspended=`（挂起从此被记成一次失败，两次就把这个工具在本轮熔断）、
    摘掉 `ok=`、换成位置写法、裸读 `outcome.ok`、换成任何别的变量或表达式 —— 也就是两轮实测里
    "行为全绿而判据已经改回文本前缀"的那一族。
    """
    problems = []
    for rel in READ_POINT_FILES:
        problems += _read_point_shape_problems(rel, _read(rel))
    assert not problems, "D1 读数点形状已被改动：\n" + "\n".join(problems)


def test_d1_read_points_are_never_fed_by_a_text_prefix():
    """形状锁的第二半：四个读数点文件里没有任何 `record_outcome(...)` 被 `startswith(...)` 的
    结果喂进去（直接喂，或经由 `tool_ok = str(...).startswith("✓")` 这类一跳变量喂）。

    红的是哪一种回归：#76a/#76b 两轮审查用的那两枚变异本体 —— 把判据换成
    `startswith("⏸ ")` / `startswith("✓")`。上一条锁已经会红，这一条是**孪生保险**：万一将来
    有人绕过形状（把前缀结果藏进一个再套一层的变量），只要账是从文本前缀算出来的，本条就红。
    """
    problems = []
    for rel in READ_POINT_FILES:
        problems += _prefix_feeding_problems(rel, _read(rel))
    assert not problems, "有记账调用被文本前缀判据喂了：\n" + "\n".join(problems)


def test_d1_read_point_shape_lock_can_go_red():
    """形状锁的牙齿（合成反例，不改任何源文件）：三枚已知坏形状必须报红，正确形状必须不报。

    没有这一条，前两把锁可能永远是空壳：判据一旦写错方向（`kws` 取错、
    `_is_structured_read_point` 恒 False），真源码全绿而人看不出来。这里把变异形状直接喂给
    **同一套判据本体**，红了才算这把锁被验过。
    """
    fake = "backend/app/synthetic_shape.py"
    for label, source, expect_red in (
            ("ok 摘回 startswith(\"✓\")（#76b 实测过的那枚）", _SYNTH_PREFIX_READ, True),
            ("suspended= 被摘掉（#76a 那一族）", _SYNTH_MISSING_SUSPENDED, True),
            ("结构化两格改成位置写法", _SYNTH_POSITIONAL_STRUCTURED, True),
            ("逐字正确的形状", _SYNTH_CORRECT_SHAPE, False)):
        problems = _read_point_shape_problems(fake, source)
        fed = _prefix_feeding_problems(fake, source)
        if expect_red:
            assert problems or fed, f"反例「{label}」没有报红 —— 这把锁是空壳：{source!r}"
        else:
            assert not problems and not fed, f"正确形状被误判：{problems + fed}"
    # "摘 suspended="那枚必须红在**缺关键字**这一句，不能只是顺带红
    only_missing = _read_point_shape_problems(fake, _SYNTH_MISSING_SUSPENDED)
    assert any("少了关键字实参 suspended=bool(outcome.suspended)" in p for p in only_missing), (
        f"摘掉 suspended= 的反例红的位置不对（本条要钉的就是这一格）：{only_missing}")


# ---------------------------------------------------------------- 锁二：禁字锁
PAUSE_GLYPH = "⏸"          # U+23F8
# 刻意比 `"⏸ "` 宽一格：扫的是那枚字符本身，`startswith("⏸")`（漏了尾随空格）也得红。
FORBIDDEN_TOKENS = (PAUSE_GLYPH, "SUSPEND_PREFIX")
# 唯一合法落点：定义（实读 :21）与渲染（实读 :87）都住在这里。
SOLE_PREFIX_OWNER = "backend/app/tools/response.py"

# docstring/注释里的"提及"豁免名单：每条必须写明理由，并且只豁免**非代码位置**。
# 匹配按 (文件, 这句话) 而不是行号 —— 行号锚会让"在上面插一句注释"把豁免判成过期条目
# （#81 那一族旧坐标无人钉就是同一个病）。
NON_CODE_EXEMPTIONS = (
    {
        "path": "backend/app/tools/executor.py",
        "must_contain": "文本投影读不出第三态的完整形状",
        "kind": "函数 docstring",
        "reason": "`execute_tool` 的散文在**描述**挂起时文本投影长什么样（那句 "
                  "`⏸ 需要用户输入：…`），它既不读这个前缀、也不是任何判据的输入；#80 落锁时"
                  "实读在 executor.py:154。这句话一改、或这段散文被搬进可执行代码，本条立刻红 ——"
                  " 豁免只覆盖『非代码位置 + 原句未改』这一种情况，不覆盖整个文件。",
    },
)


def _comment_spans(source: str) -> dict:
    """{行号: [(起始列, 结束列)]}：tokenize 只把真 `#` 注释算注释，字符串里的 `#` 不算。"""
    spans = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                spans.setdefault(tok.start[0], []).append((tok.start[1], tok.end[1]))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return spans


def _docstring_rows(source: str) -> set:
    """docstring（模块/类/函数体首的字符串常量）覆盖的行号集合。"""
    rows = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return rows
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            const = body[0].value
            rows.update(range(const.lineno, (const.end_lineno or const.lineno) + 1))
    return rows


def _is_non_code(row: int, col: int, comments: dict, docstring_rows: set) -> bool:
    """这一枚禁字是不是落在注释/docstring 里 —— **按列**判，不按行。

    按行判会漏掉 `if x.startswith("⏸ "):  # 只是注释` 这一种形状：同一行既有代码又有注释，
    而那一枚禁字在代码段里。那是本锁最该抓的坏法，不能让它从行级判定里溜走。
    """
    if row in docstring_rows:
        return True
    for start, end in comments.get(row, ()):
        if start <= col <= max(end, start + 1):
            return True
    return False


def _forbidden_hits(rel: str, source: str) -> list:
    """这份源码里每一枚禁字：回 [{row, col, token, line}]（位置给到列，判非代码要用）。"""
    hits = []
    for row, line in enumerate(source.splitlines(), start=1):
        for token in FORBIDDEN_TOKENS:
            col = line.find(token)
            while col != -1:
                hits.append({"row": row, "col": col, "token": token, "line": line})
                col = line.find(token, col + len(token))
    return hits


def _exemption_for(rel: str, line_text: str):
    for entry in NON_CODE_EXEMPTIONS:
        if entry["path"] == rel and entry["must_contain"] in line_text:
            return entry
    return None


def _prefix_lock_problems(rel: str, source: str) -> list:
    """禁字锁的判据本体（rel 不是那个唯一豁免文件时）：每一枚禁字必须**同时**满足
    "落在非代码位置" 与 "登记在豁免名单上"，才放得过。"""
    comments = _comment_spans(source)
    docstring_rows = _docstring_rows(source)
    problems = []
    for hit in _forbidden_hits(rel, source):
        if _is_non_code(hit["row"], hit["col"], comments, docstring_rows) \
                and _exemption_for(rel, hit["line"]):
            continue
        where = f"{rel}:{hit['row']} 列 {hit['col']}"
        text = hit["line"].strip()
        if _is_non_code(hit["row"], hit["col"], comments, docstring_rows):
            problems.append(
                f"{where} 在非代码位置（注释/docstring）提到 `{hit['token']}` 却没登记在豁免"
                f"名单：`{text}` —— 要么把这句话改写得不带禁字，要么在 NON_CODE_EXEMPTIONS 加"
                "一条**写明理由**的条目（不许用 pytest.skip 糊，本仓明文规矩）")
        else:
            problems.append(
                f"{where} 在**可执行代码**里消费了 `{hit['token']}`：`{text}` —— D1 只许 "
                f"{SOLE_PREFIX_OWNER} 渲染这个前缀，豁免名单救不了代码位置（这条就是为"
                "『下一处新违规』准备的）")
    return problems


def _scanned_py_files() -> list:
    """扫描集合：`backend/app/**/*.py` 去掉那个唯一豁免文件。`__pycache__` 里没有 `.py`，
    而 glob 只收源码 —— 陈旧字节码（#77 那一族假红）进不了这个集合。"""
    return sorted(_rel(p) for p in APP_DIR.rglob("*.py") if _rel(p) != SOLE_PREFIX_OWNER)


def test_d1_pause_prefix_lock_anchor_and_corpus():
    """正对照（防空转）：唯一豁免文件里"定义 + 渲染"两处必须逐字还在，而且扫描集合真的很大、
    四个读数点文件全在里面。

    红的是哪一种回归：① 那个常量被改名或搬走（禁字锁守的东西从此不存在）；② 定义的字面量不再
    是 `"⏸ "`（第三态前缀被换掉，而锁还在守一个旧串）；③ `SUSPEND_PREFIX` 在 response.py 里被
    多处消费（一枚前缀出现两个真相源）；④ `backend/app` 的目录形状变了，锁什么都扫不到还一路
    全绿。
    """
    source = _read(SOLE_PREFIX_OWNER)
    tree = _tree(SOLE_PREFIX_OWNER, source)
    definitions = [n for n in tree.body
                   if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "SUSPEND_PREFIX" for t in n.targets)]
    assert len(definitions) == 1, (
        f"{SOLE_PREFIX_OWNER} 里 `SUSPEND_PREFIX` 的模块级定义应当恰好一处，实得 "
        f"{len(definitions)} 处 —— 定义搬家了，禁字锁的豁免口径得重新谈")
    value = definitions[0].value
    assert isinstance(value, ast.Constant) and value.value == PAUSE_GLYPH + " ", (
        f"定义必须逐字是 SUSPEND_PREFIX = \"{PAUSE_GLYPH} \"（实读在 "
        f"{SOLE_PREFIX_OWNER}:{definitions[0].lineno}），实得 `{ast.unparse(value)}`")
    # ctx=Load 才算消费：定义那一侧的 Name 是 Store，否则这里会数成两处
    uses = [n for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id == "SUSPEND_PREFIX" and isinstance(n.ctx, ast.Load)]
    assert len(uses) == 1, (
        f"渲染侧对 `SUSPEND_PREFIX` 的消费应当恰好一处（实读在 {SOLE_PREFIX_OWNER}:"
        f"{uses[0].lineno if uses else '?'} 那一支 f-string），实得 {len(uses)} 处 —— "
        "第三态前缀不许有两个真相源")
    corpus = _scanned_py_files()
    assert len(corpus) >= 50, (
        f"禁字锁只扫到 {len(corpus)} 个源文件，多半是扫描根坏了（backend/app 的目录形状变了）")
    for rel in READ_POINT_FILES:
        assert rel in corpus, f"四个读数点之一的 {rel} 不在扫描集合里 —— 形状锁与禁字锁脱了钩"
    assert SOLE_PREFIX_OWNER not in corpus


def test_d1_pause_prefix_never_consumed_outside_response_py():
    """禁字锁主判据：`backend/app/**` 里除 `tools/response.py` 之外，任何 `.py` 出现 `⏸` 或
    `SUSPEND_PREFIX` 一律红 —— 可执行代码位置必红；注释/docstring 位置只有登记在
    `NON_CODE_EXEMPTIONS` 并写明理由才放得过。

    红的是哪一种回归：有人在别处 `startswith("⏸ ")` 把它当记账判据（D1 破防的第一种写法），
    或从 response 里 import 那个常量自己拼前缀 / 比对前缀（第二种写法，而且它会跟着渲染器一起
    腐烂）。已知那一处 docstring 提及（executor.py 那句）今天就是靠名单才过得去。
    """
    problems = []
    for rel in _scanned_py_files():
        source = _read(rel)
        if any(token in source for token in FORBIDDEN_TOKENS):
            problems += _prefix_lock_problems(rel, source)
    assert not problems, (
        f"`{PAUSE_GLYPH}` / `SUSPEND_PREFIX` 出了唯一合法落点 {SOLE_PREFIX_OWNER}：\n"
        + "\n".join(problems))


def test_d1_pause_prefix_exemption_list_is_all_still_true():
    """豁免名单自己不许腐烂：每条必须**今天仍然命中**该文件里恰好一行、那一行确实含禁字、
    而且那一行确实被判为非代码（豁免条目声称它是注释/docstring，就得真是）。

    红的是哪一种回归：那句散文被改写或删除（条目变成 blanket 免检）、有人把禁字从注释搬进
    代码却还指着名单说"这条豁免过"。名单条目一旦不再命中就必须红 —— 一张和现实脱节的名单比
    没有名单更坏（本仓为"注册了一把不存在的锁"那一族付过钱，见 #61）。
    """
    assert NON_CODE_EXEMPTIONS, "豁免名单为空却声称在豁免 executor 那句 docstring —— 名单被删空了"
    for entry in NON_CODE_EXEMPTIONS:
        rel = entry["path"]
        assert rel != SOLE_PREFIX_OWNER, f"唯一豁免文件不该出现在名单里：{rel}"
        source = _read(rel)
        lines = source.splitlines()
        matched = [i for i, ln in enumerate(lines, start=1) if entry["must_contain"] in ln]
        assert len(matched) == 1, (
            f"豁免条目（{rel} · {entry['kind']}）应当恰好命中一行，实得 {matched} 行 —— "
            "那句话已经改了/搬了，条目要么更新要么摘掉")
        row = matched[0]
        text = lines[row - 1]
        assert any(token in text for token in FORBIDDEN_TOKENS), (
            f"豁免条目 {rel}:{row} 那行已不含禁字，条目该摘掉：{text!r}")
        problems = _prefix_lock_problems(rel, source)
        still_red = [p for p in problems if p.startswith(f"{rel}:{row} ")]
        assert not still_red, (
            f"豁免条目 {rel}:{row} 声称是 {entry['kind']}，判据却仍判它红：{still_red}")
        assert entry["reason"].strip(), f"豁免条目必须写明理由：{entry}"


def test_d1_pause_prefix_lock_can_go_red():
    """禁字锁的牙齿（合成反例，不改任何源文件）：四种坏法必须报红，登记过的非代码提及必须不报。

    ① 代码里 `startswith("⏸ ")`（#76a 那枚变异的合成版）；② 别处 import 那个常量（第四枚变异
    的合成版）；③ 没登记过的注释提及；④ **同一行既有代码又有注释**
    （`if name.startswith("⏸ "):  # 只是注释`）—— 第四种专钉"按行判注释"这个偷懒写法，
    那样本锁最该抓的形状会被它放过。
    """
    fake = "backend/app/tools/synthetic_consumer.py"
    cases = (
        ("代码消费禁字", f'def f(name):\n    return name.startswith("{PAUSE_GLYPH} ")\n'),
        ("别处 import 常量", "from app.tools.response import SUSPEND_PREFIX\n"),
        ("未登记的注释提及", f"# 这一支以 {PAUSE_GLYPH} 开头\nVALUE = 1\n"),
        ("代码段带禁字而行尾有注释",
         f'def f(name):\n    if name.startswith("{PAUSE_GLYPH} "):  # 只是注释\n'
         "        return True\n"),
    )
    for label, source in cases:
        problems = _prefix_lock_problems(fake, source)
        assert problems, f"反例「{label}」没有报红 —— 这把锁是空壳：{source!r}"
    # 与真源码同形状的绿对照：登记过的那句（executor.py 的 docstring）必须判得过
    assert not _prefix_lock_problems("backend/app/tools/executor.py",
                                     _read("backend/app/tools/executor.py"))
