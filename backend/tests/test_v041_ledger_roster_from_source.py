"""限流账清单：从**源码**派生，与运行期 _LEDGERS 双向相等（#279 / #289，2026-11-29）。

为什么这条锁必须从源码派生，不能读运行期
----------------------------------------
#289 的实测形状：某条锁「从源码派生」改成「读 `auth_router._LEDGERS`」之后，**没有人拦**。
听起来像还没发生的事，但它有一个具体后果——

  新写一本账、忘了把它登记进 `_LEDGERS`，那么：

  · 读运行期的锁：看到的是**登记过的那几本**，忘了登记的那本不在里面，锁**必然绿**
    （它检查的对象本来就是它读的那份清单）。
  · 从源码派生的锁：源码里有 11 个 `_X = _Ledger("...") `，运行期 `_LEDGERS` 只有 10 条
    → 少了谁一目了然。

换句话说，**读运行期的那版锁，对"漏登记"这种错天然盲**——它和被检查的东西是同一个事实。
而漏登记正是这类地基的失败形状：漏扫的那本账只胀不收，没有任何可观察症状（限流照样生效），
只有在常年不重启的进程里才会长出去，而本机是开机自启的。

所以本文件：
  ① 用 AST 从 `app/core/auth_router.py` 里抽出所有 `_X = _Ledger("<name>")` 的绑定；
  ② 与 `_LEDGERS` 里的账本对象集合做**双向**比对（源码多了 → 漏登记；少了 → 幽灵条目）；
  ③ 再把「每条登记都带非零窗口」判一遍（窗口为 0 = 只认不剪，同一族错）。

为什么不能从 `_LEDGERS` 自己派生
--------------------------------
那会恒真。上面那段已经说了理由，这里再钉一次：**派生源必须与被检查对象不同**。这条
纪律在 `test_pathmask.py::_drive_rule_literals` 与 `test_ci_history_depth.py` 的
`PINNED_HISTORY_ANCHORS` 里都有先例，本文件抄同一套。

射程（实测范围）
---------------
- 只认模块级 `NAME = _Ledger(...)` 这种赋值形状。写成 `_Ledger` 的列表推导、或把账本
  建在别的模块再 import 进来，本锁看不见（今天两者都不存在，出现时这条会先红，因为
  源码侧数目会对不上——除非两种写法同时出现，那时需要人工看一眼）。
- 不判「每本账的窗口值是否合理」——那归 `test_selfcheck.py::test_real_ledgers_all_carry_a_window`。
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[2]
AUTH_ROUTER = REPO / "backend" / "app" / "core" / "auth_router.py"

# 条数钉：11 是 2026-11-29 从源码数出来的（对照 `grep -c '= _Ledger("'`），手抄字面量。
# 不从下面那个集合派生——派生即恒真，砍掉一本账时它跟着变短、永远不红。
_PINNED_LEDGER_COUNT = 11


def _source_level_ledger_bindings():
    """从源码 AST 抽出 {变量名: "账本名"}。

    只认 `NAME = _Ledger("literal")`。返回 (绑定字典, 问题列表)；问题非空时调用方应判红——
    因为那说明这份源码里出现了本判据读不懂的账本定义形状，**不能当作"没有"处理**。
    """
    problems = []
    tree = ast.parse(AUTH_ROUTER.read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not (isinstance(func, ast.Name) and func.id == "_Ledger"):
            continue
        if len(call.args) != 1 or not isinstance(call.args[0], ast.Constant) \
                or not isinstance(call.args[0].value, str):
            problems.append(target.id + " = _Ledger(...) 的第一个实参不是字符串字面量")
            continue
        found[target.id] = call.args[0].value
    if not found:
        problems.append("源码里一个 _Ledger(...) 绑定都没读到：AST 判据抓空了，下面所有断言都会恒绿")
    return found, problems


def test_the_source_ledger_roster_parses_and_is_not_empty():
    """先证明判据读得到东西。读不到就不是绿，是瞎。"""
    bindings, problems = _source_level_ledger_bindings()
    assert not problems, "源码派生出问题：" + "；".join(problems)
    assert len(bindings) == _PINNED_LEDGER_COUNT, (
        "源码里的账本绑定是 " + str(len(bindings)) + " 本，条数钉要的是 " + str(_PINNED_LEDGER_COUNT)
        + " 本。增减账本请同时改这条钉，并把 #279/#289 的单据一起更新。")


def test_every_source_ledger_is_registered_in_the_prune_roster():
    """源码里定义的每一本账，都必须在 `_LEDGERS` 里登记（否则只胀不收）。

    这是 #289 真正要的那一半：**漏登记**必须红。读运行期 `_LEDGERS` 的锁对这件事天然盲。
    """
    from app.core import auth_router

    bindings, problems = _source_level_ledger_bindings()
    assert not problems, "源码派生出问题：" + "；".join(problems)

    registered = {id(ledger) for ledger, _window in auth_router._LEDGERS}
    missing = []
    for var_name in bindings:
        ledger = getattr(auth_router, var_name)
        if id(ledger) not in registered:
            missing.append(var_name + ' ("' + bindings[var_name] + '")')
    assert not missing, (
        "这些账本在源码里定义了、但没登记进 _LEDGERS：" + "、".join(missing)
        + "。漏扫的那本只胀不收，而且没有任何可观察症状（限流照样生效）——"
        "只有在常年不重启的进程里才会长出去，而本机是开机自启的。")


def test_the_prune_roster_has_no_ghost_entries():
    """反向：`_LEDGERS` 里不许有源码里已经删掉的幽灵账本。"""
    from app.core import auth_router

    bindings, problems = _source_level_ledger_bindings()
    assert not problems, "源码派生出问题：" + "；".join(problems)

    known = {id(getattr(auth_router, name)) for name in bindings}
    ghosts = [ledger for ledger, _window in auth_router._LEDGERS if id(ledger) not in known]
    assert not ghosts, (
        "_LEDGERS 里有源码中已不存在的账本对象：" + repr(ghosts)
        + "——幽灵条目会让 _window_for 抛 RuntimeError，或让剪枝为一个死对象空转。")


def test_every_registered_ledger_carries_a_positive_window():
    """每条登记都要带非零窗口：窗口 0 = 只认不剪，与漏登记同一族的错。"""
    from app.core import auth_router

    bad = [(getattr(ledger, "name", ledger), window)
           for ledger, window in auth_router._LEDGERS
           if not window or window <= 0]
    assert not bad, "这些账本登记的窗口不是正数：" + repr(bad)


def test_the_roster_lock_can_go_red_on_a_missing_registration():
    """变异锁：**删掉一条登记**，本文件的判据必须红。

    没有这条，上面那几条「双向相等」在"永远相等"的实现下也会常绿。
    做法是把 _LEDGERS 换成少一条的元组，然后跑同一个派生判据——这一条不需要 pytest
    也能跑，因为 monkeypatch 只用来还原。
    """
    from app.core import auth_router

    original = auth_router._LEDGERS
    assert len(original) >= 2, "账本太少，砍一条就空了，这条变异测不出东西"
    try:
        auth_router._LEDGERS = tuple(original[:-1])
        try:
            test_every_source_ledger_is_registered_in_the_prune_roster()
        except AssertionError:
            pass                          # 红 = 判据有牙
        else:
            raise AssertionError(
                "砍掉一条登记之后，'每本账都登记了' 这条判据仍然绿——它读的是被砍过的"
                "那一份，于是对漏登记天然盲。这正是 #289 的形状。")
    finally:
        auth_router._LEDGERS = original
