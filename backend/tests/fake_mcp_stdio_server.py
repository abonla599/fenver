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
- ``record``          正常 server；``messages.jsonl`` 记录收到的请求 id（出站 id 递增用）。
- ``bad_version_type`` initialize 回 ``"protocolVersion": []``。
- ``hugeint``         initialize 响应含一个 5000 位整数（裸文本写入）。
- ``trueid``          initialize 响应 ``id: true``（不能当成 id=1）。
- ``stray3``          initialize 连发 3 条错 id 响应，不再回正确响应。
- ``nullerror``       initialize 回 ``"error": null``。
- ``nanresult``       ``tools/call`` 的 result 里含 NaN。
- ``surrogate``       ``tools/call`` 的文本是 ``"\\ud800"`` 孤立代理项（裸文本写入）。
- ``scalarresult``    ``tools/call`` 的 result 是标量。
- ``str_iserror``     ``tools/call`` 回 ``"isError": "false"``（字符串，不是真值 True）。
- ``int_iserror``     正常 server；``fail_int`` 回 ``"isError": 1``（整数真值，不是 True）。
- ``infresult``       ``tools/call`` 裸文本回 ``1e999``（json.loads 解析成 inf，不过
                      ``parse_constant``）。
- ``deepnest``        initialize 回 1200 层嵌套数组（约 2.4KB，远低于 512KB 行限）后 sleep。
- ``empty_cursor``    ``tools/list`` 回 ``nextCursor: ""``。
- ``bigreqid``        server 请求的 id 是 300KB 字符串（回 -32601 会超过请求上限）。
- ``eof_alive``       stdout 指到空文件（读端立刻 EOF）+ sleep 60（进程仍活）。
- ``envdump``         把 ``os.environ`` 按键排序写 ``<argv[2]>/env.json``，再走正常协议。
- ``tree``            spawn 一个 ``time.sleep(60)`` 的孙进程，父/孙 PID 写
                      ``<argv[2]>/tree.json``，并每 0.2s 追加心跳到
                      ``<argv[2]>/heartbeat.txt``；再走正常协议。
- ``hog``             先正常回 initialize，再分块分配约 128MB（保持引用）后 sleep 60。
- ``record_argv``     把 ``sys.argv`` 写 ``<argv[2]>/argv.json``，再走正常协议。
- ``sleep``           只 sleep 60（``tree`` 在 PyInstaller 冻结包里用它当孙进程，
                      frozen 解释器没有 ``-c``）。
