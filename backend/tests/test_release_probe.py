"""`/v1/release/latest` 与它背后那条取数源（v0.34 起是**自托管清单**）。

这张卡片只有一个责任："确实有新版时提一句"。所以这里钉的全是它**不该**做的事：
不该每次有人打开 App 就真去读一遍清单（缓存）、不该在读不到时说"你已是最新"
（三态）、不该让阻塞网络待在事件循环里（同步 def）、不该让一个能中间人的人
替我们决定用户去下哪个 apk（系统证书库 + 资产名必须对上版本号）。

版本比较在这里是第二份实现（第一份是壳里的 `ReleasePlan.compare`）。两份跨语言的
实现一定漂，所以那张用例表由 Java 的单测与本文件**共用同一份判据**：从 Java 测试源码里
把用例读出来，逐条喂给 Python 的那一份。

GitHub 那条取数链（`_fetch_repo` / `fetch_asset`）仍是活代码（账号解封后可接回来），
所以它的 TLS 与跳转白名单判据继续在这里钉着。
"""
import hashlib
import json
import os
import re
import urllib.error

import pytest
from fastapi.testclient import TestClient

from app.core import apk_cache, releases
from app.main import app

GOOD = {"tag_name": "v0.18", "html_url": "https://github.com/o/r/releases/tag/v0.18",
        "assets": [{"name": "ai-assistant-native-0.18.apk", "size": 98304,
                    "browser_download_url": "https://objects.example/ai-assistant-native-0.18.apk"}]}

# ---------- 自托管清单夹具 ----------
# 一份够真的"安装包"字节：摘要必须能被算出来，所以内容随便但不能是空串。
PKG = b"PK\x03\x04" + b"probe-fixture-apk" * 64
SHA = hashlib.sha256(PKG).hexdigest()
VERSION = "0.33.1"
NAME = f"fenver-{VERSION}.apk"


def make_manifest(**over) -> dict:
    """一份形状合格的清单；用例按需覆盖其中一格（用来演各种"读不出来"）。"""
    data = {"version": VERSION, "apk_name": NAME, "sha256": SHA, "size": len(PKG),
            "url": "https://ai.fenever.xyz/site/android.apk", "changelog": "v" + VERSION}
    data.update(over)
    return data


def write_manifest(path, data) -> str:
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return str(path)


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, _limit):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Urlopen:
    """假 urlopen：按脚本依次返回结果，并记下每一次调用。

    脚本用完还被打第二次就直接失败——"多了一次出站请求"正是缓存那条锁要抓的事，
    把它咽下来等于让锁闭着眼通过。
    """

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, request, timeout=None, context=None):
        self.calls.append({"request": request, "timeout": timeout, "context": context})
        if not self.script:
            raise AssertionError(f"第 {len(self.calls)} 次出站超出脚本，缓存或重试不对")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return _FakeResponse(step)


@pytest.fixture(autouse=True)
def cold_cache(tmp_path, monkeypatch):
    """每一用例一台"刚起来的机器"：缓存目录与清单都指进 tmp，进程内记账清零。

    清单必须指走：不指走的话用例写的是 `data/version.json`——那正是生产机的运行时
    清单，测试会把它读出来当真。APK 缓存同理（默认落在 <项目根>/data/apk）。
    """
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


def prime(data=None, apk=True):
    """把一台机器布置成"清单已就位、包也在盘上"的样子，返回清单路径。"""
    path = releases.runtime_manifest_path()
    write_manifest(path, data if data is not None else make_manifest())
    if apk:
        apk_cache.store(VERSION, PKG, (data or make_manifest())["sha256"])
    return path



# ---------- 三态：有 / 没有 / 不知道 ----------

def test_it_says_update_exists_only_when_the_installed_one_is_older(cold_cache):
    prime()
    out = releases.probe(have="0.16")
    assert out["ok"] and out["latest"] == VERSION and out["has_update"] is True
    assert out["size"] == len(PKG) and out["asset_name"] == NAME

    releases.reset_for_tests()
    same = releases.probe(have="v" + VERSION)    # 带不带 v 是同一个版本
    assert same["has_update"] is False

    releases.reset_for_tests()
    ahead = releases.probe(have="0.34.0")
    assert ahead["has_update"] is False


def test_not_being_able_to_read_is_not_reported_as_up_to_date(cold_cache):
    """`ok:false` 与 `has_update:false` 必须分得开：前者是"我不知道"，后者才是"你已是最新"。

    把这两件混成一件的界面会做出最坏的行为——清单读不到的那天，所有人都会被告知
    "你没有更新"，而这句话听起来完全可信。
    """
    out = releases.probe(have="0.16")            # 清单文件还没写
    assert out["ok"] is False and out["has_update"] is None
    assert "清单" in out["reason"] or "version.json" in out["reason"], out["reason"]


def test_a_manifest_without_a_useful_version_is_a_failure_not_an_empty_one(cold_cache):
    prime(data=make_manifest(version=""), apk=False)
    out = releases.probe(have="0.16")
    assert out["ok"] is False and out["has_update"] is None, "读不到版本号却回了一份'最新是空'"
    assert "version" in out["reason"], out["reason"]


def test_asking_without_saying_what_is_installed_is_its_own_answer(cold_cache):
    prime()
    out = releases.probe()
    assert out["ok"] and out["latest"] == VERSION
    assert out["has_update"] is None and "哪版" in out["reason"]


# ---------- 缓存与"出网"/读盘 ----------

def test_a_counted_read_shows_the_cache_holds(cold_cache, monkeypatch):
    """免鉴权 + 会读盘（过去是会出网）= 必须自己挡住放大。一个缓存期一读，与来多少请求无关。

    v0.34 起取数源是本地清单，"出网"变成"读盘 + 判定那份 APK 的摘要"。这里数的是
    `_read_manifest_file` 的调用次数：同一份快照在缓存期内只许读一次。

    缓存期今天由 `releases.CACHE_SECONDS` 决定（30 秒，用户要求"改完即近生效"），
    这里不写死数字——写死的话调一次 TTL 就要改一处测试，很容易漏；真正被钉的是
    "20 次问只读 1 次"这个行为。
    """
    prime()
    calls = []
    real = releases._read_manifest_file

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(releases, "_read_manifest_file", counting)
    for _ in range(20):
        releases.probe(have="0.16")
    assert len(calls) == 1, f"20 次问出了 {len(calls)} 次读盘，缓存没生效"


