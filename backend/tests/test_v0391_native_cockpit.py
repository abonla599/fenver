# v0.39.x 模型驾驶舱 Task 6：原生 per-provider 预算、done usage、上下文环 + 账本的形状锁。
# Task 7 追加接力队列的闸与状态机；Task 10 追加模型卡（思考三态 / 上限与预算 / 能力写口）
# 与设置页迁移的锁——见下面那两段分隔注释。
#
# 分工沿用 Task 4/5 的口径：预算/校准/分段/整理的**算式**由 Api.kt 里那几个与网页
# app.js 同一公式的纯函数承载（网页侧的行为真跑锁在 test_v0391_web_cockpit_ui.py，
# 用 node vm 直接跑那份 JS）；原生这边没有 JS 运行时，锁的是接线形状——
# 1) Prefs 的按模型预算 map 与旧键迁移；
# 2) done.usage 可选解析（旧服务端不崩）与 ChatEvent.Done 的落账字段；
# 3) 环的读数与真实发送读同一张 contextDropBySession / 同一个 pid（整理必须真少发）；
# 4) usage 只落给出发时那个会话（mine() 闸 + "sid:pid" 键）；
# 5) 设置页旧滑杆改读写 per-provider 预算，不许再回写旧全局键。
#
# 注：brief 里 `_block(api, "data class Done(")` 抓不到东西——Kotlin 的
# `data class Done(...) : ChatEvent()` 声明体里没有 "{"，配对括号抓取会一路滑进
# object Api 的整块。这里改用 _between（Done 声明到 Failed 声明之间），意图不变。
# Task 10 沿用同一条惯例：`data class ModelInfo(` 也改 _between（滑到 ModelsResult 为止）。
#
# Task 10（模型卡 / 模型管理 / 设置页迁移）另起一段：设置页那只 CtxRow 已撤，
# 原 `test_settings_ctxrow_writes_per_provider_budget` 按网页 Task 9 的同方向重钉成
# `test_settings_page_gives_up_the_budget_slider_to_the_model_card`（撤入口不撤真相）。
import re
from pathlib import Path

from tests.test_apk_release_discipline import _code

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp"
# 跨端逐字比对用的两头原料（本单只读不改）：网页那份文案与后端倍率标签的出处。
WEB_JS = REPO / "backend/app/web/static/app.js"
CREDITS_PY = REPO / "backend/app/core/credits.py"


def _t(rel):
    return (NATIVE / rel).read_text(encoding="utf-8")


def _c(rel):
    return _code(NATIVE / rel)


def _block(code, anchor):
    at = code.index(anchor)
    i = code.index("{", at)
    d = 0
    for j in range(i, len(code)):
        if code[j] == "{":
            d += 1
        elif code[j] == "}":
            d -= 1
            if d == 0:
                return code[i:j + 1]
    raise AssertionError(anchor)


def _between(code, a, b):
    i = code.index(a)
    j = code.index(b, i)
    return code[i:j]


def _call(code, anchor):
    """取 `anchor(` 的**圆括号**实参区间。

    为什么要有第二个切片器：`_block` 从锚点往后找第一个 `{`，而 `Slider(value = …,
    onValueChange = { v -> … })` 的第一个花括号是它自己 lambda 的——配对括号抓到 lambda
    结尾就收手，实参尾巴（`modifier = Modifier.semantics { … }`）落在尺子外面。
    绕法与 `_block` 同族：把目标实参挪到别的调用里、或让锚点先出现一次，这把尺看不见。
    """
    at = code.index(anchor)
    i = code.index("(", at)
    d = 0
    for j in range(i, len(code)):
        if code[j] == "(":
            d += 1
        elif code[j] == ")":
            d -= 1
            if d == 0:
                return code[i:j + 1]
    raise AssertionError(anchor)


def test_prefs_budget_map_and_migration():
    c = _c("Prefs.kt")
    assert "fun contextBudgetK(providerId: String)" in c
    assert "fun setContextBudget(providerId: String" in c
    assert "fun migrateContextBudget()" in c
    assert "context_tokens_k" in c, "旧键迁移没有读 source"
    assert "context_budget_map" in c, "预算 map 没有自己的键"
    # 迁移只搬一次：map 已存在就直接收工；provider 还没选就先留着旧键下次再搬。
    mig = _block(c, "fun migrateContextBudget()")
    assert "contains" in mig and "context_tokens_k" in mig


def test_done_usage_and_budget_reach_outbound():
    api = _c("Api.kt")
    assert "data class Usage(" in api
    done_decl = _between(api, "data class Done(", "data class Failed(")
    assert "val usage: Usage? = null" in done_decl, "Done 没带可选 usage（旧服务端兼容）"
    branch = _block(api, '"done" -> {')
    assert "usage" in branch and "as? JsonObject" in branch, \
        "done 帧没读 usage，或读法会在缺键/畸变时抛"
    ob = _block(api, "fun outbound(")
    assert "budgetK" in ob
    assert "* 1000" in ob, "预算 K 与估算 token 原值之间必须有显式 ×1000"
    assert "contextTokensK: Int" not in api, "outbound 还挂着旧全局键的形参名"
    # 环/账本用的三个纯函数必须住在 Api（与网页同一公式），ChatUi 只调用不自造。
    assert "fun calibrateUsedK(" in api and "fun dropOldestToBudget(" in api
    assert "fun contextSegments(" in api
    chat = _c("ui/ChatUi.kt")
    assert "Prefs.contextBudgetK(" in chat, "原生发送没走 per-model 预算"
    assert "calibrateUsedK" in chat, "原生没做 usage 校准"
    assert "Prefs.contextTokensK" not in chat, "发送还在读旧全局滑杆键"


def test_outbound_slice_and_ring_read_the_same_drop_and_pid():
    chat = _c("ui/ChatUi.kt")
    ob = _block(chat, "fun outboundMsgs(")
    assert "contextDropBySession[sessionId]" in ob and ".drop(" in ob, \
        "发送没有按会话切掉轻量整理的条数"
    assert "Prefs.contextBudgetK(" in ob and "modelCapK()" in ob
    assert "Api.outbound(" in ob
    ctx = _block(chat, "fun clientCtx(")
    assert "contextDropBySession[sessionId]" in ctx and ".drop(" in ctx, \
        "环读数与发送 offset 不同源：整理就成只改显示的假功能"
    rs = _block(chat, "fun streamInto(")
    assert "val pid = currentProvider()?.id" in rs
    assert "outboundMsgs(pid" in rs, "outboundMsgs 没用发送那一个 pid：口径能分叉"
    assert "chatPayload(pid, pid" in rs


def test_done_usage_is_booked_to_the_session_that_sent():
    chat = _c("ui/ChatUi.kt")
    rs = _block(chat, "fun streamInto(")
    assert "sidAtSend" in rs and "clientKAtSend" in rs, "发送时没捕获会话/估算锚点"
    done = _block(chat, "is ChatEvent.Done -> {")
    assert "ev.usage" in done and "lastUsageByKey" in done, "done 的 usage 没落账"
    assert "mine()" in done, "落账没有核对会话仍是出发时那一个"
    assert "lastClientKByKey" in done, "校准锚点 clientK 没和 usage 一起落"
    assert '"$sidAtSend:' in chat, "账本键不是「会话:模型」形状"
    # finally 的收尾不依赖 usage：usage 只该在 Done 分支被消费这一次，
    # 缺 usage 的旧服务端照常收尾（反向钉就靠这个计数）。
    assert chat.count("ev.usage") == 1, "usage 只该在 Done 分支被消费一次"


def test_native_ring_and_panel_compose():
    chat = _c("ui/ChatUi.kt")
    assert "private fun ContextRing(" in chat
    ring = _block(chat, "private fun ContextRing(")
    assert "drawArc" in ring and "contentDescription" in ring
    assert "size(24.dp)" in ring, "环按 brief 是 24dp"
    assert '"上下文用量 $pct%' in chat, "环的无障碍标签没带百分比"
    card = _block(chat, "private fun InputCard(")
    assert card.count("ContextRing(") >= 2, "收起/展开两行的模型按钮旁都要有环"
    assert "ModelChip(" in card
    assert card.index("ContextRing(") < card.index("ModelChip("), "环排在芯片前（同网页）"
    panel = _block(chat, "private fun ContextPanel(")
    for needle in ("轻量整理", "恢复", "Prefs.setContextBudget(", "Slider(", "Text("):
        assert needle in panel, f"账本缺件：{needle}"
    assert "Api.dropOldestToBudget(" in chat, "整理没走 Api 的同一条截断算式"
    info = _block(chat, "fun contextRingInfo(")
    assert "* 1000" in info and "/ 1000" in info, "K 与 token 原值必须显式换算"
    assert "Api.contextSegments(" in info
    assert "Prefs.migrateContextBudget()" in _block(chat, "suspend fun loadModelsNow()")


def test_native_relay_queue_state_and_guards():
    c = _c("ui/ChatUi.kt")
    assert "data class QueuedMsg(" in c
    send = _block(c, "fun sendNow(text: String) {")
    assert "if (busy) {" in send and "queues[" in send, "忙时发送没入队"
    assert "queuePaused[" in c and "canRelay" in c, "队列没有暂停/会话/前台闸"
    assert "maybeRelay(" in c, "流结束没有接力入口"
    assert "resetQueuesForIdentity" in c or "queues.clear()" in c, "换身份没清队列"


def test_native_relay_queue_data_shape_and_limits():
    c = _c("ui/ChatUi.kt")
    at = c.index("data class QueuedMsg(")
    decl = c[at:at + 160]
    for f in ("val id: String", "val text: String",
              "val attachments: List<AttItem>", "val k: Int"):
        assert f in decl, f"QueuedMsg 缺字段：{f}"
    assert "private const val QUEUE_MAX_ITEMS = 20" in c
    assert "private const val QUEUE_MAX_CHARS = 20_000" in c
    assert 'private const val QUEUE_DRAFT_KEY = "__draft__"' in c
    qk = c[c.index("private fun queueKeyOf("):]
    assert "sid.ifEmpty { QUEUE_DRAFT_KEY }" in qk[:120], "草稿哨兵没接进 queueKeyOf"
    # canRelay 是文件级纯函数：六闸一个都不能少（静态测试要能找到它）
    cr = c[c.index("private fun canRelay("):]
    expr = cr[:cr.index("\n\n")] if "\n\n" in cr[:400] else cr[:400]
    for gate in ("normal", "sameSession", "foreground", "!paused", "!busy", "hasItem"):
        assert gate in expr, f"canRelay 缺闸：{gate}"


