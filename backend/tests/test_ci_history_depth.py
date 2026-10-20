r"""#92a：把 #90a 那把浅克隆哨从"只盯 tests.yml"泛化成"run 正文命中 `HISTORY_READING_MARKERS`
的 job 必配 `fetch-depth: 0`"（#92a 修复轮把这半句措辞对齐实测：射程等于标记集合的射程，
不是"凡读历史的 job 都被判"——下面"射程"那一节的仍漏清单就是差额）。

## #92a 修复轮改了什么（独立审查判 BLOCK 的那一条）

审查员实测：在仓库外的 `git archive` 副本里给 `build-native-apk.yml` 注入
`run: git describe --tags`（或 `run: git show 262b1224:deploy/swap-package.ps1`）、不配
fetch-depth ⇒ 这把哨**静默判绿**。而三处措辞（`.github/workflows/tests.yml` 的注释、
`.github/workflows/gitleaks.yml` 的注释、本文件参数化锁的 docstring）写的是"**凡** run 正文
读仓库历史的 job 都必须 fetch-depth: 0"——声明比实测宽。本轮的四处动作：

1. 三处措辞收窄成"命中 `HISTORY_READING_MARKERS`（`_SYNTH_*` 那批钉子按条守着，条数以那个
   元组为准；写进注释的数字只是提交后态、会漂）里任一条标记"。两份 workflow 只动了 `#` 行。
2. 射程段补上**仍漏清单**：同样读历史、但在标记集合之外、实测仍绿的命令，逐条附本机
   `--depth 1` 副本与 workflow 副本的实跑结果。
3. 加三组钉子：**逐标记有牙**（每条标记自带一枚"注入它 ⇒ 必须红"的合成反例，元组的第三、
   四格就是为此存在的）、**边界钉**（仍漏清单里每条一枚 expect_red=False 的钉：谁把它加进
   标记集合而没回来改清单，这枚钉就红）、**条数钉**
   （`test_the_marker_count_and_each_markers_own_counterexample_are_pinned`）。
4. 两条间接标记收紧成命令形状（`\bpytest\b` → `python -m pytest` 那一族、`\bgitleaks\b` →
   `gitleaks detect` 那一族），代价与复验都记在射程段。

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

1. 取该作业每个步骤的 `run:` **可执行正文**，并**剥掉"去掉缩进之后行首是 `#`"的行**。YAML 注释在
   `yaml.safe_load` 之后本来就不在结构里，shell 行注释还留在 `run` 字符串里但不执行；
   两处都不算数，才配得上"读的是可执行位置，不是散文"（口径同
   `test_machine_read_pointers.py::_from_workflows`）。**这一层剥离有牙，两个相反方向都钉住了**：
   `test_comment_only_history_prose_never_becomes_a_requirement` 按形状把同一条 `git log` 各喂一次
   判据——写成整行注释的（行首即 `#`，以及剥掉缩进才到行首的那种）必须判成散文、不得要求历史；
   同一批行去掉注释前缀、以及命令后面挂 `# 数历史` 的那一版必须判成命令、必须要求历史。于是下面
   这几种缴械都会红：把那行过滤整行摘掉（散文侧全红）；把条件写成只认**无缩进**行首的 `#`——少剥，
   块标量只按首行缩进 dedent，run 正文里天然存在带缩进的注释行，那句散文就开始当判据；放宽成
   "行里含 `#` 就整行不算"——多剥，真读历史的那行被吞成散文，哨反过来放过一个没配 fetch-depth 的
   作业。这几种形状之外的缴械写法没有逐条的账，连同"把钉子本身删掉"记在射程段末尾。不许只把
   "散文不算数"写在注释里。
2. 正文命中"读仓库历史"的标记集合（`HISTORY_READING_MARKERS`）⇒ 这个作业**要求历史**。
   反过来不成立：没命中标记 = 不要求，哪怕它其实读了历史。那一半差额是本锁的边界，下面
   "射程"段的仍漏清单是**一批抽样**（名单以 `_SYNTH_STILL_MISSED` 为准，每条一枚边界钉），
   不写成"已穷尽"。
3. 要求历史的作业，它的 `actions/checkout` 步必须**恰好一条**，且 `with.fetch-depth` 是
   **整数 0**（不是 bool、不是字符串 `"0"`、不是有限正整数）——形状要求整段照抄 #90a，没松。

没命中标记的作业**一律不要求** checkout 配 `fetch-depth`。今天 `build-native-apk.yml` 的
`testDebugUnitTest` / `assembleDebug` / `upload-artifact` 三步与 `release-apk.yml` 的每一步
一条标记都不命中（`android-native/**/*.gradle` 里 grep `git` 0 命中）。把它们判红就是假锁——
判据抓错了东西时该改判据，不该改 workflow。反过来"这两份绿"也不是没跑：同一份副本里给
`build-native-apk.yml` 注入 `run: git log --oneline -1`（不带 fetch-depth）会当场红，实测见
下面的反例段。

标记集合里除了 `git cat-file / rev-list / log / merge-base / blame` 之外还有两条，理由要写在
这里而不是留给下一个人猜："跑本仓 pytest 套件"（那两把钉历史 SHA 的锁就住在这个套件里，
`run: python -m pytest backend/tests/` 与 `run: git cat-file` 在这把哨眼里是同一件事）和
"gitleaks detect"（它扫的是全历史，浅克隆让它只看得到最后一屏——`.gitleaks.toml` 那条
`detect --source /repo` 今天就是靠 `fetch-depth: 0` 才成立的）。

**这两条在 #92a 修复轮收紧过**（交付项 ④）：`\bpytest\b` → 认 `python -m pytest` 那个命令形状
（`python`/`python3`/`python3.x` 都算），`\bgitleaks\b` → 认 `gitleaks detect` 的命令形状，含
`gitleaks.yml` 那一步 `- name: gitleaks detect` 里的续行写法：镜像钉成
`ghcr.io/gitleaks/gitleaks:v8.18.4` 后面接 `\`、换行才 `detect --source /repo`。收紧前
`run: touch "$RUNNER_TEMP/pytest.log"` 这种**文件名**也算命中，
那是拿散文当判据；复验（两份真物仍被认出）由
`test_the_workflow_sweep_actually_sees_the_history_readers` 守着，两面证据在
`test_tightened_indirect_markers_hit_commands_and_ignore_prose`。**代价**记在射程段：裸
`pytest -q` 从今天起不再命中。

## 两处假理由（#92a 交付项 ②）

`gitleaks.yml` 与 `tests.yml` 的注释从前都写着"快照仓只有一个提交"。本仓不是那种形状——
HEAD、main、develop 各自带着正常生长的历史，那句话是从"快照仓"那种导入形状抄来的散文。
**这里刻意不写提交数**：那个数每提一笔就漂一次，正是本仓 #81 那一族病（把可测事实抄进注释
当理由，注释就成了第二份没人维护的真相）。改后的两句只做两件事：说清"本仓不是只有一个提交
的快照仓，钉历史 SHA 的锁要靠全历史取锚点"，并把人指向**真正守着这个口径的哨**——本文件。

## 射程（写清楚，别让它长成第二份真相）

- 只读 `steps[].run`。作业级 `defaults:`、`jobs.<id>.uses:`（可复用 workflow 的调用点）不在
  射程内；今天目录里的每一份里都没有这种形状，而 `_jobs_of` 遇到**既没有 `steps` 也不是
  `uses:`** 的作业
  会判红而不是放过——认不出就说认不出，不假装"没有作业要判"。
- `workflow_run_bodies(..., withs=True)` 多出来的那一格**只收真会被执行的 `with:` 值**：键名
  落在 `_WITH_KEYS_EXECUTABLE`（Actions 会把这个输入交给那个 action 去执行的那几个名字）里才算
  面。其余键名先查 `_WITH_KEYS_SEEN_IN_TREE` 那份真树快照——快照里没有、值又不像命令的键名
  **判红而不是放过**（放过没见过的键名，等于给"下一个 action 恰好把某个输入当命令执行"留一条
  没人看着的门）。快照由 `test_the_with_key_registry_is_a_snapshot_of_the_real_tree` 现场复量，
  真树今天的 `with:` 键名无一可执行 ⇒ 这一格在真树上命中 0；名单以那两个元组为准，注释不写条数。
  作业级 `with:`（可复用 workflow 的调用点）不在面里，由
  `test_the_surface_carries_no_job_level_with_entry_today` 钉着"今天必须看不见"。
- **判据认的是"命中标记"，不是"读没读历史"这件事本身**（#92a 修复轮把这句钉死：上一版在两份
  workflow 的注释和参数化锁的 docstring 里写的是"凡 run 正文读仓库历史的 job 都必须
  fetch-depth: 0"，比实测宽）。准确的说法只有一句：**run 正文命中
  `HISTORY_READING_MARKERS` 里任一条标记的 job，才必须 `fetch-depth: 0`**。条数钉见
  `test_the_marker_count_and_each_markers_own_counterexample_are_pinned`；注释里不许出现"任何
  git 命令"这种句子，也不许把清单写成"已穷尽"。
- **仍漏清单**：同样读仓库历史、但在标记集合之外、**实测仍绿**的形状。两组实测都出自本机实跑
  （不是推理）——① 仓库外 `git archive` 副本：把这条注入 `build-native-apk.yml` 的 `apk` 作业
  （它没有 fetch-depth）再跑本文件，**哨放它过**；同一份副本注入 `git log --oneline -1` 会当场
  红，所以那批绿是"看不见"而不是"没跑"。② 浅克隆副本（`git clone --depth 1 --no-local`，实测
  `rev-parse --is-shallow-repository` = true、`rev-list --count HEAD` = 1）里该命令的表现，
  证明它读的确实是历史：
  1. `git describe --tags --abbrev=0` —— ①仍绿；②rc=128 `fatal: No names found, cannot
     describe anything.`（同一条命令在全历史的生产树里实测 rc=0、答得出最近那个 tag——被切掉的
     正是 tag 指向的那段历史；这里刻意不写 tag 名，它每发一版就漂一次）
  2. `git show 262b1224:deploy/swap-package.ps1` —— ①仍绿；②rc=128 `fatal: invalid object
     name '262b1224'.`（正是那两把钉锚点的锁的取锚形状，审查员实测的那枚 BLOCK）
  3. `git rev-parse 262b1224^{commit}` —— ①仍绿；②rc=128 `fatal: ambiguous argument
     '262b1224^{commit}': unknown revision or path not in the working tree.`
  4. `git archive --format=tar 262b1224` —— ①仍绿；②rc=128 `fatal: not a valid object name:
     262b1224`。同一条写成 `git archive --format=tar HEAD` 在②实测 rc=0（它只要 HEAD 那一格），
     所以这条漏的是"取历史里某一格"那一半
  5. `git diff 262b1224 deadbeefdeadbeefdeadbeefdeadbeefdeadbeef -- deploy/swap-package.ps1`
     （第二枚是合成 SHA 形状 token，任何树上都解析不出来——不许把活的分支 tip 钉进 fixture）—— ①仍绿；②rc=128
     `fatal: bad revision '262b1224'`（第一枚先失败，换 token 不改实测）
  6. `git shortlog -sn HEAD` —— ①仍绿；②**rc=0 但少答**：只数到切片里那一笔（同副本
     `rev-list --count HEAD` 实测 = 1）。不报错、只少答，比 rc=128 更难发现
  7. `git bisect start 262b1224 HEAD` —— ①仍绿；②**rc=0 且不认锚点**：只回
     `status: waiting for both 'good' and 'bad' commits`，同副本里 `git bisect terms` 实测
     rc=1 `error: no terms defined`、`git rev-parse --verify 262b1224` 实测 rc=128
     `Needed a single revision`——它拿到一格不存在的历史却答"我在跑"
  8. `pytest -q backend/tests`（裸 pytest，不带 `python -m`）—— ①**改前**被 `\bpytest\b`
     抓住（实测 rc=1），④ 收紧之后①仍绿（实测 rc=0）。这是本轮主动换来的代价：不再拿文件名与
     文案当判据，代价就是裸命令这一格。写在这里而不是假装没有。
  清单里每一条都对应 `_SYNTH_STILL_MISSED` 里一枚 **expect_red=False** 的边界钉（形状照 #83
  的 `_SYNTH_TWO_HOP_FEED` / `_SYNTH_APPEND_FEED`；条数不写在这里，名单以那个元组为准）：今天必须绿，
  谁把某条加进 `HISTORY_READING_MARKERS` 而没回来改这段清单和两份 workflow 的注释，那枚钉就红。
  清单与本文件 docstring 的对应关系也被钉住
  （`test_the_marker_count_and_each_markers_own_counterexample_are_pinned` 要求每条边界钉的命令
  在这段清单里出现），所以"加一枚钉忘了写账"和"改账忘了改钉"都会红。
  **把这批命令加进标记集合是下一步的事**（#92b 或后续单），今天没做，就不许把声明写得像已经做了。
- **钉子自己的仍漏**（不是命令形状，因而没有对应的边界钉）：把
  `test_comment_only_history_prose_never_becomes_a_requirement` 这个函数整个删掉 ⇒ 其余用例照样
  全绿，**本判据今天抓不住"把钉子本身删掉"这件事**。不补一枚"钉住钉子"的元钉子是主动选择：给锁
  上锁没有尽头，还要往文件里引进一个会腐烂的函数名字符串；这一格归"本机复跑 + 独立审查"那一层。
  上面那组剥行配对夹具同理——它们钉住的是列出来的那几种形状，把剥行条件换成别的写法是否仍会红，
  逐条没有账。
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
必须真的存在"run 正文命中标记的作业"，且今天这两份（tests.yml / gitleaks.yml）必须还在里面——否则
"glob 写错 / 目录被挪走 / 标记集合被削光"都会让上面那条参数化锁在空地上判绿。git 那侧：
`rc != 0` 红，并且**分开报** rc=1 / rc=128 / 其它（#74 那条"把 rc=128 当 rc=1"就是这个坑：
128 说的是"cwd 压根不是仓库"，1 说的是"仓库在但这个问题答不出来"，两种情况的下一步动作
完全不同，混成一个数字就没人去查对的那一处）；stdout 解析不出 `true`/`false` 也红，
不返回"未知即通过"。

## 反例（先见过红 = 交付的一部分）

`WORKFLOWS_DIR_UNDER_TEST`（#92a 起取代 #90a 的 `TESTS_YML_UNDER_TEST`：泛化后判的是**一个
目录**而不是一个文件，所以口子跟着换成目录级；沿用
`test_v0350_release_materials_gate.py` 的 `PRE_TAG_GATE_PATH` 惯例）让 (a) 与覆盖自检指向
仓库外的变异副本；**默认路径就是仓内真实 workflow 目录**，CI 上从不设这个变量 ⇒ 没有绕过
路径，生产代码里也没有读它的地方。(b) 跟着**它自己所在的那棵树**走（`REPO_ROOT` 由本文件
位置算出），所以把它拷进一份真浅克隆里就能就地变红。四枚反例（加一步 `git log` 不带
fetch-depth ⇒ 红；同一处只写成 YAML 注释 ⇒ 绿；删 tests.yml 的 `fetch-depth: 0` ⇒ 红；
删 gitleaks.yml 的 `fetch-depth: 0` ⇒ 红）的实测记录写在 #92a 的提交正文里。

#92a 修复轮在同一套口子上另跑了两批（结果写在**本轮**的提交正文里，这里只说形状与去向）：

- **仓库外副本注入 `build-native-apk.yml`**（`git archive HEAD` 导出、CRLF 归一、无 BOM，与工作
  树逐字节相同）：仍漏清单里那批命令逐条注入 ⇒ 哨放它过；阳性对照 `git log --oneline -1` /
  `git cat-file -t 262b1224` / `python -m pytest backend/tests/ -q` ⇒ 红在"checkout 没有 with:
  块"那一句；不加东西的原样副本 ⇒ 全绿。这批实跑是"仍漏清单"的出处，不是推理。
- **合成反例已经进代码**，不依赖 `WORKFLOWS_DIR_UNDER_TEST`：`_synthetic_steps` 把一条 run 正文
  拼成真 YAML，喂给 `_judge_job`（参数化锁的同一条具身）。三组钉子各管一件事——
  `test_each_history_marker_has_its_own_counterexample`（逐标记有牙，每条都必须红）、
  `test_the_still_missed_list_is_still_missed_today`（边界钉，今天必须绿）、
  `test_the_marker_count_and_each_markers_own_counterexample_are_pinned`（条数钉 + "每条标记
  自带反例"的结构钉）。红路子里没有 skip，也没有 `pytest.xfail`。
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

# "这个作业读仓库历史"的标记集合。**这是一份有限清单，不是"任何读历史的做法"**——判据认的
# 是"run 正文命中下面某一条标记"，射程段（模块 docstring）另有一份"同样读历史但在清单之外、
# 实测仍绿"的仍漏清单，两边都由本文件下面的钉子守着。
# 前五条是直接的 git 历史读取；后两条是**间接但等价**的：跑本仓测试套件（那两把钉历史 SHA 的
# 锁住在里面）与 gitleaks detect（判据就是全历史）。
# 每条四格：(红话里给看的理由, 编译好的正则, 合成反例的 id, 反例的 run 正文行)。第三四格是
# #92a 修复轮加的**结构性牙齿**：加一条标记就必须同时交一枚"注入它、不配 fetch-depth ⇒ 红"
# 的反例，不然 `test_the_marker_count_and_each_markers_own_counterexample_are_pinned` 当场红
# ——这一格治的正是"改了判据忘了改清单/忘了补牙"。
# 后两条的正则是 #92a 修复轮交付项 ④ 收紧过的：`\bpytest\b` → 认 `python -m pytest` 命令形状
# （旧串连 `$RUNNER_TEMP/pytest.log` 这种文件名都命中，属过度要求）；`\bgitleaks\b` → 认
# `gitleaks detect`（含把镜像钉在 `ghcr.io/gitleaks/gitleaks:v8.18.4 \` 后面换行接 detect 的
# 续行写法，那是 gitleaks.yml 今天真正的形状）。收紧的代价记在射程段的仍漏清单里。
HISTORY_READING_MARKERS = (
    ("git cat-file（按 SHA 取对象：两把钉历史锚点的锁用的就是它）",
     re.compile(r"\bgit\s+cat-file\b"),
     "cat-file",
     ("git cat-file -e 262b1224:deploy/swap-package.ps1",)),
    ("git rev-list（数历史）", re.compile(r"\bgit\s+rev-list\b"), "rev-list",
     ("git rev-list --count HEAD",)),
    ("git log（读提交流）", re.compile(r"\bgit\s+log\b"), "git-log",
     ("git log --oneline -1",)),
    ("git merge-base（找共同祖先）", re.compile(r"\bgit\s+merge-base\b"), "merge-base",
     ("git merge-base HEAD develop",)),
    ("git blame（追某一行的归属）", re.compile(r"\bgit\s+blame\b"), "blame",
     ("git blame -L 1,10 backend/tests/test_ci_history_depth.py",)),
    ("跑本仓 pytest 套件（认 python -m pytest 这个命令形状；test_swap_package_data_snapshot.py "
     "与 test_swap_package_post_acceptance_tokens.py 那两把钉历史 SHA 的锁就在这个套件里）",
     re.compile(r"\bpython(?:3(?:\.\d+)?)?\s+-m\s+pytest\b"), "python-m-pytest",
     ("python -m pytest backend/tests/ -q --tb=short 2>&1 | tee \"$RUNNER_TEMP/pytest.log\"",)),
    ("gitleaks detect（认 gitleaks detect 的命令形状，含钉镜像后换行接 detect 的续行写法；"
     "它扫的是全历史，浅克隆只看得到最后一屏）",
     re.compile(r"\bgitleaks[\w./:@+\-]*\s+(?:\\\s*\n\s*)?detect\b"), "gitleaks-detect",
     ("docker run --rm -v \"$PWD:/repo\" ghcr.io/gitleaks/gitleaks:v8.18.4 \\",
      "detect --source /repo -c /repo/.gitleaks.toml --redact --exit-code 1")),
)

# 标记的反向索引：红话与钉子按 id 认一条标记（id 短、写得进断言消息），判据按整条理由认。
# 本测试自己抄写的那份归属声明（tests.yml 靠 `python -m pytest` 那一行进名单、gitleaks.yml 靠
# `gitleaks detect` 那一行）就是靠这两张表落到判据上的（见
# `test_the_workflow_sweep_actually_sees_the_history_readers` 的按条归属断言）。两份 workflow 里的
# 同口径注释没有任何机器读取：`safe_load` 在解析阶段就把注释丢了，那条边界记在射程段第 4 点。
_TOOTH_ID_BY_LABEL = {label: tooth_id
                      for label, _pattern, tooth_id, _lines in HISTORY_READING_MARKERS}
_LABEL_BY_TOOTH_ID = {tooth_id: label
                      for label, _pattern, tooth_id, _lines in HISTORY_READING_MARKERS}

# 收集期就把目录摊开 ⇒ 一份 workflow 一个参数化 id（目录里有几份就几条）。目录为空时
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
    """把所有步骤 `run:` 的**可执行正文**拼成一串：去掉缩进后行首是 `#` 的行被剥掉，落在这一格
    里的散文就不算数。

    YAML 注释在这一层已经不存在了（`safe_load` 不返回注释），shell 行注释还留在 `run`
    字符串里，所以这里剥一次。不剥的话，"把 `git log` 只写进注释"也会让这条锁
    要求 fetch-depth——那是拿散文当判据，正是 #90b 换窄时要拆掉的那把尺子。

    下面这行过滤是本文件唯一一处剥行，也是上面那句"散文不算数"落地的地方；它的射程由
    `test_comment_only_history_prose_never_becomes_a_requirement` 那组配对夹具钉住：整行注释（行首
    有没有缩进都算）必须被剥掉，可执行命令哪怕后面挂着 `#` 也必须留下。摘掉这行过滤、把条件写成
    只认无缩进行首、放宽成行里含 `#` 就整行剥掉，三种都会让对应那枚夹具当场红；这几种形状之外
    不保证（见射程段末尾那条声明）。
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
    """返回命中的标记理由清单；空清单 = 这把哨认为这个作业不读仓库历史。

    空清单**不等于**"这个作业真的不读历史"——射程段的仍漏清单记的就是这个差别。"""
    return [label for label, pattern, _tooth_id, _tooth_lines in HISTORY_READING_MARKERS
            if pattern.search(bodies)]


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


