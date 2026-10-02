package xyz.fenever.assistant.nativeapp

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.FlowCollector
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.MediaType.Companion.toMediaType
import xyz.fenever.assistant.core.ExportName
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

class ApiException(val status: Int, message: String) : Exception(message)

@Serializable data class AuthResult(val token: String = "", val user_id: String = "",
                                    val username: String = "", val role: String = "user")
@Serializable data class MeResult(val user_id: String = "", val username: String = "",
                                  val role: String = "user")
@Serializable data class SessionSummary(val session_id: String = "", val title: String = "新对话",
                                        val created_at: String = "", val model: String = "")
@Serializable data class SessionsList(val sessions: List<SessionSummary> = emptyList())
@Serializable data class ChatMessageDto(val role: String, val content: String)
@Serializable data class AttachmentDto(val id: String = "", val name: String = "",
                                       val kind: String = "", val size: Long = 0)
@Serializable data class StoredMessage(val role: String, val content: String,
                                       val message_id: String? = null,
                                       val model: String? = null,
                                       val attachments: List<AttachmentDto>? = null)
@Serializable data class SessionDetail(val session_id: String = "", val title: String = "新对话",
                                       val created_at: String = "", val model: String = "",
                                       val messages: List<StoredMessage> = emptyList())
@Serializable data class ModelInfo(val id: String = "", val name: String = "",
                                   val model: String = "", val supports_vision: Boolean = false,
                                   val usable: Boolean = true, val default: Boolean = false,
                                   val shared: Boolean = true, val reason: String = "",
                                   val max_context_k: Int = 0)
@Serializable data class ModelsResult(val models: List<ModelInfo> = emptyList(),
                                      val default: String? = null)
@Serializable data class UploadInfo(val id: String = "", val name: String = "",
                                    val kind: String = "", val mime: String = "",
                                    val size: Long = 0)
@Serializable data class ChatReply(val reply: String = "", val message_id: String = "")
@Serializable data class ExportTicket(val path: String = "")

/* 日程（v0.23 R3）：与服务端 app/main.py 的 /v1/schedule 两面同形。
   GET 回 {day, items, days}，PUT 回 {day, items, count}——PUT 的响应里**没有** days，
   所以"哪些天有安排"这一份要客户端自己补（见 ui/ScheduleUi.kt 的保存分支）。
   id 只在 GET 那份里有意义（整天 PUT 会重新发号），客户端不拿它做任何判断。 */
@Serializable data class ScheduleItemDto(val id: String = "", val text: String = "",
                                         val at: String = "", val done: Boolean = false)
@Serializable data class ScheduleDayResult(val day: String = "",
                                           val items: List<ScheduleItemDto> = emptyList(),
                                           val days: List<String> = emptyList())
@Serializable data class ScheduleSaveResult(val day: String = "",
                                            val items: List<ScheduleItemDto> = emptyList(),
                                            val count: Int = 0)

/* /v1/chat/stream 的帧集合（v0.25 R3/R3b 定稿，与网页 api.js:149 那份内核逐条对齐）。
   旧的 start/content/done/error 三个事件名一个字都不改（两个客户端都按 data.type 分发，
   改了名字会先打断线上）；新增的是被服务端显式发出的 cancelled/cannot_resume 两帧，
   以及"看不懂的帧不再静默丢"这一型（ChatEvent.Unknown）。

   终帧纪律：cancelled 与 cannot_resume **都不是终止帧**——它们是 done 之前的一帧，
   必须等随后那条 type:"done" 收尾。停/续不上的信息全进 done 的新字段
   （status/stopped_reason/resumable），旧骨架 full_text/message_id/model 不变。 */
