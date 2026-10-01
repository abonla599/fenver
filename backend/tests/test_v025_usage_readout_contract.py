"""v0.25 R4a：每个人读到自己用量数字的这一面（GET /v1/me/usage）。

账本（app/core/usage.py）从写下第一行起就只有管理页能读——普通用户对自己的
用量是**全盲**的。R4a 补的是读的这半边：写账的行为一个字不动，只加一个
self-only 的读面。四条契约，逐条钉：

1. 匿名进不来（401，与全站凭据门同一句话）；
2. A 读不到 B 的数字，而且这张面上根本没有可以填别人 id 的参数——
   "不是你的"与"不存在"与"空"是同一种回答（200 + rows: []），
   不存在能用状态码差别当探测器的那种形状；
3. 管理员的全局面（/v1/admin/usage）原样保留，admin 走个人面也只能看到自己的行；
4. 响应带 unknown_usage 的"下限"标注（上游没回 token 的那些次不是 0），
   并且不含任何密钥材料——provider 只给 id 与显示名。

诚实的边界：**这里只有次数与 token，没有金额**——价格估算（R4b）还没接，
本文件也不许它悄悄混进来（见最后那条锁）。

两条先于实现提交（与 v024 同一套写法）：下面的用例在端点存在之前就该红，
红了才说明锁咬得住。判据实测写在每条 docstring 里。
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.routing import APIRoute

from app.core import usage
from app.core.authz import PUBLIC_PATHS, UNAUTHORIZED_DETAIL, require_admin
from app.main import app

BOOT = {"Authorization": "Bearer boot-token"}   # enforced 夹具里 ACCESS_TOKEN 的那枚 = 管理员


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    """账本是进程级全局：每条用例都得从空账开始、把旧账还回去（与 usage 测试同一套）。"""
    prev_days, prev_path = usage._days, usage._PATH
    path = tmp_path / "usage.json"
    monkeypatch.setenv("USAGE_DB_PATH", str(path))
    usage.restore(path=str(path))
    yield path
    usage._days, usage._PATH = prev_days, prev_path


def _today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _uid(client, headers) -> str:
    """把 enforced 注册出来的用户名换成账本认的 user_id——归属判定的根子上。"""
    return client.get("/v1/auth/me", headers=headers).json()["user_id"]


def _record(uid, provider_id="p-ds", day=None, **kw):
    kw.setdefault("paid_by", "operator")
    if day is not None:
        kw["day"] = day
    usage.record_call(user_id=uid, provider_id=provider_id, **kw)


# ---------- 1. 匿名与守卫 ----------


def test_anonymous_cannot_read_my_usage(enforced, client):
    """没身份连自己的账都读不到——401 与全站凭据门同一句话，不给"这号有账吗"留信道。"""
    enforced("路过的")   # 库里有身份，排除与鉴权无关的 503 通路
    res = client.get("/v1/me/usage")
    assert res.status_code == 401, res.text
    assert res.json()["detail"] == UNAUTHORIZED_DETAIL


def test_the_route_declares_ordinary_identity_not_admin(enforced):
    """静态锁：个人面必须声明 current_principal（登录即可），且不许是 require_admin。

    主契约（test_route_auth_contract）只要求"有守卫"——把它升格成 require_admin 主契约
    照样绿，而普通用户从此看不见自己的账，这正是"账本只有管理员能读"的 v0.25 前要治的病。
    反过来若守卫被删，主契约红，这里再钉一层"是哪一个"。
    """
    from app.core.authz import current_principal

    route = next(r for r in app.routes
                 if isinstance(r, APIRoute) and r.path == "/v1/me/usage")
    tree = []

    def walk(d):
        tree.append(d.call)
        for sub in d.dependencies:
            walk(sub)

    walk(route.dependant)
    assert current_principal in tree, "/v1/me/usage 没挂登录守卫"
    assert require_admin not in tree, "/v1/me/usage 被升成了管理员面：普通用户看不见自己的账"
    assert "/v1/me/usage" not in PUBLIC_PATHS


# ---------- 2. self-only：别人的数字问不出来 ----------


def test_a_cannot_read_bs_numbers(enforced, client):
    """判据（变异实测）：把 snapshot_for_user 的归属过滤去掉，这条红——
    B 的 999999 会出现在 A 的响应里。这是本文件最不该松的一格。"""
    ha = enforced("甲先生")
    hb = enforced("乙先生")
    ua, ub = _uid(client, ha), _uid(client, hb)
    _record(ua, prompt_tokens=10, completion_tokens=5, total_tokens=15)
    _record(ub, provider_id="p-secret-pool",
            prompt_tokens=999000, completion_tokens=999, total_tokens=999999)

    res = client.get("/v1/me/usage", headers=ha)
    assert res.status_code == 200, res.text
    body = res.json()
    assert [r["provider"]["id"] for r in body["rows"]] == ["p-ds"]
    assert body["totals"]["total_tokens"] == 15
    assert "999999" not in res.text and "p-secret-pool" not in res.text


def test_there_is_no_way_to_ask_about_another_user():
    """参数面锁：这张面只许收 day。谁哪天"顺手"加一个 user_id/provider_id 查询参数，
    self-only 就从"问不出来"退化成"记得过滤就没事"——那是本项目最早付过账的错法。"""
    route = next(r for r in app.routes
                 if isinstance(r, APIRoute) and r.path == "/v1/me/usage")
    params = {p.name for p in route.dependant.query_params}
    params |= {p.name for p in route.dependant.path_params}
    assert params <= {"day"}, f"个人用量面不接受除 day 之外的筛选参数：{sorted(params)}"


def test_not_mine_and_not_there_and_empty_are_the_same_answer(enforced, client):
    """没账的人、有账但那天没用的日子——两种"空"必须是同一种回答（200 + rows: []）。

    与 R1 同一形状：不给 403/404 的差别留下"这天的账存在吗"这种可探测的缝。
    """
    ha = enforced("甲先生")
    ua = _uid(client, ha)
    _record(ua, day="2026-01-01", prompt_tokens=1, completion_tokens=1, total_tokens=2)
    hc = enforced("从没用的")   # 注册了但一次没用过

    mine_quiet = client.get("/v1/me/usage", params={"day": "2026-01-02"}, headers=ha)
    never_used = client.get("/v1/me/usage", params={"day": "2026-01-02"}, headers=hc)
    assert mine_quiet.status_code == never_used.status_code == 200
    assert mine_quiet.json()["rows"] == never_used.json()["rows"] == []
    assert set(mine_quiet.json()) == set(never_used.json()), \
        "两种空的字段形状都得齐——空不是缺键，更不是错"


# ---------- 3. 管理员两面：全局面不动，个人面仍然只读自己 ----------


def test_admin_still_reads_everything_on_the_admin_face(enforced, client):
    """R4a 不拆管理页的数据源：/v1/admin/usage 继续是"所有人"，一个字段都不改。"""
    ha = enforced("甲先生")
    hb = enforced("乙先生")
    ua, ub = _uid(client, ha), _uid(client, hb)
    _record(ua, total_tokens=15)
    _record(ub, provider_id="p-other", total_tokens=40, paid_by="user")

    body = client.get("/v1/admin/usage", headers=BOOT).json()
    seen = {r["user_id"] for r in body["rows"]}
    assert {ua, ub} <= seen, "管理面读全量的能力被削掉了——这不是本任务该动的手"


def test_admin_on_the_personal_face_sees_only_their_own_rows(enforced, client):
    """管理员也守 self-only：bootstrap 的 user_id 是 default_user，用个人面就该只有他自己的。

    否则"个人面"会变成一个旁路的管理面，两面分工（一面答"我"、一面答"所有人"）就没了。
    """
    ha = enforced("甲先生")
    ua = _uid(client, ha)
    _record(ua, total_tokens=15)
    _record("default_user", provider_id="p-boot", total_tokens=77)

    body = client.get("/v1/me/usage", headers=BOOT).json()
    assert [r["provider"]["id"] for r in body["rows"]] == ["p-boot"]
    assert body["totals"]["total_tokens"] == 77


# ---------- 4. 响应形状：零读成零、下限说清楚、密钥不出面 ----------


def test_no_data_user_gets_a_clean_zero_shape(enforced, client):
    """从没用过的人拿到的是**零**，不是 500、不是缺键：rows: [] + 全 0 合计 + 齐的键。"""
    ha = enforced("新来的")
    res = client.get("/v1/me/usage", headers=ha)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["rows"] == [] and body["days"] == []
    assert set(body) >= {"day", "days", "rows", "totals", "note"}
    assert body["day"] == _today(), "不带 day 就是今天——窗口字段不能缺"
    assert all(body["totals"][f] == 0 for f in usage.FIELDS)


def test_malformed_day_is_a_400_shaped_like_the_admin_face(enforced, client):
    """day 只认 YYYY-MM-DD：这个值直接当键查字典，放宽=任何垃圾都换一份 200 空表。
    与 /v1/admin/usage 同一条规矩、同一句话，两面不许漂移。"""
    ha = enforced("手滑的")
    res = client.get("/v1/me/usage", params={"day": "昨天"}, headers=ha)
    assert res.status_code == 400, res.text
    assert res.json()["detail"] == "day 要写成 YYYY-MM-DD"


def test_rows_are_per_provider_with_counters_window_and_floor(enforced, client):
    """行 = 一个 provider 一天的账：计数器齐全、带 day 窗口、unknown_usage>0 必须标出
    "这些条目的 token 数是下限，不是 0"——不说破，0 就会被读成"没用过"。"""
    ha = enforced("甲先生")
    ua = _uid(client, ha)
    _record(ua, day="2026-01-01", prompt_tokens=100, completion_tokens=40,
            total_tokens=140, reasoning_tokens=20, cached_tokens=5, tool_rounds=2)
    _record(ua, day="2026-01-01", provider_id="p-silent")   # 上游没回 usage 的那次

    body = client.get("/v1/me/usage", params={"day": "2026-01-01"}, headers=ha).json()
    rows = {r["provider"]["id"]: r for r in body["rows"]}
    assert set(rows) == {"p-ds", "p-silent"}
    full = rows["p-ds"]
    assert all(f in full for f in usage.FIELDS), "十个计数器一个都不能少，客户端要按字段渲染"
    assert full["total_tokens"] == 140 and full["reasoning_tokens"] == 20
    assert full["unknown_usage"] == 0 and full["tokens_are_floor"] is False
    silent = rows["p-silent"]
    assert silent["unknown_usage"] == 1 and silent["tokens_are_floor"] is True
    assert body["totals"]["tokens_are_floor"] is True, "合计里掺了下限条目，合计也得挂旗标"
    assert "下限" in body["note"] and "2026-01-01" in body["days"]


def test_provider_shows_id_and_name_only_never_key_material(enforced, client):
    """provider 对外只给 id + 显示名。conftest 那颗假 provider 的密钥就值
    sk-test-000111222333——它、它的掩码形状、base_url，一个都不许出现在响应里。
    """
    ha = enforced("甲先生")
    ua = _uid(client, ha)
    _record(ua, provider_id="fake-model", total_tokens=9)

    res = client.get("/v1/me/usage", headers=ha)
    assert res.status_code == 200, res.text
    row = res.json()["rows"][0]
    assert row["provider"] == {"id": "fake-model", "name": "测试模型"}
    for leak in ("sk-test-000111222333", "api_key", "末四位", "base_url", "example.invalid"):
        assert leak not in res.text, f"响应里出现了密钥材料/配置细节：{leak}"


def test_deleted_provider_still_reads_as_a_row_not_an_error(enforced, client):
    """用过的模型后来被管理员删了：账不能凭空蒸发（那是改历史），名字回落成 id 本身。"""
    ha = enforced("甲先生")
    ua = _uid(client, ha)
    _record(ua, provider_id="早删了", total_tokens=5)

    body = client.get("/v1/me/usage", headers=ha).json()
    assert body["rows"][0]["provider"] == {"id": "早删了", "name": "早删了"}


# ---------- 边界：R4b 不许顺路混进来 ----------


def test_no_money_fields_on_this_face_yet(enforced, client):
    """R4a 只给数字，钱是 R4b 的事。价格/货币/金额字段哪天要进这张面，
    走的应是显式改动与新的契约，而不是这里顺路加一格——这条把"现在没有钱"钉成事实。"""
    ha = enforced("甲先生")
    ua = _uid(client, ha)
    _record(ua, total_tokens=5)
    body = client.get("/v1/me/usage", headers=ha).json()
    text = client.get("/v1/me/usage", headers=ha).text
    for money in ("cost", "price", "currency", "金额", "¥", "$"):
        assert money not in text, f"钱的东西混进了 R4a 的读面：{money}"
    for row in [body["totals"], *body["rows"]]:
        assert not any(f in row for f in ("cost", "price", "amount", "currency"))
