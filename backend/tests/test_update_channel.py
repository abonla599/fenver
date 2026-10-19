"""`/v1/update/info`：壳「检查更新」的数据源（v0.34 起是**自托管清单**，不再是 GitHub）。

这条端点存在的理由是 2026-09-23 那次真实事故：壳直连 api.github.com 下载，被 ROM
里的下载器（迅雷通道）劫持成"未命名"残包。修法是把出网这一段收进这台服务器。
2026-10 又赶上 `abonla599` 账号被停用——GitHub 那条链整条死掉，于是取数源再换一次，
改读一份我们自己托管的清单。这里钉的是换源后**一条都不许松**的纪律：

1. 返回形状不许变：壳里 `ReleasePlan` 拿到的那份 JSON，字段与换源前逐字一致
   （壳装进手机就改不动了，形状变了 = 手里那批手机永远收不到更新）；
2. 摘要的取用规矩：只认清单里那一格、且必须与**磁盘上那个包的字节**对得上；
3. 读不出来时不许装出"一切正常"：502 带理由，绝不回一份能让壳误判"已是最新"的 200；
4. 免鉴权的代价与缓存：它照样会读盘，所以与卡片端点共用同一份快照（今天 30 秒一换，
   见 `releases.CACHE_SECONDS`）。
"""
import asyncio
import getpass
import os
import re
import time
from pathlib import Path

import httpx
import pytest

from app.core import apk_cache, releases
from tests.test_release_probe import (NAME, PKG, SHA, VERSION, make_manifest, prime,
                                      write_manifest)

