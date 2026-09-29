"""第 7 轮真机反馈的双端契约（v0.23.6）：粘贴工具条恢复 + 浮层卡死根治 + 原创胶囊。

钉四件事：
① 键盘开着时长按输入框必须能浮出「粘贴/全选/选择」：第 4 轮装的 NoopTextToolbar
   空壳整块退役（第 8 轮改判：允许覆写，但只许按压时长把关的壳）；
   而语音长按的键盘门控（voiceMode || !keyboardUp）必须原样保留——两者不打架；
② 穹顶卡死根治：松手(send)后必须挂超时闹钟（SETTLE_MS + postDelayed），
   到点没等到 onResults/onError 就自己收尾——有半截字按半截发，没字安静收；
   识别回调与 destroy 必须先拆闹钟，不许让浮层挂在屏上；
③ 录音浮层去穹顶换原创「语音胶囊」：不得再出现 400.dp 大圆 + offset 沉底的
   微信式蓝穹顶；换成贴底悬浮卡（圆角 26dp、品牌绿描边、麦克风呼吸点、
   「正在听你说…」/「松开手指就取消」两态文案），取消态整卡转珊瑚；
④ 第 4/5/6 轮判据不许回退（点名开麦、引擎死亡路由、手动备用通路、无 emoji）。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")


def test_paste_toolbar_restored_and_voice_gate_kept():
    # 第 7 轮「长按出粘贴」的需求在第 9 轮被用户本人收回（「这个粘贴我说过我
    # 不要」），空壳改判回输入框作用域在位——本文件只继续钉第 6/7 轮不被第 9
    # 轮顺带弄丢的两样：语音长按让键盘的门控、以及语音模式无条件接管。
    assert re.search(r"enabled = \{[^}]*!keyboardUp", CHAT), \
        "语音长按仍要让着键盘：键盘起着不许再触录音（第 9 轮真机图仍在控诉）"
    assert "voiceMode || !keyboardUp" in CHAT, "语音模式无条件接管，文本模式让键盘"


def test_release_exits_without_stuck_overlay():
    # 第 7 轮真机图「浮层卡死」的痛点依然成立，但形态第 15 轮整改：松手当场
    # 收尾（等包闹钟整个退役——用户点名「松开手指要立刻响应」），防挂死只剩
    # 60 秒会话硬顶一道闹钟守着事件彻底丢光的未知路。
    assert "SETTLE_MS" not in VOICE and "clearSettle" not in VOICE, \
        "等包闹钟不许复活：它就是『松手后卡 1.6 秒才动』的本体"
    assert "mainHandler.postDelayed(wd, MAX_SESSION_MS)" in VOICE, \
        "硬顶闹钟在位：抬手事件整个丢了，会话也得最迟一分钟自己收场"
    assert re.search(r"fun finish\(send: Boolean\) \{[\s\S]{0,400}listening = false", VOICE), \
        "finish 的收尾一步做完，不留第二阶段"
    assert re.search(r"if \(t\.isNotBlank\(\)\) onFinal\?\.invoke\(t\)\n\s*else onError\?\.invoke", VOICE), \
        "松手那一刻手里有什么就发什么（半截字也算），别把用户说过的话吞了"
    assert re.search(r"override fun onResults\(results: Bundle\?\) \{[\s\S]{0,200}if \(!listening\) return", VOICE), \
        "迟到的终包必须被守卫作废：收尾之后引擎再回不许二次发送"


def test_dome_replaced_by_original_capsule():
    assert "offset(y = 200.dp)" not in VOICE and "width(400.dp).height(400.dp)" not in VOICE, \
        "微信式蓝穹顶（大圆沉底）必须拆干净——用户原话「不用非得穹顶」"
    # 第 12 轮再改判：胶囊这个载体（26dp 圆角、呼吸麦克风点、珊瑚两态）随行内
    # VoiceBar 整块退役，录音 UI 升级为全屏 VoiceScreen——本条只钉「无穹顶」与
    # 「不卡死」两件事，两态文案与波纹资产的现行判据搬家到 v02311 契约。
    assert "语音胶囊" not in VOICE and "fun VoiceBar(" not in VOICE, \
        "行内胶囊残骸不许留在文件里误导下轮"
    assert "fun VoiceScreen(" in VOICE, "现行录音界面是全屏 VoiceScreen"


def test_earlier_rounds_do_not_regress():
    # 第 5 轮：点名 + 枚举兜底 + 手动备用通路
    assert "去给「$voiceHintLabel」开麦克风" in CHAT and "不是本助手" in CHAT
    assert "fun speechServiceCandidates(" in VOICE and "queryIntentServices" in VOICE
    assert "rememberSystemVoiceFallback" in VOICE
    assert "ACTION_RECOGNIZE_SPEECH" not in CHAT, "第 3 轮判据：不自动弹系统语音界面"
    # 第 6 轮：引擎死亡路由 + 换引擎入口
    assert "ENGINE_DEAD_CODES" in VOICE and "onEngineDead" in VOICE
    assert "换个语音引擎" in CHAT and "选一个语音识别引擎" in CHAT
    # 第 4 轮：状态条自收 + 占位符随键盘
    assert re.search(r"LaunchedEffect\(status\)[\s\S]{0,200}delay\(6000\)", CHAT)
    assert re.search(r'if \(keyboardUp\) "发消息…" else "发消息或按住说话…"', CHAT)
