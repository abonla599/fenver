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

    「这一版要发的是哪个号、是哪一形态」的权威 = docs/releases/<tag>.md 的 scope 行
        <!-- scope: server-only -->   ⇒ 本版只换服务端，安卓包不动（v0.34.1 就是这一形态）
        没有这一行 / 写 installer      ⇒ 有可安装产物的那一版
    <VERSION>  = --expect 传进来的 tag；没有 --expect 时才是 build.gradle 的 versionName
                 （pre_tag_gate.py 第 1 条判据拿来跟 tag 比的就是那个**声明侧**，但它不是
                 "要切哪一版"的权威——只换服务端的一版根本不 bump 它）
    -Marker    必须逐字符等于 "release-marker-v<VERSION>"

    **有安装包那一版**：build.gradle 的 versionName 必须已经等于 <VERSION>（没追平就是
                 versionname-not-bumped，这是它本来就该拦的）；app.js 里这一串所在的行
                 必须**恰好一行**，而且 app.js 里最靠新的那枚标记必须就是这个版本
    **只换服务端那一版**：不许要求 bump versionName（它本来就不动，硬要求等于逼着发版的人
                 去宣布一个装不上的新安卓版本）。判据是一条**世界事实**、不是"开发者手边开着
                 哪个文件"：release-marker-v<VERSION> 此刻**不得出现在线上那份包里**
                 （`dist/run_backend/_internal/app/web/static/app.js` —— 就是 `swap-package.ps1`
                 数装机侧的那一份）。仓库的 app.js 里有没有这一行只说明走到哪一步，两种都**绿**，
                 token 写明是哪一刻：没有 ⇒ `server-only-moment=add-this-line`（把这一行补进去
                 再构建）；有了 ⇒ `server-only-moment=verified-ready-to-build`（暂存≥1 / 装机=0
                 的形状已经成立，可以构建）。已经在**线上**了 ⇒ 无论仓库里有没有都拒绝
                 （`server-only-marker=already-present`）：两侧都数得到同一枚串 = v0.34.1 拿
                 `/changelog` 撞上的空转验收。并且 app.js 里不得有比它更新的标记
                 （`server-only-marker=newest-above`，标记是累积的，v0.34.2 那版就是两行）
                 **线上那份包读不到（新克隆 / CI / `dist/` 被清）⇒ 退 2 fail-closed**，
                 绝不当成"里面没有"——默认成 absent 等于在任何没有 `dist/` 的机器上把这一版事故
                 静默放成绿灯；要指认别的路径用 `--live-appjs <path>`（精确路径覆盖，
                 **不是**一个"跳过这一格"的布尔开关）
    任意两枚标记之间**不得互为子串**：`Select-String -SimpleMatch` 数的是子串命中，
    `release-marker-v0.34.1` 会命中 `release-marker-v0.34.10` 那一行，于是"看起来是两枚
    不同的标记"其实是一枚两边都有的假判据

    docs/releases 里最高的那一版说明不得跑在 <VERSION> 前面，**除非它自己声明的是
    `<!-- scope: server-only -->`**：
    只换服务端的那一版不 bump versionName，所以"说明比声明侧新"正是它的预期形状（#71）——
    否则发版准备把说明落下来的那一刻，裸跑会常年红着，还顺手教人去 bump 一个装不上的版本号。
    读不出这一版的 scope（值不认识 / 读不出文本）就**不豁免**，照旧红

    **裸跑（不带 --expect）时 <VERSION> 与形态的来源**：versionName 只是其中一条线索，还要问
    "这一版正在路上的形状"。下面几条同时成立，target 换成 docs/releases 里最新那份说明的版本，
    判据随之变成「app.js 里最新 marker == 那个领先的版本」（等号右边取自说明，不是取自标记，
    所以一枚对不上说明的领先标记照旧红）：
        · 那份说明的版本号领先 versionName；
        · 那份说明自己声明 `<!-- scope: server-only -->`（值不认识 / 读不出文本 ⇒ 算没声明）；
        · app.js 里最新那枚标记也领先 versionName（还没补行的那一刻它不领先 ⇒ 回到原判据，
          那一刻"该领先的是哪一枚"由 --expect 那一路说）。
    只换服务端的一版 marker 按设计就该领先（新包里有、线上包里绝没有），而它的 versionName
    按规矩不动，所以把裸跑的 target 焊死在 versionName 上 = 把这一版的正确形状判成红，还顺手
    印出上一版那枚已经躺在装机包里的串。
    这一支的**射程**：只读仓库里的 app.js 与那份说明，**不读装机侧那份包**（裸跑要在没有
    dist\\ 的机器上——CI、新克隆、清过盘——也能给结论；常驻锁就跑在那种机器上）。
    "这枚串线上是不是已经有了"只有 `--expect` 那一路判，见上。这一支判不了的：说明不存在、
    说明不领先 versionName、scope 行缺失或值不认识或读不出文本、标记还没补上、装机侧是否已有
    同一枚串、versionName 该不该追平（只换服务端的那一版不追）。

