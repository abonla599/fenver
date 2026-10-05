"""搜索源的 API 密钥存储：与 provider_keys.json 同一条纪律，但单独一份文件。

为什么不并进 provider_keys.json：`ProviderStore._flush()` 会按"模型 provider 清单"
整份重建那个文件——搜索源密钥不在那份清单里，管理员下一次保存模型设置就会把它
悄悄抹掉。复用的纪律是那三条：与记录文件分离存储、原子写、对外只露
providers.mask_key 的「末四位」口径；并且读到任何一把 key 都顺手登记进
logsanitizer，让它万一出现在别的日志行里也会被脱敏。

配置方式：在 data/search_keys.json 手写 {"bocha": "sk-…", "tavily": "tvly-…"}
（或用 SEARCH_KEYS_DB_PATH 指走，与其余存储同一口径）。缺失或形如占位符的 key
等于该源「未启用」：search_api_sources 直接回空、不发任何出站请求，
降级链自动落到下一源——没有 key 的机器上，整条链的行为与只有爬取源时完全一致。
"""
import json
import os

from app.core import logsanitizer
from app.core.paths import data_root
from app.core.providers import looks_placeholder, mask_key


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
    value = _read_all().get(name)
    key = (value if isinstance(value, str) else "").strip()
    return "" if looks_placeholder(key) else key


def masked(name: str) -> str:
    """唯一允许对外出现的形态：mask_key 的「末四位」。"""
    return mask_key(get(name))
