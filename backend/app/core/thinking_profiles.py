"""思考参数配方**候选表**（v0.39.1 Task 11 探针用，不是生产白名单）。

分工一句话：生产白名单是 `providers.THINKING_PROFILES`——写路径校验与界面三态
（`_thinking_status`）只认那一份；这里放的是**待核实**的配方候选，供
`tools/probe_thinking_profile.py` 按 (profile, level) 组装上游 kwargs。Task 12 拿探针
结论往 `THINKING_PROFILES` 里加条目并补 `verified: True`，本文件不进任何写路径。

为什么候选形状要先钉成代码，而不是探针里手写 kwargs：真调用额度是**一次性**的
（授权不是一句话也不是一次数，而是 `tools/probe_thinking_profile.py` 里那张
 `AUTHORIZED_GRANTS` **授权轮次表**：g1 那一轮 4 次已在 2026-11-01 花完，
 g2 那一轮 3 次冲着 OFF 方向、还没花），拼错一个字段名就白烧一次；而拼错的形状在
 fake client 上测不出来，只有钉成表 + 逐档断言才能在零网络时就发现"第三档我忘了映射"。

为什么本文件**不 import app.core.providers**：那个模块末尾有进程级单例
（providers.py:1059 `store = ProviderStore()`），import 它就等于跑 `_load()`，而
`_load()` 在记录文件缺失时会 `_seed_from_env()`（读 `DEEPSEEK_API_KEY`）、只要播种出
非空列表就 `_flush()` 回写 `providers.json` 与 keys 两份文件——本仓的开发机同时就是
生产机，那一下写的就是线上那份配置（Task 11 修复轮 1 实测过这条路径：先 import 再判
重叠，等于先把文件开进声明只读的那棵树）。所以这里保持零依赖：纯数据 + 一个查表函数，
测试里可以随便 import。（`tools/probe_thinking_profile.py` 确实要用 providers，它在
 import **之前**先把 `PROVIDERS_DB_PATH` 指到仓库外、并先判完重叠，判据见那个文件的
 `run_probe` 闸 2 与 `isolate_runtime_env`。）
"""
import copy

# 与 spec §6.1 的示例档位一致：有序 UI 标签，**不发往上游**。上游取值只认下面那张表。
THINKING_LEVEL_LABELS = ("快速", "均衡", "深入")

# 基线档：不发任何思考参数。名字与 providers.DEFAULT_THINKING_PROFILE 同值，但两份表
# 各自独立——探针要演"不带参数"这一形，不该依赖那个会拖起 ProviderStore 的模块。
NONE_PROFILE = "none"

# 档位标签 → `create(**kwargs)` 的那一份参数（注意：这是**调用形状**，不是出网体，
# 两者别混——见下面 `body_fragment`）。每条都带 `level_used`：核实结论要交给 Task 12
# 直接抄，"这一档到底改变了什么"必须写在明处，否则界面配了三个档而 wire 上永远只有
# 一个值，没人会报错。
#
# 每条还带 `direction`（Task 11b §R3 新加，取值 ∈ {"baseline","on","off"}），因为
# **判据现在吃它**：`probe_thinking_profile.classify_verdict` 按方向决定"什么算证据"。
# 这一格为什么必须显式写而不能由 profile 名字猜：`none` 之外一律当成 "on" 的话，
# 一次成功的关闭测试（推理痕迹消失）会被判成 `unverified` 并以 rc=1 退出——把
# "关掉了"记成"这次没拿到证据"，而 OFF 方向正是 g2 那一轮唯一想买的东西。取不到
# direction 时探针停在预检（rc=2、零出站、零记账），不默认成任何方向。
#
# 为什么 `enable_thinking` 与 `thinking` 都得套在 `extra_body` 里：本机 SDK（实测
# openai 2.37.0）的 `Completions.create` 签名里**没有 `**kwargs`**，裸写 `thinking=`
# 在本地就 `TypeError: unexpected keyword argument`——一次 HTTP 都不会发出去，而账本
# 已经记走一格。`extra_body` 是 SDK 的搬运通道（把字典并进请求体），出网那一格
# 仍是体顶层，与"直接写 thinking"完全一致。
PROBE_PROFILES = {
    NONE_PROFILE: {
        # 措辞是修过的（Task 11 修复轮 1，Important-4）：基线 #1/#3 证明这两个上游
        # **不发参数也在出推理**。旧 label「不使用思考（基线）」对**参数**诚实、
        # 对**模型行为**不诚实，界面照着写就成了"已关闭思考"那句假话。
        # 这一格只能说一件事：服务端这一轮不再发送那颗思考参数。
        "label": "不发思考参数（基线，不代表模型不思考）",
        "level_used": False,
        "direction": "baseline",
        "levels": {},
        "shape": "不发任何思考参数，与今天线上的调用形状完全一致；"
                 "它只是对照组，不是「思考已关闭」的证据",
    },
    "openai_reasoning_effort": {
        "label": "OpenAI reasoning_effort",
        "level_used": True,
        "direction": "on",
        "levels": {
            "快速": {"reasoning_effort": "low"},
            "均衡": {"reasoning_effort": "medium"},
            "深入": {"reasoning_effort": "high"},
        },
        "shape": "顶层 reasoning_effort，取值是上游公开的那三个英文字面量",
    },
    "dashscope_enable_thinking": {
        "label": "阿里云百炼 enable_thinking",
        "level_used": False,
        "direction": "on",
        "levels": {
            "快速": {"extra_body": {"enable_thinking": True}},
            "均衡": {"extra_body": {"enable_thinking": True}},
            "深入": {"extra_body": {"enable_thinking": True}},
        },
        "shape": ("extra_body.enable_thinking=true；这是个布尔开关，三档当前给同一个出网值"
                  "（思考预算 thinking_budget 是另一维，本单不核实，别把它当成档位已配好）"),
    },
    # ↓ 两支 OFF 方向（Task 11b §R3，g2 那一轮要发的形状）。结构与上面两支 ON 完全同构：
    #   三档逐行写死、同一个出网值、`level_used=False`——"档位"在这一维没有意义，
    #   要核实的只有"发出去之后推理痕迹还在不在"。它们与邻居的差别只有那一格取值。
    "deepseek_thinking_disabled": {
        "label": "DeepSeek thinking.type=disabled",
        "level_used": False,
        "direction": "off",
        "levels": {
            "快速": {"extra_body": {"thinking": {"type": "disabled"}}},
            "均衡": {"extra_body": {"thinking": {"type": "disabled"}}},
            "深入": {"extra_body": {"thinking": {"type": "disabled"}}},
        },
        "shape": ("体顶层 thinking={type: disabled}——与 deepseek_thinking_type 只差那一格取值"),
    },
    "dashscope_enable_thinking_off": {
        "label": "阿里云百炼 enable_thinking=false",
        "level_used": False,
        "direction": "off",
        "levels": {
            "快速": {"extra_body": {"enable_thinking": False}},
            "均衡": {"extra_body": {"enable_thinking": False}},
            "深入": {"extra_body": {"enable_thinking": False}},
        },
        "shape": "extra_body.enable_thinking=false；布尔的反方向",
    },
    "deepseek_thinking_type": {
        "label": "DeepSeek thinking.type",
        "level_used": False,
        "direction": "on",
        "levels": {
            "快速": {"extra_body": {"thinking": {"type": "enabled"}}},
            "均衡": {"extra_body": {"thinking": {"type": "enabled"}}},
            "深入": {"extra_body": {"thinking": {"type": "enabled"}}},
        },
        "shape": ("体顶层 thinking={type: enabled}——DeepSeek 自己的形状（经 extra_body 搬进体）。"
                  "补位理由：`reasoning_effort` 不是它的公开参数，"
                  "spec §6.3 那份候选清单只有 OpenAI/DashScope 两支，"
                  "直接套到 deepseek-flash 上会把一次真调用烧在一个已知不该发的字段上"),
    },
}