发版准备那次一提交说明、一进版本，这里立刻给结论，并把**该用的那一串**直接打在消息里——
标记于是是抄机器的，不是挑的。说明缺 / 读不到 / scope 值不认识 ⇒ 退 2（fail-closed），
不放行，也**不许**顺手把 versionName 顶上去当替代判据。

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
    # 只换服务端那一版才会读装机侧；仓库不在生产机上时用 --live-appjs 指到那份真在读的 app.js
    python tools/release_marker.py --expect v0.35.0 --live-appjs D:/old/run_backend/_internal/app/web/static/app.js

退出码：0 齐备；1 标记缺失/对不上号/那枚串已经在线上包里（上面那两种事故形状）；2 判据本身
拿不到（fail-closed，与 `pre_tag_gate.py` 同一档：证明不了就不许——**装机侧读不到也算拿不到**，
不放行、也不按"里面没有"处理）。
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
# 装机侧（线上那份包）的 app.js：`deploy/swap-package.ps1` 数 `$liveMarkerLines` 读的就是这一份，
# 本文件把它当同一份事实来读——判据的输入必须和它建模的那道闸的输入一致，否则"绿"只是两套
# 各算各的。仓库目录在这台机器上同时是生产机，所以缺省就在 repo_root 下面；读，**绝不写**。
LIVE_APP_JS_RELPATH = os.path.join(
    "dist", "run_backend", "_internal", "app", "web", "static", "app.js")
GRADLE_RELPATH = os.path.join("android-native", "app", "build.gradle")
RELEASES_RELPATH = os.path.join("docs", "releases")
# 与 tools/gen_changelog.py 的 _SCOPE_RE / SCOPE_SERVER_ONLY / SCOPE_INSTALLABLE 曾是各一份
# （#68 合并）：现在 `gen_changelog` 直接 import 本文件的常量与 `scope_from_text`，合法值只有
# 这一个元组、扫描只有这一份实现，否则"这一版到底是不是只换服务端"会长出第三种真相。
SCOPE_RE = re.compile(r"^\s*<!--\s*scope:\s*([A-Za-z0-9._-]+)\s*-->\s*$")
SECTION_RE = re.compile(r"^##\s+(.*)$")
SCOPE_SERVER_ONLY = "server-only"
SCOPE_INSTALLABLE = "installer"
VALID_SCOPES = (SCOPE_SERVER_ONLY, SCOPE_INSTALLABLE)
SCOPE_SERVER_ONLY_LINE = "<!-- scope: server-only -->"
NOTE_MISSING = "missing"


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


def release_note_path(repo_root, version):
    return os.path.join(repo_root, RELEASES_RELPATH, "v%s.md" % normalize_version(version))


