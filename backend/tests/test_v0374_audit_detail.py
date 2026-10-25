"""v0.37.4 审计 detail 与发版说明的收口锁。

审计承诺是用户可见的发版说明里的隐私句子：`target.detail` 落盘前必须把异常原文里的
URL 整段剥掉——`httpx.InvalidURL` 这类消息会原样带上 query/fragment/userinfo。
`test_release_notes.py` 只校验当前 `versionName` 对应的 v0.36.4，服务端版
v0.37.4 的三段式与 scope 在这里单独钉住。
"""
import json
import re
from pathlib import Path

from app.core import audit
from app.tools import fetch_core

ROOT = Path(__file__).resolve().parents[2]


def test_audit_detail_scrubs_urls_before_it_hits_disk():
    fetch_core.audit_fetch(
        "web_read", "u", "e.example", "/a", "failed", 0, 0,
        detail="InvalidURL: https://e.example/a?q=secret#frag user:pass@e.example/x")
    entries = audit.read(limit=20)
    assert entries, "审计一条都没写"
    target = entries[-1]["target"]
    text = json.dumps(target, ensure_ascii=False)
    assert "secret" not in text and "frag" not in text and "user:pass" not in text
    assert "<url>" in target["detail"], target


def test_v0374_release_note_is_server_only_and_three_sections():
    text = (ROOT / "docs" / "releases" / "v0.37.4.md").read_text(encoding="utf-8")
    assert "<!-- scope: server-only -->" in text.splitlines()[:5], "scope 标记不在引言区"
    headings = [h.strip() for h in re.findall(r"^##\s+(.+)$", text, re.M)]
    assert headings and set(headings) <= {"修复了什么", "优化了什么", "新增了什么"}, headings
    assert not re.search(r"怎么修|根因|教训", text)
