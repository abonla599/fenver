r"""#94 迁完的那三把"先剥散文再判串"的尺子：正面写明各自认得什么，认不到的逐条列成清单。

分工（一把尺子只管一种文本，别混用）：

- `workflow_run_bodies`（母本在 test_ci_history_depth.py）——判 `.github/workflows/*.yml`：
  只认 `safe_load` 解析出来的结构，YAML 注释在这一层天然不存在；`run:` 里再剥**行首**（含缩进）
  是 `#` 的那一行。可选把 `with:` 的字面值一起收进面。
- `strip_py_prose`（母本在 test_machine_read_pointers.py）——判 `*.py`：tokenize 报出的 COMMENT
  token（整行与行尾都算）+ AST 认出的「模块/类/函数体第一个字符串表达式」（docstring），两者都
  **原位刷成空格**，所以行号与字符偏移都与原文一致，`src.index(a) < src.index(b)` 这类判序照用。
- `_line_comment_free` + `_line_of`（母本在 test_swap_package_contract_v0291.py）——判 `*.ps1`
  的行号：只剥 `(?m)^[ \t]*#.*$` 那一层，**行数一一对应**；块注释绝不碰（一整段删掉会把换行也
  删了、行号全盘漂，disable_verify 那条 `len` 自检当场崩）。

三把尺子共同认得的一种正面形状：**整行（可带行首缩进）以 `#` 开头**的那一行散文。下面那份清单
是复验过的盲区，每条配一枚「今天仍然剥不掉/看不见」的钉子
（`test_the_declared_blind_spots_are_actually_blind`）与一组「尺子对认得的形状是真认得」的对照。
清单不是「已穷尽」，它只覆盖这里复验过的形状；谁以后把某种形状收进射程而没回来删掉对应那条，
钉子就红。
"""
import re
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent))

from tests.test_ci_history_depth import WORKFLOWS_DIR, workflow_run_bodies  # noqa: E402
from tests.test_machine_read_pointers import strip_py_prose  # noqa: E402
from tests.test_swap_package_contract_v0291 import (  # noqa: E402
    PS1, _line_comment_free, _line_of)
from tests.test_v0331_pre_tag_gate_contract import _gate_token_hits, _synth_workflows  # noqa: E402

WF_TOKEN = "tools/pre_tag_gate.py"
# 每条样本都埋这一串；钉子问的是「剥完之后它还在不在」。
PROBE = "PRETAG verdict=PASS"

# 每条：(编号, 尺子, 输入, 今天为什么算漏)
_STILL_MISSED_SHAPES = (
    ("ps-trailing-comment", _line_comment_free,
     "        Write-Output 'PRECHECK ok'  # 这一行为什么必须在预检之后，%s\n" % PROBE,
     "只认**整行**以 # 开头；行尾挂着的那段说明留在判据面里。收进射程得配一把按 PowerShell "
     "字符串与 here-string 边界扫描的读法，本单没做——做浅了会顺手吃掉单引号里的 #"),
    ("ps-block-comment-interior", _line_comment_free,
     "<# 帮助块\n  %s 这一行不是行首注释\n#>\n" % PROBE,
     "`<# ... #>` 整段不剥：剥了会连换行一起没了、行号漂，而这一族判据判的就是行号。块注释里"
     "写了步骤名，行号尺照样指到它。判「绝对不许出现某写法」该改用 CODE（`_strip_comments`），"
     "那是另一把尺、另一格射程"),
    ("ps-hash-inside-string", _line_comment_free,
     "        $note = '先看 # 号后面的说明 %s'\n" % PROBE,
     "**刻意不剥**（字符串里的 # 不是注释）。它进清单是因为反向的病同样会死人：放宽成「行里含 # "
     "就整行剥」，这一行会被抹掉，真步骤就再也数不到（#103 那条「多剥」的教训）"),
    ("py-string-that-is-not-a-docstring", strip_py_prose,
     'import os\nos.getcwd()\n"裸字符串常量，写着 %s"\n' % PROBE,
     "docstring 只认模块/类/函数体**第一个**表达式；位置在后的裸字符串常量按代码算——报错文案里"
     "的路径正是机器或人真要读的位置，所以不能剥。代价：往这个位置抄一句就能替真代码答到"),
    ("py-hash-inside-string", strip_py_prose,
     'MARK = "# %s"\n' % PROBE,
     "字符串字面量里的 # 不是注释：tokenize 不报 COMMENT，`ast` 也不当它是 docstring ⇒ 留在面上。"
     "同样是刻意的，但它就是「把写法藏进一个字符串常量」那条路"),
    ("workflow-trailing-shell-comment", None,
     "# 本机还要跑 %s" % PROBE,
     "`run:` 正文里**行尾**挂的 shell 注释留在面上（与 ps-trailing-comment 同一条病，换了文本"
     "种类）。YAML 层的注释不在这一格——那一层由 `safe_load` 直接吃掉"),
    ("rough-triple-quote-slice", None,
     "test_v0350_pre_tag_gate_scope_waiver.py",
     "还有一把锁**没迁**：它按三条引号 split 取「模块 docstring 之后的一小截」判禁字，token 落在"
     "那一小截之后就看不见。#74 没落地（那两条锁自己站不住），本单不动它 ⇒ 这条是**有活例**的盲区"),
)

