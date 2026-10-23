"""v0.36.2 契约锁：嵌入密钥单源 与 `_self_origin` 那道闸，必须以这一份形状存在于源码。

行为判据分别钉在 `test_embed_key_source.py` 与 `test_update_channel.py`（7b 节）；
这个文件锁的是**跨模块的契约**——变量名从哪儿读、信任名单以什么为唯一来源、
下游收口不许被顺手改掉。锚点漂移（有人把实现改回旧形状）先在这里红，
而不是等到现网把下载地址换成别人家的域。

每条锁都配了"会红"的负控说明：判据是对源文件文本的精确匹配，把对应实现改回
旧写法（`not any(...)`、Host 不设白名单、把 "testclient" 写进信任名单）都当场红。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

MAIN_SRC = (REPO_ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
MEMORY_SRC = (REPO_ROOT / "backend" / "app" / "memory" / "memory_manager.py").read_text(encoding="utf-8")
SELFCHECK_SRC = (REPO_ROOT / "backend" / "app" / "core" / "selfcheck.py").read_text(encoding="utf-8")
ENV_EXAMPLE = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")


def _body(src: str, name: str) -> str:
    """取出某个顶层 def 的源码文本（到下一个顶层 def/类边界为止）。"""
    match = re.search(rf"^def {re.escape(name)}\(.*?(?=^def |^class |\Z)",
                      src, re.S | re.M)
    assert match, f"源码里找不到顶层函数 {name}"
    return match.group(0)


# ---------- 1. `_self_origin`：Host 白名单 + 可信对端闸 ----------

def test_self_origin_gates_on_the_shared_domain_set_and_trusted_proxies():
    origin = _body(MAIN_SRC, "_self_origin")
    # 域比对只用 releases 那一份集合，不许在 main 里手抄第二清单。
    assert "releases.SELF_HOST_DOMAINS" in origin, origin
    # 形状闸门：端口 / userinfo / 路径段。
    assert '":"' in origin and '"@"' in origin and '"/"' in origin, origin
    # 规范化小写后返回 https origin。
    assert "host.lower()" in origin and 'f"https://{host}"' in origin, origin
    # XFP 只在可信对端时才有资格说话。
    assert "_origin_peer_is_trusted" in origin, origin


def test_peer_trust_is_loopback_plus_explicit_list_and_no_fixture_names():
    trusted = _body(MAIN_SRC, "_origin_peer_is_trusted")
    assert "is_loopback" in trusted, trusted
    assert "SELF_TRUSTED_PROXIES" in trusted, trusted
    # 生产信任名单里绝不许出现测试夹具的名字（"testclient" 进名单 = 把闸门焊死在
    # 测试形状上，现网反而多一个后门）。
    assert "testclient" not in trusted, trusted
    # 解析不出的对端按不可信处理：ip_address 抛 ValueError 的路径必须落到 False。
    assert "except ValueError" in trusted, trusted


def test_the_downstream_self_hosted_url_guard_is_untouched():
    """纵深防御是**加**闸不是**换**闸：releases 里那道域收口原样保留。"""
    from app.core import releases
    assert releases.self_hosted_apk_url("https://evil.example") == ""
    assert releases.self_hosted_apk_url("https://ai.fenever.xyz") == \
        "https://ai.fenever.xyz" + releases.SELF_APK_PATH
    src = (REPO_ROOT / "backend" / "app" / "core" / "releases.py").read_text(encoding="utf-8")
    assert "_is_self_hosted_url(url) else" in src, "self_hosted_apk_url 的域收口被拆了"


# ---------- 2. 嵌入密钥单源：变量名与来源顺序 ----------

def test_memory_manager_reads_the_provider_id_and_keeps_env_fallback():
    assert "EMBEDDINGS_PROVIDER_ID" in MEMORY_SRC
    # 来源顺序的两端都在：provider 命中打这一句，回落打过渡警告。
    assert "嵌入密钥来自 provider 配置" in MEMORY_SRC
    assert "过渡期" in MEMORY_SRC
    # 占位判据用 providers 那一份 looks_placeholder，不在记忆模块里另写一份。
    assert re.search(r"from app\.core\.providers import \(build_client, looks_placeholder",
                     MEMORY_SRC) or "looks_placeholder" in MEMORY_SRC, MEMORY_SRC
    # provider 读取失败不许把初始化拖崩：包在 try 里。
    assert re.search(r"try:\s*\n\s*provider_record = provider_store\.get", MEMORY_SRC), \
        "provider 读取不再 fail-safe"


def test_selfcheck_uses_the_three_piece_conjunction_not_any():
    unconf = _body(SELFCHECK_SRC, "_embeddings_unconfigured")
    # 旧口径 `not any(...)` 只在三项全缺时才说"没配"——半配被谎称已配置。这一版必须
    # 是"密钥来源 and 地址 and 模型名"的合取取非。
    assert "not any" not in unconf, unconf
    assert "_embeddings_key_present" in unconf
    assert "EMBEDDINGS_BASE_URL" in unconf and "EMBEDDINGS_MODEL" in unconf
    keypresent = _body(SELFCHECK_SRC, "_embeddings_key_present")
    assert "EMBEDDINGS_PROVIDER_ID" in keypresent and "looks_placeholder" in keypresent
    # 过渡回落口径：provider 之外仍认 .env 的 api_key。
    assert '"api_key"' in keypresent


def test_the_new_switch_is_documented_where_operators_look():
    """.env.example 必须列出这枚开关（它是单源化的用户入口）； INSTALL 的填表同步。"""
    assert "EMBEDDINGS_PROVIDER_ID" in ENV_EXAMPLE
    install = (REPO_ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    assert "EMBEDDINGS_PROVIDER_ID" in install
    guide = (REPO_ROOT / "docs" / "安装部署指南.md").read_text(encoding="utf-8")
    assert "EMBEDDINGS_PROVIDER_ID" in guide
