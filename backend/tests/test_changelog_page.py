"""官网「更新日志」页（`/changelog`）与它的产物：`site/changelog.json`。

这一页的内容源是 `docs/releases/*.md`（每版发版说明），但 **`docs/` 不进 EXE**
（`run_backend.spec` 的 datas 只收 backend/app/web/{static,admin,site}），所以内容必须
在打包前就烙进 `site/changelog.json` 这个随包走的产物里。本文件守的就是这条烙痕：

1. **同步锁**：产物必须与 `docs/releases/*.md` 一致——改了 md 忘了重新生成，这里当场红
   （而不是页面上悄悄少一段）；
2. **脚本可用**：`tools/gen_changelog.py` 必须进得了 CI 那次 checkout（`/tools/*` 默认
   被 .gitignore 排除，得靠一条 ! 例外）；
3. **页面契约**：`/changelog` 与 `/`、`/app/` 同一套缓存口径与 CSP，且页面自己去读
   同目录那份 JSON；
4. **入口**：官网导航与页脚各有一个能点到的入口。
5. **死链扫描**：整张随包发布的 Web（`site/` + `static/` + `admin/`）不许再有可点的
   github.com 链接——判据与那唯一一颗豁免见本文件末尾那两条。
"""
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
# 版本号比较只有一个尺子：`app.core.releases.compare()`（与壳里 ReleasePlan.compare 同一
# 套规则，另有跨语言锁钉着它）。本文件那把"日志不得落后于 versionName"的锁必须用它，
# 不能在这里自己拆点分段、更不能比字符串——判据见 _version_order 的注释。
from app.core.releases import compare, normalize_version
# 死链扫描要先把注释剥掉才判：留在 `/* 曾经指向 github.com/… */` 里的一句话不是可点的链，
# 而"把链接挪进注释"恰是让扫描锁静默失效的那条路。尺子复用 test_web_pwa 那两把（本文件
# 与它同属 Web 面，再抄一份剥注释的实现就是第二份真相，它会比原件先腐烂）。
from tests.test_web_pwa import _strip_html_comments, _strip_js_comments

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "tools" / "gen_changelog.py"
ARTIFACT = REPO_ROOT / "backend" / "app" / "web" / "site" / "changelog.json"
PAGE = REPO_ROOT / "backend" / "app" / "web" / "site" / "changelog.html"
# 装机清单（打包内种子）。运行期还有一份 data/version.json 覆盖它，但 data/ 是运行期资产、
# 不在仓库里，所以这把锁读的是**随包走的那一份**：页面上"能装的那一版"最终以它为准。
MANIFEST = REPO_ROOT / "backend" / "app" / "web" / "site" / "version.json"
# 机器读的适用范围标记（tools/gen_changelog.py 认它，页面按它分栏）。
SCOPE_SERVER_ONLY = "<!-- scope: server-only -->"

# ---------- 死链扫描的面（判据见 test_the_dead_github_hyperlinks_are_gone_from_the_web） ----------
WEB = REPO_ROOT / "backend" / "app" / "web"
# 用户点得到的那三面：官网、PWA、管理台。新增文件天然在面内，不需要谁记得补一处。
SCANNED_DIRS = ("site", "static", "admin")
SCANNED_SUFFIXES = {".html", ".js"}
# vendor/ 里是第三方库自带的字符串（highlight.js 的项目地址、hljs-github-dark 这个主题名）。
# 它们不是"我们把用户送去哪儿"，判红等于要求我们去改第三方源码。
EXCLUDED_PARTS = {"vendor"}
# 全仓唯一被允许挂着 github.com 链接的那一颗：PWA「关于」屏的仓库主叶（代码在哪），
# 由 tests/test_about_page_contract.py 与原生屏逐字对齐。带任何路径段的 github 地址
# （/releases、/releases/latest、/archive/…）都不在这个形状里，一律红。
REPO_HOME = "https://github.com/abonla599/fenver"
# "可点"的两种形状：href/src/action 属性，以及带引号的 URL 字面量（JS 里
# `location.href = "…"`、`window.open("…")` 与 data-* 属性里那份 JSON 都走第二条）。
_CLICKABLE_RE = re.compile(
    r"""(?:href|src|action)\s*=\s*["']([^"']*)["']"""
    r"""|['"`](https?://[^'"`\s]*)['"`]""")


def _clickable_github_links() -> list:
    """返回 [(仓库相对路径, 链接)]：注释剥掉之后，页面上真能把人带去 github 的那些形状。"""
    hits = []
    for d in SCANNED_DIRS:
        root = WEB / d
        assert root.is_dir(), f"死链扫描的面少了目录 {d}：面塌了不等于没违例"
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(WEB).as_posix()
            if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
                continue
            if EXCLUDED_PARTS & set(rel.split("/")[:-1]):
                continue
            text = path.read_text(encoding="utf-8")
            text = (_strip_html_comments(text) if path.suffix == ".html"
                    else _strip_js_comments(text))
            for groups in _CLICKABLE_RE.findall(text):
                url = next((g for g in groups if g), "")
                if "github.com" in url:
                    hits.append((path.relative_to(REPO_ROOT).as_posix(), url))
    return hits


client = TestClient(app)


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_changelog", str(GENERATOR))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- 1. 同步锁 ----------

