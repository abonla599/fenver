# backend/tests/test_v0340_web_device_vault.py
"""判据 4 的 Web 半边：续命凭据这一格怎么存、什么时候用、用坏了怎么收场。

2026-10-08 用户把口径从"Web 只用 Cookie 续期"扩成"Web 也持久化续命凭据"，所以这一格
是本版唯一新增的本地存储面。文件钉四件事：

1. `reauthSession` 的决策表**真跑**（node）。理由和 renewSession 那条一模一样：
   "冷却窗内不许敲""连不上服务端不许删凭据""410 才删"这三句，读源码永远读不出对错——
   把 `if (out.status === 410)` 改成 `if (out.status >= 400)`，文本上仍然像一份谨慎的
   阶梯，症状却是断网一次就把自愈能力弄没，下次到点只能落登录页。
2. `request()` 的**接线**也只能真跑：401 到底重打了几发、登录那条 401 有没有被误当成
   会话过期，是行为问题，不是文本问题。
3. 这一格的**形状**做结构锁：一个前缀、三个出入口、写只在"服务端刚发新值"那三处，
   名册仍然零凭据。存储面一旦多开一个口，"退出/换号就清干净"这句就没人负责了。
4. 与原生 `SessionRenew.reauthOnce` / `Api.kt` 的**判据顺序与豁免名单**逐字对齐：
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
    d = Path(tempfile.mkdtemp(prefix="device-vault-"))
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
    userId: SC.userId === undefined ? "u_x" : SC.userId,
    secret: SC.secret === undefined ? "SEC" : SC.secret,
    busy: !!SC.busy,
    lastReauthAt: SC.lastReauthAt || 0,
    now: () => SC.now,
    cooldown: SC.cooldown,
    reauth: (value) => {
      calls.push("reauth:" + value);
      if (SC.reauthThrows) return Promise.reject(new TypeError("连不上服务端"));
      return Promise.resolve({
        status: SC.status === undefined ? 200 : SC.status,
        data: SC.data === undefined ? { token: "NEW-TOKEN", device_secret: "ROTATED" } : SC.data,
      });
    },
    adopt: (t) => { calls.push("adopt:" + t); return Promise.resolve({ ok: true }); },
    save: (v) => { calls.push("save:" + v); },
    forget: () => { calls.push("forget"); },
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


def test_no_local_identity_means_no_reauth_request():
    """没认出"现在是谁"就无处可查这一格：一个请求都不该发。

    变异：删掉 `if (!deps.userId) return "no-identity"` —— 下一句用空 userId 读到空串，
    表面上也回 "no-secret"，但这一格将来按用户名以外的键存时就会在这里静默错位。
    """
    out = _run_js({"userId": "", "now": FAR_ENOUGH})
    assert out["outcome"] == "no-identity"
    assert not [c for c in out["calls"] if c.startswith("reauth:")], "没身份还去敲 reauth"


def test_a_device_that_never_logged_in_since_upgrade_does_nothing():
    """存量口径与原生一致：这一格是空的就什么都不做，401 原样落登录页。"""
    out = _run_js({"secret": "", "now": FAR_ENOUGH})
    assert out["outcome"] == "no-secret"
    assert not [c for c in out["calls"] if c.startswith("reauth:")]
    assert "forget" not in out["calls"], "空格的清理也算副作用：不该有任何写入"


def test_the_cooldown_window_blocks_a_second_attempt_on_both_ladders():
    """一次失败不许追着敲：这条端点自己有限速账，自杀式重试只会把人钉在 429 上。"""
    out = _run_js({"now": 1000, "lastReauthAt": 900})
    assert out["outcome"] == "cooling-down"
    assert not [c for c in out["calls"] if c.startswith("reauth:")]


def test_the_ladder_never_runs_two_credential_calls_at_once():
    """与 renew 共用一把闸：正在敲的时候第二次进来直接回绝。"""
    out = _run_js({"now": FAR_ENOUGH, "busy": True})
    assert out["outcome"] == "already-running"
    assert not [c for c in out["calls"] if c.startswith("reauth:")]


def test_the_cooldown_is_judged_before_the_busy_gate():
    """判据顺序钉死成原生 reauthOnce 那一条：身份 → 有没有这把 → 冷却 → 是否在敲。

    两条闸同时成立时，回绝的理由必须是冷却——顺序漂了以后"为什么没自愈"这件事在两边
    会给出不同答案，而那正是跨端行为没法解释的开始。
    """
    out = _run_js({"now": 1000, "lastReauthAt": 900, "busy": True})
    assert out["outcome"] == "cooling-down", "顺序漂了：先问的是冷却，不是忙不忙"


def test_a_successful_reauth_adopts_the_token_and_lands_the_rotated_secret():
    """主干路径：一发 reauth → 明文交给 adopt（唯一签发门）→ 立刻落下轮换后的新值。"""
    out = _run_js({"now": FAR_ENOUGH})
    assert out["outcome"] == "reauthed"
    assert out["calls"].count("reauth:SEC") == 1, "一次自愈最多敲一发"
    assert "adopt:NEW-TOKEN" in out["calls"], "新令牌没走 adopt：页面重新持有明文或根本没换会话"
    assert "save:ROTATED" in out["calls"], "单次使用的旧那把已作废，本机必须落下新值"
    assert "forget" not in out["calls"]
    assert "setBusy:false" in out["calls"], "闸没放下：整个阶梯从此永远回 already-running"


def test_a_gone_secret_is_dropped_and_nothing_is_adopted():
    """410 = 服务端判这把死了（400 天没用 / 已被轮换掉）：本机跟着删，不发会话。"""
    out = _run_js({"now": FAR_ENOUGH, "status": 410, "data": {"detail": "gone"}})
    assert out["outcome"] == "gone"
    assert "forget" in out["calls"]
    assert not [c for c in out["calls"] if c.startswith("save:")], "死钥匙不许被当成新钥匙留下"
    assert not [c for c in out["calls"] if c.startswith("adopt:")], "没签发令牌就不该收编"


def test_a_rejected_secret_is_kept_because_the_next_login_will_mint_a_fresh_one():
    """401 与 410 分开：401 只说"这次不认"，删掉它就断掉了唯一一条免口令自愈路。

    变异：`status === 410` 写成 `status >= 400` —— 一次猜错或一次服务端抖动就把格子清空，
    判据 4 的"到点自己爬起来"当场失效，用户只能重新输密码。
    """
    out = _run_js({"now": FAR_ENOUGH, "status": 401, "data": {"detail": "no"}})
    assert out["outcome"] == "failed"
    assert "forget" not in out["calls"], "401 就删凭据：把最后一次自愈能力扔了"
    assert not [c for c in out["calls"] if c.startswith("save:")]


def test_a_network_failure_never_destroys_the_stored_secret():
    """连不上服务端 ≠ 凭据失效：这一句是原生注释里的原话，Web 半边同样成立。"""
    out = _run_js({"now": FAR_ENOUGH, "reauthThrows": True})
    assert out["outcome"] == "unreachable"
    assert "forget" not in out["calls"], "断网就删钥匙：下次到点只能落登录页"
    assert "setBusy:false" in out["calls"], "抛错路径也要放闸"


def test_a_response_without_a_token_changes_nothing():
    """没拿到新明文就不动现有 Cookie：半途而废的收编比不动更糟。"""
    out = _run_js({"now": FAR_ENOUGH, "data": {"device_secret": "ROTATED"}})
    assert out["outcome"] == "failed"
    assert not [c for c in out["calls"] if c.startswith("adopt:")]
    assert not [c for c in out["calls"] if c.startswith("save:")]
    assert "forget" not in out["calls"]


def test_a_rotated_value_the_server_forgot_to_send_clears_the_spent_one():
    """服务端成功却没带新那把：旧那把已经被这次用掉，留着就是死钥匙，删。"""
    out = _run_js({"now": FAR_ENOUGH, "data": {"token": "NEW-TOKEN"}})
    assert out["outcome"] == "reauthed"
    assert "adopt:NEW-TOKEN" in out["calls"]
    assert "forget" in out["calls"] and not [c for c in out["calls"] if c.startswith("save:")]


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
    """交给 node 的真实代码：那一格存储的四个出入口 + reauthOnce 自己 + SESSION 那一行。

    单独跑 reauthOnce 才问得出"它用的是哪把闸"——决策表剧本里 busy 是测试喂进来的，
    把源码改成 `busy: false` 那种接法只有从这里才看得出来。
    """
    api = _js("api.js")
    m = re.search(r"const SESSION = \{[^}]*\};", api)
    assert m, "api.js 里 SESSION 那一行形状变了：阶梯的状态不再只有一处"
    ruler = re.search(r"const RENEW_COOLDOWN_SECONDS = [^;]+;", api)
    assert ruler, "冷却窗的尺子不是一行了：接线剧本与跨端对比一起失效"
    prefix = re.search(r"const DEVICE_SECRET_PREFIX = [^;]+;", api)
    assert prefix, "那一格的键名不是一行了：剧本里的键会和设备上的键悄悄分家"
    return (m.group(0) + "\n" + ruler.group(0) + "\n" + prefix.group(0) + "\n"
            + _fn_text(api, "deviceVault") + "\n"
            + _fn_text(api, "rememberDevice") + "\n"
            + _fn_text(api, "recallDevice") + "\n"
            + _fn_text(api, "forgetDevice") + "\n"
            + _fn_text(api, "currentIdentityId") + "\n"
            + _fn_text(api, "reauthSession") + "\n"
            + _fn_window(api, "reauthOnce"))


def _run_once(scenario: dict) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    d = Path(tempfile.mkdtemp(prefix="device-vault-once-"))
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
const store = new Map();
if (SC.stored) store.set("fenver.deviceSecret.u_x", SC.stored);
// api.js 里两种写法都有：存储面走 window.localStorage，名册走裸 localStorage。
// 两个名字必须指向同一块假存储，否则剧本绿在"读不到"上。
const fakeStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
const sandbox = {
  calls, SC, console, module, Promise, Error, JSON, Date,
  localStorage: fakeStorage,
  window: { localStorage: fakeStorage },
  csrfHeaders: () => ({}),
  noteSessionExpiry: () => {},
  adopt: (t) => { calls.push("adopt:" + t); return Promise.resolve({ ok: true }); },
  fetch: (path) => {
    calls.push("fetch:" + path);
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
  + "\n;localStorage.setItem('currentId','u_x');"
  + "\n;localStorage.setItem('identities', JSON.stringify([{userId:'u_x'}]));"
  + "\n;if (SC.renewing) SESSION.renewing = true;"
  + "\n;if (SC.lastReauthNow) SESSION.lastReauthAt = Date.now() / 1000;"
  + "\n;if (SC.lastReauthAt !== undefined) SESSION.lastReauthAt = SC.lastReauthAt;"
  + "\n;module.exports = reauthOnce;", sandbox);
Promise.resolve(sandbox.module.exports())
  .then((outcome) => process.stdout.write(JSON.stringify({
    outcome, calls, vault: store.get("fenver.deviceSecret.u_x") || null })))
  .catch((e) => process.stdout.write(JSON.stringify({ harness_error: String(e) })));
"""


