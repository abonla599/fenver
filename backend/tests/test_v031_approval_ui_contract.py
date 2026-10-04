"""v0.31 审批确认卡片双端契约：PWA 与原生文案逐字一致，漂一个字就红。

R5 验收条目之一：双端契约测试钉住审批卡片的文案。
判据来源：
- 服务端权威：backend/app/core/approval.py 的 APPROVAL_TITLE / APPROVAL_ALLOW_TEXT /
  APPROVAL_REJECT_TEXT 常量
- PWA 端：backend/app/web/static/app.js 的 APPROVAL_TITLE / APPROVAL_ALLOW_TEXT /
  APPROVAL_REJECT_TEXT 常量
- 原生端：android-native/.../ChatUi.kt 的 APPROVAL_TITLE / APPROVAL_ALLOW_TEXT /
  APPROVAL_REJECT_TEXT 常量

读法沿用本仓库判例（test_web_trace_panel_parity_v0300_contract.py）：
源码当文本读，剥注释后断言特定字面量存在。
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STATIC = REPO_ROOT / "backend" / "app" / "web" / "static"
APP_JS = STATIC / "app.js"
API_JS = STATIC / "api.js"
CHAT_KT = (REPO_ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" /
           "fenever" / "assistant" / "nativeapp" / "ui" / "ChatUi.kt")
APPROVAL_PY = REPO_ROOT / "backend" / "app" / "core" / "approval.py"


def _read(path: Path) -> str:
    assert path.is_file(), f"契约的另一端不在仓库里：{path}"
    return path.read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    """去掉 JS 的 // 与 /* */ 注释，尊重字符串字面量。"""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in ('"', "'", "`"):
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == c:
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_kt_comments(src: str) -> str:
    """去掉 Kotlin 的 // 与 /* */ 注释，尊重字符串字面量。"""
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _extract_const_js(src: str, name: str) -> str:
    """从 JS 源码提取 const NAME = "value" 的 value。"""
    pattern = rf'const\s+{name}\s*=\s*"([^"]*)"'
    m = re.search(pattern, src)
    assert m, f"JS 中找不到 const {name} = \"...\""
    return m.group(1)


def _extract_const_kt(src: str, name: str) -> str:
    """从 Kotlin 源码提取 const val NAME = "value" 的 value。"""
    pattern = rf'const\s+val\s+{name}\s*=\s*"([^"]*)"'
    m = re.search(pattern, src)
    assert m, f"Kotlin 中找不到 const val {name} = \"...\""
    return m.group(1)


def _extract_const_py(src: str, name: str) -> str:
    """从 Python 源码提取 NAME = "value" 的 value。"""
    pattern = rf'^{name}\s*=\s*"([^"]*)"'
    m = re.search(pattern, src, re.MULTILINE)
    assert m, f"Python 中找不到 {name} = \"...\""
    return m.group(1)


class TestApprovalCopyContract:
    """双端审批卡片文案契约：标题、允许、拒绝三句逐字一致。"""

    def test_server_defines_canonical_copy(self):
        """服务端是文案权威来源。"""
        src = _read(APPROVAL_PY)
        assert _extract_const_py(src, "APPROVAL_TITLE") == "执行代码确认"
        assert _extract_const_py(src, "APPROVAL_ALLOW_TEXT") == "允许"
        assert _extract_const_py(src, "APPROVAL_REJECT_TEXT") == "拒绝"

    def test_pwa_matches_server(self):
        """PWA 端文案与服务端逐字一致。"""
        py_src = _read(APPROVAL_PY)
        js_src = _strip_js_comments(_read(APP_JS))
        assert _extract_const_js(js_src, "APPROVAL_TITLE") == _extract_const_py(py_src, "APPROVAL_TITLE")
        assert _extract_const_js(js_src, "APPROVAL_ALLOW_TEXT") == _extract_const_py(py_src, "APPROVAL_ALLOW_TEXT")
        assert _extract_const_js(js_src, "APPROVAL_REJECT_TEXT") == _extract_const_py(py_src, "APPROVAL_REJECT_TEXT")

    def test_native_matches_server(self):
        """原生端文案与服务端逐字一致。"""
        py_src = _read(APPROVAL_PY)
        kt_src = _strip_kt_comments(_read(CHAT_KT))
        assert _extract_const_kt(kt_src, "APPROVAL_TITLE") == _extract_const_py(py_src, "APPROVAL_TITLE")
        assert _extract_const_kt(kt_src, "APPROVAL_ALLOW_TEXT") == _extract_const_py(py_src, "APPROVAL_ALLOW_TEXT")
        assert _extract_const_kt(kt_src, "APPROVAL_REJECT_TEXT") == _extract_const_py(py_src, "APPROVAL_REJECT_TEXT")

    def test_pwa_and_native_identical(self):
        """PWA 与原生文案互相逐字一致（双保险）。"""
        js_src = _strip_js_comments(_read(APP_JS))
        kt_src = _strip_kt_comments(_read(CHAT_KT))
        assert _extract_const_js(js_src, "APPROVAL_TITLE") == _extract_const_kt(kt_src, "APPROVAL_TITLE")
        assert _extract_const_js(js_src, "APPROVAL_ALLOW_TEXT") == _extract_const_kt(kt_src, "APPROVAL_ALLOW_TEXT")
        assert _extract_const_js(js_src, "APPROVAL_REJECT_TEXT") == _extract_const_kt(kt_src, "APPROVAL_REJECT_TEXT")


