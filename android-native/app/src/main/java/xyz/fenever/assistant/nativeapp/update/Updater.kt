package xyz.fenever.assistant.nativeapp.update

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.provider.Settings
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import xyz.fenever.assistant.core.ApkDownloader
import xyz.fenever.assistant.core.ReleasePlan
import xyz.fenever.assistant.nativeapp.BuildConfig
import xyz.fenever.assistant.nativeapp.Prefs
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/* 「静默问一次 → 有新版才露按钮 → 一点就下载 → 校验 → 系统安装页」的编排（v0.23 T1.7 + T1.8）。

 判断与字节纪律**全部住在共享核心类里**（ReleasePlan / ApkDownloader / ApkDigest / MiniJson
 这一份就是出包的那份——旧 WebView 壳已于 2026-10-08 退役，core/ 不再有两份拷贝；
 这七个类由 tools/shell_jvm_tests.py 的 JVM 台架钉着，台架数的正是这个模块，
 见 backend/tests/test_release_probe.py 的台架同源锁）——这里只做 Android 侧的三件事：
 ① 问与取的地址从 Prefs.baseUrl 现拼（服务换域名只改设置，链路上没有第二处真相）；
 ② 把状态灌进 Compose（全部经主线程 post，IO 线程绝不直接写快照）；
 ③ 下载完成后把包交给系统安装页，没授权时给出能走通的路。

 AC-4 的迁移：/v1/update/info 取不到 GitHub 时回 5xx 带 detail；这里把任何非 200
 都归成 UNUSABLE 的一句理由，**绝不伪装"已是最新版"**。但这句话只说给主动点按钮的人听：
 静默那一路问不到就当没问过（见 offer 的注释）——失败态不许常驻在「版本」那一行上。

 口径（2026-11，用户原话「有新版才有按钮，没新版就只有版本号」）：
 ① 没有「检查更新」这个动作了。设置页的「版本」行一显示就问一次（本进程 30 分钟内
    只问一次，见 REFRESH_MS），问到了新版本才在那一行里多出一颗「发现新版本 vX.Y」；
 ② 没有新版：那一行只有一个号，不弹「已是最新版本」的顶部提示（用户点名不要这噪音）；
 ③ 有新版：点那颗按钮 = 下载 + 校验 + 起系统安装页一步到位，不再分"先查再更新"两步；
 ④ 问不到：静默那一路一个字都不说，只在人主动点了按钮之后才把理由交回去（attemptDownload）。 */

private const val INFO_CONNECT_MS = 8_000
private const val INFO_READ_MS = 12_000
/** 第 15 轮（用户真机：「下载新版本一直失败超时，收 0 KB」）：/site/android.apk
 *  是服务端替包先跑一趟 GitHub 再回吐（实测首字节 8 秒上下），拿信息接口的
 *  12 秒读超时卡 7 MB 大包必死—— APK 单列一档：连接 15s、读 60s。 */
private const val APK_CONNECT_MS = 15_000
private const val APK_READ_MS = 60_000
/** 一条 release JSON 十几 KB 封顶；读满这个数还不停手就说明回来的不是它。 */
private const val INFO_MAX_BYTES = 256 * 1024
/** 与后端 releases.APK_MAX_BYTES 同一个数：壳这边同样不信"对方说多大就多大"。 */
private const val APK_MAX_BYTES = 16L * 1024 * 1024
/** 静默检查的缓存寿命：本进程问过一次，半小时内重进设置页不再问。
 *  没有这道闸，「每次开设置都问一遍」既烧服务端替所有人挡的配额，又会在慢网络下
 *  让「发现新版本」那颗按钮闪一下才出来。半小时后重问是给"刚发的新版"留一个不漏的窗口。 */
private const val REFRESH_MS = 30L * 60L * 1000L

