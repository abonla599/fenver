package xyz.fenever.assistant.nativeapp.ui

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.Rect
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.view.View
import android.view.ViewTreeObserver
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.PointerEventPass
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.abs
import kotlin.math.PI
import kotlin.math.sin

/* 按住说话：空输入框上长按启动系统语音识别，松手把识别文字直接发出，按住时
 * 上滑进取消区。识别走 SpeechRecognizer（免弹窗的边说边出字），设备上没有识别
 * 服务时退化为状态条提示。第 12 轮起录音界面是豆包式全屏接管（VoiceScreen）。 */

/** 一次识别会话的状态容器。回调都在主线程（SpeechRecognizer 的约定）。 */
@Stable
class VoiceRecorder(context: Context) {
    private val appCtx = context.applicationContext
    val available: Boolean = SpeechRecognizer.isRecognitionAvailable(appCtx)

    var listening by mutableStateOf(false); private set
    var heard by mutableStateOf(""); private set
    var level by mutableFloatStateOf(0.15f); private set
    var onFinal: ((String) -> Unit)? = null
    var onError: ((String) -> Unit)? = null
    // 权限明明给了、识别服务还是回"权限不足"（部分 ROM 的识别服务查的是它自己的
    // 录音授权）：这时候喊用户"去系统设置"是误导。交给接线方说人话兜底——
    // 2026-09-28 第 3 轮真机反馈后不再自动拉系统语音界面（它会弹服务自己的
    // 白框错误「似乎出错了呢(2)」），只给一句日常提示。
    var onMicServiceDenied: (() -> Unit)? = null
    // 引擎层面的死亡错误（第 6 轮真机反馈）：默认「系统语音引擎」一长按就回
    // 11=SERVER_TERMINATED、系统语音界面弹白框「似乎出错了呢(2)」——那是引擎
    // 连不上**它自己的**服务器，跟麦克风权限无关，再开权限也没用。把错码交给
    // 接线方：它决定自动轮换下一个引擎，还是弹「换个语音引擎」让用户挑。
    var onEngineDead: ((Int) -> Unit)? = null
    // 手动指定的识别引擎（下次新建识别器时生效）；null = 跟随系统默认。
    var engine: ComponentName? = null

    private var keepSend = true
    private var holding = false        // 手指是否还按着：错误自动重试只发生在按住期间
    private var permRetried = false    // 权限错自动重建重试一次，防"明明开了权限还说没权限"
    // 第 13 轮（用户点名「还没松手就发送了」）：手指还按着时引擎提前回了终包/
    // 提前报错——绝不当场发送、绝不当场退场，把结果暂存这里，UI 原地留住，
    // 等松手的 finish() 一次性收尾。松手发送是铁律。
    private var pending: String? = null
    private var rec: SpeechRecognizer? = null
    private val mainHandler = android.os.Handler(android.os.Looper.getMainLooper())
    // 第 13 轮第二道保险：万一抬手事件整个没送进手势循环（ROM 抽风吞事件），
    // finally 兜底也够不着——会话硬顶闹钟到点无条件收尾，UI 绝不挂死过夜。
    // 第 15 轮起这是唯一还挂在录音器上的闹钟：松手收尾改成当场完成，
    // 「等迟到包」的 settle 闹钟整个退役（用户点名要松手立刻响应）。
    private var watchdog: Runnable? = null

    private fun clearWatchdog() {
        watchdog?.let { mainHandler.removeCallbacks(it) }
        watchdog = null
    }