def test_native_relay_queue_enqueue_stop_and_normal_gates():
    c = _c("ui/ChatUi.kt")
    send = _block(c, "fun sendNow(text: String) {")
    busy_branch = _block(send, "if (busy) {")
    assert "QUEUE_MAX_ITEMS" in busy_branch and "接力队列已满" in busy_branch, \
        "队满没提示或没按 20 条上限拦"
    assert 'queues[' in busy_branch and "QueuedMsg(" in busy_branch
    st = _block(c, "fun stopNow() {")
    assert "queuePaused[queueKeyOf(sessionId)] = true" in st, "停止没暂停本会话队列"
    # streamInto 末尾经 afterStreamEnd 触发接力；normal 必须排除停止/失败/取回
    assert "var afterStreamEnd: (String, Int, Boolean) -> Unit" in c
    assert c.index("var afterStreamEnd") < c.index("fun streamInto("), \
        "局部变量不能前向引用：afterStreamEnd 必须声明在 streamInto 之前"
    rs = _block(c, "fun streamInto() {")
    assert "afterStreamEnd(sidAtSend, epochAtSend, normal)" in rs
    n_at = rs.index("val normal =")
    n_line = rs[n_at:rs.index("\n", n_at)]
    assert "!failed" in n_line and "!stopped" in n_line and "!needHistory" in n_line, \
        "normal 把停止/失败/断线取回也算正常收尾"


def test_native_relay_relay_and_force_next():
    c = _c("ui/ChatUi.kt")
    mr = _block(c, "fun maybeRelay(")
    assert "canRelay(" in mr, "maybeRelay 没走那条纯函数"
    assert "delay(300)" in mr
    assert "sendingQueueId" in mr and "supports_vision" in mr
    assert "relayImageBlocked(" in mr, "队首带图没先过视觉闸"
    assert "currentProvider() != null" in mr, "接力前没同步确认有可用模型"
    # 立即插话：对不上号的那一条要回队首并暂停，不能丢
    assert "ForceNext(" in c
    assert "queueKeyOf(fn.sid)" in c
    assert "listOf(fn.item) +" in c, "回队不是 unshift 形状：这条会排到最后或丢掉"
    # 插话前已存在的旧 forceNext 覆盖语义：先占位后停流
    fn_at = c.index("forceNext = ForceNext(")
    stop_at = c.index("stopNow()", fn_at)
    assert fn_at < stop_at, "forceNext 必须在 stopNow 之前落位，否则收尾读不到"


def test_native_relay_draft_echo_and_strip_ui():
    c = _c("ui/ChatUi.kt")
    send = _block(c, "fun sendNow(text: String) {")
    assert "val echo = t == input.text.trim()" in send, "没判正文是否就是输入框内容"
    assert 'if (echo) input = TextFieldValue("")' in send, \
        "无条件清空输入框：接力发送会抹掉用户新写的草稿"
    assert "Web" not in _block(c, "private fun QueueStrip("), \
        "队列条不许 WebView/拼 HTML"
    strip = _block(c, "private fun QueueStrip(")
    for needle in ("条待发送", "Text(", "BasicTextField(", "立即插话", "暂停"):
        assert needle in strip, f"QueueStrip 缺件：{needle}"
    assert "maxLines = 2" in strip
    assert "if (queueItems.isNotEmpty())" in c, "空队列整块不该挂"


def test_native_relay_identity_and_ring_remember():
    c = _c("ui/ChatUi.kt")
    assert "val queues = remember(identityId) { mutableStateMapOf<String, List<QueuedMsg>>() }" in c
    assert "fun resetQueuesForIdentity()" in c
    le = _block(c, "LaunchedEffect(identityId)")
    assert "resetQueuesForIdentity()" in le
    # Task 6 遗留收口：环读数从每拍组合改 remember，且队列 K 单独一行不进 usedK
    assert "val ringInfo = remember(messages, input.text" in c
    assert "queuedK" in c and "待发 +" in c
    ring_body = _block(c, "fun contextRingInfo(")
    assert "queues[" not in ring_body, "queuedK 混进了环读数公式：usedK/pct 不许动"


def test_settings_page_gives_up_the_budget_slider_to_the_model_card():
    """Task 10 设置页迁移（与网页 Task 9 同方向重钉，语义没降）。

    原锁 `test_settings_ctxrow_writes_per_provider_budget` 守的是"设置页那只滑杆不许
    再写旧全局键"。这一单把滑杆从设置页撤了，原句里那三条 CtxRow 判据随之失去对象；
    撤入口不撤真相，所以判据翻成两面：
    ① 设置页不再重复一份预算滑杆/上限输入（两个真相的源头）；
    ② 预算的真相仍只有一份，住在 Prefs，模型卡与上下文面板都从它读写。
    """
    st = _c("ui/SettingsUi.kt")
    assert "Prefs.contextTokensK" not in st, "设置页还在读写旧全局键"
    assert "private fun CtxRow(" not in st, "设置页还在重复上下文滑杆"
    assert "fCtxK" not in st, "模型服务表单还在重复模型上限"
    assert "Prefs.setContextBudget(" not in st, "预算写口没收成模型卡/账本一处"
    assert "模型上限与思考能力在「选择模型」里管理" in st, "撤了入口没留指路那句"
    # 反向钉：撤的是入口，不是真相——Prefs 与 Api/ChatUi 那条链路必须原样在位
    pf = _c("Prefs.kt")
    assert "fun contextBudgetK(providerId: String)" in pf
    assert "fun setContextBudget(providerId: String" in pf
    assert "fun contextBudgetWantedK(" in pf, "夹紧判据没有原始读数可取（会恒不成立）"
    assert "Prefs.contextBudgetK(" in _c("ui/ChatUi.kt")
    assert "budgetK" in _c("Api.kt"), "outbound 不再吃 per-provider 预算：发送与界面分叉"
    # 连接配置一格不许跟着能力字段一起被撤掉（spec §4.2「设置页的边界」）
    for keep in ("Base URL", "API Key", "supports_vision", "测试连通"):
        assert keep in st, f"撤能力入口时顺带弄丢了连接配置：{keep}"


# ---------------- Task 10：安卓模型卡 / 模型管理 / 思考偏好（2026-10-31） ----------------
# 全部是 source-shape 锁（Kotlin 侧没有 JS 那样的真跑宿主）：判"接线形状"，不判行为。
# 每条 docstring 写明「改坏什么就会红」与射程边界，能绕过的一律写在射程里。

def test_native_model_card_and_settings_move():
    """brief Step 1 逐字收进本文件（五格 / 卡吃三态 / 预算同源 / 设置页撤件）。
    唯一改动：ModelInfo 那一处的锚点从 _block 换成 _between，理由见本文件开头
    （Kotlin 的 data class 声明体没有 `{`，配对括号会滑进 object Api 整块）。"""
    api = _c("Api.kt")
    mi = _between(api, "data class ModelInfo(", "data class ModelsResult(")
    for key in ("supports_thinking", "thinking_levels", "thinking_default",
                "thinking_status", "can_edit"):
        assert key in mi, f"ModelInfo 缺 {key}"
    chat = _c("ui/ChatUi.kt")
    assert "ModelPickerScreen(" in chat
    assert "thinking_status" in _block(chat, "private fun ModelPickerScreen("), \
        "模型卡没渲染思考三态"
    assert "Prefs.contextBudgetK(" in chat
    settings = _c("ui/SettingsUi.kt")
    assert "private fun CtxRow(" not in settings, "设置页还在重复上下文滑杆"
    assert "fCtxK" not in settings, "模型服务表单还在重复模型上限"


def test_modelinfo_five_fields_all_have_defaults_old_server_survives():
    """五格必须**带默认值**——旧服务端 /v1/models 没有这些键，缺省解崩就是全量闪退。

    变异：把任一格的 `= …` 去掉 → 该格红。判据是"逐格都有 `=`"，不是"格子存在"，
    因为存在但无默认值的那一份照样崩（与 Done.usage 同一容错纪律）。
    射程：只看声明那一行区间里的 `名字: 类型 = 默认`；把默认值写成运行期才可用的
    表达式（例如引用另一个构造参数）这把尺看不见——那类写法 Kotlin 会在构造期报错，
    由 compileDebugKotlin 兜住，不在静态锁的眼里。
    锚点：brief 那段写的是 `_block(api, "data class ModelInfo(")`，Kotlin 的 data class
    声明体里没有 `{`，配对括号抓取会一路滑进 `object Api` 整块（与本文件开头为
    `data class Done(` 记的同一条陷阱），所以这里同样改用 _between，意图不变。
    """
    api = _c("Api.kt")
    mi = _between(api, "data class ModelInfo(", "data class ModelsResult(")
    for sig in ('supports_thinking: Boolean = false',
                'thinking_levels: List<String> = emptyList()',
                'thinking_default: String = ""',
                'thinking_status: String = "unsupported"',
                'can_edit: Boolean = false'):
        assert sig in mi, f"ModelInfo 缺「{sig}」：缺字段或没带默认值（旧服务端会崩）"
    # 缺省态只能是"不支持 + 不能改"，不能是"支持但未核实"这种替服务端说话的值
    assert 'thinking_status: String = "unsupported"' in mi
    assert "can_edit: Boolean = false" in mi


def test_picker_takes_modelinfo_list_not_id_name_pairs():
    """调用处必须交整份 ModelInfo：Pair(id, name) 那份装不下三态/上限/倍率。

    变异：把 `models = providers.filter { it.usable }` 改回 `.map { it.id to it.name }`
    → 第一条红（形参类型对不上，第二条也读不到那份 filter）。
    """
    chat = _c("ui/ChatUi.kt")
    assert "private fun ModelPickerScreen(models: List<ModelInfo>" in chat
    assert "models = providers.filter { it.usable }" in chat
    assert "it.id to it.name" not in chat, "模型行还只交 id/名字：卡片无从渲染"
    # 上限与模型来源不许另起炉灶：currentProvider() 是唯一口径（Task 6/7 收口过）
    at = chat.index("if (modelMenuOpen) {")
    call = chat[at:at + 700]
    assert "currentProvider()?.id" in call, "卡片当前项另挑了一条就会与实发分叉"


