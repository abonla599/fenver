"""品牌位只许有一个名字：Fenver。

v0.24 把仓库名、官网、Release 标题都定成了 Fenver，可界面上还有一半写着「AI 助手」：
桌面图标、系统设置里的应用名、登录页那行渐变字、以及发给模型的那句"你运行在哪个应用
里"——全在替旧名说话。这一轮把品牌位统一收掉，并留这一条锁：以后再加界面元素时，
不许把旧品牌名带回来，也不许三个表面各写各的。

两件刻意不做的事：

① **不扫 docs/、.github/ 与 android/（退役的 WebView 壳）**。那些地方出现的旧名要么是
   历史记述，要么是"改名前那批发布"的事实描述（旧壳写死的那个资产名就是历史事实），
   改掉反而说不清发生过什么。

② **不碰品类描述**。官网上那句「跑在你自己服务器上的 AI 智能助手」答的是"这是个什么
   东西"，不是品牌位；test_website.py 里已经有一条锁专门守着那句描述不许被品牌名顶掉。
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 旧品牌字面量的三种写法。留着这份名单而不是只查一种：改名那天最容易漏的恰恰是
# 紧凑写法（manifest 的 short_name 就是 "AI助手"，中间没有空格）。
OLD_BRAND = ("AI 智能助手", "AI 助手", "AI助手")


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_brand_slots_all_say_the_same_one_name():
    """四处品牌位（浏览器标签、网页版顶栏与安装名、手机桌面图标名）必须是同一个字符串。

    这条比"值等于 Fenver"更重要：它们各自写死在各处，改一处忘一处时界面会长成
    "同一个产品三个名字"，而没有人会报错。
    """
    title = re.search(r"<title>([^<]+)</title>", _read("backend/app/web/static/index.html")).group(1)
    manifest = json.loads(_read("backend/app/web/static/manifest.webmanifest"))
    app_name = re.search(r'<string name="app_name">([^<]+)</string>',
                         _read("android-native/app/src/main/res/values/strings.xml")).group(1)
    values = (title, manifest["name"], manifest["short_name"], app_name)
    assert set(values) == {"Fenver"}, f"品牌位不再是同一个名字：{values}"


def test_the_brand_word_shown_in_the_ui_is_fenver():
    """页面顶栏、登录页品牌字、安卓侧两处渐变字：渲染出来的那三个字面量都是新品牌。

    这里查的是**字面量本身**，不是"文件里含 Fenver"——顶栏与品牌字是两处独立硬编码，
    漏掉哪一处都是用户天天看见的东西。
    """
    html = _read("backend/app/web/static/index.html")
    # 顶栏与登录页品牌字是两处独立硬编码：数个数而不是数"含不含"，漏掉哪一处都红。
    assert html.count("<span>Fenver</span>") == 2, \
        f"网页版品牌位（顶栏 + 登录页）应当有两处：{html.count('<span>Fenver</span>')}"

    native = ROOT / "android-native" / "app" / "src" / "main" / "java" / "xyz" / "fenever" / \
        "assistant" / "nativeapp"
    gradient = []
    for name in ("ui/SessionListUi.kt", "ui/AuthUi.kt"):
        text = (native / name).read_text(encoding="utf-8")
        gradient += re.findall(r'GradientText\("([^"]+)"', text)
    assert gradient and set(gradient) == {"Fenver"}, f"壳里的渐变字品牌不唯一：{gradient}"


def test_the_accessibility_label_and_the_system_hint_name_the_app():
    """无障碍描述与"去系统设置里搜哪个应用"那两句指引：说的必须是现在这个名字。

    contentDescription 是读屏软件念给用户听的；那两句指引是把人往系统设置里送。
    两处写着旧名时，看不见的人与找不到开关的人都得靠猜。
    """
    theme = _read("android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/theme/Theme.kt")
    assert 'contentDescription = "Fenver"' in theme, "读屏念出来的还是旧应用名"
    for rel in ("android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/SettingsUi.kt",
                "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/ui/UpdateUi.kt"):
        text = _read(rel)
        assert "AI 助手" not in text, f"{rel} 里还有让人去系统里搜旧名的指引"
    assert "请到系统设置里搜「Fenver」" in _read(
        "backend/app/web/static/app.js"), "网页版那两句指引没跟上品牌名"


def test_the_model_is_told_which_app_it_runs_in():
    """环境锚点：模型被问"这个软件叫什么/什么版本"时靠的就是这一句。

    锚点里写旧名，模型就会一口咬定自己叫「AI 助手」——那不是幻觉，是我们给的事实。
    """
    assert "你运行在「Fenver」应用内" in _read("backend/app/pipeline.py")


def test_no_old_brand_literal_survives_on_a_user_facing_surface():
    """六份用户可见文件里不许再出现旧品牌的任何一种写法。

    名单是点名的，不是全仓扫：docs 与退役壳里那些旧名是历史事实（见文件头②），
    把它们一起扫掉只会让文档开始说谎。
    """
    surfaces = ("backend/app/web/static/index.html", "backend/app/web/static/app.js",
                "backend/app/web/static/manifest.webmanifest",
                "backend/app/web/admin/index.html", "backend/app/web/site/probe.html",
                "backend/app/pipeline.py")
    for rel in surfaces:
        text = _read(rel)
        for lit in OLD_BRAND:
            assert lit not in text, f"{rel} 里还留着旧品牌字面量：{lit}"