def test_the_script_is_in_the_repo_not_only_on_this_machine():
    """`/tools/*` 默认全排除；脚本进不了仓库，下面那把同步锁在 CI 上就变成一句注释。"""
    assert GENERATOR.is_file(), "tools/gen_changelog.py 必须入库"
    res = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--stdin"],
                         input=b"tools/gen_changelog.py\n", capture_output=True, timeout=60)
    assert res.returncode != 0, (
        "tools/gen_changelog.py 被 .gitignore 挡住了：CI 那次 checkout 里没有这个文件，"
        "同步锁必然空转。加一行 !/tools/gen_changelog.py"
    )


def test_the_artifact_is_in_sync_with_the_release_notes():
    """产物与 docs/releases/*.md 必须逐字节一致：改了说明没重新生成，这里红。"""
    gen = _load_generator()
    expected = gen.render(gen.build(str(REPO_ROOT)))
    assert ARTIFACT.is_file(), "site/changelog.json 不在：跑一次 python tools/gen_changelog.py"
    have = ARTIFACT.read_text(encoding="utf-8")
    assert have == expected, (
        "site/changelog.json 与 docs/releases/*.md 不同步——"
        "跑一次 python tools/gen_changelog.py 再提交那份产物"
    )


def test_the_artifact_carries_every_release_newest_first():
    data = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    releases = data["releases"]
    notes = sorted(p.name for p in (REPO_ROOT / "docs" / "releases").glob("*.md"))
    assert len(releases) == len(notes), f"产物里有 {len(releases)} 版，说明文件有 {len(notes)} 份"
    keys = [[int(x) for x in r["version"].split(".")] for r in releases]
    assert keys == sorted(keys, reverse=True), "更新日志没有按版本从新到旧排"
    for rel in releases:
        assert rel["title"], rel
        assert rel["sections"], f"{rel['version']} 一个能读的小节都没有"
        for sec in rel["sections"]:
            assert sec["heading"] and sec["items"], sec


def _version_order(newest_entry: str, version_name: str) -> int:
    """日志最新一条相对 versionName 的位置：负数=日志落后，0=齐平，正数=日志领先。

    比较只走本仓那**一个**比较器 `app.core.releases.compare()`（与壳里
    `ReleasePlan.compare` 同一套规则，跨语言不漂移的判据在 test_release_probe.py），
    `normalize_version` 先把可能的 `v` 前缀剥掉。**不许换成字符串序**：
    `"0.34.10" >= "0.34.9"` 按字符串是假的，按版本序是真的——补丁位进十那天，
    字符串序会把一份并不落后的日志判成落后（而它另一头的错更危险：把真落后的日志
    判成领先，见下面那条用例里 0.34.9 vs 0.34.10 那两组）。
    """
    return compare(normalize_version(newest_entry), normalize_version(version_name))


def _changelog_is_not_behind(newest_entry: str, version_name: str) -> bool:
    """这条锁的判据本体：日志最新一条**不落后于** versionName（允许领先）。"""
    return _version_order(newest_entry, version_name) >= 0


def test_the_current_version_is_the_newest_entry():
    """更新日志的最新一条不得**落后于** build.gradle 的 `versionName`；领先是合法的。

    这条锁防的是一件事：**发版说明漏写**（日志落后于代码）。它防的不是"两个部署物
    版本号必须相等"——现网本来就有两个各自独立部署的东西（PyInstaller 服务端整包 +
    APK），谁先走一步是常态而不是事故（历史上壳 0.27.x / 服务端 0.25.0 就分歧过）。

    v0.34.1 就是那个常态的一个具体实例：那一版只换服务端（`?v=` 反射、下载地址域
    白名单两个修复都在后端），APK 仍是 0.34.0，所以日志领先壳一位。而
    `tools/pre_tag_gate.py` 要求每个 tag 都带 `docs/releases/<tag>.md`——服务端单独
    发版**必须**多出一条日志，写死相等等于把这条发版路径堵在闸门口。

    所以这里从 `==` 放松到"不得落后"，放松的只有相等这一半：
    **日志落后于代码仍然当场红**，那一半由下面那条用例单独钉着，别把它读成"这条锁
    已经没人管了、可以顺手删"。
    """
    import re
    gradle = (REPO_ROOT / "android-native" / "app" / "build.gradle").read_text(encoding="utf-8")
    m = re.search(r'^\s*versionName\s+"([^"]+)"', gradle, re.M)
    assert m, "读不到 versionName"
    newest = json.loads(ARTIFACT.read_text(encoding="utf-8"))["releases"][0]["version"]
    assert _changelog_is_not_behind(newest, m.group(1)), (
        f"更新日志落后于代码：日志最新是 v{newest}，而 versionName 已经是 "
        f"v{m.group(1)}——本版说明没写（补 docs/releases/v{m.group(1)}.md），"
        "或写了没重新生成产物（跑一次 python tools/gen_changelog.py）")


