"""`tools/pre_tag_gate.py` 第 6 条判据（泄露隔离闸）的牙：它必须能红、能绿、能在判不出来的时候也红。

## 这一格守的是哪一种事故

某个提交里含过公开不了的凭据。它**不在任何分支的历史上**，可本地的 tag 能走到它——
被推走的是 ref，不是「当前分支」，所以「分支干净」不构成放行理由：一次 `git push --tags`
就把那个提交重新带回公开仓库。而 `.git/hooks/` 里没有 pre-push，今天唯一的屏障是
「发版那个人记得」。这一格把那句「记得」换成机器判据。

## 为什么这个文件必须有三份夹具，而不是一份

只会红的锁和只会绿的锁都不算锁。所以同一格在这里跑出**三种可见的结论**，每种都在 pytest 的
`tmp_path` 里现造一次性 git 仓库（**绝不在真仓库目录里 `git init`，绝不碰真仓库的 ref**——
那台机器同时是生产机）：

1. **红**：临时仓库里有一个可推送 ref 能走到被隔离的提交 ⇒ 拒绝，并且报出的名单点名那个 ref；
2. **绿的反例**：tag 与分支头**都**走不到被隔离的提交——那个提交仍然是库里的对象，只是没有
   ref 能到它，这正是把脏 ref 删干净之后仓库的形状。缺了这一条，第 1 条可能只是一把恒红的锁，
   作为证据等于零；
3. **fail-closed**：被隔离的 SHA 在这份库里根本没有对象 ⇒ `merge-base` 退 128 ⇒ **也拒绝**。
   第 2 与第 3 之间只差「对象在不在库里」，两条一起在场才证明分类读的是**退出码**，
   不是「看起来像不像出错了」（#74 咬的就是把 128 当成 1 那一口）。

后面还有一组「没有开关」的钉子：候选的 opt-out 旗名一律被 argparse 拒、`--help` 里不多一面旗、
环境变量改不出绿灯、三面既有降级旗一起带上也照样红。

## 诚实交代：哪几处是文本、哪几处是行为

- 行为：三档退出码（先让 git 当场把三个数退出来，再核闸门的分类）、红／绿／fail-closed 三种
  结论、候选旗名被拒（`SystemExit(2)`）、`--help` 的旗集合（问正在运行的解析器，不扫源码）、
  环境变量降级、`main()` 的返回值与输出行。
- 文本，而且只能这样（理由写在各条 docstring 里）：`QUARANTINED` 是 40 位十六进制的元组、
  这一格的调用点排在 `verdict=PASS` 之前、闸门读的环境变量名里没有提到这一格的。
- 断言一律落在 `returncode` 与输出行上，不落在「没抛异常」上（本仓库的老规矩）。
- 全文不写任何会腐烂的数（本地有多少枚 tag、几枚没推过——那些数一删 tag 就烂）。这一格只有
  一条不变式：**任何能推出去的 ref 都不许能走到被隔离的提交**。
"""
import hashlib
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_machine_read_pointers import strip_py_prose

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "tools" / "pre_tag_gate.py"

# 闸门第 0 步就按 `^v\d+\.\d+\.\d+$` 收 argv，所以喂进去的 tag 必须是这个形状。
TAG = "v0.99.0"
HEX40 = re.compile(r"^[0-9a-f]{40}$")

# 这一格该有的旗数是零：任何「听起来能把它关掉」的名字都必须被 argparse 当场拒掉。
NO_SUCH_FLAGS = (
    "--skip-leak-quarantine",
    "--no-leak-check",
    "--allow-leak",
    "--allow-quarantined",
    "--ignore-quarantined",
    "--leak-warn-only",
    "--no-quarantine-check",
)
# 同一件事的环境变量写法。（闸门确实读环境变量，但那是发版物料那一格的既有落点。）
NO_SUCH_ENVS = (
    "PRETAG_SKIP_LEAK_QUARANTINE",
    "PRETAG_ALLOW_LEAK",
    "PRETAG_QUARANTINE_OK",
    "QUARANTINED_OK",
    "FENVER_ALLOW_LEAK",
    "LEAK_QUARANTINE_WARN_ONLY",
)
# `--help` 里允许出现的旗：闸门本来的四面 + argparse 自带的 --help，外加 #113 的
# `--pre-build`（构建前的产源预检，与泄露隔离无关，也没有降级档）。多一面就是新开关。
DOCUMENTED_FLAGS = frozenset({
    "--expect", "--health-url", "--check-local", "--local-health-url", "--timeout",
    "--no-versionname-check", "--allow-unreachable", "--backend-only", "--pre-build", "--help",
})


