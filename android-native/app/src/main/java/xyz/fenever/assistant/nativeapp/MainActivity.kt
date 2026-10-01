package xyz.fenever.assistant.nativeapp

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import xyz.fenever.assistant.core.ReleasePlan
import xyz.fenever.assistant.nativeapp.theme.AiTheme
import xyz.fenever.assistant.nativeapp.theme.ThemeMode
import xyz.fenever.assistant.nativeapp.ui.AuthScreen
import xyz.fenever.assistant.nativeapp.ui.ChatScreen
import xyz.fenever.assistant.nativeapp.ui.reenterRequested

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Prefs.init(applicationContext)
        ReminderStore.init(applicationContext)
        ReminderChannels.ensure(applicationContext)
        sweepStaleApks(applicationContext)
        // 冷启动第一帧就把已存的外观偏好灌进 Compose state，不闪一下深色
        ThemeMode.value = Prefs.themeMode
        setContent {
            AiTheme { AppRoot() }
        }
    }
}

/* 第 15 轮（用户点名「下载新版本之后，新版本的安装包要直接自动删除掉」）：
 * 装完遗留的 APK 躺在应用私有下载目录里白占 7 MB。冷启动时扫一遍，只删
 * 版本 ≤ 当前已装版本的包——那是已经装完（或被弃）的旧账；版本比当前新的
 * 包不许动：可能刚下载完、安装页还攥着 FileProvider 的读权限在路上，删了
 * 就是拆用户正在装的那次升级。半截 .apk.part 同规则清掉。 */
private fun sweepStaleApks(ctx: android.content.Context) {
    val dir = ctx.getExternalFilesDir(android.os.Environment.DIRECTORY_DOWNLOADS) ?: return
    val files = dir.listFiles() ?: return
    // 两个前缀都要扫：v0.24 起新包叫 fenver-*，可这台机器上躺着的大概率还是
    // 0.23.x 那一次下载留下的 ai-assistant-native-*。只扫新名的话，改名这一次
    // 顺手把"装完自动删包"那条承诺在老包上作废了——7 MB 白占着，而它看起来什么都没坏。
    for (prefix in ReleasePlan.APK_PREFIXES) {
        for (f in files) {
            val n = f.name
            if (!n.startsWith(prefix)) continue
            val part = n.endsWith(".apk.part")
            if (!part && !n.endsWith(".apk")) continue
            val v = n.removePrefix(prefix).removeSuffix(if (part) ".apk.part" else ".apk")
            if (!Regex("[0-9][0-9A-Za-z.\\-]*").matches(v)) continue
            runCatching {
                if (ReleasePlan.compare(v, BuildConfig.VERSION_NAME) <= 0) f.delete()
            }
        }
    }
}

/* 导航结构与旧壳/网页一致：登录后只有"聊天页"一块主屏。
 * 会话列表是聊天页左侧抽屉，设置是底部弹层、五张二级页在弹层里换 view——
 * 不再有独立的 sessions/memory/settings 路由。认证页是同一层的另一张屏：
 * 未登录时它就是主屏；壳内要重新认证（注册新身份/改密码）时它盖上来，
 * 从盖上来的路径可以 × 退回聊天页（旧会话还在），登录成功则整壳重建。 */
@Composable
private fun AppRoot() {
    val ctx = LocalContext.current
    var authed by remember { mutableStateOf(Prefs.isAuthed) }
    var bootTick by remember { mutableIntStateOf(0) }        // ++ = 等价网页 location.reload
    var authMode by remember { mutableStateOf("login") }
    var showAuth by remember { mutableStateOf(!authed) }

    if (showAuth) {
        AuthScreen(initialMode = authMode,
            onBack = if (authed) {
                { showAuth = false; authMode = "login"; bootTick++ }
            } else null,
            onAuthed = {
                showAuth = false; authMode = "login"
                authed = true
                ReminderStore.setOwner(Prefs.currentEntry()?.userId ?: "")
                bootTick++
            })
    } else {
        key(bootTick) {
            LaunchedEffect(bootTick) {
                Prefs.currentEntry()?.let { ReminderStore.setOwner(it.userId) }
            }
            ChatScreen(
                onRequireAuth = { mode -> authMode = mode; showAuth = true },
                onLoggedOut = {
                    // 切换账号/收编令牌：静默换人重建聊天壳；真退出才落回登录页
                    if (reenterRequested) { reenterRequested = false; bootTick++ }
                    else { authed = false; showAuth = true; authMode = "login" }
                },
                onOpenUrl = { u ->
                    runCatching {
                        ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(u))
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    }
                },
            )
        }
    }
}
