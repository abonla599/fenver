/* 后端接口封装。一律使用相对路径：页面与 API 同源，
 * 手机通过任何地址（局域网 IP、隧道域名）访问都无需改前端配置。
 */
"use strict";

/* 断网提醒（与安卓 NetMinder.kt 同判据同文案，2026-09-28 真机反馈第 3 轮）：
 * fetch 抛 TypeError = 请求根本没出设备（没互联网 / 连不上服务端 / DNS 挂了）；
 * 4xx/5xx 是服务端正常回话，业务错误走状态条，不算断网、不弹这个。
 * 顶部居中小卡片悬浮，不占布局不挡操作；「今日不再显示」按本地日期存
 * localStorage，隔天自动恢复提醒。任何一次成功通信把卡片收起来。 */
const NetMinder = {
  _el: null,
  _today() {
    const d = new Date(), p = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
  },
  _muted() {
    try { return localStorage.getItem("netminder_mute") === this._today(); } catch (_) { return false; }
  },
  _ensure() {
    if (this._el && document.body.contains(this._el)) return this._el;
    const el = document.createElement("div");
    el.id = "netMinder";
    el.innerHTML = '<span class="nm-text">⚠ 连不上网络了，消息暂时发不出去</span>'
      + '<button type="button" class="nm-btn nm-ok">知道了</button>'
      + '<button type="button" class="nm-btn nm-mute">今日不再显示</button>';
    el.querySelector(".nm-ok").addEventListener("click", () => this.hide());
    el.querySelector(".nm-mute").addEventListener("click", () => {
      try { localStorage.setItem("netminder_mute", this._today()); } catch (_) {}
      this.hide();
    });
    document.body.appendChild(el);
    this._el = el;
    return el;
  },
  note(e) { if (e instanceof TypeError && !this._muted()) this._ensure().classList.remove("hidden"); },
  ok() { this.hide(); },
  hide() { if (this._el) this._el.classList.add("hidden"); },
};

