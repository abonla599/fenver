#!/usr/bin/env python3
"""思考参数真实核实探针（v0.39.1 Task 11）。

要回答的问题只有一个：`backend/app/core/thinking_profiles.py` 里那三份候选配方，
上游到底认不认、认了之后能不能**看见**它真的在思考。判「已核实」只认两种证据：
① 响应里出现 `reasoning_content` 之类的推理字段且有内容；②
`usage.completion_tokens_details.reasoning_tokens > 0`。**参数被上游拒掉不等于
「这个模型不支持思考」**——那一条只能留 `unverified`。

四条闸（都是代码，不是「记得」）
----------------------------------------
1. **出站总额 4 次**。`call-ledger.jsonl` 落在仓库外——这句从修复轮 2 起**也是判据**：
   账本若落在本仓树内或属于另一棵 checkout，闸 0 直接拒（N3）。每发一次**先记账再发**
   （进程半路死掉也算花掉了），发之前先数账本：`AUTHORIZED_LIVE_CALLS` 就是上限，
   到数了直接拒绝、一次都不发。`--attempt` 还要和账本序号对上——对不上说明
   「我以为这是第 3 次」和机器要发的不是同一次，那正是重复烧额度的形状。
   单次运行另外还有一道 `CALLS_PER_RUN = 1`；SDK 层用 `max_retries=0`，
   否则「一次调用」在传输层可以是三次 HTTP 请求。
   **live 模式必须显式给 `--ledger`**（修复轮 1，Critical-1）：账本缺省时它落在
   **本次**隔离目录里，而 `isolate_runtime_env(None)` 每次 `mkdtemp` 一个新目录 ⇒
   每个进程都从空账本开始、`used=0` 恒成立，"总额 4 次"这道闸永不生效。今天没超额度
   纯粹因为操作者手动传了持久 `--isolation-dir`——那叫"由操作者记得"，不叫"由机制守"。
   所以：非 `--dry-run` 而不给 `--ledger` → rc=2 拒发、**零出站、零写盘、连环境变量都不动**。
   授权用尽后**不自动续**，要更多次需要用户新的明示。
2. **绝不写 data/**。`app/core/providers.py:1059` 有模块级 `store = ProviderStore()`，
   import 它就跑 `_load()`，而 `_load()` 缺文件时 `_seed_from_env()`（读 `DEEPSEEK_API_KEY`）
   非空即 `_flush()` 落盘 `providers.json` + `provider_keys.json`（第三个 `provider_prefs.json`
   由 `set_default` 那一路写，所以单例的三个落点都验）——本仓目录同时是生产机，指错了地方
   写的就是线上配置。所以顺序被钉死：
   **先判 `overlaps(请求的隔离目录, data_dir)`** → `isolate_runtime_env()`（这一步才 mkdir）
   → 再判一次 `overlaps(iso, data_dir)`（缺省自建那一支的落点）→ 才许 `load_backend_modules(iso)`
   → `assert_store_quarantined()` 验单例的 `path`/`keys_path`/`prefs_path` 三个落点。
   **重叠判据必须在 import 之前**（修复轮 1，Important-3）：import 本身就是那一次写盘，
   先 import 再判重叠等于"先把文件开进只读目录，再宣布这里不许有文件"。判据走
   `os.path.realpath` 而非词法路径（修复轮 2，N2）：`mklink /J` 造的重解析点换个名字
   指的还是同一棵树，词法层判"不相干"就是把尺子的刻度停在字符串上。
   只读证据 `files_digest` **递归覆盖 `--data-dir` 整棵树**（修复轮 1，Important-3），
   且 `before` 快照取在 `isolate_runtime_env()` **之前**（修复轮 2，N2：原来取在
   import 之后，"隔离/import 期间"的写盘在结构上就看不见）。跑完后的比对拆成软硬两格
   （修复轮 2，concerns.3）：**硬**（rc=2，结论作废）= store 三件套变了或多/少路径；
   **软**（只追加 note、点名是哪几项）= 既有无关文件的 mtime/size 变了——现网 300 秒
   定时器会无条件重写 `data/preference.txt`，硬判等价于拿付费额度去给心跳陪葬。
   **不用 `providers.store.resolve(...)`**：那条路走的就是 import 触发的单例。
   现网那条记录是自己 `json.load` 只读出来的。
3. **密钥不出终端**。出口只允许 `provider_id / profile / level / accepted /
   has_reasoning_content / reasoning_tokens / error_class / usage 三个计数 /
   密钥的 sha256[:12]`。异常正文**先脱敏再截断**（`error_excerpt`，修复轮 1，Important-5：
   先 `[:180]` 会让一颗跨边界的无形状 key 漏出前缀碎片，而值级整值替换和整份体检都要求
   **完整值在场**，两层都不认那半截）→ `providers.scrub_secrets` → `pathmask.hide_paths`，
   最后整份 payload 还要过一次 `render_and_check`：认出密钥值或凭据形状就一个字都不打印（rc 3）。
   值级那层不是多余的：`scrub_secrets` 的按值替换读的是**进程级 store** 那份表
   （`_own_key_values()`），而这里的 store 被隔离在临时目录、根本不认识现网那两把 key。
4. **先 dry-run 绿，才许真发**。`--dry-run` 走假 client，零网络、不记账，
   退出码两种走向由 `backend/tests/test_v0391_thinking_mapping.py` 演。
   dry-run 产出的行在机器可读层必须自己承认是模拟（修复轮 2，N1）：`transport` 打
   `fake_client`（不是 `openai_sdk`），`scope.simulated` 为 `true`——§R8 第 8 条拿
   `transport` 当"零出站的机械凭据"用，让一行模拟数据顶着真发的章，烧掉的是这份凭据本身。
   附带一条同样免费的检查：配方里的字段先对着 `Completions.create` 的**签名**问一遍
   （本机 openai 2.37.0 没有 `**kwargs`，裸 `thinking=` 会在本地就 TypeError——
   一次 HTTP 都不发却已经占掉一格额度），到不了上游的形状在记账前就被挡掉。
   live 模式还多一道形状闸：`--max-tokens` 必须 ≤ `AUTHORIZED_MAX_TOKENS`（8），
   越出授权形状就拒——自由命令行值不是越权的许可证。

结论的射程也在设计里（修复轮 1，Important-4）：能判「已核实」的那两种证据只证明
**这一格参数形状被这条上游接受**。两个上游的基线本来就在出推理，所以开关效果、
三档有没有区别，**都没证**。Task 12 的消费端已经准备直接抄结论（`enabled: status === "verified"`
+ 档位候选吃服务端 `thinking_levels`），裸「已核实」在那头就变成"三个点、wire 上一个值"。
所以 payload 自带 `scope` / `level_used` / `level_distinct_wire_values` /
`baseline_had_reasoning`，`verdict_label` 也写成自带射程的句子。

诚实结果也在设计里：DashScope 兼容模式在 `stream=False` 下可能直接拒
`enable_thinking=true`。那种格子写成 `form_unverifiable`（「该形态今天不可核实」），
**不改成流式再发一次**——那会超出授权的 4 次。

本工具不许对另一个 checkout 运行（修复轮 1，Minor；修复轮 2 补 N3/N4）：`inside_dir(…, REPO_ROOT)`
只挡发起这一跑的那棵树，所以 `--data-dir`（以及 live 必填的 `--ledger`——账本同样是落盘，
N3）若沿父链上溯碰到 `.git` 且不是本工具这棵树，直接拒。N4：原判据只看父级一层，
`<other>/data/swap-snapshots/snap-1/data` 这种深树形状曾放行并一路读到密钥指纹——现在沿
**整条父链**上溯（判据只 stat 各级祖先目录有没有 `.git`，**绝不去读树里那份 data/**）。

退出码
    0  拿到证据（`verified`，或基线那一格的 `baseline_ok`）
    1  没拿到证据（`unverified` / `form_unverifiable` / 基线失败）——**不重试**
    2  闸拦停，这条结论不可采信：要么一次都没发（预检：账本没显式给 / 账本落在仓库内或
       属于另一棵 checkout / 形状越出授权 / `--data-dir` 属于另一棵 checkout；
       或隔离、重叠、账本、配方、记录任一条不成立），要么只读承诺被**硬**打破
       （跑完之后 store 三件套变了，或 `--data-dir` 树里多/少了路径；无关文件只是
       mtime/size 漂移走软判：追加 note 点名，rc 按原判据走。修复轮 2，concerns.3）
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

AUTHORIZED_LIVE_CALLS = 4        # 用户 2026-11-01 明示授权的 4 次：两上游各 1 基线 + 1 带参数
                                 # （用尽即停，**不自动续**；改宽这个数字需要用户新的明示。
                                 #  字面值与它作为默认上限的行为，由
                                 #  test_authorized_live_call_count_is_the_grant_itself 钉住）
CALLS_PER_RUN = 1                # 单次运行最多发 1 条：第 5 次绝对不许发
DEFAULT_PROMPT = "ping"          # 两个上游同一句，便于对照
DEFAULT_MAX_TOKENS = 8           # 极小：这一格花的是钱，不是结论
AUTHORIZED_MAX_TOKENS = DEFAULT_MAX_TOKENS   # 授权形状里那一格的字面值（live 模式的上限）
DEFAULT_TIMEOUT = 30.0
REDACTED = "«密钥已隐去»"
RUNTIME_DATA_FILES = ("providers.json", "provider_keys.json")
# store 单例真正拥有的三份文件（providers/keys 由 _flush 写、prefs 由 set_default 那一路写）。
# 只读证据的**硬判**认这三份 + 结构增减；别的文件漂移只算软判（修复轮 2，concerns.3）。
STORE_OWNED_FILES = ("providers.json", "provider_keys.json", "provider_prefs.json")

RC_VERIFIED = 0
RC_UNVERIFIED = 1
RC_BLOCKED = 2                   # 闸拦停：这条结论不可采信
RC_LEAK_BLOCKED = 3              # 出口泄露闸：一个字都不打

VERDICT_LABELS = {
    # 「已核实」三个字不许单独出现在结论里（Important-4）：Task 12 的消费端是
    # `enabled: status === "verified"` + 档位候选直接吃服务端 `thinking_levels`，
    # 一个不带射程的裸结论在那头就长成"三个点、wire 上一个值"。
    "verified": "参数形状已核实（开关效果与档位差别未证）",
    "unverified": "未核实",
    "form_unverifiable": "该形态今天不可核实",
    "baseline_ok": "基线通过（不代表思考已关：不发参数也在出推理）",
    "baseline_failed": "基线失败",
    "preflight_failed": "预检失败，未发出调用",
}

# 结论的射程，机器可读那一层。`not_proved` 不许空着——空着就等于让下游自己猜，
# 而"自己猜"在这一单里已经付过一次代价（见 VERDICT_LABELS 那条注释）。
VERDICT_SCOPES = {
    "verified": {
        "tier": "shape",
        "sent": True,
        "proved": "这一格的参数形状被这条上游接受，且响应里出现了推理证据",
        "not_proved": ("开关有没有效果（基线本就出推理，差分为 0）",
                       "三档有没有区别（本档只证这一个出网值）",
                       "流式形态（本单没花额度去试）",
                       "模型级 / 厂商级结论"),
    },
    "unverified": {
        "tier": "none",
        "sent": True,
        "proved": "无：参数被接受 ≠ 它在思考",
        "not_proved": ("参数形状", "开关效果", "档位差别"),
    },
    "form_unverifiable": {
        "tier": "none",
        "sent": True,
        "proved": "无：这一格只证明该形态在当前请求形状下被拒",
        "not_proved": ("参数形状", "开关效果", "档位差别", "换形态后能不能通（没试，试要超授权）"),
    },
    "baseline_ok": {
        "tier": "control",
        "sent": True,
        "proved": "这条 provider 记录今天能通（不带任何思考参数）",
        "not_proved": ("思考有没有被关闭——基线响应里就有推理痕迹时，"
                       "「关闭思考」这句话对模型行为就是不诚实的"),
    },
    "baseline_failed": {
        "tier": "none",
        "sent": True,
        "proved": "无：连不带参数的基线都没打通，这一格不能当对照用",
        "not_proved": ("参数形状", "开关效果", "档位差别"),
    },
    "preflight_failed": {
        "tier": "none",
        "sent": False,
        "proved": "无：一次都没发出去，本行不含任何上游事实",
        "not_proved": ("参数形状", "开关效果", "档位差别"),
    },
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
    """路径口径：**realpath**，不是 abspath（修复轮 2，N2）。词法口径（abspath+normcase）
    看不见重解析点：`mklink /J iso-junction → <data-dir>/iso` 被判"与 --data-dir 不相干"，
    于是 mkdir、import 照常进行，只读树里真被开出文件。realpath 把 junction/symlink
    还原到真实目标再比；不存在的尾部路径按最长存在前缀解析，不需要目标先存在。"""
    return os.path.normcase(os.path.realpath(str(path)))


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


def data_dir_belongs_to_another_checkout(data_dir) -> bool:
    """`data_dir`（或账本文件路径）是不是落在**另一棵 checkout** 里。

    为什么要有这一条：`inside_dir(target, REPO_ROOT)` 只挡得住"发起这一跑的那棵树"。
    从 `.qoder/worktrees/<x>` 跑、而 `--data-dir` 指主工作树 `data/` 这个形状，
    原本只靠别的判据兜着——而 AGENTS.md §1 把 `data/` 列为受保护运行期资产，
    主工作树那一份**就是现网正在用的那一份**。所以把它显式列入禁止名单。

    修复轮 2（N4）：原判据只看 `target.parent` 一层——`<other>/data` 拒了，
    `<other>/data/swap-snapshots/snap-1/data`（同样一份 providers.json + provider_keys.json）
    却放行并一路读到 `credential_fp`。现在沿**整条父链**上溯：先撞见 `REPO_ROOT` ⇒ 放行
    （那是咱们自己这棵树），先撞见 `.git` ⇒ 拒。

    **只加判据，不去读它**：这里只对各级祖先目录的存在性与 `.git` 做 `exists()`，
    既不打开树里任何文件，也不 `stat` 它们的内容。
    """
    root_norm = _norm(REPO_ROOT)
    current = Path(_norm(data_dir)).parent
    while True:
        if _norm(current) == root_norm:
            return False
        if (current / ".git").exists():
            return True
        parent = current.parent
        if parent == current:                       # 到盘根了：链上没有 .git，放行
            return False
        current = parent


# --------------------------------------------------------------- 闸 2：隔离 ----

def isolate_runtime_env(isolation_dir=None) -> Path:
    """把 provider 配置（连带 DATA_ROOT）整体指到**仓库外**的临时目录。

    keys/prefs 两个路径都由记录路径推导（`ProviderStore.__init__`），所以指走
    `PROVIDERS_DB_PATH` 一个变量就够了——这也是为什么这里不再单独设它们：
    两处事实来源迟早会漂。

    **本工具不许对另一个 checkout 运行**：这里挡的是发起这一跑的那棵树
    （`REPO_ROOT`）。从工作树跑而 `--data-dir` 指另一棵树（例如主工作树那份
    现网 `data/`）的形状，由 `data_dir_belongs_to_another_checkout()` 在调用本函数
    **之前**拒掉——那道判据在 `run_probe` 的预检里，不在这里，因为到这儿已经太晚了。

    另外：缺省自建的是**每次全新的 `mkdtemp`**。这正是 Critical-1 的根因——
    账本若跟着隔离目录走，每个进程都从空账本开始，"总额 4 次"永不生效。所以
    live 模式必须由调用方显式给一条跨进程稳定的 `--ledger`（见 `run_probe` 的预检）。
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
    （本仓目录同时是生产机：`_flush()` 写 `store.path` 与 `keys_path`，
    `set_default` 那一路还写 `prefs_path`，三个都验）。有人把 `isolate_runtime_env()`
    那三行删了，这里就是那一下响亮失败的地方，而且此时一次都还没发。
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
    """运行态文件「没被动过」的证据：整棵树每一项的 名字 + mtime_ns + size 的摘要。

    **递归覆盖 `directory` 下的每一层**（修复轮 1，Important-3）。原来只摘顶层那两个
    名字，于是"`<data-dir>/iso/providers.json` 被开出来了"这种事这把尺子**根本看不见**
    ——闸和它所量的洞一起瞎。现在多一个子目录、多一个深层文件、深层文件改一个字节都会动。

    不摘内容、不摘路径，也不读内容（只 `stat`）：`data/` 里可能有正在被现网服务改写的
    大文件，读它既慢又越界。代价是这条证据认的是"名字/时间戳/大小"三元组而不是逐字节哈希，
    所以报告里那句"逐字节不变"用的是外面另算的 `sha256` 清单，不是这个函数。
    这把尺子看得到多少，还取决于**快照取的时点**（修复轮 2，N2）：`before` 必须取在
    `isolate_runtime_env()` / import **之前**，否则"隔离期间"的写盘依旧不在射程里。
    跑完后的比对不是一条"相等/不等"，而是 `digest_delta` 的软硬两格（concerns.3）。
    负控见 `test_runtime_digest_actually_moves_when_the_file_is_written` 与
    `test_files_digest_covers_everything_under_the_data_dir`。
    """
    root = Path(directory)
    out = {}
    if root.exists():
        for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
            rel = path.relative_to(root).as_posix()
            try:
                stat = path.stat()
            except OSError:
                out[rel] = "unreadable"
                continue
            if path.is_dir():
                # 目录也进摘要，而且带上它自己的时间戳：删掉再建一个同名空目录
                # 也是"动过"，只记名字的话这种形状会漏（写在这里而不是"dir"常量上）。
                raw = f"{rel}|d|{stat.st_mtime_ns}"
            else:
                raw = f"{rel}|f|{stat.st_mtime_ns}|{stat.st_size}"
            out[rel] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    for name in names:
        # 顶层那两份就算不存在也要在摘要里留一格："从无到有"得能让摘要动起来。
        out.setdefault(name, "missing")
    return out


