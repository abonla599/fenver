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
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

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


def _css_decls(selector: str):
    """所有命中该选择器的 (起始位置, 声明块)，按文件顺序。

    #181 的判据不能只取第一段：``.mobile-only`` 在基础态和手机媒体查询里各出现
    一次，前者收成 none、后者放成 block/flex；只看第一段无法证明"手机那条写在
    桌面隐藏之后"，也看不出有没有第三条把它又打开了。
    """
    found = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", _css_clean()):
        selectors = [s.strip() for s in match.group(1).split(",")]
        if selector in selectors:
            found.append((match.start(), match.group(2)))
    return found


def _display_at(decl: str):
    hit = re.search(r"(?:^|;)\s*display:\s*([A-Za-z-]+)\s*(?:;|$)", decl)
    return hit.group(1) if hit else ""


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


def test_write_paths_bump_generation_before_their_request():
    """save/delete/toggle 一开始写就必须作废在途 GET。

    toggle 的代际递增在公开流程里没有行为可达路径（loadMcp 发 GET 时同步把
    mcpLoaded 置 false，toggle 会早退），所以这一处只能由静态锁钉住；save/delete
    的在途 GET 竞态由下面的 Node 行为探针实跑。
    """
    for name in ("saveMcp", "deleteMcp", "toggleMcpEnabled"):
        body = _function_body(name)
        assert body, f"{name} 不见了，这条锁要跟着改而不是删"
        assert "mcpGeneration += 1" in body, \
            f"{name} 没有递增 mcpGeneration：写请求不会作废在途的旧 GET"
        assert body.index("mcpGeneration += 1") < body.index("req("), \
            f"{name} 先发请求后递增代际，旧 GET 仍可能在窗口内落地"


def test_global_toggle_listener_is_bound_to_change_and_busy():
    js = _js()
    hit = re.search(r'\$\("mcpEnabled"\)\.addEventListener\("change",\s*([^\n]+)', js)
    assert hit, "全局开关没有绑定 change 监听"
    line = hit.group(1)
    assert "busy(" in line and "toggleMcpEnabled" in line, \
        "change 监听没有经过 busy/toggleMcpEnabled：" + line


def test_mcp_global_toggle_rolls_back_on_failure():
    toggle = _function_body("toggleMcpEnabled")
    assert "catch (e)" in toggle
    assert "box.checked = mcpEnabled;" in toggle, "开关失败没有回滚为上一次读到的值"
    assert "已回滚为原值" in toggle, "回滚没有告诉管理员"


# ---------- 4b. BLOCK-1：GET 没成功之前，全局开关不许可写 ----------

def test_load_mcp_locks_before_fetch_and_generation_guards_stale_responses():
    load = _function_body("loadMcp")
    assert load, "loadMcp 不见了"
    fetch_at = load.index('req("/v1/admin/mcp/stdio")')
    assert load.index("mcpLoaded = false") < fetch_at, \
        "loadMcp 不是一开始就置 mcpLoaded=false：首屏 GET 未回来时开关仍可点"
    assert "syncMcpToggleLock()" in load[:fetch_at], \
        "loadMcp 在发 GET 之前没有禁用全局开关"
    assert "mcpGeneration" in load and "generation !== mcpGeneration" in load, \
        "loadMcp 没有代际校验：旧 GET 的响应可以覆盖新状态"
    assert "mcpLoaded = true" in load and "syncMcpToggleLock()" in load, \
        "GET 成功后没有恢复 mcpLoaded/解锁开关"
    assert "mcpLoaded = false" in load[load.index("catch (e)"):], \
        "GET 失败分支没有保持 mcpLoaded=false"


