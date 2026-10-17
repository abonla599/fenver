#!/usr/bin/env python3
"""`-Marker` 由**这一版要发的版本号**推导出来，不再由人在换包窗口里挑。

## 为什么这个文件存在

`deploy/swap-package.ps1` 在停进程之前把标记数**两次**：这一串必须出现在暂存包的
`_internal/app/web/static/app.js` 里，并且在装机包的那份里一次都不许出现（两侧计数闸，
`$stageMarkerLines` / `$liveMarkerLines` 那一段）。问题是**只改后端的一版**
（`backend/app/web/` 一字未动）本来没有任何天然满足这个条件的串。v0.34.2 之所以过得去，
是因为有人在 `app.js` 里手工补了一行 `release-marker-v0.34.2`。

手工挑串就是事故点，它有两种红法，而且**都是窗口打开之后才发现**：

1. 挑了两边都有的串 —— 预检被自己人盖章，换包后的 `ACCEPT` 成了一句空转验收
   （v0.34.1 那一次用的 `/changelog` 就是两边各 2 次）；
2. 挑了新包里根本没有的串 —— 预检当场拒绝，而看门狗已经被停、窗口已经开着。

`ff85ee0f` 补的是**产物**（那一行注释），不是机制：下一个人照样能挑一个两边都有的串。
本文件是机制那一半：标记是**算出来的**，不是选出来的。

## 规则（可机读，并且在构建之前就核得动）

    <VERSION>  = android-native/app/build.gradle 的 versionName
                 （pre_tag_gate.py 第 1 条判据拿来跟 tag 比的就是同一个声明侧）
    -Marker    必须逐字符等于 "release-marker-v<VERSION>"
    app.js 里这一串所在的行必须**恰好一行**
    app.js 里最靠新的那枚标记必须就是这个版本（落后＝空转验收，跑在 bump 前面＝假版本）
    任意两枚标记之间**不得互为子串**：`Select-String -SimpleMatch` 数的是子串命中，
    `release-marker-v0.34.1` 会命中 `release-marker-v0.34.10` 那一行，于是"看起来是两枚
    不同的标记"其实是一枚两边都有的假判据

发版准备那次提交一 bump `versionName`，这里立刻红，并把**该用的那一串**直接打在消息里——
标记于是是抄机器的，不是挑的。

## 为什么落在这里，而不是 pre_tag_gate.py 的第 6 条

`pre_tag_gate.py` 的第 3 条要求**线上 `/health` 的 build 已经等于这个 tag**，也就是说它
只有在上**线之后**才有绿灯；而标记缺不缺必须在上**线之前**就说，否则拦住的时刻正是它
最怕的时刻（窗口中间）。发版顺序里排在"构建 staging"之前的只有 `CI 全绿 → 本地全量复跑`，
所以判据的常驻位置是那两次复跑（锁在 `backend/tests/test_release_marker_derived_from_version.py`），
本文件的 CLI 只是同一份判据的即时可读版：换包窗口里 `python tools/release_marker.py`
抄一行，不用想。

## 用法

    python tools/release_marker.py                    # 自检并打印本版的 -Marker 串
    python tools/release_marker.py --expect v0.35.0   # 构建之前按目标版本核

退出码：0 齐备；1 标记缺失/对不上号（上面那两种事故形状）；2 判据本身拿不到
（fail-closed，与 `pre_tag_gate.py` 同一档：证明不了就不许）。
"""
import argparse
import os
import re
import sys

# 与 tools/pre_tag_gate.py 的 _VERSION_NAME_RE 必须是同一条（漂移由 tests 钉住）：
# 声明侧只有一份，否则第 1 条判据和这把锁会在两个不同的"版本"上各自点头。
VERSION_NAME_RE = re.compile(r'^\s*versionName\s+"([^"]+)"', re.M)
MARKER_RE = re.compile(r'release-marker-v(\d+\.\d+\.\d+)')
RELEASE_NOTE_RE = re.compile(r'^v(\d+\.\d+\.\d+)\.md$')
FULL_TAG_RE = re.compile(r'^v\d+\.\d+\.\d+$')
APP_JS_RELPATH = os.path.join("backend", "app", "web", "static", "app.js")
GRADLE_RELPATH = os.path.join("android-native", "app", "build.gradle")
RELEASES_RELPATH = os.path.join("docs", "releases")


class MarkerSourceError(Exception):
    """判据要读的东西读不到/形状不对——一律 fail-closed，退 2，不许当成"没问题"。"""


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise MarkerSourceError("%s: %s" % (path, exc.__class__.__name__))
    except UnicodeDecodeError as exc:
        raise MarkerSourceError("%s: not-utf8(%s)" % (path, exc.__class__.__name__))


def version_key(text):
    parts = text.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise MarkerSourceError("bad-version:%s" % text)
    return tuple(int(p) for p in parts)


def normalize_version(raw):
    """`0.34.2` 与 `v0.34.2` 是同一个版本；统一成不带 v 的三段号，比较只看数值。"""
    value = (raw or "").strip()
    if value.startswith("v"):
        value = value[1:]
    version_key(value)
    return value


def declared_version(repo_root):
    """发版三件套的声明侧：build.gradle 的 versionName（与 pre_tag_gate 同源）。"""
    path = os.path.join(repo_root, GRADLE_RELPATH)
    if not os.path.isfile(path):
        raise MarkerSourceError("gradle-missing:%s" % path)
    match = VERSION_NAME_RE.search(_read_text(path))
    if not match:
        raise MarkerSourceError("gradle-no-versionName")
    return normalize_version(match.group(1))


