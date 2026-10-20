"""#147：数据根锁释放时必须校验持有者。

锁里除 pid 外还记着一个**进程级随机 nonce**。`release_data_root_lock` 读回锁内容，
pid 与 nonce 都对得上才删；对不上就打印一行并原地不动。

## 为什么 pid 不够

进程退出后 pid 会被系统复用，一个后来者完全可能顶着同一个 pid。只比 pid 的释放
路径于是分不清"自己的锁"和"别人正持有的锁"，删掉后者等于亲手制造"两个进程同写
一份数据根"——正是这把锁存在的理由。

## 验收格与对应变异

① 自己是持有者 → 正常删；变异：把比对用的 nonce 换成每次新生成的，① 红。
② 换一个持有者标识（模拟另一个进程）→ 不删且打印；变异：摘掉比对，② 红。
③ pid 相同、nonce 不同（专打 pid 复用）→ 不删且打印；变异同上，③ 红。
④ 文件不存在 → 不抛；变异：去掉 FileNotFoundError 分支，④ 红。
⑤ 内容是垃圾（含非 UTF-8 字节）→ 不抛且不删；变异：解析失败后照删，⑤ 红。
"""
import json
import os

import pytest

from app.core import data_root


def _write_lock(lock, payload):
    lock.write_text(json.dumps(payload), encoding="utf-8")
    return lock.read_text(encoding="utf-8")


def test_lock_payload_carries_pid_and_process_nonce(tmp_path):
    """锁内容必须能被释放路径认人：pid 之外还得有一个非 pid 的标识。"""
    path = data_root.acquire_data_root_lock(str(tmp_path))
    with open(path, encoding="utf-8") as f:
        holder = json.load(f)

    assert holder["pid"] == os.getpid()
    nonce = holder.get("nonce")
    assert isinstance(nonce, str) and nonce, (
        "锁里没有 nonce——只写 pid 的锁挡不住 pid 复用，释放时会把别人的锁当自己的删掉"
    )
    assert nonce != str(holder["pid"]), "nonce 不能退化成 pid 的另一种写法"

    data_root.release_data_root_lock(path)


def test_release_deletes_lock_owned_by_this_process(tmp_path):
    """格①：自己拿的锁，自己放得掉。"""
    lock = tmp_path / data_root.LOCK_NAME
    path = data_root.acquire_data_root_lock(str(tmp_path))
    assert path == str(lock)

    data_root.release_data_root_lock(path)

    assert not lock.exists(), "持有者是本进程，锁应当被删掉"


def test_release_keeps_lock_whose_holder_is_another_process(tmp_path, capsys):
    """格②：另一个进程的锁，不许删，且要留一行说明。

    pid 用的是一个不会被本进程占用的值——这条测的是"认人"，不是 pid 死活判据
    （release 不判死活，只比对身份）。
    """
    lock = tmp_path / data_root.LOCK_NAME
    before = _write_lock(lock, {"pid": 999999, "nonce": "another-process",
                                "service": "other", "started": 0.0})

    data_root.release_data_root_lock(str(lock))

    out = capsys.readouterr().out
    assert lock.exists(), "不是本进程持有的锁，绝不能删"
    assert lock.read_text(encoding="utf-8") == before, "不是自己的锁，一个字都不许改"
    assert "不是本进程持有的锁" in out, "必须留一行说明这把不是我的：%r" % out


def test_release_keeps_lock_when_pid_matches_but_nonce_differs(tmp_path, capsys):
    """格③：pid 相同、nonce 不同（pid 被复用的形状）→ 同样不删。

    负向锁：把释放路径的比对改成只看 pid，这条红——那正是"后来者抢到同一个 pid
    时，旧进程的释放逻辑删掉新持有者的锁"的事故。
    """
    lock = tmp_path / data_root.LOCK_NAME
    before = _write_lock(lock, {"pid": os.getpid(), "nonce": "pid-reuse-other-process",
                                "service": "later", "started": 0.0})

    data_root.release_data_root_lock(str(lock))

    out = capsys.readouterr().out
    assert lock.exists(), "pid 撞上不代表是自己的锁——nonce 不同就不是"
    assert lock.read_text(encoding="utf-8") == before
    assert "不是本进程持有的锁" in out, out


def test_release_tolerates_missing_file(tmp_path):
    """格④：锁已经不在了，放锁不许抛——关闭路径上没有处理异常的上下文。"""
    lock = tmp_path / data_root.LOCK_NAME
    assert not lock.exists()

    data_root.release_data_root_lock(str(lock))   # 不抛即通过

    assert not lock.exists()


@pytest.mark.parametrize("garbage", [
    b"not json at all {{",
    b"\xff\xfe\x00 binary junk that is not utf-8",
])
def test_release_keeps_garbage_lock_without_raising(tmp_path, garbage):
    """格⑤：内容坏掉/读不出来 → 不抛，也**不删**（归属未知，删了可能删到别人的）。

    两份垃圾分别打两条读路径：第一份能按 UTF-8 读出来但 JSON 解析失败，第二份
    连解码都过不去。
    """
    lock = tmp_path / data_root.LOCK_NAME
    lock.write_bytes(garbage)

    data_root.release_data_root_lock(str(lock))

    assert lock.read_bytes() == garbage, "坏内容也不许删：看不出是谁的，就不能替别人作废"
