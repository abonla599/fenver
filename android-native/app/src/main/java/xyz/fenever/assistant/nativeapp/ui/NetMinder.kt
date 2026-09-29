package xyz.fenever.assistant.nativeapp.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import xyz.fenever.assistant.nativeapp.ApiException
import xyz.fenever.assistant.nativeapp.Prefs

/* 断网提醒（2026-09-28 真机反馈第 3 轮）：连不上服务端或互联网时，在顶部浮一张
 * 小卡片说清楚"消息暂时发不出去"，带「知道了」和「今日不再显示」。
 *
 * 判据只认连接层异常（DNS 解析失败、拒绝连接、超时——即非 ApiException 的异常）：
 * 4xx/5xx 是服务端正常回话但这件事办不成，那是业务错误，走状态条，不算断网。
 * 「今日不再显示」按本地日期存 Prefs，隔天自动恢复提醒——断网这种事隔天值得再知道一次。
 * 网页端（api.js 的 NetMinder + localStorage）同一套判据与文案。 */
object NetMinder {
    private fun today(): String =
        java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.ROOT).format(java.util.Date())

    /** 最近一次网络请求是不是"根本没连上"。置 true 弹卡片，任何一次成功通信清零。 */
    var offline by mutableStateOf(false)
        private set

    fun noteSuccess() { offline = false }

    fun noteFailure(e: Exception) { if (e !is ApiException) offline = true }

    fun mutedToday(): Boolean = Prefs.offlineMuteDate == today()

    /** 卡片该不该在屏幕上：断网中，且今天没被静音。 */
    fun show(): Boolean = offline && !mutedToday()

    fun dismiss() { offline = false }

    fun muteToday() {
        Prefs.offlineMuteDate = today()
        offline = false
    }
}

/** 顶部居中小卡片：悬浮在内容之上（Box overlay），不占布局、不推内容。 */
@Composable
fun NetMinderCard(modifier: Modifier = Modifier) {
    val scheme = MaterialTheme.colorScheme
    Column(modifier.clip(RoundedCornerShape(14.dp))
        .background(scheme.surface.copy(alpha = 0.97f))
        .border(1.dp, scheme.outlineVariant, RoundedCornerShape(14.dp))
        .padding(horizontal = 14.dp, vertical = 8.dp),
        horizontalAlignment = Alignment.CenterHorizontally) {
        Row(verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            Text("⚠", fontSize = 13.sp, color = scheme.error)
            Text("连不上网络了，消息暂时发不出去", fontSize = 13.sp,
                fontWeight = FontWeight.Medium, color = scheme.onSurface)
        }
        Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            TextButton(onClick = { NetMinder.dismiss() }) {
                Text("知道了", fontSize = 12.sp)
            }
            TextButton(onClick = { NetMinder.muteToday() }) {
                Text("今日不再显示", fontSize = 12.sp, color = scheme.onSurfaceVariant)
            }
        }
    }
}
