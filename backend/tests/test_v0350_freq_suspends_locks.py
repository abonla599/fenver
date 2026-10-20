"""v0.35.0 工单 #79 / #40 的 fail-closed 锁：挂起这一族工具的**配套**。

`registry.suspends` 是一句对外许诺——"这个断点接得回去"。两句让许诺兑现不了的漏配
各由这里一把锁钉住：

- #79：声明了 `suspends` 却没在 `executor._FREQ_LIMITS` 里配档。频控闸门是白名单制
  （没登记的工具一次都不被挡，那条锁在 `test_medium2_batch_20260923.py`），而一次挂起
  至少占两次派发（首跑 + 递答案的续跑）、挂起又天生招重试——漏档的那条工具就是闸门下
  唯一挡不住的续跑通道。
- #40：声明了 `suspends` 而函数接不住 `resume=` 关键字。执行器只按关键字递断点，
  接不住 = 每次续跑都是一次 `TypeError` 的白跑，而挂起的意义恰恰是"不再白跑一次"。

两把锁都在**测试期**红（漏配就合不进去）。运行期那一侧只降级不抛（见
`executor.suspend_ready_for_model`）——`builtin_tools` 是导入期注册的，装饰期抛异常
等于现网启动即死，一次配置疏漏不该打死服务，所以这里另有两条锁钉住"症状是藏起来"。

最后一节钉 #40 的执行器形状：`resume` 必须在 schema 过滤**之后**注入。顺序倒了，
模型在 `arguments` 里塞的那份假断点就会被白名单放行，"答案由服务端存着"这条纪律当场作废。
"""
from collections import defaultdict
import functools
import inspect
import json

import pytest

from app.tools import executor as ex
from app.tools.registry import (get_available_tools_schema, is_available,
                                tools_registry)

import app.tools.builtin_tools  # noqa: F401  导入期注册：不导入就没有真注册表可扫


@pytest.fixture(autouse=True)
def _isolate_process_state(monkeypatch):
    """频控账本与 ERROR 去重集合都是进程级全局：每条用例一份自己的。

    不清的代价不是"断言不稳"而是红得莫名其妙——账本按 (工具, 用户) 累加，去重集合按
    (工具, 缺项) 只放行一行，两条都会把上一条用例的状态带进下一条。
    """
    monkeypatch.setattr(ex, "_FREQ_LEDGER", defaultdict(list))
    monkeypatch.setattr(ex, "_gap_reports", set())


def _suspendable_names():
    return sorted(n for n, info in tools_registry.items() if info.get("suspends"))


# ---------- #79：挂起这一族必须在频控表里有一档 ----------

def test_every_tool_declaring_suspends_has_a_freq_tier():
    """漏档 = 这条锁红，不是那条工具跑起来没闸。"""
    declared = _suspendable_names()
    assert declared, "注册表里没有任何工具声明 suspends——这把锁就成了空转"
    missing = [n for n in declared if n not in ex._FREQ_LIMITS]
    assert missing == [], f"这些工具声明了 suspends 却没有频控档：{missing}"


def test_plan_add_tier_is_the_decided_window_and_cap():
    """已拍板的默认档：900 秒内 6 次，含续跑。改数得先改拍板。"""
    assert ex._FREQ_LIMITS["plan_add"] == (900.0, 6)


def test_missing_tier_hides_the_tool_and_shouts_once(monkeypatch, capsys):
    """运行期降级那一半：没档 = available() False + 一行响亮 ERROR，**不抛**。

    断言里那句"不抛"是本票的命门：`builtin_tools` 在导入期注册，任何注册期/装饰期
    异常都会变成现网启动即 ImportError，而开机守护的重试额度用完就不再拉活。
    """
    from app.tools import availability

    monkeypatch.setattr(availability, "search_reachable", lambda: True)
    monkeypatch.delitem(ex._FREQ_LIMITS, "plan_add", raising=False)
    info = tools_registry["plan_add"]

    assert is_available(info) is False, "没档的挂起工具还对模型开放"
    assert ex.suspend_ready_for_model("plan_add") is False
    assert "plan_add" not in {t["function"]["name"] for t in get_available_tools_schema()}
    is_available(info)                          # 再问一次：给模型拼清单每次都问
    captured = capsys.readouterr().out
    assert captured.count("[Tools][ERROR] plan_add") == 1, captured

    monkeypatch.setitem(ex._FREQ_LIMITS, "plan_add", (900.0, 6))
    assert is_available(info) is True, "补齐之后仍然藏着它：降级读的是实时配置，不是导入期算死的"


