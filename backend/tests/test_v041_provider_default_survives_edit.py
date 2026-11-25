"""#229：编辑模型服务不许把全站默认星洗掉（2026-11-26 收口）。

现场：两端表单都硬发 `is_default=False`（表单上根本没有这一格），而 `upsert` 是整条
替换——于是「改一次模型名字」就等于把全站那颗 ★ 摘掉，默认悄悄漂到另一条上，一句
错误都不说。实测复现：before=True → 改名 → after=False。

修法：`_pending` 里给 is_default 与 pricing / max_context_k 同一条「没带就保留」纪律。
之所以用真值判断而不是 None 判断：ProviderRequest.is_default 是 `bool = False`（不是
Optional），「没带」到这一层已经变成 False，与「用户明确关掉」不可区分。所以请求里的
False 一律当没带——要改默认走 /v1/providers/default 那条专用路（它本来就存在）。

**这条锁的数据面完全隔离**：2026-11-26 的事故正是验证脚本漏设 PROVIDERS_DB_PATH、
直接改到了生产 data/providers.json。下面 fixture 把四个数据路径全指进 tmp_path，
并断言 store 真的落在 tmp 里才继续——隔离失败就 fail closed。
"""
import json

import pytest


@pytest.fixture()
def seeded_store(tmp_path, monkeypatch):
    """造一份只有一条「默认星」的 provider 库，全部路径指进 tmp。"""
    p = tmp_path / "providers.json"
    p.write_text(json.dumps([{
        "id": "p_default", "label": "默认模型",
        "base_url": "https://api.openai.com/v1", "model": "m-1",
        "supports_vision": False, "max_context_k": 64,
        "is_default": True, "paid_by": "operator", "owner": "",
}], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("PROVIDERS_DB_PATH", str(p))
    monkeypatch.setenv("PROVIDER_KEYS_PATH", str(tmp_path / "provider_keys.json"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("ADMIN_REAUTH_REQUIRED", "0")
    from app.core import providers as providers_mod
    store = providers_mod.ProviderStore()
    assert str(tmp_path) in str(store.path), \
        "隔离没生效：store 落在测试临时目录之外，拒绝继续（这是 2026-11-26 事故的形状）"
    monkeypatch.setattr(providers_mod, "provider_store", store)
    monkeypatch.setattr("app.main.provider_store", store, raising=False)
    yield store


def test_editing_a_model_keeps_the_site_default_star(client, seeded_store):
    r = client.put("/v1/providers/p_default",
                   json={"label": "默认模型-改名",
                         "base_url": "https://api.openai.com/v1",
                         "model": "m-1", "api_key": "", "is_default": False})
    assert r.status_code == 200, r.text
    after = seeded_store.get("p_default")
    assert after.get("label") == "默认模型-改名", "改名本身要生效"
    assert after.get("is_default") is True, \
        "改名把全站默认星洗掉了——这正是 #229：表单硬发 is_default=False，整条替换就丢了它"
