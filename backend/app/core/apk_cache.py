"""最新发布包的磁盘代管：下载不再依赖 GitHub「此刻恰好在线」。

背景（v0.28，用户真机反馈「这两个警告老是弹出」）：`/site/android.apk` 过去
**每一次请求**都替访问者朝 GitHub 实时搬字节——首字节实测 8 秒上下，GitHub 那头
抽风或单飞行槽被占就 302 回发布页，壳把它翻译成人话就是
「更新服务这会儿取不到安装包（GitHub 慢或正忙）」，超时那条则来自 60 秒读超时。
安装包只有一个读者关心的是"最新那一版"，GitHub 只是它的仓库，不是分发通道——
所以服务端取一次、验一次、存一份，之后所有下载流量都从本地磁盘出去。

为什么只代管**带校验值**的字节：磁盘缓存供给的不是"几分钟前取到的字节"，而是
"往后很多天里被端出去的字节"。Release 正文的 `APK-SHA256` 行是构建机上签完包
之后算的（见 `releases.APK_SHA256_RE`），它是"这份文件仍是发布的那一串字节"的
唯一凭据。正文没有这行的发布（不是我们流水线的产物）不进缓存——宁可照旧走
内存接力，也不让无法自证的字节住在磁盘上端给陌生人。

为什么每次读都重算摘要：一次 9 MB 哈希是十几毫秒的量级，比一趟 GitHub 往返便宜
两个数量级；它挡的是掉电截断、有人手改、磁盘位翻转——对一条"把字节流交给别人
手机去安装"的通道，这点偏执是原价。

预取（`prefetch`）：壳每次「检查更新」打到 `/v1/update/info` 时顺手让服务端看一眼
"最新那版在不在盘上"，不在就后台补一趟。用户在手机上看到"发现新版本"的那一刻，
恰恰是这台机器该把包备好的那一刻——点「立即更新」时字节已经在本地等着了。
"""
import hashlib
import os
import re
import tempfile
import threading

from app.core import releases
from app.core.paths import data_root

# 一份发布包 ≈ 9 MB，只留最新那一版：旧版没有任何人会再来要（更新链永远指向
# releases/latest），留着只是白占磁盘。版本号进文件名前必须先过这个形状：
# 它来自对面发布页的 tag_name，磁盘路径不能拼一个没见过的字符串。
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.\-]{0,63}$")

# 预取去重：同一版本只朝 GitHub 补一趟；失败记法是不进表（下次检查更新还会再试），
# 所以这张表只会变长不会误杀。封顶只是防一张跑了几百版的机器攒出无意义的集合。
_WARMED_CAP = 32
_warmed = set()
_warm_lock = threading.Lock()
_warm_busy = threading.Semaphore(1)


