"""测试进程不得碰到用户真实数据。

conftest 把会话、身份库、provider、附件目录都指进了临时目录，唯独长期记忆漏了：
`app/memory/memory_router.py` 在导入时构造的 `MemoryManager()` 会去看 `CHROMA_DB_PATH`，
而没人设它——于是本机跑测试时那 71 条真实记忆就在射程内。

这不是假想的担心。2026-09-19 有人拿临时写的 TestClient 冒烟测 `POST /v1/memory/decay`，
把 default_user 全部记忆的权重原地乘了 0.95：接口回 200，日志写着"已衰减 58 条"，
而那是一次**没有任何测试为它负责**的写入。衰减不夹逼、也没有反向还原的端点，
只能再乘 1/0.95 手工做回去。

CI 上这个问题看不见（`CI=true` 走 FakeMemoryStore），所以只有本机会中招——
而中招时测试照样全绿：改数据的那条请求是 200，没有任何一条断言为它负责。
这里钉的就是第四种信号：测试进程看见的记忆库，必须是它自己的。
"""
import os
from pathlib import Path

import pytest

from app.memory.memory_manager import _default_persist_dir

# 本文件位于 backend/tests/，向上两层即仓库根
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_real_memory_store_is_not_what_tests_open():
    """默认解析出来的记忆库路径，不能是仓库里那份真实的 chroma_db。

    判据写成"不等于真实目录"而不是"必须在某处之下"：后者会把将来合法的重定向
    （比如 data/ 下另起一份）一并判红，而真正要守的只有"别碰用户那一份"。
    """
    resolved = Path(_default_persist_dir()).resolve()
    real = (REPO_ROOT / "chroma_db").resolve()
    assert resolved != real, (
        f"测试打开的是用户真实的长期记忆库 {real}：任何一条用例调 add/delete/decay "
        "都会改到真数据上。conftest 需要设 CHROMA_DB_PATH。")


def _redirectable_env_vars():
    """派生清单：产品代码声明可被环境变量指走的每一份存储。

    唯一来源是 `paths.DATA_PATH_ENV_VARS`，不再手抄：这一处此前抄过一遍，而产品
    代码每加一份存储都要靠人记得来这儿补一行——不补就正好是这次的 bug 形状
    （CHROMA_DB_PATH 漏了指走，本机测试直接摸到用户那 71 条真实记忆；v0.34.3 的
    APK_CACHE_DIR / RELEASE_MANIFEST_PATH 又是一次同形漏网）。派生之后，加存储而
    忘在 conftest 补一行，红的就是这一条。

    反馈与偏好那两份例外：它们在导入期就算成模块常量，conftest 走的是
    `monkeypatch.setattr(module, "FEEDBACK_FILE", …)`，环境变量指不走它们。
    """
    from app.core.paths import DATA_PATH_ENV_VARS

    module_constanted = {"FEEDBACK_FILE", "PREFERENCE_FILE"}
    return [v for v in DATA_PATH_ENV_VARS.values() if v not in module_constanted]


def _isolation_gaps(environ, repo_root=REPO_ROOT):
    """(没被指走的, 被指进仓库里的) 两份清单；空 = 隔离墙完整。

    写成纯函数是为了让反例直接喂一份合成环境进来——真去 delenv 再跑整个会话，
    等于用"把墙上那块砖抽掉"来证明锁有牙，代价落在后续用例身上。
    """
    missing, inside = [], []
    for var in _redirectable_env_vars():
        value = str(environ.get(var) or "").strip()
        if not value:
            missing.append(var)
        elif Path(value).resolve().is_relative_to(Path(repo_root).resolve()):
            inside.append(var)
    return missing, inside


