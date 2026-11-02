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

修复轮 1（审查员 1 Critical + 4 Important + 3 Minor）新增 §8：闸的牙全部换成机制的牙
——默认参数下"总额 4 次"必须真咬、`AUTHORIZED_LIVE_CALLS` 这个数字本身有锁、重叠判据
必须在 import **之前**、交接物必须自带射程、脱敏必须在截断**之前**。每条都配会红的反例。

修复轮 2（复审 4 Important + 2 Minor）新增 §9，并补强 §6/§8 的两格：dry-run 那一行不许
冒充真发（N1）、重叠判据走 realpath 且只读快照先于隔离（N2）、账本"仓库外"从一句话
变成预检判据（N3）、"另一棵 checkout"沿父链上溯（N4）、新建账本必须自我声明计数口径
（concerns.1 残余）、只读证据拆成软硬两格（concerns.3）。本节同样**零出站**。

Task 11b（第二轮授权机制化）新增 §10，并按 §R1 改写 §8 那条额度锁：授权从「一个数字」
换成**授权轮次表** `AUTHORIZED_GRANTS`，live 模式除 `--ledger` 之外还要 `--grant <id>`，
且账本文件名绑在那一份授权上（换文件不再是加个 flag）；候选表每支显式带 `direction`，
判据改成方向感知——OFF 方向"痕迹消失"是要的证据，旧分类器在那里给的是**反结论**
（§10 那两格心脏用例演的就是这件事）。本节**零出站**：客户端一律是注入的替身或
`--dry-run`，三条 paid 调用由主控人过审后手工执行。

