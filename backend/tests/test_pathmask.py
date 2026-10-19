r"""出口路径掩码 `core.pathmask` 的判据锁：两个方向都要有人拦。

这道网只有"少掩就红"的牙（`test_selfcheck`、`test_update_channel` 那几把），一句合法 URL
被掩成半截以前没有任何一把会红。本文件补的就是**过掩**那一方向的锁，另加两把同构锁：一把把
生产网的盘符档和一份**机制不同**的 oracle 钉在同一张抽样表上；一把把全仓手抄的盘符判据按源码
点名，逐把钉到生产网的判定上。掩码判据本身是纯字符串；两把同构锁只 `ast.parse` 兄弟模块的源码
取字面量，不落盘、不 import 别的测试模块、不起服务、不碰记忆后端。

射程（实测，不是声明）：`_WIN_PATH` 认「单字母 + `:` + 分隔符 + 一串非空白字符」，豁免只认
URL scheme 那一形；`_UNC_PATH` 认 `\\服务器\共享\…`；`_POSIX_PATH` 认首段列在 `_POSIX_ROOTS`
里的绝对路径。盘符档的边界是"这一形是不是 URL 的 `://`"，不是"路径前面粘着哪一类字符"——按
字符类划界时 `错误在at-C:\Users\a`、`路径 v1C:\data\x` 整条不被掩，而那正是这张网要拦的路径。
生产网的边界写成负向前瞻里嵌定长 lookbehind，而不是消耗型分组：消耗型 `(?:^|[^…])` 会把路径
前面那个字符（`（`、`与` 前的空格）一起吃进掩码，那是"把后半句一起吃掉"的另一种删话。oracle
抄的是两支并列的等价写法——机制不同才谈得上互相验得出，抄成同一支就只是同一个错误写两遍。

**网级仍漏清单**（与 `_STILL_MISSED_SHAPES` 同一本账，每条一枚钉，全部**今天必须绿**；绿的是
"网抓不住它"，不是"它无害"。哪天把某条形状收进射程而没回来删掉这里那一条和元组里那一项，那枚钉就红）：
- `spaced-drive-path-tail`：`_SEG` 把空白排除在段字符类之外，`C:\Program Files (x86)\x`
  只掩到第一段，尾段 `Files (x86)\x` 原样走出去。现网无活例（本机用户名不含空格，
  记忆后端那句维度冲突也不带路径）。真解方向是源头不往外拼绝对路径（见 `apk_cache`
  的 `_private_where` 口径：包名出公网、位置进日志），不是把空格放进 `_SEG`——那样
  `D:\data\x 是问题所在` 会被吃成一整个掩码，两头都坏。
- `posix-prefix-outside-roots`：`_POSIX_ROOTS` 是名单制，首段不在名单里的路径只掩掉
  从名单段开始的那一截，前缀 `/ai-assistant` 照读。现网是 Windows onedir，POSIX 那句
  只出现在"另有 /home/…"这类第三方原文里。
- `listed-root-clean-path-overmasked`：名单里有 `app`，于是 `/app/index.html` 这种根本
  不是本机数据的公开路径也被整条盖掉。穷尽过免鉴权端点的 detail/reason 产出点，没有
  一条活例带 `/app/`。
- `posix-has-no-start-boundary`：POSIX 那档前缀没有任何边界，所以 URL 的 path 里只要
  出现名单段就被咬掉一截（`https://ai.fever.xyz/data/apk` → 只剩主机名）。改这条要
  注意：字符类 lookbehind 不够——引擎会退化成从更靠后的名单段起匹配。
- `scheme-shaped-drive-path`：路径写成双正斜杠、又紧跟在 scheme 字符（`+ - .` 字母数字）
  后面时（`错误在at-C://p 处`），和 URL 的 scheme 一模一样，豁免把它当 URL 放过。收掉它
  要先决定"哪些 scheme 算合法"，那是白名单制的开始。现网文案里的盘符路径一律单反斜杠 ⇒ 无活例。
- `single-slash-scheme-overmasked`：少一枚斜杠的畸形 URL（`https:/ai.fever`）没有 `://`
  可认，被当盘符路径吃掉，只剩 `http`。这是豁免认 `://` 的对偶代价，规范 URL 逐字活着由
  `_VERBATIM_URLS` 钉着 ⇒ 无活例。

**锁级看不见清单**（与 `_LOCK_BLIND_SPOTS` 同一本账，同样每条一枚钉，全部**今天必须点不到**）：
两把锁点名的形是"模块级 `NAME = re.compile(字面量)`，且字面量里出现 `[A-Za-z]` 紧跟 `:` 或 `]:`"；
认不到的抄本既不被比对、也不会让绑定锁红。射程锁
（`test_the_drive_rule_roster_is_the_whole_scanned_range`）把 `_DRIVE_RULE_SITES` 与
`_DRIVE_RULE_SCAN_SCOPES` 那两棵子树里现扫出来的盘符判据钉成同一批，所以"在册的那几处变了形"
和"子树里新抄了一把没登记"、"把名单摘短而判据还在原地"都会红；红不到的只剩下方逐条列出的那几种
形状，外加 `_DRIVE_RULE_SCAN_SCOPES` 罩不到的文件——即 `backend/app/` 与 `backend/tests/`
两棵子树之外的一切（`backend/cli.py`、`backend/requirements.txt`、仓库根的 `tools/`、`scripts/`
与前端壳）。
- `copy-not-bound-to-a-module-level-name`：判据内联在调用里（`re.search(r'…', body)`）时点不到名——
  它不挂在模块级 `NAME = re.compile(...)` 上，遍历模块级赋值根本看不见这个判据。
- `copy-assembled-at-runtime`：另一格，机制不同：名字挂对了，但实参是 `_HEAD + _SEP` 这种运行时
  才拼起来的表达式，不是字符串常量，于是这一站整个隐身。与上一条不是同一格——那条连赋值都没有，
  这条是赋值在、字面量取不出。
- `copy-spelled-differently`：同一个意思换一种写法就不认识了——字符类写成 `[a-zA-Z]`、`[A-z]`、
  `[[:alpha:]]`，或把 `:` 挪进另一枚字符类里。点名的判据是字面形状，不是语义。

另有一格是**设计决定**而不是形状漏，它不归 `_LOCK_BLIND_SPOTS` 管：同一份盘符判据在本文件的
oracle、`test_selfcheck`／`test_update_channel`／`test_apk_self_host_cache` 里的 endpoint 探测器
处各写一遍手抄，故意不收成一份——收成一份就没有第二判据可对照，那几把锁会退化成对生产网的复述。
绑定锁只保证这些抄本同判，不保证只有一份；`_ORACLE.pattern != _WIN_PATH.pattern` 那枚断言是这件
事的报警器，哪天真收成一份，把这段登记与那枚断言一起删掉。
"""
import ast
import re
import warnings
from pathlib import Path

