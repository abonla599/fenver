"""过程帧契约 —— 思考过程 / 工具调用 / 搜索网页的**唯一真源**。

为什么要单独一个模块：帧的形状以前只以字面量散在 main.py 的 generate() 里，
客户端两边各抄一份（api.js 的 if 链、Api.kt 的 when 臂）。加一种帧就要同时改
四处，漏一处不是「客户端看不见」就是「老客户端解析崩」。这里把类型名、字段、
预算、构造和序列化收在一处，客户端契约测试也从这个模块读真值，不再各写一份。

⚠️ 序列化口径不能改：json.dumps 用默认分隔符与默认 ensure_ascii。
tests/test_stream_api.py 锁的是 `'"type": "error"' in res.text`（冒号后有空格），
而且转义非 ASCII 才能保证帧内永远不出现裸换行——SSE 的分帧靠的就是换行。
"""
import json
from typing import Any, Dict, List, Optional

# ---------- 帧类型 ----------
# 前三条是本次新增；start/content/done/error 是既有契约，字段一律不动。
FRAME_START = "start"
FRAME_THINKING = "thinking"
FRAME_TOOL_CALL = "tool_call"
FRAME_TOOL_RESULT = "tool_result"
FRAME_SEARCH = "search"
FRAME_CONTENT = "content"
FRAME_DONE = "done"
FRAME_ERROR = "error"

CLIENT_FRAMES = (FRAME_START, FRAME_THINKING, FRAME_TOOL_CALL, FRAME_TOOL_RESULT,
                 FRAME_SEARCH, FRAME_CONTENT, FRAME_DONE, FRAME_ERROR)

# ---------- 预算 ----------
# 过程帧是给「人看着它一步步推进」用的，不是给模型复述用的，两头都要封顶：
# 上游 token 一个一个来，不合并就会一秒钟发几十帧把连接刷爆；而思考全文和工具
# 输出动辄上万字，全存进会话历史会让 sessions.json 无界增长。
THINKING_FLUSH_CHARS = 120        # 攒够这么多字再发一帧
THINKING_FLUSH_SECONDS = 0.4      # 或者过了这么久没发过，先 flush 让人看到进展
THINKING_TOTAL_MAX = 8000         # 单轮思考文本对外展示的上限
TOOL_NAME_MAX = 64
TOOL_ARGS_MAX = 600               #  arguments 原样进帧的字符上限
TOOL_LABEL_MAX = 160              #  label 是一行收起态文案，服务端算，两个客户端不各排一次
TOOL_SUMMARY_MAX = 500
SEARCH_RESULTS_MAX = 8
SEARCH_QUERY_MAX = 200
SEARCH_TITLE_MAX = 200
SEARCH_SNIPPET_MAX = 300
SEARCH_URL_MAX = 500

# 落盘用的整份过程留痕上限：一条回答最多 40 步、12000 字。
# 超了丢中间步、保首尾——用户回看时想知道的是「它查了什么、算没算成」，
# 而会话历史是无凭据也能一直涨的磁盘文件。
TRACE_STEPS_MAX = 40
TRACE_CHARS_MAX = 12000

_URL_SAFE_SCHEMES = ("http://", "https://")


def _cap(value: Any, limit: int) -> str:
    """转成字符串并按**字符**封顶，超出补一个省略号。

    按字符不按字节：这里的预算管的是人一眼能看到多少，而帧里全是中文。
    """
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


