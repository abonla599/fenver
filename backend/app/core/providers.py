"""模型服务（Provider）配置。

界面展示的模型、下拉里能选什么、以及真正调用时用的 base_url/密钥/模型名，
全部来自这一份数据。此前它们分散在 main.py 的手写清单、llm_client.MODEL_CONFIGS
和 streaming.MODEL_CONFIG 三处，互不同步，导致"显示 DeepSeek 实际调 GPT"、
"选了没反应的模型"这类问题无法根治。
"""
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from urllib.parse import urlparse

import httpx
from openai import OpenAI

from app.core import credits, net_guard
from app.core import thinking_profiles as _thinking_profiles
from app.core.atomic_write import write_json_atomic
from app.core.pathmask import hide_paths
from app.core.paths import data_root, load_project_env
from app.core.pinned_http import install_pins
from app.core.tls import system_ssl_context

# 回落留痕用的 logger（12B 修复轮 2 · I-R2-4）。名字写死成 "app.core.providers"，
# 与本仓 `core/mcp_client.py` 同一口径；除 debug 级的那一条回落线之外本模块不记任何东西
# ——provider 记录里全是管理员的内部配置，多写一行就多一份泄露面。
logger = logging.getLogger("app.core.providers")

# 本模块可能早于其他组件被导入（app.pipeline 就会），因此自行加载 .env，
# 否则首次运行时读不到 DEEPSEEK_API_KEY、播种不出任何模型配置。
load_project_env()

PLACEHOLDER_HINTS = ("your-", "your_", "xxx", "placeholder", "填入", "待填", "changeme")

# 出口脱敏要认的形状。宁可多隐一些字，也不要漏一条 key：这些字符串同时会进
# logs/backend.log 和管理员看得到的响应体，而日志文件不在任何加密范围内。
_SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|pk|key|api|secret|token|auth)[-_][A-Za-z0-9_.\-]{5,}", re.I),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]+"),
    re.compile(r"(?i)(api[_-]?key|authorization|access[_-]?token|secret)[\"']?\s*[:=]\s*[\"']?([^\s\"',;]{5,})"),
)
REDACTED = "«密钥已隐去»"


def _write_json_atomic(path: str, payload) -> None:
    # 收编进公共实现（tmp → flush+fsync → replace），签名原样保留给本文件内
    # 的 _write_keys/_write_prefs/_flush 三个调用点。
    write_json_atomic(path, payload)


def _own_key_values():
    """服务端自己配过哪些密钥值——形状认不出来时唯一还认得出来的东西。

    读的是进程级那份 store；构造期它还没建好，所以取不到就当没有。
    短于 8 位的不参与：那种值出现在正常报错文案里的概率比它是 key 的概率高，
    隐去它只会让日志读不出所以然。
    """
    try:
        items = store.all()
    except NameError:
        return []
    return sorted({p.get("api_key", "").strip() for p in items
                   if len(p.get("api_key", "").strip()) >= 8}, key=len, reverse=True)


def scrub_secrets(text: str) -> str:
    """把一句要往外说的话里的凭据形状与已配置的密钥值都隐掉。

    先按值替换再按形状替换：值是唯一确定的，形状是猜的——猜的那一层不该把
    已经确认过的东西留下空隙。
    """
    if not text:
        return text
    out = str(text)
    for value in _own_key_values():
        out = out.replace(value, REDACTED)
    for pattern in _SECRET_PATTERNS:
        # 第三条有两组：留下"api_key="这个标签，隐掉冒号后面那串。
        # 写成 group(1) + group(2) 就等于把"隐去"实现成了"原样保留"。
        out = pattern.sub(lambda m: (m.group(1) + REDACTED) if m.lastindex == 2
                          else REDACTED, out)
    return out



class ProviderError(Exception):
    """配置缺失或模型不可用。上层据此返回明确错误，而不是把故障当成回复内容。"""


def _default_path() -> str:
    env_path = os.getenv("PROVIDERS_DB_PATH")
    if env_path:
        return os.path.abspath(env_path)
    return os.path.join(data_root(), "data", "providers.json")


def mask_key(key: str) -> str:
    """掩码是给前端的**唯一**凭据线索，所以它自己不能变成第二个泄露点。

    旧写法把前 3 位与总长度一起给出去（`sk-…3839（35 位）`）：前缀能认出厂商，
    长度能框定爆破面，而"管理员在同一个页面上看得懂哪一把"只需要末 4 位。
    """
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"末四位 {key[-4:]}"



def looks_placeholder(key: str) -> bool:
    """非空但形如占位符的密钥，此前能骗过 `if not api_key` 闸门，是"切换模型没反应"的根因。"""
    if not key or not key.strip():
        return True
    lowered = key.strip().lower()
    return any(h in lowered for h in PLACEHOLDER_HINTS)


# ---------- base_url 的出网闸门（SSRF）----------
# v0.33。`base_url` 此前唯一的校验是 startswith(("http://", "https://"))，于是
# `http://169.254.169.254/`、`http://localhost:8000/`、`http://10.0.0.5/` 全过。
# 两条危害，第二条比第一条重：
#   1. SSRF——`POST /v1/providers/{id}/test`（store.ping）会真发一次请求并把
#      异常回显给管理员，是一条能把内网当扫描器用的读口。
#   2. 凭据外泄——**全局** provider（owner 为空、站级默认那条）的 base_url 被改成
#      攻击者的地址后，此后每一个走默认 provider 的用户每轮聊天，都会把管理员的
#      api_key 作为 `Authorization: Bearer` 发给攻击者。改一次配置，赔上全站的钱。
#
# 为什么是白名单式的"这段网段不许"而不是 `ip.is_global`、CIDR 清单里都有什么、
# IP 字面量快路与域名解析两支各自的历史缺口——都写在 app.core.net_guard 顶部。
# v0.37.0 起判据整体住进 net_guard（供多条出站路径共用，下一批的 http_fetch 也
# 长在同一个口径上）；这里只剩 URL 层：拆主机名、空壳检查，以及**调用时**从模块
# 属性取 `_resolve_addresses` 和 `ALLOW_LOCALHOST_BASE_URL` 递给 net_guard——
# 写成默认参数会在 def 时冻结引用，conftest 的 autouse DNS 桩和逐用例的
# monkeypatch 会一起失效，v033/v0342 两个文件大面积红。
# 端口限制、https、userinfo、尾点/IDN、重定向不在 net_guard：provider base_url
# 允许自定义端口，那是访问策略与配置校验的边界，net_guard 只管地址形状。
_PRIVATE_NETWORKS = net_guard.PRIVATE_NETWORKS  # 历史别名：既有引用与测试文案认这个名字

# 「localhost 放行」做成开关而不是写死：确实存在把模型自建在本机/内网直连的部署
# （http://localhost:11434/v1 那种 Ollama），一刀切拒掉会把它们连配置都保存不了。
# 但默认必须是**拒**——这个开关翻成 True 的代价是管理员本人就能把全站默认上游
# 指到回环，而默认这条路的危害是上面第 2 条（每次聊天送出一把 api_key）。
# 想开的人应当是知道自己在内网自建模型的那少数部署，而不是随手一个布尔。
ALLOW_LOCALHOST_BASE_URL = False


def _resolve_addresses(host: str) -> list:
    """主机名 → 地址字面量列表（去重、稳定排序）。

    单独拎成一个模块级函数，唯一目的就是当**注入点**：测试用 monkeypatch 换成
    固定答案，于是整套测试一次都不真的走 DNS（这台机器同时是生产机，真解析既慢、
    又随网络与 hosts 文件变，红起来查不出是产品坏了还是机器没网）；生产走真
    `socket.getaddrinfo`。判据本身不在这个函数里——它只负责"问到什么"，
    对不对由 `net_guard.reject_reason` 判，这样两侧各自也只有一处口径。
    """
    infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    return sorted({info[4][0] for info in infos})