def test_the_lock_still_bites_when_the_changelog_falls_behind():
    """把上面那条锁的**后半截判据**单独钉死：落后必须判死，而且判据真的是版本序。

    上一条从 `==` 改成"不得落后"之后，这条锁剩下的牙就只咬"日志落后于代码"那一半。
    一半判据若没有自己的用例，改天的人会以为整条锁已经是装饰品而把它删掉——所以这里
    钉的是判据本体（`_changelog_is_not_behind`，它只做一件事：把比较交给
    `releases.compare()`），四组数位各断言一个明确的**判定结果**，不是调一遍看看：

    * `0.34.1` 对 `0.34.0` —— 允许。服务端单独发版（v0.34.1 现网形状）不许红。
    * `0.34.0` 对 `0.34.0` —— 允许。这是改成 `>=` 之后唯一仍被允许的相等形状。
    * `0.34.0` 对 `0.34.1` —— **必须判死**。日志落后 = 本版说明漏写，原意图就在这儿。
    * `0.34.9` 对 `0.34.10` —— **必须判死**；`0.34.10` 对 `0.34.9` —— 必须允许。
      这两组是专门拿来证明"判据不是字符串序"的，因为这两组上两种序**恰好相反**
      （实测：`"0.34.10" >= "0.34.9"` 按字符串是 **假**，`"0.34.9" >= "0.34.10"` 按
      字符串是 **真**）：字符串序会把前一组的合法日志误判成落后去拦服务端单独发版，
      更会把后一组**真落后的日志判成合法**——锁静默失效且不报错，正是本仓反复防的
      那一类。补丁位进十之前，这两组就是这条锁"用的是版本序"唯一的牙印。
    """
    allowed = [("0.34.1", "0.34.0"),    # 服务端领先 APK 一位：v0.34.1 的现网形状
               ("0.34.0", "0.34.0"),    # 齐平：两个部署物同版
               ("0.34.10", "0.34.9")]   # 两位数补丁位领先，且字符串序会说"不领先"
    must_die = [("0.34.0", "0.34.1"),   # 落后一位：发版说明漏写
                ("0.34.9", "0.34.10")]  # 落后一位，但字符串序会说"没落后"

    for newest, ver in allowed:
        assert _changelog_is_not_behind(newest, ver), \
            f"日志 {newest} 不比 versionName {ver} 旧，这条锁却判了死——误伤会把" \
            "服务端单独发版堵死"
        assert _version_order(newest, ver) >= 0, (newest, ver)

    for newest, ver in must_die:
        assert not _changelog_is_not_behind(newest, ver), \
            f"日志 {newest} 落后于 versionName {ver} 却没被判死——这条锁已经咬不到" \
            "『发版说明漏写』了（多半是有人把版本序换成了字符串序）"
        assert _version_order(newest, ver) < 0, (newest, ver)

    # 牙印本身也要有出处：判据必须真的是 releases.compare()，不是抄了个大概。
    assert _version_order("0.34.10", "0.34.9") == compare("0.34.10", "0.34.9") > 0
    assert "0.34.10" < "0.34.9", "上面那两组的前提没了：字符串序恰好等价于版本序，" \
        "这条用例就不再证明任何事——等它重新有区别时把这条一起改掉"


# ---------- 1b. 领先的那一版必须自报"只换服务端"；头条必须是装得到的那一版 ----------
#
# 上面那把锁把"日志不得落后于 versionName"放松成"允许领先"之后（52fb66c9），剩下一个
# 没人管的方向：**领先的那一版根本没有安装包**。现网就是这句话的形状——/changelog 的
# 头条是 v0.34.1，而 /v1/update/info 报的 tag_name 与两份清单里能下载的包都是 0.34.0。
# v0.34.1 靠人在说明第一行手打一句"本版只换服务端"（docs/releases/v0.34.1.md:3），
# 那句话没有任何机器读过，下一版忘写就什么都没有了。
#
# 这里补两把，一把管声明、一把管播出：
# * **声明锁**：说明版本号领先于 versionName ⇒ 那份说明必须带机器读的 scope 标记。
#   判"领先"用的还是上面那把尺子（releases.compare），不是自己拆点分段。
# * **头条锁**：页面播出去的那个"最新版本"必须等于清单里可安装的那一版。
#   这一把才是用户看得见的那一面——声明漏了会被上一把抓住，声明齐了但页面算错了，
#   由这一把抓住。
#
# 两把都用**合成语料**验过牙（_fake_release_root + 下面那两条 bites 用例），不像
# test_apk_release_discipline 那几条读 workflow 文本的锁——那几把守的是没人跑的托管流水线。

def _manifest_installable() -> str:
    """清单里那一版 = 用户点「下载安卓版」真能拿到的那一版（打包内那一份种子）。"""
    return str(json.loads(MANIFEST.read_text(encoding="utf-8"))["version"])


def _installable_head(releases) -> str:
    """页面该播的「最新版本」：从新到旧**第一条有安装包的**条目的版本号；一条都没有则空串。

    这条规则在 `changelog.html` 里有一份 JS 实现（页面自己算，不抄版本号），这里是同一
    条规则的**锁侧**实现。两份实现必须一起改，所以
    `test_the_page_splits_server_only_notes_out_of_the_headline` 把 JS 那一侧的判据形状
    钉住：谁把 filter 换回 `releases[0]`，那一条当场红。
    """
    for rel in releases:
        if not rel.get("server_only"):
            return rel["version"]
    return ""