# 一个一眼能认出是哪一行的坏摘要（与真摘要不同，用来演"清单与盘上的字节对不上"）。
WRONG_DIGEST = "b" * 64
SELF_URL = "https://ai.fenever.xyz/site/android.apk"
REPO_ROOT = Path(releases.__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """每一用例一台"刚起来的机器"：清单与 APK 缓存都指进 tmp，进程内记账清零。"""
    monkeypatch.setenv("APK_CACHE_DIR", str(tmp_path / "apk"))
    runtime = tmp_path / "runtime" / "version.json"
    seed = tmp_path / "seed" / "version.json"
    monkeypatch.setattr(releases, "runtime_manifest_path", lambda: str(runtime))
    monkeypatch.setattr(releases, "seed_manifest_path", lambda: str(seed))
    apk_cache.reset_for_tests()
    releases.reset_for_tests()
    yield runtime, seed
    apk_cache.reset_for_tests()
    releases.reset_for_tests()


def _assets(body):
    return {a["name"]: a for a in body["assets"]}


# ---------- 1. 形状：壳读的字段一个都不能变 ----------

def test_info_returns_the_shape_the_shell_reads(client, enforced):
    """透传的目标形状：顶层 tag_name + apk_sha256，assets[] 里 name/size/内容类型/下载地址。

    壳 `ReleasePlan.decide` / `pickAsset` 真正读的就是这几格（判据见
    android-native 那份 ReleasePlan.java 的 parse 与 pickAsset）：

    * `tag_name` —— 归一成版本号，与当前版本比大小（三态的分水岭）；
    * `assets[].name` —— 必须精确等于 `fenver-<version>.apk`（新名）或旧名别名；
    * `assets[].size` —— 弹出的"约 x MB"；
    * `assets[].browser_download_url` —— 过壳那条同源白名单（自家出口 /site/android.apk）；
    * 顶层 `apk_sha256` —— 下完字节对账；缺/坏 = 壳拒绝安装。

    `draft`/`prerelease` 是"为真才拦"，所以合成的那份**不放**这两格。
    """
    prime()
    out = client.get("/v1/update/info", headers={"host": "ai.fenever.xyz"})
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["tag_name"] == "v" + VERSION
    assert body["apk_sha256"] == SHA
    assert not body.get("draft") and not body.get("prerelease")

    got = _assets(body)
    assert NAME in got, f"新名那一枚不见了：{sorted(got)}"
    fresh = got[NAME]
    assert fresh["size"] == len(PKG)
    assert fresh["content_type"] == "application/vnd.android.package-archive"
    assert fresh["browser_download_url"] == SELF_URL
    # 老壳（0.23.x）只认旧名：来源合格时服务端现合成一枚同地址的旧名条目。
    legacy = got["ai-assistant-native-" + VERSION + ".apk"]
    assert legacy["browser_download_url"] == SELF_URL
    assert legacy["size"] == len(PKG), "合成那枚的体积没跟新名对上"


def test_the_top_level_shape_is_exactly_what_we_synthesize(client, enforced):
    """顶层就三格、资产就那几格：多写了什么都不是壳要的，少了哪一格壳就收不到更新。"""
    prime()
    body = client.get("/v1/update/info", headers={"host": "ai.fenever.xyz"}).json()
    assert set(body) == {"tag_name", "assets", "apk_sha256"}, sorted(body)
    for asset in body["assets"]:
        assert set(asset) == {"name", "size", "content_type", "browser_download_url"}, sorted(asset)


def test_the_committed_seed_manifest_has_the_documented_shape():
    """仓库里那份**打包内种子**必须自带完整的形状（它随 EXE 走，是换包后第一份数据源）。"""
    import json
    seed = REPO_ROOT / "backend" / "app" / "web" / "site" / "version.json"
    assert seed.is_file(), "打包内种子不见了：冻结版将读不到任何版本清单"
    data = json.loads(seed.read_text(encoding="utf-8"))
    version = str(data["version"])
    assert releases._VERSION_SHAPE_RE.match(version), data["version"]
    assert data["apk_name"] == f"fenver-{version}.apk"
    assert releases._MANIFEST_SHA_RE.match(data["sha256"]), data["sha256"]
    assert isinstance(data["size"], int) and 0 < data["size"] <= releases.APK_MAX_BYTES
    assert releases._is_self_hosted_url(data["url"]), data["url"]
    assert "changelog" in data, "清单要带更新日志锚点这一格（可空），缺了页面就没处跳"


def test_the_seed_versions_digest_is_the_one_we_really_have_on_disk():
    """种子里写的摘要必须就是磁盘上那个包的真摘要——这是"初始内容 0.33.1"的真实凭据。

    换包前最危险的一步是"清单先写新版本、包还没放上去"：手机于是看到一个下不到的版本。
    这条锁把"清单里的摘要 == 盘上那个文件的摘要"钉死，改清单而没换包当场红。
    """
    import hashlib
    import json
    seed = REPO_ROOT / "backend" / "app" / "web" / "site" / "version.json"
    data = json.loads(seed.read_text(encoding="utf-8"))
    apk = REPO_ROOT / "data" / "apk" / f"{data['version']}.apk"
    if not apk.is_file():
        pytest.skip("本机没有那一版的包（CI 干净检出里没有 data/）")
    digest = hashlib.sha256(apk.read_bytes()).hexdigest()
    assert digest == data["sha256"], "种子写的摘要与 data/apk 里那份对不上"

    assert data["size"] == apk.stat().st_size, "种子写的体积与盘上那份对不上"


# ---------- 2. 摘要一致性（自托管特有的一格判据） ----------

def test_the_manifest_digest_must_match_the_bytes_on_disk(client, enforced):
    """清单写了一个摘要、盘上是另一串字节：按"读不出来"处理，502 带理由。

    这是自托管特有的一格：过去"这份字节是不是发布的那一串"由 GitHub Release 正文
    背书，现在清单自己就是背书人——背错了（写错、包被换掉）必须当场拒，而不是把
    一串对不上号的字节端出去。
    """
    apk_cache.store(VERSION, PKG, SHA)                     # 盘上这份是真的
    write_manifest(releases.runtime_manifest_path(), make_manifest(sha256=WRONG_DIGEST))
    out = client.get("/v1/update/info")
    assert out.status_code == 502, f"摘要对不上却回了 {out.status_code}"
    detail = out.json()["detail"]
    assert "问不到发布信息" in detail and "对不上" in detail, detail


def test_a_manifest_whose_apk_is_not_on_disk_is_a_502(client, enforced):
    """盘上没有那个包：清单说得再漂亮也没有可端出去的字节，照样 502。"""
    write_manifest(releases.runtime_manifest_path(), make_manifest())   # 只写清单，不落包
    out = client.get("/v1/update/info")
    assert out.status_code == 502 and "对不上" in out.json()["detail"]


# ---------- 3. 拉不到时的表态 ----------

def test_an_unreadable_manifest_is_a_502_with_a_reason_not_a_silent_ok(client, enforced):
    """一份清单都读不到：502 带理由——壳「检查更新」必须把"读不出来"说出来。"""
    out = client.get("/v1/update/info")
    assert out.status_code == 502, f"读不到却回了 {out.status_code}：壳会把『问不到』读成别的什么"
    assert "问不到发布信息" in out.json()["detail"]


def test_a_broken_manifest_is_a_502_not_a_fallback_to_the_seed(client, enforced, isolated):
    """运行时清单**存在但坏掉**时不回落种子：那是"我读不出来"，不是"退回上一版"。

    悄悄换回种子等于用一个旧版本冒充最新——本仓反复防的那件事。
    """
    _runtime, seed = isolated
    write_manifest(releases.runtime_manifest_path(), "{ this is not json")
    write_manifest(seed, make_manifest())                  # 种子里有一份好的
    out = client.get("/v1/update/info")
    assert out.status_code == 502 and "运行时清单" in out.json()["detail"]


# ---------- 4. AC-4 后半个词：旧快照也不许冒充"最新" ----------

def test_a_stale_snapshot_never_masquerades_as_the_latest(client, enforced):
    """读不到时手里那份旧快照不许当最新发出去：否则"我读不到"被伪装成"你已是最新"。"""
    prime()
    assert client.get("/v1/update/info").status_code == 200   # 先攒出一份好快照
    # 时间旅行：这份快照"放旧"了——等价于清单已经超过一个缓存期没读到
    releases._payload_at -= releases.CACHE_SECONDS + 1
    os.remove(releases.runtime_manifest_path())               # 清单没了，重读取不到
    out = client.get("/v1/update/info")
    assert out.status_code == 502, \
        f"旧快照被当最新发出去了（{out.status_code}）：壳会把过期数据当成『已是最新』"
    detail = out.json()["detail"]
    assert "不把旧快照冒充最新" in detail, f"理由要说清是过期不是没货：{detail!r}"


def test_the_manifest_recovers_and_the_fresh_answer_is_served_again(client, enforced):
    """清单修好之后，下一次成功读取必须把 200 与正确形状带回来，不留"永久 502"。"""
    assert client.get("/v1/update/info").status_code == 502
    prime()
    releases.reset_for_tests()
    out = client.get("/v1/update/info", headers={"host": "ai.fenever.xyz"})
    assert out.status_code == 200, "清单恢复后端点没能自愈"
    assert out.json()["tag_name"] == "v" + VERSION
    assert out.json()["apk_sha256"] == SHA


# ---------- 5. 免鉴权、缓存、同步 def ----------

def test_the_door_is_open_without_credentials(client, enforced):
    """没登录的人点了「检查更新」也得能问——这条在 PUBLIC_PATHS 里点了名。

    判据用真请求且必须在 enforced 下：disabled 里人人放行，那条断言等于没测。
    """
    prime()
    out = client.get("/v1/update/info")
    assert out.status_code not in (401, 403), "免鉴权名单改了却没改这条，门就悄悄关了"


def test_twenty_opens_still_mean_one_read(client, enforced, monkeypatch):
    """与卡片端点共用同一份快照：二十次点开只许读一次清单。

    这条是免鉴权换来的放大面的全部防线——多一处读盘路径不多缓存，等于把一个 9MB
    摘要计算挂在每个访客身上。
    """
    prime()
    reads = []
    real = releases._read_manifest_file
    monkeypatch.setattr(releases, "_read_manifest_file",
                        lambda p: (reads.append(p), real(p))[1])
    for _ in range(20):
        assert client.get("/v1/update/info").status_code == 200
    assert len(reads) == 1, f"读了 {len(reads)} 次：缓存没接住这条新端点"


def test_the_endpoint_is_a_sync_def_so_the_loop_stays_free():
    """与 /v1/release/latest 同一条课：会读盘（历史上会出网）的端点写成 async 就是冻住所有人。

    test_event_loop_not_blocked 的名单里也点了这条的名字；这一处再钉一遍是因为
    那份名单丢一条时那边只会红在参数化里，看不出是"新端点没登记"。
    """
    import inspect

    from app import main
    assert not inspect.iscoroutinefunction(main.update_info)


# ---------- 6. 优先级：运行时清单覆盖打包内种子 ----------

def test_the_runtime_manifest_overrides_the_packaged_seed(isolated):
    """两份都在时用运行时那份：发新版只改 data/version.json，不必重建 EXE。"""
    _runtime, seed = isolated
    write_manifest(seed, make_manifest())                                  # 种子说 0.33.1
    apk_cache.store(VERSION, PKG, SHA)
    data, why, used = releases.load_manifest()
    assert why == "" and data["version"] == VERSION and used.endswith("version.json")

    # 运行时那份说一个别的版本（APK 也换成它对应的字节）——它必须压过种子
    other = make_manifest(version="0.34.0", apk_name="fenver-0.34.0.apk",
                          sha256=SHA, url=SELF_URL)
    write_manifest(releases.runtime_manifest_path(), other)
    apk_cache.store("0.34.0", PKG, SHA)
    data2, why2, used2 = releases.load_manifest()
    assert why2 == "" and data2["version"] == "0.34.0", "运行时清单没有压过种子"
    assert used2 == releases.runtime_manifest_path()


def test_the_seed_is_used_when_no_runtime_manifest_exists(isolated):
    _runtime, seed = isolated
    write_manifest(seed, make_manifest())
    apk_cache.store(VERSION, PKG, SHA)
    data, why, used = releases.load_manifest()
    assert why == "" and data["version"] == VERSION and used == str(seed)


# ---------- 7. 消费方：download_plan / 官网退路 ----------

def test_the_fallback_target_is_our_own_site_not_github():
    """拿不到包时把人送回官网（首页有「安卓版」按钮），不再送回已失效的 GitHub 发布页。"""
    page = releases.releases_page()
    assert page.startswith("https://ai.fenever.xyz"), page
    assert "github.com" not in page


def test_the_site_proxy_gets_the_manifest_url_not_the_origin_rewrite(client, enforced):
    """官网那颗按钮要的始终是清单里那条地址，绝不被改成这次请求来源的那一份。

    这是这条链上唯一能把自己绕死的形状：`/site/android.apk` 代取时若照着被改过的快照
    去找地址，它就会朝自己发请求——症状是下载转圈到超时，而每一步都"合法"。
    """
    prime()
    client.get("/v1/update/info", headers={"host": "ai.fenever.xyz"})
    plan, why = releases.download_plan()
    assert plan, why
    assert plan["url"] == SELF_URL and plan["name"] == NAME
    assert plan["sha256"] == SHA


def test_an_origin_the_shell_would_not_trust_leaves_the_manifest_url_alone(client, enforced):
    """来源不合格（带端口）时地址保持清单原样：换成一个壳不信的地址比不改更糟。"""
    prime()
    body = client.get("/v1/update/info", headers={"host": "ai.fenever.xyz:8443"}).json()
    assert body["assets"][0]["browser_download_url"] == SELF_URL


@pytest.mark.parametrize("origin,expect", [
    ("https://ai.fenever.xyz", "https://ai.fenever.xyz/site/android.apk"),
    ("https://www.fenever.xyz", "https://www.fenever.xyz/site/android.apk"),
    ("https://ai.fenever.xyz/", "https://ai.fenever.xyz/site/android.apk"),
    ("", ""), ("http://ai.fenever.xyz", ""), ("https://ai.fenever.xyz:8443", ""),
    ("https://ai.fenever.xyz/extra", ""), ("https://user@ai.fenever.xyz", ""),
    # 形状合格、却根本不是我们的域：这一组是 2026-10-08 安全收口补的那一层（判据见
    # self_hosted_apk_url）。形状取自壳那边 ReleasePlanTest 的
    # everyDetourFromTheExactSameOriginPathIsRefused——两端认的"别家"必须是同一批。
    ("https://evil.example", ""), ("https://evil.example/", ""),
    ("https://ai.fenever.xyz.eil.com", ""), ("https://eil.com/site/android.apk", ""),
    ("https://fenever.xyz", ""), ("https://www.evil.example", ""),
    ("https://ai.feverov.xyz", ""), ("https://testserver", ""),
])
def test_the_self_hosted_origin_is_accepted_only_in_the_shape_the_shell_trusts(origin, expect):
    """自家出口地址的合格线：https + 纯主机 + **自家那两个域** + 那一条固定路径。

    域这一层原先只在清单校验那条路上生效（`_manifest_ok` → `_is_self_hosted_url`），
    换地址这条路上没有——而这条路的输入是请求自己的 Host 头。少这一句，一个
    `Host: evil.example` 就能把发给手机的下载地址换成别人家的 https 主机。
    """
    assert releases.self_hosted_apk_url(origin) == expect


def test_a_qualified_origin_never_lands_outside_the_self_hosted_domains():
    """换出去的地址只有两种可能：空串（什么都不换），或落在 `SELF_HOST_DOMAINS` 上。

    这条不写死任何域名：判据直接取那个集合本身。抄一份清单进测试，将来加第三个自家域时
    这把锁会跟着漂——"实现与测试各一份真相"正是本仓反复在防的事。
    """
    origins = ["https://ai.fenever.xyz", "https://www.fenever.xyz", "https://evil.example",
               "https://evil.example/", "https://ai.fenever.xyz.eil.com", "https://fenever.xyz",
               "https://ai.fenever.xyz:8443", "https://user@ai.fenever.xyz",
               "http://localhost:8000", "https://localhost", "", "not a url"]
    for origin in origins:
        url = releases.self_hosted_apk_url(origin)
        assert url == "" or releases._is_self_hosted_url(url), \
            f"{origin!r} 换出去一个不在 SELF_HOST_DOMAINS 上的地址：{url!r}"


def test_a_foreign_host_header_cannot_move_the_download_address_off_our_own_domains(client, enforced):
    """`Host: evil.example` 打 `/v1/update/info`：端给手机的地址仍只能是清单里那一条自家的。

    端到端那一半（上面两条只验函数）。`main._self_origin` 取的是 Host 头 +
    X-Forwarded-Proto，两个都由来客说了算，而 `_point_self_hosted_assets` 照着它把清单里
    那条自家地址改掉——所以判据是"改完仍然只可能是自家域"，并且不合格来源一个字节不动
    （保持 SELF_URL）。
    """
    prime()
    for host in ("evil.example", "evil.example:443", "ai.fenever.xyz.eil.com",
                 "fenever.xyz", "www.evil.example", "testserver"):
        body = client.get("/v1/update/info", headers={"host": host}).json()
        assert body["assets"], host
        for asset in body["assets"]:
            got = asset["browser_download_url"]
            assert releases._is_self_hosted_url(got), f"{host} 把地址指到了别家：{got}"
            assert got == SELF_URL, f"{host} 换掉了清单里那条自家地址：{got}"


def test_the_two_self_hosted_domains_still_get_their_origin_specific_url(client, enforced):
    """反向不许漂：自家两个域各自来时照旧换成同源的那一枚（保护的是老壳的同源判据）。

    补白名单最容易顺手干坏事的地方就在这儿——把 www 或大小写写法一起判死，症状不是报错，
    是那批把服务器配成 www.fenever.xyz 的手机从此收不到这一版。域名的 DNS 大小写不敏感，
    而壳那条同源判据比的是字面相等，所以这里按小写形比对、把来客写的大小写原样还回去。
    """
    prime()
    for host in ("ai.fenever.xyz", "www.fenever.xyz", "WWW.FENEVER.XYZ"):
        body = client.get("/v1/update/info", headers={"host": host}).json()
        assert body["assets"], host
        for asset in body["assets"]:
            assert asset["browser_download_url"].lower() == \
                "https://" + host.lower() + releases.SELF_APK_PATH, \
                f"{host} 的来源没被换成同源地址：{asset['browser_download_url']}"


def test_the_self_hosted_apk_path_is_the_same_string_in_the_server_the_route_and_the_client():
    """三处同名锁：换出去的地址必须是客户端写死的那条路径、也必须是真注册了的那条路由。

    少一个字符的症状不是报错，是老壳对着一句"看起来是下载地址"的东西回 UNUSABLE。
    2026-10-08 前这里对两个模块各数一遍（WebView 壳 + 原生）；壳退役后只剩一份，
    但判据不降级：路径必须真的是文件里那一行常量，文件必须存在（空转的锁长得和守住时一样）。
    """
    router = (REPO_ROOT / "backend" / "app" / "web" / "web_router.py").read_text(encoding="utf-8")
    assert f'@app.get("{releases.SELF_APK_PATH}"' in router, "自家出口没真注册在这条路径上"
    java_path = (REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" /
                 "fenever" / "assistant" / "core" / "ReleasePlan.java")
    assert java_path.is_file(), f"上线客户端的判断核不见了：{java_path}"
    java = java_path.read_text(encoding="utf-8")
    assert f'SELF_APK_PATH = "{releases.SELF_APK_PATH}"' in java, "客户端的那条路径对不上"


def test_a_missing_apk_is_a_302_not_an_empty_200(client, enforced, monkeypatch):
    """清单在、盘上没有包：官网上那颗按钮退 302 回官网，绝不回一个 200 空文件。

    空文件在手机上长成"下载完成了，点开却没反应"。
    """
    write_manifest(releases.runtime_manifest_path(), make_manifest())
    res = client.get("/site/android.apk", follow_redirects=False)
    assert res.status_code in (302, 307), res.status_code
    assert res.headers["location"] == releases.releases_page()
    assert not res.content


# ---------- 8. 校验值这条契约横跨三个语言，形状只能有一份 ----------

def test_the_digest_contract_is_the_same_shape_in_the_workflow_the_backend_and_the_shell():
    """`APK-SHA256: <64 位小写十六进制>` 由发布流水线写、后端解析、壳核对。

    三边各认各的形状时，漂移的表现不是报错而是【永远拒装】。这份形状仍是
    GitHub 那条链与壳对账的依据，所以一字不动地继续钉着。
    """
    wf = (REPO_ROOT / ".github" / "workflows" / "release-apk.yml").read_text(encoding="utf-8")
    assert 'APK-SHA256: %s' in wf and 'sha256sum "$file"' in wf, \
        "发布流不再写（或不再算）那行校验值了"
    assert r"[0-9a-f]\{64\}" in wf, \
        "发布流自检的 grep 形状不再是 64 位小写十六进制"

    assert releases.APK_SHA256_RE.pattern == r"^APK-SHA256: ([0-9a-f]{64})$", \
        "后端解析的形状漂了，要和上面工作流写的那一份一起改"
    assert releases._MANIFEST_SHA_RE.pattern == r"^[0-9a-f]{64}$", \
        "清单摘要的形状也要与上面同源（64 位小写十六进制）"

    plan = (REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever"
            / "assistant" / "core" / "ReleasePlan.java").read_text(encoding="utf-8")
    assert '"apk_sha256"' in plan, "壳不再从透传 JSON 读这个字段了"
    assert "length() != 64" in plan, "壳认的长度不再是 64？"
    assert "c < 'a' || c > 'f'" in plan, "壳认的字符集不再是小写十六进制？"


# ---------- 9. 免鉴权那两条端点不许说这台机器长什么样 ----------

# `/v1/update/info` 与 `/v1/release/latest` 都在 `authz.PUBLIC_PATHS` 里点了名
# （登录之前就要能检查更新），而"清单写的安装包不可用"那两句理由一路拼到 502 的
# `detail` 与卡片的 `reason` 上：每一次手机检查更新，都是任何路人在读这台机器的
# 磁盘判据。这里断言的是**整份响应体**，不是某一句文案。
REPO_DIR_NAME = REPO_ROOT.name
# 盘符路径 = 单字母 + `:` + 分隔符。豁免只认 URL scheme 那一形（`scheme 字符` 紧跟
# `单字母 + ://`）：这两条端点的正常载荷里就带着 https 地址，把 `s://` 当盘符会让判据恒红，
# 而 `at-C:\Users\a`、`v1C:\data\x` 这种粘着 `+ - .` 或字母数字的路径必须仍然算路径——只按
# "前一个字符是哪一类"划界时探测器就瞎在生产网瞎的同一处。两支并起来才与生产网
# `core.pathmask._WIN_PATH` 的一支负向前瞻同判，同判由 `test_pathmask.py` 的绑定锁按源码点名守住。
_DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9+.-])(?P<drive>[A-Za-z]):[\\/]"
                         r"|[A-Za-z0-9+.-][A-Za-z]:(?!//)[\\/]")


