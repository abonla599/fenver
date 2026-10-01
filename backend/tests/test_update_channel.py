"""`/v1/update/info`：壳「检查更新」的透传端点与它那份校验值。

这条端点存在的理由是 2026-09-23 那次真实事故：壳直连 api.github.com 下载，被 ROM
里的下载器（迅雷通道）劫持成"未命名"残包，人拿着半截文件去装，报安装失败。修法是
把出网这一段收进这台服务器——所以这里钉的是：

1. 透传不改写：壳里的 `ReleasePlan` 拿到的必须还是那份 GitHub 形状的 JSON，
   判断（三态/资产名/URL 白名单）只许有壳里那一份真相；
2. 校验值的取用规矩：正文里那行 `APK-SHA256:` 是唯一来源，形状不对等于没有；
3. 拉不到时不许装出"一切正常"：502 带理由，绝不回一份能让壳误判"已是最新"的 200；
4. 免鉴权的代价与缓存：它照样替调用方出网，所以与卡片端点共用同一份 10 分钟快照。
"""
import urllib.error

import pytest

from app.core import releases
from tests.test_release_probe import GOOD, _Urlopen

DIGEST = "a" * 63 + "f"          # 64 位小写十六进制，一眼能认出是哪一行


def _release(body_text):
    rel = dict(GOOD)
    rel["body"] = body_text
    return rel


@pytest.fixture(autouse=True)
def cold_cache():
    releases.reset_for_tests()
    yield
    releases.reset_for_tests()


# ---------- 透传 + 校验值 ----------

def test_info_passes_the_release_json_through_with_the_digest(client, enforced, monkeypatch):
    fake = _Urlopen(_release(f"本版修了点东西。\n\n---\n\nAPK-SHA256: {DIGEST}\n"))
    monkeypatch.setattr(releases.urllib.request, "urlopen", fake)
    out = client.get("/v1/update/info")
    assert out.status_code == 200, out.text
    body = out.json()
    # 原样透传：壳里 ReleasePlan 认的字段一个都不能被服务端顺手改名
    assert body["tag_name"] == "v0.18"
    assert body["assets"][0]["browser_download_url"].startswith("https://objects.example/")
    assert body["apk_sha256"] == DIGEST


def test_a_release_without_the_marker_reports_no_digest_not_a_guess(client, enforced, monkeypatch):
    fake = _Urlopen(_release("旧版发布，正文里还没有那行校验值"))
    monkeypatch.setattr(releases.urllib.request, "urlopen", fake)
    out = client.get("/v1/update/info")
    assert out.status_code == 200
    assert out.json()["apk_sha256"] == "", "没有校验值时必须给空串，壳据此拒绝下载"


@pytest.mark.parametrize("junk", [
    f"APK-SHA256: {'A' * 64}",                 # 大写：发布流程写的是小写，不认混着来的
    f"APK-SHA256: {'a' * 63}",                 # 短一位
    f"  APK-SHA256: {DIGEST}",                 # 行首缩进（引用块里的样子货）
    f"APK-SHA256:{DIGEST}",                    # 冒号后没空格
])
def test_a_malformed_marker_line_is_not_a_digest(client, enforced, monkeypatch, junk):
    # 宁可壳拒装，不许半对的值被当成可信摘要。
    releases.reset_for_tests()
    monkeypatch.setattr(releases.urllib.request, "urlopen", _Urlopen(_release(junk)))
    out = client.get("/v1/update/info")
    assert out.status_code == 200
    assert out.json()["apk_sha256"] == "", f"这行不该被认成校验值：{junk!r}"


# ---------- 拉不到时的表态 ----------

def test_unreachable_github_is_a_502_with_a_reason_not_a_silent_ok(client, enforced, monkeypatch):
    monkeypatch.setattr(releases.urllib.request, "urlopen",
                        _Urlopen(urllib.error.URLError("no route")))
    out = client.get("/v1/update/info")
    assert out.status_code == 502, f"拉不到却回了 {out.status_code}：壳会把『问不到』读成别的什么"
    assert "问不到发布信息" in out.json()["detail"]


