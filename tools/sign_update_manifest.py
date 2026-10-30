"""给自托管更新清单签名（M-7 发版步骤）。

用法（在仓库根）：

    venv\\Scripts\\python.exe tools\\sign_update_manifest.py \\
        --manifest data\\version.json \\
        --key C:\\Users\\34426\\dl\\release-signing\\update-signing-key.pem

默认原地写回；`--out` 可另存。私钥只从参数/环境拿，脚本不打印私钥，只回显版本与
签名的前 16 个字符。签名覆盖的四行定义在 app/core/release_signature.py，客户端
ReleasePlan.java 用同一份口径重算。
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.release_signature import (  # noqa: E402
    PUBLIC_KEY_HEX, canonical_for_manifest, signature_shape_ok)


def _load_private_key(path: Path):
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(path.read_bytes(), password=None)


def sign_manifest(manifest: dict, private_key) -> dict:
    """返回带 signature 的新对象；不改入参。私钥类型由 cryptography 提供。"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if public_der.hex() != PUBLIC_KEY_HEX:
        raise SystemExit("这把私钥与编进 APK 的公钥不是一对，拒绝签名")
    payload = canonical_for_manifest(manifest)
    signature = private_key.sign(payload, ec.ECDSA(hashes.SHA256())).hex()
    if not signature_shape_ok(signature):
        raise SystemExit("签名形状异常，拒绝写入")
    # 落盘前自验一次：私钥/公钥对不上或算法参数漂了，别等手机装不上才发现。
    private_key.public_key().verify(bytes.fromhex(signature), payload,
                                    ec.ECDSA(hashes.SHA256()))
    out = dict(manifest)
    out["signature"] = signature
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--key", default=os.getenv("UPDATE_SIGNING_KEY", ""))
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    if not args.key:
        print("必须给 --key 或 UPDATE_SIGNING_KEY", file=sys.stderr)
        return 2
    manifest_path = Path(args.manifest)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"清单读不了：{type(e).__name__}", file=sys.stderr)
        return 2
    if not isinstance(manifest, dict):
        print("清单顶层不是对象", file=sys.stderr)
        return 2
    key = _load_private_key(Path(args.key))
    signed = sign_manifest(manifest, key)
    target = Path(args.out) if args.out else manifest_path
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(signed, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, target)
    print(f"signed {signed.get('version')} -> {target} sig={signed['signature'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