# ---------- #40：声明了 suspends 就得接得住 resume ----------

def test_every_tool_declaring_suspends_accepts_resume(monkeypatch):
    """全表扫，不看名字不看注释，只看能不能被 `func(resume=...)` 叫动。

    射程边界（这句话的覆盖面比"任何声明 suspends 的工具"窄）：扫的是
    `registry.tools_registry` 这张进程内表，而它今天只由 `builtin_tools` 里那 6 处
    `register_tool` 在导入期写入（本模块 `import app.tools.builtin_tools` 就是为了让
    这 6 处都进表）。运行期动态注册的工具、以及第三方绕过 `builtin_tools` 直接注入的
    表项，这把锁看不见——它保证的是"这 6 处里凡是声明了 suspends 的都接得住 resume"，
    不是"任何声明 suspends 的工具都被它兜住"。
    """
    declared = _suspendable_names()
    assert declared, "注册表里没有任何工具声明 suspends——这把锁就成了空转"
    offenders = [n for n in declared
                 if not ex._takes_resume_kwarg(tools_registry[n]["function"])]
    assert offenders == [], f"这些工具声明了 suspends 却接不住 resume：{offenders}"


def test_a_functools_wraps_wrapper_is_judged_by_the_function_that_actually_runs():
    """`functools.wraps` 把 inner 的签名贴到 wrapper 上，但真正被调用的是 wrapper。

    陷阱形状（今天仓里 `functools.wraps` 是 0 处，这条锁是防将来）：
        @functools.wraps(inner)          # inner(text="", resume=None)
        def wrapper(text=""): ...        # 自己的签名只有 (text)
    `inspect.signature(wrapper)` 跟着 `__wrapped__` 报出 `(text="", resume=None)`，
    按"签名里有 resume"判就会把这条工具判成接得住；真叫 `wrapper(resume=...)` 却是
    TypeError——执行器续跑进来才发现，那次挂起就是白等的。
    """
    def inner(text: str = "", resume: dict = None):
        return {"text": text, "resume": resume}

    @functools.wraps(inner)
    def wrapper(text: str = ""):
        return inner(text)

    # 先钉住陷阱本身成立，免得这条锁在测空气：默认签名有 resume，真调用绑不上。
    assert "resume" in inspect.signature(wrapper).parameters, \
        "这条负例的前提没了：inspect.signature 不再跟随 __wrapped__"
    with pytest.raises(TypeError):
        wrapper(resume={"state": {}, "answer": "答"})

    assert ex._takes_resume_kwarg(wrapper) is False, \
        "签名跟着 __wrapped__ 走了：这条工具会被判接得住，续跑时却 TypeError"


def test_a_plain_resume_keyword_parameter_is_still_accepted():
    """配对的正例：普通 `def f(resume=None)` 不能被上一条的收紧误伤。"""
    def takes_resume(text: str = "", resume: dict = None):
        return "ok"

    assert ex._takes_resume_kwarg(takes_resume) is True


def test_the_pairing_check_says_which_half_is_missing(monkeypatch):
    """判据本身要有分辨力：缺哪一样报哪一样，`**kwargs` 不误伤，普通工具不受影响。"""
    def no_resume(text: str = ""):
        return "ok"

    def takes_resume(text: str = "", resume: dict = None):
        return "ok"

    def star_kwargs(text: str = "", **kwargs):
        return "ok"

    def plain(text: str = ""):
        return "ok"

    def make(func, suspends=True):
        return {"function": func, "description": "道具",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
                "available": None, "needs_user": False,
                "requires_approval": False, "suspends": suspends}

    for name, func, has_tier in (
            ("t79_noresume", no_resume, True),
            ("t79_nofreq", takes_resume, False),
            ("t79_star", star_kwargs, True),
            ("t79_undeclared", plain, False)):
        monkeypatch.setitem(tools_registry, name, make(func, suspends=(name != "t79_undeclared")))
        if has_tier:
            monkeypatch.setitem(ex._FREQ_LIMITS, name, (600.0, 5))

    assert ex.suspend_config_gaps("t79_noresume") == [ex.GAP_NO_RESUME_PARAM]
    assert ex.suspend_config_gaps("t79_nofreq") == [ex.GAP_NO_FREQ_TIER]
    assert ex.suspend_ready_for_model("t79_noresume") is False
    assert ex.suspend_config_gaps("t79_star") == []
    assert ex.suspend_ready_for_model("t79_star") is True
    assert ex.suspend_config_gaps("t79_undeclared") == [], "没声明 suspends 的工具不该被这两样拦"
    assert ex.suspend_config_gaps("不存在的工具") == []


