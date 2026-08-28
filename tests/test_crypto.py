import base64
import hashlib
import json

from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pyugos._crypto import (
    decrypt_bytes,
    encrypt_bytes,
    encrypt_request_body,
    rsa_encrypt_base64,
    stringify_query,
)


def test_aes_gcm_round_trip():
    key = b"0123456789abcdef0123456789abcdef"
    encrypted = encrypt_bytes(key, "日本語".encode("utf-8"))
    assert decrypt_bytes(key, encrypted) == "日本語".encode("utf-8")


def test_request_hash_uses_compact_utf8_json():
    key = b"0123456789abcdef0123456789abcdef"
    body = {"path": "/home/user/写真", "recursive": True}
    result = encrypt_request_body(key, body)
    plaintext = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    assert result["req_body_sha256"] == hashlib.sha256(plaintext).hexdigest()
    assert decrypt_bytes(key, result["encrypt_req_body"]) == plaintext


def test_rsa_uses_pkcs1_v15():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encoded = rsa_encrypt_base64(private_key.public_key(), b"secret")
    assert private_key.decrypt(base64.b64decode(encoded), padding.PKCS1v15()) == b"secret"


def test_query_matches_qs_style_for_supported_values():
    assert stringify_query({"path": "/home/user/日本 語", "type": 1, "live": False}) == (
        "path=%2Fhome%2Fuser%2F%E6%97%A5%E6%9C%AC%20%E8%AA%9E&type=1&live=false"
    )