    @Suppress("DEPRECATION")
    fun start() {
        if (listening || !available) return
        listening = true; holding = true; heard = ""; level = 0.15f; keepSend = true
        pending = null
        val r = try {
            rec ?: (engine?.let { e ->
                // 用户（或自动轮换）指定的引擎优先；它绑不上就退回系统默认，
                // 别让一次选错引擎把语音整个打死（第 6 轮）。
                runCatching { SpeechRecognizer.createSpeechRecognizer(appCtx, e) }.getOrNull()
            } ?: SpeechRecognizer.createSpeechRecognizer(appCtx))
                .also { it.setRecognitionListener(listener) }
                .also { rec = it }
        } catch (_: Exception) { null }
        if (r == null) {
            listening = false; holding = false
            onError?.invoke("这台设备没有可用的语音识别服务"); return
        }
        try {
            r.startListening(Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                    RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                .putExtra(RecognizerIntent.EXTRA_LANGUAGE, "zh-CN")
                .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
                // 不少识别服务（Google/OEM）拿 calling_package 去查调用方的录音权限，
                // 不填就直接回 ERROR_INSUFFICIENT_PERMISSIONS——哪怕权限早就开了。
                // 该 extra 在新 API 上标了废弃但服务侧仍在读，必须留着。
                .putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, appCtx.packageName))
        } catch (_: Exception) {
            listening = false; holding = false
            onError?.invoke("语音识别没能启动，再按住试一次")
        }
        clearWatchdog()
        val wd = Runnable {
            watchdog = null
            if (!listening) return@Runnable
            listening = false; holding = false
            val t = pending?.takeIf { it.isNotBlank() } ?: heard
            pending = null
            if (t.isNotBlank()) { heard = ""; onFinal?.invoke(t) }
            else { heard = ""; onError?.invoke("这轮语音自己收场了，再按住试一次") }
        }
        watchdog = wd
        mainHandler.postDelayed(wd, MAX_SESSION_MS)
    }

    /** 松手。第 15 轮改判（用户点名「松开手指之后软件不能立即响应，希望立刻响
     *  应」）：send=true 也当场收——不再挂最多 1.6 秒的迟到包闹钟。松手这一刻
     *  手里有什么就发什么（pending 优先：按住期间引擎已回的终包；其次最后半截
     *  识别字），有字立刻发、没字立刻提示，识别器就地 cancel；之后引擎再补回的
     *  迟到包由 onResults 的 !listening 守卫作废。取消（send=false）照旧秒收。 */
    fun finish(send: Boolean) {
        if (!listening) return
        holding = false
        permRetried = false   // 一次按压只自愈一次；下次长按重新计数
        clearWatchdog()       // 松手了：硬顶闹钟退役，收尾走下面这条一步到位的路
        if (send) {
            keepSend = true
            val t = (pending?.takeIf { it.isNotBlank() } ?: heard).orEmpty()
            pending = null
            listening = false; heard = ""
            runCatching { rec?.cancel() }
            if (t.isNotBlank()) onFinal?.invoke(t)
            else onError?.invoke("没听到说话，这次先不收")
        } else {
            pending = null
            keepSend = false; listening = false; heard = ""; runCatching { rec?.cancel() }
        }
    }

    fun destroy() {
        clearWatchdog()
        pending = null
        runCatching { rec?.cancel() }
        runCatching { rec?.destroy() }
        rec = null; listening = false; holding = false
    }

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) = Unit
        override fun onBeginningOfSpeech() = Unit
        override fun onEndOfSpeech() = Unit
        override fun onRmsChanged(rmsdB: Float) {
            level = ((rmsdB + 2f) / 12f).coerceIn(0f, 1f)
        }
        override fun onBufferReceived(buffer: ByteArray?) = Unit
        override fun onEvent(eventType: Int, params: Bundle?) = Unit
        override fun onPartialResults(partialResults: Bundle?) {
            firstText(partialResults)?.let { heard = it }
        }
        override fun onResults(results: Bundle?) {
            // 第 15 轮：松手已当场收尾，引擎后补的迟到终包一律作废——不然它会
            // 绕过 finish 再触发一次 onFinal，同一句话发两遍。
            if (!listening) return
            val t = firstText(results).orEmpty()
            heard = t
            if (holding) {
                // 引擎提前判停（静音/最长时限）而手指还按着：不发送、不退场，
                // 结果存进 pending，UI 原地留住等松手的 finish() 收尾（第 13 轮）。
                pending = t
                return
            }
            listening = false
            if (keepSend && t.isNotBlank()) onFinal?.invoke(t)
        }
        override fun onError(error: Int) {
            if (!listening) return
            // 权限错且手指还按着、还没重试过：旧识别器可能攥着授权前的拒权缓存，
            // 拆掉重建再启一次。用户下次长按就是新实例，"开了权限还说没权限"就地自愈。
            if (error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS &&
                keepSend && holding && !permRetried) {
                permRetried = true
                runCatching { rec?.destroy() }
                rec = null; listening = false
                start()
                return
            }
            // 第 13 轮：手指还按着时引擎提前报错（没听清/静音超时这类）——同样
            // 不提前退场：暂存已识别到的半截字，UI 留住等松手，finish 里统一收尾。
            // 引擎死亡码和权限错不放行：那两条要立刻拆 binder、弹修复入口，退场是对的。
            if (holding && keepSend && error !in ENGINE_DEAD_CODES &&
                error != SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS) {
                pending = heard
                return
            }
            listening = false; holding = false; heard = ""
            // 取消时部分设备也会回调一个错码，别拿它吓用户
            if (!keepSend) return
            // 自愈过一次还是权限错，而我们自己查权限是"已允许"：锅在识别服务
            //（它查的是它自己的录音授权，用户在本 App 的设置里怎么找都找不到那一格，
            // 2026-09-28 真机反馈）。别再指挥用户去设置里瞎找，喊接线方给一句
            // 日常提示（第 3 轮反馈后不再自动拉系统语音界面：它弹服务自己的错误白框）。
            // 顺手把识别器拆掉：授权态可能被旧 binder 缓存着，拆了下次长按才重绑新实例，
            // 用户去系统里给语音服务开完麦克风，回来就能直接用（第 4 轮反馈）。
            if (error == SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS && micGranted()) {
                runCatching { rec?.destroy() }
                rec = null
                onMicServiceDenied?.invoke()
                return
            }
            // 引擎连不上自家服务器/服务自杀/语言包缺失（4/5/8/10/11）：这是引擎本身
            // 在这台机器上用不了，再长按一百次也是同一个死法（第 6 轮真机反馈的
            // 「语音识别没成功（11）」）。拆掉旧 binder，喊接线方换引擎。
            if (error in ENGINE_DEAD_CODES) {
                runCatching { rec?.destroy() }
                rec = null
                onEngineDead?.invoke(error)
                return
            }
            onError?.invoke(errText(error))
        }
    }

    private fun micGranted(): Boolean =
        appCtx.checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED

    private fun firstText(b: Bundle?): String? =
        b?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()

    /* 文案口径（2026-09-28 真机反馈：说法要像日常说话，别像系统日志）：
     * 一句说清"怎么了"，一句说清"下一步做什么"，不出现"生效/ insufficient"这种机器词。 */
    private fun errText(code: Int): String = when (code) {
        SpeechRecognizer.ERROR_AUDIO -> "没收到声音，凑近麦克风再说一次"
        SpeechRecognizer.ERROR_CLIENT -> "语音中断了，再按住试一次"
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS ->
            "还没允许用麦克风，点「允许」之后就能按住说话"
        SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT ->
            "网络不太稳，没识别出来，再试一次"
        SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "没听清，再说一遍"
        SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "上一条还在识别，稍等一下"
        SpeechRecognizer.ERROR_SERVER -> "识别服务开了个小差，稍后再试"
        else -> "语音识别没成功（$code）"
    }

    companion object {
        /** "引擎本身不行"类错误码（第 6 轮）：网络到不了引擎服务器、引擎进程自杀、
         * 语言包缺失——这些不是权限问题，换引擎才有用。
         * 10/11 用字面量：ERROR_LANGUAGE_UNAVAILABLE / ERROR_SERVER_TERMINATED
         * 不在 compileSdk 34 的 android.jar 常量表里（CI 实测 Unresolved），
         * 但服务回包就是这两个码——真机 11（SERVER_TERMINATED）是本轮病根本体。 */
        val ENGINE_DEAD_CODES = setOf(
            SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT,
            SpeechRecognizer.ERROR_SERVER,
            10,  // ERROR_LANGUAGE_UNAVAILABLE
            11)  // ERROR_SERVER_TERMINATED

        /** 一次录音会话的硬顶时长（第 13 轮）：抬手链路整个失联（事件被 ROM
         * 吞掉、循环挂死）时，到点无条件收尾——语音 UI 宁可误收也不能挂死。 */
        const val MAX_SESSION_MS = 60_000L
    }
}