def test_a_failed_refresh_keeps_the_last_good_answer_but_retries_later(cold_cache):
    """清单这会儿读不到时，不再逐个请求去重读，而是继续用上一次的结果（并说清这次没读到）。"""
    import time

    prime()
    assert releases.probe(have="0.16")["latest"] == VERSION
    os.remove(releases.runtime_manifest_path())            # 清单被删/坏掉
    releases._fetched_at = time.monotonic() - releases.CACHE_SECONDS     # 假装缓存到期
    stale = releases.probe(have="0.16")
    assert stale["ok"] and stale["latest"] == VERSION, "读不到就清空，等于把用户已有的答案弄丢"
    assert "清单" in stale["reason"], "用了旧数据就该说清楚这次没读到"


def test_a_machine_that_just_booted_still_probes(cold_cache, monkeypatch):
    """刚开机的机器上 monotonic 只有几秒——缓存哨兵不能拿 0.0 当"该读了"。

    这不是假想出来的边界，是 CI 抓的（runner 是一台刚开的虚拟机，monotonic 远小于
    CACHE_SECONDS）：`now - 0.0 >= 30` 在那台机器上为假，于是"从没读过"被判成
    "缓存还新"，而 `_payload` 是空的。线上对应的症状是**每次重启后的头 30 秒里那张
    卡片永远不弹**——不弹、不报错、没人知道。（窗口从 600 收到 30 只是把这段缩短，
    判据本身不变。）
    """
    prime()
    reads = []
    real = releases._read_manifest_file
    monkeypatch.setattr(releases, "_read_manifest_file",
                        lambda p: (reads.append(p), real(p))[1])
    monkeypatch.setattr(releases.time, "monotonic", lambda: 5.0)        # 开机 5 秒

    out = releases.probe(have="0.16")
    assert len(reads) == 1, "刚起来的机器一次都不去读：那这 30 秒里没人能看到卡片"
    assert out["ok"] and out["has_update"] is True, out


