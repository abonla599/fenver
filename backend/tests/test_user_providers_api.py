"""个人模型服务（/v1/me/providers）与 owner 归属边界测试。

判据对应 plan-user-providers-20260923.md 的 AC-1..AC-5：
用户能自带 key 接入私有模型；私有条目对其他人（含管理员）等于不存在；
默认选择持久化在服务端并可回落。密钥出口纪律由 test_secret_scan 兜底，
这里另断言接口响应体本身不回显明文。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

MY_KEY = "sk-user-private-key-999"  # secret-scan:allow 测试里造的假密钥，这台机器上没有这颗 key


def _payload(**over):
    base = {"label": "我的私有模型", "base_url": "https://api.mine.invalid/v1",
            "api_key": MY_KEY, "model": "my-chat", "supports_vision": False}
    base.update(over)
    return base


def _add(client, hdr, **over):
    r = client.post("/v1/me/providers", json=_payload(**over), headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()["provider"]["id"]


def _chat(client, hdr, provider=None):
    body = {"messages": [{"role": "user", "content": "你好"}]}
    if provider:
        body["provider"] = provider
    return client.post("/v1/chat", json=body, headers=hdr)


# ---------- AC-1：普通用户添加并用自己的 provider 聊天 ----------

def test_user_can_add_own_provider_and_chat_with_it(client, enforced):
    a = enforced("provider-owner-a")
    pid = _add(client, a)
    assert MY_KEY not in client.get("/v1/me/providers", headers=a).text

    models = client.get("/v1/models", headers=a).json()["models"]
    entry = next(m for m in models if m["id"] == pid)
    assert entry["usable"] is True
    assert entry["shared"] is False, "自己的私有条目不该标成共享"

    r = _chat(client, a, provider=pid)
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == pid
    assert r.json()["model"] == "my-chat"


def test_placeholder_key_saves_but_marks_unusable(client, enforced):
    a = enforced("placeholder-user")
    pid = _add(client, a, api_key="your-key-here")
    mine = client.get("/v1/me/providers", headers=a).json()["mine"]
    entry = next(p for p in mine if p["id"] == pid)
    assert entry["has_key"] is False
    models = client.get("/v1/models", headers=a).json()["models"]
    assert next(m for m in models if m["id"] == pid)["usable"] is False


# ---------- AC-2：用户间隔离，越权与不存在同一种失败 ----------

def test_private_provider_invisible_and_unusable_to_other_user(client, enforced):
    a = enforced("private-owner-a")
    b = enforced("private-owner-b")
    pid = _add(client, a)

    ids_b = [m["id"] for m in client.get("/v1/models", headers=b).json()["models"]]
    assert pid not in ids_b
    mine_b = client.get("/v1/me/providers", headers=b).json()
    assert pid not in [p["id"] for p in mine_b["mine"]]

    # B 拿 A 的 id 聊天：与"不存在的 id"同一行为——回落默认，绝不改道到 A 的上游
    r = _chat(client, b, provider=pid)
    assert r.status_code == 200, r.text
    assert r.json()["provider"] != pid
    assert r.json()["model"] != "my-chat"

    # B 改/删/测/设默认 A 的条目：全是 404（与不存在的 id 无差别）
    assert client.put(f"/v1/me/providers/{pid}", json=_payload(label="劫持"), headers=b).status_code == 404
    assert client.delete(f"/v1/me/providers/{pid}", headers=b).status_code == 404
    assert client.post(f"/v1/me/providers/{pid}/test", headers=b).status_code == 404
    assert client.post("/v1/me/providers/default", json={"provider_id": pid}, headers=b).status_code == 404


def test_private_provider_never_becomes_site_default(client, enforced):
    a = enforced("private-not-default")
    c = enforced("bystander-c")
    # 断言写成"没被顶掉"而不是"等于 fake-model"：整套跑下来别的用例可能动过
    # 全站默认（★ 被点在谁头上、甚至谁被删了），写死具体 id 的断言会在离真凶
    # 很远的地方假红。这一条要守的不变式只有：加一条私有配置前后，旁观者看到
    # 的默认一个子都不变。
    base = client.get("/v1/models", headers=c).json()["default"]
    pid = _add(client, a)
    got = client.get("/v1/models", headers=c).json()["default"]
    assert got == base, f"旁观者的默认被别人的私有配置顶掉了：{base} -> {got}"
    assert got != pid


# ---------- AC-2b：创建面不接受客户端自带的 id（#224 的攻击复现锁） ----------
# 复现路径（审查员在 BASE 实测 200）：用户 B 往 /v1/me/providers POST 一条
# 带 A 的私有 id 的配置。路由只覆盖 owner/paid_by/is_default 三格，id 原样
# 进 upsert → 更新分支命中 A 那条，base_url/model/api_key 整条换成 B 给的，
# owner 仍是被冒名的那位。此后 A 聊天用的是 B 的上游与 B 的密钥。
# 私有 id 形如 p_<8 hex>（32 位熵），可穷举；共享 id 更是人人可枚举。

HIJACK_KEY = "sk-hijacker-key-777"  # secret-scan:allow 测试里造的假密钥，这台机器上没有这颗 key
_MUTABLE_FIELDS = ("label", "base_url", "model", "api_key_masked", "has_key",
                   "shared", "paid_by")


def _mine(client, hdr, pid):
    return next((p for p in client.get("/v1/me/providers", headers=hdr).json()["mine"]
                 if p["id"] == pid), None)


def _shared(client, hdr, pid):
    return next((p for p in client.get("/v1/me/providers", headers=hdr).json()["shared"]
                 if p["id"] == pid), None)


def _ids_of(client, hdr):
    """这个人视角下**已经存在**的全部 provider id（共享 + 他自己的）。

    空白 id 的 POST 只要落进这个集合，就是"命中了一条已有配置"——不管命中的是
    别人的私有条目还是全站那一条。
    """
    view = client.get("/v1/me/providers", headers=hdr).json()
    return [p["id"] for p in view["shared"]] + [p["id"] for p in view["mine"]]


def _hijack_payload(pid):
    return _payload(id=pid, label="被冒名改的那条", base_url="https://api.attacker.invalid/v1",
                    model="attacker-chat", api_key=HIJACK_KEY)


def test_post_with_other_users_private_id_is_rejected_and_his_row_untouched(client, enforced):
    a = enforced("id-trust-owner")
    b = enforced("id-trust-attacker")
    pid = _add(client, a)
    before = _mine(client, a, pid)
    assert before is not None, "前置条件没成立：A 的私有条目没建起来"

    r = client.post("/v1/me/providers", json=_hijack_payload(pid), headers=b)
    assert r.status_code == 400, f"B 拿 A 的私有 id 创建本该被拒，实际 {r.status_code}：{r.text}"
    assert "id" in r.json()["detail"], r.text

    after = _mine(client, a, pid)
    for field in _MUTABLE_FIELDS:
        assert after[field] == before[field], \
            f"A 那条私有配置的 {field} 被 B 的一次 POST 改掉了：{before[field]} -> {after[field]}"
    # 冒名不该把这条同时挂到 B 名下，也不该留下攻击者那个 label
    assert _mine(client, b, pid) is None
    assert "被冒名改的那条" not in client.get("/v1/me/providers", headers=a).text
    # 两栏纪律：这把新密钥既不许进错误 detail，也不许从任何读口回来
    assert HIJACK_KEY not in r.text
    assert HIJACK_KEY not in client.get("/v1/me/providers", headers=a).text
    assert MY_KEY not in r.text

    # A 之后聊天用的还是自己那条上游（不是攻击者的）
    chat = _chat(client, a, provider=pid)
    assert chat.status_code == 200, chat.text
    assert chat.json()["model"] == "my-chat", "A 的聊天被改道到了 B 塞的模型"


def test_post_with_shared_id_cannot_rewrite_the_site_wide_entry(client, enforced):
    """同一条漏洞在共享条目上：一次 POST 换掉全站那一条的上游与密钥。"""
    a = enforced("id-trust-shared-user")
    before = _shared(client, a, "fake-model")
    assert before is not None, "前置条件没成立：共享夹具条目不见了"

    r = client.post("/v1/me/providers", json=_hijack_payload("fake-model"), headers=a)
    assert r.status_code == 400, f"带共享 id 的创建本该被拒，实际 {r.status_code}：{r.text}"

    after = _shared(client, a, "fake-model")
    for field in _MUTABLE_FIELDS:
        assert after[field] == before[field], \
            f"共享条目「fake-model」的 {field} 被普通用户的一次 POST 改掉了"
    assert after["shared"] is True, "共享条目被改姓成私有条目"
    assert HIJACK_KEY not in r.text
    # 私有面也不许凭空多出一条同 id 的"我的"配置
    assert _mine(client, a, "fake-model") is None


def test_post_my_provider_still_generates_id_when_absent_or_blank(client, enforced):
    """正控：不带 / 空串 / 纯空白都必须照常创建（别把正常用户打死）。

    形状也必须钉：个人面是**普通用户唯一能写的那一面**，而"空白算没带"若只停在
    守卫的判据里、没把 id 归零，那条的 id 就是字面量 `"   "`——它和 `p_xxxxxxxx`
    天然不同，下面那句"三次互不相同"抓不住这种改法（#224 修复轮 1 的审查实测）。
    """
    a = enforced("id-trust-creator")
    made = []
    for over in ({}, {"id": ""}, {"id": "   "}):
        r = client.post("/v1/me/providers", json=_payload(**over), headers=a)
        assert r.status_code == 200, r.text
        pid = r.json()["provider"]["id"]
        assert pid.startswith("p_"), f"空白/空串 id 原样进了存储层，库里躺着字面量 {pid!r}"
        assert pid not in made, f"三次创建拿到同一个 id：{made + [pid]}"
        made.append(pid)
    assert [p["id"] for p in client.get("/v1/me/providers", headers=a).json()["mine"]] == made


def test_blank_id_variants_never_land_on_an_existing_row(client, enforced):
    """跨用户覆盖那条锁补的形状格：空白 id 的各种写法都不许命中已有那一行。

    审查员在 BASE 上实测活着的那条缝，走的就是这一面：
      1. A 往个人面 POST 一条 `id="   "` 的私有配置 → 200，而库里那条的 id 是
         字面量 `"   "`（存储层 `record.get("id") or 生成` 里空白是真值）；
      2. B 再 POST 一条 `id="   "` → 也 200，`upsert` 认到这个字面量走更新分支，
         把 A 那条的 label/base_url/model/api_key 整条换掉、owner 仍是 A；
      3. A 之后每次聊天都在用 B 的上游与 B 的密钥——#224 原本要修的那个后果，
         只是触发条件从"知道 id"变成"发一个空白"。
    判据写成"400 或全新 `p_` id"两种都放过，是因为归零之后它必然走后者；真正
    不许放过的是**命中已有行**那一种，所以逐格比 A 那条 + 断言新 id 从没出现过。
    三种空白各有来路：`"   "` 是表单里手打的空格，`"\t\n "` 是复制粘贴带进来的，
    `"\\xa0"` 是不破空格（中文输入法 / 网页粘贴最常见，且 `strip()` 也认它）。

    射程自陈（有实测，不是客套的警告）：**这四把锁钉的是行为，不是实现位置**，
    本锁也是其中之一。把空白 id 的归一下沉进 `providers._validate`（在那里
    `.strip()`）一样能过本锁，而那会改变「盘上遗留空白 id 行」的 PUT 语义
    （就地改一条 → 另起新行），属 #231 的射程。审查员的 M-c 变异实测：那种
    搬家形状下四把锁全绿、全量 4247 条用例无人报红——这一把尤其拦不住那种
    搬家，因为它判的是"A 那条有没有被 B 的一次 POST 改掉"，实现住在路由还是
    住在存储层它不闻不问。
    """
    a = enforced("blank-id-victim")
    b = enforced("blank-id-attacker")

    victim = client.post("/v1/me/providers",
                         json=_payload(id="   ", label="主人那条"), headers=a)
    assert victim.status_code == 200, victim.text
    vid = victim.json()["provider"]["id"]
    assert vid.startswith("p_"), f"空白 id 没被归零，A 那条的 id 是字面量 {vid!r}"
    before = _mine(client, a, vid)
    assert before is not None, "前置条件没成立：A 那条没建起来"
    default_before = client.get("/v1/models", headers=a).json()["default"]

    for variant in ("   ", "\t\n ", "\xa0"):
        seen = set(_ids_of(client, b))
        r = client.post("/v1/me/providers", json=_hijack_payload(variant), headers=b)
        assert r.status_code in (200, 400), \
            f"空白 id 变体 {variant!r} 既没被拒也没被归零：{r.status_code} {r.text}"
        if r.status_code == 200:
            pid = r.json()["provider"]["id"]
            assert pid.startswith("p_"), \
                f"空白 id 变体 {variant!r} 原样进了存储层，那条的 id 是 {pid!r}"
            assert pid not in seen, \
                f"空白 id 变体 {variant!r} 命中了一条已有配置（{pid}）——跨用户覆盖还活着"
            assert pid != vid

        after = _mine(client, a, vid)
        for field in _MUTABLE_FIELDS:
            assert after[field] == before[field], \
                f"B 带空白 id {variant!r} 的一次 POST 改掉了 A 那条的 {field}：" \
                f"{before[field]} -> {after[field]}"
        assert HIJACK_KEY not in client.get("/v1/me/providers", headers=a).text
        # 星号不许被顺带洗掉：个人面 POST 强制 is_default=False，一旦命中站内默认
        # 那条就把 ★ 抹了（那是 #229 的形状，本单归零之后这条路一并关掉，所以
        # 这里钉"默认没动"而不是"默认还在"——后者对洗掉 ★ 的改法是瞎的）。
        default_after = client.get("/v1/models", headers=a).json()["default"]
        assert default_after == default_before, \
            f"带空白 id {variant!r} 的一次 POST 动了全站默认：{default_before} -> {default_after}"


def test_both_post_create_routes_reject_client_id_before_upsert(client, enforced, monkeypatch):
    """守卫必须在 upsert 之前拦住，两条创建路由共用同一把（不各抄一份）。"""
    import inspect

    import app.core.providers as providers
    import app.main as main

    assert inspect.getsource(main).count("def _reject_client_provider_id") == 1, \
        "守卫被抄成两份：下一次改动只会漂移其中一份"

    calls = []
    real = providers.store.upsert

    def _spy(record):
        calls.append(record.get("id"))
        return real(record)

    monkeypatch.setattr(providers.store, "upsert", _spy)
    a = enforced("id-trust-no-write")
    admin = {"Authorization": "Bearer boot-token"}

    assert client.post("/v1/me/providers", json=_payload(id="fake-model"), headers=a).status_code == 400
    assert client.post("/v1/providers", json=_payload(id="fake-model"), headers=admin).status_code == 400
    assert calls == [], f"被拒的创建仍然打到了存储层：{calls}"

    assert client.post("/v1/me/providers", json=_payload(), headers=a).status_code == 200
    assert len(calls) == 1, "正控：不带 id 的创建必须照常落库"


# ---------- AC-3：管理员面只见共享、不碰私有 ----------

def test_admin_surface_never_sees_or_touches_private(client, enforced):
    a = enforced("admin-blindness-owner")
    pid = _add(client, a)
    admin = {"Authorization": "Bearer boot-token"}

    ids = [p["id"] for p in client.get("/v1/providers", headers=admin).json()["providers"]]
    assert pid not in ids, "管理员面不得暴露私有条目存在性"
    assert client.put(f"/v1/providers/{pid}", json=_payload(label="转公"), headers=admin).status_code == 404
    assert client.delete(f"/v1/providers/{pid}", headers=admin).status_code == 404
    assert client.post(f"/v1/providers/{pid}/default", headers=admin).status_code == 404
    assert client.post(f"/v1/providers/{pid}/test", headers=admin).status_code == 404


# ---------- AC-4：校验错误不回显密钥；草稿试连不落盘 ----------

def test_validation_errors_never_echo_key(client, enforced):
    a = enforced("bad-input-user")
    r = client.post("/v1/me/providers", json=_payload(base_url="ftp://nope"), headers=a)
    assert r.status_code == 400
    assert MY_KEY not in r.text
    r = client.post("/v1/me/providers", json=_payload(model=""), headers=a)
    assert r.status_code == 400
    assert MY_KEY not in r.text


def test_draft_ping_does_not_persist(client, enforced, monkeypatch):
    a = enforced("draft-user")
    calls = {}

    class _Create:
        def create(self, **kw):
            calls["model"] = kw.get("model")
            return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": "pong"})()})]})()

    class _Client:
        chat = type("Ch", (), {"completions": _Create()})()

    monkeypatch.setattr("app.main.build_client", lambda p, **kw: _Client())
    r = client.post("/v1/me/providers/test", json=_payload(), headers=a)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert calls["model"] == "my-chat"
    mine = client.get("/v1/me/providers", headers=a).json()["mine"]
    assert mine == [], "草稿试连不许留下任何已存配置"


# ---------- AC-5：「我的默认」服务端持久化与回落 ----------

def test_my_default_persists_and_falls_back(client, enforced):
    a = enforced("pref-user")
    # 新用户还没有偏好，此刻看到的默认就是全局默认。回落断言用它而不是写死
    # "fake-model"：整套里别的用例可能把 ★ 挪走，写死会在不相干的地方假红。
    base = client.get("/v1/models", headers=a).json()["default"]
    pid = _add(client, a)
    r = client.post("/v1/me/providers/default", json={"provider_id": pid}, headers=a)
    assert r.status_code == 200, r.text
    assert client.get("/v1/models", headers=a).json()["default"] == pid
    assert client.get("/v1/me/providers", headers=a).json()["default"] == pid

    # 用户也可以把默认押在共享条目上
    shared_id = client.get("/v1/models", headers=a).json()["models"][0]["id"]
    assert client.post("/v1/me/providers/default", json={"provider_id": shared_id}, headers=a).status_code == 200

    # 指回私有再删掉：默认回落全局
    client.post("/v1/me/providers/default", json={"provider_id": pid}, headers=a)
    assert client.delete(f"/v1/me/providers/{pid}", headers=a).status_code == 200
    assert client.get("/v1/models", headers=a).json()["default"] == base

    assert client.post("/v1/me/providers/default", json={"provider_id": "ghost-id"}, headers=a).status_code == 404


def test_my_default_can_point_at_shared_provider(client, enforced):
    a = enforced("shared-pref-user")
    r = client.post("/v1/me/providers/default", json={"provider_id": "fake-model"}, headers=a)
    assert r.status_code == 200, r.text
    assert client.get("/v1/models", headers=a).json()["default"] == "fake-model"


# ---------- 聊天入口：不带 provider 时用「我的默认」 ----------

def test_chat_without_provider_uses_my_default(client, enforced, monkeypatch):
    a = enforced("default-route-user")
    pid = _add(client, a)
    client.post("/v1/me/providers/default", json={"provider_id": pid}, headers=a)
    r = _chat(client, a)
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == pid
