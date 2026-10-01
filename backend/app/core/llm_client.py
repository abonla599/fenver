"""LLM 客户端 - 统一的非流式模型调用接口。

模型配置一律取自 app.core.providers。此前本模块自带一份 MODEL_CONFIGS，与
streaming 里的那份重复且互不同步，是"界面模型与实际调用不一致"的根源。

这里也是**智能体侧唯一叫模型的门**，所以 v0.25 R1 起它同时承担记账：
`/v1/agent/*`、编排任务里的每一次调用都在统一账本（app.core.usage）落一行，
否则普通用户拿到任务能力的那天，花钱的入口就又成了两条腿——一条记账、
一条不记。判据在 tests/test_v025_task_ownership_contract.py。
"""
from typing import List, Dict, Optional, Any

from app.core import usage
from app.core.providers import store, build_client


def get_llm_response(
    model: str,
    messages: List[Dict[str, Any]],
    temperature: float = 0.7,
    tools: Optional[List[Dict]] = None,
    provider_id: str = None,
    user_id: str = None,
) -> str:
    """统一 LLM 调用。

    失败时抛出异常而不是返回错误文本：把故障当正常回复返回，会让错误被写进
    会话历史、并被上层当作模型输出继续加工。

    `user_id` 是给 resolve 的归属闸门，也是账本上那一人：带了它，选路只在
    "共享 + 本人私有"的池子里做，默认模型按**这个人**的偏好解析（providers
    .default_for）——全局默认不许顶替用户自己的选择。记账与 pipeline 那条
    同一个口径：数取上游回传的 usage，上游没回就 0 并留 unknown_usage，
    抛出去的异常也要落一行 failed。
    """
    provider = store.resolve(provider_id, legacy_model=model, user_id=user_id)
    client = build_client(provider)

    params: Dict[str, Any] = {
        "model": provider["model"],
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        params["tools"] = tools

    billed = {k: 0 for k in ("prompt_tokens", "completion_tokens",
                             "total_tokens", "reasoning_tokens", "cached_tokens")}
    ok = True
    try:
        response = client.chat.completions.create(**params)
        u = getattr(response, "usage", None)
        if u is not None:
            billed["prompt_tokens"] = getattr(u, "prompt_tokens", 0) or 0
            billed["completion_tokens"] = getattr(u, "completion_tokens", 0) or 0
            billed["total_tokens"] = getattr(u, "total_tokens", 0) or 0
            details = getattr(u, "completion_tokens_details", None)
            billed["reasoning_tokens"] = (getattr(details, "reasoning_tokens", 0) or 0) if details else 0
            pdetails = getattr(u, "prompt_tokens_details", None)
            billed["cached_tokens"] = (getattr(pdetails, "cached_tokens", 0) or 0) if pdetails else 0
        return response.choices[0].message.content
    except BaseException:
        ok = False
        raise
    finally:
        usage.record_call(user_id=(user_id or "").strip() or "unattributed",
                          provider_id=provider["id"],
                          paid_by=provider.get("paid_by") or "operator",
                          ok=ok, **billed)