def test_the_two_ladder_layers_really_share_one_gate():
    """第二层正在敲的时候，第三层必须原地等：两条腿同敲同一条凭据面就是自相残杀。

    这条只能真跑：把源码里的 `busy: SESSION.renewing` 改成 `busy: false`，文本上仍然像
    "用了那把闸"（setBusy 那一句还在），运行时却谁也不拦谁。
    """
    out = _run_once({"stored": "SEC", "renewing": True})
    assert out["outcome"] == "already-running"
    assert not [c for c in out["calls"] if c.startswith("fetch:")], "闸没接上：同敲了凭据端点"
    assert out["vault"] == "SEC", "被闸拦下也顺手改了那一格"


def test_reauth_once_reads_the_current_identitys_own_slot():
    """真接线：从名册读出"现在是谁"，再按那个人那一格取钥匙；成功后落下轮换值。"""
    out = _run_once({"stored": "SEC"})
    assert out["outcome"] == "reauthed"
    assert "fetch:/v1/auth/reauth" in out["calls"]
    assert out["vault"] == "ROT", "单次使用的旧钥匙没被换掉：下一次 401 无凭可用"
    assert "adopt:NEW" in out["calls"]


def test_reauth_once_does_nothing_when_this_device_never_logged_in():
    """名册里没这个人这一格 = 存量设备：不发请求，也不动别的条目。"""
    out = _run_once({})
    assert out["outcome"] == "no-secret"
    assert not [c for c in out["calls"] if c.startswith("fetch:")]


