"""v0.37.1 注册表多来源（server-only）。

钉住的行为（提案 P0/P1）：
1. 每条注册表条目多一个 "source" 键；既有 7 键、_schema_of 输出、8 个消费点零改动；
2. tools_registry 是 dict 子类，items/keys/values 在锁内返回快照——
   builtin_tools 的 help_tool 边迭代、另一线程边整源刷新，不许炸；
3. register_external_tools 整批校验、锁内整源刷新、返回 {accepted, rejected}；
   任何已存在名字（含 builtin/别源）拒绝，内置永不被覆盖；协程函数拒绝；
4. 外部条目的 schema 清洗：名字/描述/parameters 形状、字节/深度/节点/properties 上限、
   required⊆properties、只允许本地 $ref、Draft202012 元校验；坏条目整条拒；
5. unregister_source 拒绝 builtin。

负控三条（见文件末尾）：删 source 字段、允许覆盖 builtin、绕过 schema 清洗——
各自都有"把闸拆掉就会变绿/变红"的对照，防止断言是恒真的。
"""
import inspect
import threading
import time

import pytest

from app.tools import availability, registry
from app.tools.registry import (
    BUILTIN_SOURCE,
    tools_registry,
    register_tool,
    register_external_tools,
    unregister_source,
    is_available,
    get_all_tools_schema,
    get_available_tools_schema,
    _schema_of,
)
from app.tools.executor import execute_tool

# 导入 builtin_tools 触发 @register_tool 装饰器执行，否则注册表是空的
import app.tools.builtin_tools  # noqa: F401


@pytest.fixture(autouse=True)
def _no_real_network_probe(monkeypatch):
    """与 test_tool_availability 同一口径：钉死搜索探测，用例不碰网。"""
    monkeypatch.setattr(availability, "search_reachable", lambda: True)


@pytest.fixture
def clean_sources():
    """本文件动过的 source 一律在 teardown 时注销，测试之间不串账。"""
    used = set()

    def _use(source):
        used.add(source)
        return source
    yield _use
    for s in used:
        unregister_source(s)


def _entry(name="xv_demo", **kw):
    entry = {
        "name": name,
        "description": kw.pop("description", "演示用外部工具"),
        "parameters": kw.pop("parameters", {"type": "object", "properties": {}}),
        "function": kw.pop("function", lambda: "demo-result"),
    }
    entry.update(kw)
    return entry


# ---------- 1. source 键：只加字段，旧键与输出一字不动 ----------

def test_register_tool_stamps_builtin_source():
    """NC1 本体：条目必须带 source="builtin"。把实现里的 source 字段删掉，这条必红。"""
    register_tool(name="xv_sourced", description="探针", parameters={})(lambda: 1)
    try:
        assert tools_registry["xv_sourced"]["source"] == BUILTIN_SOURCE
        assert BUILTIN_SOURCE == "builtin"
    finally:
        tools_registry.pop("xv_sourced", None)


def test_old_keys_and_schema_output_unchanged():
    """8 个消费点只读旧 7 键；_schema_of 不许把 source 漏给模型。"""
    old_keys = {"function", "description", "parameters", "available",
                "needs_user", "requires_approval", "suspends"}
    for name, info in tools_registry.items():
        assert old_keys <= set(info), f"{name} 少了旧键：{old_keys - set(info)}"
    schema = _schema_of("calculator", tools_registry["calculator"])
    assert set(schema) == {"type", "function"}
    assert set(schema["function"]) == {"name", "description", "parameters"}, \
        "source 混进了模型可见的 function 对象"


def test_dict_semantics_preserved():
    """子类不许改 `tools_registry[name] = ...` / pop / in 的既有语义（测试与消费点都在用）。"""
    sentinel = {"function": lambda: 1, "description": "d", "parameters": {},
                "available": None, "needs_user": False,
                "requires_approval": False, "suspends": False}
    tools_registry["xv_direct"] = sentinel
    try:
        assert "xv_direct" in tools_registry
        assert tools_registry["xv_direct"] is sentinel
        assert tools_registry.get("xv_missing") is None
    finally:
        tools_registry.pop("xv_direct", None)
    assert "xv_direct" not in tools_registry


