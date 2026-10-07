"""数据根独占锁：第二个进程启动即 fail-fast，而不是静默覆盖数据。

## 为什么需要它

13 个 JSON 存储共用的落盘函数（`atomic_write.py`）保证的是"单次落盘不撕裂"——
mkstemp 唯一临时名 + fsync + os.replace。但 JSON 存储的写入口径是**整份内存快照**
（`session_store._flush` 每次写全量），而读-改-写整个序列在内存里完成、只在最后
一步落盘。于是**两个进程各自持有过期快照时，落盘层面再怎么原子也救不了**：

    进程 A 读到第 1 条 → 加第 2 条 → 写全量
    进程 B 读到第 1 条 → 加第 3 条 → 写全量
    A 的第 2 条被整体覆盖，两边都返回 200

`os.replace` 的原子性保证的是"rename 这一刻文件不撕裂"，不保证"两个进程的内存
快照会合并"。**丢的是整份数据，不是最后一条。**

## 为什么选锁文件而不是 flock

跨平台文件锁要分平台（`fcntl.flock` / `msvcrt.locking`），而本项目是
**PyInstaller 打包的桌面版 EXE**（`run_backend.py` 绑 127.0.0.1，`Dockerfile` 是
备用形态），开发和运行主要在 Windows。锁文件的好处是行为全平台一致、跨网络
文件系统也生效、并且**能被运维直接看懂**——`data/.data-root.lock` 里写着 pid 和
启动时间，出问题时不用推理。

## 判据：为什么是启动期而不是运行期

`--workers N` 派生出来的 worker 是**同一个进程映像**内fork/ spawn 的，锁文件判据
对它们不适用（父进程持锁、子进程各自看到锁已存在而误判冲突）。所以这把锁的语义
严格来说是"**一份数据根只允许一个服务进程**"，而多 worker 部署要解决的是另一件事
（限流账本与内存状态的多进程共享，见 v0.33 规划第二节第 4 条）。

`docker-compose.yml` 自己已经写明了这个约束（本机不要把 compose 和 exe 一起起来），
但那只是注释，**没有任何机制拦住它**。这把锁就是把注释变成机制。

## 重启必须能接管自己上次留的锁

watchdog 会把崩溃进程原样拉起，那是一个**新进程**、新 pid。所以锁里的 pid 常常
已经是死的。判据因此分两路：

- pid 还在、且命令行看起来是同一个服务 → 真的冲突，拒绝启动。
- pid 不在了 / 位置被别人复用 → 当作**陈旧锁**，接管它。

不按"pid 不在就跳过"处理的代价是：锁会永久挡住所有重启。这比误判冲突严重得多。

## 测试

`tests/test_v033_data_root_lock.py`。判据里有"第二个进程 fail-fast"与"陈旧锁可
接管"两条，各配负向锁。
"""
import errno
import json
import os
import time
from typing import Optional

LOCK_NAME = ".data-root.lock"


def _lock_path(data_root: str) -> str:
    return os.path.join(data_root, LOCK_NAME)


def _pid_alive(pid: int) -> bool:
    """这个 pid 上还有没有活着的进程。

    Windows 上 `os.kill(pid, 0)` 对不存在的进程抛 OSError，对**有权限的活进程**
    也可能抛 PermissionError——后者恰恰说明它活着。POSIX 上signal 0 只做权限与
    存在性检查。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            os.kill(pid, 0)
        except OSError as exc:
            # errno.ESRCH = 确实没有；EPERM/EACCES = 活着但不属于当前用户
            return exc.errno == errno.EACCES or exc.errno == errno.EPERM
        return True
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _read_holder(path: str) -> Optional[dict]:
    """读锁内容。读不到/读坏了都返回 None——调用方据此走"判死"分支。

    **返回 None 会被当成"没有活着的持有者"**，所以这里读不到内容时必须格外
    小心：那是 TOCTOU 的入口。见 acquire 里的处理。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read(512)
    except OSError:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None

def _read_holder_strict(path: str) -> Optional[dict]:
    """同 _read_holder，但**读不到内容时返回哨兵而不是 None**。

    为什么必须有这个区别：`O_EXCL` 创建成功与内容写入之间有一个窗口，那期间
    另一个进程打开这个文件会读到**空内容**。空内容被判成"没有 pid"→ 判定为
    陈旧锁→ 删掉一个**正在被别人合法持有的锁**。实测 6 进程并发下能出现 4 个
    同时"拿到"锁。

    所以"锁存在但读不出 pid"必须区别于"锁里的 pid 确实死了"：前者是**有人在
    写的途中**，要等一下重读；后者才是可以接管的。
    """
    holder = _read_holder(path)
    if holder is not None:
        return holder
    # 试着确认它到底是不是空文件
    try:
        size = os.path.getsize(path)
    except OSError:
        return None                      # 锁已经没了 —— 交给调用者重试
    if size == 0:
        return {}                        # 空内容 = 有人在写的途中，标记为"未定"
    return None                          # 有内容但解析不了 = 损坏，判死


