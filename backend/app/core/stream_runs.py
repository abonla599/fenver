"""流式对话的进程内"运行簿"：run_id、序号、续播缓冲与真取消（v0.25 R3）。

为什么要有这个模块：SSE 流以前只有 start/content/done/error 四种光帧，没有
序号也没有续播凭据——断线之后客户端唯一能做的事就是把整轮重发一遍
（PWA 的 api.js 落回 /v1/chat，Android 的 ChatUi 同款），代价是同一轮付两次钱、
会话里落两条一样的助手消息。要接住"断线"，服务端必须给每一轮一个身份
（run_id）和每帧一个单调序号（seq），并把已经发过的事件留在缓冲里。

诚实的边界——这张表是**单进程、纯内存**的，改这个模块之前先把它说全：
- 活得过"断线"：进程还在，这一轮的全部事件（在上限之内）都还在缓冲里，生产线程
  也还在跑。这正是"可续播"的前提：**读者离开不再顺手掐死生产**——若一断就停，
  续播就必须重新叫上游，等于又收一次钱。因此"停止生成"从本版起是一个显式动作
  （POST /v1/chat/stream/{run_id}/cancel），不再是"关页面"的副作用。
- 但"关页面不再顺手掐死"不等于"关页面可以无限跑"（v0.25 R3b 补的钱闸）：
  第一个付费轮之后，每要起飞新一轮都必须有活读者挂在电上（attach_reader/
  detach_reader 由 sse_frames 进出，续播的重连也算读者回来）。1→0 起算宽限
  （config_store.stream_no_reader_grace_seconds，默认 120 秒），窗口里没人回来，
  就借**同一条**取消路径收场（翻标志、尽力关连接、半句在落盘闩下存会话、账
  照常结），不另起第二套"超时"语义。封顶是"已在飞行中的那一轮流完并结账、
  新的轮次为零"——断线本身不免费，close 一条正阻塞在 read 上的连接在这套
  SDK 下不可证明地即时。
- 活不过"重启"：这张表整体消失。重启后的重连拿到的是 410 cannot_resume，
  客户端被明确告知**不要重发**——结果本身在 sessions.json（消息照常落盘）、
  钱在 usage.json（账照常结清），刷新会话就能取回。
- 缓冲有上限：超过 MAX_EVENTS_PER_RUN / MAX_CHARS_PER_RUN 从最旧裁起；
  续播请求要的位置已经被裁掉（有空洞）= cannot_resume。慢的在线读者追不上
  缓冲时，也会在同一连接里收到一帧 cannot_resume 后被关闭——同样是"别重发，
  去会话里取"。
- 终态 run 的保留窗 FINISHED_RETAIN_SECONDS（默认 10 分钟）过后被清扫；
  清扫挂在新 run 创建时，没有定时器。

取消与任务面（R1）不许裂成两种"cancelled"：这里 run.status 用的字面值就是
TaskStatus.CANCELLED 的 "cancelled"；两面的语义同一句话——**已经花掉的钱照账、
已经产出的部分照落，下一个付费步骤不再发生**。差别只在停的位置：任务在当前子
任务完成后停，流式在下一个 chunk 边界停（并且尽力当场关掉上游连接，让那个
边界尽快到来）。归属判定与 main.py 的 _task_for_principal 同形：非属主与不存在
给出同一个码、同一句话，run_id 不是探测信道。
"""
import json
import threading
import time
import uuid
from typing import Dict, Generator, List, Optional

from app.core import stream_events

# 缓冲与保留的上限。取值逻辑：一次几千 token 的回答约几万次 content 帧吗？不——
# chunk 通常几个到几十个字符，2000 帧 + 200k 字符足够装下一条长回答；超过的人
# 是机器输出的极端样本，他们的正确出路是刷新会话，而不是让服务端无限攒内存。
MAX_EVENTS_PER_RUN = 2000
MAX_CHARS_PER_RUN = 200_000
FINISHED_RETAIN_SECONDS = 600.0

# run 的终态取值。"cancelled" 与 task_store.TaskStatus.CANCELLED.value 同词同义，
# 见模块文档；"completed"/"failed" 对齐 done/error 两种终帧。
TERMINAL_STATUSES = ("completed", "cancelled", "failed")

_lock = threading.RLock()
_runs: Dict[str, "StreamRun"] = {}


