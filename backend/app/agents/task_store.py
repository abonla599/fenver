"""任务状态存储：一个 JSON 文件 + 一把锁 + 原子替换。

文件头以前写着"当前使用内存字典存储，后续可升级为 Redis 或数据库"——那份内存字典
意味着任何一次重启都把任务清空，而 `/v1/tasks/{id}` 会理直气壮地回 404。规划书阶段一
的判据是"关掉服务器再开起来，昨天的任务还在，且属于正确的人"，所以这一步不引 Redis、
不加队列：与 sessions/providers 同一份写法就够了，将来真要换存储，换的是这个文件。

一处诚实的边界：**落盘的时机是"任务被放进存储"与"经由删除/取消接口改动"**。
编排循环里对 `task.status` / `results` 的字段级改动不经过这里，因此不会每一步都写盘
（那条路径 v0.25 R1 起对登录用户开放，但步级落盘与续跑是 T2.x 的事，这里刻意不提前做）。
要拿它跑长任务之前，
这一步必须补上——补法是让状态变更走一个显式的 `save(task)`，而不是在 getter 里藏写盘。
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
    PENDING = "pending"        # 待执行
    RUNNING = "running"        # 执行中
    COMPLETED = "completed"    # 已完成
    FAILED = "failed"          # 执行失败
    CANCELLED = "cancelled"    # ⭐ 新增：已取消


class Task:
    """任务数据结构。

    `user_id` 是必填的：本项目已经为"跨用户读到别人的东西"付过一次账（记忆泄露那回），
    所以一个不知道属于谁的任务在这里等于拒绝创建，而不是"先记着，读的时候再说"。

    `provider_id` 记录**发起人下单时**用的是哪条模型服务：账要能对上配置，
    排障时"这任务怎么烧的钱"不该靠考古。它只是记录，每次调用实际用谁仍由
    resolve 按发起人当下的池子现判（条目被删就按既有语义回落）。
    `legacy` 是启动回填的印记：归属维度上线之前躺在盘上的条目，恢复时被认给
    部署者主账号——列表里它们是历史，不是谁的现在。
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

    def mark_cancelled(self) -> None:                   # ⭐ 新增方法
        """标记任务为已取消"""
        self.cancelled = True
        self.status = TaskStatus.CANCELLED

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
