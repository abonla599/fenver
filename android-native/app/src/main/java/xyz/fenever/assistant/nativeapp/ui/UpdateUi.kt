package xyz.fenever.assistant.nativeapp.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import xyz.fenever.assistant.nativeapp.update.Updater

/* 应用内更新的两个底部弹层（v0.23 T1.7）：下载进度 + 「安装未知应用」授权引导。
 * 状态全部住在 Updater（对象级快照），所以设置弹层被划掉、二级页换来换去，
 * 下载中的进度都不会丢——重开设置页时弹层会原样再挂上来。 */

@Composable
fun UpdateProgressDialog(onCancel: () -> Unit) {
    val version = Updater.decision?.version ?: ""
    // 下载中不给"点外面"关掉的路，取消只有一个按钮
    FenverSheet(onDismiss = { /* 同上：唯一出口是「取消」 */ }) {
        Text("下载 v$version", style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onSurface)
        Spacer(Modifier.height(12.dp))
        val barModifier = Modifier.fillMaxWidth().padding(vertical = 8.dp)
        if (Updater.totalBytes > 0) {
            LinearProgressIndicator(progress = { Updater.percent / 100f },
                modifier = barModifier,
                color = MaterialTheme.colorScheme.primary,
                trackColor = MaterialTheme.colorScheme.surfaceVariant)
        } else {
            LinearProgressIndicator(
                modifier = barModifier,
                color = MaterialTheme.colorScheme.primary,
                trackColor = MaterialTheme.colorScheme.surfaceVariant)
        }
        Text(if (Updater.totalBytes > 0)
                "已收 ${fmtBytes(Updater.doneBytes)} / ${fmtBytes(Updater.totalBytes)}"
            else "已收 ${fmtBytes(Updater.doneBytes)}",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(12.dp))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
            FenverButton("取消", onClick = onCancel, kind = FenverButtonKind.Secondary,
                modifier = Modifier.width(96.dp))
        }
    }
}

/** Updater.installGuideVersion 非空时挂出来：装不了不是死路，给出那两页能去的路。 */
@Composable
fun InstallGuideDialog(onGoSettings: () -> Unit, onDismiss: () -> Unit) {
    val v = Updater.installGuideVersion ?: return
    val noPage = v == "NO-PAGE"
    FenverSheet(onDismiss = onDismiss) {
        Text(if (noPage) "没能打开系统设置" else "装不了：v$v",
            style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onSurface)
        Spacer(Modifier.height(8.dp))
        Text(if (noPage)
                "这台手机的系统没有给出可以改这个开关的页面。手动到「设置 → 应用 → Fenver」里" +
                "允许\"安装未知应用\"，再回来点一次立即更新就行。"
            else
                "这台手机还没允许「Fenver」安装应用。打开那个开关后再回来点一次立即更新就行。",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(12.dp))
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FenverButton("稍后", onClick = onDismiss, kind = FenverButtonKind.Secondary,
                modifier = Modifier.weight(1f))
            if (noPage) {
                FenverButton("好", onClick = onDismiss, kind = FenverButtonKind.Primary,
                    modifier = Modifier.weight(1f))
            } else {
                FenverButton("去设置", onClick = onGoSettings, kind = FenverButtonKind.Primary,
                    modifier = Modifier.weight(1f))
            }
        }
    }
}

private fun fmtBytes(b: Long): String = when {
    b >= 1_048_576 -> String.format("%.1f MB", b / 1_048_576.0)
    else -> (b / 1024).toString() + " KB"
}