def _load_gate():
    """与 `test_v0331_pre_tag_gate_contract.py` 同一手法：按路径把脚本当模块加载。

    这条路本身也是判据的一部分——`/tools/*` 在 .gitignore 里默认整目录排除，闸门靠 `!`
    例外才进得了 CI 那次 checkout；那条前提由 `test_v0331_...` 用 git check-ignore 钉着，
    这里不重复钉，只保证本文件用的是同一份加载方式。
    """
    spec = importlib.util.spec_from_file_location("pre_tag_gate", str(GATE))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo, *args):
    """在**指定的那份临时仓库**里跑一条 git，退出码交给调用方判。

    身份、签名、行尾、初始分支名全部自带 `-c`：夹具不读本机的全局配置（这台机器
    `core.autocrlf=true`，而 `git commit` 要 author identity）。
    """
    cmd = ["git",
           "-c", "user.name=gate-fixture",
           "-c", "user.email=gate-fixture@invalid.example",
           "-c", "core.autocrlf=false",
           "-c", "commit.gpgsign=false",
           "-c", "init.defaultBranch=master",
           "-C", str(repo)] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _must(repo, *args):
    """夹具用的 git：断言的是退出码，不是「命令没抛异常」。"""
    res = _git(repo, *args)
    assert res.returncode == 0, (
        "git %s 在 %s 里退了 %d（stdout=%r / stderr=%r）：夹具建不出来，后面的判据全是空转"
        % (list(args), repo, res.returncode, res.stdout, res.stderr))
    return res.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """一次性仓库：一个干净提交打底，另有一个空的 `tools/` 目录。

    那个空目录是给 `_main_at` 用的：闸门把 root 从自己的 `__file__` 往上推两级，
    把 `__file__` 指到 `<repo>/tools/pre_tag_gate.py` 之后，`main()` 全程只读这份临时仓库。
    """
    root = tmp_path / "repo"
    root.mkdir()
    _must(root, "init", "-q")
    (root / "tools").mkdir()
    (root / "clean.txt").write_text("clean work\n", encoding="utf-8")
    _must(root, "add", "clean.txt")
    _must(root, "commit", "-q", "-m", "clean work")
    return root


def _quarantined_commit(repo):
    """再补一次提交当作「被隔离的那一笔」。内容刻意写成一眼假的夹具串，免得密钥扫描器
    在一份测试夹具上当真——判据认的是**祖先关系**，不是文件名，也不是文件内容。
    """
    (repo / "leaked-credentials.txt").write_text("fixture-only-not-a-credential\n",
                                                 encoding="utf-8")
    _must(repo, "add", "leaked-credentials.txt")
    _must(repo, "commit", "-q", "-m", "the commit that carries the fixture leak")
    return _must(repo, "rev-parse", "HEAD")


def _absent_sha(repo):
    """一个形状合法、但在这份库里**没有对象**的 40 位十六进制（现算，不写死）。"""
    sha = hashlib.sha1(b"pre-tag-gate-fixture-absent-commit").hexdigest()
    assert HEX40.match(sha)
    assert _git(repo, "cat-file", "-e", sha).returncode != 0, \
        "夹具 SHA 居然撞上真对象了（%s）——换盐，否则 fail-closed 那条测不到 128" % sha
    return sha


def _delete_the_dirty_refs(repo, quarantined):
    """搭出「脏 ref 删干净之后」的形状：对象仍留在库里，但没有任何 tag/分支头能走到它。

    先 tag 那一笔、再把分支头退回干净那一笔、再删掉那枚 tag——这就是手工删完脏 tag 之后
    仓库的状态（没跑 `git gc`，对象还在，只是推不出去了）。再加两枚**到不了它**的 ref：
    一枚在干净的 tip 上，一枚在一段**无关历史**上（证明判据读的是祖先关系，不是文件名）。
    """
    _must(repo, "tag", "v9.9.9-dirty", quarantined)
    _must(repo, "reset", "--hard", "HEAD~1")
    _must(repo, "tag", "-d", "v9.9.9-dirty")
    _must(repo, "tag", "v1.0.0", "HEAD")
    _must(repo, "checkout", "-q", "--orphan", "side")
    (repo / "side.txt").write_text("unrelated history\n", encoding="utf-8")
    _must(repo, "add", "side.txt")
    _must(repo, "commit", "-q", "-m", "a root commit with no ancestry to the leak")
    _must(repo, "tag", "v2.0.0", "HEAD")
    _must(repo, "checkout", "-q", "-f", "master")


