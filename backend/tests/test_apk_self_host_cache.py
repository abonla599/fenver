"""v0.28 磁盘代管：最新发布包落到本地盘，下载不再赌 GitHub 此刻的可用性。

用户真机反馈的两条警告（「下载失败：timeout」/「更新服务这会儿取不到安装包
（GitHub 慢或正忙）」）同源：`/site/android.apk` 过去每次都替访问者朝 GitHub
实时搬字节。这一档加在闸的最前面——盘上有验过发布校验值的字节就直接端出去。

锁的形状按"改坏哪一处红哪一条"写：
* 准入：只有对得上清单校验值的字节才许落盘/出盘；对不上的字节**隔离保留、绝不出盘**
  （v0.34 起是改名到 `*.rejected-*` 而不是删除——GitHub 账号停用后盘上那份不可再生，
  判据见 apk_cache._quarantine）；
* 原子：tmp+rename，读者永远看不到半截包；只留最新一版；
* 命中即不出网：盘上有货时 fetch_asset 一次都不许被调；
* 预取：同一版本只补一趟、失败可再试、全局同时最多一趟、不阻塞响应。

v0.34 起取数源是**自托管清单**（不再出网）：本文件末尾那一节把那一条链真跑一遍——
清单在盘上、包也在盘上，`/site/android.apk` 一个字节都不许朝外跑。
"""
import getpass
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

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


def test_a_corrupted_cache_file_is_quarantined_not_served_nor_destroyed():
    """掉电截断/被人手改：摘要对不上就当场停止服务那串字节，但**绝不销毁唯一副本**。

    v0.34 修订：这条纪律原本的理由是"字节的原产在 GitHub，随时能再取"，而 2026-10
    `abonla599` 账号被停用之后那个前提不成立了——盘上这一份是不可再生的唯一一份。
    一次误判（清单里的 sha256 写错、包被替换/截断）若顺手删了它，`/site/android.apk`
    与 `/v1/update/info` 双双长期不可用，只能人工回填。所以改成改名隔离到
    `*.rejected-<时间戳>`：既保住那串字节（可恢复），又让它不再被端出去。

    这**不是**放宽：纪律的意图一个字没改——认不出的字节绝不许端给任何人。放宽的只是
    "怎么处置这串字节"（保留而不是销毁），而"端出去"这条线寸步未动。
    """
    assert apk_cache.store(_VERSION, _PKG, _SHA)[0]
    path = apk_cache.cached_file(_VERSION, _SHA)
    with open(path, "r+b") as fh:
        fh.write(b"corrupted")
    assert apk_cache.cached_file(_VERSION, _SHA) == "", "对不上号的字节不许被端出去"
    # 原路径必须不再命中：否则每趟都要重算摘要，且迟早被 store 的"只留最新"顺手清掉。
    assert not os.path.exists(path), "隔离后原路径不该还在（那它还会被反复命中）"
    directory = os.path.dirname(path)
    quarantined = [f for f in os.listdir(directory) if ".rejected-" in f]
    assert len(quarantined) == 1, f"唯一副本没被留下：{os.listdir(directory)}"
    kept = os.path.join(directory, quarantined[0])
    with open(kept, "rb") as fh:
        assert fh.read().startswith(b"corrupted"), "留下的必须是那串对不上的原字节（可恢复性）"


def test_a_quarantined_copy_survives_the_next_version_swap():
    """隔离文件不以 `.apk` 结尾，所以 store 的"只留最新一版"那一趟不会顺手把它删掉。

    若隔离名以 `.apk` 结尾（例如 `x.bad.apk`），下一次落盘新版时的清理循环会把这份
    唯一副本一起清掉——那就等于换个地方又丢了一次。这条把命名约束钉死。
    """
    assert apk_cache.store(_VERSION, _PKG, _SHA)[0]
    path = apk_cache.cached_file(_VERSION, _SHA)
    with open(path, "r+b") as fh:
        fh.write(b"corrupted")
    assert apk_cache.cached_file(_VERSION, _SHA) == ""      # 触发隔离
    directory = os.path.dirname(path)
    quarantined = [f for f in os.listdir(directory) if ".rejected-" in f]
    assert len(quarantined) == 1, os.listdir(directory)

    other = b"PK\x03\x04" + os.urandom(1024)
    assert apk_cache.store("0.34.0", other, hashlib.sha256(other).hexdigest())[0]
    assert quarantined[0] in os.listdir(directory), \
        "换版时把隔离保留的唯一副本一起清掉了"