def test_toggle_and_busy_respect_the_loaded_lock():
    toggle = _function_body("toggleMcpEnabled")
    assert toggle.lstrip().startswith("if (!mcpLoaded) return;"), \
        "toggleMcpEnabled 开头没有 !mcpLoaded 早退：读失败后仍会发 PUT settings"
    assert toggle.index("if (!mcpLoaded) return;") < toggle.index("req("), \
        "!mcpLoaded 检查排在 PUT 之后"

    busy_body = _function_body("busy")
    assert "syncMcpToggleLock()" in busy_body, \
        "busy 的 finally 仍无条件恢复控件，没按 mcpLoaded 重置 MCP 开关"
    lock = _function_body("syncMcpToggleLock")
    assert lock, "syncMcpToggleLock 不见了"
    assert 'const box = $("mcpEnabled");' in lock and "if (!box) return;" in lock, \
        "syncMcpToggleLock 对 #mcpEnabled 缺失没有空值保护（旧 HTML 缓存 + 新 JS 会 TypeError）"
    assert 'box.disabled = !mcpLoaded' in lock, \
        "syncMcpToggleLock 没有把开关状态绑定到 mcpLoaded"
    invalidate = _function_body("invalidateData")
    assert 'mcpLoaded = false' in invalidate and 'mcpGeneration += 1' in invalidate, \
        "invalidateData 没有作废在途 GET，且把加载态留成可写"
    assert '$("mcpEnabled").disabled = true' in invalidate, \
        "invalidateData 仍把全局开关设成 enabled：首屏 GET 没完成就能点"


