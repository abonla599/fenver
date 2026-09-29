"""第 9 轮真机反馈的收口契约（v0.23.8，2026-09-28）。

用户原话三件：①「这个粘贴我说过我不要」——粘贴气泡在 v0.23.7 的按时长把关下
依旧冒出来，本轮改判：输入框彻底无菜单（作用域空壳）；②「为什么键盘和语音
同时出现」——检测公式在 adjustResize 下从不扳机 + 单发 hide() 输给键盘 show
动画，两处一起修；③「刚进软件长按输入框为什么键盘会出来」——文本框按下即
抢焦点，判为长按后必须把键盘连发补刀收回去。

钉五件事：
① NoopTextToolbar 真空壳：showMenu 不放行、回调一个都不许 invoke；且
   `LocalTextToolbar provides NoopTextToolbar` 全文只出现一次（只包输入框，
   第 4 轮整屏罩的教训）；TapAwareTextToolbar/PressLedger 不许复活；
② 键盘检测换屏幕坐标差：frame.bottom 与根视图屏幕底边的距离超 1/5 屏判键盘在；
   旧式 root.height - frame.height() 在 adjustResize 下恒≈0（真机「同屏」根因），
   不许回魂；
③ stashIme 连发补刀：首发之外还有 4 发 postDelayed(180ms 步进) 的 hide(ime)，
   Handler 挂在主线程 Looper，DisposableEffect 离场清队列——不许再单发完事；
④ 长按判定的瞬间仍然是「先收键盘再开录音」的顺序（v0237 判据搬家重钉）；
⑤ 第 3~8 轮不回退：enabledRef 活门控、SETTLE_MS 闹钟、胶囊文案、占位符随键盘、
   语音模式无条件接管。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")


def test_scoped_noop_toolbar_retired_by_round11_override():
    # 原判据（第 9 轮空壳只罩输入框一层、零回调）被第 11 轮第四次改判推翻：
    # 用户点名「要和deepseek一模一样：键盘起着时长按输入框在光标旁出复制粘贴等
    # ui」——默认工具条彻底恢复，空壳与覆写整块退役、不许借壳复活。
    assert "NoopTextToolbar" not in CHAT, \
        "第 11 轮改判：长按要出系统复制/粘贴菜单，空壳必须整块退役"
    assert "LocalTextToolbar provides" in CHAT and "PopupTextToolbar" in CHAT, \
        "第 15 轮第五次改判：菜单回来且是系统 PopupMenu 壳——防气泡靠不拦截，不再靠空壳"


def test_tap_timing_gear_retired():
    assert "TapAwareTextToolbar" not in CHAT and "PressLedger" not in CHAT, \
        "第 8 轮按时长把关的器械已被真机证伪（气泡照样冒），不许留在牌桌上误导下轮"


def test_keyboard_detection_works_under_adjust_resize():
    assert "getLocationOnScreen" in VOICE, \
        "adjustResize 下 root.height 和可见区一起缩，必须拿屏幕坐标比底边差"
    assert re.search(r"\(loc\[1\] \+ root\.height\) - frame\.bottom > root\.height / 5", VOICE), \
        "第 16 轮方向改判：这台 ROM 全屏窗 + IME 悬浮，旧式 frame.bottom−窗底 算出来是负的\
——真机证据：键盘全开着占位符照写「或按住说话」（「我再说一遍…为什么还有按住说话」）。\
翻正成 窗底−可见区底边=键盘高，顶扁式机型上该差≈0 不误报"
    assert "root.height - frame.height()" not in VOICE, \
        "旧公式在 adjustResize 下恒≈0——门控从没扳机就是「键盘语音同屏」的根因，不许回魂"


def test_ime_stash_is_a_barrage_not_a_single_shot():
    stash = re.search(r"fun stashIme\(\) \{[\s\S]*?\n    \}", CHAT)
    assert stash, "stashIme 必须还在：clearFocus 之外要命令窗口控制器收 IME"
    body = stash.group(0)
    assert "focusManager.clearFocus()" in body, "先丢焦点是第一步"
    assert re.search(r"windowInsetsController\?\.hide\(WindowInsetsCompat\.Type\.ime\(\)\)", body) \
        and re.search(r"postDelayed", body) and re.search(r"for \(i in 1\.\.4\)", body), \
        "单发 hide() 会输给键盘 show 动画（第 8 轮真机）：必须再补 4 发延时的"
    assert "imeStash.removeCallbacksAndMessages(null)" in CHAT, \
        "补刀 Handler 离场要清队列，不许留下飘在页外的定时炸弹"
    assert re.search(r"val imeStash = remember \{ Handler\(Looper\.getMainLooper\(\)\) \}", CHAT), \
        "Handler 必须 remember：键盘升降正触发重组，裸 new 会让 DisposableEffect " \
        "每轮换 key、把排队中的补刀自己清光"


def test_voice_start_still_follows_ime_stash():
    assert re.search(r"stashIme\(\)\s*\n\s*voice\.start\(\)", CHAT), \
        "长按判为说话的瞬间先压键盘再开录音（第 9 轮图一「刚进软件长按键盘就出来」）"


def test_earlier_rounds_do_not_regress():
    # 第 8 轮：门控吃最新快照，不吃冻住的闭包
    assert "import androidx.compose.runtime.rememberUpdatedState" in VOICE
    assert "if (!enabledRef.value()) return@awaitEachGesture" in VOICE
    # 第 4/7 轮：占位符随键盘、6 秒自收、松手超时闹钟、胶囊文案
    assert re.search(r'if \(keyboardUp\) "发消息…" else "发消息或按住说话…"', CHAT)
    assert re.search(r"LaunchedEffect\(status\)[\s\S]{0,200}delay\(6000\)", CHAT)
    assert "SETTLE_MS" not in VOICE and "const val MAX_SESSION_MS = 60_000L" in VOICE, \
        "第 15 轮改判：松手即收尾，等包闹钟退役，防挂死靠 60s 硬顶"
    # 第 12 轮：胶囊两态文案随行内 VoiceBar 退役——录音提示改全屏「松手发送，
    # 上移取消」（v02311 契约），输入卡锚点位留「正在听你说…」
    assert '"正在听你说…"' in CHAT
    # 语音模式无条件接管：这半条门控语义本轮原样保留
    assert "voiceMode || !keyboardUp" in CHAT
