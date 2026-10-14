r"""机器真要读的那些路径，不许指着一个不存在的文件。

写这条的直接原因是 2026-10-08 删壳之后：仓库里还剩五处指着 `test_android_shell.py` 的
注释，人读着像是"那儿有一份锁"，实际上那份文件已经不在了。这一类断链【本条不全覆盖】
——注释与散文（含 Python docstring）里的历史叙述（"从前住在 X"）是刻意留下的证据，
把它判红等于逼着人删掉来路说明。本条只管**机器或复制粘贴真要读的位置**：

1. `.github/workflows/*.yml` 里 `run:` 的脚本正文与 `with:` 的参数值（Actions 真执行）；
2. `deploy/*.ps1` 与 `tools/*.py` 里**剥掉注释/docstring 之后的字符串字面量**
   （注释里说"先跑 deploy\swap-package.ps1"、docstring 里说"和 app/core/providers.py
   同一口径"都不算一次引用）；
3. 同一批脚本里**由仓库根变量拼出来的路径**：PowerShell 的 `Join-Path $ProjectRoot 'tools\cloudflared.exe'`
   与 Python 的 `os.path.join(repo_root, "android-native", "app", "build.gradle")`。
   这一类是主战场——脚本真去打开的东西多数长这样，光看 token 形状抓不到（判基准怎么认，
   见下面 `_ps_bases` / `_py_joined_pointers` 的注释）。

判红条件：抽出来的路径相对仓库根不存在。立这条的理由（AGENTS.md 第 3 条）：
"指向不存在的文件也是一种引用"，并给了一个真实判例——CI 的报错文案里指着
`docs/APK正式签名落地指引.md`，而那份指引住在被 .gitignore 排除的 `docs-internal/`：
人在 CI 上红着的时候按那句话去找文件，永远找不到。`docs-internal/` 因此**不在豁免名单里**，
指过去就该红（本机才有的东西不能出现在机器读的引用里）。

形状上的两处取舍，都是为了少误报而不是少判红：
- 含 `://` 的词整词丢掉，前面紧跟 `/` 的匹配也丢掉——那是 URL 或 HTTP 路由，不是文件
  （`could not fetch /app/app.js` 里的 `/app/app.js` 是路由）。逐词判是刻意的：
  `python tools/pre_tag_gate.py --health-url https://ai.fenever.xyz/health` 这一行里
  既有真引用也有 URL，整行丢就把发版闸门也一起放过了。
- token 类只收"至少一个分隔符 + 白名单扩展名"的形状：裸文件名（`version.txt`、
  `run_backend.exe`）在脚本里常常是相对包内目录或运行期变量拼出来的，按仓库根判会把
  对的判成红的（这类改由第 3 条的拼接规则抓，基准是能证明的根）。
"""

import ast
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
OPS = sorted(list((REPO_ROOT / "deploy").glob("*.ps1")) + list((REPO_ROOT / "tools").glob("*.py")))

# 路径形状的token：至少一个分隔符 + 白名单扩展名（取舍见上面模块 docstring）。
# 扩展名右边界 `(?![A-Za-z0-9])` 是 2026-10-15 合并自托管线时补的尺子洞：`js` 排在
# `json` 前面而 finditer 允许半截匹配，`backend/app/web/site/changelog.json` 会被
# 拦腰切成 `…changelog.js`——一个从未存在过的形状，把真在的指针判成红引用。
# 收紧右边界只会让 token 更接近"整词扩展名"的本意，不会放过任何真断链。
PATH_TOKEN = re.compile(
    r"[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+\.(?:py|md|ya?ml|ps1|gradle|kt|java|js|json|txt|spec|sh|html|css)(?![A-Za-z0-9])")

# 带这些字符的 token 不是"这里有个待打开的文件"：模板、变量、通配。
PLACEHOLDER = ("$", "{", "}", "<", ">", "*", "?", "~", "%")

# 构建产物与运行时资产：它们在两次构建/两次部署之间本就可能不在，按"引用是否存在"判
# 会把每条正常文案判红。dist/ 是现网包、dist_new/ 是暂存包、build/ 是 PyInstaller 工作目录、
# _internal/ 是 onedir 包**内部**的目录：swap-package.ps1 用相对 StageDir 的路径读
# `_internal\version.txt` 与 `_internal\app\web\static\app.js`，它们在仓库根下永远不存在。
BUILD_OR_RUNTIME_ROOTS = ("dist/", "dist_new/", "dist_stage", "build/", "data/", "chroma_db/",
                          "chroma_data/", "venv/", ".venv/", "node_modules/", "backend/static_dist/",
                          "_internal/")


