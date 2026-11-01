package xyz.fenever.assistant.nativeapp.ui

import android.content.ComponentName
import android.content.Intent
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.PopupWindow
import android.widget.TextView
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.EnterTransition
import androidx.compose.animation.ExitTransition
import androidx.compose.animation.core.CubicBezierEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.scaleOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.ime
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.Slider
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.input.pointer.PointerEventPass
import androidx.compose.ui.input.pointer.PointerInputChange
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.boundsInRoot
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.platform.LocalTextToolbar
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.platform.TextToolbar
import androidx.compose.ui.platform.TextToolbarStatus
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.compose.ui.zIndex
import androidx.core.view.WindowInsetsCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.LifecycleOwner
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import xyz.fenever.assistant.nativeapp.Api
import xyz.fenever.assistant.nativeapp.ApiException
import xyz.fenever.assistant.nativeapp.ChatEvent
import xyz.fenever.assistant.nativeapp.ChatMessageDto
import xyz.fenever.assistant.nativeapp.ModelInfo
import xyz.fenever.assistant.nativeapp.Prefs
import xyz.fenever.assistant.nativeapp.SessionSummary
import xyz.fenever.assistant.nativeapp.StoredMessage
import xyz.fenever.assistant.nativeapp.StreamNeedHistoryException
import xyz.fenever.assistant.nativeapp.Usage
import xyz.fenever.assistant.core.TaskCard
import xyz.fenever.assistant.nativeapp.theme.AiBrandMark
import xyz.fenever.assistant.nativeapp.theme.FenverTokens
import xyz.fenever.assistant.nativeapp.theme.isWebLight
import xyz.fenever.assistant.nativeapp.theme.text3Color

/* 聊天主屏 —— 逐块对位旧壳加载的网页（backend/app/web/static）：
 * index.html 的 .topbar/.messages/.composer 结构，style.css 移动端那档数值
 * （@media max-width:860 就是手机上的真身），行为语义逐条照 app.js 移植。
 * 侧栏 = 拉开的抽屉（SessionDrawer）；设置 = 底部弹层，二级页在同一张弹层里
 * 换 view —— "谁都不该是第二个弹窗"。 */

/* .att-chip / .msg-atts 的附件条目：网页字段 {id,name,kind,size,url}；
 * 原生没有 blob URL，图片按 id 现取字节解码，取不到退化成 📄 标签（同一降级语义）。 */
data class AttItem(val id: String, val name: String, val kind: String, val size: Long)

/* v0.39.x 驾驶舱 Task 7「接力队列」：任务执行中继续发送不再被吞，按网页
 * queueFor/enqueueQueueItem 的语义进按会话分桶的内存队列，流正常收尾后自动接力。
 * 全部状态住在 remember——跨进程死亡不落盘，这是本版的产品口径，不是遗漏。 */
data class QueuedMsg(val id: String, val text: String,
                    val attachments: List<AttItem>, val k: Int)

/** 插话请求：「就现在发这一条」连同出发时的会话坐标——流收尾时对不上号就把它
 *  放回**原会话**的队列头，绝不改发给中途换上的别的会话。 */
data class ForceNext(val sid: String, val epoch: Int, val item: QueuedMsg)

private const val QUEUE_MAX_ITEMS = 20
private const val QUEUE_MAX_CHARS = 20_000
// 会话还没建出来（sessionId 为空）时队列挂在这枚哨兵桶上：草稿期的待发也不许丢。
private const val QUEUE_DRAFT_KEY = "__draft__"
private fun queueKeyOf(sid: String): String = sid.ifEmpty { QUEUE_DRAFT_KEY }

/* 接力资格的闸全在这一个纯函数里，静态可查、双端可对照；
 * 「异常收尾/对不上号要暂停」的处置留在调用方，这里只回答能不能接力。 */
private fun canRelay(normal: Boolean, sameSession: Boolean, foreground: Boolean,
                     paused: Boolean, busy: Boolean, hasItem: Boolean): Boolean =
    normal && sameSession && foreground && !paused && !busy && hasItem

/* 接力/插话的图片闸：带图那条只许交给支持视觉的模型（网页 relayImageBlocked 同判据）。 */
private fun relayImageBlocked(atts: List<AttItem>, vision: Boolean): Boolean =
    atts.any { it.kind == "image" } && !vision

/* ---------------- 过程留痕（v0.29.0） ----------------
 * 步的形状与服务端 core/stream_events.py 的 step_* 同构（kind 分 thinking/tool/
 * search/omitted），网页 accumulateTrace 的 UI 原生化：流上四类过程帧并进一份列表，
 * 历史回放（StoredMessage.trace / done.trace）翻译成同一模型，面板只渲染一份。
 * 安全纪律与网页逐条对齐：所有字段一律纯文本渲染，绝不从不安全文本里造可点区间；
 * 链接只在客户端复查 http(s) 前缀之后才挂点击（服务端那道过滤不算信任前提）。 */
data class TraceHit(val title: String, val url: String, val snippet: String)

data class TraceItem(
    val kind: String,                 // thinking / tool / search / omitted（认不出的原样留 kind）
    val text: String = "",            // thinking 正文；认不出的步的回退文案
    val id: String = "",              // tool_call ↔ tool_result 的配对键
    val name: String = "",
    val label: String = "",
    val pending: Boolean = false,     // tool_call 已发、tool_result 还没等到
    val ok: Boolean = false,
    val summary: String = "",
    val elapsedMs: Int = 0,
    val truncated: Boolean = false,
    val query: String = "",
    val results: List<TraceHit> = emptyList(),
    val source: String = "",          // 降级链实际出结果的源名（v0.32 W3 上屏；老步没有）
    val omitted: Int = 0,
)

/* v0.31 审批卡片状态。与 PWA holder._approval 同一份形状。
 * 文案与 backend/app/core/approval.py 的 APPROVAL_TITLE 等逐字对齐（契约测试钉死）。 */
data class ApprovalState(
    val ticketId: String,
    val toolName: String,
    val planSummary: String,
    val expiresAt: Double,
    val resolved: Boolean = false,
    val decision: String = "",  // "allow" / "reject" / "" (pending)
)

/* v0.35 #99「需要你输入」卡片的本机状态。detailJson 是 GET /v1/tasks/{id} 的原样回包：
 * 卡片上每个字都由 core.TaskCard 从这一份 JSON 现算，不把服务端文本拆成多个字段再拼。
 * 状态住在 ChatScreen 这一层（LazyColumn 条目滚出视口会销毁组合，记在条目里会丢），
 * 并随身份一起 remember：换账号时旧卡片（常是验证码）一个字节都不留。
 * owner 是抓取时的那一个 user_id；任何一枪回来先比它，对不上就整段丢掉。 */
private data class TaskCardUi(
    val owner: String,
    val taskId: String,
    val detailJson: String,
    val draft: String = "",
    val busy: Boolean = false,
    val error: String = "",
)

/* 落盘步 → UI 步：全量读法（缺键给空值），认不出的 kind 不猜形状、只把能给的字留下。 */
private fun jtStr(o: JsonObject, k: String): String =
    (o[k] as? JsonPrimitive)?.contentOrNull.orEmpty()
private fun jtBool(o: JsonObject, k: String): Boolean =
    (o[k] as? JsonPrimitive)?.contentOrNull?.toBooleanStrictOrNull() ?: false
private fun jtInt(o: JsonObject, k: String): Int =
    (o[k] as? JsonPrimitive)?.contentOrNull?.toIntOrNull() ?: 0

internal fun traceFromStored(steps: List<JsonObject>?): List<TraceItem> =
    (steps ?: emptyList()).mapNotNull { o ->
        val kind = jtStr(o, "kind")
        if (kind.isEmpty()) return@mapNotNull null
        when (kind) {
            "thinking" -> TraceItem(kind, text = jtStr(o, "text"))
            "omitted" -> TraceItem(kind, omitted = jtInt(o, "count"))
            "tool" -> TraceItem(kind, id = jtStr(o, "id"), name = jtStr(o, "name"),
                label = jtStr(o, "label"), pending = jtBool(o, "pending"),
                ok = jtBool(o, "ok"), summary = jtStr(o, "summary"),
                elapsedMs = jtInt(o, "elapsed_ms"), truncated = jtBool(o, "truncated"))
            "search" -> TraceItem(kind, id = jtStr(o, "id"), query = jtStr(o, "query"),
                source = jtStr(o, "source"),
                results = (o["results"] as? JsonArray)?.mapNotNull { r ->
                    val rj = r as? JsonObject ?: return@mapNotNull null
                    TraceHit(jtStr(rj, "title"), jtStr(rj, "url"), jtStr(rj, "snippet"))
                } ?: emptyList())
            else -> TraceItem(kind, text = jtStr(o, "text")
                .ifEmpty { jtStr(o, "summary") }.ifEmpty { jtStr(o, "label") })
        }
    }

/* UI 步 → 落盘步：PUT 全量替换会把不带 trace 的消息在服务端抹平，这里必须把
 * 四型步原样写回（未知 kind 退化成 kind+text 回环，不再丢整步）。 */
internal fun traceToJson(steps: List<TraceItem>): List<JsonObject> = steps.map { t ->
    buildJsonObject {
        put("kind", t.kind)
        when (t.kind) {
            "thinking" -> put("text", t.text)
            "omitted" -> put("count", t.omitted)
            "tool" -> {
                put("id", t.id); put("name", t.name); put("label", t.label)
                put("ok", t.ok); put("summary", t.summary)
                put("elapsed_ms", t.elapsedMs); put("truncated", t.truncated)
            }
            "search" -> {
                put("id", t.id); put("query", t.query)
                // 与服务端 step_search 同一先例：没有 source 就不出现这个键
                if (t.source.isNotEmpty()) put("source", t.source)
                put("results", JsonArray(t.results.map { r ->
                    buildJsonObject {
                        put("title", r.title); put("url", r.url); put("snippet", r.snippet)
                    }
                }))
            }
            else -> put("text", t.text)
        }
    }
}

/** 流已经不会再有 tool_result 了：把还挂着"进行中"的步收落成一句话，别让它永远悬着。 */
private fun closePendingTrace(steps: List<TraceItem>): List<TraceItem> = steps.map {
    if (it.kind == "tool" && it.pending)
        it.copy(pending = false, ok = false,
            summary = it.summary.ifEmpty { "没等到这一步的结果" })
    else it
}

/* 一条已落地的消息。transient = 网页 runStream 失败那条 ⚠️ 气泡：看得见，
 * 但 replaceMessages/outbound 都把它滤掉，不发给服务端、不混进上下文。
 * trace/traceExpanded（v0.29.0）：这条回答的过程步与面板开合。开合记在消息上
 * 而不是组件里——LazyColumn 把条目滚出视口就销毁组合，记在组件上会丢用户点开的选择。
 * localId：还没拿到服务端 message_id 的消息（本地用户消息、⚠️ 占位）在列表里
 * 也要有稳定且唯一的 key——同一句话连发两次时，内容哈希会撞成同一个 key。 */
private val localMsgSeq = java.util.concurrent.atomic.AtomicLong(0L)
private fun nextLocalMsgSeq(): Long = localMsgSeq.incrementAndGet()

data class UiMsg(val role: String, val content: String,
                 val messageId: String? = null, val model: String? = null,
                 val feedback: Int? = null,
                 val attachments: List<AttItem> = emptyList(),
                 val transient: Boolean = false,
                 val trace: List<TraceItem> = emptyList(),
                 val traceExpanded: Boolean = false,
                 val localId: Long = nextLocalMsgSeq())

/* ---------------- v0.39.x 驾驶舱 Task 6：环与账本的读数快照 ----------------
 * 单位在字段名里写死：*Tokens 一律是 estimateTokens 的 token 原值，*K 一律是
 * 千 token（显示口径）；两者之间只许在显式 ×1000 / 除 1000.0 处换算——Task 4 网页侧
 * 那个 "叫 K 存的是 token" 的命名坑不复制（Api.calibrateUsedK 的名字是沿用网页
 * 公式的例外，语义与注释都以 token 为准）。 */
internal data class ContextRingInfo(
    val available: Boolean,
    val modelName: String,
    val providerId: String,
    val personaTokens: Int,
    val historyTokens: Int,
    val inputTokens: Int,
    val knownTokens: Int,
    val usedTokens: Int,
    val usedK: Int,
    val residualTokens: Int,
    val budgetK: Int,
    val capK: Int,
    val pct: Int,
    val hasUsage: Boolean,
    val lastPromptTokens: Int,
    val lastClientTokens: Int,
    val dropped: Int,
)

/** clientCtx 的打包：发送截断与环读数吃同一份（同一条 .drop(drop)）。 */
private data class ClientCtx(val personaTokens: Int, val historyTokens: Int,
                             val msgs: List<ChatMessageDto>, val persona: String)

/** token 原值 → K（四舍五入，与网页 Math.round(used/1000) 同一口径）。 */
private fun roundK(tokens: Int): Int = Math.round(tokens / 1000.0).toInt()

/* 网页 .suggestions 的四枚 chips（app.js SUGGESTIONS 逐字）。点击只填输入框、不发送。
 * 2026-09-28 真机反馈：原来四条偏"技术演示"，换成日常真会问的事。 */
val CHAT_SUGGESTIONS = listOf(
    "今天晚饭吃什么？帮我想个菜单",
    "帮我写条请假消息，自然一点",
    "这段话帮我改得更客气一点",
    "周末在家无聊，推荐点事做",
)

/* v0.28 贴底的统一手段：超出条目高度的 offset 会被 LazyList 夹到**最大滚动位置**——
 * 最后一项的底边正好贴住视口底（含 6dp 底衬）。不拿"上一帧量到的高度"做减法，
 * 是因为条目会在贴过之后再长高（操作行晚一拍才挂出来）：高度会撒谎，边界不会。 */
private const val MAX_SCROLL_OFFSET = Int.MAX_VALUE / 2

/* v0.36.1 审查批：落定贴底的**有界**校正上限——补贴最多 3 轮帧检，一轮都不许多。
 * 这条常量存在的意义就是可被静态锁钉死：判据是"校正有界而非无限"，
 * 写死 3、且贴底效果里除了这一个 repeat 之外不许出现任何随帧循环
 * （没有 while(true)、没有 repeat(24)、没有别的 repeat）。
 * 不许把它改大，更不许借它复活 24 帧循环——那正是 v0.36.1 首帧批砍掉的东西。 */
private const val MAX_BOTTOM_CORRECTIONS = 3

/* v0.39 性能 A 包：流式发布与跟随的两个节拍（判据见 test_v0390_native_stream_perf.py）。
 * STREAM_PUBLISH_MS——ticker 把 acc 的最新快照发布到 streamText 的窗口：content 帧
 * 到达频率远高于刷新率，逐 token 写 state 会让整棵消息列表逐 token 重组；40ms 一更
 * 在观感上是"连贯打字"，在成本上是每帧最多一次的有界功。批准区间 30..50，锁会红。
 * STREAM_FOLLOW_MS——流式跟随环的判定周期：近底才贴、每轮先 delay 不忙等；
 * 80 与网页 paint 的 50ms 节流同族，慢半拍是因为原生这侧还要量 layoutInfo。 */
private const val STREAM_PUBLISH_MS = 40L
private const val STREAM_FOLLOW_MS = 80L

/* v0.36.1 动效批：会话切换的「挤入卡片」（方案 A）节拍与缓动。
 * 只动消息区这一张卡：新卡从抽屉侧（左）轻微滑入 + 缩放淡入，旧卡向右轻微滑出，
 * 像从牌堆里把下一张顶出来。进入 260ms、退出 220ms——先走的不许比进来的慢，
 * 接替感来自这里；缓动 .2f,0f,0f,1f 为标准慢出。
 * 变换全部走 AnimatedContent 自带的 graphicsLayer 通道，不在每帧重组里做重活。 */
private const val SESSION_SWAP_ENTER_MS = 260
private const val SESSION_SWAP_EXIT_MS = 220
private val SESSION_SWAP_EASING = CubicBezierEasing(0.2f, 0f, 0f, 1f)

/** v0.36.1 动效批：切换转场的目标状态——消息区卡片画的就是这一份快照。
 * contentKey 只取 sid：流式 token、消息增长会生成新实例但 key 不变（同一张卡就地
 * 换内容，不触发转场）；只有换会话才换 key，旧卡带着**切换那一刻的 msgs** 退场，
 * 退出过程中绝不读新会话的 messages。 */
private class SwapTarget(val sid: String, val msgs: List<UiMsg>, val loading: Boolean)

