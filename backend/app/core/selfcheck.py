"""一次「东西齐不齐」的自检：把只有出事那天才看得见的东西，摆到 /health 上。

`/health` 一直只回 `{"status": "healthy"}`，量的是「进程还在」。这一轮新加的每样东西
偏偏都能在这个状态下坏：密钥库文件读不动（聊天全 401）、用量账本写不进去（钱白花）、
嵌入后端降级成全零伪嵌入（语义检索再也不可信，但不报错）、新写的限流账没登记进
`_LEDGERS`（于是永不修剪，内存单向涨）。这些都是"活着但不齐"，所以逐项问一遍。

两件刻意不做的事：

① **不因为哪一项坏了就返回非 200。** 那会让"配错了密钥"变成"看门狗每分钟重启一次
   后端"——重启修不好任何配置问题，只会把正在进行的对话一起带走。判据交给日志与
   运维，不交给进程管理器。
② **不回显绝对路径、不读任何用户内容，也不回显模型服务配置的内容。**
   `/health` 是免鉴权端点（见 `backend/tests/test_route_auth_contract.py` 的
   EXEMPT_PATHS），公开进程目录结构与用户名对探针没有用处，只多一个泄露面。
   label 与 model 同理：管理员给配置起的名字和上游模型标识，是"我们用的是哪家"
   这种项目别处当秘密的信息（memory_manager.py 对嵌入后端是同一口径）。
   所以这里只有状态、一句原因，和几个数字。**具体是哪个 provider 坏了，走日志**
   ——见 `log_startup_summary`，它只往 server.log 写，不出公网。
③ **模型名在 web 出口还有一道兜底掩码（#121）。** 源头那句"不回显 label/model"
   只覆盖我们自己写的文案；`memory_init_error` 是第三方异常原文（OpenAI 的
   404 会写 `model text-embedding-3-small`），措辞不在我们手里。所以 `run()`
   把详情再过 `_public_scrub`：已知模型名（provider 配置、`EMBEDDINGS_MODEL`、
   运行中的嵌入后端）精确替换，三段以上连字符/下划线标识符按形状兜底，掩码是
   明写的 `〈模型名已隐去〉`，句子其余部分照读。
   这道网**只挂在 web 出口**，不进 `_scrub`：同一个 `_scrub` 还喂
   `log_startup_summary`，而 server.log 里"现在用的是哪个模型/坏在哪一条"恰恰
   是运维要看的（与 #112 拒绝把数据根登记进 logsanitizer 是同一个决定：出口
   掩码不许把日志里那些可定位的行一起吞掉）。日志那条路继续只掩凭据与路径。
"""

import json
import os
import re

from app.core.pathmask import hide_paths

OK = "ok"
DEGRADED = "degraded"
NOT_CONFIGURED = "not_configured"
BROKEN = "broken"

# not_configured 与 degraded 同级：它不是"东西坏了"，但记忆确实不可用，不能当 ok
# 放过去（那会让只有这一项时 /health 回 ok、把整个功能的缺失藏进绿灯）。看门狗对
# 非 broken 的非 ok 状态一律记 WARN、不动重启预算。它仍低于 broken，真故障不会被盖住。
_SEVERITY = {OK: 0, DEGRADED: 1, NOT_CONFIGURED: 1, BROKEN: 2}

# 云端嵌入的启用判据（与 memory_manager 同一句）：密钥、地址、模型名**三者齐全**
# 才算已配置。v0.36.2 起密钥有两个来源——`EMBEDDINGS_PROVIDER_ID` 指向的 provider
# 里那条非占位密钥（单源正道），或旧部署留在 .env 的 `api_key`（过渡回退）；
# 两者都拿不到才算缺密钥这一件。
# 旧写法是 `not any(...)`：三键全缺才为真，于是"只有 api_key、没有地址和模型名"
# 这种半配状态会被说成"已配置"——memory_manager 明明走了本地/伪嵌入分支，自检却
# 不豁免它的维度冲突，口径与实现漂移。现在与实现共用同一条"齐全"判据。
_EMBED_ENV_KEYS = ("api_key", "EMBEDDINGS_BASE_URL", "EMBEDDINGS_MODEL",
                   "EMBEDDINGS_PROVIDER_ID")


