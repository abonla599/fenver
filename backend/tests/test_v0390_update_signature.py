"""M-7：更新清单签名的服务端口径、跨语言公钥与发版调用点。"""

import json
import re
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core import releases
from app.core.release_signature import (
    PUBLIC_KEY_HEX, canonical_for_manifest, canonical_payload, signature_shape_ok)

REPO_ROOT = Path(__file__).resolve().parents[2]
JAVA = REPO_ROOT / "android-native/app/src/main/java/xyz/fenever/assistant/core/ReleasePlan.java"
UPDATER = REPO_ROOT / "android-native/app/src/main/java/xyz/fenever/assistant/nativeapp/update/Updater.kt"
SEED = REPO_ROOT / "backend/app/web/site/version.json"


def _hex(b: bytes) -> str:
    return b.hex()


def _sign(manifest, private_key):
    payload = canonical_for_manifest(manifest)
    return _hex(private_key.sign(payload, ec.ECDSA(hashes.SHA256())))


def _manifest():
    return {
        "version": "0.99.0",
        "apk_name": "fenver-0.99.0.apk",
        "sha256": "a" * 64,
        "size": 123456,
        "url": "https://ai.fenever.xyz/site/android.apk",
        "changelog": "v0.99.0",
    }


def test_canonical_payload_is_exactly_the_four_lines():
    assert canonical_payload("1.2.3", "fenver-1.2.3.apk", "b" * 64, 7) == (
        ("1.2.3\nfenver-1.2.3.apk\n" + "b" * 64 + "\n7").encode("utf-8"))


def test_signature_round_trip_and_tamper():
    key = ec.generate_private_key(ec.SECP256R1())
    manifest = _manifest()
    sig = _sign(manifest, key)
    assert signature_shape_ok(sig)
    key.public_key().verify(bytes.fromhex(sig), canonical_for_manifest(manifest),
                            ec.ECDSA(hashes.SHA256()))
    tampered = dict(manifest, sha256="c" * 64)
    with pytest.raises(Exception):
        key.public_key().verify(bytes.fromhex(sig), canonical_for_manifest(tampered),
                                ec.ECDSA(hashes.SHA256()))


def test_seed_manifest_carries_a_valid_signature():
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    assert signature_shape_ok(seed.get("signature"))
    public = serialization.load_der_public_key(bytes.fromhex(PUBLIC_KEY_HEX))
    public.verify(bytes.fromhex(seed["signature"]), canonical_for_manifest(seed),
                  ec.ECDSA(hashes.SHA256()))


def test_java_public_key_is_the_same_key():
    text = JAVA.read_text(encoding="utf-8")
    block = re.search(r'UPDATE_PUBLIC_KEY_HEX\s*=\s*""(.*?);', text, re.S)
    assert block, "ReleasePlan 里找不到 UPDATE_PUBLIC_KEY_HEX"
    parts = re.findall(r'"([0-9a-f]+)"', block.group(1))
    assert "".join(parts) == PUBLIC_KEY_HEX


def test_updater_requires_the_signature():
    text = UPDATER.read_text(encoding="utf-8")
    assert "ReleasePlan.decide(BuildConfig.VERSION_NAME, fetchInfo(), Prefs.baseUrl, true)" in text


def test_synthesized_release_passes_the_signature_through():
    manifest = dict(_manifest(), signature="ab" * 70)
    out = releases._synthesize_release(manifest)
    assert out["apk_signature"] == "ab" * 70
    assert out["apk_sha256"] == manifest["sha256"]


def test_manifest_shape_accepts_a_signature_and_rejects_a_bad_one():
    manifest = _manifest()
    ok, why = releases._manifest_ok(manifest)
    assert ok, why
    ok, why = releases._manifest_ok(dict(manifest, signature="AB" * 70))
    assert not ok and "signature" in why