# ---------- AC-4 后半个词：旧快照也不许冒充"最新" ----------
# v0.23 拆解清单 T1.3 三查（sha256 注入 / 10 分钟缓存 / 5xx 带理由）里，前两项的
# 判据在上面的透传与缓存用例；这一组补的是曾经真实存在的洞：拉取失败时缓存节流的
# 时间照样前进，于是"GitHub 断供后"手里的旧快照会在整个断供期被当成最新用 200 发出——
# 症状不是报错，是老用户在壳上看到一个不存在的"已最新"。新鲜度从此看 `_payload_at`
# （最近一次**成功**），不再只看 `_fetched_at`（最近一次**尝试**）。

def test_a_stale_snapshot_never_masquerades_as_the_latest(client, enforced, monkeypatch):
    monkeypatch.setattr(releases.urllib.request, "urlopen",
                        _Urlopen(_release(f"本版修了点东西。\n\nAPK-SHA256: {DIGEST}\n")))
    assert client.get("/v1/update/info").status_code == 200   # 先攒出一份好快照
    # 时间旅行：这份快照"放旧"了——等价于 GitHub 已断供超过一个缓存期
    releases._payload_at -= releases.CACHE_SECONDS + 1
    monkeypatch.setattr(releases.urllib.request, "urlopen",
                        _Urlopen(urllib.error.URLError("github down")))
    out = client.get("/v1/update/info")
    assert out.status_code == 502, \
        f"旧快照被当最新发出去了（{out.status_code}）：壳会把过期数据当成『已是最新』"
    detail = out.json()["detail"]
    assert "不把旧快照冒充最新" in detail, f"理由要说清是过期不是没货：{detail!r}"


def test_github_recovers_and_the_fresh_release_is_served_again(client, enforced, monkeypatch):
    monkeypatch.setattr(releases.urllib.request, "urlopen",
                        _Urlopen(_release(f"APK-SHA256: {DIGEST}")))
    assert client.get("/v1/update/info").status_code == 200
    releases._payload_at -= releases.CACHE_SECONDS + 1
    monkeypatch.setattr(releases.urllib.request, "urlopen",
                        _Urlopen(urllib.error.URLError("github down")))
    assert client.get("/v1/update/info").status_code == 502
    # 恢复：下一次成功读取必须把 200 与正确校验值带回来，不留"永久 502"的坏状态
    newer = _release(f"APK-SHA256: {'b' * 64}")
    newer["tag_name"] = "v0.19"
    monkeypatch.setattr(releases.urllib.request, "urlopen", _Urlopen(newer))
    out = client.get("/v1/update/info")
    assert out.status_code == 200, "GitHub 恢复后端点没能自愈"
    assert out.json()["tag_name"] == "v0.19"
    assert out.json()["apk_sha256"] == "b" * 64


def test_a_failed_attempt_inside_the_window_still_serves_the_fresh_cache(client, enforced, monkeypatch):
    """收紧只针对**过期**的快照：缓存期内（<10 分钟）偶发一次失败不该把好消息扣住——
    那正是"一次拉取全员共享"要买的抗抖性，两条断言各钉一头，不许互相越界。"""
    fake_ok = _Urlopen(_release(f"APK-SHA256: {DIGEST}"))
    monkeypatch.setattr(releases.urllib.request, "urlopen", fake_ok)
    assert client.get("/v1/update/info").status_code == 200
    monkeypatch.setattr(releases.urllib.request, "urlopen",
                        _Urlopen(urllib.error.URLError("flaky")))
    out = client.get("/v1/update/info")
    assert out.status_code == 200 and out.json()["apk_sha256"] == DIGEST
    assert len(fake_ok.calls) == 1, "缓存期内的第二枪不该出网"


# ---------- 免鉴权与缓存 ----------

def test_the_door_is_open_without_credentials(client, enforced, monkeypatch):
    """没登录的人点了「检查更新」也得能问——这条在 PUBLIC_PATHS 里点了名。

    判据用真请求且必须在 enforced 下：disabled 里人人放行，那条断言等于没测。
    名单与中间件是两处代码（名单写没写是一回事，放不放行是另一回事），
    精确名单的锁在 test_route_auth_contract，这里补的是"门真的开着"。
    """
    monkeypatch.setattr(releases.urllib.request, "urlopen", _Urlopen(_release("x")))
    out = client.get("/v1/update/info")
    assert out.status_code not in (401, 403), "免鉴权名单改了却没改这条，门就悄悄关了"