def test_a_fresh_process_with_a_small_clock_probes():
    """上一条测的是"reset 之后的模块"，这条测的是**刚 import 进来的模块**那一行初值。

    为什么要开子进程：autouse 的 cold_cache 夹具会把模块状态重写成"刚起来"的样子，
    于是在主进程里把 `_fetched_at = None` 改回 `0.0`，上面那条照样绿——而生产要跑的
    恰恰是那一行初值（进程起来之后没人替它 reset）。判据只能在新解释器里读它。
    """
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(releases.__file__).resolve().parents[1].parent
    code = (
        "import app.core.releases as r\n"
        "r.time.monotonic = lambda: 5.0\n"                 # 一台刚开的机器
        "r._fetch = lambda: ({'version': '0.18', 'url': 'u', 'asset_name': 'a',\n"
        "                     'asset_url': 'd', 'size': 1, 'sha256': 'x',\n"
        "                     'raw': {'tag_name': 'v0.18', 'assets': []}}, '')\n"
        "print('PROBED' if r.probe(have='0.16')['ok'] else 'SILENT')\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(backend), timeout=120)
    assert "PROBED" in out.stdout, f"新进程 + 小钟 = 不去读：{out.stdout}{out.stderr}"


# ---------- P2：状态变化必须有人喊（优先级陷阱与摘要不符） ----------

def test_a_runtime_manifest_overriding_a_different_seed_says_so(capsys, cold_cache):
    """运行时清单压过了**版本号不同**的种子时，必须响亮地说一声，且同一状态只说一次。

    这是这套优先级的固有陷阱：有人在仓库里改了种子却忘了改生产机的 data/version.json，
    新版本会**静默**发不出去，谁都不会发现——所以这一格必须有人喊。按"措辞变了才打"，
    同一状态再问不刷屏。
    """
    runtime, seed = cold_cache
    write_manifest(seed, make_manifest(version="0.34.0", apk_name="fenver-0.34.0.apk"))
    write_manifest(runtime, make_manifest())                       # 运行时仍是 0.33.1
    data, why, used = releases.load_manifest()
    assert why == "" and data["version"] == VERSION
    assert used == releases.runtime_manifest_path()
    out = capsys.readouterr().out
    assert "压过了打包种子" in out, out
    assert VERSION in out and "0.34.0" in out, out

    releases.load_manifest()                                       # 同一状态：不该再刷
    assert "压过了打包种子" not in capsys.readouterr().out


def test_runtime_and_seed_agreeing_in_version_stay_quiet(capsys, cold_cache):
    """两份都在、版本号一致 = 正常运行，不喊——否则每次换包都先听到一句吓人的话。"""
    runtime, seed = cold_cache
    write_manifest(seed, make_manifest())
    write_manifest(runtime, make_manifest())
    releases.load_manifest()
    assert "压过了打包种子" not in capsys.readouterr().out


def test_a_manifest_digest_mismatch_gets_logged_and_clears_on_recovery(capsys, cold_cache):
    """「清单摘要与磁盘字节不符」也要有人喊；恢复之后标记清掉，下次再坏还能再喊。

    这一格对不上时 `/v1/update/info` 与 `/site/android.apk` 双双长期不可用，而过去
    整个模块一句日志都没有——正是 P2 要堵的那格。第 4 步刻意**不** reset（reset 会把
    标记一起清掉），改用时间旅行逼出一次同步重读，这样"恢复时清了标记"才真的被测到。
    """
    import time

    runtime, _seed = cold_cache
    apk_cache.store(VERSION, PKG, SHA)
    write_manifest(runtime, make_manifest(sha256="b" * 64))        # 盘上真、清单假
    releases.reset_for_tests()
    assert releases.download_plan()[0] is None
    out = capsys.readouterr().out
    assert "[releases]" in out and VERSION in out and "不可用" in out, out

    # 恢复：清单改回正确摘要、把包重新放回盘上（上一趟对不上时那份已被隔离保留）。
    write_manifest(runtime, make_manifest())
    apk_cache.store(VERSION, PKG, SHA)
    releases.reset_for_tests()
    assert releases.download_plan()[0] is not None
    capsys.readouterr()

    # 再坏一次（这次是包不在盘上）却**不** reset：标记若没在恢复时清掉，就不会再喊。
    skew = time.monotonic() - releases.CACHE_SECONDS - 1
    releases._fetched_at = skew
    releases._payload_at = skew
    os.remove(apk_cache.path_for(VERSION))
    assert releases.download_plan()[0] is None
    assert "不可用" in capsys.readouterr().out


def test_the_github_leg_path_still_fetches_over_the_system_trust_store(monkeypatch):
    """GitHub 那条取数链（留着可复用的活代码）回来的是要点给人去下载/安装的地址：
    能被中间人改掉，就等于能推任意安装包。所以那条请求必须走 `core/tls.py` 那份系统
    证书库上下文。写成"验不过就退回不验"是这个仓库明确拒绝过的那种修法（524 那次）。

    v0.34 起默认取数源不再出网，所以这条用例直接打 `_fetch_repo`（那条链本身没删）。
    """
    fake = _Urlopen(GOOD)
    monkeypatch.setattr(releases.urllib.request, "urlopen", fake)
    seen = {}

    def fake_context():
        seen["called"] = True
        return object()

    monkeypatch.setattr(releases, "system_ssl_context", fake_context)
    payload, why = releases._fetch_repo("o/r")
    assert why == "" and payload and payload["version"] == "0.18"
    assert seen.get("called"), "没走系统证书库"
    assert fake.calls[0]["context"] is not None, "urlopen 没收到 context"
    assert fake.calls[0]["timeout"] == releases.TIMEOUT_SECONDS, "没有超时的出站请求会拖死线程池"


def test_the_github_host_is_written_in_exactly_one_place():
    """`api.github.com` 在后端只许出现一次：多一处就多一个能漂白的地址。"""
    from pathlib import Path

    root = Path(releases.__file__).resolve().parent.parent      # backend/app
    hits = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        if "api.github.com" in body:
            hits.append(path.name)
    assert hits == ["releases.py"], f"GitHub 地址出现在多处：{hits}"


def test_a_manifest_naming_a_package_for_another_version_is_not_taken(cold_cache):
    """清单写的包名必须就是这一版那一个（`fenver-<version>.apk`）：对不上就是"读不出来"。

    一次发布可能挂着 mapping.txt、别的平台的产物或误传的旧包——这条形状锁顺带保证
    "这一版可安装的那一个"只有一个答案，也防住了把陌生人的手机指到别的文件上。
    """
    prime(data=make_manifest(apk_name="fenver-0.17.apk"), apk=False)
    out = releases.probe(have="0.16")
    assert out["ok"] is False and out["has_update"] is None, "包名与版本对不上却当成有货"
    assert "apk_name" in out["reason"], out["reason"]


# ---------- 路由 ----------

def test_the_endpoint_is_public_read_only_and_answers_200(cold_cache):
    """没登录也要答：这张卡片出现在人还没输口令的时候。"""
    from app.core.authz import PUBLIC_PATHS

    assert "/v1/release/latest" in PUBLIC_PATHS
    prime()
    res = TestClient(app).get("/v1/release/latest", params={"have": "0.16"})
    assert res.status_code == 200, res.text
    assert res.json()["has_update"] is True


def test_it_is_a_sync_endpoint_because_it_blocks(monkeypatch):
    """`def` 而不是 `async def`：FastAPI 会把同步路由丢进线程池。

    写成 async 的话，那 5 秒超时是**整个事件循环**在等——正在流式回答的人一起卡住，
    而这个仓库为同一件事付过一次 524 的账。
    """
    import inspect

    from app.main import release_latest
    assert not inspect.iscoroutinefunction(release_latest), "async 路由会把出站等待搬回事件循环"


# ---------- 跨语言那份比较规则不许漂 ----------

def _java_compare_cases():
    """从壳的 JUnit 里把 compare 的用例读出来（同一张表，两边各跑一遍）。"""
    from pathlib import Path

    path = (Path(releases.__file__).resolve().parents[3]
            / "android-native" / "app" / "src" / "test" / "java" / "xyz" / "fenever"
            / "assistant" / "core" / "ReleasePlanTest.java")
    text = path.read_text(encoding="utf-8")
    cases = []
    for a, b, op in re.findall(r'compare\("([\d.]+)",\s*"([\d.]+)"\)\s*([<>])\s*0', text):
        cases.append((a, b, -1 if op == "<" else 1))
    for a, b in re.findall(r'assertEquals\(0,\s*ReleasePlan\.compare\("([\d.]+)",\s*"([\d.]+)"\)\)', text):
        cases.append((a, b, 0))
    return path.name, cases


def test_python_and_the_shell_agree_on_every_case_the_shell_tests():
    """Java 那份测过的用例，Python 这份必须给同样的答案。

    判据从 Java 测试源码里读，不是在这里手抄一份：手抄的那份会在下一个人往 Java
    里加一条用例的那天悄悄失效，而两边不一致的表现是"壳说有新版本、卡片说不该弹"。
    """
    source, cases = _java_compare_cases()
    assert len(cases) >= 5, f"从 {source} 里只读到 {len(cases)} 条用例，这条锁快空转了"
    for a, b, expected in cases:
        got = releases.compare(a, b)
        assert (got > 0) - (got < 0) == expected, f"compare({a!r}, {b!r}) 两边给的答案不同：{got} vs {expected}"


# ---------- 官网那颗「安卓版」按钮：服务端代取 APK ----------

_SPOOFY = "0.17" + chr(13) + chr(10) + "X-Spoof: 1"


def _payload(version="0.17", name="ai-assistant-native-0.17.apk",
             url="https://github.com/abonla599/ai-assistant/releases/download/v0.17/ai-assistant-native-0.17.apk",
             size=102_400):
    return {"version": version, "url": f"https://github.com/x/releases/tag/v{version}",
            "asset_name": name, "asset_url": url, "size": size}


def _prime(monkeypatch, payload):
    monkeypatch.setattr(releases, "_payload", payload)
    monkeypatch.setattr(releases, "_fetched_at", 1e9)      # 让它以为刚拉过，不碰网络
    # 新鲜度现在看"最近一次成功"（_payload_at，T1.3 的 AC-4 收紧）——primed 快照
    # 必须同步这个读数，否则 stale_attempt 会越过假数据去真出网。
    monkeypatch.setattr(releases, "_payload_at", 1e9)


def test_download_plan_accepts_a_normal_release(monkeypatch):
    _prime(monkeypatch, _payload())
    plan, reason = releases.download_plan()
    assert reason == "" and plan["name"] == "ai-assistant-native-0.17.apk"
    assert plan["url"].startswith("https://") and plan["size"] == 102_400


@pytest.mark.parametrize("broken, why", [
    (lambda: _payload(url="http://github.com/x/apk"), "明文 http 不代理"),
    (lambda: _payload(url="https://evil.example.com/apk"), "白名单外的主机不代理"),
    (lambda: _payload(name="ai-assistant-native-0.16.apk", version="0.17"), "名字与版本不一致"),
    (lambda: _payload(size=0), "大小不知道就不代理"),
    (lambda: _payload(size=64 * 1024 * 1024), "大得离谱的资产不当 apk 代理"),
    # 资产名要原样进 Content-Disposition。光靠"名字等于 ai-assistant-native-<版本>.apk"挡不住它：
    # 名字是拿版本号拼出来的，而 tag_name 来自对面——一个带 CR/LF 的 tag_name 拼出来的是
    # 一个能对响应头做注入的值。所以形状必须先过一遍正则。
    (lambda: _payload(version=_SPOOFY, name="ai-assistant-native-" + _SPOOFY + ".apk"),
     "资产名形状不对就不代理（防响应头注入）"),
])
def test_download_plan_refuses_without_raising(monkeypatch, broken, why):
    """五种坏形状全部回 (None, 一句理由)，一句都不抛。

    这个返回值决定的是"陌生人的浏览器从我们这台服务器下载哪个字节流"，所以宁可拒。
    白名单只放 GitHub 的两个主机：`browser_download_url` 会再跳一次到
    objects.githubusercontent.com，那是这条链路上唯一合法的第二次落脚。
    """
    _prime(monkeypatch, broken())
    plan, reason = releases.download_plan()
    assert plan is None, f"{why}，却还是给了下载地址：{plan}"
    assert reason, f"{why}，但没给出理由（调用方就没法解释为什么退回 GitHub 页面）"


def test_download_plan_with_no_snapshot_says_so(cold_cache):
    """没有清单（这台机器上一份都读不到）时，(None, 理由)——调用方据此退回官网。"""
    plan, reason = releases.download_plan()
    assert plan is None and "清单" in reason, reason


def test_the_asset_is_fetched_over_the_system_trust_anchor(monkeypatch):
    """取字节必须走 core/tls 那份系统信任锚，而且不许自动跟跳转。

    跟跳转的权力要自己拿着：每一跳的目标都得重新过白名单，否则第一跳合法、
    第二跳就能把人送到任何地方去——那正是这条代理存在的理由所反对的事。
    """
    import ssl as _ssl

    seen = {}

    class _OnlyKwargs:
        """记录构造参数就够了：这一条不关心请求，只关心客户端是怎么建起来的。"""
        def __call__(self, **kwargs):
            seen.update(kwargs)
            return object()

    monkeypatch.setattr(releases.httpx, "Client", _OnlyKwargs())
    releases._open_asset()
    ctx = seen.get("verify")
    assert isinstance(ctx, _ssl.SSLContext) and ctx.verify_mode == _ssl.CERT_REQUIRED
    assert ctx.check_hostname is True and seen.get("follow_redirects") is False


class _Hops:
    """假客户端：按脚本一跳一跳地回，让我们能真跑到"跟着跳转并逐跳复核"那段。"""

    class _R:
        def __init__(self, status, location=None, chunks=()):
            self.status_code, self._chunks = status, list(chunks)
            self.headers = {"location": location} if location else {}
            self.url = "https://example.invalid/req"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_bytes(self):
            return iter(self._chunks)

    def __init__(self, script):
        self.script = list(script)
        self.requested = []

    def __call__(self):
        return self                      # 当 _open_asset 的替身用

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url):
        self.requested.append(url)
        if not self.script:
            raise AssertionError("跳转过多了：脚本已经用完")
        response = self.script.pop(0)
        # 真的 httpx 会把 response.url 填成"这一跳请求的是谁"，相对 Location 就是
        # 拿它当基准解析的。假响应里留一个固定值，测出来的就是假行为。
        response.url = url
        return response


