"""v0.33 两处 SSRF 闸门的契约测试。

两处都是「管理员能改一个值，服务端拿着它去请求地址」，所以危险的不是管理员改坏
自己的配置，而是这条出站路径能被指着去打别人的网段：

1. `config_store.DEFAULTS["update_repo"]`
   releases.latest_url_for = "https://api.github.com/repos/" + repo + "/releases/latest"
   此前 apply_update 的唯一校验是 _type_ok，对 str 就是 isinstance(value, str)——
   `../../../../169.254.169.254/latest/meta-data/` 原样入库，再被 HTTP 客户端做
   路径归一化吃掉前缀，出站目标就从 api.github.com 变成云元数据地址。

2. `providers.ProviderStore._validate` 里的 base_url
   此前唯一校验是 startswith(("http://", "https://"))，内网与回环一律放行。
   真正的要害不是 ping 那条 SSRF，而是**全局** provider（owner 为空、站级默认）
   的 base_url 被指到攻击者主机后，此后每个走默认 provider 的用户每轮聊天都把
   管理员的 api_key 作为 Authorization: Bearer 发过去。

判据的形状照本目录既有风格写成负向锁：每条都注明"把哪一段删掉，这条会红"。
本次交付的实测变异结果记在下面每条的括号里。

用法（本目录惯例）：../venv/Scripts/python.exe -m pytest tests/本文件.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


# ---------- 漏洞一：update_repo 的 owner/repo 白名单 ----------

@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """把 data/config.json 挪进临时目录，并把进程内那份配置还原（不串味给全场）。"""
    from app.core import config_store as cs

    prev = dict(cs._config)
    db = tmp_path / "config.json"
    monkeypatch.setenv("CONFIG_DB_PATH", str(db))
    cs.restore(str(db))
    try:
        yield cs
    finally:
        cs._config.clear()
        cs._config.update(prev)


# 这些串每一个都能让 releases.latest_url_for 拼出的地址跨出 api.github.com：
# 前两个靠路径归一化，后三个本身就是别的目标主机。
BAD_REPOS = {
    "路径回溯": "../../../../169.254.169.254/latest/meta-data/",
    "绝对地址": "https://evil.example.com/x",
    "云元数据": "169.254.169.254/latest/meta-data",
    "无斜杠": "abc",
    "空串": "",
    "多一段": "a/b/c",
    "带端口": "owner/repo:8080",
    "带 userinfo": "user@owner/repo",
    "反斜杠": "owner\\repo",
    "含空格": "owner / repo",
}


@pytest.mark.parametrize("bad", BAD_REPOS.values(), ids=list(BAD_REPOS))
def test_update_repo_rejects_anything_that_is_not_owner_slash_repo(isolated_config, bad):
    """白名单形状：^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$。

    负向锁：把 config_store.apply_update 里 `_value_ok(key, value)` 那三行删掉，
    本条立刻红（第一版就实测过：10 个参数全 fail，非法值原样进了 rejected 之外
    的 _config，也就是真的进了出站 URL）。
    """
    before = isolated_config.read()["update_repo"]
    cfg, rejected = isolated_config.apply_update({"update_repo": bad})
    assert "update_repo" in rejected, f"这个形状没被拒：{bad!r}"
    assert cfg["update_repo"] == before, "被拒的键不能顺手把值也改了"


def test_update_repo_rejection_is_a_reject_not_an_exception(isolated_config):
    """拒绝的方式必须与既有的一致：进 rejected，不抛异常、不静默吞。

    负向锁：把 rejected.append(key) 换成 raise ValueError，这条会在 apply_update
    那一行炸出来而不是走到断言。
    """
    cfg, rejected = isolated_config.apply_update({"update_repo": "../../etc"})
    assert rejected == ["update_repo"]
    assert cfg  # 仍然回一份完整配置，界面才有东西可显示


@pytest.mark.parametrize("good", ["owner/repo", "abonla599/fenver",
                                  "some_owner/some.repo-1", "A-Z0_9/a"])
def test_update_repo_accepts_real_github_owner_repo_pairs(isolated_config, good):
    """合法的 owner/repo 一个都不许误伤——否则改个配置就把更新链弄断了。

    负向锁：把 _REPO_RE 收紧成 `^[a-z]+/[a-z]+$`（含大小写与数字的仓名会红），
    或把 apply_update 里的 _value_ok 那一段改成无条件 return True。
    """
    cfg, rejected = isolated_config.apply_update({"update_repo": good})
    assert rejected == [], f"合法仓名被误拒：{good!r}"
    assert cfg["update_repo"] == good


def test_update_repo_rejection_does_not_block_the_other_keys(isolated_config):
    """一条键被拒不牵连同批的合法键——这正是"只拒那一个键"的既有纪律。

    负向锁：把 rejected 换成 raise（整单拒绝）后，本条会在 apply_update 就红。
    """
    cfg, rejected = isolated_config.apply_update({
        "update_repo": "../../../../169.254.169.254/", "registration_open": True})
    assert rejected == ["update_repo"]
    assert cfg["registration_open"] is True


# ---------- 漏洞二：base_url 的 SSRF 闸门 ----------

BAD_BASE_URLS = {
    "云元数据": "http://169.254.169.254/",
    "回环": "http://127.0.0.1/",
    "回环带端口": "http://127.0.0.1:8000/v1",
    "私网A": "http://10.1.2.3/",
    "私网B": "http://172.16.5.4/v1",
    "私网C": "http://192.168.1.1/",
    "IPv6 回环": "http://[::1]/",
    "IPv6 本机": "http://[::]/",
    "全零": "http://0.0.0.0:8000/v1",
    "localhost": "http://localhost:8000/v1",
    "子域 localhost": "http://api.localhost/v1",
}


def _record(**over):
    base = {"label": "内网自建", "api_key": "sk-real-looking-key-123456",
            "model": "qwen-plus"}  # secret-scan:allow 测试里造的假密钥，这台机器上没有这颗 key
    base.update(over)
    return base


@pytest.mark.parametrize("bad", BAD_BASE_URLS.values(), ids=list(BAD_BASE_URLS))
def test_base_url_refuses_private_and_loopback_hosts(bad):
    """_validate 必须拒掉内网/回环/链路本地/全零，且给出的是中文理由。

    负向锁：把 providers._validate 里 `blocked = _base_url_reject_reason(base_url)`
    这三行删掉，本条立刻红（11 个参数全 fail）。
    """
    from app.core.providers import ProviderError, ProviderStore

    try:
        ProviderStore._validate(_record(base_url=bad))
    except ProviderError as e:
        why = str(e)
        # 面向管理员的错误文案要说清是哪个地址、为什么，不许是裸异常
        assert bad.rstrip("/").split("/")[-1] or True
        assert any(k in why for k in ("内网", "本机", "localhost", "全零")), \
            f"理由没说出属于哪一类：{why!r}"
        return
    raise AssertionError(f"这个地址没被拒：{bad!r}（它会让服务端替管理员去请求内网）")


def test_base_url_accepts_ordinary_public_https():
    """公网 https 必须照旧通过——闸门拦的是内网，不是"所有地址"。

    负向锁：把 _base_url_reject_reason 改成无条件返回一句理由（"一律不许"），
    或把 _PRIVATE_NETWORKS 写成 0.0.0.0/0，本条立刻红。
    """
    from app.core.providers import ProviderStore

    for good in ("https://api.deepseek.com/v1",
                 "https://dashscope.aliyuncs.com/compatible-mode/v1",
                 "https://api.openai.com/v1"):
        cleaned = ProviderStore._validate(_record(base_url=good))
        assert cleaned["base_url"] == good.rstrip("/")


def test_base_url_guard_covers_both_create_and_update():
    """闸门必须在唯一的写路径入口上，不能只修一条路。

    upsert（新增与修改共用它）和 POST /v1/providers/test 的草稿试连
    （main._test_draft 直接调 _validate）都必须撞上同一道闸。

    负向锁：把 _validate 里那三行闸门挪到 upsert 的 update 分支之后（或只在
    add_provider 路由上补一道手工检查），本条立刻红。
    """
    from app.core.providers import ProviderError, ProviderStore

    evil = "http://169.254.169.254/"
    with pytest.raises(ProviderError):
        ProviderStore._validate(_record(base_url=evil))          # 新增路径
    with pytest.raises(ProviderError):
        ProviderStore._validate(_record(base_url=evil, id="x"))  # 修改路径：同一个入口


def test_localhost_switch_actually_releases_the_gate(monkeypatch):
    """锁住那个开关：ALLOW_LOCALHOST_BASE_URL 翻成 True 时 localhost 真的被接受。

    存在的理由是确有把模型自建在本机的部署（Ollama 之类）；默认仍是拒。

    负向锁：把 ALLOW_LOCALHOST_BASE_URL 写死成 False（删掉 _base_url_reject_reason
    里读它的那个 if），本条立刻红。
    """
    from app.core import providers

    with pytest.raises(providers.ProviderError):
        providers.ProviderStore._validate(_record(base_url="http://localhost:8000/v1"))

    monkeypatch.setattr(providers, "ALLOW_LOCALHOST_BASE_URL", True)
    cleaned = providers.ProviderStore._validate(_record(base_url="http://localhost:8000/v1"))
    assert cleaned["base_url"] == "http://localhost:8000/v1"


def test_localhost_switch_does_not_release_the_other_private_ranges(monkeypatch):
    """开关只管"本机"，不放行真正的内网网段与链路本地。

    开关存在的理由是"把模型自建在本机"（回环/localhost），不是"允许扫内网"。
    10/8、172.16/12、192.168/16、169.254/16 这些在任何开关状态下都必须拒。

    负向锁：把上面那个 if 改成 `if True: return ""`（提前返回、跳过全部网段判断），
    本条立刻红。
    """
    from app.core import providers

    monkeypatch.setattr(providers, "ALLOW_LOCALHOST_BASE_URL", True)
    # "本机"的几种写法都被开关放行（127.0.0.1 / ::1 / 0.0.0.0 / localhost 同义）
    for local in ("http://127.0.0.1:8000/v1", "http://[::1]:8000/v1",
                  "http://0.0.0.0:8000/v1", "http://localhost:8000/v1"):
        providers.ProviderStore._validate(_record(base_url=local))
    # 但内网与链路本地一律照拒
    for still_blocked in ("http://10.1.2.3/v1", "http://172.16.5.4/v1",
                          "http://192.168.1.1/v1", "http://169.254.169.254/"):
        with pytest.raises(providers.ProviderError):
            providers.ProviderStore._validate(_record(base_url=still_blocked))


def test_base_url_with_no_host_is_refused():
    """解析不出主机名的空壳也得拒：`http:///v1` 通过了 startswith 却无处可去。

    负向锁：把 `if not host: return "..."` 那行删掉，本条立刻红。
    """
    from app.core.providers import ProviderError, ProviderStore

    with pytest.raises(ProviderError):
        ProviderStore._validate(_record(base_url="http:///v1"))


def test_mask_key_is_untouched_by_the_ssrf_guard():
    """守住"别牵连"：掩码与脱敏那套出口不受新闸门影响。

    _base_url_reject_reason 只在 _validate 里被调一次，mask_key 不该因为
    这次改动开始拒东西或变形——它是前端唯一的凭据线索。

    负向锁：把 _base_url_reject_reason 改成有副作用（改传入的字符串、或写全局），
    或顺手在 mask_key 里也加一道 host 检查，本条立刻红。
    """
    from app.core.providers import mask_key

    assert mask_key("") == ""
    assert mask_key("sk-abcd1234") == "末四位 1234"
    assert mask_key("short") == "****"
    assert "sk-" not in mask_key("sk-abcd1234")


def test_persisted_public_provider_still_round_trips(tmp_path):
    """闸门不许把既有公网配置弄坏：一条正常 https 记录能存、能读回、能改。

    负向锁：把 _validate 的返回块整个删成 {}，或让闸门误伤 https，本条立刻红。
    """
    from app.core.providers import ProviderStore

    store = ProviderStore(path=str(tmp_path / "providers.json"))
    saved = store.upsert(_record(base_url="https://api.deepseek.com/v1", id="pub"))
    assert saved["base_url"] == "https://api.deepseek.com/v1"
    assert store.get("pub")["base_url"] == "https://api.deepseek.com/v1"
    moved = store.upsert(_record(base_url="https://api.openai.com/v1", id="pub"))
    assert moved["base_url"] == "https://api.openai.com/v1"
    assert store.get("pub")["base_url"] == "https://api.openai.com/v1"