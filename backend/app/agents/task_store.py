"""任务状态存储：一个 JSON 文件 + 一把锁 + 原子替换。

文件头以前写着"当前使用内存字典存储，后续可升级为 Redis 或数据库"——那份内存字典
意味着任何一次重启都把任务清空，而 `/v1/tasks/{id}` 会理直气壮地回 404。规划书阶段一
的判据是"关掉服务器再开起来，昨天的任务还在，且属于正确的人"，所以这一步不引 Redis、
不加队列：与 sessions/providers 同一份写法就够了，将来真要换存储，换的是这个文件。

一处诚实的边界：**落盘的时机是"任务被放进存储"、"经由删除/取消接口改动"，以及
（v0.35 A1 起）"状态经 `Task.set_status()` / `wait_for_user()` / `mark_cancelled()`
改动"**。编排循环里对 `task.status` / `results` 的**字段级**改动仍然不经过这里，
因此不会每一步都写盘（那条路径 v0.25 R1 起对登录用户开放，步级落盘与续跑是后面
几版的事，这里刻意不提前做）。v0.35 A1 补上的是承重的那一格——`waiting_user` 若只
活在内存里，判据 2"重启后仍在"就无从谈起，补法正是文件头原先承诺的那一条：
让状态变更走一个显式的 `save(task)`，而不是在 getter 里藏写盘。

v0.35 A5 在这份存储里加的是**读侧的秒数**与一条作废跳转（文件中部那一族
`is_waiting_timed_out` / `expire_parked_task` / `sweep_timed_out_parks`）：默认窗口
0 = 永不作废，所以默认形态下这份文件**一次多余的盘都不写**；运维把窗口设成正数
之后，作废走的仍是 `set_status` → `save` 这条既有路（同锁、同批、同一份原子替换），
跳转表一格未动。
"""
import json
import math
import os
import threading
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import List, Optional

from app.core.atomic_write import write_json_atomic
from app.core.paths import data_root


def _now() -> datetime:
    """墙上时钟的**唯一**出口（v0.35 A5）。

    为什么要有这一格：`waiting_since` 记的是时刻，而"挂多久了""到点了没"都是拿它与
    "现在"作差。判"超时"这件事要能被测试确定地推进（挂 5 分钟、挂 30 天），而不能靠
    真等——真等既慢又测不到东西。函数级的接缝（monkeypatch `_now`）比往 `Task` 上
    挂一个 `now` 参数更省：调用方一路都要带着那个 `now` 走，漏一处就测不到。

    诚实的边界：这一格用的是 `datetime.now()`（**本地墙上时钟**），不是单调时钟——
    与 A1 写进 `waiting_since` 的那一份同一个域。运维把系统时钟往回拨，一条已经挂了
    很久的停点会算出负数或更小的秒数；负数在下面**一律不判过期**（`is_waiting_timed_out`），
    方向是"少销毁一次"，不是"多销毁一次"。要换单调域就得连 `waiting_since` 的盘上格式
    一起换（那是跨重启的判据，不属于这一格）。
    """
    return datetime.now()


class TaskStatus(str, Enum):
    """任务状态枚举"""
    PENDING = "pending"          # 待执行
    RUNNING = "running"          # 执行中
    # ⭐ v0.35 A1：本轮跑到某一步停下来、要等人给一个答案。**这是本版新增的唯一一个
    # 状态字面量**，而且是刻意只有一个：提案 §四 v0.35 那行列的 `needs_input` 与
    # `resumable` 都没有落地——`resumable` 这个词在本仓已经有两个别的含义
    # （`core/stream_runs.py:248` 的函数 `resumable()`、`:291/:301` 的 JSON 布尔
    # `resumable`，以及安卓壳 `Api.kt:144/553` 那个不是一回事的 Boolean），再拿它当
    # 状态名等于让下一个读代码的人猜是哪一面。"能不能续跑"在这份存储里是**布尔字段**
    # `can_resume`，不是状态。"超时不响应"也不是新状态：提案 §四写的是 needs_input，
    # 而它的语义（挂起保活、不是失败）就是 waiting_user 本身，用一个状态说一句话。
    WAITING_USER = "waiting_user"
    COMPLETED = "completed"      # 已完成
    FAILED = "failed"            # 执行失败
    CANCELLED = "cancelled"      # ⭐ 新增：已取消


class IllegalTaskTransition(ValueError):
    """状态机之外的跳转。

    继承 ValueError 与 `Task.__init__` 拒绝空归属同一形状：调用方不必认一个新异常
    类型，也已经站在 fail-closed 那一侧。跳转不合法时**内存一格都不改**——半个跳转
    （状态换了、游标没换）比不跳更糟。
    """