class StreamRun:
    """一轮流式对话：单调序号的事件缓冲 + 取消标志 + 上游连接的可关闭句柄。

    状态只由两处在动：生产线程（append/finish，main.py 的 _produce_stream），
    和取消端点（cancel，把标志翻上并尽力 close 上游）。读者只读缓冲。
    每一轮只有一个生产者、且只启动一次，因此"助手消息只落一次、账只记一次"
    首先来自这个结构；claim_message_save() 是第二道闩，防未来有人在读者退出时
    重新驱动生产——那道闩同时防的是"重连重放"被误接回生产路径。
    """

    def __init__(self, run_id: str, user_id: str, session_id: Optional[str],
                 message_id: str):
        self.run_id = run_id
        self.user_id = user_id                      # 服务端身份，发起那一刻定死
        self.session_id = session_id
        self.message_id = message_id                # 会话里那条助手消息的 id，先备好
        self.cond = threading.Condition()           # 护着缓冲；append/finish 会 notify
        self.events: List[dict] = []                # [{"seq": n, "payload": {...}}]
        self.last_seq = 0
        self._buffered_chars = 0
        self.status = "running"                     # running -> completed|cancelled|failed
        self.finished = False
        self.finished_at = 0.0
        self.cancel_event = threading.Event()       # 取消标志：生产侧逐块检查
        self._upstream = None                       # 正在读的 openai Stream（尽力关闭用）
        self._message_saved = False
        # —— R3b 读者在场面（护在同一个 cond 上，不引第二把锁）——
        self._readers = 0                           # 正握着 SSE 连接的读者数
        self.no_reader_since = None                 # 1→0 的时刻（monotonic）；有读者即 None
        self.stop_cause = None                      # 停的成因（"no_reader"）；None=没人标注过

    # ---------- 生产侧 ----------

    def append(self, data: dict) -> int:
        """把一帧业务事件放进缓冲：seq 与 run_id 在这里注入，别处不造序号。"""
        with self.cond:
            self.last_seq += 1
            payload = {**data, "seq": self.last_seq, "run_id": self.run_id}
            self.events.append({"seq": self.last_seq, "payload": payload})
            self._buffered_chars += len(str(data.get("text") or ""))
            # 从最旧裁起，但永远留着至少一帧：终帧是最后进来的，裁不到它
            while len(self.events) > 1 and (
                    len(self.events) > MAX_EVENTS_PER_RUN
                    or self._buffered_chars > MAX_CHARS_PER_RUN):
                dropped = self.events.pop(0)
                self._buffered_chars -= len(str(dropped["payload"].get("text") or ""))
            self.cond.notify_all()
            return self.last_seq

    def finish(self, status: str) -> None:
        """定终态并唤醒所有读者。由生产者调用，且必须是最后一个动作。"""
        with self.cond:
            self.status = status
            self.finished = True
            self.finished_at = time.time()
            self.cond.notify_all()

    def claim_message_save(self) -> bool:
        """助手消息落盘的闩：谁拿到 True 谁写，整个 run 只有一个人拿得到。"""
        with self.cond:
            if self._message_saved:
                return False
            self._message_saved = True
            return True

    # ---------- 读者侧（R3b） ----------

    def attach_reader(self) -> None:
        """一条 SSE 连接进场：计数 +1，并把 1→0 的宽限计时器**停表**。

        续播（Last-Event-ID）走的也是 sse_frames，因此"窗口内重开接着看"
        不需要任何额外接线——重连的 attach 就是取消计时器的那个动作。
        """
        with self.cond:
            self._readers += 1
            self.no_reader_since = None
            self.cond.notify_all()

    def detach_reader(self) -> None:
        """一条 SSE 连接结束（含客户端断开的 GeneratorExit）：计数 -1，落到 0 时起表。"""
        with self.cond:
            self._readers = max(0, self._readers - 1)
            if self._readers == 0:
                self.no_reader_since = time.monotonic()
            self.cond.notify_all()

    def has_reader(self) -> bool:
        with self.cond:
            return self._readers > 0

    def wait_for_reader(self, grace_seconds: float) -> bool:
        """阻塞等"有活读者"，窗口从 1→0 那一刻起算；不忙轮询，全靠 cond 通知。

        True  = 读者在场或窗口内回来了；
        False = 窗口耗尽仍未回来，或期间有人显式取消（等待者被 cancel 的
                notify 叫醒，由调用方看标志位区分成因）。
        从未有过读者的 run 走到这里才起表：没有 1→0 就没有天然窗口起点，
        以本次轮次边界起算是最不坏的解释（不阻塞正常装配，也不无限悬着）。
        """
        with self.cond:
            if self._readers > 0:
                return True
            if self.no_reader_since is None:
                self.no_reader_since = time.monotonic()
            deadline = self.no_reader_since + max(0.0, float(grace_seconds))
            while self._readers == 0 and not self.cancel_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                # 1 秒上限只是兜底（同 sse_frames 的规矩）：attach/cancel 都 notify
                self.cond.wait(min(remaining, 1.0))
            return self._readers > 0

    # ---------- 取消侧 ----------

    def attach_upstream(self, stream) -> None:
        """stream_chat 每发起一轮就把它手里的上游连接交过来。"""
        with self.cond:
            self._upstream = stream

    def abort_upstream(self) -> None:
        """尽力关闭当前上游连接（线程安全的"尽力"：openai 的 close 不许并发）。

        这是取消里唯一的"当场"动作。诚实边界：close 一条正被另一个线程阻塞读取
        的连接，在不同平台/不同层的组合下**不保证**立刻打断那次读——打断了是运气
        好，没打断则取消最迟在下一个 chunk 到达（或读超时）时生效。真正被数学上
        保证的是：标志位翻上之后，下一轮 create() 不会发生（streaming.py 的
        轮首检查），所以账单不会新增一次付费调用。
        """
        with self.cond:
            stream = self._upstream
        try:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        except Exception:
            # 已经关了/正在关/层里抛出什么都行：标志位已经翻上，兜底边界照旧成立
            pass

    def cancel(self) -> str:
        """翻标志 + 关连接。返回 "cancelled"（这一次按下去的）或 "warning"（已终态）。"""
        self.cancel_event.set()
        self.abort_upstream()
        with self.cond:
            # R3b：显式取消要能立刻叫醒正挂在 wait_for_reader 里的生产线程——
            # "停止"仍是即时动作，不吃宽限窗。
            self.cond.notify_all()
            return "warning" if self.finished else "cancelled"


