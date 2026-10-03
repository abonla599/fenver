"""v0.25 R4b 最小经济内核契约：积分算得出、看得见、拦得住（默认关）——但没有钱包。

这批判据钉的是门禁 W4 的那块板：实况里唯一在跑的 DeepSeek-Flash 在 1.0 之前
0.00x 免费、新用户默认 0 分——两条叠起来，额度体系在默认模型上**空转**，现在
拦人的只剩 auth_router 那个进程内 60s/20 次频控（重启清零、按次不按消耗）。
所以本版的核心价值不是"扣积分"（本批一分钱都不扣、没有余额、没有账本），而是
**影子积分**：每次调用都按真实单价算出"本应消耗多少"，免费期照常算（这是运营方
垫资额的唯一来源），并让这个数字能拦人——`credit_shadow_daily_limit` 打开后，
到线停在「等待你确认是否继续」且不出网。

四条不许退让的口径（卡片 §2/§3、PRD D11–D25 的实现侧落点）：
1. **三态**：`0.00x` 是明确配了免费价；`?x` 是没配价。未定价**算不出来就说算不
   出来**，任何路径都不许把它当成 0 记（D14，验收 14）。
2. **倍率不是合同价**（D13）：倍率=混合单价÷基准混合单价，按写死的典型 4:1 折算，
   只进展示；结算/影子一律真实 token × 真实单价。两者不许互相代入——本文件用
   "输入便宜、输出极贵"的模型把偏差方向钉死。
3. **基准价是单一真相**（D25 Q-b）：固定基准价住在 config_store.DEFAULTS，
   换默认模型不许让任何倍率漂移——"默认模型=1.0x"那种会随运营动作变形的定义
   从根上不许出现。
4. **影子护栏复用 R3b 的轮次边界单一钩子 `before_round()`**（拆解清单并行安全
   边界 7）：检查点在 streaming.py、政策在 main.py 的 _produce_stream——
   "影子是否越线"是它的**一个判据**，与"有没有活读者"并列；不许在轮首挂两个
   互不知情的独立闸。同时请求入口（第一个付费轮之前）做一次前置判定。

反向变异自证（实现后各跑一次，记录红）：
- 把 credits.credits_for_usage 里"未定价返回 None"改成返回 0.0 →
  test_未定价的条目算不出积分_绝不悄悄当成零 与
  test_管理端影子合计把算不出的如实说成算不出 全红。
- 把 before_round 的 shadow 判据摘掉（只留读者检查）→
  test_护栏打开时第二个付费轮在轮次边界被拦_读者在场也拦 红——create() 真的
  又飞了一轮，账本多一行。
"""
import importlib
import json
import time
import types

import pytest
from fastapi.testclient import TestClient

from app.core import audit, config_store, credits, stream_runs, usage
from app.core.authz import Principal
from app.core.providers import store as provider_store
from app.main import app, launch_stream_run

BOOT = {"Authorization": "Bearer boot-token"}
USER = "default_user"


# ---------- 测试替身：形状对齐 openai SDK（与 R3b/R3 同一套） ----------

def _chunk(content=None, tool_calls=None):
    delta = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=delta, finish_reason=None)],
        usage=None)


def _usage_chunk(prompt=5, completion=7):
    return types.SimpleNamespace(
        choices=[],
        usage=types.SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=completion,
            total_tokens=prompt + completion,
            completion_tokens_details=None, prompt_tokens_details=None))


def _tool_call_chunk(call_id="call_1", name="calculator", arguments='{"expression": "1+1"}'):
    fn = types.SimpleNamespace(name=name, arguments=arguments)
    return _chunk(tool_calls=[types.SimpleNamespace(index=0, id=call_id, function=fn)])


class FakeCompletions:
    """每次 create() 消费一个预排的"轮"；弹药没了还来 = 不该发生的付费调用，当场炸。"""

    def __init__(self, rounds):
        self._rounds = list(rounds)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._rounds:
            raise AssertionError("不该发生的付费轮次发生了：护栏到线/已取消之后仍发起了 create()")
        item = self._rounds.pop(0)
        return iter(item) if isinstance(item, list) else item


class FakeClient:
    def __init__(self, rounds):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(rounds))


# ---------- 夹具 ----------

@pytest.fixture(autouse=True)
def _clean_runs():
    stream_runs.clear_all_for_tests()
    yield
    stream_runs.clear_all_for_tests()


@pytest.fixture
def clean_usage(tmp_path, monkeypatch):
    prev_days, prev_path = usage._days, usage._PATH
    usage._days = {}
    usage._PATH = str(tmp_path / "usage.json")
    try:
        yield usage._PATH
    finally:
        usage._days, usage._PATH = prev_days, prev_path


@pytest.fixture
def real_stream(monkeypatch):
    """真 stream_chat，上游换成 FakeClient（reload 模式同 R3b）。"""
    import app.core.streaming as streaming
    importlib.reload(streaming)
    holder = {}
    monkeypatch.setattr(streaming, "build_client", lambda provider: holder["client"])

    def install(rounds):
        client_ = FakeClient(rounds)
        holder["client"] = client_
        return client_.chat.completions

    yield install
    importlib.reload(streaming)


