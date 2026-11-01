"""v0.39.1 Task 11：思考参数候选配方的映射锁 + 探针的 dry-run 锁。

这个文件里**一次真实出站调用都没有**。它是发那 4 次之前的唯一防线，所以每条判据都要
先证明自己能报红——探针的绿灯本身不值钱，"这条判据认得出假绿灯"才值钱。

钉住的四件事，各配反例：

1. **映射表**（`thinking_profiles.probe_kwargs`）：三档标签各映射到什么、未知 profile /
   未知档位必须 `ValueError`（不联网就退出）、`none` 不发任何参数、返回的是深拷贝、
   候选表不许混进生产白名单。
2. **闸 2（绝不写 data/）**：`providers` 只能在隔离之后 import；隔离目录不许在仓库内、
   不许与只读的那份 `--data-dir` 重叠；导入后还要验单例的三个落点没指着禁止写入的目录。
3. **闸 1（4 次额度）**：账本先到数就不许再发、`--attempt` 与序号不符就不许发、
   记账发生在发送**之前**、dry-run 一条都不进账本、SDK 层 `max_retries=0`。
4. **闸 3（密钥不出终端）**：值级 → 形状级（`providers.scrub_secrets`）→ 路径级
   （`hide_paths`）三层，加最后一道整份体检。`scrub_secrets` 会不会打码不假设，
   当场造样本演；同时演它**认不出**无形状的值——那正是探针要多一层值级遮盖的理由。
"""
import ast
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "probe_thinking_profile", REPO_ROOT / "tools" / "probe_thinking_profile.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

from app.core import pathmask, providers, thinking_profiles    # noqa: E402

FAKE_HOST = "https://probe.invalid/v1"


def _K(*parts: str) -> str:
    """假密钥一律拼出来——和 `test_secret_scan.py` 同一约定：形态扫描器看每一行，
    把整颗 key 原样写在源码里，就是让那道闸天天在自己的测试文件上红。"""
    return "".join(parts)


SHAPEY = _K("sk-", "probe", "fake", "-0123456789abcdef")       # scrub 的形状层认得出
SHAPELESS = _K("probe", "9f3c2a77bd51e0", "46", "8a")          # 只有值级遮盖认得出


def _write_config(tmp_path: Path, *, provider_id="p_probe_deep", key=SHAPEY,
                  inline_key=None) -> Path:
    """造一份"长得像现网"的配置目录：形状是真的，值是假的。"""
    data_dir = tmp_path / "live-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    record = {"id": provider_id, "label": "probe", "base_url": FAKE_HOST,
              "model": "probe-flash", "max_context_k": 64, "supports_vision": False}
    if inline_key is not None:
        record["api_key"] = inline_key
    (data_dir / "providers.json").write_text(json.dumps([record]), encoding="utf-8")
    (data_dir / "provider_keys.json").write_text(
        json.dumps({provider_id: key}), encoding="utf-8")
    return data_dir


def _argv(tmp_path: Path, *extra: str, provider_id="p_probe_deep", key=SHAPEY,
          inline_key=None, asked=None) -> list:
    _write_config(tmp_path, provider_id=provider_id, key=key, inline_key=inline_key)
    return ["--data-dir", str(tmp_path / "live-data"), "--provider", asked or provider_id,
            "--isolation-dir", str(tmp_path / "iso"), *extra]


def _run(tmp_path: Path, *extra: str, factory=None, **kw):
    """跑一次探针。`factory` 非空时走完的是**真调用那段代码**（隔离→记账→发送→判据），
    只是客户端换成了替身；CLI 里没有这个注入口。"""
    config = probe.make_config(_argv(tmp_path, *extra, **kw))
    return probe.run_probe(config, client_factory=factory)


def _never(factory=None):
    """一个"被碰过就是失败"的替身工厂。"""
    seen = []

    def build(record):
        seen.append(record)
        raise AssertionError("闸没让过时一次都不许发")

    return build, seen


class SpyProviders:
    """只演 `build_client` 的签名，不发任何请求。"""

    def __init__(self):
        self.calls = []

    def build_client(self, provider, timeout=120.0, max_retries=2):
        self.calls.append({"timeout": timeout, "max_retries": max_retries,
                           "id": provider.get("id")})
        return probe.FakeClient("reasoning")


class FakeStore:
    def __init__(self, path, keys_path, prefs_path):
        self.path, self.keys_path, self.prefs_path = path, keys_path, prefs_path


@pytest.fixture(autouse=True)
def _restore_env():
    """`isolate_runtime_env()` 直接改 `os.environ`。不还原，后面每一格新构造的
    `ProviderStore` 都会指到我的临时目录上——那是跨文件的隐式污染。"""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


# ------------------------------------------------- 1. 候选配方：三档映射 --

@pytest.mark.parametrize("level,expected", [
    ("快速", {"reasoning_effort": "low"}),
    ("均衡", {"reasoning_effort": "medium"}),
    ("深入", {"reasoning_effort": "high"}),
])
def test_openai_reasoning_effort_maps_all_three_levels(level, expected):
    assert thinking_profiles.probe_kwargs("openai_reasoning_effort", level) == expected


