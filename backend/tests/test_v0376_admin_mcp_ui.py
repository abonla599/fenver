"""v0.37.6 管理台 MCP 分区与 #181 桌面泄漏的静态回归锁。

这一批只改前端静态文件（index.html / admin.js / admin.css），后端 MCP 管理端点
（GET /v1/admin/mcp/stdio、PUT settings、PUT/DELETE servers/{id}）已经在 v0.37.5
落地。所以这里钉的是"编译器与运行时都不管"的四类约束：

- 信息架构：第六个导航 tab 与 ``secMcp`` 一一对应，hash 路由 ``#/mcp`` 真能落到
  那一节，"进入分区就 GET 一次、手动刷新"在加载分发与顶栏刷新里都有落点；
- 数据口径：GET 的状态投影不回 args / env 值，页面也不许从响应里读它们；四个
  写/读路径必须经 ``req()``（带 CSRF），不许新起裸 ``fetch``；
- 语义诚实：整条替换的警告、"默认关闭 / 仅管理员 / 不是沙箱 / 每次调用仍需审批"
  这些边界话术必须留在页面上；
- #181：``.mobile-only`` / ``.card-list.mobile-only`` 在桌面隐藏、在手机媒体查询里
  放出，手机底部导航从 5 列改成 6 列。**真机视觉复测仍需用户设备**，代码锁只能
  证明样式表没写反，证明不了那一台手机上的渲染结果。

判语料之前一律先剥注释：解释性注释里完全可能出现被禁字样。
"""
import re
from pathlib import Path

from app.core.mcp_stdio_config import ID_RE
from app.web.web_router import ADMIN_DIR
from tests.test_web_pwa import _strip_html_comments, _strip_js_comments