import pytest

from app.core.pathmask import PATH_MASK, _SEG, _WIN_PATH, hide_paths

# 掩码字面被 `test_selfcheck.py` 与 `test_update_channel.py` 按字面写死，改这里就是
# 改公开端点上那句话的形状；本文件其余用例一律引用 PATH_MASK 而不重抄字面。
MASK = PATH_MASK

_BACKEND = Path(__file__).resolve().parents[1]

# 生产网的盘符档与测试侧 oracle 必须同判、且机制不同。oracle 抄的是两支并列的写法：一支要求
# 路径前不是 scheme 字符，另一支在前面是 scheme 字符时要求分隔符不是 `//`。只判定"这里有没有
# 一段盘符路径"，不判定掩掉多少。
_ORACLE = re.compile(r"(?<![A-Za-z0-9+.-])(?P<drive>[A-Za-z]):[\\/]"
                     r"|[A-Za-z0-9+.-][A-Za-z]:(?!//)[\\/]")

# 逐字必须活着的 URL：免鉴权端点的正常载荷里就带着 https 地址（壳与官网都靠它）。
# 后三枚是豁免的边界：带连字符的 scheme、数字结尾的 scheme、非 https 的 scheme——它们和
# `https://` 走的是同一条判据，豁免一旦写窄成"只放过 http(s)"就在这里红。
_VERBATIM_URLS = (
    "https://ai.fever",
    "http://ai.fever",
    "https://ai.fever.xyz/v1/update/info",
    "https://ai.fever.xyz/v1/release/latest?have=0.16",
    "https://ai.fever.xyz/site/android.apk",
    "ms-appx-web://host/p",
    "s3://bucket/key",
    "ftp://ai.fever.xyz/v1/update/info",
)

