# 全局工具注册表
"""全站工具注册表。

长生命周期对象（pipeline / react_agent）在**构造时**快照 schema：运行期的
注册 / 整源刷新 / 注销只影响之后构造的实例，已经构造好的会话不会看见新工具
（`help` 走实时注册表，所以它能看见）。
"""
import copy
import inspect
import json
import re
import threading
from unicodedata import category

BUILTIN_SOURCE = "builtin"

# 模块内一把 RLock：注册、整源刷新、注销、快照式遍历共用。可重入，因为
# register_external_tools 持锁后仍会经由 tools_registry 的读方法取已有条目。
_REGISTRY_LOCK = threading.RLock()


class _SnapshotRegistry(dict):
    """tools_registry 的容器：语义仍是 dict，只是把 items/keys/values/get/__contains__
    挪进锁里返回快照。

    为什么是 dict 子类而不是换协议：8 个消费点（executor、task_agent、approval、
    orchestrator……）都按普通 dict 用它——`tools_registry[name] = ...`、`.pop()`、
    `.get()`，换容器类型会让它们无谓地跟着改。真正的竞态只有一处：
    builtin_tools 的 help_tool 遍历 items() 时，另一线程做整源刷新会边迭代边删键，
    原生日报 "dictionary changed size during iteration"。快照迭代掐掉这个窗口。
    """

    def items(self):
        with _REGISTRY_LOCK:
            return list(dict.items(self))

    def keys(self):
        with _REGISTRY_LOCK:
            return list(dict.keys(self))

    def values(self):
        with _REGISTRY_LOCK:
            return list(dict.values(self))

    def get(self, key, default=None):
        with _REGISTRY_LOCK:
            return dict.get(self, key, default)

    def __contains__(self, key):
        with _REGISTRY_LOCK:
            return dict.__contains__(self, key)

    def __getitem__(self, key):
        with _REGISTRY_LOCK:
            return dict.__getitem__(self, key)


tools_registry = _SnapshotRegistry()

def register_tool(name: str, description: str, parameters: dict, available=None,
                  needs_user: bool = False, requires_approval: bool = False,
                  suspends: bool = False, source: str = BUILTIN_SOURCE):
    """
    装饰器：将函数注册为工具。
    name: 工具唯一名，如 "calculator"
    description: 工具描述，给模型看
    parameters: JSON Schema 格式的参数定义
    needs_user: 这条工具读写的是"某个具体的人"的数据。标了它，执行器会把**服务端算出来的**
               当前登录者塞进去，模型给的同名参数一律作废——否则 schema 里多一个 user_id，
               就等于请模型编一个归属人。
    requires_approval: v0.31 审批层。标了它的工具（如 execute_code）在执行前必须等用户确认。
                      确认走一次性票据，超时作废，绝不默认放行。
    suspends: v0.35 A3。标了它 = 这条工具**愿意接回一个断点**：执行器会把
             `resume={"state", "answer"}` 作为关键字参数递给它（只在这一次调用是续跑时）。
             为什么是声明而不是看函数签名猜：与 needs_user 同一条纪律——能不能把服务端
             存着的东西递进函数，是工具的**契约**，不是它的形状；靠 inspect 猜出来的
             "它有个叫 resume 的参数"会让一次改名悄悄改变协议行为。
             没标它的工具仍然**可以**挂起（raise ToolSuspended 照旧被接住、照旧问用户），
             只是那次挂起的 `can_resume` 为假：没人接得住断点，就不许许诺"能续上"。
    available: 可选，无参可调用，返回这个工具"现在能不能真跑成"。
               依赖外部条件的工具（要 Docker、要够得着某个搜索源）必须给出：
               否则模型每次都会看见它、调用它、拿回一句失败，再凭失败硬答。
    source: v0.37.1 来源标记，默认 "builtin"。只用来做整源刷新与 builtin 保护
            （见 register_external_tools）；不进 _schema_of，模型永远看不到它。
            非 builtin 来源同样要过 `_SOURCE_RE` 格式闸；目标名字已被外部来源占用时
            直接抛 ValueError——内置不许覆盖外部（builtin 重名保持既有覆盖语义）。
            内置装饰器**不做**外部清洗、不在导入期抛异常——启动路径上注册失败会
            直接打死服务（看守每小时只有 3 次重启额度），校验闸只立外部入口。
    """
    _check_source_format(source)

    def decorator(func):
        with _REGISTRY_LOCK:
            existing = dict.get(tools_registry, name)
            if existing is not None:
                owner = existing.get("source", BUILTIN_SOURCE)
                if owner != BUILTIN_SOURCE:
                    raise ValueError(
                        f"名字 {name} 已被来源 {owner} 占用，内置工具不许覆盖外部条目")
            tools_registry[name] = {
                "function": func,
                "description": description,
                "parameters": parameters,
                "available": available,
                "needs_user": needs_user,
                "requires_approval": requires_approval,
                "suspends": suspends,
                "source": source,
            }
        return func
    return decorator

