"""v0.35.0 #143①：CSP 的 script-src 用**运行时哈希**罩住页面里的内联脚本。

为什么主判据是"头里的 sha256 与响应正文逐字节算出来的一致"，而不是"头里有
script-src"：后者在写死哈希、或者哈希量与正文不是同一份文本时照样绿；而浏览器
不这么想——对不上就是一条拒绝，整页脚本全哑，且控制台之外没有任何症状。

判据的四个方向都查：
1. 正文里每一段内联脚本的哈希都在头里（少了任何一段，那段脚本就是被拒的那一段）；
2. 头里每一条 sha256 都对应正文里的一段（防止旧哈希混进来当装饰）；
3. 换水印（等价于重启后构建号变化）时哈希跟着正文一起换，不是写死的旧值；
4. 这条判据自己有牙：正文改一个字节，重算的哈希就不再出现在头里。

最后一条是"反向证明"：不做它，前三条可能只是把同一段空集合比了两次。
"""
import base64
import hashlib
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.web import web_router

client = TestClient(app)

# 测试自己的解析器：不从 web_router 借 inline_script_hashes，否则实现和判据一起错
# 也会绿。这里要量的正是"服务端发出去的字节"。
SCRIPT_RE = re.compile(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
                       re.IGNORECASE | re.DOTALL)
