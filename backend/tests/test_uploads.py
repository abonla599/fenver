"""附件上传与注入测试。"""
import io
import json
import os
import re
import struct
import sys
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + ctype + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF))


def _real_png(width: int = 1, height: int = 1) -> bytes:
    """一只真的 1×1 PNG：签名 + IHDR(带 CRC) + IDAT(zlib 压过的扫描线) + IEND。

    为什么不再用 `PNG_MAGIC + b"\\x00" * 64`：后端自 2026-10-07 起在签名之后还要按
    格式走一遍结构（见 app/core/uploads.py 的 _verify_image），那种字节过不了
    chunk CRC——它当初能当"合法图片"用，正因为当时只验文件头。
    """
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(raw))
            + _png_chunk(b"IEND", b""))


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
FAKE_PNG = _real_png()


def _upload(name, content, mime="application/octet-stream"):
    return client.post("/v1/uploads", files={"file": (name, content, mime)})


def test_text_file_upload_returns_preview():
    res = _upload("notes.txt", "第一行内容\n第二行内容".encode(), "text/plain")
    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "text"
    assert "第一行内容" in body["preview"]


def test_image_upload_detected_by_magic_bytes():
    body = _upload("shot.png", FAKE_PNG, "image/png").json()
    assert body["kind"] == "image"
    assert body["mime"] == "image/png"


def test_file_renamed_to_png_is_rejected():
    """只信扩展名会让任意字节伪装成图片，必须按文件头判定。"""
    res = _upload("evil.png", b"not really an image at all", "image/png")
    assert res.status_code == 400
    assert "图片" in res.json()["detail"]


def test_unsupported_extension_rejected():
    res = _upload("payload.exe", b"MZ\x90\x00" + b"\x00" * 40)
    assert res.status_code == 400
    assert ".exe" in res.json()["detail"]


def test_oversized_text_rejected():
    res = _upload("big.txt", b"a" * (1 * 1024 * 1024 + 10), "text/plain")
    assert res.status_code == 400
    assert "超过上限" in res.json()["detail"]


def test_uploaded_filename_cannot_escape_storage():
    """落盘文件名一律用生成的 uuid，不接受客户端给的路径。"""
    res = _upload("../../evil.txt", b"escape attempt", "text/plain")
    assert res.status_code == 200
    assert "/" not in res.json()["id"] and ".." not in res.json()["id"]


def test_text_attachment_is_injected_into_model_message(monkeypatch):
    """文本附件必须真的进入发给模型的消息，否则"上传了"只是界面假象。"""
    from types import SimpleNamespace
    import app.pipeline as pipeline
    captured = {}

    def create(**kwargs):
        captured["messages"] = kwargs["messages"]
        msg = SimpleNamespace(content="已收到附件", tool_calls=None,
                              model_dump=lambda: {"role": "assistant", "content": "已收到附件"})
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(pipeline, "build_client", lambda provider: fake)

    up = _upload("code.py", b"def hello():\n    return 1\n", "text/plain").json()
    res = client.post("/v1/chat", json={
        "model": "fake-model",
        "messages": [{"role": "user", "content": "这段代码有什么问题"}],
        "attachments": [up["id"]],
    })
    assert res.status_code == 200
    sent = captured["messages"][-1]["content"]
    assert "def hello()" in sent
    assert "code.py" in sent


def test_image_rejected_when_model_has_no_vision():
    """不支持视觉时必须明确报错，静默丢图会让用户以为模型看不懂。"""
    up = _upload("shot.png", FAKE_PNG, "image/png").json()
    res = client.post("/v1/chat", json={
        "model": "fake-model",   # conftest 里 supports_vision=False
        "messages": [{"role": "user", "content": "图里是什么"}],
        "attachments": [up["id"]],
    })
    assert res.status_code == 400
    assert "不支持图片" in res.json()["detail"]


def test_missing_attachment_id_reports_error():
    res = client.post("/v1/chat", json={
        "model": "fake-model",
        "messages": [{"role": "user", "content": "hi"}],
        "attachments": ["deadbeefdeadbeef"],
    })
    assert res.status_code == 400
    assert "附件" in res.json()["detail"]


def test_attachment_deleted_and_download_roundtrip():
    up = _upload("a.md", "# 标题".encode("utf-8"), "text/markdown").json()
    got = client.get(f"/v1/uploads/{up['id']}/file")
    assert got.status_code == 200
    assert "标题" in got.text
    assert client.delete(f"/v1/uploads/{up['id']}").status_code == 200
    assert client.get(f"/v1/uploads/{up['id']}/file").status_code == 404


# ---------- PDF ----------

