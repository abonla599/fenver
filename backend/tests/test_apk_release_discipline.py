"""出包与签名这条纪律上的锁。

2026-10-08 从 `test_android_shell.py` 拆出来：那一份里一半是"读 WebView 壳源码"的
形状锁（壳退役后跟着消失），另一半是**发版管线与签名密钥**的锁——它们盯的是
`.github/workflows/release-apk.yml`、`android-native/app/build.gradle`、
`tools/make-apk-keystore.ps1`，与壳在不在没有任何关系。拆文件的理由是这批锁比壳活得久。

这里还住着全仓共用的那把剥注释尺子（`_strip_java_comments` / `_code`）：Java/Kotlin
的源码形状锁不许各抄一份正则，尺子只有一把，`test_the_comment_ruler_needs_its_own_test`
钉着这件事。分文件之后它跟着上线模块走，不跟着退役的壳走。

`tools/*.ps1` 上的禁字判据不在这把尺子的射程里（`#` 与 `//`、`/* */` 不是同一种散文），它们
import `test_swap_package_contract_v0291._line_comment_free`——同样是不许另造正则的那条规矩：
只剥「整行（可带行首缩进）以 `#` 开头」那一层，行数与原文一一对应。
"""

import os
import re
import shutil
from pathlib import Path

import pytest

from tests.test_swap_package_contract_v0291 import _line_comment_free

REPO_ROOT = Path(__file__).resolve().parents[2]