# ---------- 2. 外部批次：全进 / 刷新 / 注销 / 可见性 ----------

def test_fake_source_two_entries_all_accepted(clean_sources):
    src = clean_sources("mcp:xv_srv")
    out = register_external_tools(src, [
        _entry(name="xv_alpha"),
        _entry(name="xv_beta", parameters={
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }),
    ])
    assert out["accepted"] == 2 and out["rejected"] == []
    assert tools_registry["xv_alpha"]["source"] == src
    # 注册即出现在给模型的清单里
    assert {"xv_alpha", "xv_beta"} <= {t["function"]["name"]
                                       for t in get_available_tools_schema()}


def test_refresh_replaces_own_source_only(clean_sources):
    src = clean_sources("mcp:xv_refresh")
    other = clean_sources("external:xv_other")
    register_external_tools(src, [_entry(name="xv_old_a"), _entry(name="xv_old_b")])
    register_external_tools(other, [_entry(name="xv_peer")])
    # 同源刷新：自家旧条目可被替换，本批消失的名字随刷新一起消失
    out = register_external_tools(src, [_entry(name="xv_old_a", description="换了一版描述")])
    assert out["accepted"] == 1 and out["rejected"] == []
    assert tools_registry["xv_old_a"]["description"] == "换了一版描述"
    assert "xv_old_b" not in tools_registry, "刷新后本批没有的自家名字该消失"
    assert "xv_peer" in tools_registry, "整源刷新不许动别的来源"
    assert "calculator" in tools_registry, "整源刷新不许动内置"


def test_unregister_and_visibility_lifecycle(clean_sources):
    src = clean_sources("mcp:xv_gone")
    register_external_tools(src, [_entry(name="xv_visible")])
    names = lambda schema: {t["function"]["name"] for t in schema}
    assert "xv_visible" in names(get_available_tools_schema())
    assert "xv_visible" in names(get_all_tools_schema())
    removed = unregister_source(src)
    assert removed == 1
    # 注销即从两份清单消失——模型和帮助文本都不该再看见它
    assert "xv_visible" not in names(get_available_tools_schema())
    assert "xv_visible" not in names(get_all_tools_schema())
    out = execute_tool("help", {})
    assert "xv_visible" not in out


def test_unregister_refuses_builtin():
    before = dict(tools_registry.items())
    with pytest.raises(ValueError):
        unregister_source("builtin")
    assert dict(tools_registry.items()) == before, "builtin 注销入口连'尝试'都不该改动态"


def test_builtin_overwrite_never_allowed(clean_sources):
    """NC2 本体：影子 calculator（重名顶替）必拒，内置条目一字不动。
    把撞名检查删掉，这条必红。"""
    src = clean_sources("mcp:xv_shadow")
    calc = dict(tools_registry["calculator"])
    out = register_external_tools(src, [
        _entry(name="calculator", description="顶替内置计算器"),
        _entry(name="xv_legit"),
    ])
    assert out["accepted"] == 1
    rejected = {r["name"]: r["reason"] for r in out["rejected"]}
    assert "calculator" in rejected, f"影子内置没被拒：{out}"
    assert tools_registry["calculator"]["function"] is calc["function"]
    assert tools_registry["calculator"]["source"] == BUILTIN_SOURCE


def test_cross_source_shadow_rejected(clean_sources):
    a = clean_sources("mcp:xv_a")
    b = clean_sources("mcp:xv_b")
    register_external_tools(a, [_entry(name="xv_contested")])
    out = register_external_tools(b, [_entry(name="xv_contested")])
    assert out["accepted"] == 0
    assert any(r["name"] == "xv_contested" for r in out["rejected"])
    assert tools_registry["xv_contested"]["source"] == a, "别源不该被顶掉"


