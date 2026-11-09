r"""§9.6 第 6 条：本版不存在任何点击/输入注入代码——射程含整棵 Android 树。

规划书 §9.3 把「点击/滑动/输入注入」整段划到范围外，§9.6 第 6 条要求用 AST 扫描钉死。
B 轨落地时这道锁只扫了服务端三个模块（`test_v040_device_context.py` 里那份），因为
那时 Android 侧还没有任何无障碍代码。A 轨要落 `AccessibilityService` 了，门必须先立：
无障碍服务一旦拿到 `performAction` / `dispatchGesture`，「只读」就只剩口头承诺。

这份锁判的是**文本**，不是 AST（射程要写准，不许说成"整个 Android 树都审过了"）：

- 扫 `android-native/app/src` 下的 `*.kt` / `*.java` / `*.xml`；注释先剥掉再判，
  免得一句解释性注释把自己判红；
- 只认下面那几串字面量。字符串拼接、反射、`javaClass.getMethod("perform"+"Action")`
  这类形状它看不见——那种写法要过 Kotlin 编译这关，且 A 轨的评审要单独看一眼；
- 判据是"有没有这个调用"，不是"调用得对不对"；正确性归 A 轨的行为测试。

禁令分两轴：

1. 代码侧：点击 / 长按 / 输入 / 粘贴 / 滚动 / 手势注入的调用与常量；
2. 声明侧：`canPerformGestures="true"`（无障碍服务自报会注入手势）与
   `flagRequestFilterKeyEvents`（键盘内容监听，§9.3 里也没有这一项）。

要放开任何一条，正确的做法不是删这行判据，而是先改规划书 §9.3 的范围表，再回来把
这一条连同它的反例一起改——这样"范围变了"会留下一笔可见的记录。
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ANDROID_SRC = REPO_ROOT / "android-native" / "app" / "src"

# 代码侧禁令：命中即"这一版能点/能输入"，与 §9.3 的"不做"直接冲突。
# 每一条都带理由，别把理由删成空串——那会让下一个人以为它和别的是同一类。
CODE_BANS = {
    "dispatchGesture": "手势注入（滑动/长按/点击都能经它合成）",
    "performAction": "节点动作，点击与输入都走这一个入口",
    "ACTION_CLICK": "点击",
    "ACTION_LONG_CLICK": "长按",
    "ACTION_SET_TEXT": "输入文本",
    "ACTION_PASTE": "粘贴（剪贴板写入输入框）",
    "ACTION_SCROLL_FORWARD": "滑动",
    "ACTION_SCROLL_BACKWARD": "滑动",
    "GestureDescription": "手势描述对象",
}

# 声明侧禁令：服务自报的能力与键盘监听，都是"能看见/能改"的开关。
XML_BANS = {
    'canPerformGestures="true"': "无障碍服务自报会注入手势",
    "flagRequestFilterKeyEvents": "键盘内容监听，§9.3 范围里没有这一项",
}


def _strip_comments(src: str) -> str:
    """剥掉 `//` 与 `/* */`：注释里提到这些名字是解释，不是能力。"""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return re.sub(r"(?m)//.*$", "", src)


def _android_sources() -> list:
    """(相对路径, 已剥注释正文, 后缀)。路径按 as_posix 排序，报错时能直接定位。"""
    out = []
    for path in sorted(ANDROID_SRC.rglob("*")):
        if path.suffix not in (".kt", ".java", ".xml") or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(REPO_ROOT).as_posix()
        out.append((rel, _strip_comments(text), path.suffix))
    return out


def _code_problems(src: str) -> list:
    return ["出现 %s（%s）" % (token, why) for token, why in CODE_BANS.items()
            if token in src]


def _xml_problems(src: str) -> list:
    return ["出现 %s（%s）" % (token, why) for token, why in XML_BANS.items()
            if token in src]


def test_scan_roots_are_not_empty_and_cover_the_package_that_will_grow():
    """先证"扫到了东西"：射程缩到空目录时，下面那条绿是空转。

    这条不是仪式。`ANDROID_SRC` 写错一层（比如少了 `main`）时，rglob 一声不响地
    返回空列表，禁令判据全部通过——那种绿比红更贵。
    """
    sources = _android_sources()
    kinds = {}
    for rel, _, suffix in sources:
        kinds[suffix] = kinds.get(suffix, 0) + 1
    # 地板只用来证"扫到了东西"，不拿来钉当前文件数：那种紧贴现状的数字会在
    # 一次正常重构（合并/删两个文件）时报一句与重构无关的红。
    assert kinds.get(".kt", 0) >= 10, "Kotlin 源文件少于 10 份：扫描根多半指错了：%s" % kinds
    assert kinds.get(".xml", 0) >= 3, "XML 少于 3 份：清单与资源树没被扫进来：%s" % kinds
    package_dir = "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/"
    assert any(rel.startswith(package_dir) for rel, _, _ in sources), (
        "扫描射程里没有 nativeapp 包：A 轨的无障碍服务正落在这个包里，"
        "射程漏掉它等于这道门对新代码完全不判")


def test_android_tree_declares_no_click_or_input_injection():
    """本版 Android 树里不许出现任何注入调用或注入能力声明。"""
    problems = []
    for rel, text, suffix in _android_sources():
        found = _xml_problems(text) if suffix == ".xml" else _code_problems(text)
        problems.extend("%s: %s" % (rel, p) for p in found)
    assert not problems, (
        "§9.3 把点击/输入注入划在范围外，这几处却把能力放进来了：\n%s\n"
        "要做动作请先改规划书 §9.3 的范围表，再回来连判据一起改——"
        "别只删这行断言" % "\n".join(problems))


@pytest.mark.parametrize("snippet", [
    'node.performAction(AccessibilityNodeInfo.ACTION_CLICK)',
    'root.dispatchGesture(gesture, null, null)',
    'node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)',
    'node.performAction(AccessibilityNodeInfo.ACTION_PASTE)',
    'val g = GestureDescription.Builder()',
])
def test_code_ban_predicate_can_go_red(snippet):
    """变异自证：塞进一句真注入调用，判据必须报红——否则上面那条绿没有牙。"""
    assert _code_problems(snippet), "判据认不出 %r：它对真实调用是瞎的" % snippet


@pytest.mark.parametrize("snippet", [
    'android:canPerformGestures="true"',
    'android:accessibilityFlags="flagDefault|flagRequestFilterKeyEvents"',
])
def test_xml_ban_predicate_can_go_red(snippet):
    assert _xml_problems(snippet), "判据认不出 %r" % snippet


def test_predicate_does_not_fire_on_the_read_only_shape_we_do_want():
    """负控：读到文本、包名、窗口标题这些**只读**形状不许被判红。

    没有这一格，判据可以靠"见谁都咬"混过上面那两条——那种锁会逼着下一个人
    把正常代码改歪来讨好测试。
    """
    ok = (
        "val text = node.text?.toString()\n"
        "val pkg = root.packageName?.toString()\n"
        "if (node.isEditable) redact(node)\n"
        "info.flags = info.flags or AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS\n"
    )
    assert _code_problems(ok) == [], "只读形状被判红了：%s" % _code_problems(ok)
    assert _xml_problems('<service android:name=".DeviceContextService" />') == []
