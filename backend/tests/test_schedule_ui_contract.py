"""v0.23 R3 日程页的双端同构契约（T2.8）。

判什么
------
"双端同构"是一句很容易被做歪的话：两端各自写一遍、各自绿一次，读源码的人看见两份
一模一样的中文，就以为它们绑在一起了。它们没有——漂的那一天没有任何一条测试会红。
所以这一节把三件事分开钉：

1. **接口面**：两端都只走 GET/PUT `/v1/schedule`，都不带 user_id（归属人只从凭据里来）。
2. **口径**：「今天」只认服务端回的那个 `day`；400 时错误条里是 detail 原文且**不**渲染
   空态；写回点只有「保存」那一颗（客户端不做内容校验）。
3. **文案与算式**：同一批中文字串必须在两端语料里都出现（缺一个字的漂动就地红）；
   日期算式则由两端各自真跑一遍同一张表——网页那份在 node 里执行仓库原文（下面
   `_run_sched_js`，与 test_web_pwa 里 `_run_shell_js` 同一手法），安卓那份在纯 JVM 的
   `ScheduleBoard` 里由 `android/app/src/test/.../ScheduleBoardTest.java` 执行
   （本地 `python tools/shell_jvm_tests.py`，CI 跑 gradle 同一批）。
   本文件把这张表钉成**唯一一份期望值**，两端各自与它对齐。

语料入口沿用现成那几把尺子（`_js`/`_html`/`_css` 剥注释，`_code` 剥 Java/Kotlin 注释）：
判"某串不存在"的那几条，如果读的是带注释的原文，会被设计稿抄来的注释喂成假绿。
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_android_shell import _code                       # noqa: E402
from tests.test_web_pwa import (STATIC, _const_object, _css, _html, _js,  # noqa: E402
                              _js_fn)

REPO = Path(__file__).resolve().parents[2]
# v0.24 T0.2 快照决议：设计稿移出开源快照，留档在 docs-internal/（未被 git 跟踪）。
# 契约本身不因此作废——留档在场（本机）时照样逐条跑；快照 CI 里没有稿子，
# 依赖稿子的两条**明说理由地 skip**，而不是假装测过（skip 数会出现在交付表里）。
MOCK = REPO / "docs-internal" / "v0.23-阶段0" / "T0.1-日程设计稿-双端同构.html"
JVM_TEST = next((REPO / "android").glob("app/src/test/java/xyz/*/assistant/core/ScheduleBoardTest.java"))


def _native(rel: str) -> Path:
    """按包名片段找原生文件：那段包名字符不进测试源码，改名时这条 glob 会先红。"""
    hits = sorted((REPO / "android-native" / "app/src/main/java/xyz").glob("*/assistant/" + rel))
    assert len(hits) == 1, f"原生语料定位失败（{len(hits)} 个命中）：{rel}"
    return hits[0]


def _core(rel: str) -> Path:
    hits = sorted((REPO / "android-native" / "app/src/main/java/xyz").glob("*/assistant/core/" + rel))
    assert len(hits) == 1, f"共享核心类定位失败（{len(hits)} 个命中）：{rel}"
    return hits[0]


def _api_entry(name: str) -> str:
    """取 api.js 里某个方法的完整文本。

    必须跨行：`getSchedule` 的 URL 是拼出来的，写在两行上。只按单行正则去判，
    第一条会连 `?day=` 都没看见——判据本身错了，红得比代码还莫名其妙。
    """
    api = _js("api.js")
    m = re.search(r"^    " + re.escape(name) + r"\s*:.*?(?=^    [A-Za-z_$][\w$]*\s*:|^\};|\Z)",
                  api, flags=re.M | re.S)
    assert m, f"api.js 里没有 {name}：这一页的数据面整块失效"
    return m.group(0)


# 网页这一页的全部算式/渲染入口（名字改了这里会先红，不会静默漏判）。
SCHED_JS_FNS = ("schedParse", "schedIso", "schedShift", "schedWeek", "schedFullDate",
                "schedDayLabel", "schedMonthTitle", "schedMonthOf", "schedShiftMonth",
                "schedMonthCells", "schedCellLabel", "schedIsPast", "schedGroupText",
                "schedCountText", "schedSnapshot", "loadSchedule", "schedCell",
                "schedCalSignature", "renderCal", "schedMonthFloor", "schedMoveMonth",
                "syncCalHead", "schedRow", "renderSchedule",
                "saveSchedule", "flashSaved", "schedCanLeave")


def _sched_js_fns() -> str:
    js = _js("app.js")
    return "\n".join(_js_fn(js, name) for name in SCHED_JS_FNS)


def _sched_kt_api() -> str:
    """原生日程的两个接口封装：从 getSchedule 起到文件尾（putSchedule 紧随其后）。"""
    kt = _code(_native("nativeapp/Api.kt"))
    at = kt.index("suspend fun getSchedule")
    return kt[at:]


def _kt_fn(src: str, head: str) -> str:
    """按花括号配对切出 Kotlin 函数体。

    用"往后找到下一个缩进 4 的 `}`"这类粗切会在 lambda / 字符串模板处断早，
    断早的那一半看着还是绿的——判据就没了。Kotlin 的 `${}` 自身成对，计数不受影响。
    """
    at = src.index(head)
    i = src.index("{", at)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[at:j + 1]
    raise AssertionError(f"Kotlin 函数体没闭合：{head}")


def _sched_ui_kt() -> str:
    return _code(_native("nativeapp/ui/ScheduleUi.kt"))


# 两端文案的唯一一份清单。加一条中文进界面，就得在这里登记一次——这正是想要的摩擦。
SHARED_COPY = [
    "日程",
    "「今天」取服务端日期",
    "今天",
    " · 今天",
    "今日安排",
    "安排",
    " 项 · 已完成 ",
    "这天还没有安排",
    "点下面「添加一项」记下第一件",
    "添加一项",
    "● 有未保存修改 · 保存将整日替换",
    "尚无修改",
    "保存",
    "保存中…",
    "已保存",
    "要做什么",
    "HH:MM",
    "—",
    # 2026-09-27 评审第 1/3 条带来的：月历读屏标签的后缀、翻月四颗钮、换天那句确认。
    # 翻月钮那四个字只在网页的 aria-label 与原生的 semantics 里出现（看不见，但读得到）。
    "，有安排",
    "上一年",
    "上个月",
    "下个月",
    "下一年",
    "有未保存的修改：换到别的日子就不会保存它们。确定切换？",
]

# 设计稿里已经写死的那部分：两端 + 稿子三方同值（稿子是数值的唯一来源）。
MOCK_COPY = ["日程", "「今天」取服务端日期", "今日安排", "这天还没有安排", "添加一项",
             "● 有未保存修改 · 保存将整日替换", "尚无修改", "保存", "HH:MM", "—"]


@pytest.fixture(scope="module")
def web_corpus():
    return _js("app.js") + "\n" + _html()


@pytest.fixture(scope="module")
def native_corpus():
    return (_code(_native("nativeapp/ui/ScheduleUi.kt")) + "\n"
            + _code(_native("nativeapp/Api.kt")) + "\n"
            + _code(_core("ScheduleBoard.java")))


def test_both_ends_render_every_shared_string(web_corpus, native_corpus):
    """同一条中文在两端语料里都还在；漂一个字（"已保存"→"保存成功"）就地红。"""
    for text in SHARED_COPY:
        assert text in web_corpus, f"网页那份没有 {text!r}：双端文案漂了"
        assert text in native_corpus, f"原生那份没有 {text!r}：双端文案漂了"


def test_design_mock_still_owns_the_visible_copy(web_corpus, native_corpus):
    """设计稿是文案的出处：稿子里那几条必须在两端都原样存在。"""
    if not MOCK.is_file():
        pytest.skip("设计稿在 docs-internal/ 内部留档，不随快照分发；稿子不在时这条不判")
    mock = MOCK.read_text(encoding="utf-8")
    for text in MOCK_COPY:
        assert text in mock, f"设计稿里没有 {text!r}：这条该从 MOCK_COPY 去掉，别拿它当依据"
        assert text in web_corpus and text in native_corpus, \
            f"{text!r} 与稿子不一致（双端同构的判据就是稿子）"


def test_no_new_color_literals_in_the_schedule_css_block():
    """日程那段样式不许带来新色值：允许的来源只有现网令牌、稿子、以及表里已在用的字面量。"""
    if not MOCK.is_file():
        pytest.skip("色值白名单的一半在设计稿里，稿子随快照决议移出后这条不判")
    css = _css()
    at = css.index(".sched-panel {")
    block = css[at:]
    head = css[:at]
    mock = MOCK.read_text(encoding="utf-8")

    def norm(s: str) -> str:
        return re.sub(r"\s+", "", s).lower()

    allowed = {norm(x) for x in re.findall(r"#[0-9a-fA-F]{6}", head + mock)}
    offenders = sorted({norm(x) for x in re.findall(r"#[0-9a-fA-F]{6}", block)} - allowed)
    assert not offenders, f"日程样式里冒出表外色值：{offenders}"

    rgba_allowed = {norm(x) for x in re.findall(r"rgba\([^)]*\)", head + mock)}
    rgba_bad = sorted({norm(x) for x in re.findall(r"rgba\([^)]*\)", block)} - rgba_allowed)
    assert not rgba_bad, f"日程样式里冒出表外 rgba：{rgba_bad}"

    kt = _code(_native("nativeapp/ui/ScheduleUi.kt"))
    assert "Color(0x" not in kt, "原生样式不许自己造色值：只用 WebTokens / colorScheme"


# ---------- 接口面 ----------

def test_both_ends_use_only_the_two_schedule_routes():
    get = _api_entry("getSchedule")
    put = _api_entry("putSchedule")
    assert '"/v1/schedule"' in get and "?day=" in get, get
    assert '"/v1/schedule"' in put and 'method: "PUT"' in put, put

    api = _code(_native("nativeapp/Api.kt"))
    assert '"/v1/schedule"' in api, "原生没有接 /v1/schedule"
    assert "getSchedule" in api and "putSchedule" in api
    assert 'call("/v1/schedule", "PUT"' in api, "原生的写回必须是整天 PUT"
    # 反向锁：这一页不许有第三条路（自己拼 POST/DELETE/PATCH 都是分家）。
    assert _sched_kt_api().count("/v1/schedule") == 2, "原生多了第三条日程路由"
    assert get.count("/v1/schedule") == 1 and put.count("/v1/schedule") == 1


def test_neither_end_sends_a_user_id():
    """归属人只从凭据里来（后端 app/core/schedule.py 同一条口径）。

    只查这一页的语料：账户模块本来就带 user_id，拿整份 api.js 当判据会一直红，
    红久了就没人信这条锁了。原生侧那一次 `Prefs.currentEntry()?.userId` 是本地
    "这份草稿是谁的"的比对，不进任何请求，所以判据要落在调用点上而不是全文扫。
    """
    web = _api_entry("getSchedule") + _api_entry("putSchedule") + _sched_js_fns()
    for banned in ("user_id", "userId"):
        assert banned not in web, f"网页的日程语料里出现 {banned}：那是把归属人交给调用方"

    api = _sched_kt_api()
    for banned in ("user_id", "userId"):
        assert banned not in api, f"原生日程接口封装里出现 {banned}：归属人只能从凭据里来"

    kt = _code(_native("nativeapp/ui/ScheduleUi.kt"))
    calls = re.findall(r"Api\.(?:get|put)Schedule\([^)]*\)", kt)
    assert len(calls) == 2, f"原生这一页的日程调用点应是 GET/PUT 各一处，实际 {calls}"
    for c in calls:
        assert "userId" not in c, f"请求里带了身份字段：{c}"
    assert kt.count("userId") == 1, "本地那份 userId 只许出现在换人重置草稿那一行"


def test_put_payload_carries_only_text_at_done():
    """写回载荷的字段集 = 服务端 set_plan 读的字段集，多一个就是一份没人校验的自由发挥。"""
    js = _js()
    body = _js_fn(js, "saveSchedule")
    m = re.search(r"payload = sched\.items\.filter\(\(it\) => it\.text\.trim\(\)\)\s*"
                  r"\.map\(\(it\) => \(\{([^}]*)\}\)\)", body)
    assert m, f"saveSchedule 里的 payload 形状变了：{body[:200]}"
    assert {k.strip() for k in m.group(1).split(",")} == {"text: it.text", "at: it.at",
                                                          "done: !!it.done"}, m.group(1)
    kt = _code(_native("nativeapp/Api.kt"))
    fn = kt[kt.index("suspend fun putSchedule"):]
    fn = fn[:fn.index("\n    }")]
    assert 'put("text"' in fn and 'put("at"' in fn and 'put("done"' in fn
    assert 'put("id"' not in fn, "PUT 不再发号：id 由服务端生成，客户端不该带"


# ---------- 三条口径 ----------

def test_today_anchor_is_the_servers_day_on_both_ends():
    js = _js()
    load = _js_fn(js, "loadSchedule")
    assert re.search(r"if \(!day\) sched\.today = data\.day;", load), \
        "网页的锚点必须只在不传 day 那一次被服务端改写（R3-AC-3）"
    kt = _code(_native("nativeapp/ui/ScheduleUi.kt"))
    assert "if (target.isNullOrEmpty()) SchedState.today = data.day" in kt, \
        "原生同理：只有那次不带 day 的 GET 能定锚点"
    # 反向锁：两端都不许从设备时钟取"今天"。
    assert "new Date()" not in load, "网页 loadSchedule 里不许现取设备日期"
    sched_ui = _code(_native("nativeapp/ui/ScheduleUi.kt"))
    for banned in ("System.currentTimeMillis", "Calendar.getInstance", "Date("):
        assert banned not in sched_ui, f"原生 ScheduleUi 里出现了 {banned}：今天必须由服务端说话"


def test_server_detail_is_shown_verbatim_and_empty_state_is_suppressed():
    js = _js()
    render = _js_fn(js, "renderSchedule")
    load = _js_fn(js, "loadSchedule")
    assert "sched.error = e.message" in load, "400 的 detail 原文要直接进错误条"
    assert '"⚠ " + sched.error' in render, "错误条内容 = 前缀 + detail 原文，不加自己的解释"
    assert 'empty.classList.toggle("hidden", hasErr' in render, "有错误时不许渲染空态（R3-AC-2）"
    assert 'group.classList.toggle("hidden", hasErr' in render, "同上：组头也收起"
    assert 'add.classList.toggle("hidden", hasErr' in render, "同上：添加行也收起"

    kt = _code(_native("nativeapp/ui/ScheduleUi.kt"))
    assert "SchedState.error = e.message" in kt, "原生同理：detail 原文直出"
    at_err = kt.index("if (SchedState.error.isNotEmpty()) {")
    at_empty = kt.index("} else if (SchedState.items.isEmpty()) {")
    assert at_err < at_empty, "原生的分支顺序错了：空态排在错误之前就会把 400 说成『这天没有安排』"


def test_save_button_is_the_only_write_point_and_stays_disabled_when_clean():
    js = _js()
    render = _js_fn(js, "renderSchedule")
    assert 'save.disabled = !dirty || sched.saving' in render, "只有 dirty 且不在保存中才可点"
    html = _html()
    assert 'class="btn-save" id="schedSaveBtn" disabled' in html, "初始 HTML 就必须是禁用态"
    assert "putSchedule" in js
    kt = _code(_native("nativeapp/ui/ScheduleUi.kt"))
    assert "val enabled = dirty && !saving" in kt, "原生同一判据"
    assert "onSave = { save() }" in kt, "原生的写回点也只有保存这一颗"
    assert "Api.putSchedule" in kt


def test_both_ends_keep_the_draft_on_failure_and_clear_it_on_success():
    """pristine（基准指纹）只许在服务端回成功之后换；提前换成草稿 = 失败看起来像成功。"""
    js = _js()
    body = _js_fn(js, "saveSchedule")
    assert body.count("sched.pristine = schedSnapshot();") == 1, \
        "写回前就改基准：存失败时 dirty 会被悄悄清掉（改动还在，界面却说已同步）"
    assert body.index("sched.pristine = schedSnapshot();") > body.index("API.putSchedule"), \
        "基准必须在 await 之后换"
    assert "sched.saving = false" in body
    assert re.search(r"catch \(e\) \{[\s\S]*?sched\.error = e\.message", body), \
        "保存失败要留下 dirty（改了能直接再存），不许顺手把人写的清掉"

    kt = _code(_native("nativeapp/ui/ScheduleUi.kt"))
    save = _kt_fn(kt, "fun save()")
    assert save.count("SchedState.pristine = snapshotOf(") == 1, "原生同理：基准只许换一次"
    assert save.index("SchedState.pristine = snapshotOf(") > save.index("Api.putSchedule")
    assert "SchedState.error = e.message" in save
    # load 拆成 load（守卫）+ fetch（取数）后，换基准这件事归 fetch 管。
    assert _kt_fn(kt, "fun fetch(").count("SchedState.pristine = snapshotOf(") == 1


def test_calendar_is_disabled_while_loading_on_both_ends():
    js = _js()
    assert 'calbar.classList.toggle("busy", sched.loading)' in _js_fn(js, "renderSchedule")
    css = _css()
    assert ".calbar.busy" in css, "禁用态要有样式，不然只是改了个 class 名"
    kt = _code(_native("nativeapp/ui/ScheduleUi.kt"))
    assert "enabled = !SchedState.loading" in kt


def test_save_flushes_the_open_edit_row_on_both_ends():
    """2026-09-28 评审第 1 条：编辑行开着直接按保存，不许把空行/未收的行发给服务端。

    原生这份的病根是"编辑内容存在行自己的状态里，保存读到的还是空串"——修法是把
    每次击键实时写回草稿（onLive），并在保存前再滤一遍空文本行；网页那份靠 blur 先于
    click 的事件顺序侥幸躲过，但同一道滤也要有：不许把正确性押在时序上。
    """
    js = _js()
    body = _js_fn(js, "saveSchedule")
    assert re.search(r"sched\.items\.filter\(\(it\) => it\.text\.trim\(\)\)", body), \
        "网页保存前必须滤掉空文本行，别再让一行空文本 400 掉整天"
    kt = _sched_ui_kt()
    assert "val payload = SchedState.items.filter { it.text.isNotBlank() }" in kt, \
        "原生保存前必须滤掉空文本行"
    assert "onValueChange = { text = it; onLive(it, at) }" in kt, \
        "原生编辑行必须每敲一个字就实时写回草稿——保存不许赌『失焦提交先于点击』的时序"


def test_loading_keeps_the_list_visible_on_both_ends():
    """2026-09-28 评审第 2 条 + 真机反馈第 2 轮：换天在途时旧内容（清单**或空态**）
    置灰留着——上一天也是空的时候，藏掉空态就是用户看到的『先闪一帧空白』。"""
    render = _js_fn(_js(), "renderSchedule")
    assert 'items.classList.toggle("hidden", hasErr);' in render, \
        "清单区只许被错误态收起；加载中整块 hidden 就是那一帧『闪一下』"
    assert 'items.classList.toggle("busy", sched.loading)' in render
    assert 'items.classList.toggle("hidden", hasErr || sched.loading)' not in render
    assert 'empty.classList.toggle("hidden", hasErr || !!sched.items.length || (sched.loading && !sched.day))' in render, \
        "空态只许被错误态、有清单、或『首屏连锚点都没有』收起；换天在途藏掉它就是空白闪"
    assert 'empty.classList.toggle("busy", sched.loading)' in render
    css = _css()
    assert "#schedItems.busy" in css, "busy 要有样式（置灰 + 禁点），不然只是改了个 class 名"
    assert "#schedEmpty.busy" in css, "空态在途也要有同款置灰样式"
    kt = _sched_ui_kt()
    assert "val busy = SchedState.loading" in kt
    assert "alpha(if (busy) 0.55f else 1f)" in kt, "原生同理：在途时旧清单半透明留着"
    assert "interactive = !busy" in kt, "在途时行上的勾/删/改一律禁点"
    assert "SchedState.loading && SchedState.items.isEmpty() && SchedState.day.isEmpty()" in kt, \
        "原生留白只许发生在首屏（day 还没锚上）；换天在途要留空态"
    assert "alpha(if (SchedState.loading) 0.55f else 1f)" in kt, "原生空态在途同理置灰"


def test_time_is_picked_not_typed_on_both_ends():
    """2026-09-28 评审第 3 条 + 真机反馈：时间不靠手输；原生表盘换成了上下滚轮。
    写回格式仍是 HH:MM，仍允许留空。"""
    row = _js_fn(_js(), "schedRow")
    assert 'at.type = "time"' in row, "网页时间格要用原生时间控件（手机弹滚轮、桌面弹面板）"
    assert 'at.placeholder = "HH:MM"' not in row, "type=time 没有 placeholder 一说，别留死代码"
    kt = _sched_ui_kt()
    assert "private fun WheelColumn(" in kt, "原生要自己画上下滚轮的一列"
    assert "WheelColumn(count = 24" in kt and "WheelColumn(count = 60" in kt, \
        "滚轮是时(24)/分(60)两列，少一列就选不出完整时间"
    assert "TimePicker(state = st)" not in kt and "rememberTimePickerState(" not in kt, \
        "M3 表盘已被真机反馈判死（点不准），别引回来"
    assert '"%02d:%02d".format(wh, wm)' in kt, "选出来的必须钉回 HH:MM"


def test_days_before_today_cannot_be_picked_on_both_ends():
    """2026-09-27 评审第 1 条：今天以前的日子在月历里点不动。

    算式只有 `isBefore` 一条（ISO 定宽，字典序即日期序），但**用它**的地方有两处：
    网页 `b.disabled = past`，原生 `clickable = enabled && !past && !sel`。
    少写一处就是"看着灰着，其实还点得进去"——所以样式与判据都要各自钉。
    """
    js = _js()
    cell = _js_fn(js, "schedCell")
    assert "schedIsPast(iso, sched.today)" in cell, "网页格子没算'过去'这一格"
    assert "b.disabled = past" in cell, "网页过去那几格必须真的 disabled"
    css = _css()
    assert ".calcell.off" in css, "过去日没有样式：只是禁了点，看不出不能点"
    kt = _code(_native("nativeapp/ui/ScheduleUi.kt"))
    calcell = _kt_fn(kt, "private fun CalCell(")
    assert "ScheduleBoard.isBefore(iso, today)" in calcell, "原生格子没算'过去'这一格"
    assert "enabled && !past && !sel" in calcell, "原生过去日仍可点：与网页不同判据"


def test_month_and_year_navigation_is_clamped_to_today_on_both_ends():
    """翻月/翻年都要有，但都不许翻到今天所在月之前——那个月里一格都点不动。"""
    js = _js()
    move = _js_fn(js, "schedMoveMonth")
    assert "schedShiftMonth(" in move and "schedMonthFloor()" in move, "网页翻月没夹下界"
    assert "if (m[0] * 12 + m[1] < floor) return" in move, "网页翻月夹的是月序号，不是日子"
    floor = _js_fn(js, "schedMonthFloor")
    assert "if (!sched.today) return -1" in floor, "没锚点时网页整排钮该禁用"
    head = _js_fn(js, "syncCalHead")
    for btn in ("calPrevYear", "calPrevMonth", "calNextMonth", "calNextYear"):
        assert '$("' + btn + '").disabled' in head, f"网页抬头少了 {btn} 的可用态"
    for delta in ("-12", "-1", "1", "12"):
        assert f"schedMoveMonth({delta})" in js, f"网页少了翻月钮 one-shot：{delta}"
    kt = _code(_native("nativeapp/ui/ScheduleUi.kt"))
    nav = _kt_fn(kt, "fun moveMonth(")
    assert "ScheduleBoard.shiftMonth(" in nav, "原生没走同一份翻月算式"
    assert "return" in nav and "monthIndex(anchor[0], anchor[1])" in nav, "原生翻月没夹下界"
    for glyph, label in (("«", "上一年"), ("‹", "上个月"), ("›", "下个月"), ("»", "下一年")):
        assert f'CalNav("{glyph}", "{label}"' in kt, f"原生少了 {label} 那颗钮"


def test_grid_is_not_rebuilt_while_a_day_switch_is_in_flight():
    """2026-09-27 实测缺陷：手上草稿未保存时点别的日子，第一下不生效，要点第二下。

    根因是渲染无条件 `innerHTML = ""` 重建月历：blur 先收编辑行并 renderSchedule()，
    人正按着的那一格就在 mousedown 与 mouseup 之间被销毁，click 落空。
    所以这一片只许在"签名变了"时重建。锁在源码上：先比对签名、再 return，
    重建语句必须在 return 之后。运行时那一份判据见 _run_cal_js。
    """
    js = _js()
    render_cal = _js_fn(js, "renderCal")
    sig = _js_fn(js, "schedCalSignature")
    assert "schedCalSignature()" in render_cal, "月历重建没走签名判据：又回到无条件重建"
    assert render_cal.index("return") < render_cal.index("grid.innerHTML"), \
        "签名相同也要重建：等于没这道闸，点按还是会丢"
    assert sig.count("sched.days") == 1 and "sched.day" in sig and "sched.viewYear" in sig, \
        f"月历签名要覆盖 视图月/今天/选中/有安排：{sig}"


# ---------- 算式表：两端各自真跑，期望值只有这一份 ----------

TABLE = {
    "today": "2026-09-26",
    # 2026-09-26 = 周六（设计稿那一排：周一21 … 今天26 … 周一28）
    "weekday": {"2026-09-21": "周一", "2026-09-25": "周五", "2026-09-26": "周六",
                "2026-09-27": "周日", "2026-09-28": "周一"},
    "full_date": {"2026-09-26": "2026年9月26日", "2026-01-05": "2026年1月5日"},
    "day_label": {"2026-09-26": "2026年9月26日 · 今天", "2026-09-25": "2026年9月25日"},
    "day_number": {"2026-09-26": "26", "2026-10-09": "9"},
    "group_title": {"2026-09-26": "今日安排", "2026-10-01": "2026年10月1日安排"},
    "count_text": [[3, 1, "3 项 · 已完成 1"], [0, 0, "0 项 · 已完成 0"]],
    "cell_label": {"2026-09-26|false": "2026年9月26日 周六",
                   "2026-09-26|true": "2026年9月26日 周六，有安排"},
    # ---- 月历（2026-09-27 评审第 1 条换上的那一套算式）----
    "week_head": ["一", "二", "三", "四", "五", "六", "日"],
    "month_title": {"2026-8": "2026年9月", "2026-0": "2026年1月", "2026-11": "2026年12月"},
    # 2026-09-01 是周二 → 行首空一格；2026-02-01 是周日 → 行首空六格。都补齐到整周。
    "month_cells": {
        "2026-8": ["", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05",
                   "2026-09-06", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10",
                   "2026-09-11", "2026-09-12", "2026-09-13", "2026-09-14", "2026-09-15",
                   "2026-09-16", "2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20",
                   "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
                   "2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30",
                   "", "", "", ""],
        "2026-1": ["", "", "", "", "", "", "2026-02-01", "2026-02-02", "2026-02-03",
                   "2026-02-04", "2026-02-05", "2026-02-06", "2026-02-07", "2026-02-08",
                   "2026-02-09", "2026-02-10", "2026-02-11", "2026-02-12", "2026-02-13",
                   "2026-02-14", "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18",
                   "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
                   "2026-02-24", "2026-02-25", "2026-02-26", "2026-02-27", "2026-02-28",
                   ""],
    },
    "month_of": {"2026-09-26": "2026-8", "2026-01-05": "2026-0", "2026-12-31": "2026-11"},
    "month_nav": {"2026-8|1": "2026-9", "2026-8|-1": "2026-7", "2026-8|3": "2026-11",
                  "2026-8|12": "2027-8", "2026-8|-12": "2025-8", "2026-8|-9": "2025-11",
                  "2027-0|-1": "2026-11", "2026-11|1": "2027-0"},
    # 早于 today 的那些天才该点不动；今天本身可选。
    "past": {"2025-12-31": True, "2026-09-25": True, "2026-09-26": False, "2026-09-27": False,
             "": False},
    "bad_days": ["", "2026-9-6", "2026-02-30", "2026-13-01", "2026-09-26T00:00"],
}


SCHED_MATH_FNS = ("schedParse", "schedIso", "schedShift", "schedWeek", "schedFullDate",
                  "schedDayLabel", "schedGroupText", "schedCountText", "schedMonthTitle",
                  "schedMonthOf", "schedShiftMonth", "schedMonthCells", "schedCellLabel",
                  "schedIsPast")


def _run_sched_js() -> dict:
    """在 node 里真跑 app.js 那份日程纯算式函数（读磁盘原文，不读剥过注释的副本）。

    与 test_web_pwa._run_shell_js 同一理由：读源码只能证明"写了什么"，证明不了
    "算出什么"。日期换算最容易在跨月、跨闰、月日补零、月序号跨年进位这四处出错，
    判据只能问运行时。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    consts = re.search(r"^const SCHED_WEEK = .*$", js, flags=re.M)
    head_const = re.search(r"^const SCHED_WEEK_HEAD = .*$", js, flags=re.M)
    assert consts and head_const, "app.js 里那两张星期表改了名：这张判据表跟着失效"
    fns = "\n".join(_js_fn(js, name) for name in SCHED_MATH_FNS)
    harness = f"""
"use strict";
{consts.group(0)}
{head_const.group(0)}
{fns}
const T = {json.dumps(TABLE, ensure_ascii=False)};
const ym = (s) => s.split("-").map(Number);
const key = ([y, m]) => y + "-" + m;
const split = (s) => s.split("|");
function dayNumber(iso) {{ const d = schedParse(iso); return isNaN(d.getTime()) ? "" : String(d.getDate()); }}
console.log(JSON.stringify({{
  weekday: Object.fromEntries(Object.keys(T.weekday).map(d => [d, schedWeek(d)])),
  full_date: Object.fromEntries(Object.keys(T.full_date).map(d => [d, schedFullDate(d)])),
  day_label: Object.fromEntries(Object.keys(T.day_label).map(d => [d, schedDayLabel(d, T.today)])),
  day_number: Object.fromEntries(Object.keys(T.day_number).map(d => [d, dayNumber(d)])),
  group_title: Object.fromEntries(Object.keys(T.group_title).map(d => [d, schedGroupText(d, T.today)])),
  count_text: T.count_text.map(([n, m]) => schedCountText(
      Array.from({{length: n}}, (_, i) => ({{done: i < m}})))),
  cell_label: Object.fromEntries(Object.keys(T.cell_label).map(k => {{
    const [d, has] = split(k); return [k, schedCellLabel(d, has === "true")]; }})),
  week_head: SCHED_WEEK_HEAD,
  month_title: Object.fromEntries(Object.keys(T.month_title).map(k =>
      [...ym(k)].length ? [k, schedMonthTitle(...ym(k))] : [k, ""])),
  month_cells: Object.fromEntries(Object.keys(T.month_cells).map(k =>
      [k, schedMonthCells(...ym(k))])),
  month_of: Object.fromEntries(Object.keys(T.month_of).map(d => [d, key(schedMonthOf(d))])),
  month_nav: Object.fromEntries(Object.keys(T.month_nav).map(k => {{
    const [ymk, delta] = split(k); return [k, key(schedShiftMonth(...ym(ymk), Number(delta)))]; }})),
  past: Object.fromEntries(Object.keys(T.past).map(d => [d, schedIsPast(d, T.today)])),
  bad: T.bad_days.map(d => [d, !isNaN(schedParse(d).getTime()) && schedIso(schedParse(d)) === d]),
}}));
"""
    tmp = Path(tempfile.mkdtemp(prefix="sched-js-")) / "harness.cjs"
    tmp.write_text(harness, encoding="utf-8")
    r = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"harness 自己就跑失败了：\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout)


