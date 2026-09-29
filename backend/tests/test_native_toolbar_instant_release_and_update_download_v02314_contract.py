"""第 15 轮真机反馈的四地契约（v0.23.14）：原生菜单 + 收键盘即回胶囊 + 松手秒发 + 更新下载。

用户原话（附三图）：「这个粘贴为什么不是手机系统原生的？还有就是为什么一旦出现
不管点击什么地方都不会消失？……我要求当键盘收起来之后输入框就立即更换成发消息
和按住说话的样式。……当我松开手指之后软件不能立即响应，我希望立刻响应我松开手指
的交互……我点击检查更新，这个下载新版本的界面一直下载失败超时，找一下问题所在，
并且下载新版本之后，新版本的安装包要直接自动删除掉。」

钉五件事：
① 长按菜单换系统 PopupMenu 壳（PopupTextToolbar 实现 TextToolbar，接口形状按
   BOM 2024.06.00 / Compose 1.6.8 的 showMenu(rect, 四可空回调) 钉死；动作仍走
   文本框原回调，只换壳不改行为），经 LocalTextToolbar 只罩聊天输入框与消息编辑
   框两处：原生长相、点外面/选一项自动消失；
② 键盘信号改判：第 14 轮的焦点代理整个撤下（收键盘不收焦点→信号卡死），换
   窗口绝对高度缩水（adjustResize 物理事实）：maxH 基线 + 1/5 阈值 + 宽度变化
   重置基线；
③ 松手秒收尾：finish(send=true) 当场消费 pending ?: heard 并关闭会话，
   SETTLE_MS 等包闹钟整个退役；迟到终包由 onResults 的 !listening 守卫作废；
   60s 硬顶 watchdog 保留；按住期间引擎提前回包仍只进 pending（第 13 轮铁律）；
④ 更新下载：APK 连接 15s/读 60s 超时单列一档（服务端替包跑 GitHub 首字节 ~8s，
   12 秒必死）；不跟 302（跳 github.com 在国内网络挂死在 0 KB），3xx 给人话；
⑤ 装完自动清包：冷启动扫私有下载目录，只删版本 ≤ 当前已装版本的
   ai-assistant-native-*.apk(.part)——比当前新的包是用户正在装的那次升级，不许动。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"
RELEASE_DOC = REPO_ROOT / "docs" / "releases" / "v0.23.14.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")
UPD = _read(UI / "update" / "Updater.kt")
MAIN = _read(UI / "MainActivity.kt")


def test_native_popup_toolbar_at_both_text_fields():
    # 第 15 轮：Compose 自绘气泡（不原生、点不关）换系统 PopupMenu 壳；
    # 第 17 轮再改判（用户：「粘贴的UI不好看，希望小一点并且位置跟着光标」）：
    # PopupMenu 竖排大列表也退场，换光标锚定的紧凑胶囊 PopupWindow。
    # 本轮钉的是不变量——只换壳不改行为：接口形状、四动作、两处挂载。
    assert "import android.widget.PopupMenu" not in CHAT and "PopupMenu(" not in CHAT, \
        "第 17 轮：v02314 的大列表壳被用户否了，import 与调用全退场（胶囊壳细节钉在 v02316 契约）"
    chip = re.search(r"class PopupTextToolbar\([\s\S]*?\n\}", CHAT)
    body = chip.group(0)
    assert "PopupWindow" in body, "壳仍是自己实现的浮层，不再借系统列表菜单"
    # 接口形状按 BOM 2024.06.00（Compose 1.6.8）钉死：showMenu(rect, 四回调)——
    # CI 红过一次的风险：新版 SDK 换成 textActions/showToolbar，照新名写编译不过
    assert re.search(r"override fun showMenu\(rect: Rect, onCopyRequested", body), \
        "1.6.8 的 TextToolbar 形状：showMenu 收四个可空回调"
    assert "override val status: TextToolbarStatus" in body, \
        "1.6.8 接口要求的 status 成员不许漏"
    assert re.search(r"override fun hide\(\) \{\s*\n\s*popup\?\.dismiss\(\)", body), \
        "点外面/失焦自动收：hide 必须真 dismiss（用户：『点哪都不消失』的本体）"
    for act in ("剪切", "复制", "粘贴", "全选"):
        assert act in body, f"菜单项 {act} 照回调供给，一个都不缺"
    assert CHAT.count("LocalTextToolbar provides") == 2, \
        "聊天输入框 + 消息编辑框两处都罩上自绘壳，别处文本域不受牵连"
    assert "NoopTextToolbar" not in CHAT and "TapAwareTextToolbar" not in CHAT, \
        "历史上的空壳/魔改壳都不许借尸"


def test_keyboard_up_is_window_height_shrink():
    # 「键盘收起来之后输入框立即换回胶囊样式」：焦点信号撤下，换窗高缩水
    assert "fieldFocused" not in CHAT and "onFieldFocus" not in CHAT, \
        "收键盘不收焦点——第 14 轮焦点代理天生测不到『键盘已收』，不许复活"
    assert re.search(r"val keyboardUp = WindowInsets\.ime\.getBottom\(density\) > 0"
                     r" \|\| frameKeyboardUp", CHAT), \
        "keyboardUp = ime 内衬 || rememberKeyboardVisible"
    assert re.search(r"var maxH = 0", VOICE) and re.search(r"else if \(h > maxH\) maxH = h", VOICE), \
        "历史最高窗高基线：adjustResize 键盘起窗被顶扁"
    assert re.search(r"maxH - h > maxH / 5", VOICE), "现高矮过基线 1/5 判键盘在"
    assert re.search(r"if \(w != lastW\) \{ lastW = w; maxH = h \}", VOICE), \
        "宽度变化（旋转/分屏）重立基线，别把换窗当起键盘"
    assert re.search(r"\(loc\[1\] \+ root\.height\) - frame\.bottom > root\.height / 5 \|\|", VOICE), \
        "第 16 轮翻正减法方向：全屏窗 + IME 悬浮下窗底−可见区底=键盘高；" \
        "旧式 frame.bottom−窗底 在这台 ROM 恒为负，键盘开着也判不出（真机铁证）"


def test_release_closes_the_session_on_the_spot():
    # 「松开手指之后要立刻响应」：等包闹钟退役，finish 一步做完
    assert "SETTLE_MS" not in VOICE and "clearSettle" not in VOICE and \
        "settleAlarm" not in VOICE, \
        "等包闹钟是『松手卡 1.6 秒』的本体，整个拆干净不许留残骸"
    send = re.search(r"if \(send\) \{[\s\S]*?\n        \} else", VOICE)
    assert send, "finish 的 send 分支必须在"
    s = send.group(0)
    assert re.search(r"val t = \(pending\?\.takeIf \{ it\.isNotBlank\(\) \} \?: heard\)\.orEmpty\(\)", s), \
        "手到擒来：pending（按住期间已回终包）优先，其次最后半截识别字"
    assert re.search(r"listening = false[\s\S]*?rec\?\.cancel\(\)[\s\S]*?onFinal\?\.invoke\(t\)", s), \
        "先关状态、掐引擎，再把话发出去——全在松手这一刻，不挂任何闹钟"
    assert re.search(r"override fun onResults\(results: Bundle\?\) \{[\s\S]{0,200}if \(!listening\) return", VOICE), \
        "迟到终包作废守卫：收尾后引擎再回不许二次发送"
    # 第 13 轮铁律不回退：按住期间提前回包仍只进 pending；硬顶 watchdog 仍值守未知
    assert re.search(r"if \(holding\) \{[\s\S]{0,200}pending = t\n\s*return", VOICE), \
        "还没松手就来的终包照旧暂存——松手发送是铁律"
    assert "const val MAX_SESSION_MS = 60_000L" in VOICE and \
        "mainHandler.postDelayed(wd, MAX_SESSION_MS)" in VOICE and \
        re.search(r"fun finish\(send: Boolean\) \{[\s\S]{0,200}clearWatchdog\(\)", VOICE), \
        "松手撤硬顶、事件全丢硬顶收场——双保险只剩这一对，都在位"


def test_update_download_timeout_and_redirect_traps_fixed():
    # 「下载新版本一直失败超时」两根因：12 秒读超时卡死服务端替包（首字节 ~8s）；
    # 默认跟 302 跳到手机够不着的 github.com 挂死在 0 KB
    assert "APK_CONNECT_MS = 15_000" in UPD and "APK_READ_MS = 60_000" in UPD, \
        "APK 超时单列一档，不再蹭信息接口的 8s/12s"
    apk = re.search(r"fun openApkConnection\(url: String\)[\s\S]*?\n        \}", UPD)
    body = apk.group(0)
    assert "connectTimeout = APK_CONNECT_MS" in body and "readTimeout = APK_READ_MS" in body, \
        "新档只上 APK 这条路；信息接口那路不许被顺带动到"
    assert "instanceFollowRedirects = false" in body, \
        "不跟 302：跳 github.com 在国内网络就是 0 KB 挂死"
    assert UPD.count("instanceFollowRedirects = true") == 1, \
        "全文只剩 fetchInfo 一处跟跳（/v1/update/info 是本域小 JSON，无此坑）"
    assert re.search(r'e\.contains\("HTTP 3"\)\) "更新服务这会儿取不到安装包', UPD), \
        "服务端丢包（单飞槽被占/上游抽风）回 302：给人话，别甩『服务回 HTTP 302』"


def test_stale_apk_swept_on_boot_with_version_gate():
    # 「新版本的安装包要直接自动删除掉」+ 不拆正在装的那次升级
    assert re.search(r"Prefs\.init\(applicationContext\)[\s\S]{0,300}sweepStaleApks\(applicationContext\)", MAIN), \
        "冷启动就扫包，不等用户手动清"
    assert "ReleasePlan.APK_PREFIX" in MAIN and \
        re.search(r'\.startsWith\(ReleasePlan\.APK_PREFIX\)', MAIN), \
        "只碰自家下载目录的包，别人家的文件一个字节都不许动"
    assert re.search(r"ReleasePlan\.compare\(v, BuildConfig\.VERSION_NAME\) <= 0\) f\.delete\(\)", MAIN), \
        "版本闸：只删已装完（≤ 当前版本）的旧账；比当前新的包是路上/待装的升级，留下"
    assert '".apk.part"' in MAIN or ".apk.part" in MAIN, \
        "半截 .part 同闸清掉，不留磁盘残渣"


def test_earlier_rounds_do_not_regress():
    assert re.search(r'if \(keyboardUp\) "发消息…" else "发消息或按住说话…"', CHAT), \
        "第 13 轮占位符判据：键盘起着只写『发消息…』"
    assert re.search(r"VoiceScreen\(voice\.heard, voice\.level, voiceCancel,\s*"
                     r"Modifier\.align\(Alignment\.BottomCenter\)", CHAT), \
        "第 13 轮贴底卡片不回退"
    assert re.search(r"if \(!expanded\) \{[\s\S]{0,220}if \(chipVisible\) ModelChip", CHAT), \
        "第 13 轮收起态模型钮在位"
    assert re.search(r"ModalNavigationDrawer\(\s*\n\s*drawerState = drawerState,\s*\n"
                     r"[\s\S]{0,400}gesturesEnabled = !voice\.listening", CHAT), \
        "第 14 轮录音中锁抽屉不回退"
    assert re.search(r"val voiceArmed = input\.text\.isEmpty\(\) && !busy", CHAT) and \
        "!fieldPending" in CHAT, "第 10 轮闸门在位不挂框（零键盘闪）"
    assert "fun stashIme()" in CHAT and "for (i in 1..4)" in CHAT, "第 8 轮键盘压制双保险在位"
    assert "换个语音引擎" in CHAT and "ENGINE_DEAD_CODES" in VOICE, "第 6 轮换引擎通路在位"


def test_release_notes_brief_format():
    doc = _read(RELEASE_DOC)
    allowed = {"修复了什么", "优化了什么", "新增了什么"}
    heads = re.findall(r"^##\s*(.+?)\s*$", doc, flags=re.M)
    assert heads and set(heads) <= allowed, \
        "更新日志三段式（第 12 轮钦定延续）：没有的不写，解释性段落禁止"
    assert not re.search(r"怎么修|根因|教训", doc), "简版口径：只报结果，不展开过程"
