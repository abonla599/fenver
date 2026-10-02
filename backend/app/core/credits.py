"""积分的经济内核：单价的形状、基准价、倍率（展示）与积分（结算/影子）的换算。

为什么单独一个模块而不是散进 providers/usage：同一把尺子被三条路读——
配置面（providers 的 pricing 校验）、展示面（/v1/models 与 _public 的倍率）、
护栏与管理看板（影子积分的按天派生）。口径只许有一份，两处各写一套正是
卡片 §2 开篇点名要防的事故（"价格消耗倍率"这类数字一旦漂移，用户拿它当合同价）。

四条定死的口径（PRD D11–D25、卡片第二版 §2/§3）：
1. **三态**：`0.00x` = 明确配了免费价；`?x` = 未定价；正常 = 真实倍率。
   未定价**算不出就说算不出**（None + 人话缺口），任何路径不许当成 0 记——
   在平台代付的实况里，"未定价当 0"等于白烧部署者的钱还报"没花钱"。
2. **倍率只进展示，结算只认真值**（D13）：倍率 = 该模型混合单价 ÷ 基准混合单价，
   按写死的典型输入:输出比例（默认 4:1）折算，两位小数 + 一处常量后缀 `x`；
   结算/影子一律真实 token × 真实单价（内部 6 位小数）。两者不许互相代入。
3. **基准价是单一真相**，住在 `config_store.DEFAULTS["credit_benchmark"]`
   （示例口径 输入 ¥1/百万 + 输出 ¥4/百万 = 1.00x，部署者可改）。
   刻意**不**用"默认模型 = 1.0x"：锚跟着 ★ 漂移，倍率表就不能当长期记忆。
4. **影子积分**（T5.20，本批的核心价值）：每次调用按真实单价算出"本应消耗多少
   积分"。`0.00x` 的免费期照常算——这是运营方垫资额的唯一来源，也是 1.0 定价的
   唯一依据。**派生显示、按「当日账行 × 当前单价」现算**：`usage.json` 的写口径
   一字不动（只存真值 token，不塞积分）；本批没有真扣钱，所以不需要账本快照
   （卡片 §2.5：只有将来真 consume 的那笔才落库带快照）。

计价档位口径（T0.3 卡片 D10 的延续）：`reasoning_tokens` 含在 `completion_tokens`
里，随输出档计价（不另加一遍，加了就双重计费）；`cached_tokens` 含在
`prompt_tokens` 里，**不打折**（各家缓存价不同，打折就是把账算成猜的）。
上游没回 usage 的行 token 记 0，影子也是 0，但 `unknown_usage` 计数在账上，
看板要如实说"这部分是被低估的下限"。

`pricing.mode = per_call` 本版只预留字段、不实现结算（D24）：按次/按张/按秒计价的
API 没有输入输出两档，套 token 公式会算出无意义的数（且极易显示成 `0.00x`，正掉进
"免费/没价"混淆的坑）。结算路径遇到它必须显式拒绝并给人话，不许静默按 token 算。
"""
from datetime import datetime

from app.core import config_store, usage

# 倍率后缀：全产品一个符号、一处常量（卡片 §2.3b 拍板半角 x）。
# 网页/安卓两条泳道都从这一份数据层的 label 字符串抄，不许各写一次。
MULTIPLIER_SUFFIX = "x"
UNPRICED_LABEL = "?" + MULTIPLIER_SUFFIX

# 影子护栏到线时用户看到的那句话（卡片 §3-2：停在人话提示上，不是错误码）。
# 纪律：本批没有余额概念，用户端文案不许出现「余额/已消费/已扣」。
SHADOW_STOP_MESSAGE = ("今日积分护栏已到线：已为你暂停，等待你确认是否继续；"
                       "本次没有向模型发起新的付费调用")

# pricing.mode 的合法取值：token 已实现；per_call 是**结构预留**（流水是 append-only，
# 事后加字段比事前贵），但任何结算路径碰到它都必须拒绝。
PRICING_MODES = ("token", "per_call")

_CREDITS_DECIMALS = 6  # 内部 6 位小数；界面 2 位是显示层的事


class PricingError(ValueError):
    """单价配置形状不对：一句人话点名缺哪一格。配置错误不该被静默宽容（半套价格
    比没价格更危险——它会被当成"配过了"参与计算）。"""


class PricingModeError(RuntimeError):
    """结算路径遇到了本版不支持的计价模式（per_call）。显式拒绝，绝不偷套公式。"""