def _base_url_reject_reason(base_url: str) -> str:
    """这个 base_url 能不能让服务端替它出网。返回 "" = 放行，否则是**对外**理由。

    判据全在 net_guard（清单、判定顺序与详细句式都在那边）；本函数只剩 URL 层：
    拆主机名、空壳检查，以及把 net_guard 的详细理由过一遍
    `public_reject_reason`。详细理由里有解析出的地址、命中的 CIDR、zone-id 值与
    主机名——它经 `ProviderError` 会原样进普通用户的 400 响应，逐字回显就是一台
    内网 DNS oracle。所以那层细节只留在内部（直接调 `net_guard.reject_reason`
    排障/审计），这里对外只给类别。

    本函数是**写入时**的校验（唯一调用点是 `ProviderStore._validate`），只回答
    "这个 base_url 该不该被保存"。真正出站的那条路（`build_client`）走
    `_resolve_and_check`：写入与出站各解析一次，写入那次的结果不会传给连接层。
    `_resolve_and_check` 关掉的是"出站判闸 → 连接"这一次——解析一次，同一份
    地址列表既判闸又钉进连接层（DNS rebinding / TOCTOU 的窗口在那里，不在两次
    解析之间；TTL 到期后重建客户端会重新解析、重新判闸）。
    """
    try:
        host = urlparse(base_url).hostname
    except ValueError:
        return "base_url 解析失败"
    if not host:
        return "base_url 里没有主机名"
    # resolve / allow_loopback 必须在**调用时**从模块属性取（裸名字即可：函数体
    # 里的全局名每次执行都重新解析）——这正是 conftest autouse 桩与逐用例
    # monkeypatch 能生效的原因；net_guard 拿到的只是这一次调用的引用。
    # 详细理由只在这一层内部使用：跨出这一层的只有公开类别。
    detail = net_guard.reject_reason(
        host,
        resolve=_resolve_addresses,
        allow_loopback=ALLOW_LOCALHOST_BASE_URL,
        where="base_url",
    )
    return net_guard.public_reject_reason(detail)


def _resolve_and_check(base_url: str) -> tuple:
    """解析一次、判闸一次，返回 `(host, addresses, public_reason)`。

    与 `_base_url_reject_reason` 同一套判据（同一个 net_guard 调用、同一个
    `_resolve_addresses` 注入点），区别是这里把**解析出来的那份地址列表**也交
    出去：`build_client` 把它原样钉进连接层，于是"判闸看到的地址"与"连接用的
    地址"是同一次解析的结果，中间没有第二次 DNS 查询——DNS rebinding / TOCTOU
    的窗口就关在这一次解析上。对外理由仍是 `public_reject_reason` 的类别（M-4）。

    两条快路不碰 DNS（与 net_guard 一致）：IP 字面量的地址就是它自己；
    localhost 名字由 net_guard 按开关判，开关关着时下面先拒，走不到取地址那步。
    解析失败/结果为空由 net_guard 折成 fail-closed 的理由，行为与旧写法一致。
    """
    try:
        host = urlparse(base_url).hostname
    except ValueError:
        return "", [], "base_url 解析失败"
    if not host:
        return "", [], "base_url 里没有主机名"

    try:
        literal = str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        literal = None
    if literal is not None:
        detail = net_guard.reject_reason(
            host, resolve=lambda _h: [literal],
            allow_loopback=ALLOW_LOCALHOST_BASE_URL, where="base_url")
        return host, [literal], net_guard.public_reject_reason(detail)

    resolved = []

    def _resolve_once(_host):
        # net_guard 对域名只调一次；这里再兜一层"只解析一次"，别让今后的改动
        # 在判闸循环里重复问 DNS。
        if not resolved:
            resolved.extend(_resolve_addresses(host))
        return resolved

    detail = net_guard.reject_reason(
        host, resolve=_resolve_once,
        allow_loopback=ALLOW_LOCALHOST_BASE_URL, where="base_url")
    if not detail and not resolved:
        # 只有 localhost 这一支会到这里：net_guard 的快路不解析，而连接层必须
        # 有一个可连的地址。能走到这里说明开关放行了"本机"这一类。
        try:
            resolved.extend(_resolve_addresses(host))
        except Exception:                    # noqa: BLE001 - 解析失败一律不放行
            return host, [], net_guard.public_reject_reason(
                f"base_url 的主机名 {host} 解析失败")
        if not resolved:
            return host, [], net_guard.public_reject_reason(
                f"base_url 的主机名 {host} 解析结果为空")
    return host, list(resolved), net_guard.public_reject_reason(detail)


def _pin_key(base_url: str) -> str:
    """连接层实际查询的主机名键：httpx 的 `raw_host`（IDNA / ASCII bytes）。

    httpcore 不用 `urlparse(...).hostname`：httpx 交给它的是 URL 经 IDNA 编码的
    `raw_host`，httpcore 再 `decode("ascii")` 后作为 `connect_tcp(host=...)`。
    pin 键必须是这同一个值——Unicode 主机名（如 `例子.测试`）若按 `urlparse`
    的 Unicode 形式钉、连接层却按 `xn--...` 查，就会被判成"没有钉住"而拒绝，
    合法 IDN provider 从"可用"变成"全挂"。

    与 net_guard 各取所需：判闸用的是用户写的原始主机名（Unicode 照旧，解析与
    公开理由口径不变），这里只算连接层查询用的 ASCII 键。解析失败一律
    fail-closed，公开文案不带主机名（不替调用方回显输入）。
    """
    try:
        key = httpx.URL(base_url).raw_host.decode("ascii")
    except Exception:  # noqa: BLE001 - httpx 认不出的 URL 不许建立连接
        raise ProviderError("base_url 解析失败") from None
    if not key:
        raise ProviderError("base_url 解析失败")
    return key


# 模型档案里"上下文上限（K token）"的兜底值：没填的条目按 64K 算。
# 定成 64 而不是更大：它是"界面敢让你把预算拉到多远"的闸门，虚高的代价是
# 越界报错（上游 4xx），偏低的代价只是少带几轮历史——选边明显。
DEFAULT_MAX_CONTEXT_K = 64
MAX_CONTEXT_K_CEIL = 10000

def normalize_max_context_k(value) -> int:
    """把任意输入洗成 1..10000 的整数 K；None/空/非法 → 缺省 64。

    写路径（_validate）与读路径（catalog/_public 对老记录的兜底）共用这一个口径，
    别各写一份——两处数字对不上时，界面封顶和实际预算会静默分叉。
    """
    if value is None or value == "":
        return DEFAULT_MAX_CONTEXT_K
    try:
        k = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONTEXT_K
    return max(1, min(MAX_CONTEXT_K_CEIL, k))


# ---------- 思考能力（v0.39.1 Task 8；登记表自 Task 12A 起住在 thinking_profiles）----------
# "这个模型能不能思考"和"选了某档之后到底往上游发什么参数"是两件事。本版仍然只有
# `none`（不带任何思考参数）：7 次真实探针调用之后没有任何配方够格进登记表，所以
# supports=True 的条目状态只能到 unverified——界面据此显示"支持思考（参数未核实）"。
# Task 12A 交付的是**登记机制**：`verified` 从此绑在「配方 + 这条记录的 host+model」
# 两格上（`_thinking_status`），`level_count` 从此约束档位条数（`_thinking_fields`），
# 出网参数只有一个写者（`thinking_profiles.build_thinking_kwargs`）。以后往表里加条目
# 的人不需要改这里的判据——判据已经在，漏写 `verified_for` 只会得到 unverified。
DEFAULT_THINKING_PROFILE = _thinking_profiles.DEFAULT_PROFILE
# **必须是别名，不是拷贝**：既有测试是就地 `monkeypatch.setitem(providers.THINKING_PROFILES, …)`
# 塞 synthetic 条目的（test_v0391_provider_capabilities.py:318/:349/:351）。抄一份就变成
# "测试改的那份没人读、判据读的那份没人改"，那几条锁当场失牙变成恒绿。
# 正面锁在 test_v0391_thinking_mapping.py §12：`providers.THINKING_PROFILES is
# thinking_profiles.PROFILES`。
THINKING_PROFILES = _thinking_profiles.PROFILES
MAX_THINKING_LEVELS = 3
THINKING_LEVEL_LABEL_MAX = 16

# 写路径合并与校验之间唯一的名单：这四格「这次没带」与「这次填了个空值」必须可区分。
_THINKING_KEYS = ("supports_thinking", "thinking_levels",
                  "thinking_default", "thinking_profile")


def _requested_thinking_keys(record: dict) -> set:
    """请求里**真正带来**的那几格思考键。

    判据与 `_pending` 的合并判据是同一条（键缺失或值为 None = 这次没动它），所以
    只写一处：显式 `False` / `[]` / `""` 都是用户真填的值，照样算"带来了"。
    """
    return {k for k in _THINKING_KEYS if record.get(k) is not None}


def _clean_levels(value) -> list:
    """把任意输入洗成思考档位标签列表：去空白、去空、去重，**不截断**。

    截不截断是两个职责，不在这里判：写路径（`_thinking_fields`）清洗后看到 >3 个
    且那 4 个是请求带来的必须拒收，读路径（`_thinking_view`）才 `[:3]` 兜老数据。
    悄悄截掉第 4 档等于把"没配上"演成"配上了"；读的时候报错则会让畸形老记录在
    界面上凭空消失。
    """
    if value is None:
        return []
    seq = value if isinstance(value, (list, tuple)) else []
    out = []
    for x in seq:
        t = str(x or "").strip()[:THINKING_LEVEL_LABEL_MAX]
        if t and t not in out:
            out.append(t)
    return out