def test_web_date_math_matches_the_table():
    out = _run_sched_js()
    for key in ("weekday", "full_date", "day_label", "day_number", "group_title",
                "cell_label", "week_head", "month_title", "month_cells", "month_of",
                "month_nav", "past"):
        assert out[key] == TABLE[key], f"网页 {key} 与判据表不符：{out[key]}"
    assert out["count_text"] == [row[2] for row in TABLE["count_text"]], out["count_text"]
    assert [ok for _, ok in out["bad"]] == [False] * len(TABLE["bad_days"]), \
        f"这些串都不该被当成合法日期：{out['bad']}"


CAL_RUNTIME_FNS = SCHED_MATH_FNS + ("schedCell", "schedCalSignature", "renderCal",
                                    "schedMonthFloor", "schedMoveMonth", "syncCalHead")

_CAL_JS = """
"use strict";
const ok = (name, cond) => { if (!cond) throw new Error("FAIL: " + name); };
class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attrs = {}; this.className = "";
    this.disabled = false; this.textContent = ""; this._html = "";
  }
  appendChild(c) { this.children.push(c); return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(c)); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = v; if (v === "") this.children = []; }
}
const els = {};
const $ = (id) => (els[id] = els[id] || new El("div"));
const document = { createElement: (t) => new El(t) };
const loadCalls = [];
function loadSchedule(day) { loadCalls.push(day); }
let renderCount = 0;
function renderSchedule() { renderCount++; renderCal($("schedCalGrid")); syncCalHead(); }
let schedCalDrawn = "";
const sched = { day: "2026-09-27", today: "2026-09-27", days: ["2026-09-25", "2026-10-01"],
  viewYear: 2026, viewMonth: 8, loading: false };
__FNS__
const grid = $("schedCalGrid");
renderSchedule();
ok("九月摊开是整五周 35 格", grid.children.length === 35);
ok("月内 30 天都是可点的 button", grid.children.filter((c) => c.tag === "button").length === 30);
const cellOf = (iso) => grid.children.find((c) => (c.attrs["aria-label"] || "")
  .startsWith(schedFullDate(iso)));
ok("过去的格子被禁点", cellOf("2026-09-26").disabled === true);
ok("今天的格子可以点", cellOf("2026-09-27").disabled === false);
const before = cellOf("2026-09-28");
before.onclick();
ok("点 28 号发出了一次切换请求", loadCalls.length === 1 && loadCalls[0] === "2026-09-28");
sched.loading = true;
const drawn = grid.children.slice();
renderSchedule();
renderSchedule();
sched.loading = false;
ok("请求在途的多次重渲染没有重建格子（第二下才生效的病灶）",
   grid.children.length === drawn.length &&
   grid.children.every((c, i) => c === drawn[i]));
sched.day = "2026-09-28";
renderSchedule();
const after = cellOf("2026-09-28");
ok("真的换了天才重建", after !== before);
ok("选中态跟着挪过去", after.className.includes(" sel") &&
   !cellOf("2026-09-27").className.includes(" sel"));
sched.viewYear = 2026; sched.viewMonth = 8;
schedMoveMonth(-1);
ok("翻回上月被锚在今天所在的那个月", sched.viewYear === 2026 && sched.viewMonth === 8);
schedMoveMonth(1);
ok("翻到十月", sched.viewMonth === 9);
ok("十月 31 天整五周", grid.children.filter((c) => c.tag === "button").length === 31 &&
   grid.children.length === 35);
schedMoveMonth(-1);
schedMoveMonth(12);
ok("按年翻到明年同月", sched.viewYear === 2027 && sched.viewMonth === 8);
schedMoveMonth(-12);
ok("按年翻回也不会穿过去", sched.viewYear === 2026 && sched.viewMonth === 8);
ok("标题写着看的是哪个月", $("calTitle").textContent === "2026年9月");
ok("锚点月的往回两钮都 disabled", $("calPrevMonth").disabled && $("calPrevYear").disabled);
ok("向前两钮可用", !$("calNextMonth").disabled && !$("calNextYear").disabled);
console.log("OK");
"""