def _tokens(text: str) -> list:
    r"""抽路径 token：按空白与引号切词，URL 整词丢掉，前面紧跟 `/` 的匹配也丢掉。

    - 丢 URL 词（含 `://`）是刻意的：`python tools/pre_tag_gate.py --health-url
      https://ai.fenever.xyz/health` 这一行里既有真引用也有 URL，逐词判才不至于把闸门
      那一条连同 URL 一起放过。
    - 丢"前一个字符是 `/`"的匹配，因为那是**路由**不是文件：swap-package.ps1 的报错文案
      `could not fetch /app/app.js` 里的 `/app/app.js` 是 HTTP 路径。本仓机器读的文件引用
      只有两种形状——相对仓库根的（`tools/pre_tag_gate.py`）和 Windows 绝对路径
      （`C:\...`，它的 token 前面是 `:` 或 `\`，不受这条影响）。
    """
    out = []
    for word in re.split(r"[\'\"\s]+", text):
        if "://" in word:
            continue
        for m in PATH_TOKEN.finditer(word):
            if m.start() > 0 and word[m.start() - 1] == "/":
                continue
            token = m.group(0)
            if not any(ch in token for ch in PLACEHOLDER):
                out.append(token)
    return out


def _from_workflows() -> list:
    """返回 [(出处, 路径token)]，只取 Actions 真读的正文，不取注释。"""
    out = []
    for wf in WORKFLOWS:
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        for job in (doc.get("jobs") or {}).values():
            for i, step in enumerate(job.get("steps") or []):
                run = step.get("run")
                if isinstance(run, str):
                    # `run:` 正文里的 YAML 注释行（`# ...`）不算：那是解释，不是命令
                    body = "\n".join(ln for ln in run.splitlines()
                                     if not ln.lstrip().startswith("#"))
                    out += [(f"{wf.name}#step{i}:{step.get('name')}", t) for t in _tokens(body)]
                with_ = step.get("with")
                if isinstance(with_, dict):
                    for value in with_.values():
                        if isinstance(value, str):
                            out += [(f"{wf.name}#step{i}:with", t) for t in _tokens(value)]
    return out


def _strip_ps_comments(text: str) -> str:
    r"""剥掉 PowerShell 的 `#` 行注释与 `<# ... #>` 块注释；字符串里的 `#` 不碰。

    简化的代价写在这里：脚本没有跨行字符串续行的复杂用法，取"行首是 #"这一种形状足够，
    而块注释必须整段剥掉——swap-package.ps1 的头 <# ... #> 里就写着它自己的路径。
    """
    text = re.sub(r"<#.*?#>", " ", text, flags=re.S)
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines)


