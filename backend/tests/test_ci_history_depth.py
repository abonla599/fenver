r"""#92a：把 #90a 那把浅克隆哨从"只盯 tests.yml"泛化成"任何读历史的 job 必配 `fetch-depth: 0`"。

## 这把锁守的是哪一格（#90a 的原始事故，一个字没改）

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

## #92a 泛化的那一步：尺子从"一个文件"挪到"整个 workflow 目录"

#90a 那版把判据钉死在 tests.yml 一个文件上，于是它天生管不到"下一个人把读历史的命令抄进
别的 workflow"——那正是 #90b 反向钉只覆盖一个文件留下的同一个洞。泛化后按
`.github/workflows/*.yml` 逐份、逐作业走三步：

1. 取该作业每个步骤的 `run:` **可执行正文**，并**剥掉行首是 `#` 的行**。YAML 注释在
   `yaml.safe_load` 之后本来就不在结构里，shell 行注释还留在 `run` 字符串里但不执行；
   两处都不算数，才配得上"读的是可执行位置，不是散文"（口径同
   `test_machine_read_pointers.py::_from_workflows`）。
2. 正文命中"读仓库历史"的标记集合（`HISTORY_READING_MARKERS`）⇒ 这个作业**要求历史**。
3. 要求历史的作业，它的 `actions/checkout` 步必须**恰好一条**，且 `with.fetch-depth` 是
   **整数 0**（不是 bool、不是字符串 `"0"`、不是有限正整数）——形状要求整段照抄 #90a，没松。

不读历史的作业**一律不要求** checkout 配 `fetch-depth`。今天 `build-native-apk.yml` 的
`testDebugUnitTest` / `assembleDebug` / `upload-artifact` 三步都不碰历史
（`android-native/**/*.gradle` 里 grep `git` 0 命中），`release-apk.yml` 的十三步也不碰。
把它们判红就是假锁——判据抓错了东西时该改判据，不该改 workflow。

标记集合里除了 `git cat-file / rev-list / log / merge-base / blame` 之外还有两条，理由要写在
这里而不是留给下一个人猜："跑本仓 pytest 套件"（那两把钉历史 SHA 的锁就住在这个套件里，
`run: python -m pytest backend/tests/` 与 `run: git cat-file` 在这把哨眼里是同一件事）和
"gitleaks detect"（它扫的是全历史，浅克隆让它只看得到最后一屏——`.gitleaks.toml` 那条
`detect --source /repo` 今天就是靠 `fetch-depth: 0` 才成立的）。

## 两处假理由（#92a 交付项 ②）

`gitleaks.yml` 与 `tests.yml` 的注释从前都写着"快照仓只有一个提交"。本仓不是那种形状——
HEAD、main、develop 各自带着正常生长的历史，那句话是从"快照仓"那种导入形状抄来的散文。
**这里刻意不写提交数**：那个数每提一笔就漂一次，正是本仓 #81 那一族病（把可测事实抄进注释
当理由，注释就成了第二份没人维护的真相）。改后的两句只做两件事：说清"本仓不是只有一个提交
的快照仓，钉历史 SHA 的锁要靠全历史取锚点"，并把人指向**真正守着这个口径的哨**——本文件。

## 射程（写清楚，别让它长成第二份真相）

- 只读 `steps[].run`。作业级 `defaults:`、`jobs.<id>.uses:`（可复用 workflow 的调用点）不在
  射程内；今天四份里没有这种形状，而 `_jobs_of` 遇到**既没有 `steps` 也不是 `uses:`** 的作业
  会判红而不是放过——认不出就说认不出，不假装"没有作业要判"。
- #90b 那条反向钉（`run` 正文不许再出现 `tools/pre_tag_gate.py`）目前**只覆盖
  release-apk.yml 一个文件**，抄进别的 workflow 今天不红；扩到全 `run:` 集合归 #92b。
  这句话同时写在
  `test_v0331_pre_tag_gate_contract.py::test_ci_step_is_narrowed_to_what_ci_can_actually_judge`
  的 docstring 里（#92a 交付项 ③，只写注释、没改判据）。

## 缺东西一律判红，不 skip

本仓 #56/#63/#74 那一族病都是 skip 让覆盖静默消失。所以这里：目录不存在、枚举为空、
文件读不到、YAML 解析不出映射、取不到 jobs / steps、作业形状认不出、checkout 步不恰好
一条、`with` 缺失、值不是整数 0 —— 全是 `assert` 红；另外
`test_the_workflow_sweep_actually_sees_the_history_readers` 是一条**反空转**判据：枚举结果里
必须真的存在"读历史的作业"，且今天这两份（tests.yml / gitleaks.yml）必须还在里面——否则
"glob 写错 / 目录被挪走 / 标记集合被削光"都会让上面那条参数化锁在空地上判绿。git 那侧：
`rc != 0` 红，并且**分开报** rc=1 / rc=128 / 其它（#74 那条"把 rc=128 当 rc=1"就是这个坑：
128 说的是"cwd 压根不是仓库"，1 说的是"仓库在但这个问题答不出来"，两种情况的下一步动作
完全不同，混成一个数字就没人去查对的那一处）；stdout 解析不出 `true`/`false` 也红，
不返回"未知即通过"。

## 反例（先见过红 = 交付的一部分）

`WORKFLOWS_DIR_UNDER_TEST`（#92a 起取代 #90a 的 `TESTS_YML_UNDER_TEST`：泛化后判的是**一个
目录**而不是一个文件，所以口子跟着换成目录级；沿用
`test_v0350_release_materials_gate.py:35` 的 `PRE_TAG_GATE_PATH` 惯例）让 (a) 与覆盖自检指向
仓库外的变异副本；**默认路径就是仓内真实 workflow 目录**，CI 上从不设这个变量 ⇒ 没有绕过
路径，生产代码里也没有读它的地方。(b) 跟着**它自己所在的那棵树**走（`REPO_ROOT` 由本文件
位置算出），所以把它拷进一份真浅克隆里就能就地变红。四枚反例（加一步 `git log` 不带
fetch-depth ⇒ 红；同一处只写成 YAML 注释 ⇒ 绿；删 tests.yml 的 `fetch-depth: 0` ⇒ 红；
删 gitleaks.yml 的 `fetch-depth: 0` ⇒ 红）的实测记录写在 #92a 的提交正文里。
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# 默认判仓内那一份真实的 workflow 目录。WORKFLOWS_DIR_UNDER_TEST 只服务于"先见过红"那几次
# 实验（把改过的 workflow 副本放到仓库外来跑），生产代码里没有读这个变量的地方，CI 上也
# 从不设它——泛化之后判的是一个目录，所以 #90a 那个单文件口子（TESTS_YML_UNDER_TEST）跟着
# 换成目录级；指向仓库外不影响"默认即仓内真物"这条硬要求。
WORKFLOWS_DIR = Path(os.environ.get("WORKFLOWS_DIR_UNDER_TEST")
                     or (REPO_ROOT / ".github" / "workflows"))

# 浅克隆下解析不出来的就是这三枚字面 SHA（两把既有锁的锚点）。这里只作为红话里的
# 线索写给人看，**不**参与判据——判据是 checkout 那一步的 fetch-depth 值。
PINNED_HISTORY_ANCHORS = ("262b1224", "b0cde6be", "00f7fded")

_PROBE_TIMEOUT_SECONDS = 60

# "这个作业读仓库历史"的标记集合。前五条是直接的 git 历史读取；后两条是**间接但等价**的：
# 跑本仓测试套件（那两把钉历史 SHA 的锁住在里面）与 gitleaks detect（判据就是全历史）。
# 每条带一句"为什么算"的理由，是为了红话说得出人话，不是为了好看。
HISTORY_READING_MARKERS = (
    ("git cat-file（按 SHA 取对象：两把钉历史锚点的锁用的就是它）",
     re.compile(r"\bgit\s+cat-file\b")),
    ("git rev-list（数历史）", re.compile(r"\bgit\s+rev-list\b")),
    ("git log（读提交流）", re.compile(r"\bgit\s+log\b")),
    ("git merge-base（找共同祖先）", re.compile(r"\bgit\s+merge-base\b")),
    ("git blame（追某一行的归属）", re.compile(r"\bgit\s+blame\b")),
    ("跑本仓 pytest 套件（test_swap_package_data_snapshot.py 与 "
     "test_swap_package_post_acceptance_tokens.py 那两把钉历史 SHA 的锁就在这个套件里）",
     re.compile(r"\bpytest\b")),
    ("gitleaks detect（扫的是全历史，浅克隆只看得到最后一屏）",
     re.compile(r"\bgitleaks\b")),
)

# 收集期就把目录摊开 ⇒ 一份 workflow 一个参数化 id（今天四份 ⇒ 四条）。目录为空时
# `WORKFLOW_NAMES` 为空，上面那条参数化锁不成立也别静默：**覆盖自检**那条会红（见它的 docstring）。
WORKFLOW_NAMES = sorted({p.name for p in WORKFLOWS_DIR.glob("*")
                         if p.is_file() and p.suffix in (".yml", ".yaml")})


def _action_name(step) -> str:
    """把 `uses: actions/checkout@v3` 折成 `actions/checkout`。

    去掉 `@ref` 再比是刻意的：v3→v4 之类升版不该让这条锁红（那是另一件事），而
    整串相等会把"升了版"误判成"checkout 步消失了"——红话会把人支到错的方向上。
    """
    uses = step.get("uses")
    if not isinstance(uses, str):
        return ""
    return uses.split("@", 1)[0].strip()


def _run_bodies(steps) -> str:
    """把所有步骤 `run:` 的**可执行正文**拼成一串；散文一概不算数。

    YAML 注释在这一层已经不存在了（`safe_load` 不返回注释），shell 行注释还留在 `run`
    字符串里，所以这里按行首 `#` 剥一次。不剥的话，"把 `git log` 只写进注释"也会让这条锁
    要求 fetch-depth——那是拿散文当判据，正是 #90b 换窄时要拆掉的那把尺子。
    """
    lines = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run")
        if isinstance(run, str):
            lines.extend(ln for ln in run.splitlines() if not ln.lstrip().startswith("#"))
    return "\n".join(lines)


def _history_markers_in(bodies: str) -> list:
    """返回命中的标记理由清单；空清单 = 这个作业不读仓库历史。"""
    return [label for label, pattern in HISTORY_READING_MARKERS if pattern.search(bodies)]


def _load_doc(path: Path, where: str):
    assert path.is_file(), (where + " 读不到：workflow 目录的内容就是这把哨的世界事实，"
                            "收集用例时它还在、判的时候没了（被删或被改名），判红而不是跳过。")
    raw = path.read_bytes()
    assert raw, where + " 是空文件：没有内容可判，判红。"
    doc = yaml.safe_load(raw.decode("utf-8"))
    assert isinstance(doc, dict), (where + " 解析不出 YAML 映射（读到的是 " + type(doc).__name__
                                   + "）：这条锁判的是那份 workflow 的实际内容，读不出结构就"
                                     "无从下判，判红不判跳过。")
    return doc


def _jobs_of(doc, where: str) -> list:
    """返回 [(作业名, steps)]；steps 为 None 表示"可复用 workflow 的调用点"，不在射程内。

    认不出的作业形状一律红：这里最坏的结局不是报错，是"以为没有作业要判"而安静放行。
    """
    jobs = doc.get("jobs")
    assert isinstance(jobs, dict) and jobs, (
        where + " 里没有 jobs: 映射：结构变了，本锁按 jobs.<id>.steps 定位 checkout 的那条路"
        "已经不存在，红话得由人来更新而不是假装通过。")
    out = []
    for job_id in sorted(jobs):
        job = jobs[job_id]
        assert isinstance(job, dict), (
            where + " 的作业 " + str(job_id) + " 不是映射（读到 " + repr(job) + "）：读不出步骤"
            "清单就读不出它读不读历史，判红而不是跳过。")
        steps = job.get("steps")
        if isinstance(steps, list) and steps:
            out.append((str(job_id), steps))
            continue
        if isinstance(job.get("uses"), str):
            out.append((str(job_id), None))
            continue
        pytest.fail(where + " 的作业 " + str(job_id) + " 既没有非空 steps:，也不是 uses: 调用"
                    "（读到键 " + str(sorted(job)) + "）：这是本锁没认领过的作业形状。判红而不是"
                    "当成\"这个作业不需要历史\"——放过认不出的形状，等于给'读历史却没配 "
                    "fetch-depth'开一条没人看着的门。")
    return out


def _assert_checkout_fetches_full_history(steps, where: str, markers: list) -> None:
    """形状判据（#90a 整段保留）：恰好一条 checkout，`with.fetch-depth` 必须是整数 0。"""
    hits = [(i, s) for i, s in enumerate(steps)
            if isinstance(s, dict) and _action_name(s) == "actions/checkout"]
    assert len(hits) == 1, (
        where + " 要求全历史（run 正文命中：" + "；".join(markers) + "），但它的 checkout 步不"
        "恰好一条（读到 " + str(len(hits)) + " 条，位置 " + str([i for i, _ in hits])
        + " / 全部 uses "
        + str([s.get("uses") for s in steps if isinstance(s, dict) and s.get("uses")])
        + "）：一条都没有 ⇒ 没有任何东西负责把历史取进 runner；多条 ⇒ 后一条的浅默认会"
          "覆盖前一条的 fetch-depth: 0。两种都不是'配置没问题'。")
    idx, checkout = hits[0]

    with_ = checkout.get("with")
    assert isinstance(with_, dict), (
        where + " 第 " + str(idx) + " 步（" + str(checkout.get("uses")) + "）**with: 块缺失或"
        "读不出映射**（读到 " + repr(with_) + "）："
        "fetch-depth 就用的是 checkout 的默认浅克隆，那两把钉历史 SHA 的锁（锚点 "
        + " / ".join(PINNED_HISTORY_ANCHORS) + "）会在 checkout 之后红成\"锚点解析不出来\"，"
        "看着像 deploy/swap-package.ps1 坏了。这是本锁要防的那一格，别把它改成注释。")
    assert "fetch-depth" in with_, (
        where + " 的 checkout 步有 with: 块，但里面没有 fetch-depth（读到键 "
        + str(sorted(with_)) + "）：缺这一条 = 用默认浅克隆，同上。判据认的是这个键的实际取值，"
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


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_workflow_that_reads_history_checks_out_the_whole_history(workflow_name):
    """(a) 配置判据（#92a 泛化版）：**逐份 workflow、逐作业**判"读历史 ⇒ 必须 fetch-depth: 0"。

    为什么必须是 0 而不是"某个较大的数"：`fetch-depth` 取正整数时只往回取 N 代，
    而钉历史 SHA 的锁要的是**任意深度**的历史（锚点是几月前的提交，深度随历史增长而
    变，任何有限值都会在下一次 squash/rebase 之后重新不够）。0 = 全量。

    反过来的那一半同样重要：**不读历史的作业不要求配 fetch-depth**。今天
    build-native-apk.yml 与 release-apk.yml 就是这样两份（它们的 run 正文一步历史都不碰），
    它们必须绿；无条件要求 checkout 配 fetch-depth 会得到一把假锁。
    """
    path = WORKFLOWS_DIR / workflow_name
    where = str(path)
    doc = _load_doc(path, where)

    for job_id, steps in _jobs_of(doc, where):
        if steps is None:
            continue
        markers = _history_markers_in(_run_bodies(steps))
        if not markers:
            continue
        _assert_checkout_fetches_full_history(steps, where + " 的作业 " + str(job_id), markers)


def test_the_workflow_sweep_actually_sees_the_history_readers():
    """反空转 + 射程自检：这把泛化后的哨必须真的抓得到"在读历史的作业"。

    上面那条参数化锁只会说"该配的没配"，它说不出自己是不是在空地上跑：目录被挪走、glob
    写错、标记集合被削光、tests.yml 那份被改名——四种都让它一条用例都不生成或全部静默
    通过，而 CI 依旧绿。这一条就把"覆盖在场"本身钉住：

    1. 判据指向的目录存在、枚举非空（今天四份）；
    2. 枚举结果里**至少有一份**作业在 run 正文读历史；
    3. 今天这两份必须在名单里：`tests.yml`（跑本仓套件 ⇒ 间接读历史）与 `gitleaks.yml`
       （直接扫全历史）。它们哪天不再被认出来，要么是真不读历史了（那 fetch-depth 该跟着
       撤，由人来看），要么是标记集合瞎了——两种都必须红着让人来看，不许静默掉。
    """
    assert WORKFLOWS_DIR.is_dir(), (
        "浅克隆哨（a）：workflow 目录 " + str(WORKFLOWS_DIR) + " 不存在——判据没有世界事实可读，"
        "上面那条参数化锁会退化成空跑。判红，不 skip。")
    assert WORKFLOW_NAMES, (
        "浅克隆哨（a）：" + str(WORKFLOWS_DIR) + " 里枚举不到任何 *.yml/*.yaml（读到 "
        + str(WORKFLOW_NAMES) + "）：目录空了或 glob 不认今天的文件名，那条参数化锁就一条用例"
        "都不生成——覆盖静默消失正是 #56/#63 那一族病。")

    readers = []
    for name in WORKFLOW_NAMES:
        path = WORKFLOWS_DIR / name
        doc = _load_doc(path, str(path))
        for job_id, steps in _jobs_of(doc, str(path)):
            if steps is None:
                continue
            if _history_markers_in(_run_bodies(steps)):
                readers.append(name + "::" + job_id)

    assert readers, (
        "浅克隆哨（a）：扫了 " + str(len(WORKFLOW_NAMES)) + " 份 workflow（"
        + str(WORKFLOW_NAMES) + "），没有任何一份的 run 正文命中读历史的标记集合 "
        + str([label for label, _ in HISTORY_READING_MARKERS]) + "：这个判据已经空转。")
    missing = [name for name in ("tests.yml", "gitleaks.yml")
               if not any(r.startswith(name + "::") for r in readers)]
    assert not missing, (
        "浅克隆哨（a）：今天确实在读历史的这几份不再被认出来了 " + str(missing)
        + "（认出来的名单是 " + str(readers) + "）。要么标记集合瞎了（判据要修），要么它们"
        "真的不再读历史（那 fetch-depth: 0 该跟着撤，由人来看）——两种都必须红着人来判。")


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
