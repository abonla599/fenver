r"""#94 迁完的那三把"先剥散文再判串"的尺子：正面写明各自认得什么，认不到的逐条列成清单。

分工（一把尺子只管一种文本，别混用）：

- `workflow_run_bodies`（母本在 test_ci_history_depth.py）——判 `.github/workflows/*.yml`：
  只认 `safe_load` 解析出来的结构，YAML 注释在这一层天然不存在；`run:` 里再剥**行首**（含缩进）
  是 `#` 的那一行。`withs=True` 那一格只收**真会被执行**的 `with:` 值（键名在可执行名单里才算，
  名单与三层判据都在 `_with_surface`），没登记的键名当场红而不是放过。
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

清单里的编号（账与判据同源：每条编号都必须出现在本 docstring 里，条数与唯一性由
`test_the_blind_spot_list_is_counted_named_and_ledged_against_its_own_docstring` 钉，本段不写条数）：
`ps-trailing-comment`、`ps-block-comment-interior`、`ps-hash-inside-string`、
`py-string-that-is-not-a-docstring`、`py-hash-inside-string`、
`workflow-trailing-shell-comment`。
"""
import inspect
import re
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent))

from tests.test_ci_history_depth import WORKFLOWS_DIR, workflow_run_bodies  # noqa: E402
from tests.test_machine_read_pointers import strip_py_prose  # noqa: E402
from tests.test_swap_package_contract_v0291 import (  # noqa: E402
    PS1, _line_comment_free, _line_of)
from tests.test_v0331_pre_tag_gate_contract import GATE_TOKEN, _synth_workflows  # noqa: E402

WF_TOKEN = "tools/pre_tag_gate.py"
# 每条样本都埋这一串；钉子问的是「剥完之后它还在不在」。
PROBE = "PRETAG verdict=PASS"

# 每条五格：(编号, 尺子, 输入, 专用钉的名字, 今天为什么算漏)。
# `尺子` 为 None 的那几条判的不是"剥完还在不在"这一层——它们由第四格那枚**专用钉**当场命中自己
# （`test_the_declared_blind_spots_are_actually_blind` 真去调用它），所以第三、四格不得同时为空；
# 有尺子的条目第四格写 None 是有意的：剥层判据本身就是那枚钉。
_STILL_MISSED_SHAPES = (
    ("ps-trailing-comment", _line_comment_free,
     "        Write-Output 'PRECHECK ok'  # 这一行为什么必须在预检之后，%s\n" % PROBE,
     None,
     "只认**整行**以 # 开头；行尾挂着的那段说明留在判据面里。收进射程得配一把按 PowerShell "
     "字符串与 here-string 边界扫描的读法，本单没做——做浅了会顺手吃掉单引号里的 #"),
    ("ps-block-comment-interior", _line_comment_free,
     "<# 帮助块\n  %s 这一行不是行首注释\n#>\n" % PROBE,
     None,
     "`<# ... #>` 整段不剥：剥了会连换行一起没了、行号漂，而这一族判据判的就是行号。块注释里"
     "写了步骤名，行号尺照样指到它。判「绝对不许出现某写法」该改用 CODE（`_strip_comments`），"
     "那是另一把尺、另一格射程"),
    ("ps-hash-inside-string", _line_comment_free,
     "        $note = '先看 # 号后面的说明 %s'\n" % PROBE,
     None,
     "**刻意不剥**（字符串里的 # 不是注释）。它进清单是因为反向的病同样会死人：放宽成「行里含 # "
     "就整行剥」，这一行会被抹掉，真步骤就再也数不到（#103 那条「多剥」的教训）"),
    ("py-string-that-is-not-a-docstring", strip_py_prose,
     'import os\nos.getcwd()\n"裸字符串常量，写着 %s"\n' % PROBE,
     None,
     "docstring 只认模块/类/函数体**第一个**表达式；位置在后的裸字符串常量按代码算——报错文案里"
     "的路径正是机器或人真要读的位置，所以不能剥。代价：往这个位置抄一句就能替真代码答到"),
    ("py-hash-inside-string", strip_py_prose,
     'MARK = "# %s"\n' % PROBE,
     None,
     "字符串字面量里的 # 不是注释：tokenize 不报 COMMENT，`ast` 也不当它是 docstring ⇒ 留在面上。"
     "同样是刻意的，但它就是「把写法藏进一个字符串常量」那条路"),
    ("workflow-trailing-shell-comment", None,
     "# 本机还要跑 %s" % PROBE,
     "test_the_workflow_ruler_leaves_trailing_shell_comments_in_the_surface",
     "`run:` 正文里**行尾**挂的 shell 注释留在面上（与 ps-trailing-comment 同一条病，换了文本"
     "种类）。YAML 层的注释不在这一格——那一层由 `safe_load` 直接吃掉"),
)

