"""2026-10-01 安全批次的判据：一次请求能占多少、被挡时说什么、盘上留什么权限。

这些锁分三组，对应三个当时真实存在的洞：

1. **体积**（413 与各字段的 max_length）。限流管的是"多少次"，管不到"一次多大"；
   此前 `summarize=true` 的记忆接口会把整段请求体原样送进一次上游 chat 调用，
   也就是按调用方的字数扣服务方的钱。
2. **花钱的入口都要记账**。`/v1/chat` 有账本，`/v1/memory/*` 里没有一条有，
   而 add/search/update 同样出网、同样计费——绕开唯一闸门的那条路就是这里。
3. **失败时说什么**。503 的 detail 直接拼了底层异常原话，上游那句
   `Error code: 401 - ... sk-xxx` 就顺着响应体回到调用方手里。

外加两条不碰请求形状的：可变 JSON 一律 0600 落盘，两个安卓壳都关掉备份导出。
"""
import json
import os
import stat
from pathlib import Path

import pytest

from app.core import auth_router as ar
from app.main import MAX_CHAT_MESSAGES, MAX_MESSAGE_CHARS
from app.memory import memory_router as mr

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def clean_chat_ledger():
    """本文件按定义就要反复打同一 IP + 同一身份的账本，前后各清一次免得互相串。"""
    ar._CHATS.clear()
    yield
    ar._CHATS.clear()


# ---------- 1. 体积：JSON 总体积闸门 ----------

def test_an_oversized_json_body_is_refused_before_parsing(client):
    """老实声明了 Content-Length 的超大 JSON，413 掉，不进解析、不鉴权、不记账。"""
    body = json.dumps({"content": "x" * (3 * 1024 * 1024)}).encode()
    res = client.post("/v1/memory/add", content=body,
                      headers={"Content-Type": "application/json"})
    assert res.status_code == 413, res.text


def test_a_normal_json_body_still_gets_through(client):
    res = client.post("/v1/memory/add", json={"content": "普通长度的一条记忆", "summarize": False})
    assert res.status_code == 200, res.text


def test_the_size_gate_leaves_multipart_alone(client, tmp_path):
    """附件走 multipart，有自己的 MAX_UPLOAD_BYTES；在这儿一并卡住会把上传打死。"""
    res = client.post("/v1/uploads", files={"file": ("a.txt", "你好".encode("utf-8"), "text/plain")})
    assert res.status_code in (200, 201), res.text


# ---------- 1b. 体积：各字段的封顶 ----------

def test_chat_rejects_a_history_beyond_the_message_cap(client):
    msgs = [{"role": "user", "content": "m"} for _ in range(MAX_CHAT_MESSAGES + 1)]
    res = client.post("/v1/chat", json={"messages": msgs})
    assert res.status_code == 422, res.text


def test_chat_rejects_an_oversized_single_message(client):
    res = client.post("/v1/chat",
                      json={"messages": [{"role": "user", "content": "x" * (MAX_MESSAGE_CHARS + 1)}]})
    assert res.status_code == 422, res.text


