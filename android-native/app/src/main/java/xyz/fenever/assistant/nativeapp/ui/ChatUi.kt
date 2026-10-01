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
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeIn
import androidx.compose.animation.slideInVertically
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
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
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.platform.LocalTextToolbar
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.platform.TextToolbar
import androidx.compose.ui.platform.TextToolbarStatus
import androidx.compose.ui.text.AnnotatedString
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
import androidx.core.view.WindowInsetsCompat
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import xyz.fenever.assistant.nativeapp.Api
import xyz.fenever.assistant.nativeapp.ApiException
import xyz.fenever.assistant.nativeapp.ChatEvent
import xyz.fenever.assistant.nativeapp.ChatMessageDto
import xyz.fenever.assistant.nativeapp.ModelInfo
import xyz.fenever.assistant.nativeapp.Prefs
import xyz.fenever.assistant.nativeapp.SessionSummary
import xyz.fenever.assistant.nativeapp.StoredMessage
import xyz.fenever.assistant.nativeapp.theme.AiBrandMark
import xyz.fenever.assistant.nativeapp.theme.AiGlowBackground
import xyz.fenever.assistant.nativeapp.theme.WebTokens
import xyz.fenever.assistant.nativeapp.theme.aiPrimaryBrush
import xyz.fenever.assistant.nativeapp.theme.glassColor
import xyz.fenever.assistant.nativeapp.theme.glassStrongColor
import xyz.fenever.assistant.nativeapp.theme.isWebLight
import xyz.fenever.assistant.nativeapp.theme.text3Color
import xyz.fenever.assistant.nativeapp.theme.userBubbleBrush

/* 聊天主屏 —— 逐块对位旧壳加载的网页（backend/app/web/static）：
 * index.html 的 .topbar/.messages/.composer 结构，style.css 移动端那档数值
 * （@media max-width:860 就是手机上的真身），行为语义逐条照 app.js 移植。
 * 侧栏 = 拉开的抽屉（SessionDrawer）；设置 = 底部弹层，二级页在同一张弹层里
 * 换 view —— "谁都不该是第二个弹窗"。 */

/* .att-chip / .msg-atts 的附件条目：网页字段 {id,name,kind,size,url}；
 * 原生没有 blob URL，图片按 id 现取字节解码，取不到退化成 📄 标签（同一降级语义）。 */
data class AttItem(val id: String, val name: String, val kind: String, val size: Long)

/* 一条已落地的消息。transient = 网页 runStream 失败那条 ⚠️ 气泡：看得见，
 * 但 replaceMessages/outbound 都把它滤掉，不发给服务端、不混进上下文。 */
data class UiMsg(val role: String, val content: String,
                 val messageId: String? = null, val model: String? = null,
                 val feedback: Int? = null,
                 val attachments: List<AttItem> = emptyList(),
                 val transient: Boolean = false)

/* 网页 .suggestions 的四枚 chips（app.js SUGGESTIONS 逐字）。点击只填输入框、不发送。
 * 2026-09-28 真机反馈：原来四条偏"技术演示"，换成日常真会问的事。 */
