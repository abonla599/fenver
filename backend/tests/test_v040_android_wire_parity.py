"""设备上下文的两面对照锁：服务端（Python）与设备侧（Java）必须是同一份口径。

规划书 §9.7 把 A/B 两轨并行，前提是 wire schema 冻结。冻结不等于"写在文档里"——
设备侧要把同一件事再判一遍（哪些文本算敏感、截到多长、节点数/深度/字节数的上限、
JSON 的键叫什么），两份实现漂移时**没有任何行为测试会红**：服务端只会收到一份它以为
客户端已经脱敏过的文本。

这条锁是仓库里唯一同时读得到两边的位置：

- 服务端一侧全部用**运行时内省**（{@code model_fields}、模块常量、{@code _redact_node}
  真跑一遍取键序），不拿正则去啃 Python 源码——那样改个排版就假红；
- Java 一侧只能读源码（本机不跑 Gradle）：常量、关键词表、{@code put("...")} 的键序
  都从 `core/DeviceContextPayload.java` 现扫，扫不到就报 MISSING，不许自我派生。

射程要写准：这条锁判"名字与数字对不对得上"，判不了"两边语义是否真的等价"——
后者归 Java 台架（`app/src/test/.../DeviceContextPayloadTest.java`，CI 的
`testDebugUnitTest` 会跑）与 `test_v040_device_context.py` 的行为断言各管一半。
"""
import re
from pathlib import Path

import pytest

from app.core import device_context

REPO_ROOT = Path(__file__).resolve().parents[2]
JAVA_SRC = (REPO_ROOT / "android-native" / "app" / "src" / "main" / "java"
            / "xyz" / "fenever" / "assistant" / "core" / "DeviceContextPayload.java")

# 数值常量在 Java 里可能写成表达式（128 * 1024），两边都按表达式求值后再比。
_INT_CONST = re.compile(r"public\s+static\s+final\s+int\s+(\w+)\s*=\s*([0-9*\s]+);")
_STR_CONST = re.compile(r'public\s+static\s+final\s+String\s+(\w+)\s*=\s*"([^"]*)";')


def _java_text() -> str:
    assert JAVA_SRC.is_file(), f"设备侧类不见了：{JAVA_SRC}"
    return JAVA_SRC.read_text(encoding="utf-8")


def _java_ints(src: str) -> dict:
    out = {}
    for name, expr in _INT_CONST.findall(src):
        try:
            out[name] = eval(expr)  # noqa: S307 - 只吃 [0-9*\s]，没有可执行的成分
        except SyntaxError:  # pragma: no cover - 表达式形状变了就报出来
            pytest.fail(f"Java 常量 {name} 的取值 {expr!r} 不是纯算术表达式")
    return out


def _java_sensitive_pattern(src: str) -> str:
    # 锚定 SENSITIVE 常量名，不取「第一枚 (?i)」：一枚字面量完全正确的诱饵
    # SENSITIVE_LOCK_DECOY 排在真表之前时，first-match 会读到诱饵、放过真表被抽词。
    m = re.search(r"\bSENSITIVE\b\s*=\s*Pattern\.compile\(\s*\"(\(\?i\)\([^\"]+\))\"", src)
    assert m, "Java 侧没找到敏感词表：SENSITIVE = Pattern.compile(\"(?i)(...)\"...)"
    return m.group(1)


def _keywords(pattern: str) -> list:
    """剥掉 `(?i)(` 与尾括号，按 | 切。两侧都走这一个函数，切法不会各说各话。"""
    assert pattern.startswith("(?i)(") and pattern.endswith(")"), \
        f"敏感词表的形状变了，本锁的切法要跟着改：{pattern!r}"
    return pattern[len("(?i)("):-1].split("|")


def _java_put_keys(src: str, receiver: str) -> list:
    return re.findall(re.escape(receiver) + r'\.put\(\s*"([^"]+)"', src)