def digest_delta(before: dict, after: dict) -> tuple:
    """只读证据的**软硬两格**（修复轮 2，concerns.3；审查员给的两格拆法，不加第三条）。

    硬（结论作废，rc=2）：store 拥有的三件套变了，**或**树里多/少了路径——后者才是
    Important-3 要挡的形状（`<data-dir>/iso/providers.json` 被开出来）。
    软（只追加 note 并点名是哪几项）：树里**既有无关文件**的 mtime/size 变了。现网
    300 秒定时器（`main.py:426-442` → `analyze_all_preferences()`）无条件重写
    `data/preference.txt`，把它硬判等于拿已经花掉的付费额度给心跳陪葬——误红既烧钱
    又把下一次真红训成"狼来了"。

    返回 `(hard, soft)`，均为有序名单。单元负控：`test_digest_delta_separates_hard_from_soft`。
    """
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(after) & set(before) if before[k] != after[k])
    store_changed = [k for k in changed if k in STORE_OWNED_FILES]
    soft = [k for k in changed if k not in STORE_OWNED_FILES]
    return sorted(added + removed + store_changed), soft


def readonly_delta_verdict(payload, before: dict, data_dir) -> list:
    """把 `digest_delta` 的结论写进 payload：`runtime_data_untouched`、软硬两条 note。
    返回硬判名单（空 = 通过原判据）。成功路与失败路共用这一个函数——写盘这件事
    不挑结论（修复轮 2；错误那一支以前只记 flag 不作废，store 被改了也照样 rc=1）。"""
    after = files_digest(data_dir)
    hard, soft = digest_delta(before, after)
    payload["runtime_data_untouched"] = not hard and not soft
    if hard:
        payload["notes"].append("只读承诺被打破：store 三件套被改写或树里多/少了路径（"
                                + ", ".join(hard) + "），本次结论不予采信")
    if soft:
        payload["notes"].append("窗口内有无关既有文件被重写（mtime/size 变了，不影响本次判据，"
                                "点名如下）：" + ", ".join(soft))
    return hard


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
    """只给"这一格为什么没核实"所需的那一句，且**先脱敏、后截断**。

    顺序是有原因的（Important-5）：值级整值替换和出口那道整份体检都要求**完整值在场**。
    先 `[:limit]` 再脱敏，一颗没有凭据形状的 key 跨在边界上时，留在句子里的那半截
    两层都不认——于是 ≥6 字的前缀碎片就这么走进终端历史和报告了（审查员实测三组样本
    `exit_gate=allows print`）。截完之后再过一遍三层，是为了挡住"截断把某个已确认
    的片段切成新形状"这种次要情形。反例锁：
    `test_error_excerpt_is_redacted_before_it_is_truncated`。
    """
    full = redact(f"{type(exc).__name__}: {exc}", secrets, scrub, hide)[:limit]
    return redact(full, secrets, scrub, hide)


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


