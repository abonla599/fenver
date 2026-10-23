"""v0.37.1 注册表多来源（server-only）。

钉住的行为（提案 P0/P1 + 审查修复 B1-B6 / N1-N7）：
1. 每条注册表条目多一个 "source" 键；既有 7 键、_schema_of 输出、8 个消费点零改动；
2. tools_registry 是 dict 子类，items/keys/values 在锁内返回**调用时刻的快照**——
   builtin_tools 的 help_tool 边迭代、另一线程边整源刷新，不许炸；
3. register_external_tools 整批校验、锁内整源刷新、返回 {accepted, rejected}；
   任何已存在名字（含 builtin/别源）拒绝，内置永不被覆盖；协程函数/异步可调用对象拒绝；
   条目级异常一律折成 rejected，不拖垮整批；
4. 外部条目的 schema 清洗：名字/描述/parameters 形状、字节/深度/节点/properties 上限、
   required⊆properties、只允许字面本地 $ref（$dynamicRef/$recursiveRef 拒绝）、
   非有限数值拒绝、parameters 内字符串字符闸、Draft202012 元校验；坏条目整条拒；
   通过校验后 JSON 往返回炉成深拷贝，注册后改调用方原 dict 影响不到注册表；
5. 整源刷新：本批校验被拒的同源旧条目保留，全坏批次不清空该 source；
6. unregister_source 拒绝 builtin；register_tool 不许反向覆盖外部条目。

负控（见文件末尾）：拆掉 source 字段 / schema 闸 / 撞名闸，各自都有对照。

测试确定性纪律（N11）：
- 快照语义不靠概率性竞态，直接证明"取快照后改注册表，快照内容不变"；
- 坏 schema 用逐闸 reason 断言，而不是只断言 rejected 名字集合；
- 布尔标记用 `is True/False` 精确断言，不用恒真的 `in (True, False)`。
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


def _names(schema):
    return {t["function"]["name"] for t in schema}


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
    assert {"xv_alpha", "xv_beta"} <= _names(get_available_tools_schema())


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


def test_bad_batch_preserves_old_entries(clean_sources):
    """N1：全坏批次不得清空该 source——本批被拒的同源旧条目保留。"""
    src = clean_sources("mcp:xv_hold_all")
    register_external_tools(src, [
        _entry(name="xv_hold_a", description="旧 a"),
        _entry(name="xv_hold_b", description="旧 b"),
    ])
    out = register_external_tools(src, [
        _entry(name="xv_hold_a", parameters=None),
        _entry(name="xv_hold_b", parameters={"type": "array"}),
    ])
    assert out["accepted"] == 0
    assert {r["name"] for r in out["rejected"]} == {"xv_hold_a", "xv_hold_b"}
    assert tools_registry["xv_hold_a"]["description"] == "旧 a", "坏条目把旧版本删了"
    assert tools_registry["xv_hold_b"]["description"] == "旧 b", "全坏批次清空了 source"


def test_bad_entry_plus_good_new_keeps_rejected_old_entry(clean_sources):
    """N1：坏 a + 好 new → 旧 a 保留、new 进；没被批次的旧名字仍随刷新消失。"""
    src = clean_sources("mcp:xv_hold_mix")
    register_external_tools(src, [
        _entry(name="xv_hold_a", description="旧 a"),
        _entry(name="xv_hold_gone", description="本批没提"),
    ])
    out = register_external_tools(src, [
        _entry(name="xv_hold_a", parameters=None),
        _entry(name="xv_hold_new", description="新的"),
    ])
    assert out["accepted"] == 1
    assert tools_registry["xv_hold_a"]["description"] == "旧 a"
    assert "xv_hold_new" in tools_registry
    assert "xv_hold_gone" not in tools_registry, "本批没提到的旧名字仍应随整源刷新消失"


def test_unregister_and_visibility_lifecycle(clean_sources):
    src = clean_sources("mcp:xv_gone")
    register_external_tools(src, [_entry(name="xv_visible")])
    assert "xv_visible" in _names(get_available_tools_schema())
    assert "xv_visible" in _names(get_all_tools_schema())
    removed = unregister_source(src)
    assert removed == 1
    # 注销即从两份清单消失——模型和帮助文本都不该再看见它
    assert "xv_visible" not in _names(get_available_tools_schema())
    assert "xv_visible" not in _names(get_all_tools_schema())
    out = execute_tool("help", {})
    assert "xv_visible" not in out


def test_unregister_refuses_builtin():
    before = dict(tools_registry.items())
    with pytest.raises(ValueError):
        unregister_source("builtin")
    assert dict(tools_registry.items()) == before, "builtin 注销入口连'尝试'都不该改动态"


def test_builtin_overwrite_never_allowed(clean_sources):
    """NC2 本体：影子 calculator（重名顶替）必拒，内置条目一字不动。
    把撞名闸（_conflict_owner）换成恒 None，这条必红。"""
    src = clean_sources("mcp:xv_shadow")
    calc = dict(tools_registry["calculator"])
    out = register_external_tools(src, [
        _entry(name="calculator", description="顶替内置计算器"),
        _entry(name="xv_legit"),
    ])
    assert out["accepted"] == 1
    rejected = {r["name"]: r["reason"] for r in out["rejected"]}
    assert "calculator" in rejected, f"影子内置没被拒：{out}"
    assert "占用" in rejected["calculator"]
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


# ---------- 3. schema 清洗（坏条目整条拒，且逐闸说明理由） ----------

def test_bad_parameters_all_rejected(clean_sources):
    """NC3 本体：每条坏形状各自被**对应那道闸**拒绝、整批不落注册表。

    断言的是 name → reason 子串，而不是只断言 rejected 名字集合：全拒绝变异
    （_validate_parameters 恒返回错误）下，这些 reason 会对不上 → 必红。
    """
    src = clean_sources("mcp:xv_bad")
    # 约 5000 层嵌套：迭代预检必须在 json.dumps 之前拦下，不能抛 RecursionError
    deep = {"type": "string"}
    for _ in range(5000):
        deep = {"type": "object", "properties": {"a": deep}}
    bad = [
        # type 不是 object（类型闸）
        _entry(name="xv_b1", parameters={"type": "string"}),
        # 非本地 $ref（ref 闸）
        _entry(name="xv_b2", parameters={
            "type": "object",
            "properties": {"a": {"$ref": "https://evil.example/s"}}}),
        # required 里有 properties 没声明的字段（required 闸）
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
        # parameters 缺失 / 根本不是 dict（结构闸）
        _entry(name="xv_b8", parameters=None),
        # 元校验都拦不住的形状（属性 schema 的 type 是个数）——Draft202012 该报红
        _entry(name="xv_b9", parameters={
            "type": "object", "properties": {"a": {"type": 42}}}),
        # B1：NaN / +Inf / -Inf（非有限闸）
        _entry(name="xv_b10", parameters={
            "type": "object", "properties": {"x": {"type": "number",
                                                   "maximum": float("nan")}}}),
        _entry(name="xv_b11", parameters={
            "type": "object", "properties": {"x": {"type": "number",
                                                   "maximum": float("inf")}}}),
        _entry(name="xv_b12", parameters={
            "type": "object", "properties": {"x": {"type": "number",
                                                   "maximum": float("-inf")}}}),
        # B5：required 元素不是字符串（旧写法 `in` dict 抛 unhashable TypeError）
        _entry(name="xv_b13", parameters={
            "type": "object", "properties": {"a": {"type": "string"}},
            "required": [[]]}),
        _entry(name="xv_b14", parameters={
            "type": "object", "properties": {"a": {"type": "string"}},
            "required": [{}]}),
        # B5：5000 层嵌套（旧写法先 json.dumps → RecursionError）
        _entry(name="xv_b15", parameters=deep),
        # N5：$dynamicRef / $recursiveRef（只承诺字面本地 $ref）
        _entry(name="xv_b16", parameters={
            "type": "object", "properties": {"a": {
                "$dynamicRef": "https://evil.example/s"}}}),
        _entry(name="xv_b17", parameters={
            "type": "object", "properties": {"a": {
                "$recursiveRef": "https://evil.example/s"}}}),
        # N2：required 非空但 properties 缺失
        _entry(name="xv_b18", parameters={
            "type": "object", "required": ["x"]}),
    ]
    expected = {
        "xv_b1": "type 必须是 object",
        "xv_b2": "本地 $ref",
        "xv_b3": "required 含 properties",
        "xv_b4": "字节",
        "xv_b5": "properties 字段数",
        "xv_b6": "节点数",
        "xv_b7": "嵌套超过",
        "xv_b8": "parameters 必须是 JSON Schema 对象",
        "xv_b9": "元校验",
        "xv_b10": "非有限",
        "xv_b11": "非有限",
        "xv_b12": "非有限",
        "xv_b13": "required 必须是字符串数组",
        "xv_b14": "required 必须是字符串数组",
        "xv_b15": "嵌套超过",
        "xv_b16": "dynamicRef",
        "xv_b17": "recursiveRef",
        "xv_b18": "required 非空但 properties",
    }
    out = register_external_tools(src, bad)
    assert out["accepted"] == 0
    reasons = {r["name"]: r["reason"] for r in out["rejected"]}
    assert set(reasons) == set(expected), f"拒绝集合不对：{set(reasons) ^ set(expected)}"
    for name, needle in expected.items():
        assert needle in reasons[name], f"{name} 没命中对应闸：{reasons[name]!r}"
    for name in expected:
        assert name not in tools_registry, "坏条目整条拒——一条都不许进注册表"
    assert "xv_b15" not in _names(get_all_tools_schema())


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
        # N5 正例：properties 里名为 "$ref" 的**属性名**是合法的，不能当关键字误拒
        _entry(name="xv_prop_named_ref", parameters={
            "type": "object",
            "properties": {"$ref": {"type": "string"}}}),
        # N5：本地 $ref 循环本版不解析，允许（只保证字面本地）
        _entry(name="xv_ref_cycle", parameters={
            "type": "object",
            "properties": {"next": {"$ref": "#/$defs/node"}},
            "$defs": {"node": {"type": "object", "properties": {
                "next": {"$ref": "#/$defs/node"}}}}}),
    ])
    assert out["accepted"] == 5 and out["rejected"] == [], out
    assert tools_registry["xv_ctl"]["description"] == "外部工具说明"
    assert "xv_prop_named_ref" in tools_registry


def test_non_finite_numbers_rejected(clean_sources):
    """B1：NaN/Infinity 必须拒绝——它们会打崩每一次模型请求的序列化。"""
    src = clean_sources("mcp:xv_nan")
    out = register_external_tools(src, [
        _entry(name="xv_nan", parameters={
            "type": "object", "properties": {"x": {"type": "number",
                                                   "maximum": float("nan")}}}),
        _entry(name="xv_inf", parameters={
            "type": "object", "properties": {"x": {"type": "number",
                                                   "maximum": float("inf")}}}),
        _entry(name="xv_ninf", parameters={
            "type": "object", "properties": {"x": {"type": "number",
                                                   "maximum": float("-inf")}}}),
    ])
    assert out["accepted"] == 0
    reasons = {r["name"]: r["reason"] for r in out["rejected"]}
    assert set(reasons) == {"xv_nan", "xv_inf", "xv_ninf"}
    for reason in reasons.values():
        assert "非有限" in reason
    assert not ({"xv_nan", "xv_inf", "xv_ninf"} & _names(get_all_tools_schema()))


def test_top_level_description_strips_format_and_rejects_surrogate(clean_sources):
    """B2：顶层 description 的 Cf/Zl 可剥离；Cs（孤立代理）必须整条拒绝。"""
    src = clean_sources("mcp:xv_desc")
    out = register_external_tools(src, [
        _entry(name="xv_desc_fmt", description="ok\ufeff\u202e\u200b\u2028end"),
        _entry(name="xv_desc_surrogate", description="ok\ud800end"),
    ])
    assert out["accepted"] == 1
    assert tools_registry["xv_desc_fmt"]["description"] == "okend", \
        "BOM/RTL override/零宽/行分隔应被剥掉"
    reasons = {r["name"]: r["reason"] for r in out["rejected"]}
    assert "xv_desc_surrogate" in reasons
    assert "UTF-8" in reasons["xv_desc_surrogate"] or "代理" in reasons["xv_desc_surrogate"]
    assert "xv_desc_surrogate" not in tools_registry


def test_parameter_strings_with_format_or_surrogate_rejected(clean_sources):
    """B2：parameters 内任何字符串键/值含 Cc/Cf/Cs/Zl/Zp 或不可 UTF-8 编码 → 整条拒。"""
    src = clean_sources("mcp:xv_param_chars")
    bad = [
        # 属性名含 RTL override（Cf）
        _entry(name="xv_p_key_rtl", parameters={
            "type": "object", "properties": {"ev\u202eil": {"type": "string"}}}),
        # 嵌套 description 含零宽空格（Cf）
        _entry(name="xv_p_desc_zwsp", parameters={
            "type": "object", "properties": {
                "a": {"type": "string", "description": "d\u200b"}}}),
        # 嵌套 description 含 BOM（Cf）
        _entry(name="xv_p_desc_bom", parameters={
            "type": "object", "properties": {
                "a": {"type": "string", "description": "d\ufeff"}}}),
        # 字符串值含行分隔符（Zl）
        _entry(name="xv_p_val_ls", parameters={
            "type": "object", "properties": {
                "a": {"type": "string", "const": "x\u2028y"}}}),
        # 属性名是孤立代理（Cs）
        _entry(name="xv_p_key_surrogate", parameters={
            "type": "object", "properties": {"\ud800": {"type": "string"}}}),
    ]
    out = register_external_tools(src, bad)
    assert out["accepted"] == 0
    reasons = {r["name"]: r["reason"] for r in out["rejected"]}
    assert set(reasons) == {e["name"] for e in bad}, out
    for reason in reasons.values():
        assert "控制/格式" in reason, reason


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


def test_trailing_newline_names_and_sources_rejected(clean_sources):
    """B6：`.match` + `$` 会放行尾随换行；fullmatch 必须拒。"""
    src = clean_sources("mcp:xv_nl")
    out = register_external_tools(src, [_entry(name="xv_nl\n"), _entry(name="xv\r")])
    assert out["accepted"] == 0
    assert {r["name"] for r in out["rejected"]} == {"xv_nl\n", "xv\r"}
    assert registry._TOOL_NAME_RE.fullmatch("xv_nl\n") is None
    assert registry._SOURCE_RE.fullmatch("builtin\n") is None
    for bad_src in ("builtin\n", "mcp:xv\n"):
        with pytest.raises(ValueError):
            register_external_tools(bad_src, [_entry()])
        with pytest.raises(ValueError):
            unregister_source(bad_src)


def test_async_callable_object_rejected(clean_sources):
    """N4：不仅是 async def 函数，`__call__` 为 async def 的实例也要拒。"""
    class AsyncCallableTool:
        async def __call__(self):
            return 1

    src = clean_sources("mcp:xv_async_obj")
    out = register_external_tools(src, [
        _entry(name="xv_async_obj", function=AsyncCallableTool())])
    assert out["accepted"] == 0
    assert "协程" in out["rejected"][0]["reason"]
    assert "xv_async_obj" not in tools_registry


def test_duplicate_in_batch_rejected(clean_sources):
    src = clean_sources("mcp:xv_dup")
    out = register_external_tools(src, [_entry(name="xv_twice"), _entry(name="xv_twice")])
    assert out["accepted"] == 1
    assert any(r["name"] == "xv_twice" and "批内重名" in r["reason"]
               for r in out["rejected"])


def test_source_format_guard(clean_sources):
    for bad_src in ("builtin", "Bad Source!", "mcp:has space", "_leading", "a" * 65,
                    42, "builtin\n", "mcp:xv\n"):
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


def test_entry_validation_exception_becomes_rejected(clean_sources, monkeypatch):
    """B5：条目级异常（含 RecursionError）必须折成 rejected，不许抛出整批。"""
    src = clean_sources("mcp:xv_entry_boom")

    def exploding(entry):
        raise RecursionError("模拟条目级爆炸")

    monkeypatch.setattr(registry, "_validate_external_entry", exploding)
    out = register_external_tools(src, [_entry(name="xv_entry_boom")])
    assert out["accepted"] == 0
    assert out["rejected"] == [
        {"name": "xv_entry_boom", "reason": "条目校验异常: RecursionError"}]


# ---------- 4. 注册后别名修改：入库与出口都是深拷贝 ----------

def test_parameters_are_deep_copied_from_caller(clean_sources):
    """B3：注册后改调用方原 dict，注册表与模型可见 schema 都不许变。"""
    src = clean_sources("mcp:xv_alias")
    params = {"type": "object",
              "properties": {"safe": {"type": "string", "description": "原始"}}}
    out = register_external_tools(src, [_entry(name="xv_alias", parameters=params)])
    assert out["accepted"] == 1

    params["properties"]["evil"] = {"$ref": "https://evil.example/schema.json"}
    params["properties"]["safe"]["description"] = "被调用方改了"

    stored = tools_registry["xv_alias"]["parameters"]
    assert "evil" not in stored["properties"], "注册表仍保留调用方别名"
    assert stored["properties"]["safe"]["description"] == "原始"
    schema = {t["function"]["name"]: t for t in get_all_tools_schema()}["xv_alias"]
    assert "evil" not in schema["function"]["parameters"]["properties"]
    assert schema["function"]["parameters"]["properties"]["safe"]["description"] == "原始"


def test_schema_of_returns_deep_copy(clean_sources):
    """B3：消费者改 _schema_of 返回值，注册表本体不得变。"""
    src = clean_sources("mcp:xv_alias2")
    register_external_tools(src, [_entry(name="xv_alias2", parameters={
        "type": "object", "properties": {"safe": {"type": "string"}}})])
    schema = _schema_of("xv_alias2", tools_registry["xv_alias2"])
    schema["function"]["parameters"]["properties"]["injected"] = {"type": "string"}
    schema["function"]["parameters"]["properties"]["safe"]["type"] = "integer"
    assert "injected" not in tools_registry["xv_alias2"]["parameters"]["properties"]
    assert tools_registry["xv_alias2"]["parameters"]["properties"]["safe"]["type"] == "string"


# ---------- 5. 并发与执行器：快照确定性与单次取条目 ----------

def test_registry_read_views_are_point_in_time_snapshots():
    """N11：快照是确定性的——取快照后改注册表，快照内容不变。

    去掉 items/keys/values 的快照（换回原生 dict view），这条必红：
    原生 view 会立刻反映 pop/新增。
    """
    sentinel = {"function": lambda: 1, "description": "快照探针", "parameters": {},
                "available": None, "needs_user": False,
                "requires_approval": False, "suspends": False}
    tools_registry["xv_snap_keep"] = sentinel
    try:
        keys_before = tools_registry.keys()
        items_before = tools_registry.items()
        values_before = tools_registry.values()
        size = len(keys_before)
        assert isinstance(keys_before, list) and isinstance(items_before, list)
        tools_registry.pop("xv_snap_keep")
        tools_registry["xv_snap_other"] = sentinel
        try:
            assert len(keys_before) == size
            assert "xv_snap_keep" in keys_before
            assert "xv_snap_other" not in keys_before, "keys() 不是调用时刻的快照"
            assert len(items_before) == size
            assert {k for k, _ in items_before} == set(keys_before)
            assert len(values_before) == size
        finally:
            tools_registry.pop("xv_snap_other", None)
    finally:
        tools_registry.pop("xv_snap_keep", None)


def test_execute_tool_single_lookup_no_toctou(clean_sources, monkeypatch):
    """B4：execute_tool 只取一次条目。

    旧写法 `in` 之后 `[]` 是两次独立快照——这里让 __contains__ 在返回 True 后
    立刻删键（等价于整源刷新插在中间），旧写法必抛 KeyError；新写法只 get 一次，
    不碰 __contains__，不得抛。
    """
    src = clean_sources("mcp:xv_toctou")
    register_external_tools(src, [_entry(
        name="xv_toctou", requires_approval=False,
        function=lambda: "toctou-ok")])
    cls = type(tools_registry)
    original_contains = cls.__contains__
    calls = {"contains": 0}

    def racy_contains(self, key):
        hit = original_contains(self, key)
        if key == "xv_toctou" and hit:
            calls["contains"] += 1
            dict.pop(self, key, None)   # in 与 [] 之间被整源刷新/注销删掉
        return hit

    monkeypatch.setattr(cls, "__contains__", racy_contains)
    result = execute_tool("xv_toctou", {})
    assert "toctou-ok" in result, result
    assert calls["contains"] == 0, "又走了 `in`：仍是两次独立快照"


def test_execute_tool_unknown_after_removal(clean_sources):
    src = clean_sources("mcp:xv_unknown")
    register_external_tools(src, [_entry(name="xv_unknown",
                                         requires_approval=False)])
    unregister_source(src)
    out = execute_tool("xv_unknown", {})
    assert "未知工具" in out


def test_concurrent_refresh_while_iterating_does_not_explode(clean_sources):
    """并发压力：拆掉 items() 快照会撞 RuntimeError；同时断言批次真的被收录，
    避免"全拒绝变异下仍然绿"的假绿。"""
    src_a = clean_sources("mcp:xv_race_a")
    src_b = clean_sources("external:xv_race_b")
    errors = []
    stop = time.monotonic() + 0.8

    def writer(src, tag):
        i = 0
        try:
            while time.monotonic() < stop:
                i += 1
                out = register_external_tools(src, [
                    _entry(name=f"{tag}_one"),
                    _entry(name=f"{tag}_two_i{i}"),
                ])
                if out["accepted"] != 2:
                    errors.append(AssertionError(
                        f"{tag} 批次没有被收录：{out}"))
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
                                               requires_approval=False,
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


# ---------- 6. 外部条目的执行标记（N7） ----------

def test_external_flag_defaults_and_explicit_false(clean_sources):
    """N7：外部来源 requires_approval 缺省 True（不可信来源默认要审批，产品决定）。"""
    src = clean_sources("mcp:xv_flags")
    out = register_external_tools(src, [
        _entry(name="xv_flags_default"),
        _entry(name="xv_flags_off", needs_user=False,
               requires_approval=False, suspends=False),
    ])
    assert out["accepted"] == 2 and out["rejected"] == []
    default = tools_registry["xv_flags_default"]
    assert default["needs_user"] is False
    assert default["requires_approval"] is True
    assert default["suspends"] is False
    explicit = tools_registry["xv_flags_off"]
    assert explicit["needs_user"] is False
    assert explicit["requires_approval"] is False
    assert explicit["suspends"] is False


def test_external_flags_must_be_real_bools(clean_sources):
    src = clean_sources("mcp:xv_flags_bad")
    bad = [
        _entry(name="xv_flag_str_nu", needs_user="false"),
        _entry(name="xv_flag_str_ra", requires_approval="false"),
        _entry(name="xv_flag_str_su", suspends="false"),
        _entry(name="xv_flag_int", requires_approval=0),
    ]
    out = register_external_tools(src, bad)
    assert out["accepted"] == 0
    reasons = {r["name"]: r["reason"] for r in out["rejected"]}
    assert set(reasons) == {e["name"] for e in bad}
    for reason in reasons.values():
        assert "bool" in reason, reason


# ---------- 7. register_tool 反向覆盖与 source 格式闸（N3） ----------

def test_register_tool_cannot_overwrite_external(clean_sources):
    src = clean_sources("mcp:xv_taken")
    register_external_tools(src, [_entry(name="xv_taken")])
    with pytest.raises(ValueError):
        register_tool(name="xv_taken", description="内置来抢",
                      parameters={})(lambda: 1)
    info = tools_registry["xv_taken"]
    assert info["source"] == src, "外部条目被内置静默替换了"
    assert info["description"] == "演示用外部工具"


def test_register_tool_source_format_guard():
    for bad in ("builtin\n", "builtin ", "builtin\r", "", None, 42, "Bad Source!"):
        with pytest.raises(ValueError):
            register_tool(name="xv_src_bad", description="d",
                          parameters={}, source=bad)
    assert "xv_src_bad" not in tools_registry
    # 非 builtin 来源走同一格式闸后允许注册，并按声明打标
    register_tool(name="xv_src_ok", description="d", parameters={},
                  source="mcp:xv_ext")(lambda: 1)
    try:
        assert tools_registry["xv_src_ok"]["source"] == "mcp:xv_ext"
    finally:
        tools_registry.pop("xv_src_ok", None)


# ---------- 8. 消费点兼容：8 处只读旧键的形状都还在 ----------

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
    # core/approval.py:291 —— get 后读 requires_approval（内置精确为 False）
    assert tools_registry.get("calculator", {}).get("requires_approval") is False
    # agents/orchestrator.py:140 —— get 后链式读 suspends（外部缺省精确为 False）
    assert (tools_registry.get("xv_seen") or {}).get("suspends") is False
    assert (tools_registry.get("xv_seen") or {}).get("requires_approval") is True
    # builtin_tools.py:286 —— help 的迭代
    assert "xv_seen" in execute_tool("help", {})
    # react_agent.py:137/256 —— 提示词只用 schema 里的 name/description
    pairs = {(_schema_of(n, i)["function"]["name"],
              _schema_of(n, i)["function"]["description"])
             for n, i in tools_registry.items()}
    assert ("xv_seen", "演示用外部工具") in pairs


# ---------- 9. 负控：把闸拆掉，对照必须反过来 ----------

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
    """NC2 的牙：把撞名终判 `_conflict_owner` 换成恒 None，外部工具必须能真的
    占用 calculator 名字；退出 monkeypatch 后闸门必须恢复——若实现里把
    `_conflict_owner` 写成恒 None，后半段必红（这也是变异实验 4 的对照）。"""
    src = clean_sources("mcp:xv_teeth2")
    other = clean_sources("external:xv_teeth3")
    original = dict(tools_registry["calculator"])
    try:
        with monkeypatch.context() as mp:
            mp.setattr(registry, "_conflict_owner", lambda existing, source: None)
            out = register_external_tools(src, [
                _entry(name="calculator", description="顶替内置")])
            assert out["accepted"] == 1, \
                "拆闸后外部工具没能占用 calculator（撞名闸不在注册路径上/假绿）"
            assert tools_registry["calculator"]["source"] == src
        # 还原后闸门必须重新生效
        out2 = register_external_tools(other, [
            _entry(name="calculator", description="别的来源再来顶")])
        assert out2["accepted"] == 0, \
            "还原后闸门失效——实现里的 _conflict_owner 可能被写死成恒 None"
        assert tools_registry["calculator"]["source"] == src
    finally:
        unregister_source(src)
        unregister_source(other)
        tools_registry["calculator"] = original
    assert tools_registry["calculator"]["source"] == BUILTIN_SOURCE


# ---------- 10. 启动路径纪律：内置导入期零异常 ----------

def test_builtin_import_registered_everything_without_raising():
    """builtin_tools 在 import 期跑装饰器；外部清洗绝不挂在那条路上
    （看守每小时只有 3 次重启额度，导入期抛异常=现网打死）。"""
    for name in ("calculator", "web_search", "help",
                 "execute_code", "today_plan", "plan_add"):
        info = tools_registry.get(name)
        assert info is not None, f"内置 {name} 没注册"
        assert info["source"] == BUILTIN_SOURCE
        assert not inspect.iscoroutinefunction(info["function"])