# ---------- 续跑也计次（#79 那句"含续跑"）----------

@pytest.fixture
def t79_probe(monkeypatch):
    """一条声明 suspends、接得住 resume、并把收到的关键字参数原样记下来的道具。"""
    seen = {}

    def make(freq=None, suspends=True):
        def probe(text: str = "", resume: dict = None, user_id: str = None):
            seen["text"] = text
            seen["resume"] = resume
            seen["user_id"] = user_id
            return "跑到了"

        name = "t79_probe"
        monkeypatch.setitem(tools_registry, name, {
            "function": probe,
            "description": "道具",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
            "available": None, "needs_user": False,
            "requires_approval": False, "suspends": suspends,
        })
        if freq is not None:
            monkeypatch.setitem(ex._FREQ_LIMITS, name, freq)
        return name, seen

    return make


def test_resume_dispatch_consumes_the_freq_budget(t79_probe):
    """一次挂起至少两次派发：续跑不计次的话，档就成了只挡首跑的摆设。"""
    name, seen = t79_probe(freq=(600.0, 2))
    packet = {"state": {"step": 2}, "answer": "8888"}

    assert ex.execute_tool_detailed(name, {"text": "a"}, user_id="u-1").ok
    assert ex.execute_tool_detailed(name, {"text": "a"}, user_id="u-1",
                                    resume=packet).ok
    assert seen["resume"] == packet

    third = ex.execute_tool_detailed(name, {"text": "a"}, user_id="u-1", resume=packet)
    assert "过于频繁" in third.text, third.text
    assert third.tool_ran is False, "被闸门挡下的续跑不该已经进了函数体"


def test_a_misshapen_resume_does_not_consume_the_budget(t79_probe):
    """形状不对的续跑在闸门之前就被拒掉，白扣额度等于替攻击者把预算花干净。"""
    name, seen = t79_probe(freq=(600.0, 2))

    refused = ex.execute_tool_detailed(name, {"text": "a"}, user_id="u-1", resume={"state": {}})
    assert refused.ok is False and seen.get("text") is None, refused.text
    valid = ex.execute_tool_detailed(name, {"text": "a"}, user_id="u-1",
                                     resume={"state": {"step": 2}, "answer": "8888"})
    assert valid.ok is True, valid.text


# ---------- #40：resume 必须在 schema 过滤之后注入 ----------

def test_server_resume_overrides_the_argument_the_model_gave(t79_probe):
    """把注入挪到过滤**之前**，这条必红：那时服务端那一份会被白名单过滤掉，
    留在 kwargs 里的是模型自己写的那一份假断点。"""
    name, seen = t79_probe()
    forged = {"state": {"step": 999}, "answer": "模型自己编的答案"}
    server = {"state": {"step": 2}, "answer": "用户给的答案"}

    out = ex.execute_tool(name, {"text": "a", "resume": forged}, user_id="u-1", resume=server)
    assert out.startswith("✓"), out
    assert seen["resume"] == server, f"进函数的是模型那一份：{seen['resume']}"


def test_the_model_cannot_hand_in_a_resume_by_argument_alone(t79_probe):
    """没有续跑就没有断点：arguments 里那份 `resume` 是 schema 外的东西，必须被丢掉。"""
    name, seen = t79_probe()

    out = ex.execute_tool(name, {"text": "a", "resume": {"state": {"x": 1}, "answer": "z"}},
                          user_id="u-1")
    assert out.startswith("✓"), out
    assert seen["resume"] is None, "模型递得进断点 = 它能伪造一次续跑"


def test_resume_argument_is_not_part_of_the_model_visible_schema():
    """`resume` 不进参数表：schema 是模型能碰的唯一契约，多一个口子就多一个可写点。"""
    info = tools_registry["plan_add"]
    assert "resume" not in (info["parameters"].get("properties") or {}), info["parameters"]
    assert "resume" not in json.dumps(info["parameters"])


# ---------- 本票的身份：判据 1 那一条帧现在造得出来了 ----------

def test_the_real_registry_now_reaches_a_resumable_suspension():
    """#79/#40 之前的现场：没有一条真工具声明 suspends ⇒ `_can_resume` 恒假 ⇒
    编排器那个 `can_resume` 分叉永远走 failed，waiting_user 造不出帧。
    """
    declared = _suspendable_names()
    assert "plan_add" in declared, declared
    assert ex._can_resume("plan_add", {"step": 1}) is True
    assert ex._can_resume("plan_add", "不是 dict") is False, "断点形状这一半不许松"
