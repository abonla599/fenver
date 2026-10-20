"""`tools/pre_tag_gate.py --pre-build`（#113）：构建前必须证明"这棵树就是那一笔 tag"。

## 这一格守的是哪一种事故

包里的版本戳是构建时烘进去的（`version.txt`），构建本身从不问 git"这段字节来自哪一笔"。
于是在一棵脏树、或 HEAD 停在 tag 之前的提交上构建，产出的包**照样自称 vX**：版本号是真的，
内容不是。打 tag 只保证 ref 存在，不保证正在构建的那棵树就是它。

判据只有两条，都是世界事实：

1. `git rev-parse HEAD` == `git rev-parse v<tag>^{commit}`；
2. `git status --porcelain` 为空（含未跟踪文件——PyInstaller 会把树里的东西收进包）。

## 为什么这个文件里必须有反例

- **脏树**：改一个已跟踪文件、以及新增一个未跟踪文件，两种都必须 rc≠0。只测干净的绿灯，
  这条锁可能根本没在读 `status`；
- **HEAD 落后于 tag**：tag 打在 HEAD~1 上必须 rc≠0。只测"HEAD 比 tag 新"会漏掉方向；
- **tag 不存在 / 目录不是仓库**：判不出来与已证相反必须分档（1 vs 2），沿用本仓库对
  `pre_tag_gate.py` 既有各格的退出码口径。

全部在 `tmp_path` 里现造一次性 git 仓库：这台机器同时是生产机，**绝不在真仓库里 tag/commit**。
另有一组"没有开关"的钉子：候选逃生旗被 argparse 拒，既有降级旗与 `--pre-build` 合用时当场退 2
——脏树不是"取不到"，它是反的，不接受任何降级。
"""
import importlib.util
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "tools" / "pre_tag_gate.py"

# 闸门第 0 步按 `^v\d+\.\d+\.\d+$` 收 argv；用一枚合成 tag，绝不碰真仓库里已有的 ref。
TAG = "v0.99.0"

# 任何"听起来能跳过产源预检"的旗名都必须被 argparse 当场拒掉（SystemExit(2)）。
NO_SUCH_FLAGS = (
    "--allow-dirty",
    "--skip-pre-build",
    "--ignore-dirty",
    "--no-provenance-check",
    "--skip-provenance",
    "--dirty-ok",
)


