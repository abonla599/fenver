"""测试进程写现网 data/ 的机器锁（#77）。

本机仓库目录同时是生产机（AGENTS.md §1），而 conftest 顶部那四条重定向
（`APPROVALS_DB_PATH` / `USER_FEEDBACK_FILE` / `FEEDBACK_FILE` / `PREFERENCE_FILE`）
是测试与现网 `data/` 之间唯一的一道墙。2026-10-17 已实证过一次越界：在 pytest
之外直接 exec 一个测试文件，12 条审批票据被写进了现网 `data/approvals.json`。

这道锁分三路，分别回答三个问题：

1. **谁写的**（写者归因，精确）：pytest 进程里挂一个 `sys.addaudithook`，任何以
   写模式打开 `<repo>/data/**`（以及项目根那几份历史 feedback/preference）的调用
   当场抛 `ProductionDataWriteError`。线上那个 300 秒定时器住在**另一个进程**里，
   它重写 `data/preference*.txt` 不会被这条看见——这就是"测试写的"与"定时器写的"
   能分开的依据，不靠猜 mtime。
2. **净变化**（会话对账，兜底）：`session_snapshot()` 只 `stat`、不读内容，记
   `(mtime_ns, size)`。会话结束逐条对比：
   - 严格档（`data/approvals.json`、两份 feedback）：变了就红；
   - 定时器档（`data/preference*.txt`）：线上每 300 秒重写一次，严格对账必然
     误红，所以不在这里判，改由第 3 条 fail-closed 兜住。
3. **生效路径必须落在测试临时根内**：`effective_watched_paths()` 取测试进程真正
   会用的值（审批票据走 `_default_path()` 现算，其余是模块常量），
   `path_violations()` 要求每一条都在 `_TEST_DATA_DIR` / 系统临时根之内、且不在
   仓库目录之下。指回仓库的绝对路径、相对路径常量、把 env 指到现网，都算违规。

与 #54 的分工：#54 补的是"值级路径闸"（`resolve_all_data_paths()` 那份全量表）；
这里只覆盖 #77 这四条会被测试写脏的路径，不抄那张全量表，也不改 conftest 已有的
重定向。等 #54 落地后，`effective_watched_paths()` 可以改为调用它。
"""
import os
import sys
from pathlib import Path


class ProductionDataWriteError(AssertionError):
    """测试进程在写现网数据。继承 AssertionError，pytest 会把它当断言失败报。"""


def repo_root() -> Path:
    """本文件在 backend/tests/ 下，向上两层就是仓库根（与 conftest 同一口径）。"""
    return Path(__file__).resolve().parents[2]


def _canonical(path) -> str:
    """比较用的绝对路径：realpath（消 symlink）+ normcase（Windows 大小写）。"""
    return os.path.normcase(
        os.path.realpath(os.path.abspath(os.fsdecode(os.fspath(path)))))


def _within(path: str, root: str) -> bool:
    """按分隔符对齐的前缀判据：`/data` 本身算在里面，`/databak` 不算。"""
    return path == root or path.startswith(root + os.sep)


