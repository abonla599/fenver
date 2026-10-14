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
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
# 死链扫描要先把注释剥掉才判：留在 `/* 曾经指向 github.com/… */` 里的一句话不是可点的链，
# 而"把链接挪进注释"恰是让扫描锁静默失效的那条路。尺子复用 test_web_pwa 那两把（本文件
# 与它同属 Web 面，再抄一份剥注释的实现就是第二份真相，它会比原件先腐烂）。
from tests.test_web_pwa import _strip_html_comments, _strip_js_comments

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "tools" / "gen_changelog.py"
ARTIFACT = REPO_ROOT / "backend" / "app" / "web" / "site" / "changelog.json"
PAGE = REPO_ROOT / "backend" / "app" / "web" / "site" / "changelog.html"

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


def test_the_current_version_is_the_newest_entry():
    """最新那条必须就是本仓当前 versionName 那一版——日志不能落后于代码。"""
    import re
    gradle = (REPO_ROOT / "android-native" / "app" / "build.gradle").read_text(encoding="utf-8")
    m = re.search(r'^\s*versionName\s+"([^"]+)"', gradle, re.M)
    assert m, "读不到 versionName"
    newest = json.loads(ARTIFACT.read_text(encoding="utf-8"))["releases"][0]["version"]
    assert newest == m.group(1), f"日志最新是 v{newest}，代码已经是 v{m.group(1)}"


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
