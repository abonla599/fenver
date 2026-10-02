"""v0.25 安卓（Kotlin 壳）泳道：断线不烧钱的行为契约（T2.4 / T2.5 / T2.8）。

后端在 task/v025-r3b-no-reader-billing 把协议定稿了，网页在
task/v025-web-resume-20261001 把同一件事做完了：每帧带 `id: <run_id>:<seq>`，
续播走 `Last-Event-ID`，续不上回 410，"停止"有 POST /v1/chat/stream/{run_id}/cancel，
被停的流以旧解析器认得的 done 收尾（停的信息进 done 的新字段 status/stopped_reason/
resumable）。**这一节钉的是安卓这一侧必须跟上同一套协议**，而不是停在老行为。

老 Api.kt 的 parseFrame 只认 start/content/done/error，收到看不懂的新帧 `else -> null`
静默丢；老 ChatUi.kt 的 streamInto 在流非正常结束时 `val retryable = ...`，随即
`Api.chat(payload)` 整段重发回 /v1/chat——用户断线或退后台就是**再付一次钱、再落一条
一样的助手消息**。用户的原始诉求就一句：「不要让用户关闭页面也烧钱」。

本机没有 Android SDK（gradle/ANDROID_HOME 都缺），跑不了 :app:testDebugUnitTest，
所以这里全是"读源文本"的形状锁（先例见 test_netminder_contract.py、test_android_shell.py
的 _strip_java_comments），钉的是"客户端必须走的这条路"而非运行流本身；真正的控制流
（退避逐次变大、410 转取历史、cancelled/cannot_resume 等随后的 done）编译与实机由
build-native-apk.yml 的 assembleDebug 兜编译、其余仍需真机。

判据都对着当前 Kotlin 是红的：实现把它们拆掉/补上之后才该绿。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / \
    "fenever" / "assistant" / "nativeapp"
API_KT = UI / "Api.kt"
CHATUI_KT = UI / "ui" / "ChatUi.kt"
STATIC = REPO_ROOT / "backend" / "app" / "web" / "static"

# 双端逐字对齐的三句人话——唯一出处在网页端，安卓不许改写一个字。
STREAM_RESUME_PHRASE = "连接断了，正在接着上次的进度取回…"
TO_HISTORY_PHRASE = "这一轮接不上了，正在去会话里取回结果…"
STOPPED_PHRASE = "已停止生成"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_kotlin_comments(src: str) -> str:
    """去掉 // 与 /* */ 注释，但【尊重字符串字面量】（照 test_android_shell.py 那把尺子）。

    拿剥完的源码去判"某个符号还在不在"才不会被注释里解释旧行为的那句话骗到：
    把一行注掉不等于把那处调用删了，反之在注释里写一句"这里不再 Api.chat"也不算数。
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"' or c == "'":
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _kt_code(p: Path) -> str:
    return _strip_kotlin_comments(_read(p))


# ---------- T2.4：流非正常结束绝不整段重发 ----------

def test_native_stream_never_resends_a_whole_turn_when_it_breaks():
    """断线不重跑这一轮：ChatUi 不许再打非流式的 Api.chat 重发，也不许再有 retryable 那对判据。

    反向锁，方向钉死——老代码的两个出血点是 (1) `val retryable = ...`，
    (2) `if (retryable) Api.chat(payload)` 整段 POST 回 /v1/chat。这两处今天还在，
    所以这条现在是红的；留着任何一个，用户关页面就是一次重复计费 + 一条重复入库的助手消息。
    """
    chat = _kt_code(CHATUI_KT)
    assert "Api.chat(" not in chat, \
        "ChatUi 还在整段重发 /v1/chat：这正是断线烧钱的那一手（应改为续播→取历史）"
    assert "retryable" not in chat, \
        "ChatUi 还在给非正常结束的流打 retryable：上层据此重发整轮"


# ---------- 帧解析：接住新帧、不改老帧名、未知帧不静默丢 ----------

