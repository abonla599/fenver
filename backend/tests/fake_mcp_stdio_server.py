"""v0.37.5 T2 测试用假 MCP stdio server（stdlib only，``sys.executable`` 启动）。

用法：``python fake_mcp_stdio_server.py <mode> [<record_dir>]``。所有落点都在 argv
传入的临时目录里，不写网络、不写仓库外固定路径。

模式（argv[1]）：
- ``ok``              正常握手 + 两页 tools/list；``echo`` / ``add`` / ``fail``(isError)
                      / ``boom``(JSON-RPC error)，中文与换行原样回。
- ``slow_init``       收到 initialize 后 sleep 10s 再回。
- ``slow_call``       握手正常；``tools/call`` 前 sleep 10s。
- ``bigline``         启动即写 600KB 不带换行。
- ``badjson``         启动即写 ``not json`` 一行。
- ``batch``           启动即写一个 JSON 数组（批量分帧）。
- ``content_length``  启动即写 ``Content-Length: 42`` 一行（LSP 分帧）。
- ``wrongid``         第一条响应先用 id=999（且 result 刻意是坏协议版本），再发正确响应。
- ``dup``             正确 initialize 响应后 0.25s 再发一条迟到重复。
- ``serverreq``       发 ``sampling/createMessage`` 请求，把收到的 ``-32601`` 回包写进
                      记录目录的 ``serverreq.json``。
- ``crlf``            所有行以 ``\\r\\n`` 结尾。
- ``bom``             initialize 响应首行带 UTF-8 BOM。
- ``flood``           ``tools/call`` 先灌 1200 条通知再回响应。
- ``eof``             启动即退出。
- ``refuse_init``     initialize 回 JSON-RPC error（code -32000）。
- ``version_mismatch`` initialize 回 ``2024-11-05``。
- ``paginate``        5 页。
- ``paginate_more``   6 页。
- ``toomany``         70 条工具。
- ``cursor_repeat``   ``nextCursor`` 永远是同一个值（死循环）。
"""
import json
import os
import sys
import threading
import time

PROTOCOL = "2025-06-18"
MODE = sys.argv[1] if len(sys.argv) > 1 else "ok"
RECORD_DIR = sys.argv[2] if len(sys.argv) > 2 else None
EOL = b"\r\n" if MODE == "crlf" else b"\n"

OUT = sys.stdout.buffer
WRITE_LOCK = threading.Lock()
_FIRST_RESPONSE = True


def _record_dir() -> str:
    if RECORD_DIR:
        try:
            os.makedirs(RECORD_DIR, exist_ok=True)
        except OSError:
            pass
    return RECORD_DIR or ""


def _append(name: str, obj) -> None:
    directory = _record_dir()
    if not directory:
        return
    try:
        with open(os.path.join(directory, name), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except OSError:
        pass


def send(obj, *, bom: bool = False) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    with WRITE_LOCK:
        OUT.write(data + EOL)
        OUT.flush()


def _tool(name: str, properties=None) -> dict:
    return {
        "name": name,
        "description": f"假工具 {name}",
        "inputSchema": {"type": "object", "properties": properties or {}},
    }


OK_TOOLS = {
    "echo": _tool("echo", {"text": {"type": "string"}}),
    "add": _tool("add", {"a": {"type": "number"}, "b": {"type": "number"}}),
    "fail": _tool("fail", {"reason": {"type": "string"}}),
    "boom": _tool("boom"),
}

NORMAL_MODES = {"ok", "wrongid", "dup", "crlf", "bom", "serverreq", "slow_call", "flood"}


def _startup() -> None:
    if MODE == "eof":
        sys.exit(0)
    if MODE == "bigline":
        OUT.write(b"x" * (600 * 1024))
        OUT.flush()
        time.sleep(60)
        sys.exit(0)
    if MODE == "badjson":
        OUT.write(b"not json\n")
        OUT.flush()
    elif MODE == "batch":
        OUT.write(b'[{"jsonrpc":"2.0","id":1,"result":{}}]\n')
        OUT.flush()
    elif MODE == "content_length":
        OUT.write(b"Content-Length: 42\r\n")
        OUT.flush()


def _send_response(message: dict) -> None:
    """所有响应走这里：``wrongid`` 先发一条坏 id 的响应。"""
    global _FIRST_RESPONSE
    if MODE == "wrongid" and _FIRST_RESPONSE:
        _FIRST_RESPONSE = False
        decoy = {
            "jsonrpc": "2.0",
            "id": 999,
            "result": {
                "protocolVersion": "1999-01-01",
                "capabilities": {},
                "serverInfo": {"name": "decoy", "version": "0"},
            },
        }
        send(decoy)
    send(message)


def _handle_initialize(message: dict) -> None:
    request_id = message.get("id")
    if MODE == "slow_init":
        time.sleep(10)
    if MODE == "refuse_init":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": "fake server refuses initialize"},
            }
        )
        return
    version = "2024-11-05" if MODE == "version_mismatch" else PROTOCOL
    if MODE == "serverreq":
        send(
            {
                "jsonrpc": "2.0",
                "id": "srv-1",
                "method": "sampling/createMessage",
                "params": {"messages": []},
            }
        )
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-stdio", "version": "1.0"},
        },
    }
    if MODE == "bom":
        # 客户端读到的第一行就是这条响应，BOM 加在它前面。
        send(response, bom=True)
    else:
        _send_response(response)
    if MODE == "dup":
        duplicate = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-stdio", "version": "1.0"},
            },
        }
        threading.Timer(0.25, lambda: send(duplicate)).start()


