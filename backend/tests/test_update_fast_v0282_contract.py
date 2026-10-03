"""v0.28.2「检查更新要等半天 / 打开软件很久出不了模型」的两条快线。

判据分两头：
1. 服务端 releases 缓存的**过半补货**——手里有货就先交货，出网这一腿搬到后台单飞；
   收紧的只有"什么时候去拉"，「过期旧快照不许冒充最新」（AC-4）不许被顺手放松；
2. 壳侧元数据 GET 的**快线与重试**——5 分钟读超时是流式那条路的，几 KB 的 JSON
   不该陪它挂死。这一半是 Compose 侧的 Kotlin，JVM 台架够不着，用同源剥注释的
   尺子（test_android_shell._code）钉源码形状。

另钉 lifespan 接了 warmer：测试进程的 TestClient 不走 lifespan，真起线程发生在
部署进程里，所以这里钉**接线本身**加幂等闩的行为。
"""
import re
import time
import types
import urllib.error
from pathlib import Path

import pytest

from app.core import releases
from tests.test_android_shell import _code
from tests.test_release_probe import GOOD, _Urlopen

BACKEND = Path(releases.__file__).resolve().parents[2]        # backend 目录
ROOT = Path(releases.__file__).resolve().parents[3]           # 仓库根
NATIVE = ROOT / "android-native" / "app" / "src" / "main" / "java" / \
    "xyz" / "fenever" / "assistant" / "nativeapp"


@pytest.fixture(autouse=True)
def cold_cache():
    releases.reset_for_tests()
    yield
    releases.reset_for_tests()


GOOD_NEWER = dict(GOOD)
GOOD_NEWER["tag_name"] = "v0.19"


# ---------- 服务端：过半补货 ----------

def test_a_half_old_snapshot_is_served_now_and_the_trip_happens_off_thread(monkeypatch):
    """缓存过半没过期：问的人**立刻**拿到手里的货，补货那一腿在后台、绝不同步出网。"""
    spawned = []
    monkeypatch.setattr(releases, "_spawn_refresh", lambda: spawned.append(True))
    fake = _Urlopen(GOOD, GOOD_NEWER)
    monkeypatch.setattr(releases.urllib.request, "urlopen", fake)

    assert releases.probe(have="0.16")["latest"] == "0.18"   # 冷：这一趟是应拉的同步
    assert len(fake.calls) == 1
    # 时间旅行到"过半但没过期"：这一问不该再碰网络，但要把后台那一腿踢出去
    releases._fetched_at = time.monotonic() - releases.REFRESH_AHEAD_SECONDS
    out = releases.probe(have="0.16")
    assert out["latest"] == "0.18" and out["ok"]
    assert len(fake.calls) == 1, "过半补货把出网搬回了提问者线程上"
    assert spawned == [True], "该踢的后台补货没踢"
    # 单飞：补货在途时再来一问，不再踢第二腿（也不许同步出网）
    releases._refreshing = True
    releases._fetched_at = time.monotonic() - releases.REFRESH_AHEAD_SECONDS
    releases.probe(have="0.16")
    assert spawned == [True], "补货在途又踢了一腿：单飞闩没生效"


def test_the_background_refresh_swaps_the_snapshot_in_place(monkeypatch):
    """后台那一腿回来的新货必须真换成下一个人手里的货；失败则旧货原样留着、闩落下。"""
    monkeypatch.setattr(releases, "_spawn_refresh", lambda: None)
    fake = _Urlopen(GOOD, GOOD_NEWER)
    monkeypatch.setattr(releases.urllib.request, "urlopen", fake)
    releases.probe(have="0.16")
    releases._refresh_now()                       # 替后台把那一腿当场跑完
    assert releases.probe(have="0.16")["latest"] == "0.19", "补回来的新货没接进快照"

    monkeypatch.setattr(releases.urllib.request, "urlopen",
                        _Urlopen(urllib.error.URLError("boom")))
    releases._fetched_at = time.monotonic() - releases.REFRESH_AHEAD_SECONDS
    releases.probe(have="0.16")                   # 踢腿（spawn 已被换成 no-op）
    releases._refresh_now()
    assert releases.probe(have="0.16")["latest"] == "0.19", "后台失败把手里的货弄丢了"
    assert releases._refreshing is False, "补货的闩没落下：以后永远不再补货"


