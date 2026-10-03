package xyz.fenever.assistant.nativeapp.ui

import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DatePicker
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TimePicker
import androidx.compose.material3.rememberDatePickerState
import androidx.compose.material3.rememberTimePickerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.LifecycleOwner
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import xyz.fenever.assistant.nativeapp.Api
import xyz.fenever.assistant.nativeapp.ApiException
import xyz.fenever.assistant.nativeapp.AuthResult
import xyz.fenever.assistant.nativeapp.BuildConfig
import xyz.fenever.assistant.core.ExportName
import xyz.fenever.assistant.core.ReleasePlan
import xyz.fenever.assistant.nativeapp.export.ExportTracker
import xyz.fenever.assistant.nativeapp.export.SessionDownloads
import xyz.fenever.assistant.nativeapp.ModelInfo
import xyz.fenever.assistant.nativeapp.Prefs
import xyz.fenever.assistant.nativeapp.ReminderChannels
import xyz.fenever.assistant.nativeapp.ReminderScheduler
import xyz.fenever.assistant.nativeapp.ReminderStore
import xyz.fenever.assistant.nativeapp.update.Updater
import xyz.fenever.assistant.nativeapp.theme.ThemeMode
import xyz.fenever.assistant.nativeapp.theme.aiSoftBrush
import xyz.fenever.assistant.nativeapp.theme.isWebLight
import xyz.fenever.assistant.nativeapp.theme.text3Color
import java.io.File

/* 设置弹层 = 网页 index.html 的 #settings 一块（.set-sheet → .set-bar → .set-scroll
 * → .set-group/.set-card/.set-row → 二级页 .set-view#setPages）的原生化。
 * 一级列表 + 五个二级页在同一张弹层里换 view（网页原话：谁都不该是第二个弹窗），
 * 行序、文案、值槽、›/⇅/↗ 后缀、按钮集合与 app.js 的渲染函数逐处对齐：
 * - 一级：syncSetIdentity/renderModelSelect+chatNote/syncPersonaChip/syncCtxRow/
 *   exportCurrent/loadRemindersVal/renderAboutRows/applyTheme/connInfo/logoutCurrent
 * - accounts=renderAccounts　providers=loadProviders+providerRow+provForm
 * - persona/memory=loadMemories　reminders=renderReminders+reminderStatusCard
 * 数值全部取自 style.css 移动档（行高 54=拇指热区、卡片半径 18、12.5px 组头等）。
 */

/** 原生有每个人的 Bearer 令牌，「切换账号」可以静默换人（网页 Cookie 装不下第二个人，
 *  只能逼人重输密码——那是方案 C 的限制，不是我们的取舍）。换完要求重建聊天壳，
 *  MainActivity 看到这一枚旗标就把 ChatScreen 整个重挂（等价网页 location.reload）。 */
internal var reenterRequested = false

private val SET_PAGES = mapOf(
    "providers" to "模型服务", "accounts" to "账户", "persona" to "角色设定",
    "memory" to "长期记忆", "reminders" to "提醒", "schedule" to "日程",
    "feedback" to "反馈",
)

/* v0.24 T4.1（D16 保留项）：「关于」页对外的三句话在这里只写一遍，网页侧
 * backend/app/web/static/index.html 的「关于」组写的是同一串字面量——
 * 逐字同值由 backend/tests/test_about_page_contract.py 判。
 * 为什么值得为三行字立一条锁：手机上说一套、浏览器里说另一套，谁都不会报错，
 * 只有真想去看代码、去看许可证的那个人会撞上，而那时他已经不信任这一屏了。 */
private const val ABOUT_TAGLINE = "Fenver：跑在你自己服务器上的个人 Agent 助理"
private const val ABOUT_REPO_HOST = "github.com/abonla599/fenver"
private const val ABOUT_REPO_URL = "https://github.com/abonla599/fenver"
private const val ABOUT_LICENSE = "Apache-2.0"

@Composable
fun SettingsSheet(page: String, onOpenPage: (String?) -> Unit,
                  onRequireAuth: (String) -> Unit, onOpenUrl: (String) -> Unit,
                  onModelsChanged: () -> Unit, onLoggedOut: () -> Unit,
                  canExport: Boolean) {
    val scope = rememberCoroutineScope()

    // ---------- 跨页共享的状态（对照 app.js 的 state / 各 sync* 函数） ----------
    var identityTick by remember { mutableIntStateOf(0) }          // 名单/身份变了就 ++
    var note by remember { mutableStateOf("") }                     // 网页的 setStatus 落点
    var noteErr by remember { mutableStateOf(false) }
    var providers by remember { mutableStateOf(listOf<ModelInfo>()) }
    var serverDefault by remember { mutableStateOf<String?>(null) }
    var memoryCount by remember { mutableStateOf("") }
    var scheduleVal by remember { mutableStateOf("") }        // 「N 天有安排」              // 「N 条」/「50+ 条」/""
    var serverBuild by remember { mutableStateOf("") }

    /* 每页各存一份滚动位置：主列表滑到靠下 → 点进任意子页 → 返回，不再弹回顶部
       （真机反馈：以前返回后得从头重新滑到靠下才能继续操作）。弹层切页时整体不离开
       组合，这份 map 随它存活；页名做 key，主列表是 ""，各子页互不串位。 */
    val scrollByPage = remember { mutableStateMapOf<String, ScrollState>() }
    val pageScroll = scrollByPage.getOrPut(page) { ScrollState(0) }

    fun setStatus(t: String, err: Boolean = false) { note = t; noteErr = err }
    /* 首拉有没有回来过：设置页一进来 providers 是空的，那不等于"服务端没有模型"。
     * v0.28.2 真机反馈 2 的第二半——把"还没读到"说成「服务端还没有可用的模型」，
     * 人会去添加重复的模型服务；读取中就说读取中。 */
    var modelsLoaded by remember { mutableStateOf(false) }
    fun reloadProviders() {
        scope.launch {
            runCatching { val r = Api.models(); providers = r.models; serverDefault = r.default }
            modelsLoaded = true
            onModelsChanged()
        }
    }

    LaunchedEffect(Unit) {
        runCatching { val r = Api.models(); providers = r.models; serverDefault = r.default }
        modelsLoaded = true
        runCatching {
            val el = Api.listMemory(50)
            val arr = (el as? JsonObject)?.get("memories") as? JsonArray
            memoryCount = if (arr == null) "" else
                if (arr.size >= 50) "50+ 条" else "${arr.size} 条"
        }
        runCatching {
            serverBuild = Api.health()["build"]?.jsonPrimitive?.contentOrNull ?: ""
        }
    }

    // 用户要求：设置页顶到最上面 —— 弹层高度不再封顶 600dp，直接铺满整屏
    Column(Modifier.fillMaxWidth().fillMaxHeight()
        .padding(bottom = 18.dp)) {
        SetBar(page, onOpenPage)
        if (note.isNotEmpty()) {
            Text(note, fontSize = 13.sp,
                color = if (noteErr) MaterialTheme.colorScheme.error else
                    MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 6.dp))
        }
        Column(Modifier.weight(1f, fill = false).verticalScroll(pageScroll)
            .padding(start = 16.dp, end = 16.dp, top = 4.dp, bottom = 22.dp)) {
            when (page) {
                "providers" -> ProvidersPage(onChanged = { reloadProviders() })
                "accounts" -> AccountsPage(identityTick,
                    onTick = { identityTick++ },
                    onRequireAuth = onRequireAuth,
                    onSwitched = { reenterRequested = true; onLoggedOut() })
                "persona" -> PersonaPage(onNote = { setStatus(it) },
                    onClose = { onOpenPage(null) })
                "memory" -> MemoryPage(onCountChanged = { memoryCount = it },
                    onNote = { setStatus(it, true) })
                "schedule" -> SchedulePage(onNote = { t, err -> setStatus(t, err) },
                    onRequireAuth = onRequireAuth,
                    onValChanged = { scheduleVal = it })
                "feedback" -> FeedbackPage(onNote = { t, err -> setStatus(t, err) },
                    onClose = { onOpenPage("") })
                "reminders" -> RemindersPage(onNote = { t, err -> setStatus(t, err) })
                else -> SettingsList(onOpenPage, onRequireAuth, onOpenUrl, onLoggedOut,
                    providers = providers, memoryCount = memoryCount,
                    scheduleVal = scheduleVal,
                    serverBuild = serverBuild, identityTick = identityTick,
                    modelsLoading = !modelsLoaded,
                    onTick = { identityTick++ }, onNote = { t, err -> setStatus(t, err) },
                    onModelsChanged = { reloadProviders() }, canExport = canExport)
            }
        }
    }
}

/* ---------------- 弹层头（.set-bar：56 高、17/600 标题、1px 下边线） ---------------- */
@Composable
private fun SetBar(page: String, onOpenPage: (String?) -> Unit) {
    val sub = page.isNotEmpty()
    Row(Modifier.fillMaxWidth().heightIn(min = 56.dp)
        .padding(start = 4.dp, end = 8.dp, top = 6.dp, bottom = 6.dp),
        verticalAlignment = Alignment.CenterVertically) {
        if (sub) {
            // .set-back：‹ 回了就是一级列表（网页 closeSetPage）
            Text("‹", fontSize = 26.sp, color = MaterialTheme.colorScheme.onSurface,
                modifier = Modifier.clip(CircleShape)
                    .clickable { onOpenPage("") }
                    .padding(horizontal = 10.dp, vertical = 2.dp))
        }
        Text(if (sub) SET_PAGES[page] ?: "设置" else "设置",
            fontSize = 17.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface,
            textAlign = if (sub) TextAlign.Start else TextAlign.Center,
            maxLines = 1, overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f))
        if (!sub) {
            // .set-x：× 只在一级页出现——二级页的退路是 ‹，不是关整层
            Text("×", fontSize = 24.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.clip(CircleShape)
                    .clickable { onOpenPage(null) }
                    .padding(horizontal = 10.dp, vertical = 2.dp))
        } else Spacer(Modifier.width(12.dp))
    }
    Box(Modifier.fillMaxWidth().height(1.dp)
        .background(MaterialTheme.colorScheme.outline))
}

/* ---------------- 一行的积木（.set-row / .set-card / .set-group / .set-mini） ---------------- */

