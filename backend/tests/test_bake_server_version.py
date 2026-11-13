"""#322(a)：构建戳生成器 `tools/bake_server_version.py` 的契约锁。

旧配方 `git describe --tags --abbrev=0` 取"最近一枚"，tag 命名空间分家后可能把
安卓线号烘进服务端包（/health 的 build 退成安卓号）。新判据：`git tag --points-at
HEAD` 按服务端正则过滤后**必须恰好一枚**，否则拒绝构建（fail-closed）。

判据源是 `tmp_path` 现造的一次性 git 仓库的**真实输出**，不是文本：0 枚⇒拒 /
恰 1 枚⇒出串 / 2 枚⇒拒 三档 + 过滤 + 必填 --out + 单源正则 + CI checkout 前提。
**绝不在真仓库 git init、绝不写真 version.txt**：所有 git 操作与输出都落在 tmp_path。
"""
import ast
import importlib
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "bake_server_version.py"
GUIDE = REPO_ROOT / "docs" / "安装部署指南.md"
AGENTS = REPO_ROOT / "AGENTS.md"


def _load_bake():
    spec = importlib.util.spec_from_file_location("bake_server_version_under_test", str(TOOL))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git_repo(parent, tags=(), annotated=False):
    """一次性 git 仓库：一个提交 + 可选的 tag。只在 tmp_path 里造，不碰真仓库。"""
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / ("repo-%d" % len(list(parent.glob("repo-*"))))
    root.mkdir()

    def git(*args):
        r = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (args, r.stderr)
        return r.stdout

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    (root / "f.txt").write_text("x\n", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-qm", "init")
    for tag in tags:
        if annotated:
            git("tag", "-a", tag, "-m", tag)
        else:
            git("tag", tag)
    return root


def _out(tmp_path):
    out = tmp_path / "stamp" / "version.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


# ---------------------------------------------------------------- 三档枚数判据

def test_zero_server_tags_refuses_and_writes_nothing(tmp_path):
    root = _git_repo(tmp_path)
    out = _out(tmp_path)
    bake = _load_bake()
    rc = bake.main(["--repo-root", str(root), "--out", str(out)])
    assert rc == 1, "HEAD 上一枚服务端 tag 都没有却烘了戳"
    assert not out.exists(), "拒绝时不许留下输出文件"


def test_exactly_one_server_tag_writes_the_tag(tmp_path):
    root = _git_repo(tmp_path, tags=("v1.2.3",), annotated=True)
    out = _out(tmp_path)
    bake = _load_bake()
    rc = bake.main(["--repo-root", str(root), "--out", str(out)])
    assert rc == 0
    assert out.read_bytes() == b"v1.2.3\n", "烘进去的必须是那枚 tag 本身（ascii + 换行）"


def test_two_server_tags_refuse_and_write_nothing(tmp_path):
    root = _git_repo(tmp_path, tags=("v1.2.3", "v1.2.4"))
    out = _out(tmp_path)
    bake = _load_bake()
    rc = bake.main(["--repo-root", str(root), "--out", str(out)])
    assert rc == 1, "两枚服务端 tag 指着同一提交却被放行：版本权威不唯一"
    assert not out.exists()


def test_non_server_tags_are_filtered_out(tmp_path):
    """安卓线/宽形状 tag 不算服务端 tag：过滤后恰一枚 ⇒ 出串；一枚都没有 ⇒ 拒。"""
    root = _git_repo(tmp_path, tags=("android-v1.2.3", "v1.2", "v1.2.3"))
    out = _out(tmp_path)
    bake = _load_bake()
    assert bake.main(["--repo-root", str(root), "--out", str(out)]) == 0
    assert out.read_bytes() == b"v1.2.3\n"

    only_android = _git_repo(tmp_path / "only", tags=("android-v9.9.9",))
    out2 = _out(tmp_path / "only")
    assert bake.main(["--repo-root", str(only_android), "--out", str(out2)]) == 1
    assert not out2.exists()


def test_out_is_required_no_default(tmp_path):
    """--out 必填无默认：version.txt 是运行时资产，默认输出路径一次误跑就覆写生产戳。

    用 0 枚 tag 的假树跑：即使判据被改宽，也不会有任何输出落到真仓库。
    """
    root = _git_repo(tmp_path)
    bake = _load_bake()
    with pytest.raises(SystemExit) as exc:
        bake.main(["--repo-root", str(root)])
    assert exc.value.code == 2, "缺 --out 必须由 argparse 当场拒绝"


def test_git_unreadable_fails_closed_with_rc2(tmp_path):
    bake = _load_bake()
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    out = _out(tmp_path)
    rc = bake.main(["--repo-root", str(not_a_repo), "--out", str(out)])
    assert rc == 2, "git 读不出必须退 2（判据拿不到），不是 1 也不是 0"
    assert not out.exists()


def test_unwritable_out_fails_closed_with_rc2(tmp_path):
    """输出路径写不进（这里传一个目录）⇒ 退 2，不烘。"""
    root = _git_repo(tmp_path, tags=("v3.0.0",))
    bake = _load_bake()
    rc = bake.main(["--repo-root", str(root), "--out", str(tmp_path)])
    assert rc == 2, "写不进输出文件却报了成功"


def test_the_script_writes_only_the_out_path(tmp_path):
    """只写 --out：跑完之后假树里不许多出任何文件（version.txt 也不许出现在树里）。"""
    root = _git_repo(tmp_path, tags=("v2.0.0",))
    before = {p.relative_to(root) for p in root.rglob("*")}
    out = _out(tmp_path)
    bake = _load_bake()
    assert bake.main(["--repo-root", str(root), "--out", str(out)]) == 0
    after = {p.relative_to(root) for p in root.rglob("*")}
    assert after == before, "脚本在 --out 之外写了东西：%r" % (after - before,)


# ---------------------------------------------------------------- 单源与前提

def test_full_tag_re_is_imported_not_copied():
    """服务端正则只许 import release_marker.FULL_TAG_RE，不许在脚本里重抄第三份。"""
    bake = _load_bake()
    shared = importlib.import_module("release_marker")
    assert bake.FULL_TAG_RE is shared.FULL_TAG_RE, "正则不是同一个对象：又抄了一份"
    gate_src = (REPO_ROOT / "tools" / "pre_tag_gate.py").read_text(encoding="utf-8")
    assert bake.FULL_TAG_RE.pattern == r"^v\d+\.\d+\.\d+$"
    assert r"^v\d+\.\d+\.\d+$" in gate_src, "闸门那条同串正则不在了：对照失去参照"
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "FULL_TAG_RE"
                           for t in node.targets)
                   for node in ast.walk(tree)), "脚本里又本地定义了 FULL_TAG_RE"