def test_the_digest_of_unchanged_bytes_is_computed_once(monkeypatch):
    """摘要按 (路径, mtime, size, ino) 缓存：同一次窗口内同一份字节只许哈希一次。

    P3 把清单 TTL 从 600 秒收到 30 秒之后，同一分钟内快照层、download_plan、预取会各
    问一次判定；没有这层缓存就是拿 CPU 换时间（本机实测那份 6.8 MB 的哈希约 4.5~5 ms
    一次）。这条同时钉住"缓存键必须跟着字节走"：只动 mtime 就得重算，绝不把旧摘要
    借给新字节。
    """
    assert apk_cache.store(_VERSION, _PKG, _SHA)[0]
    path = apk_cache.path_for(_VERSION)
    counts = {"n": 0}
    real = hashlib.sha256

    def counting(data=b""):
        counts["n"] += 1
        return real(data)

    monkeypatch.setattr(apk_cache.hashlib, "sha256", counting)
    for _ in range(5):
        assert apk_cache.cached_file(_VERSION, _SHA)
    assert counts["n"] == 1, f"5 次判定算了 {counts['n']} 次摘要，缓存没生效"

    # 字节没变但 mtime 动了（等价于有人原地重写过同一份内容）：键变 → 必须重算。
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
    assert apk_cache.cached_file(_VERSION, _SHA)
    assert counts["n"] == 2, "mtime 变了却不重算：旧摘要会借给新字节"


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


# ---------- v0.34：自托管清单这一条链真跑一遍（纯本地、零出网） ----------
# 与上面那些"把 download_plan 打桩"的用例不同，这一节让**真的** _fetch/download_plan
# 跑：清单在盘上、包也在盘上，整条链一个字节都不许朝外跑（GitHub 已不可用，
# 这条链必须能自证）。

def _manifest(version=_VERSION, sha=_SHA, size=None, url=None, name=None):
    return {"version": version, "apk_name": name or f"fenver-{version}.apk",
            "sha256": sha, "size": len(_PKG) if size is None else size,
            "url": url or "https://ai.fenever.xyz/site/android.apk",
            "changelog": "v" + version}


def _write_manifest(path, data):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return str(path)


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    """把清单也指进 tmp：运行时清单是这台机器唯一的版本宣告。"""
    runtime = tmp_path / "runtime" / "version.json"
    monkeypatch.setattr(releases, "runtime_manifest_path", lambda: str(runtime))
    monkeypatch.setattr(releases, "seed_manifest_path", lambda: str(tmp_path / "seed.json"))
    return runtime


def test_self_hosted_manifest_needs_no_network_at_all(monkeypatch, hosted):
    """清单 + 盘上那份包就够：/site/android.apk 命中磁盘，一次都不朝外跑。"""
    assert apk_cache.store(_VERSION, _PKG, _SHA)[0]
    _write_manifest(hosted, _manifest())

    def no_trip(url):
        raise AssertionError("自托管清单命中了磁盘还朝外跑")
    monkeypatch.setattr(releases, "fetch_asset", no_trip)

    c = peer_client(("203.0.113.31", 3131))
    res = c.get("/site/android.apk")
    assert res.status_code == 200 and res.content == _PKG
    assert _NAME in res.headers["content-disposition"]


def test_a_manifest_whose_digest_disagrees_with_disk_is_refused(hosted):
    """清单写的摘要与盘上那份字节不一致：当作没有货——退回官网，绝不端错字节。"""
    assert apk_cache.store(_VERSION, _PKG, _SHA)[0]
    _write_manifest(hosted, _manifest(sha="a" * 64))

    plan, why = releases.download_plan()
    assert plan is None and ("对不上" in why or "摘要" in why), why

    res = peer_client(("203.0.113.32", 3232)).get("/site/android.apk", follow_redirects=False)
    assert res.status_code in (302, 307)
    assert res.headers["location"] == releases.releases_page()


def test_prefetch_is_a_noop_when_the_self_hosted_package_is_already_on_disk(monkeypatch, hosted):
    """预取不该为空转：盘上已有且摘要对得上，连后台线程都不许起。"""
    assert apk_cache.store(_VERSION, _PKG, _SHA)[0]
    _write_manifest(hosted, _manifest())
    monkeypatch.setattr(releases, "fetch_asset", lambda u: pytest.fail("盘上有货不该出网"))
    apk_cache.prefetch()
    time.sleep(0.05)
    assert not [t for t in threading.enumerate() if t.name.startswith("apk-prefetch")]


def test_no_runtime_manifest_means_no_plan(hosted):
    """运行时清单不在、种子里也没有：download_plan 回 (None, 理由)，端点据此退回官网。"""
    plan, why = releases.download_plan()
    assert plan is None and why


# ---------- 磁盘判据那句理由：说清是哪个包，不说清是哪台机器 ----------