val CHAT_SUGGESTIONS = listOf(
    "今天晚饭吃什么？帮我想个菜单",
    "帮我写条请假消息，自然一点",
    "这段话帮我改得更客气一点",
    "周末在家无聊，推荐点事做",
)

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

    // ---------- 状态：对照 app.js 的 state / pref ----------
    var sessionId by remember { mutableStateOf(Prefs.lastSessionId) }
    var sessions by remember { mutableStateOf(listOf<SessionSummary>()) }
    var messages by remember { mutableStateOf(listOf<UiMsg>()) }
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
    var attachOpen by remember { mutableStateOf(false) }
    var modelMenuOpen by remember { mutableStateOf(false) }
    var drawerTick by remember { mutableStateOf(0) }
    var settingsPage by remember { mutableStateOf<String?>(null) }  // null=关；""=一级列表
    var streamJob by remember { mutableStateOf<Job?>(null) }
    var stopRequested by remember { mutableStateOf(false) }
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
    val listState = rememberLazyListState()
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

    fun logoutIf401(e: Throwable): Boolean {
        if (e is ApiException && e.status == 401) { Prefs.clearAuth(); onLoggedOut(); return true }
        return false
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

    // ---------- 冷启动：模型清单 + 会话列表（网页 boot 的 loadModels/loadSessions） ----------
    suspend fun loadModelsNow() {
        runCatching { Api.models() }.onSuccess { r ->
            providers = r.models
            serverDefault = r.default
            if (providers.none { it.id == Prefs.defaultProviderId && it.usable })
                serverDefaultProvider()?.let { Prefs.defaultProviderId = it.id }
            if (providers.any { it.usable }) setStatus("")
        }.onFailure { if (!logoutIf401(it)) setStatus(it.message ?: "", true) }
    }
    fun refreshSessions() {
        scope.launch { runCatching { Api.listSessions() }
            .onSuccess { sessions = it }.onFailure { logoutIf401(it) } }
    }
    LaunchedEffect(Unit) { loadModelsNow(); refreshSessions() }

    // switchSession：getSession 拉全量消息（含服务端留存的附件条目）
    fun openSession(id: String) {
        sessionId = id; Prefs.lastSessionId = id
        // 换会话必关编辑态：编辑框是【这一条会话里第 index 行】的承诺，换列表不兑现
        editingIndex = null; editingDraft = null
        scope.launch {
            runCatching { Api.getSession(id) }.onSuccess { d ->
                messages = d.messages.map { m ->
                    UiMsg(m.role, m.content, m.message_id, m.model,
                        attachments = (m.attachments ?: emptyList())
                            .map { AttItem(it.id, it.name, it.kind, it.size) })
                }
            }.onFailure { if (!logoutIf401(it)) setStatus(it.message ?: "", true) }
        }
    }

    // 抽屉拉开就重拉列表（网页不留快照）
    LaunchedEffect(drawerState.currentValue) {
        if (drawerState.currentValue == DrawerValue.Open) {
            drawerTick++
            scope.launch { runCatching { Api.listSessions() }.onSuccess { sessions = it } }
        }
    }

    // 贴底跟随：网页 paint() 的语义 —— 本来贴底才滚，上翻阅读不被打断
    LaunchedEffect(messages.size, streamText) {
        val info = listState.layoutInfo
        val total = info.totalItemsCount
        if (total > 0) {
            val lastVisible = info.visibleItemsInfo.lastOrNull()?.index ?: 0
            if (lastVisible >= total - 2) listState.scrollToItem(total - 1)
        }
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
            Api.replaceMessages(sessionId, list.filter { !it.transient }
                .map { StoredMessage(it.role, it.content, it.messageId, it.model) })
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
                    sessionId = ""
                    Prefs.lastSessionId = ""
                    messages = emptyList()
                    editingIndex = null; editingDraft = null
                    runCatching { sessions = Api.listSessions() }
                    false
                }
                else -> { setStatus("同步到服务端失败：" + e.message, true); false }
            }
        }
    }

    fun outboundMsgs(): List<ChatMessageDto> =
        Api.outbound(messages.filter { it.content.isNotEmpty() && !it.transient }
            .map { ChatMessageDto(it.role, it.content) },
            Prefs.persona(sessionId), Prefs.contextTokensK, modelCapK())

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

    /* ---------------- runStream（app.js send/runStream 的移植） ----------------
     * content 增量 → 局部气泡重画；done → 落 message_id/model；
     * 流内 error 事件 = 不可重试（⚠️ 占位气泡，不重发，避免重复计费）；
     * 通道层失败 = 可重试 → 回退非流式 /v1/chat；
     * 停止 = 半截内容 +「（已停止生成）」照样落本地并持久化（网页 AbortError 一支如此）。 */
    fun streamInto() {
        if (busy) return
        streamJob = scope.launch {
            busy = true; stopRequested = false
            streamText = ""
            setStatus("生成中…")
            val acc = StringBuilder()
            var doneId: String? = null
            var doneModel: String? = null
            var failed = false
            var stopped = false
            val pid = currentProvider()?.id
            val atts = messages.lastOrNull { it.role == "user" }?.attachments
                ?.map { it.id } ?: emptyList()
            val payload = Api.chatPayload(pid, pid, outboundMsgs(), atts, sessionId)
            try {
                Api.streamChat(payload).collect { ev ->
                    if (stopRequested) throw CancellationException("stop")
                    when (ev) {
                        is ChatEvent.Start -> Unit
                        is ChatEvent.Content -> { acc.append(ev.text); streamText = acc.toString() }
                        is ChatEvent.Done -> { doneId = ev.messageId; doneModel = ev.model }
                        is ChatEvent.Failed -> throw ApiException(-1, ev.message)
                    }
                }
                NetMinder.noteSuccess()
                messages = messages + UiMsg("assistant", acc.toString(), doneId,
                    doneModel?.ifBlank { null })
            } catch (e: CancellationException) {
                stopped = true
                messages = messages + UiMsg("assistant",
                    acc.toString() + "\n\n（已停止生成）", doneId, doneModel?.ifBlank { null })
                setStatus("已停止生成")
            } catch (e: Exception) {
                NetMinder.noteFailure(e)
                val retryable = !(e is ApiException && e.status == -1)
                val fallback = try {
                    if (retryable) Api.chat(payload) else null
                } catch (e2: Exception) {
                    NetMinder.noteFailure(e2)
                    if (!logoutIf401(e2)) setStatus(e2.message ?: "", true)
                    null
                }
                if (fallback != null) {
                    NetMinder.noteSuccess()
                    messages = messages + UiMsg("assistant", fallback.reply,
                        fallback.message_id.ifBlank { null })
                } else {
                    failed = true
                    val msg = e.message ?: "请求失败"
                    messages = messages + UiMsg("assistant",
                        (if (acc.isNotEmpty()) acc.toString() + "\n\n" else "") + "⚠️ " + msg,
                        transient = true)
                    if (!logoutIf401(e)) setStatus(msg, true)
                }
            } finally {
                streamText = null; busy = false; streamJob = null
            }
            // persistCurrent + loadSessions：标题可能因首条消息被服务端改掉（顶栏要跟着新）
            withContext(NonCancellable) {
                persist(messages)
                runCatching { sessions = Api.listSessions() }
            }
            if (!failed && !stopped) setStatus("")
        }
    }

    /* app.js send()：空输入且没有附件 → 什么都不做；清单还没回来先补拉一次；
     * 没有可用模型 → 指路文案（管理员/普通用户两句不一样）并打开「模型服务」。 */
    fun sendNow(text: String) {
        if (busy) return
        val t = text.trim()
        if (t.isEmpty() && pending.isEmpty()) return
        streamJob = null
        scope.launch {
            if (providers.isEmpty()) loadModelsNow()
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
            input = TextFieldValue("")
            streamInto()
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
    val root = LocalView.current
    val keyboardController = LocalSoftwareKeyboardController.current
    val frameKeyboardUp = rememberKeyboardVisible()
    // 第 15 轮改判（用户真机：「点键盘下箭头收了键盘，输入框还停在打字态」）：
    // 第 14 轮拿焦点当键盘代理，收键盘不收焦点——信号永远卡在"有键盘"，胶囊回不去。
    // 焦点信号整个撤下，换 rememberKeyboardVisible 里的窗口绝对高度缩水信号：
    // 键盘起=窗被顶扁（本 App manifest 声明 adjustResize，第 9 轮真机已证），
    // 键盘收=窗弹回，无论用什么方式收都能测到。ime 内衬那路保留兜别的机型。
    val keyboardUp = WindowInsets.ime.getBottom(density) > 0 || frameKeyboardUp
    // 第 9 轮：这 ROM 上键盘的 show 动画会吞掉同窗口里紧随的一次 hide() 调用——
    // 第 8 轮单发 stashIme 真机实测压不住（胶囊和键盘照样同屏）。改成连发补刀：
    // 先发落在动画里也无妨，后面 4 发（180/360/540/720ms）落在动画结束之后必收。
    // 必须 remember：键盘升降正会触发重组，裸 new Handler 会让 DisposableEffect
    // 的 key 每轮都变、把排队中的补刀全清掉——自己拆自己的台。
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
            runCatching { Api.feedback(m.messageId, rating) }.onSuccess {
                // 网页：同值再点撤销；message_id 缺失那句错误在上方已拦
                messages = messages.mapIndexed { i, x ->
                    if (i == index) x.copy(feedback = if (x.feedback == rating) null else rating) else x
                }
            }.onFailure { if (!logoutIf401(it)) setStatus("反馈失败：" + it.message, true) }
        }
    }

    // 顶栏标题 = syncTopTitle：在会话清单里查当前 id，查不到就是「新对话」
    val topTitle = sessions.firstOrNull { it.session_id == sessionId }?.title
        ?.ifBlank { "新对话" } ?: "新对话"

    fun closeDrawer() { scope.launch { drawerState.close() } }

    // 第 11 轮改判（对齐 DeepSeek/豆包）：输入框恢复系统默认文字工具条——键盘起着
    // 时长按输入框出复制/粘贴/全选（光标旁）；键盘没起时输入位是占位条，单点不挂框
    // 不冒菜单、长按归语音。语音长按只在键盘收起（或语音模式）时接管，靠 voiceHold
    // 的 enabled 门控分流。
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
                onNewChat = {
                    // 网页 newChat：先清空再 ensureSession —— 新对话立刻建出来
                    scope.launch {
                        sessionId = ""; Prefs.lastSessionId = ""
                        if (ensureSession()) closeDrawer()
                    }
                },
                onOpen = { id -> openSession(id); closeDrawer() },
                onMemory = { settingsPage = "memory"; closeDrawer() },
                onSettings = { settingsPage = ""; closeDrawer() },
                onModelService = { settingsPage = "providers"; closeDrawer() },
                onDeleted = { id ->
                    if (id == sessionId) {
                        sessionId = ""; Prefs.lastSessionId = ""; messages = emptyList()
                    }
                },
                onLoggedOut = onLoggedOut,
                onClose = { closeDrawer() },
            )
        },
    ) {
        AiGlowBackground {
            Column(Modifier.fillMaxSize()) {
                // ---------- .topbar：☰ + 标题；玻璃底 + 1px 下边框 ----------
                Row(Modifier.fillMaxWidth().background(glassColor())
                    .padding(horizontal = 12.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Box(Modifier.clickable { scope.launch { drawerState.open() } }
                        .padding(6.dp)) {
                        IconMenu(MaterialTheme.colorScheme.onSurfaceVariant, Modifier.size(18.dp))
                    }
                    Text(topTitle, fontSize = 15.sp, fontWeight = FontWeight.SemiBold,
                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f))
                }
                Box(Modifier.fillMaxWidth().height(1.dp)
                    .background(MaterialTheme.colorScheme.outline))

                // ---------- .messages：padding 14/12/6，条目间 gap 22 ----------
                LazyColumn(Modifier.weight(1f).fillMaxWidth().imePadding()
                    .padding(horizontal = 12.dp),
                    state = listState,
                    contentPadding = PaddingValues(top = 14.dp, bottom = 6.dp),
                    verticalArrangement = Arrangement.spacedBy(22.dp)) {
                    if (messages.isEmpty() && !busy && streamText == null) item {
                        EmptyHero()
                    }
                    itemsIndexed(messages) { index, m ->
                        MessageRow(
                            m = m,
                            isLastAssistant = m.role == "assistant" &&
                                index == messages.lastIndex && streamText == null,
                            editing = editingIndex == index,
                            streaming = busy || streamText != null,
                            seedDraft = editingDraft,
                            copyTip = copyTip?.takeIf { it.first == index }?.second,
                            onStartEdit = { editingDraft = null; editingIndex = index },
                            onCancelEdit = { editingIndex = null; editingDraft = null },
                            onSaveEdit = { text ->
                                scope.launch {
                                    val next = messages.take(index).toMutableList()
                                    next.add(messages[index].copy(content = text))
                                    // T2.2 AC-3：先落服务端，失败就退回编辑态且草稿原样保留
                                    // （网页同判据：commit 里 await 抛错就不 renderMessages，
                                    // 编辑框和字都还在，按下一次「保存」即重试）。
                                    if (!persist(next)) {
                                        if (index <= messages.lastIndex) {
                                            editingIndex = index; editingDraft = text
                                        } else { editingIndex = null; editingDraft = null }
                                        return@launch
                                    }
                                    editingIndex = null; editingDraft = null
                                    messages = next
                                    // 网页：编辑用户消息后重新生成后续（重走一轮流式）
                                    if (next[index].role == "user") streamInto()
                                }
                            },
                            onCopy = { ok -> copyTip = index to (if (ok) "已复制" else "失败") },
                            onDelete = {
                                scope.launch {
                                    val next = messages.filterIndexed { i, _ -> i != index }
                                    persist(next)
                                    messages = next
                                }
                            },
                            onRegen = {
                                scope.launch {
                                    val keep = messages.take(index)
                                    persist(keep)
                                    messages = keep
                                    streamInto()
                                }
                            },
                            onFeedback = { sendFeedback(index, it) },
                        )
                    }
                    streamText?.let { s -> item { StreamingBubble(s) } }
                }

                // ---------- .composer：附件行 / 建议 / 输入卡 / 下挂两张玻璃面板 ----------
                Column(Modifier.fillMaxWidth().imePadding()
                    .padding(horizontal = 12.dp)
                    .padding(top = 4.dp, bottom = 12.dp)) {
                    if (pending.isNotEmpty())
                        AttRow(pending) { a -> pending = pending.filter { it.id != a.id } }
                    if (messages.isEmpty() && !busy && streamText == null) {
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
                        onStop = { stopRequested = true; streamJob?.cancel() },
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
                        val cands = speechServiceCandidates(ctx)
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
                                        cands.forEach { info ->
                                            EngineRow(info.label,
                                                manualOn && info.pkg == manualPkg) {
                                                applyEngine(info); enginePickerOpen = false
                                                setStatus("已切到「${info.label}」，按住说一句试试")
                                            }
                                        }
                                        if (cands.isEmpty())
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
                    // .attach-menu：卡片下方玻璃面板（网页 DOM 顺序就在 .input-card 之后）
                    AnimatedVisibility(visible = attachOpen,
                        enter = fadeIn(tween(140)) + slideInVertically(tween(140)) { it / 3 }) {
                        Row(Modifier.fillMaxWidth().padding(top = 8.dp)
                            .background(glassStrongColor(), RoundedCornerShape(16.dp))
                            .border(BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
                                RoundedCornerShape(16.dp))
                            .padding(10.dp),
                            horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                            val tileInk = MaterialTheme.colorScheme.onSurfaceVariant
                            AttachTile({ IconCamera(tileInk, Modifier.size(17.dp)) }, "相机") { attachOpen = false; camera.launch(null) }
                            AttachTile({ IconImage(tileInk, Modifier.size(17.dp)) }, "图片") { attachOpen = false; picker.launch("image/*") }
                            AttachTile({ IconFile(tileInk, Modifier.size(17.dp)) }, "文件") { attachOpen = false; picker.launch("*/*") }
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
            // 点模型钮弹出全屏「选择模型」页（对标图 3）：标题 + 右上 × + 模型清单，当前项 ✓。
            if (modelMenuOpen) {
                ModelPickerScreen(
                    models = providers.filter { it.usable }.map { it.id to it.name },
                    currentId = currentProvider()?.id,
                    onClose = { modelMenuOpen = false },
                    onPick = { id ->
                        modelMenuOpen = false
                        // switchModel：选择即生效；服务端「我的默认」写失败不反悔
                        Prefs.defaultProviderId = id
                        scope.launch { runCatching { Api.setMyDefaultProvider(id) } }
                        setStatus("")
                    },
                )
            }
            // 断网小卡片：顶部居中悬浮，不占布局不推内容；今日静音过就不出
            if (NetMinder.show()) {
                NetMinderCard(Modifier.align(Alignment.TopCenter).statusBarsPadding()
                    .padding(horizontal = 36.dp, vertical = 10.dp))
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
        Text("开始一段对话", fontSize = 20.sp, fontWeight = FontWeight.SemiBold,
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
                    if (a.kind == "image") IconImage(attInk, Modifier.size(16.dp))
                    else IconFile(attInk, Modifier.size(16.dp))
                }
                Text(a.name, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.widthIn(max = 110.dp))
                Text(fmtSize(a.size), fontSize = 11.sp, color = text3Color())
                Text("×", fontSize = 16.sp, color = text3Color(),
                    modifier = Modifier.clickable { onRemove(a) }.padding(horizontal = 4.dp))
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
    if (isWebLight()) WebTokens.LBgHover else WebTokens.BgHover

/* ---------------- 输入卡（第 12 轮真机反馈：豆包三态输入区） ----------------
 * 收起态一行：[声纹] [发消息或按住说话…] [＋]；
 * 单点展开两行：上行「输入消息…」文本区，下行 [声纹] [模型钮] [＋] [发送/停止]。
 * 声纹钮=语音模式开关（切语音换键盘图标+主色）；模型钮点开全屏「选择模型」页。
 * 占位符跟着键盘走：键盘没起「发消息或按住说话…」（长按即说），起了「输入消息…」。
 * 录音中整屏由 VoiceScreen 接管（见 ChatScreen 根层），输入位只留手势锚点。 */

/* 圆形图标钮：按下缩一点（无涟漪不糊），可选填充/描边色/整枚旋转/彩色光晕。
 * 发送钮就是它的「渐变填充 + 品牌色光晕 + 40dp」特化版，一族按钮同一只手筋。 */
@Composable
private fun GhostCircleButton(onClick: () -> Unit, size: Dp = 38.dp, rotation: Float = 0f,
                              fill: Color? = null, fillBrush: Brush? = null,
                              stroke: Color = MaterialTheme.colorScheme.outline,
                              glow: Color? = null,
                              content: @Composable () -> Unit) {
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val scale by animateFloatAsState(if (pressed) 0.9f else 1f, tween(120), label = "btnScale")
    Box(Modifier.size(size)
        .graphicsLayer { scaleX = scale; scaleY = scale; rotationZ = rotation }
        .then(if (glow != null) Modifier.shadow(9.dp, CircleShape, clip = false,
            ambientColor = glow, spotColor = glow) else Modifier)
        .then(if (fillBrush != null) Modifier.background(fillBrush, CircleShape)
              else Modifier.background(fill ?: Color.Transparent, CircleShape))
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

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun InputCard(input: TextFieldValue, onInput: (TextFieldValue) -> Unit,
                      focusRequester: FocusRequester,
                      onSendKey: () -> Unit,
                      attachOpen: Boolean, onToggleAttach: () -> Unit,
                      status: String, statusErr: Boolean, busy: Boolean,
                      chipModel: String, chipVisible: Boolean,
                      onToggleChip: () -> Unit, onStop: () -> Unit, onSend: () -> Unit,
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
    // 第 12 轮（豆包三态输入区，用户逐图钦定）：收起=单胶囊 [声纹][发消息或按住
    // 说话…][＋]；单点展开（键盘起/有字/生成中）=上行「输入消息…」文本区 +
    // 下行 [声纹][模型钮][＋][发送/停止]。
    val expanded = keyboardUp || input.text.isNotEmpty() || busy
    // 语音闸门在位就不挂文本框（第 10 轮零键盘闪机制原样保留）：没框就没焦点可抢。
    val gateOn = voiceMode || voiceArmed || voiceLive
    Surface(Modifier.fillMaxWidth(), shape = RoundedCornerShape(22.dp),
        color = scheme.surface, border = BorderStroke(1.dp, scheme.outline),
        shadowElevation = 6.dp) {
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
                        if (voiceMode) IconKeyboard(scheme.primary, Modifier.size(19.dp))
                        else IconVoiceWave(scheme.onSurfaceVariant, Modifier.size(19.dp))
                    }
                }
                Box(Modifier.weight(1f).heightIn(min = 42.dp).then(voiceMod),
                    contentAlignment = if (gateOn) Alignment.Center else Alignment.TopStart) {
                    if (gateOn) {
                        if (voiceMode) Row(horizontalArrangement = Arrangement.Center,
                            verticalAlignment = Alignment.CenterVertically) {
                            IconMic(scheme.onSurfaceVariant, Modifier.size(17.dp))
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
                                .focusRequester(focusRequester),
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
                if (!expanded) {
                    // 第 13 轮（用户点名「模型切换放到加号旁边，不然我进去了怎么
                    // 找到」）：收起态也在 ＋ 左边挂模型钮，不展开输入框也能换模型。
                    if (chipVisible) ModelChip(chipModel, onToggleChip)
                    // 收起右钮：＋附件面板开关；open 态整枚旋转 45° 变「×」语义
                    GhostCircleButton(onClick = onToggleAttach,
                        rotation = if (attachOpen) 45f else 0f,
                        stroke = if (attachOpen) scheme.primary else scheme.outline) {
                        IconPlus(if (attachOpen) scheme.primary else scheme.onSurfaceVariant,
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
                        if (voiceMode) IconKeyboard(scheme.primary, Modifier.size(19.dp))
                        else IconVoiceWave(scheme.onSurfaceVariant, Modifier.size(19.dp))
                    }
                    // 模型钮（对标图 2 的 Auto）：点开全屏「选择模型」页（见 ChatScreen 根层）
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
                            IconStop(scheme.onSurface, Modifier.size(13.dp))
                            Text("停止", fontSize = 14.sp, color = scheme.onSurface)
                        }
                    } else {
                        GhostCircleButton(onClick = onToggleAttach,
                            rotation = if (attachOpen) 45f else 0f,
                            stroke = if (attachOpen) scheme.primary else scheme.outline) {
                            IconPlus(if (attachOpen) scheme.primary else scheme.onSurfaceVariant,
                                Modifier.size(19.dp))
                        }
                        if (canSend) {
                            // 发送：40dp 渐变圆钮 + 品牌色光晕；深主题深字、浅主题白字（网页同规则）
                            GhostCircleButton(onClick = onSend, size = 40.dp,
                                fillBrush = aiPrimaryBrush(),
                                glow = if (isWebLight()) Color(0x592451D6) else Color(0x595C8CFF),
                                stroke = Color.Transparent) {
                                IconArrowUp(if (isWebLight()) Color.White
                                            else WebTokens.BtnPrimaryInk, Modifier.size(20.dp))
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

/* 模型切换钮（ⒶAuto）：第 13 轮起收起态也挂——用户点名「不然我进去了怎么找到」，
 * 就贴在 ＋ 左边；展开态下行照旧。点开全屏「选择模型」页（ModelPickerScreen）。 */
@Composable
private fun ModelChip(chipModel: String, onToggleChip: () -> Unit) {
    val scheme = MaterialTheme.colorScheme
    Row(Modifier.background(scheme.surfaceVariant, CircleShape)
        .border(BorderStroke(1.dp, scheme.outline), CircleShape)
        .clickable(onClick = onToggleChip)
        .padding(horizontal = 10.dp, vertical = 5.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(chipModel, fontSize = 13.sp, maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            color = scheme.onSurface,
            modifier = Modifier.widthIn(max = 110.dp))
        IconChevronDown(scheme.onSurface.copy(alpha = 0.7f), Modifier.size(13.dp))
    }
}

/* ---------------- 全屏「选择模型」页（第 12 轮，对标豆包图 3） ----------------
 * 点输入卡下行的模型钮整屏盖上来：标题居中 + 右上 ×，下面模型清单——每行
 * 圆角方块头像 + 名字，当前项主色 + ✓。整页吃掉点击，底下的聊天页摸不到。
 * （豆包页上的折扣横幅/价格倍率/默认·自定义分组是它自己的运营数据，本 App
 * 没有对应字段，不造假数据硬凑，只对齐结构与交互。） */
@Composable
private fun ModelPickerScreen(models: List<Pair<String, String>>, currentId: String?,
                              onClose: () -> Unit, onPick: (String) -> Unit) {
    val scheme = MaterialTheme.colorScheme
    Column(Modifier.fillMaxSize().background(scheme.background).statusBarsPadding()
        .clickable(interactionSource = remember { MutableInteractionSource() },
            indication = null) {}) {
        Box(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp)) {
            Text("选择模型", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
                color = scheme.onSurface, modifier = Modifier.align(Alignment.Center))
            Text("×", fontSize = 26.sp, color = scheme.onSurface,
                modifier = Modifier.align(Alignment.CenterEnd)
                    .clickable(onClick = onClose).padding(8.dp))
        }
        Column(Modifier.fillMaxWidth().weight(1f).verticalScroll(rememberScrollState())) {
            models.forEach { (id, name) ->
                val cur = id == currentId
                Row(Modifier.fillMaxWidth().clickable { onPick(id) }
                    .padding(horizontal = 16.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    Box(Modifier.size(40.dp).background(scheme.surfaceVariant,
                        RoundedCornerShape(12.dp)), contentAlignment = Alignment.Center) {
                        Text(name.trim().take(1).ifEmpty { "?" }.uppercase(),
                            fontSize = 16.sp, fontWeight = FontWeight.SemiBold,
                            color = scheme.onSurfaceVariant)
                    }
                    Text(name, fontSize = 15.sp, maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                        color = if (cur) scheme.primary else scheme.onSurface,
                        fontWeight = if (cur) FontWeight.SemiBold else FontWeight.Normal,
                        modifier = Modifier.weight(1f))
                    if (cur) Text("✓", fontSize = 18.sp, color = scheme.primary)
                }
            }
            if (models.isEmpty())
                Text("没有可用的模型", fontSize = 13.sp, color = text3Color(),
                    modifier = Modifier.padding(horizontal = 20.dp, vertical = 12.dp))
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
                       onRegen: () -> Unit, onFeedback: (Int) -> Unit) {
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
            val shape = if (mine) RoundedCornerShape(topStart = 20.dp, topEnd = 20.dp,
                bottomEnd = 6.dp, bottomStart = 20.dp)
            else RoundedCornerShape(topStart = 6.dp, topEnd = 20.dp,
                bottomEnd = 20.dp, bottomStart = 20.dp)
            val bubbleMod = if (mine) Modifier
                .widthIn(max = 320.dp)
                .background(userBubbleBrush(), shape)
            else Modifier
                .fillMaxWidth()
                .background(scheme.surfaceVariant, shape)
                .border(BorderStroke(1.dp, scheme.outline), shape)
            Column(bubbleMod.padding(
                horizontal = if (mine) 15.dp else 16.dp,
                vertical = if (mine) 10.dp else 12.dp)) {
                if (mine) Text(m.content, fontSize = 15.sp, lineHeight = 24.sp,
                    color = if (isWebLight()) WebTokens.LText else WebTokens.Text)
                else RichText(m.content, scheme.onSurface)
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
                ToolBtn("👍", on = m.feedback == 1) { onFeedback(1) }
                ToolBtn("👎", on = m.feedback == -1) { onFeedback(-1) }
            }
            ToolBtn("删除") { onDelete() }
        }
    }
}

private fun UiMsg.textOrEmpty(): String = content


/* .msg-tools button：无边、透明底、12px text-3、padding 3/8、圆角 7；.on 吃强调色对。
 * enabled=false 是 R1 的"流式中编辑置灰"：字色再淡一档、不吃点击，按钮还在原位。 */
@Composable
private fun ToolBtn(label: String, on: Boolean = false, enabled: Boolean = true,
                    onClick: () -> Unit) {
    val scheme = MaterialTheme.colorScheme
    Box(Modifier.background(
            if (on) scheme.primaryContainer else Color.Transparent,
            RoundedCornerShape(7.dp))
        .then(if (enabled) Modifier.clickable(onClick = onClick) else Modifier)
        .padding(horizontal = 8.dp, vertical = 3.dp)) {
        Text(label, fontSize = 12.sp,
            color = when {
                !enabled -> text3Color().copy(alpha = 0.35f)
                on -> scheme.primary
                else -> text3Color()
            })
    }
}

/* 流式中的那条助手气泡：网页 = .msg.assistant.typing，body 里实时长字 + ▋ 呼吸光标。 */
@Composable
private fun StreamingBubble(text: String) {
    val scheme = MaterialTheme.colorScheme
    val shape = RoundedCornerShape(topStart = 6.dp, topEnd = 20.dp,
        bottomEnd = 20.dp, bottomStart = 20.dp)
    Column(Modifier.fillMaxWidth(),
        horizontalAlignment = Alignment.Start,
        verticalArrangement = Arrangement.spacedBy(5.dp)) {
        Text("助手", fontSize = 11.sp, color = text3Color(),
            modifier = Modifier.padding(horizontal = 4.dp))
        Column(Modifier.fillMaxWidth()
            .background(scheme.surfaceVariant, shape)
            .border(BorderStroke(1.dp, scheme.outline), shape)
            .padding(horizontal = 16.dp, vertical = 12.dp)) {
            if (text.isEmpty()) BlinkCursor()
            else { RichText(text, scheme.onSurface); BlinkCursor() }
        }
    }
}

/* .typing .msg-body::after { content:"▋"; animation: blink 1s steps(2) } —— 两帧硬切。 */
@Composable
private fun BlinkCursor() {
    val tr = rememberInfiniteTransition(label = "cursor")
    val a by tr.animateFloat(1f, 0f,
        infiniteRepeatable(tween(500), RepeatMode.Reverse), label = "blink")
    Text("▋", fontSize = 15.sp, color = MaterialTheme.colorScheme.primary.copy(alpha = a))
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
            Text("📄 ${a.name}", fontSize = 12.sp, color = scheme.onSurfaceVariant)
        }
    }
    if (a.kind != "image") { tag(); return }
    var bmp by remember(a.id) { mutableStateOf(AttCache.mem.get(a.id)) }
    LaunchedEffect(a.id) {
        if (bmp == null) runCatching { Api.uploadBytes(a.id) }.onSuccess { bytes ->
            val opts = android.graphics.BitmapFactory.Options().apply { inSampleSize = 2 }
            val b = android.graphics.BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opts)
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
 * 横向可滚），其余按段落文本。完整 MD 排版留给网页版；原生保证可读、形状对得上。 */
@Composable
internal fun RichText(text: String, color: Color) {
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
    Column {
        parts.forEach { (seg, code) ->
            if (code) {
                val preShape = RoundedCornerShape(14.dp)
                Text(seg.trimEnd('\n'),
                    fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace,
                    fontSize = 13.sp, lineHeight = 21.sp,
                    color = WebTokens.Text,
                    modifier = Modifier.fillMaxWidth()
                        .padding(vertical = 4.dp)
                        .background(WebTokens.CodeBg, preShape)
                        .border(1.dp, WebTokens.Line, preShape)
                        .padding(horizontal = 14.dp, vertical = 12.dp)
                        .horizontalScroll(rememberScrollState()))
            } else if (seg.isNotBlank()) Text(seg, fontSize = 15.sp, lineHeight = 25.sp,
                color = color)
        }
    }
}

/* ---------------- 第 11 轮：输入框恢复系统默认文字工具条 ----------------
 * 第 9 轮的空壳工具条整块退役——用户点名「要和deepseek一模一样」：键盘起着时
 * 长按输入框要在光标旁出复制/粘贴/全选。键盘没起的窗口里输入位是占位条（闸门
 * 分支），单点不挂框、不冒菜单；长按归语音。两头各走各的，不再需要扣壳。 */