# 对照组：尺子对它说「我认得」的那种形状是真认得——没有这一组，上面那一串「剥不掉」可能只是
# 尺子在空转。
_CONTROLS = (
    (_line_comment_free, "        # 整行注释里写着 %s\n" % PROBE),
    (strip_py_prose, '"""docstring 里写着 %s"""\nimport os\n' % PROBE),
    (strip_py_prose, "import os  # 行尾注释里写着 %s\n" % PROBE),
)

# 手抄的两份账，与判据各算各的，才分得出"面变窄了"与"命中变了"两种病。
_WORKFLOWS_SCANNED_BY_THE_BAN = ("build-native-apk.yml", "gitleaks.yml",
                                 "release-apk.yml", "tests.yml")
# 应当命中 `WF_TOKEN` 的文件——今天为空表：那份教学命令只在 YAML 注释里，而注释在 `safe_load`
# 那一层就不存在。哪天它真被抄回 `run:` 或被抄进 `with:` 的可执行字段，这里必须跟着变，
# 而 v0331 那把禁当场红。
_TOKEN_HITS_EXPECTED = ()


def _run_blind_spot_pin(name: str, pin_name: str, tmp_path):
    """表驱动地调用「尺子为 None」那几条盲区的专用钉，并把红话归到条目上。

    钉不在 globals()、不是 callable、名字不以 `test_` 开头、或者它自己不再成立——四种都在这里红。
    这一格治的是"清单里指着一枚根本不存在的牙"：那种条目读起来像有判据，实际什么都没判。
    """
    pin = globals().get(pin_name)
    assert pin_name.startswith("test_") and pin is not None and callable(pin), (
        "%s 声明的专用钉 %r 不是本文件里一条 callable 的 test_ 用例（读到 %r）：清单指着一条"
        "不存在的牙，这一格等于没判" % (name, pin_name, pin))
    kwargs = {"tmp_path": tmp_path} if "tmp_path" in inspect.signature(pin).parameters else {}
    try:
        pin(**kwargs)
    except AssertionError as exc:
        raise AssertionError("%s 的专用钉 %s 不再成立：" % (name, pin_name) + str(exc)) from exc


def test_the_declared_blind_spots_are_actually_blind(tmp_path):
    """清单里每一条今天都得仍然剥不掉；对照组三种形状必须真被剥掉。

    有尺子的那几条走剥层判据；尺子为 None 的那几条走 `_run_blind_spot_pin`——两种都得给出证据，
    不许"没尺子"就当"这条自动通过"。
    """
    for name, ruler, sample, pin_name, _note in _STILL_MISSED_SHAPES:
        assert not (ruler is None and pin_name is None), (
            "%s 既没有尺子也没有专用钉：这一条盲区没有任何东西在判，清单在这里长成散文" % name)
        if ruler is None:
            _run_blind_spot_pin(name, pin_name, tmp_path)
            continue
        assert PROBE in sample, "%s 的样本没埋探针（这一跑作废）" % name
        assert PROBE in ruler(sample), (
            "%s 这条已经剥得掉了：射程声明该改、清单里该删这一条" % name)
    for ruler, sample in _CONTROLS:
        assert PROBE in sample, "对照样本没带探针（这一跑作废）"
        assert PROBE not in ruler(sample), (
            "连对照组都剥不掉 ⇒ 尺子在空转，上面那一串「剥不掉」不算证据：" + ruler.__name__)


