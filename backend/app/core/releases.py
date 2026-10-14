"""发布信息探针：网页上那张「发现版本更新」卡片与壳「检查更新」的数据来源。

## v0.34：取数源从 GitHub 换成**自托管清单**

2026-10 起 `abonla599` 这个 GitHub 账号被停用：`api.github.com` 对这个仓恒回 404，
手机壳「检查更新」因此恒回 502，官网「安卓版」按钮也拿不到下载地址。于是更新通道
被整条脱开 GitHub，改读一份**我们自己托管**的清单：

* 运行时清单：`<项目根>/data/version.json`（可变、随发版手改，不需要重建/重启服务）；
* 打包内种子：`app/web/site/version.json`（随 EXE 一起打进去，`run_backend.spec` 收了
  `backend/app/web/site`）。**运行时文件存在就用它，否则回落到种子**——两者形状相同。

清单的形状（见 `_manifest_ok`）：`version` / `apk_name`（`fenver-<version>.apk`）/
`sha256`（64 位小写十六进制）/ `size` / `url`（自托管下载地址，域必须是 ai 或 www
的 fenever.xyz）/ `changelog`（更新日志锚点，可空）。

GitHub 那条链没有删（`_API_HOST` / `candidate_repos` / `_fetch_repo` / `fetch_asset`
都还在），但**不再作为默认路径**：解封之后可以再把它接回来，接口形状不用动。

## 为什么出网这件事值得单独收一层

手机直连 GitHub 的链路要过运营商、代理与各家 ROM 的下载器（2026-09-23 那次"迅雷
劫持 → 残包 → 安装失败"的案发通道）。所以问与取都收在这台服务器上：一台机器出网、
全员共享缓存。换成本地清单以后连这一趟出网也没有了。

一条与安全有关的取舍（GitHub 路径仍在时适用）：回来的 `url` 会被点给系统去下载/
安装，所以那条请求走 `core/tls.py` 那一份**系统证书库**的上下文，而不是 `ssl` 的
默认证书包——本机装了带 HTTPS 扫描的杀软时，默认包会验不过（524 那次的同源根因），
而"验不过就退回不验"等于把发布地址交给一个能中间人的人。验不过就报错、就不弹。
"""
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

import httpx

from app.core.paths import data_root
from app.core.tls import system_ssl_context

# 全后端的 GitHub 地址只写在这一处（`api.` 那条由 test_release_probe.py 数着）：
# 一条问"最新是哪一版"，一条是问不到之后的退路——点了按钮的人总得拿到文件。
# v0.24 T1.4：仓库名从 config_store 现读（默认新快照仓 abonla599/fenver）。
# 旧仓名留作**回落**而不是替换：v0.23.x 的老用户装着旧包来查更新时，fenver 仓
# 还没有他们的版本——拉取按"新仓在前、旧仓兜底"排队，改名那天更新链不断。
LEGACY_REPO = "abonla599/ai-assistant"
_API_HOST = "https://api.github.com/repos/"
_PAGE_HOST = "https://github.com/"

# ---------- 自托管清单（v0.34：更新通道的唯一真相源） ----------
# 官网首页与更新日志页所在的那台机器。清单里的下载地址只许落在这两个域上，别的一律
# 判死——它是"这份清单是我们发布的"最粗也最硬的一层判据（域写错了就该报错，而不是
# 把陌生人的手机指到别人家去）。
SELF_HOST_DOMAINS = frozenset({"ai.fenever.xyz", "www.fenever.xyz"})
# 失败回退目标：拿不到包时把人送回**我们自己的官网**（首页有「安卓版」那颗按钮）。
# 2026-10 前这里指 GitHub 发布页，账号被停用后那条链接已失效。
SITE_URL = "https://ai.fenever.xyz/"
# 清单里那串摘要的形状：与发布流水线写在 Release 正文里的那一行、与壳里对账的那一处
# 逐字相同（64 位小写十六进制）。大小写敏感：需要"再加工一下才像真的"的值不算数。
_MANIFEST_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# 清单里的版本号：只认"数字.数字…"（compare() 认的形状），带 v 或不带都行。
_VERSION_SHAPE_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
# 运行时清单的环境变量口子：与 apk_cache 的 APK_CACHE_DIR 同一套"指得走"的纪律，
# 测试与多机部署都靠它把清单挪出生产目录。
MANIFEST_ENV_VAR = "RELEASE_MANIFEST_PATH"


def candidate_repos() -> list:
    """按优先级排好的仓库清单：配置里的在前，旧仓兜底在后。"""
    from app.core import config_store
    repo = config_store.update_repo()
    return [repo] if repo == LEGACY_REPO else [repo, LEGACY_REPO]


def latest_url_for(repo: str) -> str:
    return _API_HOST + repo + "/releases/latest"


def releases_page_for(repo: str) -> str:
    return _PAGE_HOST + repo + "/releases/latest"


def releases_page() -> str:
    """拿不到字节时给用户看的那条退路：我们自己的官网（首页有「安卓版」按钮）。

    2026-10 前这条路指向 GitHub 发布页；账号被停用后那条链接已是 404，于是改指官网。
    与它成对的 `releases_page_for(repo)`（GitHub 那条）留着，解封后可直接接回来。
    """
    return SITE_URL