@pytest.fixture
def grace_env(monkeypatch):
    def set_grace(seconds):
        monkeypatch.setenv("STREAM_NO_READER_GRACE_SECONDS", str(seconds))
    return set_grace


@pytest.fixture
def shadow_limit_env(monkeypatch):
    """影子护栏的 env 通道（每次现读，与宽限期同一口径）。0/不设 = 不拦截。"""
    def set_limit(value):
        if value is None:
            monkeypatch.delenv("CREDIT_SHADOW_DAILY_LIMIT", raising=False)
        else:
            monkeypatch.setenv("CREDIT_SHADOW_DAILY_LIMIT", str(value))
    return set_limit


@pytest.fixture
def config_memory(tmp_path, monkeypatch):
    """把 config_store 的内存态借出来改，用完原样还——测基准价的配置通道用。"""
    prev = dict(config_store._config)
    monkeypatch.setenv("CONFIG_DB_PATH", str(tmp_path / "config.json"))
    try:
        yield config_store
    finally:
        with config_store._lock:
            config_store._config.clear()
            config_store._config.update(prev)


@pytest.fixture
def priced_providers():
    """四条定价形态各一条：正常(token)、免费期(0.00x 但配了真实价)、per_call、未定价
    （fake-model 本就是未定价）。用例后删干净，不污染进程级 store。"""
    created = []

    def make(pid, pricing, **over):
        rec = {"id": pid, "label": pid, "base_url": "https://pricing.invalid/v1",
               "api_key": "sk-pricing-test-000111222",  # secret-scan:allow 测试里造的假密钥
               "model": "fake-chat", "supports_vision": False, "is_default": False,
               "paid_by": "operator"}
        if pricing is not None:
            rec["pricing"] = pricing
        rec.update(over)
        provider_store.upsert(rec)
        created.append(pid)
        return provider_store.get(pid)

    yield make
    for pid in created:
        provider_store.delete(pid)


# ---------- 工具 ----------

def _tool_pipe():
    return types.SimpleNamespace(
        tools_schema=[{"type": "function",
                       "function": {"name": "calculator", "description": "",
                                    "parameters": {"type": "object"}}}],
        save_interaction=lambda *a: None)


def _today_row(user_id, provider_id, *, prompt=0, completion=0, day=None):
    usage.record_call(user_id=user_id, provider_id=provider_id, paid_by="operator",
                      prompt_tokens=prompt, completion_tokens=completion,
                      total_tokens=prompt + completion, day=day)


def _frames(body: str):
    out = []
    for block in body.split("\n\n"):
        data = None
        for line in block.split("\n"):
            if line.startswith("data: "):
                try:
                    data = json.loads(line[6:])
                except ValueError:
                    data = None
        if data is not None:
            out.append(data)
    return out


# ===========================================================================
# T5.3 单价配置面：三态、校验、写权限、审计
# ===========================================================================

def test_parse_pricing_keeps_the_full_deployment_surface():
    """单价配置面一次收齐：mode/currency/两档单价/核对日期/免费截止——少一维将来都得改结构。"""
    p = credits.parse_pricing({
        "mode": "token", "currency": "CNY",
        "input_per_m": 1.0, "output_per_m": 4.0,
        "price_checked_on": "2026-10-01", "free_until": "2026-12-31"})
    assert p["mode"] == "token" and p["currency"] == "CNY"
    assert p["input_per_m"] == 1.0 and p["output_per_m"] == 4.0
    assert p["price_checked_on"] == "2026-10-01"
    assert p["free_until"] == "2026-12-31"


def test_未配置单价就是未定价_代码里不许有任何默认价格悄悄生效(priced_providers):
    """T5.3 的「绝不用猜的价填进去」：没配 pricing 的条目读回来必须是 None（三态之一），
    而不是某个内置示例价——示例价表只进文档供人抄，不进代码当默认。"""
    saved = priced_providers("unpriced-model", None)
    assert saved.get("pricing") is None
    assert credits.parse_pricing(None) is None and credits.parse_pricing({}) is None
    # 任何条目都不许因为"没配"而拿到一个非 None 的价格
    assert credits.pricing_of("unpriced-model") is None


def test_单价形状不对就说人话_不许半套价格混进账(priced_providers):
    """token 模式缺任一档单价 = 半套价格：那比没配更危险（会被当成"配过了"参与计算）。
    判据：ProviderError 一句人话点名缺哪格，条目不落盘。"""
    from app.core.providers import ProviderError
    with pytest.raises(ProviderError) as exc:
        priced_providers("half-pricing", {"mode": "token", "currency": "CNY",
                                          "input_per_m": 1.0})
    assert "输出" in str(exc.value), f"要说清缺哪一档，不能只甩'格式错误'：{exc.value}"
    assert provider_store.get("half-pricing") is None


def test_负单价与非法模式当场拒绝():
    """单价是钱：负数不是"便宜"，是账算反；mode 收野值等于把三态变成无态。"""
    with pytest.raises(credits.PricingError):
        credits.parse_pricing({"mode": "token", "input_per_m": -1.0, "output_per_m": 2.0})
    with pytest.raises(credits.PricingError):
        credits.parse_pricing({"mode": "per_second", "input_per_m": 1.0, "output_per_m": 2.0})