def test_picker_rows_and_card_render_thinking_tri_state_and_disable_branch():
    """三态文案不许说谎：判据只认服务端 thinking_status，未核实的那一支必须禁用+给原因。

    变异：① 把 'unverified' 那句文案改成「可切换思考强度」→ 第一条红（界面替服务端
    拔高）；② 删掉 `clickable(enabled = ctlEnabled`（禁用态改成永远可点）→ 第三条红；
    ③ 把 reason 那一句撤掉 → 第四条红（禁用不说为什么就是"控件坏了"）；
    ④ 把「本机偏好」那句里的逗号改回顿号（上一版的实测形状）→ 最后一条红（跨端逐字比对）。
    射程：只判"这些字符串与这道门在代码里"，不判运行时真的点不动——真机点按留在手测
    清单；把 enabled 判据换成 `status != "unsupported"` 这种"未核实也能点"的写法，
    本锁看不见字符串以外的等价物，所以同锁钉住 `ctlEnabled` 只能是 `== "verified"`。
    """
    chat = _c("ui/ChatUi.kt")
    label = _block(chat, "private fun thinkingStatusLabel(")
    assert 'verified' in label and 'unverified' in label
    assert "可切换思考强度" in label
    assert "支持思考（参数未核实，暂时不能开）" in label, "unverified 文案与网页不同尺"
    assert "不支持思考" in label
    reason = _block(chat, "private fun thinkingStateReason(")
    assert "参数配方还未核实" in reason and "这个模型没有思考能力" in reason
    page = _block(chat, "private fun ModelPickerScreen(")
    assert "thinking_status" in page and "thinkingStatusLabel(" in page
    card = _block(chat, "private fun ModelCard(")
    assert 'val ctlEnabled = item.thinking_status == "verified"' in card, \
        "三态门换了判据（unverified 也给了点就是界面在说谎）"
    assert "enabled = ctlEnabled" in card, "禁用分支只剩文案没有门"
    pill = _block(chat, "private fun ModelMiniPill(")
    assert "clickable(enabled = enabled" in pill, "小胶囊钮的 enabled 没接到 clickable：禁用是假的"
    assert "thinkingStateReason(" in card, "禁用了没给原因"
    assert "Prefs.thinkingEnabled(" in card and "Prefs.setThinkingEnabled(" in card
    assert "Prefs.thinkingLevel(" in card and "Prefs.setThinkingLevel(" in card
    # 今天不落进请求体（Task 12/13 的活）：界面不许出现"已生效/已发送"那种话
    assert "本轮会带上" in card, "verified 那一支的措辞必须与网页同句（今天不可达）"
    # 修复轮 1 的 Minor 8：报告 §2 写的是"同句"，实测差一个标点（原生用顿号、网页用逗号）。
    # 这句改成**跨端逐字比对**：串从 app.js 里抠出来再要求原生卡里出现同一串，
    # 以后任何一端改措辞、改标点，这一枚先红，不必再靠人来对眼。
    js = WEB_JS.read_text(encoding="utf-8")
    at = js.index("开关与档位是这台设备自己的偏好")
    shared_sentence = js[at:js.index("换设备不同步", at) + len("换设备不同步")]
    assert shared_sentence in card, \
        "「本机偏好」这句两端逐字不同（按网页为准改原生，网页那边别动）"


def test_card_budget_slider_shares_the_panel_map_and_can_edit_gates_cap_only():
    """预算与上限分家：滑杆读写同一张 map，`can_edit` 只闸上限、不闸预算。

    变异：① 卡片自己 `sp.putString("card_budget…")` 另存一份 → 第二条红；
    ② 把预算滑杆整段挪进 `if (canEdit)` 里（写口在 canEdit 之后先出现）→ 第三条红；
    ③ 让 can_edit=false 的人也能改上限（把上限输入挪到 canEdit 之前）→ 第四条红。
    射程：按"两个写口在函数体里的先后"判归属，不是判嵌套——把闸门外层的 if 删掉但保留
    语句顺序这一条骗得过第二条断言，所以同锁另外钉住只读那句原话必须在 canEdit 之后
    出现（真删闸门会先撞那条）。真正的越权拒绝由服务端二验兜着（本单不改后端）。
    """
    chat = _c("ui/ChatUi.kt")
    card = _block(chat, "private fun ModelCard(")
    assert "Prefs.contextBudgetK(" in card and "Prefs.setContextBudget(" in card
    assert "sp.putString" not in card and "localStorage" not in card, "卡片另存一份预算"
    # 门用 `if (canEdit)` 那一处（不是 `val canEdit = …` 那行声明）：声明在预算之前是
    # 正常的取值顺序，只有把预算滑杆塞进那道分支才是"共享模型连自己那一栏也改不了"。
    assert card.index("Prefs.setContextBudget(") < card.index("if (canEdit)"), \
        "预算写口排到了 can_edit 分支之后：共享模型的用户被自己改不了的那一栏连坐了"
    gate = card[card.index("if (canEdit)"):]
    assert "onSaveCaps(" in gate, "上限/能力的写口排到了 canEdit 分支之前：只读成了摆设"
    assert "模型上限" in gate and "本会话预算仍然归你改" in gate, "只读那句实话没写全"
    # 与上下文面板同源：面板仍在，且读写同一对函数（撤设置页不撤面板）
    panel = _block(chat, "private fun ContextPanel(")
    assert "Prefs.setContextBudget(" in panel


def test_cap_lowering_clamps_budget_and_says_so_once():
    """上限夹小时必须吭一声（spec §3.2「不静默保留越界值」，网页 Task 9 同一条）。

    变异：① 把判据 `stored > capK` 换成用读数比（读数已夹过，恒不成立）→ 第一条红
    （这正是网页 Task 9 踩过的坑，这里钉的是"取原始值"那个函数名）；
    ② 删掉那句提示 → 第二/三条红；③ 夹了不写回 map → 第四条红。
    射程：提示"一次"清在 onClose/换身份两处（修复轮起这三处出口共用 closeModelMenu），
    静态只判"这两句都存在"；渲染时机与真的只印一次要靠真机手测（清单在报告里）。
    **"这句提示到底能不能被用户看见"不由本锁管**——上一版就是在这里以为"进了状态条
    就一定看得见"，实际同帧被 `setStatus("")` 冲掉；那一条由
    `test_i2_clamp_note_survives_the_status_clear` 钉住。
    """
    chat = _c("ui/ChatUi.kt")
    rec = _block(chat, "fun reconcileBudgetCap(")
    assert "Prefs.contextBudgetWantedK(" in rec, "拿夹过的读数比上限：那条分支恒不成立"
    assert "stored > capK" in rec
    assert "Prefs.setContextBudget(" in rec, "夹了不写回：本机那份越界值一直躺着"
    assert "budgetClampNote(" in chat
    note = _block(chat, "private fun budgetClampNote(")
    assert "上限降到了" in note and "已经按上限夹小" in note
    assert "budgetClamped = null" in chat, "提示没有「只一次」的收口"


def test_capability_put_body_comes_from_the_full_record():
    """PUT 是全量语义：body 的每一格从 /v1/me/providers 那条完整记录取。

    变异：① 把 `rec` 换成 /v1/models 的 ModelInfo（那里没有 base_url）→ 第一/二条红；
    ② 补一行 `put("paid_by", …)` 或 `put("id", …)` → 反向断言红（前者 ProviderRequest
    没这一格、留着只让下一个人以为界面改得动；后者路由按 path 钉这一条）；
    ③ 把 api_key 那行删了 → 第三条红（服务端按占位符保原密钥，缺键就是洗密钥）；
    ④ scope 分流改成固定一面 → 第六条红（共享条目会打到属主门上被拒）。
    射程：只判"这些键在不在那个月亮符区间里"，不判真发出去的报文——真实 PUT 需要登录
    态与服务端，本单没跑（报告里登记）。
    """
    chat = _c("ui/ChatUi.kt")
    body = _block(chat, "private fun capabilityBody(")
    for keep in ('put("api_key", "")', "is_default", "base_url", "supports_vision",
                 "max_context_k", "thinking_profile", "thinking_levels", "label"):
        assert keep in body, f"全量 PUT 少了一格：{keep}"
    assert "paid_by" not in body, "paid_by 不该由界面发（ProviderRequest 没这一格）"
    assert '"id"' not in body, "PUT body 不许带 id：路由按 path 钉这一条"
    assert "ProviderRequest" not in body
    scope = _block(chat, "private fun providerCapabilityScope(")
    assert 'shared' in scope and '"admin"' in scope and '"mine"' in scope
    save = _block(chat, "fun saveModelCapabilities(")
    assert "providerRecords[" in save, "写口的记录来源不是那份完整记录缓存"
    assert "capabilityBody(rec" in save
    assert "Api.updateProvider(" in save and "Api.updateMyProvider(" in save
    loader = _block(chat, "suspend fun loadProviderRecordsNow(")
    assert "Api.myProviders()" in loader, "记录不是来自 /v1/me/providers"
    for section in ('"shared"', '"mine"'):
        assert section in loader, f"没把 {section} 那一栏并进缓存"
    assert "remember(identityId) { mutableStateOf(mapOf<String, JsonObject>()) }" in chat, \
        "记录缓存不随身份复位：换账号会拿着上一个人的 base_url 往新账号的门里 PUT"