# ---------- 登记表 ----------

def create_run(user_id: str, session_id: Optional[str] = None) -> StreamRun:
    """登记一轮新流。清扫挂在这里：没有定时器，表只在有人进场时瘦身。"""
    run = StreamRun(run_id=str(uuid.uuid4()), user_id=user_id,
                    session_id=session_id, message_id=str(uuid.uuid4()))
    now = time.time()
    with _lock:
        expired = [rid for rid, r in _runs.items()
                   if r.finished and now - r.finished_at > FINISHED_RETAIN_SECONDS]
        for rid in expired:
            _runs.pop(rid, None)
        _runs[run.run_id] = run
    return run


def get_run(run_id: str) -> Optional[StreamRun]:
    with _lock:
        return _runs.get(run_id)


def clear_all_for_tests() -> None:
    """进程内状态进用例前必须清——与 task_store/限流账本同一个规矩。"""
    with _lock:
        _runs.clear()


# ---------- SSE 帧与读者 ----------

def _frame(run_id: str, seq: int, payload: dict) -> str:
    # data 行保持 `data: {json}\n\n` 的旧形状（两个客户端都只认 data: 行里的
    # type 分发）；新增的 id: 行对旧客户端是"看不懂的行"，直接跳过——向后兼容
    # 靠的是这个，不是运气（判据见 tests/test_v025_stream_cancel_contract.py）。
    return f"id: {run_id}:{seq}\ndata: {json.dumps(payload)}\n\n"


def resumable(run: Optional[StreamRun], cursor: int) -> bool:
    """这个游标之后的每一帧都还补得出来吗？空洞、越界、没有这一轮，都是 False。"""
    if run is None or cursor < 0:
        return False
    with run.cond:
        if cursor > run.last_seq:
            return False                            # 客户端要的比服务器发过的还多
        if run.events and run.events[0]["seq"] > cursor + 1:
            return False                            # 下一个正要补的帧已被裁掉：有空洞
        return True