def is_available(info: dict) -> bool:
    """这个工具现在能不能用。检查本身出错时按"能用"处理。

    方向要选对：误开只是多一次失败调用，误关是把一个能用的功能悄悄藏起来——
    前者用户会抱怨，后者没人会去查，所以宁可误开。
    """
    check = info.get("available")
    if check is None:
        return True
    try:
        return bool(check())
    except Exception as e:
        print(f"⚠️ 工具可用性检查失败，按可用处理：{e}")
        return True

def _schema_of(name: str, info: dict) -> dict:
    # parameters 必须深拷贝：模型清单、help、测试都从这里取，任何消费者改到返回值
    # 都不许改到注册表本体（B3）。description/name 是不可变字符串，无需复制。
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": info["description"],
            "parameters": copy.deepcopy(info["parameters"])
        }
    }

def get_all_tools_schema():
    """生成符合 OpenAI 工具格式的 schema 列表，用于 API 调用。

    注意：这是**全量**清单，不看可用性。给模型的一律用
    get_available_tools_schema()，这份留给运维和"到底注册了什么"的核对。
    """
    return [_schema_of(name, info) for name, info in tools_registry.items()]

def get_available_tools_schema():
    """给模型的清单：只含现在真能跑成的那几个。"""
    return [_schema_of(name, info) for name, info in tools_registry.items()
            if is_available(info)]


# ---------------------------------------------------------------------------
# v0.37.1 多来源（server-only）：外部来源的整批注册 / 整源刷新 / 注销
# ---------------------------------------------------------------------------

# 名字与 source 都限定在安全字符集：这两串会进日志、进模型可见的 function.name，
# 上游（mcp_client，v0.37.2）不该有机会把控制字符或路径形状的东西塞进来。
# 一律用 fullmatch：`.match` + `$` 会放行尾随换行（"xv_nl\n"、"builtin\n"）。
_TOOL_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
_SOURCE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_:.-]{0,63}")

_DESCRIPTION_MAX = 1024
_SCHEMA_JSON_MAX_BYTES = 16 * 1024
_SCHEMA_MAX_DEPTH = 8
_SCHEMA_MAX_NODES = 256
_SCHEMA_MAX_PROPERTIES = 64

# description 允许"剥离"的类别：控制/格式/行分隔/段分隔。Cs（孤立代理）与无法
# UTF-8 编码的字符不在此列——它们会直接打崩模型请求与 help 的序列化，必须拒绝。
_STRIP_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
# parameters 内的字符串键/值更严：上述五类任一出现即整条拒绝，不静默改名/改值。
_REJECT_TEXT_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

# 这些关键字的**值**是"名字 → schema"的映射容器，不是 schema 本身。遍历时必须
# 把映射的键当普通名字看：`properties: {"$ref": {...}}` 是合法的属性名，不能拿
# 它的键当 schema 关键字误拒（N5）。
_SCHEMA_MAP_KEYWORDS = frozenset({
    "properties", "patternProperties", "$defs", "definitions", "dependentSchemas",
})


def _check_source_format(source) -> None:
    """source 只做格式校验（不判保留源）：必须是安全字符集内的字符串。"""
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
        raise ValueError("非法 source：只允许 [A-Za-z][A-Za-z0-9_:.-]{0,63}"
                         "（如 mcp:<server_id>、external:<id>）")


def _check_source(source) -> None:
    """source 必须是安全字符集内的字符串，且绝不许是 builtin——那是内置保留源。"""
    _check_source_format(source)
    if source == BUILTIN_SOURCE:
        raise ValueError("builtin 是内置保留源，外部入口不许使用")