def test_thinking_prefs_are_local_per_identity_with_honest_defaults():
    """思考偏好住本机、按「用户 + provider」一格；enabled 默认 false；档位不落空。

    变异：① 内键去掉 userId 前缀（`providerId` 单键）→ 第二条红（A 的开关跟着 B 回来）；
    ② enabled 默认值改成 true → 第三条红（不给全站悄悄改计费行为）；
    ③ `thinkingLevel` 在候选里没有默认档时返回 ""（"有档位但没默认档"那个存不下状态）
    → 第五条红。
    射程：判的是键形与缺省那几串，不判 SharedPreferences 真的按身份分格读写；
    真跑需要 Android 运行时，本机没有宿主（报告里登记为未证实）。
    """
    pf = _c("Prefs.kt")
    assert "fun thinkingEnabled(providerId: String)" in pf
    assert "fun thinkingLevel(providerId: String" in pf
    assert "fun setThinkingEnabled(providerId: String" in pf
    assert "fun setThinkingLevel(providerId: String" in pf
    assert "thinking_prefs_map" in pf, "思考偏好没有自己的 map 键"
    assert '"$uid:$providerId"' in pf, "内键不是「用户:provider」形状：换身份会串"
    assert "val enabled: Boolean = false" in pf
    # 档位归一只有一处：候选里有默认档用默认档，否则落第一档（后端同一口径）
    lvl = _block(pf, "fun thinkingLevel(")
    assert "levels.contains(saved)" in lvl
    assert "levels.contains(thinkingDefault)" in lvl and "levels.first()" in lvl, \
        "档位回落不是「默认档→第一档」：界面上会冒出一个本机存不下的空档位"
    assert 'return ""' in lvl, "没有档位时该给空串，不该编一个假的"
    # 键形只有两套：预算 map 与思考 map（不许自创第三套按 provider 的散键）
    for stray in ('"thinking_" + providerId', '"thinking:"', 'providerId + "-thinking"'):
        assert stray not in pf, f"自创键形：{stray}"


def test_level_labels_survive_spaces_and_are_not_truncated_locally():
    """档位标签：分隔判据与网页/后端同尺（空格不是分隔符），本地不截断（交服务端拒收）。

    变异：把分隔符退回含 `\\s` 的那一份（"快速 思考" 被劈成两档）→ 第二条红；
    加一句 `.take(3)`（界面悄悄砍掉第 4 档，把"没配上"演成"配上了"）→ 第三条红。
    """
    chat = _c("ui/ChatUi.kt")
    p = _block(chat, "private fun parseThinkingLevels(")
    assert "Regex(" in p or "split(" in p
    assert "，、;" in p or "\\uFF0C" in p, "分隔符集合里没有全角逗号/顿号"
    assert "\\s" not in p, "把空白当分隔符会劈开带空格的档位标签（网页 Task 9 同一条）"
    assert "take(3)" not in p and "drop(3)" not in p, "本地截断=替服务端作答"


def test_native_preset_cap_travels_only_with_a_new_draft():
    """预置上限（Task 9 的 I-2）：原生确实有「选预置新建」这条路，必须只在新建时走一次。

    变异：① 条件放宽成只看预置（编辑态也发这一格）→ 第一条红（编辑会把模型卡里的
    1000K 洗成预置那份）；② 新建时干脆不发 → 第二条红（选 DeepSeek 预置新建的
    1000K 落回服务端缺省 64K）；③ 表单又加回一个能改上限的输入框 → 第三条红
    （两个真相）。
    射程：判的是 draft() 那一行的形状与 openForm 里的复位；真实"点预置→保存→卡片读数"
    需要登录态与服务端，本单没跑（列进手测清单）。
    """
    st = _c("ui/SettingsUi.kt")
    draft = _block(st, "fun draft(): JsonObject")
    assert "editingId.isEmpty() && presetCtxK != null" in draft, \
        "预置上限的条件不是「新建 + 点过预置」：编辑路径会洗掉模型卡那份"
    assert 'put("max_context_k"' in draft, "新建时不带预置上限 = 1000K 被洗成 64K"
    assert "fCtxK" not in st, "表单还留着能改上限的输入框"
    assert "上下文上限（K token）" not in st, "上限输入项没撤干净"
    form = _block(st, "fun openForm(")
    # 修复轮 1 的 Minor 3：`presetCtxK = ` 这种前缀判据把 `presetCtxK = 1000` 也算复位，
    # 而那正是这条锁该拦的形状（编辑态带着上一个人的预置数发出去）。钉**置空**这个动作本身。
    assert "presetCtxK = null" in form, "开单没把预置上限置空：上一单的数会跟着下一单发"
    assert "ps.ctxK" in _c("ui/SettingsUi.kt"), "预置那份上限的取数被删了（新建会落空）"


def test_native_card_text_is_plain_compose_text_only():
    """动态文本（模型名/档位标签/服务端报错）只走 Text(：不许 WebView、不许拼 HTML。

    变异：把某处改成 WebView/evaluateJavascript/注解字符串 → 断言红（沿用 QueueStrip
    那条同款尺子）。
    """
    chat = _c("ui/ChatUi.kt")
    for name in ("private fun ModelPickerScreen(", "private fun ModelCard("):
        block = _block(chat, name)
        assert "Text(" in block, f"{name} 没有走 Text"
        for sink in ("Web", "html", "Html", "evaluateJavascript", "AnnotatedString"):
            assert sink not in block, f"{name} 里出现了 {sink}"


# ---------------- Task 7 修复轮 1（独立审查 Changes requested，2026-10-31） ----------------
# 每条都是「把对应修法删掉/改回旧形状就会红」的静态形状锁；docstring 里写明
# 变异操作，报告要求逐条实跑证明会红。

def test_c1_force_next_send_requires_current_session_coordinates():
    """C-1（Critical）：立即插话立靶后、终帧前切会话，A 的消息会发进 B。
    变异：删掉 `fn.sid == sessionId` / `fn.epoch == sessionEpoch` 两个当前坐标
    条件（旧代码正是只剩前两个恒真条件）→ 本测试必须红。"""
    c = _c("ui/ChatUi.kt")
    ae = _block(c, "afterStreamEnd = {")
    # 对号支路：被停流的坐标（与靶同源、恒真）+ 当前屏幕坐标（真判据）缺一不可
    for needle in ("fn.sid == sid", "fn.epoch == epoch",
                   "fn.sid == sessionId", "fn.epoch == sessionEpoch"):
        assert needle in ae, f"插话放行的对号支路缺条件：{needle}"
    # 对不上当前坐标时必须仍走回队支路：放回**原会话**队列头并暂停
    assert "queueKeyOf(fn.sid)" in ae
    assert "listOf(fn.item) + rest" in ae, "回队支路不再 unshift 原会话队列"


def test_i1_ring_remember_keys_use_values_not_sizes():
    """I-1（Important）：环读数的 remember 键按值不入 size。
    变异：把任一按值键（如 ringDrop）换回 contextDropBySession.size → 本测试必须红
    （覆写既有键 size 不变，第二次轻量整理/换模型/改预算回来都会吃旧缓存）。"""
    c = _c("ui/ChatUi.kt")
    keys = _between(c, "val ringInfo = remember(", ") { contextRingInfo() }")
    for needle in ("ringProviderId", "ringBudget", "ringPersona", "ringDrop",
                   "ringUsage", "ringClientK", "ringQueue"):
        assert needle in keys, f"环 remember 键缺按值项：{needle}"
    for dead in ("lastUsageByKey.size", "contextDropBySession.size",
                 "lastClientKByKey.size"):
        assert dead not in keys, f"环键还挂着假覆盖 {dead}"


def test_m2_relay_gate_cleared_only_at_stream_end():
    """M-2：sendingQueueId 第二道闸的清除从 sendNow 后挪到每轮收尾入口。
    变异：把 `sendingQueueId = null` 挪回 maybeRelay 的 sendNow(item.text) 之后
    → tail 断言红；删掉同步段的 sessions.any → 第二条断言红。"""
    c = _c("ui/ChatUi.kt")
    ae = _block(c, "afterStreamEnd = {")
    assert "sendingQueueId = null" in ae, "收尾入口没统一清防重入闸"
    mr = _block(c, "fun maybeRelay(")
    # 同步段加纯内存判据：保证 sendNow 里 ensureSession() 立即返回 true，
    # 不在「闸已清、busy 未置真」的窗口里挂起
    assert "sessions.any { it.session_id == sessionId }" in mr, \
        "同步段没确保证 ensureSession 直回：可能走进新建会话的挂起路径"
    tail = mr[mr.index("sendNow(item.text)"):]
    assert "sendingQueueId = null" not in tail, \
        "清闸挪回 sendNow 之后：busy 置真前的窗口又敞开了"


def test_m3_afterstream_end_survives_cancellation():
    """M-3：收尾与接力触发不许被取消态截断。
    变异：把 afterStreamEnd(...) 挪回 withContext(NonCancellable) 块外 → 第一条红；
    把 runCatching { refreshTaskCard() } 还原成裸的 refreshTaskCard() → 第二条红。"""
    c = _c("ui/ChatUi.kt")
    wc = _block(c, "withContext(NonCancellable)")
    assert "afterStreamEnd(" in wc, \
        "收尾在取消态会被 refreshTaskCard 再抛的 CancellationException 截断"
    assert "runCatching { refreshTaskCard() }" in wc, \
        "任务卡刷新没包 runCatching：取消态下它自己就是那颗炸雷"
    assert wc.index("runCatching { refreshTaskCard() }") < wc.index("afterStreamEnd("), \
        "refreshTaskCard 必须排在 afterStreamEnd 之前（persist 之后顺序不变）"


def test_m4_queue_strip_editing_survives_queue_writes():
    """M-4：编辑态不再被队列写重置。
    变异：把 editingId/draft 改回挂在 items 键上 → `remember(items)` 重新出现，红；
    删掉孤儿态回收 → LaunchedEffect(items) 消失，红。"""
    c = _c("ui/ChatUi.kt")
    strip = _block(c, "private fun QueueStrip(")
    assert "remember(items)" not in strip, \
        "编辑态又挂在队列键上：接力弹出/入队/删除会清掉正在编辑的半句"
    assert "LaunchedEffect(items)" in strip, \
        "缺 items 变化后的孤儿编辑态回收（编辑中那条被删掉时要复位）"


# ---------------- Task 10 修复轮 1（独立审查 Changes requested，2026-11-01） ----------------
# 三条 Important + 一批 Minor。这一轮的锁与上一轮同族：全部 source-shape（Kotlin 侧没有
# 真跑宿主，也没有 kotlinx 的单测运行时）。每条 docstring 里写清「改坏什么会红」和
# 「哪些形状绕得过」——绕得过的一律不写成已覆盖，只登记进 task-10-fix1-report.md 的
# 未跑清单（真机 / 单测 / 服务端联调）。