_MCP_PROBE_JS = r'''
"use strict";
const fs = require("fs");
const vm = require("vm");

class El {
  constructor(id) {
    this.id = id || "";
    this.children = [];
    this._text = "";
    this._listeners = {};
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((n) => classes.add(n)),
      remove: (...names) => names.forEach((n) => classes.delete(n)),
      toggle: (name, on) => {
        const want = on === undefined ? !classes.has(name) : !!on;
        if (want) classes.add(name); else classes.delete(name);
      },
      contains: (name) => classes.has(name),
    };
    this.style = {};
    this.dataset = {};
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.readOnly = false;
    this.colSpan = 1;
  }
  appendChild(child) { this.children.push(child); return child; }
  append(child) { this.children.push(child); }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener() {}
  click() { (this._listeners.click || []).forEach((fn) => fn({ currentTarget: this, target: this })); }
  dispatchEvent(event) {
    const type = event && event.type;
    (this._listeners[type] || []).forEach((fn) => fn({ type, target: this, currentTarget: this }));
    return true;
  }
  setAttribute(name, value) { this["attr_" + name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this, "attr_" + name) ? this["attr_" + name] : null; }
  removeAttribute(name) { delete this["attr_" + name]; }
  querySelector() { return new El(); }
  querySelectorAll() { return []; }
  closest() { return null; }
  focus() {}
  contains() { return false; }
  get textContent() {
    return this.children.length
      ? this.children.map((child) => child.textContent || "").join("")
      : this._text;
  }
  set textContent(value) { this.children = []; this._text = String(value); }
  get firstElementChild() { return this.children[0] || null; }
}

const byId = new Map();
const el = (id) => {
  if (!byId.has(id)) byId.set(id, new El(id));
  return byId.get(id);
};

const document = {
  title: "",
  visibilityState: "visible",
  activeElement: null,
  body: new El("body"),
  getElementById: el,
  createElement: (tag) => new El(tag),
  createElementNS: (_ns, tag) => new El(tag),
  createTextNode: (text) => { const node = new El("#text"); node.textContent = text; return node; },
  querySelector: () => new El(),
  querySelectorAll: () => [],
  addEventListener() {},
  contains: () => false,
};
const location = { hash: "#/overview" };
const history = { replaceState() {} };
const window = { addEventListener() {}, location };

let calls = [];
let handler = () => new Promise(() => {});
const fetch = (path, opts) => {
  calls.push({ path: String(path), opts: opts || {} });
  return handler(String(path), opts || {});
};

const source = fs.readFileSync(process.argv[2], "utf8");
const probeCode = "\n  globalThis.__mcpProbe = {\n"
  + "    loadMcp, toggleMcpEnabled, saveMcp, deleteMcp, busy, pickMcp, syncMcpToggleLock,\n"
  + "    get loaded() { return mcpLoaded; },\n"
  + "    get enabled() { return mcpEnabled; },\n"
  + "    get servers() { return mcpServers; },\n"
  + "    get generation() { return mcpGeneration; },\n"
  + "    box: $(\"mcpEnabled\"),\n"
  + "  };\n";
if (!/\}\)\(\);\s*$/.test(source)) {
  throw new Error("admin.js 的 IIFE 收尾不是预期的 });");
}
const instrumented = source.replace(/\}\)\(\);\s*$/, probeCode + "})();");

const sandbox = {
  document, location, history, window, fetch,
  localStorage: { removeItem() {} },
  console, setTimeout, clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(instrumented, sandbox, { filename: "admin.js" });

const probe = sandbox.__mcpProbe;
const expect = (cond, message) => { if (!cond) throw new Error(message); };
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const waitFor = async (pred, message) => {
  for (let i = 0; i < 400; i += 1) {
    if (pred()) return;
    await tick();
  }
  throw new Error(message || "等待条件超时");
};

(async () => {
  // 1) GET 失败：开关必须保持禁用，mcpLoaded 必须为 false。
  handler = () => Promise.resolve({ status: 500, ok: false, json: async () => ({ detail: "boom" }) });
  await probe.loadMcp();
  expect(probe.loaded === false, "GET 失败后 mcpLoaded 仍为 true");
  expect(probe.box.disabled === true, "GET 失败后开关没有保持禁用");

  // 2) 用户点这颗开关本身：busy(box, toggle) 的 finally 不许把它无条件解锁。
  await probe.busy(probe.box, probe.toggleMcpEnabled);
  expect(probe.box.disabled === true, "busy 之后开关被无条件解锁（旧 bug 复现）");
  expect(probe.loaded === false, "busy 之后 mcpLoaded 被改写");

  // 3) 没加载成功时直接调用 toggle 也不许发 PUT。
  calls = [];
  handler = () => Promise.resolve({ status: 200, ok: true, json: async () => ({ enabled: true }) });
  probe.box.checked = true;
  await probe.toggleMcpEnabled();
  expect(!calls.some((call) => call.path.indexOf("/mcp/stdio/settings") >= 0),
    "mcpLoaded=false 时仍发出了 PUT settings");

  // 4) 成功的 GET 才解锁。
  handler = () => Promise.resolve({ status: 200, ok: true, json: async () => ({ enabled: false, servers: [] }) });
  await probe.loadMcp();
  expect(probe.loaded === true, "GET 成功后 mcpLoaded 不是 true");
  expect(probe.box.disabled === false, "GET 成功后开关仍被禁用");

  // 5) 并发刷新：新代先到、旧代后到，旧响应不许覆盖新状态。
  const pending = [];
  handler = () => new Promise((resolve) => pending.push(resolve));
  const first = probe.loadMcp();
  const second = probe.loadMcp();
  expect(pending.length === 2, "两次 loadMcp 没有各自发一个 GET，pending=" + pending.length);
  pending[1]({ status: 200, ok: true, json: async () => ({ enabled: true, servers: [{ id: "new" }] }) });
  await second;
  pending[0]({ status: 200, ok: true, json: async () => ({ enabled: false, servers: [{ id: "old" }] }) });
  await first;
  expect(probe.servers.map((server) => server.id).join(",") === "new",
    "旧代 GET 覆盖了新状态：" + JSON.stringify(probe.servers));

  // 6) DELETE 404 也要重读，不许把过期行留在表里。
  calls = [];
  handler = (path, opts) => {
    if ((opts.method || "GET") === "DELETE") {
      return Promise.resolve({ status: 404, ok: false, json: async () => ({ detail: "server_not_found" }) });
    }
    return Promise.resolve({ status: 200, ok: true, json: async () => ({ enabled: true, servers: [] }) });
  };
  const deleting = probe.deleteMcp({ id: "gone" });
  byId.get("askOk").click();
  await deleting;
  const methods = calls.map((call) => (call.opts.method || "GET") + " " + call.path);
  expect(methods[0] === "DELETE /v1/admin/mcp/stdio/servers/gone",
    "deleteMcp 没有先确认再 DELETE：" + JSON.stringify(methods));
  expect(methods.indexOf("GET /v1/admin/mcp/stdio") > 0,
    "DELETE 404 后没有重新 GET：" + JSON.stringify(methods));
  expect(probe.servers.length === 0, "DELETE 404 后还留着旧行");

  // 7) 真实 change 事件必须驱动全局开关，且监听链经过 busy：
  //    PUT 在途时开关应被禁用；绕开 busy 直调 toggle 时它一直可点。
  calls = [];
  const put7 = [];
  const get7 = [];
  handler = (path, opts) => {
    const method = (opts && opts.method) || "GET";
    if (method === "PUT") return new Promise((resolve) => put7.push(resolve));
    return new Promise((resolve) => get7.push(resolve));
  };
  probe.box.checked = false;
  const changeEvent = typeof Event === "function" ? new Event("change") : { type: "change" };
  probe.box.dispatchEvent(changeEvent);
  expect(probe.box.disabled === true,
    "change 监听没有经过 busy：PUT 在途时全局开关仍可点");
  expect(put7.length === 1, "真实 change 事件没有触发 PUT settings");
  const put7Call = calls.find((call) => call.path.indexOf("/mcp/stdio/settings") >= 0);
  expect(put7Call && JSON.parse(put7Call.opts.body).enabled === false,
    "change 监听没有把勾选后的新值发给 settings");
  put7[0]({ status: 200, ok: true, json: async () => ({ enabled: false }) });
  await waitFor(() => get7.length === 1, "开关 PUT 成功后没有再 GET 一次状态");
  get7[0]({ status: 200, ok: true, json: async () => ({ enabled: false, servers: [] }) });
  await waitFor(() => probe.loaded === true && probe.box.disabled === false,
    "开关切换后没有回到已加载/可点状态");
  expect(probe.enabled === false, "开关切换后 mcpEnabled 不是新值");

  // 8) 确认弹层打开期间来了新一轮 GET：确认保存必须作废那份写前快照。
  handler = () => Promise.resolve({
    status: 200, ok: true, json: async () => ({ enabled: true, servers: [{ id: "srv" }] }) });
  await probe.loadMcp();
  expect(probe.loaded === true && probe.servers.length === 1, "保存前装载基线失败");
  probe.pickMcp({ id: "srv", command: "/usr/bin/srv", env_names: [] });
  calls = [];
  const get8 = [];
  const put8 = [];
  handler = (path, opts) => {
    const method = (opts && opts.method) || "GET";
    if (method === "PUT") return new Promise((resolve) => put8.push(resolve));
    return new Promise((resolve) => get8.push(resolve));
  };
  const saving = probe.saveMcp();
  await tick();
  expect(put8.length === 0, "saveMcp 没有先等确认就发 PUT");
  const staleLoad8 = probe.loadMcp();
  expect(get8.length === 1, "确认弹层期间的 loadMcp 没有发 GET");
  byId.get("askOk").click();
  await tick();
  expect(put8.length === 1, "确认后没有发 PUT");
  get8[0]({ status: 200, ok: true, json: async () => ({ enabled: false, servers: [{ id: "stale" }] }) });
  await staleLoad8;
  expect(probe.loaded === false,
    "保存期间的旧 GET 把 mcpLoaded 解锁了：写路径没有作废在途 GET");
  expect(probe.enabled === true && probe.servers.map((server) => server.id).join(",") === "srv",
    "旧 GET 的写前快照覆盖了当前状态：" + JSON.stringify(probe.servers));
  expect(byId.get("mcpRows").textContent.indexOf("stale") < 0,
    "旧 GET 把旧行又渲染回表里了");
  put8[0]({ status: 200, ok: true, json: async () => ({}) });
  await waitFor(() => get8.length === 2, "保存成功后没有重新 GET 状态");
  get8[1]({ status: 200, ok: true, json: async () => ({ enabled: true, servers: [{ id: "srv" }] }) });
  await saving;
  expect(probe.loaded === true && probe.servers.map((server) => server.id).join(",") === "srv",
    "保存结束后状态没有回到最新一次 GET");

  // 9) DELETE 在途时回来的旧 GET 也不能把刚删掉的那行画回去。
  handler = () => Promise.resolve({
    status: 200, ok: true, json: async () => ({ enabled: true, servers: [{ id: "keep" }] }) });
  await probe.loadMcp();
  calls = [];
  const get9 = [];
  const del9 = [];
  handler = (path, opts) => {
    const method = (opts && opts.method) || "GET";
    if (method === "DELETE") return new Promise((resolve) => del9.push(resolve));
    return new Promise((resolve) => get9.push(resolve));
  };
  const staleLoad9 = probe.loadMcp();
  const deleting9 = probe.deleteMcp({ id: "keep" });
  byId.get("askOk").click();
  await tick();
  expect(del9.length === 1, "deleteMcp 没有先确认再 DELETE");
  get9[0]({ status: 200, ok: true, json: async () => ({ enabled: false, servers: [{ id: "stale" }] }) });
  await staleLoad9;
  expect(probe.loaded === false,
    "删除期间的旧 GET 把 mcpLoaded 解锁了：deleteMcp 没有作废在途 GET");
  expect(probe.enabled === true && probe.servers.map((server) => server.id).join(",") === "keep",
    "DELETE 期间旧 GET 的写前快照覆盖了当前状态：" + JSON.stringify(probe.servers));
  expect(byId.get("mcpRows").textContent.indexOf("stale") < 0,
    "DELETE 期间旧 GET 把已删的行又渲染回表里了");
  del9[0]({ status: 200, ok: true, json: async () => ({}) });
  await waitFor(() => get9.length === 2, "删除成功后没有重新 GET 状态");
  get9[1]({ status: 200, ok: true, json: async () => ({ enabled: true, servers: [] }) });
  await deleting9;
  expect(probe.servers.length === 0, "删除结束后还留着旧行");

  // 10) 旧 HTML 缓存 + 新 JS：#mcpEnabled 缺失时，收尾同步不许 TypeError。
  const realGetElementById = document.getElementById;
  document.getElementById = (id) => (id === "mcpEnabled" ? null : realGetElementById(id));
  let guardError = null;
  try { probe.syncMcpToggleLock(); } catch (err) { guardError = err; }
  document.getElementById = realGetElementById;
  expect(!guardError, "#mcpEnabled 缺失时 syncMcpToggleLock 抛了 TypeError：" + guardError);

  console.log(JSON.stringify({ ok: true, calls: methods }));
})().catch((err) => {
  console.error("PROBE FAIL: " + (err && err.stack ? err.stack : err));
  process.exit(1);
});
'''