def test_fetch_asset_follows_the_hop_to_the_host_github_really_redirects_to(monkeypatch):
    """跟跳转要自己管，而且**每一跳的目标都重新过白名单**。

    这条的存在理由是一次真实翻车形状：真机量到 `browser_download_url` 在 github.com，
    而它 302 去 release-assets.githubusercontent.com；白名单只写了 objects.* 的话，
    每一次代取都在第二跳被自己拒掉，端点看起来"能用"（回 200 是回不了的，
    但静默 302 回发布页，点的人会以为自己点的链接本来就这样）。
    """
    hops = _Hops([_Hops._R(302, location="https://release-assets.githubusercontent.com/a.apk"),
                  _Hops._R(200, chunks=[b"PK\x03\x04", b"more"])])
    monkeypatch.setattr(releases, "_open_asset", hops)
    data, why = releases.fetch_asset("https://github.com/o/r/releases/download/v1/a.apk")
    assert why == "", why
    assert data == b"PK\x03\x04more", data
    assert hops.requested[-1] == "https://release-assets.githubusercontent.com/a.apk", hops.requested

    evil = _Hops([_Hops._R(302, location="https://evil.example.com/a.apk")])
    monkeypatch.setattr(releases, "_open_asset", evil)
    data, why = releases.fetch_asset("https://github.com/o/r/releases/download/v1/a.apk")
    assert data is None and "白名单" in why, f"第二跳没复核：{data!r} / {why}"

    relative = _Hops([_Hops._R(302, location="/elsewhere/a.apk"), _Hops._R(200, chunks=[b"ok"])])
    monkeypatch.setattr(releases, "_open_asset", relative)
    data, why = releases.fetch_asset("https://github.com/o/r/releases/download/v1/a.apk")
    assert data == b"ok", f"相对跳转没接住（真实响应里这种写法很常见）：{data!r} / {why}"


