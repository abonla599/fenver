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
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

import pytest

from app.core.providers import ProviderStore, ProviderError


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

def test_explicit_disable_clears_levels_and_rejects_old_profile(tmp_path):
    """显式 False 是用户确实这么填：档位清空；但旧 profile 不能跟着进来。"""
    s = _store(tmp_path, [_old(supports_thinking=True, thinking_levels=["快速"],
                               thinking_default="快速", thinking_profile="none")])
    s.upsert({**_old(), "supports_thinking": False})
    saved = s.get("p1")
    assert saved["thinking_levels"] == []
    assert saved["thinking_default"] == ""
    assert saved["thinking_profile"] == "none"

    # 盘上一条 profile 是野值的老记录：保留它 → 白名单必须先拒收，不许静默回落
    s2 = _store(tmp_path, [_old(supports_thinking=True, thinking_levels=["快速"],
                                thinking_default="快速", thinking_profile="nope")])
    with pytest.raises(ProviderError):
        s2.upsert({**_old(), "label": "只改个名"})


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
    # default 必须显式给空串——不带它会被裁决 A 从库里补成上一轮的"快速"，
    # 和新档位不一致，组合校验会先拒收（那是对的，这次请求动了 levels）。
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
