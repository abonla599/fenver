# backend/tests/test_v0340_web_renew_ladder.py
"""判据 9 与 11 的 Web 半边：续期阶梯真跑一遍，口令一个字节都不许落本地。

这份文件钉的是**浏览器这一侧的行为**，不是服务端的（那在 test_v0340_session_renew.py）：

1. `renewSession` 的决策表必须真跑（node），因为判据 9 说的是"什么时候**不许**动手"。
   读源码看不出"有流在跑时到底发没发 renew"——`if (deps.streamBusy) return "deferred"`
   这一行删掉，文本上仍然是一份像样的阶梯，症状却是回答正在往外冒的时候令牌被换掉、
   输出当场断。这一类"形状合规、行为致命"的缺陷只有运行时能抓。
2. 阶梯的**接线**只能做结构锁：`request()` 与流尾各挂一次，`SESSION.streams` 的增减
   必须夹住 `runResilientStream` 且写在 `finally` 里（报错与被停止也是"流没了"）。
3. 触发口径来自服务端的头，客户端不许自己拿本机时钟猜到期——时钟偏一点就能把
   "还剩 8 天"算成"还剩 6 天"，而这是唯一一条只有服务端知道答案的问题。
4. 判据 11（负向）：本地存储里搜不到口令，也不许出现"加密保存的口令"。

语料一律走 test_web_pwa 那把剥注释的尺子：注释里提到"password""令牌"这类词不算行为。
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.test_web_pwa import STATIC, _fn_text, _js    # noqa: E402

# 与 api.js 里那两行同源；写在这里是为了让"判据的方向"在本文件里也能被断言。
THRESHOLD = 7 * 24 * 3600
# 剧本里的"现在"要离 lastRenewAt 足够远，否则测的是冷却而不是主干路径。
FAR_ENOUGH = 1_000_000


def _fn_window(src: str, name: str) -> str:
    """从 `function name(` 到**下一个同级函数声明**之前。

    不用 test_web_pwa 的 _fn_text：`request(path, { method = "GET", body, signal })`
    的解构形参里带花括号，那把按配对切的尺子在签名行就断了（实测它返回的就是那一行）。
    这里要的只是"这一段的正文里有没有那两句"，切到同级下一个函数就够。
    """
    start = src.index(f"function {name}(")
    nxt = re.compile(r"\n  (?:async )?function ").search(src, start + 10)
    return src[start:nxt.start()] if nxt else src[start:]


def _run_ladder_js(scenario: dict) -> dict:
    """在 node 里真跑仓库那一份 renewSession（api.js），deps 全是假的但会记账。

    与 _run_stream_js 同理：交给 node 的函数文本经 _js() 剥注释，读文件的是 node 不是
    本函数；被执行的**控制流**逐字取自仓库。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    src = _fn_text(_js("api.js"), "renewSession")
    d = Path(tempfile.mkdtemp(prefix="renew-ladder-"))
    (d / "fns.js").write_text(src, encoding="utf-8")
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

// renewSession 只依赖 deps 里进来的东西：这组假叶子记录每一次调用，返回值由剧本给。
const calls = [];
function makeDeps() {
  return {
    remaining: SC.remaining === undefined ? null : SC.remaining,
    streamBusy: !!SC.streamBusy,
    renewing: !!SC.renewing,
    lastRenewAt: SC.lastRenewAt || 0,
    now: () => SC.now,
    threshold: SC.threshold,
    cooldown: SC.cooldown,
    renew: () => {
      calls.push("renew");
      if (SC.renewThrows) return Promise.reject(new Error("剧本里 renew 就是失败的"));
      if (SC.renewNoToken) return Promise.resolve({ status: "x" });
      return Promise.resolve({ token: "NEW-TOKEN", expires_at: 1, absolute_deadline: 2 });
    },
    adopt: (t) => {
      calls.push("adopt:" + t);
      if (SC.adoptThrows) return Promise.reject(new Error("剧本里 adopt 就是失败的"));
      return Promise.resolve({ ok: true });
    },
    setRenewing: (v) => { calls.push("setRenewing:" + v); },
    setLastRenewAt: (t) => { calls.push("setLastRenewAt:" + t); },
  };
}

const sandbox = { calls, SC, makeDeps, Promise, Error, Date, console, module };
vm.createContext(sandbox);
vm.runInContext(realFns + "\n;module.exports = renewSession;", sandbox);
Promise.resolve(sandbox.module.exports(makeDeps()))
  .then((outcome) => process.stdout.write(JSON.stringify({ outcome, calls })))
  .catch((e) => { process.stdout.write(JSON.stringify({ harness_error: String(e) })); });
