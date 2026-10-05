"""搜索源的 API 密钥存储：与 provider_keys.json 同一条纪律，但单独一份文件。

为什么不并进 provider_keys.json：`ProviderStore._flush()` 会按"模型 provider 清单"
整份重建那个文件——搜索源密钥不在那份清单里，它要是躺在那儿，管理员下一次保存
模型设置就会把它悄悄抹掉。复用的纪律是那三条：与记录文件分离存储、原子写
（走公共 write_json_atomic，0600）、对外只露 providers.mask_key 的「末四位」口径；
并且读到任何一把 key 都顺手登记进 logsanitizer，让它万一出现在别的日志行里
也会被脱敏。

配置方式（v0.32 W2 起）：管理员在「设置 → 搜索源」里登记，走 /v1/search-keys
那组端点进本模块的 set_key/delete_key；直接手写 data/search_keys.json
（或用 SEARCH_KEYS_DB_PATH 指走）仍然有效——读侧本来就认这份文件。
缺失或形如占位符的 key 等于该源「未启用」：search_api_sources 直接回空、
不发任何出站请求，降级链自动落到下一源——没有 key 的机器上，整条链的行为
与只有爬取源时完全一致。
"""
import json
import os
import threading

from app.core import logsanitizer
from app.core.atomic_write import write_json_atomic
from app.core.paths import data_root
from app.core.providers import looks_placeholder, mask_key

# 允许登记密钥的源名白名单。这份名单必须与降级链里的 API-key 源同步——
# 漂移的后果是「界面上登记了一把没人消费的 key」，判据由合同测试
# （tests/test_search_keys_api_contract.py）钉着，不靠人记忆。
ALLOWED_SOURCES = ("bocha", "tavily")

# 写路径的读-改-写要串行：两个管理员同时各存一把源 key，
# 没有这把锁就是后写入的那份整体覆盖前一份，悄悄抹掉一把已经生效的凭据。
_WRITE_LOCK = threading.Lock()


class SearchKeyError(ValueError):
    """登记请求过不了闸：未知源名、空 key、占位符形状。消息里没有值，只有形状。"""


def path() -> str:
    env_path = os.getenv("SEARCH_KEYS_DB_PATH")
    if env_path:
        return os.path.abspath(env_path)
    return os.path.join(data_root(), "data", "search_keys.json")


def _read_all() -> dict:
    p = path()
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
    except (ValueError, OSError) as e:
        # 读不动就当没配：与 ProviderStore._load_keys 同一个方向——宁可少一个源，
        # 也不要拿一份解析不明的文件里的东西当凭据发出去。
        print(f"⚠️ 搜索源密钥读不了（{e}），本轮按未配置处理")
        return {}
    for value in data.values():
        logsanitizer.register(value if isinstance(value, str) else "")
    return data


def get(name: str) -> str:
    """该源实际使用的 key；""（未启用）包含缺失与占位符两种。

    每次调用现读文件而不是导入期缓存：配 key 不该要求重启，而这份文件几十字节，
    读的代价相对一次外网请求可以忽略。
    """
    return _usable(_read_all().get(name))


def masked(name: str) -> str:
    """唯一允许对外出现的形态：mask_key 的「末四位」。"""
    return mask_key(get(name))


def _usable(value) -> str:
    """存储里那格的取用口径，与 get() 完全同一条：非 str、空白、占位符都算没配。"""
    key = (value if isinstance(value, str) else "").strip()
    return "" if looks_placeholder(key) else key


def set_key(name: str, key: str) -> str:
    """登记该源的 key，返回唯一可外显的掩码形态。

    纪律逐字照抄 providers 写路径：原子写（write_json_atomic，0600）、值一落盘
    就登记进 logsanitizer、明文只进这一次磁盘写——返回值、日志、审计里出现的
    都是掩码。占位符形状直接拒收而不是"存下但视作未启用"：配界面上一格显示
    「已配置」而链实际不用，正是 providers 当年"切换模型没反应"的那个根因形状，
    不复发。
    """
    if name not in ALLOWED_SOURCES:
        raise SearchKeyError("未知搜索源")
    value = str(key or "").strip()
    if not value:
        raise SearchKeyError("密钥不能为空")
    if looks_placeholder(value):
        raise SearchKeyError("密钥形如占位符，请填写真实凭据")
    with _WRITE_LOCK:
        data = _read_all()
        data[name] = value
        write_json_atomic(path(), data)
    logsanitizer.register(value)
    return mask_key(value)


def delete_key(name: str) -> bool:
    """移除该源的 key；原本没有登记过回 False（与 ProviderStore.delete 同一口径）。"""
    if name not in ALLOWED_SOURCES:
        raise SearchKeyError("未知搜索源")
    with _WRITE_LOCK:
        data = _read_all()
        if name not in data:
            return False
        data.pop(name)
        write_json_atomic(path(), data)
    return True


def list_masked() -> list:
    """配置面的整份视图：每个可登记的源一行，只有状态与末四位掩码。

    这里是密钥对外能看到的全部：明文、长度、前缀一概不出这道门
    （mask_key 的注释写着为什么连"35 位"都不该给）。
    """
    data = _read_all()
    rows = []
    for name in ALLOWED_SOURCES:
        key = _usable(data.get(name))
        rows.append({"name": name, "configured": bool(key),
                     "masked": mask_key(key) if key else ""})
    return rows
