# backend/tests/test_v0340_web_device_vault.py
"""Web 端续命凭据的行为契约——v0.35.0 #143② 之后它住在 httpOnly Cookie 里。

历史：v0.34.0 判据 4 的 Web 半边把 device_secret 按 userId 存在 localStorage，
这份文件当时钉的是"一个前缀、三个出入口、只在服务端刚发新值时写"。2026-10-08 的
口径是"Web 也持久化续命凭据"；#143② 把这一格整个搬进 httpOnly + Secure +
SameSite=Strict Cookie（服务端 register/login/reauth 种、每次 reauth 轮换、
logout/改密/找回清），JS 从此读不到这把 400 天寿命的钥匙。文件名保留是因为阶梯
（reauthSession / request 的 401 接线）仍然住在这里；localStorage 那一格已经没了。

本文件钉五件事：

1. `reauthSession` 的决策表**真跑**（node）：冷却窗内不许敲、连不上服务端不算
   凭据失效、410 才回 gone、没拿到新明文就不动现有 Cookie。把
   `if (out.status === 410)` 改成 `>= 400`，文本上仍像一份谨慎的阶梯，症状却是
   一次断网/一次 401 就把自愈判死。
2. `request()` 的**接线**也只能真跑：401 到底重打了几发、登录那条 401 有没有被
   误当成会话过期，是行为问题，不是文本问题。
3. `reauthOnce` 的请求形状：Cookie-only（不把 device_secret 放进 body）、
   `credentials:"same-origin"`、带 X-CSRF、不带 Authorization、不读 localStorage。
4. 升级清理：页面加载时把老版本留下的 `fenver.deviceSecret.*` 全部删掉，坏存储
   不许把页面打停；除此之外 api.js 不再读/写这一格。
5. 与原生 `SessionRenew.reauthOnce` / `Api.kt` 的判据顺序与豁免名单继续对齐：
   两边错开就是"手机上能自愈、网页上不能"这类没法解释的差别。

语料一律走 test_web_pwa 那把剥注释的尺子。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.test_web_pwa import STATIC, _fn_text, _js    # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
API_KT = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / \
    "fenever" / "assistant" / "nativeapp" / "Api.kt"
SESSION_RENEW_KT = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / \
    "xyz" / "fenever" / "assistant" / "nativeapp" / "SessionRenew.kt"

COOLDOWN = 6 * 3600
# 剧本里的"现在"要离 lastReauthAt 足够远，否则测的是冷却而不是主干路径。
FAR_ENOUGH = 1_000_000


def _fn_window(src: str, name: str) -> str:
    """从 `function name(` 到**下一个同级函数声明**之前。

    request 的解构形参 `{ method = "GET", body, signal }` 里有花括号，按配对切的
    _fn_text() 在签名行就断了（renew ladder 那份文件踩过同一个坑，注释里写着）。
    """
    start = src.index(f"function {name}(")
    if src[:start].rstrip().endswith("async"):
        start = src.rindex("async", 0, start)      # async 关键字被切在外面就是语法错
    nxt = re.compile(r"\n  (?:async )?function ").search(src, start + 10)
    return src[start:nxt.start()] if nxt else src[start:]


def _ladder_source() -> str:
    """交给 node 的真实代码：reauthSession 本体 + request 本体 + 那份豁免名单。

    HEAL_EXEMPT_PATHS 也从仓库里取——如果名单只在测试里手写一份，那么把 request() 里
    `!HEAL_EXEMPT_PATHS.has(path)` 删掉，运行时剧本照样绿（它用的是测试那份名单）。
    """
    api = _js("api.js")
    m = re.search(r"const HEAL_EXEMPT_PATHS = new Set\(\[.*?\]\);", api, re.S)
    assert m, "api.js 里找不到那份豁免名单：名单改名了，运行时剧本与跨语言对比一起失效"
    return (m.group(0) + "\n" + _fn_text(api, "reauthSession") + "\n"
            + _fn_window(api, "request"))


def _run_js(scenario: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    d = Path(tempfile.mkdtemp(prefix="device-cookie-"))
    (d / "fns.js").write_text(_ladder_source(), encoding="utf-8")
    (d / "harness.cjs").write_text(_HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(d / "harness.cjs"), str(d / "fns.js"),
                        json.dumps(scenario)],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"harness 自己就跑失败了：\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout)


_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const realFns = fs.readFileSync(process.argv[2], "utf8");
const SC = JSON.parse(process.argv[3]);

const calls = [];
function fakeFetch(path, opts) {
  calls.push("fetch:" + path);
  const status = (SC.fetchSequence && SC.fetchSequence.length)
    ? SC.fetchSequence.shift() : (SC.status === undefined ? 200 : SC.status);
  if (SC.fetchThrows) return Promise.reject(new TypeError("连不上"));
  const ok = status >= 200 && status <= 299;
  return Promise.resolve({
    status, ok,
    headers: { get: () => (SC.expires === undefined ? null : String(SC.expires)) },
    json: () => Promise.resolve(ok ? (SC.body || { token: "T2" }) : { detail: "剧本里的失败" }),
  });
}

function makeReauthDeps() {
  return {
    busy: !!SC.busy,
    lastReauthAt: SC.lastReauthAt || 0,
    now: () => SC.now,
    cooldown: SC.cooldown,
    reauth: () => {
      calls.push("reauth");
      if (SC.reauthThrows) return Promise.reject(new TypeError("连不上服务端"));
      return Promise.resolve({
        status: SC.status === undefined ? 200 : SC.status,
        data: SC.data === undefined ? { token: "NEW-TOKEN", device_secret: "ROTATED" } : SC.data,
      });
    },
    adopt: (t) => { calls.push("adopt:" + t); return Promise.resolve({ ok: true }); },
    setBusy: (v) => { calls.push("setBusy:" + v); },
    setLastReauthAt: (t) => { calls.push("setLastReauthAt:" + t); },
  };
}

const sandbox = {
  calls, SC, makeReauthDeps, fakeFetch, Promise, Error, TypeError, JSON, console, module,
  // request() 的其它模块作用域邻居：这里一律给假叶子，真跑的是 request() 自己的控制流。
  authHeaders: () => ({}),
  csrfHeaders: () => ({}),
  NetMinder: { note: () => { calls.push("netminder"); }, ok: () => {} },
  noteSessionExpiry: () => { calls.push("noteExpiry"); },
  sessionTick: () => { calls.push("sessionTick"); },
  parseError: async (res) => { const e = new Error("HTTP " + res.status); e.status = res.status; return e; },
  reauthOnce: () => { calls.push("reauthOnce"); return Promise.resolve(SC.healOutcome || "reauthed"); },
  fetch: fakeFetch,
};
vm.createContext(sandbox);
vm.runInContext(realFns + "\n;module.exports = { reauthSession, request };", sandbox);

(async () => {
  try {
    if (SC.entry === "request") {
      try {
        const data = await sandbox.module.exports.request(
          SC.path, { method: SC.method || "GET", body: SC.body }, !!SC.healed);
        process.stdout.write(JSON.stringify({ calls, returned: data }));
      } catch (e) {
        process.stdout.write(JSON.stringify({ calls, threw: e.status === undefined ? String(e.message) : e.status }));
      }
    } else {
      const outcome = await sandbox.module.exports.reauthSession(makeReauthDeps());
      process.stdout.write(JSON.stringify({ outcome, calls }));
    }
  } catch (e) {
    process.stdout.write(JSON.stringify({ harness_error: String(e) }));
  }
})();
"""


