"""依赖声明完整性：backend/app 导入的每个第三方包都必须在 requirements.txt 里。

CI 是按 requirements.txt 从零装包的，所以少声明一条就在收集期整轮报错。
这个洞本可以一直藏着：开发机上手动装过的包会让本地全绿，于是"本地通过"从来没
证明过依赖是完整的——ddgs / docker / pymupdf 三条正是这样漏了很久的。
"""
import ast
import pathlib
import re
import subprocess
import sys

BACKEND_APP = pathlib.Path(__file__).resolve().parents[1] / "app"
REQUIREMENTS = pathlib.Path(__file__).resolve().parents[1] / "requirements.txt"
LOCK = pathlib.Path(__file__).resolve().parents[1] / "requirements.lock"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
LOCK_CHECKER = REPO_ROOT / "tools" / "check_requirements_lock.py"

# 导入名与发行包名不一致的情况
IMPORT_TO_DIST = {"fitz": "pymupdf", "yaml": "pyyaml", "PIL": "pillow", "cv2": "opencv-python",
                  "dotenv": "python-dotenv"}

# 可选依赖：缺失时代码自己捕获 ImportError 并降级，所以刻意不进必装清单
# （sentence-transformers 会把几百 MB 模型拖进冻结版，而云端嵌入是默认路径）
OPTIONAL_DEPS = {
    "sentence_transformers": "本地嵌入的退路；_init_local_embed 捕获 ImportError 后降级",
    # 共享限流账（v0.34.0 D1）是"多 worker 之前才需要"的那条路，本版不落生产，所以不把
    # redis 拖进必装清单和冻结包。方向要说清：捕获 ImportError **不是降级**，
    # ledger_backend.select_backend 把它转成 RuntimeError——宁可启动失败，
    # 也不要"每个 worker 一本账、把每窗口阈值悄悄乘以进程数"。
    "redis": "共享限流账的可选后端；select_backend 捕获 ImportError 后抛 RuntimeError，不退回内存",
}


