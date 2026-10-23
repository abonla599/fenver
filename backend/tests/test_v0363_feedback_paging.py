"""v0.36.3 反馈分页/导出与会话到期的契约。

三组判据：
1. 不带参数时 `/v1/admin/user-feedback` 仍是旧形状（全量 + unread），旧调用方不许坏；
2. CSV 导出：BOM、列头、CRLF、公式注入前缀、Content-Disposition、no-store；
3. `_public_user` 的 next_session_expiry_at 只由时间戳派生，跳过吊销/过期/无签发时刻，
   并且一个摘要字节都不回显。
"""
import csv
import datetime
import io
import json
import re
import time

import pytest

from app import user_feedback_storage
from app.core import auth_router
from app.core.auth import SESSION_TOKEN_TTL_SECONDS


@pytest.fixture
def uf_file(monkeypatch, tmp_path):
    path = str(tmp_path / "user_feedback.json")
    monkeypatch.setattr(user_feedback_storage, "USER_FEEDBACK_FILE", path)
    return path


def _submit(client, text, email=None):
    body = {"text": text}
    if email:
        body["email"] = email
    res = client.post("/v1/user-feedback", json=body)
    assert res.status_code == 200, res.text
    return res.json()["id"]


def _five(client):
    return [_submit(client, f"第{i}条") for i in range(1, 6)]


# ---------- 旧形状与分页 ----------

def test_no_parameters_still_returns_the_old_shape(client, uf_file):
    _five(client)
    res = client.get("/v1/admin/user-feedback")
    assert res.status_code == 200
    body = res.json()
    assert [item["id"] for item in body["items"]] == [
        "FB-000005", "FB-000004", "FB-000003", "FB-000002", "FB-000001"]
    assert body["unread"] == 5
    assert "next_before" not in body
    assert res.headers.get("cache-control") == "no-store"


def test_limit_and_before_page_newest_first(client, uf_file):
    _five(client)
    first = client.get("/v1/admin/user-feedback", params={"limit": "2"}).json()
    assert [item["id"] for item in first["items"]] == ["FB-000005", "FB-000004"]
    assert first["unread"] == 5
    assert first["next_before"] == "FB-000004"

    second = client.get("/v1/admin/user-feedback",
                        params={"limit": "2", "before": first["next_before"]}).json()
    assert [item["id"] for item in second["items"]] == ["FB-000003", "FB-000002"]
    assert second["next_before"] == "FB-000002"

    third = client.get("/v1/admin/user-feedback",
                       params={"limit": "2", "before": second["next_before"]}).json()
    assert [item["id"] for item in third["items"]] == ["FB-000001"]
    assert third["next_before"] is None


def test_unread_filter_only_pages_unread_and_never_deletes(client, uf_file):
    ids = _five(client)
    assert client.post(f"/v1/admin/user-feedback/{ids[2]}/read").status_code == 200
    body = client.get("/v1/admin/user-feedback",
                      params={"unread": "1", "limit": "10"}).json()
    assert body["unread"] == 4
    assert ids[2] not in [item["id"] for item in body["items"]]
    assert len(user_feedback_storage.list_all()) == 5, "分页/筛选不许删数据"
    assert user_feedback_storage.get(ids[2])["read"] is True


@pytest.mark.parametrize("params", [
    {"limit": "abc"}, {"limit": "0"}, {"limit": "201"},
    {"before": "123"}, {"before": "FB-1"}, {"unread": "maybe"},
])
def test_bad_paging_parameters_are_400(client, uf_file, params):
    _five(client)
    assert client.get("/v1/admin/user-feedback", params=params).status_code == 400


def test_a_cursor_that_no_longer_exists_yields_an_empty_page(client, uf_file):
    _five(client)
    body = client.get("/v1/admin/user-feedback",
                      params={"before": "FB-000999"}).json()
    assert body["items"] == []
    assert body["next_before"] is None


# ---------- CSV 导出 ----------