# ---------- 决策表：什么时候不许敲那条端点 ----------


def test_the_cooldown_window_blocks_a_second_attempt_on_both_ladders():
    """一次失败不许追着敲：这条端点自己有限速账，自杀式重试只会把人钉在 429 上。"""
    out = _run_js({"now": 1000, "lastReauthAt": 900})
    assert out["outcome"] == "cooling-down"
    assert "reauth" not in out["calls"]


def test_the_ladder_never_runs_two_credential_calls_at_once():
    """与 renew 共用一把闸：正在敲的时候第二次进来直接回绝。"""
    out = _run_js({"now": FAR_ENOUGH, "busy": True})
    assert out["outcome"] == "already-running"
    assert "reauth" not in out["calls"]


def test_the_cooldown_is_judged_before_the_busy_gate():
    """判据顺序钉死：冷却 → 是否在敲（身份与本地 secret 两道判断已随 Cookie 化退场）。

    两条闸同时成立时，回绝的理由必须是冷却——顺序漂了以后"为什么没自愈"这件事在
    两端会给出不同答案，而那正是跨端行为没法解释的开始。
    """
    out = _run_js({"now": 1000, "lastReauthAt": 900, "busy": True})
    assert out["outcome"] == "cooling-down", "顺序漂了：先问的是冷却，不是忙不忙"