ADMIN = Path(ADMIN_DIR)
MCP_FUNCTIONS = (
    "loadMcp", "renderMcpServers", "mcpServerRow", "mcpServerCard", "mcpActions",
    "pickMcp", "resetMcpForm", "saveMcp", "deleteMcp", "toggleMcpEnabled",
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
    return re.sub(r"/\*[\s\S]*?\*/", "", _css())


def _css_rule(selector: str) -> str:
    """取某条选择器（逗号分隔的组里精确匹配一项）的第一段声明块。"""
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", _css_clean()):
        selectors = [s.strip() for s in match.group(1).split(",")]
        if selector in selectors:
            return match.group(2)
    return ""


def _mobile_css() -> str:
    """手机媒体查询那一段（899px 断点）；最后的 `}` 顶格，是这一段的终点。"""
    css = _css_clean()
    start = css.index("@media (max-width: 899px) {")
    end = css.index("\n}", start)
    return css[start:end]


def _function_body(name: str) -> str:
    """取一个顶层函数体；找不到就返回空串，由调用方断言。"""
    match = re.search(r"function " + name + r"\([^)]*\)\s*\{([\s\S]*?)\n  \}", _js())
    return match.group(1) if match else ""


# ---------- 1. 导航 / 分区 / 路由 ----------

def test_sixth_nav_tab_and_mcp_section_are_wired():
    html = _html()
    tabs = re.findall(r'<button[^>]*\bclass="nav-btn[^"]*"[^>]*>', html)
    assert len(tabs) == 6, f"一级导航按钮不是 6 个：{len(tabs)}"
    tag = next((t for t in tabs if 'id="tab-mcp"' in t), "")
    assert tag, "第六个导航 tab 不见了"
    for needle in ('aria-controls="secMcp"', 'data-goto="secMcp"',
                   'data-route="mcp"', 'data-icon="mcp"'):
        assert needle in tag, f"MCP tab 缺少 {needle}：{tag[:160]}"

    section = re.search(
        r'<section class="sec[^"]*" id="secMcp" role="tabpanel" aria-labelledby="tab-mcp"', html)
    assert section, "secMcp 没有作为 tabpanel 挂到 tab-mcp 上"
    secs = re.findall(r'<section class="sec[^"]*" id="([^"]+)"', html)
    assert len(secs) == 6, f"一级分区不是 6 个：{secs}"

    js = _js()
    assert '"mcp": "secMcp"' in js, "ROUTE_SECTIONS 没有把 mcp 映射到 secMcp"
    assert 'mcp: "MCP 外部工具"' in js, "PAGE_TITLES 没有 MCP 的标题"
    assert '|| raw === "mcp"' in js, "routeFromHash 不认 #/mcp"

    enter = _function_body("enterSection")
    assert enter, "enterSection 不像原来的形状了，这条锁要跟着改而不是删"
    assert 'sectionId === "secMcp"' in enter and "loadMcp()" in enter, \
        "进入 MCP 分区不会 GET 一次状态"
    refresh = _function_body("refreshCurrent")
    assert refresh, "refreshCurrent 不像原来的形状了"
    assert 'route === "mcp"' in refresh and "loadMcp()" in refresh, \
        "顶栏刷新不会刷新 MCP 状态"


def test_mcp_icon_is_inline_and_defined():
    js, html = _js(), _html()
    block = re.search(r"const ICONS = \{([\s\S]*?)\n  \};", js)
    assert block, "ICONS 这张表不像原来的形状了"
    assert re.search(r"^\s{4}mcp:\s*\[", block.group(1), re.M), "ICONS 里没有 mcp 图标"
    assert 'data-icon="mcp"' in html
    # 内联线性图标：有 path 数据、没有 emoji，也不引外部图标库。
    icon_block = re.search(r"mcp:\s*\[([^\]]+)\]", block.group(1))
    assert icon_block and icon_block.group(1).count('"M') >= 3
    assert not re.search(r"[\U0001F300-\U0001FAFF]", _raw("admin.js"))


# ---------- 2. 四个端点都经 req()（带 CSRF），不新起裸 fetch ----------

def test_mcp_endpoints_are_all_wired_through_req():
    js = _js()
    load = _function_body("loadMcp")
    assert load, "loadMcp 不见了"
    assert 'req("/v1/admin/mcp/stdio")' in load, "状态读取没走 GET /v1/admin/mcp/stdio"

    toggle = _function_body("toggleMcpEnabled")
    assert toggle, "toggleMcpEnabled 不见了"
    assert 'req("/v1/admin/mcp/stdio/settings", { method: "PUT", body: { enabled: next } })' in toggle, \
        "全局开关没有 PUT 到 settings，或 body 不是 {{enabled}}"

    save = _function_body("saveMcp")
    assert save, "saveMcp 不见了"
    assert '"/v1/admin/mcp/stdio/servers/" + encodeURIComponent(id)' in save, \
        "保存没有 PUT 到 servers/{id}（id 要过 encodeURIComponent）"
    assert "{ method: \"PUT\", body: body }" in save, "保存不是 PUT"

    delete = _function_body("deleteMcp")
    assert delete, "deleteMcp 不见了"
    assert '"/v1/admin/mcp/stdio/servers/" + encodeURIComponent(s.id)' in delete, \
        "删除没有 DELETE 到 servers/{id}"
    assert '{ method: "DELETE" }' in delete, "删除不是 DELETE"


def test_mcp_actions_never_use_a_bare_fetch_or_poll():
    for name in MCP_FUNCTIONS:
        body = _function_body(name)
        assert body, f"{name} 不见了，这条锁要跟着改而不是删"
        assert "fetch(" not in body, f"{name} 绕开 req() 直接 fetch"
        assert "setInterval" not in body, f"{name} 在分区里起轮询：这一节要求手动刷新"
        assert "XMLHttpRequest" not in body
    js = _js()
    assert "mcpTimer" not in js, "MCP 分区出现了自动刷新计时器"
    assert "syncSecurityTimer" in js, "安全分区的 20 秒定时器不在了（说明这段被误删）"
    req_body = _function_body("req")
    assert req_body, "req 不像原来的形状了"
    assert 'headers["X-CSRF"] = "1"' in req_body, \
        "req 不再给不安全方法加 CSRF 头：MCP 的 PUT/DELETE 会被后端拒绝"


# ---------- 3. 状态渲染只碰服务端投影里的键 ----------

def test_mcp_rendering_never_reads_args_or_env_values():
    bodies = "\n".join(_function_body(name) for name in
                       ("renderMcpServers", "mcpServerRow", "mcpServerCard", "mcpActions",
                        "mcpStateTag", "mcpToolsText", "mcpEnvText"))
    assert bodies, "MCP 渲染函数不见了"
    for needle in ("env_names", "command_basename", "tools_total", "tools_exposed",
                   "restarts", "last_error", "pid", "state"):
        assert needle in bodies, f"状态渲染没有消费 {needle}"
    for banned in ("args", "env_allowlist", "env_values"):
        assert banned not in bodies, f"状态渲染读了 {banned}：GET 不返回它，页面也不该找它"
    js = _js()
    assert ".args" not in js, "页面在用 .args 读响应字段（GET 永远不会有这个键）"
    assert ".env_allowlist" not in js, "页面在用 .env_allowlist 读响应字段"
    assert "env_names" in js, "页面没有消费 env_names（只允许渲染变量名）"


def test_mcp_empty_state_and_retry_are_present():
    render = _function_body("renderMcpServers")
    assert "默认关闭，尚未配置" in render, "空表没有说「默认关闭，尚未配置」"
    load = _function_body("loadMcp")
    assert 'paintTableError($("mcpRows"), 9' in load, "桌面表读取失败没有可重试的错误行"
    assert "paintRetry($(\"mcpCards\")" in load, "手机卡片读取失败没有可重试的空态"
    assert 'btn("refresh", "重试"' in load, "错误态没有重试按钮"
    assert 'id="mcpState"' in _html(), "错误/提示容器 #mcpState 没有落点"
    assert "#mcpState" in _css(), "#mcpState 的样式挂点不见了"


# ---------- 4. 整条替换语义与表单校验 ----------

def test_whole_entry_replace_warning_is_explicit_and_args_stay_empty():
    html, js = _html(), _js()
    assert "整条替换，原 args 不会回显，保存前请重新填写" in html, \
        "表单旁没有整条替换的警告：编辑时 args 为空，保存会静默清空原参数"
    assert "整条替换" in html
    pick = _function_body("pickMcp")
    assert pick, "pickMcp 不见了"
    assert 'mcpEditingId = s.id' in pick
    assert 'readOnly = true' in pick, "编辑已有条目时 id 没有锁成只读"
    assert '$("mcpArgs").value = "";' in pick, \
        "编辑时 args 框没有显式清空：残留的旧值会让管理员以为服务端回显了它"
    save = _function_body("saveMcp")
    assert "没重新填的 args 会被清空" in save, "保存前没有再次说明整条覆盖会清空 args"
    assert "整条替换" in save, "编辑保存没有二次确认整条替换"
    assert save.index("ask(") < save.index("req("), "整条替换没有先确认再写"


def test_mcp_form_body_has_only_the_four_writable_fields():
    save = _function_body("saveMcp")
    block = re.search(r"const body = \{([\s\S]*?)\n    \};", save)
    assert block, "saveMcp 的 body 形状变了，这条锁要跟着改而不是删"
    keys = re.findall(r"(\w+):", block.group(1))
    assert keys == ["command", "args", "env_allowlist", "enabled"], \
        f"PUT body 只该有 command/args/env_allowlist/enabled，实际：{keys}"
    assert "id" not in keys, "PUT body 发了 id；后端要求 id 只在路径里"

    assert re.search(r"const MCP_ID_RE = /(.+?)/;", _js()).group(1) == ID_RE.pattern, \
        "前端 id 正则与 mcp_stdio_config.ID_RE 不一致（后端仍权威，但提示要先对）"
    args_fn = _function_body("mcpArgsFromForm")
    assert args_fn, "mcpArgsFromForm 不见了"
    assert '.split("\\n")' in args_fn and ".trim()" in args_fn and ".filter(" in args_fn, \
        "args 没有按行转换、去空白、去空项"
    env_fn = _function_body("mcpEnvFromForm")
    assert env_fn, "mcpEnvFromForm 不见了"
    assert '.split(",")' in env_fn and ".trim()" in env_fn and ".filter(" in env_fn, \
        "env_allowlist 没有按逗号拆分、去空白、去空项"


def test_mcp_global_toggle_rolls_back_on_failure():
    toggle = _function_body("toggleMcpEnabled")
    assert "catch (e)" in toggle
    assert "box.checked = mcpEnabled;" in toggle, "开关失败没有回滚为上一次读到的值"
    assert "已回滚为原值" in toggle, "回滚没有告诉管理员"


# ---------- 5. 文案里的安全边界 ----------

def test_mcp_copy_states_the_boundaries_without_promising_isolation():
    html = _html()
    for phrase in ("默认关闭", "仅管理员", "不是沙箱", "每次调用仍需审批"):
        assert phrase in html, f"MCP 分区说明缺少「{phrase}」"
    assert "安全隔离" not in html, "页面在承诺安全隔离；stdio 子进程不是沙箱"


# ---------- 6. #181 回归锁：mobile-only 桌面隐藏、手机显示；六列导航 ----------

def test_mobile_only_cards_are_hidden_on_desktop_and_shown_on_mobile():
    """#181 的泄漏面是"手机专属卡片在桌面又渲染一份"。

    真机视觉复测仍需用户设备；这里只钉住样式表里的三条规则与它们的出场顺序：
    基础态 ``.mobile-only`` / ``.card-list.mobile-only`` 收成 none，899px 断点里
    再分别放成 block / flex。``.card-list { display:flex }`` 写在 ``.mobile-only``
    之后，所以那条更具体的 ``.card-list.mobile-only`` 不能少。
    """
    assert re.search(r"(?:^|;)\s*display:\s*none\s*(?:;|$)", _css_rule(".mobile-only")), \
        "桌面端 .mobile-only 又显示出来了"
    assert re.search(r"(?:^|;)\s*display:\s*none\s*(?:;|$)", _css_rule(".card-list.mobile-only")), \
        "桌面端的“手机专属”卡片列表没收起：表格下面会多一份重复内容"
    mobile = _mobile_css()
    assert re.search(r"\.mobile-only\s*\{\s*display:\s*block\s*;", mobile), \
        "手机断点没有把 .mobile-only 放成 block"
    assert re.search(r"\.card-list\.mobile-only\s*\{\s*display:\s*flex\s*;", mobile), \
        "手机断点没有把 .card-list.mobile-only 放成 flex"
    assert re.search(r"\.desktop-only\s*\{\s*display:\s*none\s*;", mobile), \
        "手机断点没有收起 .desktop-only 列表"


def test_all_card_lists_pair_a_desktop_table_with_mobile_only_cards():
    html = _html()
    pairs = (("userRows", "userCards"), ("secEventRows", "secEventCards"), ("mcpRows", "mcpCards"))
    for row_id, card_id in pairs:
        card_tag = re.search(r'<div class="([^"]*)" id="%s"' % card_id, html)
        assert card_tag, f"#{card_id} 不见了"
        classes = set(card_tag.group(1).split())
        assert {"card-list", "mobile-only"} <= classes, f"#{card_id} 不是手机专属卡片列表"
        pos = html.index('id="%s"' % card_id)
        assert 'class="table-wrap desktop-only"' in html[max(0, pos - 600):pos], \
            f"#{row_id} 没有和 #{card_id} 成对出现（一个桌面表 + 一份手机卡片）"


def test_mobile_nav_has_six_columns():
    mobile = _mobile_css()
    assert re.search(r"grid-template-columns:\s*repeat\(6,\s*minmax\(0,\s*1fr\)\)", mobile), \
        "手机底部导航不是 6 列：第六个 tab 点不到"
    assert "repeat(5," not in _css_clean(), "手机导航还留着旧的 5 列"
