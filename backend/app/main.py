import sys
import os

# 中文 Windows 的控制台默认 GBK，打包成 EXE 后任何 emoji 日志都会抛
# UnicodeEncodeError 并在导入阶段终止进程（开发终端能显示 UTF-8，所以看不出来）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import uuid
import threading
import time
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
import uvicorn

# ---------- 路径设置 ----------
# 确保项目根目录 (backend) 在路径中，以便支持 from app.xxx import xxx
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) # 指向 backend/
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# ---------- 可选导入：后台任务模块（本人负责） ----------

# 1. preference_analyzer：定时任务里唯一真正干活的后台模块。
#    memory_weight_updater 从前也挂在这个 import 上，但它整个模块只有一个 print，
#    每 300 秒被调用一次、每轮都印一句"执行记忆权重更新"——而它什么都不改。
#    现在它只提供一句启动时打印的实话（见该文件的模块文档），不再进定时任务。
try:
    from app import preference_analyzer
    HAS_BG_TASKS = True
except ImportError as e:
    HAS_BG_TASKS = False
    preference_analyzer = None
    print(f"⚠️ 偏好分析模块未找到: {e}")

from app import memory_weight_updater

# 2. feedback_storage 和 analyze_and_update_preference (假设它们在 app 目录下)
# 注意：如果 preference_analyzer 已经在上面导入成功，这里可以直接从 app.preference_analyzer 导入函数
try:
    from app.feedback_storage import save_feedback, FEEDBACK_FILE
    # 如果 preference_analyzer 模块存在，从中导入具体函数
    # 偏好摘要按人分账之后，"重算一次"这个动作必须说清楚是替谁重算：
    # analyze_and_update_preference(user_id) 服务单个用户，
    # analyze_all_preferences() 服务后台定时器（它没有"当前调用者"这个身份）。
    if preference_analyzer:
        from app.preference_analyzer import (analyze_and_update_preference,
                                             analyze_all_preferences)
    else:
        analyze_and_update_preference = None
        analyze_all_preferences = None
except ImportError as e:
    save_feedback = None
    FEEDBACK_FILE = None
    analyze_and_update_preference = None
    analyze_all_preferences = None
    print(f"⚠️ 反馈存储/偏好分析模块未找到: {e}")

# ---------- 其他核心导入 ----------
try:
    from app.core.llm_client import get_llm_response
    from app.pipeline import ChatPipeline
    USE_PIPELINE = True
except ImportError as e:
    USE_PIPELINE = False
    print(f"❌ ChatPipeline 不可用，记忆注入与工具调用已失效（非流式对话将直接调用模型）: {e}")

try:
    from app.agents.orchestrator import Orchestrator
    # 不给 Orchestrator 传模型名：留空 = 每次调用现走 setDefault 的 provider。
    # 在这里刻一个服务商名，等于把用户在设置页里换默认模型的权力没收。
    orchestrator = Orchestrator()
except ImportError as e:
    orchestrator = None
    print(f"⚠️ 编排器不可用，/v1/agent/orchestrate 等端点将返回 503: {e}")

try:
    from app.agents.task_store import task_store, get_task, tasks_of, TaskStatus
except ImportError:
    task_store = {}
    get_task = None
    TaskStatus = None

    def tasks_of(user_id):                # 模块未就绪时按人过滤空表——绝不返回全量
        return [t for t in task_store.values()
                if getattr(t, "user_id", None) == user_id]

# ... (其余代码保持不变) ...

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行
    # 先把"这八份可变数据此刻到底写在哪儿"打出来：它原先只是文档里的一段散文，
    # 而 data_file() 会在 data/ 下没有同名文件时退回项目根那份历史文件——于是
    # "所有数据都在 data/ 下吗"取决于本机有没有一个老 preference.txt，光看文档猜不出来。
    from app.core.paths import log_data_locations
    log_data_locations()
    # 紧接着打一行"齐不齐"：出事时人在看日志，而不是去猜当时 /health 回过什么。
    from app.core.selfcheck import log_startup_summary
    log_startup_summary()
    # v0.24 T1.4：再补一行配置口径——注册开没开、更新去哪个仓找。这两件事直接
    # 决定来人第一次怎么进门，值得在日志里留一句，而不是让人去翻 data/config.json。
    from app.core.config_store import log_config_summary
    log_config_summary()
    # uvicorn 的 access/error logger 也要过一遍脱敏：访问日志会把查询串原样写出来
    # （?model=deepseek-chat 就是这么漏进 named.log 的），光给文件流加壳拦不住它。
    # 放在启动摘要之后，是因为这条之前已经有人往日志里写过东西。
    from app.core import logsanitizer
    logsanitizer.install_std_filters()
    # 搜索源探测在后台线程里跑，但线程得早点起：第一轮要十几秒（DNS 被黑洞时
    # getaddrinfo 不吃 socket 超时），而这段时间工具清单按"能用"处理。
    from app.tools.availability import start_probe
    start_probe()
    # v0.28.2：发布页快照的暖缓存线程。冷启动先拉一次、之后每半个缓存期照看一眼，
    # 让「检查更新」永远走在已经握在手里的快照上——判据与限流账见 releases 模块。
    from app.core import releases as _releases
    _releases.start_cache_warmer()
    start_background_scheduler()
    yield
    # 关闭时执行（如果需要清理资源）
    print("服务正在关闭...")

# 记忆模块路由（包含完整的 /v1/memory/* 端点）
from app.memory.memory_router import router as memory_router

# 身份端点：邀请码注册 + 管理面。哪个端点免凭据由 authz.PUBLIC_PATHS 说了算，
# 这里只负责挂载，不在此处再判一遍凭据。
from app.core.auth_router import router as auth_router
from app.core.auth_router import throttle_paid_upstream

# PWA 前端（手机浏览器访问 /app 即可使用，与 API 同源）
from app.web.web_router import mount_admin, mount_pwa

# ---------- 创建 FastAPI 应用 ----------
# 身份与文档开关的规则都在 app/core/authz.py，这里只负责装上。
# 模式仍由 authz 现读 env：本模块不自带 AUTH_MODE 默认值，免得两处默认不一致。
from app.core.authz import (_auth_mode, CurrentPrincipal, docs_kwargs_for_mode,
                            install_auth, Principal, RequireAdmin)

# 非 disabled 模式连文档路由都不生成（路由表本身就是侦察材料）
app = FastAPI(
    title="AI 智能助手",
    description="多模型、工具调用、记忆管理的智能助手系统",
    version="1.0.0",
    lifespan=lifespan,  # 注册 lifespan
    **docs_kwargs_for_mode(_auth_mode())
)

# ---------- 错误形状 ----------
# 形状错（整个字段缺失、字段类型不对）落在 FastAPI 的 RequestValidationError 上，默认
# detail 是一个结构化 list，而且 pydantic 会把提交进来的值原样放进 input——注册与找回
# 那两格里过的就是密码和找回答案。两个客户端因此同时坏：api.js 是 `new Error(detail)`，
# 数组过去显示成 [object Object]；管理页 admin.js 同一套写法。
#
# 这句话只在服务端说一次。找回那道 pydantic 条数闸门撤掉之后，"字段缺失/不是数组"这一类
# 仍然走结构化 detail，如果让每个客户端各自兜一层（Array.isArray(detail) ? ... : ...），
# 形状就有了 N 个事实来源，而 curl 与桌面壳这两个没兜的东西照样看不懂。
# 判据见 tests/test_auth_endpoints.py 的
# test_a_malformed_body_says_one_plain_chinese_sentence。
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

MALFORMED_BODY_DETAIL = "提交的内容不完整或格式不对：请检查每一栏都填了"


@app.exception_handler(RequestValidationError)
async def on_malformed_body(_: Request, exc: RequestValidationError):
    """把结构化校验错误压成一句人话，状态码仍是 422。"""
    # 原始错误只进服务端日志，且只取字段路径：够定位是哪儿没填，
    # 又不会把密码或答案写进控制台与 EXE 的日志文件里。
    where = "、".join(".".join(str(part) for part in (err.get("loc") or ()))
                      for err in exc.errors()[:8])
    print(f"⚠️ 请求体形状不对（{where or '未知字段'}）")
    return JSONResponse(status_code=422, content={"detail": MALFORMED_BODY_DETAIL})

# 注册记忆路由（优先使用 Router 中的端点）
app.include_router(memory_router)

# 注册身份端点（/v1/auth/*、/v1/admin/*）
app.include_router(auth_router)

# 注册 PWA 前端。挂载在 /app 下，接口仍走 /v1/*，两者互不干扰。
mount_pwa(app)

# 管理员页挂在 /admin：一个不含数据的空壳，数据一律经 /v1/admin/* 取。
mount_admin(app)

# ---------- 访问鉴权 ----------
# 身份规则见 app/core/authz.py（import 在创建应用那一节）。这里只负责装上。
install_auth(app)

# 耗时日志装在鉴权**之后**，于是它包在鉴权外面：被 401 挡掉的那几次同样留一行。
# 人打不开页面的那些分钟，最需要知道的是"请求到底有没有到"——把观察器装在门里面，
# 被门挡掉的那些就正好是日志里的一片空白。
from app.core.request_log import RequestTiming

app.add_middleware(RequestTiming)

# 后加的中间件包在先加的**外面**，所以这一条是全场第一道门：超大 JSON 体在解析、
# 鉴权、记账之前就被挡掉。只认 application/json —— 上传走 multipart，它有自己的
# MAX_UPLOAD_BYTES(10MB) 判据，在这儿一并卡住会把附件上传直接打死。
MAX_JSON_BODY_BYTES = 2 * 1024 * 1024


