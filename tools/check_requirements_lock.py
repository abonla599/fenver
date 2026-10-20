#!/usr/bin/env python3
"""依赖锁一致性检查：backend/requirements.txt 与 backend/requirements.lock 不许分家。

## 为什么要有这一步（#146）

CI 从锁装包（`pip install --require-hashes -r backend/requirements.lock`），
`backend/requirements.txt` 是唯一的人工清单。两者分家只需要一句话：

- 改了清单却没重编锁 ⇒ CI 装的是旧锁，测试跑在一套"清单里没写"的环境上，
  绿灯证明不了清单可用；
- 有人把锁里的某条钉回区间、或删掉哈希 ⇒ `--require-hashes` 要么装不上，
  要么哈希只是装饰，索引上的同版本产物换了也没人知道。

所以这里只判三件事，纯文本、不读网络、不跑 pip：

1. 清单每一行都是精确 `==`（区间在锁里没有意义：锁只认一个版本）；
2. 清单里每一条都能在锁里找到同名同版本的条目；
3. 锁文件头是 `uv pip compile ... --universal ... --generate-hashes` 生成的，
   且每一条都带至少一个形状合法的 `--hash=sha256:`。

做法与 `tools/gen_changelog.py --check` 同一路数：可以本地跑，也可以进 CI；
退出码 0 = 一致，1 = 分家（逐条打印原因，不猜、不跳过）。

## 用法

    python tools/check_requirements_lock.py
    python tools/check_requirements_lock.py <清单> <锁>   # 反例实验用

默认路径按脚本位置定位仓库根，所以在哪个目录下调用都一样。
"""
import argparse
import re
import sys
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "backend" / "requirements.txt"
DEFAULT_LOCK = REPO_ROOT / "backend" / "requirements.lock"

# 锁里的哈希形状：uv 只写 sha256，长度固定 64 个十六进制字符。形状不合法
# （比如手工截短）不算"有哈希"，否则 `--require-hashes` 那个开关等于没开。
_HASH_RE = re.compile(r"--hash=sha256:[0-9a-f]{64}\b")
# 锁文件头必须证明它是这样生成的：换掉其中任何一项（非 universal、无哈希），
# 锁就不再是"Windows 能装、Linux CI 也能装"的那一份。
_REQUIRED_HEADER_TOKENS = ("uv pip compile", "--universal", "--generate-hashes")