# ⭐ v0.35 A1：合法跳转的**唯一一份**定义。判据不散进各调用方的记忆里
# （approval.py 的 is_granted 讲的正是同一个教训：闸门要挂在数据上，不是挂在人身上）。
#
#   pending      → running / cancelled
#   running      → running / waiting_user / completed / failed / cancelled
#   waiting_user → running / completed / failed / cancelled     # 进得来也出得去
#   completed    →（无出口，终态）
#   failed       → running                                      # 断点重跑，既有语义
#   cancelled    →（无出口，终态）
#
# 三处刻意的决定：
# - `pending → waiting_user` **不走**。任务一步都没跑，没有"停在哪儿"可记，放进去只会
#   造出一个没有游标的等待态，而游标才是这一版要解决的那个缺口。
# - `running → running` 自环合法：被重启打断的进行中任务续跑时正是这个形状
#   （见 restore() 里的 interrupted_by_restart），禁掉它等于让每次续跑先炸一次。
# - 终态不出边。注意这份表答的是"能不能跳"，**不是** `/v1/tasks/{id}/cancel` 那句
#   "要不要取消"（main.py 那边把 failed 也算作无需取消的终态，两问不同，不合并）。
ALLOWED_TRANSITIONS = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.RUNNING, TaskStatus.WAITING_USER,
                                   TaskStatus.COMPLETED, TaskStatus.FAILED,
                                   TaskStatus.CANCELLED}),
    TaskStatus.WAITING_USER: frozenset({TaskStatus.RUNNING, TaskStatus.COMPLETED,
                                        TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset({TaskStatus.RUNNING}),
    TaskStatus.CANCELLED: frozenset(),
}


def _as_status(value) -> TaskStatus:
    """把枚举/字面量收成 TaskStatus，认不出就抛——不猜、不兜 default。

    "猜一个 pending" 是这一族最贵的错：一个正在等用户输入的任务被认成待执行，
    列表里它就永远不再需要任何人。
    """
    if isinstance(value, TaskStatus):
        return value
    return TaskStatus(value)


def can_transition(current, target) -> bool:
    """这一跳合不合法。给测试与读代码的人用；写路径走 Task.set_status。"""
    return _as_status(target) in ALLOWED_TRANSITIONS[_as_status(current)]


class Task:
    """任务数据结构。

    `user_id` 是必填的：本项目已经为"跨用户读到别人的东西"付过一次账（记忆泄露那回），
    所以一个不知道属于谁的任务在这里等于拒绝创建，而不是"先记着，读的时候再说"。

    `provider_id` 记录**发起人下单时**用的是哪条模型服务：账要能对上配置，
    排障时"这任务怎么烧的钱"不该靠考古。它只是记录，每次调用实际用谁仍由
    resolve 按发起人当下的池子现判（条目被删就按既有语义回落）。
    `legacy` 是启动回填的印记：归属维度上线之前躺在盘上的条目，恢复时被认给
    部署者主账号——列表里它们是历史，不是谁的现在。

    ⭐ v0.35 A1 三件套（`waiting_user` 的配套，全部随 to_dict 落盘）：
    - `resume_cursor`：断点位置与已收集参数（`wait_for_user()` 写它）。挂起之所以有用，
      全靠这一份"跑到哪儿停住、手里已经有什么"；只有状态没有游标的话，续跑的人只能
      从头再烧一次钱。
    - `can_resume`：**布尔字段**而不是状态字面量（见 TaskStatus 上那段注释为什么不叫
      resumable）。不变式：为真当且仅当状态是 waiting_user。
    - `waiting_since`：进入等待的时刻（ISO 文本），"挂起保活多久"要有起点才可判。
    """

    def __init__(self, goal: str, subtasks: List[str] = None, user_id: str = None,
                 provider_id: str = None):
        if not (user_id or "").strip():
            raise ValueError("任务必须有归属人：user_id 不能为空")
        self.task_id = str(uuid.uuid4())
        self.user_id = user_id.strip()                  # 属于谁
        self.provider_id = (provider_id or "").strip() or None   # 发起人选的模型服务
        self.legacy = False                             # 回填认领的历史条目
        self.goal = goal                                # 用户最初的目标
        self.subtasks = subtasks or []                  # 计划子任务列表
        self.current_subtask = 0                        # 当前执行到第几个子任务
        self.results = []                               # 每个子任务的执行结果
        self.status = TaskStatus.PENDING                # 当前状态
        self.created_at = datetime.now().isoformat()    # 创建时间
        self.final_answer = None                        # 最终汇总答案
        self.error = None                               # 错误信息（如果失败）
        self.cancelled = False                          # ⭐ 新增：取消标志
        # ⭐ v0.24 R3：重启前挂着没跑完的任务，恢复后带这个标注。列表可查、
        # 绝不自动续跑——编排上下文（模型、中间结果）早没了，假装续跑比明说
        # "被打断过"更坑人。
        self.interrupted_by_restart = False
        # ⭐ v0.35 A1：waiting_user 的配套三件（默认值就是"老库迁移"的答案——
        # 现网那份 tasks.json 里没有任何一条有这些键，缺省必须正好是"没在等人"）
        self.resume_cursor = None                       # 断点位置 + 已收集参数
        self.can_resume = False                         # 布尔字段，不是状态
        self.waiting_since = None                       # 进入等待的时刻

    def mark_cancelled(self) -> None:                   # ⭐ 新增方法
        """标记任务为已取消。走状态机（v0.35 A1）：终态上的"再取消一次"现在会抛。

        两个调用点本来就不在终态上调它（main.py 的取消端点先用
        `task.status in [COMPLETED, FAILED, CANCELLED]` 挡了一道，
        orchestrator.py:110 是在 RUNNING 的循环里），所以这不是行为变化，是把
        "只有非终态能被取消"这句一直存在的口头约定变成代码里的判据。
        """
        self.cancelled = True
        self.set_status(TaskStatus.CANCELLED)

    def set_status(self, new_status, persist: bool = True, **fields) -> TaskStatus:
        """状态机的唯一出口：跳转不合法就抛，合法才改，改完（默认）落盘。

        为什么需要一个方法而不是继续 `task.status = ...`：字段级赋值绕过落盘（那份
        文件开头道歉的正是这件事），也让"谁能进 waiting_user"没有唯一答案。

        `**fields` 是"随这一跳一起落下"的配套字段——waiting_user 的游标与等待时刻走的
        就是这条。这个参数是为了**原子性**：先赋游标、后判跳转的话，被拒绝的那一跳会在
        内存里留下"有游标却没在等人"的半个形状（第一条真跑这行的测试就是这么抓出来的）。
        不合法的跳转，self 一格都不改。

        落盘走 `save()`，于是复用既有的"改表与落盘同一把锁"（__setitem__）与原子替换
        （_write_unlocked → core/atomic_write），这里不自己写文件、不自己加锁。
        """
        target = _as_status(new_status)
        current = _as_status(self.status)
        if target not in ALLOWED_TRANSITIONS[current]:
            raise IllegalTaskTransition(
                f"非法的任务状态跳转：{current.value} → {target.value}"
                f"（任务 {self.task_id}）。合法出口："
                f"{sorted(s.value for s in ALLOWED_TRANSITIONS[current])}")
        for name, value in fields.items():
            # 拼错的字段名当成新属性静默挂上去，是"写了但读不回来"那一类错的另一种：
            # to_dict 不认识它，下一次 restore 就凭空少了这一格。
            if not hasattr(self, name):
                raise AttributeError(f"Task 没有这个字段：{name}")
            setattr(self, name, value)
        self.status = target
        # 不变式：can_resume 为真 ⟺ 正在等用户。离开 waiting_user 时游标留着
        # （它就是"上次停在哪儿"），但"能续"这件事此刻不成立；等待时刻同理清掉。
        self.can_resume = target is TaskStatus.WAITING_USER
        if not self.can_resume:
            self.waiting_since = None
        if persist:
            save(self)
        return target

    def wait_for_user(self, question: str, cursor: dict = None) -> TaskStatus:
        """挂起等用户：running → waiting_user，并把断点与已收集参数一起落盘。

        游标是一张自由 dict，不是新表：v0.35 之后那条"可挂起的工具执行协议"
        （提案 §四 v0.35 第三条）要往里加键（第几步工具、call_id、已拿到的参数），
        不该每加一个键就回来改 to_dict/from_dict——那两个函数是本存储的承重墙。
        三个键由这里兜住，保证任何一条 waiting_user 记录都答得出"断在哪"：
        `subtask_index`（第几个子任务）、`subtask_total`、`question`（在等什么）。
        游标先在**局部**拼好再交一跳：见 set_status 的原子性那一段。
        """
        merged = dict(cursor or {})
        merged.setdefault("subtask_index", self.current_subtask)
        merged.setdefault("subtask_total", len(self.subtasks))
        merged.setdefault("question", str(question or ""))
        return self.set_status(TaskStatus.WAITING_USER,
                               resume_cursor=merged,
                               waiting_since=_now().isoformat())

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "user_id": self.user_id, "goal": self.goal,
            "provider_id": self.provider_id, "legacy": bool(self.legacy),
            "subtasks": list(self.subtasks), "current_subtask": self.current_subtask,
            "results": list(self.results),
            # .value 而不是 str()：`class TaskStatus(str, Enum)` 的 str() 给的是
            # "TaskStatus.PENDING"，恢复回来时 TaskStatus(...) 认不出它——写进去的
            # 东西读不回来，就是这一版要修的同一类错。
            "status": getattr(self.status, "value", self.status),
            "created_at": self.created_at, "final_answer": self.final_answer,
            "error": self.error, "cancelled": bool(self.cancelled),
            "interrupted_by_restart": bool(self.interrupted_by_restart),
            # ⭐ v0.35 A1：挂起三件套。can_resume 存**布尔**而不是"读侧看状态猜"——
            # 列表/详情/续跑判断要的是一句现成的话，让它去比 status 字面量等于把
            # 不变式复制进第二个文件，那是本仓反复在拆的形状。
            "resume_cursor": self.resume_cursor,
            "can_resume": bool(self.can_resume),
            "waiting_since": self.waiting_since,
        }

    @staticmethod
    def from_dict(data: dict) -> "Task":
        # 不复用 __init__ 的 uuid 生成：恢复回来的是同一个 task_id，否则重启之后
        # 手里还握着旧 id 的人只会拿到 404。
        task = Task(goal=data.get("goal", ""), user_id=data.get("user_id"),
                    provider_id=data.get("provider_id"))
        for field in ("task_id", "subtasks", "current_subtask", "results", "created_at",
                      "final_answer", "error"):
            if field in data:
                setattr(task, field, data[field])
        task.legacy = bool(data.get("legacy", False))
        task.status = TaskStatus(data.get("status", TaskStatus.PENDING))
        task.cancelled = bool(data.get("cancelled", False))
        task.interrupted_by_restart = bool(data.get("interrupted_by_restart", False))
        # ⭐ v0.35 A1 迁移：这三件是**新键**，现网那份 tasks.json 里一条都没有。缺键不是
        # 错误，走 __init__ 的默认（None/False/None，语义是"没在等任何人"）；形状不对
        # 才是错误，单独说一句再按缺省跑——与 config_store.credit_benchmark() 的逐键
        # 消毒同纪律：坏的那一键退回默认，同一条记录里其余的好键不许被它带走。
        cursor = data.get("resume_cursor", None)
        if cursor is not None and not isinstance(cursor, dict):
            print(f"⚠️ 任务 {task.task_id} 的 resume_cursor 不是一张 dict"
                  f"（收到 {type(cursor).__name__}），按没有游标跑")
            cursor = None
        task.resume_cursor = cursor
        task.waiting_since = data.get("waiting_since", None)
        # 不变式在恢复这一侧同样成立：waiting_user 就可续（哪怕盘上那一格被手改成
        # false），反过来非 waiting_user 的 true 一律不认。
        task.can_resume = task.status is TaskStatus.WAITING_USER
        return task