def workflow_run_bodies(path, where: str = "", withs: bool = False) -> str:
    """一份 workflow 文件里**所有作业**的可执行正文（`safe_load` → `jobs` → `steps` → 剥行首注释）。

    这是 `_run_bodies` 那把母本的目录级出口，别的文件要判 workflow 正文就复用它，不要再各造
    一把 `read_text()` + `in`——原文里注释与命令长得一样，删真行留注释就判不出来。
    `uses:` 调用点没有正文可判，跳过；其余认不出的作业形状由 `_jobs_of` 判红。
    `withs=True` 把 `with:` 里**真会被执行**的那几格值也拼进面（三层判据见 `_with_surface`）：
    键名不在可执行名单里、又不像命令的字面值就不进面——Actions 读的是那个键的**语义**，不是
    "with 下任何一段字符串"。
    """
    path = Path(path)
    label = where or path.name
    doc = _load_doc(path, label)
    out = []
    for _job_id, steps in _jobs_of(doc, label):
        if steps is None:
            continue
        out.append(_run_bodies(steps))
        if withs:
            for step in steps:
                if isinstance(step, dict) and isinstance(step.get("with"), dict):
                    out.extend(_with_surface(step["with"], label))
    return "\n".join(out)


# `with:` 那一格的三层判据。被复用它的那把 GATE_TOKEN 禁面拿去判（母本在
# test_v0331_pre_tag_gate_contract.py::_gate_token_hits），所以名单与形状都住在这里。
# 真树这几份 workflow 出现过的全部 `with:` 键名都**不是**会被执行的字段，于是 A 那一层今天的
# 命中是 0——它防的是"下一个人把 `script:`/`args:` 写进 `with:`"，不是今天这几份。
# 两份名单比的都是**归一后的键名**（`_with_key_norm`：`-`→`_`、转小写），所以名单里的串一律写成
# 归一形态——`fetch_depth` 而不是 `fetch-depth`。这一条不是排版偏好：名单拿归一后的串去查，
# 登记时抄了 YAML 里带连字符的原名就会**登记失败**，真物反而被 C 层判红。
# 值那一侧不做归一：值就是会被原样交出去的那串字符。
_WITH_KEYS_EXECUTABLE = ("arg", "args", "command", "commands", "commandtorun",
                         "entrypoint", "inlinescript", "script", "scripts", "shell")