# ---------- 资产名对齐：工作流发的名字 = 后端挑的名字 = 壳认的名字（T1.7/T1.8） ----------

# v0.24.0 那一版曾同发两个资产名（品牌换 Fenver，新名做主名、旧名留作别名）；
# v0.24.1 起**只发新名一个**，旧名那枚改由服务端在给老壳的 JSON 里现合成。
# 这里仍留着这一对，是因为**读**的这一侧两名都还得认：0.23.x 那批发布只有旧名，
# 而 v0.24.0 是唯一一名双发的历史版本。顺序即优先级：两个都在时取新名。
ASSET_PAIR = ("fenver-", "ai-assistant-native-")


def _asset(name, url=None):
    return {"name": name, "size": 102_400,
            "browser_download_url": url or f"https://github.com/o/r/releases/download/v0.24/{name}"}


def test_the_asset_name_is_the_one_the_workflow_publishes():
    """只发一个名字这件事，钉在四份文本上（工作流 cp、工作流上传、后端、壳）。

    2026-09-25 那次漂移的教训照搬：工作流换成发原生包、前缀改成 ai-assistant-native-，
    而后端与壳还按旧名找——表现不是哪一环报错，是【官网按钮与 App 内更新双双静默退回
    发布页】，每份 JSON 都"合法"，只是没人能找到那个资产（与 v0.22 的 302 白名单同一课）。

    v0.24.1 的改动把"发几个名字"收紧成一个，所以这里的锁也跟着换边：
    * 工作流只 cp / 只 upload 新名那一份，仓库里不许再出现第二份字节相同的公开文件；
    * 后端 `PUBLISH_ASSET_PREFIXES` 必须与工作流真发的那一个逐字相同——多一个就意味着
      有一枚名字后端会去 GitHub 找、而发布流永远不产它（那是静默退回发布页的形状）；
      少一个则反之。
    * **老壳那一半挪进接口层**：旧名不再是仓库里的文件，而是 /v1/update/info 返回时
      合成的一枚条目，所以这条锁必须同时要求后端源码里有那次合成。断言本体在
      test_update_channel.py（拿真壳判断核跑的台架在 docs 里指认的那两个文件）。

    射程：`wf` 是 `run:` 的**可执行正文**（YAML 结构与行首 `#` 已由 `workflow_run_bodies` 剥掉）。
    上面每一步的注释都在解释"别名为什么撤了"，里面就带着 `legacy` 这个词——拿原文判串的话，
    删掉 `$legacy` 那行真代码、注释里留着，锁照样绿；正文这一层没有这个洞。
    """
    from pathlib import Path

    from tests.test_ci_history_depth import workflow_run_bodies

    repo = Path(releases.__file__).resolve().parents[3]
    wf = workflow_run_bodies(repo / ".github" / "workflows" / "release-apk.yml",
                             where="release-apk.yml 的 run 正文（#94 W 片）")

    staged = re.findall(r'cp "\$apk" "([^"]+)"', wf)
    assert staged == [p + "${ver}.apk" for p in releases.PUBLISH_ASSET_PREFIXES], \
        f"发布流 Stage 那步 cp 出的名字漂了：{staged}"
    # cp 一份不等于发一份：真正决定用户能看到什么的是挂上 Release 的那串参数。
    assert 'gh release upload "$tag" "$file" --clobber' in wf, \
        "重发版时多传了文件：别名那枚早就不该出现在发布页上"
    assert '"$file" --title' in wf, \
        "首发时多传了文件：同上"
    named = re.findall(r'^\s*(?:file|legacy)="([^"]+)"\s*$', wf, re.M)
    assert named == staged, f"工作流里 cp 的名字与挂出去的名字不是一对：{staged} vs {named}"
    assert "$legacy" not in wf, "工作流里还留着旧名那枚变量：别名发布没被真撤干净"

    assert releases.PUBLISH_ASSET_PREFIXES == ("fenver-",), \
        "后端认为会发出去的名字与工作流对不上"
    assert releases.ASSET_PREFIXES == ASSET_PAIR, "后端读的两个前缀漂了，要和上面一起改"
    assert releases.asset_names("0.24.0") == tuple(p + "0.24.0.apk" for p in ASSET_PAIR), \
        "后端拼出来的两个名字与顺序漂了"
    assert "alias_legacy_asset_to_self_host" in Path(releases.__file__).read_text(encoding="utf-8"), \
        "老壳那枚旧名条目的合成不在后端里：仓库只发一个新名之后，0.23.x 会看不见可装资产"
    assert releases._ASSET_NAME_RE.match("fenver-0.24.0.apk") is not None
    assert releases._ASSET_NAME_RE.match("ai-assistant-native-0.24.0.apk") is not None
    assert releases._ASSET_NAME_RE.match("totally-other-0.24.0.apk") is None, \
        "形状正则放宽到任意前缀了：它唯一的活儿就是只认这两个名字（防响应头注入）"

    plan = (repo / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever"
            / "assistant" / "core" / "ReleasePlan.java").read_text(encoding="utf-8")
    shell = tuple(re.findall(r'String (?:APK_PREFIX|LEGACY_APK_PREFIX) = "([^"]+)"', plan))
    assert shell == ASSET_PAIR, f"壳认的两个前缀与发布/后端不是同一对：{shell}"
    assert "APK_PREFIXES = {APK_PREFIX, LEGACY_APK_PREFIX}" in plan, \
        "壳里的优先级顺序漂了：两个名字都在时取哪个，就由这行字面量决定"

    # 仓名与资产名是同一条纪律的两半：改名那天正式发布的仓换了，而旧仓里那些
    # release 的 browser_download_url 永远长成旧仓的路径。后端按 candidate_repos()
    # 两个仓轮着问，壳就必须两个仓的前缀都认——只认一边都是全量断更，而且不报错。
    shell_repos = tuple(re.findall(r'String (?:REPO|LEGACY_REPO) = "([^"]+)"', plan))
    from app.core import config_store
    server_repos = tuple(r.split("/")[1] for r in
                         ([config_store.DEFAULTS["update_repo"]] + [releases.LEGACY_REPO]))
    assert set(shell_repos) == set(server_repos), \
        f"壳认的仓与后端问的仓不是一对：壳 {shell_repos} vs 服务端 {server_repos}"
    assert releases.LEGACY_REPO == "abonla599/ai-assistant", \
        "旧仓名改了等于把历史上每一个 release 的官方地址判成别人的"