# 句子其余部分必须照读：只盖路径那一段，前后连接词与标点一个都不许跟着消失。
# 最后两条把 URL 和盘符路径放进同一句：纯 URL 的夹具里没有任何掩码，纯路径的夹具里没有 URL，
# "已经掩过就再补一刀"这类条件式判据只有在这两条上才露形——补刀会把 `s://` 一起咬掉。
_SENTENCE_CASES = (
    (r"位置 C:\x\y 后续还有中文说明", "位置 " + MASK + " 后续还有中文说明"),
    (r"盘上没有（C:\Users\a，另有 /home/b/c）", "盘上没有（" + MASK + "，另有 " + MASK + "）"),
    (r"C:\a 与 C:\b\c", MASK + " 与 " + MASK),
    (r"问不到发布信息：清单摘要与盘上字节不符（包名 app.apk，位置 C:\Users\a\b）",
     "问不到发布信息：清单摘要与盘上字节不符（包名 app.apk，位置 " + MASK + "）"),
    (r"错误在at-C:\Users\a 处", "错误在at-" + MASK + " 处"),
    (r"位置 C:\x 详见 https://ai.fever.xyz/v1/update/info",
     "位置 " + MASK + " 详见 https://ai.fever.xyz/v1/update/info"),
    (r"问不到发布信息 https://ai.fever.xyz/v1/release/latest（位置 C:\Users\someone\chroma_db）",
     "问不到发布信息 https://ai.fever.xyz/v1/release/latest（位置 " + MASK + "）"),
)

# 路径前面的那个字符是句子的，不是路径的——lookbehind 与消耗型分组就差在这一个字符上。
_LEADING_CHAR_CASES = (
    (r"（C:\x", "（" + MASK),
    (r"v1是C:\x", "v1是" + MASK),
    (r"（\\srv\share", "（" + MASK),
)

# 少掩方向：这三档各自一枚，网要是松了必须在这里红。
_IN_RANGE_CASES = (
    (r"C:\Users\someone\proj\chroma_db", MASK),
    (r"\\srv\share\x", MASK),
    (r"C:\\Users\\someone\\proj", MASK),
    (r"/home/someone/proj/chroma_db", MASK),
    ("attempt to write a readonly database（" + r"C:\Users\a" + "），另有 /home/b/c 一处读不动",
     "attempt to write a readonly database（" + MASK + "），另有 " + MASK + " 一处读不动"),
    # 现网 /health 那句记忆库维度冲突的真形状：盘符路径被前后空格夹在中文句子里
    # （用户名换成合成串，仓库是公开的）。补边界之后它必须仍被整段掩掉。
    ("请固定使用同一嵌入后端，或删除 " + r"C:\Users\someone\ai-assistant\chroma_db" + " 重建（会丢失已有记忆）。",
     "请固定使用同一嵌入后端，或删除 " + MASK + " 重建（会丢失已有记忆）。"),
    # 路径前面粘着 `+ - .`、字母或数字时也必须掩：这些字符属于句子（`at-`、版本号 `v1`），
    # 边界一旦写成"前一字符是哪一类"，整条路径就不掩了，而少掩方向没有别的锁会红。
    (r"错误在at-C:\Users\a 处", "错误在at-" + MASK + " 处"),
    (r"路径 v1C:\data\x", "路径 v1" + MASK),
    (r"值取自x.C:\p 一处", "值取自x." + MASK + " 一处"),
)