@Composable
fun rememberVoiceRecorder(): VoiceRecorder {
    val ctx = LocalContext.current
    val rec = remember(ctx) { VoiceRecorder(ctx) }
    DisposableEffect(rec) { onDispose { rec.destroy() } }
    return rec
}

/** 系统语音识别服务是哪个 App：部分 ROM 上「我们权限明明给了、服务还说不许录音」
 * 的根因是那个语音服务 App 自己的麦克风授权被关了——它不在本 App 的设置页里，
 * 只能把用户送到那个应用的系统详情页去开（2026-09-28 第 4 轮真机反馈）。
 * 第 5 轮再修：resolveService 在部分 ROM 上返回 null（入口直接消失），兜底改成
 * 枚举两个识别动作的全部候选服务；且入口必须报出目标 App 的名字——用户会拿
 * 本 App（就叫「Fenver」）的设置页来对答案，不说名字分不清开的是谁的权限。 */
fun speechServicePackage(context: Context): String? =
    runCatching {
        @Suppress("DEPRECATION")
        context.packageManager.resolveService(
            Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH), 0)?.serviceInfo?.packageName
    }.getOrNull() ?: speechServiceCandidates(context).firstOrNull()?.pkg

/** 一个提供语音识别的服务 App：包名 + 用户看得懂的应用名 + 服务类名
 *（第 6 轮：换引擎要用 ComponentName(pkg, cls) 直绑识别服务，光有包名不够）。 */