def _strip_java_comments(src: str) -> str:
    """去掉 // 与 /* */ 两种注释，但【尊重字符串字面量】。

    不尊重字符串的话就毁在自家门口：ShellEvents 里那个
    `OPEN_URI_PREFIX = "assistant://open/"` 会被当成注释从 `//` 起整段切掉，
    于是凡拿剥完的源码去数 URI 形状的地方都会因为读不到而永远绿着。
    注释里的符号也不该算数：把一行注掉不等于把那处调用删了。
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == '"' or c == "'":
            quote = c
            out.append(c)
            i += 1
            while i < n:
                if src[i] == "\\" and i + 1 < n:
                    out.append(src[i:i + 2])
                    i += 2
                    continue
                out.append(src[i])
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            out.append(" ")            # 保住两侧的换行以外的分词边界
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _code(path: Path) -> str:
    return _strip_java_comments(path.read_text(encoding="utf-8"))


def _gradle_block(text: str, header: str) -> str:
    """取 `header {` 到与之配对的那个 `}` 之间的正文（按花括号深度配对，跨行算）。

    判"某一行在不在文件里"永远看不出**它写在哪儿**，而 Groovy DSL 恰恰是按位置解释的：
    同一句 storeFile 放进 signingConfigs 是对的，放进 buildTypes 是配置期就炸。
    """
    start = text.index(header)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1:i]
    raise AssertionError(f"{header} 这块没有闭合，gradle 文件本身就不对")


def _released_apk_module() -> str:
    """从 release-apk.yml 正文里抓出**真正被 assemble 的那个模块目录**。

    这条函数存在的理由：签名纪律的锁从前只读 `android/`（壳），而发版管线跑的是
    `gradle -p android-native` ——锁盯着一个不出包的模块，上线那个模块当时
    一条签名锁都没有。判据不能靠人记住"管线改过没有"，要从管线自己嘴里读。
    2026-10-08 壳退役之后，这条仍是唯一真相：管线哪天换模块，这里先红。
    """
    raw = (REPO_ROOT / ".github" / "workflows" / "release-apk.yml").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("#"))
    hits = re.findall(r"gradle\s+-p\s+(\S+)", body)
    assert hits, "release-apk.yml 里找不到 `gradle -p <模块>`：不知道谁在替我们出正式包"
    assert len(set(hits)) == 1, f"发布流同时 assemble 了多个模块：{sorted(set(hits))}"
    return hits[0]


def _shipped_gradles() -> list:
    """签名纪律要钉的 build.gradle 清单：由发版管线自己报出模块名，文件必须在。"""
    shipped = _released_apk_module()
    assert shipped == "android-native", \
        f"发版管线换模块了（现在是 {shipped}）：先确认这个模块有签名配置，再改这条锁"
    path = REPO_ROOT / shipped / "app" / "build.gradle"
    assert path.is_file(), f"上线模块 {shipped} 没有 app/build.gradle：签名锁在空转"
    return [(shipped, path.read_text(encoding="utf-8"))]


def test_released_apks_are_signed_with_one_pinned_key():
    """发出去的每个包必须签在**同一把**密钥上——这条为一次真实的"更新失败"而写。

    Android 判能不能覆盖安装看的是签名，不看版本号。发布流之前跑的是 assembleDebug，
    而每台 GitHub runner 都会现造一把 debug keystore：实测 v0.15 与 v0.16 两个 Release
    的证书 SHA-256 指纹不同（60f1ded9… / 8de2fbe3…），所以用户点"检查更新"永远装不上，
    只能卸载重装。debug 签名还带 android:debuggable=true——任何能连 adb 的人都能读出
    客户端存的会话令牌。

    所以这里钉三件：① 发布流不许再出 debug 包；② 密钥从 Secrets 来、从环境变量读，
    仓库里不许躺任何密钥文件；③ 缺 secret 时必须**失败**，不许悄悄退回现造一把新钥匙
    ——那等于再给用户制造一次"必须卸载重装"，而这件事一旦发生就收不回来。
    """
    raw_workflow = (REPO_ROOT / ".github" / "workflows" / "release-apk.yml").read_text(encoding="utf-8")
    # 判 YAML 的正文，不判注释：这条锁要禁的字符串（assembleDebug）正是注释里
    # 解释"为什么禁它"时用到的那个词——拿原文去 grep，锁会因为它自己说的话而红。
    workflow = "\n".join(ln for ln in raw_workflow.splitlines() if not ln.lstrip().startswith("#"))

    for forbidden in ("assembleDebug", "apk/debug/"):
        assert forbidden not in workflow, f"发布流又回到 {forbidden}：每次一把新 debug 钥匙，老用户永远装不上"
    assert "assembleRelease" in workflow, "发布流不再出 release 包了"
    assert "secrets.APK_KEYSTORE_BASE64" in workflow, "签名密钥不再来自 Secrets"
    assert 'if [ -z "${KEY_B64:-}" ]' in workflow and "exit 1" in workflow, (
        "缺 secret 时没有停下来：宁可发布失败，也不该发一把新钥匙签的包")
    assert "keytool" in workflow, "不再把签进包里的指纹打进日志——下次查签名问题又要靠回忆"

    # 2026-10-08 重挂：这里从前只读 `android/app/build.gradle`（壳），而发版管线
    # `gradle -p android-native ... assembleRelease` 出的是原生模块——也就是说
    # **真正签给用户那个包的签名配置，一条锁都没有**。现在按管线自己报的模块来钉。
    gradles = _shipped_gradles()
    assert any(mod == "android-native" for mod, _ in gradles), \
        "上线模块的 build.gradle 没被钉住：这条锁又变成盯着不出包的那个模块空转"
    for mod, gradle in gradles:
        # 只问"这些名字在文件里出现过吗"是不够的——2026-10-08 变异验证实测：把
        # signingConfigs 里的 `System.getenv("APK_KEYSTORE")` 改成读一个不存在的
        # 变量名，这条锁仍然是绿的，因为 buildTypes 里那句
        # `if (System.getenv("APK_KEYSTORE"))` 把整个文件的substring判据救活了。
        # 于是把它收进 signingConfigs 那一格里判：密钥四项必须**在配密钥的地方**读环境变量。
        signing = _gradle_block(_strip_java_comments(gradle), "signingConfigs")
        for need in ('System.getenv("APK_KEYSTORE")',
                    'System.getenv("APK_KEYSTORE_PASSWORD")',
                    'System.getenv("APK_KEY_ALIAS")',
                    'System.getenv("APK_KEY_PASSWORD")'):
            assert need in signing, f"{mod} 的 signingConfigs 里没有 {need}：密钥不再从环境变量读"
        for leak in ("storeFile file(\"release.jks\")", "keyPassword \""):
            assert leak not in gradle, f"{mod} 的 gradle 里出现了写死的密钥材料：{leak}"


def test_signature_guard_compares_against_a_previous_release_that_has_an_apk():
    """签名守卫的参照必须是最近一个**带 APK**的正式版，不能是只换服务端的那一版。

    2026-10-22 v0.35.4 实测：v0.35.1–v0.35.3 全是 server-only、Release 里没有 APK，
    发布流却按"最近一个正式 Release"取到 v0.35.3，然后在"没有可下载的 .apk 资产"
    处退出，整个 Release 建不出来。参照的选择条件里必须带资产过滤。
    """
    raw = (REPO_ROOT / ".github" / "workflows" / "release-apk.yml").read_text(encoding="utf-8")
    workflow = "\n".join(ln for ln in raw.splitlines() if not ln.lstrip().startswith("#"))
    assert "releases?per_page=50" in workflow, "上一版列表又缩回 20 条：连续多个 server-only 版会把带包的那版挤出窗口"
    assert "and ([.assets[]? | select(.name | endswith(" in workflow, (
        "签名守卫的上一版选择没有过滤 APK 资产：server-only 版会把守卫挡在起跑线")
    assert "length > 0))][0].tag_name" in workflow, (
        "上一版选择没有要求至少一个 APK 资产：找不到可比的包时仍会整个 Release 失败")


def test_the_signing_material_sits_in_signingconfigs_not_buildtypes():
    """签名四项 + storeType 必须待在 signingConfigs 里，buildTypes 只许引用它。

    为一次真实的发版失败而写：v0.17 第一次跑 release-apk.yml，"Restore the pinned
    signing key" 过了、"Assemble release APK" 炸了。原因是那几行被写在
    `buildTypes.release { … }` 里面——`BuildType` 没有 storeFile 这个属性，Groovy 在
    配置期就报 unknown property。上一条锁（test_released_apks_are_signed_with_one_pinned_key）
    当时是全绿的，因为它只问"这些名字在文件里出现过吗"，而它们确实出现过、只是位置错了。
    本机没有 AGP 也跑不了 gradle，所以位置只能这样钉。
    """
    for mod, raw in _shipped_gradles():
        gradle = _strip_java_comments(raw)          # 注释里也出现过 storeFile 这个词
        signing = _gradle_block(gradle, "signingConfigs")
        build_types = _gradle_block(gradle, "buildTypes")

        for prop in ("storeFile", "storePassword", "keyAlias", "keyPassword", "storeType"):
            assert prop in signing, f"{mod}：{prop} 不在 signingConfigs 里：密钥配不出签名"
            assert prop not in build_types, (
                f"{mod}：{prop} 又回到 buildTypes 里了：BuildType 没这个属性，配置期直接报错，"
                "而 CI 之前跑 assembleDebug 时这段根本不执行，所以看不见")

        release = _gradle_block(build_types, "release")
        assert re.search(r"signingConfig\s+signingConfigs\.release", release), \
            f"{mod}：release 这个 buildType 没挂上 signingConfig——签出来的包与那把钉死的密钥无关"
        assert 'storeType "PKCS12"' in signing, \
            f"{mod}：storeType 不再写死：JDK 默认值一改，gradle 就会拿 JKS 去读一把 PKCS12"

    # 两边说的是同一件事：生成脚本也得显式写 PKCS12，不能靠 keytool 的默认值。判的是剥掉整行
    # `#` 注释之后还在的那一行——脚本的注释里合法地在解释"为什么写死格式"，那句话不算实现。
    generator = _line_comment_free(
        (REPO_ROOT / "tools" / "make-apk-keystore.ps1").read_text(encoding="utf-8"))
    assert "-storetype PKCS12" in generator, \
        "生成脚本靠 JDK 默认格式，而 gradle 写死了 PKCS12——两边有一边会先漂"


def _workflow_step(name: str) -> str:
    """取 release-apk.yml 里那一步的 shell 正文（原样，不改一个字符）。"""
    import yaml

    doc = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "release-apk.yml")
                         .read_text(encoding="utf-8"))
    for step in doc["jobs"]["release"]["steps"]:
        if step.get("name") == name:
            return step["run"]
    raise AssertionError(f"发布流里没有这一步：{name}")


def _run_bash_e(script: str, env: dict):
    """按 Actions 的方式跑一段 shell：`bash -e`。

    为什么必须真跑而不是读文本：第一次写"把失败翻译成人话"那两步时忘了 Actions 默认
    带 `-e`，keytool 一非零整步就地中止，::error:: 一个都没发出去——诊断本身坏了，
    而且坏得没有任何声音（步骤是红的，annotations 是空的）。读文本看不出这件事。
    """
    import subprocess

    if not shutil.which("bash"):
        pytest.skip("这台机器上没有 bash，跑不了这段 shell")
    full = {**os.environ, **env}
    return subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True,
                          timeout=120, env=full)


MISSING = "SIGNING KEY MISSING -> 没贴上来：secret APK_KEYSTORE_BASE64"
CORRUPTED = "SIGNING KEY CORRUPTED -> 重贴 secret APK_KEYSTORE_BASE64（粘贴掉字了）"
UNREADABLE = "SIGNING KEY UNREADABLE -> 重贴 secret APK_KEYSTORE_PASSWORD / APK_KEY_ALIAS"


def test_the_key_diagnostic_reports_instead_of_just_going_red(tmp_path):
    """keytool 读不出密钥时，那一步必须把原因写成 annotation，而不是静默中止。"""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    keytool = fake_bin / "keytool"
    keytool.write_text("#!/bin/sh\n"
                       "echo 'keytool error: java.io.IOException: ' >&2\n"
                       "echo 'keystore was tampered with, or password was incorrect' >&2\n"
                       "exit 1\n", encoding="utf-8")
    keytool.chmod(0o755)

    run = _run_bash_e(_workflow_step(UNREADABLE), {
        "JAVA_HOME": fake_bin.parent.as_posix(),
        "APK_KEYSTORE": (tmp_path / "release.jks").as_posix(),
        "APK_KEYSTORE_PASSWORD": "whatever",
        "APK_KEY_ALIAS": "ai-assistant",
    })
    assert run.returncode != 0, "钥匙读不出来却成功了：这一步白加了"
    assert "::error::" in run.stdout, (
        f"没有任何 annotation，红得没有原因（多半是 -e 抢在中止前）：\n"
        f"stdout={run.stdout!r}\nstderr={run.stderr!r}")
    assert "tampered" in run.stdout, f"keytool 的原话没被搬出来：{run.stdout!r}"


def test_a_mangled_paste_is_named_as_a_paste_problem(tmp_path):
    """base64 掉一个字符时，报的必须是「粘贴坏了」，而不是往后漂成「口令不对」。"""
    import base64
    import hashlib

    payload = base64.b64encode("一把假密钥的字节".encode("utf-8")).decode()
    good = hashlib.sha256("一把假密钥的字节".encode("utf-8")).hexdigest()

    def paste(value, expected):
        """按顺序跑「落盘」与「核指纹」两步，返回第二步的结果。

        第二步读的是第一步写在 RUNNER_TEMP 里的那个文件，所以两步必须连着跑——
        单独跑第二步会因为文件不存在而红，那是测试脚手架的错，不是产品的错。
        """
        runner_temp = tmp_path / f"runner-{value[-2:]}-{expected[:6]}"
        runner_temp.mkdir(exist_ok=True)
        env_file = runner_temp / "github_env"
        env_file.write_text("", encoding="utf-8")
        env = {"RUNNER_TEMP": runner_temp.as_posix(), "GITHUB_ENV": env_file.as_posix(),
               "KEY_B64": value, "EXPECTED_SHA": expected}
        first = _run_bash_e(_workflow_step(MISSING), env)
        if first.returncode != 0:
            return first
        return _run_bash_e(_workflow_step(CORRUPTED), env)

    mangled = paste(payload[:-2] + "AA", good)
    assert mangled.returncode != 0, "指纹不一致却放行了"
    assert "::error::" in mangled.stdout and "粘贴" in mangled.stdout, mangled.stdout

    intact = paste(payload, good)
    assert intact.returncode == 0, f"指纹一致反而失败了：{intact.stdout} {intact.stderr}"

    absent = paste("", good)
    assert absent.returncode != 0 and "缺少 secret" in absent.stdout, absent.stdout


def test_each_signing_step_asserts_exactly_one_thing():
    """三步各查一件事，而且**结论写在步骤名里**——这是这套诊断能被远程读懂的唯一途径。

    未鉴权调 /actions/runs/{id}/jobs 时 `output` 整个是空的（2026-09-21 实测），
    ::error:: 的正文只有登录的人看得见。所以"哪一步红了"必须自己就是那句话：
    MISSING = 没贴、CORRUPTED = 贴坏、UNREADABLE = 口令或 alias 不对。
    一步只断言一件事，否则名字就开始说谎。
    """
    import yaml

    doc = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "release-apk.yml")
                         .read_text(encoding="utf-8"))
    steps = doc["jobs"]["release"]["steps"]
    names = [s.get("name") for s in steps]
    ordered = [MISSING, CORRUPTED, UNREADABLE]
    assert all(n in names for n in ordered), f"三步不齐：{names}"
    assert names.index(MISSING) < names.index(CORRUPTED) < names.index(UNREADABLE)
    assert names.index(UNREADABLE) < names.index("Assemble release APK"), \
        "验钥匙得排在构建之前：让 gradle 去报「签不出来」就晚了"
    for n in ordered:
        assert "->" in n and ("secret" in n or "PASSWORD" in n), f"步骤名没点名要重贴哪个：{n}"

    presence, digest, unlock = (_workflow_step(n) for n in ordered)
    assert "sha256sum" not in presence, "第一步不该顺带核指纹：那会让 CORRUPTED 那步永远轮不到红"
    assert "[ -z" not in digest and "-z \"" not in digest, "第二步不该顺带查空"
    assert "sha256sum" not in unlock and "[ -z" not in unlock, "第三步只查钥匙能不能打开"


def test_a_manual_run_can_never_publish_a_release():
    """workflow_dispatch 是"再验一次签名"的按钮，不是发版按钮。

    为什么要在意这个：重贴 secret 之后要能自己点一下就知道红不红，而不是我每试一次
    就把 v0.17 这个 tag 删了重钉一次。但 dispatch 事件里没有 tag，Release 的名字与
    包里 versionName 无从对上——所以发版那一步必须只认 tag 事件，
    而签名那几步在两种事件下都得真跑到。
    """
    import yaml

    doc = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "release-apk.yml")
                         .read_text(encoding="utf-8"))
    steps = {s["name"]: s for s in doc["jobs"]["release"]["steps"] if s.get("name")}
    guard = "startsWith(github.ref, 'refs/tags/')"

    for gated in ("Publish GitHub Release", "Check tag matches versionName"):
        assert guard in (steps[gated].get("if") or ""), \
            f"{gated} 没有只认 tag：手工触发会发出一份名字对不上版本号的公开 Release"
    for always in (MISSING, CORRUPTED, UNREADABLE, "Assemble release APK"):
        assert steps[always].get("if") is None, \
            f"{always} 被加了 if：手工触发时正好跳过要验的那几步"


def test_no_signing_key_lives_in_the_repository():
    """密钥文件一旦进了这个公开仓库，就等于把"能给他的用户发更新"的能力公开送人。

    tools/secret_scan.py 已经按扩展名挡了 .jks/.keystore，那条是"提交了会红"；
    这条是"根本没打算提交"的正向确认——两件事都写下来，下一个人才不会觉得多余。
    """
    import subprocess

    listed = subprocess.run(["git", "ls-files", "-z"], cwd=str(REPO_ROOT),
                            capture_output=True, timeout=60)
    assert listed.returncode == 0, "问不到 git，这条锁就是空的"
    tracked = [p.decode() for p in listed.stdout.split(b"\0") if p]
    bad = [p for p in tracked if p.endswith((".jks", ".keystore")) or p.endswith("keystore.properties")]
    assert not bad, f"版本库里躺着签名密钥：{bad}"


def test_the_keystore_generator_refuses_to_overwrite():
    """生成脚本必须拒绝覆盖已有的密钥。

    丢了这把钥匙没有补救办法：从此每个新包都是新签名，所有装过的人都要先卸载。
    一个"顺手再跑一次"就把这件事做掉的脚本，比没有脚本更危险。

    **射程**：这里的判据都吃 `_line_comment_free` 剥过整行 `#` 注释的那一份（母本在
    test_swap_package_contract_v0291.py，import 它、不另造正则），判的是代码行——原文里
    注释与代码长得一样，"删掉 `exit 2` 那行、注释里留着这句话"就替真代码答到了。行尾挂的注释与
    `<# ... #>` 块注释内部仍留在面上，那两种形状记在 test_v0350_lock_comment_stripping_scope 清单里的
    `ps-trailing-comment` 与 `ps-block-comment-interior`。
    """
    script = _line_comment_free(
        (REPO_ROOT / "tools" / "make-apk-keystore.ps1").read_text(encoding="utf-8"))
    assert "REFUSING TO OVERWRITE" in script, "覆盖前不再拦一道"
    assert "exit 2" in script, "拦下来却不以非零退出：脚本照样被下一步当成成功"
    assert "-validity 10000" in script, "证书有效期缩短会让未来的包签不上（Android 要求签名证书有效到 2033 之后）"


def test_the_ps1_locks_read_code_lines_not_comment_lines():
    """**成对夹具**（R9）：同一串只写在 `#` 行里 ⇒ 命中归 0；写在代码行里 ⇒ 命中。

    `.ps1` 上那族禁字判据迁到 `_line_comment_free` 之后必须仍然绿，而"仍然绿"要有证据才算数：
    这一组把两个方向各钉一枚——只有注释时判据读不到（旧写法会读到），代码行原样时读得到
    （尺子没在空转）。行数一一对应也在这里钉：那族判据判的是行号，剥注释不许改行数。
    """
    commented = "# -storetype PKCS12\n# REFUSING TO OVERWRITE\n"
    code_lines = "        -storetype PKCS12\n    Write-Output 'REFUSING TO OVERWRITE'\n"
    for token in ("-storetype PKCS12", "REFUSING TO OVERWRITE"):
        assert token in commented and token in code_lines, (
            "夹具没把这两串写进原文（这一跑作废）")
        assert token not in _line_comment_free(commented), (
            "整行 `#` 注释没被剥掉：%s 那处判据又吃回原文，删真行留注释照样绿" % token)
        assert token in _line_comment_free(code_lines), (
            "代码行也被剥掉了 ⇒ 尺子在空转，上面那句「读不到」不算证据")
    assert len(_line_comment_free(commented).splitlines()) == len(commented.splitlines()), (
        "剥注释改动了行数：所有行号判据从此与原文对不上")


def test_the_ci_test_step_names_the_lock_that_went_red(tmp_path):
    """CI 的「Run tests」必须把红的那条锁的**名字**送进一个不登录也看得见的地方。

    2026-09-21 实测两次：未鉴权调 /actions/runs/{id}/jobs 时 `output` 整个是空的，
    annotations 里只有 "Process completed with exit code 1"。于是"哪条测试红了"这个问题
    要先登录、再翻三百行日志才有答案——而这件事发生在我手机上（只想知道哪红了的时候）。
    发布流那边靠"结论写进步骤名"绕过了它；pytest 没法拆成多步，所以换个载体：
    把 FAILED / ERROR 那几行原样抄成 ::error::，它们进 annotations，
    而 /check-runs/{id}/annotations 未鉴权可读（同一轮实测）。

    这条锁**真跑**那段 shell，喂一个必定失败的假 pytest：既要它保持非零退出
    （否则红被吞掉，CI 就永远绿着骗人），又要它把测试名报出来；再反过来喂一次全绿，
    要求这一步跟着绿、且不凭空造错误标注。三种改法会被抓到：撤掉那段 echo、
    把要抄的前缀写错、把 `exit $rc` 换成 `exit 0`。

    一件它**不**能抓到的事，写下来免得这句话变成谎：那段 shell 里的 `set +e` 今天
    是保险而不是必需——管道以 tee 结尾，`bash -e` 看的是最后一个命令的退出码，
    撤掉 set +e 这一步今天照样发得出 annotation。它防的是"下一个人把 tee 拿掉"那天。
    """
    import yaml

    doc = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "tests.yml")
                         .read_text(encoding="utf-8"))
    steps = doc["jobs"]["test"]["steps"]
    script = next((s.get("run") for s in steps if s.get("name") == "Run tests"), None)
    assert script, "tests.yml 里没有「Run tests」这一步了：这条锁跟着改名一起失效"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    py = fake_bin / "python"
    py.write_text("#!/bin/sh\n"
                  'echo "...."\n'
                  'echo "FAILED backend/tests/test_demo.py::test_the_demo - AssertionError: 演示"\n'
                  'echo "1 failed in 0.01s"\n'
                  "exit 1\n", encoding="utf-8")
    py.chmod(0o755)

    run = _run_bash_e(script, {
        "PATH": fake_bin.as_posix() + os.pathsep + os.environ.get("PATH", ""),
        "RUNNER_TEMP": tmp_path.as_posix(),          # 不许往工作树里写临时文件
    })
    assert run.returncode != 0, "pytest 失败了这一步却成功了：CI 会永远绿着骗人"
    assert "::error" in run.stdout, (
        "红得没有原因——FAILED 那几行没被抄成 annotation，而未鉴权只能看见 annotations。"
        f"\nstdout={run.stdout!r}\nstderr={run.stderr!r}")
    assert "test_the_demo" in run.stdout, f"抄了，但没抄到名字：{run.stdout!r}"
    assert not list(REPO_ROOT.glob("pytest.log")), "那段 shell 把临时文件写进了工作树"

    # 反过来一遍：全绿时这一步必须跟着绿，而且不许凭空造错误标注。
    # 只测失败路径的锁会把"永远判红"这种改法放过去——那等于把 CI 变成装饰。
    py.write_text("#!/bin/sh\n"
                  'echo "784 passed in 175.10s"\n'
                  "exit 0\n", encoding="utf-8")
    ok = _run_bash_e(script, {
        "PATH": fake_bin.as_posix() + os.pathsep + os.environ.get("PATH", ""),
        "RUNNER_TEMP": tmp_path.as_posix(),
    })
    assert ok.returncode == 0, f"全绿却被判红：{ok.stdout!r} {ok.stderr!r}"
    assert "::error" not in ok.stdout, "没失败却发了错误标注：下一次没人相信它了"
