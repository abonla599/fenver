#!/usr/bin/env python3
"""打 tag 之前的闸门：线上正在被服务的那一版，必须就是准备发版的那一版。

## 为什么这把锁存在

`docs/安装部署指南.md` 第六节写的顺序是

    CI 全绿 → 本地全量复跑 → 构建 staging → 换包上线 → 线上验收 → 打 tag → 发 Release → 播报

**"换包在前、打 tag 在后"不是洁癖**：tag 一推出去，`release-apk.yml` 立刻生成公开
Release 和固定下载链接，用户装的是**那个 tag 号**，而设备上真正跑的东西由线上
`/health` 说了算。两者一旦错位，就会出现"Release 写着 v0.29.0，服务器还在跑
v0.28.1"——事后谁也说不清用户装的是哪一版，而这恰好是最需要一条可信记录的时刻。

**这条顺序连着被违反两次**（v0.29.0、v0.33.0，两次的换包执行记录里都写了同一段
"流程没有机制"的检讨）。提醒不是机制：提醒依赖"当时那个人记得"，而人被催着发版时
最先跳过的就是最后一步验收。所以把判据写成脚本，让 CI 在**生成 Release 之前**跑它
一次——跳不过去的那种。

## 判据为什么取 `/health` 的 build 字段

冻结包里的版本戳由构建时写入（`_internal/version.txt`），`/health` 把它原样报出来。
也就是说这个值来自**正在应答的那个进程自己的包**，不是来自 git、不是来自人的转述。
这正是错位要防的那个位置：仓库里的声明和机器上的现实，只有后者能证明"用户现在
访问到的就是这一版"。

## fail-closed

线上取不到、返回的不是 JSON、没有 build 字段——一律按**不放行**处理。
"网络抖了一下"和"服务器跑的是上一版"在证据上是同一件事：你无法证明线上是这一版，
那就别打 tag。确实要在离线环境放行，必须显式带 `--allow-unreachable`，脚本会把
这件事打印成一条 WARNING 留痕，而不是安静地放行。

## 用法

本机（打 tag 之前，推荐连本地 8000 一起核）：

    python tools/pre_tag_gate.py --expect v0.33.1 --check-local

CI（`release-apk.yml` 在签名与构建之前跑，红得越早越省钱）：

    python tools/pre_tag_gate.py --expect "$GITHUB_REF_NAME" --no-versionname-check

退出码：0 放行；1 版本不符（含 versionName 与 tag 不符）；2 判据拿不到（fail-closed）。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_HEALTH_URL = "https://ai.fenever.xyz/health"
DEFAULT_LOCAL_HEALTH_URL = "http://127.0.0.1:8000/health"

# build.gradle 里那一行的形状由发版三件套自己钉死（见版本锁测试），这里只做宽松
# 抓取：抓不到时报错而不是猜一个，"读不到"在这把锁里永远等于不放行。
_VERSION_NAME_RE = re.compile(r'^\s*versionName\s+"([^"]+)"', re.M)


def _fail(msg: str, code: int = 1) -> "int":
    print("PRETAG verdict=BLOCK reason=%s" % msg)
    return code


def fetch_build(url: str, timeout: float):
    """取回这一台机器自报的 build 戳。返回 (build 或 None, 说明).

    把"取不到"的**原因**带出来是刻意的：闸门红的时候，运维要知道红在网络上还是红在
    版本上——这两种红的处置完全不同（前者等一等重跑，后者说明根本没换包）。
    """
    req = urllib.request.Request(url, headers={"User-Agent": "fenver-pre-tag-gate/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return None, "http-%s" % exc.code
    except (urllib.error.URLError, OSError) as exc:
        return None, "unreachable:%s" % type(exc).__name__
    if status != 200:
        return None, "http-%s" % status
    try:
        data = json.loads(body)
    except ValueError:
        return None, "not-json"
    if not isinstance(data, dict):
        return None, "json-not-object"
    build = data.get("build")
    if not build:
        # v0.33 起 /health 不再回显 provider 标签与模型名（那是信息泄漏面），
        # 但 build 字段是**故意保留**的：它就是"线上跑的是哪一版"的唯一自证。
        # 哪天有人把它也删了，这把锁必须当场红，而不是安静地退化成"200 就是好的"。
        return None, "no-build-field"
    return str(build).strip(), "ok"


def declared_version_name(repo_root: str):
    """从 android-native/app/build.gradle 读 versionName（发版三件套的声明侧）。"""
    path = os.path.join(repo_root, "android-native", "app", "build.gradle")
    if not os.path.isfile(path):
        return None, "gradle-missing"
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return None, "gradle-unreadable:%s" % exc.__class__.__name__
    m = _VERSION_NAME_RE.search(text)
    if not m:
        return None, "gradle-no-versionName"
    return m.group(1).strip(), "ok"


def release_note_exists(repo_root: str, tag: str) -> bool:
    """`docs/releases/<tag>.md` 必须在——发版说明与 tag 一起走，事后靠它对账。"""
    return os.path.isfile(os.path.join(repo_root, "docs", "releases", "%s.md" % tag))


def git_branch(repo_root: str) -> str:
    try:
        out = subprocess.run(["git", "-C", repo_root, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=20)
    except Exception:
        return "unknown"
    return (out.stdout or "").strip() or "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="block tagging unless the LIVE build is the target version")
    ap.add_argument("--expect", required=True,
                    help="the tag about to be pushed, exactly as it will appear, e.g. v0.33.1")
    ap.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    ap.add_argument("--check-local", action="store_true",
                    help="also assert the machine doing the swap reports the same build "
                         "(本机 127.0.0.1:8000)：证明换上去的就是这一版，而不只是隧道对面有东西应答")
    ap.add_argument("--local-health-url", default=DEFAULT_LOCAL_HEALTH_URL)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--no-versionname-check", action="store_true",
                    help="skip the build.gradle comparison (CI already has a dedicated step; "
                         "use this only when the checkout does not carry android-native/)")
    ap.add_argument("--allow-unreachable", action="store_true",
                    help="downgrade 'cannot read the live verdict' to a WARNING. Off by "
                         "default and should stay off: not-proven means do-not-tag.")
    args = ap.parse_args(argv)

    tag = args.expect.strip()
    if not re.match(r"^v\d+\.\d+\.\d+$", tag):
        return _fail("expect-must-be-a-full-tag-like-vX.Y.Z (got %s)" % tag)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print("PRETAG expect=%s branch=%s health=%s" % (tag, git_branch(root), args.health_url))

    # 1) 声明侧：仓库里写的 versionName 必须等于这个 tag。
    if not args.no_versionname_check:
        declared, why = declared_version_name(root)
        if declared is None:
            print("PRETAG versionname=unknown (%s)" % why)
            if not args.allow_unreachable:
                return _fail("versionname-unreadable: %s" % why, 2)
        elif declared != tag.lstrip("v"):
            return _fail("versionname-mismatch declared=%s tag=%s —— 三件套还没改，"
                         "这个 tag 会发出一个和 APK 里不一致的 Release" % (declared, tag))
        else:
            print("PRETAG versionname=ok declared=%s" % declared)

    # 2) 发版说明必须已经在树里
    if release_note_exists(root, tag):
        print("PRETAG release-note=ok docs/releases/%s.md" % tag)
    else:
        print("PRETAG release-note=MISSING docs/releases/%s.md" % tag)
        if not args.allow_unreachable:
            return _fail("release-note-missing: %s" % tag)

    # 3) 现实侧：**线上**自报的 build 必须就是这个 tag
    live, why = fetch_build(args.health_url, args.timeout)
    if live is None:
        line = "PRETAG live-build=unreachable (%s)" % why
        if args.allow_unreachable:
            print(line + " -> WARNING, operator chose to proceed")
        else:
            print(line)
            return _fail("live-build-unproven: %s —— 证明不了线上是这一版就不许打 tag" % why, 2)
    elif live != tag:
        return _fail("live-build-mismatch online=%s expected=%s —— 换包还没做（或做的不是这一版）。"
                     "顺序是换包上线 + 线上验收在打 tag 之前，不是之后" % (live, tag))
    else:
        print("PRETAG live-build=ok %s" % live)

    # 4) 可选：出包那台机器自己也报同一版
    if args.check_local:
        local, lwhy = fetch_build(args.local_health_url, args.timeout)
        if local is None:
            print("PRETAG local-build=unreachable (%s)" % lwhy)
            if not args.allow_unreachable:
                return _fail("local-build-unproven: %s" % lwhy, 2)
        elif local != tag:
            return _fail("local-build-mismatch local=%s expected=%s —— 这台机器上的包不是要发的那版"
                         % (local, tag))
        else:
            print("PRETAG local-build=ok %s" % local)

    print("PRETAG verdict=PASS —— 线上（与本机）正在服务的就是 %s，可以打 tag" % tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