def _main_at(gate, repo, monkeypatch, extra=()):
    """让 `main()` 真的走到第 6 格：root 挪进临时仓库，前五格按本仓库既有手法接掉。

    接掉旁证是为了让每条断言只测它名义上那件事（照 `test_v0331_...` 的 `_stub_side_checks`）。
    **第 6 格本身一个 stub 都不加**——它必须真的被执行、真的自己下结论。
    """
    monkeypatch.setattr(gate, "__file__", str(Path(repo) / "tools" / "pre_tag_gate.py"))
    monkeypatch.setattr(gate, "release_note_exists", lambda root_, tag_: True)
    monkeypatch.setattr(gate, "check_announcement_materials",
                        lambda root_, ver: (True, "ok", "stubbed：本用例只核泄露隔离那一格"))
    monkeypatch.setattr(gate, "fetch_build", lambda url, timeout: (TAG, "ok"))
    return gate.main(["--expect", TAG, "--no-versionname-check"] + list(extra))


@pytest.fixture
def gate():
    return _load_gate()


# --------------------------------------------------------------------------
# 0) 三档退出码先由 git 自己证实：闸门的分类不是从 stderr 文案里猜出来的
# --------------------------------------------------------------------------

def test_git_answers_the_three_classes_with_three_different_exit_codes(repo):
    """`merge-base --is-ancestor` 在这份仓库里当场退出 0 / 1 / 128 三个不同的数。

    这条是整个文件的前提：如果 git 把「查无此对象」也退成 1，那「128 必须红」就只是一句
    空话；先让它把三个数退出来，下面的分类才有可核的对象。
    """
    quarantined = _quarantined_commit(repo)
    _must(repo, "tag", "v9.9.9", quarantined)
    _must(repo, "tag", "v0.0.1", _must(repo, "rev-parse", "HEAD~1"))

    reaches = _git(repo, "merge-base", "--is-ancestor", quarantined, "refs/tags/v9.9.9")
    assert reaches.returncode == 0, "能走到却不止 0（stderr=%r）：`reaches` 那一档失去依据" % reaches.stderr
    clean = _git(repo, "merge-base", "--is-ancestor", quarantined, "refs/tags/v0.0.1")
    assert clean.returncode == 1, "走不到却不止 1（stderr=%r）：`clean` 那一档失去依据" % clean.stderr
    absent = _git(repo, "merge-base", "--is-ancestor", _absent_sha(repo), "refs/tags/v9.9.9")
    assert absent.returncode == 128, (
        "查无此对象本该落在 128 这一档（实退 %d，stderr=%r）——把 128 读成 1 就是 #74 那一口"
        % (absent.returncode, absent.stderr))


# --------------------------------------------------------------------------
# 1) 红：可推送 ref 能走到被隔离的提交 ⇒ 拒绝，并点名那个 ref
# --------------------------------------------------------------------------

@pytest.mark.parametrize("where", ["at-the-quarantined-commit", "on-a-descendant-of-it"])
def test_red_a_tag_that_reaches_the_quarantine_is_refused(repo, gate, monkeypatch, where):
    """tag 直接指在那一笔上、以及指在它**的下游**，两种形状都必须红。

    「下游」那一支是这一格存在的全部理由：泄露藏在历史中间时，ref 的名字与 tip 的内容都
    看不出问题，只有祖先关系看得见——而 `git push` 推的正是整条祖先链。
    """
    quarantined = _quarantined_commit(repo)
    target = quarantined
    if where == "on-a-descendant-of-it":
        (repo / "later.txt").write_text("later work\n", encoding="utf-8")
        _must(repo, "add", "later.txt")
        _must(repo, "commit", "-q", "-m", "later work on top of the bad commit")
        target = _must(repo, "rev-parse", "HEAD")
    _must(repo, "tag", "v9.9.9", target)
    monkeypatch.setattr(gate, "QUARANTINED", (quarantined,))

    ok, token, detail = gate.check_leak_quarantine(str(repo))
    assert ok is False, "能走到被隔离提交的 tag 却没拦住：%s" % detail
    assert token == "ref-reaches-quarantined", "拦住了，但记号不是那一档：%s / %s" % (token, detail)
    assert "refs/tags/v9.9.9" in detail, "BLOCK 不点名 offending 的 ref，运维照着什么删？%s" % detail


