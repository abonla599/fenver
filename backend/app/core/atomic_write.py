"""JSON 存储的公共原子落盘：mkstemp → flush + fsync → os.replace。

为什么收成这一个函数：13 个可变数据存储（sessions、feedback、uploads 索引、
providers、usage、schedule、tasks、users、search_keys、search_usage、config 等）
此前各自抄过同一段写法，抄漏一处就是"replace 说成功、盘上却没有"——断电或蓝屏
之后丢的正是最后那次写入，而 watchdog 会把进程原样拉起来，谁都不会发现。fsync 在
replace 之前是这条路径唯一的落盘证明。

## 为什么 tmp 必须用 mkstemp 而不是 `path + ".tmp"`

原写法 `tmp = path + ".tmp"` 是**固定名**，两个进程同时写同一个数据文件时，
两次 `os.open` 拿到的是**同一个 inode**，两个 fd 交错写进去——落盘结果是半截 JSON
拼接。更糟的是这些存储的写入口径是"整份内存快照"（`session_store._flush` 每次写
全量），于是交错写的结果不是丢一条，而是**A 写的第 2 条被 B 的第 3 条整体覆盖，
两边都返回 200**。

同一个仓库里 `apk_cache.py` 用的一直是对的写法（`tempfile.mkstemp(prefix=".part-")`），
只有这里收编 8 处时把旧写法一起收了进来。

`os.replace` 的原子性也只在单进程内成立——它保证的是"rename 这一刻不撕裂"，
不保证"两个进程各自的内存快照会合并"。

## 剩下的那个洞：跨进程的文件锁

mkstemp 修好了"文件撕裂"，但没修"两进程各自持有过期快照再整体覆盖"。那需要
读-改-写整个序列都在同一把跨进程锁里，见 `data_root_lock`。本模块的锁是**单次落盘
粒度**的，它挡不住"两个进程都读到了第 1 条，然后各自加一条写回去"。

真正兜住那个洞的是 `data_root.py::acquire_data_root_lock`：数据根被第二个进程占用
时启动即fail-fast，而不是静默覆盖。见那份文件的说明。
"""
import json
import os
import tempfile


def write_json_atomic(path: str, payload) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # mkstemp 在同目录里造一个**唯一**的临时名：同目录是 os.replace 的前提
    # （跨文件系统 rename 会失败），唯一名是"两个进程不撞同一个 inode"的前提。
    # 前缀带 `.` 让它在同目录的其它文件里不显眼，也避免被任何 `*.json` 通配扫到。
    fd, tmp = tempfile.mkstemp(prefix=".w-", suffix=".tmp", dir=directory or None)
    try:
        # 权限必须在这次 open 里就定下来，而不是"写完再 chmod"：后者的那个瞬间，
        # providers.json 的明文密钥、users.json 的口令摘要正以 0644 躺在同目录下。
        # POSIX：mkstemp 建出来就是 0600，下面这行只是把它钉住（umask 只可能更紧）。
        # Windows：POSIX 权限位不生效，os.chmod 只动只读属性（实测 data/approvals.json
        # 落盘后仍是 0666）。隔离来自数据目录的 NTFS ACL 继承——SYSTEM /
        # Administrators / 属主（pytest 的 mkdtemp 目录里显示为 OWNER RIGHTS），
        # 不含 BUILTIN\Users；不是这行 chmod。若把数据目录放到对 Users 开放的卷上，
        # Windows 上这里挡不住，那是 ACL 的职责。这些 JSON 全是服务端私有数据，
        # 没有任何一个读者在进程外，收紧权限不牺牲任何功能。
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            # replace 原子不等于落盘：flush 到 OS、fsync 到磁盘，之后才 replace。
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # 失败时不留半个临时文件。原先固定名的写法失败后会留下一个 path + ".tmp"
        # 残骸，下一次写入会拿它当自己的目标——那个残骸可能正好是上一次写坏的。
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
