package xyz.fenever.assistant.nativeapp

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/* v0.34.0 续期阶梯的原生半边（判据 1、4、9 与 PRD 第四节「安卓原生 App」那一行）。
 *
 * 判据顺序与 Web 完全一致，而且**顺序本身就是要钉的东西**：
 *   没有头条 → 不猜；剩余 > 7 天 → 不动手；有流在跑 → 推迟；已在续 → 不重复；
 *   冷却窗内 → 不动；过完这些才发这一次 renew。
 * 原生这里比 Web 更要紧：没有 Cookie 通道，令牌就是 Prefs.token。续期一成功老值只剩
 * 60 秒命，而一次长答的读取循环正握着那一枚——判据 9 在 `streams > 0` 这一句上。
 *
 * 「失败绝不重试超过 1 次」是 PRD 点名的（限速器不吃自杀式重试）：冷却窗就是这一句的
 * 实现，它同时兜住"一次 401 之后循环重打"那条退路。
 *
 * 存量账号（升级到 v0.34 之前登录的）本机没有 secret：`reauthOnce` 读到空串直接返回
 * false，401 原样抛给界面落登录页——这条降级路径与发布说明那句"没重新登录过的设备
 * 拿不到这个能力"是同一件事。
 */
object SessionRenew {
    const val THRESHOLD_SECONDS = 7L * 24 * 3600      // 与服务端 RENEW_ALLOWED_BELOW_SECONDS 同数
    const val COOLDOWN_MS = 6L * 3600 * 1000          // 一天四次的天花板，失败也算一次

    private val json = Json { ignoreUnknownKeys = true }

    @Volatile private var remainingSeconds = -1L      // -1 = 服务端还没告诉过我们
    @Volatile private var lastRenewAt = 0L
    @Volatile private var lastReauthAt = 0L
    private val busy = AtomicBoolean(false)            // renew 与 reauth 共用一把：同一时刻只敲一次
    private val streams = AtomicInteger(0)

    fun noteExpiry(raw: String?, status: Int) {
        if (status < 200 || status > 299) return       // 非 2xx 的头条说的不是"还剩多久"
        val v = raw?.toLongOrNull() ?: return
        if (v >= 0) remainingSeconds = v
    }

    fun streamOpen() { streams.incrementAndGet() }
    fun streamClose() { if (streams.decrementAndGet() < 0) streams.set(0) }

    /** 纯判据：把"什么时候不许动手"写成一条能被源码级盯住的顺序。 */
    fun shouldRenew(now: Long): Boolean {
        if (remainingSeconds < 0) return false
        if (remainingSeconds > THRESHOLD_SECONDS) return false
        if (streams.get() > 0) return false            // 判据 9：这一轮流正在往外冒字
        if (busy.get()) return false
        if (now - lastRenewAt < COOLDOWN_MS) return false
        return true
    }

    fun renewIfDue() {
        val now = System.currentTimeMillis()
        if (!shouldRenew(now)) return
        if (!busy.compareAndSet(false, true)) return
        lastRenewAt = now
        try {
            // 网络层炸了（断网 / 超时 / DNS）也算「失败什么都不做」：一次后台续期
            // 绝不能把异常冒给它的触发点——那儿刚刚好好端出一屏数据，不该被它变红。
            val (status, text, expires) = runCatching {
                Api.ladderCall("/v1/auth/renew", "{}", Prefs.token)
            }.getOrNull() ?: return
            if (status < 200 || status > 299) return   // 判据原文：失败**什么都不做**
            val fresh = field(text, "token")
            if (fresh.isEmpty()) return                // 没拿到新明文就不动现有令牌
            Prefs.token = fresh
            noteExpiry(expires, status)
        } finally {
            busy.set(false)
        }
    }

    /** 阶梯第三层：会话到绝对上限之后的第一个 401，用本机加密区里那把换一段新会话。
     *  返回 true 才允许调用方把原来那一枪重打**一次**。 */
    fun reauthOnce(): Boolean {
        val userId = Prefs.currentEntry()?.userId ?: return false
        val secret = SecretStore.read(userId)
        if (secret.isEmpty()) return false             // 存量账号 / 加密区不可用：落登录页
        val now = System.currentTimeMillis()
        if (now - lastReauthAt < COOLDOWN_MS) return false
        if (!busy.compareAndSet(false, true)) return false
        lastReauthAt = now
        try {
            val body = buildJsonObject { put("device_secret", JsonPrimitive(secret)) }.toString()
            // 连不上服务端 ≠ 凭据失效：这一枪原样把 401 交给界面落登录页就够了，
            // 不许在这里把「网络故障」误判成「这把 secret 死了」而把它删掉。
            val (status, text, expires) = runCatching {
                Api.ladderCall("/v1/auth/reauth", body, null)
            }.getOrNull() ?: return false
            if (status == 410) SecretStore.drop(userId)   // 服务端已判这把死，本机跟着删
            if (status < 200 || status > 299) return false
            val fresh = field(text, "token")
            if (fresh.isEmpty()) return false
            Prefs.token = fresh
            // 单次使用：老那把在服务端已经作废，本机必须立刻落下新的，否则下次 401 无凭可用
            SecretStore.save(userId, field(text, "device_secret"))
            noteExpiry(expires, status)
            return true
        } finally {
            busy.set(false)
        }
    }

    private fun field(text: String, name: String): String = runCatching {
        json.parseToJsonElement(text).jsonObject[name]?.jsonPrimitive?.contentOrNull
    }.getOrNull().orEmpty()
}
