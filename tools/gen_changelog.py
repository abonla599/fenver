#!/usr/bin/env python3
"""把 `docs/releases/*.md` 渲染成 `backend/app/web/site/changelog.json`（官网更新日志页的数据源）。

## 为什么需要这一步（而不是让页面去读 docs/）

服务端上线形态是 PyInstaller onedir 整包更换，`run_backend.spec` 的 `datas` 只收
`backend/app/web/{static,admin,site}`——**`docs/` 不进 EXE**。所以线上那份包读不到
`docs/releases/*.md`，官网「更新日志」这一页必须在打包时就把内容烙进一个随包走的产物里。

发版说明的唯一真相仍然是 `docs/releases/*.md`；`changelog.json` 只是它的**投影**。
两者漂移由 `backend/tests/test_changelog_page.py` 那一把锁当场抓住：改了 md 忘了重新
生成，测试就红——而不是页面上悄悄少一段。

## 每版说明自带一个「适用范围」标记（v0.34.2 加）

官网那一页原先把 `releases[0]` 当成「最新版本」播出去，而现网有两个各自独立部署的
东西（PyInstaller 服务端整包 + 安卓 APK）：只换服务端的一版（v0.34.1）会让页面头条
写着一个**装不上**的版本号，而能下载的那一枚仍是上一版。这不是措辞问题，是把用户送去
点一个不存在的包。所以说明里带一行机器读得懂的结构：

    <!-- scope: server-only -->

写在标题之后、第一个 `##` 小节之前，一行独立注释；本脚本把它读成每条记录的
`server_only` 布尔值，`/changelog` 那页据此把「最新版本」只从**有安装包**的条目里挑，
其余条目收进「仅服务端修正（无安装包）」那一块（信息不删不藏，只是不再冒充可安装版本）。
为什么是 HTML 注释而不是正文里加粗一句：官网把说明当**文字**渲染，注释不会被渲染出来，
于是同一行既是机器读的字段，又不会变成用户看到的一句黑话。
漏标不是静默放过——`backend/tests/test_changelog_page.py` 有两条锁分别咬
「领先于 versionName 的说明没标」与「页面头条不是清单里那个可安装的版本」。

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

# 每版说明的「适用范围」标记：一行独立的 HTML 注释，写在标题与第一个 `##` 小节之间。
#   <!-- scope: server-only -->
# 认这一种形状，别的写法（正文里加粗一句「本版只换服务端」）不算——那正是 v0.34.1
# 留给人读、但没有任何机器读过的那句话。两侧空白与大小写可以松，`scope:` 后面的值必须
# 是下面两个之一；写错值不当"没标"处理而是**报错**（见 _parse_scope），因为静默回落到
# "有安装包"就是把一个装不上的版本播出去，而那正是本标记要防的事。
_SCOPE_RE = re.compile(r"^\s*<!--\s*scope:\s*([A-Za-z0-9._-]+)\s*-->\s*$")
SCOPE_SERVER_ONLY = "server-only"      # 只换服务端，没有可安装的产物
SCOPE_INSTALLABLE = "installer"        # 有可安装的产物（安卓包），也是【缺省】
_VALID_SCOPES = (SCOPE_SERVER_ONLY, SCOPE_INSTALLABLE)


def _parse_scope(text: str, version: str) -> bool:
    """这份说明声明的是"只换服务端"吗？扫标题之后、第一个 `##` 之前那一段。

    只看引言区而不是整篇：`##` 小节里的正文（比如 v0.34.1 那句"本版只换服务端"）是给
    人读的历史叙述，不该被第二次当成机器判据——一份说明有**一个**声明位置就够了，
    两处都认就会长出"到底哪一处算"的第三种真相。
    """
    for raw in text.splitlines():
        if _SECTION_RE.match(raw):
            break                                   # 进了小节，声明区已过
        m = _SCOPE_RE.match(raw)
        if not m:
            continue
        value = m.group(1).strip().lower()
        if value not in _VALID_SCOPES:
            raise ValueError(
                "docs/releases/v%s.md 的 scope 标记写的是 %r，只认 %s"
                % (version, m.group(1), " / ".join(_VALID_SCOPES)))
        return value == SCOPE_SERVER_ONLY
    return False                                    # 缺省：有安装包（历史各版都是这一形状）


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
    """把一份发版说明切成 {version, server_only, title, intro, sections:[{heading, items}]}。

    保持文档顺序：新版在最前是排序那一步的事，这里只忠实搬运。

    `server_only` 来自 `_parse_scope` 认的那一行标记，且那一行**不会**同时进 `intro`：
    官网把 JSON 里的正文当纯文字渲染，把 `<!-- scope: ... -->` 原样搬进 intro 就等于
    在页面上印一行机器用的黑话（它也不会让页面变聪明，页面读的是那个布尔值）。
    """
    title = ""
    intro_blocks, blocks, headings = [], [], []
    current = None            # None = 还没进任何 `##` 小节（正文的引言区）
    for raw in text.splitlines():
        m_title = _TITLE_RE.match(raw)
        if m_title and not title:
            title = m_title.group(1).strip()
            continue
        if _SCOPE_RE.match(raw):
            continue                                # 声明行只喂给 _parse_scope，不进正文
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
    return {"version": version, "server_only": _parse_scope(text, version),
            "title": title or ("v" + version), "intro": intro, "sections": sections}


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
    ap = argparse.ArgumentParser(
        description="render docs/releases/*.md into backend/app/web/site/changelog.json")
    ap.add_argument("--check", action="store_true",
                    help="只比对产物是否与 md 同步，不写盘")
    args = ap.parse_args(argv)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        text = render(build(root))
    except ValueError as e:
        # 一份说明写错 scope 值时必须**停下并说人话**，而不是回落到"有安装包"那一半：
        # 回落的结果是官网把只有一个域名的东西播出去，而红字里看不出任何事。
        print("changelog: BAD SCOPE —— %s" % e)
        print('changelog: 一行只认两种值：<!-- scope: server-only -->（本版没有可安装的包）'
              ' / <!-- scope: installer -->（有安装包）')
        return 1
    path = output_path(root)
    if args.check:
        try:
            with open(path, encoding="utf-8") as fh:
                have = fh.read()
        except OSError as e:
            print("changelog-check: MISSING %s (%s)" % (path, type(e).__name__))
            return 1
        if have != text:
            print("changelog-check: STALE —— backend/app/web/site/changelog.json "
                  "与 docs/releases/*.md 不同步，跑一次 python tools/gen_changelog.py")
            return 1
        print("changelog-check: ok")
        return 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("changelog: wrote %s (%d bytes)" % (path, len(text.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
