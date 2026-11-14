"""v0.36.9：冷启动首帧不许空等网络（2026-11-14）。

用户真机反馈：「打开软件时会话需要加载一下才显示，点设置也要加载一下才完整」。
根因是冷启动首帧没有任何本地数据：会话列表与模型清单都要等一次网络往返（服务端在
Cloudflare 隧道后面，往返 300ms~1s 量级），屏上就先空一拍再跳出来。

修法是本机存一份按 userId 隔离的启动快照，首帧直接渲染上次的真值，网络回来只负责刷新。
这条锁钉四件事：
① 快照真的存在，且按 userId 分键读——换账号后绝不能把上一个人的会话标题显示出来；
② ❌ 不变量：失败时不覆盖旧快照（一次断网不许把真值擦成空表）；
③ ChatScreen 的首帧初值来自快照，不是空表；
④ 快照写入点是 loadModelsNow / refreshSessionsNow 的成功分支，不是别处。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = (REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever"
      / "assistant" / "nativeapp")


def _read(rel):
    return (UI / rel).read_text(encoding="utf-8")


def test_snapshot_is_per_user_and_validated_on_read():
    prefs = _read("Prefs.kt")
    assert "data class StartupSnapshot(" in prefs
    assert "val owner: String" in prefs, "快照必须带属主，否则跨账号会串"
    assert "private fun snapshotKey(owner: String)" in prefs, "按 userId 分键"
    assert "if (owner.isEmpty()) return StartupSnapshot()" in prefs
    assert "parsed.owner == owner" in prefs, \
        "读到别人的快照必须当空处理——这是唯一一条防止跨账号显示的防线"
    assert "if (s.owner.isEmpty()) return" in prefs, "未登录不许写快照"


def test_chat_screen_first_frame_comes_from_the_snapshot():
    chat = _read("ui/ChatUi.kt")
    assert "val bootSnapshot = remember(bootOwner) { Prefs.snapshot(bootOwner) }" in chat
    assert "var sessions by remember { mutableStateOf(bootSnapshot.sessions) }" in chat, \
        "会话列表首帧要来自快照；退回空表就是这次要修的「空一拍」"
    assert "var providers by remember { mutableStateOf(bootSnapshot.models) }" in chat
    assert "var serverDefault by remember { mutableStateOf(bootSnapshot.serverDefault) }" in chat
    assert "listOf<SessionSummary>()" not in chat, \
        "会话首帧不许再退回空表（那正是用户反馈的现象）"


def test_snapshot_is_written_only_on_success():
    chat = _read("ui/ChatUi.kt")
    assert "fun persistStartupSnapshot()" in chat
    body = chat[chat.index("fun persistStartupSnapshot()"):]
    body = body[:body.index("\n    }")]
    assert "Prefs.saveSnapshot(" in body
    # 写入点必须在成功分支：数一遍调用位置，且都不在 onFailure 里
    calls = [m.start() for m in re.finditer(r"persistStartupSnapshot\(\)", chat)]
    assert len(calls) >= 3, "至少：定义 1 处 + 两处成功写入"
    for m in re.finditer(r"persistStartupSnapshot\(\)", chat):
        prev = chat[max(0, m.start() - 400):m.start()]
        assert "onFailure" not in prev[-120:], \
            "快照绝不能在失败分支写：一次断网会把上次的真值擦成空表"