def _make_pdf(text: str) -> bytes:
    fitz = pytest.importorskip("fitz", reason="PDF 支持需要 pymupdf")
    doc = fitz.open()
    doc.new_page().insert_text((72, 100), text, fontname="helv")
    blob = doc.tobytes()
    doc.close()
    return blob


def test_pdf_upload_is_parsed_into_text():
    blob = _make_pdf("quantum key distribution")
    res = _upload("论文.pdf", blob, "application/pdf")
    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "text"
    # 界面上仍显示原文件名，用户不必知道自己传的不是 txt
    assert body["name"] == "论文.pdf"
    assert "quantum key distribution" in body["preview"]


def test_pdf_size_reported_as_original_file():
    """转换后只剩几 KB 文本，若按转换后计体积会误导用户。"""
    blob = _make_pdf("hello")
    body = _upload("doc.pdf", blob, "application/pdf").json()
    assert body["size"] == len(blob)


def test_garbage_named_pdf_is_rejected():
    res = _upload("evil.pdf", b"definitely not a pdf", "application/pdf")
    assert res.status_code == 400
    assert "PDF" in res.json()["detail"]


# ---------- .docx ----------

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_blob(document_xml: str, body_part: str = "word/document.xml") -> bytes:
    """手工搭一个 .docx（本质是 zip）。不借 python-docx：测试不能依赖一个
    生产代码并不需要的包，否则测试通过与否和生产环境的行为脱节。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("_rels/.rels", "<Relationships/>")
        z.writestr(body_part, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                              + document_xml)
    return buf.getvalue()


def _wdoc(body: str) -> str:
    return f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>'


def _para(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def test_docx_upload_is_parsed_into_text():
    blob = _docx_blob(_wdoc(
        _para("实验一 网络协议分析")
        + "<w:p><w:r><w:t>目的：</w:t><w:tab/><w:t>掌握 Wireshark 抓包</w:t></w:r></w:p>"
        + "<w:p><w:r><w:t>第一行</w:t><w:br/><w:t>第二行</w:t></w:r></w:p>"
        # 表格单元格里的文字也是 w:p，必须一起读到：实验数据常放在表里
        + '<w:tbl><w:tr><w:tc>' + _para("端口") + '</w:tc><w:tc>' + _para("8080") + '</w:tc></w:tr></w:tbl>'))
    res = _upload("实验报告（实验1）.docx", blob,
                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert res.status_code == 200
    body = res.json()
    assert body["kind"] == "text"
    assert body["name"] == "实验报告（实验1）.docx"
    preview = body["preview"]
    assert "实验一 网络协议分析" in preview
    assert "目的：\t掌握 Wireshark 抓包" in preview, "w:tab 丢了会让字段挤成一坨"
    assert "第一行\n第二行" in preview, "w:br 是软换行，不能和下一行粘在一起"
    assert "端口" in preview and "8080" in preview


def test_docx_xml_escapes_are_decoded():
    blob = _docx_blob(_wdoc(_para("a &amp; b &lt;tag&gt; &#26816;&#26597;")))
    preview = _upload("t.docx", blob).json()["preview"]
    assert preview == "a & b <tag> 检查"


def test_legacy_doc_named_docx_tells_the_user_to_resave():
    """老 .doc 是 OLE 复合文档，不是 zip。光说"打不开"没出路，得给出另存为。"""
    res = _upload("旧报告.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 200)
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "另存为 .docx" in detail


def test_zip_without_document_body_names_the_real_problem():
    """xlsx/pptx 同样是 zip。只说"打不开"会让人以为是文件损坏。"""
    blob = _docx_blob("<x/>", body_part="xl/workbook.xml")
    res = _upload("成绩表.docx", blob)
    assert res.status_code == 400
    assert "缺少正文" in res.json()["detail"]


def test_docx_declaring_an_entity_is_refused():
    """内部实体会被解析器展开（billion laughs），外部实体让服务器去取远端资源。

    Word/WPS 不会写出这种声明，所以拒绝它不损失任何真实文档。
    """
    xml = ('<?xml version="1.0"?><!DOCTYPE w:document [<!ENTITY a "' + "A" * 40 + '">]>'
           + _wdoc(_para("&a;")))
    res = _upload("bomb.docx", _docx_blob(xml))
    assert res.status_code == 400
    assert "实体" in res.json()["detail"]


def test_entity_declaration_hidden_behind_a_long_comment_is_still_refused():
    """正对照：DOCTYPE 前面允许有任意长的注释，只查文件开头会被绕过。"""
    xml = ('<?xml version="1.0"?>' + "<!--" + "x" * 9000 + "-->"
           + '<!DOCTYPE w:document [<!ENTITY a "AAAA">]>' + _wdoc(_para("&a;")))
    res = _upload("sneaky.docx", _docx_blob(xml))
    assert res.status_code == 400
    assert "实体" in res.json()["detail"]


def test_docx_decompressed_bomb_is_refused():
    """原始体积不到 10MB 上限，解压后却有 20MB：zip bomb。

    按解压后的字节数封顶才拦得住——file_size 是 zip 头自报的，改小它就能过关。
    夹具大小写死 20MB、不从 MAX_DOCX_XML_BYTES 反推：否则把上限一抬，夹具跟着
    变成几十 GB 的字符串，测的就不再是这件事了。
    """
    from app.core.uploads import MAX_DOCX_XML_BYTES

    assert MAX_DOCX_XML_BYTES < 20 * 1024 * 1024, \
        f"解压上限已抬到 {MAX_DOCX_XML_BYTES}，这份 20MB 夹具不再是炸弹"
    blob = _docx_blob(_wdoc("<w:p/>" * (20 * 1024 * 1024 // 6 + 1)))
    assert len(blob) < 1024 * 1024, "夹具必须远小于 10MB，否则测的是体积上限而不是炸弹"
    res = _upload("zipbomb.docx", blob)
    assert res.status_code == 400
    assert "远超正常文档" in res.json()["detail"]


def test_image_only_docx_says_why_there_is_no_text():
    blob = _docx_blob(_wdoc(
        '<w:p><w:r><w:drawing>'
        '<wp:inline xmlns:wp="urn:wp"/></w:drawing></w:r></w:p>'))
    res = _upload("扫描件.docx", blob)
    assert res.status_code == 400
    assert "未提取到文字" in res.json()["detail"]


def test_doc_extension_tables_stay_in_step():
    """DOC_EXTS / DOC_MIMES / DOC_EXTRACTORS 三份清单必须同一批扩展名。

    detect_kind 用第一份放行、第二份报 MIME，save() 用第三份取解析器；
    少一个键就是"选择器能选、上传却 500"。
    """
    from app.core.uploads import DOC_EXTS, DOC_EXTRACTORS, DOC_MIMES

    assert DOC_EXTS and set(DOC_EXTS) == set(DOC_MIMES) == set(DOC_EXTRACTORS)
    # 正对照：故意漏一个键时上面这条必须变红
    assert ".docx" in DOC_EXTS and ".pdf" in DOC_EXTS


# ---------- 附件怎么进模型 ----------

def test_image_without_a_vision_model_is_an_explicit_refusal():
    """界面上那句"当前模型不支持图片输入"就是这道闸门。

    静默把图丢掉更糟：用户会以为模型看不懂这张图，而不是自己选的模型根本读不了图。
    """
    from app.core.uploads import UploadError, build_user_content, store as upload_store

    rec = upload_store.save("shot.png", FAKE_PNG, "image/png", owner="u_gate")
    with pytest.raises(UploadError) as e:
        build_user_content("解读一下", [rec["id"]], False, owner="u_gate")
    assert "不支持图片输入" in str(e.value)
    assert "1 张" in str(e.value), "得说清被拦下几张，否则多张附件要一张张试"


def test_image_with_a_vision_model_reaches_the_model_as_a_data_uri():
    from app.core.uploads import build_user_content, store as upload_store

    rec = upload_store.save("shot.png", FAKE_PNG, "image/png", owner="u_ok")
    content = build_user_content("解读一下", [rec["id"]], True, owner="u_ok")
    assert isinstance(content, list), "带图必须是多模态数组，不能还是纯字符串"
    assert content[0] == {"type": "text", "text": "解读一下"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_text_and_image_attachments_coexist_in_one_turn():
    from app.core.uploads import build_user_content, store as upload_store

    txt = upload_store.save("数据.csv", b"a,b\n1,2", "text/csv", owner="u_mix")
    img = upload_store.save("shot.png", FAKE_PNG, "image/png", owner="u_mix")
    content = build_user_content("对照看看", [txt["id"], img["id"]], True, owner="u_mix")
    assert content[0]["text"].startswith("对照看看")
    assert "数据.csv" in content[0]["text"] and "a,b" in content[0]["text"]
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


# ---------- 前端 accept 与后端白名单必须一致 ----------

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"
# 图片按钮用 MIME，后端用扩展名，这里做一层桥接
IMAGE_MIME_TO_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                     "image/webp": ".webp", "image/bmp": ".bmp"}


def _accept_of(html: str, element_id: str) -> list:
    tag = re.search(rf'<input[^>]*id="{element_id}"[^>]*>', html)
    assert tag, f"index.html 里找不到 #{element_id}"
    accept = re.search(r'accept="([^"]+)"', tag.group(0))
    assert accept, f"#{element_id} 没有 accept，系统选择器会列出全部类型"
    return [t.strip() for t in accept.group(1).split(",") if t.strip()]


@pytest.mark.parametrize("element_id", ["imageInput", "fileInput"])
def test_picker_accept_only_offers_types_the_server_accepts(element_id):
    """选择器里能选中的东西，后端必须真的收得下。

    放开一个后端不支持的类型，比干脆不提供更糟：用户费事选完文件，
    换来的是一句"不支持的文件类型"。
    """
    from app.core.uploads import IMAGE_EXTS, detect_kind

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    tokens = _accept_of(html, element_id)
    assert tokens, f"#{element_id} 的 accept 为空"

    for token in tokens:
        if element_id == "imageInput":
            assert token in IMAGE_MIME_TO_EXT, f"图片选择器出现后端不认的 {token}"
            assert IMAGE_MIME_TO_EXT[token] in IMAGE_EXTS
        else:
            assert token.startswith("."), f"文件选择器只应列扩展名，出现 MIME {token}"
            detect_kind("sample" + token, b"arbitrary bytes")   # 不抛错即后端接受


def test_picker_offers_every_type_the_server_accepts():
    """反方向也要对上：后端支持的扩展名，选择器必须列出来。

    两边各留一份清单迟早对不上——后端加了 .docx 而 accept 没加时，桌面浏览器
    会把 .docx 直接过滤掉，用户以为功能没做；而在安卓壳里 accept 常被忽略，
    选得上却换来一句"不支持的文件类型"（2026-09-20 就是这么撞上的）。
    """
    from app.core.uploads import DOC_EXTS, TEXT_EXTS

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    offered = set(_accept_of(html, "fileInput"))
    assert offered == TEXT_EXTS | DOC_EXTS, (
        f"选择器缺 {sorted((TEXT_EXTS | DOC_EXTS) - offered)}、"
        f"多 {sorted(offered - (TEXT_EXTS | DOC_EXTS))}")


def test_file_picker_does_not_offer_images():
    """图片有专门的入口，混在文件里会让用户不知道模型能不能看懂。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    assert not image_exts & set(_accept_of(html, "fileInput"))