# 各判据（生产网、oracle、三把探测器）在同一条抽样表上必须给同一个"有没有盘符路径"。
# `+ - .` 各占一枚、字母紧贴一枚、数字紧贴一枚；`9:\a` 钉的是"盘符档是**单字母**"——字符类
# 一旦加宽成 `[A-Za-z0-9]`，生产网在这里认出盘符而 oracle 不认，抽样表当场分家；URL 与路径
# 同句两枚钉的是"同一句里有掩码时不许再补一刀"；`C://x/y` 与 `错误在at-C://p 处` 是豁免的两侧。
_DRIVE_EQUIVALENCE_CASES = (
    r"C:\Users\someone\proj",
    r"位置 C:\x\y 后续",
    "https://ai.fever",
    "https://ai.fever.xyz/v1/update/info",
    "http://ai.fever",
    "ms-appx-web://host/p",
    r"（C:\x",
    r"v1是C:\x",
    r"aC:\x",
    r"1.2:C:\x",
    r"+C:\x",
    r"x-C:\p\q",
    r"x.C:\p",
    r"app=C:\p",
    r"9:\a",
    r"错误在at-C:\Users\a 处",
    r"路径 v1C:\data\x",
    r"C://x/y",
    r"错误在at-C://p 处",
    "https:/ai.fever",
    r"位置 C:\x 详见 https://ai.fever.xyz/v1/update/info",
    r"问不到发布信息 https://ai.fever.xyz/v1/release/latest（位置 C:\Users\someone\chroma_db）",
    r"D:\data\x 是问题所在",
    r"\\srv\share",
    "/home/a/b",
)

# 仍漏清单的钉子（与模块 docstring 那段是同一本账）。每条 (case_id, 喂进去的句子,
# 今天实测的结果, 为什么算漏)。今天必须绿；哪天网真的抓住了它，结果就变了，钉就红。
_STILL_MISSED_SHAPES = (
    ("spaced-drive-path-tail",
     r"C:\Program Files (x86)\x",
     MASK + " Files (x86)\\x",
     "段字符类不含空白，只咬到第一段；空格放进段类会把反向的句子吃成一整个掩码"),
    ("posix-prefix-outside-roots",
     "/ai-assistant/data/apk",
     "/ai-assistant" + MASK,
     "首段不在 `_POSIX_ROOTS` 名单里，前缀照读——名单制的设计后果"),
    ("listed-root-clean-path-overmasked",
     "/app/index.html",
     MASK,
     "名单含 `app`，公开路径也被整条盖掉；现网 detail/reason 无 `/app/` 活例"),
    ("posix-has-no-start-boundary",
     "https://ai.fever.xyz/data/apk",
     "https://ai.fever.xyz" + MASK,
     "POSIX 那档前无边界，URL 的 path 里出现名单段就咬掉一截"),
    ("scheme-shaped-drive-path",
     r"错误在at-C://p 处",
     r"错误在at-C://p 处",
     "双正斜杠写法紧跟在 scheme 字符后面时与 URL 同形，被当 URL 放过；收它要先决定哪些 scheme 合法"),
    ("single-slash-scheme-overmasked",
     "https:/ai.fever",
     "http" + MASK,
     "少一枚斜杠就没有 `://` 可认，被当盘符路径吃掉——豁免认 `://` 的对偶代价，规范 URL 由 _VERBATIM_URLS 钉着"),
)

# 锁级看不见清单：每条 (case_id, 合成源码, 为什么点不到名)。今天必须点不到；点到了就是射程收了
# 这一格（把 docstring 那条和这里这一项一起删），或者合成源码写坏了（那这枚钉绿得没有意义）。
_LOCK_BLIND_SPOTS = (
    ("copy-not-bound-to-a-module-level-name",
     "import re\nBODY = ''\n"
     "def _leaked():\n"
     "    return bool(re.search(r'[A-Za-z]:[/\\\\]', BODY))\n",
     "判据内联在 `re.search` 的调用里，模块级赋值遍历看不见它"),
    ("copy-assembled-at-runtime",
     "import re\n_HEAD = r'[A-Za-z]:'\n_SEP = r'[/\\\\]'\n"
     "_DRIVE_PATH = re.compile(_HEAD + _SEP)\n",
     "名字挂对了但实参是 `BinOp`，字符串常量取不出来，这一站整个隐身"),
    ("copy-spelled-differently",
     "import re\n_DRIVE_PATH = re.compile(r'[a-zA-Z]:[/\\\\]')\n",
     "字符类换一种写法就不匹配点名用的字面形状（`[a-zA-Z]`、`[A-z]` 同理）"),
)

