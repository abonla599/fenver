"""E1：数据根独占锁 + 落盘临时名唯一性。

两条判据，各配负向锁：

1. `write_json_atomic` 的 tmp 名必须**唯一**。原写法 `path + ".tmp"` 是固定名，
   两个进程同时写同一个数据文件时两次 `os.open` 拿到同一 inode，两个 fd 交错写
   进去，落盘是半截 JSON 拼接。
2. 数据根必须**独占**：第二个进程启动即 fail-fast，而不是静默覆盖。

第2 条为什么必须做在启动期而不是运行期：这些 JSON 存储的写入口径是整份内存
快照（`session_store._flush` 每次写全量），读-改-写整个序列在内存里完成。于是两个
进程各自持有过期快照时，落盘层面再怎么原子也救不了——A 的第 2 条被 B 的第 3 条
整体覆盖，两边都返回 200。**丢的是整份数据。**

负向锁：把 mkstemp 换回固定名 → 第 1 组红；把 pid 存活判据改成"永远当成陈旧锁" →
第 2 组里"真冲突必须拒绝"红。
"""
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest


# ---------- 判据 1：落盘临时名唯一 ----------

def test_atomic_write_uses_unique_tmp_name():
    """负向锁：把 mkstemp 换回 `path + ".tmp"`，这条测试红。

    判据盯的是**真正决定临时名的那个调用与那个赋值**，不是全文搜字符串：模块文档里
    正好引用了 `tmp = path + ".tmp"` 这段反例来说明为什么不能用它（`atomic_write.py`
    的 docstring），搜全文会匹配到自己的说明文字，于是实现无论对不对都红。
    """
    import ast
    from app.core import atomic_write

    source = open(atomic_write.__file__, encoding="utf-8").read()
    # 临时名由 mkstemp 产生
    assert "tempfile.mkstemp(" in source, "应当用 tempfile.mkstemp 造唯一临时名"

    # 扫赋值语句：任何把固定后缀拼到 path 上当临时名的写法都会在这里露出来。
    # ast 天然不返回 docstring，所以不会匹配到模块自己的说明文字。
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and "tmp" in t.id for t in targets):
            continue
        rendered = ast.unparse(node.value)
        assert ".tmp" not in rendered or "mkstemp" in rendered, (
            f"临时名不得由 path + 固定后缀拼成（发现：{rendered}）—— "
            "两进程会拿到同一 inode，fd 交错写 → 半截 JSON"
        )