def _anonymous_get(*paths):
    """真·匿名（不带 Authorization、不带会话 Cookie）连打两条免鉴权端点。

    用 httpx 的 ASGITransport 而不是 `TestClient`（本仓跨请求证据的既有写法）：这两条
    共用 `releases` 的模块级快照，判据要的正是"第二条读到的就是第一趟攒下的那份理由"，
    而 `TestClient` 每请求另开一个 portal，跨请求那一半根本观察不到。
    """
    from app.main import app

    async def go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="https://ai.fenever.xyz", timeout=60.0) as c:
            return [await c.get(p) for p in paths]
    return asyncio.run(go())


def _assert_no_machine_shape(responses, cache_root, where):
    for r in responses:
        assert r.status_code != 401, f"{where}：免鉴权端点索要凭据了，这条锁没在测匿名"
        body = r.text
        hit = _DRIVE_PATH.search(body)
        assert hit is None, f"{where}：响应体里出现盘符路径 {hit.group(0)!r} → {body}"
        for forbidden in (cache_root, getpass.getuser(), REPO_DIR_NAME):
            assert forbidden not in body, f"{where}：响应体回显了 {forbidden!r} → {body}"


def test_anonymous_missing_package_answer_carries_no_local_path(enforced, tmp_path):
    """清单就位、盘上没有那串字节：匿名调用者拿到的两份东西里都没有磁盘位置。

    两条端点一起打：502 的 `detail`（`releases.latest_release_manifest` 那条理由）与
    卡片的 `reason`（`releases.probe` 的失败分支），背后是同一句 `cached_file_state`。
    """
    write_manifest(releases.runtime_manifest_path(), make_manifest())
    responses = _anonymous_get("/v1/update/info", "/v1/release/latest?have=0.16")
    info, latest = responses
    assert info.status_code == 502, info.text
    assert latest.status_code == 200 and latest.json()["ok"] is False, latest.text
    _assert_no_machine_shape(responses, str(tmp_path / "apk"), "缺包")


