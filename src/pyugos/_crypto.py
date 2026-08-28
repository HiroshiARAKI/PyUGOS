"""Cryptographic wire-format helpers used by the UGOS private API."""

import base64
import hashlib
import json
import os
from typing import Any, Dict, Mapping, Tuple
from urllib.parse import quote, urlencode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import TransportError


PublicKey = rsa.RSAPublicKey


def decode_base64(value: str) -> bytes:
    """Decode padded or unpadded standard Base64."""

    compact = "".join(value.split())
    compact += "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(compact, validate=True)
    except (ValueError, TypeError) as exc:
        raise TransportError("UGOS returned invalid Base64 data") from exc


def load_rsa_public_key(value: bytes) -> PublicKey:
    """Load the PEM or DER RSA public-key formats observed in UGOS."""

    candidates = [value]
    if b"BEGIN RSA PUBLIC KEY" in value:
        candidates.append(
            value.replace(b"BEGIN RSA PUBLIC KEY", b"BEGIN PUBLIC KEY").replace(
                b"END RSA PUBLIC KEY", b"END PUBLIC KEY"
            )
        )

    for candidate in candidates:
        for loader in (
            serialization.load_pem_public_key,
            serialization.load_der_public_key,
        ):
            try:
                key = loader(candidate)
            except (ValueError, TypeError):
                continue
            if isinstance(key, rsa.RSAPublicKey):
                return key

    raise TransportError("UGOS returned an unsupported RSA public key")


def rsa_encrypt_base64(public_key: PublicKey, value: bytes) -> str:
    encrypted = public_key.encrypt(value, padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("ascii")


def json_stringify(value: Any) -> bytes:
    """Serialize like JSON.stringify for the request shapes used here."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def encrypt_bytes(key: bytes, plaintext: bytes) -> str:
    if len(key) != 32:
        raise ValueError("UGOS AES keys must contain 32 bytes")
    iv = os.urandom(12)
    ciphertext_and_tag = AESGCM(key).encrypt(iv, plaintext, None)
    return base64.b64encode(iv + ciphertext_and_tag).decode("ascii")


def decrypt_bytes(key: bytes, encoded: str) -> bytes:
    raw = decode_base64(encoded)
    if len(raw) < 28:
        raise TransportError("UGOS returned a truncated encrypted response")
    try:
        return AESGCM(key).decrypt(raw[:12], raw[12:], None)
    except Exception as exc:
        raise TransportError("Could not authenticate the encrypted UGOS response") from exc


def encrypt_request_body(key: bytes, body: Mapping[str, Any]) -> Dict[str, str]:
    plaintext = json_stringify(body)
    return {
        "encrypt_req_body": encrypt_bytes(key, plaintext),
        "req_body_sha256": hashlib.sha256(plaintext).hexdigest(),
    }


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def stringify_query(params: Mapping[str, Any]) -> str:
    """Serialize scalar parameters in the RFC 3986 form used by qs.stringify."""

    items = []
    for key, value in params.items():
        if isinstance(value, (list, tuple)):
            items.extend(("{}[{}]".format(key, index), _query_value(item)) for index, item in enumerate(value))
        else:
            items.append((key, _query_value(value)))
    return urlencode(items, doseq=False, quote_via=quote, safe="")


def encrypt_query(key: bytes, params: Mapping[str, Any]) -> Dict[str, str]:
    plaintext = stringify_query(params).encode("utf-8")
    return {"encrypt_query": encrypt_bytes(key, plaintext)}


def decrypt_json(key: bytes, encoded: str) -> Any:
    try:
        return json.loads(decrypt_bytes(key, encoded).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransportError("UGOS returned an invalid encrypted JSON response") from exc


def split_encrypted_blob(encoded: str) -> Tuple[bytes, bytes, bytes]:
    """Return IV, ciphertext, and tag; useful to protocol tests and debugging."""

    raw = decode_base64(encoded)
    if len(raw) < 28:
        raise TransportError("UGOS returned a truncated encrypted value")
    return raw[:12], raw[12:-16], raw[-16:]