# 真树 `with:` 键名的快照（归一形态，由
# `test_the_with_key_registry_is_a_snapshot_of_the_real_tree` 现场复量）：登记它才允许出现在面
# 之外，没登记的键名一律红（C 层）。
_WITH_KEYS_SEEN_IN_TREE = ("distribution", "fetch_depth", "gradle_version",
                           "if_no_files_found", "java_version", "name", "path",
                           "python_version")
# B 层的值形状：既含被禁的闸门命令串、又像一条会被执行的命令。
_WITH_TOKEN_SHAPE = "tools/pre_tag_gate.py"
_WITH_COMMAND_SHAPE = re.compile(r"^\s*(?:python3?|bash|sh|pwsh)\b|--expect")


def _with_key_norm(key) -> str:
    return str(key).replace("-", "_").lower()


def _with_surface(step_with: dict, label: str) -> list:
    """一个步骤的 `with:` 映射里进入判据面的值，三层各回一句**不同的**红话。

    - **A 键名层**：键名在 `_WITH_KEYS_EXECUTABLE` 里 ⇒ 值原样进面。这一层不抛红，命中由调用方
      的 token 判据报（红话里的键名由 `workflow_with_face` 提供），因为"值被禁"是调用方的事。
      值**不剥注释**：`with:` 的值没有"行首 `#` 就不执行"这一层语义，把它剥掉就是替 Actions
      撒谎。
    - **B 值形状层**：两份名单都不认得的键名，值同时含闸门命令串**且**有命令形状 ⇒ 红。
    - **C 键名登记层**：两份名单都不认得的键名，值也不像命令 ⇒ 红。

    B 排在 C 前面，于是"键不认识"与"值像命令"是两句不同的话——排查的人一眼分得清是名单该登记
    一个新键名，还是真的有人把命令塞进了一个没人登记过的输入。
    """
    out = []
    for key in sorted(step_with, key=str):
        norm = _with_key_norm(key)
        value = step_with[key]
        if norm in _WITH_KEYS_EXECUTABLE:
            if isinstance(value, str):
                out.append(value)
            continue
        if norm in _WITH_KEYS_SEEN_IN_TREE:
            continue
        if (isinstance(value, str) and _WITH_TOKEN_SHAPE in value
                and _WITH_COMMAND_SHAPE.search(value)):
            raise AssertionError(label + " 的 `with:` 里出现没登记的键名 " + str(key) +
                                 "，而它的值既含闸门命令串 " + _WITH_TOKEN_SHAPE + " 又带着命令"
                                 "形状（值首行读到 " + repr(value.splitlines()[0]) +
                                 "）：B 层不放它过去。要么把 " + str(key) + " 登记进 "
                                 "_WITH_KEYS_EXECUTABLE（那等于承认 Actions 会执行它，面要跟着"
                                 "重新声明），要么把这条命令从 `with:` 里拿掉。")
        raise AssertionError(label + " 的 `with:` 里出现没登记的键名 " + str(key) +
                             "（读到值 " + repr(value) + "）：C 层认不出这个输入，不许把它静默"
                             "当成「不会被执行」的那一格——下一个 action 恰好把同名输入当命令跑时，"
                             "这里就是唯一的入口。登记进 `_WITH_KEYS_SEEN_IN_TREE`（值不进面）或 "
                             "`_WITH_KEYS_EXECUTABLE`（值进面）二选一，由人来看。")
    return out


