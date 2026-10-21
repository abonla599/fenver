"""设置页首帧直接显示已加载的模型，不许先闪「读取中」。

真机反馈（2026-10-22）：点设置时先闪一帧「读取中… / 正在读取模型清单…」，
再跳到真实模型。根因不是网络慢——ChatScreen 冷启动已经把 /v1/models 拉回来了，
是 SettingsSheet 每次重新进组合时把自己的 providers 重置成空列表，首帧必然
画等待态。修法是把 ChatScreen 已有的清单当首帧，后台刷新保留。

本机跑不了 Compose，观感留给真机；源码层只钉住这条初始化形状：
SettingsSheet 必须接收 cachedProviders 并以它起算，ChatScreen 必须把 providers
传进去。判据全是剥掉注释后的原文，只在注释里说"已缓存"骗不过去。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_apk_release_discipline import _code                        # noqa: E402
from tests.test_schedule_ui_contract import _native                       # noqa: E402


def _settings() -> str:
    return _code(_native("nativeapp/ui/SettingsUi.kt"))


def _chat() -> str:
    return _code(_native("nativeapp/ui/ChatUi.kt"))


def test_settings_sheet_starts_from_the_cached_provider_list():
    src = _settings()
    assert "fun SettingsSheet(page: String, cachedProviders: List<ModelInfo>," in src, \
        "SettingsSheet 不再接收 ChatScreen 已加载的模型清单：首帧又要从空列表起算"
    assert "var providers by remember { mutableStateOf(cachedProviders) }" in src, \
        "providers 没有用缓存清单做初值：点设置会先闪「读取中」"
    assert "var modelsLoaded by remember { mutableStateOf(cachedProviders.isNotEmpty()) }" in src, \
        "modelsLoaded 没跟着缓存初始化：有缓存也会先挂「正在读取模型清单」"
    assert "listOf<ModelInfo>()" not in src, \
        "SettingsUi 里又出现空模型列表初值：首帧等待态会回来"


def test_chat_screen_passes_its_loaded_models_into_settings():
    assert "cachedProviders = providers" in _chat(), \
        "ChatScreen 没把已经拉到的 providers 传给 SettingsSheet：缓存接线断了"
