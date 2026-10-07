"""附件上传存储：文本/代码、图片与 PDF。

落盘在 data/uploads/，文件名一律用生成的 uuid，绝不采用客户端给出的路径或
文件名，避免目录穿越与互相覆盖；原始文件名只作为元数据保存用于界面展示。

图片先按文件头字节**认格式**（MAGIC），再按该格式的**结构**走一遍——两件事都必须过。
只认文件头是不够的：BM 只有 2 字节、JPEG 的 SOI 只有 3 字节，
`b"BM" + b"<html>…"` 与 `b"\xff\xd8\xff<script>…"` 都能过签名判定，于是任意二进制
（谁都能随手造一段）被当成图片存下来，再以 data: URI 送模型反复烧多模态 token。

所以签名之后还有第二道：`_verify_image` 按各格式真正必需的字段读一遍
（PNG 的 chunk 长度与 CRC、BMP 的 DIB 头尺寸与宽高、GIF 的逻辑屏幕宽高、
JPEG 走到 SOF 帧头、WebP 的 RIFF 容器与 chunk 尺寸），缺一即拒。
文档类早就这么做（_docx_document_xml 的解压上限 + XXE 防护），图片类此前是真校验
与只信文件头的差距，这一轮把它补平。

为什么不用现成解码库：`requirements.txt` 里没有 Pillow，而为一个 10MB 上限的上传
校验引入一个新的二进制依赖不划算（PyInstaller 冻结 EXE 还会多一个隐藏导入点，
见 _docx_text 的注释）。`imghdr` 更不能用——它在 3.11 起废弃、3.13 已移除，
且它本身也只是文件头嗅探器（实测 `imghdr.what(b"BM" + b"<html>"*10)` 就返回 b"bmp"），
比现有 MAGIC 强不了半点。这里手写的量级也就几十行标准库。

PDF 与 .docx 在上传时即抽取为纯文本再落盘，因此下游的预览与上下文注入不必区分来源格式。

每条索引记录带 owner：这里存的是别人上传的合同、账单、论文，索引一旦共用，
"知道 id"就等于"拿到文件"，所以读、下载、删除都必须先过归属。
"""
import base64
import io
import json
import os
import re
import threading
import uuid
import zipfile
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime

from app.core.atomic_write import write_json_atomic
from app.core.owner_backfill import backfill_owner
from app.core.paths import data_root
# 历史数据认给谁，这个决定只能有一份：会话与附件同属"身份层之前建的东西"，
# 两处各写一个字符串迟早会对不上（对上不了的话，老用户的附件就永远找不回了）。
# 这里只借常量，不依赖 SessionStore 的任何行为。
from app.session.session_store import SessionStore

MAX_TEXT_BYTES = 1 * 1024 * 1024          # 文本/代码 1MB
MAX_IMAGE_BYTES = 10 * 1024 * 1024        # 图片 10MB
MAX_DOC_BYTES = 10 * 1024 * 1024          # 文档类按原始体积计，解析后转文本再截断
# 字节流进内存前的硬上限：save() 里按 kind 分档的校验发生在**整blob已读进内存**
# 之后，谁都能用一个任意文件名把进程顶掉内存。读满 max(三档)+1 字节就拒收，
# 上限以内的文件仍按 save() 原有的分档口径逐类判定，行为不变。
MAX_UPLOAD_BYTES = max(MAX_TEXT_BYTES, MAX_IMAGE_BYTES, MAX_DOC_BYTES)
MAX_INJECT_CHARS = 20000                  # 注入模型的文本上限，防止长文件撑爆上下文