def _today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _date_or_none(value, field: str):
    text = str(value or "").strip()
    if not text:
        return None
    # 日期是护栏与切价日程的比较基准，格式坏了必须当场说，不许留到比较那天
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise PricingError(f"pricing.{field} 要写成 YYYY-MM-DD（收到 {text!r}）")
    return text


def _price_or_none(value, field: str, currency: str):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PricingError(f"pricing.{field}（每百万 token 的{currency}价）得是个数字，"
                           f"收到 {value!r}——宁缺毋滥，不许拿字符串或猜测填价")
    number = float(value)
    if number < 0:
        raise PricingError(f"单价不能是负数（pricing.{field}={number}）——那会把账算反")
    return number


def parse_pricing(raw):
    """把一条 provider 记录里的 pricing 洗成内核认识的标准形状。

    None/缺失/空对象 → None（未定价三态之一，与"配了 0 价"严格不同）。
    token 模式必须两档单价齐全——半套价格会被当成"配过了"参与计算，比没配更危险。
    """
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise PricingError("pricing 得是一个对象（键值对），不是一个值")
    mode = str(raw.get("mode") or "token").strip()
    if mode not in PRICING_MODES:
        raise PricingError("pricing.mode 只许 token 或 per_call"
                           f"（收到 {raw.get('mode')!r}）；按秒/按张这类模式等它进版再来配")
    currency = str(raw.get("currency") or "CNY").strip().upper() or "CNY"
    input_per_m = _price_or_none(raw.get("input_per_m"), "input_per_m", currency)
    output_per_m = _price_or_none(raw.get("output_per_m"), "output_per_m", currency)
    if mode == "token":
        missing = []
        if input_per_m is None:
            missing.append("输入单价 input_per_m")
        if output_per_m is None:
            missing.append("输出单价 output_per_m")
        if missing:
            raise PricingError("token 计价必须同时给出两档单价（每百万 "
                               + currency
                               + " 价），缺：" + "、".join(missing)
                               + "；还没核价就先别配 pricing——未定价会被如实标成算不出")
    return {
        "mode": mode,
        "currency": currency,
        "input_per_m": input_per_m,
        "output_per_m": output_per_m,
        "price_checked_on": _date_or_none(raw.get("price_checked_on"),
                                          "price_checked_on"),
        "free_until": _date_or_none(raw.get("free_until"), "free_until"),
    }


def benchmark() -> dict:
    """基准价（¥→积分的锚）。唯一真相在 config_store，别处不许再抄一份数字。"""
    return config_store.credit_benchmark()


def _mixed_unit_price(input_per_m: float, output_per_m: float, ratio: float) -> float:
    """按典型输入:输出比例折算的"混合 token 单价"（每百万混合 token）。
    ratio=4 即 4:1 —— 每 5 个混合 token 里 4 个按输入价、1 个按输出价。"""
    return (input_per_m * ratio + output_per_m) / (ratio + 1.0)


def _benchmark_mixed() -> float:
    bench = benchmark()
    return _mixed_unit_price(float(bench["input_per_m"]), float(bench["output_per_m"]),
                             float(bench["mixed_input_output_ratio"]))


def is_free_on(pricing, day: str = None) -> bool:
    """`0.00x` 是**有期限的显式免费**（D22）：free_until 当天及以前算免费期，
    到点自动回到真实倍率——日期驱动，不靠人记得改价。"""
    if not pricing or pricing.get("mode") != "token":
        return False
    free_until = pricing.get("free_until")
    return bool(free_until) and (day or _today()) <= free_until


def multiplier_label(pricing, day: str = None):
    """展示用倍率：两位小数 + 一处常量后缀。
    未定价 → `?x`（绝不显示 0.00x，D14）；per_call → None（不显示倍率，D24）；
    免费期 → `0.00x`。注意这只是"选模型时的直觉"，**不是合同价**（见模块头第 2 条）。
    """
    if pricing is None:
        return UNPRICED_LABEL
    if pricing.get("mode") != "token":
        return None
    if is_free_on(pricing, day):
        return f"{0.0:.2f}{MULTIPLIER_SUFFIX}"
    bench_mixed = _benchmark_mixed()
    if bench_mixed <= 0:
        return UNPRICED_LABEL  # 基准价被配坏了：宁可标"算不出"也不给一个错误的数
    bench = benchmark()
    model_mixed = _mixed_unit_price(float(pricing["input_per_m"]),
                                    float(pricing["output_per_m"]),
                                    float(bench["mixed_input_output_ratio"]))
    return f"{model_mixed / bench_mixed:.2f}{MULTIPLIER_SUFFIX}"