# 对照组：绑定锁对它说"认得"的那个形状是真认得到——没有这一枚，上面那串"点不到名"是尺子在空转。
_DISCOVERABLE_COPY = "import re\n_DRIVE_PATH = re.compile(r'[A-Za-z]:(?!//)[/]')\n"

# 绑定锁的点名表：全仓手抄的盘符判据住在哪（相对 backend 的路径 → 常量名）。这张表的射程不由
# 它自己说——由 `_scanned_drive_rule_sites()` 现扫出来跟它比（见
# `test_the_drive_rule_roster_is_the_whole_scanned_range`）；点名器认不到的形状见 `_LOCK_BLIND_SPOTS`。
_DRIVE_RULE_SITES = {
    "app/core/pathmask.py": {"_WIN_PATH"},
    "tests/test_pathmask.py": {"_ORACLE"},
    "tests/test_selfcheck.py": {"_DRIVE_PATH"},
    "tests/test_update_channel.py": {"_DRIVE_PATH"},
    "tests/test_apk_self_host_cache.py": {"_DRIVE_PATH"},
}

# 射程锁现扫的范围（相对 backend 的 glob 模式）。要放大射程就正面往这里加一条模式，
# 别在锁里另写一份范围。
_DRIVE_RULE_SCAN_SCOPES = ("app/**/*.py", "tests/*.py")

# 点认识的形状：`[A-Za-z]` 后面紧跟 `:`，或先收掉包着它的那枚右括号再跟 `:`
# （`(?P<drive>[A-Za-z]):` 是 oracle 与探测器的写法）。
_DRIVE_SHAPE = re.compile(r"\[A-Za-z]\)?:")


def _drive_rule_literals(source, filename):
    """点名源码里的盘符判据，返回 [(常量名, 字面量)]。

    认得：模块级 `NAME = re.compile(<一枚字符串字面量>)`（相邻的几枚 raw 字面量在编译期就并成
    一枚，算一枚）；f-string 只允许 `{_SEG}` 这一种插值（生产网那一枚）。非字符串实参、bytes
    实参、别的插值一律当认不到——那一站从结果里消失，而消失会在 `_DRIVE_RULE_SITES` 的一致性
    比对上红，不会静默通过。
    """
    found = []
    for node in ast.parse(source, filename=filename).body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Attribute) and func.attr == "compile" and node.value.args):
            continue
        arg = node.value.args[0]
        literal = None
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            literal = arg.value
        elif isinstance(arg, ast.JoinedStr):
            parts = []
            for piece in arg.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    parts.append(piece.value)
                elif (isinstance(piece, ast.FormattedValue)
                      and isinstance(piece.value, ast.Name) and piece.value.id == "_SEG"):
                    parts.append(_SEG)
                else:
                    parts = None
                    break
            literal = "".join(parts) if parts else None
        if literal and _DRIVE_SHAPE.search(literal):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            found.append((names[0] if names else "<无名>", literal))
    return found


def _discovered_sites():
    """按 `_DRIVE_RULE_SITES` 那份名单逐文件点名，返回 {路径: {常量名: 字面量}}。"""
    sites = {}
    for rel in _DRIVE_RULE_SITES:
        source = (_BACKEND / rel).read_text(encoding="utf-8")
        for name, literal in _drive_rule_literals(source, rel):
            sites.setdefault(rel, {})[name] = literal
    return sites