sealed class ChatEvent {
    data class Start(val runId: String, val messageId: String, val model: String) : ChatEvent()
    data class Content(val text: String) : ChatEvent()
    // 停止或宽限到点：半句已产出，随后紧跟一条 done 收尾，这里不单独终结。
    data class Cancelled(val fullText: String, val messageId: String, val message: String) : ChatEvent()
    // 这条连接追不上服务端缓冲（这一轮续不上了）：同样等随后的 done 收尾，不单独终结。
    data class CannotResume(val message: String) : ChatEvent()
    data class Done(val fullText: String, val messageId: String, val model: String,
                    val status: String, val stoppedReason: String, val resumable: Boolean) : ChatEvent()
    data class Failed(val message: String) : ChatEvent()
    // 认不出的帧类型：不静默丢——上报一次，同时不打断正常收尾（网页 onUnknown 同语义）。
    data class Unknown(val type: String) : ChatEvent()
}

/* 断线不重跑整轮的可续播参数（v0.25 安卓 · T2.4/T2.8），与网页 api.js 逐字/逐值对齐：
   - STREAM_RESUME_STATUS 是断线重连那一句人话，全场只有一份，界面直接落状态条。
   - STREAM_RESUME_MAX_ATTEMPTS=5、BACKOFF_BASE_MS=500 是重连的次数上限与退避起始，
     与网页一致。**上限不是省钱旋钮**：宽限期长短与花销由服务端的轮次边界钱闸决定，
     和这里试几次无关；上限存在的意义只是"别无限期敲一条已经续不上的门"，到点转取历史。
   所以安卓这一侧刻意不加"为了少花钱提前放弃重连"的逻辑。 */
const val STREAM_RESUME_STATUS = "连接断了，正在接着上次的进度取回…"
const val STREAM_RESUME_MAX_ATTEMPTS = 5
private const val BACKOFF_BASE_MS = 500L

/* 续不上（410 / cannot_resume / 试到上限）的统一信号：去会话历史取回，绝不重发生成。
   对应网页 runResilientStream 抛的那个 err.needHistory=true、retryable=false。 */
class StreamNeedHistoryException(message: String) : Exception(message)

/* 后端接口的原生封装。契约面与 backend/app/web/static/api.js 逐条对齐：
 * - 凭据只走 Authorization: Bearer（authz._header_credential 认可的头）。
 * - 所有路径相对 Prefs.baseUrl，服务换域名只改一处。
 */