# ---------- 坏索引必须先留证再重启：附件索引是唯一那份 id→文件 映射 ----------
# 这里曾经只 print 一句"忽略历史附件记录"就以空表启动，而下一次 save() 会把只有
# 一条记录的索引写回 index.json：历史附件的文件还在磁盘上，却再也认不出属主与原
# 文件名，等于把"索引坏了"升级成"所有人上传过的东西全丢了"。session_store 与
# auth 两个兄弟存储都是先改名 .corrupt，这条对齐由下面两个用例钉住。

BROKEN_INDEXES = [
    ('{"abc": {"name": "合同.pdf", "owner": "u_1", "tru', "半截 JSON"),
    ('[{"id": "abc", "name": "合同.pdf"}]', "读得懂但顶层是列表"),
]


@pytest.mark.parametrize("broken,desc", BROKEN_INDEXES)
def test_broken_index_is_quarantined_not_overwritten(tmp_path, broken, desc):
    """两种坏法都必须改名留证；第二例是重点——`json.load` 不抛错，except 抓不到它。"""
    from app.core.uploads import UploadStore

    d = tmp_path / "uploads"
    d.mkdir()
    (d / "index.json").write_text(broken, encoding="utf-8")

    store = UploadStore(directory=str(d))
    assert (d / "index.json.corrupt").exists(), f"{desc}：必须先备份成 .corrupt"
    assert not (d / "index.json").exists(), f"{desc}：原件要让位，不能留在原地等着被覆盖"
    assert store._index == {}, f"{desc}：仍然以空表启动，服务不该因此起不来"

    # 真正的判据在这一行之后：重启后的第一次上传写的是新文件，
    # 而那份认不出来的历史仍完整可读——运维还有得救。
    store.save("笔记.txt", "内容".encode("utf-8"), "text/plain", owner="u_new")
    assert (d / "index.json.corrupt").read_text(encoding="utf-8") == broken, \
        f"{desc}：备份必须活过一次 save()"
    fresh = json.loads((d / "index.json").read_text(encoding="utf-8"))
    assert [r["owner"] for r in fresh.values()] == ["u_new"]