const API = (() => {
  /* 方案 C：页面不再持有任何凭据明文。
   * 会话身份来自服务端签发的 httpOnly Cookie（authz.SESSION_COOKIE），浏览器对
   * 同源请求自动附带——下面每个 fetch 都显式写 credentials:"same-origin"，是把
   * 意图钉死，不让哪个引擎的默认值替我们做主。authHeaders() 保留成空对象是历史
   * 形状的墓碑：谁再往这里塞 Authorization，就是在重新把令牌喂回 JS 可读的内存。
   *
   * 仅剩两处显式带凭据的请求头，都是"凭据在调用处参数里"的一次性语义：
   * adopt(token)——把刚拿到手的明文收编成 Cookie；logout(token)——替清单里的
   * 另一个人撤销他那一枚。除此之外任何请求都不该有 Authorization。
   *
   * CSRF：不安全方法（POST/PUT/PATCH/DELETE）一律带 X-CSRF:1。后端只在
   * "没有请求头凭据而凭据出自 Cookie"时才检查它（见 authz.py），带上了对
   * 头认证客户端无害。
   */
  const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
  function authHeaders() { return {}; }
  function csrfHeaders(method) {
    return UNSAFE_METHODS.has(String(method || "").toUpperCase()) ? { "X-CSRF": "1" } : {};
  }

  /* 记忆检索的条数上限，与后端 SearchMemoryRequest.top_k 的 le=20 同源
   * （test_web_pwa 会把这两个数字对一次）。越过上限不是"少给几条"，而是整个
   * 请求 422：搜索框看上去就是坏的。所以在这里夹回来，宁可少给也要有结果。
   */
  const MEMORY_TOP_K_MAX = 20;
  function clampTopK(want) {
    const n = Number(want) || 10;
    return Math.min(MEMORY_TOP_K_MAX, Math.max(1, n));
  }

  async function parseError(res) {
    let detail = res.statusText || ("HTTP " + res.status);
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    const err = new Error(detail);
    err.status = res.status;
    return err;
  }

  /* 撤销**指定那一枚**令牌（不是当前这枚）。从本机清单里移除一个人时可以不
     带参数——只靠 Cookie 退当前会话。先把他切成当前身份再退出，等于为了删除
     而把他的会话加载到共用设备的屏幕上，所以历史上的显式 token 形参保留。 */
  async function logout(token) {
    const res = await fetch("/v1/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        ...(token ? { Authorization: "Bearer " + token } : {}),
        ...csrfHeaders("POST"),
      },
    });
    if (!res.ok) throw await parseError(res);
    return res.json();
  }

  /* 把一枚刚拿到手的令牌明文收编成 httpOnly 会话 Cookie（全页面唯一发
     Authorization 头的常规路径）。成功后明文就该被丢掉——app.js 里它只作为
     局部变量活过这一次调用。 */
  async function adopt(token) {
    const res = await fetch("/v1/auth/adopt", {
      method: "POST",
      credentials: "same-origin",
      headers: { Authorization: "Bearer " + token, ...csrfHeaders("POST") },
    });
    if (!res.ok) throw await parseError(res);
    return res.json();
  }

  async function request(path, { method = "GET", body, signal } = {}) {
    let res;
    try {
      res = await fetch(path, {
        method,
        signal,
        credentials: "same-origin",
        headers: {
          ...(body ? { "Content-Type": "application/json" } : {}),
          ...authHeaders(),
          ...csrfHeaders(method),
        },
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (e) { NetMinder.note(e); throw e; }
    if (!res.ok) throw await parseError(res);
    NetMinder.ok();
    return res.status === 204 ? null : res.json();
  }

  /* 断线不重跑整轮的可续播内核（v0.25 web · T2.4/T2.8）。
   *
   * 老行为：流尾没拿到 done 就抛 retryable=true，上层据此整段 POST 回 /v1/chat 重发
   * 这一轮——用户一关页面/断线就是重复计费 + 重复入库。后端已把协议定稿（每帧带
   * `id: <run_id>:<seq>`、续播走 Last-Event-ID、续不上回 410、被停的流以旧解析器认得的
   * done 收尾），这里把网页这一侧接上去：断线先带游标重开同一条流端点续播剩下的帧；
   * 服务端说续不上（410）或试到次数上限，就交一句"去会话里取回"的实话
   * （err.needHistory=true、retryable=false），绝不回头重发生成。
   *
   * 刻意把 open/sleep/onChunk/onEvent/onStatus/onRun/onUnknown/maxAttempts/resumeStatus
   * 全从 deps 进来，只为了让测试能用 node 真跑这份控制流（见 test_web_pwa 的 _run_stream_js）：
   * "断线到底走了续播还是重发、退避有没有真在退、410 是不是转取历史"读源码读不出对错。
   *
   * v0.29.0 加 onEvent：过程帧（thinking/tool_call/tool_result/search）连同 start/done
   * 这些非内容帧都从这一条缝交出去，**onChunk 仍只吃 content 文本**——老调用点一个字没变。
   */
  const STREAM_RESUME_STATUS = "连接断了，正在接着上次的进度取回…";
  // 重连次数上限。这不是"省钱旋钮"：宽限期长短与花销由服务端的轮次边界钱闸决定，和这里
  // 试几次无关；上限存在的意义只是"别无限期敲一条已经续不上的门"，到点转取历史。
  const STREAM_RESUME_MAX_ATTEMPTS = 5;

  function defaultSleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  async function runResilientStream(deps) {
    const open = deps.open;
    const onChunk = deps.onChunk;
    const onEvent = deps.onEvent;
    const onStatus = deps.onStatus;
    const onRun = deps.onRun;
    const onUnknown = deps.onUnknown;
    // 全部依赖收在这一处，runResilientStream 因此能被单独取出来用 node 真跑（见测试）：
    // 兜底值都写死在这里，不引用模块作用域的名字，否则单独执行它会在提取体外炸出 ReferenceError。
    const sleep = deps.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
    // 兜底值写成字面量而不是引模块作用域的常量：runResilientStream 要能被单独取出来
    // 用 node 跑，引用体外名字会让单独执行时炸出 ReferenceError（那句话的唯一出处仍是上面
    // 的 STREAM_RESUME_STATUS，由调用方 streamChat 传进来，这里不复制第二份）。
    const maxAttempts = deps.maxAttempts === undefined ? 5 : deps.maxAttempts;
    const resumeStatus = deps.resumeStatus;
    const BACKOFF_BASE_MS = 500;

    const decoder = new TextDecoder("utf-8");
    const unknown = [];
    let runId = "";
    let lastEventId = "";       // "<run_id>:<seq>"，续播时放进 Last-Event-ID
    let resumable = true;       // 收到 cannot_resume 置 false：这一轮续不上了
    let attempt = 0;            // 已经续播过几次

    for (;;) {
      let buf = "";
      let res;
      try {
        res = await open(lastEventId || null);
      } catch (e) {
        // open 抛出 = 这一枪根本没到服务端（断网 / DNS / 连不上）。
        // 从没握手成功 = 还没开这一轮，退避后重开一次同一条流端点是安全的；
        // 已经拿到过 run_id = 服务端正在跑这一轮，只能带游标续播，不能重发生成。
        if (attempt < maxAttempts) {
          attempt += 1;
          if (runId && onStatus) onStatus(resumeStatus);
          await sleep(BACKOFF_BASE_MS * Math.pow(2, attempt - 1));
          continue;
        }
        const err = new Error("连不上服务端，这一轮没能接上：不用重发原文，结果会写进会话历史，刷新会话取回");
        err.kind = "need-history";
        err.needHistory = true;
        err.retryable = false;
        throw err;
      }

      if (!res.ok || !res.body) {
        // 410 是明确的"续不上，别重发，去会话里取"；其它非 2xx 是这一轮请求本身的问题。
        // 两种都不回头重发生成；有 run_id 且没到上限的意外状态，当作断线退避后重试。
        const status = res.status;
        if (status !== 410 && runId && resumable && attempt < maxAttempts) {
          attempt += 1;
          if (onStatus) onStatus(resumeStatus);
          await sleep(BACKOFF_BASE_MS * Math.pow(2, attempt - 1));
          continue;
        }
        const err = new Error(status === 410
          ? "这一轮接不上了：不用重发原文，结果会写进会话历史，刷新会话取回"
          : "这一轮没能开始：服务端没有接受这次请求");
        err.kind = "need-history";
        err.needHistory = true;
        err.retryable = false;
        err.status = status;
        throw err;
      }

      const reader = res.body.getReader();
      let terminal = null;
      readLoop:
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const lines = frame.split("\n");
          const idLine = lines.find((l) => l.startsWith("id:"));
          const dataLine = lines.find((l) => l.startsWith("data:"));
          if (idLine) lastEventId = idLine.slice(3).trim();
          if (!dataLine) continue;
          let evt;
          try { evt = JSON.parse(dataLine.slice(5).trim()); } catch (_) { continue; }
          // run_id 从 id 行或帧里的 run_id 字段拿，拿到一次就交给上层做"停止"的目标。
          if (evt && evt.run_id && !runId) { runId = evt.run_id; if (onRun) onRun(runId); }
          else if (!runId && idLine) {
            runId = idLine.slice(3).trim().split(":")[0];
            if (onRun) onRun(runId);
          }
          const type = evt && evt.type;
          // 非内容帧一律先交 onEvent 这一条缝（v0.29.0 的过程帧：thinking/tool_call/
          // tool_result/search，以及 start/done）。两种情况都不能把流弄断：没传回调等于
          // 没有这件事；回调自己炸了（界面在画一个已经摘掉的节点）也只是这一帧没画上，
          // 正文与终帧照走下面的分发。
          if (type !== "content" && onEvent) {
            try { onEvent(evt); } catch (_) { /* 渲染失败不杀流 */ }
          }
          if (type === "content") {
            if (onChunk) onChunk(evt.text || "");
          } else if (type === "done") {
            terminal = evt;              // completed / cancelled / cannot_resume 都以 done 收尾
            break readLoop;
          } else if (type === "cancelled") {
            // 停止或宽限到点：半句已产出，后面紧跟一条 done 收尾，这里不单独终结。
          } else if (type === "cannot_resume") {
            resumable = false;
          } else if (type === "error") {
            const err = new Error(evt.message || "模型返回错误");
            err.kind = "error";
            err.retryable = false;       // 原因已给出：不该再重发整轮去二次付费
            throw err;
          } else if (type === "start") {
            // 带 message_id/model/run_id；run_id 已在上面捕获。
          } else if (type === "thinking" || type === "tool_call"
                     || type === "tool_result" || type === "search") {
            // v0.29.0 的过程帧：上面那条 onEvent 缝已经把整帧交出去了，这里只是别让它们
            // 掉进下面的 else——unknown 那一笔的语义是「契约之外冒出来的东西」，把已知帧
            // 记进去等于给上报加噪声（帧名唯一出处仍是 core/stream_events.py 的 CLIENT_FRAMES）。
          } else {
            // 认不出的帧：不静默丢——记下来上报，同时不打断正常收尾。
            unknown.push(evt);
            if (onUnknown) onUnknown(evt);
          }
        }
      }

      if (terminal) return { done: terminal, unknown };

      // 连接关闭却没有终帧 = 断线。拿不到 run_id、或服务端已说续不上，就没有可续播的东西：
      // 转取历史，绝不重发生成。否则带游标退避重开续播，试到上限同样转取历史。
      if (!runId || !resumable) {
        const err = new Error("这一轮接不上了：不用重发原文，结果会写进会话历史，刷新会话取回");
        err.kind = "need-history";
        err.needHistory = true;
        err.retryable = false;
        throw err;
      }
      if (attempt >= maxAttempts) {
        const err = new Error("试了几次都没能接上这一轮：不用重发原文，结果会写进会话历史，刷新会话取回");
        err.kind = "need-history";
        err.needHistory = true;
        err.retryable = false;
        throw err;
      }
      attempt += 1;
      if (onStatus) onStatus(resumeStatus);
      await sleep(BACKOFF_BASE_MS * Math.pow(2, attempt - 1));
      // 回到 for(;;)：下一次 open 带上 lastEventId 续播
    }
  }

  /* 流式对话。POST 无法用 EventSource，故手工读流并按空行分帧（分帧/续播/退避在上面的
   * 内核里）。open 把"重开同一条流"封成一个函数：首次带原文 body 开轮，续播带 Last-Event-ID
   * 头重开——服务端据此只补缓冲帧，不叫模型、不重记账。onStatus/onRun/onUnknown 由界面传入，
   * 分别用于人话状态、记住 run_id（给"停止"打 cancel）、上报看不懂的帧。
   *
   * 第三个形参 onEvent 是 v0.29.0 加的那条缝：只吃非内容帧，onChunk 照旧只管 content
   * 文本，所以 `streamChat(opts, onChunk)` 这一句老写法一个字节都还是对的。写成可选、
   * 且 opts.onEvent 也算同一条缝（与 onStatus/onRun 那一组同风格），界面两种挂法都行。
   */
  async function streamChat(opts, onChunk, onEvent) {
    const open = async (lastEventId) => {
      const headers = { "Content-Type": "application/json", ...authHeaders(), ...csrfHeaders("POST") };
      if (lastEventId) headers["Last-Event-ID"] = lastEventId;   // 续播凭据：<run_id>:<seq>
      let res;
      try {
        res = await fetch("/v1/chat/stream", {
          method: "POST",
          signal: opts.signal,
          credentials: "same-origin",
          headers,
          body: JSON.stringify({
            model: opts.model, provider: opts.provider, messages: opts.messages,
            attachments: opts.attachments || [],
            session_id: opts.sessionId,
          }),
        });
      } catch (e) { NetMinder.note(e); throw e; }
      if (res.ok && res.body) NetMinder.ok();
      return res;
    };
    const out = await runResilientStream({
      open, onChunk, onEvent: onEvent || (opts && opts.onEvent),
      onStatus: opts.onStatus, onRun: opts.onRun, onUnknown: opts.onUnknown,
      sleep: defaultSleep, maxAttempts: STREAM_RESUME_MAX_ATTEMPTS,
      resumeStatus: STREAM_RESUME_STATUS,
    });
    return out.done;   // 兼容旧调用点：done 里有 message_id/model/full_text/status
  }

  /* 图片预览：改用 Cookie 会话后 <img> 那条老问题换了答案——fetch 带
   * credentials 就能认身份，blob 转本地 URL 的做法保留（它同时挡掉
   * "URL 里露凭据"的另一类泄露）。
   */
  async function fileBlobUrl(uploadId) {
    const res = await fetch(`/v1/uploads/${encodeURIComponent(uploadId)}/file`,
      { headers: authHeaders(), credentials: "same-origin" });
    if (!res.ok) throw await parseError(res);
    return URL.createObjectURL(await res.blob());
  }

  async function upload(file) {
    const fd = new FormData();
    fd.append("file", file, file.name);
    const res = await fetch("/v1/uploads", {
      method: "POST", body: fd, credentials: "same-origin",
      headers: { ...authHeaders(), ...csrfHeaders("POST") },
    });
    if (!res.ok) throw await parseError(res);   // 不设 Content-Type，交给浏览器带 boundary
    return res.json();
  }

  return {
    /* 身份：注册与登录的响应体里仍有一次性的 token（对外契约没动），但它在页面
     * 里的正确用法是当场交给 adopt() 换成 httpOnly Cookie，然后让明文自生自灭。
     * me() 是前端唯一的"我到底是谁"来源——角色不能靠猜，猜错就把 403 按钮留在页面上。
     * 密码只出现在注册/登录/改密这几个请求的 body 里，绝不进任何其它请求头。
     *
     * 这里不再有 recovery 封装：找回的三道题是全站固定常量（app.js 里那份
     * RECOVERY_QUESTIONS，test_web_pwa 会拿后端 auth.RECOVERY_QUESTIONS 逐字比一次），
     * 界面自己渲染，不必问服务器要。服务器上一次"报出问题"的响应，本质上是一份
     * "这个用户名存在吗"的名单，所以那条路由连同这个封装一起删了。
     */
    register: (username, password, security_answers) =>
      request("/v1/auth/register", { method: "POST",
        body: { username, password, security_answers } }),
    login: (username, password) =>
      request("/v1/auth/login", { method: "POST", body: { username, password } }),
    /* 三条答案 + 新密码一次提交：分开验答案就给外人一个"这个答案对不对"的 oracle。
     * new_answers（轮换找回答案）是可选的，界面不提供，于是也不该发一个 undefined 出去。
     * 成功只回 {"status": "password_reset"} 且不发令牌——该人名下所有令牌同时作废。
     */
    resetPassword: (username, answers, new_password) =>
      request("/v1/auth/reset", { method: "POST",
        body: { username, answers, new_password } }),
    /* 首登强制改密（v0.24 T3.3）：管理员建号/重置发出的初始密码没有配套的找回答案，
     * 答案通道救不了这种号——持旧密换新密是它唯一的自救路径，后端只认当前会话。 */
    changePassword: (old_password, new_password) =>
      request("/v1/auth/change-password", { method: "POST",
        body: { old_password, new_password } }),
    me: () => request("/v1/auth/me"),
    logout: (token) => logout(token),
    // 定义在上面的 adopt()：注册/登录拿到明文后的第一步就是把它收编成 Cookie，
    // 漏了这一行，app.js 里那三处 API.adopt 会在手机上炸成"API.adopt is not a
    // function"（2026-09-23 v0.19 首发注册实测）。锁见 test_web_pwa。
    adopt,

    models: () => request("/v1/models"),
    upload,
    fileBlobUrl,

    /* 模型服务配置（管理员面）：改的是**所有人**的上游，后端要求管理员。
     * 普通用户拿 403；他们的自助面在下面 /v1/me/providers 那一组。
     */
    providers: () => request("/v1/providers"),
    addProvider: (rec) => request("/v1/providers", { method: "POST", body: rec }),
    updateProvider: (id, rec) => request("/v1/providers/" + encodeURIComponent(id), { method: "PUT", body: rec }),
    deleteProvider: (id) => request("/v1/providers/" + encodeURIComponent(id), { method: "DELETE" }),
    setDefaultProvider: (id) => request(`/v1/providers/${encodeURIComponent(id)}/default`, { method: "POST" }),
    testProvider: (id) => request(`/v1/providers/${encodeURIComponent(id)}/test`, { method: "POST" }),
    testProviderDraft: (rec) => request("/v1/providers/test", { method: "POST", body: rec }),

    /* 个人模型服务面：所有登录用户可用，作用域由服务端按身份圈定——
     * 这里不传也不需要传任何 user_id；别人的私有条目在这里等于不存在。
     * 「我的默认」持久化在服务端，换设备不再回到站级默认。
     */
    myProviders: () => request("/v1/me/providers"),
    addMyProvider: (rec) => request("/v1/me/providers", { method: "POST", body: rec }),
    updateMyProvider: (id, rec) => request("/v1/me/providers/" + encodeURIComponent(id), { method: "PUT", body: rec }),
    deleteMyProvider: (id) => request("/v1/me/providers/" + encodeURIComponent(id), { method: "DELETE" }),
    setMyDefaultProvider: (id) => request("/v1/me/providers/default", { method: "POST", body: { provider_id: id } }),
    testMyProvider: (id) => request(`/v1/me/providers/${encodeURIComponent(id)}/test`, { method: "POST" }),
    testMyProviderDraft: (rec) => request("/v1/me/providers/test", { method: "POST", body: rec }),

    /* 日程（v0.23 R3 · T2.6）：一天一份清单。day 留空 = 让服务端说今天是哪天，
     * 客户端自己不猜：手机改了系统时间、或跨天 0 点后才打开这一页，问出的"今天"
     * 都该是服务端那一份（R3-AC-3）。保存是整天 PUT 幂等替换，不是逐条增删——
     * 界面手里本来就握着全天那份，一次 PUT 重放结果相同，逐条的口子留给工具侧。
     */
    getSchedule: (day) => request("/v1/schedule" +
      (day ? "?day=" + encodeURIComponent(day) : "")),
    putSchedule: (day, items) => request("/v1/schedule",
      { method: "PUT", body: { day, items } }),

    createSession: (model) => request("/v1/sessions?model=" + encodeURIComponent(model), { method: "POST" }),
    listSessions: () => request("/v1/sessions"),
    getSession: (id) => request("/v1/sessions/" + encodeURIComponent(id)),
    deleteSession: (id) => request("/v1/sessions/" + encodeURIComponent(id), { method: "DELETE" }),
    /* 导出：换一张一次性下载票据。壳里的 WebView 收不到 blob 下载、也不会给下载
       请求带 Authorization，所以文件必须由服务端给一个真实的 https 链接。 */
    exportTicket: (id) => request(`/v1/sessions/${encodeURIComponent(id)}/export-ticket`, { method: "POST" }),
    replaceMessages: (id, messages) =>
      request("/v1/sessions/" + encodeURIComponent(id) + "/messages", { method: "PUT", body: { messages } }),
    chat: (payload) => request("/v1/chat", { method: "POST", body: payload }),
    streamChat,
    /* "停止"是一个显式动作，不再只是关页面的副作用：带 run_id 打服务端取消，翻标志、
     * 尽力当场关上游、下一个付费轮不发生（见 core/stream_runs.py + main.py 的 cancel 端点）。
     * 归属判定在服务端：不是你的/不存在的 run_id 都是同一句 404，cancel 不是探测信道。 */
    cancelStreamRun: (runId) =>
      request("/v1/chat/stream/" + encodeURIComponent(runId) + "/cancel", { method: "POST" }),

    /* 记忆：身份只来自访问令牌，这些函数不收任何身份参数（也不该收）。 */
    addMemory: (content) =>
      request("/v1/memory/add", { method: "POST", body: { content, summarize: false } }),
    listMemory: (limit = 50) =>
      request(`/v1/memory/list?limit=${limit}`),
    searchMemory: (query, topK = 10) =>
      request("/v1/memory/search", { method: "POST", body: { query, top_k: clampTopK(topK) } }),
    deleteMemory: (memoryIds) =>
      request("/v1/memory/delete", { method: "DELETE", body: { memory_ids: memoryIds } }),
    // 全库统计是管理员端点：调用前先看角色（app.js 的 isAdmin），
    // 别把一个"你没权限"报成"服务挂了"。
    memoryStats: () => request("/v1/memory/stats"),
    feedback: (messageId, rating, comment) =>
      request("/v1/feedback", { method: "POST", body: { message_id: messageId, rating, comment } }),
    /* 意见反馈 → 管理员收集页（v0.27，与安卓 submitUserFeedback 同一条链路）。
       图片走「先传后附」：每张先 upload() 换成 upload id，再连正文/邮箱一起提交。
       编号在服务端分配（FB-0000NN），这一版起反馈不再跳去仓库 issue。 */
    submitUserFeedback: (text, email, images) =>
      request("/v1/user-feedback", { method: "POST",
        body: { text, email: email || null, images: images && images.length ? images : null } }),
  };
})();