def sse_frames(run: StreamRun, cursor: int = 0) -> Generator[str, None, None]:
    """从 cursor（已收到的最后一个 seq）起补帧，run 未终则挂着等新帧。

    同步生成器：StreamingResponse 会把每次迭代丢进线程池——一个读者占一个池线程,
    与改造前"生成器自己拉着模型读"占用相同；结构上多出来的只有生产线程。
    读者因客户端断开被 close 时，cond.wait 随线程栈一起退出，run 不受影响。

    R3b：这条生成器就是"活读者"的定义——进场 attach、以任何方式离场 detach
    （正常耗尽、显式 close、断开引发的 GeneratorExit 都走同一个 finally）。
    首帧 yield 之前完成 attach，因此握连接的语义与判据里"实际持有连接的
    SSE 生成器"逐字对应；续播同样经这里，重连即停宽限计时器。
    """
    run.attach_reader()
    try:
        while True:
            with run.cond:
                gap = bool(run.events) and run.events[0]["seq"] > cursor + 1
                batch = [] if gap else [e for e in run.events if e["seq"] > cursor]
                finished = run.finished
                last = run.last_seq
                if not gap and not batch and not finished:
                    # 1 秒的超时只是兜底：append/finish 都 notify，正常等待不该走到它
                    run.cond.wait(1.0)
                    gap = bool(run.events) and run.events[0]["seq"] > cursor + 1
                    batch = [] if gap else [e for e in run.events if e["seq"] > cursor]
                    finished = run.finished
                    last = run.last_seq
            if gap:
                # 在线读者被缓冲的裁减追上了：明说续不上，别将错就错——这条帧没有
                # seq，因为它不属于缓冲里那个连续的事件流
                yield ("data: " + json.dumps({
                    "type": "cannot_resume", "code": "cannot_resume", "resumable": False,
                    "run_id": run.run_id,
                    "message": "服务端缓冲已装不下你断点之后的全部事件：不要重发原文，"
                               "结果会写进会话历史，刷新会话取回"}) + "\n\n")
                # R3b-3：cannot_resume 是旧客户端（api.js 只认 content/done/error）
                # 看不懂的类型——若以此收尾，旧客户端按"没有 done"判 retryable，
                # 会把整轮重发回 /v1/chat 再付一次钱。补一条同为连接级（不占缓冲
                # 序号）的 done 终帧：停的信息放进新字段，旧解析器只取它认的骨架。
                yield ("data: " + json.dumps({
                    "type": "done", "status": "cannot_resume",
                    "stopped_reason": "buffer_gap", "resumable": False,
                    "run_id": run.run_id, "message_id": run.message_id,
                    "message": "本连接因追不上服务端缓冲而结束：不要重发原文，"
                               "结果会写进会话历史，刷新会话取回"}) + "\n\n")
                return
            for e in batch:
                yield _frame(run.run_id, e["seq"], e["payload"])
                cursor = e["seq"]
            if finished and cursor >= last:
                return
    finally:
        run.detach_reader()


# ---------- 跨属主补帧的隐私边界（v0.29.1） ----------

PRIVATE_TRACE_FRAME_TYPES = frozenset({
    stream_events.FRAME_THINKING, stream_events.FRAME_TOOL_CALL,
    stream_events.FRAME_TOOL_RESULT, stream_events.FRAME_SEARCH,
})


def scrub_private_trace(frames: Generator[str, None, None]) -> Generator[str, None, None]:
    """把"只属于发起者"的过程留痕从补帧里摘掉，其余逐字节原样透出。

    为什么要有这一层：续播那条路允许 admin 跨属主接上别人的 run（main.py 的
    resume 分支，注释写的"与任务面同权同形"）。v0.25 时代那条缓冲里只有正文，
    跨属主接上去看到的是回答；v0.29 把思考文本、工具入参与执行结果、搜索命中
    也写进了**同一个**缓冲，于是同一条通道开始送模型对用户输入的复述和用户的
    私有参数。这不是既有权限：管理端从来不读任何人的会话内容（/admin 界面不调
    任何 /v1/sessions，逐条路由见 tests/test_cross_owner_replay_scrubs_trace_v0291.py），
    不该由"给同一轮多加了四种帧"顺手扩大。

    只在出口做减法：那三类帧整条不吐（连 `id:` 行一起，序号因此留空档——两个
    客户端都按 data 行的 type 分发、按 id 记游标，空档不影响下次续播），done 里
    的 trace 字段删掉，骨架字段一个不动，所以旧客户端照旧拿到完整正文与终帧。
    """
    try:
        for chunk in frames:
            head, sep, rest = chunk.partition("data: ")
            if not sep:
                yield chunk                             # 连 data 行都没有：不猜它的形状
                continue
            payload_text, end, tail = rest.partition("\n\n")
            try:
                payload = json.loads(payload_text)
            except ValueError:
                yield chunk                             # 看不懂的帧照原样送，别在这里制造第二次断裂
                continue
            ftype = payload.get("type")
            if ftype in PRIVATE_TRACE_FRAME_TYPES:
                continue
            if ftype == stream_events.FRAME_DONE and "trace" in payload:
                payload.pop("trace")
                yield head + sep + json.dumps(payload) + end + tail
                continue
            yield chunk
    finally:
        # 外层被 close（读者断开）时必须把内层一起关掉：sse_frames 的 detach_reader
        # 挂在它的 finally 上，R3b 的"没有活读者"计时就系在这一关，晚一步是白等宽限。
        frames.close()