/* 第 7 轮真机反馈改判：第 4 轮为赶跑"点一下就浮出且关不掉的粘贴气泡"，把整屏
 * 文字工具条换成了空壳——代价是键盘开着时长按输入框也什么都出不来，想粘贴没门。
 * 用户钦定：键盘弹着的时候长按要出「粘贴/全选/选择」那套系统工具条。空壳拆掉，
 * 恢复 Compose 默认工具条；长按语音的门控本来就在（键盘在=不接管），互不打架。 */

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun ChatScreen(onRequireAuth: (String) -> Unit, onLoggedOut: () -> Unit,
               onOpenUrl: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    val ctx = LocalContext.current
    val drawerState = rememberDrawerState(DrawerValue.Closed)
    // 当前身份（任务卡片的归属锚点）：换账号时 MainActivity 会整壳重建，这里再 remember
    // 一次，让任何一条重建路径都把旧卡片清干净，而不是只靠"外面恰好重建了"。
    val identityId = Prefs.currentEntry()?.userId.orEmpty()

    // ---------- 状态：对照 app.js 的 state / pref ----------
    var sessionId by remember { mutableStateOf(Prefs.lastSessionId) }
    // v0.36.1 切会话竞态守卫：每一次会话切换都推进号；getSession 回包时号已不是
    // 自己出发那一号就整包丢弃——连点两下抽屉里两条会话，慢的那一包绝不能落在
    // 新会话的屏幕上（旧消息挂在新标题下就是这么来的）。
    var sessionEpoch by remember { mutableIntStateOf(0) }
    // 会话内容在途标记：加载中屏上不给旧消息留位，画一条轻量读取提示，
    // 数据回来后一次性替换（对齐网页 renderSessions 的"要么旧要么新，不混"）。
    var sessionLoading by remember { mutableStateOf(false) }
    var sessions by remember { mutableStateOf(listOf<SessionSummary>()) }
    var messages by remember { mutableStateOf(listOf<UiMsg>()) }
    // v0.36.1 动效批：会话切换「挤入卡片」的转场快照（见 SwapTarget 注释）——
    // 换会话/换消息/换加载态都会生成新实例，但 AnimatedContent 的 contentKey 只认
    // sid：流式 token 与消息增长不触发转场，只有真正的切换才换卡。
    val swapTarget = remember(sessionId, messages, sessionLoading) {
        SwapTarget(sessionId, messages, sessionLoading)
    }
    // 首次组合不播：进 App 恢复上一次会话是「恢复现场」，不是「切换」。
    // swappedOnce 由三个真实切换入口（openSession、新对话、删除当前会话）打标——
    // 打标前 transitionSpec 恒为 None，首帧零动画；打标后每次都走完整转场。
    var swappedOnce by remember { mutableStateOf(false) }
    // 用户关了系统动画（Settings.Global.ANIMATOR_DURATION_SCALE == 0）就不该硬塞动效。
    // remember 只取一次（每组合反复查系统服务是明确禁止的写法）：
    // 组合期读一拍 Global.getFloat，此后整棵组件树复用该值，换会话时不再问系统。
    val animScaleOff = remember {
        Settings.Global.getFloat(ctx.contentResolver,
            Settings.Global.ANIMATOR_DURATION_SCALE, 1f) == 0f
    }
    // TextFieldValue 而不是 String：程序改文案（点建议标语、发送后清空）时
    // String 版 BasicTextField 会把光标弹回句首，用户接着打字很别扭。
    var input by remember { mutableStateOf(TextFieldValue("")) }
    var providers by remember { mutableStateOf(listOf<ModelInfo>()) }
    var serverDefault by remember { mutableStateOf<String?>(null) }
    var pending by remember { mutableStateOf(listOf<AttItem>()) }
    var status by remember { mutableStateOf("") }
    var statusErr by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var streamText by remember { mutableStateOf<String?>(null) }
    // v0.39 性能 A 包：组合期不再逐 token 读 streamText 的字符串——streamActive 是
    // "这一轮在流式"的开关（起流置真、finally 置假），streamVisible 只回答
    // "有没有正文"；streamText 的真实读取挪进 item 体内（StreamingBubble 要字）
    // 与两条 effect 的协程内（落定判据、跟随退出条件），父级条件全吃派生量。
    var streamActive by remember { mutableStateOf(false) }
    val streamVisible by remember { derivedStateOf { streamText != null } }
    // v0.29.0 过程面板：流式期间累积的过程步 + 面板开合。流式期间默认展开、落定自动
    // 折叠（网页 plan §7 同一判据）；用户点过头（touched）就把他的选择带进落定态，
    // 任何自动逻辑不再替他改主意。
    var streamTrace by remember { mutableStateOf(listOf<TraceItem>()) }
    var streamTraceOpen by remember { mutableStateOf(true) }
    var streamTraceTouched by remember { mutableStateOf(false) }
    // v0.31 审批卡片：流式中收到的 approval_request 帧。用户允许/拒绝/超时后置 resolved。
    var streamApproval by remember { mutableStateOf<ApprovalState?>(null) }
    // v0.35 #99 任务卡片：当前在 waiting_user 的那一条（由 GET /v1/tasks 找到、详情由
    // GET /v1/tasks/{id} 现读）。流上的挂起前缀只触发"去读一次"，从不当卡片状态用。
    var taskCard by remember(identityId) { mutableStateOf<TaskCardUi?>(null) }
    /* v0.39.x 驾驶舱 Task 6 的三张表（都随身份 remember——换账号不能把上一个人的
     * 用量/整理画到新会话头上，网页 resetViewForIdentity 的本机对位）：
     * - lastUsageByKey / lastClientKByKey：done.usage 落账与校准锚点，键「会话:模型」；
     * - contextDropBySession：轻量整理按会话记「从最早处切掉 N 条」，outboundMsgs 与
     *   clientCtx 读同一张表——只改显示不改发送的整理是假功能。历史 messages 永不动。 */
    val lastUsageByKey = remember(identityId) { mutableStateMapOf<String, Usage>() }
    val lastClientKByKey = remember(identityId) { mutableStateMapOf<String, Int>() }
    val contextDropBySession = remember(identityId) { mutableStateMapOf<String, Int>() }
    /* v0.39.x 驾驶舱 Task 7「接力队列」：忙碌时点发送不再吞掉，按会话分桶排队，
     * 流正常收尾后自动接力。全部住在 remember(identityId)——换账号绝不把上一个
     * 人的待发替他发出去；列表值用不可变 List，写回 map 才看得见变化。
     * 内存态、不落盘：跨进程死亡就清空，这是本版口径（网页端同为内存）。 */
    val queues = remember(identityId) { mutableStateMapOf<String, List<QueuedMsg>>() }
    val queuePaused = remember(identityId) { mutableStateMapOf<String, Boolean>() }
    // 接力发送中的那条：防重入的第二道闸（delay 醒来后 sendNow 之前的短窗）
    var sendingQueueId by remember(identityId) { mutableStateOf<String?>(null) }
    // 「立即插话」的待发射：随出发时的会话/请求号，收尾时核对坐标再放行
    var forceNext by remember(identityId) { mutableStateOf<ForceNext?>(null) }
    var queueNowConfirm by remember(identityId) { mutableStateOf<QueuedMsg?>(null) }
    // 退后台整条接力链停下：不在用户看不见的地方替他往外发消息
    var foreground by remember { mutableStateOf(true) }

    fun resetQueuesForIdentity() {
        queues.clear(); queuePaused.clear(); sendingQueueId = null; forceNext = null
    }
    // 首次组合时各表本来就是空，这一枪是 no-op；换身份立刻全清。
    LaunchedEffect(identityId) { resetQueuesForIdentity() }
    val lifecycleOwner = ctx as? LifecycleOwner
    DisposableEffect(lifecycleOwner) {
        val obs = LifecycleEventObserver { _, ev ->
            when (ev) {
                Lifecycle.Event.ON_START -> foreground = true
                Lifecycle.Event.ON_STOP -> {
                    foreground = false
                    queuePaused[queueKeyOf(sessionId)] = true
                }
                else -> Unit
            }
        }
        lifecycleOwner?.lifecycle?.addObserver(obs)
        onDispose { lifecycleOwner?.lifecycle?.removeObserver(obs) }
    }
    // 预算住在 SharedPreferences（非快照态）：滑杆每写一次手动 +1，环/账本才跟着重画。
    var ringTick by remember { mutableIntStateOf(0) }
    var contextPanelOpen by remember { mutableStateOf(false) }
    var attachOpen by remember { mutableStateOf(false) }
    var modelMenuOpen by remember { mutableStateOf(false) }
    /* v0.39.x 驾驶舱 Task 10「模型卡」的四格本机态 + 一条记录缓存：
     * - providerRecords：/v1/me/providers 那份**完整**记录（含 base_url/api_key_masked/
     *   is_default/thinking_profile）。全量 PUT 的 body 只能从它取——/v1/models 的目录项
     *   没有 base_url，拿目录项凑等于把上游地址洗掉。随身份 remember：换账号绝不能
     *   拿着上一个人的记录往新账号的门里 PUT（网页 resetViewForIdentity 的同一条）。
     * - modelCardOpen：窄屏一次只展开一张卡（点行展开/收起，同网页 toggleModelDetail 触屏支）。
     * - modelManage：能力编辑区开关。关掉弹单**不清**它（收起弹单≠退出管理，
     *   管理员中途去看清单再回来还得接着改）——与网页 hideModelMenu 同一判据。
     * - budgetClamped：上限被调小后把本机预算夹下来的那一笔（id/夹前/夹后），
     *   模型卡据此说一句人话；弹单关掉即消化（网页是渲染一次即清，Compose 不许在
     *   组合期写状态，所以收口点放在 onClose——差在哪已写进 Task 10 报告）。 */
    var providerRecords by remember(identityId) { mutableStateOf(mapOf<String, JsonObject>()) }
    var modelCardOpen by remember { mutableStateOf("") }
    var modelManage by remember { mutableStateOf(false) }
    var modelSaving by remember { mutableStateOf(false) }
    var modelSaved by remember { mutableStateOf<Pair<String, String>?>(null) }
    var modelCardError by remember { mutableStateOf("") }
    var budgetClamped by remember(identityId) { mutableStateOf<Triple<String, Int, Int>?>(null) }
    /* 关弹单的收尾**只有一份**（修复轮 Minor 1 + 6）：右上角 ×、卡里的「设为这条」、
     * 系统返回键三个出口都走这里。从前 onPick 只关了 modelMenuOpen，卡片展开态与那三句
     * 提示都没消化 ⇒ 换个模型再打开，还是那张卡展开着、还可能重印上一轮那句。
     * modelManage 仍然**不清**（收起弹单 ≠ 退出管理，与网页 hideModelMenu 同一判据）。
     * 清的都是本机快照态，不写 SharedPreferences，所以组合外调用是安全的。 */
    fun closeModelMenu() {
        modelMenuOpen = false
        modelCardOpen = ""
        budgetClamped = null
        modelSaved = null
        modelCardError = ""
    }
    var drawerTick by remember { mutableStateOf(0) }
    var settingsPage by remember { mutableStateOf<String?>(null) }  // null=关；""=一级列表
    var streamJob by remember { mutableStateOf<Job?>(null) }
    var stopRequested by remember { mutableStateOf(false) }
    // 这一轮的 run_id：从 start 帧/onRun 抓来存着——"停止"要打服务端 cancel 就认它，
    // 断线续播也认它。没有它就退化成只能本地断连（等于没停生产）。
    var currentRunId by remember { mutableStateOf<String?>(null) }
    var editingIndex by remember { mutableStateOf<Int?>(null) }
    // v0.23 T2.2 AC-3：提交失败回编辑态时，草稿要原样还在——父侧存一份，
    // 只给 MessageRow 当初始值（子侧照常自己维护输入），重试成功即清空。
    var editingDraft by remember { mutableStateOf<String?>(null) }
    var copyTip by remember { mutableStateOf<Pair<Int, String>?>(null) }
    // 语音模式（第 4 轮真机反馈，对齐参考图）：点 🎙 把输入区换成「按住说话」大按钮，
    // 说话不再依赖"键盘恰好收着"这种碰运气的手势判定。
    var voiceMode by remember { mutableStateOf(false) }
    // 语音服务自己拒录音时记下它的包名/应用名：输入卡上给直达入口（第 5 轮：必须
    // 带上目标 App 的名字——用户会去开本 App「Fenver」自己的麦克风，俩不是一回事）。
    var voiceServicePkg by remember { mutableStateOf<String?>(null) }
    var voiceServiceLabel by remember { mutableStateOf<String?>(null) }
    var voiceHintOn by remember { mutableStateOf(false) }
    // 第 6 轮真机反馈：默认引擎连不上它自己的服务器（11/SERVER_TERMINATED），
    // 开麦克风没用——要能换引擎。picker=「换个语音引擎」弹窗；streak 数的是
    // 连续引擎死亡次数，到 2 就自动轮换下一个候选，别让用户干按。
    var enginePickerOpen by remember { mutableStateOf(false) }
    var engineDeadStreak by remember { mutableStateOf(0) }
    // v0.36.1：引擎候选改由 effect 在 IO 线程枚举（speechServiceCandidates 走
    // PackageManager 两次 queryIntentServices + 逐个 getApplicationInfo/Label，
    // 是同步 binder 往返）。从前它挂在弹窗的组合体里——一开弹窗就在主线程组合
    // 阶段查系统服务，帧直接被查询钉住。null = 这一枪还没回，弹窗先说"正在查找"。
    var engineCands by remember { mutableStateOf<List<SpeechServiceInfo>?>(null) }
    LaunchedEffect(enginePickerOpen) {
        if (!enginePickerOpen) return@LaunchedEffect
        engineCands = withContext(Dispatchers.IO) { speechServiceCandidates(ctx) }
    }
    val listState = rememberLazyListState()
    // v0.28：composer 那一块此刻的实测高度（像素）。悬浮「跳到底部」箭头
    // 要悬在它正上方 14dp——箭头是浮层不占布局，位置只能按实测高度算。
    var composerPx by remember { mutableIntStateOf(0) }
    val inputFocus = remember { FocusRequester() }
    val focusManager = LocalFocusManager.current

    fun setStatus(t: String, err: Boolean = false) { status = t; statusErr = err }

    // 提示条 6 秒自己收（第 4 轮真机反馈：红色提示常驻非常影响观感）。
    // 「生成中…」这类由流程自己清的空串不碍事；常驻从此没有。
    LaunchedEffect(status) {
        if (status.isEmpty()) return@LaunchedEffect
        delay(6000)
        status = ""
        statusErr = false
    }

    /* 第 16 轮真机回归（v0.28.0 后「模型丢失 + 会话丢失」）：服务端把每账户令牌
     * 封顶 8 条、超了静默挤最旧——一条 401 只说明**当前这条令牌**作废，过去用
     * clearAuth() 会把这台机器上所有身份（含各自的 providerId/lastSessionId）
     * 一起抹掉，把一次可恢复的过期放大成"账户全没"。改为只 forgetCurrent()。 */
    fun logoutIf401(e: Throwable): Boolean {
        if (e is ApiException && e.status == 401) {
            // 任务卡片里常是短信验证码/收货地址：落登录页这一刻就要让它消失，
            // 不等下一次重组或下一次读列表。
            taskCard = null
            Prefs.forgetCurrent(); onLoggedOut(); return true
        }
        return false
    }

    /* ---------------- v0.35 #99 任务卡片：读侧只有一个真相源 ----------------
     * 1) 触发面只有两个：冷启动/换人后进来读一次；流上出现挂起前缀时再读一次。
     *    帧上没有任务身份，所以"哪一条"由 GET /v1/tasks 的 waiting_user 现找，详情
     *    一律 GET /v1/tasks/{id} 现读——重启后卡片还在，靠的就是这一步不靠进程内缓冲。
     * 2) 回答是一次长阻塞 POST；提交在途时按钮与输入一起置灰（同一枚断点只许一个答案
     *    接走，双击送出的第二枪只会拿到"没接住"）。无论回没回话，收尾都再读一次 GET：
     *    断连时服务端照跑，唯一说得出"现在挂在哪一步"的地方还是读接口。
     * 3) 每一枪回来先比 owner；换人之后旧请求的结果一个字段都不许落进新界面。 */
    fun taskReadError(e: Throwable): String =
        if (e is ApiException) TaskCard.readError(e.status, e.message.orEmpty())
        else TaskCard.NETWORK_ERROR_TEXT

    suspend fun refreshTaskCard() {
        val owner = Prefs.currentEntry()?.userId.orEmpty()
        if (owner.isEmpty()) { taskCard = null; return }
        val listRaw = try {
            Api.listTasks()
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            if (logoutIf401(e)) { taskCard = null; return }
            // 列表没读到：已经显示着的那份不动（它仍是最后一次读到的真相），只把
            // 读不到这件事落在卡片上；没有卡片时不凭空造一张错误卡。
            taskCard = taskCard?.copy(error = taskReadError(e))
            return
        }
        if (Prefs.currentEntry()?.userId.orEmpty() != owner) return
        val id = TaskCard.waitingTaskId(listRaw, owner)
        if (id.isEmpty()) { taskCard = null; return }
        val detailRaw = try {
            Api.getTask(id)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            if (logoutIf401(e)) { taskCard = null; return }
            val cur = taskCard
            if (cur != null && cur.owner == owner && cur.taskId == id)
                taskCard = cur.copy(error = taskReadError(e))
            return
        }
        if (Prefs.currentEntry()?.userId.orEmpty() != owner) return
        val view = TaskCard.view(detailRaw, false)
        taskCard = if (!view.visible) null else {
            // 同一条任务的草稿和在途标记跨刷新留着（用户可能刚打一半/刚按提交，
            // 一次并发读回来的旧忙碌态不该把按钮重新点亮）；换了一条就从空框开始，
            // 绝不把上一条的答案带进下一条。
            val cur = taskCard?.takeIf { it.owner == owner && it.taskId == id }
            TaskCardUi(owner, id, detailRaw, draft = cur?.draft.orEmpty(),
                busy = cur?.busy ?: false)
        }
    }

    fun submitTaskAnswer() {
        val cur = taskCard ?: return
        if (cur.busy) return                      // 双击/连点：第二下直接丢，不送第二枪
        val text = cur.draft.trim()
        if (text.isEmpty()) return
        val view = TaskCard.view(cur.detailJson, false)
        if (!view.canAnswer) return
        val owner = cur.owner
        taskCard = cur.copy(busy = true, error = "")
        scope.launch {
            var loggedOut = false
            var cancelled = false
            try {
                Api.answerTask(view.goal, view.taskId, text)
            } catch (e: CancellationException) {
                cancelled = true
                throw e
            } catch (e: Exception) {
                if (logoutIf401(e)) {
                    loggedOut = true
                    taskCard = null
                } else if (Prefs.currentEntry()?.userId.orEmpty() == owner) {
                    val now = taskCard
                    // 这一枪出发之后卡片可能已换成另一条任务：错误只落回它自己那一条。
                    if (now != null && now.owner == owner && now.taskId == view.taskId)
                        taskCard = now.copy(error = taskReadError(e))
                }
            } finally {
                if (!loggedOut && Prefs.currentEntry()?.userId.orEmpty() == owner) {
                    val now = taskCard
                    if (now != null && now.owner == owner && now.taskId == view.taskId) {
                        // 取消（整壳被换掉）也要把在途标记收回；只有真跑完的那一枪才
                        // 需要再去 GET 一次——取消之后界面已经不归这一次组合管了。
                        taskCard = now.copy(busy = false,
                            draft = if (cancelled) now.draft else "")
                    }
                    if (!cancelled) refreshTaskCard()
                }
            }
        }
    }

    /* app.js 的同一组纯函数：serverDefaultProvider / currentProvider / modelCapK。
     * 选中的那条不可用（新身份 providerId 是空的）→ 退回服务端默认，不拦发送键。 */
    fun serverDefaultProvider(): ModelInfo? =
        providers.firstOrNull { it.id == serverDefault && it.usable }

    fun currentProvider(): ModelInfo? {
        val listed = providers.firstOrNull { it.id == Prefs.defaultProviderId }
        return if (listed != null && listed.usable) listed else serverDefaultProvider()
    }

    fun modelCapK(): Int = currentProvider()?.max_context_k?.takeIf { it > 0 } ?: 64

    /* v0.39.x Task 10：上限比本机记着的预算小了（管理员/属主把这一格调小），当场把它
     * 夹回上限并记一笔该说的那句话。
     * 判据取 map 里的**原始**那一份（contextBudgetWantedK）。原生这边的理由是：它能把
     * 「这台设备从没为这条记过」（返回 0，没什么可夹）与「记过且越界」分开——
     * contextBudgetK 是读数口，只夹 1..10000（Prefs 那一对函数一字未动），**不夹模型上限**，
     * 所以网页那句"拿夹过的数比上限恒不成立"在这边并不成立，别把它的理由照抄过来。
     * 返回值（修复轮 I-2）：本轮这句话是不是由状态条说了。调用方那句清状态必须给它让路，
     * 否则同一帧两次写状态条、只有最后一次可见，这句话用户永远看不见。 */
    fun reconcileBudgetCap(pid: String, capK: Int): Boolean {
        if (pid.isEmpty() || capK <= 0) return false
        val stored = Prefs.contextBudgetWantedK(pid)
        if (stored > capK) {
            Prefs.setContextBudget(pid, capK)
            budgetClamped = Triple(pid, stored, capK)
            ringTick++
            if (!modelMenuOpen) {
                val name = providers.firstOrNull { it.id == pid }?.name ?: "这条模型"
                setStatus(budgetClampNote(name, stored, capK))
                // 手机上"要先点开模型卡才看得见"等于没说，所以这一句落状态条；
                // 回报 true 是给 loadModelsNow 看的：别在同一帧把它抹掉。
                return true
            }
        }
        return false
    }

    /* 全量 PUT 的原料：/v1/me/providers 的 shared + mine 两段按 id 建一份缓存。
     * 只在需要编辑时读一次（缓存为空才发这一枪），读不到就让保存键禁用并在卡上明说。 */
    suspend fun loadProviderRecordsNow() {
        runCatching { Api.myProviders() }.onSuccess { data ->
            val out = mutableMapOf<String, JsonObject>()
            for (section in listOf("shared", "mine")) {
                ((data[section] as? JsonArray) ?: JsonArray(emptyList())).forEach { el ->
                    val o = el as? JsonObject ?: return@forEach
                    val rid = o["id"]?.jsonPrimitive?.contentOrNull ?: ""
                    if (rid.isNotEmpty()) out[rid] = o
                }
            }
            providerRecords = out
        }
    }
    fun loadProviderRecords() { scope.launch { loadProviderRecordsNow() } }

    // ---------- 冷启动：模型清单 + 会话列表（网页 boot 的 loadModels/loadSessions） ----------
    suspend fun loadModelsNow() {
        runCatching { Api.models() }.onSuccess { r ->
            providers = r.models
            serverDefault = r.default
            if (providers.none { it.id == Prefs.defaultProviderId && it.usable })
                serverDefaultProvider()?.let { Prefs.defaultProviderId = it.id }
            // 预算迁移的挂点：清单回来才真正确定「那一位默认模型」——旧全局滑杆键
            // 只在这一刻被搬进按模型存的 map（map 已存在时是 no-op，见 Prefs）。
            Prefs.migrateContextBudget()
            // 清单到手即核对一次：服务端把某条上限调小过，本机预算跟着夹下来并说一声
            val clampedNow = reconcileBudgetCap(currentProvider()?.id ?: "", modelCapK())
            // 修复轮 I-2：这句清状态**不能无条件**。上一版这里是"同帧两次写"——
            // 上面 reconcileBudgetCap 刚把「上限降到了…」写进状态条，这里立刻写回空串，
            // 只有最后一次可见，那句提示等于从没说过。本轮它说过话就让它留在屏上
            // （状态条自己 6 秒收，见上面那条 LaunchedEffect(status)）。
            if (providers.any { it.usable } && !clampedNow) setStatus("")
        }.onFailure { if (!logoutIf401(it)) setStatus(it.message ?: "", true) }
    }

    /* 模型卡的唯一写口（网页 saveModelCapabilities 的本机对位）：
     * PUT 是全量语义 ⇒ body 必须来自那条完整记录 + 这次要改的那几格；打哪扇门只由
     * 归属决定（shared → 管理员面 /v1/providers，否则自助面），can_edit=false 时界面
     * 只读、服务端仍二次校验，两边都不许省。
     * 成功后按「先记录、后清单」重读：反过来的话卡片会在新缓存落地之前自己再发一枪。 */
    fun saveModelCapabilities(id: String, patch: JsonObject) {
        val rec = providerRecords[id]
        if (rec == null) {
            modelCardError = "没拿到这条模型的完整记录，保存已禁用：模型目录里没有 base_url，" +
                "拿它 PUT 等于把上游地址洗掉。请先开一次「设置 → 模型服务」再回来。"
            return
        }
        modelSaving = true
        scope.launch {
            runCatching {
                val body = capabilityBody(rec, patch)
                if (providerCapabilityScope(rec) == "admin") Api.updateProvider(id, body)
                else Api.updateMyProvider(id, body)
            }.onSuccess {
                modelCardError = ""
                val label = rec["label"]?.jsonPrimitive?.contentOrNull ?: "这条模型"
                modelSaved = id to "$label 的能力已保存"
                loadProviderRecordsNow()
                loadModelsNow()
                reconcileBudgetCap(id, providers.firstOrNull { it.id == id }?.max_context_k ?: 0)
            }.onFailure {
                // 服务端那句人话原样落在卡上（428 二次验证的 detail 也走这一条：
                // 原生设置页今天就是把 ApiException.message 直接显示出来，卡片不另起一套）
                modelCardError = it.message ?: "保存失败：服务端没给理由"
            }
            modelSaving = false
        }
    }
    suspend fun refreshSessionsNow(): String? =
        try {
            sessions = Api.listSessions()
            null
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            if (!logoutIf401(e)) e.message ?: "加载失败" else null
        }
    fun refreshSessions() {
        scope.launch { refreshSessionsNow() }
    }
    LaunchedEffect(Unit) {
        // 三路互不依赖：各发各的，谁先回来谁先落，不让任务卡读接口把模型清单挡住。
        // 各自的 401/错误处理仍在 loadModelsNow / refreshSessionsNow / refreshTaskCard 里。
        coroutineScope {
            launch { loadModelsNow() }
            launch { refreshSessionsNow() }
            launch { refreshTaskCard() }
        }
    }

    // switchSession：getSession 拉全量消息（含服务端留存的附件条目）
    fun openSession(id: String) {
        val epoch = sessionEpoch + 1
        sessionEpoch = epoch
        swappedOnce = true   // v0.36.1 动效：从这一次起切换才走「挤入卡片」转场
        sessionId = id; Prefs.lastSessionId = id
        // 换会话必关编辑态：编辑框是【这一条会话里第 index 行】的承诺，换列表不兑现
        editingIndex = null; editingDraft = null
        // v0.36.1：旧消息不挂在新标题下——切的那一刻就清空进加载态，
        // 回包一次性替换；再点别的会话会把这一枪的号作废，慢包整包丢弃。
        messages = emptyList()
        sessionLoading = true
        scope.launch {
            runCatching { Api.getSession(id) }.onSuccess { d ->
                if (epoch != sessionEpoch) return@onSuccess   // 已不是当前会话的请求：丢
                messages = d.messages.map { m ->
                    UiMsg(m.role, m.content, m.message_id, m.model,
                        attachments = (m.attachments ?: emptyList())
                            .map { AttItem(it.id, it.name, it.kind, it.size) },
                        // 落盘回放：重开一条会话也看得见过程（服务端那份 trace 就是为这一句留的）
                        trace = traceFromStored(m.trace))
                }
                sessionLoading = false
            }.onFailure {
                if (epoch == sessionEpoch) {
                    sessionLoading = false
                    if (!logoutIf401(it)) setStatus(it.message ?: "", true)
                }
            }
        }
    }

    // 抽屉拉开时先显示已预取的列表，再由抽屉自己后台刷新；这里只通知它刷新。
    LaunchedEffect(drawerState.currentValue) {
        if (drawerState.currentValue == DrawerValue.Open) drawerTick++
    }

    // 贴底跟随：流式期间对齐网页 paint()（app.js:1556）——本来贴底才滚，上翻阅读不被打断。
    //
    // v0.28 把"贴底"的算法从"最后一项高出视口的部分"换成**顶到滚动边界**
    // （scrollToItem(last, MAX_SCROLL_OFFSET)）：旧算法拿上一帧量到的高度算 offset，
    // 而落定的那一拍消息条目还会**事后长高**——操作行（复制/编辑/重新生成/删除）
    // 要等 streaming 标记落下才挂出来。高度一变，按旧高度贴的底就把条目尾巴推进了
    // 输入卡后面（真机反馈「输入框老是挡着上面的输出」，截图里操作行被切掉半截）。
    // 越界的 offset 会被 LazyList 夹到最大滚动位置——"贴到最底"从此不依赖高度测量，
    // 量多高都贴得实。落定分支的退出判据也换成结果导向：最后一项底边已经进了视口
    // 才算贴完（v0.28 当时是随帧重贴直到那一刻、上限 24 帧；v0.36.1 收敛成
    // "下一帧量一次、没进视口才补一发"，切会话整屏替换不再背着循环跑）。
    // v0.29.0 过程面板与贴底的共存：面板长高搭的是 content 帧重跑的顺风车；落定那一拍
    // 的无条件顶到底会把整块过程连同答案尾行一起送回眼前；生成中分支自带「只有近底才
    // 跟」闸门，上翻阅读不被打断。
    // v0.29.2 改判：顺风车没了。服务端把思考帧改成即产即发之后，"只想不说"的那几十秒
    // 里 streamText 一直是空串——面板在长高、列表却一动不动，最新那几行思考始终在视口
    // 外，用户看到的仍然是"憋半天没动静"。所以跟着面板的**变化量**重跑这条效果：
    // 步数与思考总字数。为什么不拿 streamTrace 列表本身当 key——连续思考是就地并进最后
    // 一步的（30 个分片不许变 30 行），列表身份不变，那样 key 永不触发，等于没修。
    // 贴底/夹边界的判据一个字没动（test_settled_bottom_and_title_ink 钉的就是那部分），
    // 生成中分支的近底闸门继续挡住"每个 token 把上翻阅读的人拽回底部"。
    // v0.39 性能 A 包再改判：跟随的一半拆出去独立成 LaunchedEffect(streamActive) 的
    // 80ms 环（下面紧跟着那条），这里的 key 从 streamText 换成 streamActive——
    // 组合期逐 token 读正文本身就是病根，面板变化量（步数 + 思考总字数）留在 key 里
    // 继续叫醒"落定"那一支的判据重估；落定分支照旧在协程内读 streamText == null。
    val streamTraceChars = streamTrace.sumOf { it.text.length }
    LaunchedEffect(messages.size, streamActive, streamTrace.size, streamTraceChars) {
        val first = listState.layoutInfo
        val total = first.totalItemsCount
        if (total == 0) return@LaunchedEffect

        if (streamText == null && !busy) {
            // 落定：无视中途上翻，把答案的最后一行送回眼前（对齐网页 renderMessages
            // 的无条件 scrollTop=scrollHeight）。
            // v0.36.1 改判：从前这里是一条最多 24 帧的随帧重贴循环，而切会话
            // messages 整屏替换那一拍也会触发它——一进会话就要连贴十几帧的"回底"，
            // 正是首帧观感卡顿的组成部分。收敛成"先把最后一项拉进视口，下一帧量一次"。
            // v0.36.1 审查批再收敛半步：只量一次的话，操作行/图片在量过之后才长高时，
            // 尾巴还是可能再藏进输入卡。改成【有界】短校正：最多
            // MAX_BOTTOM_CORRECTIONS(=3) 轮帧检，每轮量一次底边、没进视口才补一发
            // 夹边界；最后一项已经不在视口里（用户上翻走开）就立刻收手，不跟他抢滚动。
            // 有界性证据（静态锁钉死，见 test_native_ui_smoothness_v0361）：整个落定
            // 分支只有这一枚 repeat(MAX_BOTTOM_CORRECTIONS)，没有 while、没有别的
            // repeat——计数器轮数写死在常量里，不随布局结果续期，绝不是 24 帧循环复活。
            listState.scrollToItem(total - 1, 0)
            repeat(MAX_BOTTOM_CORRECTIONS) {
                withFrameNanos { }
                val now = listState.layoutInfo
                val t = now.totalItemsCount
                if (t == 0) return@LaunchedEffect
                val last = now.visibleItemsInfo.lastOrNull { it.index == t - 1 }
                // 1.6.8 的 ListItemInfo 没有 .end：底边就是 offset + size。
                if (last == null) return@LaunchedEffect   // 已离近底：这轮校正不归它管
                if (last.offset + last.size <= now.viewportEndOffset) return@LaunchedEffect
                listState.scrollToItem(t - 1, MAX_SCROLL_OFFSET)
            }
        }
    }

    // v0.39 性能 A 包：流式跟随独立环——起流（streamActive=true）时开跑，每轮先
    // delay(STREAM_FOLLOW_MS) 再判"近底"：只有本来就在底部附近（lastVisible >=
    // total-2，与搬走前同一道闸）才把列表顶到滚动边界；**无变化不滚**（见下）。
    // streamText 在这里是
    // **协程内**读取（退出条件），不是组合期 key；收尾那拍 streamActive 翻假，
    // LaunchedEffect 重入、循环条件不成立即退——面板长高的"只想不说"阶段由这环
    // 覆盖（streamText 是空串而非 null，条件成立），不再借落定效果的顺风车。
    // v0.39 修复批：环只在"这一拍真的变了"时才滚。孤儿流场景——用户从 A 切到 B
    // 后 A 的流静默（没有帧触发孤儿清扫，streamText 仍是 A 的旧串、streamActive
    // 仍真），没有这道比较时环会在 B 上每 80ms 滚一次底、劫走用户的滚动位置。
    // 判据是记住上一拍的（正文长度, 过程步数, 思考总字数），三者都没变就
    // continue；变了才更新标记、再走近底闸门。两个形状细节：初值取环启动那一刻
    // （streamText 此刻可能是 null，故 ?: -1），起流第一拍不白滚；标记更新排在
    // 近底判断**之前**——用户上翻时也算"已看见这次变化"，不在他回到底部后补滚。
    LaunchedEffect(streamActive) {
        var lastTextLen = streamText?.length ?: -1
        var lastTraceSize = streamTrace.size
        var lastTraceChars = streamTrace.sumOf { it.text.length }
        while (streamActive && streamText != null) {
            delay(STREAM_FOLLOW_MS)
            val text = streamText.orEmpty()
            val traceSize = streamTrace.size
            val traceChars = streamTrace.sumOf { it.text.length }
            if (text.length == lastTextLen && traceSize == lastTraceSize &&
                traceChars == lastTraceChars)
                continue
            lastTextLen = text.length
            lastTraceSize = traceSize
            lastTraceChars = traceChars
            val info = listState.layoutInfo
            val total = info.totalItemsCount
            if (total == 0) continue
            val lastVisible = info.visibleItemsInfo.lastOrNull()?.index ?: -1
            if (lastVisible < total - 2) continue
            listState.scrollToItem(total - 1, MAX_SCROLL_OFFSET)
        }
    }

    // v0.28：贴底与否——最后一项的底边在不在视口里。悬浮「跳到底部」小箭头据此
    // 出现/收起；上面的贴底逻辑每帧也靠同一判据收工，两处共用一份真相。
    var atBottom by remember { mutableStateOf(true) }
    LaunchedEffect(listState) {
        snapshotFlow {
            val i = listState.layoutInfo
            val t = i.totalItemsCount
            if (t == 0) true
            else (i.visibleItemsInfo.lastOrNull { it.index == t - 1 }
                ?.let { it.offset + it.size <= i.viewportEndOffset } ?: false)
        }.collect { atBottom = it }
    }

    // 复制提示 1.5s 后回到「复制」（网页 setTimeout 同语义）
    LaunchedEffect(copyTip) {
        val tip = copyTip ?: return@LaunchedEffect
        delay(1500)
        if (copyTip == tip) copyTip = null
    }

    // ---------- 附件（＋菜单三格：相机 / 图片 / 文件） ----------
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri == null) return@rememberLauncherForActivityResult
        scope.launch {
            runCatching {
                val bytes = ctx.contentResolver.openInputStream(uri)?.use { it.readBytes() }
                    ?: throw Exception("读取附件失败")
                var name = "file.bin"
                ctx.contentResolver.query(uri,
                    arrayOf(android.provider.OpenableColumns.DISPLAY_NAME), null, null, null)
                    ?.use { c -> if (c.moveToFirst()) name = c.getString(0) ?: name }
                val mime = ctx.contentResolver.getType(uri) ?: "application/octet-stream"
                Api.upload(bytes, name, mime)
            }.onSuccess { u ->
                pending = pending + AttItem(u.id, u.name, u.kind, u.size)
            }.onFailure { setStatus("附件上传失败：" + it.message, true) }
        }
    }
    val camera = rememberLauncherForActivityResult(
        ActivityResultContracts.TakePicturePreview()) { bmp ->
        if (bmp == null) return@rememberLauncherForActivityResult
        scope.launch {
            val out = java.io.ByteArrayOutputStream()
            bmp.compress(android.graphics.Bitmap.CompressFormat.JPEG, 90, out)
            runCatching { Api.upload(out.toByteArray(), "camera.jpg", "image/jpeg") }
                .onSuccess { u -> pending = pending + AttItem(u.id, u.name, u.kind, u.size) }
                .onFailure { setStatus("拍照上传失败：" + it.message, true) }
        }
    }

    // ---------- 持久化与出站（app.js replaceMessages / outbound 的原生对位） ----------
    /** 返回是否落到了服务端。true 也涵盖"本地草稿会话根本没 id"——那没什么可落。 */
    suspend fun persist(list: List<UiMsg>): Boolean {
        if (sessionId.isEmpty()) return true
        return try {
            // trace 必须随行回写：整份历史是 PUT 全量替换，这一句不带 trace，
            // 每一次保存都会把服务端落好的过程留痕抹平（v0.29.0 落盘回放）。
            Api.replaceMessages(sessionId, list.filter { !it.transient }
                .map { StoredMessage(it.role, it.content, it.messageId, it.model,
                    trace = traceToJson(it.trace)) })
            NetMinder.noteSuccess()
            true
        } catch (e: Exception) {
            NetMinder.noteFailure(e)
            when {
                logoutIf401(e) -> false
                // 7.1-4 多端场景：会话在别的端被删了。提示后会话刷新——
                // 回空态重开，绝不让用户对着一条不存在的会话继续编辑重试。
                e is ApiException && e.status == 404 -> {
                    setStatus("这个会话已在其他端被删除，已为你刷新", true)
                    sessionEpoch++   // v0.36.1：这条会话已经没了，在途的读取/落地一并作废
                    sessionId = ""
                    Prefs.lastSessionId = ""
                    messages = emptyList()
                    sessionLoading = false
                    editingIndex = null; editingDraft = null
                    runCatching { sessions = Api.listSessions() }
                    false
                }
                else -> { setStatus("同步到服务端失败：" + e.message, true); false }
            }
        }
    }

    /* 轻量整理与发送的同一份切片：先按本会话的 drop 从最早处切掉，再滤空/瞬时条。
     * clientCtx 与 outboundMsgs 各自 .drop( 同一张 contextDropBySession 的同一个数——
     * 两边各维护一套 offset 迟早分叉（网页 Task 4 的同一教训）。 */
    fun clientCtx(): ClientCtx {
        val drop = contextDropBySession[sessionId] ?: 0
        val msgs = messages.drop(drop).filter { it.content.isNotEmpty() && !it.transient }
            .map { ChatMessageDto(it.role, it.content) }
        val persona = Prefs.persona(sessionId)
        val personaTokens = if (persona.isNotEmpty()) Api.estimateTokens(persona) + 4 else 0
        val historyTokens = msgs.sumOf { Api.estimateTokens(it.content) + 4 }
        return ClientCtx(personaTokens, historyTokens, msgs, persona)
    }

    /** 实发组装：drop 之后按「这个模型的」预算截断（Prefs.contextBudgetK(pid)），
     *  读数侧对模型上限的 min 由 Api.outbound 那层防线兜住。K→token 的 ×1000 在 Api 内。 */
    fun outboundMsgs(providerId: String): List<ChatMessageDto> {
        val drop = contextDropBySession[sessionId] ?: 0
        return Api.outbound(messages.drop(drop)
            .filter { it.content.isNotEmpty() && !it.transient }
            .map { ChatMessageDto(it.role, it.content) },
            Prefs.persona(sessionId), Prefs.contextBudgetK(providerId), modelCapK())
    }

    /** 环与账本的读数（token 原值算，K 只为显示）：与 outboundMsgs 同一条 drop、
     *  同一个 pid（currentProvider 是唯一来源）；校准锚点吃「会话:模型」那一份 usage。 */
    fun contextRingInfo(): ContextRingInfo {
        ringTick   // 预算写在 SharedPreferences 里，快照看不见：每次落笔手动敲一拍
        val cm = currentProvider()
        val pid = cm?.id ?: Prefs.defaultProviderId
        val ctx = clientCtx()
        val known = ctx.personaTokens + ctx.historyTokens          // token 原值
        val last = lastUsageByKey["$sessionId:$pid"]
        val lastClient = lastClientKByKey["$sessionId:$pid"] ?: 0
        val used = Api.calibrateUsedK(known, last?.prompt_tokens ?: 0, lastClient)
        val capK = modelCapK()
        val budgetK = if (cm != null) minOf(Prefs.contextBudgetK(pid), capK) else 0
        val budgetTokens = maxOf(1, budgetK * 1000)                // K → token：显式换算
        val pct = if (cm == null) 0 else minOf(100, used * 100 / budgetTokens)
        val inputTokens = Api.estimateTokens(input.text)
        return ContextRingInfo(
            available = cm != null,
            modelName = cm?.name ?: "",
            providerId = pid,
            personaTokens = ctx.personaTokens,
            historyTokens = ctx.historyTokens,
            inputTokens = inputTokens,
            knownTokens = known,
            usedTokens = used,
            usedK = Math.round(used / 1000.0).toInt(),  // token → K：显示口径显式换算
            residualTokens = Api.contextSegments(used, ctx.personaTokens,
                ctx.historyTokens, inputTokens),
            budgetK = budgetK,
            capK = capK,
            pct = pct,
            hasUsage = last != null,
            lastPromptTokens = last?.prompt_tokens ?: 0,
            lastClientTokens = lastClient,
            dropped = contextDropBySession[sessionId] ?: 0,
        )
    }

    /* 轻量整理：按当前模型的预算算出要吐掉几条最早的历史，条数累加进本会话的 drop。
     * 只改这张表，绝不删 messages——历史原文是用户资产；outboundMsgs/clientCtx 都读
     * 同一张表，所以省下来的每一 K 都是真不发出去的。 */
    fun applyContextTidy() {
        val cm = currentProvider() ?: return
        val ctx = clientCtx()
        val kept = Api.dropOldestToBudget(ctx.msgs, Prefs.contextBudgetK(cm.id) * 1000)
        val dropped = maxOf(0, ctx.msgs.size - kept.size)
        if (dropped <= 0) {
            setStatus("已在预算内，无需整理")
            return
        }
        contextDropBySession[sessionId] = (contextDropBySession[sessionId] ?: 0) + dropped
        setStatus("已从本轮发送移除最早 $dropped 条；历史原文未动")
    }

    /** 恢复：本会话的整理清零，下一轮发送重新带上全部历史。 */
    fun resetContextTidy() {
        if ((contextDropBySession[sessionId] ?: 0) == 0) {
            setStatus("本轮没有整理过，无需恢复")
            return
        }
        contextDropBySession.remove(sessionId)
        setStatus("已恢复本轮全部历史")
    }

    /* app.js ensureSession：会话不在清单里（或压根还没有）才建一条，model 记当前 provider。 */
    suspend fun ensureSession(): Boolean {
        if (sessionId.isNotEmpty() && sessions.any { it.session_id == sessionId }) return true
        return try {
            val created = Api.createSession(currentProvider()?.id)
            sessionId = created.session_id
            Prefs.lastSessionId = created.session_id
            messages = emptyList()
            runCatching { sessions = Api.listSessions() }
            true
        } catch (e: Exception) {
            NetMinder.noteFailure(e)
            if (!logoutIf401(e)) setStatus("会话创建失败：" + e.message, true)
            false
        }
    }

    /* app.js pullFromHistory：断线续不上、或服务端判这一轮不可续播时，去会话历史里取回
     * 已经落库的助手回复。关键是"不重发生成"：那一轮的钱要么已经花、要么被服务端的轮次
     * 边界钱闸停在了边界，结果都在 sessions 里；重发只会再付一次、再落一条一样的助手消息。
     * 取回只渲染，不再 PUT 覆盖——历史里那条就是这一轮的最终版。
     * v0.36.1 审查批：这条也是「请求→落地」路径，照 openSession 补守卫——出发时扣下
     * 请求号与当时的会话 id，回包（以及失败分支）落地前两样都必须还是当前的，否则整包
     * 丢弃。断线取回往往慢，期间用户完全可能切了会话；旧会话的答案绝不能追加到
     * 新会话的屏幕上（那是脱敏之外的第二种"串门"）。 */
    suspend fun pullFromHistory() {
        setStatus("这一轮接不上了，正在去会话里取回结果…")
        val epochAtRequest = sessionEpoch
        val sidAtRequest = sessionId
        try {
            val d = Api.getSession(sidAtRequest)
            if (epochAtRequest != sessionEpoch || sidAtRequest != sessionId) return
            val lastAssistant = d.messages.lastOrNull { it.role == "assistant" }
            if (lastAssistant != null) {
                messages = messages + UiMsg("assistant", lastAssistant.content,
                    lastAssistant.message_id, lastAssistant.model,
                    // 断线取回的不止正文：这一轮的过程在服务端已经落盘，取回来照样摊得开
                    trace = traceFromStored(lastAssistant.trace))
                setStatus("")
            } else {
                // 历史里还没有这一轮的助手消息（多半服务端还在收尾）：明说，别重发。
                val tip = "没能在会话里找到这一轮的结果，稍后刷新再看；不用重发同一条"
                messages = messages + UiMsg("assistant", tip, transient = true)
                setStatus(tip, true)
            }
        } catch (e: Exception) {
            NetMinder.noteFailure(e)
            // 失败分支同样守卫：切了会话就别把旧会话的取回报错甩在新屏上。
            if (epochAtRequest != sessionEpoch || sidAtRequest != sessionId) return
            messages = messages + UiMsg("assistant",
                "⚠️ " + (e.message ?: "取回失败"), transient = true)
            if (!logoutIf401(e)) setStatus(e.message ?: "", true)
        }
    }

    /* ---------------- runStream（app.js send/runStream 的移植 · v0.25 安卓 T2.4/T2.5/T2.8） ----------------
     * content 增量 → 局部气泡重画；done 收尾按 status 分流：
     *   completed → 落 message_id/model；cancelled → 半截 +「（已停止生成）」照样落本地并持久化；
     *   cannot_resume → 不重发，去会话历史取回这一轮已落库的最终答案；
     * error 帧（模型真故障）→ ⚠️ 占位气泡，不重发、不取历史；
     * 流非正常结束（断线）→ Api.streamChat 内核里带 Last-Event-ID 续播，续不上/到上限抛
     * StreamNeedHistoryException → 去会话里取回。**绝不回头 POST /v1/chat 重发整轮**——
     * 老代码 `retryable → Api.chat(payload)` 那一手正是断线/退后台时再付一次钱、再落一条
     * 一样助手消息的病根。停止 = 先打服务端 cancel（run_id 来自 onRun），本地取消只是兜底。 */
    /* 接力触发槽：Kotlin 局部函数不能前向引用，streamInto 够不到后面才定义的
     * maybeRelay/sendNow——先声明空槽，sendNow/maybeRelay 全部就位后再接线。 */
    var afterStreamEnd: (String, Int, Boolean) -> Unit = { _, _, _ -> }
    fun streamInto() {
        if (busy) return
        streamJob = scope.launch {
            // v0.36.1 发版前审查批：runStream 主落点的跨会话守卫（同 openSession /
            // pullFromHistory 的机制，扣的换成发流这一刻的请求号与会话 id）。用户从 A
            // 切到 B 之后，A 这一枪的流式回答绝不能 append 进 B 的 messages、更不能
            // persist 回 B——不匹配的帧与终局落地整包丢弃。
            // 有意不硬断仍在跑的流：本地 cancel 会改服务端的轮次走向（cancel 与任其
            // 跑完是两笔账），计费语义不动。A 的这轮由服务端照常收尾落库，用户回到 A
            // 经 openSession 取回——这是"先不落错会话"下的可接受最小行为。
            val epochAtSend = sessionEpoch
            val sidAtSend = sessionId
            fun mine() = epochAtSend == sessionEpoch && sidAtSend == sessionId
            busy = true; stopRequested = false
            currentRunId = null
            streamText = ""
            // 顺序有讲究：streamActive 在 streamText 落好之后翻——跟随环的退出条件
            // 读 streamText != null，先翻表会让环在"槽位还是 null"的那一拍直接空转退出。
            streamActive = true
            streamTrace = emptyList(); streamTraceOpen = true; streamTraceTouched = false
            streamApproval = null
            setStatus("生成中…")
            val acc = StringBuilder()
            // 过程步的本地累积（网页 holder.trace 的原生对位）：只 append 不重排，
            // 每帧发布一份快照给组合读（思考帧服务端已按 120 字/0.4 秒攒片，频率可控）。
            val traceAcc = ArrayList<TraceItem>()
            fun publishTrace() { streamTrace = traceAcc.toList() }
            var doneTrace = emptyList<JsonObject>()   // done 带回的权威步序（服务端裁过）
            // 落定形状：done.trace 是含 omitted 的权威版，优先用它——屏幕上的过程与
            // 重开会话后看到的才是同一份；没有（老服务端/半途断）就用本地累积收掉未决步。
            fun settledTrace(): List<TraceItem> =
                if (doneTrace.isNotEmpty()) closePendingTrace(traceFromStored(doneTrace))
                else closePendingTrace(traceAcc.toList())
            // 面板开合：用户点过就带走他的选择；没点过，落定即自动折叠。
            fun settledExpanded(): Boolean = streamTraceTouched && streamTraceOpen
            var doneId: String? = null
            var doneModel: String? = null
            var doneStatus = ""
            var doneFullText = ""
            var failed = false
            var failMsg = ""
            var stopped = false
            var needHistory = false
            // 错帧只扫一次屏：孤儿流的临时气泡不必每帧都清一遍
            var staleSwept = false
            // v0.39 性能 A 包：发布 ticker（try 内启动、finally 先 cancel 再清槽）。
            var publishJob: Job? = null
            // v0.35 #99：流上出现挂起前缀 = "去读一次任务"。它只是触发器，不是状态；
            // 帧上没有任务身份，真正在等哪一条由列表 + 详情两个 GET 回答。
            var sawSuspend = false
            // v0.39 修复批：pid/atts/payload 的构造三行整体挪进 try 内的第一段。
            // 留在 try 外时任一抛错（provider 恰好被删、消息体形状意外）都会绕过
            // catch/finally——busy、streamText、streamActive 永久卡在真值上：busy
            // 是发消息的闸，streamActive 还把上面的跟随环钉死在空转。构造仍排在
            // publishJob = launch 之前：payload 抛错时 ticker 还没起来，不会留下孤儿快照。
            try {
                val pid = currentProvider()?.id
                // v0.39.x 驾驶舱：出发锚点——usage 与校准 clientK 都只写给「出发时那个
                // 会话+模型」的账（Done 分支过 mine() 闸才落）；预算与 payload 的 provider
                // 同源（currentProvider 是唯一来源），各读各的必分叉。
                val usageKey = "$sidAtSend:${pid.orEmpty()}"
                val clientKAtSend = clientCtx().let { it.personaTokens + it.historyTokens }
                val atts = messages.lastOrNull { it.role == "user" }?.attachments
                    ?.map { it.id } ?: emptyList()
                val payload = Api.chatPayload(pid, pid, outboundMsgs(pid.orEmpty()), atts, sessionId)
                // v0.39 性能 A 包：content 帧不再逐条写 streamText——只进 acc；屏上由
                // 这条 40ms 的 ticker 发布最新快照（无变化不写，省一次整页重组）。
                // mine() 为假立刻停笔：A 的孤儿流不许把字画到 B 的卡上——守卫比"照发"
                // 多花的这一行，正是会话切换竞态的旧账（v0.36.1 审查批同一判据）。
                publishJob = launch {
                    while (isActive) {
                        delay(STREAM_PUBLISH_MS)
                        if (!mine()) break
                        val snapshot = acc.toString()
                        if (snapshot != streamText) streamText = snapshot
                    }
                }
                Api.streamChat(payload,
                    // 断线重连那句人话直接落状态条——但只对号的那一拍才落，
                    // 切了会话就不许再占用 B 屏幕上的状态行。
                    onStatus = { if (mine()) setStatus(it) },
                    // run_id 不设守卫是刻意的：它是「停止」要打的那一枪的靶子，不是
                    // 画面/持久化落点。认下最新 run_id，用户切了会话按停止也够得着
                    // 真在跑的那条服务端 run，计费语义不变。
                    onRun = { currentRunId = it }    // 记住 run_id：停止打 cancel、续播也认它
                ).collect { ev ->
                    // 流式期间切了会话：这一帧整包丢弃，屏上/list/状态行一概不碰；
                    // 头一帧对不上号时顺手把这一轮挂在屏上的临时气泡与过程撤下，
                    // 别让 A 的半截字继续画在 B 的卡上（B 的卡是 live 的，读的就是
                    // 这些全局临时槽）。
                    if (!mine()) {
                        if (!staleSwept) {
                            staleSwept = true
                            streamText = null; streamTrace = emptyList(); streamApproval = null
                        }
                        return@collect
                    }
                    if (stopRequested) throw CancellationException("stop")
                    when (ev) {
                        is ChatEvent.Start -> if (ev.runId.isNotEmpty()) currentRunId = ev.runId
                        is ChatEvent.Content -> acc.append(ev.text)
                        // v0.29.0 过程帧：与网页 accumulateTrace 同规则 —— 连续 thinking
                        // 并进最后一步（30 个分片不许变 30 行）、tool_result 按 id 认领它的
                        // tool_call（id 对不上就退化成配最近的 pending）、search 一步一行。
                        is ChatEvent.Thinking -> {
                            val text = ev.text
                            if (text.isNotEmpty()) {
                                val last = traceAcc.lastIndex
                                if (last >= 0 && traceAcc[last].kind == "thinking")
                                    traceAcc[last] = traceAcc[last].copy(
                                        text = traceAcc[last].text + text)
                                else traceAcc.add(TraceItem("thinking", text = text))
                                publishTrace()
                            }
                        }
                        is ChatEvent.ToolCall -> {
                            // 参数（arguments/arguments_text）这一版不占行：网页面板也没画，
                            // 折叠着读的是 label；帧照收不误，界面上多一格是一期的事。
                            traceAcc.add(TraceItem("tool", id = ev.id, name = ev.name,
                                label = ev.label.ifEmpty { ev.name }, pending = true))
                            publishTrace()
                        }
                        is ChatEvent.ToolResult -> {
                            // 挂起这一枪只说明"值得去读一次任务"：逐字前缀取自
                            // backend/app/tools/response.py，判据仍只认 GET 回来的状态。
                            if (ev.summary.startsWith(TaskCard.SUSPEND_PREFIX)) sawSuspend = true
                            val at = traceAcc.indexOfLast {
                                it.kind == "tool" && it.pending &&
                                    (ev.id.isEmpty() || it.id == ev.id)
                            }
                            if (at < 0) {
                                traceAcc.add(TraceItem("tool", id = ev.id, name = ev.name,
                                    label = ev.name.ifEmpty { "工具调用" },
                                    ok = ev.ok, summary = ev.summary,
                                    elapsedMs = ev.elapsedMs, truncated = ev.truncated))
                            } else {
                                traceAcc[at] = traceAcc[at].copy(pending = false,
                                    name = ev.name.ifEmpty { traceAcc[at].name },
                                    ok = ev.ok, summary = ev.summary,
                                    elapsedMs = ev.elapsedMs, truncated = ev.truncated)
                            }
                            publishTrace()
                        }
                        is ChatEvent.Search -> {
                            traceAcc.add(TraceItem("search", id = ev.id, query = ev.query,
                                source = ev.source,
                                results = ev.results.map {
                                    TraceHit(it.title, it.url, it.snippet)
                                }))
                            publishTrace()
                        }
                        // v0.31 审批帧：不进 trace 面板，单独显示确认卡片。
                        is ChatEvent.ApprovalRequest -> {
                            streamApproval = ApprovalState(
                                ticketId = ev.ticketId, toolName = ev.toolName,
                                planSummary = ev.planSummary, expiresAt = ev.expiresAt)
                        }
                        // cancelled / cannot_resume 都不是终止帧：半句与"续不上"的信号都在流里，
                        // 等随后那条 done 收尾，这里不单独终结（与网页内核同一语义，别自创）。
                        is ChatEvent.Cancelled -> Unit
                        is ChatEvent.CannotResume -> Unit
                        is ChatEvent.Done -> {
                            doneId = ev.messageId; doneModel = ev.model
                            doneStatus = ev.status
                            if (ev.fullText.isNotEmpty()) doneFullText = ev.fullText
                            if (ev.trace.isNotEmpty()) doneTrace = ev.trace
                            // done.usage 落账（含 cancelled 终帧——服务端 billed 照发）：
                            // mine() 为假（切了会话）就不落，别让这一轮的用量记到新会话
                            // 头上；环读数吃快照 map，落账即自动跟上，不另起刷新链。
                            // 老服务端没有 usage 键 → null，这里什么都不做，收尾链路不依赖它。
                            val u = ev.usage
                            if (u != null && mine()) {
                                lastUsageByKey[usageKey] = u
                                lastClientKByKey[usageKey] = clientKAtSend
                            }
                        }
                        is ChatEvent.Failed -> { failed = true; failMsg = ev.message }
                        // 认不出的帧：不静默丢——内核已落 ChatEvent.Unknown，这里不打断正常收尾。
                        is ChatEvent.Unknown -> Unit
                    }
                }
                NetMinder.noteSuccess()
                // 终局落点（消息 append、状态行）整段先对号：切了会话，这一枪的
                // 收尾对屏幕就是废纸——不写 B 的列表、不占 B 的状态行。
                if (mine()) {
                    if (failed) {
                        // error 帧：模型真故障，原因已给出，绝不重发整轮去二次付费。
                        messages = messages + UiMsg("assistant",
                            (if (acc.isNotEmpty()) acc.toString() + "\n\n" else "") + "⚠️ " + failMsg,
                            transient = true, trace = settledTrace(),
                            traceExpanded = settledExpanded())
                        setStatus(failMsg, true)
                    } else when (doneStatus) {
                        "cancelled" -> {
                            stopped = true
                            val text = if (acc.isNotEmpty()) acc.toString() else doneFullText
                            messages = messages + UiMsg("assistant",
                                text + "\n\n（已停止生成）", doneId, doneModel?.ifBlank { null },
                                trace = settledTrace(), traceExpanded = settledExpanded())
                            setStatus("已停止生成")
                        }
                        "cannot_resume" -> needHistory = true
                        else -> {
                            val text = if (acc.isNotEmpty()) acc.toString() else doneFullText
                            messages = messages + UiMsg("assistant", text, doneId,
                                doneModel?.ifBlank { null },
                                trace = settledTrace(), traceExpanded = settledExpanded())
                        }
                    }
                }
            } catch (e: CancellationException) {
                // 本地取消的兜底一支：只有服务端 cancel 打不出去时才会走到这里。
                stopped = true
                if (mine()) {
                    messages = messages + UiMsg("assistant",
                        acc.toString() + "\n\n（已停止生成）", doneId, doneModel?.ifBlank { null },
                        trace = settledTrace(), traceExpanded = settledExpanded())
                    setStatus("已停止生成")
                }
            } catch (e: StreamNeedHistoryException) {
                // 410 / 续不上 / 试到上限：去会话里取回，不重发生成（这一枪到了服务端，不算断网）。
                needHistory = true
            } catch (e: Exception) {
                NetMinder.noteFailure(e)
                failed = true
                val msg = e.message ?: "请求失败"
                if (mine()) {
                    messages = messages + UiMsg("assistant",
                        (if (acc.isNotEmpty()) acc.toString() + "\n\n" else "") + "⚠️ " + msg,
                        transient = true, trace = settledTrace(),
                        traceExpanded = settledExpanded())
                    if (!logoutIf401(e)) setStatus(msg, true)
                }
            } finally {
                // finally 里这几行刻意不设守卫：streamText/busy 是全局单例流槽
                // （busy 闸在，A 的孤儿流没收尾前 B 根本起不了第二轮），这一枪
                // 终局时清掉它对任何会话都是事实；反过来若守卫掉不清，B 会被
                // busy=true 永久卡死发不出消息。这里只清临时显示态，不碰 messages、
                // 不 persist——那两样全在上面的对号分支里。
                // v0.39 性能 A 包：先撤 ticker 再清槽——反过来的话，被撤的那一拍
                // 可能把一份孤儿快照写回刚清空的 streamText。
                publishJob?.cancel()
                streamText = null; streamActive = false; busy = false; streamJob = null
                // 过程已经随 UiMsg 落地，流上的临时累积就地清空（面板不会双倍挂两份）
                streamTrace = emptyList()
            }
            // 取回也是落地：切了会话目标就作废——不能对着 B 去取回「这一轮」的结果。
            if (needHistory && mine()) pullFromHistory()
            // persistCurrent + loadSessions：标题可能因首条消息被服务端改掉（顶栏要跟着新）
            withContext(NonCancellable) {
                // persist 落地前必须对号：此刻 messages 是屏幕上的活列表（可能已是 B 的），
                // 不守卫就会把 A 的这一轮 PUT 进 B——更狠的变体是 B 还在加载中、列表为空，
                // 一发空 persist 把 B 的服务端历史整平。会话清单刷新取的是
                // 服务端真值、与这一轮内容无关，不设守卫照落。
                if (mine()) persist(messages)
                runCatching { sessions = Api.listSessions() }
                // 任务卡读的是「当前身份在等哪一条」：身份级状态、不是这一轮的消息落点，无需守卫。
                // 取消态下它自己会再抛 CancellationException——必须 runCatching 接住并留在
                // NonCancellable 块内：裸调一抛，后面的 afterStreamEnd 就被截掉，
                // forceNext 插话隐身（用户点了「立即插话」却什么都没有）。
                if (sawSuspend) runCatching { refreshTaskCard() }
                if (mine() && !failed && !stopped) setStatus("")
                // 收尾同一 job 内触发接力。normal 只认「真的好好说完了」：停止/失败/
                // 断线取回/服务端 cancelled、cannot_resume 全算异常——异常时不该在用户
                // 没留神的地方替他接着发消息（maybeRelay 收到 false 会把队列暂停）。
                val normal = !failed && !stopped && !needHistory && !stopRequested &&
                    doneStatus != "cancelled" && doneStatus != "cannot_resume"
                afterStreamEnd(sidAtSend, epochAtSend, normal)
            }
        }
    }

    /* app.js stop()：「停止」终于是一个动作，不再只是关页面/断连的副作用。
     * 主路径 = POST /v1/chat/stream/{run_id}/cancel（翻标志、尽力当场关上游、下一个付费轮
     * 不发生）；本地取消只是兜底——只在客户端断连等于没停生产，还把这个动作伪装成了"关页面"。
     * 取消成功后不本地断连：那条流会自己收到 cancelled→done 终帧并按 doneStatus=="cancelled"
     * 正常收尾（半句 +「（已停止生成）」照样落本地）。run_id 由 streamInto 的 onRun 抓来落在
     * currentRunId 上。次序钉死：服务端取消在本地取消前面。 */
    fun stopNow() {
        // 按「停止」=「别再替我自作主张往下发」：本会话队列先暂停，由用户点「继续队列」
        // 恢复。这一枪排在取消之前——不管 cancel 走哪条分支，接力都不该被这一下点击放行。
        queuePaused[queueKeyOf(sessionId)] = true
        val rid = currentRunId
        if (rid != null) {
            scope.launch {
                try {
                    Api.cancelStreamRun(rid)
                    return@launch          // 服务端已收到取消，让流自己的终帧收尾，不硬断本地连接
                } catch (e: Exception) {
                    // 取消这一枪没打出去（网络断了 / run 已终态 404）：落到下面的本地兜底。
                }
                stopRequested = true; streamJob?.cancel()
            }
            return
        }
        // 还没拿到 run_id（这轮尚未握手成功）：没有可取消的服务端 run，只能本地断。
        stopRequested = true; streamJob?.cancel()
    }

    /* IME 压制件（原本挂在语音闸门那段，v0.30 上移到这里）：
     * sendNow 发完消息要立刻收键盘，而局部函数不能前向引用——这套件必须在 sendNow 之前定义。
     * 第 9 轮：这 ROM 上键盘的 show 动画会吞掉同窗口里紧随的一次 hide() 调用——
     * 第 8 轮单发 stashIme 真机实测压不住（胶囊和键盘照样同屏）。改成连发补刀：
     * 先发落在动画里也无妨，后面 4 发（180/360/540/720ms）落在动画结束之后必收。
     * 必须 remember：键盘升降正会触发重组，裸 new Handler 会让 DisposableEffect
     * 的 key 每轮都变、把排队中的补刀全清掉——自己拆自己的台。 */
    val root = LocalView.current
    val keyboardController = LocalSoftwareKeyboardController.current
    val imeStash = remember { Handler(Looper.getMainLooper()) }
    DisposableEffect(imeStash) { onDispose { imeStash.removeCallbacksAndMessages(null) } }
    fun stashIme() {
        focusManager.clearFocus()
        runCatching { root.windowInsetsController?.hide(WindowInsetsCompat.Type.ime()) }
        for (i in 1..4) {
            imeStash.postDelayed({
                runCatching { root.windowInsetsController?.hide(WindowInsetsCompat.Type.ime()) }
            }, i * 180L)
        }
    }

    /* app.js send()：空输入且没有附件 → 什么都不做；清单还没回来先补拉一次；
     * 没有可用模型 → 指路文案（管理员/普通用户两句不一样）并打开「模型服务」。 */
    fun sendNow(text: String) {
        if (busy) {
            // 任务执行中点发送不再吞掉（网页 enqueueQueueItem 同语义）：正文+附件
            // 进当前会话队列，输入框与附件条一起清——用户按的就是"把它发出去"。
            val t = text.trim()
            if (t.isEmpty() && pending.isEmpty()) return
            val key = queueKeyOf(sessionId)
            val items = queues[key].orEmpty()
            if (items.size >= QUEUE_MAX_ITEMS ||
                items.sumOf { it.text.length } + t.length > QUEUE_MAX_CHARS) {
                setStatus("接力队列已满，先发掉一条或清空", true)
                return
            }
            val k = Math.round(Api.estimateTokens(t) / 1000.0).toInt().coerceAtLeast(1)
            queues[key] = items +
                QueuedMsg(java.util.UUID.randomUUID().toString(), t, pending.toList(), k)
            pending = emptyList()
            input = TextFieldValue("")
            setStatus("已加入接力队列")
            return
        }
        val t = text.trim()
        if (t.isEmpty() && pending.isEmpty()) return
        streamJob = null
        scope.launch {
            if (providers.isEmpty()) {
                // 冷启动清单还没回来的那几秒按了发送：先说一声在补拉，别让键像坏了
                //（v0.28.2 真机反馈 2；快线超时见 Api.metaClient，不会再挂 5 分钟）
                setStatus("正在读取模型清单…")
                loadModelsNow()
            }
            if (currentProvider() == null) {
                setStatus(if (Prefs.role == "admin")
                    "当前没有可用模型，请在「设置 → 模型服务」中配置"
                else
                    "当前没有可用模型：可在「设置 → 模型服务」用自己的 API Key 添加模型，或联系管理员配置", true)
                settingsPage = "providers"
                return@launch
            }
            if (!ensureSession()) return@launch
            val atts = pending.toList()
            pending = emptyList()
            messages = messages + UiMsg("user", t, attachments = atts)
            // 只清「发出去的正是输入框里那段」：按钮/IME/语音传的都成立；接力与插话
            // 发的是队列里的正文，此时输入框里可能是用户刚起笔的新草稿，绝不能抹。
            val echo = t == input.text.trim()
            if (echo) input = TextFieldValue("")
            // v0.30 用户诉求：发完问题键盘自动收起，省掉一次手动收键盘，直接看输出。
            // 用现成的 stashIme（clearFocus + 连发压 IME），不另起一套——单发 hide()
            // 在这 ROM 上会被 show 动画吞掉（第 8/9 轮真机判例）。
            stashIme()
            streamInto()
        }
    }

    /* 接力（网页 maybeRelay 的原生对位）：正常收尾且还是出发时那个会话才往下发。
     * 同步闸先过 canRelay；异常（没好好说完/会话或请求号对不上）不静默吞——把这条
     * 队列标暂停，让「继续队列」成为用户看得见、点得动的出口。 */
    fun maybeRelay(sidAtSend: String, epochAtSend: Int, normal: Boolean) {
        val key = queueKeyOf(sidAtSend)
        val items = queues[key].orEmpty()
        val sameSession = epochAtSend == sessionEpoch && sidAtSend == sessionId
        if (!canRelay(normal, sameSession, foreground, queuePaused[key] == true,
                busy, items.isNotEmpty())) {
            if (!normal || !sameSession) queuePaused[key] = true
            return
        }
        scope.launch {
            // 让 300ms：落定动画与状态行收尾走完再起飞下一条。这 300ms 是竞态窗口
            // ——用户可能又发了一条/切了会话/退到后台/按了暂停，所以醒来后全量重验。
            delay(300)
            val fresh = queues[key].orEmpty()
            if (!canRelay(true, epochAtSend == sessionEpoch && sidAtSend == sessionId,
                    foreground, queuePaused[key] == true, busy, fresh.isNotEmpty()) ||
                sendingQueueId != null) return@launch
            val item = fresh.first()
            if (relayImageBlocked(item.attachments,
                    currentProvider()?.supports_vision == true)) {
                queuePaused[key] = true
                setStatus("队首这条带图片，当前模型不支持视觉：换视觉模型或移除附件后点「继续」", true)
                return@launch
            }
            // 从这里到 sendNow 之间不允许任何挂起点：一 suspend，用户就可能在这空档
            // 又发了一轮，sendNow 的「忙时入队」会把这条重新塞回队列——重复发送。
            // sessions.any 是纯内存判断（零 suspend）：补这道判据是为了 sendNow 里的
            // ensureSession() 走「已在清单」支路立即返回 true，不跌进新建会话的挂起路径。
            if (currentProvider() != null && sessionId.isNotEmpty() &&
                sessions.any { it.session_id == sessionId }) {
                sendingQueueId = item.id
                queues[key] = fresh.drop(1)
                pending = item.attachments
                // 发完就地清闸是假清：sendNow 的 launch 还没跑起来、busy 还没置真时，
                // 第二道闸已经敞开，下一个 300ms 协程能穿闸重发。清除统一挪到
                // afterStreamEnd 入口（每轮收尾只清一次，对本轮和接力轮都成立）。
                sendNow(item.text)
            } else {
                queuePaused[key] = true
            }
        }
    }

    /* 立即插话（队列条上那条确认后走这里）：先过闸再离队——点一下就把消息弄丢不可接受，
     * 闸不过一律回队首 + 暂停 + 说明原因。忙时的发法是先立 forceNext 靶子再停止：
     * stopNow 的停是暂停与取消，不是发送信号，那条消息要等这一轮按旧坐标收尾后放行。 */
    fun sendQueuedNow(item: QueuedMsg) {
        val key = queueKeyOf(sessionId)
        val blocked = relayImageBlocked(item.attachments,
            currentProvider()?.supports_vision == true)
        if (blocked || currentProvider() == null || sessionId.isEmpty()) {
            val rest = queues[key].orEmpty()
            if (rest.none { it.id == item.id }) queues[key] = listOf(item) + rest
            queuePaused[key] = true
            setStatus(if (blocked)
                "这条带图片，当前模型不支持视觉：换视觉模型或移除附件后再插话"
            else
                "没有可用模型或会话还没建起来，这条已放回队列并暂停", true)
            return
        }
        queues[key] = queues[key].orEmpty().filter { it.id != item.id }
        if (busy && streamJob != null) {
            forceNext = ForceNext(sessionId, sessionEpoch, item)
            queuePaused[key] = true
            stopNow()
        } else {
            pending = item.attachments
            sendNow(item.text)
        }
    }

    // 接线接力触发槽（streamInto 只认这一个口子）：forceNext 优先——用户点名「就现在
    // 发这条」的那一条先走，队列暂停与否管不着它；没有插话待发射才轮到常规接力。
    afterStreamEnd = { sid, epoch, normal ->
        // 防重入第二道闸统一在每轮收尾入口清（不再由 maybeRelay 发完就地清）：
        // sendNow 的 launch 若还在排队，busy 未置真的窗口里闸必须仍立着。
        sendingQueueId = null
        val fn = forceNext
        if (fn != null) {
            if (fn.sid == sid && fn.epoch == epoch &&
                fn.sid == sessionId && fn.epoch == sessionEpoch) {
                // 四个条件缺一不可：前两个对「被停的那条流」恒真（靶就是它立的），
                // 真判据是后两个——当前屏幕仍是原会话。终帧前切了会话，A 的插话
                // 绝不能替 B 按下发送；对不上就落回下面的回队支路。
                forceNext = null
                queuePaused[queueKeyOf(fn.sid)] = true   // 插话吃掉这一轮，剩余等「继续」
                pending = fn.item.attachments
                sendNow(fn.item.text)
            } else {
                // 插话那轮半途切了会话/换了身份：放回**原会话**队列头并暂停，
                // 绝不发给中途换上的别的会话；宁可重复也不丢。
                val fk = queueKeyOf(fn.sid)
                val rest = queues[fk].orEmpty()
                if (rest.none { it.id == fn.item.id }) queues[fk] = listOf(fn.item) + rest
                forceNext = null
                queuePaused[fk] = true
                setStatus("插话那轮已切走：这条已放回原会话队列并暂停", true)
            }
        } else {
            maybeRelay(sid, epoch, normal)
        }
    }

    /* ---------------- 按住说话（VoiceUi 接线，放在 sendNow 之后：局部函数不能前向引用） ----------------
     * 只在输入框为空、没在生成时接管长按；松手出字直接 sendNow，上滑取消。 */
    val voice = rememberVoiceRecorder()
    var voiceCancel by remember { mutableStateOf(false) }
    val micPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) setStatus("麦克风打开了，按住说话就行")
        else setStatus("没让用麦克风，语音暂时用不了；想用的话再长按一次输入框", true)
    }
    // 识别服务自己拒录音（权限明明给了）：不再拉起系统语音界面兜底——那东西在
    // 部分 ROM 上会弹它自己的白框错误「似乎出错了呢(2)」，比不响还吓人
    //（2026-09-28 真机反馈第 3 轮）。第 4 轮：提示中性色 + 直达入口。第 5 轮再修：
    // 用户拿本 App「Fenver」的麦克风页来对答案——提示必须点名是哪个语音服务 App，
    // 入口按钮也带上它的名字；另给一条手动的「系统语音输入」备用通路（用户点按
    // 才拉起，不违反第 3 轮"不自动弹"的教训）。
    voice.onFinal = { t -> voiceHintOn = false; voiceServicePkg = null; engineDeadStreak = 0; sendNow(t) }
    voice.onError = { m -> setStatus(m, true) }
    voice.onMicServiceDenied = {
        val cands = speechServiceCandidates(ctx)
        voiceServicePkg = speechServicePackage(ctx) ?: cands.firstOrNull()?.pkg
        voiceServiceLabel = cands.firstOrNull { it.pkg == voiceServicePkg }?.label
        voiceHintOn = true
        val who = if (voiceServiceLabel != null) "「$voiceServiceLabel」" else "语音服务"
        setStatus("不肯录音的是" + who + "那个App自己（不是本助手），先打字聊；" +
            "点下面的按钮去给它开麦克风，或试系统语音输入")
    }
    // 第 6 轮真机反馈：默认「系统语音引擎」连不上**它自己的**服务器——长按闪一下
    // 蓝穹顶就回 11（SERVER_TERMINATED），开麦克风怎么给都没用。装回上次选的引擎，
    // 并给接线：连续两次引擎死且机器上不止一个候选 → 自动轮换下一个；用户手动
    // 锁过引擎就不自作主张，只把「换个语音引擎」弹窗递过去。
    voice.engine = remember(ctx) {
        val p = Prefs.voiceEnginePkg; val c = Prefs.voiceEngineCls
        if (p.isNotEmpty() && c.isNotEmpty()) ComponentName(p, c) else null
    }
    fun applyEngine(info: SpeechServiceInfo?) {
        Prefs.voiceEnginePkg = info?.pkg.orEmpty()
        Prefs.voiceEngineCls = info?.cls.orEmpty()
        voice.engine = info?.cls?.takeIf { it.isNotEmpty() }
            ?.let { ComponentName(info.pkg, it) }
        voice.destroy()   // 拆掉旧 binder：下次长按重绑新引擎
        engineDeadStreak = 0
    }
    voice.onEngineDead = { code ->
        val cands = speechServiceCandidates(ctx)
        val curPkg = voice.engine?.packageName ?: speechServicePackage(ctx)
        voiceServicePkg = curPkg
        voiceServiceLabel = cands.firstOrNull { it.pkg == curPkg }?.label
        voiceHintOn = true
        engineDeadStreak++
        val who = if (voiceServiceLabel != null) "「$voiceServiceLabel」" else "这个语音引擎"
        val manual = Prefs.voiceEngineCls.isNotEmpty()
        if (!manual && cands.size > 1 && engineDeadStreak >= 2) {
            val i = cands.indexOfFirst { it.pkg == curPkg }
            val nxt = cands[((i + 1) + cands.size) % cands.size]
            applyEngine(nxt)
            setStatus(who + "连不上它自己的服务器（$code），已自动切到「${nxt.label}」，再按住说一句试试")
        } else {
            setStatus(who + "连不上它自己的服务器（$code），开麦克风没用——点「换个语音引擎」挑一个能用的")
        }
    }
    val trySystemVoice = rememberSystemVoiceFallback(
        onText = { t -> voiceHintOn = false; sendNow(t) },
        onFail = { m -> setStatus(m) })
    // 键盘弹出时长按归文本框自己（选字），键盘没出来才接管为按住说话。
    // 第 3 轮只用 IME 内衬判——本机是 adjustResize 非 edge-to-edge，键盘把窗口
    // 整个顶扁、内衬恒为 0，门控在真机上形同虚设（第 4 轮反馈的根因）。
    // 双保险：内衬有值吃内衬，另外实测窗口可见区缩水。
    val density = LocalDensity.current
    val frameKeyboardUp = rememberKeyboardVisible()
    // 第 15 轮改判（用户真机：「点键盘下箭头收了键盘，输入框还停在打字态」）：
    // 第 14 轮拿焦点当键盘代理，收键盘不收焦点——信号永远卡在"有键盘"，胶囊回不去。
    // 焦点信号整个撤下，换 rememberKeyboardVisible 里的窗口绝对高度缩水信号：
    // 键盘起=窗被顶扁（本 App manifest 声明 adjustResize，第 9 轮真机已证），
    // 键盘收=窗弹回，无论用什么方式收都能测到。ime 内衬那路保留兜别的机型。
    val keyboardUp = WindowInsets.ime.getBottom(density) > 0 || frameKeyboardUp
    // root / keyboardController / imeStash / stashIme 已上移到 sendNow 之前：
    // v0.30 起「发送即收键盘」要在 sendNow 里调 stashIme，而局部函数不能前向引用。
    // 第 10 轮（用户点名「不希望出现键盘闪一下」，学豆包/DeepSeek）：语音闸门在位的
    // 窗口里根本不挂文本框——没框就没焦点可抢，键盘无从闪现。短按松手由 voiceHold 的
    // onTap 记一笔 fieldPending：挂回文本框、程序化聚焦并请出键盘（点按=打字，长按=说话）。
    var fieldPending by remember { mutableStateOf(false) }
    val voiceArmed = input.text.isEmpty() && !busy && !voice.listening &&
        !voiceMode && !keyboardUp && !fieldPending
    LaunchedEffect(fieldPending, keyboardUp) {
        if (!fieldPending) return@LaunchedEffect
        if (keyboardUp) fieldPending = false
        else {
            runCatching { inputFocus.requestFocus() }
            keyboardController?.show()
        }
    }
    val voiceHoldMod = Modifier.voiceHold(
        enabled = { input.text.isEmpty() && !busy && !voice.listening && (voiceMode || !keyboardUp) },
        // 第 10 轮：短按松手 = 想打字——记一笔 fieldPending 让下面重组挂回文本框请键盘。
        onTap = { if (!voiceMode) fieldPending = true },
        onStart = {
            if (!voice.available) {
                setStatus("这台设备没有语音识别服务，用不了按住说话", true)
            } else if (ctx.checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)
                != android.content.pm.PackageManager.PERMISSION_GRANTED
            ) {
                micPermission.launch(android.Manifest.permission.RECORD_AUDIO)
            } else {
                voiceCancel = false
                // 双保险压键盘：第 10 轮起闸门在位时文本框压根不挂载，正常路径这里
                // 是空转；只防极窄的重组间隙里键盘恰好在升的动画途中。
                stashIme()
                voice.start()
            }
        },
        onZone = { voiceCancel = it },
        onFinish = { cancelling -> voice.finish(!cancelling) },
    )
    // 语音模式开关：切过去先收键盘（stashIme：clearFocus+窗口控制器双保险，第 8 轮
    // 实测 clearFocus 单独用在这 ROM 上键盘不收），输入区整块变成「按住说话」；
    // 切回文字模式不自动弹键盘，用户点输入框自己来，省一次"莫名其妙键盘跳出来"。
    fun setVoiceMode(on: Boolean) {
        voiceMode = on
        attachOpen = false
        modelMenuOpen = false
        if (on) stashIme()
    }

    fun sendFeedback(index: Int, rating: Int) {
        val m = messages.getOrNull(index) ?: return
        if (m.messageId == null) {
            setStatus("这条回复缺少 message_id，无法提交反馈", true); return
        }
        scope.launch {
            // v0.36.1 发版前审查批：反馈是「请求→按 index 落地」路径——回包慢的
            // 那一程里切了会话，index 指向的就是 B 的第 index 行，落不得。
            val epochAtRequest = sessionEpoch
            val sidAtRequest = sessionId
            fun stillHere() = epochAtRequest == sessionEpoch && sidAtRequest == sessionId
            runCatching { Api.feedback(m.messageId, rating) }.onSuccess {
                if (!stillHere()) return@onSuccess
                // 网页：同值再点撤销；message_id 缺失那句错误在上方已拦
                messages = messages.mapIndexed { i, x ->
                    if (i == index) x.copy(feedback = if (x.feedback == rating) null else rating) else x
                }
            }.onFailure {
                if (!stillHere()) return@onFailure
                if (!logoutIf401(it)) setStatus("反馈失败：" + it.message, true)
            }
        }
    }

    // 顶栏标题 = syncTopTitle：在会话清单里查当前 id，查不到就是「新对话」
    val topTitle = sessions.firstOrNull { it.session_id == sessionId }?.title
        ?.ifBlank { "新对话" } ?: "新对话"

    fun closeDrawer() { scope.launch { drawerState.close() } }

    /* 任务卡片的可画字段只在"详情/在途"变了才重算：打字只改 draft，不该每次都把
     * 那份 JSON 再解析一遍。visible 为假就整块不挂——一条不在等人的任务不该占位。 */
    val taskUi = taskCard
    val taskView = remember(taskUi?.detailJson, taskUi?.busy) {
        taskUi?.let { TaskCard.view(it.detailJson, it.busy) }
    }

    // 第 11 轮改判（对齐 DeepSeek/豆包）：输入框恢复系统默认文字工具条——键盘起着
    // 时长按输入框出复制/粘贴/全选（光标旁）；键盘没起时输入位是占位条，单点不挂框
    // 不冒菜单、长按归语音。语音长按只在键盘收起（或语音模式）时接管，靠 voiceHold
    // 的 enabled 门控分流。
    // v0.39.x 驾驶舱：环/账本读数收进 remember——组合每拍都重算一遍全量估算太贵。
    // 修复轮 I-1：键必须按「值」入，不按 .size——usage/校准/整理都是往既有键上覆写，
    // size 恒定不变，换默认模型、同一会话第二次轻量整理、设置页改角色/预算回来，
    // 全都会吃旧缓存。先把 contextRingInfo 读的非快照态取成局部值，逐个入键；
    // ringTick 保留（预算住在 SharedPreferences，纯非快照写入仍靠它敲拍）。
    val ringProviderId = currentProvider()?.id ?: Prefs.defaultProviderId
    val ringBudget = Prefs.contextBudgetK(ringProviderId)
    val ringPersona = Prefs.persona(sessionId)
    val ringUsage = lastUsageByKey["$sessionId:$ringProviderId"]
    val ringClientK = lastClientKByKey["$sessionId:$ringProviderId"]
    val ringDrop = contextDropBySession[sessionId] ?: 0
    val ringQueue = queues[queueKeyOf(sessionId)]
    val ringInfo = remember(messages, input.text, ringTick, sessionId, sessionEpoch,
        providers, ringProviderId, ringBudget, ringPersona, ringUsage, ringClientK,
        ringDrop, ringQueue) { contextRingInfo() }
    // 队列的「待发」只在账本里做附注：usedK/pct 是实发口径，还没发出去的不许混进去。
    val queuedK = queues[queueKeyOf(sessionId)]?.sumOf { it.k } ?: 0
    ModalNavigationDrawer(
        drawerState = drawerState,
        // 第 14 轮（用户真机：按住语音向右滑，整页被抽屉手势拖开、录音卡跟着错位，
        // 「非常影响观感」）：录音进行中锁死抽屉滑动手势——手指正占在语音状态机上，
        // 任何横向拖拽都不该再被第二套手势抢走。松手退场即恢复。
        // （此参数名按 material3 BOM 2024.06.00 定；新版改名了，别照旧名写——CI 红过一次）
        gesturesEnabled = !voice.listening,
        drawerContent = {
            SessionDrawer(
                tick = drawerTick,
                currentId = sessionId,
                initialSessions = sessions,
                onNewChat = {
                    // 网页 newChat：先清空再 ensureSession —— 新对话立刻建出来
                    scope.launch {
                        sessionId = ""; Prefs.lastSessionId = ""
                        // v0.36.1：换会话根同样先作废在途的 getSession、清屏再走，
                        // 旧列表不许挂到"新对话"标题下。
                        // 动效批：这也是一次切换，打过标后走完整转场。
                        swappedOnce = true
                        sessionEpoch++; sessionLoading = false; messages = emptyList()
                        if (ensureSession()) closeDrawer()
                    }
                },
                onOpen = { id -> openSession(id); closeDrawer() },
                onMemory = { settingsPage = "memory"; closeDrawer() },
                onSettings = { settingsPage = ""; closeDrawer() },
                onModelService = { settingsPage = "providers"; closeDrawer() },
                onDeleted = { id ->
                    if (id == sessionId) {
                        swappedOnce = true   // 动效批：删当前会话清零也算一次切换
                        sessionEpoch++   // 在途的那一枪作废：删掉的会话不该再往屏上落字
                        sessionId = ""; Prefs.lastSessionId = ""; messages = emptyList()
                        sessionLoading = false
                    }
                    refreshSessions()
                },
                onRefreshSessions = { refreshSessionsNow() },
                onClose = { closeDrawer() },
            )
        },
    ) {
        Box(Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background)) {
            Column(Modifier.fillMaxSize()) {
                // ---------- 顶栏：56dp，菜单 44dp 可点区，右侧模型胶囊 ----------
                FenverTopBar(
                    title = topTitle,
                    leading = {
                        FenverTopBarAction(onClick = { scope.launch { drawerState.open() } }) {
                            FenverIcon(FenverIcon.Menu,
                                MaterialTheme.colorScheme.onSurfaceVariant,
                                Modifier.size(20.dp))
                        }
                    },
                    actions = {
                        if (providers.any { it.usable }) {
                            FenverChip(
                                text = currentProvider()?.name ?: "选择模型",
                                onClick = { modelMenuOpen = true; attachOpen = false },
                                trailing = {
                                    FenverIcon(FenverIcon.ChevronDown,
                                        MaterialTheme.colorScheme.onSurfaceVariant,
                                        Modifier.size(14.dp))
                                },
                            )
                        }
                    },
                )

                // ---------- v0.36.1 动效批：会话切换「挤入卡片」（方案 A） ----------
                // 只动消息区这一张卡（输入卡/顶栏/抽屉本体不在转场射程）。
                // AnimatedContent 以 sessionId 为 contentKey：流式 token、消息增减只换
                // 快照内容不换 key——同一张卡就地换内容，绝不触发转场；只有换会话才换
                // key 整卡对调。新卡从抽屉侧（左）轻微滑入、放大、淡入，旧卡向右轻微
                // 滑出、缩小、淡出；新卡 zIndex 恒高于旧卡（旧卡从底下退走）。
                // 旧卡画 card.msgs——切换那一刻的快照——退出过程不读新会话的 messages；
                // 交互回调全部 live 闸门把关，旧卡是一张只许看的过场海报。
                // 时长/缓动见文件级常量；变换走 AnimatedContent 自带的 graphicsLayer
                // 通道，本转场不在每帧重组里做任何测量级重活。
                AnimatedContent(
                    targetState = swapTarget,
                    contentKey = { it.sid },
                    transitionSpec = {
                        // 首次组合不播（swappedOnce 由真实切换入口打标）；
                        // 系统动画缩放为 0（用户关了动画）时旁路转场，直接换卡。
                        if (animScaleOff || !swappedOnce)
                            EnterTransition.None togetherWith ExitTransition.None
                        else {
                            // 中缀 togetherWith 必须跟在左值同一行（Kotlin 解析规则），
                            // 六段式写不清爽——先各自攒出 enter/exit 再拼。
                            // v0.36.1 审查批对齐口径：位移按 -12%/+18% 卡宽直写
                            // （旧的 -it/12≈-8.3%、it/5=+20% 与提案对不上）；乘积是
                            // Float，滑入偏移要 Int，收尾取整、判据仍锁在系数上。
                            val enter = slideInHorizontally(
                                animationSpec = tween(SESSION_SWAP_ENTER_MS,
                                    easing = SESSION_SWAP_EASING),
                                initialOffsetX = { (-it * 0.12f).toInt() }) +
                                scaleIn(
                                    animationSpec = tween(SESSION_SWAP_ENTER_MS,
                                        easing = SESSION_SWAP_EASING),
                                    initialScale = 0.96f) +
                                fadeIn(tween(SESSION_SWAP_ENTER_MS,
                                    easing = SESSION_SWAP_EASING))
                            val exit = slideOutHorizontally(
                                animationSpec = tween(SESSION_SWAP_EXIT_MS,
                                    easing = SESSION_SWAP_EASING),
                                targetOffsetX = { (it * 0.18f).toInt() }) +
                                scaleOut(
                                    animationSpec = tween(SESSION_SWAP_EXIT_MS,
                                        easing = SESSION_SWAP_EASING),
                                    targetScale = 0.94f) +
                                fadeOut(tween(SESSION_SWAP_EXIT_MS,
                                    easing = SESSION_SWAP_EASING))
                            enter togetherWith exit
                        }
                    },
                    modifier = Modifier.weight(1f).fillMaxWidth(),
                ) { card ->
                    // live = sessionId 当前指着的那张卡；旧卡一进入退场就不再 live。
                    val live = card.sid == sessionId
                    // LazyListState 只能挂一枚 LazyColumn：转场那一帧新旧两张共用
                    // listState 会当场炸。live 卡用外层 listState（贴底效果、近底
                    // 判据、悬浮箭头都钉在它上面）；旧卡拿一次性 state——220ms 的
                    // 退场不再关心滚动位置。
                    val areaListState = if (live) listState else rememberLazyListState()
                    Box(Modifier.fillMaxSize().zIndex(if (live) 1f else 0f)) {
                        // ---------- .messages：padding 14/12/6，条目间 gap 22 ----------
                        LazyColumn(Modifier.fillMaxWidth().imePadding()
                            .padding(horizontal = 12.dp),
                            state = areaListState,
                            contentPadding = PaddingValues(top = 14.dp, bottom = 6.dp),
                            verticalArrangement = Arrangement.spacedBy(22.dp)) {
                            if (card.msgs.isEmpty() && (!live || (!busy && !streamVisible))) {
                                if (card.loading) item {
                                    // v0.36.1 读取中：旧消息已清、空态英雄不画——一行了无负担的
                                    // 提示顶着；数据回来后 key 没变，在同一次转场里填上，
                                    // 不发生第二次转场。
                                    Text("正在读取会话…", fontSize = 13.sp, color = text3Color(),
                                        modifier = Modifier.padding(horizontal = 4.dp, vertical = 10.dp))
                                } else if (live) item {
                                    EmptyHero()
                                }
                            }
                            itemsIndexed(card.msgs, key = { _, m -> m.messageId ?: "local-${m.localId}" }, contentType = { _, m -> m.role }) { index, m ->
                                MessageRow(
                                    m = m,
                                    isLastAssistant = m.role == "assistant" &&
                                        index == card.msgs.lastIndex && (!live || !streamVisible),
                                    editing = live && editingIndex == index,
                                    streaming = live && (busy || streamVisible),
                                    seedDraft = if (live) editingDraft else null,
                                    copyTip = if (live) copyTip?.takeIf { it.first == index }?.second else null,
                                    onStartEdit = { if (live) { editingDraft = null; editingIndex = index } },
                                    onCancelEdit = { if (live) { editingIndex = null; editingDraft = null } },
                                    onSaveEdit = { text ->
                                        scope.launch {
                                            // 旧卡退场中：index 是快照的 index，拿它动全局
                                            // messages 会删错行——点不动才对。
                                            if (!live) return@launch
                                            // v0.36.1 发版前审查批：persist 是挂起点，保存
                                            // 在途时用户完全可能切了会话——回包之后的每一处
                                            // 落地（编辑态回填、消息列表、起重流）都必须还是
                                            // 出发那一号那一会话，否则整包作废。
                                            val epochAtAction = sessionEpoch
                                            val sidAtAction = sessionId
                                            fun stillHere() =
                                                epochAtAction == sessionEpoch &&
                                                    sidAtAction == sessionId
                                            val next = messages.take(index).toMutableList()
                                            next.add(messages[index].copy(content = text))
                                            // T2.2 AC-3：先落服务端，失败就退回编辑态且草稿原样保留
                                            // （网页同判据：commit 里 await 抛错就不 renderMessages，
                                            // 编辑框和字都还在，按下一次「保存」即重试）。
                                            if (!persist(next)) {
                                                if (stillHere()) {
                                                    if (index <= messages.lastIndex) {
                                                        editingIndex = index; editingDraft = text
                                                    } else { editingIndex = null; editingDraft = null }
                                                }
                                                return@launch
                                            }
                                            if (!stillHere()) return@launch
                                            editingIndex = null; editingDraft = null
                                            messages = next
                                            // 网页：编辑用户消息后重新生成后续（重走一轮流式）
                                            if (next[index].role == "user") streamInto()
                                        }
                                    },
                                    onCopy = { ok ->
                                        if (live) copyTip = index to (if (ok) "已复制" else "失败")
                                    },
                                    onDelete = {
                                        scope.launch {
                                            if (!live) return@launch
                                            // 同 onSaveEdit：persist 挂起期间切了会话，
                                            // 回包后不许把 A 的删行结果落成 B 的列表。
                                            val epochAtAction = sessionEpoch
                                            val sidAtAction = sessionId
                                            val next = messages.filterIndexed { i, _ -> i != index }
                                            persist(next)
                                            if (epochAtAction != sessionEpoch ||
                                                sidAtAction != sessionId) return@launch
                                            messages = next
                                        }
                                    },
                                    onRegen = {
                                        scope.launch {
                                            if (!live) return@launch
                                            val epochAtAction = sessionEpoch
                                            val sidAtAction = sessionId
                                            val keep = messages.take(index)
                                            persist(keep)
                                            // 挂起在途切了会话：截断列表与起重流都作废，
                                            // 否则 A 的截断挂到 B 上、A 的回答流进 B。
                                            if (epochAtAction != sessionEpoch ||
                                                sidAtAction != sessionId) return@launch
                                            messages = keep
                                            streamInto()
                                        }
                                    },
                                    onFeedback = { if (live) sendFeedback(index, it) },
                                    onToggleTrace = {
                                        // 人点过就尊重人的选择（落定时的"自动折叠"只没被碰过的面板）
                                        if (live) messages = messages.mapIndexed { i, x ->
                                            if (i == index)
                                                x.copy(traceExpanded = !x.traceExpanded) else x
                                        }
                                    },
                                    onOpenUrl = onOpenUrl,
                                )
                            }
                            // v0.39 性能 A 包：挂载条件吃派生量 streamVisible（组合期
                            // 只读"有没有正文"这一个布尔），streamText 的字符串读取
                            // 挪进 item 体内——StreamingBubble 拿最新快照重画它自己。
                            if (live && streamVisible) item {
                                    // 流式气泡属于"当下这张卡"：旧卡退场 220ms 里若新会话
                                    // 恰好在流式，不许把这轮的字画到旧卡上。
                                    StreamingBubble(streamText.orEmpty(), streamTrace, streamTraceOpen,
                                        onToggleTrace = {
                                    streamTraceTouched = true
                                    streamTraceOpen = !streamTraceOpen
                                },
                                        onOpenUrl = onOpenUrl,
                                        approval = streamApproval,
                                        onApprovalDecision = { decision ->
                                    val cur = streamApproval ?: return@StreamingBubble
                                    if (cur.resolved) return@StreamingBubble
                                    if (decision == "__timeout__") {
                                        // 超时作废：不 POST，只置灰卡片
                                        streamApproval = cur.copy(resolved = true, decision = "timeout")
                                        return@StreamingBubble
                                    }
                                    scope.launch {
                                        try {
                                            Api.confirmApproval(cur.ticketId, decision)
                                        } catch (_: Exception) { /* 404=票据失效，不杀界面 */ }
                                        streamApproval = cur.copy(resolved = true, decision = decision)
                                    }
                                })
                            }
                            // v0.35 #99：卡片挂在消息与流式气泡之后。它是"这一轮停下来等你"，
                            // 不该夹在历史中间；状态住在 ChatScreen 层，滚出视口再回来还是同一份。
                            // 动效批：任务卡同样只挂 live 卡——它是当前会话的状态，不是快照。
                            val tc = taskUi
                            val tv = taskView
                            if (live && tc != null && tv != null && tv.visible) {
                                item(key = "task-card") {
                                    TaskWaitingCard(
                                        view = tv,
                                        draft = tc.draft,
                                        busy = tc.busy,
                                        error = tc.error,
                                        onDraft = { t -> taskCard = taskCard?.copy(draft = t) },
                                        onSubmit = { submitTaskAnswer() },
                                    )
                                }
                            }
                        }
                    }
                }

                // ---------- .composer：附件行 / 建议 / 输入卡 / 下挂两张玻璃面板 ----------
                Column(Modifier.fillMaxWidth().imePadding()
                    .onSizeChanged { composerPx = it.height }
                    .padding(horizontal = 12.dp)
                    .padding(top = 4.dp, bottom = 12.dp)) {
                    if (pending.isNotEmpty())
                        AttRow(pending) { a -> pending = pending.filter { it.id != a.id } }
                    if (messages.isEmpty() && !busy && !streamVisible) {
                        // 网页 .suggestions 是 flex-wrap：换行摊开、每条都看得见读得完。
                        // 原来是横滑一行——第二条起就被屏幕切半，既看不清也懒得滑
                        //（2026-09-28 真机反馈"横向排列不够直观"）。
                        FlowRow(Modifier.fillMaxWidth().padding(bottom = 8.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalArrangement = Arrangement.spacedBy(8.dp)) {
                            CHAT_SUGGESTIONS.forEach { s ->
                                SuggestionPill(s) {
                                    // 光标落在句尾：填完标语直接接着打字，不用手动挪
                                    input = TextFieldValue(s, selection = TextRange(s.length))
                                    runCatching { inputFocus.requestFocus() }
                                }
                            }
                        }
                    }
                    // 接力队列条（网页 .queue-strip 对位）：只有待发才挂；暂停态写在
                    // 头行按钮上——排队的人要看得见「为什么还没发」和出口在哪。
                    val qKey = queueKeyOf(sessionId)
                    val queueItems = queues[qKey].orEmpty()
                    if (queueItems.isNotEmpty())
                        QueueStrip(
                            items = queueItems,
                            paused = queuePaused[qKey] == true,
                            onTogglePause = {
                                if (queuePaused[qKey] == true) {
                                    queuePaused[qKey] = false
                                    // 空闲时立刻接力（内部还有 300ms 全量重验）；
                                    // 忙时只解除暂停，等这一轮正常收尾自己接上。
                                    if (!busy) maybeRelay(sessionId, sessionEpoch, normal = true)
                                } else {
                                    queuePaused[qKey] = true
                                }
                            },
                            onEdit = { id, text ->
                                if (text.isEmpty()) {
                                    setStatus("改成空的了——想发就补几个字，不想发就删除这条", true)
                                } else {
                                    queues[qKey] = queues[qKey].orEmpty()
                                        .map { if (it.id == id) it.copy(text = text) else it }
                                }
                            },
                            onDelete = { id ->
                                val rest = queues[qKey].orEmpty().filter { it.id != id }
                                if (rest.isEmpty()) queues.remove(qKey) else queues[qKey] = rest
                            },
                            onShowNow = { item -> queueNowConfirm = item },
                        )
                    InputCard(
                        input = input, onInput = { input = it },
                        focusRequester = inputFocus,
                        onSendKey = { sendNow(input.text) },
                        attachOpen = attachOpen,
                        onToggleAttach = { attachOpen = !attachOpen; modelMenuOpen = false },
                        status = status, statusErr = statusErr, busy = busy,
                        chipModel = currentProvider()?.name ?: "选择模型",
                        chipVisible = providers.any { it.usable },
                        onToggleChip = { modelMenuOpen = true; attachOpen = false },
                        ringPct = ringInfo.pct, ringUsedK = ringInfo.usedK,
                        ringBudgetK = ringInfo.budgetK,
                        onRingClick = { contextPanelOpen = true },
                        onStop = { stopNow() },
                        onSend = { sendNow(input.text) },
                        canSend = input.text.isNotBlank() || pending.isNotEmpty(),
                        voiceMod = voiceHoldMod,
                        voiceMode = voiceMode,
                        onToggleVoice = { setVoiceMode(!voiceMode) },
                        keyboardUp = keyboardUp,
                        voiceArmed = voiceArmed,
                        voiceLive = voice.listening,
                        voiceHintOn = voiceHintOn,
                        voiceHintLabel = voiceServiceLabel,
                        onOpenVoiceService = {
                            val pkg = voiceServicePkg
                            if (pkg == null) {
                                setStatus("没找到语音服务App，去系统设置的「应用管理」里找手机自带的语音助手给它开麦克风")
                                return@InputCard
                            }
                            runCatching {
                                ctx.startActivity(
                                    Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                                        .setData(Uri.fromParts("package", pkg, null))
                                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                            }.onFailure { setStatus("跳不过去，请在系统设置里找到语音服务开麦克风") }
                        },
                        onTrySystemVoice = { trySystemVoice() },
                        onPickEngine = { enginePickerOpen = true },
                    )
                    // 引擎选择弹窗（第 6 轮）：默认引擎连不上自家服务器时，换引擎
                    // 才是正解。列全部候选 + 跟随系统默认 + 系统语音设置直达。
                    if (enginePickerOpen) {
                        // v0.36.1：组合阶段不查系统服务——候选由上面的 LaunchedEffect
                        // 在 IO 线程枚举好，这里只读结果；没回来时明说"正在查找"。
                        val cands = engineCands ?: emptyList()
                        val manualPkg = Prefs.voiceEnginePkg
                        val manualOn = Prefs.voiceEngineCls.isNotEmpty()
                        Dialog(onDismissRequest = { enginePickerOpen = false }) {
                            Surface(shape = RoundedCornerShape(20.dp),
                                color = MaterialTheme.colorScheme.surface,
                                border = BorderStroke(1.dp, MaterialTheme.colorScheme.outline)) {
                                Column(Modifier.padding(vertical = 14.dp)) {
                                    Text("选一个语音识别引擎", fontSize = 16.sp,
                                        fontWeight = FontWeight.SemiBold,
                                        modifier = Modifier.padding(horizontal = 20.dp, vertical = 4.dp))
                                    Text("现在的引擎连不上它自己的服务器，换一个通常就能按住说话了",
                                        fontSize = 12.sp, color = text3Color(),
                                        modifier = Modifier.padding(horizontal = 20.dp, vertical = 2.dp))
                                    Column(Modifier.heightIn(max = 320.dp)
                                        .verticalScroll(rememberScrollState())) {
                                        EngineRow("跟随系统默认", !manualOn) {
                                            applyEngine(null); enginePickerOpen = false
                                            setStatus("已切回系统默认引擎，按住说一句试试")
                                        }
                                        if (engineCands == null)
                                            Text("正在查找可用引擎…",
                                                fontSize = 12.sp, color = text3Color(),
                                                modifier = Modifier.padding(horizontal = 20.dp, vertical = 6.dp))
                                        if (cands.isNotEmpty()) cands.forEach { info ->
                                            EngineRow(info.label,
                                                manualOn && info.pkg == manualPkg) {
                                                applyEngine(info); enginePickerOpen = false
                                                setStatus("已切到「${info.label}」，按住说一句试试")
                                            }
                                        }
                                        if (engineCands != null && cands.isEmpty())
                                            Text("这台手机没枚举到第二个引擎：去「设置 → 应用管理」确认语音服务有没有被停用",
                                                fontSize = 12.sp, color = text3Color(),
                                                modifier = Modifier.padding(horizontal = 20.dp, vertical = 6.dp))
                                        EngineRow("打开系统语音设置") {
                                            enginePickerOpen = false
                                            runCatching {
                                                ctx.startActivity(
                                                    Intent("android.settings.VOICE_INPUT_SETTINGS")
                                                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                                            }.onFailure {
                                                setStatus("这台手机没有系统语音设置页，去「设置 → 应用管理」里找语音服务")
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    // .attach-menu：卡片下方的 surface 面板（网页 DOM 顺序就在 .input-card 之后）
                    AnimatedVisibility(visible = attachOpen,
                        enter = fadeIn(tween(140)) + slideInVertically(tween(140)) { it / 3 }) {
                        FenverSurface(
                            modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                            color = MaterialTheme.colorScheme.surface,
                            border = BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
                            shape = RoundedCornerShape(16.dp),
                        ) {
                            Row(Modifier.fillMaxWidth().padding(10.dp),
                                horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                                val tileInk = MaterialTheme.colorScheme.onSurfaceVariant
                                AttachTile({ FenverIcon(FenverIcon.Camera, tileInk, Modifier.size(17.dp)) }, "相机") { attachOpen = false; camera.launch(null) }
                                AttachTile({ FenverIcon(FenverIcon.Image, tileInk, Modifier.size(17.dp)) }, "图片") { attachOpen = false; picker.launch("image/*") }
                                AttachTile({ FenverIcon(FenverIcon.File, tileInk, Modifier.size(17.dp)) }, "文件") { attachOpen = false; picker.launch("*/*") }
                            }
                        }
                    }
                    // .model-menu 第 12 轮改版：输入卡下挂的小面板退役，点模型钮改出
                    // 全屏「选择模型」页（豆包同款，见根层 ModelPickerScreen）。
                }
            }
            // 第 13 轮（用户点名全屏版「占据整个屏幕很不美观」）：录音界面收小成
            // 贴在屏幕底部的圆角卡片，正好盖住输入卡——输入框、模型钮、＋都藏进
            // 卡片后面，但不再糊满全屏。手势锚点仍在输入位那个 Box：卡片不挂
            // pointerInput、不吃事件，已按下手指的后续事件照常发给锚点。
            if (voice.listening) {
                VoiceScreen(voice.heard, voice.level, voiceCancel,
                    Modifier.align(Alignment.BottomCenter).padding(bottom = 14.dp))
            }
            // 修复轮 Minor 6：这层全屏弹层从前只有右上角 × 一个出口——本文件唯一的
            // BackHandler 属于设置弹层（下面那枚），modelMenuOpen 从来没接返回键，
            // 真机上按返回可能直接退到会话外面。enabled 必须绑 modelMenuOpen：不绑就是
            // 常驻吃返回键。收尾走 closeModelMenu()，与 × 同一条路（两份收体会漂移）。
            BackHandler(enabled = modelMenuOpen) { closeModelMenu() }
            // 点模型钮弹出全屏「选择模型」页（对标图 3）：标题 + 右上 × + 模型清单，当前项 ✓。
            if (modelMenuOpen) {
                ModelPickerScreen(
                    models = providers.filter { it.usable },
                    currentId = currentProvider()?.id,
                    card = ModelCardUi(providerRecords, modelCardOpen, modelManage,
                        modelSaving, modelSaved?.first ?: "", modelSaved?.second ?: "",
                        modelCardError, budgetClamped?.first ?: "",
                        budgetClamped?.second ?: 0, budgetClamped?.third ?: 0),
                    onOpenCard = { id ->
                        // 点行展开/收起同一张卡（窄屏一次只开一张，同网页 toggleModelDetail
                        // 的触屏支）；要改能力的那一条才去读完整记录，别白打一枪。
                        val next = if (modelCardOpen == id) "" else id
                        modelCardOpen = next
                        if (next.isNotEmpty()) {
                            // 成功那句**不在这里清**：它绑着"我保存的是哪一张"（savedId），
                            // 由打印它的那张卡消化（网页 Task 9 修复轮 Minor 3 同一判据——
                            // 切出去看一眼再回来，"已保存"不该凭空消失）。错误那句切卡就清。
                            modelCardError = ""
                            val hit = providers.firstOrNull { it.id == next }
                            if (hit != null && hit.can_edit && providerRecords.isEmpty())
                                loadProviderRecords()
                        }
                    },
                    onToggleManage = { on -> modelManage = on },
                    onBudgetTouched = {
                        ringTick++
                        reconcileBudgetCap(currentProvider()?.id ?: "", modelCapK())
                    },
                    onSaveCaps = { id, patch -> saveModelCapabilities(id, patch) },
                    onClose = {
                        // 关掉弹单：卡片收起、三句提示消化掉；管理区**不**清（收起≠退出管理，
                        // 与网页 hideModelMenu 同一判据，注释与代码不许再打反向）
                        closeModelMenu()
                    },
                    onPick = { id ->
                        // 修复轮 Minor 1：换模型走**同一套**收尾。旧代码这里只关了
                        // modelMenuOpen ⇒ 换个模型再打开，还是那张卡展开着、
                        // 还可能重印上一轮那句「已保存/夹小了/失败」。
                        closeModelMenu()
                        // switchModel：选择即生效；服务端「我的默认」写失败不反悔
                        Prefs.defaultProviderId = id
                        scope.launch { runCatching { Api.setMyDefaultProvider(id) } }
                        setStatus("")
                    },
                )
            }
            // v0.39.x 驾驶舱：点上下文环开账本（弹层）。整理/恢复写 contextDropBySession，
            // 滑杆写 Prefs.setContextBudget——都落在发送真正吃得到的那张表上。
            if (contextPanelOpen) {
                ContextPanel(
                    info = ringInfo,
                    providerId = currentProvider()?.id ?: "",
                    queuedK = queuedK,
                    onBudgetChanged = { ringTick++ },
                    onTidy = { applyContextTidy() },
                    onReset = { resetContextTidy() },
                    onClose = { contextPanelOpen = false },
                )
            }
            // 「立即插话」确认弹层：忙时这一枪的代价是停掉正在生成的回答，必须说破
            // 并让用户点一次确认；空闲时就是「别排队，现在发」。
            queueNowConfirm?.let { item ->
                Dialog(onDismissRequest = { queueNowConfirm = null }) {
                    Surface(shape = RoundedCornerShape(20.dp),
                        color = MaterialTheme.colorScheme.surface,
                        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outline)) {
                        Column(Modifier.fillMaxWidth()
                            .padding(horizontal = 20.dp, vertical = 14.dp)) {
                            Text("立即插话", fontSize = 16.sp, fontWeight = FontWeight.SemiBold,
                                color = MaterialTheme.colorScheme.onSurface)
                            Text(item.text, fontSize = 13.sp, maxLines = 3,
                                overflow = TextOverflow.Ellipsis,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                modifier = Modifier.padding(vertical = 8.dp))
                            Text(if (busy)
                                    "这会停掉正在生成的回答，然后把这条发出去；剩下待发的先暂停。"
                                else
                                    "现在没在生成，这条会直接发出去。",
                                fontSize = 12.sp, color = text3Color())
                            Row(Modifier.fillMaxWidth().padding(top = 10.dp),
                                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                ActionPill("取消", onClick = { queueNowConfirm = null },
                                    filled = false)
                                ActionPill("确认插话", onClick = {
                                    queueNowConfirm = null
                                    sendQueuedNow(item)
                                })
                            }
                        }
                    }
                }
            }
            // 断网小卡片：顶部居中悬浮，不占布局不推内容；今日静音过就不出
            if (NetMinder.show()) {
                NetMinderCard(Modifier.align(Alignment.TopCenter).statusBarsPadding()
                    .padding(horizontal = 36.dp, vertical = 10.dp))
            }
            // v0.28（用户点名照参考图加「小箭头，点击之后直接跳转到最下方」）：
            // 豆包同款「回到底部」——上翻阅读或流式结尾没在眼前时冒出来，点一下
            // 把最后一项的底边顶到视口底。录音卡盖场、模型页全屏时不抢位，先让路。
            AnimatedVisibility(
                visible = !atBottom && !voice.listening && !modelMenuOpen,
                enter = fadeIn(tween(160)) + scaleIn(tween(160), initialScale = 0.7f),
                exit = fadeOut(tween(120)) + scaleOut(tween(120), targetScale = 0.7f),
                modifier = Modifier.align(Alignment.BottomCenter)
                    .padding(bottom = with(LocalDensity.current) { composerPx.toDp() } + 14.dp),
            ) {
                FenverSurface(
                    modifier = Modifier.size(40.dp),
                    color = MaterialTheme.colorScheme.surface,
                    border = BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
                    shape = CircleShape,
                    contentAlignment = Alignment.Center,
                    onClick = {
                        scope.launch {
                            val t = listState.layoutInfo.totalItemsCount
                            if (t > 0) listState.scrollToItem(t - 1, MAX_SCROLL_OFFSET)
                        }
                    },
                ) {
                    FenverIcon(FenverIcon.ArrowDown, MaterialTheme.colorScheme.onSurface, Modifier.size(20.dp))
                }
            }
        }
    }

    // 设置弹层（.modal 手机档：底部出、一张弹层内换 view，不开第二层）。
    // 用户钦定出口只有两个：返回键 和 一级页右上角 ×。M3 1.6 的 ModalBottomSheet
    // 遮罩点击/下拉手势和返回键共用 onDismissRequest 分不开，也没有关手势的参数，
    // 所以这里用全屏 Dialog 自己拼底部弹层：dismissOnClickOutside=false 让点遮罩
    // 彻底无响应，无拖拽手势可关，返回键由弹层内 BackHandler 接管——二级页先退回
    // 一级列表（同 ‹），一级页关整层。
    if (settingsPage != null) {
        Dialog(onDismissRequest = { /* 只有 ×/返回键会改 settingsPage，这里轮不到 */ },
            properties = DialogProperties(usePlatformDefaultWidth = false,
                                          dismissOnClickOutside = false,
                                          dismissOnBackPress = false)) {
            BackHandler {
                val p = settingsPage ?: ""
                settingsPage = if (p.isNotEmpty()) "" else null
            }
            Box(Modifier.fillMaxSize()
                .background(MaterialTheme.colorScheme.scrim.copy(alpha = 0.4f))) {
                Column(Modifier.fillMaxWidth().align(Alignment.BottomCenter)
                    .clip(RoundedCornerShape(topStart = 22.dp, topEnd = 22.dp))
                    .background(MaterialTheme.colorScheme.surface)) {
                    SettingsSheet(
                        page = settingsPage ?: "",
                        cachedProviders = providers,
                        onOpenPage = { settingsPage = it },
                        onRequireAuth = { mode -> settingsPage = null; onRequireAuth(mode) },
                        onOpenUrl = onOpenUrl,
                        onModelsChanged = { scope.launch { loadModelsNow() } },
                        onLoggedOut = { settingsPage = null; onLoggedOut() },
                        // 网页 exportCurrent 的空会话判据：transient 的 ⚠️ 气泡不算话
                        canExport = messages.any { !it.transient },
                    )
                }
            }
        }
    }
}

/* ---------------- 空对话首屏（.empty-state：icon.png +「开始一段对话」，就这两样） ---------------- */
@Composable
private fun EmptyHero() {
    Column(Modifier.fillMaxWidth().padding(top = 60.dp),
        horizontalAlignment = Alignment.CenterHorizontally) {
        Box(Modifier.padding(bottom = 14.dp)
            .border(BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
                RoundedCornerShape(16.dp))) {
            AiBrandMark(54)
        }
        Text("开始一段对话", style = MaterialTheme.typography.titleLarge,
            color = MaterialTheme.colorScheme.onSurface)
    }
}

/* ---------------- 待发送附件行（.att-row / .att-chip） ---------------- */
@Composable
private fun AttRow(items: List<AttItem>, onRemove: (AttItem) -> Unit) {
    Row(Modifier.fillMaxWidth().padding(bottom = 8.dp)
        .horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        items.forEach { a ->
            Row(Modifier.background(MaterialTheme.colorScheme.surfaceVariant,
                RoundedCornerShape(10.dp))
                .border(BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
                    RoundedCornerShape(10.dp))
                .padding(start = 6.dp, end = 8.dp, top = 5.dp, bottom = 5.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Box(Modifier.size(30.dp).background(hoverBg(), RoundedCornerShape(6.dp)),
                    contentAlignment = Alignment.Center) {
                    val attInk = MaterialTheme.colorScheme.onSurfaceVariant
                    if (a.kind == "image") FenverIcon(FenverIcon.Image, attInk, Modifier.size(16.dp))
                    else FenverIcon(FenverIcon.File, attInk, Modifier.size(16.dp))
                }
                Text(a.name, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.widthIn(max = 110.dp))
                Text(fmtSize(a.size), fontSize = 11.sp, color = text3Color())
                Box(Modifier.clickable { onRemove(a) }.padding(horizontal = 4.dp).size(18.dp),
                    contentAlignment = Alignment.Center) {
                    FenverIcon(FenverIcon.Close, text3Color(), Modifier.size(14.dp))
                }
            }
        }
    }
}

private fun fmtSize(bytes: Long): String = when {
    bytes < 1024 -> "$bytes B"
    bytes < 1024 * 1024 -> "${bytes / 1024} KB"
    else -> "%.1f MB".format(bytes / 1024.0 / 1024.0)
}

/* .bg-hover 不在 MaterialTheme 色板上（网页它是独立一档），按主题直取。 */
@Composable
internal fun hoverBg(): Color =
    if (isWebLight()) FenverTokens.LBgHover else FenverTokens.BgHover

/* ---------------- 输入卡（第 12 轮真机反馈：豆包三态输入区） ----------------
 * 收起态一行：[声纹] [发消息或按住说话…] [＋]；
 * 单点展开两行：上行「输入消息…」文本区，下行 [声纹] [模型钮] [＋] [发送/停止]。
 * 声纹钮=语音模式开关（切语音换键盘图标+主色）；模型钮点开全屏「选择模型」页。
 * 占位符跟着键盘走：键盘没起「发消息或按住说话…」（长按即说），起了「输入消息…」。
 * 录音中整屏由 VoiceScreen 接管（见 ChatScreen 根层），输入位只留手势锚点。 */

/* 圆形图标钮：按下缩一点（无涟漪不糊），可选填充/描边色/整枚旋转。
 * 发送钮是它的 40dp 实心 primary 特化版，一族按钮同一只手筋；不再有彩色光晕。 */
@Composable
private fun GhostCircleButton(onClick: () -> Unit, size: Dp = 38.dp, rotation: Float = 0f,
                              fill: Color? = null,
                              stroke: Color = MaterialTheme.colorScheme.outline,
                              content: @Composable () -> Unit) {
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val scale by animateFloatAsState(if (pressed) 0.9f else 1f, tween(120), label = "btnScale")
    Box(Modifier.size(size)
        .graphicsLayer { scaleX = scale; scaleY = scale; rotationZ = rotation }
        .background(fill ?: Color.Transparent, CircleShape)
        .border(BorderStroke(1.dp, stroke), CircleShape)
        .clickable(interactionSource = interaction, indication = null, onClick = onClick),
        contentAlignment = Alignment.Center) { content() }
}

/* 第二行的小胶囊入口（去开麦克风 / 试系统语音）：主色实心与中性两种档位。 */
@Composable
private fun ActionPill(text: String, onClick: () -> Unit, filled: Boolean = true) {
    val scheme = MaterialTheme.colorScheme
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val scale by animateFloatAsState(if (pressed) 0.94f else 1f, tween(120), label = "pillScale")
    Box(Modifier.graphicsLayer { scaleX = scale; scaleY = scale }
        .background(if (filled) scheme.primaryContainer else scheme.surfaceVariant, CircleShape)
        .border(BorderStroke(1.dp, if (filled) scheme.primary else scheme.outline), CircleShape)
        .clickable(interactionSource = interaction, indication = null, onClick = onClick)
        .padding(horizontal = 10.dp, vertical = 5.dp)) {
        Text(text, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.widthIn(max = 200.dp),
            color = if (filled) scheme.primary else scheme.onSurfaceVariant)
    }
}

/* 引擎选择弹窗的一行（第 6 轮）：左侧实心小圆点标"当前在用"，与图标族同手筋。 */
@Composable
private fun EngineRow(name: String, selected: Boolean = false, onClick: () -> Unit) {
    val scheme = MaterialTheme.colorScheme
    Row(Modifier.fillMaxWidth().clickable(onClick = onClick)
        .padding(horizontal = 20.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        Box(Modifier.size(8.dp).background(
            if (selected) scheme.primary else scheme.surfaceVariant, CircleShape)
            .border(BorderStroke(1.dp, if (selected) scheme.primary else scheme.outline),
                CircleShape))
        Text(name, fontSize = 14.sp, color = scheme.onSurface,
            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
            modifier = Modifier.weight(1f))
    }
}

/* ---------------- 接力队列条（v0.39.x 驾驶舱 Task 7） ----------------
 * 队列内容全是用户自己刚打的纯文本：一律 Compose Text/BasicTextField 渲染，
 * 不引入任何内嵌浏览视图、不拼标记语言。空队列由调用方整块不挂；暂停态
 * 明写在头行（"已暂停"+继续按钮），别让排队的人以为界面卡死。 */
@Composable
private fun QueueStrip(items: List<QueuedMsg>, paused: Boolean,
                       onTogglePause: () -> Unit, onEdit: (String, String) -> Unit,
                       onDelete: (String) -> Unit, onShowNow: (QueuedMsg) -> Unit) {
    // 行内编辑的临时态：普通 remember——队列一有写（接力弹出队首/入队/删除）就换
    // items，若编辑态跟着挂在 items 键上会被重置，半句草稿凭空消失（修复轮 M-4）。
    // 同 id 的队列写不重置草稿；正在编辑的那条真没了（弹出/删除）才由下面的
    // LaunchedEffect 回收孤儿编辑态。
    var editingId by remember { mutableStateOf<String?>(null) }
    var draft by remember { mutableStateOf("") }
    LaunchedEffect(items) {
        val id = editingId
        if (id != null && items.none { it.id == id }) {
            editingId = null; draft = ""
        }
    }
    val scheme = MaterialTheme.colorScheme
    FenverSurface(
        modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp),
        color = scheme.surface,
        border = BorderStroke(1.dp, scheme.outline),
        shape = RoundedCornerShape(16.dp),
    ) {
        Column(Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text("${items.size} 条待发送", fontSize = 13.sp,
                    fontWeight = FontWeight.SemiBold, color = scheme.onSurface,
                    modifier = Modifier.weight(1f))
                if (paused) {
                    Text("已暂停", fontSize = 12.sp, color = text3Color(),
                        modifier = Modifier.padding(end = 8.dp))
                    ActionPill("继续队列", onClick = onTogglePause)
                } else {
                    ActionPill("暂停", onClick = onTogglePause, filled = false)
                }
            }
            items.forEach { item ->
                Column(Modifier.fillMaxWidth().padding(top = 6.dp)) {
                    if (editingId == item.id) {
                        BasicTextField(
                            value = draft, onValueChange = { draft = it },
                            textStyle = TextStyle(fontSize = 13.sp, color = scheme.onSurface),
                            cursorBrush = SolidColor(scheme.primary),
                            modifier = Modifier.fillMaxWidth()
                                .background(scheme.surfaceVariant, RoundedCornerShape(8.dp))
                                .border(BorderStroke(1.dp, scheme.outline),
                                    RoundedCornerShape(8.dp))
                                .padding(horizontal = 8.dp, vertical = 6.dp),
                            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                            keyboardActions = KeyboardActions(onDone = {
                                onEdit(item.id, draft.trim()); editingId = null
                            }),
                        )
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp),
                            modifier = Modifier.padding(top = 4.dp)) {
                            ActionPill("保存", onClick = {
                                onEdit(item.id, draft.trim()); editingId = null
                            }, filled = false)
                            ActionPill("取消", onClick = { editingId = null }, filled = false)
                        }
                    } else {
                        Row(Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically) {
                            Text(item.text, fontSize = 13.sp, color = scheme.onSurface,
                                maxLines = 2, overflow = TextOverflow.Ellipsis,
                                modifier = Modifier.weight(1f))
                            Text("+${item.k}K", fontSize = 11.sp, color = text3Color(),
                                modifier = Modifier.padding(start = 6.dp))
                        }
                        if (item.attachments.isNotEmpty())
                            Text("带 ${item.attachments.size} 个附件", fontSize = 11.sp,
                                color = text3Color())
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            ActionPill("编辑", onClick = {
                                editingId = item.id; draft = item.text
                            }, filled = false)
                            ActionPill("删除", onClick = { onDelete(item.id) }, filled = false)
                            ActionPill("立即插话", onClick = { onShowNow(item) })
                        }
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun InputCard(input: TextFieldValue, onInput: (TextFieldValue) -> Unit,
                      focusRequester: FocusRequester,
                      onSendKey: () -> Unit,
                      attachOpen: Boolean, onToggleAttach: () -> Unit,
                      status: String, statusErr: Boolean, busy: Boolean,
                      chipModel: String, chipVisible: Boolean,
                      onToggleChip: () -> Unit,
                      ringPct: Int, ringUsedK: Int, ringBudgetK: Int,
                      onRingClick: () -> Unit,
                      onStop: () -> Unit, onSend: () -> Unit,
                      canSend: Boolean, voiceMod: Modifier,
                      voiceMode: Boolean, onToggleVoice: () -> Unit,
                      keyboardUp: Boolean,
                      voiceArmed: Boolean, voiceLive: Boolean,
                      voiceHintOn: Boolean, voiceHintLabel: String?,
                      onOpenVoiceService: () -> Unit, onTrySystemVoice: () -> Unit,
                      onPickEngine: () -> Unit) {
    val scheme = MaterialTheme.colorScheme
    val view = LocalView.current
    // 第 17 轮（用户验收改判：「粘贴的UI不好看，希望它小一点并且位置跟着光标」）：
    // 第 15 轮的 PopupMenu 竖排大列表换紧凑胶囊 PopupWindow，弹在光标正上方
    // （见 PopupTextToolbar）。只罩输入框这一层，别处文本域不受牵连。
    val toolbarBg = scheme.inverseSurface.toArgb()
    val toolbarFg = scheme.inverseOnSurface.toArgb()
    val nativeToolbar = remember(view, toolbarBg, toolbarFg) {
        PopupTextToolbar(view, toolbarBg, toolbarFg)
    }
    // v0.28.3（真机「空框长按没粘贴」兜底，见 pasteBackstop 注释）：键盘起+空框+
    // 语音闸门不在位时，长按归文本框自家链条；链条哑了（工具条没弹起）才补位。
    val latestInput by rememberUpdatedState(input)
    var fieldRect by remember { mutableStateOf<Rect?>(null) }
    // 第 12 轮（豆包三态输入区，用户逐图钦定）：收起=单胶囊 [声纹][发消息或按住
    // 说话…][＋]；单点展开（键盘起/有字/生成中）=上行「输入消息…」文本区 +
    // 下行 [声纹][模型钮][＋][发送/停止]。
    val expanded = keyboardUp || input.text.isNotEmpty() || busy
    // 语音闸门在位就不挂文本框（第 10 轮零键盘闪机制原样保留）：没框就没焦点可抢。
    val gateOn = voiceMode || voiceArmed || voiceLive
    val pasteGuardMod = Modifier.pasteBackstop(
        toolbar = nativeToolbar,
        eligible = { !gateOn && keyboardUp && !voiceMode && latestInput.text.isEmpty() },
        anchorRect = { fieldRect },
        onPasteText = { text ->
            val cur = latestInput
            val start = cur.selection.min
            val end = cur.selection.max
            onInput(TextFieldValue(cur.text.replaceRange(start, end, text),
                selection = TextRange(start + text.length)))
        },
    )
    FenverSurface(
        modifier = Modifier.fillMaxWidth(),
        color = scheme.surface,
        border = BorderStroke(1.dp, scheme.outline),
        shape = RoundedCornerShape(12.dp),
    ) {
        Column(Modifier.padding(horizontal = 10.dp, vertical = 8.dp)) {
            // 输入位行：锚点 Box 三态同一个节点——收起/展开切换不销毁它，
            // 已按下的手指不会半路丢录音状态机（第 11 轮锚点槽位教训）。
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (!expanded) {
                    // 收起左钮：声纹=语音模式开关；语音模式换键盘图标+主色实心一眼可辨
                    GhostCircleButton(onClick = onToggleVoice,
                        fill = if (voiceMode) scheme.primaryContainer else null,
                        stroke = if (voiceMode) scheme.primary else scheme.outline) {
                        if (voiceMode) FenverIcon(FenverIcon.Keyboard, scheme.primary, Modifier.size(19.dp))
                        else FenverIcon(FenverIcon.VoiceWave, scheme.onSurfaceVariant, Modifier.size(19.dp))
                    }
                }
                Box(Modifier.weight(1f).heightIn(min = 42.dp).then(voiceMod)
                    .then(pasteGuardMod),
                    contentAlignment = if (gateOn) Alignment.Center else Alignment.TopStart) {
                    if (gateOn) {
                        if (voiceMode) Row(horizontalArrangement = Arrangement.Center,
                            verticalAlignment = Alignment.CenterVertically) {
                            FenverIcon(FenverIcon.Mic, scheme.onSurfaceVariant, Modifier.size(17.dp))
                            Spacer(Modifier.width(8.dp))
                            Text("按住说话", fontSize = 15.sp,
                                fontWeight = FontWeight.SemiBold,
                                color = scheme.onSurfaceVariant)
                        }
                        else Text(if (voiceLive) "正在听你说…" else "发消息或按住说话…",
                            fontSize = 15.sp, color = text3Color())
                    } else {
                        CompositionLocalProvider(
                            LocalTextToolbar provides nativeToolbar) {
                        BasicTextField(
                            value = input, onValueChange = onInput,
                            textStyle = TextStyle(fontSize = 15.sp, lineHeight = 24.sp,
                                color = scheme.onSurface),
                            cursorBrush = SolidColor(scheme.primary),
                            modifier = Modifier.fillMaxWidth().heightIn(min = 24.dp, max = 200.dp)
                                .padding(horizontal = 6.dp, vertical = 7.dp)
                                .focusRequester(focusRequester)
                                // v0.28.3：兜底粘贴弹胶囊要贴着框——把根坐标量下来备着
                                .onGloballyPositioned { fieldRect = it.boundsInRoot() },
                            // 网页：Enter 发送、Shift+Enter 换行。手机键盘对位：Send 键=发送，
                            // 键盘上的回车/换行键照常插入换行（Gboard 上 Shift 语义由键面自己给）。
                            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                            keyboardActions = KeyboardActions(onSend = { onSendKey() }),
                            decorationBox = { inner ->
                                Box {
                                    if (input.text.isEmpty())
                                        Text(if (keyboardUp) "发消息…" else "发消息或按住说话…",
                                            fontSize = 15.sp, color = text3Color(),
                                            modifier = Modifier.padding(
                                                horizontal = 6.dp, vertical = 7.dp))
                                    inner()
                                }
                            },
                        )
                        }
                    }
                }
                // 第 13 轮（用户点名「模型切换放到加号旁边，不然我进去了怎么
                // 找到」）：收起态也在 ＋ 左边挂模型钮，不展开输入框也能换模型。
                // v0.39.x 驾驶舱：环钉在模型钮左边（与网页同序）——点环开账本、点钮仍开
                // 模型清单，两个手势互不抢。（这两行各自保持 `if (chipVisible) X(...)` 单句
                // 形状——v02312/v02313/v02314 的静态锁按原文吃这段的间距与整句，别折叠。）
                if (!expanded) {
                    if (chipVisible) ContextRing(ringPct, ringUsedK, ringBudgetK, onRingClick)
                    if (chipVisible) ModelChip(chipModel, onToggleChip)
                    // 收起右钮：＋附件面板开关；open 态整枚旋转 45° 变「×」语义
                    GhostCircleButton(onClick = onToggleAttach,
                        rotation = if (attachOpen) 45f else 0f,
                        stroke = if (attachOpen) scheme.primary else scheme.outline) {
                        FenverIcon(FenverIcon.Plus,
                            if (attachOpen) scheme.primary else scheme.onSurfaceVariant,
                            Modifier.size(19.dp))
                    }
                }
            }
            if (expanded) {
                // 下行（对标图 2）：[声纹] [模型钮] … [＋] [发送/停止]。相机钮从输入条
                // 退役（＋面板里「相机」tile 照旧），腾出的位置给豆包同款声纹/模型钮。
                Row(Modifier.fillMaxWidth().padding(top = 6.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    GhostCircleButton(onClick = onToggleVoice,
                        fill = if (voiceMode) scheme.primaryContainer else null,
                        stroke = if (voiceMode) scheme.primary else scheme.outline) {
                        if (voiceMode) FenverIcon(FenverIcon.Keyboard, scheme.primary, Modifier.size(19.dp))
                        else FenverIcon(FenverIcon.VoiceWave, scheme.onSurfaceVariant, Modifier.size(19.dp))
                    }
                    // 模型钮（对标图 2 的 Auto）：点开全屏「选择模型」页（见 ChatScreen 根层）；
                    // 环排它左边（v0.39.x 驾驶舱，同网页 #ctxRingBtn 在 #modelChip 前）。
                    if (chipVisible) ContextRing(ringPct, ringUsedK, ringBudgetK, onRingClick)
                    if (chipVisible) ModelChip(chipModel, onToggleChip)
                    Spacer(Modifier.weight(1f))
                    if (busy) {
                        // 生成中：右侧只留「停止」一枚，＋/发送都让位，避免误点
                        Row(Modifier.border(BorderStroke(1.dp, scheme.outline), CircleShape)
                            .background(scheme.surface, CircleShape)
                            .clickable(onClick = onStop)
                            .padding(start = 12.dp, end = 14.dp, top = 7.dp, bottom = 7.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                            FenverIcon(FenverIcon.Stop, scheme.onSurface, Modifier.size(13.dp))
                            Text("停止", fontSize = 14.sp, color = scheme.onSurface)
                        }
                    } else {
                        GhostCircleButton(onClick = onToggleAttach,
                            rotation = if (attachOpen) 45f else 0f,
                            stroke = if (attachOpen) scheme.primary else scheme.outline) {
                            FenverIcon(FenverIcon.Plus, if (attachOpen) scheme.primary else scheme.onSurfaceVariant,
                                Modifier.size(19.dp))
                        }
                        if (canSend) {
                            // 发送：40dp 实心 primary 圆钮，无光晕；字色跟着 onPrimary。
                            GhostCircleButton(onClick = onSend, size = 40.dp,
                                fill = scheme.primary,
                                stroke = Color.Transparent) {
                                FenverIcon(FenverIcon.ArrowUp, scheme.onPrimary, Modifier.size(20.dp))
                            }
                        }
                    }
                }
            }
            // 语音修复行（第 5 轮点名开麦 → 第 6 轮加换引擎）：真机证明这台手机的
            // 病根是引擎连不上自家服务器，不是麦克风——「换个语音引擎」排第一位，
            // 开麦入口降级为辅助。三枚胶囊用 FlowRow 摊开，窄屏换行不挤没。
            if (voiceHintOn) {
                FlowRow(Modifier.fillMaxWidth().padding(top = 6.dp),
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    ActionPill(onClick = onPickEngine, text = "换个语音引擎")
                    ActionPill(onClick = onOpenVoiceService, filled = false,
                        text = if (voiceHintLabel != null) "去给「$voiceHintLabel」开麦克风"
                               else "去开语音服务权限")
                    ActionPill(onClick = onTrySystemVoice, text = "用系统语音输入试试",
                        filled = false)
                }
            }
            // 状态条：6 秒自收（见 ChatScreen 的 LaunchedEffect），不再常驻挡视线
            //（第 4 轮真机反馈）。模型胶囊第 12 轮起搬进展开态下行（豆包同款 Auto 钮），
            // 这里只剩状态文字。
            if (status.isNotEmpty()) {
                Text(status, Modifier.fillMaxWidth().padding(top = 6.dp),
                    fontSize = 12.sp, maxLines = 2,
                    color = if (statusErr) scheme.error else text3Color())
            }
        }
    }
}

/* 第 15 轮起长按菜单不再是 Compose 自绘气泡（点外面不关）。第 15 轮的壳是系统
 * PopupMenu——原生但长相是贴控件角落的竖排大列表；第 17 轮用户验收改判：
 * 「这次改的不错，保持，但是这个粘贴的UI不好看，而且我希望它小一点并且位置是
 * 跟着光标的」。换紧凑胶囊 PopupWindow：横排一行 13sp 小字、10dp 圆角深色卡片
 * （inverseSurface 配色，和系统文本选择条一个味道）、带投影；位置直接用文本框
 * 递来的光标/选区 rect。第 18 轮再收（真机：「粘贴离光标还是有点远」）：多行
 * 选区的 rect 是整个大框，按顶边定位选区长就飘远——改锚选区末端把手：贴 rect
 * 右缘、悬最后一行上方；放不下翻到下方。
 * 非 focusable：不抢输入框焦点、键盘不收；点外面自动收（outsideTouchable）、
 * 选一项即关。动作仍走文本框原回调——只换壳不改行为。接口形状照 BOM 2024.06.00
 * （Compose 1.6.8）的 showMenu(rect, 四个可空回调)；新版才换 textActions/showToolbar 名。 */
private class PopupTextToolbar(
    private val view: View,
    private val bgColor: Int,
    private val fgColor: Int,
) : TextToolbar {
    private var popup: PopupWindow? = null

    override fun showMenu(rect: Rect, onCopyRequested: (() -> Unit)?,
                          onPasteRequested: (() -> Unit)?, onCutRequested: (() -> Unit)?,
                          onSelectAllRequested: (() -> Unit)?) {
        hide()
        val items = ArrayList<Pair<String, (() -> Unit)?>>(4)
        onCutRequested?.let { items.add("剪切" to it) }
        onCopyRequested?.let { items.add("复制" to it) }
        onPasteRequested?.let { items.add("粘贴" to it) }
        onSelectAllRequested?.let { items.add("全选" to it) }
        if (items.isEmpty()) return
        val ctx = view.context
        val dp = ctx.resources.displayMetrics.density
        fun d(v: Float) = (v * dp).toInt()
        val row = LinearLayout(ctx).apply {
            orientation = LinearLayout.HORIZONTAL
            background = GradientDrawable().apply {
                cornerRadius = d(10f).toFloat()
                setColor(bgColor)
            }
            setPadding(d(2f), 0, d(2f), 0)
        }
        for ((label, act) in items) {
            row.addView(TextView(ctx).apply {
                text = label
                setTextColor(fgColor)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
                setPadding(d(12f), d(9f), d(12f), d(9f))
                gravity = Gravity.CENTER
                isClickable = true
                setOnClickListener { act?.invoke(); hide() }
            })
        }
        row.measure(View.MeasureSpec.UNSPECIFIED, View.MeasureSpec.UNSPECIFIED)
        val w = row.measuredWidth
        val h = row.measuredHeight
        val screenW = ctx.resources.displayMetrics.widthPixels
        // 第 18 轮（真机：「粘贴离光标还是有点远」+截图）：多行选区递来的 rect
        // 是整个选区的大框，第 17 轮按 rect 顶边定位——选区长就飘得远。改锚到
        // 选区末端（绿色把手所在）：横向贴着 rect 右缘、纵向悬在最后一行上方，
        // 和系统把手工具条同落点；单光标 rect 本来就小，行为不变照样贴光标。
        val x = (rect.right - w - 6f * dp)
            .coerceAtMost(screenW - w - 8f * dp)
            .coerceAtLeast(8f * dp)
        val above = rect.bottom - h - 6f * dp
        val y = if (above >= 0f) above else rect.bottom + 6f * dp
        val p = PopupWindow(row, ViewGroup.LayoutParams.WRAP_CONTENT,
            ViewGroup.LayoutParams.WRAP_CONTENT, false)
        p.isOutsideTouchable = true
        p.elevation = d(12f).toFloat()
        p.showAtLocation(view, Gravity.TOP or Gravity.START, x.toInt(), y.toInt())
        popup = p
    }

    override fun hide() {
        popup?.dismiss()
        popup = null
    }

    override val status: TextToolbarStatus
        get() = if (popup != null) TextToolbarStatus.Shown else TextToolbarStatus.Hidden
}

/* v0.28.3（真机反馈「在这个界面长按怎么没有粘贴了」+截图：键盘起、空框）：
 * Compose 1.6.8 的长按链条里，「粘贴」一项被 clipboardManager.hasText() 把关——
 * 它只认 text/plain 这一种 MIME。国产 ROM 上不少 App 复制出来的剪贴板带的是
 * 自定义/非标准 MIME（内容其实是文字），hasText() 说不算，于是 showMenu 收到
 * 四个全 null 的回调，胶囊静默不弹，用户眼里就是"长按没反应、粘贴没了"。
 * 另一条可疑路径是整条长按链在个别 ROM 上压根没触发（同样无声无息）。
 *
 * 兜底观察器与 voiceHold 同款姿势：挂在输入位锚点 Box 上、PointerEventPass.Final
 * 收事件、一口不吃——文本框自己的正常链照常跑；只有"长按抬手后工具条仍未弹起"
 * （status != Shown，子节点 Main 段先于父节点 Final 段处理同一记 up，判定时机
 * 天然靠后）才补位：直接读平台剪贴板（coerceToText 能把非标准 MIME 里的文字
 * 硬取出来），复用同一只 PopupTextToolbar 弹「粘贴」，插入走 onInput 光标位。
 * 只管空框长按这一被点名的场景：框里有字时长按选词出剪切/复制/粘贴，那条链
 * 的判定不经过 hasText 门（复制项由选区决定），没有同样的哑点。 */
@Composable
private fun Modifier.pasteBackstop(
    toolbar: PopupTextToolbar,
    eligible: () -> Boolean,
    anchorRect: () -> Rect?,
    onPasteText: (String) -> Unit,
): Modifier {
    val ctx = LocalContext.current
    val eligibleRef = rememberUpdatedState(eligible)
    val rectRef = rememberUpdatedState(anchorRect)
    val pasteRef = rememberUpdatedState(onPasteText)
    return pointerInput(toolbar) {
        val slop = viewConfiguration.touchSlop
        val holdMs = viewConfiguration.longPressTimeoutMillis
        awaitEachGesture {
            val down = awaitFirstDown(requireUnconsumed = false)
            if (!eligibleRef.value()) return@awaitEachGesture
            // 一趟循环等这一指头真正离开：滑出/丢指/取消都拿不到干净的抬手，直接让位。
            // 这段不许包进 run {} 或任何换接收者的挂起 lambda——AwaitPointerEventScope
            // 带 @RestrictSuspension，换了接收者就调不动 awaitPointerEvent（CI 判例）。
            var lift: PointerInputChange? = null
            while (lift == null) {
                val e = awaitPointerEvent(PointerEventPass.Final)
                val c = e.changes.firstOrNull { it.id == down.id } ?: break
                if ((c.position - down.position).getDistance() > slop) break
                if (!c.pressed) lift = c
            }
            val up = lift ?: return@awaitEachGesture
            // 按下不足长按时限 = 普通点按/放光标：这一下归文本框自己，全程不掺和
            if (up.uptimeMillis - down.uptimeMillis < holdMs) return@awaitEachGesture
            if (toolbar.status == TextToolbarStatus.Shown) return@awaitEachGesture
            val text = readClipText(ctx) ?: return@awaitEachGesture
            val rect = rectRef.value() ?: return@awaitEachGesture
            toolbar.showMenu(rect, null, { pasteRef.value.invoke(text) }, null, null)
        }
    }
}

/* 平台剪贴板直读：Compose 的 hasText() 只认 text/plain，这里退一步——
 * coerceToText 能把带自定义 MIME 的文本项还原成字符串；图片/文件项
 * coerceToText 吐回来的是 content URI，那不算"可粘贴的文字"，拒掉。 */
private fun readClipText(ctx: android.content.Context): String? {
    val cm = ctx.getSystemService(android.content.ClipboardManager::class.java) ?: return null
    val clip = cm.primaryClip ?: return null
    if (clip.itemCount == 0) return null
    val t = clip.getItemAt(0).coerceToText(ctx)?.toString()?.trim() ?: return null
    if (t.isEmpty() || t.startsWith("content://") || t.startsWith("file://")) return null
    return t
}

/* ---------------- v0.39.x 驾驶舱 Task 6：上下文环与账本（原生 Compose） ----------------
 * 网页版是 conic-gradient + aria-label；Compose 这边用 Canvas 画弧 + semantics 标签，
 * 颜色全部吃既有 colorScheme（≥70% error / 其余 primary，网页的 --fv-warn 本来就是
 * 空头支票落到 --danger，这里不新增 token）。动态文本一律 Text/semantics 字面量——
 * 模型名、账本读数是服务端或本机原文，绝不拼 HTML/注解字符串往界面里塞。 */
@Composable
private fun ContextRing(pct: Int, usedK: Int, budgetK: Int, onClick: () -> Unit) {
    val scheme = MaterialTheme.colorScheme
    val ink = if (pct >= 70) scheme.error else scheme.primary
    val track = scheme.surfaceVariant
    val frac = pct.coerceIn(0, 100) / 100f
    Canvas(modifier = Modifier.size(24.dp)
        .semantics { contentDescription = "上下文用量 $pct%，${usedK}K / ${budgetK}K" }
        .clickable(onClick = onClick)) {
        val stroke = Stroke(width = 3.dp.toPx(), cap = StrokeCap.Round)
        drawArc(color = track, startAngle = -90f, sweepAngle = 360f,
            useCenter = false, style = stroke)
        drawArc(color = ink, startAngle = -90f, sweepAngle = 360f * frac,
            useCenter = false, style = stroke)
    }
}

/* 账本弹层：used/budget、模型上限、分段（角色/历史/输入；服务端注入差额只在收到过
 * usage 时才说——客户端不猜服务端注入了什么）、预算滑杆（写 Prefs.setContextBudget，
 * 与 outbound 同源）、「轻量整理」「恢复」（写 contextDropBySession，实发真的少）。 */
@Composable
private fun ContextPanel(info: ContextRingInfo, providerId: String, queuedK: Int,
                         onBudgetChanged: () -> Unit, onTidy: () -> Unit,
                         onReset: () -> Unit, onClose: () -> Unit) {
    val scheme = MaterialTheme.colorScheme
    val hi = maxOf(4, info.capK)
    val step = if (hi <= 64) 2 else if (hi <= 256) 8 else 32
    var k by remember(info.budgetK, hi) { mutableIntStateOf(info.budgetK.coerceIn(2, hi)) }
    Dialog(onDismissRequest = onClose) {
        Surface(shape = RoundedCornerShape(20.dp), color = scheme.surface,
            border = BorderStroke(1.dp, scheme.outline)) {
            Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp, vertical = 14.dp)) {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Text("上下文账本", fontSize = 16.sp, fontWeight = FontWeight.SemiBold,
                        color = scheme.onSurface, modifier = Modifier.weight(1f))
                    Box(Modifier.size(32.dp).clip(CircleShape)
                        .clickable(onClick = onClose), contentAlignment = Alignment.Center) {
                        FenverIcon(FenverIcon.Close, scheme.onSurfaceVariant, Modifier.size(18.dp))
                    }
                }
                if (!info.available) {
                    // 清单为空/没有可用模型：如实说没有账可算，不画旧账。
                    Text("还没有可用模型，上下文用量暂无从算起。", fontSize = 13.sp,
                        color = text3Color(),
                        modifier = Modifier.padding(vertical = 10.dp))
                } else {
                    Text("${info.modelName} · ${info.usedK}K / ${info.budgetK}K（${info.pct}%）",
                        fontSize = 14.sp, fontWeight = FontWeight.SemiBold,
                        color = scheme.onSurface,
                        modifier = Modifier.padding(top = 4.dp, bottom = 6.dp))
                    Text("模型上限 ${info.capK}K · 发送按本模型预算截断", fontSize = 12.sp,
                        color = text3Color(), modifier = Modifier.padding(bottom = 6.dp))
                    Text("分段（估算）：角色 ${roundK(info.personaTokens)}K · " +
                        "历史 ${roundK(info.historyTokens)}K · 输入 ${roundK(info.inputTokens)}K",
                        fontSize = 12.sp, color = scheme.onSurface,
                        modifier = Modifier.padding(bottom = 4.dp))
                    if (info.hasUsage) {
                        Text("其余 ${roundK(info.residualTokens)}K = 系统提示/检索注入/校准差额；" +
                            "上轮实报 prompt ${roundK(info.lastPromptTokens)}K（当时客户端估 " +
                            "${roundK(info.lastClientTokens)}K，模型 ${info.providerId}）",
                            fontSize = 12.sp, color = text3Color(),
                            modifier = Modifier.padding(bottom = 4.dp))
                    } else {
                        Text("还没收到服务端 usage：当前读数全是客户端估算，首轮回账后自动校准。",
                            fontSize = 12.sp, color = text3Color(),
                            modifier = Modifier.padding(bottom = 4.dp))
                    }
                    Text("本轮发送已从最早处切掉 ${info.dropped} 条；历史原文未动。",
                        fontSize = 12.sp, color = text3Color(),
                        modifier = Modifier.padding(bottom = 8.dp))
                    // 待发是附注不是实发：接力队列的 K 不进 usedK/pct 那条公式。
                    if (queuedK > 0)
                        Text("待发 +${queuedK}K（接力队列）", fontSize = 12.sp,
                            color = text3Color(), modifier = Modifier.padding(bottom = 6.dp))
                    Text("预算 $k/${hi}K", fontSize = 13.sp, color = scheme.onSurface,
                        modifier = Modifier.padding(bottom = 2.dp))
                    Slider(value = k.toFloat(),
                        onValueChange = { v ->
                            val raw = ((v - 2) / step).toInt() * step + 2
                            k = raw.coerceIn(2, hi)
                        },
                        valueRange = 2f..hi.toFloat(),
                        onValueChangeFinished = {
                            // 与 outbound 同源的同一次落笔：K 值进按模型存的 map。
                            if (providerId.isNotEmpty())
                                Prefs.setContextBudget(providerId, k)
                            onBudgetChanged()
                        })
                    Row(Modifier.fillMaxWidth().padding(top = 8.dp, bottom = 4.dp),
                        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        ActionPill("轻量整理", onClick = onTidy)
                        ActionPill("恢复", filled = false, onClick = onReset)
                    }
                    Text("轻量整理只影响本轮发送带哪几条，历史不删不改。", fontSize = 11.sp,
                        color = text3Color())
                }
            }
        }
    }
}

/* 模型切换钮（ⒶAuto）：第 13 轮起收起态也挂——用户点名「不然我进去了怎么找到」，
 * 就贴在 ＋ 左边；展开态下行照旧。点开全屏「选择模型」页（ModelPickerScreen）。
 * 外观收编到 FenverChip，触控区与顶栏那枚同源。 */
@Composable
private fun ModelChip(chipModel: String, onToggleChip: () -> Unit) {
    FenverChip(
        text = chipModel,
        onClick = onToggleChip,
        trailing = {
            FenverIcon(FenverIcon.ChevronDown, MaterialTheme.colorScheme.onSurfaceVariant, Modifier.size(14.dp))
        },
    )
}

/* ---------------- v0.39.x 驾驶舱 Task 10：模型卡 / 模型管理（与网页 Task 9 同一把尺） ----------------
 * 三态只认服务端派生的 thinking_status（providers._thinking_status），界面不按模型名猜；
 * 本版白名单只有 none ⇒ 所有 supports_thinking=true 的条目结构上都是 unverified，
 * 卡上没有任何「已开启」的说法，verified 一支今天不可达但代码路径要在（P0c 补 verified
 * 旗标后自动可用）。预算与思考偏好都只在本机（Prefs 那两张 map），与上下文面板同一份，
 * 卡片绝不另存一份真相。能力 PUT 是全量语义：body 每一格来自 /v1/me/providers。
 * 窄屏纪律：整页 fillMaxSize + 一列 verticalScroll，卡贴在行下面跟着滚，不开第二层弹窗、
 * 也不给固定高度（本 App 是 adjustResize，键盘与导航条会挤，写死高度就是撑破视口）。 */

/** 三态文案（与网页 thinkingStatusLabel 逐字同值）：verified 说的是「参数形状已核实」，
 *  不是「这个模型一定支持思考」，所以措辞落在"可切换强度"。 */
private fun thinkingStatusLabel(status: String): String = when (status) {
    "verified" -> "可切换思考强度"
    "unverified" -> "支持思考（参数未核实，暂时不能开）"
    else -> "不支持思考"
}

/** 禁用必须给原因（不给原因就是"控件坏了"）；enabled 的那一支返回空串。 */
private fun thinkingStateReason(status: String): String = when (status) {
    "unsupported" -> "这个模型没有思考能力，所以开关不给点。"
    "unverified" -> "这个模型标了支持思考，但参数配方还未核实（本版服务端白名单只有 none）：" +
        "现在把开关打开也发不出去，所以这里禁用，核实通过后（P0c）这一栏自动能用。"
    else -> ""
}

/* 档位标签：只清洗（去空/去重）**不截断**——界面悄悄砍掉第 4 档等于把"没配上"演成
 * "配上了"，长度上限由服务端拒收并把那句理由写在卡上。分隔符与后端 _clean_levels 同尺：
 * 后端拿到的已经是列表、对每项只 strip，从不按空格劈开一项，所以这里也不能把 \s 当分隔符
 * （网页 Task 9 的 Minor 7：「快速 思考」被劈成两档，管理员填两个、库里存四个）。 */
private fun parseThinkingLevels(text: String): List<String> {
    val out = mutableListOf<String>()
    text.split(Regex("[,，、;；]+")).forEach { raw ->
        val t = raw.trim()
        if (t.isNotEmpty() && out.none { it == t }) out.add(t)
    }
    return out
}

/** 上限夹小的那句人话（与网页 budgetClampNoteK 同一件事，措辞按手机重排）。
 *  写成块体而不是表达式体：本仓那把剥注释尺子配的是"锚点到第一个 `{`"，
 *  表达式体里的 `${to}` 会被当成函数体抓走（锁 test_cap_lowering_... 读的就是这块）。 */
private fun budgetClampNote(modelName: String, from: Int, to: Int): String {
    return "$modelName 的上限降到了 ${to}K：这台设备上原本记着的预算是 ${from}K，" +
        "已经按上限夹小，多出来的那部分不会带上。要把窗口放大，先把上限调回去、" +
        "再拖这只滑杆（原来那个数已经被夹掉，不会自己回来）。"
}

/* 打哪扇门只由归属决定（服务端那份 can_edit 已经算过角色，这里不再猜第二次）：
 * 共享条目 → 管理员面 /v1/providers（真门是 RequireAdmin + 二次验证），
 * 自己的条目 → 自助面 /v1/me/providers（真门是属主闸）。 */
private fun providerCapabilityScope(rec: JsonObject): String {
    val shared = rec["shared"]?.jsonPrimitive?.booleanOrNull == true
    return if (shared) "admin" else "mine"
}

/* 全量 PUT 的 body：每一格都从 /v1/me/providers 那条**完整记录**里取——/v1/models 的
 * 目录项没有 base_url，拿它凑等于把上游地址洗掉。三格不许省：
 *   api_key 传空串 → 服务端按占位符保留原密钥（掩码永不出服务端，回显不了）；
 *   is_default 原样带上 → 否则一次改上限就把全站那颗 ★ 洗掉；
 *   thinking_profile 原样带上 → 否则已核实的配方被洗回 none。
 * pricing 缺席（未定价）时不发这一格：没带=保留，显式 null=主动取消定价。
 * 这里**没有** paid_by，也不是遗漏：main.ProviderRequest 就没这一格，客户端发了也会被
 * Pydantic 丢掉，写进 body 只会让下一个读代码的人以为"计费归属是界面改得动的"；真正
 * 保住它的是 providers.ProviderStore.upsert 那份"请求没带就沿用库里"的保留。
 * 也不发 id——路由按 path 钉这一条。 */
private fun capabilityBody(rec: JsonObject, patch: JsonObject): JsonObject {
    fun str(key: String) = rec[key]?.jsonPrimitive?.contentOrNull ?: ""
    fun flag(key: String) = rec[key]?.jsonPrimitive?.booleanOrNull ?: false
    return buildJsonObject {
        put("label", str("label"))
        put("base_url", str("base_url"))
        put("model", str("model"))
        put("api_key", "")
        put("supports_vision", flag("supports_vision"))
        put("is_default", flag("is_default"))
        put("max_context_k", rec["max_context_k"]?.jsonPrimitive?.intOrNull ?: 64)
        put("supports_thinking", flag("supports_thinking"))
        put("thinking_levels", rec["thinking_levels"] ?: JsonArray(emptyList()))
        put("thinking_default", str("thinking_default"))
        put("thinking_profile", str("thinking_profile").ifEmpty { "none" })
        (rec["pricing"] as? JsonObject)?.let { put("pricing", it) }
        patch.forEach { (key, value) -> put(key, value) }
    }
}

/** 模型卡的画布态：一次最多一张卡展开（窄屏），加上写口/提示的四格外部状态。 */
private data class ModelCardUi(
    val records: Map<String, JsonObject>, val openId: String, val manage: Boolean,
    val saving: Boolean, val savedId: String, val savedText: String, val error: String,
    val clampId: String, val clampFrom: Int, val clampTo: Int,
)

/* 卡上的小胶囊钮：禁用态必须**真的**点不动（clickable(enabled=false)），
 * 不是把颜色调淡骗人；选中态吃 primary 描边。一族钮同一只手筋，与 ActionPill 同源。
 * 修复轮 Minor 5：读屏念不出"颜色变淡了"，所以禁用态要念得出「不可用：原因」。
 * 原因由调用方给 `disabledHint`——这里至少有三种"为什么点不动"（思考参数未核实 /
 * 完整记录还没读回来 / 共享模型要管理员二次验证），写死一句通用的等于又造一个假实话。
 * 名字不另加 contentDescription：钮上的 Text 本身就是可读标签，再加一份就是双读。 */
@Composable
private fun ModelMiniPill(text: String, enabled: Boolean, selected: Boolean = false,
                          disabledHint: String = "", onClick: () -> Unit) {
    val scheme = MaterialTheme.colorScheme
    val ink = when {
        !enabled -> scheme.onSurfaceVariant.copy(alpha = 0.5f)
        selected -> scheme.primary
        else -> scheme.onSurface
    }
    Box(Modifier.heightIn(min = 32.dp)
        .semantics {
            stateDescription = when {
                !enabled -> if (disabledHint.isEmpty()) "不可用" else "不可用：$disabledHint"
                selected -> "已选中"
                else -> "未选中"
            }
        }
        .background(if (selected) scheme.primaryContainer else scheme.surfaceVariant,
            CircleShape)
        .border(BorderStroke(1.dp, if (selected) scheme.primary else scheme.outline),
            CircleShape)
        .clickable(enabled = enabled, onClick = onClick)
        .padding(horizontal = 11.dp, vertical = 6.dp)) {
        Text(text, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis, color = ink)
    }
}

@Composable
private fun ModelPickerScreen(models: List<ModelInfo>, currentId: String?, card: ModelCardUi,
                              onOpenCard: (String) -> Unit, onToggleManage: (Boolean) -> Unit,
                              onBudgetTouched: () -> Unit,
                              onSaveCaps: (String, JsonObject) -> Unit,
                              onClose: () -> Unit, onPick: (String) -> Unit) {
    val scheme = MaterialTheme.colorScheme
    Column(Modifier.fillMaxSize().background(scheme.background).statusBarsPadding()
        .clickable(interactionSource = remember { MutableInteractionSource() },
            indication = null) {}) {
        Box(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp)) {
            Text("选择模型", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
                color = scheme.onSurface, modifier = Modifier.align(Alignment.Center))
            Box(Modifier.align(Alignment.CenterEnd)
                .clickable(onClick = onClose).padding(8.dp)) {
                FenverIcon(FenverIcon.Close, scheme.onSurface, Modifier.size(22.dp))
            }
        }
        // 清单 + 展开的卡都在这一列里滚：卡片撑不破视口，靠的就是"不给它固定高度"。
        Column(Modifier.fillMaxWidth().weight(1f).verticalScroll(rememberScrollState())) {
            models.forEach { m ->
                val cur = m.id == currentId
                val open = m.id == card.openId
                val status = m.thinking_status
                val statusInk = when (status) {
                    "verified" -> scheme.primary
                    "unverified" -> scheme.onSurfaceVariant
                    else -> text3Color()
                }
                Column(Modifier.fillMaxWidth()
                    .background(if (open) hoverBg() else scheme.background,
                        RoundedCornerShape(14.dp))) {
                    Row(Modifier.fillMaxWidth().heightIn(min = 56.dp)
                        .clickable { onOpenCard(m.id) }
                        .padding(horizontal = 16.dp, vertical = 8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        Box(Modifier.size(40.dp).background(scheme.surfaceVariant,
                            RoundedCornerShape(12.dp)), contentAlignment = Alignment.Center) {
                            Text(m.name.trim().take(1).ifEmpty { "?" }.uppercase(),
                                fontSize = 16.sp, fontWeight = FontWeight.SemiBold,
                                color = scheme.onSurfaceVariant)
                        }
                        // 一行装不下五件事（名/归属/三态/上限/倍率），窄屏改两行、各自省略
                        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                            Text(m.name, fontSize = 15.sp, maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                                color = if (cur) scheme.primary else scheme.onSurface,
                                fontWeight = if (cur) FontWeight.SemiBold else FontWeight.Normal)
                            Text(thinkingStatusLabel(status), fontSize = 11.5.sp,
                                maxLines = 2, overflow = TextOverflow.Ellipsis,
                                color = statusInk)
                            Text("${if (m.shared) "共享" else "我的"} · " +
                                "${if (m.max_context_k > 0) m.max_context_k else 64}K · " +
                                // 修复轮 I-1：multiplier 是可空（服务端对 per_call 定价回
                                // null），兜底写法与网页同串（app.js 的 `item.multiplier || "?x"`）
                                "${if (m.priced) (m.multiplier ?: "?x") else "未定价"}",
                                fontSize = 11.5.sp, color = text3Color(), maxLines = 1,
                                overflow = TextOverflow.Ellipsis)
                        }
                        if (cur) FenverIcon(FenverIcon.Check, scheme.primary, Modifier.size(18.dp))
                    }
                    if (open) ModelCard(item = m, card = card, onToggleManage = onToggleManage,
                        onBudgetTouched = onBudgetTouched, onSaveCaps = onSaveCaps, onPick = onPick)
                }
            }
            if (models.isEmpty())
                Text("没有可用的模型", fontSize = 13.sp, color = text3Color(),
                    modifier = Modifier.padding(horizontal = 20.dp, vertical = 12.dp))
        }
    }
}

/* 模型卡：能力读数 + 本会话预算 + 思考三态 + 上限（can_edit 才给写口）+ 设为这条。
 * 顺序有理由：预算那栏排在 can_edit 那道门**之前**——它是这台设备的设置，共享模型
 * 的用户照样能改（spec §4.2「can_edit 只约束能力字段与上限」），把门开在它前面
 * 就是"改不动别人的配置，连自己的窗口也调不了"。
 * 动态文本全走 Text(：模型名、档位标签、服务端报错原文都可能是别人写的字。 */
@Composable
private fun ModelCard(item: ModelInfo, card: ModelCardUi, onToggleManage: (Boolean) -> Unit,
                      onBudgetTouched: () -> Unit,
                      onSaveCaps: (String, JsonObject) -> Unit, onPick: (String) -> Unit) {
    val scheme = MaterialTheme.colorScheme
    val rec = card.records[item.id]
    val canEdit = item.can_edit
    /* 修复轮 I-3（说实话，不补 CookieJar）：管理员改**共享**条目的能力字段，服务端那道门
     * 是 RequireAdmin + RequireAdminReauth，而它判的是 `request.cookies` 里那张二次验证票
     * （authz.py 的 `_admin_reauth_valid`）。原生这条通路**结构上没有**：两个 OkHttpClient
     * 都没设 cookieJar（OkHttp 默认 NO_COOKIES），`Api.request()` 只加 Authorization: Bearer
     * ——所以这一格按下去必回 428。网页那边真有那条链（api.js 收到 428 弹 #reauthModal、
     * POST /v1/auth/admin-reauth 换票再重打）。真正的接线另立 **#228**，本单不做，
     * 只做两件事：把这句实话写在管理区里 + 把那颗必失败的「保存能力」钮禁用。
     * 判据取 `canEdit && item.shared`：can_edit 服务端已经算过角色，只有管理员才为 true，
     * 再放宽成只看 shared 就会把这句话印到根本没管理区的用户卡上。 */
    val reauthBlocked = canEdit && item.shared
    // Minor 5：管理区这几颗钮至少有三种"为什么点不动"，原因在这里一次算清给读屏念
    val capsHint = when {
        reauthBlocked -> "共享模型要管理员二次验证，本版没接"
        rec == null -> "这条模型的完整记录还没读回来"
        card.saving -> "正在保存"
        else -> ""
    }
    val ctlEnabled = item.thinking_status == "verified"
    val capK = if (item.max_context_k > 0) item.max_context_k else 64
    val hi = maxOf(4, capK)
    val step = if (hi <= 64) 2 else if (hi <= 256) 8 else 32
    val budgetNow = Prefs.contextBudgetK(item.id)
    var budgetK by remember(item.id, budgetNow, hi) {
        mutableIntStateOf(budgetNow.coerceIn(2, hi))
    }
    val levels = item.thinking_levels
    var thinkingOn by remember(item.id, item.thinking_status) {
        mutableStateOf(Prefs.thinkingEnabled(item.id))
    }
    var levelPick by remember(item.id, item.thinking_default, levels.joinToString("|")) {
        mutableStateOf(Prefs.thinkingLevel(item.id, levels, item.thinking_default))
    }
    // 能力编辑草稿：初值只吃服务端那份记录（本机偏好不是能力字段，别混当原料）
    val levelsText = levels.joinToString("，")
    var capDraft by remember(item.id, card.manage, capK) { mutableStateOf(capK.toString()) }
    var supDraft by remember(item.id, card.manage, item.supports_thinking) {
        mutableStateOf(item.supports_thinking)
    }
    var levelsDraft by remember(item.id, card.manage, levelsText) { mutableStateOf(levelsText) }
    var defaultDraft by remember(item.id, card.manage, levelsText, item.thinking_default) {
        mutableStateOf(item.thinking_default)
    }

    Surface(shape = RoundedCornerShape(16.dp), color = scheme.surface,
        border = BorderStroke(1.dp, scheme.outline),
        modifier = Modifier.fillMaxWidth().padding(horizontal = 10.dp, vertical = 6.dp)) {
        Column(Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(item.name, fontSize = 15.sp, fontWeight = FontWeight.SemiBold,
                color = scheme.onSurface, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text("上游 ${item.model.ifEmpty { "未填" }} · ${if (item.shared) "共享模型" else "我的模型"} · " +
                "${if (item.priced) (item.multiplier ?: "?x") else "未定价"}",
                fontSize = 12.sp, color = text3Color(), maxLines = 2,
                overflow = TextOverflow.Ellipsis)
            Text("${thinkingStatusLabel(item.thinking_status)} · 模型上限 $capK K · " +
                "${if (item.supports_vision) "支持图片" else "只收文字"}",
                fontSize = 12.sp, color = scheme.onSurface, maxLines = 2,
                overflow = TextOverflow.Ellipsis)

            // 本会话预算：与上下文账本那只滑杆读写同一份 map（Prefs，卡片不另存）
            Text("本会话预算 $budgetK/$hi K", fontSize = 13.sp, color = scheme.onSurface)
            Slider(value = budgetK.toFloat(),
                onValueChange = { v ->
                    val raw = ((v - 2) / step).toInt() * step + 2
                    budgetK = raw.coerceIn(2, hi)
                },
                valueRange = 2f..hi.toFloat(),
                onValueChangeFinished = {
                    Prefs.setContextBudget(item.id, budgetK)
                    onBudgetTouched()
                },
                // 修复轮 Minor 5：滑杆自己不带文字，读屏只能念出"滑块"——给它名字和当前格。
                // 既有的 ContextPanel 那只同缺，按审查口径不在本单范围，已登记进报告。
                modifier = Modifier.semantics {
                    contentDescription = "本会话预算"
                    stateDescription = "$budgetK / $hi K"
                })
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ModelMiniPill("用满 $hi K", enabled = true,
                    selected = budgetK >= hi) {
                    budgetK = hi
                    Prefs.setContextBudget(item.id, hi)
                    onBudgetTouched()
                }
            }
            // 上限被调小、这一格的预算因此被夹下来：说一句人话（提示绑着那张卡）
            if (card.clampId == item.id && card.clampFrom > card.clampTo) {
                Text(budgetClampNote(item.name, card.clampFrom, card.clampTo),
                    fontSize = 12.sp, color = scheme.error)
            }

            // 思考：本机偏好开关 + 档位。三态里只有 verified 给点，其余禁用并说明原因。
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("思考（这台设备自己的偏好）", fontSize = 12.sp,
                    color = if (ctlEnabled) scheme.onSurface else scheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f), maxLines = 2)
                ModelMiniPill(if (thinkingOn) "开" else "关", enabled = ctlEnabled,
                    selected = ctlEnabled && thinkingOn,
                    // Minor 5：禁用态要念得出原因，这里的原因就是那一枚三态标签本身
                    disabledHint = if (ctlEnabled) "" else thinkingStatusLabel(item.thinking_status)) {
                    thinkingOn = !thinkingOn
                    Prefs.setThinkingEnabled(item.id, thinkingOn)
                }
            }
            levels.forEach { lv ->
                // 档位候选同样只在 verified 才给点：未核实点了也只是存一个发不出去的值
                ModelMiniPill(lv, enabled = ctlEnabled,
                    selected = ctlEnabled && thinkingOn && levelPick == lv,
                    disabledHint = if (ctlEnabled) "" else thinkingStatusLabel(item.thinking_status)) {
                    levelPick = lv
                    Prefs.setThinkingLevel(item.id, lv)
                }
            }
            Text(if (ctlEnabled)
                    "本轮会带上「${if (thinkingOn) levelPick.ifEmpty { "默认档" } else "不带上思考参数"}」：" +
                        // 修复轮 Minor 8：这一段与网页 app.js 逐字对齐（上一版这里是顿号，
                        // 网页是逗号；按网页为准改原生）。同锁跨端抠串比对。
                        "开关与档位是这台设备自己的偏好，服务端不存，换设备不同步；" +
                        "这一档今天还没接进请求体（Task 12/13），界面不声称已经生效。"
                else thinkingStateReason(item.thinking_status),
                fontSize = 11.5.sp, color = text3Color(), maxLines = 4)

            Text("模型上限 $capK K · 发送按本会话预算截断（预算是本机设置，上限由服务端存）",
                fontSize = 11.5.sp, color = text3Color(), maxLines = 3)

            if (canEdit) {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(if (card.manage) "模型上限与思考能力（保存到服务端）" else "",
                        fontSize = 12.5.sp, fontWeight = FontWeight.SemiBold,
                        color = scheme.onSurface)
                    if (card.manage) {
                        // 修复轮 I-3：管理员改共享条目今天必 428（原生没有 Cookie 通道接
                        // 二次验证票），这条实话要在动手前说，别等失败那句英文令牌冒出来。
                        if (reauthBlocked) {
                            Text("共享模型的这项改动需要管理员二次验证，手机本版没接，" +
                                "请到网页管理台改（这条改动只有网页那条路今天走得通）。",
                                fontSize = 12.sp, color = scheme.error, maxLines = 3)
                        }
                        FenverTextField(value = capDraft, onValueChange = { capDraft = it },
                            placeholder = "模型上限（K token）：1..10000，越界由服务端钳住",
                            keyboardType = KeyboardType.Number,
                            enabled = rec != null && !card.saving)
                        Row(verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            ModelMiniPill(if (supDraft) "支持思考：开" else "支持思考：关",
                                enabled = rec != null && !card.saving, selected = supDraft,
                                disabledHint = capsHint) {
                                supDraft = !supDraft
                            }
                        }
                        FenverTextField(value = levelsDraft,
                            onValueChange = { levelsDraft = it },
                            placeholder = "档位标签，最多 3 个，用逗号分开",
                            enabled = rec != null && !card.saving)
                        val candidates = parseThinkingLevels(levelsDraft)
                        candidates.forEach { c ->
                            ModelMiniPill("默认档：$c", enabled = rec != null && !card.saving,
                                selected = defaultDraft == c, disabledHint = capsHint) { defaultDraft = c }
                        }
                        if (candidates.isNotEmpty() && !candidates.contains(defaultDraft)) {
                            Text("默认档只能是候选之一：现在保存会落「${candidates.first()}」。",
                                fontSize = 11.5.sp, color = text3Color())
                        }
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            // 修复轮 I-3：`!reauthBlocked` 这一枚是这一格唯一"按下去只会拿到
                            // 428"的钮，禁用它并在上面写清为什么；草稿输入框照旧能填
                            // （它们不发请求，最小改动只收这颗必失败的按钮）。
                            ModelMiniPill(if (card.saving) "保存中…" else "保存能力",
                                enabled = rec != null && !card.saving && !reauthBlocked,
                                disabledHint = capsHint) {
                                val patch = buildJsonObject {
                                    val k = capDraft.trim().toIntOrNull()
                                    if (k != null && k > 0) put("max_context_k", k)
                                    put("supports_thinking", supDraft)
                                    put("thinking_levels",
                                        JsonArray(candidates.map { JsonPrimitive(it) }))
                                    put("thinking_default",
                                        if (candidates.contains(defaultDraft)) defaultDraft
                                        else candidates.firstOrNull() ?: "")
                                }
                                onSaveCaps(item.id, patch)
                            }
                            ModelMiniPill("收起管理", enabled = true) { onToggleManage(false) }
                        }
                        Text("参数配方保持「${rec?.get("thinking_profile")
                            ?.jsonPrimitive?.contentOrNull ?: "none"}」：本版服务端白名单只有 none，" +
                            "没有可选项，所以能力改完思考状态仍是「未核实」。越界与超限由服务端判" +
                            "（钳 1..10000、档位最多 3 个），那句理由会写在这里。",
                            fontSize = 11.5.sp, color = text3Color(), maxLines = 5)
                        if (rec == null) {
                            Text("这条模型的完整记录还没读回来，保存键是禁用的：" +
                                "全量 PUT 缺了 base_url 会洗掉上游地址。",
                                fontSize = 12.sp, color = scheme.error, maxLines = 3)
                        }
                    } else {
                        ModelMiniPill("管理此模型", enabled = true) { onToggleManage(true) }
                    }
                }
            } else {
                // 只读要说清谁能改、以及"你自己那一栏照样归你改"——不然是白挂一个禁用的控件
                Text("这一条是共享模型：思考能力与模型上限由管理员维护，你这里只能读。" +
                    "上面那栏的本会话预算仍然归你改——那是这台设备自己的设置，不动别人的配置。",
                    fontSize = 11.5.sp, color = text3Color(), maxLines = 4)
            }

            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                ModelMiniPill("设为这条", enabled = true) { onPick(item.id) }
            }
            if (card.error.isNotEmpty()) {
                Text(card.error, fontSize = 12.sp, color = scheme.error, maxLines = 5)
            } else if (card.savedId == item.id && card.savedText.isNotEmpty()) {
                Text(card.savedText, fontSize = 12.sp, color = scheme.primary, maxLines = 2)
            }
        }
    }
}

/* .attach-tile：32px 图标方块（bg-hover 底、圆角 10）+ 标签。图标位收 composable，
 * 附件面板与输入栏用同一族自绘线性图标（第 5 轮）。 */
@Composable
private fun AttachTile(ico: @Composable () -> Unit, label: String, onClick: () -> Unit) {
    val scheme = MaterialTheme.colorScheme
    Row(Modifier.background(scheme.surfaceVariant, RoundedCornerShape(14.dp))
        .border(BorderStroke(1.dp, scheme.outline), RoundedCornerShape(14.dp))
        .clickable(onClick = onClick)
        .padding(start = 9.dp, end = 16.dp, top = 9.dp, bottom = 9.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        Box(Modifier.size(32.dp).background(hoverBg(), RoundedCornerShape(10.dp)),
            contentAlignment = Alignment.Center) {
            ico()
        }
        Text(label, fontSize = 14.sp, color = scheme.onSurface)
    }
}

/* .suggestions button：胶囊、surface 底、line 描边、13sp text-2。 */
@Composable
private fun SuggestionPill(text: String, onClick: () -> Unit) {
    val scheme = MaterialTheme.colorScheme
    Box(Modifier.background(scheme.surface, CircleShape)
        .border(BorderStroke(1.dp, scheme.outline), CircleShape)
        .clickable(onClick = onClick)
        .padding(horizontal = 14.dp, vertical = 6.dp)) {
        Text(text, fontSize = 13.sp, color = scheme.onSurfaceVariant, maxLines = 1)
    }
}

/* ---------------- 消息行（.msg：role 行 + 气泡 + 附件 + 工具行，条目内 gap 5） ----------------
 * 工具行手机上常显（style.css @media：.msg-tools{opacity:1}）；
 * 「重新生成」只在最后一条助手消息上出现，👍/👎 只属于助手 —— 与 messageNode 的两条
 * classList.toggle 同规则。 */
@Composable
private fun MessageRow(m: UiMsg, isLastAssistant: Boolean, editing: Boolean,
                       streaming: Boolean, seedDraft: String?,
                       copyTip: String?,
                       onStartEdit: () -> Unit, onCancelEdit: () -> Unit,
                       onSaveEdit: (String) -> Unit,
                       onCopy: (Boolean) -> Unit, onDelete: () -> Unit,
                       onRegen: () -> Unit, onFeedback: (Int) -> Unit,
                       onToggleTrace: () -> Unit, onOpenUrl: (String) -> Unit) {
    val mine = m.role == "user"
    val scheme = MaterialTheme.colorScheme
    val clipboard = LocalClipboardManager.current
    var draft by remember(editing) { mutableStateOf(seedDraft ?: m.content) }
    Column(Modifier.fillMaxWidth(),
        horizontalAlignment = if (mine) Alignment.End else Alignment.Start,
        verticalArrangement = Arrangement.spacedBy(5.dp)) {
        // .msg-role：「我」/「助手 · 模型名」，11px text-3
        Text(if (mine) "我" else "助手" + (m.model?.let { " · $it" } ?: ""),
            fontSize = 11.sp, color = text3Color(),
            modifier = Modifier.padding(horizontal = 4.dp))
        if (editing) {
            // .edit-box：accent 描边、圆角 10、surface 底；Enter 保存 / Esc 取消
            // 第 15 轮：编辑框同款工具条（和聊天输入框一个病、一个药）；
            // 第 17 轮：同款换紧凑胶囊壳、跟光标（配色也走 inverseSurface 对）
            // LocalView.current 是组合读取，必须留在 remember 计算体外（CI 编译判例）
            val editView = LocalView.current
            val editTbBg = scheme.inverseSurface.toArgb()
            val editTbFg = scheme.inverseOnSurface.toArgb()
            CompositionLocalProvider(LocalTextToolbar provides
                remember(editView, editTbBg, editTbFg) {
                    PopupTextToolbar(editView, editTbBg, editTbFg)
                }) {
            BasicTextField(
                value = draft, onValueChange = { draft = it },
                textStyle = TextStyle(fontSize = 15.sp, lineHeight = 24.sp,
                    color = scheme.onSurface),
                cursorBrush = SolidColor(scheme.primary),
                modifier = Modifier.fillMaxWidth().widthIn(max = 320.dp)
                    .background(scheme.surface, RoundedCornerShape(10.dp))
                    .border(BorderStroke(1.dp, scheme.primary), RoundedCornerShape(10.dp))
                    .padding(horizontal = 12.dp, vertical = 9.dp),
                // 网页 Enter 保存/Esc 取消；手机对位 Done 键保存（空稿=取消）
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                keyboardActions = KeyboardActions(onDone = {
                    val t = draft.trim()
                    if (t.isNotEmpty()) onSaveEdit(t) else onCancelEdit()
                }),
            )
            }
        } else {
            // 助手 surface、用户 surfaceVariant；两边同为 16dp 圆角 + 发丝线，不再有
            // 不对称气泡、渐变用户气泡与彩色阴影。
            if (mine) {
                FenverCard(
                    modifier = Modifier.widthIn(max = 320.dp),
                    color = scheme.surfaceVariant,
                ) {
                    Text(m.content, style = MaterialTheme.typography.bodyLarge,
                        color = scheme.onSurface)
                }
            } else {
                FenverCard(modifier = Modifier.fillMaxWidth(), color = scheme.surface) {
                    // 过程面板排在正文上方（网页 .msg-trace 同位）：历史落定态默认折叠，
                    // 点标题行展开。只有助手消息带得到 trace。
                    if (m.trace.isNotEmpty())
                        TracePanel(m.trace, m.traceExpanded, null, onToggleTrace, onOpenUrl)
                    RichText(m.content, scheme.onSurface)
                }
            }
        }
        // .msg-atts：图片缩略（≤240×168，圆角 10 描边）或 📄 胶囊；只挂在用户侧
        if (m.attachments.isNotEmpty()) {
            Row(Modifier.fillMaxWidth().padding(horizontal = 4.dp)
                .horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                m.attachments.forEach { a -> AttPreview(a) }
            }
        }
        // .msg-tools：复制 / 编辑(→保存) / 重新生成 / 👍 / 👎 / 删除
        Row(Modifier.fillMaxWidth().padding(horizontal = 2.dp),
            horizontalArrangement = Arrangement.spacedBy(2.dp)) {
            ToolBtn(copyTip ?: "复制") {
                val ok = runCatching {
                    clipboard.setText(AnnotatedString(m.content)); true
                }.getOrDefault(false)
                onCopy(ok)
            }
            if (editing) ToolBtn("保存", enabled = !streaming) {
                val t = draft.trim(); if (t.isNotEmpty()) onSaveEdit(t) else onCancelEdit()
            } else ToolBtn("编辑", enabled = !streaming) { onStartEdit() }
            // R1 边界条款：流式进行中禁止编辑类提交（置灰不隐藏）——网页没有这道闸，
            // 是 PRD R1-AC 明确要求原生补上的唯一有意偏差，核对记录见 T2.3 文档。
            if (!mine && isLastAssistant) ToolBtn("重新生成", enabled = !streaming) { onRegen() }
            if (!mine) {
                ToolBtn(icon = FenverIcon.ThumbUp, on = m.feedback == 1) { onFeedback(1) }
                ToolBtn(icon = FenverIcon.ThumbDown, on = m.feedback == -1) { onFeedback(-1) }
            }
            ToolBtn("删除") { onDelete() }
        }
    }
}

private fun UiMsg.textOrEmpty(): String = content


/* .msg-tools button：无边、透明底、12px text-3、padding 3/8、圆角 7；.on 吃强调色对。
 * enabled=false 是 R1 的"流式中编辑置灰"：字色再淡一档、不吃点击，按钮还在原位。
 * icon 非空时这一枚是纯图标钮（👍/👎 那两枚换进 Fenver 图标语言后就是 ThumbUp/ThumbDown），
 * 文字钮照旧：同一语义全 App 只用 FenverIcon 里那一枚。 */
@Composable
private fun ToolBtn(label: String = "", icon: FenverIcon? = null,
                    on: Boolean = false, enabled: Boolean = true,
                    onClick: () -> Unit) {
    val scheme = MaterialTheme.colorScheme
    val ink = when {
        !enabled -> text3Color().copy(alpha = 0.35f)
        on -> scheme.primary
        else -> text3Color()
    }
    Box(Modifier.background(
            if (on) scheme.primaryContainer else Color.Transparent,
            RoundedCornerShape(7.dp))
        .then(if (enabled) Modifier.clickable(onClick = onClick) else Modifier)
        .padding(horizontal = 8.dp, vertical = 3.dp)) {
        if (icon != null) FenverIcon(icon, ink, Modifier.size(16.dp))
        else Text(label, fontSize = 12.sp, color = ink)
    }
}

/* 流式中的那条助手气泡：网页 = .msg.assistant.typing，body 里实时长字 + 呼吸光标。
 * v0.29.0：正文上方先挂过程面板（流式期间默认展开，判据在 ChatScreen 的 streamTrace*）。
 * v0.30：面板在"还在想、一个字都没发"的那几十秒也要出现——原来 trace 为空就整块不挂，
 * 用户看到的只有一个光标（真机截图：气泡里就一个方块）。现在正文还没开口就先给
 * 一行「正在思考 •••」，等第一步落了地再换成带秒数的实时状态条。 */
@Composable
private fun StreamingBubble(text: String, trace: List<TraceItem>, traceExpanded: Boolean,
                            onToggleTrace: () -> Unit, onOpenUrl: (String) -> Unit,
                            approval: ApprovalState? = null,
                            onApprovalDecision: ((String) -> Unit)? = null) {
    val scheme = MaterialTheme.colorScheme
    Column(Modifier.fillMaxWidth(),
        horizontalAlignment = Alignment.Start,
        verticalArrangement = Arrangement.spacedBy(5.dp)) {
        Text("助手", fontSize = 11.sp, color = text3Color(),
            modifier = Modifier.padding(horizontal = 4.dp))
        FenverCard(modifier = Modifier.fillMaxWidth(), color = scheme.surface) {
            if (trace.isNotEmpty())
                TracePanel(trace, traceExpanded,
                    if (text.isEmpty()) "正在思考" else "正在生成", onToggleTrace, onOpenUrl)
            else if (text.isEmpty()) TraceThinkingNow()
            // v0.31 审批卡片：与 PWA .msg-approval 同一套视觉语言与文案。
            if (approval != null)
                ApprovalCard(approval, onApprovalDecision)
            if (text.isEmpty()) BlinkCursor()
            // v0.39 性能 A 包：流式期显示原始 Markdown（早退不吃解析），落定后
            // 消息行仍走完整解析恢复排版——取舍与网页流式同一句话。
            else { RichText(text, scheme.onSurface, streaming = true); BlinkCursor() }
        }
    }
}

/* 一个字都还没有的那几秒：只有呼吸三点 + 「正在思考」。
 * 与面板里的实时状态条同款动画，但这里不报秒数——还没开始跑任何一步。 */
@Composable
private fun TraceThinkingNow() {
    Row(verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        FenverIcon(FenverIcon.ThinkingDots, text3Color(), Modifier.size(22.dp), phase = thinkingPhase())
        Text("正在思考", fontSize = 12.sp, color = text3Color())
    }
}

/* 三点呼吸的相位：0→1 无限跑。单独抽出来，面板与"还没开始"那行共用一套节奏。 */
@Composable
private fun thinkingPhase(): Float {
    val tr = rememberInfiniteTransition(label = "thinkdots")
    val p by tr.animateFloat(0f, 1f,
        infiniteRepeatable(tween(1200), RepeatMode.Restart), label = "phase")
    return p
}

/* ---------------- 过程面板（v0.29.0 · 网页 .msg-trace 的原生对位；v0.30 改版） ----------------
 * 全部手搓 Column/Row/Text/clickable + Icons.kt 里的 Canvas 直绘图标 —— 本工程没有
 * markdown 库，也不为这一个面板加依赖。配色只吃文件里现成的档位（scheme + text3Color）。
 * 改版要点（对着用户给的两张参考图）：标题行说"查看 N 个步骤"、当前那一步单独一条
 * 实时状态条带秒数、步前标记一律画（字形缺了就变豆腐块）。 */

/** 客户端侧的最后一道闸：只放行 http(s)，其余（含空串）一律按纯文本处理。
 *  搜索标题/摘要/网址是别人写的网页内容——服务端过滤过一轮，这里仍不互信（纵深防御）。 */
private fun safeTraceUrl(url: String): String {
    val u = url.trim()
    val lower = u.lowercase()
    return if (lower.startsWith("http://") || lower.startsWith("https://")) u else ""
}

/** 毫秒 → 一位小数的秒：面板里所有时长同一个口径（参考图就是 "18.7s"）。 */
private fun fmtTraceSecs(ms: Int): String = "%.1fs".format(ms / 1000.0)

/** 折叠着也能读的那一行标题 + 正在跑的那一步的实时状态条。
 *
 * liveLabel=null（历史落定态）只剩标题行 + 展开后的步列表；
 * liveLabel 非空（流式中，调用方给"正在思考/正在生成"）时底部那条状态条一直在——
 * 收起着也看得见"它现在在干什么、干了多久"，这正是用户提的
 * "复杂任务我看不出想到哪一步"的答案。 */
@Composable
private fun TracePanel(items: List<TraceItem>, expanded: Boolean, liveLabel: String?,
                       onToggle: () -> Unit, onOpenUrl: (String) -> Unit) {
    val pages = items.filter { it.kind == "search" }.sumOf { it.results.size }
    val steps = items.count { it.kind != "omitted" }
    // 当前在跑的那一步：工具发出去了还没等到结果 → 报这一步的名字；否则报调用方给的态。
    val running = items.lastOrNull { it.kind == "tool" && it.pending }
    val status = if (running != null)
        running.label.ifEmpty { running.name.ifEmpty { "处理中" } }
    else liveLabel.orEmpty()
    // 计时起点挂在"是哪一步"上：换步归零，同一步反复重组不重置。
    // 服务端只有 tool_result 带 elapsed_ms，进行中的这一步没人报进度，只能客户端数。
    val clockKey = running?.id?.ifEmpty { running.label } ?: "live"
    val startedAt = remember(clockKey) { android.os.SystemClock.elapsedRealtime() }
    val chevron by animateFloatAsState(if (expanded) 180f else 0f, label = "chevron")
    FenverSurface(
        modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp),
        color = MaterialTheme.colorScheme.surfaceVariant,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
        shape = RoundedCornerShape(12.dp),
        contentPadding = PaddingValues(horizontal = 10.dp, vertical = 8.dp),
    ) {
        Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Row(Modifier.fillMaxWidth().clickable(onClick = onToggle).padding(vertical = 2.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                FenverIcon(FenverIcon.TraceEye, text3Color(), Modifier.size(14.dp))
                Text((if (expanded) "收起 " else "查看 ") + "$steps 个步骤" +
                        if (pages > 0) " · $pages 个网页" else "",
                    fontSize = 12.sp, color = text3Color(),
                    modifier = Modifier.weight(1f))
                FenverIcon(FenverIcon.ChevronDown, text3Color(), Modifier.size(14.dp).rotate(chevron))
            }
            if (expanded) items.forEach { TraceStepRow(it, onOpenUrl) }
            if (liveLabel != null) TraceLiveBar(status, startedAt)
        }
    }
}

/* 当前这一步的实时状态条（参考图："阅读中…  18.7s"）。
 * 秒数每 100ms 走一格：只重组这一条 Row，不去碰整张列表的贴底跟随。 */
@Composable
private fun TraceLiveBar(label: String, startedAt: Long) {
    val scheme = MaterialTheme.colorScheme
    val spin by rememberInfiniteTransition(label = "livebar")
        .animateFloat(0f, 360f,
            infiniteRepeatable(tween(900), RepeatMode.Restart), label = "spin")
    var now by remember(startedAt) {
        mutableStateOf(android.os.SystemClock.elapsedRealtime())
    }
    LaunchedEffect(startedAt) {
        while (true) {
            now = android.os.SystemClock.elapsedRealtime()
            delay(100)
        }
    }
    Row(Modifier.fillMaxWidth().padding(top = 2.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        FenverIcon(FenverIcon.StepBusy, scheme.primary, Modifier.size(14.dp), rotation = spin)
        Text(label, fontSize = 12.sp, color = scheme.onSurfaceVariant,
            maxLines = 1, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f))
        Text(fmtTraceSecs((now - startedAt).toInt()), fontSize = 11.sp,
            color = text3Color())
    }
}

/* ---------- v0.31 审批确认卡片 ----------
 * 与 PWA .msg-approval 同一套视觉语言（沿用状态条画法：圆角、边框、淡底）与文案。
 * 文案由契约测试钉死，漂一个字就红：
 *   标题：「执行代码确认」
 *   正文：plan_summary（服务端已生成）
 *   按钮：「允许」/「拒绝」
 * 三条出路：允许 → POST confirm → 卡片置灰；拒绝 → 同 → 本轮熔断；
 * 超时 → 票据 60s 后作废，卡片自动置灰（不重复弹）。 */

// 双端契约文案常量（与 backend/app/core/approval.py 逐字对齐）
private const val APPROVAL_TITLE = "执行代码确认"
private const val APPROVAL_ALLOW_TEXT = "允许"
private const val APPROVAL_REJECT_TEXT = "拒绝"

@Composable
private fun ApprovalCard(state: ApprovalState,
                         onDecision: ((String) -> Unit)?) {
    val scheme = MaterialTheme.colorScheme
    val resolved = state.resolved
    // 超时自动置灰：票据 expires_at 到点后卡片不再可操作（不重复弹）。
    LaunchedEffect(state.ticketId, state.expiresAt) {
        if (resolved) return@LaunchedEffect
        val remainingMs = ((state.expiresAt * 1000) - System.currentTimeMillis()).toLong()
        if (remainingMs > 0) {
            kotlinx.coroutines.delay(remainingMs)
        }
        // 到期后如果还没决定，标记为已解决（超时作废）
        if (!state.resolved) {
            onDecision?.invoke("__timeout__")
        }
    }
    FenverCard(
        modifier = Modifier.fillMaxWidth(),
        color = scheme.surfaceVariant,
        shape = RoundedCornerShape(12.dp),
        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 10.dp),
    ) {
        Text(APPROVAL_TITLE, style = MaterialTheme.typography.labelLarge,
            color = scheme.onSurface)
        Spacer(Modifier.height(4.dp))
        Text(state.planSummary, style = MaterialTheme.typography.bodySmall,
            color = scheme.onSurfaceVariant,
            maxLines = 1, overflow = TextOverflow.Ellipsis)
        Spacer(Modifier.height(8.dp))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FenverButton(
                text = APPROVAL_REJECT_TEXT,
                onClick = { onDecision?.invoke("reject") },
                kind = FenverButtonKind.Secondary,
                enabled = !resolved,
                modifier = Modifier.weight(1f),
            )
            FenverButton(
                text = APPROVAL_ALLOW_TEXT,
                onClick = { onDecision?.invoke("allow") },
                kind = FenverButtonKind.Primary,
                enabled = !resolved,
                modifier = Modifier.weight(1f),
            )
        }
    }
}