def test_per_call是合法预留值_但结算路径必须显式拒绝而不是偷按token公式():
    """D24/验收 18：mode=per_call 本版只预留结构——结算路径遇到它必须抛人话异常。
    静默按 token 公式算会算出无意义的数（还极易显示成 0.00x，正掉进"免费/没价"混淆坑）。"""
    p = credits.parse_pricing({"mode": "per_call", "currency": "CNY"})
    assert p is not None and p["mode"] == "per_call"
    with pytest.raises(credits.PricingModeError) as exc:
        credits.credits_for_usage(p, prompt_tokens=1000, completion_tokens=50)
    msg = str(exc.value)
    assert "按次" in msg and "token" in msg.lower(), f"人话要点名『按次未实现、没偷套公式』：{msg}"
    # 展示面也不许给它编一个倍率（§2.6：倍率公式的前提是 token 计价）
    assert credits.multiplier_label(p) is None


def test_共享条目改单价是管理员的门_普通用户两条路都进不去(enforced_available):
    """T5.3 写权限 + 「本版三处错一次就出血」之一：providers.json 含密钥，单价写权与它同文件同级。
    普通用户改共享价 = 全站成本说改就改；管理员面碰私有条目 = 不存在（同 404 不泄露归属）。"""
    admin_headers, user_headers, private_id, _ = enforced_available
    client = TestClient(app)
    shared = client.post("/v1/providers", headers=admin_headers, json={
        "id": "shared-priced", "label": "共享", "base_url": "https://s.invalid/v1",
        "api_key": "sk-shared-000111222",  # secret-scan:allow 测试里造的假密钥
        "model": "m", "pricing": {"mode": "token", "currency": "CNY",
                                  "input_per_m": 1.0, "output_per_m": 4.0}})
    assert shared.status_code == 200, shared.text
    try:
        # 普通用户走管理员面：403（角色不够）；走个人面碰共享：404（不是他的条目）
        res = client.put("/v1/providers/shared-priced", headers=user_headers, json={
            "label": "共享", "base_url": "https://s.invalid/v1", "model": "m",
            "pricing": {"mode": "token", "currency": "CNY",
                        "input_per_m": 0.0, "output_per_m": 0.0}})
        assert res.status_code == 403, f"普通用户改得动共享单价：{res.status_code}"
        res = client.put("/v1/me/providers/shared-priced", headers=user_headers, json={
            "label": "共享", "base_url": "https://s.invalid/v1", "model": "m"})
        assert res.status_code == 404, "别人的共享条目在个人面必须是『不存在』"
        # 管理员也休想碰用户的私有条目（与 R1 的防枚举纪律同一形状）
        res = client.put("/v1/providers/%s" % private_id, headers=admin_headers, json={
            "label": "私改", "base_url": "https://p.invalid/v1", "model": "m"})
        assert res.status_code == 404, "私有条目对管理员面也是不存在——不泄露存在性"
    finally:
        provider_store.delete("shared-priced")


@pytest.fixture
def enforced_available(monkeypatch, tmp_path):
    """enforced 模式的管理员 + 两个普通用户；返回 (admin头, user头, 用户私有条目id)。
    第二个用户与私有条目用于『改价必须落审计』——跨用户那一面在那里各测一次。"""
    from app.core import authz
    from app.core.auth import AuthStore
    store = AuthStore(path=str(tmp_path / "users.json"))
    monkeypatch.setattr(authz, "auth_store", store)
    monkeypatch.setenv("AUTH_MODE", "enforced")
    monkeypatch.setenv("ACCESS_TOKEN", "boot-token")
    _, tok = store.register(username="属主甲", password="correct-horse-battery")
    user_headers = {"Authorization": "Bearer " + tok}
    _, tok2 = store.register(username="路人乙", password="correct-horse-battery2")
    other_headers = {"Authorization": "Bearer " + tok2}
    client = TestClient(app)
    saved = client.post("/v1/me/providers", headers=user_headers, json={
        "id": "mine-priced", "label": "我的", "base_url": "https://m.invalid/v1",
        "api_key": "sk-mine-000111222",  # secret-scan:allow 测试里造的假密钥
        "model": "m", "pricing": {"mode": "token", "currency": "CNY",
                                  "input_per_m": 2.0, "output_per_m": 8.0}})
    assert saved.status_code == 200, saved.text
    try:
        yield BOOT, user_headers, "mine-priced", other_headers
    finally:
        provider_store.delete("mine-priced")


def test_私有条目的属主可以改自己的单价_别人连条目都不存在(enforced_available):
    """属主改自己条目的价 = 自己的 key 自己的钱，允许；路人乙 PUT 属主甲的条目 = 404。"""
    _, user_headers, private_id, other_headers = enforced_available
    client = TestClient(app)
    res = client.put("/v1/me/providers/%s" % private_id, headers=other_headers, json={
        "label": "抢改", "base_url": "https://m.invalid/v1", "model": "m"})
    assert res.status_code == 404, "别人的私有条目必须是『不存在』，不是 403"
    res = client.put("/v1/me/providers/%s" % private_id, headers=user_headers, json={
        "label": "我的", "base_url": "https://m.invalid/v1", "model": "m",
        "pricing": {"mode": "token", "currency": "CNY",
                    "input_per_m": 3.0, "output_per_m": 9.0}})
    assert res.status_code == 200, res.text
    assert provider_store.get(private_id)["pricing"]["input_per_m"] == 3.0