# ---------------------------------------------------------------- 三字段谓词对照
# 裁决口径：package=形状闸，device_id/window_title=敏感词。键序锁只抓键名不管值
# 表达式，把 cleanText(pkg) 换成 fieldText(pkg) 不会红——这一节补的就是那个洞。
# 两个探针即可区分「形状闸 / 敏感词 / 只清洗」三种谓词：
#   com.passwordvault.app —— 合法形状 + 含敏感词（形状闸放行、敏感词替换）
#   not-a-package         —— 非法形状 + 无敏感词（形状闸替换、敏感词放行）
_FIELD_PREDICATE = {"device_id": "sensitive", "package": "shape", "window_title": "sensitive"}
_PROBE_SHAPE_OK_SENSITIVE = "com.passwordvault.app"
_PROBE_SHAPE_BAD_PLAIN = "not-a-package"
_PROBE_SENSITIVE_TEXT = "验证码 123456"
_PROBES = (_PROBE_SHAPE_OK_SENSITIVE, _PROBE_SHAPE_BAD_PLAIN, _PROBE_SENSITIVE_TEXT)

_SERVER_EXPECTED = {
    "shape": {_PROBE_SHAPE_OK_SENSITIVE: _PROBE_SHAPE_OK_SENSITIVE,
              _PROBE_SHAPE_BAD_PLAIN: device_context.REDACTED,
              _PROBE_SENSITIVE_TEXT: device_context.REDACTED},
    "sensitive": {_PROBE_SHAPE_OK_SENSITIVE: device_context.REDACTED,
                  _PROBE_SHAPE_BAD_PLAIN: _PROBE_SHAPE_BAD_PLAIN,
                  _PROBE_SENSITIVE_TEXT: device_context.REDACTED},
}
_JAVA_FIELD_HELPER = {"sensitive": "fieldText", "shape": "packageText"}
# put 值表达式不仅判助手名，还判参数名：fieldText(windowTitle) 喂给 device_id 是
# 参数调包——助手名照对、键名键序全不动，只判助手名的判据看不见（实际参数名现读 build）。
_FIELD_PUT_PARAM = {"device_id": "id", "package": "pkg", "window_title": "windowTitle"}


def _server_field_outcomes(monkeypatch) -> dict:
    """服务端一侧真 ingest 一遍取快照值（运行时行为，不啃源码排版）。"""
    monkeypatch.setenv("FENVER_DEVICE_CONTEXT_ENABLED", "1")
    store = device_context.DeviceContextStore()
    out = {}
    for field in _FIELD_PREDICATE:
        out[field] = {}
        for probe in _PROBES:
            kw = {"device_id": "probe-device", "permission": "accessibility"}
            kw[field] = probe
            payload = device_context.DeviceContextPayload(**kw)
            ok, reason, _ = store.ingest(payload, "probe-owner")
            assert ok, reason
            snap = store.lookup("probe-owner", payload.device_id)
            out[field][probe] = snap[field]
    return out


def _java_put_exprs(src: str, receiver: str) -> dict:
    """键 -> 值表达式。值里可能套一层调用括号，吃到 `);` 才算完整。"""
    return dict(re.findall(re.escape(receiver) + r'\.put\(\s*"([^"]+)"\s*,\s*(.+?)\)\s*;', src))


def _java_method_body(src: str, name: str) -> str:
    m = re.search(r"static\s+String\s+" + re.escape(name)
                  + r"\(\s*String\s+\w+\s*\)\s*\{([\s\S]*?)\n    \}", src)
    return m.group(1) if m else ""


def _java_shape_pattern(src: str) -> str:
    m = re.search(r'PACKAGE_SHAPE\s*=\s*Pattern\.compile\(\s*"([^"]+)"', src)
    assert m, "Java 侧没找到 package 形状闸：PACKAGE_SHAPE = Pattern.compile(...)"
    return m.group(1).replace("\\\\", "\\")


# 真 SENSITIVE 声明前缀与完整词表字面量（与 Java 源码逐字同；read_text 已归一为 LF）。
_SENSITIVE_DECL = "private static final Pattern SENSITIVE = Pattern.compile("
_FULL_TABLE = '"(?i)(密码|口令|验证码|动态码|卡号|身份证|password|passwd|otp|cvv|card.?no|id.?card)"'