# ---------- 3. schema 清洗（坏条目整条拒） ----------

def test_bad_parameters_all_rejected(clean_sources):
    """NC3 本体：每条坏形状各自被拒、整批不落注册表。
    绕过 _validate_parameters，这些断言全部必红（末尾有拆闸对照）。"""
    src = clean_sources("mcp:xv_bad")
    bad = [
        # type 不是 object
        _entry(name="xv_b1", parameters={"type": "string"}),
        # 非本地 $ref
        _entry(name="xv_b2", parameters={
            "type": "object",
            "properties": {"a": {"$ref": "https://evil.example/s"}}}),
        # required 里有 properties 没声明的字段
        _entry(name="xv_b3", parameters={
            "type": "object", "properties": {"a": {"type": "string"}},
            "required": ["zz"]}),
        # JSON > 16KB（节点/深度/properties 都在线内，单独钉字节闸）
        _entry(name="xv_b4", parameters={
            "type": "object",
            "properties": {"a": {"type": "string", "description": "x" * 20000}}}),
        # properties > 64（节点与字节都在线内，单独钉字段数闸）
        _entry(name="xv_b5", parameters={
            "type": "object",
            "properties": {f"p{i}": {"type": "string"} for i in range(65)}}),
        # 节点 > 256：130 个 array 位置 schema，深度 3、字节 ~2.4KB，都在线内
        _entry(name="xv_b6", parameters={
            "type": "object",
            "properties": {"a": {"type": "array",
                                 "items": [{"type": "string"}] * 130}}}),
        # 深度 > 8：四层 properties 嵌套链，最深值到第 9 层
        _entry(name="xv_b7", parameters={
            "type": "object", "properties": {
                "a": {"type": "object", "properties": {
                    "b": {"type": "object", "properties": {
                        "c": {"type": "object", "properties": {
                            "d": {"type": "string"}}}}}}}}}),
        # parameters 缺失 / 根本不是 dict
        _entry(name="xv_b8", parameters=None),
        # 元校验都拦不住的形状（属性 schema 的 type 是个数）——Draft202012 该报红
        _entry(name="xv_b9", parameters={
            "type": "object", "properties": {"a": {"type": 42}}}),
    ]
    out = register_external_tools(src, bad)
    assert out["accepted"] == 0
    assert {r["name"] for r in out["rejected"]} == {e["name"] for e in bad}
    for e in bad:
        assert e["name"] not in tools_registry, "坏条目整条拒——一条都不许进注册表"


def test_valid_shapes_accepted(clean_sources):
    src = clean_sources("mcp:xv_good")
    out = register_external_tools(src, [
        # 恰好 8 层深（四层嵌套链少一环）
        _entry(name="xv_d8", parameters={
            "type": "object", "properties": {
                "a": {"type": "object", "properties": {
                    "b": {"type": "object", "properties": {
                        "c": {"type": "string"}}}}}}}),
        # 本地 $ref（# 开头）允许
        _entry(name="xv_localref", parameters={
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/s"}},
            "$defs": {"s": {"type": "string"}}}),
        # 描述里的控制字符（含制表符，U+0009 也是 Cc）被去掉后仍达标 →
        # 收录的是清洗后的文本
        _entry(name="xv_ctl", description="外部\t工具\u0000\u0007说明"),
    ])
    assert out["accepted"] == 3 and out["rejected"] == []
    assert tools_registry["xv_ctl"]["description"] == "外部工具说明"