def test_chat_still_accepts_a_multimodal_message_under_the_text_cap(client, monkeypatch):
    """图里的 base64 不计入 32000 字：否则发一张图就"一句话都没说"被判超长。"""
    import app.pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod.ChatPipeline, "_call_model_with_tool_loop",
                        lambda *a, **kw: "ok")
    parts = [{"type": "text", "text": "hi"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64," + ("A" * 50_000)}}]
    res = client.post("/v1/chat", json={"messages": [{"role": "user", "content": parts}]})
    assert res.status_code == 200, res.text


@pytest.mark.parametrize("path,payload,over,bounds", [
    ("/v1/memory/add", {"content": "x", "summarize": False},
     {"content": "x" * (mr.MAX_MEMORY_CONTENT_CHARS + 1)}, ("content",)),
    ("/v1/memory/search", {"query": "x"},
     {"query": "x" * (mr.MAX_MEMORY_QUERY_CHARS + 1)}, ("query",)),
    ("/v1/memory/delete", {"memory_ids": ["a"]},
     {"memory_ids": ["a"] * (mr.MAX_MEMORY_IDS + 1)}, ("memory_ids",)),
])
def test_memory_fields_are_bounded(client, path, payload, over, bounds):
    assert client.request("DELETE" if path.endswith("delete") else "POST",
                          path, json=payload).status_code == 200
    res = client.request("DELETE" if path.endswith("delete") else "POST", path, json=over)
    assert res.status_code == 422, f"{path} 的 {bounds} 没有上限"


def test_update_content_is_bounded_but_a_weight_only_edit_is_not(client):
    mem = client.post("/v1/memory/add", json={"content": "原文", "summarize": False}).json()["memory_id"]
    ok = client.put("/v1/memory/update", json={"memory_id": mem, "new_weight": 2.0})
    assert ok.status_code == 200, ok.text
    over = client.put("/v1/memory/update",
                      json={"memory_id": mem, "new_content": "x" * (mr.MAX_MEMORY_CONTENT_CHARS + 1)})
    assert over.status_code == 422, over.text


# ---------- 2. 花钱的入口都要记账 ----------

@pytest.mark.parametrize("method,path,payload", [
    ("post", "/v1/memory/add", {"content": "记忆", "summarize": True}),
    ("post", "/v1/memory/search", {"query": "记忆", "top_k": 5}),
    ("put", "/v1/memory/update", {"memory_id": "whatever", "new_content": "改一下"}),
])
def test_paid_memory_endpoints_share_the_chat_ledger(client, method, path, payload, monkeypatch):
    """这三个都会朝上游伸一次手，就必须和 /v1/chat 用同一本账。

    断言里同时钉住"被挡的那一次没花钱"：把 store 的入口换成 spy，第 21 次不许再
    触到它——先判断、再记账、才干活，顺序反了这本账就只是记账。
    """
    touched = []
    monkeypatch.setattr(mr.FakeMemoryStore, "add",
                        lambda self, *a, **kw: touched.append("add") or "id-1")
    monkeypatch.setattr(mr.FakeMemoryStore, "search",
                        lambda self, *a, **kw: touched.append("search") or [])
    monkeypatch.setattr(mr.FakeMemoryStore, "update",
                        lambda self, *a, **kw: touched.append("update") or True)

    for _ in range(ar.MAX_CHAT_CALLS_PER_WINDOW):
        res = getattr(client, method)(path, json=payload)
        assert res.status_code == 200, res.text
    before = len(touched)
    blocked = getattr(client, method)(path, json=payload)
    assert blocked.status_code == 429, blocked.text
    assert "Retry-After" in blocked.headers
    assert len(touched) == before, "被挡之后还是去叫了上游：这一下是真花钱的"


def test_free_memory_endpoints_are_not_billed_against_chat(client):
    """只碰本地 chroma 的端点不该占对话额度，也不该被对话额度牵连。

    做法是先把账用 /add 打满（第 21 次确实 429，证明账是活的），再看只读/只改本地
    的那三条——它们必须照常 200。反过来写（给它们也加限流）会让"删掉自己的一条记忆"
    跟着别人的聊天频率一起被挡，那是把守卫装错地方换来的假安全感。
    """
    mem = client.post("/v1/memory/add", json={"content": "待删", "summarize": False}).json()["memory_id"]
    for _ in range(ar.MAX_CHAT_CALLS_PER_WINDOW):
        client.post("/v1/memory/add", json={"content": "x", "summarize": False})
    assert client.post("/v1/memory/add", json={"content": "x", "summarize": False}).status_code == 429
    assert client.get("/v1/memory/list").status_code == 200
    assert client.request("DELETE", "/v1/memory/delete",
                          json={"memory_ids": [mem]}).status_code == 200
    assert client.post("/v1/memory/decay?decay_factor=0.9").status_code == 200


# ---------- 3. 失败时说什么 ----------

def test_a_startup_failure_does_not_echo_the_raw_exception(client, monkeypatch):
    """503 要说"记忆服务不可用"，但不要把初始化异常原话搬到响应体里。"""
    monkeypatch.setattr(mr, "memory_manager", None)
    monkeypatch.setattr(mr, "memory_init_error",
                        "Error code: 401 - incorrect api key provided: sk-abcdefghijklmnop")
    res = client.post("/v1/memory/add", json={"content": "x", "summarize": False})
    assert res.status_code == 503
    detail = res.json()["detail"]
    assert "sk-abcdefghijklmnop" not in detail, detail
    assert "记忆服务不可用" in detail, "脱敏不许把'出了什么事'一起抹掉"


def test_a_storage_failure_detail_is_scrubbed_and_short(client, monkeypatch):
    seen = {}

    class BrokenStore:
        def get_user_memories(self, *a, **kw):
            raise RuntimeError("Bearer sk-abcdefghijklmnop rejected at https://x")

    monkeypatch.setattr(mr, "memory_manager", BrokenStore())
    real = mr.scrub_secrets

    def spy(text):
        seen["called"] = True
        return real(text)

    monkeypatch.setattr(mr, "scrub_secrets", spy)
    res = client.get("/v1/memory/list")
    assert res.status_code == 503
    assert seen.get("called"), "底层异常没走脱敏就直接进了响应体"
    assert "sk-abcdefghijklmnop" not in res.json()["detail"]
    assert len(res.json()["detail"]) < 300


# ---------- 4. 盘上留什么权限 ----------

@pytest.mark.skipif(os.name != "posix", reason="Windows 不实现 POSIX 权限位")
def test_mutable_json_lands_read_only_by_owner(tmp_path):
    """providers.json 里是明文密钥、users.json 里是口令摘要：0644 等于交给同机任何账号。"""
    from app.core.atomic_write import write_json_atomic
    path = str(tmp_path / "providers.json")
    write_json_atomic(path, {"api_key": "sk-secret"})
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, oct(mode)


@pytest.mark.skipif(os.name != "posix", reason="Windows 不实现 POSIX 权限位")
def test_the_temporary_file_is_not_world_readable_either(tmp_path):
    """只 chmod 最终文件会留一个窗口：.tmp 那几百毫秒正是 0644。"""
    from app.core.atomic_write import write_json_atomic
    captured = {}
    real_replace = os.replace

    def spy(src, dst):
        captured["mode"] = stat.S_IMODE(os.stat(src).st_mode)
        return real_replace(src, dst)
    os.replace = spy
    try:
        write_json_atomic(str(tmp_path / "usage.json"), {"a": 1})
    finally:
        os.replace = real_replace
    assert captured["mode"] == 0o600, oct(captured["mode"])


# ---------- 5. 两个壳都不许被备份导出 ----------

@pytest.mark.parametrize("manifest", [
    REPO_ROOT / "android" / "app" / "src" / "main" / "AndroidManifest.xml",
    REPO_ROOT / "android-native" / "app" / "src" / "main" / "AndroidManifest.xml",
])
def test_no_shell_leaves_backup_enabled(manifest):
    """allowBackup 默认 true：一次 `adb backup` 或云备份就能把会话 Cookie 与令牌捞走。"""
    assert manifest.exists(), manifest
    assert 'android:allowBackup="false"' in manifest.read_text(encoding="utf-8")


def test_the_image_does_not_run_as_root():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "\nUSER " in dockerfile, "容器默认 root：任意写就够到宿主机"
    assert "USER root" not in dockerfile