@app.middleware("http")
async def reject_oversized_json_body(request: Request, call_next):
    """声明体积超过 2MB 的 JSON 请求体，落地之前先回 413。

    为什么光有模型层的限条数/限长不够：`/v1/chat` 的上限是 50 条 × 32000 字，
    乘起来仍是 1.6M 字符；而全仓还有别的字符串字段（feedback 的 comment 等）。
    一处体积闸门比在每个模型上补一遍 max_length 更不容易漏。

    诚实的边界：这里信的是 `Content-Length` 头。分块传输（`Transfer-Encoding:
    chunked`）不发这个头，那条路要靠 uvicorn 自己的缓冲上限兜——本闸门挡的是
    "客户端老老实实声明了要塞 500MB"，不是伪装协议。
    """
    if (request.headers.get("content-type") or "").lower().startswith("application/json"):
        declared = (request.headers.get("content-length") or "").strip()
        if declared.isdigit() and int(declared) > MAX_JSON_BODY_BYTES:
            print(f"⚠️ 拒绝超大 JSON 请求体: {request.url.path} 声明 {declared} 字节")
            return JSONResponse(status_code=413, content={"detail": "请求体过大"})
    return await call_next(request)

# ---------- 数据模型 ----------

# 一次聊天请求的三条体积闸门。限流挡的是**次数**，这三条挡的是**单次的大小**——
# 乘起来才是真正花掉的 token 与吃掉的内存；只装前者，20 次/分每次塞几 MB 照样能
# 把服务打穿。判据集中在 backend/tests/test_request_size_caps.py。
MAX_CHAT_MESSAGES = 50
MAX_MESSAGE_CHARS = 32_000
MAX_CHAT_ATTACHMENTS = 5
MAX_FEEDBACK_COMMENT_CHARS = 500


def _message_text_chars(message) -> int:
    """一条消息里"文本"的字符数：多模态数组只数 text 部件。

    不数 image_url 里的 base64 是因为那部分本就有 2MB 总体积闸门兜着，而把它按
    字符算进 32000 的额度，等于让发一张图的用户"一句话都没说"就被判超长。
    """
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(part.get("text", "")) for part in content
                   if isinstance(part, dict) and part.get("type") == "text")
    return 0


class ChatRequest(BaseModel):
    # 兼容字段：作为 provider 的别名解析。默认从写死的服务商名改为 None（F-1f）：
    # 名字刻在这儿，用户在设置页换了默认 provider 也不会有任何影响——resolve
    # 对 None 与对该名字的兜底路径本来就是同一个 default()，去掉刻名只是把
    # "碰巧被上游兜住"改成"这里本来就没写"。
    model: Optional[str] = None
    provider: Optional[str] = None        # 模型服务 id（首选）
    attachments: List[str] = Field(default_factory=list, max_length=MAX_CHAT_ATTACHMENTS)
    messages: list[dict]
    session_id: Optional[str] = None

    @field_validator("messages")
    @classmethod
    def _within_size_caps(cls, messages: list) -> list:
        if len(messages) > MAX_CHAT_MESSAGES:
            raise ValueError(f"messages 最多 {MAX_CHAT_MESSAGES} 条")
        if any(_message_text_chars(m) > MAX_MESSAGE_CHARS for m in messages):
            raise ValueError(f"单条消息文本最多 {MAX_MESSAGE_CHARS} 字")
        return messages

class FeedbackRequest(BaseModel):
    message_id: str
    rating: int
    # comment 存进 feedback.json，而那个文件是"读全表—追加—写全表"：不限长等于
    # 让单条点踩备注把整本账撑大，之后每次写入都要重写它。500 字够写一句人话。
    comment: Optional[str] = Field(None, max_length=MAX_FEEDBACK_COMMENT_CHARS)

class AgentRequest(BaseModel):
    task: str = Field(..., max_length=MAX_MESSAGE_CHARS)
    max_turns: Optional[int] = 10
    max_duration: Optional[int] = 120
    # provider id（或旧式模型名）；留空走默认配置。端点原先向上写死
    # "deepseek-chat"，等于在代码里刻了一个服务商名。
    model: Optional[str] = None

class OrchestrateRequest(BaseModel):
    goal: str
    task_id: Optional[str] = None

# ---------- 会话存储 ----------
from app.session.session_store import SessionStore

# ⚠️ 导入期副作用：这一行会把 $SESSION_DB_PATH 指向的文件读进来并做 owner 回填，
# 未设置该变量时就是仓库真实的 data/sessions.json —— 也就是说"只是 import 一下
# app.main"就会改写用户真实数据，并在旁边落下 sessions.json.bak-<时间戳>。
# 脚本与测试必须先定 SESSION_DB_PATH / UPLOAD_DIR / USERS_DB_PATH，
# 再导入本模块（backend/tests/conftest.py 就是为此存在）。
# fail-fast 是刻意的；改成惰性构造留给 Task 8。
sessions_store = SessionStore()

# ---------- 后台定时任务 ----------
def run_scheduler_cycle():
    """跑一轮周期性后台任务。

    单独成一个函数，是为了这一轮能被测试调用：`backend/tests/
    test_memory_weight_scheduler.py` 用一个 tripwire 钉住"定时器不改权重"。
    原先整段逻辑埋在一个 while True 里，谁都调不到它，于是那句谎话印了
    多少个周期都没人能为它写一条断言。

    这里刻意不碰权重：写权重的只有用户反馈与两个显式端点，见
    `app/memory_weight_updater.py` 的模块文档。
    """
    print("--- 开始执行周期性后台任务 ---")
    if HAS_BG_TASKS and preference_analyzer:
        # 定时器没有"当前调用者"，所以它不能替某一个人决定归属：
        # 反馈里出现过谁就重算谁那一份（偏好摘要按人分账，见
        # preference_analyzer）。原先这里调用的是不带身份的
        # analyze_and_update_preference()，它把所有人合并成一份
        # 全站摘要再注入每个人的提示词。
        preference_analyzer.analyze_all_preferences()


def run_scheduler():
    while True:
        try:
            run_scheduler_cycle()
            print("--- 周期性后台任务执行完毕 ---")
        except Exception as e:
            print(f"后台任务执行出错: {e}")
        time.sleep(300)

def start_background_scheduler():
    # 一句话说明权重到底由谁改写。只在启动时印这一次：它描述的是"没有自动衰减"
    # 这件事，而这件事不会因为又过了 300 秒而变化——按周期印只会让人以为有东西在跑。
    memory_weight_updater.log_weight_policy()

    # 启动偏好分析线程
    bg_thread = threading.Thread(target=run_scheduler, daemon=True)
    bg_thread.start()
    print("🚀 后台偏好分析定时任务已启动（只重算偏好摘要，不改写记忆权重）")

    # 这里原来还起一条"实时反馈闭环监听器"线程，并打印"🔁 实时反馈闭环监听器已启动！"。
    # 它没做事：调用时没传 callback，auto_weight_adjuster 退化成默认 lambda，只往
    # stdout 印一句"未提供回调函数"。真正改权重的是 /v1/feedback 的同步路径，需求已经
    # 被它覆盖。留着的结果是一行永远在说谎的启动日志——本项目最贵的那类东西。
    # 线程删除后 auto_weight_adjuster 模块本体就成了零引用死代码，2026-10-01 一并删除。



# ---------- API 端点 ----------
# 同步 def：自检要读盘（密钥库、账本、chroma 的集合元数据）。挂在 async 上就是
# 把阻塞 I/O 放回事件循环——那正是 524 的根因，而且会连带冻住流式回答。
@app.get("/health")
def health_check():
    """进程活着只是及格线；这里报的是"那几样会静默坏掉的东西齐不齐"。

    状态码不随检查结果变化（永远 200）：坏的是配置，重启修不好它，而"非 200 就拉起"
    的守护会因此每分钟杀掉一次正在进行的对话。判据给日志和运维，不给进程管理器。

    `build` 是这套部署的构建戳（构建时由 git tag 生成，见 app/core/buildinfo.py）。
    放在这里而不是新开一个端点：这是免鉴权的公开信息（版本号本来就在公开仓库的 tag
    上），而「设置 → 关于」那一行正是没有登录态的时候也要能显示。
    """
    from app.core.buildinfo import build_version
    from app.core.selfcheck import run
    return {"build": build_version(), **run()}


# 同步 def：这条会替调用方去 GitHub 拉一次（最多 5 秒）。写成 async 就是把阻塞网络
# 放回事件循环——524 那节课的第二遍，代价是所有人的流式回答一起卡住。
@app.get("/v1/release/latest")
def release_latest(have: str = None):
    """手机上那张「发现版本更新」要问的全部：最新是哪版、比手上这版新吗、去哪儿下。

    免鉴权（在 `authz.PUBLIC_PATHS` 里点了名）：它发生在人还没登录的时候。免鉴权不等于
    没有代价——它会替调用方出一次网，所以那条请求带 10 分钟缓存，一小时内最多 6 次，
    与来多少请求无关（判据与理由都在 `app/core/releases.py`）。

    拉不到时回 `ok:false` 而不是 502：界面据此**不弹**，也不会把"我读不到"报成"你已是最新"。
    """
    from app.core import apk_cache, releases
    out = releases.probe(have)
    if out.get("ok"):
        # 有人打开 App 在问"有没有新版"——正是服务端该把包备到盘上的时刻。
        # 不等网络、按版本去重，判据见 core/apk_cache.prefetch。
        apk_cache.prefetch()
    return out


# 同步 def：与上面那条共用同一份 10 分钟快照，同样可能朝 GitHub 走一趟。
@app.get("/v1/update/info")
def update_info(request: Request):
    """壳「检查更新」的数据源：原样的 GitHub 发布 JSON + 顶层 `apk_sha256`。

    为什么壳不自己问 GitHub 的发布接口：手机到 GitHub 的链路要过运营商、代理与各家
    ROM 的下载器，正是 2026-09-23 那次"迅雷劫持 → 残包 → 安装失败"的案发通道；
    而手机到这台服务器是天天在用的链路。一台机器出网、全员共享缓存，代价与
    `/v1/release/latest` 完全同构（判据在 tests/test_release_probe.py）。

    与那条卡片端点的分工：卡片要的是"要不要提一句"（拉不到就**不弹**，ok:false）；
    这条要的是"人主动点了检查"，拉不到必须说出来——所以拉不到时是 502 带理由，
    而不是一份能让壳误判"已是最新"的 200。三态纪律（读不出来 ≠ 已是最新）两头同款；
    AC-4 的后半句「不伪装最新」在这一头还意味着：GitHub 恢复前，超过缓存期的旧快照
    对这条端点等同于读不出来（判据在 `releases.latest_release_manifest`，
    钉在 test_update_channel 的 stale 用例组）。

    搬仓以后这条还要多做一件小事：把**旧名**那枚资产的下载地址换成自家代取端点
    （`releases.route_legacy_asset_to_self_host`）。0.23.x 及更早的壳把 GitHub 路径
    白名单写死成旧仓，装进手机就改不动了；它们只认旧名，所以只要看见自家出口地址
    照样能收这一版。来源取自请求自己的 Host 头——壳是照 `Prefs.baseUrl` 打到这台
    机器的，同源判据比的正是那个主机，拼不出合格来源（本地 http、带端口）就不换。
    """
    from fastapi.responses import JSONResponse
    from app.core import apk_cache, releases
    manifest, reason = releases.latest_release_manifest(_self_origin(request))
    if manifest is None:
        return JSONResponse(status_code=502, content={"detail": f"问不到发布信息：{reason}"})
    # 人主动点了「检查更新」：马上可能就要点「立即更新」。让这一问顺手把包备到
    # 盘上（不等网络、同版本只补一趟），下载那一步就从"替 GitHub 扛可用性"变成
    # 读本地磁盘。判据与纪律在 core/apk_cache.py。
    apk_cache.prefetch()
    return manifest