def _clean_description(description):
    """剥离 Cc/Cf/Zl/Zp 后要求 1-1024。返回 (清洗后文本, 拒绝理由或 None)。

    Cs（孤立代理）与无法 UTF-8 编码的字符一律拒绝：它们一旦进入模型可见描述，
    json 序列化 / help 打印会直接抛 UnicodeEncodeError，整份工具清单请求失败。
    """
    if not isinstance(description, str):
        return None, "description 必须是字符串"
    try:
        description.encode("utf-8")
    except UnicodeEncodeError:
        return None, "description 含孤立代理或无法 UTF-8 编码的字符"
    cleaned = "".join(ch for ch in description
                      if category(ch) not in _STRIP_CATEGORIES).strip()
    if not 1 <= len(cleaned) <= _DESCRIPTION_MAX:
        return None, f"description 去控制字符后须为 1-{_DESCRIPTION_MAX} 字符"
    return cleaned, None


def _bad_text(text: str) -> bool:
    """parameters 内的字符串是否含 Cc/Cf/Cs/Zl/Zp 或无法 UTF-8 编码。"""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return any(category(ch) in _REJECT_TEXT_CATEGORIES for ch in text)


def _walk_schema(root):
    """迭代遍历 parameters，返回拒绝理由或 None。

    显式栈带 depth，**绝不递归**：5000 层输入也要在 `json.dumps` 之前被这里拦下，
    而不是先炸 RecursionError（那样异常会跑出 register_external_tools，违反
    "条目级问题一律走 rejected 不抛"）。

    检查项：深度 / 节点数 / properties 字段数 / required 形状与
    required⊆properties / 只允许字面本地 `$ref`（不解析引用目标；`$dynamicRef`、
    `$recursiveRef` 一律拒）/ 字符串键值的字符类别与非 UTF-8 编码。
    非有限浮点不在这里判：交给 `json.dumps(..., allow_nan=False)` 的 ValueError，
    `_validate_parameters` 把它翻成可读的"parameters 含非有限数值"。

    遍历用 kind 标记容器类型：`"schema"` 是 schema 节点；`"map"` 是
    properties/$defs 这类"名字 → schema"的映射，它的键是普通名字、值才是 schema。
    """
    stack = [(root, 1, "schema")]
    nodes = 0
    while stack:
        node, depth, kind = stack.pop()
        if depth > _SCHEMA_MAX_DEPTH:
            return f"parameters 嵌套超过 {_SCHEMA_MAX_DEPTH} 层"
        nodes += 1
        if nodes > _SCHEMA_MAX_NODES:
            return f"parameters 节点数超过 {_SCHEMA_MAX_NODES}"
        if kind == "map":
            if not isinstance(node, dict):
                continue
            for key, value in node.items():
                if not isinstance(key, str) or _bad_text(key):
                    return "parameters 的键含控制/格式字符或无法 UTF-8 编码"
                stack.append((value, depth + 1, "schema"))
            continue
        if isinstance(node, dict):
            if "$dynamicRef" in node or "$recursiveRef" in node:
                return "只承诺字面本地 $ref：不允许 $dynamicRef/$recursiveRef"
            if "$ref" in node:
                ref = node["$ref"]
                if not (isinstance(ref, str) and ref.startswith("#")):
                    return "只允许本地 $ref（以 # 开头）"
            props = node.get("properties")
            if isinstance(props, dict) and len(props) > _SCHEMA_MAX_PROPERTIES:
                return f"properties 字段数超过 {_SCHEMA_MAX_PROPERTIES}"
            if "required" in node:
                required = node["required"]
                if not isinstance(required, list) or not all(
                        isinstance(r, str) for r in required):
                    return "required 必须是字符串数组"
                if required:
                    if not isinstance(props, dict):
                        return "required 非空但 properties 缺失或不是对象"
                    if any(r not in props for r in required):
                        return "required 含 properties 之外的字段"
            for key, value in node.items():
                if not isinstance(key, str) or _bad_text(key):
                    return "parameters 的键含控制/格式字符或无法 UTF-8 编码"
                if key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
                    stack.append((value, depth + 1, "map"))
                else:
                    stack.append((value, depth + 1, "schema"))
            continue
        if isinstance(node, (list, tuple)):
            for value in node:
                stack.append((value, depth + 1, "schema"))
            continue
        # 标量
        if isinstance(node, str):
            if _bad_text(node):
                return "parameters 含控制/格式字符或无法 UTF-8 编码的字符串"
    return None


