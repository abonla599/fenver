"""搜索源密钥端点组（/v1/search-keys）与 search_keys 写路径的锁——v0.32 W2。

判据分三层，各钉一个真实的失败形状：

1. **存储层**：set/delete/list 的闸（未知源名、空、占位符）与掩码口径。占位符在
   providers 那边吃过"界面显示已配置、链实际不用"的亏，这里同一形状直接拒收。
2. **端点面**：谁能碰（管理员）、响应里有什么（末四位，别的什么都没有）、
   审计账本里有什么（源名与掩码，明文绝不落脚本）。
3. **一条链**：端点登记的 key 立刻被 search_keys.get 读到——"存进了一份没人消费的文件"
   是配置界面最坏的形状，ALLOWED_SOURCES 与降级链注册表的同步也钉在这里。

假密钥一律单行 `# secret-scan:allow`（test_user_providers_api 的同款约定）：
仓里的密钥形状扫描器看每一行，整颗 key 只写一次，其余地方引用变量。
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.core import audit, logsanitizer, search_keys
from app.tools import search_chain
from app.tools.search_api_sources import BochaSource, TavilySource

KEY = "sk-w2-contract-key-00042"  # secret-scan:allow 测试里造的假密钥，这台机器上没有这颗 key
MASK = "末四位 0042"              # mask_key(KEY) 的唯一合法外显形态


@pytest.fixture
def keys_db(tmp_path, monkeypatch):
    """每条用例一份独立的密钥文件（指向不存在的路径 = 全源未启用的起点）。"""
    p = tmp_path / "search_keys.json"
    monkeypatch.setenv("SEARCH_KEYS_DB_PATH", str(p))
    return p


@pytest.fixture
def audit_file(tmp_path, monkeypatch):
    p = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(p))
    return p


# ------------------------------------------------------------------ 存储层 --

def test_set_key_returns_mask_and_persists_plaintext_only_to_the_file(keys_db):
    assert search_keys.set_key("bocha", KEY) == MASK
    assert json.loads(keys_db.read_text(encoding="utf-8"))["bocha"] == KEY
    assert search_keys.get("bocha") == KEY
    assert search_keys.masked("bocha") == MASK


def test_set_key_gates_unknown_source_empty_and_placeholder(keys_db):
    with pytest.raises(search_keys.SearchKeyError):
        search_keys.set_key("bing", "sk-whatever")        # 爬取源没有 key 可配
    with pytest.raises(search_keys.SearchKeyError):
        search_keys.set_key("bocha", "   ")
    with pytest.raises(search_keys.SearchKeyError):
        search_keys.set_key("bocha", "your-key-here")     # 占位符：拒收，不"存下但视作未启用"
    assert search_keys.get("bocha") == ""                 # 三枪都没在文件里留下任何东西


def test_hand_written_placeholder_reads_as_unconfigured(keys_db):
    """手写文件里躺了颗占位符：get 的口径仍是"没配"，与 W1 的零出站判据一致。"""
    keys_db.write_text(json.dumps({"tavily": "changeme"}), encoding="utf-8")
    assert search_keys.get("tavily") == ""
    row = next(r for r in search_keys.list_masked() if r["name"] == "tavily")
    assert row["configured"] is False and row["masked"] == ""


def test_delete_key_is_idempotent_report(keys_db):
    search_keys.set_key("tavily", KEY)
    assert search_keys.delete_key("tavily") is True
    assert search_keys.delete_key("tavily") is False
    assert search_keys.get("tavily") == ""


def test_registered_key_reaches_the_log_sanitizer(keys_db):
    """set_key 顺手把明文登记进出站脱敏：万一它出现在别的日志行里也得被隐掉。"""
    search_keys.set_key("bocha", KEY)
    assert KEY not in logsanitizer.redact(f"leaked line: {KEY}")


def test_allowed_sources_all_have_a_real_consumer(keys_db):
    """名单不许漂：ALLOWED 里的每个源名都必须在降级链注册表里有一个真源，
    且每个真吃 API-key 的源都在名单里——界面上登记一把没人消费的 key 是最坏的绿。"""
    names = {s.name for s in search_chain.SOURCES}
    assert set(search_keys.ALLOWED_SOURCES) <= names
    assert {BochaSource.name, TavilySource.name} == set(search_keys.ALLOWED_SOURCES)


# ---------------------------------------------------------------- 端点面 --

def test_endpoints_need_no_login_in_disabled_mode(client, keys_db):
    r = client.get("/v1/search-keys")
    assert r.status_code == 200
    body = r.json()
    assert [row["name"] for row in body["sources"]] == list(search_keys.ALLOWED_SOURCES)
    assert body["order"] == [s.name for s in search_chain.SOURCES]
    assert all(row["configured"] is False for row in body["sources"])


def test_put_then_get_shows_only_the_mask(client, keys_db):
    r = client.put("/v1/search-keys/bocha", json={"api_key": KEY})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "saved", "source": "bocha",
                        "configured": True, "masked": MASK}
    assert KEY not in r.text

    g = client.get("/v1/search-keys")
    assert KEY not in g.text        # 整份响应面里明文一个片段都没有
    row = next(s for s in g.json()["sources"] if s["name"] == "bocha")
    assert row["configured"] is True and row["masked"] == MASK
    # 端点这一枪登记的 key 当场进链：存储与消费是同一份文件、同一个取用口径
    assert search_keys.get("bocha") == KEY


def test_delete_roundtrip_and_404_when_nothing_registered(client, keys_db):
    client.put("/v1/search-keys/tavily", json={"api_key": KEY})
    assert client.delete("/v1/search-keys/tavily").status_code == 200
    assert client.delete("/v1/search-keys/tavily").status_code == 404
    assert search_keys.get("tavily") == ""


def test_bad_requests_fail_closed_without_touching_the_file(client, keys_db):
    assert client.put("/v1/search-keys/bing", json={"api_key": KEY}).status_code == 404
    assert client.put("/v1/search-keys/nope", json={"api_key": KEY}).status_code == 404
    assert client.put("/v1/search-keys/bocha", json={"api_key": "xxx"}).status_code == 400
    assert client.put("/v1/search-keys/bocha", json={"api_key": ""}).status_code == 400
    assert not keys_db.exists()     # 四枪全灭，文件根本没被创建过


def test_plaintext_never_reaches_the_audit_ledger(client, audit_file, keys_db):
    client.put("/v1/search-keys/bocha", json={"api_key": KEY})
    client.delete("/v1/search-keys/bocha")
    raw = audit_file.read_text(encoding="utf-8")
    assert KEY not in raw
    assert "search-key.set" in raw and "search-key.delete" in raw
    assert MASK in raw              # 账本记的是掩码：出事后对账够用，泄密不背锅


def test_mere_user_cannot_touch_search_keys(client, enforced, keys_db):
    hdr = enforced("search-key-outsider")
    assert client.get("/v1/search-keys", headers=hdr).status_code == 403
    assert client.put("/v1/search-keys/bocha", json={"api_key": KEY},
                      headers=hdr).status_code == 403
    assert client.delete("/v1/search-keys/bocha", headers=hdr).status_code == 403
    # 管理员那面照常能进：boot 令牌就是 enforced 模式下的管理员身份
    admin = {"Authorization": "Bearer " + os.environ["ACCESS_TOKEN"]}
    assert client.get("/v1/search-keys", headers=admin).status_code == 200