object Updater {
    private val main = Handler(Looper.getMainLooper())
    private val io = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** 上一份决定。startDownload 读它（校验值、版本号都在里面）；null = 这个进程还没问到。 */
    var decision by mutableStateOf<ReleasePlan.Decision?>(null)
        private set
    /** 非 null = 通道里有一个比已装版新的版本，值是它的版本号；「版本」行只在这种时候
     *  多挂一颗「发现新版本 vX.Y」。没有新版、没问过、问不到——一律 null，那一行就只有
     *  已装的那一个号。这是全页唯一一处"可安装的是哪一版"，不许再开第二个来源。 */
    var available by mutableStateOf<String?>(null)
        private set
    /** 下载进度：downloading 时 percent/doneBytes/totalBytes 供进度对话框读。 */
    var downloading by mutableStateOf(false)
        private set
    var percent by mutableStateOf(0)
        private set
    var doneBytes by mutableStateOf(0L)
        private set
    var totalBytes by mutableStateOf(-1L)
        private set
    /** 非 null = 弹出「装不了：vX.Y → 去设置」的授权引导对话框（T1.7 的授权引导）。 */
    var installGuideVersion by mutableStateOf<String?>(null)
        private set

    @Volatile private var activeConn: HttpURLConnection? = null
    /** 静默检查的在途标记。刻意不是 Compose 状态：它不许驱动任何一帧——「版本」值槽
     *  一挂出来就必须已经是终态（只有一个号，或版本号 + 发现新版本），没有第三种样子。 */
    @Volatile private var asking = false
    /** 上一条检查（静默的或补问的）的发起时刻，elapsedRealtime 口径，只管 REFRESH_MS 那道闸。 */
    @Volatile private var lastAskAt = 0L

    private fun infoUrl() = Prefs.baseUrl.trimEnd('/') + "/v1/update/info"

    /** 字节出口与旧壳/官网按钮同一个：baseUrl + ReleasePlan.SELF_APK_PATH（同源纪律）。 */
    private fun apkUrl() = Prefs.baseUrl.trimEnd('/') + ReleasePlan.SELF_APK_PATH

    /** 静默问一次：只有 AVAILABLE 才把版本号放上 available，其余一律什么也不说。
     *
     *  唯一调用点是设置页「版本」行的一次性副作用，不是按钮——"有没有更新"不再是一个
     *  用户要按的动作。REFRESH_MS 内不重问：反复进出设置不许每次都打一趟网络。
     *  问不到不抛、不报错、不常驻：失败态只在人主动点了按钮之后才由 attemptDownload 说出。 */
    fun offer() {
        val now = SystemClock.elapsedRealtime()
        if (asking || now - lastAskAt < REFRESH_MS) return
        asking = true
        lastAskAt = now
        io.launch {
            val d = try {
                ReleasePlan.decide(BuildConfig.VERSION_NAME, fetchInfo(), Prefs.baseUrl, true)
            } catch (e: Exception) {
                // 连不上/超时/被网关改了：说清是哪一种，绝不当成"已经是最新版"。
                // 静默那一路不显示这句话；谁主动点了按钮，谁在 attemptDownload 里读到它。
                ReleasePlan.unusable("连不上更新服务（" + e.javaClass.simpleName + "）")
            }
            main.post {
                asking = false
                decision = d
                available = if (d.kind == ReleasePlan.Kind.AVAILABLE) d.version else null
            }
        }
    }

    /** 「发现新版本 vX.Y」按下去了：一步走完"确认这一版还在 + 开始下载"。
     *
     *  正常情况下按钮露出来就是因为上一拍已经问到了，这里直接进下载——不许再等一次
     *  网络，否则点下去半天没反应。只有手里真没有那份 AVAILABLE 决定时（进程刚起、
     *  缓存被清、上一次问失败）才补问一次；补问的失败理由这时才交给 onDone。
     *  这就是「检查失败只在用户主动触发时提示」的那一半：静默那一路从不说话。 */
    fun attemptDownload(app: Context, onDone: (String?) -> Unit) {
        val asked = decision
        if (asked != null && asked.kind == ReleasePlan.Kind.AVAILABLE) {
            startDownload(app, onDone)       // 失败理由（含"没带校验值"）由 startDownload 按人话报回
            return
        }
        if (asking) return                    // 同一时刻只许有一条检查在途
        asking = true
        lastAskAt = SystemClock.elapsedRealtime()
        io.launch {
            val d = try {
                ReleasePlan.decide(BuildConfig.VERSION_NAME, fetchInfo(), Prefs.baseUrl, true)
            } catch (e: Exception) {
                ReleasePlan.unusable("连不上更新服务（" + e.javaClass.simpleName + "）")
            }
            main.post {
                asking = false
                decision = d
                available = if (d.kind == ReleasePlan.Kind.AVAILABLE) d.version else null
                when (d.kind) {
                    ReleasePlan.Kind.AVAILABLE -> startDownload(app, onDone)
                    ReleasePlan.Kind.UP_TO_DATE ->
                        onDone("已经是最新版本了，不用再装")
                    else -> onDone(d.reason ?: "没问到这一版能不能装")
                }
            }
        }
    }