def test_改单价必须落审计_谁改的改前改后与差值一样不少(enforced_available):
    """audit.jsonl 里要能回答：谁、把哪格、从多少改到多少。改前改后与 actor 少一样，
    事后『成本怎么突然变了』就没有可辩护的答案（这是单价写权限被列为出血点的原因）。"""
    _, user_headers, private_id, _ = enforced_available
    client = TestClient(app)
    before_audit = len(audit.read(limit=1000))
    res = client.put("/v1/me/providers/%s" % private_id, headers=user_headers, json={
        "label": "我的", "base_url": "https://m.invalid/v1", "model": "m",
        "pricing": {"mode": "token", "currency": "CNY",
                    "input_per_m": 5.0, "output_per_m": 20.0}})
    assert res.status_code == 200, res.text
    entries = audit.read(limit=1000)[before_audit:]
    hit = [e for e in entries if e["action"] == "provider.pricing.update"]
    assert len(hit) == 1, f"改价必须恰好落一条 pricing.update：{[e['action'] for e in entries]}"
    rec = hit[0]
    assert rec["actor_name"] == "属主甲", rec
    assert rec["before"]["pricing"]["input_per_m"] == 2.0, rec   # 改前
    assert rec["after"]["pricing"]["input_per_m"] == 5.0, rec    # 改后
    assert "3.0" in rec["detail"] or "5.0" in rec["detail"], f"detail 要写出差值：{rec}"


def test_单价没出现在请求里_不等于要清掉它_老表单编辑不许洗价(enforced_available):
    """管理员面/个人面的旧表单（ProviderRequest）不认识 pricing 字段：一次普通的
    改名保存如果把价洗成 None，那笔调用就从『有价』偷渡成『未定价』。
    判据：请求里不带 pricing 键 = 保留原值；显式 pricing:{} 才算主动取消定价。"""
    admin_headers, user_headers, private_id, _ = enforced_available
    client = TestClient(app)
    res = client.put("/v1/me/providers/%s" % private_id, headers=user_headers, json={
        "label": "只改个名", "base_url": "https://m.invalid/v1", "model": "m"})
    assert res.status_code == 200, res.text
    assert provider_store.get(private_id)["pricing"] is not None, \
        "没带 pricing 的 PUT 把单价洗掉了"
    res = client.put("/v1/me/providers/%s" % private_id, headers=user_headers, json={
        "label": "只改个名", "base_url": "https://m.invalid/v1", "model": "m",
        "pricing": {}})
    assert res.status_code == 200, res.text
    assert provider_store.get(private_id)["pricing"] is None, "显式空对象 = 主动取消定价"


# ===========================================================================
# T5.8 基准价与倍率：单一真相、一处常量、可配可调
# ===========================================================================

def test_倍率后缀是全产品一处常量():
    """卡片 §2.3b 的格式约定：后缀只有一个符号、住在一个常量里（半角 x）。
    两端各写一次是漂移之母。基准价自己必须是 1.00x——两位小数 + 后缀的格式形状。"""
    assert credits.MULTIPLIER_SUFFIX == "x"
    label = credits.multiplier_label(credits.parse_pricing(
        {"mode": "token", "currency": "CNY", "input_per_m": 1.0, "output_per_m": 4.0}))
    assert label == "1.00x", f"两位小数+后缀 x 的格式：{label}"


def test_基准价住在config_store的DEFAULTS里_是唯一真相():
    """示例口径 输入 ¥1/百万 + 输出 ¥4/百万 = 1.00x，部署者可改。
    这一条钉的是『别处不许再抄一份基准』——credits 必须从 config_store 读。"""
    bench = config_store.DEFAULTS["credit_benchmark"]
    assert bench["input_per_m"] == 1.0 and bench["output_per_m"] == 4.0
    assert bench["tokens_per_credit"] == 1000, "1 积分 = 基准价跑 1000 个混合 token"
    assert bench["mixed_input_output_ratio"] == 4.0, "展示折算的典型输入:输出 = 4:1"
    assert credits.benchmark()["currency"] == bench["currency"]


def test_改基准价配置_倍率与积分一起跟着变_证明没有第二真相(config_memory):
    """基准是 ¥→积分的锚：锚必须只有一个。改了 config 生效 = 读的是同一份。"""
    config_store.apply_update({"credit_benchmark": {
        "currency": "CNY", "input_per_m": 2.0, "output_per_m": 2.0,
        "mixed_input_output_ratio": 4.0, "tokens_per_credit": 1000}})
    assert credits.benchmark()["input_per_m"] == 2.0
    p = credits.parse_pricing({"mode": "token", "currency": "CNY",
                               "input_per_m": 2.0, "output_per_m": 2.0})
    assert credits.multiplier_label(p) == "1.00x", "新的基准自身恒 1.00x"