def _record_host(record: dict) -> str:
    """这条记录的 hostname（小写），登记表比对用的那一个坐标。

    记录里存的是 `base_url.rstrip("/")` 过的整条 URL（例如
    `https://api.deepseek.com/v1`），而 `verified_for` 里写的是**主机名**：换 scheme
    或换 path 不该让一次核实作废，换 host 或换 model 才该。反过来用整串比对或
    子串匹配都不行——前者会把已核实的事实洗掉，后者会让 `https://evil/api.deepseek.com`
    这种地址亮起来。取不到主机名（老记录里根本不是 URL）返回空串，判据那边据此降级。
    """
    return urlparse(str((record or {}).get("base_url") or "")).hostname or ""


def _verified_scope_hit(pairs, host, model) -> bool:
    """`verified_for` 命中判据：host 与 model **同时**在场且成对命中。

    三条硬规矩（M1，逐条有反例锁）：
    - 缺参就降级：`host` 或 `model` 为空 ⇒ 一律不命中。忘记传参的那一方必须输得安全，
      不能输成"亮"。
    - 空清单 / 缺这一格 / 不是清单 ⇒ 不命中。**"空清单"绝不等于"不限"**，那正是
      "登记一条、全站变绿"那句假话的入口。
    - host 小写比对（`urlparse().hostname` 本就小写，登记表里手写大小写混着不该作废）；
      model 逐字比对（model 名在真调用里是大小写敏感的标识符，不做模糊化）。
    """
    h = str(host or "").strip().lower()
    m = str(model or "").strip()
    if not h or not m:
        return False
    for pair in (pairs or []):
        if not isinstance(pair, dict):
            continue
        ph = str(pair.get("host") or "").strip().lower()
        pm = str(pair.get("model") or "").strip()
        if ph and pm and ph == h and pm == m:
            return True
    return False


def _thinking_status(supports: bool, profile: str, host=None, model=None) -> str:
    """给前端的三态结论：不支持 / 支持但参数未核实 / 支持且参数**在这条记录上**已核实。

    不重新判 supports 的来源，只把"supports + profile + 这条记录的 host/model"合成
    一句人话——合成口径只在这一个函数里，两个展示面共用，别各写一份。

    `verified` 一支的成立条件**四者全齐**（M1，缺任何一个都回落 unverified）：
    ① `supports` 为真；② `profile` 不是默认配方；③ 登记表里有这一条且 `verified` 为真；
    ④ `entry["verified_for"]` 里存在一项 `host` 与 `model` 同时命中**这条记录**。
    第 ④ 格是本版新增的全部理由：`thinking_profile` 是每条记录各有一格的字段，
    只绑配方名的话，将来任何一条 `verified` 条目一登记，**所有**填了该配方的记录
    都会一起亮——包括探针从没打过的那个端点、那个 model。探针自陈的射程是
    「单条 provider 记录 + 它当时的 base_url + 它当时的 model」，判据必须和它一样窄。

    "核实"这个结论只认登记表那一格自己说的 `verified`，别把"profile 不是 none"
    当成核实：盘上/历史里出现过一个谁都没验过的 profile 时，界面会说谎。
    `DEFAULT_THINKING_PROFILE` 必须先单独排除——`none` 在表里也标了 verified: True
    （"不带参数"这件事本身是确定的），但它不等于"思考参数已核实"，它也没有档位可言。

    回落一律是 unverified，**不是** unsupported："这条记录没被核实到"与"这个模型不会
    思考"是两件事，别把前者演成后者。
    """
    if not supports:
        return "unsupported"
    if profile == DEFAULT_THINKING_PROFILE:
        return "unverified"
    entry = THINKING_PROFILES.get(profile)
    if entry is None or not entry.get("verified"):
        return "unverified"
    if _verified_scope_hit(entry.get("verified_for"), host, model):
        return "verified"
    return "unverified"


def _thinking_view(p: dict) -> dict:
    """读路径的思考视图四格（supports/levels/default/status）——单源，两个出口都走它。

    与写路径的分工照旧：这里只做防御性兜底，拒收是 `_thinking_fields` 的职责。
    抄两份的下场见上面 `normalize_max_context_k` 那段：两处数字对不上时没人会报错，
    只有界面和实际预算悄悄分叉。
    """
    view = {
        "supports_thinking": bool(p.get("supports_thinking")),
        # 读路径才截断：盘上可能有别的版本/手工写坏的畸形老记录，这里报错会让那些
        # 模型在界面上凭空消失。
        "thinking_levels": _clean_levels(p.get("thinking_levels"))[:MAX_THINKING_LEVELS],
        # default 与 levels 用同一把尺子量（同一个 20 字串不该当档位收、当默认档拒）
        "thinking_default": str(p.get("thinking_default") or "").strip()[:THINKING_LEVEL_LABEL_MAX],
        # 记录作用域（M1）：核实结论要绑到"这条记录的 hostname + 这个 model"上，两个坐标
        # 由**调用方**算好传进去（host 不在记录的独立字段上，只在 base_url 里）。
        # 对外视图的键一个字都不许多给：catalog 与 _public 的键集有锁
        # （test_v0391_native_cockpit.py / test_v0391_web_cockpit_ui.py）。
        "thinking_status": _thinking_status(bool(p.get("supports_thinking")),
                                            str(p.get("thinking_profile")
                                                or DEFAULT_THINKING_PROFILE),
                                            _record_host(p), str(p.get("model") or "")),
    }
    if not view["supports_thinking"]:
        # 库里自洽的那一份必然空着；读到"关着思考却带着候选"说明是手工写坏的，
        # 界面照画就成一个永远点不动的选项。
        view["thinking_levels"] = []
        view["thinking_default"] = ""
    elif view["thinking_default"] not in view["thinking_levels"]:
        # 截掉第 4 档、或 default 本来就指着不存在的那一档：归回候选，
        # 别把"选中值不在候选里"甩给界面去兜。
        view["thinking_default"] = view["thinking_levels"][0] if view["thinking_levels"] else ""
    return view