/* ---------- v0.35 #99「需要你输入」卡片 ----------
 * 形状照 v0.31 的 ApprovalCard（同一档底色/描边/圆角与字号），内容与 PWA 的
 * .msg-task 同一套文案：标题、问题原文、进度/等待那一行、回答输入、状态句与错误句。
 * 服务端文本一律当字面量渲染，不拼 HTML；按钮只有一枚「提交」，在途时输入和按钮一起
 * 置灰——同一枚断点只许一个答案接走（服务端 _claim_execution 的闸），双击送出的
 * 第二枪只会拿到一句"没接住"，白等一次长阻塞。 */
@Composable
private fun TaskWaitingCard(view: TaskCard.View, draft: String, busy: Boolean,
                            error: String, onDraft: (String) -> Unit,
                            onSubmit: () -> Unit) {
    val scheme = MaterialTheme.colorScheme
    val canSubmit = !busy && draft.trim().isNotEmpty()
    FenverCard(
        modifier = Modifier.fillMaxWidth(),
        color = scheme.surfaceVariant,
        shape = RoundedCornerShape(12.dp),
        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 10.dp),
    ) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(TaskCard.TITLE, style = MaterialTheme.typography.labelLarge,
                color = scheme.onSurface, modifier = Modifier.weight(1f))
            if (view.state.isNotEmpty())
                Text(view.state, style = MaterialTheme.typography.labelSmall,
                    color = text3Color())
        }
        if (view.question.isNotEmpty())
            Text(view.question, style = MaterialTheme.typography.bodyMedium,
                color = scheme.onSurfaceVariant)
        val meta = listOf(view.progress, view.waiting)
            .filter { it.isNotEmpty() }.joinToString(" · ")
        if (meta.isNotEmpty())
            Text(meta, style = MaterialTheme.typography.labelSmall, color = text3Color())
        Spacer(Modifier.height(4.dp))
        BasicTextField(
            value = draft, onValueChange = onDraft, enabled = !busy,
            textStyle = TextStyle(fontSize = 14.sp, lineHeight = 21.sp,
                color = scheme.onSurface),
            cursorBrush = SolidColor(scheme.primary),
            modifier = Modifier.fillMaxWidth()
                .background(scheme.surface, RoundedCornerShape(12.dp))
                .border(BorderStroke(1.dp, scheme.outline), RoundedCornerShape(12.dp))
                .padding(horizontal = 10.dp, vertical = 8.dp),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
            keyboardActions = KeyboardActions(onSend = { if (canSubmit) onSubmit() }),
            decorationBox = { inner ->
                Box {
                    if (draft.isEmpty())
                        Text(if (busy) "正在提交…" else "在这里输入你的回答",
                            fontSize = 14.sp, color = text3Color())
                    inner()
                }
            })
        if (error.isNotEmpty())
            Text(error, fontSize = 11.sp, lineHeight = 16.sp, color = scheme.error)
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            FenverButton(
                text = if (busy) "提交中…" else "提交",
                onClick = onSubmit,
                kind = FenverButtonKind.Primary,
                enabled = canSubmit,
                modifier = Modifier.width(104.dp),
            )
        }
    }
}