def _embeddings_key_present() -> bool:
    """嵌入密钥到底有没有：provider 单源优先，回落 .env api_key（过渡期兼容）。"""
    pid = (os.getenv("EMBEDDINGS_PROVIDER_ID") or "").strip()
    if pid:
        try:
            from app.core.providers import looks_placeholder
            from app.core.providers import store as provider_store
            record = provider_store.get(pid)
            key = str((record or {}).get("api_key") or "").strip()
            if key and not looks_placeholder(key):
                return True
        except Exception:
            # 判据读不动时不许把自检本身弄崩：落到 env 那一档再看。
            pass
    return bool((os.getenv("api_key") or "").strip())


def _embeddings_unconfigured() -> bool:
    """三件（密钥来源 / EMBEDDINGS_BASE_URL / EMBEDDINGS_MODEL）缺任意一项——
    memory_manager 会落到本地/伪嵌入的那条路。"""
    return not (_embeddings_key_present()
                and (os.getenv("EMBEDDINGS_BASE_URL") or "").strip()
                and (os.getenv("EMBEDDINGS_MODEL") or "").strip())


def _is_unconfigured_dim_conflict(error) -> bool:
    """区分"没配嵌入"与"真的坏了"，判据只有两条，且都不看文案。

    `dim_conflict` 由 memory_manager 的专用异常挂着：字符串匹配在这里不算数——
    异常原文是第三方写的、措辞会变，而"这次失败是不是配置缺失"本来就不在文案里。
    嵌入判「未配置」（密钥/地址/模型名三者不齐，见 `_embeddings_unconfigured`）
    才可能是"这台机子本来就没打算用云端嵌入"：伪嵌入固定 384 维，撞上 1536 维
    旧库是预期形状。反过来，磁盘 OSError 哪怕嵌入配置一个都没配也仍是 broken
    ——缺配置不是它发生的原因，不能把一次真故障洗成一条配置提示。
    """
    return bool(getattr(error, "dim_conflict", False)) and _embeddings_unconfigured()


def _check_provider_config() -> tuple:
    """有没有一个真能拿去调用的模型配置。resolve() 就是聊天时会走的那条判断。

    详情里**只有条数，没有 label 也没有 model**，这条不是洁癖：
    `/health` 在 EXEMPT_PATHS 里免鉴权，而 label 是管理员自己给配置起的名字
    （"公司网关-主""备用那套"）、model 是上游模型标识——"我们用的是哪家"本身就是
    项目在别处当秘密的信息（app/memory/memory_manager.py 里对嵌入后端就是同一口径：
    这个仓库是公开的，"哪家在替我们做嵌入"本身就是不想公开的信息）。
    出口脱敏（_scrub）只挡 key 的形状，挡不住一个正常的模型名，所以这里只能不回。

    要看具体是哪一条坏了，看日志：`log_startup_summary` 走的是另一条路，
    它把 label/model 印进 server.log，不出公网。
    """
    from app.core.providers import ProviderError, store as provider_store

    try:
        provider_store.resolve()
    except ProviderError:
        # ProviderError 原文描述的是配置形状（"还没配任何模型服务" / "N 条都缺密钥"
        # / "key 还是占位符"），回显它等于把管理员的密钥库现状播给路人。归成一句话。
        return BROKEN, "没有可用的模型服务（详见服务端日志）"
    except Exception as e:
        return BROKEN, f"读取模型服务配置时出错：{type(e).__name__}"
    try:
        total = len(provider_store.all())
    except Exception:
        total = 0
    return OK, f"已配置 {total} 条"


def _check_key_vault() -> tuple:
    """密钥库文件与内存里那份对不对得上。

    只比条数，不比内容：内容对不上是"哪条密钥被掉了"，条数对不上是"有一边在悄悄丢"。
    后者才是这里要抓的形状——记录搬走了密钥、密钥文件却没落盘，界面上一切正常。
    """
    from app.core.providers import store as provider_store

    records = provider_store.all()
    in_memory = sum(1 for p in records if p.get("api_key"))
    path = provider_store.keys_path
    if not os.path.isfile(path):
        if in_memory:
            return BROKEN, f"{in_memory} 条配置自报带密钥，密钥库文件却不在"
        return DEGRADED, "还没配过任何密钥"
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return BROKEN, "密钥库文件读不动或不是合法 JSON"
    if not isinstance(data, dict):
        return BROKEN, "密钥库形状不是 dict"
    on_disk = sum(1 for v in data.values() if v)
    if on_disk != in_memory:
        return BROKEN, f"密钥库 {on_disk} 条、配置里 {in_memory} 条，有一边在丢密钥"
    return OK, f"{on_disk} 条"


