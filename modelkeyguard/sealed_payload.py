from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

try:  # production path
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except Exception:  # pragma: no cover
    AESGCM = None  # type: ignore


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def derive_key(app_key: str) -> bytes:
    if not app_key:
        raise ValueError("MODELKEYGUARD_GRAPH_KEY is required to decrypt graph payloads")
    return hashlib.sha256(app_key.encode("utf-8")).digest()


def seal_json(payload: dict[str, Any], app_key: str, *, aad: bytes = b"kgw-modelkeyguard-v2") -> dict[str, str]:
    """Seal JSON payload with authenticated encryption.

    Uses AES-GCM when cryptography is installed. The output contains only
    ciphertext/tag/nonce and never stores plaintext graph payload fields.
    """
    key = derive_key(app_key)
    nonce = secrets.token_bytes(12)
    plain = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if AESGCM is not None:
        cipher = AESGCM(key).encrypt(nonce, plain, aad)
        return {"alg": "AES-256-GCM", "nonce": _b64e(nonce), "ciphertext": _b64e(cipher), "aad": _b64e(aad)}
    stream = _fallback_keystream(key, nonce, len(plain))
    ciphertext = bytes(a ^ b for a, b in zip(plain, stream))
    tag = hmac.new(key, aad + nonce + ciphertext, hashlib.sha256).digest()
    return {"alg": "KGW-HMAC-XOR-fallback", "nonce": _b64e(nonce), "ciphertext": _b64e(ciphertext), "tag": _b64e(tag), "aad": _b64e(aad)}


def open_json(sealed: dict[str, str], app_key: str) -> dict[str, Any]:
    key = derive_key(app_key)
    nonce = _b64d(sealed["nonce"])
    aad = _b64d(sealed.get("aad", _b64e(b"kgw-modelkeyguard-v1")))
    ciphertext = _b64d(sealed["ciphertext"])
    if sealed.get("alg") == "AES-256-GCM" and AESGCM is not None:
        plain = AESGCM(key).decrypt(nonce, ciphertext, aad)
    else:
        tag = _b64d(sealed["tag"])
        expected = hmac.new(key, aad + nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise ValueError("sealed graph payload authentication failed")
        stream = _fallback_keystream(key, nonce, len(ciphertext))
        plain = bytes(a ^ b for a, b in zip(ciphertext, stream))
    data = json.loads(plain.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("sealed payload must decode to object")
    return data


def _fallback_keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:n])
