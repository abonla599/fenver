"""v0.33 三条已确认问题的负向锁（每条都做过"删掉修复让它红"的变异验证）。

三条问题的共同形状：**出口是公开的，而闸门只验了最便宜的那一层**。

1. 图片上传只认文件头（app/core/uploads.py）。`b"BM"` 2 字节、`\\xff\\xd8\\xff`
   3 字节，谁都能随手造一段 `b"BM" + b"<html>…"` 冒充图片，落地后以 data: URI
   送模型反复烧多模态 token。同文件的 docx 分支早就是真校验（解压上限 + XXE），
   图片类此前只信文件头——这一轮把两者的差距补平。
2. `.dockerignore` 只挡了 10 行，`dist`/`build`/`.worktrees` 一个都没挡。
   构建上下文是仓库根（根 Dockerfile `COPY backend/ ./backend/`），于是
   18.9MB 的 EXE、86MB 的 build/、101MB 的 3 份源码副本全在上传量里。
3. `/health` 免鉴权，却在 OK 分支回显 provider 的 label 与 model——把"我们用的是
   哪家上游"发给任何能打到这个端点的人（memory_manager.py 对嵌入后端是同一口径）。

为什么不用现成解码库锁第 1 条：requirements.txt 里没有 Pillow，而为一个 10MB 上限
的上传校验引新二进制依赖不划算。`imghdr` 更锁不住——实测
`imghdr.what(b"BM" + b"<html>"*10)` 就返回 b"bmp"，它和原 MAGIC 是同一类检查，
3.11 起废弃、3.13 已移除。所以第 1 条锁的是"结构走查"这个行为本身（各格式的必需
字段），而不是某个库的函数名：换实现只要行为不变，这些锁就该照样绿。
"""
import fnmatch
import json
import struct
import sys
import zlib
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

import pytest

from app.core import selfcheck
from app.core.selfcheck import BROKEN, NOT_CONFIGURED, OK, run
from app.core.uploads import IMAGE_EXTS, UploadError, detect_kind


# --------------------------------------------------------------- 图片夹具 --
# 全部自己造，不引 Pillow、也不依赖仓库里有没有这么一张图（fixture 依赖仓库里的
# 素材，那素材一被优化掉测试就跟着红，而且红得毫无道理）。

def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + ctype + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF))


def real_png(width: int = 2, height: int = 2) -> bytes:
    """一只真的 PNG：IHDR/IDAT/IEND 三块都带正确 CRC，IDAT 是 zlib 压过的扫描线。"""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(raw))
            + _png_chunk(b"IEND", b""))


def real_bmp(width: int = 2, height: int = 2) -> bytes:
    """一只真的 BMP：BITMAPINFOHEADER(40) + 按 4 字节对齐的像素行。"""
    row = (width * 3 + 3) // 4 * 4
    pixels = b"".join((b"\x00\x00\xff" * width).ljust(row, b"\x00") for _ in range(height))
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels),
                      2835, 2835, 0, 0)
    return b"BM" + struct.pack("<IHHI", 14 + len(dib) + len(pixels), 0, 0, 54) + dib + pixels


def real_gif(width: int = 2, height: int = 2) -> bytes:
    """一只真的 GIF：逻辑屏幕描述 + 图像描述符 + LZW 最小码长 + 0x3B trailer。"""
    return (b"GIF89a" + struct.pack("<HHBBB", width, height, 0, 0, 0)
            + b"\x2c" + struct.pack("<HHHHB", 0, 0, width, height, 0)
            + b"\x02\x02\x44\x01\x00" + b"\x3b")


def real_jpeg() -> bytes:
    """一只真的 JPEG。用 PyMuPDF 造——它是 requirements.txt 里已有的依赖
    （uploads.py 自己就 import fitz 抽 PDF 文本），不是为测试新装的。"""
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 2, 2))
    pix.set_rect(pix.irect, (255, 255, 255))
    return pix.tobytes("jpg")


def real_webp() -> bytes:
    """一只真的 WebP（VP8X 扩展头）。PyMuPDF 不编码 webp，所以自己按 RIFF 拼：
    'RIFF' + 总长 + 'WEBP' + 'VP8X' + 10 字节画布尺寸，其余 VP8/VP8L 位流留空
    ——结构完整到能过容器校验，而本条锁判的正是"容器尺寸与 chunk 自洽"。"""
    vp8x = b"\x10\x00\x00\x00" + (1).to_bytes(3, "little") + (1).to_bytes(3, "little")
    body = b"WEBP" + b"VP8X" + struct.pack("<I", len(vp8x)) + vp8x
    return b"RIFF" + struct.pack("<I", len(body)) + body


