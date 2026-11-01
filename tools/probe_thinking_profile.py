#!/usr/bin/env python3
"""思考参数真实核实探针（v0.39.1 Task 11）。

要回答的问题只有一个：`backend/app/core/thinking_profiles.py` 里那三份候选配方，
上游到底认不认、认了之后能不能**看见**它真的在思考。判「已核实」只认两种证据：
① 响应里出现 `reasoning_content` 之类的推理字段且有内容；②
`usage.completion_tokens_details.reasoning_tokens > 0`。**参数被上游拒掉不等于
「这个模型不支持思考」**——那一条只能留 `unverified`。

四条闸（都是代码，不是「记得」）
----------------------------------------
1. **出站总额 4 次**。`call-ledger.jsonl` 落在仓库外，每发一次**先记账再发**
   （进程半路死掉也算花掉了），发之前先数账本：`AUTHORIZED_LIVE_CALLS` 就是上限，
   到数了直接拒绝、一次都不发。`--attempt` 还要和账本序号对上——对不上说明
   「我以为这是第 3 次」和机器要发的不是同一次，那正是重复烧额度的形状。
   单次运行另外还有一道 `CALLS_PER_RUN = 1`；SDK 层用 `max_retries=0`，
   否则「一次调用」在传输层可以是三次 HTTP 请求。
2. **绝不写 data/**。`app/core/providers.py:1059` 有模块级 `store = ProviderStore()`，
   import 它就跑 `_load()`，而 `_load()` 看到内联密钥会 `_flush()` 回写
   `data/providers.json` + `provider_keys.json`——本仓目录同时是生产机，那一下写的
   就是线上配置。所以顺序被钉死：`isolate_runtime_env()` 把 `PROVIDERS_DB_PATH`
   指到仓库外 → `load_backend_modules(iso)` 才许 import providers →
   `assert_store_quarantined()` 再验一遍单例的三个路径（keys/prefs 由记录路径推导，
   而 `_flush()` 三个都写，所以三个都验）。**不用 `providers.store.resolve(...)`**：
   那条路走的就是 import 触发的单例。现网那条记录是自己 `json.load` 只读出来的。
3. **密钥不出终端**。出口只允许 `provider_id / profile / level / accepted /
   has_reasoning_content / reasoning_tokens / error_class / usage 三个计数 /
   密钥的 sha256[:12]`。异常正文先过值级遮盖（`secrets` 里那把真 key 只做内存替换）
   → `providers.scrub_secrets` → `pathmask.hide_paths`，最后整份 payload 还要过一次
   `render_and_check`：认出密钥值或凭据形状就一个字都不打印（rc 3）。
   值级那层不是多余的：`scrub_secrets` 的按值替换读的是**进程级 store** 那份表
   （`_own_key_values()`），而这里的 store 被隔离在临时目录、根本不认识现网那两把 key。
4. **先 dry-run 绿，才许真发**。`--dry-run` 走假 client，零网络、不记账，
   退出码两种走向由 `backend/tests/test_v0391_thinking_mapping.py` 演。
   附带一条同样免费的检查：配方里的字段先对着 `Completions.create` 的**签名**问一遍
   （本机 openai 2.37.0 没有 `**kwargs`，裸 `thinking=` 会在本地就 TypeError——
   一次 HTTP 都不发却已经占掉一格额度），到不了上游的形状在记账前就被挡掉。

诚实结果也在设计里：DashScope 兼容模式在 `stream=False` 下可能直接拒
`enable_thinking=true`。那种格子写成 `form_unverifiable`（「该形态今天不可核实」），
**不改成流式再发一次**——那会超出授权的 4 次。

退出码
    0  拿到证据（`verified`，或基线那一格的 `baseline_ok`）
    1  没拿到证据（`unverified` / `form_unverifiable` / 基线失败）——**不重试**
    2  闸拦停，这条结论不可采信：要么一次都没发（隔离/账本/配方/记录不成立），
       要么只读承诺被打破（跑完之后现网那两个文件的摘要变了）
    3  出口泄露闸拦停：payload 里还认得出凭据，整份一个字都不打印
"""
import argparse
import hashlib
import inspect
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:                       # 纯数据模块，import 它没有副作用
    sys.path.insert(0, str(BACKEND_DIR))

