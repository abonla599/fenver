"""原生「设置 → 关于 → 版本」行：一个号码 + 「检查更新」胶囊（方案 B）。

判什么
------
常驻的版本值只许有一个号码——这台设备上真装的那一版（BuildConfig.VERSION_NAME，
由 android-native/app/build.gradle 烘进包）。独立的「检查更新」行已删除：检查入口
是版本行值槽里的一颗胶囊，更新各态全部走带标签的瞬态面——检查中/已是最新/失败走
顶部 note，发现新版弹 UpdateAvailableDialog，下载/安装沿用既有两张弹层。常驻列表
永不保留第二个号：通道号在回滚后可能比已装版更旧，服务端号装不到手机上，两个号
不带标签并排摆正是让用户读错的形状。禁止字符串那一族是这条指令的机制，不是装饰。

「已是最新」不带号：旧文案报的是**通道**版本（`已是最新版本 v${d.version}`），
通道回滚到比已装更旧时它会显示一个比版本行更小的号——那一形已判死，见
test_the_up_to_date_note_carries_no_version。

射程（实测范围，不是全覆盖宣称）
--------------------------------
本锁读剥注释源码（_code），把版本行切片里 `Updater.startCheck(` 之前的部分当
「常驻渲染段」（valSlot 的组合内容），判：第二个值槽 / 裸 Text / 字面号码 /
字符串插值 / contentDescription / semantics / AnnotatedString / 读通道决定。
仍漏（与 web 侧 #334 同族，如实登记）：
- 别名/间接层：号码经另一个变量或别处定义的 Modifier 扩展进入渲染段，文本判据看不见；
- 组件内部：SetValText/SetMini/FenverRow 自身定义里若加 contentDescription 或拼接，
  不在本文件切片内（它们住在 FenverComponents.kt，本锁不读那份）；
- 运行期复合：shellVer 来源正则钉死了它的拼法，但「把第二个号拼进一个再传入的
  变量」这类运行期形状，源码文本锁判不到；
- 渲染树：本机无 Compose JVM 宿主，不能像 web 的 DOM 锁那样数渲染后的号码。
所以这里不写「第二个号已不可能出现」，只写上面这些形状会被咬住。

语料用 `_code()`（剥注释那把尺子，住在 test_apk_release_discipline.py）：
说明注释里原样写着旧文案，判"某串不存在"的条目读原文会假红，读剥掉注释的不会。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_apk_release_discipline import _code                    # noqa: E402
from tests.test_schedule_ui_contract import _native                   # noqa: E402

REPO = Path(__file__).resolve().parents[2]

# 行首字形是 FenverIcon 枚举报（v0.36.1 图标语言批起）。UPDATE_ROW 保留为
# 「必须消失」的锚点：方案 B 删掉了独立的检查更新行，它再出现就是第二个常驻面。
VERSION_ROW = 'SetRow(FenverIcon.Info, "版本"'
UPDATE_ROW = 'SetRow(FenverIcon.Refresh, "检查更新"'
NEXT_ROW = 'SetRow(FenverIcon.Mail, "意见反馈"'

# 禁止字符串那一族：第二个号的来源与拼装手法。条数与集合各钉一道（独立手抄，
# 不是 len() 派生）——砍味或换味都必红，见 test_the_forbidden_list_keeps_its_full_length。
FORBIDDEN = ("serverBuild", "Api.health", "服务端", "joinToString", "listOfNotNull", '"壳 ')
FORBIDDEN_COUNT = 6
FORBIDDEN_SET = frozenset({"serverBuild", "Api.health", "服务端", "joinToString",
                           "listOfNotNull", '"壳 '})

# 常驻渲染段不许出现的属性/语义位与富文本拼装（Compose 侧的「属性位藏号」同族）。
_ATTR_MARKERS = ("contentDescription", "semantics", "AnnotatedString", "buildAnnotatedString")


def _ui() -> str:
    return _code(_native("nativeapp/ui/SettingsUi.kt"))


def _update_ui() -> str:
    return _code(_native("nativeapp/ui/UpdateUi.kt"))


def _slice(text: str, start: str, stop: str) -> str:
    at = text.index(start)
    return text[at:text.index(stop, at)]


def _persistent_row(ui: str) -> str:
    """版本行的常驻渲染段：锚点起、到检查动作（startCheck）之前。

    胶囊 onClick 里的回调是瞬态动作（note/弹窗），不是常驻显示；它的文案另由
    五态判据钉。切在 startCheck 是因为回调体必然跟在它后面——这是文本近似，
    不是控制流分析，射程见模块 docstring。
    """
    ver = _slice(ui, VERSION_ROW, NEXT_ROW)
    return ver[:ver.index("Updater.startCheck(")]


def _version_row_problems(ui: str) -> list[str]:
    """常驻版本行单号谓词：返回问题标签列表（空 = 通过）。

    正向锁与反向锁共用这一份：反向锁往合成语料里注入第二个号，断言这里报得出。
    """
    problems = []
    if ui.count(VERSION_ROW) != 1:
        return ["anchor-version"]
    if ui.count(NEXT_ROW) != 1:
        return ["anchor-next"]
    ver = _slice(ui, VERSION_ROW, NEXT_ROW)
    if "Updater.startCheck(" not in ver:
        return ["no-trigger"]
    persist = ver[:ver.index("Updater.startCheck(")]
    if persist.count("SetValText(") != 1 or "SetValText(shellVer)" not in persist:
        problems.append("value-slot")
    for flavor in FORBIDDEN:
        if flavor in ver:
            problems.append("forbidden:" + flavor)
    for marker in _ATTR_MARKERS:
        if marker in persist:
            problems.append("attr:" + marker)
    if re.search(r'"[^"\n]*\d+\.\d+[^"\n]*"', persist):
        problems.append("literal-number")
    if re.search(r'"[^"\n]*\$[^"\n]*"', persist):
        problems.append("interpolation")
    if ".version" in persist or "Updater.decision" in persist:
        problems.append("channel-read")
    if "Text(" in persist.replace("SetValText(", ""):
        problems.append("raw-text")
    return problems


def test_the_corpus_is_the_real_settings_screen():
    """正对照：锚点各就各位、切片真切到东西，否则下面每条都在空串上判、恒绿。"""
    ui = _ui()
    assert ui.count(VERSION_ROW) == 1, "「版本」那一行不再是恰好一处（改名/复制都会让切片失真）"
    assert ui.count(NEXT_ROW) == 1, "「意见反馈」锚点不再是恰好一处"
    assert ui.count(UPDATE_ROW) == 0, \
        "独立的「检查更新」行又回来了——方案 B 把它删了，检查入口是版本行里的胶囊"
    ver = _slice(ui, VERSION_ROW, NEXT_ROW)
    assert len(ver) > 200, f"「版本」那一行的切片短得不像话：{ver!r}"


def test_the_version_row_shows_the_shell_version_alone():
    """核心那条：常驻渲染段只有壳那一个号，没有任何第二个来源参与。"""
    ui = _ui()
    problems = _version_row_problems(ui)
    assert not problems, f"常驻版本行不再是单号（问题：{problems}）"
    # shellVer 本身也只能有一个来源：出包那份 gradle 烘进 BuildConfig 的 versionName
    assert re.search(r'val shellVer = "v" \+ BuildConfig\.VERSION_NAME', ui), \
        "shellVer 不再只由 BuildConfig.VERSION_NAME 拼出来——版本号出现了第二个来源"


def test_the_second_number_has_no_source_left_on_the_settings_page():
    """整份文件里第二个号的来源一并没了：留着"拉来不用"等于白跑一次网络还留着坑。"""
    ui = _ui()
    assert "serverBuild" not in ui, "serverBuild 那份状态还在（值不许显示，来源也不该留着）"
    assert "Api.health" not in ui, "设置页还在拉 /health 的 build"
    assert "versionVal" not in ui, "两号相加那个中间变量还在，说明拼接的逻辑没走远"


def test_the_update_row_still_names_the_installable_version():
    """「检查更新」那一行已删，但可安装版本仍必须被报出来——现在由带标签的弹层报。

    用例名保留旧台账条目：守的意图仍是「可安装版本有出口、检查动作有人点」，
    不是行的形状。三条文案锁迁往新载体：已是最新→note（不带号）、发现新版→
    UpdateAvailableDialog 标题、触发动作→版本行胶囊 onClick。
    """
    ui = _ui()
    ver = _slice(ui, VERSION_ROW, NEXT_ROW)
    assert "Updater.startCheck(" in ver, "检查动作不再在版本行（胶囊）里"
    assert 'onNote("已是最新版本", false)' in ui, "「已是最新」不再走顶部 note"
    upd = _update_ui()
    assert "发现新版本 v$version" in upd, "发现新版弹层不再报可安装的那一版"


def test_the_server_build_field_is_still_served():
    """反向一半：/health 的 build 字段没被删（AGENTS.md §1/§5 的判据依赖它）。"""
    main = (REPO / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    assert '"build": build_version()' in main, \
        "/health 不再回 build 字段——换包验收与 tools/pre_tag_gate.py 读的就是它"
    buildinfo = (REPO / "backend" / "app" / "core" / "buildinfo.py").read_text(encoding="utf-8")
    assert "def build_version() -> str:" in buildinfo, "构建戳的取法被删了"


def test_the_native_health_client_is_untouched():
    """客户端那条 health() 也留着：删它不属于本次范围，且它是公开端点的对位。"""
    api = _code(_native("nativeapp/Api.kt"))
    assert 'suspend fun health(): JsonObject' in api, "Api.health() 被删了"
    assert 'call("/health")' in api, "Api.health() 不再打 /health"


# ---------- 五态各一格：号码只许出现在带标签的瞬态里 ----------


def test_the_checking_state_says_so_without_a_number():
    """检查中：胶囊文字换成「正在检查更新…」并禁用，不带任何号码。"""
    persist = _persistent_row(_ui())
    assert 'SetMini(if (Updater.checking) "正在检查更新…" else "检查更新"' in persist, \
        "检查中态不再是胶囊文字（或文案漂了）"
    assert "enabled = !Updater.checking" in persist, "检查中胶囊不再禁用：连点会叠检查"


def test_the_up_to_date_note_carries_no_version():
    """已是最新：note 不带号。旧文案报的是通道版本，通道回滚时会比版本行更小。"""
    ui = _ui()
    assert 'onNote("已是最新版本", false)' in ui, "「已是最新」note 不在或形状漂了"
    assert "已是最新版本 v" not in ui, \
        "带号的「已是最新版本 v…」回来了——通道回滚时它会显示一个比已装版更小的号"


def test_the_offer_dialog_labels_the_number_and_only_offers_download():
    """发现新版：号码只在带标签的瞬态弹层标题里；弹层只暴露下载，不再检查。"""
    ui = _ui()
    upd = _update_ui()
    assert "fun UpdateAvailableDialog(version: String" in upd, "发现新版弹层不在了"
    assert 'Text("发现新版本 v$version"' in upd, "弹层标题不再带标签报号（或改成了手写号）"
    assert 'FenverButton("稍后"' in upd and 'FenverButton("立即更新"' in upd, \
        "弹层的两颗按钮（稍后/立即更新）不齐"
    assert "Updater.startCheck(" not in upd, \
        "更新弹层里又调了 startCheck——全树恰好一处的纪律会被打破（检查入口只有胶囊）"
    # 挂载是瞬态的：待弹旗标非空才挂，消费（更新/稍后）即清
    assert "var offerUpdate by remember { mutableStateOf<ReleasePlan.Decision?>(null) }" in ui, \
        "待弹旗标不在了（重组会反复弹，或弹层失去一次性）"
    assert "offerUpdate?.let" in ui and "UpdateAvailableDialog(" in ui, "弹层没有挂在待弹旗标后面"
    assert ui.count("offerUpdate = null") >= 2, "消费后不清旗标：弹层会赖着或反复弹"


def test_the_failure_note_names_the_reason_without_a_number():
    """失败：note 只插原因，不插版本号。"""
    ui = _ui()
    assert 'onNote("检查更新失败：$err", true)' in ui, "失败态不再走带原因的 note"


def test_the_download_and_install_sheets_still_mount():
    """下载/安装：沿用既有两张瞬态弹层，挂载不许被顺手删掉。"""
    ui = _ui()
    assert "if (Updater.downloading) {" in ui, "下载进度弹层的挂载条件没了"
    assert "UpdateProgressDialog(onCancel = { Updater.cancelDownload() })" in ui, \
        "下载进度弹层不再挂"
    assert "if (Updater.installGuideVersion != null) {" in ui, "授权引导弹层的挂载条件没了"
    assert "InstallGuideDialog(" in ui, "授权引导弹层不再挂"


# ---------- 常驻单号的结构判据 + 反向锁（合成语料实跑） ----------


def test_the_persistent_version_row_has_exactly_one_number():
    """常驻渲染段的结构：唯一值槽、无属性/语义位、无字面号、无插值、不读通道。"""
    persist = _persistent_row(_ui())
    assert persist.count("SetValText(") == 1 and "SetValText(shellVer)" in persist, \
        "常驻值槽不再是唯一的 SetValText(shellVer)"
    for marker in _ATTR_MARKERS:
        assert marker not in persist, f"常驻渲染段出现了属性/语义位 {marker}（第二个号的藏身处）"
    assert not re.search(r'"[^"\n]*\d+\.\d+[^"\n]*"', persist), "常驻渲染段有手写号码字面量"
    assert not re.search(r'"[^"\n]*\$[^"\n]*"', persist), "常驻渲染段的字符串里有插值"
    assert ".version" not in persist and "Updater.decision" not in persist, \
        "常驻渲染段在读通道决定——第二个号的来源"
    assert "Text(" not in persist.replace("SetValText(", ""), "常驻渲染段绕过值槽直接 Text()"


def _inject(ui: str, needle: str, extra: str) -> str:
    assert ui.count(needle) == 1, f"注入锚点不唯一：{needle!r}"
    return ui.replace(needle, needle + extra, 1)


def test_the_single_number_predicate_bites_a_second_number():
    """反向锁：往常驻版本行注入第二个号的自然形状，谓词必须报得出。

    每一味对应一种真会发生的写法（第二值槽/无障碍属性/条件追加/手写号/插值/
    语义位/富文本/裸 Text）。合成语料直接喂给与正向锁同一个谓词。
    """
    ui = _ui()
    assert not _version_row_problems(ui), "正对照失败：真源码本身就有问题"
    anchor = "SetValText(shellVer)"
    cases = [
        ("second-slot", _inject(ui, anchor, '\n        SetValText("v" + d.version)'),
         {"value-slot", "channel-read"}),
        ("content-description", _inject(
            ui, anchor, '\n        Modifier.semantics { contentDescription = "通道 v0.36.5" }'),
         {"attr:contentDescription", "attr:semantics", "literal-number"}),
        ("conditional-append", _inject(
            ui, anchor,
            '\n        if (Updater.decision != null) SetValText("通道 " + Updater.decision!!.version)'),
         {"value-slot", "channel-read"}),
        ("literal-number", _inject(ui, anchor, '\n        SetValText("v0.39.2")'),
         {"value-slot", "literal-number"}),
        ("interpolation", ui.replace('"检查更新"', '"检查更新 ${Updater.decision?.version}"', 1),
         {"interpolation", "channel-read"}),
        ("semantics-empty", _inject(ui, anchor, '\n        Modifier.semantics { }'),
         {"attr:semantics"}),
        ("annotated-string", _inject(
            ui, anchor, '\n        val a = buildAnnotatedString { append("v0.36.5") }'),
         {"attr:AnnotatedString", "attr:buildAnnotatedString", "literal-number"}),
        ("raw-text", _inject(ui, anchor, '\n        Text("v0.36.5")'),
         {"raw-text", "literal-number"}),
    ]
    for name, corpus, want in cases:
        got = set(_version_row_problems(corpus))
        assert want <= got, f"反向锁没咬住 {name}：期望 {want}，实得 {got}"


def test_each_forbidden_flavor_still_bites():
    """禁止字符串逐味反例：把每一味写回常驻版本行，谓词必须按该味报红。

    注入形如 `val serverBuild = ""`——只有禁字判据能咬住它（值槽仍唯一、无号码
    字面量），证明每一味各自有牙，不是靠别的判据顺带兜住。
    """
    ui = _ui()
    anchor = "SetValText(shellVer)"
    payloads = {
        "serverBuild": '\n        val serverBuild = ""',
        "Api.health": '\n        val h = Api.health',
        "服务端": '\n        val s = "服务端"',
        "joinToString": '\n        val j = listOf("").joinToString()',
        "listOfNotNull": '\n        val n = listOfNotNull("")',
        '"壳 ': '\n        val k = "壳 "',
    }
    assert set(payloads) == FORBIDDEN_SET, "反例清单与禁字清单不再一一对应"
    for flavor, payload in payloads.items():
        got = _version_row_problems(_inject(ui, anchor, payload))
        assert "forbidden:" + flavor in got, f"禁字 {flavor!r} 写回去却没红：{got}"


def test_the_forbidden_list_keeps_its_full_length():
    """条数钉 + 集合钉：砍味（len 变）与换味（等长换词）都必红。

    那个 6 与这份 frozenset 都是独立手抄，不是从 FORBIDDEN 派生——否则清单
    变短时判据跟着变短，恒绿。
    """
    assert len(FORBIDDEN) == FORBIDDEN_COUNT, \
        f"禁字清单条数漂了：{len(FORBIDDEN)} != {FORBIDDEN_COUNT}（砍味/加味都要先过这一格）"
    assert frozenset(FORBIDDEN) == FORBIDDEN_SET, "禁字清单被换味：集合与手抄副本不再相等"