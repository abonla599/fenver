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
"""
import importlib.util
import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "tools" / "gen_changelog.py"
ARTIFACT = REPO_ROOT / "backend" / "app" / "web" / "site" / "changelog.json"
PAGE = REPO_ROOT / "backend" / "app" / "web" / "site" / "changelog.html"

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


def test_the_dead_github_hyperlinks_are_gone_from_the_site():
    """官网不许再有指向 github.com/abonla599/fenver 的**超链接**。

    `abonla599` 被停用后那条地址恒 404（本机实测），把访客送去一个 404 比不放链接更糟。
    运维层面它仍可能解封，所以仓库地址作为**文字**保留在开源区块里（不是可点链接），
    由 test_website 那几条继续看着。
    """
    index = (REPO_ROOT / "backend" / "app" / "web" / "site" / "index.html").read_text(
        encoding="utf-8")
    dead = [ln.strip() for ln in index.splitlines()
            if 'href="https://github.com/abonla599' in ln]
    assert not dead, f"官网还挂着失效的 GitHub 超链接：{dead}"


def test_the_generated_artifact_is_a_project_file_not_a_runtime_asset():
    """产物必须进版本库（它是随包发布的页面数据），不能落在被忽略的 data/ 下。"""
    assert ARTIFACT.is_file()
    res = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "-q",
                          "backend/app/web/site/changelog.json"],
                         capture_output=True, timeout=60)
    assert res.returncode != 0, "changelog.json 被 .gitignore 挡住了：CI 那次 checkout 里不会有它"
