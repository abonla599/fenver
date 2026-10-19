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
   那一句启动摘要，而运维查的恰恰是它。这里的网只对**响应**生效，日志那份原样保留。

边界卡在字符类上（`_SEG`）：路径后面常常紧跟中文标点或空格，把后半句一起吃掉就等于
悄悄删了别人的话。POSIX 那一档只认列在 `_POSIX_ROOTS` 里的首段——`/v1/memory`、
`/health` 是路由名，不是路径，掩掉它们等于把"去哪儿看这条接口"改坏。
"""
import re

PATH_MASK = "〈路径已隐去〉"

# 路径里允许出现的字符 = 除空格与中文标点之外的可见字符。
_SEG = r"[^\s，。；、：\"'（）()<>｜|]"
_WIN_PATH = re.compile(rf"[A-Za-z]:[\\/]{_SEG}*")
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
