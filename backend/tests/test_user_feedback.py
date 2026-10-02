"""意见反馈（/v1/user-feedback + 管理端读面）的行为合同。

判的是这条链路的四件事：编号独立且递增不复用、提交校验（长度/邮箱/图片张数与
属主）、管理员"已读才能删"的生命周期、以及鉴权边界（普通用户既进不了管理面、
也借这条面读不到别人的附件）。存储文件与截图目录都指进 tmp，测试不碰真实数据。
"""
import os

import pytest

from app import user_feedback_storage

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-ihdr-bytes-for-tests"


@pytest.fixture
def uf_file(monkeypatch, tmp_path):
    """把意见反馈数据文件指进 tmp；截图目录跟着数据文件走（_image_dir 现算）。"""
    path = str(tmp_path / "user_feedback.json")
    monkeypatch.setattr(user_feedback_storage, "USER_FEEDBACK_FILE", path)
    return path


def _upload(client, filename="shot.png", blob=PNG):
    res = client.post("/v1/uploads", files={"file": (filename, blob, "image/png")})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def test_submit_returns_independent_increasing_ids(client, uf_file):
    r1 = client.post("/v1/user-feedback", json={"text": "希望加深色模式"})
    assert r1.status_code == 200, r1.text
    assert r1.json()["id"] == "FB-000001"
    r2 = client.post("/v1/user-feedback", json={"text": "反馈第二条"})
    assert r2.json()["id"] == "FB-000002"
    assert r1.json()["id"] != r2.json()["id"], "每条反馈必须有独立编号"


def test_admin_list_newest_first_with_unread_count(client, uf_file):
    client.post("/v1/user-feedback", json={"text": "第一条"})
    client.post("/v1/user-feedback", json={"text": "第二条", "email": "a@b.co"})
    res = client.get("/v1/admin/user-feedback")
    assert res.status_code == 200
    data = res.json()
    assert data["unread"] == 2
    assert [it["id"] for it in data["items"]] == ["FB-000002", "FB-000001"]
    assert data["items"][0]["email"] == "a@b.co"
    # 展示形状不带磁盘路径：截图只能经专用管理端点取
    assert "images" not in data["items"][0]
    assert data["items"][0]["image_count"] == 0


@pytest.mark.parametrize("body,why", [
    ({"text": ""}, "空文本"),
    ({"text": "   "}, "纯空白"),
    ({"text": "字" * 301}, "超 300 字"),
    ({"text": "正常", "email": "not-an-email"}, "邮箱形状不对"),
    ({"text": "正常", "images": ["0123456789abcdef"]}, "引用不存在的截图"),
])
def test_submit_rejections_leave_no_record(client, uf_file, body, why):
    res = client.post("/v1/user-feedback", json=body)
    assert res.status_code == 400, f"{why} 应被拒（400），实际 {res.status_code}"
    assert client.get("/v1/admin/user-feedback").json()["items"] == [], \
        f"{why} 被拒后不该留下任何记录：{res.text}"


def test_too_many_images_rejected(client, uf_file):
    ids = [_upload(client, f"s{i}.png") for i in range(5)]
    res = client.post("/v1/user-feedback", json={"text": "五张图", "images": ids})
    assert res.status_code in (400, 422), "超过 4 张必须被拒"
    assert client.get("/v1/admin/user-feedback").json()["items"] == []


def test_non_image_attachment_rejected(client, uf_file):
    text_id = _upload(client, filename="note.txt", blob=b"hello")
    res = client.post("/v1/user-feedback", json={"text": "附图不是图", "images": [text_id]})
    assert res.status_code == 400
    assert "图片" in res.json()["detail"]


def test_image_copied_and_served_to_admin(client, uf_file):
    upload_id = _upload(client)
    res = client.post("/v1/user-feedback", json={"text": "带截图的反馈", "images": [upload_id]})
    assert res.status_code == 200
    fb_id = res.json()["id"]
    # 复制语义：用户随后删掉自己的附件，管理员案头那份还在
    assert client.delete(f"/v1/uploads/{upload_id}").status_code == 200
    img = client.get(f"/v1/admin/user-feedback/{fb_id}/image/0")
    assert img.status_code == 200
    assert img.content == PNG
    assert client.get(f"/v1/admin/user-feedback/{fb_id}/image/1").status_code == 404