SRC_RE = re.compile(r"\bsrc\s*=", re.IGNORECASE)
TYPE_RE = re.compile(r"""\btype\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE)
HASH_RE = re.compile(r"'sha256-([A-Za-z0-9+/=]+)'")

JS_TYPES = {"", "text/javascript", "application/javascript", "module",
            "text/ecmascript", "application/ecmascript"}


def _inline_scripts(html: str):
    """页面里会被浏览器执行的内联脚本正文（与 CSP 判据同一个射程）。"""
    for match in SCRIPT_RE.finditer(html):
        attrs = match.group("attrs")
        if SRC_RE.search(attrs):
            continue
        type_match = TYPE_RE.search(attrs)
        if type_match:
            mime = next(g for g in type_match.groups() if g is not None)
            if mime.split(";", 1)[0].strip().lower() not in JS_TYPES:
                continue
        yield match.group("body")


def _sha256_b64(text: str) -> str:
    """按浏览器的口径算：HTML 解析时 CRLF/CR 先统一成 LF，再对脚本正文做哈希。

    这里不加这一步，测试与服务端会一起按磁盘原始字节算 CRLF 哈希、一起绿；而浏览器
    拿到的是规范化后的文本，头里的哈希对不上，整页脚本被拒。2026-10-21 更新日志页
    就是这样停在“正在载入…”的。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")


def _parse_directives(csp: str) -> dict:
    """CSP 头 → {指令名: 值}；同名指令重复即判死。

    为什么要在这里判死：CSP 规范（CSP3 §2.2.1）规定重复的同名指令**只取第一条**，
    浏览器照此执行；而 `dict[name] = value` 取的是**末值**。于是"同名重复、恶意值在
    前"的头（`frame-src *; … frame-src 'none'`）在浏览器里 frame-src 仍是 `*`
    （WebView 桥会挂到每个 frame），按末值解析却读成 'none'——这条链就是旧解析器
    被骗绿的那条路。宁可判"头本身不合法"，也不替浏览器猜它认哪一条。
    """
    out = {}
    for part in csp.split(";"):
        part = part.strip()
        if part:
            name, _, value = part.partition(" ")
            assert name not in out, (
                f"CSP 里 {name} 出现了不止一次（浏览器只认第一条，后写的会被忽略）：{csp!r}")
            out[name] = value.strip()
    return out


def _directives(res) -> dict:
    return _parse_directives(res.headers.get("content-security-policy") or "")


def _header_hashes(res) -> list:
    script_src = _directives(res).get("script-src", "")
    return HASH_RE.findall(script_src)


def _assert_header_matches_body(res):
    """主判据，双向：头 ⇄ 正文。任何一边多一条少一条都红。"""
    body_hashes = [_sha256_b64(s) for s in _inline_scripts(res.text)]
    assert body_hashes, "这一页一段内联脚本都没解析出来，判据就空了"
    assert sorted(_header_hashes(res)) == sorted(body_hashes), (
        "CSP 头里的 sha256 与正文里的内联脚本对不上：\n"
        f"  头: {_header_hashes(res)}\n  正文: {body_hashes}"
    )
    return body_hashes


def test_the_inline_script_hash_in_the_header_matches_the_served_body():
    """/app 是主判据：唯一那段内联脚本（含水印）的哈希必须与正文逐字节一致。"""
    res = client.get("/app/")
    assert res.status_code == 200
    assert "__ASSET_TOKEN__" not in res.text, "水印占位没替换：哈希算的是模板而不是正文"

    body_hashes = _assert_header_matches_body(res)
    assert len(body_hashes) == 1, "/app 的内联脚本不止一段：每一段都得单独进头"

    # 哈希罩住的必须就是带着**当下水印**的那一段——这正是不能写死哈希的原因。
    (script,) = list(_inline_scripts(res.text))
    token = web_router.asset_token()
    assert f'window.__ASSETS__ = "{token}"' in script
    assert _sha256_b64(script) == body_hashes[0]


def test_the_policy_is_complete_and_script_src_stays_strict():
    """② 完整的一组指令都在场，且 script-src 里没有 'unsafe-inline'（有它哈希白算）。"""
    directives = _directives(client.get("/app/"))
    for name in ("default-src", "script-src", "style-src", "img-src", "connect-src",
                 "frame-src", "object-src", "base-uri", "form-action"):
        assert name in directives, f"CSP 少了 {name}"

    assert directives["default-src"] == "'self'"
    assert directives["frame-src"] == "'none'"
    assert directives["object-src"] == "'none'"
    assert directives["base-uri"] == "'self'"
    assert directives["form-action"] == "'self'"

    script_src = directives["script-src"]
    assert script_src.startswith("'self' ")
    assert "'unsafe-inline'" not in script_src
    assert "'unsafe-eval'" not in script_src
    assert HASH_RE.search(script_src), "script-src 没有哈希：内联脚本会被浏览器拒绝"

    # 这两条不是"越紧越好"：blob: 是附件预览的取处（app.js/api.js 的 objectURL），
    # 两个搜索源是「直连搜索」从浏览器真发出去的请求。收紧它们要有替代实现。
    img_sources = directives["img-src"].split()
    assert "data:" in img_sources and "blob:" in img_sources
    connect = directives["connect-src"]
    assert "https://api.bochaai.com/v1/web-search" in connect
    assert "https://api.tavily.com/search" in connect


def test_the_hash_follows_the_watermark_instead_of_being_frozen(monkeypatch):
    """换一版（等价于重启后 mtime 变）：哈希必须跟着正文换，而不是停在旧值。"""
    before = client.get("/app/")
    before_hashes = _assert_header_matches_body(before)

    monkeypatch.setattr(web_router, "_TOKEN", "20990101010101")
    after = client.get("/app/")
    assert 'window.__ASSETS__ = "20990101010101"' in after.text, "换水印后正文没跟上"

    after_hashes = _assert_header_matches_body(after)
    assert after_hashes != before_hashes, "水印换了而哈希没换：旧哈希会让新页面整页脚本被拒"


def test_the_304_reuses_the_policy_it_computed_for_the_same_body():
    """30 秒缓存那条路上，304 带的 CSP 必须与 200 同一次渲染的结果一致。"""
    first = client.get("/app/")
    second = client.get("/app/", headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304
    assert second.headers["content-security-policy"] == first.headers["content-security-policy"]


def test_the_directive_parser_refuses_a_duplicate_directive():
    """反例常驻：同名指令重复时解析必须判死，不许按"后写的那个"取值。

    CSP 规范（CSP3 §2.2.1）规定只取第一条，浏览器照此执行；`dict` 赋值取末值。
    `frame-src *; … frame-src 'none'` 这种"恶意值在前"的头在浏览器里 frame-src 仍
    是 `*`，按末值解析却读成 'none'——这条链就是旧解析器被骗绿的那条。这条用例把
    "解析器不许再退回 dict 赋值"变成常驻的牙，而不是只靠一次手工变异实验。
    """
    with pytest.raises(AssertionError):
        _parse_directives("frame-src *; default-src 'self'; frame-src 'none'")
    # 正向对照：没有重复时照常解析，免得上面那条红是因为解析器根本不会工作。
    assert _parse_directives("frame-src 'none'; default-src 'self'") == {
        "frame-src": "'none'", "default-src": "'self'"}


def test_every_html_page_gets_hashes_for_its_own_inline_scripts():
    """头是全站共用的（site_headers），但哈希必须按每一页自己的正文算。

    各页的内联脚本段数不同（入口页、更新日志页、探测页各有各的正文，这里不数它们）；
    /admin/ 没有内联脚本。任何一页拿到别页的哈希，它自己的脚本就是被拒的那一段。
    """
    for path in ("/", "/changelog", "/site/probe.html", "/admin/"):
        res = client.get(path)
        assert res.status_code == 200, f"{path} 拿不到 200（{res.status_code}）"
        body_hashes = [_sha256_b64(s) for s in _inline_scripts(res.text)]
        assert sorted(_header_hashes(res)) == sorted(body_hashes), (
            f"{path} 的 CSP 哈希与它自己的正文对不上：\n"
            f"  头: {_header_hashes(res)}\n  正文: {body_hashes}"
        )
    assert _header_hashes(client.get("/admin/")) == [], "/admin/ 没有内联脚本，不该多出哈希"


def test_only_the_probe_page_may_connect_beyond_self_and_the_search_sources():
    """探测页单独一档 connect-src；其余页面保持严格。

    探测页的 fetch 目标是用户填的 base URL（默认 api.deepseek.com），这一页的
    1–6 项直连探测全靠它。connect-src 若只剩 'self' + 两个搜索源，那些探测会在
    浏览器里全部被拒——页面上只会看到"跑了但全红"，而 changelog 还在教用户跑它。
    所以这一页放开 https（不含 http/ws）；其余页面照旧，一个都不许跟着放开。
    """
    probe = _directives(client.get("/site/probe.html"))["connect-src"].split()
    assert set(probe) == {"'self'", "https:"}, \
        f"探测页的 connect-src 不是「'self' + https:」：{probe}"

    strict = {"'self'", "https://api.bochaai.com/v1/web-search",
              "https://api.tavily.com/search"}
    for path in ("/app/", "/", "/changelog", "/admin/"):
        got = set(_directives(client.get(path))["connect-src"].split())
        assert got == strict, (
            f"{path} 的 connect-src 不该跟着探测页放开：{got}——"
            "放开 https 只属于 /site/probe.html 这一页的既定用途")
        assert "https:" not in got, f"{path} 出现了通配 https:，全站口径被放开了"


def test_crlf_and_lf_scripts_hash_the_same_because_the_browser_normalizes_them():
    """行尾不是脚本文本的一部分：同一段脚本的 CRLF 与 LF 版本必须得到同一个哈希。

    这条不依赖本机检出的行尾，也不借服务端实现自证：两种文本各算一次，要求相等；
    同时用一个只改行尾的反例确认它不是把空集合比了两次。Windows 检出（CRLF）与
    Linux CI（LF）必须给出同一份 CSP，否则同一版在一边能跑、另一边整页脚本被拒。
    """
    lf = "<script>\nvar answer = 42;\n</script>"
    crlf = lf.replace("\n", "\r\n")
    assert crlf != lf
    assert web_router.inline_script_hashes(lf) == web_router.inline_script_hashes(crlf), (
        "同一段脚本只换行尾就换了 CSP 哈希：浏览器会按 LF 算，头里的 CRLF 哈希必被拒")
    assert web_router.inline_script_hashes(lf) == [_sha256_b64(lf[len("<script>"):-len("</script>")])]


def test_a_one_byte_change_in_the_body_breaks_the_match():
    """反向证明：这条判据量的不是'头里有个 sha256 的形状'，而是正文本身。

    把正文里的内联脚本改一个字节，重算出来的哈希就不在头里了。少了这一条，上面的
    等价断言可能只是把同一段空集合比了两次。
    """
    res = client.get("/app/")
    header_hashes = set(_header_hashes(res))
    (script,) = list(_inline_scripts(res.text))
    mutated = script.replace("window.__ASSETS__", "window.__ASSETX__", 1)
    assert mutated != script
    assert _sha256_b64(mutated) not in header_hashes