def test_the_blind_spot_list_is_counted_named_and_ledged_against_its_own_docstring():
    """结构三枚：条数钉、编号与专用钉的唯一性、账与判据同源。

    那个数是**提交后态**，故意写成裸断言而不是散文——本文件模块 docstring 与 `_STILL_MISSED_SHAPES`
    的注释都只写"以那个元组为准"而**刻意不写条数**，这样"改了清单忘了改声明"就红在这里，而不是让
    声明长成第二份没人维护的真相（口径同 test_ci_history_depth.py 的条数钉）。
    """
    assert len(_STILL_MISSED_SHAPES) == 6, (
        "_STILL_MISSED_SHAPES 现在是 " + str(len(_STILL_MISSED_SHAPES)) + " 条，而本文件模块 "
        "docstring 与清单自己的注释都按这一枚条数钉写「以那个元组为准、声明里不写数」。改清单就"
        "同批核那两处散文与编号账；只改一边就是「声明比实测宽」再次复现。")
    names = [name for name, _r, _s, _p, _w in _STILL_MISSED_SHAPES]
    assert len(set(names)) == len(names), (
        "清单里有重复编号：编号是这一族账的主键，重复就有两条形状共用一枚钉、还有一条没人判")
    pins = [pin for _n, _r, _s, pin, _w in _STILL_MISSED_SHAPES if pin is not None]
    assert len(set(pins)) == len(pins), (
        "两条盲区指着同一枚专用钉：那枚钉只兜得住一条形状，另一条是空的")
    # 账与判据同源：每条编号都必须出现在模块 docstring 里（换行折叠后再比）。加一条盲区忘了写账、
    # 或改了账忘了改清单，都在这里红，而不必在声明里再抄一个会腐烂的条数。
    collapsed = re.sub(r"\s+", " ", __doc__ or "")
    unledged = [name for name in names if name not in collapsed]
    assert not unledged, (
        "清单与模块 docstring 的编号账分家了：这几条在 `_STILL_MISSED_SHAPES` 里、却没写进 "
        "docstring 那段清单 " + str(unledged) + "。射程只能有一处真相：改清单就把那段一起改。")
    for name, ruler, _s, pin_name, _w in _STILL_MISSED_SHAPES:
        if pin_name is None:
            continue
        assert ruler is None, (
            "%s 既有尺子又声明了专用钉：两条判据管同一格，将来其中一条被删掉没人知道哪条是真的"
            % name)


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
    """`GATE_TOKEN` 那把禁的面 = 目录里**每一份** workflow 的 run 正文 + 可执行字段那一格的 with。

    这一格从前写成 `per_file == _gate_token_hits()`，而两边是**同一个表达式、同一个串**
    （`WF_TOKEN` 与 v0331 的 `GATE_TOKEN` 同值，`_gate_token_hits` 也调同一个 `workflow_run_bodies`）
    ——那是一条恒绿、零牙的断言：判据怎么被改窄被改宽，它都照不到。现在改成与手抄的两份账比，
    两种病各回一句不同的红话：

    - **面变窄了/变了**：目录里读到的 workflow 文件名与本文件手抄的那份清单分家——glob 改了、
      文件被改名或挪走、目录被换（`WORKFLOWS_DIR_UNDER_TEST`）都走这一句。面少了一份，命中清单
      自然也可能跟着"变绿"，那种绿最容易被当成好消息。
    - **命中变了**：判据算出的命中文件与手抄的"应当命中清单"分家。今天空表，所以任何一次命中都
      必须有人先看一眼真物、再决定是改 workflow 还是改这份账。

    两处比的都是**这个文件自己**算出来的 `per_file`，不再拿 v0331 的返回值当预期——那才是同义反复
    的来源。两个名字（`WF_TOKEN` / `GATE_TOKEN`）必须仍是同一个串，这一条也钉住：串漂了而两处
    名单没跟着改，判据就各判各的。
    """
    assert WF_TOKEN == GATE_TOKEN, (
        "本文件的 WF_TOKEN 与 test_v0331_pre_tag_gate_contract.GATE_TOKEN 不再是同一个串（读到 "
        + repr(WF_TOKEN) + " / " + repr(GATE_TOKEN) + "）：那把禁与本文件的射程声明说的是两件事")
    files = sorted(WORKFLOWS_DIR.glob("*.yml"))
    assert files, "%s 里没有 workflow：这格面判据在空转" % WORKFLOWS_DIR
    assert tuple(f.name for f in files) == _WORKFLOWS_SCANNED_BY_THE_BAN, (
        "面变窄了/变了：目录里读到的 workflow 清单是 " + str([f.name for f in files])
        + "，与本文件手抄的 " + str(_WORKFLOWS_SCANNED_BY_THE_BAN) + " 分家了。少一份 = 那一份的 "
        "run 正文与 with 再也没人判（多出来的那一份同理，账得跟着真物走）；改这份账之前先确认真"
        "物为什么变了，别把 glob 改宽来凑。")
    per_file = [f.name for f in files
                if WF_TOKEN in workflow_run_bodies(f, where=str(f), withs=True)]
    assert tuple(per_file) == _TOKEN_HITS_EXPECTED, (
        "命中变了：判据在 " + str([f.name for f in files]) + " 里读到 token 的文件是 "
        + str(per_file) + "，本文件手抄的「应当命中清单」是 " + str(_TOKEN_HITS_EXPECTED)
        + "。今天应当是空表——出现命中说明那条闸门命令又被抄回真会被执行的那一格（run 正文或 "
        "with: 的可执行字段），先按 v0331 那句红话处理真物；只有确认这是合法的新形状时，才由人"
        "来改这份账并同时改射程声明。")
    raw = (WORKFLOWS_DIR / "release-apk.yml").read_text(encoding="utf-8")
    assert WF_TOKEN in raw and not per_file, (
        "注释里那句教学命令被当成 CI 里的真命令了（要么多剥了，要么那句注释已经不在了）")