def test_a_successful_reauth_adopts_the_token_and_touches_no_local_storage():
    """主干路径：一发 reauth → 明文交给 adopt（唯一签发门）。

    v0.35.0 之后没有"落下轮换值"这一步：新 device_secret 由服务端在同一次响应里
    Set-Cookie，JS 读不到也不需要读。这里钉的是"客户端不再有第二个存储动作"。
    """
    out = _run_js({"now": FAR_ENOUGH})
    assert out["outcome"] == "reauthed"
    assert out["calls"].count("reauth") == 1, "一次自愈最多敲一发"
    assert "adopt:NEW-TOKEN" in out["calls"], "新令牌没走 adopt：页面重新持有明文或根本没换会话"
    assert not [c for c in out["calls"] if c.startswith("save:") or c in ("forget",)], \
        "客户端还在本地存取续命凭据：它现在只该住在 httpOnly Cookie 里"
    assert "setBusy:false" in out["calls"], "闸没放下：整个阶梯从此永远回 already-running"


def test_a_gone_secret_is_dropped_and_nothing_is_adopted():
    """410 = 服务端判这把死了（400 天没用 / 已被轮换掉）：不发会话，Cookie 由响应清。"""
    out = _run_js({"now": FAR_ENOUGH, "status": 410, "data": {"detail": "gone"}})
    assert out["outcome"] == "gone"
    assert not [c for c in out["calls"] if c.startswith("adopt:")], "没签发令牌就不该收编"


def test_a_rejected_secret_does_not_trigger_a_second_attempt():
    """401 只说"这次不认"：本次阶梯到此为止，下一次由冷却窗与下一发 401 决定。

    变异：`status === 410` 写成 `status >= 400` —— 一次猜错或一次服务端抖动就被当成
    "钥匙已死"，判据 4 的"到点自己爬起来"当场失效。
    """
    out = _run_js({"now": FAR_ENOUGH, "status": 401, "data": {"detail": "no"}})
    assert out["outcome"] == "failed"
    assert not [c for c in out["calls"] if c.startswith("adopt:")]


def test_a_network_failure_never_destroys_the_self_heal_path():
    """连不上服务端 ≠ 凭据失效：这一句是原生注释里的原话，Web 半边同样成立。"""
    out = _run_js({"now": FAR_ENOUGH, "reauthThrows": True})
    assert out["outcome"] == "unreachable"
    assert "setBusy:false" in out["calls"], "抛错路径也要放闸"


def test_a_response_without_a_token_changes_nothing():
    """没拿到新明文就不动现有 Cookie：半途而废的收编比不动更糟。"""
    out = _run_js({"now": FAR_ENOUGH, "data": {"device_secret": "ROTATED"}})
    assert out["outcome"] == "failed"
    assert not [c for c in out["calls"] if c.startswith("adopt:")]