def _default_path() -> str:
    env_path = os.getenv("TASKS_DB_PATH")
    if env_path:
        return os.path.abspath(env_path)
    return os.path.join(data_root(), "data", "tasks.json")


_lock = threading.RLock()   # 可重入：写盘时可能正持着同一把

# 无主旧条目的认领人：一个字面量都不再新写——"部署者主账号"这个身份在仓库里
# 只许有一份定义（判据锁在 tests/test_feedback_loop.py：LEGACY 字面量只许
# session_store 一处赋值），这里与 uploads 同一个做法：真引用 SessionStore 那份。
from app.session.session_store import SessionStore

LEGACY_OWNER = SessionStore.LEGACY_OWNER


class _TaskStore(dict):
    """写进字典就等于落盘。

    为什么挂在 __setitem__ 而不是让每个调用方记得 save：现成的写入点
    （`orchestrator.py` 建任务那句）本来就是 `task_store[task.task_id] = task`，
    要求所有将来的人多记一步，等于赌没人忘——而"忘了那一步"的症状是重启后
    静悄悄少一批任务，正是这份文件开头在道歉的那个形状。
    """

    def __setitem__(self, key, value):
        # 改表与落盘必须在同一把锁里（审查 #12）：_flush 持锁遍历 items()，
        # 旧写法改表却在锁外——两个编排线程同时"放进存储"，正读表的那个
        # 就能撞上 dictionary changed size during iteration。锁是 RLock，
        # _flush 再进一次不成问题。
        with _lock:
            super().__setitem__(key, value)
            _flush()

    def __delitem__(self, key):
        with _lock:
            super().__delitem__(key)
            _flush()


