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

## 发版物料顺序（第 5 条判据，#22）

**清单宣告在前、包落盘在后 = 不发版。** v0.34.2 收尾那次出的正是这个形状：自托管
更新通道被指向一个当时在盘上还验不出摘要的版本，宣告只能回滚。手机侧的症状不是
"温柔地留在旧版"，而是「检查更新失败」——`/v1/update/info` 在清单摘要与盘上字节
对不上时整条 502（判据 `releases._fetch_manifest` + `apk_cache.cached_file_state`）。

**运行时是 fail-closed 的，所以案发时运行时一声不吭**：它每次都正确地拒绝端出对不上
号的字节，红的是手机，而发版那一刻没有任何东西变红。缺的那一格是**发版时的顺序闸**，
所以补在这里。它问的是一个机器问题：「这份清单此刻可以宣告这一版吗？」——清单解析与
优先级照抄运行时（`$RELEASE_MANIFEST_PATH` > `data/version.json`，没有才回落打包内
种子；运行时那份存在但读不动/形状不对时**不回落**），包的位置照抄 `apk_cache`
（`$APK_CACHE_DIR` > `data/apk/<版本>.apk`），摘要用同一个 sha256 现算。

纯后端的一版（本版不发新 APK，v0.35.x 就可能是）是合法的，跳过它必须是**操作者显式
带的 `--backend-only`**，闸门绝不自己推断"清单没宣告这一版 = 大概是不发 APK 吧"——
推断就是这次事故的重演入口。选了会打印 `apk-materials=SKIPPED` 留痕。反过来它也**不
授权事故本身**：清单已经把这一版宣告出去、盘上却没有那串字节时，带了这个开关照样红。

## 第 1 条判据的形态豁免（#67）

**只换服务端的一版根本不 bump `build.gradle` 的 versionName**，实测 v0.34.1：
`git show v0.34.1:android-native/app/build.gradle` 是 `versionCode 60 / versionName "0.34.0"`，
而 `docs/releases/v0.34.1.md` 第 3 行写着 `<!-- scope: server-only -->`。旧写法在这里先于
第 2 条判据红（`versionname-mismatch`），唯一的出路是 `--no-versionname-check`，而那面旗的
help 自己写明只给"这份 checkout 里没有 android-native/"用（CI 那一格，`release-apk.yml`）。
把"这一版是什么形态"的判断权交回人的嘴 = #65/#69/#71 在另一把锁上刚修掉的同一个错误，
而同一脚本的第 5 条判据早就接受形态声明（`--backend-only` 的 help 还写着"纯后端版，
v0.35.x 就可能是"）——一把锁两种说法。

现在第 1 条判据**读这一版自己那份发版说明**：
- `scope: server-only` ⇒ 不要求 `versionName == tag`，打印 `versionname=waived-scope` 留痕，
  并且反过来核**逆命题**：`versionName` 已经等于这个 tag 时拒绝（`versionname-bumped-without-package`）——
  只换服务端的一版把 versionName 顶上去，等于对外宣布一个盘上根本不存在的可安装安卓版本，
  违反"只播最新**装得上**的那一版"（#17/#18）；
- 缺说明 / 说明读不动 / scope 值不认识 ⇒ **不豁免**，第 1 条照旧要求 `versionName == tag`，
  与改动前逐字一致（默认成 server-only 就是把这条锁静默放成绿灯）；
- 其余形态一字未改，`--no-versionname-check` 的语义仍是"这份 checkout 没有 android-native/"。

scope 的读法**不在这里重写**：直接吃 `tools/release_marker.py` 的 `note_scope`（同目录、
`!/tools/release_marker.py` 保证 CI checkout 里有；那个文件的路径检查由契约测试钉住），
否则"这一版是不是只换服务端"会长出第三份真相。`tools/gen_changelog.py` 那一处副本归 #68。

## 用法

本机（打 tag 之前，推荐连本地 8000 一起核）：

    python tools/pre_tag_gate.py --expect v0.33.1 --check-local