def marker_for(version):
    """唯一的合法标记串：它只能被算出来。"""
    return "release-marker-v%s" % normalize_version(version)


def marker_lines(app_js_text):
    """按 Select-String 的口径返回 [(行号, 该行的标记版本)]，一行只算第一次命中。"""
    found = []
    for index, line in enumerate(app_js_text.splitlines(), start=1):
        match = MARKER_RE.search(line)
        if match:
            found.append((index, normalize_version(match.group(1))))
    return found


def count_marker_lines(app_js_text, marker):
    """装机侧/暂存侧那条闸数的是**行数**（子串命中），这里必须用同一把尺子。"""
    return sum(1 for line in app_js_text.splitlines() if marker in line)


def released_note_versions(repo_root):
    """docs/releases/vX.Y.Z.md 里已写过的最高版本（发版说明侧，第 2 条判据的仓库镜像）。"""
    folder = os.path.join(repo_root, RELEASES_RELPATH)
    if not os.path.isdir(folder):
        raise MarkerSourceError("releases-dir-missing:%s" % folder)
    versions = []
    for name in os.listdir(folder):
        match = RELEASE_NOTE_RE.match(name)
        if match:
            versions.append(normalize_version(match.group(1)))
    return versions


def collisions(marker_a, marker_b):
    """两枚标记互为子串就是假判据：-SimpleMatch 数不出区别。"""
    if marker_a == marker_b:
        return False
    return marker_a in marker_b or marker_b in marker_a


def evaluate(repo_root, expect=None):
    """返回 (rc, 机读行列表)。rc：0 绿 / 1 形状不对 / 2 判据拿不到。"""
    lines = []
    try:
        declared = declared_version(repo_root)
        app_js_path = os.path.join(repo_root, APP_JS_RELPATH)
        if not os.path.isfile(app_js_path):
            raise MarkerSourceError("appjs-missing:%s" % app_js_path)
        app_js = _read_text(app_js_path)
        notes = released_note_versions(repo_root)
    except MarkerSourceError as exc:
        return 2, ["RELEASE-MARKER verdict=UNKNOWN reason=%s —— 判据读不到就是不许" % exc]

    target = declared
    if expect:
        value = expect.strip()
        if not FULL_TAG_RE.match(value):
            return 1, ["RELEASE-MARKER verdict=FAIL token=expect-must-be-full-tag got=%s" % value]
        target = normalize_version(value)
        if version_key(target) != version_key(declared):
            return 1, ["RELEASE-MARKER verdict=FAIL token=versionname-not-bumped "
                       "expect=v%s declared=v%s —— 三件套的声明侧还没改到这个版本，"
                       "先把 build.gradle 的 versionName  bump 上来再核标记"
                       % (target, declared)]

    marker = marker_for(target)
    lines.append("RELEASE-MARKER declared=v%s target=v%s marker=%s" % (declared, target, marker))
    problems = []

    found = marker_lines(app_js)
    if not found:
        problems.append("token=marker=absent app.js 里一枚 release-marker-vX.Y.Z 都没有，"
                        "-Marker 就没有可推导的来源；补一行 %r" % marker)
    else:
        highest = max((v for _, v in found), key=version_key)
        if version_key(highest) != version_key(target):
            direction = "落后（拿它当标记就是空转验收）" if version_key(highest) < version_key(target) \
                else "跑在声明版本前面（标记指向一个还没被声明的版本）"
            problems.append("token=marker-newest=mismatch newest=v%s target=v%s —— %s；"
                            "这一版该用的那一串是 %r" % (highest, target, direction, marker))

        hit_lines = count_marker_lines(app_js, marker)
        if hit_lines != 1:
            problems.append("token=marker-lines=count expected=1 got=%d —— -Marker 靠数行判两侧，"
                            "%r 在 app.js 里必须是恰好一行" % (hit_lines, marker))

        for position, (_, earlier) in enumerate(found):
            for _, later in found[position + 1:]:
                if version_key(earlier) == version_key(later):
                    continue
                if collisions(marker_for(earlier), marker_for(later)):
                    problems.append("token=marker=prefix-collision v%s <-> v%s —— "
                                    "Select-String 是按子串数行的，这两枚标记互相命中，"
                                    "换包预检分不清新旧两侧，等于挑了一个两边都有的假判据"
                                    % (earlier, later))
                    break
            else:
                continue
            break

    if notes:
        newest_note = max(notes, key=version_key)
        if version_key(newest_note) > version_key(target):
            problems.append("token=release-note=ahead docs/releases/v%s.md 比目标版本 v%s 还新 —— "
                            "说明文档跑在声明侧前面，先核 versionName" % (newest_note, target))

    if problems:
        lines.extend("RELEASE-MARKER verdict=FAIL " + p for p in problems)
        lines.append("RELEASE-MARKER use-this-marker=%s" % marker)
        return 1, lines
    lines.append("RELEASE-MARKER verdict=PASS —— -Marker 就是这一串，不用挑：%s" % marker)
    return 0, lines


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="derive the swap -Marker from the version being released (never hand-pick it)")
    parser.add_argument("--repo-root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--expect", default=None,
                        help="the tag about to be cut, e.g. v0.35.0; default = build.gradle versionName")
    args = parser.parse_args(argv)
    rc, lines = evaluate(args.repo_root, args.expect)
    for line in lines:
        print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main())
