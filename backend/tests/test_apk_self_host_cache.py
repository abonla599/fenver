"""v0.28 磁盘代管：最新发布包落到本地盘，下载不再赌 GitHub 此刻的可用性。

用户真机反馈的两条警告（「下载失败：timeout」/「更新服务这会儿取不到安装包
（GitHub 慢或正忙）」）同源：`/site/android.apk` 过去每次都替访问者朝 GitHub
实时搬字节。这一档加在闸的最前面——盘上有验过发布校验值的字节就直接端出去。

锁的形状按"改坏哪一处红哪一条"写：
* 准入：只有对得上 Release 正文 `APK-SHA256` 的字节才许落盘/出盘；
* 原子：tmp+rename，读者永远看不到半截包；只留最新一版；
* 命中即不出网：盘上有货时 fetch_asset 一次都不许被调；
* 预取：同一版本只补一趟、失败可再试、全局同时最多一趟、不阻塞响应。
"""
import hashlib
import os
import threading
import time

import pytest

from app.core import apk_cache, releases
from app.web import web_router
from tests.conftest import peer_client

_PKG = b"PK\x03\x04" + os.urandom(2048)          # 装个"APK"的样子：够真字节算摘要
_SHA = hashlib.sha256(_PKG).hexdigest()
_VERSION = "0.28.0"
_NAME = f"fenver-{_VERSION}.apk"


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """每一用例一台"刚起来的机器"：缓存目录指进 tmp，预取记账清零，闸门复位。"""
    monkeypatch.setenv("APK_CACHE_DIR", str(tmp_path / "apk"))
    apk_cache.reset_for_tests()
    web_router.reset_apk_gates_for_tests()
    releases.reset_for_tests()
    yield
    apk_cache.reset_for_tests()
    web_router.reset_apk_gates_for_tests()
    releases.reset_for_tests()


def _plan(sha=_SHA):
    return ({"url": f"https://github.com/o/r/releases/download/v{_VERSION}/{_NAME}",
             "name": _NAME, "size": len(_PKG), "version": _VERSION, "sha256": sha}, "")


# ---------- 落盘与出盘的准入纪律 ----------

def test_store_then_find_roundtrip():
    ok, why = apk_cache.store(_VERSION, _PKG, _SHA)
    assert ok and why == ""
    path = apk_cache.cached_file(_VERSION, _SHA)
    assert path and os.path.isfile(path)
    assert os.path.getsize(path) == len(_PKG)


def test_bytes_without_a_publish_digest_never_enter_the_cache():
    """正文没带 APK-SHA256 的发布（不是我们流水线的形状）：不落盘，也不从盘上端。"""
    ok, why = apk_cache.store(_VERSION, _PKG, "")
    assert not ok and "校验值" in why
    assert apk_cache.cached_file(_VERSION, "") == "", "没有凭据的字节不许被当成有缓存"


def test_a_corrupted_cache_file_is_deleted_not_served():
    """掉电截断/被人手改：摘要对不上就当场作废删掉，绝不"差不多就先端着"。"""
    assert apk_cache.store(_VERSION, _PKG, _SHA)[0]
    path = apk_cache.cached_file(_VERSION, _SHA)
    with open(path, "r+b") as fh:
        fh.write(b"corrupted")
    assert apk_cache.cached_file(_VERSION, _SHA) == ""
    assert not os.path.exists(path), "对不上号的残骸该被清走，而不是躺在盘上"


def test_only_the_newest_version_stays_on_disk():
    """只留最新一版：更新链永远指向 latest，攒旧版是白占 9 MB。"""
    assert apk_cache.store("0.27.0", b"old-bytes", hashlib.sha256(b"old-bytes").hexdigest())[0]
    assert apk_cache.store(_VERSION, _PKG, _SHA)[0]
    assert apk_cache.cached_file("0.27.0",
                                 hashlib.sha256(b"old-bytes").hexdigest()) == ""
    assert apk_cache.cached_file(_VERSION, _SHA)


@pytest.mark.parametrize("bad", ["", "..", "../evil", "0.28.0/../../x", "a" * 200,
                                 "0.28.0;rm", "0.28.0\x00"])
def test_a_version_that_does_not_look_like_a_version_touches_no_path(bad):
    """版本号来自对面发布页的 tag_name：拼进磁盘路径前必须先过形状关。"""
    assert apk_cache.path_for(bad) == ""


# ---------- 端点：命中磁盘档就不出网 ----------

def test_site_serves_from_disk_without_touching_github(monkeypatch):
    """盘上有货：fetch_asset 一次都不许被调，响应仍是完整的包 + 附件名。"""
    assert apk_cache.store(_VERSION, _PKG, _SHA)[0]
    monkeypatch.setattr(releases, "download_plan", _plan)

    def no_trip(url):
        raise AssertionError("磁盘命中了还朝 GitHub 跑")
    monkeypatch.setattr(releases, "fetch_asset", no_trip)

    c = peer_client(("203.0.113.21", 2121))
    res = c.get("/site/android.apk")
    assert res.status_code == 200 and res.content == _PKG
    assert _NAME in res.headers["content-disposition"]
    # 出口字节照扣格子：这一格量的是带宽，不是 GitHub 趟数
    assert len(web_router._APK_DOWNLOADS["203.0.113.21"]) == 1