CI（`release-apk.yml` 在签名与构建之前跑，红得越早越省钱）：

    python tools/pre_tag_gate.py --expect "$GITHUB_REF_NAME" --no-versionname-check

退出码：0 放行；1 版本不符（含 versionName 与 tag 不符、清单宣告跑在了落盘的包前面）；
2 判据拿不到（fail-closed）。
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# ---- 形态判读复用 `tools/release_marker.py` 的那把尺子（#67），不写第三份 scope 正则 ----
# 两个文件同在 `tools/` 下，`!/tools/release_marker.py` 保证它在 CI 那次 checkout 里
# （这条前提由 backend/tests/test_v0350_pre_tag_gate_scope_waiver.py 用 git check-ignore
# 钉住，与 test_v0331_pre_tag_gate_contract.py 对闸门自己做的事同一手法）。
# `release_marker.py` 的 argparse 全在 `main()` 内、模块级只有 `if __name__ == "__main__"`
# 守卫（实测该文件 :419/:441），import 它不会顺手解析本闸门的 argv。
# 按脚本跑时 sys.path[0] 就是 tools/，被测试用 importlib 加载时不是，所以补一句。
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from release_marker import (  # noqa: E402
    MarkerSourceError,
    NOTE_MISSING,
    SCOPE_SERVER_ONLY_LINE,
    note_scope,
)

DEFAULT_HEALTH_URL = "https://ai.fenever.xyz/health"
DEFAULT_LOCAL_HEALTH_URL = "http://127.0.0.1:8000/health"

# build.gradle 里那一行的形状由发版三件套自己钉死（见版本锁测试），这里只做宽松
# 抓取：抓不到时报错而不是猜一个，"读不到"在这把锁里永远等于不放行。
_VERSION_NAME_RE = re.compile(r'^\s*versionName\s+"([^"]+)"', re.M)

# ---- 第 5 条判据用的三格常量：与运行时**同名同义**，不是新写的一套规矩 ----
# 清单落点的环境变量口子（backend/app/core/releases.py:69 MANIFEST_ENV_VAR）。
MANIFEST_ENV_VAR = "RELEASE_MANIFEST_PATH"
# 包在盘上的落点的环境变量口子（backend/app/core/apk_cache.py:66 cache_dir）。
APK_CACHE_ENV_VAR = "APK_CACHE_DIR"
# 清单里那串摘要的形状：与 releases.py:64 `_MANIFEST_SHA_RE` 逐字相同。
# 大小写敏感：需要"再加工一下才像真的"的值不算数。
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# 版本号形状：与 releases.py:66 `_VERSION_SHAPE_RE` 逐字相同。
_VERSION_SHAPE_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")


def normalize_version(text) -> str:
    """`v0.35` 与 `0.35` 是同一个版本：剥掉前导的 v（与 releases.py:436 同一句话）。"""
    value = str(text or "").strip()
    return value[1:] if value[:1] in ("v", "V") else value


