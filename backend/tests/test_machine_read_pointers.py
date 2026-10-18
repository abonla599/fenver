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

判红条件：抽出来的路径相对仓库根**不存在，而且 .gitignore 没说它是被有意排除的**。
立这条的理由（AGENTS.md 第 3 条）："指向不存在的文件也是一种引用"，并给了一个真实判例
——CI 的报错文案里指着 `docs/APK正式签名落地指引.md`，而那份指引住在被 .gitignore 排除的
`docs-internal/`：人在 CI 上红着的时候按那句话去找文件，永远找不到。

"运行期资产"与"真断链"这两类怎么分？**只问 git**（`_is_deliberately_excluded`），不再手挑
一张前缀表。原来那张 `BUILD_OR_RUNTIME_ROOTS` 是第二份真相：它写的是 `"data/"`（带斜杠），
而 watchdog.ps1 从 `Join-Path $ProjectRoot 'data'` 抽出来的 token 是 `data`（不带斜杠），
漏过过滤 ⇒ 任何干净检出（含 CI，因为这两个东西都是 gitignored 的运行期资产）必红两条。
现在判据直接落在 `.gitignore` 这一份唯一来源上：它说"这里的东西本来就不该在检出里"，
那就不是断链；它没这么说而文件又不在，那就是断链。

一处口径上的变化要说在前面：以 .gitignore 为唯一判据之后，**指向 `docs-internal/`
这类"本机私有、被有意不入库的内容"也不再红**（旧表把它算作断链，是拿一张手挑的名单在
替"这段文案该不该这么写"下判决）。那个判例真正要防的是"人在 CI 上红着的时候按那句话
去找文件、永远找不到，而没人告诉他那儿本来就找不到"——现在那一层由文案自己承担：
release-apk.yml 的对应那句写的是"参考**本机的** docs-internal/APK正式签名落地指引.md
（这份指引按仓库约定不入公开库）"，指向与说明同时在场。要不要指过去是文案判断，
不是这张表能替人做的决定；把它写成锁，只会得到第二份需要有人维护的真相。

形状上的三处取舍，都是为了少误报而不是少判红：
- 含 `://` 的词整词丢掉，前面紧跟 `/` 的匹配也丢掉——那是 URL 或 HTTP 路由，不是文件
  （`could not fetch /app/app.js` 里的 `/app/app.js` 是路由）。逐词判是刻意的：
  `python tools/pre_tag_gate.py --health-url https://ai.fenever.xyz/health` 这一行里
  既有真引用也有 URL，整行丢就把发版闸门也一起放过了。
- token 类只收"至少一个分隔符 + 白名单扩展名"的形状：裸文件名（`version.txt`、
  `run_backend.exe`）在脚本里常常是相对包内目录或运行期变量拼出来的，按仓库根判会把
  对的判成红的（这类改由第 3 条的拼接规则抓，基准是能证明的根）。
- `Join-Path $某个变量 'a\b.ext'` 里那段字面量**不按仓库根判**（见 `_literals` 里那步
  抹除）：基准证明不了的拼接交给 `_ps_joined_pointers`，那里只认能从 `$PSScriptRoot`
  算出来的根。以前这一步漏了，swap-package.ps1 里 `Join-Path $StageDir '_internal\…'`
  那两条相对**包内目录**的路径就被当成仓库根下的引用扫到了——它俩恰好都在旧表里，
  于是这个形状一直没红过，而表一撤它俩就会变成两条新的假红。