def _thinking_fields(record: dict, requested=None) -> dict:
    """写路径的思考四格：清洗 + 拒收非法组合，返回库里该存的那一份。

    `requested` 是**这次请求真正带来的那几格**（由 `ProviderStore._pending` 一并交出来）。
    严格判据只打在请求带的值上，从库里补进来的继承值只做宽容归一、永不 raise：
    补进来的那份不是用户此刻的意图，拿它判越界等于让一次改名、一次显式关思考
    统统存不下去。宽容也不等于丢掉不变式——归一之后的结果照样自洽，库里存的
    那一份永远合法。

    判序是刻意的：先 `profile` 白名单、再通用的「最多 3 档」、然后**先看 supports**：
    关着思考就走清空那一条（不判配方声明的 `level_count`，M-R1），开着才判数量相等、
    最后判 default 与候选的组合。一个不认识的值摆在面前时，第一句该说的是"这个值我不认"，
    而不是"你不支持思考"；而一次"关掉思考"的保存里，档位清单该被清空，不该被审判。

    关掉思考（显式 `supports_thinking=False`）时档位与默认**清空**而不是报错：
    "我不再用这个模型的思考"是一次真实的用户动作，不该因为表单里还留着上一次的
    候选而被挡住；但这次请求亲手带着非 `none` 的参数配方还要关掉思考才是矛盾输入，
    拒收。
    """
    brought = _requested_thinking_keys(record) if requested is None else set(requested)
    supports = bool(record.get("supports_thinking"))
    profile = str(record.get("thinking_profile") or DEFAULT_THINKING_PROFILE).strip()
    if profile not in THINKING_PROFILES:
        if "thinking_profile" in brought:
            raise ProviderError("未知的思考参数配置")
        profile = DEFAULT_THINKING_PROFILE        # 继承来的野值：归一，不挡保存
    levels = _clean_levels(record.get("thinking_levels"))
    if len(levels) > MAX_THINKING_LEVELS:
        # M-3（12B 修复轮 1，主控已裁决：**维持拒收**，不把这一闸挪到 supports 支之后）。
        # 结论与理由，写给下一个人，别再当"半开的边界"重新裁决一次：
        # `MAX_THINKING_LEVELS` 是**存储形状不变量**（库里一份清单最多 3 格），判的是
        # "这份数据能不能被存成一个自洽的形状"，与"这条记录会不会思考"无关——所以它摆
        # 在 supports 判据之前是**对的**，不是遗漏。真正与 supports 有关的那一闸是配方声明
        # 的 `level_count`（下面 supports=False 一支的 M-R1 注释），那一半已经按裁决挪走了。
        # 不能一起挪的实验事实（审查员 12B §5 变异 E，仓库外克隆实测）：整块搬进
        # supports=True 那一支会**同时**红既有锁
        # `test_v0391_provider_capabilities.py::test_invalid_capabilities_are_rejected[bad0]`
        # （`:40-45` 那份基座没有 `supports_thinking`，supports 落 False 却带 4 档）与
        # 本版的 `test_the_generic_level_cap_is_still_judged_when_thinking_is_off`
        # ⇒ 挪它等于放宽 Task 8 留下的既有断言，明令禁止。
        # 代价说清楚（12B 修复轮 2，审查 M-R2-1：上一版那句"UI 永远发不出 >3 档"是假话，
        # 现在按实测写成事实）：这一墙**从界面就是可达的**。设置页的档位框是自由文本
        # （`app.js:1874` 把清单 `join("，")` 写进输入框），`app.js:1554-1560` 的
        # `parseThinkingLevels` 只做去空白/去重、**没有条数上限**，`app.js:1914` 原样提交，
        # 而 `main.py:1460` 的 `thinking_levels: Optional[list[str]]` 没有 `max_length`
        # ⇒ 管理员在模型卡里打四个标签、再取消勾选"支持思考"，这次 PUT 就带着 4 档走到这里，
        # "关掉思考 + 4 档"不是什么只有直连 API 才撞得到的形状。
        # 但拒收的裁决与上面三条理由一个字不动：撞到这一墙的人收到的是"这个形状存不下"
        # 那句 400，不是"你不支持思考"——归因仍然没坏。剩下那半件（界面在提交前就提示上限、
        # 关思考时顺带清档位）是 **Task 13** 的活，本单按硬约束不动 `app.js`。
        if "thinking_levels" in brought:
            raise ProviderError(f"思考档位最多 {MAX_THINKING_LEVELS} 个")
        levels = levels[:MAX_THINKING_LEVELS]     # 继承来的畸形档位：与读路径同口径截 3
    default = str(record.get("thinking_default") or "").strip()[:THINKING_LEVEL_LABEL_MAX]
    if not supports:
        # M-R1（从 12A 审查继承的验收项）：**先看 supports**。关掉思考是一次真实的
        # 用户动作，档位与默认按 Task 8 裁决清空，不判 `level_count`——旧写法把档位闸
        # 排在这一步之前，于是"关掉思考"的那一次 PUT 若顺手带着一份与配方数量不符的
        # 清单，会被"审判"而不是被"清空"。今天不可达（表里只有 `none`，它不声明
        # level_count），但形状必须现在就对：两侧都有锁（关掉+数量不匹配 → 通过并清空；
        # 开着+同样这份清单 → 拒收），见 test_v0391_provider_capabilities.py 那两格。
        levels, default = [], ""
        if profile != DEFAULT_THINKING_PROFILE:
            if "thinking_profile" in brought:
                raise ProviderError("不支持思考的模型不能配置思考参数")
            profile = DEFAULT_THINKING_PROFILE
    else:
        # 档位条数必须等于这一配方**出网的不同取值数**（M2）。判据只对"请求真正带来的
        # 那一格"生效，与上面 思考档位最多 3 个 同一口径：继承来的畸形值永远不许抛
        # （Task 8 裁决 A 的遗产）。这件事上**真的会红**的锁按名单点名（文件 =
        # test_v0391_provider_capabilities.py；行号是 12A 修复轮 1 的现场坐标、会漂，认锁
        # 以名字为准；两枚变异均在仓库外克隆实测还原，那一轮的本行还在 supports 判据之前）：
        # 摘本行 brought 门 ⇒ `test_inherited_levels_never_hit_the_level_count_gate`（:450）
        # 独红；摘本行 `level_count is not None` 守卫 ⇒
        # `test_request_brought_invalid_values_are_still_rejected`（:300）、
        # `test_level_count_is_the_wire_value_count_and_only_judges_brought_levels`（:418）、
        # `test_update_levels_without_default_renormalises_inherited_default`（:235）
        # 等一批红。两枚变异下都不红的正是旧注释点名的
        # `test_malformed_legacy_record_accepts_three_writes`（:267，审查员 12A M-R2）——别再指它。
        # 为什么是**拒收**而不是截断：悄悄砍掉第 3 档等于把"没配上"演成"配上了"——
        # 布尔配方三档同一个出网值（g2 实测）时，界面给三个档而线上只有一个值，
        # 用户点第 3 档什么也没变。这里选择响亮地拒绝，让它去改成 1 档。
        # `level_count is None`（含 `none`）不判这一条：今天不可达，但两侧都有锁
        # （synthetic 布尔条目填 3 档被拒、填 1 档通过），不许它恒真。
        level_count = (THINKING_PROFILES.get(profile) or {}).get("level_count")
        if (level_count is not None and "thinking_levels" in brought
                and len(levels) != level_count):
            raise ProviderError(f"思考档位数量必须与该配方出网的不同取值数一致（应为 {level_count} 个）")
        if default not in levels:
            # 只有用户此刻亲笔填的那个值才配得上一句拒收；空 default（没填或主动清空）
            # 和继承来的旧默认越界一样，归到候选第一个，没有候选就是空串。
            if default and "thinking_default" in brought:
                raise ProviderError("思考默认档位不在候选里")
            default = levels[0] if levels else ""
    return {
        "supports_thinking": supports,
        "thinking_levels": levels,
        "thinking_default": default,
        "thinking_profile": profile,
    }


# ---------- 请求侧（v0.39.1 Task 12B）：档位标签 → 出网序号，唯一的换算点 ----------
#
# 这一族只在**读/请求**侧判，不写库：写路径的判据是 `_thinking_fields`（拒收非法组合），
# 请求路径的判据是"这条记录此刻能不能吃这个档位"。两份判据共用同一份 `_thinking_view`
# 与同一张登记表，所以界面亮绿的那一格和真正出网的那一次永远不会分叉。
THINKING_REQUEST_REJECTED = "该模型当前不支持所选的思考档位"


def thinking_request_index(record, label):
    """把客户端送来的**档位标签**换算成 `build_thinking_kwargs` 要的**序号**。

    返回值三种：`None` = 这次不发思考参数；`1..level_count` = 出网第 N 个不同取值；
    其余情况一律 `ProviderError`，detail 是 `THINKING_REQUEST_REJECTED` 那一句固定的话——
    **不回显这条记录的能力字段**（档位清单、配方名、上限都不许出现在错误里，否则一次
    探测就能读出这条记录的思考配置，而那是管理员的内部设置）。

    四条判据（B1/B2，缺一个就是让"未核实就不发"变成一句希望）：

    1. 缺省与空串都不发。空串**不当成"第 0 档"**：档位清单是管理员自填的字符串，
       `_clean_levels` 在写路径就把空串去掉了，把它读成序号 0 只会撞上
       `build_thinking_kwargs` 的下界，把一个"用户没填"演成一个 500。
    2. 判据只有一份：走 `_thinking_view`（它内部就是带 host+model 的 `_thinking_status`），
       不在调用方另抄。host 取 `urlparse(base_url).hostname`，取不到就按"不匹配"走
       unverified，不猜。
    3. 标签必须在这条记录自己的 `thinking_levels` 里——清单为空时任何标签都拒收，
       因为那时服务端一个可选档位都没有，放行等于凭空发明一档。
    4. 换算出的序号必须真发得出去：拿 `build_thinking_kwargs` 原地预检一次。库里那份
       清单可能比配方声明的出网取值数长（M2 只挡写路径，`_load` 不跑 `_validate`，
       盘上手写坏的老记录照样读得进来），此时越界的那一格必须是 400，不能是 500。
       这也是"请求路径永远不把序号推到越界"这条要求的实现处——判据不复制，
       而是问唯一的写者本身。
    """
    text = str(label).strip() if isinstance(label, str) else label
    if not text:
        return None
    view = _thinking_view(record or {})
    if view["thinking_status"] != "verified":
        raise ProviderError(THINKING_REQUEST_REJECTED)
    levels = view["thinking_levels"]
    if text not in levels:
        raise ProviderError(THINKING_REQUEST_REJECTED)
    index = levels.index(text) + 1
    profile = str((record or {}).get("thinking_profile") or DEFAULT_THINKING_PROFILE)
    try:
        _thinking_profiles.build_thinking_kwargs(profile, index)
    except ValueError:
        raise ProviderError(THINKING_REQUEST_REJECTED) from None
    return index


# 回落留痕（12B 修复轮 2 · I-R2-4）。"这条记录没被核实到 / 这个序号在它此刻的清单里越界"
# 过去是一次完全静默的回落：用户点了档位没反应，事后无人能复盘。留痕只许 debug 级，
# 字段按裁决逐字收紧成 **provider id + thinking_status + 序号 + 这一句固定文案**——
# 档位清单原文、配方名、base_url 全文、模型名、任何凭据都不许进来（那一份记录里全是
# 管理员的内部配置，`_thinking_view` 与 `catalog` 的对外字段名单正是同一道边界）；
# 写之前照本仓老规矩再过一层 `scrub_secrets` → `hide_paths`，认不出来的形状由它们兜。
# 文案本身不含任何记录内部信息，所以它同时是测试挑出这一行的锚点（改名等于缴械）。
THINKING_FALLBACK_PHRASE = "思考档位未发送"