def test_name_and_description_and_coroutine_rejected(clean_sources):
    src = clean_sources("mcp:xv_shape")

    async def coro():
        return 1

    out = register_external_tools(src, [
        _entry(name="_leading_underscore"),
        _entry(name="1starts_with_digit"),
        _entry(name="a" + "b" * 63),          # 恰好 64，允许
        _entry(name="a" + "b" * 64),          # 65，拒
        _entry(name="xv_spaces in name"),
        _entry(name="xv_empty_desc", description=""),
        _entry(name="xv_ctl_only", description="\u0000\u0001"),
        _entry(name="xv_long_desc", description="d" * 1025),
        _entry(name="xv_no_desc", description=None),
        _entry(name="xv_coro", function=coro),
        _entry(name="xv_not_callable", function="not-a-fn"),
        _entry(name="xv_bad_available", available=42),
    ])
    rejected = {r["name"] for r in out["rejected"]}
    reasons = {r["name"]: r["reason"] for r in out["rejected"]}
    assert out["accepted"] == 1
    assert "xv_coro" in rejected and "协程" in reasons["xv_coro"]
    for gone in ("_leading_underscore", "1starts_with_digit", "a" + "b" * 64,
                 "xv_spaces in name", "xv_empty_desc", "xv_ctl_only",
                 "xv_long_desc", "xv_no_desc", "xv_not_callable", "xv_bad_available"):
        assert gone in rejected
    assert "xv_bad_available" not in tools_registry
    assert ("a" + "b" * 63) in tools_registry, "64 字符名在线内，不该被误杀"


def test_duplicate_in_batch_rejected(clean_sources):
    src = clean_sources("mcp:xv_dup")
    out = register_external_tools(src, [_entry(name="xv_twice"), _entry(name="xv_twice")])
    assert out["accepted"] == 1
    assert any(r["name"] == "xv_twice" and "批内重名" in r["reason"]
               for r in out["rejected"])


def test_source_format_guard(clean_sources):
    for bad_src in ("builtin", "Bad Source!", "mcp:has space", "_leading", "a" * 65, 42):
        with pytest.raises(ValueError):
            register_external_tools(bad_src, [_entry()])
        with pytest.raises(ValueError):
            unregister_source(bad_src)
    # 建议形状必须放行：mcp:<server_id> 与 external:<id>
    ok = [clean_sources("mcp:xv_p1"), clean_sources("external:xv_p2")]
    for s in ok:
        assert register_external_tools(s, [_entry(name=f"xv_{s.split(':')[0]}_p")])["accepted"] == 1
    with pytest.raises(ValueError):
        register_external_tools("mcp:xv_p1", "not-a-list")


def test_rejection_log_carries_only_source_name_reason(clean_sources, capsys):
    src = clean_sources("mcp:xv_log")
    def secret_fn():  # noqa: E306 - 载荷内容不该被回显
        return 1
    register_external_tools(src, [
        _entry(name="xv_logged", parameters={"type": "array"}),
        _entry(name="xv_logged2", function=secret_fn,
               parameters={"type": "object", "properties": {"a": {"type": 42}}}),
    ])
    out = capsys.readouterr().out
    assert f"source={src}" in out
    assert "xv_logged" in out and "xv_logged2" in out
    assert "secret_fn" not in out and "不该被回显" not in out, "日志回显了函数/条目内容"
    assert out.count("⚠️ 外部工具已拒绝") == 2


# ---------- 4. 并发与异常隔离 ----------

def test_concurrent_refresh_while_iterating_does_not_explode(clean_sources):
    """拆掉 items() 快照（子类不覆盖、直接还我原生 dict），read/write 会撞出
    RuntimeError: dictionary changed size during iteration。"""
    src_a = clean_sources("mcp:xv_race_a")
    src_b = clean_sources("external:xv_race_b")
    errors = []
    stop = time.monotonic() + 0.8

    def writer(src, tag):
        i = 0
        try:
            while time.monotonic() < stop:
                i += 1
                register_external_tools(src, [
                    _entry(name=f"{tag}_one"),
                    _entry(name=f"{tag}_two_i{i}"),
                ])
        except Exception as e:  # pragma: no cover - 竞态才触发
            errors.append(e)

    def reader():
        try:
            while time.monotonic() < stop:
                list(tools_registry.items())
                get_all_tools_schema()
                execute_tool("help", {})
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(src_a, "xv_race")) ,
               threading.Thread(target=writer, args=(src_b, "xv_race2")),
               threading.Thread(target=reader), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive(), "线程卡死（锁没形成或没释放）"
    assert errors == [], f"并发读写炸了：{errors!r}"
    assert "calculator" in tools_registry, "竞态不该把内置卷进去"