/* 搜索源名的上屏口径（v0.32 W3）。与 PWA app.js、backend/app/core/stream_events.py
 * 同一张映射表、同一句连接词——三端逐字互锁，谁也不许自己另写一套。 */
private const val SEARCH_SOURCE_PREFIX = " · 来自 "
private val SEARCH_SOURCE_LABELS =
    mapOf("bocha" to "博查", "tavily" to "Tavily", "bing" to "必应")

internal fun searchSourceName(source: String): String {
    val s = source.trim()
    if (s.isEmpty()) return ""
    return SEARCH_SOURCE_LABELS[s] ?: s
}

@Composable
private fun TraceStepRow(t: TraceItem, onOpenUrl: (String) -> Unit) {
    val scheme = MaterialTheme.colorScheme
    val spin by rememberInfiniteTransition(label = "stepicon")
        .animateFloat(0f, 360f,
            infiniteRepeatable(tween(900), RepeatMode.Restart), label = "spin")
    when (t.kind) {
        // 思考文本与答案一眼分开：矮一档墨色 + 左边一道竖线（参考图的排版）。
        // 原来靠斜体区分，斜体在中文里没有字形、只会合成拉伸，读着更累。
        "thinking" -> Text(t.text, fontSize = 13.sp, lineHeight = 20.sp,
            color = scheme.onSurfaceVariant,
            modifier = Modifier
                .drawBehind {
                    drawLine(scheme.outline, Offset(0f, 0f), Offset(0f, size.height),
                        strokeWidth = 2.dp.toPx())
                }
                .padding(start = 10.dp)
                .fillMaxWidth())
        "omitted" -> Text("…中间 ${t.omitted} 步已省略…",
            fontSize = 12.sp, color = text3Color(),
            modifier = Modifier.fillMaxWidth().padding(vertical = 2.dp))
        "tool" -> Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                // 状态标记改成画出来的：✓/✗/▋ 这些字形在部分设备上没有，
                // 缺字就成实心方块（v0.30 用户截图里那条"只有方块的气泡"）。
                when {
                    t.pending -> FenverIcon(FenverIcon.StepBusy, text3Color(), Modifier.size(14.dp), rotation = spin)
                    t.ok -> FenverIcon(FenverIcon.StepDone, scheme.primary, Modifier.size(14.dp))
                    else -> FenverIcon(FenverIcon.StepFailed, scheme.error, Modifier.size(14.dp))
                }
                Text(t.label.ifEmpty { t.name.ifEmpty { "工具调用" } },
                    fontSize = 13.sp, color = scheme.onSurface,
                    modifier = Modifier.weight(1f))
                if (!t.pending && t.elapsedMs > 0)
                    Text(fmtTraceSecs(t.elapsedMs), fontSize = 11.sp, color = text3Color())
            }
            if (!t.pending && t.summary.isNotEmpty())
                Text(t.summary, fontSize = 12.sp, lineHeight = 18.sp,
                    color = scheme.onSurfaceVariant)
            if (t.truncated)
                Text("输出过长，给模型的那一份已截断", fontSize = 11.sp, color = text3Color())
        }
        "search" -> Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                FenverIcon(FenverIcon.TraceEye, scheme.primary, Modifier.size(14.dp))
                // W3：步带 source 时在标题行尾追加「 · 来自 X」；没有就连前缀一起省，
                // 与 PWA traceStepNode 的 search 臂逐字同文案。
                val src = searchSourceName(t.source)
                Text("搜索：${t.query}" + if (src.isEmpty()) "" else SEARCH_SOURCE_PREFIX + src,
                    fontSize = 12.sp, color = text3Color())
            }
            t.results.forEach { TraceResultRow(it, onOpenUrl) }
        }
        // 认不出的步（服务端以后加的新 kind）：能给字就给字，猜形状是错的方向
        else -> if (t.text.isNotEmpty())
            Text(t.text, fontSize = 12.sp, color = text3Color())
    }
}