def test_anonymous_digest_mismatch_answer_carries_no_local_path(enforced, tmp_path):
    """盘上有包但摘要对不上：同一句理由的第二格，同样只许说包不许说机器。"""
    apk_cache.store(VERSION, PKG, SHA)
    write_manifest(releases.runtime_manifest_path(), make_manifest(sha256=WRONG_DIGEST))
    responses = _anonymous_get("/v1/update/info", "/v1/release/latest?have=0.16")
    info, latest = responses
    assert info.status_code == 502, info.text
    assert latest.json()["ok"] is False, latest.text
    _assert_no_machine_shape(responses, str(tmp_path / "apk"), "摘要不符")


def test_the_refusal_still_tells_which_package_and_what_is_missing(enforced, tmp_path):
    """反向证据：隐去位置之后运维仍然查得到"是哪一版、缺的是什么"——不是把句子吞掉。

    读的字段就两格：502 的 `detail`（含包名与问题种类）与卡片的 `latest`（版本号）。
    把拒绝理由整句删短成"详见日志"会红在这里，而那正是 #112 出口层拒绝的那种做法。
    """
    write_manifest(releases.runtime_manifest_path(), make_manifest())
    detail = _anonymous_get("/v1/update/info", "/v1/release/latest?have=0.16")[0].json()["detail"]
    assert VERSION + ".apk" in detail, f"看不出缺的是哪个包：{detail}"
    assert "盘上没有这一版的字节" in detail, detail
    assert SHA[:8] in detail, f"清单期望的校验值前缀没了：{detail}"

    apk_cache.store(VERSION, PKG, SHA)
    releases.reset_for_tests()
    write_manifest(releases.runtime_manifest_path(), make_manifest(sha256=WRONG_DIGEST))
    info, latest = _anonymous_get("/v1/update/info", "/v1/release/latest?have=0.16")
    detail = info.json()["detail"]
    assert VERSION + ".apk" in detail and "摘要" in detail, detail
    assert WRONG_DIGEST[:8] in detail and SHA[:8] in detail, f"两句前缀至少要在一处读得出：{detail}"
    # 卡片那一头读的是 `reason` 字段：失败时 `latest` 是空串（三态纪律），所以"缺的是
    # 哪一版的包"必须仍然写在那句话里，不能只剩"详见日志"。
    card = latest.json()
    assert card["ok"] is False and card["has_update"] is None, card
    assert VERSION + ".apk" in card["reason"], card["reason"]