def test_a_token_only_response_is_enough_because_the_cookie_rotates_server_side():
    """轮换值走 Set-Cookie，不走响应体：只有 token 也算成功。"""
    out = _run_js({"now": FAR_ENOUGH, "data": {"token": "NEW-TOKEN"}})
    assert out["outcome"] == "reauthed"
    assert "adopt:NEW-TOKEN" in out["calls"]


# ---------- request() 的接线：401 到底重打几发 ----------


def test_an_expired_session_reauthenticates_and_resends_the_original_call_once():
    """判据 4 的用户可见结果：到点这一次 401，用户不该被踢到登录页。"""
    out = _run_js({"entry": "request", "path": "/v1/sessions",
                    "fetchSequence": [401, 200], "healOutcome": "reauthed"})
    assert out["returned"] == {"token": "T2"}, "重打那一枪的结果没交回调用点"
    assert out["calls"].count("fetch:/v1/sessions") == 2, "原来那一枪没重打，或打了不止一次"
    assert out["calls"].count("reauthOnce") == 1, "一次失败最多一次自愈"


def test_a_failed_self_heal_still_throws_the_original_401():
    """自愈不成 = 老行为：401 原样抛给 needsAuth 落登录页，一层都不多演。"""
    out = _run_js({"entry": "request", "path": "/v1/sessions", "status": 401,
                    "healOutcome": "no-secret"})
    assert out["threw"] == 401
    assert out["calls"].count("fetch:/v1/sessions") == 1, "自愈没成还重打：白戳一发"


def test_the_resend_does_not_self_heal_again():
    """递归闸门：重打那一枪再 401 就照老路抛出去，绝不在这条缝里循环。"""
    out = _run_js({"entry": "request", "path": "/v1/sessions", "healed": True,
                    "status": 401})
    assert out["threw"] == 401
    assert "reauthOnce" not in out["calls"], "重打的那一发又来自愈：这条端点会被自己打进限速"


def test_a_wrong_password_is_not_mistaken_for_an_expired_session():
    """登录/注册/找回/改密的 401 意思是"口令不对"。误判的代价是两条：
    猜错一次密码反而白得一个可用会话；重打那一枪把登录失败账记了两格。"""
    for path in ["/v1/auth/login", "/v1/auth/register", "/v1/auth/reset",
                 "/v1/auth/change-password"]:
        out = _run_js({"entry": "request", "path": path, "status": 401,
                       "method": "POST", "body": {"username": "u", "password": "p"}})
        assert out["threw"] == 401
        assert "reauthOnce" not in out["calls"], f"{path} 的 401 被当成会话过期去自愈了"
        assert out["calls"].count(f"fetch:{path}") == 1, f"{path} 失败后被重打"


def test_only_a_401_triggers_the_ladder():
    """403/429/500 与"会话过期"无关：拿它们敲 reauth 是往限速账里送人头。"""
    for status in (403, 429, 500):
        out = _run_js({"entry": "request", "path": "/v1/sessions", "status": status})
        assert out["threw"] == status
        assert "reauthOnce" not in out["calls"]


def test_an_authorized_response_never_touches_the_ladder():
    out = _run_js({"entry": "request", "path": "/v1/sessions", "status": 200})
    assert "reauthOnce" not in out["calls"]
    assert "sessionTick" in out["calls"], "第二层（到期续期）的接线不在成功路径上"


# ---------- 跨端一致性：与原生同一份名单、同一条顺序 ----------


def _kotlin_set(text: str, name: str) -> set:
    m = re.search(rf"{name} = setOf\((.*?)\)", text, re.S)
    assert m, f"Api.kt 里找不到 {name}：改名了就把跨端一致性这条锁一起废了"
    return set(re.findall(r'"(/v1/auth/[^"]+)"', m.group(1)))


