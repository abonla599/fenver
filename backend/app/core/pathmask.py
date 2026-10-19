"""出口层的路径掩码：公开端点上那句理由可以说话，但不许说出这台机器长什么样。

为什么要有第二层：免鉴权端点上的原因文案不只有我们写的那几份。`/health` 的
`memory_init_error` 是第三方异常原文（chroma/SQLite 读盘失败会把库路径写进 OSError 的
message），`/v1/update/info` 的 502 detail 里那句"安装包不可用"来自磁盘判据——措辞将来
还会被别的来源填进来。`scrub_secrets` 认的是凭据形状，绝对路径既不是凭据也不在它的
模式里，于是一句路径能原样走到公开端点上。源头那些文案各自改过一轮之后，这里再兜一道。

为什么是**明写的掩码**而不是删掉、也不是登记进 logsanitizer：
① 删掉会让"这句话为什么少了一截"没人说得清，整句吞掉更是把故障说成"说不清"——那是
   另一种 bug，所以只盖路径那一段，句子其余部分照读；
② logsanitizer 作用于 server.log，把数据根登记进去会连带打码"我的数据到底落在哪"
   那一句启动摘要，而运维查的恰恰是它。
   这张网的实际射程比"只管响应"宽：`selfcheck` 是先 `_scrub` 再打印，所以**它自己那行
   启动摘要也被掩**。不经这道网的是 `server.log` 里 `paths`／`memory_manager`／`apk_cache`
   那几类——`apk_cache._private_where` 明写刻意不走出口脱敏，因为运维查缺包查的就是
   "盘上到底是哪一个文件"。

边界卡在字符类上（`_SEG`）：路径后面常常紧跟中文标点或空格，把后半句一起吃掉就等于
悄悄删了别人的话。POSIX 那一档只认列在 `_POSIX_ROOTS` 里的首段——`/v1/memory`、
`/health` 是路由名，不是路径，掩掉它们等于把"去哪儿看这条接口"改坏。
"""
import re

PATH_MASK = "〈路径已隐去〉"

# 路径里允许出现的字符 = 除空格与中文标点之外的可见字符。
_SEG = r"[^\s，。；、：\"'（）()<>｜|]"
# 盘符档的豁免只认 URL scheme 那一形：`scheme 字符 + 单字母 + ://` 才放过，其余照掩。
# 免鉴权端点的正常载荷里就带着 https 地址（壳与官网都靠它），把主机名盖掉等于把"去哪儿看
# 这条接口"改坏；而 `s:/` 与 `C:\` 同形，靠的正是 `://` 这枚 scheme 分隔符把它们分开。
# 豁免不挂在"前一个字符属于哪一类"上：那样划界会让路径前面粘着 `+`、`-`、`.`、字母或数字
# 的形状（`错误在at-C:\Users\a`、`路径 v1C:\data\x`）整条不被掩，而那正是这张网要拦的路径；
# 少掩的方向无人拦，比多掩一个畸形 URL 贵得多。
# 条件写成负向前瞻里嵌定长 lookbehind，而不是消耗型分组：后者会把路径前那个字符（`（`、
# `与` 前的空格）一起吃进掩码，那是"把后半句一起吃掉"的另一种删话。
# 认得的形状（正面声明）：`C:\x`、`C:/x`、`C:\\x`（转义写法）、粘在 `at-`/`v1`/`x.`/`a`
# 后面的同一形状。放过的是 `https://…`/`ftp://…`/`ms-appx-web://…` 这类规范 scheme，
# 以及数字结尾的 scheme（`s3://…`——`:` 前不是字母，本来就不在盘符档的形状里）。
_WIN_PATH = re.compile(rf"(?!(?<=[A-Za-z0-9+.-])[A-Za-z]://)[A-Za-z]:[\\/]{_SEG}*")
_UNC_PATH = re.compile(rf"\\\\{_SEG}+")
# 只认这些首段的 POSIX 路径：`/data/x` 是路径，`/v1/memory` 与 `/health` 是路由名，
# 把后者也换成掩码等于把"去哪儿看这条接口"这类正常说明改坏。
_POSIX_ROOTS = ("home|users|usr|var|private|tmp|opt|root|data|mnt|srv|app|"
                "volumes|workspace|repo")
_POSIX_PATH = re.compile(rf"/(?:{_POSIX_ROOTS})/{_SEG}*", re.IGNORECASE)


def hide_paths(text: str) -> str:
    """把绝对路径换成明写的掩码，句子其余部分照读（空串原样返回）。"""
    if not text:
        return text
    # 盘符路径先走：`C:\\Users\\…` 这种转义写法里也含成对反斜杠，UNC 规则先跑会把
    # 整个路径从中间截断、把开头的 `C:` 留在外面。
    out = _WIN_PATH.sub(PATH_MASK, text)
    out = _UNC_PATH.sub(PATH_MASK, out)
    return _POSIX_PATH.sub(PATH_MASK, out)