def scope_from_text(text):
    """从一段发版说明里读 scope 行：`(found, value)`，`value` 已 strip().lower()。

    只扫标题之后、第一个 `##` 之前的引言区：`##` 小节里的正文（比如 v0.34.1 那句"本版
    只换服务端"）是给人读的历史叙述，不该被第二次当成机器判据——一份说明有**一个**声明
    位置就够了。两行 scope 时**第一行**说了算，与两把闸原来的读法一致。

    **全仓只有这一份扫描**（#68）：本文件的 `note_scope` 与 `tools/gen_changelog.py` 的
    `_parse_scope` 都吃它，同一份说明不会在"换包闸"和"发版说明页"里读出两个结论。
    这里只负责认出原始值、**不判**它合不合法：调用方拿 `VALID_SCOPES` 判，认不出的值必须
    各自 fail-closed（本文件退 2，生成器抛 ValueError），不许回落成"有安装包"。
    """
    for raw in text.splitlines():
        if SECTION_RE.match(raw):
            break
        match = SCOPE_RE.match(raw)
        if match:
            return True, match.group(1).strip().lower()
    return False, None


def note_scope(repo_root, version):
    """这一版声明的是"只换服务端"吗？权威就是它自己那份发版说明的 scope 行。

    返回 `(server_only, reason)`：reason 为 None 表示读到了结论；否则是
    `NOTE_MISSING`（没有这份说明）/ 一段"读不到"的说明（不是 utf-8、是目录……）。
    扫描是 `scope_from_text`（`tools/gen_changelog.py` 吃同一份实现，#68）：只扫标题之后、
    第一个 `##` 之前的引言区。scope 值不认识 = 报错，不当"没标"处理——静默回落成"有安装包"
    就是把一个装不上的版本号播出去，正是这枚标记要防的事。
    """
    path = release_note_path(repo_root, version)
    if not os.path.isfile(path):
        return False, NOTE_MISSING
    try:
        text = _read_text(path)
    except MarkerSourceError as exc:
        return False, "release-note-unreadable:%s" % exc
    found, value = scope_from_text(text)
    if not found:
        return False, None
    if value not in VALID_SCOPES:
        return False, "release-note-scope-unknown:%s=%s（只认 %s）" % (
            path, value, " / ".join(VALID_SCOPES))
    return value == SCOPE_SERVER_ONLY, None


def collisions(marker_a, marker_b):
    """两枚标记互为子串就是假判据：-SimpleMatch 数不出区别。"""
    if marker_a == marker_b:
        return False
    return marker_a in marker_b or marker_b in marker_a


def live_appjs_path(repo_root, override=None):
    return override if override else os.path.join(repo_root, LIVE_APP_JS_RELPATH)


def read_live_appjs(repo_root, override=None):
    """装机侧那份 app.js ——「这枚串是不是已经在线上」的唯一判得住的事实。

    返回 `(text, path, None)` 或 `(None, path, reason)`。**读不到绝不回落成"里面没有"**：
    默认成 absent 就等于在任何没有 `dist\\` 的机器（新克隆、CI、清过盘）上把两侧都有的空转
    验收静默放成绿灯——那正是本文件要拦的事故，不能由判据自己制造。调用方拿到 reason
    一律退 2（fail-closed）。只读，不写、不建目录、不碰网络。
    """
    path = live_appjs_path(repo_root, override)
    if not os.path.isfile(path):
        return None, path, "live-appjs-missing:%s" % path
    try:
        return _read_text(path), path, None
    except MarkerSourceError as exc:
        return None, path, "live-appjs-unreadable:%s" % exc