/** 触感反馈的取用点（借鉴 WorkBuddy 的「触感反馈」开关）：设置页里所有
 *  可点的行、分段与开关共用这一条；关掉总闸后各处都静默。
 *  走 View 平台的 performHapticFeedback 而不是 Compose 的 LocalHapticFeedback——
 *  后者在本工程锁定的 BOM 2024.06 里 import 不解析（CI 实测），前者是它的地基。 */
@Composable
private fun rememberSetHaptic(): () -> Unit {
    val view = LocalView.current
    return {
        if (Prefs.hapticsEnabled) {
            view.performHapticFeedback(android.view.HapticFeedbackConstants.VIRTUAL_KEY)
        }
    }
}

/** WorkBuddy 那颗绿色开关的原生对位：轨道吃强调色，白点走两端。
 *  不复用 Material3 Switch——它的默认配色与本页深浅档都不贴，形状也不同。 */
@Composable
private fun SetSwitch(on: Boolean, onChange: (Boolean) -> Unit) {
    val haptic = rememberSetHaptic()
    val scheme = MaterialTheme.colorScheme
    Box(Modifier.width(46.dp).height(28.dp).clip(CircleShape)
        .background(if (on) scheme.primary else scheme.outline)
        .clickable { haptic(); onChange(!on) }
        .padding(3.dp),
        contentAlignment = if (on) Alignment.CenterEnd else Alignment.CenterStart) {
        Box(Modifier.size(22.dp).background(Color.White, CircleShape))
    }
}

/** 「外观」行的三态分段（系统/浅色/深色，借鉴 WorkBuddy 的主题行）。
 *  选中态读 ThemeMode.value 而不是 Prefs.themeMode：前者是 Compose state，
 *  切一下这一条才会立刻重画；深档选中底是 primaryContainer（暗薄荷），
 *  字吃 primary——与全站「选中 = 强调色」的旧规矩同一口径。 */
@Composable
private fun ThemeSegment() {
    val haptic = rememberSetHaptic()
    val scheme = MaterialTheme.colorScheme
    val mode = ThemeMode.value
    Row(Modifier.clip(RoundedCornerShape(10.dp))
        .border(1.dp, scheme.outline, RoundedCornerShape(10.dp))
        .padding(2.dp),
        horizontalArrangement = Arrangement.spacedBy(2.dp)) {
        listOf("system" to "系统", "light" to "浅色", "dark" to "深色").forEach { (v, l) ->
            val on = mode == v
            Box(Modifier.clip(RoundedCornerShape(8.dp))
                .background(if (on) scheme.primaryContainer else Color.Transparent)
                .clickable { if (!on) { haptic(); Prefs.themeMode = v } }
                .padding(horizontal = 10.dp, vertical = 5.dp)) {
                Text(l, fontSize = 12.5.sp,
                    color = if (on) scheme.primary else text3Color(),
                    fontWeight = if (on) FontWeight.SemiBold else FontWeight.Normal)
            }
        }
    }
}

@Composable
private fun SetCard(content: @Composable ColumnScope.() -> Unit) {
    Column(Modifier.fillMaxWidth()
        .background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(18.dp)),
        content = content)
}

@Composable
private fun SetGroup(title: String) {
    Text(title, fontSize = 12.5.sp, color = text3Color(),
        modifier = Modifier.padding(start = 2.dp, top = 22.dp, bottom = 8.dp))
}

/** 网页 .pane-note（13px / --text-2 / 下留白 14px）的原生化，与 SetGroup 同样的左边距。
 *  v0.24 T4.1 起「关于」用它念那句定位语——那一句在网页里就是组标题下面的一条注脚，
 *  不是卡片里的一行：它要说的是"这一组在讲什么"，而不是"这里有个能点的东西"。 */
@Composable
private fun SetNote(text: String) {
    Text(text, fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(start = 2.dp, bottom = 14.dp))
}

/** 网页 .set-row：min-height 54、padding 0 14、gap 12、15px 字；last-child 无底边线。
 *  onClick 放最后一位且非空：调用方尾随 lambda 走它，valSlot 用命名参数传。 */