def test_red_a_branch_head_is_in_scope_not_only_tags(repo, gate, monkeypatch):
    """分支头也在射程里：泄露只要回到任何一条头上，`git push` 一句话就能带走。"""
    quarantined = _quarantined_commit(repo)   # HEAD 停在这笔上，refs/heads/* 因此能走到它
    monkeypatch.setattr(gate, "QUARANTINED", (quarantined,))

    ok, token, detail = gate.check_leak_quarantine(str(repo))
    head_ref = "refs/heads/" + _must(repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert ok is False and token == "ref-reaches-quarantined"
    assert head_ref in detail, "只看了 tag、没看分支头：%s" % detail


def test_red_the_gate_refuses_to_bless_the_tag_itself(repo, gate, monkeypatch, capsys):
    """整把闸门的行为：脏 ref 在场时 `main()` 非零，`verdict=PASS` 一个字都不许出现。"""
    quarantined = _quarantined_commit(repo)
    _must(repo, "tag", "v9.9.9", quarantined)
    monkeypatch.setattr(gate, "QUARANTINED", (quarantined,))

    code = _main_at(gate, repo, monkeypatch)
    out = capsys.readouterr().out
    assert code != 0, "可推送 ref 能走到被隔离的提交，闸门却封了这一版"
    assert "leak-quarantine=REFUSED token=ref-reaches-quarantined" in out
    assert "refs/tags/v9.9.9" in out, "结论里要点名 ref：%s" % out
    assert "verdict=BLOCK reason=leak-quarantine-ref-reaches-quarantined" in out
    assert "verdict=PASS" not in out, "BLOCK 与 PASS 同时出现 = 这条锁被写成了两段话"


# --------------------------------------------------------------------------
# 2) 绿的反例：删干净之后的形状必须放绿（否则第 1 条只是一把恒红的锁）
# --------------------------------------------------------------------------

def test_green_once_no_ref_reaches_it_the_check_passes(repo, gate, monkeypatch):
    """**这就是脏 tag 被删干净之后仓库的形状**：对象还在库里，ref 都到不了它。

    没有这条反例，「红」证明不了任何事——恒红的闸门和恒绿的闸门一样没用，只是红得更早。
    """
    quarantined = _quarantined_commit(repo)
    _delete_the_dirty_refs(repo, quarantined)
    monkeypatch.setattr(gate, "QUARANTINED", (quarantined,))

    assert _git(repo, "cat-file", "-t", quarantined).returncode == 0, (
        "夹具自己坏了：这一条要证的正是「对象还在、只是到不了」，对象没了就什么都没证")
    refs, why = gate.pushable_refs(str(repo))
    assert why == "ok" and refs, "一个可推送 ref 都没列出来，下面的绿灯是空转：%r" % refs
    assert "refs/tags/v9.9.9-dirty" not in refs, "脏 tag 没删掉，这颗绿不算反例"
    for ref in refs:
        probe = _git(repo, "merge-base", "--is-ancestor", quarantined, ref)
        assert probe.returncode == 1, "夹具里 %s 其实还能走到它（rc=%d）" % (ref, probe.returncode)

    ok, token, detail = gate.check_leak_quarantine(str(repo))
    assert ok is True and token == "ok", "名单清干净了仍然红，这把锁恒红：%s" % detail


def test_green_the_gate_reaches_verdict_pass_afterwards(repo, gate, monkeypatch, capsys):
    """同一形状的整闸门版本：前五格照旧、第 6 格绿，`verdict=PASS` 这时才被印出来。"""
    quarantined = _quarantined_commit(repo)
    _delete_the_dirty_refs(repo, quarantined)
    monkeypatch.setattr(gate, "QUARANTINED", (quarantined,))

    code = _main_at(gate, repo, monkeypatch)
    out = capsys.readouterr().out
    assert code == 0, "名单清干净了还不放行：闸门会把自己变成不可用的：%s" % out
    assert "leak-quarantine=ok" in out and "verdict=PASS" in out
    assert "REFUSED" not in out


# --------------------------------------------------------------------------
# 3) fail-closed：判不出来不是「没问题」
# --------------------------------------------------------------------------

def test_fail_closed_an_undecidable_probe_blocks_not_passes(repo, gate, monkeypatch, capsys):
    """被隔离的 SHA 在这份库里没有对象 ⇒ `merge-base` 退 128 ⇒ 必须 BLOCK。

    最常写错的正是这一档：把「查无此对象」读成「没找到问题」。它与上面那条绿灯用例的
    区别只有「对象在不在库里」，两条都在才说明分类读的是退出码。
    """
    _quarantined_commit(repo)
    _must(repo, "tag", "v9.9.9", "HEAD")
    absent = _absent_sha(repo)
    monkeypatch.setattr(gate, "QUARANTINED", (absent,))

    ok, token, detail = gate.check_leak_quarantine(str(repo))
    assert ok is False, "查无此对象被当成了「干净」：这就是 fail-open：%s" % detail
    assert token == "probe-undecidable", "记号应当是判不出来那一档，读到 %r" % token
    assert "rc=128" in detail, "说明里要带上退出码，否则下次没人知道是 128：%s" % detail

    code = _main_at(gate, repo, monkeypatch)
    out = capsys.readouterr().out
    assert code == 2, "判不出来走的是「判据拿不到」那一档（2），不是已证相反（1）：%s" % out
    assert "leak-quarantine=REFUSED token=probe-undecidable" in out
    assert "verdict=PASS" not in out


def test_fail_closed_a_directory_that_is_not_a_repository_is_not_a_green_light(tmp_path, gate):
    """连 ref 清单都列不出来的目录 ⇒ BLOCK(2)。「读不到」永远不算「通过」。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "tools").mkdir()
    # 先证明这个目录真的落在任何仓库之外，否则下面测的是「某份仓库列不出 ref」，不是这一档。
    assert _git(plain, "rev-parse", "--git-dir").returncode != 0, (
        "%s 原来落在某个仓库里面，这条夹具测不到 refs-unenumerable 那一档" % plain)

    ok, token, detail = gate.check_leak_quarantine(str(plain))
    assert ok is False and token == "refs-unenumerable", (
        "一个连是不是仓库都答不上来的目录，闸门回报的是 %r / %r" % (token, detail))


def test_fail_closed_a_repository_with_nothing_to_scan_is_not_a_green_light(tmp_path, repo, gate,
                                                                           monkeypatch):
    """列得出清单却是**空的** ⇒ 同样 BLOCK。空扫描不叫"扫过且干净"。

    这一档和上一档的区别必须自己成立，否则测的是同一件事：`pushable_refs` 在这里退回的是
    `[]` 加 `why == "ok"`（git 成功应答、只是没有 tag 也没有分支头），不是 rc≠0。
    反例两半：换一份「名单清干净、有分支头」的仓库照旧放绿——否则这条是恒红锁。
    """
    empty = tmp_path / "no-refs"
    empty.mkdir()
    _must(empty, "init", "-q")
    refs, why = gate.pushable_refs(str(empty))
    assert refs == [] and why == "ok", (
        "夹具没造出「列得出来但为空」这一档（%r / %r），下面测的就不是这一件事" % (refs, why))

    ok, token, detail = gate.check_leak_quarantine(str(empty))
    assert ok is False and token == "refs-unenumerable", (
        "一个 ref 都没扫到的仓库拿到了绿灯：%r / %r" % (token, detail))

    quarantined = _quarantined_commit(repo)
    _delete_the_dirty_refs(repo, quarantined)
    monkeypatch.setattr(gate, "QUARANTINED", (quarantined,))
    refs2, why2 = gate.pushable_refs(str(repo))
    assert refs2 and why2 == "ok", "对照组夹具坏了：这份仓库连一个分支头都没有"
    ok2, token2, detail2 = gate.check_leak_quarantine(str(repo))
    assert ok2 is True and token2 == "ok", "非空清单照旧放行才是这一格的形状：%s" % detail2


def test_fail_closed_the_empty_ref_list_blocks_the_whole_gate(repo, gate, monkeypatch, capsys):
    """整闸门版本：第 6 格拿到空清单时 `verdict=PASS` 不许出现，退出码走"判不出来"那档（2）。

    这里接掉 `pushable_refs` 是为了把「没有 ref」这件事造在一份**其它条件全绿**的仓库上：
    真拿空仓库跑 `main()` 会先红在前五格，量不到这一格。
    """
    monkeypatch.setattr(gate, "pushable_refs", lambda root_: ([], "ok"))
    code = _main_at(gate, repo, monkeypatch)
    out = capsys.readouterr().out
    assert code == 2, "空清单走的是「判据拿不到」那一档（2），不是已证相反（1）：%s" % out
    assert "leak-quarantine=REFUSED token=refs-unenumerable" in out, out
    assert "verdict=PASS" not in out, "空扫描仍然封版了"


# --------------------------------------------------------------------------
# 4) 没有开关：判定是世界事实，不是操作者嘴边的旗
# --------------------------------------------------------------------------

@pytest.mark.parametrize("flag", NO_SUCH_FLAGS)
def test_no_opt_out_flag_exists_for_this_check(gate, flag):
    """行为的一面：每个「听起来能关掉这一格」的旗名，argparse 一律拒（退 2）。"""
    with pytest.raises(SystemExit) as exc:
        gate.main(["--expect", TAG, flag])
    assert exc.value.code == 2, (
        "%s 竟然被解析器收下了（退出码 %r）：这一格长出了开关" % (flag, exc.value.code))


def test_the_running_parser_advertises_no_new_flag(gate, capsys):
    """`--help` 里出现的每一面旗都必须是既有那几面——问正在运行的解析器，不扫源码。

    这比「数一数源码里有几个 store_true」强：它按**旗名**拒绝任何新增旗，而不会因为有人
    加了一条与这一格无关的正经旗就误伤（那种情形这里报的是旗名，不是一个会烂的数）。
    """
    with pytest.raises(SystemExit) as exc:
        gate.main(["--help"])
    assert exc.value.code == 0, "--help 不是正常退出：%r" % exc.value.code
    seen = set(re.findall(r"--[a-z][a-z0-9-]*", capsys.readouterr().out))
    extra = seen - DOCUMENTED_FLAGS
    assert not extra, "闸门多了一面旗 %s：泄露名单的判定不许变成操作者手边的开关" % sorted(extra)


def test_no_environment_variable_turns_the_check_off(repo, gate, monkeypatch, capsys):
    """把六个「大概是逃生门吧」的变量一次设满，第 6 格照样红，而且照样点名 ref。"""
    quarantined = _quarantined_commit(repo)
    _must(repo, "tag", "v9.9.9", quarantined)
    monkeypatch.setattr(gate, "QUARANTINED", (quarantined,))
    for name in NO_SUCH_ENVS:
        monkeypatch.setenv(name, "1")

    code = _main_at(gate, repo, monkeypatch)
    out = capsys.readouterr().out
    assert code != 0, "环境变量把这一格拧开了：那它就不是机制"
    assert "leak-quarantine=REFUSED token=ref-reaches-quarantined" in out
    assert "refs/tags/v9.9.9" in out
    assert "verdict=PASS" not in out


@pytest.mark.parametrize("extra", [
    (),
    ("--allow-unreachable",),
    ("--backend-only",),
    ("--check-local",),
    ("--allow-unreachable", "--backend-only", "--check-local"),
])
def test_the_existing_degradation_flags_do_not_reach_this_check(repo, gate, monkeypatch,
                                                               capsys, extra):
    """三面降级旗各自降级的是别的判据；一起带上也照样红在这一格。

    `--allow-unreachable` 是唯一可能让人以为「离线就能过」的那面旗，所以这组里必须带它。
    """
    quarantined = _quarantined_commit(repo)
    _must(repo, "tag", "v9.9.9", quarantined)
    monkeypatch.setattr(gate, "QUARANTINED", (quarantined,))

    code = _main_at(gate, repo, monkeypatch, extra=extra)
    out = capsys.readouterr().out
    assert code != 0, "带着 %s 就把含泄露的 ref 封成了版本（退出码 %d）" % (list(extra), code)
    assert "leak-quarantine=REFUSED" in out, "%s 之下这一格根本没被执行：%s" % (list(extra), out)
    assert "verdict=PASS" not in out


# --------------------------------------------------------------------------
# 5) 结构性钉子（文本的那几处，理由见文件头的「诚实交代」）
# --------------------------------------------------------------------------

def test_quarantine_list_is_a_tuple_of_full_shas(gate):
    """隔离清单的形状：元组、非空、每一项都是完整 40 位小写十六进制、不重复。

    短 SHA 会让 `merge-base` 解析成歧义或干脆 128——而这一格的全部价值就在于把
    「证明到不了」与「解析不了」分清，所以清单里不许出现缩写。用元组不用列表：加一项是
    改一行数据、过一次 review，不是运行期能拼出来的东西。
    """
    assert isinstance(gate.QUARANTINED, tuple), "清单应当是元组（运行期不改写的形状）"
    assert gate.QUARANTINED, "隔离清单是空的：这一格会恒绿，等于没有这一格"
    for item in gate.QUARANTINED:
        assert isinstance(item, str) and HEX40.match(item), (
            "被隔离的提交必须是 40 位小写十六进制的完整 SHA，读到 %r" % (item,))
    assert len(set(gate.QUARANTINED)) == len(gate.QUARANTINED), "同一项列了两遍：名单要一眼数得清"


def test_the_check_runs_before_the_one_and_only_green_line():
    """位置钉（文本）：这一格的调用点排在 `verdict=PASS` 之前，而 PASS 全文件只印一次。

    为什么这里只能按文本：源码里的顺序就是执行顺序；「红的时候 PASS 不出现」已经由行为那条
    覆盖了，这一条兜的是「把这一格整段挪到 PASS 之后」或「把 PASS 多印一份到前面」。

    射程：读的是 `strip_py_prose` 剥掉注释与 docstring 的那一份，坐标（行号、字符偏移）与原文
    一致，所以判序仍然对得上文件里真实的先后。原文时代有两刀：注释里多印一次 `PRETAG
    verdict=PASS` 就把"只印一次"那枚钉假红掉；而禁字那圈从前只拿三条引号去 split、取模块
    docstring 之后的**一小截**，旗名抄在那一截之后照样过，现在禁字扫的是整份可执行代码。
    """
    src = strip_py_prose(GATE.read_text(encoding="utf-8"))
    assert src.count("PRETAG verdict=PASS") == 1, "PASS 行不止一处，那就没有「最后才放行」这回事"
    assert src.index("check_leak_quarantine(root)") < src.index("PRETAG verdict=PASS"), (
        "泄露隔离闸挪到了 `verdict=PASS` 之后：它能被跳过，就不再是闸")
    for word in ("skip_leak", "SKIP_LEAK", "allow_leak", "ALLOW_LEAK", "warn_only"):
        assert word not in src, "源码里出现 %r 这一类名字：那是一面旗或一个逃生门的形状" % word


def test_no_environment_read_names_this_check():
    """（文本）闸门读的环境变量名里，没有一个提到这一格。

    行为那一面由 `test_no_environment_variable_turns_the_check_off` 负责；但「没有新读一个
    变量」没法穷举所有可能的变量名，所以这里把源码里真实存在的那几处读点扫一遍——它只可能
    漏在「变量名是拼出来的」那种写法上，而那种写法本身就该在 review 里被拦下。

    射程：扫的是 `strip_py_prose` 那一份（注释与 docstring 原位刷空）。原文里一句"我们不给它留
    `getenv` 读 `PRETAG_SKIP…` 这种口子"的说明，就会在穷举面上凭空多出一个读点，判据因为自己
    说的话而红。
    """
    src = strip_py_prose(GATE.read_text(encoding="utf-8"))
    names = set(re.findall(r'(?:getenv\(|environ\.get\(|environ\[)\s*"?"?([A-Za-z_]'
                           r'[A-Za-z0-9_]*)', src))
    mod = _load_gate()
    # 常量名解析成它们的值：这一把锁把环境变量名放在常量里（MANIFEST_ENV_VAR 等）。
    resolved = {(getattr(mod, n, None) if isinstance(getattr(mod, n, None), str) else n)
                for n in names}
    bad = sorted(x for x in resolved if re.search(r"leak|quarantin", x, re.I))
    assert not bad, "第 6 格的判定被挂到了环境变量 %s 上：那是第四面旗" % bad
    assert "RELEASE_MANIFEST_PATH" in resolved and "APK_CACHE_DIR" in resolved, (
        "扫出来的读点应当包含发版物料那两处的既有落点；这里空了就说明正则没匹配上，"
        "上面那条断言也跟着空转")