# ---------- v0.34.2：索引里的 path 是外部输入，四个消费点共用一道闸 ----------
# `index.json` 是从磁盘读回来的，`record["path"]` 从 _load() 那一步起就不再是
# 我们写下去的那个值。此前它被四处裸用：get() 拿它 isfile、read_text() 拿它 open
# 并把内容拼进 prompt、data_uri() 拿它 open+base64 送给模型、delete() 拿它
# os.remove。于是"索引被手改过/损坏/被另一个写入者动过"一条 `path` 值就分别等于
# 任意文件读、任意文件读出去、任意删除。
# 判据：realpath 规范化之后必须落在 self.dir 之内，用 commonpath 而不是字符串
# 前缀——前缀匹配会把 `<父>/uploads_evil/x.txt` 判成在 `<父>/uploads` 里面
# （"uploads" 正是它的字符串前缀），而那恰好是这道闸门唯一要挡的那一种。
# 违规口径是"这条记录当作不存在"：get→None、read_text→""、data_uri→None、
# delete→False 且磁盘上一个字节都不动。
#
# 下面所有用例都在 tmp_path 里造那个"目录外的文件"，绝不指向仓库真实的 .env 或
# 任何真实数据文件——这条纪律是硬性的：用例一旦指向真文件，它就成了"能删生产"的
# 一份说明书。