def test_env_isolation_covers_every_redirectable_store():
    """conftest 声称能指走的存储，就得真的指走了。

    判据本体在 `_isolation_gaps`，清单从 `paths.DATA_PATH_ENV_VARS` 派生（理由见
    上面那个 helper）。这里只拿真实进程环境问一次。
    """
    covered = _redirectable_env_vars()
    assert len(covered) >= 9, f"派生出来的清单短得不像话，这条锁在空转：{covered}"
    # v0.34.3 的两块新砖：名单若被摘短（或派生源里删了它们），这里先红。
    assert {"APK_CACHE_DIR", "RELEASE_MANIFEST_PATH"} <= set(covered), covered

    missing, inside = _isolation_gaps(os.environ)
    assert not missing, f"这些存储在跑测试时没有被指走：{missing}"
    assert not inside, (
        f"这些变量被指进了仓库内，重建/提交都会牵连到真实数据：{inside}")


@pytest.mark.parametrize("var", ["APK_CACHE_DIR", "RELEASE_MANIFEST_PATH"])
def test_the_isolation_lock_goes_red_when_a_release_hook_is_unpinned(var):
    """反例：抽掉发版物料的任一只口子，派生锁必须当场点名它。

    没有这一条，`_isolation_gaps` 少列了谁都不会有人知道——上面那条拿真实环境
    问，而真实环境里只要 variable 还在，名单短一截也照样绿。
    """
    environ = dict(os.environ)
    environ.pop(var, None)
    missing, _inside = _isolation_gaps(environ)
    assert var in missing, f"{var} 被摘掉后锁没报红：{missing}"


@pytest.mark.parametrize("var, real", [
    ("RELEASE_MANIFEST_PATH", Path("data") / "version.json"),
    ("APK_CACHE_DIR", Path("data") / "apk"),
])
def test_the_isolation_lock_goes_red_when_a_release_hook_points_into_the_repo(var, real):
    """反例的另一半：口子还在、但指进仓库——锁必须报"指进仓库"而不是绿。"""
    environ = dict(os.environ)
    environ[var] = str(REPO_ROOT / real)
    _missing, inside = _isolation_gaps(environ)
    assert var in inside, f"{var} 指进仓库后锁没报红：{inside}"


def test_release_materials_resolve_away_from_the_real_data_tree():
    """与 `test_real_memory_store_is_not_what_tests_open` 同形：解析结果不许是真落点。

    判据写成"不等于真实路径"而不是"必须在某处之下"：将来合法重定向（比如把
    APK 缓存挪到别的盘）不该被一并判红，真正要守的只有"别碰线上那一份"。
    """
    from app.core import apk_cache, releases

    cases = (
        ("发布清单", releases.runtime_manifest_path(), REPO_ROOT / "data" / "version.json"),
        ("安装包缓存", apk_cache.cache_dir(), REPO_ROOT / "data" / "apk"),
    )
    for label, resolved, real in cases:
        assert Path(resolved).resolve() != Path(real).resolve(), (
            f"测试解析到的{label}是线上真实落点 {real}：构造假清单/假包会直接写进"
            "线上 data/。conftest 需要用对应的环境变量指走。")


@pytest.mark.parametrize("endpoint", ["/v1/memory/list", "/v1/memory/stats"])
def test_memory_reads_do_not_create_a_real_collection(client, endpoint):
    """连"读"也要在隔离库上跑。

    真实 chroma 是 PersistentClient：一打开就可能 get_or_create_collection，
    在用户的数据目录里落下新文件。这条用例不校验业务结果，只校验跑完之后
    仓库那份 chroma_db 的文件清单没有变长。
    """
    before = _tree(REPO_ROOT / "chroma_db")
    client.get(endpoint)
    after = _tree(REPO_ROOT / "chroma_db")
    assert after == before, f"一次测试请求改动了真实记忆库目录：{after ^ before}"


def _tree(path: Path):
    if not path.exists():
        return frozenset()
    return frozenset(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file())