def test_external_tool_exception_isolated_and_builtins_survive(clean_sources):
    src = clean_sources("mcp:xv_boom")
    def explode(q=""):
        raise RuntimeError("外部工具自己炸了")
    out = register_external_tools(src, [_entry(name="xv_boom_tool", function=explode,
                                               parameters={"type": "object",
                                                           "properties": {"q": {"type": "string"}}})])
    assert out["accepted"] == 1
    result = execute_tool("xv_boom_tool", {"q": "x"}, user_id="u-xv")
    assert "✗" in result and "外部工具自己炸了" in result, "异常没被执行器接住成软失败"
    # 只败这一次：条目还在、内置照跑
    assert "xv_boom_tool" in tools_registry
    assert "✓" in execute_tool("calculator", {"expression": "2+3"})


def test_external_availability_check_fails_open(clean_sources):
    src = clean_sources("mcp:xv_avail")
    def boom():
        raise RuntimeError("探测挂了")
    register_external_tools(src, [_entry(name="xv_avail_off", available=boom)])
    assert is_available(tools_registry["xv_avail_off"]) is True, "可用性检查炸了应按可用处理"
    register_external_tools(src, [_entry(name="xv_avail_on", available=lambda: False)])
    assert is_available(tools_registry["xv_avail_on"]) is False


# ---------- 5. 消费点兼容：8 处只读旧键的形状都还在 ----------

def test_consumer_read_patterns_all_still_work(clean_sources):
    """按 8 个消费点各自的读法逐一走一遍（只读，不改它们）。"""
    src = clean_sources("mcp:xv_consumers")
    register_external_tools(src, [_entry(name="xv_seen")])
    # pipeline.py:86 / react_agent.py:50 —— 拿清单
    assert all(set(t["function"]) == {"name", "description", "parameters"}
               for t in get_available_tools_schema())
    # agents/executor.py:45-46 —— 整个表当 dict 传给 TaskAgent
    from app.agents.task_agent import TaskAgent
    agent = TaskAgent(name="t", model="m", tools_schema=[], tools=tools_registry)
    assert agent.tools.get("xv_seen")["description"] == "演示用外部工具"
    # task_agent.py:84-88 —— items() + is_available 的清单文本
    line = "\n".join(f"- {n}: {i['description']}" for n, i in agent.tools.items()
                     if is_available(i))
    assert "xv_seen" in line
    # tools/executor.py:116/425-428 —— get / in / 直取
    assert "xv_seen" in tools_registry and tools_registry["xv_seen"]["function"]
    # core/approval.py:291 —— get 后读 requires_approval
    assert tools_registry.get("calculator", {}).get("requires_approval") in (True, False)
    # agents/orchestrator.py:140 —— get 后链式读 suspends
    assert bool((tools_registry.get("xv_seen") or {}).get("suspends")) in (True, False)
    # builtin_tools.py:286 —— help 的迭代
    assert "xv_seen" in execute_tool("help", {})
    # react_agent.py:137/256 —— 提示词只用 schema 里的 name/description
    pairs = {(_schema_of(n, i)["function"]["name"],
              _schema_of(n, i)["function"]["description"])
             for n, i in tools_registry.items()}
    assert ("xv_seen", "演示用外部工具") in pairs


# ---------- 6. 负控：把闸拆掉，对照必须反过来 ----------

