"""#147：数据根锁释放时必须校验持有者。

锁里除 pid 外还记着一个**进程级随机 nonce**。`release_data_root_lock` 读回锁内容，
pid 与 nonce 都对得上才删；对不上就打印一行并原地不动。

## 为什么 pid 不够

进程退出后 pid 会被系统复用，一个后来者完全可能顶着同一个 pid。只比 pid 的释放
路径于是分不清"自己的锁"和"别人正持有的锁"，删掉后者等于亲手制造"两个进程同写
一份数据根"——正是这把锁存在的理由。

## 验收格与对应变异

① 自己是持有者 → 正常删；变异：把比对用的 nonce 换成每次新生成的，① 红。
② 换一个持有者标识（模拟另一个进程）→ 不删且打印；变异：把**整个**持有者比对摘掉，
   ② 红。只摘 nonce 那一半不够——② 的 pid 本来就不同，它验的是"别的进程"这条粗判据。
③ pid 相同、nonce 不同（专打 pid 复用）→ 不删且打印，日志要带 nonce 前 8 位并写明
   "pid 相同但 nonce 不同"；变异：只摘 nonce 比较（保留 pid 比较），③ 红而 ② 仍绿。
④ 文件不存在 → 不抛；变异：让缺失文件的异常直接冒出去，④ 红。**只删 FileNotFoundError
   分支不够**：它会落进下面 `except (OSError, ValueError)` 的兜底，测试照样绿（#154 复跑
   变异时实测）。
⑤ 内容是垃圾（含非 UTF-8 字节）→ 不抛且不删；变异：解析失败后当成自己的锁照删，⑤ 红。
   只有"能解码但 JSON 坏了"那一份会红；非 UTF-8 那一份在解码时就进了保留分支，属预期。
⑥ service 名 430 字符 → payload 534 B，超过旧 read(512) 窗口 → 整份读到、release 正常删；
   变异：读回 read(512)，⑥ 红（acquire 只报"反复被抢占"，锁被截断 → 当坏内容 → 永久锁）。
⑦ 内容超过 4096 字节读取上限 → 明说"读取上限"且不删；变异：无上限整份读，⑦ 红
   （会当成自己的锁删掉）。
⑧ 写侧超过同一上限 → ValueError，且不留下锁文件；变异：去掉写侧校验，⑧ 红。
⑨ acquire 读到超限锁 → 明确报"读取上限"、拒绝启动、锁一字不动；变异：把超限当 None，
   ⑨ 红（只剩"反复被抢占"，看不出是大小问题）。
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
    assert "another-" in out, "日志要带锁里 nonce 的前 8 位，运维才对得上号：%r" % out


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
    assert "pid 相同但 nonce 不同" in out, (
        "pid 撞上时必须说明真正区分两者的是 nonce，只报 pid 会让运维以为是自己：%r" % out
    )
    assert "pid-reus" in out, "日志要带锁里 nonce 的前 8 位：%r" % out
    assert data_root._PROCESS_NONCE[:8] in out, "也要带本进程 nonce 的前 8 位，便于对照：%r" % out


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


# ---------- #154：整份读，但要有上限 ----------

def test_release_recognises_long_service_name_payload(tmp_path):
    """格⑥：service 名 430 字符 → payload 534 B，超过旧 `read(512)` 的窗口。

    #154：旧实现只读前 512 字节，JSON 被截断，release 判"内容坏了"不删；下一次
    acquire 也读不出 pid、接管不了，锁就成了只能人工删的永久锁。这条让 acquire
    写出真实的长 payload，两条读路径都走一遍。
    """
    service = "s" * 430
    path = data_root.acquire_data_root_lock(str(tmp_path), service=service)
    lock = tmp_path / data_root.LOCK_NAME
    size = len(lock.read_bytes())
    assert size > 512, "payload 只有 %d B，没超过旧 512 B 窗口，这条判据会失去意义" % size

    # acquire 侧必须认得出活着的持有者，且文案里带得出长 service 名（截断就读不到）。
    with pytest.raises(data_root.DataRootBusyError) as exc:
        data_root.acquire_data_root_lock(str(tmp_path), service=service)
    assert service in str(exc.value), (
        "acquire 读锁时被截断了，文案里没有 service 名：%r" % str(exc.value)
    )

    data_root.release_data_root_lock(path)

    assert not lock.exists(), (
        "%d B 的合法锁被截断后当成坏内容，release 没删——它会变成永久锁" % size
    )


def test_release_keeps_over_bound_payload_and_says_why(tmp_path, capsys):
    """格⑦：内容超过 4096 字节读取上限 → 明说"读取上限"，保留不删。

    payload 里放的是**本进程的 pid 与 nonce**：退回"无上限整份读"会把它当成自己的
    锁删掉，这条红；退回"读 4096 字节截断"则会报成"内容不是合法的持有者记录"，
    没有"读取上限"四个字，也红。
    """
    lock = tmp_path / data_root.LOCK_NAME
    lock.write_text(json.dumps({
        "pid": os.getpid(),
        "nonce": data_root._PROCESS_NONCE,
        "service": "x" * 5000,
    }), encoding="utf-8")
    before = lock.read_bytes()
    assert len(before) > data_root._MAX_LOCK_BYTES

    data_root.release_data_root_lock(str(lock))

    out = capsys.readouterr().out
    assert lock.read_bytes() == before, "超限不等于内容坏了，绝不能删"
    assert "读取上限" in out, "超限要明说是大小问题，不能报成'内容坏了'：%r" % out
    assert str(data_root._MAX_LOCK_BYTES) in out, out


def test_acquire_refuses_to_write_payload_over_read_bound(tmp_path):
    """格⑧：读侧有上限，写侧就不能写出超过它的锁——否则写一把自己读不回来的锁。"""
    with pytest.raises(ValueError) as exc:
        data_root.acquire_data_root_lock(str(tmp_path), service="s" * 5000)

    assert "读取上限" in str(exc.value), str(exc.value)
    assert not (tmp_path / data_root.LOCK_NAME).exists(), (
        "写侧拒绝必须发生在创建锁文件之前，不能留下半把锁"
    )


def test_acquire_reports_over_bound_lock_instead_of_generic_retry(tmp_path):
    """格⑨：acquire 读到超限锁 → 直说"读取上限"，拒绝启动，且一个字节都不动。

    旧形状（把超限当成解析失败）：重试 6 次后报"反复被抢占"，运维看不出根因是
    锁文件比正常大了一个数量级。pid 用 -1：若真的按无上限读进来，它会走陈旧锁
    接管并改写文件，这条也会红。
    """
    lock = tmp_path / data_root.LOCK_NAME
    lock.write_text('{"pid": -1, "service": "' + "x" * 5000 + '"}', encoding="utf-8")
    before = lock.read_bytes()

    with pytest.raises(data_root.DataRootBusyError) as exc:
        data_root.acquire_data_root_lock(str(tmp_path))

    msg = str(exc.value)
    assert "读取上限" in msg, "超限要报成大小问题，而不是'反复被抢占'：%r" % msg
    assert lock.read_bytes() == before, "超限时不能删也不能改——宁可拒绝启动"