def _decoy_then_drop(src: str, word: str) -> str:
    """在真 SENSITIVE 前插一枚字面量完全正确的诱饵，再从真表抽走 word。

    锚定常量名的键表锁必须读到被抽词的真表（与服务端不等 ⇒ 红）；旧的 first-match
    会读到排在前面的诱饵（完整词表 ⇒ 与服务端相等 ⇒ 绿），这正是 G3 的缴械路径。
    """
    dropped = _FULL_TABLE.replace("|" + word, "", 1)
    assert dropped != _FULL_TABLE, f"词表里没有 {word}，抽词变异没生效"
    s = src.replace(_FULL_TABLE, dropped, 1)          # 先抽真表（此时全文件仅一处）
    decoy = ("private static final Pattern SENSITIVE_LOCK_DECOY = Pattern.compile(\n"
             "            " + _FULL_TABLE + ",\n"
             "            Pattern.UNICODE_CASE);\n    ")
    return s.replace(_SENSITIVE_DECL, decoy + _SENSITIVE_DECL, 1)


def _java_field_predicate_problems(src: str) -> list[str]:
    problems = []
    exprs = _java_put_exprs(src, "payload")
    bodies = {h: _java_method_body(src, h) for h in _JAVA_FIELD_HELPER.values()}
    for field, pred in _FIELD_PREDICATE.items():
        helper = _JAVA_FIELD_HELPER[pred]
        param = _FIELD_PUT_PARAM[field]
        expr = exprs.get(field, "").strip()
        if not re.fullmatch(re.escape(helper) + r"\(\s*" + re.escape(param) + r"\s*\)", expr):
            problems.append(f"{field} 的 put 值不是 {helper}({param})：{expr!r}")
    for helper, matcher, other in (("fieldText", "SENSITIVE.matcher", "PACKAGE_SHAPE"),
                                   ("packageText", "PACKAGE_SHAPE.matcher", "SENSITIVE.matcher")):
        body = bodies[helper]
        if matcher not in body or "REDACTED" not in body or "cleanText(" not in body:
            problems.append(f"{helper} 本体缺清洗/判定/替换")
        if other in body:
            problems.append(f"{helper} 混进了另一套谓词 {other}")
        # 子串判据会被死代码喂饱：if(false){真脱敏} 照样含全部子串。本体必须是
        # 直白的条件脱敏——不许分支/循环，返回必须是唯一一条含判定与替换的三元表达式。
        if re.search(r"\b(if|else|switch|for|while|do)\b", body):
            problems.append(f"{helper} 本体出现分支/循环，脱敏被藏进了控制流")
        rets = re.findall(r"\breturn\b([^;]*);", body)
        if (len(rets) != 1 or "?" not in rets[0] or ":" not in rets[0]
                or matcher not in rets[0] or "REDACTED" not in rets[0]):
            problems.append(f"{helper} 的返回不是唯一的直接三元脱敏表达式")
    if _java_shape_pattern(src) != device_context._PACKAGE_RE.pattern:
        problems.append("package 形状闸正则两侧不同值")
    return problems


# 服务端那一侧：全部内省，不去啃源码排版。
SERVER_INTS = {
    "MAX_PAYLOAD_BYTES": device_context.MAX_PAYLOAD_BYTES,
    "MAX_NODES": device_context.MAX_NODES,
    "MAX_DEPTH": device_context.MAX_DEPTH,
    "MAX_TEXT_CHARS": device_context.MAX_TEXT_CHARS,
    "MAX_DEVICE_ID_CHARS": device_context.MAX_DEVICE_ID_CHARS,
    "TTL_SECONDS": int(device_context.DEVICE_CONTEXT_TTL_SECONDS),
}
SERVER_STRS = {
    "REDACTED": device_context.REDACTED,
    "NO_PERMISSION": device_context.NO_PERMISSION,
}


