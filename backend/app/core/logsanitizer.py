"""运行期日志脱敏：服务商名、模型 id、上游地址不允许以明文进日志文件。

这个仓库是公开的，而部署版把 stdout/stderr 整体接进 data/backend.log——
"哪家在替我们做推理"写进源码是泄露，打进行为日志同样是泄露（09-22 安全审查
已在 named.log/auth.log 抓到实例）。日志不加密，唯一可靠的办法是让它压根
不含这些词。

本模块是唯一的打码点：
- register(term)：谁持有敏感串谁登记（providers store 的模型 id/label/host、
  memory_manager 的嵌入模型名）。幂等，代价只是一次 set.add。
- redact(text)：把已登记词与 `model=<值>` 这类通用模式换成固定掩码。
- RedactingStream：包住日志文件对象，写之前逐条打码——冻结版 exe 的全部
  print 与 uvicorn 日志都从它过。
- install_std_filters()：给 uvicorn 两个 logger 挂 logging.Filter，兜住
  没走 RedactingStream 的源码直跑 + 手工重定向场景。

掩码是固定串而不是保留前缀：留 "deep***" 这种尾巴等于把服务商指纹继续
暴露一半。
"""
import logging
import re
import threading

MASK = "●已脱敏●"

_lock = threading.Lock()
_terms: set = set()
_sorted: list = []          # 按长度降序的生效词表（长词先替，避免子串漏替）


def register(term) -> None:
    if not term or not isinstance(term, str):
        return
    term = term.strip()
    if len(term) < 4:       # 太短的词会误伤正文（比如模型别名 "gpt"）
        return
    with _lock:
        if term not in _terms:
            _terms.add(term)
            _sorted.sort(key=len, reverse=True)
            _sorted.append(term)


def registered_terms():
    with _lock:
        return frozenset(_terms)


def clear_terms_for_tests() -> None:
    with _lock:
        _terms.clear()
        _sorted.clear()


_MODEL_QUERY = re.compile(r"([?&]model=)[^&\s\"']+")


def redact(text):
    if not isinstance(text, str) or not text:
        return text
    with _lock:
        terms = list(_sorted)
    for term in sorted(terms, key=len, reverse=True):
        if term in text:
            text = text.replace(term, MASK)
    return _MODEL_QUERY.sub(lambda m: m.group(1) + MASK, text)


class RedactingStream:
    """把任何文本流包成"落盘前逐条打码"的流。

    write() 的返回值必须是**原始**字符串长度：print 与 logging 都拿它做换行
    补齐判断，返回打码后的长度会把行拼歪。
    """

    def __init__(self, inner):
        self._inner = inner

    def write(self, s):
        self._inner.write(redact(s))
        return len(s)

    def flush(self):
        try:
            self._inner.flush()
        except (ValueError, OSError):
            pass

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _RedactFilter(logging.Filter):
    """逐条打码，但**保持 record 的可格式化形状**。

    不能图省事写成 `record.msg = redact(record.getMessage()); record.args = ()`：
    uvicorn 的 AccessFormatter 不看 msg，它把 `record.args` 当成五元组拆开
    （client_addr/method/full_path/http_version/status_code，见
    uvicorn/logging.py 的 AccessFormatter.formatMessage）。args 一旦被清空，
    每条访问日志都在 formatter 里抛
    `ValueError: not enough values to unpack (expected 5, got 0)`，logging 把这
    一段 `--- Logging error ---` 连同整条调用栈吐到 stderr——也就是
    data/backend.log。现网实测：换包前后每一代日志里各有 828 / 2,462 次，
    真正的访问记录被噪声挤掉，而 1MB 轮转又把噪声本身也滚没了：出事故时
    唯一该留下的东西（谁在什么时候打了哪个接口）恰恰不在。请求本身不受影响
    （状态码照旧 200），所以这条不会让任何功能测试变红——正是本仓反复拆的那类形状。

    改法：msg 模板与 args 里的每个值各打各的码，元组长度原样留着。敏感串本来就
    在值里（模型 id、上游 host），不在 `%s` 模板里，所以脱敏性质一点没丢。
    """

    @staticmethod
    def _redact_value(v):
        return redact(v) if isinstance(v, str) else v

    def filter(self, record):
        try:
            args = record.args
            if isinstance(args, tuple):
                record.args = tuple(self._redact_value(a) for a in args)
            elif isinstance(args, dict):
                record.args = {k: self._redact_value(v) for k, v in args.items()}
            elif args is not None:
                record.args = self._redact_value(args)
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
        except Exception:
            pass                        # 打码失败绝不吞掉日志本身
        return True


def install_std_filters(loggers=("uvicorn.access", "uvicorn.error")) -> None:
    """幂等地给指定 logger 挂脱敏 filter。"""
    f = _RedactFilter()
    for name in loggers:
        lg = logging.getLogger(name)
        if not any(isinstance(x, _RedactFilter) for x in lg.filters):
            lg.addFilter(f)