def _tools_page(cursor):
    if MODE in ("paginate", "paginate_more"):
        pages = 5 if MODE == "paginate" else 6
        index = 1 if cursor is None else int(str(cursor).lstrip("p"))
        tools = [_tool(f"t{index}a"), _tool(f"t{index}b")]
        next_cursor = f"p{index + 1}" if index < pages else None
        return tools, next_cursor
    if MODE == "toomany":
        return [_tool(f"t{i}") for i in range(70)], None
    if MODE == "cursor_repeat":
        return [_tool("loop")], "same"
    if MODE in NORMAL_MODES:
        if cursor is None:
            return [OK_TOOLS["echo"], OK_TOOLS["add"]], "page2"
        return [OK_TOOLS["fail"], OK_TOOLS["boom"]], None
    return [], None


def _handle_list(message: dict) -> None:
    params = message.get("params") or {}
    tools, next_cursor = _tools_page(params.get("cursor"))
    result = {"tools": tools}
    if next_cursor:
        result["nextCursor"] = next_cursor
    send({"jsonrpc": "2.0", "id": message.get("id"), "result": result})


def _handle_call(message: dict) -> None:
    params = message.get("params") or {}
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if MODE == "slow_call":
        time.sleep(10)
    if MODE == "flood":
        for index in range(1200):
            send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {"n": index},
                }
            )
    if name == "echo":
        text = arguments.get("text", "")
        result = {
            "content": [{"type": "text", "text": str(text)}],
            "isError": False,
        }
    elif name == "add":
        total = arguments.get("a", 0) + arguments.get("b", 0)
        result = {
            "content": [{"type": "text", "text": str(total)}],
            "isError": False,
        }
    elif name == "fail":
        reason = arguments.get("reason", "未说明")
        result = {
            "content": [{"type": "text", "text": f"工具失败: {reason}"}],
            "isError": True,
        }
    elif name == "boom":
        send(
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32000, "message": "fake boom"},
            }
        )
        return
    else:
        send(
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32602, "message": "unknown tool"},
            }
        )
        return
    send({"jsonrpc": "2.0", "id": message.get("id"), "result": result})


def _handle_client_response(message: dict) -> None:
    if MODE == "serverreq":
        _append("serverreq.json", {"response": message})


def main() -> None:
    _startup()
    try:
        for raw in sys.stdin.buffer:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            _append("messages.jsonl", message)
            if "method" not in message:
                if "id" in message:
                    _handle_client_response(message)
                continue
            method = message.get("method")
            if method == "initialize":
                _handle_initialize(message)
            elif method == "tools/list":
                _handle_list(message)
            elif method == "tools/call":
                _handle_call(message)
            elif "id" in message:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {"code": -32601, "message": "method not found"},
                    }
                )
    except (BrokenPipeError, ConnectionResetError, OSError):
        return


if __name__ == "__main__":
    main()