def workflow_with_face(path, where: str = "") -> list:
    """这份 workflow 落在 A 那一层的 `with:` 键名，只读键名、不跑 B/C 所以不会抛。

    调用方用它把红话写实：命中到底是 `run:` 正文还是 `with:` 的哪个可执行字段，两种红的下一步
    动作不一样（前者是"CI 又去跑本机闸门"，后者是"某个 action 的输入里躺着那条命令"）。
    """
    path = Path(path)
    label = where or path.name
    doc = _load_doc(path, label)
    keys = []
    for _job_id, steps in _jobs_of(doc, label):
        if steps is None:
            continue
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("with"), dict):
                continue
            keys.extend(sorted(str(k) for k in step["with"]
                               if _with_key_norm(k) in _WITH_KEYS_EXECUTABLE))
    return keys


def _all_step_with_keys(path, where: str = "") -> set:
    """这份 workflow 里**步骤级** `with:` 出现过的全部键名（归一后），供快照复量用。

    刻意不复用 `workflow_run_bodies(withs=True)`：那份快照判的是"真树出现过哪些键名"，遇到新键名
    必须能报出"多出的是哪一个"，而不是先被 C 层的红打断。作业级 `with:` 不算在这一格里
    （它不在面里，见 `test_the_surface_carries_no_job_level_with_entry_today`）。
    """
    path = Path(path)
    label = where or path.name
    doc = _load_doc(path, label)
    seen = set()
    for _job_id, steps in _jobs_of(doc, label):
        if steps is None:
            continue
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("with"), dict):
                seen.update(_with_key_norm(k) for k in step["with"])
    return seen


def _assert_checkout_fetches_full_history(steps, where: str, markers: list) -> None:
    """形状判据（#90a 整段保留）：恰好一条 checkout，`with.fetch-depth` 必须是整数 0。

    #92a 修复轮四处红话各带上 `why`（这一格是被哪条标记要求历史的）。#90a 只在"checkout 步
    不恰好一条"那一句里写过标记，剩下三句只说"缺 fetch-depth"——于是红话讲不出"凭什么是这一格
    要历史"，下一个人只能自己回去 grep 标记集合。判据一个字没改，改的是红话能不能指对人。
    """
    why = "（run 正文命中：" + "；".join(markers) + "）"
    hits = [(i, s) for i, s in enumerate(steps)
            if isinstance(s, dict) and _action_name(s) == "actions/checkout"]
    assert len(hits) == 1, (
        where + " 要求全历史" + why + "，但它的 checkout 步不"
        "恰好一条（读到 " + str(len(hits)) + " 条，位置 " + str([i for i, _ in hits])
        + " / 全部 uses "
        + str([s.get("uses") for s in steps if isinstance(s, dict) and s.get("uses")])
        + "）：一条都没有 ⇒ 没有任何东西负责把历史取进 runner；多条 ⇒ 后一条的浅默认会"
          "覆盖前一条的 fetch-depth: 0。两种都不是'配置没问题'。")
    idx, checkout = hits[0]

    with_ = checkout.get("with")
    assert isinstance(with_, dict), (
        where + " 第 " + str(idx) + " 步（" + str(checkout.get("uses")) + "）**with: 块缺失或"
        "读不出映射**（读到 " + repr(with_) + "）" + why + "："
        "fetch-depth 就用的是 checkout 的默认浅克隆，那两把钉历史 SHA 的锁（锚点 "
        + " / ".join(PINNED_HISTORY_ANCHORS) + "）会在 checkout 之后红成\"锚点解析不出来\"，"
        "看着像 deploy/swap-package.ps1 坏了。这是本锁要防的那一格，别把它改成注释。")
    assert "fetch-depth" in with_, (
        where + " 的 checkout 步有 with: 块，但里面没有 fetch-depth（读到键 "
        + str(sorted(with_)) + "）" + why + "：缺这一条 = 用默认浅克隆，同上。判据认的是这个键的实际取值，"
          "不是文件里有没有出现过 fetch-depth 这个词。")

    depth = with_["fetch-depth"]
    # bool 先挡掉：YAML 里 `fetch-depth: no` 会解析成 False，而 False == 0 在 Python 里为真——
    # 那不是"全量"，那是把默认浅克隆放过的另一条路。
    assert isinstance(depth, int) and not isinstance(depth, bool), (
        "fetch-depth 的值不是整数（读到 " + repr(depth) + "，类型 " + type(depth).__name__
        + "）" + why + "：字符串 \"0\" 与 null 都不会让 checkout 取全历史，"
        "别让它们在 Python 的相等里蒙过去。")
    assert depth == 0, (
        "fetch-depth 读到的值是 " + str(depth) + "，判据要求恰好 0（全量历史）" + why + "。"
        "正整数只往回取那么多多代，锚点 " + " / ".join(PINNED_HISTORY_ANCHORS)
        + " 里最老的一枚在 " + str(depth) + " 代之外时，那两把锁照样红——浅克隆哨不放行有限深度。")


def _judge_job(steps, where: str) -> list:
    """对一个作业的 steps 走完整三步判据，返回命中的标记清单（空表 = 这把哨不要求它配）。

    参数化锁与下面所有合成反例走的都是这一个函数：反例必须喂给**判据本体**，不能另抄一份
    近似逻辑——否则"见过红"证明的是那份近似而不是这把锁（口径同 #83 的 `_read_point_shape_problems`）。
    """
    markers = _history_markers_in(_run_bodies(steps))
    if markers:
        _assert_checkout_fetches_full_history(steps, where, markers)
    return markers