    /** 只有非 200 一种事实要处理：后端把"问不到 GitHub"回成 502 带理由，这里不解读理由。 */
    private fun fetchInfo(): String {
        val conn = (URL(infoUrl()).openConnection() as HttpURLConnection).apply {
            connectTimeout = INFO_CONNECT_MS
            readTimeout = INFO_READ_MS
            instanceFollowRedirects = true
            setRequestProperty("User-Agent", "ai-assistant-native/" + BuildConfig.VERSION_NAME)
        }
        try {
            if (conn.responseCode != HttpURLConnection.HTTP_OK) {
                throw IOException("HTTP " + conn.responseCode)
            }
            conn.inputStream.use { input ->
                val buf = ByteArrayOutputStream(8 * 1024)
                val chunk = ByteArray(8 * 1024)
                var total = 0
                while (true) {
                    val read = input.read(chunk)
                    if (read <= 0) break
                    total += read
                    if (total > INFO_MAX_BYTES) throw IOException("返回体积异常")
                    buf.write(chunk, 0, read)
                }
                return buf.toString("UTF-8")
            }
        } finally {
            conn.disconnect()
        }
    }

    /**
     * 开始下载。onDone(失败理由 | null) 在主线程回调——成功那一刻已经顺带拉起安装页。
     * 没带可信校验值不下第一字节（R5：残包事故的原样就是"先下下来再说"）。
     */
    fun startDownload(app: Context, onDone: (String?) -> Unit) {
        if (downloading) return
        val d = decision
        if (d == null || d.kind != ReleasePlan.Kind.AVAILABLE) {
            // 走到这里只可能是"手里真没有那一版"（进程刚起/缓存刚被清）：说一句人话，
            // 不许像从前那样静默 return —— 一颗点了没反应的按钮比没有按钮更糟。
            onDone("还没问到这一版能不能装，稍后再点一次")
            return
        }
        if (d.sha256 == null) {
            onDone("这一版的发布信息里没带安装包的校验值，拒绝下载来源不可核对的包")
            return
        }
        val dir = app.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
        if (dir == null) {
            onDone("这台设备现在给不出下载目录（存储未就绪？）")
            return
        }
        val name = ReleasePlan.assetName(d.version)
        val target = File(dir, name)
        val temp = File(dir, "$name.part")
        if (target.isFile && !target.delete()) {
            onDone("上一次的文件删不掉，先重启手机再试一次")
            return
        }
        if (temp.isFile) temp.delete()   // 半截的 .part 只是浪费磁盘，删不掉也继续
        downloading = true
        percent = 0; doneBytes = 0; totalBytes = -1
        io.launch {
            val result = ApkDownloader.download(apkUrl(), temp, target, APK_MAX_BYTES,
                d.sha256,
                ApkDownloader.Connector { url -> openApkConnection(url) },
                ApkDownloader.Progress { p, done, total ->
                    // IO 线程只许经这里回主线程写快照，一次/百分点，量级无害
                    main.post { percent = p; doneBytes = done; totalBytes = total }
                })
            main.post {
                downloading = false
                if (result.error == null) {
                    launchInstaller(app, name, d.version, onDone)
                } else {
                    onDone(friendlyDownloadError(result.error))
                }
            }
        }
    }

