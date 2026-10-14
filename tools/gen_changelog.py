#!/usr/bin/env python3
"""把 `docs/releases/*.md` 渲染成 `backend/app/web/site/changelog.json`（官网更新日志页的数据源）。

## 为什么需要这一步（而不是让页面去读 docs/）

服务端上线形态是 PyInstaller onedir 整包更换，`run_backend.spec` 的 `datas` 只收
`backend/app/web/{static,admin,site}`——**`docs/` 不进 EXE**。所以线上那份包读不到
`docs/releases/*.md`，官网「更新日志」这一页必须在打包时就把内容烙进一个随包走的产物里。

发版说明的唯一真相仍然是 `docs/releases/*.md`；`changelog.json` 只是它的**投影**。
两者漂移由 `backend/tests/test_changelog_page.py` 那一把锁当场抓住：改了 md 忘了重新
生成，测试就红——而不是页面上悄悄少一段。

## 用法

    python tools/gen_changelog.py            # 写入 site/changelog.json
    python tools/gen_changelog.py --check     # 只比对，不写（CI/测试用）

退出码：0 = 一致（或已写入）；1 = `--check` 下产物与 md 不同步。
"""
import argparse
import json
import os
import re
import sys

# 每版发版说明的标题形状：`# v0.33.1 更新说明` / `# v0.14（2026-09-20）`。
_TITLE_RE = re.compile(r"^#\s+(.*)$")
# 小节标题：`## 修复了什么`。
_SECTION_RE = re.compile(r"^##\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^(\d+)\.\s+(.*)$")
_VERSION_RE = re.compile(r"^v([0-9]+(?:\.[0-9]+)*)$")


def _version_key(version: str):
    return [int(part) for part in version.split(".")]


def _join_block(block):
    """把一条被硬换行折成多行的条目拼回一行。

    为什么不是简单 `"".join`：中文正文里换行处两半通常紧挨着（拼空串对），但英文/代码
    被折行时两半之间需要一个空格，否则 `sha256` 会被粘成 `sha 256`。判据是"前一行末尾
    与后一行开头都是 ASCII 字母数字才补空格"，其余一律拼空串。
    """
    out = ""
    for line in block:
        piece = line.strip()
        if not piece:
            continue
        if out and (out[-1].isascii() and out[-1].isalnum()
                    and piece[0].isascii() and piece[0].isalnum()):
            out += " "
        out += piece
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def parse_release(text: str, version: str) -> dict:
    """把一份发版说明切成 {version, title, intro, sections:[{heading, items}]}。

    保持文档顺序：新版在最前是排序那一步的事，这里只忠实搬运。
    """
    title = ""
    intro_blocks, blocks, headings = [], [], []
    current = None            # None = 还没进任何 `##` 小节（正文的引言区）
    for raw in text.splitlines():
        m_title = _TITLE_RE.match(raw)
        if m_title and not title:
            title = m_title.group(1).strip()
            continue
        m_sec = _SECTION_RE.match(raw)
        if m_sec:
            headings.append(m_sec.group(1).strip())
            blocks.append([])
            current = len(blocks) - 1
            continue
        if not raw.strip():
            if current is not None:
                blocks[current].append(None)      # 段落分隔
            else:
                intro_blocks.append(None)
            continue
        if current is None:
            intro_blocks.append(raw)
        else:
            blocks[current].append(raw)

    def items_of(lines):
        out, buf = [], []
        for line in lines:
            if line is None:
                if buf:
                    out.append(_join_block(buf))
                    buf = []
                continue
            m_bullet = _BULLET_RE.match(line)
            m_num = _NUMBERED_RE.match(line)
            if m_bullet:
                if buf:
                    out.append(_join_block(buf))
                buf = [m_bullet.group(1)]
            elif m_num:
                if buf:
                    out.append(_join_block(buf))
                buf = [m_num.group(2)]
            else:
                buf.append(line)
        if buf:
            out.append(_join_block(buf))
        return [item for item in out if item]

    sections = []
    for idx, heading in enumerate(headings):
        items = items_of(blocks[idx])
        if items:
            sections.append({"heading": heading, "items": items})
    intro = _join_block([b for b in intro_blocks if b is not None])
    return {"version": version, "title": title or ("v" + version),
            "intro": intro, "sections": sections}


def load_releases(docs_dir: str) -> list:
    """读 docs/releases 下每一份 v<版本>.md，按版本从新到旧排好。"""
    releases = []
    for name in sorted(os.listdir(docs_dir)):
        m = _VERSION_RE.match(os.path.splitext(name)[0])
        if not m or not name.endswith(".md"):
            continue
        with open(os.path.join(docs_dir, name), encoding="utf-8") as fh:
            releases.append(parse_release(fh.read(), m.group(1)))
    releases.sort(key=lambda r: _version_key(r["version"]), reverse=True)
    return releases


def build(repo_root: str) -> dict:
    return {
        "generated_from": "docs/releases/*.md",
        "releases": load_releases(os.path.join(repo_root, "docs", "releases")),
    }


def render(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def output_path(repo_root: str) -> str:
    return os.path.join(repo_root, "backend", "app", "web", "site", "changelog.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="render docs/releases/*.md into site/changelog.json")
    ap.add_argument("--check", action="store_true",
                    help="只比对产物是否与 md 同步，不写盘")
    args = ap.parse_args(argv)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = render(build(root))
    path = output_path(root)
    if args.check:
        try:
            with open(path, encoding="utf-8") as fh:
                have = fh.read()
        except OSError as e:
            print("changelog-check: MISSING %s (%s)" % (path, type(e).__name__))
            return 1
        if have != text:
            print("changelog-check: STALE —— site/changelog.json 与 docs/releases/*.md 不同步，"
                  "跑一次 python tools/gen_changelog.py")
            return 1
        print("changelog-check: ok")
        return 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("changelog: wrote %s (%d bytes)" % (path, len(text.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