def stamp_verdict(payload, simulated) -> None:
    """结论与它的射程一起写：label 自带射程，`scope` 给机器读。

    为什么不能只留一个「已核实」：Task 12 那头的消费端是
    `enabled: status === "verified"` + 档位候选直接吃服务端 `thinking_levels`，
    "配方已核实 + 管理员填三档"在那里就等于**三个点、wire 上一个值**，而且没人会报错。

    `scope.simulated` 是修复轮 2（N1）加的一格：dry-run / 注入替身跑出来的行，与真证据
    在机器可读层原本只差 `live: false` 一格，而 `proved` 那句「这一格的参数形状被这条
    上游接受」对模拟行是不成立的。显式一格 `simulated`，让抄 payload 的人不必自己推断。
    """
    verdict = payload["verdict"]
    payload["verdict_label"] = VERDICT_LABELS[verdict]
    scope = dict(VERDICT_SCOPES[verdict])
    scope["simulated"] = bool(simulated)
    scope["covers"] = ("单条 provider 记录 + 它当时的 base_url + 它当时的 model；"
                       "不是模型级、不是厂商级——换端点或换 model 要重开探针")
    payload["scope"] = scope


def distinct_wire_values(profile, baseline=None):
    """三档折算成**出网体那一格**之后还剩几个不同的值；基线不适用时给 None。

    这是 Important-4 要的"三档同一个出网值"那句事实的机器可读形式：原来它只写在候选表的
    注释和测试里，交接那 4 行一个字不提。表自己畸形（缺档位）时返回 None——
    那种情况后面 `probe_kwargs` 会响亮报错，不该在这里先炸栈。
    """
    if profile == thinking_profiles.NONE_PROFILE:
        return None
    try:
        seen = {json.dumps(thinking_profiles.body_fragment(
                    thinking_profiles.probe_kwargs(profile, lvl)),
                    sort_keys=True, ensure_ascii=False)
                for lvl in thinking_profiles.THINKING_LEVEL_LABELS}
    except (ValueError, KeyError):
        return None
    return len(seen)


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
        description="思考参数核实探针（出站额度 4 次，一次一记；本单授权已用尽）")
    parser.add_argument("--data-dir", required=True,
                        help="provider 配置所在目录（只读；不许指向另一棵 checkout 的 data/——"
                             "判据沿父链上溯找 .git，含 swap-snapshots 那类深层子树）")
    parser.add_argument("--provider", required=True, help="provider_id，如 deepseek-chat")
    parser.add_argument("--profile", required=True, choices=list(thinking_profiles.probe_profile_ids()),
                        help="候选配方名；none = 基线（不发任何思考参数，不代表模型不思考）")
    parser.add_argument("--level", default=None,
                        help="档位标签（%s）；基线不填" % "、".join(thinking_profiles.THINKING_LEVEL_LABELS))
    parser.add_argument("--attempt", type=int, default=None, help="登记的第几次，须与账本序号一致")
    parser.add_argument("--isolation-dir", default=None,
                        help="仓库外临时目录；缺省每次新建一个（所以账本**不再**跟着它走）")
    parser.add_argument("--ledger", default=None,
                        help="出站账本，要一份跨进程稳定的路径。**live 模式必填**：缺省即 rc=2 拒发，"
                             "因为跟着隔离目录走的账本每个进程都是空的，「总额 4 次」永不生效；"
                             "也不许落在 --data-dir 里面、**不许在仓库树内、不许属于另一棵 checkout**"
                             "（修复轮 2，N3：这三条都是预检判据，不再由操作者记得）；"
                             "另注意这道闸按账本文件计数，换路径从 0 重计——路径必须钉死同一份")
    parser.add_argument("--dry-run", action="store_true", help="假 client：零网络、不记账")
    parser.add_argument("--fake", default="reasoning",
                        choices=("reasoning", "quiet", "extra_reasoning", "error_form",
                                 "error_plain", "error_leak"), help="dry-run 演哪一种形态")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                        help="默认 8；live 模式必须 ≤ %d（越出授权形状就拒）"
                             % AUTHORIZED_MAX_TOKENS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return vars(parser.parse_args(argv))