def _check_usage_ledger() -> tuple:
    """账本读得动吗。写坏了的话这一轮的钱就不在任何人的账上了。"""
    from app.core import usage

    rows = usage.snapshot()
    days = usage.days()
    if not isinstance(rows, list) or not isinstance(days, list):
        return BROKEN, "账本接口返回了意外的形状"
    return OK, f"已记 {len(days)} 天"


def _check_memory_backend() -> tuple:
    """嵌入后端是哪一种。伪嵌入必须在这里露出来。

    伪嵌入本身是 degraded 而不是 broken：全零向量下 add/query 都不报错，
    只是检索结果完全不可信——它不影响"能不能聊"，影响的是"记不记得住"，
    所以报 degrade、不报断。

    后端根本没起来时再分两路：维度冲突 + 嵌入判为未配置（密钥/地址/模型名三者
    不齐）= not_configured（机器本来就没打算用云端嵌入，伪嵌入撞上旧库是预期
    形状），其余一律 broken。
    """
    from app.memory.memory_router import memory_init_error, memory_manager

    if memory_manager is None:
        if _is_unconfigured_dim_conflict(memory_init_error):
            # 只报"没配"，不回显异常里的维度/库信息：那是一条配置提示，不是故障现场。
            return NOT_CONFIGURED, ("未配置嵌入后端（嵌入密钥 / 嵌入地址 / 嵌入模型名 "
                                    "缺任意一项），记忆检索不可用")
        return BROKEN, f"记忆后端没起来：{memory_init_error or '未知原因'}"
    if hasattr(memory_manager, "memories"):
        # 测试/CI 的内存替身：本来就不做嵌入，这不是降级，是这套后端的设计。
        # 不认出来的话 CI 每次启动都白报一条⚠️，而习惯了自己说谎的告警等于没有告警。
        return OK, "内存替身（测试/CI）"
    if getattr(memory_manager, "_dummy_embed", False):
        return DEGRADED, "伪嵌入（全零向量），语义检索不可信"
    if getattr(memory_manager, "embed_model", None) == "injected":
        return OK, "测试注入的确定性向量"
    if getattr(memory_manager, "use_local_embed", False):
        return OK, "本地嵌入"
    if getattr(memory_manager, "client", None) is not None:
        return OK, "云端嵌入"
    return DEGRADED, "说不清用的是哪种嵌入后端"


def _check_limiters() -> tuple:
    """每本限流账都登记了吗——没登记的那本永远不会被修剪。

    `_prune` 只遍历 `_LEDGERS`。新写一本账忘了注册，结果是这个进程的内存随来源数
    单向上涨，而且限流本身照样生效，所以没有任何症状。这里数一下本数、并确认每本
    都带着自己的窗口长度。
    """
    from app.core import auth_router

    ledgers = auth_router._LEDGERS
    if not ledgers:
        return BROKEN, "一本限流账都没登记"
    malformed = [i for i, item in enumerate(ledgers)
                 if not (isinstance(item, tuple) and len(item) == 2
                         and isinstance(item[1], (int, float)) and item[1] > 0)]
    if malformed:
        return BROKEN, f"第 {malformed} 本账没有合法的窗口长度，_prune 会按 0 处理"
    return OK, f"{len(ledgers)} 本"


