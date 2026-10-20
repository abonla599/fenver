"""`/health` 现在回的是「东西齐不齐」，不只是「进程还在」（app/core/selfcheck.py）。

进程活着这个判据，本仓已经证明过一次不够用：2026-09-20 那次 Error 1033，cloudflared
进程全程健在、看门狗每分钟全绿，而外面早就进不来。后端这边是同一个形状——密钥库
文件读不动、账本写不进、嵌入降级成全零伪嵌入、新限流账没登记，四种都不会让进程消失，
也全都不在旧 /health 的那一句 healthy 里。

所以这里两头都锁：坏东西必须被报出来（各条正向对照），以及**状态码不许跟着判决走**
（否则"配错了密钥"会变成看门狗每分钟重启一次后端，而重启修不好配置）。
"""
import getpass
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.core import selfcheck
from app.core.selfcheck import BROKEN, DEGRADED, NOT_CONFIGURED, OK, run


EXPECTED_CHECKS = {"模型服务", "密钥库", "用量账本", "记忆后端", "限流账", "数据落点"}


def test_health_reports_each_check_not_just_that_the_process_lives(client):
    body = client.get("/health").json()
    assert set(body["checks"]) == EXPECTED_CHECKS, body["checks"].keys()
    for label, item in body["checks"].items():
        assert item["status"] in (OK, DEGRADED, NOT_CONFIGURED, BROKEN), (label, item)
        assert item["detail"], f"{label} 报了状态却没说为什么"


def test_a_broken_verdict_still_returns_200(client, monkeypatch):
    """这条守的是一个决定，不是一个巧合。

    让 /health 在自检失败时返回 5xx，看门狗就会每分钟重启后端——而重启修不好任何
    配置问题，只会把正在进行的对话一起带走。判据交给日志与人。
    """
    monkeypatch.setattr(selfcheck, "CHECKS", (("模型服务", lambda: (BROKEN, "密钥库被删了")),))
    resp = client.get("/health")
    assert resp.status_code == 200, resp.status_code
    assert resp.json()["status"] == BROKEN


def test_a_check_that_raises_becomes_broken_instead_of_500(monkeypatch):
    """自检自己也会坏（磁盘没了、依赖 import 失败）。坏成一行 broken，不许坏成异常。"""
    def boom():
        raise RuntimeError("读盘读到一半炸了")

    monkeypatch.setattr(selfcheck, "CHECKS", (("用量账本", boom),))
    body = run()
    assert body["status"] == BROKEN
    item = body["checks"]["用量账本"]
    assert item["status"] == BROKEN and "RuntimeError" in item["detail"], item


def test_overall_status_is_the_worst_item(monkeypatch):
    monkeypatch.setattr(selfcheck, "CHECKS", (
        ("a", lambda: (OK, "好")),
        ("b", lambda: (DEGRADED, "凑合")),
        ("c", lambda: (BROKEN, "坏了")),
    ))
    assert run()["status"] == BROKEN
    monkeypatch.setattr(selfcheck, "CHECKS", (
        ("a", lambda: (OK, "好")), ("b", lambda: (DEGRADED, "凑合")),
    ))
    assert run()["status"] == DEGRADED


def test_every_status_is_registered_in_severity_and_survives_the_merge(monkeypatch):
    """状态全集钉死：新增状态却不登记 `_SEVERITY`，run() 会在合并时 KeyError。

    那不是一条测试红，而是 /health 500：pre_tag_gate BLOCK、换包中止、看门狗把
    整轮巡检 catch 掉。`not_configured` 就是第四个状态，所以这里既钉全集，也把
    每个状态真的走一遍合并——只钉集合不跑合并的话，合并那段被改坏仍然发现不了。
    """
    assert set(selfcheck._SEVERITY) == {OK, DEGRADED, NOT_CONFIGURED, BROKEN}
    assert selfcheck._SEVERITY[OK] < selfcheck._SEVERITY[NOT_CONFIGURED] \
        < selfcheck._SEVERITY[BROKEN]

    for status in (OK, DEGRADED, NOT_CONFIGURED, BROKEN):
        monkeypatch.setattr(selfcheck, "CHECKS",
                            (("单项", lambda s=status: (s, "原因")),))
        assert run()["status"] == status


# ------------------------------------------------------------------ 各条判据 --