def test_i1_modelinfo_nullable_cells_declare_null():
    """I-1：`ModelInfo.multiplier` 必须是可空。服务端 `credits.multiplier_label` 对
    `per_call` 定价**返回 None**，而 `Api.json` 只开了 `ignoreUnknownKeys`、没开
    `coerceInputValues` ⇒ 显式 null 撞非空 `String` 抛 JsonDecodingException ⇒
    整份 `/v1/models` 解不开 ⇒ 手机端「没有可用的模型」，选模型与预算全废。

    变异：① 把 `String? = null` 改回 `String = ""` → 第一条红；② 界面把兜底写成
    `.ifEmpty { … }`（对 null 根本不成立）→ 第四/五条红；③ 把 `priced` 也顺手改成可空 →
    第三条红（服务端恒给布尔，那格没有 null 的形状，改窄了是替服务端编造不确定性）；
    ④ 删掉 credits.py 里那条 `return None`（前提变了）→ 第六条红，届时重议这条纪律，
    不许就地删断言。
    射程：**这是形状锁**。它钉的是"那一格声明成可空 + 两处读数各自兜底"，
    **不**证明"服务端回 null 时真解得开"——那需要 kotlinx 解码的单测运行时，本仓没有，
    列进真机/单测待办。开 `coerceInputValues` 能一并绕过（但它会改全站所有 DTO 的 null
    语义，需另票，本单明确不开，故同锁钉住 json 构造体没有这个开关）。
    """
    api = _c("Api.kt")
    mi = _between(api, "data class ModelInfo(", "data class ModelsResult(")
    assert "val multiplier: String? = null" in mi, \
        "multiplier 非空：服务端对 per_call 定价回 null，整份 /v1/models 会解崩"
    assert "val multiplier: String =" not in mi, "multiplier 又变回非空 String"
    assert "val priced: Boolean = false" in mi, "priced 不会是 null，保持非空 + 默认值"
    # 同一条容错纪律在本文件的其它落点（防止只修被点名的那一格）
    mr = _between(api, "data class ModelsResult(", "data class UploadInfo(")
    assert "val default: String? = null" in mr, "ModelsResult.default 的可空形状被改了"
    assert "coerceInputValues" not in _block(api, "private val json = Json {"), \
        "不许用 coerceInputValues 一把糊：那会改全站所有 DTO 的 null 语义（另票）"
    chat = _c("ui/ChatUi.kt")
    assert "multiplier.ifEmpty" not in chat, "对可空格用 ifEmpty：null 那一路没兜住"
    assert '(m.multiplier ?: "?x")' in chat and '(item.multiplier ?: "?x")' in chat, \
        "两处读数（模型行 / 卡内副标题）没各自兜底"
    credits = CREDITS_PY.read_text(encoding="utf-8")
    at = credits.index("def multiplier_label(")
    assert "return None" in credits[at:credits.index("\ndef ", at)], \
        "前提变了（倍率标签不再回 None）：重议 DTO 可空这条纪律，别删这条断言"


def test_i2_clamp_note_survives_the_status_clear():
    """I-2：「上限调小把本机预算夹下来了」那句必须活过同一帧的 `setStatus("")`。

    旧形状（b47d62f2 的 ChatUi.kt:793-794）：`reconcileBudgetCap(...)` 在弹单没开着时
    往状态条写了一句，**紧接着**无条件 `if (providers.any { it.usable }) setStatus("")`
    ——同一帧两次写，只有最后一次可见，那句话被就地冲掉；另两个调用点（保存能力后 /
    拖预算后）都发生在弹单开着时，走不到那条分支。所以上一轮报告 §5.4 那句补救实测零效果。
    现在 `reconcileBudgetCap` 回报 Boolean（本轮这句话由状态条说了），清状态那一句让路。

    变异：① 清状态改回无条件 `if (providers.any { it.usable }) setStatus("")` → 第五条红；
    ② 去掉 `return true`（说过话不回报）→ 第四条红；③ 把签名/记账退回 Unit → 第一条红；
    ④ 把顺序倒过来（先清状态再核对夹紧）→ 第六条红；⑤ 判据从 `!clampedNow` 换成别的名字
    （例如 `budgetClamped == null`）→ 第五条红（字符串判据找不到那一枚）。
    射程：**形状锁**。钉的是"这一句不再无条件 + 记账排在清状态之前"，不钉"用户肉眼看见
    了那句话"——弹单没开、清单回来看得见的状态条要真机（6 秒自动收那条 LaunchedEffect
    也没被这把尺量过），列进未跑清单。
    """
    chat = _c("ui/ChatUi.kt")
    assert "fun reconcileBudgetCap(pid: String, capK: Int): Boolean" in chat, \
        "夹紧函数不回报「本轮我说过话」，调用方只能盲清"
    rec = _block(chat, "fun reconcileBudgetCap(")
    spoken = _block(rec, "if (!modelMenuOpen) {")
    assert "setStatus(budgetClampNote(" in spoken and "return true" in spoken, \
        "状态条说了这句话却没回报：下一次清状态照样把它抹掉"
    lm = _block(chat, "suspend fun loadModelsNow()")
    at = lm.index('setStatus("")')
    line = lm[lm.rindex("\n", 0, at) + 1:lm.index("\n", at)]
    assert "providers.any { it.usable }" in line and "!clampedNow" in line, \
        "清状态那句又是无条件的（或没把本轮夹紧记账算进条件）：提示被就地冲掉"
    assert "val clampedNow = reconcileBudgetCap(" in lm, "没接返回值记账，条件就是空头支票"
    assert lm.index("val clampedNow = reconcileBudgetCap(") < at, "顺序反了：先清后记账"


def test_i3_shared_capability_edit_says_the_reauth_gap_and_disables_the_save_pill():
    """I-3：原生**没有 Cookie 通道**，管理员改共享模型的能力字段结构上必 428。
    证据链：`authz.py` 的 `require_admin_reauth` 判 `request.cookies.get(admin_reauth.COOKIE_NAME)`，
    `ADMIN_REAUTH_REQUIRED` 默认开；原生两个 OkHttpClient 都没设 `cookieJar`（OkHttp 默认
    NO_COOKIES），`request()` 只加 `Authorization: Bearer`。网页那边真有这条链
    （api.js 428 → #reauthModal → POST /v1/auth/admin-reauth → 带票重试）。
    本单**不补 CookieJar**（接线另立 #228），只把话说实话 + 禁用那颗必失败的钮。

    变异：① 删掉那句实话 → 第一/二条红；② 把 `!reauthBlocked` 从「保存能力」的 enabled
    摘掉 → 第三条红（留一个按下去只会拿到 428 的按钮）；③ 判据从 `canEdit && item.shared`
    放宽成只看 `item.shared` → 第四条红（普通用户根本没有管理区，说明会到处乱印）；
    ④ 把说明改成「与网页一致」→ 第五条红（那正是这一轮改掉的那句错判）。
    射程：形状锁。它钉"这句话在这个分支里 + 这颗钮在这种情形下禁用 + 票号登记在注释"，
    不钉"真机上读屏念得出、那颗钮真的点不动"，更不钉 428 从此不会发生（要 #228 接线）。
    """
    raw = _t("ui/ChatUi.kt")
    card = _block(_c("ui/ChatUi.kt"), "private fun ModelCard(")
    assert "val reauthBlocked = canEdit && item.shared" in card, \
        "「只有管理员能按、按了必失败」这一格没单独记账"
    assert "需要管理员二次验证" in card and "手机本版没接" in card, \
        "共享模型的这项改动没说实话（上一版写「与网页一致」是错判）"
    assert "请到网页管理台" in card, "只说不接没给出路"
    at = card.index('ModelMiniPill(if (card.saving) "保存中…" else "保存能力"')
    assert "!reauthBlocked" in card[at:at + 260], \
        "保存能力钮在必失败的情形下还 enabled：按下去只会拿到 428"
    assert "与网页一致" not in card, "卡里不许再声称这条通路「与网页一致」"
    assert "#228" in raw, "这段实话必须在注释里登记另票号（真正的接线是 #228）"


def test_m1_picking_a_model_and_closing_share_one_teardown():
    """Minor 1：换完模型的收尾与关弹单同一套（旧 onPick 只关了 modelMenuOpen ⇒
    下次打开还是那张卡展开、还可能重印上一轮那句）。
    Minor 6 的另一半也在这里：收尾只有一份，三个出口都走它。

    变异：① 从 closeModelMenu 里删掉任意一行收尾 → 对应 needle 红；
    ② 让 onPick 自己另写一套（不叫 closeModelMenu）→ 第三条红；
    ③ 把 `modelManage = false` 塞进收尾 → 第二条红（收起弹单 ≠ 退出管理，既有判据）。
    射程：只判"收尾只有一份、三个出口都调它、里面那五行都在"，不判真机视觉次序。
    """
    chat = _c("ui/ChatUi.kt")
    teardown = _block(chat, "fun closeModelMenu(")
    for needle in ("modelMenuOpen = false", 'modelCardOpen = ""', "budgetClamped = null",
                   "modelSaved = null", 'modelCardError = ""'):
        assert needle in teardown, f"公共收尾缺这一行：{needle}"
    assert "modelManage" not in teardown, "清掉了 modelManage：与 hideModelMenu 的判据反向"
    assert "closeModelMenu()" in _block(chat, "onPick = {"), "换模型没走同一套收尾"
    assert "closeModelMenu()" in _between(chat, "onClose = {\n", "onPick = {"), \
        "× 关单没走同一套收尾：两份收体会漂移"


def test_m6_model_menu_has_a_back_handler():
    """Minor 6：`modelMenuOpen` 一直没有 BackHandler（本文件唯一的 BackHandler 属于
    设置弹层），全屏弹层在真机上可能只能靠右上角 × 关。补一枚与 onClose 同一套收尾的。

    变异：① 删掉 BackHandler → 第一条红；② enabled 漏掉（改成常驻 `BackHandler {}`）→
    第二条红（那样弹单没开时也会吃掉返回键）；③ 里面另写一套收尾 → 第三条红（与 ①
    的关单行为漂移，两个出口各清各的）。
    射程：形状锁。真机按返回键到底关没关，本机没有宿主，列进未跑清单；与设置弹层那枚
    BackHandler 的组合先后（同屏都开着时谁赢）这把尺也不判——实测两条路互斥（全屏弹层
    盖住抽屉，开不了设置页），报告里记这条前提。
    """
    chat = _c("ui/ChatUi.kt")
    assert "BackHandler(enabled = modelMenuOpen)" in chat, "模型弹单没有返回键出口"
    bh = _block(chat, "BackHandler(enabled = modelMenuOpen)")
    assert "closeModelMenu()" in bh, "返回键自己另写一套收尾：两个出口会漂移"
    assert "import androidx.activity.compose.BackHandler" in _t("ui/ChatUi.kt")


