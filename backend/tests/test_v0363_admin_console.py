"""v0.36.3 管理台第一批的静态回归锁。

这一批不改任何 Python 应用代码，只重排 front-end。行为正确性最终由浏览器与
第二批的 security 端点一起验收，但下面这些“编译器与运行时都不管”的约束必须
现在就钉住：

- 信息架构：六个一级分区（概览/账号/安全/MCP/反馈/运行）与六个导航按钮必须一一对应，
  runtime 的六个子页也必须有稳定的 hash 键；
- 安全底线：不拼 HTML、不碰任何本机存储、不引任何外站资源（这一页的全部数据都
  经同源接口，离线/断网时也不能去拉 CDN）；
- 契约：security summary/events 的路径、8 个固定事件类型、3 档 severity、
  next_before 游标与 audit 字段必须按第二批实现所依据的契约出现；
- 手机可用：44px 热区、prefers-reduced-motion、反馈正文 pre-wrap；
- 旧的 casefold 回归锁仍然留在 test_admin_page.py 里。

判语料之前一律先剥注释：解释性注释里完全可能出现被禁字样。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.web.web_router import ADMIN_DIR
from tests.test_web_pwa import _strip_html_comments, _strip_js_comments

ADMIN = Path(ADMIN_DIR)

NAV = (
    ("overview", "secOverview"),
    ("accounts", "secAccounts"),
    ("security", "secSecurity"),
    ("mcp", "secMcp"),
    ("feedback", "secFeedback"),
    ("runtime", "secRuntime"),
)
RUNTIME_KEYS = ("usage", "search", "memory", "tasks", "agent", "config")
SEC_TYPES = (
    "auth.fail", "auth.rate_limited", "auth.blocked", "admin.action",
    "tool.blocked", "approval.denied", "provider.error", "system.anomaly",
)
SEC_DETAILS = (
    "login.failed", "register.rejected", "reset.rejected", "reauth.denied",
    "limit.login", "limit.register", "limit.reset", "limit.reauth", "limit.chat",
    "limit.orchestrate", "limit.admin", "token.unknown", "csrf.missing", "unspecified",
)


def _raw(name: str) -> str:
    return (ADMIN / name).read_text(encoding="utf-8")


def _html() -> str:
    return _strip_html_comments(_raw("index.html"))


def _js() -> str:
    return _strip_js_comments(_raw("admin.js"))


def _css() -> str:
    return _raw("admin.css")


def _css_clean() -> str:
    """判"有没有这条规则"之前剥掉 CSS 注释：注释里的声明不算数。"""
    return re.sub(r"/\*[\s\S]*?\*/", "", _css())


def _css_rule(selector: str) -> str:
    """取某条选择器（逗号分隔的组里精确匹配一项）的声明块；找不到返回空串。"""
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", _css_clean()):
        selectors = [s.strip() for s in match.group(1).split(",")]
        if selector in selectors:
            return match.group(2)
    return ""


# ---------- 1. 六个一级分区与导航 ----------

def test_primary_sections_have_matching_tabs():
    html = _html()
    sections = re.findall(r'<section class="sec[^"]*" id="([^"]+)"', html)
    assert sections == [sid for _, sid in NAV], f"一级分区不对：{sections}"

    for key, sid in NAV:
        assert f'id="tab-{key}"' in html, f"导航按钮 tab-{key} 不见了"
        assert f'aria-controls="{sid}"' in html, f"tab-{key} 没指向 {sid}"
        assert f'data-goto="{sid}"' in html, f"tab-{key} 没挂到 {sid}"
    assert len(re.findall(r'data-goto="', html)) == len(NAV), "一级导航多一个少一个都会有一栏看不见"
    assert 'role="tablist"' in html
    assert 'aria-selected="true"' in html
    assert 'role="tabpanel"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-live="polite"' in html

    for sid, owner in re.findall(
            r'<section class="sec[^"]*" id="([^"]+)" role="tabpanel" aria-labelledby="([^"]+)"', html):
        assert any(owner == "tab-" + key for key, _ in NAV), f"{sid} 的 aria-labelledby 指错"
    assert len(re.findall(r'<section class="sec[^"]*" id="', html)) == len(NAV)


def test_primary_nav_buttons_carry_the_route_key_they_navigate_to():
    """导航靠文档级的 `[data-route]` 处理器跳转；`data-goto` 只负责高亮。

    删掉某一颗按钮的 `data-route` 时五个按钮里只有一个变哑：点击不改 hash，
    `data-goto`/aria 的锁却照绿。这里按顺序钉住 (data-route, data-goto) 与 NAV
    一一对应，并把 data-goto 和 section id 的对应关系从"集合相等"收紧到顺序相等。
    """
    html = _html()
    tags = re.findall(r'<button[^>]*\bclass="nav-btn[^"]*"[^>]*>', html)
    assert len(tags) == len(NAV), f"一级导航按钮数量不对：{len(tags)}"
    pairs = []
    for tag in tags:
        route = re.search(r'data-route="([^"]+)"', tag)
        goto = re.search(r'data-goto="([^"]+)"', tag)
        assert route, f"导航按钮少了 data-route，点了不会跳：{tag[:120]}"
        assert goto, f"导航按钮少了 data-goto，选中态不会动：{tag[:120]}"
        pairs.append((route.group(1), goto.group(1)))
    assert pairs == list(NAV), f"data-route / data-goto 与分区路由键对不上：{pairs}"
    secs = re.findall(r'<section class="sec[^"]*" id="([^"]+)"', html)
    assert [goto for _, goto in pairs] == secs, "导航的顺序与分区顺序不一致"


def test_runtime_subpages_are_all_present():
    html = _html()
    for key in RUNTIME_KEYS:
        assert f'data-sub="{key}"' in html, f"运行子页 {key} 没有入口"
        assert f'id="subtab-{key}"' in html
    assert len(re.findall(r'data-sub="', html)) == 6


def test_pure_icon_buttons_carry_aria_labels():
    """纯图标按钮没有可读文字：没有 aria-label 时读屏只剩“按钮”。"""
    html = _html()
    for tag in re.findall(r"<button\b[^>]*>", html):
        if "data-icon=" in tag and "data-label=" not in tag:
            assert "aria-label=" in tag, f"纯图标按钮缺 aria-label：{tag[:120]}"


# ---------- 2/10. casefold 回归锁 ----------

def test_casefold_stays_out_and_lowercase_stays_in():
    js = _js()
    assert ".casefold(" not in js, "admin.js 用了 Python 的 casefold()，浏览器会炸"
    assert ".trim().toLowerCase()" in js, "账号搜索没有统一小写，大小写过滤会失效"


def test_original_casefold_lock_is_still_present():
    """这条锁锁的是“另一条锁不许被删”：casefold 事故的回归断言还住在旧文件里。"""
    src = Path(__file__).with_name("test_admin_page.py").read_text(encoding="utf-8")
    assert ".casefold(" in src, "test_admin_page.py 里那条 casefold 回归锁被删了"


def test_unsafe_requests_send_csrf_header_and_keep_cookie_credentials():
    """请求层两条安全底线：Cookie 必须随同源请求走，不安全方法必须带 X-CSRF。

    后端 authz 对 Cookie-only 的不安全请求要求这个头；前端只在 UNSAFE 集合内加，
    GET 多带一个头不致命，但把集合写空或无条件加都说明这条防线被人改过。
    """
    js = _js()
    unsafe = re.search(r"const UNSAFE = new Set\(\[([^\]]*)\]\)", js)
    assert unsafe, "UNSAFE 集合不见了"
    assert set(re.findall(r'"([A-Z]+)"', unsafe.group(1))) == \
        {"POST", "PUT", "PATCH", "DELETE"}, "UNSAFE 不再恰好是四个写方法"

    req = _function_body("req", js)
    assert req, "req 不像原来的形状了，这条锁要跟着改而不是删"
    assert 'credentials: "same-origin"' in req, "fetch 不再携带同源 Cookie"
    assert re.search(r'if \(UNSAFE\.has\(method\)\)\s*headers\["X-CSRF"\] = "1";', req), \
        "X-CSRF 不再只在 UNSAFE 方法上附加"
    assert req.count('headers["X-CSRF"]') == 1, "req 里出现了第二处 X-CSRF 赋值"


# ---------- 3/4. 不拼 HTML、不碰本机存储 ----------

def test_admin_js_has_no_html_sinks():
    js = _js()
    for needle in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function"):
        assert needle not in js, f"admin.js 出现了 {needle}"


def test_admin_js_only_clears_the_legacy_token_from_local_storage():
    """新凭据只住 httpOnly Cookie；本机存储唯一允许的引用是清旧版明文那一次。

    旧版把令牌写进 localStorage 的 accessToken（82318778 那一版第 18-23 行）。
    从没打开过旧页的用户浏览器里可能还留着它，所以保留一次 removeItem；除此之外
    不许出现任何读写，尤其不许恢复写入。
    """
    js = _js()
    for needle in ("sessionStorage", "document.cookie"):
        assert needle not in js, f"admin.js 碰到了 {needle}"
    refs = [line.strip() for line in js.splitlines() if "localStorage" in line]
    assert len(refs) == 1, f"localStorage 引用不止一处（只许清旧版遗留）：{refs}"
    assert 'localStorage.removeItem("accessToken")' in refs[0], \
        f"唯一的 localStorage 引用不是清 accessToken：{refs[0]}"
    assert "setItem" not in js and ".accessToken =" not in js, "页面又把凭据写回本机存储了"


# ---------- 5. 安全页契约 ----------

def _function_body(name: str, js: str) -> str:
    """取一个顶层函数体；找不到就返回空串，由调用方断言。"""
    match = re.search(r"function " + name + r"\([^)]*\)\s*\{([\s\S]*?)\n  \}", js)
    return match.group(1) if match else ""


def test_security_endpoints_and_enums_match_the_contract():
    """两处同名片段必须各归各的函数体：概览那条命中不能替安全页作证。

    `summary?window=` 同时出现在概览（24h 固定）与安全页；`limit=50` 同时出现在
    反馈与安全事件；`e.status === 404` 也同时出现在两边。只在全文里搜字符串时，
    把安全页那一支整段删掉，概览/反馈的副本仍让断言绿。所以这里按函数体切片判。
    """
    js = _js()
    for event_type in SEC_TYPES:
        assert '"' + event_type + '"' in js, f"固定事件类型 {event_type} 没有映射"
    severity = re.search(r"const SEC_SEVERITY = \{([^}]*)\}", js)
    assert severity, "SEC_SEVERITY 不见了"
    assert set(re.findall(r"(\w+):", severity.group(1))) == {"info", "warn", "alert"}
    for field in ("data.by_type", "data.by_source", "data.by_account",
                  "data.audit", "data.warnings", "next_before", "audit.size_bytes",
                  "audit.entries", "audit.last_ts"):
        assert field in js, f"契约字段 {field} 没有被消费"
    assert "Promise.allSettled" in js, "概览四张卡没有做到一张失败不影响其他"
    assert '"auth.fail"' in js and "countType" in js

    load = _function_body("loadSecurity", js)
    assert load, "loadSecurity 不像原来的形状了，这条锁要跟着改而不是删"
    assert '"/v1/admin/security/summary?window=" + encodeURIComponent(secWindow)' in load, \
        "安全页的 summary 请求不见了（概览那次不算）"
    assert "buildSecurityEventsUrl(null)" in load, "安全页首屏不再请求事件"

    url = _function_body("buildSecurityEventsUrl", js)
    assert url, "buildSecurityEventsUrl 不像原来的形状了"
    assert '"/v1/admin/security/events?"' in url, "events 端点不见了"
    assert 'parts = ["limit=50"]' in url, "事件分页不是 50 条（反馈那处不算）"
    for param in ('"type="', '"source="', '"account="', '"before="'):
        assert "parts.push(" + param in url, f"事件筛选参数 {param} 不再拼进 URL"

    events = _function_body("handleEvents", js)
    assert events, "handleEvents 不像原来的形状了"
    assert "安全事件端点尚未上线" in events, "events 404 的降级文案不见了"
    assert "e.status === 404" in events, "events 404 分支被删（概览那处不算）"

    summary = _function_body("handleSummary", js)
    assert summary, "handleSummary 不像原来的形状了"
    assert "安全日志端点尚未上线" in summary, "summary 404 的降级文案不见了"
    assert "e.status === 404" in summary, "summary 404 分支被删"


def test_security_auto_refresh_is_gated_by_route_and_visibility():
    js = _js()
    assert "visibilityState" in js
    assert "20000" in js, "自动刷新窗口不是 20 秒"
    assert "clearInterval" in js and "setInterval" in js
    assert 'route === "security"' in js


def test_security_empty_state_degrades_instead_of_blanking():
    js = _js()
    assert "安全日志端点尚未上线" in js
    assert "安全事件端点尚未上线" in js
    assert "e.status === 404" in js


def test_security_page_summarises_attacks_in_plain_language():
    """安全页先给一句人话总结，再给来源/账号/类型排行，最后才是明细。

    管理员不必先读表头再自己拼结论：窗口内发生了几次什么、主要来自哪里、
    打的是谁、有没有高危，一眼能看全。来源仍只显示网络前缀，不暴露原始 IP。
    """
    js, html, css = _js(), _html(), _css()
    assert 'id="secNarrative"' in html
    assert 'id="secTypes"' in html
    assert "主要来源" in js and "主要涉及账号" in js
    assert "条高危" in js
    assert "不显示原始 IP" in js
    assert "攻击类型" in html
    assert "最近事件" in html
    assert ".security-summary" in css


# ---------- 安全页 v0.36.4：风险横幅 / 严重度分布 / 时间线 / 原因 / 级别筛选 ----------

def test_security_attack_visuals_are_wired_into_the_summary_path():
    """三个可视化锚点必须在 HTML 里有落点、在 summary 渲染路径上被调用。

    只在文件里出现函数名不算：把 renderSecurityTimeline 定义留着、调用删掉，
    页面不会报错，时间线却永远空白。所以按 renderSecuritySummary 的函数体切片判。
    """
    js, html = _js(), _html()
    for anchor in ("secRisk", "secSeverity", "secTimeline", "secSeverityFilter"):
        assert f'id="{anchor}"' in html, f"#{anchor} 没有落点"

    summary = _function_body("renderSecuritySummary", js)
    assert summary, "renderSecuritySummary 不像原来的形状了"
    assert "renderRiskBanner(totals)" in summary, "风险横幅没有接进汇总渲染"
    assert "renderSeverityBreakdown(totals)" in summary, "严重度分布没有接进汇总渲染"
    assert "renderSecurityTimeline(" in summary, "时间线没有接进汇总渲染"
    assert "data.timeline" in summary, "时间线没有消费后端 data.timeline"

    risk = _function_body("renderRiskBanner", js)
    assert risk, "renderRiskBanner 不见了"
    assert "条高危事件，建议优先处理" in risk, "高危横幅文案不见了"
    assert "当前窗口没有高危事件" in risk, "安全态横幅文案不见了"

    timeline = _function_body("renderSecurityTimeline", js)
    assert timeline, "renderSecurityTimeline 不见了"
    assert "这个窗口没有事件" in timeline, "时间线空态不见了"
    assert 'setAttribute("aria-label"' in timeline, "柱子没有可读标签"
    assert "条（高危 " in timeline, "柱子 title/aria-label 没有带高危条数"
    assert "timeline-bar" in timeline, "没有建柱子"

    severity = _function_body("renderSeverityBreakdown", js)
    assert severity, "renderSeverityBreakdown 不见了"
    assert "sev-seg " in severity and "style.width" in severity, "严重度条不是按占比给宽度"
    assert "sev-legend" in severity, "严重度图例不见了"


def test_security_detail_labels_cover_the_backend_enum():
    js = _js()
    mapping = re.search(r"const SEC_DETAIL = \{([^}]*)\}", js)
    assert mapping, "SEC_DETAIL 不见了"
    assert set(re.findall(r'"([^"]+)":', mapping.group(1))) == set(SEC_DETAILS), \
        "SEC_DETAIL 没有逐项覆盖后端 DETAILS"
    row = _function_body("securityEventRow", js)
    assert "secDetail(event.detail)" in row, "桌面表没有消费原因映射"
    card = _function_body("securityEventCard", js)
    assert 'line("原因", secDetail(event.detail))' in card, "手机卡片没有原因一行"


def test_security_events_table_gained_the_reason_column_and_nine_colspan():
    js, html = _js(), _html()
    assert "<th>时间</th><th>级别</th><th>类型</th><th>原因</th><th>来源</th>" in html, \
        "桌面表头没有原因列（或列序被改了）"
    row = _function_body("securityEventRow", js)
    assert row.count("tr.appendChild") == 9, "事件行不是 9 列"
    paint = _function_body("paintSecurityEvents", js)
    assert ", 9)" in paint, "空表 colspan 没有跟着改成 9"
    events = _function_body("handleEvents", js)
    assert 'paintTableError($("secEventRows"), 9' in events, "错误行 colspan 没有改成 9"


def test_security_severity_filter_is_part_of_every_request():
    """级别筛选必须进 URL（含加载更多的 before 请求），切换即重取。"""
    js = _js()
    url = _function_body("buildSecurityEventsUrl", js)
    assert url, "buildSecurityEventsUrl 不像原来的形状了"
    assert 'parts.push("severity=" + encodeURIComponent(secFilters.severity))' in url, \
        "severity 没有拼进事件 URL"
    apply = _function_body("applySecurityFilters", js)
    assert apply, "applySecurityFilters 不像原来的形状了"
    assert 'severity: $("secSeverityFilter").value' in apply, "筛选状态没有读级别下拉"
    assert '$("secSeverityFilter").addEventListener("change"' in js, "切换级别不会重取"


def test_security_source_shows_the_fingerprint_but_never_the_raw_ip():
    js, css = _js(), _css()
    assert "event.source_fp" in js, "页面没有消费 source_fp"
    assert "slice(0, 8)" in js, "指纹没有截到 8 位"
    assert '"source-fp"' in js, "指纹没有独立的 dim/mono 样式挂点"
    assert ".source-fp" in css and "ui-monospace" in _css_rule(".source-fp"), \
        "指纹不是等宽小字"
    assert "innerHTML" not in js


def test_security_visual_styles_use_existing_tokens():
    css = _css()
    for selector in (".risk-banner", ".risk-banner.risk-alert", ".risk-banner.risk-safe",
                     ".sev-bar", ".sev-seg.alert", ".sev-seg.warn", ".sev-seg.info",
                     ".sev-legend", ".sev-dot.alert", ".timeline-bars",
                     ".timeline-bar", ".timeline-alert", ".timeline-axis", ".source-fp"):
        assert _css_rule(selector), f"{selector} 规则不见了"
    assert "var(--danger)" in _css_rule(".risk-banner.risk-alert"), "高危横幅没用 danger"
    assert "var(--fv-danger-soft)" in _css_rule(".risk-banner.risk-alert"), \
        "高危横幅没读现有淡底令牌"
    assert "var(--danger)" in _css_rule(".timeline-alert"), "有高危的桶顶没用 danger 色"
    assert "overflow-x: auto" in _css_rule(".timeline-bars"), "时间线窄屏不能横向滚动"
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", css), "可视化引入了硬编码颜色"


# ---------- 6/9. 样式硬约束 ----------

def test_css_has_tap_targets_motion_fallback_and_prewrap():
    css = _css()
    assert "--fv-tap" in css or "min-height: 44px" in css, "44px 触控热区不见了"
    assert "prefers-reduced-motion" in css, "晕动症退路不见了"
    assert "white-space: pre-wrap" in css, "反馈正文不再保留换行"
    assert "focus-visible" in css, "焦点态不可见"


def test_css_does_not_redefine_the_palette():
    css = _css()
    # 只允许令牌引用与局部 rgba 阴影/蒙层；不允许出现第二套十六进制色板。
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not hexes, f"admin.css 里出现了硬编码颜色：{hexes[:5]}"
    # danger 淡底只许在 :root 定义一次，标签与高危徽章都读它。
    assert css.count("--fv-danger-soft:") == 1, "danger 淡底令牌被定义/复制了不止一次"
    for rule in (".tag.off", ".tag.warn", ".sev-tag.alert"):
        assert "var(--fv-danger-soft)" in _css_rule(rule), \
            f"{rule} 还在硬编码 danger 淡底，没读 --fv-danger-soft"


def test_desktop_sidebar_width_and_brand_flex_are_explicitly_overridden():
    """共享 style.css 的 `.sidebar { width: 268px }` 与 `.brand { flex: 1 }` 会级联进来。

    只在 admin.css 里写 grid 第一列 232px 不够：侧栏自身仍被撑到 268px，盖住正文
    36px；品牌块还会被拉到半屏高，把导航推到下半页。这里按规则块判，不认"232px
    出现在任意位置"——写在注释或别的选择器里都救不了这一页。
    """
    shell = _css_rule(".shell")
    assert re.search(r"(?:^|;)\s*grid-template-columns:\s*232px\b", shell), \
        ".shell 的第一列不再是 232px"
    sidebar = _css_rule("body.admin .sidebar")
    assert re.search(r"(?:^|;)\s*width:\s*232px\s*(?:;|$)", sidebar), \
        "body.admin .sidebar 没有显式 width: 232px（共享 268px 会继续生效）"
    brand = _css_rule("body.admin .brand")
    assert re.search(r"(?:^|;)\s*flex:\s*none\s*(?:;|$)", brand), \
        "body.admin .brand 没有 flex: none（共享 flex:1 会把导航推到下半页）"


def test_mobile_card_lists_stay_hidden_on_desktop():
    """`.card-list { display:flex }` 写在 `.mobile-only { display:none }` 后面且同特异性。

    缺这条更具体的收起规则时，桌面端表格下面会再渲染一整份手机卡片（账号、安全事件
    各一份）；窄屏媒体查询里的 `.card-list.mobile-only { display:flex }` 在文件末尾，
    仍能把它放出来。
    """
    base = _css_rule(".card-list.mobile-only")
    assert re.search(r"(?:^|;)\s*display:\s*none\s*(?:;|$)", base), \
        "桌面端没有把“手机专属”卡片列表收起来：表格下面会多一份重复内容"


def test_admin_js_parses_with_node_when_available():
    """语法错能让整页白屏，而所有正则锁照绿；有 node 的机器上真跑一次 --check。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了 node --check；文本判据那几条照跑")
    result = subprocess.run(
        [node, "--check", str(ADMIN / "admin.js")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, f"admin.js 语法检查失败：\n{result.stdout}\n{result.stderr}"


# ---------- 7. hash 路由 ----------

def test_hash_routes_cover_all_sections_and_six_runtime_pages():
    js = _js()
    for key in ("overview", "accounts", "security", "mcp", "feedback", "runtime"):
        assert '"' + key + '"' in js, f"一级路由键 {key} 不见了"
    for key in RUNTIME_KEYS:
        assert '"runtime/' + key + '"' in js, f"运行子页路由键 {key} 不见了"
    assert "hashchange" in js, "路由不是由 hashchange 驱动的"
    assert 'history.replaceState(null, "", "#/overview")' in js, "空/非法 hash 没有回落到概览"


# ---------- 8. 没有任何外部引用 ----------

def test_no_external_url_anywhere_in_the_three_files():
    """三份文件里不许有任何外站引用——注释里的文档链接也不算。

    注释先剥掉：解释里出现 `https://…` 不该让这条红，而把真外链挪进注释同样
    不能放行。协议相对 URL（`//cdn.example/...`）不带 scheme，上面两条看不见，
    所以按具体属性/调用再扫一遍。
    """
    corpus = {
        "index.html": _html(),
        "admin.css": _css_clean(),
        "admin.js": _js(),
    }
    for name, text in corpus.items():
        assert "http://" not in text, f"{name} 里出现了 http:// 外部引用"
        assert "https://" not in text, f"{name} 里出现了 https:// 外部引用"
        for pattern in ('src="//', 'href="//', 'fetch("//', 'import("//', "url(//"):
            assert pattern not in text, f"{name} 里出现了协议相对外链：{pattern}"


# ---------- 账号与反馈契约 ----------

def test_account_rows_consume_next_session_expiry_without_failing_when_absent():
    js = _js()
    assert "next_session_expiry_at" in js
    assert "72 * 3600 * 1000" in js, "即将到期的 72 小时阈值不见了"
    assert "即将到期" in js
    assert '"—"' in js


def test_feedback_pagination_search_and_export_are_wired():
    js = _js()
    assert "/v1/admin/user-feedback?limit=50" not in js  # 参数是拼出来的
    assert '"/v1/admin/user-feedback?"' in js
    assert '"limit=50"' in js
    assert "unread=1" in js
    assert "before=" in js
    assert "next_before" in js
    assert "仅搜索已加载条目" in _html()
    assert '"/v1/admin/user-feedback/export.csv"' in js
    assert "导出端点尚未上线" in js


def test_feedback_delete_is_still_hidden_for_unread_items():
    js = _js()
    assert "if (!f.read)" in js, "未读条目不再分支：删除按钮会出现在不该出现的地方"


def test_feedback_images_are_thumbnails_with_an_in_page_viewer():
    """截图不能只是一个新标签链接：先缩略图，点开在当前页放大。

    图片端点仍是管理员专属的同源 GET，`<img>` 自动带 httpOnly Cookie；缩略图与
    查看器都不许拼 HTML（加载失败时用 textContent 写回退文案）。
    """
    js, html, css = _js(), _html(), _css()
    assert "fb-thumb" in js and "openImageViewer" in js
    assert '"/v1/admin/user-feedback/" + encodeURIComponent(f.id) + "/image/"' in js
    assert 'id="imageViewerImg"' in html
    assert 'id="imageViewerClose"' in html
    assert ".fb-thumb img" in css and "object-fit: cover" in css
    assert ".image-viewer-body img" in css and "object-fit: contain" in css
    assert "innerHTML" not in js


def test_feedback_badge_refreshes_on_boot_and_manual_refresh():
    """未读角标必须在任何入口都出现：停在 #/accounts 刷新后也要更新。

    boot 里那次是无条件的兜底（非概览/反馈入口没有别的代码会拉未读数），
    refreshCurrent 里那次管顶栏「刷新」。函数本身只读 unread、失败静默，
    不许碰当前分区列表，也不许为此再拉一次用户表。
    """
    js = _js()
    boot = _function_body("boot", js)
    assert boot, "boot 不像原来的形状了，这条锁要跟着改而不是删"
    assert "refreshFeedbackBadge()" in boot, "boot 成功后没有刷新未读角标"
    refresh = _function_body("refreshCurrent", js)
    assert refresh, "refreshCurrent 不像原来的形状了，这条锁要跟着改而不是删"
    assert "refreshFeedbackBadge()" in refresh, "顶栏刷新不再更新未读角标"

    badge = _function_body("refreshFeedbackBadge", js)
    assert badge, "refreshFeedbackBadge 不见了"
    assert '"/v1/admin/user-feedback"' in badge, "角标刷新没读反馈接口"
    assert "setFeedbackBadge" in badge and "unread" in badge, "角标没用未读数更新"
    assert "flash(" not in badge and "setFeedbackBadge(0)" not in badge, \
        "角标读取失败时不再静默：会把整页提示条或角标清空"
    assert js.count('req("/v1/admin/users")') == 1, "角标刷新引入了第二处用户表读法"


# ---------- 对话框可访问性 ----------

def test_dialogs_are_modal_and_manage_focus():
    """按 id 逐个判，而不是数 `aria-modal` 的个数：第二批合法加弹层不该误红。"""
    html, js = _html(), _js()
    assert html.count('aria-modal="true"') >= 3, "三个基础弹层少了至少一个"
    for sheet_id in ("reveal", "ask", "detail", "imageViewer"):
        tag = re.search(r'<div[^>]*\bid="' + sheet_id + r'"[^>]*>', html)
        assert tag, f"弹层 #{sheet_id} 不见了"
        opening = tag.group(0)
        assert 'role="dialog"' in opening, f"#{sheet_id} 缺 role=dialog"
        assert 'aria-modal="true"' in opening, f"#{sheet_id} 缺 aria-modal"
        label = re.search(r'aria-labelledby="([^"]+)"', opening)
        assert label, f"#{sheet_id} 缺 aria-labelledby"
        assert f'id="{label.group(1)}"' in html, \
            f"#{sheet_id} 的 aria-labelledby 指向不存在的 id：{label.group(1)}"
    for needle in ("Escape", "trapFocus", "sheetReturnFocus", ".focus()"):
        assert needle in js, f"对话框缺少 {needle}"
    assert 'document.querySelector(".sheet:not(.hidden)")' in js


# ---------- 运行子页懒加载 ----------

def test_runtime_subpages_are_lazy_and_refresh_one_page_at_a_time():
    js = _js()
    assert "runtimeLoaded" in js
    assert "RUNTIME_LOADERS" in js
    assert "if (!force && runtimeLoaded[key]) return;" in js
    assert "refreshCurrent" in js