@Composable
private fun SetRow(ico: String, label: String,
                   divider: Boolean = true,
                   danger: Boolean = false, plain: Boolean = false,
                   trailing: String = "›",
                   valSlot: (@Composable () -> Unit)? = null,
                   onClick: () -> Unit = {}) {
    val scheme = MaterialTheme.colorScheme
    val haptic = rememberSetHaptic()
    val labelColor = if (danger) scheme.error else scheme.onSurface
    Row(Modifier.fillMaxWidth()
        .then(if (!plain) Modifier.clickable { haptic(); onClick() } else Modifier)
        .heightIn(min = 54.dp).padding(horizontal = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(ico, fontSize = 16.sp,
            color = if (danger) scheme.error else scheme.onSurfaceVariant,
            modifier = Modifier.width(20.dp), textAlign = TextAlign.Center)
        Text(label, fontSize = 15.sp, color = labelColor, maxLines = 1,
            overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
        valSlot?.invoke()
        if (trailing.isNotEmpty()) {
            Text(trailing, fontSize = 15.sp, color = text3Color(),
                modifier = Modifier.width(14.dp), textAlign = TextAlign.Center)
        }
    }
    if (divider && !plain) {
        Box(Modifier.fillMaxWidth().padding(horizontal = 14.dp).height(1.dp)
            .background(scheme.outline))
    }
}

@Composable
private fun SetValText(text: String) {
    if (text.isNotEmpty()) {
        Text(text, fontSize = 14.sp, color = text3Color(), maxLines = 1,
            overflow = TextOverflow.Ellipsis)
    }
}

/** .set-mini：胶囊小按钮（去设置/取消/切换账号/删除账号）。del=true 描红。 */
@Composable
private fun SetMini(text: String, del: Boolean = false, enabled: Boolean = true,
                    onClick: () -> Unit) {
    val scheme = MaterialTheme.colorScheme
    val c = when {
        !enabled -> text3Color()
        del -> scheme.error
        else -> scheme.onSurfaceVariant
    }
    Box(Modifier.clip(CircleShape)
        .border(1.dp, c, CircleShape)
        .then(if (enabled) Modifier.clickable(onClick = onClick) else Modifier)
        .padding(horizontal = 12.dp, vertical = 9.dp)) {
        Text(text, fontSize = 12.5.sp, color = c)
    }
}

/** .btn-primary 的小号对位：浅档实底强调色，深档深绿底浅绿字。 */
@Composable
private fun SetMiniPrimary(text: String, enabled: Boolean = true, onClick: () -> Unit) {
    val scheme = MaterialTheme.colorScheme
    val light = isWebLight()
    val bg = if (light) scheme.primary else Color(0xFF062518)
    val fg = if (light) Color.White else Color(0xFFBFF3D9)
    Box(Modifier.clip(CircleShape)
        .background(bg)
        .border(1.dp, scheme.primary, CircleShape)
        .then(if (enabled) Modifier.clickable(onClick = onClick) else Modifier)
        .padding(horizontal = 14.dp, vertical = 9.dp)) {
        Text(text, fontSize = 12.5.sp, color = fg)
    }
}

/* ---------------- 一级列表（index.html #setList 逐行） ---------------- */

@Composable
private fun SettingsList(onOpenPage: (String?) -> Unit,
                         onRequireAuth: (String) -> Unit, onOpenUrl: (String) -> Unit,
                         onLoggedOut: () -> Unit,
                         providers: List<ModelInfo>, memoryCount: String,
                         scheduleVal: String,
                         serverBuild: String, identityTick: Int,
                         modelsLoading: Boolean,
                         onTick: () -> Unit,
                         onNote: (String, Boolean) -> Unit,
                         onModelsChanged: () -> Unit, canExport: Boolean) {
    val scope = rememberCoroutineScope()
    val ctx = LocalContext.current

    // ---------- 导出（T2.5）：一次点击的三条路都汇到 startExport ----------
    // 授权框回来说"不给"时，得记得是给哪个会话导过——票据不缓存（R2-AC-3），
    // 记的只是会话 id，重签新票再走兜底路。
    var pendingSid by remember { mutableStateOf<String?>(null) }

    fun startExport(sid: String, viaPrivate: Boolean) {
        scope.launch {
            runCatching {
                // 先取服务端标题再签票：标题拿不到就不浪费一张一次性票据；
                // 用服务端算好的 title 而不是本地消息重切，避免第二份截断规则。
                val title = Api.getSession(sid).title
                val ticket = Api.exportTicket(sid)
                if (!ExportName.isTicketPath(ticket.path))
                    throw IllegalStateException("服务端给的票据形状不对")
                val fileName = ExportName.exportFileName(title)
                if (viaPrivate) {
                    ExportTracker.savePrivateCopy(ctx, ticket.path, fileName)
                } else {
                    SessionDownloads.enqueue(ctx,
                        Prefs.baseUrl.trimEnd('/') + ticket.path, fileName)
                        .also { ExportTracker.watch(ctx, it, sid) }
                }
            }.onSuccess { dest ->
                if (viaPrivate) onNote("未获存储权限：已导出到应用私有目录 $dest", true)
            }.onFailure { onNote(exportClickFailure(it), true) }
        }
    }

    val storageAsk = rememberLauncherForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.RequestPermission()
    ) { granted ->
        val s = pendingSid
        pendingSid = null
        if (s != null) startExport(s, viaPrivate = !granted)
    }
    // 「检查更新」的三态与下载进度住在 Updater（对象级快照，弹层被划掉也不丢）；
    // 值槽与两个对话框都从它读——这里不再另起一份 remember 状态做第二真相。
    val admin = Prefs.role == "admin"
    val me = Prefs.currentEntry()
    val name = me?.username ?: ""
    val usable = providers.filter { it.usable }
    // v0.28.2：清单还没回来就说"在读了"——「服务端还没有可用的模型」会把一次慢加载
    // 说成一件不存在的事实，人会照着这句话去添加重复的模型服务。
    val chatNote = when {
        usable.isNotEmpty() -> "${usable.size} 个模型可用"
        modelsLoading -> "正在读取模型清单…"
        else -> "服务端还没有可用的模型"
    }
    val current = remember(providers, identityTick) {
        providers.firstOrNull { it.id == Prefs.defaultProviderId && it.usable }
            ?: providers.firstOrNull { it.id == serverDefaultOf(providers) && it.usable }
    }
    val cap = ((current?.max_context_k ?: 0).let { if (it > 0) it else 64 }).coerceAtLeast(2)

    // —— 「通用」组的三样：通知授权要能从系统页回来后重画（与提醒页同一个 ON_RESUME 路子），
    //    缓存大小在 IO 线程数一次、清完再数一次。
    var permTick by remember { mutableIntStateOf(0) }
    val owner = ctx as? LifecycleOwner
    DisposableEffect(owner) {
        val obs = LifecycleEventObserver { _, ev ->
            if (ev == Lifecycle.Event.ON_RESUME) permTick++
        }
        owner?.lifecycle?.addObserver(obs)
        onDispose { owner?.lifecycle?.removeObserver(obs) }
    }
    permTick
    val notifGranted = ReminderChannels.notificationsGranted(ctx)
    var hapticsOn by remember { mutableStateOf(Prefs.hapticsEnabled) }
    var cacheBytes by remember { mutableStateOf(-1L) }
    var confirmClearCache by remember { mutableStateOf(false) }
    val appCtx = ctx.applicationContext
    fun dirBytes(f: File?): Long = f?.let {
        runCatching { it.walkTopDown().filter { x -> x.isFile }.sumOf { x -> x.length() } }
            .getOrDefault(0L)
    } ?: 0L
    fun cacheBytesNow(): Long = dirBytes(appCtx.cacheDir) + dirBytes(appCtx.externalCacheDir)
    LaunchedEffect(Unit) { cacheBytes = withContext(Dispatchers.IO) { cacheBytesNow() } }

    // .set-me：头像首字（弱渐变底 + accent 字）+ 名字 + 角色 chip
    Spacer(Modifier.height(14.dp))
    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(18.dp))
        .background(MaterialTheme.colorScheme.surfaceVariant)
        .padding(14.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Box(Modifier.size(40.dp).background(aiSoftBrush(), CircleShape),
            contentAlignment = Alignment.Center) {
            Text(name.take(1).ifBlank { "·" }, fontSize = 16.sp,
                color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.SemiBold)
        }
        Text(name.ifBlank { "未登录" }, fontSize = 15.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface, maxLines = 1,
            overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
        if (name.isNotEmpty()) {
            Box(Modifier.clip(CircleShape).background(MaterialTheme.colorScheme.primaryContainer)
                .padding(horizontal = 8.dp, vertical = 3.dp)) {
                Text(if (admin) "管理员" else "普通用户", fontSize = 11.5.sp,
                    color = MaterialTheme.colorScheme.primary)
            }
        }
    }

    // —— 账户 ——
    SetGroup("账户")
    SetCard {
        SetRow("⇄", "切换账户", valSlot = {
            identityTick
            SetValText("${Prefs.readIdentities().size} 个已登录")
        }) { onOpenPage("accounts") }
        SetRow("∗", "改密码", divider = false) { onRequireAuth("recover") }
    }

    // —— 模型 ——（当前模型 + 模型服务同吃一张卡片，网页 .set-card 就两张行）
    SetGroup("模型")
    SetCard {
        ModelPickRow(providers, current, identityTick, modelsLoading) { id, p ->
            // 网页 renderModelSelect 的 select：选不可用的 → 一句红字 + 弹回原值
            if (!p.usable) {
                onNote(if (admin) "该模型未配置密钥，请先在「设置 → 模型服务」补全"
                else "该模型还没配好密钥，请联系管理员处理", true)
                return@ModelPickRow
            }
            onNote("", false)
            if (id == Prefs.defaultProviderId) return@ModelPickRow
            Prefs.defaultProviderId = id
            scope.launch { runCatching { Api.setMyDefaultProvider(id) } }
            onModelsChanged()
            onTick()
        }
        SetRow("⚙", "模型服务", divider = false, valSlot = { SetValText(chatNote) }) {
            onOpenPage("providers")
        }
    }

    // —— 对话 ——
    SetGroup("对话")
    SetCard {
        SetRow("✎", "角色设定", valSlot = {
            identityTick
            SetValText(if (Prefs.persona(Prefs.lastSessionId).trim().isNotEmpty()) "已填写" else "")
        }) { onOpenPage("persona") }
        CtxRow(cap, providersKnown = providers.isNotEmpty())
        SetRow("↓", "导出本次对话", divider = false) {
            // v0.23 T2.4：逐条对齐网页 exportCurrent（app.js:2672）——空会话守卫 →
            // 签票 → 兑换。网页把 location.href 指过去、文件名由 Content-Disposition
            // 说话；原生没有这层浏览器，文件名自己按同源规则算（ExportName），
            // 字节流交给 DownloadManager 落系统 Downloads，完成通知是系统下载队列的。
            // 曾经这里是 onOpenUrl(票据)：把一次性兑换权递给外部浏览器，既拿不到
            // 落盘通知也多一个能看见 URL 的人，按 R2-AC-3 判负，别再改回去。
            //
            // v0.23 T2.5 补上三条异常路径（R2-AC-2/3）：空会话文案沿用 T2.4（与网页
            // 逐字同值）；≤28 写公共 Downloads 先要旧存储权限，被拒改走 App 私有目录
            // 并把完整路径念给人听；签票/取标题阶段的断网给专属文案，不是"未知错误"。
            // 票据过期（DownloadManager 侧的秒失败/迟到失败）由 ExportTracker 轮询
            // 识别后自动重签一次，仍失败才发通知报错。
            if (!canExport) { onNote("当前没有可导出的对话", true); return@SetRow }
            val sid = Prefs.lastSessionId
            if (!ExportTracker.hasLegacyStorage(ctx)) {
                pendingSid = sid
                storageAsk.launch(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
                return@SetRow
            }
            startExport(sid, viaPrivate = false)
        }
    }

    // —— 记忆 ——
    SetGroup("记忆")
    SetCard {
        SetRow("◈", "长期记忆", divider = false, valSlot = { SetValText(memoryCount) }) {
            onOpenPage("memory")
        }
    }

    // —— 日程 ——
    SetGroup("日程")
    SetCard {
        SetRow("▤", "日程", divider = false, valSlot = { SetValText(scheduleVal) }) {
            onOpenPage("schedule")
        }
    }

    // —— 设备 ——
    SetGroup("设备")
    SetCard {
        SetRow("⚑", "提醒", divider = false, valSlot = {
            val n = ReminderStore.list().size
            SetValText(if (n > 0) "$n 条" else "")
        }) { onOpenPage("reminders") }
    }

    // —— 通用 ——（借鉴 WorkBuddy 的「触感反馈」「消息通知设置」与 Qoder 的「清理缓存」：
    //    通知行值槽直读系统授权状态、点下去进系统那一页；缓存行报出大小、清完重量）
    SetGroup("通用")
    SetCard {
        SetRow("✦", "触感反馈", trailing = "", valSlot = {
            SetSwitch(hapticsOn) { v -> hapticsOn = v; Prefs.hapticsEnabled = v }
        }) { hapticsOn = !hapticsOn; Prefs.hapticsEnabled = hapticsOn }
        SetRow("⊙", "消息通知设置", trailing = "›", valSlot = {
            SetValText(if (notifGranted) "已开启" else "已关闭")
        }) {
            if (!openSystemNotificationSettings(appCtx))
                onNote("打不开系统那一页，请到系统设置里搜「Fenver」", true)
        }
        SetRow("◫", "清理缓存", divider = false, trailing = "›", valSlot = {
            SetValText(if (cacheBytes < 0) "计算中…" else fmtBytes(cacheBytes))
        }) { confirmClearCache = true }
    }

    // —— 关于（renderAboutRows）——
    SetGroup("关于")
    SetNote(ABOUT_TAGLINE)
    SetCard {
        val shellVer = "v" + BuildConfig.VERSION_NAME
        val versionVal = listOfNotNull(
            "壳 $shellVer",
            "服务端 $serverBuild".takeIf { serverBuild.isNotEmpty() },
        ).joinToString(" · ")
        SetRow("ⓘ", "版本", plain = true, trailing = "",
            valSlot = { SetValText(versionVal) })
        // 网页版这行会新开浏览器去 GitHub；原生按用户要求改成应用内检查：
        // v0.23 T1.8 起问的是自家后端 /v1/update/info（判据全在共享 ReleasePlan，
        // 三态不许滑成"已是最新"），发现新版时值槽挂「立即更新」——全程不跳外部。
        SetRow("↻", "检查更新", trailing = "", valSlot = {
            val d = Updater.decision
            val available = d != null && d.kind == ReleasePlan.Kind.AVAILABLE
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                SetValText(when {
                    Updater.checking -> "正在检查…"
                    Updater.downloading -> "下载中 ${Updater.percent}%"
                    available -> "发现新版本 ${d!!.version}"
                    d != null && d.kind == ReleasePlan.Kind.UP_TO_DATE ->
                        "已是最新版本 v${d.version}"
                    d != null -> "检查更新失败：${d.reason}"
                    else -> "$shellVer · 点按检查"
                })
                if (available && !Updater.checking && !Updater.downloading) {
                    SetMiniPrimary("立即更新") {
                        Updater.startDownload(ctx.applicationContext) { err ->
                            err?.let { onNote("更新失败：$it", true) }
                        }
                    }
                }
            }
        }) {
            if (Updater.checking) return@SetRow
            Updater.startCheck()
        }
        // 「外观」从两态点切升级成三态分段（借鉴 WorkBuddy 的主题行）：多了「系统」。
        // 行名仍叫「外观」不叫「主题」——双端「关于」组的同文案合同钉着这两个字。
        SetRow("☾", "外观", trailing = "", valSlot = { ThemeSegment() })
        // 上面那颗「检查更新」全程不跳外部，下面那行「开源仓库」却是要把人送到 GitHub
        // 上去的——它问的是"这个项目本身在哪、能不能拿去看"，↗ 后缀（网页 .set-go
        // 同一条）就是"点下去会离开本应用"的意思，别换成 ›。
        // 「意见反馈」v0.27 起不再跳 GitHub：改成应用内一页（› 前缀），填完直达
        // 管理员的「反馈」收集页，每条带回独立编号。
        SetRow("✉", "意见反馈", trailing = "›", valSlot = { SetValText("提交问题与建议") }) {
            onOpenPage("feedback")
        }
        SetRow("↗", "开源仓库", trailing = "↗", valSlot = { SetValText(ABOUT_REPO_HOST) }) {
            onOpenUrl(ABOUT_REPO_URL)
        }
        SetRow("©", "许可证", plain = true, trailing = "",
            valSlot = { SetValText(ABOUT_LICENSE) })
        SetRow("◉", "服务地址", plain = true, trailing = "", divider = false, valSlot = {
            SetValText(hostOf(Prefs.baseUrl))
        })
    }

    // —— 退出（.set-last + .set-danger）——
    Spacer(Modifier.height(18.dp))
    SetCard {
        SetRow("→", "退出登录", danger = true, trailing = "", divider = false) {
            val hit = Prefs.currentEntry() ?: run { onLoggedOut(); return@SetRow }
            scope.launch {
                runCatching { Api.logout(hit.token) }   // 服务端不作数也得把本机忘掉
                Prefs.forgetCurrent()
                onLoggedOut()
            }
        }
    }

    // —— 清理缓存（Qoder「清理缓存」的对位）：只动 cacheDir/externalCacheDir 的内容，
    //    目录本身留给系统；会话、设置、登录态都不住在这里。IO 线程算字节 + 删。
    if (confirmClearCache) {
        AlertDialog(onDismissRequest = { confirmClearCache = false },
            title = { Text("清理缓存？") },
            text = { Text("只删除临时文件，不影响对话、设置和登录状态。") },
            confirmButton = {
                TextButton(onClick = {
                    confirmClearCache = false
                    scope.launch {
                        cacheBytes = withContext(Dispatchers.IO) {
                            runCatching {
                                listOfNotNull(appCtx.cacheDir, appCtx.externalCacheDir)
                                    .forEach { d -> d.listFiles()?.forEach { it.deleteRecursively() } }
                            }
                            cacheBytesNow()
                        }
                    }
                }) { Text("清理") }
            },
            dismissButton = { TextButton(onClick = { confirmClearCache = false }) { Text("取消") } })
    }

    // —— 应用内更新（T1.7）：进度对话框 + 「安装未知应用」授权引导 ——
    // 状态在 Updater 上，弹层被划掉再进来时这两个对话框会原样挂回来（下载不丢）。
    if (Updater.downloading) {
        UpdateProgressDialog(onCancel = { Updater.cancelDownload() })
    }
    if (Updater.installGuideVersion != null) {
        InstallGuideDialog(
            onGoSettings = { Updater.openInstallPermissionSettings(ctx.applicationContext) },
            onDismiss = { Updater.dismissInstallGuide() })
    }
}

/* serverDefault 的口径在网页是模块变量；这一处从 /v1/models 盖章的 default 字段重取。 */
private fun serverDefaultOf(providers: List<ModelInfo>): String? =
    providers.firstOrNull { it.default }?.id

/* 当前模型行：点开是一张只列模型的弹单（网页 <select> 的原生对位）。
   不可用的条目灰着、括号里带 reason，点它不生效（网页 disabled 选项选不中）。 */
@Composable
private fun ModelPickRow(providers: List<ModelInfo>, current: ModelInfo?,
                         tick: Int, loading: Boolean,
                         onPick: (String, ModelInfo) -> Unit) {
    var open by remember { mutableStateOf(false) }
    tick
    SetRow("◆", "当前模型", trailing = "⌄", valSlot = {
        SetValText(current?.name ?: if (loading) "读取中…" else "未配置模型")
    }) { open = true }
    if (open) {
        AlertDialog(onDismissRequest = { open = false },
            confirmButton = { TextButton(onClick = { open = false }) { Text("关闭") } },
            title = { Text("当前模型", fontSize = 17.sp) },
            text = {
                Column {
                    if (providers.isEmpty()) Text(if (loading) "正在读取模型清单…" else "未配置模型",
                        fontSize = 14.sp,
                        color = text3Color())
                    providers.forEach { p ->
                        val label = if (p.usable) {
                            if (p.supports_vision) "${p.name} · 支持图片" else p.name
                        } else "${p.name}（${p.reason.ifBlank { "不可用" }}）"
                        val on = p.id == (current?.id ?: Prefs.defaultProviderId)
                        Text(label, fontSize = if (p.usable) 15.sp else 13.sp,
                            color = when {
                                p.usable && on -> MaterialTheme.colorScheme.primary
                                p.usable -> MaterialTheme.colorScheme.onSurface
                                else -> text3Color()
                            },
                            fontWeight = if (on && p.usable) FontWeight.SemiBold else FontWeight.Normal,
                            modifier = Modifier.fillMaxWidth()
                                .clip(RoundedCornerShape(8.dp))
                                .then(if (p.usable) Modifier.clickable {
                                    open = false; onPick(p.id, p)
                                } else Modifier)
                                .padding(horizontal = 10.dp, vertical = 9.dp))
                    }
                }
            },
            containerColor = MaterialTheme.colorScheme.surface)
    }
}

/* 上下文长度行（网页 #ctxRange + 「8/64k」）：滑杆量程=当前模型上限，
   步长 cap≤64→2 / ≤256→8 / 其余 32；清单在手才把超限的旧预算收敛（syncCtxRow 原注释）。 */
@Composable
private fun CtxRow(cap: Int, providersKnown: Boolean) {
    var k by remember(cap) { mutableIntStateOf(Prefs.contextTokensK.coerceIn(2, cap)) }
    LaunchedEffect(cap, providersKnown) {
        val v = if (providersKnown) k.coerceAtMost(cap) else k
        if (v != k) k = v
        if (Prefs.contextTokensK != v) Prefs.contextTokensK = v
    }
    val step = if (cap <= 64) 2 else if (cap <= 256) 8 else 32
    Row(Modifier.fillMaxWidth().heightIn(min = 54.dp).padding(horizontal = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("≡", fontSize = 16.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.width(20.dp), textAlign = TextAlign.Center)
        Text("上下文长度", fontSize = 15.sp, color = MaterialTheme.colorScheme.onSurface,
            maxLines = 1, modifier = Modifier.weight(1f))
        CtxValue(k, cap)
        Slider(modifier = Modifier.width(110.dp),
            value = k.toFloat(),
            onValueChange = { v ->
                val raw = ((v - 2) / step).toInt() * step + 2
                k = raw.coerceIn(2, cap)
            },
            valueRange = 2f..cap.toFloat(),
            onValueChangeFinished = { Prefs.contextTokensK = k })
    }
    Box(Modifier.fillMaxWidth().padding(horizontal = 14.dp).height(1.dp)
        .background(MaterialTheme.colorScheme.outline))
}

/** 「8/64k」：加粗那截吃强调色（网页 <b>ctxVal</b>/ctxCap k）。 */
@Composable
private fun CtxValue(k: Int, cap: Int) {
    val styled = buildAnnotatedString {
        withStyle(SpanStyle(color = MaterialTheme.colorScheme.primary,
            fontWeight = FontWeight.SemiBold)) { append(k.toString()) }
        withStyle(SpanStyle(color = text3Color())) { append("/$cap" + "k") }
    }
    Text(styled, fontSize = 14.sp, maxLines = 1)
}

private fun hostOf(url: String): String = runCatching {
    val u = Uri.parse(url)
    val h = u.host ?: ""
    val def = when (u.scheme) { "https" -> 443; "http" -> 80; else -> -1 }
    if (u.port > 0 && u.port != def) "$h:${u.port}" else h
}.getOrDefault("")

/** 缓存大小的读法（借鉴 Qoder 的「清理缓存」值槽）：B / KB / 一位小数 MB。 */
private fun fmtBytes(b: Long): String = when {
    b >= 1L shl 20 -> "%.1f MB".format(b / 1048576.0)
    b >= 1L shl 10 -> "%d KB".format(b / 1024)
    else -> "$b B"
}

/** 跳到系统「通知设置」这一页（与提醒页「去设置」同一枚 intent）。 */
private fun openSystemNotificationSettings(ctx: android.content.Context): Boolean = runCatching {
    if (Build.VERSION.SDK_INT >= 26) {
        ctx.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
            .putExtra(Settings.EXTRA_APP_PACKAGE, ctx.packageName)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    } else {
        ctx.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
            .setData(Uri.parse("package:" + ctx.packageName))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }
}.isSuccess

/* v0.23 T1.8 起这里不再有"顺手的一份版本比较"（原 isNewerVersion 已删）：
 * 版号判"有没有更新"的真相只有共享的 ReleasePlan.compare 一份——
 * 跨语言第二实现一定漂，漂的样子不是报错，是有的设备劝人下载有的不劝。 */

/* ---------------- 账户页（renderAccounts 逐行） ---------------- */
@Composable
private fun AccountsPage(tick: Int, onTick: () -> Unit,
                         onRequireAuth: (String) -> Unit, onSwitched: () -> Unit) {
    tick
    val here = Prefs.currentEntry()?.userId ?: ""
    val list = Prefs.readIdentities().sortedByDescending { it.addedAt }
    Spacer(Modifier.height(12.dp))
    SetCard {
        list.forEachIndexed { i, x ->
            val isHere = x.userId == here
            Row(Modifier.fillMaxWidth().heightIn(min = 54.dp)
                .padding(horizontal = 14.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (isHere) Box(Modifier.width(3.dp).height(22.dp)
                    .background(MaterialTheme.colorScheme.primary, CircleShape))
                Text(x.username.ifBlank { x.userId }, fontSize = 15.sp,
                    color = MaterialTheme.colorScheme.onSurface, maxLines = 1,
                    overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
                if (isHere) SetValText("当前")
                else {
                    SetMini("切换账号") {
                        if (Prefs.switchTo(x.userId)) onSwitched()
                    }
                    SetMini("删除账号", del = true) {
                        // 方案 C 的既成代价照抄：删别人只忘本机
                        Prefs.dropIdentity(x.userId)
                        onTick()
                    }
                }
            }
            if (i != list.size - 1) {
                Box(Modifier.fillMaxWidth().padding(horizontal = 14.dp).height(1.dp)
                    .background(MaterialTheme.colorScheme.outline))
            }
        }
    }
    val me = Prefs.currentEntry()
    Spacer(Modifier.height(12.dp))
    // .pane-note whoInfo：当前身份那一行
    Text(if (me == null) "未登录"
    else "当前身份：${me.username.ifBlank { me.userId }}" +
            "（${if (me.role == "admin") "管理员" else "普通用户"}）",
        fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(bottom = 14.dp))
    SetMini("添加账户") { onRequireAuth("register") }
}

/* ---------------- 角色设定页（personaInput + 保存/清除） ---------------- */
@Composable
private fun PersonaPage(onNote: (String) -> Unit, onClose: () -> Unit) {
    var text by remember { mutableStateOf(Prefs.persona(Prefs.lastSessionId)) }
    Spacer(Modifier.height(12.dp))
    Box(Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp))
        .background(MaterialTheme.colorScheme.surfaceVariant)
        .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(10.dp))) {
        BasicTextField(text, { text = it },
            textStyle = TextStyle(fontSize = 14.sp,
                color = MaterialTheme.colorScheme.onSurface),
            cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
            modifier = Modifier.fillMaxWidth().heightIn(min = 110.dp)
                .padding(horizontal = 12.dp, vertical = 10.dp),
            decorationBox = { inner ->
                Box {
                    if (text.isEmpty()) Text(
                        "例如：你是一位严谨的网络安全助教，回答给出依据与命令示例。",
                        fontSize = 14.sp, color = text3Color())
                    inner()
                }
            })
    }
    Spacer(Modifier.height(10.dp))
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        SetMiniPrimary("保存") {
            Prefs.setPersona(Prefs.lastSessionId, text.trim())
            onClose()   // 网页保存后直接收整层设置
        }
        SetMini("清除") {
            Prefs.setPersona(Prefs.lastSessionId, "")
            text = ""
            onNote("角色设定已清除")
        }
    }
}