def test_twenty_opens_still_mean_one_trip_to_github(client, enforced, monkeypatch):
    """与卡片端点共用同一份快照：二十次点开只许出网一次。

    这条是免鉴权换来的放大面的全部防线——多一处出网路径不多缓存，等于把
    GitHub 的 60 次/小时限流挂在每个访客身上。
    """
    fake = _Urlopen(_release(f"APK-SHA256: {DIGEST}"))
    monkeypatch.setattr(releases.urllib.request, "urlopen", fake)
    for _ in range(20):
        assert client.get("/v1/update/info").status_code == 200
    assert len(fake.calls) == 1, f"出网 {len(fake.calls)} 次：缓存没接住这条新端点"


def test_the_endpoint_is_a_sync_def_so_the_loop_stays_free():
    """与 /v1/release/latest 同一条课：会出网的端点写成 async 就是冻住所有人。

    test_event_loop_not_blocked 的名单里也点了这条的名字；这一处再钉一遍是因为
    那份名单丢一条时那边只会红在参数化里，看不出是"新端点没登记"。
    """
    import inspect

    from app import main
    assert not inspect.iscoroutinefunction(main.update_info)


# ---------- 校验值这条契约横跨三个语言，形状只能有一份 ----------

def test_the_digest_contract_is_the_same_shape_in_the_workflow_the_backend_and_the_shell():
    """`APK-SHA256: <64 位小写十六进制>` 由发布流水线写、后端解析、壳核对。

    三边各认各的形状时，漂移的表现不是报错而是【永远拒装】：流水线哪天写成大写，
    后端正则认不出→ apk_sha256 恒为空→ 每一台壳都拒绝下载每一版，且每一环都"正常工作"。
    所以这里把三份形状并排钉成同一个：都是"APK-SHA256 前缀 + 64 + 小写十六进制"。
    """
    from pathlib import Path

    repo = Path(releases.__file__).resolve().parents[3]
    wf = (repo / ".github" / "workflows" / "release-apk.yml").read_text(encoding="utf-8")
    assert 'APK-SHA256: %s' in wf and 'sha256sum "$file"' in wf, \
        "发布流不再写（或不再算）那行校验值了"
    assert r"[0-9a-f]\{64\}" in wf, \
        "发布流自检的 grep 形状不再是 64 位小写十六进制"

    assert releases.APK_SHA256_RE.pattern == r"^APK-SHA256: ([0-9a-f]{64})$", \
        "后端解析的形状漂了，要和上面工作流写的那一份一起改"

    plan = (repo / "android" / "app" / "src" / "main" / "java" / "xyz" / "fenever"
            / "assistant" / "core" / "ReleasePlan.java").read_text(encoding="utf-8")
    assert '"apk_sha256"' in plan, "壳不再从透传 JSON 读这个字段了"
    assert "length() != 64" in plan, "壳认的长度不再是 64？"
    assert "c < 'a' || c > 'f'" in plan, "壳认的字符集不再是小写十六进制？"


# ---------- T1.4：mock GitHub 的剩余形状 —— 服务端一格判断都不许长出 ----------

def test_the_first_wellformed_marker_line_wins(client, enforced, monkeypatch):
    """正文里挂着好几行长得像校验值的东西：取第一条【合法】的，形状不对的直接跳过。

    search() 的语义在这里是被钉住的行为而不是实现巧合：发布流程只在末尾写一行，
    多出来的行只可能来自手改正文——第一条合法行赢，后面的花活不掺和。
    """
    fake = _Urlopen(_release(
        f"APK-SHA256: {DIGEST}\n\n有人手抄了一遍：APK-SHA256: {'b' * 64}"))
    monkeypatch.setattr(releases.urllib.request, "urlopen", fake)
    assert client.get("/v1/update/info").json()["apk_sha256"] == DIGEST
    # 首行形状不对（大写）不算数，下一个合法行才上位
    releases.reset_for_tests()
    junky = _release(f"APK-SHA256: {'A' * 64}\nAPK-SHA256: {DIGEST}")
    monkeypatch.setattr(releases.urllib.request, "urlopen", _Urlopen(junky))
    assert client.get("/v1/update/info").json()["apk_sha256"] == DIGEST


