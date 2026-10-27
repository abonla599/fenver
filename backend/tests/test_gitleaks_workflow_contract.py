"""gitleaks 工作流的契约锁：钉版本镜像、脱敏输出、失败退出与全历史扫描。

这几项各自守着一件真实事故：镜像换成 `:latest` 等于每次换一份规则集；去掉
`--redact` 会把命中的凭据原文写进 CI 日志，扫描本身变成第二次泄露；去掉
`--exit-code 1` 会让"扫到了"变成绿色通过；浅克隆则让全历史扫描只看得到最后一屏。
删掉其中任何一项，本仓其余测试都不会变红，所以在这里逐项钉住。

只读文本、不解析 YAML：要判的就是这份文件里真正写了什么。
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


def _workflow_text() -> str:
    if not WORKFLOW_PATH.is_file():
        pytest.fail("找不到 " + str(WORKFLOW_PATH) + "：秘密扫描闸门消失必须让人看见，"
                    "判红而不是跳过。")
    return _CONTINUATION.sub(" ", WORKFLOW_PATH.read_text(encoding="utf-8"))


def _command() -> str:
    """gitleaks 那一行 docker 命令（续行已拼回）；形状对不上就红，不猜。"""
    hits = [" ".join(line.split()) for line in _workflow_text().splitlines()
            if "docker run" in line and _IMAGE in line]
    if len(hits) != 1:
        pytest.fail("gitleaks 工作流里应恰好有一条 `docker run ... " + _IMAGE
                    + "...` 命令，实读到 " + str(len(hits)) + " 条（" + str(hits)
                    + "）：读不出命令就无从检查参数，判红而不是跳过。")
    return hits[0]


def _has_arg(command: str, arg: str) -> bool:
    return re.search(r"(?<!\S)" + re.escape(arg) + r"(?!\S)", command) is not None


def test_workflow_file_exists():
    assert WORKFLOW_PATH.is_file(), (
        "找不到 " + str(WORKFLOW_PATH) + "：没有这份工作流就没有秘密扫描闸门，"
        "判红而不是跳过。")


def test_image_is_pinned_to_a_release_tag():
    command = _command()
    refs = re.findall(re.escape(_IMAGE) + r"(?::(\S+))?", command)
    assert len(refs) == 1, (
        "gitleaks 镜像应恰好出现一次，实读 " + str(len(refs)) + " 次（" + command + "）。")
    tag = refs[0]
    assert tag, (
        "gitleaks 镜像没有 tag（实读：" + command + "）：Docker 会按 :latest 解析，"
        "每次跑的可能是另一份规则集。")
    assert _PINNED_TAG.match(tag), (
        "gitleaks 镜像 tag 必须是 vX.Y.Z 形式，实读 :" + tag + "："
        "浮动 tag 会让扫描规则随上游漂移。")


def test_command_detects_with_repo_source_and_project_config():
    command = _command()
    for arg in ("detect", "--source /repo", "-c /repo/.gitleaks.toml"):
        assert _has_arg(command, arg), (
            "gitleaks 命令缺了 `" + arg + "`（实读：" + command + "）：必须对着仓库根、"
            "用本仓的规则文件扫。")


def test_command_redacts_matches():
    command = _command()
    assert _has_arg(command, "--redact"), (
        "gitleaks 命令缺了 `--redact`（实读：" + command + "）：命中内容会以明文进 CI "
        "日志，扫描本身成了第二次泄露。")


def test_command_exits_nonzero_on_matches():
    command = _command()
    assert _has_arg(command, "--exit-code 1"), (
        "gitleaks 命令缺了 `--exit-code 1`（实读：" + command + "）：扫到凭据也返回 0，"
        "闸门永远是绿的。")


def test_checkout_fetches_full_history():
    assert re.search(r"(?m)^\s*fetch-depth:\s*0\s*(?:#.*)?$", _workflow_text()), (
        str(WORKFLOW_PATH) + " 的 checkout 没有 fetch-depth: 0：默认浅克隆下 gitleaks "
        "只扫得到最后一屏，历史里的凭据形状全部漏掉。")


def test_failures_cannot_be_swallowed():
    text = _workflow_text()
    forbidden = (
        (r"(?m)^\s*continue-on-error:\s*true\b", "continue-on-error: true"),
        (r"\|\|\s*true\b", "|| true"),
        (r"\|\|\s*exit\s+0\b", "|| exit 0"),
    )
    hits = [label for pattern, label in forbidden if re.search(pattern, text)]
    assert not hits, (
        "gitleaks 工作流里有把失败吞掉的写法 " + str(hits) + "：扫描失败必须让 job 红，"
        "否则闸门等于不存在。")


def test_project_config_exists():
    assert CONFIG_PATH.is_file(), (
        "找不到 " + str(CONFIG_PATH) + "：gitleaks 的 `-c /repo/.gitleaks.toml` 会指向"
        "一个不存在的文件，扫描在容器里直接失败。")
    assert CONFIG_PATH.read_text(encoding="utf-8").strip(), (
        str(CONFIG_PATH) + " 是空文件：容器里的 gitleaks 解析不出规则，"
        "开工单前先确认它是有内容的规则集。")
