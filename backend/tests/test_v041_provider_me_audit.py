"""#230：个人面 provider 的写操作必须有审计（2026-11-25 收口）。

为什么钉：之前个人面三个写口（POST/PUT/DELETE `/v1/me/providers`）**零审计**——
冒名覆盖不会被记录，被拒的 400 也看不见。管理员面对应的三个动作早就有账，个人面漏了
整整一层，于是"这条私有模型是谁什么时候改的"根本答不出来。

这条锁不读源码形状，它**真调端点**再查 audit.jsonl：形状锁只能证明「写了 audit.log」，
证明不了那一行真的落盘、也证明不了动作名对得上。
"""
import json

import pytest

MY_KEY = "sk-audit-probe-key-0123456789"  # secret-scan:allow 测试里造的假密钥


@pytest.fixture(autouse=True)
def audit_file(tmp_path, monkeypatch):
    """每条用例一份独立的账（与 test_audit_contract 同一口径）。"""
    p = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(p))
    yield p


def _payload(**over):
    base = {"label": "审计探针", "base_url": "https://api.openai.com/v1",
            "api_key": MY_KEY, "model": "m-1", "supports_vision": False}
    base.update(over)
    return base


def _actions(audit_file):
    if not audit_file.is_file():
        return []
    return [json.loads(l).get("action")
            for l in audit_file.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_personal_provider_writes_are_audited(client, enforced, audit_file):
    """一条用例走完 create → update → delete，三个动作各要一条账。"""
    hdr = enforced("provider-me-audit")

    created = client.post("/v1/me/providers", json=_payload(), headers=hdr)
    assert created.status_code == 200, created.text
    pid = created.json()["provider"]["id"]

    # PUT 是 exclude_none 的全量语义，但 body 里仍得带必填项——只发 label 会 422。
    upd = client.put(f"/v1/me/providers/{pid}", json=_payload(label="改名后"),
                     headers=hdr)
    assert upd.status_code == 200, upd.text
    assert client.delete(f"/v1/me/providers/{pid}", headers=hdr).status_code == 200

    acts = _actions(audit_file)
    for want in ("provider.me.create", "provider.me.update", "provider.me.delete"):
        assert want in acts, f"个人面写操作没落审计：缺 {want}（实际 {acts}）"


def test_personal_provider_audit_never_carries_the_api_key(client, enforced, audit_file):
    """补账不许把密钥也记进去——那是拿一个洞换另一个洞。"""
    hdr = enforced("provider-me-audit-key")
    r = client.post("/v1/me/providers", json=_payload(), headers=hdr)
    assert r.status_code == 200, r.text
    raw = audit_file.read_text(encoding="utf-8") if audit_file.is_file() else ""
    assert MY_KEY not in raw, "审计把 api_key 明文写进去了"
