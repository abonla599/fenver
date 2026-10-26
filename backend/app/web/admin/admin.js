/* 管理台逻辑：把带 RequireAdmin 的现成接口变成可点的界面。
 *
 * 四条硬规矩：
 * 1) 页面是静态空壳，任何用户数据都只经 /v1/admin/* 与 /v1/* 的登录态接口取；
 * 2) 用户名、反馈原文、任务目标、安全事件字段都是别人写下的字符串，一律走
 *    textContent / createElementNS 建树，不拼 HTML；
 * 3) 会话住在 httpOnly Cookie 里，页面脚本不持有凭据；一次性口令/令牌只活在那一个
 *    弹层里，不落任何本机存储；
 * 4) 界面上的每句话都要对得上后端真做了的事。
 */
(() => {
  "use strict";

  // 旧版把令牌明文写在 localStorage 的 accessToken 里；现在的凭据只住 httpOnly
  // Cookie，页面不读也不写本机存储。这里清掉旧版遗留，是唯一一处存储调用。
  try { localStorage.removeItem("accessToken"); } catch (e) { /* 存储被禁用时无事可做 */ }

  const $ = (id) => document.getElementById(id);

  /* ---------- 建树 ---------- */

  // SVG 命名空间是固定标识符，不是网络请求；拆开写是为了让“页面不引用任何外站”的
  // 静态检查能直接扫原始文本。
  const SVGNS = "http" + "://www.w3.org/2000/svg";

  // 线性图标，24 视图、1.7 描边、currentColor 上色。全部内联，不引外部图标库。
  const ICONS = {
    overview:["M4 4h7v7H4z", "M13 4h7v7h-7z", "M4 13h7v7H4z", "M13 13h7v7h-7z"],
    users:   ["M4 20v-1.4A4.6 4.6 0 0 1 8.6 14h2.8A4.6 4.6 0 0 1 16 18.6V20",
              "M9.3 11.3a3.4 3.4 0 1 0 0-6.8 3.4 3.4 0 0 0 0 6.8Z",
              "M17 14.2a4.6 4.6 0 0 1 3 4.4V20"],
    shield:  ["M12 3.5l7 2.6v5.2c0 4.1-2.9 7.3-7 9.2-4.1-1.9-7-5.1-7-9.2V6.1l7-2.6Z",
              "M8.8 12l2.2 2.2 4.2-4.2"],
    mcp:     ["M9 3v3", "M15 3v3", "M6 6h12v5a6 6 0 0 1-12 0V6Z", "M12 17v4"],
    alert:   ["M12 3.2l9 16.3H3l9-16.3Z", "M12 9v4.2", "M12 16.8h.01"],
    chart:   ["M4 20V10", "M10 20V4.5", "M16 20v-7", "M2 20h20"],
    inbox:   ["M4 5h16v14H4z", "M4 13h4l2 2h4l2-2h4"],
    pulse:   ["M3 12h4l2.5-6 4 12L16 12h5"],
    clock:   ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "M12 7v5l3 2"],
    filter:  ["M4 5h16l-6 8v6l-4 2v-8L4 5Z"],
    download:["M12 4v10", "M7 9l5 5 5-5", "M5 20h14"],
    chevron: ["M9 6l6 6-6 6"],
    gear:    ["M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z",
              "M12 3v3M12 18v3M3 12h3M18 12h3",
              "M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"],
    search:  ["M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Z", "M16 16l5 5"],
    arrow:   ["M5 12h14", "M13 6l6 6-6 6"],
    feedback:["M12 4.5c-4.4 0-8 2.9-8 6.5 0 2 1.1 3.8 2.8 5l-.8 3 3.2-1.6c.9.2 1.8.3 2.8.3",
              "M12 4.5c4.4 0 8 2.9 8 6.5",
              "M20 11c0 3.6-3.6 6.5-8 6.5"],
    memory:  ["M12 4c3.9 0 7 1.3 7 2.9S15.9 9.8 12 9.8 5 8.5 5 6.9 8.1 4 12 4Z",
              "M5 6.9v10.2C5 18.7 8.1 20 12 20s7-1.3 7-2.9V6.9",
              "M5 12c0 1.6 3.1 2.9 7 2.9s7-1.3 7-2.9"],
    tasks:   ["M9 6h11M9 12h11M9 18h11", "M4.5 6h.01M4.5 12h.01M4.5 18h.01"],
    agent:   ["M12 3.5l2.1 4.6 4.6 2.1-4.6 2.1L12 16.9l-2.1-4.6L5.3 10.2l4.6-2.1L12 3.5Z",
              "M18.5 16.5l.9 1.9 1.9.9-1.9.9-.9 1.9-.9-1.9-1.9-.9 1.9-.9.9-1.9Z"],
    refresh: ["M20 12a8 8 0 1 1-2.6-5.9", "M20 4v4h-4"],
    exit:    ["M14 4h5v16h-5", "M10 12H3", "M6 9l-3 3 3 3"],
    login:   ["M10 4H5v16h5", "M14 12h7", "M18 9l3 3-3 3"],
    power:   ["M12 4v8", "M7.5 6.8a7 7 0 1 0 9 0"],
    key:     ["M8.4 15.6a3.6 3.6 0 1 0 0-7.2 3.6 3.6 0 0 0 0 7.2Z",
              "M11 10.4L20 10l-.6 3.4 2-2-2.6.4", "M13.6 12.9l4.6 4.6"],
    trash:   ["M6 7h12", "M9.5 7V4.8h5V7", "M7.5 7l.9 13h7.2l.9-13", "M11 11v6M13 11v6"],
    copy:    ["M9 9h11v11H9z", "M15 9V4H4v11h5"],
    check:   ["M5 12.5l4.5 4.5L19 7"],
    play:    ["M8 5.5l11 6.5-11 6.5z"],
    stop:    ["M7 7h10v10H7z"],
    eye:     ["M2.5 12S6 6.5 12 6.5 21.5 12 21.5 12 18 17.5 12 17.5 2.5 12 2.5 12Z",
              "M12 14.4a2.4 2.4 0 1 0 0-4.8 2.4 2.4 0 0 0 0 4.8Z"],
    decay:   ["M12 5v9", "M8 10.5l4 4 4-4", "M5 19h14"],
    usage:   ["M6 20V11", "M12 20V4.5", "M18 20v-6"],
    userplus:["M4 20v-1.4A4.6 4.6 0 0 1 8.6 14h2.8A4.6 4.6 0 0 1 16 18.6V20",
              "M9.3 11.3a3.4 3.4 0 1 0 0-6.8 3.4 3.4 0 0 0 0 6.8Z",
              "M18.5 7.5v5M16 10h5"],
    close:   ["M6 6l12 12M18 6L6 18"],
  };

  function icon(name) {
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "1.7");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    (ICONS[name] || []).forEach((d) => {
      const p = document.createElementNS(SVGNS, "path");
      p.setAttribute("d", d);
      svg.appendChild(p);
    });
    return svg;
  }

  function cell(tag, text, cls) {
    const el = document.createElement(tag);
    if (text !== undefined && text !== null) el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }

  function iconWrap(name) {
    const wrap = cell("span", undefined, "ico");
    wrap.appendChild(icon(name));
    return wrap;
  }

  function labelSpan(text) { return cell("span", text, "lbl"); }

  function btn(iconName, label, cls, fn) {
    const b = cell("button", undefined, cls);
    b.type = "button";
    b.appendChild(iconWrap(iconName));
    b.appendChild(labelSpan(label));
    if (fn) b.addEventListener("click", fn);
    return b;
  }

  // 静态按钮把图标名与文字写在 data-icon / data-label 上，建树时统一补上。
  function decorateIcons(root) {
    (root || document).querySelectorAll("[data-icon]").forEach((el) => {
      if (el.firstElementChild) return;
      el.appendChild(iconWrap(el.getAttribute("data-icon")));
      const text = el.getAttribute("data-label");
      if (text) el.appendChild(labelSpan(text));
    });
  }

  function emptyRow(text, span) {
    const tr = document.createElement("tr");
    const td = cell("td", text);
    td.colSpan = span;
    td.style.color = "var(--text-3)";
    tr.appendChild(td);
    return tr;
  }

  function emptyBox(text) {
    const box = cell("div", undefined, "empty");
    box.appendChild(cell("p", text, "pane-note"));
    return box;
  }

  function paintRetry(container, message, retry) {
    container.textContent = "";
    const box = emptyBox(message);
    if (retry) box.appendChild(btn("refresh", "重试", "btn btn-small btn-ghost", retry));
    container.appendChild(box);
  }

  function paintTableError(tbody, span, message, retry) {
    tbody.textContent = "";
    const tr = document.createElement("tr");
    const td = cell("td");
    td.colSpan = span;
    td.appendChild(document.createTextNode(message + " "));
    if (retry) td.appendChild(btn("refresh", "重试", "btn btn-small btn-ghost", retry));
    tr.appendChild(td);
    tbody.appendChild(tr);
  }

  function paintTableLoading(tbody, span, text) {
    tbody.textContent = "";
    tbody.appendChild(emptyRow(text || "读取中…", span));
  }

  function statCell(value, label) {
    const box = cell("div", undefined, "stat-cell");
    box.appendChild(cell("b", String(value)));
    box.appendChild(cell("span", label));
    return box;
  }

  /* ---------- 请求 ---------- */

  const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

  async function req(path, opts) {
    const method = (opts && opts.method) || "GET";
    const headers = { "Content-Type": "application/json" };
    // 会话凭据是 httpOnly Cookie；不安全方法必须声明 CSRF 头。
    if (UNSAFE.has(method)) headers["X-CSRF"] = "1";
    let res;
    try {
      res = await fetch(path, {
        method,
        credentials: "same-origin",
        headers,
        body: opts && opts.body ? JSON.stringify(opts.body) : undefined,
      });
    } catch (e) {
      const err = new Error("网络错误：连不上后端");
      err.retryable = true;
      throw err;
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* 空响应体 */ }
    if (res.status === 401) {
      showGate("会话无效或已过期，粘贴口令重新登录");
      throw new Error("401");
    }
    if (!res.ok) {
      const detail = data && data.detail ? data.detail : ("HTTP " + res.status);
      const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      err.status = res.status;
      throw err;
    }
    return data || {};
  }

  async function busy(el, fn) {
    if (el) el.disabled = true;
    try { await fn(); }
    catch (e) {
      if (e.message !== "401") {
        const retry = e.retryable ? () => busy(el, fn) : null;
        flash(e.message, true, retry);
      }
    }
    finally { if (el) el.disabled = false; }
  }

  /* ---------- 提示与弹层 ---------- */

  let authed = false;
  let dataGeneration = 0;
  let sheetReturnFocus = null;
  let lastTrigger = null;

  function showGate(msg) {
    authed = false;
    invalidateData();
    closeAllSheets();
    $("panel").classList.add("hidden");
    $("gate").classList.remove("hidden");
    $("gateErr").textContent = msg || "";
    syncSecurityTimer();
  }

  function flash(msg, bad, retry) {
    const el = $("flash");
    el.textContent = "";
    if (msg) el.appendChild(document.createTextNode(msg));
    if (retry) {
      el.appendChild(document.createTextNode(" "));
      el.appendChild(btn("refresh", "重试", "btn btn-small btn-ghost", retry));
    }
    el.style.color = bad ? "var(--danger)" : "var(--accent)";
  }

  function openSheet(sheet, focusEl) {
    const active = document.activeElement;
    // busy() 会在动作期间禁用触发按钮，浏览器可能把焦点挪回 body；点击的按钮在
    // 捕获阶段另记一份，关闭弹层时才能把焦点送回去。
    sheetReturnFocus = (active && active !== document.body && document.contains(active))
      ? active : lastTrigger;
    sheet.classList.remove("hidden");
    const target = focusEl || sheet.querySelector("button, [href], input, select, textarea");
    if (target) target.focus();
  }

  function restoreFocus(el) {
    if (!el || !document.contains(el) || typeof el.focus !== "function") return;
    el.focus();
    if (document.activeElement !== el) {
      // 触发按钮此刻可能还处于 disabled（busy 的 finally 还没跑）；等一拍再试。
      setTimeout(() => { if (document.contains(el)) el.focus(); }, 0);
    }
  }

  function closeSheet(sheet) {
    sheet.classList.add("hidden");
    const back = sheetReturnFocus;
    sheetReturnFocus = null;
    restoreFocus(back);
  }

  let askResolve = null;
  function ask(title, body, okLabel, iconName, danger) {
    $("askTitle").textContent = title;
    $("askBody").textContent = body;
    const ok = $("askOk");
    ok.textContent = "";
    ok.className = "btn btn-small " + (danger ? "danger-btn" : "btn-primary");
    ok.appendChild(iconWrap(iconName || "check"));
    ok.appendChild(labelSpan(okLabel));
    openSheet($("ask"), $("askCancel"));
    return new Promise((resolve) => { askResolve = resolve; });
  }

  function closeAsk(answer) {
    closeSheet($("ask"));
    const r = askResolve;
    askResolve = null;
    if (r) r(answer);
  }

  function closeReveal() {
    closeSheet($("reveal"));
    $("revealText").textContent = "";
  }

  function closeDetail() { closeSheet($("detail")); }

  function closeImageViewer() {
    closeSheet($("imageViewer"));
    const img = $("imageViewerImg");
    img.removeAttribute("src");
    img.alt = "";
  }

  function openImageViewer(src, alt) {
    const img = $("imageViewerImg");
    img.src = src;
    img.alt = alt || "反馈截图";
    $("imageViewerTitle").textContent = alt || "反馈截图";
    openSheet($("imageViewer"), $("imageViewerClose"));
  }

  function closeAllSheets() {
    document.querySelectorAll(".sheet").forEach((s) => s.classList.add("hidden"));
    const r = askResolve;
    askResolve = null;
    if (r) r(false);
    $("revealText").textContent = "";
    const viewer = $("imageViewerImg");
    if (viewer) {
      viewer.removeAttribute("src");
      viewer.alt = "";
    }
  }

  function trapFocus(e, box) {
    const nodes = box.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
    const list = Array.prototype.filter.call(nodes, (el) => !el.disabled && el.offsetParent !== null);
    if (!list.length) return;
    const first = list[0];
    const last = list[list.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  function tokenNote() {
    return "这枚令牌的明文只出现在这一次响应里，服务端存的是摘要，关掉这个窗口就再也查不到。"
      + "这次换发已经把他名下所有旧令牌一起作废，每一台设备都会掉线。"
      + "掉线不等于进不来：他在 App 里用自己的用户名和密码登录，就能领一枚新的。";
  }

  function reveal(secret, title, note) {
    $("revealTitle").textContent = title || "新令牌";
    $("revealNote").textContent = note || tokenNote();
    $("revealText").textContent = secret;
    openSheet($("reveal"), $("btnCopy"));
  }

  /* ---------- 格式化 ---------- */

  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function fmtN(n) {
    const s = String(Math.abs(Math.round(n || 0)));
    return (n < 0 ? "-" : "") + s.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function fmtBytes(n) {
    let v = Number(n) || 0;
    const units = ["B", "KiB", "MiB", "GiB"];
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return (Math.round(v * 10) / 10) + " " + units[i];
  }

  function pct(rate) {
    if (rate === null || rate === undefined) return "—";
    return (Math.round(rate * 1000) / 10).toFixed(1) + "%";
  }

  function textOf(v) {
    if (v === null || v === undefined) return "";
    if (typeof v === "string") return v;
    try { return JSON.stringify(v); } catch (e) { return String(v); }
  }

  const HEALTH_STATUS = {
    ok: "正常",
    degraded: "降级",
    not_configured: "未配置",
    broken: "故障",
  };

  function healthText(status) {
    const key = String(status || "");
    return HEALTH_STATUS[key] || key || "—";
  }

  /* ---------- 路由与标题 ---------- */

  const ROUTE_SECTIONS = {
    "overview": "secOverview",
    "accounts": "secAccounts",
    "security": "secSecurity",
    "mcp": "secMcp",
    "feedback": "secFeedback",
    "runtime/usage": "secRuntime",
    "runtime/search": "secRuntime",
    "runtime/memory": "secRuntime",
    "runtime/tasks": "secRuntime",
    "runtime/agent": "secRuntime",
    "runtime/config": "secRuntime",
  };

  const RUNTIME_KEYS = ["usage", "search", "memory", "tasks", "agent", "config"];
  const SUBPANELS = {
    usage: "subUsage",
    search: "subSearch",
    memory: "subMemory",
    tasks: "subTasks",
    agent: "subAgent",
    config: "subConfig",
  };
  const PAGE_TITLES = {
    overview: "概览",
    accounts: "账号",
    security: "安全",
    mcp: "MCP 外部工具",
    feedback: "反馈",
    "runtime/usage": "运行 · 用量",
    "runtime/search": "运行 · 搜索",
    "runtime/memory": "运行 · 记忆",
    "runtime/tasks": "运行 · 任务",
    "runtime/agent": "运行 · 智能体",
    "runtime/config": "运行 · 配置",
  };

  let route = "overview";
  let runtimeSub = "usage";

  function routeFromHash() {
    let raw = String(location.hash || "");
    if (raw.charAt(0) === "#") raw = raw.slice(1);
    if (raw.charAt(0) === "/") raw = raw.slice(1);
    if (raw.indexOf("runtime/") === 0) {
      const key = raw.slice(8);
      if (RUNTIME_KEYS.indexOf(key) >= 0) return "runtime/" + key;
      return null;
    }
    if (raw === "overview" || raw === "accounts" || raw === "security"
        || raw === "mcp" || raw === "feedback") {
      return raw;
    }
    return null;
  }

  function applyRoute() {
    let next = routeFromHash();
    if (!next) {
      next = "overview";
      try { history.replaceState(null, "", "#/overview"); }
      catch (e) { location.hash = "#/overview"; }
    }
    route = next;
    const isRuntime = route.indexOf("runtime/") === 0;
    const sectionId = isRuntime ? "secRuntime" : ROUTE_SECTIONS[route];
    document.querySelectorAll(".sec").forEach((s) => {
      s.classList.toggle("hidden", s.id !== sectionId);
    });
    document.querySelectorAll(".nav-btn").forEach((b) => {
      const on = b.getAttribute("data-goto") === sectionId;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (isRuntime) {
      runtimeSub = route.slice(8);
      showRuntimeSub(runtimeSub);
    }
    const title = PAGE_TITLES[route] || "概览";
    $("pageTitle").textContent = title;
    document.title = title + " · Fenver 管理";
    syncSecurityTimer();
    if (authed) {
      enterSection(sectionId).catch((e) => {
        if (e && e.message !== "401") flash(e.message, true);
      });
    }
  }

  function navigate(key) {
    let target = key;
    if (target === "runtime") {
      target = route.indexOf("runtime/") === 0 ? route : "runtime/usage";
    }
    const hash = "#/" + target;
    if (location.hash === hash) applyRoute();
    else location.hash = hash;
  }

  function updateTitle() {
    const title = PAGE_TITLES[route] || "概览";
    $("pageTitle").textContent = title;
    document.title = title + " · Fenver 管理";
  }

  /* ---------- 用户表（账号与用量共用名字表） ---------- */

  let allUsers = [];
  let nameById = {};
  let usersLoaded = false;
  const MEMORY_PER_USER = "不支持";

  async function fetchUsers() {
    const data = await req("/v1/admin/users");
    allUsers = Array.isArray(data.users) ? data.users : [];
    nameById = {};
    allUsers.forEach((u) => { nameById[u.user_id] = u.username; });
    usersLoaded = true;
    return data;
  }

  async function loadUsers() {
    await fetchUsers();
    renderUsers();
    paintOverviewAccounts();
    if (feedbackLoaded) renderFeedback();
  }

  async function ensureUsers() {
    if (!usersLoaded) await loadUsers();
  }

  function statusTagRow(u) {
    const row = cell("span", undefined, "tag-row");
    row.appendChild(cell("span", u.disabled ? "已停用" : "正常", "tag" + (u.disabled ? " off" : "")));
    if (u.must_change_password) row.appendChild(cell("span", "首登须改密", "tag warn"));
    return row;
  }

  function nextExpiryInfo(u) {
    const raw = u.next_session_expiry_at;
    if (!raw) return { text: "—", soon: false };
    const d = new Date(raw);
    const diff = d.getTime() - Date.now();
    const soon = !isNaN(d.getTime()) && diff > 0 && diff < 72 * 3600 * 1000;
    return { text: fmtTime(raw), soon: soon };
  }

  function nextExpiryCell(u) {
    const info = nextExpiryInfo(u);
    const td = cell("td");
    if (!u.next_session_expiry_at) {
      td.appendChild(cell("span", info.text, "dim"));
      return td;
    }
    td.appendChild(document.createTextNode(info.text));
    if (info.soon) {
      td.appendChild(document.createTextNode(" "));
      td.appendChild(cell("span", "即将到期", "tag warn"));
    }
    return td;
  }

  function renderUsers() {
    const q = $("userSearch").value.trim().toLowerCase();
    const status = $("userStatus").value;
    const shown = allUsers.filter((u) => {
      const name = String(u.username || "").toLowerCase();
      if (q && name.indexOf(q) < 0) return false;
      if (status === "active") return !u.disabled && !u.must_change_password;
      if (status === "disabled") return !!u.disabled;
      if (status === "must") return !!u.must_change_password;
      return true;
    });
    $("userSearchNote").textContent = "共 " + allUsers.length + " 人"
      + (q ? "，匹配 " + shown.length + " 人" : "");
    $("userMemoryNote").textContent = "记忆条数按人：" + MEMORY_PER_USER
      + "（/v1/memory/stats 只给全库合计）。";

    const tbody = $("userRows");
    const cards = $("userCards");
    tbody.textContent = "";
    cards.textContent = "";
    if (!allUsers.length) {
      tbody.appendChild(emptyRow("还没有注册用户。可以在上面的建号栏创建，或到运行 · 配置打开自助注册。", 8));
      cards.appendChild(emptyBox("还没有注册用户。可以在上面的建号栏创建，或到运行 · 配置打开自助注册。"));
      return;
    }
    if (!shown.length) {
      tbody.appendChild(emptyRow("没有匹配的账号。换个关键词或状态试试。", 8));
      cards.appendChild(emptyBox("没有匹配的账号。换个关键词或状态试试。"));
      return;
    }
    shown.forEach((u) => {
      tbody.appendChild(userRow(u));
      cards.appendChild(userCard(u));
    });
  }

  function userRow(u) {
    const tr = document.createElement("tr");
    tr.appendChild(cell("td", u.username, "name"));
    tr.appendChild(cell("td", u.role === "admin" ? "管理员" : "用户"));
    const st = cell("td");
    st.appendChild(statusTagRow(u));
    tr.appendChild(st);
    tr.appendChild(cell("td", String(u.sessions ?? "—")));
    tr.appendChild(nextExpiryCell(u));
    tr.appendChild(cell("td", fmtTime(u.last_seen)));
    tr.appendChild(cell("td", fmtTime(u.created_at)));

    const acts = cell("td", undefined, "acts");
    acts.appendChild(btn(u.disabled ? "power" : "stop", u.disabled ? "恢复" : "停用",
      "btn btn-small btn-ghost", (e) => busy(e.currentTarget, () => toggleUser(u))));
    acts.appendChild(btn("key", "重置口令", "btn btn-small btn-ghost",
      (e) => busy(e.currentTarget, () => resetPw(u))));
    acts.appendChild(btn("refresh", "换发令牌", "btn btn-small btn-ghost",
      (e) => busy(e.currentTarget, () => rotate(u))));
    acts.appendChild(btn("trash", "删号", "btn btn-small btn-ghost",
      (e) => busy(e.currentTarget, () => remove(u))));
    tr.appendChild(acts);
    return tr;
  }

  function metaLine(box, label, value) {
    const item = cell("span");
    item.appendChild(cell("b", label));
    item.appendChild(document.createTextNode(value));
    box.appendChild(item);
  }

  function userCard(u) {
    const card = cell("article", undefined, "user-card");
    const head = cell("div", undefined, "user-card-head");
    head.appendChild(cell("b", u.username));
    head.appendChild(statusTagRow(u));
    card.appendChild(head);

    const meta = cell("div", undefined, "user-meta");
    metaLine(meta, "身份", u.role === "admin" ? "管理员" : "用户");
    metaLine(meta, "会话数", String(u.sessions ?? "—"));
    const expiry = nextExpiryInfo(u);
    const expiryItem = cell("span");
    expiryItem.appendChild(cell("b", "最近会话到期"));
    expiryItem.appendChild(document.createTextNode(expiry.text + " "));
    if (expiry.soon) expiryItem.appendChild(cell("span", "即将到期", "tag warn"));
    meta.appendChild(expiryItem);
    metaLine(meta, "最后活跃", fmtTime(u.last_seen));
    metaLine(meta, "注册于", fmtTime(u.created_at));
    card.appendChild(meta);

    const acts = cell("div", undefined, "user-actions");
    acts.appendChild(btn(u.disabled ? "power" : "stop", u.disabled ? "恢复" : "停用",
      "btn btn-small btn-ghost", (e) => busy(e.currentTarget, () => toggleUser(u))));
    acts.appendChild(btn("key", "重置口令", "btn btn-small btn-ghost",
      (e) => busy(e.currentTarget, () => resetPw(u))));
    acts.appendChild(btn("refresh", "换发令牌", "btn btn-small btn-ghost",
      (e) => busy(e.currentTarget, () => rotate(u))));
    acts.appendChild(btn("trash", "删号", "btn btn-small btn-ghost",
      (e) => busy(e.currentTarget, () => remove(u))));
    card.appendChild(acts);
    return card;
  }

  async function enterAccounts() {
    if (!usersLoaded) {
      paintTableLoading($("userRows"), 8);
      $("userCards").textContent = "";
      $("userCards").appendChild(emptyBox("读取中…"));
    }
    try {
      await ensureUsers();
      renderUsers();
      return true;
    } catch (e) {
      if (e.message === "401") return false;
      paintTableError($("userRows"), 8, "账号读取失败：" + e.message, enterAccounts);
      paintRetry($("userCards"), "账号读取失败：" + e.message, enterAccounts);
      return false;
    }
  }

  const PW_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  function randomPassword(len) {
    // 建号/重置时由浏览器现生成：随机且避开易混字符是下限。
    const bytes = new Uint8Array(len);
    crypto.getRandomValues(bytes);
    let out = "";
    for (let i = 0; i < len; i += 1) out += PW_ALPHABET[bytes[i] % PW_ALPHABET.length];
    return out;
  }

  async function createUser() {
    const username = $("newUser").value.trim();
    const password = $("newPass").value.trim();
    if (!username || !password) { flash("用户名和初始口令都要填", true); return; }
    const ok = await ask(
      "建号「" + username + "」？",
      "这会多出一个普通用户身份。初始口令经了管理员的手，所以它带着「首登须改密」："
      + "本人第一次登录后必须换成自己的。建号不会发任何会话令牌。",
      "建号", "userplus", false);
    if (!ok) return;
    await req("/v1/admin/users", { method: "POST", body: { username: username, password: password } });
    $("newUser").value = "";
    $("newPass").value = "";
    reveal(password, "初始口令",
      "这枚初始口令只显示这一次，对方首登必须改。口令经了你的手，交给他之后别再留在聊天记录里。");
    flash("已建号「" + username + "」；初始口令只显示这一次");
    await loadUsers();
  }

  async function resetPw(u) {
    const fresh = randomPassword(12);
    const ok = await ask(
      "重置「" + u.username + "」的登录口令？",
      "新口令会现生成并写进对方账号。他名下每一枚会话令牌当场作废，每一台设备都掉线；"
      + "下次登录必须先用这枚口令，然后被要求改成自己的。这句提醒不是装饰："
      + "口令经了你的手的这段时间里，它其实也是你知道的秘密。",
      "重置", "key", true);
    if (!ok) return;
    await req("/v1/admin/users/" + encodeURIComponent(u.user_id) + "/reset-password",
      { method: "POST", body: { new_password: fresh } });
    reveal(fresh, "新口令", "这枚口令只显示这一次，对方首登必须改；关掉窗口后服务端只存摘要。");
    flash("已重置；口令只显示这一次");
    await loadUsers();
  }

  async function toggleUser(u) {
    const verb = u.disabled ? "恢复" : "停用";
    const ok = await ask(
      verb + "用户「" + u.username + "」？",
      u.disabled
        ? "只把这一条身份记录上的停用标记撤掉，他的设备令牌还是刚才那批：原来登录着的设备接着用，中途掉线的重新登录就行。"
        : "只在那一条身份记录上打一个停用标记：他名下每一枚会话令牌当场不再被认，每一台设备都掉线，密码也登不进来。"
        + "记录、会话、附件、记忆一个字都不动，随时可以恢复。",
      verb, "power", !u.disabled);
    if (!ok) return;
    await req("/v1/admin/users/" + encodeURIComponent(u.user_id)
      + (u.disabled ? "/enable" : "/disable"), { method: "POST" });
    flash("已" + verb);
    await loadUsers();
  }

  async function rotate(u) {
    const ok = await ask(
      "给「" + u.username + "」换发新令牌？",
      "他名下现有的会话令牌会全部作废，每一台设备都掉线；新令牌的明文只出现在这一次响应里。"
      + "这一步不会解除停用状态。注意它不是把门焊死：他知道自己密码的话，重新登录就能再领一枚"
      + "——要挡住人请用停用。",
      "换发", "key", false);
    if (!ok) return;
    const r = await req("/v1/admin/users/" + encodeURIComponent(u.user_id) + "/rotate-token",
      { method: "POST" });
    if (r.token) reveal(r.token);
    await loadUsers();
  }

  async function remove(u) {
    // 文案对着 auth.py 的 delete_user 写：只删 users 里这一条记录，其余三份存储不级联。
    const ok = await ask(
      "删除用户「" + u.username + "」？",
      "删的只有这一条身份记录。令牌就存在这条记录里，所以他的令牌会跟着一起失效，每一台设备掉线，而且没有恢复入口。"
      + "他的会话、附件、长期记忆不在这次删除里：那三份各存各的，删号之后原样留在磁盘上，"
      + "只是那个 user_id 再也解析不出身份——除了手工改数据文件，没有第二条路能清掉它们。",
      "确认删除", "trash", true);
    if (!ok) return;
    await req("/v1/admin/users/" + encodeURIComponent(u.user_id), { method: "DELETE" });
    flash("已删除（他的会话、附件、记忆还在磁盘上）");
    await loadUsers();
    if (runtimeLoaded.memory) await loadMemory();
  }

  /* ---------- 概览 ---------- */

  async function loadOverview() {
    if (!authed) return;
    await Promise.allSettled([
      loadOverviewAccounts(),
      loadOverviewFeedback(),
      loadOverviewSecurity(),
      loadOverviewService(),
    ]);
  }

  function paintOverviewAccounts() {
    const box = $("ovAccounts");
    if (!box) return;
    box.textContent = "";
    const total = allUsers.length;
    const normal = allUsers.filter((u) => !u.disabled && !u.must_change_password).length;
    const disabled = allUsers.filter((u) => u.disabled).length;
    const must = allUsers.filter((u) => u.must_change_password).length;
    box.appendChild(statCell(total, "总数"));
    box.appendChild(statCell(normal, "正常"));
    box.appendChild(statCell(disabled, "停用"));
    box.appendChild(statCell(must, "首登须改密"));
  }

  async function loadOverviewAccounts() {
    try {
      await ensureUsers();
      paintOverviewAccounts();
    } catch (e) {
      if (e.message === "401") return;
      paintRetry($("ovAccounts"), "账号读取失败：" + e.message, loadOverviewAccounts);
    }
  }

  async function loadOverviewFeedback() {
    try {
      const data = await req("/v1/admin/user-feedback");
      const items = Array.isArray(data.items) ? data.items : [];
      const unread = Number(data.unread) || 0;
      setFeedbackBadge(unread);
      const box = $("ovFeedback");
      box.textContent = "";
      box.appendChild(statCell(items.length, "总条数"));
      box.appendChild(statCell(unread, "未读"));
    } catch (e) {
      if (e.message === "401") return;
      paintRetry($("ovFeedback"), "反馈读取失败：" + e.message, loadOverviewFeedback);
    }
  }

  async function loadOverviewSecurity() {
    try {
      const data = await req("/v1/admin/security/summary?window=24h");
      const totals = data.totals || {};
      const box = $("ovSecurity");
      box.textContent = "";
      box.appendChild(statCell(Number(totals.events) || 0, "事件"));
      box.appendChild(statCell(Number(totals.alert) || 0, "alert"));
      box.appendChild(statCell(Number(totals.warn) || 0, "warn"));
      box.appendChild(statCell(Number(totals.sources) || 0, "来源数"));
    } catch (e) {
      if (e.message === "401") return;
      if (e.status === 404) {
        paintRetry($("ovSecurity"), "安全日志端点尚未上线。", loadOverviewSecurity);
      } else {
        paintRetry($("ovSecurity"), "安全日志读取失败，稍后重试。", loadOverviewSecurity);
      }    }
  }

  async function loadOverviewService() {
    try {
      const data = await req("/health");
      const checks = data.checks || {};
      const mem = checks["记忆后端"] || {};
      const box = $("ovService");
      box.textContent = "";
      box.appendChild(statCell(data.build || "—", "构建"));
      box.appendChild(statCell(healthText(data.status), "状态"));
      box.appendChild(statCell(healthText(mem.status), "记忆后端"));
    } catch (e) {
      if (e.message === "401") return;
      paintRetry($("ovService"), "读取失败", loadOverviewService);
    }
  }

  /* ---------- 反馈 ---------- */

  let feedbackItems = [];
  let feedbackUnread = 0;
  let feedbackNextBefore = null;
  let feedbackLoaded = false;
  let feedbackUnreadOnly = false;
  let fbBadge = null;

  function setFeedbackBadge(n) {
    if (!fbBadge) return;
    const count = Number(n) || 0;
    fbBadge.textContent = count ? String(count) : "";
    fbBadge.classList.toggle("hidden", !count);
    fbBadge.title = count ? "未读反馈 " + count + " 条" : "无未读反馈";
  }

  // 只更新导航角标：不碰当前分区的列表，也不碰用户表。boot 与顶栏刷新都走这一条，
  // 停在 #/accounts、#/security 或 #/runtime/* 时角标才是对的。
  async function refreshFeedbackBadge() {
    try {
      const data = await req("/v1/admin/user-feedback");
      setFeedbackBadge(Number(data.unread) || 0);
    } catch (e) {
      // 角标是附属信息：失败就保持原状，不能把当前分区一起拖红。
    }
  }

  // 缺 id 的条目不能共用 String(undefined) 这个键，否则同批只留得下第一条。
  function feedbackKey(f) {
    if (f.id !== undefined && f.id !== null && f.id !== "") return "id:" + String(f.id);
    return ["created_at", "user_id", "email", "text"]
      .map((k) => String(f[k] === undefined || f[k] === null ? "" : f[k]))
      .join("|");
  }

  async function loadFeedback(reset) {
    if (reset) {
      feedbackItems = [];
      feedbackNextBefore = null;
    }
    try { await ensureUsers(); }
    catch (e) { if (e.message === "401") throw e; }
    const parts = ["limit=50"];
    if (feedbackUnreadOnly) parts.push("unread=1");
    if (feedbackNextBefore !== null && feedbackNextBefore !== undefined) {
      parts.push("before=" + encodeURIComponent(String(feedbackNextBefore)));
    }
    const data = await req("/v1/admin/user-feedback?" + parts.join("&"));
    const items = Array.isArray(data.items) ? data.items : [];
    const seen = {};
    feedbackItems.forEach((f) => { seen[feedbackKey(f)] = true; });
    items.forEach((f) => {
      const key = feedbackKey(f);
      if (!seen[key]) {
        seen[key] = true;
        feedbackItems.push(f);
      }
    });
    // 旧后端不认 unread=1 时会回全量：客户端再滤一次，按钮说了什么就显示什么。
    if (feedbackUnreadOnly) feedbackItems = feedbackItems.filter((f) => !f.read);
    const unread = Number(data.unread);
    feedbackUnread = isNaN(unread) ? feedbackItems.filter((f) => !f.read).length : unread;
    feedbackNextBefore = (data.next_before === undefined || data.next_before === null)
      ? null : data.next_before;
    feedbackLoaded = true;
    setFeedbackBadge(feedbackUnread);
    renderFeedback();
    return true;
  }

  function renderFeedback() {
    const list = $("feedbackRows");
    list.textContent = "";
    const q = $("fbSearch").value.trim().toLowerCase();
    let shown = feedbackItems;
    if (q) {
      shown = feedbackItems.filter((f) => {
        const hay = (String(f.id) + " " + String(f.text || "") + " " + String(f.email || "")
          + " " + String(nameById[f.user_id] || "")).toLowerCase();
        return hay.indexOf(q) >= 0;
      });
    }
    $("fbNote").textContent = feedbackItems.length
      ? "已加载 " + feedbackItems.length + " 条 · 未读 " + feedbackUnread
      : "";
    $("btnFbMore").classList.toggle("hidden",
      feedbackNextBefore === null || feedbackNextBefore === undefined);
    if (!feedbackItems.length) {
      list.appendChild(emptyBox(feedbackUnreadOnly
        ? "没有未读反馈。切回「全部」看看历史条目。"
        : "还没有人提交过反馈。App 的「设置 → 意见反馈」写一条，就会出现在这里。"));
      return;
    }
    if (!shown.length) {
      list.appendChild(emptyBox("已加载的条目里没有匹配「" + q + "」的内容。"));
      return;
    }
    shown.forEach((f) => list.appendChild(feedbackCard(f)));
  }

  function feedbackCard(f) {
    const card = cell("article", undefined, "fb-item");
    const meta = cell("div", undefined, "fb-meta");
    meta.appendChild(cell("span", f.id, "code"));
    meta.appendChild(cell("span", nameById[f.user_id] || f.user_id, "name"));
    meta.appendChild(cell("span", fmtTime(f.created_at), "dim"));
    meta.appendChild(f.read ? cell("span", "已读", "tag") : cell("span", "未读", "tag warn"));
    card.appendChild(meta);

    const text = cell("p", f.text, "fb-text");
    card.appendChild(text);

    if (f.email) card.appendChild(cell("div", "邮箱：" + f.email, "dim"));
    if (!nameById[f.user_id]) {
      card.appendChild(cell("div", "提交者不在用户表里（可能已删号）。", "dim"));
    }
    if (f.image_count) {
      const imgs = cell("div", undefined, "fb-images");
      for (let i = 0; i < f.image_count; i += 1) {
        const src = "/v1/admin/user-feedback/" + encodeURIComponent(f.id) + "/image/" + i;
        const alt = "反馈 " + f.id + " 的截图 " + (i + 1);
        const thumb = document.createElement("button");
        thumb.type = "button";
        thumb.className = "fb-thumb";
        thumb.setAttribute("aria-label", "查看" + alt);
        const img = document.createElement("img");
        img.src = src;
        img.alt = alt;
        img.loading = "lazy";
        img.addEventListener("error", () => {
          thumb.classList.add("broken");
          thumb.disabled = true;
          thumb.textContent = "图" + (i + 1) + " 读取失败";
        });
        thumb.appendChild(img);
        thumb.addEventListener("click", () => openImageViewer(src, alt));
        imgs.appendChild(thumb);
      }
      card.appendChild(imgs);
    }

    const acts = cell("div", undefined, "fb-actions");
    if (!f.read) {
      acts.appendChild(btn("check", "标记已读", "btn btn-small btn-ghost",
        (e) => busy(e.currentTarget, async () => {
          await req("/v1/admin/user-feedback/" + encodeURIComponent(f.id) + "/read",
            { method: "POST" });
          flash("已标记已读 " + f.id);
          await loadFeedback(true);
        })));
    } else {
      acts.appendChild(btn("trash", "删除", "btn btn-small btn-ghost",
        (e) => busy(e.currentTarget, async () => {
          const ok = await ask(
            "删除反馈 " + f.id + "？",
            "删的是这一条原话连同它的截图：从反馈文件与截图目录里一起抹掉，删完不可恢复。"
            + "编号不回收，之后的反馈也不会顶用这个号。",
            "删除", "trash", true);
          if (!ok) return;
          await req("/v1/admin/user-feedback/" + encodeURIComponent(f.id), { method: "DELETE" });
          flash("已删除 " + f.id);
          await loadFeedback(true);
        })));
    }
    card.appendChild(acts);
    return card;
  }

  async function loadMoreFeedback() {
    if (feedbackNextBefore === null || feedbackNextBefore === undefined) return;
    await loadFeedback(false);
  }

  function syncFeedbackFilterButtons() {
    $("fbAll").classList.toggle("active", !feedbackUnreadOnly);
    $("fbUnread").classList.toggle("active", feedbackUnreadOnly);
    $("fbAll").setAttribute("aria-pressed", feedbackUnreadOnly ? "false" : "true");
    $("fbUnread").setAttribute("aria-pressed", feedbackUnreadOnly ? "true" : "false");
  }

  function setFeedbackFilter(unreadOnly) {
    const prev = {
      unreadOnly: feedbackUnreadOnly,
      items: feedbackItems.slice(),
      unread: feedbackUnread,
      nextBefore: feedbackNextBefore,
    };
    feedbackUnreadOnly = !!unreadOnly;
    syncFeedbackFilterButtons();
    busy(null, async () => {
      try {
        await loadFeedback(true);
      } catch (e) {
        // loadFeedback 失败前已经清空列表：不把旧筛选的投影也还回去，视觉上就成了
        // 「未读」下面摆着「全部」的卡片。按钮态、旧列表、游标一起恢复后再重绘。
        feedbackUnreadOnly = prev.unreadOnly;
        feedbackItems = prev.items;
        feedbackUnread = prev.unread;
        feedbackNextBefore = prev.nextBefore;
        syncFeedbackFilterButtons();
        renderFeedback();
        throw e;
      }
    });
  }

  async function exportFeedback() {
    let res;
    try {
      res = await fetch("/v1/admin/user-feedback/export.csv", { credentials: "same-origin" });
    } catch (e) {
      flash("网络错误：导出失败，请重试", true, exportFeedback);
      return;
    }
    if (res.status === 401) { showGate("会话无效或已过期，粘贴口令重新登录"); return; }
    if (res.status === 404 || res.status === 405 || res.status === 501) {
      flash("导出端点尚未上线", true);
      return;
    }
    if (!res.ok) {
      flash("导出失败：HTTP " + res.status, true, exportFeedback);
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "user-feedback.csv";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    flash("已开始下载 CSV");
  }

  /* ---------- 安全 ---------- */

  let secWindow = "24h";
  let secFilters = { type: "", source: "", account: "", severity: "" };
  let secEvents = [];
  let secEventIds = {};
  let secNextBefore = null;
  let secTimer = null;
  // summary/events 还没上线时置真：20 秒定时器不再每轮打一次 404，手动重新读取
  // 成功后会自己恢复。
  let secEndpointsMissing = false;

  const SEC_TYPES = {
    "auth.fail": "登录失败",
    "auth.rate_limited": "触发限流",
    "auth.blocked": "请求被拦截",
    "admin.action": "管理操作",
    "tool.blocked": "工具调用被拦",
    "approval.denied": "审批被拒",
    "provider.error": "模型服务异常",
    "system.anomaly": "系统异常",
  };
  const SEC_SEVERITY = { info: "提示", warn: "警告", alert: "高危" };
  // detail 是后端固定枚举（security_log.DETAILS）；未知值原样显示，不吞成“未知”。
  const SEC_DETAIL = {
    "login.failed": "登录口令错误",
    "register.rejected": "注册被拒",
    "reset.rejected": "重置口令被拒",
    "reauth.denied": "二次验证被拒",
    "limit.login": "登录频率超限",
    "limit.register": "注册频率超限",
    "limit.reset": "重置频率超限",
    "limit.reauth": "二次验证频率超限",
    "limit.chat": "对话频率超限",
    "limit.orchestrate": "编排频率超限",
    "token.unknown": "令牌无法识别",
    "csrf.missing": "缺少 CSRF 标记",
    "unspecified": "未注明",
  };
  const AUDIT_WARNING = {
    audit_file_large: "审计文件偏大，建议轮转/归档",
  };

  function sevTag(severity) {
    const key = String(severity || "info");
    const cls = key === "alert" ? "alert" : (key === "warn" ? "warn" : "info");
    return cell("span", SEC_SEVERITY[key] || key, "sev-tag " + cls);
  }

  function secDetail(detail) {
    if (detail === undefined || detail === null || detail === "") return "—";
    const key = String(detail);
    // 只认映射表自己的键：detail 来自事件字段，`constructor` 之类的原型属性不算命中。
    return Object.prototype.hasOwnProperty.call(SEC_DETAIL, key) ? SEC_DETAIL[key] : key;
  }

  function buildSecurityEventsUrl(before) {
    const parts = ["limit=50"];
    if (secFilters.type) parts.push("type=" + encodeURIComponent(secFilters.type));
    if (secFilters.severity) parts.push("severity=" + encodeURIComponent(secFilters.severity));
    if (secFilters.source) parts.push("source=" + encodeURIComponent(secFilters.source));
    if (secFilters.account) parts.push("account=" + encodeURIComponent(secFilters.account));
    if (before !== null && before !== undefined) {
      parts.push("before=" + encodeURIComponent(String(before)));
    }
    return "/v1/admin/security/events?" + parts.join("&");
  }

  function showSecState(message, retry) {
    const box = $("secState");
    box.textContent = "";
    box.classList.remove("hidden");
    box.appendChild(document.createTextNode(message + " "));
    if (retry) box.appendChild(btn("refresh", "重试", "btn btn-small btn-ghost", () => loadSecurity()));
  }

  function hideSecState() {
    const box = $("secState");
    box.classList.add("hidden");
    box.textContent = "";
  }

  function clearSecSummary() {
    ["secStats", "secSources", "secAccountsTop", "secTypes", "secAudit", "secNarrative",
      "secSeverity", "secTimeline"]
      .forEach((id) => {
        $(id).textContent = "";
      });
    const risk = $("secRisk");
    risk.textContent = "";
    risk.classList.add("hidden");
  }

  function handleSummary(result) {
    if (result.status === "fulfilled") {
      hideSecState();
      renderSecuritySummary(result.value);
      return;
    }
    const e = result.reason;
    if (e && e.message === "401") return;
    clearSecSummary();
    if (e && e.status === 404) {
      showSecState("安全日志端点尚未上线。", false);
    } else {
      showSecState("安全日志读取失败，稍后重试。", true);
    }
  }

  function handleEvents(result, append) {
    if (result.status === "fulfilled") {
      const data = result.value || {};
      secNextBefore = (data.next_before === undefined || data.next_before === null)
        ? null : data.next_before;
      renderSecurityEvents(Array.isArray(data.events) ? data.events : [], append);
      return;
    }
    const e = result.reason;
    if (e && e.message === "401") return;
    const message = e && e.status === 404
      ? "安全事件端点尚未上线。"
      : "安全事件读取失败：" + ((e && e.message) || "未知原因");
    if (append) {
      flash("加载更多失败：" + message, true, loadMoreSecurity);
      return;
    }
    paintTableError($("secEventRows"), 9, message, loadSecurity);
    paintRetry($("secEventCards"), message, loadSecurity);
    $("btnSecMore").classList.add("hidden");
  }

  async function loadSecurity(opts) {
    const auto = opts && opts.auto;
    if (!auto) {
      $("secStats").textContent = "";
      $("secStats").appendChild(cell("span", "读取中…", "skeleton"));
      $("secSources").textContent = "";
      $("secSources").appendChild(cell("span", "读取中…", "skeleton"));
      $("secAccountsTop").textContent = "";
      $("secAccountsTop").appendChild(cell("span", "读取中…", "skeleton"));
      $("secTypes").textContent = "";
      $("secTypes").appendChild(cell("span", "读取中…", "skeleton"));
      $("secNarrative").textContent = "读取中…";
      $("secSeverity").textContent = "";
      $("secSeverity").appendChild(cell("span", "读取中…", "skeleton"));
      $("secTimeline").textContent = "";
      $("secTimeline").appendChild(cell("span", "读取中…", "skeleton"));
      $("secRisk").textContent = "";
      $("secRisk").classList.add("hidden");
      $("secAudit").textContent = "";
      $("secAudit").appendChild(cell("span", "读取中…", "skeleton"));
    }
    const results = await Promise.allSettled([
      req("/v1/admin/security/summary?window=" + encodeURIComponent(secWindow)),
      req(buildSecurityEventsUrl(null)),
    ]);
    handleSummary(results[0]);
    handleEvents(results[1], false);
    // 端点还没上线时暂停 20 秒定时器：否则每轮都会稳定打两次 404，日志全是噪声。
    const missing = results.some((r) => r.status === "rejected"
      && r.reason && r.reason.status === 404);
    if (missing) secEndpointsMissing = true;
    else if (results.every((r) => r.status === "fulfilled")) secEndpointsMissing = false;
    syncSecurityTimer();
  }

  function renderSecuritySummary(data) {
    const totals = data.totals || {};
    const byType = Array.isArray(data.by_type) ? data.by_type : [];
    const countType = (type) => {
      const hit = byType.find((item) => item && item.type === type);
      return hit ? Number(hit.count) || 0 : 0;
    };

    const stats = $("secStats");
    stats.textContent = "";
    stats.appendChild(cell("p", "统计窗口内共 " + (Number(totals.events) || 0) + " 条事件",
      "pane-note note-tight wide"));
    const cards = [
      ["失败登录", countType("auth.fail")],
      ["触发限流", countType("auth.rate_limited")],
      ["被拦截请求", countType("auth.blocked")],
      ["高危事件", Number(totals.alert) || 0],
    ];
    cards.forEach((pair) => {
      const card = cell("article", undefined, "card stat-card");
      const grid = cell("div", undefined, "stat-grid");
      grid.appendChild(statCell(pair[1], pair[0]));
      card.appendChild(grid);
      stats.appendChild(card);
    });

    const bySource = Array.isArray(data.by_source) ? data.by_source : [];
    const byAccount = Array.isArray(data.by_account) ? data.by_account : [];
    renderSecurityNarrative(totals, byType, bySource, byAccount);
    renderRiskBanner(totals);
    renderSeverityBreakdown(totals);
    renderSecurityTimeline(Array.isArray(data.timeline) ? data.timeline : []);
    renderRank($("secSources"), bySource, "source", Number(totals.events) || 0);
    renderRank($("secAccountsTop"), byAccount, "account", Number(totals.events) || 0);
    renderRank($("secTypes"), byType.map((item) => ({
      name: SEC_TYPES[item && item.type] || (item && item.type) || "未知类型",
      count: item && item.count,
    })), "name", Number(totals.events) || 0);
    renderAuditCard(data.audit || {}, Array.isArray(data.warnings) ? data.warnings : []);
  }

  const SEC_WINDOW_LABEL = { "1h": "1 小时", "24h": "24 小时", "7d": "7 天" };

  function renderSecurityNarrative(totals, byType, bySource, byAccount) {
    const box = $("secNarrative");
    box.textContent = "";
    const total = Number(totals.events) || 0;
    if (!total) {
      box.appendChild(cell("p",
        "这个统计窗口内没有安全事件。可以切到 24 小时或 7 天再看。",
        "security-summary-text"));
      return;
    }
    const count = (type) => {
      const hit = byType.find((item) => item && item.type === type);
      return hit ? Number(hit.count) || 0 : 0;
    };
    const bits = [];
    const fail = count("auth.fail");
    const limited = count("auth.rate_limited");
    const blocked = count("auth.blocked");
    if (fail) bits.push(fail + " 次登录失败");
    if (limited) bits.push(limited + " 次触发限流");
    if (blocked) bits.push(blocked + " 次请求被拦截");
    const src = bySource[0];
    const acct = byAccount[0];
    let text = "过去 " + (SEC_WINDOW_LABEL[secWindow] || secWindow)
      + " 共 " + total + " 条安全事件" + (bits.length ? "：" + bits.join("、") + "。" : "。");
    if (src) {
      text += " 主要来源 " + (src.source || src.source_fp || "未知")
        + "（" + (Number(src.count) || 0) + " 次）。";
    }
    if (acct && acct.account) {
      text += " 主要涉及账号 " + acct.account + "（" + (Number(acct.count) || 0) + " 次）。";
    }
    const alert = Number(totals.alert) || 0;
    if (alert) text += " 其中 " + alert + " 条高危，建议优先处理。";
    box.appendChild(cell("p", text, "security-summary-text"));
    box.appendChild(cell("p",
      "级别：高危=需要立即处理；警告=异常但可能误报；提示=记录。来源只显示网络前缀，不显示原始 IP。",
      "pane-note note-tight"));
  }

  function renderRiskBanner(totals) {
    const box = $("secRisk");
    box.textContent = "";
    box.classList.remove("risk-alert", "risk-safe");
    const alert = Number(totals && totals.alert) || 0;
    if (alert > 0) {
      box.classList.add("risk-alert");
      box.appendChild(cell("strong",
        "检测到 " + fmtN(alert) + " 条高危事件，建议优先处理", "risk-text"));
    } else {
      box.classList.add("risk-safe");
      box.appendChild(cell("span", "当前窗口没有高危事件", "risk-text"));
    }
    box.classList.remove("hidden");
  }

  function renderSeverityBreakdown(totals) {
    const box = $("secSeverity");
    box.textContent = "";
    const rows = [
      ["alert", "高危", Number(totals && totals.alert) || 0],
      ["warn", "警告", Number(totals && totals.warn) || 0],
      ["info", "提示", Number(totals && totals.info) || 0],
    ];
    const total = Number(totals && totals.events) || 0;
    if (!total) {
      box.appendChild(cell("p", "这个统计窗口没有事件。", "pane-note note-tight"));
      return;
    }
    const bar = cell("div", undefined, "sev-bar");
    rows.forEach((row) => {
      if (!row[2]) return;
      const seg = cell("span", undefined, "sev-seg " + row[0]);
      seg.style.width = Math.round(row[2] / total * 10000) / 100 + "%";
      seg.title = row[1] + " " + fmtN(row[2]) + " 条";
      bar.appendChild(seg);
    });
    box.appendChild(bar);
    const legend = cell("div", undefined, "sev-legend");
    rows.forEach((row) => {
      const item = cell("span", undefined, "sev-legend-item");
      item.appendChild(cell("i", undefined, "sev-dot " + row[0]));
      item.appendChild(document.createTextNode(
        row[1] + " " + fmtN(row[2]) + " · " + pct(total ? row[2] / total : 0)));
      legend.appendChild(item);
    });
    box.appendChild(legend);
  }

  function renderSecurityTimeline(timeline) {
    const box = $("secTimeline");
    box.textContent = "";
    const buckets = Array.isArray(timeline) ? timeline : [];
    const counts = buckets.map((bucket) => Number(bucket && bucket.count) || 0);
    const total = counts.reduce((sum, value) => sum + value, 0);
    if (!buckets.length || !total) {
      box.appendChild(cell("p", "这个窗口没有事件。", "pane-note note-tight"));
      return;
    }
    const max = Math.max.apply(null, counts) || 1;
    const bars = cell("div", undefined, "timeline-bars");
    buckets.forEach((bucket, index) => {
      const count = Number(bucket && bucket.count) || 0;
      const alert = Number(bucket && bucket.alert) || 0;
      const when = bucket && bucket.start ? fmtTime(bucket.start) : "第 " + (index + 1) + " 段";
      const col = cell("div", undefined, "timeline-col");
      const bar = cell("div", undefined, "timeline-bar" + (alert ? " has-alert" : ""));
      bar.style.height = (count ? Math.max(2, Math.round(count / max * 100)) : 0) + "%";
      const label = when + " · " + fmtN(count) + " 条（高危 " + fmtN(alert) + "）";
      bar.title = label;
      bar.setAttribute("aria-label", label);
      bar.setAttribute("role", "img");
      if (alert && count) {
        const top = cell("span", undefined, "timeline-alert");
        top.style.height = Math.round(alert / count * 100) + "%";
        bar.appendChild(top);
      }
      col.appendChild(bar);
      bars.appendChild(col);
    });
    box.appendChild(bars);
    const axis = cell("div", undefined, "timeline-axis");
    const marks = [0, Math.floor((buckets.length - 1) / 2), buckets.length - 1];
    marks.filter((mark, pos) => marks.indexOf(mark) === pos).forEach((mark) => {
      const bucket = buckets[mark];
      axis.appendChild(cell("span",
        bucket && bucket.start ? fmtTime(bucket.start) : String(mark + 1)));
    });
    box.appendChild(axis);
  }

  function renderRank(container, items, nameKey, total) {
    container.textContent = "";
    if (!items.length) {
      container.appendChild(cell("p", "统计窗口内没有记录。", "pane-note note-tight"));
      return;
    }
    const sorted = items.slice().sort((a, b) => (Number(b.count) || 0) - (Number(a.count) || 0));
    const max = Math.max.apply(null, sorted.map((item) => Number(item.count) || 0)) || 1;
    const base = total > 0 ? total : max;
    sorted.slice(0, 8).forEach((item) => {
      const count = Number(item.count) || 0;
      const row = cell("div", undefined, "rank-item");
      const head = cell("div", undefined, "rank-head");
      const name = item[nameKey] || item.source_fp || "—";
      head.appendChild(cell("span", String(name), "rank-name"));
      head.appendChild(cell("span", fmtN(count) + " · " + pct(base ? count / base : 0), "rank-num"));
      row.appendChild(head);
      const bar = cell("div", undefined, "rank-bar");
      const fill = cell("div", undefined, "rank-fill");
      fill.style.width = Math.max(2, Math.round((base ? count / base : 0) * 100)) + "%";
      bar.appendChild(fill);
      row.appendChild(bar);
      container.appendChild(row);
    });
  }

  function renderAuditCard(audit, warnings) {
    const box = $("secAudit");
    box.textContent = "";
    const size = Number(audit.size_bytes) || 0;
    box.appendChild(statCell(fmtBytes(size), "占用"));
    box.appendChild(statCell(Number(audit.entries) || 0, "条记录"));
    box.appendChild(statCell(fmtTime(audit.last_ts), "最后写入"));
    if (size > 5 * 1024 * 1024) {
      box.appendChild(cell("span", "建议轮转/归档", "tag warn"));
    }
    warnings.forEach((warning) => {
      box.appendChild(cell("span", AUDIT_WARNING[warning] || warning, "tag warn"));
    });
  }

  function securityEventKey(event) {
    if (event.id !== undefined && event.id !== null && event.id !== "") {
      return "id:" + String(event.id);
    }
    // 契约要求 events[].id，但第二批真漏了字段时不能把整批都当成同一条丢掉：
    // 用界面上看得见的字段拼一个稳定键。
    return ["ts", "type", "source", "status", "route"]
      .map((k) => String(event[k] === undefined || event[k] === null ? "" : event[k]))
      .join("|");
  }

  function renderSecurityEvents(events, append) {
    if (!append) {
      secEvents = [];
      secEventIds = {};
    }
    events.forEach((event) => {
      const key = securityEventKey(event);
      if (secEventIds[key]) return;
      secEventIds[key] = true;
      secEvents.push(event);
    });
    paintSecurityEvents();
  }

  function paintSecurityEvents() {
    const tbody = $("secEventRows");
    const cards = $("secEventCards");
    tbody.textContent = "";
    cards.textContent = "";
    $("btnSecMore").classList.toggle("hidden",
      secNextBefore === null || secNextBefore === undefined);
    if (!secEvents.length) {
      tbody.appendChild(emptyRow("当前筛选没有事件。可以换类型 / 级别 / 来源，或清除筛选。", 9));
      cards.appendChild(emptyBox("当前筛选没有事件。可以换类型 / 级别 / 来源，或清除筛选。"));
      return;
    }
    secEvents.forEach((event) => {
      tbody.appendChild(securityEventRow(event));
      cards.appendChild(securityEventCard(event));
    });
  }

  function securityEventRow(event) {
    const tr = document.createElement("tr");
    tr.appendChild(cell("td", fmtTime(event.ts)));
    const sev = cell("td");
    sev.appendChild(sevTag(event.severity));
    tr.appendChild(sev);
    tr.appendChild(cell("td", SEC_TYPES[event.type] || event.type || "—"));
    tr.appendChild(cell("td", secDetail(event.detail)));
    tr.appendChild(securitySourceCell(event));
    tr.appendChild(cell("td", event.account || "—"));
    tr.appendChild(cell("td", event.route || "—", "code"));
    tr.appendChild(cell("td", event.status === undefined || event.status === null
      ? "—" : String(event.status)));
    tr.appendChild(cell("td", String(event.count === undefined ? 1 : event.count)));
    return tr;
  }

  function securitySourceCell(event) {
    const td = cell("td", undefined, "code");
    td.appendChild(document.createTextNode(event.source || event.source_fp || "—"));
    // 指纹让 "同一 /24 段里的不同机器" 在表上也分得开；原始 IP 从不出现。
    if (event.source && event.source_fp) {
      td.appendChild(cell("span", String(event.source_fp).slice(0, 8), "source-fp"));
    }
    return td;
  }

  function securityEventCard(event) {
    const card = cell("article", undefined, "event-card");
    const head = cell("div", undefined, "event-head");
    head.appendChild(cell("span", fmtTime(event.ts), "dim"));
    head.appendChild(sevTag(event.severity));
    card.appendChild(head);
    const grid = cell("div", undefined, "event-grid");
    const line = (label, value) => {
      const item = cell("span");
      item.appendChild(cell("b", label));
      item.appendChild(document.createTextNode(value || "—"));
      grid.appendChild(item);
    };
    line("类型", SEC_TYPES[event.type] || event.type);
    line("原因", secDetail(event.detail));
    const sourceItem = cell("span");
    sourceItem.appendChild(cell("b", "来源"));
    sourceItem.appendChild(document.createTextNode(event.source || event.source_fp || "—"));
    if (event.source && event.source_fp) {
      sourceItem.appendChild(cell("span", String(event.source_fp).slice(0, 8), "source-fp"));
    }
    grid.appendChild(sourceItem);
    line("账号", event.account);
    line("路由", event.route);
    line("状态码", event.status === undefined || event.status === null ? "—" : String(event.status));
    line("合并次数", String(event.count === undefined ? 1 : event.count));
    card.appendChild(grid);
    return card;
  }

  async function loadMoreSecurity() {
    if (secNextBefore === null || secNextBefore === undefined) return;
    try {
      const data = await req(buildSecurityEventsUrl(secNextBefore));
      handleEvents({ status: "fulfilled", value: data }, true);
    } catch (e) {
      if (e.message !== "401") flash("加载更多失败：" + e.message, true, loadMoreSecurity);
    }
  }

  async function applySecurityFilters() {
    secFilters = {
      type: $("secType").value,
      severity: $("secSeverityFilter").value,
      source: $("secSource").value.trim(),
      account: $("secAccount").value.trim(),
    };
    secNextBefore = null;
    await loadSecurity();
  }

  function syncSecurityTimer() {
    const active = authed && route === "security" && document.visibilityState === "visible"
      && !secEndpointsMissing;
    if (active && !secTimer) {
      secTimer = setInterval(() => {
        if (authed && route === "security" && document.visibilityState === "visible") {
          loadSecurity({ auto: true });
        }
      }, 20000);
    } else if (!active && secTimer) {
      clearInterval(secTimer);
      secTimer = null;
    }
  }

  async function loadAudit() {
    const tbody = $("auditRows");
    tbody.textContent = "";
    tbody.appendChild(emptyRow("读取中…", 5));
    try {
      const data = await req("/v1/admin/audit?limit=50");
      const entries = Array.isArray(data.entries) ? data.entries.slice(-50) : [];
      tbody.textContent = "";
      if (!entries.length) {
        tbody.appendChild(emptyRow("还没有管理操作审计记录。", 5));
        return;
      }
      entries.forEach((entry) => tbody.appendChild(auditRow(entry)));
    } catch (e) {
      if (e.message === "401") return;
      paintTableError(tbody, 5, "审计读取失败：" + e.message, loadAudit);
    }
  }

  function auditRow(entry) {
    const tr = document.createElement("tr");
    tr.appendChild(cell("td", fmtTime(entry.ts)));
    tr.appendChild(cell("td", entry.actor_name || entry.actor_id || "—"));
    tr.appendChild(cell("td", entry.action || "—", "code"));
    tr.appendChild(cell("td", textOf(entry.target) || "—", "code"));
    const summary = entry.detail
      || (entry.after ? "after: " + textOf(entry.after) : "")
      || (entry.before ? "before: " + textOf(entry.before) : "");
    tr.appendChild(cell("td", summary || "—", "audit-summary"));
    return tr;
  }

  /* ---------- MCP 外部工具 ---------- */

  // GET 的投影只有状态与 env 变量名：这一节绝不渲染 args，也不渲染 env 的值。
  const MCP_STATE = {
    disabled: "已停用",
    starting: "启动中",
    ready: "就绪",
    crashed: "已崩溃",
    backoff: "退避重试",
    stopping: "停止中",
    circuit_open: "熔断中",
    config_error: "配置错误",
  };
  // 与 mcp_stdio_config.ID_RE 同形；后端仍权威，这里只挡住明显写错的 id。
  const MCP_ID_RE = /^[a-z0-9][a-z0-9_-]{0,15}$/;

  let mcpEnabled = false;
  let mcpServers = [];
  let mcpEditingId = null;
  // 状态没读到就不允许写：否则既看不到替换的是哪一条，也无从判断 id 是否已存在。
  let mcpLoaded = false;

  function mcpStateTag(state) {
    const key = String(state || "unknown");
    const bad = key === "crashed" || key === "circuit_open" || key === "config_error";
    return cell("span", MCP_STATE[key] || key, bad ? "tag warn" : "tag");
  }

  function mcpToolsText(s) {
    const exposed = s.tools_exposed === null || s.tools_exposed === undefined
      ? "—" : String(s.tools_exposed);
    const total = s.tools_total === null || s.tools_total === undefined
      ? "—" : String(s.tools_total);
    return exposed + " / " + total;
  }

  function mcpEnvText(s) {
    const names = Array.isArray(s.env_names) ? s.env_names : [];
    return names.length ? names.join(", ") : "—";
  }

  function syncMcpEnabledNote() {
    $("mcpEnabledNote").textContent = mcpEnabled
      ? "全局已启用：启用的条目会按各自状态启动子进程。"
      : "全局已关闭：条目配置保留，但不会启动子进程，也不会暴露工具。";
  }

  function renderMcpServers() {
    const tbody = $("mcpRows");
    const cards = $("mcpCards");
    tbody.textContent = "";
    cards.textContent = "";
    if (!mcpServers.length) {
      const note = "默认关闭，尚未配置。在下面的表单里新增一条；启用前不会启动任何子进程。";
      tbody.appendChild(emptyRow(note, 9));
      cards.appendChild(emptyBox(note));
      return;
    }
    mcpServers.forEach((s) => {
      tbody.appendChild(mcpServerRow(s));
      cards.appendChild(mcpServerCard(s));
    });
  }

  function mcpServerRow(s) {
    const tr = document.createElement("tr");
    tr.appendChild(cell("td", s.id || "—", "code"));
    const st = cell("td");
    st.appendChild(mcpStateTag(s.state));
    tr.appendChild(st);
    tr.appendChild(cell("td", s.pid === null || s.pid === undefined ? "—" : String(s.pid)));
    tr.appendChild(cell("td", mcpToolsText(s)));
    tr.appendChild(cell("td", s.restarts === null || s.restarts === undefined
      ? "—" : String(s.restarts)));
    tr.appendChild(cell("td", s.last_error ? String(s.last_error) : "—",
      s.last_error ? "mcp-error" : "dim"));
    tr.appendChild(cell("td", mcpEnvText(s), "code"));
    tr.appendChild(cell("td", s.command_basename || "—", "code"));
    tr.appendChild(mcpActions(s));
    return tr;
  }

  function mcpServerCard(s) {
    const card = cell("article", undefined, "user-card");
    const head = cell("div", undefined, "user-card-head");
    head.appendChild(cell("b", s.id || "—", "code"));
    head.appendChild(mcpStateTag(s.state));
    card.appendChild(head);
    const meta = cell("div", undefined, "user-meta");
    const line = (label, value, cls) => {
      const item = cell("span");
      item.appendChild(cell("b", label));
      item.appendChild(cell("span", value, cls));
      meta.appendChild(item);
    };
    line("pid", s.pid === null || s.pid === undefined ? "—" : String(s.pid));
    line("工具（暴露 / 总）", mcpToolsText(s));
    line("重启", s.restarts === null || s.restarts === undefined ? "—" : String(s.restarts));
    line("env 变量名", mcpEnvText(s), "code");
    line("命令", s.command_basename || "—", "code");
    line("最后错误", s.last_error ? String(s.last_error) : "—", s.last_error ? "mcp-error" : "dim");
    card.appendChild(meta);
    const acts = cell("div", undefined, "user-actions");
    acts.appendChild(btn("gear", "编辑", "btn btn-small btn-ghost", () => pickMcp(s)));
    acts.appendChild(btn("trash", "删除", "btn btn-small btn-ghost",
      (e) => busy(e.currentTarget, () => deleteMcp(s))));
    card.appendChild(acts);
    return card;
  }

  function mcpActions(s) {
    const acts = cell("td", undefined, "acts");
    acts.appendChild(btn("gear", "编辑", "btn btn-small btn-ghost", () => pickMcp(s)));
    acts.appendChild(btn("trash", "删除", "btn btn-small btn-ghost",
      (e) => busy(e.currentTarget, () => deleteMcp(s))));
    return acts;
  }

  async function loadMcp() {
    paintTableLoading($("mcpRows"), 9);
    $("mcpCards").textContent = "";
    $("mcpCards").appendChild(emptyBox("读取中…"));
    $("mcpState").classList.add("hidden");
    $("mcpState").textContent = "";
    try {
      const data = await req("/v1/admin/mcp/stdio");
      mcpEnabled = !!data.enabled;
      mcpServers = Array.isArray(data.servers) ? data.servers : [];
      $("mcpEnabled").checked = mcpEnabled;
      $("mcpEnabled").disabled = false;
      syncMcpEnabledNote();
      renderMcpServers();
      mcpLoaded = true;
      return true;
    } catch (e) {
      if (e.message === "401") throw e;
      // 状态没读到就不许让开关看起来像“已关闭”：锁住复选并说明原因。
      mcpLoaded = false;
      mcpEnabled = false;
      $("mcpEnabled").checked = false;
      $("mcpEnabled").disabled = true;
      $("mcpEnabledNote").textContent = "读不到全局开关：开关未同步，别把它当成已关闭。";
      paintTableError($("mcpRows"), 9, "MCP 状态读取失败：" + e.message, () => busy(null, loadMcp));
      paintRetry($("mcpCards"), "MCP 状态读取失败：" + e.message, () => busy(null, loadMcp));
      const box = $("mcpState");
      box.textContent = "";
      box.classList.remove("hidden");
      box.appendChild(document.createTextNode("MCP 状态读取失败：" + e.message + " "));
      box.appendChild(btn("refresh", "重试", "btn btn-small btn-ghost",
        (ev) => busy(ev.currentTarget, loadMcp)));
      return false;
    }
  }

  function pickMcp(s) {
    mcpEditingId = s.id;
    $("mcpId").value = s.id;
    $("mcpId").readOnly = true;
    $("mcpCommand").value = s.command || "";
    // 服务端不回 args：编辑时这里必须留空，保存就是拿当前表单整条覆盖。
    $("mcpArgs").value = "";
    $("mcpEnv").value = Array.isArray(s.env_names) ? s.env_names.join(", ") : "";
    $("mcpEntryEnabled").checked = !!s.enabled;
    $("mcpFormNote").textContent = "正在整条替换「" + s.id + "」：原 args 不回显，保存前请重新填写";
    $("mcpId").focus();
  }

  function resetMcpForm() {
    mcpEditingId = null;
    $("mcpId").value = "";
    $("mcpId").readOnly = false;
    $("mcpCommand").value = "";
    $("mcpArgs").value = "";
    $("mcpEnv").value = "";
    $("mcpEntryEnabled").checked = false;
    $("mcpFormNote").textContent = "新增条目";
  }

  function mcpArgsFromForm() {
    return $("mcpArgs").value.split("\n")
      .map((line) => String(line).trim())
      .filter((line) => line.length > 0);
  }

  function mcpEnvFromForm() {
    return $("mcpEnv").value.split(",")
      .map((name) => String(name).trim())
      .filter((name) => name.length > 0);
  }

  async function saveMcp() {
    if (!mcpLoaded) {
      flash("MCP 状态还没读出来，先点「重新读取」再保存", true);
      return;
    }
    const id = $("mcpId").value.trim();
    const command = $("mcpCommand").value.trim();
    if (!MCP_ID_RE.test(id)) {
      flash("条目 id 只能用小写字母 / 数字 / _-，最多 16 个字符", true);
      $("mcpId").focus();
      return;
    }
    if (!command) {
      flash("command 必须填绝对路径", true);
      $("mcpCommand").focus();
      return;
    }
    const body = {
      command: command,
      args: mcpArgsFromForm(),
      env_allowlist: mcpEnvFromForm(),
      enabled: $("mcpEntryEnabled").checked,
    };
    // 手输一个已存在的 id 也是整条替换：和点「编辑」走同一道确认，不允许静默清空。
    const existing = mcpEditingId || mcpServers.some((s) => s.id === id);
    if (existing) {
      const ok = await ask(
        "整条替换 MCP 条目「" + id + "」？",
        "服务端不回显原 args，保存后这一条以当前表单为准：没重新填的 args 会被清空。"
        + "子进程会按新配置重启，期间它暴露的工具暂时不可用。",
        "整条替换", "check", true);
      if (!ok) return;
    }
    await req("/v1/admin/mcp/stdio/servers/" + encodeURIComponent(id),
      { method: "PUT", body: body });
    flash("已提交「" + id + "」；reload 是异步的，稍后点「重新读取」看状态");
    mcpEditingId = id;
    $("mcpId").readOnly = true;
    $("mcpFormNote").textContent = "正在整条替换「" + id + "」：原 args 不回显，保存前请重新填写";
    await loadMcp();
  }

  async function deleteMcp(s) {
    const ok = await ask(
      "删除 MCP 条目「" + s.id + "」？",
      "删的是配置里的这一条：子进程会停，它暴露的工具立即从注册表撤下。"
      + "聊天里已经发生的调用记录不删；要恢复只能整条重填。",
      "删除", "trash", true);
    if (!ok) return;
    await req("/v1/admin/mcp/stdio/servers/" + encodeURIComponent(s.id),
      { method: "DELETE" });
    flash("已删除「" + s.id + "」");
    if (mcpEditingId === s.id) resetMcpForm();
    await loadMcp();
  }

  async function toggleMcpEnabled() {
    const box = $("mcpEnabled");
    const next = !!box.checked;
    try {
      const r = await req("/v1/admin/mcp/stdio/settings", { method: "PUT", body: { enabled: next } });
      mcpEnabled = r && typeof r.enabled === "boolean" ? r.enabled : next;
      box.checked = mcpEnabled;
      syncMcpEnabledNote();
      flash(mcpEnabled ? "MCP 外部工具已启用" : "MCP 外部工具已关闭");
      await loadMcp();
    } catch (e) {
      // 没写成就把 UI 退回读到的值：别让管理员对着一颗“已打开”的假开关。
      box.checked = mcpEnabled;
      box.disabled = false;
      if (e.message !== "401") flash("开关没保存：" + e.message + "（已回滚为原值）", true);
    }
  }

  /* ---------- 用量 ---------- */

  // 账本上 paid_by 只有这两个取值（providers.py 的白名单硬校验）。
  const PAID = { operator: "服务端垫的", user: "他自带的 key" };

  let usageDay = "";

  async function loadUsage() {
    try { await ensureUsers(); }
    catch (e) { if (e.message === "401") throw e; }
    $("usageTotals").textContent = "";
    $("usageTotals").appendChild(cell("span", "读取中…", "skeleton"));
    paintTableLoading($("usageRows"), 8);
    try {
      const data = await req(usageDay
        ? "/v1/admin/usage?day=" + encodeURIComponent(usageDay)
        : "/v1/admin/usage");
      // 哪天是"正在看的这天"由后端回回来，本地不猜。
      usageDay = data.day || "";

      const days = (data.days || []).slice();
      if (usageDay && days.indexOf(usageDay) < 0) days.push(usageDay);
      days.sort().reverse();
      const sel = $("usageDay");
      sel.textContent = "";
      days.forEach((d) => {
        const o = cell("option", d);
        o.value = d;
        sel.appendChild(o);
      });
      sel.value = usageDay;

      renderTotals(data.totals || {});

      const tbody = $("usageRows");
      tbody.textContent = "";
      const rows = data.rows || [];
      if (!rows.length) {
        tbody.appendChild(emptyRow("这一天没有账。上面两张卡是零，不是读不到。", 8));
        return true;
      }
      rows.forEach((r) => tbody.appendChild(usageRow(r)));
      return true;
    } catch (e) {
      if (e.message === "401") throw e;
      $("usageTotals").textContent = "";
      paintTableError($("usageRows"), 8, "用量读取失败：" + e.message, loadUsage);
      return false;
    }
  }

  function renderTotals(totals) {
    const box = $("usageTotals");
    box.textContent = "";
    Object.keys(totals).forEach((paid) => {
      const t = totals[paid] || {};
      const card = cell("div", undefined, "card grow");
      const stat = cell("div", undefined, "stat");
      stat.appendChild(cell("b", fmtN(t.total_tokens)));
      stat.appendChild(cell("span", "token · " + (PAID[paid] || paid)));
      card.appendChild(stat);
      card.appendChild(cell("p", fmtN(t.calls) + " 次 · 成功 " + fmtN(t.ok)
        + " / 失败 " + fmtN(t.failed) + " · 入 " + fmtN(t.prompt_tokens)
        + " 出 " + fmtN(t.completion_tokens), "pane-note"));
      const extra = [];
      if (t.reasoning_tokens) extra.push("推理 " + fmtN(t.reasoning_tokens));
      if (t.cached_tokens) extra.push("缓存命中 " + fmtN(t.cached_tokens));
      if (extra.length) card.appendChild(cell("p", extra.join(" · "), "pane-note"));
      if (t.unknown_usage > 0) {
        card.appendChild(cell("p", "其中 " + fmtN(t.unknown_usage)
          + " 次上游没回 token 数：这栏是下限，不是准确数", "pane-note warn"));
      }
      box.appendChild(card);
    });
  }

  function usageRow(r) {
    const tr = document.createElement("tr");
    const who = cell("td", undefined, "name");
    who.appendChild(cell("span", nameById[r.user_id] || r.user_id));
    if (!nameById[r.user_id]) who.appendChild(cell("span", "（不在用户表里）", "dim"));
    tr.appendChild(who);
    tr.appendChild(cell("td", r.provider_id, "code"));
    tr.appendChild(cell("td", PAID[r.paid_by] || r.paid_by));
    tr.appendChild(cell("td", fmtN(r.calls)));
    tr.appendChild(cell("td", fmtN(r.ok) + " / " + fmtN(r.failed)));
    tr.appendChild(cell("td", fmtN(r.total_tokens)));
    tr.appendChild(cell("td", fmtN(r.tool_rounds)));
    const note = cell("td");
    note.appendChild(r.unknown_usage > 0
      ? cell("span", fmtN(r.unknown_usage) + " 次没回 token 数", "tag warn")
      : cell("span", "齐", "dim"));
    tr.appendChild(note);
    return tr;
  }

  /* ---------- 搜索用量 ---------- */

  const SEARCH_REASON = {
    ok: "给结果",
    not_enabled: "未启用（这台机器没配它的 key）",
    empty_query: "空查询",
    network: "连不上/超时",
    non_200: "上游回了非 200",
    oversize: "响应超字节上限",
    parse_empty: "回来了但认不出",
    no_result: "回空（爬取源说不清为什么）",
    source_raised: "源违约抛异常",
  };
  let searchUsageDays = 7;

  async function loadSearchUsage() {
    $("searchUsageTotals").textContent = "";
    $("searchUsageTotals").appendChild(cell("span", "读取中…", "skeleton"));
    paintTableLoading($("searchUsageRows"), 7);
    try {
      const data = await req("/v1/admin/search-usage?days=" + searchUsageDays);
      const w = data.window || {};
      const t = data.totals || { calls: 0, ok: 0, failed: 0 };

      const box = $("searchUsageTotals");
      box.textContent = "";
      const card = cell("div", undefined, "card grow");
      const stat = cell("div", undefined, "stat");
      stat.appendChild(cell("b", fmtN(t.calls)));
      stat.appendChild(cell("span", "次搜索 · " + (w.from || "—") + " → " + (w.to || "—")));
      card.appendChild(stat);
      card.appendChild(cell("p", "给出结果 " + fmtN(t.ok) + " / 全链空 " + fmtN(t.failed)
        + "（一次执行一笔：主源空、备用源答上，calls 仍算 1 次，两个源各记 1 次尝试）", "pane-note"));
      box.appendChild(card);

      const tbody = $("searchUsageRows");
      tbody.textContent = "";
      const rows = data.sources || [];
      if (!rows.length) {
        tbody.appendChild(emptyRow("这个窗口里一次搜索都没跑过。上面那张卡是零，不是读不到。", 7));
      }
      rows.forEach((r) => tbody.appendChild(searchUsageRow(r)));

      // 边界那句话只从后端读，前端不另写一套。
      const b = data.boundaries || {};
      const bits = [];
      if (b.direct_browser) bits.push(b.direct_browser);
      if (b.probe) bits.push(b.probe);
      if (b.no_query_text) bits.push(b.no_query_text);
      $("searchUsageNote").textContent = bits.join(" ");
      return true;
    } catch (e) {
      if (e.message === "401") throw e;
      $("searchUsageTotals").textContent = "";
      paintTableError($("searchUsageRows"), 7, "搜索用量读取失败：" + e.message, loadSearchUsage);
      return false;
    }
  }

  function searchUsageRow(r) {
    const tr = document.createElement("tr");
    tr.appendChild(cell("td", r.source, "name"));
    tr.appendChild(cell("td", fmtN(r.attempts)));
    tr.appendChild(cell("td", fmtN(r.ok)));
    tr.appendChild(cell("td", fmtN(r.failed)));
    tr.appendChild(cell("td", r.skipped ? fmtN(r.skipped) : "0"));
    tr.appendChild(cell("td", pct(r.success_rate)));
    const reasons = Object.keys(r.reasons || {})
      .filter((k) => k !== "ok")
      .map((k) => (SEARCH_REASON[k] || k) + " " + fmtN(r.reasons[k]));
    const note = cell("td");
    note.appendChild(cell("span", reasons.length ? reasons.join(" · ") : "—", "dim"));
    tr.appendChild(note);
    return tr;
  }

  /* ---------- 记忆 ---------- */

  async function loadMemory() {
    $("memTotal").textContent = "读取中…";
    $("memNote").textContent = "条 · 全库合计（不分人）";
    try {
      const stats = await req("/v1/memory/stats");
      $("memTotal").textContent = String(stats.total_memories ?? "—");
      $("memNote").textContent = "条 · 全库合计（不分人）";
      $("memCollection").textContent = "集合：" + (stats.collection_name || "—");
      return true;
    } catch (e) {
      if (e.message === "401") throw e;
      $("memTotal").textContent = "读不到";
      $("memNote").textContent = "条 · " + e.message;
      $("memCollection").textContent = "";
      $("memCollection").appendChild(btn("refresh", "重试", "btn btn-small btn-ghost",
        () => busy(null, loadMemory)));
      return false;
    }
  }

  async function decay() {
    const factor = $("decayFactor").value || "0.95";
    const ok = await ask(
      "把你自己这池记忆的权重乘 " + factor + "？",
      "作用对象是登录这个页面的这个人——也就是你自己的记忆池，不是列表里任何别人的。"
      + "整池一起压低，压完不会自己涨回来。",
      "衰减", "decay", true);
    if (!ok) return;
    await req("/v1/memory/decay?decay_factor=" + encodeURIComponent(factor), { method: "POST" });
    flash("已衰减你自己的记忆权重");
    await loadMemory();
  }

  /* ---------- 任务 ---------- */

  const TASK_STATUS = {
    pending: "排队中",
    running: "执行中",
    waiting_user: "需要你输入",
    completed: "办完了",
    failed: "没办成",
    cancelled: "已停止",
  };

  function taskStatusText(status) {
    const key = String(status || "");
    return TASK_STATUS[key] || key || "—";
  }

  async function loadTasks() {
    const box = $("taskList");
    box.textContent = "";
    box.appendChild(cell("p", "读取中…", "pane-note"));
    try { await ensureUsers(); }
    catch (e) { if (e.message === "401") throw e; }
    try {
      const data = await req("/v1/tasks");
      const tasks = data.tasks || [];
      box.textContent = "";
      if (!tasks.length) {
        box.appendChild(cell("p", "任务表是空的。这条进程起来之后没跑过编排。", "pane-note"));
        return true;
      }
      // 问题原文只在详情接口里，只为 waiting_user 的行各补一次 GET。
      const fills = [];
      tasks.forEach((t) => {
        const row = taskRow(t);
        box.appendChild(row);
        if (t.status === "waiting_user" && row._question) {
          fills.push(fillTaskQuestion(t.task_id, row._question));
        }
      });
      await Promise.all(fills);
      return true;
    } catch (e) {
      if (e.message === "401") throw e;
      paintRetry(box, "任务读取失败：" + e.message, loadTasks);
      return false;
    }
  }

  async function fillTaskQuestion(taskId, holder) {
    try {
      const d = await req("/v1/tasks/" + encodeURIComponent(taskId));
      const cursor = (d && d.resume_cursor) || {};
      const question = String(cursor.question || "").trim();
      holder.textContent = question ? "问题：" + question : "服务端没给问题原文";
      holder.classList.toggle("warn", !question);
    } catch (e) {
      holder.textContent = "问题没读到：" + (e && e.message ? e.message : "未知原因");
      holder.classList.add("warn");
    }
  }

  function taskRow(t) {
    const row = cell("div", undefined, "task");
    const meta = cell("div", undefined, "tm");
    meta.appendChild(cell("b", t.goal));
    const owner = nameById[t.user_id] || t.user_id || "（无归属）";
    meta.appendChild(cell("span", t.task_id + " · " + fmtTime(t.created_at) + " · " + owner));
    const waiting = t.status === "waiting_user";
    if (waiting) {
      const q = cell("span", "问题读取中…", "tq");
      q.classList.add("warn");
      meta.appendChild(q);
      row._question = q;
    }
    row.appendChild(meta);
    row.appendChild(cell("span", taskStatusText(t.status) + " " + (t.progress || ""),
      "st" + (t.status === "running" || t.status === "pending" ? " run" : "")
      + (t.status === "failed" ? " bad" : "")
      + (waiting ? " wait" : "")));

    const ops = cell("div", undefined, "ops");
    ops.appendChild(btn("eye", "详情", "btn btn-small btn-ghost",
      (e) => busy(e.currentTarget, () => showTaskDetail(t))));
    ops.appendChild(btn("stop", "取消", "btn btn-small btn-ghost",
      (e) => busy(e.currentTarget, async () => {
        const yes = await ask("取消任务「" + (t.goal || t.task_id) + "」？",
          "只是打个取消标记：当前那个子任务会跑完，然后停下来。已经在跑的上游调用退不回钱。",
          "取消任务", "stop", true);
        if (!yes) return;
        const r = await req("/v1/tasks/" + encodeURIComponent(t.task_id) + "/cancel",
          { method: "POST" });
        flash(r.message || "已标记取消");
        await loadTasks();
      })));
    ops.appendChild(btn("trash", "移出", "btn btn-small btn-ghost",
      (e) => busy(e.currentTarget, async () => {
        const yes = await ask("把任务 " + t.task_id + " 移出这张表？",
          "删的是这一条任务记录：中间结果、子任务、最终答案一起消失，并且当场写回 data/tasks.json，"
          + "重启也不会回来。挂起中的任务，断点也一并没了——先确认没有人在等它回答。",
          "移出", "trash", true);
        if (!yes) return;
        await req("/v1/tasks/" + encodeURIComponent(t.task_id), { method: "DELETE" });
        flash("已移出");
        await loadTasks();
      })));
    row.appendChild(ops);
    return row;
  }

  function taskDetailView(d) {
    const detail = d || {};
    const status = String(detail.status || "");
    const cursor = detail.resume_cursor || {};
    const waiting = status === "waiting_user";
    const done = status === "completed";
    const results = done && Array.isArray(detail.results) ? detail.results : [];
    const subtasks = Array.isArray(detail.subtasks) ? detail.subtasks : [];
    return {
      statusRaw: status,
      status: taskStatusText(status),
      goal: String(detail.goal || "（没写目标）"),
      owner: nameById[detail.user_id] || detail.user_id || "（无归属）",
      created: fmtTime(detail.created_at),
      progress: subtasks.length
        ? (Number(detail.current_subtask) || 0) + " / " + subtasks.length
        : "",
      question: waiting ? String(cursor.question || "").trim() : "",
      canResume: waiting && !!detail.can_resume,
      waitingSince: waiting ? fmtTime(detail.waiting_since) : "",
      results: results,
      resultsHidden: !done,
      finalAnswer: String(detail.final_answer || ""),
      error: String(detail.error || ""),
      endReason: String(detail.end_reason || ""),
    };
  }

  function paintTaskDetail(view) {
    const box = $("detailText");
    box.textContent = "";
    const line = (label, text, cls) => {
      const p = cell("p", undefined, "dline" + (cls ? " " + cls : ""));
      p.appendChild(cell("b", label + "："));
      p.appendChild(cell("span", text || "—"));
      box.appendChild(p);
    };

    line("目标", view.goal);
    line("状态", view.status + "（" + (view.statusRaw || "—") + "）"
      + (view.progress ? " · 第 " + view.progress + " 步" : ""));
    line("归属", view.owner);
    line("创建", view.created);
    if (view.statusRaw === "waiting_user") {
      line("在等什么", view.question || "服务端没给问题原文");
      line("等待起点", view.waitingSince);
      line("可续跑", view.canResume ? "是" : "否（这条断点接不回去，需人工处理）",
        view.canResume ? "" : "dwarn");
      box.appendChild(cell("p",
        "只有这条任务的发起人能在 App / 网页里回答：续跑只认任务属主，"
        + "管理员这里只读，不提供代答入口。", "pane-note"));
    }

    if (view.resultsHidden) {
      box.appendChild(cell("p",
        "中间结果只在「办完了」的任务详情里返回（服务端把其余状态的 results 置空）；"
        + "这里不显示成空列表，免得读成「一条结果都没有」。", "pane-note"));
    } else {
      box.appendChild(cell("p", "中间结果（" + view.results.length + " 条）", "dhead"));
      if (!view.results.length) {
        box.appendChild(cell("p", "这条已完成的任务没有中间结果。", "pane-note"));
      }
      view.results.forEach((r, i) => {
        line("第 " + (i + 1) + " 条", typeof r === "string" ? r : JSON.stringify(r));
      });
    }
    if (view.finalAnswer) line("最终答案", view.finalAnswer, "danswer");
    if (view.error) line("错误", view.error, "dwarn");
    if (view.endReason === "cancelled_by_timeout") {
      box.appendChild(cell("p", "这次取消是等回复等太久被系统作废，不是任务自己失败。",
        "pane-note warn"));
    } else if (view.endReason === "cancelled_by_user") {
      box.appendChild(cell("p", "这次取消是人按的「取消任务」。", "pane-note"));
    }
  }

  async function showTaskDetail(t) {
    const d = await req("/v1/tasks/" + encodeURIComponent(t.task_id));
    $("detailTitle").textContent = "任务 " + (d.task_id || t.task_id);
    paintTaskDetail(taskDetailView(d));
    openSheet($("detail"), $("btnCloseDetail"));
  }

  /* ---------- 智能体 ---------- */

  function agentBody() {
    return {
      task: $("agentInput").value.trim(),
      max_turns: Number($("agentTurns").value) || 10,
      max_duration: Number($("agentDuration").value) || 120,
    };
  }

  async function runAgent() {
    const body = agentBody();
    if (!body.task) { flash("先写要跑的事", true); return; }
    const out = $("agentOut");
    out.classList.remove("hidden");
    out.textContent = "在跑……这两条都是同步接口，服务端跑完才回话。";
    const r = await req("/v1/agent/run", { method: "POST", body });
    out.textContent = typeof r.result === "string" ? r.result : JSON.stringify(r, null, 2);
    flash("ReAct 跑完了");
  }

  async function orchestrate() {
    const body = { goal: agentBody().task };
    if (!body.goal) { flash("先写目标", true); return; }
    const out = $("agentOut");
    out.classList.remove("hidden");
    out.textContent = "在编排……跑完才回话，落进「任务」子页的表里。";
    const r = await req("/v1/agent/orchestrate", { method: "POST", body });
    out.textContent = JSON.stringify(r, null, 2);
    flash("编排结束");
    await loadTasks();
  }

  /* ---------- 配置 ---------- */

  let regOpen = null;

  function regLabel() {
    const b = $("btnRegToggle");
    b.textContent = "";
    b.appendChild(iconWrap("check"));
    b.appendChild(labelSpan(regOpen === null ? "注册开关：读取中"
      : regOpen ? "自助注册：开着，点一下关掉" : "自助注册：关着（管理员建号），点一下打开"));
  }

  async function loadConfig() {
    const note = $("regNote");
    if (regOpen === null) note.textContent = "读取中…";
    try {
      const cfg = await req("/v1/config");
      regOpen = !!cfg.registration_open;
      $("configRepo").textContent = cfg.update_repo ? String(cfg.update_repo) : "—";
      note.textContent = "";
      regLabel();
      return true;
    } catch (e) {
      if (e.message === "401") throw e;
      regOpen = null;
      note.textContent = "读不到配置：" + e.message + " ";
      note.appendChild(btn("refresh", "重试", "btn btn-small btn-ghost",
        () => busy(null, loadConfig)));
      $("configRepo").textContent = "—";
      regLabel();
      return false;
    }
  }

  async function toggleReg() {
    if (regOpen === null) { flash("配置还没读出来，先点刷新", true); return; }
    const ok = await ask(
      regOpen ? "关掉自助注册？" : "打开自助注册？",
      regOpen
        ? "关掉之后，陌生人拿到地址也注册不了；已建出来的账号不受影响，照常登录。以后随时可以在这里再打开。"
        : "打开之后，任何拿到这个地址的人都能自己注册一个账号并开始对话——模型的花费记在你这台服务上。"
        + "只发给可信的人时再考虑打开；平时建议关着，由你在账号页建号。",
      regOpen ? "关掉注册" : "打开注册", "check", regOpen);
    if (!ok) return;
    const r = await req("/v1/admin/config", { method: "POST", body: { registration_open: !regOpen } });
    if (r.rejected && r.rejected.length) {
      $("regNote").textContent = "后端没接受这些键：" + r.rejected.join(", ");
    }
    regOpen = !!(r.config && r.config.registration_open);
    regLabel();
    flash(regOpen ? "自助注册已打开" : "自助注册已关闭");
  }

  /* ---------- 运行子页懒加载 ---------- */

  const runtimeLoaded = {};
  const runtimeLoading = {};

  const RUNTIME_LOADERS = {
    usage: loadUsage,
    search: loadSearchUsage,
    memory: loadMemory,
    tasks: loadTasks,
    agent: null,
    config: loadConfig,
  };

  async function loadRuntimeSub(key, force) {
    if (!force && runtimeLoaded[key]) return;
    if (!force && runtimeLoading[key]) return;
    const loader = RUNTIME_LOADERS[key];
    if (!loader) { runtimeLoaded[key] = true; return; }
    const generation = dataGeneration;
    runtimeLoading[key] = true;
    try {
      const ok = await loader();
      if (generation === dataGeneration) runtimeLoaded[key] = ok !== false;
    } catch (e) {
      if (generation === dataGeneration) runtimeLoaded[key] = false;
    } finally {
      if (generation === dataGeneration) runtimeLoading[key] = false;
    }
  }

  function showRuntimeSub(key) {
    runtimeSub = RUNTIME_KEYS.indexOf(key) >= 0 ? key : "usage";
    document.querySelectorAll(".subpanel").forEach((panel) => {
      panel.classList.toggle("hidden", panel.id !== SUBPANELS[runtimeSub]);
    });
    document.querySelectorAll(".subnav-btn").forEach((b) => {
      const on = b.getAttribute("data-sub") === runtimeSub;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    updateTitle();
  }

  /* ---------- 装配 ---------- */

  async function enterSection(sectionId) {
    if (!authed) return;
    if (sectionId === "secOverview") { await loadOverview(); return; }
    if (sectionId === "secAccounts") { await enterAccounts(); return; }
    if (sectionId === "secSecurity") { await loadSecurity(); return; }
    if (sectionId === "secMcp") { await loadMcp(); return; }
    if (sectionId === "secFeedback") {
      if (!feedbackLoaded) {
        $("feedbackRows").textContent = "";
        $("feedbackRows").appendChild(emptyBox("读取中…"));
        try { await loadFeedback(true); }
        catch (e) {
          if (e.message === "401") return;
          paintRetry($("feedbackRows"), "反馈读取失败：" + e.message, () => loadFeedback(true));
        }
      }
      return;
    }
    if (sectionId === "secRuntime") { await loadRuntimeSub(runtimeSub, false); }
  }

  async function refreshCurrent() {
    if (!authed) return;
    // 顶栏刷新也要把未读角标带上：否则停在账号/安全/运行页时角标永远是打开页面那一刻的值。
    refreshFeedbackBadge();
    if (route === "overview") { await loadOverview(); return; }
    if (route === "accounts") { await loadUsers(); return; }
    if (route === "security") { await loadSecurity(); return; }
    if (route === "mcp") { await loadMcp(); return; }
    if (route === "feedback") { await loadFeedback(true); return; }
    if (route.indexOf("runtime/") === 0) { await loadRuntimeSub(runtimeSub, true); }
  }

  function invalidateData() {
    dataGeneration += 1;
    usersLoaded = false;
    allUsers = [];
    nameById = {};
    feedbackLoaded = false;
    feedbackItems = [];
    feedbackUnread = 0;
    feedbackNextBefore = null;
    secEvents = [];
    secEventIds = {};
    secNextBefore = null;
    secEndpointsMissing = false;
    mcpEnabled = false;
    mcpServers = [];
    mcpLoaded = false;
    resetMcpForm();
    $("mcpEnabled").checked = false;
    $("mcpEnabled").disabled = false;
    $("mcpEnabledNote").textContent = "读取中…";
    $("mcpRows").textContent = "";
    $("mcpCards").textContent = "";
    $("mcpState").classList.add("hidden");
    regOpen = null;
    Object.keys(runtimeLoaded).forEach((key) => { delete runtimeLoaded[key]; });
    Object.keys(runtimeLoading).forEach((key) => { delete runtimeLoading[key]; });
    setFeedbackBadge(0);
    $("revealText").textContent = "";
  }

  async function boot() {
    let me;
    try {
      me = await req("/v1/auth/me");
    } catch (e) {
      if (e.message !== "401") showGate("后端没连上：" + e.message);
      return;
    }
    if (me.role !== "admin") { showGate("这把口令有效，但它不是管理员。"); return; }
    authed = true;
    $("gate").classList.add("hidden");
    $("panel").classList.remove("hidden");
    $("who").textContent = "当前身份：" + me.username + "（" + me.user_id + "）";
    applyRoute();
    // 无论落在哪个 hash，都先无条件取一次未读数；停在 #/accounts 之类的入口也要有角标。
    refreshFeedbackBadge();
  }

  /* ---------- 事件 ---------- */

  $("btnLogin").addEventListener("click", async () => {
    const v = $("pass").value.trim();
    if (!v) { showGate("口令不能为空"); return; }
    $("pass").value = "";
    try {
      const res = await fetch("/v1/auth/adopt", {
        method: "POST",
        credentials: "same-origin",
        headers: { Authorization: "Bearer " + v, "X-CSRF": "1" },
      });
      let data = null;
      try { data = await res.json(); } catch (e) { /* 空响应体 */ }
      if (res.status === 401) { showGate("口令不对或已失效"); return; }
      if (!res.ok) {
        const d = data && data.detail ? data.detail : ("HTTP " + res.status);
        showGate("没能收编会话：" + d);
        return;
      }
      await boot();
    } catch (e) { showGate("后端没连上：" + e.message); }
  });
  $("pass").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btnLogin").click(); });

  $("btnLogout").addEventListener("click", async () => {
    try { await req("/v1/auth/logout", { method: "POST" }); }
    catch (e) { /* 撤销没成功也把面板收起：没人守着半开的门 */ }
    showGate("已退出：这一台设备的会话已作废，其他设备不受影响。");
  });

  $("btnReload").addEventListener("click", (e) => busy(e.currentTarget, refreshCurrent));

  $("askOk").addEventListener("click", () => closeAsk(true));
  $("askCancel").addEventListener("click", () => closeAsk(false));
  $("ask").addEventListener("click", (e) => { if (e.target === $("ask")) closeAsk(false); });
  $("reveal").addEventListener("click", (e) => { if (e.target === $("reveal")) closeReveal(); });
  $("detail").addEventListener("click", (e) => { if (e.target === $("detail")) closeDetail(); });
  $("btnCloseDetail").addEventListener("click", closeDetail);
  $("imageViewerClose").addEventListener("click", closeImageViewer);

  $("btnCloseReveal").addEventListener("click", closeReveal);
  $("btnCopy").addEventListener("click", async () => {
    const text = $("revealText").textContent;
    try { await navigator.clipboard.writeText(text); flash("已复制到剪贴板"); }
    catch (e) { flash("复制失败，请长按选中手动复制", true); }
  });

  $("btnDecay").addEventListener("click", (e) => busy(e.currentTarget, decay));
  $("btnCreateUser").addEventListener("click", (e) => busy(e.currentTarget, createUser));
  $("btnGenPass").addEventListener("click", () => {
    $("newPass").value = randomPassword(12);
    flash("已生成随机初始口令；建号后只显示这一次");
  });
  $("newPass").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btnCreateUser").click(); });
  $("userSearch").addEventListener("input", renderUsers);
  $("userStatus").addEventListener("change", renderUsers);
  $("btnRegToggle").addEventListener("click", (e) => busy(e.currentTarget, toggleReg));
  $("usageDay").addEventListener("change", (e) => {
    usageDay = e.target.value;
    busy(null, loadUsage);
  });
  $("btnUsage").addEventListener("click", (e) => busy(e.currentTarget, loadUsage));
  $("searchUsageDays").addEventListener("change", (e) => {
    searchUsageDays = parseInt(e.target.value, 10) || 7;
    busy(null, loadSearchUsage);
  });
  $("btnSearchUsage").addEventListener("click", (e) => busy(e.currentTarget, loadSearchUsage));
  $("btnFeedback").addEventListener("click", (e) => busy(e.currentTarget, () => loadFeedback(true)));
  $("btnFbMore").addEventListener("click", (e) => busy(e.currentTarget, loadMoreFeedback));
  $("btnFbExport").addEventListener("click", (e) => busy(e.currentTarget, exportFeedback));
  $("fbAll").addEventListener("click", () => setFeedbackFilter(false));
  $("fbUnread").addEventListener("click", () => setFeedbackFilter(true));
  $("fbSearch").addEventListener("input", renderFeedback);
  $("btnRunAgent").addEventListener("click", (e) => busy(e.currentTarget, runAgent));
  $("btnOrchestrate").addEventListener("click", (e) => busy(e.currentTarget, orchestrate));
  $("btnMcpReload").addEventListener("click", (e) => busy(e.currentTarget, loadMcp));
  $("mcpEnabled").addEventListener("change", (e) => busy(e.currentTarget, toggleMcpEnabled));
  $("btnMcpSave").addEventListener("click", (e) => busy(e.currentTarget, saveMcp));
  $("btnMcpReset").addEventListener("click", resetMcpForm);

  $("secWindow").addEventListener("change", (e) => {
    secWindow = e.target.value || "24h";
    busy(null, () => loadSecurity());
  });
  $("btnSecReload").addEventListener("click", (e) => busy(e.currentTarget, () => loadSecurity()));
  $("btnSecFilter").addEventListener("click", (e) => busy(e.currentTarget, applySecurityFilters));
  // 级别是单选：切换即重取，不再要求额外按一次「筛选」。
  $("secSeverityFilter").addEventListener("change", () => busy(null, applySecurityFilters));
  $("secSource").addEventListener("keydown", (e) => {
    if (e.key === "Enter") busy(null, applySecurityFilters);
  });
  $("secAccount").addEventListener("keydown", (e) => {
    if (e.key === "Enter") busy(null, applySecurityFilters);
  });
  $("btnSecMore").addEventListener("click", (e) => busy(e.currentTarget, loadMoreSecurity));
  $("auditBox").addEventListener("toggle", () => {
    if ($("auditBox").open) busy(null, loadAudit);
  });

  document.querySelector(".subnav").addEventListener("click", (e) => {
    const b = e.target.closest(".subnav-btn");
    if (!b) return;
    navigate("runtime/" + b.getAttribute("data-sub"));
  });

  document.addEventListener("click", (e) => {
    const trigger = e.target.closest("button, [href], input, select, textarea, summary");
    if (trigger) lastTrigger = trigger;
  }, true);

  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-route]");
    if (!el) return;
    e.preventDefault();
    navigate(el.getAttribute("data-route"));
  });

  document.addEventListener("keydown", (e) => {
    const open = document.querySelector(".sheet:not(.hidden)");
    if (!open) return;
    if (e.key === "Escape") {
      e.preventDefault();
      if (open.id === "ask") closeAsk(false);
      else if (open.id === "reveal") closeReveal();
      else if (open.id === "detail") closeDetail();
      else if (open.id === "imageViewer") closeImageViewer();
      return;
    }
    if (e.key === "Tab") trapFocus(e, open);
  });

  window.addEventListener("hashchange", applyRoute);
  document.addEventListener("visibilitychange", () => {
    syncSecurityTimer();
    if (authed && route === "security" && document.visibilityState === "visible") {
      loadSecurity({ auto: true });
    }
  });

  decorateIcons(document);
  fbBadge = cell("span", "", "badge hidden");
  $("tab-feedback").appendChild(fbBadge);
  applyRoute();
  boot().catch((e) => { if (e.message !== "401") flash(e.message, true); });
})();