data class SpeechServiceInfo(val pkg: String, val label: String, val cls: String = "")

/** 枚举设备上所有语音识别服务候选（两条标准动作都查，按包去重）。 */
@Suppress("DEPRECATION")
fun speechServiceCandidates(context: Context): List<SpeechServiceInfo> = runCatching {
    val pm = context.packageManager
    val seen = LinkedHashMap<String, SpeechServiceInfo>()
    for (act in listOf(RecognizerIntent.ACTION_RECOGNIZE_SPEECH,
                       "android.speech.RecognitionService")) {
        pm.queryIntentServices(Intent(act), 0)?.forEach { ri ->
            val si = ri.serviceInfo ?: return@forEach
            val pkg = si.packageName
            if (pkg !in seen) {
                val label = runCatching {
                    pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()
                }.getOrDefault(pkg)
                seen[pkg] = SpeechServiceInfo(pkg, label, si.name.orEmpty())
            }
        }
    }
    seen.values.toList()
}.getOrDefault(emptyList())

/**
 * 手动版「系统语音输入」备用通路（第 5 轮真机反馈：用户开了本 App 的麦克风仍
 * 录不了，需要一个不依赖我们绑定识别器的路径）。走 RecognizerIntent 活动，
 * 在识别服务自己的进程里起界面——**只在用户点按钮时拉起，绝不自动弹**
 * （第 3 轮的白框错误就是自动拉起吓到人）。成了回文字，砸了回一句人话。
 */
@Composable
fun rememberSystemVoiceFallback(onText: (String) -> Unit, onFail: (String) -> Unit): () -> Unit {
    val ctx = LocalContext.current
    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult()) { res ->
        val txt = res.data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)?.firstOrNull()
        if (res.resultCode == android.app.Activity.RESULT_OK && !txt.isNullOrBlank()) onText(txt)
        // 第 6 轮改口径：这条路和长按用的是**同一个**识别引擎，它挂了这里也挂——
        // 再喊"去开麦克风"就是误导（真机：开完权限照样白框「似乎出错了呢(2)」）。
        else onFail("系统语音输入也没成——它和长按用的是同一个引擎，点「换个语音引擎」挑一个再试")
    }
    return {
        runCatching {
            launcher.launch(Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                    RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                .putExtra(RecognizerIntent.EXTRA_LANGUAGE, "zh-CN")
                .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1))
        }.onFailure { onFail("这台手机连系统语音界面都起不来，只能去给语音服务开麦克风") }
    }
}