def test_key_vault_disagreeing_with_the_records_is_broken(monkeypatch, tmp_path):
    """记录自报带密钥、密钥库文件却不在了——"搬了记录没搬密钥"那一类。

    症状是聊天全 401，而这一步之前界面上看一切都正常。
    """
    from app.core import providers

    class _Store:
        keys_path = str(tmp_path / "provider_keys.json")

        @staticmethod
        def all():
            return [{"id": "p1", "api_key": "sk-a" + "1" * 20}]

    monkeypatch.setattr(providers, "store", _Store)
    status, detail = selfcheck._check_key_vault()
    assert status == BROKEN, detail
    assert "1 条配置自报带密钥" in detail

    (tmp_path / "provider_keys.json").write_text("{ not json", encoding="utf-8")
    assert selfcheck._check_key_vault()[0] == BROKEN

    (tmp_path / "provider_keys.json").write_text(
        json.dumps({"p1": "sk-a" + "1" * 20}), encoding="utf-8")
    assert selfcheck._check_key_vault()[0] == OK


def test_dummy_embeddings_are_degraded_and_say_so(monkeypatch):
    """全零伪嵌入 add/query 都不报错，只是检索结果完全不可信。

    这条是 degraded 不是 broken：不影响"能不能聊"，影响"记不记得住"。但它必须说得出
    来——否则它就是一个没人知道的功能缺失，而这正是本项目最贵的那类问题。
    """
    from app.memory import memory_router

    class _M:
        _dummy_embed = True

    monkeypatch.setattr(memory_router, "memory_manager", _M, raising=False)
    status, detail = selfcheck._check_memory_backend()
    assert status == DEGRADED and "伪嵌入" in detail, (status, detail)

    monkeypatch.setattr(memory_router, "memory_manager", None, raising=False)
    monkeypatch.setattr(memory_router, "memory_init_error", "维度冲突", raising=False)
    status, detail = selfcheck._check_memory_backend()
    assert status == BROKEN and "维度冲突" in detail, (status, detail)


def test_a_ledger_without_a_window_is_caught(monkeypatch):
    """`_prune` 只遍历 `_LEDGERS`，且按每本账自己的窗口裁剪。

    新写一本账忘了登记，或者登记了但窗口给了 0：内存随来源数单向涨，限流本身照样
    生效，所以没有任何可观察症状——这种"只对一半"的账正是本文件存在的理由。
    """
    from app.core import auth_router

    extra = dict(test=dict())
    monkeypatch.setattr(auth_router, "_LEDGERS", ((extra, 0),), raising=False)
    status, detail = selfcheck._check_limiters()
    assert status == BROKEN and "窗口" in detail, (status, detail)

    monkeypatch.setattr(auth_router, "_LEDGERS", (), raising=False)
    assert selfcheck._check_limiters()[0] == BROKEN


def test_real_ledgers_all_carry_a_window():
    """正向对照的反面：今天真实登记的那几本必须过这条，否则上一条是空转的。"""
    from app.core import auth_router

    assert len(auth_router._LEDGERS) >= 5, "限流账少于五本，这条锁就检不到东西了"
    assert selfcheck._check_limiters()[0] == OK, selfcheck._check_limiters()[1]


def test_missing_data_directory_is_broken(monkeypatch, tmp_path):
    """数据落在不存在的目录里 = 下一次写才炸。提前到自检这里说。"""
    from app.core import paths

    monkeypatch.setattr(paths, "resolve_all_data_paths",
                        lambda: [("会话", str(tmp_path / "nope" / "sessions.json"))])
    status, detail = selfcheck._check_data_paths()
    assert status == BROKEN and "会话" in detail, (status, detail)

    assert selfcheck._check_data_paths.__doc__, "判据为什么在这儿，得写下来"