def _run_cal_js() -> None:
    """在 node 的假 DOM 里把"点格子 → 请求在途 → 重渲染 → 切换完成"整条走真.

    2026-09-27 用户实测的缺陷（未保存时点 28 号要按第二下才切）就是渲染重建
    在途节点造成的；静态断言只能证明"签名早退写在那儿"，证明不了"同样的输入
    再 render 一次，格子对象还是原来那批"。这条只能在运行时问。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    consts = re.search(r"^const SCHED_WEEK = .*$", js, flags=re.M)
    head_const = re.search(r"^const SCHED_WEEK_HEAD = .*$", js, flags=re.M)
    assert consts and head_const, "app.js 里那两张星期表改了名：这条运行时用例跟着失效"
    fns = "\n".join(_js_fn(js, name) for name in CAL_RUNTIME_FNS)
    harness = _CAL_JS.replace("__FNS__", f"{consts.group(0)}\n{head_const.group(0)}\n{fns}")
    tmp = Path(tempfile.mkdtemp(prefix="sched-cal-")) / "harness.cjs"
    tmp.write_text(harness, encoding="utf-8")
    r = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"月历运行时用例没过：\n{r.stdout}\n{r.stderr}"
    assert r.stdout.strip() == "OK", r.stdout


def test_runtime_grid_render_survives_an_inflight_day_switch():
    _run_cal_js()


# 原生那一侧必须在台架里真跑到这几个锚点日：跨月/跨闰/行首空几格，都只在锚点上才看得出算错。
JVM_MONTH_ANCHORS = ["2026-09-01", "2026-09-30", "2026-02-01", "2026-02-28", "2028-02-29"]


def test_jvm_table_of_literals_is_the_same_table_and_is_executed():
    """安卓那份的期望值就是上面这张表，而且它确实在台架/CI 里被执行过。

    这里不重复跑 javac（那是 tools/shell_jvm_tests.py 与 gradle testDebugUnitTest 的活），
    要钉的是"两端各自对齐的是同一张表"：JVM 用例里必须出现这一批同样的期望串，
    并且这一批算式（月标题、格子、翻月、过去日、格子标签）都得被它真调用一次。
    """
    src = JVM_TEST.read_text(encoding="utf-8")
    for value in list(TABLE["weekday"].values()) + list(TABLE["full_date"].values()) \
            + list(TABLE["day_label"].values()) + list(TABLE["group_title"].values()) \
            + list(TABLE["month_title"].values()) + list(TABLE["cell_label"].values()) \
            + list(TABLE["week_head"]) + [row[2] for row in TABLE["count_text"]]:
        assert value in src, f"JVM 用例里没有 {value!r}：两端的算式表分家了"
    for day in JVM_MONTH_ANCHORS:
        assert day in src, f"JVM 用例里没有 {day}：月历格子的锚点日没被钉住"
    for call in ("ScheduleBoard.monthTitle(", "ScheduleBoard.monthCells(2026, 8)",
                 "ScheduleBoard.monthOf(", "ScheduleBoard.shiftMonth(",
                 "ScheduleBoard.monthIndex(", "ScheduleBoard.isBefore(",
                 "ScheduleBoard.cellLabel(", "ScheduleBoard.WEEK_HEAD"):
        assert call in src, f"JVM 用例没调用 {call}：这一条算式只在网页那侧跑过"
    kt = _code(_native("nativeapp/ui/ScheduleUi.kt"))
    assert kt.count("ScheduleBoard.monthCells(SchedState.viewYear, SchedState.viewMonth)") == 1, \
        "原生月历格子必须只有 monthCells 这一个入口（第二条就是算式分家）"


def test_group_order_in_settings_is_the_same_on_both_ends():
    """一级列表的行序：记忆 → 日程 → 设备（两端同一处插，别一端在关于后面）。"""
    html = _html()
    pos_web = [html.index('<p class="set-group">记忆'), html.index('<p class="set-group">日程'),
               html.index('<p class="set-group">设备')]
    assert pos_web == sorted(pos_web), "网页的设置分组顺序变了"
    kt = _code(_native("nativeapp/ui/SettingsUi.kt"))
    pos_native = [kt.index('SetGroup("记忆")'), kt.index('SetGroup("日程")'),
                  kt.index('SetGroup("设备")')]
    assert pos_native == sorted(pos_native), "原生的设置分组顺序变了"


def test_second_level_page_key_and_title_match():
    js = _js()
    kt = _code(_native("nativeapp/ui/SettingsUi.kt"))
    assert re.search(r"const SET_PAGES = \{[^}]*schedule: \"日程\"", js, flags=re.S), \
        "网页 SET_PAGES 少了 schedule 这一条"
    assert re.search(r'val SET_PAGES = mapOf\([^)]*"schedule" to "日程"', kt, flags=re.S), \
        "原生 SET_PAGES 少了 schedule 这一条"
    assert '"schedule" -> SchedulePage(' in kt, "原生 when 分派没接上日程页"
    assert 'if (name === "schedule") loadSchedule();' in js, "网页进页没拉数据"


def test_unsaved_changes_are_asked_before_leaving_on_web():
    js = _js()
    guard = _js_fn(js, "schedCanLeave")
    assert "confirm(" in guard and "有未保存的修改" in guard, "离开日程页要有二次确认"
    # 原生同理：Compose 里没有 confirm，口径是"改动不静默丢弃"——页面销毁即草稿丢弃，
    # 但同一会话内切天/切页都走 load() 的草稿优先分支，不许把人写的盖掉。
    kt = _code(_native("nativeapp/ui/ScheduleUi.kt"))
    assert "snapshotOf(SchedState.items) != SchedState.pristine" in _kt_fn(kt, "fun load("), \
        "原生缺草稿优先判据：切回来会覆盖未保存内容"
    assert "schedSnapshot() !== sched.pristine" in _js_fn(_js(), "loadSchedule"), \
        "网页同理：这一页的草稿必须挡住一次新的 GET"
    for handler in ('$("closeSettings").onclick', '$("setBack").onclick', '$("settings").onclick'):
        at = js.index(handler)
        assert "schedCanLeave()" in js[at:at + 200], f"{handler} 没过未保存这道门"

# ---------- 运行时判据：把"顺序对就够了"升级成"结果必须对" ----------
#
# 上面那批锁是源码顺序锁：`sched.pristine = schedSnapshot();` 出现一次、排在
# `API.putSchedule` 之后。顺序对而结果错的做法它拦不住，最典型的一条是
# "顺序完全合法，但 pristine 是在 items 被服务端回包覆盖**之前**定的"——
# 界面会永远显示"有未保存修改"，而源码每一行都还是那个顺序。
# 所以这里把仓库那份 saveSchedule 原文放进 node 真跑一遍：成功、失败、
# 服务端回包改了内容这三种结局各问一次运行时。

def _run_save_js(outcome: str) -> dict:
    """真跑 app.js 的 saveSchedule()：outcome ∈ {"ok", "normalized", "fail400", "auth401"}。

    叶子全部是假的（API/渲染/needsAuth），函数本体一个字不改地从磁盘原文里取——
    判的是仓库那份实现，不是测试自己的复刻。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node，跑不了这段 JS")
    js = _js()
    # `_js_fn` 从 `function 名字(` 起切，`async` 这个前缀会被它吃掉——真跑得补回来，
    # 否则切出来的那份在第一个 `await` 上直接 SyntaxError，harness 连门都进不去。
    body = ("async " if "async function saveSchedule(" in js else "") + _js_fn(js, "saveSchedule")
    snap = _js_fn(js, "schedSnapshot")
    state = _const_object(js, "sched")
    harness = """
"use strict";
const API = { putSchedule: async (day, payload) => {
  global.__reqs.push([day, payload]);
  if (global.__fail) { const e = new Error(global.__fail); e.status = global.__status; throw e; }
  return global.__echo;
} };
function renderSchedule() { global.__renders++; }
function flashSaved() { global.__flashes++; }
function needsAuth(e) { return !!e && e.status === 401; }
$STATE
$SNAP
$BODY
global.__reqs = []; global.__renders = 0; global.__flashes = 0;
global.__echo = null; global.__fail = null; global.__status = 0;
global.__setup = (o) => {
  sched.day = "2026-09-26"; sched.today = "2026-09-26"; sched.days = ["2026-09-20"];
  sched.items = [{ text: "  写周报  ", at: "09:30", done: false }];
  sched.pristine = "[]";              // 服务端那份还是空的，手上有草稿 = 未保存
  if (o === "fail400") { global.__fail = "日期要写成 YYYY-MM-DD"; global.__status = 400; }
  if (o === "auth401") { global.__fail = ""; global.__status = 401; }
  if (o === "ok") global.__echo = { day: "2026-09-26", count: 1,
    items: [{ text: "  写周报  ", at: "09:30", done: false }] };
  if (o === "normalized") global.__echo = { day: "2026-09-26", count: 1,
    items: [{ text: "写周报", at: "09:30", done: false }] };   // 服务端把空格去了
  if (o === "double") global.__echo = { day: "2026-09-26", count: 1,
    items: [{ text: "  写周报  ", at: "09:30", done: false }] };  // 连点两颗同一份
};
global.__run = async (o) => {
  global.__setup(o);
  const before = sched.pristine;
  if (o === "double") await Promise.all([saveSchedule(), saveSchedule()]);
  else await saveSchedule();
  return {
    outcome: o,
    before,
    pristine: sched.pristine,
    items: sched.items,
    dirty: schedSnapshot() !== sched.pristine,
    error: sched.error,
    saving: sched.saving,
    days: sched.days,
    requests: global.__reqs.length,
    payload: global.__reqs[0] ? global.__reqs[0][1] : null,
    flashes: global.__flashes,
  };
};
"""
    harness = harness.replace("$STATE", state).replace("$SNAP", snap).replace("$BODY", body)
    tmp = Path(tempfile.mkdtemp(prefix="sched-save-")) / "harness.cjs"
    tmp.write_text(harness + "\n;(async () => console.log(JSON.stringify(await global.__run(%s))))();\n"
                   % json.dumps(outcome), encoding="utf-8")
    r = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"harness 自己就跑失败了：\n{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout)