def _undeclared_leaders(releases, version_name: str) -> list:
    """领先于 versionName、却没声明"只换服务端"的那些条（声明锁的判决本体）。"""
    return [rel["version"] for rel in releases
            if _version_order(rel["version"], version_name) > 0 and not rel.get("server_only")]


def _gradle_version_name() -> str:
    gradle = (REPO_ROOT / "android-native" / "app" / "build.gradle").read_text(encoding="utf-8")
    m = re.search(r'^\s*versionName\s+"([^"]+)"', gradle, re.M)
    assert m, "读不到 versionName"
    return m.group(1)


def _artifact_releases() -> list:
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))["releases"]


def test_a_note_ahead_of_the_installable_version_declares_itself_server_only():
    """声明锁：说明比 versionName 新 ⇒ 那一版就没有安装包，必须机器可读地声明出来。

    为什么判"领先"而不是判"不相等"：历史里 47 版都各自有包、都"不等于"今天的
    versionName，拿"不相等"当判据等于要求把整叠旧说明都补标记——那种锁的第一批
    违例是它自己造的。**领先**这一半才是只有"服务端单独发版"能产生的形状。

    反过来（写了标记、版本却不领先）由下面那把头条锁管：标记会把那条从可安装里摘出去，
    头条于是落后于清单，当场红。两把合起来才是闭合的。
    """
    leaders = _undeclared_leaders(_artifact_releases(), _gradle_version_name())
    assert not leaders, (
        f"这几版的说明领先于 versionName 却没带 scope 标记：{leaders}——"
        f"领先就只能是没有安装包的一版，请在 docs/releases/v{leaders[0]}.md 的标题之后、"
        f"第一个小节之前补一行 {SCOPE_SERVER_ONLY}")


def test_the_headline_version_is_one_people_can_actually_install():
    """头条锁：页面播出去的「最新版本」必须等于清单里那个真能下载的包。

    这条直接对着用户被拒绝过的那句话——日志头条 v0.34.1、能装的只有 0.34.0。
    它同时也是"删掉最新一条可安装说明"的那把闸：那种改动会让可安装头退到上一版，
    与清单不符，当场红（原来那把"日志不得落后于 versionName"的锁抓不到这一类，
    因为它看的是**任意**最新条目，而那条可能只是个服务端修正）。

    只对齐**清单**（`site/version.json`），不去对齐 build.gradle：后者是"这个 APK 源码
    自己叫啥"，前者才是"这台机器现在发得出去的包是哪一版"。两者不同步时该红的是后者，
    而那由 `tools/pre_tag_gate.py` 在打 tag 前管。
    """
    head = _installable_head(_artifact_releases())
    manifest = _manifest_installable()
    assert head, "更新日志里一条可安装的条目都没有：页面那句「最新版本」将无处可取"
    assert head == manifest, (
        f"更新日志播的最新可安装版本是 v{head}，而清单里能下载的包是 v{manifest}——"
        "要么有一版服务端单独发版的说明漏了 scope 标记（它被当成可安装的那一版播出去了），"
        "要么最新那条可安装的说明/清单被删改过。用户点「下载安卓版」拿到的一定要是"
        "页面上那个号，否则这一页就是在把人送去装一个装不上的版本")


def test_the_scope_marker_is_parsed_into_a_boolean_and_not_left_in_the_prose():
    """标记必须是**机器读的那一份**，同时不许把人读的那句话挤掉。

    两个失败形状都在这里判：
    - 标记没被生成器认（那两把锁全在空转）；
    - 标记被当成正文搬进 `intro`（官网就会印一行 `<!-- scope: ... -->` 那种黑话）。
    现网 v0.34.1 那一版同时验两条：它有标记 ⇒ server_only 为真；它原本那句
    "本版只换服务端，安卓包仍是 0.34.0……" 必须还在 intro 里（补机器标记不是删人话）。
    """
    note = (REPO_ROOT / "docs" / "releases" / "v0.34.1.md").read_text(encoding="utf-8")
    assert SCOPE_SERVER_ONLY in note.splitlines(), \
        "v0.34.1 的说明没带机器读的 scope 标记（那一版就是「只换服务端」的定义样例）"
    entry = [rel for rel in _artifact_releases() if rel["version"] == "0.34.1"]
    assert entry, "产物里没有 0.34.1 这一条"
    assert entry[0]["server_only"] is True, \
        f"生成器没把标记读成布尔值：{entry[0]['server_only']!r}"
    for rel in _artifact_releases():
        assert "scope:" not in rel["intro"], \
            f"v{rel['version']} 的 intro 里混进了 scope 那行：官网会原样印出来"
    assert "只换服务端" in entry[0]["intro"], \
        "v0.34.1 引言里那句给人读的话被删了：补机器标记不是拿机器标记换掉人话"


def _page_splits_the_two_groups(html: str) -> bool:
    """changelog.html 是否真的"按能不能装分栏、头条取可安装那一版"。"""
    code = _strip_html_comments(html)
    return bool(
        re.search(r"releases\.filter\(function \(rel\) \{\s*return !rel\.server_only;\s*\}\)",
                  code)
        and re.search(r"releases\.filter\(function \(rel\) \{\s*return !!rel\.server_only;\s*\}\)",
                      code)
        and "installable[0].version" in code                    # 头条取自筛过的那一组
        and "releases[0].version" not in code                   # 而不是"最新那一条"
        and "仅服务端修正（无安装包）" in code                       # 另一块有明确的标签
        and "articleFor" in code)                               # 两组共用同一份正文渲染