/* 一条命中网页：标题 + 站点名一行、摘要下一行。标题只有过了 safeTraceUrl 那道闸
 * 才挂点击（走 ChatScreen 既有的 onOpenUrl 出口），否则整行都是死的纯文本。 */
@Composable
private fun TraceResultRow(r: TraceHit, onOpenUrl: (String) -> Unit) {
    val scheme = MaterialTheme.colorScheme
    val url = safeTraceUrl(r.url)
    Column(Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(r.title.ifEmpty { url.ifEmpty { "（无标题）" } },
                fontSize = 13.sp, maxLines = 2, overflow = TextOverflow.Ellipsis,
                color = if (url.isNotEmpty()) scheme.primary else scheme.onSurfaceVariant,
                modifier = if (url.isNotEmpty())
                    Modifier.weight(1f).clickable { onOpenUrl(url) }
                else Modifier.weight(1f))
            // 站点名从已过闸的 url 里解析；解析不出就空着，绝不拿原 url 充数
            val host = if (url.isNotEmpty())
                runCatching { java.net.URI(url).host }.getOrNull().orEmpty() else ""
            if (host.isNotEmpty())
                Text(host, fontSize = 11.sp, color = text3Color(), maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.widthIn(max = 110.dp))
        }
        if (r.snippet.isNotEmpty())
            Text(r.snippet, fontSize = 12.sp, lineHeight = 17.sp,
                maxLines = 3, overflow = TextOverflow.Ellipsis, color = text3Color())
    }
}

