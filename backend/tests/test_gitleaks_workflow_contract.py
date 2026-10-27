"""gitleaks 工作流的契约锁：钉版本镜像、脱敏输出、失败退出与全历史扫描。

这几项各自守着一件真实事故：镜像换成 `:latest` 等于每次换一份规则集；去掉
`--redact` 会把命中的凭据原文写进 CI 日志，扫描本身变成第二次泄露；去掉
`--exit-code 1` 会让"扫到了"变成绿色通过；浅克隆则让全历史扫描只看得到最后一屏。
删掉其中任何一项，本仓其余测试都不会变红，所以在这里逐项钉住。

校验入口是纯函数 `_validate(text) -> list[str]`：先剥掉 YAML/Shell 注释、再拼回
续行，然后逐项检查。注释里的命令不参与断言——把真命令注释掉、步骤换成 echo 必须
判红。`continue-on-error` 按 YAML 值判真，`fetch-depth: 0` 只在 jobs.gitleaks
块内认，别的 job 写一行不算数。
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "gitleaks.yml"
CONFIG_PATH = REPO_ROOT / ".gitleaks.toml"

_IMAGE = "ghcr.io/gitleaks/gitleaks"
_PINNED_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
# shell 里 `\` 加换行是续行，参数必须在拼回一行之后再断言，否则会被行尾切碎。
_CONTINUATION = re.compile(r"\\\s*\n\s*")
# 2 空格缩进的是 job key；4 空格起是 job 内容，0 缩进是顶层 key。
_JOB_KEY = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*$")
_FETCH_DEPTH = re.compile(r"^\s+fetch-depth:\s*[\"']?0[\"']?\s*$")
_CONTINUE_ON_ERROR = re.compile(r"(?m)^[ \t]*continue-on-error:[ \t]*(.*?)[ \t]*$")
_SWALLOW = (
    (re.compile(r"\|\|\s*true\b"), "|| true"),
    (re.compile(r"\|\|\s*exit\s+0\b"), "|| exit 0"),
    (re.compile(r";\s*true\b"), "; true"),
    (re.compile(r";\s*exit\s+0\b"), "; exit 0"),
    (re.compile(r"&&\s*true\b"), "&& true"),
)


def _raw_workflow_text() -> str:
    if not WORKFLOW_PATH.is_file():
        pytest.fail("找不到 " + str(WORKFLOW_PATH) + "：秘密扫描闸门消失必须让人看见，"
                    "判红而不是跳过。")
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """逐行剥掉 YAML/Shell 注释：从行内 `#` 起到行尾。

    gitleaks 命令里没有合法的 `#`，所以可以按第一个 `#` 切；注释里的命令
    不得参与任何断言。
    """
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _prepare(text: str) -> str:
    """先剥注释再拼续行：顺序反了会把注释里的续行拼进真命令。"""
    return _CONTINUATION.sub(" ", _strip_comments(text))


def _workflow_text() -> str:
    return _prepare(_raw_workflow_text())


def _docker_command_hits(prepared: str) -> list[str]:
    return [" ".join(line.split()) for line in prepared.splitlines()
            if "docker run" in line and _IMAGE in line]


def _find_command(prepared: str) -> str | None:
    hits = _docker_command_hits(prepared)
    return hits[0] if len(hits) == 1 else None


def _command() -> str:
    """gitleaks 那一行 docker 命令（续行已拼回）；形状对不上就红，不猜。"""
    hits = _docker_command_hits(_workflow_text())
    if len(hits) != 1:
        pytest.fail("gitleaks 工作流里应恰好有一条 `docker run ... " + _IMAGE
                    + "...` 命令，实读到 " + str(len(hits)) + " 条（" + str(hits)
                    + "）：读不出命令就无从检查参数，判红而不是跳过。"
                    "注释里的命令不算数。")
    return hits[0]


def _has_arg(command: str, arg: str) -> bool:
    return re.search(r"(?<!\S)" + re.escape(arg) + r"(?!\S)", command) is not None


def _image_errors(command: str) -> list[str]:
    refs = re.findall(re.escape(_IMAGE) + r"(?::(\S+))?", command)
    if len(refs) != 1:
        return ["gitleaks 镜像应恰好出现一次，实读 " + str(len(refs)) + " 次（"
                + command + "）。"]
    tag = refs[0]
    if not tag:
        return ["gitleaks 镜像没有 tag（实读：" + command + "）：Docker 会按 :latest "
                "解析，每次跑的可能是另一份规则集。"]
    if not _PINNED_TAG.match(tag):
        return ["gitleaks 镜像 tag 必须是 vX.Y.Z 形式，实读 :" + tag + "："
                "浮动 tag 会让扫描规则随上游漂移。"]
    return []


_REQUIRED_ARGS = ("detect", "--source /repo", "-c /repo/.gitleaks.toml")


def _detect_errors(command: str) -> list[str]:
    return ["gitleaks 命令缺了 `" + arg + "`（实读：" + command + "）：必须对着仓库根、"
            "用本仓的规则文件扫。"
            for arg in _REQUIRED_ARGS if not _has_arg(command, arg)]


def _redact_false(command: str) -> bool:
    return re.search(r"(?<!\S)--redact(?:\s*=\s*|\s+)(?:false|0|no|off)\b",
                     command, re.IGNORECASE) is not None


def _redacts(command: str) -> bool:
    return re.search(r"(?<!\S)--redact(?:=true)?(?=\s|$)", command,
                     re.IGNORECASE) is not None


def _redact_errors(command: str) -> list[str]:
    if _redact_false(command):
        return ["gitleaks 命令写了 `--redact=false`（实读：" + command + "）："
                "命中内容会以明文进 CI 日志，扫描本身成了第二次泄露。"]
    if _redacts(command):
        return []
    return ["gitleaks 命令缺了 `--redact`（实读：" + command + "）：命中内容会以明文"
            "进 CI 日志，扫描本身成了第二次泄露。"]


def _exit_code_errors(command: str) -> list[str]:
    if re.search(r"(?<!\S)--exit-code(?:\s+|=)0(?!\d)", command):
        return ["gitleaks 命令写了 `--exit-code 0`（实读：" + command + "）："
                "扫到凭据也返回 0，闸门永远是绿的。"]
    if re.search(r"(?<!\S)--exit-code(?:\s+|=)1(?!\d)", command):
        return []
    return ["gitleaks 命令缺了 `--exit-code 1`（实读：" + command + "）："
            "扫到凭据也返回 0，闸门永远是绿的。"]


def _job_block(prepared: str, job: str) -> list[str] | None:
    """按 2 空格缩进的 job key 切出某个 job 的全部行；找不到返回 None。"""
    lines = prepared.splitlines()
    start = None
    for index, line in enumerate(lines):
        match = _JOB_KEY.match(line)
        if match and match.group(1) == job:
            start = index + 1
            break
    if start is None:
        return None
    block = []
    for line in lines[start:]:
        if _JOB_KEY.match(line) or (line.strip() and not line.startswith(" ")):
            break
        block.append(line)
    return block


def _fetch_depth_errors(prepared: str) -> list[str]:
    block = _job_block(prepared, "gitleaks")
    if block is None:
        return ["找不到 jobs.gitleaks：秘密扫描闸门这个 job 本身不见了。"]
    if not any(_FETCH_DEPTH.match(line) for line in block):
        return [str(WORKFLOW_PATH) + " 的 jobs.gitleaks 里没有 `fetch-depth: 0`："
                "checkout 默认浅克隆，gitleaks 只扫得到最后一屏，历史里的凭据形状"
                "全部漏掉。写在别的 job 里的 fetch-depth 不算数。"]
    return []


def _yaml_truthy(value: str) -> bool:
    """按 YAML / GitHub 表达式判真；只有能静态判定为假的值才放行。"""
    value = value.strip()
    if value.startswith("${{") and value.endswith("}}"):
        value = value[3:-2].strip()
    value = value.strip("'\"")
    return value.lower() not in {"false", "no", "off", "0"}


def _continue_on_error_errors(prepared: str) -> list[str]:
    errors = []
    for match in _CONTINUE_ON_ERROR.finditer(prepared):
        value = match.group(1).strip()
        if _yaml_truthy(value):
            errors.append("gitleaks 工作流里 `continue-on-error: " + value + "` "
                          "为真：扫描失败必须让 job 红，否则闸门等于不存在。")
    return errors


def _swallow_errors(prepared: str) -> list[str]:
    return ["gitleaks 工作流里有把失败吞掉的写法 `" + label + "`：扫描失败必须让 "
            "job 红，否则闸门等于不存在。"
            for pattern, label in _SWALLOW if pattern.search(prepared)]


def _validate(text: str) -> list[str]:
    """纯函数：返回这份 workflow 的全部契约错误；空列表表示通过。"""
    prepared = _prepare(text)
    errors: list[str] = []
    hits = _docker_command_hits(prepared)
    if len(hits) != 1:
        errors.append("gitleaks 工作流里应恰好有一条 `docker run ... " + _IMAGE
                      + "...` 命令，实读到 " + str(len(hits)) + " 条（" + str(hits)
                      + "）：读不出命令就无从检查参数，判红而不是跳过。"
                      "注释里的命令不算数。")
    else:
        command = hits[0]
        errors.extend(_image_errors(command))
        errors.extend(_detect_errors(command))
        errors.extend(_redact_errors(command))
        errors.extend(_exit_code_errors(command))
    errors.extend(_fetch_depth_errors(prepared))
    errors.extend(_continue_on_error_errors(prepared))
    errors.extend(_swallow_errors(prepared))
    return errors


def test_workflow_file_exists():
    assert WORKFLOW_PATH.is_file(), (
        "找不到 " + str(WORKFLOW_PATH) + "：没有这份工作流就没有秘密扫描闸门，"
        "判红而不是跳过。")


def test_image_is_pinned_to_a_release_tag():
    errors = _image_errors(_command())
    assert not errors, " ".join(errors)


def test_command_detects_with_repo_source_and_project_config():
    errors = _detect_errors(_command())
    assert not errors, " ".join(errors)


def test_command_redacts_matches():
    errors = _redact_errors(_command())
    assert not errors, " ".join(errors)


def test_command_exits_nonzero_on_matches():
    errors = _exit_code_errors(_command())
    assert not errors, " ".join(errors)


def test_checkout_fetches_full_history():
    errors = _fetch_depth_errors(_workflow_text())
    assert not errors, " ".join(errors)


def test_failures_cannot_be_swallowed():
    prepared = _workflow_text()
    errors = _continue_on_error_errors(prepared) + _swallow_errors(prepared)
    assert not errors, " ".join(errors)


def test_project_config_exists():
    assert CONFIG_PATH.is_file(), (
        "找不到 " + str(CONFIG_PATH) + "：gitleaks 的 `-c /repo/.gitleaks.toml` 会指向"
        "一个不存在的文件，扫描在容器里直接失败。")
    assert CONFIG_PATH.read_text(encoding="utf-8").strip(), (
        str(CONFIG_PATH) + " 是空文件：容器里的 gitleaks 解析不出规则，"
        "开工单前先确认它是有内容的规则集。")


def test_validate_accepts_real_workflow():
    errors = _validate(_raw_workflow_text())
    assert errors == [], "真实 workflow 不该有契约错误：\n" + "\n".join(errors)


def _append_extra_job(text: str, job: str, step_lines: str) -> str:
    return (text.rstrip("\n") + "\n  " + job + ":\n    runs-on: ubuntu-latest\n"
            "    steps:\n" + step_lines)


def _without_fetch_depth(text: str) -> str:
    stripped = re.sub(r"(?m)^[ \t]*fetch-depth:[ \t]*[\"']?0[\"']?[ \t]*$", "", text)
    if stripped == text:
        pytest.fail("负样本前置条件不成立：真实 workflow 里找不到 fetch-depth: 0。")
    return stripped


def _with_exit_code_form(text: str, form: str) -> str:
    pattern = re.compile(r"(?<!\S)--exit-code(?:\s+|=)1(?!\d)")
    if not pattern.search(text):
        pytest.fail("负样本前置条件不成立：真实 workflow 里找不到 `--exit-code 1`。")
    return pattern.sub(form, text, count=1)


def _with_command_suffix(text: str, suffix: str) -> str:
    return _with_exit_code_form(text, "--exit-code 1" + suffix)


def _with_redact_form(text: str, form: str) -> str:
    pattern = re.compile(r"(?<!\S)--redact(?:=true)?(?=\s|$)", re.IGNORECASE)
    if not pattern.search(text):
        pytest.fail("负样本前置条件不成立：真实 workflow 里找不到 `--redact`。")
    return pattern.sub(form, text, count=1)


def _comment_out_gitleaks_command(text: str) -> str:
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines)
                  if "docker run" in line and _IMAGE in line), None)
    if start is None:
        pytest.fail("负样本前置条件不成立：真实 workflow 里找不到 gitleaks 命令。")
    end = start
    while end + 1 < len(lines) and "--exit-code" not in lines[end]:
        end += 1
    out = []
    for index, line in enumerate(lines):
        if start <= index <= end:
            indent = line[: len(line) - len(line.lstrip())]
            out.append(indent + "# " + line.lstrip())
        else:
            out.append(line)
    out.insert(end + 1, "          echo \"gitleaks 被挪进注释了\"")
    return "\n".join(out) + "\n"


_NEGATIVE_SAMPLES = (
    ("comment-only", _comment_out_gitleaks_command, "恰好有一条"),
    ("continue-on-error-expression",
     lambda text: _append_extra_job(
         text, "extra",
         "      - run: echo\n        continue-on-error: ${{ true }}\n"),
     "continue-on-error"),
    ("continue-on-error-capital-true",
     lambda text: _append_extra_job(
         text, "extra",
         "      - run: echo\n        continue-on-error: True\n"),
     "continue-on-error"),
    ("fetch-depth-other-job",
     lambda text: _append_extra_job(
         _without_fetch_depth(text), "other",
         "      - uses: actions/checkout@v4\n        with:\n"
         "          fetch-depth: 0\n"),
     "fetch-depth"),
    ("missing-redact", lambda text: _with_redact_form(text, ""), "--redact"),
    ("redact-false", lambda text: _with_redact_form(text, "--redact=false"),
     "--redact=false"),
    ("exit-code-zero", lambda text: _with_exit_code_form(text, "--exit-code 0"),
     "--exit-code 0"),
    ("swallow-or-true", lambda text: _with_command_suffix(text, " || true"), "|| true"),
    ("swallow-or-exit-0", lambda text: _with_command_suffix(text, " || exit 0"),
     "|| exit 0"),
    ("swallow-semicolon-true", lambda text: _with_command_suffix(text, "; true"),
     "; true"),
    ("swallow-semicolon-exit-0", lambda text: _with_command_suffix(text, "; exit 0"),
     "; exit 0"),
    ("swallow-and-true", lambda text: _with_command_suffix(text, " && true"),
     "&& true"),
)

_EQUIVALENT_SAMPLES = (
    ("exit-code-equals",
     lambda text: _with_exit_code_form(text, "--exit-code=1")),
    ("redact-equals-true",
     lambda text: _with_redact_form(text, "--redact=true")),
    ("continue-on-error-false",
     lambda text: _append_extra_job(
         text, "extra",
         "      - run: echo\n        continue-on-error: false\n")),
)


@pytest.mark.parametrize(
    ("build", "expected"),
    [(build, expected) for _, build, expected in _NEGATIVE_SAMPLES],
    ids=[name for name, _, _ in _NEGATIVE_SAMPLES],
)
def test_validate_rejects_negative_samples(build, expected):
    errors = _validate(build(_raw_workflow_text()))
    assert errors, "负样本应判红，_validate 却返回空。"
    assert any(expected in error for error in errors), (
        "负样本报错未命中 " + repr(expected) + "，实报：" + str(errors))


@pytest.mark.parametrize(
    "build",
    [build for _, build in _EQUIVALENT_SAMPLES],
    ids=[name for name, _ in _EQUIVALENT_SAMPLES],
)
def test_validate_accepts_equivalent_forms(build):
    errors = _validate(build(_raw_workflow_text()))
    assert errors == [], "合法等价写法不该判红，实报：" + str(errors)