@pytest.mark.parametrize("level", thinking_profiles.THINKING_LEVEL_LABELS)
def test_dashscope_uses_extra_body_enable_thinking(level):
    assert thinking_profiles.probe_kwargs("dashscope_enable_thinking", level) == {
        "extra_body": {"enable_thinking": True}}


@pytest.mark.parametrize("level", thinking_profiles.THINKING_LEVEL_LABELS)
def test_deepseek_uses_its_own_thinking_type_shape(level):
    """`deepseek_thinking_type` 是本单补的那一格：`reasoning_effort` 不是 DeepSeek 的
    公开参数，拿它去核实等于把一次真调用烧在一个已知不该发的字段上。
    出网那一格是体顶层 `thinking={type: enabled}`； kwargs 层必须套 `extra_body`，
    因为本机 SDK 的 `create()` 不收裸 `thinking`（见 `test_every_candidate_shape_can_
    actually_reach_the_sdk`）。"""
    kwargs = thinking_profiles.probe_kwargs("deepseek_thinking_type", level)
    assert kwargs == {"extra_body": {"thinking": {"type": "enabled"}}}
    assert thinking_profiles.body_fragment(kwargs) == {"thinking": {"type": "enabled"}}


def test_body_fragment_separates_the_call_shape_from_the_wire_shape():
    """`extra_body` 是搬运通道，不是体里的一层嵌套。混写这两层的后果是 Task 12
    照着其中一层配、另一层永远不通。"""
    assert thinking_profiles.body_fragment(
        thinking_profiles.probe_kwargs("dashscope_enable_thinking", "均衡")) == {
        "enable_thinking": True}
    # native 那支不过夜：`reasoning_effort` 本来就是 create() 的参数，体里也是这一格。
    assert thinking_profiles.body_fragment(
        thinking_profiles.probe_kwargs("openai_reasoning_effort", "深入")) == {
        "reasoning_effort": "high"}
    assert thinking_profiles.body_fragment({}) == {}
    assert thinking_profiles.body_fragment(None) == {}
    # 负控：搬运通道的名字不许留在体上，否则报告会说"上游收到了一颗 extra_body"。
    assert "extra_body" not in thinking_profiles.body_fragment(
        {"extra_body": {"thinking": {"type": "enabled"}}})


@pytest.mark.parametrize("profile", ["openai_reasoning_effort", "dashscope_enable_thinking",
                                     "deepseek_thinking_type"])
def test_candidate_table_covers_every_level(profile):
    """档位标签是有序 UI 值（spec §6.1）：表里少一档，界面就多一个永远点不动的选项。"""
    for level in thinking_profiles.THINKING_LEVEL_LABELS:
        assert thinking_profiles.probe_kwargs(profile, level)


def test_none_profile_sends_nothing_at_all():
    assert thinking_profiles.probe_kwargs("none", "均衡") == {}
    assert thinking_profiles.probe_kwargs("none", None) == {}


def test_unknown_profile_raises_value_error_without_network():
    with pytest.raises(ValueError, match="未知的思考 profile"):
        thinking_profiles.probe_kwargs("claude_thinking_budget", "均衡")


@pytest.mark.parametrize("profile", ["openai_reasoning_effort", "dashscope_enable_thinking",
                                     "deepseek_thinking_type"])
def test_unknown_level_raises_value_error(profile):
    """断言必须钉到**那一句**错：`probe_kwargs` 里有两支 ValueError 都带"档位"字样
    （调用方传错 vs 候选表自己畸形）。只判"档位"的话，摘掉档位校验也照样绿——
    变异实验实测过，所以这里收严到各自的措辞。"""
    for bogus in ("随便来个", ""):
        with pytest.raises(ValueError, match="未知的思考档位"):
            thinking_profiles.probe_kwargs(profile, bogus)


def test_a_malformed_table_row_fails_louder_than_a_bad_caller(monkeypatch):
    """候选表加了三档标签、却漏了一档的映射：那是**表**畸形，不能退回去发别的值，
    也不能报成"你档位填错了"。这一格同时把那条分支钉住，否则它是永远走不到的死码。"""
    broken = dict(thinking_profiles.PROBE_PROFILES)
    broken["probe_broken"] = {"label": "畸形", "level_used": True,
                              "levels": {"快速": {"reasoning_effort": "low"}}}
    monkeypatch.setattr(thinking_profiles, "PROBE_PROFILES", broken)
    with pytest.raises(ValueError, match="缺档位"):
        thinking_profiles.probe_kwargs("probe_broken", "均衡")
    # 同一支表里"填对了"那一档仍然要能组装出来：畸形不能连带把好的那一档也拒了。
    assert thinking_profiles.probe_kwargs("probe_broken", "快速") == {"reasoning_effort": "low"}


def test_upstream_literals_are_not_accepted_as_level_labels():
    """负控：把出网值本身当档位传进来必须被拒。档位是 UI 标签，映射只在这张表里；
    放行它等于两套词汇混用，两份事实来源迟早对不上。"""
    with pytest.raises(ValueError):
        thinking_profiles.probe_kwargs("openai_reasoning_effort", "low")