def test_concurrent_writes_leave_valid_json(tmp_path):
    """真并发：N 个线程各写一份，文件必须始终是**合法 JSON**。

    这条是判据 1 的行为验证。固定名写法下两个 os.open 拿到同一 inode，交错写
    会产出 `{"a": 1}{"b": 2}` 这种非法 JSON。

    **不校验条数**：Windows 上 `os.replace` 撞上正被别处打开的文件会抛
    PermissionError（共享模式限制），所以读-改-写循环里会丢轮次——那是平台的
    文件共享语义，不是这条判据要测的东西。**"始终是合法 JSON" 才是**：
    固定名写法在 Linux/macOS 上会稳定产出非法 JSON，而 Windows 至少还会拒绝
    硬写，两边都能被这条判据捕获。
    """
    from app.core.atomic_write import write_json_atomic
    import threading

    target = str(tmp_path / "ledger.json")
    n = 20
    errors = []

    def writer(i):
        for _ in range(5):
            try:
                try:
                    with open(target, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    data = {}
                data[f"k{i}"] = i
                write_json_atomic(target, data)
            except PermissionError:
                # Windows 独占语义，跳过这一轮，不影响"绝不产出非法 JSON"这条
                continue
            except Exception as e:  # noqa: BLE001 — 这条要看到任何别的失败
                errors.append(e)
                return

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写入抛出异常：{errors[:3]}"
    with open(target, encoding="utf-8") as f:
        text = f.read()
    # 非法 JSON 会让这一步直接抛 —— 那就是"半截拼接"的直接证据
    final = json.loads(text)
    assert isinstance(final, dict) and final, "落盘结果应是非空对象"


def test_failed_write_leaves_no_temp_residue(tmp_path):
    """写失败不许留临时文件残骸。

    固定名写法失败后留下 `path + ".tmp"`，下一次写入会拿它当自己的目标——那个
    残骸可能正好是上一次写坏的。
    """
    from app.core.atomic_write import write_json_atomic

    class Unserializable:
        pass

    target = str(tmp_path / "x.json")
    with pytest.raises(TypeError):
        write_json_atomic(target, {"bad": Unserializable()})

    leftovers = [p for p in os.listdir(str(tmp_path)) if p != "x.json"]
    assert not leftovers, f"失败后留下残骸：{leftovers}"


def test_permissions_are_0600(tmp_path):
    """落盘文件权限必须是 0600——providers.json 存明文密钥、users.json 存口令摘要。

    走 mkstemp 之后权限由mkstemp 自己定（0600），所以这条锁的是"别把它改松"。
    """
    from app.core.atomic_write import write_json_atomic

    if os.name == "nt":
        pytest.skip("Windows 上 os.chmod 只改只读位，POSIX 权限位不适用")
    target = tmp_path / "secret.json"
    write_json_atomic(str(target), {"k": "v"})
    mode = os.stat(str(target)).st_mode & 0o777
    assert mode == 0o600, f"落盘权限应为 0600，实际 {oct(mode)}"


# ---------- 判据 2：数据根独占 ----------

def test_second_acquire_while_holder_alive_is_refused(tmp_path):
    """占着的时候再来一个，必须拒绝。

    负向锁：把 `classify_pid` 改成恒返回 "dead"（"永远当成陈旧锁"），这条测试红。
    """
    from app.core.data_root import DataRootBusyError, acquire_data_root_lock

    root = str(tmp_path / "data")
    path = acquire_data_root_lock(root)
    assert os.path.exists(path)

    # 本进程 pid 一定活着，所以第二次拿应当被当成真冲突
    with pytest.raises(DataRootBusyError) as exc:
        acquire_data_root_lock(root)
    msg = str(exc.value)
    assert "只允许一个服务进程" in msg, f"错误文案要说清后果：{msg}"
    assert str(os.getpid()) in msg, "错误文案要报出占用方的 pid，否则运维无从下手"


def test_stale_lock_from_dead_process_is_taken_over(tmp_path):
    """pid 已经不在的锁必须能被接管——**否则锁会永久挡住所有重启**。

    watchdog 把崩溃进程拉起时那是一个新进程、新 pid，锁里的 pid 常常是死的。
    这条比"拒绝冲突"更重要：误判冲突只是要人删个文件，误判陈旧会让服务起不来。
    """
    from app.core.data_root import acquire_data_root_lock

    root = tmp_path / "data"
    root.mkdir()
    lock = root / ".data-root.lock"
    # 锁内容是 JSON（与实现里的 payload 同一套格式，见 data_root.acquire 注释）。
    # pid 用一个不可能活着的值：**不能用 0x7FFFFFFF**——它在 Windows 上是合法的
    # pid 上限，`classify_pid` 可能判它"活着"，于是本用例测的是真冲突分支而不是陈旧锁。
    # 用 -1：POSIX 与 Windows 都把负 pid 视为非法，classify_pid 的 pid<=0 前置
    # 直接判死，这才是稳定的"死 pid"。
    lock.write_text(json.dumps({"pid": -1, "service": "old", "started": 0.0}),
                   encoding="utf-8")

    path = acquire_data_root_lock(str(root))     # 不抛异常就是接管成功
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        holder = json.loads(f.read())
    assert holder["pid"] == os.getpid(), "接管后应写入当前 pid"


def test_unknown_pid_is_never_taken_over(tmp_path, monkeypatch):
    """**判不出死活 ≠ 已死**：这种时候必须拒绝启动并且**不碰锁文件**。

    这条是 v0.33.0 事故的直接回归锁。当时的判据把"判不出"并进了"已死"，于是第二
    个进程理直气壮地删掉**活着的**服务的独占记录再自己起一份——两个进程同写一份
    数据根，正是这把锁要防的事，而防它的那段代码成了破门的人。

    负向锁：把 acquire 里的 `state == _UNKNOWN` 分支删掉（让它落到接管分支），红。
    把判据从"抛错"改成"打印警告后继续"，也红。
    """
    from app.core import data_root
    from app.core.data_root import _UNKNOWN, DataRootBusyError, acquire_data_root_lock

    root = tmp_path / "data"
    root.mkdir()
    lock = root / ".data-root.lock"
    lock.write_text(json.dumps({"pid": 424242, "service": "mystery", "started": 0.0}),
                    encoding="utf-8")
    before = lock.read_text(encoding="utf-8")

    monkeypatch.setattr(data_root, "classify_pid", lambda pid: _UNKNOWN)
    with pytest.raises(DataRootBusyError) as exc:
        acquire_data_root_lock(str(root))

    assert "无法判定" in str(exc.value), "文案要说明是判不出，不是确认冲突"
    assert lock.exists(), "判不出时绝不能删锁——删掉的就是活着的服务的独占记录"
    assert lock.read_text(encoding="utf-8") == before, "判不出时也不能改写锁内容"



def test_lock_records_holder_readable_by_operator(tmp_path):
    """锁文件要能被运维直接看懂——出问题时不用推理。"""
    from app.core.data_root import acquire_data_root_lock

    root = str(tmp_path / "data")
    path = acquire_data_root_lock(root, service="fenver-backend")
    with open(path, encoding="utf-8") as f:
        holder = json.loads(f.read())
    assert holder["pid"] == os.getpid()
    assert holder["service"] == "fenver-backend"


def test_detached_holder_is_alive_and_lock_is_respected(tmp_path):
    """**分离进程**形状下必须判活并且拒绝第二次启动——v0.33.0 生产事故的原样重现。

    为什么已有的"本进程 pid"那条测试不够：`os.kill(pid, 0)` 在 Windows 上是
    `GenerateConsoleCtrlEvent`，对自己 Popen 出来的**子进程**恰好能用，所以旧判据
    在这条测试上是绿的；而生产的形状是看门狗 / 登录自启用 `Start-Process` 起的
    **另一个控制台、另一个进程组**的进程，那种调用直接 `WinError 87` 失败，旧判据
    把它当成"进程没了"，于是第二个进程删掉活服务的锁并起来。
    判据只能靠"真的起一个分离进程"来锁住，任何在本解释器里 Popen 的写法都测不到
    这条盲区。

    只在 Windows 上跑（Linux 上没有这个平台差异，POSIX 分支另有上面的用例）。
    """
    if os.name != "nt":
        pytest.skip("Windows 平台差异用例：Linux 上 os.kill 语义本来就对")

    from app.core.data_root import (DataRootBusyError, acquire_data_root_lock,
                                    classify_pid)

    # 用 powershell 的 Start-Process 起一个真正分离、且会自己活足够久的进程。
    # 注意别用 `cmd /c timeout /t N`：timeout 在 stdin 被重定向时立刻退出
    # （"ERROR: Input redirection is not supported"），那会测到一个真死 pid。
    launcher = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "$p = Start-Process -FilePath powershell.exe -ArgumentList "
         "'-NoProfile','-Command','Start-Sleep -Seconds 120' -WindowStyle Hidden "
         "-PassThru; Write-Output $p.Id"],
        capture_output=True, text=True, timeout=120)
    pids = [ln.strip() for ln in launcher.stdout.splitlines() if ln.strip().isdigit()]
    assert pids, f"未能起出分离进程：{launcher.stdout!r} {launcher.stderr!r}"
    holder_pid = int(pids[-1])
    try:
        time.sleep(1.0)                          # 让它真的站上那个 pid

        # 独立事实源确认它活着，避免用被测判据自证
        check = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process -Id %d | Select-Object -Expand Id" % holder_pid],
            capture_output=True, text=True, timeout=60)
        assert check.stdout.strip(), "探针起的分离进程自己就没了，这条测试没有意义"

        assert classify_pid(holder_pid) == "alive", (
            "分离进程被判成非存活——这正是 v0.33.0 让两个进程同写一份数据根的判据"
        )

        root = tmp_path / "data"
        root.mkdir()
        lock = root / ".data-root.lock"
        lock.write_text(json.dumps({"pid": holder_pid, "service": "detached",
                                    "started": 0.0}), encoding="utf-8")
        before = lock.read_text(encoding="utf-8")

        with pytest.raises(DataRootBusyError) as exc:
            acquire_data_root_lock(str(root))
        assert str(holder_pid) in str(exc.value)
        assert lock.read_text(encoding="utf-8") == before, (
            "持有者活着的时候，第二次启动一个字都不许改锁"
        )
    finally:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Stop-Process -Id %d -Force -ErrorAction SilentlyContinue" % holder_pid],
            capture_output=True, text=True, timeout=60)


