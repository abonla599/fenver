#!/usr/bin/env python3
"""构建戳生成器：版本号唯一来源是 HEAD 上的 git tag，且必须恰好一枚服务端 tag。

旧配方 `git describe --tags --abbrev=0` 取的是"最近一枚"。tag 命名空间分家之后，
最近一枚可能是安卓线的 tag，把它烘进服务端包会让 /health 的 build 退成安卓号。
本脚本改问 `git tag --points-at HEAD`，按服务端 tag 正则过滤后**必须恰好一枚**，
否则拒绝构建：

- 0 枚 ⇒ HEAD 不在任何服务端 tag 上（从 develop 随手烤预览包正是这个形状）；
- 2 枚以上 ⇒ 同一个提交被两枚服务端 tag 指着，版本权威不唯一。

两种都 fail-closed：宁可不烘，不烘一个猜出来的号。这与 pre_tag_gate --pre-build
的「HEAD 必须就是 tag 那一笔」同向：发版流程内 tag 先于构建，自洽；流程外硬拒。
日常预览构建**不跑本脚本**：没有 version.txt 时 buildinfo 返回空串，「关于」行
只是显示不出服务端号，合法。

`--out` 必填、无默认值：version.txt 是运行时资产，带默认输出路径的脚本一次误跑
就可能覆写生产构建戳。发版配方显式传 version.txt；测试只能传临时路径。

服务端正则只 import `release_marker.FULL_TAG_RE`——再抄一份就是新的分叉源。

退出码：0 已写入；1 tag 枚数不是恰好一枚（拒绝）；2 git 读不出（fail-closed）。
"""
import argparse
import os
import subprocess
import sys

# 按脚本跑时 sys.path[0] 就是 tools/，被测试用 importlib 加载时不是，所以补一句
# （与 pre_tag_gate / gen_changelog 的 import 同一手法）。
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from release_marker import FULL_TAG_RE  # noqa: E402


def server_tags_at_head(repo_root):
    """`git tag --points-at HEAD` 按服务端正则过滤，返回 (tag 列表 | None, 错误说明)。"""
    try:
        out = subprocess.run(["git", "-C", repo_root, "tag", "--points-at", "HEAD"],
                             capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return None, "git-unreadable:%s" % type(exc).__name__
    if out.returncode != 0:
        err = (out.stderr or "").strip().replace("\n", " ")
        return None, "git-unreadable:rc=%d%s" % (
            out.returncode, (" %s" % err[:180] if err else ""))
    return [ln.strip() for ln in (out.stdout or "").splitlines()
            if FULL_TAG_RE.match(ln.strip())], ""


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="bake the server build stamp from the EXACTLY-ONE server tag at HEAD")
    ap.add_argument("--out", required=True,
                    help="output file (the release recipe passes version.txt). Required, no "
                         "default: version.txt is a runtime asset and a default path could "
                         "overwrite the production stamp on a stray run.")
    ap.add_argument("--repo-root", default=os.path.dirname(_TOOLS_DIR))
    args = ap.parse_args(argv)

    tags, why = server_tags_at_head(args.repo_root)
    if tags is None:
        print("bake-server-version: REFUSED %s —— git 读不出就证明不了 HEAD 是哪一版，不烘" % why)
        return 2
    if len(tags) != 1:
        print("bake-server-version: REFUSED token=server-tag-count expected=1 got=%d%s —— "
              "HEAD 上必须恰好一枚服务端 tag：0 枚说明 HEAD 不在服务端 tag 上（预览构建不跑"
              "本脚本），多枚说明版本权威不唯一；不烘一个猜出来的号"
              % (len(tags), (" (%s)" % " ".join(tags)) if tags else ""))
        return 1
    tag = tags[0]
    try:
        with open(args.out, "w", encoding="ascii", newline="\n") as fh:
            fh.write(tag + "\n")
    except OSError as exc:
        print("bake-server-version: REFUSED out-unwritable:%s (%s) —— 写不进输出文件就不烘"
              % (args.out, type(exc).__name__))
        return 2
    print("bake-server-version: wrote %s (%s)" % (args.out, tag))
    return 0


if __name__ == "__main__":
    sys.exit(main())