def test_returned_kwargs_are_a_deep_copy():
    """改返回值不许把表改脏：表是全局唯一事实来源。"""
    kwargs = thinking_profiles.probe_kwargs("dashscope_enable_thinking", "均衡")
    kwargs["extra_body"]["enable_thinking"] = "不是布尔"
    kwargs["reasoning_effort"] = "high"
    assert thinking_profiles.probe_kwargs("dashscope_enable_thinking", "快速") == {
        "extra_body": {"enable_thinking": True}}


def test_level_used_flag_matches_the_table():
    """`level_used` 是给 Task 12 抄的那一栏：True 的档位必须逐档不同，False 的三档必须
    同一个出网值。标志和表对不上时，界面就是在骗人。"""
    for name, entry in thinking_profiles.PROBE_PROFILES.items():
        if name == thinking_profiles.NONE_PROFILE:
            continue
        values = {json.dumps(entry["levels"][lvl], sort_keys=True)
                  for lvl in thinking_profiles.THINKING_LEVEL_LABELS}
        assert len(values) == (3 if entry["level_used"] else 1), name


def test_probe_table_does_not_import_providers():
    """候选表一旦被 providers 拖住，import 它就等于 import 那个模块级 store——本单最贵
    的那条雷就是这么埋回来的。用 AST 判真 import，不逐行扫字符串（docstring 里本来就在
    讨论 providers，扫字符串会把自己念成红）。"""
    source = (REPO_ROOT / "backend" / "app" / "core" / "thinking_profiles.py").read_text(
        encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            assert not any("providers" in (alias.name or "") for alias in node.names), node.lineno
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").endswith("providers"), node.lineno


def test_probe_table_is_not_the_production_whitelist():
    """Task 11 只建候选表：生产白名单（写路径与界面三态唯一认的那份）不许被顺手改宽。
    Task 12 是带着核实结论来改它的，那时这一格会红得正好。"""
    assert thinking_profiles.PROBE_PROFILES is not providers.THINKING_PROFILES
    assert set(providers.THINKING_PROFILES) == {"none"}
    for name in thinking_profiles.PROBE_PROFILES:
        if name != thinking_profiles.NONE_PROFILE:
            assert name not in providers.THINKING_PROFILES


# ------------------------------------------------------- 2. 闸：隔离与只读 --

def test_isolation_refuses_a_directory_inside_the_repo():
    """本仓目录同时是生产机：临时目录落在仓库内就等于把要保护的那份挂进来。"""
    with pytest.raises(ValueError, match="不许落在仓库内"):
        probe.isolate_runtime_env(REPO_ROOT / "tmp-iso")


def test_isolation_moves_the_provider_db_out_of_the_repo(tmp_path):
    iso = probe.isolate_runtime_env(tmp_path / "iso")
    assert os.environ["PROVIDERS_DB_PATH"] == str(iso / "providers.json")
    assert not probe.inside_dir(iso, REPO_ROOT)


def test_importing_providers_without_isolation_is_blocked():
    """这条是整个闸 2 的牙：不传隔离目录就 import providers 必须响亮失败。
    删掉 `load_backend_modules` 里那句 raise，这一格立刻红。"""
    for empty in (None, ""):
        with pytest.raises(RuntimeError, match="未隔离"):
            probe.load_backend_modules(empty)


def test_load_backend_modules_refuses_isolation_dir_inside_repo():
    with pytest.raises(RuntimeError, match="仓库内"):
        probe.load_backend_modules(REPO_ROOT / "backend")


def test_store_quarantine_guard_rejects_a_store_pointing_at_the_live_data(tmp_path):
    """`_flush()` 写的是 store 的三个路径。任何一个落在"我即将只读的那份配置"里，
    所谓只读承诺就是假话——包括 keys/prefs 这两个由记录路径推导出来的兄弟。"""
    data_dir = _write_config(tmp_path)
    iso = tmp_path / "iso"
    bad = type("M", (), {"store": FakeStore(str(iso / "providers.json"),
                                            str(data_dir / "provider_keys.json"),
                                            str(iso / "provider_prefs.json"))})
    with pytest.raises(RuntimeError, match="keys_path"):
        probe.assert_store_quarantined(bad, [REPO_ROOT, data_dir])
    # 负控的另一半：三个路径都在禁止目录之外时必须放行，否则上面那条红是"恒红"。
    ok = type("M", (), {"store": FakeStore(str(iso / "providers.json"),
                                          str(iso / "provider_keys.json"),
                                          str(iso / "provider_prefs.json"))})
    probe.assert_store_quarantined(ok, [REPO_ROOT, data_dir])


def test_store_quarantine_guard_rejects_a_missing_singleton():
    with pytest.raises(RuntimeError, match="拿不到 providers.store"):
        probe.assert_store_quarantined(type("M", (), {}), [REPO_ROOT])


def test_run_blocks_when_isolation_dir_overlaps_the_live_data(tmp_path):
    """`--isolation-dir` 指进 `--data-dir` 里面：环境变量确实设了，但"隔离"的正是那份
    现网配置。这一格必须停手，而且一次都不发。"""
    data_dir = _write_config(tmp_path)
    config = probe.make_config(["--data-dir", str(data_dir), "--provider", "p_probe_deep",
                                "--profile", "none", "--dry-run",
                                "--isolation-dir", str(data_dir / "iso")])
    build, seen = _never()
    payload, rc, _ = probe.run_probe(config, client_factory=build)
    assert rc == probe.RC_BLOCKED and seen == [] and payload["isolation_ok"] is False
    assert payload["verdict"] == "preflight_failed"


def test_dry_run_goes_through_the_real_shape_scrub(tmp_path):
    """dry-run 不是"绕过脱敏"的借口：正文里塞一颗**不在 secrets 名单**里的有形状 key，
    只有真的 `providers.scrub_secrets` 能把它盖掉。把 scrub 换成假函数，这条就红。"""
    payload, rc, _ = _run(tmp_path, "--profile", "dashscope_enable_thinking", "--level", "均衡",
                          key=SHAPELESS,
                          factory=lambda r: probe.FakeClient("error_leak", leak_value=SHAPEY))
    assert rc == probe.RC_UNVERIFIED and SHAPEY not in payload["error_excerpt"]
    assert providers.REDACTED in payload["error_excerpt"]


# ------------------------------------------------------- 3. 闸：4 次额度 --

def _fill_ledger(path: Path, n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"seq": i, "event": "attempt"}) + "\n"
                            for i in range(1, n + 1)), encoding="utf-8")


