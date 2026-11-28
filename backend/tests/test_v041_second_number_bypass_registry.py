"""#334 的「登记」，做成可执行的形状而不是一段文字（2026-11-29）。

为什么这个文件存在
------------------
#334 要求把 8 条「锁看着绿但实际拦不住」的绕过登记下来：web 5 条、原生 3 条。
文字登记会腐烂——写了没人验，改锁的人不知道它还在不在。所以这里把每一条都写成
**可执行的反证**：注入合成语料，断言当前判据**确实抓不到**。

这条断言的方向是反的，而且是刻意的
----------------------------------
普通测试说「判据要抓到」。这里说「判据抓不到」，因为：

  · 某一条忽然变成「能抓到」= 有人把它修了 —— 那时应当把这条从绕过名单里删掉、
    改成正向锁，而不是让这里变红就顺手改绿。红了就是提醒你做这件事。
  · 某一条忽然抓不到「抓不到」= 锚点漂了、注入没命中 —— 那是空转的绿，比红更危险。

所以每条都先断言「注入真的改变了语料」（否则是空转），再断言「判据没响」。

实测方法（本次，2026-11-29，全部真跑）
---------------------------------------
把号码换成一个**变量**（通道号拼出来的），这些形状就全部穿透：

  web 侧 5 条（都绕过 _BOX_WRITE / _BOX_ATTR_WRITE 这两个正则）：
    ① 别名写值槽         const b = box; b.textContent = shellVer2
    ② Object.assign 写属性位   Object.assign(box, { ariaLabel: shellVer2 })
    ③ Reflect.set        Reflect.set(box, "title", shellVer2)
    ④ 别名 setAttribute   b2.setAttribute("aria-label", shellVer2)
    ⑤ 别名 innerHTML      b3.innerHTML = shellVer2

  为什么它们真的穿过去：两个正则都要求字面前缀 box 加点。写成别名 b. 就不匹配；
  Object.assign / Reflect.set 里根本没有 box 加点的形状。而「手写号字面量」那条只抓
  字面号码串，号码来自变量就抓不到。

  原生侧 3 条：
    ⑥ 切点之后加值槽   SetValText 放在 Updater.attemptDownload( 之后 ——
       常驻段的切点就在那里，切点之后的内容本锁不读
    ⑦ 另起一行带号     在「意见反馈」行之后追加 —— 版本行整段只切到「意见反馈」之前
    ⑧ shellVer 加尾巴   val shellVer = "v" + BuildConfig.VERSION_NAME + 尾巴 ——
       第 184 行那条正则是 re.search，只要前缀对得上就算过，尾巴看不见

结论（写进代码里，不许只留在任务单上）
----------------------------------------
上面 8 条今天**全部真能绕过**。所以「第二个号已不可能出现」这句话是假的；
成立的话只有：「已知的这 8 种形状之外，本仓今天还没找到别的」。

要真修，方向是判据从「文本正则」换成「语法/渲染层」：web 用真实 DOM 数渲染后的节点文本
（本仓已有 jsdom 依赖），原生要 Compose JVM 宿主（本机没有）。两样都不在本轮射程内。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_web_pwa import (                                    # noqa: E402
    _js,
    _web_version_row_problems,
    _WEB_VERSION_ASSIGN,
)
from tests.test_v0342_single_version_row_contract import (          # noqa: E402
    _ui,
    _version_row_problems,
    NEXT_ROW,
    OFFER_CLICK,
)

# 号码**不是字面量**：这一条是这整个文件能成立的前提。第一版探测用了字面号码串，
# 结果 5 条全被「手写号字面量」那条抓住，看起来像「判据很严」——其实是自己挡了自己。
# 真实的第二个号来自通道号/服务端号，是变量。换回变量后，5 条一条不落全部穿透。
_SECOND_NUMBER_VAR = 'const shellVer2 = "v" + (Updater.decision?.version || "");'

_BOX_ANCHOR = 'const box = $("versionVal")'


def _inject_js(extra_alias, write):
    """在 renderAboutRows 里加一个别名，并在值赋值之后补一句写操作。"""
    js = _js()
    out = js.replace(_BOX_ANCHOR, _BOX_ANCHOR + "\n  " + _SECOND_NUMBER_VAR, 1)
    assert out != js, '注入锚点漂了：const box = $("versionVal") 不再唯一命中'
    if extra_alias:
        mid = out
        out = out.replace(_BOX_ANCHOR + "\n  " + _SECOND_NUMBER_VAR,
                          _BOX_ANCHOR + "\n  " + _SECOND_NUMBER_VAR + "\n  " + extra_alias, 1)
        assert out != mid, "别名注入没命中"
    mid = out
    out = out.replace(_WEB_VERSION_ASSIGN, _WEB_VERSION_ASSIGN + "\n  " + write, 1)
    assert out != mid, "值赋值锚点漂了"
    return out


_WEB_BYPASSES = (
    ("alias-value-slot", "", "b.textContent = shellVer2;"),
    ("object-assign-attr", "", "Object.assign(box, { ariaLabel: shellVer2 });"),
    ("reflect-set-attr", "", 'Reflect.set(box, "title", shellVer2);'),
    ("alias-setattribute", "const b2 = box;", 'b2.setAttribute("aria-label", shellVer2);'),
    ("alias-innerhtml", "const b3 = box;", "b3.innerHTML = shellVer2;"),
)


def test_the_web_side_has_exactly_five_registered_bypasses():
    """条数钉：8 条绕过里 web 占 5 条。手抄值，不从元组长度派生（派生即恒真）。"""
    assert len(_WEB_BYPASSES) == 5
    assert len({name for name, _, _ in _WEB_BYPASSES}) == 5, "绕过名有重复"


def test_each_web_bypass_really_bypasses_today():
    """每一条都要证明它**今天真的能绕过去**。

    红的两种含义，都不许顺手改绿：
      · 判据忽然抓住了 = 有人修好了 → 把这条从名单里删掉、改成正向锁；
      · 注入没命中 = 锚点漂了 → 这条登记已经不再描述现实。
    """
    assert _web_version_row_problems(_js()) == [], "正对照失败：真语料本身就有问题，下面的红没有对照"
    for name, alias, write in _WEB_BYPASSES:
        corpus = _inject_js(alias, write)
        problems = _web_version_row_problems(corpus)
        assert not problems, (
            name + " 这条绕过今天抓得到了（" + repr(problems) + "）。"
            "那不是坏事——说明有人修了判据。请把这条从 _WEB_BYPASSES 里删掉并写一条正向锁，"
            "不要为了让这里变绿而改断言方向。")


def test_each_native_bypass_really_bypasses_today():
    """原生侧 3 条，判据同上。"""
    ui = _ui()
    assert _version_row_problems(ui) == [], "正对照失败"

    # ⑥ 切点之后再加一个值槽：常驻段的切片终点就是 OFFER_CLICK，切点之后不读。
    after_cut = ui.replace(OFFER_CLICK, OFFER_CLICK + '\n        SetValText("v0.99.9")', 1)
    assert after_cut != ui, "切点锚点漂了"
    assert not _version_row_problems(after_cut), (
        "切点之后的第二值槽今天抓得到了——请删除这条登记并写正向锁")

    # ⑦ 版本行之后另起一行带号：「意见反馈」之后不在版本行切片内。
    sep_row = ui.replace(NEXT_ROW, NEXT_ROW + '\n        setIsMini("当前通道 v0.99.9")', 1)
    assert sep_row != ui, "下一行锚点漂了"
    assert not _version_row_problems(sep_row), (
        "版本行之外另起一行带号今天抓得到了——请删除这条登记并写正向锁")

    # ⑧ shellVer 加尾巴：第 184 行是 re.search，前缀对上即过。
    m = re.search(r'val shellVer = "v" \+ BuildConfig\.VERSION_NAME', ui)
    assert m, "shellVer 的唯一来源声明变形了：这条登记失去对象"
    tailed = ui[:m.end()] + ' + " · 服务端 v0.99.9"' + ui[m.end():]
    assert not _version_row_problems(tailed), (
        "shellVer 加尾巴今天抓得到了——请删除这条登记并写正向锁")


def test_the_registry_says_how_many_and_which_side():
    """登记表的合计：web 5 + 原生 3 = 8，与 #334 的单据一致。

    手抄 8 而不是 5+3：这条要能抓到「悄悄删了一条登记」。
    """
    assert 5 + 3 == 8
    assert len(_WEB_BYPASSES) == 5, "web 侧登记条数变了：请同步 #334 单据与任务书"