REAL = {
    "image/png": real_png,
    "image/bmp": real_bmp,
    "image/gif": real_gif,
    "image/jpeg": real_jpeg,
    "image/webp": real_webp,
}
EXT_OF = {"image/png": ".png", "image/bmp": ".bmp", "image/gif": ".gif",
          "image/jpeg": ".jpg", "image/webp": ".webp"}


# =========================================================== 锁 1：伪图片 ==

def test_fake_bmp_that_is_really_html_is_rejected():
    """本轮的核心那一条：`b"BM"` 只有 2 字节，谁都能在后面接任意内容。

    攻击面有限（落地后只以 data: URI 送模型或 attachment 下载，没有存储型 XSS），
    但"任意二进制 + 一个 .bmp 扩展名"就够反复烧多模态 token 了。
    """
    blob = b"BM" + b"<html><script>alert(1)</script></html>" * 8
    assert blob.startswith(b"BM"), "夹具本身得先过文件头那道闸，否则测的不是我们想测的"
    with pytest.raises(UploadError) as e:
        detect_kind("shot.bmp", blob)
    assert "不是有效的图片内容" in str(e.value), str(e.value)


def test_fake_jpeg_that_is_really_a_script_is_rejected():
    """JPEG 的 SOI 是 3 字节 `\xff\xd8\xff`，同样随手可造。"""
    blob = b"\xff\xd8\xff" + b"<script>fetch('/v1/admin/users')</script>" * 8
    with pytest.raises(UploadError) as e:
        detect_kind("shot.jpg", blob)
    assert "不是有效的图片内容" in str(e.value), str(e.value)


@pytest.mark.parametrize("mime,make", sorted(REAL.items()))
def test_real_images_of_every_supported_format_still_pass(mime, make):
    """反向的那一半：真图必须照旧收下。

    只锁拒绝不锁通过，等于把上传功能一起关掉也算通过——所以每种格式各来一张真的。
    """
    kind, got = detect_kind(f"shot{EXT_OF[mime]}", make())
    assert kind == "image", (mime, kind)
    assert got == mime, f"{mime} 的魔数判定被改坏了：认出成了 {got}"


def test_magic_signature_mapping_is_unchanged_for_every_format():
    """锁住"MAGIC 认格式"这一层没被动过。

    结构校验是**加在签名之后**的第二道，不是替换：签名认出的 mime 必须与改动前
    逐一对上（png/jpeg/gif87a/gif89a/bmp/webp），否则扩展名与 mime 的对应关系
    就变了，而 data: URI 的 mime 是要交给模型的。
    """
    from app.core.uploads import MAGIC

    assert MAGIC == (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"BM", "image/bmp"),
    ), "MAGIC 表被改了：扩展名→mime 的对应关系变了"


def test_gif87a_is_still_recognised_and_still_validated():
    """GIF87a 只在 MAGIC 里出现过一次，很容易在重构时被当成冗余删掉。"""
    blob = (b"GIF87a" + struct.pack("<HHBBB", 2, 2, 0, 0, 0)
            + b"\x2c" + struct.pack("<HHHHB", 0, 0, 2, 2, 0)
            + b"\x02\x02\x44\x01\x00" + b"\x3b")
    assert detect_kind("old.gif", blob) == ("image", "image/gif")
    # 同一个头，尾巴不是 0x3B trailer：必须拒
    broken = blob[:-1] + b"\x00"
    with pytest.raises(UploadError):
        detect_kind("old.gif", broken)


def test_a_png_with_a_corrupted_chunk_crc_is_rejected():
    """PNG 的 chunk 自带 CRC32——这是标准白送的一道完整性校验，不用白不用。

    改一个像素的字节而不重算 CRC，任何解码器都会当场报错：这正是"真解码校验"与
    "文件头嗅探"的分界线。
    """
    blob = bytearray(real_png())
    blob[30] ^= 0xFF                       # 动 IDAT 里的一个字节
    with pytest.raises(UploadError):
        detect_kind("shot.png", bytes(blob))


def test_a_png_without_iend_is_rejected():
    """签名 + IHDR 都对，但没有 IEND 收尾 = 文件被截断了。"""
    blob = real_png()
    iend = blob.rfind(b"IEND")
    assert iend > 0
    with pytest.raises(UploadError):
        detect_kind("shot.png", blob[:iend - 4])