def _py_code_strings(path: Path) -> list:
    """用 AST 取这个 .py 里**不是 docstring** 的字符串常量。

    docstring 是散文，按本条口径不算机器读引用（`secret_scan.py` 的"和
    app/core/providers.py::mask_key 同一口径"就是这种）；`#` 注释 AST 天然不含。
    模块/类/函数体首位的 Expr 常量逐个跳过，其余字符串都算——包括报错文案里的路径：
    运维照着红字去找文件时，那句文案就是机器（人复制粘贴）真要读的位置。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


def _literals(path: Path) -> list:
    """取"机器真要读的字符串"清单，不要整篇。"""
    if path.suffix == ".py":
        return _py_code_strings(path)
    body = _strip_ps_comments(path.read_text(encoding="utf-8"))
    return [m[0] or m[1] for m in re.findall(r"'([^']*)'|\"([^\"]*)\"", body)]


def _pointer_pairs() -> list:
    pairs = _from_workflows()
    for src in OPS:
        for literal in _literals(src):
            for token in _tokens(literal):
                pairs.append((src.relative_to(REPO_ROOT).as_posix(), token))
        if src.suffix == ".ps1":
            pairs += _ps_joined_pointers(src)
        else:
            pairs += _py_joined_pointers(src)
    return pairs


def _kept(pairs: list) -> list:
    """去掉构建/运行时产物，剩下的就是"声称这里有个文件"的引用（按出处+路径去重）。"""
    seen = set()
    kept = []
    for origin, token in pairs:
        clean = token.replace("\\", "/")
        if clean.startswith(BUILD_OR_RUNTIME_ROOTS):
            continue
        key = (origin, clean)
        if key in seen:
            continue
        seen.add(key)
        kept.append(key)
    return kept


def _corpus() -> list:
    return _kept(_pointer_pairs())


# ---------------------------------------------------------------- 变量根拼出来的路径
# 这一类才是主战场：脚本里真去打开的东西常常写成 `$ProjectRoot` / `repo_root` 加一段字面量，
# 单独的 token 形状抓不到它们。而现网的守护进程就靠这种拼接找到隧道程序
# （watchdog.ps1: `Join-Path $ProjectRoot 'tools\cloudflared.exe'`）——那份 exe 不在的时候，
# 红的是夜里，不是 CI。
#
# 基准不按变量名猜，按**赋值行**证：只有能从 `$PSScriptRoot` 算出来的根才认
# （`Split-Path -Parent $PSScriptRoot` = 仓库根，`$PSScriptRoot` 自己 = 脚本所在目录）。
# `$StageDir`、`$Dir`、`$DataDir` 这类运行期才知道的根一律不收：它们指向包内或产物目录，
# 按仓库根判就是误报。
_PS_ROOT_ASSIGN = re.compile(r"\$(\w+)\s*=\s*\(?Split-Path\b([^\r\n]*)", re.I)
_PS_JOIN = re.compile(r"Join-Path\s+\$\{?(\w+)\}?\s+['\"]([^'\"]+)['\"]", re.I)


def _ps_bases(path: Path, text: str) -> dict:
    bases = {"PSScriptRoot": path.parent}
    for m in _PS_ROOT_ASSIGN.finditer(text):
        rest = m.group(2)
        if "PSScriptRoot" not in rest:
            continue
        bases[m.group(1)] = path.parent.parent if "-Parent" in rest else path.parent
    return bases


def _rel_to_root(target: Path) -> str:
    """能相对仓库根表示才收；表示不了（跑到仓库外）就交给别的锁管。"""
    try:
        return target.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return ""


def _ps_joined_pointers(path: Path) -> list:
    text = _strip_ps_comments(path.read_text(encoding="utf-8"))
    bases = _ps_bases(path, text)
    origin = path.relative_to(REPO_ROOT).as_posix()
    out = []
    for m in _PS_JOIN.finditer(text):
        base = bases.get(m.group(1))
        if base is None:
            continue
        rel = _rel_to_root(base / m.group(2).replace("\\", "/"))
        if rel:
            out.append((origin, rel))
    return out


def _py_joined_pointers(path: Path) -> list:
    """`os.path.join(<名字里带 root 的量>, "a", "b")` → 相对仓库根的 a/b。

    只认第一段是**变量名**且名字里带 root 的调用：`data_root()`、`os.path.dirname(...)`
    这种运行期根一律不收（它们指向 data/ 或包内，按仓库根判是误报）。拼出来的一段里
    带 `%`/`{`/`$` 的（如 `"%s.md" % tag`）丢掉那一段，保留前面确实存在的目录段。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    origin = path.relative_to(REPO_ROOT).as_posix()
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "join" or "path" not in ast.dump(node.func.value).lower():
            continue
        if not node.args or not isinstance(node.args[0], ast.Name) \
                or "root" not in node.args[0].id.lower():
            continue
        pieces = [a.value for a in node.args[1:]
                  if isinstance(a, ast.Constant) and isinstance(a.value, str)
                  and not any(ch in a.value for ch in PLACEHOLDER)]
        if not pieces:
            continue
        rel = _rel_to_root(REPO_ROOT / "/".join(pieces))
        if rel:
            out.append((origin, rel))
    return out


def test_the_pointer_scanner_reads_enough_real_pointers():
    """正对照：一条独立、不 parametrize 的锁——空集合在 pytest 里是 skip，不是 fail。

    本仓已经为这个形状付过钱（test_request_size_caps 那条 glob 锁的空集静默）。这里钉
    "扫到了足够多"且"两个已知存在的锚点确实在名单里"，否则正则或读法一坏，"没有断链"和
    "什么都没扫到"长得一模一样。

    门限 8 故意低于当前实测条数（条数在报错文本里，别信注释）：余量留给"以后有人把某条
    引用改成变量"，不留给"扫描坏掉"。低于门限说明**扫描本身坏了**（正则、剥注释、AST 读法、
    或 workflows/deploy/tools 的目录形状变了），这时全绿没有任何意义。
    """
    kept = _corpus()
    assert len(kept) >= 8, f"只扫到 {len(kept)} 处机器读的引用，这条锁多半在空转：{kept}"
    tokens = {t for _, t in kept}
    assert "tools/pre_tag_gate.py" in tokens, \
        "名单里没有 tools/pre_tag_gate.py：发版闸门在机器读的引用里都不出现了，多半是 workflow 读法坏了"
    assert "android-native/app/build.gradle" in tokens, \
        "名单里没有 android-native/app/build.gradle：CI 与 pre_tag_gate 核对 versionName 的位置" \
        "扫不到了，多半是 token 或 AST 读法坏了"
    assert "tools/cloudflared.exe" in tokens, \
        "名单里没有 tools/cloudflared.exe：watchdog 的 Join-Path 那一类不再被扫了——" \
        "这一类正是本条的主战场，掉了就说明 _ps_bases/_ps_joined_pointers 坏了"


@pytest.mark.parametrize("origin,token", _corpus())
def test_every_machine_read_path_exists(origin, token):
    """CI 正文与脚本字面量里出现的每个路径，仓库里必须真有它。"""
    target = REPO_ROOT / token
    assert target.exists(), (
        f"{origin} 里引用了 {token}，而仓库根下没有这个东西——"
        "机器按这句话去找会永远找不到；改指向，别删内容")
