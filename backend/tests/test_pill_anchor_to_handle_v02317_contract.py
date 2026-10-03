"""第 18 轮真机反馈的锚点契约（v0.23.17）：胶囊菜单从「选区大框顶边」收锚到「末端把手」。

用户原话（附真机截图：四行长选区，绿色把手在最后一行，胶囊却飘在最上面一行
之上）：「新版本的这个粘贴离光标还是有点远。」

结案：胶囊壳本身过关（小、原生气质都在），坏在落点取的边——多行选区时文本框
递来的 rect 是整个选区的大框，第 17 轮按 rect.top 定位，选区越长菜单飘得越高；
截图里离把手隔了三行多。改锚选区末端：横向贴 rect.right、纵向悬在 rect.bottom
（最后一行/把手所在）上方，和系统把手工具条同落点；单光标 rect 本来就窄小，
行为不变照样贴光标。只动这两行公式，壳/动作/收放逻辑零改动。

钉三件事：
① 落点公式收锚到选区末端（rect.right / rect.bottom），旧顶边公式不许复活；
② 第 17 轮胶囊壳不变量不回退（PopupWindow、13sp、圆角卡、非 focusable、
   点外面收、四动作、两处挂载、1.6.8 接口形状）；
③ 版本 40/0.23.17，更新日志三段式简版。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"
BUILD = REPO_ROOT / "android-native" / "app" / "build.gradle"
RELEASE_DOC = REPO_ROOT / "docs" / "releases" / "v0.23.17.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")


def test_pill_reanchored_to_selection_end_handle():
    pill = re.search(r"private class PopupTextToolbar\([\s\S]*?\n\}", CHAT)
    body = pill.group(0)
    assert re.search(r"val x = \(rect\.right - w - 6f \* dp\)", body), \
        "横向贴选区末端（把手所在右缘），不再从大框左缘起算"
    assert re.search(r"val above = rect\.bottom - h - 6f \* dp", body), \
        "纵向悬在 rect.bottom（最后一行/把手）上方——多行选区不再把菜单顶老高"
    assert "rect.top" not in body, \
        "第 17 轮的顶边落点是本轮病根（选区越长飘越远），不许复活"
    assert (re.search(r"coerceAtMost\(screenW - w - 8f \* dp\)", body) and
        re.search(r"coerceAtLeast\(8f \* dp\)", body)), "出屏夹回双闸还在"


def test_pill_shell_invariants_not_regressed():
    pill = re.search(r"private class PopupTextToolbar\([\s\S]*?\n\}", CHAT)
    body = pill.group(0)
    assert "PopupWindow(row" in body and "showAtLocation(view, Gravity.TOP or Gravity.START" in body, \
        "胶囊壳本体不回退（细节钉在 v02316 契约，这里只守大门）"
    assert re.search(r"setTextSize\(TypedValue\.COMPLEX_UNIT_SP, 13f\)", body), "13sp 小字不回退"
    assert "isOutsideTouchable = true" in body, "点外面自动收不回退"
    assert re.search(r"override fun showMenu\(rect: Rect, onCopyRequested", body), \
        "1.6.8 接口形状不回退"
    assert CHAT.count("LocalTextToolbar provides") == 2, "两处挂载不回退"


def test_version_bumped_and_release_notes_brief():
    gradle = _read(BUILD)
    # 版本号只有一份真相（gradle），所以每一轮的契约文件都跟着钉**当前**那一格：
    # 第 18 轮推到 40，v0.24.0 推到 41，v0.24.1 推到 42，v0.25.0 推到 43，v0.25.1 推到 44，v0.25.2 推到 45，v0.26.0 推到 46，v0.27.0 推到 47，v0.28.0 推到 48，v0.28.1 推到 49，v0.28.2 推到 50，v0.28.3 推到 51，v0.29.0 推到 52。下面断的是现在那一格，不是当年那一轮。
    assert "versionCode 52" in gradle and 'versionName "0.29.0"' in gradle, \
        "gradle 那一格漂了：这一版应是 52 / 0.29.0，且必须与 docs/releases/v0.29.0.md 同时存在"
    doc = _read(RELEASE_DOC)
    allowed = {"修复了什么", "优化了什么", "新增了什么"}
    heads = re.findall(r"^##\s*(.+?)\s*$", doc, flags=re.M)
    assert heads and set(heads) <= allowed, \
        "更新日志三段式（第 12 轮钦定延续）：没有的不写，解释性段落禁止"
    assert not re.search(r"怎么修|根因|教训|锚点|把手", doc), "简版口径：只报结果，不展开过程"