def _validate_parameters(parameters):
    """外部条目的 parameters 清洗。返回拒绝理由或 None。

    顺序是刻意的：先做迭代式结构预检（深度/节点/ref/required/字符），再
    json.dumps（allow_nan=False，纵深已被封顶不会 RecursionError；非有限浮点由
    allow_nan 的 ValueError 拦下并翻成可读理由），最后才是字节数与 Draft202012
    元校验。深炸弹必须在序列化之前被拦下。
    """
    if not isinstance(parameters, dict):
        return "parameters 必须是 JSON Schema 对象"
    if parameters.get("type") != "object":
        return "parameters.type 必须是 object"
    err = _walk_schema(parameters)
    if err:
        return err
    try:
        blob = json.dumps(parameters, ensure_ascii=False, allow_nan=False)
    except ValueError as e:
        if "Circular" in str(e):
            return "parameters 存在循环引用"
        return "parameters 含非有限数值"
    except (TypeError, RecursionError):
        return "parameters 不是可 JSON 序列化的结构"
    if len(blob.encode("utf-8")) > _SCHEMA_JSON_MAX_BYTES:
        return f"parameters JSON 超过 {_SCHEMA_JSON_MAX_BYTES} 字节"
    try:
        from jsonschema.validators import Draft202012Validator
    except ImportError:
        # fail-closed：校验器缺席就不收外部工具。内置不依赖这一闸，导入期不受影响。
        return "JSON Schema 校验器不可用，外部工具拒收"
    try:
        Draft202012Validator.check_schema(parameters)
    except Exception as e:
        return f"parameters 未通过 Draft202012 元校验: {type(e).__name__}"
    return None