def test_a_label_that_happens_to_contain_a_key_shape_is_scrubbed(monkeypatch, tmp_path):
    """/health 是免鉴权的公开端点，而管理员给配置起的名字是人手打的。

    详情一句要经过出口脱敏才回得去——同一件事在 /v1/providers 上已经做过（A-2），
    这里是第二个出口，容易被忘记的就是这种"第二个出口"。

    2026-10-07 起这条锁的**机制**变了，但判据没变：模型服务这一项不再回显 label
    与 model（只回"已配置 N 条"），所以那个带 key 形状的名字压根没进过详情。
    也就是说，脱敏从"最后一道网"退成了纵深防御——原先这条断言盯的是"网还在"，
    现在盯的是"根本没有东西需要网来接"。
    """
    from app.core import providers

    secret = "sk-" + "a1b2c3d4" * 4

    class _Store:
        keys_path = str(tmp_path / "provider_keys.json")

        @staticmethod
        def all():
            return [{"id": "p1", "label": "我的", "api_key": secret, "model": "m"}]

        @staticmethod
        def resolve():
            return {"id": "p1", "label": f"我的{secret}号", "model": "m"}

    monkeypatch.setattr(providers, "store", _Store)
    body = json.dumps(run(), ensure_ascii=False)
    assert secret not in body, "自检详情把密钥原样带出去了"
    # 连"名字"本身都不该出现，而不只是名字里的密钥被替换掉
    assert "我的" not in body, body


def test_scrubbing_still_catches_a_key_shape_from_another_check(monkeypatch):
    """脱敏本身不能因为模型服务那项不再回显 label 就一起废掉。

    上面那条锁证明的是"这一项不回显"。这一条证明的是**出口那道网还在**：别的自检项
    （将来新增的、或者 ProviderError 原文之类）一旦带出 key 形状，_scrub 仍要接住。
    少了这一条，把 _scrub 删掉换成一个恒等函数，测试依然全绿——那才叫假覆盖。
    """
    secret = "sk-" + "b7c8d9e0" * 4

    monkeypatch.setattr(selfcheck, "CHECKS", (
        ("用量账本", lambda: (OK, f"回传 usage 的那家 key 是 {secret}")),
    ))
    body = json.dumps(run(), ensure_ascii=False)
    assert secret not in body, "出口脱敏被摘掉了，别的自检项带出的密钥会直发"
    assert "已隐去" in body or "末四位" in body, body


def test_scrubbing_fails_closed_when_the_vault_itself_is_unreadable(monkeypatch):
    """脱敏要看密钥库，而"密钥库读不动"恰是这里要报的那类故障。

    两个方向差一个字：把详情原样发出去是失败开放（路人读到 key），换成一句
    "说不清"是失败封闭（少一条线索）。这里必须选后者。
    """
    from app.core import providers

    def boom(_text):
        raise OSError("密钥库文件被别的进程锁着")

    monkeypatch.setattr(providers, "scrub_secrets", boom)
    body = run()
    assert body["checks"], "自检什么都不肯报，这条锁就是空转"
    for label, item in body["checks"].items():
        assert item["detail"] == "（详情无法安全呈现）", (label, item)
        assert item["status"] in (OK, DEGRADED, NOT_CONFIGURED, BROKEN), item


def test_no_absolute_paths_leak_into_the_public_answer(client):
    """另一处泄露面：进程目录结构。探针要的判据是 ok/不 ok，不是这台机器长什么样。"""
    body = json.dumps(client.get("/health").json(), ensure_ascii=False)
    assert "Users" not in body and "\\" not in body, body


# ------------------------------------------------ 记忆初始化失败也不许报机器长相 --

# collection 元数据里那枚"建库时用的哪款嵌入模型"是探针最想白捡的信息（哪家在替我们
# 做嵌入），拿一个假名当它的气味探针，真冲突了就知道它有没有跟着异常原文出来。
CANARY_MODEL = "vendorx-embed-9k"
REPO_DIR_NAME = Path(__file__).resolve().parents[2].name
# 盘符路径 = 单字母 + `:` + 分隔符。豁免只认 URL scheme 那一形：`scheme 字符` 紧跟
# `单字母 + ://` 时放过，粘着 `+ - .` / 字母 / 数字却用单反斜杠写的路径（`at-C:\Users\a`）
# 仍算路径。写成两支是因为探测器只看"有没有"：一支要求路径前不是 scheme 字符，另一支在
# 前面是 scheme 字符时要求分隔符不是 `//`——两支并起来才和生产网 `core.pathmask._WIN_PATH`
# 的一支负向前瞻同判。这个同判由 `test_pathmask.py` 的绑定锁按源码点名守住：这里改窄
# （探测器瞎在生产网瞎的同一处 ⇒ 下面两条断言退化成恒绿自证）或改宽（遇合法 URL 恒红）都会红。
_DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9+.-])(?P<drive>[A-Za-z]):[\\/]"
                         r"|[A-Za-z0-9+.-][A-Za-z]:(?!//)[\\/]")
