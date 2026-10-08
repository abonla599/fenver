package xyz.fenever.assistant.nativeapp

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/* 续命凭据（device_secret）的专用加密区，v0.34.0 判据 4/11。
 *
 * 为什么单独一个文件而不是往 Prefs 里加一个 var：那个 `ai_native_prefs` 是明文
 * SharedPreferences（MODE_PRIVATE 只保证"别的 app 读不到"，不保证文件本身不落地明文），
 * 而这把凭据的作用恰恰是"半年后不用重新输口令"——它一旦被从设备镜像/备份里读出来，
 * 拿到的就是免口令开新会话的能力。PRD 在这一点上只留了一条路：加密区，拉不到依赖就
 * 退回平台 Keystore 手写封装，**绝不许**把 secret 塞回明文 Prefs。
 *
 * 建不出加密区时的降级方向也是有意选边：宁可"这台设备没有静默重登（到点要重输一次
 * 口令）"，也不"凭据躺在明文里但功能看起来更顺"。ready 为假时登录照旧成功，只是
 * 不落这把凭据：SessionRenew.reauthOnce 读到空串直接返回 false，401 原样冒给界面落登录页。
 *
 * 会话令牌本身仍留在 Prefs 的明文条目里（本版改动面收口的决定，PRD 第三节明写
 * "令牌是否一并搬由开工时的改动面定"）：它 30 天滑动、180 天封顶，且 revoke/logout
 * 都能立即作废；device_secret 是另一类——400 天没动过才失效，而且它换的是"免口令开
 * 一段新会话"，两者不同级。
 */
object SecretStore {
    private const val FILE = "ai_native_secrets"
    private const val PREFIX = "device_secret:"

    @Volatile private var store: SharedPreferences? = null
    @Volatile var ready: Boolean = false
        private set

    fun init(ctx: Context) {
        val app = ctx.applicationContext
        store = runCatching {
            val masterKey = MasterKey.Builder(app)
                .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                .build()
            EncryptedSharedPreferences.create(
                app, FILE, masterKey,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
            ) as SharedPreferences
        }.getOrNull()
        ready = store != null
        if (!ready) {
            // 不抛：这台机器上少了静默重登这一个能力，其余功能照常。留一句话给日志。
            android.util.Log.w("SecretStore", "加密偏好区不可用，本机不落续命凭据")
        }
    }

    /** 只有加密区真的建起来了才写；调用方不需要知道这件事（判据 4 不依赖它成功）。 */
    fun save(userId: String, secret: String) {
        val s = store ?: return
        if (userId.isEmpty() || secret.isEmpty()) return
        runCatching { s.edit().putString(PREFIX + userId, secret).apply() }
    }

    fun read(userId: String): String =
        if (userId.isEmpty()) "" else runCatching {
            store?.getString(PREFIX + userId, "")
        }.getOrNull().orEmpty()

    fun drop(userId: String) {
        val s = store ?: return
        runCatching { s.edit().remove(PREFIX + userId).apply() }
    }

    fun clear() {
        runCatching { store?.edit()?.clear()?.apply() }
    }
}