def test_failed_save_keeps_dirty_and_never_claims_saved():
    """存失败之后：基准不许动、dirty 必须还在、错误是服务端原文、不许闪「已保存」。"""
    out = _run_save_js("fail400")
    assert out["pristine"] == out["before"], \
        "保存失败却把 pristine 换成了草稿：dirty 被悄悄清掉，人以为存上了（改动其实还在手里）"
    assert out["dirty"] is True, "失败之后必须还是 dirty——保存钮要重新亮起来，改了能直接再存"
    assert out["error"] == "日期要写成 YYYY-MM-DD", f"错误位必须是服务端 detail 原文：{out['error']!r}"
    assert out["flashes"] == 0, "失败这一趟不许闪「已保存」"
    assert out["saving"] is False, "saving 必须在 finally 里落回 false，否则这颗钮永久卡死"
    assert out["items"][0]["text"] == "  写周报  ", "失败不许顺手清洗人写的东西"
    assert out["days"] == ["2026-09-20"], "失败不许改动日期条上的星点"


def test_unauthorized_save_does_not_look_like_a_successful_save():
    out = _run_save_js("auth401")
    assert out["flashes"] == 0, "掉登录这一趟不许闪「已保存」"
    assert out["dirty"] is True and out["pristine"] == out["before"], \
        "401 也要保留草稿与 dirty：重新登录后还得能把刚才那一条存下去"


