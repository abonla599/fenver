"""上线那个安卓模块（android-native）的源码形状锁。

2026-10-08 WebView 壳（android/）退役。壳退役不该把纪律一起退役：这些锁从前读的是
壳的 Java 源码，而它们钉的那些坑（Android 14 的注册接收器导出标志、没授权那一支也
要记账并推进排期、ParcelFileDescriptor 在 android.os 不在 android.content、手写
provider 的 authority 三处必须一致、起安装页与那条例外权限同批存在、不点就不会自己
查更新）在原生模块里**一处不少地都还在**——Reminders.kt 就是从壳的 ReminderReceiver
逐段搬过来的，ApkFileProvider.kt 与 Updater.kt 同一条边界。所以这里把判据重挂到
真正出包那一个模块上，读法沿用 `_code`（剥注释的尺子只有一把，住在
test_apk_release_discipline.py）。

一件跟着壳一起退役、原生没有对应物的事，写下来免得下一个人以为它还在被钉着：
长按图标的快捷方式与桌面组件（shortcuts.xml / widget_assistant.xml / AssistantWidget）
是壳独有的形态，原生模块既没有那张 XML 也没有那个组件类，因此"三处列同一批入口"
那条锁没有可挂之处，随壳删除。桥（ShellBridge）同理，见本文件末那条冻结快照。
WebView 的 onPermissionRequest 白名单（"网页要什么就批什么"那格）也一样：原生不嵌
WebView，录音走运行时 RECORD_AUDIO 权限，没有那一格可守（原判据在
test_medium2_batch_20260923.py，原地留了指针）。

还有一件**不是退役而是缺口**，如实记在这里：壳的 PermissionStatus.notificationsGranted
同时问运行时权限与通知总开关（areNotificationsEnabled），原生
Reminders.kt 的 notificationsGranted 只问 checkSelfPermission。总开关被关掉时，
原生会照旧 notify()（被系统吞掉）并记 markFired，屏幕上那句"上次发出 xx:xx"因此
谎报。这条锁今天没有挂上来——挂上就是红的，而它属于产品行为改动，需要发版后再走
真机验收。留作 v0.35 的第一条待办，不要让它因为这段注释而消失。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NATIVE_MAIN = REPO_ROOT / "android-native" / "app" / "src" / "main"
NATIVE_SRC = NATIVE_MAIN / "java"
MANIFEST = NATIVE_MAIN / "AndroidManifest.xml"
KT = sorted(NATIVE_SRC.rglob("*.kt"))
JAVA = sorted(NATIVE_SRC.rglob("*.java"))
WEB_STATIC = REPO_ROOT / "backend" / "app" / "web" / "static"

from tests.test_apk_release_discipline import _code          # noqa: E402

NATIVE_PKG = NATIVE_SRC / "xyz" / "fenever" / "assistant" / "nativeapp"
REMINDERS_KT = NATIVE_PKG / "Reminders.kt"
UPDATER_KT = NATIVE_PKG / "update" / "Updater.kt"
PROVIDER_KT = NATIVE_PKG / "update" / "ApkFileProvider.kt"
SETTINGS_UI_KT = NATIVE_PKG / "ui" / "SettingsUi.kt"

# Android 14（targetSdk 34）起，Context.registerReceiver 只有在过滤器【全部】命中
# AOSP IntentFilter.SYSTEM_ONLY_ACTIONS 那张硬编码表时才能省掉导出标志，否则直接抛
# SecurityException。android.intent.action.DOWNLOAD_COMPLETE 不在那张表里。
EXPORT_FLAGS = ("RECEIVER_EXPORTED", "RECEIVER_NOT_EXPORTED")


def _call_args(src: str, open_paren: int) -> str:
    """从左括号起取到配对的右括号，跨行也算。"""
    depth = 0
    for i in range(open_paren, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[open_paren + 1:i]
    return src[open_paren:]


def test_no_runtime_receiver_registration_without_an_export_flag():
    """v0.13 崩在壳的 MainActivity.onCreate 里的原因：registerReceiver 少给一个标志。

    那条注释当时写着"属系统广播，因此不必传 RECEIVER_EXPORTED"——这个豁免不成立，
    于是 Android 14+ 的设备上点图标闪一下回桌面，而服务端一条请求都收不到。原生模块
    今天靠清单静态声明接收器（`<receiver … android:exported="false"/>`），一个运行时
    注册点都没有，所以这条锁现在守的是【别新增】：哪天有人为了监听下载完成写一句
    `registerReceiver(...)`，判据当场生效。

    带标志的重载是 API 33 才有的，minSdk 24 必须按版本分岔，所以"没带标志"本身不是罪：
    只有当它**同时**不在 SDK_INT 判据的保护下才红。这是源码形状的近似（往回看 12 行），
    不是精确的控制流分析——它守的是"别再靠注释宣称豁免"，不是编译器。

    正对照：路径写错时 rglob 一个文件都找不到，"没有违规"和"守住了"长得一模一样，
    所以这里必须钉扫到了多少个源文件。原生模块的注册点为零，钉的是文件数不是命中数。
    """
    assert len(KT) >= 20, f"只扫到 {len(KT)} 个原生 Kotlin 文件，这条锁多半在空转"
    offenders = []
    for path in KT + JAVA:
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"(?<!un)\bregisterReceiver\s*\(", src):
            args = _call_args(src, m.end() - 1)
            if any(flag in args for flag in EXPORT_FLAGS):
                continue
            line = src[:m.start()].count("\n") + 1
            guarded = "SDK_INT" in "\n".join(src.splitlines()[max(0, line - 13):line - 1])
            if not guarded:
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line}")

    assert not offenders, (
        "这些 registerReceiver 既没给导出标志、也不在 SDK_INT 保护下，"
        "Android 14+ 上会抛 SecurityException：" + "、".join(offenders))


def test_the_reminder_receiver_books_both_branches_and_always_advances():
    """到点这一支必须留下证据、且两支都照常推进排期：发出去了记 firedAt，发不出去记 missed。

    以前通知没授权时直接 return、一笔不记，于是"设过的提醒从来没响过"在屏幕上读不出来。
    更坏的那一支是只记 missed 就 return，不 advance 也不重排——看着像"既然没响成就不该
    改用户的排期"，实际是把一次性的闹钟消费掉之后再也不排第二轮：daily/weekly 从此变成
    列表里看得见、重启不管、授权恢复也不会再响的孤儿。现在两支共用同一套骨架，唯一区别
    就是有没有 notify()。

    这条从壳重挂到原生：判据是【两支的形状】——granted 支有 notify 没 markMissed，
    else 支有 markMissed 没 notify，而 markFired/markMissed 之外的 advance 与重排在
    两支之外只出现一次。把 else 支改成"记一笔就 return"，或者把 advance 挪进 granted
    支，都会当场红。
    """
    src = _code(REMINDERS_KT)
    at = src.index("val granted")
    tail = src[at:]
    end = tail.index("\n    }", tail.index("for (r in due)"))
    loop = tail[:end]

    branch = re.search(r"if \(granted\) \{(.*?)\} else \{(.*?)\}", loop, re.S)
    assert branch, "两支的骨架不在了：提醒的记账形状需要从 if (granted) / else 里读"
    fired, missed = branch.group(1), branch.group(2)
    assert "notify(manager" in fired and "markFired" in fired, \
        f"授权那一支不发通知或不记账：{fired}"
    assert "markMissed" in missed and "notify(manager" not in missed, \
        f"没授权那一支不发出去却不记 missed，或者反过来真发了：{missed}"
    # 两支之外：推进与重排各一次，位置在 if/else 之后。挪进任何一支都会让另一支变孤儿。
    after = loop[branch.end():]
    assert after.count("ReminderStore.advance(r, now)") == 1, "advance 不再对两支共用"
    assert "ReminderScheduler.schedule(context, next)" in after, \
        "重排不在共用段里：daily/weekly 会重新变成孤儿轮次"
    assert "byId(r.id)" in after, "推进后没有回查 once 是否已被删：重排会对不存在的记录开火"


def test_the_apk_provider_imports_parcelfiledescriptor_from_the_real_package():
    """ParcelFileDescriptor 在 android.os，不在 android.content——壳第一次签发 v0.19 就死在这。

    本机没有 Android SDK，这类"类名对、包名错"的 import 只有 CI 编译时才炸；而炸的位置
    恰好是全链路唯一没有 JVM 台架覆盖的一环（provider 依赖平台类，纯 Java 测试跑不了）。
    原生模块的 provider 是同一套边界，所以这条锁跟着搬过来：错包名一旦出现，develop
    推送即红，不用等打 tag 才发现。
    """
    provider = PROVIDER_KT.read_text(encoding="utf-8")
    # Kotlin 的 import 不带分号——判据按原生那一侧的真实形状写，不是把 Java 的写法搬过来。
    assert "import android.os.ParcelFileDescriptor" in provider, \
        "ParcelFileDescriptor 的 import 包名又漂了：它在 android.os"
    assert "import android.content.ParcelFileDescriptor" not in provider, \
        "android.content 下没有这个类，编译必炸（壳 v0.19 首发实测）"


def test_the_apk_provider_registration_and_the_installer_share_one_authority():
    """手写 FileProvider 的 authority 是三处拼出来的（manifest、Kotlin 常量、Intent），必须一致。

    对不上的表现不是崩溃，是安装页拿到一个解析不了的 URI 后一句"找不到文件"——
    用户视角与残包事故几乎同一个症状，查起来却要先怀疑下载。
    """
    manifest = MANIFEST.read_text(encoding="utf-8")
    assert 'android:authorities="${applicationId}.apkprovider"' in manifest, \
        "manifest 里的 provider authority 形状变了，Kotlin 侧那份要跟着改"
    assert 'android:name=".update.ApkFileProvider"' in manifest
    assert 'android:exported="false"' in manifest and "grantUriPermissions" in manifest

    provider = _code(PROVIDER_KT)
    assert 'AUTHORITY_SUFFIX = ".apkprovider"' in provider, \
        "Kotlin 侧那份后缀与 manifest 漂了"
    updater = _code(UPDATER_KT)
    assert "ApkFileProvider.uriForFile" in updater, "安装页不再通过手写 provider 拿 URI"


def test_install_intent_and_its_permission_arrive_together():
    """起安装页与 REQUEST_INSTALL_PACKAGES 必须同批存在。

    只改一边各有两种坏法：有 Intent 没权限 → 点了下载永远装不上（还会被 catch 成一句
    "去设置"，看起来像 ROM 的问题）；有权限没 Intent → 白要一个吓人的授权，
    而"这个应用想装别的软件"是会被用户记住的那一类声明。
    """
    updater = _code(UPDATER_KT)
    provider = _code(PROVIDER_KT)
    manifest = MANIFEST.read_text(encoding="utf-8")
    mime = re.search(r'APK_MIME = "([^"]+)"', provider)
    assert mime, "读不到 provider 里那个 MIME 常量：形状改了要两边一起看"
    fires = "Intent(Intent.ACTION_VIEW)" in updater and mime.group(1) == \
        "application/vnd.android.package-archive"
    declared = "REQUEST_INSTALL_PACKAGES" in manifest
    assert fires and declared, (
        "安装链路断了一头：" + ("清单没声明 REQUEST_INSTALL_PACKAGES" if not declared
                                else "Updater 里没有起安装页的那一句，或那个 MIME 不再是安装包"))


def test_only_a_user_action_can_start_an_update_check():
    """「不点检查更新就不会替你下载/安装」这句承诺在原生侧的下半段：那条链没有自动触发。

    判据是"可数"而不是"看着像"：全模块的 `Updater.startCheck(` 调用点恰好一处，且那一处
    在设置行（SetRow）的动作槽里——那是人点出来的。今天多一处"顺手在 LaunchedEffect 里
    查一下"，这句话就变成谎，而且没人会报错：GitHub 那个接口按来源 IP 限 60 次/小时，
    自动化的第一个代价是把配额自己烧光；而这一条与网页那张底部卡片不冲突——卡片每天
    自动问一次的是**服务端**只读端点，下载与安装仍然必须有人点。

    正对照同样钉死：`Updater.checking` 在原生里出现多次，说明扫到了文件；调用点数为 0
    意味着这条链整个消失（那也该红，因为发版后就没人能点）。
    """
    hits = []
    for path in KT:
        body = _code(path)
        for m in re.finditer(r"Updater\.startCheck\s*\(", body):
            hits.append(path)
    assert len(hits) == 1, (
        f"Updater.startCheck 的调用点不是恰好一处（现在 {len(hits)} 处："
        f"{[p.name for p in hits]}）——多出来的那一处就是「不点也会检查更新」")
    row = _code(SETTINGS_UI_KT)
    at = row.index("Updater.startCheck(")
    window = row[max(0, row.rindex("\n", 0, at - 1200)):at]
    assert "SetRow" in window or "return@SetRow" in window, \
        "唯一的调用点不在设置行的动作槽里：那它跟着界面一起自己跑"

    # 探测是【页面】的事，不是客户端的事。原生一旦自己去问 /v1/release/latest，网页那把
    # 「一天一次」的 localStorage 闸就管不到它了，而它烧的是服务端替所有人挡在前的配额。
    leakers = [p.name for p in KT + JAVA if "release/latest" in _code(p)]
    assert not leakers, f"原生引用了页面那张卡片的探测地址：出发的位置从页面挪到了客户端：{leakers}"


# ---------- 出包那条 CI 必须真的跑这批迁移过来的单测 ----------


def test_the_pipeline_that_ships_the_apk_also_runs_the_migrated_unit_tests():
    """单测迁到 android-native 之后，判据必须跟着落到**出包那条流水线**上。

    2026-10-08 壳退役时把 `android-tests.yml` 一起删了。如果只删不补，core/ 那七个类的
    单测就变成"本机台架跑、CI 不跑"——本机没有 Gradle 的人（包括 CI 自己）永远不知道
    `testDebugUnitTest` 在带 Android SDK 的环境里是不是照样绿，而发版顺序里那句"CI 全绿"
    不再包含任何安卓单测。所以这里钉三件：那一步存在、跑的是 `testDebugUnitTest`、
    并且排在 assemble 之前（红了就不该再花时间出一个不该存在的包）。

    正对照：整份 YAML 解析出来判 steps 非空、且模块名与 assemble 那一步**同一个**——
    两步指不同模块时，"测试绿了"和"包出来了"说的不是同一件事。
    """
    import yaml

    wf = REPO_ROOT / ".github" / "workflows" / "build-native-apk.yml"
    assert wf.is_file(), "原生出包流水线不在了：这条锁与发版顺序一起失效"
    doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
    steps = [s for job in doc["jobs"].values() for s in job["steps"]]
    assert steps, "流水线里一个步骤都没有：判据在空转"

    def run_of(frag):
        return [i for i, s in enumerate(steps) if frag in (s.get("run") or "")]

    tested = run_of("testDebugUnitTest")
    assert len(tested) == 1, f"跑安卓单测的步骤应当恰好一步（现在 {len(tested)} 步）：" \
                             "多一步通常意味着有人把它抄进了别的 job，而 job 之间的 SDK 不一样"
    assembled = run_of("assembleDebug") + run_of("assembleRelease")
    assert assembled, "流水线里没有 assemble 步骤：那它不出包，这条锁要看的是别的文件"
    assert tested[0] < min(assembled), "单测排在出包之后：包已经发出去了才发现测试红"
    test_mod = re.search(r"gradle\s+-p\s+(\S+)", steps[tested[0]]["run"])
    asm_mod = re.search(r"gradle\s+-p\s+(\S+)", steps[min(assembled)]["run"])
    assert test_mod and asm_mod, "那两步不再用 `gradle -p <模块>` 的写法：判据要跟着换读法"
    assert test_mod.group(1) == asm_mod.group(1), (
        f"测试跑的是 {test_mod.group(1)}，出包出的是 {asm_mod.group(1)}：两句『全绿』说的不是同一份代码")


# ---------- 随壳退役的那张桥：冻结成快照，不再有两份真相 ----------
# 最后一个发布版本（v0.23 系）的壳在 @JavascriptInterface 上暴露的十个方法。android/ 的
# 源码删掉之后，这批名字就是**已经装在用户手机上的那个 APK** 的接口——它不会再变，而
# shell.js 仍然要服务这些旧客户端。所以这里的判据从"两边比集合"变成"页面只许调这十个"：
# 加一个名字（旧壳没有，点了没反应）或改一个名字（解析不到 → 无桥降级）都会当场红。
SHELL_BRIDGE_METHODS = frozenset({
    "capabilities", "checkUpdate", "setOwner", "scheduleReminder", "cancelReminder",
    "listReminders", "openSettings", "pendingShares", "readShareChunk", "consumeShare",
})

# 同一批里旧壳 capabilities() 会回的那五个键。app.js 只按这些键决定那一行的画法：
# 页面新读一个键不会崩，只会永远取到 undefined，于是所有旧壳上的那一行静默退化。
SHELL_CAPABILITY_KEYS = frozenset({"shell", "update", "version", "notifications", "exactAlarms"})


def test_the_page_only_uses_the_bridge_surface_the_shipped_shell_has():
    """页面调的桥方法与读的能力键，必须都在已发布壳提供的那一份里。

    这条替代了原来"读壳的 Java 源码比集合"的锁：壳源码删掉后，比对的另一半不再是仓库里
    的东西，而是用户手机里的 APK。快照因此成为唯一可钉的形状，而它守的事故和原来同一件
    ——改名、漏接、页面多调一个壳根本没提供的方法（点了没反应，或整行静默降级）。
    """
    from tests.test_web_pwa import _js      # JS 那把剥注释的尺子只有一份，不在这里抄第二遍

    called = set(re.findall(r"\b(?:raw|call|rows)\(\s*\"(\w+)\"", _js("shell.js")))
    assert called, "shell.js 里一个桥调用都扫不到：正则失效了，这条锁在空转"
    assert called == SHELL_BRIDGE_METHODS, (
        f"页面调了旧壳没有的方法：{sorted(called - SHELL_BRIDGE_METHODS)}；"
        f"旧壳有而页面不再接：{sorted(SHELL_BRIDGE_METHODS - called)}")

    keys = set(re.findall(r"\bcaps\.(\w+)", _js("app.js")))
    assert keys, "app.js 里读不到任何能力键：这一页不再看壳的 capabilities() 了？"
    unknown = keys - SHELL_CAPABILITY_KEYS
    assert not unknown, (
        f"页面开始读旧壳不会回的能力键：{sorted(unknown)}——旧壳上那一行会静默退化")