# 合成反例的载体：把几条 shell 行拼成一份**真 YAML**，再按参数化锁同一条路解析出 steps。
# 不手搓 dict——手搓的 dict 会替 YAML 撒谎（块标量怎么剥缩进、`fetch-depth: no` 解析成什么，
# 都由 yaml 说了算）。checkout 那一步刻意不带 `with:`：反例要演的就是"命中了标记却没配
# fetch-depth"那一格，于是有牙的那批红在这里，边界钉那批正因为没命中标记而走到不了这里。
_SYNTH_WORKFLOW = """name: Synthetic
on: [push]
jobs:
  synth:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: synthetic step
        run: |
{body}
"""


def _synthetic_steps(run_lines) -> list:
    body = "\n".join("          " + ln for ln in run_lines)
    doc = yaml.safe_load(_SYNTH_WORKFLOW.replace("{body}", body))
    jobs = _jobs_of(doc, "合成 workflow（#92a 修复轮的反例）")
    assert [name for name, _steps in jobs] == ["synth"], (
        "合成模板解析不出 synth 这一个作业：反例喂不进判据本体，判红而不是让它假绿")
    steps = jobs[0][1]
    assert steps is not None, "合成作业被当成 uses: 调用点，反例没被注入到 run 正文"
    return steps


# **仍漏清单**（交付项 ② 的钉子）。每条 (id, 注入的 run 正文, 今天为什么算漏)。三条实测来源：
# ① 仓库外 `git archive` 副本里把这条注入 build-native-apk.yml 的 apk 作业（它没有 fetch-depth），
#    跑本文件 ⇒ rc=0、哨静默放行（不红也不 skip）；同一份副本注入 `git log --oneline -1` ⇒ rc=1 红，
#    所以那批绿是"看不见"而不是"没跑"。
# ② 同一批命令在本机 `--depth 1`（`git clone --depth 1 --no-local`，实测
#    `rev-parse --is-shallow-repository` = true、`rev-list --count HEAD` = 1）的副本里的表现，
#    证明它们确实读的是历史而不是一格工作树。逐条数字写在模块 docstring 的射程段。
# ③ 收紧间接标记（交付项 ④）换来的那一格代价（bare-pytest 那条）。
# 每一枚都是 expect_red=False 的边界钉：谁把这条形状加进 HISTORY_READING_MARKERS 而没回来改
# 清单，这枚钉立刻红——这就是"改了判据忘了改清单"的代价必须落在谁头上的答案。
_SYNTH_STILL_MISSED = (
    ("git-describe", "git describe --tags --abbrev=0",
     "浅克隆副本实测 rc=128：`fatal: No names found, cannot describe anything.`（同一条命令在"
     "全历史的生产树里实测 rc=0，答得出来——被切掉的正是 tag 指向的那段历史）"),
    ("git-show-sha", "git show 262b1224:deploy/swap-package.ps1",
     "浅克隆副本实测 rc=128：`fatal: invalid object name '262b1224'.`——正是那两把钉锚点的锁的取锚形状"),
    ("git-rev-parse-sha", "git rev-parse 262b1224^{commit}",
     "浅克隆副本实测 rc=128：`unknown revision or path not in the working tree`"),
    ("git-archive-old-sha", "git archive --format=tar 262b1224",
     "浅克隆副本实测 rc=128：`fatal: not a valid object name: 262b1224`；同一条命令写成 "
     "`git archive --format=tar HEAD` 实测 rc=0（它只要 HEAD 那一格），所以这条漏的是「取历史"
     "里某一格」那一半"),
    ("git-diff-two-shas", "git diff 262b1224 deadbeefdeadbeefdeadbeefdeadbeefdeadbeef -- deploy/swap-package.ps1",
     "浅克隆副本实测 rc=128：`fatal: bad revision '262b1224'`（第二枚 SHA 是合成 token，全树不可解析；"
     "不钉活的分支 tip——那个数每合一笔就漂）"),
    ("git-shortlog", "git shortlog -sn HEAD",
     "浅克隆副本实测 rc=0 但**少答**：只数到切片里那一笔（同副本 `git rev-list --count HEAD` "
     "实测 = 1）。不报错、只少答，比 rc=128 更难发现"),
    ("git-bisect", "git bisect start 262b1224 HEAD",
     "浅克隆副本实测 rc=0 且**不认锚点**：只回 `status: waiting for both 'good' and 'bad' "
     "commits`，同副本里 `git bisect terms` 实测 rc=1 `error: no terms defined`、"
     "`git rev-parse --verify 262b1224` 实测 rc=128 `Needed a single revision`——它拿到一格"
     "不存在的历史却答「我在跑」"),
    ("bare-pytest-command", "pytest -q backend/tests",
     "④ 收紧的代价：从前 `\\bpytest\\b` 命中它（副本实测改前 rc=1），改认 `python -m pytest` "
     "之后不再命中（实测改后 rc=0）"),
)

# 交付项 ④ 的两面：收紧之后**命令形状仍要命中**、**散文与文件名形状不再命中**。
# 命中的那几枚是"没把判据收没"的证据，不命中的那几枚是"不再拿散文当判据"的证据。
_SYNTH_MARKER_SHAPE_CASES = (
    ("py3-min-m-pytest", "python3 -m pytest -q", True,
     "python3 也要认：CI 与本机都可能出现这个写法"),
    ("py312-min-m-pytest", "python3.12 -m pytest backend/tests", True,
     "带小数点的解释器名同样要认"),
    ("gitleaks-direct-cli", "gitleaks detect --source . --redact", True,
     "直接 CLI 那一支"),
    ("gitleaks-image-same-line",
     "docker run --rm ghcr.io/gitleaks/gitleaks:v8.18.4 detect --source /repo", True,
     "把续行合成一行的写法也要认，否则收紧就成了新的漏"),
    ("pytest-log-filename", "touch \"$RUNNER_TEMP/pytest.log\"", False,
     "收紧前被 `\\bpytest\\b` 命中：日志文件名里有 pytest，不等于这一步读了仓库历史"),
    ("pytest-annotation-title",
     "echo \"::error title=pytest 红了一条::$line\"", False,
     "tests.yml 里那句 annotations 文案，从前也算命中——那是过度要求"),
    ("pip-install-pytest", "pip install pytest==8.3.3", False,
     "装 pytest 不跑它，不需要历史"),
    ("gitleaks-bare-word", "echo \"不用 gitleaks 官方 action\"", False,
     "收紧前被 `\\bgitleaks\\b` 命中的散文形状"),
    ("gitleaks-config-only", "ls -l .gitleaks.toml", False,
     "只看配置文件，不扫历史"),
    ("gitleaks-install-only", "apt-get install -y gitleaks", False,
     "装上 gitleaks 而没 detect，判据不该要求全历史"),
)

