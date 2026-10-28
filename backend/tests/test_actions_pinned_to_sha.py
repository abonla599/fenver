r"""GitHub Actions 的 `uses:` 一律固定到 40 位 commit SHA（审计 M-6 的静态收口）。

`@v4` / `@main` 这类引用是可移动的：上游随时能把 tag 或分支指针改到另一笔提交，
CI 下一次跑的就是另一份代码，而我们的 diff 里什么都不会出现。固定到 commit SHA
之后，换执行代码必然表现为一次可见的提交。

判据入口是纯函数 `_violations(text)`：逐行认 `uses:`，跳过整行注释与本地 action
（`./`），其余必须匹配

    uses:\s*[^@\s]+@[0-9a-f]{40}(?:\s*#.*)?$

`@vN`、`@main`、`@master`、没有 `@` 的形状全部算违规。行尾 `# vN` 注释只给人看，
不参与判据——Actions 读到的是 `#` 之前那一串。

射程与不做的事：

- 只判 `.github/workflows/` 下的 `*.yml` / `*.yaml`（两种扩展名在 Actions 里等价）；
  仓库里其它位置的 `uses:`（例如复合 action 的 `action.yml`）不在面里。
- 不判 SHA 指向哪一笔提交、是不是最新版本——那是升级动作，不是这条锁的事。
- `environment:` / 部署审批配置不在这里（需要仓库环境配置，另单处理）。

缺目录、缺文件、读不到、枚举为空一律判红，不 skip：判据面消失必须让人看见。
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# 审计覆盖的 workflow。写成代码而不是散文：删掉或改名其中任何一份，覆盖自检都会红，
# 而不是让判据面悄悄少一份。目录里多出来的 workflow 也会被自动扫到，不需要登记。
AUDITED_WORKFLOWS = (
    "tests.yml",
    "build-native-apk.yml",
    "release-apk.yml",
    "gitleaks.yml",
)

_USES_PINNED = re.compile(r"uses:\s*[^@\s]+@[0-9a-f]{40}(?:\s*#.*)?$")


def workflow_files(root=None) -> list:
    """`root` 里所有 workflow 文件：`.yml` 与 `.yaml` 都收。

    Actions 对两种扩展名等价；只 glob 一种的话，下一个人存成另一种就永久落在面外。
    """
    base = Path(root) if root else WORKFLOWS_DIR
    return sorted({p for p in base.glob("*.yml")} | {p for p in base.glob("*.yaml")})


def _uses_statements(text: str) -> list:
    """逐行抽 `uses:` 语句，返回 [(行号, 去掉缩进与列表符后的整句)]。

    整行注释不算：被注释掉的一行不会被执行，拿它判红等于拿散文当判据。行内 `#`
    不剥——`# vN` 是这条语句的一部分，合不合法由判据模板决定。
    """
    out = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            line = line[2:].lstrip()
        if line.startswith("uses:"):
            out.append((lineno, line))
    return out


def _violations(text: str) -> list:
    """纯函数：返回这份 workflow 里所有没有固定到 commit SHA 的 `uses:`。

    空列表 = 通过。本地 action（`./...`）没有远端仓库可固定，豁免；其余目标必须
    匹配 `uses:\\s*[^@\\s]+@[0-9a-f]{40}(?:\\s*#.*)?$`。红话带行号与整句，读的人
    不用回去搜。
    """
    out = []
    for lineno, statement in _uses_statements(text):
        target = statement[len("uses:"):].strip()
        if target.startswith("./"):
            continue
        if not _USES_PINNED.match(statement):
            out.append("第 %d 行：%s" % (lineno, statement))
    return out


WORKFLOW_NAMES = [p.name for p in workflow_files()]


def test_the_audited_workflows_are_all_present():
    """覆盖自检：目录在、枚举非空、审计名单里的每一份都还在。

    这份名单缩水（文件被删或被改名）时，上面的参数化锁会少测一份而全绿——判据面
    的消失必须红着让人看见，而不是靠"没有失败就是没问题"。
    """
    assert WORKFLOWS_DIR.is_dir(), (
        "workflow 目录 " + str(WORKFLOWS_DIR) + " 不存在：判据没有世界事实可读，"
        "参数化锁会退化成空跑。判红，不 skip。")
    names = {p.name for p in workflow_files()}
    assert names, (
        "workflow 目录 " + str(WORKFLOWS_DIR) + " 里枚举不到任何 *.yml/*.yaml："
        "目录空了或 glob 坏了，判红，不 skip。")
    missing = [name for name in AUDITED_WORKFLOWS if name not in names]
    assert not missing, (
        "审计名单里的 workflow 不见了：" + str(missing) + "（实际枚举到 "
        + str(sorted(names)) + "）。删掉一份 workflow 必须同时改 AUDITED_WORKFLOWS "
        "并说明那份的 action 固定交给谁管；不许让它静默退出判据面。")


@pytest.mark.parametrize("workflow_name", WORKFLOW_NAMES)
def test_every_uses_is_pinned_to_a_commit_sha(workflow_name):
    """真物判据：workflow 里的每条 `uses:` 都必须固定到 40 位 commit SHA。"""
    path = WORKFLOWS_DIR / workflow_name
    assert path.is_file(), (
        str(path) + " 读不到：workflow 目录就是这条锁的世界事实，收集用例时它还在、"
        "判的时候没了（被删或被改名），判红而不是跳过。")
    text = path.read_text(encoding="utf-8")
    assert text.strip(), str(path) + " 是空文件：没有内容可判，判红。"

    violations = _violations(text)
    assert not violations, (
        str(path) + " 里还有没有固定到 commit SHA 的 action 引用：\n"
        + "\n".join(violations)
        + "\n改用 `uses: <owner>/<repo>@<40 位小写十六进制 SHA> # <原版本号>`："
        "tag 与分支都是可移动引用，上游改指针时我们的 diff 里什么都看不见。")


def test_the_ruler_actually_reads_the_audited_files():
    """反空转：审计名单里的每份 workflow 都必须读出至少一条 `uses:`。

    上面那条参数化锁只会说"读到的语句都合规"，它说不出自己是不是一条都没读到。
    解析器被改坏（例如只认行首、不认 `- uses:`）时，这里先红。
    """
    blind = []
    for name in AUDITED_WORKFLOWS:
        path = WORKFLOWS_DIR / name
        assert path.is_file(), str(path) + " 读不到：反空转判据没有世界事实可读"
        if not _uses_statements(path.read_text(encoding="utf-8")):
            blind.append(name)
    assert not blind, (
        "这些 workflow 里一条 `uses:` 都没读到：" + str(blind)
        + "。要么它们真的不再调用任何 action（那样必须把它从 AUDITED_WORKFLOWS 里"
          "移走并说明固定责任落在哪），要么 _uses_statements 的读法坏了——后者会让"
          "上面那条锁在空地上判绿。")


# 合成样本：判据的两种方向都摆在一张表里。期望值写在表里，测试不重算规则。
# 应当红的是"可移动引用"与"读不出 ref"的全部形状；应当绿的是固定 SHA（带不带版本
# 注释都算）、本地 action、以及被整行注释掉的语句。
_SYNTH_CASES = (
    ("tag-ref", "      - uses: actions/checkout@v4", True),
    ("tag-ref-with-comment", "      - uses: actions/checkout@v4 # v4", True),
    ("branch-main", "      - uses: actions/checkout@main", True),
    ("branch-master", "      - uses: actions/checkout@master", True),
    ("no-ref", "      - uses: actions/checkout", True),
    ("short-sha", "      - uses: actions/checkout@11d5960", True),
    ("pinned-sha", "      - uses: actions/checkout@"
     "11d5960a326750d5838078e36cf38b85af677262", False),
    ("pinned-sha-with-version-comment", "      - uses: actions/checkout@"
     "11d5960a326750d5838078e36cf38b85af677262 # v4", False),
    ("local-action", "      - uses: ./local-action", False),
    ("commented-out-tag", "      # - uses: actions/checkout@v4", False),
)


@pytest.mark.parametrize("case_id,line,expect_violation", _SYNTH_CASES,
                         ids=[case[0] for case in _SYNTH_CASES])
def test_the_ruler_judges_the_whole_shape_table(case_id, line, expect_violation):
    """一张表同时量两边：`@vN`/`@main`/`@master`/无 `@`/短 SHA 必须报，
    固定 SHA、本地 action、注释行必须放行。只钉其中一边的话，恒真或恒假的尺子
    都能通过。
    """
    got = _violations(line + "\n")
    assert bool(got) is expect_violation, (
        "样本「" + case_id + "」的期望是 %s，实得 %r（原句 %r）"
        % ("报违规" if expect_violation else "放行", got, line))


def test_negative_control_the_ruler_flags_a_moving_ref_and_accepts_a_sha():
    """负控（交付要求的单独一条）：同一把尺子对 `@v4` 会报、对 40 位 SHA 不报。

    缺了这一对，"全部通过"可能只是因为判据恒绿。样本用合成文本，不依赖仓库里
    今天恰好改成了什么。
    """
    tag_sample = "      - uses: actions/checkout@v4\n"
    sha_sample = ("      - uses: actions/checkout@"
                  "11d5960a326750d5838078e36cf38b85af677262 # v4\n")
    assert _violations(tag_sample), (
        "`actions/checkout@v4` 没有被判违规：这把尺子对可移动引用恒绿，"
        "真物全绿不能说明任何问题。")
    assert _violations(sha_sample) == [], (
        "固定到 40 位 commit SHA 的引用被判成了违规：" + str(_violations(sha_sample))
        + "。判据模板与注释里的形状不一致时，改完 workflow 也过不了这把锁。")