# 模型名的形状：连字符/下划线分隔的三段以上 ASCII 标识符（text-embedding-3-small、
# all-MiniLM-L6-v2 都是这一族）。单段或两段（user_memories）不算，免得把集合名当模型名。
_MODEL_SHAPED = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+){2,}", re.IGNORECASE)


def _a_real_dimension_conflict(tmp_path):
    """真的让 MemoryManager 起坏一次，拿它实际抛出的那句原文与库根位置。

    先按 1536 维把 collection 建好（元数据里的模型名换成假名），再拿一枚 3 维的
    注入向量去开它——走的正是 `_open_collection` 那条维度判据。库根只落在
    tmp_path，绝不碰仓库里那份 chroma_db。
    """
    import chromadb

    from app.memory.memory_manager import MemoryManager

    root = tmp_path / "chroma-conflict"
    seed = chromadb.PersistentClient(path=str(root))
    seed.create_collection("user_memories", metadata={
        "embedding_model": CANARY_MODEL,
        "embedding_dim": "1536",
        "hnsw:space": "cosine",
    })
    with pytest.raises(RuntimeError) as caught:
        MemoryManager(persist_dir=str(root), embedding_fn=lambda _text: [0.1, 0.2, 0.3])
    return str(caught.value), str(root)


_EMBED_ENV = ("api_key", "EMBEDDINGS_BASE_URL", "EMBEDDINGS_MODEL")


def _production_dummy_conflict(tmp_path, monkeypatch):
    """按线上形状起坏一次：旧库 1536 维，当前后端是生产伪嵌入的 384 维。

    与 `_a_real_dimension_conflict` 的区别只在向量来源：那条用 3 维注入替身证明
    "维度判据会响"，这条用 `_dummy_embed_dim=384` 的真实降级分支，证明"没配嵌入
    的机器撞上云端旧库"这个生产形状确实会响，而不是只有替身才响。
    """
    import chromadb

    from app.memory.memory_manager import MemoryManager

    for key in _EMBED_ENV:
        monkeypatch.delenv(key, raising=False)
    # 本地模型也不可用 → 走 `_dummy_embed_dim = 384` 那条生产降级分支。
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    root = tmp_path / "chroma-prod-shape"
    seed = chromadb.PersistentClient(path=str(root))
    seed.create_collection("user_memories", metadata={
        "embedding_model": CANARY_MODEL,
        "embedding_dim": "1536",
        "hnsw:space": "cosine",
    })
    with pytest.raises(RuntimeError) as caught:
        MemoryManager(persist_dir=str(root))
    return caught.value, str(root)


def test_production_dummy_384_conflicts_with_an_existing_1536_collection(tmp_path, monkeypatch):
    """缺嵌入配置的生产机：伪嵌入 384 维撞上 1536 维旧库，开集合必须失败。

    旧用例拿 3 维注入向量制造冲突；3 只是测试随手挑的数，而生产伪嵌入的
    `_dummy_embed_dim` 是 384。这条按生产形状再走一遍：如果判据只对 3 维成立
    （例如有人对注入向量特判），这里当场红。
    """
    from app.memory.memory_manager import EmbeddingDimConflict

    error, root = _production_dummy_conflict(tmp_path, monkeypatch)
    assert isinstance(error, EmbeddingDimConflict), type(error)
    assert error.dim_conflict is True
    message = str(error)
    assert "1536" in message and "384" in message, message
    assert root not in message, "库路径只该进日志，不该随异常原文走到 /health"


def test_dim_conflict_without_any_embedding_config_is_not_configured(tmp_path, monkeypatch):
    """第四态：维度冲突 + 三件嵌入配置全缺 = "没配"，不是"坏了"。

    这是开发机/新机器最常见的形状：没有嵌入配置 → 伪嵌入 384 维 → 撞上 1536 维
    旧库 → 记忆后端起不来。报 broken 会让巡检每次都拉一条 ERROR，而运维能做的
    只是"去把嵌入配好"，不是"去修一个故障"。
    """
    from app.memory import memory_router

    error, root = _production_dummy_conflict(tmp_path, monkeypatch)
    monkeypatch.setattr(memory_router, "memory_manager", None, raising=False)
    monkeypatch.setattr(memory_router, "memory_init_error", error, raising=False)

    status, detail = selfcheck._check_memory_backend()
    assert status == NOT_CONFIGURED, (status, detail)
    assert "未配置嵌入后端" in detail and "记忆检索不可用" in detail, detail
    # 隐私口径不变：没有库路径、没有模型名，连维度数字也不进这条配置提示。
    for forbidden in (root, CANARY_MODEL, "1536", "384"):
        assert forbidden not in detail, f"{forbidden} 出现在公开详情里：{detail}"
    assert not _DRIVE_PATH.search(detail), detail
    assert not _MODEL_SHAPED.search(detail), detail