# 「剥掉行首（去缩进后）是 `#` 的行」这一层的**配对**夹具：少剥与多剥两个相反方向各有夹具，
# 单独任何一枚都留着一段缴械空间。四格：(参数化 id, 注入的 run 正文行,
# 这一批行应当被当成散文(True)还是可执行命令(False), 这一格存在的理由)。
# 注释那几枚刻意把无害命令放在**第一行**：YAML 块标量的缩进量由首行决定，注释行放前面会让整块
# 按更深的缩进 dedent 而直接解析失败——放在后面才落到「剥掉缩进才到行首」的真实形状上。
_SYNTH_PROSE_STRIP_CASES = (
    ("plain-comment-line", ("# git log --oneline -1", "echo ok"), True,
     "行首即 `#` 的整行注释：shell 根本不执行它，判据不得因此要求历史"),
    ("indented-comment-line", ("echo ok", "    # git log --oneline -1"), True,
     "去掉缩进之后才到行首的整行注释：`run: |` 的正文里天然存在这种行。把条件写成只认**无缩进**"
     "行首的 `#`（少剥），这句散文就开始当判据"),
    ("bare-history-command", ("git log --oneline -1", "echo ok"), False,
     "同一批行去掉注释前缀：这是对照——注释那几枚的空命中只能来自剥离，不是夹具本身看不见东西"),
    ("command-with-trailing-comment", ("git log --oneline -1  # 数历史", "echo ok"), False,
     "可执行命令挂尾随 `#` 注释：命令在前、注释在后，这一行是要跑的。放宽成「行里含 `#` 就整行"
     "不算」（多剥），真读历史的那格就被吞成散文，哨反过来放过一个没配 fetch-depth 的作业"),
)


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_workflow_that_reads_history_checks_out_the_whole_history(workflow_name):
    """(a) 配置判据（#92a 泛化版）：**逐份 workflow、逐作业**判
    "run 正文命中 `HISTORY_READING_MARKERS` ⇒ 必须 `fetch-depth: 0`"。

    这句话的射程就等于标记集合的射程，**不是**"凡读历史的 job 都被判"：一条命令只要不在那个
    元组里，注入它、不配 fetch-depth，今天照样静默过（副本实测见模块 docstring 的仍漏清单）。
    #92a 修复轮把这处措辞对齐实测，是审查员判 BLOCK 的那一条。

    为什么必须是 0 而不是"某个较大的数"：`fetch-depth` 取正整数时只往回取 N 代，
    而钉历史 SHA 的锁要的是**任意深度**的历史（锚点是几月前的提交，深度随历史增长而
    变，任何有限值都会在下一次 squash/rebase 之后重新不够）。0 = 全量。

    反过来的那一半同样重要：**没命中标记的作业不要求配 fetch-depth**。今天
    build-native-apk.yml 与 release-apk.yml 就是这样两份（它们的 run 正文一条标记都不命中），
    它们必须绿；无条件要求 checkout 配 fetch-depth 会得到一把假锁。
    """
    path = WORKFLOWS_DIR / workflow_name
    where = str(path)
    doc = _load_doc(path, where)

    for job_id, steps in _jobs_of(doc, where):
        if steps is None:
            continue
        _judge_job(steps, where + " 的作业 " + str(job_id))


def test_the_workflow_sweep_actually_sees_the_history_readers():
    """反空转 + 射程自检：这把泛化后的哨必须真的抓得到"在读历史的作业"。

    上面那条参数化锁只会说"该配的没配"，它说不出自己是不是在空地上跑：目录被挪走、glob
    写错、标记集合被削光、tests.yml 那份被改名——四种都让它一条用例都不生成或全部静默
    通过，而 CI 依旧绿。这一条就把"覆盖在场"本身钉住：

    1. 判据指向的目录存在、枚举非空（目录里至少有一份）；
    2. 枚举结果里**至少有一份**作业在 run 正文命中标记集合；
    3. 今天这两份必须在名单里：`tests.yml`（run 正文命中 `python -m pytest` 那条标记 ⇒ 间接
       读历史）与 `gitleaks.yml`（命中 `gitleaks detect` 那条 ⇒ 直接扫全历史）。#92a 修复轮
       ④ 把这两条间接标记收紧成命令形状之后，这一条就是"收紧没把判据收没"的复验点：它们哪天
       不再被认出来，要么是真不读历史了（那 fetch-depth 该跟着撤，由人来看），要么是标记集合
       瞎了——两种都必须红着让人来看，不许静默掉。
    4. 上面那句"靠哪一条标记进名单"跟着按条钉死：这两份各自命中的标记**恰好**是声明的那一条。
       放宽某条正则让真物多命中一条、或把两条标记的命令形状互换，都在这里红。**这枚钉兜住的是
       本函数里那张 `declared_reader_markers` 不与判据分家**，也就是测试自己的归属声明与实测命中
       各说各话时会红；`.github/workflows/tests.yml` 与 `gitleaks.yml` 里那两句同口径的注释
       **没有任何机器读取**（`yaml.safe_load` 在解析阶段就把注释丢了，本文件只读 `steps[].run`），
       注释写错、被删、与判据分家，今天都判不出红——那两处仍然只是给人看的，改判据时得人去核，
       不许把它说成"注释有判据兜着"。
    """
    assert WORKFLOWS_DIR.is_dir(), (
        "浅克隆哨（a）：workflow 目录 " + str(WORKFLOWS_DIR) + " 不存在——判据没有世界事实可读，"
        "上面那条参数化锁会退化成空跑。判红，不 skip。")
    assert WORKFLOW_NAMES, (
        "浅克隆哨（a）：" + str(WORKFLOWS_DIR) + " 里枚举不到任何 *.yml/*.yaml（读到 "
        + str(WORKFLOW_NAMES) + "）：目录空了或 glob 不认今天的文件名，那条参数化锁就一条用例"
        "都不生成——覆盖静默消失正是 #56/#63 那一族病。")

    readers = []
    hits_by_file = {}
    for name in WORKFLOW_NAMES:
        path = WORKFLOWS_DIR / name
        doc = _load_doc(path, str(path))
        for job_id, steps in _jobs_of(doc, str(path)):
            if steps is None:
                continue
            # 这里只判"认没认出来"，不判 fetch-depth：那条判据归参数化锁。两处各判各的，
            # 红话才说得出是哪一格坏了（这里红 = 覆盖不在场，那儿红 = 配置没配）。
            hits = _history_markers_in(_run_bodies(steps))
            if hits:
                hits_by_file.setdefault(name, []).extend(hits)
                readers.append(name + "::" + job_id + " 命中[" + "；".join(hits) + "]")

    assert readers, (
        "浅克隆哨（a）：扫了 " + str(len(WORKFLOW_NAMES)) + " 份 workflow（"
        + str(WORKFLOW_NAMES) + "），没有任何一份的 run 正文命中读历史的标记集合 "
        + str([label for label, _pattern, _tooth_id, _lines in HISTORY_READING_MARKERS])
        + "：这个判据已经空转。")
    missing = [name for name in ("tests.yml", "gitleaks.yml")
               if not any(r.startswith(name + "::") for r in readers)]
    assert not missing, (
        "浅克隆哨（a）：今天确实在读历史的这几份不再被认出来了 " + str(missing)
        + "（认出来的名单是 " + str(readers) + "）。要么标记集合瞎了（判据要修），要么它们"
        "真的不再读历史（那 fetch-depth: 0 该跟着撤，由人来看）——两种都必须红着人来判。")

    # 这里钉的是**本测试自己抄写的那份归属声明**（两份真物各自靠哪一条标记进名单）与判据的
    # 实际命中：命中的标记 id 必须**恰好**是声明的那一条。workflow 里的注释不在这枚钉的射程内。
    declared_reader_markers = {
        "tests.yml": ("python-m-pytest",),
        "gitleaks.yml": ("gitleaks-detect",),
    }
    misattributed = []
    for name, declared in declared_reader_markers.items():
        actual = tuple(sorted({_TOOTH_ID_BY_LABEL.get(label, label)
                               for label in hits_by_file.get(name, [])}))
        if actual != tuple(sorted(declared)):
            misattributed.append(name + "：本测试的归属声明把这一份记成靠 " + str(declared)
                                 + " 进名单，实测命中的是 " + str(actual))
    assert not misattributed, (
        "浅克隆哨（a）：这两份真物进读历史名单所靠的标记与本测试抄写的归属声明分家了 "
        + str(misattributed) + "。以真物为准：要么把 `HISTORY_READING_MARKERS` 改回只认声明的"
        "那条形状（放宽正则会让不读历史的作业也被要求 fetch-depth），要么改 .github/workflows/"
        "tests.yml 与 gitleaks.yml 的注释、以及本文件射程段与条数钉那段散文，让它们说清真物"
        "实际命中的是哪一条。")


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


@pytest.mark.parametrize("marker", HISTORY_READING_MARKERS,
                         ids=[m[2] for m in HISTORY_READING_MARKERS])
def test_each_history_marker_has_its_own_counterexample(marker):
    """逐标记有牙（#92a 修复轮交付项 ③ 第一组）：标记集合里**每一条**都得单独见过一次红。

    做法：把这条标记自带的 run 正文（元组第四格）注入一份合成 workflow，checkout 刻意不带
    `with:` ⇒ 判据必须红，而且红话里必须点名这一条标记。每条标记各自有牙：删掉任何一条的
    反例、或任一条反例不再命中自己的判据，参数化用例里对应那一条当场红——而不是全场只有
    `git log` 一枚有牙。

    顺带钉住两件事：这条反例**只**命中它自己那一条标记（命中两条以上 = 标记集合互相重叠，
    红话会指着错的那条让人查；零命中 = 反例已经跟判据分家），以及红的位置确实是
    「checkout 没有 with: 块」那一句而不是顺带撞到别的断言。
    """
    label, _pattern, tooth_id, tooth_lines = marker
    steps = _synthetic_steps(list(tooth_lines))
    hit = _history_markers_in(_run_bodies(steps))
    assert hit == [label], (
        "反例「" + str(tooth_id) + "」应当只命中它自己那条标记，实得 " + str(hit) + "：命中数不对"
        "说明标记集合互相重叠，或这条反例已经不再命中自己的判据——两种都会让红话指错地方。")
    with pytest.raises(AssertionError) as excinfo:
        _judge_job(steps, "合成 workflow 的作业 synth")
    message = str(excinfo.value)
    assert label in message, (
        "反例「" + str(tooth_id) + "」是红了，但红话里没点名这条标记（读到 " + message[:200] + "）")
    assert "with: 块缺失" in message, (
        "反例「" + str(tooth_id) + "」应当红在「第 0 步的 checkout **with: 块缺失**」那一句"
        "（合成 workflow 的 checkout 就是不带 with 写的），实得红话：" + message[:200])


