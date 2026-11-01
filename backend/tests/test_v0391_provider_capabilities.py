"""v0.39.1 Task 8：provider 思考能力字段 + /v1/models 的 can_edit。

判据源：.superpowers/sdd/2026-10-31-model-cockpit/task-8-brief.md。brief 内部两处
自相矛盾按裁决落地（裁决 A/B 是 spec 第 207-237 行的口径）：

  裁决 A —— 更新时四个 thinking 字段的保留发生在 `_validate` **之前**。放在校验之后
            会出现非法组合：老记录 `supports_thinking=True, profile=verified_x`，
            一次只带 `supports_thinking=False` 的 PUT 先以 profile=none 过了组合校验，
            随后旧 profile 被补回来，库里落下「不支持思考却带思考参数」。
  裁决 B —— 非法档位**拒收**，不截断不夹值。brief 的示例实现会把 4 个档位截到 3 个、
            default 不在候选里时夹成 levels[0]，那是把"没配上"演成"配上了"。
            只有读路径（catalog/_public）对畸形老数据防御性截断。

修复轮 1（A 与 B 相撞的新口径）—— 严格判据只作用在**请求真正带来的那几格**上；
从库里补进来的继承值只做宽容归一，永不 raise。合并在 `_validate` 之前不变（A），
但校验层按来源分叉（B 收窄）：请求带的野 profile / 4 档 / 越界 default 照样拒收，
继承来的同一样畸形值归一成自洽的一份。于是一次改名、一次显式关思考都存得下去，
而"库里存的那一份永远自洽"这条不变式照样守住。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

import pytest

from app.core import providers
from app.core.providers import (DEFAULT_THINKING_PROFILE, ProviderError,
                                ProviderStore)


def _store(tmp_path, items):
    p = tmp_path / "providers.json"
    p.write_text(json.dumps(items), encoding="utf-8")
    return ProviderStore(path=str(p))


def _old(**over):
    base = {"id": "p1", "label": "旧模型", "base_url": "https://1.1.1.1/v1",
            "api_key": "sk-test-123456",  # secret-scan:allow 测试里造的假密钥，这台机器上没有这颗 key
            "model": "m", "supports_vision": False, "is_default": True}
    base.update(over)
    return base


def test_old_records_get_safe_defaults_and_unverified_status(tmp_path):
    s = _store(tmp_path, [_old()])
    e = s.catalog()[0]
    assert e["supports_thinking"] is False and e["thinking_levels"] == []
    assert e["thinking_default"] == "" and e["thinking_status"] == "unsupported"


def test_thinking_fields_validate_and_old_client_put_preserves_them(tmp_path):
    s = _store(tmp_path, [_old()])
    s.upsert({**_old(), "supports_thinking": True,
              "thinking_levels": ["快速", "均衡"], "thinking_default": "均衡"})
    saved = s.get("p1")
    assert saved["thinking_levels"] == ["快速", "均衡"]
    # 旧客户端 PUT 不带新字段：必须保留，不许静默洗成 false
    s.upsert({**_old(), "label": "改名"})
    assert s.get("p1")["supports_thinking"] is True
    assert s.get("p1")["thinking_levels"] == ["快速", "均衡"]


# 三类非法输入各自钉死报错文本。bad2 同时钉"先判 profile 再判组合"：
# supports=False + 野 profile 必须报"未知的思考参数配置"，而不是先到组合校验
# 报"不支持思考的模型不能配置思考参数"——一个不认识的值摆在面前，
# 第一句该说的是"这个值我不认"。
@pytest.mark.parametrize("bad,msg", [
    ({"thinking_levels": ["a", "b", "c", "d"]}, "思考档位最多 3 个"),
    ({"supports_thinking": True, "thinking_levels": ["快速"], "thinking_default": "均衡"},
     "思考默认档位不在候选里"),
    ({"supports_thinking": False, "thinking_profile": "nope"}, "未知的思考参数配置"),
])
def test_invalid_capabilities_are_rejected(tmp_path, bad, msg):
    s = _store(tmp_path, [_old()])
    with pytest.raises(ProviderError, match=msg):
        s.upsert({**_old(), **bad})


# ---------- 裁决 B：四条组合口径逐条判，且报错而不是夹值 ----------

def test_explicit_disable_clears_levels_and_normalises_inherited_profile(tmp_path):
    """显式 False 是用户确实这么填：档位清空；继承来的野 profile 归一，不挡保存。"""
    s = _store(tmp_path, [_old(supports_thinking=True, thinking_levels=["快速"],
                               thinking_default="快速", thinking_profile="none")])
    s.upsert({**_old(), "supports_thinking": False})
    saved = s.get("p1")
    assert saved["thinking_levels"] == []
    assert saved["thinking_default"] == ""
    assert saved["thinking_profile"] == "none"

    # 盘上一条 profile 是野值的老记录：一次只改名的 PUT 不许再被它挡住（I-2）。
    # 野值是从库里补进来的、不是这次请求填的 → 宽容归一成白名单缺省，落库自洽。
    s2 = _store(tmp_path, [_old(supports_thinking=True, thinking_levels=["快速"],
                                thinking_default="快速", thinking_profile="nope")])
    saved2 = s2.upsert({**_old(), "label": "只改个名"})
    assert saved2["thinking_profile"] == DEFAULT_THINKING_PROFILE
    assert saved2["thinking_levels"] == ["快速"]
    assert saved2["thinking_default"] == "快速"

    # 同一条野值记录，请求**自己带了**野 profile 就还是拒收（严格性不许退）
    with pytest.raises(ProviderError, match="未知的思考参数配置"):
        s2.upsert({**_old(), "thinking_profile": "nope"})


def test_empty_levels_means_empty_default_not_a_clamped_one(tmp_path):
    """支持思考但没配档位：default 是空串，不是被夹出来的某个值。"""
    s = _store(tmp_path, [_old()])
    s.upsert({**_old(), "supports_thinking": True})
    assert s.get("p1")["thinking_levels"] == []
    assert s.get("p1")["thinking_default"] == ""

    # 未给 default 时取清洗后的第一个候选，而不是原始输入的第一个
    s.upsert({**_old(), "supports_thinking": True,
              "thinking_levels": ["  快速 ", "快速", " 均衡 "]})
    assert s.get("p1")["thinking_levels"] == ["快速", "均衡"]
    assert s.get("p1")["thinking_default"] == "快速"

    # 裁决 B 的另一格：单个标签超 16 字符截断后入库，default 跟着取截断后的那份。
    # 这里显式给空串 = 用户主动清空默认档。**不带** default 走的是另一条口
    # （修复轮 1）：继承来的旧默认落在新候选之外时归到新候选第一个，见
    # test_update_levels_without_default_renormalises_inherited_default。
    s.upsert({**_old(), "supports_thinking": True, "thinking_default": "",
              "thinking_levels": ["一二三四五六七八九十一二三四五六七八九十"]})
    assert s.get("p1")["thinking_levels"] == ["一二三四五六七八九十一二三四五六"]
    assert s.get("p1")["thinking_default"] == "一二三四五六七八九十一二三四五六"


# ---------- catalog / _public 的可见性边界 ----------

def test_catalog_and_public_status_and_profile_visibility(tmp_path):
    s = _store(tmp_path, [_old()])
    entry = s.catalog()[0]
    assert entry["supports_thinking"] is False
    assert entry["thinking_levels"] == []
    assert entry["thinking_default"] == ""
    assert entry["thinking_status"] == "unsupported"

    s.upsert({**_old(), "supports_thinking": True,
              "thinking_levels": ["快速", "均衡"], "thinking_default": "均衡"})
    entry = s.catalog()[0]
    assert entry["thinking_status"] == "unverified"
    # profile 是"怎么调参"的内部配方，普通模型清单不给它（P0c 之前它也只可能是 none）
    assert "thinking_profile" not in entry

    public = ProviderStore._public(s.get("p1"))
    assert public["thinking_profile"] == "none"
    assert public["thinking_status"] == "unverified"


def test_read_path_truncates_malformed_legacy_levels_write_path_refuses(tmp_path):
    """同一个清洗函数，两侧职责不同：读兜底截 3 个，写必须报红。"""
    s = _store(tmp_path, [_old(supports_thinking=True,
                               thinking_levels=["一", "二", "三", "四"])])
    assert len(s.catalog()[0]["thinking_levels"]) == 3
    with pytest.raises(ProviderError):
        s.upsert({**_old(), "supports_thinking": True,
                  "thinking_levels": ["一", "二", "三", "四"]})


# ---------- 修复轮 1 新口径：严格只作用在「请求真正带来的值」上 ----------

def test_update_levels_without_default_renormalises_inherited_default(tmp_path):
    """I-1：只改档位、不带 default —— 继承来的旧默认越界时归一，不许 400。

    旧实现把补进来的 "均衡" 当成用户显式填写去判越界，于是"改档位"这个动作
    本身永远存不下去（严格判据打在了一盘没人碰过的旧值上）。
    """
    s = _store(tmp_path, [_old(supports_thinking=True, thinking_levels=["快速", "均衡"],
                               thinking_default="均衡", thinking_profile="none")])
    saved = s.upsert({**_old(), "supports_thinking": True, "thinking_levels": ["深度思考"]})
    assert saved["thinking_levels"] == ["深度思考"]
    assert saved["thinking_default"] == "深度思考"

    # 连 supports 那一格都不带（旧表单只回填改了名的档位）同样成立
    s2 = _store(tmp_path, [_old(supports_thinking=True, thinking_levels=["快速", "均衡"],
                                thinking_default="均衡", thinking_profile="none")])
    saved2 = s2.upsert({**_old(), "thinking_levels": ["深度思考"]})
    assert saved2["thinking_default"] == "深度思考"


def _assert_self_consistent(rec):
    """一次成功 upsert 之后库里那份必须自洽——这是当初裁决 A 要保的不变式。"""
    assert rec["thinking_profile"] in providers.THINKING_PROFILES
    assert len(rec["thinking_levels"]) <= 3
    if rec["thinking_levels"]:
        assert rec["thinking_default"] in rec["thinking_levels"]
    else:
        assert rec["thinking_default"] == ""
    if not rec["supports_thinking"]:
        assert rec["thinking_levels"] == [] and rec["thinking_default"] == ""
        assert rec["thinking_profile"] == DEFAULT_THINKING_PROFILE


def test_malformed_legacy_record_accepts_three_writes(tmp_path):
    """I-2：盘上一份畸形老记录（4 档 + 野 profile），三种写都得存下去。"""
    bad = _old(supports_thinking=True, thinking_levels=["一", "二", "三", "四"],
               thinking_default="四", thinking_profile="nope")

    # ① 只改名：畸形值全是继承来的 → 宽容归一
    s1 = _store(tmp_path, [bad])
    renamed = s1.upsert({**_old(), "label": "改个名"})
    assert renamed["label"] == "改个名"
    _assert_self_consistent(renamed)

    # ② 显式关思考：`_thinking_fields` 自己那句 docstring 承诺过
    #    "关掉思考不该被上一次候选挡住"，畸形老记录也不能破这条
    s2 = _store(tmp_path, [bad])
    off = s2.upsert({**_old(), "supports_thinking": False})
    _assert_self_consistent(off)
    assert off["thinking_levels"] == [] and off["thinking_default"] == ""

    # ③ 显式带合法 profile：档位仍是继承来的 → 截到 3，default 跟着落回候选
    s3 = _store(tmp_path, [bad])
    fixed = s3.upsert({**_old(), "thinking_profile": DEFAULT_THINKING_PROFILE})
    _assert_self_consistent(fixed)
    assert fixed["thinking_levels"] == ["一", "二", "三"]
    assert fixed["thinking_default"] == "一"


@pytest.mark.parametrize("bad,msg", [
    ({"supports_thinking": True, "thinking_levels": ["一", "二", "三", "四"]}, "思考档位最多 3 个"),
    ({"supports_thinking": True, "thinking_levels": ["快速"], "thinking_default": "别的"},
     "思考默认档位不在候选里"),
    ({"supports_thinking": True, "thinking_profile": "nope"}, "未知的思考参数配置"),
    ({"supports_thinking": False, "thinking_profile": "nope"}, "未知的思考参数配置"),
])
def test_request_brought_invalid_values_are_still_rejected(tmp_path, bad, msg):
    """严格性不许退：拿"继承宽容"当借口把拒收删掉是本任务最容易犯的错。

    库里同时躺着一份畸形老记录——每条用例都既能证明"请求带的值照样判"，
    也能证明"继承来的值没被顺手当显式填写去判"（判序仍按 profile→档位→组合）。
    """
    s = _store(tmp_path, [_old(supports_thinking=True, thinking_levels=["一", "二", "三", "四"],
                               thinking_default="四", thinking_profile="nope")])
    with pytest.raises(ProviderError, match=msg):
        s.upsert({**_old(), **bad})


def test_disable_with_requested_profile_raises_but_inherited_one_normalises(tmp_path, monkeypatch):
    """supports=False + 非 none 的 profile：请求带的拒收，继承来的归一。

    白名单本版只有 none，而 none 就是缺省值，所以"合法但非 none"这格只能靠
    monkeypatch 造出来（P0c 真加 verified profile 时这条自动变成实际路径）。
    """
    monkeypatch.setitem(providers.THINKING_PROFILES, "low",
                        {"label": "低", "verified": False})

    fresh = _store(tmp_path, [_old()])
    with pytest.raises(ProviderError, match="不支持思考的模型不能配置思考参数"):
        fresh.upsert({**_old(), "supports_thinking": False, "thinking_profile": "low"})

    s = _store(tmp_path, [_old(supports_thinking=True, thinking_levels=["快速"],
                               thinking_default="快速", thinking_profile="low")])
    off = s.upsert({**_old(), "supports_thinking": False})
    assert off["thinking_profile"] == DEFAULT_THINKING_PROFILE
    _assert_self_consistent(off)


# ---------- I-3：读侧 verified 派生必须读白名单 ----------

def test_read_status_uses_the_profile_whitelist(tmp_path):
    """野 profile 不许在出口报 verified；none 虽被标了 verified，也不算"参数已核实"。"""
    s = _store(tmp_path, [_old(supports_thinking=True, thinking_levels=["快速"],
                               thinking_default="快速", thinking_profile="nope")])
    assert s.catalog()[0]["thinking_status"] == "unverified"
    assert ProviderStore._public(s.get("p1"))["thinking_status"] == "unverified"

    s2 = _store(tmp_path, [_old(supports_thinking=True,
                                thinking_profile=DEFAULT_THINKING_PROFILE)])
    assert s2.catalog()[0]["thinking_status"] == "unverified"
    assert ProviderStore._public(s2.get("p1"))["thinking_status"] == "unverified"


def test_verified_requires_the_whitelist_entry_to_say_so(tmp_path, monkeypatch):
    """verified 只来自白名单那一格的 verified 为真——两格假档案把判据钉死。"""
    monkeypatch.setitem(providers.THINKING_PROFILES, "fake_verified",
                        {"label": "x", "verified": True})
    monkeypatch.setitem(providers.THINKING_PROFILES, "fake_unverified",
                        {"label": "y", "verified": False})
    for profile, expected in (("fake_verified", "verified"),
                              ("fake_unverified", "unverified"),
                              ("nope", "unverified"),
                              (DEFAULT_THINKING_PROFILE, "unverified")):
        s = _store(tmp_path, [_old(supports_thinking=True, thinking_profile=profile)])
        assert s.catalog()[0]["thinking_status"] == expected, profile
        assert ProviderStore._public(s.get("p1"))["thinking_status"] == expected, profile

    # supports=False 压过 profile：已核实的配方摆在关掉思考的模型上也是 unsupported
    s2 = _store(tmp_path, [_old(supports_thinking=False, thinking_profile="fake_verified")])
    assert s2.catalog()[0]["thinking_status"] == "unsupported"


# ---------- Minor：同一把尺子量两侧 + 读侧候选化 + 单源 + 共享面 ----------

def test_default_label_gets_the_same_clamp_as_levels(tmp_path):
    """Minor-1：同一个 20 字串当档位收、当默认档拒（报错误导），default 也过一遍截断。"""
    long_label = "一二三四五六七八九十一二三四五六七八九十"   # 20 字
    assert len(long_label) == 20
    s = _store(tmp_path, [_old()])
    saved = s.upsert({**_old(), "supports_thinking": True,
                      "thinking_levels": [long_label], "thinking_default": long_label})
    assert saved["thinking_levels"] == [long_label[:16]]
    assert saved["thinking_default"] == long_label[:16]


def test_read_path_snaps_default_into_the_candidates(tmp_path):
    """Minor-2：读侧截完档位，default 还在候选外就归到候选第一个，别甩给界面。"""
    s = _store(tmp_path, [_old(supports_thinking=True, thinking_levels=["一", "二"],
                               thinking_default="三")])
    assert s.catalog()[0]["thinking_default"] == "一"
    assert ProviderStore._public(s.get("p1"))["thinking_default"] == "一"

    s2 = _store(tmp_path, [_old(supports_thinking=True, thinking_levels=[],
                                thinking_default="孤儿")])
    assert s2.catalog()[0]["thinking_default"] == ""
    assert ProviderStore._public(s2.get("p1"))["thinking_default"] == ""

    # 畸形 4 档被截掉后，指着第 4 档的那个 default 也要跟着落回候选
    s3 = _store(tmp_path, [_old(supports_thinking=True, thinking_levels=["一", "二", "三", "四"],
                                thinking_default="四")])
    assert s3.catalog()[0]["thinking_levels"] == ["一", "二", "三"]
    assert s3.catalog()[0]["thinking_default"] == "一"

    # 手工写坏的"关着思考却带着候选"：读侧不给界面画一个永远点不动的选项
    s4 = _store(tmp_path, [_old(supports_thinking=False, thinking_levels=["一"],
                                thinking_default="一")])
    assert s4.catalog()[0]["thinking_levels"] == []
    assert s4.catalog()[0]["thinking_default"] == ""
    assert ProviderStore._public(s4.get("p1"))["thinking_levels"] == []


def test_thinking_view_is_a_single_source(tmp_path):
    """Minor-3：catalog 与 _public 的思考视图一份实现，两处数字不许各写各的。"""
    src = Path(providers.__file__).read_text(encoding="utf-8")
    assert "def _thinking_view(" in src
    assert src.count('"thinking_levels": _clean_levels(') == 1

    s = _store(tmp_path, [_old(supports_thinking=True,
                               thinking_levels=["一", "二", "三", "四"],
                               thinking_default="三", thinking_profile="nope")])
    entry = s.catalog()[0]
    public = ProviderStore._public(s.get("p1"))
    for key in ("supports_thinking", "thinking_levels", "thinking_default", "thinking_status"):
        assert public[key] == entry[key], key
    # _public 在单源之上只多给一格 profile（管理表单回传需要）
    assert public["thinking_profile"] == "nope"
    assert "thinking_profile" not in entry


# ---------- can_edit 矩阵与 /v1/models ----------

def test_can_edit_matrix():
    from app.main import _can_edit_provider
    from app.main import Principal

    admin = Principal(user_id="u0", username="a", role="admin")
    user = Principal(user_id="u1", username="b", role="user")
    assert _can_edit_provider(admin, {"shared": True}) is True
    assert _can_edit_provider(admin, {"shared": False}) is True
    assert _can_edit_provider(user, {"shared": True}) is False
    assert _can_edit_provider(user, {"shared": False}) is True


def test_models_endpoint_can_edit_and_hides_profile(client, enforced):
    hdr = enforced("cap-user")
    models = client.get("/v1/models", headers=hdr).json()["models"]
    shared = next(m for m in models if m["shared"] is True)
    assert shared["can_edit"] is False
    assert "thinking_profile" not in shared

    r = client.post("/v1/me/providers", headers=hdr, json={
        "label": "我的思考模型", "base_url": "https://1.1.1.1/v1",
        "api_key": "sk-test-123456",  # secret-scan:allow 测试里造的假密钥，这台机器上没有这颗 key
        "model": "m", "supports_vision": False,
        "supports_thinking": True, "thinking_levels": ["快速", "均衡"],
        "thinking_default": "均衡",
    })
    assert r.status_code == 200, r.text
    pid = r.json()["provider"]["id"]
    try:
        models = client.get("/v1/models", headers=hdr).json()["models"]
        mine = next(m for m in models if m["id"] == pid)
        assert mine["can_edit"] is True
        assert mine["thinking_status"] == "unverified"
    finally:
        assert client.delete(f"/v1/me/providers/{pid}", headers=hdr).status_code == 200

    # 本机管理员（bootstrap 令牌）连共享条目也能改
    admin_models = client.get("/v1/models",
                              headers={"Authorization": "Bearer boot-token"}).json()["models"]
    assert next(m for m in admin_models if m["shared"] is True)["can_edit"] is True


def test_me_providers_shared_section_hides_profile(client, enforced):
    """Minor-4：/v1/me/providers 的 shared 段剥掉 thinking_profile（内部配方）。

    共享条目对普通用户根本不可写，把"怎么调参"发过去只会让人以为改得动；
    mine 段是本人自己的 provider，回传那一格是"改完保存别把配方洗成 none"的根据。
    """
    hdr = enforced("cap-owner")
    body = client.get("/v1/me/providers", headers=hdr).json()
    assert body["shared"], "夹具里没有共享条目的话这条断言什么都没测"
    for item in body["shared"]:
        assert "thinking_profile" not in item
        assert "thinking_status" in item          # 结论照给，界面要显示"参数未核实"

    pid = client.post("/v1/me/providers", headers=hdr, json={
        "label": "我的配方", "base_url": "https://1.1.1.1/v1",
        "api_key": "sk-test-123456",  # secret-scan:allow 测试里造的假密钥，这台机器上没有这颗 key
        "model": "m", "supports_vision": False,
        "supports_thinking": True, "thinking_levels": ["快速"], "thinking_default": "快速",
    }).json()["provider"]["id"]
    try:
        mine = next(m for m in client.get("/v1/me/providers", headers=hdr).json()["mine"]
                    if m["id"] == pid)
        assert mine["thinking_profile"] == DEFAULT_THINKING_PROFILE
        assert mine["thinking_status"] == "unverified"
        assert mine["thinking_levels"] == ["快速"]
    finally:
        assert client.delete(f"/v1/me/providers/{pid}", headers=hdr).status_code == 200
