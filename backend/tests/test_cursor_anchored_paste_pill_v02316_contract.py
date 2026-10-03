"""第 17 轮验收改判的粘贴菜单契约（v0.23.16）：大列表退场，光标锚定小胶囊上位。

用户原话：「这次改的不错，保持，但是这个粘贴的UI不好看，而且我希望它小一点
并且位置是跟着光标的」。

「改的不错，保持」= v0.23.15 键盘信号四件套原样不动（本轮只动菜单壳）；
「不好看/小一点/跟着光标」= 第 15 轮的 PopupMenu 竖排大列表被否——那是贴控件
角落的系统下拉，块头大、位置死。换成紧凑胶囊 PopupWindow：横排一行 13sp 小字、
inverseSurface 深色圆角卡片＋投影，直接弹在文本框递来的光标 rect 正上方（右缘
出屏夹回、顶部放不下翻下方）；非 focusable 不抢键盘；点外面/选一项自动收。

钉五件事：
① PopupMenu 整个退场（import 都不留），胶囊壳 PopupWindow + showAtLocation
   光标锚定在位；
② TextToolbar 接口形状（1.6.8 showMenu(rect, 四可空回调) + status）不许动，
   四个动作照旧由文本框回调执行；
③ 两处挂载点（聊天输入框/消息编辑框）都换成带配色的新签名，LocalView.current
   仍在 remember 计算体外（CI 编译判例）；
④ 第 16 轮键盘信号四件套不回退（减法翻正/maxFB/自愈/轮询）；
⑤ 版本 39/0.23.16，更新日志三段式简版。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UI = REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"
BUILD = REPO_ROOT / "android-native" / "app" / "build.gradle"
RELEASE_DOC = REPO_ROOT / "docs" / "releases" / "v0.23.16.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


CHAT = _read(UI / "ui" / "ChatUi.kt")
VOICE = _read(UI / "ui" / "VoiceUi.kt")


def test_popup_menu_retired_cursor_anchored_pill_in_place():
    # 「粘贴的UI不好看…小一点…位置跟着光标」——竖排大列表换光标锚定小胶囊
    assert "import android.widget.PopupMenu" not in CHAT and "PopupMenu(" not in CHAT, \
        "第 17 轮改判：v0.23.14 的 PopupMenu 大列表被用户否了（块头大、贴角落、不跟光标），import 与调用全退场（注释里留案底无妨）"
    pill = re.search(r"private class PopupTextToolbar\([\s\S]*?\n\}", CHAT)
    body = pill.group(0)
    assert "PopupWindow(row" in body and "showAtLocation(view, Gravity.TOP or Gravity.START" in body, \
        "胶囊壳：PopupWindow 按窗口坐标定位，不是锚控件的列表菜单"
    assert (re.search(r"val x = \(rect\.right - w - 6f \* dp\)", body) and
        re.search(r"coerceAtMost\(screenW - w - 8f \* dp\)", body)), \
        "第 18 轮收锚（真机「离光标还是有点远」）：x 贴选区/光标右缘，右缘出屏夹回"
    assert (re.search(r"val above = rect\.bottom - h - 6f \* dp", body) and
        re.search(r"if \(above >= 0f\) above else rect\.bottom", body)), \
        "纵向锚到选区末端把手（最后一行上方）——多行大选区不再把菜单顶得老高"
    assert re.search(r"setTextSize\(TypedValue\.COMPLEX_UNIT_SP, 13f\)", body), \
        "「小一点」：13sp 小字横排一行"
    assert "cornerRadius = d(10f)" in body and "setColor(bgColor)" in body, \
        "10dp 圆角卡片底（inverseSurface 配色由构造参数递入）"


def test_toolbar_interface_and_actions_unchanged():
    pill = re.search(r"private class PopupTextToolbar\([\s\S]*?\n\}", CHAT)
    body = pill.group(0)
    assert re.search(r"override fun showMenu\(rect: Rect, onCopyRequested", body), \
        "接口形状仍按 BOM 2024.06.00（Compose 1.6.8）showMenu(rect, 四可空回调) 钉死"
    assert "override val status: TextToolbarStatus" in body, "1.6.8 要求的 status 成员不许漏"
    for act in ("剪切", "复制", "粘贴", "全选"):
        assert act in body, f"四个动作照回调供给，一个都不缺——只换壳不改行为"
    assert re.search(r"setOnClickListener \{ act\?\.invoke\(\); hide\(\) \}", body), \
        "点一项：动作走文本框原回调，菜单当场收"
    assert "ViewGroup.LayoutParams.WRAP_CONTENT, false)" in body, \
        "PopupWindow 非 focusable：不抢输入框焦点、键盘不收"
    assert "isOutsideTouchable = true" in body, "点外面自动收（第 15 轮铁律延续）"
    assert re.search(r"override fun hide\(\) \{\s*\n\s*popup\?\.dismiss\(\)", body), \
        "hide 真 dismiss——选择变化/重弹前先收干净"


def test_both_call_sites_upgraded_colors_and_view_hoist_kept():
    assert CHAT.count("LocalTextToolbar provides") == 2, \
        "聊天输入框 + 消息编辑框两处都罩胶囊壳，别处文本域不受牵连"
    assert ("val toolbarBg = scheme.inverseSurface.toArgb()" in CHAT and
        "PopupTextToolbar(view, toolbarBg, toolbarFg)" in CHAT), \
        "输入框挂点：深色胶囊配色从组合里取好递进构造"
    assert "PopupTextToolbar(editView, editTbBg, editTbFg)" in CHAT, \
        "编辑框挂点同款升级"
    assert "remember(view, toolbarBg, toolbarFg)" in CHAT and \
        "remember(editView, editTbBg, editTbFg)" in CHAT, \
        "LocalView.current/配色都在 remember 计算体外取（CI 编译判例不回退）"


def test_keyboard_signal_round16_suite_not_regressed():
    # 「这次改的不错，保持」——第 16 轮四件套一个字不动
    assert re.search(r"\(loc\[1\] \+ root\.height\) - frame\.bottom > root\.height / 5", VOICE), \
        "减法翻正路保持"
    assert re.search(r"maxFB - frame\.bottom > maxFB / 5", VOICE), "maxFB 自校准路保持"
    assert "if (!oiv.isAlive)" in VOICE, "僵尸观察者自愈保持"
    assert re.search(r"while \(true\) \{\s*\n\s*delay\(250\)\s*\n\s*polled\[0\]\?\.invoke\(\)", VOICE), \
        "250ms 轮询兜底保持"
    assert re.search(r'if \(keyboardUp\) "发消息…" else "发消息或按住说话…"', CHAT), \
        "占位符判据保持"


def test_version_bumped_and_release_notes_brief():
    gradle = _read(BUILD)
    # 版本号只有一份真相（gradle），所以每一轮的契约文件都跟着钉**当前**那一格：
    # 第 17 轮把它推到 39，v0.24.0 推到 41，v0.24.1 推到 42，v0.25.0 推到 43，v0.25.1 推到 44，v0.25.2 推到 45，v0.26.0 推到 46，v0.27.0 推到 47，v0.28.0 推到 48，v0.28.1 推到 49，v0.28.2 推到 50，v0.28.3 推到 51，v0.29.0 推到 52，v0.29.1 推到 53。下面断的是现在那一格，不是当年那一轮。
    assert "versionCode 53" in gradle and 'versionName "0.29.1"' in gradle, \
        "gradle 那一格漂了：这一版应是 53 / 0.29.1，且必须与 docs/releases/v0.29.1.md 同时存在"
    doc = _read(RELEASE_DOC)
    allowed = {"修复了什么", "优化了什么", "新增了什么"}
    heads = re.findall(r"^##\s*(.+?)\s*$", doc, flags=re.M)
    assert heads and set(heads) <= allowed, \
        "更新日志三段式（第 12 轮钦定延续）：没有的不写，解释性段落禁止"
    assert not re.search(r"怎么修|根因|教训|逆向|改判", doc), "简版口径：只报结果，不展开过程"
