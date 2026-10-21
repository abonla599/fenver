"""静态资源水印与缓存：一次开门为什么从 11 趟回源变成几乎不用回源。

判据全部从"服务端发出去的那份 HTML"里现读，不另列一份资源清单——列了就等于第二份
真相：加一个资源时人想起改 HTML 却忘了改清单，或者反过来，测试绿而线上漏盖章。
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.web import web_router

client = TestClient(app)

# 与 web_router._REF 认的那一批扩展名对应。两边各写一次是有意的：这个测试要抓的正是
# "目录里冒出一个新类型，而盖章的规则不知道"。
STAMPED = (".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".webmanifest")
# 不盖章的本地文件：HTML 是被盖章那一批的出处（它自己只许缓存 30 秒），APK 是动态代取的，
# .md 是随包带出去的第三方许可说明（`static/NOTICE.md`），没有任何页面引用它。
# .json 是随包带出的**数据**（官网更新日志 changelog.json 与自托管清单 version.json）：
# 它们由页面按裸地址取，不参与"资源水印"那一套——改一次内容就该立刻被看到，
# 所以走的是 RevalidatingStaticFiles 的 no-cache 分支，而不是编进 URL 的 ?v=。
UNSTAMPED_OK = (".html", ".apk", ".md", ".json")

PAGES = ("/app/", "/admin/", "/")


def _refs(html: str):
    return re.findall(r'\b(?:src|href)="([^"]*)"', html)


def _path_of(ref: str) -> str:
    return ref.split("?")[0].split("#")[0]


def _local_files_in(root: str):
    out = []
    for base, _, files in __import__("os").walk(root):
        out.extend(f for f in files)
    return out


def test_every_asset_the_page_points_at_carries_the_current_watermark():
    """三个页面的每一条资源引用都必须带着当下水印，一条不漏。

    放回哪种 bug 会红：`_REF` 少认一个扩展名、`stamp_assets` 整段不调用、或者
    `site_index` 绕过 `rewritten_html` 直接回 FileResponse——那时 `/site/site.css`
    会以裸地址出现，这里当场点名。
    """
    token = web_router.asset_token()
    for page in PAGES:
        refs = _refs(client.get(page).text)
        assert refs, f"{page} 里一条引用都没有，这条测试就空了"
        for ref in refs:
            path = _path_of(ref)
            if path.rsplit("/", 1)[-1] == "sw.js":
                continue                      # worker 的地址必须固定，见 web_router 注释
            if not path.endswith(STAMPED):
                continue
            assert ref == f"{path}?v={token}", f"{page} 引用没盖当下水印：{ref!r}"


def test_a_new_file_type_in_the_static_dirs_forces_a_decision():
    """静态目录里冒出 STAMPED 之外的类型时要红——它意味着有一种资源永远不会被盖章。

    这条是给"以后加字体/图标集"准备的：`_REF` 与 HTML 都不改的话，那个文件会退回
    每次回源，而这正是本文件要消灭的东西；宁可在这里挡下来让人显式表态。
    """
    for root in (web_router.STATIC_DIR, web_router.ADMIN_DIR, web_router.SITE_DIR):
        for name in _local_files_in(root):
            suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
            assert suffix in STAMPED + UNSTAMPED_OK, \
                f"{root} 里出现了没被归类过的文件 {name}：要盖章就加进 _REF 与 STAMPED"


def test_versioned_assets_are_immutable_while_bare_and_stale_ones_revalidate():
    """带当下水印 → 一年 immutable；裸地址与旧水印 → no-cache。

    后一半是正确性的那一半：一个 URL 一旦被人存进书签、或被上一版 service worker
    预取过，它的地址就固定了。如果服务端对"任何带 ?v= 的请求"都回 immutable，
    旧 URL 会被钉死在一年——那正好复刻 2026-09-17 那次"改版没生效"。
    """
    token = web_router.asset_token()
    for path in ("/app/style.css", "/app/app.js", "/site/site.css", "/admin/admin.css"):
        bare = client.get(path)
        assert bare.status_code == 200, path
        assert bare.headers["cache-control"] == "no-cache", \
            f"{path} 不带水印却给了 {bare.headers['cache-control']!r}：旧 URL 会被钉住"
        assert bare.headers.get("etag"), f"{path} 没 ETag：no-cache 会退化成每次全量重传"

        fresh = client.get(path, params={"v": token})
        assert fresh.status_code == 200, path
        assert fresh.headers["cache-control"] == web_router.IMMUTABLE, \
            f"{path}?v={token} 仍要回源：首屏就还是十几个往返"

        stale = client.get(path, params={"v": "1970"})
        assert stale.headers["cache-control"] == "no-cache", \
            f"{path} 对旧水印也回 long cache：页面与资源会各停在一版"


def test_a_page_may_be_stale_for_at_most_the_agreed_window():
    """HTML 可以晚一步，但只能晚 30 秒——这个数字是拿"旧页面配新脚本"换来的。

    2026-09-22 定的那条边界：整页 HTML 是"重复打开"这条路上剩下的最后那一趟回源，
    给它正 max-age 才能省掉；代价是最坏情况下人拿到旧骨架跑新代码，症状是缺元素、
    点了没反应。窗口由页面自己兜住（下一条锁钉它在不在），而窗口大小按整串头钉：
    谁把 PAGE_CACHE 调大或者干脆改成 immutable，这里红，而不是等到线上偶发一次空白。
    """
    for page in PAGES:
        head = client.get(page).headers["cache-control"]
        m = re.fullmatch(r"public, max-age=(\d+)", head)
        assert m, f"{page} 的 HTML 缓存头不是说得出价钱的那一种: {head!r}"
        assert 0 < int(m.group(1)) <= 30, f"{page} 允许晚 {m.group(1)} 秒，超出谈定的窗口"
    # sw.js 反过来：它是"现在线上是哪一版"的那一个问处，缓存住就等于永远问不到新版。
    assert client.get("/app/sw.js").headers["cache-control"] == "no-cache", \
        "sw.js 被允许缓存：版本自愈那条路就断了"


def test_a_second_visit_to_the_page_answers_304_with_no_body():
    """过了那 30 秒还是要问一次，问价必须便宜：整页三十多 KB 全量重传比改动之前还慢。"""
    first = client.get("/app/")
    assert first.headers.get("etag"), "没有 ETag，条件请求无从谈起"
    second = client.get("/app/", headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 304, f"带着 ETag 仍旧全量重发：{second.status_code}"
    assert not second.content, "304 却带了正文"
    assert second.headers["cache-control"] == web_router.PAGE_CACHE, \
        "304 上没带缓存头：客户端与边缘只能按自己的启发式猜这一页能留多久"


def test_the_page_declares_which_build_it_is():
    """页面必须自报构建号，而且要和服务端当下那一版、和资源 URL 上的水印是同一个值。

    这是"HTML 允许晚 30 秒"的那根保险销：app.js 拿 `window.__ASSETS__` 去问 sw.js
    现在是多少，对不上就自己跳一次。占位没被替换、这一行被删掉、或者填进去的是别的
    数——保险就悄悄没了，而症状只是"改版后偶尔有人点不动"。
    """
    token = web_router.asset_token()
    page = client.get("/app/").text
    assert f'window.__ASSETS__ = "{token}"' in page, \
        "页面没有自报构建号（占位没替换或那一句被删了）"
    assert "__ASSET_TOKEN__" not in page, "HTML 里的占位漏替换：填号的那条路只覆盖了 sw.js"
    assert f'src="app.js?v={token}"' in page, "构建号与资源水印不是同一个出处"


def test_the_watermark_moves_with_the_files_and_both_copies_agree(monkeypatch):
    """换一版：HTML 里的引用与 sw.js 里的常量必须一起换。

    两处不同步是本项目最贵的一类形状——worker 预取的是上一版的 URL，而页面引的是
    新 URL，于是离线回退悄悄指向旧文件（效果没了，也不报错）。
    """
    html = client.get("/app/").text
    served = client.get("/app/sw.js").text
    assert "__ASSET_TOKEN__" not in served, "sw.js 里的占位没被替换：worker 拿不到水印"
    token = web_router.asset_token()
    assert f'const V = "{token}"' in served, f"sw.js 的 V 与 HTML 用的水印不一致：{token}"

    monkeypatch.setattr(web_router, "_TOKEN", "20990101010101")
    moved_html = client.get("/app/").text
    moved_sw = client.get("/app/sw.js").text
    assert f'href="style.css?v=20990101010101"' in moved_html, "换水印后 HTML 没跟上"
    assert 'const V = "20990101010101"' in moved_sw, "换水印后 sw.js 没跟上"
    assert token not in moved_html and token not in moved_sw, "两份水印同时在线"


def _sw_list(sw_text: str, name: str):
    """从 sw.js 的响应正文里取出 `const <name> = [...]` 那一串字面量。"""
    marker = f"const {name} = ["
    assert marker in sw_text, f"sw.js 里没有 {marker}：清单本身不见了"
    block = sw_text.split(marker, 1)[1].split("]", 1)[0]
    return re.findall(r'"([^"]+)"', block)


def test_the_worker_precaches_exactly_the_files_that_exist():
    """sw.js 预取清单里的每一条，都得真的取得到，而且带水印时回 immutable。

    名字写错一位（比如 vendor 下改了文件名）今天的表现是"离线时少一个文件"——不报错、
    在线时看不出。这里把它变成红的。

    这是**正向**那一半（清单 → 文件）；反过来的另一半（文件 → 清单）在下面
    `test_every_stampable_static_file_is_in_the_workers_precache_list`。
    """
    token = web_router.asset_token()
    entries = _sw_list(client.get("/app/sw.js").text, "ASSETS")
    assert len(entries) >= 10, f"预取清单读出来只有 {len(entries)} 条，解析本身大概是坏了"
    for name in entries:
        assert (Path(web_router.STATIC_DIR) / name).is_file(), f"预取清单里 {name} 不存在"
        res = client.get("/app/" + name, params={"v": token})
        assert res.status_code == 200, f"/app/{name} 取不到：{res.status_code}"
        assert res.headers["cache-control"] == web_router.IMMUTABLE


def _versioned_files():
    """STATIC_DIR 下**会被 `stamp_assets` 盖章**的文件（相对路径，posix 分隔）。

    判据不另抄一份扩展名清单，而是拿生产代码自己问：把每条相对路径拼成一条
    `<a href="…">`，`stamp_assets` 盖了章就算数。于是 `_REF` 认哪些扩展名、
    `stamp_assets` 跳过谁（sw.js 的注册地址固定）都由那一处说了算——这正是
    "加一个资源不必想起改第二处"的反向用法。
    """
    root = Path(web_router.STATIC_DIR)
    out = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        ref = f'<a href="{rel}">x</a>'
        if web_router.stamp_assets(ref) != ref:
            out.add(rel)
    return out


def _assert_worker_precaches_every_versioned_file(sw_text: str) -> None:
    """反向锁本体：服务端会盖章的每个静态文件，都必须出现在 ASSETS 里。"""
    missing = sorted(_versioned_files() - set(_sw_list(sw_text, "ASSETS")))
    assert not missing, (
        "这些文件服务端会以带水印的 immutable 地址发出去，却不在 sw.js 的 ASSETS 里："
        f"{missing}。它们不会被预缓存：离线首开取不到，而清单里的旧条目可能停在上一版。"
        "要预缓存就加进 ASSETS；确实不该预缓存的，要在 sw.js 旁边写明理由。"
    )


def test_every_stampable_static_file_is_in_the_workers_precache_list():
    """反向锁：服务端会盖章的每个静态文件，都必须出现在 sw.js 的 ASSETS 里。

    现有的两条锁都是单向的：前一条只判"清单里每条都取得到"，test_web_pwa 只判
    "列出来的取得到"。往 static/ 里放一个新 .js/.css 而不改 sw.js 时，两边都绿——
    在线时那个文件照样工作，离线时它没被预缓存，旧 shell 配上新文件，正是
    2026-09-17 那次新旧混发的形状。

    射程（只说这一头）：这只覆盖 `_REF` 认的那批扩展名、且 `stamp_assets` 不跳过的
    文件。运行时动态生成的 URL（例如 /site/android.apk 的代取）不在其中；`_REF` 之外
    的扩展名（.html、.json、.md…）也不在，它们本来就没有"加进 ASSETS"这回事。

    正向对照：今天 `_versioned_files()` 的每一项都在 ASSETS 里。下面两条钉子钉的是
    "推导本身没有空转"——`stamp_assets` 哪天整段不盖章，推导会退化成空集，仅凭差集
    为空会假绿。
    """
    servable = _versioned_files()
    assert "app.js" in servable, "推导没认出 app.js：盖章这条路整段没生效，反向锁在空转"
    assert "sw.js" not in servable, "推导把 sw.js 也算了进来：它的注册地址是固定的，不盖章"
    _assert_worker_precaches_every_versioned_file(client.get("/app/sw.js").text)


def test_the_reverse_lock_fails_red_when_one_precache_entry_is_deleted():
    """负向对照：在 sw.js 的**副本**上删掉一行 ASSETS，判据必须当场点名缺的那个文件。

    生产文件一个字不动：`client.get` 拿到的正文就是一份副本，手术只做在副本上。
    没有这一条，上面那条锁的绿灯只证明"今天恰好相等"，不证明它有能力报红。
    """
    body = client.get("/app/sw.js").text
    _assert_worker_precaches_every_versioned_file(body)   # 未动手术的同一份正文必须是绿的

    removed = "app.js"
    lines = body.splitlines(keepends=True)
    kept = [line for line in lines if line.strip() != f'"{removed}",']
    assert len(kept) == len(lines) - 1, f"副本里没找到 {removed} 那一行，负向对照没演成"
    broken = "".join(kept)

    with pytest.raises(AssertionError) as excinfo:
        _assert_worker_precaches_every_versioned_file(broken)
    assert removed in str(excinfo.value), \
        f"判据红了，但没点名被删的那个文件：{str(excinfo.value)[:200]}"


def test_stamp_assets_is_idempotent_and_leaves_the_worker_alone():
    """盖章函数自己：重复盖不叠出两个 `?v=`，`sw.js` 的地址不许变。

    幂等这一条防的是"HTML 被盖章两次"的路径（比如某个中间件循环调用）；
    sw.js 那条防的是给 worker 换地址——注册 URL 一变，旧 worker 永远收不到更新。
    """
    once = web_router.stamp_assets('<script src="app.js"></script>'
                                    '<link rel="manifest" href="manifest.webmanifest">'
                                    '<a href="/app/sw.js">w</a>'
                                    '<a href="#top">锚</a>')
    twice = web_router.stamp_assets(once)
    assert once.count("?v=") == 2, once
    assert twice == once, f"重复盖章会叠出第二个查询：{twice!r}"
    assert 'href="/app/sw.js"' in twice, "sw.js 的地址被动了"
    assert 'href="#top"' in twice, "锚点被当成资源盖了章"


# ---------- 6. `?v=` 是来客可控的输入，而它会被拼进 JS 字面量：只认服务端自己那一枚 ----------
# 这一节防的不是"今天有人打线上"，而是"下一版又写回一个不校验的 token"：水印那一条
# 占位符（`__ASSET_TOKEN__`）落在 `static/index.html:753` 的 `window.__ASSETS__ = "…"`
# 与 `sw.js` 的 `const V = "…"`，两处都是**字符串字面量内部**，所以查询串里任何
# 一个引号都是一次逃逸。写 `rewritten_html` 的人当时只想"带水印的 URL 走 immutable"，
# 没说出口的假设是"那个值是我们自己生成的"。判据与理由见 web_router._query_token。

# 三个页面 + worker：占位符只住在 /app 那两份里，`/admin/` 与 `/` 是前瞻性的锁
# （今天它们不含 `__ASSET_TOKEN__`，哪天接上查询参数就必须立刻被这节点名）。
WATERMARKED = ("/app/", "/app/sw.js")
ALL_PAGES = PAGES + ("/app/sw.js",)

# 每一个 payload 里都埋着同一个纯字母记号 `MARKER`。原因是真话：查询串经过 httpx 的
# URL 编码才送到 ASGI，引号变成 %22、尖括号变成 %3C/%3E，于是"断言那一串原文没出现"
# 会是一条永远为真的空断言（假绿）。字母记号被反射时一定原样出现在响应体里，所以
# `MARKER not in body` 是一条会红的断言。这节一共三条判据，指向同一件事：
# 记号不出现、发出的字节与不带 `?v=` 时逐字相同、页面/worker 自报的水印仍是服务端那一枚。
MARKER = "zzinjectedzz"
INJECTION_SHAPES = (
    'A"B',                                    # 用户在线上证过的那一条：一个引号截断字面量
    '1";alert(1);//',
    "1</script><script>alert(1)</script>",
    "1" + chr(10) + "2" + chr(13),             # CR/LF：响应头注入的另一半原料
    "%22evil%22",                             # 已经百分号编码过的形状
    "",                                       # 空值：本来就该走"没带"那一档
)
INJECTIONS = [MARKER + shape for shape in INJECTION_SHAPES]


def _js_watermarks(body: str):
    """响应里那几处"自报构建号"的 JS 字面量取值（可能一处都没有）。"""
    return re.findall(r'(?:window[.]__ASSETS__|const V) = "([^"]*)"', body)


def test_query_token_accepts_only_the_shape_the_server_itself_emits():
    """`_query_token` 自己：纯数字才放行，其余一律算"没有带"。

    这一条是**绕过 httpx 直接喂原始字节**的：查询串在 scope 里就是没解码的字节，
    真实来客递进来的正是这种形状（引号、`</script>`、CR/LF、百分号、非 ASCII 数字），
    而经过 HTTP 客户端的 URL 编码之后再断言"原文没出现"会绿得没有意义。
    """
    good = web_router.asset_token()
    assert re.fullmatch(r"[0-9]+", good), f"服务端自己那枚水印就不是纯数字了：{good!r}"

    def scope(raw: bytes):
        return {"query_string": raw}

    assert web_router._query_token(scope(b"v=" + good.encode())) == good
    assert web_router._query_token(scope(b"lang=zh&v=" + good.encode())) == good

    bad = [b'v=A"B', b'v=1";alert(1);//', b"v=1</script><script>alert(1)</script>",
           b"v=1" + bytes([10]) + b"2", b"v=1" + bytes([13]) + b"2", b"v=evilmark",
           b"v=", b"v=" + bytes([0xd9, 0xa1]),        # U+0661 阿拉伯-印度数字：\d 会认，[0-9] 不认
           b"v=%31", b"v=1%22X", b"v=1 2", b"v=-1", b"v=+1", b"v=1.5", b"v=1e3",
           b"v=1\x002", b"v=1'--", b"v=1 onerror=alert(1)"]
    for raw in bad:
        got = web_router._query_token(scope(raw))
        assert got == "", f"这一串原始查询参数被当成水印放行了：{raw!r} -> {got!r}"


def test_a_bogus_v_query_changes_nothing_the_server_sends_out():
    """`/app/`、`/admin/`、`/`、`/app/sw.js`：带着注入形状的 `?v=` 进来，发出去的字节一字不动。

    反面形状（这一节存在的全部理由）：`/app/?v=A"B` 让页面里出现
    `window.__ASSETS__ = "A"B";` —— 一次查询参数反射，落在别人浏览器的 JS 里。
    `/admin/` 与 `/` 今天不含那处占位符，所以那两页是前瞻性的锁：形状一旦接上查询参数，
    这里当场点名（`body == clean` 那条不依赖占位符是否存在）。
    """
    for page in ALL_PAGES:
        clean = client.get(page).text
        assert "__ASSET_TOKEN__" not in clean, f"{page} 的占位符没被替换：这条判据是空转的"
        for payload in INJECTIONS:
            res = client.get(page, params={"v": payload})
            assert res.status_code == 200, f"{page}?v={payload!r} 回 {res.status_code}"
            body = res.text
            assert body == clean, \
                f"{page} 对 ?v={payload!r} 发了与不带查询参数时不同的字节：来客可控的输入进了响应体"
            assert MARKER not in body, f"{page} 把注入记号 {MARKER!r} 原样发了出去"
            assert "__ASSET_TOKEN__" not in body, f"{page} 漏替换占位符"
            for value in _js_watermarks(body):
                assert re.fullmatch(r"[0-9]+", value), \
                    f"{page} 的构建号字面量里出现了 {value!r}：它可以夹带引号与标签"


def test_the_watermark_the_page_carries_is_still_the_servers_own(monkeypatch):
    """不合格的那一枚被拒之后，页面与 worker 必须回落到**服务端那一份**，不是空串。

    回落成空串也算"没被注入"，但它让 `window.__ASSETS__` 变成 `""`：`app.js` 的
    `staleBuild` 对账从此问不到东西，人正好落进"旧 HTML 配新 JS"那一档（缺元素、点了
    没反应）。所以这里钉的是"仍然带着当下水印"，而不是"没带别的东西"。
    """
    token = web_router.asset_token()
    for page in WATERMARKED:
        clean = client.get(page).text
        assert _js_watermarks(clean) == [token], \
            f"{page} 的自报构建号读出来不是恰好一处：{clean[:120]!r}（这节就空转了）"
        for payload in INJECTIONS:
            body = client.get(page, params={"v": payload}).text
            assert _js_watermarks(body) == [token], \
                f"{page}?v={payload!r} 的构建号不再是服务端那一枚"

    # 换一版水印之后仍然如此：判据不能只在"当前这一枚"上成立（那是恰好等于常量）。
    monkeypatch.setattr(web_router, "_TOKEN", "20990101010101")
    for page in WATERMARKED:
        for payload in INJECTIONS:
            assert _js_watermarks(client.get(page, params={"v": payload}).text) == ["20990101010101"]


def test_a_rejected_v_query_buys_no_long_cache():
    """不合格 = 当"没带水印"：进不了 HTML，也换不来一年 immutable。

    这条钉的是回落的方向：`_query_token` 判不合格回空串，而缓存那一档比的是
    "这串等不等于当下水印"，所以不认识的 `?v=` 一律 no-cache。哪天有人想"任何带 ?v= 的
    地址都敢给一年 immutable"（那正是 2026-09-17 那次改版没生效的复现形状），这条会红。
    """
    for path in ("/app/app.js", "/admin/admin.css", "/site/site.css"):
        res = client.get(path, params={"v": MARKER})
        assert res.headers["cache-control"] == "no-cache", \
            f"{path} 对不合格的 ?v= 给了 {res.headers['cache-control']!r}"