def runtime_manifest_path() -> str:
    """运行时清单的落点：$RELEASE_MANIFEST_PATH > <项目根>/data/version.json。

    挂在 data/ 那棵树下与其余可变运行时数据同宗（判据见 core/paths.py 的模块注释）：
    PyInstaller 重建只换 dist/run_backend/，数据根不跟着蒸发。**发新版只改这一份**，
    不需要重建 EXE、也不需要重启服务。
    """
    override = os.getenv(MANIFEST_ENV_VAR, "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.join(data_root(), "data", "version.json")


def seed_manifest_path() -> str:
    """打包内种子的落点：随 EXE 一起打进去的那份（`run_backend.spec` 收了 site/）。

    运行时那份不存在时用它兜底：一台刚换包、还没人动过 data/ 的机器，靠种子也能立刻
    答出"最新是哪一版"。
    """
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return os.path.join(base, "app", "web", "site", "version.json")
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "web", "site", "version.json"))


def _read_manifest_file(path: str):
    """读一份清单 JSON；读不动/不是合法 JSON/不是对象 一律回 (None, 理由)。不抛。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(64 * 1024)
    except OSError as e:
        return None, f"读不动（{type(e).__name__}）"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "不是合法 JSON"
    if not isinstance(data, dict):
        return None, "顶层不是一个对象"
    return data, ""


def _manifest_ok(data: dict):
    """形状关：清单里每一格都能被下游原样拿去用，或明确判死。返回 (可以, 理由)。

    这不是宽容解析：清单是"我们自己发布的"唯一宣告，任何一格对不上都宁可报错——
    把一个拼错的版本号或摘要端给手机，表现不是报错，是"检查更新失败"或"装不上"，
    而且每一环都"正常工作"。
    """
    version = normalize_version(data.get("version"))
    if not version or not _VERSION_SHAPE_RE.match(version):
        return False, f"version 不是一个版本号：{data.get('version')!r}"
    want_name = asset_names(version)[0]                 # fenver-<version>.apk
    if data.get("apk_name") != want_name:
        return False, f"apk_name 与 version 对不上：{data.get('apk_name')!r}（应为 {want_name!r}）"
    sha = data.get("sha256")
    if not isinstance(sha, str) or not _MANIFEST_SHA_RE.match(sha):
        return False, "sha256 不是 64 位小写十六进制"
    size = data.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or size > APK_MAX_BYTES:
        return False, f"size 不像一个 APK：{size!r}"
    url = data.get("url")
    if not isinstance(url, str) or not _is_self_hosted_url(url):
        return False, f"url 不是我们自家域名上的 https 地址：{url!r}"
    changelog = data.get("changelog", "")
    if changelog and not isinstance(changelog, str):
        return False, "changelog 若是存在就必须是字符串（更新日志锚点）"
    return True, ""


def _is_self_hosted_url(url) -> bool:
    """这个地址是不是我们自家域名上的 https 出口。"""
    if not isinstance(url, str) or not url:
        return False
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and parts.hostname in SELF_HOST_DOMAINS


def load_manifest():
    """按优先级取一份**已过形状关**的清单，返回 (manifest | None, 理由, 用了哪一份)。

    优先级：运行时文件存在就用它，否则回落到打包内的种子。**运行时文件存在但读不动
    或形状不对时不回落**——那正是"我读不出来"要如实说出来的一格，把它悄悄换回种子
    等于用一个旧版本冒充"最新"，是本仓反复防的那件事。
    """
    runtime = runtime_manifest_path()
    if os.path.isfile(runtime):
        data, why = _read_manifest_file(runtime)
        if data is None:
            return None, f"运行时清单读不出来：{why}", runtime
        ok, why = _manifest_ok(data)
        if not ok:
            return None, f"运行时清单形状不对：{why}", runtime
        return data, "", runtime
    seed = seed_manifest_path()
    if os.path.isfile(seed):
        data, why = _read_manifest_file(seed)
        if data is None:
            return None, f"打包内清单读不出来：{why}", seed
        ok, why = _manifest_ok(data)
        if not ok:
            return None, f"打包内清单形状不对：{why}", seed
        return data, "", seed
    return None, ("运行时与打包内都没有版本清单"
                  "（data/version.json 与 app/web/site/version.json 都不在）"), ""


def _synthesize_release(manifest: dict) -> dict:
    """把一份清单翻成壳认得的**发布 JSON 形状**（壳的判断题一个字都不用改）。

    `/v1/update/info` 的返回形状必须与换源前逐字一致，这是手里那批手机能**零发版**
    恢复更新的唯一原因。壳 `ReleasePlan.decide` / `pickAsset` 真正读的字段就这几个
    （判据见 android-native 那份 ReleasePlan.java）：顶层 `tag_name`、`assets[]` 里每项
    的 `name`/`size`/`browser_download_url`、顶层 `apk_sha256`；`draft`/`prerelease`
    只有为真才拦，所以我们**不放**这两格。
    """
    version = normalize_version(manifest["version"])
    return {
        "tag_name": "v" + version,
        "assets": [{
            "name": manifest["apk_name"],
            "size": int(manifest["size"]),
            "content_type": "application/vnd.android.package-archive",
            "browser_download_url": manifest["url"],
        }],
        "apk_sha256": manifest["sha256"],
    }


def _point_self_hosted_assets(manifest: dict, origin: str) -> int:
    """把清单里那枚自家下载地址换成**这次请求来源**对应的那一份；返回改了几枚。

    壳的那条同源判据比的是"地址主机 == 它自己配置的主机"：清单里写死的域（ai 或 www）
    一旦和这台手机配置的那个不一致，同一个地址会被它自己判成不可信。所以地址跟着请求
    的 Host 走（与 `alias_legacy_asset_to_self_host` 同一招）。来源不合格（本地 http）
    时一个字节都不动——把一个它不信的地址换上去比不改更糟。
    """
    url = self_hosted_apk_url(origin)
    if not url:
        return 0
    changed = 0
    for asset in manifest.get("assets") or []:
        if isinstance(asset, dict) and _is_self_hosted_url(asset.get("browser_download_url")):
            asset["browser_download_url"] = url
            changed += 1
    return changed


def _preferred_repo() -> str:
    """最近一次成功读取来自哪个仓；没读过就用配置首选仓。

    只在 **GitHub 那条链**（`_fetch_github` / `_fetch_repo`）上有意义：自托管清单没有
    "货从哪个仓来"这件事。留着是为了切回 GitHub 时 `releases_page_for` 那条"发布页跟着
    手里这份货的出处走"的承诺不丢。
    """
    with _lock:
        repo = _repo_of_payload or ""
    return repo or candidate_repos()[0]


USER_AGENT = "ai-assistant-release-probe"
TIMEOUT_SECONDS = 5.0
CACHE_SECONDS = 600
MAX_BYTES = 256 * 1024

# v0.28.2「检查更新要转半天」的服务端那一半：缓存过点的头一个人替全网去 GitHub 跑一趟，
# 实测那趟 2.1 秒起步、GitHub 慢时顶到 TIMEOUT_SECONDS×两个候选仓≈10 秒——正好逼近壳里
# Updater 的 12 秒读超时。现在快照超过半个缓存期就**先交手里的货、后台补新货**（单飞），
# 进程起来另有 warmer 先拉一次。收紧的只有"什么时候去拉"；「过期旧快照不许冒充最新」
# （AC-4）由 _payload_at 那条判据原样守着，一条都没放松。
# 限流账：匿名 GitHub 按 IP 60 次/小时，warmer 满打满算 12 次/小时，是承诺的零头。
REFRESH_AHEAD_SECONDS = CACHE_SECONDS // 2

# ---------- 官网那颗「安卓版」按钮要代取的字节 ----------
# 白名单是两条，不是一条，而且这条是量出来的不是记住的：`browser_download_url` 在
# github.com 上，它 302 去的是 **release-assets.githubusercontent.com**（2026-09-22 真跑
# 代取时第一版只放了 objects.*，结果每一次都卡在"目标主机不在白名单里"、静默退回
# 发布页——功能上线即失效）。objects.* 留着是因为别的资产形状确实会走它；
# 每一跳都重新过这个判断（fetch_asset 里 follow_redirects 是关着的），否则
# "第一跳合法、第二跳随你"，那正是这条代理存在的理由所反对的事。
DOWNLOAD_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com",
                            "objects.githubusercontent.com"})
ASSET_TIMEOUT_SECONDS = 20.0
APK_MAX_BYTES = 16 * 1024 * 1024
MAX_HOPS = 3
# 这个形状同时保证它放进 Content-Disposition 是安全的：没有 CR/LF、没有引号、没有分号。
# 前缀与发布流水线真实产出的资产名逐字对齐（判据见
# test_the_asset_name_is_the_one_the_workflow_publishes），漂了的不是报错，
# 是官网按钮与 App 内更新双双静默退回发布页。
#
# v0.24 阶段4 收口（用户点名"APK 产物文件名按新品牌名规则同步，下载端与构建端一致"）：
# 正式名换 `fenver-<版本>.apk`。
#
# v0.24.1（用户决定"迁移期双名资产不发了"）：**一次发布只挂新名这一个资产**。
# 老壳（0.23.x 及更早）仍然不断链，但兜底的手段从"仓库里多发一份字节相同的别名"
# 换成"服务端在给壳的那份 JSON 里现造一枚旧名条目"——见 `alias_legacy_asset_to_self_host`。
# 为什么这一半挪进服务端才是对的：
# * 旧名那枚资产在 GitHub 上从来不是给任何人点的，它唯一的读者是老壳按名字挑资产。
#   把它做成一份公开文件，等于把一条只该出现在机器间接口里的适配层挂到用户看得见的位置，
#   还让人以为一次发布有两个版本的包；
# * 挪进 JSON 以后，退场只是删一段代码，不用再回收一个已经发出去的资产；
# * 读的这一侧照旧两名都认：历史上每一版真实挂过什么名字，就还得能读出来
#   （0.23.x 在旧仓里只有旧名；v0.24.0 那一版两名都发过）。
ASSET_PREFIX = "fenver-"                        # 唯一发出去的那个名字
LEGACY_ASSET_PREFIX = "ai-assistant-native-"    # 老壳唯一认得的名字：只在读与合成里活着
PUBLISH_ASSET_PREFIXES = (ASSET_PREFIX,)        # 发布流水线真正 cp 与 upload 的名字
# 读侧优先级：新名在前。两名并挂的发布（只有 v0.24.0 这么发过）取新名。
ASSET_PREFIXES = (ASSET_PREFIX, LEGACY_ASSET_PREFIX)
_ASSET_NAME_RE = re.compile(r"^(?:fenver|ai-assistant-native)-[0-9][0-9A-Za-z.\-]*\.apk$")


def asset_names(version: str) -> tuple:
    """这一版所有**可读**的资产名，按优先级排：新名在前、旧名兜底。

    顺序就是判据：一份 release 同时挂着两名（只有 v0.24.0 这么发过）时取新名，
    只有旧名时取旧名——后者正是 0.23.x 那批发布的全部形状，它们永远躺在旧仓里。
    这里不是"该发哪几个名字"：发出去的那一个由 `PUBLISH_ASSET_PREFIXES` 决定。
    """
    return tuple(prefix + version + ".apk" for prefix in ASSET_PREFIXES)

_lock = threading.Lock()
_payload = None                 # 上一次**成功**拉到的那份
# 上一次成功那份来自哪个仓（发布页跳转与回落判断要跟着它走，而不是跟着配置走——
# 配置可以被人改，手里这份货的出处不能事后被追认）。None = 这一世还没成功过。
_repo_of_payload = None
# monotonic 读数；None = 这一世还没试过。**这里不能用 0.0 当"该拉了"的哨兵**：
# monotonic 是从开机算的，刚起来的机器上 now 本身就小于 CACHE_SECONDS，
# 于是 `now - 0.0 >= 600` 为假——"该拉一次"被判成"缓存还新"，而 `_payload` 是空的。
# 症状是每次重启后的头 10 分钟里那张卡片永远不弹，且一句错都不报。
# （不是假想：CI 就是这么抓到的，runner 是一台刚开的虚拟机。）
_fetched_at = None
# 最近一次**成功**的读数。和 `_fetched_at`（最近一次**尝试**）分开记，是为了让
# "手里这份货超过 10 分钟了"能独立发问：失败已经把 `_fetched_at` 推到下一轮，
# 若缓存期只认尝试时间，一份超过 TTL 的旧快照会在 GitHub 恢复前被一直当成最新供出去
# ——那正是 AC-4「伪装最新」的样子。
_payload_at = None
# 后台补货的单飞闩：同一时刻最多一条腿在替全网跑 GitHub，
# 补货中的请求一律先拿手里的快照回去——这就是 AC-1 的全部机关。
_refreshing = False
# warmer 只许被 lifespan 起一条（幂等闩；测试进程不走 lifespan，天然不起线程）。
_warmer_started = False


def _numeric(segments):
    """把 "0.16" 拆成 [0, 16]。任何一段不是纯数字就返回 None（调用方判不认识）。"""
    out = []
    for part in segments.split("."):
        part = part.strip()
        if not part.isdigit():
            return None
        out.append(int(part))
    return out


def compare(a: str, b: str) -> int:
    """与壳里 `ReleasePlan.compare` 同一套规则：按点分段比数值，短的那边补 0。

    两份实现跨语言，漂移的判据在 `test_release_probe.py`：那张用例表同时喂给 Java 的
    单测和这里，两边算得不一样就红。
    """
    left, right = _numeric(a), _numeric(b)
    if left is None or right is None:
        raise ValueError(f"版本形状不认识：{a!r} / {b!r}")
    n = max(len(left), len(right))
    for i in range(n):
        l = left[i] if i < len(left) else 0
        r = right[i] if i < len(right) else 0
        if l != r:
            return -1 if l < r else 1
    return 0


def normalize_version(text) -> str:
    """`v0.16` 与 `0.16` 是同一个版本：剥掉前导的 v，两端空白不算。"""
    value = str(text or "").strip()
    return value[1:] if value[:1] in ("v", "V") else value


def _pick_asset(body: dict, version: str):
    """在资产里找这一版那个包：新名优先、旧名兜底，两个都只认**名字精确等于**。

    与壳里 `ReleasePlan.pickAsset` 同一个理由：一次发布可以同时挂着 mapping.txt、
    别的平台的产物或上一次误传的文件，而这里挑中的东西是要弹给人去安装的。
    名字对上版本号顺带钉住了"这个包就是这一版"。
    两趟而不是一趟 `in`：一趟没法表达"两个名字都在时取哪一个"。
    """
    assets = body.get("assets") or []
    for want in asset_names(version):
        for asset in assets:
            if isinstance(asset, dict) and asset.get("name") == want:
                return asset
    return None


def _fetch_repo(repo: str):
    """对一个仓库拉一次发布页，返回 (payload | None, reason)。这个函数**不抛**。"""
    request = urllib.request.Request(
        latest_url_for(repo), headers={"User-Agent": USER_AGENT,
                                       "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS,
                                    context=system_ssl_context()) as response:
            body = json.loads(response.read(MAX_BYTES).decode("utf-8"))
    except Exception as e:                      # 超时/DNS/TLS/非 2xx/不是 JSON，全是一条 reason
        return None, f"拉取发布页失败：{type(e).__name__}"
    if not isinstance(body, dict):
        return None, "发布页回来的不是对象"
    version = normalize_version(body.get("tag_name"))
    if not version:
        return None, "最新那条发布没有 tag_name"
    asset = _pick_asset(body, version)
    return {
        "version": version,
        "url": str(body.get("html_url") or ""),
        "asset_name": (asset or {}).get("name") or "",
        "asset_url": (asset or {}).get("browser_download_url") or "",
        "size": int((asset or {}).get("size") or 0),
        # 正文里那行发布校验值（没有就是空串）。快照层顺手存下它，是为了让
        # `download_plan` 能把它一起交给磁盘缓存（apk_cache）：只有对得上这串
        # 摘要的字节才许落盘代管——判据见 apk_cache 的模块注释。
        "sha256": apk_sha256(body.get("body")),
        # 原样的那条发布 JSON。壳的「检查更新」走 /v1/update/info 透传它——判断逻辑
        # （三态、资产名、URL 白名单）整个活在壳里且被 JVM 台架钉着，服务端只做
        # "一台机器出网 + 缓存"这一段，不另起一份判断的第二真相。
        "raw": body,
    }, ""


def _fetch_github():
    """GitHub 那条链（**留着可复用**）：按候选仓顺序各拉一次，第一个成功的赢。

    新仓还没发版时旧仓兜底，v0.23.x 老用户的更新链不因改名而断。全失败时理由取首选仓
    那次——排查的人会先去配置里的仓看发布页，报错方向要和他的动线一致。

    v0.34 起它不再是默认取数源（账号被停用，这条链恒 404），但它是完整的、可用的一整条
    路：解封之后把 `_fetch` 里那一行换成 `return _fetch_github()` 就接回来了，接口形状
    一个字都不用动。
    """
    repos = candidate_repos()
    first_reason = ""
    for repo in repos:
        payload, why = _fetch_repo(repo)
        if payload is not None:
            global _repo_of_payload
            with _lock:
                _repo_of_payload = repo
            return payload, ""
        if not first_reason:
            first_reason = f"{why}（{repo}）" if len(repos) > 1 else why
    return None, first_reason


def _fetch():
    """读一次**自托管清单**，验形状、验磁盘摘要，攒成一份快照。这个函数**不抛**。

    这就是"取数源"那一格：v0.34 起从 GitHub 换成本地清单（账号被停用，见模块注释）。
    要换回 GitHub，把下面这一行改成 `return _fetch_github()` 即可——GitHub 那条链
    （`_fetch_github` / `_fetch_repo`）一字未删。

    自托管特有的一格判据在 `_fetch_manifest` 里：清单里写的 `sha256` 必须与**磁盘上
    那个安装包**的真实摘要一致。不一致按"读不出来"处理——症状是 502 带理由，而不是把
    一串对不上号的字节端出去。用的仍是 apk_cache 的同一条准入纪律。
    """
    return _fetch_manifest()


def _fetch_manifest():
    """自托管那一趟：读清单（含形状关）→ 验磁盘摘要 → 攒一份壳认得的快照。不抛。"""
    manifest, why, _path = load_manifest()
    if manifest is None:
        return None, why
    from app.core import apk_cache                 # 局部导入：apk_cache 反向依赖本模块
    version = normalize_version(manifest["version"])
    sha = manifest["sha256"]
    if not apk_cache.cached_file(version, sha):
        have = apk_cache.path_for(version) or "路径不可用"
        return None, (f"清单写的安装包摘要与本地盘上的字节对不上（{have}），"
                      f"不把对不上号的字节端出去")
    return {
        "version": version,
        "url": manifest["url"],
        "asset_name": manifest["apk_name"],
        "asset_url": manifest["url"],
        "size": int(manifest["size"]),
        # 快照层顺手存下摘要，让 `download_plan` 能把它交给磁盘缓存（apk_cache）——
        # 只有对得上这串摘要的字节才许落盘/出盘，判据见 apk_cache 的模块注释。
        "sha256": sha,
        # 原样的**给壳看的**发布 JSON。壳的「检查更新」走 /v1/update/info 透传它——
        # 判断逻辑（三态、资产名、URL 白名单）整个活在壳里且被 JVM 台架钉着，服务端
        # 只做"把清单翻成那个形状"这一段，不另起一份判断的第二真相。
        "raw": _synthesize_release(manifest),
    }, ""


def _refresh_now():
    """后台补一次快照：与 _snapshot 同步分支同一组记账，只是不站在提问者的线程上。
    _fetch 本身不抛（模块纪律），这里的 try/finally 守的是闩一定要落下。"""
    global _payload, _fetched_at, _payload_at, _refreshing
    try:
        payload, _why = _fetch()
        with _lock:
            _fetched_at = time.monotonic()
            if payload is not None:
                _payload = payload
                _payload_at = time.monotonic()
    finally:
        with _lock:
            _refreshing = False


def _spawn_refresh():
    """把补货这件事交出去——单独成函数，是为了契约用例能钉住"踢了但没同步出网"。"""
    threading.Thread(target=_refresh_now, daemon=True,
                     name="release-refresh").start()


def start_cache_warmer() -> None:
    """进程一起来就先把快照攒上，之后每半个缓存期照看一眼。由 main.py 的 lifespan 调用。

    为什么值得为"没人问也去拉"付账：「今天还没人开过 App」的清晨，第一个点开设置页
    点「检查更新」的人不该替所有人去撞那条 5 秒出站。冷启动那一次由本线程 synchronous
    地拉（用户还没进来），此后每轮都只会命中 _snapshot 的后台补货分支。
    """
    global _warmer_started
    with _lock:
        if _warmer_started:
            return
        _warmer_started = True
    threading.Thread(target=_warm_loop, daemon=True, name="release-warm").start()


def _warm_loop():
    while True:
        try:
            _snapshot()      # 冷：这一趟同步拉一次把缓存攒热；热且过半：只踢后台一脚
        except Exception:    # warmer 不许把进程带倒，下一轮再说
            pass
        time.sleep(REFRESH_AHEAD_SECONDS)


# 发版工作流写在 Release 正文末尾的那一行：`APK-SHA256: <64 位小写十六进制>`。
# 摘要在构建机上、签完包之后算——所以它验的是"装进手机的那串字节就是发布的那一串"，
# 服务端与下载通道都只是过手的人。锚定整行、大小写敏感：正文里的散文不许凑巧长成
# 一条校验值。
APK_SHA256_RE = re.compile(r"^APK-SHA256: ([0-9a-f]{64})$", re.M)


def apk_sha256(body_text) -> str:
    """从 Release 正文里取那行校验值；没有（旧版发布、手改正文）就是空串。"""
    if not isinstance(body_text, str):
        return ""
    m = APK_SHA256_RE.search(body_text)
    return m.group(1) if m else ""


def latest_release_manifest(origin: str = ""):
    """壳「检查更新」要的那份 JSON：把自托管清单翻成壳认得的形状 + 顶层 `apk_sha256`。

    返回 (dict, reason)。读不出来时 (None, 理由)——调用方必须把这句理由原样带给人，
    而不是回一份"看起来没有更新"的空 JSON：那条三态纪律（读不出来 ≠ 已是最新）
    从壳里一路管到服务端这一层。

    换源没有放宽任何一条纪律：形状由 `_synthesize_release` 钉住（壳真读的字段一个不
    少），`apk_sha256` 只认 `_fetch` 验过磁盘字节的那一串摘要，过期旧快照照样算"读不
    出来"。`origin` 与 `repo` 见下。

    超过一个缓存期都没再成功过的旧快照同样算"拉不到"：手动点「检查更新」问的就是
    "现在有没有新版"，把 GitHub 断供前攒下的旧货当最新发出去，恰恰是把"我读不到"
    伪装成"你已是最新"——AC-4 的后半个词是这么被违反的。卡片端点不收紧，是因为
    它拉不到就不弹，旧快照撑死多弹一句"去下载"，方向上仍是真话。

    `origin` 与 `repo` 是调用方（那条端点）带进来的这次请求的来源，见
    `alias_legacy_asset_to_self_host`：两个条件都对上才给老壳递一枚指向自家出口的
    旧名资产，否则一个字节都不动——透传仍然是默认行为，改名只是它的一条过渡期例外。
    """
    snapshot, reason, stale = _snapshot()
    if not snapshot:
        return None, reason or "还没有一次成功过的版本清单读取"
    if stale:
        return None, (f"版本清单已超过 {CACHE_SECONDS // 60} 分钟没有一次成功的读取"
                      f"（最近一次：{reason or '原因未知'}），不把旧快照冒充最新")
    raw = snapshot.get("raw")
    if not isinstance(raw, dict):
        return None, "版本快照里没有原样 JSON（内部状态坏了）"
    manifest = dict(raw)
    # assets 是共享缓存里的那份：每个请求拿一份自己的拷贝再改地址。原地改会把换过的
    # 地址带给下一个调用方（官网按钮与 download_plan 要的是清单里那一条）。
    manifest["assets"] = [dict(a) if isinstance(a, dict) else a
                          for a in (raw.get("assets") or [])]
    # 摘要只认清单里那一格（经 _fetch 验过磁盘字节），不是从 JSON 顶层或正文里"顺手读"的。
    manifest["apk_sha256"] = str(snapshot.get("sha256") or "")
    _point_self_hosted_assets(manifest, origin)
    # 老壳那枚旧名条目：来源合格时补上（自托管清单里从来只有新名一枚）。
    alias_legacy_asset_to_self_host(manifest, origin, "")
    return manifest, ""


# 自家代取端点。这一串不是我们随口起的名字，是量产壳里写死的第二条信任规则
# （`ReleasePlan.SELF_APK_PATH`），也是 web_router 注册的那条路由——三处必须一字
# 不差，钉在 test_update_channel 的对齐锁里。少一个字符的症状不是报错，是老壳对着一
# 个"看起来是下载地址"的东西回一句 UNUSABLE。
SELF_APK_PATH = "/site/android.apk"


def self_hosted_apk_url(origin: str) -> str:
    """把调用方的来源拼成自家出口地址；来源不合格时返回空串（＝不换）。

    合格线照抄壳的判据，因为这条 URL 最终是过壳的闸的：必须 https、主机里不许带端口
    或 userinfo。本地开发用 http://localhost:8000 时这里必然回空串——那正是想要的：
    老壳从来不信 http，把一个它不信的地址换上去只是把一条能用的 GitHub 地址换成
    一条用不了的。
    """
    value = (origin or "").strip()
    if not value.startswith("https://"):
        return ""
    host = value[len("https://"):].strip("/")
    if not host or "/" in host or ":" in host or "@" in host:
        return ""
    return "https://" + host + SELF_APK_PATH


def alias_legacy_asset_to_self_host(manifest: dict, origin: str, repo: str) -> int:
    """给**老壳**凑齐它那一枚旧名资产，地址指自家出口；返回这次改或加了几枚。

    两种形状都要照顾，因为它们分属改名的前后两段：
    * 这一版在 GitHub 上真挂了旧名（只有 v0.24.0 那一版这么发过）：把它的
      `browser_download_url` 换成自家出口；
    * 这一版只挂新名（v0.24.1 起这是唯一的发布形状）：从新名那枚**合成**一枚旧名条目。
      壳读的是这份 JSON，不是 GitHub 的资产表——旧名从来只是它挑资产的钥匙
      （`ReleasePlan.pickAsset` 按 `ai-assistant-native-<版本>.apk` 精确匹配名字），
      钥匙由服务端递给老壳，仓库就不必再挂那份谁也不会点开的重复文件。

    为什么必须换、必须合成，而不是原样透传：0.23.x 及更早的壳把 GitHub 路径白名单写死
    成旧仓，装进手机那一刻就改不动；搬到新仓以后 `github.com/abonla599/fenver/...`
    会被它自己的判据打成 UNUSABLE。2026-09-30 用 HEAD(=0.23.17) 那份 ReleasePlan +
    MiniJson 真跑过 `decide`：新仓双名与新仓只挂新名两档都是 UNUSABLE，只有"旧仓那条
    路径"和"自家出口"是 AVAILABLE。所以老壳能用回来的地址只有自家这一条，而它要求先
    看见那个旧名字。

    条件都对上才动手，为的是把"透传"留在默认位：
    * `repo` 仍是旧仓时不换——老壳的白名单本来就认那条路径，换了只是白占服务器带宽；
    * 合成那一枚只在新名那枚真存在时发生——新名是"这是改名之后我们发的那一版"的凭据，
      凭它才有可靠的体积与类型可抄。仓库里只挂旧名的发布（应急漏发）走上面那条换地址。

    合成那枚只带壳会读的字段（名字、地址、体积、类型），体积取自新名那枚：猜一个 size
    会让那句"约 9 MB"变成假话。新名那枚原样留着 GitHub 直连——新壳走直连，一台机器的
    带宽只兜住还没升级的那批人；而且这层转发改不动字节校验：装进手机的仍然是 Release
    正文那行 APK-SHA256 对上号的同一串字节。

    必须**改了再返回**而不是改缓存里那份：`manifest` 的 `assets` 是从 `_payload` 的
    原样 JSON 浅拷来的，原地改会把换过的地址带给下一个没给 origin 的调用方——官网那颗
    按钮与 `download_plan` 要的始终是 GitHub 真地址，换成自家出口就是自己给自己发请求。
    """
    url = self_hosted_apk_url(origin)
    if not url:
        return 0
    if repo == LEGACY_REPO:
        return 0
    version = normalize_version(manifest.get("tag_name"))
    if not version:
        return 0
    fresh, legacy = asset_names(version)         # 新名在前：它的存在是"改名之后的发布"的凭据
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        return 0
    fresh_entry = None
    copied, changed = [], 0
    for asset in assets:
        if isinstance(asset, dict):
            if asset.get("name") == fresh:
                fresh_entry = asset
            if asset.get("name") == legacy:
                asset = dict(asset)
                asset["browser_download_url"] = url
                changed += 1
        copied.append(asset)
    if not changed and isinstance(fresh_entry, dict):
        # 只挂新名的那一版：给老壳补一枚旧名条目。补在末尾，新名那枚的位置不动。
        copied.append({"name": legacy,
                       "browser_download_url": url,
                       "size": fresh_entry.get("size"),
                       "content_type": fresh_entry.get("content_type") or
                                       "application/vnd.android.package-archive"})
        changed = 1
    if changed:
        manifest["assets"] = copied
    return changed


def _snapshot() -> tuple:
    """返回 (最新一次的版本快照 | None, 这次读取失败的理由, 快照是否已过缓存期)。

    缓存的**唯一**入口：卡片（probe）与官网代取（download_plan）都从这里读，
    所以"十分钟内最多读一次清单"这条承诺只有一处实现，不会一边省、一边不省。
    重试节流看 `_fetched_at`（最近一次尝试），但**是否还新鲜**看 `_payload_at`
    （最近一次成功）：两者分开，旧货出不了「检查更新」这道门，见
    `latest_release_manifest` 的注释。

    v0.28.2 起多一档"过半补货"：手里有货、尝试时间过半个缓存期但还没过期——
    立刻把手里的货交出去（AC-1：提问的人不等网络），后台单飞补一次新货。
    过半个期但**没成功过**的旧货照样走 stale_attempt 的同步分支，新鲜度判据不动。

    v0.34 起 `_fetch` 读的是本地清单（不再出网），缓存窗口保留的意义从"省 GitHub
    的 60 次/小时限流"变成"同一份清单不重复读盘 + 不重复算 9MB 摘要"；语义未变。
    """
    global _payload, _fetched_at, _payload_at, _refreshing
    now = time.monotonic()
    kick = False
    with _lock:
        stale_attempt = (_fetched_at is None or (now - _fetched_at) >= CACHE_SECONDS
                         or _payload_at is None or (now - _payload_at) >= CACHE_SECONDS)
        if (not stale_attempt and not _refreshing and _payload is not None
                and (now - _fetched_at) >= REFRESH_AHEAD_SECONDS):
            _refreshing = True
            kick = True
    if kick:
        _spawn_refresh()
    reason = ""
    if stale_attempt:
        payload, why = _fetch()
        with _lock:
            # 失败也把时间推进到下一轮：不然每个打开 App 的人都替 GitHub 挡一次枪。
            _fetched_at = time.monotonic()
            if payload is not None:
                _payload = payload
                _payload_at = time.monotonic()
        if why:
            reason = why
    with _lock:
        snap = dict(_payload) if _payload else None
        stale = not (_payload_at is not None
                     and (time.monotonic() - _payload_at) < CACHE_SECONDS)
        return snap, reason, stale


def probe(have: str = None) -> dict:
    """这张卡片要问的全部：最新是哪版、比手上这版新吗、去哪儿下。"""
    snapshot, reason, _stale = _snapshot()

    out = {"ok": bool(snapshot), "latest": (snapshot or {}).get("version", ""),
           "url": (snapshot or {}).get("url", ""),
           "asset_name": (snapshot or {}).get("asset_name", ""),
           "size": (snapshot or {}).get("size", 0),
           "have": normalize_version(have), "has_update": None, "reason": reason}
    if not snapshot:
        out["reason"] = reason or "还没有一次成功过的版本清单读取"
        return out
    if not out["have"]:
        out["reason"] = "没告诉我现在装的是哪版，无法判断有没有更新"
        return out
    try:
        out["has_update"] = compare(out["have"], snapshot["version"]) < 0
    except ValueError as e:
        out["reason"] = str(e)
    return out


def reset_for_tests() -> None:
    """把缓存清空——测试用它等价于"换一台刚起来的机器"。

    刚起来 = `_fetched_at` 是 None，不是 0.0：后者在 monotonic 还很小（真·刚开机、
    或 CI 的虚拟机）时会被判成"缓存还新"，那正是这条缓存要防的反面。
    """
    global _payload, _fetched_at, _payload_at, _repo_of_payload, _refreshing
    with _lock:
        _payload = None
        _fetched_at = None
        _payload_at = None
        _repo_of_payload = None
        _refreshing = False


def _host_ok(url: str):
    """这一跳能不能替用户去取。返回 (可以, 理由)。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        return False, f"下载地址不是 https：{parts.scheme or '空'}"
    if parts.hostname not in DOWNLOAD_HOSTS:
        return False, f"目标主机不在白名单里：{parts.hostname}"
    return True, ""


