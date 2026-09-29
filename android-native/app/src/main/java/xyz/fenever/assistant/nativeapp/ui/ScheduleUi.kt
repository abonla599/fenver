package xyz.fenever.assistant.nativeapp.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.focusable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
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
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.focus.onFocusEvent
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.roundToInt
import xyz.fenever.assistant.core.ScheduleBoard
import xyz.fenever.assistant.nativeapp.Api
import xyz.fenever.assistant.nativeapp.ApiException
import xyz.fenever.assistant.nativeapp.ScheduleItemDto
import xyz.fenever.assistant.nativeapp.Prefs
import xyz.fenever.assistant.nativeapp.theme.WebTokens
import xyz.fenever.assistant.nativeapp.theme.isWebLight
import xyz.fenever.assistant.nativeapp.theme.text3Color

/* 日程页（v0.23 R3 · T2.7）= 网页 backend/app/web/static/{index.html#paneSchedule,
 * style.css 的日程块, app.js 的 sched 模块} 的原生化，规格与文案的唯一来源是
 * docs/v0.23-阶段0/T0.1-日程设计稿-双端同构.html。
 *
 * 三条验收口径与网页逐字同构（判据本身在纯 JVM 的 ScheduleBoard 里，台架直接跑）：
 *  · 「今天」取服务端 GET 回来的 day，不读设备时钟（R3-AC-3）；
 *  · 400 把服务端 detail 原文放进错误条，并且**不**渲染空态（R3-AC-2）；
 *  · 客户端不做内容校验，保存的写回点只有「保存」这一颗（整天 PUT，幂等）。
 *
 * 唯一允许的两处平台差异（设计稿评审记录里点名的那两条）：
 *  · 操作按钮常显（网页 hover 才显形 —— 手机没有 hover）；
 *  · 进场动效：网页 CSS nth-child delay 逐行 stagger，这里用 LaunchedEffect + delay 做同一件事。
 */

/** 一条事项：与 GET/PUT 的 items 同形（id 只在服务端那份里有意义，这里不持有）。 */
private data class SchedItem(val text: String, val at: String, val done: Boolean)

/**
 * 页面状态住在文件级对象里，不住 remember 里 —— 与网页那份 `const sched = {…}` 同构：
 * 离开这一页再回来（设置 → 别的页 → 设置 → 日程）草稿必须还在，而 remember 会随
 * 组合销毁。代价是它是进程内单例：换账号时由 MainActivity 整壳重建（reenterRequested）
 * 清掉，与网页换人时 reload 同一语义。
 */
private object SchedState {
    var owner by mutableStateOf("")           // 草稿属于谁（换人时必须整份清掉）
    var today by mutableStateOf("")           // 服务端今天（那次不传 day 的 GET 回到的 day）
    var day by mutableStateOf("")             // 当前看的那一天
    var items by mutableStateOf(listOf<SchedItem>())   // 本地草稿
    var pristine by mutableStateOf("")        // 与服务端一致那一份的指纹 = dirty 判据
    var days by mutableStateOf(listOf<String>())       // 有安排的那些天 → 日期点
    var loading by mutableStateOf(false)
    var saving by mutableStateOf(false)
    var error by mutableStateOf("")           // 服务端 detail 原文；非空即错误态
    var editing by mutableStateOf(-1)         // 原地编辑的行号，-1 = 没有
    var isNew by mutableStateOf(false)        // 正在编辑这行是本次新加的（取消时要收掉）
    var savedTick by mutableStateOf(0)        // 「已保存」淡入淡出的一次性发令枪
    var viewYear by mutableStateOf(0)         // 月历看的是哪一年 / 哪一月（0 = 还没锚上）
    var viewMonth by mutableStateOf(0)        // 月从 0 起，与 Calendar.MONTH 同口径
    var askDay by mutableStateOf<String?>(null)   // 待确认的那个日子：非空即弹「换天会丢草稿」

    fun reset() {
        today = ""; day = ""; items = emptyList(); pristine = ""; days = emptyList()
        loading = false; saving = false; error = ""; editing = -1; isNew = false
        viewYear = 0; viewMonth = 0; askDay = null
    }
}

/** 换一天看之前的那句确认：与网页 loadSchedule 里同一串字（原生没有 window.confirm）。 */
private const val SWITCH_DAY_ASK = "有未保存的修改：换到别的日子就不会保存它们。确定切换？"

/** 长度前缀式指纹：比 JSON 少一个依赖，也比它难撞（文本里出现分隔符也不会算错 dirty）。 */
private fun snapshotOf(items: List<SchedItem>): String {
    val sb = StringBuilder()
    for (it in items) {
        sb.append(it.text.length).append(':').append(it.text)
            .append(it.at.length).append(':').append(it.at)
            .append(if (it.done) '1' else '0')
    }
    return sb.toString()
}

