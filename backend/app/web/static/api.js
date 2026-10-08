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

  /* ---------------- v0.34.0 续期阶梯（判据 1、9；PRD 第四节 Web 那一行） ----------------
   *
   * 页面依旧不持有任何凭据：会话在 httpOnly Cookie 里，所以续期就是**用 Cookie 打**
   * /v1/auth/renew。响应体里的新令牌只活在这一次调用的局部作用域，当场交给 adopt 收编
   * 成新 Cookie——adopt 仍然是全服务端唯一 Set-Cookie 的地方，会话固定那道缝不开。
   *
   * 触发口径是**服务端告诉我们的**：每个 2xx 已鉴权响应带 X-Token-Expires-In，值是
   * min(滑动剩余, 绝对剩余)。客户端自己不猜时间——本机时钟偏一点就能把"还剩 8 天"
   * 算成"还剩 6 天"，那是一条只有服务端知道答案的问题。
   *
   * renewSession 把全部依赖收在 deps 里、兜底值写字面量，是为了这份控制流能被单独取出
   * 来用 node 真跑（test_v0340_web_renew_ladder）："剩余还多就不许动手"与"有流在跑就
   * 绝不动手"读源码读不出对错，只有拿运行时说话（沿用 runResilientStream 的规矩）。
   */
  const TOKEN_EXPIRES_HEADER = "X-Token-Expires-In";
  const RENEW_TRIGGER_SECONDS = 7 * 24 * 3600;    // 与后端 RENEW_ALLOWED_BELOW_SECONDS 同一个数
  const RENEW_COOLDOWN_SECONDS = 6 * 3600;        // 失败不追着敲：限速器不吃自杀式重试

  const SESSION = { remaining: null, streams: 0, renewing: false, lastRenewAt: 0, lastReauthAt: 0 };

  async function renewSession(deps) {
    // 兜底值写字面量而不是引用上面那两个模块作用域的常量：这份函数要能被单独取出来
    // 用 node 真跑（沿用 runResilientStream 的同一条规矩），引用体外名字会炸 ReferenceError。
    // 数字的唯一出处仍是 RENEW_TRIGGER_SECONDS / RENEW_COOLDOWN_SECONDS 那两行。
    const threshold = deps.threshold === undefined ? 7 * 24 * 3600 : deps.threshold;
    const cooldown = deps.cooldown === undefined ? 6 * 3600 : deps.cooldown;
    const now = deps.now || (() => Date.now() / 1000);
    // 没有头条 = 这次响应没告诉我们还剩多久（未鉴权、bootstrap、或老版本服务端）。
    // 不猜：续期这件事的判据在服务端，这里缺输入就什么都不做。
    if (deps.remaining === null || deps.remaining === undefined) return "no-header";
    if (deps.remaining > threshold) return "not-due";        // 判据 1：还早，别把续期当心跳
    if (deps.streamBusy) return "deferred";                   // 判据 9：在途 SSE 一个字都不碰
    if (deps.renewing) return "already-running";
    const moment = now();
    if (moment - deps.lastRenewAt < cooldown) return "cooling-down";
    deps.setRenewing(true);
    deps.setLastRenewAt(moment);
    try {
      const data = await deps.renew();
      if (!data || !data.token) return "failed";     // 没拿到新明文就不动现有 Cookie
      await deps.adopt(data.token);
      return "renewed";
    } catch (e) {
      // 判据原文：renew 失败**什么都不做**。老令牌还能用到绝对上限，界面照常可用；
      // 真到上限那一次 401 由 needsAuth 落登录页——那是最后一层，不是这一层的事。
      return "failed";
    } finally {
      deps.setRenewing(false);
    }
  }

  function noteSessionExpiry(res) {
    // 只从 2xx 取头：401/403/429 的头条（如果有）说的不是"你这个会话还剩多久"。
    try {
      if (!res || !res.ok || !res.headers) return;
      const raw = typeof res.headers.get === "function"
        ? res.headers.get(TOKEN_EXPIRES_HEADER) : null;
      if (raw === null) return;
      const n = Number(raw);
      if (Number.isFinite(n) && n >= 0) SESSION.remaining = n;
    } catch (_) { /* 读头这件事永远不许把业务打停 */ }
  }

  /* 真正的续期动作：fire-and-forget。调用点不 await 它——一次后台续期不该让
     用户等，更不该因为它炸了就把这轮的正文弄丢。 */
  function sessionTick() {
    return renewSession({
      remaining: SESSION.remaining,
      streamBusy: SESSION.streams > 0,
      renewing: SESSION.renewing,
      lastRenewAt: SESSION.lastRenewAt,
      threshold: RENEW_TRIGGER_SECONDS,
      cooldown: RENEW_COOLDOWN_SECONDS,
      renew: async () => {
        const res = await fetch("/v1/auth/renew", {
          method: "POST",
          credentials: "same-origin",
          headers: csrfHeaders("POST"),
        });
        if (!res.ok) throw await parseError(res);
        noteSessionExpiry(res);          // 续完立刻用服务端给的头条重新校准
        return res.json();
      },
      adopt,
      setRenewing: (v) => { SESSION.renewing = v; },
      setLastRenewAt: (t) => { SESSION.lastRenewAt = t; },
    }).catch(() => "failed");
  }

  /* ---------------- 续命凭据的本地保管（v0.34.0 判据 4 的 Web 半边） ----------------
   *
   * 2026-10-08 用户改口径："Web 也持久化续命凭据"。PRD 第四节原本写的是"本地存储现状
   * 不变"，那是"Web 只用 Cookie 续期"那一半的口径；这一句把它扩到"绝对上限之后也能
   * 自己爬起来"——没有这把凭据，网页在 180 天绝对上限那一次 401 只能落登录页，而
   * 同一台手机上的原生 App 已经能免口令续上，两边行为不一致才是更坏的结局。
   *
   * 与判据 11 的关系要说清楚：那条禁的是**口令**进本地存储，这条存的是 device_secret。
   * 两者不同类，风险却同类——浏览器没有 Keystore，这一格是 JS 可读的明文。三条把风险
   * 关在笼子里的现实约束，全部有对应的锁（test_v0340_web_device_vault）：
   * ① 只有一格、只有一个键名前缀、只有三个出入口（写 / 读 / 清），名册仍然零凭据；
   * ② 服务端已经是它的权威生命周期：改密与三题找回清全部 secrets、退出清全部、
   *    单次使用、猜错即废、400 天不用即 410——本机这一格坏了也换不到会话；
   * ③ 用一次就轮换：reauth 成功立刻落下新值，旧值在服务端同时作废，本机不留副本。
   *
   * 存量口径与原生一致：升级到本版之前登录的设备这一格是空的，reauthSession 读到空
   * 就什么都不做，401 原样落登录页——发布说明那句"没重新登录过的设备暂时拿不到这个
   * 能力"同样适用于浏览器。
   */
  const DEVICE_SECRET_PREFIX = "fenver.deviceSecret.";

  function deviceVault() {
    // localStorage 会被隐私模式 / 配额 / 禁用第三方存储抛异常：读不到就当没记过，
    // 续期这件事永远不许把业务打停（与 noteSessionExpiry 同一个纪律）。
    try { return window.localStorage || null; } catch (_) { return null; }
  }

  function rememberDevice(userId, value) {
    const store = deviceVault();
    if (!store || !userId || !value) return false;
    try { store.setItem(DEVICE_SECRET_PREFIX + userId, value); return true; }
    catch (_) { return false; }
  }

  function recallDevice(userId) {
    const store = deviceVault();
    if (!store || !userId) return "";
    try { return store.getItem(DEVICE_SECRET_PREFIX + userId) || ""; }
    catch (_) { return ""; }
  }

  function forgetDevice(userId) {
    const store = deviceVault();
    if (!store || !userId) return;
    try { store.removeItem(DEVICE_SECRET_PREFIX + userId); } catch (_) {}
  }

  /* 阶梯第三层：会话过了绝对上限之后的那一次 401，用本机这一格换一段全新会话。
   * 判据顺序逐字对齐原生 SessionRenew.reauthOnce（本机身份 → 有没有这把 → 冷却窗 →
   * 是不是已经在敲），两边错开顺序就会出现"手机上能自愈、网页上不能"这类没法解释的差别。
   * 依赖全从 deps 进来、兜底写字面量：这份控制流要被单独取出来用 node 真跑。 */
  async function reauthSession(deps) {
    const cooldown = deps.cooldown === undefined ? 6 * 3600 : deps.cooldown;
    const now = deps.now || (() => Date.now() / 1000);
    if (!deps.userId) return "no-identity";
    if (!deps.secret) return "no-secret";          // 存量设备：不落登录页之外什么都不做
    const moment = now();
    if (moment - (deps.lastReauthAt || 0) < cooldown) return "cooling-down";
    if (deps.busy) return "already-running";       // 与 renew 共用一把闸：同一时刻只敲一次
    deps.setBusy(true);
    deps.setLastReauthAt(moment);
    try {
      const out = await deps.reauth(deps.secret);
      if (out && out.status === 410) { deps.forget(); return "gone"; }
      // 连不上服务端（fetch 抛错）≠ 凭据失效：这一格一个字都不许动，
      // 否则一次断网就把自愈能力弄没了，下次到点只能落登录页。
      if (!out || out.status < 200 || out.status > 299) return "failed";
      const token = (out.data && out.data.token) || "";
      if (!token) return "failed";                 // 没拿到新明文就不动现有 Cookie
      await deps.adopt(token);
      /* 单次使用：老那把在服务端已经作废，本机必须立刻落下新的。服务端成功时一定带
         新值；万一没带（老版本服务端 / 契约漂了），旧那把已经用掉，留着就是一把
         永远换不到会话的死钥匙——所以是 forget，不是"什么都不做"。 */
      const rotated = (out.data && out.data.device_secret) || "";
      if (rotated) deps.save(rotated); else deps.forget();
      return "reauthed";
    } catch (e) {
      return "unreachable";
    } finally {
      deps.setBusy(false);
    }
  }

  /* 真正的自愈动作：给 request() 用，返回 true 才允许把原来那一枪重打一次。
   * 走的是裸 fetch，不经 request()——不然 401 会顺着 request() 再敲一次自己，
   * 那就是这条端点限速账要抓的形状。 */
  function reauthOnce() {
    const userId = (currentIdentityId && currentIdentityId()) || "";
    return reauthSession({
      userId,
      secret: recallDevice(userId),
      busy: SESSION.renewing,
      lastReauthAt: SESSION.lastReauthAt,
      cooldown: RENEW_COOLDOWN_SECONDS,
      reauth: async (value) => {
        const res = await fetch("/v1/auth/reauth", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", ...csrfHeaders("POST") },
          body: JSON.stringify({ device_secret: value }),
        });
        const data = res.ok ? await res.json() : null;
        if (res.ok) noteSessionExpiry(res);
        return { status: res.status, data };
      },
      adopt,
      save: (value) => rememberDevice(userId, value),
      forget: () => forgetDevice(userId),
      setBusy: (v) => { SESSION.renewing = v; },
      setLastReauthAt: (t) => { SESSION.lastReauthAt = t; },
    }).catch(() => "unreachable");
  }

  /* 本机"现在是谁"的唯一读法：名册条目只是显示用的，但这一格按 userId 存，
   * 所以它得知道该读哪一格。app.js 的 currentEntry() 是同一份名册的另一半，
   * api.js 不 import 它（这个文件是独立 script），于是这里读同样的两个键。
   * 读法刻意保守：名册坏掉 / 没记过人 → 空串 → reauthSession 回 "no-identity"，
   * 与"存量设备没这把"落在同一条降级路径上。 */
  function currentIdentityId() {
    try {
      const list = JSON.parse(localStorage.getItem("identities") || "[]");
      const current = localStorage.getItem("currentId");
      const hit = Array.isArray(list) && list.find((x) => x && x.userId === current);
      return (hit || (Array.isArray(list) && list[list.length - 1]) || {}).userId || "";
    } catch (_) { return ""; }
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
    /* 退出在服务端会连带清掉这个人全部续命凭据（auth.revoke 那一段），本机这一格
       留着就是"一把永远换不到会话的死钥匙"。只在退当前会话时清：带 token 的那一形
       是替清单里的另一个人撤销，他的 userId 不在 currentIdentityId() 的答案里。 */
    if (!token) forgetDevice(currentIdentityId());
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

  /* 401 有两种含义，自愈只服务其中一种：
   * "会话过期"（业务请求）→ 拿续命凭据换一段新会话再重打；
   * "口令/答案不对"（下面这四条）→ 这就是正常的失败文案，绝不能顺手再签一发新会话。
   * 不排掉它们会同时踩两个坑：猜错一次密码 = 白得一个可用会话 + 失败账被记两格
   * （重打那一枪又戳一次），而限速预算本来就只有几次。原生 Api.kt 里的
   * SELF_HEAL_EXEMPT 是同一份名单，test_v0340_web_device_vault 逐字比两边。 */
  const HEAL_EXEMPT_PATHS = new Set(["/v1/auth/login", "/v1/auth/register",
    "/v1/auth/reset", "/v1/auth/change-password"]);

  async function request(path, { method = "GET", body, signal } = {}, healed = false) {
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
    if (res.status === 401 && !healed && !HEAL_EXEMPT_PATHS.has(path)) {
      /* 绝对上限到点的那一次 401：先试阶梯第三层，成了就把原来这一枪原样重打一次。
       * `healed` 是递归闸门——重打那一枪再 401 就照老路抛给 needsAuth 落登录页，
       * 一次失败最多两发请求，绝不在这条缝里循环。 */
      const outcome = await reauthOnce();
      if (outcome === "reauthed") return request(path, { method, body, signal }, true);
    }
    if (!res.ok) throw await parseError(res);
    NetMinder.ok();
    noteSessionExpiry(res);
    sessionTick();                       // 续期在后台发生，不占这一请求的时间
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
                     || type === "tool_result" || type === "search"
                     || type === "approval_request") {
            // v0.29.0 的过程帧与 v0.31 审批帧：上面那条 onEvent 缝已经把整帧交出去了，
            // 这里只是别让它们掉进下面的 else——unknown 那一笔的语义是「契约之外冒出
            // 的东西」，把已知帧记进去等于给上报加噪声。
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
      noteSessionExpiry(res);
      return res;
    };
    /* 这一轮流在跑的时候，续期一律不动手（判据 9）：renew 一成功，老 digest 就进
       60 秒排水窗，超过 60 秒还在用老令牌的流会被硬拒——而"回答正在一个字一个字
       往外冒"时把令牌换掉，症状就是输出当场断。计数在 finally 里减：正常收尾、
       报错、被停止都算流没了，被推迟的那一次续期在流尾补上。 */
    SESSION.streams += 1;
    let out;
    try {
      out = await runResilientStream({
        open, onChunk, onEvent: onEvent || (opts && opts.onEvent),
        onStatus: opts.onStatus, onRun: opts.onRun, onUnknown: opts.onUnknown,
        sleep: defaultSleep, maxAttempts: STREAM_RESUME_MAX_ATTEMPTS,
        resumeStatus: STREAM_RESUME_STATUS,
      });
    } finally {
      SESSION.streams -= 1;
      if (SESSION.streams < 0) SESSION.streams = 0;
      sessionTick();                     // 流尾补做被推迟的那一次续期
    }
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

    /* 续命凭据这一格的两个对外口：写只在"服务端刚发新值"那一刻（app.js 的 afterAuth
     * 覆盖注册、登录、找回后自动登录三条路），清在名册移除时。闸门本体仍在这个文件里，
     * app.js 拿不到 localStorage 的键名，也就绕不出第三条写路径。 */
    rememberDevice,
    forgetDevice,

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

    /* 搜索源密钥（v0.32 W2，管理员面）：明文只在这一个请求体里过一趟，
     * 任何响应都不回显它（后端只给「末四位」掩码）。普通用户拿到 403——
     * 这把 key 服务全站降级链，边界与上面 /v1/providers 管理员面同一条。
     */
    searchKeys: () => request("/v1/search-keys"),
    setSearchKey: (source, api_key) =>
      request("/v1/search-keys/" + encodeURIComponent(source), { method: "PUT", body: { api_key } }),
    deleteSearchKey: (source) =>
      request("/v1/search-keys/" + encodeURIComponent(source), { method: "DELETE" }),

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

    /* v0.31 审批层：一次性票据确认。decision 只接受 "allow" / "reject"。
     * 票据超时或已消费时服务端返回 404，UI 据此判定卡片失效（不重复弹）。 */
    confirmApproval: (ticketId, decision) =>
      request("/v1/approval/confirm", { method: "POST",
        body: { ticket_id: ticketId, decision: decision } }),
    getApprovalStatus: (ticketId) =>
      request("/v1/approval/" + encodeURIComponent(ticketId)),
  };
})();