def _js_set(text: str, name: str) -> set:
    m = re.search(rf"const {name} = new Set\(\[(.*?)\]\);", text, re.S)
    assert m, f"api.js 里找不到 {name}"
    return set(re.findall(r'"(/v1/auth/[^"]+)"', m.group(1)))


def test_the_web_and_the_native_exempt_the_same_four_endpoints():
    """两份名单必须逐字相同，否则"手机上被拦、网页上没拦"没人能解释。"""
    web = _js_set(_js("api.js"), "HEAL_EXEMPT_PATHS")
    native = _kotlin_set(API_KT.read_text(encoding="utf-8"), "SELF_HEAL_EXEMPT")
    assert web == native == {"/v1/auth/login", "/v1/auth/register",
                             "/v1/auth/reset", "/v1/auth/change-password"}


def test_the_native_ladder_still_routes_the_exempt_paths_around_self_heal():
    """原生半边确实把名单接进了判据里——只在 Web 加等于两边不一致。"""
    body = API_KT.read_text(encoding="utf-8")
    line = re.search(r"if \(err is ApiException.*?\{", body, re.S)
    assert line and "path !in SELF_HEAL_EXEMPT" in line.group(0), \
        "Api.kt 的 401 自愈没看名单：猜错口令在原生上会白得一个会话"


def _once_source() -> str:
    """交给 node 的真实代码：reauthOnce + reauthSession + 共享闸那两行。

    单独跑 reauthOnce 才问得出"它用的是哪把闸"——决策表剧本里 busy 是测试喂进来的，
    把源码改成 `busy: false` 那种接法只有从这里才看得出来。v0.35.0 之后这份源码里
    不该再有 localStorage 或 device_secret。
    """
    api = _js("api.js")
    m = re.search(r"const SESSION = \{[^}]*\};", api)
    assert m, "api.js 里 SESSION 那一行形状变了：阶梯的状态不再只有一处"
    ruler = re.search(r"const RENEW_COOLDOWN_SECONDS = [^;]+;", api)
    assert ruler, "冷却窗的尺子不是一行了：接线剧本与跨端对比一起失效"
    return (m.group(0) + "\n" + ruler.group(0) + "\n"
            + _fn_text(api, "reauthSession") + "\n"
            + _fn_window(api, "reauthOnce"))