/* .typing .msg-body::after { content:"▋"; animation: blink 1s steps(2) } —— 两帧硬切。
 * v0.30 改成画出来的竖条：网页上 ▋ 有字体兜着，原生这边这台设备根本没这个字形，
 * 真机截图中那条空气泡里就剩一块实心绿方块（缺字渲染）。不再赌字体覆盖。 */
@Composable
private fun BlinkCursor() {
    val tr = rememberInfiniteTransition(label = "cursor")
    val a by tr.animateFloat(1f, 0f,
        infiniteRepeatable(tween(500), RepeatMode.Reverse), label = "blink")
    Box(Modifier.width(3.dp).height(16.dp)
        .background(MaterialTheme.colorScheme.primary.copy(alpha = a),
            RoundedCornerShape(1.5.dp)))
}

/* ---------------- 附件缩略图：按 id 现取字节（网页 fileBlobUrl 的原生对位） ----------------
 * LruCache 32 张；取不到 → 📄 文件名胶囊（hydrateImageUrls 的 catch 就是这句"只显示文件名"）。 */
private object AttCache {
    val mem = android.util.LruCache<String, android.graphics.Bitmap>(48)
}

@Composable
private fun AttPreview(a: AttItem) {
    val scheme = MaterialTheme.colorScheme
    val tag: @Composable () -> Unit = {
        Row(Modifier.background(scheme.surfaceVariant, CircleShape)
            .border(BorderStroke(1.dp, scheme.outline), CircleShape)
            .padding(horizontal = 10.dp, vertical = 2.dp),
            verticalAlignment = Alignment.CenterVertically) {
            // 📄 字形换进图标语言：File 一枚 + 文件名（取不到缩略图时的降级胶囊原语义）
            FenverIcon(FenverIcon.File, scheme.onSurfaceVariant, Modifier.size(14.dp))
            Spacer(Modifier.width(6.dp))
            Text(a.name, fontSize = 12.sp, color = scheme.onSurfaceVariant)
        }
    }
    if (a.kind != "image") { tag(); return }
    var bmp by remember(a.id) { mutableStateOf(AttCache.mem.get(a.id)) }
    LaunchedEffect(a.id) {
        if (bmp == null) runCatching { Api.uploadBytes(a.id) }.onSuccess { bytes ->
            // 解码是一段不短的 CPU 工作（还有 2× 下采样），放默认调度器，别占主线程。
            val b = withContext(Dispatchers.Default) {
                val opts = android.graphics.BitmapFactory.Options().apply { inSampleSize = 2 }
                android.graphics.BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opts)
            }
            if (b != null) { AttCache.mem.put(a.id, b); bmp = b }
        }
    }
    val b = bmp
    if (b == null) { tag(); return }
    Image(b.asImageBitmap(), contentDescription = a.name,
        contentScale = ContentScale.Fit,
        modifier = Modifier.widthIn(max = 240.dp).heightIn(max = 168.dp)
            .clip(RoundedCornerShape(10.dp))
            .border(BorderStroke(1.dp, scheme.outline), RoundedCornerShape(10.dp)))
}