_ABSENT = object()   # "path 字段整个不存在"这一形态，与 path=None 要分开演


def _manual_index(up: Path, record: dict) -> None:
    """把手工记录写成 index.json（重建 store 走真实读盘那条路，不只是内存塞一条）。"""
    (up / "index.json").write_text(
        json.dumps({record["id"]: record}, ensure_ascii=False), encoding="utf-8")


def _record(path_value=_ABSENT, kind="text", **over):
    rec = {"id": "a" * 16, "name": "账单.txt", "kind": kind, "mime": "text/plain",
           "size": 12, "path": path_value, "owner": "u_1",
           "created_at": "2026-10-17T00:00:00"}
    rec.update(over)
    if rec.get("path", None) is _ABSENT:
        del rec["path"]
    return rec


# 分两组，是因为它们钉的东西不一样，混在一处会被读成"八条独立的锁"：
#
# A 组「指向目录外一份**真的存在**的文件」——这组才是漏洞本体。判据是双向的：
#   闸门在时读不到、闸门拆掉后**读得到**（下面那条用例的注释写了拆哪一行）。
#   注意 `..` 的层数必须让目标真的存在：写成 up/../../secret.txt 会指到 tmp 之外
#   一个不存在的文件，那样旧写法（裸 isfile）也返回 False、用例就绿得什么都没测。
#
# B 组「形状就不对」（空串 / None / 不是字符串 / 字段缺失 / 目录本身）——
#   这组里有些形态旧代码也会"碰巧"拒（isfile("") 与 isfile(dir) 都是 False），
#   所以它们不许只靠四条出口断言交差：每条都另外直接断一次
#   `_resolve_within_dir(record) == ""`，把"闸门自己判了它"钉住。
OUTSIDE_CASE_IDS = (
    "A 隔壁目录（startswith 会放行的那一条）",
    "A 父目录里的文件",
    "A 用 .. 从目录里走回上层",
    "A 父目录文件的绝对+相对混写",
    "B 上传目录本身",
    "B 空串",
    "B None",
    "B 不是字符串",
    "B path 字段整个缺失",
)


def _outside_case_value(which: str, up: Path):
    """按用例名在 tmp_path 里造出那份"目录外的真文件"，并返回要写进索引的 path 值。

    刻意不在模块顶层/装饰器里造：那样 import 这个文件就会在当前工作目录下
    mkdir 出 uploads_evil/ 与 secret.txt——本仓库的目录同时是生产机。
    """
    sibling = up.parent / "uploads_evil"
    parent_file = up.parent / "secret.txt"
    if which.startswith("A"):
        sibling.mkdir(parents=True, exist_ok=True)
        (sibling / "s.txt").write_text("隔壁目录的正文", encoding="utf-8")
        parent_file.write_text("别人的账单正文", encoding="utf-8")
    if which.endswith("那一条）"):                  # 隔壁目录
        return str(sibling / "s.txt")
    if which == "A 父目录里的文件":
        return str(parent_file)
    if which == "A 用 .. 从目录里走回上层":
        return str(up / ".." / "secret.txt")        # 一层 .. 才真的指到上面那份
    if which == "A 父目录文件的绝对+相对混写":
        return str(up) + os.sep + os.pardir + os.sep + "secret.txt"
    if which == "B 上传目录本身":
        return str(up)
    if which == "B 空串":
        return ""
    if which == "B None":
        return None
    if which == "B 不是字符串":
        return 12345
    return _ABSENT                                   # B path 字段整个缺失