def credits_for_usage(pricing, *, prompt_tokens: int = 0, completion_tokens: int = 0):
    """结算/影子口径：**真实 token × 真实单价**（D13），内部保留 6 位小数。

        积分 = (输入/1e6 × 输入价 + 输出/1e6 × 输出价) ÷ 基准混合价 × 1000

    返回 None 只有一种成因：未定价——"算不出"，绝不是 0（D14/验收 14）。
    配了 0 价的免费条目照样算得出（结果可以是 0.0，那是"确认为 0"，与 None 两回事）。
    `per_call` 显式拒绝：不许静默套 token 公式（D24/验收 18）。
    """
    if pricing is None:
        return None
    if pricing.get("mode") != "token":
        raise PricingModeError(
            "该模型是按次计价（per_call）：本版本没有按次结算，也不会把它偷偷按 "
            "token 公式折算成积分。请部署者改为 token 计价，或等按次结算进版。")
    bench = benchmark()
    bench_mixed = _benchmark_mixed()
    if bench_mixed <= 0:
        return None
    cost = (int(prompt_tokens or 0) / 1e6 * float(pricing["input_per_m"])
            + int(completion_tokens or 0) / 1e6 * float(pricing["output_per_m"]))
    return round(cost / bench_mixed * float(bench["tokens_per_credit"]), _CREDITS_DECIMALS)


def credits_for_tokens(pricing_raw, *, prompt: int = 0, completion: int = 0,
                       reasoning: int = 0, cached: int = 0):
    """账行层的换算入口：吃一条 usage 行的四类 token 计数。

    reasoning/cached **不参与加算**，这是口径不是遗漏：reasoning_tokens 含在
    completion_tokens 里（按输出档计价），cached_tokens 含在 prompt_tokens 里
    （不打折）。这个签名收下列名，是为了让"看过口径的人一眼看出没另算一遍"，
    也让判据能构造"同 prompt/completion、不同 reasoning/cached → 积分相同"。
    """
    return credits_for_usage(parse_pricing(pricing_raw),
                             prompt_tokens=prompt, completion_tokens=completion)


def pricing_of(provider_id: str):
    """按 id 现取当前单价（派生显示的"× 当前单价"那半句）。库从 providers 的进程级
    store 读——延迟 import，避免模块环。找不到条目 = 未定价（None），不是报错。"""
    from app.core import providers
    record = providers.store.get(provider_id)
    return (record or {}).get("pricing")


def label_of(provider_id: str) -> str:
    from app.core import providers
    record = providers.store.get(provider_id) or {}
    return record.get("label") or provider_id


def _row_credits(row, day: str = None):
    """一条 usage 行 → (credits|None, status, reason)。status 四态：
    priced/free/unpriced/per_call。None 永远带一句人话 reason，不许空着被当成 0。"""
    pricing = pricing_of(row.get("provider_id") or "")
    if pricing is None:
        return None, "unpriced", "未定价：算不出积分，这不是免费"
    if pricing.get("mode") != "token":
        return None, "per_call", "按次计价本版未实现结算：不会按 token 公式偷算"
    credits = credits_for_usage(pricing,
                                prompt_tokens=row.get("prompt_tokens", 0),
                                completion_tokens=row.get("completion_tokens", 0))
    if is_free_on(pricing, day):
        return credits, "free", "免费期（0.00x）：影子照常计算，未向用户扣费"
    return credits, "priced", ""


def user_shadow_day(user_id: str, day: str = None) -> dict:
    """某用户某日的影子合计：{"total", "free_total", "gaps"}。

    total = 所有**算得出**的影子积分之和（6 位精度）；算不出的行进 gaps（带 provider_id、
    credits=None 与人话 reason），**绝不进 total、绝不按 0 溜过去**。
    free_total = 其中处于免费期的部分——「免费期已为你垫付 N 积分」的数据源（D23）。
    """
    total = 0.0
    free_total = 0.0
    gaps = []
    for row in usage.snapshot(day):
        if row.get("user_id") != user_id:
            continue
        credits, status, reason = _row_credits(row, day)
        if credits is None:
            gaps.append({"provider_id": row.get("provider_id"),
                         "status": status, "credits": None, "reason": reason})
            continue
        total += credits
        if status == "free":
            free_total += credits
    return {"total": round(total, _CREDITS_DECIMALS),
            "free_total": round(free_total, _CREDITS_DECIMALS),
            "gaps": gaps}


def shadow_daily_limit() -> float:
    """每日影子护栏（T5.20 的新配置项）。默认 0 = 不拦截——本批口径是
    「算得出、看得见、拦得住（默认关）」：部署者填正数即开，与 env
    CREDIT_SHADOW_DAILY_LIMIT > data/config.json > 默认 0 的既有三层同套路。"""
    return config_store.credit_shadow_daily_limit()