def test_read_then_delete_lifecycle(client, uf_file):
    upload_id = _upload(client)
    fb_id = client.post("/v1/user-feedback",
                        json={"text": "要删的反馈", "images": [upload_id]}).json()["id"]
    assert client.delete(f"/v1/admin/user-feedback/{fb_id}").status_code == 409, \
        "未读不许删：这是用户要的合同"
    marked = client.post(f"/v1/admin/user-feedback/{fb_id}/read")
    assert marked.status_code == 200
    assert marked.json()["read"] is True and marked.json()["read_at"]
    # 幂等：重复已读不报错，read_at 记第一次
    again = client.post(f"/v1/admin/user-feedback/{fb_id}/read").json()
    assert again["read_at"] == marked.json()["read_at"]
    assert client.delete(f"/v1/admin/user-feedback/{fb_id}").status_code == 200
    assert client.get("/v1/admin/user-feedback").json()["items"] == []
    # 删除连同截图文件一起清掉，不留孤儿字节
    assert os.listdir(user_feedback_storage._image_dir()) == []
    assert client.delete(f"/v1/admin/user-feedback/{fb_id}").status_code == 404
    assert client.get(f"/v1/admin/user-feedback/{fb_id}/image/0").status_code == 404


def test_ids_never_reused_after_delete(client, uf_file):
    first = client.post("/v1/user-feedback", json={"text": "一"}).json()["id"]
    client.post(f"/v1/admin/user-feedback/{first}/read")
    client.delete(f"/v1/admin/user-feedback/{first}")
    second = client.post("/v1/user-feedback", json={"text": "二"}).json()["id"]
    assert second == "FB-000002", "编号只增不回收：删掉的那条不许让新反馈顶号"


def test_counter_survives_reload(client, uf_file):
    """编号计数器在文件里，不在进程记忆里：模拟重启后重新读文档再提交。"""
    client.post("/v1/user-feedback", json={"text": "重启前"})
    import json as _json
    with open(uf_file, encoding="utf-8") as f:
        assert _json.load(f)["next_no"] == 2
    # 截图与正文落在数据文件旁边，指走数据文件就够了（隔离合同依赖这一点）
    assert os.path.dirname(user_feedback_storage._image_dir()) == os.path.dirname(uf_file)


def test_delete_is_audited(client, uf_file):
    fb_id = client.post("/v1/user-feedback", json={"text": "留痕"}).json()["id"]
    client.post(f"/v1/admin/user-feedback/{fb_id}/read")
    client.delete(f"/v1/admin/user-feedback/{fb_id}")
    entries = client.get("/v1/admin/audit?limit=50").json()["entries"]
    actions = [(e.get("action"), e.get("target")) for e in entries]
    assert ("feedback.read", fb_id) in actions
    assert ("feedback.delete", fb_id) in actions


def test_enforced_auth_boundaries(client, uf_file, enforced):
    """普通用户：进不了管理面，也借反馈接口引用不到别人的附件。"""
    res = client.post("/v1/user-feedback", json={"text": "匿名提交"})
    assert res.status_code == 401
    headers = enforced("feedback-alice")
    admin = client.get("/v1/admin/user-feedback", headers=headers)
    assert admin.status_code == 403
    assert client.post("/v1/admin/user-feedback/FB-000001/read", headers=headers).status_code == 403
    assert client.delete("/v1/admin/user-feedback/FB-000001", headers=headers).status_code == 403
    # default_user（bootstrap 管理员）先传一张图，Alice 引用它 → 与不存在同一句话
    res = client.post("/v1/uploads", files={"file": ("shot.png", PNG, "image/png")},
                      headers={"Authorization": "Bearer boot-token"})
    assert res.status_code == 200, res.text
    upload_id = res.json()["id"]
    mine = client.post("/v1/user-feedback", json={"text": "Alice 的反馈"}, headers=headers)
    assert mine.status_code == 200
    steal = client.post("/v1/user-feedback", json={"text": "借图", "images": [upload_id]},
                        headers=headers)
    assert steal.status_code == 400
    assert client.get("/v1/admin/user-feedback/FB-000001/image/0", headers=headers).status_code == 403