def evaluate(repo_root, expect=None, live_appjs=None):
    """返回 (rc, 机读行列表)。rc：0 绿 / 1 形状不对 / 2 判据拿不到。

    `live_appjs` 只换服务端那一版才会用到（装机侧路径覆盖，形状同 `swap-package.ps1` 读的
    那份）；有安装包那一版**不读** `dist\\`——它的 versionName 已经追平，标记天然就在已装的
    那份包里，把"不在装机侧"套到它头上就是把 v0.34.2 这种已上线的形状判成红。
    """
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
    bare_marker_ahead = False
    if expect:
        value = expect.strip()
        if not FULL_TAG_RE.match(value):
            return 1, ["RELEASE-MARKER verdict=FAIL token=expect-must-be-full-tag got=%s" % value]
        target = normalize_version(value)
    else:
        # 裸跑（不带 --expect）：target 与 scope 的权威不再只有 versionName。只换服务端的一版
        # 按规矩不 bump versionName，而它的 -Marker 按设计**就该**领先 versionName（新包里有、
        # 线上包里绝没有），所以只看 versionName 会把这一版在途的形状读成 installer，进而要求
        # app.js 里最新那枚标记停在上一版——恰好是把该领先的那一枚判成红。进到下面这一支要
        # 同时成立，任何一条不成立都回到原有判据、一字未改：
        #   · docs/releases 里最新那份说明的版本号领先 versionName；
        #   · 那份说明自己声明 SCOPE_SERVER_ONLY（值不认识、读不出文本都算**没声明**：读不出就
        #     不豁免，这是"照旧红"，不是"放行"）；
        #   · app.js 里最新那枚标记也领先 versionName。还没补行的那一刻它不领先，那一刻仓库里
        #     没有任何机器可读的东西说清"该领先的是哪一枚"，形态判读归 --expect 那一路。
        # 成立时 target = 那份说明的版本，判据随之变成「app.js 里最新 marker == 那个领先的版本」
        # ——等号右边取自**说明**而不是取自标记，所以一枚领先却对不上说明的标记（领先到别的版本
        # 号）在这里仍然是红的。"恰好一行""不得互为子串"由 installer 那一支原样复用。
        # **射程**：这一支只读仓库里的 app.js 与那份说明，**不读装机侧那份包**——
        # "这枚串线上是不是已经有了"只有带 --expect 的那一路判，裸跑在没有 dist\\ 的机器
        # （CI、新克隆、清过盘）上也必须给得出结论，否则常驻锁在 CI 里就是恒 rc=2。
        # 这一支判不了的：说明不存在、说明不领先 versionName、scope 行缺失或值不认识或读不出
        # 文本、标记还没补上、装机侧是否已有同一枚串、以及"versionName 该不该追平"（本版不追）。
        newest_note = max(notes, key=version_key) if notes else None
        if newest_note is not None and version_key(newest_note) > version_key(declared):
            note_is_server_only, _ = note_scope(repo_root, newest_note)
            newest_marker = max((v for _, v in marker_lines(app_js)), key=version_key, default=None)
            if note_is_server_only and newest_marker is not None \
                    and version_key(newest_marker) > version_key(declared):
                target = newest_note
                bare_marker_ahead = True

    # 形态判读：要切的这一版是不是"只换服务端"，权威是它自己的发版说明，不是 versionName。
    server_only, reason = note_scope(repo_root, target)
    if reason == NOTE_MISSING:
        # 缺说明只在"目标还没被声明侧追平"时才拦：那一刻除了那份说明，仓库里没有任何机器
        # 可读的东西说明这一版是什么形态，顺着操作者的嘴放行 = 把装不上的版本号播出去，
        # 而按旧判据逼他 bump versionName 是同一个错的另一种写法。
        if expect and version_key(target) != version_key(declared):
            return 2, ["RELEASE-MARKER verdict=UNKNOWN token=no-release-note-for-target "
                       "target=v%s declared=v%s —— docs/releases/v%s.md 还不存在，现在没有任何"
                       "机器可读的东西说明 v%s 是「只换服务端」还是「有安装包」，所以既不放行、"
                       "也不许去把 build.gradle 的 versionName 顶上来；先把这一版的发版说明落下来"
                       "（只换服务端的那一版在标题之后、第一个小节之前补一行 %s）"
                       % (target, declared, target, target, SCOPE_SERVER_ONLY_LINE)]
    elif reason:
        return 2, ["RELEASE-MARKER verdict=UNKNOWN reason=%s —— 判据读不到就是不许" % reason]

    if not server_only and version_key(target) != version_key(declared):
        # 有安装包的那一版：这条判据是对的，留着——装不上的包不 bump versionName，能装的必须 bump。
        return 1, ["RELEASE-MARKER verdict=FAIL token=versionname-not-bumped "
                   "expect=v%s declared=v%s —— 本版说明没带 %s，那就是要出安卓包的一版，"
                   "而三件套的声明侧还没改到这个版本：先把 build.gradle 的 versionName bump "
                   "上来再核标记（如果这一版其实只换服务端，那就在 docs/releases/v%s.md 补 scope "
                   "标记，而不是 bump 版本号）"
                   % (target, declared, SCOPE_SERVER_ONLY, target)]

    marker = marker_for(target)
    lines.append("RELEASE-MARKER declared=v%s target=v%s scope=%s marker=%s"
                 % (declared, target,
                    SCOPE_SERVER_ONLY if server_only else SCOPE_INSTALLABLE, marker))
    if bare_marker_ahead:
        # 把"这一支为什么换了 target"写在输出里：裸跑抄一行的人不该自己去猜这一版是不是
        # 只换服务端的那一形态，也不该猜这一枚串是怎么来的。
        lines.append("RELEASE-MARKER token=bare-shape=server-only-marker-ahead note=v%s "
                     "declared=v%s —— 本版按最新那份说明（只换服务端）判：target 取那份说明的版本"
                     "而不是 versionName（这一版按规矩不 bump 它），所以该用的是 %r，它得在 app.js "
                     "里恰好一行、且是最新的那枚；这一格只核仓库里的文本，线上是不是已经有这一串"
                     "要用 --expect v%s 另核" % (newest_note, declared, marker, target))
    problems = []

    found = marker_lines(app_js)
    if server_only and not bare_marker_ahead:
        # 只换服务端的一版不 bump versionName，所以"这一枚串能不能把两侧分开"跟开发者手上
        # 那个文件有没有改过无关（#65 把 app.js 里在不在当判据，于是补了行的那一版和该补行的
        # 那一版**都**红——现网不存在绿色状态，而这一刻恰恰是标记必须存在的时刻）。真正分得开的
        # 只有装机侧那一份事实：它在线上包里 = 两侧都数得到 = 空转验收；不在 = 形状成立。
        repo_lines = count_marker_lines(app_js, marker)
        live_text, live_path, live_reason = read_live_appjs(repo_root, live_appjs)
        if live_reason:
            lines.append("RELEASE-MARKER verdict=UNKNOWN token=live-appjs=unreadable %s —— "
                         "本版说明写的是 %s，判据要读装机侧那份 app.js 才能分清「合法的未来标记」"
                         "和「两边都有的盖章」；读不到**不能**当成「里面没有这一串」——那等于在每台"
                         "没有 dist\\ 的机器（新克隆 / CI / 清过盘）上把这次事故静默放成绿灯。"
                         "把包放回来，或 --live-appjs <path> 指到真在读的那份；本工具没有跳过"
                         "这一格的开关" % (live_reason, SCOPE_SERVER_ONLY))
            return 2, lines
        live_lines = count_marker_lines(live_text, marker)
        if live_lines:
            problems.append("token=server-only-marker=already-present marker=%s live-lines=%d "
                            "repo-lines=%d —— 本版说明写的是 %s（不出新安卓包），而 %r 已经出现在"
                            "**线上那份包**（%s）里：换包预检两侧都数得到它，这正是 v0.34.1 用 "
                            "/changelog 撞上的空转验收。从仓库里删掉这一行不解决问题——线上那份里"
                            "有它，这枚串对本版就不是判据；要么版本号真的往前走一版，要么本版其实"
                            "带了前端改动（那就不是 server-only）"
                            % (marker, live_lines, repo_lines, SCOPE_SERVER_ONLY, marker, live_path))
        above = [v for _, v in found if version_key(v) > version_key(target)]
        if above:
            problems.append("token=server-only-marker=newest-above newest=v%s target=v%s —— "
                            "app.js 里已经有比这一版更新的标记（标记是累积的，v0.34.2 那版就是"
                            "两行）：版本号错位的一刻正是数行数错的一刻"
                            % (max(above, key=version_key), target))
        if not live_lines and not above:
            # 两侧形状已经分得开了，但"补行"和"已补"是发版链上两个不同的时刻，token 必须说清
            # 现在站在哪一刻——否则下一个人还是只能猜（这正是 #65 猜错的地方）。
            if repo_lines:
                lines.append("RELEASE-MARKER verdict=OK token=server-only-moment="
                             "verified-ready-to-build marker=%s repo-lines=%d live-lines=0 —— "
                             "这一行已经在仓库的 app.js 里、线上那份包里没有：暂存侧≥1 / 装机侧=0 "
                             "正是换包闸要的两侧形状，可以构建了（构建完再跑一次不会变红）"
                             % (marker, repo_lines))
            else:
                lines.append("RELEASE-MARKER verdict=OK token=server-only-moment=add-this-line "
                             "marker=%s repo-lines=0 live-lines=0 —— 线上那份包里没有这一串、仓库的 "
                             "%s 里也还没有：把 %r 原样补成一行再构建，补完再跑这里会报 "
                             "verified-ready-to-build" % (marker, APP_JS_RELPATH, marker))
        for existing in sorted({v for _, v in found}, key=version_key):
            if version_key(existing) == version_key(target):
                continue
            if collisions(marker_for(existing), marker):
                problems.append("token=marker=prefix-collision v%s <-> v%s —— "
                                "Select-String 是按子串数行的，这一版该用的那枚和 app.js 里已有的"
                                "那枚互相命中，换包预检分不清新旧两侧，等于挑了一个两边都有的假判据"
                                % (existing, target))
                break
    elif not found:
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
            # #71：这一格吃的是 `:270` 同一把尺子（`note_scope`），不是第二套读法。
            # 只换服务端的一版**本来就不 bump** build.gradle 的 versionName（实测 v0.34.1：
            # build.gradle 停在 0.34.0/60，而 docs/releases/v0.34.1.md 存在并写着只换服务端），
            # 所以发版准备把 docs/releases/v0.35.0.md 落下来的那一刻，裸跑（target=声明侧）看到
            # "说明比声明侧新"就是这一形态的**预期形状**。照老写法它从这里开始常年红——同一版
            # 说明会一直领先，直到换包把版本号追平，而这一版根本不追——而且它印的"先核
            # versionName"正是 #65/#67 禁止的动作：逼着人把一个装不上的安卓版本声明出去。
            # 注意要读的是**最高那一版说明**自己的 scope（`server_only` 那个变量是 `:270` 按
            # target 判的，裸跑时 target 还是上一版，复用它会一点都不松）。读不出结论（scope 值
            # 不认识 / 说明读不出文本）就**不豁免**，照旧红：这条豁免只在真的读到
            # `scope: server-only` 时生效，其余情形一字未改。
            newest_server_only, _ = note_scope(repo_root, newest_note)
            if not newest_server_only:
                problems.append("token=release-note=ahead docs/releases/v%s.md 比目标版本 v%s 还新 —— "
                                "说明文档跑在声明侧前面，先核 versionName（如果这一版其实只换服务端，"
                                "那就在 docs/releases/v%s.md 补一行 %s，而不是 bump 版本号）"
                                % (newest_note, target, newest_note, SCOPE_SERVER_ONLY_LINE))

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
                        help="the tag about to be cut, e.g. v0.35.0; its release note's scope line "
                             "decides whether versionName must have reached it; "
                             "default = build.gradle versionName")
    parser.add_argument("--live-appjs", default=None, dest="live_appjs",
                        help="path of the INSTALLED package's app.js, used only by a "
                             "server-only release to prove the marker is not already live; "
                             "default = <repo-root>\\dist\\run_backend\\_internal\\app\\web\\static\\app.js. "
                             "This is an exact-path override for reading a different package, "
                             "NOT a switch that skips the check: an unreadable live copy fails "
                             "closed with rc=2.")
    args = parser.parse_args(argv)
    rc, lines = evaluate(args.repo_root, args.expect, args.live_appjs)
    for line in lines:
        print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main())