def _self_origin(request: Request) -> str:
    """这台服务器在这一次请求里的"https://主机"来源；不合格就是空串（＝什么都不换）。

    只认 Host 头不认 `request.url`：生产是 TLS 在前面的反代终结的，容器里看到的是
    http，而壳那条同源判据比的是它自己配置的 `https://` 主机。带端口或 userinfo 的
    主机在这里会被 `self_hosted_apk_url` 判死，所以本机开发（localhost:8000）走的是
    "不换"这一档——老壳从来不信 http，把一个它不信的地址换上去只是把一条能用的地址
    换成一条不能用的。
    """
    host = (request.headers.get("host") or "").strip()
    proto = (request.headers.get("x-forwarded-proto") or "https").split(",")[0].strip().lower()
    return f"{proto}://{host}" if proto == "https" and host else ""

# ---------- 聊天接口 ----------
from app.core import stream_events
from app.core import credits
from app.core.providers import (store as provider_store, ProviderError, PRESETS,
                                looks_placeholder, build_client, scrub_secrets)
from app.core.uploads import (store as upload_store, build_user_content,
                              UploadError, MAX_UPLOAD_BYTES)


def _fail_reason(e: BaseException) -> str:
    """把异常链压成一句能定位的话：类型全留，重复的句子并掉。

    SDK 的默认文案不含任何线索（openai 那句 "Connection error." 底下才躺着
    "CERTIFICATE_VERIFY_FAILED：本机卡巴斯基拆了 TLS"），根因只在 __cause__ /
    __context__ 上。真机上量到这条链四层里有三层是同一句话，所以按文案分组、组内
    并掉重名类型：一层原文配一串类型名，才是既说得清又不刷屏的形状。

    最后一句要过一次 scrub_secrets：这个函数是**日志与响应体共用的那一个出口**
    （main.py 的四处调用 + 管理员的探活都从这儿过），脱敏长在这里才只有一份口径；
    写在每个调用点上迟早会漏一处，而漏的那一处正好是上游把 key 打印回来的那次。
    """
    groups = []          # 每项是 ([类型名...], 文案)
    seen = set()
    cur = e
    while cur is not None and len(seen) < 8 and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__
        text = str(cur)[:160]
        if groups and groups[-1][1] == text:
            if groups[-1][0][-1] != name:
                groups[-1][0].append(name)
        else:
            groups.append(([name], text))
        cur = cur.__cause__ or cur.__context__
    return scrub_secrets(" ← ".join(" → ".join(names) + (f": {text}" if text else "")
                                    for names, text in groups))


def _throttle_chat(http: Request, principal: Principal) -> None:
    """第五本限流账：60 秒 20 次，键 `IP + 登录身份`。

    顺序是**先判断、再记账、才叫模型**——反过来写的话，被挡下的那一次也会把
    真金白银花出去，而那正是这本账要挡的事。判据在 test_chat_throttle.py 里。

    实现搬到 `app.core.auth_router.throttle_paid_upstream`：账本必须只有一处，
    否则记忆接口那条同样出网的通道可以绕开它（v0.24.1 体检查实的问题）。
    """
    throttle_paid_upstream(http, principal)


def _prepare_chat(request: ChatRequest, principal: Principal):
    """解析模型服务、拼装附件。

    返回 (provider, 发给模型的消息列表, 写入会话历史的用户文本)。
    历史里只记原始文本加附件名，避免把整份文件塞进会话记录。

    principal 一路传进来而不是在这里再取一次：附件归属必须由"谁在请求"决定，
    请求里那个 attachments 列表只是别人塞进来的 id 集合。
    """
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")

    provider = provider_store.resolve(request.provider, legacy_model=request.model,
                                      user_id=principal.user_id)

    messages = list(request.messages)
    last = messages[-1] or {}
    raw = last.get("content")
    text = raw if isinstance(raw, str) else ChatPipeline.text_of(raw)

    content = build_user_content(text, request.attachments, provider["supports_vision"],
                                 principal.user_id)
    messages[-1] = {**last, "content": content}

    history_text = text
    if request.attachments:
        names = "、".join(
            (upload_store.get(a, owner=principal.user_id) or {}).get("name", a)
            for a in request.attachments)
        history_text = (text + "\n" if text else "") + f"[附件] {names}"

    return provider, messages, history_text