def test_the_marker_count_and_each_markers_own_counterexample_are_pinned():
    """条数钉 + 结构钉（交付项 ③ 第三枚）。这里那个 7 是**提交后态**，故意写成裸断言而不是散文。

    `len(HISTORY_READING_MARKERS) == 7`：加一条或删一条都必须让这里红。三处射程声明
    （`.github/workflows/tests.yml` 与 `.github/workflows/gitleaks.yml` 的注释、本文件模块
    docstring 的射程段）都只写"以那个元组为准"而**刻意不写数**——数只活在本钉里，这样
    "改了判据忘了改清单"就红在这里，而不是让注释悄悄长成第二份没人维护的真相（同一句
    "声明比实测宽"的病本会话已经犯过：#83、#90b、#92a 各一笔，再犯就该由这枚钉在提交前
    拦下来，而不是等审查员再判一次 BLOCK）。

    结构钉管另一头：每条标记必须自带合成反例（第三、四格非空且能命中自己），id 与理由必须唯一，
    且 `_SYNTH_STILL_MISSED` 里每条边界钉的命令都必须出现在本文件 docstring 的仍漏清单里。
    加了标记忘了配牙、或加了钉忘了写账 → 这里红，而不是让上面那枚参数化反例静默少生成一条用例。
    """
    assert len(HISTORY_READING_MARKERS) == 7, (
        "HISTORY_READING_MARKERS 现在是 " + str(len(HISTORY_READING_MARKERS)) + " 条，而射程声明"
        "（.github/workflows/tests.yml 与 gitleaks.yml 的注释、本文件模块 docstring 的射程段）都按"
        "这一枚条数钉写「以那个元组为准、注释里不写数」。改判据就同批核那三处散文与本钉；只改一边"
        "就是「声明比实测宽」再次复现。")
    labels = [label for label, _p, _i, _l in HISTORY_READING_MARKERS]
    tooth_ids = [tid for _l, _p, tid, _x in HISTORY_READING_MARKERS]
    assert len(set(labels)) == len(labels), "标记理由必须互不相同：红话靠它定位命中的是哪一条"
    assert len(set(tooth_ids)) == len(tooth_ids), "合成反例的 id 必须互不相同：参数化 id 会撞车"
    for label, pattern, tooth_id, tooth_lines in HISTORY_READING_MARKERS:
        assert isinstance(pattern, re.Pattern), (
            "标记「" + str(tooth_id) + "」第二格不是编译好的正则：判据无从执行")
        assert tooth_lines and all(isinstance(ln, str) and ln.strip() for ln in tooth_lines), (
            "标记「" + str(tooth_id) + "」没带自己的合成反例正文：加一条标记就必须同时交一枚"
            "「注入它、不配 fetch-depth ⇒ 红」的反例（交付项 ③）。")
        assert label in _history_markers_in("\n".join(tooth_lines)), (
            "标记「" + str(tooth_id) + "」自带的反例命中不了自己：这条标记实际上没有牙。")
    # 仍漏清单与 docstring 是**同一本账**：每条边界钉的命令都必须出现在模块 docstring 的那段清单
    # 里（换行折叠后再比）。这样"加一枚钉忘了写账"和"改账忘了改钉"都会红，注释里就不必再写条数。
    collapsed = re.sub(r"\s+", " ", __doc__ or "")
    unlisted = [case_id for case_id, run_line, _why in _SYNTH_STILL_MISSED
                if run_line not in collapsed]
    assert not unlisted, (
        "边界钉与模块 docstring 的「仍漏清单」分家了：这几条在 `_SYNTH_STILL_MISSED` 里、却没写进 "
        "docstring 射程段的清单 " + str(unlisted) + "。射程只能有一处真相：改判据就把那段一起改。")


@pytest.mark.parametrize("case_id,run_line,why", _SYNTH_STILL_MISSED,
                         ids=[c[0] for c in _SYNTH_STILL_MISSED])
def test_the_still_missed_list_is_still_missed_today(case_id, run_line, why):
    """边界钉（交付项 ③ 第二组；形状照 #83 的 `_SYNTH_TWO_HOP_FEED` / `_SYNTH_APPEND_FEED`，
    expect_red=False）——这几枚**今天必须绿**。

    绿的是「判据抓不住它」这件事，不是「这条命令无害」：它读的确实是历史，本机 `--depth 1`
    副本里逐条的 rc 与首行报错写在 `_SYNTH_STILL_MISSED` 与模块 docstring 的仍漏清单里。

    为什么这枚钉是本单最有价值的一格：哪天有人把 `git describe` 之类加进
    HISTORY_READING_MARKERS（那是正确的方向），这一枚会红，红话写着「从 _SYNTH_STILL_MISSED
    与射程段的仍漏清单里删掉它，并同步两份 workflow 的注释与条数钉」——清单就不会和判据分家。
    """
    steps = _synthetic_steps([run_line])
    hit = _history_markers_in(_run_bodies(steps))
    assert hit == [], (
        "边界钉「" + case_id + "」红了：这条形状如今被标记集合抓住了（命中 " + str(hit) + "）。"
        "判据变强是好消息，但**账必须一起改**：从本文件的 `_SYNTH_STILL_MISSED` 与模块 docstring "
        "的仍漏清单里删掉它，并按需要同步 .github/workflows/tests.yml 与 gitleaks.yml 的注释和 "
        "test_the_marker_count_and_each_markers_own_counterexample_are_pinned 那个数。这条原本"
        "记的实测是：" + why)
    assert _judge_job(steps, "合成 workflow 的作业 synth") == [], (
        "边界钉「" + case_id + "」：标记没命中，判据却要求了 fetch-depth —— _judge_job 与 "
        "_history_markers_in 有一份在撒谎，红话不可采信。")


@pytest.mark.parametrize("case_id,run_line,expect_hit,why", _SYNTH_MARKER_SHAPE_CASES,
                         ids=[c[0] for c in _SYNTH_MARKER_SHAPE_CASES])
def test_tightened_indirect_markers_hit_commands_and_ignore_prose(case_id, run_line,
                                                                  expect_hit, why):
    """交付项 ④ 的两面证据：两条间接标记收紧成命令形状之后——

    命令形状（含 `gitleaks.yml` 今天真正写的那一版：钉死的镜像后面接 `\\` 换行再 `detect`）
    **必须仍命中**；散文与文件名形状（`$RUNNER_TEMP/pytest.log`、`title=pytest 红了一条`、
    裸 `gitleaks` 那个词）**必须不再命中**。

    这一枚只判标记本身（`_history_markers_in`），不判 fetch-depth：真物那两份还在不在名单里由
    `test_the_workflow_sweep_actually_sees_the_history_readers` 判（它要求 tests.yml 与
    gitleaks.yml 都在，并按条钉住各自靠哪一条标记进名单），逐条标记的红由
    `test_each_history_marker_has_its_own_counterexample` 判。
    入参是**裸 run 行**、不经 `_run_bodies`，所以"行首 `#` 要不要剥掉"这一格不归它判——
    那由 `test_comment_only_history_prose_never_becomes_a_requirement` 守着。
    """
    hit = _history_markers_in(run_line)
    if expect_hit:
        assert hit, (
            "形状「" + case_id + "」不再被认出来了：" + why + "。收紧判据不许把真读历史的那一格"
            "收没——认不出就停下重新挑标记串，而不是把这份 workflow 的 fetch-depth 撤掉。")
    else:
        assert not hit, (
            "形状「" + case_id + "」仍被命中（" + str(hit) + "）：" + why + "。这是过度要求："
            "拿散文当判据会让不读历史的 job 也被要求 fetch-depth。")


