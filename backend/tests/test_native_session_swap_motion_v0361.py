"""v0.36.1 动效批：会话切换「挤入卡片」（方案 A）的源码形状锁。

射程（一条要求至少一把锁，全部打在剥掉注释后的代码文本上）：
1. AnimatedContent 以会话 id 为 contentKey——流式 token/消息增长不换 key、不换卡；
2. 双向位移/缩放/淡出+时长 260/220+CubicBezierEasing(.2f,0f,0f,1f) 逐项钉死；
3. 新卡 zIndex 高于旧卡；旧卡画切换时的 SwapTarget 快照，退出中绝不读新会话 messages；
4. 旧卡交互全部 live 闸门——快照上的 index 拿不动全局列表；
5. 首次组合不播（swappedOnce 由三个真实切换入口打标）；
6. 系统 ANIMATOR_DURATION_SCALE==0 旁路转场，且读取包在 remember 里、全文件只查一次；
7. 加载中骨架「正在读取会话…」画在新卡上，数据回来在同一次转场里填上；
8. 只动消息区：全文件 AnimatedContent 仅此一枚，顶栏/输入卡在它之外。

锁形状不锁观感——手感归真机（与 test_native_ui_smoothness_v0361 同一口径）。
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from tests.test_apk_release_discipline import _code                       # noqa: E402

CHATUI = REPO / "android-native" / "app" / "src" / "main" / "java" / "xyz" / \
    "fenever" / "assistant" / "nativeapp" / "ui" / "ChatUi.kt"


def _chat() -> str:
    return _code(CHATUI)


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


def test_swap_transition_is_keyed_by_session_id_only():
    c = _chat()
    assert "AnimatedContent(" in c, "会话切换的 AnimatedContent 没了：切会话又是硬闪"
    assert c.count("AnimatedContent(") == 1, \
        "消息区之外又长出第二枚 AnimatedContent：本批明令只动消息区这一张卡"
    spec_body = _block(c, "transitionSpec = {")
    # targetState 是带快照的 SwapTarget，contentKey 只认 sid——流式 token/消息增长
    # 只生成新实例不换 key，同一张卡就地换内容，绝不触发转场。
    assert "targetState = swapTarget" in c, "转场目标状态没接 SwapTarget"
    assert "contentKey = { it.sid }" in c, \
        "contentKey 不再只取会话 id：流式/消息增长会误触发转场（方案 A 的前提被破坏）"
    # 快照载体在：sid 取自 sessionId（切换才变），msgs/loading 是当时的引用
    assert "SwapTarget(sessionId, messages, sessionLoading)" in c, \
        "swapTarget 没有按 (sessionId, messages, sessionLoading) 生成：快照链路断了"


def test_enter_exit_transform_offsets_and_scales_are_exact():
    c = _chat()
    spec = _block(c, "transitionSpec = {")
    # 新卡从抽屉侧（左）挤进来：初偏移 -1/12 宽 + 0.96 起放 + 淡入
    assert "initialOffsetX = { -it / 12 }" in spec, "入场不再是抽屉侧轻微滑入"
    assert "initialScale = 0.96f" in spec, "入场不再从 .96 放大"
    assert "fadeIn(" in spec, "入场丢了淡入"
    # 旧卡向右退出：+1/5 宽滑出 + 缩到 .94 + 淡出
    assert "targetOffsetX = { it / 5 }" in spec, "退场不再向右滑出"
    assert "targetScale = 0.94f" in spec, "退场不再缩小到 .94"
    assert "fadeOut(" in spec, "退场丢了淡出"
    assert "togetherWith" in spec, "enter/exit 没有拼成 ContentTransform"


def test_swap_durations_and_easing_are_the_pinned_constants():
    c = _chat()
    assert "private const val SESSION_SWAP_ENTER_MS = 260" in c, "进入时长写死 260ms"
    assert "private const val SESSION_SWAP_EXIT_MS = 220" in c, "退出时长写死 220ms"
    assert "CubicBezierEasing(0.2f, 0f, 0f, 1f)" in c, "缓动不再是 .2f,0f,0f,1f"
    spec = _block(c, "transitionSpec = {")
    assert spec.count("tween(SESSION_SWAP_ENTER_MS") >= 3, \
        "入场三兄弟（滑/放/淡）没都吃 260ms 常量"
    assert spec.count("tween(SESSION_SWAP_EXIT_MS") >= 3, \
        "退场三兄弟没都吃 220ms 常量"


def test_new_card_z_index_above_old_and_old_card_is_snapshot():
    c = _chat()
    assert "zIndex(if (live) 1f else 0f)" in c, \
        "新卡不再垫在旧卡之上：交替瞬间谁盖谁变成赌绘制顺序"
    # 旧会话内容必须用切换时的快照：列表画 card.msgs，全文件不许再有裸 messages 的 itemsIndexed
    assert "itemsIndexed(card.msgs" in c, "消息列表没接快照：退出过程会读到新会话的 messages"
    assert "itemsIndexed(messages" not in c, "旧的直读全局 messages 的列表形状回来了"


def test_leaving_card_interactions_are_all_live_gated():
    """退场那张卡是只读海报：快照的 index 拿不动全局列表，流式气泡/任务卡也不许画上旧卡。"""
    c = _chat()
    body = _block(c, "val live = card.sid == sessionId")
    assert body.count("if (!live) return@launch") >= 3, \
        "保存/删除/重生成的落地不再被 live 闸门拦住（旧卡一点就按快照 index 动全局）"
    assert "if (live) streamText?.let" in body, "流式气泡没挂 live 闸门：新会话的字会画到旧卡上"
    assert "if (live && tc != null" in body, "任务卡没挂 live 闸门：它是当前会话的状态，不是快照"
    assert "seedDraft = if (live) editingDraft else null" in body, \
        "编辑态没按 live 隔离：旧卡会接管新会话的编辑框"


def test_first_composition_does_not_play():
    c = _chat()
    spec = _block(c, "transitionSpec = {")
    assert "if (animScaleOff || !swappedOnce)" in spec, \
        "首次组合/关动画的旁路判据没了：恢复上次会话也要挤一下"
    assert "EnterTransition.None togetherWith ExitTransition.None" in spec, \
        "旁路分支不再是无转场直接切换"
    assert c.count("swappedOnce = true") >= 3, \
        "真实切换入口（openSession/新对话/删当前会话）打标少了：首帧判据只剩注释"


def test_animator_scale_zero_bypass_reads_system_service_only_once():
    c = _chat()
    assert c.count("ANIMATOR_DURATION_SCALE") == 1, \
        "动画缩放不止一处读取：不许在组合里反复查系统服务"
    assert "val animScaleOff = remember {" in c, \
        "动画缩放没有包进 remember：每次重组都 contentResolver 往返"


def test_loading_skeleton_stays_and_settles_inside_the_same_transition():
    c = _chat()
    assert '"正在读取会话…"' in c, "上一批定下的读取中骨架没了：切换瞬间又成白屏"
    # 骨架画在快照卡上（card.loading），数据回来 key 不变——同一次转场填上，无第二次转场
    assert "if (card.loading) item {" in c, \
        "加载提示没接 SwapTarget.loading：旧卡会读到新会话的加载态"