def test_first_fetch_persists_for_everyone_after(monkeypatch):
    """第一趟照旧实时代取，但取回的字节要落盘：下一个访问者不再出网。"""
    trips = []

    def fetch(url):
        trips.append(url)
        return (_PKG, "")
    monkeypatch.setattr(releases, "download_plan", _plan)
    monkeypatch.setattr(releases, "fetch_asset", fetch)

    a = peer_client(("203.0.113.22", 2222))
    assert a.get("/site/android.apk").status_code == 200
    assert len(trips) == 1 and apk_cache.cached_file(_VERSION, _SHA)

    # 接力 TTL 过期、槽位被占死：磁盘档仍然端得出来——这正是"GitHub 慢或正忙"
    # 从下载链路上消失的那一刻。
    web_router.reset_apk_gates_for_tests()
    monkeypatch.setattr(releases, "fetch_asset",
                        lambda u: pytest.fail("盘上有货还不许出网"))
    b = peer_client(("203.0.113.23", 2323))
    assert b.get("/site/android.apk").status_code == 200
    assert len(trips) == 1


def test_a_release_without_a_digest_keeps_the_old_behaviour(monkeypatch):
    """旧版发布（正文没有校验值）：一切照旧走接力/实时代取，磁盘档完全不参与。"""
    plan = _plan(sha="")[0]
    trips = []

    def fetch(url):
        trips.append(url)
        return (_PKG, "")
    monkeypatch.setattr(releases, "download_plan", lambda: (plan, ""))
    monkeypatch.setattr(releases, "fetch_asset", fetch)
    c = peer_client(("203.0.113.24", 2424))
    assert c.get("/site/android.apk").status_code == 200
    assert len(trips) == 1
    assert apk_cache.cached_file(_VERSION, "") == ""
    cache_root = os.environ["APK_CACHE_DIR"]
    files = os.listdir(cache_root) if os.path.isdir(cache_root) else []
    assert not files, f"没有校验值的发布不该往盘上落任何东西：{files}"


# ---------- 预取：检查更新顺手备货 ----------

def _wait_warm(timeout=2.0):
    """等预取线程收工：轮询盘上出现文件或所有 prefetch 线程退出。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [t for t in threading.enumerate() if t.name.startswith("apk-prefetch")]
        if not alive:
            return
        time.sleep(0.02)
    raise AssertionError("预取线程没收工：单飞行或线程生命周期有问题")


def test_prefetch_warms_the_disk_once_per_version(monkeypatch):
    calls = []
    monkeypatch.setattr(releases, "download_plan", _plan)
    monkeypatch.setattr(releases, "fetch_asset",
                        lambda u: (calls.append(u), (_PKG, ""))[1])
    apk_cache.prefetch()
    _wait_warm()
    assert len(calls) == 1 and apk_cache.cached_file(_VERSION, _SHA)

    # 盘上已有货：再来一万次检查更新，一次都不许多跑
    for _ in range(5):
        apk_cache.prefetch()
    assert len(calls) == 1


def test_prefetch_failure_is_not_marked_warm(monkeypatch):
    """GitHub 那头抽风：这次不记功，下次检查更新还会再试——备货不能一次失败就躺平。"""
    calls = []

    def flaky(url):
        calls.append(url)
        return (None, "模拟超时") if len(calls) == 1 else (_PKG, "")
    monkeypatch.setattr(releases, "download_plan", _plan)
    monkeypatch.setattr(releases, "fetch_asset", flaky)
    apk_cache.prefetch()
    _wait_warm()
    assert apk_cache.cached_file(_VERSION, _SHA) == ""
    apk_cache.prefetch()
    _wait_warm()
    assert len(calls) == 2 and apk_cache.cached_file(_VERSION, _SHA)


def test_prefetch_never_blocks_the_caller(monkeypatch):
    """预取是搭车动作：网络再慢也不许拖住「检查更新」的回包。"""
    gate = threading.Event()

    def slow(url):
        gate.wait(5)
        return (_PKG, "")
    monkeypatch.setattr(releases, "download_plan", _plan)
    monkeypatch.setattr(releases, "fetch_asset", slow)
    started = time.monotonic()
    apk_cache.prefetch()
    assert time.monotonic() - started < 0.5, "prefetch 在等网络——端点会被它拖慢"
    gate.set()
    _wait_warm()


def test_prefetch_skips_releases_without_a_digest(monkeypatch):
    monkeypatch.setattr(releases, "download_plan", lambda: (_plan(sha="")[0], ""))

    def no_trip(url):
        raise AssertionError("没有校验值的发布不该被预取")
    monkeypatch.setattr(releases, "fetch_asset", no_trip)
    apk_cache.prefetch()
    time.sleep(0.05)
    assert not [t for t in threading.enumerate() if t.name.startswith("apk-prefetch")]


# ---------- 快照层把校验值递给下游 ----------

def test_download_plan_carries_the_publish_digest(monkeypatch):
    """快照里的 sha256 来自 Release 正文那行 APK-SHA256，并随 download_plan 透传。"""
    body = {"tag_name": "v0.28.0",
            "html_url": "https://github.com/o/r/releases/tag/v0.28.0",
            "body": f"安装说明\nAPK-SHA256: {_SHA}",
            "assets": [{"name": _NAME, "size": len(_PKG),
                        "browser_download_url":
                            "https://github.com/o/r/releases/download/v0.28.0/" + _NAME}]}
    assert releases.apk_sha256(body["body"]) == _SHA
    monkeypatch.setattr(releases, "_payload", {
        "version": "0.28.0", "url": body["html_url"], "asset_name": _NAME,
        "asset_url": body["assets"][0]["browser_download_url"], "size": len(_PKG),
        "sha256": _SHA, "raw": body})
    monkeypatch.setattr(releases, "_payload_at", time.monotonic() + 10_000)
    monkeypatch.setattr(releases, "_fetched_at", time.monotonic() + 10_000)
    plan, why = releases.download_plan()
    assert why == "" and plan["sha256"] == _SHA
