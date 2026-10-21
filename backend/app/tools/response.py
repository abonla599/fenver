"""工具执行协议的出口形状：一次调用要么给出结果、要么失败、要么**停在断点问用户**。

前两个出口是 v0.31 之前长出来的，第三个是 v0.35 A3 加的（提案 §二 第三行把"执行器是
同步函数、`ToolResponse` 一次性返回"列为本版必须自己铺的第三块地基，§四 v0.35 第三条
要的是"从同步返回扩为可挂起 → 等外部输入 → 恢复"）。

改这一份文件要先读懂的一条约束：**老字段不许变形**。`success` 仍然是 bool、
`to_string()` 的 ✓/✗ 两支仍然逐字节不变、`__init__` 的前五个参数仍然是位置可传的。
第三态一律由**新增的、带默认值的**字段说，因为本协议的既有读者遍布
（`execute_tool_detailed` 里读 `bool(response.success)`、两条聊天路径与 ReAct 读 `to_string()`
的文本），让老字段变成三态等于要求所有人在同一轮里认新形状。
"""
from typing import Any, Optional

# 给模型的文本前缀：三态三把锁，互不重叠。
# `⏸` 这一支既不是 ✓ 也不是 ✗，所以任何"按前缀猜成败"的老读法**只会**把它读成
# "这一次没产出可用结果"，绝不会读成"成功了"——失败的方向是安全的（把挂起当成功
# 才是事故：模型会替用户编一个验证码继续往下跑）。
OK_PREFIX = "✓ "
FAIL_PREFIX = "✗ 错误: "
SUSPEND_PREFIX = "⏸ "


def with_pause_marker(text: str) -> str:
    """把一段文本投影成挂起出口的文本。前缀只在本模块拼。"""
    return SUSPEND_PREFIX + str(text)


def ensure_pause_marker(text: str, suspended: bool) -> str:
    """按结构化判据补齐挂起前缀，幂等；非挂起原样返回。

    帧的 summary 与模型的文本投影共用这一处，调用方不许自己比对这个前缀。
    """
    body = str(text)
    if not suspended:
        return body
    marker = with_pause_marker("")
    return body if body.startswith(marker) else marker + body


SUSPEND_TAIL = ("〔执行已经停在断点：断点之前那几步是**真金白银跑过的**，不要重跑；"
                "把上面那句原样问用户，拿到答案后从断点继续。〕")


class ToolSuspended(Exception):
    """工具跑到某一步，发现少的是**用户手里的一个答案**（验证码、收货地址、"要不要继续"）。

    用法：工具函数体内 `raise ToolSuspended("请输入短信验证码", state={"step": 3, ...})`。

    这条异常**绝不允许飘出执行器**：`_dispatch` 就地接住、折成一个 `suspended=True` 的
    `ToolResponse`（见 `executor.py` 的 `_settle_suspension`）。理由是向后兼容的承重墙，
    不是洁癖——所有既有调用侧都写着兜底 `except Exception`（`streaming` 的非富路径、
    `pipeline.py`、`react_agent` 的 `except`），异常一旦外飘就立刻被折成一句"工具执行错误"，
    第三态当场退化回失败态，正是这一版要拆掉的形状。工具要挂起就 raise，
    raise 出去的东西由执行器负责变成值。
    """

    def __init__(self, prompt: str, state: Any = None, hint: str = ""):
        super().__init__(str(prompt or ""))
        self.prompt = prompt                 # 要问用户的那句话（缺了就挂不起，见执行器）
        self.state = state                   # 断点：第几步、已经拿到哪些参数
        self.hint = hint or ""


class ToolResponse:
    def __init__(self, success: bool, data: Any = None, error: str = "", hint: str = "",
                 artifacts: Optional[dict] = None, suspended: bool = False,
                 prompt: str = "", resume_state: Optional[dict] = None):
        self.success = success
        self.data = data
        self.error = error
        self.hint = hint
        # artifacts 是给"过程面板"用的结构化副产品（例如一次网页搜索的
        # {kind,query,results:[{title,url,snippet}]}）。它绝不进 to_string()——
        # 给模型看到的文本必须和引入这一字段之前逐字节一致，否则改的是模型输入而不
        # 只是给人看的留痕。默认 None：绝大多数工具（计算器/日程…）没有这类副产物。
        self.artifacts = artifacts
        # ⭐ v0.35 A3 第三态三件套，全部带默认值 = 既有每一处构造逐字不变。
        # suspended 与 success 是**两个问题**：success 问"这一次跑出结果了吗"，
        # suspended 问"是不是停在断点等人"。挂起时 success 仍是 False（确实没产出结果），
        # 但读侧必须先看 suspended，否则就把"问用户要验证码"记成一次工具失败。
        # 判据"能不能续跑"不在这里，由执行器一处算好（它才知道 registry 有没有声明
        # 可挂起、这份断点带不带得过重启）——见 executor._settle_suspension。
        self.suspended = bool(suspended)
        self.prompt = prompt
        self.resume_state = resume_state

    @classmethod
    def suspend(cls, prompt: str, state: Any = None, hint: str = "") -> "ToolResponse":
        """挂起出口的唯一构造口（另一个是工具直接 raise `ToolSuspended`，两条都收在这一处）。

        为什么给构造口而不是让调用方自己拼参数：`success=False` 这一格是**刻意的**，
        写在一个类方法里 + 注释，比让每个工具作者重新发明一遍"挂起时 success 该填什么"
        可靠。与本仓 `Task.wait_for_user()` 同一个做法：语义收进一个名字里。
        """
        return cls(False, suspended=True, prompt=prompt, resume_state=state, hint=hint)

    def to_string(self) -> str:
        # 顺序是判据：suspended 排在最前面。若排在 success 之后，"挂起 + 忘了改 success"
        # 那种自相矛盾的形状就会走 ✓ 那一支——把"还在等人"说成"已经办成了"，
        # 模型会直接替用户把后面的步骤编完。执行器也会把这格判成不合法，两处都堵。
        if self.suspended:
            question = str(self.prompt or "").strip() or "（工具没有说清要问用户什么）"
            tail = f"。建议: {self.hint}" if self.hint else ""
            return with_pause_marker(f"需要用户输入：{question}{SUSPEND_TAIL}{tail}")
        if self.success:
            return f"✓ {self.data}"
        return f"✗ 错误: {self.error}。建议: {self.hint}"