def test_pristine_follows_the_servers_echo_not_the_local_draft():
    """服务端把文本里的空格去掉时，基准必须跟着**回包**走，而不是跟着送出去的草稿走。

    顺序锁在这里是绿的：`sched.pristine = schedSnapshot();` 仍然只出现一次、仍然排在
    `API.putSchedule` 之后。但把它定在 items 被回包覆盖**之前**，dirty 会永远清不掉，
    界面一直挂着"有未保存修改"。只有真跑能问出这一格。
    """
    out = _run_save_js("normalized")
    assert out["dirty"] is False, "服务端已确认，界面却还在喊未保存：基准跟错了对象"
    assert out["items"][0]["text"] == "写周报", "清单要以服务端回包为准"
    assert out["flashes"] == 1, "确认成功才许闪那一次「已保存」"
    assert out["error"] == "", "成功这一趟不许留着上一轮的错"


def test_saved_day_dot_and_payload_shape_survive_the_round_trip():
    out = _run_save_js("ok")
    assert out["dirty"] is False and out["flashes"] == 1
    assert "2026-09-26" in out["days"], "count>0 就要把这一天补上星点（不靠猜别的天）"
    assert out["requests"] == 1, "一次点保存只发整天 PUT，不许重放"
    assert out["payload"] == [{"text": "  写周报  ", "at": "09:30", "done": False}], \
        f"整天 PUT 的载荷形状变了：{out['payload']}"


def test_rapid_double_click_sends_one_whole_day_put():
    """连点两颗保存只发一次整天 PUT：第二次必须被 saving 闸门挡在门外。

    这条只能真跑：grep 看得见 `if (sched.saving) return;` 那一行在不在，
    看不见它挡不挡得住——把闸门挪到 await 之后，源码里那行字还在。
    """
    out = _run_save_js("double")
    assert out["requests"] == 1, f"两次点按发了 {out['requests']} 次整天替换，后一次会把前一次的响应盖掉"
    assert out["dirty"] is False and out["error"] == ""
    assert out["saving"] is False