/* ---------------- 意见反馈页（v0.27：应用内提交 → 管理员「反馈」收集页） ---------------- */

/** 一张已选中的反馈截图：Uri 留着上传时读字节，Bitmap 现在就解码好画缩略图。 */
private data class FbImg(val uri: Uri, val bmp: Bitmap)

/** 与后端 user_feedback_storage 同一组边界：正文 300、图片 4 张。
 *  这里挡的是"手滑超长"，真正的硬校验在服务端（界面从来不是边界）。 */
private const val FB_TEXT_MAX = 300
private const val FB_IMAGE_MAX = 4

@Composable
private fun FeedbackPage(onNote: (String, Boolean) -> Unit, onClose: () -> Unit) {
    val scope = rememberCoroutineScope()
    val ctx = LocalContext.current
    val haptic = rememberSetHaptic()

    var text by remember { mutableStateOf("") }
    var email by remember { mutableStateOf("") }
    var imgs by remember { mutableStateOf(listOf<FbImg>()) }
    var submitting by remember { mutableStateOf(false) }
    var doneId by remember { mutableStateOf<String?>(null) }   // 成功回执里的独立编号

    // 一次选多张：系统图片选择器（Photo Picker，activity-compose 1.9 起可用）。
    // maxItems 建时给满 4；合进来时再按剩余额度截断——已选 N 张就只补到 4。
    val picker = rememberLauncherForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.PickMultipleVisualMedia(FB_IMAGE_MAX)
    ) { uris ->
        if (uris.isEmpty()) return@rememberLauncherForActivityResult
        val room = FB_IMAGE_MAX - imgs.size
        val taken = uris.take(room)
        val decoded = ArrayList<FbImg>(taken.size)
        for (u in taken) {
            val b = ctx.contentResolver.openInputStream(u)?.use {
                BitmapFactory.decodeStream(it)
            }
            if (b != null) decoded.add(FbImg(u, b))   // 解不开的（非图/损坏）静默跳过
        }
        if (decoded.isEmpty()) onNote("这几张图打不开，换一张试试", true)
        else imgs = imgs + decoded
    }

    fun submit() {
        val body = text.trim()
        if (body.isEmpty()) { onNote("先说说你遇到的问题或建议", true); return }
        if (submitting) return
        submitting = true
        scope.launch {
            runCatching {
                // 先图后信：每张走 /v1/uploads 换成 upload id，再连同正文提交。
                val ids = ArrayList<String>(imgs.size)
                for (f in imgs) {
                    val bytes = ctx.contentResolver.openInputStream(f.uri)?.use { it.readBytes() }
                        ?: throw Exception("读取图片失败")
                    var name = "image.jpg"
                    ctx.contentResolver.query(f.uri,
                        arrayOf(android.provider.OpenableColumns.DISPLAY_NAME), null, null, null)
                        ?.use { c -> if (c.moveToFirst()) name = c.getString(0) ?: name }
                    val mime = ctx.contentResolver.getType(f.uri) ?: "image/jpeg"
                    ids.add(Api.upload(bytes, name, mime).id)
                }
                Api.submitUserFeedback(body, email, ids)
            }.onSuccess { r ->
                submitting = false
                doneId = r.id
            }.onFailure { e ->
                submitting = false
                onNote(when {
                    e is ApiException && e.status == 401 ->
                        "登录状态失效：请到「设置 → 账户」重新登录后再提交"
                    else -> "提交失败：" + (e.message ?: "未知错误")
                }, true)
            }
        }
    }

    Spacer(Modifier.height(12.dp))
    SetNote("把遇到的问题或对 Fenver 的建议写在这里，提交后会直达管理员的反馈收件箱。")

    // —— 正文（多行 + 右下角 0/300 计数，贴着截图里那张输入框） ——
    Box(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp))
        .background(MaterialTheme.colorScheme.surfaceVariant)
        .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(12.dp))) {
        BasicTextField(text, { v -> text = v.take(FB_TEXT_MAX) },
            textStyle = TextStyle(fontSize = 14.sp,
                color = MaterialTheme.colorScheme.onSurface),
            cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
            modifier = Modifier.fillMaxWidth().heightIn(min = 120.dp)
                .padding(start = 12.dp, end = 12.dp, top = 12.dp, bottom = 30.dp),
            decorationBox = { inner ->
                Box(Modifier.fillMaxWidth()) {
                    if (text.isEmpty()) Text(
                        "请描述你遇到的问题和对 Fenver 的建议",
                        fontSize = 14.sp, color = text3Color(),
                        modifier = Modifier.padding(end = 8.dp))
                    inner()
                    // 计数固定在右下角，随输入实时变
                    Text("${text.length}/$FB_TEXT_MAX", fontSize = 12.sp,
                        color = text3Color(),
                        modifier = Modifier.align(Alignment.BottomEnd)
                            .padding(horizontal = 4.dp))
                }
            })
    }

    Spacer(Modifier.height(12.dp))

    // —— 图片（0/4）：缩略图网格 + 每张可删 + 末尾一张"+ 添加"占位 ——
    Row(verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        modifier = Modifier.padding(bottom = 6.dp)) {
        Text("截图", fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text("${imgs.size}/$FB_IMAGE_MAX", fontSize = 12.sp, color = text3Color())
    }
    Row(Modifier.fillMaxWidth().heightIn(min = 72.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        imgs.forEachIndexed { i, f ->
            Box(Modifier.size(72.dp).clip(RoundedCornerShape(10.dp))
                .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(10.dp))) {
                Image(f.bmp.asImageBitmap(), contentDescription = null,
                    modifier = Modifier.matchParentSize())
                // 右上角 × 删掉这一张（不删服务端附件，只是不带上）
                Text("×", fontSize = 14.sp, color = Color.White,
                    modifier = Modifier.align(Alignment.TopEnd)
                        .padding(2.dp).size(18.dp).clip(CircleShape)
                        .background(Color(0x99000000), CircleShape)
                        .clickable {
                            haptic()
                            imgs = imgs.filterIndexed { j, _ -> j != i }
                        },
                    textAlign = TextAlign.Center)
            }
        }
        if (imgs.size < FB_IMAGE_MAX) {
            Box(Modifier.size(72.dp).clip(RoundedCornerShape(10.dp))
                .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(10.dp))
                .background(MaterialTheme.colorScheme.surfaceVariant)
                .clickable {
                    haptic()
                    picker.launch(
                        androidx.activity.result.PickVisualMediaRequest(
                            androidx.activity.result.contract.ActivityResultContracts
                                .PickVisualMedia.ImageOnly)
                    )
                },
                contentAlignment = Alignment.Center) {
                Text("＋", fontSize = 24.sp, color = text3Color())
            }
        }
    }

    Spacer(Modifier.height(14.dp))

    // —— 邮箱（选填，方便管理员回复你）——
    FormRow("邮箱（选填，方便回复你）", email,
        { v -> email = v.take(120) }, btn = "")

    Spacer(Modifier.height(16.dp))

    // —— 提交（整宽主按钮，对应截图那颗蓝色"提交"）——
    val scheme = MaterialTheme.colorScheme
    val light = isWebLight()
    val bg = if (light) scheme.primary else Color(0xFF062518)
    val fg = if (light) Color.White else Color(0xFFBFF3D9)
    val canSubmit = !submitting && text.trim().isNotEmpty()
    Box(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp))
        .background(if (canSubmit) bg else scheme.surfaceVariant)
        .border(1.dp, if (canSubmit) scheme.primary else scheme.outline, RoundedCornerShape(12.dp))
        .clickable(enabled = canSubmit) { submit() }
        .padding(vertical = 13.dp),
        contentAlignment = Alignment.Center) {
        Text(if (submitting) "提交中…" else "提交",
            fontSize = 15.sp, fontWeight = FontWeight.SemiBold,
            color = if (canSubmit) fg else text3Color())
    }

    // —— 成功回执：把这条反馈的独立编号念给用户，让他知道"发出去了、有据可查" ——
    doneId?.let { id ->
        AlertDialog(onDismissRequest = { doneId = null; onClose() },
            title = { Text("反馈已提交") },
            text = { Text("你的反馈编号是 $id，管理员已能在收件箱看到它。\n" +
                "保留这个编号，方便日后追问对应的是哪一条。") },
            confirmButton = {
                TextButton(onClick = {
                    doneId = null
                    text = ""; email = ""; imgs = emptyList()
                    onClose()
                }) { Text("好的") }
            })
    }
}