def test_a_zero_dimension_image_is_rejected():
    """宽或高为 0 不是任何解码器认的图（0×0 的 PNG 照样能过文件头与 chunk CRC）。"""
    ihdr = struct.pack(">IIBBBBB", 0, 2, 8, 2, 0, 0, 0)
    raw = b"\x00" + b"\xff\x00\x00" * 0
    blob = (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(raw)) + _png_chunk(b"IEND", b""))
    with pytest.raises(UploadError):
        detect_kind("shot.png", blob)


def test_a_webp_whose_riff_size_exceeds_the_file_is_rejected():
    """`RIFF....WEBP` 后面接什么都行——那 4 个 WEBP 字节和 4 个尺寸字节同样可填。
    真正能判的是"自报的尺寸容得下文件吗"。
    """
    blob = bytearray(real_webp())
    blob[4:8] = struct.pack("<I", len(blob) + 4096)     # 自报比文件大得多
    with pytest.raises(UploadError):
        detect_kind("shot.webp", bytes(blob))


def test_an_unknown_image_signature_is_still_rejected_the_old_way():
    """原有错误路径不许变形：认不出签名时仍是那句"文件头无法识别"。"""
    with pytest.raises(UploadError) as e:
        detect_kind("evil.png", b"not really an image at all")
    assert "文件头无法识别" in str(e.value), str(e.value)


def test_non_image_extensions_keep_their_original_paths():
    """结构校验只该加在图片分支上。文本与文档类一个字节都不许变——
    docx 分支本来就有自己的一套（解压上限 + XXE），别让图片这道闸顺手改了它们。
    """
    assert detect_kind("notes.txt", "第一行\n第二行".encode()) == ("text", "text/plain")
    assert detect_kind("a.csv", b"a,b\n1,2") == ("text", "text/plain")
    assert detect_kind("x.docx", b"not a zip at all")[0] == "doc"
    with pytest.raises(UploadError) as e:
        detect_kind("payload.exe", b"MZ\x90\x00" + b"\x00" * 40)
    assert ".exe" in str(e.value)