def test_decay_via_default_client_cannot_reach_the_real_store(client):
    """把 2026-09-19 那次事故的原样钉住：一条冒烟请求把真实记忆全池权重乘了 0.95。

    修好之后这条必然绿——它守的不是"衰减能工作"，而是"以后别再绿得那么侥幸"。
    哪天真数据又被碰到了，它会是第一条红的，而且红在事发的那一次调用上，
    不是红在几小时后发现"我的记忆排序怎么变了"。
    """
    import sqlite3

    real_db = REPO_ROOT / "chroma_db" / "chroma.sqlite3"
    if not real_db.exists():
        pytest.skip("本机没有真实记忆库，无从证明没被改")

    def weights():
        con = sqlite3.connect(f"file:{real_db.as_posix()}?mode=ro", uri=True)
        try:
            return sorted(con.execute(
                "select id, float_value from embedding_metadata where key='weight'"))
        finally:
            con.close()

    before = weights()
    resp = client.post("/v1/memory/decay?decay_factor=0.5")
    # 先证明这次请求真的执行了衰减：只断言"数据没变"的话，一次 503 也算通过，
    # 那条断言就成了空转（与上面"这一轮连偏好分析都没跑"同一类毛病）。
    assert resp.status_code == 200, f"请求没走到写入，这条断言就成了空转：{resp.status_code}"
    assert weights() == before, "测试客户端把衰减写进了用户真实的记忆池"


def test_fresh_provider_store_is_not_seeded_from_the_local_env(tmp_path):
    """空路径上的 ProviderStore 必须是空的——不许被开发机那份 .env 播种。

    2026-09-26 的形状：三条断言"私有记录不越权 / 首条不自动当默认 / 别人的私有 id
    回落站级默认"的用例，在带真 `.env` 的本机全红、在 CI 上全绿。根因不在那三条断言，
    而在 `ProviderStore._load()` 撞没见过文件时走 `_seed_from_env()`，把
    `DEEPSEEK_API_KEY` 播种成一条 `is_default` 记录，于是"回落默认"回落到的
    是这台机器自己的真凭据。同一份代码在两种机器上给出相反结果，比红更糟——
    红会被人按"我机器的问题"忽略掉。

    这条用例在 CI 上恒绿（那里本来就没有 .env），它的价值在本机：conftest 一旦
    忘了清这颗变量，它第一时间红，并且红在原因上而不是红在三条不相干的断言上。
    """
    from app.core import providers

    store = providers.ProviderStore(path=str(tmp_path / "providers.json"))
    assert store.all() == [], (
        "测试进程里的 ProviderStore 被本机 .env 播种成了真模型："
        f"{[p.get('id') for p in store.all()]}。conftest 需要把 DEEPSEEK_API_KEY 置空。")


def test_uvicorn_loggers_still_propagate_after_the_whole_suite():
    """uvicorn 那两个 logger 的 propagate 必须还是 True：断在 False 上，caplog 收零条。

    2026-10-03 的形状：新加一条起真 uvicorn 的端到端用例之后，落在它后面的
    test_log_sanitizer 里那条"访问行必须打码"变红，红出来的话是
    `assert '●已脱敏●' in ''` ——看着像脱敏器坏了，实际是 caplog 一条都没收到。
    根因：uvicorn 一起来就把 uvicorn / uvicorn.access 的 propagate 置 False（写死在
    它默认日志配置里，`log_config=None` 也挡不住），而 pytest 的 caplog 挂在 root 上。
    套件是字母序，这条用例的名字排在 test_log_sanitizer 前面，所以它**每次**都会把
    后面那条弄红——不是偶发，是刚加进来的那一晚本机全量才第一次露出来。

    起服务方的义务是进出各存一份、还原一份（见该文件的 live_server 夹具）。这条判据
    不看谁做的：任何新用例借用全局日志状态不还，都会在这里先红，而不是把不相干的
    用例拖下水。
    """
    import logging

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).propagate, (
            f"{name}.propagate 被某条用例改成 False 了：caplog 从此收不到它的任何记录，"
            "红会红在离真凶很远的用例上。起 uvicorn 的用例必须自己还原（先例见 "
            "test_live_trace_relay_v0292.py 的 live_server 夹具）。")