def test_换默认模型不许让任何倍率漂移_默认模型等于1x的定义是禁止的(priced_providers):
    """Q-b 拍板『固定基准价』的理由：以默认模型为 1.0x，则管理员点一颗 ★
    全站倍率集体重排——倍率要能当长期记忆用。判据：给不同 provider 互换
    is_default，任何 multiplier 一字不动。"""
    a = priced_providers("mult-a", {"mode": "token", "currency": "CNY",
                                    "input_per_m": 1.0, "output_per_m": 4.0})
    b = priced_providers("mult-b", {"mode": "token", "currency": "CNY",
                                    "input_per_m": 0.5, "output_per_m": 2.0})
    label_a = credits.multiplier_label(a["pricing"])
    label_b = credits.multiplier_label(b["pricing"])
    assert (label_a, label_b) == ("1.00x", "0.50x")
    provider_store.set_default("mult-b")
    try:
        assert credits.multiplier_label(provider_store.get("mult-a")["pricing"]) == label_a
        assert credits.multiplier_label(provider_store.get("mult-b")["pricing"]) == label_b
        # 展示面（/v1/models）也不许偷偷按"谁是默认"折算
        client = TestClient(app)
        models = {m["id"]: m for m in client.get("/v1/models").json()["models"]}
        assert models["mult-a"]["multiplier"] == label_a
        assert models["mult-b"]["multiplier"] == label_b
    finally:
        provider_store.set_default("fake-model")


def test_三态显示在数据层就分家_000x与问号和零是三种东西(priced_providers):
    """D14 的根：`0.00x`（明确免费）、`?x`（未定价）、真实倍率是三件事。
    参考图把前两个混成 0.00x 是这次要修的坑——所以未定价必须 `?x`，
    且 0.00x 必须带 free_until（有期限的显式免费，D22）。"""
    free = priced_providers("free-tri", {"mode": "token", "currency": "CNY",
                                         "input_per_m": 1.0, "output_per_m": 4.0,
                                         "free_until": "2099-12-31"})
    zero = priced_providers("zero-tri", {"mode": "token", "currency": "CNY",
                                         "input_per_m": 0.0, "output_per_m": 0.0})
    assert credits.multiplier_label(free["pricing"]) == "0.00x", "免费期显示显式免费"
    assert credits.is_free_on(free["pricing"]) is True
    assert credits.multiplier_label(zero["pricing"]) == "0.00x"
    assert credits.is_free_on(zero["pricing"]) is False, "零价永久免费 ≠ 有期限免费"
    assert credits.multiplier_label(None) == "?x", "未定价绝不显示 0.00x"
    # 免费期过后自动回到真实倍率——日期驱动，不靠人记得改
    assert credits.multiplier_label(free["pricing"], day="2100-01-01") == "1.00x"


# ===========================================================================
# T5.9 结算与展示分工（D13）：倍率不是合同价
# ===========================================================================

def test_一千个基准混合token恰好等于一积分():
    """换算的锚：1 积分 = 基准价（in1/out4，4:1 混合）跑 1000 个混合 token。
    800 输入 + 200 输出在基准价上 = ¥0.0016 = 基准混合价 = 恰好 1 积分。"""
    bench = credits.parse_pricing({"mode": "token", "currency": "CNY",
                                   "input_per_m": 1.0, "output_per_m": 4.0})
    assert credits.credits_for_usage(bench, prompt_tokens=800, completion_tokens=200) == 1.0


def test_内部保留六位小数_手算核对不许漂():
    """in=0.333/out=4、1 个输入 token：0.333e-6 ÷ 1.6 × 1000 = 0.000208125 → 6 位 = 0.000208。
    钉的是『真算、真舍』，不是浮点噪声里蒙对。"""
    p = credits.parse_pricing({"mode": "token", "currency": "CNY",
                               "input_per_m": 0.333, "output_per_m": 4.0})
    got = credits.credits_for_usage(p, prompt_tokens=1, completion_tokens=0)
    assert got == 0.000208, got


def test_偏差方向钉死_倍率当合同价会在输入重的调用上高估一个数量级():
    """本文件最重要的一条（卡片验收 2）：in=0.5/out=100 的模型按 4:1 折算
    倍率 12.75x。一条输入极重的真实调用（1M 输入 + 1 万输出）：
    按倍率『读』出 12877.5 积分；按真实单价结算只有 937.5。方向必须是
    输入重 → 真值远低于倍率暗示值；再反构一条输出重的（1 万输入 + 1M 输出），
    真值 62503.125 远高于暗示值——两头都钉住，谁也不许拿单边的巧合当合同。"""
    p = credits.parse_pricing({"mode": "token", "currency": "CNY",
                               "input_per_m": 0.5, "output_per_m": 100.0})
    m = credits.multiplier_label(p)
    assert m == "12.75x", m
    implied = (1_010_000 / 1000) * 12.75          # 把倍率当合同价的人这么算
    input_heavy = credits.credits_for_usage(p, prompt_tokens=1_000_000,
                                            completion_tokens=10_000)
    assert input_heavy == pytest.approx(937.5), input_heavy
    assert input_heavy < implied / 5, "输入重时倍率严重高估——这条方向不许反"
    output_heavy = credits.credits_for_usage(p, prompt_tokens=10_000,
                                             completion_tokens=1_000_000)
    assert output_heavy == pytest.approx(62503.125), output_heavy
    assert output_heavy > implied, "输出重时倍率严重低估——两条都不许多余"