def _require_session_owner(session_id, principal: Principal) -> None:
    """有 session_id 就先证明它属于调用者，否则 404，且必须在调模型之前。

    为什么不能只靠 add_message 返回 False：那样这条路会对"别人的会话 id"回一个
    200，而模型已经调完、token 已经花掉，转录则被静默丢掉——调用方看到的界面
    上一切正常，历史里一个字都没落。写没写进去是这条链路的契约，就不能靠一个
    被丢弃的返回值来表达。

    404 不会把它变成探测器："不是你的"和"根本不存在"经 store 返回同一个 None，
    因此发出的是同一个 404、同一句话，跟 /v1/sessions/{id} 那些路由一模一样。
    """
    if session_id and sessions_store.get(session_id, owner=principal.user_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")


def _shadow_gate(principal: Principal) -> None:
    """影子护栏的请求入口前置判定（v0.25 T5.20）：在第一个付费轮出网之前先看线。

    与轮次边界上的 before_round 判据是同一条规则的两个位置（入口拦截 + 轮内拦截），
    不是两座互不知情的闸——轮首那一个闸在 streaming.py 检查点、政策在这里，
    本函数只负责"连第一条消息都不该出网"的情况。护栏默认关（0），开着且当日
    影子合计到线才拦。

    402 Payment Required 取其本义"额度闸"：这不是刷屏（那是 429 的地盘），
    更不是错误码dump——detail 就是用户看到的整句人话，停在「等待你确认是否
    继续」上。被拦下的这一次对 usage.json 零增量：先判断、再出网。
    """
    if credits.shadow_limit_exceeded(principal.user_id):
        raise HTTPException(status_code=402, detail=credits.SHADOW_STOP_MESSAGE)


@app.post("/v1/chat")
def chat(request: ChatRequest, http: Request, principal: Principal = CurrentPrincipal):
    _throttle_chat(http, principal)
    _require_session_owner(request.session_id, principal)
    _shadow_gate(principal)   # 入口先过线：到线就不出网，这一次账本零增量
    try:
        provider, messages, user_text = _prepare_chat(request, principal)
    except (ProviderError, UploadError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        used_memory_ids = []
        trace = []
        if USE_PIPELINE:
            result = ChatPipeline(user_id=principal.user_id).process(
                provider["model"], messages, provider_id=provider["id"])
            reply = result.get("reply", "")
            used_memory_ids = result.get("used_memory_ids") or []
            trace = result.get("trace") or []
        else:
            reply = get_llm_response(
                model=provider["model"], messages=messages,
                temperature=0.7, provider_id=provider["id"],
                user_id=principal.user_id)   # 账要落在叫它的人头上
    except HTTPException:
        raise
    except Exception as e:
        # 失败就明确失败：把错误当回复返回会让它被写进会话历史、伪装成成功
        reason = _fail_reason(e)
        print(f"模型调用失败: {reason}", file=sys.stderr, flush=True)
        raise HTTPException(status_code=502, detail=f"模型调用失败：{reason}")

    message_id = str(uuid.uuid4())
    # 落盘前先压一次：客户端把整份过程原样回写时不该绕过预算。
    # compact_trace 产出的就是纯 JSON 安全字典，不必再 json 兜一圈。
    compacted = stream_events.compact_trace(trace) if trace else []
    if request.session_id:
        sessions_store.add_message(request.session_id, principal.user_id, "user", user_text)
        sessions_store.add_message(request.session_id, principal.user_id, "assistant", reply,
                                   message_id, used_memory_ids, trace=compacted)
    out = {"reply": reply, "message_id": message_id,
           "provider": provider["id"], "model": provider["model"]}
    # 只加新键，不动既有键：老客户端读 reply 照旧，新客户端据 trace 还原过程面板。
    out["trace"] = compacted
    return out

# ---------- 流式聊天接口 ----------
from fastapi.responses import StreamingResponse
import json as json_module

# run 的登记表/序号缓冲/续播帧生成都在 app.core.stream_runs（那里的模块文档
# 把"什么活得过断线、什么活不过重启"写全了）。这里负责产品接线：谁的一轮、
# 消息落哪、账记在谁头上。
from app.core import stream_runs
from app.core import config_store

# 续播失败的那一句全场只有一份：不存在 / 不是你的 / 缓冲有空洞 / 已过保留期
# 逐字节相同——与 _task_for_principal 同一个纪律，run_id 不许成为探测信道。
CANNOT_RESUME = {"detail": "续播不了：这一轮不属于你、已经不在这台进程里、或缓冲已装不下你要的位置。"
                           "请不要重发原文——结果已经（或将会）写进会话历史，刷新会话即可取回",
                 "code": "cannot_resume", "resumable": False}


def _parse_last_event_id(raw: str):
    """SSE 标准头 Last-Event-ID 的形状是 `<run_id>:<seq>`；解不出就 (None, 0)。

    客户端不用 EventSource：这一族是 POST fetch（请求体带着对话内容），所以
    续播凭据必须由客户端手动放进头里——协议上这是 SSE 的合法用法（EventSource
    只是自动帮你发这个头的那层糖）。
    """
    head, sep, tail = (raw or "").rpartition(":")
    if not sep:
        return None, 0
    try:
        seq = int(tail)
    except ValueError:
        return None, 0
    if seq < 0:
        return None, 0
    return head or None, seq


def launch_stream_run(run: "stream_runs.StreamRun", provider, messages, user_text,
                      principal: Principal, pipe, used_memory_ids) -> threading.Thread:
    """启动生产线程。与端点分开写，是为了契约测试能在不开 HTTP 流的处境下
    装配一轮（test_v025_stream_cancel_contract 需要中途取消的确定性时机）。"""
    t = threading.Thread(target=_produce_stream,
                         args=(run, provider, messages, user_text, principal,
                               pipe, used_memory_ids),
                         daemon=True, name=f"stream-run-{run.run_id[:8]}")
    t.start()
    return t


def _produce_stream(run, provider, messages, user_text, principal, pipe, used_memory_ids):
    """把一轮流式对话的每一步都变成带序号的缓冲帧：生产者只此一个线程。

    与改造前的 generate() 的三点不同，每一点都是 R3 判据的正面：
    1. 读者断开不再终结这里——生成跑完它的；"停止"走 cancel 端点（显式动作）。
       R3b 给这条自由加上了钱的边界：第一个付费轮之后，轮次边界要有活读者才
       起飞下一轮（判据见下面 before_round 的注释，检查点在 streaming.py）。
    2. 助手消息只经 claim_message_save() 落一次：续播、多读者、将来的任何
       重复驱动都写不出第二条（idempotent 落盘）。
    3. usage 只在 stream_chat 的 finally 里记一次：这个函数整个 run 生命周期
       只调用它一次，续播走的分支根本不经过这里（见端点的 resume 分支）。
    取消的半句话也落盘：用户屏幕上已经看到的东西不该在历史里凭空消失——
    这与"用户消息先落盘"是同一条原则。

    终帧纪律（R3b-3）：被停掉的流在 cancelled 之后必须再补一条 type:"done"
    的缓冲帧收尾。旧 PWA（api.js:160-185）只认 content/done/error，其余静默
    丢；流若以一个旧客户端看不懂的帧收尾，它按"没 done"判 retryable，会把
    整轮重发进 /v1/chat 再付一次钱——那正是本版要堵的洞。停的信息放进 done
    的新字段（status/stopped_reason/resumable），旧解析器不受影响。
    """
    from app.core.streaming import (stream_chat, StreamCancelled, begin_trace,
                                    drain_frames, collect_steps, end_trace)
    from app.core import stream_events as se

    def before_round():
        """R3b-1 的钱闸·判据侧：检查点在 streaming.py 的轮次边界（付费边界）。

        两个判据、一个钩子（v0.25 R4b 并行安全边界 7）：
        1) 影子积分护栏（shadow_limit_exceeded）——到线就不出网；
        2) 有没有活读者（wait_for_reader）——没读者不留窗口空转。
        它们回答的是同一个问题「这一轮该不该花钱出网」，必须挂在同一个
        before_round 上、走同一条取消出口；不许在轮首另起第二座互不知情的闸。

        宽限期单一真相：config_store.stream_no_reader_grace_seconds()（env
        STREAM_NO_READER_GRACE_SECONDS > data/config.json > 默认 120 秒）。
        窗口内读者回来（含 Last-Event-ID 续播）一切照常；没回来就借既有的
        取消路径收场——翻标志、尽力关连接、半句在落盘闩下进会话、账照常
        结清——绝不 fork 第二套"超时"语义。

        诚实的边界：这里能保证的是"已在飞行中的那一轮流完并结账，新的轮次
        为零"。它**不**让断线免费——close() 一条正阻塞在 read 上的连接在这套
        SDK 下不可证明地即时，所以在飞行中的那一轮照常付账。
        """
        if credits.shadow_limit_exceeded(principal.user_id):
            if not run.cancel_event.is_set():
                run.stop_cause = "shadow_daily_limit"
                run.cancel()
            return False
        grace = config_store.stream_no_reader_grace_seconds()
        if run.wait_for_reader(grace):
            return True
        if not run.cancel_event.is_set():      # 到期；若期间已被显式取消，成因不归这里
            run.stop_cause = "no_reader"
            run.cancel()
        return False

    status = "failed"
    full_text = ""
    trace: list = []

    def _emit_pending():
        """把引擎塞进本线程 sink 的非内容帧补发进 run 缓冲，保持产出顺序。

        thinking/tool_call/tool_result/search 这些被 stream_chat 的文本过滤器吃掉了，
        只能靠 drain_frames 交回来——它们与 content 帧的相对次序即引擎的产出次序。
        """
        for frame in drain_frames():
            run.append(frame)

    begin_trace()
    try:
        run.append(se.start_frame(run.message_id, provider["model"]))

        # 用户消息先落盘：模型调用失败时也不该让用户刚发的话凭空消失
        if run.session_id:
            sessions_store.add_message(run.session_id, principal.user_id,
                                       "user", user_text)

        # tools 走的是同一个 pipeline 实例：非流式那条一直把工具传给模型，
        # 流式这条以前一个都没传，于是界面上工具等于不存在（模型如实说它
        # 不会用计算器）。清单装配见 ChatPipeline.__init__。
        for chunk in stream_chat(provider["model"], messages,
                                 provider_id=provider["id"],
                                 tools=pipe.tools_schema if pipe else None,
                                 user_id=principal.user_id,        # 账本要落在人头上
                                 cancel_event=run.cancel_event,    # 停止的主判据
                                 on_upstream_start=run.attach_upstream,  # 供当场关闭
                                 before_round=before_round):       # R3b 轮次边界的钱闸
            # 先补发这段正文之前引擎产出的过程帧（思考/工具/搜索），再发正文本身，
            # 于是"它在想 → 它去调工具 → 它说结论"在上屏顺序上就是发生顺序。
            _emit_pending()
            full_text += chunk
            run.append(se.content_frame(chunk))
        # 末轮的 tool_call/tool_result/search 在最后一块正文之后才产出，收尾必须再抽一次。
        _emit_pending()

        # 全程过程步收拢成可落盘的 trace（封顶步数/字数在 compact_trace 里）
        trace = se.compact_trace(collect_steps())

        # 保存助手回复到会话（如果提供了 session_id）——闩内只有一次
        if run.session_id and run.claim_message_save():
            sessions_store.add_message(
                run.session_id, principal.user_id, "assistant",
                full_text, run.message_id, used_memory_ids, trace=trace)

        # 按信号写长期记忆，判据与非流式路径同一处（pipeline.save_interaction）
        if pipe is not None:
            try:
                pipe.save_interaction(user_text)
            except Exception as e:
                print(f"流式记忆保存失败（不影响已返回的回复）: {_fail_reason(e)}",
                      file=sys.stderr, flush=True)

        run.append({**se.done_frame(full_text, run.message_id, provider["model"],
                                    trace or None),
                    "status": "completed", "resumable": False})
        status = "completed"
    except StreamCancelled:
        # 用户按了停止（或 R3b 的宽限到期借同一条路收场）：已经流出去的半句
        # 照样落盘（同一个 message_id，反馈仍可关联），然后发 cancelled 帧、
        # 再以 done 收尾。账已由 stream_chat 的 finally 结清——那是取消前真
        # 消费的部分，此后这里不再叫上游（轮首检查与读者闸门两处都拦着）。
        # 取消点之前已经产出的过程帧与留痕照发照存：屏幕上看到过的不该在历史里消失。
        _emit_pending()
        trace = se.compact_trace(collect_steps())
        if run.session_id and full_text and run.claim_message_save():
            sessions_store.add_message(run.session_id, principal.user_id,
                                       "assistant", full_text,
                                       run.message_id, used_memory_ids, trace=trace)
        # 到线的成因决定那句话：影子护栏停在「等待你确认是否继续」上（卡片 §3-2），
        # 断线仍停在原来那句上——同一个出口、两条实话，不许合并成一句含糊的"已取消"。
        stop_message = (credits.SHADOW_STOP_MESSAGE
                        if run.stop_cause == "shadow_daily_limit"
                        else "已取消：生成停在块边界，未再发起新的付费调用")
        run.append({"type": "cancelled", "full_text": full_text,
                    "message_id": run.message_id,
                    "message": stop_message})
        # R3b-3 的收尾 done：旧客户端从 cancelled 身上得不到"结束"，必须给一条
        # 它今天就认的 done；停的信息只加新字段，不改旧骨架。
        run.append({**se.done_frame(full_text, run.message_id, provider["model"],
                                    trace or None),
                    "status": "cancelled",
                    "stopped_reason": run.stop_cause or "user_cancel",
                    "resumable": False})
        status = "cancelled"
    except Exception as e:
        reason = _fail_reason(e)
        print(f"流式模型调用失败: {reason}", file=sys.stderr, flush=True)
        run.append(se.error_frame(f"模型调用失败：{reason}"))
        status = "failed"
    finally:
        # 无论走到哪条出口，run 必须有终态：没有它，读者会挂在 cond.wait 上，
        # 那正是旧结构里"连接还开着但再也没动静"的形状。
        end_trace()
        run.finish(status)


@app.post("/v1/chat/stream")
def stream_chat_endpoint(request: ChatRequest, http: Request,
                         principal: Principal = CurrentPrincipal):
    """流式聊天端点，返回 Server-Sent Events（v0.25 R3：带 run_id 与序号，可取消、可续播）。

    端点与生成侧都必须是同步的：模型 token 是从阻塞 socket 上读出来的，
    留在事件循环里会让一个慢请求冻住整台服务（线上 524）。

    生产者/读者分离（R3 的地基）：上游块由专用生产线程写入 run 缓冲
    （stream_runs），HTTP 响应只从缓冲读。断线因此不再掐死生成——这是"续播
    不必重付整轮"的前提；"停止"变成显式动作：POST /v1/chat/stream/{run_id}/cancel。

    续播：带 `Last-Event-ID: <run_id>:<seq>` 头的请求不叫模型、不落消息、不记账，
    只补发 seq 之后的缓冲帧；续不上（不存在/不是你的/空洞/过期）给逐字节相同的
    410 cannot_resume——这句话的含义就是"别自动重发原文"。续播不计入付费限流
    账：它不出网，只读这一进程的内存（防刷屏靠保留窗与缓冲上限，不靠钱闸门）。
    """
    raw = http.headers.get("last-event-id")
    if raw:
        run_id, cursor = _parse_last_event_id(raw)
        run = stream_runs.get_run(run_id) if run_id else None
        mine = run is not None and (principal.role == "admin"
                                    or run.user_id == principal.user_id)
        # 三类"续播不了"合成一句话（CANNOT_RESUME 常量）：拿 410 探 run_id
        # 存在与否，得到的信息量为零。admin 可跨属主续播，与任务面同权同形。
        if not mine or not stream_runs.resumable(run, cursor):
            return JSONResponse(status_code=410, content=CANNOT_RESUME)
        return StreamingResponse(stream_runs.sse_frames(run, cursor),
                                 media_type="text/event-stream")

    # 节流同样必须在这里判，理由和下面那条归属一样：流一开，状态码就锁死在 200，
    # 那时再挡只能断流，而 429 与 Retry-After 根本送不出去。
    _throttle_chat(http, principal)

    # 归属必须在这里判，不能在生成侧判：流一开始 HTTP 状态就锁死在 200，
    # 那时再发现 session_id 不是你的，只能静默不落盘（原先正是这样）。
    _require_session_owner(request.session_id, principal)

    # 影子护栏的入口前置判定：到线就 402 停在人话上，run 根本不创建、上游一次
    # 都不碰——轮内到线由 before_round 的同一判据停（两处一个规则，不是两座闸）。
    _shadow_gate(principal)

    # 解析放在返回流之前：否则配置错误只能混在流里，HTTP 状态仍是 200
    try:
        provider, messages, user_text = _prepare_chat(request, principal)
    except (ProviderError, UploadError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 流式此前直接调 stream_chat、绕过 pipeline，因此既没注入记忆与偏好，
    # 也不会把本轮对话写回记忆。这里复用同一个 pipeline 实例补齐两者。
    used_memory_ids = []
    pipe = ChatPipeline(user_id=principal.user_id) if USE_PIPELINE else None
    if pipe is not None and messages:
        try:
            query_text = ChatPipeline.text_of(messages[-1].get("content"))
            messages, used_memory_ids = pipe.inject_context(messages, query_text)
        except Exception as e:
            print(f"流式上下文注入失败（不影响本次对话）: {_fail_reason(e)}",
                  file=sys.stderr, flush=True)

    run = stream_runs.create_run(user_id=principal.user_id,
                                 session_id=request.session_id)
    launch_stream_run(run, provider, messages, user_text, principal, pipe,
                      used_memory_ids)
    return StreamingResponse(stream_runs.sse_frames(run, 0),
                             media_type="text/event-stream")


@app.post("/v1/chat/stream/{run_id}/cancel")
def cancel_stream_run(run_id: str, principal: Principal = CurrentPrincipal):
    """"停止"终于是一个动作：翻标志、尽力当场关闭上游连接、下一轮付费调用不发生。

    归属与 _task_for_principal 同形：非属主与不存在的 id 是同一个 404、同一句话
    （"运行不存在"），cancel 不是 run_id 探测器；admin 可跨属主取消——与任务面
    同一权限同一形状。"cancelled" 一词与 TaskStatus.CANCELLED 同一个语义：已花
    的照账、已产出的照落、下一个付费步骤不再发生。

    真停的位置在 app/core/streaming.py：轮首与逐块的 cancel_event 检查，加上
    这里经由 StreamRun.abort_upstream 对上游连接的一次尽力 close。诚实边界写在
    那两处——close 不保证打断正阻塞的读，所以最迟在下一个块或读超时（默认
    120s）生效；被保证的是取消之后不会再有新的 create()。
    """
    run = stream_runs.get_run(run_id)
    if run is None or (principal.role != "admin" and run.user_id != principal.user_id):
        raise HTTPException(status_code=404, detail="运行不存在")
    outcome = run.cancel()
    if outcome == "warning":
        return {"status": "warning", "run_id": run_id,
                "message": f"运行已处于终态: {run.status}，无需取消"}
    return {"status": "cancelled", "run_id": run_id,
            "message": "已请求取消：停在下一个块边界，不再发起新的付费调用；"
                       "流以 done 终帧收尾（status=cancelled，cancelled 事件在其之前）"}

# ---------- 会话管理 ----------
# 归属只由 principal 推导：路由不接受任何来自 body/query/path 的 user_id 或
# owner，否则"我是谁"就成了客户端说了算。
# 非本人一律 404 而不是 403：403 等于承认这个 id 存在，session_id 是 uuid4，
# 但只要有一次 403 漏出来，这个接口就成了"哪些会话真实存在"的探测器。
@app.post("/v1/sessions")
def create_session(model: Optional[str] = None,
                         principal: Principal = CurrentPrincipal):
    # 默认不再刻服务商名（F-1f）。会话上的 model 只是展示元数据——真正用哪个
    # provider 是每次聊天时 resolve 决定的，这里传空串而不是 None：老记录与
    # 白名单投影里该字段一直是字符串，别让"没指定"把形状改成 null。
    return sessions_store.create(model or "", owner=principal.user_id)

@app.get("/v1/sessions")
def list_sessions(principal: Principal = CurrentPrincipal):
    return {"sessions": sessions_store.list_summaries(principal.user_id)}

@app.get("/v1/sessions/{session_id}")
def get_session(session_id: str, principal: Principal = CurrentPrincipal):
    session = sessions_store.get(session_id, owner=principal.user_id)
    if session is None:
        # 404 而非 403：403 等于承认这个 id 存在，可以被拿来枚举
        raise HTTPException(status_code=404, detail="会话不存在")
    # 投影而不是裸记录：owner 是存储内部字段，列表侧早有白名单，详情没有就等于
    # 三条读路径各说各话，下一个加字段的人只能猜该抄哪一条。
    return sessions_store.public(session)

@app.delete("/v1/sessions/{session_id}")
def delete_session(session_id: str, principal: Principal = CurrentPrincipal):
    if sessions_store.delete(session_id, owner=principal.user_id):
        return {"status": "deleted", "session_id": session_id}
    raise HTTPException(status_code=404, detail="会话不存在")

class SessionMessagesRequest(BaseModel):
    messages: List[Dict[str, Any]] = []

@app.put("/v1/sessions/{session_id}/messages")
def replace_session_messages(session_id: str, req: SessionMessagesRequest,
                                   principal: Principal = CurrentPrincipal):
    """整体替换会话消息，使前端编辑/删除/重新生成后的视图与后端一致。"""
    if not sessions_store.replace(session_id, principal.user_id, req.messages):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"status": "updated", "count": len(req.messages)}

# ---------- 日程（"今天该干什么"那份清单） ----------
# 归属人一律从凭据里取（principal.user_id），请求体与查询串里都没有 user_id 这个口子：
# 与记忆/会话/附件同一口径，见 app/core/schedule.py 的模块注释。
from app.core import audit, schedule


class ScheduleItem(BaseModel):
    text: str
    at: Optional[str] = None            # 24 小时的 HH:MM，或干脆不填
    done: bool = False


class ScheduleRequest(BaseModel):
    day: Optional[str] = None           # 空 = 服务端本地日期的今天
    items: List[ScheduleItem] = []


@app.get("/v1/schedule")
def get_schedule(day: Optional[str] = None, principal: Principal = CurrentPrincipal):
    """这一天的清单，外加这个人有过安排的那些天（给日期选择器用）。

    坏日期回 400，不回"一份空清单"：后者与"那天确实没安排"长得一模一样，界面会把
    一句问错了的话渲染成一句真话。
    """
    try:
        target = schedule.normalize_day(day)
        return {"day": target,
                "items": schedule.plan(principal.user_id, target),
                "days": schedule.days_for(principal.user_id)}
    except schedule.ScheduleError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/v1/schedule")
def put_schedule(req: ScheduleRequest, principal: Principal = CurrentPrincipal):
    """整天一次替换（幂等）。为什么不是逐条增删，写在 schedule.set_plan 的注释里。"""
    try:
        target = schedule.normalize_day(req.day)
        rows = schedule.set_plan(principal.user_id,
                                 [item.model_dump() for item in req.items], day=target)
    except schedule.ScheduleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"day": target, "items": rows, "count": len(rows)}


# ---------- 导出下载（一次性票据） ----------
# 前端 blob: + <a download> 在 Android WebView 壳里存不下文件（壳收不到 blob 下载
# 回调，也不会给下载请求带 Authorization），所以导出必须换成一条免登录、真实、
# 一次性的 HTTPS 链接。鉴权靠"链接本身就是票据"，不靠请求头：
#   1. 签发（下面第一条）走 CurrentPrincipal，且复用 _require_session_owner——
#      "不是你的会话"和"这会话不存在"是同一句话、同一个 404，不是枚举信道；
#   2. 兑换（第二条）免凭据可达，但免登录的门只放行"整条恰好是票据形状"的路径
#      （authz.PUBLIC_ROUTE_TEMPLATES），且票据 5 分钟过期、兑换一次即作废。
from app.session.export_store import (EXPORT_PATH_PREFIX, TICKET_TTL_SECONDS,
                                      content_disposition, export_ticket_store,
                                      session_markdown)
from fastapi.responses import Response

# 兑换失败只有一句话：票据不存在、已过期、已被用过、签发后会话又被删了，四种
# 走到这里都回同一份 404。区分它们等于把这个端点养成一台"这条链接是否真存在过"
# 的探测器——正是票据链接最不该成为的东西。
EXPORT_TICKET_INVALID_DETAIL = "导出链接无效或已过期"


@app.post("/v1/sessions/{session_id}/export-ticket")
def create_export_ticket(session_id: str, principal: Principal = CurrentPrincipal):
    """为属于自己的会话签一张一次性导出票据，返回可匿名兑换的相对路径。

    同步 def：签发要读写进程内会话存储（带锁 + 落盘），放在事件循环里就是
    那次线上 524 的形状。归属校验必须在签票之前，且复用 _require_session_owner
    而不是另写一套判断。
    """
    _require_session_owner(session_id, principal)
    # 惰性清理（剔除过期条目）写在 store.issue 内部：每次签发顺带扫一遍，
    # 既不必起后台线程，也不会让过期票据在表里越积越多。
    ticket_id = export_ticket_store.issue(session_id, principal.user_id)
    return {"path": f"{EXPORT_PATH_PREFIX}{ticket_id}",
            "expires_in": TICKET_TTL_SECONDS}


@app.get("/v1/exports/{ticket_id}")
def redeem_export_ticket(ticket_id: str):
    """用票据换回该会话的 Markdown。免登录，所以这一条路由不挂任何身份依赖。

    兑换即作废：store.consume 无论命中与否都把票据弹出，第二次永远拿不回内容。
    取会话用签发时记下的 owner（不是当前请求者——这里根本没有请求者身份），
    只导出服务端真实存着的消息。文件名走 RFC 5987，标题绝不原样拼进响应头。
    """
    redeemed = export_ticket_store.consume(ticket_id)
    if redeemed is None:
        raise HTTPException(status_code=404, detail=EXPORT_TICKET_INVALID_DETAIL)
    session_id, owner = redeemed
    session = sessions_store.get(session_id, owner=owner)
    if session is None:
        # 签发与会话删除之间有窗口：票据还在，会话已经没了。回同一句话，
        # 别把"票据有效但会话已删"这件事透露出去。
        raise HTTPException(status_code=404, detail=EXPORT_TICKET_INVALID_DETAIL)
    return Response(content=session_markdown(session),
                    media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": content_disposition(
                        session.get("title"))})