@pytest.mark.parametrize("which", OUTSIDE_CASE_IDS)
def test_outside_dir_path_refuses_read_preview_datauri_and_delete(tmp_path, which):
    """越界 path 的四条出口全部按"没有这个附件"处理，外面那份文件不许被动。

    这里刻意重建一次 store 而不是只往内存里塞记录：现网"索引被人改过"走的就是
    从 index.json 读回来这条路。

    负向锁（A 组）：把 get() 里 `path = self._resolve_within_dir(record)` 那两行换回
    `if not os.path.isfile(record["path"])`，四条 A 参数全红（内容读得出去了）。
    负向锁（B 组，也是全部九条）：把 _resolve_within_dir 改成 `return record.get("path")`
    ——下面那次直接断言立刻红，B 组再也不会"旧写法碰巧也拒"糊过去。
    """
    from app.core.uploads import UploadStore

    up = tmp_path / "uploads"
    up.mkdir()
    rec = _record(_outside_case_value(which, up))
    _manual_index(up, rec)

    store = UploadStore(directory=str(up))
    # 这一行是"闸门自己判了它"，不是"文件恰好不存在"——A/B 两组都必须过。
    assert store._resolve_within_dir(store._index.get(rec["id"])) == "", \
        f"{which}：闸门没有把它判成越界"

    assert store.get(rec["id"], owner="u_1") is None, f"{which}：越界记录必须当成不存在"
    assert store.read_text(rec["id"], owner="u_1", max_chars=400) == "", \
        f"{which}：预览把目录外的正文读出去了"
    assert store.data_uri(rec["id"], owner="u_1") is None, f"{which}：data_uri 读出去了"
    assert store.delete(rec["id"], owner="u_1") is False, f"{which}：越界不该报删除成功"

    # 真正的判据：别人家那两份文件都还在原地、内容未变（只有 A 组会造它们）。
    if which.startswith("A"):
        assert (tmp_path / "secret.txt").read_text(encoding="utf-8") == "别人的账单正文", \
            f"{which}：目录外的文件被读走或被删了"
        assert (tmp_path / "uploads_evil" / "s.txt").read_text(
            encoding="utf-8") == "隔壁目录的正文", f"{which}：uploads_evil 那份被动过"
    assert [p.name for p in up.iterdir()] == ["index.json"], \
        "越界处置不该在上传目录里留下新文件"


@pytest.mark.parametrize("kind,mime", [("text", "text/plain"), ("image", "image/png")])
def test_outside_path_refused_for_both_text_and_image_exits(tmp_path, kind, mime):
    """两类附件各有自己的出口（read_text / data_uri），两条都要单独拒。

    只给一条加闸是这类改动最常见的漏法：图片那条把整份文件 base64 送进上游请求。

    负向锁：删掉 data_uri 里那次 `_resolve_within_dir`，kind=image 那条红。
    """
    from app.core.uploads import UploadStore

    up = tmp_path / "uploads"
    up.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("不该被读到的内容", encoding="utf-8")
    rec = _record(str(outside), kind=kind, mime=mime)
    _manual_index(up, rec)

    store = UploadStore(directory=str(up))
    assert store.read_text(rec["id"], owner="u_1") == ""
    assert store.data_uri(rec["id"], owner="u_1") is None
    assert outside.read_text(encoding="utf-8") == "不该被读到的内容", "内容被动过"


def test_outside_dir_record_is_left_in_index_as_evidence(tmp_path):
    """越界那条 delete 返回 False 之后，索引条目必须**还在**。

    悄悄 pop 掉等于把"有人改过索引"的证据擦了，而下一个人就查不到是谁改的。
    这一条钉的就是那个选择。

    负向锁：把 delete() 越界分支改成先 pop 再 return False，本条红（条目不见了）。
    """
    from app.core.uploads import UploadStore

    up = tmp_path / "uploads"
    up.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("还在原地", encoding="utf-8")
    rec = _record(str(victim))
    _manual_index(up, rec)

    store = UploadStore(directory=str(up))
    assert store.delete(rec["id"], owner="u_1") is False
    assert victim.exists(), "越界的 path 不该被删"
    assert rec["id"] in store._index, "越界记录要留在索引里等人工处置"
    on_disk = json.loads((up / "index.json").read_text(encoding="utf-8"))
    assert rec["id"] in on_disk, "不该写回一份「已经把可疑条目删掉」的索引"


def test_legit_path_inside_dir_still_reads_previews_and_deletes(tmp_path):
    """正向对照：闸门不许把正常附件一起拦掉，否则"绿"只是因为什么都拒。

    负向锁：把 _within_dir 改成无条件返回 None，本条红。
    """
    from app.core.uploads import UploadStore

    up = tmp_path / "uploads"
    store = UploadStore(directory=str(up))
    pub = store.save("合同.txt", "甲方与乙方".encode("utf-8"), "text/plain", owner="u_1")

    assert store.get(pub["id"], owner="u_1") is not None, "合法附件被误拒"
    assert "甲方与乙方" in store.read_text(pub["id"], owner="u_1")
    assert "甲方" in pub["preview"], "预览这条出口也得照常工作（它走的是同一道闸）"
    assert store.delete(pub["id"], owner="u_1") is True
    left = [p.name for p in up.iterdir() if p.name != "index.json"]
    assert left == [], f"删除后目录里该清空，还剩 {left}"


