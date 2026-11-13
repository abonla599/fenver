# v0.40 自动压缩：模型摘要走专用端点，且这个端点必须是无副作用的。
#
# 为什么单独钉一格：自动压缩是**后台**动作，用户没按任何按钮。如果摘要请求走的是
# /v1/chat，那个端点有 session_id 就往会话历史里落两条消息（用户一条、助手一条），
# 于是每压缩一次，用户的历史里就多两段谁都没说过的话——往上翻会看见"自己"问过
# 一段从没问过的原文。这个形状没有任何报错，只有翻历史的人才撞得上。
#
# 另一半是反向的：test_web_pwa 有一条锁禁止 app.js 出现 API.chat(（防的是断线后
# 整段重发烧钱）。两条锁方向一致但理由不同，所以这里把它钉成"摘要必须另有出口"，
# 免得下一个人为了让那条锁变绿，把摘要改回 /v1/chat。

import inspect
from pathlib import Path

import app.main as main_mod


REPO = Path(__file__).resolve().parents[2]
APP_JS = REPO / "backend" / "app" / "web" / "static" / "app.js"
API_JS = REPO / "backend" / "app" / "web" / "static" / "api.js"


def _route_paths(app) -> set:
    out = set()
    for r in app.routes:
        p = getattr(r, "path", None)
        if p:
            out.add(p)
    return out


def test_the_summarize_endpoint_exists_and_is_not_the_chat_endpoint():
    assert "/v1/chat/summarize" in _route_paths(main_mod.app), \
        "没有 /v1/chat/summarize：自动压缩只能退回抽取式摘要，模型总结这一路等于不存在"


def test_the_summarize_endpoint_never_writes_to_session_history():
    """无副作用是这个端点的全部意义：不落会话、不挂附件、不发工具。"""
    src = inspect.getsource(main_mod.chat_summarize)
    # 只钉真正会写东西的两样。不用 "trace"/"attachments" 这种泛词当判据：
    # 函数自己的注释里就写着"不记 trace、不挂附件"，拿词当判据会把注释也判红
    # ——那正是文案锁退化成噪音的形状。
    for forbidden in ("add_message(", "sessions_store"):
        assert forbidden not in src, \
            f"摘要端点里出现了 {forbidden}：它必须无副作用，否则压缩会污染用户历史"


def test_the_client_summarizes_through_its_own_call_and_falls_back():
    app_src = APP_JS.read_text(encoding="utf-8")
    api_src = API_JS.read_text(encoding="utf-8")

    assert "summarize:" in api_src and "/v1/chat/summarize" in api_src, \
        "api.js 没有 summarize 出口"
    assert "API.summarize(" in app_src, "app.js 没有调 summarize 出口"
    assert "extractiveSummaryOf(" in app_src, \
        "模型摘要失败时没有本地兜底：断网/超时会让自动压缩直接失效，用户撞上下文超限"
    # 兜底必须在**触发点同步落位**：异步模型摘要回来前，发送范围已经收窄了，
    # 那段空窗里若没有摘要，模型就是纯粹"忘了前文"。
    assert "state.contextSummaryBySession[sid] = extractiveSummaryOf(moved)" in app_src, \
        "兜底摘要没有在触发点同步落位：模型摘要回来之前那一轮会真的失忆"