def _int_field(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _safe_url(value: Any) -> str:
    """只放行 http/https。

    搜索结果的 URL 出自别人写的网页。`javascript:` 或 `data:` 一旦被客户端当成
    链接，就等于把第三方内容升格成了可执行入口；这一层在服务端先挡掉，两个客户端
    少犯一次错的概率都比「各自记得校验」高。
    """
    url = _cap((value or "").strip(), SEARCH_URL_MAX)
    if url.startswith(_URL_SAFE_SCHEMES):
        return url
    return ""


def thinking_frame(text: str) -> Dict[str, Any]:
    return {"type": FRAME_THINKING, "text": _cap(text, THINKING_FLUSH_CHARS * 2)}


def tool_call_frame(call_id: str, name: str, arguments: Any,
                    label: str = "") -> Dict[str, Any]:
    """工具开始执行前的一帧：让用户看到「它现在要去算什么」。

    `arguments` 尽量给结构化的键值对象（客户端按行渲染最好看）；模型给出的参数
    不是合法 JSON 时退回 `arguments_text`——那一帧仍然有价值，因为它说明工具被
    调了、参数长什么样，而「参数不是 JSON」本身就是接下来失败的原因。
    """
    frame: Dict[str, Any] = {
        "type": FRAME_TOOL_CALL,
        "id": _cap(call_id, 64),
        "name": _cap(name, TOOL_NAME_MAX),
        "label": _cap(label or name, TOOL_LABEL_MAX),
    }
    if isinstance(arguments, dict):
        slim: Dict[str, Any] = {}
        for key, val in list(arguments.items())[:12]:
            slim[_cap(key, 40)] = _cap(val, TOOL_ARGS_MAX // 4)
        frame["arguments"] = slim
    elif arguments is not None:
        frame["arguments_text"] = _cap(arguments, TOOL_ARGS_MAX)
    return frame


def tool_result_frame(call_id: str, name: str, ok: bool, summary: str,
                      elapsed_ms: int = 0, truncated: bool = False) -> Dict[str, Any]:
    return {
        "type": FRAME_TOOL_RESULT,
        "id": _cap(call_id, 64),
        "name": _cap(name, TOOL_NAME_MAX),
        "ok": bool(ok),
        "summary": _cap(summary, TOOL_SUMMARY_MAX),
        "elapsed_ms": _int_field(elapsed_ms),
        "truncated": bool(truncated),
    }


def search_frame(call_id: str, query: str,
                 results: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """把一次搜索命中的网页原样交给客户端。

    以前这些 {title,url,snippet} 只在 app/tools/web_search.py 的源头存在，
    builtin_tools 把它压成一段给模型看的文本之后就丢了——于是「它到底搜了什么网页」
    这件事用户永远看不见，也没法核对来源。这一帧就是为了把 URL 还给人。
    """
    rows: List[Dict[str, Any]] = []
    for item in (results or [])[:SEARCH_RESULTS_MAX]:
        if not isinstance(item, dict):
            continue
        url = _safe_url(item.get("url"))
        row = {"title": _cap(item.get("title"), SEARCH_TITLE_MAX),
               "url": url,
               "snippet": _cap(item.get("snippet"), SEARCH_SNIPPET_MAX)}
        if not row["title"] and not url:
            continue
        rows.append(row)
    return {
        "type": FRAME_SEARCH,
        "id": _cap(call_id, 64),
        "query": _cap(query, SEARCH_QUERY_MAX),
        "results": rows,
    }


def content_frame(text: str) -> Dict[str, Any]:
    return {"type": FRAME_CONTENT, "text": text}


def start_frame(message_id: str, model: str) -> Dict[str, Any]:
    return {"type": FRAME_START, "message_id": message_id, "model": model}


def done_frame(full_text: str, message_id: str, model: str,
               trace: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """done 多带一个可选 trace：既有字段一个不动，老客户端照旧忽略新键。

    带上它是因为「过程」只有在客户端中途断掉之后还有用，而客户端只能从 done
    拿回整份留痕，否则刷新一次就变成了一段没有来历的正文。
    """
    frame: Dict[str, Any] = {"type": FRAME_DONE, "full_text": full_text,
                             "message_id": message_id, "model": model}
    if trace:
        frame["trace"] = trace
    return frame


def error_frame(message: str) -> Dict[str, Any]:
    return {"type": FRAME_ERROR, "message": message}


# ---------- 落盘形状 ----------
# 会话里存的是「步」而不是「帧」：帧是流上的瞬时协议（thinking 一段一段），
# 步是人回看时的单位（这一轮总共想了什么、调了哪几个工具、搜到哪些网页）。
def step_thinking(text: str) -> Dict[str, Any]:
    return {"kind": "thinking", "text": _cap(text, THINKING_TOTAL_MAX)}


def step_tool(call_id: str, name: str, label: str, ok: bool, summary: str,
              elapsed_ms: int = 0, truncated: bool = False) -> Dict[str, Any]:
    return {"kind": "tool", "id": _cap(call_id, 64), "name": _cap(name, TOOL_NAME_MAX),
            "label": _cap(label, TOOL_LABEL_MAX), "ok": bool(ok),
            "summary": _cap(summary, TOOL_SUMMARY_MAX),
            "elapsed_ms": _int_field(elapsed_ms), "truncated": bool(truncated)}


def step_search(call_id: str, query: str,
                results: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    frame = search_frame(call_id, query, results)
    return {"kind": "search", "id": frame["id"], "query": frame["query"],
            "results": frame["results"]}


def _trace_chars(steps: List[Dict[str, Any]]) -> int:
    """trace 的代价按「人真正会读到的字数」算，不按 JSON 序列化长度算。

    序列化那一版不能当预算：ensure_ascii 把每个中文膨胀成 6 个字符，同一个预算
    数字下「4000 字思考」会被当成 24000 直接丢掉——于是这条 trace 里最先消失的
    恰好是用户最想看的部分。按字符串叶子求和才是这里想管的量。
    """
    def _walk(node: Any) -> int:
        if isinstance(node, str):
            return len(node)
        if isinstance(node, dict):
            return sum(_walk(v) for v in node.values())
        if isinstance(node, (list, tuple)):
            return sum(_walk(v) for v in node)
        return 0
    return sum(_walk(s) for s in steps)


def compact_trace(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把一轮的过程步压成可落盘的形状：封顶步数、封顶字数、被丢的都记进 omitted。

    两步各管一件事，顺序不能反：先按步数裁再按字数裁，字数那一刀才可能留下
    「最有信息量的那几步」而不是「碰巧排在前面那几步」。
    """
    kept = [s for s in steps if isinstance(s, dict) and s.get("kind")]
    dropped = 0

    if len(kept) > TRACE_STEPS_MAX:
        head_n = TRACE_STEPS_MAX // 2
        tail_n = TRACE_STEPS_MAX - head_n
        dropped += len(kept) - head_n - tail_n
        kept = kept[:head_n] + kept[-tail_n:]

    # 字数超预算时删掉「最长的那一条」：思考步通常远长于工具步，于是先被牺牲的是
    # 大段独白，而「调了哪个工具、搜到哪些网址」这些真正要核对的证据留到最后。
    # 下限两条：一份 trace 至少留住首尾，否则「留痕」会变成「什么都不剩」。
    while len(kept) > 2 and _trace_chars(kept) > TRACE_CHARS_MAX:
        biggest = max(range(len(kept)), key=lambda i: _trace_chars([kept[i]]))
        del kept[biggest]
        dropped += 1

    if dropped:
        middle = len(kept) // 2
        if kept[middle:middle + 1] and kept[middle].get("kind") == "omitted":
            kept[middle] = {"kind": "omitted",
                            "count": kept[middle].get("count", 0) + dropped}
        else:
            kept.insert(middle, {"kind": "omitted", "count": dropped})
    return kept


def sse_data(frame: Dict[str, Any]) -> str:
    """一帧的线上字节形态：`data: {json}\\n\\n`，JSON 内绝不出现裸换行。"""
    return f"data: {json.dumps(frame)}\n\n"