def test_lock_file_is_gitignored():
    """锁必须在 `.gitignore` 里：它是运行时产物，入库会让"工作区干净"这个门禁失效。

    换包与发版流程都拿 `git status` 判干净（见 `deploy/swap-backend.ps1` 与
    `.github/workflows/release-apk.yml` 的前置门禁）。锁文件落在**项目根**而不是
    `data/` 下（`data_root()` 返回项目根），所以上面那条 `data/` 挡不住它；不显式
    记一行，每次本机启动都会多出一条脏文件，久而久之门禁就被"习惯性忽略"了。

    判据走 `git check-ignore` 的真实语义而不是搜字符串：`.gitignore` 的匹配规则
    （前导 `/`、`**`、否定行）搜是搜不准的。

    负向锁：删掉那行 `/.data-root.lock` → 红。
    """
    from app.core import paths

    # 测试自己的位置反推仓库根：backend/tests/ 上两级
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    # 锁的**真实落点**：data_root() + LOCK_NAME。用运行时值而不是写死".data-root.lock"，
    # 于是将来若真把它搬进 data/，这条测试照样锁得住。
    from app.core.data_root import LOCK_NAME
    data_root_dir = paths.data_root()
    assert os.path.isabs(data_root_dir), "data_root() 应返回绝对路径"
    rel = os.path.relpath(os.path.join(data_root_dir, LOCK_NAME), repo_root)
    if rel.startswith(os.pardir):
        pytest.skip("本机把数据根指到了仓库之外，gitignore 判据不适用")

    # input 走 **bytes**：text=True 时 Python 在 Windows 上会把 "\n" 换成 "\r\n"，
    # git 于是拿 ".data-root.lock\r" 去匹配，永远匹配不上——判据会因为换行符自己变红。
    res = subprocess.run(
        ["git", "-C", repo_root, "check-ignore", "-q", "--stdin"],
        input=(rel.replace(os.sep, "/") + "\n").encode("utf-8"),
        capture_output=True, timeout=60)
    assert res.returncode == 0, (
        "数据根锁 %s 没有被 gitignore 覆盖（check-ignore 退出码 %d，stderr=%r）"
        "——加一行 /.data-root.lock，别让运行时锁污染发版门禁的工作区判据"
        % (rel, res.returncode, res.stderr.strip())
    )