# ---------- 模型列表（由 Provider 配置派生） ----------
from fastapi import UploadFile, File
from fastapi.responses import FileResponse

@app.get("/v1/models")
def list_models(principal: Principal = CurrentPrincipal):
    """模型清单：前端那个下拉就靠它渲染。

    清单是**按当前用户裁剪的**：全局共享条目 + 这个人自己的私有 provider。
    别人的私有条目从这里根本不存在（与 resolve 的越权即回落同一套口径），
    所以这个端点同时也是一个"哪些模型存在"的诚实答案，不掺枚举信号。

    挂身份依赖不是为了挡住匿名读取
    ——那道门由 install_auth 的中间件在路由之前守着，但只在 **enforced 模式下、
    且只在 authz._PROTECTED_PREFIXES 那几个前缀（含 /v1/）之下**成立：disabled 模式
    人人都是本机管理员，websocket 握手更是压根不经过这个 HTTP 中间件（实测见
    tests/test_route_auth_contract.py）。要的理由就两条：路由契约
    （tests/test_route_auth_contract.py）不接受没有身份的 /v1 端点，且这是中间件
    之外多出来的一把锁。完整理由见下面 providers 段那段注释。
    key masking 原样保留——catalog() 只报 usable/reason，密钥永不出这道门。
    """
    return {
        "models": provider_store.catalog(principal.user_id),
        "default": (provider_store.default_for(principal.user_id) or {}).get("id"),
        "presets": PRESETS,
    }

