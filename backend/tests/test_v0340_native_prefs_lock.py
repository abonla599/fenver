# backend/tests/test_v0340_native_prefs_lock.py
"""v0.34.0 判据 4 与 11 的原生半边：安卓那一侧哪些东西落在哪儿，源码级就能定生死。

为什么单独一份文件、而且**不许挂回那份读壳源码的锁文件**（判据 11 原文点名
`test_android_shell.py`，2026-10-08 壳退役后它的上线半边住在
`test_apk_release_discipline.py` 与 `test_native_android_discipline.py`）：老的那一份读的是
`android/`（正在退役的 WebView 壳），壳一删它就跟着消失，本地存储纪律会从"盯错对象"
变成"没人盯"。这里读的是发版管线真正出包的那一个：`android-native/app/src/main`。

本机没有 Android SDK 也没有 gradlew，跑不了 Robolectric/JVM 单测，所以这一份全是
**读源码**的锁。它们的价值不在于证明 Kotlin 能编译（那由 CI 的 build-native-apk.yml
负责），而在于把三件只能靠文字钉死的事钉住：

1. 续命凭据（device_secret）只进 Keystore 加密区，一次都不许经过明文 `SharedPreferences`；
   加密区建不起来时的降级方向必须是"这台设备没有静默重登"，**不是**"退回明文存"。
2. 口令（password/passwd/pwd/passphrase）永远不落本地——加密保存也不行（判据 11 明写
   "也不许'加密保存'"）。所以这一条扫的是"写本地存储"的那些行，不是"提到 password
   这个词"的那些行：登录请求的形参、密码输入框的遮蔽开关，都合法且必须合法。
3. 阶梯的判据**顺序**与"绝不重试超过 1 次"：判据 9 的原生半边（有流在跑就不动手）和
   401 之后只重打一枪，都是"形状合规、行为致命"的那类——删掉一行代码仍然像一份阶梯，
   症状却是长答往外冒字的时候令牌被换走、或者一次 401 变成对着限速器的连打。

语料一律先剥注释：注释里出现"口令""secret"不算行为，把一行注掉也不等于把那处调用删了。
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NATIVE_MAIN = REPO_ROOT / "android-native" / "app" / "src" / "main"
NATIVE_SRC = NATIVE_MAIN / "java" / "xyz" / "fenever" / "assistant" / "nativeapp"
BUILD_GRADLE = REPO_ROOT / "android-native" / "app" / "build.gradle"

# 判据 11 要搜的是"口令"，不是"凭据"这一大类：令牌（token）本版仍留在明文条目里，
# 那是 PRD 第三节明写、由改动面收口决定的一件事，不属于这条锁的红线。
PASSWORD_WORDS = r"(?:password|passwd|passphrase|pwd)"

pytestmark = pytest.mark.skipif(
    not NATIVE_SRC.is_dir(),
    reason="android-native 模块不在这台机器上（工作副本不完整）",
)


def _strip_kt_comments(src: str) -> str:
    """去掉 `//` 与 `/* */`，但【尊重字符串字面量】。

    自己实现而不 import 那把共用尺子（test_apk_release_discipline 的同名函数）：判据 11
    点名要拆掉的正是那条依赖（当时它住在壳的锁文件里，会随 `android/` 一起消失）。
    算法同源，两处注释掉的都是同一件事。

    不尊重字符串就毁在自家门口：`baseUrl` 校验里的 "https://" 会被当注释整段切掉，
    于是拿剥完的源码去数 URL 形状的锁会永远读不到、永远绿着。
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


def _kt_paths() -> list[Path]:
    return sorted(p for p in NATIVE_SRC.rglob("*.kt"))


def _code(name: str) -> str:
    """某个文件剥注释后的正文（按文件名取，路径只有一份事实来源）。"""
    hit = next(p for p in _kt_paths() if p.name == name)
    return _strip_kt_comments(hit.read_text(encoding="utf-8"))


def _corpus() -> str:
    return "\n".join(_strip_kt_comments(p.read_text(encoding="utf-8")) for p in _kt_paths())


# ---------- 尺子本身的健康：负向锁最怕"读不到所以永远绿" ----------