/* ---------------- 长期记忆页（loadMemories + 两个表单） ---------------- */
@Composable
private fun MemoryPage(onCountChanged: (String) -> Unit, onNote: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    var query by remember { mutableStateOf("") }      // state.memoryQuery（已提交）
    var qInput by remember { mutableStateOf("") }
    var addInput by remember { mutableStateOf("") }
    var items by remember { mutableStateOf(listOf<JsonObject>()) }
    var loading by remember { mutableStateOf(true) }
    var err by remember { mutableStateOf("") }

    fun reload() {
        scope.launch {
            loading = true; err = ""
            try {
                val data: JsonElement = if (query.isBlank()) Api.listMemory(50)
                else Api.searchMemory(query, 20)
                val arr = (data as? JsonObject)?.let {
                    (it["memories"] ?: it["results"]) as? JsonArray
                } ?: JsonArray(emptyList())
                items = arr.mapNotNull { it as? JsonObject }
                if (query.isBlank()) {
                    onCountChanged(if (items.size >= 50) "50+ 条" else "${items.size} 条")
                }
            } catch (e: Exception) {
                err = memoryListErrorText(e)
            }
            loading = false
        }
    }
    LaunchedEffect(Unit) { reload() }

    Spacer(Modifier.height(12.dp))
    // .row #memoryAddForm（添加在前，搜索在后——index.html 就这个序）
    FormRow("让助手记住一件事，例如：我叫张三", addInput, { addInput = it }, "添加") {
        val t = addInput.trim()
        if (t.isEmpty()) return@FormRow
        scope.launch {
            runCatching { Api.addMemory(t) }
                .onSuccess { addInput = ""; reload() }
                .onFailure { onNote("添加记忆失败：" + (it.message ?: "")) }
        }
    }
    FormRow("语义搜索记忆…", qInput, { qInput = it }, "搜索") {
        query = qInput.trim(); reload()
    }
    if (err.isNotEmpty()) {
        Text(err, fontSize = 13.sp, color = MaterialTheme.colorScheme.error,
            modifier = Modifier.padding(vertical = 6.dp))
    }
    if (!loading && items.isEmpty() && err.isEmpty()) {
        Text(if (query.isNotBlank()) "没有匹配的记忆" else "还没有记忆",
            fontSize = 14.sp, color = text3Color(),
            modifier = Modifier.padding(vertical = 8.dp))
    }
    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        items.forEach { m ->
            val content = m["content"]?.jsonPrimitive?.contentOrNull ?: ""
            val wEl = m["weight"] ?: (m["metadata"] as? JsonObject)?.get("weight")
            val weight = (wEl as? JsonPrimitive)?.content?.toDoubleOrNull() ?: 1.0
            val id = m["id"]?.jsonPrimitive?.contentOrNull
                ?: m["memory_id"]?.jsonPrimitive?.contentOrNull
            MemRow(weight, content, onDelete = {
                if (id == null) { onNote("这条记忆没有 id，无法删除"); return@MemRow }
                scope.launch {
                    runCatching { Api.deleteMemory(listOf(id)) }
                        .onSuccess { reload() }
                        .onFailure { onNote("删除失败：" + (it.message ?: "")) }
                }
            })
        }
    }
}