def test_budget_gate_refuses_the_fifth_call_without_sending(tmp_path):
    """授权就 4 次。第 5 次不许"再试一次"——账本到数就停，停手时客户端一次都没被碰。"""
    ledger = tmp_path / "iso" / "call-ledger.jsonl"
    _fill_ledger(ledger, probe.AUTHORIZED_LIVE_CALLS)
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_type", "--level", "均衡",
                          "--attempt", "5", "--ledger", str(ledger), factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert len(probe.read_ledger(ledger)) == probe.AUTHORIZED_LIVE_CALLS
    assert payload["verdict"] == "preflight_failed"


def test_registered_attempt_must_match_the_ledger_sequence(tmp_path):
    """登记第 3 次而账本上下一个序号是 1：要么我记错了，要么别人已经发过。
    两种都不许发——那正是重复烧额度的形状。对不上账时还不许留半条账。"""
    ledger = tmp_path / "iso" / "call-ledger.jsonl"
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "none", "--attempt", "3",
                          "--ledger", str(ledger), factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert payload["error_class"] == "AttemptMismatch"
    assert ledger.exists() is False


def test_the_slot_is_booked_before_the_request_goes_out(tmp_path):
    """先记账后发：进程在请求中途死掉，那一次额度也必须已经记在账上。
    判据是替身在**被调用的那一刻**回头读账本，看到 1 条才算数；把 reserve 挪到发送
    之后，这里读到的就是 0。"""
    ledger = tmp_path / "iso" / "call-ledger.jsonl"
    seen = []

    def build(record):
        seen.append(len(probe.read_ledger(ledger)))
        return probe.FakeClient("reasoning")

    _run(tmp_path, "--profile", "dashscope_enable_thinking", "--level", "均衡",
         "--attempt", "1", "--ledger", str(ledger), factory=build)
    assert seen == [1]


def test_live_run_consumes_exactly_one_slot(tmp_path):
    ledger = tmp_path / "iso" / "call-ledger.jsonl"
    payload, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_type", "--level", "均衡",
                          "--attempt", "1", "--ledger", str(ledger),
                          factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_VERIFIED and payload["call_slot"] == 1
    assert len(probe.read_ledger(ledger)) == 1


def test_dry_run_never_touches_the_authorized_budget(tmp_path):
    for mode in ("reasoning", "quiet", "error_form", "error_leak"):
        assert probe.main(_argv(tmp_path, "--profile", "dashscope_enable_thinking",
                               "--level", "均衡", "--dry-run", "--fake", mode)) in (0, 1)
    assert not (tmp_path / "iso" / "call-ledger.jsonl").exists()


def test_second_send_in_the_same_run_is_refused(tmp_path):
    """`stream=False` 拿不到证据时"再来一次"是人之常情，也是超授权的头号入口。"""
    budget = probe.CallBudget()
    budget.spend()
    with pytest.raises(probe.BudgetExhausted):
        budget.spend()


def test_live_client_is_built_with_max_retries_zero():
    """SDK 默认 `max_retries=2`：那意味着"一次调用"在传输层可以是三次 HTTP 请求，
    4 次的授权当场名存实亡。这条钉住构造那一步真的传了 0。"""
    spy = SpyProviders()
    assert probe.build_live_client(spy, {"id": "p_probe_deep"}, timeout=30.0) is not None
    assert spy.calls == [{"timeout": 30.0, "max_retries": 0, "id": "p_probe_deep"}]
    assert spy.calls[0]["max_retries"] != 2, "把那句 max_retries=0 删了就会落到 SDK 的默认 2"


