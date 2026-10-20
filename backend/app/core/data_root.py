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
文件系统也生效、并且**能被运维直接看懂**——`<项目根>/.data-root.lock`（`data_root()`
就是项目根，见 `paths.py`）里写着 pid、进程级随机 nonce 和启动时间，出问题时不用推理。
nonce 是为了让释放路径能回答"这把锁是不是本进程留下的"：pid 会被复用，单独一个
pid 分不清"自己"和"抢到同一个 pid 的后来者"。

## 判据：为什么是启动期而不是运行期

`--workers N` 派生出来的 worker 是**同一个进程映像**内fork/ spawn 的，锁文件判据
对它们不适用（父进程持锁、子进程各自看到锁已存在而误判冲突）。所以这把锁的语义
严格来说是"**一份数据根只允许一个服务进程**"，而多 worker 部署要解决的是另一件事
（限流账本与内存状态的多进程共享，见 v0.33 规划第二节第 4 条）。

`docker-compose.yml` 自己已经写明了这个约束（本机不要把 compose 和 exe 一起起来），
但那只是注释，**没有任何机制拦住它**。这把锁就是把注释变成机制。

## 重启必须能接管自己上次留的锁

watchdog 会把崩溃进程原样拉起，那是一个**新进程**、新 pid。所以锁里的 pid 常常
已经是死的。判据因此分三路（**三路，不是两路——这是 v0.33.0 上线后补的**）：

- pid 确认活着 → 真的冲突，拒绝启动。
- pid 确认已死 → 当作**陈旧锁**，接管它。
- **判不出** → 拒绝启动，且**不碰锁文件**。判不出不等于已死。

不按"pid 不在就跳过"处理的代价是：锁会永久挡住所有重启。这比误判冲突严重得多。
但反过来的代价更严重：v0.33.0 就是把"判不出"并进了"已死"，Windows 上对分离进程
恒判不出（见 `classify_pid`），于是每次重启都"成功接管"了一个**活着的**服务的锁，
两个进程同写一份数据根——这把锁要防的事故由这把锁自己制造了。

## 测试