def _top_level_imports():
    """收集 app 包内所有顶层导入的第三方模块名（含函数内的延迟导入）。"""
    std = set(sys.stdlib_module_names)
    found = set()
    for path in BACKEND_APP.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:  # 语法错的文件不该悄悄跳过
            raise AssertionError(f"{path} 解析失败，依赖检查无法覆盖它：{e}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    local = {"app", "tests"}
    return {m for m in found if m and m not in std and m not in local and not m.startswith("_")}


def _declared_names():
    """requirements.txt 里声明的发行包名（去掉版本约束与行内注释）。"""
    names = set()
    # utf-8-sig 不是讲究：这份清单文件头带 BOM，用 "utf-8" 读会把第一行读成
    # "﻿annotated-doc"，strip() 去不掉 U+FEFF——于是清单的第一项对这条锁永久隐形。
    for line in REQUIREMENTS.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        names.add(re.split(r"[<>=!~;\[ ]", line, maxsplit=1)[0].strip().lower())
    return names


def test_every_third_party_import_is_declared_or_explicitly_optional():
    declared = _declared_names()
    undeclared = []
    for module in sorted(_top_level_imports()):
        if module in OPTIONAL_DEPS:
            continue
        dist = IMPORT_TO_DIST.get(module, module).lower()
        if dist not in declared:
            undeclared.append(module)
    assert not undeclared, (
        f"以下包被 backend/app 导入却没写进 requirements.txt：{undeclared}\n"
        "CI 会因此整轮收集失败。若它确实是可选的，就加进 OPTIONAL_DEPS 并写清降级位置。"
    )


def test_optional_deps_are_actually_optional_in_code():
    """可选清单不能变成垃圾抽屉：条目必须真的被 try/except 兜着。"""
    text = "\n".join(p.read_text(encoding="utf-8") for p in BACKEND_APP.rglob("*.py"))
    for module, rationale in OPTIONAL_DEPS.items():
        assert module in text, f"{module} 已不在代码里被导入，应从 OPTIONAL_DEPS 删除"
        assert rationale.strip(), f"{module} 的可选理由不能是空的"


def test_there_is_exactly_one_dependency_manifest():
    """依赖清单只许有一份，而且 CI / Docker / 锁三边必须是同一套版本。

    仓库根原先躺着一份 118 行的 pip freeze 转储（UTF-16、零注释），`Dockerfile`
    COPY 的正是它，而 CI 装的是 `backend/requirements.txt` —— 两份的 numpy、
    onnxruntime、fastapi 版本互不相同。症状不是报错，是"容器里跑出来的行为和
    CI 绿的那套不是一回事"，而且没人会去查：两边都装得上，都起得来。
    这属于本仓反复栽的那种第二个事实来源，只不过这次藏在依赖声明里。

    #146 之后多了一把锁：CI 不再现场解析清单，而是装 `backend/requirements.lock`
    （跨平台 + 逐包哈希）；Docker 仍装清单，但清单里的 7 条宽区间已经钉成精确版本。
    两边装的是不是同一套，由 `tools/check_requirements_lock.py` 在 CI 里判（本文件
    下面两条用例守着它有牙）。所以这里仍要求根目录不长出第二份清单，并要求 CI 的
    run 正文同时点得到清单与锁——只装锁而不读清单，清单改了 CI 不会红。
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    stray = [p.name for p in root.glob("requirements*.txt")]
    assert not stray, f"仓库根又长出一份依赖清单：{stray}（唯一的一份在 backend/ 下）"

    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY backend/requirements.txt" in dockerfile, \
        "Docker 不再从 backend/ 取清单：它会在根目录找那份不存在的文件"
    assert "pip install --no-cache-dir -r /app/requirements.txt" in dockerfile, \
        "Docker 装依赖那一步的清单路径被改动了，和上面 COPY 的目标对不上就是空装"

    from tests.test_ci_history_depth import workflow_run_bodies

    ci = workflow_run_bodies(root / ".github" / "workflows" / "tests.yml",
                             where="tests.yml 的 run 正文（#94 W 片）")
    assert "backend/requirements.txt" in ci, \
        "CI 的依赖锁检查不再读这份清单，这条锁就管不到它了"
    assert "backend/requirements.lock" in ci, \
        "CI 不再从锁装包：清单与锁分家时没有任何一步会红，锁就成了摆设"
    assert "--require-hashes" in ci, \
        "CI 从锁装包却没开 --require-hashes：锁里的哈希只是注释，索引上的产物换了也没人知道"
    # 解释器也要一致：容器此前用 3.11，而 CI/venv/打包用的都是 3.12
    assert re.search(r"FROM python:3\.12", dockerfile), \
        "容器的 Python 版本又和 CI/打包那份错开了（chromadb 对上限敏感）"


def test_the_manifest_is_parsed_from_its_first_line():
    """正向对照：清单第一项必须被认出来。

    上一版这条锁用 encoding="utf-8" 读一份带 BOM 的文件，于是第一行变成
    "\ufeffannotated-doc"——它既匹配不上任何 import，也永远不会让谁变红，
    整份清单的第一项就这样对检查隐形了。这类"检不到东西的检查看起来全绿"
    是本仓最贵的失败形状，所以这里不判"读法对不对"，直接判结果：第一项在不在。
    """
    names = _declared_names()
    assert names, "清单解析为空，下面两条断言都是空的"
    assert not [n for n in names if "\ufeff" in n], f"有包名带 BOM 残留：{sorted(n for n in names if chr(0xFEFF) in n)}"
    first_line = next(l.split("#", 1)[0].strip() for l in
                      REQUIREMENTS.read_text(encoding="utf-8-sig").splitlines()
                      if l.strip() and not l.lstrip().startswith("#"))
    first_name = re.split(r"[<>=!~;\[ ]", first_line, maxsplit=1)[0].strip().lower()
    assert first_name in names, f"清单第一行 {first_line!r} 没被解析出包名，那第一项就检不到"


def test_installed_versions_satisfy_the_declared_ranges():
    """跑这条测试的解释器，装的包必须落在清单钉的区间里。

    这条锁的由来是 2026-09-21 的一次 CI 红：conftest 写了
    `TestClient(app, client=("127.0.0.1", 54321))`，而 `client=` 这个关键字是
    starlette 0.46 才有的；清单钉的是 `starlette>=0.37.2,<0.41.0`，CI 从零按清单装，
    于是凡是用到 client 夹具的用例全部 TypeError。本地那次"全绿"跑的是另一个解释器
    （装着 starlette 1.0.0），同一行代码两边结论相反——而 CI 那侧才是真的。
    区间不匹配只说明一件事：这个解释器不是 CI 那一份，它给出的绿不作数。
    """
    from importlib import metadata

    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.specifiers import InvalidSpecifier

    drift = []
    checked = 0
    for line in REQUIREMENTS.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-") or "@" in line:
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue                     # 交不上打包器的行由上面那几条锁管
        if not req.specifier:
            continue
        try:
            installed = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            continue                     # 没装就不判：可选依赖与平台专属包都在这类
        checked += 1
        try:
            if not req.specifier.contains(installed, prereleases=True):
                drift.append(f"{req.name}=={installed}（清单要 {req.specifier}）")
        except InvalidSpecifier:
            continue
    assert checked >= 20, f"区间检查只落到 {checked} 个包上，这条锁多半在空转"
    assert not drift, (
        "这些包装的版本不在清单区间里，本机结论与 CI 不是一回事：" + "; ".join(drift)
        + "\n用 `venv\\Scripts\\python.exe -m pytest backend/tests/ -q` 跑（那份与 CI 对齐），"
          "或者 `pip install -r backend/requirements.txt` 把当前解释器拉回来。")


def _run_lock_checker(manifest, lock):
    """跑 CI 里那同一个检查器（不在这里另抄一份近似逻辑）。"""
    return subprocess.run([sys.executable, str(LOCK_CHECKER), str(manifest), str(lock)],
                          cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)


def test_the_lock_matches_the_manifest_and_is_fully_hashed():
    """CI 装的那把锁必须与本清单同版本，且每条都带形状合法的 sha256 哈希。

    锁是依赖的第三个事实来源：#146 之前只有清单和 Dockerfile，两边各装一套的教训
    写在上面那条用例里。现在 CI 只装锁，所以"清单改了锁没重编"必须红——判据就是
    `tools/check_requirements_lock.py`，CI 里跑的是同一份实现，这里跑它而不是重写一遍。
    """
    assert LOCK.is_file(), f"依赖锁不存在：{LOCK}（CI 已改成从它装包）"
    proc = _run_lock_checker(REQUIREMENTS, LOCK)
    assert proc.returncode == 0, (
        "依赖锁与清单分家了（CI 会装上一套清单里没写的环境）：\n"
        + (proc.stderr or proc.stdout).strip()
        + "\n改完清单请重编锁，命令见 backend/requirements.txt 顶部的注释。")


def test_the_lock_check_goes_red_when_the_manifest_or_lock_drifts(tmp_path):
    """两枚反例：清单改了版本没重编锁、锁里丢了哈希——检查器都必须红。

    不钉包名/版本号（它们每升一次就漂）：从真物里取第一条精确钉版，把 patch 号 +1
    当成"清单漂了"的合成输入；锁那侧则把第一条的哈希行整段摘掉。两枚都只改副本，
    真物不动——红的必须是检查器，不是被改坏的真文件。
    """
    manifest_text = REQUIREMENTS.read_text(encoding="utf-8-sig")
    pin = re.search(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9]+)\.([0-9]+)\.([0-9]+)$",
                    manifest_text, re.MULTILINE)
    assert pin, "清单里找不到可改的 x.y.z 钉版，反例造不出来"
    bumped = f"{pin.group(1)}=={pin.group(2)}.{pin.group(3)}.{int(pin.group(4)) + 1}"
    drifted_manifest = tmp_path / "requirements.txt"
    drifted_manifest.write_text(manifest_text[:pin.start()] + bumped + manifest_text[pin.end():],
                                encoding="utf-8-sig")
    proc = _run_lock_checker(drifted_manifest, LOCK)
    assert proc.returncode == 1 and "版本分家" in proc.stderr, (
        "清单改了一个版本、锁没重编，检查器却放行了——CI 会装旧的一套而绿灯。实得 rc="
        + str(proc.returncode) + "，stderr=" + proc.stderr[:300])

    lock_lines = LOCK.read_text(encoding="utf-8-sig").splitlines()
    stripped, removed, i = [], False, 0
    while i < len(lock_lines):
        line = lock_lines[i]
        if not removed and "==" in line and line.rstrip().endswith("\\"):
            stripped.append(line.rstrip()[:-1].rstrip())   # 第一条包声明，去掉续行反斜杠
            i += 1
            while i < len(lock_lines) and "--hash=" in lock_lines[i]:
                i += 1
            removed = True
            continue
        stripped.append(line)
        i += 1
    assert removed, "锁里找不到哈希行，反例造不出来"
    hashless_lock = tmp_path / "requirements.lock"
    hashless_lock.write_text("\n".join(stripped) + "\n", encoding="utf-8-sig")
    proc = _run_lock_checker(REQUIREMENTS, hashless_lock)
    assert proc.returncode == 1 and "哈希" in proc.stderr, (
        "锁里丢了一条哈希，检查器却放行——`pip install --require-hashes` 在那一条上"
        "等于没开。实得 rc=" + str(proc.returncode) + "，stderr=" + proc.stderr[:300])