def test_mcp_toggle_stays_locked_after_failed_load_behaviour(tmp_path):
    """真跑一遍 admin.js：读失败锁开关、真实 change 走 busy、写路径作废在途 GET。

    7) 用 ``dispatchEvent(new Event("change"))`` 触发全局开关，PUT 在途时开关必须
    处于 disabled——监听绕开 busy 时这条会红；8/9) 在 save 的确认弹层与 DELETE
    在途时放行一份旧 GET，旧响应不许解锁、覆盖状态或把旧行画回来——删掉写路径的
    ``mcpGeneration += 1`` 时这两条会红；10) 旧 HTML 缓存缺 ``#mcpEnabled`` 时
    ``syncMcpToggleLock`` 不许抛 TypeError。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了行为探针；静态判据照跑")
    harness = tmp_path / "mcp_probe.cjs"
    harness.write_text(_MCP_PROBE_JS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(ADMIN / "admin.js")],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, \
        "MCP 行为探针失败：\nstdout=%s\nstderr=%s" % (proc.stdout, proc.stderr)
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["ok"] is True


# ---------- 4c. 射程：确认顺序、404 重读、禁 HTML 注入 ----------

def test_delete_asks_first_and_mutations_reload():
    delete = _function_body("deleteMcp")
    assert delete, "deleteMcp 不见了"
    assert delete.index("ask(") < delete.index("req("), "删除没有先确认再 DELETE"
    assert "e.status === 404" in delete, "DELETE 404 没有单独处理"
    assert delete.count("await loadMcp()") >= 2, \
        "DELETE 成功和 404 两条路都要重新读一次，别留旧行"

    for name in ("saveMcp", "toggleMcpEnabled"):
        body = _function_body(name)
        assert "await loadMcp()" in body, f"{name} 写成功后没有重新读状态"


def test_v0376_code_never_writes_html_strings():
    js, html = _js(), _html()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
        assert sink not in js, f"admin.js 出现 {sink}：本版只允许 textContent 建树"
        assert sink not in html, f"index.html 出现 {sink}"


def test_mcp_copy_describes_the_real_projection_and_command_prefill():
    html = _html()
    assert "列表只显示 id、状态、pid、工具数、重启次数、最后错误、命令文件名与 env 变量名" in html, \
        "分区说明没有如实列出 GET 投影里会显示的那些字段"
    assert "列表只显示状态与 env 变量名" not in html, \
        "旧文案还在：列表明明也显示 id/pid/工具/重启/最后错误/命令"
    assert "command 仅用于编辑预填，GET 不回显 args/env 值" in html, \
        "表单旁没有说明 command 只用于预填、args/env 值不回显"



# ---------- 5. 文案里的安全边界 ----------

def test_mcp_copy_states_the_boundaries_without_promising_isolation():
    html = _html()
    for phrase in ("默认关闭", "仅管理员", "不是沙箱", "每次调用仍需审批"):
        assert phrase in html, f"MCP 分区说明缺少「{phrase}」"
    assert "安全隔离" not in html, "页面在承诺安全隔离；stdio 子进程不是沙箱"


# ---------- 6. #181 回归锁：mobile-only 桌面隐藏、手机显示；六列导航 ----------

def test_mobile_only_cards_are_hidden_on_desktop_and_shown_on_mobile():
    """#181 的泄漏面是"手机专属卡片在桌面又渲染一份"。

    真机视觉复测仍需用户设备；这里钉住的是声明顺序，而不是"第一条命中"：
    ``.mobile-only`` / ``.card-list.mobile-only`` 的基础态收成 none，899px 断点里
    再分别放成 block / flex，且手机断点那条必须排在桌面隐藏之后。``.card-list
    { display:flex }`` 写在 ``.mobile-only`` 之后，所以更具体的
    ``.card-list.mobile-only`` 不能少。
    """
    css = _css_clean()
    mobile = _mobile_css()
    mobile_start = css.index(mobile)
    assert mobile_start > 0

    for selector, mobile_display in (
        (".mobile-only", "block"),
        (".card-list.mobile-only", "flex"),
    ):
        decls = _css_decls(selector)
        assert decls, f"{selector} 一条规则都没有"
        desktop = [(pos, _display_at(decl))
                   for pos, decl in decls
                   if pos < mobile_start and _display_at(decl) == "none"]
        mobile_rule = [(pos, _display_at(decl))
                       for pos, decl in decls
                       if pos > mobile_start and _display_at(decl) == mobile_display]
        assert desktop, f"桌面端 {selector} 没有 display:none"
        assert mobile_rule, f"手机断点没有把 {selector} 放成 {mobile_display}"
        assert min(pos for pos, _ in mobile_rule) > max(pos for pos, _ in desktop), \
            f"手机断点的 {selector} 显示规则排在桌面隐藏规则之前，可能被覆盖"
        assert _display_at(decls[-1][1]) == mobile_display, \
            f"{selector} 最后一条 display 不是手机断点的 {mobile_display}（有更晚的覆盖）"

    assert any(_display_at(decl) == "none"
               for pos, decl in _css_decls(".desktop-only") if pos > mobile_start), \
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