def test_the_script_is_in_the_repo_not_only_on_this_machine():
    """`/tools/*` 默认全排除；脚本进不了 CI 那次 checkout，上面那批锁就全在空转。

    用 `--no-index` 纯按规则判：脚本一旦入库，不带它的 check-ignore 对已跟踪路径
    恒回"未忽略"，摘掉 ! 例外也不会红（假阴性）。--no-index 才量得到例外本身。
    """
    assert TOOL.is_file()
    res = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--no-index",
                          "--", "tools/bake_server_version.py"],
                         capture_output=True, timeout=60)
    assert res.returncode == 1, (
        "tools/bake_server_version.py 被 .gitignore 挡住了（rc=%d）：加一行 "
        "!/tools/bake_server_version.py（末尾追加，不插空行）" % res.returncode)


# ---------------------------------------------------------------- 文本配方同步

def test_the_guide_recipe_calls_the_bake_script():
    """唯一执行点必须换成脚本；旧 describe 配方不许再是执行点，距离后缀残留要写明。"""
    text = GUIDE.read_text(encoding="utf-8")
    assert "tools\\bake_server_version.py --out version.txt" in text, (
        "部署指南的烤包配方没调用构建戳生成器")
    assert "git describe --tags --abbrev=0 | Out-File" not in text, (
        "旧 git describe 配方仍是执行点：它会取最近一枚（可能是安卓 tag）")
    assert "rev-list --count" in text and "距离后缀" in text, (
        "describe 距离后缀失真这一残留没写进指南（含正确算法）")


def test_agents_md_names_the_bake_script_and_the_distance_residual():
    text = AGENTS.read_text(encoding="utf-8")
    assert "bake_server_version.py" in text, "AGENTS.md §5 没提构建戳生成器"
    assert "rev-list --count" in text, "AGENTS.md 没写服务端开发距离的正确算法"