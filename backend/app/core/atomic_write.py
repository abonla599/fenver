"""JSON 存储的公共原子落盘：tmp → flush + fsync → os.replace。

为什么收成这一个函数：8 个可变数据存储（sessions、feedback、uploads 索引、
providers、usage、schedule、tasks、users）此前各自抄过同一段写法，抄漏一处
就是"replace 说成功、盘上却没有"——断电或蓝屏之后丢的正是最后那次写入，
而 watchdog 会把进程原样拉起来，谁都不会发现。fsync 在 replace 之前是这条
路径唯一的落盘证明。

行为与收编前的 8 份逐一等价：同目录 .tmp、UTF-8、ensure_ascii=False、
indent=2、失败不清理（feedback_storage 自己包了清理逻辑，保持原样）。
"""
import json
import os


def write_json_atomic(path: str, payload) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        # replace 原子不等于落盘：flush 到 OS、fsync 到磁盘，之后才 replace。
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