def test_a_digest_field_injected_into_the_github_json_is_overwritten_not_trusted(
        client, enforced, monkeypatch):
    """GitHub 形状的 JSON 顶层【自带】一个 apk_sha256 也不许透出去——只认正文那行重算的。

    这是透传设计唯一会漏的地方：raw 是别人家的 JSON，若服务端顺手"没有才注入"，
    一条能改发布 JSON 的通道就能伪造校验值，壳的对账闸当场变成摆设。所以这里
    是无条件覆盖：正文里没有合法行 ⇒ 空串，哪怕顶层自称有值。
    """
    rel = _release("这一版正文里没写校验值那行")
    rel["apk_sha256"] = "b" * 64                      # 伪造的顶层字段
    monkeypatch.setattr(releases.urllib.request, "urlopen", _Urlopen(rel))
    out = client.get("/v1/update/info")
    assert out.status_code == 200
    assert out.json()["apk_sha256"] == "", "自报的校验值被透出去了：对账闸被绕过"


@pytest.mark.parametrize("body_value", [None, 123, ["APK-SHA256: " + DIGEST]])
def test_a_body_that_is_not_text_yields_no_digest_without_an_exception(
        client, enforced, monkeypatch, body_value):
    """GitHub 哪天把 body 换成 null/数字/数组，这里不许 500——只能老实说"没有校验值"。"""
    rel = dict(GOOD)
    rel["body"] = body_value
    monkeypatch.setattr(releases.urllib.request, "urlopen", _Urlopen(rel))
    out = client.get("/v1/update/info")
    assert out.status_code == 200, out.text
    assert out.json()["apk_sha256"] == ""


def test_a_snapshot_without_the_raw_json_is_a_502_not_an_empty_ok(client, enforced):
    """内部状态坏了（快照里没有"原样 JSON"这一格）也要明说，不许拼一份空对象糊弄壳。

    直接喂坏 _payload：这条测的是 latest_release_manifest 自己的防御，与网络无关。
    """
    import time as _time
    releases._payload = {"version": "0.18", "url": "", "asset_name": "",
                         "asset_url": "", "size": 0}          # 缺 "raw"
    releases._fetched_at = _time.monotonic()
    releases._payload_at = _time.monotonic()
    out = client.get("/v1/update/info")
    assert out.status_code == 502
    assert "原样 JSON" in out.json()["detail"]


def test_a_top_level_list_from_github_is_a_502_with_the_fetch_reason(client, enforced, monkeypatch):
    """GitHub 换成别的顶层形状（比如哪天回了一个数组）：reason 一路带到人前，不静默。"""
    monkeypatch.setattr(releases.urllib.request, "urlopen", _Urlopen([GOOD]))
    out = client.get("/v1/update/info")
    assert out.status_code == 502
    assert "不是对象" in out.json()["detail"]


def test_draft_and_vless_tags_pass_through_untouched(client, enforced, monkeypatch):
    """draft=true、tag 不带 v——服务端一个字段都不改写：三态分辨全在壳的 ReleasePlan。

    透传层的纪律就一条：GitHub 给什么形状，壳见到什么形状（外加 apk_sha256 那一格）。
    服务端若"顺手"把 tag 补个 v 或把 draft 过滤掉，壳里钉着的判据就成了对不上号的第二真相。
    """
    rel = dict(GOOD)
    rel["tag_name"] = "0.19"                                    # 不带 v
    rel["draft"] = True
    rel["body"] = f"APK-SHA256: {DIGEST}"
    monkeypatch.setattr(releases.urllib.request, "urlopen", _Urlopen(rel))
    out = client.get("/v1/update/info")
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["tag_name"] == "0.19", "tag 被改写：壳里 normalizeTag 的判据对不上号了"
    assert body["draft"] is True, "draft 被吞：壳会把草稿版当成可装版本放行"
    assert body["apk_sha256"] == DIGEST