# `cached_file_state` 的第二返回值不只在日志里躺着：它经 `releases._fetch_manifest`
# 拼进 `/v1/update/info` 的 502 detail 与 `/v1/release/latest` 的 `reason`，而那两条
# 都在 `authz.PUBLIC_PATHS` 里点了名——任何路人都读得到。所以这里按"路人能读到的一切"
# 断言：盘符路径、当前用户名、仓库目录名都不许出现。
REPO_DIR_NAME = Path(apk_cache.__file__).resolve().parents[3].name
# 盘符路径 = 单字母 + `:` + 分隔符。豁免只认 URL scheme 那一形（`scheme 字符` 紧跟
# `单字母 + ://`）——测试夹具的下载地址全是 https 开头，认不出 `://` 判据就恒红；反过来只按
# "前一个字符是哪一类"划界，`at-C:\Users\a` 这类路径就让探测器瞎在生产网瞎的同一处。
# 两支并起来才与生产网 `core.pathmask._WIN_PATH` 的一支负向前瞻同判，同判由
# `test_pathmask.py` 的绑定锁按源码点名守住。
_DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9+.-])(?P<drive>[A-Za-z]):[\\/]"
                         r"|[A-Za-z0-9+.-][A-Za-z]:(?!//)[\\/]")


def _no_machine_shape(text: str, where: str, cache_root: str = "") -> None:
    assert not _DRIVE_PATH.search(text), f"{where} 里出现盘符路径：{text}"
    assert getpass.getuser() not in text, f"{where} 回显了当前用户名：{text}"
    assert REPO_DIR_NAME not in text, f"{where} 回显了仓库目录名：{text}"
    if cache_root:
        assert cache_root not in text, f"{where} 回显了数据根位置：{text}"


def test_the_missing_package_verdict_names_the_package_not_the_directory(tmp_path, capsys):
    """盘上什么都没有：那句理由要指得出"哪个包、清单期望什么"，指不出"这台机器在哪"。

    反向证据（不是信息压制）：包名与清单校验值前缀仍在对外那句里，运维读得出到底缺
    的是哪一版；数据根位置改由 `[apk-cache]` 那行打进 stdout，也就是 server.log。
    """
    cache_root = str(tmp_path / "apk")
    path, why = apk_cache.cached_file_state(_VERSION, _SHA)
    assert path == "" and why, why
    _no_machine_shape(why, "缺包那句", cache_root)
    assert os.path.basename(apk_cache.path_for(_VERSION)) in why, why
    assert _SHA[:8] in why, f"清单期望的校验值前缀被改没了：{why}"

    logged = capsys.readouterr().out
    assert "[apk-cache]" in logged and cache_root in logged, \
        f"位置没进日志，运维只剩包名可查：{logged}"


def test_the_digest_mismatch_verdict_names_both_prefixes_without_a_path(tmp_path, capsys):
    """清单与盘上字节对不上：两句前缀（盘上/清单）+ 包名，同样不许带出磁盘位置。

    这一格是"包被换掉/截断/清单写错"的唯一症状，措辞改坏了就等于出事那天没人看得懂；
    而"到底是盘上哪一个文件"是运维拿日志查的东西，不是拿手机查的东西。
    """
    assert apk_cache.store(_VERSION, _PKG, _SHA)[0]
    cache_root = str(tmp_path / "apk")
    tampered = b"tampered-bytes"
    with open(apk_cache.path_for(_VERSION), "wb") as fh:
        fh.write(tampered)

    path, why = apk_cache.cached_file_state(_VERSION, _SHA)
    assert path == "" and "摘要" in why, why
    _no_machine_shape(why, "摘要不符那句", cache_root)
    assert os.path.basename(apk_cache.path_for(_VERSION)) in why, why
    assert _SHA[:8] in why and hashlib.sha256(tampered).hexdigest()[:8] in why, why
    assert "隔离保留" in why, f"「没销毁唯一副本」这句话被改丢了：{why}"

    logged = capsys.readouterr().out
    assert "[apk-cache]" in logged and cache_root in logged, logged


def test_an_unreadable_cache_file_reports_the_error_class_not_its_location(tmp_path,
                                                                          monkeypatch,
                                                                          capsys):
    """`_digest` 读不动那份字节时，异常类型名可以外传，文件位置不行。

    这一格最容易漏：理由取自 `_digest` 的 `why`，本身不含路径，但它被拼进 `（…）`
    的位置参数时如果被顺手补上 `{path}` 就会走到公开端点上——所以单独钉一条。
    只挡 `open` 不挡 `os.stat`：后者一并挂掉会让 `os.path.isfile` 也说"没有"，
    测到的是缺包那一格而不是读不动那一格。
    """
    assert apk_cache.store(_VERSION, _PKG, _SHA)[0]
    cache_root = str(tmp_path / "apk")
    target = apk_cache.path_for(_VERSION)

    import builtins

    real_open = builtins.open

    def guarded(file, *args, **kwargs):
        if str(file) == target:
            raise PermissionError(13, "模拟读不动")
        return real_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", guarded)

    path, why = apk_cache.cached_file_state(_VERSION, _SHA)
    monkeypatch.undo()
    assert path == "" and "读缓存文件失败" in why, why
    _no_machine_shape(why, "读不动那句", cache_root)
    assert os.path.basename(target) in why, why
    assert "PermissionError" in why, f"异常类型名是判据的一部分，不该一起消失：{why}"
    logged = capsys.readouterr().out
    assert cache_root in logged, logged