from app.core import thinking_profiles                      # noqa: E402  候选配方表（零依赖）

AUTHORIZED_LIVE_CALLS = 4        # 用户授权的出站总额，一次一记
CALLS_PER_RUN = 1                # 单次运行最多发 1 条：第 5 次绝对不许发
DEFAULT_PROMPT = "ping"          # 两个上游同一句，便于对照
DEFAULT_MAX_TOKENS = 8           # 极小：这一格花的是钱，不是结论
DEFAULT_TIMEOUT = 30.0
REDACTED = "«密钥已隐去»"
RUNTIME_DATA_FILES = ("providers.json", "provider_keys.json")

RC_VERIFIED = 0
RC_UNVERIFIED = 1
RC_BLOCKED = 2                   # 闸拦停：这条结论不可采信
RC_LEAK_BLOCKED = 3              # 出口泄露闸：一个字都不打

VERDICT_LABELS = {
    "verified": "已核实",
    "unverified": "未核实",
    "form_unverifiable": "该形态今天不可核实",
    "baseline_ok": "基线通过",
    "baseline_failed": "基线失败",
    "preflight_failed": "预检失败，未发出调用",
}


class BudgetExhausted(RuntimeError):
    """账本已记满 AUTHORIZED_LIVE_CALLS：再发就是超出授权。"""


class AttemptMismatch(RuntimeError):
    """`--attempt` 与账本序号对不上：登记的那次和机器要发的不是同一次。"""


class SecretLeak(RuntimeError):
    """出口内容里仍有凭据形状或已加载的密钥值：整份 payload 不许打印。"""


