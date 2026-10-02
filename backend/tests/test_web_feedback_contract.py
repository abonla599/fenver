"""v0.27：网页/ PWA 的「意见反馈」改成应用内一页，直达管理员，不再导向仓库。

对应诉求「把软件的反馈也改一下吧，现在不要导向我的仓库」。安卓那一侧在 PR #16 已经
把反馈从「跳 GitHub Issue」换成应用内表单 → POST /v1/user-feedback；这一版把同一件事
做到网页侧，两端同一条链路、同一个后端收口。

判据全部在**剥掉注释的前端原文**上（复用 test_web_pwa 的 _html/_js 尺子，理由同那文件：
只在 HTML/JS 注释里写一遍骗不过去）。这里不重跑后端（/v1/user-feedback 的行为由
test_user_feedback.py 钉），只钉这一版前端契约的四条红线：

1. 「关于」卡片里那一行反馈入口是 <button>，不是带 href 的外链——它是页内导航；
2. 反馈那一页里不出现仓库地址 / 任何 github 外链：反馈的落点是后端，不是 issue；
3. 入口真的接上了这一页（rowFeedback → openSetPage("feedback")），且这一页真的提交到
   /v1/user-feedback（经 API.submitUserFeedback），三处断链任何一处都红；
4. 正文/截图上限与后端常量同值，避免前端放 500 字、后端 300 字截断这种"各写各的"。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import user_feedback_storage                   # noqa: E402
from tests.test_web_pwa import _html, _js, STATIC       # noqa: E402

REPO_HOST = "github.com/abonla599/fenver"


def _feedback_section() -> str:
    """反馈那一页：data-page="feedback" 的 section 起，到下一个 </section> 为止。"""
    html = _html()
    at = html.index('data-page="feedback"')
    end = html.index("</section>", at)
    return html[at:end]


def test_feedback_row_is_a_button_not_an_external_link():
    """入口是页内按钮：有 id=rowFeedback、是 <button>，且这一行没有 href。

    「关于」组只允许 github.com 一家外链（test_about_page_contract 钉死）。反馈如果做成
    <a href> 指向仓库，就等于把用户又送回仓库——正是这一版要拆掉的东西。
    """
    html = _html()
    m = re.search(r"<(a|button)\b[^>]*id=\"rowFeedback\"", html)
    assert m, "「关于」里没有 id=rowFeedback 的反馈入口"
    assert m.group(1) == "button", "反馈入口不是 <button>：它不该是带 href 的外链"
    row = html[m.start():html.index(">", m.start()) + 1]
    assert "href" not in row, "反馈那一行还挂着 href：反馈不该跳去外部页面"


def test_feedback_page_has_no_repository_link():
    """反馈页里不许出现仓库地址或任何外链——这一版的判决就是"别再把反馈导向仓库"。"""
    sec = _feedback_section()
    assert REPO_HOST not in sec, "反馈页里还写着仓库地址"
    assert "github" not in sec.lower(), "反馈页里还留着指向 github 的东西"
    assert 'href="http' not in sec, "反馈页里出现外链 href：反馈的落点应是后端不是仓库"


def test_feedback_form_elements_exist():
    """表单要件齐：正文（≤300）、截图选择（多选）、邮箱、提交按钮、状态行。"""
    sec = _feedback_section()
    assert 'id="fbText"' in sec and 'maxlength="%d"' % user_feedback_storage.TEXT_MAX in sec, \
        "正文框缺失或长度上限与后端 TEXT_MAX 不一致"
    assert 'id="fbImageInput"' in sec and "multiple" in sec, "截图选择缺失或不支持多选"
    assert 'id="fbEmail"' in sec, "邮箱输入缺失"
    assert 'id="fbSubmitBtn"' in sec, "提交按钮缺失"


def test_entry_is_wired_to_the_page_and_the_page_posts_to_backend():
    """三处接线都得在：入口→页面、页面提交→API、API→/v1/user-feedback（相对路径）。"""
    app_js = _js("app.js")
    assert 'openSetPage("feedback")' in app_js, "rowFeedback 没有接到 openSetPage(\"feedback\")"
    assert "API.submitUserFeedback" in app_js, "反馈页没调用 submitUserFeedback"
    assert "API.upload" in app_js, "反馈页没有把截图先上传换成 id（先传后附）"

    api_js = _js("api.js")
    assert re.search(r'submitUserFeedback\s*:', api_js), "api.js 没导出 submitUserFeedback"
    assert "/v1/user-feedback" in api_js, "submitUserFeedback 打的后端路径不对"
    # 相对路径：前端出现绝对 URL 由 test_frontend_uses_relative_api_paths_only 兜底。
    assert '"/v1/user-feedback"' in api_js or "`/v1/user-feedback" in api_js, \
        "接口路径不是相对字符串"


def test_frontend_caps_match_backend_constants():
    """正文/截图上限一处口径：前端常量直接引后端存储模块的数，杜绝各自改动。"""
    app_js = _js("app.js")
    text_max = re.search(r"FB_TEXT_MAX\s*=\s*(\d+)", app_js)
    img_max = re.search(r"FB_IMG_MAX\s*=\s*(\d+)", app_js)
    assert text_max and img_max, "前端没有把上限写成具名常量（FB_TEXT_MAX/FB_IMG_MAX）"
    assert int(text_max.group(1)) == user_feedback_storage.TEXT_MAX, \
        "前端正文上限与后端 TEXT_MAX 不等"
    assert int(img_max.group(1)) == user_feedback_storage.IMAGE_MAX, \
        "前端截图上限与后端 IMAGE_MAX 不等"