def test_a_real_failure_stays_broken_even_when_embedding_config_is_missing(monkeypatch):
    """缺配置不能把磁盘故障洗成"没配"：判据只认 `dim_conflict` 标记。

    OSError 与"没配嵌入"可以同时发生，但前者不是后者造成的；把它报成
    not_configured，等于把一次真故障降级成一条配置提示。
    """
    from app.memory import memory_router

    for key in _EMBED_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(memory_router, "memory_manager", None, raising=False)
    monkeypatch.setattr(memory_router, "memory_init_error",
                        OSError("attempt to write a readonly database"), raising=False)

    status, detail = selfcheck._check_memory_backend()
    assert status == BROKEN, (status, detail)
    assert "readonly database" in detail, detail


def test_dim_conflict_is_not_excused_when_embedding_config_is_present(monkeypatch):
    """同一枚维度冲突，只要三件配置里有一件在，就不是"没配"。

    已经在配嵌入的机器上出现维度冲突，说明后端或库被换了，属于真故障。这条挡住
    "看到 dim_conflict 就一律降级"的写法——那会让线上真故障静音。
    """
    from app.memory import memory_router
    from app.memory.memory_manager import EmbeddingDimConflict

    monkeypatch.setattr(memory_router, "memory_manager", None, raising=False)
    monkeypatch.setattr(memory_router, "memory_init_error",
                        EmbeddingDimConflict("记忆库嵌入维度冲突"), raising=False)

    for key in _EMBED_ENV:
        monkeypatch.delenv(key, raising=False)
    assert selfcheck._check_memory_backend()[0] == NOT_CONFIGURED

    for key in _EMBED_ENV:
        monkeypatch.setenv(key, "configured")
        assert selfcheck._check_memory_backend()[0] == BROKEN, \
            f"{key} 已配置，维度冲突还报 not_configured"
        monkeypatch.delenv(key)


# 子进程的标记行：chromadb 自己也会往 stdout 打东西，只认这一行。
_CHILD_MARKER = "SELFCHECK-RESULT "