class FakeBadRequestError(RuntimeError):
    """假 client 用的 4xx 替身——`error_class` 要能报出形状，又不能是真异常类型。"""


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def inside_dir(child, parent) -> bool:
    """`child` 是否落在 `parent` 里头（Windows 大小写不敏感；跨盘符一律算不在）。"""
    c, p = _norm(child), _norm(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False


def overlaps(a, b) -> bool:
    """两个目录任一方向有包含关系就算重叠——临时目录和现网配置目录必须**互不相干**。"""
    return inside_dir(a, b) or inside_dir(b, a)


# --------------------------------------------------------------- 闸 2：隔离 ----

def isolate_runtime_env(isolation_dir=None) -> Path:
    """把 provider 配置（连带 DATA_ROOT）整体指到**仓库外**的临时目录。

    keys/prefs 两个路径都由记录路径推导（`ProviderStore.__init__`），所以指走
    `PROVIDERS_DB_PATH` 一个变量就够了——这也是为什么这里不再单独设它们：
    两处事实来源迟早会漂。
    """
    if isolation_dir:
        target = Path(os.path.abspath(str(isolation_dir)))
    else:
        target = Path(tempfile.mkdtemp(prefix="thinking-probe-iso-"))
    if inside_dir(target, REPO_ROOT):
        raise ValueError("隔离目录不许落在仓库内：那里同时是生产机，写进去就是动线上数据")
    target.mkdir(parents=True, exist_ok=True)
    os.environ["PROVIDERS_DB_PATH"] = str(target / "providers.json")
    os.environ["DATA_ROOT"] = str(target)
    return target


def load_backend_modules(isolation_dir):
    """唯一被允许 import `app.core.providers` 的入口，且必须先隔离过。

    顺序靠代码而不是靠注释：`isolation_dir` 为空就直接 raise，调用方拿不到 providers，
    也就碰不到那个模块级单例。判据的牙由
    `test_importing_providers_without_isolation_is_blocked` 演。
    """
    if not isolation_dir:
        raise RuntimeError("未隔离就不许 import app.core.providers："
                           "providers.py:1059 的模块级 store 会在导入时读写 data/")
    iso = Path(os.path.abspath(str(isolation_dir)))
    if inside_dir(iso, REPO_ROOT):
        raise RuntimeError("隔离目录在仓库内，拒绝导入 providers")
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from app.core import pathmask, providers
    return providers, pathmask


def assert_store_quarantined(providers_mod, forbidden_dirs) -> None:
    """导入之后**再验一遍**那个单例到底指着哪儿。

    判据取「不许落在这些目录里」而不是「必须等于我的临时目录」，是有意的：测试进程里
    `providers.store` 早就被 conftest 指到别的临时目录了，拿等号去判会把一条真闸演成
    假红。而真正要守的是——**这个单例不能指着我们即将只读的那份配置，也不能指着仓库**
    （本仓目录同时是生产机，`_flush()` 会写 `store.path`/`keys_path`/`prefs_path`
    三个文件，三个都验）。有人把 `isolate_runtime_env()` 那三行删了，这里就是那一下
    响亮失败的地方，而且此时一次都还没发。
    """
    store = getattr(providers_mod, "store", None)
    if store is None:
        raise RuntimeError("拿不到 providers.store，无法证明它没指着现网配置")
    roots = [Path(p) for p in forbidden_dirs]
    for label in ("path", "keys_path", "prefs_path"):
        target = getattr(store, label, None)
        if not target:
            raise RuntimeError(f"providers.store.{label} 读不出来，无法证明它被隔离")
        for root in roots:
            if inside_dir(target, root):
                raise RuntimeError(f"providers.store.{label} 落在禁止写入的目录里（{label}），拒绝继续")


# -------------------------------------------------------- 现网记录（只读） ----

def read_provider_record(data_dir, provider_id) -> dict:
    """`json.load` 只读出那一条 provider 记录，密钥从 keys 文件补进内存。

    不走 `providers.store`：那条路要 import，而 import 就会构造单例并可能回写
    现网配置（见模块 docstring 闸 2）。也不许 `cp provider_keys.json`——密钥只活在
    这个进程内存里，多一份副本就多一个泄露点。
    """
    base = Path(data_dir)
    providers_file = base / "providers.json"
    keys_file = base / "provider_keys.json"
    for f in (providers_file, keys_file):
        if not f.is_file():
            raise LookupError(f"读不到 {f.name}：--data-dir 得指到那份配置真正住着的目录")
    with providers_file.open("r", encoding="utf-8") as fh:
        records = json.load(fh)
    if not isinstance(records, list):
        raise LookupError("providers.json 不是列表，按现网形状读不懂")
    found = next((r for r in records if isinstance(r, dict) and r.get("id") == provider_id), None)
    if found is None:
        raise LookupError(f"现网配置里没有 provider_id={provider_id}")
    record = dict(found)
    inline = str(record.get("api_key") or "").strip()
    if inline:
        # 现网那份早就搬过家（记录里没有 api_key）。真有内联值时以它为准——keys 文件
        # 可能还没同步；来源记进 payload，别让"到底用的哪一把"变成事后考古。
        record["api_key"] = inline
        record["credential_origin"] = "inline"
    else:
        with keys_file.open("r", encoding="utf-8") as fh:
            keys = json.load(fh)
        value = str((keys or {}).get(provider_id) or "").strip()
        if not value:
            raise LookupError("这条记录没有可用密钥（keys 文件里没有它的这一格）")
        record["api_key"] = value
        record["credential_origin"] = "keys_file"
    for field in ("base_url", "model"):
        if not str(record.get(field) or "").strip():
            raise LookupError(f"记录缺 {field}，这条调用发出去也是白费额度")
    return record


def key_fingerprint(key: str) -> str:
    """密钥指纹前缀：能对照「是不是同一把」，反推不出值。"""
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:12] if key else ""


def files_digest(directory, names=RUNTIME_DATA_FILES) -> dict:
    """运行态文件「没被动过」的证据：文件名 + mtime_ns + size 的摘要。

    不摘内容、不摘路径，但改一个字节或写一次盘都会变——负控见
    `test_runtime_digest_actually_moves_when_the_file_is_written`。
    """
    out = {}
    for name in names:
        path = Path(directory) / name
        try:
            stat = path.stat()
        except OSError:
            out[name] = "missing"
            continue
        raw = f"{name}|{stat.st_mtime_ns}|{stat.st_size}"
        out[name] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return out