# ------------------------------------------------ 4. 发送形状与判据走向 --

def test_the_wire_shape_is_the_authorized_one(tmp_path):
    """prompt 固定 `ping`、`max_tokens=8`、`stream=False`，思考配方并进去。
    这三个值都是授权内容的一部分，改了就越权。"""
    fake = probe.FakeClient("reasoning")
    payload, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_type", "--level", "均衡",
                          factory=lambda r: fake)
    assert rc == probe.RC_VERIFIED
    assert fake.calls == [{
        "model": "probe-flash", "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": probe.DEFAULT_MAX_TOKENS, "stream": False,
        "extra_body": {"thinking": {"type": "enabled"}}}]
    assert payload["request_kwargs"] == {"extra_body": {"thinking": {"type": "enabled"}}}
    assert payload["request_body"] == {"thinking": {"type": "enabled"}}
    assert payload["sdk_kwarg_check"] == "checked"
    assert payload["request_shape"] == {"prompt": "ping", "max_tokens": 8, "stream": False,
                                        "timeout": probe.DEFAULT_TIMEOUT, "max_retries": 0}


def test_the_local_sdk_signature_is_what_this_table_was_written_against():
    """这张表是照着本机 SDK 写的：`create()` 收 `extra_body`、**不收**裸 `thinking`，
    而且没有 `**kwargs`。哪天这三条里有一条变了（上游加了原生 `thinking`，或者签名
    开始吞任意参数），这一格会红——那时"套 extra_body"就不再是唯一答案，得重挑。"""
    names = probe.create_kwarg_names()
    assert names is not None, "签名开始接受任意 kwargs：SDK 闸已退化成空判，得重看这张表"
    assert "extra_body" in names and "thinking" not in names and "enable_thinking" not in names


@pytest.mark.parametrize("profile", ["openai_reasoning_effort", "dashscope_enable_thinking",
                                     "deepseek_thinking_type"])
@pytest.mark.parametrize("level", thinking_profiles.THINKING_LEVEL_LABELS)
def test_every_candidate_shape_can_actually_reach_the_sdk(profile, level):
    """候选配方必须**塞得进**真 SDK 的 create()：表里写出一个签名不收的裸字段，
    本地就 TypeError，一次 HTTP 都不发——而那已经是花掉一格额度之后。
    这条实测抓走过 `{"thinking": {...}}` 那版写法（本机 openai 2.37.0 复现过）。"""
    kwargs = thinking_profiles.probe_kwargs(profile, level)
    assert probe.kwargs_rejected_by_sdk(kwargs, probe.create_kwarg_names()) == []


def test_sdk_gate_rejects_an_unsendable_shape_without_booking_it(tmp_path, monkeypatch):
    """负控（判据自己会红）：往表里塞一支裸 `thinking=` 的畸形配方，探针必须停在
    记账**之前**——账本里没有这一格，客户端一次都没被碰。"""
    broken = dict(thinking_profiles.PROBE_PROFILES)
    broken["probe_unsendable"] = {
        "label": "畸形", "level_used": False,
        "levels": {lvl: {"thinking": {"type": "enabled"}}
                   for lvl in thinking_profiles.THINKING_LEVEL_LABELS}}
    monkeypatch.setattr(thinking_profiles, "PROBE_PROFILES", broken)
    ledger = tmp_path / "iso" / "call-ledger.jsonl"
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "probe_unsendable", "--level", "均衡",
                          "--attempt", "1", "--ledger", str(ledger), factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert ledger.exists() is False, "到不了上游的配方不许占额度"
    assert payload["request_body"] == {"thinking": {"type": "enabled"}}
    assert any("thinking" in note for note in payload["notes"])


def test_sdk_gate_is_inert_when_the_signature_cannot_be_read():
    """第二重的负控：尺子取不到（None）时闸门必须**不咬人**——把"读不到签名"演成
    "字段不合法"会挡掉本来发得出去的调用，那是另一种白烧。"""
    assert probe.kwargs_rejected_by_sdk({"thinking": {"type": "enabled"}}, None) == []
    assert probe.kwargs_rejected_by_sdk({"reasoning_effort": "low"}, {"reasoning_effort"}) == []
    assert probe.kwargs_rejected_by_sdk({"thinking": {}}, {"reasoning_effort"}) == ["thinking"]


def test_verified_via_reasoning_content_only(tmp_path):
    """推理字段躲在 `model_extra` 里（不是属性）、usage 又不给细节：也算已核实——
    两颗证据是"或"的关系，而不同网关把同一件事放在不同位置。"""
    payload, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_type", "--level", "快速",
                          factory=lambda r: probe.FakeClient("extra_reasoning"))
    assert rc == probe.RC_VERIFIED and payload["verdict"] == "verified"
    assert payload["has_reasoning_content"] is True and payload["reasoning_tokens"] == 0
    assert payload["evidence_fields"] == ["message.reasoning_content"]