def cache_dir() -> str:
    """缓存目录：$APK_CACHE_DIR > <项目根>/data/apk。

    挂在 data/ 那棵树下与其余可变运行时数据同宗：PyInstaller 重建只换
    dist/run_backend/，数据根不跟着蒸发（判据见 core/paths.py 的模块注释）。
    """
    override = os.getenv("APK_CACHE_DIR", "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(data_root(), "data", "apk")


def path_for(version: str) -> str:
    """这一版在盘上的文件；版本形状不认识就是空串（调用方视为"没有缓存"）。"""
    if not _VERSION_RE.match(version or ""):
        return ""
    return os.path.join(cache_dir(), version + ".apk")


def _digest(path: str):
    """流式算文件的 sha256；读不动（被删/截断/权限）返回 (空串, 理由)。"""
    sha = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                sha.update(chunk)
    except OSError as e:
        return "", f"读缓存文件失败：{type(e).__name__}"
    return sha.hexdigest(), ""


def cached_file(version: str, sha256: str) -> str:
    """这一版在盘上且摘要对得上时返回路径；否则 None（顺手清掉对不上的残骸）。

    `sha256` 为空串（那一版发布正文没带校验值）时**永远算没缓存**：不是读不到，
    是这份字节没有自证能力，代管纪律不允许它端出去。
    """
    path = path_for(version)
    if not path or not (sha256 or ""):
        return ""
    if not os.path.isfile(path):
        return ""
    actual, _why = _digest(path)
    if actual != sha256:
        _remove_quietly(path, f"摘要与发布校验值不符（{actual[:8]}…≠{sha256[:8]}…）")
        return ""
    return path


def _remove_quietly(path: str, why: str) -> None:
    """删一个已经不可信（或不再需要）的缓存文件；删不动只打日志。

    为什么要删：留在盘上的是"下一版对不上号也照样躺在那"的垃圾，而它的存在
    没有任何恢复价值——字节的原产在 GitHub，随时能再取。
    """
    try:
        os.remove(path)
    except OSError as e:
        print(f"[apk-cache] 删不掉 {os.path.basename(path)}：{why}（{type(e).__name__}）",
              flush=True)


def store(version: str, data: bytes, sha256: str):
    """把这一版的字节落盘：先验后写、tmp+rename 原子换、清掉旧版。返回 (可以, 理由)。

    原子性是给"边写边有人读"准备的：os.replace 同目录内是原子的，读者要么看到
    完整的旧文件、要么看到完整的新文件，永远不会读到半截包——半截包在这条链路上
    的长相是"校验失败的安装包"，比没有包更糟。
    """
    if not (sha256 or ""):
        return False, "这一版的发布正文没带校验值，不做磁盘代管"
    if hashlib.sha256(data).hexdigest() != sha256:
        return False, "取回的字节与发布校验值不符，拒绝落盘"
    path = path_for(version)
    if not path:
        return False, f"版本号形状不认识，不落盘：{version!r}"
    directory = cache_dir()
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".part-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError:
            _remove_quietly(tmp, "写失败路径上的临时文件")
            raise
        # 只留最新：旧版没人会再来要，攒在盘上是白占 9 MB。
        keep = os.path.basename(path)
        for stale in os.listdir(directory):
            if stale.endswith(".apk") and stale != keep:
                _remove_quietly(os.path.join(directory, stale), "已被新版顶掉")
    except OSError as e:
        return False, f"写缓存文件失败：{type(e).__name__}"
    return True, ""


def prefetch() -> None:
    """后台补一趟"最新那版"的字节；本函数**不等网络**，立即返回。

    由「检查更新」与更新卡片两条端点触发（壳每次问"有没有新版"，正是服务端该
    备货的时刻）。三道闸让它可以被放心地高频调用：
    ① 盘上已有且摘要对得上 → 什么都不做；
    ② 同一版本只补一次（`_warmed`），失败不进表、下次还会再试；
    ③ 全局同时最多一趟（`_warm_busy`），抢不到就放弃——预取是锦上添花，
       永远让位给真有人在等的下载请求。
    没有合格校验值的发布（老版形状）直接放弃：磁盘代管的准入纪律不分"主动取"
    与"被动取"。
    """
    plan, _why = releases.download_plan()
    if not plan or not plan.get("sha256"):
        return
    version = plan["version"]
    if cached_file(version, plan["sha256"]):
        return
    with _warm_lock:
        if version in _warmed:
            return
    if not _warm_busy.acquire(blocking=False):
        return
    threading.Thread(target=_warm, args=(dict(plan),), daemon=True,
                     name=f"apk-prefetch-{version}").start()


def _warm(plan: dict) -> None:
    """预取那一趟：取字节 → 落盘 → 记表。任何一步失败都只打日志，不重试轰炸。"""
    try:
        data, why = releases.fetch_asset(plan["url"])
        if data is None:
            print(f"[apk-cache] 预取失败（下次检查更新会再试）：{why}", flush=True)
            return
        ok, why = store(plan["version"], data, plan["sha256"])
        if not ok:
            print(f"[apk-cache] 预取落盘被拒：{why}", flush=True)
            return
        with _warm_lock:
            _warmed.add(plan["version"])
            while len(_warmed) > _WARMED_CAP:
                # 封顶只是防长跑机器攒垃圾集合，退谁无所谓——被退的顶多再补一趟
                _warmed.discard(next(iter(_warmed)))
        print(f"[apk-cache] 已代管 {plan['name']}（{len(data)} 字节），"
              f"后续下载不再依赖 GitHub 实时可用", flush=True)
    except Exception as e:
        print(f"[apk-cache] 预取线程异常：{type(e).__name__}", flush=True)
    finally:
        _warm_busy.release()


def reset_for_tests() -> None:
    """清掉进程内的预取记账（测试用它等价于"换一台刚起来的机器"）。"""
    with _warm_lock:
        _warmed.clear()
