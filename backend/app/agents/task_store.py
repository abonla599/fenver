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
"""
import json
import os
import threading
import uuid
from datetime import datetime
from enum import Enum
from typing import List, Optional

from app.core.atomic_write import write_json_atomic
from app.core.paths import data_root


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
                               waiting_since=datetime.now().isoformat())

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
