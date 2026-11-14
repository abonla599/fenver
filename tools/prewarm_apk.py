r"""把刚发出去的安卓包在 Cloudflare 边缘上先捂热（2026-11-14）。

为什么需要它 —— 实测数据，不是推测
----------------------------------
/site/android.apk 的缓存口径是 public, max-age=0, must-revalidate + 版本钉死的强校验值。
这个口径让**重复**下载很便宜：边缘手里已经有字节，回源只问一句「还是不是这一份」
（304，几百字节）。同一台机器、同一时刻、公网 URL 实测：

    第 1 次   7 922 447 字节   5.73s   1.38 MB/s   cf-cache-status: REVALIDATED
    第 2 次   7 922 447 字节   1.27s   5.31 MB/s   cf-cache-status: REVALIDATED
    第 3 次   7 922 447 字节   1.49s   6.23 MB/s   cf-cache-status: REVALIDATED
    第 4 次   7 922 447 字节   1.38s   5.74 MB/s   cf-cache-status: REVALIDATED

差别不在「每次下载都慢」，而在**某一个版本发布后的第一次**：边缘手上还没有这一版的
字节，必须经 Cloudflare 隧道把 7.9 MB 从这台家用宽带整份拉上去。这一次由第一个下载
的人承担——他正好就是「点发现新版本」的那位。

预热就是把这个代价挪进发版流程，由我们付一次；之后所有人的下载都落在热的那一侧。
因此它**不需要改客户端，也不需要新的 URL 方案**（曾经设想过版本化地址 + immutable 缓存，
按上面这组数字算过：边缘本来就已经在替源站省字节，immutable 只省掉一次几百字节的
重验证往返，对首下那 7.9 MB 一点用都没有，纯属多一层复杂度）。

用法
----
    venv\Scripts\python.exe tools\prewarm_apk.py
    venv\Scripts\python.exe tools\prewarm_apk.py --expect-sha256 <清单里的 sha256>

放在哪一步：data/version.json 与种子清单都签好之后、对外说「可以更新了」之前。
判据是**产出**不是退出码：最后一遍必须又快又命中缓存，否则本脚本非零退出，发版不算完。
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://ai.fenever.xyz/site/android.apk"
# 热的那一侧实测 1.3~1.5s。留一倍余量：家用网络有抖动，卡在 1.5 会天天假红。
DEFAULT_MAX_WARM_SECONDS = 4.0
WARM_CACHE_STATES = {"HIT", "REVALIDATED", "STALE", "UPDATING"}
CHUNK = 64 * 1024


def fetch_once(url, timeout):
    """整份 GET 一次，返回这趟的量测。网络错也当成一次失败的量测，不抛。"""
    started = time.monotonic()
    digest = hashlib.sha256()
    total = 0
    cache_state = ""
    status = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fenver-prewarm/1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            cache_state = (resp.headers.get("cf-cache-status") or "").upper()
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "error": str(exc), "seconds": time.monotonic() - started,
                "bytes": total, "sha256": digest.hexdigest(), "cache": cache_state,
                "status": status}
    return {"ok": True, "error": "", "seconds": time.monotonic() - started,
            "bytes": total, "sha256": digest.hexdigest(), "cache": cache_state,
            "status": status}


def main(argv=None):
    parser = argparse.ArgumentParser(description="给即将对外宣告的 APK 预热 Cloudflare 边缘缓存")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--passes", type=int, default=3,
                        help="整份 GET 的次数：第 1 遍把字节拉上边缘，后面几遍看它能不能被快速供给")
    parser.add_argument("--timeout", type=float, default=180.0, help="单次 GET 的超时（秒）")
    parser.add_argument("--max-warm-seconds", type=float, default=DEFAULT_MAX_WARM_SECONDS,
                        help="最后一遍超过这个秒数就算没热（默认 4.0，实测热态 1.3~1.5）")
    parser.add_argument("--expect-sha256", default="",
                        help="可选：清单里的 sha256，用来确认预热的是要宣告的那一份字节")
    args = parser.parse_args(argv)

    passes = max(3, args.passes)
    print("PREWARM url=" + args.url)
    last = None
    history = []
    for i in range(1, passes + 1):
        got = fetch_once(args.url, args.timeout)
        last = got
        history.append(got)
        if not got["ok"]:
            print("PREWARM pass=%d FAILED reason=%s" % (i, got["error"]))
            return 2
        speed = got["bytes"] / got["seconds"] / 1000000.0 if got["seconds"] else 0.0
        print("PREWARM pass=%d http=%d bytes=%d seconds=%.2f speed=%.1fMB/s cf=%s" % (
            i, got["status"], got["bytes"], got["seconds"], speed, got["cache"] or "-"))

    if args.expect_sha256:
        want = args.expect_sha256.strip().lower()
        if last["sha256"] != want:
            print("PREWARM FAILED 字节对不上：清单 sha256=%s 实际=%s" % (want, last["sha256"]))
            return 3
        print("PREWARM sha256=ok " + want[:16])

    # 判据为什么是「任一遍够快」而不是「最后一遍够快」：实测这条链路会抖。同一分钟里
    # 四次整包是 5.73s / 1.27s / 1.49s / 1.38s，另一次是 1.61s / 5.95s——边缘有时把
    # 整包供给得很快，有时又真的经隧道从源站拉一遍。所以只要**有一遍**证明它能够快速
    # 供给，就说明字节已经在边缘手上、且链路可用；要求"每遍都快"只会天天假红。
    fast = [p for p in history[1:] if p["seconds"] <= args.max_warm_seconds]
    if not fast:
        print("PREWARM NOT-WARM 最后一遍 %.2fs 仍超过 %.2fs —— 边缘这趟还在从源站拉整包。"
              % (last["seconds"], args.max_warm_seconds)
              + "再跑一遍本脚本；连着两三遍都这样，说明隧道/家宽这一路有问题，"
              + "这时候对外宣告「可以更新」就是让第一个用户替我们等。")
        return 1
    if last["cache"] and last["cache"] not in WARM_CACHE_STATES:
        print("PREWARM NOT-WARM cf-cache-status=" + last["cache"] + " —— 边缘没把它当可缓存的响应")
        return 1

    best = min(fast, key=lambda p: p["seconds"])
    print("PREWARM DONE 最快一遍 %.2fs（共 %d 遍）cf=%s —— 现在对外宣告更新，用户点下去就是热的"
          % (best["seconds"], len(history), best["cache"] or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