def _scanned_drive_rule_sites():
    """不读名单、自己按 `_DRIVE_RULE_SCAN_SCOPES` 现扫，返回 {(相对路径, 常量名): 字面量}。

    用的尺子与绑定锁同一把（`_drive_rule_literals`），所以两把锁对"什么算一盘符判据"没有两套
    口径；范围也只从 `_DRIVE_RULE_SCAN_SCOPES` 取，名单在这把尺子之外没有任何发言权。
    """
    found = {}
    with warnings.catch_warnings():
        # 扫到的是别人的源码文本，不是本锁的产物：被点名的模块自带 `\` 转义写法时会往测试输出里
        # 塞一条 SyntaxWarning，把人的注意力引到"要去改那个模块"上。这里只读字面量，不作答。
        warnings.simplefilter("ignore", SyntaxWarning)
        for pattern in _DRIVE_RULE_SCAN_SCOPES:
            for path in sorted(_BACKEND.glob(pattern)):
                rel = path.relative_to(_BACKEND).as_posix()
                for name, literal in _drive_rule_literals(path.read_text(encoding="utf-8"), rel):
                    found[(rel, name)] = literal
    return found

# 对照组：这条**在射程内**，所以"看不见"那串必须能和它区分开——尺子要是连它都量不到，
# 上面那一串绿就只是空转。
_IN_RANGE_CONTROL = (r"C:\Users\a\b", MASK)


def test_a_plain_url_survives_verbatim():
    """过掩方向第一把锁：合法 URL 必须逐字活着。

    `https://ai.fever` 里 `s:/` 那个冒号不是盘符分隔符，判据认不出 scheme 时它会被咬成
    `http〈路径已隐去〉`——免鉴权端点的载荷里本来就带着 https 地址，把主机名删掉等于把
    "去哪儿看这条接口"改坏。带连字符与数字结尾的 scheme 走的是同一条豁免。
    """
    for url in _VERBATIM_URLS:
        assert hide_paths(url) == url, url


def test_the_rest_of_the_sentence_stays_readable():
    """只盖路径那一段：句子其余部分照读，连接词和两侧空格都得留着。"""
    for text, expected in _SENTENCE_CASES:
        assert hide_paths(text) == expected, text


def test_the_path_boundary_is_a_lookbehind_not_a_consuming_group():
    """路径前面那个字符属于句子，不属于路径。

    把边界写成消耗型分组 `(?:^|[^…])` 的话，`（C:\\x` 会连 `（` 一起进掩码、`C:\\a 与 C:\\b\\c`
    会连 `与` 前的空格一起进掩码——那就是"把后半句一起吃掉"。
    """
    for text, expected in _LEADING_CHAR_CASES:
        assert hide_paths(text) == expected, text


def test_the_under_masked_shapes_are_still_caught():
    """少掩方向：盘符、UNC、转义写法、POSIX，以及路径前粘着 `+ - .` 字母数字的形状，网松了就红。"""
    for text, expected in _IN_RANGE_CASES:
        assert hide_paths(text) == expected, text


def test_the_mask_literal_is_the_one_the_endpoint_locks_hardcode():
    """掩码字面被别的端点锁按字面写死，这里钉住它不许改。"""
    assert PATH_MASK == "〈路径已隐去〉"
    assert hide_paths("") == ""


def test_production_drive_rule_agrees_with_the_two_branch_oracle():
    """「生产 ≡ oracle」同构锁：两判在同一张表上必须给同一个"有没有盘符路径"。

    生产网自己看不见自己的边界有没有——发现形状错靠的是它和一份机制不同的抄本对不上。
    所以这枚钉先于"把副本收成一份"存在：只收副本不补这枚钉，等于拆掉唯一的报警器。
    """
    disagreed = [s for s in _DRIVE_EQUIVALENCE_CASES
                 if bool(_WIN_PATH.search(s)) != bool(_ORACLE.search(s))]
    assert not disagreed, "生产盘符档与两支并列的 oracle 在这些串上分家了：" + str(disagreed)