修复轮 2（审查员 round 2 的阻塞项 C-1）新增 §11：那条"另一棵 checkout 就拒"的闸按
现在的接法把本工具唯一的用途（**读**现网那条 provider 记录）一起禁掉了，三次调用一次
都发不出去。F1 把它收到真正要挡的动作上——预检里针对 `--data-dir` 的那一次调用删掉，
针对 `--ledger` 的那一次保留。所以 §11 的重点不是"新闸有多严"而是**改窄之后牙还在**：
读那一面有新的正面锁（§3.1），写的那三面（账本进别树 / 账本进只读树 / 隔离目录进只读树）
逐条还咬，只读尺子与单例名单在新放行面上照样生效，外加一行"变异 #7 就是让 §3.2 变红的"。
本节同样**零出站**，且"另一棵 checkout"一律是 tmp_path 下现造的假树——主工作树那份
`data/` 连读都不必读。
"""
import ast
import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "probe_thinking_profile", REPO_ROOT / "tools" / "probe_thinking_profile.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

from app.core import pathmask, providers, thinking_profiles    # noqa: E402

FAKE_HOST = "https://probe.invalid/v1"

# —— §R1 授权轮次表带来的三个夹具常量 ——
# 授权表里的 `allowed_shapes` 是 **provider × profile 的白名单**，且只有现网那两条
# provider id。夹具的 provider 因此必须跟着 profile 走（dashscope 那两支住在 relay 上），
# 否则每一格 live 用例都会先撞白名单、测不到它本来要测的那一格。
GRANT1 = "g1-20261101"
GRANT2 = "g2-20261102"
DEEPSEEK = "deepseek-chat"
RELAY = "p_55a83a43"


def _K(*parts: str) -> str:
    """假密钥一律拼出来——和 `test_secret_scan.py` 同一约定：形态扫描器看每一行，
    把整颗 key 原样写在源码里，就是让那道闸天天在自己的测试文件上红。"""
    return "".join(parts)


SHAPEY = _K("sk-", "probe", "fake", "-0123456789abcdef")       # scrub 的形状层认得出
SHAPELESS = _K("probe", "9f3c2a77bd51e0", "46", "8a")          # 只有值级遮盖认得出

# 除基线以外的**全部**候选配方——从表本身推导，不写死名单：§R3 新加的 OFF 两支、以及
# 以后往表里补的任何一支，都会自动落进"三档齐不齐 / 表外档位必须报错 / 塞不塞得进真 SDK"
# 这三把锁里。写死名单的话，加配方的人只要不记得改名单，这三把新锁就是假绿灯。
NON_BASELINE_PROFILES = [p for p in thinking_profiles.probe_profile_ids()
                         if p != thinking_profiles.NONE_PROFILE]


def _write_config(tmp_path: Path, *, provider_id=DEEPSEEK, key=SHAPEY,
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


def _provider_for(profile) -> str:
    """profile → 授权表里唯一合法的宿主 provider（见上面那三个常量的注释）。"""
    return RELAY if str(profile or "").startswith("dashscope_") else DEEPSEEK


def _ledger(base, grant=GRANT2) -> Path:
    """某一轮授权的**权威账本路径**：basename 必须等于该 grant 的 `ledger_name`（§R1
    的核心那一步）。取不到表时退回同一命名规则的字面量——这样夹具在本单实现之前依然
    可 import，红的就是"闸不存在"那一格，而不是夹具自己炸。"""
    name = (getattr(probe, "AUTHORIZED_GRANTS", {}) or {}).get(grant, {}) \
        .get("ledger_name") or "call-ledger-%s.jsonl" % grant
    return Path(base) / name


def _next_seq(ledger) -> int:
    """账本上下一个序号（夹具用来声明"这一跑就是下一格"）。只数 `event == "attempt"` 的行，
    与 `reserve_call_slot` 同一口径；文件不存在时是 1，**不创建任何东西**。"""
    return len(probe.read_ledger(ledger)) + 1


def _effective_ledger(tmp_path: Path, extra: tuple):
    """这一跑真正会写的那份账本：命令行里最后一次 `--ledger` 说了算（argparse 取最后一个），
    站点没指定时才是夹具那份 `_ledger(tmp_path/"iso")`。"""
    picks = [i for i, a in enumerate(extra) if a == "--ledger"]
    return Path(extra[picks[-1] + 1]) if picks else _ledger(tmp_path / "iso")


def _argv(tmp_path: Path, *extra: str, provider_id=None, key=SHAPEY,
          inline_key=None, asked=None, grant=GRANT2, attempt=None) -> list:
    """夹具命令行。**live 形状（不带 --dry-run）必须有一份显式账本 + 显式授权轮次 + 显式序号**，
    这是 Critical-1、§R1 与修复轮 1（I-2）修完之后的新契约：缺省账本会落进每次新建的隔离目录 ⇒ "总额"
    永不咬；缺 `--grant` 则"哪一份文件是这一轮的权威账本"又回到"由操作者记得"；缺 `--attempt`
    就没人声明"我以为这是第几次"，机器那份对账就没有左半边。所以这里
    给每格测试一条固定的持久账本（在 data_dir 之外、文件名等于该轮的 `ledger_name`），
    让既有用例继续演真调用那一段。
    位置在 `*extra` **之前**：谁要自己指定账本/授权轮次/序号，谁说了算（argparse 取最后一次）。
    provider 缺省时按 profile 推（白名单只认现网那两条 provider id）。
    `--attempt` 那一格同样：站点显式写了就按它演（对账用例全靠这一条）。缺省时 live 形状
    由夹具按**这一跑真正那份账本**的下一个序号声明（"这一跑就是下一格"），既不夸大也不掩盖；
    `attempt="omit"` 是留给 I-2 那条必填闸的负控开关——**故意不声明**，看闸会不会红。"""
    profile = extra[extra.index("--profile") + 1] if "--profile" in extra else "none"
    provider_id = provider_id or _provider_for(profile)
    _write_config(tmp_path, provider_id=provider_id, key=key, inline_key=inline_key)
    argv = ["--data-dir", str(tmp_path / "live-data"), "--provider", asked or provider_id]
    if grant:                                   # `grant=None` 演"live 不给授权轮次"那一格
        argv += ["--grant", grant]
    argv += ["--ledger", str(_ledger(tmp_path / "iso")),
             "--isolation-dir", str(tmp_path / "iso")]
    if ("--dry-run" not in extra and attempt != "omit" and "--attempt" not in extra):
        argv += ["--attempt", str(attempt if attempt is not None
                                   else _next_seq(_effective_ledger(tmp_path, extra)))]
    elif attempt == "omit":                     # 必填闸的负控：什么都不声明，且不许由别处补上
        assert "--attempt" not in extra, "attempt=\"omit\" 与站点自带 --attempt 是互相矛盾的输入"
    elif attempt is not None:                   # 站点点名一个具体序号（不读账本，照抄）
        argv += ["--attempt", str(attempt)]
    return argv + list(extra)


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


@pytest.mark.parametrize("profile", NON_BASELINE_PROFILES)
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


@pytest.mark.parametrize("profile", NON_BASELINE_PROFILES)
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


# 反向 import 的六种形状。12A 审查员实测：旧判据只认"整条 module 以 providers 结尾"，
# `from app.core import providers` 塞进生产文件后 220 格全绿（她的变异 F）——最自然的
# 写法恰好漏网。这里把每一种形状都点名，`_providers_import_offenders` 必须逐形报红；
# 负控用 tmp_path 临时文件实演，**不动生产表**（生产文件由下一格继续判真源）。
REVERSE_IMPORT_SHAPES = [
    ("import_providers", "import providers\n"),
    ("import_app_core_providers", "import app.core.providers\n"),
    ("from_app_core_import_providers", "from app.core import providers\n"),
    ("from_app_core_import_providers_as_alias", "from app.core import providers as prov\n"),
    ("from_app_core_providers_import_name", "from app.core.providers import PROFILES\n"),
    ("importlib_dynamic", "import importlib\nimportlib.import_module('app.core.providers')\n"),
]


def _providers_import_offenders(source: str) -> list:
    """AST 判「把 providers 拖进来」的反向 import，返回违例 (行号, 形状) 清单。

    按**模块事实**判而不是数 `import x` 语句形：
    ① `import providers` / `import app.core.providers`（Import，按段名含 providers）；
    ② `from app.core import providers`（含 as 别名——查 `node.names`，旧版只查
       `node.module` 漏掉的就是这一形，审查员变异 F 实证 220 格全绿）；
    ③ `from app.core.providers import <anything>`（module 按段名判，子模块也算）；
    ④ `importlib.import_module(...)` / `__import__(...)`：字面量能解析且不含 providers
       才放行，**解析不出取到的是什么就 fail-closed 记违例**，不许 except 掉。
    口径与 `git ls-files` 那套老规矩一致：取不到真源就红，不 skip（文件存在性由调用方
    断言，这里的 ast.parse 不套 try——语法坏了的"锁"本身也不算锁）。
    """
    offenders = []

    def flagged(dotted):
        # 判宽不判窄：段名相等之外，任何含 providers 的名字都记下来。fail-closed 的
        # 方向是宁误报不漏报；本树今天没有"名字里带 providers 却不是它"的模块。
        return "providers" in (dotted or "")

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if flagged(alias.name):
                    offenders.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if flagged(node.module) or flagged(alias.name):
                    offenders.append((node.lineno,
                                      f"from {node.module} import {alias.name}"))
        elif isinstance(node, ast.Call):
            fname = (node.func.attr if isinstance(node.func, ast.Attribute)
                     else node.func.id if isinstance(node.func, ast.Name) else None)
            if fname in ("import_module", "__import__"):
                first = node.args[0] if node.args else None
                target = (first.value if isinstance(first, ast.Constant)
                          and isinstance(first.value, str) else None)
                if target is None:
                    offenders.append((node.lineno,
                                      f"{fname}(<动态取值>): 解析不出取到哪门，fail-closed"))
                elif flagged(target):
                    offenders.append((node.lineno, f"{fname}({target!r})"))
    return offenders


def test_probe_table_does_not_import_providers():
    """候选表一旦被 providers 拖住，import 它就等于 import 那个模块级 store——本单最贵
    的那条雷就是这么埋回来的。用 AST 判真 import，不逐行扫字符串（docstring 里本来就在
    讨论 providers，扫字符串会把自己念成红）。取不到真源就红，不许 skip。"""
    path = REPO_ROOT / "backend" / "app" / "core" / "thinking_profiles.py"
    assert path.is_file(), f"AST 锁取不到真源，红而不是 skip：{path}"
    offenders = _providers_import_offenders(path.read_text(encoding="utf-8"))
    assert not offenders, f"thinking_profiles.py 反向 import 了 providers：{offenders}"


@pytest.mark.parametrize("shape,source", REVERSE_IMPORT_SHAPES,
                         ids=[name for name, _ in REVERSE_IMPORT_SHAPES])
def test_ast_lock_is_red_on_every_reverse_import_shape(shape, source, tmp_path):
    """AST 锁的牙锁：点名名单里每一种形状都必须被 `_providers_import_offenders` 报红。

    恒绿的判据比没有判据更坏——12A 的 docstring 正指着这条锁说话（"这里零依赖，所以
    可以随便 import"），锁不吃某一形时那句担保就是借来的信用。反向对照（干净源不许
    报红）由上一格在生产文件上绿着来给，这一格只演"每形必红"。
    """
    tmp = tmp_path / "shaped_import.py"
    tmp.write_text(source, encoding="utf-8")
    offenders = _providers_import_offenders(tmp.read_text(encoding="utf-8"))
    assert offenders, f"AST 锁漏掉了形状 {shape}"


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
    config = probe.make_config(["--data-dir", str(data_dir), "--provider", DEEPSEEK,
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


def test_budget_gate_refuses_the_next_call_once_the_grant_is_full(tmp_path):
    """授权就那么多格。多出来的那一格不许"再试一次"——账本到数就停，停手时客户端一次
    都没被碰。

    **条数写死字面量**（Important-2 的同一口径，本轮改到 g2 的 3）：这一格是"填满即拒"
    的那道锁，填充条数若跟着判据同源漂，把上限改成 9 就变成"填满 9 条再发第 10 次"⇒
    全绿。轮次表本身由 `test_authorized_live_call_count_is_the_grant_itself` 逐轮字面钉。
    """
    ledger = _ledger(tmp_path / "iso")
    _fill_ledger(ledger, 3)
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_type", "--level", "均衡",
                          "--attempt", "4", "--ledger", str(ledger), factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert len(probe.read_ledger(ledger)) == 3, "被拒的那一次不许在账上留痕"
    assert payload["verdict"] == "preflight_failed"


def test_registered_attempt_must_match_the_ledger_sequence(tmp_path):
    """登记第 3 次而账本上下一个序号是 1：要么我记错了，要么别人已经发过。
    两种都不许发——那正是重复烧额度的形状。对不上账时还不许留半条账。"""
    ledger = _ledger(tmp_path / "iso")
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
    ledger = _ledger(tmp_path / "iso")
    seen = []

    def build(record):
        seen.append(len(probe.read_ledger(ledger)))
        return probe.FakeClient("reasoning")

    _run(tmp_path, "--profile", "dashscope_enable_thinking", "--level", "均衡",
         "--attempt", "1", "--ledger", str(ledger), factory=build)
    assert seen == [1]


def test_live_run_consumes_exactly_one_slot(tmp_path):
    ledger = _ledger(tmp_path / "iso")
    payload, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_type", "--level", "均衡",
                          "--attempt", "1", "--ledger", str(ledger),
                          factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_VERIFIED and payload["call_slot"] == 1
    assert len(probe.read_ledger(ledger)) == 1


def test_dry_run_never_touches_the_authorized_budget(tmp_path):
    for mode in ("reasoning", "quiet", "error_form", "error_leak"):
        assert probe.main(_argv(tmp_path, "--profile", "dashscope_enable_thinking",
                               "--level", "均衡", "--dry-run", "--fake", mode)) in (0, 1)
    assert not _ledger(tmp_path / "iso").exists()


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


@pytest.mark.parametrize("profile", NON_BASELINE_PROFILES)
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
        "label": "畸形", "level_used": False, "direction": "on",
        "levels": {lvl: {"thinking": {"type": "enabled"}}
                   for lvl in thinking_profiles.THINKING_LEVEL_LABELS}}
    monkeypatch.setattr(thinking_profiles, "PROBE_PROFILES", broken)
    ledger = _ledger(tmp_path / "iso")
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
                          "--dry-run",
                          factory=lambda r: SimpleNamespace(
                              chat=SimpleNamespace(completions=SimpleNamespace(
                                  create=lambda **kw: response))))
    assert rc == probe.RC_VERIFIED and payload["verdict"] == "verified"
    assert payload["reasoning_tokens"] == 12
    assert payload["usage"] == {"prompt_tokens": 9, "completion_tokens": 8, "total_tokens": 17}


def test_unverified_when_the_upstream_accepts_but_shows_nothing(tmp_path):
    """`quiet` 那一格演的是"200 但不是证据"：参数被接受 ≠ 它在思考。
    （`--dry-run`：`openai_reasoning_effort` 不在两轮授权的任何 `allowed_shapes` 里，
    live 形状会被白名单闸挡下——判据走向要演的形状用模拟那一跑才安全，见 §R1。）"""
    payload, rc, _ = _run(tmp_path, "--profile", "openai_reasoning_effort", "--level", "深入",
                          "--dry-run", factory=lambda r: probe.FakeClient("quiet"))
    assert rc == probe.RC_UNVERIFIED and payload["verdict"] == "unverified"
    assert payload["accepted"] is True
    assert payload["has_reasoning_content"] is False and payload["reasoning_tokens"] == 0


def test_rejected_parameter_is_not_read_as_unsupported(tmp_path):
    """参数被拒 ≠ 模型不支持思考：这一格只能写 unverified，绝不能写 unsupported。"""
    payload, rc, _ = _run(tmp_path, "--profile", "openai_reasoning_effort", "--level", "均衡",
                          "--dry-run", factory=lambda r: probe.FakeClient("error_plain"))
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
    ledger = _ledger(tmp_path / "iso")
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
    config = probe.make_config(["--data-dir", str(data_dir), "--provider", DEEPSEEK,
                                "--profile", "deepseek_thinking_type", "--level", "均衡",
                                "--grant", GRANT2,
                                "--ledger", str(_ledger(tmp_path / "iso")),
                                "--isolation-dir", str(tmp_path / "iso"),
                                # 这格绕开夹具直给命令行（要自己造 before 摘要），所以修复轮 1
                                # （I-2）新加的 live 必填 `--attempt` 也得在这里自己声明。
                                "--attempt", "1"])
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


# ---------------- 8. 修复轮 1：审查员 1 Critical + 4 Important + 3 Minor --
#
# 这一节守的不是上游，是**这一版探针自己的闸**。审查员的实测结论是：五条问题全在闸上，
# 而且都能被"操作者记得"暂时掩盖过去。所以每条判据都配一条已知应当相反的输入，
# 并当场实跑过红（变异名单与还原摘要写在 `C:/Users/34426/dl/sdd-takeover-20261101/`
# 那份报告的「修复轮 1」一节）。本节**零出站**：客户端一律是注入的替身。

def test_live_run_without_an_explicit_ledger_is_refused(tmp_path):
    """Critical-1 的一半：`--ledger` 缺省时账本落在**本次**隔离目录里，而 `isolate_runtime_env(None)`
    每次 `mkdtemp` 一个新目录 ⇒ 每个进程从空账本开始，`used=0` 恒成立，"总额"永不咬。
    修法是把默认拿掉：live 模式不给显式账本就 rc=2 拒发，**零出站、零写盘、连环境都不动**。"""
    data_dir = _write_config(tmp_path)
    argv = ["--data-dir", str(data_dir), "--provider", DEEPSEEK,
            "--profile", "deepseek_thinking_type", "--level", "均衡",
            "--isolation-dir", str(tmp_path / "iso")]
    build, seen = _never()
    env_before = dict(os.environ)
    payload, rc, _ = probe.run_probe(probe.make_config(argv), client_factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert payload["verdict"] == "preflight_failed"
    assert payload["isolation_ok"] is False, "账本没定就别开始隔离：这道闸必须在隔离之前"
    assert payload["transport"] is None, "一次都没发，不许报 transport"
    assert payload["call_slot"] is None
    assert not (tmp_path / "iso").exists(), "拒发这一支不许开出隔离目录"
    assert dict(os.environ) == env_before, "没走到隔离就不许改 PROVIDERS_DB_PATH / DATA_ROOT"
    assert any("--ledger" in note for note in payload["notes"]), payload["notes"]


def test_five_live_runs_against_one_persistent_ledger_stop_at_the_fourth(tmp_path):
    """Critical-1 的另一半：给了持久账本之后，**超出那一轮上限的那一格**必须被拒。
    本轮跑的是 `g1`（上限 4，事实已用尽），所以填 4 格真发（替身，零出站）后第 5 格
    连客户端都不许被构造。g2（上限 3）那一侧由 §10 的额度锁与下面的负控清单演。"""
    ledger = _ledger(tmp_path / "budget", grant=GRANT1)
    for seq in (1, 2, 3, 4):
        payload, rc, _ = _run(tmp_path, "--profile", "none", "--grant", GRANT1, "--attempt",
                              str(seq), "--ledger", str(ledger),
                              factory=lambda r: probe.FakeClient("reasoning"))
        assert rc == probe.RC_VERIFIED and payload["call_slot"] == seq, seq
    assert len(probe.read_ledger(ledger)) == 4
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "none", "--grant", GRANT1,
                          "--ledger", str(ledger), factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert payload["error_class"] == "BudgetExhausted"
    assert payload["transport"] is None
    assert len(probe.read_ledger(ledger)) == 4, "被拒的第五格不许把账本撑到 5"


def test_authorized_live_call_count_is_the_grant_itself(tmp_path):
    """Important-2 → §R1：授权不再是一个数字，而是**一张授权轮次表**。这个数字原本只被
    上一格当填充条数用（尺子与被测同源），审查员把常量改成 9 ⇒ 83 条全绿。所以逐轮字面钉：
    格数、形状那一格、引文/目的/账本文件名，以及**每轮的 `allowed_shapes` 只能是候选表里
    已知的 provider × profile 对**。授权用尽后不自动续，要更多次需要用户新的明示。
    """
    grants = probe.AUTHORIZED_GRANTS
    assert probe.CALLS_PER_RUN == 1, "§R6：单次运行也只许 1 条，第二条就是超授权（闸原样活着）"
    assert set(grants) == {GRANT1, GRANT2}, "新增/删除一轮授权 = 需要用户新的明示授权"
    assert grants[GRANT1]["calls"] == 4, "g1 那一轮就是 4 次（两上游各 1 基线 + 1 带参数）"
    assert grants[GRANT2]["calls"] == 3, "g2 那一轮就是 3 次（OFF 方向），不是 4 也不是任意数"
    for gid in (GRANT1, GRANT2):
        g = grants[gid]
        assert g["max_tokens"] == 8, "§R2：两轮授权的那一格都是 8，改宽度等于换实验"
        assert str(g["quote"]).strip(), "引文必须原样落在代码里，不然是谁授权的说不清"
        assert str(g["purpose"]).strip(), "目的写不清，下一轮就会被读成「顺便多试一种形状」"
        assert str(g["ledger_name"]).strip() and str(g["authorized_on"]).strip()
        # 白名单只能是**已知 provider × 已知 profile** 的组合（不许漂到表外配方上）
        known_providers = {DEEPSEEK, RELAY}
        known_profiles = set(thinking_profiles.probe_profile_ids())
        for provider_id, shape in g["allowed_shapes"]:
            assert provider_id in known_providers, (gid, provider_id)
            assert shape in known_profiles, (gid, shape)
    # 一份授权绑一份权威账本。这里先钉**字面文件名**（修复轮 1，I-1）：上面那句
    # `str(g["ledger_name"]).strip()` 只要求"非空"，夹具 `_ledger()` 又是**从表里读**这个名字的，
    # 于是把两轮的花名一起改掉（仍含 grant id、仍互不相同）时 138 格全绿——闸还在，
    # 但它认的那份文件被换掉了，计数从 0 开始。字面钉住之后，换名=改代码+**这一格红**。
    assert probe.AUTHORIZED_GRANTS["g1-20261101"]["ledger_name"] == "call-ledger-g1-20261101.jsonl"
    assert probe.AUTHORIZED_GRANTS["g2-20261102"]["ledger_name"] == "call-ledger-g2-20261102.jsonl"
    # 两条结构判据（不是互不相同的装饰）：① 每轮的名字必须以该轮 grant id 为子串；
    # ② 两轮互不相同。共用的话计数口径就从「这一轮」退回「这个文件」。
    names = {grants[GRANT1]["ledger_name"], grants[GRANT2]["ledger_name"]}
    assert len(names) == 2, "两轮共用一个文件名的话，计数口径就从「这一轮」退回「这个文件」"
    assert GRANT1 in grants[GRANT1]["ledger_name"] and GRANT2 in grants[GRANT2]["ledger_name"]
    # 行为那一半：判据的上限必须**显式取自轮次表**，`reserve_call_slot` 不再有自己的默认值。
    # 常量漂到 9、或者有人把默认值加回来 ⇒ 这两句一起红。
    signature = inspect.signature(probe.reserve_call_slot)
    assert signature.parameters["limit"].default is inspect.Parameter.empty, \
        "limit 不许再有默认值：判据必须显式读 AUTHORIZED_GRANTS[grant][\"calls\"]"
    ledger = _ledger(tmp_path / "iso")
    _fill_ledger(ledger, 3)
    limit = grants[GRANT2]["calls"]
    with pytest.raises(probe.BudgetExhausted):
        probe.reserve_call_slot(ledger, DEEPSEEK, "none", None, None, limit)
    assert len(probe.read_ledger(ledger)) == 3, "被拒的那一次不许留痕"


def test_overlap_with_the_live_data_is_refused_before_providers_is_imported(tmp_path, monkeypatch):
    """Important-3 的顺序锁：原顺序是 `isolate → load_backend_modules → if overlaps → assert`，
    而 **import providers 就会跑 `ProviderStore._load()`**：缺文件时 `_seed_from_env()`
    （读 `DEEPSEEK_API_KEY`）非空即 `_flush()` 落盘三个文件 ⇒
    `--isolation-dir <data-dir>/iso` 会在声明只读的 `--data-dir` **里面**开出
    `iso/providers.json` + `iso/provider_keys.json`。AGENTS.md §1 说 `data/` 是受保护运行期资产。
    修法：重叠判据前移到 import 之前。把那句 `overlaps` 挪回 `load_backend_modules` 之后，
    这一格立刻红（`imported` 非空 + 目标树被写了）。"""
    data_dir = _write_config(tmp_path)
    before = probe.files_digest(data_dir)
    monkeypatch.setenv("DEEPSEEK_API_KEY", SHAPEY)      # 播种的燃料：没这颗 `_seed_from_env` 会返回空表
    imported = []

    def spy(isolation_dir):
        imported.append(isolation_dir)
        raise AssertionError("重叠时一次都不许走到 import app.core.providers")

    monkeypatch.setattr(probe, "load_backend_modules", spy)
    config = probe.make_config(["--data-dir", str(data_dir), "--provider", DEEPSEEK,
                                "--profile", "none", "--dry-run",
                                "--isolation-dir", str(data_dir / "iso")])
    build, seen = _never()
    payload, rc, _ = probe.run_probe(config, client_factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert imported == [], "重叠判据必须在 import 之前——import 本身就会写盘"
    assert not (data_dir / "iso").exists()
    assert probe.files_digest(data_dir) == before, "声明只读的那棵树必须逐字节没动"
    assert payload["isolation_ok"] is False


def test_files_digest_covers_everything_under_the_data_dir(tmp_path):
    """Important-3 的尺子锁：原来只盯顶层那两个名字，所以"`iso/` 子目录被开出来"这件事
    它**根本看不见**——那道闸和它所量的洞一起瞎。改成递归覆盖整棵树之后：
    多一个子目录、多一个深层文件、深层文件改一个字节，都要动。"""
    data_dir = _write_config(tmp_path)
    before = probe.files_digest(data_dir)
    (data_dir / "iso").mkdir()
    assert probe.files_digest(data_dir) != before, "只多一个空子目录也是一种写盘"
    deep = data_dir / "iso" / "deeper"
    deep.mkdir()
    (deep / "provider_keys.json").write_text("{}", encoding="utf-8")
    mid = probe.files_digest(data_dir)
    assert mid != before, "深层子目录里的文件也在射程内（原尺子看不见）"
    assert probe.files_digest(data_dir) == mid, "同一棵树原地不动时必须相等——不许是恒红的尺子"
    (deep / "provider_keys.json").write_text('{"a": 1}', encoding="utf-8")
    assert probe.files_digest(data_dir) != mid
    # 负控的最后一半：目录不存在时仍是"missing"那一格，既有那格用例的口径不变
    assert set(probe.files_digest(tmp_path / "nope").values()) == {"missing"}


def test_none_profile_label_describes_the_request_not_the_model():
    """Important-4 的第 3 格：`none` 那格旧 label 是「不使用思考（基线）」。
    基线 #1/#3 证明这两个上游**不发参数也在思考** ⇒ 这句话对**参数**诚实、
    对**模型行为**不诚实，界面照着写就成了"已关闭思考"。label 只能说"服务端不发这颗参数"。"""
    entry = thinking_profiles.PROBE_PROFILES[thinking_profiles.NONE_PROFILE]
    assert "不使用思考" not in entry["label"], "不许宣称模型不思考，只能宣称不发这颗参数"
    assert "不发思考参数" in entry["label"] and "不代表模型不思考" in entry["label"]
    assert "不发任何思考参数" in entry["shape"]


def test_handoff_payload_declares_what_the_three_levels_actually_do(tmp_path):
    """Important-4 第 1 格的锁：`level_used` 与"三档折算成出网值还剩几个"必须在交接物里。
    原来"三档同一个出网值"这件事只写在候选表的注释和测试里，交接那 4 行一个字不提 ⇒
    Task 12 照着填三档，界面就是三个点、wire 上一个值，而且没人会报错。"""
    same, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_type", "--level", "均衡",
                       factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_VERIFIED
    assert same["level_used"] is False, "这一支三档同一个出网值，标志必须说实话"
    assert same["level_distinct_wire_values"] == 1
    # 负控：真的三档不同那一支（openai_reasoning_effort）必须报 True / 3
    three, rc2, _ = _run(tmp_path, "--profile", "openai_reasoning_effort", "--level", "深入",
                         "--dry-run", factory=lambda r: probe.FakeClient("reasoning"))
    assert rc2 == probe.RC_VERIFIED
    assert three["level_used"] is True and three["level_distinct_wire_values"] == 3
    # 负控：基线那一格没有档位可言，必须是 None 而不是 1（"1"会被读成"三档塌成一档"）
    base, rc3, _ = _run(tmp_path, "--profile", "none", factory=lambda r: probe.FakeClient("quiet"))
    assert rc3 == probe.RC_VERIFIED and base["level_distinct_wire_values"] is None


def test_handoff_payload_carries_the_scope_the_evidence_actually_supports(tmp_path):
    """Important-4 第 2 格的锁：结论必须自带射程。Task 12 的消费端是
    `enabled: status === "verified"` + 档位候选直接吃服务端 `thinking_levels`，
    裸「已核实」在那头就长成"三档可切换"。改回裸「已核实」这一格必须红（变异实测过）。"""
    payload, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_type", "--level", "均衡",
                          factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_VERIFIED
    label = payload["verdict_label"]
    assert label.strip() != "已核实", "「已核实」三个字不许单独出现在结论里"
    assert "已核实" in label and "未证" in label, label
    assert payload["scope"]["tier"] == "shape"
    assert payload["scope"]["sent"] is True
    assert payload["scope"]["not_proved"], "射程字段不许空着让下游自己猜"
    assert "不是模型级" in payload["scope"]["covers"]
    # 锁 2 的负控：预检被拒那一格也有射程字段，且明说"一次都没发"
    # （档位传表外标签就走 `probe_kwargs` 那句 ValueError；`none` 那一档不吃档位，所以换一支）
    blocked_payload, rc3, _ = _run(tmp_path, "--profile", "openai_reasoning_effort",
                                   "--level", "瞎填", "--dry-run")
    assert rc3 == probe.RC_BLOCKED
    assert blocked_payload["scope"]["tier"] == "none"
    assert blocked_payload["scope"]["sent"] is False


def test_baseline_row_reports_that_the_upstream_was_already_reasoning(tmp_path):
    """Important-4 的第 3 格（机器可读那一半）：基线那一格必须把"不发参数也在出推理"
    这件事写进 payload，否则 Task 12 的界面会把它写成"已关闭思考"。"""
    loud, rc, _ = _run(tmp_path, "--profile", "none",
                       factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_VERIFIED and loud["baseline_had_reasoning"] is True
    assert "不代表" in loud["verdict_label"] and "已核实" not in loud["verdict_label"]
    assert loud["scope"]["not_proved"], "基线那一格也要带射程"
    # 负控 A：基线安静时必须是 False，不许留 None 让下游自己猜
    quiet, rc2, _ = _run(tmp_path, "--profile", "none", factory=lambda r: probe.FakeClient("quiet"))
    assert rc2 == probe.RC_VERIFIED and quiet["baseline_had_reasoning"] is False
    # 负控 B：非基线那一格没有"基线推理"这件事可报，留 None
    other, _rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_type", "--level", "均衡",
                         factory=lambda r: probe.FakeClient("reasoning"))
    assert other["baseline_had_reasoning"] is None


def test_error_excerpt_is_redacted_before_it_is_truncated(tmp_path):
    """Important-5：`redact(f"{type}:{exc}"[:180], ...)` 先截后遮。值级整值替换和出口
    整份体检都要求**完整值在场**，于是一颗没有凭据形状的 key 跨在 180 边界上时，
    会漏出前缀碎片而两层都不认（审查员直接调 `render_and_check` 实测三组样本
    `exit_gate=allows print`）。修法：先脱敏、再截断、出口再体检一遍。"""
    padding = "x" * 150          # 前缀 21+5+150=176，让 key 正好跨在 180 那条边上
    exc = probe.FakeBadRequestError("上游拒绝 " + padding + SHAPELESS)
    out = probe.error_excerpt(exc, [SHAPELESS], providers.scrub_secrets, pathmask.hide_paths)
    assert SHAPELESS not in out
    for n in range(4, len(SHAPELESS) + 1):
        assert SHAPELESS[:n] not in out, "漏出 %d 字的 key 前缀碎片就是泄露" % n
    assert len(out) <= 180
    # 出口那道整份体检对这一格是**放行**的（它本来就认不出半截 key），所以截断顺序
    # 是唯一的防线——这里同时证明"改完之后这仍是一句能打印的话"。
    assert probe.render_and_check({"error_excerpt": out}, [SHAPELESS],
                                  providers.scrub_secrets)


def test_max_tokens_beyond_the_authorized_shape_is_refused(tmp_path):
    """Minor：授权形状是 `max_tokens=8`，而它是自由命令行值——改成 2000 也照发就是越权
    （这一格花的是钱）。live 模式越出形状就拒，且**在记账之前**拒。"""
    ledger = _ledger(tmp_path / "iso")
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "none", "--attempt", "1",
                          "--ledger", str(ledger), "--max-tokens", "2000", factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert ledger.exists() is False, "越权的形状不许占额度"
    assert payload["transport"] is None
    assert payload["request_shape"]["max_tokens"] == 2000, "拒了也要如实记下它想发什么"
    assert any("max_tokens" in note for note in payload["notes"]), payload["notes"]
    # 负控 A：授权形状内（8）照走
    ok, rc2, _ = _run(tmp_path, "--profile", "none", "--max-tokens", "8",
                      factory=lambda r: probe.FakeClient("reasoning"))
    assert rc2 == probe.RC_VERIFIED
    # 负控 B：dry-run 零出站，不受这一道约束（拿来演别的形状才是安全的）
    dry, rc3, _ = _run(tmp_path, "--profile", "none", "--dry-run", "--max-tokens", "2000")
    assert rc3 == probe.RC_VERIFIED and dry["request_shape"]["max_tokens"] == 2000
    # 另一半：0 与负数也不是"更小所以更省"，那是发不出去也照样占额度的畸形形状
    for bogus in ("0", "-5"):
        b, s = _never()
        blocked_payload, rc4, _ = _run(tmp_path, "--profile", "none", "--max-tokens", bogus,
                                       "--ledger", str(_ledger(tmp_path / "iso")),
                                       factory=b)
        assert rc4 == probe.RC_BLOCKED and s == [], bogus
        assert blocked_payload["request_shape"]["max_tokens"] == int(bogus)


def test_ledger_pointed_into_the_read_only_data_dir_is_refused(tmp_path):
    """Critical-1 的配套：账本从此必填，那它就有可能被指进 `--data-dir`——而那一轮它
    是声明只读的（每发一次都要在那棵树里写一笔，`files_digest` 从此只会一直红）。
    预检就拒：零出站、零写盘。"""
    data_dir = _write_config(tmp_path)
    inside = data_dir / "call-ledger.jsonl"
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "none", "--ledger", str(inside), factory=build)
    assert rc == probe.RC_BLOCKED and seen == [] and payload["transport"] is None
    assert inside.exists() is False
    assert any("--ledger" in n and "--data-dir" in n for n in payload["notes"]), payload["notes"]
    # 负控：账本在树外时这一条必须放行（否则这道闸是恒红的装饰）
    ok, rc2, _ = _run(tmp_path, "--profile", "none",
                      "--ledger", str(_ledger(tmp_path / "out")),
                      factory=lambda r: probe.FakeClient("reasoning"))
    assert rc2 == probe.RC_VERIFIED and ok["call_slot"] == 1


def test_transport_is_only_stamped_when_the_call_is_actually_attempted(tmp_path, monkeypatch):
    """Minor：`transport: "openai_sdk"` 原本在 `preflight_failed`（一次都没发）时也照打，
    与 `live: true` 并读会被当成"发过"。改成只在真进入传输那一步才打。
    真发那一支用替身顶掉 `build_live_client`——真客户端的唯一出口被换掉，没有 socket 被创建。

    修复轮 2（N1）补上第三格：dry-run 走脚本自带的 `FakeClient`，上一版却把它打成
    `openai_sdk`——一行模拟数据与一行真证据在机器可读层只差 `live: false` 一格，而 §R8
    第 8 条恰恰拿 `transport` 当"零出站的机械凭据"、Task 12 抄的又正是这份 payload。
    所以 dry-run 那一支归 `fake_client`，且 `scope.simulated` 必须自己把话说死。"""
    blocked_payload, rc, _ = _run(tmp_path, "--profile", "openai_reasoning_effort",
                                  "--level", "瞎填", "--dry-run")
    assert rc == probe.RC_BLOCKED and blocked_payload["transport"] is None
    sent, rc2, _ = _run(tmp_path, "--profile", "none",
                        factory=lambda r: probe.FakeClient("reasoning"))
    assert rc2 == probe.RC_VERIFIED and sent["transport"] == "test_double"
    assert sent["scope"]["simulated"] is True, "注入替身那一行也必须自己声明是模拟"
    built = []

    def stub(provider, record, timeout=0):
        built.append(record["id"])
        return probe.FakeClient("reasoning")

    monkeypatch.setattr(probe, "build_live_client", stub)
    live, rc3, _ = _run(tmp_path, "--profile", "none",
                        "--ledger", str(_ledger(tmp_path / "b")))
    assert rc3 == probe.RC_VERIFIED and live["transport"] == "openai_sdk"
    assert built == [DEEPSEEK], "构造真客户端的唯一入口只许走一次"
    assert live["scope"]["simulated"] is False, "真发那一支的 simulated 必须是 False"
    # 第四格（本轮点名的那格）：CLI dry-run，不注入任何注入口，走脚本自带的假 client。
    dry, rc4, _ = _run(tmp_path, "--profile", "none", "--dry-run")
    assert rc4 == probe.RC_VERIFIED
    assert dry["transport"] == "fake_client", "dry-run 不许冒充 openai_sdk：那是谎报为真发"
    assert dry["live"] is False
    assert dry["scope"]["simulated"] is True, "同一行必须有一格显式的模拟标记，而不是只靠 live"


def test_the_other_checkout_predicate_judges_writes_not_reads(tmp_path, monkeypatch):
    """修复轮 2（C-1/F1）把这条判据的**口径**改了：它现在判的是「这个路径会不会被本工具
    写」，不再是「它属于哪棵树」。`--data-dir`（只读面）不再走它，`--ledger`（每发一次就
    写一笔的那一面）照旧走它。**函数本身保留**，所以这里锁的是它的返回值仍然诚实：
    另一棵 checkout 的顶层 `data/`、以及 N4 那棵深层子树（`<other>/data/swap-snapshots/
    snap-1/data`，同样一份 providers.json + provider_keys.json）都必须报 True——判据只看
    各级祖先目录有没有 `.git`，沿**整条父链**上溯，只查一层等于换个子目录就绕开。
    运行形状那一半（读放行 / 写还咬）在本文件 §11。"""
    other = tmp_path / "checkout"
    (other / ".git").mkdir(parents=True)
    live = other / "data"
    live.mkdir()
    deep = live / "swap-snapshots" / "snap-1" / "data"
    deep.mkdir(parents=True)
    (deep / "providers.json").write_text("[]", encoding="utf-8")
    (deep / "provider_keys.json").write_text("{}", encoding="utf-8")
    assert probe.data_dir_belongs_to_another_checkout(deep) is True, "只查一层 = 换个子目录就绕开"
    assert probe.data_dir_belongs_to_another_checkout(live) is True
    # 负控：本工具自己那棵树里的 data/、以及宿主没有 .git 的夹具目录都必须放行
    assert probe.data_dir_belongs_to_another_checkout(REPO_ROOT / "data") is False
    assert probe.data_dir_belongs_to_another_checkout(_write_config(tmp_path)) is False

    def _no_open(*a, **kw):
        raise AssertionError("这条判据只许对祖先目录做 exists()，不许打开那棵树里的任何文件")

    # 「只加判据，绝不去读那份目录」这一句从散文变成锁：判据若在树上开任何一个文件句柄，
    # 下面四调用立刻红（它只做 `.exists()` 与 `.parent`，走的是 os.stat，不碰 Path.open）。
    monkeypatch.setattr(Path, "open", _no_open)
    assert probe.data_dir_belongs_to_another_checkout(deep) is True
    assert probe.data_dir_belongs_to_another_checkout(live) is True
    assert probe.data_dir_belongs_to_another_checkout(REPO_ROOT / "data") is False
    assert probe.data_dir_belongs_to_another_checkout(tmp_path / "checkout" / "data") is True


# ------------- 9. 修复轮 2：N1–N4 + concerns.1 残余 + concerns.3 软硬拆分 ----------
#
# 这一节全是**上一轮修复自己修出来的新洞**（审查员复审原话方向）：dry-run 那一行谎报为
# 真发、重叠判据停在词法层、"账本落在仓库外"又回到"由操作者记得"、另一棵 checkout 只查
# 父级一层、递归尺子把 300 秒定时器判成凶手。全部零出站：注入替身 / --dry-run。

def _tree_sha_listing(root: Path) -> dict:
    """测试自备的**逐字节**清单（内容 sha256）。`files_digest` 只 stat 不读内容，
    "目标树跑前跑后 sha256 清单一致"这条锁要的是字节级证据，得另拿一把尺子。"""
    out = {}
    for path in sorted(root.rglob("*"), key=lambda q: q.as_posix()):
        rel = path.relative_to(root).as_posix()
        out[rel] = "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _make_dir_link(link: Path, target: Path) -> bool:
    """造一个指向 `target` 的目录级重解析点：Windows 用 `mklink /J`（junction，非管理员
    即可），POSIX 用目录 symlink。两条路都走不通时返回 False，由调用方声明退到的
    "最接近的等价形状"（见那一格的 skip 文案）。"""
    try:
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, check=False)
        if done.returncode == 0 and link.is_dir():
            return True
    except OSError:
        pass
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except OSError:
        return False


def test_junction_isolation_dir_into_the_data_tree_is_refused(tmp_path, monkeypatch):
    """N2 第一层：`_norm` 原本只做 `abspath`+`normcase`，不解析重解析点。审查员造
    `mklink /J iso-junction → <data-dir>/iso`，跑 `--isolation-dir <junction> --dry-run`：
    词法判"不相干" → mkdir → import providers → `_seed_from_env()`→`_flush()`，
    **只读树里真的被开出两个文件**，payload 却报 `isolation_ok: true`、rc=0。
    修法：`_norm` 走 `os.path.realpath`。锁：目标树跑前跑后**逐字节**清单一致 + rc=2。"""
    data_dir = _write_config(tmp_path)
    hidden = data_dir / "iso"
    hidden.mkdir()                            # junction 的落点：data_dir 树里的一个真目录
    link = tmp_path / "iso-junction"
    if not _make_dir_link(link, hidden):
        pytest.skip("本机造不出目录级重解析点（mklink /J 与 symlink 都失败）；"
                    "最接近的等价形状是 normcase 已挡的大小写/斜杠漂移，那条既有负控仍要过")
    before = _tree_sha_listing(data_dir)
    monkeypatch.setenv("DEEPSEEK_API_KEY", SHAPEY)   # 播种燃料：没有它 _seed_from_env 返回空表
    config = probe.make_config(["--data-dir", str(data_dir), "--provider", DEEPSEEK,
                                "--profile", "none", "--dry-run",
                                "--isolation-dir", str(link)])
    build, seen = _never()
    payload, rc, _ = probe.run_probe(config, client_factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert payload["isolation_ok"] is False
    assert not (hidden / "providers.json").exists(), "junction 绕开词法判据后依旧不许写树"
    assert _tree_sha_listing(data_dir) == before, "只读树必须逐字节没动（清单是内容 sha256）"


def test_the_readonly_ruler_starts_before_isolation(tmp_path, monkeypatch):
    """N2 第二层（顺序）：`before = files_digest(...)` 原本取在 SDK 签名检查之后
    （import 之后）——递归化并没有让这把尺子**看得见 import 那一刻**的写盘。
    修法：快照提到 `isolate_runtime_env()` 之前（`data_dir` 在闸 0 就已定）。
    复现形状：把隔离函数换成"顺手往只读树里开一个文件"的替身——旧顺序里这个文件
    在 before 之前就存在，after == before ⇒ 假绿灯；新顺序里它是**多出的路径** ⇒ 硬判 rc=2。"""
    data_dir = _write_config(tmp_path)
    real_isolate = probe.isolate_runtime_env

    def dirty_isolate(isolation_dir=None):
        (data_dir / "seeded-during-import.json").write_text("{}", encoding="utf-8")
        return real_isolate(isolation_dir)

    monkeypatch.setattr(probe, "isolate_runtime_env", dirty_isolate)
    config = probe.make_config(["--data-dir", str(data_dir), "--provider", DEEPSEEK,
                                "--profile", "none", "--dry-run",
                                "--isolation-dir", str(tmp_path / "iso")])
    payload, rc, _ = probe.run_probe(config, client_factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_BLOCKED, "隔离/import 期间的写盘必须在尺子射程内"
    assert any("只读承诺被打破" in n and "seeded-during-import.json" in n
               for n in payload["notes"]), payload["notes"]


def test_ledger_inside_the_repository_is_refused(tmp_path):
    """N3：docstring 明写"账本落在仓库外"，预检却只判 `overlaps(ledger, data_dir)`。
    实测：账本**真的写进了那棵树的 `data/call-ledger.jsonl`**，rc=0、notes 一个字没有。
    这句话从此由机制守：账本在本仓树内 ⇒ 预检拒（本仓目录同时是生产机，AGENTS.md §1）。
    成对负控：树外、且父链上没有 `.git` 的路径必须放行。"""
    ledger = REPO_ROOT / "data" / "call-ledger-round2-lock.jsonl"
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "none", "--attempt", "1",
                          "--ledger", str(ledger), factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert ledger.exists() is False, "拒发这一支不许在仓库里留下一个字节"
    assert payload["transport"] is None and payload["call_slot"] is None
    assert any("--ledger" in n and "仓库外" in n for n in payload["notes"]), payload["notes"]
    # 负控：仓库外（本夹具的 tmp 树，父链上没有 .git）照旧放行
    outside = _ledger(tmp_path / "out-of-repo")
    ok, rc2, _ = _run(tmp_path, "--profile", "none", "--ledger", str(outside),
                      factory=lambda r: probe.FakeClient("reasoning"))
    assert rc2 == probe.RC_VERIFIED and ok["call_slot"] == 1


def test_ledger_of_another_checkout_is_refused(tmp_path):
    """N3 的另一半：账本不许属于**另一棵 checkout**（判据与 --data-dir 同一条，
    复用 `data_dir_belongs_to_another_checkout`，N4 之后它沿父链上溯）。"""
    other = tmp_path / "other-checkout"
    (other / ".git").mkdir(parents=True)
    (other / "data").mkdir()
    ledger = other / "data" / "call-ledger.jsonl"
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "none", "--ledger", str(ledger), factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert ledger.exists() is False and not (other / "data" / "call-ledger.jsonl").exists()
    assert any("checkout" in n for n in payload["notes"]), payload["notes"]


def test_another_checkout_deep_tree_is_readable_but_still_not_writable(tmp_path):
    """N4 的两半在修复轮 2（C-1/F1）之后**分成两件事**，不再是"整棵树一律拒"：
    ① `<other>/data/swap-snapshots/snap-1/data` 这份配置**可以读**——F1 删掉的正是
       预检里针对 `--data-dir` 的那一次判据，而读现网记录是本工具唯一的用途；
    ② 往这棵**别的 checkout** 里**写**（`--ledger`）仍然 rc=2，N4"沿整条父链上溯找
       `.git`"那一半牙还在——深树正是旧判据（只看 `target.parent` 一层）放行的形状。
    零出站：① 走 `--dry-run`（脚本自带的假 client），② 撞闸用的工厂是被碰就抛的哨兵。"""
    other = tmp_path / "other-checkout"
    (other / ".git").mkdir(parents=True)
    deep = other / "data" / "swap-snapshots" / "snap-1" / "data"
    deep.mkdir(parents=True)
    (deep / "providers.json").write_text(json.dumps(
        [{"id": "p_probe_deep", "base_url": FAKE_HOST, "model": "probe-flash"}]), encoding="utf-8")
    (deep / "provider_keys.json").write_text(json.dumps({"p_probe_deep": SHAPEY}), encoding="utf-8")
    before = _tree_sha_listing(deep)
    # ① 读：预检放行，且 `credential_fp` 必须在场——不然"放行"只是没炸，不是办成了用途。
    #     这一跑**不注入**任何注入口，走的就是 CLI 那条 dry-run（`--fake quiet`），
    #     所以 `transport` 是那一条 `fake_client`；注入替身那一格在 §3.1 正面锁里演。
    dry, rc, _ = probe.run_probe(probe.make_config(
        ["--data-dir", str(deep), "--provider", "p_probe_deep", "--profile", "none",
         "--dry-run", "--fake", "quiet", "--isolation-dir", str(tmp_path / "iso")]))
    assert rc == probe.RC_VERIFIED and dry["transport"] == "fake_client"
    assert dry["credential_fp"], "读不到那条记录 = 这个工具没有用途（审查员 C-1 的形状）"
    assert not any("checkout" in n for n in dry["notes"]), dry["notes"]
    assert _tree_sha_listing(deep) == before, "读这一趟不许动那棵树一个字节"
    # ② 写：同一棵别的 checkout 里放账本 ⇒ 仍然拒，且一个字节都不许多。
    #    落点刻意选在 `--data-dir`（那棵深树）**之外**、`other/.git` 父链**之内**——
    #    于是三条 `--ledger` 判据里只剩 N4 那条"沿整条父链上溯找 .git"在说话。
    ledger = other / "data" / "swap-snapshots" / probe.AUTHORIZED_GRANTS[GRANT2]["ledger_name"]
    other_before = _tree_sha_listing(other)
    build, seen = _never()
    blocked_payload, rc2, _ = probe.run_probe(probe.make_config(
        ["--data-dir", str(deep), "--provider", "p_probe_deep", "--profile", "none",
         "--grant", GRANT2, "--attempt", "1", "--ledger", str(ledger),
         "--isolation-dir", str(tmp_path / "iso")]), client_factory=build)
    assert rc2 == probe.RC_BLOCKED and seen == [] and not ledger.exists()
    assert blocked_payload["transport"] is None and blocked_payload["call_slot"] is None
    assert any("checkout" in n for n in blocked_payload["notes"]), blocked_payload["notes"]
    assert _tree_sha_listing(deep) == before
    assert _tree_sha_listing(other) == other_before, "拒发那一支不许在别人的树里留一个字节"


def test_fresh_ledger_run_declares_the_gate_counts_per_file(tmp_path):
    """concerns.1 的残余（审查员判可接受，但要求登记 + 一句提示）：闸绑的是"这一份账本
    文件 ≤那一轮的格数"而不是"这次授权 ≤格数"——换 `--ledger` 路径就从 0 重计（实测两条
    路径连发 8 格全放行、notes=[] 零提示）。跨账本累计不修（一次性探针不值那道工程税），
    换文件也不再是加个 flag：§R1 把文件名钉进了授权轮次表，要对上一轮的文件名就得改代码
    + 用户再授权。但**新建账本**那一跑必须自己把计数口径说出来，且**只在真新建时**说。"""
    ledger = _ledger(tmp_path / "budget")
    payload, rc, _ = _run(tmp_path, "--profile", "none", "--attempt", "1",
                          "--ledger", str(ledger),
                          factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_VERIFIED
    assert any("新建账本" in n and "跨账本不累计" in n for n in payload["notes"]), payload["notes"]
    # 同一份账本的第二次跑：文件已存在 ⇒ 这句不许再出现（否则"新建"成了套话）
    payload2, rc2, _ = _run(tmp_path, "--profile", "none", "--attempt", "2",
                            "--ledger", str(ledger),
                            factory=lambda r: probe.FakeClient("reasoning"))
    assert rc2 == probe.RC_VERIFIED
    assert not any("新建账本" in n for n in payload2["notes"]), payload2["notes"]


def test_digest_delta_separates_hard_from_soft():
    """concerns.3 判据单元的负控：软的一格（既有无关文件时间戳/大小变了）不许升成硬，
    硬的两格（store 三件套变了、树里多/少路径）不许降成软。"""
    before = {"providers.json": "a", "provider_keys.json": "b", "preference.txt": "c"}
    moved = dict(before, **{"preference.txt": "c2"})
    assert probe.digest_delta(before, moved) == ([], ["preference.txt"])
    store = dict(before, **{"providers.json": "a2"})
    assert probe.digest_delta(before, store)[0] == ["providers.json"]
    added = dict(before, **{"iso": "d"})
    assert probe.digest_delta(before, added)[0] == ["iso"]
    assert probe.digest_delta(added, before)[0] == ["iso"], "少一个路径同样是结构变化"


def test_unrelated_file_rewritten_during_the_window_stays_pass_with_a_note(tmp_path):
    """concerns.3 软那一格：现网 300 秒定时器（main.py:426-442 → analyze_all_preferences
    无条件重写 `data/preference.txt`）在调用窗口内写一下，旧写法整棵树摘要一变就 rc=2
    ——**那一格付费额度已经花掉**，误红等于烧钱 + 把下一次真红训成"狼来了"。
    新判据：无关既有文件被重写 → rc 按原判据走（这里是 baseline_ok → 0），只追加一条
    **点名是哪几项**的 note。注入点选在替身被调用的那一刻，正落在 before/after 窗口里。"""
    data_dir = _write_config(tmp_path)
    (data_dir / "preference.txt").write_text("old\n", encoding="utf-8")

    def build(record):
        (data_dir / "preference.txt").write_text("timer rewrote this line, longer\n",
                                                 encoding="utf-8")
        return probe.FakeClient("reasoning")

    payload, rc, _ = _run(tmp_path, "--profile", "none", "--dry-run", factory=build)
    assert rc == probe.RC_VERIFIED, "无关文件被定时器重写不该作废本次结论"
    assert payload["runtime_data_untouched"] is False
    assert any("preference.txt" in n for n in payload["notes"]), payload["notes"]
    assert not any("只读承诺被打破" in n for n in payload["notes"])


def test_store_owned_file_rewritten_during_the_window_is_hard(tmp_path):
    """concerns.3 硬那一格 A：store 拥有的三份文件（providers/provider_keys/provider_prefs）
    变了 ⇒ rc=2、结论作废——这才是 Important-3 要挡的形状。"""
    data_dir = _write_config(tmp_path)

    def build(record):
        (data_dir / "providers.json").write_text("[1,2,3] some longer payload", encoding="utf-8")
        return probe.FakeClient("reasoning")

    payload, rc, _ = _run(tmp_path, "--profile", "none", "--dry-run", factory=build)
    assert rc == probe.RC_BLOCKED
    assert payload["runtime_data_untouched"] is False
    assert any("只读承诺被打破" in n and "providers.json" in n for n in payload["notes"])


def test_new_path_in_the_data_tree_during_the_window_is_hard(tmp_path):
    """concerns.3 硬那一格 B：树里**多出路径**（`iso/providers.json` 被开出来——
    正是修复轮 1 Important-3 的形状）⇒ rc=2，不许被"无关文件时间戳漂移"的软判顺路放过。"""
    data_dir = _write_config(tmp_path)

    def build(record):
        (data_dir / "iso").mkdir()
        (data_dir / "iso" / "providers.json").write_text("[]", encoding="utf-8")
        return probe.FakeClient("reasoning")

    payload, rc, _ = _run(tmp_path, "--profile", "none", "--dry-run", factory=build)
    assert rc == probe.RC_BLOCKED
    assert any("iso/providers.json" in n for n in payload["notes"]), payload["notes"]


# ------- 10. Task 11b：第二轮授权做成机制（§R1–§R5）+ 方向感知判据（§R4） --------
#
# 这一节守的是**发那 3 次之前的最后一道形状闸**，本身零出站：客户端一律是注入的替身或
# `--dry-run`。为什么判据长这样：上一轮 4 次证明了两条上游不发参数也在出推理，所以
# 「思考开关」今天没有任何一次证据证明它**关得掉**——这一轮就是冲着 OFF 方向去的，
# 而 OFF 方向最容易死在两处：① 授权是"一个数字"，换份账本文件就能重计；② 判据不看方向，
# 把"关成功了"读成"这次没拿到证据"。§10 的 15 格一格挡一处。

# —— §R1：授权轮次表 ——

def test_live_run_without_a_grant_is_refused(tmp_path):
    """§R1：live 模式除 `--ledger` 之外还要 `--grant <id>`。缺授权轮次 ⇒ rc=2、
    零出站、零记账、连隔离目录都不该开。"""
    ledger = _ledger(tmp_path / "iso")
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "none", "--ledger", str(ledger),
                          grant=None, factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert payload["transport"] is None and payload["call_slot"] is None
    assert payload["verdict"] == "preflight_failed"
    assert not ledger.exists(), "没定授权轮次就不许记账"
    assert any("--grant" in n for n in payload["notes"]), payload["notes"]


def test_live_run_without_an_explicit_attempt_is_refused(tmp_path):
    """I-2 的收口：live 模式 `--attempt` 从可选改成**必填**，与"live 必须显式给 `--ledger`"
    同一格、同一支判据（预检，零出站、零写盘、零记账、连环境都不动）。
    为什么这一格值得付"31 处既有用例补参数"的代价：上限绑的是**文件名**，目录每次调用都能换，
    而 `--attempt` 把「我以为这是第几次」和「账本上下一个序号是不是这个」变成机器对账。
    诚实边界（措辞有锁，见下面三条断言）：它**不**保证换目录不能重计——换目录 + 谎报
    `--attempt 1` 仍是操作者做得到的事；真正的权威是 **(grant, 账本路径) 这一对** 加上
    主控人留的跑批记录。这一条不是"防轮换"，谁写成防轮换谁就该红。"""
    data_dir = _write_config(tmp_path)
    ledger = _ledger(tmp_path / "iso")
    argv = ["--data-dir", str(data_dir), "--provider", DEEPSEEK, "--profile", "none",
            "--grant", GRANT2, "--ledger", str(ledger), "--isolation-dir", str(tmp_path / "iso")]
    build, seen = _never()
    env_before = dict(os.environ)
    payload, rc, _ = probe.run_probe(probe.make_config(argv), client_factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert payload["verdict"] == "preflight_failed"
    assert payload["transport"] is None and payload["call_slot"] is None
    assert payload["isolation_ok"] is False, "缺 --attempt 这一跑不许开始隔离"
    assert not (tmp_path / "iso").exists(), "预检拒掉的那一支连隔离目录都不该开"
    assert not ledger.exists(), "零记账：账本一行都不许有"
    assert dict(os.environ) == env_before, "没走到隔离就不许改 PROVIDERS_DB_PATH / DATA_ROOT"
    note = " ".join(payload["notes"])
    # 判据在场，且话只说两句能说的
    assert "--attempt" in note, payload["notes"]
    assert "机器对账" in note and "账本上下一个序号" in note, payload["notes"]
    assert "(grant, 账本路径)" in note, "拒绝理由必须点名真正的权威是这一对，而不是这个 flag"
    assert "防轮换" not in note, "这一道闸不许被写成防轮换——它挡不住换目录 + 谎报序号"
    # 那句诚实边界必须**整句到场**：`error_excerpt` 会把拒绝理由截到 180 字，写长了被切掉的
    # 恰好是尾巴（"…这一对 + 主控人的跑批记录。本次零出站"）——那是这句判据的射程本身。
    assert payload["notes"][-1].endswith("本次零出站"), payload["notes"][-1]
    # 负控（这条判据不是恒红的装饰）：同一条命令行补上 --attempt 1 就走得到传输那一步
    ok, rc2, _ = probe.run_probe(probe.make_config(argv + ["--attempt", "1"]),
                                 client_factory=lambda r: probe.FakeClient("quiet"))
    assert rc2 == probe.RC_VERIFIED and ok["call_slot"] == 1
    assert len(probe.read_ledger(ledger)) == 1


def test_five_sibling_ledger_dirs_with_declared_attempts_book_exactly_one(tmp_path):
    """I-2 的实测形状（审查员注入替身跑过：5 个目录 ⇒ 5 次 rc=0，只追了一条 note）。
    这里演**对照**那一半：同一 `--grant`、同一权威花名、五个兄弟目录，但每次都按
    「我以为这是第几次」显式声明 `--attempt 1..5` ⇒ 只成 1 次，其余四次
    `AttemptMismatch` + `rc=2`、`call_slot=None`，五份账本合计 1 行。
    零出站：客户端是注入替身，`--data-dir` 是夹具造的假配置（base_url 用 RFC 2606 的
    `.invalid`）。**这条锁不声称挡住"换目录 + 谎报 --attempt 1"**——那是上一格 note
    里明写的诚实边界，跨账本累计不在本单射程（concerns.1 的残余）。"""
    data_dir = _write_config(tmp_path)
    results = []
    for index in range(1, 6):
        ledger = _ledger(tmp_path / ("led%d" % index))
        argv = ["--data-dir", str(data_dir), "--provider", DEEPSEEK, "--profile", "none",
                "--grant", GRANT2, "--ledger", str(ledger),
                "--isolation-dir", str(tmp_path / "iso"), "--attempt", str(index)]
        payload, rc, _ = probe.run_probe(probe.make_config(argv),
                                         client_factory=lambda r: probe.FakeClient("quiet"))
        results.append((rc, payload["call_slot"], payload["error_class"], len(probe.read_ledger(ledger))))
    booked = [r for r in results if r[0] == probe.RC_VERIFIED]
    assert len(booked) == 1, "五个兄弟目录 + 各自声明的序号：只成一次 ⇒ %r" % (results,)
    assert booked[0][1] == 1 and sum(r[3] for r in results) == 1
    for rc, slot, error_class, lines in [r for r in results if r[0] != probe.RC_VERIFIED]:
        assert rc == probe.RC_BLOCKED and slot is None and lines == 0
        assert error_class == "AttemptMismatch", "换目录重计那一跑必须由序号对账挡下"


def test_unknown_grant_id_never_reaches_the_run_layer(tmp_path):
    """表外的授权 id 在 argparse 就被 `choices` 挡掉：拿一个不存在的那一轮去发，
    连 payload 都不该有——"表里有这一轮"是这道闸的前提，不是事后检查。"""
    with pytest.raises(SystemExit) as boom:
        probe.make_config(_argv(tmp_path, "--profile", "none", "--grant", "g9-表外的那一轮"))
    assert boom.value.code == 2


def test_live_ledger_renamed_in_the_same_directory_is_refused(tmp_path):
    """§R1 的核心那一步：这道闸本质上是**按账本文件计数**的，所以"哪一份文件是这一轮的
    权威账本"必须绑进授权表。同一目录、同一内容、只换文件名 ⇒ rc=2、零记账、零出站。
    以前"换一份 --ledger 就从 0 重计"是操作者的自觉（上一版只把它写进 note），
    现在轮换文件不再是"随手加个 flag"，而是"改代码 + 用户再授权"。"""
    canonical = _ledger(tmp_path / "iso")
    renamed = tmp_path / "iso" / "call-ledger-换了个名字.jsonl"
    _fill_ledger(renamed, 1)                      # 内容正常、位置也合法，只有名字不对
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "none", "--ledger", str(renamed), factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert payload["transport"] is None and payload["call_slot"] is None
    assert len(probe.read_ledger(renamed)) == 1, "被拒的这一跑不许在账上留痕"
    assert not canonical.exists()
    assert any("账本文件名" in n for n in payload["notes"]), payload["notes"]
    # 负控：本轮权威文件名必须放行（否则这道闸是恒红的装饰）
    ok, rc2, _ = _run(tmp_path, "--profile", "none", "--ledger", str(canonical),
                      factory=lambda r: probe.FakeClient("reasoning"))
    assert rc2 == probe.RC_VERIFIED and ok["call_slot"] == 1


def test_a_ledger_belonging_to_another_round_is_refused(tmp_path):
    """拿 g1 的权威账本文件名去请求 `--grant g2-20261102` ⇒ rc=2。
    单独演一遍是有原因的：文件名里**带着** grant id 只是"看起来对得上"，真判据是
    "等于本轮表里那一份"；而反向（g1 用它自己的名字）必须放行，两轮各用各的账本。"""
    g1_ledger = _ledger(tmp_path / "iso", grant=GRANT1)
    _fill_ledger(g1_ledger, 2)
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "none", "--grant", GRANT2,
                          "--ledger", str(g1_ledger), factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert len(probe.read_ledger(g1_ledger)) == 2, "串轮记账比不记账更糟"
    assert payload["call_slot"] is None and payload["transport"] is None
    ok, rc2, _ = _run(tmp_path, "--profile", "none", "--grant", GRANT1,
                      "--ledger", str(g1_ledger),
                      factory=lambda r: probe.FakeClient("reasoning"))
    assert rc2 == probe.RC_VERIFIED and ok["grant"] == GRANT1 and ok["grant_calls"] == 4
    assert ok["call_slot"] == 3, "g1 那份账本已经记了 2 格，本轮从第 3 格接着数"


def test_a_shape_outside_the_grants_whitelist_is_refused_without_booking(tmp_path):
    """§R1：`allowed_shapes` 是 provider × profile 的**白名单**。拿着活密钥时最容易
    漂出去的那句话是「既然都开了，顺便再试个别的形状」——表外组合 ⇒ rc=2、账本不涨、
    客户端一次都没被构造。三种漂移形状各演一次：
    ① 表内配方挂到表外 provider 上；② 整支配方不在任何一轮授权里；③ 反过来的挂法。"""
    cases = ((DEEPSEEK, "dashscope_enable_thinking", "均衡"),
             (DEEPSEEK, "openai_reasoning_effort", "深入"),
             (RELAY, "deepseek_thinking_type", "快速"))
    for index, (provider, profile, level) in enumerate(cases):
        ledger = _ledger(tmp_path / ("iso%d" % index))
        build, seen = _never()
        payload, rc, _ = _run(tmp_path, "--profile", profile, "--level", level,
                              "--ledger", str(ledger), provider_id=provider, factory=build)
        assert rc == probe.RC_BLOCKED and seen == [], (provider, profile)
        assert not ledger.exists(), (provider, profile, "白名单外不许占额度")
        assert payload["transport"] is None and payload["call_slot"] is None
        assert any("allowed_shapes" in n or "白名单" in n for n in payload["notes"]), payload["notes"]
        # 修复轮 1（M-4）：拒绝理由必须把**整份 (provider, profile) 对表**说出来。
        # 旧文案只打 provider 那一半却自称"provider × 配方组合"——撞闸的人拿不到合法配方名单，
        # 而 §R5 第 3 次怎么花正要看它。这一格把"话说到什么程度"钉住：说得出配对才许绿。
        note = " ".join(payload["notes"])
        for approved_provider, approved_shape in probe.AUTHORIZED_GRANTS[GRANT2]["allowed_shapes"]:
            assert approved_provider in note and approved_shape in note, (approved_provider,
                                                                         approved_shape, note)
        assert "provider × 配方组合" not in note, "旧那句把只列了 provider 的话说大了（M-4）"
    # 负控：白名单内的 (provider, profile) 必须放行，否则这道闸恒红
    ok, rc2, _ = _run(tmp_path, "--profile", "dashscope_enable_thinking_off", "--level", "快速",
                      provider_id=RELAY, factory=lambda r: probe.FakeClient("quiet"))
    assert rc2 == probe.RC_VERIFIED and ok["verdict"] == "switch_off_observed"


def test_the_budget_refusal_names_the_grant_and_its_limit(tmp_path):
    """§R1 的"判据必须显式取 `AUTHORIZED_GRANTS[grant]["calls"]`"那一格：
    g2 的上限是 3，所以填满 3 格之后第 4 次必须被拒，而且**拒绝理由要说出是哪一轮的几格**
    ——只报"额度用尽"的话，读的人不知道自己站在哪一轮上。"""
    ledger = _ledger(tmp_path / "iso")
    _fill_ledger(ledger, 3)
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "none", "--ledger", str(ledger), factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert payload["error_class"] == "BudgetExhausted"
    assert payload["grant"] == GRANT2 and payload["grant_calls"] == 3
    assert any("授权上限 3 次" in n for n in payload["notes"]), payload["notes"]
    assert len(probe.read_ledger(ledger)) == 3


def test_legacy_constants_are_views_and_not_the_source_of_the_gate():
    """§R1 允许把两个旧常量留作只读视图，但不许留"名字还在、判据不看它"的中间态：
    ① 两个视图必须**等于**当前轮次表项（漂了就红）；② `reserve_call_slot` 与
    `classify_verdict` 都不许再有那条默认值；③ §R2：任何一轮的 max_tokens 都不许 > 8。"""
    grants = probe.AUTHORIZED_GRANTS
    g2 = grants[GRANT2]
    assert probe.AUTHORIZED_LIVE_CALLS == g2["calls"], "旧名字必须只是视图，不许另存一份事实"
    assert probe.AUTHORIZED_MAX_TOKENS == g2["max_tokens"]
    assert probe.DEFAULT_MAX_TOKENS == 8
    assert max(g["max_tokens"] for g in grants.values()) == 8, \
        "§R2：OFF 方向的判据是同格从非零变 0，改宽度等于换了实验"
    assert max(g["calls"] for g in grants.values()) == 4, "两轮里最宽的那一轮也只有 4 格"
    for func, name in ((probe.reserve_call_slot, "limit"), (probe.classify_verdict, "direction")):
        parameter = inspect.signature(func).parameters[name]
        assert parameter.default is inspect.Parameter.empty, \
            "%s 的 %s 不许有默认值：判据必须显式取授权表/候选表" % (func.__name__, name)

# —— §R3：候选表加 OFF 方向，每支标 direction ——

def test_every_candidate_row_declares_its_direction():
    """§R3：遍历断言，**不写死名单**——表里每一支都必须显式带 `direction`，
    取值 ∈ {baseline, on, off}。缺字段或值在集合外就红。为什么要有这一格：判据（§R4）
    吃的就是它，而一支没有方向的配方落进 live 队列时，最省事的做法是"当成 on"——
    那正是本单最容易做错的一格。"""
    allowed = {"baseline", "on", "off"}
    for name, entry in thinking_profiles.PROBE_PROFILES.items():
        assert "direction" in entry, "%s 缺 direction 这一格" % name
        assert entry["direction"] in allowed, (name, entry["direction"])
    assert {e["direction"] for e in thinking_profiles.PROBE_PROFILES.values()} == allowed, \
        "三个方向都得在表里有样本，否则判据的那三支分支没人演过"


def test_the_off_candidates_carry_their_own_pinned_wire_shape():
    """OFF 两支的逐档字面形状（与 ON 那两支同构：`level_used=False`、三档同值）。
    写成字面量是有意的：这一格是"发出去的到底是什么"的唯一诚实记录。"""
    for level in thinking_profiles.THINKING_LEVEL_LABELS:
        kwargs = thinking_profiles.probe_kwargs("deepseek_thinking_disabled", level)
        assert kwargs == {"extra_body": {"thinking": {"type": "disabled"}}}, (level, kwargs)
        assert thinking_profiles.body_fragment(kwargs) == {"thinking": {"type": "disabled"}}
        off = thinking_profiles.probe_kwargs("dashscope_enable_thinking_off", level)
        assert off == {"extra_body": {"enable_thinking": False}}, (level, off)
        assert thinking_profiles.body_fragment(off) == {"enable_thinking": False}
    for name in ("deepseek_thinking_disabled", "dashscope_enable_thinking_off"):
        entry = thinking_profiles.PROBE_PROFILES[name]
        assert entry["direction"] == "off" and entry["level_used"] is False
        assert "disabled" in entry["shape"] or "false" in entry["shape"], entry["shape"]
    # 布尔那一格必须是**布尔**：`False == 0` 在 Python 里成立，只比字典相等会放过 0/""。
    fragment = thinking_profiles.body_fragment(
        thinking_profiles.probe_kwargs("dashscope_enable_thinking_off", "快速"))
    assert fragment["enable_thinking"] is False, "必须是布尔 False，不是 0 也不是空串"


def test_on_and_off_rows_are_paired_by_the_flipped_value():
    """§R3 配对锁（机械可判、不靠人记）：两对 ON/OFF 的出网体必须是**同一格、取值相反**。
    只断言"两支不相等"不够——两支一起漂到同一个错值时那条判据会笑，所以逐格钉住
    两头的字面值，末尾再单独钉一次 disabled 的字面形状。"""
    pairs = (("deepseek_thinking_type", "deepseek_thinking_disabled",
              {"thinking": {"type": "enabled"}}, {"thinking": {"type": "disabled"}}),
             ("dashscope_enable_thinking", "dashscope_enable_thinking_off",
              {"enable_thinking": True}, {"enable_thinking": False}))
    for on_name, off_name, on_wire, off_wire in pairs:
        for level in thinking_profiles.THINKING_LEVEL_LABELS:
            on_frag = thinking_profiles.body_fragment(
                thinking_profiles.probe_kwargs(on_name, level))
            off_frag = thinking_profiles.body_fragment(
                thinking_profiles.probe_kwargs(off_name, level))
            assert on_frag == on_wire, (on_name, level, on_frag)
            assert off_frag == off_wire, (off_name, level, off_frag)
            assert set(on_frag) == set(off_frag) == set(on_wire), "出网体只许有那一格"
            only_key = next(iter(on_wire))
            assert on_frag[only_key] != off_frag[only_key], "反方向的值必须真的相反"
    assert thinking_profiles.body_fragment(
        thinking_profiles.probe_kwargs("deepseek_thinking_disabled", "快速")) == {
        "thinking": {"type": "disabled"}}


def test_a_row_without_a_direction_is_refused_instead_of_guessed(tmp_path, monkeypatch):
    """§R4 的"取不到 direction 就 rc=2，**不许默认成 on**"：把 OFF 那一支的 direction 摘掉
    （= 表畸形），探针必须停在预检——零出站、零记账，并把"缺 direction"说在 note 里。"""
    broken = dict(thinking_profiles.PROBE_PROFILES)
    row = dict(broken["deepseek_thinking_disabled"])
    row.pop("direction")
    broken["deepseek_thinking_disabled"] = row
    monkeypatch.setattr(thinking_profiles, "PROBE_PROFILES", broken)
    ledger = _ledger(tmp_path / "iso")
    build, seen = _never()
    payload, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_disabled", "--level", "快速",
                          "--ledger", str(ledger), factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert not ledger.exists(), "方向不明的配方不许占额度"
    assert payload["verdict"] == "preflight_failed" and payload["transport"] is None
    assert payload["direction"] is None, "取不到方向就报 None，不许偷偷填一个 on"
    assert any("direction" in n for n in payload["notes"]), payload["notes"]

# —— §R4：判据方向感知（本单的心脏）——

def test_classify_verdict_takes_the_direction_and_refuses_to_guess_it():
    """判据本体逐格钉死（§R4 那张表）：四格走向 + 基线两格 + 异常之外的一切都不许
    被读成"关掉"。`direction` 没有默认值，表外值必须响亮报错而不是退成 on。"""
    quiet = probe.read_evidence(probe.FakeClient("quiet")._create())
    loud = probe.read_evidence(probe.FakeClient("reasoning")._create())
    assert probe.classify_verdict(False, loud, "on") == "verified"
    assert probe.classify_verdict(False, quiet, "on") == "unverified"
    assert probe.classify_verdict(False, quiet, "off") == "switch_off_observed"
    assert probe.classify_verdict(False, loud, "off") == "switch_off_not_observed"
    assert probe.classify_verdict(True, loud, "baseline") == "baseline_ok"
    assert probe.classify_verdict(True, quiet, "baseline") == "baseline_ok", \
        "基线只看这一跑通没通：有没有痕迹是那条上游的事，不是判据的事"
    for bogus in (None, "", "也许", "OFF"):
        with pytest.raises(ValueError):
            probe.classify_verdict(False, quiet, bogus)
    # "observed" 的三个前提缺一不可：被接受、无字段级痕迹、reasoning_tokens == 0。
    field_only = dict(quiet, has_reasoning_content=True)
    assert probe.classify_verdict(False, field_only, "off") == "switch_off_not_observed"
    tokens_only = dict(quiet, reasoning_tokens=7)
    assert probe.classify_verdict(False, tokens_only, "off") == "switch_off_not_observed"
    not_accepted = dict(quiet, accepted=False)
    assert probe.classify_verdict(False, not_accepted, "off") == "switch_off_not_observed", \
        "请求都没被接受，就说不上「关闭方向已观察到」"


def test_off_direction_with_the_trace_gone_is_the_evidence_we_paid_for(tmp_path):
    """**验收心脏（上半）**：OFF 方向 + `FakeClient("quiet")` ⇒ `switch_off_observed`、rc=0。
    上一版的 `classify_verdict` 在这一格给的是 `unverified` + rc=1——一次成功的关闭测试
    被标成"这次没拿到证据"，而它恰恰是这一轮唯一想要的东西（旧结论的实跑取证写在报告）。"""
    payload, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_disabled", "--level", "快速",
                          "--dry-run", factory=lambda r: probe.FakeClient("quiet"))
    assert rc == probe.RC_VERIFIED, "OFF 成功必须是 0：留 1 就等于对下一次说「没拿到证据」"
    assert payload["verdict"] == "switch_off_observed"
    assert payload["direction"] == "off" and payload["accepted"] is True
    assert payload["has_reasoning_content"] is False and payload["reasoning_tokens"] == 0
    assert payload["request_body"] == {"thinking": {"type": "disabled"}}
    assert payload["transport"] == "test_double" and payload["scope"]["simulated"] is True
    # 另一支 OFF（relay / 布尔的反方向）同方向同结论
    other, rc2, _ = _run(tmp_path, "--profile", "dashscope_enable_thinking_off",
                         "--level", "均衡", "--dry-run",
                         factory=lambda r: probe.FakeClient("quiet"))
    assert rc2 == probe.RC_VERIFIED and other["verdict"] == "switch_off_observed"
    assert other["request_body"] == {"enable_thinking": False}


def test_off_direction_that_keeps_reasoning_is_not_read_as_a_success(tmp_path):
    """**验收心脏（下半）**：OFF 方向 + `FakeClient("reasoning")` ⇒
    `switch_off_not_observed`、rc=1。上一版在这一格给的是 `verified` + rc=0——
    把"关不掉"报成"参数形状已核实"，Task 12 那头的 `enabled: status === "verified"`
    会直接把它抄成"这个开关可用"。"""
    payload, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_disabled", "--level", "快速",
                          "--dry-run", factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_UNVERIFIED and payload["verdict"] == "switch_off_not_observed"
    assert payload["accepted"] is True and payload["reasoning_tokens"] > 0
    assert payload["verdict_label"] == probe.VERDICT_LABELS["switch_off_not_observed"]
    assert any("对照" in n for n in payload["notes"]), payload["notes"]


def test_on_and_baseline_directions_survive_the_new_branch(tmp_path):
    """反向负控（摘掉方向分支、全判成 off 时这一格会红）：ON 那两支仍然**必须有痕迹**
    才算核实，基线仍然只看这一跑通没通。新判据是加分支，不是把老分支放宽。"""
    loud, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_type", "--level", "快速",
                       factory=lambda r: probe.FakeClient("reasoning"))
    assert rc == probe.RC_VERIFIED and loud["verdict"] == "verified" and loud["direction"] == "on"
    quiet, rc2, _ = _run(tmp_path, "--profile", "deepseek_thinking_type", "--level", "快速",
                         factory=lambda r: probe.FakeClient("quiet"))
    assert rc2 == probe.RC_UNVERIFIED and quiet["verdict"] == "unverified"
    base, rc3, _ = _run(tmp_path, "--profile", "none",
                        factory=lambda r: probe.FakeClient("quiet"))
    assert rc3 == probe.RC_VERIFIED and base["verdict"] == "baseline_ok"
    assert base["direction"] == "baseline" and base["baseline_had_reasoning"] is False


def test_an_off_call_that_threw_keeps_the_existing_verdicts(tmp_path):
    """§R4 表格最后一行：抛异常时**沿用** `form_unverifiable` / `unverified` 两支，
    不许因为方向是 off 就新造一个"关闭失败"——那会把"这条记录今天没打通"说成
    "思考关不掉"，是这一单里最贵的一类假结论。"""
    plain, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_disabled", "--level", "快速",
                        "--dry-run", factory=lambda r: probe.FakeClient("error_plain"))
    assert rc == probe.RC_UNVERIFIED and plain["verdict"] == "unverified"
    assert plain["direction"] == "off", "方向照打，结论仍然是旧的这两支之一"
    form, rc2, _ = _run(tmp_path, "--profile", "dashscope_enable_thinking_off",
                        "--level", "均衡", "--dry-run",
                        factory=lambda r: probe.FakeClient("error_form"))
    assert rc2 == probe.RC_UNVERIFIED and form["verdict"] == "form_unverifiable"


def test_the_two_new_verdicts_carry_their_own_scope():
    """§R4：两支新 verdict 的 label/scope 必须**自带射程**。`switch_off_observed` 只能说
    "这一格无痕迹"，不能说"开关有效"——单看它不能排除"这条上游今天本来就不出推理"，
    因果结论要靠同轮对照行，所以那句话必须写在 not_proved 里。"""
    assert probe.VERDICT_LABELS["switch_off_observed"] == \
        "本次响应无推理痕迹（同账本内的 ON/基线对照成立才等于开关有效）"
    assert probe.VERDICT_LABELS["switch_off_not_observed"] == \
        "参数被接受但仍在出推理：关闭方向未证实"
    for verdict in ("switch_off_observed", "switch_off_not_observed"):
        scope = probe.VERDICT_SCOPES[verdict]
        assert scope["sent"] is True and scope["not_proved"], "射程字段不许空着让下游自己猜"
        assert str(scope["tier"]).strip(), "tier 也要填：Task 12 是按这一格分层消费的"
    observed = " ".join(probe.VERDICT_SCOPES["switch_off_observed"]["not_proved"])
    assert "本来就不出推理" in observed and "对照" in observed, observed
    assert "参数被接受" in probe.VERDICT_SCOPES["switch_off_not_observed"]["proved"]


def test_no_label_or_proved_claim_says_the_switch_is_off():
    """§R4 的禁句锁：没有对照就不许写「已关闭思考」「思考已关闭」「支持关闭」。
    遍历所有 label 与每个 scope 的 `proved`，不写死 verdict 名单——以后加一支新结论
    也一样落在扫描里。"""
    banned = ("已关闭思考", "思考已关闭", "支持关闭")
    for name, label in probe.VERDICT_LABELS.items():
        for word in banned:
            assert word not in label, (name, word)
    for name, scope in probe.VERDICT_SCOPES.items():
        for word in banned:
            assert word not in scope["proved"], (name, word)


def test_payload_names_the_grant_the_limit_and_the_direction(tmp_path):
    """§R4 末尾：payload 除 `request_shape.max_tokens` 之外必须新增并钉住
    `grant` / `grant_calls` / `direction`。dry-run 时 grant 允许是 None，
    但 direction 必须照打——读 payload 的人不该靠自己回忆这一跑属于哪一轮。"""
    dry, rc, _ = _run(tmp_path, "--profile", "deepseek_thinking_disabled", "--level", "快速",
                      "--dry-run", grant=None, factory=lambda r: probe.FakeClient("quiet"))
    assert rc == probe.RC_VERIFIED
    assert dry["grant"] is None and dry["grant_calls"] is None
    assert dry["direction"] == "off", "不属任何一轮授权的模拟跑，方向也照样要说清"
    assert dry["request_shape"]["max_tokens"] == 8
    live, rc2, _ = _run(tmp_path, "--profile", "deepseek_thinking_disabled", "--level", "快速",
                        factory=lambda r: probe.FakeClient("quiet"))
    assert rc2 == probe.RC_VERIFIED and live["grant"] == GRANT2 and live["grant_calls"] == 3
    assert live["direction"] == "off"
    assert live["request_shape"]["max_tokens"] == probe.AUTHORIZED_GRANTS[GRANT2]["max_tokens"]


def test_the_third_call_rule_is_pre_registered_in_the_tool():
    """§R5：三次怎么花在跑之前就钉死在文件里，跑的人不许临场改。这一格锁"话在场"
    （实现不判它：判它要读上游结论，那不是命令行能定的），因为这一单的教训就是
    "临场决定第 3 次花在哪"等于没有预登记。"""
    doc = probe.__doc__ or ""
    for needle in ("第 1 次", "第 2 次", "第 3 次", "预登记", "A/B/A", "用完 3 格即停"):
        assert needle in doc, needle
    assert "不省" in doc, "两次都因异常没结论时第 3 次照发不省——这条也在预登记里"


def test_the_candidate_table_stops_claiming_a_flat_four_calls():
    """§R3 末尾：`thinking_profiles.py` 顶部 docstring 那句「本单授权 4 次」必须改口成
    授权轮次表的口径。留着它的后果看得见：下一个读代码的人会拿 4 去算"今天还剩几格"，
    而 4 那一轮（g1）已经用尽了。"""
    doc = thinking_profiles.__doc__ or ""
    assert "本单授权 4 次" not in doc
    assert "授权轮次" in doc, "要说清授权是分批的，并指向探针里那张表"


# ------- 11. 修复轮 2（C-1/F1）：闸收到「会不会写」上——读放行、写还咬 --------------
#
# 审查员 round 2 唯一的阻塞项：BASE 上 `--data-dir` 指主工作树那份现网 `data/` 会被
# 预检里针对 `--data-dir` 的那一次 `data_dir_belongs_to_another_checkout()` 直接 rc=2 拒掉，
# 而**读那一条 provider 记录是本工具唯一的用途**——三次授权一次都发不出去。
# F1 只删那一次调用，保留 `--ledger` 那一次；本节是"改窄之后牙还在"的全部证据：
#   §3.1 新的放行面（正面锁，BASE 上必红 = 阻塞项的证据）
#   §3.2 账本写进另一棵 checkout 还咬（变异 #7 就是让这一格红的那枚）
#   §3.3 账本写进 `--data-dir` 里面还咬
#   §3.4 隔离目录与 `--data-dir` 重叠还咬，且顺序判据不许改成事后判
#   §3.5 只读尺子在新的放行面上照样硬判（多一个路径 ⇒ 结论作废）
#   §3.6 `assert_store_quarantined` 吃的名单里 `data_dir` 还在
#   文案 F2：模块 docstring 那句"不许对另一个 checkout 运行"改完就是假话，必须改口径，
#        并且它引用的三个行号必须真的指着那三个函数。
# 全部**零出站**：客户端一律是注入的替身（`transport` 必须等于 `test_double`）或
# `--dry-run`（`fake_client`）。"另一棵 checkout"一律是 tmp_path 下现造的假树
# （`.git` 目录 + 合成 key + RFC 2606 的 `probe.invalid`），绝不把主工作树那份 `data/`
# 交给探针——那一棵是现网资产，本单只读它都不必。

def _fake_checkout(tmp_path: Path, *, provider_id=DEEPSEEK, key=SHAPEY) -> Path:
    """在仓库外造一棵"**别的 checkout**"：`.git` 目录 + 一份长得像现网的 `data/`。
    形状是真的（两个文件名、列表结构、`base_url`/`model` 齐），值是合成的。"""
    other = tmp_path / "other-checkout"
    (other / ".git").mkdir(parents=True, exist_ok=True)
    data_dir = other / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    record = {"id": provider_id, "label": "replica", "base_url": FAKE_HOST,
              "model": "probe-flash", "max_context_k": 64, "supports_vision": False}
    (data_dir / "providers.json").write_text(json.dumps([record]), encoding="utf-8")
    (data_dir / "provider_keys.json").write_text(json.dumps({provider_id: key}), encoding="utf-8")
    return data_dir


def _replica_argv(tmp_path: Path, data_dir, *extra: str, grant=GRANT2, ledger=None,
                  isolation=None, attempt="1") -> list:
    """指向那棵假树的 live 命令行。夹具 `_argv` 把 `--data-dir` 写死成它自己那份
    `live-data`、改不了，所以这一节直接给 argv（与 §9 那两格同样的做法）。
    账本与隔离目录默认落在 `tmp_path/iso`——**所有 checkout 之外**，父链上没有 `.git`，
    于是这一跑只剩"被测的那一条判据"在说话。"""
    argv = ["--data-dir", str(data_dir)]
    if grant:
        argv += ["--grant", grant]
    argv += ["--ledger", str(ledger or _ledger(tmp_path / "iso")),
             "--isolation-dir", str(isolation or tmp_path / "iso")]
    if "--dry-run" not in extra and attempt is not None:
        argv += ["--attempt", str(attempt)]
    return argv + list(extra)


def test_reading_the_live_record_in_another_checkout_is_allowed_and_booked(tmp_path):
    """§3.1 正面锁（**BASE 上这一格必红**）：`--data-dir` 指另一棵 checkout ⇒ 预检放行、
    记账成功、`transport == "test_double"`（注入替身，零出站），而那棵树逐字节没动。
    这就是审查员 C-1 说的那件事：不放开这一格，这三次调用一次都发不出去。"""
    data_dir = _fake_checkout(tmp_path)
    before = _tree_sha_listing(data_dir)
    ledger = _ledger(tmp_path / "iso")
    payload, rc, _ = probe.run_probe(probe.make_config(_replica_argv(
        tmp_path, data_dir, "--provider", DEEPSEEK,
        "--profile", "deepseek_thinking_disabled", "--level", "均衡")),
        client_factory=lambda r: probe.FakeClient("quiet"))
    assert rc == probe.RC_VERIFIED, payload["notes"]
    assert payload["transport"] == "test_double", "live 形状一律注入替身，本单零出站"
    assert payload["call_slot"] == 1 and len(probe.read_ledger(ledger)) == 1, "记账必须成功"
    assert payload["credential_fp"], "读不到那条记录 ⇒ 放行只是没炸，不是办成了用途"
    assert payload["verdict"] == "switch_off_observed" and payload["isolation_ok"] is True
    assert not any("checkout" in n for n in payload["notes"]), payload["notes"]
    assert _tree_sha_listing(data_dir) == before, "读这一趟不许动那棵树一个字节"
    # 负控：放行不是"什么都放行"——同一条命令行少了 `--grant` 仍然 rc=2、账本不涨。
    build, seen = _never()
    refused, rc2, _ = probe.run_probe(probe.make_config(_replica_argv(
        tmp_path, data_dir, "--provider", DEEPSEEK,
        "--profile", "deepseek_thinking_disabled", "--level", "均衡",
        grant=None)), client_factory=build)
    assert rc2 == probe.RC_BLOCKED and seen == [] and refused["call_slot"] is None
    assert len(probe.read_ledger(ledger)) == 1, "被拒的那一格不许在账上留痕"


def test_ledger_inside_another_checkout_is_refused_even_under_the_authorized_name(tmp_path):
    """§3.2 写面还咬（**变异 #7 让它红的那一格**）：账本落在另一棵 checkout 里 ⇒ rc=2、
    账本没长、零出站。这一格刻意把 basename 写成该轮授权的权威账本名，并把目录放在
    `--data-dir` 之外、仓库之外——三条 `--ledger` 落盘判据里只剩"属于另一棵 checkout"
    这一条还站着。把它删掉（F1 若顺手删了整条闸）这一跑就会走通、并把账本写进别人的树。"""
    data_dir = _fake_checkout(tmp_path)
    other = data_dir.parent
    before = _tree_sha_listing(other)
    ledger = other / probe.AUTHORIZED_GRANTS[GRANT2]["ledger_name"]
    build, seen = _never()
    payload, rc, _ = probe.run_probe(probe.make_config(_replica_argv(
        tmp_path, data_dir, "--provider", DEEPSEEK, "--profile", "none",
        "--ledger", str(ledger))), client_factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert payload["transport"] is None and payload["call_slot"] is None
    assert not ledger.exists(), "拒发那一支不许在别人的树里留下一个字节"
    assert _tree_sha_listing(other) == before
    assert any("checkout" in n and "--ledger" in n for n in payload["notes"]), payload["notes"]
    # 负控：同一跑把账本挪回所有 checkout 之外 ⇒ 放行（否则这条闸是恒红的装饰）
    ok, rc2, _ = probe.run_probe(probe.make_config(_replica_argv(
        tmp_path, data_dir, "--provider", DEEPSEEK, "--profile", "none")),
        client_factory=lambda r: probe.FakeClient("quiet"))
    assert rc2 == probe.RC_VERIFIED and ok["call_slot"] == 1


def test_ledger_inside_another_checkouts_read_only_data_dir_is_refused(tmp_path):
    """§3.3：账本落在**本次声明只读**的那棵 `--data-dir` 里面 ⇒ 预检就拒（`overlaps`），
    而且它排在 checkout 判据**之前**——所以这条不依赖 F1 留着的那一支，是独立的一格。"""
    data_dir = _fake_checkout(tmp_path)
    before = _tree_sha_listing(data_dir)
    ledger = data_dir / probe.AUTHORIZED_GRANTS[GRANT2]["ledger_name"]
    build, seen = _never()
    payload, rc, _ = probe.run_probe(probe.make_config(_replica_argv(
        tmp_path, data_dir, "--provider", DEEPSEEK, "--profile", "none",
        "--ledger", str(ledger))), client_factory=build)
    assert rc == probe.RC_BLOCKED and seen == [] and not ledger.exists()
    assert _tree_sha_listing(data_dir) == before
    assert any("--ledger" in n and "--data-dir" in n for n in payload["notes"]), payload["notes"]


def test_isolation_dir_overlapping_another_checkout_data_dir_is_refused_before_import(
        tmp_path, monkeypatch):
    """§3.4：放行面变大之后，"隔离目录不许开进我只读的这棵树"这条**顺序**判据还在。
    牙沿用既有的 spy 口径：`load_backend_modules` 一次都不许被调（import 本身就是那次
    写盘，把它改成事后判等于先把文件开进只读树、再宣布这里不许有文件）。"""
    data_dir = _fake_checkout(tmp_path)
    before = probe.files_digest(data_dir)
    monkeypatch.setenv("DEEPSEEK_API_KEY", SHAPEY)     # 播种燃料：没有它 `_seed_from_env` 返回空表
    imported = []

    def spy(isolation_dir):
        imported.append(isolation_dir)
        raise AssertionError("重叠时一次都不许走到 import app.core.providers")

    monkeypatch.setattr(probe, "load_backend_modules", spy)
    build, seen = _never()
    payload, rc, _ = probe.run_probe(probe.make_config(_replica_argv(
        tmp_path, data_dir, "--provider", DEEPSEEK, "--profile", "none", "--dry-run",
        "--isolation-dir", str(data_dir / "iso"))), client_factory=build)
    assert rc == probe.RC_BLOCKED and seen == []
    assert imported == [], "重叠判据必须在 import 之前——这一句不许改成事后判"
    assert not (data_dir / "iso").exists()
    assert probe.files_digest(data_dir) == before
    assert payload["isolation_ok"] is False


def test_a_new_path_in_the_another_checkout_tree_still_voids_the_conclusion(tmp_path):
    """§3.5：读被放行 ⇒ 那把只读尺子必须在**新的放行面**上照样硬判。替身被调用的那一刻
    往那棵树里多开一个路径 ⇒ rc=2、结论作废、点名是哪个路径。
    BASE 上这一格也红，但红的是另一件事：那一跑根本没走到尺子就被宽闸拒了（notes 里只有
    checkout 那一句）——旧的宽判据把这条真判据的射程整个遮住了。"""
    data_dir = _fake_checkout(tmp_path)

    def build(record):
        (data_dir / "seeded-by-nobody.json").write_text("{}", encoding="utf-8")
        return probe.FakeClient("quiet")

    payload, rc, _ = probe.run_probe(probe.make_config(_replica_argv(
        tmp_path, data_dir, "--provider", DEEPSEEK,
        "--profile", "deepseek_thinking_disabled", "--level", "均衡")), client_factory=build)
    assert rc == probe.RC_BLOCKED
    assert payload["verdict"] != "preflight_failed", "结论是跑完再作废的，不是没跑"
    assert any("只读承诺被打破" in n and "seeded-by-nobody.json" in n
               for n in payload["notes"]), payload["notes"]
    assert payload["call_slot"] == 1, "作废结论不退款：那一格已经先记在账上了（§R1 先记账后发）"


def test_the_quarantine_guard_still_eats_the_data_dir_it_just_read(tmp_path, monkeypatch):
    """§3.6：F1 明写"不许把 `data_dir` 从 `assert_store_quarantined` 的禁止名单里拿掉"。
    这一格两头都钉：① 调用点喂进去的名单里 `data_dir` 与 `REPO_ROOT` 都必须在场（拿
    spy 把实参抄下来，不看源码猜）；② 真把一个指着刚读的那棵树的单例喂进去 ⇒ rc=2、
    零出站、零记账。①缺了就等于"名单悄悄改窄而行为测试全绿"。"""
    data_dir = _fake_checkout(tmp_path)
    ledger = _ledger(tmp_path / "iso")
    captured = []
    real = probe.assert_store_quarantined

    def spy(providers_mod, forbidden_dirs):
        captured.append([probe._norm(str(p)) for p in forbidden_dirs])
        return real(providers_mod, forbidden_dirs)

    monkeypatch.setattr(probe, "assert_store_quarantined", spy)
    payload, rc, _ = probe.run_probe(probe.make_config(_replica_argv(
        tmp_path, data_dir, "--provider", DEEPSEEK, "--profile", "none")),
        client_factory=lambda r: probe.FakeClient("quiet"))
    assert rc == probe.RC_VERIFIED
    assert captured, "预检放行以后这一句一次都没被调用 = 名单锁成了空话"
    booked = len(probe.read_ledger(ledger))
    for names in captured:
        assert probe._norm(str(REPO_ROOT)) in names, names
        assert probe._norm(str(data_dir)) in names, \
            "把 data_dir 从禁止名单里拿掉了：单例可以指临时目录，绝不能指现网"
    # ② 反例：单例指进刚被读过的那棵树 ⇒ 隔离闸拒，且一次都没发、账本一行都不许多
    monkeypatch.setattr(providers, "store",
                        FakeStore(str(data_dir / "providers.json"),
                                  str(data_dir / "provider_keys.json"),
                                  str(data_dir / "provider_prefs.json")))
    build, seen = _never()
    refused, rc2, _ = probe.run_probe(probe.make_config(_replica_argv(
        tmp_path, data_dir, "--provider", DEEPSEEK, "--profile", "none")), client_factory=build)
    assert rc2 == probe.RC_BLOCKED and seen == []
    assert refused["isolation_ok"] is False and refused["transport"] is None
    assert len(probe.read_ledger(ledger)) == booked, "隔离没过关就不许多记一笔账"


def test_the_header_describes_the_write_gate_and_its_residual_risk():
    """F2「不许留假话」：闸改窄之后，模块 docstring 那句"本工具不许对另一个 checkout
    运行"就成了假话，必须删；同时三层读面保护与这一轮换来的残留风险必须点名在场。
    这一格只锁**话**，行为那一半在上面六格里。"""
    doc = probe.__doc__ or ""
    assert "本工具不许对另一个 checkout 运行" not in doc, "改完闸还留这句就是假话"
    assert "`--data-dir` 属于另一棵 checkout" not in doc, "退出码表里那条 rc=2 理由已经不存在了"
    for needle in ("「读现网那条 provider 记录」是用途", "「写进另一棵 checkout」是禁区",
                   "残留风险", "json.load", "allowed_shapes"):
        assert needle in doc, needle


def test_the_line_numbers_the_header_cites_still_point_at_those_functions():
    """F2 要求三层读面保护"含 `:行号`"点名。行号会漂，所以这一格把它做成会红的锁：
    docstring 里每出现一处 `` `名字`（:NNN） ``，那个函数在**当前这份文件**里的 def 行
    必须等于 NNN。挪了函数忘了改文档 ⇒ 红（比"引用不存在的行号"更响）。"""
    doc = probe.__doc__ or ""
    cited = re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`（:(\d+)）", doc)
    assert len(cited) >= 3, cited
    seen_names = set()
    for name, line in cited:
        fn = getattr(probe, name, None)
        assert fn is not None, f"docstring 引用了 {name}（:{line}），但模块里没有这个名字"
        start = inspect.getsourcelines(fn)[1]
        assert start == int(line), f"{name} 的 def 实际在第 {start} 行，docstring 写的是 :{line}"
        seen_names.add(name)
    assert {"read_provider_record", "assert_store_quarantined", "files_digest"} <= seen_names, \
        "三层读面保护必须逐层点名（brief §F2），少一层就是文案没跟上判据"


# ---- 12. Task 12A：生产登记表——机制先于条目（M1 记录作用域 + 出网参数唯一写者） ----
#
# 7 次真实调用之后没有任何配方够格，所以 `thinking_profiles.PROFILES` 今天仍然只有
# `none`（上面 :337-344 那格继续绿是**结论**，它变红就是有人偷偷登记）。这一节因此全部
# 靠 monkeypatch 的 synthetic 条目把两侧演出来：判据必须在"没有真条目"的现状下也已经
# 有牙，否则下一个人加条目时加的就是全局假话。零出站。

SYNTH_HOST = "api.example.invalid"
SYNTH_MODEL = "synth-flash"


def _synth_entry(**over):
    """一条 synthetic **生产**条目：只进 monkeypatch 的内存表，绝不进生产数据。

    形状就是登记契约（A1）：`label` / `verified` / `level_count` / `verified_for` /
    `kwargs`。缺任何一格都必须在下面某格里被判出来，不是靠运气。
    """
    entry = {
        "label": "合成配方",
        "verified": True,
        "level_count": 3,
        "verified_for": [{"host": SYNTH_HOST, "model": SYNTH_MODEL}],
        "kwargs": lambda level: {"synth_level": level},
    }
    entry.update(over)
    return entry


def test_production_table_shape_and_the_none_entry_declaration():
    """A1 的表形状：`none` 自己必须声明"没有档位、没有核实射程"，且 `kwargs` 给空字典。

    `level_count=None` 与 `verified_for=None` 不是偷懒：M2 的档位判据靠前者跳过 `none`，
    M1 的记录判据靠后者把 `none` 挡在 verified 之外（它不发参数，本就不该亮）。
    """
    assert thinking_profiles.DEFAULT_PROFILE == "none"
    assert set(thinking_profiles.PROFILES) == {"none"}
    entry = thinking_profiles.PROFILES["none"]
    assert entry["verified"] is True
    assert entry["level_count"] is None
    assert entry["verified_for"] is None
    assert entry["kwargs"](1) == {}


def test_unknown_profile_is_refused_before_anything_else():
    """① 表里没有的名字（含候选表那五支）一律 ValueError——候选表不是生产白名单。"""
    for name in ("openai_reasoning_effort", "deepseek_thinking_type", "dashscope_enable_thinking",
                 "nope", "", None):
        with pytest.raises(ValueError, match="未知的思考参数配置"):
            thinking_profiles.build_thinking_kwargs(name, 1)


def test_unverified_profile_is_refused_and_that_gate_can_go_green(monkeypatch):
    """② 表里有但 `verified` 不为真 ⇒ "思考参数未核实"；同一形状翻成 verified 必须放行。

    两侧都跑：只演拒的一面，判据写成"永远 raise"也能绿，那条函数就成了一段散文。
    """
    monkeypatch.setitem(thinking_profiles.PROFILES, "synth_unverified",
                        _synth_entry(verified=False))
    monkeypatch.setitem(thinking_profiles.PROFILES, "synth_verified", _synth_entry())
    with pytest.raises(ValueError, match="思考参数未核实"):
        thinking_profiles.build_thinking_kwargs("synth_unverified", 1)
    # 判序：越界的档位不许抢在"未核实"前面报错
    with pytest.raises(ValueError, match="思考参数未核实"):
        thinking_profiles.build_thinking_kwargs("synth_unverified", 99)
    assert thinking_profiles.build_thinking_kwargs("synth_verified", 1) == {"synth_level": 1}


def test_none_returns_empty_dict_after_the_verified_gate(monkeypatch):
    """③ `none` 返回 `{}`，档位 irrelevant——它没有档位，所以越界也轮不到它报错。"""
    assert thinking_profiles.build_thinking_kwargs("none", 1) == {}
    assert thinking_profiles.build_thinking_kwargs("none", 99) == {}
    assert thinking_profiles.build_thinking_kwargs("none", None) == {}
    # 反例：把 none 自己改成未核实，必须回落到"未核实"而不是"返回空字典"
    monkeypatch.setitem(thinking_profiles.PROFILES, "none",
                        {"label": "不发思考参数", "verified": False,
                         "level_count": None, "verified_for": None,
                         "kwargs": lambda level: {}})
    with pytest.raises(ValueError, match="思考参数未核实"):
        thinking_profiles.build_thinking_kwargs("none", 1)


def test_level_outside_the_declared_wire_values_is_refused_on_both_sides(monkeypatch):
    """④ 档位只在 `1..level_count` 里活着：三档配方拒 0/4，布尔配方拒 2/3 且第 1 档必须过。

    两侧都跑（防恒真）：布尔条目（g2 实测"三档同一个出网值"那一形）第 1 档放行、
    第 2 档拒绝；三档条目 1..3 放行、4 拒绝。
    """
    monkeypatch.setitem(thinking_profiles.PROFILES, "synth_three", _synth_entry(level_count=3))
    monkeypatch.setitem(thinking_profiles.PROFILES, "synth_bool", _synth_entry(level_count=1))
    for level in (1, 2, 3):
        assert thinking_profiles.build_thinking_kwargs("synth_three", level) == {"synth_level": level}
    for level in (4, 0, -1, None, "", "快速"):
        with pytest.raises(ValueError, match="该模型不支持这个思考强度"):
            thinking_profiles.build_thinking_kwargs("synth_three", level)
    assert thinking_profiles.build_thinking_kwargs("synth_bool", 1) == {"synth_level": 1}
    for level in (2, 3):
        with pytest.raises(ValueError, match="该模型不支持这个思考强度"):
            thinking_profiles.build_thinking_kwargs("synth_bool", level)


def test_build_thinking_kwargs_hands_back_a_fresh_dict_each_call():
    """出网参数是每次调用自己的一份：改返回值不许污染下一次（表是全局事实来源）。"""
    first = thinking_profiles.build_thinking_kwargs("none", 1)
    assert first == {} and first is not thinking_profiles.PROFILES["none"]["kwargs"](1)
    first["reasoning_effort"] = "minimal"
    assert thinking_profiles.build_thinking_kwargs("none", 1) == {}


def test_returned_thinking_kwargs_are_a_deep_copy_of_the_table(monkeypatch):
    """F1（审查员 I-R1）：docstring 承诺"返回值每次都是新字典……调用方改它不该把登记表
    改脏"，但 `dict(entry[kwargs(level)])` 只拷最外层——真形状全是嵌套的
    （`{"extra_body": {"thinking": {...}}}`），改一层内格就把表改脏到进程结束。
    同文件给候选表用的 `probe_kwargs` 早已是 `copy.deepcopy`，两处口径必须一致。

    用 synthetic 条目（只进 monkeypatch 的内存表）演嵌套形状：拿返回值改一层 → 再取一次
    → 第二次与第一次逐字节相同，且表里那一格没动。12B 接线前这条是常驻锁。
    """
    shared = {"extra_body": {"thinking": {"type": "enabled"}}}
    monkeypatch.setitem(thinking_profiles.PROFILES, "synth_nested",
                        _synth_entry(level_count=1, kwargs=lambda level: shared))
    first = thinking_profiles.build_thinking_kwargs("synth_nested", 1)
    assert json.dumps(first, sort_keys=True) == json.dumps(shared, sort_keys=True)
    first["extra_body"]["thinking"]["type"] = "被改脏了"
    first["extra_body"]["enable_thinking"] = "不是布尔"
    second = thinking_profiles.build_thinking_kwargs("synth_nested", 1)
    expected = {"extra_body": {"thinking": {"type": "enabled"}}}
    assert json.dumps(second, sort_keys=True) == json.dumps(expected, sort_keys=True), \
        "登记表被调用方的改动污染了：docstring 的承诺兑现不了"
    assert shared == expected, "表里那一格被改了"


def test_production_whitelist_is_the_same_object_as_the_registration_table(monkeypatch):
    """A2：`providers.THINKING_PROFILES` 必须是 `thinking_profiles.PROFILES` 的**别名**。

    这不是风格问题：既有测试是**就地** `monkeypatch.setitem` 往 `providers.THINKING_PROFILES`
    里塞 synthetic 条目（`test_v0391_provider_capabilities.py:318/:349/:351`）。抄一份的
    话测试改的那份没人读、判据读的那份没人改，那几条锁当场失牙变成恒绿——这一格就是
    专门来抓这件事的正面锁。
    """
    assert providers.THINKING_PROFILES is thinking_profiles.PROFILES
    assert providers.DEFAULT_THINKING_PROFILE == thinking_profiles.DEFAULT_PROFILE
    monkeypatch.setitem(providers.THINKING_PROFILES, "alias_probe", _synth_entry(verified=False))
    assert "alias_probe" in thinking_profiles.PROFILES
    assert thinking_profiles.PROFILES["alias_probe"] is providers.THINKING_PROFILES["alias_probe"]


def test_thinking_status_verified_needs_the_record_host_and_model(monkeypatch):
    """M1 的四条件：supports ∧ profile≠default ∧ 表里 verified ∧ host+model 命中 verified_for。

    少任何一个条件都不许亮 verified，而且**不许**演成 unsupported——"这条记录没被核实到"
    与"这个模型不会思考"是两件事。`verified_for` 比的是 hostname（不是整条 URL）：
    换 scheme/path 不该让核实作废，换 host 或换 model 才该。
    """
    monkeypatch.setitem(providers.THINKING_PROFILES, "synth_verified", _synth_entry())
    hit = providers._thinking_status(True, "synth_verified", SYNTH_HOST, SYNTH_MODEL)
    assert hit == "verified"

    # ④ 缺参就降级：忘记传参的那一方必须输得安全，不能输成"亮"
    for host, model in ((None, None), (None, SYNTH_MODEL), (SYNTH_HOST, None), ("", ""),
                        ("", SYNTH_MODEL), (SYNTH_HOST, "")):
        got = providers._thinking_status(True, "synth_verified", host, model)
        assert got == "unverified", (host, model, got)

    # 换 host / 换 model 各一跑：同 model 换 host、同 host 换 model 都不算命中
    assert providers._thinking_status(True, "synth_verified",
                                      "other.example.invalid", SYNTH_MODEL) == "unverified"
    assert providers._thinking_status(True, "synth_verified",
                                      SYNTH_HOST, "other-model") == "unverified"

    # host 小写比对（urlparse().hostname 本就小写，登记表里手写大小写混着不该作废）
    assert providers._thinking_status(True, "synth_verified", SYNTH_HOST.upper(),
                                      SYNTH_MODEL) == "verified"

    # 清单里多项：命中任意一项即算核实
    monkeypatch.setitem(providers.THINKING_PROFILES, "synth_multi",
                        _synth_entry(verified_for=[{"host": "a.example.invalid", "model": "m"},
                                                   {"host": SYNTH_HOST, "model": SYNTH_MODEL}]))
    assert providers._thinking_status(True, "synth_multi", SYNTH_HOST, SYNTH_MODEL) == "verified"

    # supports=False 压过一切：全命中也是 unsupported
    assert providers._thinking_status(False, "synth_verified",
                                      SYNTH_HOST, SYNTH_MODEL) == "unsupported"
    # 默认配方永远不亮（它不发参数，本来就不该 verified）
    assert providers._thinking_status(True, "none", SYNTH_HOST, SYNTH_MODEL) == "unverified"


def test_empty_or_missing_verified_for_never_means_unrestricted(monkeypatch):
    """负控常驻版：`verified_for` 填 `[]` 或缺这一格 ⇒ 一律 unverified，不是"不限"。

    "空清单 = 全部放行"是这一格最自然的误读，而它正好把 M1 想挡的那句全局假话
    原样放出去：条目一登记，所有填了该配方的记录一起亮。
    """
    monkeypatch.setitem(providers.THINKING_PROFILES, "synth_empty_scope",
                        _synth_entry(verified_for=[]))
    monkeypatch.setitem(providers.THINKING_PROFILES, "synth_no_scope",
                        _synth_entry(verified_for=None))
    monkeypatch.setitem(providers.THINKING_PROFILES, "synth_bare",
                        {"label": "登记时忘了写 verified_for", "verified": True})
    for name in ("synth_empty_scope", "synth_no_scope", "synth_bare"):
        got = providers._thinking_status(True, name, SYNTH_HOST, SYNTH_MODEL)
        assert got == "unverified", (name, got)