private fun toDto(items: List<SchedItem>): List<ScheduleItemDto> =
    items.map { ScheduleItemDto(text = it.text, at = it.at, done = it.done) }

/**
 * 日程页主体。`onValChanged` 回给一级列表「N 天有安排」那格（对照网页 #scheduleVal）。
 */
@Composable
fun SchedulePage(onNote: (String, Boolean) -> Unit, onRequireAuth: (String) -> Unit,
                 onValChanged: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    // fetch 在前、load 在后：Kotlin 的局部函数没有"先声明后使用"的豁免，
    // load 要调 fetch，声明顺序反了就是 unresolved reference（CI 红过一次，别再红第二次）。
    fun fetch(target: String?) {
        scope.launch {
            SchedState.loading = true; SchedState.error = ""; SchedState.editing = -1
            SchedState.isNew = false
            try {
                val data = Api.getSchedule(target)
                // 只有"没指定哪天"那一次才允许改写锚点：点了别的日子不能把今天挪走。
                if (target.isNullOrEmpty()) SchedState.today = data.day
                SchedState.day = data.day
                // 月历跟着看的那一天走：跨月进来也要落在那一年那一个月上。
                val month = ScheduleBoard.monthOf(data.day)
                if (month != null) { SchedState.viewYear = month[0]; SchedState.viewMonth = month[1] }
                SchedState.items = data.items.map { SchedItem(it.text, it.at, it.done) }
                SchedState.days = data.days
                SchedState.pristine = snapshotOf(SchedState.items)
            } catch (e: Exception) {
                if (e is ApiException && e.status == 401) {
                    onNote("还没有登录：登录后可查看日程", true)
                    onRequireAuth("login")
                    SchedState.items = emptyList()
                } else {
                    SchedState.error = e.message ?: ""    // 服务端 detail 原文，不加自己的解释
                    SchedState.items = emptyList()
                    if (!target.isNullOrEmpty()) SchedState.day = target
                }
            } finally {
                SchedState.loading = false
                onValChanged(if (SchedState.today.isEmpty()) ""
                    else "${SchedState.days.size} 天有安排")
            }
        }
    }

    // 草稿优先：还在同一天、手上有未保存改动，就不要拿服务端那份把人写的盖掉。
    fun load(target: String?) {
        if (SchedState.day.isNotEmpty() && (target == null || target == SchedState.day)
            && snapshotOf(SchedState.items) != SchedState.pristine) return
        // 换一天看 = 手上这份草稿会被那一天的服务端清单盖掉。网页用 confirm() 问一句
        //（同步，浏览器把这一屏挡住了），Compose 里没有同步弹窗，只能把它记成一次待确认
        // 的换天，由下面那颗 AlertDialog 接着问——问的是同一句话，口径不分家。
        if (target != null && SchedState.day.isNotEmpty() && target != SchedState.day
            && snapshotOf(SchedState.items) != SchedState.pristine) {
            SchedState.askDay = target
            return
        }
        fetch(target)
    }

    /** 翻月 / 翻年（delta 以月为单位，±12 就是年）。翻不到今天所在月之前：那个月里一格都点不动。 */
    fun moveMonth(delta: Int) {
        val anchor = ScheduleBoard.monthOf(SchedState.today) ?: return
        val m = ScheduleBoard.shiftMonth(SchedState.viewYear, SchedState.viewMonth, delta)
        if (ScheduleBoard.monthIndex(m[0], m[1])
            < ScheduleBoard.monthIndex(anchor[0], anchor[1])) return
        SchedState.viewYear = m[0]
        SchedState.viewMonth = m[1]
    }

    fun save() {
        if (SchedState.saving) return
        // 空文本行在发出去之前滤掉：编辑行虽然已实时写回，但"刚点添加、一个字没写"的
        // 空行还留在草稿里——服务端 clean_text 会因一行空文本 400 掉整天保存，错误条
        // 又按 R3-AC-2 盖掉清单，人刚写的字看起来就"没了"（2026-09-28 评审第 1 条，网页同一口径）。
        val payload = SchedState.items.filter { it.text.isNotBlank() }
        scope.launch {
            // 这里绝不提前把 pristine 换成草稿：pristine 只代表"服务端确认过的那一份"。
            // 提前定住会让失败的这一趟看起来像成功——人以为存上了，其实改动还在手里。
            SchedState.saving = true
            try {
                val data = Api.putSchedule(SchedState.day, toDto(payload))
                SchedState.items = data.items.map { SchedItem(it.text, it.at, it.done) }
                if (data.day.isNotEmpty()) SchedState.day = data.day
                SchedState.error = ""
                // PUT 的响应没有 days：这一天的有无由 count 说了算，别的天不靠猜。
                val has = (if (data.count > 0) data.count else SchedState.items.size) > 0
                SchedState.days = if (has) {
                    (SchedState.days + SchedState.day).distinct().sorted()
                } else {
                    SchedState.days.filter { it != SchedState.day }
                }
                SchedState.pristine = snapshotOf(SchedState.items)
                SchedState.savedTick++
            } catch (e: Exception) {
                if (e is ApiException && e.status == 401) {
                    onNote("还没有登录：登录后可保存日程", true); onRequireAuth("login")
                } else {
                    // 失败保留 dirty：改完可以直接再存一次，不用重写一遍。
                    SchedState.error = e.message ?: ""
                }
            } finally {
                SchedState.saving = false
                onValChanged(if (SchedState.today.isEmpty()) ""
                    else "${SchedState.days.size} 天有安排")
            }
        }
    }

    LaunchedEffect(Unit) {
        // 草稿是"谁写的"这件事必须跟着身份走：同一台设备换账号或退出后重登，
        // 上一个人的未保存清单不该出现在这一页（网页换人走 reload，同一语义）。
        val who = Prefs.currentEntry()?.userId ?: ""
        if (SchedState.owner != who) {
            SchedState.reset()
            SchedState.owner = who
        }
        if (SchedState.day.isEmpty()) load(null)
    }

    val dirty = snapshotOf(SchedState.items) != SchedState.pristine
    val today = SchedState.today
    val day = SchedState.day

    // 换天会丢掉手上这份草稿 —— 与网页 confirm() 问的是同一句话，只是这里得用一颗弹窗。
    SchedState.askDay?.let { pending ->
        AlertDialog(
            onDismissRequest = { SchedState.askDay = null },
            text = { Text(SWITCH_DAY_ASK) },
            confirmButton = {
                TextButton(onClick = {
                    SchedState.askDay = null
                    fetch(pending)     // 问过一句了：这一趟直接发 GET，别再弹回来
                }) { Text("确定切换") }
            },
            dismissButton = {
                TextButton(onClick = { SchedState.askDay = null }) { Text("留下保存") }
            },
        )
    }

    SchedPanel(
        head = {
            Row(verticalAlignment = Alignment.Bottom,
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("日程", fontSize = 19.sp, fontWeight = FontWeight.SemiBold,
                    letterSpacing = 0.3.sp, color = MaterialTheme.colorScheme.onSurface)
                if (day.isNotEmpty()) {
                    Text(ScheduleBoard.dayLabel(day, today), fontSize = 12.5.sp,
                        letterSpacing = 0.4.sp, color = text3Color(),
                        modifier = Modifier.padding(bottom = 2.dp))
                }
            }
            Spacer(Modifier.height(2.dp))
            Text("「今天」取服务端日期", fontSize = 12.5.sp,
                letterSpacing = 0.4.sp, color = text3Color())
        },
        calendar = {
            val floor = if (today.isEmpty()) -1 else (ScheduleBoard.monthOf(today)
                ?.let { ScheduleBoard.monthIndex(it[0], it[1]) } ?: -1)
            val at = ScheduleBoard.monthIndex(SchedState.viewYear, SchedState.viewMonth)
            Row(Modifier.fillMaxWidth().padding(start = 8.dp, end = 8.dp, top = 6.dp, bottom = 6.dp),
                verticalAlignment = Alignment.CenterVertically) {
                CalNav("«", "上一年", floor < 0 || at - 12 < floor) { moveMonth(-12) }
                CalNav("‹", "上个月", floor < 0 || at <= floor) { moveMonth(-1) }
                Text(if (floor < 0) "" else ScheduleBoard.monthTitle(
                    SchedState.viewYear, SchedState.viewMonth),
                    fontSize = 15.sp, fontWeight = FontWeight.SemiBold, letterSpacing = 0.4.sp,
                    color = MaterialTheme.colorScheme.onSurface,
                    textAlign = TextAlign.Center, modifier = Modifier.weight(1f))
                CalNav("›", "下个月", floor < 0) { moveMonth(1) }
                CalNav("»", "下一年", floor < 0) { moveMonth(12) }
            }
            Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp)) {
                for (w in ScheduleBoard.WEEK_HEAD) {
                    Text(w, fontSize = 10.5.sp, letterSpacing = 0.5.sp, color = text3Color(),
                        textAlign = TextAlign.Center, modifier = Modifier.weight(1f))
                }
            }
            // 锚点还没回来（或返回了坏日期）时整片格子不渲染 —— 与网页 `if (sched.today)` 同一件事。
            if (floor >= 0) {
                for (week in ScheduleBoard.monthCells(SchedState.viewYear, SchedState.viewMonth)
                        .toList().chunked(7)) {
                    Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp)) {
                        for (iso in week) {
                            if (iso.isEmpty()) Spacer(Modifier.weight(1f).height(44.dp))
                            else CalCell(iso = iso, today = today,
                                has = SchedState.days.contains(iso), sel = iso == day,
                                enabled = !SchedState.loading,
                                modifier = Modifier.weight(1f),
                                onClick = { if (iso != day) load(iso) })
                        }
                    }
                }
            }
        },
        error = SchedState.error,
        body = {
            if (SchedState.error.isNotEmpty()) {
                // 错误态与空态严格互斥：有 detail 就只给错误条，清单区整块收起（R3-AC-2）。
            } else if (SchedState.loading && SchedState.items.isEmpty() && SchedState.day.isEmpty()) {
                // 只有"首屏在途、连锚点都没有"这一种情况才留白：这一页还没见过任何一天，
                // 说什么都算瞎猜。换天不在这里——上一天也是空的时候，藏掉空态就是
                // 用户看到的"先闪一帧空白"（2026-09-28 真机反馈），所以下面空态在途时置灰留着。
                Spacer(Modifier.height(28.dp))
            } else if (SchedState.items.isEmpty()) {
                Column(Modifier.fillMaxWidth().alpha(if (SchedState.loading) 0.55f else 1f)
                    .padding(horizontal = 16.dp, vertical = 34.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text("▤", fontSize = 26.sp, color = text3Color(),
                        modifier = Modifier.alpha(0.8f))
                    Text("这天还没有安排", fontSize = 14.sp, color = text3Color(),
                        textAlign = TextAlign.Center)
                    Text("点下面「添加一项」记下第一件", fontSize = 14.sp, color = text3Color(),
                        textAlign = TextAlign.Center)
                }
            } else {
                // 换天在途时清单不再整块消失：旧内容置灰禁点地留着，新数据回来整体替换
                //（以前这里就是"闪一下"的那一帧空面板；网页 .busy 同一口径）。
                val busy = SchedState.loading
                Column(Modifier.fillMaxWidth().alpha(if (busy) 0.55f else 1f)) {
                    Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp,
                        top = 22.dp, bottom = 8.dp),
                        horizontalArrangement = Arrangement.SpaceBetween) {
                        Text(ScheduleBoard.groupTitle(day, today), fontSize = 12.5.sp,
                            letterSpacing = 0.4.sp, color = text3Color())
                        Text(ScheduleBoard.countText(SchedState.items.size,
                            SchedState.items.count { it.done }),
                            fontSize = 12.5.sp, color = text3Color(), modifier = Modifier.alpha(0.75f))
                    }
                    SchedState.items.forEachIndexed { index, item ->
                        Staggered(index) {
                            ItemRow(item = item,
                                editing = SchedState.editing == index,
                                interactive = !busy,
                                onLive = { t, a ->
                                    // 编辑内容实时写回草稿清单：任何时候按保存，存的都是看到的
                                    //（保存不再赌"失焦提交先于点击"的时序，2026-09-28 评审第 1 条）。
                                    SchedState.items = SchedState.items.toMutableList().also {
                                        it[index] = SchedItem(t, a, item.done)
                                    }
                                },
                                onToggle = {
                                    SchedState.items = SchedState.items.toMutableList().also {
                                        it[index] = item.copy(done = !item.done)
                                    }
                                },
                                onStartEdit = {
                                    SchedState.editing = index; SchedState.isNew = false
                                },
                                onDelete = {
                                    // 不做删除确认：保存才是唯一写回点，整天 PUT 的语义下这一步随时
                                    // 可以反悔（不保存就走 = 什么都没发生）。
                                    SchedState.items = SchedState.items.filterIndexed { i, _ -> i != index }
                                    if (SchedState.editing >= SchedState.items.size) SchedState.editing = -1
                                },
                                onCommit = { text, at ->
                                    // 空文本按"取消"收：服务端 clean_text 会回一句「事项不能是空的」并 400
                                    // 掉整天，而错误条按 R3-AC-2 会盖掉整张清单——留一行注定存不下去的东西
                                    // 不如根本没加过。网页 schedRow 的 commit 同一口径。
                                    if (text.isBlank()) {
                                        if (SchedState.isNew) {
                                            SchedState.items = SchedState.items
                                                .filterIndexed { i, _ -> i != index }
                                        }
                                        SchedState.editing = -1; SchedState.isNew = false
                                    } else {
                                        SchedState.items = SchedState.items.toMutableList().also {
                                            it[index] = SchedItem(text.trim(), at.trim(), item.done)
                                        }
                                        SchedState.editing = -1; SchedState.isNew = false
                                    }
                                },
                                onCancel = {
                                    if (SchedState.isNew) {
                                        SchedState.items = SchedState.items.filterIndexed { i, _ -> i != index }
                                    }
                                    SchedState.editing = -1; SchedState.isNew = false
                                })
                        }
                    }
                }
            }
        },
        addLabel = "＋ 添加一项",
        onAdd = {
            if (!SchedState.loading) {
                SchedState.items = SchedState.items + SchedItem("", "", false)
                SchedState.editing = SchedState.items.size - 1
                SchedState.isNew = true
            }
        },
        dirty = dirty,
        saving = SchedState.saving,
        savedTick = SchedState.savedTick,
        onSave = { save() },
    )
}