def test_the_corpus_has_no_constructs_this_stripper_cannot_see():
    """Kotlin 的三引号原始字符串会骗过这把尺子，所以先钉死"语料里没有它"。

    这条不是形式主义：解析器在第一个 `"` 就闭合、第二个起开一段新串，注释标记会因此
    被吞或被造出来。今天 android-native 一个 `\"\"\"` 都没有（实测），哪天有人加了原始
    字符串，这条要红着提醒"先把尺子升级了再谈锁"，而不是让下面每一条负向锁悄悄变绿。
    """
    for p in _kt_paths():
        raw = p.read_text(encoding="utf-8")
        assert '"""' not in raw, f"{p.name} 里出现了三引号原始字符串：这把尺子会瞎，先修尺子"


# ---------- 判据 4：续命凭据只进加密区 ----------

def test_the_renewal_secret_lives_in_a_keystore_backed_store_and_nowhere_else():
    """`device_secret` 的落点只有 SecretStore 一个，且它自己绝不开明文通道。

    正向钉 EncryptedSharedPreferences + MasterKey；负向钉"加密区建不起来时不许退回
    getSharedPreferences"——那句 PRD 明写是备选路线（手写 Keystore 封装），不是明文 Prefs。
    """
    sec = _code("SecretStore.kt")
    assert "EncryptedSharedPreferences" in sec, "加密区没建起来：凭据就没地方落了"
    assert "MasterKey" in sec, "没有 Keystore 主密钥，EncryptedSharedPreferences 无从谈起"
    assert "getSharedPreferences" not in sec, \
        "加密区里出现 getSharedPreferences = 拉不到依赖时退回明文存，判据 4 的红线"


def test_prefs_hands_the_secret_to_the_encrypted_store_and_never_writes_it_itself():
    """明文偏好区里不许出现任何名字带 secret 的键；Prefs 只能"转交"，不能"落盘"。

    两条一起才够：只查 putString 会漏掉把值拼进 identities JSON 的那条路，只查文件归属
    会漏掉新增一个 var 的写法。所以这里同时钉：
      - 全模块（除 SecretStore.kt）没有任何 `putString("...secret...")`；
      - Prefs.kt 里每一处 `device_secret` 都必须出现在调用 SecretStore.* 的那一行上。
    """
    for p in _kt_paths():
        if p.name == "SecretStore.kt":
            continue
        body = _strip_kt_comments(p.read_text(encoding="utf-8"))
        for m in re.finditer(r'putString\(\s*"[^"]*"', body, re.I):
            assert not re.search(r"secret", m.group(0), re.I), \
                f"{p.name} 往明文偏好区写了名字带 secret 的键：{m.group(0)}"
    prefs = _code("Prefs.kt")
    for line in prefs.splitlines():
        if "device_secret" in line:
            assert "SecretStore." in line, \
                f"Prefs 里出现了不经 SecretStore 的 device_secret：{line.strip()}"


def test_the_encrypted_store_is_bound_to_the_lifecycle_of_the_local_identity_list():
    """人从本机清单上消失，那把凭据必须跟着消失——否则加密区里堆无主的免口令凭据。

    三处都对不上就是漏：登录没 save（升级白做）、忘不掉（drop 缺失）、挤掉最旧的没 drop
    （清单上限 5 个人，第 6 次登录会留一枚孤儿）、换服务器没 clear（旧地址的 secret 在
    新地址上毫无意义，却还占着设备）。
    """
    prefs = _code("Prefs.kt")
    assert prefs.count("SecretStore.save(") == 1, "登录/注册这一刻不落 secret：静默重登没接上"
    assert prefs.count("SecretStore.drop(") == 2, \
        "忘一人或挤掉最旧时得各删一次：少一处就在本机留下无主的续命凭据"
    assert prefs.count("SecretStore.clear()") == 1, "清空全部身份时不清加密区：换服务器会留旧凭据"
    assert "SecretStore.init(" in prefs, "加密区没在冷启动建起来，save 会一路静默失败"


# ---------- 判据 11：口令一个字节都不许落本地（加密保存也不许） ----------

def test_no_password_is_persisted_on_the_device_neither_plain_nor_encrypted():
    """凡是"写进本机存储"的行，都不许出现口令类词；SecretStore 里也不许。

    扫描口径是【持久化动作】而不是词频：login/register 的形参、SettingsUi 的密码输入框
    （visualTransformation）都合法，把它们算进去这条会永远红，下一个 PR 就把它当噪声拆了。
    反向也钉：判据原文连"加密保存口令"一起禁，所以专用加密区里也不许出现口令类词——
    它只装 device_secret 这一类"免口令开新会话"的凭据。
    """
    for p in _kt_paths():
        body = _strip_kt_comments(p.read_text(encoding="utf-8"))
        for line in body.splitlines():
            if re.search(r"putString\(|putInt\(|putBoolean\(|encodeToString\(", line):
                assert not re.search(PASSWORD_WORDS, line, re.I), \
                    f"{p.name} 把口令类数据写进本机存储：{line.strip()}"
        if p.name == "SecretStore.kt":
            assert not re.search(PASSWORD_WORDS, body, re.I), \
                "加密区里出现口令 = 用'加密保存'绕过判据 11，红线一样是红线"