def test_numeric_limits_match_on_both_sides():
    """六个上限/时长逐个对。任何一侧改了数字，这里当场红。"""
    java = _java_ints(_java_text())
    missing = sorted(set(SERVER_INTS) - set(java))
    assert not missing, f"设备侧缺这些常量（名字必须与服务端同名同义）：{missing}"
    drift = {k: (SERVER_INTS[k], java[k]) for k in SERVER_INTS if java[k] != SERVER_INTS[k]}
    assert not drift, (
        "设备侧上限与服务端不一致（服务端, 设备侧）：%s\n"
        "这类漂移不会让任何行为测试变红——客户端会照旧把超限的快照发出去，"
        "服务端只会回一句 413，用户看到的是「这次没成功」" % drift)


def test_string_constants_match_on_both_sides():
    """`[已脱敏]` 与 `none` 必须逐字同值：它们会出现在 wire 上，不是内部常量。"""
    java = dict(_STR_CONST.findall(_java_text()))
    for name, value in SERVER_STRS.items():
        assert name in java, f"设备侧缺 {name}"
        assert java[name] == value, f"{name} 两侧不同值：服务端 {value!r} / 设备侧 {java[name]!r}"


def test_sensitive_keyword_table_is_the_same_ordered_list():
    """敏感词是一条条列的，不是"意思差不多"。少一条就少一类明文往外送。"""
    server = _keywords(device_context._SENSITIVE_RE.pattern)
    device = _keywords(_java_sensitive_pattern(_java_text()))
    assert device == server, (
        "敏感词表两侧不一致：\n  服务端 %s\n  设备侧 %s\n"
        "顺序与条目都要同——设备侧少一条，那类字段就以明文上传，"
        "服务端第二道虽然还会拦，但设备侧那道已经名存实亡" % (server, device))


def test_wire_key_names_and_order_match_the_server_models():
    """payload 与节点的键名、键序都要对得上：键序也是这份 schema 的一部分。"""
    src = _java_text()
    server_payload = list(device_context.DeviceContextPayload.model_fields.keys())
    server_node = list(device_context._redact_node(device_context.DeviceNode()).keys())
    assert _java_put_keys(src, "payload") == server_payload
    assert _java_put_keys(src, "m") == server_node


def test_field_predicates_match_on_both_sides(monkeypatch):
    """三字段两端谓词逐字对应：package=形状闸、device_id/window_title=敏感词。

    服务端用行为探针（真 ingest 一遍看快照值），Java 侧扫 put 值表达式与助手本体：
    put 值逐字段判助手名**与参数名**（id/pkg/windowTitle），助手本体不许分支/循环、
    返回必须是唯一一条直接三元脱敏（子串判据会被死代码掏空喂饱）。
    任一端把某字段的谓词换掉（哪怕键名键序都没动），这里当场红。
    """
    server = _server_field_outcomes(monkeypatch)
    for field, pred in _FIELD_PREDICATE.items():
        assert server[field] == _SERVER_EXPECTED[pred], (field, server[field])
    assert _java_field_predicate_problems(_java_text()) == []


def test_permission_vocabulary_matches():
    """设备侧认的权限取值不许比服务端多：多一个就是"客户端以为能发、服务端 403"。"""
    src = _java_text()
    m = re.search(r"Arrays\.asList\(([^)]*)\)", src)
    assert m, "Java 侧没找到权限取值表：Arrays.asList(...)"
    device = set(re.findall(r'"([^"]+)"', m.group(1)))
    assert device == set(device_context.VALID_PERMISSIONS), (
        "权限取值两侧不一致：服务端 %s / 设备侧 %s"
        % (sorted(device_context.VALID_PERMISSIONS), sorted(device)))


def test_device_side_class_stays_free_of_android_imports():
    """core 包必须是纯 JVM：一旦 import android.*，CI 的 testDebugUnitTest 就跑不了，
    这条对照锁也就失去了"在 JVM 上真跑一遍"的前提。"""
    src = _java_text()
    assert "import android." not in src
    assert "import androidx." not in src
    # 正控：同包的老类确实满足这条（证明判据不是"看谁都是空的"）
    for name in ("MiniJson.java", "ExportName.java"):
        neighbor = JAVA_SRC.parent / name
        assert "import android." not in neighbor.read_text(encoding="utf-8"), name