def _check_data_paths() -> tuple:
    """每份可变数据的实际落点，父目录在不在。

    这条是 PyInstaller 那次教训的形状：打包版的数据原先落在 EXE 同级目录，而 `dist/`
    每次重建都被整体删空——一次构建抹光长期记忆。落点从此由 paths 单源决定，但"到底
    落在哪、还在不在"只能问当下这个进程，所以把它打印出来。
    """
    from app.core.paths import resolve_all_data_paths

    resolved = resolve_all_data_paths()
    missing = [label for label, path in resolved
               if not os.path.isdir(os.path.dirname(os.path.abspath(path)))]
    if missing:
        return BROKEN, f"这几份数据所在目录不存在：{'、'.join(missing)}"
    return OK, f"{len(resolved)} 处可写"


CHECKS = (
    ("模型服务", _check_provider_config),
    ("密钥库", _check_key_vault),
    ("用量账本", _check_usage_ledger),
    ("记忆后端", _check_memory_backend),
    ("限流账", _check_limiters),
    ("数据落点", _check_data_paths),
)


def _scrub(detail: str) -> str:
    """出口脱敏。它自己读不动时宁可整条丢掉。

    `/health` 免鉴权，而"密钥库读不动"恰恰是本文件要报的那类故障——脱敏依赖
    密钥库，于是它可能正好在报错的那一刻坏掉。这时候把详情原样发出去是失败开放，
    把详情换成一句"说不清"是失败封闭：宁可少说，不可把别人的 key 说给路人听。

    路径这一层为什么也要在出口补一道：详情里最不可控的一句是
    `memory_init_error`，那是第三方异常原文（chroma 读盘失败会把库路径写进
    OSError 的 message），不是我们能逐字改写的文案。`scrub_secrets` 挡的是凭据
    形状，路径既不是凭据也不在它的模式里，于是一句绝对路径能原样走到公开端点上。
    掩码的判据与边界收在 `core.pathmask`，那张网同时罩着免鉴权的更新通道两条端点
    （`/v1/update/info` 的 502 detail 与 `/v1/release/latest` 的 `reason`）。

    为什么换成一个明写的掩码而不是悄悄删掉，也不用 logsanitizer 登记数据根：
    删掉会让"这句话为什么少了一截"没人说得清；登记进 logsanitizer 则是把同一段
    文字在 **server.log 里**一起打码，而运维查"数据到底落在哪"恰恰要看那一句
    （见 memory_manager 初始化成功那行与 paths 的启动摘要）。这张网的实际射程比
    "只管响应"宽：本模块是先 `_scrub` 再打印，所以**自己那行启动摘要也被掩**。不经
    这道网的是 `paths`／`memory_manager`／`apk_cache` 那几行日志——`apk_cache` 的
    `_private_where` 明写刻意不走出口脱敏。
    """
    from app.core.providers import scrub_secrets

    try:
        return hide_paths(scrub_secrets(detail))
    except Exception:
        return "（详情无法安全呈现）"


# 模型名的明写掩码。与路径掩码同一个口径：只盖那一段，句子其余部分照读。
MODEL_MASK = "〈模型名已隐去〉"
# 模型名的形状：连字符/下划线分隔的三段以上 ASCII 标识符（text-embedding-3-small、
# all-MiniLM-L6-v2 都是这一族）。单段或两段（user_memories、qwen-max）不按形状认——
# 那会把集合名和普通标识符一起吃掉；配置里的短名走下面那一档精确匹配。
_MODEL_SHAPED = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+){2,}", re.IGNORECASE)


def _known_model_names() -> list:
    """这台机器可能出现在第三方异常里的模型名，长的优先。

    三个来源都要问：provider 配置里的 model、嵌入配置的 `EMBEDDINGS_MODEL`、以及
    已经起来的记忆后端自报的 `embed_model`。只认长度 >= 4 的串（`m` 这种一字母
    别名会把详情里的普通字母一起吃掉，且它本身指认不出任何厂商）。
    任何一源读不动都只跳过它：这道网是纵深防御，不许因为配置坏了把详情变成 500。
    """
    names = set()
    env_model = (os.getenv("EMBEDDINGS_MODEL") or "").strip()
    if env_model:
        names.add(env_model)
    try:
        from app.core.providers import store as provider_store
        for record in provider_store.all():
            if isinstance(record, dict):
                model = str(record.get("model") or "").strip()
                if model:
                    names.add(model)
    except Exception:
        pass
    try:
        from app.memory.memory_router import memory_manager
        model = str(getattr(memory_manager, "embed_model", "") or "").strip()
        if model:
            names.add(model)
    except Exception:
        pass
    return sorted((name for name in names if len(name) >= 4), key=len, reverse=True)


