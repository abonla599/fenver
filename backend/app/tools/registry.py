# 全局工具注册表
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
            内置装饰器**不做**外部清洗、不在导入期抛异常——启动路径上注册失败会
            直接打死服务（看守每小时只有 3 次重启额度），校验闸只立外部入口。
    """
    def decorator(func):
        with _REGISTRY_LOCK:
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
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": info["description"],
            "parameters": info["parameters"]
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
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SOURCE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_:.-]{0,63}$")

_DESCRIPTION_MAX = 1024
_SCHEMA_JSON_MAX_BYTES = 16 * 1024
_SCHEMA_MAX_DEPTH = 8
_SCHEMA_MAX_NODES = 256
_SCHEMA_MAX_PROPERTIES = 64


def _check_source(source) -> None:
    """source 必须是安全字符集内的字符串，且绝不许是 builtin——那是内置保留源。"""
    if not isinstance(source, str) or not _SOURCE_RE.match(source):
        raise ValueError("非法 source：只允许 [A-Za-z][A-Za-z0-9_:.-]{0,63}"
                         "（如 mcp:<server_id>、external:<id>）")
    if source == BUILTIN_SOURCE:
        raise ValueError("builtin 是内置保留源，外部入口不许使用")


def _clean_description(description):
    """去控制字符后要求 1-1024。返回 (清洗后文本, 拒绝理由或 None)。"""
    if not isinstance(description, str):
        return None, "description 必须是字符串"
    cleaned = "".join(ch for ch in description if category(ch) != "Cc").strip()
    if not 1 <= len(cleaned) <= _DESCRIPTION_MAX:
        return None, f"description 去控制字符后须为 1-{_DESCRIPTION_MAX} 字符"
    return cleaned, None


def _walk_schema(node, depth, counter):
    """深度/节点数/properties 数/$ref 本地性/required⊆properties，返回拒绝理由或 None。

    深度按 JSON 值层级计（根=1）。这里不判 schema 合法性——那是 meta 校验的事；
    这里拦的是形状炸弹：深到能炸递归、大到撑爆上下文、外链 $ref 能把内部结构
    寄给一个模型请求路径上根本不该碰的地址。
    """
    if depth > _SCHEMA_MAX_DEPTH:
        return f"parameters 嵌套超过 {_SCHEMA_MAX_DEPTH} 层"
    counter[0] += 1
    if counter[0] > _SCHEMA_MAX_NODES:
        return f"parameters 节点数超过 {_SCHEMA_MAX_NODES}"
    if isinstance(node, dict):
        ref = node.get("$ref")
        if ref is not None and not (isinstance(ref, str) and ref.startswith("#")):
            return "只允许本地 $ref（以 # 开头）"
        props = node.get("properties")
        if isinstance(props, dict) and len(props) > _SCHEMA_MAX_PROPERTIES:
            return f"properties 字段数超过 {_SCHEMA_MAX_PROPERTIES}"
        required = node.get("required")
        if isinstance(required, list) and isinstance(props, dict):
            if any(r not in props for r in required):
                return "required 含 properties 之外的字段"
        for value in node.values():
            err = _walk_schema(value, depth + 1, counter)
            if err:
                return err
    elif isinstance(node, (list, tuple)):
        for value in node:
            err = _walk_schema(value, depth + 1, counter)
            if err:
                return err
    return None


def _validate_parameters(parameters):
    """外部条目的 parameters 清洗。返回拒绝理由或 None。"""
    if not isinstance(parameters, dict):
        return "parameters 必须是 JSON Schema 对象"
    if parameters.get("type") != "object":
        return "parameters.type 必须是 object"
    try:
        blob = json.dumps(parameters, ensure_ascii=False)
    except (TypeError, ValueError):
        return "parameters 不是可 JSON 序列化的结构"
    if len(blob.encode("utf-8")) > _SCHEMA_JSON_MAX_BYTES:
        return f"parameters JSON 超过 {_SCHEMA_JSON_MAX_BYTES} 字节"
    err = _walk_schema(parameters, 1, [0])
    if err:
        return err
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
    必须拒在注册时，而不是留在运行时。
    """
    if not isinstance(entry, dict):
        return "", None, "条目必须是 dict"
    name = entry.get("name")
    if not isinstance(name, str) or not _TOOL_NAME_RE.match(name):
        shown = name if isinstance(name, str) else ""
        return shown, None, "name 不符合 ^[A-Za-z][A-Za-z0-9_-]{0,63}$"
    func = entry.get("function")
    if not callable(func):
        return name, None, "function 缺失或不可调用"
    if inspect.iscoroutinefunction(func):
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
    cleaned = {
        "function": func,
        "description": description,
        "parameters": entry["parameters"],
        "available": available,
        "needs_user": bool(entry.get("needs_user")),
        "requires_approval": bool(entry.get("requires_approval")),
        "suspends": bool(entry.get("suspends")),
    }
    return name, cleaned, None


def register_external_tools(source: str, entries) -> dict:
    """整批注册一个外部来源的工具，锁内整源刷新。返回 {"accepted": n, "rejected": [{"name","reason"}]}。

    语义（v0.37.1 拍板）：
    - 锁外逐条清洗校验：坏条目**整条**拒，不拖垮整批，也不让坏 schema 进模型视野；
      日志只记 source/name/reason，不回显 schema 或函数内容；
    - 锁内先做撞名终判（校验与插队之间可能有人刚注册进来），再删掉该 source 名下
      全部旧条目、插入本批——同源刷新允许替换自家旧条目，本批里消失的名字随刷新一起消失；
    - 任何已存在名字（builtin 或别的 source）一律拒绝：内置永不被覆盖，来源之间
      不许互相影子；
    - source 只作账本标记，不进 _schema_of，模型看不到；
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
        name, cleaned, reason = _validate_external_entry(entry)
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
            if existing is not None and existing.get("source", BUILTIN_SOURCE) != source:
                owner = existing.get("source", BUILTIN_SOURCE)
                reason = f"名字已被来源 {owner} 占用（内置/其他来源永不被覆盖）"
                rejected.append({"name": name, "reason": reason})
                print(f"⚠️ 外部工具已拒绝：source={source} name={name} reason={reason}")
                continue
            final.append((name, cleaned))
        for stale_name in [n for n, info in dict.items(tools_registry)
                           if info.get("source") == source]:
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