def test_native_sse_parser_recognises_the_new_frames_without_renaming_the_old_ones():
    """解析器要认 cancelled / cannot_resume 两帧，又不许把老的 start/content/done/error 换名。

    后端契约（test_v025_stream_cancel_contract 钉过）说旧事件名不能动——两个客户端都按
    data.type 分发。同时钉新帧被显式接住：cancelled/cannot_resume 不能像老代码那样被当
    未知丢掉；未知帧还要有一个明确出口（ChatEvent.Unknown），不再 `else -> null` 静默吞。
    """
    api = _kt_code(API_KT)
    for old in ('"start"', '"content"', '"done"', '"error"'):
        assert old in api, f"解析器丢了旧帧 {old}：改了名字会先打断线上"
    assert '"cancelled"' in api, "cancelled 帧没被接住：会被当未知丢掉"
    assert '"cannot_resume"' in api, "cannot_resume 帧没被接住：断线之后不知道不能再续"
    # 未知帧不静默丢：要么有 ChatEvent.Unknown 这一型，界面/内核都看得见它
    assert "ChatEvent.Unknown" in api, "解析器没有未知帧的出口：任何新帧都会像老代码那样被静默丢弃"


def test_cancelled_and_cannot_resume_are_not_terminal_frames_in_the_native_kernel():
    """cancelled / cannot_resume 都不是终止帧，必须等随后那条 done 收尾（与网页同一语义）。

    钉两件事：① ChatEvent 里确有 Cancelled / CannotResume 两个非终态事件型；
    ② Done 带 status/stopped_reason/resumable 新字段——停/续不上的信息全在这条终帧里，
    旧骨架（full_text/message_id/model）一字不改，新字段只是"加"。
    """
    api = _kt_code(API_KT)
    assert "ChatEvent.Cancelled" in api, "cancelled 没有对应的非终态事件型"
    assert "ChatEvent.CannotResume" in api, "cannot_resume 没有对应的非终态事件型"
    assert re.search(r"data class Done\([^)]*\bstatus\b[^)]*\)", api), \
        "ChatEvent.Done 没带 status 字段：分不清 completed/cancelled/cannot_resume"
    assert "stopped_reason" in api or "stoppedReason" in api, \
        "done 帧的新字段 stopped_reason 没解析"
    assert "resumable" in api, "done 帧的新字段 resumable 没解析"


# ---------- T2.4/T2.8：续播是带 Last-Event-ID 重开流端点，不是重发 ----------

def test_native_reconnect_reopens_the_stream_with_last_event_id():
    """续播是带游标重开同一条流，不是重发原文：Last-Event-ID = <run_id>:<seq>。

    续播请求仍然 POST /v1/chat/stream（带 Last-Event-ID 头），服务端据此只补缓冲帧、
    不叫模型不记账。钉三件：头名对、游标由已收到的最后一个 id 拼出、重开打的是流端点。
    """
    api = _kt_code(API_KT)
    assert "Last-Event-ID" in api, "续播没带 Last-Event-ID 头"
    assert 'id:' in api or '"id:"' in api or "startsWith(\"id:\")" in api, \
        "解析器没从 id: 行取游标，续播的 seq 无从累计"
    assert "$runId:" in api or "runId" in api and ":" in api, \
        "续播游标不是 <run_id>:<seq> 这个形状"


def test_native_stream_open_still_posts_the_payload_body():
    """openStream 必须把 payload 以 POST 体发出去——少一步 .post()，OkHttp 就默认 GET。

    现网实伤（2026-10-02，v0.25.0 壳）：续播改造把开流抽成 openStream 时只拼了 URL 和
    Last-Event-ID 头，忘了挂 body，OkHttp 对无方法的 Builder 默认发 GET /v1/chat/stream；
    该路由只收 POST，FastAPI 回 405 "Method Not Allowed"，用户每一轮对话当场失败。
    网页端 open() 是 method:"POST" + body，续播也照发 body（服务端凭 Last-Event-ID 头
    识别续播，只补缓冲帧、不叫模型）。上一条测试只钉了头名，没钉方法——这条把方法钉上。
    """
    api = _kt_code(API_KT)
    m = re.search(r"fun openStream\([^)]*\)[^{]*\{(.*?)\n    \}", api, re.S)
    assert m, "找不到 openStream：开流实现搬家了，这条锁要跟着搬，不许直接删"
    body = m.group(1)
    assert re.search(r"\.post\(\s*payload\.toRequestBody", body), \
        "openStream 没把 payload POST 出去——OkHttp 会默认 GET，打 POST-only 路由必 405"