def _public_scrub(detail: str) -> str:
    """web 出口的第三层：先凭据与路径，再把模型名换成明写掩码（#121）。

    只在 `run()` 里用，`log_startup_summary` 不走这里（理由见模块 docstring ③）。
    已知名精确替换放在形状兜底之前：`qwen-max` 这类两段式只有精确档认得出；
    形状档接住的是"配置里没登记、但一看就是模型名"的第三方原文。
    """
    text = _scrub(detail)
    if not text:
        return text
    try:
        for name in _known_model_names():
            if name in text:
                text = text.replace(name, MODEL_MASK)
        return _MODEL_SHAPED.sub(MODEL_MASK, text)
    except Exception:
        return "（详情无法安全呈现）"


def _evaluate() -> dict:
    """逐项自检的原始结果（详情尚未经过 `_scrub`，更没有 web 模型名掩码）。

    分成两层是为了 #121 那个决定：web 出口要加模型名掩码，日志出口不加。
    混在一个 `run()` 里，server.log 的启动摘要就会跟着丢掉模型名——那是运维
    排"哪一条 provider/嵌入后端坏了"唯一的定位线索（同一个理由见 #112）。
    """
    checks = {}
    worst = OK
    for label, fn in CHECKS:
        try:
            status, detail = fn()
        except Exception as e:                       # 自检本身坏了也要看得见
            status, detail = BROKEN, f"自检项异常：{type(e).__name__}"
        checks[label] = {"status": status, "detail": detail}
        if _SEVERITY[status] > _SEVERITY[worst]:
            worst = status
    return {"status": worst, "checks": checks}


def run() -> dict:
    """逐项自检，返回 **web 出口** 的那一份：详情过 `_public_scrub`。

    任何一项抛异常都折算成 broken，绝不把异常透出到 /health 上。
    """
    result = _evaluate()
    return {
        "status": result["status"],
        "checks": {
            label: {"status": item["status"], "detail": _public_scrub(item["detail"])}
            for label, item in result["checks"].items()
        },
    }


def log_startup_summary() -> None:
    """启动时打一行，让 server.log 里留一份"开机时齐不齐"的证据。

    只在 /health 上给结论不够：出事的时候人多半在看日志，而不是去猜当时 /health
    回过什么。

    而"是哪个 provider 坏了"恰恰是 _check_provider_config 在 /health 上不再说的事
    （那里只回条数，见该函数的注释）。所以模型服务这一项在这里**单独再问一遍**，
    把 label/model 与 ProviderError 原文补进日志——日志只进 server.log，
    不出公网，正是该放这些细节的地方。少了这一段，运维就只剩"没配"三个字，
    还得自己去翻 providers.json 才知道是哪一条、缺的是密钥还是占位符。
    """
    from app.core.providers import ProviderError, store as provider_store

    # 走 `_evaluate` 而不是 `run()`：日志走日志口径（`_scrub`），不加 web 的
    # 模型名掩码——server.log 里"是哪一个模型/哪一条配置坏了"必须还读得出来。
    result = _evaluate()
    bad = {k: {"status": v["status"], "detail": _scrub(v["detail"])}
           for k, v in result["checks"].items() if v["status"] != OK}
    if bad and "模型服务" in result["checks"]:
        try:
            provider = provider_store.resolve()
            detail = f"{provider.get('label') or provider.get('id')} / {provider.get('model')}"
        except ProviderError as e:
            detail = f"没有可用的模型服务：{e}"
        except Exception as e:
            detail = f"读取模型服务配置时出错：{type(e).__name__}: {e}"
        bad["模型服务"] = {"status": result["checks"]["模型服务"]["status"],
                           "detail": _scrub(detail)}
    if not bad:
        print("✅ 自检通过：模型服务、密钥库、账本、记忆、限流账、数据落点都齐")
        return
    icon = "🚨" if result["status"] == BROKEN else "⚠️"
    print(f"{icon} 自检 {result['status']}：" +
          "；".join(f"{k}={v['status']}({v['detail']})" for k, v in bad.items()))