@pytest.mark.parametrize("mutate, detector, hint", [
    (lambda s: s.replace("MAX_NODES = 400;", "MAX_NODES = 300;"), "int", "MAX_NODES"),
    (lambda s: s.replace("|card.?no", ""), "kw", "card"),
    (lambda s: s.replace('payload.put("window_title"', 'payload.put("window"'), "key", "window"),
    (lambda s: s.replace('"clipboard", ', ""), "perm", "clipboard"),
    (lambda s: s.replace("public final class DeviceContextPayload {",
                         "import android.os.Build;\npublic final class DeviceContextPayload {"),
     "android", "android."),
    (lambda s: s.replace('payload.put("package", packageText(pkg))',
                         'payload.put("package", cleanText(pkg))'), "pred", "package"),
    (lambda s: s.replace('payload.put("device_id", fieldText(id))',
                         'payload.put("device_id", packageText(id))'), "pred", "device_id"),
    (lambda s: s.replace('payload.put("window_title", fieldText(windowTitle))',
                         'payload.put("window_title", cleanText(windowTitle))'), "pred", "window_title"),
    (lambda s: s.replace("return SENSITIVE.matcher(cleaned).find() ? REDACTED : cleaned;",
                         "return cleaned;"), "pred", "fieldText-body"),
    (lambda s: s.replace("PACKAGE_SHAPE.matcher(cleaned).matches()",
                         "SENSITIVE.matcher(cleaned).find()"), "pred", "packageText-body"),
    # G1：死代码掏空 fieldText（保留全部判据子串）——本体判据必须仍报红。
    (lambda s: s.replace(
        "        String cleaned = cleanText(raw);\n"
        "        return SENSITIVE.matcher(cleaned).find() ? REDACTED : cleaned;",
        "        String cleaned = cleanText(raw);\n"
        '        if (Boolean.parseBoolean("false")) {\n'
        "            return SENSITIVE.matcher(cleaned).find() ? REDACTED : cleaned;\n"
        "        }\n"
        "        return cleaned;"), "pred", "dead-code"),
    # G2：参数调包 device_id←windowTitle（助手名照对）——put 判据必须判参数名。
    (lambda s: s.replace('payload.put("device_id", fieldText(id))',
                         'payload.put("device_id", fieldText(windowTitle))'), "pred", "param-swap"),
    # G3：干净诱饵 + 真表抽走「口令」——键表锁锚定常量名才读得到被抽词的真表。
    (lambda s: _decoy_then_drop(s, "口令"), "kw", "decoy-drop"),
])
def test_parity_checks_can_go_red(mutate, detector, hint):
    """变异自证：把设备侧改坏一处，对应的那条对照必须报得出来。

    没有这一格，上面五条可以靠"永远读不到东西"一起绿——那种绿比红贵得多。
    """
    good = _java_text()
    bad = mutate(good)
    assert bad != good, "变异没生效，这一格等于空转"
    if detector == "int":
        assert _java_ints(bad)["MAX_NODES"] != SERVER_INTS["MAX_NODES"]
    elif detector == "kw":
        assert _keywords(_java_sensitive_pattern(bad)) != _keywords(
            device_context._SENSITIVE_RE.pattern)
    elif detector == "key":
        assert _java_put_keys(bad, "payload") != list(
            device_context.DeviceContextPayload.model_fields.keys())
    elif detector == "perm":
        m = re.search(r"Arrays\.asList\(([^)]*)\)", bad)
        assert set(re.findall(r'"([^"]+)"', m.group(1))) != set(
            device_context.VALID_PERMISSIONS)
    elif detector == "pred":
        assert _java_field_predicate_problems(bad), "谓词漂移没被报出来"
    else:
        assert hint in bad, "变异没落进源码里"
        assert "import android." in bad