def runtime_manifest_path(repo_root: str) -> str:
    """宣告侧那一份：$RELEASE_MANIFEST_PATH > <repo_root>/data/version.json。

    与 `releases.runtime_manifest_path` 同一套优先级。**这一份就是"告诉所有手机有新
    版了"那句公开宣告**，闸门只读它，一个字都不写。
    """
    override = os.getenv(MANIFEST_ENV_VAR, "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(repo_root, "data", "version.json")


def seed_manifest_path(repo_root: str) -> str:
    """打包内种子的落点（`releases.seed_manifest_path` 的非 frozen 那一支）。"""
    return os.path.normpath(os.path.join(repo_root, "backend", "app", "web",
                                         "site", "version.json"))


def package_path_for(repo_root: str, version: str) -> str:
    """这一版在盘上的字节：$APK_CACHE_DIR > <repo_root>/data/apk，文件名 `<版本>.apk`。

    照 `apk_cache.path_for`：版本形状不认识就回空串（调用方按"没有可用的包"处理）。
    """
    if not _VERSION_SHAPE_RE.match(version or ""):
        return ""
    cache = os.getenv(APK_CACHE_ENV_VAR, "").strip() or os.path.join(repo_root, "data", "apk")
    return os.path.join(os.path.abspath(cache), version + ".apk")


def _read_json_object(path: str):
    """读一份清单 JSON；读不动/不是合法 JSON/不是对象 一律回 (None, 理由)。不抛。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(64 * 1024)
    except OSError as e:
        return None, "读不动（%s）" % type(e).__name__
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "不是合法 JSON"
    if not isinstance(data, dict):
        return None, "顶层不是一个对象"
    return data, ""


def load_announcement(repo_root: str):
    """按运行时的优先级取一份**已过形状关**的清单，返回 (清单 | None, 理由, 用了哪一份)。

    优先级与 `releases.load_manifest` 逐字对齐：运行时那份存在就用它；运行时那份存在
    但读不动/形状不对时**不回落种子**——悄悄换回种子等于用一个旧版本冒充"最新"，
    而那正是本条判据要防的方向。都不存在时才算"宣告侧拿不到"。
    """
    runtime = runtime_manifest_path(repo_root)
    if os.path.isfile(runtime):
        data, why = _read_json_object(runtime)
        if data is None:
            return None, "运行时清单读不出来：%s" % why, runtime
        ok, why = _announcement_shape(data)
        if not ok:
            return None, "运行时清单形状不对：%s" % why, runtime
        return data, "", runtime
    seed = seed_manifest_path(repo_root)
    if os.path.isfile(seed):
        data, why = _read_json_object(seed)
        if data is None:
            return None, "种子清单读不出来：%s" % why, seed
        ok, why = _announcement_shape(data)
        if not ok:
            return None, "种子清单形状不对：%s" % why, seed
        return data, "", seed
    return None, "运行时清单与打包内种子都不存在", runtime


def _announcement_shape(data: dict):
    """形状关（照 `releases._manifest_ok` 的必需几格）：version / sha256 / apk_name。

    只取顺序闸要用得上的三格：体积（size）与下载地址（url）由运行时自己那一条把关，
    这里重复一遍只会多一个会走样的第二真相。
    """
    version = normalize_version(data.get("version"))
    if not version or not _VERSION_SHAPE_RE.match(version):
        return False, "version 不是一个版本号：%r" % (data.get("version"),)
    sha = data.get("sha256")
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        return False, "sha256 不是 64 位小写十六进制"
    want_name = "fenver-%s.apk" % version          # 与 releases.asset_names 的新名同形
    if data.get("apk_name") != want_name:
        return False, "apk_name 与 version 对不上：%r（应为 %r）" % (data.get("apk_name"), want_name)
    return True, ""


def sha256_of(path: str) -> str:
    """流式算这串字节的 sha256；读不动回空串（读不动＝验不了＝不放行）。"""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def check_announcement_materials(repo_root: str, expect_version: str):
    """**这份清单此刻可以宣告 `expect_version` 这一版吗？** 返回 (可以, 记号, 说明)。

    顺序是"包先落盘并验过摘要，清单才许宣告这一版"，所以判据按这个方向问：

    1. 宣告侧拿不到（没有清单 / 读不动 / 形状不对）→ `manifest-unreadable`：证明不了
       清单宣告的是哪一版，就不许打 tag（与"线上取不到"同一格 fail-closed）；
    2. 清单宣告的版本 ≠ 本次 tag → `manifest-version-mismatch`：这一版的宣告还没落地
       （要么忘了改清单，要么这本来就是纯后端版）。**后者必须由操作者显式说**
       （`--backend-only`），闸门不推断；
    3. 清单宣告了这一版，但盘上没有那串字节 → `package-missing`；
    4. 盘上有，但现算的摘要与清单写的那串对不上 → `package-digest-mismatch`
       （与 `apk_cache.cached_file_state` 同一格判据，两边都只报前 8 位，不整串外泄）。

    全过才回 (True, "ok", ...)。**这个函数只读不写**：它不改清单、不隔离包、不动盘上
    任何东西——它要验的正是"别人改之前东西齐不齐"。
    """
    want = normalize_version(expect_version)
    data, why, path = load_announcement(repo_root)
    if data is None:
        return False, "manifest-unreadable", "%s（%s）" % (why, path)
    announced = normalize_version(data.get("version"))
    if announced != want:
        return False, "manifest-version-mismatch", (
            "清单 %s 宣告的是 %s，本次 tag 是 %s —— 这一版的宣告还没落地；"
            "本版确实不发新 APK 就显式带 --backend-only" % (path, announced, want))
    sha = str(data.get("sha256") or "")
    apk = package_path_for(repo_root, announced)
    if not apk:
        return False, "package-missing", "版本号形状不认识，拼不出磁盘路径：%r" % (announced,)
    if not os.path.isfile(apk):
        return False, "package-missing", "清单已宣告 %s，但盘上没有那串字节（%s）" % (announced, apk)
    actual = sha256_of(apk)
    if not actual:
        return False, "package-missing", "盘上有那份字节但读不动，摘要验不出来（%s）" % apk
    if actual != sha:
        return False, "package-digest-mismatch", (
            "清单摘要与盘上字节不符（盘上 %s…≠清单 %s…，%s）"
            % (actual[:8], sha[:8], apk))
    return True, "ok", ("清单 %s 宣告 %s，包 %s 在盘上且摘要对得上（sha256 %s…）"
                        % (path, announced, apk, actual[:12]))


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


def note_declares_server_only(repo_root: str, tag: str):
    """**要切的这一版**自己声明的是不是"只换服务端"？返回 (是不是, 读不出的理由)。

    权威是 `docs/releases/<tag>.md` 的 scope 行，读法是 `tools/release_marker.py:note_scope`
    那**一把**尺子（#67：同一句判据在 `gen_changelog.py` 和 `release_marker.py` 里已经有两份
    副本，这里不再抄第三份）。

    fail-closed 的方向必须钉死：**只有真的读到 `scope: server-only` 才回 True**。缺说明
    （`NOTE_MISSING`）、说明读不动、scope 值不认识——一律回 False，也就是"不豁免"，第 1 条
    判据照旧要求 `versionName == tag`，与改动前逐字一致。默认成 server-only 等于把这把发版
    闸门在任何一份坏掉的说明面前静默放成绿灯，而那正是它被写出来要拦的时刻。
    """
    try:
        return note_scope(repo_root, normalize_version(tag))
    except MarkerSourceError as exc:
        # 版本号形状不认识之类：读不出形态 = 不豁免（第 1 条照旧核），不退 2、不放行。
        return False, "release-note-scope-error:%s" % exc


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
    ap.add_argument("--backend-only", action="store_true",
                    help="本版不发新 APK（纯后端版，v0.35.x 就可能是）：显式跳过第 5 条"
                         "发版物料顺序闸中『清单没宣告这一版』那一格。闸门**不会自己推断**"
                         "这件事，只会拒绝；只有带上这个开关才跳过，并且会打印 SKIPPED 留痕。"
                         "清单已经把这一版宣告出去的时候它不作保——那正是要拦的形状。"
                         "它不影响前面四条判据，也不影响 --allow-unreachable 的边界。")
    args = ap.parse_args(argv)

    tag = args.expect.strip()
    if not re.match(r"^v\d+\.\d+\.\d+$", tag):
        return _fail("expect-must-be-a-full-tag-like-vX.Y.Z (got %s)" % tag)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print("PRETAG expect=%s branch=%s health=%s" % (tag, git_branch(root), args.health_url))

    # 1) 声明侧：仓库里写的 versionName 必须等于这个 tag——**除非这一版自己声明只换服务端**。
    #    豁免的判据是"世界事实"（docs/releases/<tag>.md 里那一行），不是操作者手边的旗子：
    #    --no-versionname-check 的语义保持原样（这份 checkout 里没有 android-native/，CI 那一格）。
    if not args.no_versionname_check:
        server_only, scope_reason = note_declares_server_only(root, tag)
        if scope_reason and scope_reason != NOTE_MISSING:
            # 读不出形态就不豁免；打印出来是为了日志能说清"为什么没走 server-only 那一格"，
            # 而不是让人以为判据坏了、再顺手去补一面旗。
            print("PRETAG versionname=scope-unreadable reason=%s —— 不豁免，照旧核 versionName"
                  % scope_reason)
        declared, why = declared_version_name(root)
        if declared is None:
            print("PRETAG versionname=unknown (%s)" % why)
            if not args.allow_unreachable:
                return _fail("versionname-unreadable: %s" % why, 2)
        elif server_only:
            if declared == tag.lstrip("v"):
                # 逆命题：只换服务端的一版把 versionName 顶到这个号 = 对外宣布一个盘上没有的
                # 可安装安卓版本，正是"只播最新装得上的那一版"（#17/#18）禁止的形状。
                return _fail(
                    "versionname-bumped-without-package declared=%s tag=%s —— docs/releases/%s.md "
                    "声明的是 %s（不出新安卓包），而 build.gradle 的 versionName 已经等于这个号："
                    "这一格会把一个装不上的安卓版本播出去。要么把 build.gradle 回退到上一版，"
                    "要么本版其实出了新包（那就把说明的 scope 行改成 installer 并重新核第 5 条）"
                    % (declared, tag, tag, SCOPE_SERVER_ONLY_LINE))
            print("PRETAG versionname=waived-scope note=%s scope=server-only declared=%s —— "
                  "本版说明声明只换服务端，versionName 不动才是预期形状（实测 v0.34.1 停在 0.34.0）；"
                  "这一格**不是** --no-versionname-check，那面旗只表示『这份 checkout 没有 "
                  "android-native/』" % (tag, declared))
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

    # 5) 发版物料顺序（#22）：**包先落盘并验过摘要，清单才许宣告这一版**。
    #    挂在最后一条是为了不改前面四条的顺序与语义；它只读、不写任何东西。
    #    `--backend-only` 只覆盖"清单根本没宣告这一版"那一种状态（纯后端版就是这个状态）；
    #    清单已经把这一版宣告出去了的时候它**不作保**——那正是 v0.34.2 那次的形状，
    #    一个开关能授权事故形状的闸门不是机制。
    ok, token, detail = check_announcement_materials(root, tag.lstrip("v"))
    if args.backend_only:
        if token == "manifest-version-mismatch":
            print("PRETAG apk-materials=SKIPPED (--backend-only) —— 操作者显式声明本版不发新 "
                  "APK，清单宣告的也不是 %s；这是选择，不是闸门推断出来的" % tag)
            ok = True
        elif ok:
            print("PRETAG apk-materials=ok (--backend-only 带上了，但清单宣告的正是这一版，"
                  "所以照样核过) %s" % detail)
        else:
            print("PRETAG apk-materials=REFUSED token=%s %s" % (token, detail))
            return _fail("apk-materials-%s —— --backend-only 挡不住这条：清单 %s 已经把这一版"
                         "宣告出去了，开关只授权『不发新 APK』，不授权『宣告跑在了包前面』"
                         % (token, tag), 1)
    elif not ok:
        # `manifest-unreadable` 走 2（判据拿不到＝fail-closed，与线上取不到同一档），
        # 其余三条都是"已经证明它是反的"＝1（版本不符那一档）。
        code = 2 if token == "manifest-unreadable" else 1
        print("PRETAG apk-materials=REFUSED token=%s %s" % (token, detail))
        return _fail("apk-materials-%s —— 顺序是包先落盘并验过摘要、清单才许宣告这一版；"
                     "本版确实不发新 APK 就显式带 --backend-only" % token, code)
    else:
        print("PRETAG apk-materials=ok %s" % detail)

    print("PRETAG verdict=PASS —— 线上（与本机）正在服务的就是 %s，可以打 tag" % tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
