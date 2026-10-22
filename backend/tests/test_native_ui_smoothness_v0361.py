"""v0.36.1「首帧卡顿 + 切会话竞态」的源码形状锁。

射程（对应规划员定位并核实过的根因，一条根因至少一把锁）：
1. SettingsUi：进程级 SettingsCache 按 owner 隔离；memoryCount/scheduleVal/cacheBytes
   用缓存值起算（命中缓存首帧画旧真值，不再走"计算中…"）；models+memory 并行拉；
   记忆页不许「loading=true 且空列表」的白屏首帧；模型服务页重进先画缓存、
   骨架只属于首次，后台刷新失败不清列表但错误照说。
2. ScheduleUi：逐行进场只在"该身份第一次拿到清单"播一次，重进直接显示
   （首开 28dp 留白是有意设计，反向钉住它不许被顺手删掉）。
3. SessionListUi：分组 remember(sessions)，格式化器全文件一枚复用，
   搜索每键不重建；预取接线原样保留。
4. ChatUi：切会话带请求序号（回包时号不对整包丢弃）、旧消息不挂新标题、
   加载中画一行读取提示；贴底不再是 24 帧循环（改"下一帧量一次、没贴实补一发"）；
   引擎弹窗的组合区不查 PackageManager（枚举挪进 IO 线程的 effect）。

这些判据只锁形状不锁帧率——帧率归真机测量（与 test_native_ui_performance_shape_v0360
同一口径）。断言一律打在剥掉注释后的代码文本上：注释里写"已缓存"骗不过锁。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from tests.test_apk_release_discipline import _code                       # noqa: E402

NATIVE = REPO / "android-native" / "app" / "src" / "main" / "java" / "xyz" / \
    "fenever" / "assistant" / "nativeapp"


def _settings() -> str:
    return _code(NATIVE / "ui" / "SettingsUi.kt")


def _schedule() -> str:
    return _code(NATIVE / "ui" / "ScheduleUi.kt")


def _drawer() -> str:
    return _code(NATIVE / "ui" / "SessionListUi.kt")


def _chat() -> str:
    return _code(NATIVE / "ui" / "ChatUi.kt")


def _block(code: str, anchor: str) -> str:
    """从 anchor 的首个 `{{`… 起做大括号配平，把断言收进这一块而不是整文件。"""
    at = code.find(anchor)
    assert at >= 0, f"找不到锚点 {anchor!r}"
    open_at = code.index("{", at)
    depth = 0
    for i in range(open_at, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[open_at:i + 1]
    raise AssertionError(f"{anchor!r} 的大括号没闭合")


# ---------- ① SettingsUi：缓存初值 + owner 隔离 + 并行拉取 ----------

def test_settings_cache_object_exists_and_is_owner_scoped():
    s = _settings()
    assert "private object SettingsCache {" in s, \
        "v0.36.1 的进程级设置缓存没了：设置页首帧又要从空值起算、画一帧等待态"
    assert "fun adopt(who: String) {" in s, \
        "SettingsCache 不再按 owner 认领：换身份时上一个人的条数/清单会留给下一个人"
    assert 'SettingsCache.adopt(Prefs.currentEntry()?.userId ?: "")' in s, \
        "进设置页没有先认领当前身份：owner 隔离只剩注释"


def test_first_frame_values_come_from_the_cache_not_empty():
    s = _settings()
    assert "var memoryCount by remember { mutableStateOf(SettingsCache.memoryCount) }" in s, \
        "记忆条数又用空串起算：重进设置先闪空值槽"
    assert "var scheduleVal by remember { mutableStateOf(SettingsCache.scheduleVal) }" in s, \
        "日程值槽又用空串起算：同上"
    assert "var cacheBytes by remember { mutableStateOf(SettingsCache.cacheBytes) }" in s, \
        "缓存大小又用 -1 起算：每次开设置都要看一帧「计算中…）」"
    assert "var cacheBytes by remember { mutableStateOf(-1L) }" not in s, \
        "cacheBytes 的空初值回来了"
    assert "SettingsCache.setMemoryCount(" in s, \
        "记忆条数刷新成功没经 owner 闸写回缓存（审查批：裸赋值会把旧身份的在途尾巴落给新人）"
    assert "SettingsCache.cacheBytes = b" in s, "缓存大小算完没写回进程级缓存"


def test_models_and_memory_refresh_run_in_parallel():
    s = _settings()
    body = _block(s, "coroutineScope {")
    assert "Api.models()" in body and "Api.listMemory(50)" in body, \
        "models/memory 有一路不在并行分支里：打开设置又串行等两次往返"
    assert body.count("launch {") >= 2, "并行分支只剩一条：coroutineScope 退化成摆设"


def test_memory_page_has_no_blank_loading_first_frame():
    s = _settings()
    assert "var items by remember { mutableStateOf(SettingsCache.memoryItems) }" in s, \
        "记忆页又从空列表起算了"
    assert "var loading by remember { mutableStateOf(!SettingsCache.memoryItemsLoaded) }" in s, \
        "记忆页 loading 没跟着 loaded 位起算：0 条的人重进又要白屏一帧"
    assert "var loading by remember { mutableStateOf(SettingsCache.memoryItems.isEmpty()) }" not in s, \
        "空列表当\"没加载过\"的老判据回来了：加载过且为空与首次进入分不开"
    assert "var loading by remember { mutableStateOf(true) }" not in s, \
        "「loading=true 且空列表」的首帧形状回来了"
    assert '"正在读取记忆…"' in s, "无缓存的首次读取没有可读的话，只剩空白"
    assert "SettingsCache.setMemoryItems(who, items)" in s, \
        "读取成功没经 owner 闸写回记忆列表缓存"


def test_providers_page_renders_cache_first_and_keeps_list_on_failure():
    s = _settings()
    assert "var shared by remember { mutableStateOf(SettingsCache.provShared) }" in s, \
        "模型服务页共享清单又从空列表起算：重进先闪空架"
    assert "var mine by remember { mutableStateOf(SettingsCache.provMine) }" in s, \
        "模型服务页我的清单同上"
    assert "SettingsCache.setProviders(who, shared, mine, myDefault)" in s, \
        "刷新成功没经 owner 闸写回模型服务清单缓存"
    assert '"正在读取模型服务…"' in s, "进程内首次加载没有骨架/等待态"
    err_at = s.index('模型服务没拉到')
    blk_at = s.rindex(".onFailure {", 0, err_at)
    fb = _block(s[blk_at:], ".onFailure {")
    assert "shared =" not in fb and "mine =" not in fb, \
        "后台刷新失败把已显示的清单清空了——错误要照说，缓存不许被抹"


# ---------- ② ScheduleUi：进场只播一次 ----------

def test_schedule_stagger_replays_only_on_first_reveal():
    s = _schedule()
    assert 'var animatedOwner = ""' in s, "进场门牌没了：重进日程又要播一遍入场"
    assert "Staggered(index, animate = firstReveal)" in s, \
        "清单行没有接 firstReveal：每次重进都从透明起算"
    assert "private fun Staggered(index: Int, animate: Boolean" in s, \
        "Staggered 又无条件播入场"
    assert "var shown by remember { mutableStateOf(!animate) }" in s, \
        "不播的分支没有直接落在已显示初值上"
    assert "var shown by remember { mutableStateOf(false) }" not in s, \
        "无条件 false 起算的老形状回来了"
    assert "SchedState.animatedOwner = SchedState.owner" in s, \
        "播完后没人摘门牌：下一次重进又要播"


def test_schedule_first_open_gap_is_kept():
    """首开 28dp 留白是有意设计（还没见过任何一天时不瞎猜），不许被性能批顺手删掉。"""
    assert "Spacer(Modifier.height(28.dp))" in _schedule(), \
        "首开空窗留白没了：这是有意设计，不在这批射程"


# ---------- ③ SessionListUi：分组 remember + 格式化器复用 ----------

def test_drawer_groups_are_memoized_and_formatter_is_shared():
    s = _drawer()
    assert "val groupedListAll = remember(sessions) {" in s, \
        "抽屉分组又挂在每次重组上：每敲一个搜索字都重算全表日期差"
    assert s.count("SimpleDateFormat") == 1, \
        f"格式化器应有且仅有文件级一枚，实际 {s.count('SimpleDateFormat')} 枚"
    assert 'drawerDayFmt = java.text.SimpleDateFormat("yyyy-MM-dd"' in s, \
        "复用的那枚分组格式化器不在"
    assert "val visible = sessions.filter" not in s, \
        "每键全表重建 visible 的老路径回来了"
    assert "if (visibleCount == 0) item {" in s, "空态判据没跟到分组过滤之后"


def test_drawer_prefetch_wiring_is_untouched():
    s = _drawer()
    assert "var sessions by remember { mutableStateOf(initialSessions) }" in s, \
        "预取初值接线断了"
    assert "LaunchedEffect(initialSessions) { sessions = initialSessions }" in s, \
        "预取同步接线断了"


# ---------- ④ ChatUi：切会话竞态 + 贴底循环 + 组合内不查系统服务 ----------

def test_open_session_carries_a_request_epoch():
    c = _chat()
    assert "var sessionEpoch by remember { mutableIntStateOf(0) }" in c, \
        "切会话的请求序号没了：慢回包又会落进新会话的屏幕"
    body = _block(c, "fun openSession(id: String) {")
    assert "if (epoch != sessionEpoch) return@onSuccess" in body, \
        "getSession 回包不再对号：竞态守卫失效"
    assert "if (epoch == sessionEpoch) {" in body, "失败分支没有对号守卫"
    assert "sessionLoading = true" in body, "切会话没有进入加载态"


def test_stale_messages_never_sit_under_the_new_title():
    c = _chat()
    body = _block(c, "fun openSession(id: String) {")
    assert "messages = emptyList()" in body, \
        "切换那一刻没清屏：旧消息仍挂在新标题下等回包"
    assert '"正在读取会话…"' in c, "加载中没有可读的轻量提示"
    assert "if (sessionLoading) item {" in c, "加载提示没有接进消息列表"
    assert "} else item {" in c, "空态英雄没有挂在加载态的 else 分支上（接线被改形）"


def test_settled_scroll_is_single_pass_with_one_next_frame_correction():
    c = _chat()
    assert "repeat(24)" not in c, \
        "24 帧贴底循环回来了：进会话整屏替换那一拍又要随帧连贴"
    body = _block(c, "LaunchedEffect(messages.size, streamText, streamTrace.size, streamTraceChars) {")
    assert "withFrameNanos { }" in body, "落定不再等下一帧量高矮：贴的是旧高度"
    assert "scrollToItem(t - 1, MAX_SCROLL_OFFSET)" in body, \
        "没贴实时不再补夹滚动边界"


def test_settled_bottom_correction_is_bounded_not_an_unbounded_loop():
    """审查批改判：单发补一次 → 有界短校正（最多 3 轮帧检），并静态证明它有界。

    有界的证据链三格缺一即红：① 轮数上限写死在文件级常量 =3；
    ② 落定效果里只有这一枚 repeat、且它吃的是那枚常量；③ 体内没有 while——
    计数器不随布局结果续期，绝非 24 帧循环换皮。"""
    c = _chat()
    assert "private const val MAX_BOTTOM_CORRECTIONS = 3" in c, \
        "贴底校正的轮数上限没了/被改大：校正必须是有界的 3 轮"
    body = _block(c, "LaunchedEffect(messages.size, streamText, streamTrace.size, streamTraceChars) {")
    assert "repeat(MAX_BOTTOM_CORRECTIONS)" in body, \
        "落定分支不再走有界常量：要么退回单发、要么在别处自造循环"
    assert body.count("repeat(") == 1, \
        f"落定效果里出现 {body.count('repeat(')} 枚 repeat：有界判据只许一枚"
    assert "while (" not in body and "while(" not in body, \
        "贴底效果里冒出 while：那是无界循环的形状，本批明令禁止"
    # 只在仍处于近底时才继续补：最后一项已不在视口（用户上翻）必须立刻收手
    assert "if (last == null) return@LaunchedEffect" in body, \
        "近底闸门没了：用户上翻走开后校正还在跟他抢滚动"


def test_engine_picker_does_not_query_packagemanager_in_composition():
    c = _chat()
    body = _block(c, "if (enginePickerOpen) {")
    assert "speechServiceCandidates" not in body, \
        "组合阶段又同步查 PackageManager：开弹窗那一帧被 binder 往返钉住"
    assert "val cands = engineCands ?: emptyList()" in body, "弹窗不再只读 effect 的结果"
    assert "engineCands = withContext(Dispatchers.IO) { speechServiceCandidates(ctx) }" in c, \
        "枚举没有挪进 IO 线程的 LaunchedEffect"


# ---------- ⑤ 审查批：pullFromHistory epoch 守卫 + SettingsCache owner 校验 + loaded 位 ----------

def test_pull_from_history_is_epoch_guarded():
    """审查批：pullFromHistory 也是「请求→落地」路径，必须照 openSession 写法守卫。

    断网取回通常慢——期间切会话，旧会话答案绝不能追加到新会话屏上。
    钉三件：① 请求前捕获 epoch 和 sessionId；② 回包落地前两者都必须仍匹配；
    ③ 失败分支也要守卫（旧会话的报错也不该甩在新屏上）。"""
    c = _chat()
    body = _block(c, "suspend fun pullFromHistory() {")
    assert "val epochAtRequest = sessionEpoch" in body, \
        "pullFromHistory 没有在发请求前扣 epoch：回包无法知道自己是不是过期了"
    assert "val sidAtRequest = sessionId" in body, \
        "pullFromHistory 没有在发请求前扣 sessionId：切会话后 sid 变了守卫无从比对"
    assert "if (epochAtRequest != sessionEpoch || sidAtRequest != sessionId) return" in body, \
        "成功回包落地前没有双守卫（epoch+sid）：断线取回期间切会话，旧答案仍会追加到新屏"
    assert body.count("epochAtRequest != sessionEpoch || sidAtRequest != sessionId") >= 2, \
        "失败分支没有守卫：旧会话的取回报错也会甩到新会话屏上"


def test_settings_cache_writes_are_owner_checked():
    """审查批：SettingsCache 每次写入必须比对当前 owner，不一致就丢弃。

    钉住四个带 who 的 setter 都在对象里，且调用方全部换成 setter 版本。"""
    s = _settings()
    obj = _block(s, "private object SettingsCache {")
    assert "fun setMemoryCount(who: String" in obj, "setMemoryCount owner 校验 setter 缺失"
    assert "fun setScheduleVal(who: String" in obj, "setScheduleVal owner 校验 setter 缺失"
    assert "fun setMemoryItems(who: String" in obj, "setMemoryItems owner 校验 setter 缺失"
    assert "fun setProviders(who: String" in obj, "setProviders owner 校验 setter 缺失"
    # 四个 setter 内部都必须是 "if (owner == who)" 校验
    assert obj.count("if (owner == who)") >= 4, \
        "SettingsCache setter 里有不校验 owner 的写入口"
    # 调用方：三处已知的 owner 作用域写入都换成 setter 版本
    assert "SettingsCache.setMemoryCount(cacheOwner, c)" in s, \
        "记忆条数并行拉取路径没走 owner 闸"
    assert "SettingsCache.setMemoryCount(cacheOwner, it)" in s, \
        "onCountChanged 回调没走 owner 闸"
    assert "SettingsCache.setScheduleVal(cacheOwner, it)" in s, \
        "日程值刷新没走 owner 闸"


def test_settings_cache_fields_are_volatile():
    """审查批：所有跨线程可读的字段必须 @Volatile，尤其是 cacheBytes（long，防撕裂）。"""
    s = _settings()
    obj = _block(s, "private object SettingsCache {")
    assert "@Volatile var owner" in obj, "owner 字段没有 @Volatile"
    assert "@Volatile var memoryCount" in obj
    assert "@Volatile var memoryItems" in obj
    assert "@Volatile var provShared" in obj
    assert "@Volatile var provListLoaded" in obj, \
        "provListLoaded 没有 @Volatile：loaded 位跨线程不可见"
    assert "@Volatile var memoryItemsLoaded" in obj, \
        "memoryItemsLoaded 没有 @Volatile"
    assert "@Volatile var cacheBytes" in obj, \
        "cacheBytes 没有 @Volatile：long 撕裂理论风险不许赌"


def test_providers_page_uses_loaded_bit_not_empty_heuristic():
    """审查批：provLoaded 初值必须吃 provListLoaded 位，不许再用"两个清单都不为空"判"加载过"。

    0 供应商/0 记忆的人 loaded 位也是 true，重进直接显示空态，不再闪一帧骨架。"""
    s = _settings()
    assert "mutableStateOf(SettingsCache.provListLoaded)" in s, \
        "provLoaded 初值没吃 provListLoaded 位：0 供应商的人重进要闪骨架"
    assert "SettingsCache.provShared.isNotEmpty() || SettingsCache.provMine.isNotEmpty()) }" \
        not in s, \
        "provLoaded 的空列表启发式回来了：加载过且为空被当成还没加载"