def test_image_extension_set_is_unchanged():
    """前端选择器的 accept 与后端这份白名单必须一致（test_uploads.py 也锁了同一件事）。"""
    assert IMAGE_EXTS == {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


# ========================================================= 锁 2：dockerignore ==

DOCKERIGNORE = ROOT / ".dockerignore"


def _dockerignore_patterns():
    return [line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def _ignored_by_dockerignore(rel_path: str) -> str | None:
    """近似 docker 语义。**带尾斜杠的模式只匹配目录**，所以只对祖先目录求值，
    绝不对最后一个文件名分量求值——否则 build*/ 会把 buildinfo.py 打成 blocked，
    那正是本文件要防的误伤（app/core/buildinfo.py 在 COPY 路径里）。

    Docker 自己那套语义在 Go 侧（patternmatcher），这里用 fnmatch 近似：足以
    锁住"这些大件被挡了、这些源码没被挡"这个判据。
    """
    parts = rel_path.split("/")
    for pat in _dockerignore_patterns():
        dir_only = pat.endswith("/")
        bare = pat.rstrip("/")
        if "/" in bare:
            if fnmatch.fnmatch(rel_path, bare):
                return pat
            if any(fnmatch.fnmatch("/".join(parts[:i]), bare) for i in range(1, len(parts))):
                return pat
        elif dir_only:
            if any(fnmatch.fnmatch(p, bare) for p in parts[:-1]):
                return pat
        elif any(fnmatch.fnmatch(p, bare) for p in parts):
            return pat
    return None


@pytest.mark.parametrize("rel_path,why", [
    ("dist/run_backend/run_backend.exe", "1.3GB 的桌面版产物"),
    ("backend/dist/ai-assistant.exe", "18.9MB，就在 COPY 路径 backend/ 里面"),
    ("build/fenver-snapshot/Dockerfile", "86MB，内含一整份仓库快照"),
    ("build/gitleaks.exe", "本地工具二进制"),
    ("build_new/run_backend", "run_backend.spec 写的工作目录名"),
    (".worktrees/v032-d1/Dockerfile", "101MB，3 份完整源码副本"),
    ("dist_new/run_backend", "另一次构建的产物"),
    ("deploy_local/swap_backend_v028.py", "一次性换包脚本"),
    ("logs/backend.log", "运行时日志，已从 data/ 迁到项目根 logs/"),
    ("logs/backend.log.1", "轮转份：*.log 不覆盖 .log.1，靠 logs/ 目录条目挡"),
])
def test_dockerignore_blocks_the_bulky_build_artifacts(rel_path, why):
    assert _ignored_by_dockerignore(rel_path), f"{rel_path}（{why}）仍会进构建上下文"


@pytest.mark.parametrize("rel_path,why", [
    ("backend/requirements.txt", "Dockerfile 第 16 行 COPY 的依赖清单"),
    ("backend/app/main.py", "应用入口"),
    ("backend/app/core/uploads.py", "本轮改的文件"),
    ("backend/app/core/buildinfo.py", "文件名以 build 开头——build*/ 绝不能打中它"),
    ("backend/app/core/selfcheck.py", "本轮改的文件"),
    ("backend/app/web/static/app.js", "前端主包"),
    ("backend/app/web/site/probe.html", "直连探测页"),
    ("backend/tests/test_uploads.py", "测试也在镜像里（COPY backend/ 是整目录）"),
    ("Dockerfile", "根 Dockerfile 自身"),
    ("docker-compose.yml", "编排文件"),
    ("run_backend.spec", "PyInstaller 打包描述，文件名 dist/build 都不沾但语义相关"),
    ("docker/sandbox/Dockerfile", "沙箱镜像 ai-sandbox 的唯一构建描述"),
    ("docker/sandbox/Dockerfile.node", "沙箱镜像 ai-sandbox-node 的唯一构建描述"),
])
def test_dockerignore_does_not_block_anything_the_image_needs(rel_path, why):
    """这一半比挡掉那半更重要：挡错一个文件，镜像当场构建不出来或跑不起来。

    docker/ 尤其要留意——根 Dockerfile 虽然不 COPY 它，但 docker/sandbox/ 下那两个
    是 app/sandbox/sandbox_manager.py 里 ai-sandbox / ai-sandbox-node 两个镜像的唯一
    构建描述。挡掉它们，镜像还在、沙箱还能用，但**再也重建不出来**，而且要等到真要
    修沙箱的那天才发现。
    """
    hit = _ignored_by_dockerignore(rel_path)
    assert hit is None, f"{rel_path}（{why}）被 {hit!r} 挡住了"


def test_dockerignore_keeps_the_original_ten_entries():
    """原有那几条不能被顺手删掉——它们各挡一样东西（.git/.env/.venv/chroma_db）。"""
    pats = _dockerignore_patterns()
    for need in ("__pycache__", "*.pyc", ".git", ".env", ".venv", "venv",
                 "*.egg-info", "chroma_db"):
        assert need in pats, f"{need} 从 .dockerignore 里不见了"


def test_dockerignore_directory_patterns_are_directory_only():
    """`dist`/`build` 不带尾斜杠会按**文件名**匹配，于是 buildinfo.py、run_backend.spec
    这类真·源码会被一起打中。这一条把"必须带尾斜杠"锁死，别让后人"顺手清理"。
    """
    for pat in _dockerignore_patterns():
        if pat.rstrip("/") in ("dist", "build") or pat.startswith(("dist", "build")):
            if "*" not in pat:
                assert pat.endswith("/"), f"{pat} 缺尾斜杠：会误伤同名文件"


# ============================================================== 锁 3：/health ==

def _provider_store(monkeypatch, *, records, resolved=None, error=None):
    """把 providers.store 换成一个可控替身：records 决定 all()，resolved 决定 resolve()。"""
    from app.core import providers

    class _Store:
        keys_path = "provider_keys.json"

        @staticmethod
        def all():
            return records

        @staticmethod
        def resolve():
            if error is not None:
                raise error
            return resolved

    monkeypatch.setattr(providers, "store", _Store)


def test_health_never_echoes_the_provider_label_or_model(monkeypatch, client):
    """/health 在 EXEMPT_PATHS 里免鉴权，而 label 与 model 是"我们用的是哪家上游"。

    memory_manager.py 对嵌入后端是同一口径（这个仓库是公开的，"哪家在替我们做
    嵌入"本身就是不想公开的信息）。所以免鉴权端点上只能出现条数。
    """
    secretish = {"id": "p1", "label": "公司网关-主", "model": "qwen-max-2026-07-secret"}
    _provider_store(monkeypatch, records=[secretish], resolved=secretish)

    body = client.get("/health").text
    assert "公司网关-主" not in body, "/health 把 provider 的名字回显给了免鉴权的调用方"
    assert "qwen-max-2026-07-secret" not in body, "/health 把上游模型标识回显给了免鉴权的调用方"
    assert "p1" not in body, "/health 把 provider 的 id 也回显了"


def test_health_provider_item_reports_a_count_instead(monkeypatch):
    """改的不是"有没有信息"，而是"信息的颗粒度"：条数留着，身份不留。

    运维仍然能看出"配了几条"，而"是哪一条"去日志里看（见下面那条锁）。
    """
    secretish = {"id": "p1", "label": "公司网关-主", "model": "m-x"}
    _provider_store(monkeypatch, records=[secretish, secretish], resolved=secretish)
    item = run()["checks"]["模型服务"]
    assert item["status"] == OK
    assert item["detail"] == "已配置 2 条", item


def test_health_does_not_echo_the_provider_error_text(monkeypatch):
    """ProviderError 原文描述的是配置形状，回显它等于把密钥库现状播给路人。"""
    from app.core.providers import ProviderError

    err = ProviderError("3 条配置都缺密钥，其中第 2 条的 base_url 是 https://内网网关/v1")
    _provider_store(monkeypatch, records=[], error=err)

    item = run()["checks"]["模型服务"]
    assert item["status"] == BROKEN
    assert "内网网关" not in item["detail"], item
    assert str(err) not in item["detail"], item
    assert "没有可用的模型服务" in item["detail"], item


def test_health_response_shape_is_unchanged_for_the_probes(monkeypatch, client):
    """改返回**内容**不许改返回**结构**——消费方还在。

    真正解析这个端点的：deploy/watchdog.ps1（读 checks.*.status 与 .detail）、
    deploy_local/swap_backend_v028.py（读 status 与 checks.*.status）、
    `app.js` 与 Android `Api.kt` 读 `build` 的那一处。status/build/checks 三个键与
    每项的 status/detail 两个字段必须都在，watchdog 那条 foreach 才不会空转。
    """
    secretish = {"id": "p1", "label": "网关", "model": "m"}
    _provider_store(monkeypatch, records=[secretish], resolved=secretish)

    body = client.get("/health").json()
    assert set(body) >= {"build", "status", "checks"}, body.keys()
    assert body["status"] in (OK, "degraded", NOT_CONFIGURED, BROKEN)
    assert body["checks"], "checks 空了：watchdog 的 foreach 会静默空转"
    for label, item in body["checks"].items():
        assert set(item) >= {"status", "detail"}, (label, item)
        assert item["status"] in (OK, "degraded", NOT_CONFIGURED, BROKEN), (label, item)
        assert item["detail"], f"{label} 报了状态却没说为什么"


def test_the_log_path_still_names_the_broken_provider(monkeypatch, capsys):
    """收敛的是出口，不是可观测性：日志里必须还能看出是哪个 provider、哪个模型。

    这是本轮最容易顺手改坏的地方——把 _check_provider_config 里的 label/model 删掉
    就完事的话，运维就只剩"没配"三个字，还得自己去翻 providers.json 才知道缺的是
    密钥还是占位符。所以 log_startup_summary 走的是另一条路，那条路上 label/model
    与 ProviderError 原文都还在（日志只进 server.log，不出公网）。
    """
    from app.core import providers

    class _Store:
        keys_path = "provider_keys.json"

        @staticmethod
        def all():
            return []

        @staticmethod
        def resolve():
            raise providers.ProviderError("已配 2 条，但都没有可用密钥")

    monkeypatch.setattr(providers, "store", _Store)
    selfcheck.log_startup_summary()
    out = capsys.readouterr().out
    assert "模型服务" in out, out
    assert "broken" in out, out
    # 具体原因（日志里保留 ProviderError 原文）
    assert "都没有可用密钥" in out, "运维在日志里看不到具体坏在哪：\n" + out


def test_the_log_path_still_names_the_active_provider(monkeypatch, capsys):
    """OK 那一侧同理：日志里要看得见"现在在用哪家"，只是 /health 上不给人看。"""
    from app.core import providers

    class _Store:
        keys_path = "provider_keys.json"

        @staticmethod
        def all():
            return [{"id": "p1", "label": "公司网关-主", "model": "qwen-max", "api_key": "k"}]

        @staticmethod
        def resolve():
            return {"id": "p1", "label": "公司网关-主", "model": "qwen-max"}

    monkeypatch.setattr(providers, "store", _Store)
    # 密钥库那项会 broken（keys_path 不存在），从而让摘要真的打出来
    selfcheck.log_startup_summary()
    out = capsys.readouterr().out
    assert "模型服务" in out, out
    assert "公司网关-主" in out, "日志里应当保留 provider 名字，否则等于没有可观测性：\n" + out
    assert "qwen-max" in out, out
    # 但它绝不能出现在 /health 的返回体里——那是本轮锁 3 的正题
    assert "公司网关-主" not in json.dumps(run(), ensure_ascii=False)