/** 键盘弹没弹，实测窗口可见区。第 9 轮换公式：本 App 是 adjustResize，键盘把整
 *  个窗口顶扁，root.height 和可见区 frame 高一起缩——旧式"根视图高减可见区高"
 *  在这永远≈0，检测根本没扳机（真机「键盘语音同屏」的一条根因）。换成屏幕坐标差：
 *  可见区底边 与 根视图屏幕底边 的距离。键盘弹出时这个差就是键盘高（resize 下
 *  窗底被顶到键盘上沿，pan 不缩窗时 frame 底边被抬到键盘上沿，两种模式都成立）。
 *  差超 1/5 屏判键盘在。
 *  第 15 轮再补一路（用户真机：「键盘收起来之后输入框要立刻换回胶囊样式」）：
 *  坐标差那路在个别 ROM 上会被贴边抹平，而 adjustResize 下**窗口绝对高度缩水**
 *  是物理事实、抹不掉——键盘起窗被顶扁、键盘收窗弹回，跟焦点无关（第 14 轮
 *  拿焦点当键盘代理，收键盘不收焦点，信号永远卡在"有键盘"）。取历史最高窗高
 *  maxH，现高比它矮 1/5 判键盘在；宽度一变（旋转/分屏）旧基线作废，重立。
 *  第 16 轮方向改判（真机 v0.23.14：键盘全开着，占位符照写「发消息或按住
 *  说话…」、长按照进录音——两路几何信号全哑结案）：这台 ROM 窗口压根不缩——
 *  第 14 轮「窗底被顶到键盘上沿」把证据读反了，真实物理是**全屏窗 + IME 悬
 *  浮**：DecorView 恒占满屏（maxH−h≡0），可见区 frame 底边被 IME 抬到键盘上
 *  沿。于是第 9 轮那路减法算出来是负的（frame.bottom − 窗底 = −键盘高），永远
 *  够不着阈值。翻正：窗底 − frame.bottom 才是键盘高，超 1/5 屏判键盘在；
 *  顶扁式机型上该差≈0 不误报，由 maxH 那路兜底。两路取或，悬浮/顶扁通吃。
 *  第 16 轮再补两件套（同一真机两路全哑的另一条嫌疑：监听器本身死了）：
 *  ① 第三路自校准信号——frame 底边自己的历史最低点 maxFB（键盘收着的时刻
 *  采得），现值比它高 1/5 屏判键盘在。这路不依赖窗口缩不缩，只要键盘起落时
 *  可见区底边会动（第 14 轮截图已实证会动）就扳得响；
 *  ② 僵尸观察者自愈——ViewTreeObserver 会随窗口树重建整个作废，旧实例上挂
 *  的监听器从此一声不吭，几何信号全哑的现场和它完全吻合。每次回调先验
 *  isAlive，死了就抓当前新 OIV 重挂；另挂 OnAttachStateChangeListener，视图
 *  重新入窗时同样补挂一遍；
 *  ③ 轮询兜底——悬浮式 IME 的机型上键盘起落可能压根不触发布局，监听器一次
 *  都不回调，公式再对也是死的。LaunchedEffect 每 250ms 主动量一次，信号最迟
 *  四分之一秒必刷新，从此不存在"卡死在旧值"这条路。 */
@Composable
fun rememberKeyboardVisible(): Boolean {
    val view = LocalView.current
    var visible by remember { mutableStateOf(false) }
    val polled = remember { arrayOfNulls<() -> Unit>(1) }
    DisposableEffect(view) {
        val root = view.rootView
        var oiv = root.viewTreeObserver
        val loc = IntArray(2)
        var maxH = 0
        var lastW = -1
        var maxFB = 0
        var lastFBW = -1
        lateinit var listener: ViewTreeObserver.OnGlobalLayoutListener
        fun measure() {
            if (!oiv.isAlive) {
                oiv = root.viewTreeObserver
                oiv.addOnGlobalLayoutListener(listener)
            }
            val frame = Rect()
            root.getWindowVisibleDisplayFrame(frame)
            root.getLocationOnScreen(loc)
            val h = root.height
            val w = root.width
            if (w != lastW) { lastW = w; maxH = h }
            else if (h > maxH) maxH = h
            if (w != lastFBW) { lastFBW = w; maxFB = frame.bottom }
            else if (frame.bottom > maxFB) maxFB = frame.bottom
            visible = (loc[1] + root.height) - frame.bottom > root.height / 5 ||
                (maxH - h > maxH / 5) ||
                (maxFB - frame.bottom > maxFB / 5)
        }
        listener = ViewTreeObserver.OnGlobalLayoutListener { measure() }
        val rehook = object : View.OnAttachStateChangeListener {
            override fun onViewAttachedToWindow(v: View) {
                if (!oiv.isAlive) {
                    oiv = root.viewTreeObserver
                    oiv.addOnGlobalLayoutListener(listener)
                }
            }
            override fun onViewDetachedFromWindow(v: View) = Unit
        }
        root.addOnAttachStateChangeListener(rehook)
        oiv.addOnGlobalLayoutListener(listener)
        polled[0] = { measure() }
        onDispose {
            polled[0] = null
            root.removeOnAttachStateChangeListener(rehook)
            if (oiv.isAlive) oiv.removeOnGlobalLayoutListener(listener)
        }
    }
    LaunchedEffect(view) {
        while (true) {
            delay(250)
            polled[0]?.invoke()
        }
    }
    return visible
}