def _validate_external_entry(entry):
    """单条外部条目校验。返回 (名字, 清洗后的条目(不含 source), 拒绝理由或 None)。

    协程函数直接拒：执行器按同步 `func(**kwargs)` 调用外部工具，协程函数会返回
    一个没人 await 的 coroutine 对象——调用"成功"、结果永远是垃圾，这种静默坏法
    必须拒在注册时，而不是留在运行时。异步可调用对象（`__call__` 是 async def）
    同样拒。

    通过校验的 parameters 会再做一次 `json.loads(json.dumps(...))` 深拷贝后入库：
    调用方在注册后修改自己那份 dict，影响不到注册表（B3）。

    执行标记的缺省值是**产品决定**（N7）：`needs_user=False`、
    `suspends=False`，而外部来源不可信，`requires_approval` 缺省 **True**
    （显式传 False 才关闭）；三个标记都必须是真正的 bool，传 "false" 这类字符串
    整条拒绝，绝不 bool() 强转。
    """
    if not isinstance(entry, dict):
        return "", None, "条目必须是 dict"
    name = entry.get("name")
    if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
        shown = name if isinstance(name, str) else ""
        return shown, None, "name 不符合 [A-Za-z][A-Za-z0-9_-]{0,63}"
    func = entry.get("function")
    if not callable(func):
        return name, None, "function 缺失或不可调用"
    if inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(
            getattr(func, "__call__", None)):
        return name, None, "拒绝协程函数：执行器按同步方式调用，await 无人负责"
    description, err = _clean_description(entry.get("description"))
    if err:
        return name, None, err
    err = _validate_parameters(entry.get("parameters"))
    if err:
        return name, None, err
    available = entry.get("available")
    if available is not None and not callable(available):
        return name, None, "available 必须是可调用对象或省略"
    flags = {}
    for key, default in (("needs_user", False), ("requires_approval", True),
                         ("suspends", False)):
        value = entry.get(key, default)
        if not isinstance(value, bool):
            return name, None, f"{key} 必须是 bool（收到 {type(value).__name__}）"
        flags[key] = value
    try:
        frozen = json.loads(json.dumps(entry["parameters"], ensure_ascii=False,
                                       allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        return name, None, "parameters 不是可 JSON 序列化的结构"
    cleaned = {
        "function": func,
        "description": description,
        "parameters": frozen,
        "available": available,
        "needs_user": flags["needs_user"],
        "requires_approval": flags["requires_approval"],
        "suspends": flags["suspends"],
    }
    return name, cleaned, None


def _conflict_owner(existing, source):
    """撞名终判的**唯一**闸门：返回占用者 source，None = 允许写入。

    - 条目不存在 → None；
    - 同源重名 → None（整源刷新允许替换自家旧条目）；
    - 内置（含缺 source 的旧手工条目）或别的来源 → 返回占用者，调用方拒绝。

    抽成模块级小函数是有意的：负控测试把它整个换成恒 None，就能证明
    "内置/别源永不被覆盖"真的是这道闸给的，而不是断言恒真。
    """
    if existing is None:
        return None
    owner = existing.get("source", BUILTIN_SOURCE)
    if owner == source:
        return None
    return owner


def register_external_tools(source: str, entries) -> dict:
    """整批注册一个外部来源的工具，锁内整源刷新。返回 {"accepted": n, "rejected": [{"name","reason"}]}。

    语义（v0.37.1 拍板，v0.37.1 审查后补 N1）：
    - 锁外逐条清洗校验：坏条目**整条**拒，不拖垮整批，也不让坏 schema 进模型视野；
      条目级异常（含 RecursionError）一律折成 rejected，绝不抛出整批；
      日志只记 source/name/reason，不回显 schema 或函数内容；
    - 锁内先做撞名终判（校验与插队之间可能有人刚注册进来），再处理 stale：
      **stale 删除集合 = 该 source 旧名字 − 本批 accepted − 本批 rejected 且当前
      仍归该 source 的名字**。也就是说，本批校验被拒的同源旧条目会保留；全坏批次
      不会清空该 source；只有"本批没提到"的旧名字才随整源刷新消失；
    - 任何已存在名字（builtin 或别的 source）一律拒绝：内置永不被覆盖，来源之间
      不许互相影子；
    - source 只作账本标记，不进 _schema_of，模型看不到；
    - 长生命周期对象（pipeline/react_agent）在构造时快照 schema，这里只影响之后
      构造的实例；
    - 非法 source / entries 不是 list 会抛 ValueError——那是调用方的程序 bug，
      要响亮；条目级问题一律走 rejected 不抛。
    """
    _check_source(source)
    if not isinstance(entries, (list, tuple)):
        raise ValueError("entries 必须是 list/tuple，每条为 dict")

    rejected = []
    accepted = []
    seen = set()
    for entry in entries:
        try:
            name, cleaned, reason = _validate_external_entry(entry)
        except Exception as e:  # 含 RecursionError：条目级异常绝不拖垮整批
            name = entry.get("name") if isinstance(entry, dict) else ""
            if not isinstance(name, str):
                name = ""
            reason = f"条目校验异常: {type(e).__name__}"
            rejected.append({"name": name, "reason": reason})
            print(f"⚠️ 外部工具已拒绝：source={source} "
                  f"name={name or '<缺失>'} reason={reason}")
            continue
        if reason:
            rejected.append({"name": name, "reason": reason})
            print(f"⚠️ 外部工具已拒绝：source={source} name={name or '<缺失>'} reason={reason}")
            continue
        if name in seen:
            reason = "批内重名"
            rejected.append({"name": name, "reason": reason})
            print(f"⚠️ 外部工具已拒绝：source={source} name={name} reason={reason}")
            continue
        seen.add(name)
        accepted.append((name, cleaned))

    with _REGISTRY_LOCK:
        final = []
        for name, cleaned in accepted:
            existing = dict.get(tools_registry, name)
            owner = _conflict_owner(existing, source)
            if owner is not None:
                reason = f"名字已被来源 {owner} 占用（内置/其他来源永不被覆盖）"
                rejected.append({"name": name, "reason": reason})
                print(f"⚠️ 外部工具已拒绝：source={source} name={name} reason={reason}")
                continue
            final.append((name, cleaned))
        accepted_names = {name for name, _ in final}
        rejected_names = {r["name"] for r in rejected if r.get("name")}
        for stale_name in [n for n, info in dict.items(tools_registry)
                           if info.get("source") == source
                           and n not in accepted_names
                           and n not in rejected_names]:
            dict.__delitem__(tools_registry, stale_name)
        for name, cleaned in final:
            entry_record = dict(cleaned)
            entry_record["source"] = source
            dict.__setitem__(tools_registry, name, entry_record)

    return {"accepted": len(final), "rejected": rejected}


def unregister_source(source: str) -> int:
    """注销该来源的全部条目，返回移除条数。拒绝 builtin——内置只能因不再注册而消失。"""
    _check_source(source)
    with _REGISTRY_LOCK:
        stale = [n for n, info in dict.items(tools_registry)
                 if info.get("source") == source]
        for name in stale:
            dict.__delitem__(tools_registry, name)
    return len(stale)