def test_memory_router_import_time_failure_reaches_health_as_not_configured(tmp_path):
    """真的在导入期起坏一次：memory_manager 自然为 None，router 自己捕获异常对象。

    上面那些用例手工把 `memory_router.memory_manager` 摆成 None。这里在子进程里
    走真实路径：旧库 1536 维 + 无嵌入配置 + 本地模型不可用 → `MemoryManager()`
    在 `memory_router` 导入时抛错 → router 捕获并保留异常对象 → 自检读出
    `not_configured`。子进程是必须的：包内 reload 会把 router 与模块全局换掉，
    污染同一进程里后面所有记忆用例。
    """
    data = tmp_path / "data"
    data.mkdir()
    env = os.environ.copy()
    # Windows 的 os.environ.copy() 给的是大写键，大小写两种都摘一遍才摘得干净。
    for key in _EMBED_ENV:
        for name in (key, key.upper()):
            env.pop(name, None)
    backend_dir = Path(__file__).resolve().parents[1]
    env.update({
        "DATA_ROOT": str(data),
        "CHROMA_DB_PATH": str(tmp_path / "chroma-import-time"),
        "CI": "",
        "USE_FAKE_STORE": "",
        # 子进程的中文警告也要按 UTF-8 出，否则 Windows 管道解码会炸。
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONPATH": os.pathsep.join(
            [str(backend_dir)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])),
    })
    script = f"""
import json, os, sys
sys.modules["sentence_transformers"] = None
import chromadb
seed = chromadb.PersistentClient(path=os.environ["CHROMA_DB_PATH"])
seed.create_collection("user_memories", metadata={{
    "embedding_model": {CANARY_MODEL!r},
    "embedding_dim": "1536",
    "hnsw:space": "cosine",
}})
from app.memory import memory_router as mr
from app.core import selfcheck
item = selfcheck.run()["checks"]["记忆后端"]
error = mr.memory_init_error
print({_CHILD_MARKER!r} + json.dumps({{
    "manager_is_none": mr.memory_manager is None,
    "error_kept_as_object": error is not None and not isinstance(error, str),
    "dim_conflict": bool(getattr(error, "dim_conflict", False)),
    "status": item["status"],
    "detail": item["detail"],
}}, ensure_ascii=True))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=str(backend_dir), env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stderr
    lines = [l for l in proc.stdout.splitlines() if l.startswith(_CHILD_MARKER)]
    assert lines, proc.stdout
    payload = json.loads(lines[-1][len(_CHILD_MARKER):])

    assert payload["manager_is_none"] is True
    assert payload["error_kept_as_object"] is True, \
        "router 又把异常转成了字符串，dim_conflict 已经在路上丢了"
    assert payload["dim_conflict"] is True
    assert payload["status"] == NOT_CONFIGURED
    assert "未配置嵌入后端" in payload["detail"]
    assert CANARY_MODEL not in payload["detail"]
    assert str(tmp_path) not in payload["detail"]


def test_a_real_memory_init_failure_never_describes_the_machine(client, tmp_path, monkeypatch):
    """免鉴权端点上唯一一句由异常原文拼出来的详情，必须说不出这台机器长什么样。

    上面那条 `test_no_absolute_paths_leak_into_the_public_answer` 检不到这一格：
    pytest 下记忆后端恒走内存替身分支，真实的库路径压根没机会出现在详情里。
    这里把真 manager 起坏一次，再把它抛出的原话摆进 memory_init_error，然后按
    "任何路人打到 /health 能读到的一切"来断言——序列化整份响应体，不是只看某一句。
    """
    from app.memory import memory_router

    message, root = _a_real_dimension_conflict(tmp_path)
    assert "1536" in message and "3 维" in message, \
        "冲突的可判断依据被改没了：维度数字是运维唯一需要的部分"

    monkeypatch.setattr(memory_router, "memory_manager", None, raising=False)
    monkeypatch.setattr(memory_router, "memory_init_error", message, raising=False)

    body = json.dumps(client.get("/health").json(), ensure_ascii=False)
    item = json.loads(body)["checks"]["记忆后端"]
    assert item["status"] == BROKEN, item
    assert "维度冲突" in item["detail"], item

    for forbidden in (root, getpass.getuser(), REPO_DIR_NAME, CANARY_MODEL, "chroma_db"):
        assert forbidden not in body, f"{forbidden} 被公开端点回显了：{body}"
    assert not _DRIVE_PATH.search(body), body
    assert not _MODEL_SHAPED.search(json.dumps(item, ensure_ascii=False)), item


def test_the_exit_net_hides_paths_from_error_text_we_did_not_write(client, monkeypatch):
    """第二层守的是"我们改不动的那句"。

    memory_init_error 是第三方异常的原文：chroma/SQLite 读盘失败会把库路径写进
    OSError 的 message 里，措辞不在我们手里。scrub_secrets 的模式只认凭据形状，
    绝对路径压根不在射程内——所以出口这里补一道，且只掩路径那一段：整句吞掉等于
    把故障说成"说不清"，明写的掩码才让人知道后半句还在、只是位置不能公开。
    """
    from app.memory import memory_router

    bs = chr(92)
    windows = "C:" + bs + "Users" + bs + "someone" + bs + "proj" + bs + "chroma_db"
    posix = "/home/someone/proj/chroma_db"
    monkeypatch.setattr(memory_router, "memory_manager", None, raising=False)
    monkeypatch.setattr(memory_router, "memory_init_error",
                        f"attempt to write a readonly database（{windows}），另有 {posix} 一处读不动",
                        raising=False)

    body = json.dumps(client.get("/health").json(), ensure_ascii=False)
    item = json.loads(body)["checks"]["记忆后端"]
    assert item["status"] == BROKEN, item
    assert "readonly database" in item["detail"], "整句被吞了：该隐去的只有路径"
    assert "〈路径已隐去〉" in item["detail"], item
    assert windows not in body and posix not in body, body
    assert not _DRIVE_PATH.search(body), body