def test_release_lets_next_acquire_in(tmp_path):
    """放掉之后要能立刻再拿——正常重启路径不能被自己的锁挡住。"""
    from app.core.data_root import acquire_data_root_lock, release_data_root_lock

    root = str(tmp_path / "data")
    p1 = acquire_data_root_lock(root)
    release_data_root_lock(p1)
    assert not os.path.exists(p1)
    p2 = acquire_data_root_lock(root)     # 不抛= 成功
    assert os.path.exists(p2)


def test_acquire_is_atomic_under_racing_processes(tmp_path):
    """真并发：N 个子进程同时抢一份数据根，**有且只有一个**成功。

    走真 subprocess 而不用线程，因为要验的正是 `O_CREAT | O_EXCL` 的原子性——
    "先判断存在再创建"那种写法在两个进程之间有窗口，会让双方都以为自己拿到了。
    """
    root = tmp_path / "data"
    root.mkdir()
    # 子进程**必须长驻**：进程一退出它的 pid 就死了，别的进程会把锁当陈旧的接管，
    # 于是"只有一个人拿到"这条判据在短命进程上根本不成立。生产里的服务进程是
    # 长驻的，模拟也该如此。
    #
    # 用法：每个子进程拿完锁后 sleep 到测试放行，测试逐一收集结果再统一放行。
    # 子进程要 import 的是 `app.core.*`，而 `app` 包在 backend/ 下。**不能取当前工作目录**：
    # CI 是 `python -m pytest backend/tests/`（仓库根为 CWD），此时 abspath(".") 指向仓库根，
    # 六个子进程全部 ModuleNotFoundError，一条 OK 都打不出来，断言就以 "实际 0 个" 收场——
    # 红的是这条锁自己的取径写法，不是被测的锁。判据从测试文件自身位置反推 backend 目录，
    # 于是仓库根与 backend/ 两种 CWD 下都成立。
    backend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    child = textwrap.dedent("""
        import sys, time
        sys.path.insert(0, %r)
        from app.core.data_root import acquire_data_root_lock, DataRootBusyError
        try:
            acquire_data_root_lock(%r, service="racer")
            print("OK", flush=True)
        except DataRootBusyError:
            print("BUSY", flush=True)
            time.sleep(30)          # 被拒的进程也留一会儿，别立刻退
            raise SystemExit(0)
        time.sleep(30)# 拿到锁的留到测试放行
    """) % (backend_root, str(root))
    env = dict(os.environ)
    env["PYTHONPATH"] = backend_root
    procs = []
    try:
        for _ in range(6):
            procs.append(subprocess.Popen(
                [sys.executable, "-c", child],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env))
        # 给它们时间互相发现：抢锁发生在启动后的最初几十毫秒里
        oks = 0
        for p in procs:
            line = p.stdout.readline().strip()
            if line.startswith("OK"):
                oks += 1
    finally:
        for p in procs:
            p.kill()
            p.wait(timeout=10)
    assert oks == 1, (
        f"有且只有一个进程应拿到锁，实际 {oks} 个——这是 fail-open："
        "两个进程会各自持有内存快照再整体覆盖写盘"
    )


# ---------- 与启动流程的接线 ----------

def test_lifespan_takes_the_lock_before_anything_else():
    """锁必须在 lifespan 的**最前面**取，早于任何读/写数据的步骤。

    判据用源码位置而不是运行时行为：运行时要真的起两个 app 实例，成本高且脆弱。
    看的是 acquire 在源码里排在日志摘要、启动探测、调度器之前。
    """
    import app.main as main

    source = open(main.__file__, encoding="utf-8").read()
    idx_lock = source.index("acquire_data_root_lock(data_root())")
    for later in ("log_data_locations()", "log_startup_summary()",
                  "start_probe()", "start_background_scheduler()"):
        if later not in source:
            continue
        assert idx_lock < source.index(later), (
            f"数据根锁必须在 {later} 之前获取——那一步就会读/写存储"
        )


def test_lifespan_releases_the_lock():
    """关闭时放锁。不放也能启动（判据会当陈旧锁接管），但那把一条干净的判据变脏。"""
    import app.main as main

    source = open(main.__file__, encoding="utf-8").read()
    assert "release_data_root_lock" in source, (
        "关闭路径应释放数据根锁"
    )