# ---------- 模型服务（Provider）配置 ----------
# 这一面补上了身份依赖，且角色已收归管理员。写侧升级是 Task 7 明写的活：
# 改默认 provider、改 base_url 或塞进一把密钥，就把**所有人**的对话改道到攻击者
# 指定的上游——那是跨用户外泄通道，不是"他能看到别人的会话"那种局部越权。
# 邀请码人人可换（Task 3），所以"注册用户"在这道门前不含任何信任量。
# 身份依赖本身仍然保留，理由与 /v1/models 那条一样：路由契约要求每条 /v1 路由声明
# 身份；install_auth 的中间件之外多一把锁（受保护前缀哪天收窄、挂载顺序哪天被动过，
# 锁不至于只有一把）；将来要按人记账时身份已经在手里。
# 前端（/app 的设置页）配合收起这些入口，但那只是不让普通用户点到一个必然 403 的
# 按钮：手搓请求仍旧由这里的依赖拒绝，界面从来不是边界。
class ProviderRequest(BaseModel):
    id: Optional[str] = None
    label: str
    base_url: str
    api_key: str = ""
    model: str
    supports_vision: bool = False
    # 上下文上限（K token）。留 None → 存储层归一为缺省 64；界面据此给
    # 「上下文长度」滑杆封顶。钳制/非法值判据只写在 providers._validate 一处。
    max_context_k: Optional[int] = None
    is_default: bool = False
    # v0.25 R4b T5.3：单价配置面。形状/三态校验全在 credits.parse_pricing，
    # 这里只开一格传送。None（没填）= 不改（与 paid_by 同一条"旧表单不许洗值"
    # 纪律，见 update 路由的 exclude_none）；显式 {} = 主动取消定价（回到未定价）。
    pricing: Optional[dict] = None

def _owner_gate(provider_id: str, user_id: str, require_own: bool) -> dict:
    """取一条 provider 并验证归属。require_own=True 时只有主人过闸。

    别人的私有 provider 与不存在的 id 走同一个 404、同一句话——和 sessions、
    uploads 那两处的防枚举纪律一模一样：管理面与个人面都不许长成
    "这个 id 存在吗"的探测器。
    """
    existing = provider_store.get(provider_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="模型服务不存在")
    owner = existing.get("owner") or ""
    if require_own:
        if owner != user_id:
            raise HTTPException(status_code=404, detail="模型服务不存在")
    elif owner:
        # 管理员面碰私有条目：不存在（对管理员也不暴露用户私配的存在性）
        raise HTTPException(status_code=404, detail="模型服务不存在")
    return existing

@app.get("/v1/providers")
def list_providers(_: Principal = RequireAdmin):
    # 绝不返回明文密钥，只给掩码与"是否已配置"。清单只含共享条目：
    # 用户私有 provider 连"存在"这件事都不进管理员面（owner 维度见 providers.py）
    return {"providers": provider_store.public_list(), "presets": PRESETS}

@app.post("/v1/providers")
def add_provider(req: ProviderRequest, actor: Principal = RequireAdmin):
    try:
        saved = provider_store.upsert(req.model_dump())
    except ProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # after 走 _public：密钥在这一层就已经是掩码，审计要的是"这一格被建过"。
    audit.log(actor, "provider.create", target=saved["id"],
              after=provider_store._public(saved))
    return {"status": "saved", "provider": provider_store._public(saved)}

@app.put("/v1/providers/{provider_id}")
def update_provider(provider_id: str, req: ProviderRequest,
                          actor: Principal = RequireAdmin):
    existing = _owner_gate(provider_id, "", require_own=False)
    # exclude_none：请求里没带的字段 = "这次没动它"，不是"把它清空"。
    # pricing 与 paid_by 同一条纪律——旧表单不认识单价，一次普通改名
    # 绝不能把「有价」洗成「未定价」（那是把真花费报成算不出）。
    record = req.model_dump(exclude_none=True)
    record["id"] = provider_id
    try:
        saved = provider_store.upsert(record)
    except ProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _audit_pricing_change(actor, provider_id, existing, saved)
    audit.log(actor, "provider.update", target=provider_id,
              after=provider_store._public(saved))
    return {"status": "saved", "provider": provider_store._public(saved)}


def _audit_pricing_change(actor, provider_id: str, existing: dict, saved: dict) -> None:
    """单价一旦真的变了，必须在 audit.jsonl 里能回答：谁、把哪格、从多少改到多少。
    没变不落条——审计流不是活动日志，『成本怎么突然变了』要的是可核对的变更史，
    每存一次配置都冒一行会把真变更淹掉。before/after 只带 pricing 这一维
    （_public 那层没有密钥明文，这里连对象都不带，只带钱）。"""
    before = (existing or {}).get("pricing")
    after = (saved or {}).get("pricing")
    if before == after:
        return
    audit.log(actor, "provider.pricing.update", target=provider_id,
              before={"pricing": before}, after={"pricing": after},
              detail=credits.describe_pricing_change(before, after))

@app.delete("/v1/providers/{provider_id}")
def remove_provider(provider_id: str, actor: Principal = RequireAdmin):
    _owner_gate(provider_id, "", require_own=False)
    if provider_store.delete(provider_id):
        audit.log(actor, "provider.delete", target=provider_id)
        return {"status": "deleted", "id": provider_id}
    raise HTTPException(status_code=404, detail="模型服务不存在")

@app.post("/v1/providers/{provider_id}/default")
def set_default_provider(provider_id: str, actor: Principal = RequireAdmin):
    _owner_gate(provider_id, "", require_own=False)
    if provider_store.set_default(provider_id):
        audit.log(actor, "provider.set-default", target=provider_id)
        return {"status": "ok", "default": provider_id}
    raise HTTPException(status_code=404, detail="模型服务不存在")

@app.post("/v1/providers/{provider_id}/test")
def test_provider(provider_id: str, _: Principal = RequireAdmin):
    """对已保存的配置真实发一次请求，用于验证密钥与地址是否可用。

    必须是同步 def：ping 是一次 timeout=20 的阻塞模型调用，留在 async 里就是
    "点一下测试，整台服务二十秒不响应"（见 test_event_loop_not_blocked）。
    """
    _owner_gate(provider_id, "", require_own=False)
    try:
        return provider_store.ping(provider_id)
    except ProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))

def _test_draft(req: ProviderRequest) -> dict:
    """保存前用草稿配置试连，避免存了一个根本用不了的模型。"""
    try:
        candidate = provider_store._validate(req.model_dump())
    except ProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if looks_placeholder(candidate["api_key"]):
        return {"ok": False, "detail": "请先填写有效的 API Key"}
    try:
        client = build_client(candidate)
        client.chat.completions.create(model=candidate["model"],
                                       messages=[{"role": "user", "content": "ping"}],
                                       max_tokens=4)
        return {"ok": True, "detail": f"{candidate['model']} 响应正常"}
    except Exception as e:
        # 上游/中转站可能把 Authorization 原样打印回来——出口过一次 scrub。
        return {"ok": False, "detail": scrub_secrets(_fail_reason(e))}

@app.post("/v1/providers/test")
def test_provider_draft(req: ProviderRequest, _: Principal = RequireAdmin):
    return _test_draft(req)