/* 轻量 Markdown：``` 围栏内是代码卡（--code-bg 深底、line 描边、圆角 14、等宽 13/1.6，
 * 横向可滚），围栏之外再认 **加粗**。完整 MD 排版留给网页版；原生保证可读、形状对得上。
 *
 * 加粗这一层是 2026-10-03 用户点名补的：满屏的 `**` 号。原因不是正则写错，也不是
 * emoji / 全角括号不匹配——是**从来就没有实现过**：整个 RichText 只切 ``` 围栏，
 * 其余一律当纯文本吐给 Text()。所以模型按 Markdown 写的每一条加粗，在原生端都
 * 带着星号原样上屏，而网页版（marked + DOMPurify）一直是正常的。
 *
 * 只认成对的 `**`：单个 `*` 留给算术（"2 * 3 * 4"不许变斜体，那是另一种坏）。
 * 流式期间遇到只开了头还没闭合的 `**`，按原样显示——宁可晚一帧变粗，也不许把
 * 已经到手的字吞掉。 */
internal fun boldSegments(src: String): List<Pair<String, Boolean>> {
    val out = ArrayList<Pair<String, Boolean>>()
    var rest = src
    while (true) {
        val open = rest.indexOf("**")
        if (open < 0) { if (rest.isNotEmpty()) out.add(rest to false); break }
        if (open > 0) out.add(rest.substring(0, open) to false)
        rest = rest.substring(open + 2)
        val close = rest.indexOf("**")
        if (close < 0) { out.add("**" + rest to false); break }
        out.add(rest.substring(0, close) to true)
        rest = rest.substring(close + 2)
    }
    return out
}