def test_verified_via_reasoning_tokens_only(tmp_path):
    from types import SimpleNamespace
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))],
        usage=SimpleNamespace(prompt_tokens=9, completion_tokens=8, total_tokens=17,
                              completion_tokens_details=SimpleNamespace(reasoning_tokens=12)))
    payload, rc, _ = _run(tmp_path, "--profile", "openai_reasoning_effort", "--level", "均衡",
                          factory=lambda r: SimpleNamespace(
                              chat=SimpleNamespace(completions=SimpleNamespace(
                                  create=lambda **kw: response))))
    assert rc == probe.RC_VERIFIED and payload["verdict"] == "verified"
    assert payload["reasoning_tokens"] == 12
    assert payload["usage"] == {"prompt_tokens": 9, "completion_tokens": 8, "total_tokens": 17}


def test_unverified_when_the_upstream_accepts_but_shows_nothing(tmp_path):
    """`quiet` 那一格演的是"200 但不是证据"：参数被接受 ≠ 它在思考。"""
    payload, rc, _ = _run(tmp_path, "--profile", "openai_reasoning_effort", "--level", "深入",
                          factory=lambda r: probe.FakeClient("quiet"))
    assert rc == probe.RC_UNVERIFIED and payload["verdict"] == "unverified"
    assert payload["accepted"] is True
    assert payload["has_reasoning_content"] is False and payload["reasoning_tokens"] == 0


def test_rejected_parameter_is_not_read_as_unsupported(tmp_path):
    """参数被拒 ≠ 模型不支持思考：这一格只能写 unverified，绝不能写 unsupported。"""
    payload, rc, _ = _run(tmp_path, "--profile", "openai_reasoning_effort", "--level", "均衡",
                          factory=lambda r: probe.FakeClient("error_plain"))
    assert rc == probe.RC_UNVERIFIED and payload["verdict"] == "unverified"
    assert payload["error_class"] == "FakeBadRequestError"
    assert payload["verdict_label"] == "未核实"


def test_non_streaming_rejection_is_reported_as_the_form_not_the_model(tmp_path):
    """预期中的那条诚实结果：兼容模式在 `stream=False` 下可能直接拒
    `enable_thinking=true`。这一格写「该形态今天不可核实」，不改成流式再发一次。"""
    payload, rc, _ = _run(tmp_path, "--profile", "dashscope_enable_thinking", "--level", "均衡",
                          factory=lambda r: probe.FakeClient("error_form"))
    assert rc == probe.RC_UNVERIFIED and payload["verdict"] == "form_unverifiable"
    assert payload["verdict_label"] == "该形态今天不可核实"
    assert "stream" in payload["error_excerpt"]


def test_a_lone_thinking_400_is_not_promoted_to_form_unverifiable():
    """负控：只提 `thinking`、不提流式的 400 不许判成「不可核实」——那是把「我没试」
    说成「试不了」。摘掉 `looks_form_unverifiable` 里那句 `"stream" in low`，这条就红。"""
    exc = probe.FakeBadRequestError("Error code: 400 - enable_thinking is not supported")
    assert probe.looks_form_unverifiable(exc, f"{type(exc).__name__} {exc}") is False


def test_connection_error_is_never_read_as_a_verdict(tmp_path):
    class ConnectionFellOver(RuntimeError):
        pass

    def build(record):
        raise ConnectionFellOver("upstream dial failed")

    payload, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_type", "--level", "均衡",
                          factory=build)
    assert rc == probe.RC_UNVERIFIED and payload["verdict"] == "unverified"
    assert payload["error_class"] == "ConnectionFellOver"


@pytest.mark.parametrize("mode,expected_rc", [("reasoning", probe.RC_VERIFIED),
                                              ("error_plain", probe.RC_UNVERIFIED)])
def test_exit_code_both_ways_are_reachable_under_a_fake_client(tmp_path, mode, expected_rc):
    """brief 明要求：假 client 下退出码两种走向都要覆盖。"""
    assert probe.main(_argv(tmp_path, "--profile", "dashscope_enable_thinking",
                            "--level", "均衡", "--dry-run", "--fake", mode)) == expected_rc