def _data_class_fields(src: str, name: str) -> str | None:
    """从 `data class Name(` 取到配对的右括号——按深度配对，不按"第一个右括号"。

    用正则在这里是给自己埋雷：字段表和下一个 `)`（可能是别的函数的）之间隔着多少行，
    取决于这个文件今天长什么样。锁要盯的是【这个类的字段表】，配对切出来的才是它。
    """
    m = re.search(rf"data class {name}\(", src)
    if not m:
        return None
    i, depth = m.end() - 1, 0
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[m.end():i]
        i += 1
    return None


def test_the_local_identity_schema_carries_a_token_and_never_a_password():
    """落盘的数据形状本身要钉死：Identity 有 token（本版仍明文），没有口令字段。

    这条盯的是最容易绕过上一锁的那扇门：`saveIdentities` 写的是 encodeToString 的整坨
    JSON，逐行扫键名扫不到它。所以直接读 data class 的字段表。
    """
    fields = _data_class_fields(_code("Prefs.kt"), "Identity")
    assert fields is not None, "读不到 Identity 的字段表：这条锁盯的对象变了，请同步改锁"
    assert "token" in fields, "本机条目里没有 token：原生没有 Cookie 通道，登录态会当场失效"
    assert not re.search(PASSWORD_WORDS, fields, re.I), \
        "Identity 多出口令字段 = 每次登录都把口令抄进明文偏好区"


# ---------- 判据 9 的原生半边：阶梯接线与"绝不重试超过 1 次" ----------

def test_the_build_declares_the_keystore_crypto_dependency():
    """`androidx.security:security-crypto` 必须在（判据 4 的实现前提），而且不能被注释掉。

    拉不到就退回平台 Keystore 手写封装——那条退路同样不许把 secret 写回明文 Prefs，
    上面两条锁已经把它钉住了。这里只保证依赖声明是真的、生效的。
    """
    text = BUILD_GRADLE.read_text(encoding="utf-8")
    hits = [ln.strip() for ln in text.splitlines()
            if "security-crypto" in ln and not ln.strip().startswith("//")]
    assert hits, "build.gradle 没有生效的 security-crypto 依赖：EncryptedSharedPreferences 无从谈起"
    assert re.search(r"androidx\.security:security-crypto:\d+\.\d+\.\d+", hits[0]), \
        f"依赖没写版本号，CI 每次解析到的可能都不是同一个东西：{hits[0]}"


def test_the_stream_counter_is_handed_back_when_the_flow_ends_however_it_ends():
    """判据 9 的另一半：streams 的减必须写在 onCompletion 里，少一条路就变成"永不续期"。

    正常收尾 / 异常 / 上层取消（"停止"）三条路都会走 onCompletion；把它写在正文末尾就
    只有第一条算数。streamOpen 与 streamClose 必须成对，缺一边这条就红。
    """
    api = _code("Api.kt")
    assert "SessionRenew.streamOpen()" in api, "流开始不计数：判据 9 形同虚设"
    m = re.search(r"\.onCompletion\s*\{(.*?)\}\s*\.flowOn", api, re.S)
    assert m, "streamClose 不在 onCompletion 里：被停止/报错的那一轮会把计数永远留在 >0"
    assert "streamClose()" in m.group(1), "onCompletion 里减的不是 streams"


def test_the_ladder_judge_is_the_single_gate_and_the_stream_guard_comes_first():
    """判据 9 在原生这一侧的落点：动手之前先问 `shouldRenew`，而它在有流时说不。

    钉三件事的顺序：未到窗口 → 有流在跑 → `return true`。把 `streams.get() > 0` 那一句
    删掉，文本上仍是一份像样的阶梯，症状却是长答正在往外冒字时令牌被换走（老值只剩 60 秒
    命，读取循环握着的正是那一枚）——输出当场断。这一类只能靠"守卫必须在 true 之前"钉住。
    """
    renewer = _code("SessionRenew.kt")
    m = re.search(r"fun shouldRenew\(now: Long\): Boolean \{(.*?)\n    \}", renewer, re.S)
    assert m, "读不到 shouldRenew 的正文：判据顺序这条锁盯的对象变了"
    body = m.group(1)
    marks = ["remainingSeconds > THRESHOLD_SECONDS", "streams.get() > 0", "return true"]
    pos = [body.index(x) for x in marks]
    assert pos == sorted(pos), "守卫顺序倒了：到没到窗口与有没有流都排到了放行之后"
    assert re.search(r"fun renewIfDue\(\).*shouldRenew\(", renewer, re.S), \
        "renewIfDue 绕过统一判据自己攒了一套条件：两处顺序会各自漂移"