# ----------------------------------------------------------- 闸 1：记账 ----

def read_ledger(ledger_path) -> list:
    path = Path(ledger_path)
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue                      # 半行（写到一半被杀）不参与计数，宁可少计一次也不炸栈
        if isinstance(parsed, dict) and parsed.get("event") == "attempt":
            entries.append(parsed)
    return entries


def reserve_call_slot(ledger_path, provider_id, profile, level, attempt,
                      limit=AUTHORIZED_LIVE_CALLS) -> int:
    """先记账再发，返回这次占用的序号（从 1 数）。

    写在发之前是刻意的：调用中途进程死了、Ctrl+C 了、上游 hang 到超时——那一次的
    额度都已经花了，账上必须看得见。失败不重试，所以也不回头改账写什么 "result"。
    """
    used = len(read_ledger(ledger_path))
    if used >= limit:
        raise BudgetExhausted(f"账本已记 {used} 次，授权上限 {limit} 次：一次都不许再发")
    seq = used + 1
    if attempt is not None and int(attempt) != seq:
        raise AttemptMismatch(f"登记的是第 {attempt} 次，账本上下一个序号是 {seq}")
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"seq": seq, "event": "attempt", "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
             "provider_id": provider_id, "profile": profile, "level": level,
             "nonce": uuid.uuid4().hex[:8]}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return seq


# --------------------------------------------------------- 出口脱敏（闸 3） ----

def redact(text, secrets, scrub=None, hide=None) -> str:
    """一句要打印的话过三层：值级 → 形状级（scrub_secrets）→ 路径级（hide_paths）。

    值级放最前面不是习惯问题：`providers.scrub_secrets` 的按值替换读的是**进程级**
    store 那份密钥表，而这里 store 被隔离在临时目录、不认识现网那把 key。
    少了这一层，「上游把 Authorization 原样打印回来」就会带着真 key 走到屏幕上。
    """
    out = "" if text is None else str(text)
    for value in sorted({str(s) for s in (secrets or []) if s and len(str(s)) >= 4},
                        key=len, reverse=True):
        out = out.replace(value, REDACTED)
    if scrub is not None:
        out = scrub(out)
    if hide is not None:
        out = hide(out)
    return out


def render_and_check(payload, secrets, scrub=None) -> str:
    """整份 payload 的最后一道：认出密钥值或凭据形状就一个字都不打。

    前一层（`redact`）是逐字段打码，这一层是整份体检：它防的不是"这次忘没忘打码"，
    而是"以后谁往 payload 里加了个没过 redact 的字段"——那条路必须是响亮失败
    （rc 3），而不是静默把 key 送进终端历史和报告。
    """
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    for value in {str(s) for s in (secrets or []) if s and len(str(s)) >= 4}:
        if value in text:
            raise SecretLeak("payload 里还带着已加载的密钥值")
    if scrub is not None and scrub(text) != text:
        raise SecretLeak("payload 里还有凭据形状，拒绝打印整份结果")
    return text


def error_excerpt(exc, secrets, scrub=None, hide=None, limit=180) -> str:
    """只给"这一格为什么没核实"所需的那一句，且必须先过三层脱敏。"""
    return redact(f"{type(exc).__name__}: {exc}"[:limit], secrets, scrub, hide)


# ------------------------------------------------------------- 读响应证据 ----

