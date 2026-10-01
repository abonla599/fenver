"""v0.24 T4.1：双端「关于」页同文案（PRD D16 保留项，D17 打 tag 前置第二条）。

「关于」是一个开源项目对外的自我介绍屏，要说三件事：这是什么、代码在哪、以什么
许可证发布。网页（backend/app/web/static/index.html）与安卓原生壳
（android-native/.../ui/SettingsUi.kt）是同一款产品，这三句必须逐字同值——
手机上一套说法、浏览器里另一套说法不会报错，只会让同时看过两端的那个人不知道信哪份。

判据全在源码层（本机跑不了 Compose，也不假装跑过；观感留给真机矩阵）：
- 两端读的都是**剥掉注释的原文**（Kotlin 走 `_code`，HTML 手工去 `<!-- -->`），
  所以"只在注释里写了那句话"骗不过去；
- 原生侧的文案收在 ABOUT_* 常量里（文件顶部，同一屏只写一份），所以判两件事分开写：
  常量的值逐字等于权威串、以及这一屏确实把常量挂上了值槽——只判前一件的话，
  把那一行删掉照样不红，而界面上许可证就凭空消失了；
- 真正两端都看得见的接线（SetNote(ABOUT_TAGLINE)、"开源仓库"、onOpenUrl(ABOUT_REPO_URL)）
  单独在切片里判——常量改了名、那一行挪出这一组，都会红在这里；
- 语料定位失败一律先 assert 命中数：路径写错造成的"零命中"与"守住了"长得一模一样。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_android_shell import _code                        # noqa: E402
from tests.test_schedule_ui_contract import _native               # noqa: E402
from tests.test_web_pwa import STATIC                             # noqa: E402

TAGLINE = "Fenver：跑在你自己服务器上的个人 Agent 助理"
REPO_HOST = "github.com/abonla599/fenver"
REPO_URL = "https://github.com/abonla599/fenver"
LICENSE = "Apache-2.0"

def _code_text(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _web_raw() -> str:
    return re.sub(r"<!--[\s\S]*?-->", "", _code_text("index.html"))


def _native_raw() -> str:
    return _code(_native("nativeapp/ui/SettingsUi.kt"))


def _slice(text: str, start: str, stop: str) -> str:
    at = text.index(start)
    return text[at:text.index(stop, at)]


def _web_about() -> str:
    """网页侧「关于」那一组：组标题起、到退出卡片为止（与 test_web_pwa 的切法同一口径）。"""
    return _slice(_web_raw(), '<p class="set-group">关于</p>', "set-card set-last")


def _native_about() -> str:
    """原生侧「关于」那一组：SetGroup("关于") 起、到退出登录那一行为止。"""
    return _slice(_native_raw(), 'SetGroup("关于")', 'SetRow("→", "退出登录"')


def test_both_corpuses_are_real_ones():
    """正对照：两条切片都真拿到了内容，否则下面每一条都在空串上判、恒绿。"""
    web, native = _web_about(), _native_about()
    assert "版本" in web and "版本" in native, "连「版本」都切没了，多半是界面改动顶掉了切法"
    assert "外观" in web and "外观" in native, "同上：「外观」也是这一组的老住户"
    assert len(web) > 400 and len(native) > 400, "切片短得不像话"


def test_the_three_sentences_are_said_on_both_ends():
    """那句定位语、那个仓库地址、那张许可证，两端都要真的说出口。

    两侧的取法不同，因为两种语言里"说出口"的形状本来就不同：
    网页侧的字面量就写在这一屏的 markup 里，直接看切片；
    原生侧把它们收在 ABOUT_* 常量里（同一屏只写一份），所以判两件事——
    ① 常量的值逐字等于权威串，② 这一屏确实把常量摆上了值槽/注脚（见下面接线那条）。
    """
    web = _web_about()
    for wanted in (TAGLINE, REPO_HOST, LICENSE):
        assert wanted in web, f"网页侧「关于」没有这句：{wanted}"

    native = _native_raw()
    for const, wanted in (("ABOUT_TAGLINE", TAGLINE), ("ABOUT_REPO_HOST", REPO_HOST),
                          ("ABOUT_LICENSE", LICENSE)):
        assert f'{const} = "{wanted}"' in native, f"原生侧 {const} 的值与权威串不再逐字相等"


def test_the_native_constants_are_wired_into_the_about_card():
    """常量得真的挂在这一屏上：只定义、没人用，等于这一屏什么都没写。

    这条与上一条是一对：上一条判"值对不对"，这一条判"用没用它"。缺了它，
    把 SetValText(ABOUT_LICENSE) 删掉那一行照样不红——而界面上许可证就凭空消失了。
    """
    native = _native_about()
    assert "SetNote(ABOUT_TAGLINE)" in native, "原生侧那句定位语没挂在「关于」上"
    assert "SetValText(ABOUT_REPO_HOST)" in native, "原生侧仓库地址没挂在「关于」上"
    assert "SetValText(ABOUT_LICENSE)" in native, "原生侧许可证没挂在「关于」上"


def test_the_repository_link_is_the_project_itself():
    """网页那颗链的 href 就是仓库本身（不是 releases 那一层），且带 target=_blank。

    仓库行与「检查更新」那颗「去下载页」不是一件事：前者是"代码在哪"，后者是"包在哪"。
    """
    web = _web_about()
    assert f'href="{REPO_URL}"' in web, f"网页「关于」里没有指向 {REPO_URL} 的链"
    at = web.index(f'href="{REPO_URL}"')
    head = web[max(0, at - 200):at]          # 这一行的起始标签部分（href 之前）
    tail = web[at:at + 200]                  # 属性收尾部分（href 之后）
    assert 'class="set-row"' in head, "仓库那一行不是标准 .set-row：它会没有分隔线、没有 54px 热区"
    assert 'target="_blank"' in tail, "外链没有开新标签：点一下会把当前界面整个换掉"
    assert "noopener" in tail, "外链没有 noopener：新开的页面能反过来摸到这一页"


def test_the_row_labels_match_too():
    """行名也算文案：值一样、一个叫「开源仓库」一个叫「项目地址」，两份文档就没法写。"""
    web, native = _web_about(), _native_about()
    for label in ("开源仓库", "许可证"):
        assert label in web and label in native, f"「{label}」这一行两端不同名"


def test_the_row_order_matches():
    """行序：开源仓库 → 许可证 → 服务地址，两端同序（这一屏的行序本身就是契约）。"""
    markers = {
        "开源仓库": (f'href="{REPO_URL}"', '"开源仓库"'),
        "许可证": (">许可证<", '"许可证"'),
        "服务地址": ('id="connRow"', '"服务地址"'),
    }
    web, native = _web_about(), _native_about()
    order = ("开源仓库", "许可证", "服务地址")
    web_at = {k: web.index(v[0]) for k, v in markers.items()}
    native_at = {k: native.index(v[1]) for k, v in markers.items()}
    assert [web_at[k] for k in order] == sorted(web_at[k] for k in order), f"网页行序变了：{web_at}"
    assert [native_at[k] for k in order] == sorted(native_at[k] for k in order), f"原生行序变了：{native_at}"


def test_the_tagline_is_a_note_not_a_row_on_both_ends():
    """那句定位语在两端都是"组标题下面的一条注脚"，不是卡片里一行能点的东西。

    它说的不是"这里有个功能"，是"这一组在讲什么"。排进卡片就会多一条分隔线、
    一个 54px 拇指热区，用户会去点它。
    """
    web = _web_about()
    note = re.search(r'<p class="set-group">关于</p>\s*<p class="pane-note">([^<]*)</p>', web)
    assert note, "网页侧那句定位语不再是紧跟组标题的 .pane-note（或被挪进了卡片里）"
    assert note.group(1) == TAGLINE, f"网页侧注脚与权威串不等：{note.group(1)!r}"

    native = _native_about()
    assert re.search(r'SetGroup\("关于"\)\s*SetNote\(ABOUT_TAGLINE\)', native), \
        "原生侧那句定位语不再紧跟组标题（SetNote 被挪位或改名了）"
    assert f'ABOUT_TAGLINE = "{TAGLINE}"' in _native_raw(), "原生侧常量值与权威串不再逐字相等"


def test_no_third_party_link_was_added_to_the_about_group():
    """这一屏唯一允许的外链主机就是项目自己的仓库。

    「关于」是全应用唯一一处会大方给出外部链接的地方，也因此最好塞东西：
    明天有人加一枚"关注我们"或 CDN 图标，谁都不会觉得异常。
    """
    hosts = set(re.findall(r'href="(https?://[^"/]+)', _web_about()))
    assert hosts == {"https://github.com"}, f"关于组的外链主机不再是唯一一家：{hosts}"


def test_native_opens_the_repository_in_the_browser_not_in_app():
    """同一个卡片里两个相反的要求，所以两条都得钉：

    仓库那一行是"去看这个项目"，本来就该离开 App，走外部浏览器；
    更新那一行是"换这台机器上的安装包"，2026-09 那次劫持残包事故之后不许再把 URL 递给浏览器。
    """
    native = _native_about()
    assert "onOpenUrl(ABOUT_REPO_URL)" in native, "原生「开源仓库」那一行没有真的去开仓库"
    assert 'ABOUT_REPO_URL = "%s"' % REPO_URL in _native_raw(), "原生侧仓库地址被改了"
    assert "onOpenUrl(Prefs.baseUrl" not in native, "安装包地址又被递给外部浏览器了（D2 那条判决没作废）"