task_store = _TaskStore()


def save(task: "Task") -> "Task":
    """把一份内存改动落到盘上：显式一步，而不是在 getter 里藏写盘。

    这份文件开头承诺的补法就是它。走 `__setitem__` 因此顺带拿到既有的两条纪律
    （改表与落盘同一把锁 :133 区域、原子替换 _write_unlocked），这里不再写第二套。

    任务还没进存储时**不落盘**：那时候 `task_store[id] = task` 等于把一个半成品当成
    新条目插进表里（并触发一次全量写盘），而调用方接下来还会再放一次。
    """
    if task.task_id in task_store:
        task_store[task.task_id] = task
    return task


# ---------- ⭐ v0.35 A5：超时是"还在挂"，不是失败 ----------
#
# 提案 §四 v0.35 那一行写的是「超时语义：用户不响应**不是失败**，是 `needs_input`
# （挂起保活）」，验收判据 1 写的是「跑到第 3 步需要验证码的任务能挂起 5 分钟，
# 用户输入后从第 4 步继续，全上下文不丢」。A3 交付了"挂得起也接得回"（它的测试文件
# 开头就把"5 分钟"那一半交还给本票），所以 A5 只管**时间**那一半。两处措辞按 A1
# 已经落地的形状读，两条都不按字面抄：
#
# - `needs_input` 没有落地，这一格也不新加字面量：本版仍然只有一个等待态
#   `waiting_user`（见 TaskStatus 上面那段），"挂起保活"说的就是这个状态本身。
# - 「超时」在这里指**这条任务自己挂了多久**（`waiting_since` 到现在的秒数），
#   **不是**流式那条路上"没有读者的宽限期"。后者是
#   `config_store.stream_no_reader_grace_seconds`（默认 120 秒），管的是「没有活读者
#   就不起飞新一轮」那道钱闸，A5 一个字不动它（判据锁在
#   `tests/test_v025_r3b_no_reader_billing.py`）。两条路回答的其实是同一个问题
#   ——「没人等的时候要不要继续花钱」——而答案刻意不对称：流式那边**正有**模型调用
#   在飞，所以到点要停手；任务这边挂在断点上时一个调用都没在飞、一分钱都没在花，
#   所以到点既不停手也不改状态，**停着本身就是零开销**。把这条不对称抹平
#   （"超时就把挂起的任务判 failed"）省不下一分钱，只会让用户回来发现断点没了、
#   只能从第 1 步再烧一遍——那正是提案 §二 第一行写的"问了就是整轮失败"。
#
# 一条诚实的边界（与 `orchestrator._EXECUTING` 那段同一个形状）：这一族的判据是
# **本进程内存里的那张表**。多 worker 各持一份 tasks.json 的内存视图，作废与续跑
# 都只在自己那一进程内串行；现网是单进程（`run_backend.py:63` 那句 `uvicorn.run`
# 不带 workers），跨进程的同一枚停点还归不了一。已作为 follow-up 上报，不属于这一格。