def test_refresh_ahead_never_resurrects_an_expired_snapshot(monkeypatch):
    """AC-4 原样在：过半个期可以拿"还新鲜"的货先答；过一整个期，谁问都不给。"""
    monkeypatch.setattr(releases, "_spawn_refresh", lambda: None)
    fake = _Urlopen(GOOD, urllib.error.URLError("down"))
    monkeypatch.setattr(releases.urllib.request, "urlopen", fake)
    releases.probe(have="0.16")
    releases._payload_at -= releases.CACHE_SECONDS + 1        # 成功读数过期——货放馊了
    releases._fetched_at = time.monotonic()                   # 尝试时间却还是新的
    manifest, why = releases.latest_release_manifest("")
    assert manifest is None and "不把旧快照冒充最新" in why


# ---------- 服务端：warmer ----------

def test_the_warmer_is_wired_into_lifespan():
    """main.py 的 lifespan 必须真起这条线程——放在调度器之前，冷启动越早攒货越好。"""
    src = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    body = src[src.index("async def lifespan"):]
    body = body[:body.index("start_background_scheduler()")]
    assert "start_cache_warmer()" in body, "warmer 没接进 lifespan"


def test_starting_the_warmer_twice_starts_one_thread(monkeypatch):
    started = []

    class _Thread:
        def __init__(self, target=None, daemon=None, name=""):
            self._name = name

        def start(self):
            started.append(self._name)

    stub = types.SimpleNamespace(Thread=_Thread,
                                 Lock=getattr(releases.threading, "Lock"))
    monkeypatch.setattr(releases, "threading", stub)
    monkeypatch.setattr(releases, "_warmer_started", False)
    releases.start_cache_warmer()
    releases.start_cache_warmer()
    assert started == ["release-warm"], f"warmer 起了 {len(started)} 条线程"


def test_the_warmer_loop_fetches_through_the_single_cache_entry(monkeypatch):
    """warmer 的一轮 = _snapshot 一次：冷则同步攒货，热且过半只踢后台一脚——
    走唯一入口，才不会出现第二份"什么时候该拉"的口径。"""
    calls = []
    monkeypatch.setattr(releases, "_snapshot", lambda: calls.append(len(calls)))

    def _boom(_seconds):
        raise StopIteration("loop tick")

    monkeypatch.setattr(releases, "time",
                        types.SimpleNamespace(sleep=_boom, monotonic=time.monotonic))
    with pytest.raises(StopIteration):
        releases._warm_loop()
    assert calls == [0], "warmer 的一轮没有走 _snapshot 这唯一入口"


# ---------- 壳侧：元数据 GET 快线 ----------

def test_metadata_gets_travel_on_the_fast_lane_with_one_retry():
    api = _code(NATIVE / "Api.kt")
    assert "private val metaClient = OkHttpClient.Builder()" in api, "元数据快线没了"
    assert re.search(r"metaClient = OkHttpClient\.Builder\(\)[\s\S]{0,240}?"
                     r"readTimeout\(15, TimeUnit\.SECONDS\)", api), \
        "快线读超时不是 15 秒档"
    assert 'if (req.method == "GET") return@withContext getMeta(req)' in api, \
        "GET 不再走快线"
    assert "metaClient.newCall(req)" in api and "for (attempt in 0 until 2)" in api, \
        "快线的一次重试没了"
    assert "catch (e: java.io.IOException)" in api, "重试没按'只重网络层失败'收口"
    # 大超时那条线还得给流式与字节用：两处都在才算各走各的
    assert "readTimeout(5, TimeUnit.MINUTES)" in api, "流式的 5 分钟档被一起改掉了"
    assert "client.newCall(rb.build()).execute()" in api, "非 GET 被挪进快线了"


def test_a_slow_model_load_says_so_instead_of_bouncing_silently():
    chat = _code(NATIVE / "ui" / "ChatUi.kt")
    assert "if (providers.isEmpty()) {" in chat and 'setStatus("正在读取模型清单…")' in chat, \
        "发送键按下时清单未回，不再先说一句就静默补拉"


def test_the_settings_page_stops_misreading_a_loading_list_as_no_models():
    ui = _code(NATIVE / "ui" / "SettingsUi.kt")
    assert "modelsLoading: Boolean" in ui, "读取中态没有接到设置列表"
    assert "modelsLoading = !modelsLoaded" in ui
    assert '"正在读取模型清单…"' in ui, "模型服务那行还是会把加载中报成没有模型"
    assert '"读取中…"' in ui, "当前模型那行还是会把加载中报成未配置"
    # 加载真失败之后那句诚实的话还得在——不许改成"永远加载中"糊过去
    assert '"服务端还没有可用的模型"' in ui
