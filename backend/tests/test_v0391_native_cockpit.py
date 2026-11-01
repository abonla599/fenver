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
from pathlib import Path

from tests.test_apk_release_discipline import _code

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp"


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
    ③ 把 reason 那一句撤掉 → 第四条红（禁用不说为什么就是"控件坏了"）。
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
    射程：提示"一次"清在 onClose/换身份两处，静态只判"这两句都存在"；渲染时机与
    真的只印一次要靠真机手测（清单在报告里）。
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
    assert "presetCtxK = " in form, "开单没复位预置上限：上一单的数会跟着下一单发"
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