private fun boldAnnotated(src: String, color: Color): AnnotatedString {
    val b = AnnotatedString.Builder()
    b.pushStyle(SpanStyle(color = color))
    for ((seg, isBold) in boldSegments(src)) {
        if (isBold) {
            b.pushStyle(SpanStyle(fontWeight = FontWeight.Bold))
            b.append(seg)
            b.pop()
        } else {
            b.append(seg)
        }
    }
    return b.toAnnotatedString()
}

/* 围栏切分从组合里抽出来：同一段文本只在它变化时解析一次（remember(text)），
 * 流式追加才重新切，其余重组不再对全文重扫。判定规则与抽出来之前逐字一致：
 * 围栏后第一行若在 40 字符内当语言标记吞掉，闭合围栏后同规则吞掉换行。 */
internal fun parseRichTextParts(text: String): List<Pair<String, Boolean>> {
    val parts = ArrayList<Pair<String, Boolean>>()
    var rest = text
    while (true) {
        val open = rest.indexOf("```")
        if (open < 0) { parts.add(rest to false); break }
        if (open > 0) parts.add(rest.substring(0, open) to false)
        rest = rest.substring(open + 3)
        val nl = rest.indexOf('\n').takeIf { it >= 0 && it < 40 } ?: 0
        if (nl > 0) rest = rest.substring(nl + 1)
        val close = rest.indexOf("```")
        if (close < 0) { parts.add(rest to true); break }
        parts.add(rest.substring(0, close) to true)
        rest = rest.substring((close + 3).let {
            val after = rest.indexOf('\n', it)
            if (after >= 0 && after - it < 40) after + 1 else it
        })
    }
    return parts
}

/** 正文渲染。streaming=true（流式期）时**早退**：一条裸 Text 直接上屏——
 *  半截围栏、半截加粗都还没定形，逐 token 重切全文就是"每帧整页重组"的 CPU 大头；
 *  代价是流式期屏幕上看得到原始 Markdown 标记（`**`、``` ），终稿落进 messages 后
 *  消息行按默认 streaming=false 走完整解析恢复排版。判据见
 *  test_v0390_native_stream_perf.py 与 test_native_bold_markdown_v0292.py。 */
@Composable
internal fun RichText(text: String, color: Color, streaming: Boolean = false) {
    if (streaming) {
        Text(text, fontSize = 16.sp, lineHeight = 24.sp, color = color)
        return
    }
    val parts = remember(text) { parseRichTextParts(text) }
    Column {
        parts.forEach { (seg, code) ->
            if (code) {
                val preShape = RoundedCornerShape(14.dp)
                Text(seg.trimEnd('\n'),
                    fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace,
                    fontSize = 13.sp, lineHeight = 21.sp,
                    color = FenverTokens.Text,
                    modifier = Modifier.fillMaxWidth()
                        .padding(vertical = 4.dp)
                        .background(FenverTokens.CodeBg, preShape)
                        .border(1.dp, FenverTokens.Line, preShape)
                        .padding(horizontal = 14.dp, vertical = 12.dp)
                        .horizontalScroll(rememberScrollState()))
            } else if (seg.isNotBlank()) {
                // 同一段文本每次重组都重拼一遍 AnnotatedString 是白烧：按段 + 颜色缓存。
                val annotated = remember(seg, color) { boldAnnotated(seg, color) }
                Text(annotated, fontSize = 16.sp, lineHeight = 24.sp)
            }
        }
    }
}

/* ---------------- 第 11 轮：输入框恢复系统默认文字工具条 ----------------
 * 第 9 轮的空壳工具条整块退役——用户点名「要和deepseek一模一样」：键盘起着时
 * 长按输入框要在光标旁出复制/粘贴/全选。键盘没起的窗口里输入位是占位条（闸门
 * 分支），单点不挂框、不冒菜单；长按归语音。两头各走各的，不再需要扣壳。 */