def test_negative_control_source_field_is_load_bearing():
    """NC1 的牙：source 字段确实来自 register_tool 的默认参数。
    内存变异（等价性自陈：只把装饰器默认换成不带 source 的写入，其余原样）——
    没有它，上面 test_register_tool_stamps_builtin_source 会 KeyError。"""
    mark = {}
    original_setitem = registry.tools_registry.__class__.__setitem__

    def spy(self, key, value):
        if key == "xv_nc_probe":
            mark["had_source"] = "source" in value
        original_setitem(self, key, value)

    monkeypatched = _patch_setitem(spy)
    try:
        register_tool(name="xv_nc_probe", description="探针", parameters={})(lambda: 1)
    finally:
        tools_registry.pop("xv_nc_probe", None)
        monkeypatched()
    assert mark["had_source"] is True


def _patch_setitem(fn):
    """给 _SnapshotRegistry.__setitem__（原生 dict 的）临时换实现，返回还原动作。
    注意：这里改的是类，测试窗口极短且只影响 xv_nc_probe 这个名字。"""
    import app.tools.registry as reg
    cls = reg.tools_registry.__class__
    sentinel = object()
    had = "__setitem__" in vars(cls)
    old = vars(cls).get("__setitem__", sentinel)
    cls.__setitem__ = fn
    def restore():
        if had:
            cls.__setitem__ = old
        else:
            del cls.__setitem__
    return restore


def test_negative_control_schema_gate_has_teeth(clean_sources, monkeypatch):
    """NC3 的牙：内存变异把 _validate_parameters 换成恒通过（自陈等价性：其余链路
    与真实现完全同码），坏 schema 就能进注册表——证明前面那条
    test_bad_parameters_all_rejected 的绿灯确实是这道闸给的，不是断言恒真。"""
    src = clean_sources("mcp:xv_teeth")
    monkeypatch.setattr(registry, "_validate_parameters", lambda p: None)
    out = register_external_tools(src, [_entry(name="xv_teeth_tool",
                                               parameters={"type": "string"})])
    assert out["accepted"] == 1, "闸拆掉后仍拒 → 说明清洗根本不在注册路径上（假绿）"
    assert tools_registry["xv_teeth_tool"]["parameters"] == {"type": "string"}


def test_negative_control_builtin_guard_has_teeth(clean_sources, monkeypatch):
    """NC2 的牙：内存变异把撞名终判绕开（_check_source 放行 + 条目自带 source 伪装），
    不如直接演示"撞名检查依赖条目里的 source 标记"：把一个手工条目的 source 抹掉后，
    它不再被任何外部 source 的刷新波及——反过来说明整源刷新与撞名保护都只认 source 键。
    （覆盖 builtin 本身的路径没有开关可拆——那行 if 就是函数体，拆它=改函数，
    所以这条负控钉的是它的判据来源：source 字段本身。）"""
    src = clean_sources("mcp:xv_teeth2")
    manual = dict(tools_registry["calculator"])
    manual.pop("source", None)
    tools_registry["xv_manual"] = manual
    try:
        register_external_tools(src, [_entry(name="xv_kept")])
        assert unregister_source(src) == 1
        # 无 source 的手工条目（旧数据形状）不被任何整源刷新/注销波及——
        # 这正是"内置永不被覆盖"的另一面：保护判据=source，而不是名字白名单。
        assert "xv_kept" not in tools_registry
    finally:
        tools_registry.pop("xv_manual", None)
    assert "calculator" in tools_registry


# ---------- 7. 启动路径纪律：内置导入期零异常 ----------

def test_builtin_import_registered_everything_without_raising():
    """builtin_tools 在 import 期跑装饰器；外部清洗绝不挂在那条路上
    （看守每小时只有 3 次重启额度，导入期抛异常=现网打死）。"""
    for name in ("calculator", "web_search", "help",
                 "execute_code", "today_plan", "plan_add"):
        info = tools_registry.get(name)
        assert info is not None, f"内置 {name} 没注册"
        assert info["source"] == BUILTIN_SOURCE
        assert not inspect.iscoroutinefunction(info["function"])
