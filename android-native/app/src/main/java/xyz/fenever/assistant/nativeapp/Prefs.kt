package xyz.fenever.assistant.nativeapp

import android.content.Context
import android.content.SharedPreferences
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/* 本机配置与凭据存储。结构照抄网页端 localStorage 的那套事实来源：
 * - identities/currentId：这台机器上认识谁（多账户切换），上限 5 个人；
 *   条目字段与 app.js 的 addIdentity 逐一对应（userId/username/role/
 *   lastSessionId/providerId/addedAt）。网页方案 C 不存凭据明文（走 Cookie），
 *   原生没有 Cookie 通道，Bearer 令牌与续命凭据都落加密区（SecretStore）；
 *   条目里的 token 字段只是旧版明文数据的迁移来源，启动后立即清空。
 * - persona:{sessionId}：角色设定按会话存，全局不共享。
 * - contextTokensK：上下文预算（k token），默认 8，同网页。
 * - reminders：提醒存在设备侧（服务端没有这件事），所以原生也是本机排期。
 */
@Serializable
data class Identity(val userId: String, val username: String = "", val role: String = "user",
                    val token: String = "", val lastSessionId: String = "",
                    val providerId: String = "", val addedAt: String = "")

/* 思考偏好的每格值（Task 10）：enabled 的默认值必须是 false。服务端从 Task 12B 起**有**
 * thinking_level 这一格（可发的档位仍只有登记表里核实过的那几条，今天一条都没有，所以
 * 带了档位也发不出去）；客户端接线在 Task 13。默认开就等于界面在承诺一件发不出去的事。
 * 两格都带默认值：旧版本机 JSON 里缺任何一格都解得开，不炸启动。 */
@Serializable
private data class ThinkingPref(val enabled: Boolean = false, val level: String = "")

object Prefs {
    private const val PREF_NAME = "ai_native_prefs"
    private const val IDENTITY_CAP = 5
    private lateinit var sp: SharedPreferences
    private val json = Json { ignoreUnknownKeys = true }
    // 加密区建不起来时的退路：令牌只活在这个进程里，进程一死就要重新登录。
    // 宁可多输一次口令，也不把令牌落回明文文件。
    private val tokenMemory = java.util.concurrent.ConcurrentHashMap<String, String>()