def _status_of(task: "Task") -> Optional[TaskStatus]:
    """这条任务的状态，认不出回 None（不猜）。

    `_as_status` 抛 ValueError 是给**写路径**用的（写坏状态要响）；这里在读路径上，
    一条读不懂的记录该被当成"判不出挂了多久"，而不是让一次 GET 变 500。
    """
    try:
        return _as_status(task.status)
    except (ValueError, AttributeError):
        return None


def _window_seconds(timeout_seconds=None) -> float:
    """把"要用的作废窗口"归成一个**非负有限**秒数。坏值一律收成 0（= 关）。

    `None` = 现读 `config_store.task_waiting_user_timeout_seconds()`。这一格与 TTL 那
    两格方向相反，所以兜底也相反：那两格的坏值会倒向"更严"，而这里的任何不确定都
    不许倒向"销毁"——0 是"永不做废"，是这里唯一安全的兜底。
    """
    if timeout_seconds is None:
        return task_waiting_user_timeout_seconds()
    try:
        value = float(timeout_seconds)
    except (TypeError, ValueError):
        print(f"⚠️ 停点作废窗口不是数（{timeout_seconds!r}），按不作废跑")
        return 0.0
    if not math.isfinite(value):        # inf / nan：比较起来恒 False，不能让它穿过判据
        return 0.0
    return max(0.0, value)


def task_waiting_user_timeout_seconds() -> float:
    """作废窗口的读数口径：单一真相住在 `core/config_store.py` 那一格，这里只是现读。

    函数内导入（不在文件头 import config_store）：`paths.resolve_all_data_paths()`
    会在函数内导入 task_store，模块级互相引用会把导入顺序变成一份债。
    """
    from app.core import config_store
    return config_store.task_waiting_user_timeout_seconds()


