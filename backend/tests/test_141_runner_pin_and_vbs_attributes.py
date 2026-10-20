"""#141：发版 lane 的 runner 版本与 `.vbs` 行尾声明都必须由仓库说了算。

Ubuntu 26 迁移说明把 `ubuntu-latest` 变成一个会自己变的输入。签名产物需要可复现的
执行环境，所以 `release-apk.yml` 钉 `ubuntu-24.04`；测试/扫描/原生构建继续用
`latest`，让镜像漂移尽早暴露。`.vbs` 是 wscript 直接执行的隐藏入口，和 `.ps1`/`.bat`
一样不能靠检出机的 `core.autocrlf` 兜着。
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RELEASE = ".github/workflows/release-apk.yml"
LATEST_LANES = (
    ".github/workflows/tests.yml",
    ".github/workflows/gitleaks.yml",
    ".github/workflows/build-native-apk.yml",
)


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _runner_of(text: str) -> str:
    match = re.search(r"^\s*runs-on:\s*(\S+)\s*$", text, re.MULTILINE)
    assert match, "workflow 里找不到 runs-on"
    return match.group(1)


def _vbs_rule_declared(attrs: str) -> bool:
    return bool(re.search(r"^\*\.vbs\s+text\s+eol=crlf\s*$", attrs, re.MULTILINE))


def test_the_release_lane_is_pinned_and_other_lanes_stay_on_latest():
    assert _runner_of(_read(RELEASE)) == "ubuntu-24.04", \
        "发版 lane 必须钉 24.04：签名产物不能跟着 ubuntu-latest 漂"
    for rel in LATEST_LANES:
        assert _runner_of(_read(rel)) == "ubuntu-latest", \
            f"{rel} 不在发版 lane，应继续用 latest 以暴露镜像漂移"


def test_every_tracked_vbs_file_is_declared_text_crlf():
    attrs = _read(".gitattributes")
    assert _vbs_rule_declared(attrs), ".gitattributes 缺 `*.vbs text eol=crlf`"
    listed = subprocess.run(["git", "ls-files", "*.vbs"], cwd=REPO,
                            capture_output=True, text=True, timeout=20)
    assert listed.returncode == 0, listed.stderr
    files = listed.stdout.split()
    assert files, "一个 .vbs 都没扫到——路径或 glob 坏了"
    checked = subprocess.run(["git", "check-attr", "text", "eol", "--", *files],
                             cwd=REPO, capture_output=True, text=True, timeout=20)
    assert checked.returncode == 0, checked.stderr
    for rel in files:
        rows = [line for line in checked.stdout.splitlines()
                if line.startswith(rel + ": ")]
        assert len(rows) == 2, f"{rel} 的 check-attr 输出形状不对：{rows}"
        assert any("text: set" in row for row in rows), rows
        assert any("eol: crlf" in row for row in rows), rows


def test_the_vbs_rule_check_can_go_red():
    assert _vbs_rule_declared("*.vbs text eol=crlf\n")
    assert not _vbs_rule_declared("# only ps1\n*.ps1 text eol=crlf\n")
    assert not _vbs_rule_declared("*.vbs text\n")
