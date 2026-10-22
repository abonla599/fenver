package xyz.fenever.assistant.nativeapp.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import xyz.fenever.assistant.nativeapp.Api
import xyz.fenever.assistant.nativeapp.Prefs
import xyz.fenever.assistant.nativeapp.SessionSummary

/* 左侧会话抽屉：surface 全高面板，品牌行 + 导航 + 搜索 + 按日分组的会话 + 账户行。
 * tick 每次拉开 ++ 以重拉清单（不在这里做缓存）；
 * 非活动行没有删除钮，长按兜底；活动行才露「×」。 */
@OptIn(androidx.compose.foundation.ExperimentalFoundationApi::class)
@Composable
fun SessionDrawer(tick: Int, currentId: String, initialSessions: List<SessionSummary>,
                  onNewChat: () -> Unit, onOpen: (String) -> Unit,
                  onMemory: () -> Unit, onSettings: () -> Unit,
                  onModelService: () -> Unit, onDeleted: (String) -> Unit,
                  onRefreshSessions: suspend () -> String?, onClose: () -> Unit) {
    val scope = rememberCoroutineScope()
    var sessions by remember { mutableStateOf(initialSessions) }
    var loading by remember { mutableStateOf(initialSessions.isEmpty()) }
    var error by remember { mutableStateOf<String?>(null) }
    var search by remember { mutableStateOf("") }
    var pendingDelete by remember { mutableStateOf<xyz.fenever.assistant.nativeapp.SessionSummary?>(null) }

    // 抽屉先显示 ChatScreen 已预取的列表，再在后台刷新；不在打开时清空重拉。
    LaunchedEffect(initialSessions) { sessions = initialSessions }
    LaunchedEffect(tick) {
        if (tick == 0) return@LaunchedEffect
        loading = sessions.isEmpty()
        error = onRefreshSessions()
        loading = false
    }

    val kw = search.trim().lowercase()
    val visible = sessions.filter {
        kw.isEmpty() || it.title.lowercase().contains(kw) || it.session_id.lowercase().contains(kw)
    }
    val groupedList = LinkedHashMap<String, MutableList<SessionSummary>>()
    visible.forEach { s -> groupedList.getOrPut(groupOf(s.created_at)) { mutableListOf() }.add(s) }

    val screenW = LocalConfiguration.current.screenWidthDp.dp
    val drawerW = minOf(screenW * 0.84f, 300.dp)

    FenverSurface(
        modifier = Modifier.width(drawerW).fillMaxSize(),
        color = MaterialTheme.colorScheme.surface,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outline),
        shape = RoundedCornerShape(0.dp),
        contentPadding = PaddingValues(0.dp),
    ) {
        Column(Modifier.fillMaxSize()) {
            Row(Modifier.fillMaxWidth().heightIn(min = 56.dp)
                .padding(start = 16.dp, end = 4.dp),
                verticalAlignment = Alignment.CenterVertically) {
                xyz.fenever.assistant.nativeapp.theme.AiBrandMark(22)
                Spacer(Modifier.width(10.dp))
                BrandText("Fenver", 16)
                Spacer(Modifier.weight(1f))
                FenverTopBarAction(onClose) {
                    Text("×", fontSize = 20.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }

            DrawerNav("＋", "新对话", onNewChat)
            DrawerNav("⌗", "模型服务", onModelService)
            DrawerNav("◈", "长期记忆", onMemory)

            Box(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp)
                .clip(RoundedCornerShape(12.dp))
                .background(MaterialTheme.colorScheme.surfaceVariant)
                .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(12.dp))) {
                BasicTextField(search, { search = it }, singleLine = true,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                    textStyle = TextStyle(fontSize = 14.sp,
                        color = MaterialTheme.colorScheme.onSurface),
                    cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
                    decorationBox = { inner ->
                        Box(Modifier.padding(horizontal = 12.dp, vertical = 10.dp)) {
                            if (search.isEmpty()) Text("搜索会话", fontSize = 14.sp,
                                color = xyz.fenever.assistant.nativeapp.theme.text3Color())
                            inner()
                        }
                    },
                    modifier = Modifier.fillMaxWidth())
            }

            Text("会话", fontSize = 12.sp,
                color = xyz.fenever.assistant.nativeapp.theme.text3Color(),
                modifier = Modifier.padding(start = 16.dp, top = 12.dp, bottom = 2.dp))

            LazyColumn(Modifier.fillMaxWidth().weight(1f),
                contentPadding = PaddingValues(bottom = 8.dp)) {
                // Compose 在组合退出时用这条取消文案结束抽屉的协程；它已经在 catch 里
                // 按 CancellationException 重新抛出。这里再挡一层：任何路径把这条无害的
                // 取消消息写进 error 时都不该长成给用户看的红字。
                val shownError = error?.takeUnless { it.contains("left the composition") }
                if (shownError != null) item {
                    Text(shownError, fontSize = 13.sp, color = MaterialTheme.colorScheme.error,
                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp))
                }
                if (visible.isEmpty()) item {
                    // 网页 renderSessions 的空态文案；预取未回来时先说在读取。
                    Text(if (loading && kw.isEmpty()) "正在读取会话…"
                         else if (kw.isNotEmpty()) "没有匹配的会话" else "还没有会话",
                        fontSize = 12.sp,
                        color = xyz.fenever.assistant.nativeapp.theme.text3Color(),
                        modifier = Modifier.padding(horizontal = 16.dp, vertical = 10.dp))
                }
                groupedList.forEach { (label, list) ->
                    item {
                        Text(label, fontSize = 12.sp,
                            color = xyz.fenever.assistant.nativeapp.theme.text3Color(),
                            modifier = Modifier.fillMaxWidth()
                                .padding(start = 16.dp, end = 16.dp, top = 10.dp, bottom = 2.dp))
                    }
                    items(list, key = { it.session_id }) { s ->
                        SessionDrawerRow(s, active = s.session_id == currentId,
                            onOpen = { onOpen(s.session_id); onClose() },
                            onDelete = { pendingDelete = s })
                    }
                }
            }

            Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp)) {
                Spacer(Modifier.height(8.dp))
                Box(Modifier.fillMaxWidth().height(1.dp)
                    .background(MaterialTheme.colorScheme.outline))
                Row(Modifier.fillMaxWidth().heightIn(min = 48.dp)
                    .clip(RoundedCornerShape(12.dp))
                    .clickable(onClick = { onSettings(); onClose() }),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Box(Modifier.size(28.dp)
                        .background(MaterialTheme.colorScheme.primaryContainer, CircleShape),
                        contentAlignment = Alignment.Center) {
                        Text(Prefs.username.take(1).ifBlank { "·" },
                            fontSize = 13.sp, color = MaterialTheme.colorScheme.primary,
                            fontWeight = FontWeight.SemiBold)
                    }
                    Text(Prefs.username.ifBlank { "未登录" }, fontSize = 14.sp,
                        color = MaterialTheme.colorScheme.onSurface,
                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.weight(1f))
                    Text("设置", fontSize = 13.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text("⚙", fontSize = 15.sp,
                        color = xyz.fenever.assistant.nativeapp.theme.text3Color())
                }
                Spacer(Modifier.height(8.dp))
            }
        }
    }

    pendingDelete?.let { target ->
        AlertDialog(
            onDismissRequest = { pendingDelete = null },
            title = { Text("删除这个会话？") },
            confirmButton = {
                TextButton(onClick = {
                    pendingDelete = null
                    scope.launch {
                        try {
                            Api.deleteSession(target.session_id)
                            sessions = sessions.filter { it.session_id != target.session_id }
                            onDeleted(target.session_id)
                        } catch (e: CancellationException) {
                            // 删除请求在抽屉随组合退出时被取消：不是删除失败，不写红字。
                            throw e
                        } catch (e: Exception) {
                            error = e.message
                        }
                    }
                }) { Text("删除", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = { TextButton(onClick = { pendingDelete = null }) { Text("取消") } },
        )
    }
}

@Composable
private fun DrawerNav(ico: String, label: String, onClick: () -> Unit) {
    Row(Modifier.fillMaxWidth().heightIn(min = 48.dp)
        .clickable(onClick = onClick)
        .padding(horizontal = 16.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Box(Modifier.size(20.dp), contentAlignment = Alignment.Center) {
            Text(ico, fontSize = 18.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Text(label, fontSize = 15.sp, color = MaterialTheme.colorScheme.onSurface)
    }
}

@OptIn(androidx.compose.foundation.ExperimentalFoundationApi::class)
@Composable
private fun SessionDrawerRow(s: xyz.fenever.assistant.nativeapp.SessionSummary,
                             active: Boolean, onOpen: () -> Unit, onDelete: () -> Unit) {
    val title = s.title.ifBlank { "新对话" }
    Row(Modifier.fillMaxWidth().heightIn(min = 48.dp)
        .then(if (active) Modifier.background(
            MaterialTheme.colorScheme.primaryContainer)
        else Modifier)
        .combinedClickable(onClick = onOpen, onLongClick = onDelete)
        .padding(start = 16.dp, end = 8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        if (active) {
            Box(Modifier.width(2.dp).height(24.dp)
                .background(MaterialTheme.colorScheme.primary, RoundedCornerShape(999.dp)))
            Spacer(Modifier.width(10.dp))
        }
        Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis,
            fontSize = 15.sp,
            color = MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.weight(1f).padding(vertical = 12.dp))
        if (active) {
            Box(Modifier.size(44.dp).clip(CircleShape)
                .clickable(onClick = onDelete), contentAlignment = Alignment.Center) {
                Text("×", fontSize = 16.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

/* 会话按日分组，标签与网页 groupLabel 逐字：今天/昨天/近 7 天/近 30 天/更早
 * （判据同一条：days<=0 今天，==1 昨天，<=7、<=30，其余更早）。
 * created_at 是 ISO 串，minSdk 24 用 SimpleDateFormat 算日差。 */
private fun groupOf(iso: String): String {
    if (iso.length < 10) return "更早"
    val fmt = java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US)
    val then = try { fmt.parse(iso.take(10)) } catch (e: Exception) { null } ?: return "更早"
    val today = try { fmt.parse(fmt.format(java.util.Date())) } catch (e: Exception) { null }
        ?: return "更早"
    val days = ((today.time - then.time) / (24 * 3600 * 1000L)).toInt()
    return when {
        days <= 0 -> "今天"
        days == 1 -> "昨天"
        days <= 7 -> "近 7 天"
        days <= 30 -> "近 30 天"
        else -> "更早"
    }
}