def test_export_has_bom_header_crlf_and_disposition(client, uf_file):
    _submit(client, "导出测试", email="a@b.co")
    res = client.get("/v1/admin/user-feedback/export.csv")
    assert res.status_code == 200
    assert res.content.startswith(b"\xef\xbb\xbf"), "Excel 需要 UTF-8 BOM"
    assert b"\r\n" in res.content, "csv 行尾必须是 CRLF"
    assert res.headers["content-type"].startswith("text/csv")
    assert re.fullmatch(r'attachment; filename="fenver-feedback-\d{8}\.csv"',
                        res.headers["content-disposition"])
    assert res.headers.get("cache-control") == "no-store"

    rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
    assert rows[0] == ["id", "user_id", "created_at", "read", "email", "text"]
    assert rows[1][0] == "FB-000001"
    assert rows[1][1] == "default_user"
    assert rows[1][3] == "false"
    assert rows[1][4] == "a@b.co"
    assert rows[1][5] == "导出测试"


def test_export_neutralises_formula_injection(client, uf_file):
    _submit(client, "=HYPERLINK(\"http://evil\",\"click\")")
    res = client.get("/v1/admin/user-feedback/export.csv")
    rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
    assert rows[1][5].startswith("'="), "以 = 开头的单元格必须前置单引号"


@pytest.mark.parametrize("dangerous", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"])
def test_csv_cell_guard_covers_every_dangerous_prefix(dangerous):
    from app.main import _csv_safe_cell
    assert _csv_safe_cell(dangerous) == "'" + dangerous


def test_csv_cell_guard_leaves_plain_text_alone():
    from app.main import _csv_safe_cell
    assert _csv_safe_cell("普通文本") == "普通文本"
    assert _csv_safe_cell("a=b") == "a=b"
    assert _csv_safe_cell(None) == ""


# ---------- 会话到期派生 ----------

def test_next_session_expiry_takes_the_earliest_live_token():
    now = time.time()
    record = {
        "user_id": "u_test", "username": "tester", "role": "user",
        "tokens": ["sha256:a", "sha256:b", "sha256:c", "sha256:d"],
        "token_issued_at": {"sha256:a": now - 100,
                            "sha256:b": now - 200,
                            "sha256:c": now - 50},
        "token_absolute_deadline": {"sha256:a": now + 200,
                                    "sha256:b": now + 10 * SESSION_TOKEN_TTL_SECONDS,
                                    "sha256:c": now + 10},
        # c 已吊销：哪怕它的到期时刻最早也不许参与
        "token_revoked_at": {"sha256:c": now - 5},
        # d 没有签发时刻（存量令牌）：跳过
    }
    view = auth_router._public_user(record)
    assert view["sessions"] == 4
    assert view["next_session_expiry_at"] is not None
    parsed = datetime.datetime.fromisoformat(view["next_session_expiry_at"])
    assert abs(parsed.timestamp() - (now + 200)) < 2, (
        "应取 a 的绝对上限，而不是 c（已吊销）或 b（更晚）")
    dumped = json.dumps(view, ensure_ascii=False)
    assert "sha256:" not in dumped and "sha256:a" not in dumped, "摘要被回显了"


def test_next_session_expiry_is_none_when_no_token_is_live():
    now = time.time()
    record = {
        "user_id": "u_test", "username": "tester", "role": "user",
        "tokens": ["sha256:expired", "sha256:revoked", "sha256:legacy", "sha256:bad"],
        "token_issued_at": {
            "sha256:expired": now - SESSION_TOKEN_TTL_SECONDS - 10,
            "sha256:revoked": now - 20,
            "sha256:legacy": float("nan"),
        },
        "token_absolute_deadline": {"sha256:revoked": now + 999,
                                    "sha256:bad": now + 999},
        "token_revoked_at": {"sha256:revoked": now - 1},
    }
    assert auth_router._public_user(record)["next_session_expiry_at"] is None


def test_next_session_expiry_is_none_for_an_admin_created_user(client):
    res = client.post("/v1/admin/users",
                      json={"username": "没有会话的人", "password": "correct-horse-battery"})
    assert res.status_code == 200, res.text
    assert res.json()["next_session_expiry_at"] is None