def test_m4_capability_body_applies_the_patch_after_reading_the_record():
    """Minor 4（审查员变异 F）：`capabilityBody` 里 patch 的覆盖必须排在"从完整记录取值"
    **之后**。上一轮这件事没有任何锁：把 `patch.forEach` 挪到那一串 put 之前，每次保存都
    把用户刚改的新值洗回旧值，而 28 枚锁全绿（rc=0）。

    变异：把 `patch.forEach { (key, value) -> put(key, value) }` 上移到任意一格 rec 取值
    之前 → 对应 needle 红；把覆盖整行删掉（改成写死值）→ 第七条红（保存的能力永远不生效）。
    射程：只判**字面顺序**与"覆盖只有一处"。空 patch 时两种顺序等价，这把尺与那个 bug
    都看不见它；真实"改上限 → 保存 → 卡片读回 256"要登录态与服务端，本单没跑。
    """
    chat = _c("ui/ChatUi.kt")
    body = _block(chat, "private fun capabilityBody(")
    at = body.index("patch.forEach")
    for needle in ('put("label", str("label"))', 'put("base_url", str("base_url"))',
                   'put("model", str("model"))', 'put("api_key", "")',
                   'put("supports_vision", flag("supports_vision"))',
                   'put("is_default", flag("is_default"))', 'put("max_context_k"',
                   'put("supports_thinking"', 'put("thinking_levels"',
                   'put("thinking_default"', 'put("thinking_profile"', 'put("pricing"'):
        assert needle in body[:at], f"这一格排到了 patch 覆盖之后：保存会把新值洗回旧值 → {needle}"
    assert body[at:].count("put(") == 1, "覆盖之后还有第二处 put：谁赢说不清"
    # 覆盖本身必须在（挪没了等于"保存的能力永远不生效"）
    assert 'patch.forEach { (key, value) -> put(key, value) }' in body


def test_m5_new_controls_carry_minimal_accessibility_semantics():
    """Minor 5：本轮新增的控件补最小无障碍语义——卡内预算滑杆要有 contentDescription +
    stateDescription；ModelMiniPill 的禁用态要让读屏念得出「不可用：…（原因）」，
    而不是只把颜色调淡。原因由调用方给（`disabledHint`），因为这里至少有三种"为什么不可用"。

    变异：① 删掉 `import …semantics.stateDescription` → 第一条红；② 把 disabledHint 形参
    去掉、里面写死一句通用话 → 第二条红；③ 删掉 ModelMiniPill 的 semantics 块 → 第三条红；
    ④ 滑杆的 modifier 整行删掉 → 第四/五条红；⑤ 只给 contentDescription 不给
    stateDescription → 第五条红（念得出名字念不出当前值）；⑥ 调用方不传原因 → 第六条红。
    射程：形状锁 + import 在位。TalkBack 实际念什么、禁用态是否真被读成"不可用"，
    本仓没有宿主，列进未跑清单。**既有的 ContextPanel 那只滑杆同缺**，按审查口径不在
    本单范围，已登记进报告（Task 6 那把环带百分比的锁 :3087 一字未动）。
    """
    raw = _c("ui/ChatUi.kt")
    assert "import androidx.compose.ui.semantics.stateDescription" in _t("ui/ChatUi.kt"), \
        "语义没 import（_c 也会保留 import 行，这里按原文钉，防止有人只在注释里提它）"
    # 形参在**签名**里，`_block` 只给函数体（从第一个 `{` 起），所以这一段用 _between：
    # 锚点从函数名到函数体第一行。写成 _block 会恒找不到 disabledHint —— 那不是变异，是尺子。
    sig = _between(raw, "private fun ModelMiniPill(", "\n    val scheme = MaterialTheme.colorScheme")
    assert 'disabledHint: String = ""' in sig, "禁用原因没由调用方给，只能念一句通用的"
    pill = _block(raw, "private fun ModelMiniPill(")
    assert "stateDescription" in pill and "不可用：" in pill, "禁用态只把颜色调淡，读屏念不出原因"
    card = _block(raw, "private fun ModelCard(")
    slider = _call(card, "Slider(")
    assert "contentDescription" in slider, "预算滑杆没有名字"
    assert "stateDescription" in slider, "预算滑杆没有当前值语义"
    assert "disabledHint = " in card, "卡上被禁的钮没把原因传给读屏"


def test_m2_clamp_comment_states_the_native_reason_not_the_web_one():
    """Minor 2：那段注释的理由是**网页的**。原生 `Prefs.contextBudgetK` 只夹 `1..10000`，
    **不夹模型上限**，所以"拿夹过的读数比上限恒不成立"在这边不成立；取
    `contextBudgetWantedK` 依然是对的，但真理由是"区分这台设备从没为这条记过（0）
    与记过且越界"。注释按实测改口径（代码一行不动）。

    判据吃的是**未剥注释**的原文（`_t`）——`_c` 会把注释剥掉，用 `_c` 写这条锁等于恒真。
    变异：把注释改回"恒不成立"那句网页理由 → 第一条红；把注释整段删掉 → 第二条红
    （下一个读代码的人会以为 `wanted` 与 `contextBudgetK` 等价，把它换回去）。
    射程：这条只钉"注释说了可核实的理由"，它不是行为锁；判据会被"照抄这段注释到别处"
    绕过（本文件里这一句只该出现一次，故同锁钉计数）。
    """
    raw = _t("ui/ChatUi.kt")
    at = raw.index("fun reconcileBudgetCap(")
    doc = raw[max(0, at - 900):at]
    assert "从没为这条记过" in doc, "没写原生取 wanted 的真理由"
    assert "不夹模型上限" in doc and "1..10000" in doc, "没交代原生 contextBudgetK 夹的是什么"
    assert raw.count("恒不成立") == 1, \
        "「恒不成立」这句网页理由又被抄开了：原生读数不夹模型上限，这条不成立"


# ================ Task 13B（原生）：档位只在已核实时发送 ================
# 网页那半在 b029759e + d44c3399 已落地，下面四枚照它的形状对位，不另起口径：
# 三道闸的**顺序**（状态 → 本机开关 → 这条记录当前的清单）、"整格省略而不是发
# null/空串"那一格的序列化形状、§0 禁句清单、以及"卡上那句'本轮会带上什么'必须由
# 发送路径上同一个判据回答"（网页 app.js:1869 同一条纪律）。
#
# 射程（改这段之前先读）：本文件这些 Kotlin 锁**全是静态源码形状**判据（`_c` 剥注释
# + `_block`/`_between`），仓库里没有 Kotlin 测试运行器，所以它们认的是**文本形状**、
# 不是运行时行为；"真的能编译"由 :app:compileDebugKotlin 那一步证明，不由这几枚证明。
# 每条新锁都配一次"故意写坏会红"的实测（变异只在仓库外克隆里做，见 task-13b-report.md）。

# 禁句清单的唯一出处是 `task-12-amendment.md` §2 那一条。那份文档被
# `.superpowers/sdd/.gitignore:1` 整目录排除、CI 上读不到，所以把裁决原文抄进锁里、
# 由它现抠「」内容当清单来源，再用**句数 + 集合**两道比对钉住扫描实际吃的那份——
# 少一味、多一味、抄错一味，当场红（13A 复审 Finding 1：手抄 tuple 会悄悄变短）。
_AMENDMENT_S2_BANNED_LINE = (
    "界面/发版说明/文档里不许出现「可关闭思考」「思考已关闭」「支持关闭」「三档强度」"
    "这类今天没有对照支持的句子"
)
# §2 那一条点名的句数（裁决原文就是四味；这个数字是清单的体检尺）。
_AMENDMENT_S2_BANNED_COUNT = 4
# 裁决 §2 现抠出来的那份（清单来源）
_AMENDMENT_S2_BANNED = tuple(re.findall(r"「([^「」]+)」", _AMENDMENT_S2_BANNED_LINE))
# 原生扫描实际吃的那份（被改窄会撞上下面两道断言）
NATIVE_BANNED_THINKING_PHRASES = ("可关闭思考", "思考已关闭", "支持关闭", "三档强度")
# §R4 探针那把锁（`test_v0391_thinking_mapping.py:1917`）用的是它自己的措辞「已关闭思考」，
# 不在 §2 那四味里；而探针**不覆盖**§2 的哪两味也必须写死在册——三处清单的互钉见
# `test_banned_phrase_lists_are_cross_pinned_across_web_native_and_probe`。
_PROBE_SIDE_OWN_PHRASES = ("已关闭思考",)
_PROBE_SIDE_NOT_COVERED = ("可关闭思考", "三档强度")

# ---- 本单硬化的两把尺（13B 复审两枚 Critical：省略锁只钉子串、禁句锁只扫三份文件）----
# 发送点档位实参的**完整**形状。只判子串 `in` 的话，在这串后面补一个 `?: ""`（把 null 又
# 变回空串）完全不被识别——复审 M1 实测 `40 passed`，而线上真会带上 `"thinking_level": ""`。
_LEVEL_ARG_CANON = "thinkingLevelForSend(currentProvider()).takeIf { it.isNotEmpty() }"
# 「把空值补成空串」的尾巴，显式列成一味都不许接的负判清单。这几味不是臆想的靶子：全树现存
# `orEmpty()` 41 处、`ifEmpty` 25 处、`getOrDefault` 6 处——都是下一个接手的人写得出的形状。
_LEVEL_ARG_EMPTY_BACKFILL = ("?: \"\"", "?: ''", "orEmpty()", "ifEmpty", "ifBlank",
                             "getOrDefault", "defaultIfBlank")

NATIVE_ROOT = REPO / "android-native"


