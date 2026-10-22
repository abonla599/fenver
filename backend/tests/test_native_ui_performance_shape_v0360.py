"""v0.36.0 性能改造的源码形状锁（只锁本任务真正落地的几项）。

射程：
1. MainActivity 启动不在主线程扫 APK，且启动路径没有 runBlocking；首帧 setContent
   仍排在清扫之后立即执行（清扫只是被挪到后台，不是被挪到界面之前）；
2. ChatUi 冷启动三路并行、消息列表带稳定 key、RichText 的围栏解析按文本 remember；
3. AttPreview 的图片解码离开主线程；
4. 设置弹层关闭窗口动画。

这些判据只证明形状，不证明帧率——帧率归 spec 8.5 的真机测量。设置状态缓存与
IME inset 收口不在本任务内，这里不下断言，免得把没做的事锁成"已完成"。
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NATIVE = REPO / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp"


def _t(rel):
    return (NATIVE / rel).read_text(encoding="utf-8")


def _body(src, anchor):
    """从 anchor 起做大括号配平，把断言收进函数体而不是整文件。"""
    at = src.find(anchor)
    assert at >= 0, f"找不到锚点 {anchor!r}"
    open_at = src.index("{", at)
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at:i + 1]
    raise AssertionError(f"{anchor!r} 的大括号没闭合")


def test_startup_keeps_apk_sweep_off_the_main_thread():
    src = _t("MainActivity.kt")
    assert "runBlocking" not in src, "启动路径又出现了 runBlocking"
    assert 'thread(name = "apk-sweep")' in src, "APK 清扫回到主线程了：首帧又要等磁盘"
    assert src.index("setContent {") > src.index('thread(name = "apk-sweep")'), \
        "首帧挂载不再排在清扫之后：界面被清扫挡住了"


def test_chat_boot_fans_out_and_rich_text_parses_once_per_text():
    src = _t("ui/ChatUi.kt")
    assert "itemsIndexed(messages, key =" in src, "消息列表又丢了稳定 key"
    assert "remember(text) { parseRichTextParts(text) }" in src, \
        "RichText 又在组合体里对全文重切 ``` 了"
    for call in ("loadModelsNow()", "refreshSessions()", "refreshTaskCard()"):
        assert f"launch {{ {call} }}" in src, f"{call} 没进并行分支，冷启动又串行了"


def test_attachment_decode_leaves_the_main_thread():
    body = _body(_t("ui/ChatUi.kt"), "private fun AttPreview")
    assert "withContext(Dispatchers.Default" in body, \
        "AttPreview 的图片解码又占主线程了"


def test_settings_dialog_window_animation_is_off():
    assert "setWindowAnimations(0)" in _t("ui/SettingsUi.kt"), \
        "设置弹层又叠了一层系统窗口动画"