def probe_profile_ids() -> tuple:
    """候选表里的 profile 名单（排序后给出，便于测试与报告对账）。"""
    return tuple(sorted(PROBE_PROFILES))


def probe_kwargs(profile: str, level: str = None) -> dict:
    """把 (profile, 档位标签) 组装成 `chat.completions.create(**kwargs)` 的那一份参数。

    契约三条，Task 12 复用这个函数时就照这三条判：

    1. profile 不在候选表里 → `ValueError`。**不联网就退出**：认不出来的配方绝不能
       "当成没有参数"发出去——那会把"我要核实 X"演成"我发了一次基线"，白烧额度。
    2. level 不在三档标签里 → `ValueError`，**每一支都要填**（连 `level_used=False`
       那两支也一样）：界面给用户的候选就是这三档，传进表外标签、或者干脆空着，
       都说明调用方在猜，猜就得停。`none` 是唯一例外——它本来就不带参数。
    3. 返回的是**深拷贝**：调用方改它不能把表改脏。表是全局事实来源，一次
       `kwargs["reasoning_effort"] = "minimal"` 会让后面每一次调用都带着这个值。
    """
    name = str(profile or "").strip()
    entry = PROBE_PROFILES.get(name)
    if entry is None:
        raise ValueError(f"未知的思考 profile：{name}（候选表只有 {', '.join(probe_profile_ids())}）")
    if name == NONE_PROFILE:
        # 基线：不带任何思考参数。档位在这一支没有意义，给什么都不发。
        return {}
    label = str(level or "").strip()
    if label not in THINKING_LEVEL_LABELS:
        raise ValueError(f"未知的思考档位：{label or '（没填）'}（候选只有 "
                         f"{', '.join(THINKING_LEVEL_LABELS)}）")
    kwargs = entry["levels"].get(label)
    if kwargs is None:
        # 加了三档标签却漏了这一档的映射：这是候选表自己畸形，不是调用方的错，
        # 但也绝不能退回去发一个别的值——宁可停在这儿，也不带着猜的去烧一次真调用。
        raise ValueError(f"profile {name} 的候选表缺档位 {label} 的映射")
    return copy.deepcopy(kwargs)


def body_fragment(kwargs: dict) -> dict:
    """把 `create(**kwargs)` 的那一份参数折算成**出网 JSON 体**里对应的那一格。

    分得开这两层是必需的：`extra_body` 是 SDK 的搬运通道，不是体里的一层嵌套。
    只看 kwargs 会说"我发的是 extra_body"，而上游收到的其实是体顶层的
    `enable_thinking` / `thinking`——报告里把这两件事混写，Task 12 就会照着
    `extra_body` 去配界面，或照着体形状去改 SDK 调用，两种都通不了。
    """
    given = kwargs or {}
    out = {key: value for key, value in given.items() if key != "extra_body"}
    out.update(given.get("extra_body") or {})
    return copy.deepcopy(out)