def test_public_response_shape_is_unchanged(tmp_path):
    """public() 的对外形状一个键都不许多、一个键都不许少（客户端依赖它）。

    收紧落点判据这一轮不该顺手把 path 或"是否越界"这类内部信息透出界面去。

    负向锁：在 public() 里回一个 `"safe": ...` 或把 path 加进出参，本条红。
    """
    from app.core.uploads import UploadStore

    store = UploadStore(directory=str(tmp_path / "uploads"))
    text_shape = store.save("笔记.txt", "正文".encode("utf-8"), "text/plain", owner="u_1")
    assert set(text_shape) == {"id", "name", "kind", "mime", "size", "created_at",
                               "preview"}
    img_shape = store.save("截图.png", FAKE_PNG, "image/png", owner="u_1")
    assert set(img_shape) == {"id", "name", "kind", "mime", "size", "created_at"}
    for shape in (text_shape, img_shape):
        assert "path" not in shape and str(tmp_path / "uploads") not in str(shape), \
            "落盘路径不许出现在对外响应里"


# ---------- 写侧：客户端的文件名不许决定落点 ----------

# detect_kind 认下的扩展名全集，落盘时一个都不许被改动（改了就是把图片存成无后缀文件）。
# 在测试函数里现取，不在模块顶层抄一份名字：抄的那份迟早和 uploads.py 对不上。
# 这些形状都不许拼进落盘名：要么被 detect_kind 当场拒，要么被洗成"没有扩展名"。
ODD_EXTS = {
    "带正斜杠": "notes.txt/../../evil",
    "带反斜杠": "notes.txt\\..\\evil",
    "全角扩展名": "照片.ＰＮＧ",
    "超长扩展名": "report." + ("a" * 80),
    "带控制字符": "weird.\x00txt",
    "只有点": "notes.",
    "点开头无扩展": ".notes",
    "带盘符": "C:\\x.txt",
    "换行": "notes\n.txt",
}


def test_store_ext_keeps_every_legit_extension():
    """白名单内的扩展名逐个验：`_store_ext` 一个都不许改。

    存在的理由：如果只测"怪形状被洗掉"，把白名单写成空集也能绿——那时每个附件
    都落成无扩展名的文件，图片那条链（按后缀推 mime）就静默歪了。

    负向锁：把 `return ext if ext in STORE_EXTS else ""` 里的 STORE_EXTS 换成
    空集，本条红。
    """
    from app.core.uploads import DOC_EXTS, IMAGE_EXTS, STORE_EXTS, TEXT_EXTS, _store_ext

    legit = sorted(TEXT_EXTS | IMAGE_EXTS | DOC_EXTS)
    assert legit == sorted(STORE_EXTS), "STORE_EXTS 与三类之和不一致，白名单漂了"
    for ext in legit:
        assert _store_ext("报告" + ext) == ext, f"合法扩展名被改动：{ext}"
        assert _store_ext("a/b" + ext) == ext, f"带目录前缀时改动：{ext}"
    # 大写必须规范化成小写（.PNG 存成 .png 是既有行为，不许退）
    assert _store_ext("X.PNG") == ".png"
    # 文档转文本那一步直接赋 ".txt"，它必须在白名单里，否则 doc 上传会落成怪名字
    assert ".txt" in STORE_EXTS


def test_store_ext_normalises_odd_shapes_to_no_extension():
    """怪形状一律洗成 ""（而不是原样拼进路径），且绝不含分隔符。

    负向锁：把 _store_ext 换成裸 `os.path.splitext(...)[1].lower()`，
    带分隔符/超长/控制字符那几条会拿到非空值而红。
    """
    from app.core.uploads import STORE_EXTS, _store_ext

    for desc, name in ODD_EXTS.items():
        got = _store_ext(name)
        assert "/" not in got and "\\" not in got, f"{desc}：洗出来的值还带分隔符 {got!r}"
        assert "\x00" not in got and "\n" not in got, f"{desc}：洗出控制字符 {got!r}"
        assert len(got) <= len(".markdown"), f"{desc}：扩展名过长 {got!r}"
        assert got == "" or got in STORE_EXTS, f"{desc}：洗出来的值不在白名单 {got!r}"


@pytest.mark.parametrize("name", [
    "../../etc/passwd.txt", "a/../../b.txt", "..\\..\\notes.txt",
    "子目录/x.txt", "空格 名.txt"])
