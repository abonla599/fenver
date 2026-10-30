"""自托管更新清单的签名口径（M-7）。

客户端拿到的 `sha256` 与下载地址来自同一份清单：能改站点的人可以同时改包和摘要。
所以清单本身要带一枚离线私钥签出来的签名，公钥编进 APK；签名只覆盖"这一版是哪个包、
多少字节、摘要是什么"这三件客户端真正会用来放行安装的事，不覆盖会随请求域名改写的
下载地址。

签名文本是固定四行（UTF-8，LF）：

    <version>\\n<apk_name>\\n<sha256>\\n<size>

Python 这一份只负责拼串与公钥常量；私钥不出现在仓库与运行进程里，签名由
`tools/sign_update_manifest.py` 在发版机用 repo 外的私钥完成。
"""
import re

# P-256 公钥的 X.509 SubjectPublicKeyInfo（DER）十六进制。
# 与 android-native/.../core/ReleasePlan.java 的 UPDATE_PUBLIC_KEY_HEX 逐字符相同，
# 由 backend/tests/test_v0390_update_signature.py 跨语言对一次。
PUBLIC_KEY_HEX = (
    "3059301306072a8648ce3d020106082a8648ce3d03010703420004"
    "6399ac26e125d62dfa40ac56f7ec257beb2acb471296a8b8d07ee52f085246c9"
    "c844fd0d7b757d3305a5fbe44b37504118ac00028149d0438b838afcb6f33b8d"
)

_SIGNATURE_RE = re.compile(r"^[0-9a-f]{132,144}$")


def canonical_payload(version: str, apk_name: str, sha256: str, size: int) -> bytes:
    """客户端会重算的那四行。少一个字段、换一种拼法都会让两边验签不一致。"""
    return f"{version}\n{apk_name}\n{sha256}\n{int(size)}".encode("utf-8")


def canonical_for_manifest(manifest: dict) -> bytes:
    return canonical_payload(
        str(manifest.get("version") or ""),
        str(manifest.get("apk_name") or ""),
        str(manifest.get("sha256") or ""),
        int(manifest.get("size") or 0),
    )


def signature_shape_ok(value) -> bool:
    """只认小写十六进制 DER 签名；形状不对的签名不该被端到手机上再判一次。"""
    return isinstance(value, str) and bool(_SIGNATURE_RE.match(value))