/**
 * 长按手势修饰符：按住 [holdMs] 毫秒不滑动即判定「按住说话」，
 * 上滑超过 [cancelUp] 进取消区，抬手回调结果。
 * [enabled] 每次按下时现算（输入框非空/生成中就不接管触摸，行为照常编辑）。
 *
 * 两点不显然的：
 * - 不用 detectTapGestures(onLongPress)：它的 slop 不可配，判定语义也不同，
 *   索性手写状态机；
 * - 事件用 PointerEventPass.Final 收：文本框会在 Initial 段消费掉 change，
 *   Final 段拿到的事件与 pressed 状态不受消费先后影响，抬手一定看得见。
 */
@Composable
fun Modifier.voiceHold(enabled: () -> Boolean, holdMs: Long = 260L, cancelUp: Dp = 100.dp,
                       onTap: () -> Unit = {},
                       onStart: () -> Unit, onZone: (Boolean) -> Unit,
                       onFinish: (Boolean) -> Unit): Modifier {
    val slopPx = with(LocalDensity.current) { 12.dp.toPx() }
    val cancelPx = with(LocalDensity.current) { cancelUp.toPx() }
    // 第 8 轮总根因：pointerInput 只在 keys 变化时重启协程，协程一直吃第 1 次组合
    // 传进来的 enabled 闭包——那里面 keyboardUp 是普通 Boolean 快照，永远冻在 false。
    // 于是"键盘起了不接管语音"的门控在真机上从来没生效过（第 4~8 轮反复复发的病根）。
    // rememberUpdatedState 让每次按下现取最新那份 lambda，键盘态才吃得上。
    val enabledRef = rememberUpdatedState(enabled)
    val tapRef = rememberUpdatedState(onTap)
    return pointerInput(holdMs, cancelUp) {
        // PointerGestureScope 本身不是 CoroutineScope（1.6 实测 launch 报接收者
        // 类型不匹配），长按定时器要挂在外面这层 coroutineScope 上。
        coroutineScope {
        awaitEachGesture {
            val down = awaitFirstDown(requireUnconsumed = false)
            if (!enabledRef.value()) return@awaitEachGesture
            // 第 10 轮（豆包/DeepSeek 式接管）：闸门生效就把按下在 Initial 段一口吃掉
            // ——本分支下文本框压根没挂载，消费只是把"绝不冒键盘"钉死在源头。
            down.consume()
            var started = false
            var cancelling = false
            var movedOut = false
            // 第 13 轮（用户点名「不说话松手后语音 UI 依然在」）：onFinish 必须
            // 一次且仅一次交到状态机手里——正常抬手走循环出口，协程被掐/异常
            // 退出走 finally 兜底。漏掉一次，录音状态就没人收尾，UI 挂死在屏上。
            var settled = false
            // 抬手/手势终止时显式掐掉定时器：定时器挂在 pointerInput 级的作用域上，
            // 不会随手势自己收摊——不掐就是"手指都离开了还凭空开始录音"。
            val timer = launch {
                delay(holdMs)
                if (!movedOut) { started = true; onStart() }
            }
            try {
                while (true) {
                    val event = awaitPointerEvent(PointerEventPass.Final)
                    val c = event.changes.firstOrNull { it.id == down.id } ?: break
                    val upShift = down.position.y - c.position.y
                    if (!started &&
                        (abs(upShift) > slopPx || abs(c.position.x - down.position.x) > slopPx))
                        movedOut = true
                    if (started) {
                        val nowCancel = upShift > cancelPx
                        if (nowCancel != cancelling) { cancelling = nowCancel; onZone(nowCancel) }
                    }
                    if (!c.pressed) break
                }
                if (started) { settled = true; onFinish(cancelling) }
                else if (!movedOut) { settled = true; tapRef.value() }
            } finally {
                timer.cancel()
                if (started && !settled) onFinish(cancelling)
            }
        }
        }
    }
}

