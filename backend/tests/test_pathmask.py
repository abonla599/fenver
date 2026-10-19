r"""出口路径掩码 `core.pathmask` 的判据锁：两个方向都要有人拦。

这道网只有"少掩就红"的牙（`test_selfcheck`、`test_update_channel` 那几把），一句合法 URL
被掩成半截以前没有任何一把会红。本文件补的就是**过掩**那一方向的锁，另加一枚"生产 ≡ 机制
不同的 oracle"抽样同构锁：盘符判据的形状一旦和生产网不同判，抽样表当场分家。判据本身是纯
字符串；同构锁只 `re.search`，不落盘、不起服务、不碰记忆后端。

射程（实测，不是声明）：`_WIN_PATH` 认「单字母 + `:` + 分隔符 + 一串非空白字符」，豁免只认
URL scheme 那一形；`_UNC_PATH` 认 `\\服务器\共享\…`；`_POSIX_PATH` 认首段列在 `_POSIX_ROOTS`
里的绝对路径。盘符档的边界是"这一形是不是 URL 的 `://`"，不是"路径前面粘着哪一类字符"——按
字符类划界时 `错误在at-C:\Users\a`、`路径 v1C:\data\x` 整条不被掩，而那正是这张网要拦的路径。
生产网的边界写成负向前瞻里嵌定长 lookbehind，而不是消耗型分组：消耗型 `(?:^|[^…])` 会把路径
前面那个字符（`（`、`与` 前的空格）一起吃进掩码，那是"把后半句一起吃掉"的另一种删话。oracle
抄的是两支并列的等价写法——机制不同才谈得上互相验得出，抄成同一支就只是同一个错误写两遍。
同一份盘符判据在本文件的 oracle、以及 `test_selfcheck`／`test_update_channel`／
`test_apk_self_host_cache` 里的 endpoint 探测器处各写一遍手抄，故意不收成一份：收成一份就没有
第二判据可对照，那些锁会退化成对生产网的复述。

**仍漏清单**（本文件的钉子每条一枚，全部**今天必须绿**；绿的是"网抓不住它"，不是"它无害"。
哪天把某条形状收进射程而没回来删掉这里那一条和元组里那一项，那枚钉就红）：
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
"""
import re

import pytest

from app.core.pathmask import PATH_MASK, _WIN_PATH, hide_paths

# 掩码字面被 `test_selfcheck.py` 与 `test_update_channel.py` 按字面写死，改这里就是
# 改公开端点上那句话的形状；本文件其余用例一律引用 PATH_MASK 而不重抄字面。
MASK = PATH_MASK

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
_SENTENCE_CASES = (
    (r"位置 C:\x\y 后续还有中文说明", "位置 " + MASK + " 后续还有中文说明"),
    (r"盘上没有（C:\Users\a，另有 /home/b/c）", "盘上没有（" + MASK + "，另有 " + MASK + "）"),
    (r"C:\a 与 C:\b\c", MASK + " 与 " + MASK),
    (r"问不到发布信息：清单摘要与盘上字节不符（包名 app.apk，位置 C:\Users\a\b）",
     "问不到发布信息：清单摘要与盘上字节不符（包名 app.apk，位置 " + MASK + "）"),
    (r"错误在at-C:\Users\a 处", "错误在at-" + MASK + " 处"),
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

# 生产盘符档 ≡ oracle 的抽样表：两判在这些串上必须给同一个"有没有盘符路径"。
# 名单里的 `+ - .` 各占一枚——它们就是"版本号与 URL 里那串不算边界"的理由。
_DRIVE_EQUIVALENCE_CASES = (
    r"C:\Users\someone\proj",
    r"位置 C:\x\y 后续",
    "https://ai.fever",
    "https://ai.fever.xyz/v1/update/info",
    "http://ai.fever",
    r"（C:\x",
    r"v1是C:\x",
    r"aC:\x",
    r"1.2:C:\x",
    r"+C:\x",
    r"x-C:\p\q",
    r"x.C:\p",
    r"app=C:\p",
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


def test_the_still_missed_ledger_and_the_teeth_are_one_and_the_same():
    """清单与本文件 docstring 是同一本账：每条钉子都得在射程段里有一句人话。

    加了钉忘了写账、改了账忘了改钉都会红在这里，于是注释里不必再写条数（那个数会腐烂）。
    """
    ids = [c[0] for c in _STILL_MISSED_SHAPES]
    assert len(set(ids)) == len(ids), "case_id 必须互不相同：参数化 id 会撞车"
    doc = re.sub(r"\s+", " ", __doc__ or "")
    unlisted = [i for i in ids if i not in doc]
    assert not unlisted, "这几条有钉子却没写进 docstring 的仍漏清单：" + str(unlisted)
    assert _ORACLE.pattern != _WIN_PATH.pattern, (
        "盘符判据在生产网与 oracle 已是同一份字面 ⇒ 副本收成一份 oracle 了，"
        "把 docstring 里那段手抄登记与这枚断言删掉，并把抽样同构锁换成单点判据。")


def test_the_control_shape_is_actually_caught():
    """对照组：这条**在射程内**且必须掩得住。

    连它都量不到 ⇒ 上面那一串"看不见"是尺子在空转，不是射程外。
    """
    sample, expected = _IN_RANGE_CONTROL
    assert hide_paths(sample) == expected, sample
