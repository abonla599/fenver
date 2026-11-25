"""#326：发版闸门读 build 的那一趟不许走任何代理（2026-11-25 收口）。

为什么要钉：`tools/pre_tag_gate.py::fetch_build` 是「线上跑的是哪一版」这条判据的唯一读口。
它原来用 `urllib.request.urlopen` —— 默认 opener 在 Windows 上会装一个读「环境变量 +
注册表 Internet Settings」的 ProxyHandler。谁能改那组键，谁就能把这把闸门读到的 build 串
改道到一个自己复述的假 /health 上，于是闸门「放行」一个根本没换过的包。

修法与仓库里已有的那一处同源（`app/core/releases.py::_fetch_repo`）：显式
`build_opener(ProxyHandler({}))`。空 dict 才是「不走任何代理」；`ProxyHandler()` 不给实参
会触发 `getproxies()`，是同一个坑的另一面。

**TLS 行为刻意不变**：不传 context，与 urlopen 的默认上下文同源。钉 cafile/capath 是被明令
禁止的（裁决 R1）。这条锁只钉「代理被砍掉」与「TLS 上下文没被偷偷钉死」两件事。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "tools" / "pre_tag_gate.py"


def test_fetch_build_uses_a_proxy_free_opener():
    src = GATE.read_text(encoding="utf-8")
    body = src[src.index("def fetch_build("):]
    body = body[:body.index("\ndef ")]
    assert "build_opener(urllib.request.ProxyHandler({}))" in body, \
        "fetch_build 必须用空 dict 的 ProxyHandler：否则注册表/环境代理能改道它读到的 build"
    assert "urllib.request.urlopen(" not in body, \
        "fetch_build 不许再走 urlopen 的默认 opener（那正是 #326 的洞）"


def _code_lines(body: str) -> str:
    """只留代码行：注释里写禁令词（说明为什么禁）不算违规，那是自证的一部分。"""
    out = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def test_fetch_build_does_not_pin_a_trust_store():
    src = GATE.read_text(encoding="utf-8")
    body = src[src.index("def fetch_build("):]
    body = body[:body.index("\ndef ")]
    code = _code_lines(body)
    for banned in ("cafile", "capath", "cadata", "SSLContext("):
        assert banned not in code, \
            "fetch_build 不许自己钉信任锚（裁决 R1：本机信任根 209→147，模型/搜索/取包会全红）"