"""

import ast
import re
import subprocess
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

# PowerShell 里 `Join-Path <根> '<字面量>'` 的形状：字面量是相对那个根的，不是相对仓库根。
# 名字带 WITH_ARG 是有意的——下面 `_ps_joined_pointers` 那条 `_PS_JOIN` 要取的是**两组**
# （根变量名 + 字面量），这里这条只要"整段抹掉"，两者共用一个名字会被后定义的那条覆盖。
_PS_JOIN_WITH_ARG = re.compile(r"Join-Path\s+\$\{?\w+\}?\s+['\"][^'\"]+['\"]", re.I)


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
    """取"机器真要读的字符串"清单，不要整篇。

    PowerShell 那一支先把 `Join-Path $某根 '<字面量>'` 整段抹掉再取字面量：那段字面量
    相对的是**那个根**，不是仓库根，按仓库根去判存在性就是把 `$StageDir\\_internal\\…`
    这种包内路径当成仓库断链（旧表里那条 `_internal/` 就是在给这个形状打补丁）。
    根能证明的拼接由 `_ps_joined_pointers` 单独收，那里只认从 `$PSScriptRoot` 算得出的根。
    """
    if path.suffix == ".py":
        return _py_code_strings(path)
    body = _strip_ps_comments(path.read_text(encoding="utf-8"))
    body = _PS_JOIN_WITH_ARG.sub(" ", body)
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


def _dedup(pairs: list) -> list:
    """按出处+路径去重。这里**不再**过滤运行期资产：豁免的判断挪到 `.gitignore` 那
    一份唯一来源上（见 `_is_deliberately_excluded`），这张表一旦手写就会和现实脱节——
    它上一版的 `"data/"` 与 watchdog 抽出来的 token `data` 就是差一个斜杠而红了两条。"""
    seen, kept = set(), []
    for origin, token in pairs:
        clean = token.replace("\\", "/")
        key = (origin, clean)
        if key in seen:
            continue
        seen.add(key)
        kept.append(key)
    return kept


def _corpus() -> list:
    return _dedup(_pointer_pairs())


# ------------------------------------------------------------------ 什么算"被有意排除"
# 只问 git，不在这份文件里再抄一张"哪些目录是运行期资产"的表：那张表就是第二份真相，
# 它会比 .gitignore 先腐烂（历史上正是它少写一个斜杠，让每条干净检出都必红两条）。
def _git_check_ignore(probe: str):
    """git check-ignore 对一条路径的回答：0=被排除，1=没被排除，其它=它没答上来（返回 None）。"""
    try:
        res = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--", probe],
                             capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):      # 没有 git / 超时 / 不是仓库
        return None
    if res.returncode == 0:
        return True
    if res.returncode == 1:
        return False
    return None


# 拿一个不可能成为真文件的名字去探"这个位置**当作目录**时排不排"。
# 为什么不是"给路径补一个尾斜杠再问一次"那种直觉写法：本仓 `.gitignore` 第 175 行是
# 一个**裸 CR**（CRLF 文件里的空行），git 把它读成一枚空模式，于是
# `git check-ignore -- '任何路径/'` 一律回"被排除"。实测：
# `deploy/这一版根本不存在.ps1/` → rc=0，报的是 `.gitignore:175:`。
# 那条写法会把"豁免"变成恒真——正是本修法要躲的那个偷懒方向，而且它绿得毫无痕迹。
# 换成哨兵子路径就没有这个形状：`data/__probe__` 被 `data/` 命中，而
# `不存在的东西/__probe__` 谁也不命中（哨兵名字里带 `__probe__`，不与任何真扩展名撞车）。
_SENTINEL = "__gitignore_probe__"


def _is_deliberately_excluded(rel: str):
    """这个位置是 `.gitignore` 有意排除的东西吗？True / False；git 答不了就是 None。

    True  ⇒ 检出里没有它是**设计如此**（现网包、暂存包、数据根、venv、构建戳、
            本机才有的隧道 exe……），按"引用是否存在"判它就是把正常文案判红；
    False ⇒ .gitignore 没这么说而文件又不在 ⇒ 真断链，机器照这句话去找会永远找不到，该红；
    None  ⇒ 判不了。调用处一律按"不许绿"处理：一条静默放弃判断的锁比一条红的锁更糟，
            它会让"没有断链"和"什么都没判"长得一模一样（本仓为这个形状付过钱，
            见 test_request_size_caps 那条 glob 锁的空集静默）。

    问两次，因为指针可能是目录也可能是文件：`git check-ignore data` 回"没排除"
    （`data/` 那条模式要目录身份，而这个检出里根本没有 data/ 让 git 判断），
    而 watchdog 从 `Join-Path $ProjectRoot 'data'` 抽出来的 token 正是这个不带斜杠的
    `data`——本条原来那张手挑表漏的也正是这一个斜杠，于是每条干净检出必红两条。
    """
    trimmed = rel.rstrip("/")
    for probe in (trimmed, f"{trimmed}/{_SENTINEL}"):
        verdict = _git_check_ignore(probe)
        if verdict is None:
            return None
        if verdict:
            return True
    return False


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
    # ---- 这枚锚点自 #90b 起换了对象：**这是削锁**，改前/改后/补偿都写在这里，等审查员判 ----
    # 改前钉的是 `"tools/pre_tag_gate.py" in tokens`。它在整套名单里唯一的机器读出处是
    # `release-apk.yml` 那一步 `python3 tools/pre_tag_gate.py --expect "$GITHUB_REF_NAME"
    # --no-versionname-check` 的 run 正文；闸门自己「用法」那一节写的也是这一行，但它在
    # 模块 docstring 里（按本文件口径不算机器读引用）、release_marker.py 那条是注释、
    # deploy/*.ps1 没有这个串——所以 #90b 之后这个 token 在整套名单里**再没有任何机器读的出处**。
    # 改后钉的是 `"backend/requirements.txt"` 而且**同时钉它来自 workflow**：CI 真会 exec 的
    # 那行 `pip install … -r backend/requirements.txt` 里 pip 真会打开这个文件，比原来那一枚
    # 还硬一格（原来那枚的出处是 echo 出来的文案，这一枚是真参数）。红法和改前同一件事：
    # `_from_workflows` 的读法坏了、`run:` 正文不再被抽、或 workflows 目录形状变了。
    # 补偿控制（不归这把尺子管，写在这里是因为"少了一道"这件事必须看得见）：
    #   · 闸门文件仍在 CI 那次 checkout 里 —— `test_v0331_pre_tag_gate_contract.py` 用
    #     `git check-ignore` 钉着 `.gitignore` 里 `!/tools/pre_tag_gate.py` 那条例外；
    #   · 闸门仍是打 tag 前的全量核对 —— 同文件
    #     `test_the_two_hand_off_checks_still_run_in_the_local_gate` 真的 exec `main()`，
    #     线上 build 与物料顺序那两格任何一格被削掉都会红；
    #   · CI 侧不许再把闸门抄回 run 正文 —— 同文件
    #     `test_ci_step_is_narrowed_to_what_ci_can_actually_judge` 反向钉这个 token 不在。
    assert any(o.startswith("tests.yml") and t == "backend/requirements.txt" for o, t in kept), \
        "名单里没有来自 tests.yml 的 backend/requirements.txt：workflow 的 run 正文不再被抽出来了，" \
        "多半是 _from_workflows 坏了（#90b 之前这一格由 tools/pre_tag_gate.py 顶着）"
    assert "android-native/app/build.gradle" in tokens, \
        "名单里没有 android-native/app/build.gradle：CI 与 pre_tag_gate 核对 versionName 的位置" \
        "扫不到了，多半是 token 或 AST 读法坏了"
    assert "tools/cloudflared.exe" in tokens, \
        "名单里没有 tools/cloudflared.exe：watchdog 的 Join-Path 那一类不再被扫了——" \
        "这一类正是本条的主战场，掉了就说明 _ps_bases/_ps_joined_pointers 坏了"
    # `data` 这条不带斜杠的 token 是旧表漏掉的那一条（`Join-Path $ProjectRoot 'data'`）。
    # 它现在由 .gitignore 判豁免，但**必须仍在名单里**：从名单里掉出去和判成断链一样糟，
    # 前者是"这条引用不再有人看"，后者是本条要修的假红。
    assert "data" in tokens, \
        "名单里没有 watchdog 的 data 那一条：$ProjectRoot 拼出来的裸目录名不再被扫到了，" \
        "说明 _ps_joined_pointers 坏了——旧表就是在这条上漏了一个斜杠才让 CI 必红"


def test_the_exemption_engine_still_has_teeth():
    """豁免引擎的**双向**正对照：它得认得出"被有意排除"，也得对一条编出来的路径说不。

    本条存在的理由就是本修法最容易偷懒的那个方向：把"文件不在"一律判成"运行期资产，
    不红"，全绿立刻到手，而真断链从此再也不会红。所以这里不只看绿的那半，还钉红的那半：
    - 一条既不存在、又没被 .gitignore 写过的路径，必须得到 False（判 False 才有牙）；
    - git 答不了（None）在这套判法里等于不许绿，所以这里也钉一次它不是 None。
    两条运行期资产（`data`、`tools/cloudflared.exe`）在这里当已知锚点用：它们是不是
    被有意排除，由 .gitignore 说了算，本条只是核对 git 真的这么答——不是另写一张表。
    """
    fake = "deploy/this_pointer_was_invented_for_this_test.ps1"
    assert not (REPO_ROOT / fake).exists(), f"正对照用的那条假路径竟然真在：{fake}"
    verdict = _is_deliberately_excluded(fake)
    assert verdict is not None, "git check-ignore 答不了：这台机器上的判据退化成了恒真"
    assert verdict is False, (
        f"{fake} 既不存在也没被 .gitignore 排除，豁免引擎却说它是「运行期资产」——"
        "这条锁从此对真断链免疫了，正是本修法要躲的那个偷懒方向")
    for runtime_asset in ("data", "tools/cloudflared.exe"):
        got = _is_deliberately_excluded(runtime_asset)
        assert got is True, (
            f"{runtime_asset} 是 .gitignore 有意排除的运行期资产，判据却回 {got!r}："
            "哨兵子路径那一次探测坏了（`<路径>/__gitignore_probe__`），干净检出又要开始必红")


@pytest.mark.parametrize("origin,token", _corpus())
def test_every_machine_read_path_exists(origin, token):
    """CI 正文与脚本字面量里出现的每个路径，要么仓库里真有，要么 .gitignore 真说过不要它。"""
    target = REPO_ROOT / token
    if target.exists():
        return
    verdict = _is_deliberately_excluded(token)
    if verdict is None:
        pytest.fail(f"{origin} 里引用了 {token}，文件不在，而 git 拒绝回答它是否被有意排除"
                    f"（不是仓库 / 没有 git / 超时）——判不了就**不许**当作没断链")
    if verdict:
        # 不是"放过"，是"这条本就不该在检出里"：读它的脚本只跑在装有那东西的机器上。
        # skip 而不是 pass，为的是在计数行里看得见"有几条是按 .gitignore 豁免的"——
        # 这个数字哪天悄悄涨起来，就该去读一眼 .gitignore 是不是被人改宽了。
        pytest.skip(f"{token}：.gitignore 有意排除的运行期资产，检出里没有它是设计如此"
                    f"（出处 {origin}）")
    pytest.fail(f"{origin} 里引用了 {token}，而仓库根下没有这个东西，.gitignore 也没说它是"
                "被有意排除的——机器按这句话去找会永远找不到；改指向，别删内容")


# ---------------------------------------------------------------- 豁免名单自己也要有牙
# 上面那两条验的是"引用还在不在"。这一条验的是名单的另一半：`.gitignore` 里那条
# `android-native/.gradle/` 必须**真在、并且真是被它盖住的**。
def test_the_gradle_project_cache_is_ignored_by_a_rule_that_actually_exists():
    """Gradle 的项目本地缓存必须由一条真规则盖住，不能被一枚空模式"看起来盖住"。

    为什么值得单独钉一条（2026-10-08 安全收口）：`android-native/.gradle/` 里住的是
    checksums/、fileHashes/*.bin、executionHistory.bin——一次 `git add -A` 就把这台机器
    的二进制哈希库推进**公开**仓库，而且它每构建一遍就换一遍，"工作区干净"那条发版门禁
    判据从此恒假。它此前看着像"早就被忽略了"，而那是本文件上面 `_SENTINEL` 那段记下的
    同一枚假阳性：带尾斜杠的问法对**任何东西**都回"被排除"（命中的是 .gitignore 里一个
    空行的裸 CR，git 读成一枚空模式）。所以这里的问法是目录里的一个具体文件：

    * 不带斜杠问目录名本身（`android-native/.gradle`）在干净检出里会回"未被忽略"——那条
      模式要目录身份，而 CI 机器上没有那个目录；拿它当判据的锁会在 CI 上必红；
    * 问里面的具体文件不需要目录身份，本机与 CI 答得一样。

    两条都验：判据回 True，并且 `check-ignore -v` 指出的**模式**就是那一行本身——只看
    返回值不够，因为报告行里本来就带着被问的那个路径，光 `"android-native/.gradle/" in 报告`
    会被路径那一半蒙过去。
    """
    probe = "android-native/.gradle/fileHashes/fileHashes.bin"
    assert _is_deliberately_excluded(probe) is True, (
        f"{probe} 没被 .gitignore 排除：那枚 Gradle 项目本地缓存又在 `git add -A` 的射程里了")

    res = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "-v", "--", probe],
                         capture_output=True, timeout=60)
    report = (res.stdout or b"").decode("utf-8", "replace").strip()
    assert res.returncode == 0 and report, f"git 对 {probe} 没答上来：rc={res.returncode}"
    rule_part, _, path_part = report.partition(chr(9))
    assert path_part.endswith(probe), f"报告行认不出被问的是哪条路径：{report!r}"
    assert rule_part.endswith(":android-native/.gradle/"), (
        f"{probe} 命中的不是那条规则本身，而是 {rule_part!r}——"
        "空模式（一枚裸 CR 的空行）也算命中，而那正是这条锁要挡的假阳性")