def test_baseline_row_reports_the_control_not_a_verdict(tmp_path):
    """基线那一格（不带任何思考参数）不许冒充「已核实」——它只证明这条端点今天通。"""
    payload, rc, _ = _run(tmp_path, "--profile", "none",
                          factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_VERIFIED and payload["verdict"] == "baseline_ok"
    assert payload["request_kwargs"] == {} and payload["level"] is None


def test_baseline_failure_is_its_own_verdict(tmp_path):
    payload, rc, _ = _run(tmp_path, "--profile", "none",
                          factory=lambda r: probe.FakeClient("error_plain"))
    assert rc == probe.RC_UNVERIFIED and payload["verdict"] == "baseline_failed"


# ---------------------------------------------------- 5. 出口：密钥不外泄 --

def test_scrub_secrets_really_masks_a_shaped_key():
    """负控（brief 明令不许假设它会）：拿真的 `providers.scrub_secrets` 演一遍样本。"""
    out = providers.scrub_secrets(f"上游拒绝：Authorization: Bearer {SHAPEY} 被拒")
    assert SHAPEY not in out and providers.REDACTED in out


def test_scrub_secrets_alone_misses_a_shapeless_value():
    """这条是**故意演它失败**：`scrub_secrets` 认形状，以及"进程级 store 里配过的值"；
    而探针的 store 被隔离在临时目录、不认识现网那把 key。所以出口必须再加一层值级遮盖。"""
    assert providers.scrub_secrets(f"header was {SHAPELESS}") == f"header was {SHAPELESS}"


def test_value_layer_masks_what_the_shape_layer_cannot_see():
    out = probe.redact(f"header was {SHAPELESS}", [SHAPELESS], providers.scrub_secrets,
                       pathmask.hide_paths)
    assert SHAPELESS not in out and providers.REDACTED in out


def test_redact_masks_absolute_paths_too():
    out = probe.redact(f"读不到 {os.path.join(str(REPO_ROOT), 'data', 'providers.json')}",
                       [], providers.scrub_secrets, pathmask.hide_paths)
    assert pathmask.PATH_MASK in out and str(REPO_ROOT) not in out


def test_live_error_excerpt_never_carries_the_loaded_key(tmp_path):
    """演的是「上游把 Authorization 原样打印回来」那一形：正文里有真 key（这里是假的），
    出口必须只剩掩码。"""
    payload, rc, secrets = _run(tmp_path, "--profile", "dashscope_enable_thinking",
                                "--level", "均衡",
                                factory=lambda r: probe.FakeClient("error_leak",
                                                                   leak_value=SHAPEY))
    assert rc == probe.RC_UNVERIFIED
    assert SHAPEY not in json.dumps(payload, ensure_ascii=False)
    assert providers.REDACTED in payload["error_excerpt"]
    assert secrets == [SHAPEY] and payload["credential_fp"] == probe.key_fingerprint(SHAPEY)


def test_rendered_payload_carries_only_the_fingerprint(tmp_path, capsys):
    probe.main(_argv(tmp_path, "--profile", "deepseek_thinking_type", "--level", "均衡",
                     "--dry-run", key=SHAPEY))
    printed = capsys.readouterr().out
    parsed = json.loads(printed)
    assert SHAPEY not in printed
    assert parsed["credential_fp"] == probe.key_fingerprint(SHAPEY) != SHAPEY


def test_exit_guard_refuses_to_print_a_payload_carrying_the_key(tmp_path, capsys, monkeypatch):
    """最后一道整份体检：字段级脱敏被谁漏掉时，必须一个字都不打，而不是"打个半截"。"""
    leaky = {"provider_id": "p_probe_deep", "error_excerpt": f"header was {SHAPELESS}"}
    monkeypatch.setattr(probe, "run_probe", lambda config: (leaky, probe.RC_UNVERIFIED,
                                                             [SHAPELESS]))
    rc = probe.main(_argv(tmp_path, "--profile", "none", "--dry-run"))
    printed = capsys.readouterr().out
    assert rc == probe.RC_LEAK_BLOCKED and SHAPELESS not in printed
    assert json.loads(printed)["exit_guard"] == "blocked"


def test_exit_guard_does_not_fire_on_a_clean_payload(tmp_path, capsys, monkeypatch):
    """负控：体检不许是"恒红"的装饰。同样过一遍检查，干净的那份必须照打。"""
    clean = {"provider_id": "p_probe_deep", "error_excerpt": "header was «redacted»"}
    monkeypatch.setattr(probe, "run_probe", lambda config: (clean, probe.RC_UNVERIFIED,
                                                             [SHAPELESS]))
    rc = probe.main(_argv(tmp_path, "--profile", "none", "--dry-run"))
    printed = capsys.readouterr().out
    assert rc == probe.RC_UNVERIFIED
    assert json.loads(printed)["error_excerpt"] == clean["error_excerpt"]


def test_render_and_check_rejects_a_credential_shape_it_was_not_told_about():
    """整份体检还认形状：就算 `secrets` 里没有这颗（以后从别的口子带进来也一样拦）。
    负控：同一份 payload 换成掩码值就必须放行。"""
    with pytest.raises(probe.SecretLeak):
        probe.render_and_check({"detail": f"key was {SHAPEY}"}, [], providers.scrub_secrets)
    probe.render_and_check({"detail": "key was «redacted»"}, [], providers.scrub_secrets)


def test_crash_path_prints_only_the_exception_type(tmp_path, capsys, monkeypatch):
    """崩在中途时那句 message 没过任何一层脱敏（它可能带着 key 或本机路径），
    所以出口只报类型名。"""
    def boom(config):
        raise RuntimeError(f"读盘失败 {SHAPEY} {REPO_ROOT}")

    monkeypatch.setattr(probe, "run_probe", boom)
    rc = probe.main(_argv(tmp_path, "--profile", "none", "--dry-run"))
    printed = capsys.readouterr().out
    assert rc == probe.RC_BLOCKED
    assert SHAPEY not in printed and str(REPO_ROOT) not in printed
    assert "RuntimeError" in printed


# ------------------------------------------------------- 6. 预检：零出站 --

def test_unknown_profile_never_reaches_the_network_layer(tmp_path):
    """候选表外的名字在 argparse 就被 choices 挡掉，连 payload 都不该有。"""
    with pytest.raises(SystemExit) as boom:
        probe.make_config(_argv(tmp_path, "--profile", "does_not_exist", "--level", "均衡",
                                "--dry-run"))
    assert boom.value.code == 2


def test_level_outside_the_table_blocks_before_any_call(tmp_path):
    """argparse 不管档位（自由字符串），所以这一格走 `probe_kwargs` 那句 ValueError：
    隔离已经做完，但配方不认识就一个字节都不发。"""
    payload, rc, _ = _run(tmp_path, "--profile", "openai_reasoning_effort", "--level", "瞎填",
                          "--dry-run")
    assert rc == probe.RC_BLOCKED and payload["isolation_ok"] is True
    assert payload["error_class"] == "ValueError" and payload["request_kwargs"] == {}
    assert any("未知的思考档位" in note for note in payload["notes"]), payload["notes"]


def test_missing_record_blocks_without_calling_anything(tmp_path):
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "none", "--dry-run", asked="p_not_in_file",
                          factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert payload["error_class"] == "LookupError" and payload["credential_fp"] is None


def test_placeholder_key_is_refused_before_the_budget_is_spent(tmp_path):
    """`looks_placeholder` 那一形（`your-key-here`）发出去必 401，白烧一格额度。
    用 providers 那份真判据，别在探针里再抄一份规则。"""
    ledger = tmp_path / "iso" / "call-ledger.jsonl"
    payload, rc, _ = _run(tmp_path, "--profile", "none", "--attempt", "1",
                          "--ledger", str(ledger), key=_K("sk-", "your", "-", "keyhere9"),
                          factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_BLOCKED and payload["verdict"] == "preflight_failed"
    assert ledger.exists() is False


def test_record_with_inline_key_is_read_and_the_source_is_reported(tmp_path):
    """现网那份早就把 key 搬走了；真有内联值时以它为准，但来源要写在明处。"""
    payload, rc, _ = _run(tmp_path, "--profile", "none", inline_key=SHAPEY,
                          factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_VERIFIED and payload["credential_origin"] == "inline"
    assert payload["credential_fp"] == probe.key_fingerprint(SHAPEY)


def test_keys_file_is_used_when_the_record_carries_no_key(tmp_path):
    payload, rc, _ = _run(tmp_path, "--profile", "none",
                          factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_VERIFIED and payload["credential_origin"] == "keys_file"


# ------------------------------------------- 7. 只读证据的尺子本身会动 --

def test_runtime_digest_actually_moves_when_the_file_is_written(tmp_path):
    """负控：那条"没写 data/"的证据不能是一把永远不动的尺子。"""
    data_dir = _write_config(tmp_path)
    before = probe.files_digest(data_dir)
    (data_dir / "provider_keys.json").write_text(json.dumps({"p_probe_deep": "改了"}),
                                                 encoding="utf-8")
    assert probe.files_digest(data_dir) != before
    assert set(probe.files_digest(tmp_path / "nope").values()) == {"missing"}


def test_run_reports_the_live_config_untouched(tmp_path):
    data_dir = _write_config(tmp_path)
    config = probe.make_config(["--data-dir", str(data_dir), "--provider", "p_probe_deep",
                                "--profile", "deepseek_thinking_type", "--level", "均衡",
                                "--isolation-dir", str(tmp_path / "iso")])
    # 摘要必须在 `make_config` 之后取：`_run` 那层助手会重写这份夹具，先把 before
    # 记下来就会把"夹具自己写的"当成"探针写的"——那是假红，不是证据。
    before = probe.files_digest(data_dir)
    payload, rc, _ = probe.run_probe(config, client_factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_VERIFIED and payload["runtime_data_untouched"] is True
    assert probe.files_digest(data_dir) == before


def test_probe_writes_nothing_into_the_repository(tmp_path):
    """整条链跑完（含隔离目录、账本、payload），仓库里不该多一个文件。
    brief 那条"只进这棵树、临时产物留仓库外"的机器版。"""
    watch = REPO_ROOT / "data"
    listing = {str(p.relative_to(watch)) for p in watch.rglob("*")} if watch.is_dir() else set()
    for mode in ("reasoning", "quiet", "error_form"):
        probe.main(_argv(tmp_path, "--profile", "dashscope_enable_thinking", "--level", "均衡",
                         "--dry-run", "--fake", mode))
        iso = Path(os.environ["PROVIDERS_DB_PATH"]).parent
        assert not probe.inside_dir(iso, REPO_ROOT)
    after = {str(p.relative_to(watch)) for p in watch.rglob("*")} if watch.is_dir() else set()
    assert after == listing


def test_default_isolation_dir_lives_outside_the_repo(tmp_path):
    iso = probe.isolate_runtime_env(None)
    try:
        assert not probe.inside_dir(iso, REPO_ROOT)
        assert os.environ["DATA_ROOT"] == str(iso)
    finally:
        shutil.rmtree(iso, ignore_errors=True)