def run_probe(config, client_factory=None):
    """跑一次探针，返回 `(payload, rc, secrets)`。

    `client_factory` 只给测试用：注入一个替身，就能在零网络下走完真实调用那段代码
    （预检 → 隔离 → 重叠判据 → 单例校验 → 记账 → 发 → 读证据 → 判据），CLI 里没有这个开关。
    第三个返回值是那把真 key——只在本进程内存里，`main()` 把它交给出口体检，绝不打印。

    顺序本身就是判据，三处不能换（各自都有会红的反例）：
    **重叠判据先于 `load_backend_modules()`**（import 会写盘），
    **预检先于隔离**（越权的命令行形状连目录都不该开、环境都不该动），
    **`before` 快照先于 `isolate_runtime_env()`**（修复轮 2，N2：晚取一步，
    隔离/import 期间的写盘就永远不在尺子射程里）。
    """
    baseline = config["profile"] == thinking_profiles.NONE_PROFILE
    entry = thinking_profiles.PROBE_PROFILES.get(config["profile"]) or {}
    # 这一跑的"是不是模拟"在入口就定死（修复轮 2，N1）：dry-run 走脚本自带的 FakeClient，
    # 注入 client_factory 走测试替身——两者的行都不许被读成上游事实。真发那一支才是 False
    # （即便 build_live_client 被测试用替身顶掉，那也不是 CLI 能走出来的形状）。
    simulated_run = bool(config["dry_run"]) or client_factory is not None
    # 字段名有条反作用在身上的约束：整份 payload 出门前会再过一次 `scrub_secrets`
    # （`render_and_check`），而它的第一条形状规则连**键名**一起吃——`key_fp`、
    # `key_source` 这种名字会被它当成凭据掩掉，于是干净的结果被误判成泄露、整份拒打
    # （实测踩过）。所以这里回避 `sk_/pk_/key_/api_/secret_/token_/auth_` 开头的键名：
    # 宁可名字绕一点，也不让那道闸在自己的输出上恒红。
    payload = {
        "probe": "thinking_profile", "task": "v0.39.1 Task 11",
        "live": not config["dry_run"],
        # 只在真进入传输那一步才盖章（见下面 `factory` 那一段）：预检被拒、一次都没发时
        # 这里必须留 None，否则与 `live: true` 并读会被当成"发过"。
        "transport": None,
        "provider_id": config["provider"], "profile": config["profile"],
        "level": config["level"], "authorized_live_calls": AUTHORIZED_LIVE_CALLS,
        "attempt": config["attempt"], "call_slot": None,
        "request_shape": {"prompt": DEFAULT_PROMPT, "max_tokens": config["max_tokens"],
                          "stream": False, "timeout": config["timeout"], "max_retries": 0},
        "request_kwargs": {}, "request_body": {}, "sdk_kwarg_check": None,
        # —— 交接物自带的射程（Important-4）：这三格是 Task 12 会直接抄的那一层。
        "level_used": entry.get("level_used"),
        "level_distinct_wire_values": distinct_wire_values(config["profile"], baseline),
        "level_shape": entry.get("shape"),
        "baseline_had_reasoning": None,
        "credential_fp": None, "credential_origin": None,
        "accepted": None, "has_reasoning_content": False, "reasoning_tokens": 0,
        "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
        "evidence_fields": [], "error_class": None, "error_excerpt": None,
        "verdict": None, "verdict_label": None, "scope": None, "isolation_ok": False,
        "runtime_data_untouched": None, "notes": [],
    }
    secrets = []
    scrub = hide = None

    def blocked(note, exc=None):
        """闸拦停：不给结论，只给"为什么没发"——射程那一格也必须写清"一次都没发"。"""
        payload["verdict"] = payload["verdict"] or "preflight_failed"
        stamp_verdict(payload, simulated_run)
        payload["notes"].append(redact(note, secrets, scrub, hide))
        if exc is not None:
            payload["error_class"] = type(exc).__name__
            payload["notes"].append(error_excerpt(exc, secrets, scrub, hide))
        return payload, RC_BLOCKED, secrets

    # —— 闸 0：预检。四条判据全部走在这里，而不是隔离/记账之后：
    #    越权的命令行形状一次都不该发，也不该在磁盘上留下任何东西。
    try:
        data_dir = Path(os.path.abspath(str(config["data_dir"])))
        if data_dir_belongs_to_another_checkout(data_dir):
            raise RuntimeError("--data-dir 的宿主目录是另一棵 checkout（有 .git 却不是本工具这棵树）："
                               "本工具不许对另一个 checkout 运行，主工作树那份 data/ 是现网资产")
        ledger = str(config["ledger"] or "").strip()
        if not config["dry_run"] and not ledger:
            raise RuntimeError("live 模式必须显式给 --ledger（一份跨进程稳定的账本路径）："
                               "缺省的账本会落进本次新建的隔离目录，每个进程都从空账本开始 ⇒ "
                               "「总额 4 次」这道闸永不生效。本次零出站")
        if ledger and overlaps(ledger, data_dir):
            raise RuntimeError("--ledger 不许落在 --data-dir 里面：那是本次声明只读的目录，"
                               "账本每发一次都要写它一笔")
        if ledger and inside_dir(ledger, REPO_ROOT):
            raise RuntimeError("--ledger 必须在仓库外：docstring 那句「账本落在仓库外」从这一版起"
                               "由判据守，不再由操作者记得（修复轮 2，N3）——本仓目录同时是生产机，"
                               "AGENTS.md §1 的受保护运行期资产就在树里")
        if ledger and data_dir_belongs_to_another_checkout(ledger):
            raise RuntimeError("--ledger 落在另一棵 checkout 的树里（沿父链撞上 .git）："
                               "账本与 --data-dir 同一条判据，不许写进别的树")
        if not config["dry_run"] and not 1 <= int(config["max_tokens"]) <= AUTHORIZED_MAX_TOKENS:
            raise RuntimeError(f"max_tokens={config['max_tokens']} 越出授权形状"
                               f"（授权的那一格是 {AUTHORIZED_MAX_TOKENS}，只许等于或小于它、且必须 ≥1）："
                               f"live 模式拒发")
    except Exception as exc:                                    # noqa: BLE001 - 闸没让过就停
        return blocked("预检闸拒绝：命令行形状不在授权范围内，一次都不发", exc)

    # —— 只读证据的 `before` 快照取在**隔离之前**（修复轮 2，N2）：原来它取在 SDK 签名
    #    检查之后（也就是 import providers 之后）——"隔离/import 期间"发生的写盘在结构上
    #    就不在这把尺子的射程里，递归化并没有让它看得见那一刻。`data_dir` 在闸 0 就已定，
    #    不必等隔离。后面的软硬比对见 `readonly_delta_verdict`。
    before = files_digest(data_dir)

    # —— 闸 2：先隔离，**先判重叠**，才许 import providers；再把"会写哪儿"验一遍。
    #    重叠判据必须在 `load_backend_modules()` **之前**（Important-3）：import providers
    #    这一步本身就会跑 `ProviderStore._load()`，缺文件时 `_seed_from_env()` 非空即
    #    `_flush()`，于是 `--isolation-dir <data-dir>/iso` 会在声明只读的 `--data-dir`
    #    **里面**开出两个文件——先 import 再判重叠，等于先把文件写进去再宣布这里不许有文件。
    #    所以这里查两次：显式 `--isolation-dir` 的形状在** mkdir 之前**就拒（连开目录都不许），
    #    缺省自建那一支的落点在隔离之后再验一遍（`mkdtemp` 正常不可能落在 data_dir 里，
    #    但"不可能"不是判据）。反例锁：
    #    `test_overlap_with_the_live_data_is_refused_before_providers_is_imported`。
    try:
        requested_iso = config["isolation_dir"]
        if requested_iso and overlaps(Path(os.path.abspath(str(requested_iso))), data_dir):
            raise RuntimeError("显式 --isolation-dir 与 --data-dir 重叠：在开任何目录之前就拒了")
        iso = isolate_runtime_env(requested_iso)
        if overlaps(iso, data_dir):
            raise RuntimeError("隔离目录与 --data-dir 重叠，拒绝继续（且一次 import 都没做）")
        providers_mod, pathmask_mod = load_backend_modules(iso)
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

    # —— 闸 1：真实调用先记账（dry-run 不进账本，它没花额度）
    #    `--ledger` 在预检那一步已经强制存在（Critical-1）：这里**不再**回退到
    #    `iso / "call-ledger.jsonl"`——那个默认值正是"每个进程从空账本开始"的来源。
    if not config["dry_run"]:
        ledger_is_new = not Path(ledger).is_file()
        try:
            payload["call_slot"] = reserve_call_slot(
                ledger, config["provider"], config["profile"], config["level"],
                config["attempt"])
        except (BudgetExhausted, AttemptMismatch, OSError) as exc:   # noqa: BLE001
            return blocked("账本没让过：这一次不发", exc)
        if ledger_is_new:
            # concerns.1 的残余（审查员判可接受，但要求登记 + 一句提示）：这道闸绑的是
            # "这一份账本文件 ≤4"而不是"这次授权 ≤4"——换 --ledger 路径就从 0 重计。
            # 跨账本累计不修（一次性 4 格探针不值那道工程税），但新建那一跑必须自己说。
            payload["notes"].append(
                "新建账本：这道闸按账本文件计数，跨账本不累计——换一份 --ledger 就从 0 重计；"
                "授权上限 4 次真正的凭据是那本权威账本，路径必须钉死同一份")

    if client_factory is not None:
        factory = client_factory
    elif config["dry_run"]:
        factory = lambda record: FakeClient(mode=config["fake"], leak_value=key)   # noqa: E731
    else:
        factory = lambda record: build_live_client(providers_mod, record,          # noqa: E731
                                                    config["timeout"])
    # 走到这一行才算"进入传输那一步"。上面任何一条闸返回的 payload 都留 transport=None，
    # 免得 `live: true` + `transport: "openai_sdk"` 并读时被当成"已经发过一次"。
    # 修复轮 2（N1）：上一版这一支只判了 `client_factory is None`，把 dry-run 也打成
    # `openai_sdk`——`openai_sdk` 这个值是"这条记录来自真上游"的机械凭据（§R8 第 8 条），
    # 而 Task 12 抄的正是这份 payload，一行模拟数据不许顶着它。三分：注入替身 test_double、
    # 脚本自带 FakeClient 的 dry-run 走 fake_client、只有真 CLI 那一支才是 openai_sdk。
    if client_factory is not None:
        payload["transport"] = "test_double"
    elif config["dry_run"]:
        payload["transport"] = "fake_client"
    else:
        payload["transport"] = "openai_sdk"
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
            payload["baseline_had_reasoning"] = False
        else:
            payload["verdict"] = ("form_unverifiable" if looks_form_unverifiable(exc, message)
                                  else "unverified")
        stamp_verdict(payload, simulated_run)
        payload["notes"].append("失败不重试：这一格的证据就是这条错误本身")
        if readonly_delta_verdict(payload, before, data_dir):
            # 修复轮 2：错误那一支以前只记 flag 不作废——store 三件套被改了也照样 rc=1。
            # 写盘这件事不挑结论，硬判两条路共用同一个函数。
            return payload, RC_BLOCKED, secrets
        return payload, RC_UNVERIFIED, secrets

    payload.update({k: v for k, v in evidence.items() if k != "accepted"})
    payload["accepted"] = True
    payload["verdict"] = classify_verdict(baseline, evidence)
    if baseline:
        # 基线那一格的证据必须自己说话（Important-4 第 3 格的机器可读那一半）：
        # 不发任何思考参数而响应里仍有推理痕迹 ⇒ 界面不许把这一格写成"已关闭思考"。
        payload["baseline_had_reasoning"] = bool(evidence["has_reasoning_content"]) or \
            int(evidence["reasoning_tokens"] or 0) > 0
    stamp_verdict(payload, simulated_run)
    # 软硬两格（concerns.3）：硬（store 三件套变了或多/少路径）作废结论 rc=2；
    # 软（无关既有文件被现网定时器之类的东西重写）只追加点名 note，rc 按原判据走。
    if readonly_delta_verdict(payload, before, data_dir):
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