`tests/test_v033_data_root_lock.py`。判据里有"第二个进程 fail-fast"与"陈旧锁可
接管"两条，各配负向锁。释放路径的持有者校验（pid 相同、nonce 不同也不许删）见
`tests/test_v0350_dataroot_lock_release_holder.py`。
"""
import errno
import json
import os
import time
import uuid
from typing import Optional

LOCK_NAME = ".data-root.lock"

# 本进程的持有者标识，进程内生成一次、所有本进程拿到的锁共用。
#
# **为什么 pid 不够**：pid 只是进程在某一刻的编号，进程退出后系统会把它发给下一个
# 进程（Windows 上尤其常见）。只比对 pid 时，"后来者抢到同一个 pid"与"自己"长得
# 一模一样——释放路径就会把别人正持有的锁删掉，两个进程随即同写一份数据根，正是
# 这把锁要防的事故。nonce 随进程出生、随进程消亡，pid 被复用时它不会跟着复用。
_PROCESS_NONCE = uuid.uuid4().hex


def _lock_path(data_root: str) -> str:
    return os.path.join(data_root, LOCK_NAME)


# 进程活死的三种结局。**第三种必须存在**：v0.33.0 在生产上出的事，就是"判不出"
# 被当成了"已死"——判不出的一方于是删掉了**活着的**服务留下的锁，再自己起一份，
# 两个进程同写一份数据根，而这恰恰是这把锁存在的全部理由。
_ALIVE = "alive"
_DEAD = "dead"
_UNKNOWN = "unknown"

# Windows 上 OpenProcess 拿不到句柄时，只有这两个错误码是**确定的"进程不在了"**；
# 其余一律按"判不出"处理，不许据此删别人的锁。
# 本机实测（不存在的 pid，跨 1/2/3/32768/65535/70000/424242/999998/999999 九个取值）
# 一律返回 87，与 MSDN 对 OpenProcess 只列 ERROR_INVALID_PARAMETER 的说法一致；
# 3 一并算"确定没有"是因为它同属"路径找不到"这一类，从来不是权限问题。
# 刚退出还没被回收的 pid 句柄仍开得到（返回 0），那种情况由下面的退出码分支判死，
# 不靠错误码——watchdog 重启接管陈旧锁走的就是这条路。
_ERR_NOT_FOUND = 3
_ERR_ACCESS_DENIED = 5                 # 句柄开不动，但进程确实在
_ERR_INVALID_PARAMETER = 87            # 分离进程被判死的元凶，见 classify_pid 注释


def classify_pid(pid: int) -> str:
    """这个 pid 上是 _ALIVE / _DEAD / _UNKNOWN。**判不出就不猜**。

    ## 为什么不能用 `os.kill(pid, 0)`

    Windows 上 **signal 0 就是 `signal.CTRL_C_EVENT`**，CPython 走的是
    `GenerateConsoleCtrlEvent(0, pid)`：对被 `Start-Process` 起在另一个控制台/
    进程组的进程直接失败，抛 `OSError [WinError 87] 参数错误`（errno EINVAL）。
    v0.33.0 那版判据把"抛异常且不是 EACCES/EPERM"当成"进程没了"，于是
    **看门狗与登录自启拉起的那一份后端——恰好就是生产的形状——永远被判成死进程**。
    而本解释器自己 Popen 出来的子进程反而判得对，所以单测与 Linux CI 一起绿：
    这条盲区只有"用分离进程去测"才会暴露（见 test_v033_data_root_lock.py 里
    `test_detached_holder_is_alive_and_lock_is_respected`，那条在本机用 Start-Process
    起真分离进程；把判据改回 `os.kill` 后只有它红，其余全绿——这就是"旧测试锁不住
    这个形状"的直接证据）。

    ## 正解

    开 `PROCESS_QUERY_LIMITED_INFORMATION` 句柄读退出码，`STILL_ACTIVE(259)` 才算活着。
    句柄开不出来时按错误码分死/活/未知，不再一锅端。

    ## 已知的代价（如实写着，不是隐藏）

    pid 复用：锁里的 pid 恰好被另一个活进程占用时会判成真冲突，第二次启动被拒。
    这是**故意选择的方向**——宁可拒绝启动并让运维删文件，也不能删掉活着的服务的
    独占记录。报错文案里把"怎么人工确认"写清楚，就是为了这种时刻不至于没人会处理。
    """
    if pid <= 0:
        return _DEAD                   # 非法 pid：确定不是任何进程，接管陈旧锁的正常入口
    if os.name == "nt":
        return _classify_pid_windows(pid)
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return _DEAD
        if exc.errno in (errno.EPERM, errno.EACCES):
            return _ALIVE              # 活着，只是不属于当前用户
        return _UNKNOWN
    except Exception:
        return _UNKNOWN
    return _ALIVE


def _classify_pid_windows(pid: int) -> str:
    """Windows 判活：句柄 + 退出码，而不是给目标进程发 Ctrl+C。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.DWORD)]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        code = kernel32.GetLastError()
        if code in (_ERR_NOT_FOUND, _ERR_INVALID_PARAMETER):
            return _DEAD
        if code == _ERR_ACCESS_DENIED:
            return _ALIVE
        return _UNKNOWN
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return _UNKNOWN            # 读不到退出码就不许判死
        return _ALIVE if exit_code.value == STILL_ACTIVE else _DEAD
    finally:
        kernel32.CloseHandle(handle)


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

    五种结局，每一种都有判据（fail-closed 优先于 fail-open）：

    1. `O_EXCL` 成功 → 拿到锁，**把 pid 一起写进同一次 open**。
    2. 撞到锁，里面 pid 还活着 → 抛（真冲突）。
    3. 撞到锁，**内容为空**（别人正在写） → 短暂退避后重读；连续读不到就抛。
       **绝不能当成陈旧锁去删**：那份内容马上就会被填上。
    4. 撞到锁，pid 已死 → 当陈旧锁接管：删掉重试一次；仍撞上就抛。
    5. 撞到锁，**pid 死活判不出** → 抛，且**不碰文件**。第 4 条只允许发生在
       "确定已死"上。

    第 3 条是第 2 篇那次失败的教训：最初没有它，6 进程并发下有 4 个同时拿到锁。
    第 5 条是 v0.33.0 上线后的教训：判据用 `os.kill`，Windows 上对分离进程恒
    判死，于是第 4 条被无条件触发（见 classify_pid）。
    """
    os.makedirs(data_root, exist_ok=True)
    path = _lock_path(data_root)
    # 内容用 JSON 而不是 key=value 文本：_read_holder 解析的是 JSON，两边必须
    # 是同一套格式（写成文本行的那版会让持有者永远读不出来，于是真冲突检测
    # 不到——静默 fail-open）。JSON 对运维也更好读，且以后加字段不用改解析。
    payload = json.dumps({
        "pid": os.getpid(),
        "nonce": _PROCESS_NONCE,
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
            # 三种结局分三路。**关键改动是第三种**：判不出时既不算活也不算死，
            # 而是原地不动地抛错。v0.33.0 把"判不出"并进了"已死"，于是第二个进程
            # 删掉了活着的服务的锁——两个进程同写一份数据根，锁的存在理由被自己
            # 摧毁。宁可启动失败让运维看一眼，也不能赌。
            state = classify_pid(pid)
            if state == _UNKNOWN:
                raise DataRootBusyError(
                    "数据根 %s 的锁 %s 里写着 pid=%d（%s），但本机**无法判定**它是否还活着"
                    "（不是确认已死）。为避免误删活服务的独占记录，本次拒绝启动。\n"
                    "请人工确认：任务管理器或 `Get-Process -Id %d` 看那个进程在不在。\n"
                    "  · 在   → 说明已有服务占用这份数据根，本次启动本就该终止；\n"
                    "  · 不在 → 删掉 %s 再启动即可。"
                    % (data_root, path, pid, holder.get("service", "?"), pid, path)
                )
            if state == _ALIVE:
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
    """放掉**本进程自己拿到的那把**锁；不是自己的就不动。

    释放前读回锁内容，pid 与进程级 nonce 都对得上才删。只对 pid 是不够的：pid
    会被系统复用，一个抢到同一个 pid 的后来者留下的锁与"自己的"在 pid 上无法区分，
    删掉它就是让别人正持有的独占记录消失——两个进程从此同写一份数据根。nonce
    在进程出生时生成，pid 被复用时它不会跟着复用，这才是"这把是不是我的"。

    文件不存在、读不出来、内容坏掉都**不抛**：调用方在 lifespan 的退出路径上，
    那里没有处理异常的上下文，放锁失败也不该把关闭流程弄挂。这几种情况一律
    **不删**——看不出来归属的文件，删了就可能删到别人的。真正的遗留锁会由下一次
    acquire 的陈旧锁判据（pid 确认已死才接管）收拾。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read(512)
    except FileNotFoundError:
        return                         # 已经不在了：放锁是幂等的
    except (OSError, ValueError):
        # 读不出来（含非 UTF-8 的垃圾字节）——归属未知，保留不删。
        print("[数据根锁] %s 读不出内容，无法确认是不是本进程的，保留不删。" % path)
        return

    try:
        holder = json.loads(text)
    except ValueError:
        holder = None
    if not isinstance(holder, dict):
        # 坏内容也照旧不删：删掉它等于替别人做了"这把锁作废"的决定。
        print("[数据根锁] %s 的内容不是合法的持有者记录，保留不删。" % path)
        return

    if holder.get("pid") != os.getpid() or holder.get("nonce") != _PROCESS_NONCE:
        print("[数据根锁] %s 不是本进程持有的锁（锁里 pid=%r，本进程 pid=%d），保留不删。"
              % (path, holder.get("pid"), os.getpid()))
        return

    try:
        os.unlink(path)
    except OSError:
        pass                           # 删不掉不算错：陈旧锁会被下一次的判据接管


class DataRootBusyError(RuntimeError):
    """数据根已被另一个服务进程占用。"""
