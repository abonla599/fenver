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

摘要什么时候算、对不上会怎样：认不出的字节绝不许端给任何人——它挡的是掉电截断、
有人手改、磁盘位翻转，对一条"把字节流交给别人手机去安装"的通道，这点偏执是原价。
但"认不出就删掉"那条纪律赖以成立的前提在 v0.34 不成立了：它原本的理由是"字节的原产
在 GitHub，随时能再取"，而 2026-10 `abonla599` 账号被停用之后，盘上这一份就是**唯一
一份**（不可再生）。所以摘要不符时改成**隔离保留**（改名到 `*.rejected-<时间戳>`，
见 `_quarantine`）——既不销毁唯一样本，也不让它被端出去。
摘要计算本身按 `(路径, mtime, size, ino)` 记账缓存（见 `_digest`）：清单 TTL 从 10 分钟
收到约 30 秒（见 releases.CACHE_SECONDS）之后，同一份字节在一分钟内会被问好几次，
缓存把"重复读盘"与"重复哈希"分开了；字节真被换掉时（store 的原子 rename 会同时换掉
ino 与 mtime）键必然改变，缓存不会把旧摘要借给新字节。

预取（`prefetch`）：壳每次「检查更新」打到 `/v1/update/info` 时顺手让服务端看一眼
"最新那版在不在盘上"，不在就后台补一趟。用户在手机上看到"发现新版本"的那一刻，
恰恰是这台机器该把包备好的那一刻——点「立即更新」时字节已经在本地等着了。
"""
import hashlib
import os
import re
import tempfile
import threading
import time

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

# 摘要缓存：键 (路径, mtime_ns, size, ino)，值 sha256。封顶很小——正常只有"最新那一版"
# 一个条目，多出来的多半是改名/替换留下的旧 inode，退谁无所谓（被退的下次重算一遍）。
_DIGEST_CAP = 16
_DIGEST_CACHE = {}
_DIGEST_LOCK = threading.Lock()


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
    """流式算文件的 sha256；读不动（被删/截断/权限）返回 (空串, 理由)。

    结果按 `(路径, mtime_ns, size, ino)` 缓存。理由：清单 TTL 从 10 分钟收到约 30 秒
    之后，同一份没变过的字节在一分钟里会被问好几次（快照层一次、download_plan 一次、
    预取一次），而那 6.8 MB 的哈希实测约 4.5~5 ms——不缓存就是拿 CPU 换时间。键里带着
    ino 与 mtime，所以任何**真**改动（store 的原子 rename 换掉整个 inode、掉电截断改掉
    size）都必然换一个键；缓存的是"某个 inode 的摘要"，不是"某个路径的摘要"。
    """
    try:
        before = os.stat(path)
    except OSError as e:
        return "", f"读缓存文件失败：{type(e).__name__}"
    key = (path, before.st_mtime_ns, before.st_size, before.st_ino)
    with _DIGEST_LOCK:
        hit = _DIGEST_CACHE.get(key)
    if hit is not None:
        return hit, ""
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
    digest = sha.hexdigest()
    try:
        after = os.stat(path)
    except OSError:                      # 读着读着文件被换了/删了：不落缓存，下次再说
        return digest, ""
    if (after.st_mtime_ns, after.st_size, after.st_ino) == key[1:]:
        with _DIGEST_LOCK:
            _DIGEST_CACHE[key] = digest
            while len(_DIGEST_CACHE) > _DIGEST_CAP:
                _DIGEST_CACHE.pop(next(iter(_DIGEST_CACHE)))
    return digest, ""


def cached_file_state(version: str, sha256: str):
    """这一版的磁盘判定 + 一句理由，返回 (可用路径 | "", 理由)。

    `cached_file` 是它的薄壳（只要路径）。分出这一层是为了让上层（releases）能把
    "为什么没有可用缓存"原样打进日志——P2 那条纪律要求"清单摘要与磁盘字节不符"这件事
    有人喊，而只有这里知道到底是"不符"还是"不在盘上"。

    理由那句话**不许带绝对路径**：它经 `releases._fetch_manifest` 拼进
    `/v1/update/info` 的 502 detail 与 `/v1/release/latest` 的 `reason`，而这两条端点
    在 `authz.PUBLIC_PATHS` 里点了名（检查更新发生在人登录之前）。数据根里同时写着
    这台机器的用户名与目录名，而每次手机检查更新都是任何路人来读这一句。判断要的
    东西（哪个包、清单期望什么、盘上实际是什么）留在对外那句，"到底是盘上哪一个文件"
    走 `_private_where` 那行——server.log 不出公网。
    """
    path = path_for(version)
    if not path:
        return "", f"版本号形状不认识，拼不出磁盘路径：{version!r}"
    if not (sha256 or ""):
        return "", "这一版没有发布校验值，盘上的字节没有自证能力"
    if not os.path.isfile(path):
        _private_where("盘上缺包", version, path, sha256)
        return "", (f"盘上没有这一版的字节（包名 {os.path.basename(path)}、"
                    f"清单校验值 {sha256[:8]}…，位置见服务端日志）")
    actual, why = _digest(path)
    if why:
        _private_where("缓存文件读不动", version, path, sha256)
        return "", f"{why}（包名 {os.path.basename(path)}，位置见服务端日志）"
    if actual != sha256:
        _quarantine(path, f"摘要与清单校验值不符（盘上 {actual[:8]}…≠清单 {sha256[:8]}…）")
        _private_where("摘要与清单校验值不符", version, path, sha256, actual)
        return "", (f"清单摘要与盘上字节不符（包名 {os.path.basename(path)}，"
                    f"盘上 {actual[:8]}…≠清单 {sha256[:8]}…，"
                    "那串字节已隔离保留、未删除；位置见服务端日志）")
    return path, ""


def _private_where(what: str, version: str, path: str, sha256: str = "",
                   actual: str = "") -> None:
    """把"是盘上哪一个文件"补进日志——对外那句只留判断依据，位置只写这里。

    沿用本模块既有的 `print(..., flush=True)` 口径（EXE 是隐藏窗口起的，不打印就只剩
    人猜是哪一层坏了）。这里刻意不走出口脱敏：掩码会把日志里"盘上到底是哪一个文件"
    一起盖掉，而运维查缺包时查的正是那一处。
    """
    bits = [f"{what}", f"版本 {version}", f"盘上位置 {path}"]
    if sha256:
        bits.append(f"清单校验值 {sha256[:8]}…")
    if actual:
        bits.append(f"盘上摘要 {actual[:8]}…")
    print("[apk-cache] 可定位细节（仅日志）：" + "、".join(bits), flush=True)


def cached_file(version: str, sha256: str) -> str:
    """这一版在盘上且摘要对得上时返回路径；否则返回空串（对不上号的字节会被隔离）。

    `sha256` 为空串（那一版发布正文没带校验值）时**永远算没缓存**：不是读不到，
    是这份字节没有自证能力，代管纪律不允许它端出去。
    """
    return cached_file_state(version, sha256)[0]


def _quarantine(path: str, why: str) -> str:
    """把一份不可信的缓存改名到一个不会再被命中的名字下，**保住那串字节**。

    为什么是"改名"而不是"删掉"（v0.34 修订）：这条纪律原本的理由是"字节的原产在
    GitHub，随时能再取"——2026-10 `abonla599` 账号被停用之后这个前提不成立了，盘上
    这一份就是唯一一份（不可再生）。摘要不符时删掉它 = 一次误判（清单里的 `sha256`
    手滑写错、包被替换/截断）即永久丢包，`/site/android.apk` 与 `/v1/update/info`
    双双长期不可用，只能靠人工回填。所以改成**绝不销毁唯一副本**；但纪律的意图一个
    字不改——**认不出的字节绝不许端给任何人**，改名之后它既不是 `path_for` 会命中的
    名字（`cached_file` 只认 `<版本>.apk`），也不以 `.apk` 结尾，所以 `store` 的
    "只留最新一版"那一趟也不会顺手把它清掉。它安静躺在那里等人工处置（改名带时间戳，
    一眼看得出是哪次事件留下的）。

    改名失败（权限/被占用）时**什么都不做**：文件留在原地，下一次 `cached_file` 照样
    因摘要不符而拒服务——宁可每次多算一次摘要，也不冒"没挪成还顺手删了"的风险。
    """
    dest = f"{path}.rejected-{int(time.time())}"
    try:
        os.replace(path, dest)
    except OSError as e:
        print(f"[apk-cache] 摘要不符但隔离失败，字节留在原地（仍不出盘）："
              f"{os.path.basename(path)}（{why}；{type(e).__name__}）", flush=True)
        return path
    print(f"[apk-cache] 摘要不符，已隔离保留、不再端出去：{os.path.basename(dest)}"
          f"（{why}）", flush=True)
    return dest


def _remove_quietly(path: str, why: str) -> None:
    """删一个已经不再需要（被顶掉的旧版 / 写失败留下的临时文件）的缓存文件。

    它只处理**可再生的**东西：临时文件与"已被新版顶掉"的旧版。摘要不符的那一份走
    `_quarantine`（改名保留），绝不走这里——那串字节是不可再生的唯一副本。
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
    """清掉进程内的预取记账与摘要缓存（测试用它等价于"换一台刚起来的机器"）。"""
    with _warm_lock:
        _warmed.clear()
    with _DIGEST_LOCK:
        _DIGEST_CACHE.clear()