@pytest.mark.parametrize("case_id,run_lines,expect_prose,why", _SYNTH_PROSE_STRIP_CASES,
                         ids=[c[0] for c in _SYNTH_PROSE_STRIP_CASES])
def test_comment_only_history_prose_never_becomes_a_requirement(case_id, run_lines,
                                                                expect_prose, why):
    """「去掉缩进后行首是 `#` 的行不算数」这一层剥离是有牙的，两个相反方向各钉了一枚。

    夹具只差注释前缀与缩进，于是散文那几枚的"命中为空"只能来自 `_run_bodies` 那一次剥行，而不是
    夹具本身看不见东西——同一批行去掉注释前缀的对照必须命中 `git log`：

    - **散文侧（少剥的那个方向）**：行首即 `#`、以及剥掉缩进才到行首的整行注释，可执行正文里都不
      该出现它们，命中清单必须是空，作业级判定不得要求历史。把那一行过滤的条件写成只认**无缩进**
      行首的 `#` ⇒ 缩进那枚当场红（YAML 块标量只按首行缩进 dedent，带缩进的注释行是真物的形状）。
    - **命令侧（多剥的那个方向）**：去掉前缀的同一批行、以及 `git log --oneline -1` 后面挂
      `# 数历史` 的那一版，都必须只命中 `git log` 那条标记并红在「checkout 没有 with: 块」那一句。
      把条件放宽成「行里含 `#` 就整行不算」⇒ 尾随注释那枚当场红（哨会放过一个真读历史却没配
      fetch-depth 的作业）。

    摘掉那行过滤 ⇒ 散文侧全红。这几种形状之外的缴械写法（含"把本函数整个删掉"）没有逐条的账，
    见模块 docstring 射程段末尾那条声明。
    """
    steps = _synthetic_steps(list(run_lines))
    bodies = _run_bodies(steps)
    hit = _history_markers_in(bodies)

    if expect_prose:
        assert "git log" not in bodies, (
            "注释剥离失效（少剥的方向）：散文开始当判据——`_run_bodies` 把「" + case_id
            + "」这一行留在了可执行正文里（读到 " + repr(bodies) + "）。" + why
            + "。那一行过滤是本文件模块 docstring 与本函数 docstring 那句「散文不算数」的唯一"
              "执行处，它的射程由本组配对夹具钉住。")
        assert hit == [], (
            "注释剥离失效（少剥的方向）：只把历史命令写进注释的作业仍被认成读仓库历史，命中 "
            + str(hit) + "（可执行正文读到 " + repr(bodies) + "）。" + why)
        try:
            judged = _judge_job(steps, "合成 workflow 的作业 synth")
        except AssertionError as exc:
            pytest.fail("注释剥离失效（少剥的方向）：不读历史的合成作业被要求 fetch-depth: 0，"
                        "「" + case_id + "」红话首行：" + str(exc).strip().splitlines()[0])
        assert judged == [], (
            "注释剥离失效：标记清单说没命中，作业级判定却给出了要求 " + str(judged)
            + "：两份判据有一份在撒谎，红话不可采信。")
        return

    assert hit == [_LABEL_BY_TOOTH_ID["git-log"]], (
        "剥离过头（多剥的方向）：夹具「" + case_id + "」本该是可执行命令，实得命中 " + str(hit)
        + "（可执行正文读到 " + repr(bodies) + "）。" + why + "。把剥行条件从「去掉缩进后行首是 `#`」"
        "放宽成「行里含 `#` 就整行不算」时这一格空手：真读历史的那行被吞成散文，参数化锁对它判绿——"
        "哨就此静默放过一个读了历史却没配 fetch-depth 的作业。")
    with pytest.raises(AssertionError) as excinfo:
        _judge_job(steps, "合成 workflow 的作业 synth")
    assert "with: 块缺失" in str(excinfo.value), (
        "命令侧夹具「" + case_id + "」命中了标记，作业级判定却没红在「第 0 步的 checkout "
        "**with: 块缺失**」那一句，实得红话：" + str(excinfo.value)[:200])


def test_the_with_key_registry_is_a_snapshot_of_the_real_tree():
    """C 层那份快照必须与真树**逐键相等**，两个方向都判。

    - 真树出现了名单里没有的键名 ⇒ 红（那正是 C 层要拦的形状，只是这一枚给出的是"多出的是哪
      一个"，比 C 的红话更好定位）；
    - 名单里登记了真树已经没有的键名 ⇒ 也红。登记表自己也会腐烂：workflow 改了输入名而名单不动，
      下一个用同名的人就被"已经登记"骗过去。
    另外钉三件结构事实：两份名单必须各自互不相同、必须互不重叠（重叠时 A 抢在 C 前面，"这个键
    到底进不进面"取决于判序而不是名单），以及**真树今天的键名无一落在可执行名单里**——那句
    "面收窄到真会被执行的字段"在真树上的兑现就是这个空集，它哪天不空了必须红着让人重读面。
    """
    observed = set()
    for name in WORKFLOW_NAMES:
        observed |= _all_step_with_keys(WORKFLOWS_DIR / name, where=str(WORKFLOWS_DIR / name))
    registered = set(_WITH_KEYS_SEEN_IN_TREE) | set(_WITH_KEYS_EXECUTABLE)
    unregistered = sorted(observed - registered)
    stale = sorted(set(_WITH_KEYS_SEEN_IN_TREE) - observed)
    assert not unregistered and not stale, (
        "`with:` 键名快照与真树分家：真树里没登记的 " + str(unregistered) + "、名单里真树已经"
        "没有的 " + str(stale) + "（真树读到 " + str(sorted(observed)) + "）。C 层的牙建立在"
        "这份快照是真的上面：名单腐烂了，'新键名即红'就退化成'随机键名即红'。")
    assert len(set(_WITH_KEYS_SEEN_IN_TREE)) == len(_WITH_KEYS_SEEN_IN_TREE), (
        "`_WITH_KEYS_SEEN_IN_TREE` 里有重复键名：快照当成对，重复就是数错了一遍")
    assert len(set(_WITH_KEYS_EXECUTABLE)) == len(_WITH_KEYS_EXECUTABLE), (
        "`_WITH_KEYS_EXECUTABLE` 里有重复键名：A 层的名单当成对")
    assert not (set(_WITH_KEYS_SEEN_IN_TREE) & set(_WITH_KEYS_EXECUTABLE)), (
        "两份名单重叠了：" + str(sorted(set(_WITH_KEYS_SEEN_IN_TREE) & set(_WITH_KEYS_EXECUTABLE)))
        + "。同一个键名既「值不进面」又「值进面」，谁生效取决于判序——面就不再是读得出来的那一份。")
    executable_in_tree = sorted(observed & set(_WITH_KEYS_EXECUTABLE))
    assert not executable_in_tree, (
        "真树的 `with:` 里出现了可执行字段 " + str(executable_in_tree) + "：A 层那一格不再是空集，"
        "GATE_TOKEN 的禁面从此会在真树上命中，射程声明得跟着重写（先确认那个 action 真会把这个"
        "输入交给 shell，再把它留在名单里）。")


def test_the_surface_carries_no_job_level_with_entry_today():
    """作业级 `with:`（可复用 workflow 的调用点）今天必须**看不见**——面里没有它，是声明过的。

    `_jobs_of` 对 `uses:` 作业返回 `steps=None`，`workflow_run_bodies` 直接 `continue`，所以作业级
    `with:` 既不进 `run:` 面也不进 A 层。这一枚钉的是"现状与声明一致"：哪天仓库里出现一个带
    `with:` 的 workflow 调用点，这里红，由人来决定那一格算不算被执行的面——而不是让面在没人
    重读的情况下自己变宽。
    """
    found = []
    for name in WORKFLOW_NAMES:
        path = WORKFLOWS_DIR / name
        doc = _load_doc(path, str(path))
        jobs = doc.get("jobs")
        assert isinstance(jobs, dict) and jobs, str(path) + " 里没有 jobs: 映射：判据读不到世界事实"
        for job_id, job in sorted(jobs.items()):
            if isinstance(job, dict) and "with" in job:
                found.append(name + "::" + str(job_id) + " 键 " + str(sorted(map(str, job["with"]))))
    assert not found, (
        "真树出现了作业级 `with:`：" + str(found) + "。调用点传进去的参数会不会被那个 reusable "
        "workflow 执行，取决于它自己的正文，本锁读不到——要么把面明确扩到那一格并补判据，要么"
        "改这份 workflow；不许只把本函数改成恒真让声明继续假装成立。")
