"""工单 #80：给已经落地的 D1 各补一把**会红的锁**（读数点形状锁 + `⏸`/`SUSPEND_PREFIX` 禁字锁）。

D1 是这条纪律：**全站只许一种"停在断点 / 成败"的判据 = 读结构化那两格**
（`outcome.ok` / `outcome.suspended`，唯一赋值点 `executor.py:197` 与 `:181`）。代码侧 #76a/#76b
已经把四个读数点改对了；本文件**不改 app 侧任何判据**，只负责"改回去的那一刻必须红"。

工单 #83 动的是**这两把锁自己**的四件事，逐条都在本机 `5aab5573` 上实跑过（app 代码零改动）：
① 把"任何 `record_outcome(...)` 都不许被前缀喂 / 再套一层也红"那句收窄成**一跳之内**，并把
"两跳实测静默漏"如实写进 `test_d1_read_points_are_never_fed_by_a_text_prefix`；
② 更正本文件指 `test_v0350_76a_…` 的三枚自指旧坐标（`:262/:311/:362` → 实读
`:271/:321/:372`），并给出"测试互引行号要不要降级成不指行号"的建议；
③ 给禁字锁补上 `chr(9208)` / `chr(0x23f8)` / `\\u23f8` 这类**构造写法**（补之前它们全绿），
同时写死"文本级判据挡不住运行时拼接"这条边界，不声称已穷尽；
④ 更正"`backend/app/web/**` 不纳入扫描"那句 —— 集合按 `.py` 收，web 下那 2 个 `.py`
（`web_router.py`、`__init__.py`）**一直在集合内**；本单把声明改对、集合不动。

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

两把锁各自红的是哪一种回归（#83 把两处口径收到**实测**上，原文写的是"任何 … 就红"）：
1. **形状锁**（`test_d1_read_point_*` 那三条）：**带着 `ok=` / `suspended=` 关键字**的读数点里
   任何一处把 `ok=` / `suspended=` 摘掉、换成位置写法、裸读 `outcome.ok`（少了 `bool(...)`
   那一层）、换成**别的变量**（`ok=tool_ok`）或任何从文本前缀算出来的值 —— 立刻红，文案给出
   `文件:行号` 与"应为 `ok=bool(outcome.ok)`、实得 …"。因为口径是**逐字**那个形状，这一条对
   变量间接的**套层深度不敏感**（#83 实测：`feed_a = str(...).startswith("✓")` →
   `feed_b = feed_a` → `ok=feed_b` 两跳喂进 `react_agent.py:212`，红的那句是
   "读数点的 ok= 不再是结构化那一格：应为 `ok=bool(outcome.ok)`，实得 `ok=feed_b`"）。
   这份"逐字"确实**比语义严**：`ok=outcome.ok`（去掉 `bool(...)` 包裹）也会红 ——
   `_slot_shape_ok` 要的是那一次 `bool` 调用，裸读是 `Attribute` 不是 `Call`（#80 审查员实测过
   这枚变异）。这是**有意付的维护税**，换一把确定的牙；"顺手放宽成允许裸读"等于把这把锁做回
   没牙的样子，#83 明确不做。
   **它不收**的那一类：既不带你那两格属性、也不带关键字的**纯位置写法**（
   `react_agent.py:217` 兜底支那种）—— 那一侧只有下面孪生判据的**一跳**射程，见
   `test_d1_read_points_are_never_fed_by_a_text_prefix` 的边界段。
2. **禁字锁**（`test_d1_pause_prefix_*` 那三条）：`backend/app/**` 里除 `tools/response.py`
   之外任何 `.py` 出现 `⏸`、`SUSPEND_PREFIX`，或 #83 补上的**构造写法** `chr(9208)` /
   `chr(0x23f8)` / `\\u23f8` —— 立刻红。它备的是"下一处新违规"：今天这一枚前缀只有一处合法
   落点，明天有人在 registry.py 里 `startswith("⏸ ")`、或到别处 import 那个常量、或改用
   `chr(9208) + " "` 拼一个看起来一样的串，本条就拦住。**边界（#83 实测后写死）**：这是
   文本级判据，守的是"源码里看得见的写法"，运行时才拼出来的串（从数据库/配置读出来的前缀、
   `chr(n)` 里 `n` 是变量、`chr( 9208 )` 这种加空格的写法）一律在射程外 —— **不声称已穷尽**。

两把锁都配**同一套判据本体的合成反例**（两条 `*_can_go_red` 测试）：判据先在现造的源码上
被证明报得出红，才许采信它对真源码的绿灯（本仓规矩：一条从没红过的锁不算交付，参照
`test_machine_read_pointers.py` 的"豁免引擎自己也要有牙"那一族）。反例走 `ast.parse` 现造的
字符串，**不改任何源文件**。真实文件的四枚变异实验另在 #80 的 commit 正文里逐条记录。

不许碰的两条承重（动了上面那段"恒等"的论证就得重写）：`executor.py:251-256` 的 fail-closed、
`executor.py:82` 的保头裁剪。本文件只读源码、不 import 任何 app 模块，所以它与 `data/` 之间
没有可写路径。

范围与口径上的五个取舍，写死在这里免得下一个人猜：
* **行号只是实测记录，不是锚点。** 四个读数点与 `response.py` 那两处（定义 / 渲染）在 #80
  落锁时逐一实读复核过（无漂移，见各条断言文案），但定位一律走 AST —— 本仓有 #44/#51 那一族
  "按列 0 字面锚导致 try 内两行无法常规缩进"的排版债，行号锚会把一次无害的缩进改动判成红。
* **只管 `backend/app/**` 的 `.py`。** 这一条 #83 改了两句**比实测宽**的话（审查员实测 + 本单
  复现），逐句对齐如下：
  ① 测试侧那三处 `startswith("⏸ ")` 断的是**回灌给模型的文本**、不是记账判据，合法，不在射程内。
     三处都在 `test_v0350_76a_suspend_consumer_third_state.py`，且是同一句
     `assert tool_msgs and tool_msgs[-1]["content"].startswith("⏸ ")` —— #83 自己 grep 复核的
     实读坐标是 **:276 / :326 / :377**（#80 原文写的 `:262/:311/:362` 是它改完 76a 文件头之后
     当场漂掉的旧坐标；本单改 76a 文件头又把它往下推了 5 行，所以这里的数按**提交后**的状态记）。
     行号仍只是实测记录，认那**句话的字面文本**才是口径，理由见下条。
  ② 扫描集合是 `backend/app/**/*.py` 去掉唯一豁免文件，**按扩展名收**。#83 实读
     `backend/app/web/` 下有 2 个 `.py`（`web_router.py`、`__init__.py`）**就在集合内** ——
     #80 原文"本条也不把它们算进集合"是**假陈述**（比声明严不会假绿，但下一个人会照着它推理），
     所以本单把**声明改对、集合不动**（收紧集合会真的少测两个文件）。真正不在集合里的只有
     web 那批**非 `.py`** 的 js/html：它们属渲染消费者（第三态的帧形状归 A4 与工单 #82），
     落锁时实测那批文件里一枚 `⏸` 都没有；把渲染层判红等于替 #82 预先决定帧形状。
* **测试文件之间互引行号，建议降级成"不指行号"的描述（本单只把上面那三枚坐标改对，不铺全仓）。**
  理由：会漂的数没人钉就迟早变假 —— 同一族病在本仓已经挂着 #81/#46/#53 三张单，而本条这一处
  漂掉的间隔不是一个月，是**一单**（#80 自己改的被引文件）。可复跑的替代口径这个文件里已经有了：
  `NON_CODE_EXEMPTIONS` 按 **(文件, 那句话的字面片段)** 命中，并由
  `test_d1_pause_prefix_exemption_list_is_all_still_true` 钉住"今天仍然命中恰好一行" —— 改一句
  话就红，比行号锚又便宜又准。建议后续单把测试互引统一换成"字面片段 + 命中数"，行号只作为注释
  里的实测记录保留并写明会漂。这一处属**待拍板**，本单不动别的文件。
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
    结果喂进去 —— 射程是**一跳之内**：直接喂，或经由 `tool_ok = str(...).startswith("✓")`
    这类**一层**变量喂。

    红的是哪一种回归：#76a/#76b 两轮审查用的那两枚变异本体 —— 把判据换成
    `startswith("⏸ ")` / `startswith("✓")`。上一条锁已经会红，这一条是**孪生保险**。

    **边界（#83 按实测收窄；原文那句"把前缀结果藏进一个再套一层的变量……本条就红"是假的）**：
    `_assignments_from_prefix` 只做一跳（它自己的 docstring 早写明了"只做**一跳**"，与原文
    自相矛盾），所以"前缀结果再套一层变量"这条坏法**本条判不出来**。#83 在本机 `5aab5573`
    复现过两枚：把 `react_agent.py` 兜底支那处改成
    `fallback_ok = str(tool_result).startswith("✓")` → `fallback_feed = fallback_ok` →
    `guard.record_outcome(tool_name, fallback_feed)`（**两跳**）⇒ 本文件 8 条全绿、rc=0，静默漏；
    同一处只套一层（**一跳**）⇒ 本条红，文案点是 `…的变量 fallback_ok 来自文本前缀判据`。
    ⇒ 这一单的处置是**措辞对齐实测**，判据射程一律不动。

    为什么接受这个边界，以及剩下那一半靠什么兜：
    * 它**不是无人守**。那四处结构化读数点由上一条形状锁按**逐字** `ok=bool(outcome.ok)` /
      `suspended=bool(outcome.suspended)` 把关 —— 变量无论套几层都不可能等于那个字面形状，
      所以"两跳以上的间接"在**带关键字**的读数点上照样红。本条只是它的孪生保险，不是唯一防线。
    * 真漏的那一格只剩一种形状：**既不读 `outcome.*` 也不带 `ok=`/`suspended=` 关键字的纯位置
      写法**（`_is_structured_read_point` 按口径不收，例如 `guard.record_outcome(tool_name,
      False)` 那处兜底支）**并且**间接两跳以上。要抓它得做数据流分析，与本文件
      `_assignments_from_prefix` 那句"再往上的数据流分析属于过度工程"同一口径。
    * 扩成多跳匹配（顺带决定"纯位置写法算不算第五类读数点"）是**待拍板**事项，本单不动判据，
      只把话说对 —— 过度声称比边界小更坏：它会让人以为那一族回归已经被牙咬住了。
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
PAUSE_GLYPH = "⏸"          # U+23F8 == 码点 9208
# 刻意比 `"⏸ "` 宽一格：扫的是那枚字符本身，`startswith("⏸")`（漏了尾随空格）也得红。
#
# #83 补上的**构造写法**两枚（`chr(...)` 与 `\u23f8` 转义）。为什么要补：审查员实测、本单在本机
# `5aab5573` 复现过 —— 旧口径只认 `⏸` / `SUSPEND_PREFIX` 这两种**字面文本**，于是把
# `chr(9208) + " "` 或 `"\u23f8 "` 放进 `backend/app/**` 里，本锁 8 条全绿（rc=0）：同一枚字符
# 换个写法就绕过去，那不是"D1 允许这么写"，那是漏。识别表按**小写**比对，故 `chr(0x23F8)`、
# `"\u23F8"` 这类同码点的大小写变体一样进射程（十进制 9208 与十六进制 0x23f8 都收，见
# `test_d1_pause_prefix_lock_anchor_and_corpus` 里"两枚数字必须仍指同一枚字符"那枚正对照）。
CONSTRUCTED_PAUSE_TOKENS = ("chr(9208)", "chr(0x23f8)", "\\u23f8")
FORBIDDEN_TOKENS = (PAUSE_GLYPH, "SUSPEND_PREFIX") + CONSTRUCTED_PAUSE_TOKENS
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
    """这份源码里每一枚禁字：回 [{row, col, token, line}]（位置给到列，判非代码要用）。

    比对**不分大小写**（#83 起）：构造写法那几枚带十六进制字母（`chr(0X23F8)`、`"\\u23F8"`），
    而这条锁禁的是"那一枚码点 / 那个常量的消费"，不是"恰好同一串大小写的字"。今天真源码里
    `SUSPEND_PREFIX` 只有唯一合法落点那两处（#83 实读：定义 :21 + 渲染 :87，且在豁免文件内），
    所以放宽到大小写不敏感**不带来任何新的误红**；会带来的那一点是**更严**（`suspend_prefix`
    这种小写别名也进射程）。
    """
    hits = []
    for row, line in enumerate(source.splitlines(), start=1):
        lowered = line.lower()
        for token in FORBIDDEN_TOKENS:
            needle = token.lower()
            col = lowered.find(needle)
            while col != -1:
                hits.append({"row": row, "col": col, "token": token, "line": line})
                col = lowered.find(needle, col + len(needle))
    return hits


def _source_has_forbidden_token(source: str) -> bool:
    """整份源码里有没有任何一枚禁字 —— 与 `_forbidden_hits` 同一套大小写口径（只做预筛）。"""
    lowered = source.lower()
    return any(token.lower() in lowered for token in FORBIDDEN_TOKENS)


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
    全绿；⑤ #83 新增 —— 构造写法识别表里那几枚**数字**不再指向 `PAUSE_GLYPH` 这一枚字符
    （前缀换掉了而锁还在守 9208），或那张表被删空；⑥ web 那两枚 `.py` 被从集合里排除掉，
    而文件头的声明说的是"集合按 `.py` 收、它们在 inside"。
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
    # #83：构造写法那几枚必须由 PAUSE_GLYPH **推出**，不是抄一组对得上今天的数字 ——
    # 前缀哪天换码点，表里那三枚必须跟着红，否则这把锁守的是上一个字符。
    codepoint = ord(PAUSE_GLYPH)
    for needle in (f"chr({codepoint})", f"chr(0x{codepoint:x})", f"\\u{codepoint:04x}"):
        assert needle in CONSTRUCTED_PAUSE_TOKENS, (
            f"`PAUSE_GLYPH` 是 U+{codepoint:04X}，而构造写法识别表里没有 `{needle}` —— "
            "表与守的字符脱钩了（#83 补的就是这一族漏）")
    assert CONSTRUCTED_PAUSE_TOKENS, "构造写法识别表被删空 —— #83 补的那两枚等于没补"
    corpus = _scanned_py_files()
    assert len(corpus) >= 50, (
        f"禁字锁只扫到 {len(corpus)} 个源文件，多半是扫描根坏了（backend/app 的目录形状变了）")
    for rel in READ_POINT_FILES:
        assert rel in corpus, f"四个读数点之一的 {rel} 不在扫描集合里 —— 形状锁与禁字锁脱了钩"
    assert SOLE_PREFIX_OWNER not in corpus
    # #83 更正过文件头那句"web 不算进集合"（实为**在**集合内）；这里把更正后的事实钉住，
    # 免得下一个人口头"照声明"把 web/ 排除掉 —— 真要排除是允许的，但那必须同时改这份声明。
    for rel in ("backend/app/web/web_router.py", "backend/app/web/__init__.py"):
        assert rel in corpus, (
            f"{rel} 不在扫描集合里，与文件头 #83 更正后的声明相反（集合按 `.py` 收，"
            "web 下那两枚 `.py` 在集合内）")


def test_d1_pause_prefix_never_consumed_outside_response_py():
    """禁字锁主判据：`backend/app/**` 里除 `tools/response.py` 之外，任何 `.py` 出现 `⏸`、
    `SUSPEND_PREFIX`，或 #83 补上的构造写法 `chr(9208)` / `chr(0x23f8)` / `\\u23f8` 一律红 ——
    可执行代码位置必红；注释/docstring 位置只有登记在 `NON_CODE_EXEMPTIONS` 并写明理由才放得过。

    红的是哪一种回归：有人在别处 `startswith("⏸ ")` 把它当记账判据（D1 破防的第一种写法），
    或从 response 里 import 那个常量自己拼前缀 / 比对前缀（第二种写法，而且它会跟着渲染器一起
    腐烂），或 #83 实测过的那第三种：`chr(9208) + " "` / `"\\u23f8 "` 这种**把同一枚字符构造出来**
    的写法 —— 补之前旧口径只认字面文本，本条对它是绿的（静默漏）。已知那一处 docstring 提及
    （executor.py 那句）今天就是靠名单才过得去。

    **边界（不许再写成"已穷尽"）**：这是**文本级**判据，射程是"源码里看得见的写法"。运行时才
    拼出来的串挡不住，也本来就不该由它挡：前缀从数据库/配置/常量表里读出来再拼接、`chr(n)` 的
    `n` 是个变量、`chr( 9208 )` 这种在括号里加空格的写法、以及任何非 `.py` 的落点（js/html 走
    #82 那条渲染锁）—— 都在射程外。真要把 D1 守成"任何写法"，得的是运行时探针或数据流分析，
    那是另一把锁、另一张单，不是把这句话改响一点。
    """
    problems = []
    for rel in _scanned_py_files():
        source = _read(rel)
        if _source_has_forbidden_token(source):
            problems += _prefix_lock_problems(rel, source)
    assert not problems, (
        f"`{PAUSE_GLYPH}` / `SUSPEND_PREFIX` / 构造写法 {CONSTRUCTED_PAUSE_TOKENS} "
        f"出了唯一合法落点 {SOLE_PREFIX_OWNER}：\n" + "\n".join(problems))


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
        assert _source_has_forbidden_token(text), (
            f"豁免条目 {rel}:{row} 那行已不含禁字，条目该摘掉：{text!r}")
        problems = _prefix_lock_problems(rel, source)
        still_red = [p for p in problems if p.startswith(f"{rel}:{row} ")]
        assert not still_red, (
            f"豁免条目 {rel}:{row} 声称是 {entry['kind']}，判据却仍判它红：{still_red}")
        assert entry["reason"].strip(), f"豁免条目必须写明理由：{entry}"


def test_d1_pause_prefix_lock_can_go_red():
    """禁字锁的牙齿（合成反例，不改任何源文件）：九种坏法必须报红，登记过的非代码提及必须不报。

    ① 代码里 `startswith("⏸ ")`（#76a 那枚变异的合成版）；② 别处 import 那个常量（第四枚变异
    的合成版）；③ 没登记过的注释提及；④ **同一行既有代码又有注释**
    （`if name.startswith("⏸ "):  # 只是注释`）—— 第四种专钉"按行判注释"这个偷懒写法，
    那样本锁最该抓的形状会被它放过。
    ⑤⑥⑦⑧⑨ 是 #83 补的**构造写法**反例（十进制 `chr(9208)`、大写十六进制 `chr(0X23F8)`、
    `\\u23f8` 与 `\\u23F8` 两种转义、以及注释里提一句构造写法）：这五枚在补锁之前**全都是绿的**
    —— 审查员实测过、本单在本机 `5aab5573` 拿真文件复现过（临时未跟踪探针放进
    `backend/app/agents/` 里，旧口径 8 passed / rc=0，新口径每一条都点名到那一枚构造写法）。
    这里额外要求"红在那一枚上"（`expected in problems`），并且要求主判据那条的**预筛**
    `_source_has_forbidden_token` 也看得见 —— 预筛与命中判据口径不一致时，真源码那一路会整条
    跳过，锁就白补了。
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
    # #83 补的五枚构造反例：必须红，而且必须红在**那一枚构造写法**上（顺带钉住预筛同口径）。
    constructed_cases = (
        ("chr(9208) 十进制拼前缀", 'PAUSE = chr(9208) + " "\n', "chr(9208)"),
        ("chr(0X23F8) 大写十六进制拼前缀", 'PAUSE = chr(0X23F8) + " "\n', "chr(0x23f8)"),
        ("\\u23f8 转义（小写）", 'PAUSE = "\\u23f8 "\n', "\\u23f8"),
        ("\\u23F8 转义（大写，靠不分大小写命中）", 'PAUSE = "\\u23F8 "\n', "\\u23f8"),
        ("注释里提构造写法却没登记", "# 这一支用 chr(9208) 拼\nVALUE = 1\n", "chr(9208)"),
    )
    for label, source, expected in constructed_cases:
        problems = _prefix_lock_problems(fake, source)
        assert problems, f"构造反例「{label}」没有报红 —— #83 那两枚等于没补：{source!r}"
        assert any(expected in p for p in problems), (
            f"构造反例「{label}」红错了地方（本条要钉的就是 `{expected}` 这一枚）：{problems}")
        assert _source_has_forbidden_token(source), (
            f"构造反例「{label}」被主判据的预筛漏掉了 —— 真源码那一路会整条跳过：{source!r}")
    # 与真源码同形状的绿对照：登记过的那句（executor.py 的 docstring）必须判得过
    assert not _prefix_lock_problems("backend/app/tools/executor.py",
                                     _read("backend/app/tools/executor.py"))