def shadow_limit_exceeded(user_id: str) -> bool:
    """影子是否越线——`before_round()` 的一个判据、请求入口前置判据的同一个问题：
    「这一轮/这一条请求该不该花钱出网」。护栏关着（默认 0）永远 False。"""
    limit = shadow_daily_limit()
    if limit <= 0:
        return False
    return user_shadow_day(user_id)["total"] >= limit


def shadow_breakdown(day: str = None) -> dict:
    """管理端读面（D23 的看板数据源）：某日影子合计，按 provider 分组。
    界面语义是「免费期已垫付 N 积分 ≈ ¥M」——那是文案，这一层只给可核对的数：
    total/fronted/approx_cost/缺口清单。派生即算即用：改了单价再看此视图会重算
    （卡片 §2.5：派生显示可重算；本批没有落库的扣款，所以没有"追改历史"问题）。
    """
    rows = usage.snapshot(day)
    per_provider = {}
    for row in rows:
        pid = row.get("provider_id") or "unknown-provider"
        bucket = per_provider.setdefault(pid, {"provider_id": pid, "calls": 0,
                                               "prompt_tokens": 0, "completion_tokens": 0,
                                               "unknown_usage": 0})
        bucket["calls"] += row.get("calls", 0)
        bucket["prompt_tokens"] += row.get("prompt_tokens", 0)
        bucket["completion_tokens"] += row.get("completion_tokens", 0)
        bucket["unknown_usage"] += row.get("unknown_usage", 0)
    out_rows = []
    total = 0.0
    fronted = 0.0
    gaps = 0
    for pid, bucket in sorted(per_provider.items()):
        credits, status, reason = _row_credits({**bucket, "provider_id": pid}, day)
        if credits is not None:
            total += credits
            if status == "free":
                fronted += credits
        else:
            gaps += 1
        out_rows.append({"provider_id": pid, "label": label_of(pid),
                         "calls": bucket["calls"],
                         "prompt_tokens": bucket["prompt_tokens"],
                         "completion_tokens": bucket["completion_tokens"],
                         "unknown_usage": bucket["unknown_usage"],
                         "credits": credits, "status": status, "reason": reason})
    bench = benchmark()
    bench_mixed = _benchmark_mixed()
    per_credit_cost = (bench_mixed / float(bench["tokens_per_credit"])
                       if bench_mixed > 0 else 0.0)
    return {"day": day or _today(),
            "limit": shadow_daily_limit(),
            "rows": out_rows,
            "total_credits": round(total, _CREDITS_DECIMALS),
            "fronted_credits": round(fronted, _CREDITS_DECIMALS),
            "gap_providers": gaps,
            "approx_cost": round(total * per_credit_cost, _CREDITS_DECIMALS),
            "currency": bench["currency"]}


def _fmt_price(pricing, field: str) -> str:
    if not pricing or pricing.get(field) is None:
        return "—"
    return f"{pricing[field]:g}"


def describe_pricing_change(old, new) -> str:
    """审计 detail：一句人能读懂的差值话（谁改的由 audit 的 actor 字段管）。
    这是给事后排"成本怎么变了"的人看的，逐档点名改前→改后。"""
    if old is None and new is None:
        return "无变化"
    if old is None:
        return (f"新增定价：输入 {_fmt_price(new, 'input_per_m')}、"
                f"输出 {_fmt_price(new, 'output_per_m')}/百万 "
                f"{(new or {}).get('currency', '')}")
    if new is None:
        return (f"移除定价：原 输入 {_fmt_price(old, 'input_per_m')}、"
                f"输出 {_fmt_price(old, 'output_per_m')}/百万 {old.get('currency', '')}")
    bits = []
    for field, name in (("input_per_m", "输入单价"), ("output_per_m", "输出单价")):
        if old.get(field) != new.get(field):
            o, n = old.get(field), new.get(field)
            if isinstance(o, (int, float)) and isinstance(n, (int, float)):
                bits.append(f"{name} {o}→{n}（差 {n - o:+}）")
            else:
                bits.append(f"{name} {o}→{n}")
    if old.get("currency") != new.get("currency"):
        bits.append(f"币种 {old.get('currency')}→{new.get('currency')}")
    if old.get("free_until") != new.get("free_until"):
        bits.append(f"免费截止 {old.get('free_until') or '无'}→{new.get('free_until') or '无'}")
    if old.get("mode") != new.get("mode"):
        bits.append(f"计价模式 {old.get('mode')}→{new.get('mode')}")
    return "；".join(bits) or "元数据变化（核对日期等）"