/* ---------------- 录音界面（第 13 轮：输入位上方的紧凑卡片） ----------------
 * 第 12 轮的全屏接管被真机判「占据整个屏幕很不美观，大小比输入框大点就行」——
 * 本轮收小：一枚圆角卡片贴在屏幕底部，正好盖住输入卡（输入框、模型钮、＋全部
 * 藏进卡片后面），高度只比输入卡大一圈。配色沿用豆包实录：录音=绿渐变+
 * 「松手发送，上移取消」+白波纹；上滑=粉渐变红字「松手取消」+红波纹。
 * 手势锚点仍在输入位那个 Box 上：本卡片不挂 pointerInput、不吃事件，录音状态
 * 机照常走；卡片出现也不影响已按下手指的事件分发。 */

@Composable
fun VoiceScreen(heard: String, level: Float, cancelling: Boolean,
                modifier: Modifier = Modifier) {
    val bg = if (cancelling) Brush.verticalGradient(listOf(Color(0xFFF7D4CF), Color(0xFFEFB3AC)))
             else Brush.verticalGradient(listOf(Color(0xFF8EDFBC), Color(0xFF5FCF9E)))
    val ink = if (cancelling) Color(0xFFD8443A) else Color(0xFF123B2E)
    val waveInk = if (cancelling) Color(0xFFE0463C) else Color(0xFFFFFFFF)
    Column(modifier.fillMaxWidth().padding(horizontal = 14.dp)
        .clip(RoundedCornerShape(26.dp)).background(bg)
        .padding(horizontal = 18.dp, vertical = 14.dp),
        horizontalAlignment = Alignment.CenterHorizontally) {
        Text(if (cancelling) "松手取消" else "松手发送，上移取消",
            fontSize = 14.sp, fontWeight = FontWeight.SemiBold, color = ink)
        Spacer(Modifier.height(8.dp))
        WaveBars(level, waveInk, bars = 24)
        if (heard.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text(heard, fontSize = 16.sp, fontWeight = FontWeight.Medium, color = ink,
                maxLines = 2, overflow = TextOverflow.Ellipsis,
                textAlign = TextAlign.Center)
        }
    }
}

/* 波纹条：高度 = 实时音量(level) × 每根柱子的固定权重 × 一个呼吸脉冲。
 * 第 12 轮起随全屏场景搬家：颜色由外层传入（录音白、取消红），宽版 30 根。 */
@Composable
private fun WaveBars(level: Float, barColor: Color, bars: Int = 30) {
    val pulse by rememberInfiniteTransition(label = "voicePulse").animateFloat(
        0.85f, 1.15f, infiniteRepeatable(tween(360), RepeatMode.Reverse), label = "v")
    val amp = 0.2f + 0.8f * level
    Row(Modifier.height(30.dp).padding(top = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(3.dp)) {
        // 权重两头低中间高（半圆 sin），再叠一个 fract(sin) 抖动打散，避免齐步走
        repeat(bars) { i ->
            val t = if (bars > 1) i / (bars - 1).toFloat() else 0.5f
            val center = sin(t * PI.toFloat()) * 0.7f + 0.3f
            val raw = sin(i * 12.9898f) * 43758.5453f
            val jitter = abs(raw - raw.toInt())
            val h = (4f + 24f * amp * center * (0.4f + 0.6f * jitter) * pulse)
                .coerceIn(3f, 28f)
            Box(Modifier.width(3.dp).height(h.dp)
                .clip(RoundedCornerShape(2.dp))
                .background(barColor.copy(alpha = 0.55f + 0.45f * center)))
        }
    }
}