/** .mem-list li：weight 药丸（.w）+ 正文 + ×。 */
@Composable
private fun MemRow(weight: Double, content: String, onDelete: () -> Unit) {
    Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp))
        .background(MaterialTheme.colorScheme.surfaceVariant)
        .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(10.dp))
        .padding(horizontal = 11.dp, vertical = 9.dp),
        verticalAlignment = Alignment.Top,
        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Box(Modifier.clip(CircleShape)
            .border(1.dp, MaterialTheme.colorScheme.outline, CircleShape)
            .background(MaterialTheme.colorScheme.surface)
            .padding(horizontal = 7.dp, vertical = 1.dp)) {
            Text("%.2f".format(weight), fontSize = 11.sp, color = text3Color())
        }
        Text(content, fontSize = 14.sp, color = MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.weight(1f))
        Text("×", fontSize = 15.sp, color = text3Color(),
            modifier = Modifier.clip(CircleShape).clickable(onClick = onDelete)
                .padding(horizontal = 4.dp))
    }
}

/** .row：输入 + 按钮（记忆添加/搜索/令牌三个表单同一条积木；btn 空串 = 只有输入）。 */
@Composable
private fun FormRow(placeholder: String, value: String, onValue: (String) -> Unit,
                    btn: String = "保存", onSubmit: () -> Unit = {}) {
    Row(Modifier.fillMaxWidth().padding(vertical = 5.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Box(Modifier.weight(1f).clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(10.dp))) {
            BasicTextField(value, onValue, singleLine = true,
                textStyle = TextStyle(fontSize = 14.sp,
                    color = MaterialTheme.colorScheme.onSurface),
                cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                modifier = Modifier.fillMaxWidth(),
                decorationBox = { inner ->
                    Box(Modifier.padding(horizontal = 12.dp, vertical = 9.dp)) {
                        if (value.isEmpty()) Text(placeholder, fontSize = 14.sp,
                            color = text3Color())
                        inner()
                    }
                })
        }
        if (btn.isNotEmpty()) SetMiniPrimary(btn) { onSubmit() }
    }
}

/** 网页 memoryListErrorText 的三句逐字。 */
private fun memoryListErrorText(e: Throwable): String {
    if (e is ApiException) {
        if (e.status == 401) return "未登录或令牌已失效：请在「设置 → 账户」重新登录"
        if (e.status == 403) return "这个账号没有读取记忆的权限，请找管理员确认"
    }
    return "记忆服务不可用：" + (e.message ?: "")
}

/* ---------------- 模型服务页（loadProviders + providerRow + provForm） ---------------- */

private data class ProvRow(val id: String, val label: String, val baseUrl: String,
                           val model: String, val keyMasked: String, val hasKey: Boolean,
                           val vision: Boolean, val isDefault: Boolean, val ctxK: Int)

private fun parseProv(el: JsonElement?): ProvRow? {
    val o = el as? JsonObject ?: return null
    fun str(k: String) = o[k]?.jsonPrimitive?.contentOrNull ?: ""
    return ProvRow(id = str("id"), label = str("label"), baseUrl = str("base_url"),
        model = str("model"), keyMasked = str("api_key_masked"),
        hasKey = o["has_key"]?.jsonPrimitive?.booleanOrNull ?: false,
        vision = o["supports_vision"]?.jsonPrimitive?.booleanOrNull ?: false,
        isDefault = o["is_default"]?.jsonPrimitive?.booleanOrNull ?: false,
        ctxK = o["max_context_k"]?.jsonPrimitive?.intOrNull ?: 0)
}

private data class Preset(val label: String, val baseUrl: String, val model: String,
                          val ctxK: Int?, val vision: Boolean)