def _get(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _extra(obj) -> dict:
    """pydantic v2 把上游多给的字段放在 `model_extra`；替身或老形状退回空表。"""
    extra = getattr(obj, "model_extra", None)
    return extra if isinstance(extra, dict) else {}


def read_evidence(response) -> dict:
    """只取「有没有在思考」这一件事，正文一个字不取。

    `reasoning_content` 的内容永远不进 payload——那份输出会被贴进报告、留在终端历史里，
    而推理文本可能带着对话语境。判据要的是"这颗字段存在且非空"，不是它写了什么。
    """
    evidence = {"accepted": True, "has_reasoning_content": False, "reasoning_tokens": 0,
                "usage": {"prompt_tokens": None, "completion_tokens": None,
                          "total_tokens": None},
                "evidence_fields": []}
    choices = _get(response, "choices") or []
    message = _get(choices[0], "message") if choices else None
    raw_rc = _get(message, "reasoning_content") or _extra(message).get("reasoning_content")
    if isinstance(raw_rc, str) and raw_rc.strip():
        evidence["has_reasoning_content"] = True
        evidence["evidence_fields"].append("message.reasoning_content")
    usage = _get(response, "usage")
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = _get(usage, name)
        evidence["usage"][name] = value if isinstance(value, int) else None
    details = _get(usage, "completion_tokens_details")
    reasoning_tokens = _get(details, "reasoning_tokens")
    if isinstance(reasoning_tokens, int) and reasoning_tokens:
        evidence["reasoning_tokens"] = reasoning_tokens
        evidence["evidence_fields"].append("usage.completion_tokens_details.reasoning_tokens")
    return evidence


def classify_verdict(baseline: bool, evidence: dict) -> str:
    """只有两种证据能判「已核实」，其它一律 unverified——参数被认下来不等于它在思考。"""
    if baseline:
        return "baseline_ok"
    if evidence.get("has_reasoning_content") or int(evidence.get("reasoning_tokens") or 0) > 0:
        return "verified"
    return "unverified"


def looks_form_unverifiable(exc, message: str) -> bool:
    """窄判据：同一句话里既提到思考参数又提到流式形态，才敢说「这个形态今天不可核实」。

    宁可少判：一条只说 `enable_thinking` 的 400 我们不知道它卡在哪一维，那种写
    `unverified`。把「我没试」说成「试不了」是本探针最贵的一类错。
    """
    low = str(message or "").lower()
    kind = type(exc).__name__.lower()
    client_side = ("badrequest" in kind or "invalidrequest" in kind
                   or "400" in low or "invalid_parameter" in low)
    return client_side and "thinking" in low and "stream" in low


# ------------------------------------------------------------ 假 client ----

class FakeClient:
    """零网络替身：形状照 OpenAI SDK 的返回对象搭，值全是钉好的。

    真调用只有一次机会，判据必须先在假的了跑对，所以每种形态各演一遍：
    `reasoning`（两颗证据都有）/ `quiet`（200 但没有任何推理痕迹）/
    `extra_reasoning`（推理字段躲在 model_extra 里，不是属性）/
    `error_form`（非流式被拒）/ `error_plain`（其它 4xx）/ `error_leak`（把 key 印回正文）。
    """

    def __init__(self, mode="reasoning", leak_value=None):
        self.mode = mode
        self.leak_value = leak_value
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.mode == "error_leak":
            raise FakeBadRequestError(f"invalid request; header was {self.leak_value}")
        if self.mode == "error_form":
            raise FakeBadRequestError(
                "Error code: 400 - enable_thinking is only supported when stream=true")
        if self.mode == "error_plain":
            raise FakeBadRequestError("Error code: 400 - unsupported parameter")
        if self.mode == "reasoning":
            message = SimpleNamespace(content="pong", reasoning_content="……若干字……")
            usage = SimpleNamespace(prompt_tokens=9, completion_tokens=8, total_tokens=17,
                                    completion_tokens_details=SimpleNamespace(reasoning_tokens=6))
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)
        message = SimpleNamespace(content="pong")
        if self.mode == "extra_reasoning":
            message.model_extra = {"reasoning_content": "……若干字……"}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=9, completion_tokens=8, total_tokens=17,
                                  completion_tokens_details=SimpleNamespace(reasoning_tokens=0)))


# ------------------------------------------------------------------ 主流程 ----

class CallBudget:
    """进程内的出站计数器：单次运行最多 1 条，超出直接 raise。"""

    def __init__(self, limit=CALLS_PER_RUN):
        self.limit = limit
        self.sent = 0

    def spend(self):
        if self.sent >= self.limit:
            raise BudgetExhausted(f"本次运行已发 {self.sent} 次，上限 {self.limit} 次：不重试")
        self.sent += 1