def test_reasoning按输出档计价_cached不打折_且都不许被二次进账():
    """口径（T0.3/D10）：reasoning_tokens 含在 completion_tokens 里按输出档计价，
    cached_tokens 含在 prompt_tokens 里不打折。判据：同 prompt/completion 的两行，
    reasoning/cached 分布不同 → 积分完全相同（既没漏计、也没双重计价/折扣）。"""
    p = {"mode": "token", "currency": "CNY", "input_per_m": 1.0, "output_per_m": 4.0}
    a = credits.credits_for_tokens(p, prompt=1000, completion=500,
                                   reasoning=400, cached=0)
    b = credits.credits_for_tokens(p, prompt=1000, completion=500,
                                   reasoning=0, cached=800)
    assert a == b == credits.credits_for_usage(
        credits.parse_pricing(p), prompt_tokens=1000, completion_tokens=500)


# ===========================================================================
# T5.20 影子积分：算得出、看得见、拦得住（默认关）——usage 写口径一字不动
# ===========================================================================

def test_未定价的条目算不出积分_绝不悄悄当成零(priced_providers):
    """D14/验收 14 的内核半句：未定价的调用算不出就是算不出（None），
    当成 0 记 = 白烧部署者的钱还被当成『没花钱』。护栏与展示都必须把
    None 当缺口传播，而不是 sum 时溜成 0。"""
    assert credits.pricing_of("fake-model") is None
    assert credits.credits_for_usage(None, prompt_tokens=5000, completion_tokens=900) is None
    _today_row("ghost-user", "fake-model", prompt=10_000, completion=1_000)
    shadow = credits.user_shadow_day("ghost-user")
    assert shadow["total"] == 0.0 and len(shadow["gaps"]) == 1
    gap = shadow["gaps"][0]
    assert gap["provider_id"] == "fake-model" and gap["credits"] is None
    assert "未定价" in gap["reason"], f"缺口要给人话：{gap}"


def test_零倍率免费期照常算影子_垫资额是它存在的唯一理由(priced_providers):
    """0.00x 的免费期不扣积分，但影子必须照算——这是运营方垫资额的唯一来源
    （§2.3b：没有它，1.0 定价就是拍脑袋）。免费期由 free_until 表达，真实单价
    仍留在 pricing 里，影子 = 真实单价 × 真实 token。"""
    priced_providers("free-run", {"mode": "token", "currency": "CNY",
                                  "input_per_m": 1.0, "output_per_m": 4.0,
                                  "free_until": "2099-12-31"})
    _today_row("payer-user", "free-run", prompt=100_000, completion=20_000)
    shadow = credits.user_shadow_day("payer-user")
    # (0.1 + 0.08) ÷ 1.6 × 1000 = 112.5
    assert shadow["total"] == pytest.approx(112.5), shadow
    assert shadow["free_total"] == pytest.approx(112.5), "『免费期已垫付』单独可报"


def test_影子值只是派生显示_usage的写口径一个字都不许变(clean_usage, real_stream):
    """卡片 §2.5：usage.json 继续只存真值 token、不塞积分（派生可重算的报表与
    将来不可改的账目不许挤在一个文件）。判据：跑完一条真流式调用后，账行键集合
    仍恰是 FIELDS + paid_by，且盘上 JSON 里不出现任何 credit/积分字样。"""
    real_stream([[_chunk(content="一句"), _usage_chunk(3, 4)]])
    res = TestClient(app).post("/v1/chat/stream", headers=BOOT, json={
        "provider": "fake-model", "messages": [{"role": "user", "content": "说一句话"}]})
    assert res.status_code == 200, res.text
    rows = usage.snapshot()
    assert rows, "调用没落账"
    for r in rows:
        assert set(r) == set(usage.FIELDS) | {"paid_by", "day", "user_id", "provider_id"}, r
    with open(usage._PATH, encoding="utf-8") as f:
        blob = f.read()
    assert "credit" not in blob.lower() and "积分" not in blob, "影子值被写进账本了"


def test_影子护栏默认关_关着时算得出但绝不拦人(clean_usage, shadow_limit_env, priced_providers):
    """本批口径『拦得住（默认关）』：credit_shadow_daily_limit 默认 0 = 只算不拦。
    影子再大，流式请求也照常出网——开不开是部署者的决定，不是代码的默认。"""
    shadow_limit_env(None)
    assert credits.shadow_daily_limit() == 0.0
    priced_providers("loud-free", {"mode": "token", "currency": "CNY",
                                   "input_per_m": 1000.0, "output_per_m": 4000.0,
                                   "free_until": "2099-12-31"})
    _today_row(USER, "loud-free", prompt=1_000_000, completion=1_000_000)
    assert credits.user_shadow_day(USER)["total"] > 1e6
    assert credits.shadow_limit_exceeded(USER) is False, "默认不许拦"
    res = TestClient(app).post("/v1/chat/stream", headers=BOOT, json={
        "provider": "fake-model", "messages": [{"role": "user", "content": "继续说话"}]})
    assert res.status_code == 200, res.text