def parse_waiting_since(value) -> Optional[datetime]:
    """把 `waiting_since` 认回一个 naive 本地 datetime；认不出回 **None**（不猜、不抛）。

    认不出 = "不知道它挂了多久"，而"不知道"在这一族里永远不等于"可以作废"。
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        # 盘上那份是 naive 本地时刻（`_now().isoformat()`）。带时区的一条按**同一个域**
        # 读：先换到本机时区再去掉 tzinfo——naive 与 aware 直接作差是 TypeError，会被
        # 当成"读不懂"，一条本来判得出的停点就白判了。
        try:
            parsed = parsed.astimezone().replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            return None
    return parsed


def waiting_elapsed_seconds(task: "Task", now: datetime = None) -> Optional[float]:
    """这条任务已经在断点上挂了多少秒。答不出来回 None（**≠ 0 秒**）。

    两种 None 都是实话：它此刻不在等待态（没有"挂了多久"这回事），或 `waiting_since`
    那一格读不懂（起点没记上，宁可说不知道）。可能为负：系统时钟被往回拨时就是这个
    形状，而 `is_waiting_timed_out` 只认 `>= 正窗口`，负数永远不作废。
    """
    if _status_of(task) is not TaskStatus.WAITING_USER:
        return None
    start = parse_waiting_since(getattr(task, "waiting_since", None))
    if start is None:
        return None
    reference = _now() if now is None else now
    return (reference - start).total_seconds()


def waiting_deadline(task: "Task", timeout_seconds=None,
                     now: datetime = None) -> Optional[datetime]:
    """这条停点该在哪一刻作废。None = 此刻没有到期时刻（不在等待态 / 窗口关 / 起点读不懂）。"""
    window = _window_seconds(timeout_seconds)
    if window <= 0 or _status_of(task) is not TaskStatus.WAITING_USER:
        return None
    start = parse_waiting_since(getattr(task, "waiting_since", None))
    if start is None:
        return None
    return start + timedelta(seconds=window)


def is_waiting_timed_out(task: "Task", now: datetime = None,
                         timeout_seconds=None) -> bool:
    """这条挂起的任务是否**已经**超过作废窗口。每一条判据都朝"不作废"那一侧倒。

    fail-closed 的四格：窗口 ≤0（默认就是这一格：0 = 永不作废）、不在等待态、
    起点读不懂、秒数还不够。反过来，只有"窗口是正数且挂的秒数 ≥ 它"才回 True。
    """
    window = _window_seconds(timeout_seconds)
    if window <= 0:
        return False
    elapsed = waiting_elapsed_seconds(task, now=now)
    if elapsed is None:
        return False
    return elapsed >= window


def expire_parked_task(task: "Task", now: datetime = None, timeout_seconds=None,
                       reason: str = None) -> bool:
    """把一条挂太久的停点作废：**cancelled + 游标清空**，同一跳落盘。

    True 只在这一跳真的成立时给。False 的三种情形（没超时 / 被并发抢先定了论 / 落盘
    坏掉）里任务**一格都没改**，还是那条能续的 waiting_user——销毁失败不许顺带把
    "还没作废"说成"已作废"。

    ⭐ 为什么是 `cancelled` 而不是 `failed`——这一条就是本票与工单 #33 的接缝：
    `failed` 在 A1 的跳转表里有一条出边 `failed → running`（断点重跑，既有语义，见
    `ALLOWED_TRANSITIONS[TaskStatus.FAILED]` 那一行）。一条 `failed` 却还带着
    `kind=="tool_suspension"` 游标的记录因此谁都能再把它从第 1 步点着跑一遍——查店、
    加购、再发一次验证码，那正是 #33 说的重复付费形状（`orchestrator.run` 的 FAILED
    分支不读游标，直接进通用续跑）。
    `cancelled` 在表里**一条出边都没有**（`ALLOWED_TRANSITIONS[TaskStatus.CANCELLED]`
    是空集），作废之后没有任何代码路径能再把它跑起来；这一格另外还把游标本身清空
    （`resume_cursor=None`），于是即便将来有人在 cancelled 上开出口，手里也没有一份
    "看起来还能接"的断点。两道一起才是 fail-closed。

    与 A1 三条既有纪律不冲突：走 `set_status`（判、跳、落盘同锁同批，见那个方法自己的
    文档）、`can_resume` 由不变式跟着翻成 False、`waiting_since` 由同一句清掉，
    **跳转表一格未动**（`waiting_user → cancelled` 本来就在表里）。
    """
    window = _window_seconds(timeout_seconds)
    with _lock:
        if not is_waiting_timed_out(task, now=now, timeout_seconds=window):
            return False
        elapsed = waiting_elapsed_seconds(task, now=now) or 0.0
        text = reason or (
            f"在断点上挂了 {elapsed:.0f} 秒，超过运维设的上限 {window:.0f} 秒："
            "停点已作废（resume_cursor 已清空，答案递进去也接不回去）。"
            "要接着办请重新发起一次任务——这一步没有重跑，也没有再花钱。")
        # set_status 的落盘在**改完内存之后**（先 `self.status = target`，最后才 save），
        # 所以 OSError 抛出来时内存已经是 cancelled + 游标清空的那一份了。不还原的话
        # 症状是"盘上还在等、内存里没游标"——调用方接着往下走会读不到断点，掉进
        # 通用续跑那一路把第 1~3 步重烧一遍（工单 #33 那一笔钱）。与
        # `orchestrator._record_failure` 里"跳一半失败要把 error 吐回去"同一条纪律：
        # 先抄这几格，写不回去就原样还回去。
        snapshot = (task.status, task.resume_cursor, task.cancelled, task.error,
                    task.waiting_since, task.can_resume)
        try:
            task.set_status(TaskStatus.CANCELLED,
                            resume_cursor=None, cancelled=True, error=text)
        except Exception as e:
            (task.status, task.resume_cursor, task.cancelled, task.error,
             task.waiting_since, task.can_resume) = snapshot
            print(f"⚠️ 任务 {task.task_id} 的停点作废那一跳落不了盘"
                  f"（{type(e).__name__}: {e}）：内存一格未还，它照旧挂在断点上等人")
            return False
        print(f"⏳ 任务 {task.task_id} 的停点超时作废（挂了 {elapsed:.0f}s > 上限 "
              f"{window:.0f}s）：翻 cancelled、游标清空；没重跑子任务、没叫模型")
        return True


def sweep_timed_out_parks(now: datetime = None, timeout_seconds=None,
                          tasks: List["Task"] = None,
                          skip_task_ids=()) -> List[str]:
    """把挂太久的停点挨个作废，返回被作废的 task_id 列表。惰性清扫：**没有定时器**。

    与 `core/approval.py:_sweep_locked` 同一个做法（那里是"每个入口都扫"），与
    `core/stream_runs.py` 的"清扫挂在新 run 创建时"同一个形状：本仓不起后台线程
    （判据见 `tests/test_v0350_a3_suspendable_tool_protocol.py` 末尾那条
    `test_no_new_background_threads_timers_or_loop_blocking_io`），所以触发点是
    **走进任务面的那一次请求**（`Orchestrator.run` 里认领之前的那一步），不是时钟。

    默认窗口 0 → 头一句就返回 []：既不遍历也不写盘，**今天的可观察行为一字不变**。

    `skip_task_ids` = 这一轮不动的那几条 id。编排器传的是**本趟请求正要处理的那一条**：
    它由后面的 `_expire_if_timed_out` 处理，那里能给调用方一句"你的答案没被采用，因为
    停点超时作废了"（`reason=waiting_timeout`）；清扫先一步把它作废的话，调用方拿到的
    会是那句通用的"任务已被取消"——同一件事实，前者说得更准。跳过不改变结果，只改变
    那一句由谁写。

    `tasks` 是给测试与将来的调用方留的一圈：默认扫进程内那张表；传一份**手工造的列表**
    就能在不碰真库的前提下判"这条扫得动"。本机这个目录同时是生产机，所以测试里绝不
    把真的 `TASKS_DB_PATH` 放进环境——判据要验的是扫法，不是写盘。
    """
    window = _window_seconds(timeout_seconds)
    if window <= 0:
        return []
    skip = set(skip_task_ids or ())
    rows = list(task_store.values()) if tasks is None else list(tasks)
    voided: List[str] = []
    for task in rows:
        if getattr(task, "task_id", None) in skip:
            continue
        try:
            if expire_parked_task(task, now=now, timeout_seconds=window):
                voided.append(task.task_id)
        except Exception as e:
            # 一条作废不动不许带走其余的，也不许把调用它的那一次请求带崩（那的症状是 500）。
            print(f"⚠️ 任务 {getattr(task, 'task_id', '?')} 的停点作废扫不动"
                  f"（{type(e).__name__}: {e}）：跳过这一条，其余照扫")
    return voided


def waiting_timeout_info(task: "Task", now: datetime = None,
                         elapsed: bool = True) -> dict:
    """读侧那三个数：挂了多久 / 窗口多长 / 哪一刻作废。**纯计算，一个字节都不写盘**。

    为什么必须是纯的：详情端点是 `async def`，往里加一次落盘就是往事件循环上放阻塞
    I/O——那正是 `tests/test_event_loop_not_blocked.py` 文件头那次线上 524 的形状。
    清扫因此只挂在同步 def 的 `/v1/agent/orchestrate` 上，读侧只把此刻算得出来说出来。

    `elapsed=False` 那一档给**编排器**用，理由是本仓一条既有判据：A3 的
    `test_a_stranger_answering_a_parked_task_gets_the_same_answer_as_a_missing_id`
    要求"非属主"与"不存在的 id"两次拿到**逐字节相同**的一句话。挂了多久这一格是拿
    `now` 作差出来的，同一条任务隔一毫秒读两次就不一样——它进了那个响应，那句"同一
    句话"就永远对不齐（本票第一次就是这么把 A3 弄红的，改的是这里而不是那条测试）。
    所以：**编排器的响应只报政策（窗口 + 到期时刻），"现在过了多久"归 GET 详情**。

    三个键按 A1 的口径都是**只增**：读不到就给 None，老客户端一个比特都不用改。
    `waiting_expires_at` 给 None 就是"永不做废"，这正是默认形态——UI 那半边（超时
    倒计时，提案 §四 第四条）要渲染的也就是这一格。
    """
    reference = _now() if now is None else now
    window = _window_seconds(None)
    deadline = waiting_deadline(task, timeout_seconds=window, now=reference)
    info = {
        "waiting_timeout_seconds": window,
        "waiting_expires_at": deadline.isoformat() if deadline is not None else None,
    }
    if elapsed:
        info["waiting_elapsed_seconds"] = waiting_elapsed_seconds(task, now=reference)
    return info


def _flush() -> None:
    with _lock:
        _write_unlocked()


def _write_unlocked() -> None:
    payload = {"tasks": {tid: t.to_dict() for tid, t in task_store.items()}}
    # 写法收编进公共函数（tmp+fsync+replace），与其余七家同源；判据见
    # tests/test_v024_foundation_contract.py。
    write_json_atomic(_default_path(), payload)


def restore(path: str = None) -> int:
    """从盘上把任务读回来（启动时一次；测试用它等价于"重启进程"）。"""
    target = os.path.abspath(path or _default_path())
    with _lock:
        task_store.clear()
        try:
            with open(target, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return 0
        except (ValueError, OSError) as e:
            # 读不懂不该让进程起不来，也不该悄悄当成"没有任务"：留一份备份，
            # 与 providers 那套同一形状——坏掉的东西要看得见，不能被当成空。
            backup = target + ".corrupt"
            try:
                os.replace(target, backup)
                print(f"⚠️ 任务存储读不了（{e}），已备份为 {backup}")
            except OSError:
                print(f"⚠️ 任务存储读不了且无法备份（{e}）")
            return 0
        items = (data or {}).get("tasks") or {}
        # ⭐ v0.25 R1：归属维度之前写下的条目没有 user_id。判据是"缺或不等于有"
        # 而不是 "user_id" not in record（与会话/附件回填同一口径）：手工写成 null
        # 也算没迁完，放过去会让它永远等于谁的都不像。认领给部署者主账号并打
        # legacy 标——老数据不许崩，老用户也不该开页看到一片空。
        backfilled = 0
        for record in items.values():
            if not isinstance(record, dict):
                continue                     # 坏记录留给下面的逐条 try 报给人看
            uid = record.get("user_id")
            if not (uid.strip() if isinstance(uid, str) else ""):
                record["user_id"] = LEGACY_OWNER
                record["legacy"] = True
                backfilled += 1
        for tid, record in items.items():
            try:
                task = Task.from_dict(record)
                # 文件里的键才是 id 的权威：早期条目没有 task_id 字段时，
                # from_dict 会现造一个 uuid，内存键与对象 id 从此两个答案——
                # 列表给 uuid、详情按键 404，正是"恢复回来却读不到"的形状。
                task.task_id = tid
                # 直接塞进父类：这里逐条写盘毫无意义，恢复完再统一一次
                dict.__setitem__(task_store, tid, task)
            except Exception as e:                       # 一条坏记录不许带走其余的
                print(f"⚠️ 跳过一条读不懂的任务记录（{tid}）：{e}")
        # ⭐ R3：重启前还 PENDING/RUNNING 的任务，恢复即标注"被重启打断"，
        # 并把标注写回一次盘——不然内存里标了、盘上还是 RUNNING，下次重启
        # 又冒出一批"进行中"的僵尸。列表可查、不自动续跑。
        #
        # ⭐ v0.35 A1：**waiting_user 不在这一族里，这是这一版的全部意义**。
        # pending/running 被打断时上下文（模型中间态、工具回路）确实没了，说"被打断"
        # 是实话；而 waiting_user 的定义就是"停在一步上等人，停点已经随 resume_cursor
        # 落盘"，重启不许把它说成失败——提案 §四 v0.35 判据 2 要的就是这一条。
        # 所以这里既不加它进 interrupted，也不给它另造一个"复活"标：can_resume
        # 由 from_dict 按不变式认回来，续跑的人从游标接。
        interrupted = 0
        for t in task_store.values():
            if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                t.interrupted_by_restart = True
                interrupted += 1
        if interrupted or backfilled:
            _write_unlocked()
        if backfilled:
            print(f"🧭 已为 {backfilled} 条无主历史任务认给 {LEGACY_OWNER}"
                  f"（legacy 标注），数据文件 {target}")
        return len(task_store)


def get_task(task_id: str) -> Optional[Task]:
    """根据task_id获取任务对象"""
    return task_store.get(task_id)


def tasks_of(user_id: str) -> List[Task]:
    """属于某个人的任务。按创建时间排，不按 dict 顺序——那会变，而列表顺序一变
    界面上就会看到"昨天的任务跑到中间去了"。"""
    return [t for t in task_store.values()
            if t.user_id == user_id]


def delete_task(task_id: str) -> bool:
    """删除任务。检查与删除同一把锁里做：分开的话，两个人同时删一个任务，
    后一个会在 `del` 上撞 KeyError——那是一个把 True/False 说成崩溃的形状。"""
    with _lock:
        if task_id in task_store:
            del task_store[task_id]
            return True
        return False


restore()