object Api {
    private val json = Json { ignoreUnknownKeys = true }
    private val JSON_MT = "application/json".toMediaType()

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.MINUTES)   // 流式回答与工具循环都从这条线路上走
        .writeTimeout(2, TimeUnit.MINUTES)
        .build()

    private fun request(path: String, tokenOverride: String? = null): Request.Builder =
        Request.Builder().url(Prefs.baseUrl.trimEnd('/') + path)
            .apply {
                val t = tokenOverride ?: Prefs.token
                if (t.isNotEmpty()) header("Authorization", "Bearer " + t)
            }

    private fun failDetail(body: String, status: Int): String =
        runCatching {
            json.parseToJsonElement(body).jsonObject["detail"]?.jsonPrimitive?.contentOrNull
        }.getOrNull() ?: "HTTP $status"

    private suspend fun call(path: String, method: String = "GET", body: String? = null,
                             tokenOverride: String? = null): String =
        withContext(Dispatchers.IO) {
            val rb = request(path, tokenOverride)
            when (method) {
                "GET" -> rb.get()
                "DELETE" -> rb.delete(body?.toRequestBody(JSON_MT) ?: "".toRequestBody(null))
                // OkHttp 与 fetch 不同：POST/PUT 缺 body 会直接抛
                // "method 'POST' must have a body"。网页侧这些接口都是空 body 裸 POST
                // （建会话、设默认、测连通），这里补一个零字节体保持同一语义。
                else -> rb.method(method, body?.toRequestBody(JSON_MT) ?: "".toRequestBody(null))
            }
            client.newCall(rb.build()).execute().use { res ->
                val text = res.body?.string() ?: ""
                if (!res.isSuccessful) throw ApiException(res.code, failDetail(text, res.code))
                text
            }
        }

    private suspend inline fun <reified T> callJson(path: String, method: String = "GET",
                                                    body: String? = null): T =
        json.decodeFromString<T>(call(path, method, body))

    private fun obj(pairs: List<Pair<String, JsonElement?>>): String = buildJsonObject {
        pairs.forEach { (k, v) -> if (v != null) put(k, v) }
    }.toString()

    private fun s(v: String?): JsonElement? = v?.let { JsonPrimitive(it) }
    private fun arr(v: List<String>?): JsonElement? =
        v?.let { JsonArray(it.map { x -> JsonPrimitive(x) }) }

    // ---------- 身份 ----------
    suspend fun login(username: String, password: String): AuthResult =
        callJson("/v1/auth/login", "POST",
            obj(listOf("username" to s(username), "password" to s(password))))

    suspend fun register(username: String, password: String, answers: List<String>): AuthResult =
        callJson("/v1/auth/register", "POST",
            obj(listOf("username" to s(username), "password" to s(password),
                       "security_answers" to arr(answers))))

    suspend fun resetPassword(username: String, answers: List<String>, newPassword: String) {
        call("/v1/auth/reset", "POST",
            obj(listOf("username" to s(username), "answers" to arr(answers),
                       "new_password" to s(newPassword))))
    }

    suspend fun me(): MeResult = callJson("/v1/auth/me")

    /** 手工令牌收编：POST /v1/auth/adopt 用头凭据换回真实身份（响应与 /v1/auth/me 同形）。 */
    suspend fun adopt(token: String): MeResult =
        json.decodeFromString<MeResult>(call("/v1/auth/adopt", "POST", null, token))

    /** 撤销指定那一枚令牌（删掉清单里某个人时用它自己的令牌，不当场切换身份）。 */
    suspend fun logout(tokenOverride: String) {
        call("/v1/auth/logout", "POST", "{}", tokenOverride)
    }

    // ---------- 会话 ----------
    suspend fun listSessions(): List<SessionSummary> = callJson<SessionsList>("/v1/sessions").sessions
    suspend fun createSession(model: String? = null): SessionDetail =
        callJson("/v1/sessions" + (if (model.isNullOrEmpty()) ""
            else "?model=" + java.net.URLEncoder.encode(model, "UTF-8")), "POST")
    suspend fun getSession(id: String): SessionDetail =
        callJson("/v1/sessions/" + enc(id))
    suspend fun deleteSession(id: String) { call("/v1/sessions/" + enc(id), "DELETE") }
    suspend fun replaceMessages(id: String, messages: List<StoredMessage>) {
        val arr = JsonArray(messages.map { m ->
            buildJsonObject {
                put("role", JsonPrimitive(m.role))
                put("content", JsonPrimitive(m.content))
                m.message_id?.let { put("message_id", JsonPrimitive(it)) }
                m.model?.let { put("model", JsonPrimitive(it)) }
            }
        })
        call("/v1/sessions/" + enc(id) + "/messages", "PUT",
            buildJsonObject { put("messages", arr) }.toString())
    }
    /* 导出：换一张一次性下载票据。T2.4 起原生不再把票据交给外部浏览器——
       签回的 path 过 ExportName.isTicketPath 门后直接进 DownloadManager 兑换
       （正常链路），或走下面 fetchTicketBytes 自取字节（≤28 拒权的私有目录兜底）。 */
    suspend fun exportTicket(id: String): ExportTicket =
        callJson("/v1/sessions/" + enc(id) + "/export-ticket", "POST")

    /**
     * 匿名兑换票据字节流（v0.23 T2.5 的 DownloadManager 替代路）。
     *
     * 不带 Authorization：与网页 location.href 同一身份——链接本身就是凭据。
     * 顺手带过期 token 反而会被鉴权门误伤，网页侧从来不带，这里逐字对齐。
     * 形状门在发网之前再过一遍：这是服务端 path 字段唯一可能流向网络的第二处，
     * 门禁不收窄成一处有例外。
     */
    suspend fun fetchTicketBytes(path: String): ByteArray = withContext(Dispatchers.IO) {
        if (!ExportName.isTicketPath(path)) throw IllegalStateException("服务端给的票据形状不对")
        client.newCall(Request.Builder()
                .url(Prefs.baseUrl.trimEnd('/') + path).build())
            .execute().use { res ->
                if (!res.isSuccessful) {
                    val text = runCatching { res.body?.string() }.getOrNull() ?: ""
                    throw ApiException(res.code, failDetail(text, res.code))
                }
                res.body?.bytes() ?: ByteArray(0)
            }
    }

    private fun enc(v: String) = java.net.URLEncoder.encode(v, "UTF-8")

    // ---------- 模型清单 ----------
    suspend fun models(): ModelsResult = callJson("/v1/models")

    // ---------- 模型服务（供应商）：与 api.js 同一组路径 ----------
    /* /v1/providers 仅管理员；自助面 /v1/me/providers 仅本人。
       原生侧只消费 /v1/me/providers 的 {shared, mine, default, presets} 与
       管理员的共享增删改（网页 paneProviders 的按钮集合一字不差搬过来）。 */
    suspend fun myProviders(): JsonObject =
        json.parseToJsonElement(call("/v1/me/providers")).jsonObject
    suspend fun providers(): JsonObject =
        json.parseToJsonElement(call("/v1/providers")).jsonObject
    suspend fun addMyProvider(rec: JsonObject) { call("/v1/me/providers", "POST", rec.toString()) }
    suspend fun updateMyProvider(id: String, rec: JsonObject) {
        call("/v1/me/providers/" + enc(id), "PUT", rec.toString())
    }
    suspend fun deleteMyProvider(id: String) { call("/v1/me/providers/" + enc(id), "DELETE") }
    suspend fun setMyDefaultProvider(id: String) {
        call("/v1/me/providers/default", "POST",
            obj(listOf("provider_id" to s(id))))
    }
    suspend fun testMyProvider(id: String): JsonObject =
        json.parseToJsonElement(call("/v1/me/providers/" + enc(id) + "/test", "POST")).jsonObject
    suspend fun setDefaultProvider(id: String) {
        call("/v1/providers/" + enc(id) + "/default", "POST")
    }
    suspend fun testProvider(id: String): JsonObject =
        json.parseToJsonElement(call("/v1/providers/" + enc(id) + "/test", "POST")).jsonObject
    suspend fun testProviderDraft(rec: JsonObject): JsonObject =
        json.parseToJsonElement(call("/v1/providers/test", "POST", rec.toString())).jsonObject

    /** /health 免鉴权；build 字段 = 服务端构建戳（关于页「服务端 …」那一截）。 */
    suspend fun health(): JsonObject =
        json.parseToJsonElement(call("/health")).jsonObject
    // v0.23 T1.8：这里不再有 latestRelease()——检查更新改走自建服务端的
    // GET /v1/update/info（唯一下载入口，见 update/Updater.kt）；客户端直连
    // api.github.com 是 v0.22 那类「GitHub 漂了自家不漂」事故的入口，封死。
    suspend fun testMyProviderDraft(rec: JsonObject): JsonObject =
        json.parseToJsonElement(call("/v1/me/providers/test", "POST", rec.toString())).jsonObject
    suspend fun addProvider(rec: JsonObject) { call("/v1/providers", "POST", rec.toString()) }
    suspend fun updateProvider(id: String, rec: JsonObject) {
        call("/v1/providers/" + enc(id), "PUT", rec.toString())
    }
    suspend fun deleteProvider(id: String) { call("/v1/providers/" + enc(id), "DELETE") }

    // ---------- 附件 ----------
    suspend fun upload(bytes: ByteArray, filename: String, mime: String): UploadInfo =
        withContext(Dispatchers.IO) {
            val part = MultipartBody.Part.createFormData("file", filename,
                bytes.toRequestBody(mime.ifBlank { "application/octet-stream" }.toMediaType()))
            val body = MultipartBody.Builder().setType(MultipartBody.FORM).addPart(part).build()
            val rb = request("/v1/uploads").post(body)
            client.newCall(rb.build()).execute().use { res ->
                val text = res.body?.string() ?: ""
                if (!res.isSuccessful) throw ApiException(res.code, failDetail(text, res.code))
                json.decodeFromString<UploadInfo>(text)
            }
        }

    /* 消息气泡里的图片缩略图：网页端是 fetch(...)/file → blob URL；
       原生侧取同一端点的字节自己解码，取不到就退化成 📄 文件名（同一降级语义）。 */
    suspend fun uploadBytes(id: String): ByteArray = withContext(Dispatchers.IO) {
        val rb = request("/v1/uploads/" + enc(id) + "/file")
        client.newCall(rb.build()).execute().use { res ->
            val text = if (res.isSuccessful) null else res.body?.string().orEmpty()
            if (!res.isSuccessful) throw ApiException(res.code, failDetail(text.orEmpty(), res.code))
            res.body?.bytes() ?: ByteArray(0)
        }
    }

    // ---------- 对话 ----------
    /* token 估算与截断逐行照抄 app.js 的 estimateTokens/truncateWithin：
     * CJK 一字一 token，其余四个字符一 token；每条 +4 包装开销；
     * 最后一条永远带上，往前塞不下就整条收手——不回头丢中间。 */
    fun estimateTokens(text: String): Int {
        var cjk = 0
        for (ch in text) {
            val c = ch.code
            if ((c in 0x2E80..0x9FFF) || (c in 0xF900..0xFAFF) || (c in 0xFF01..0xFF60)) cjk++
        }
        return cjk + kotlin.math.ceil((text.length - cjk) / 4.0).toInt()
    }

    fun truncateWithin(list: List<ChatMessageDto>, budgetTokens: Int): List<ChatMessageDto> {
        val out = ArrayDeque<ChatMessageDto>()
        var used = 0
        for (i in list.lastIndex downTo 0) {
            val cost = estimateTokens(list[i].content) + 4
            if (out.isNotEmpty() && used + cost > budgetTokens) break
            out.addFirst(list[i])
            used += cost
        }
        return out.toList()
    }

    /** 组装出站消息：本机历史（去掉空/瞬时条）按预算截断，角色设定置顶为 system。
     *  budgetK 读数时对模型上限再取一次 min（与网页 outbound() 同一条防线）。 */
    fun outbound(history: List<ChatMessageDto>, persona: String, contextTokensK: Int,
                 modelCapK: Int): List<ChatMessageDto> {
        val reserve = if (persona.isNotEmpty()) estimateTokens(persona) + 4 else 0
        val budgetK = minOf(contextTokensK, modelCapK)
        val msgs = truncateWithin(history.filter { it.content.isNotEmpty() },
            maxOf(500, budgetK * 1000 - reserve))
        return if (persona.isNotEmpty()) listOf(ChatMessageDto("system", persona)) + msgs else msgs
    }

    /* payload 形状与网页一致：{model, provider, messages, attachments, session_id}。
       providerId 传 null 时整个键省略（服务端用它自己的默认）。 */
    fun chatPayload(providerId: String?, model: String?, messages: List<ChatMessageDto>,
                    attachments: List<String>, sessionId: String?): String {
        val msgs = JsonArray(messages.map { m ->
            buildJsonObject {
                put("role", JsonPrimitive(m.role))
                put("content", JsonPrimitive(m.content))
            }
        })
        return obj(listOf(
            "model" to s(model),
            "provider" to s(providerId),
            "messages" to msgs,
            "attachments" to arr(attachments),
            "session_id" to s(sessionId),
        ))
    }

    suspend fun chat(payload: String): ChatReply = callJson("/v1/chat", "POST", payload)

    suspend fun feedback(messageId: String, rating: Int) {
        call("/v1/feedback", "POST",
            obj(listOf("message_id" to s(messageId), "rating" to JsonPrimitive(rating))))
    }

    /* "停止"终于是一个动作，不再只是关页面/断连的副作用：带 run_id 打服务端取消，
       翻标志、尽力当场关上游、下一个付费轮不发生（见 core/stream_runs.py + main.py 的 cancel 端点）。
       归属判定在服务端：不是你的/不存在的 run_id 都是同一句 404，cancel 不是探测信道。
       与网页 api.js 的 cancelStreamRun 同一条路径。 */
    suspend fun cancelStreamRun(runId: String) {
        call("/v1/chat/stream/" + enc(runId) + "/cancel", "POST")
    }

    /* 续播内核的一次游标累加（v0.25 安卓 · T2.4/T2.8）。这几个状态跨多次重开存活：
       run_id 给"停止"当目标、lastEventId 给续播当凭据、resumable 一旦被 cannot_resume 置
       false 就不再尝试续播、terminal 收到 done/error 才止住重连。 */
    private class StreamResumeState {
        var runId = ""
        var lastEventId = ""      // "<run_id>:<seq>"，续播时放进 Last-Event-ID
        var resumable = true
        var terminal = false
    }

    /* 打开这一条流：首次不带游标 = 开这一轮（一次付费）；续播带 Last-Event-ID 头重开
       同一条流端点，服务端据此只补缓冲帧、不叫模型不记账。POST 无法用 EventSource，
       所以续播凭据由这里手动塞进头里（协议上这是 SSE 的合法用法）。
       body 每次照发（与网页 api.js 的 open 逐字同语义）：路由是 POST-only，Builder 不挂
       方法就是 GET——v0.25.0 壳在这里漏过一次 .post()，线上整轮 405。 */
    private fun openStream(payload: String, lastEventId: String?): okhttp3.Response {
        val rb = request("/v1/chat/stream").post(payload.toRequestBody(JSON_MT)).apply {
            if (lastEventId != null) header("Last-Event-ID", lastEventId)
        }
        return client.newCall(rb.build()).execute()
    }

    /* 流式对话（可续播内核，对应网页 api.js:149 的 runResilientStream）。
       服务端帧形状固定：每帧 `id: <run_id>:<seq>\ndata: {json}\n\n`，JSON 内不含裸换行，
       所以按行读、空行封帧即可，无需引入 SSE 库。

       断线不重发整轮，这是 T2.4 的核心：
       - 首次 open 不带游标开这一轮；之后每次重开都带 Last-Event-ID 续播，绝不 POST 回
         /v1/chat 重跑生成（老代码 retryable→Api.chat 那一手就是断线烧钱的病根）。
       - 服务端回 410、或帧里说 cannot_resume（这一轮续不上了），或试到
         STREAM_RESUME_MAX_ATTEMPTS 上限，一律抛 StreamNeedHistoryException 让界面去会话
         历史取回已落库的最终答案——那一轮的钱要么已花、要么被服务端的轮次边界钱闸停在边界，
         结果都在 sessions 里，重发只会再付一次、再落一条一样的助手消息。
       - 断线重连那一句话人话走 onStatus；run_id 一旦取到交 onRun（界面存起来给"停止"用）。 */
    fun streamChat(payload: String,
                   onStatus: ((String) -> Unit)? = null,
                   onRun: ((String) -> Unit)? = null): Flow<ChatEvent> = flow {
        val st = StreamResumeState()
        var attempt = 0           // 已经续播过几次
        resumeLoop@ while (!st.terminal) {
            // open 抛出 = 这一枪根本没到服务端（断网 / DNS / 连不上）。
            val res = try {
                openStream(payload, st.lastEventId.ifEmpty { null })
            } catch (ce: CancellationException) {
                throw ce           // 上层取消（"停止"的本地兜底）不能被当断线吞掉
            } catch (e: Exception) {
                null
            }
            if (res == null) {
                // 从没握手成功 = 还没开这轮，退避后重开同一条流端点是安全的；
                // 已经拿到 run_id = 服务端正跑这一轮，只能带游标续播，不能重发生成。
                if (attempt < STREAM_RESUME_MAX_ATTEMPTS) {
                    attempt += 1
                    if (st.runId.isNotEmpty()) onStatus?.invoke(STREAM_RESUME_STATUS)
                    delay(BACKOFF_BASE_MS shl (attempt - 1))
                    continue@resumeLoop
                }
                throw StreamNeedHistoryException(
                    "试了几次都没能接上这一轮：不用重发原文，结果会写进会话历史，刷新会话取回")
            }

            try {
                if (res.code == 410) {
                    // 410 是明确的"续不上，别重发，去会话里取"；绝不回头重发生成。
                    throw StreamNeedHistoryException(
                        "这一轮接不上了：不用重发原文，结果会写进会话历史，刷新会话取回")
                }
                if (!res.isSuccessful) {
                    // 有 run_id、还能续、且没到上限的意外状态，当作断线退避后带游标重试。
                    if (st.runId.isNotEmpty() && st.resumable &&
                        attempt < STREAM_RESUME_MAX_ATTEMPTS) {
                        attempt += 1
                        onStatus?.invoke(STREAM_RESUME_STATUS)
                        delay(BACKOFF_BASE_MS shl (attempt - 1))
                        continue@resumeLoop
                    }
                    val text = runCatching { res.body?.string() }.getOrNull() ?: ""
                    throw ApiException(res.code, failDetail(text, res.code))
                }

                val src = res.body!!.source()
                var idValue: String? = null
                var dataLine: String? = null
                while (true) {
                    val line = src.readUtf8Line() ?: break
                    if (line.isEmpty()) {
                        emitFrame(this, dataLine, idValue, st, onRun)
                        idValue = null; dataLine = null
                        if (st.terminal) break
                        continue
                    }
                    if (line.startsWith("id:")) idValue = line.substring(3).trim()
                    else if (line.startsWith("data:")) dataLine = line.substring(5).trim()
                }
                if (!st.terminal) emitFrame(this, dataLine, idValue, st, onRun)

                // 连接关闭却没有终帧 = 断线。拿不到 run_id、或服务端已说续不上，就没有可
                // 续播的东西：转取历史，绝不重发生成；否则带游标退避重开续播，到上限同样转取历史。
                if (!st.terminal) {
                    if (st.runId.isEmpty() || !st.resumable) {
                        throw StreamNeedHistoryException(
                            "这一轮接不上了：不用重发原文，结果会写进会话历史，刷新会话取回")
                    }
                    if (attempt >= STREAM_RESUME_MAX_ATTEMPTS) {
                        throw StreamNeedHistoryException(
                            "试了几次都没能接上这一轮：不用重发原文，结果会写进会话历史，刷新会话取回")
                    }
                    attempt += 1
                    onStatus?.invoke(STREAM_RESUME_STATUS)
                    delay(BACKOFF_BASE_MS shl (attempt - 1))
                }
            } finally {
                res.close()
            }
        }
    }.flowOn(Dispatchers.IO)

    /* 单帧分发：先累加游标与 run_id（停止/续播都认它），再按 data.type 落成 ChatEvent。
       旧事件名 start/content/done/error 原样保留；cancelled/cannot_resume 是 done 之前的
       非终帧；认不出的帧不静默丢——落一个 ChatEvent.Unknown 上报，同时不打断正常收尾。 */
    private suspend fun emitFrame(col: FlowCollector<ChatEvent>, data: String?,
                                  idValue: String?, st: StreamResumeState,
                                  onRun: ((String) -> Unit)?) {
        if (idValue != null) st.lastEventId = idValue
        if (data == null) return
        val o = runCatching { json.parseToJsonElement(data).jsonObject }.getOrNull() ?: return
        o["run_id"]?.jsonPrimitive?.contentOrNull?.let { rid ->
            if (rid.isNotEmpty() && st.runId.isEmpty()) { st.runId = rid; onRun?.invoke(rid) }
        }
        if (st.runId.isEmpty() && st.lastEventId.isNotEmpty()) {
            st.runId = st.lastEventId.substringBefore(":")
            if (st.runId.isNotEmpty()) onRun?.invoke(st.runId)
        }
        val str: (String) -> String = { k -> o[k]?.jsonPrimitive?.contentOrNull.orEmpty() }
        when (val type = o["type"]?.jsonPrimitive?.contentOrNull) {
            "start" -> col.emit(ChatEvent.Start(st.runId, str("message_id"), str("model")))
            "content" -> col.emit(ChatEvent.Content(str("text")))
            "cancelled" -> col.emit(ChatEvent.Cancelled(str("full_text"), str("message_id"),
                str("message")))
            "cannot_resume" -> {
                st.resumable = false
                col.emit(ChatEvent.CannotResume(str("message")))
            }
            "done" -> {
                st.terminal = true
                val resumable = o["resumable"]?.jsonPrimitive?.contentOrNull?.toBooleanStrictOrNull()
                    ?: true
                col.emit(ChatEvent.Done(str("full_text"), str("message_id"), str("model"),
                    str("status"), str("stopped_reason"), resumable))
            }
            "error" -> {
                st.terminal = true
                col.emit(ChatEvent.Failed(str("message").ifEmpty { "模型返回错误" }))
            }
            else -> col.emit(ChatEvent.Unknown(type ?: ""))   // 未知帧不静默丢
        }
    }

    // ---------- 记忆 ----------
    suspend fun addMemory(content: String): JsonElement =
        json.parseToJsonElement(call("/v1/memory/add", "POST",
            obj(listOf("content" to s(content), "summarize" to JsonPrimitive(false)))))

    suspend fun listMemory(limit: Int = 50): JsonElement =
        json.parseToJsonElement(call("/v1/memory/list?limit=$limit"))

    suspend fun searchMemory(query: String, topK: Int = 10): JsonElement =
        callJson<JsonElement>("/v1/memory/search", "POST",
            obj(listOf("query" to s(query), "top_k" to JsonPrimitive(topK.coerceIn(1, 20)))))

    suspend fun deleteMemory(ids: List<String>) {
        call("/v1/memory/delete", "DELETE",
            obj(listOf("memory_ids" to arr(ids))))
    }

    // ---------- 日程（v0.23 R3 · T2.7）：与 api.js 的 getSchedule/putSchedule 同两条路 ----------
    /* 归属人只从凭据里来：这里不拼任何 user_id，服务端按 token 认人（与
       backend/app/core/schedule.py 的"归属人只从凭据里来"同一个口径）。
       day 传空 = 问服务端"今天"，回来的 day 字段就是这一页的锚点（R3-AC-3）。 */
    suspend fun getSchedule(day: String? = null): ScheduleDayResult =
        callJson("/v1/schedule" + (if (day.isNullOrEmpty()) ""
            else "?day=" + enc(day)))

    /** 整天替换（幂等）：items 只带 {text, at, done}，id 由服务端重新发。 */
    suspend fun putSchedule(day: String, items: List<ScheduleItemDto>): ScheduleSaveResult {
        val arr = JsonArray(items.map { it2 ->
            buildJsonObject {
                put("text", JsonPrimitive(it2.text))
                put("at", JsonPrimitive(it2.at))
                put("done", JsonPrimitive(it2.done))
            }
        })
        return json.decodeFromString(call("/v1/schedule", "PUT",
            buildJsonObject {
                put("day", JsonPrimitive(day))
                put("items", arr)
            }.toString()))
    }
}