def _tracked_android_kt():
    """`git ls-files android-native` 里那份 .kt 集合——基数钉的**仓库事实**一侧。

    问不到 git 或返回空一律红，不 skip：本仓老规矩，读不到来源的锁等于没有锁。
    """
    import subprocess

    g = subprocess.run(["git", "ls-files", "-z", "android-native"], cwd=str(REPO),
                       capture_output=True, timeout=60)
    assert g.returncode == 0, "问不到 `git ls-files android-native`：同集合比对不许 skip 后当绿"
    tracked = {p.decode() for p in g.stdout.split(b"\0") if p.endswith(b".kt")}
    assert tracked, "`git ls-files android-native` 返回 0 份 .kt：基数钉读不到仓库事实"
    assert not [p for p in tracked if "/build/" in p], f"版本库里躺着 build 中间产物的 .kt：{sorted(tracked)}"
    return tracked


def _native_kt_corpus():
    """全树 `android-native/**/*.kt` 的剥注释语料（写死三份就是 M6 那个洞）。

    两侧必须**同一个集合**：rglob 少一份（改名/挪走/walk 进空目录）、多一份（没入库的游离
    .kt）都红——"扫不到"和"守住了"不许长得一样（`test_native_android_discipline.py:105`
    那句自述）。构建中间产物按 `/build/` 段排除，同一枚锁里钉住 git 侧一份 build 产物都没有。
    """
    on_disk = {p.relative_to(REPO).as_posix() for p in sorted(NATIVE_ROOT.rglob("*.kt"))
               if "/build/" not in p.relative_to(REPO).as_posix()}
    tracked = _tracked_android_kt()
    assert on_disk == tracked, (
        f"禁句语料的射程与仓库事实不同：磁盘扫到 {len(on_disk)} 份 / git 记 {len(tracked)} 份，"
        f"只在磁盘={sorted(on_disk - tracked)} 只在 git={sorted(tracked - on_disk)}")
    inside = {p.split("nativeapp/", 1)[-1] for p in on_disk}
    assert {"Api.kt", "ui/ChatUi.kt", "Prefs.kt"} <= inside, \
        "13B 点名的三份已不在语料里：禁句扫描的射程比那天还短了"
    return {rel: _code(REPO / rel) for rel in sorted(on_disk)}


def _call_inner_args(code, anchor):
    """**每一个** `anchor(` 的圆括号内文（跳字符串里的括号，跨行算；括号不闭合就红）。

    与 `_call` 的差别就是射程：`_call` 只看第一处，于是"第二个发送点开在别的文件里"
    这种形状在文本上是隐形的（复审 :91 点名的同源窄射程）。
    """
    spans, at = [], 0
    while True:
        k = code.find(anchor, at)
        if k < 0:
            return spans
        i = code.index("(", k)
        depth, j, quote, esc = 0, i, None, False
        while j < len(code):
            c = code[j]
            if quote:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == quote:
                    quote = None
            elif c in "\"'":
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        assert depth == 0, f"{anchor} 的圆括号没闭合：实参尺子滑出了函数"
        spans.append(code[i + 1:j])
        at = k + len(anchor)


def _split_top_level_args(inner):
    """按**顶层**逗号切实参列表（`()`/`[]`/`{}`/字符串里的逗号不算分隔）。"""
    out, cur, depth, quote, esc = [], [], 0, None, False
    for c in inner:
        if quote:
            cur.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                quote = None
            continue
        if c in "\"'":
            quote = c
            cur.append(c)
        elif c in "([{":
            depth += 1
            cur.append(c)
        elif c in ")]}":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    out.append("".join(cur).strip())
    return out


def test_native_send_gate_emits_a_level_only_for_a_verified_record():
    """§2.1/§2.2：三道闸按顺序钉死，两半都写（否则在"今天一条都不发"的基线上恒绿）。

    网页 `thinkingLevelForSend` 的原生对位。两半的含义：unverified 那半钉"状态不是
    verified 就 return 空"，verified 那半钉"状态对且开关开且标签在当前清单里就产出标签"
    ——只写前一半的话，今天所有记录都是 unverified，锁永远绿，而"以后 verified 了发不发"
    没人管（brief §4.1 点名的恒绿形状）。
    第二道闸的理由：prefs 是历史值、能力是服务端给的，两者可能不一致，不一致时以服务端为准
    ——所以状态闸必须排在 Prefs 读数**之前**（顺序也钉住，见下面 index 比较）。
    变异：① 删掉/放宽 verified 那道闸 → 第一、二条红；② 把 `Prefs.thinkingEnabled` 那道
    闸删了 → 第三条红（开关关着也发＝界面在替用户做他没说过的决定）；③ 去掉
    `levels.contains(pick)` 复检 → 第四条红（§5.3：清单改了照发旧标签）；
    ④ 把 Prefs 读数挪到状态闸前面 → 第五条红（本机值盖过服务端能力）。
    射程：判的是函数体文本形状，不判 SharedPreferences 真读到什么——那是 Android 运行时，
    本机没有宿主，真跑列进手测清单。
    """
    chat = _c("ui/ChatUi.kt")
    g = _block(chat, "private fun thinkingLevelForSend(")
    assert 'if (item == null || item.thinking_status != "verified") return ""' in g, \
        "状态闸不在了（或换了判据）：unverified 也能产出标签，界面就开始说谎"
    assert "Prefs.thinkingEnabled(" in g, "本机开关那道闸没了：关着也发"
    assert "Prefs.thinkingLevel(" in g, "档位没走 Prefs 那唯一的归一口"
    assert "levels.contains(pick)" in g and 'return if (levels.contains(pick)) pick else ""' in g, \
        "没在这条记录**当前**那份清单里复检标签：会发出一个换不出参数的旧标签"
    assert g.index('!= "verified"') < g.index("Prefs.thinkingEnabled("), \
        "Prefs 读数排在状态闸之前：本机历史值盖过服务端能力（§2.2 第二道闸失效）"
    # 清单不留副本：闸函数只读 item.thinking_levels，不许自己缓存条数
    for cache in ("thinkingLevelCount", "levelCount", "cachedLevels", "levelsCache"):
        assert cache not in chat, f"原生缓存了「这条记录有几个档」的副本：{cache}"


def test_native_body_omits_the_key_rather_than_sending_null_or_empty():
    """§2.1 + 13A 复审：不发就**整格省略**——不是 null、不是 ""、不是 0。

    形状靠 `obj()` 那把既有尺子：值为 null 的键根本不进序列化结果（`email` 那格是同一族
    先例），所以发送点必须把空串转成 null（`takeIf { it.isNotEmpty() }`），而 chatPayload
    的形参必须是**可空**且缺省为 null——写成 `String = ""` 就等于把"没带这一格"演成
    "带了一格但值是空的"，服务端把这两件事当两件事。
    变异：① `thinkingLevel: String? = null` 改成 `String = ""` → 第二条红；
    ② `"thinking_level" to thinkingLevel?.let { s(it) }` 改成 `to s(thinkingLevel.orEmpty())`
    → 第三、四条红（未核实那一轮会带上 ""）；③ 换成 `put("thinking_level", …)` 无条件写 →
    第五条红；④ 在**任意一份 .kt** 另开第二个 `Api.chatPayload(` 发送点 → 全树计数那条红
    （不再是 `chat.count(...)`：第二条路开在别的文件里今天不会再隐形）；⑤ 在档位实参尾巴上
    补任意一味"空值回补"（`?: ""` / `?: ''` / `orEmpty()` / `ifEmpty` / `ifBlank` /
    `getOrDefault`）→ 整串钉与负判那两条红（复审 M1 在 350ddb07 上是 `40 passed` 全绿）。
    射程：静态判"这一格只从条件插入那一条路出来"；真出网的字节要真机抓包（手测清单）。
    """
    api = _c("Api.kt")
    chat = _c("ui/ChatUi.kt")
    sig = _between(api, "fun chatPayload(", "return obj(")
    assert "thinkingLevel: String? = null" in sig, \
        "形参不是可空+缺省 null：缺省值会被序列化成空的一格"
    assert 'thinkingLevel: String = ""' not in sig, "形参缺省写成了空串"
    assert '"thinking_level" to thinkingLevel?.let { s(it) }' in api, \
        "这一格不是条件插入：没走 obj() 的 null 跳过，就会带出一个空值"
    for shape in ('put("thinking_level"', '"thinking_level" to JsonPrimitive',
                  "thinkingLevel?.let { JsonPrimitive", '"thinking_level" to JsonNull'):
        assert shape not in api, f"请求体里出现了无条件/空值形状：{shape}"
    assert "JsonNull" not in api, "用 JsonNull 占位＝发了一个 null 格而不是省略"
    rs = _block(chat, "fun streamInto(")
    assert "thinkingLevelForSend(currentProvider())" in rs, \
        "发送点没现读这条记录再问闸（用了缓存副本或干脆没问）"
    assert ".takeIf { it.isNotEmpty() }" in rs, "空串没转成 null：省略会退化成发 \"\""
    # 「只有一个出处」这句要**全树**数才成立：调用点搬到别的 .kt 里，`chat.count(...)` 数不到它；
    # 而且整串钉住之后，尾巴上再补一层"空值回补"也当场红（复审 M1 今天能带着空串绿）。
    corpus = _native_kt_corpus()
    slots = [(rel, " ".join(_split_top_level_args(inner)[-1].split()))
             for rel, text in corpus.items()
             for inner in _call_inner_args(text, "Api.chatPayload(")]
    assert len(slots) == 1, \
        f"全树 `Api.chatPayload(` 的发送点应是 1 处，实得 {len(slots)} 处：{[r for r, _ in slots]}"
    where, slot = slots[0]
    assert slot == _LEVEL_ARG_CANON, \
        f"发送点档位实参不是那一整串（{where}）：这一格只许『闸产出 → 空串转 null』，尾巴不接加工：{slot}"
    for shape in _LEVEL_ARG_EMPTY_BACKFILL:
        assert shape not in slot, \
            f"档位实参接了『把空值补成空串』的尾巴 {shape}（{where}）：请求体会带上空的 thinking_level"
    assert sum(t.count("fun chatPayload(") for t in corpus.values()) == 1, \
        "chatPayload 的定义开了第二份签名：两处各自决定这一格发不发"