def test_every_hand_copied_drive_rule_is_bound_to_production():
    """绑定锁：全仓手抄的盘符判据逐把与生产网同判，改窄改宽都红。

    探测器与生产网同构时它瞎在网瞎的同一处，那几把 endpoint 锁就退化成恒绿自证；而"把探测器
    改窄一点（删掉豁免、换个 lookbehind、少一类字符）"这种改动原本全仓没有任何一把锁会红。
    这里补的就是那把锁：先按名单点名（在册的抄本变了形 ⇒ 不一致；名单本身有没有被人摘短、
    子树里有没有新抄一把没登记，由下面那把射程锁管），再把每把抽出来的字面量编译起来，
    在 `_DRIVE_EQUIVALENCE_CASES` 上要求它与生产网给同一个答案
    ——改窄（对 `错误在at-C:\\Users\\a` 失明）与改宽（遇合法 URL 恒红）两个方向都会红。抽取
    fidelity 自己也被钉住：生产网与 oracle 那两站抽出来的字面量必须与在用的判据一字不差。
    """
    discovered = _discovered_sites()
    got = {rel: set(names) for rel, names in discovered.items()}
    assert got == _DRIVE_RULE_SITES, (
        "点名结果与名单不一致（名单 " + str(sorted(_DRIVE_RULE_SITES.items()))
        + "，实点 " + str(sorted(got.items())) + "）：新增一处手抄就把它登记进 `_DRIVE_RULE_SITES`，"
        "删掉一处就连名单一起删；判据换了形状（见 `_LOCK_BLIND_SPOTS`）就正面回来改点名的形状。")
    in_use = {
        "app/core/pathmask.py": _WIN_PATH.pattern,
        "tests/test_pathmask.py": _ORACLE.pattern,
    }
    for rel, literals in sorted(discovered.items()):
        for name, literal in sorted(literals.items()):
            if rel in in_use:
                assert literal == in_use[rel], rel + ":" + name + " 抽出来的字面量与在用的判据对不上"
            copy = re.compile(literal)
            disagreed = [s for s in _DRIVE_EQUIVALENCE_CASES
                         if bool(copy.search(s)) != bool(_WIN_PATH.search(s))]
            assert not disagreed, (rel + ":" + name + " 与生产网在这些串上分家"
                                   "（改窄=恒绿自证，改宽=遇合法 URL 恒红）：" + str(disagreed))


def test_the_drive_rule_roster_is_the_whole_scanned_range():
    """射程锁：`_DRIVE_RULE_SITES` 必须正好等于现扫出来的那一批盘符判据，三个方向分开报。

    绑定锁只读名单里列的那几份文件，所以"把名单摘短、一条断言不动"能让它对着变小的射程继续
    宣称全仓手抄逐把同判——名字里那句"全仓"要靠这把射程锁兑现。这里把名单和
    `_DRIVE_RULE_SCAN_SCOPES` 里现扫的结果放在一起比，红话按不对称的方向分：在册的文件里冒出
    没登记的常量名 ⇒ 有人新抄了一把（或把在册那把改了名）；扫到的文件整条不在名单里 ⇒ 要么
    新抄本没登记，要么有人把 `_DRIVE_RULE_SITES` 那一行摘掉了而判据还留在原地；名单登记了却
    扫不到 ⇒ 那一站的判据被删、改名或换成了点名器认不到的形状，名单陈旧。
    """
    scanned = set(_scanned_drive_rule_sites())
    registered = {(rel, name) for rel, names in _DRIVE_RULE_SITES.items() for name in names}
    new_in_registered_file = sorted(
        (rel, name) for rel, name in scanned - registered if rel in _DRIVE_RULE_SITES)
    new_file = sorted({rel for rel, name in scanned - registered if rel not in _DRIVE_RULE_SITES})
    stale = sorted(registered - scanned)
    assert not new_in_registered_file, (
        "在册文件里冒出没登记的盘符判据 " + str(new_in_registered_file)
        + "：有人新抄了一把没登记（或把在册那一把改了名）。登记进 `_DRIVE_RULE_SITES`，"
        "它随即在绑定锁里与生产网比同判。")
    assert not new_file, (
        "扫到了名单里没有的文件 " + str(new_file)
        + "：要么是新抄本没登记，要么是有人把 `_DRIVE_RULE_SITES` 里这几行摘短了而判据还在原地"
        "（只删名单不删判据就是把射程改窄）。两种都正面回来把那一行加回去。")
    assert not stale, (
        "名单登记了、现扫点不到 " + str(stale)
        + "：那一站的判据被删了、改了名，或换成了点名器认不到的形状（见 `_LOCK_BLIND_SPOTS`）。"
        "真删了就连名单一起删；换形状就改点名的形状，别让名单比射程长。")