def test_a_release_carrying_both_names_resolves_to_the_new_one():
    """同包两名都在：后端必须挑新名——旧名那份只是给老壳的兜底，不是第二个版本。

    反过来说更重要：这一版在 GitHub 上是两个资产、一个字节流。若挑的顺序不稳，
    官网按钮与 App 内更新可能一个给人 fenver-、一个给人 ai-assistant-native-，
    两边缓存不同步时表现就是"下了两次同一个包，还各自说自己是新的"。
    """
    old_first = {"assets": [_asset(ASSET_PAIR[1] + "0.24.0.apk"),
                            _asset(ASSET_PAIR[0] + "0.24.0.apk")]}
    picked = releases._pick_asset(old_first, "0.24.0")
    assert picked and picked["name"] == ASSET_PAIR[0] + "0.24.0.apk", picked


def test_a_release_with_only_the_legacy_name_still_resolves():
    """只挂旧名的那一版（v0.23 及更早，以及任何一次漏发别名的应急发版）照样要挑得出来。

    这条是"改名不断链"里【已装机老壳】那一半：老壳只会按旧名找，而它找的 release
    正是这一份。把它判成"没有可安装资产"不报错，只是那一代人从此停在原地。
    """
    only_old = {"assets": [_asset(ASSET_PAIR[1] + "0.23.17.apk"), {"name": "mapping.txt"}]}
    picked = releases._pick_asset(only_old, "0.23.17")
    assert picked and picked["name"] == ASSET_PAIR[1] + "0.23.17.apk", picked
    # 正对照：换个版本号就找不到——匹配仍是"名字精确对上版本"，不是"看着像 apk 就拿"。
    assert releases._pick_asset(only_old, "0.24.0") is None


def test_the_jvm_bench_and_the_shipped_core_are_one_and_the_same_copy():
    """纯 JVM 判断核只剩一份：台架编译的那批 == 出包模块里的那批，而且每个类都有用例。

    从前这条是 sha256 逐字节相等，因为 core/ 在 `android/` 与 `android-native/` 各躺一份，
    两份拷贝一定漂（digest 契约那条注释说的同一件事）。2026-10-08 WebView 壳退役之后
    "两边对齐"没有对象了，于是把它真正在守的那件事钉死：**本机三秒台架跑的就是发版
    那个模块里的源码**——台架对这份拷贝成立，不需要"前提是它一个字节没改"。

    判据从台架脚本自己嘴里读（它自己数 CORE_PKG_DIR / TEST_PKG_DIR），所以脚本的目录
    写错、改名、或者只改一边，都会当场红：目录不存在时 glob 回空集，而空集不等于清单。
    """
    import importlib.util
    from pathlib import Path

    repo = Path(releases.__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "shell_jvm_bench", repo / "tools" / "shell_jvm_tests.py")
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)                      # 只有常量与函数，main() 在 guard 里

    shipped = sorted(p.name for p in
                     (repo / "android-native" / "app" / "src" / "main" / "java" / "xyz"
                      / "fenever" / "assistant" / "core").glob("*.java"))
    assert shipped == ["ApkDigest.java", "ApkDownloader.java", "ExportName.java",
                       "ExportRedeem.java", "MiniJson.java", "ReleasePlan.java",
                       "ScheduleBoard.java", "TaskCard.java"], \
        f"上线模块的判断核与名单对不上（现在是 {shipped}）：先确认谁被加/删，再改这条"

    benched = sorted(p.name for p in bench.CORE_PKG_DIR.glob("*.java"))
    assert benched == shipped, (
        f"台架编译的 core（{benched}）与出包模块里的（{shipped}）不是同一批——"
        "台架绿不再等于上线代码绿")

    tested = sorted(p.stem[:-len("Test")] for p in bench.TEST_PKG_DIR.glob("*Test.java"))
    assert tested == [n[:-len(".java")] for n in shipped], (
        f"判断核与用例名单不再一一对应（类 {shipped} / 用例 {tested}）："
        "要么新增的类没人测，要么测的文件跟着壳一起没了")