def test_reauth_once_keeps_the_secret_when_the_server_rejects_it():
    """401 只说"这次不认"：这一格不许被抹掉，否则免口令自愈当场失效。"""
    out = _run_once({"stored": "SEC", "status": 401})
    assert out["outcome"] == "failed"
    assert out["vault"] == "SEC"


def test_reauth_once_clears_the_slot_when_the_secret_is_dead():
    out = _run_once({"stored": "SEC", "status": 410})
    assert out["outcome"] == "gone"
    assert out["vault"] is None, "服务端判死了这一把，本机还留着"


def test_the_cooldown_gate_is_wired_to_the_real_state_too():
    """接线半边：冷却窗读的是 SESSION.lastReauthAt，不是恒零的局部变量。"""
    out = _run_once({"stored": "SEC"})
    assert out["calls"].count("fetch:/v1/auth/reauth") == 1, "第一次到点该敲，且只敲一发"
    seconds = _run_once({"stored": "SEC", "lastReauthNow": True})
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


# ---------- 这一格的形状：一个前缀、三个出入口、写只在服务端刚发新值时 ----------


def test_the_vault_has_exactly_one_key_prefix_and_one_write_door():
    api = _js("api.js")
    assert api.count("const DEVICE_SECRET_PREFIX") == 1, "键名前缀不止一份：清干净这句话没人负责"
    assert api.count("DEVICE_SECRET_PREFIX + userId") == 3, \
        "这一格的出入口应该是写/读/清三处，多一处就有人绕过闸门"
    assert api.count("function rememberDevice(") == 1
    assert api.count("function recallDevice(") == 1
    assert api.count("function forgetDevice(") == 1