"""


# ---------- 判据 1 / 9：什么时候动手，什么时候必须不动 ----------


def test_plenty_of_time_left_means_no_renew_request_at_all():
    """判据 1 的客户端半边：剩余还多于一周，一个 renew 请求都不该发出去。

    变异：删掉 `if (deps.remaining > threshold) return "not-due"` —— renew 会变成
    每个请求都敲一次，用户开着页面就是在替服务端做心跳，而那正是限流账要抓的形状。
    """
    out = _run_ladder_js({"remaining": THRESHOLD + 3600, "now": 1000, "lastRenewAt": 0})
    assert out["outcome"] == "not-due"
    assert "renew" not in out["calls"], "剩余还多就发了 renew：续期被做成了心跳"


def test_renew_is_deferred_while_a_stream_is_in_flight():
    """判据 9 的正身：流在跑就绝不动手。这条是本版唯一一条浏览器自动化锁。

    为什么这条必须是真跑而不是读源码：`SESSION.streams > 0` 传对了、分支写反了（把
    deferred 写成放行），文本上两处都还在，只有运行时能看出 renew 到底发没发。
    症状是"用户正在看输出，输出断了"——那是比掉线更难复述的一类故障。
    """
    out = _run_ladder_js({"remaining": 60, "streamBusy": True, "now": 1000, "lastRenewAt": 0})
    assert out["outcome"] == "deferred"
    assert "renew" not in out["calls"], "有流在跑时发了 renew：在途 SSE 会被令牌轮换打断"


def test_due_and_idle_renews_then_adopts_the_new_token():
    """到点且空闲：renew 之后必须把新明文交给 adopt 收编成 Cookie。

    少了 adopt 这一步等于白续：Cookie 里还是老的那一枚，60 秒后它硬，用户掉线。
    变异：把 `await deps.adopt(data.token)` 删掉，这条红。
    """
    out = _run_ladder_js({"remaining": 60, "streamBusy": False, "now": FAR_ENOUGH,
                          "lastRenewAt": 0})
    assert out["outcome"] == "renewed"
    assert "renew" in out["calls"]
    assert "adopt:NEW-TOKEN" in out["calls"], "续到的新令牌没被 adopt：Cookie 还是老的那一枚"


def test_renew_failure_changes_nothing():
    """PRD 第四节 Web 那一行：renew 失败**什么都不做**，老令牌还能用到绝对上限。

    不许有的行为：重试风暴（追着敲限速）、清 Cookie、弹登录页——那三样都会把"续期
    没成功"这件无害的事变成"用户被踢出去"。变异：adopt 被塞进 catch 里、或失败后
    立刻再 renew，这条红。
    """
    out = _run_ladder_js({"remaining": 60, "now": FAR_ENOUGH, "lastRenewAt": 0,
                          "renewThrows": True})
    assert out["outcome"] == "failed"
    assert [c for c in out["calls"] if c.startswith("adopt")] == [], \
        "renew 失败还去 adopt：拿一个不存在的新令牌动了现有 Cookie"
    assert out["calls"].count("renew") == 1, "renew 失败当场重试：限速器不吃自杀式重试"


def test_renew_response_without_token_does_not_touch_the_cookie():
    """没拿到明文就当没发生：现有 Cookie 一个字都不动。"""
    out = _run_ladder_js({"remaining": 60, "now": FAR_ENOUGH, "lastRenewAt": 0,
                          "renewNoToken": True})
    assert out["outcome"] == "failed"
    assert [c for c in out["calls"] if c.startswith("adopt")] == []


def test_no_header_means_the_client_guesses_nothing():
    """没有 X-Token-Expires-In（未鉴权、bootstrap、老服务端）时不猜到期。

    猜的办法一定是"拿本机时钟 + 一个天数"，而那条路把判据交给了客户端的表；
    这里要求的是**什么都不做**。
    """
    out = _run_ladder_js({"remaining": None, "now": 1000, "lastRenewAt": 0})
    assert out["outcome"] == "no-header"
    assert "renew" not in out["calls"]


def test_a_renew_already_in_flight_is_not_duplicated():
    """两个请求几乎同时到点：只许发一次 renew（第二个是并发竞态的复现）。"""
    out = _run_ladder_js({"remaining": 60, "renewing": True, "now": 1000, "lastRenewAt": 0})
    assert out["outcome"] == "already-running"
    assert "renew" not in out["calls"]


def test_failures_are_throttled_not_retried_immediately():
    """刚敲过一次（无论成败）就退到冷却窗外：判据 1 的另一半是"不许成心跳"。"""
    out = _run_ladder_js({"remaining": 60, "now": 1000, "lastRenewAt": 999, "cooldown": 3600})
    assert out["outcome"] == "cooling-down"
    assert "renew" not in out["calls"]


def test_the_boundary_is_exactly_the_servers_threshold():
    """阈值是"剩余 > 7 天才不续"，边界值 7 天当天应当动手——服务端 renew 在那一刻也放行。

    两边方向对不上时，客户端与服务端各自以为安全的区间正好错开一天，症状是
    "页面明明去续了却拿到 401"。
    """
    ok = _run_ladder_js({"remaining": THRESHOLD, "now": FAR_ENOUGH, "lastRenewAt": 0})
    assert ok["outcome"] == "renewed", "阈值方向写反：边界那天客户端不动，服务端却已 ready"
    no = _run_ladder_js({"remaining": THRESHOLD + 1, "now": FAR_ENOUGH, "lastRenewAt": 0})
    assert no["outcome"] == "not-due"


# ---------- 接线：阶梯真的挂在请求与流尾上 ----------


def test_session_tick_is_wired_into_plain_requests_and_stream_ends():
    """每个 2xx 之后都要有机会续期，而**流的生命周期**必须夹住续期判定。

    三条各钉一处：`request()` 里读头 + tick；`streamChat` 里进入时 `streams += 1`、
    退出写在 `finally`（报错与被停止也算退出）、退出后补一次 tick。
    少了 finally 那一半，一次流报错就把 `streams` 永远留在 >0，判据 9 反过来变成
    "永不续期"——那是比打断更糟的一种沉默。
    """
    api = _js("api.js")
    req = _fn_window(api, "request")
    assert "noteSessionExpiry(res)" in req, "普通请求没读头条：阶梯永远不会知道还剩多久"
    assert "sessionTick()" in req, "普通请求没挂 tick：到点了也没人去续"

    stream = _fn_window(api, "streamChat")
    start = stream.index("SESSION.streams += 1")
    kernel = stream.index("runResilientStream(")
    end = stream.index("SESSION.streams -= 1")
    assert start < kernel < end, "streams 计数没夹住那轮流：判据 9 说的「忙」是个假信号"
    assert "finally" in stream[kernel:end], \
        "streams 的减号不在 finally 里：流一报错就永不续期"
    assert "sessionTick()" in stream[end:], "流尾没补做被推迟的那一次续期"


def test_expiry_comes_from_the_server_header_and_nowhere_else():
    """到期只从响应头来：`SESSION.remaining` 的写入点必须只有一处，且在读头那个函数里。

    变异：在别处再写一次 `SESSION.remaining = Date.now()/1000 + 30*86400`，这条红——
    那等于客户端自己造了一份第二真相，而它的判据是服务端给的。
    """
    api = _js("api.js")
    assert api.count("SESSION.remaining =") == 1, \
        "SESSION.remaining 有多个写入点：到期口径不再只有一份出处"
    note = _fn_text(api, "noteSessionExpiry")
    assert "SESSION.remaining =" in note, "唯一的写入点不在读头的函数里"
    assert 'headers.get(TOKEN_EXPIRES_HEADER)' in note, "写入点没从那个头取值"
    assert "res.ok" in note, "非 2xx 的头条也被采信：401 的头说的不是还剩多久"


def test_the_renew_request_carries_the_same_credentials_as_every_post():
    """renew 打的是同源带 Cookie 的 POST，并且带 X-CSRF：Cookie 认证的不安全方法第二道闸。

    少 CSRF 头这一句在现网会被中间件 403 挡死（凭据出自 Cookie 才检查），所以这条
    同时是"renew 到底能不能用"的行为锁，不只是风格锁。
    """
    tick = _fn_text(_js("api.js"), "sessionTick")
    assert '"/v1/auth/renew"' in tick, "阶梯打的不是那条新端点"
    assert 'method: "POST"' in tick and 'csrfHeaders("POST")' in tick, \
        "renew 没带 CSRF 头：Cookie 会话下它会被 403"
    assert 'credentials: "same-origin"' in tick, "renew 没钉死同源带 Cookie"
    assert "Authorization" not in tick, \
        "renew 自己塞了请求头凭据：页面重新持有明文，方案 C 的墓碑白写了"


def test_only_adopt_still_sets_the_cookie_on_the_web_side():
    """续期在 Web 侧不许变成第二个种 Cookie 的地方：它只把明文交给 adopt。

    与 test_cookie_auth.py 那条服务端锁配成一对——那边钉"全服务端只有 /v1/auth/adopt
    发 Set-Cookie"，这边钉"客户端没有绕过 adopt 去手搓 Cookie"（document.cookie）。
    """
    api = _js("api.js")
    app = _js("app.js")
    assert "document.cookie" not in api and "document.cookie" not in app, \
        "前端直接写 document.cookie：绕过了唯一那扇签发门"
    renew = _fn_text(api, "renewSession")
    assert "deps.adopt(" in renew, "renew 的明文没走 adopt：新令牌落在别处"


# ---------- 判据 11 的 Web 半边：本地存储搜不到口令 ----------


@pytest.mark.parametrize("name", ["app.js", "api.js"])
def test_no_password_or_secret_is_persisted_locally(name):
    """负向锁：localStorage / sessionStorage 里不许出现口令，也不许出现"加密保存的口令"。

    判据 11 的原话是"任何一层都不许把口令写进本地存储，**也不许"加密保存"**"——所以
    这里盯的是键名与写入值两侧的形状，而不是"有没有 btoa"。引入"记住密码"这类明文键
    正是这条要红的形状。

    2026-10-08 用户改口径之后，本版有一处**声明过的例外**：device_secret 那一格
    （续命凭据，不是口令）。例外只开到那一行为止——整份文件里带 DEVICE_SECRET_PREFIX
    的写入必须恰好一处，多一处就回到"凭据随便往本地存储塞"那条老路上；那一格自己的
    生命周期（谁能写、什么时候清、用一次就轮换）由 test_v0340_web_device_vault.py 钉。
    """
    src = _js(name)
    vault_writes = [l for l in src.splitlines()
                    if "setItem" in l and "DEVICE_SECRET_PREFIX" in l]
    assert len(vault_writes) == (1 if name == "api.js" else 0), \
        f"{name} 里那处声明过的例外变了：续命凭据的写门应当只有 api.js 那一扇"
    # 词边界而不是子串：`contextTokensK`（上下文预算，一个数字）里就含 "token"，
    # 用子串匹配的锁第一跑就红在这个不相干的键上——而一条"永远红"或"永远绿"的锁
    # 都会在下一个 PR 被当噪声拆掉。
    credential_word = re.compile(r"(?i)\b(password|passwd|pwd|passphrase|secret"
                                 r"|device_secret|token|credential|authorization)\b")
    for line in src.splitlines():
        if "setItem" not in line and "sessionStorage" not in line:
            continue
        if "DEVICE_SECRET_PREFIX" in line:
            continue                     # 上面已经数过这一处例外，且只许这一处
        assert not credential_word.search(line), \
            f"本地存储写入里出现了凭据字样：{line.strip()}"
    # 会话令牌的明文只该活过一次 adopt；老版本留在 localStorage 的那个键，今天仍然
    # 不许被写回去（app.js 里那次 removeItem 是清理历史，方向相反，不在射程内）。
    assert 'setItem("accessToken"' not in src and "setItem('accessToken'" not in src, \
        "会话令牌又被写进 localStorage：方案 C 的存储边界被重新打开"
    assert "rememberPassword" not in src and "remember_password" not in src, \
        "引入了「记住密码」：判据 11 点名要红的那个形状"


def test_the_identity_roster_still_carries_no_credential():
    """人名册里只有"这台机器认识谁"，没有凭什么。

    addIdentity 收的是登录响应；这里钉它落盘的字段名里没有 token / password / secret，
    并且 adopt 之后那句"明文到此为止"仍然成立（局部变量，不进存储）。
    """
    app = _js("app.js")
    add = _fn_text(app, "addIdentity")
    for forbidden in ("token", "password", "secret"):
        assert forbidden not in add.lower(), \
            f"addIdentity 往名册里写了 {forbidden}：名册成了凭据存储"


def test_static_assets_ship_the_ladder_without_new_storage_apis():
    """第二层（Cookie 续期）自己不碰本地存储：它的输入是服务端的头，状态只在内存里。

    变异：把 SESSION 改成 localStorage 持久化（"刷新页面就不续了"的"修法"），这条红。
    本版新增的那一格续命凭据是**第三层**的存储面，不在这条的射程内——它由
    test_v0340_web_device_vault.py 按"一个前缀、三个出入口、用一次就轮换"单独钉。
    """
    api = _js("api.js")
    body = _fn_text(api, "renewSession") + _fn_text(api, "sessionTick") \
        + _fn_text(api, "noteSessionExpiry")
    assert "localStorage" not in body and "sessionStorage" not in body, \
        "续期阶梯自己开了本地存储面：PRD 说的「现状不变」被实现改了"
    assert (STATIC / "api.js").exists()
