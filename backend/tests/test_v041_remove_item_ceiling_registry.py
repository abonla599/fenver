"""#302：Remove-Item 白名单锁的间接执行天花板，做成可执行的反证（2026-11-29）。

锁在保什么
----------
test_swap_package_contract_v0291.py::_remove_item_violations 是一套**正向白名单**：
扫出源码里每一处 Remove-Item，要求它的 path 实参每个数组元素都是 env: token，
并把 ri / rm / rmdir / del / rd 一族别名整体禁掉。

它保的东西是对的，而且重要：**备份包是唯一的回滚位**。删了它，换包失败就没有退路。
白名单是唯一没法被话术绕过的形状——「前缀剥离」那版（#295）只要把回滚目录挂在 env:
变量后面当第二个数组元素就判绿，而命令实际会删掉回滚位（C3e/C3f 两种写法）。

天花板在哪
----------
白名单判的是「**这个名字**有没有被用于删东西」。于是它只看 Remove-Item 与其别名。
PowerShell 里删掉一个目录还有别的路，**每条都不含那几个名字**，因此全部判绿：

  ① [System.IO.Directory]::Delete(dir, true)      —— .NET API，递归删
  ② [System.IO.File]::Delete(file)                —— .NET API，单文件
  ③ [System.IO.Directory]::Move(a, b)             —— 挪走 = 实质上丢了回滚位
  ④ Clear-Content file                            —— 内容清空，包成空壳
  ⑤ cmd /c rd /s /q dir                           —— 交给 cmd，在另一个进程里
  ⑥ Get-ChildItem ... | ForEach-Object { $_.Delete() }  —— 对象方法

本文件把 ①②③④ 做成可执行反证（⑤⑥ 只登记，理由见下）：它们**今天全部判绿**。
也就是说，「旧包只改名不删除」这句话，只能在「用的是 Remove-Item 这条路」时成立。

一个必须说清的讽刺
------------------
swap-package.ps1 里清 marker 那一句，2026-11-28 我自己从 Remove-Item 改成了
[System.IO.File]::Delete —— 起因是 Remove-Item 撞上了这条白名单。那个改动本身是对的
（marker 不是回滚位），但它**正好走在①②这条天花板上**。所以这条天花板不是假想的：
它今天已经在被使用，只是这次指向的是无害目标。

真修的方向（不在本轮射程）
--------------------------
判据从「命令名白名单」升级成「能不能证明这条语句动不到 dist\\run_backend_old-* 与
dist\\run_backend」——那是数据流/污点分析，不是正则能做的。或者反过来：
**把回滚位挪出这个进程的可写范围**（备份到另一块盘/另一个账户下），让「删了它」在
物理上就做不到。后者更稳，但要改部署形态。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_swap_package_contract_v0291 import _remove_item_violations    # noqa: E402

# 路径全是**不存在的假路径**：这些语句不会被真的执行，只喂给纯函数判据。
# 这一点必须写明——否则下一个人以为这个文件会去删东西。
_FAKE_ROLLBACK = "C:\\nonexistent-302-probe\\run_backend_old-20260101-000000"

_BYPASSES = (
    ("dotnet-directory-delete",
     '[System.IO.Directory]::Delete("' + _FAKE_ROLLBACK + '", $true)'),
    ("dotnet-file-delete",
     '[System.IO.File]::Delete("' + _FAKE_ROLLBACK + '\\run_backend.exe")'),
    ("dotnet-directory-move-away",
     '[System.IO.Directory]::Move("' + _FAKE_ROLLBACK + '", "' + _FAKE_ROLLBACK + '-gone")'),
    ("clear-content",
     'Clear-Content "' + _FAKE_ROLLBACK + '\\run_backend.exe"'),
)


def test_the_whitelist_still_catches_what_it_was_built_for():
    """正对照：白名单对「本来就该抓的那几种」必须仍然红。

    没有这条，下面的反证可能只是「判据整体失效了」——那也会让它们全判绿。
    """
    assert _remove_item_violations("Remove-Item -Recurse -Force $RollbackDir"), "白名单已经不抓目录删除了"
    assert _remove_item_violations('rd /s /q "$Live"'), "别名 rd 已经不抓了"
    assert not _remove_item_violations('Remove-Item ("env:" + $n)'), "合规的 env: 删除被误判成违规"


def test_each_dotnet_bypass_sails_past_the_whitelist_today():
    """①②③④ 今天全部判绿。

    红的含义是「有人把判据升级到能看穿这几条了」——那不是坏事，请把对应的形状从这里删掉
    并改写成一条正向锁，**不要为了让这里变绿而改断言方向**。
    """
    for name, code in _BYPASSES:
        violations = _remove_item_violations(code)
        assert not violations, (
            name + " 今天已经被白名单抓住了（" + repr(violations) + "）。"
            "说明判据升级了——请把这条从 _BYPASSES 删掉，写成正向锁。")


def test_the_bypass_count_is_pinned():
    """条数钉 4：手抄。删掉一条而不改这个数，说明有人悄悄缩小了登记面。"""
    assert len(_BYPASSES) == 4, "登记条数变了：请同步 #302 单据与函数 docstring"
    assert len({name for name, _code in _BYPASSES}) == 4, "绕过名有重复"


def test_this_file_never_executes_the_bypass_strings():
    """本文件只喂纯函数，不执行。断言源码里没有真正去跑它们的调用。"""
    src = Path(__file__).read_text(encoding="utf-8")
    # 禁词本身会出现在这一行的清单里，所以先把自己这一整段（从本函数定义起）剥掉再扫。
    # 不剥的话这条锁永远红——而且红得看起来像"文件真的在执行东西"，是个假警报。
    body = src[src.index("def test_this_file_never_executes_the_bypass_strings"):]
    body = body.split("forbidden in", 1)[0]
    for forbidden in ("Invoke" + "-Expression", "Start" + "-Process", "sub" + "process", "os" + ".system"):
        assert forbidden not in body, (
            "这个文件出现了 " + forbidden + " —— 它是纯文本判据的登记，不许执行任何东西")
    assert "不会被真的执行" in src, "缺少「这些语句不会被执行」的说明，容易被误读"