def test_the_vault_never_writes_an_empty_value_and_survives_a_broken_storage():
    """隐私模式 / 配额满 / 禁用存储：读不到就当没记过，续期不许把业务打停。"""
    body = _fn_text(_js("api.js"), "rememberDevice")
    assert "!value" in body, "空值也往格子里写：读出来是一把永远用不了的死钥匙"
    for name in ("rememberDevice", "recallDevice", "forgetDevice", "deviceVault"):
        assert "catch" in _fn_text(_js("api.js"), name), f"{name} 没兜异常：存储坏了界面就坏了"


def test_the_secret_is_landed_at_the_moment_the_server_mints_one():
    """写只有三个时机：注册、登录、reauth 轮换。少一个就有设备拿不到自愈能力。

    注册与登录在界面上共用 afterAuth 这一个收口（找回后的自动登录也走它），所以写入点
    钉在它身上；把它拆回 api.js 的每个封装里，漏一条路就没人报警。
    """
    app = _js("app.js")
    after = _fn_text(app, "afterAuth")
    assert "API.rememberDevice(res.user_id, res.device_secret);" in after, \
        "登录后没落这一格：判据 4 在这条路上从第一步就断了"
    assert after.index("addIdentity(res)") < after.index("API.rememberDevice("), \
        "先写钥匙后记名字：currentId 还没指对人，下一次 401 找不到自己那一格"
    api = _js("api.js")
    assert "rememberDevice(res" not in api, "写入点散回 api.js 的每个封装：漏一条路没人报"