TEXT_EXTS = {".txt", ".md", ".markdown", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
             ".csv", ".tsv", ".log", ".ini", ".cfg", ".html", ".css", ".c", ".h",
             ".cpp", ".java", ".go", ".rs", ".sh", ".bat", ".ps1", ".sql", ".xml"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
DOC_EXTS = {".pdf", ".docx"}
DOC_MIMES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# 文件头签名 -> 规范 MIME，避免伪造扩展名或 Content-Type
MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def _png_ok(blob: bytes) -> bool:
    """走一遍 chunk 链：长度自洽，每个 chunk 的 CRC32 对得上，最后以 IEND 收尾。

    CRC 这一条是关键：PNG 的 chunk 是 (长度, 类型, 数据, CRC32)，攻击者要伪造就得
    把整条 chunk 重算一遍，而"文件头对了、chunk 是乱码"这种最常见的伪造成本为零。
    只读 IHDR 的宽高是不够的——`PNG_MAGIC + b"\x00" * 64` 的宽高都是 0，
    但真正骗得过解码器的是那些宽高看着正常、数据却根本不是图片的字节。
    """
    if len(blob) < 8 + 12 + 12:          # 签名 + 至少一个 chunk 头 + IEND
        return False
    pos = 8
    seen_ihdr = False
    total = len(blob)
    while pos + 12 <= total:
        length = int.from_bytes(blob[pos:pos + 4], "big")
        ctype = blob[pos + 4:pos + 8]
        # 长度自述必须落在文件内，否则后面全是越界读
        if length > total - pos - 12:
            return False
        data = blob[pos + 8:pos + 8 + length]
        crc = int.from_bytes(blob[pos + 8 + length:pos + 12 + length], "big")
        if zlib.crc32(ctype + data) & 0xFFFFFFFF != crc:
            return False
        if not seen_ihdr:
            # 第一个 chunk 必须是 IHDR，且宽高都非 0（0×0 不是任何解码器认的图）
            if ctype != b"IHDR" or length != 13:
                return False
            if int.from_bytes(data[0:4], "big") == 0 or int.from_bytes(data[4:8], "big") == 0:
                return False
            seen_ihdr = True
        elif ctype == b"IEND":
            return length == 0
        pos += 12 + length
    return False                            # 没走到 IEND：文件被截断了


# BMP 的 DIB 头尺寸：OS/2 位图头 12、其余（BITMAPINFOHEADER 及其后继）40/52/56/64/108/124。
# 只放行这组，"BM" 后面随便接 HTML 时按 little-endian 读出来的是乱码，落在表外即拒。
_BMP_DIB_SIZES = frozenset((12, 16, 40, 52, 56, 64, 108, 124))


def _bmp_ok(blob: bytes) -> bool:
    if len(blob) < 26:
        return False
    dib = int.from_bytes(blob[14:18], "little")
    if dib not in _BMP_DIB_SIZES:
        return False
    # BITMAPINFOHEADER 起的宽高在 18/22，是有符号 32 位（top-down 位图高度为负）
    if dib == 12:                            # OS/2 位图头：宽高是 16 位且偏移不同
        width = int.from_bytes(blob[18:20], "little")
        height = int.from_bytes(blob[20:22], "little")
    else:
        width = int.from_bytes(blob[18:22], "little", signed=True)
        height = int.from_bytes(blob[22:26], "little", signed=True)
    if width == 0 or height == 0:
        return False
    # 文件头自报的体积必须容得下这份 DIB（bfSize 在 2..6）
    declared = int.from_bytes(blob[2:6], "little")
    return declared == 0 or len(blob) >= declared


def _gif_ok(blob: bytes) -> bool:
    """逻辑屏幕宽高（6..10）非 0，且以 0x3B  trailer 收尾。"""
    if len(blob) < 13:
        return False
    if int.from_bytes(blob[6:8], "little") == 0 or int.from_bytes(blob[8:10], "little") == 0:
        return False
    return blob[-1:] == b"\x3b"


# SOFn：除 DHT(c4)/JPG(c8)/DAC(cc) 外的全部 SOF 变体，都带同样的宽高字段
_JPEG_SOF = frozenset((0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF))


def _jpeg_ok(blob: bytes) -> bool:
    """从 SOI 逐段走到 SOF 帧头：高度(2) 宽度(2) 都要非 0。

    熵编码段（SOS 之后）里 0xFF 一定被转义成 0xFF00，所以逐段解析在 SOS 处就该停；
    再往后按段长走只会走进压缩数据里，那是解码器的事，不是这里要判的。
    """
    if len(blob) < 4:
        return False
    pos = 2
    total = len(blob)
    while pos + 4 <= total:
        if blob[pos] != 0xFF:
            return False
        marker = blob[pos + 1]
        if marker == 0xD8:                  # 嵌套 SOI 不是合法段
            return False
        length = int.from_bytes(blob[pos + 2:pos + 4], "big")
        if length < 2 or length > total - pos - 2:
            return False
        if marker in _JPEG_SOF:
            if length < 8:
                return False
            height = int.from_bytes(blob[pos + 5:pos + 7], "big")
            width = int.from_bytes(blob[pos + 7:pos + 9], "big")
            return width != 0 and height != 0
        if marker == 0xDA:                  # SOS：后面是熵编码数据，不再逐段解析
            return False
        pos += 2 + length
    return False


def _webp_ok(blob: bytes) -> bool:
    """RIFF 容器：自报尺寸必须容得下文件，且首个 chunk 是 VP8/VP8L/VP8X 之一。

    只查 blob[8:12] == b"WEBP" 的话，`RIFF....WEBP` + 任意尾巴都能过——那 4 个
    WEBP 字节和 4 个尺寸字节同样是可以随手填的。
    """
    if len(blob) < 16:
        return False
    declared = int.from_bytes(blob[4:8], "little")
    if declared < 4 or declared > len(blob) - 8:
        return False
    return blob[12:16] in (b"VP8 ", b"VP8L", b"VP8X")


_IMAGE_STRUCTURE = {
    "image/png": _png_ok,
    "image/jpeg": _jpeg_ok,
    "image/gif": _gif_ok,
    "image/bmp": _bmp_ok,
    "image/webp": _webp_ok,
}


def _verify_image(filename: str, blob: bytes, mime: str) -> None:
    """签名过了不等于是一张图。这里按 mime 走一遍该格式的结构，缺一即拒。"""
    checker = _IMAGE_STRUCTURE.get(mime)
    if checker is None:                     # 新增签名忘了配校验器：宁可拒，不放行
        raise UploadError("这个图片格式暂不支持校验，请另存为 PNG 或 JPEG 后再上传")
    try:
        ok = checker(blob)
    except Exception:                        # 校验器自己都不该把异常透给调用方
        ok = False
    if not ok:
        raise UploadError(
            f"{filename} 不是有效的图片内容"
            f"（文件头像是 {mime}，但按该格式解不开）")


def _default_dir() -> str:
    env_path = os.getenv("UPLOAD_DIR")
    if env_path:
        return os.path.abspath(env_path)
    return os.path.join(data_root(), "data", "uploads")


class UploadError(Exception):
    pass


def _pdf_text(blob: bytes) -> str:
    """提取 PDF 全文。fitz 延迟导入：缺少该组件时只让本次上传失败，不拖垮模块。"""
    try:
        import fitz
    except ImportError as e:
        raise UploadError(f"服务器缺少 PDF 解析组件（pip install pymupdf）：{e}")
    try:
        doc = fitz.open(stream=blob, filetype="pdf")
    except Exception as e:
        raise UploadError(f"PDF 无法打开：{type(e).__name__}: {str(e)[:120]}")
    try:
        if doc.needs_pass:
            raise UploadError("该 PDF 已加密，无法提取文本")
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
# 正文解压后的上限。原始体积已被 MAX_DOC_BYTES(10MB) 卡住，但 zip 里一个条目
# 就能压到几 GB（zip bomb），所以读的时候按解压后的字节数封顶。
# 16MB 约合几百万字，而注入模型的只有 MAX_INJECT_CHARS 那么多——再大只是白吃内存。
MAX_DOCX_XML_BYTES = 16 * 1024 * 1024
# Word/WPS 写出的 document.xml 只有 <?xml?> 声明，从不带 DTD 或实体定义。
# 出现这两种声明就说明文件是手工构造的：内部实体会被解析器展开（billion laughs），
# 外部实体会让服务器去取远端资源。一律拒绝，而不是"试着解析看看"。
# 整份都要查不能只查开头——DOCTYPE 前允许有任意长的注释，卡前 4KB 会被绕过。
DOCX_FORBIDDEN = re.compile(rb"<!\s*(?:doctype|entity)", re.IGNORECASE)


def _docx_document_xml(blob: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            if "word/document.xml" not in z.namelist():
                raise UploadError("该 .docx 缺少正文（word/document.xml 不存在），"
                                  "可能是 .xlsx/.pptx 改了扩展名")
            with z.open("word/document.xml") as f:
                # 按解压后的字节数封顶，多读 1 字节就用来判定"超了"。
                # 不信任 file_size：那是 zip 头里自报的，改小它就能带着真炸弹过关。
                xml = f.read(MAX_DOCX_XML_BYTES + 1)
    except UploadError:
        raise
    except RuntimeError as e:  # 加密条目：zipfile 只抛裸 RuntimeError
        raise UploadError("该 .docx 设了打开密码，无法提取文字。请去掉密码或另存一份再传") from e
    except (zipfile.BadZipFile, ValueError, zlib.error) as e:
        raise UploadError("该 .docx 打不开（不是有效的 zip 容器），可能是老版 .doc —— "
                          "请用 Word/WPS 另存为 .docx 后再传") from e
    if len(xml) > MAX_DOCX_XML_BYTES:
        raise UploadError("该 .docx 正文解压后远超正常文档大小，已拒绝解析")
    return xml


def _docx_text(blob: bytes) -> str:
    """提取 .docx 正文，只用标准库。

    不引 python-docx：冻结版 EXE 少一个隐藏导入点（这类依赖在打包后"本地能跑、
    线上炸"是本项目反复踩过的），而且这里只要文字，zip 里的 word/document.xml
    顺着 w:p 走一遍就够——表格单元格本身也是 w:p，所以表里的内容一起读到。
    """
    xml = _docx_document_xml(blob)
    if DOCX_FORBIDDEN.search(xml):
        raise UploadError("该 .docx 正文声明了 XML 实体或 DTD，出于安全拒绝解析")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise UploadError(f".docx 正文无法解析：{e}") from e

    lines = []
    for para in root.iter(DOCX_NS + "p"):
        parts = []
        for node in para.iter():
            tag = node.tag
            if tag == DOCX_NS + "t":
                parts.append(node.text or "")
            elif tag == DOCX_NS + "tab":
                parts.append("\t")
            elif tag == DOCX_NS + "br":
                parts.append("\n")
        text = "".join(parts)
        if text.strip():
            lines.append(text)
    return "\n".join(lines)


DOC_EXTRACTORS = {".pdf": _pdf_text, ".docx": _docx_text}


def detect_kind(filename: str, blob: bytes):
    """返回 (kind, mime)。kind 为 text / image / doc，无法识别则抛 UploadError。"""
    ext = os.path.splitext(filename or "")[1].lower()

    if ext in IMAGE_EXTS:
        mime = next((m for sig, m in MAGIC if blob.startswith(sig)), None)
        if mime is None and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            mime = "image/webp"
        if mime is None:
            raise UploadError(f"{filename} 不是有效的图片内容（文件头无法识别）")
        # 签名只认出"像哪张图"，还要结构走一遍才认它是图
        _verify_image(filename, blob, mime)
        return "image", mime

    if ext in TEXT_EXTS:
        return "text", "text/plain"

    if ext in DOC_EXTS:
        # 内容是否真是该格式交给解析器判定，不必再维护一份文件头签名
        return "doc", DOC_MIMES[ext]

    raise UploadError(
        f"不支持的文件类型 {ext or '(无扩展名)'}。"
        f"文本/代码支持 {len(TEXT_EXTS)} 种扩展名，图片支持 {', '.join(sorted(IMAGE_EXTS))}，"
        f"文档支持 {', '.join(sorted(DOC_EXTS))}")


class UploadStore:
    # 与 SessionStore 同一个常量：迁移只负责把历史数据认给 bootstrap 身份，
    # 它不是任何方法的默认参数（见 save/get/delete 的 owner）。
    LEGACY_OWNER = SessionStore.LEGACY_OWNER

    def __init__(self, directory: str = None):
        self.dir = os.path.abspath(directory or _default_dir())
        self.index_path = os.path.join(self.dir, "index.json")
        self._lock = threading.Lock()
        self._index = {}
        os.makedirs(self.dir, exist_ok=True)
        self._load()

    def _load(self):
        if not os.path.isfile(self.index_path):
            return
        try:
            with open(self.index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError) as e:
            self._quarantine(f"附件索引损坏（{e}）")
            return
        if not isinstance(data, dict):
            # 能 parse、但顶层不是对象（列表/字符串/数字）：上面那行 except 抓不到它。
            self._quarantine(f"附件索引形状不对（顶层是 {type(data).__name__}，应为对象）")
            return
        self._index = data
        self._backfill_owner()

    def _quarantine(self, why: str):
        """把读不懂的索引先挪开，再允许以空表启动。

        为什么不能只是 print 一句然后继续：`index.json` 是**唯一**的
        id → 磁盘文件 映射，条目里存着 path、owner、原始文件名。以空表启动后，
        下一次 save() 就会 _flush() 出一份只有一条记录的索引——所有历史附件的
        文件还在磁盘上，却再没有任何人认得出该把谁的文件给谁，也没法再找回属主
        （归属校验会把它们一律当成不存在）。既不备份也不拒绝，等于把"索引坏了"
        升级成"附件全丢了"。所以这里与 session_store、auth 两个兄弟存储对齐：
        改名留证 + 显式告警。
        """
        backup = self.index_path + ".corrupt"
        try:
            os.replace(self.index_path, backup)
            print(f"⚠️ {why}，已备份为 {backup}，历史附件本次不可见，"
                  f"请从该备份文件恢复后再重启（数据文件 {self.index_path}）")
        except OSError as e:
            print(f"⚠️ {why}，但备份失败（{e}）：{self.index_path} 未被挪走，"
                  "请先手工复制一份出来再重启，否则下一次上传会覆盖它")

    def _backfill_owner(self):
        """身份层之前的附件都是本机管理员自己传的，认给他。

        回填的具体规矩（只在真的缺 owner 时执行一次、字段齐了一个字节都不写、
        备份或原子写回失败一律向上抛）与会话存储共用一份实现，见
        app/core/owner_backfill.py。

        ⚠️ 本方法是导入期跑的：模块级 `store = UploadStore()` 一被执行就迁移
        $UPLOAD_DIR（未设置时是仓库真实的 data/uploads/）。见该行的注释。
        """
        backfill_owner(self._index, self.index_path, entity="附件",
                       legacy_owner=self.LEGACY_OWNER, flush=self._flush)

    def _flush(self):
        # replace 原子不等于落盘：公共函数里 fsync 在前、replace 在后，断电丢不了索引。
        write_json_atomic(self.index_path, self._index)

    def save(self, filename: str, blob: bytes, claimed_mime: str = "",
             *, owner: str) -> dict:
        if not blob:
            raise UploadError("文件内容为空")

        kind, mime = detect_kind(filename, blob)
        ext = os.path.splitext(filename or "")[1].lower()
        display_size = len(blob)

        limit = {"text": MAX_TEXT_BYTES, "image": MAX_IMAGE_BYTES, "doc": MAX_DOC_BYTES}[kind]
        if len(blob) > limit:
            raise UploadError(
                f"{filename} 超过上限 {limit // 1024 // 1024 or limit // 1024}MB"
                f"（实际 {len(blob) // 1024}KB）")

        if kind == "doc":
            text = DOC_EXTRACTORS[ext](blob)
            if not text.strip():
                hint = ("可能是扫描版（整页图片）PDF，需要先做 OCR" if ext == ".pdf"
                        else "可能整篇都是贴图，没有可选中的文字")
                raise UploadError(f"{filename} 未提取到文字，{hint}")
            # 转成文本后走既有的预览与注入链路，模型看到的仍是可读文字
            blob, ext, kind, mime = text.encode("utf-8"), ".txt", "text", "text/plain"

        upload_id = uuid.uuid4().hex[:16]
        stored = os.path.join(self.dir, upload_id + ext)

        with open(stored, "wb") as f:
            f.write(blob)

        record = {
            "id": upload_id,
            "name": os.path.basename(filename or "unnamed"),
            "kind": kind,
            "mime": mime or claimed_mime,
            "size": display_size,
            "path": stored,
            "owner": owner,
            "created_at": datetime.now().isoformat(),
        }
        with self._lock:
            self._index[upload_id] = record
            self._flush()
        return self.public(record)

    def get(self, upload_id: str, owner: str):
        """非属主拿到 None，与"附件不存在"同一句话：id 是 16 位十六进制，
        403 就等于把"这个 id 是真的"送出去。"""
        with self._lock:
            record = self._index.get(upload_id)
        if not record or record.get("owner") != owner:
            return None
        if not os.path.isfile(record["path"]):
            return None
        return record

    def public(self, record: dict) -> dict:
        out = {k: record[k] for k in ("id", "name", "kind", "mime", "size", "created_at")}
        if record["kind"] == "text":
            out["preview"] = self.read_text(record["id"], owner=record["owner"],
                                            max_chars=400)
        return out

    def read_text(self, upload_id: str, *, owner: str,
                  max_chars: int = MAX_INJECT_CHARS) -> str:
        record = self.get(upload_id, owner)
        if not record or record["kind"] != "text":
            return ""
        with open(record["path"], "r", encoding="utf-8", errors="replace") as f:
            text = f.read(max_chars + 1)
        if len(text) > max_chars:
            return text[:max_chars] + f"\n…（已截断，原文件 {record['size']} 字节）"
        return text

    def data_uri(self, upload_id: str, *, owner: str):
        record = self.get(upload_id, owner)
        if not record or record["kind"] != "image":
            return None
        with open(record["path"], "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f"data:{record['mime']};base64,{b64}"

    def delete(self, upload_id: str, owner: str) -> bool:
        if self.get(upload_id, owner) is None:
            return False
        with self._lock:
            record = self._index.pop(upload_id, None)
            if record is None:
                return False
            try:
                os.remove(record["path"])
            except OSError:
                pass
            self._flush()
            return True


# ⚠️ 导入期副作用：这一行不只是读 $UPLOAD_DIR 下的索引，它会在索引缺 owner 时
# **改写用户真实的数据文件**，并在旁边落下一个 index.json.bak-<时间戳>。
# $UPLOAD_DIR 未设置时解析到仓库真实的 data/uploads/，所以任何脚本、REPL、
# 测试都必须"先定 env，再 import app.core.uploads"（本任务的一次迁移演练正是
# 漏了这一步，把真实索引给迁了）。fail-fast 是刻意的：宁可启动失败，也不让
# 没有归属的记录被校验放行。改成惰性构造留给 Task 8，这一轮先付注释这笔钱。
store = UploadStore()


def build_user_content(text: str, attachment_ids: list, supports_vision: bool,
                       owner: str):
    """把附件拼进用户消息。

    文本/代码并入正文文本，这样写进会话历史后追问时上下文不丢；图片只在模型
    声明支持视觉时走多模态数组，否则明确报错——静默丢弃图片会让用户以为模型"看
    不懂"。

    owner 由调用方（路由）从凭据里取，绝不从附件 id 反查是谁传的：附件 id 是
    可以被人塞进请求的，谁能引用它不等于谁拥有它。
    """
    body = (text or "").strip()
    images = []

    for upload_id in attachment_ids or []:
        record = store.get(upload_id, owner)
        if record is None:
            raise UploadError(f"附件不存在或已清理：{upload_id}")
        if record["kind"] == "text":
            block = store.read_text(upload_id, owner=owner)
            body = (body + "\n\n" if body else "") + f"[附件 {record['name']}]\n```\n{block}\n```"
        else:
            images.append(record)

    if images and not supports_vision:
        raise UploadError(
            f"当前模型不支持图片输入（{len(images)} 张附件）。"
            "请在「设置 → 模型服务」改用带视觉的模型，或只发送文本附件。")

    if not images:
        return body

    parts = [{"type": "text", "text": body or "请查看图片"}]
    for record in images:
        parts.append({"type": "image_url",
                      "image_url": {"url": store.data_uri(record["id"], owner=owner)}})
    return parts