def _run_once(scenario: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    d = Path(tempfile.mkdtemp(prefix="device-cookie-once-"))
    (d / "fns.js").write_text(_once_source(), encoding="utf-8")
    (d / "harness.cjs").write_text(_HARNESS_ONCE, encoding="utf-8")
    r = subprocess.run([node, str(d / "harness.cjs"), str(d / "fns.js"),
                        json.dumps(scenario)],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"harness 自己就跑失败了：\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout)


_HARNESS_ONCE = r"""
const fs = require("fs"), vm = require("vm");
const realFns = fs.readFileSync(process.argv[2], "utf8");
const SC = JSON.parse(process.argv[3]);
const calls = [];
const requests = [];
const store = new Map(Object.entries(SC.store || {}));
let reads = 0;
const fakeStorage = {
  get length() { return store.size; },
  key: (i) => (Array.from(store.keys())[i] === undefined ? null : Array.from(store.keys())[i]),
  getItem: (k) => { reads += 1; return store.has(k) ? store.get(k) : null; },
  setItem: (k, v) => { store.set(k, String(v)); },
  removeItem: (k) => { store.delete(k); },
};
const sandbox = {
  calls, SC, console, module, Promise, Error, JSON, Date,
  localStorage: fakeStorage,
  window: { localStorage: fakeStorage },
  csrfHeaders: (method) => ({ "X-CSRF": "1" }),
  noteSessionExpiry: () => calls.push("noteExpiry"),
  adopt: (t) => { calls.push("adopt:" + t); return Promise.resolve({ ok: true }); },
  fetch: (path, opts) => {
    calls.push("fetch:" + path);
    requests.push({ path, opts: opts || {} });
    const st = SC.status === undefined ? 200 : SC.status;
    return Promise.resolve({
      status: st, ok: st >= 200 && st <= 299,
      headers: { get: () => null },
      json: () => Promise.resolve({ token: "NEW", device_secret: "ROT" }),
    });
  },
};
vm.createContext(sandbox);
vm.runInContext(realFns
  + "\n;if (SC.renewing) SESSION.renewing = true;"
  + "\n;if (SC.lastReauthNow) SESSION.lastReauthAt = Date.now() / 1000;"
  + "\n;if (SC.lastReauthAt !== undefined) SESSION.lastReauthAt = SC.lastReauthAt;"
  + "\n;module.exports = reauthOnce;", sandbox);
Promise.resolve(sandbox.module.exports())
  .then((outcome) => process.stdout.write(JSON.stringify({
    outcome, calls, requests, reads, stored: Object.fromEntries(store) })))
  .catch((e) => process.stdout.write(JSON.stringify({ harness_error: String(e) })));
"""


def test_the_two_ladder_layers_really_share_one_gate():
    """第二层正在敲的时候，第三层必须原地等：两条腿同敲同一条凭据面就是自相残杀。

    这条只能真跑：把源码里的 `busy: SESSION.renewing` 改成 `busy: false`，文本上仍然像
    "用了那把闸"（setBusy 那一句还在），运行时却谁也不拦谁。
    """
    out = _run_once({"renewing": True})
    assert out["outcome"] == "already-running"
    assert not [c for c in out["calls"] if c.startswith("fetch:")], "闸没接上：同敲了凭据端点"


def test_reauth_once_is_cookie_only_and_declares_csrf():
    """真接线：空 body + 同源 Cookie + X-CSRF，不读 localStorage，不带会话凭据。"""
    out = _run_once({})
    assert out["outcome"] == "reauthed"
    assert len(out["requests"]) == 1
    req = out["requests"][0]
    assert req["path"] == "/v1/auth/reauth"
    assert req["opts"].get("method") == "POST"
    assert req["opts"].get("credentials") == "same-origin"
    assert req["opts"].get("headers", {}).get("X-CSRF") == "1"
    assert "body" not in req["opts"], "还在往请求体里放 device_secret：明文回到了 JS"
    assert "Authorization" not in req["opts"].get("headers", {}), \
        "reauth 自己带了请求头凭据：受众隔离被前端绕过"
    assert out["reads"] == 0, "reauth 还在读 localStorage：凭据来源只该是 httpOnly Cookie"
    assert out["stored"] == {}, "reauth 往 localStorage 写了东西"


def test_reauth_once_uses_the_returned_token_through_adopt():
    out = _run_once({})
    assert "adopt:NEW" in out["calls"], "轮换后的令牌没有走唯一那道签发门"


def test_reauth_once_works_without_any_local_identity_roster():
    """存量/新浏览器都可能没有名册条目；Cookie 在不在只有服务端知道，客户端不预判。"""
    out = _run_once({"store": {}})
    assert out["outcome"] == "reauthed"
    assert len(out["requests"]) == 1


def test_reauth_once_keeps_the_cookie_when_the_server_rejects_it():
    """401 只说"这次不认"：客户端没有本机副本可删，Cookie 由服务端在 410 时清。"""
    out = _run_once({"status": 401})
    assert out["outcome"] == "failed"
    assert not [c for c in out["calls"] if c.startswith("adopt:")]


def test_reauth_once_reports_gone_when_the_secret_is_dead():
    out = _run_once({"status": 410})
    assert out["outcome"] == "gone"
    assert not [c for c in out["calls"] if c.startswith("adopt:")]


def test_the_cooldown_gate_is_wired_to_the_real_state_too():
    """接线半边：冷却窗读的是 SESSION.lastReauthAt，不是恒零的局部变量。"""
    out = _run_once({})
    assert out["calls"].count("fetch:/v1/auth/reauth") == 1, "第一次到点该敲，且只敲一发"
    seconds = _run_once({"lastReauthNow": True})
    assert seconds["outcome"] == "cooling-down", "冷却状态没接进来：每次 401 都会再敲一发"
    assert not [c for c in seconds["calls"] if c.startswith("fetch:")]


def test_both_ladders_share_one_renewal_gate_and_one_cooldown_ruler():
    """Web 的 RENEW_COOLDOWN_SECONDS 与原生 COOLDOWN_MS 必须是同一个数（六小时）。"""
    api = _js("api.js")
    assert "const RENEW_COOLDOWN_SECONDS = 6 * 3600;" in api
    kt = SESSION_RENEW_KT.read_text(encoding="utf-8")
    assert "COOLDOWN_MS = 6L * 3600 * 1000" in kt
    assert re.search(r"busy:\s*SESSION\.renewing", _fn_window(api, "reauthOnce")), \
        "reauth 没用 renew 那把闸：两条腿可以同时对同一条端点开火"


# ---------- 升级清理：老版本留在 localStorage 的那一格 ----------


def _legacy_source() -> str:
    api = _js("api.js")
    m = re.search(r"const LEGACY_DEVICE_SECRET_PREFIX = [^;]+;", api)
    assert m, "api.js 里找不到存量清理的前缀：升级后那批明文凭据会永远留在设备上"
    return m.group(0) + "\n" + _fn_text(api, "purgeLegacyDeviceSecrets")


def _run_purge(scenario: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    d = Path(tempfile.mkdtemp(prefix="device-purge-"))
    (d / "fns.js").write_text(_legacy_source(), encoding="utf-8")
    (d / "harness.cjs").write_text(_HARNESS_PURGE, encoding="utf-8")
    r = subprocess.run([node, str(d / "harness.cjs"), str(d / "fns.js"),
                        json.dumps(scenario)],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"harness 自己就跑失败了：\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout)


_HARNESS_PURGE = r"""
const fs = require("fs"), vm = require("vm");
const realFns = fs.readFileSync(process.argv[2], "utf8");
const SC = JSON.parse(process.argv[3]);
const store = new Map(Object.entries(SC.store || {}));
const fakeStorage = {
  get length() { return store.size; },
  key: (i) => (Array.from(store.keys())[i] === undefined ? null : Array.from(store.keys())[i]),
  removeItem: (k) => { store.delete(k); },
};
const sandbox = { console, module, Map, Array, JSON, String };
if (SC.broken) {
  sandbox.window = {};
  Object.defineProperty(sandbox.window, "localStorage", {
    get() { throw new Error("storage denied"); },
  });
} else {
  sandbox.window = { localStorage: fakeStorage };
}
vm.createContext(sandbox);
vm.runInContext(realFns + "\n;module.exports = purgeLegacyDeviceSecrets;", sandbox);
let removed;
try { removed = sandbox.module.exports(); }
catch (e) { process.stdout.write(JSON.stringify({ harness_error: String(e) })); process.exit(0); }
process.stdout.write(JSON.stringify({ removed, keys: Array.from(store.keys()) }));
"""


def test_legacy_device_secret_entries_are_purged_and_nothing_else_is():
    """升级清理只删 `fenver.deviceSecret.*`：名册、指针、断网静音一个都不许动。"""
    out = _run_purge({"store": {
        "fenver.deviceSecret.u1": "SEC-1",
        "fenver.deviceSecret.u2": "SEC-2",
        "identities": "[]",
        "currentId": "u1",
        "netminder_mute": "2026-10-20",
    }})
    assert out["removed"] == 2
    assert sorted(out["keys"]) == ["currentId", "identities", "netminder_mute"]


def test_a_broken_storage_does_not_break_boot():
    """隐私模式 / 禁用存储：清理失败返回 0，页面照常启动。"""
    out = _run_purge({"broken": True})
    assert out["removed"] == 0
    assert "harness_error" not in out


# ---------- 形状：Web 端不再有第二处凭据存储面 ----------


def test_the_old_vault_entry_points_are_gone():
    api = _js("api.js")
    # 用带声明关键字的形状，别用裸名字：`LEGACY_DEVICE_SECRET_PREFIX` 里就含
    # `DEVICE_SECRET_PREFIX` 这一串，裸子串匹配会把存量清理本身判成复活。
    for gone in ("const DEVICE_SECRET_PREFIX", "function deviceVault",
                 "function rememberDevice", "function recallDevice",
                 "function forgetDevice", "function currentIdentityId"):
        assert gone not in api, f"api.js 里还留着 {gone}：localStorage 那一格没有真正退役"
    assert api.count('"fenver.deviceSecret."') == 1, \
        "存量前缀出现了不止一处：清理之外的读/写路径又长出来了"


def test_the_app_no_longer_writes_or_clears_a_local_secret():
    app = _js("app.js")
    assert "API.rememberDevice" not in app and "API.forgetDevice" not in app
    assert "device_secret" not in app and "fenver.deviceSecret" not in app, \
        "app.js 还在搬运续命凭据：它现在只该由服务端 Set-Cookie 收编"


def test_the_reauth_call_carries_no_session_credential_and_no_body_secret():
    """那条端点只认 Cookie（原生/老 web 的 body 回落由服务端保留，Web 不用）。"""
    body = _fn_window(_js("api.js"), "reauthOnce")
    assert '"/v1/auth/reauth"' in body and 'credentials: "same-origin"' in body
    assert "Authorization" not in body, "reauth 自己带了请求头凭据：受众隔离被前端绕过"
    assert "csrfHeaders(\"POST\")" in body, "reauth 没带 CSRF：Cookie 会话下这发会被 403"
    assert "body:" not in body and "device_secret" not in body, \
        "Web 又把续命凭据放回请求体：它应当只在 httpOnly Cookie 里"


def test_logout_has_no_local_secret_housekeeping_left():
    body = _fn_text(_js("api.js"), "logout")
    assert "localStorage" not in body and "forgetDevice" not in body, \
        "退出还在清本机那一格：它已经不存在了，清 Cookie 是服务端响应的事"


def test_the_front_end_still_never_writes_a_cookie_by_hand():
    """Cookie 只由服务端 Set-Cookie 种：前端不碰 document.cookie。"""
    for name in ("api.js", "app.js"):
        assert "document.cookie" not in _js(name), f"{name} 手搓 Cookie：签发门被绕过"
    assert "Set-Cookie" not in _js("api.js")


def test_the_identity_roster_still_holds_nothing_but_names():
    """名册里只有"这台机器认识谁"，没有凭什么——Cookie 化之后这条边界没松。"""
    app = _js("app.js")
    add = _fn_text(app, "addIdentity")
    for forbidden in ("token", "password", "secret", "device"):
        assert forbidden not in add.lower(), f"名册里出现了 {forbidden}：方案 C 的存储边界又开了"
    assert "fenver.deviceSecret" not in app, "app.js 自己拼存量键名：清理只在 api.js 一处"


def test_the_renew_ladder_body_itself_still_touches_no_storage():
    """第二层（Cookie 续期）的口径没被这次改动带跑：它仍然只在内存里做判断。"""
    api = _js("api.js")
    for name in ("renewSession", "noteSessionExpiry"):
        assert "localStorage" not in _fn_text(api, name)
    # 存储面只剩断网静音的 setItem 与存量清理的 removeItem，别处一个都不许多。
    assert api.count("deviceVault()") == 0, "存储面又长出了出入口函数"
    assert (STATIC / "api.js").exists()