# ---------- 改名过渡期：旧名那枚资产换到自家出口（0.23.x 的更新链不断） ----------
# 0.23.x 及更早的壳把 GitHub 路径白名单写死成旧仓，装进手机就改不动。发布搬到
# abonla599/fenver 之后，它们读到的那条 browser_download_url 会被自己的判据打成
# UNUSABLE——这不是推演，是 2026-09-30 拿 HEAD(=0.23.17) 的 ReleasePlan + MiniJson
# 真跑 decide 量出来的（新仓双名/只挂新名两档 UNUSABLE，旧仓路径与自家出口两档
# AVAILABLE）。修法是服务端把**旧名**那一枚的地址换成壳本来就信的同源出口。
# 下面钉的是：换得准（只换那一枚）、换得窄（四种不该换的情况都不换）、不脏缓存。

@pytest.fixture
def published_in_new_repo(monkeypatch):
    """把配置指回**新仓**——现网 v0.24 起就是这么发的。

    conftest 那份测试配置里 update_repo 还是旧仓，而"货在哪个仓"正是这条改写的第一
    个闸；不改这一处，下面每一档都会因为"还在旧仓"而天然不换，锁就成了空转。
    """
    from app.core import config_store
    monkeypatch.setattr(config_store, "update_repo",
                        lambda: config_store.DEFAULTS["update_repo"])


FENVER_APK = "https://github.com/abonla599/fenver/releases/download/v0.24.0/fenver-0.24.0.apk"
LEGACY_APK = ("https://github.com/abonla599/fenver/releases/download/v0.24.0"
              "/ai-assistant-native-0.24.0.apk")


def _dual_named_release():
    """v0.24 起真实发布形状：同一版并挂两名，外加一枚不相干的 mapping.txt。"""
    return {"tag_name": "v0.24.0",
            "html_url": "https://github.com/abonla599/fenver/releases/tag/v0.24.0",
            "assets": [{"name": "mapping.txt", "size": 9,
                        "browser_download_url": "https://github.com/abonla599/fenver/releases/download/v0.24.0/mapping.txt"},
                       {"name": "fenver-0.24.0.apk", "size": 98304,
                        "browser_download_url": FENVER_APK},
                       {"name": "ai-assistant-native-0.24.0.apk", "size": 98304,
                        "browser_download_url": LEGACY_APK}]}


def _assets_by_name(body):
    return {a["name"]: a["browser_download_url"] for a in body["assets"]}


def test_the_legacy_named_asset_moves_to_the_self_hosted_exit(published_in_new_repo, client, enforced, monkeypatch):
    """只有旧名那一枚换成自家出口；新名与别的资产一个字节都不动。

    新壳优先取新名，走的仍是 GitHub 直连（一台机器替人搬字节这段只兜还没升级的人）；
    mapping.txt 是控制组：证明这里换的是"那一枚资产"，不是"这个 release 的所有地址"。
    """
    monkeypatch.setattr(releases.urllib.request, "urlopen",
                        _Urlopen(_dual_named_release()))
    out = client.get("/v1/update/info", headers={"host": "ai.fenever.xyz"})
    assert out.status_code == 200, out.text
    got = _assets_by_name(out.json())
    assert got["ai-assistant-native-0.24.0.apk"] == "https://ai.fenever.xyz/site/android.apk"
    assert got["fenver-0.24.0.apk"] == FENVER_APK, "新名不该被换：那是新壳的直连通道"
    assert got["mapping.txt"].endswith("/mapping.txt"), "非 APK 的资产不在改写范围内"


@pytest.mark.parametrize("host", ["localhost:8000", "user@ai.fenever.xyz", "ai.fenever.xyz:8443"])
def test_an_origin_the_shell_would_not_trust_leaves_every_url_alone(        host, published_in_new_repo, client, enforced, monkeypatch):
    """控制组：拼不出合格来源（本机开发、带端口、带 userinfo）时一个都不换。

    壳那条同源判据只要 https、默认端口、纯主机。把一条它不会信的地址换上去，等于
    把"本来能用的 GitHub 地址"换成"用不了的自家地址"——那比不改更糟。
    """
    monkeypatch.setattr(releases.urllib.request, "urlopen",
                        _Urlopen(_dual_named_release()))
    out = client.get("/v1/update/info", headers={"host": host})
    assert out.status_code == 200, out.text
    got = _assets_by_name(out.json())
    assert got["ai-assistant-native-0.24.0.apk"] == LEGACY_APK
    assert got["fenver-0.24.0.apk"] == FENVER_APK