# 对照组：尺子对它说「我认得」的那种形状是真认得——没有这一组，上面那一串「剥不掉」可能只是
# 尺子在空转。
_CONTROLS = (
    (_line_comment_free, "        # 整行注释里写着 %s\n" % PROBE),
    (strip_py_prose, '"""docstring 里写着 %s"""\nimport os\n' % PROBE),
    (strip_py_prose, "import os  # 行尾注释里写着 %s\n" % PROBE),
)


def test_the_declared_blind_spots_are_actually_blind():
    """清单里每一条今天都得仍然剥不掉；对照组三种形状必须真被剥掉。"""
    for name, ruler, sample, _note in _STILL_MISSED_SHAPES:
        if ruler is None:
            continue
        assert PROBE in sample, "%s 的样本没埋探针（这一跑作废）" % name
        assert PROBE in ruler(sample), (
            "%s 这条已经剥得掉了：射程声明该改、清单里该删这一条" % name)
    for ruler, sample in _CONTROLS:
        assert PROBE in sample, "对照样本没带探针（这一跑作废）"
        assert PROBE not in ruler(sample), (
            "连对照组都剥不掉 ⇒ 尺子在空转，上面那一串「剥不掉」不算证据：" + ruler.__name__)


def test_the_workflow_ruler_leaves_trailing_shell_comments_in_the_surface(tmp_path):
    """`workflow-trailing-shell-comment` 那枚的专用钉子：整行注释剥得掉、行尾注释剥不掉。"""
    d = _synth_workflows(tmp_path / "whole-line", ["# 本机还要跑 " + WF_TOKEN])
    bodies = workflow_run_bodies(d / "tests.yml", where="合成 workflow（射程钉子）")
    assert WF_TOKEN not in bodies, (
        "整行注释那一格现在也被剥得掉了？不对——它本来就该被剥掉，"
        "这条红话留给「清单里该删掉 workflow 那一条」的那个改动")

    d2 = _synth_workflows(tmp_path / "trailing", ['echo x  # 顺带跑 ' + WF_TOKEN])
    bodies2 = workflow_run_bodies(d2 / "tests.yml", where="合成 workflow（射程钉子）")
    assert WF_TOKEN in bodies2, (
        "行尾注释也被剥掉了：射程声明与清单都要跟着改（同时得回头查 ps 那一族是不是多剥了）")


def test_the_line_ruler_keeps_line_count_and_the_real_step_line():
    """行号尺的两枚格式钉子：剥完之后行数不变，且真步骤仍落在原文那一行。"""
    assert len(_line_comment_free(PS1).splitlines()) == len(PS1.splitlines()), (
        "剥注释改动了行数：所有行号判据从此与原文对不上")
    at = _line_of(r"^Write-Output 'SWAPPED'$", PS1)
    assert PS1.splitlines()[at].strip() == "Write-Output 'SWAPPED'", (
        "行号尺指的不是那一行真代码：要么剥多了，要么锚改了")


def test_every_workflow_file_is_inside_the_token_ban_surface():
    """`GATE_TOKEN` 那把禁的面 = 目录里**每一份** workflow 的 run 正文与 with: 值。"""
    files = sorted(WORKFLOWS_DIR.glob("*.yml"))
    assert files, "%s 里没有 workflow：这格面判据在空转" % WORKFLOWS_DIR
    per_file = [f.name for f in files if WF_TOKEN in workflow_run_bodies(f, where=str(f), withs=True)]
    assert per_file == _gate_token_hits(), (
        "同一批文件两次算出不同的命中清单：这把禁读的不是全部 run 正文")
    raw = (WORKFLOWS_DIR / "release-apk.yml").read_text(encoding="utf-8")
    assert WF_TOKEN in raw and not per_file, (
        "注释里那句教学命令被当成 CI 里的真命令了（要么多剥了，要么那句注释已经不在了）")


def test_the_rough_slice_blind_spot_still_has_a_live_example():
    """`rough-triple-quote-slice` 声明的是「还有活例」，这一枚钉的就是那个活例还在。

    #74 落地（那两处禁字改吃 `strip_py_prose`）之后这里会红：红了就把清单里那条删掉，
    而不是把钉子改成随便怎么都绿。
    """
    hit = [p.name for p in TESTS_DIR.glob("test_*.py")
           if re.search(r"split\('\"\"\"'\)", p.read_text(encoding="utf-8"))]
    assert hit == ["test_v0350_pre_tag_gate_scope_waiver.py"], (
        "糙切片的活例清单变了（现在是 %s）：射程声明与清单都要跟着改" % hit)