# ---------- 个人模型服务（用户自带 API） ----------
# 与管理员面的分界线：这里每条路由都按 principal.user_id 圈所有权。
# 用户能增删改的只有自己名下的条目；自己的密钥只服务自己的请求（paid_by=user
# 在 _validate 之上由这里钉死）。"普通用户可改写全站上游"依然是禁区——
# 私有条目永不进站级默认（providers.default() 已滤），也不对其他人可见。

class ProviderDefaultRequest(BaseModel):
    provider_id: str

@app.get("/v1/me/providers")
def my_providers(principal: Principal = CurrentPrincipal):
    """这个人视角的模型服务面：共享清单（只读）+ 我的清单（可编辑）+ 我的默认。"""
    return {
        "shared": provider_store.public_list(None),
        "mine": provider_store.mine_public(principal.user_id),
        "default": provider_store.get_pref(principal.user_id),
        "presets": PRESETS,
    }

@app.post("/v1/me/providers")
def add_my_provider(req: ProviderRequest, principal: Principal = CurrentPrincipal):
    record = req.model_dump()
    record.update(owner=principal.user_id, paid_by="user", is_default=False)
    try:
        saved = provider_store.upsert(record)
    except ProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "saved", "provider": provider_store._public(saved)}

@app.put("/v1/me/providers/{provider_id}")
def update_my_provider(provider_id: str, req: ProviderRequest,
                       principal: Principal = CurrentPrincipal):
    existing = _owner_gate(provider_id, principal.user_id, require_own=True)
    # exclude_none 的口径与管理员面一致：没带 pricing = 保留原值。
    record = req.model_dump(exclude_none=True)
    record["id"] = provider_id
    record.update(owner=principal.user_id, paid_by="user", is_default=False)
    try:
        saved = provider_store.upsert(record)
    except ProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 私有单价同样进审计：属主改的是"自己烧多少积分"的尺子，影子、倍率、
    # 护栏都跟着它重算——跨用户看不了内容，但变更史必须在（管理员排账时
    # 这是唯一能说"哪天这把尺子换过"的地方）。
    _audit_pricing_change(principal, provider_id, existing, saved)
    return {"status": "saved", "provider": provider_store._public(saved)}

@app.delete("/v1/me/providers/{provider_id}")
def remove_my_provider(provider_id: str, principal: Principal = CurrentPrincipal):
    _owner_gate(provider_id, principal.user_id, require_own=True)
    provider_store.delete(provider_id)
    return {"status": "deleted", "id": provider_id}

@app.post("/v1/me/providers/default")
def set_my_default_provider(req: ProviderDefaultRequest,
                            principal: Principal = CurrentPrincipal):
    """把「我默认用哪个模型」存到服务端。共享或自己的私有条目都可以指。"""
    provider = provider_store.get(req.provider_id)
    if provider is None or not provider_store.visible_to(req.provider_id, principal.user_id):
        raise HTTPException(status_code=404, detail="模型服务不存在")
    try:
        provider_store.set_pref(principal.user_id, req.provider_id)
    except ProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "default": req.provider_id}

@app.post("/v1/me/providers/{provider_id}/test")
def test_my_provider(provider_id: str, principal: Principal = CurrentPrincipal):
    """只许试自己的条目：拿别人的（含共享的）已存密钥去发探测请求不是这个门的功能。"""
    _owner_gate(provider_id, principal.user_id, require_own=True)
    try:
        return provider_store.ping(provider_id)
    except ProviderError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/v1/me/providers/test")
def test_my_provider_draft(req: ProviderRequest, principal: Principal = CurrentPrincipal):
    """保存前草稿试连：密钥是这个人刚填的，出口照过 scrub。"""
    return _test_draft(req)

# ---------- 附件上传 ----------
@app.post("/v1/uploads")
def upload_attachment(file: UploadFile = File(...),
                            principal: Principal = CurrentPrincipal):
    # 先读满硬上限+1 字节再判，而不是裸 read() 全量进内存：save() 的分档体积校验
    # 在拿到完整 blob 之后才跑，挡不住"先把你内存打爆"。read(n) 对 SpooledTemporaryFile
    # 只多要一字节用于判超限，正常文件行为与原来逐字节一致。
    blob = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(blob) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过单次上传上限 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
    try:
        record = upload_store.save(file.filename or "unnamed", blob,
                                   file.content_type or "", owner=principal.user_id)
    except UploadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return record

@app.get("/v1/uploads/{upload_id}/file")
def download_attachment(upload_id: str, principal: Principal = CurrentPrincipal):
    # 附件是别人传的账单和论文。非属主给 404，不给 403：后者会把"id 存在"这件事
    # 白送出去，而 id 只有 16 位十六进制。
    record = upload_store.get(upload_id, owner=principal.user_id)
    if record is None:
        raise HTTPException(status_code=404, detail="附件不存在或已清理")
    return FileResponse(record["path"], media_type=record["mime"], filename=record["name"])

@app.delete("/v1/uploads/{upload_id}")
def delete_attachment(upload_id: str, principal: Principal = CurrentPrincipal):
    if upload_store.delete(upload_id, owner=principal.user_id):
        return {"status": "deleted", "id": upload_id}
    raise HTTPException(status_code=404, detail="附件不存在")

# ---------- 反馈 ----------
FEEDBACK_WEIGHT_STEP = 0.1


@app.post("/v1/feedback")
def submit_feedback(feedback: FeedbackRequest,
                          principal: Principal = CurrentPrincipal):
    """记录反馈，并立刻把它作用回系统。

    此前反馈只落盘到 feedback.json 和一份无人读取的 preference.txt，对模型行为
    零影响；这里改为真正闭环：调整本次回答所用记忆的权重 + 即时刷新偏好摘要。

    归属必须先于任何写入。原先的顺序是反的：先无条件往 feedback.json 加一行、
    再重算偏好，然后才按 message_id 反查记忆——而 preference.txt 会被注入
    **所有人**的提示词，于是任何持凭据者都能靠别人的 message_id 表态，改写的
    却是全站的行为。反查用带归属的 find_message：不属于你就当作不存在，
    两种情况同一个 404、同一句话，这个端点不是探测他人 message_id 的信道。

    偏好摘要现在按人分账：重算的是**调用者自己**那一份，别人的反馈进不了他的
    摘要（见 preference_analyzer.preference_path）。
    """
    from app.memory.memory_router import memory_manager

    if save_feedback is None:
        raise HTTPException(status_code=503, detail="反馈存储不可用")

    located = sessions_store.find_message(feedback.message_id, principal.user_id)
    if located is None:
        raise HTTPException(status_code=404, detail="消息不存在")

    try:
        save_feedback(feedback.message_id, feedback.rating, feedback.comment or "",
                      principal.user_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"反馈保存失败：{e}")

    adjusted = []
    # 消息是你的，不代表消息上挂的 memory_ids 是你的：整份回写会话的端点接受
    # 客户端给的 memory_ids（前端编辑历史要用），于是别人家的记忆 id 能被种进
    # 你自己的会话，再点一次反馈就去调它的权重。筛选写在 adjust_weights 内部
    # （owner 必填），这里就不再自己预筛一遍——能被调用方忘记的守卫迟早会被忘记。
    claimed = located.get("message", {}).get("memory_ids") or []
    if claimed and memory_manager is not None:
        delta = FEEDBACK_WEIGHT_STEP if feedback.rating > 0 else -FEEDBACK_WEIGHT_STEP
        try:
            adjusted = memory_manager.adjust_weights(
                claimed, principal.user_id, delta).get("updated", [])
        except Exception as e:
            print(f"记忆权重调整失败: {e}")

    # 立即重算他自己那份偏好，使下一轮对话就能生效，而不是等后台定时器
    try:
        if HAS_BG_TASKS and preference_analyzer:
            preference_analyzer.analyze_and_update_preference(principal.user_id)
    except Exception as e:
        print(f"偏好刷新失败（不影响反馈记录）: {e}")

    return {
        "status": "success",
        "message": "反馈已记录并生效",
        "memory_weight_adjusted": len(adjusted),
        "used_memories": bool(adjusted),
    }

# ---------- 意见反馈（用户写给管理员的话，与上面的 👍/👎 反馈分家） ----------
# 存储与编号合同在 app/user_feedback_storage.py；这里只做身份、校验与审计。
import mimetypes
import re as _re
from app import user_feedback_storage

_EMAIL_SHAPE_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserFeedbackRequest(BaseModel):
    text: str
    email: Optional[str] = None
    images: Optional[List[str]] = Field(None, max_length=user_feedback_storage.IMAGE_MAX)


@app.post("/v1/user-feedback")
def submit_user_feedback(req: UserFeedbackRequest,
                         principal: Principal = CurrentPrincipal):
    """提交一条意见反馈，返回它的独立编号（FB-0000NN）。

    图片走「先传后附」：客户端先用 /v1/uploads 传图，这里只引用 upload id。
    每个 id 都必须过 upload_store.get 的属主闸——引用别人的附件与引用不存在的
    附件是同一句话（get 本就合并这两种为 None），这个端点不是探测别人 upload_id
    的信道。校验通过后图片被**复制**进反馈目录，之后用户删自己的附件也删不掉
    管理员案头的那份凭据。
    """
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="反馈内容不能为空")
    if len(text) > user_feedback_storage.TEXT_MAX:
        raise HTTPException(status_code=400,
                            detail=f"反馈内容不能超过 {user_feedback_storage.TEXT_MAX} 字")
    email = (req.email or "").strip()
    if email:
        if len(email) > 120 or not _EMAIL_SHAPE_RE.match(email):
            raise HTTPException(status_code=400, detail="邮箱格式不对")
    sources = []
    for upload_id in req.images or []:
        record = upload_store.get(upload_id, owner=principal.user_id)
        if record is None:
            raise HTTPException(status_code=400, detail=f"截图不存在或已清理：{upload_id}")
        if record["kind"] != "image":
            raise HTTPException(status_code=400, detail="意见反馈只能附图片")
        ext = os.path.splitext(record["path"])[1].lower() or ".bin"
        sources.append((record["path"], ext))
    try:
        saved = user_feedback_storage.submit(principal.user_id, text, email, sources)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"意见反馈保存失败：{e}")
    return {"id": saved["id"], "created_at": saved["created_at"]}