/** 面板外壳：18 圆角 + 1px 细描边 + 强玻璃底（.sched-panel）。 */
@Composable
private fun SchedPanel(head: @Composable () -> Unit, calendar: @Composable () -> Unit,
                       error: String, body: @Composable () -> Unit, addLabel: String,
                       onAdd: () -> Unit, dirty: Boolean, saving: Boolean,
                       savedTick: Int, onSave: () -> Unit) {
    Column(Modifier.fillMaxWidth()
        .clip(RoundedCornerShape(18.dp))
        .background(glassStrong())
        .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(18.dp))) {
        Column(Modifier.padding(start = 16.dp, end = 16.dp, top = 16.dp, bottom = 10.dp)) { head() }
        calendar()
        if (error.isNotEmpty()) {
            Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 16.dp, bottom = 14.dp)
                .clip(RoundedCornerShape(12.dp))
                .background(MaterialTheme.colorScheme.error.copy(alpha = 0.08f))
                .border(1.dp, MaterialTheme.colorScheme.error.copy(alpha = 0.25f),
                    RoundedCornerShape(12.dp))
                .padding(horizontal = 12.dp, vertical = 10.dp)) {
                Text("⚠ " + error, fontSize = 13.5.sp, color = MaterialTheme.colorScheme.error)
            }
        }
        body()
        Box(Modifier.fillMaxWidth().height(1.dp).background(MaterialTheme.colorScheme.outline))
        Row(Modifier.fillMaxWidth().heightIn(min = 54.dp)
            .clickable(onClick = onAdd)
            .padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("＋", fontSize = 17.sp, color = MaterialTheme.colorScheme.primary,
                modifier = Modifier.width(20.dp), textAlign = TextAlign.Center)
            Text(addLabel.substring(1), fontSize = 15.sp, color = text3Color())
        }
        SchedFoot(dirty = dirty, saving = saving, savedTick = savedTick, onSave = onSave)
    }
}