"""
import json
import os
import subprocess
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


def send_raw(text: str) -> None:
    """写一条手搓的 JSON 行（用于构造 json.dumps 本身产不出的坏形状）。"""
    data = text.encode("utf-8")
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
    "fail_int": _tool("fail_int", {"reason": {"type": "string"}}),
    "boom": _tool("boom"),
}

NORMAL_MODES = {
    "ok",
    "record",
    "wrongid",
    "dup",
    "crlf",
    "bom",
    "serverreq",
    "slow_call",
    "flood",
    "int_iserror",
    "envdump",
    "tree",
    "hog",
    "record_argv",
}

_HOG_REF = []


def _write_json(name: str, obj) -> None:
    """把调试产物写到 argv 传入的临时目录（没有目录就什么都不做）。"""
    directory = _record_dir()
    if not directory:
        return
    try:
        with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
            json.dump(obj, handle, ensure_ascii=False, sort_keys=True)
    except OSError:
        pass


def _heartbeat_loop(path: str) -> None:
    while True:
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("%.6f\n" % time.time())
        except OSError:
            return
        time.sleep(0.2)


def _start_tree() -> None:
    """拉一个孙进程并留痕；孙进程留在同一进程组/Job 里，供杀树测试。"""
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        if getattr(sys, "frozen", False):
            # PyInstaller 冻结包没有 ``-c``：用本 exe 的 ``sleep`` 模式当孙进程。
            child_argv = [sys.executable, "sleep", RECORD_DIR or ""]
        else:
            child_argv = [sys.executable, "-c", "import time; time.sleep(60)"]
        grandchild = subprocess.Popen(child_argv, **kwargs)
    except OSError:
        return
    _write_json("tree.json", {"parent": os.getpid(), "grandchild": grandchild.pid})
    directory = _record_dir()
    if directory:
        threading.Thread(
            target=_heartbeat_loop,
            args=(os.path.join(directory, "heartbeat.txt"),),
            daemon=True,
        ).start()


def _hog() -> None:
    """先让 initialize 的响应出去，再吃约 128MB，给测试留观察窗口。"""
    try:
        for _index in range(128):
            _HOG_REF.append(bytearray(1024 * 1024))
    except MemoryError:
        sys.stderr.write("hog: MemoryError\n")
        sys.stderr.flush()
        sys.exit(1)
    time.sleep(60)


def _startup() -> None:
    if MODE == "eof":
        sys.exit(0)
    if MODE == "sleep":
        time.sleep(60)
        sys.exit(0)
    if MODE == "envdump":
        _write_json("env.json", dict(os.environ))
    elif MODE == "record_argv":
        _write_json("argv.json", list(sys.argv))
    elif MODE == "tree":
        _start_tree()
    if MODE == "eof_alive":
        # stdout 由测试 spawner 指到空文件：读端一到文件尾就是 EOF，而进程继续 sleep。
        # （Windows 上子进程关管道句柄不一定产生可观测 EOF，句柄有内部副本。）
        time.sleep(60)
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
    if MODE == "deepnest":
        # 1200 层嵌套数组（约 2.4KB，远低于 512KB 行限）：json.loads 在 C 层能过，
        # 客户端 Python 层的代理项归一化会 RecursionError。
        send_raw("[" * 1200 + "]" * 1200)
        time.sleep(60)
        return
    if MODE == "slow_init":
        time.sleep(10)
    if MODE == "bigreqid":
        # server 主动请求的 id 是 300KB 字符串：客户端回 -32601 会超过请求体上限。
        send(
            {
                "jsonrpc": "2.0",
                "id": "x" * 300_000,
                "method": "sampling/createMessage",
                "params": {},
            }
        )
        return
    if MODE == "nullerror":
        send({"jsonrpc": "2.0", "id": request_id, "error": None})
        return
    if MODE == "trueid":
        send(
            {
                "jsonrpc": "2.0",
                "id": True,
                "result": {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "serverInfo": {"name": "fake-stdio", "version": "1.0"},
                },
            }
        )
        return
    if MODE == "stray3":
        for bad in (999, 998, 997):
            send(
                {
                    "jsonrpc": "2.0",
                    "id": bad,
                    "result": {
                        "protocolVersion": PROTOCOL,
                        "capabilities": {},
                        "serverInfo": {"name": "fake-stdio", "version": "1.0"},
                    },
                }
            )
        return
    if MODE == "hugeint":
        # 5000 位整数：json.dumps 自己也打不出来，只能手搓裸文本。
        line = (
            '{"jsonrpc":"2.0","id":%s,"result":{"protocolVersion":"%s",'
            '"capabilities":{},"serverInfo":{"name":"fake-stdio","version":"1.0"},'
            '"n":%s}}'
        ) % (json.dumps(request_id), PROTOCOL, "9" * 5000)
        send_raw(line)
        return
    if MODE == "refuse_init":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": "fake server refuses initialize"},
            }
        )
        return
    if MODE == "version_mismatch":
        version = "2024-11-05"
    elif MODE == "bad_version_type":
        version = []
    else:
        version = PROTOCOL
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
    if MODE == "hog":
        _hog()
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
        if MODE == "int_iserror":
            return [OK_TOOLS["fail"], OK_TOOLS["fail_int"], OK_TOOLS["boom"]], None
        return [OK_TOOLS["fail"], OK_TOOLS["boom"]], None
    return [], None


def _handle_list(message: dict) -> None:
    if MODE == "empty_cursor":
        send(
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {"tools": [_tool("echo")], "nextCursor": ""},
            }
        )
        return
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
    if MODE == "nanresult":
        send(
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {
                    "content": [{"type": "text", "text": float("nan")}],
                    "isError": False,
                },
            }
        )
        return
    if MODE == "surrogate":
        line = (
            '{"jsonrpc":"2.0","id":%s,"result":{"content":'
            '[{"type":"text","text":"\\ud800"}],"isError":false}}'
        ) % json.dumps(message.get("id"))
        send_raw(line)
        return
    if MODE == "escapedpair":
        line = (
            '{"jsonrpc":"2.0","id":%s,"result":{"content":'
            '[{"type":"text","text":"\\ud83d\\ude00"}],"isError":false}}'
        ) % json.dumps(message.get("id"))
        send_raw(line)
        return
    if MODE == "infresult":
        # 1e999 是合法 JSON 数字字面量，json.loads 直接解析成 inf，不经过
        # parse_constant；只能靠客户端严格回写（allow_nan=False）拦下。
        line = '{"jsonrpc":"2.0","id":%s,"result":{"n":1e999}}' % json.dumps(
            message.get("id")
        )
        send_raw(line)
        return
    if MODE == "scalarresult":
        send({"jsonrpc": "2.0", "id": message.get("id"), "result": 42})
        return
    if MODE == "str_iserror":
        send(
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {
                    "content": [{"type": "text", "text": "still ok"}],
                    "isError": "false",
                },
            }
        )
        return
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
    elif name == "fail_int":
        # 整数 1 是真值但不是 True：客户端只能按 ``is True`` 判错。
        reason = arguments.get("reason", "未说明")
        result = {
            "content": [{"type": "text", "text": f"int isError: {reason}"}],
            "isError": 1,
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