def _load_gate():
    """与既有闸门测试同一手法：按路径把脚本当模块加载。"""
    spec = importlib.util.spec_from_file_location("pre_tag_gate", str(GATE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo, *args):
    """在**指定的那份临时仓库**里跑一条 git；身份/行尾/签名全部自带，不读本机配置。"""
    cmd = ["git",
           "-c", "user.name=gate-fixture",
           "-c", "user.email=gate-fixture@invalid.example",
           "-c", "core.autocrlf=false",
           "-c", "commit.gpgsign=false",
           "-c", "init.defaultBranch=master",
           "-C", str(repo)] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _must(repo, *args):
    """夹具用的 git：断言退出码，而不是"命令没抛异常"。"""
    res = _git(repo, *args)
    assert res.returncode == 0, (
        "git %s 在 %s 里退了 %d（stdout=%r / stderr=%r）：夹具建不出来，后面的判据全是空转"
        % (list(args), repo, res.returncode, res.stdout, res.stderr))
    return res.stdout.strip()


@pytest.fixture
def gate():
    return _load_gate()


@pytest.fixture
def repo(tmp_path):
    """一次性仓库：一个干净提交打底，另有一个空的 `tools/` 目录。

    `tools/` 是给 `_main_at` 用的：闸门把 root 从自己的 `__file__` 往上推两级，
    把 `__file__` 指到 `<repo>/tools/pre_tag_gate.py` 之后，`main()` 全程只读这份临时仓库。
    本地配置钉 `core.autocrlf=false`：`check_build_provenance` 里的 `git status` 不带
    夹具那几枚 `-c`，不钉的话行尾转换会把干净树读成脏（这台机器 `core.autocrlf=true`）。
    """
    root = tmp_path / "repo"
    root.mkdir()
    _must(root, "init", "-q")
    _must(root, "config", "core.autocrlf", "false")
    (root / "tools").mkdir()
    (root / "clean.txt").write_text("clean work\n", encoding="utf-8")
    _must(root, "add", "clean.txt")
    _must(root, "commit", "-q", "-m", "clean work")
    return root


def _main_at(gate, repo, monkeypatch, extra=()):
    """让 `main()` 真的走 `--pre-build` 那一支，root 挪进临时仓库。这一格本身零 stub。"""
    monkeypatch.setattr(gate, "__file__", str(Path(repo) / "tools" / "pre_tag_gate.py"))
    return gate.main(["--expect", TAG, "--pre-build"] + list(extra))


def test_clean_tree_at_the_tag_passes(repo, gate, monkeypatch, capsys):
    """绿的反例：HEAD 就是 tag 那一笔、工作树干净 ⇒ rc=0。

    没有这条，"脏树必红"证明不了任何事——恒红的锁和恒绿的锁一样没用。
    """
    _must(repo, "tag", TAG)
    code = _main_at(gate, repo, monkeypatch)
    out = capsys.readouterr().out
    assert code == 0, "干净树 + HEAD 就在 tag 上，预检却拦了构建：%s" % out
    assert "pre-build=ok" in out, "绿灯没有留下可核的结论行：%s" % out


def test_an_annotated_tag_is_peeled_to_its_commit(repo, gate, monkeypatch, capsys):
    """annotated tag 的 `rev-parse` 回的是 tag 对象，不是提交：必须按 `^{commit}` 剥一层。

    少了这一层，仓里用 annotated tag 的那一版会在构建前恒红，而它恰恰是最规范的那种 tag。
    """
    _must(repo, "tag", "-a", TAG, "-m", "annotated release tag")
    code = _main_at(gate, repo, monkeypatch)
    out = capsys.readouterr().out
    assert code == 0, "annotated tag 没被剥到提交：%s" % out
    assert "pre-build=ok" in out


@pytest.mark.parametrize("shape", ["modified-tracked", "untracked"])
def test_a_dirty_tree_blocks(repo, gate, monkeypatch, capsys, shape):
    """**反例**：已跟踪文件改过、以及树里多了一个未跟踪文件，两种都必须 rc≠0。

    未跟踪那一支不是凑数：PyInstaller 收的是树上的文件，不查 git 索引——一个没提交的新
    文件同样会变成"包里有、tag 里没有"的字节，而这正是产源预检要拦的形状。
    """
    _must(repo, "tag", TAG)
    if shape == "modified-tracked":
        (repo / "clean.txt").write_text("dirty work\n", encoding="utf-8")
    else:
        (repo / "new-bytes.txt").write_text("not committed\n", encoding="utf-8")

    code = _main_at(gate, repo, monkeypatch)
    out = capsys.readouterr().out
    assert code != 0, "脏树（%s）拿到了绿灯：包里会混进不属于 %s 的字节" % (shape, TAG)
    assert "pre-build=REFUSED token=tree-dirty" in out, "拒绝的理由不是脏树：%s" % out
    assert "verdict=PASS" not in out, "BLOCK 与 PASS 同时出现 = 这一格被写成了两段话"


def test_head_behind_the_tag_blocks(repo, gate, monkeypatch, capsys):
    """**反例（HEAD-behind-tag）**：tag 打在 HEAD~1 上 ⇒ rc≠0，并同时点名两个 SHA。

    这是"在 tag 之前多提交了一笔、标签还留在后面"的形状；此时构建出的包把后一笔的字节
    烘进去，却顶着 tag 的版本号。只核对"tag 存在"（而不是 HEAD == tag）的判据会放过它。
    """
    _must(repo, "tag", TAG)
    (repo / "later.txt").write_text("later work\n", encoding="utf-8")
    _must(repo, "add", "later.txt")
    _must(repo, "commit", "-q", "-m", "later work after the tag")

    code = _main_at(gate, repo, monkeypatch)
    out = capsys.readouterr().out
    assert code != 0, "HEAD 落后于 tag 却放行了构建：包会自称 %s、内容是 HEAD 的" % TAG
    assert "pre-build=REFUSED token=head-not-at-tag" in out, "拒绝的理由不是 HEAD 错位：%s" % out
    assert "verdict=PASS" not in out

    head = _must(repo, "rev-parse", "HEAD")
    tag_sha = _must(repo, "rev-parse", "%s^{commit}" % TAG)
    assert head != tag_sha, "夹具自己坏了：HEAD 与 tag 居然是同一笔，这条反例什么都没测"
    assert head[:12] in out and tag_sha[:12] in out, "说明里要同时给出两个 SHA，人才知道差在哪：%s" % out


def test_a_missing_tag_blocks(repo, gate, monkeypatch, capsys):
    """tag 根本不存在 ⇒ rc≠0（已证相反那一档，1）：构建现在不可能来自它。

    与"目录不是仓库"那条的区别必须自己成立：这里 git 答得出 HEAD，只是没有这个 ref。
    """
    code = _main_at(gate, repo, monkeypatch)
    out = capsys.readouterr().out
    assert code == 1, "没有 tag 时应当是「已证相反」那一档（1），读到 %d：%s" % (code, out)
    assert "pre-build=REFUSED token=tag-missing" in out, out
    assert "verdict=PASS" not in out


def test_a_directory_that_is_not_a_repository_is_rc_two(tmp_path, gate, monkeypatch, capsys):
    """判不出来不是"没问题"：不是 git 仓库 ⇒ rc=2（与线上取不到同一档），不是 1。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "tools").mkdir()
    assert _git(plain, "rev-parse", "--git-dir").returncode != 0, (
        "%s 原来落在某个仓库里面，这条夹具测不到 git-unreadable 那一档" % plain)

    code = _main_at(gate, plain, monkeypatch)
    out = capsys.readouterr().out
    assert code == 2, "判不出来应当走 2，读到 %d：%s" % (code, out)
    assert "pre-build=REFUSED token=git-unreadable" in out, out


@pytest.mark.parametrize("flag", NO_SUCH_FLAGS)
def test_no_skip_flag_exists_for_the_preflight(gate, flag):
    """**没有跳过开关**：每个"听起来能关掉产源预检"的旗名，argparse 一律拒（退 2）。"""
    with pytest.raises(SystemExit) as exc:
        gate.main(["--expect", TAG, "--pre-build", flag])
    assert exc.value.code == 2, "%s 竟然被解析器收下了：这一格长出了开关" % flag


@pytest.mark.parametrize("flag", ["--allow-unreachable", "--backend-only",
                                  "--check-local", "--no-versionname-check"])
def test_degradation_flags_cannot_be_combined_with_the_preflight(gate, flag):
    """既有降级旗与 `--pre-build` 合用时当场退 2：它们降级的都是别的格，在这里不生效。

    静默忽略一面旗就是让操作者以为"我带了 --allow-unreachable，离线能过"——而脏树/错位
    HEAD 不是可达性问题，它没有降级档。
    """
    with pytest.raises(SystemExit) as exc:
        gate.main(["--expect", TAG, "--pre-build", flag])
    assert exc.value.code == 2, "%s 与 --pre-build 合用没有当场拒掉（退出码 %r）" % (
        flag, exc.value.code)
