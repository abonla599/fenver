"""v0.36.2 嵌入密钥单源：`EMBEDDINGS_PROVIDER_ID` 指向的 provider 是第一来源。

形状（判据全在 `memory_manager.py` 与 `core/selfcheck.py`）：

* 设了 id 且该 provider 有非占位密钥 → 用它，日志只写"嵌入密钥来自 provider 配置"，
  **绝不写 key/label/model**；
* 过渡期兼容：id 已设但 provider 缺失/密钥空/仍是占位 → .env 的 `api_key` 仍有值则
  回落并打一条响亮警告；两者都没有才走本地/伪嵌入。任何一条都不许让
  `MemoryManager.__init__` 抛异常；
* 没设 id → 仍旧只读 .env `api_key`（旧部署与既有测试的形状不变）；
* 自检口径：密钥来源（provider 或 env 任一有值）+ 地址 + 模型名**三者齐全**才算
  已配置——旧 `not any(...)` 只在三项全缺时才说"没配"，半配状态会被谎称已配置。

现网记忆库是 1024 维 22 条：provider 解析失败后落到 384 维伪嵌入会触发维度冲突、
记忆 503。所以"provider 缺失就降级"只是不许崩，**不是正常路径**——发布前必须验证
provider 那条路真的可用（见 docs/releases/v0.36.2.md 的验收段）。

回退必红自查：把 memory_manager 的 provider 分支删掉，用例 1 红；把过渡回落的警告
删掉，用例 2/3 红；把 selfcheck 改回 `not any(...)`，齐备性用例里"缺一项"的参数组红。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.core.providers as providers_module
import app.memory.memory_manager as mm
from app.core import logsanitizer, selfcheck

EMBED_ENV = ("api_key", "EMBEDDINGS_BASE_URL", "EMBEDDINGS_MODEL",
             "EMBEDDINGS_PROVIDER_ID")

PROVIDER_KEY = "prov-key-1234567890abcdef"
ENV_KEY = "env-key-0000111122223333"
PLACEHOLDER_KEY = "sk-your-xxxx-placeholder"
CANARY_MODEL = "canary-embed-model-9k"


@pytest.fixture(autouse=True)
def _clean_embed_env(monkeypatch):
    """每条用例从"嵌入四键全未设"出发：开发机 .env 里可能真有值。"""
    for key in EMBED_ENV:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _no_local_model(monkeypatch):
    """挡住本地 sentence-transformers：落到本地还是伪嵌入由用例自己定，
    不许取决于本机装没装模型。"""
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)


@pytest.fixture(autouse=True)
def _sanitizer_terms_untouched():
    """云端分支会 register 测试模型名进进程级脱敏表，跑完原样还回去（同
    test_memory_real_branches 的搭法）。"""
    before = logsanitizer.registered_terms()
    yield
    logsanitizer.clear_terms_for_tests()
    for term in before:
        logsanitizer.register(term)


class _FakeOpenAI:
    """假 OpenAI：记下构造与探发参数，绝不出网。"""

    def __init__(self, bucket):
        self._bucket = bucket

    def __call__(self, **kwargs):
        self._bucket.update(kwargs)
        return _Client(self._bucket)


class _Client:
    def __init__(self, bucket):
        self._bucket = bucket

    def with_options(self, **kwargs):
        return self

    @property
    def embeddings(self):
        bucket = self._bucket

        class _E:
            def create(self, **kwargs):
                bucket["probe_model"] = kwargs.get("model")
                return SimpleNamespace(
                    data=[SimpleNamespace(embedding=[0.1, 0.2, 0.3, 0.4])])
        return _E()


class _FakeStore:
    """替掉 memory_manager 模块绑定的那份 store：get 返回给定记录或抛错。"""

    def __init__(self, record=None, raises=False):
        self._record = record
        self._raises = raises

    def get(self, pid):
        if self._raises:
            raise RuntimeError("provider store 读不动")
        return self._record


def _boot(monkeypatch, tmp_path, bucket, store=None):
    """按给定环境起一次 MemoryManager（云端三件里地址/模型名恒配好）。"""
    monkeypatch.setenv("EMBEDDINGS_BASE_URL", "https://embed.invalid/v1")
    monkeypatch.setenv("EMBEDDINGS_MODEL", CANARY_MODEL)
    if store is not None:
        monkeypatch.setattr(mm, "provider_store", store)
    monkeypatch.setattr(mm, "OpenAI", _FakeOpenAI(bucket))
    return mm.MemoryManager(persist_dir=str(tmp_path / "chroma"))


# ---------- 1. provider 是第一来源 ----------

def test_provider_key_wins_when_both_sources_have_a_key(tmp_path, monkeypatch, capsys):
    """双 key 时 provider 优先：OpenAI 客户端拿到的是 provider 那把。"""
    monkeypatch.setenv("EMBEDDINGS_PROVIDER_ID", "embed-main")
    monkeypatch.setenv("api_key", ENV_KEY)
    bucket = {}
    manager = _boot(monkeypatch, tmp_path, bucket,
                    _FakeStore({"api_key": PROVIDER_KEY}))
    assert bucket.get("api_key") == PROVIDER_KEY
    assert bucket.get("probe_model") == CANARY_MODEL
    assert manager._dummy_embed is False and manager.use_local_embed is False

    out = capsys.readouterr().out
    assert "嵌入密钥来自 provider 配置" in out, out
    assert "过渡期" not in out, "provider 密钥可用却还报了回落警告"
    # 日志不含 key：两把都不许出现。
    assert PROVIDER_KEY not in out and ENV_KEY not in out


# ---------- 2/3. 过渡期回落 + 响亮警告 ----------

def test_unusable_provider_key_falls_back_to_env_with_loud_warning(
        tmp_path, monkeypatch, capsys):
    """三种"拿不出可用密钥"（缺失/空/占位）都回落 env 并警告——绝不静默双源。"""
    for i, record in enumerate((None, {"api_key": ""}, {"api_key": PLACEHOLDER_KEY})):
        for key in EMBED_ENV:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("EMBEDDINGS_PROVIDER_ID", "embed-main")
        monkeypatch.setenv("api_key", ENV_KEY)
        bucket = {}
        manager = _boot(monkeypatch, tmp_path / f"fb{i}", bucket,
                        _FakeStore(record))
        assert bucket.get("api_key") == ENV_KEY, f"{record} 形状没有回落到 env"
        assert manager._dummy_embed is False
        out = capsys.readouterr().out
        assert "过渡期" in out and "EMBEDDINGS_PROVIDER_ID" in out, \
            f"{record} 形状回落时没打响亮警告：{out}"
        assert PROVIDER_KEY not in out and ENV_KEY not in out, "回落日志漏了明文 key"


def test_both_sources_empty_goes_local_or_dummy_without_raising(
        tmp_path, monkeypatch, capsys):
    """id 已设、provider 无可用密钥、env 也没有 → 本地/伪嵌入分支，初始化不抛。"""
    monkeypatch.setenv("EMBEDDINGS_PROVIDER_ID", "embed-main")
    monkeypatch.setattr(mm, "provider_store", _FakeStore(None))
    monkeypatch.setattr(mm, "OpenAI", _FakeOpenAI({}))
    manager = mm.MemoryManager(persist_dir=str(tmp_path / "chroma"))
    assert manager._dummy_embed is True   # 本地模型已被 fixture 挡掉
    out = capsys.readouterr().out
    assert "未配置云端嵌入" in out and "降级为伪嵌入" in out


def test_store_read_error_never_breaks_initialization(tmp_path, monkeypatch, capsys):
    """provider store 整个读不动（抛异常）也只是按无密钥处理，不许把后端拖崩。"""
    monkeypatch.setenv("EMBEDDINGS_PROVIDER_ID", "embed-main")
    bucket = {}
    manager = _boot(monkeypatch, tmp_path, bucket, _FakeStore(raises=True))
    out = capsys.readouterr().out
    assert "读取 provider 配置失败" in out, out
    assert bucket == {}, "store 抛错后竟还进了云端分支"
    assert manager._dummy_embed is True


# ---------- 4. 未设 id：旧路径 ----------

def test_without_provider_id_the_env_key_is_the_only_source(tmp_path, monkeypatch, capsys):
    """没设 id 时行为与 v0.36.1 逐字一致：只读 .env api_key，不提 provider。"""
    monkeypatch.setenv("api_key", ENV_KEY)
    bucket = {}
    manager = _boot(monkeypatch, tmp_path, bucket)   # 不换 store：真 store 也不该被碰
    assert bucket.get("api_key") == ENV_KEY
    assert manager._dummy_embed is False
    out = capsys.readouterr().out
    assert "provider 配置" not in out
    assert "过渡期" not in out


# ---------- 5. 日志不含 key（独立重申一遍最坏形状） ----------

def test_no_log_line_across_all_paths_contains_a_key(tmp_path, monkeypatch, capsys):
    """四条路径的日志里都找不到任何一把 key 的明文。"""
    cases = [
        {"api_key": PROVIDER_KEY},
        None,
        {"api_key": PLACEHOLDER_KEY},
    ]
    for i, record in enumerate(cases):
        for key in EMBED_ENV:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("EMBEDDINGS_PROVIDER_ID", "embed-main")
        monkeypatch.setenv("api_key", ENV_KEY)
        _boot(monkeypatch, tmp_path / f"leak{i}", {}, _FakeStore(record))
        out = capsys.readouterr().out
        for forbidden in (PROVIDER_KEY, PLACEHOLDER_KEY, ENV_KEY):
            assert forbidden not in out, f"{forbidden} 明文进了日志"


# ---------- 6. 自检口径：三者齐全才算已配置 ----------

def _patch_store_for_selfcheck(monkeypatch, record):
    monkeypatch.setattr(providers_module, "store", _FakeStore(record))


def test_selfcheck_configured_requires_key_base_and_model_all_three(monkeypatch):
    """缺任意一件都算"没配"；三件齐备才算已配置。旧 not any(...) 口径的红处。"""
    for key in EMBED_ENV:
        monkeypatch.delenv(key, raising=False)
    _patch_store_for_selfcheck(monkeypatch, None)

    assert selfcheck._embeddings_unconfigured() is True

    # 只配了密钥：地址与模型名仍缺 → 依旧没配（旧口径会误判"已配置"）。
    monkeypatch.setenv("api_key", ENV_KEY)
    assert selfcheck._embeddings_unconfigured() is True
    monkeypatch.setenv("EMBEDDINGS_BASE_URL", "https://embed.invalid/v1")
    assert selfcheck._embeddings_unconfigured() is True
    monkeypatch.setenv("EMBEDDINGS_MODEL", CANARY_MODEL)
    assert selfcheck._embeddings_unconfigured() is False, "三件齐备还被判没配"

    # 撤掉任意一件又回到没配。
    for drop in ("api_key", "EMBEDDINGS_BASE_URL", "EMBEDDINGS_MODEL"):
        monkeypatch.delenv(drop)
        assert selfcheck._embeddings_unconfigured() is True, f"缺 {drop} 仍被判已配置"
        monkeypatch.setenv(drop, ENV_KEY if drop == "api_key" else "x")


def test_selfcheck_accepts_provider_as_the_key_source(monkeypatch):
    """单源正道：env 一个嵌入变量都不配，id 指向的 provider 有非占位密钥即算有 key。"""
    for key in EMBED_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EMBEDDINGS_PROVIDER_ID", "embed-main")
    monkeypatch.setenv("EMBEDDINGS_BASE_URL", "https://embed.invalid/v1")
    monkeypatch.setenv("EMBEDDINGS_MODEL", CANARY_MODEL)

    _patch_store_for_selfcheck(monkeypatch, {"api_key": PROVIDER_KEY})
    assert selfcheck._embeddings_unconfigured() is False

    _patch_store_for_selfcheck(monkeypatch, {"api_key": PLACEHOLDER_KEY})
    assert selfcheck._embeddings_unconfigured() is True, "占位密钥被当成了已配置"

    _patch_store_for_selfcheck(monkeypatch, None)
    assert selfcheck._embeddings_unconfigured() is True, "provider 不存在还被当成已配置"

    # 过渡期回落：provider 缺失但 env api_key 还在 → 仍算有密钥来源。
    monkeypatch.setenv("api_key", ENV_KEY)
    assert selfcheck._embeddings_unconfigured() is False


def test_selfcheck_survives_a_broken_provider_store(monkeypatch):
    """判据读不动不许把自检弄崩：store.get 抛异常时落到 env 那一档。"""
    for key in EMBED_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EMBEDDINGS_PROVIDER_ID", "embed-main")
    _patch_store_for_selfcheck(monkeypatch, _FakeStore(raises=True))
    assert selfcheck._embeddings_unconfigured() is True
    monkeypatch.setenv("api_key", ENV_KEY)
    monkeypatch.setenv("EMBEDDINGS_BASE_URL", "https://embed.invalid/v1")
    monkeypatch.setenv("EMBEDDINGS_MODEL", CANARY_MODEL)
    assert selfcheck._embeddings_unconfigured() is False