def download_plan():
    """官网「安卓版」那颗按钮要的真东西：一个可以替用户去取的 APK 地址。

    返回 ({"url", "name", "size", "version"}, "") 或 (None, 一句理由)。这个返回值决定
    的是"陌生人的浏览器从我们这台服务器落下哪个字节流"，所以任何一项对不上都宁可拒：
    调用方拿不到 plan 就退回发布页，而不是硬编一个地址给人。

    资产名必须等于 `fenver-<这一版>.apk`（或过渡期的那一个旧名别名，见 `asset_names`）：
    一次发布可以同时挂着 mapping.txt、
    别的平台的产物或上一次误传的旧包（`_pick_asset` 同一个理由），而且这条正则顺带
    保证了它放进 Content-Disposition 是安全的——没有 CR/LF、没有引号、没有分号。
    """
    snapshot, reason, _stale = _snapshot()
    if not snapshot:
        return None, reason or "还没有一次成功过的版本清单读取"
    version = snapshot.get("version") or ""
    name = snapshot.get("asset_name") or ""
    url = snapshot.get("asset_url") or ""
    sha256 = str(snapshot.get("sha256") or "")
    try:
        size = int(snapshot.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    if not _ASSET_NAME_RE.match(name):
        return None, f"资产名不是我们发布的那个形状：{name!r}"
    if name not in asset_names(version):
        return None, f"资产名与版本号对不上：{name!r} vs {version!r}"
    if not size or size > APK_MAX_BYTES:
        return None, f"这个包的大小不像一个 APK：{size}"
    if _is_self_hosted_url(url):
        # 自托管：字节只住在本地盘上（清单里的地址就是这颗按钮自己的出口）。盘上没有、
        # 或摘要对不上，就没有"可端给陌生人"的字节——退回官网，而不是回一个空文件。
        # 判据与快照层同一套（apk_cache 的准入纪律），这里再验一次是为了兜住
        # "快照攒下之后盘上那份被换掉了"这一段。
        from app.core import apk_cache
        if not apk_cache.cached_file(version, sha256):
            return None, "自托管的安装包不在本地盘上（或与清单摘要不符），没有可端出去的字节"
    else:
        ok, why = _host_ok(url)
        if not ok:
            return None, why
    return {"url": url, "name": name, "size": size, "version": version,
            # 给磁盘缓存的准入凭据（apk_cache 只认对得上这串摘要的字节）；
            # 旧版发布没有它就是空串——那一版永远不进缓存，照旧走实时代取。
            "sha256": sha256}, ""


def _open_asset():
    """取包用的客户端。跳转自己管（见 fetch_asset），所以这里必须关掉自动跟。"""
    return httpx.Client(verify=system_ssl_context(), timeout=ASSET_TIMEOUT_SECONDS,
                        follow_redirects=False, headers={"User-Agent": USER_AGENT})


def fetch_asset(url: str):
    """替用户把包取回来，返回 (字节 | None, 理由)。这个函数不抛。

    一次下载 = 一台机器替所有点按钮的人去 GitHub 跑一趟：出口只有这一个 IP，
    所以上面那条 10 分钟缓存省的是元数据，这里省不掉的是字节。
    """
    try:
        with _open_asset() as client:
            target = url
            for _ in range(MAX_HOPS):
                ok, why = _host_ok(target)
                if not ok:
                    return None, why
                with client.stream("GET", target) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location") or ""
                        if not location:
                            return None, "上游说要跳转却没给地方"
                        target = urllib.parse.urljoin(str(response.url), location)
                        continue
                    if response.status_code != 200:
                        return None, f"上游回 {response.status_code}"
                    chunks, total = [], 0
                    for part in response.iter_bytes():
                        total += len(part)
                        if total > APK_MAX_BYTES:
                            return None, f"下载超出上限：{total}+"
                        chunks.append(part)
                    return b"".join(chunks), ""
            return None, f"跳转次数超过 {MAX_HOPS} 跳"
    except Exception as e:
        return None, f"取包失败：{type(e).__name__}"