# ---------- 2b. 页面渲染真跑：node + 最小 DOM 替身（判例 test_v032_w3_citation_source_contract.py） ----------
#
# 上面那把 `_page_splits_the_two_groups` 是**文本**判据：它能抓住"筛子被撤掉"，抓不住
# "筛子还在但筛错了对象"。本仓对网页 JS 的既有做法是把那段真跑一遍（没有浏览器、不装
# 依赖，一个 node + 一份手写 DOM 替身），所以这里沿用同一招：把 changelog.html 里含
# `function render(` 的那段内联脚本抽出来，喂给它**真产物**，再把渲染完的 DOM 收回来判。
# 没有 node 的机器上这几条 skip（与 test_v0340_swap_path_normalization.py:68 同一口径），
# 文本判据那几条照跑。
_RENDER_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const htmlPath = process.argv[2], dataPath = process.argv[3];
const DATA = JSON.parse(fs.readFileSync(dataPath, "utf8"));
const html = fs.readFileSync(htmlPath, "utf8");
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const code = blocks.find((b) => b.indexOf("function render(") >= 0);
if (!code) { console.error("no inline script with function render()"); process.exit(3); }

function makeEl(tag) {
  const el = { tag: tag, id: "", className: "", children: [], attrs: {}, _own: "" };
  el.appendChild = (c) => { el.children.push(c); return c; };
  el.setAttribute = (k, v) => { el.attrs[k] = v; };
  el.scrollIntoView = () => {};
  Object.defineProperty(el, "textContent", {
    get() {
      if (el.tag === "#text") return el._own;
      return el._own + el.children.map((c) => c.textContent).join("");
    },
    set(v) { el._own = String(v); el.children = []; },
  });
  return el;
}
function walk(node, out) {
  out.push(node);
  node.children.forEach((c) => walk(c, out));
  return out;
}
const root = makeEl("body");
const byId = {};
["logHint", "installableLine", "logList"].forEach((id) => {
  const el = makeEl("p");
  el.id = id;
  byId[id] = el;
  root.appendChild(el);
});
const document = {
  createElement: makeEl,
  createTextNode: (t) => { const el = makeEl("#text"); el._own = String(t); return el; },
  getElementById: (id) => byId[id] || walk(root, []).find((n) => n.id === id) || null,
  documentElement: makeEl("html"),
};
document.documentElement.setAttribute = () => {};
const sandbox = {
  document,
  location: { hash: "" },
  fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(DATA) }),
  Promise, JSON, Object, Array, String, Number, Boolean, Error, RegExp, Math, console,
  setTimeout, decodeURIComponent,
  window: { matchMedia: () => ({ matches: false }) },
  localStorage: { getItem: () => null, setItem: () => {} },
};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
// 页面走的是 fetch().then(render)，微任务得先落地再收 DOM
setTimeout(() => {
  const all = walk(root, []);
  const text = (id) => { const n = byId[id]; return n ? n.textContent : null; };
  process.stdout.write(JSON.stringify({
    hint: text("logHint"),
    headline: text("installableLine"),
    articles: all.filter((n) => n.tag === "article").map((n) => n.id),
    headings: all.filter((n) => n.tag === "h2").map((n) => n.textContent),
    listKids: byId.logList.children.map((n) => n.tag + "|" + n.id + "|" + n.textContent.slice(0, 24)),
  }));
}, 30);
"""


def _render_page(html_text: str, releases: list) -> dict:
    """真跑一次页面渲染，收回 {hint, headline, articles, headings, listKids}。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段页面 JS；文本判据那几条照跑")
    work = Path(tempfile.mkdtemp(prefix="changelog-render-"))
    page = work / "changelog.html"
    page.write_text(html_text, encoding="utf-8")
    data = work / "changelog.json"
    data.write_text(json.dumps({"generated_from": "test", "releases": releases},
                               ensure_ascii=False), encoding="utf-8")
    harness = work / "harness.cjs"
    harness.write_text(_RENDER_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(harness), str(page), str(data)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    assert r.returncode == 0, f"harness 自己就红了：\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout)


def test_the_rendered_page_headlines_the_version_people_can_install():
    """真渲染出来的那一页：头条必须是清单里那一版，服务端修正的那条不许当头条。

    这条是 `test_the_headline_version_is_one_people_can_actually_install` 的另一半：
    那条判产物里的数据**支持**正确答案，这条判页面**算出来的**就是那个答案。
    两者用同一个判据（挑第一条非 server_only 的条目）却走完全不同的路径
    （Python 侧筛 JSON / node 侧跑页面 JS），所以「筛子还在但筛错对象」这类改法
    只红在这一条上——文本判据看不见筛错了什么。
    """
    releases = _artifact_releases()
    out = _render_page(PAGE.read_text(encoding="utf-8"), releases)
    installable = [rel for rel in releases if not rel.get("server_only")]
    server_only = [rel for rel in releases if rel.get("server_only")]
    assert installable and server_only, (
        f"现网形状没了（可安装 {len(installable)} 条 / 只换服务端 {len(server_only)} 条）："
        "这条用例的判据前提是两个分组都得非空，回去读 v0.34.1 那条说明")

    assert "v" + installable[0]["version"] in out["headline"], \
        f"页面头条报的不是可安装那一版：{out['headline']!r}"
    for rel in server_only:
        assert "v" + rel["version"] not in out["headline"], \
            f"页面把一个没有安装包的版本（v{rel['version']}）报成了最新可安装"
    # 一条都不能少：分栏不是"藏起来"
    assert len(out["articles"]) == len(releases), (
        f"产物里 {len(releases)} 条，页面上只剩 {len(out['articles'])} 条：分栏不许吞内容")
    assert any("仅服务端修正（无安装包）" in h for h in out["headings"]), \
        f"页面上没有那个分栏标题：{out['headings']}"
    # 服务端修正那几条必须排在分栏标题**之后**（不然"分开"只是说说）
    order = out["listKids"]
    group_at = [i for i, k in enumerate(order) if "仅服务端修正（无安装包）" in k]
    assert group_at, "分栏标题不在渲染出来的列表里"
    first_server_at = [i for i, k in enumerate(order)
                       if k.split("|")[1] == "v" + server_only[0]["version"]]
    assert first_server_at and first_server_at[0] > group_at[0], \
        f"只换服务端的那几条没有排在分栏标题之后：{order[:4]}…{order[group_at[0]:group_at[0] + 3]}"


def test_the_rendered_page_goes_red_when_the_headline_follows_the_newest_entry():
    """上面那条真渲染判据的牙：把页面头条改回「最新那一条」，渲染结果必须错。

    文本判据抓得住「筛子被撤了」，抓不住「筛子还在、筛错了对象」。变异样本喂进**同一个**
    node harness，判它渲染出来的那行字里出现了装不上的版本号。样本必须先生效
    （`mutated != page`），否则这条红是 fixture 坏了，不是锁有牙。
    """
    page = PAGE.read_text(encoding="utf-8")
    mutated = page.replace("installable[0].version", "releases[0].version")
    assert mutated != page, "变异样本没生效：页面里那一处 installable[0].version 不在了"
    releases = _artifact_releases()
    server_only = [rel for rel in releases if rel.get("server_only")]
    assert server_only, "现网已经没有「只换服务端」的条目了，这个样本无从构造"

    out = _render_page(mutated, releases)
    assert "v" + server_only[0]["version"] in out["headline"], (
        f"把头条退回 releases[0] 之后，渲染出来那行字里竟然没出现装不上的版本号："
        f"{out['headline']!r} —— 说明这条判据读的不是它声称读的那一行")

    real = _render_page(page, releases)
    assert "v" + server_only[0]["version"] not in real["headline"], \
        f"真文件的渲染结果就把那个版本当头条播出去了：{real['headline']!r}"


def test_the_page_splits_server_only_notes_out_of_the_headline():
    """页面契约：服务端单独发版的那些条收进「仅服务端修正（无安装包）」，头条只报可安装的。

    信息一条不删（`articleFor` 两组共用，深链 `#v<版本>` 也照常落点），改的只是
    "页面把哪个版本号当『最新』播出去"。这一条是文本锁——本仓没有 JS 引擎可跑，
    所以判据形状钉死 + 变异自证（下面三组内存变异），与
    `test_the_changelog_page_keeps_release_notes_as_plain_text` 同一档。
    """
    assert PAGE.is_file(), "site/changelog.html 不见了"
    assert _page_splits_the_two_groups(PAGE.read_text(encoding="utf-8")), (
        "更新日志页不再按 server_only 分栏了：头条会重新跟着 releases[0] 走，"
        "而那正是「把一个装不上的版本号播成最新版本」的那个形状")

    # 变异一：头条退回"最新那一条"——这正是被用户拒绝过的现网形状
    page = PAGE.read_text(encoding="utf-8")
    back_to_newest = page.replace('installable[0].version', 'releases[0].version')
    assert back_to_newest != page, "变异样本没生效：installable[0] 那一处不在了"
    assert not _page_splits_the_two_groups(back_to_newest), \
        "把头条退回 releases[0]，这条锁必须红"

    # 变异二：分栏筛子撤掉（全部当可安装）
    no_filter = page.replace("releases.filter(function (rel) { return !rel.server_only; })",
                             "releases")
    assert no_filter != page and not _page_splits_the_two_groups(no_filter), \
        "撤掉可安装那一组的筛子，这条锁必须红"

    # 变异三：只把标签那句删掉（信息还在，但不再有「无安装包」这个说明）
    no_label = page.replace('"仅服务端修正（无安装包）"', '"改过的版本"')
    assert no_label != page and not _page_splits_the_two_groups(no_label), \
        "分栏标签被换成中性说法，这条锁必须红：用户分不出哪一版装得上"


def _fake_release_root(tmp_path: Path, notes: dict) -> Path:
    """一棵只用来喂生成器的假 docs/releases（不碰仓库、不碰 data/、不联网）。"""
    root = tmp_path / "fake-repo"
    out = root / "docs" / "releases"
    out.mkdir(parents=True, exist_ok=True)
    for name, body in notes.items():
        (out / name).write_text(body, encoding="utf-8")
    return root


_FAKE_FULL = """# v{ver} 更新说明

{scope}## 修复了什么

- 这一版有安装包，改动都在壳里。
"""


def test_the_declaration_lock_bites_when_a_leading_note_forgets_the_marker(tmp_path):
    """声明锁的牙：把一版领先于 versionName 的说明**不写**标记喂进去，必须判红。

    这里跑的是真生成器 + 真判决函数，语料是 tmp_path 下的假 docs/releases：
    判据一旦在合成语料上咬不动（比如有人把 _undeclared_leaders 改成恒返回空），
    上面那条真产物的用例与这条会一起红。
    """
    gen = _load_generator()
    root = _fake_release_root(tmp_path, {
        "v0.9.8.md": _FAKE_FULL.format(ver="0.9.8", scope=""),
        "v0.9.9.md": _FAKE_FULL.format(ver="0.9.9", scope=""),       # 忘了声明
    })
    releases = gen.build(str(root))["releases"]
    assert _undeclared_leaders(releases, "0.9.8") == ["0.9.9"], \
        "领先一版却没写 scope 标记，声明锁没判红——它已经在空转"
    assert _installable_head(releases) == "0.9.9", \
        "没标记时这条本该被当成可安装的那一版（头条锁因此也必须红）"

    fixed = _fake_release_root(tmp_path / "fixed", {
        "v0.9.8.md": _FAKE_FULL.format(ver="0.9.8", scope=""),
        "v0.9.9.md": _FAKE_FULL.format(ver="0.9.9", scope=SCOPE_SERVER_ONLY + "\n\n"),
    })
    releases2 = gen.build(str(fixed))["releases"]
    assert _undeclared_leaders(releases2, "0.9.8") == [], \
        "补了标记还判红：声明锁会把人推回去删标记而不是补标记"
    assert _installable_head(releases2) == "0.9.8", \
        "补了标记之后，可安装头条应退回 0.9.8（清单那一版），这才对得上"


def test_the_headline_lock_bites_when_the_newest_installable_note_disappears(tmp_path):
    """头条锁的牙：最新那条可安装的说明被撤走（这里用"改标成只换服务端"复现），必须红。

    原来那把"日志不得落后于 versionName"的锁对这类改动**是绿的**——它的 releases[0]
    仍是 0.9.9（不比 versionName 旧）。所以"落后要红"这一半在这里由头条锁接管：
    可安装头退到 0.9.8 之前的空集，而清单还写着 0.9.9。
    """
    gen = _load_generator()
    root = _fake_release_root(tmp_path, {
        "v0.9.8.md": _FAKE_FULL.format(ver="0.9.8", scope=SCOPE_SERVER_ONLY + "\n\n"),
        "v0.9.9.md": _FAKE_FULL.format(ver="0.9.9", scope=SCOPE_SERVER_ONLY + "\n\n"),
    })
    releases = gen.build(str(root))["releases"]
    head = _installable_head(releases)
    assert head == "", "两条都声明只换服务端时，可安装头必须是空串（页面那句要走的分支）"
    # 老那把锁在这个形状上是**绿的**：releases[0] 仍是 0.9.9，不比 versionName 旧。
    # 这句不是装饰——它记的是"为什么要另立一把"：哪天有人把老锁改成也能判死这一类，
    # 本条就该并进那条，而不是留下两把重复的闸。
    assert _changelog_is_not_behind(releases[0]["version"], "0.9.9"), (
        "老锁现在也能判死这一类了：那这把头条锁的独立存在理由要重新评估")
    assert head != "0.9.9", "清单那一版仍然被页面当头条播出去——这条锁没牙了"
    # 真产物的同一判据（把上面的合成语料换成现网那份，走的是同一个 _installable_head）
    assert _installable_head(_artifact_releases()) == _manifest_installable()


def test_the_generator_refuses_an_unknown_scope_value(tmp_path):
    """写错值不许静默回落到"有安装包"：那正是这条标记要防的那个事故。"""
    gen = _load_generator()
    root = _fake_release_root(tmp_path, {
        "v0.9.9.md": _FAKE_FULL.format(ver="0.9.9",
                                       scope="<!-- scope: serverOnly -->\n\n"),
    })
    with pytest.raises(ValueError) as e:
        gen.build(str(root))
    assert "server-only" in str(e.value), f"报错没说清只认哪些值：{e.value}"



# ---------- 2. 页面契约 ----------

def test_the_changelog_page_is_served_with_the_same_page_policy_as_the_homepage():
    """/changelog 与 /、/app/ 是同一套页面口径（cache 与 CSP 都得对上），别自造第二套。"""
    res = client.get("/changelog")
    assert res.status_code == 200, res.text
    assert res.headers["cache-control"] == client.get("/").headers["cache-control"], \
        "更新日志页的缓存口径和首页走岔了"
    assert res.headers["content-security-policy"] == \
        client.get("/").headers["content-security-policy"]
    assert "frame-src 'none'" in res.headers["content-security-policy"]


def test_the_page_reads_the_artifact_from_its_own_directory():
    """页面必须去读同目录那份 changelog.json（而不是把内容抄进 HTML——那就第三份真相）。"""
    assert PAGE.is_file(), "site/changelog.html 不见了"
    html = PAGE.read_text(encoding="utf-8")
    assert "/site/changelog.json" in html, "页面不去读产物 JSON"
    assert "fetch(" in html, "页面没有把 JSON 取回来"
    # 正文不许硬编版本号（同 test_website 那条 KPI：会自己变的事实不抄进静态页）
    assert not __import__("re").search(r"v\d+\.\d+", html), "页面里写死了版本号"


def test_the_page_and_artifact_travel_with_the_package():
    """spec 收的是整个 site/ 目录，所以新加的两个文件天然随包走——这条把前提钉住。"""
    spec = (REPO_ROOT / "run_backend.spec").read_text(encoding="utf-8")
    assert "backend/app/web/site" in spec and "'app/web/site'" in spec


def test_the_homepage_links_to_the_changelog():
    """官网导航里要有一个点得到的「更新日志」入口。"""
    index = (REPO_ROOT / "backend" / "app" / "web" / "site" / "index.html").read_text(
        encoding="utf-8")
    assert 'href="/changelog"' in index, "首页没有指向更新日志页的链接"


def test_the_dead_github_hyperlinks_are_gone_from_the_web():
    """整张随包发布的 Web（官网 + PWA + 管理台）不许再有可点的 github.com 链接。

    `abonla599` 被停用后那条地址恒 404（本机实测），把访客送去一个 404 比不放链接更糟。
    这把锁原来只扫 `site/index.html`——v0.34 合流时 PWA（`static/index.html`）那两颗
    「检查更新 / 立即更新」还挂在 `github.com/.../releases/latest` 上没人对齐，就是
    "面只铺了一半"的代价：官网干净了，用户真点的那一面没干净。现在面按目录铺，
    新增页面天然在扫描范围内，不需要谁记得再来补一处。

    判据写成"除仓库主叶之外一律不许"，而不是"releases 那一层不许"：账号停用之后
    github.com 上这个仓的**任何**地址都是 404，只钉 /releases 等于给下一次换个
    `/releases/download/…` 或 `/archive/…` 留了门缝。

    唯一豁免是 PWA「关于」屏那一颗仓库主叶（`开源仓库` 那一行）：它答的是"代码在哪"，
    不是"包在哪"，摘掉它等于把开源项目自己的源码入口删了。官网把同一句话留成**纯文字**
    （site/index.html 那句"仓库 github.com/abonla599/fenver 里的 docs/INSTALL.md"），
    由 tests/test_website.py 两处钉着；PWA 这一颗则被 tests/test_about_page_contract.py
    与原生「关于」屏逐字对齐，动它就是动双端同文案那条契约。两种留法都不是可点的更新链。
    """
    hits = _clickable_github_links()
    off_home = [(f, u) for f, u in hits if u != REPO_HOME]
    assert not off_home, (
        "这些是可点的 github.com 链接，而那个账号停用之后它们恒 404——"
        "更新链的出口在自家端点上（/site/android.apk、/changelog），别指回站外："
        f"{off_home}")
    homes = [f for f, u in hits if u == REPO_HOME]
    assert homes == ["backend/app/web/static/index.html"], (
        f"仓库主叶那一颗的豁免必须恰好一处、且只在 PWA 的「关于」屏，实际在：{homes}——"
        "多了就说明有人又给 github 挂了一颗链，少了说明「关于」屏的源码入口被顺手删了")


def test_the_changelog_page_keeps_release_notes_as_plain_text():
    """更新日志页把 JSON 里的正文当**文字**渲染，这是那些历史 github 地址不成为死链的前提。

    `site/changelog.json` 里 v0.23.x 那几条发版说明白纸黑字写着
    `https://github.com/abonla599/fenver/releases/tag/…`。它们是历史证据（"那一版的包
    当初发在哪儿"），改史等于说谎，所以不动；它们不构成上一条锁的违例，仅仅因为
    `changelog.html` 的 `inline()` 刻意不解析链接（那句注释写着"免得把发版说明里一个
    失效地址变成页面上一颗可点的按钮"）。哪天有人给它加上自动 linkify 或
    `innerHTML`，那几段历史文字就当场变成可点死链——而上面那把扫描锁看不见 JSON 里的
    字面量会变成链接。所以这里钉住"不建 <a>"这一件事本身。
    """
    page = PAGE.read_text(encoding="utf-8")
    assert "function inline(" in page, "更新日志页的行内渲染被换了，本条的判据对象没了"
    assert 'createElement("a")' not in page, \
        "changelog.html 开始自己造 <a>：历史正文里的地址会变成可点死链"
    assert "innerHTML" not in page, \
        "更新日志页改用 innerHTML：Markdown/HTML 里的链接会被浏览器原样点亮"
    assert "createTextNode" in page, \
        "行内渲染不再走 createTextNode：上面那两句判的前提已经不成立了"


def test_the_generated_artifact_is_a_project_file_not_a_runtime_asset():
    """产物必须进版本库（它是随包发布的页面数据），不能落在被忽略的 data/ 下。"""
    assert ARTIFACT.is_file()
    res = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "-q",
                          "backend/app/web/site/changelog.json"],
                         capture_output=True, timeout=60)
    assert res.returncode != 0, "changelog.json 被 .gitignore 挡住了：CI 那次 checkout 里不会有它"
