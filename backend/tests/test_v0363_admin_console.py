"""v0.36.3 管理台第一批的静态回归锁。

这一批不改任何 Python 应用代码，只重排 front-end。行为正确性最终由浏览器与
第二批的 security 端点一起验收，但下面这些“编译器与运行时都不管”的约束必须
现在就钉住：

- 信息架构：五个一级分区（概览/账号/安全/反馈/运行）与五个导航按钮必须一一对应，
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
from pathlib import Path

from app.web.web_router import ADMIN_DIR
from tests.test_web_pwa import _strip_html_comments, _strip_js_comments

ADMIN = Path(ADMIN_DIR)

NAV = (
    ("overview", "secOverview"),
    ("accounts", "secAccounts"),
    ("security", "secSecurity"),
    ("feedback", "secFeedback"),
    ("runtime", "secRuntime"),
)
RUNTIME_KEYS = ("usage", "search", "memory", "tasks", "agent", "config")
SEC_TYPES = (
    "auth.fail", "auth.rate_limited", "auth.blocked", "admin.action",
    "tool.blocked", "approval.denied", "provider.error", "system.anomaly",
)


def _raw(name: str) -> str:
    return (ADMIN / name).read_text(encoding="utf-8")


def _html() -> str:
    return _strip_html_comments(_raw("index.html"))


def _js() -> str:
    return _strip_js_comments(_raw("admin.js"))


def _css() -> str:
    return _raw("admin.css")


# ---------- 1. 五个一级分区与导航 ----------

def test_five_primary_sections_have_matching_tabs():
    html = _html()
    sections = re.findall(r'<section class="sec[^"]*" id="([^"]+)"', html)
    assert sections == [sid for _, sid in NAV], f"五个一级分区不对：{sections}"

    for key, sid in NAV:
        assert f'id="tab-{key}"' in html, f"导航按钮 tab-{key} 不见了"
        assert f'aria-controls="{sid}"' in html, f"tab-{key} 没指向 {sid}"
        assert f'data-goto="{sid}"' in html, f"tab-{key} 没挂到 {sid}"
    assert len(re.findall(r'data-goto="', html)) == 5, "一级导航多一个少一个都会有一栏看不见"
    assert 'role="tablist"' in html
    assert 'aria-selected="true"' in html
    assert 'role="tabpanel"' in html
    assert 'aria-modal="true"' in html
    assert 'aria-live="polite"' in html

    for sid, owner in re.findall(
            r'<section class="sec[^"]*" id="([^"]+)" role="tabpanel" aria-labelledby="([^"]+)"', html):
        assert any(owner == "tab-" + key for key, _ in NAV), f"{sid} 的 aria-labelledby 指错"
    assert len(re.findall(r'<section class="sec[^"]*" id="', html)) == 5


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


# ---------- 3/4. 不拼 HTML、不碰本机存储 ----------

def test_admin_js_has_no_html_sinks():
    js = _js()
    for needle in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function"):
        assert needle not in js, f"admin.js 出现了 {needle}"


def test_admin_js_never_touches_local_storage_or_cookies():
    js = _js()
    for needle in ("localStorage", "sessionStorage", "document.cookie"):
        assert needle not in js, f"admin.js 碰到了 {needle}"


# ---------- 5. 安全页契约 ----------

def test_security_endpoints_and_enums_match_the_contract():
    js = _js()
    assert '"/v1/admin/security/summary?window=' in js, "summary 端点不见了"
    assert '"/v1/admin/security/events?' in js, "events 端点不见了"
    for event_type in SEC_TYPES:
        assert '"' + event_type + '"' in js, f"固定事件类型 {event_type} 没有映射"
    severity = re.search(r"const SEC_SEVERITY = \{([^}]*)\}", js)
    assert severity, "SEC_SEVERITY 不见了"
    assert set(re.findall(r"(\w+):", severity.group(1))) == {"info", "warn", "alert"}
    for field in ("data.by_type", "data.by_source", "data.by_account",
                  "data.audit", "data.warnings", "next_before", "audit.size_bytes"):
        assert field in js, f"契约字段 {field} 没有被消费"
    assert "limit=50" in js, "事件分页不是 50 条"
    assert "Promise.allSettled" in js, "概览四张卡没有做到一张失败不影响其他"
    assert '"auth.fail"' in js and "countType" in js


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


def test_pending_route_allowlist_is_exactly_the_security_contract():
    """test_admin_page 的放行名单只许住着两个 security 端点。

    它是一张“暂时还没落地”的名单，不是“随便写什么都放行”的口子。第二批实现后
    这两个端点会自己进路由表，但名单不该因此长出第三条。
    """
    from tests.test_admin_page import PENDING_CONTRACT_ROUTES

    assert PENDING_CONTRACT_ROUTES == {
        "/v1/admin/security/summary",
        "/v1/admin/security/events",
    }


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


# ---------- 7. hash 路由 ----------

def test_hash_routes_cover_five_sections_and_six_runtime_pages():
    js = _js()
    for key in ("overview", "accounts", "security", "feedback", "runtime"):
        assert '"' + key + '"' in js, f"一级路由键 {key} 不见了"
    for key in RUNTIME_KEYS:
        assert '"runtime/' + key + '"' in js, f"运行子页路由键 {key} 不见了"
    assert "hashchange" in js, "路由不是由 hashchange 驱动的"
    assert 'history.replaceState(null, "", "#/overview")' in js, "空/非法 hash 没有回落到概览"


# ---------- 8. 没有任何外部引用 ----------

def test_no_external_url_anywhere_in_the_three_files():
    for name in ("index.html", "admin.css", "admin.js"):
        text = _raw(name)
        assert "http://" not in text, f"{name} 里出现了 http:// 外部引用"
        assert "https://" not in text, f"{name} 里出现了 https:// 外部引用"


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


# ---------- 对话框可访问性 ----------

def test_dialogs_are_modal_and_manage_focus():
    html, js = _html(), _js()
    assert html.count('aria-modal="true"') == 3
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