@Composable
private fun ProvidersPage(onChanged: () -> Unit, onAdopted: () -> Unit = onChanged) {
    val scope = rememberCoroutineScope()
    val admin = Prefs.role == "admin"
    var shared by remember { mutableStateOf(listOf<ProvRow>()) }
    var mine by remember { mutableStateOf(listOf<ProvRow>()) }
    var myDefault by remember { mutableStateOf<String?>(null) }
    var presets by remember { mutableStateOf(listOf<Preset>()) }
    var err by remember { mutableStateOf("") }
    // 表单：editingId 空串 → 新建；formScope="admin"/"mine" 决定走哪面
    var formOpen by remember { mutableStateOf(false) }
    var editingId by remember { mutableStateOf("") }
    var formScope by remember { mutableStateOf("mine") }
    var fLabel by remember { mutableStateOf("") }
    var fBase by remember { mutableStateOf("") }
    var fKey by remember { mutableStateOf("") }
    var fModel by remember { mutableStateOf("") }
    var fCtxK by remember { mutableStateOf("") }
    var fVision by remember { mutableStateOf(false) }
    var keyPh by remember { mutableStateOf("填入 API Key") }
    var result by remember { mutableStateOf("") }
    var confirmDel by remember { mutableStateOf<ProvRow?>(null) }
    var delScope by remember { mutableStateOf("mine") }
    var tokenInput by remember { mutableStateOf("") }
    var tokenBusy by remember { mutableStateOf(false) }

    fun reload() {
        scope.launch {
            err = ""
            runCatching { Api.myProviders() }.onSuccess { data ->
                shared = ((data["shared"] as? JsonArray) ?: JsonArray(emptyList()))
                    .mapNotNull(::parseProv)
                mine = ((data["mine"] as? JsonArray) ?: JsonArray(emptyList()))
                    .mapNotNull(::parseProv)
                myDefault = data["default"]?.jsonPrimitive?.contentOrNull
                presets = buildList {
                    (data["presets"] as? JsonObject)?.forEach { (_, v) ->
                        val o = v as? JsonObject ?: return@forEach
                        val po = (o["preset"] as? JsonObject) ?: o   // 两种形状都收
                        add(Preset(
                            label = po["label"]?.jsonPrimitive?.contentOrNull ?: "",
                            baseUrl = po["base_url"]?.jsonPrimitive?.contentOrNull ?: "",
                            model = po["model"]?.jsonPrimitive?.contentOrNull ?: "",
                            ctxK = po["max_context_k"]?.jsonPrimitive?.intOrNull,
                            vision = po["supports_vision"]?.jsonPrimitive?.booleanOrNull
                                ?: false))
                    }
                }
            }.onFailure {
                err = "模型服务没拉到：" + (it.message ?: "")
            }
        }
    }
    LaunchedEffect(Unit) { reload() }

    fun draft(): JsonObject = buildJsonObject {
        if (editingId.isNotEmpty()) put("id", editingId)
        put("label", fLabel.trim()); put("base_url", fBase.trim())
        put("api_key", fKey.trim()); put("model", fModel.trim())
        fCtxK.trim().toIntOrNull()?.let { if (it > 0) put("max_context_k", it) }
        put("supports_vision", fVision); put("is_default", false)
    }
    fun testBox(t: String) { result = t }
    fun openForm(p: ProvRow?, scopeName: String) {
        editingId = p?.id ?: ""
        formScope = scopeName
        fLabel = p?.label ?: ""; fBase = p?.baseUrl ?: ""
        fModel = p?.model ?: ""
        fCtxK = if (p != null && p.ctxK > 0) p.ctxK.toString() else ""
        fVision = p?.vision ?: false
        fKey = ""
        keyPh = if (p != null && p.keyMasked.isNotEmpty())
            "已设置（${p.keyMasked}），留空则不修改" else "填入 API Key"
        result = ""; formOpen = true
    }
    fun runTest(id: String, adminFirst: Boolean) {
        scope.launch {
            testBox("测试中…")
            runCatching {
                if (adminFirst) Api.testProvider(id) else Api.testMyProvider(id)
            }.onSuccess { r ->
                val ok = r["ok"]?.jsonPrimitive?.booleanOrNull ?: false
                testBox((if (ok) "✅ " else "❌ ") +
                    (r["detail"]?.jsonPrimitive?.contentOrNull ?: ""))
            }.onFailure { testBox("❌ " + (it.message ?: "")) }
        }
    }
    fun setMyDefault(id: String) {
        scope.launch {
            runCatching { Api.setMyDefaultProvider(id) }
            Prefs.defaultProviderId = id
            reload(); onChanged()
        }
    }

    Spacer(Modifier.height(12.dp))
    if (err.isNotEmpty()) {
        Text(err, fontSize = 13.sp, color = MaterialTheme.colorScheme.error,
            modifier = Modifier.padding(bottom = 12.dp))
    }
    // —— 共享模型（管理员维护）——
    Text("共享模型（管理员维护）", fontSize = 12.5.sp, color = text3Color(),
        modifier = Modifier.padding(bottom = 8.dp))
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        shared.forEach { p ->
            ProvRowCard(p, scope = if (admin) "admin" else "shared",
                myDefault = myDefault,
                onEdit = { openForm(p, "admin") },
                onTest = { id -> runTest(id, admin) },
                onMyDefault = { id -> setMyDefault(id) },
                onSiteDefault = if (admin) ({ id ->
                    scope.launch {
                        runCatching { Api.setDefaultProvider(id) }
                        reload(); onChanged()
                    }
                }) else null,
                onDelete = { row -> confirmDel = row; delScope = "admin" })
        }
    }
    if (admin) {
        Spacer(Modifier.height(8.dp))
        SetMiniPrimary("＋ 添加共享模型服务") { openForm(null, "admin") }
    }
    // —— 我的模型 ——
    Text("我的模型（自带 API Key）", fontSize = 12.5.sp, color = text3Color(),
        modifier = Modifier.padding(top = 22.dp, bottom = 8.dp))
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        mine.forEach { p ->
            ProvRowCard(p, scope = "mine", myDefault = myDefault,
                onEdit = { openForm(p, "mine") },
                onTest = { id -> runTest(id, false) },
                onMyDefault = { id -> setMyDefault(id) },
                onSiteDefault = null,
                onDelete = { row -> confirmDel = row; delScope = "mine" })
        }
    }
    Spacer(Modifier.height(8.dp))
    SetMiniPrimary("＋ 添加我的模型") { openForm(null, "mine") }

    // —— 表单（.prov-form：虚线框，preset 胶囊行在最上）——
    if (formOpen) {
        Spacer(Modifier.height(12.dp))
        Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp))
            .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(12.dp))
            .padding(14.dp)) {
            if (presets.isNotEmpty()) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    presets.forEach { ps ->
                        Box(Modifier.clip(CircleShape)
                            .border(1.dp, MaterialTheme.colorScheme.outline, CircleShape)
                            .clickable {
                                if (editingId.isEmpty()) fLabel = ps.label
                                fBase = ps.baseUrl; fModel = ps.model
                                ps.ctxK?.let { fCtxK = it.toString() }
                                fVision = ps.vision
                            }.padding(horizontal = 11.dp, vertical = 4.dp)) {
                            Text(ps.label, fontSize = 12.5.sp,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }
                Spacer(Modifier.height(12.dp))
            }
            Field("显示名称", "例如 DeepSeek Chat", fLabel, { fLabel = it })
            Field("Base URL", "https://api.deepseek.com/v1", fBase, { fBase = it })
            Field("API Key", keyPh, fKey, { fKey = it }, password = true)
            Field("模型名", "deepseek-flash / qwen-vl-max", fModel, { fModel = it })
            Field("上下文上限（K token）", "留空按 64，例如 256 或 1000", fCtxK,
                { fCtxK = it }, keyboardType = KeyboardType.Number)
            Row(verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.padding(bottom = 14.dp)) {
                Checkbox(fVision, { fVision = it })
                Text("该模型支持图片输入（视觉）", fontSize = 14.sp,
                    color = MaterialTheme.colorScheme.onSurface)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically) {
                SetMini("测试连通") {
                    scope.launch {
                        testBox("测试中…")
                        runCatching {
                            if (formScope != "admin") Api.testMyProviderDraft(draft())
                            else Api.testProviderDraft(draft())
                        }.onSuccess { r ->
                            val ok = r["ok"]?.jsonPrimitive?.booleanOrNull ?: false
                            testBox((if (ok) "✅ " else "❌ ") +
                                (r["detail"]?.jsonPrimitive?.contentOrNull ?: ""))
                        }.onFailure { testBox("❌ " + (it.message ?: "")) }
                    }
                }
                SetMiniPrimary("保存") {
                    scope.launch {
                        runCatching {
                            if (editingId.isNotEmpty()) {
                                if (formScope != "admin") Api.updateMyProvider(editingId, draft())
                                else Api.updateProvider(editingId, draft())
                            } else if (formScope != "admin") Api.addMyProvider(draft())
                            else Api.addProvider(draft())
                        }.onSuccess {
                            testBox("✅ 已保存")
                            formOpen = false; editingId = ""
                            reload(); onChanged()
                        }.onFailure { testBox("❌ " + (it.message ?: "")) }
                    }
                }
                SetMini("取消") { formOpen = false; editingId = ""; result = "" }
            }
            if (result.isNotEmpty()) {
                Text(result, fontSize = 13.sp,
                    color = if (result.startsWith("❌")) MaterialTheme.colorScheme.error
                    else MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 10.dp))
            }
        }
    }

    // —— 手工令牌：管理员专属行（index.html #tokenInput + saveTokenBtn）——
    if (admin) {
        Spacer(Modifier.height(16.dp))
        FormRow("访问令牌", tokenInput, { tokenInput = it }, "保存") {
            if (tokenBusy) return@FormRow
            val t = tokenInput.trim()
            if (t.isEmpty()) { testBox("令牌那一格还是空的"); return@FormRow }
            tokenBusy = true
            scope.launch {
                runCatching { Api.adopt(t) }.onSuccess { info ->
                    tokenInput = ""; tokenBusy = false; result = ""
                    // 本机收编这个人（明文令牌只活过这一次请求头，之后躺在私有存储里）
                    Prefs.addIdentity(AuthResult(token = t, user_id = info.user_id,
                        username = info.username, role = info.role))
                    onAdopted()
                }.onFailure { tokenBusy = false; testBox("这枚凭据没被认：" + (it.message ?: "")) }
            }
        }
        if (result.isNotEmpty()) {
            Text(result, fontSize = 13.sp,
                color = if (result.startsWith("❌")) MaterialTheme.colorScheme.error
                else MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 4.dp))
        }
    }

    confirmDel?.let { row ->
        AlertDialog(onDismissRequest = { confirmDel = null },
            title = { Text("删除「${row.label}」？") },
            confirmButton = {
                TextButton(onClick = {
                    val r = row; val sc = delScope; confirmDel = null
                    scope.launch {
                        runCatching {
                            if (sc == "admin") Api.deleteProvider(r.id)
                            else Api.deleteMyProvider(r.id)
                        }
                        reload(); onChanged()
                    }
                }) { Text("删除", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = { TextButton(onClick = { confirmDel = null }) { Text("取消") } })
    }
}

@Composable
private fun ProvRowCard(p: ProvRow, scope: String, myDefault: String?,
                        onEdit: () -> Unit, onTest: (String) -> Unit,
                        onMyDefault: (String) -> Unit,
                        onSiteDefault: ((String) -> Unit)?,
                        onDelete: (ProvRow) -> Unit) {
    val scheme = MaterialTheme.colorScheme
    var suffix = ""
    if (p.isDefault && scope != "mine") suffix += "（全站默认）"
    if (myDefault == p.id) suffix += "（我的默认）"
    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp))
        .background(scheme.surfaceVariant)
        .border(1.dp, scheme.outline, RoundedCornerShape(12.dp))
        .padding(horizontal = 12.dp, vertical = 10.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            Column(Modifier.weight(1f)) {
                Text(p.label + suffix, fontSize = 14.5.sp, fontWeight = FontWeight.SemiBold,
                    color = scheme.onSurface, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text("${p.model} · ${p.baseUrl} · ${p.keyMasked.ifBlank { "未填密钥" }}",
                    fontSize = 12.sp, color = text3Color(), maxLines = 1,
                    overflow = TextOverflow.Ellipsis)
            }
            val tagC = if (p.hasKey) scheme.primary else scheme.error
            Box(Modifier.border(1.dp, tagC, CircleShape)
                .padding(horizontal = 8.dp, vertical = 1.dp)) {
                Text(if (p.hasKey) { if (p.vision) "可用·视觉" else "可用" } else "缺密钥",
                    fontSize = 11.sp, color = tagC)
            }
        }
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp),
            verticalAlignment = Alignment.CenterVertically) {
            if (scope != "shared") {
                SetMini("✎ 编辑") { onEdit() }
                SetMini("⚡ 测试连通") { onTest(p.id) }
            }
            if (p.hasKey) SetMini("★ 我的默认") { onMyDefault(p.id) }
            if (onSiteDefault != null) {
                SetMini("◎ 全站默认") { onSiteDefault(p.id) }
            }
            if (scope != "shared") SetMini("× 删除", del = true) { onDelete(p) }
        }
    }
}

@Composable
private fun Field(label: String, placeholder: String, value: String,
                  onValue: (String) -> Unit, password: Boolean = false,
                  keyboardType: KeyboardType = KeyboardType.Text) {
    Column(Modifier.fillMaxWidth().padding(bottom = 14.dp)) {
        Text(label, fontSize = 13.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(bottom = 6.dp))
        Box(Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(10.dp))) {
            BasicTextField(value, onValue, singleLine = true,
                visualTransformation = if (password) PasswordVisualTransformation()
                else VisualTransformation.None,
                keyboardOptions = KeyboardOptions(keyboardType = keyboardType),
                textStyle = TextStyle(fontSize = 14.sp,
                    color = MaterialTheme.colorScheme.onSurface),
                cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
                decorationBox = { inner ->
                    Box(Modifier.padding(horizontal = 12.dp, vertical = 9.dp)) {
                        if (value.isEmpty()) Text(placeholder, fontSize = 14.sp,
                            color = text3Color())
                        inner()
                    }
                },
                modifier = Modifier.fillMaxWidth())
        }
    }
}