def test_the_native_shell_never_talks_to_github_directly():
    """v0.23 T1.8 起，native 壳与 WebView 壳同一条纪律：源码里不许出现 api.github.com。

    WebView 壳那份同源锁（test_android_shell 的 "the_shell_never_talks_to_github_directly"）
    随 2026-10-08 壳退役；原生这条从那天起是唯一一份，判据没变。
    native 壳换的是同一层皮——它上一次「检查更新」还是直接 GET releases/latest，
    2026-09-23 的迅雷劫持链路在 native 里原样重演了一遍，只是这次连下载都没回自家。
    现在问与取都从 Prefs.baseUrl 现拼（/v1/update/info + /site/android.apk），
    再出现一处 GitHub API 地址 = 有人在 native 里重新接那条被拆掉的线。

    与 WebView 壳那条同款的正对照：先数够文件（路径写错时"没命中"和"守住了"长得一样），
    且注释剥完才算数（这里唯一的合法出现是 Api.kt 解释"为什么封死"的那行注释）。
    """
    from pathlib import Path
    from tests.test_apk_release_discipline import _code   # 剥注释的尺子只有一份，不抄第二遍

    src = Path(releases.__file__).resolve().parents[3] / "android-native" \
        / "app" / "src" / "main" / "java"
    files = sorted(p for p in src.rglob("*.kt") if "core" not in p.parts)
    assert len(files) >= 10, f"只扫到 {len(files)} 个 native 源码文件，这条锁多半在空转"
    hits = [p.name for p in files if "api.github.com" in _code(p)]
    assert not hits, f"native 壳又直连 GitHub 了（这些文件里出现地址）：{hits}"

    updater = _code(src / "xyz" / "fenever" / "assistant" / "nativeapp"
                    / "update" / "Updater.kt")
    assert "/v1/update/info" in updater, "native 的检查更新不再问自家端点了？"
    assert "ReleasePlan.SELF_APK_PATH" in updater, "native 的字节流不再走 /site/android.apk 了？"


# ---------- v0.23 T2.4：导出文件名与票据链路的接缝 ----------

def _repo_root():
    from pathlib import Path
    return Path(releases.__file__).resolve().parents[3]


def _decode_java_literal(s):
    """把 Java 字符串字面量的转义还原成真实字符（只需覆盖测试里用到的几种）。"""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == "u":
                out.append(chr(int(s[i + 2:i + 6], 16)))
                i += 6
                continue
            out.append({"\\":"\\", '"':'"', "r":"\r", "n":"\n", "t":"\t"}.get(nxt, nxt))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _java_exportname_cases():
    """从 ExportNameTest.java 里把 safeFilename 的用例行读出来（同 _java_compare_cases 的招）。"""
    path = (_repo_root() / "android-native" / "app" / "src" / "test" / "java" / "xyz"
            / "fenever" / "assistant" / "core" / "ExportNameTest.java")
    text = path.read_text(encoding="utf-8")
    pairs = [(_decode_java_literal(a), _decode_java_literal(b)) for b, a in
             re.findall(
                 r'assertEquals\("((?:[^"\\]|\\.)*)",\s*'
                 r'ExportName\.safeFilename\("((?:[^"\\]|\\.)*)"\)\)', text)]
    return path.name, pairs


def test_python_and_the_shell_agree_on_every_exportname_case_the_shell_tests():
    """Java ExportName 测过的清洗用例，Python _safe_filename 必须给同样的答案。"""
    from app.session.export_store import _safe_filename

    source, cases = _java_exportname_cases()
    assert len(cases) >= 8, f"从 {source} 里只读到 {len(cases)} 条用例，这条锁快空转了"
    for inp, expected in cases:
        got = _safe_filename(inp)
        assert got == expected, f"_safe_filename({inp!r}) 两边答案不同：{got!r} vs {expected!r}"


def test_exportname_mirrors_the_python_rules_verbatim():
    """清洗规则、票据形状、兜底常量：Java 侧的字面量与 Python 侧逐条对位。

    行为用例对上不等于规则同源——两条 40 截断的用例碰巧一致，正则字符集却少一个
    竖线，这种缝只有把字面量并排钉住才看得见。native 壳改走 DownloadManager 后，
    客户端算的文件名必须与服务端 Content-Disposition 的 filename* 同规则，否则
    两端下载文件悄悄改名。
    """
    from pathlib import Path
    from app.session import export_store

    java = (_repo_root() / "android-native" / "app" / "src" / "main" / "java" / "xyz"
            / "fenever" / "assistant" / "core" / "ExportName.java").read_text(encoding="utf-8")
    py = Path(export_store.__file__).read_text(encoding="utf-8")

    # 禁用字符集：Java 字符串解完转义后必须与 Python r 字面量的正则体逐字符相同
    m = re.search(r'FORBIDDEN_RUNS\s*=\s*\n?\s*Pattern\.compile\("((?:[^"\\]|\\.)*)"\)', java)
    assert m, "Java 侧找不到禁用字符正则，形状改了要两边一起改"
    java_regex = _decode_java_literal(m.group(1))
    p = re.search(r"re\.sub\(r'(\[[^']*\]\+)'", py)
    assert p, "Python 侧清洗正则找不到形状"
    assert java_regex == p.group(1), f"禁用字符集漂移：{java_regex} vs {p.group(1)}"

    # 票据形状：字节数实算对 Java 字面量 {22}，前缀对 EXPORT_PATH_PREFIX
    assert "{%d}" % export_store.TICKET_ID_CHARS in java, \
        "票据长度字面量与生成侧实算不再同值——改 TICKET_BYTES 时忘了改 Java"
    assert f'TICKET_PATH_PREFIX = "{export_store.EXPORT_PATH_PREFIX}"' in java, \
        "票据前缀两端不同值"

    # 兜底名 / 上限 / 扩展名：逐字对位
    assert 'EMPTY_FALLBACK = "对话"' in java and '(cleaned or "对话")[:40]' in py, \
        "空名兜底漂移"
    assert "NAME_LIMIT = 40" in java and '[:40]' in py, "截断上限漂移"
    assert 'MARKDOWN_EXT = ".md"' in java and "f'{name}.md'" in py, "扩展名漂移"


