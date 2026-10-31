# v0.39.x 模型驾驶舱 Task 6：原生 per-provider 预算、done usage、上下文环 + 账本的形状锁。
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


def test_settings_ctxrow_writes_per_provider_budget():
    st = _c("ui/SettingsUi.kt")
    assert "Prefs.contextTokensK" not in st, "设置滑杆还在读写旧全局键"
    row_sig = _between(st, "private fun CtxRow(", "{")  # 签名段（体首brace之前）
    assert "providerId: String" in row_sig
    row = _block(st, "private fun CtxRow(")
    assert "Prefs.contextBudgetK(" in row and "Prefs.setContextBudget(" in row
    call = _between(st, "CtxRow(cap", "SetRow(FenverIcon.Export")
    assert "?: Prefs.defaultProviderId" in call, "调用点没给 provider 口径（显示会分叉）"