@pytest.mark.parametrize("case_id,sample,today,why", _STILL_MISSED_SHAPES,
                         ids=[c[0] for c in _STILL_MISSED_SHAPES])
def test_the_declared_still_missed_shapes_are_still_missed_today(case_id, sample, today, why):
    """边界钉：仍漏清单里每一条**今天都得看不见**，钉的是"声明与测量一致"。

    红了只有两种可能：要么这条形状真的进了射程（好消息——把 docstring 那一条和元组里
    这一项删掉），要么网在这些串上的行为变了而没人知道（坏消息——先回答为什么变了）。
    """
    assert hide_paths(sample) == today, (
        "边界钉「" + case_id + "」对不上了：实测结果不是清单里写的那个形状（实得 "
        + repr(hide_paths(sample)) + "，清单写 " + repr(today) + "）。这一条记的漏是：" + why
        + "。网变强了就回来把本文件的仍漏清单与这条钉子一起删掉。")


def test_the_still_missed_ledgers_and_the_teeth_are_one_and_the_same():
    """两本账与本文件 docstring 是同一本账：每条钉子都得在射程段里有一句人话，反之亦然。

    加钉忘写账、改账忘改钉、**把清单里某一条无声删掉**（参数化跟着少跑一条，那才是真正的漏）
    都会红在这里——两个方向都比，条数就不必写进注释（那个数会腐烂），一致性由这里现算。
    `_ORACLE.pattern != _WIN_PATH.pattern` 那枚断言住在这里：它是"手抄没收成一份"这件事的报警器。
    """
    ids = [c[0] for c in _STILL_MISSED_SHAPES] + [c[0] for c in _LOCK_BLIND_SPOTS]
    assert len(set(ids)) == len(ids), "case_id 必须互不相同：参数化 id 会撞车"
    declared = re.findall(r"(?m)^- `([a-z0-9-]+)`", __doc__ or "")
    assert len(set(declared)) == len(declared), "docstring 的清单里有重名的条目"
    unlisted = [i for i in ids if i not in declared]
    unpinned = [d for d in declared if d not in ids]
    assert not unlisted, "这几条有钉子却没写进 docstring 的清单：" + str(unlisted)
    assert not unpinned, "docstring 里写了这几条却没有对应的钉子（清单被无声删了一条？）：" + str(unpinned)
    assert _ORACLE.pattern != _WIN_PATH.pattern, (
        "盘符判据在生产网与 oracle 已是同一份字面 ⇒ 副本收成一份 oracle 了，"
        "把 docstring 里那段手抄登记与这枚断言删掉，并把抽样同构锁换成单点判据。")


@pytest.mark.parametrize("case_id,source,why", _LOCK_BLIND_SPOTS,
                         ids=[c[0] for c in _LOCK_BLIND_SPOTS])
def test_the_declared_lock_blind_spots_are_blind_today(case_id, source, why):
    """边界钉：锁级看不见清单里每一条**今天都点不到名**，钉的是"声明与测量一致"。

    红了两种可能：点名器学会了这种形状（好消息——把 docstring 那一条和元组里那一项删掉），
    或者合成源码写坏了（那这枚钉绿得没有意义，先回答它到底还在不在指那一格）。
    """
    assert _drive_rule_literals(source, case_id) == [], (
        "「" + case_id + "」已经点得到名了：" + why + "。射程收了就回来删这条账。")


def test_the_binding_lock_actually_sees_a_registered_shape():
    """对照组：点名器对它说"认得"的那个形状是真认得到。

    连它都点不到 ⇒ 上面那一串"点不到名"是尺子在空转，不是射程外。
    """
    found = _drive_rule_literals(_DISCOVERABLE_COPY, "<control>")
    assert [name for name, _ in found] == ["_DRIVE_PATH"], found


def test_the_control_shape_is_actually_caught():
    """对照组：这条**在射程内**且必须掩得住。

    连它都量不到 ⇒ 上面那一串"看不见"是尺子在空转，不是射程外。
    """
    sample, expected = _IN_RANGE_CONTROL
    assert hide_paths(sample) == expected, sample