def test_护栏打开_请求入口就到线时第一条消息就停住_不出网且usage零增量(
        clean_usage, shadow_limit_env, priced_providers):
    """到线语义（卡片 §3-3）：停在『等待你确认是否继续』这句人话上，且这一次
    对 usage.json 零增量——先判断、再出网，反过来写闸门就永远慢一步。"""
    shadow_limit_env(1.0)
    priced_providers("big-priced", {"mode": "token", "currency": "CNY",
                                    "input_per_m": 1000.0, "output_per_m": 4000.0})
    _today_row(USER, "big-priced", prompt=100_000, completion=10_000)
    before = usage.snapshot()
    before_calls = sum(r["calls"] for r in before)
    client = TestClient(app)
    for path, body in (("/v1/chat/stream", {"provider": "big-priced",
                                            "messages": [{"role": "user", "content": "hi"}]}),
                       ("/v1/chat", {"provider": "big-priced",
                                     "messages": [{"role": "user", "content": "hi"}]})):
        res = client.post(path, headers=BOOT, json=body)
        assert res.status_code == 402, f"{path} 到线还放出去网：{res.status_code} {res.text[:120]}"
        detail = res.json()["detail"]
        assert "等待你确认是否继续" in detail, f"必须停在人话提示上：{detail}"
        assert "余额" not in detail and "已消费" not in detail, \
            f"本批没有余额概念，用户端不许出现这些字样：{detail}"
    after = usage.snapshot()
    assert sum(r["calls"] for r in after) == before_calls, "被拦下的请求对账本必须零增量"


def test_护栏打开时第二个付费轮在轮次边界被拦_读者在场也拦(
        clean_usage, shadow_limit_env, priced_providers, real_stream, grace_env):
    """影子越线必须是 before_round 的**一个判据**（并行安全边界 7）：与"有没有活
    读者"并列、共用同一个钩子与同一条取消出口。测试把活读者挂着、宽限设到 60 秒
    ——如果实现把影子判据挂成第二个闸或干脆漏掉，这条都会以"第二轮 create() 发生"
    或"停了但成因不是影子"翻红。"""
    shadow_limit_env(1.0)
    grace_env(60)
    # 到线的种子行落在另一个已定价 provider 上：usage 按 (user, provider) 并桶，
    # 若种子也记在 round-priced 上，"只有起飞的那一轮结账"就无法用 calls 恰为 1 判。
    priced_providers("seed-overlimit", {"mode": "token", "currency": "CNY",
                                        "input_per_m": 1000.0, "output_per_m": 4000.0})
    _today_row(USER, "seed-overlimit", prompt=100_000, completion=10_000)
    priced_providers("round-priced", {"mode": "token", "currency": "CNY",
                                      "input_per_m": 1000.0, "output_per_m": 4000.0})
    completions = real_stream([
        [_chunk(content="第一轮的半句"), _usage_chunk(9, 9), _tool_call_chunk()],
        [_chunk(content="不该出现的第二轮"), _usage_chunk(1, 1)],
    ])
    run = stream_runs.create_run(user_id=USER)
    launch_stream_run(run, provider_store.resolve("round-priced"),
                      [{"role": "user", "content": "算一下"}], "算一下",
                      Principal(USER, "本机管理员", "admin"), _tool_pipe(), [])
    reader_gen = stream_runs.sse_frames(run, 0)
    assert next(reader_gen) is not None          # 真读者在场
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and not run.finished:
        time.sleep(0.02)
    reader_gen.close()
    assert run.finished, "流没有收尾"
    assert len(completions.calls) == 1, \
        f"影子到线后第二轮照样出网了——护栏没接进轮次边界：{len(completions.calls)} 次 create()"
    assert run.status == "cancelled" and run.stop_cause == "shadow_daily_limit", \
        f"停在闸上可以，成因必须说清楚：{run.status}/{run.stop_cause}"
    payloads = [e["payload"] for e in run.events]
    cancelled = [p for p in payloads if p.get("type") == "cancelled"]
    assert cancelled and "等待你确认是否继续" in cancelled[-1]["message"], cancelled[-1]
    done = [p for p in payloads if p.get("type") == "done"][-1]
    assert done["status"] == "cancelled" and done["stopped_reason"] == "shadow_daily_limit"
    rows = [r for r in usage.snapshot() if r["provider_id"] == "round-priced"]
    assert sum(r["calls"] for r in rows) == 1, "只有起飞的那一轮结账"


