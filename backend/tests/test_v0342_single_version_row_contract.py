"""原生「设置 → 关于 → 版本」行：平时只有一个号码，有新版才多一颗「发现新版本」。

判什么
------
2026-11 用户改的交互口径（原话）：「这个版本号旁边的检查更新按钮，我想要实现的效果
就是当如果有新版本的时候，然后这个按钮才会弹出来，如果没有新版本的话，就没有检查
更新这个按钮，就只是显示版本号……就改成发现新版本这个按钮，然后点击就可以下载新版本了。」

于是这一行有三条硬要求，本文件逐条钉住：

A. **常驻只有一个号码**：这台设备上真装的那一版（BuildConfig.VERSION_NAME，由
   android-native/app/build.gradle 烘进包）。通道号在回滚后可能比已装版更旧，服务端号
   装不到手机上——两个号不带标签并排摆正是让人读成「这一版我能装」的形状。禁止字符串
   那一族是这条指令的机制，不是装饰。
B. **没有「检查更新」这颗常驻胶囊**：检查不再是用户要按的动作，而是进设置页时静默
   发生一次（Updater.offer，带 30 分钟缓存闸）。那一行在没新版时什么都不多显示，
   也就不会有「已是最新版本」这类噪音提示——用户点名不要。
C. **有新版本才露出「发现新版本 vX.Y」**，且全页只有这一处报可安装的是哪一版；
   点它一步进下载（Updater.attemptDownload），不再分「先检查再更新」两步。

为什么新口径比旧口径更对（不是「用户改主意了」就算了）
------------------------------------------------------
旧口径（版本行里一颗常驻「检查更新」胶囊 + 查到 AVAILABLE 时弹一次性邀约弹层）有两个
结构性问题：① 那颗胶囊在绝大多数时间里是**无效入口**——没有新版时点它只会换来一句
「已是最新版本」的顶部提示，而那正是用户点名要删的噪音；② 可安装版本被报在**弹层**里，
弹层一划掉就再也找不回来，用户只能再点一次胶囊。新口径把「有没有新版」这个事实从一个
动作的结果，变成行本身的状态：有就有按钮，没有就只有号码；按钮同时是「可安装的是哪
一版」的唯一出处，点下去直接下载。判据的形状也跟着从「常驻胶囊的字面文案」变成
「按钮的挂载条件 + 号码只在按钮里 + 点击一步进下载」，比旧口径更难被悄悄改坏。

射程（实测范围，不是全覆盖宣称）
--------------------------------
本锁读剥注释源码（_code），把版本行切片里 `Updater.attemptDownload(` 之前的部分当
「常驻渲染段」（valSlot 的组合内容），判：第二个值槽 / 裸 Text / 字面号码 / 字符串插值 /
contentDescription / semantics / AnnotatedString。仍漏（与 web 侧 #334 同族，如实登记）：
- 别名/间接层：号码经另一个变量或别处定义的 Modifier 扩展进入渲染段，文本判据看不见；
- 组件内部：SetValText/SetMini/FenverRow 自身定义里若加 contentDescription 或拼接，
  不在本文件切片内（它们住在 FenverComponents.kt，本锁不读那份）；
- 运行期复合：shellVer 来源正则钉死了它的拼法，但「把第二个号拼进一个再传入的
  变量」这类运行期形状，源码文本锁判不到；
- 渲染树：本机无 Compose JVM 宿主，不能像 web 的 DOM 锁那样数渲染后的号码。
所以这里不写「第二个号已不可能出现」，只写上面这些形状会被咬住。

语料用 `_code()`（剥注释那把尺子，住在 test_apk_release_discipline.py）：
说明注释里原样写着旧文案，判「某串不存在」的条目读原文会假红，读剥掉注释的不会。

口径变更的账（旧锁 → 新锁，逐条交代为什么不是「删断言了事」）
--------------------------------------------------------------
1. `test_the_checking_state_says_so_without_a_number` → 删除并替换为
   `test_the_checking_chip_is_gone`。旧锁钉的是常驻胶囊的「正在检查更新…」禁用态；
   新口径里**没有检查态**，静默检查不许驱动任何一帧（Updater.asking 刻意不是 Compose
   状态）。静态锁没有对象了，判据改钉「那两颗旧胶囊字面量一个字都不许回来」。
2. `test_the_up_to_date_note_carries_no_version` → 转强。不是「不带号」，而是**连这句
   都不许有**（用户点名不要这噪音）。见 `test_no_up_to_date_banner_and_no_failure_banner_by_default`。
3. `test_the_offer_dialog_labels_the_number_and_only_offers_download` → 转写为
   `test_the_offer_button_labels_the_number_and_starts_the_download`。旧锁要求号码在
   弹层标题、弹层不许调检查；新口径号码在按钮文字里、点击一步进下载。「只有一个号码
   出口」的意图没降：旧弹层被删掉（UpdateUi.kt 里不许再有第二处报号）。
4. `test_the_failure_note_names_the_reason_without_a_number` → 收紧成
   `test_the_download_path_reports_human_reasons` + 上面第 2 条里的一半：失败文案的
   载体从「检查失败的顶部 note」变成「点按钮之后才可能出现的 note」，且静默那一路
   没有任何回调可挂。
5. 新增 `test_the_silent_check_runs_once_per_visit_and_is_cached`：把「不许每次开设置
   都打网络」钉成可数的事实（30 分钟闸 + 全仓恰好一个 offer() 调用点 + 它不在按钮里）。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_apk_release_discipline import _code                    # noqa: E402
from tests.test_schedule_ui_contract import _native                   # noqa: E402

REPO = Path(__file__).resolve().parents[2]

# 行首字形是 FenverIcon 枚举报（v0.36.1 图标语言批起）。
VERSION_ROW = 'SetRow(FenverIcon.Info, "版本"'
# 三条「必须消失」的锚点：独立行、旧常驻胶囊、旧邀约弹层都是旧口径的入口。
UPDATE_ROW = 'SetRow(FenverIcon.Refresh, "检查更新"'
UPDATE_CHIP = 'SetMini(if (Updater.checking)'
OLD_DIALOG = 'UpdateAvailableDialog('
NEXT_ROW = 'SetRow(FenverIcon.Mail, "意见反馈"'
# 新口径的按钮：文字里带版本号（号码只出现在这一颗带标签的按钮里），点击一步进下载。
OFFER_CHIP = 'SetMini("发现新版本 " + Updater.available.orEmpty()'
OFFER_CLICK = 'Updater.attemptDownload(appCtx)'

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


def _updater() -> str:
    return _code(_native("nativeapp/update/Updater.kt"))


def _slice(text: str, start: str, stop: str) -> str:
    at = text.index(start)
    return text[at:text.index(stop, at)]


def _row(ui: str) -> str:
    """版本行整段（锚点起、到「意见反馈」之前）——只给「整段里恰好一处」那几条用。"""
    return _slice(ui, VERSION_ROW, NEXT_ROW)


def _persistent_row(ui: str) -> str:
    """版本行的常驻渲染段：锚点起、到点击动作（attemptDownload）之前。

    切在 attemptDownload 是因为回调体必然跟在它后面；那段 lambda 是被点出来的
    瞬态动作（下载/一句人话），不是常驻显示。这是文本近似，不是控制流分析，
    射程见模块 docstring。
    """
    ver = _row(ui)
    return ver[:ver.index(OFFER_CLICK)]


def _version_row_problems(ui: str) -> list[str]:
    """常驻版本行单号谓词：返回问题标签列表（空 = 通过）。

    正向锁与反向锁共用这一份：反向锁往合成语料里注入第二个号，断言这里报得出。
    """
    problems = []
    if ui.count(VERSION_ROW) != 1:
        return ["anchor-version"]
    if ui.count(NEXT_ROW) != 1:
        return ["anchor-next"]
    ver = _row(ui)
    if OFFER_CLICK not in ver:
        return ["no-trigger"]
    persist = ver[:ver.index(OFFER_CLICK)]
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
        "独立的「检查更新」行又回来了——它删掉之后检查是静默副作用，不是一行"
    assert UPDATE_CHIP not in _row(ui), \
        "旧口径那颗常驻「检查更新」胶囊回来了（它同时是「检查中」禁用态的唯一出处）"
    assert OFFER_CHIP in _row(ui), "新口径那颗「发现新版本」按钮不在版本行里"
    assert len(_row(ui)) > 200, f"「版本」那一行的切片短得不像话：{_row(ui)!r}"


def test_the_version_row_shows_the_shell_version_alone():
    """核心那条：常驻渲染段只有壳那一个号，没有任何第二个来源参与。"""
    ui = _ui()
    problems = _version_row_problems(ui)
    assert not problems, f"常驻版本行不再是单号（问题：{problems}）"
    # shellVer 本身也只能有一个来源：出包那份 gradle 烘进 BuildConfig 的 versionName
    assert re.search(r'val shellVer = "v" \+ BuildConfig\.VERSION_NAME', ui), \
        "shellVer 不再只由 BuildConfig.VERSION_NAME 拼出来——版本号出现了第二个来源"


def test_the_second_number_has_no_source_left_on_the_settings_page():
    """整份文件里第二个号的来源一并没了：留着「拉来不用」等于白跑一次网络还留着坑。"""
    ui = _ui()
    assert "serverBuild" not in ui, "serverBuild 那份状态还在（值不许显示，来源也不该留着）"
    assert "Api.health" not in ui, "设置页还在拉 /health 的 build"
    assert "versionVal" not in ui, "两号相加那个中间变量还在，说明拼接的逻辑没走远"


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

# ---------- 新口径的状态机：静默检查 / 有新版本才露按钮 / 一点就下载 ----------


def test_the_checking_chip_is_gone():
    """B 的前半：常驻胶囊与它的「检查中」态一起退场。

    「检查中」是一帧中间态，而新口径要求这一行**一挂出来就是终态**（只有一个号，或
    版本号 + 发现新版本）。旧锁 test_the_checking_state_says_so_without_a_number 钉的是
    那颗胶囊的文案与禁用位；胶囊没有对象了，于是替换成这一条——判据从「文案对不对」
    变成「那颗胶囊（含它的检查中分支）一个字都不许留」，同样可数、同样会咬人。
    """
    ui = _ui()
    assert UPDATE_CHIP not in ui, "「正在检查更新…/检查更新」那颗常驻胶囊回来了：检查又变成用户要按的动作"
    assert "正在检查更新" not in ui, "「正在检查更新…」这句回来了：这一行会出现检查中那一帧"
    assert "startCheck" not in ui + _updater(), \
        "startCheck 这个名字还在：检查入口必须只剩静默的 Updater.offer()"
    # 正对照：扫的确实是这两份文件（写错路径时「没命中」和「守住了」长得一样）
    assert "SetValText(shellVer)" in ui and "object Updater" in _updater()


def test_the_silent_check_runs_once_per_visit_and_is_cached():
    """B 的后半：检查在进设置页时静默发生一次，且不许每次都打网络。

    三条一起钉，缺一条这个「静默」就会变成另一种噪音：
    ① 唯一的触发点是一次性副作用（LaunchedEffect(Unit) 里调 Updater.offer()），
       不是按钮、不在 SetRow 的动作槽里——否则它又变成用户要按的东西；
    ② Updater 侧有 30 分钟缓存闸（REFRESH_MS），反复进出设置不重复打网络；
    ③ 检查态不进 Compose 状态：asking 是 @Volatile 而不是 mutableStateOf，
       这样它不可能让「版本」值槽先画一帧「检查中」再跳（抖动）。
    """
    ui = _ui()
    up = _updater()
    assert ui.count("Updater.offer()") == 1, \
        f"静默检查的调用点不是恰好一处（现在 {ui.count('Updater.offer()')} 处）"
    at = ui.index("Updater.offer()")
    window = ui[max(0, at - 900):at]
    assert "LaunchedEffect(Unit)" in window, \
        "静默检查不在 LaunchedEffect(Unit) 的一次性副作用里：重进设置会再来一遍"
    block = window[window.rindex("LaunchedEffect"):]
    assert "SetRow" not in block, \
        "静默检查落进了某一行（SetRow）的动作槽：那它就是被点出来的，不是静默的"
    assert "private const val REFRESH_MS = 30L * 60L * 1000L" in up, \
        "静默检查没有 30 分钟缓存闸：每次开设置都会打一趟网络"
    assert "if (asking || now - lastAskAt < REFRESH_MS) return" in up, \
        "在途守卫与缓存闸不再一起挡在 offer() 的第一行"
    assert "var asking by mutableStateOf(false)" not in up, \
        "asking 变成了 Compose 状态：它一旦驱动重组，「版本」那一行就会先画一帧检查中"
    assert "@Volatile private var asking = false" in up, "asking 不再是纯线程标记"
    # 没新版 / 没问到 时 available 必须归 null：按钮的唯一挂载条件就是它非空
    assert "available = if (d.kind == ReleasePlan.Kind.AVAILABLE) d.version else null" in up, \
        "available 不再只在 AVAILABLE 时才有值：没新版时按钮也会露出来"


def test_no_up_to_date_banner_and_no_failure_banner_by_default():
    """用户点名要删的噪音：没有新版时不许多说一个字，检查失败也不许常驻。

    旧锁 test_the_up_to_date_note_carries_no_version 守的是「已是最新版本」不带号；
    新口径更进一步——这句根本不该出现（用户：「如果没有新版本的话，就没有检查更新
    这个按钮，就只是显示版本号」）。失败态同理：静默那一路一个字都不说，
    只有人主动点了按钮才可能看到理由，而且理由由 Updater 交回来，不是设置页自己弹。
    """
    ui = _ui()
    up = _updater()
    assert "已是最新版本" not in ui, \
        "「已是最新版本」这句回来了：没新版时用户只想看到版本号，不要这个顶部提示"
    assert "检查更新失败" not in ui, "设置页还在自己拼「检查更新失败：…」：失败态又常驻了"
    # 静默那一路：offer() 的回包只落状态，不回调、不报错
    offer = _slice(up, "fun offer()", "fun attemptDownload(")
    assert "onDone" not in offer, "静默检查带上了回调：它有了一条能弹提示的路"
    assert "decision = d" in offer and "available = if" in offer, "静默检查的回包没落进状态"
    # 人主动点按钮那一路才允许说话，且话是 Updater 给的（不是设置页的第二种文案）
    assert "Updater.attemptDownload(appCtx) { err -> err?.let { onNote(it, true) } }" in ui, \
        "点按钮之后没有把失败理由交给顶部 note：下载中途断掉会变成一声不响"


def test_the_offer_button_labels_the_number_and_starts_the_download():
    """C：号码只在那一颗带标签的按钮里；点它一步进下载，不再有第二步确认。

    旧锁 test_the_offer_dialog_labels_the_number_and_only_offers_download 守的是弹层
    标题里的「发现新版本 v版本号」；载体换成按钮之后，守的东西一样不少：
    ① 文字里带「发现新版本」标签（号码不带标签就会被读成一个常驻版本号）；
    ② 号码来自 Updater.available（全页唯一一处可安装版本的真相）；
    ③ 点击直接 attemptDownload —— 不许再出现「先检查、再点一次才下载」的两步形状；
    ④ 旧的一次性邀约弹层连同它的旗标一起退场（留着就是第二处报号的出口）。
    """
    ui = _ui()
    upd = _update_ui()
    assert OFFER_CHIP in ui, "「发现新版本」按钮不再带标签报出可安装的那一版"
    assert "if (Updater.available != null) {" in ui, \
        "按钮不再以 available 非空为挂载条件：没新版时会露出一颗置灰的空胶囊"
    # 挂载条件必须写在 if 上而不是 enabled 上：SetMini 总会画，只是置灰——
    # 用 enabled 当开关，没有新版时那一行就会多出一颗点不动的「发现新版本 」
    assert "enabled = Updater.available != null" not in ui, \
        "按钮又用 enabled 当开关了：没新版时它会以置灰形态常驻在版本行里"
    assert OFFER_CLICK in ui, "点「发现新版本」没有一步进下载"
    assert "Updater.startCheck(" not in ui and "Updater.startCheck(" not in upd, \
        "更新这条路里又出现了「先检查」那一拍：两步形状回来了"
    # 全页只有这一处报可安装的版本号
    assert "发现新版本" not in upd, "UpdateUi.kt 里又出现「发现新版本」：第二处报号出口"
    assert OLD_DIALOG not in ui and OLD_DIALOG not in upd, "旧的一次性邀约弹层还在"
    assert "offerUpdate" not in ui, "旧的待弹旗标还在：它会让弹层与按钮同时成为报号出口"
    assert "fun UpdateAvailableDialog(" not in upd, "旧邀约弹层函数没删干净"


def test_the_download_path_reports_human_reasons():
    """点下去直接就下载，所以中途失败的**那唯一一句话**必须是人话。

    旧链路里失败发生在用户明确的第二步（「立即更新」）上；新链路里它是「我点了那颗
    按钮」的唯一后果，屏幕上没有第二句解释可以兜底。这里钉：① 断网 / 服务端忙 /
    校验不过各有各的说法；② 什么都不说就 return 的静默失败路径全部消失。
    """
    up = _updater()
    assert "fun attemptDownload(app: Context, onDone: (String?) -> Unit)" in up, \
        "一步下载的入口（attemptDownload）不在了"
    for human in ("网络断了", "更新服务这边正忙", "对不上", "已经是最新版本了"):
        assert human in up, f"下载失败链路上少了这句人话：{human}"
    assert "还没问到这一版能不能装" in up, \
        "手里没有决定时静默 return 了：会留下一颗点了没反应的按钮"
    assert "friendlyDownloadError" in up, "失败理由不再经人话翻译就上屏"


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

    每一味对应一种真会发生的写法（第二值槽 / 无障碍属性 / 条件追加 / 手写号 /
    插值 / 语义位 / 富文本 / 裸 Text）。合成语料直接喂给与正向锁同一个谓词。
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
        # 插值那一味用 decision 而不是 available：available 本来就在常驻段里（它是那颗
        # 按钮的挂载条件），拿它当违例会把真源码判红。第二个号经插值上屏时，掐它的
        # 仍然是「字符串里不许有 $」+「不许读通道决定」这两条。
        ("interpolation", ui.replace('SetMini("发现新版本 " + Updater.available.orEmpty()',
                                     'SetMini("发现新版本 ${Updater.decision?.version}")', 1),
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
