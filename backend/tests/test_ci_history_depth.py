r"""#90a 浅克隆哨：CI 必须先真把仓库历史取到手，否则那两把钉历史 SHA 的锁红得没道理。

## 这条锁守的是哪一格

`.github/workflows/tests.yml` 的 `actions/checkout` 从前**没有** `with:`，也就吃的是
checkout 的默认浅克隆。而本仓有两把锁的判据是**仓库历史本身**，锚点钉成字面 SHA：

- `backend/tests/test_swap_package_data_snapshot.py` 的 `262b1224`（取锚 `_pin_blob` 用
  `git cat-file -e <sha>:deploy/swap-package.ps1`）；
- `backend/tests/test_swap_package_post_acceptance_tokens.py` 的 `b0cde6be` / `00f7fded`
  （取锚 `_head_blob`，同形状）。

浅克隆下这三枚对象根本不在本地（2026-10-09 在 `--depth 1` 副本里实测：
`git cat-file -e 262b1224:deploy/swap-package.ps1` 给 **rc=128 / invalid object name**，
`git rev-list --count HEAD` = 1）。那两把锁本身是**正面样本**——它们响亮失败、绝不 skip
（docstring 明写"指不到历史只能算失败，不许算跳过"）；缺的是它们的前置：CI 得真的把
历史给够。否则红出现在 checkout 之后，话术是"锚点在这份工作树里解析不出
deploy/swap-package.ps1"，下一个人先去查 `swap-package.ps1` 的代码，而错的是那行
`fetch-depth`。本单（工单 #55 的立项依据）第一次被实跑出来，不是推理出来的。

## 两条判据，各管一件事

(a) **配置判据**：按 YAML 结构定位 `tests.yml` 里的 checkout 步，断它 `with` 块的
    `fetch-depth` **值等于 0**。判的是那个文件此刻的实际内容（世界事实），不是它的
    自报，也不是"注释里提到了 fetch-depth"——注释只是给人看的来路说明，把它当判据
    等于把锁交给散文。
(b) **本机形状判据**：断 `git rev-parse --is-shallow-repository` 答 `false`。这条管的
    是另一头：万一将来有人在浅克隆的工作树上跑全量，它直接报"这台机器没有全历史，
    那两把钉 SHA 的锁的结论不可信"，而不是让那两把以难懂的方式红。

## YAML 依赖用的是哪一份，为什么不再手解

`PyYAML==6.0.3` 本来就在 `backend/requirements.txt`（第 81 行，chromadb 的传递依赖），
本仓"读 workflow 的测试"用的就是它：`test_machine_read_pointers.py::_from_workflows`、
`test_secret_scan.py::test_ci_runs_this_gate`、`test_apk_release_discipline.py` 都是
`yaml.safe_load` 之后按结构取步。沿用它 ⇒ 零新增依赖（改依赖表要走单独授权），而且
判据落在**结构**上：按 `uses` 的前半段认 checkout 步，不数行号、不按片段切文本，所以
往那一步里加/挪注释不会把这条锁惊动，反过来把 `fetch-depth` 挂到别的步上也瞒不过去。

## 缺东西一律判红，不 skip

本仓 #56/#63/#74 那一族病都是 skip 让覆盖静默消失。所以这里：文件读不到、YAML 解析
不出映射、取不到 steps、checkout 步不恰好一条、`with` 缺失、值不是整数 0 —— 全是
`assert` 红。git 那侧：`rc != 0` 红，并且**分开报** rc=1 / rc=128 / 其它（#74 那条
"把 rc=128 当 rc=1"就是这个坑：128 说的是"cwd 压根不是仓库"，1 说的是"仓库在但这个问题
答不出来"，两种情况的下一步动作完全不同，混成一个数字就没人去查对的那一处）；stdout
解析不出 `true`/`false` 也红，不返回"未知即通过"。

## 反例（先见过红 = 交付的一部分）

`TESTS_YML_UNDER_TEST` 让 (a) 能指向仓库外的变异副本；(b) 跟着**它自己所在的那棵树**
走（`REPO_ROOT` 由本文件位置算出），所以把它拷进一份真浅克隆里就能就地变红。两次实验
的实测记录写在提交正文里，生产代码里没有读这个环境变量的地方。
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# 默认判仓库里那份 tests.yml。TESTS_YML_UNDER_TEST 只服务于"先见过红"那次实验
# （把改过 fetch-depth 的副本放到仓库外来跑），生产代码里没有读这个变量的地方——
# 沿用 test_v0350_release_materials_gate.py:35 的 PRE_TAG_GATE_PATH 惯例。
TESTS_YML = Path(os.environ.get("TESTS_YML_UNDER_TEST")
                 or (REPO_ROOT / ".github" / "workflows" / "tests.yml"))

# 浅克隆下解析不出来的就是这三枚字面 SHA（两把既有锁的锚点）。这里只作为红话里的
# 线索写给人看，**不**参与判据——判据是 checkout 那一步的 fetch-depth 值。
PINNED_HISTORY_ANCHORS = ("262b1224", "b0cde6be", "00f7fded")

_PROBE_TIMEOUT_SECONDS = 60


def _action_name(step) -> str:
    """把 `uses: actions/checkout@v3` 折成 `actions/checkout`。

    去掉 `@ref` 再比是刻意的：v3→v4 之类升版不该让这条锁红（那是另一件事），而
    整串相等会把"升了版"误判成"checkout 步消失了"——红话会把人支到错的方向上。
    """
    uses = step.get("uses")
    if not isinstance(uses, str):
        return ""
    return uses.split("@", 1)[0].strip()


def _steps_of_tests_yml(doc) -> list:
    """从解析结果里取 jobs.test.steps；取不到就红，不返回空清单让下游断言空转。"""
    assert isinstance(doc, dict), (
        str(TESTS_YML) + " 解析不出 YAML 映射（读到的是 " + type(doc).__name__ + "）："
        "这条锁判的是那份 workflow 的实际内容，读不出结构就无从下判，判红不判跳过。")
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict) and jobs, (
        str(TESTS_YML) + " 里没有 jobs: 映射：结构变了，本锁按 jobs.test.steps 定位 checkout "
        "的那条路已经不存在，红话得由人来更新而不是假装通过。")
    test_job = jobs.get("test")
    assert isinstance(test_job, dict), (
        "jobs 里没有名为 test 的作业（读到 " + str(sorted(jobs)) + "）：tests.yml 的作业名改了，"
        "这条锁的定位方式跟着失效。")
    steps = test_job.get("steps")
    assert isinstance(steps, list) and steps, (
        "jobs.test.steps 不是非空清单（读到 " + repr(steps) + "）：没有步骤就没有 checkout，"
        "判红而不是判绿。")
    return steps


def test_tests_yml_checkout_fetches_the_whole_history():
    """(a) 配置判据：tests.yml 的 checkout 步必须显式 `fetch-depth: 0`。

    为什么必须是 0 而不是"某个较大的数"：`fetch-depth` 取正整数时只往回取 N 代，
    而钉历史 SHA 的锁要的是**任意深度**的历史（锚点是几月前的提交，深度随历史增长而
    变，任何有限值都会在下一次 squash/rebase 之后重新不够）。0 = 全量。
    """
    raw = TESTS_YML.read_bytes()
    assert raw, str(TESTS_YML) + " 是空文件：没有内容可判，判红。"
    steps = _steps_of_tests_yml(yaml.safe_load(raw.decode("utf-8")))

    hits = [(i, s) for i, s in enumerate(steps)
            if isinstance(s, dict) and _action_name(s) == "actions/checkout"]
    assert len(hits) == 1, (
        "checkout 步不恰好一条（读到 " + str(len(hits)) + " 条，位置 "
        + str([i for i, _ in hits]) + " / 全部 uses "
        + str([s.get("uses") for s in steps if isinstance(s, dict) and s.get("uses")])
        + "）：一条都没有 ⇒ 没有任何东西负责把历史取进 runner；多条 ⇒ 后一条的浅默认会"
          "覆盖前一条的 fetch-depth: 0。两种都不是'配置没问题'。")
    idx, checkout = hits[0]

    with_ = checkout.get("with")
    assert isinstance(with_, dict), (
        "tests.yml 第 " + str(idx) + " 步（" + str(checkout.get("uses")) + "）**没有 with: 块**："
        "fetch-depth 就用的是 checkout 的默认浅克隆，那两把钉历史 SHA 的锁（锚点 "
        + " / ".join(PINNED_HISTORY_ANCHORS) + "）会在 checkout 之后红成\"锚点解析不出来\"，"
        "看着像 deploy/swap-package.ps1 坏了。这是本锁要防的那一格，别把它改成注释。")
    assert "fetch-depth" in with_, (
        "checkout 步有 with: 块，但里面没有 fetch-depth（读到键 " + str(sorted(with_))
        + "）：缺这一条 = 用默认浅克隆，同上。判据认的是这个键的实际取值，"
          "不是文件里有没有出现过 fetch-depth 这个词。")

    depth = with_["fetch-depth"]
    # bool 先挡掉：YAML 里 `fetch-depth: no` 会解析成 False，而 False == 0 在 Python 里为真——
    # 那不是"全量"，那是把默认浅克隆放过的另一条路。
    assert isinstance(depth, int) and not isinstance(depth, bool), (
        "fetch-depth 的值不是整数（读到 " + repr(depth) + "，类型 " + type(depth).__name__
        + "）：字符串 \"0\" 与 null 都不会让 checkout 取全历史，别让它们在 Python 的相等里蒙过去。")
    assert depth == 0, (
        "fetch-depth 读到的值是 " + str(depth) + "，判据要求恰好 0（全量历史）。"
        "正整数只往回取那么多多代，锚点 " + " / ".join(PINNED_HISTORY_ANCHORS)
        + " 里最老的一枚在 " + str(depth) + " 代之外时，那两把锁照样红——浅克隆哨不放行有限深度。")


def test_this_checkout_is_not_a_shallow_clone():
    """(b) 本机形状判据：跑全量的这棵树必须有全历史，否则 (a) 之外的第二道防线塌了。

    CI 侧 (a) 管的是"workflow 有没有配 fetch-depth"，这条管的是"脚下这份工作树是不是
    真拿到了历史"。两者独立成立才闭环：配了但有人在浅克隆工作树上跑本机复跑，那两把
    钉 SHA 的锁给的是"锚点解析不出来"这种和代码无关的红，而报告里没人会想到去查克隆形状。
    """
    try:
        proc = subprocess.run(["git", "rev-parse", "--is-shallow-repository"],
                              cwd=str(REPO_ROOT), capture_output=True,
                              timeout=_PROBE_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        pytest.fail("浅克隆哨（b）：找不到 git 可执行文件（" + str(exc) + "）。这台机器答不出"
                    "自己是浅克隆还是全历史，那两把钉历史 SHA 的锁（锚点 "
                    + " / ".join(PINNED_HISTORY_ANCHORS) + "）的结论就不可信 —— 判红，不 skip："
                    "skip 会让这道防线在这台机器上静默消失，正是 #56/#63 那一族病。")
    except subprocess.TimeoutExpired as exc:
        pytest.fail("浅克隆哨（b）：git rev-parse 在 " + str(_PROBE_TIMEOUT_SECONDS)
                    + " 秒内没答话（" + str(exc) + "）。问不出克隆形状就只能算判据没成立，"
                    "判红不 skip。")

    rc = proc.returncode
    stdout = (proc.stdout or b"").decode("utf-8", "replace").strip()
    stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()

    # 认领 rc，并把 1 / 128 / 其它分开报（#74 的坑就是把 128 当 1 抄了一遍，红话
    # 指着一个不存在的问题让人查）。三种情况的下一步动作不同：
    #   128 ⇒ cwd 不是 git 仓库（或被 git 拒绝访问）——先问"我在哪棵树上跑的"；
    #     1 ⇒ 仓库在，但这一个问题答不出来（参数/用法被改坏）——先问"这条命令还写对吗"；
    #   其它 ⇒ 未知退出，一律按判据不成立处理，不猜它绿。
    if rc == 128:
        pytest.fail("浅克隆哨（b）：git 在 cwd=" + str(REPO_ROOT) + " 上 rc=128（"
                    + stderr + "）。128 说的是**这里不是 git 仓库**——那就没有任何历史可言，"
                    "钉历史 SHA 的两把锁在这里必然给假结论。判红；别改成 skip，那等于承认"
                    "这条锁在 CI 之外的树上是装饰。")
    if rc == 1:
        pytest.fail("浅克隆哨（b）：git rev-parse --is-shallow-repository 在这棵仓库里 rc=1（"
                    + stderr + "）。rc=1 是仓库在、但这道题没答上——命令写法被改坏或 git 不认"
                    "这个开关。判红不 skip：答不出克隆形状时，那两把钉历史 SHA 的锁的绿灯"
                    "不可采信。")
    assert rc == 0, (
        "浅克隆哨（b）：git 给了没认领过的退出码 rc=" + str(rc) + "（stderr=" + stderr
        + "，stdout=" + stdout + "）。不认领的退出码一律算判据不成立，判红；把它当成 0 或"
        "当成 skip 都是在给'未知的克隆形状'发通行证。")

    if stdout == "true":
        pytest.fail("浅克隆哨（b）：这份工作树是**浅克隆**（cwd=" + str(REPO_ROOT)
                    + "，git rev-list --count HEAD 只有一段历史）。这台机器没有全历史，"
                    "所以 test_swap_package_data_snapshot.py 的锚点 262b1224 与 "
                    "test_swap_package_post_acceptance_tokens.py 的 b0cde6be/00f7fded "
                    "在这里解析不出来（实测 git cat-file -e 给 rc=128 invalid object name）——"
                    "那两把锁在这里的红与代码无关，它们的绿灯同样不可采信。要么把 CI 的 "
                    "fetch-depth: 0 配齐，要么在有全历史的检出里跑，别把克隆形状当环境变量绕过去。")
    assert stdout == "false", (
        "浅克隆哨（b）：rc=0 但 stdout 解析不出 true/false（读到 " + repr(stdout)
        + "，stderr=" + stderr + "）。答非所问就当作判据没成立——判红，不返回'看着不像 true "
        "就当全历史'。git 对不认识的开关会把参数原样回显（实测 rc=0 + stdout 是那串参数），"
        "这一支就是接住那种形状的。")