def test_native_card_promise_comes_from_the_same_gate_and_never_promises_a_lie():
    """§5.2/§5.3 + 13A 复审 Finding 2：卡上"会带上什么"必须问发送路径那同一个判据。

    网页 13A 那枚 bug 的形状是：`thinkingStaleLevelNote` 不看状态也不看开关，于是同一张卡
    上两句相邻说明自相矛盾（一句说"按默认档发"，另一句说"参数不生效"），而 `thinkingLevelForSend`
    当轮返回空。原生这边**不许复现**：那句"这一轮按默认档…发"只许出现在闸真的产出标签的
    分支里，所以判据吃两样东西——函数体里 `willSend` 取自 `thinkingLevelForSend`，且
    "不发档位这一格"那条分支排在承诺句**之前**（顺序即牙：把承诺挪到判空前面就红）。
    措辞与网页同句，别在原生改字（跨端逐字比对已有先例见上面 :384 那枚）。
    变异：① 卡片自己再算一遍（把 `willSendLevel` 换成 `levelPick`）→ 第一、二条红；
    ② 去掉 `if (willSend.isEmpty())` 那道分支（承诺常驻）→ 第三、四条红；
    ③ 把 `thinkingInactiveNote` 那句"参数不生效"删掉 → 第五条红（§5.2 回落不可复盘）；
    ④ 保留"这一档今天还没接进请求体"那句旧实话 → 第六条红（接线后它就变成假话）。
    射程：与网页的差集登记清楚——网页用 `state.thinkingStaleLevel` 记"这句已经印过"，
    原生这张卡是点开才有的弹层，说明随条件常驻在卡上（不是常驻在整个页面上），所以这边
    没有那份一次性状态；"用户真的看见这句"仍属渲染时机，不在静态锁射程内（真机手测）。
    """
    chat = _c("ui/ChatUi.kt")
    card = _block(chat, "private fun ModelCard(")
    assert "val willSendLevel = thinkingLevelForSend(item)" in card, \
        "卡片自己算了一遍会带什么：卡上说会带上、请求里没有那一格就是界面说谎"
    assert '本轮会带上「${willSendLevel.ifEmpty { "不带上思考参数" }}」' in card, \
        "verified 那一支的承诺没接闸：开关关着也承诺要带"
    assert "这一档今天还没接进请求体" not in chat, \
        "旧实话还留着：13B 之后这一格确实进了请求体，这句已是假话"
    note = _block(chat, "private fun thinkingStaleLevelNote(")
    assert "thinkingLevelForSend(" in note, "回落说明没问发送判据，自己判了一份真相"
    assert "if (willSend.isEmpty())" in note, "少了一道「本轮不发」的分支"
    assert note.index("不发档位这一格") < note.index("这一轮按默认档"), \
        "承诺句排在不发那支之前：Finding 2 那个自相矛盾在原生复现了"
    assert "这一轮按默认档" in note and "本机开关没开" in note and "这条记录未核实" in note, \
        "回落那句没交代为什么不发"
    inactive = _block(chat, "private fun thinkingInactiveNote(")
    assert "参数不生效：这一轮请求里不会带上档位" in inactive, \
        "§5.2 回落不可复盘：prefs 记着开而状态不许发时没实话"
    assert 'if (item.thinking_status == "verified") return ""' in inactive, \
        "已核实那一支还说「参数不生效」：两句相邻说明自相矛盾"


def test_native_kotlin_copy_is_free_of_banned_thinking_phrases():
    """§0 禁句：语料是**全树** `android-native/**/*.kt`（本树 21 份，随仓库事实浮动），不是三份。

    13A 复审点名的洞：网页侧这条扫描落地时，原生侧覆盖是**零**。清单从裁决 §2 那行现抠，
    再与扫描实际吃的那份比**句数 + 集合**——手抄 tuple 少一味不再是"安静地少扫一句"，而是当场红。
    13B 复审的第二枚 Critical 是同一个失败类的另一半：**语料写死三份**。往 `ui/SettingsUi.kt`
    注入「这款模型支持三档强度」在 350ddb07 上是 `40 passed`（M6），因为那三份之外的 18 份
    根本不在射程里。改全树之后，还补一枚**基数钉**：磁盘扫到的集合与 `git ls-files
    android-native` 的 .kt 集合必须**同一个集合**，少一份（改名/挪走/walk 进空目录）
    多一份（没入库的游离 .kt）都红；git 问不到或返回空一律红，不 skip 后当绿。
    变异（实测见 task-13b-report.md §修复轮 1）：① tuple 摘掉「三档强度」→ 第三条红；
    ② 注入 `ui/ChatUi.kt` 或 `ui/SettingsUi.kt` 或任意一份别的 .kt → 最后的扫描红；
    ③ §2 原文抄窄一味 → 第一条红；④ 把 `ui/UpdateUi.kt` 改名/挪走 → 基数钉红；
    ⑤ 新增一份没入库的 .kt → 基数钉红。
    射程：语料走 `_code`（剥注释），判的是**给用户看的字符串**里有没有这些句子；注释里出现
    同词不判（与网页 `_js()`/`_html()` 同口径）。`/build/` 段下的中间产物不参与比对，
    同一枚锁里钉住 git 侧没有 build 产物，所以这条排除面吃不进真源码。
    """
    assert len(_AMENDMENT_S2_BANNED) == _AMENDMENT_S2_BANNED_COUNT, \
        f"裁决 §2 那一行只抠出 {len(_AMENDMENT_S2_BANNED)} 味，点名应是 " \
        f"{_AMENDMENT_S2_BANNED_COUNT} 味：清单来源本身被改窄了（{_AMENDMENT_S2_BANNED}）"
    assert len(NATIVE_BANNED_THINKING_PHRASES) == len(_AMENDMENT_S2_BANNED), \
        f"扫描只吃了 {len(NATIVE_BANNED_THINKING_PHRASES)} 味，裁决 §2 点名 " \
        f"{len(_AMENDMENT_S2_BANNED)} 味：漏掉的那句写进界面没有任何锁会红"
    assert set(NATIVE_BANNED_THINKING_PHRASES) == set(_AMENDMENT_S2_BANNED), \
        f"扫描清单与裁决 §2 不同：只扫 {set(NATIVE_BANNED_THINKING_PHRASES)} / " \
        f"§2 点名 {set(_AMENDMENT_S2_BANNED)}"
    corpora = _native_kt_corpus()
    assert len(corpora) >= 20, f"全树只扫到 {len(corpora)} 份 .kt：walk 到空目录也算『没有违规』"
    for banned in NATIVE_BANNED_THINKING_PHRASES:
        for name, text in corpora.items():
            assert banned not in text, f"{name} 里出现禁句「{banned}」：今天没有一条记录能说关闭"


def test_banned_phrase_lists_are_cross_pinned_across_web_native_and_probe():
    """票 #240 + 13B 复审 Minor：§2 清单在三处各有一份消费者，本单把它们互钉。

    原状：网页与原生各自抄一份 §2 原文、各抠各的集合——谁把**两边同时**改成另外四味
    （句数仍是 4），两把锁一起绿，裁决 §2 换了脸而没人红。这里加等式：两份抄写的**原文行**
    逐字相等，两份 tuple 与 §2 抠出的集合三者相等、且都是 `_AMENDMENT_S2_BANNED_COUNT` 味。
    探针那侧（`test_v0391_thinking_mapping.py:1917`，三味）**取 (b) 明确窄射程**：它只判
    `probe_thinking_profile.py` 的 verdict label 与 `proved` 文案，界面与原生由那两把全扫锁
    覆盖；它的措辞「已关闭思考」是 §R4 自己的、不在 §2 四味里，所以等式在这里退化成两条：
    探针清单 ⊆（§2 四味 ∪ 登记在册的探针自有措辞），且 §2 里探针**不覆盖**的那两味必须
    正好是 `_PROBE_SIDE_NOT_COVERED`。探针再摘窄一味、或 §2 加了味而探针没跟上 → 红。
    读不到探针那份锁（改名/挪走/换了写法）一律红，不 skip。
    天花板登记：§2 的原件在被 gitignore 的 `task-12-amendment.md` 里，CI 读不到，所以"三处
    同时改成另外四味"这一枚只能靠每份文件自己的句数钉 + 本条的逐字原文等式兜，无法对原件比。
    """
    from tests import test_v0391_web_cockpit_ui as web

    assert web._AMENDMENT_S2_BANNED_LINE == _AMENDMENT_S2_BANNED_LINE, \
        "网页与原生抄的裁决 §2 原文行已经分叉：两份清单从此各自解释"
    assert set(web.BANNED_THINKING_PHRASES) == set(NATIVE_BANNED_THINKING_PHRASES) == \
        set(_AMENDMENT_S2_BANNED), \
        f"三份清单不再是同一份：网页 {set(web.BANNED_THINKING_PHRASES)} / 原生 " \
        f"{set(NATIVE_BANNED_THINKING_PHRASES)} / §2 {set(_AMENDMENT_S2_BANNED)}"
    assert len(web.BANNED_THINKING_PHRASES) == len(NATIVE_BANNED_THINKING_PHRASES) == \
        _AMENDMENT_S2_BANNED_COUNT, "某一端被改窄/改宽：跨端等式的基数不再一致"
    src = (REPO / "backend" / "tests" / "test_v0391_thinking_mapping.py").read_text(
        encoding="utf-8")
    at = src.index("def test_no_label_or_proved_claim_says_the_switch_is_off(")
    found = re.search(r"banned = \(([^)]*)\)", src[at:at + 1200])
    assert found, "读不到探针那把禁句锁的清单（改名/挪走/换了写法）：互钉不许 skip 后当绿"
    probe_list = tuple(a or b for a, b in
                       re.findall(r'"([^"]*)"|\'([^\']*)\'', found.group(1)))
    assert probe_list, "探针那份禁句清单是空的"
    assert set(probe_list) <= set(_AMENDMENT_S2_BANNED) | set(_PROBE_SIDE_OWN_PHRASES), \
        f"探针禁句冒出没登记过的措辞：{sorted(set(probe_list) - set(_AMENDMENT_S2_BANNED) - set(_PROBE_SIDE_OWN_PHRASES))}"
    assert set(_AMENDMENT_S2_BANNED) - set(probe_list) == set(_PROBE_SIDE_NOT_COVERED), \
        f"探针的射程变了：现在它不覆盖 {sorted(set(_AMENDMENT_S2_BANNED) - set(probe_list))}，" \
        f"登记的是 {sorted(_PROBE_SIDE_NOT_COVERED)}——谁少了谁都得写进这两份登记，别让它静默漂"