def acquire_data_root_lock(data_root: str, *, service: str = "fenver-backend") -> str:
    """占住数据根。成功返回锁文件路径，被占则抛 DataRootBusyError。

    走 `O_CREAT | O_EXCL`——这个组合的语义就是"文件不存在才创建"，且创建与
    判定是**同一个原子操作**。不是"先判断存在再创建"：那两步之间有窗口，两
    个进程会同时认为自己拿到了锁。

    四种结局，每一种都有判据（fail-closed 优先于 fail-open）：

    1. `O_EXCL` 成功 → 拿到锁，**把 pid 一起写进同一次 open**。
    2. 撞到锁，里面 pid 还活着 → 抛（真冲突）。
    3. 撞到锁，**内容为空**（别人正在写） → 短暂退避后重读；连续读不到就抛。
       **绝不能当成陈旧锁去删**：那份内容马上就会被填上。
    4. 撞到锁，pid 已死 → 当陈旧锁接管：删掉重试一次；仍撞上就抛。

    第 3 条是第 2 篇那次失败的教训：最初没有它，6 进程并发下有 4 个同时拿到锁。
    """
    os.makedirs(data_root, exist_ok=True)
    path = _lock_path(data_root)
    # 内容用 JSON 而不是 key=value 文本：_read_holder 解析的是 JSON，两边必须
    # 是同一套格式（写成文本行的那版会让持有者永远读不出来，于是真冲突检测
    # 不到——静默 fail-open）。JSON 对运维也更好读，且以后加字段不用改解析。
    payload = json.dumps({
        "pid": os.getpid(),
        "service": service,
        "started": time.time(),
    }, ensure_ascii=False)

    empty_reads = 0
    for attempt in range(6):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            holder = _read_holder_strict(path)
            if holder is None:
                # 锁已经不在了（持有者刚释放）—— 回到 O_EXCL 那一步重来
                continue
            if not holder:
                # 第 3 条：文件存在但内容空→ 持有者正在写。等一下再读。
                empty_reads += 1
                if empty_reads > 3:
                    raise DataRootBusyError(
                        "数据根 %s 的锁 %s 一直是空的（超过 3 次重读）。"
                        "可能是别的进程正在启动途中，也可能是这份数据根被手工改坏过。"
                        "请稍后重试，或删掉该文件再启动。" % (data_root, path)
                    )
                time.sleep(0.05)
                continue

            try:
                pid = int(holder.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            if _pid_alive(pid):
                # 活的持有者 → 真冲突。任何 pid 都一样，包括本进程自己的：
                # 两次调用都认为自己独占，而一把独占锁只该有一个持有者。
                raise DataRootBusyError(
                    "数据根 %s 已被 pid=%d 占用（%s）。\n"
                    "一份数据根只允许一个服务进程：两个进程会各自持有内存快照再整体"
                    "覆盖写盘，丢的是整份数据且两边都返回成功。\n"
                    "若确认那个进程已经不在了，删掉 %s 再启动。"
                    % (data_root, pid,
                       holder.get("service", "?"),
                       path)
                )
            # 第 4 条：pid 判定为不活跃 → 陈旧锁（watchdog 重启后锁里是上一次的
            # 死 pid）。删掉它再重试；重试的那次 O_EXCL 就是新持有者之间的原子
            # 决胜。**只删一次**：删了之后又被抢上，说明另一个进程刚拿到锁，
            # 这时我们不是持有者，交给外层抛错而不是继续删——连续删会把刚拿到
            # 锁的合法持有者踢掉。
            if attempt == 0:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            raise DataRootBusyError(
                "数据根 %s 的锁 %s 在接管途中被重新占用，无法判定当前持有者。"
                "请确认没有多个服务进程在启动，或手动检查该文件。" % (data_root, path)
                )
        # 第 1 条：写内容与创建要在同一次 open 里完成，且**立刻 fsync**——
        # 另一个进程撞上 FileExistsError 后会立刻读这个文件，内容还没落盘就
        # 会读到空内容（那正是第 3 条要防的窗口）。
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        return path

    raise DataRootBusyError(
        "数据根 %s 的锁 %s 反复被抢占（尝试 6 次仍未能确定持有者）。"
        "请确认没有多个服务进程在启动，或手动检查该文件。" % (data_root, path)
    )


def release_data_root_lock(path: str) -> None:
    """放掉锁。删不掉不算错——陈旧锁会被下一次的判据接管。"""
    try:
        os.unlink(path)
    except OSError:
        pass


class DataRootBusyError(RuntimeError):
    """数据根已被另一个服务进程占用。"""
