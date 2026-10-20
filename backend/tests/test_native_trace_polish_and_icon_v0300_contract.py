"""v0.30.0 的三件界面事：发完自动收键盘、过程面板改成画的、桌面图标换成自己的元素。

用户 2026-10-04 的原话：
  「这个思考的UI我觉得可以改一下，图片中的这两个UI都可以进行参考。」
  「每次我发送问题之后键盘自动收起，这样我能少一个收起键盘的动作，直接看到输出结果。」
  「软件的图标也可以改一下了，因为图标太普通了，没有任何我这个软件的元素。」

读法沿用本仓库判例：本机没有 Gradle 也没有 Android SDK，所以前两条把 Kotlin 源码当文本读
（CI 那条 `build-native-apk.yml` 从今天起确实会跑 `testDebugUnitTest`，但那是推上去之后的事，
本机改一行等两分钟不是反馈回路；纯 JVM 判断核那一段由 tools/shell_jvm_tests.py 三秒出结果）。
图标那两条读 PNG 文件头与 res/ 下的 XML —— 都是"改坏了当场红"而不是"装到手机上才发现"。
"""
import re
import struct
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / \
     "assistant" / "nativeapp" / "ui"
CHAT = UI / "ChatUi.kt"
ICONS = UI / "Icons.kt"
RES = REPO_ROOT / "android-native" / "app" / "src" / "main" / "res"
BUILD = REPO_ROOT / "android-native" / "app" / "build.gradle"
RELEASE_DOC = REPO_ROOT / "docs" / "releases" / "v0.30.0.md"
ICON_TOOL = REPO_ROOT / "tools" / "render-app-icon.ps1"
WEB_ICON = REPO_ROOT / "backend" / "app" / "web" / "static" / "icon.png"
WEB_MANIFEST = REPO_ROOT / "backend" / "app" / "web" / "static" / "manifest.webmanifest"

# 密度桶 -> (桌面图标边长, 自适应前景层边长)。minSdk 24：API 24/25 只吃栅格图，
# 所以五桶 x 三张（launcher / round / foreground）一张都不能少。
DENSITIES = {"mdpi": (48, 108), "hdpi": (72, 162), "xhdpi": (96, 216),
             "xxhdpi": (144, 324), "xxxhdpi": (192, 432)}


def _text(path: Path) -> str:
    assert path.is_file(), f"契约的另一端不在仓库里：{path}"
    return path.read_text(encoding="utf-8")