def test_native_reconnect_backoff_and_cap_match_the_web_to_the_number():
    """退避 + 次数上限与网页逐值对齐：上限 5、起始 500ms、指数倍增；上限不是省钱旋钮。

    宽限期长短与花销由服务端的轮次边界钱闸决定，和这里试几次无关——上限只是"别无限期
    敲一条已经续不上的门"，到点转取历史。所以安卓不许自造一个更小的上限或"为省钱提前放弃"。
    """
    api = _kt_code(API_KT)
    m_kt = re.search(r"STREAM_RESUME_MAX_ATTEMPTS\s*=\s*(\d+)", api)
    assert m_kt, "安卓没有 STREAM_RESUME_MAX_ATTEMPTS 常量：续播上限没被钉死"
    m_js = re.search(r"STREAM_RESUME_MAX_ATTEMPTS\s*=\s*(\d+)", _read(STATIC / "api.js"))
    assert m_js, "网页 api.js 的 STREAM_RESUME_MAX_ATTEMPTS 读不到了（对照失效）"
    assert m_kt.group(1) == m_js.group(1) == "5", \
        f"续播上限双端不一致或不是 5：android={m_kt.group(1)} web={m_js.group(1)}"
    assert "500" in api, "安卓续播退避起始不是 500ms（与网页 BACKOFF_BASE_MS 对齐）"


# ---------- 410 / 不可续播 → 去会话历史取回，而不是回头重发 ----------

def test_native_sends_a_broken_or_unresumable_stream_to_history_not_a_rebill():
    """410 / 续不上 / 试到上限：转「去会话里取回」，绝不重发生成。

    内核把这三条都归一成"需要取历史"的信号（对应网页 err.needHistory），界面据此拉
    会话历史接口取回这一轮已落库的最终答案并渲染——取回走 Api.getSession，不 POST 回
    /v1/chat、不再叫一次上游。
    """
    api = _kt_code(API_KT)
    chat = _kt_code(CHATUI_KT)
    assert "410" in api, "安卓内核没把服务端 410 当成不可续播"
    assert "StreamNeedHistoryException" in api, "410/续不上/到上限没有转「取历史」的信号"
    assert "StreamNeedHistoryException" in chat, "界面没接住取历史信号：还会走回头重发那条老路"
    assert re.search(r"Api\.getSession\(", chat), "取历史没走会话历史接口（Api.getSession）"


# ---------- T2.5：停止打服务端 cancel，本地取消只是兜底 ----------

def test_native_stop_button_asks_the_server_to_cancel_this_run():
    """停止是显式动作：先打 POST /v1/chat/stream/{run_id}/cancel，本地 cancel 只是兜底。

    run_id 必须从流里拿到并记住（onRun 落到 currentRunId），否则 cancel 这一枪找不到目标。
    次序钉死：服务端取消在本地取消前面——只在客户端断连等于没停生产，还把这个动作伪装成
    了"关页面"。
    """
    api = _kt_code(API_KT)
    chat = _kt_code(CHATUI_KT)
    assert "cancelStreamRun" in api and '"/v1/chat/stream/"' in api and '"/cancel"' in api, \
        "Api.kt 没有打向 /v1/chat/stream/{run_id}/cancel 的封装"
    assert "Api.cancelStreamRun(" in chat, "停止没打服务端取消：只是本地 abort"
    assert "currentRunId" in chat, "停止没拿住这一轮的 run_id"
    assert "onRun" in chat, "没从流里抓 run_id：cancel 的目标无从取得"
    assert chat.index("Api.cancelStreamRun(") < chat.index("streamJob?.cancel()"), \
        "本地取消排在了服务端取消前面：主路径/兜底路径反了"


# ---------- 双端逐字文案：三句人话一个字都不许漂 ----------

def test_the_resume_stop_and_history_words_are_identical_on_both_ends():
    """断线重连 / 取历史 / 已停止三句话，安卓与网页必须逐字一致——不许各造各的句子。

    网页是唯一出处（api.js:142 / app.js:1662 / app.js:1588,1597）；正对照同时钉住"网页那句
    还在"，防的是有人改安卓时顺手把网页也改了、两头一起漂。
    """
    js = _read(STATIC / "api.js") + _read(STATIC / "app.js")
    kt = _kt_code(API_KT) + _kt_code(CHATUI_KT)
    for phrase in (STREAM_RESUME_PHRASE, TO_HISTORY_PHRASE, STOPPED_PHRASE):
        assert phrase in js, f"这句在网页端消失了（对照锚点坏了）：{phrase}"
        assert phrase in kt, f"安卓端还没有这句逐字一致的人话：{phrase}"