def test_only_afterAuth_and_the_ladder_ever_write_the_vault():
    """整条链路上写这一格的地方就两处：afterAuth（服务端刚发）与 reauth 轮换。"""
    api = _js("api.js")
    app = _js("app.js")
    assert api.count("rememberDevice(") == 2, \
        "api.js 里带括号的 rememberDevice 只该有两处：函数定义本身 + reauth 轮换那一句落值"
    assert app.count("API.rememberDevice(") == 1, "写入点不止一处：名册与保险柜的边界会重新糊掉"


def test_logout_clears_the_current_devices_secret_because_the_server_revokes_all_of_them():
    """auth.revoke 在服务端清掉这个人全部 secrets；本机不留一把死钥匙。"""
    body = _fn_text(_js("api.js"), "logout")
    assert "if (!token) forgetDevice(currentIdentityId());" in body, \
        "退出没清这一格（或替别人撤销时误清了当前那位的）"
    app = _js("app.js")
    assert "API.forgetDevice(userId);" in _fn_text(app, "dropIdentity"), \
        "名册里移除一个人却留着他的那一格：忘掉名字不等于忘掉凭据"


def test_the_reauth_call_carries_no_session_credential():
    """那条端点只认 device_secret（判据 5 的反向半边）：这里塞 Authorization 就是混淆受众。"""
    body = _fn_window(_js("api.js"), "reauthOnce")
    assert '"/v1/auth/reauth"' in body and 'credentials: "same-origin"' in body
    assert "Authorization" not in body, "reauth 自己带了请求头凭据：受众隔离被前端绕过"
    assert "csrfHeaders(\"POST\")" in body, "reauth 没带 CSRF：Cookie 会话下这发会被 403"


def test_the_front_end_still_never_writes_a_cookie_by_hand():
    """这一格是 localStorage，不是 document.cookie：签发门仍然只有 adopt 那一道。"""
    for name in ("api.js", "app.js"):
        assert "document.cookie" not in _js(name), f"{name} 手搓 Cookie：唯一那道 Set-Cookie 门被绕过"
    assert "Set-Cookie" not in _js("api.js")


def test_the_identity_roster_still_holds_nothing_but_names():
    """新增这一格不等于把凭据塞回名册：名册与保险柜是两回事，键名也不许在两侧各写一遍。"""
    app = _js("app.js")
    add = _fn_text(app, "addIdentity")
    for forbidden in ("token", "password", "secret", "device"):
        assert forbidden not in add.lower(), f"名册里出现了 {forbidden}：方案 C 的存储边界又开了"
    assert "fenver.deviceSecret" not in app and "DEVICE_SECRET_PREFIX" not in app, \
        "app.js 自己拼键名：绕过唯一那道写门，退出时就不可能清干净"


def test_the_renew_ladder_body_itself_still_touches_no_storage():
    """第二层（Cookie 续期）的口径没被这次改动带跑：它仍然只在内存里做判断。"""
    api = _js("api.js")
    for name in ("renewSession", "noteSessionExpiry"):
        assert "localStorage" not in _fn_text(api, name)
    # 存储面集中在那三个出入口 + 一个读身份的小函数里，别处一个都不许多。
    assert api.count("deviceVault()") == 4, \
        "存储面不止这四个出入口（写/读/清 + deviceVault 自己）：又多开了一道门"
    assert (STATIC / "api.js").exists()
