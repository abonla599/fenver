"""v0.40.1：安卓包预热工具的存在性锁（2026-11-14）。

为什么要有这条锁：用户真机反馈「点发现新版本下载很慢」。实测拆开之后，慢的不是每一次下载，
而是**每个版本发布后的第一次**——边缘手上没有这一版字节，必须经 Cloudflare 隧道把 7.9MB
从家用宽带整份拉上去（首次 5.73s）；热起来之后同一台机器只要 1.3~1.6 秒。

这个代价必须由发版流程先付掉，而不是让第一个点更新的用户付。工具本体在
tools/prewarm_apk.py，这条锁钉三件事：
① 工具在，且给得出「够不够热」的判据（退出码非零 = 没热）；
② 它认得出字节是不是要宣告的那一份（--expect-sha256）；
③ 安装部署指南的发版清单里真的有这一步——工具存在但没人跑，等于没有。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "prewarm_apk.py"
GUIDE = REPO_ROOT / "docs" / "安装部署指南.md"


def test_the_prewarm_tool_exists_and_reports_a_verdict():
    assert TOOL.is_file(), "tools/prewarm_apk.py 不见了：发版流程少了一步"
    src = TOOL.read_text(encoding="utf-8")
    assert "def main(" in src
    assert "cf-cache-status" in src, "判据必须看边缘缓存状态，而不是只看耗时"
    assert "--expect-sha256" in src, "必须能确认真的是要宣告的那一份字节"
    assert "--max-warm-seconds" in src, "没有阈值就没有判据：它得能说『没热』"
    assert "sys.exit(main())" in src, "入口要能把结论交回给调用方（非零 = 没热）"


def test_the_release_checklist_actually_runs_it():
    guide = GUIDE.read_text(encoding="utf-8")
    assert "prewarm_apk.py" in guide, \
        "发版清单里没有预热这一步：工具存在但没人跑，等于没有"
    assert re.search(r"prewarm_apk\.py[^\n]*expect-sha256", guide), \
        "清单里那一步要带上 --expect-sha256，否则热的是哪一份字节没人核"