def _stamp(path: Path):
    """(mtime_ns, size)；文件不存在是 None——"从无到有"也是一种变化。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def production_candidates(repo=None):
    """(严格档, 定时器档)：每一档是 {中文标签: (Path, ...)}。

    严格档也列**还不存在**的候选：测试把生产文件从"没有"写成"有"同样是越界，
    `_stamp(None)` 正好把这种出现表达成 None -> (mtime_ns, size)。

    两类反馈文件各列两条：`paths.data_file()` 在 `data/` 没有同名文件时会退回项目根
    那份历史文件，测试若硬编码了老路径，写的就不是 data/ 那一份。
    """
    root = Path(repo or repo_root())
    data = root / "data"
    strict = {
        "审批票据": (data / "approvals.json",),
        "意见反馈": (data / "user_feedback.json", root / "user_feedback.json"),
        "反馈原文": (data / "feedback.json", root / "feedback.json"),
    }
    # 偏好摘要是线上 300 秒定时器的常规产物：preference.txt 与按人分账的
    # preference-<uid>.txt 都会每个 tick 重写一遍，不进严格档。
    timer_paths = {data / "preference.txt", root / "preference.txt"}
    timer_paths.update(data.glob("preference*.txt"))
    timer_paths.update(root.glob("preference*.txt"))
    timer = {"偏好摘要": tuple(sorted(timer_paths, key=str))}
    return strict, timer


def _flatten(groups):
    return {(label, str(path)): _stamp(path)
            for label, paths in groups.items() for path in paths}


def session_snapshot(repo=None):
    strict, timer = production_candidates(repo)
    return {"strict": _flatten(strict), "timer": _flatten(timer)}


def snapshot_changes(before, after):
    """[(标签, 路径, 旧戳, 新戳), ...]，只列变了或出现/消失的。"""
    keys = set(before) | set(after)
    return [(label, path, before.get((label, path)), after.get((label, path)))
            for label, path in sorted(keys)
            if before.get((label, path)) != after.get((label, path))]


def _failed(exc):
    return f"<读取失败 {type(exc).__name__}: {exc}>"


def _module_attr(module_name, attr):
    """模块常量；导入/属性缺失都转成占位串，让 fail-closed 判据把它当违规。"""
    try:
        module = __import__(module_name, fromlist=[attr])
        return getattr(module, attr)
    except Exception as exc:                       # noqa: BLE001 - 读不到就 fail closed
        return _failed(exc)


def effective_watched_paths():
    """测试进程真正会用的四条路径：[(标签, 值), ...]。

    审批票据走 `_default_path()` 现算（它每次读 `APPROVALS_DB_PATH`）；另外三条在
    导入期就算成模块常量，环境变量事后改动不会影响它们，所以必须读模块常量本身。
    `feedback_storage` 与 `preference_analyzer` 各有一份 `FEEDBACK_FILE`，两份都读。
    """
    try:
        from app.core import approval
        approvals = os.environ.get("APPROVALS_DB_PATH") or approval._default_path()
    except Exception as exc:                       # noqa: BLE001 - 读不到就 fail closed
        approvals = _failed(exc)
    pairs = [("审批票据", str(approvals))]
    for label, module_name, attr in (
            ("意见反馈", "app.user_feedback_storage", "USER_FEEDBACK_FILE"),
            ("反馈原文(feedback_storage)", "app.feedback_storage", "FEEDBACK_FILE"),
            ("反馈原文(preference_analyzer)", "app.preference_analyzer", "FEEDBACK_FILE"),
            ("偏好摘要", "app.preference_analyzer", "PREFERENCE_FILE")):
        pairs.append((label, str(_module_attr(module_name, attr))))
    return pairs


def path_violations(pairs, allowed_roots, repo=None):
    """[(标签, 解析后的路径/占位串, 原因), ...]：空值、仓库内、临时根外都算违规。

    反例喂的是**手工造的清单**，不是真把 env 指到仓库：本目录同时是生产机，把真库
    路径放进环境里哪怕一瞬，后面任何一次 flush 都可能照着那一刻的环境写盘。
    """
    repo_dir = _canonical(repo or repo_root())
    roots = [_canonical(root) for root in allowed_roots]
    violations = []
    for label, raw in pairs:
        text = "" if raw is None else str(raw).strip()
        if not text or text.startswith("<读取失败"):
            violations.append((label, text, "空值或读取失败（fail closed）"))
            continue
        resolved = _canonical(text)
        if _within(resolved, repo_dir):
            violations.append(
                (label, resolved, "落在仓库目录之下（本机仓库目录就是生产机）"))
            continue
        if not any(_within(resolved, root) for root in roots):
            violations.append((label, resolved, "不在测试临时根之内"))
    return violations


def format_violations(where, violations):
    lines = [f"{where}：测试进程的可变数据落点越出了测试临时根（fail closed）。"]
    for label, path, reason in violations:
        lines.append(f"  - {label}: {path!r} —— {reason}")
    lines.append(
        "这四条路径（APPROVALS_DB_PATH / USER_FEEDBACK_FILE / FEEDBACK_FILE / "
        "PREFERENCE_FILE）是测试与现网 data/ 之间唯一的墙；测试请用 tmp_path 或 "
        "conftest 的临时目录，不要在用例里硬编码仓库 data/ 的路径。")
    return "\n".join(lines)


def format_strict_changes(changes):
    lines = ["测试会话期间现网数据被改过（严格档：这些文件没有 300 秒定时器重写）："]
    for label, path, before, after in changes:
        lines.append(f"  - {label}: {path}  {before} -> {after}")
    lines.append(
        "测试进程没有把它指到临时根。请用 tmp_path / _TEST_DATA_DIR，"
        "并检查是不是又有人在 pytest 之外 exec 了仓内测试文件。")
    return "\n".join(lines)


class _AuditHandle:
    def __init__(self, records):
        self.enabled = True
        self.records = records

    def disable(self):
        """只给反例用：审计钩子装上去就摘不掉，用开关模拟"这个根不再监视"。"""
        self.enabled = False


def _write_intent(mode, flags):
    """这次 open 是不是写模式。builtin open 给 mode 字符串，os.open 给 flags 整数。"""
    if isinstance(mode, str):
        return any(ch in mode for ch in "wax+")
    if mode is None and isinstance(flags, int):
        bits = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        return bool(flags & bits)
    return False


def install_write_audit_hook(repo=None, sink=None):
    """在**当前进程**里拦下所有对 `<repo>/data/**` 与项目根历史 feedback/preference
    的写操作。返回一个带 `disable()` 的句柄；拦截记录同时追加进 `sink`（若有）。

    为什么用审计钩子而不是只靠前后 mtime：钩子只看见**本进程**的写，线上定时器
    在另一个进程里，两者因此可以分开。mtime 对账留在 `ProductionWriteGuard.finish`，
    防的是子进程（钩子不跨进程）与"写回同字节但动了 mtime"的形状。
    """
    root = Path(repo or repo_root()).resolve()
    data_dir = _canonical(root / "data")
    repo_dir = _canonical(root)
    records = sink if sink is not None else []
    handle = _AuditHandle(records)

    def watched(target):
        if not isinstance(target, (str, bytes, os.PathLike)):
            return False
        resolved = _canonical(target)
        if _within(resolved, data_dir):
            return True
        if os.path.dirname(resolved) == repo_dir:
            name = os.path.basename(resolved)
            if name in ("feedback.json", "user_feedback.json"):
                return True
            if name.startswith("preference") and name.endswith(".txt"):
                return True
        return False

    def hook(event, args):
        if not handle.enabled:
            return
        if event == "open":
            target, mode, flags = args
            if not _write_intent(mode, flags):
                return
            targets = (target,)
        elif event == "os.rename":
            # 两个端点都要看：把生产文件搬走同样是改生产数据
            targets = (args[0], args[1])
        elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.truncate"):
            targets = (args[0],)
        else:
            return
        for target in targets:
            if not watched(target):
                continue
            message = (
                f"测试进程试图写现网数据：{_canonical(target)}（{event}）。"
                "本机仓库目录同时是生产机；仓内测试只能在 pytest 下跑，"
                "并把四条数据路径指到测试临时根。")
            records.append(message)
            raise ProductionDataWriteError(message)

    sys.addaudithook(hook)
    return handle


class ProductionWriteGuard:
    """一次 pytest 会话的锁：装钩子、拍快照、会话前后各查一次生效路径。"""

    def __init__(self, repo_root=None, allowed_roots=None):
        self.repo = Path(repo_root or repo_root()).resolve()
        self.allowed_roots = tuple(allowed_roots or ())
        self.handle = None
        self.started = None
        self.attempts = []
        self.timer_changes = []

    def install(self):
        self.handle = install_write_audit_hook(self.repo, sink=self.attempts)
        self.started = session_snapshot(self.repo)
        return self

    def check_effective_paths(self, where):
        violations = path_violations(
            effective_watched_paths(), self.allowed_roots, self.repo)
        assert not violations, format_violations(where, violations)

    def finish(self):
        if self.handle is not None:
            self.handle.disable()
        assert not self.attempts, (
            "测试进程尝试写过现网数据（审计钩子当场拦住，操作未生效；"
            "但尝试本身就是违规）：\n  " + "\n  ".join(self.attempts))
        after = session_snapshot(self.repo)
        changed = snapshot_changes(self.started["strict"], after["strict"])
        assert not changed, format_strict_changes(changed)
        # 定时器档只记录不判：线上 300 秒定时器会重写 data/preference*.txt。测试进程
        # 若真写过，上面的审计钩子已经记进 self.attempts 并当场抛过了。
        self.timer_changes = snapshot_changes(self.started["timer"], after["timer"])


_ACTIVE = None


def activate(guard):
    """conftest 注册本次会话的锁；测试通过 `active()` 用同一份允许根。"""
    global _ACTIVE
    _ACTIVE = guard
    return guard


def active():
    assert _ACTIVE is not None, "conftest 没有把 #77 的会话锁挂上"
    return _ACTIVE