class TestApprovalUIPresence:
    """双端审批 UI 元素存在性：确保卡片骨架已就位。"""

    def test_pwa_html_has_approval_card(self):
        """PWA HTML 模板里有 .msg-approval 卡片骨架。"""
        html = _read(STATIC / "index.html")
        assert 'class="msg-approval hidden"' in html, "PWA 缺少 .msg-approval 卡片"
        assert 'class="approval-head"' in html, "PWA 缺少 .approval-head"
        assert 'class="approval-summary"' in html, "PWA 缺少 .approval-summary"
        assert 'class="approval-allow"' in html, "PWA 缺少 .approval-allow 按钮"
        assert 'class="approval-reject"' in html, "PWA 缺少 .approval-reject 按钮"

    def test_pwa_js_handles_approval_event(self):
        """PWA JS 处理 approval_request 帧。"""
        js = _strip_js_comments(_read(APP_JS))
        assert '"approval_request"' in js, "PWA JS 不识别 approval_request 帧"
        assert "paintApproval" in js, "PWA JS 缺少 paintApproval 函数"
        assert "confirmApproval" in js, "PWA JS 缺少 confirmApproval 函数"

    def test_pwa_api_has_approval_methods(self):
        """PWA API 封装有审批相关方法。"""
        js = _strip_js_comments(_read(API_JS))
        assert "confirmApproval" in js, "PWA API 缺少 confirmApproval"
        assert "/v1/approval/confirm" in js, "PWA API 缺少 /v1/approval/confirm 路径"

    def test_native_has_approval_card_composable(self):
        """原生端有 ApprovalCard Composable。"""
        kt = _strip_kt_comments(_read(CHAT_KT))
        assert "fun ApprovalCard" in kt, "原生端缺少 ApprovalCard Composable"

    def test_native_handles_approval_event(self):
        """原生端处理 ChatEvent.ApprovalRequest。"""
        kt_src = _read(CHAT_KT)
        assert "ChatEvent.ApprovalRequest" in kt_src, "原生端不处理 ChatEvent.ApprovalRequest"
        assert "ApprovalRequest" in kt_src, "原生端缺少 ApprovalRequest 事件类型"

    def test_native_api_has_approval_methods(self):
        """原生端 API 有审批相关方法。"""
        api_kt = _read(REPO_ROOT / "android-native" / "app" / "src" / "main" /
                        "java" / "xyz" / "fenever" / "assistant" / "nativeapp" / "Api.kt")
        assert "confirmApproval" in api_kt, "原生 API 缺少 confirmApproval"
        assert "/v1/approval/confirm" in api_kt, "原生 API 缺少 /v1/approval/confirm 路径"


class TestApprovalThreePaths:
    """三条出路覆盖：允许→执行、拒绝→熔断、超时→作废。"""

    def test_pwa_allow_path(self):
        """PWA 允许路径：点允许 → POST decision=allow。"""
        js = _strip_js_comments(_read(APP_JS))
        assert '"allow"' in js, "PWA 缺少 allow 决策"

    def test_pwa_reject_path(self):
        """PWA 拒绝路径：点拒绝 → POST decision=reject。"""
        js = _strip_js_comments(_read(APP_JS))
        assert '"reject"' in js, "PWA 缺少 reject 决策"

    def test_pwa_timeout_path(self):
        """PWA 超时路径：expires_at 到期后卡片置灰。"""
        js = _strip_js_comments(_read(APP_JS))
        assert "startApprovalExpiry" in js, "PWA 缺少超时处理"
        assert "expires_at" in js, "PWA 不读 expires_at"

    def test_native_allow_path(self):
        """原生允许路径。"""
        kt = _strip_kt_comments(_read(CHAT_KT))
        assert '"allow"' in kt, "原生缺少 allow 决策"

    def test_native_reject_path(self):
        """原生拒绝路径。"""
        kt = _strip_kt_comments(_read(CHAT_KT))
        assert '"reject"' in kt, "原生缺少 reject 决策"

    def test_native_timeout_path(self):
        """原生超时路径。"""
        kt = _strip_kt_comments(_read(CHAT_KT))
        assert "__timeout__" in kt or "timeout" in kt.lower(), "原生缺少超时处理"