def _thinking_fallback_note(record, index, status, kind) -> str:
    """拼那一行留痕：只给四个允许字段，越界用 kind 标记而不是猜一个"最大档"。"""
    return (f"{THINKING_FALLBACK_PHRASE} reason={kind} provider="
            f"{str((record or {}).get('id') or '-')} status={status} index={index}")


def _log_thinking_fallback(record, index, status, kind) -> None:
    logger.debug(hide_paths(scrub_secrets(_thinking_fallback_note(record, index, status, kind))))


def thinking_kwargs_for(record, index) -> dict:
    """四个出网站点唯一的合并入口：`index` 为 None ⇒ 一个字节都不加。

    **这一格自己重判记录作用域**（12B 修复轮 1，审查 I-1）。序号是调用方
    （`_prepare_chat` → `thinking_request_index`）按那条记录换算出来的，但流式那条路
    在生产者线程里还会**再解析一次** provider（`main.py:938-946` → `streaming.py:206`
    的 `store.resolve(provider_id, ...)`），那一下比请求校验点（`main.py:699`）**晚**：
    两次解析之间一次管理员 PUT 就能把这条记录换成"同一个在册且 verified 的配方、但这条
    host+model 从没进过它的 `verified_for`"。所以"已核实"这句话必须在发送点再说一遍，
    不能采信调用方带过来的序号是哪条记录换的——那正是 M1 那句假话（把一次逐条观察演成
    全局结论）的出口。非流式两支（`pipeline.py:205`、`core/llm_client.py:37`）同样是
    "校验在前、发送在后"的两次解析，这道门对三条发送路都生效。

    判据只有一份：走 `_thinking_view`（12A 那把唯一尺子，内部就是带 host+model 的
    `_thinking_status`），`thinking_status != "verified"` 直接返回 `{}`。**不在这里另拼
    一份 status**，也**不接受调用方传进来的"已 verified"标记**——多一个入参就是多一条
    "调用方自己说了算"的通道，与 B1 换算点只此一处同源。

    作用域判完还要**用这条记录此刻的清单重判序号上界**（12B 修复轮 2，审查 I-R2-3，
    主控裁"要重判"）：序号是在第一次解析时换算的，管理员在那之前之后把 `thinking_levels`
    从 3 档改成 1 档，旧的 `thinking_index=3` 就指向一条**这条记录没有的**档位。
    这与 I-1 是同一类形状，不留到"以后"。越界一律 `{}`——fail-closed：**宁可这一轮不发
    参数，也不发一个指向错档位的值**；不许回退成"取最后一档"那种猜测，也不许 raise
    （raise 会把一次配置改动演成 500）。`build_thinking_kwargs` 只看**配方**声明的
    `level_count`、看不见这条记录的清单，所以这一判只能在发送点做。

    这一判的上界是**这条记录清单的长度**，不是配方的 `level_count`，也不是标签本身——
    把话说到底（整枝审查 I-2）：它挡得住"清单变短了、旧序号落在清单外"，挡不住
    "清单长度没变、内容重排了"。`[快, 慢]` 改成 `[慢, 快]` 之后，旧的 `index=1` 仍在界内，
    指的却是另一个标签。要关掉这一残余形状，得让**标签**跟着走到发送点，而本批故意没有
    那么做（换算点只有一处、出网只认序号，见 `thinking_request_index` 第 4 判）。
    所以这里只承诺长度上界，不承诺同一性。

    最后一道是**组装本身也不许把异常送到出网路径上**（整枝审查 I-3）：继承支（`_pending`
    替没带 `thinking_levels` 的 PUT 补库里那份，M2 那一闸只判请求带来的格子）能存下
    "清单 3 档 / 配方声明 `level_count=1`"这份组合，长度上界看得见它、配方看不见，
    于是 `build_thinking_kwargs` 在这里会 `ValueError`——四个站点都在 `kwargs.update(...)`
    里裸调这一格，非流式两支会把它直接演成 500，违背的就是上面那句"不许 raise"。
    现在按同一口径收：配方拒收 ⇒ 留痕 `reason=recipe_rejected` 并回 `{}`。

    两次回落都不是静默的（I-R2-4）：各留一行 debug，字段只有 provider id、status、
    序号与一句固定文案，见 `_log_thinking_fallback`。
    组装仍交给 `build_thinking_kwargs`：登记表里没核实过的配方它自己 raise，`none` 它自己
    回 `{}`，所以今天（表里只有 `none`）这四条路永远收到空字典。
    """
    if index is None:
        return {}
    view = _thinking_view(record or {})
    status = str(view["thinking_status"])
    if status != "verified":
        _log_thinking_fallback(record, index, status, "scope")
        return {}
    if index > len(view["thinking_levels"]):
        _log_thinking_fallback(record, index, status, "ordinal_out_of_range")
        return {}
    profile = str((record or {}).get("thinking_profile") or DEFAULT_THINKING_PROFILE)
    try:
        return _thinking_profiles.build_thinking_kwargs(profile, index)
    except Exception:
        # 只吞这一格的装配失败，且吞完必须留痕：出网路径的裁决是"不发参数"，不是"报错"
        # 更不是把一次配置改动演成 500（见上面 I-3 那段）。
        _log_thinking_fallback(record, index, status, "recipe_rejected")
        return {}


# 常见 OpenAI 兼容端点，仅作为新增表单的预填模板，用户可改
PRESETS = {
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
                 "model": "deepseek-flash", "supports_vision": True,
                 # 官方 v4 系（flash/pro）窗口 1M token（2026-09 用户核实）
                 "max_context_k": 1000},
    "dashscope": {"label": "阿里云百炼 Qwen", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "supports_vision": False},
    "dashscope-vl": {"label": "阿里云百炼 Qwen 视觉", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-vl-max", "supports_vision": True},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4o", "supports_vision": True},
}


def _seed_from_env() -> list:
    """首次运行时用 .env 里已有的密钥建一条 DeepSeek，避免升级后无模型可用。

    这里也是 DEEPSEEK_BASE_URL 第一次真正被代码读取的地方——此前它写在 .env 里
    却没有任何代码引用，用户改了也不生效。
    """
    key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if looks_placeholder(key):
        return []
    # deepseek-flash 是 api.deepseek.com 现在列出的、实测能吃图的那一个
    # （2026-09-20 用真截图验过：prompt_tokens 计入图像、能读出图中文字）；
    # 同端点的 deepseek-v4-pro 会直接回"我无法查看这张图片"，别拿它当视觉模型。
    return [{
        "id": "deepseek-chat",
        "label": "deepseek-flash",
        "base_url": (os.getenv("DEEPSEEK_BASE_URL") or PRESETS["deepseek"]["base_url"]).strip(),
        "api_key": key,
        "model": "deepseek-flash",
        "max_context_k": PRESETS["deepseek"]["max_context_k"],
        "supports_vision": True,
        "is_default": True,
    }]