def test_the_401_self_heal_fires_from_exactly_one_place():
    """「绝不重试超过 1 次」在源码级的形状：reauthOnce 只有一处调用点。

    整模块剥注释后 `reauthOnce` 应当恰好出现两次：SessionRenew 的定义 + Api 的那一枪。
    多一处调用点就多半是"重打完再打个循环"，那正对着服务端限速器：自杀式重试吃的就是它。
    """
    corpus = _corpus()
    assert corpus.count("reauthOnce") == 2, \
        "reauthOnce 的定义与调用点之外还多了一处：续命重登开始不止一枪"
    api = _code("Api.kt")
    assert "SessionRenew.reauthOnce()" in api, "401 之后没人拿 device_secret 换新生：阶梯第三层没接上"


def test_the_renewal_only_happens_when_the_server_s_header_says_it_is_due():
    """客户端不许拿本机时钟猜到期：唯一事实来源是 X-Token-Expires-In。

    两处入口（普通 call 与元数据 GET）都必须先 noteExpiry 再判要不要动手；把 GET 那条
    删掉，症状是"界面只在拉列表时才有头条，续期窗口整个版本没被敲过一次"。
    """
    api = _code("Api.kt")
    assert api.count('SessionRenew.noteExpiry(res.header("X-Token-Expires-In")') >= 2, \
        "只有一条响应路径采到头条：另一条永远不知道该不该续"
    renewer = _code("SessionRenew.kt")
    assert "/v1/auth/renew" in renewer and "/v1/auth/reauth" in renewer, \
        "阶梯的两枪得打在服务端新给的这两个端点上"


def test_the_native_trigger_threshold_is_the_same_number_as_the_server_s():
    """7 天这个数只许有一份事实来源：拿 Kotlin 的常量算式和服务端常量对数。

    Web 侧那条边界测试钉的是 JS，这条钉 Kotlin。改窄=多敲 renew（服务端不吃，因为判据
    在服务端），改宽=令牌真的会掉：两边不同步时后者才是事故。
    """
    from app.core.auth import RENEW_ALLOWED_BELOW_SECONDS

    renewer = _code("SessionRenew.kt")
    m = re.search(r"const val THRESHOLD_SECONDS\s*=\s*([0-9L*\s]+)", renewer)
    assert m, "读不到原生续期阈值常量：这条锁盯的对象变了"
    expr = m.group(1).strip()
    assert re.fullmatch(r"[0-9]+(?:[Ll])?(?:\s*\*\s*[0-9]+(?:[Ll])?)+", expr), \
        f"阈值要写成可对的乘法式（7 天写成分秒就没人能对数了）：{expr!r}"
    value = 1
    for part in expr.split("*"):
        value *= int(part.strip().rstrip("Ll"))
    assert value == RENEW_ALLOWED_BELOW_SECONDS, \
        f"原生 {value}s vs 服务端 {RENEW_ALLOWED_BELOW_SECONDS}s：到点不该续或提前乱续"


def test_a_legacy_device_falls_to_the_login_page_instead_of_guessing():
    """升级前登录的设备本机没有 secret：读到空串直接放弃，410 时把死钥匙删干净。

    判据 4 的降级方向。少了 `secret.isEmpty()` 那一句，存量账号会在每一枪 401 上敲一次
    空 body 的 reauth；少了 410 → drop，一把已被服务端判死的钥匙会一直躺在加密区里被重试。
    """
    renewer = _code("SessionRenew.kt")
    m = re.search(r"fun reauthOnce\(\): Boolean \{(.*)", renewer, re.S)
    assert m, "读不到 reauthOnce 的正文"
    body = m.group(1)
    head = body.split("try {")[0]
    assert re.search(r"if \(secret\.isEmpty\(\)\) return false", head), \
        "空 secret 不早退：存量账号每次 401 都会打一发注定失败的 reauth"
    assert re.search(r"status == 410.*SecretStore\.drop", body), \
        "服务端说这把死了却不删：本机留着它只会对着限速器反复敲"