def test_the_exit_net_masks_a_path_in_text_we_did_not_write(enforced, tmp_path, monkeypatch):
    """第二层守的是"措辞不在我们手里"的那半：磁盘判据将来被别的来源填进路径时也要盖住。

    这里把一句**带盘符路径**的理由直接摆进 `cached_file_state` 的返回值（模拟第三方
    异常原文或后来新增的判据），断言公开端点上只剩明写的〈路径已隐去〉，句子其余部分
    照读——整句吞掉是另一种 bug（见 #112 那条拒绝 logsanitizer 登记数据根的理由）。
    """
    bs = chr(92)
    windows = "C:" + bs + "Users" + bs + "someone" + bs + "proj" + bs + "data" + bs + "apk"
    posix = "/home/someone/proj/data/apk"
    monkeypatch.setattr(apk_cache, "cached_file_state",
                        lambda v, s: ("", f"盘上没有这一版的字节（{windows}，另有 {posix}）"))
    write_manifest(releases.runtime_manifest_path(), make_manifest())
    responses = _anonymous_get("/v1/update/info", "/v1/release/latest?have=0.16")
    info, latest = responses
    assert info.status_code == 502 and latest.status_code == 200, info.text
    for r in (info, latest):
        assert windows not in r.text and posix not in r.text, r.text
    assert "〈路径已隐去〉" in info.json()["detail"], info.text
    assert "问不到发布信息" in info.json()["detail"], "整句被吞了：该隐去的只有路径"
    assert "不把对不上号的字节端出去" in info.json()["detail"], info.text
