"""模型服务（Provider）配置。

界面展示的模型、下拉里能选什么、以及真正调用时用的 base_url/密钥/模型名，
全部来自这一份数据。此前它们分散在 main.py 的手写清单、llm_client.MODEL_CONFIGS
和 streaming.MODEL_CONFIG 三处，互不同步，导致"显示 DeepSeek 实际调 GPT"、
"选了没反应的模型"这类问题无法根治。
"""
import hashlib
import json
import os
import re
import socket
import threading
import uuid
from urllib.parse import urlparse

import httpx
from openai import OpenAI

from app.core import credits, net_guard
from app.core.atomic_write import write_json_atomic
from app.core.paths import data_root, load_project_env
from app.core.tls import system_ssl_context

# 本模块可能早于其他组件被导入（app.pipeline 就会），因此自行加载 .env，
# 否则首次运行时读不到 DEEPSEEK_API_KEY、播种不出任何模型配置。
load_project_env()

PLACEHOLDER_HINTS = ("your-", "your_", "xxx", "placeholder", "填入", "待填", "changeme")

# 出口脱敏要认的形状。宁可多隐一些字，也不要漏一条 key：这些字符串同时会进
# data/backend.log 和管理员看得到的响应体，而日志文件不在任何加密范围内。
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
    """这个 base_url 能不能让服务端替它出网。返回 "" = 放行，否则返回拒的理由。

    拒的判据（说清是哪一条，管理员才改得动；清单与句式在 net_guard）：
    * 解析不出主机名——`http:///v1`、`http://:80/v1` 这类空壳；
    * 主机名是本机——回环（127.0.0.0/8、::1）、全零（0.0.0.0、::）或 localhost
      这个名字：受 ALLOW_LOCALHOST_BASE_URL 管，默认拒；
    * 主机名是 IP 字面量且落在 net_guard 清单的任一段里（私有/链路本地/组播/
      CGNAT/文档与基准/保留段，或 IPv4 映射的 IPv6 写法）：无论如何都拒；
    * 主机名是域名：**解析之后**任一地址落在上述任一类里也拒。这一条是 v0.34.2
      补的——此前域名只看名字本身，于是一条指着 127.0.0.1 或 169.254.169.254 的
      A 记录能直接过关；解析失败同样拒（fail closed），因为一个连服务端都解析
      不出来的名字，放行它等于让下一次连接去猜它指向哪儿。

    还有一件事这一层**没有**关掉，别把它当成已覆盖：闸门只在配置写入时跑一次
    （唯一调用点是 `ProviderStore._validate`），而真正出站在 `build_client` 里由
    httpx/OpenAI SDK 重新解析一次主机名。写入时验过的那个 IP 和连接时用的那个 IP
    之间隔着一次重新解析，这就是 DNS rebinding / TOCTOU 窗口。要真关掉它，得把
    "已校验的 IP"钉进连接层（自定 transport 按 IP 连、Host/SNI 另给），那是改动
    整条客户端路径的另一件事，不该混在配置校验里顺手做一半。
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
    return net_guard.reject_reason(
        host,
        resolve=_resolve_addresses,
        allow_loopback=ALLOW_LOCALHOST_BASE_URL,
        where="base_url",
    )


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
    def upsert(self, record: dict) -> dict:
        cleaned = self._validate(record)
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
    def _validate(record: dict) -> dict:
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


def build_client(provider: dict, timeout: float = 120.0, max_retries: int = 2) -> OpenAI:
    """唯一一个构造上游客户端的地方。

    timeout/max_retries 以前是各调用点自己传的（聊天走默认、探活走 20s/0 次），
    于是"探活"自己又现构了一份客户端——那份和这份漂移出一个参数，
    就会出现"探活说通、聊天说超时"这种查不出形状的话。

    同一份配置现在只建一次（2026-09-23 审查 #9）：旧写法每次聊天都新建
    `httpx.Client` + `OpenAI` 且从不 close，没有连接复用、fd 一路泄漏。
    构造放锁内：客户端的构造不发网络请求，代价可忽略，换来的是"两个并发
    首调各建一份、后一份覆盖前一份、前一份永远没人 close"这种竞态不存在。
    任何配置写路径（_flush）整表作废：改过 key/base_url 的下一次调用必然建新的。
    """
    key = _client_cache_key(provider, timeout, max_retries)
    with _CLIENT_CACHE_LOCK:
        client = _CLIENT_CACHE.get(key)
        if client is None:
            client = OpenAI(api_key=provider["api_key"], base_url=provider["base_url"],
                            timeout=timeout, max_retries=max_retries,
                            http_client=httpx.Client(verify=system_ssl_context()))
            _CLIENT_CACHE[key] = client
        return client


def invalidate_client_cache() -> None:
    """配置变更时作废整表。不按键精确淘汰是刻意的：条目就几 providers 的量，
    整表清空没有代价，而"哪些维度算变更"以后一旦漂移，精确淘汰就会留下旧客户端。"""
    with _CLIENT_CACHE_LOCK:
        _CLIENT_CACHE.clear()


_CLIENT_CACHE = {}
_CLIENT_CACHE_LOCK = threading.Lock()


store = ProviderStore()