def test_轮首只有一个闸_影子与读者是同一钩子的两个判据而非两座闸():
    """把『不许在轮首挂两个互不知情的独立闸』从口头纪律变成可执行判据：
    streaming 的检查点仍恰好一处（before_round() 调用），main.py 的钩子只有
    一个 before_round 定义、它内部同时持有影子与读者两个判据。有人再往轮首
    另起一个独立闸，先红在这里，再去评审里解释为什么『两处都在回答这一轮
    该不该花钱出网』的问题被拆成了两半。

    v0.29.0 起轮次循环从 stream_chat 搬进了 stream_chat_events（stream_chat 退化成
    只透正文的 str 薄过滤器），原来"读 stream_chat 那一个函数的源码"这个切面就
    失效了——不是判据错了，是尺子量错了地方。这里把数法改硬而不是改松：整个
    streaming 模块里 before_round() 的调用点恰好一处，并且那一条必须挂在真的会
    create() 出网的那个函数里，同时 stream_chat 必须把钩子原样转给那个函数。
    于是"闸门搬家"照样绿，"另起一座闸"和"闸挂在没人走的函数上"都会红。
    """
    import ast
    import inspect
    import app.core.streaming as streaming
    # conftest 的 autouse 桩挂在模块属性上（streaming.stream_chat = fake_stream），
    # 直接 inspect 会扫到桩。与 real_stream 同一招：reload 换回真函数再读源码——
    # 本用例只读源码、不发调用，reload 不影响任何行为判据。
    importlib.reload(streaming)
    src = inspect.getsource(streaming)
    assert src.count("before_round()") == 1, src

    def _plain_calls(node, fname):
        return sum(1 for n in ast.walk(node)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == fname)

    def _upstream_creates(node):
        return sum(1 for n in ast.walk(node)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "create")

    top_funcs = [n for n in ast.parse(src).body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    holders = [n.name for n in top_funcs if _plain_calls(n, "before_round")]
    assert len(holders) == 1, f"轮首检查点只许有一处，现在出现在 {holders}"
    holder = next(n for n in top_funcs if n.name == holders[0])
    assert _upstream_creates(holder) >= 1, (
        f"唯一的检查点挂在 {holders[0]} 上，可它自己从不 create()：说明真正的轮次"
        "循环在别处另起了一处，正是这条判据要拦的『两座闸』")
    filter_src = next((ast.get_source_segment(src, f) for f in top_funcs
                       if f.name == "stream_chat"), "")
    assert filter_src, "stream_chat 不在了：老签名那条兼容路径没人管，判据得跟着重写"
    assert holders[0] in filter_src and "before_round=before_round" in filter_src, (
        "stream_chat 没把 before_round 转给真正出网的那个函数：走 str 签名的调用方"
        "（非流式回退与一批既有用例）会绕过轮首这座闸")

    import app.main as m
    hook_src = inspect.getsource(m._produce_stream)
    assert hook_src.count("def before_round") == 1
    assert "shadow_limit_exceeded" in hook_src and "wait_for_reader" in hook_src, \
        "before_round 必须同时是影子判据与读者判据的挂靠点"


def test_管理端影子合计按天按provider_算不出的如实说算不出(clean_usage, priced_providers):
    """管理端读面（D23 的看板数据源）：语义是『免费期已垫付 N 积分』；未定价与
    per_call 的缺口单独列出来，而不是消失在总和里——『有 N 次调用算不出积分，
    这不是免费』的前半句必须有数据支撑。"""
    priced_providers("adm-priced", {"mode": "token", "currency": "CNY",
                                    "input_per_m": 1.0, "output_per_m": 4.0,
                                    "free_until": "2099-12-31"})
    priced_providers("adm-percall", {"mode": "per_call", "currency": "CNY"})
    _today_row(USER, "adm-priced", prompt=100_000, completion=20_000)   # 112.5 垫付
    _today_row(USER, "adm-percall", prompt=1000, completion=10)         # 算不出
    _today_row(USER, "fake-model", prompt=999, completion=9)            # 未定价
    body = TestClient(app).get("/v1/admin/usage").json()
    view = body["credits"]
    assert view["day"] == body["day"]
    per = {r["provider_id"]: r for r in view["rows"]}
    assert per["adm-priced"]["status"] == "free"
    assert per["adm-priced"]["credits"] == pytest.approx(112.5)
    assert per["adm-percall"]["credits"] is None and "按次" in per["adm-percall"]["reason"]
    assert per["fake-model"]["credits"] is None and "未定价" in per["fake-model"]["reason"]
    assert view["fronted_credits"] == pytest.approx(112.5), "免费期已垫付的口径"
    assert view["gap_providers"] == 2, "两个算不出的必须数得出来"
    assert view["total_credits"] == pytest.approx(112.5)
    assert "approx_cost" in view and view["currency"] == "CNY", "≈¥M 的数据源"


def test_影子合计跨天不串账_昨天的价今天重算但不改账(clean_usage, priced_providers):
    """派生显示可重算（§2.5 模式 A 那半句）：按天取数、当日行 × 当前单价现算。
    给昨天记了账、今天查护栏，昨天的影子不许混进今天的每日判定。"""
    priced_providers("yday", {"mode": "token", "currency": "CNY",
                              "input_per_m": 1000.0, "output_per_m": 4000.0})
    _today_row("someone", "yday", prompt=1_000_000, completion=0, day="2000-01-01")
    assert credits.user_shadow_day("someone", day="2000-01-01")["total"] > 0
    assert credits.user_shadow_day("someone", day="2000-01-02")["total"] == 0.0


def test_展示面给足双端抄写的原料_单价核对日期与免费期都在数据层(priced_providers):
    """界面文案归网页/安卓两条泳道，但『0.00x（1.0 前免费 · 单价核对于 …）』
    的原料必须在这一批的数据层就到位：_public/catalog 带 pricing 与 multiplier。"""
    priced_providers("surface", {"mode": "token", "currency": "CNY",
                                 "input_per_m": 1.0, "output_per_m": 4.0,
                                 "price_checked_on": "2026-10-01",
                                 "free_until": "2099-12-31"})
    pub = provider_store._public(provider_store.get("surface"))
    assert pub["pricing"]["price_checked_on"] == "2026-10-01"
    assert pub["multiplier"] == "0.00x" and pub["priced"] is True
    models = {m["id"]: m for m in TestClient(app).get("/v1/models").json()["models"]}
    assert models["surface"]["multiplier"] == "0.00x"
    assert models["fake-model"]["multiplier"] == "?x", "未定价在清单里必须是问号不是 0"