def test_the_native_export_row_goes_through_the_gates():
    """原生导出行的接线必须走：空会话守卫 → exportTicket → isTicketPath → DownloadManager。

    曾经的形状是把票据 URL 交给 onOpenUrl（外部浏览器）——那等于让第三个 App
    持有兑换权，也拿不到"落系统 Downloads + 已下载通知"。这条锁同时钉住：
    票据不经浏览器外流、兑换只走 DownloadManager、空会话文案与网页逐字同值。
    """
    from tests.test_apk_release_discipline import _code

    repo = _repo_root()
    ui = repo / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" \
        / "assistant" / "nativeapp" / "ui" / "SettingsUi.kt"
    body = _code(ui)
    assert 'Api.exportTicket' in body, "导出行不再签票了？"
    assert "ExportName.isTicketPath" in body, "兑换地址没有过票据形状门——服务端字段不该裸拼进下载器"
    assert "ExportName.exportFileName" in body, "落盘文件名没有走同源清洗？"
    assert "SessionDownloads.enqueue" in body, "导出不再经 DownloadManager 落系统 Downloads？"
    assert "onOpenUrl(Prefs.baseUrl" not in body, "票据 URL 又被递给外部浏览器了"

    appjs = (repo / "backend" / "app" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert '"当前没有可导出的对话"' in appjs and "当前没有可导出的对话" in body, \
        "空会话文案与网页版不再逐字同值（R2 场景 3）"

    dl = repo / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" \
        / "assistant" / "nativeapp" / "export" / "SessionDownloads.kt"
    dl_body = _code(dl)
    assert "DIRECTORY_DOWNLOADS" in dl_body and "VISIBILITY_VISIBLE_NOTIFY_COMPLETED" in dl_body, \
        "下载器不再落系统 Downloads / 不再保留完成通知（D2 的『已下载通知』）"

    manifest = (repo / "android-native" / "app" / "src" / "main" / "AndroidManifest.xml") \
        .read_text(encoding="utf-8")
    assert 'android.permission.WRITE_EXTERNAL_STORAGE' in manifest \
        and 'android:maxSdkVersion="28"' in manifest, \
        "API≤28 写公共 Downloads 需要旧存储权限，清单里没了这一条"


# ---------- v0.23 T2.5：票据过期重签与异常文案的两端接缝 ----------

def test_expired_ticket_detail_is_the_same_sentence_on_both_sides():
    """客户端识别过期用的字符串，必须与服务端回的那句逐字同值。

    判据是字节级子串查找——差一个标点，过期就会被当成普通网络失败：
    不触发自动重签、通知文案也说错原因，正是 R2 边缘case要防的事。
    """
    from pathlib import Path
    import app.main as main_mod

    java = (_repo_root() / "android-native" / "app" / "src" / "main" / "java" / "xyz"
            / "fenever" / "assistant" / "core" / "ExportRedeem.java").read_text(encoding="utf-8")
    m = re.search(r'TICKET_INVALID_DETAIL\s*=\s*"([^"]+)"', java)
    assert m, "Java 侧的过期判据字面量不见了"
    assert m.group(1) == main_mod.EXPORT_TICKET_INVALID_DETAIL, \
        (f"两端判据不同值：Java {m.group(1)!r} vs 服务端 "
         f"{main_mod.EXPORT_TICKET_INVALID_DETAIL!r}——改服务端文案必须同步 Java")


def test_the_expired_ticket_gets_exactly_one_reissue():
    """过期 → 自动重签一次；再败才报错。预算按会话记，且重签走的是签票端点。

    PRD 的原话是"票据 5 分钟过期未兑换 → 自动重新签一次，仍失败则报错"。
    两次签发之间没有别的凭据面可钻：预算 set 加过一次就不再进重签分支。
    """
    from pathlib import Path
    from tests.test_apk_release_discipline import _code

    tracker = (_repo_root() / "android-native" / "app" / "src" / "main" / "java"
               / "xyz" / "fenever" / "assistant" / "nativeapp" / "export"
               / "ExportTracker.kt")
    body = _code(tracker)
    assert "Api.exportTicket(sid)" in body, "重签没有回到签票端点？"
    assert "retriedSids.add(sid)" in body and "SessionDownloads.enqueue" in body, \
        "过期之后没有『重签→重新入队』这一拍"
    assert "ExportRedeem.looksExpired" in body, "没有先认服务端那句 detail 再决定重签？"
    assert "retriedSids" in body, "重签预算的记账不见了——套娃重签就回来了"
    # DownloadManager 压根没有 ERROR_HTTP 这个常量（第一次 CI 构建就是这么红的）：
    # HTTP 失败时 COLUMN_REASON 直接就是状态码。只认 4xx/5xx，本地失败原因
    # （空间不足/取消/重试过多，都落在 1xx~3xx）不许白占重签预算。
    assert "ERROR_HTTP" not in body, "DownloadManager 不导出 ERROR_HTTP，别再引回去"
    assert "reason in 400..599" in body, "读不出失败体时的 HTTP 判据不见了"


def test_the_permission_denied_fallback_names_the_private_path():
    """≤28 拒权 → 先弹一次系统授权；仍拒则落 App 私有目录并把路径说全（R2 边缘）。

    这一格防的是"默默降级"：文件存到了人找不到的地方却不告诉人在哪，
    等于没导出。所以路径必须进用户可见的文案，且兜底兑换依旧走匿名票据
    （fetchTicketBytes 不带 Authorization 且自带形状门）。
    """
    from pathlib import Path
    from tests.test_apk_release_discipline import _code

    ui = _code(_repo_root() / "android-native" / "app" / "src" / "main" / "java"
               / "xyz" / "fenever" / "assistant" / "nativeapp" / "ui" / "SettingsUi.kt")
    assert "ExportTracker.hasLegacyStorage" in ui, "≤28 的存储权限门不见了？"
    assert "storageAsk.launch(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)" in ui, \
        "拒权路径不再先征求一次授权？"
    assert "viaPrivate = !granted" in ui and "已导出到应用私有目录" in ui, \
        "拒绝后没有落私有目录并把路径念给用户"
    assert "网络似乎不通" in ui, "断网那一格又没有专属文案了"

    api = _code(_repo_root() / "android-native" / "app" / "src" / "main" / "java"
                / "xyz" / "fenever" / "assistant" / "nativeapp" / "Api.kt")
    assert "fetchTicketBytes" in api and "ExportName.isTicketPath(path)" in api, \
        "兜底字节路没有复用票据形状门？"
    tracker = _code(_repo_root() / "android-native" / "app" / "src" / "main" / "java"
                    / "xyz" / "fenever" / "assistant" / "nativeapp" / "export"
                    / "ExportTracker.kt")
    assert "Api.fetchTicketBytes" in tracker and "getExternalFilesDir" in tracker, \
        "私有目录兜底的落盘点不见了？"