    /** 下载/校验失败一律返回一句人话：这条路现在是「点一次就下载」，中途断掉时
     *  屏幕上只有这一句解释，不许出现「下载失败：null」或「服务回 HTTP 302」这种机器话。
     *  3xx 意味着服务端把包弄丢了（单飞槽被占或 GitHub 那头抽风）：那不是用户点错了。 */
    private fun friendlyDownloadError(e: String): String = when {
        e.contains("HTTP 3") -> "更新服务这会儿取不到安装包（GitHub 慢或正忙），等一两分钟再点一次"
        e.contains("HTTP 5") -> "更新服务这边正忙，等一两分钟再点一次"
        e.contains("校验值") -> "下回来的包和发布的那一份对不上，已经丢掉重来一次试试"
        e.contains("中断") || e.contains("超时") || e.contains("Socket") || e.contains("timed out") ->
            "网络断了，包没下全（已清掉），换个网络再点一次"
        else -> e
    }

    /** 取消 = 掐断那条连接；ApkDownloader 的失败路径会把 .part 就地清掉，不留残渣。 */
    fun cancelDownload() {
        runCatching { activeConn?.disconnect() }
    }

    private fun openApkConnection(url: String): ApkDownloader.Opened {
        val conn = (URL(url).openConnection() as HttpURLConnection).apply {
            connectTimeout = APK_CONNECT_MS
            readTimeout = APK_READ_MS
            // 第 15 轮第二道坑：服务端抢不到单飞槽/上游抽风时把 /site/android.apk
            // 302 回 GitHub 发布页—— HttpURLConnection 默认跟着跳，手机直连
            // github.com 在国内网络就是挂死在 0 KB。不跟跳，让它停在 3xx，
            // ApkDownloader 会原样回「服务回 HTTP 302」，由下面换成人话。
            instanceFollowRedirects = false
            setRequestProperty("User-Agent", "ai-assistant-native/" + BuildConfig.VERSION_NAME)
        }
        activeConn = conn
        val status = conn.responseCode
        return object : ApkDownloader.Opened {
            override fun status() = status
            override fun contentLength() = conn.contentLength.toLong()
            override fun body() = conn.inputStream
            override fun closeQuietly() {
                activeConn = null
                conn.disconnect()
            }
        }
    }

    // ---------- 交给系统安装页（T1.7） ----------

    /**
     * content:// 走自己手写的 ApkFileProvider：Android 7 起跨进程给 file:// 直接抛
     * FileUriExposedException。读权限只随这一条 Intent 临时授予，装完即随进程回收。
     * 抛异常 ≈ 这台手机还没允许本应用"安装未知应用"——那是引导的入口，不是失败的终点。
     */
    private fun launchInstaller(app: Context, fileName: String, version: String,
                                onDone: (String?) -> Unit) {
        if (!File(app.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS), fileName).isFile) {
            onDone("下载完成了，但找不到那个文件")
            return
        }
        val install = Intent(Intent.ACTION_VIEW).apply {
            setDataAndType(ApkFileProvider.uriForFile(app, fileName), ApkFileProvider.APK_MIME)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        try {
            app.startActivity(install)
            onDone(null)
        } catch (e: Exception) {
            onDone(null)                  // 包已就位——先把"要授权"说清楚，别混成下载失败
            installGuideVersion = version
        }
    }

    /** 「去设置」：优先直达本应用的"安装未知应用"开关页，个别 ROM 没那页就退到应用详情。 */
    fun openInstallPermissionSettings(app: Context) {
        installGuideVersion = null
        val pkg = Uri.parse("package:" + app.packageName)
        val pages = listOf(
            Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, pkg),
            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, pkg),
        )
        for (page in pages) {
            try {
                page.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                app.startActivity(page)
                return
            } catch (ignored: Exception) {
                // 下一档；两档都没有时下面兜底
            }
        }
        installGuideVersion = "NO-PAGE"   // UI 读到它只能显示"系统没给出那个页面"
    }

    fun dismissInstallGuide() {
        installGuideVersion = null
    }
}