def test_a_release_carrying_only_the_legacy_name_is_passed_through(        published_in_new_repo, client, enforced, monkeypatch):
    """改名之前的老发布（只有一枚旧名）不换：没有"新仓地址老壳读不懂"这个问题。"""
    rel = _dual_named_release()
    rel["assets"] = [a for a in rel["assets"] if a["name"] != "fenver-0.24.0.apk"]
    monkeypatch.setattr(releases.urllib.request, "urlopen", _Urlopen(rel))
    out = client.get("/v1/update/info", headers={"host": "ai.fenever.xyz"})
    assert _assets_by_name(out.json())["ai-assistant-native-0.24.0.apk"] == LEGACY_APK


def test_a_release_still_hosted_in_the_legacy_repo_is_passed_through(
        client, enforced, monkeypatch):
    """货还在旧仓时不换：老壳的白名单本来就认那条路径，换了只是白占服务器带宽。"""
    from app.core import config_store
    monkeypatch.setattr(config_store, "update_repo", lambda: releases.LEGACY_REPO)
    monkeypatch.setattr(releases.urllib.request, "urlopen",
                        _Urlopen(_dual_named_release()))
    out = client.get("/v1/update/info", headers={"host": "ai.fenever.xyz"})
    assert out.status_code == 200, out.text
    got = _assets_by_name(out.json())
    assert got["ai-assistant-native-0.24.0.apk"] == LEGACY_APK
    assert releases._repo_of_payload == releases.LEGACY_REPO, "这条演的是'货从旧仓来'"


def test_the_rewrite_never_mutates_the_cached_snapshot(published_in_new_repo, client, enforced, monkeypatch):
    """改写只发生在返回给这一次请求的那份拷贝上。

    `_payload["raw"]` 是全端点共用的缓存：把它原地改了，下一个不认自家出口的调用方
    （官网按钮、卡片）就会拿到一条本该由 GitHub 直连的地址——症状是代取白跑一趟。
    """
    fake = _Urlopen(_dual_named_release())
    monkeypatch.setattr(releases.urllib.request, "urlopen", fake)
    out = client.get("/v1/update/info", headers={"host": "ai.fenever.xyz"})
    assert _assets_by_name(out.json())["ai-assistant-native-0.24.0.apk"].endswith("/site/android.apk")
    cached = _assets_by_name(releases._payload["raw"])
    assert cached["ai-assistant-native-0.24.0.apk"] == LEGACY_APK, "缓存里那份必须还是原样"
    assert cached["fenver-0.24.0.apk"] == FENVER_APK


@pytest.mark.parametrize("origin,expect", [
    ("https://ai.fenever.xyz", "https://ai.fenever.xyz/site/android.apk"),
    ("https://ai.fenever.xyz/", "https://ai.fenever.xyz/site/android.apk"),
    ("", ""), ("http://ai.fenever.xyz", ""), ("https://ai.fenever.xyz:8443", ""),
    ("https://ai.fenever.xyz/extra", ""), ("https://user@ai.fenever.xyz", ""),
])
def test_the_self_hosted_origin_is_accepted_only_in_the_shape_the_shell_trusts(origin, expect):
    """自家出口地址的合格线：https + 纯主机 + 那一条固定路径，别的形状一律拼不出。"""
    assert releases.self_hosted_apk_url(origin) == expect


def test_the_self_hosted_apk_path_is_the_same_string_in_the_server_the_route_and_the_shell():
    """三处同名锁：换出去的地址必须是壳写死的那条路径、也必须是真注册了的那条路由。

    少一个字符的症状不是报错，是老壳对着一句"看起来是下载地址"的东西回 UNUSABLE。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    router = (root / "backend" / "app" / "web" / "web_router.py").read_text(encoding="utf-8")
    assert f'@app.get("{releases.SELF_APK_PATH}"' in router, "自家出口没真注册在这条路径上"
    for shell in ("android", "android-native"):
        java = (root / shell / "app" / "src" / "main" / "java" / "xyz" / "fenever" /
                "assistant" / "core" / "ReleasePlan.java").read_text(encoding="utf-8")
        assert f'SELF_APK_PATH = "{releases.SELF_APK_PATH}"' in java, f"{shell} 的那条路径对不上"