class ProviderStore:
    def __init__(self, path: str = None):
        self.path = os.path.abspath(path or _default_path())
        # 密钥单独一个文件，路径从记录文件推导而不是再开一个环境变量：
        # 两处事实来源迟早会漂移（"搬了记录没搬密钥"就是下一次的数据丢失），
        # 而 PROVIDERS_DB_PATH 一个变量本来就该把这份配置整体指走。
        self.keys_path = os.path.join(os.path.dirname(self.path), "provider_keys.json")
        # 每人"默认用哪个模型"也是同一份配置的第三张脸，同样从记录路径推导。
        # 此前这个偏好只活在设备 localStorage 里，换手机就回到全局默认——
        # 用户在自己的私有模型和管理员共享模型之间的选择从此有了一份服务端答案。
        self.prefs_path = os.path.join(os.path.dirname(self.path), "provider_prefs.json")
        self._lock = threading.Lock()
        self._items = []
        self._keys = {}
        self._prefs = self._load_prefs()
        self._load()

    def _load_keys(self):
        if not os.path.isfile(self.keys_path):
            return {}
        try:
            with open(self.keys_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError) as e:
            # 读不动就当没有：宁可让 resolve() 说「缺少有效密钥」，
            # 也不要在这里悄悄把内联在记录里的 key 复制回盘上——那等于没搬。
            print(f"⚠️ 密钥文件读不了（{e}），本轮按未配置密钥处理")
            return {}

    def _write_keys(self):
        _write_json_atomic(self.keys_path, self._keys)

    def _load_prefs(self):
        if not os.path.isfile(self.prefs_path):
            return {}
        try:
            with open(self.prefs_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError) as e:
            # 偏好读不动就当没设过：default_for 会回落全局默认，用户顶多回到
            # "用站级默认"，不该把整个聊天链路拖崩。
            print(f"⚠️ 模型偏好读不了（{e}），本轮按未设置默认处理")
            return {}

    def _write_prefs(self):
        _write_json_atomic(self.prefs_path, self._prefs)

    def _load(self):
        migrated = False
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._items = [p for p in data if isinstance(p, dict) and p.get("id")]
                    self._keys = self._load_keys()
                    for p in self._items:
                        inline = p.pop("api_key", "") or ""
                        if inline:
                            # 升级前那份文件里 key 就写在记录上；读的时候顺手搬走
                            self._keys.setdefault(p["id"], inline)
                            migrated = True
                        p["api_key"] = self._keys.get(p["id"], "")
                    if migrated:
                        self._flush()
                    self._register_log_terms()
                    return
            except (ValueError, OSError) as e:
                backup = self.path + ".corrupt"
                try:
                    os.replace(self.path, backup)
                    print(f"⚠️ Provider 配置损坏（{e}），已备份为 {backup}")
                except OSError:
                    print(f"⚠️ Provider 配置损坏且无法备份（{e}）")
        self._keys = self._load_keys()
        self._items = _seed_from_env()
        if self._items:
            self._flush()
            print(f"ℹ️ 已从 .env 初始化 {len(self._items)} 个模型服务配置")

    def _register_log_terms(self):
        # 服务商名/模型 id/上游 host 一旦进过配置，就不许再明文进日志：登记进
        # logsanitizer，之后所有落盘输出统一打码。判据见 app/core/logsanitizer.py。
        from urllib.parse import urlparse
        from app.core import logsanitizer
        for p in self._items:
            logsanitizer.register(p.get("model", ""))
            logsanitizer.register(p.get("label", ""))
            host = urlparse(p.get("base_url", "") or "").hostname
            if host:
                logsanitizer.register(host)

    def _flush(self):
        # 调用方都持着 self._lock（_load 在构造期单线程）。写记录时把 api_key 整个剔掉，
        # 留空字段比删字段更糟：下一个读这份文件的人会以为值在这儿。
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        _write_json_atomic(self.path, [{k: v for k, v in p.items() if k != "api_key"}
                                       for p in self._items])
        self._keys = {p["id"]: p.get("api_key", "") for p in self._items if p.get("api_key")}
        _write_json_atomic(self.keys_path, self._keys)
        self._register_log_terms()
        # 写路径 = 配置变了（改 key、换 base_url、动 timeout 之外的任何一维都算）。
        # 客户端缓存整表作废，下一次 build_client 用新值重建——漏掉这一句，
        # "改了设置没生效"会精确复现在上游连接层。
        invalidate_client_cache()


    # ---- 查询 ----
    def all(self) -> list:
        with self._lock:
            return [dict(p) for p in self._items]

    @staticmethod
    def _is_private(p: dict) -> bool:
        return bool(p.get("owner"))

    @staticmethod
    def _in_pool(p: dict, user_id: str) -> bool:
        """某个用户"看得见、用得了"的判定：全局共享条目人人可见，私有条目只有主人可见。"""
        owner = p.get("owner") or ""
        return not owner or (user_id is not None and owner == user_id)

    def pool(self, user_id: str = None) -> list:
        """该用户可用的 provider 全集（共享 + 自己的私有）。user_id 为 None 只看共享。"""
        with self._lock:
            return [dict(p) for p in self._items if self._in_pool(p, user_id)]

    def mine(self, user_id: str) -> list:
        with self._lock:
            return [dict(p) for p in self._items if p.get("owner") == user_id]

    def visible_to(self, provider_id: str, user_id: str = None) -> bool:
        p = self.get(provider_id)
        return p is not None and self._in_pool(p, user_id)

    def get(self, provider_id: str):
        with self._lock:
            for p in self._items:
                if p["id"] == provider_id:
                    return dict(p)
        return None

    def default(self):
        """「调用方没给 id 时真正会用哪个」的唯一答案（/v1/models 的 default 就是它）。

        只在**可用**的里面挑，判据与 catalog() 给前端的 usable 同一条
        （looks_placeholder）：此前这里只看 is_default 标记，而前端是
        `usable.find(p => p.default) || usable[0]`，于是管理员把 ★ 点在一条只填了
        占位符密钥的配置上时，界面显示的是 B、实际发请求用的是 A——"默认是哪个"
        有了两个答案。现在前端不再自己挑（见 static/app.js 的 serverDefaultProvider）。

        一个都不可用时仍退回老顺序，好让 resolve() 说出准确那句「缺少有效密钥」，
        而不是把"配了但没填 key"说成"尚未配置任何模型服务"。

        私有条目（owner 非空）永不参选：用户自带 key 的模型只服务他一个人，
        它要是能顶掉站级默认，一个人的配置就改变了所有人的默认上游。
        """
        with self._lock:
            shared = [p for p in self._items if not self._is_private(p)]
            usable = [p for p in shared
                      if not looks_placeholder(p.get("api_key", ""))]
            for pool in (usable, shared):
                if not pool:
                    continue
                for p in pool:
                    if p.get("is_default"):
                        return dict(p)
                return dict(pool[0])
            return None

    def get_pref(self, user_id: str):
        if not user_id:
            return None
        with self._lock:
            return self._prefs.get(user_id)

    def default_for(self, user_id: str = None):
        """「这个人不指定模型时真正会用哪个」的唯一答案。

        顺序：他自己的默认偏好（仍在其可用池内且密钥可用）→ 站级默认。
        偏好失效（被删、密钥被清空）不报错，静默回落——换一台设备登录的人
        不该因为上一台设备选过什么而被挡住聊天。
        """
        pref_id = self.get_pref(user_id)
        if pref_id:
            for p in self.pool(user_id):
                if p["id"] == pref_id and not looks_placeholder(p.get("api_key", "")):
                    return p
        return self.default()

    def set_pref(self, user_id: str, provider_id: str) -> None:
        """把「我的默认模型」持久化到服务端。可指向共享或自己的私有条目。"""
        if not user_id:
            raise ProviderError("需要登录身份才能设置默认模型")
        provider = self.get(provider_id)
        if provider is None or not self._in_pool(provider, user_id):
            raise ProviderError("模型服务不存在")
        if looks_placeholder(provider.get("api_key", "")):
            raise ProviderError(f"模型「{provider.get('label')}」缺少有效密钥")
        with self._lock:
            self._prefs[user_id] = provider_id
            self._write_prefs()

    def resolve(self, provider_id: str = None, legacy_model: str = None,
                user_id: str = None) -> dict:
        """按 provider id 取配置；兼容旧的 model 字段；都没有则用默认。

        user_id 是给 HTTP 入口用的归属闸门：带了它，查找只在"共享 + 本人私有"
        这个池子里做，别人的私有条目和根本不存在的条目是同一种失败（回落默认），
        这条链路因此不会变成一个"这个 provider id 存在吗"的探测器。
        不带 user_id 的既有调用方（pipeline/streaming/agents）拿的是入口已验过
        归属的具体 id，行为保持原样。
        """
        wanted = provider_id or legacy_model
        pool = self.pool(user_id)
        provider = self.get(wanted) if wanted else None
        if provider is not None and user_id is not None and not self._in_pool(provider, user_id):
            provider = None
        if provider is None and wanted:
            # 旧客户端可能传 provider id 之外的写法（如模型名），忽略大小写在池内再试一次
            for p in pool:
                if p["id"].lower() == wanted.lower() or p.get("model", "").lower() == wanted.lower():
                    provider = p
                    break
        if provider is None:
            provider = self.default_for(user_id) if user_id is not None else self.default()
        if provider is None:
            raise ProviderError("尚未配置任何模型服务，请在「设置 → 模型服务」中添加")
        if looks_placeholder(provider.get("api_key", "")):
            raise ProviderError(f"模型「{provider.get('label')}」缺少有效密钥，请在设置中填写 API Key")
        return provider

    # ---- 变更 ----
    def _pending(self, record: dict) -> tuple:
        """更新已有条目时，把请求里没带的思考四格 + 模型上限从库里那份补进**待校验的 dict**。

        返回 `(pending, requested)`：requested 是这次请求真正带来的那几格**思考**键名
        （上限不在里面，它的判据只有归一一条，见下面那段注释）。
        这半边和合并同等重要——校验层要靠它分清"用户填的"和"我们替他补的"，
        严格拒收只配作用于前者（判据见 `_thinking_fields`）。

        位置就是这条实现的全部理由（裁决 A）：必须在 `_validate` 之前。放在校验之后
        （brief 的原写法）会造出非法组合——一条 `supports_thinking=True,
        thinking_profile=verified_x` 的老记录，被一次只带 `supports_thinking=False`
        的 PUT 更新时，请求里 profile 是缺的，于是先以 `none` 通过了组合校验，
        随后旧 profile 才被补回来，库里就此存下"不支持思考却带思考参数"。

        只有**键缺失或值为 None** 才算"这次没动它"（与 pricing / paid_by 同一条纪
        律；PUT 路由因此必须走 `model_dump(exclude_none=True)`，否则旧客户端把
        Optional 字段序列化成 None 之外的形状时这条保留会被绕过）。显式 False /
        [] / "" 是用户确实这么填，算 requested、不覆盖。

        锁只用来取那一份旧记录的拷贝：`_validate` 里有 DNS 解析，绝不能挪进锁里
        （持锁解析会把所有 provider 的读写串成一条队伍）。
        """
        provider_id = str(record.get("id") or "")
        if not provider_id:
            return record, _requested_thinking_keys(record)
        with self._lock:
            existing = next((p for p in self._items if p["id"] == provider_id), None)
            old = dict(existing) if existing is not None else None
        if old is None:
            return record, _requested_thinking_keys(record)
        pending = dict(record)
        requested = set()
        for key in _THINKING_KEYS:
            if pending.get(key) is None:
                pending[key] = old.get(key)
            else:
                requested.add(key)
        # 模型上限（max_context_k）走同一条纪律（v0.39.1 Task 9）：这一格从
        # 「设置 → 模型服务」的表单搬进了模型卡，而表单那次 PUT 从此**不带**它。
        # 不接回来的话，`_validate` 会按缺失兜底成 64、`upsert` 又是整条替换——
        # 「改一次模型名字 = 把 1000K 的 DeepSeek 洗成 64K」，全站预算跟着塌，
        # 而且一句错误都不说。显式给值仍走 normalize_max_context_k（越界钳制，
        # 不在这里新增 raise）；新建路径没有"库里那一份"可保留，行为不变。
        # 不进 requested：思考四格的"严格只判带来的值"是给 `_thinking_fields` 的，
        # 上限的判据从头到尾只有归一这一条。
        if pending.get("max_context_k") is None:
            pending["max_context_k"] = old.get("max_context_k")
        return pending, requested

    def upsert(self, record: dict) -> dict:
        pending, requested = self._pending(record)
        cleaned = self._validate(pending, thinking_requested=requested)
        with self._lock:
            existing = next((i for i, p in enumerate(self._items) if p["id"] == cleaned["id"]), None)
            if existing is None:
                # 私有记录永不占站级默认位（包括"库里第一条"的自动置顶）。
                if self._is_private(cleaned):
                    cleaned["is_default"] = False
                else:
                    cleaned["is_default"] = cleaned["is_default"] or not self._items
                self._items.append(cleaned)
                # 「最多一颗 ★」是不变式，两条写路径都得守：更新分支清了别人的，
                # 新增分支不清的话库里能存下两颗，而 default() 只认遍历到的第一颗。
                if cleaned["is_default"]:
                    self._clear_default_except(cleaned["id"])
            else:
                # 归属是服务端管的事实，不从请求里取：更新一条已存在的记录时
                # owner 一律沿用库内那份——管理员的 PUT 不该把共享条目"改姓"，
                # 也不该有任何路径能把私有条目转公或转给另一个人。
                cleaned["owner"] = self._items[existing].get("owner") or ""
                if self._is_private(cleaned):
                    cleaned["is_default"] = False
                # 未填新密钥时保留原密钥，避免编辑界面回显掩码后被写回
                if looks_placeholder(cleaned["api_key"]):
                    cleaned["api_key"] = self._items[existing].get("api_key", "")
                # 同理保留计费归属：前端 PUT 的请求模型（ProviderRequest）没有
                # paid_by 字段，_validate 一律兜底成 operator。不接回来的话，
                # "用户自带 key" 那条账每改一次配置就被静默翻成"管理员垫钱"，
                # 账本从此是假账。只有调用方明确传了 paid_by 才允许改。
                if not str(record.get("paid_by") or "").strip():
                    cleaned["paid_by"] = self._items[existing].get("paid_by") or "operator"
                # 单价与 paid_by 同一条纪律（v0.25 R4b）：旧表单（ProviderRequest）
                # 不认识 pricing，一次普通的改名保存不该把"有价"洗成"未定价"。
                # 请求里根本没有 pricing 键 = 保留原值；显式给 null/{} = 主动取消定价。
                if "pricing" not in record:
                    cleaned["pricing"] = self._items[existing].get("pricing")
                self._items[existing] = cleaned
                if cleaned["is_default"]:
                    self._clear_default_except(cleaned["id"])
            self._flush()
        return cleaned

    def delete(self, provider_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [p for p in self._items if p["id"] != provider_id]
            if len(self._items) == before:
                return False
            if self._items and not any(p.get("is_default") for p in self._items):
                first_shared = next((p for p in self._items if not self._is_private(p)), None)
                if first_shared is not None:
                    first_shared["is_default"] = True
            # 有人把默认押在这条上：删了就顺手清偏好，让 default_for 静默回落，
            # 而不是留一个指向幽灵 id 的偏好永远走回落分支（行为一样，但脏）。
            dirty = False
            for uid, pid in list(self._prefs.items()):
                if pid == provider_id:
                    del self._prefs[uid]
                    dirty = True
            if dirty:
                self._write_prefs()
            self._flush()
            return True

    def set_default(self, provider_id: str) -> bool:
        with self._lock:
            hit = next((p for p in self._items if p["id"] == provider_id), None)
            if hit is None or self._is_private(hit):
                return False
            self._clear_default_except(provider_id)
            self._flush()
            return True

    def _clear_default_except(self, keep_id: str):
        for p in self._items:
            p["is_default"] = (p["id"] == keep_id)

    @staticmethod
    def _validate(record: dict, thinking_requested=None) -> dict:
        label = str(record.get("label") or "").strip()
        base_url = str(record.get("base_url") or "").strip()
        model = str(record.get("model") or "").strip()
        if not label:
            raise ProviderError("label 不能为空")
        if not base_url.startswith(("http://", "https://")):
            raise ProviderError("base_url 必须以 http:// 或 https:// 开头")
        # SSRF 闸门紧跟在这一行后面，而不是散在各路由里：_validate 是**唯一**的
        # 写路径入口（upsert 走它，POST /v1/providers/test 的草稿试连也直接走它），
        # 放这里就等于新增、改配置、存前试连三条路一次都跑不掉。
        # 读路径（catalog/_public）刻意不复验：老记录里可能有当年合法的内网地址，
        # 复验会让升级后这些模型在界面上凭空消失，而它们从未被当作合法配置写入过
        # （本函数从 v0.33 起就是闸门，之前的版本不在这条纪律的追溯范围内）。
        blocked = _base_url_reject_reason(base_url)
        if blocked:
            raise ProviderError(blocked)
        if not model:
            raise ProviderError("model 不能为空")
        # 「这口钱谁出」是账本上的一列，不是可选备注：内置=operator、用户自带=user。
        # 缺省给 operator，因为现存那几条记录确实都是管理员垫的——把缺省定成 user
        # 会让升级后的第一天账目集体变成"用户自己的钱"，那是假账。
        paid_by = str(record.get("paid_by") or "operator").strip()
        if paid_by not in ("operator", "user"):
            raise ProviderError("paid_by 只许 operator 或 user")
        # owner 只由服务端写路径设置（/v1/me/providers 钉上调用者 user_id）；
        # 这里只做归一，空串 = 全局共享。
        owner = str(record.get("owner") or "").strip()
        # v0.25 R4b T5.3：单价配置面。校验口径全在 credits.parse_pricing（三态、
        # 半套价格拒收、per_call 结构预留）——这里只负责把 PricingError 换成
        # ProviderError，让写路径说人话而不是甩一个内部异常类型。
        try:
            pricing = credits.parse_pricing(record.get("pricing"))
        except credits.PricingError as e:
            raise ProviderError(str(e))
        return {
            "id": str(record.get("id") or f"p_{uuid.uuid4().hex[:8]}"),
            "label": label,
            "base_url": base_url.rstrip("/"),
            "api_key": str(record.get("api_key") or "").strip(),
            "model": model,
            "supports_vision": bool(record.get("supports_vision")),
            "max_context_k": normalize_max_context_k(record.get("max_context_k")),
            "is_default": bool(record.get("is_default")),
            "paid_by": paid_by,
            "owner": owner,
            "pricing": pricing,
            # 思考四格。thinking_requested=None 只可能来自直接调 `_validate` 的调用方
            # （没经过 `_pending` 那一步合并，也就没有"继承来的值"要宽容）：那时
            # dict 里非 None 的那几格就是带来的值，照旧逐格严格判。
            **_thinking_fields(record, thinking_requested),
        }

    # ---- 对外视图（绝不返回明文密钥）----
    def public_list(self, user_id: str = None) -> list:
        """user_id=None → 全局共享清单（管理员面）；带 user_id → 该用户可用池。"""
        return [self._public(p) for p in self.pool(user_id)]

    def mine_public(self, user_id: str) -> list:
        return [self._public(p) for p in self.mine(user_id)]

    def catalog(self, user_id: str = None) -> list:
        items = []
        pool = self.pool(user_id) if user_id is not None else [
            p for p in self.all() if not self._is_private(p)]
        for p in pool:
            usable = not looks_placeholder(p.get("api_key", ""))
            items.append({
                "id": p["id"],
                "name": p["label"],
                "model": p["model"],
                "supports_vision": p["supports_vision"],
                # 老记录可能在字段加入之前就躺在盘上：读路径同样过一次归一，
                # 界面封顶永远拿到一个真数字，而不是 undefined。
                "max_context_k": normalize_max_context_k(p.get("max_context_k")),
                "default": bool(p.get("is_default")),
                "shared": not self._is_private(p),
                "usable": usable,
                "reason": "" if usable else "未配置有效密钥",
                # v0.25 R4b：/v1/models 里的倍率与定价状态。倍率只用于"选模型时的
                # 直觉"（卡片 §2.3b），不是合同价；未定价 = "?x"，绝不显示 0.00x。
                "priced": p.get("pricing") is not None,
                "multiplier": credits.multiplier_label(p.get("pricing")),
                # 思考视图四格。这里**不给** thinking_profile：那是"怎么调参"的内部
                # 配方，模型清单（下拉、卡片）用不上，而管理表单要的那一面是 _public。
                # 读路径的兜底口径（截 3 档、default 归回候选）在 _thinking_view 一处，
                # 与 _public 同一份，别在这里再抄一遍。
                **_thinking_view(p),
            })
        return items

    @staticmethod
    def _public(p: dict) -> dict:
        pricing = p.get("pricing")
        return {
            "id": p["id"], "label": p["label"], "base_url": p["base_url"],
            "model": p["model"], "supports_vision": p["supports_vision"],
            "max_context_k": normalize_max_context_k(p.get("max_context_k")),
            "is_default": bool(p.get("is_default")),
            "shared": not ProviderStore._is_private(p),
            "api_key_masked": mask_key(p.get("api_key", "")),
            "has_key": not looks_placeholder(p.get("api_key", "")),
            "paid_by": p.get("paid_by") or "operator",
            # v0.25 R4b T5.3/T5.8：展示面双端（网页/安卓）都从这里抄原料。
            # multiplier 是 None 时界面不显示倍率（per_call），"?x" = 未定价；
            # 单价核对日期与免费截止是「0.00x（1.0 前免费 · 单价核对于 …）」的根据。
            "pricing": pricing,
            "priced": pricing is not None,
            "multiplier": credits.multiplier_label(pricing),
            # 思考视图：与 catalog 同一份 `_thinking_view`，这一面只多给一格
            # thinking_profile——管理表单保存时得把它原样回传，否则一次编辑就把
            # 配方洗回 none。（共享条目对普通用户不给这一格，见 main.my_providers。）
            **_thinking_view(p),
            "thinking_profile": str(p.get("thinking_profile") or DEFAULT_THINKING_PROFILE),
        }

    # ---- 探活 ----
    def ping(self, provider_id: str) -> dict:
        provider = self.resolve(provider_id)
        try:
            client = build_client(provider, timeout=20.0, max_retries=0)
            completion = client.chat.completions.create(
                model=provider["model"],
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=4,
            )
            return {"ok": True, "detail": f"{provider['model']} 响应正常",
                    "sample": (completion.choices[0].message.content or "")[:40]}
        except Exception as e:
            # 这一句会进管理员的屏幕。上游/中转站把 Authorization 原样打印回来
            # 不是假设，是这类网关的常见做法，所以出口在这儿过一次。
            return {"ok": False, "detail": scrub_secrets(f"{type(e).__name__}: {str(e)[:180]}")}


def _client_cache_key(provider: dict, timeout: float, max_retries: int) -> tuple:
    """缓存键带全"会改变上游连接行为"的每一维。
    api_key 只进摘要不进字典键：键会随 dump/日志排查被整份打出来，
    明文凭据不该多活一份副本。
    """
    raw = f"{provider.get('id')}|{provider.get('base_url')}|{provider.get('api_key')}"
    return (hashlib.sha256(raw.encode("utf-8")).hexdigest(), timeout, max_retries)


# 钉住地址的客户端最多复用多久（秒）。pin 之后旧 IP 不能永久复用：CDN / CNAME /
# 灾备换了地址时，缓存的客户端会一直连"校验时那个已经没有这个域名的 IP"。
# TTL 到期后下一次调用重建并重新解析/校验；期间**不在每个请求上做 DNS**，代价是
# 换 IP 后最多 300 秒才生效。
_CLIENT_CACHE_TTL_SECONDS = 300.0


def build_client(provider: dict, timeout: float = 120.0, max_retries: int = 2) -> OpenAI:
    """唯一一个构造上游客户端的地方。

    timeout/max_retries 以前是各调用点自己传的（聊天走默认、探活走 20s/0 次），
    于是"探活"自己又现构了一份客户端——那份和这份漂移出一个参数，
    就会出现"探活说通、聊天说超时"这种查不出形状的话。

    同一份配置现在只建一次（2026-09-23 审查 #9）：旧写法每次聊天都新建
    `httpx.Client` + `OpenAI` 且从不 close，没有连接复用、fd 一路泄漏。
    任何配置写路径（_flush）整表作废：改过 key/base_url 的下一次调用必然建新的。

    DNS 只在缓存未命中时解析一次（`_resolve_and_check`），校验过的地址列表由
    `install_pins` 钉进连接层，httpcore 不再自己解析主机名。pin 的键是
    `_pin_key`——httpx 交给 httpcore 的 IDNA `raw_host`，不是 net_guard 判闸用的
    原始（可能是 Unicode 的）主机名；两者各取所需，见 `_pin_key`。解析与判闸在锁**外**
    （DNS 可能阻塞到超时，持锁会把所有 provider 的取客户端串成一条队伍）；锁内
    只做二次检查与构造。两个并发首调各自解析、先到的那份进缓存，另一份的客户端
    由调用方照常使用（不 close 旧客户端是本次接受的残余：换下的 client 只等 GC）。
    """
    key = _client_cache_key(provider, timeout, max_retries)
    now = time.monotonic()
    with _CLIENT_CACHE_LOCK:
        cached = _CLIENT_CACHE.get(key)
        if cached is not None and now - cached["resolved_at"] < _CLIENT_CACHE_TTL_SECONDS:
            return cached["client"]

    _validated_host, addresses, blocked = _resolve_and_check(provider["base_url"])
    if blocked:
        raise ProviderError(blocked)
    pins = {_pin_key(provider["base_url"]): addresses}
    now = time.monotonic()

    with _CLIENT_CACHE_LOCK:
        cached = _CLIENT_CACHE.get(key)
        if cached is not None and now - cached["resolved_at"] < _CLIENT_CACHE_TTL_SECONDS:
            return cached["client"]
        # trust_env=False 是**有意与旧行为不同**：httpx 默认会读 HTTP(S)_PROXY，
        # 一旦挂了代理，连接走的是 proxy mounts 而不是被 patch 的默认连接池，
        # 已校验地址的 pin 就被绕过去了。代价是"只能靠代理出网"的部署会连不上。
        # follow_redirects=False 保持旧行为（SDK 只在请求显式要求时才下传重定向）。
        http_client = httpx.Client(verify=system_ssl_context(), trust_env=False,
                                   follow_redirects=False)
        try:
            install_pins(http_client, pins)
        except Exception as exc:             # noqa: BLE001 - 钉不住就不许连
            http_client.close()
            raise ProviderError(
                "无法把已校验的上游地址钉进连接层，拒绝建立连接") from exc
        try:
            client = OpenAI(api_key=provider["api_key"], base_url=provider["base_url"],
                            timeout=timeout, max_retries=max_retries,
                            http_client=http_client)
        except Exception:
            http_client.close()
            raise
        _CLIENT_CACHE[key] = {"client": client, "resolved_at": now}
        return client


def invalidate_client_cache() -> None:
    """配置变更时作废整表。不按键精确淘汰是刻意的：条目就几 providers 的量，
    整表清空没有代价，而"哪些维度算变更"以后一旦漂移，精确淘汰就会留下旧客户端。"""
    with _CLIENT_CACHE_LOCK:
        _CLIENT_CACHE.clear()


_CLIENT_CACHE = {}
_CLIENT_CACHE_LOCK = threading.Lock()


store = ProviderStore()