def _user_feedback_public(record: dict) -> dict:
    """管理端展示形状：截图只报张数——路径与文件名不出这道门，
    取图另有带 RequireAdmin 的专用端点。"""
    return {
        "id": record["id"],
        "user_id": record["user_id"],
        "text": record["text"],
        "email": record.get("email", ""),
        "image_count": len(record.get("images", [])),
        "created_at": record["created_at"],
        "read": bool(record.get("read")),
        "read_at": record.get("read_at"),
    }


@app.get("/v1/admin/user-feedback")
def admin_list_user_feedback(_: Principal = RequireAdmin):
    """反馈收集列表：新的在前 + 未读计数，管理员页角标读的就是这两个数。"""
    items = [_user_feedback_public(r) for r in user_feedback_storage.list_all()]
    items.reverse()
    return {"items": items, "unread": sum(1 for r in items if not r["read"])}


@app.post("/v1/admin/user-feedback/{fb_id}/read")
def admin_mark_user_feedback_read(fb_id: str, actor: Principal = RequireAdmin):
    """标记已读。幂等：重复标记返回同一份记录，read_at 记第一次。"""
    record = user_feedback_storage.mark_read(fb_id)
    if record is None:
        raise HTTPException(status_code=404, detail="反馈不存在")
    audit.log(actor, "feedback.read", target=fb_id)
    return _user_feedback_public(record)


@app.delete("/v1/admin/user-feedback/{fb_id}")
def admin_delete_user_feedback(fb_id: str, actor: Principal = RequireAdmin):
    """删除一条反馈。**只许删已读**：闸门在服务层而不在界面按钮——
    用户要的是「管理员已读之后可以选择删除已读」，未读即消失的反馈等于
    用户的话可以没被看过就被销毁。删除连同截图一起清掉，编号不回收。"""
    status, record = user_feedback_storage.delete(fb_id)
    if status == "not_found":
        raise HTTPException(status_code=404, detail="反馈不存在")
    if status == "unread":
        raise HTTPException(status_code=409, detail="未读的反馈不能删除：先标记已读")
    audit.log(actor, "feedback.delete", target=fb_id,
              before={"user_id": record["user_id"], "created_at": record["created_at"]})
    return {"status": "deleted", "id": fb_id}


@app.get("/v1/admin/user-feedback/{fb_id}/image/{index}")
def admin_get_user_feedback_image(fb_id: str, index: int, _: Principal = RequireAdmin):
    """管理员读反馈截图。读的是反馈目录里那份复制件，不是用户的附件——
    所以这里不需要、也不存在一条"越属主读别人 uploads"的通道。"""
    record = user_feedback_storage.get(fb_id)
    if record is None:
        raise HTTPException(status_code=404, detail="反馈不存在")
    path = user_feedback_storage.image_path(record, index)
    if path is None:
        raise HTTPException(status_code=404, detail="截图不存在或已清理")
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(path, media_type=mime)

# ---------- 智能体 ----------
# v0.25 R1：这一面从 require_admin 改成了"登录可用 + 严格按属主"。旧注释写的
# 开放前提是"没有归属可谈"，今天前提已经翻面：Task 必填 user_id、无主旧条目在
# 恢复时认给部署者主账号（app/agents/task_store）。守卫换掉而归属没跟上 = 把
# 全表开放给全体注册用户，所以判据不在这里，在契约测试：
# tests/test_route_auth_contract.py（静态：缺鉴权/缺归属各自点名）与
# tests/test_v025_task_ownership_contract.py（行为：跨用户、枚举、记账、限流）。
def _resolve_agent_provider(principal: Principal, requested: str = None) -> dict:
    """把"这个人用哪条模型服务"定在叫模型之前：发起人自己的池子与默认
    （providers.default_for），全局默认不许顶替用户选过的东西。
    没配 provider 的人是 400 加一句给他看的话，不是 500——这是他的配置状态，
    不是服务故障。出口照全站规矩过 scrub_secrets。
    """
    try:
        return provider_store.resolve(requested, legacy_model=requested,
                                      user_id=principal.user_id)
    except ProviderError as e:
        raise HTTPException(status_code=400, detail=scrub_secrets(str(e)))


@app.post("/v1/agent/run")
def run_agent(request: AgentRequest, http: Request,
              principal: Principal = CurrentPrincipal):
    # 与聊天同一本限流账（先判断、才叫模型）：这一族从"管理员专享"变成
    # "登录可用"的那一刻起，它就不再是可以没有闸门的遗留端点。
    throttle_paid_upstream(http, principal)
    provider = _resolve_agent_provider(principal, request.model)
    try:
        from app.agents.react_agent import ReActAgent
        agent = ReActAgent(
            model=provider["model"],
            max_turns=request.max_turns
        )
        # user_id 必须往下传：needs_user 类工具（查日程/查记忆）靠执行器用服务端
        # 身份覆盖模型参数；provider_id 带上解析结果——统一出口按人记账。
        result = agent.run(
            task=request.task,
            max_duration=request.max_duration,
            user_id=principal.user_id,
            provider_id=provider["id"],
        )
        return {"result": result}
    except ImportError:
        return {"result": "智能体模块尚未就绪，请稍后再试"}

@app.post("/v1/agent/orchestrate")
def orchestrate_task(request: OrchestrateRequest, http: Request,
                     principal: Principal = CurrentPrincipal):
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="编排器模块尚未就绪")
    throttle_paid_upstream(http, principal)
    # 身份与解析出的模型服务都必须往下传：以前这一格写的是 `_: Principal`
    # （收下就丢），于是任务建出来不知道属于谁，而"先记着、读的时候再说"
    # 正是这个项目付过账的形状。
    provider = _resolve_agent_provider(principal)
    return orchestrator.run(
        goal=request.goal,
        task_id=request.task_id,
        user_id=principal.user_id,
        provider_id=provider["id"],
    )

# ---------- 任务状态 ----------
# 读侧与写侧同一道门：属主判定收在 _task_for_principal 一个函数里，四个端点
# 谁也别想绕过——与会话/附件"非属主即 404"同形，id 存在与否在这道门外不可知。
def _task_for_principal(task_id: str, user_id: str, is_admin: bool):
    """"这个任务是不是你的"的唯一答案。非属主与不存在：同一个 404、同一句话。

    admin 见全量是这一族从 v0.24 继承的既有能力（当年人人都要 admin），
    开放给普通用户之后不缩——但列表与详情都要把 user_id 带在响应里，
    管理页才答得出"这是谁的任务"。
    """
    task = task_store.get(task_id) if task_store else None
    if task is None or (not is_admin and task.user_id != user_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.get("/v1/tasks/{task_id}")
async def get_task_status(task_id: str, principal: Principal = CurrentPrincipal):
    if get_task is None:
        raise HTTPException(status_code=503, detail="任务存储模块尚未就绪")
    task = _task_for_principal(task_id, principal.user_id,
                               principal.role == "admin")
    response = {
        "task_id": task.task_id,
        "user_id": task.user_id,
        "legacy": bool(getattr(task, "legacy", False)),
        "goal": task.goal,
        "status": task.status,
        "current_subtask": task.current_subtask,
        "total_subtasks": len(task.subtasks),
        "subtasks": task.subtasks,
        "results": task.results if task.status == "completed" else None,
        "final_answer": task.final_answer,
        "error": task.error,
        "created_at": task.created_at,
        "cancelled": task.cancelled
    }
    if len(task.subtasks) > 0:
        response["progress_percent"] = round(
            (task.current_subtask / len(task.subtasks)) * 100, 1
        )
    else:
        response["progress_percent"] = 0
    return response

@app.get("/v1/tasks")
async def list_all_tasks(principal: Principal = CurrentPrincipal):
    if task_store is None:
        return {"total": 0, "tasks": []}
    is_admin = principal.role == "admin"
    # 非属主过滤走 task_store 自己的 tasks_of（那里是数据层唯一的"属于谁"口径），
    # 不在端点里再抄一份列表推导——抄的那份迟早和 getter 漂移。
    tasks = (list(task_store.values()) if is_admin
             else tasks_of(principal.user_id))
    return {
        "total": len(tasks),
        "tasks": [
            {
                "task_id": t.task_id,
                "goal": t.goal[:50] + "..." if len(t.goal) > 50 else t.goal,
                "status": t.status,
                "user_id": t.user_id,
                "legacy": bool(getattr(t, "legacy", False)),
                "progress": f"{t.current_subtask}/{len(t.subtasks)}",
                "created_at": t.created_at
            }
            for t in tasks
        ]
    }

@app.post("/v1/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, principal: Principal = CurrentPrincipal):
    task = _task_for_principal(task_id, principal.user_id,
                               principal.role == "admin")
    if TaskStatus and task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
        return {
            "status": "warning",
            "task_id": task_id,
            "message": f"任务已处于终态: {task.status.value}，无需取消"
        }
    task.mark_cancelled()
    return {
        "status": "cancelled",
        "task_id": task_id,
        "message": "任务已标记为取消，将在当前子任务完成后停止"
    }

@app.delete("/v1/tasks/{task_id}")
async def delete_task(task_id: str, principal: Principal = CurrentPrincipal):
    _task_for_principal(task_id, principal.user_id, principal.role == "admin")
    del task_store[task_id]
    return {"status": "deleted", "task_id": task_id}

# ---------- 官网 ----------
# 放最后只是因为这一节属于"对外长什么样",和上面那堆接口分开摆。
# R7 之后它不再承担顺序语义：没有 catch-all Mount,所以放早也不会吞掉 /v1。
from app.web.web_router import install_site
install_site(app)

# ---------- 启动入口 ----------
if __name__ == "__main__":
    # 默认绑回环。此前这里硬写 0.0.0.0，而 `.env.example` 写着 `HOST=127.0.0.1` 却
    # 没有任何代码读它——配置说谎 + 局域网里任何人都能直连源站。打包版
    # (run_backend.py) 一直绑的是 127.0.0.1，源码跑没理由更宽。
    # 对外只应经 Cloudflare 隧道；真要换部署形态，改这个环境变量，不是改代码。
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)