def test_separators_in_filename_still_store_inside_dir(tmp_path, name):
    """文件名里带 .. 或分隔符，落点仍然必须在上传目录**里面**。

    判据不是"save 没炸"，而是目录里正好多出一个文件、且它 resolve 之后还在目录内、
    并且目录之外（含 tmp_path 的上一级）没有凭空多出任何东西。

    负向锁：把 save() 里 `ext = _store_ext(filename)` 换回裸 splitext 并删掉落点
    那一次 `_within_dir` 检查，带分隔符那几条会在目录外落下文件而红。
    """
    from app.core.uploads import UploadStore

    up = tmp_path / "uploads"
    store = UploadStore(directory=str(up))
    pub = store.save(name, "正文".encode("utf-8"), "text/plain", owner="u_1")

    stored = store._index[pub["id"]]["path"]
    real_up = Path(os.path.realpath(up))
    real_stored = Path(os.path.realpath(stored))
    assert real_stored.is_relative_to(real_up), f"落点跑到目录外：{stored}"
    assert os.path.isfile(stored), "文件应当真的存在"
    files = [p for p in up.iterdir() if p.name != "index.json"]
    assert len(files) == 1, f"上传目录里应当只有一份附件，拿到 {[p.name for p in files]}"
    # 落盘名由 uuid 主导：客户端那串一个字都不进文件名
    assert pub["id"] in files[0].name
    assert ".." not in files[0].name and os.sep not in files[0].name
    assert not (tmp_path / "etc").exists(), "在 tmp_path 之上造出了目录"


def test_within_dir_primitive_rejects_the_prefix_trap(tmp_path):
    """直接钉"为什么不是 startswith"：隔壁的 uploads_evil 必须判为目录外。

    这是整道闸门唯一一种"看起来在里面"的形态，也是本项目最容易写错的一处
    （`str(path).startswith(dir)` 读起来完全正确，错得完全不显眼）。

    负向锁：把 _within_dir 里的 commonpath 换成 `real_item.startswith(real_base)`，
    本条立刻红（uploads_evil 被判成在里面）。
    """
    from app.core.uploads import _within_dir

    up = tmp_path / "uploads"
    up.mkdir()
    (tmp_path / "uploads_evil").mkdir()
    inside = up / "a.txt"
    inside.write_text("x", encoding="utf-8")
    trap = tmp_path / "uploads_evil" / "b.txt"
    trap.write_text("x", encoding="utf-8")

    assert _within_dir(str(up), str(inside)) is not None, "合法路径被误拒"
    assert _within_dir(str(up), str(trap)) is None, "前缀陷阱被放行（startswith 之错）"
    assert _within_dir(str(up), str(tmp_path / "secret.txt")) is None
    assert _within_dir(str(up), str(up / ".." / "secret.txt")) is None
    # 一个绝对一个相对 / 不同盘符：commonpath 抛 ValueError，要收成 None 而不是炸出来
    assert _within_dir(str(up), "Z:\\somewhere\\x.txt") is None
    assert _within_dir(str(up), "relative/x.txt") is None
    assert _within_dir(str(up), "") is None
    assert _within_dir(str(up), None) is None
    # 目录自己不算"目录里的附件"：没有这一条时闸门会放行它，全靠调用方那句
    # isfile(dir) 恰好 False 兜住——判据不能寄存别人的返回值。
    assert _within_dir(str(up), str(up)) is None, "上传目录自身应当判为不可用"


def test_within_dir_accepts_the_same_file_written_two_ways(tmp_path):
    """同一个文件的多种写法（多余的一个 `.`、join 出来的、Windows 换大小写）不许被误拒。

    这一条守的是别把闸门写成"什么都拒"的假安全：误拒的代价是用户的附件凭空消失，
    那正是 v0.33 读路径刻意不复验的理由（见 providers._validate 的注释）。

    负向锁：把 normcase 换成无条件 `.lower()`，POSIX 那半红（含大写的真路径被洗掉）；
    把 normcase 整个删掉，Windows 那半红（大小写不同的同一文件被判成目录外）。
    """
    from app.core.uploads import _within_dir

    up = tmp_path / "uploads"
    up.mkdir()
    inside = up / "a.txt"
    inside.write_text("x", encoding="utf-8")

    assert _within_dir(str(up), str(up / "." / "a.txt")) is not None
    assert _within_dir(str(up), os.path.join(str(up), "a.txt")) is not None
    if os.path.normcase("A") == "a":            # Windows：路径大小写不敏感
        assert _within_dir(str(up).upper(), str(inside)) is not None, \
            "目录名换大小写就把真附件判成外面"
        assert _within_dir(str(up), str(inside).upper()) is not None, \
            "同一文件换大小写就被误判成目录外"
    else:                                       # POSIX：normcase 必须是恒等
        assert os.path.normcase(str(inside)) == str(inside), \
            "POSIX 上把路径洗成小写会误拒含大写的真附件"