/* ---------------- 提醒页（renderReminders + reminderStatusCard 逐字） ---------------- */

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun RemindersPage(onNote: (String, Boolean) -> Unit) {
    val ctx = LocalContext.current
    // 从系统设置那一页回来：网页靠 visibilitychange 重画状态卡，原生对位 ON_RESUME
    var permTick by remember { mutableIntStateOf(0) }
    val owner = LocalContext.current as? LifecycleOwner
    DisposableEffect(owner) {
        val obs = LifecycleEventObserver { _, ev ->
            if (ev == Lifecycle.Event.ON_RESUME) permTick++
        }
        owner?.lifecycle?.addObserver(obs)
        onDispose { owner?.lifecycle?.removeObserver(obs) }
    }
    permTick
    val notifGranted = ReminderChannels.notificationsGranted(ctx)
    val exactOk = ReminderScheduler.exactAllowed(ctx)

    var remTick by remember { mutableIntStateOf(0) }
    var title by remember { mutableStateOf("") }
    var dateMillis by remember { mutableStateOf<Long?>(null) }
    var timeHM by remember { mutableStateOf<Pair<Int, Int>?>(null) }
    var repeat by remember { mutableStateOf("once") }
    var showDate by remember { mutableStateOf(false) }
    var showTime by remember { mutableStateOf(false) }
    val items = remember(remTick) { ReminderStore.list() }

    Spacer(Modifier.height(12.dp))
    // 状态卡排在表单之前、且空列表时也在（网页注释原话：它回答的是"为什么不响"）
    SetCard {
        ReminderStatusRow("通知", notifGranted, "已授权", "没授权：到点发不出去",
            onGo = {
                val ok = runCatching {
                    if (Build.VERSION.SDK_INT >= 26) {
                        ctx.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                            .putExtra(Settings.EXTRA_APP_PACKAGE, ctx.packageName)
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    } else {
                        ctx.startActivity(
                            Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
                                .setData(Uri.parse("package:" + ctx.packageName))
                                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    }
                }.isSuccess
                if (!ok) onNote("打不开系统那一页，请到系统设置里搜「Fenver」", true)
            })
        ReminderStatusRow("闹钟", exactOk, "能准点", "没给精准闹钟：可能被省电推迟",
            divider = false,
            onGo = {
                val ok = runCatching {
                    if (Build.VERSION.SDK_INT >= 31) {
                        ctx.startActivity(
                            Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM)
                                .setData(Uri.parse("package:" + ctx.packageName))
                                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    } else {
                        ctx.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                            .putExtra(Settings.EXTRA_APP_PACKAGE, ctx.packageName)
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    }
                }.isSuccess
                if (!ok) onNote("打不开系统那一页，请到系统设置里搜「Fenver」", true)
            })
    }
    Spacer(Modifier.height(12.dp))

    // 表单：标题 + 日期时间 + 重复 + 添加（网页 .row 一行，原生排几行更稳）
    FormRow("提醒我什么", title, { title = it }, btn = "")
    Row(Modifier.fillMaxWidth().padding(vertical = 5.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.weight(1f).clip(RoundedCornerShape(10.dp))
            .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(10.dp))
            .clickable { showDate = true }
            .padding(horizontal = 12.dp, vertical = 10.dp)) {
            Text(dateMillis?.let { fmtMD(it) } ?: "提醒时间", fontSize = 14.sp,
                color = if (dateMillis == null) text3Color()
                else MaterialTheme.colorScheme.onSurface)
        }
        Box(Modifier.weight(1f).clip(RoundedCornerShape(10.dp))
            .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(10.dp))
            .clickable { showTime = true }
            .padding(horizontal = 12.dp, vertical = 10.dp)) {
            Text(timeHM?.let { "%02d:%02d".format(it.first, it.second) } ?: "时间",
                fontSize = 14.sp,
                color = if (timeHM == null) text3Color()
                else MaterialTheme.colorScheme.onSurface)
        }
    }
    Spacer(Modifier.height(4.dp))
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp),
        verticalAlignment = Alignment.CenterVertically) {
        listOf("once" to "只一次", "daily" to "每天", "weekly" to "每周").forEach { (v, l) ->
            val on = repeat == v
            Box(Modifier.clip(CircleShape)
                .then(if (on) Modifier.background(MaterialTheme.colorScheme.primaryContainer)
                else Modifier.border(1.dp, MaterialTheme.colorScheme.outline, CircleShape))
                .clickable { repeat = v }
                .padding(horizontal = 12.dp, vertical = 7.dp)) {
                Text(l, fontSize = 12.5.sp,
                    color = if (on) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        Spacer(Modifier.weight(1f))
        SetMiniPrimary("添加") {
            val t = title.trim()
            if (t.isEmpty()) { onNote("先写上要提醒什么", true); return@SetMiniPrimary }
            val d = dateMillis; val hm = timeHM
            if (d == null || hm == null) { onNote("时间还没选好", true); return@SetMiniPrimary }
            // too_many 的整句文案（壳侧判据同 REMinders.MAX_PER_OWNER）
            if (ReminderStore.list().size >= 32) {
                onNote("这个人已经有 32 条提醒了，先取消几条", true); return@SetMiniPrimary
            }
            val cal = java.util.Calendar.getInstance().apply {
                timeInMillis = d
                set(java.util.Calendar.HOUR_OF_DAY, hm.first)
                set(java.util.Calendar.MINUTE, hm.second)
                set(java.util.Calendar.SECOND, 0)
                set(java.util.Calendar.MILLISECOND, 0)
            }
            val item = ReminderStore.add(cal.timeInMillis, t, "", repeat)
            if (item == null) {
                onNote("没能设这条提醒：壳没有应答", true); return@SetMiniPrimary
            }
            ReminderScheduler.schedule(ctx, item)
            onNote("", false)
            title = ""; dateMillis = null; timeHM = null; repeat = "once"
            remTick++
        }
    }
    Spacer(Modifier.height(12.dp))

    if (items.isEmpty()) {
        Text("还没有提醒", fontSize = 13.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        // 日期/时间选择器在空列表分支也要能弹——return 前先挂上
    } else {
        SetCard {
            items.forEachIndexed { i, r ->
                Row(Modifier.fillMaxWidth().heightIn(min = 54.dp).padding(horizontal = 14.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    Text(r.title.ifBlank { "提醒" }, fontSize = 15.sp,
                        color = MaterialTheme.colorScheme.onSurface, maxLines = 1,
                        overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
                    val hist = fmtFiredHistory(r.firedAt, r.missed)
                    Text(fmtReminderAt(r.at) +
                            when (r.repeat) { "daily" -> " 每天"; "weekly" -> " 每周"; else -> "" } +
                            if (hist.isNotEmpty()) " · $hist" else "",
                        fontSize = 14.sp, color = text3Color(), maxLines = 1)
                    SetMini("取消", del = true) {
                        ReminderScheduler.cancel(ctx, r.id)
                        ReminderStore.cancel(r.id)
                        remTick++
                    }
                }
                if (i != items.size - 1) {
                    Box(Modifier.fillMaxWidth().padding(horizontal = 14.dp).height(1.dp)
                        .background(MaterialTheme.colorScheme.outline))
                }
            }
        }
    }

    if (showDate) {
        val st = rememberDatePickerState()
        DatePickerDialog(onDismissRequest = { showDate = false },
            confirmButton = {
                TextButton(onClick = {
                    st.selectedDateMillis?.let { utc ->
                        val c = java.util.Calendar.getInstance(
                            java.util.TimeZone.getTimeZone("UTC")).apply {
                            timeInMillis = utc
                        }
                        val local = java.util.Calendar.getInstance().apply {
                            clear()
                            set(c.get(java.util.Calendar.YEAR),
                                c.get(java.util.Calendar.MONTH),
                                c.get(java.util.Calendar.DAY_OF_MONTH))
                        }
                        dateMillis = local.timeInMillis
                    }
                    showDate = false
                }) { Text("确定") }
            }) { DatePicker(state = st) }
    }
    if (showTime) {
        val st = rememberTimePickerState(
            initialHour = timeHM?.first ?: 9,
            initialMinute = timeHM?.second ?: 0, is24Hour = true)
        AlertDialog(onDismissRequest = { showTime = false },
            confirmButton = {
                TextButton(onClick = { timeHM = st.hour to st.minute; showTime = false }) {
                    Text("确定")
                }
            },
            text = { TimePicker(state = st) })
    }
}

/* reminderStatusCard 的一行：标签 15px + 状态值 + set-mini「去设置」。
   （不复用 SetRow——那行的图标槽在这里没有图标，且整行不可点，只有按钮可点。） */
@Composable
private fun ReminderStatusRow(label: String, granted: Boolean, okText: String,
                              badText: String, divider: Boolean = true,
                              onGo: () -> Unit) {
    Row(Modifier.fillMaxWidth().heightIn(min = 54.dp).padding(horizontal = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(label, fontSize = 15.sp, color = MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.weight(1f))
        SetValText(if (granted) okText else badText)
        SetMini("去设置", onClick = onGo)
    }
    if (divider) {
        Box(Modifier.fillMaxWidth().padding(horizontal = 14.dp).height(1.dp)
            .background(MaterialTheme.colorScheme.outline))
    }
}

/** fmtReminderAt：月/日 时:分（网页注释：跨年提醒没意义）。 */
private fun fmtReminderAt(ms: Long): String =
    java.text.SimpleDateFormat("MM/dd HH:mm", java.util.Locale.US).format(java.util.Date(ms))

private fun fmtMD(ms: Long): String =
    java.text.SimpleDateFormat("MM/dd", java.util.Locale.US).format(java.util.Date(ms))

/** fmtFiredHistory 逐字：上次发出 … / N 次到点没发出 / 到点还没响过。 */
private fun fmtFiredHistory(firedAt: Long, missed: Long): String {
    val parts = ArrayList<String>()
    if (firedAt > 0) parts.add("上次发出 " + fmtReminderAt(firedAt))
    if (missed > 0) parts.add("$missed 次到点没发出")
    return if (parts.isEmpty()) "到点还没响过" else parts.joinToString(" · ")
}

/* 点击即败的专属文案（T2.5，R2-AC-2「断网」那一格）。
   只分两类：够不着服务（IOException 一族）说网络话；服务端给了明确回答的
   把 detail 原样念出来——与网页 setStatus("导出失败：" + e.message) 同一口径。 */
private fun exportClickFailure(e: Throwable): String = when {
    e is java.io.IOException -> "导出失败：网络似乎不通，请检查连接后重试"
    else -> "导出失败：" + (e.message ?: "未知原因")
}