def _canonical(name: str) -> str:
    """PEP 503 名字归一化：typing_extensions / typing-extensions 是同一个包。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _strip_comment(line: str) -> str:
    """去掉行内注释。本仓的清单里没有 URL（URL 里的 `#` 会误伤），这一条够用。"""
    return line.split("#", 1)[0].strip()


def _exact_version(req: Requirement, where: str) -> tuple:
    """取 `==x.y.z` 的版本；不是精确钉版就返回问题而不是猜一个。"""
    specifiers = list(req.specifier)
    if len(specifiers) != 1 or specifiers[0].operator != "==" or "*" in specifiers[0].version:
        return None, (f"{where} 不是精确钉版：{str(req)!r}（锁只认一个版本，"
                      f"区间请先用 uv pip compile 解出来再写回清单）")
    return specifiers[0].version, None


def _parse_manifest(path: Path) -> tuple:
    """返回 ({归一化包名: (原包名, 版本, 行号)}, [问题])。"""
    pins = {}
    problems = []
    text = path.read_text(encoding="utf-8-sig")
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line:
            continue
        if line.startswith("-"):
            problems.append(f"清单第 {lineno} 行是选项行（{line!r}）：本检查只认包声明，"
                            "出现 -r/--index-url 之类会让锁与清单的关系不再是可判的")
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement as exc:
            problems.append(f"清单第 {lineno} 行解析失败：{line!r}（{exc}）")
            continue
        if req.url:
            problems.append(f"清单第 {lineno} 行是 URL 依赖（{line!r}）：锁检查不支持这种形态")
            continue
        version, problem = _exact_version(req, f"清单第 {lineno} 行")
        if problem:
            problems.append(problem)
            continue
        canonical = _canonical(req.name)
        if canonical in pins:
            problems.append(f"清单第 {lineno} 行重复声明 {req.name}（第一次在第 {pins[canonical][2]} 行）")
            continue
        pins[canonical] = (req.name, version, lineno)
    if not pins:
        problems.append("清单里一条精确钉版都没解析出来：检查文件被清空或读法失效")
    return pins, problems


def _logical_entries(text: str):
    """把锁的续行拼回逻辑条目，逐个 yield (起始行号, 拼接后的正文)。

    uv 的输出形状是 `name==ver \\` 后面跟若干 `--hash=... \\`，最后一行不带反斜杠；
    按"上一行以反斜杠结尾"归组，哈希行就不会被当成下一条包声明。
    """
    current = []
    start = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not current:
            start = lineno
        current.append(line)
        if line.endswith("\\"):
            continue
        yield start, " ".join(part.rstrip("\\").strip() for part in current)
        current = []
    if current:  # 文件末尾缺了收尾行也要判，不能吞掉
        yield start, " ".join(part.rstrip("\\").strip() for part in current)


def _parse_lock(path: Path) -> tuple:
    """返回 ({归一化包名: (版本, 带引号的行号描述)}, [问题])。"""
    pins = {}
    problems = []
    text = path.read_text(encoding="utf-8-sig")

    head = "\n".join(text.splitlines()[:5])
    missing_tokens = [token for token in _REQUIRED_HEADER_TOKENS if token not in head]
    if missing_tokens:
        problems.append(
            "锁文件头缺少生成命令的标记 " + str(missing_tokens)
            + "：这份锁不是按跨平台 + 带哈希编出来的（头部读到 " + repr(head[:200]) + "）")

    for lineno, joined in _logical_entries(text):
        hashes = _HASH_RE.findall(joined)
        body = _HASH_RE.sub(" ", joined).strip()
        try:
            req = Requirement(body)
        except InvalidRequirement as exc:
            problems.append(f"锁第 {lineno} 行解析失败：{body!r}（{exc}）")
            continue
        version, problem = _exact_version(req, f"锁第 {lineno} 行")
        if problem:
            problems.append(problem)
            continue
        if not hashes:
            problems.append(f"锁第 {lineno} 行的 {req.name} 没有形状合法的 sha256 哈希："
                            "`--require-hashes` 会因此装不上，或哈希根本没被执行")
        canonical = _canonical(req.name)
        if canonical in pins:
            problems.append(f"锁第 {lineno} 行重复出现 {req.name}（第一次在 {pins[canonical][1]}）")
            continue
        pins[canonical] = (version, f"第 {lineno} 行")
    if not pins:
        problems.append("锁里一条包声明都没解析出来：检查文件被清空或读法失效")
    return pins, problems


def check(manifest: Path = DEFAULT_MANIFEST, lock: Path = DEFAULT_LOCK) -> list:
    """返回问题清单；空表 = 一致。"""
    for path, what in ((manifest, "清单"), (lock, "锁")):
        if not path.is_file():
            return [f"{what}文件不存在：{path}"]
    manifest_pins, problems = _parse_manifest(manifest)
    lock_pins, lock_problems = _parse_lock(lock)
    problems.extend(lock_problems)

    for canonical, (name, version, lineno) in sorted(manifest_pins.items()):
        if canonical not in lock_pins:
            problems.append(f"清单第 {lineno} 行的 {name}=={version} 在锁里找不到")
            continue
        lock_version, where = lock_pins[canonical]
        try:
            same = Version(lock_version) == Version(version)
        except InvalidVersion:
            same = lock_version == version
        if not same:
            problems.append(f"{name} 版本分家：清单要 {version}（第 {lineno} 行），"
                            f"锁里是 {lock_version}（{where}）")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="检查 backend/requirements.txt 与锁是否同版本")
    parser.add_argument("manifest", nargs="?", default=str(DEFAULT_MANIFEST))
    parser.add_argument("lock", nargs="?", default=str(DEFAULT_LOCK))
    args = parser.parse_args(argv)

    manifest, lock = Path(args.manifest), Path(args.lock)
    problems = check(manifest, lock)
    if problems:
        print(f"[FAIL] {manifest} 与 {lock} 不一致：", file=sys.stderr)
        for problem in problems:
            print("  - " + problem, file=sys.stderr)
        return 1
    print(f"[ok] {manifest.name} 的每条精确钉版都在 {lock.name} 里同版本出现且带 sha256 哈希")
    return 0


if __name__ == "__main__":
    sys.exit(main())
