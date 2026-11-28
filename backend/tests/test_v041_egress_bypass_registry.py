"""#334 的「出网闸四条绕过」登记，同样做成可执行的反证（2026-11-29）。

为什么要登记而不是修
--------------------
`tests/_egress_gate.py` 是**会话级 monkeypatch**：它替换 `socket` 模块上的函数属性。
monkeypatch 有一个固有天花板——**它只改"以后从模块取名字"的那条路**。任何在装闸之前
就把函数抓到手里的代码，拿到的是原函数，闸管不到它。

这不是实现不够细，是这一类方案的性质。所以本轮不"再补几个 patch 点"（那只会把清单
越拉越长、还给人"已经全覆盖"的错觉），而是把已知的绕过逐条钉成反证。

四条绕过（全部实测确认，2026-11-29）
------------------------------------
① **缓存引用**：装闸前先 `raw_connect = socket.socket.connect`，之后调 `raw_connect`
   直接穿过去。真实世界里长这样：某个模块在 import 期就 `from socket import create_connection`
   绑定了名字，或某个 SDK 在初始化时把方法存进实例属性。

② **_socket 底层模块**：`import _socket; _socket.socket().connect(...)` 完全不过闸。
   `socket.socket` 是 `_socket.socket` 的 Python 子类；patch 子类的方法不影响基类。
   实测 `_socket.socket.connect is` 原函数 —— 未被替换。

③ **进程外**：`subprocess` 起 `curl`、`powershell Invoke-WebRequest`、或者任何
   子进程。闸活在当前进程里，管不到另一个进程。

④ **C 扩展内部的 socket**：任何 C 扩展自己调 libc 的 `connect()`（不经过 Python 的
   socket 模块）。本仓依赖里的原生扩展（chromadb 的 rust 绑定、PyMuPDF）理论上都在这一类。

第 ④ 条**不做可执行反证**：要证明它得真起一个 C 扩展去连外网，那既是真出网、又要为
测试引入新依赖，代价远大于收益。它登记在这里，标为"未做机器验证"，不许被当成已证伪。

前三条的判据形状
----------------
不是"验证它能出网"（那会真的出网）。而是**验证闸没有接管那条路**：

  · ① 比较函数身份：装闸后 `socket.socket.connect` 变了，而提前保存的引用没变。
  · ② 同样比较身份：`_socket.socket.connect` 在装闸前后是同一个对象。
  · ③ 断言 `subprocess` 没有被 patch（闸的 install 列表里没有它）——这是形状断言，
    比真起一个 curl 安全，而且抓到的东西一样。

这样每一条都是零出网的，可以常驻跑。
"""
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests._egress_gate as GATE                                    # noqa: E402


class _RecordingMonkeyPatch:
    """只实现 _egress_gate.install 用到的那两个方法，避免依赖 pytest 的 fixture。"""

    def __init__(self):
        self._saved = []

    def setattr(self, obj, name, value, raising=True):
        self._saved.append((obj, name, getattr(obj, name, None)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._saved):
            setattr(obj, name, old)
        self._saved = []


def _with_gate(fn):
    """装闸 → 跑 fn → 卸闸。返回 fn 的返回值。"""
    mp = _RecordingMonkeyPatch()
    GATE.install(mp)
    try:
        return fn()
    finally:
        mp.undo()


def test_bypass_1_a_reference_captured_before_install_keeps_working():
    """① 装闸前保存的引用不受管。

    这条不是"bug 待修"，是登记：monkeypatch 只对"之后从模块取名字"生效。红的含义是
    "闸变成不可绕过了"——那说明有人换了更强的方案，请把这条从登记里删掉。
    """
    import _socket  # noqa: F401  仅为让下面的比较有对象

    raw_connect = socket.socket.connect
    raw_getaddrinfo = socket.getaddrinfo

    def body():
        patched_connect = socket.socket.connect
        patched_ga = socket.getaddrinfo
        return {
            "connect_patched": patched_connect is not raw_connect,
            "getaddrinfo_patched": patched_ga is not raw_getaddrinfo,
            "cached_connect_still_original": raw_connect is not patched_connect,
            "cached_ga_still_original": raw_getaddrinfo is not patched_ga,
        }

    seen = _with_gate(body)
    assert seen["connect_patched"] is True, "闸没装到 socket.socket.connect 上：正对照失败"
    assert seen["getaddrinfo_patched"] is True, "闸没装到 getaddrinfo 上：正对照失败"
    assert seen["cached_connect_still_original"] is True, (
        "① 缓存引用这条绕过今天已经不成立（闸升级了）——请删除这条登记，改写成'已封堵'")
    assert seen["cached_ga_still_original"] is True, (
        "① 缓存 getaddrinfo 这条绕过今天已经不成立——请删除这条登记")


def test_bypass_2_the_underscore_socket_module_is_never_patched():
    """② `_socket.socket.connect` 是 C 层基类，闸只 patch 了 Python 子类。

    实测（2026-11-29）：装闸前后 `_socket.socket.connect` 是同一个函数对象。
    所以 `import _socket; _socket.socket().connect(addr)` 完全不过闸。
    """
    import _socket

    raw_low_level = _socket.socket.connect
    raw_python_level = socket.socket.connect

    def body():
        return {
            "python_level_patched": socket.socket.connect is not raw_python_level,
            "low_level_patched": _socket.socket.connect is not raw_low_level,
        }

    seen = _with_gate(body)
    assert seen["python_level_patched"] is True, "正对照失败：Python 层没被 patch"
    assert seen["low_level_patched"] is False, (
        "② _socket 底层今天已经被封住了——请删除这条登记，改写成'已封堵'。"
        "若确实做了这件事，记得同时更新 _egress_gate 的模块 docstring 射程声明。")


def test_bypass_3_subprocess_is_outside_the_gates_reach():
    """③ 子进程不在射程：闸活在当前进程里。

    形状断言（不真起 curl）：install 的接管列表里没有任何 subprocess 相关名字。
    这条**不可能**被封堵（进程边界是物理事实），所以它永远不该变红；写在这里是为了
    让"跑测试不出网"这句话的边界显式可见——起子进程仍然会出网。
    """
    mp = _RecordingMonkeyPatch()
    GATE.install(mp)
    try:
        src = Path(GATE.__file__).read_text(encoding="utf-8")
    finally:
        mp.undo()

    for token in ("subprocess", "Popen", "os.system", "os.exec"):
        assert token not in src, (
            "③ 闸里出现了 " + token + " —— 如果真去接管子进程了，请更新这条登记；"
            "如果没有接管却出现了这个名字，说明文档在说假话")
    assert "monkeypatch" in src or "MonkeyPatch" in src or "mp.setattr" in src, (
        "闸的实现方式变了（不再是 monkeypatch）：这条登记的前提没了，需要重新评估")


def test_bypass_4_c_extension_sockets_are_registered_but_not_proven():
    """④ C 扩展内部的 socket 调用：**登记但未做机器验证**。

    要证明它得真起一个原生扩展去连外网（真出网）或为测试引入新依赖。本文件只钉住
    "这条登记存在且写明了未验证"，防止它被读成"已证伪"。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    assert "C 扩展内部" in src, "④ 这条登记的正文被删了"
    assert "未做机器验证" in src or "不做可执行反证" in src, (
        "④ 的登记没有写明'未验证'：不写就会被下一个人读成已证伪")
