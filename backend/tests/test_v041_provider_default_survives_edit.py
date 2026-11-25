"""#229：编辑模型服务不许把全站默认星洗掉（2026-11-26 收口）。

现场：两端表单都硬发 `is_default=False`（表单上根本没有这一格），而 `upsert` 是整条
替换——于是「改一次模型名字」就等于把全站那颗 ★ 摘掉，默认悄悄漂到另一条上，一句错误
都不说。实测复现：before=True → 改名 → after=False。

修法：`_pending` 给 is_default 与 pricing / max_context_k 同一条「没带就保留」纪律。
之所以用真值判断而不是 None 判断：ProviderRequest.is_default 是 `bool = False`（不是
Optional），「没带」到这一层已经变成 False，与「用户明确关掉」不可区分。所以请求里的
False 一律当没带——要改默认走 /v1/providers/default 那条专用路（它本来就存在）。

**测的是 ProviderStore 本体而不是 HTTP 端点**：修复就落在 _pending→upsert 这一层，
直接测它既精准、又天然隔离（自己 new 一个指到 tmp_path 的 store，碰不到任何全局单例）。
2026-11-26 的事故正是验证脚本漏设 PROVIDERS_DB_PATH、改到了生产 data/providers.json，
所以这里连 app.main 都不 import。
"""
import json

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """一个只属于这条用例的 store：两个路径都在 tmp 里，碰不到生产 data/。"""
    db = tmp_path / "providers.json"
    db.write_text(json.dumps([{
        "id": "p_default", "label": "默认模型",
        "base_url": "https://api.openai.com/v1", "model": "m-1",
        "supports_vision": False, "max_context_k": 64,
        "is_default": True, "paid_by": "operator", "owner": "",
}], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("PROVIDERS_DB_PATH", str(db))
    monkeypatch.setenv("PROVIDER_KEYS_PATH", str(tmp_path / "provider_keys.json"))
    from app.core.providers import ProviderStore
    s = ProviderStore()
    assert str(tmp_path) in str(s.path), \
        "隔离没生效：store 落在测试临时目录之外，拒绝继续（2026-11-26 事故的形状）"
    yield s


def test_editing_a_model_keeps_the_site_default_star(store):
    """表单只改名字、硬带 is_default=False —— 改完 ★ 必须还在。"""
    before = store.get("p_default")
    assert before.get("is_default") is True

    # 这就是真实表单发出的形状：label 变了，is_default 是无脑 False。
    saved = store.upsert({
        "id": "p_default", "label": "默认模型-改名",
        "base_url": "https://api.openai.com/v1", "model": "m-1",
        "supports_vision": False, "is_default": False,
    })

    assert saved.get("label") == "默认模型-改名", "改名本身要生效"
    assert saved.get("is_default") is True, \
        "改名把全站默认星洗掉了——这正是 #229：表单硬发 is_default=False，整条替换就丢了它"


def test_an_explicit_new_default_still_takes_effect(store):
    """保留不等于冻结：显式设 True 仍然能把 ★ 挪走（/v1/providers/default 走的就是这条）。"""
    store.upsert({
        "id": "p_second", "label": "第二条",
        "base_url": "https://api.openai.com/v1", "model": "m-2",
        "supports_vision": False, "is_default": True,
    })
    assert store.get("p_second").get("is_default") is True
    assert store.get("p_default").get("is_default") is False, "最多一颗 ★ 是不变式"