def build_live_client(providers_mod, record, timeout=DEFAULT_TIMEOUT):
    """真客户端只从这里出。`max_retries=0` 是额度闸的一部分，不是风格问题。"""
    return providers_mod.build_client(record, timeout=timeout, max_retries=0)


def create_kwarg_names():
    """真实 SDK 的 `create()` 收哪些顶层参数名；读不到签名（或它接受任意 kwargs）时给 None。

    这一问值一次真调用：本机 openai 2.37.0 的签名里**没有 `**kwargs`**，表里写出一个
    裸 `thinking=` 会在本地就 TypeError——一次 HTTP 都不发，而账本已经记走一格。
    所以签名检查必须发生在记账**之前**。
    """
    try:
        from openai.resources.chat.completions import Completions
    except Exception:                                           # noqa: BLE001 - 读不到就不演这条
        return None
    parameters = inspect.signature(Completions.create).parameters
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return None
    return set(parameters)


def kwargs_rejected_by_sdk(kwargs, names):
    """表里那份参数能不能塞进 `create()`：返回被拒的字段名列表（空 = 都能上）。"""
    if names is None:
        return []
    return sorted(key for key in (kwargs or {}) if key not in names)


def send_once(client, record, kwargs, budget, prompt=DEFAULT_PROMPT,
              max_tokens=DEFAULT_MAX_TOKENS):
    """一次 `stream=False` 的非流式调用。合并顺序钉死：思考配方盖在基础参数之后。"""
    budget.spend()
    params = {"model": record["model"],
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": max_tokens, "stream": False}
    params.update(kwargs or {})
    return client.chat.completions.create(**params)


def make_config(argv=None) -> dict:
    parser = argparse.ArgumentParser(
        description="思考参数核实探针（出站额度 4 次，一次一记）")
    parser.add_argument("--data-dir", required=True, help="provider 配置所在目录（只读）")
    parser.add_argument("--provider", required=True, help="provider_id，如 deepseek-chat")
    parser.add_argument("--profile", required=True, choices=list(thinking_profiles.probe_profile_ids()),
                        help="候选配方名；none = 基线（不发任何思考参数）")
    parser.add_argument("--level", default=None,
                        help="档位标签（%s）；基线不填" % "、".join(thinking_profiles.THINKING_LEVEL_LABELS))
    parser.add_argument("--attempt", type=int, default=None, help="登记的第几次，须与账本序号一致")
    parser.add_argument("--isolation-dir", default=None, help="仓库外临时目录；缺省自建")
    parser.add_argument("--ledger", default=None, help="出站账本；缺省落在隔离目录里")
    parser.add_argument("--dry-run", action="store_true", help="假 client：零网络、不记账")
    parser.add_argument("--fake", default="reasoning",
                        choices=("reasoning", "quiet", "extra_reasoning", "error_form",
                                 "error_plain", "error_leak"), help="dry-run 演哪一种形态")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return vars(parser.parse_args(argv))


def run_probe(config, client_factory=None):
    """跑一次探针，返回 `(payload, rc, secrets)`。

    `client_factory` 只给测试用：注入一个替身，就能在零网络下走完真实调用那段代码
    （隔离 → 单例校验 → 记账 → 发 → 读证据 → 判据），CLI 里没有这个开关。
    第三个返回值是那把真 key——只在本进程内存里，`main()` 把它交给出口体检，绝不打印。
    """
    baseline = config["profile"] == thinking_profiles.NONE_PROFILE
    # 字段名有条反作用在身上的约束：整份 payload 出门前会再过一次 `scrub_secrets`
    # （`render_and_check`），而它的第一条形状规则连**键名**一起吃——`key_fp`、
    # `key_source` 这种名字会被它当成凭据掩掉，于是干净的结果被误判成泄露、整份拒打
    # （实测踩过）。所以这里回避 `sk_/pk_/key_/api_/secret_/token_/auth_` 开头的键名：
    # 宁可名字绕一点，也不让那道闸在自己的输出上恒红。
    payload = {
        "probe": "thinking_profile", "task": "v0.39.1 Task 11",
        "live": not config["dry_run"],
        "transport": "openai_sdk" if (client_factory is None and not config["dry_run"]) else "test_double",
        "provider_id": config["provider"], "profile": config["profile"],
        "level": config["level"], "authorized_live_calls": AUTHORIZED_LIVE_CALLS,
        "attempt": config["attempt"], "call_slot": None,
        "request_shape": {"prompt": DEFAULT_PROMPT, "max_tokens": config["max_tokens"],
                          "stream": False, "timeout": config["timeout"], "max_retries": 0},
        "request_kwargs": {}, "request_body": {}, "sdk_kwarg_check": None,
        "credential_fp": None, "credential_origin": None,
        "accepted": None, "has_reasoning_content": False, "reasoning_tokens": 0,
        "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
        "evidence_fields": [], "error_class": None, "error_excerpt": None,
        "verdict": None, "verdict_label": None, "isolation_ok": False,
        "runtime_data_untouched": None, "notes": [],
    }
    secrets = []
    scrub = hide = None

    def blocked(note, exc=None):
        """闸拦停：不给结论，只给"为什么没发"。"""
        payload["verdict"] = payload["verdict"] or "preflight_failed"
        payload["verdict_label"] = VERDICT_LABELS[payload["verdict"]]
        payload["notes"].append(redact(note, secrets, scrub, hide))
        if exc is not None:
            payload["error_class"] = type(exc).__name__
            payload["notes"].append(error_excerpt(exc, secrets, scrub, hide))
        return payload, RC_BLOCKED, secrets

    # —— 闸 2：先隔离，才许 import providers；再把"会写哪儿"验一遍
    try:
        data_dir = Path(os.path.abspath(str(config["data_dir"])))
        iso = isolate_runtime_env(config["isolation_dir"])
        providers_mod, pathmask_mod = load_backend_modules(iso)
        # 临时目录与现网配置目录必须互不相干：`--isolation-dir` 指到 data/ 里面去的话，
        # "隔离"就恰好变成了"把要保护的那份挂进来"。
        if overlaps(iso, data_dir):
            raise RuntimeError("隔离目录与 --data-dir 重叠，拒绝继续")
        assert_store_quarantined(providers_mod, [REPO_ROOT, data_dir])
    except Exception as exc:                                    # noqa: BLE001 - 闸没让过就停
        return blocked("隔离闸拒绝：providers 单例没被请到仓库外", exc)
    payload["isolation_ok"] = True
    scrub, hide = providers_mod.scrub_secrets, pathmask_mod.hide_paths

    # —— 只读现网那条记录
    try:
        record = read_provider_record(config["data_dir"], config["provider"])
    except Exception as exc:                                    # noqa: BLE001
        return blocked("读不到可用记录，未发出调用", exc)
    key = record["api_key"]
    secrets.append(key)
    payload["credential_fp"] = key_fingerprint(key)
    payload["credential_origin"] = record["credential_origin"]
    if providers_mod.looks_placeholder(key):
        return blocked("密钥看着像占位符，发出去也是白烧额度")

    # —— 配方：表里没有这个形状就 ValueError 退出，零调用
    try:
        kwargs = thinking_profiles.probe_kwargs(config["profile"], config["level"])
    except ValueError as exc:
        return blocked("候选表里没有这个形状（不联网就退出）", exc)
    payload["request_kwargs"] = kwargs
    payload["request_body"] = thinking_profiles.body_fragment(kwargs)

    # —— 这一份参数能不能塞进真 SDK（在记账**之前**问，别用一格额度去撞一个本地 TypeError）
    names = create_kwarg_names()
    payload["sdk_kwarg_check"] = "skipped" if names is None else "checked"
    rejected = kwargs_rejected_by_sdk(kwargs, names)
    if rejected:
        payload["notes"].append("SDK 的 create() 不收这些字段（没发出去，也没记账）："
                                + ", ".join(rejected))
        return blocked("配方到不了上游：字段不在 create() 签名里", None)

    before = files_digest(config["data_dir"])

    # —— 闸 1：真实调用先记账（dry-run 不进账本，它没花额度）
    if not config["dry_run"]:
        ledger = config["ledger"] or str(iso / "call-ledger.jsonl")
        try:
            payload["call_slot"] = reserve_call_slot(
                ledger, config["provider"], config["profile"], config["level"],
                config["attempt"])
        except (BudgetExhausted, AttemptMismatch, OSError) as exc:   # noqa: BLE001
            return blocked("账本没让过：这一次不发", exc)

    if client_factory is not None:
        factory = client_factory
    elif config["dry_run"]:
        factory = lambda record: FakeClient(mode=config["fake"], leak_value=key)   # noqa: E731
    else:
        factory = lambda record: build_live_client(providers_mod, record,          # noqa: E731
                                                    config["timeout"])
    budget = CallBudget()
    try:
        evidence = read_evidence(send_once(factory(record), record, kwargs, budget,
                                           max_tokens=config["max_tokens"]))
    except Exception as exc:                                    # noqa: BLE001 - 错误本身就是结论
        message = f"{type(exc).__name__} {exc}"
        payload.update({"accepted": False, "error_class": type(exc).__name__,
                        "error_excerpt": error_excerpt(exc, secrets, scrub, hide)})
        if baseline:
            payload["verdict"] = "baseline_failed"
        else:
            payload["verdict"] = ("form_unverifiable" if looks_form_unverifiable(exc, message)
                                  else "unverified")
        payload["verdict_label"] = VERDICT_LABELS[payload["verdict"]]
        payload["notes"].append("失败不重试：这一格的证据就是这条错误本身")
        payload["runtime_data_untouched"] = files_digest(config["data_dir"]) == before
        return payload, RC_UNVERIFIED, secrets

    payload.update({k: v for k, v in evidence.items() if k != "accepted"})
    payload["accepted"] = True
    payload["verdict"] = classify_verdict(baseline, evidence)
    payload["verdict_label"] = VERDICT_LABELS[payload["verdict"]]
    payload["runtime_data_untouched"] = files_digest(config["data_dir"]) == before
    if not payload["runtime_data_untouched"]:
        payload["notes"].append("只读承诺被打破：跑完之后现网那两个文件的摘要变了，"
                                "本次结论不予采信")
        return payload, RC_BLOCKED, secrets
    if payload["verdict"] in ("verified", "baseline_ok"):
        return payload, RC_VERIFIED, secrets
    payload["notes"].append("没有推理字段也没有 reasoning_tokens：参数被接受 ≠ 它在思考，"
                            "这一格留 unverified")
    return payload, RC_UNVERIFIED, secrets


def _loaded_scrub():
    """出口那道 scrub 的取法：providers 已经导入过就直接用它，不为打印再走一次隔离闸。"""
    mod = sys.modules.get("app.core.providers")
    return getattr(mod, "scrub_secrets", None) if mod else None


def _blocked_line(reason) -> str:
    return json.dumps({"probe": "thinking_profile", "exit_guard": "blocked",
                       "reason": str(reason)[:80], "printed_fields": 0},
                      ensure_ascii=False, sort_keys=True)


def main(argv=None) -> int:
    config = make_config(argv)
    try:
        payload, rc, secrets = run_probe(config)
        text = render_and_check(payload, secrets, _loaded_scrub())
    except SecretLeak as exc:
        # 整份 payload 都不打——宁可这一格没有记录，也不把 key 送进终端历史与报告。
        print(_blocked_line(exc))
        return RC_LEAK_BLOCKED
    except Exception as exc:                                        # noqa: BLE001
        # 崩在这里时那句 message 没经过任何一层脱敏（它可能带着 key 或本机路径），
        # 所以只报类型名。栈留给调试器，不留在 stdout。
        print(_blocked_line(f"探针异常：{type(exc).__name__}"))
        return RC_BLOCKED
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