@Composable
private fun glassStrong(): androidx.compose.ui.graphics.Color =
    if (isWebLight()) WebTokens.LGlassStrong else WebTokens.GlassStrong

/** .cal-nav：翻月/翻年那一排。不可用时只降透明度，位置不动 —— 钮会跳位比看不见更难用。 */
@Composable
private fun CalNav(glyph: String, label: String, enabled: Boolean, onClick: () -> Unit) {
    Box(Modifier.size(width = 34.dp, height = 30.dp).clip(RoundedCornerShape(10.dp))
        .alpha(if (enabled) 1f else 0.3f)
        .then(if (enabled) Modifier.clickable(onClick = onClick) else Modifier.focusable())
        .semantics { contentDescription = label },
        contentAlignment = Alignment.Center) {
        Text(glyph, fontSize = 16.sp, lineHeight = 16.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

/** .calcell：月历里的一格。今天填主色，选中描主色 + 软底，有安排带一颗点；
 *  今天以前的日子 disabled —— 日程是"往后的事"，把已经过去的一天点开编辑没有意义。
 */
@Composable
private fun CalCell(iso: String, today: String, has: Boolean, sel: Boolean,
                    enabled: Boolean, modifier: Modifier = Modifier, onClick: () -> Unit) {
    val past = ScheduleBoard.isBefore(iso, today)
    val clickable = enabled && !past && !sel
    val isToday = ScheduleBoard.isToday(iso, today)
    Column(modifier.height(44.dp)
        .clip(RoundedCornerShape(12.dp))
        .background(if (sel) accentSoft() else Color.Transparent, RoundedCornerShape(12.dp))
        .border(1.dp, if (sel) MaterialTheme.colorScheme.primary else Color.Transparent,
            RoundedCornerShape(12.dp))
        .then(if (clickable) Modifier.clickable(onClick = onClick) else Modifier.focusable())
        .alpha(if (past) 0.45f else 1f)
        .semantics { contentDescription = ScheduleBoard.cellLabel(iso, has) },
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center) {
        Box(Modifier.size(26.dp).clip(CircleShape)
            .background(if (isToday) MaterialTheme.colorScheme.primary else Color.Transparent,
                CircleShape),
            contentAlignment = Alignment.Center) {
            Text(ScheduleBoard.dayNumber(iso), fontSize = 14.sp,
                fontWeight = FontWeight.SemiBold,
                color = if (isToday) MaterialTheme.colorScheme.onPrimary
                else MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Box(Modifier.padding(top = 3.dp).size(4.dp).clip(CircleShape)
            .background(when {
                !has -> Color.Transparent
                sel -> MaterialTheme.colorScheme.primary
                else -> accent2()
            }, CircleShape))
    }
}

/** 条目行：勾选 + 正文 + 时间角标 + 操作（.op 在触摸端常显）。`interactive=false` = 在途加载，全部禁点。 */
@Composable
private fun ItemRow(item: SchedItem, editing: Boolean, interactive: Boolean,
                    onLive: (String, String) -> Unit, onToggle: () -> Unit,
                    onStartEdit: () -> Unit, onDelete: () -> Unit,
                    onCommit: (String, String) -> Unit, onCancel: () -> Unit) {
    Column(Modifier.fillMaxWidth()) {
        // 网页 `.item { border-top: 1px solid var(--line) }`：Compose 没有单边边框，
        // 用一条 1dp 的线做同一件事（清单区第一行也带线，与网页一致）。
        Box(Modifier.fillMaxWidth().height(1.dp).background(MaterialTheme.colorScheme.outline))
        if (editing) {
            EditRow(item = item, onLive = onLive, onCommit = onCommit, onCancel = onCancel)
        } else {
            Row(Modifier.fillMaxWidth().heightIn(min = 54.dp)
                .padding(horizontal = 16.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Box(Modifier.size(20.dp).clip(RoundedCornerShape(6.dp))
                    .border(1.5.dp, if (item.done) MaterialTheme.colorScheme.primary
                    else text3Color(), RoundedCornerShape(6.dp))
                    .background(if (item.done) MaterialTheme.colorScheme.primary
                    else androidx.compose.ui.graphics.Color.Transparent,
                        RoundedCornerShape(6.dp))
                    .clickable(enabled = interactive, onClick = onToggle),
                    contentAlignment = Alignment.Center) {
                    if (item.done) Text("✓", fontSize = 13.sp,
                        color = MaterialTheme.colorScheme.onPrimary)
                }
                Text(item.text, fontSize = 15.sp, color = if (item.done) text3Color()
                else MaterialTheme.colorScheme.onSurface,
                    textDecoration = if (item.done) androidx.compose.ui.text.style.TextDecoration.LineThrough
                    else null,
                    modifier = Modifier.weight(1f))
                Text(if (item.at.isEmpty()) ScheduleBoard.NO_TIME else item.at,
                    fontSize = 12.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    fontFamily = FontFamily.Monospace, letterSpacing = 0.4.sp,
                    modifier = Modifier.alpha(if (item.at.isEmpty()) 0f else 1f)
                        .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(999.dp))
                        .background(MaterialTheme.colorScheme.surfaceVariant,
                            RoundedCornerShape(999.dp))
                        .padding(horizontal = 10.dp, vertical = 3.dp))
                Text("✎", fontSize = 14.sp, color = text3Color(),
                    modifier = Modifier.clip(CircleShape).clickable(enabled = interactive,
                        onClick = onStartEdit)
                        .padding(horizontal = 6.dp, vertical = 4.dp))
                Text("🗑", fontSize = 14.sp, color = text3Color(),
                    modifier = Modifier.clip(CircleShape).clickable(enabled = interactive,
                        onClick = onDelete)
                        .padding(horizontal = 6.dp, vertical = 4.dp))
            }
        }
    }
}

/** "HH:MM" → (时, 分)；坏值一律回落到 09:00，与网页 input[type=time] 的空值起点同一思路。 */
private fun parseHm(s: String): Pair<Int, Int> {
    val m = Regex("^(\\d{1,2}):(\\d{2})$").find(s) ?: return 9 to 0
    val h = m.groupValues[1].toIntOrNull() ?: return 9 to 0
    val mi = m.groupValues[2].toIntOrNull() ?: return 9 to 0
    return if (h in 0..23 && mi in 0..59) h to mi else 9 to 0
}

/** .edit-box 两格：正文手输且**实时写回草稿**；时间不手输，点开上下滚轮
 *  （时/分两列，拖动选数），写回的一律是 HH:MM 或空
 *  （2026-09-28 真机反馈第 3 条：M3 表盘在手机上难用，换成滚轮更顺手）。 */
@Composable
private fun EditRow(item: SchedItem, onLive: (String, String) -> Unit,
                    onCommit: (String, String) -> Unit, onCancel: () -> Unit) {
    var text by remember { mutableStateOf(item.text) }
    var at by remember { mutableStateOf(item.at) }
    var textFocused by remember { mutableStateOf(false) }
    var picking by remember { mutableStateOf(false) }
    val keyboard = LocalSoftwareKeyboardController.current
    val focusReq = remember { FocusRequester() }
    LaunchedEffect(Unit) { runCatching { focusReq.requestFocus() } }
    fun commit() {
        keyboard?.hide()
        onCommit(text, at)
    }
    fun esc(e: androidx.compose.ui.input.key.KeyEvent): Boolean {
        if (e.type == KeyEventType.KeyDown && e.key == Key.Escape) { onCancel(); return true }
        return false
    }
    fun setTime(v: String) { at = v; onLive(text, v) }
    // 走开才收：弹窗会抢焦点，但行不能先关——关了选出来的时间就没地方落了，
    // 所以 picking 在途时这一路闸住（网页那一份是 activeElement 判焦点，同一件事）。
    LaunchedEffect(textFocused, picking) {
        if (textFocused || picking) return@LaunchedEffect
        delay(120)
        if (!textFocused && !picking) commit()
    }
    Row(Modifier.fillMaxWidth().heightIn(min = 54.dp)
        .padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        BasicTextField(
            value = text, onValueChange = { text = it; onLive(it, at) },
            singleLine = true,
            textStyle = TextStyle(fontSize = 15.sp, color = MaterialTheme.colorScheme.onSurface),
            cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
            keyboardActions = KeyboardActions(onDone = { commit() }),
            modifier = Modifier.weight(1f).focusRequester(focusReq)
                .onPreviewKeyEvent { esc(it) }
                .onFocusEvent { textFocused = it.isFocused }
                .border(1.dp, MaterialTheme.colorScheme.primary, RoundedCornerShape(10.dp))
                .background(MaterialTheme.colorScheme.surface, RoundedCornerShape(10.dp)),
            decorationBox = { inner ->
                Box(Modifier.padding(horizontal = 12.dp, vertical = 9.dp)) {
                    if (text.isEmpty()) Text("要做什么", fontSize = 15.sp, color = text3Color())
                    inner()
                }
            }
        )
        Box(Modifier.width(74.dp).heightIn(min = 38.dp)
            .clip(RoundedCornerShape(10.dp))
            .border(1.dp, MaterialTheme.colorScheme.primary, RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surface, RoundedCornerShape(10.dp))
            .clickable { picking = true }
            .semantics { contentDescription = "选择时间" },
            contentAlignment = Alignment.Center) {
            Text(if (at.isEmpty()) ScheduleBoard.TIME_PLACEHOLDER else at,
                fontSize = 15.sp, fontFamily = FontFamily.Monospace, letterSpacing = 0.4.sp,
                color = if (at.isEmpty()) text3Color() else MaterialTheme.colorScheme.onSurface)
        }
    }
    if (picking) {
        val (h0, m0) = parseHm(at)
        var wh by remember { mutableStateOf(h0) }
        var wm by remember { mutableStateOf(m0) }
        AlertDialog(onDismissRequest = { picking = false },
            confirmButton = {
                TextButton(onClick = {
                    picking = false
                    setTime("%02d:%02d".format(wh, wm))
                }) { Text("确定") }
            },
            dismissButton = {
                TextButton(onClick = { picking = false; setTime("") }) { Text("清除") }
            },
            text = {
                Row(Modifier.fillMaxWidth().padding(vertical = 4.dp),
                    horizontalArrangement = Arrangement.Center,
                    verticalAlignment = Alignment.CenterVertically) {
                    WheelColumn(count = 24, value = wh, onSelect = { wh = it })
                    Text(":", fontSize = 20.sp, fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface,
                        modifier = Modifier.padding(horizontal = 8.dp))
                    WheelColumn(count = 60, value = wm, onSelect = { wm = it })
                }
            })
    }
}

/** 上下滚轮的一列：5 行窗口、正中是当前值；按住上下拖就换数，到头停住不循环。
 *  不用平台 NumberPicker：它的配色跟着系统主题走，在我们这层深色弹窗里会糊成一片；
 *  纯 Compose 自己画，颜色全部走 colorScheme，双端主题一致性由现有令牌保证。 */
@Composable
private fun WheelColumn(count: Int, value: Int, onSelect: (Int) -> Unit) {
    val itemH = 34.dp
    val scheme = MaterialTheme.colorScheme
    // 拖动起点必须在整段手势里钉死：value 每变一次都会重组，闭包直接捕获它
    // 会把起点越拖越远；rememberUpdatedState 让 down 那一刻才读最新值。
    val latest by rememberUpdatedState(value)
    Column(Modifier.width(64.dp).height(itemH * 5)
        .clip(RoundedCornerShape(10.dp))
        .background(scheme.surfaceVariant)
        .pointerInput(count) {
            awaitEachGesture {
                val down = awaitFirstDown(requireUnconsumed = false)
                val startY = down.position.y
                val startV = latest
                val px = itemH.toPx()
                while (true) {
                    val ev = awaitPointerEvent()
                    val c = ev.changes.firstOrNull { it.id == down.id } ?: break
                    if (!c.pressed) break
                    val steps = ((startY - c.position.y) / px).roundToInt()
                    onSelect((startV + steps).coerceIn(0, count - 1))
                }
            }
        }, horizontalAlignment = Alignment.CenterHorizontally) {
        for (off in -2..2) {
            val v = value + off
            Box(Modifier.height(itemH).fillMaxWidth(), contentAlignment = Alignment.Center) {
                if (v in 0 until count) Text("%02d".format(v),
                    fontSize = if (off == 0) 19.sp else 14.sp,
                    fontWeight = if (off == 0) FontWeight.SemiBold else FontWeight.Normal,
                    color = if (off == 0) scheme.onSurface
                        else scheme.onSurfaceVariant.copy(
                            alpha = if (off == 1 || off == -1) 0.75f else 0.4f))
            }
        }
    }
}

/** 逐行进场：40ms 一档、最多 200ms，与网页 nth-child 的 delay 表同一组数值。 */
@Composable
private fun Staggered(index: Int, content: @Composable () -> Unit) {
    var shown by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        delay((index * 40).coerceAtMost(200).toLong())
        shown = true
    }
    Column(Modifier.alpha(if (shown) 1f else 0f)
        .offset(y = if (shown) 0.dp else 6.dp)) { content() }
}

/** .sched-foot：dirty 提示 + 「保存」。bg-soft 那一层，与清单区用一条上边线切开。 */
@Composable
private fun SchedFoot(dirty: Boolean, saving: Boolean, savedTick: Int, onSave: () -> Unit) {
    var savedFlash by remember { mutableStateOf(false) }
    LaunchedEffect(savedTick) {
        if (savedTick > 0) {
            savedFlash = true
            delay(1600)
            savedFlash = false
        }
    }
    Row(Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surfaceVariant)
        .border(1.dp, MaterialTheme.colorScheme.outline.copy(alpha = 0f))
        .padding(horizontal = 16.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween) {
        if (savedFlash) {
            Text("已保存", fontSize = 12.5.sp, letterSpacing = 0.4.sp,
                color = MaterialTheme.colorScheme.primary)
        } else {
            Text(if (dirty) "● 有未保存修改 · 保存将整日替换" else "尚无修改",
                fontSize = 12.5.sp, letterSpacing = 0.4.sp,
                color = if (dirty) accent2() else text3Color())
        }
        val enabled = dirty && !saving
        Box(Modifier.clip(RoundedCornerShape(999.dp))
            .alpha(if (enabled) 1f else 0.35f)
            .background(MaterialTheme.colorScheme.primary, RoundedCornerShape(999.dp))
            .then(if (enabled) Modifier.clickable(onClick = onSave) else Modifier.focusable())
            .padding(horizontal = 26.dp, vertical = 10.dp)) {
            Text(if (saving) "保存中…" else "保存", fontSize = 14.5.sp,
                fontWeight = FontWeight.SemiBold, letterSpacing = 0.5.sp,
                color = MaterialTheme.colorScheme.onPrimary)
        }
    }
}

@Composable
private fun accentSoft(): androidx.compose.ui.graphics.Color =
    if (isWebLight()) WebTokens.LAccentSoft else WebTokens.AccentSoft

@Composable
private fun accent2(): androidx.compose.ui.graphics.Color =
    if (isWebLight()) WebTokens.LAccent2 else WebTokens.Accent2
