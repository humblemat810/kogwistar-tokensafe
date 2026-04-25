from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def derive_key(app_key: str) -> bytes:
    if not app_key:
        raise ValueError("MODELKEYGUARD_GRAPH_KEY is required to decrypt graph payloads")
    return hashlib.sha256(app_key.encode("utf-8")).digest()


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:n])


def seal_json(payload: dict[str, Any], app_key: str) -> dict[str, str]:
    """Seal JSON payload using stdlib-only authenticated encryption.

    This keeps the sample repo dependency-free. For production, swap this for
    AES-GCM via KMS/Vault/libsodium. The graph state never stores plaintext
    payload fields.
    """
    key = derive_key(app_key)
    nonce = secrets.token_bytes(16)
    plain = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    stream = _keystream(key, nonce, len(plain))
    cipher = bytes(a ^ b for a, b in zip(plain, stream))
    tag = hmac.new(key, b"kgw-modelkeyguard-v1" + nonce + cipher, hashlib.sha256).digest()
    return {"alg": "KGW-HMAC-XOR-v1", "nonce": _b64e(nonce), "ciphertext": _b64e(cipher), "tag": _b64e(tag)}


def open_json(sealed: dict[str, str], app_key: str) -> dict[str, Any]:
    key = derive_key(app_key)
    nonce = _b64d(sealed["nonce"])
    cipher = _b64d(sealed["ciphertext"])
    tag = _b64d(sealed["tag"])
    expected = hmac.new(key, b"kgw-modelkeyguard-v1" + nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("sealed graph payload authentication failed")
    stream = _keystream(key, nonce, len(cipher))
    plain = bytes(a ^ b for a, b in zip(cipher, stream))
    data = json.loads(plain.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("sealed payload must decode to object")
    return data