def _strip_kotlin_comments(src: str) -> str:
    """去掉 // 与 /* */ 注释，且尊重字符串字面量。

    不剥注释，注释里那句「原来这里打的是 ▋」就会冒充成"代码里还有 ▋"，把一条好端端的
    判据变成永远红或者永远绿不了的真假难辨。同一把尺子在
    test_native_live_trace_follow_v0292.py 已有先例。
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in ('"', "'"):
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_xml_comments(src: str) -> str:
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


def _body(src: str, anchor: str) -> str:
    """从 anchor 起做大括号配平，截出那一块代码（把搜索面收到真分支上）。"""
    at = src.find(anchor)
    assert at >= 0, f"找不到锚点 {anchor!r}——那一处结构变了，锁得跟着改读法"
    open_at = src.index("{", at)
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at:i + 1]
    raise AssertionError(f"大括号没闭合：{anchor}")


def _png_head(path: Path):
    raw = path.read_bytes()[:26]
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} 不是 PNG"
    assert raw[12:16] == b"IHDR", f"{path.name} 没有 IHDR"
    w, h = struct.unpack(">II", raw[16:24])
    return w, h, raw[25]  # 25 = color type：6 = RGBA


# ==========================================================================
# 1. 发送即收键盘
# ==========================================================================

def test_sending_a_question_stashes_the_keyboard():
    """sendNow 收下这条消息的那一刻就得把键盘压下去（用户：少一个手动收键盘的动作）。

    用现成的 stashIme 而不是 keyboardController?.hide() 单发：第 8/9 轮真机已经证过
    这 ROM 上 show 动画会吞掉紧随的一次 hide()，单发压不住。
    """
    src = _text(CHAT)
    send = _body(src, "fun sendNow(text: String)")
    assert "stashIme()" in send, "发送路径不再收键盘了：用户每次都要多点一下键盘的收起"
    assert send.index("stashIme()") < send.index("streamInto()"), \
        "收键盘排在 streamInto() 之后：流一开就重组，键盘压不干净，得在起流之前"


def test_ime_kit_is_declared_before_send_now():
    """Kotlin 局部函数不能前向引用：stashIme 必须定义在 sendNow 之前。

    这条不是风格问题——把这套件挪回语音闸门那一段（它原来就在那），sendNow 里的
    stashIme() 直接编译不过，而 CI 是唯一会编 Kotlin 的地方。
    """
    src = _text(CHAT)
    kit = src.index("fun stashIme()")
    send = src.index("fun sendNow(text: String)")
    assert kit < send, "stashIme 又跑到 sendNow 后面了：局部函数前向引用，assembleDebug 当场红"
    assert src.index("val imeStash = remember { Handler(Looper.getMainLooper()) }") < send
    assert "imeStash.removeCallbacksAndMessages(null)" in src, "离开组合时清不掉排队中的补刀"


# ==========================================================================
# 2. 过程面板：状态标记改成画的
# ==========================================================================

# 缺字风险分两档，不搞一刀切：
#   BAN_EVERYWHERE —— ▋ ▾ ▸ 这三个几何标记，真机已经判过字体里就是没有（那条
#     "只剩一个绿方块的气泡"就是 ▋），全文件禁，一个字符串字面量都不许留。
#   BAN_IN_PANEL  —— ✓ ✗ 是 Unicode 常见符号，模型选择页那枚「当前项」勾（ChatUi.kt
#     的 Text("✓"）与日程页）被 v0.23.11 的老契约钉着，不是本轮要动的东西；这一档
#     只在本面板的三块体（光标 / 步骤行 / 面板头）里禁——那儿才是用户这次看到的界面。
BAN_EVERYWHERE = ["▋", "▾", "▸"]
BAN_IN_PANEL = BAN_EVERYWHERE + ["✓", "✗"]


def _glyph_literals(src: str, banned=BAN_EVERYWHERE):
    """只挑字符串字面量里的缺字风险字形（注释先剥掉——注释不上屏）。"""
    return [g for g in banned if re.search(r'"[^"\n]*%s[^"\n]*"' % re.escape(g), src)]


def test_the_detector_itself_works():
    """自证：上面那把尺子真的抓得到，别又是一条"检不到东西的检查看起来全绿"。"""
    assert _glyph_literals('Text("▋", fontSize = 15.sp)') == ["▋"]
    assert _glyph_literals('Text("查看 3 个步骤")') == []
    assert _glyph_literals('if (cur) Text("✓", fontSize = 18.sp)', BAN_IN_PANEL) == ["✓"]
    # 注释里提一句"原来打的是 ▋"不该被判成代码里还有 ▋——这条就是防那个。
    assert _glyph_literals(_strip_kotlin_comments('/* 原来是 ▋ */\nText("正在思考")')) == []


def test_no_status_mark_depends_on_a_font_that_may_not_have_it():
    """用户真机截图：一条空气泡里只剩一个实心绿方块——那是 ▋ 缺字渲染成的豆腐。

    面板与光标用的每一个标记（呼吸光标、展开箭头、步前 ✓/✗/进行中）一律改画。
    """
    src = _strip_kotlin_comments(_text(CHAT))
    left = _glyph_literals(src)
    assert not left, f"ChatUi.kt 又用字形当状态标记了（{left}）：换台设备就是方块"
    cursor = _body(src, "private fun BlinkCursor()")
    assert "Text(" not in cursor, "光标又回到打字形：缺字就是那块绿方块"
    assert "background(" in cursor and "Box(" in cursor, "光标改成画的竖条"
    step = _body(src, "private fun TraceStepRow")
    for icon in ("IconStepDone", "IconStepFailed", "IconStepBusy"):
        assert icon in step, f"步前标记少了画出来的 {icon}：三态里有一态又靠字形了"
    panel = _body(src, "private fun TracePanel")
    assert "IconChevronDown" in panel and "rotate(" in panel, \
        "展开箭头仍是 ▾/▸ 字形"
    for name, body in (("BlinkCursor", cursor), ("TraceStepRow", step), ("TracePanel", panel)):
        got = _glyph_literals(body, BAN_IN_PANEL)
        assert not got, f"面板里的 {name} 又拿字形当标记了（{got}）"
    assert _glyph_literals(_strip_kotlin_comments(_text(ICONS))) == [], \
        "Icons.kt 里也不许留缺字字形"


# ==========================================================================
# 3. 过程面板：看得出现在在干什么、干了多久
# ==========================================================================

def test_live_step_reports_elapsed_time_measured_on_the_client():
    """参考图那条「阅读中… 18.7s」：进行中的那一步必须自带秒数。

    服务端只有 tool_result 帧带 elapsed_ms（executor.py 量的），thinking / search
    与"还没等到结果"的工具步都没有时长——所以这一格只能客户端自己数。
    """
    src = _text(CHAT)
    bar = _body(src, "private fun TraceLiveBar")
    assert "elapsedRealtime()" in bar, "秒数不是本地表的：那是抄的一个假进度"
    assert re.search(r"delay\(100\)", bar), "不 ticks 就走不到 0.1 秒一格"
    panel = _body(src, "private fun TracePanel")
    assert "remember(clockKey)" in panel, \
        "计时起点没挂在「是哪一步」上：换步不归零，用户看到的秒数是累计的假账"
    assert "android.os.SystemClock.elapsedRealtime()" in panel
    assert "if (liveLabel != null) TraceLiveBar" in panel, \
        "落定的历史消息也挂上实时条：老消息在原地自己数秒"


def test_panel_is_there_before_the_first_frame_lands():
    """还在想、一个字都没发的那几十秒也要有东西——原来 trace 为空整块面板不挂。

    这正是用户"复杂任务我看不出它想到哪一步"里最难的那一段：什么都还没有的时候。
    """
    src = _text(CHAT)
    bubble = _body(src, "private fun StreamingBubble")
    assert re.search(r"else if \(text\.isEmpty\(\)\) TraceThinkingNow\(\)", bubble), \
        "空 trace 且正文还没开始时空着：用户只剩一条光标"
    assert 'TracePanel(m.trace, m.traceExpanded, null' in src, \
        "历史气泡传了非空的 liveLabel：落定的回答下面会永远挂着一条进行中的状态条"


def test_omitted_and_panel_names_survive_the_redesign():
    """改版不许把别的锁读法拆掉：函数名与那句省略标记是 test_process_trace_contract 的锚。"""
    src = _text(CHAT)
    for anchor in ("private fun TracePanel", "private fun TraceStepRow",
                   "private fun safeTraceUrl"):
        assert anchor in src, f"{anchor} 被改名：另一把锁的锚点找不到了"
    assert re.search(r'"…中间 \$\{t\.omitted\} 步已省略…"', src), \
        "双端同文案的省略标记被改：原生这侧不再报被裁掉多少步"


# ==========================================================================
# 4. 桌面图标
# ==========================================================================

def test_launcher_art_is_present_at_every_density():
    """minSdk 24：API 24/25 的桌面只认栅格图，五桶一张不许多一张也不能少。"""
    for bucket, (launcher, foreground) in DENSITIES.items():
        d = RES / f"mipmap-{bucket}"
        for name, side in (("ic_launcher.png", launcher),
                           ("ic_launcher_round.png", launcher),
                           ("ic_launcher_foreground.png", foreground)):
            p = d / name
            assert p.is_file(), f"{p.relative_to(REPO_ROOT)} 不在了"
            w, h, color_type = _png_head(p)
            assert (w, h) == (side, side), f"{name} 是 {w}x{h}，应是 {side}x{side}"
            assert color_type == 6, f"{name} 不是 RGBA：透明层会带一层黑底"


def test_adaptive_icon_background_is_the_gradient_not_a_stale_color():
    """底色从 @color 换成 @drawable 的渐变；旧的那格色值必须一起删掉。

    留着 #7030E0 那一格就是留一份"对不上的真相"：下次谁照它改图标就改错了地方。
    """
    for name in ("ic_launcher.xml", "ic_launcher_round.xml"):
        xml = _strip_xml_comments(_text(RES / "mipmap-anydpi-v26" / name))
        assert "@drawable/ic_launcher_background" in xml, f"{name} 还指着纯色那格"
        assert "@color/ic_launcher_background" not in xml
        assert "@mipmap/ic_launcher_foreground" in xml
    assert (RES / "drawable" / "ic_launcher_background.xml").is_file()
    # 剥注释再查：colors.xml 里留着的那段说明文字正好提到这个格子名，
    # 不剥就是一句"解释"把自己判成"还留着定义"。
    colors = _strip_xml_comments(_text(RES / "values" / "colors.xml"))
    assert "ic_launcher_background" not in colors, \
        "colors.xml 里那格纯色还留着：两份底色，改一份就漂"


def test_icons_are_generated_from_a_script_not_hand_edited():
    """整套图标出自 tools/render-app-icon.ps1：换配色重跑一条命令，五桶不会各画一套。"""
    assert ICON_TOOL.is_file()
    raw = ICON_TOOL.read_bytes()
    assert all(b < 0x80 for b in raw), \
        "render-app-icon.ps1 混进非 ASCII：Windows PowerShell 5.1 读无 BOM 的脚本会乱码"
    tool = raw.decode("ascii")
    # 脚本里必须真的画的是同一套东西（气泡 + 思考轨迹 + 火花），不是随手贴一张图
    assert "Get-SparkPath" in tool and "Add-RoundedRect" in tool
    assert "#0E7C86" in tool and "#7030E0" in tool, "渐变两端换了色，图标与主题就分家了"


def test_web_icon_and_manifest_still_agree():
    """PWA 那一份也得跟着换：manifest 写 1024x1024，文件就得是 1024x1024。"""
    w, h, _ = _png_head(WEB_ICON)
    assert (w, h) == (1024, 1024), f"static/icon.png 是 {w}x{h}"
    manifest = _text(WEB_MANIFEST)
    assert '"1024x1024"' in manifest.replace(" ", "") or "1024x1024" in manifest
    assert "Fenver" in manifest, "名字被图标改版顺手改掉：test_brand_surface 那把锁认的就是这个"
    assert (RES / "drawable-nodpi" / "app_icon.png").is_file(), "App 内品牌位没了"


def test_version_bumped_and_release_notes_brief():
    gradle = _text(BUILD)
    # 版本号只有一份真相（gradle），所以每一轮的契约文件都跟着钉**当前**那一格：
    # v0.28.0 推到 48，v0.28.1 推到 49，v0.28.2 推到 50，v0.28.3 推到 51，v0.29.0 推到 52，
    # v0.29.1 推到 53，v0.29.2 推到 54，v0.30.0 推到 55，v0.31.0 推到 56，v0.32.0 推到 57，v0.33.0 推到 58，v0.33.1 推到 59，v0.34.0 推到 60、v0.34.1 只换服务端没推、v0.34.2 推到 61、v0.35.0 推到 62。下面断的是现在那一格。
    assert "versionCode 62" in gradle and 'versionName "0.35.0"' in gradle, \
        "gradle 那一格漂了：这一版应是 62 / 0.35.0，且必须与 docs/releases/v0.35.0.md 同时存在"
    doc = _text(RELEASE_DOC)
    allowed = {"修复了什么", "优化了什么", "新增了什么"}
    heads = re.findall(r"^##\s*(.+?)\s*$", doc, flags=re.M)
    assert heads and set(heads) <= allowed, \
        "更新日志三段式（第 12 轮钦定延续）：没有的不写，解释性段落禁止"
    assert not re.search(r"怎么修|根因|教训|改判|逆向", doc), "简版口径：只报结果，不展开过程"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