    fun init(ctx: Context) {
        sp = ctx.applicationContext.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)
        // 续命凭据与会话令牌都进 Keystore 加密区（v0.39 M-8）
        SecretStore.init(ctx)
        migrateLegacyTokens()
        // 旧全局预算键只搬给「已选定默认模型」的那一位；没选定就留到 loadModelsNow 后再搬。
        migrateContextBudget()
        // 冷启动第一帧就带上外观，不闪一下深色
        xyz.fenever.assistant.nativeapp.theme.ThemeMode.value = themeMode
    }

    /** 旧版把会话令牌明文写在 identities JSON 里：搬进加密区（或进程内存）后立刻抹掉。 */
    private fun migrateLegacyTokens() {
        val list = readIdentities()
        if (list.none { it.token.isNotEmpty() }) return
        val cleaned = list.map {
            if (it.token.isEmpty()) it else {
                stashToken(it.userId, it.token)
                it.copy(token = "")
            }
        }
        saveIdentities(cleaned)
    }

    var baseUrl: String
        get() = sp.getString("base_url", null) ?: BuildConfig.DEFAULT_BASE_URL
        set(value) = sp.edit().putString("base_url", value.trim().trimEnd('/')).apply()

    /* 外观三态：system / dark / light。网页设置行只有深浅两态；原生多了
     * "system"（渲染侧 Theme.kt 按 isSystemInDarkTheme 落地）。默认仍是深色。 */
    var themeMode: String
        get() = sp.getString("theme_mode", "dark") ?: "dark"
        set(value) {
            sp.edit().putString("theme_mode", value).apply()
            xyz.fenever.assistant.nativeapp.theme.ThemeMode.value = value
        }

    /* 触感反馈总开关（借鉴 WorkBuddy 的同名设置行）：默认开，关掉后
     * 设置页所有行点击、分段选择与开关都不再震动。 */
    var hapticsEnabled: Boolean
        get() = sp.getBoolean("haptics_enabled", true)
        set(value) = sp.edit().putBoolean("haptics_enabled", value).apply()

    /* 上下文预算（k token），聊天发送前按它截断历史。
     * v0.39.x 驾驶舱起降级为「迁移来源」：新读取一律走 contextBudgetK(providerId)，
     * 本键只在 migrateContextBudget() 里被搬进按模型存的 map，之后不再写。 */
    var contextTokensK: Int
        get() = sp.getInt("context_tokens_k", 8)
        set(value) = sp.edit().putInt("context_tokens_k", value.coerceIn(2, 10000)).apply()

    /* v0.39.x 模型驾驶舱：上下文预算按「模型（provider id）」存一份 K 值（网页端
     * 「用户+provider」map 的本机对位——原生一台机一个登录态概念，键只到 provider）。
     * 值一律是 K token；对模型档案 max_context_k 的 min 由读数侧再夹一次
     *（Api.outbound 那层防线保留），这里只管存储与兜底夹逼。 */
    private fun budgetMapKey(): String = "context_budget_map"

    private fun budgetMap(): MutableMap<String, Int> =
        runCatching {
            json.decodeFromString<Map<String, Int>>(sp.getString(budgetMapKey(), "{}") ?: "{}")
        }.getOrNull()?.toMutableMap() ?: mutableMapOf()

    fun contextBudgetK(providerId: String): Int {
        val cap = 10000 // 硬兜底上限：真实上限由调用方与 ModelInfo.max_context_k 取 min
        return budgetMap()[providerId]?.coerceIn(1, cap) ?: contextTokensK.coerceIn(1, cap)
    }

    /** map 里**原始**那一份（没夹过）：上限调小时"存着的预算越过了新上限"这件事
     *  只有拿它比才看得出来——`contextBudgetK` 读数时已经夹过，拿夹过的数比上限，
     *  那条分支恒不成立，本机那份越界值就一直躺着（spec §3.2 禁的正是这个）。
     *  没记过返回 0，调用方按"没什么可夹"处理。与网页 contextBudgetWantedK 同一判据。 */
    fun contextBudgetWantedK(providerId: String): Int = budgetMap()[providerId] ?: 0

    fun setContextBudget(providerId: String, k: Int) {
        if (providerId.isEmpty()) return
        val map = budgetMap()
        map[providerId] = k.coerceIn(1, 10000)
        sp.edit().putString(budgetMapKey(), json.encodeToString(map)).apply()
    }

    /** 旧全局滑杆键只搬一次：map 已存在就直接收工；还没选过模型就先留着旧键，
     *  下次（loadModelsNow 定出默认 provider 后）再搬——不丢用户调过的预算。 */
    fun migrateContextBudget() {
        if (sp.contains(budgetMapKey())) return
        val pid = defaultProviderId
        if (pid.isEmpty()) return
        setContextBudget(pid, contextTokensK)
        sp.edit().remove("context_tokens_k").apply()
    }

    /* v0.39.x 驾驶舱 Task 10：思考偏好（这台设备自己的开关与档位）。
     * 与预算同一族写法——一个 JSON map 键 + 复合内键，内键形状照 usage 表的 "sid:pid"
     * 改成 "userId:providerId"：换身份读的是自己那一格，上一个人的开关不会跟着
     * provider id 回来（网页端 fv.thinking.<userId>.v1 的本机对位）。
     * 三条纪律：① 只在本机，不发服务端；② enabled 默认 **false**（不给全站悄悄改
     * 计费行为）；③ 档位归一只有这一处——本机存的值不在候选里就落「服务端默认档」，
     * 默认档也不在候选里就落第一档，界面上不存在"有档位但默认档为空"那种存不下的状态。 */
    private fun thinkingMapKey(): String = "thinking_prefs_map"

    private fun thinkingSlot(providerId: String): String {
        val uid = currentEntry()?.userId ?: ""
        return "$uid:$providerId"
    }

    private fun thinkingMap(): MutableMap<String, ThinkingPref> =
        runCatching {
            json.decodeFromString<Map<String, ThinkingPref>>(
                sp.getString(thinkingMapKey(), "{}") ?: "{}")
        }.getOrNull()?.toMutableMap() ?: mutableMapOf()

    fun thinkingEnabled(providerId: String): Boolean =
        thinkingMap()[thinkingSlot(providerId)]?.enabled ?: false

    /** 当前会用的档位：候选为空给空串（"这个模型没有档位"是事实，不编一个假的）。 */
    fun thinkingLevel(providerId: String, levels: List<String>,
                      thinkingDefault: String): String {
        if (levels.isEmpty()) return ""
        val saved = thinkingMap()[thinkingSlot(providerId)]?.level.orEmpty()
        if (levels.contains(saved)) return saved
        return if (levels.contains(thinkingDefault)) thinkingDefault else levels.first()
    }

    fun setThinkingEnabled(providerId: String, enabled: Boolean) {
        if (providerId.isEmpty()) return
        val map = thinkingMap()
        val slot = thinkingSlot(providerId)
        map[slot] = (map[slot] ?: ThinkingPref()).copy(enabled = enabled)
        sp.edit().putString(thinkingMapKey(), json.encodeToString(map)).apply()
    }

    fun setThinkingLevel(providerId: String, level: String) {
        if (providerId.isEmpty()) return
        val map = thinkingMap()
        val slot = thinkingSlot(providerId)
        map[slot] = (map[slot] ?: ThinkingPref()).copy(level = level)
        sp.edit().putString(thinkingMapKey(), json.encodeToString(map)).apply()
    }

    /* 断网提醒「今日不再显示」：存静音当天日期（yyyy-MM-dd），隔天自动失效。 */
    var offlineMuteDate: String
        get() = sp.getString("offline_mute_date", "") ?: ""
        set(value) = sp.edit().putString("offline_mute_date", value).apply()

    /* 手动选定的语音识别引擎（第 6 轮真机反馈：默认引擎连不上它自己的服务器，
     * 错误码 11，开麦克风没用——得能换引擎并记住选择）。空串 = 跟随系统默认。 */
    var voiceEnginePkg: String
        get() = sp.getString("voice_engine_pkg", "") ?: ""
        set(value) = sp.edit().putString("voice_engine_pkg", value).apply()
    var voiceEngineCls: String
        get() = sp.getString("voice_engine_cls", "") ?: ""
        set(value) = sp.edit().putString("voice_engine_cls", value).apply()

    // ---------- 身份清单（多账户） ----------
    fun readIdentities(): List<Identity> {
        val raw = sp.getString("identities", null) ?: return emptyList()
        return runCatching { json.decodeFromString<List<Identity>>(raw) }
            .getOrNull()?.filter { it.userId.isNotEmpty() } ?: emptyList()
    }

    private fun saveIdentities(list: List<Identity>) {
        sp.edit().putString("identities", json.encodeToString(list)).apply()
    }

    var currentId: String
        get() = sp.getString("current_id", "").orEmpty()
        set(value) = sp.edit().putString("current_id", value).apply()

    /** 当前那一条：currentId 缺失或指向已不在清单里的人 → 取最新那条并写回
     *（与 app.js 的 currentEntry 同一条兜底规则）。 */
    fun currentEntry(): Identity? {
        val list = readIdentities()
        if (list.isEmpty()) return null
        list.find { it.userId == currentId }?.let { return it }
        val hit = list.sortedByDescending { it.addedAt }.first()
        currentId = hit.userId
        return hit
    }

    private fun patchCurrent(fields: Identity.() -> Identity) {
        val hit = currentEntry() ?: return
        saveIdentities(readIdentities().map { if (it.userId == hit.userId) it.fields() else it })
    }

    /** 登录/注册成功后记一个人：同 userId 旧条目先去掉再追加，超上限挤掉最旧的。
     * 返回被挤掉那几位的令牌（调用方负责 best-effort 调服务端 logout 让它们失效；
     * 本机只忘不撤会留下看不见的活凭据——M-8 第 3 条）。
     * minSdk 24 没有 java.time，addedAt 用定长毫秒串，排序语义与 ISO 串等价。 */
    fun addIdentity(res: AuthResult): List<String> {
        val list = readIdentities().filter { it.userId != res.user_id }.toMutableList()
        list.add(Identity(userId = res.user_id, username = res.username, role = res.role,
            addedAt = "%013d".format(System.currentTimeMillis())))
        saveIdentities(list)
        currentId = res.user_id
        /* v0.39 M-8：会话令牌与续命凭据都只进加密区，清单里不再留明文。
         * adopt 来的令牌也走同一条路；两个 SecretStore 方法在加密区不可用时
         * 静默降级（令牌只活进程内存）。 */
        stashToken(res.user_id, res.token)
        SecretStore.save(res.user_id, res.device_secret)
        val evicted = mutableListOf<String>()
        while (readIdentities().size > IDENTITY_CAP) {
            val oldest = readIdentities().minByOrNull { it.addedAt }!!
            val dead = tokenFor(oldest.userId)
            saveIdentities(readIdentities().filter { it.userId != oldest.userId })
            dropToken(oldest.userId)
            SecretStore.drop(oldest.userId)
            if (dead.isNotEmpty()) evicted += dead
        }
        return evicted
    }

    fun dropIdentity(userId: String) {
        saveIdentities(readIdentities().filter { it.userId != userId })
        // 本机忘掉这个人 = 服务端那把续命凭据在本机也不再留着（调用方会先/随后
        // 用 tokenFor 拿到的令牌去调 logout；这里只负责本机侧不再保留）。
        dropToken(userId)
        SecretStore.drop(userId)
        if (currentId == userId) currentId = ""
    }

    fun switchTo(userId: String): Boolean {
        val hit = readIdentities().find { it.userId == userId } ?: return false
        currentId = userId
        return true
    }

    fun touchIdentity(username: String, role: String) =
        patchCurrent { copy(username = username, role = role) }

    // ---------- 当前身份的派生读写（都落在清单条目上，不再单独存键） ----------
    /* 令牌本体不在条目里：SecretStore 加密区优先，加密区不可用时退到进程内存。
     * 旧版留下的明文 token 字段由 migrateLegacyTokens() 搬走并清空。 */
    var token: String
        get() {
            val id = currentEntry()?.userId ?: return ""
            return tokenFor(id)
        }
        set(value) {
            val id = currentEntry()?.userId ?: return
            stashToken(id, value)
        }

    /** 指定身份那一枚令牌：删别人/退出登录时要在丢掉条目之前拿到它去调 logout。 */
    fun tokenFor(userId: String): String {
        if (userId.isEmpty()) return ""
        val encrypted = SecretStore.readToken(userId)
        if (encrypted.isNotEmpty()) return encrypted
        return tokenMemory[userId].orEmpty()
    }

    private fun stashToken(userId: String, token: String) {
        if (userId.isEmpty()) return
        if (token.isEmpty()) { dropToken(userId); return }
        if (SecretStore.ready) {
            SecretStore.saveToken(userId, token)
            // saveToken 内部吞异常（加密区坏了不该炸登录）：回读一次确认真的落住了，
            // 没落住就退回进程内存，别让一次静默失败变成"重启后凭空掉登录"。
            if (SecretStore.readToken(userId) == token) tokenMemory.remove(userId)
            else tokenMemory[userId] = token
        } else {
            tokenMemory[userId] = token
        }
    }

    private fun dropToken(userId: String) {
        SecretStore.dropToken(userId)
        tokenMemory.remove(userId)
    }

    var username: String
        get() = currentEntry()?.username ?: ""
        set(value) = patchCurrent { copy(username = value) }

    var role: String
        get() = currentEntry()?.role ?: "user"
        set(value) = patchCurrent { copy(role = value) }

    var defaultProviderId: String
        get() = currentEntry()?.providerId ?: ""
        set(value) = patchCurrent { copy(providerId = value) }

    var lastSessionId: String
        get() = currentEntry()?.lastSessionId ?: ""
        set(value) = patchCurrent { copy(lastSessionId = value) }

    val isAuthed: Boolean get() = token.isNotEmpty() && currentEntry() != null

    fun setPersona(sessionId: String, text: String) {
        val e = sp.edit()
        if (text.isEmpty()) e.remove("persona:$sessionId") else e.putString("persona:$sessionId", text)
        e.apply()
    }

    fun persona(sessionId: String): String = sp.getString("persona:$sessionId", "").orEmpty()

    /* 换服务器等于换一个身份世界：所有身份在新地址上都不作数，清凭据回登录页。 */
    fun setBaseUrlAndReauth(url: String) {
        baseUrl = url
        clearAuth()
    }

    /** 退出当前身份：调用方先向服务端撤销令牌，再走这里忘掉本机条目。 */
    fun forgetCurrent() {
        val hit = currentEntry() ?: return
        dropIdentity(hit.userId)
    }

    fun clearAuth() {
        saveIdentities(emptyList())
        currentId = ""
        // 清单整本清空 = 加密区也不能留任何一把（换服务器时旧地址那把在新地址上毫无意义）
        tokenMemory.clear()
        SecretStore.clear()
    }
}
