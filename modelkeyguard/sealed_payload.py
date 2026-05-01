from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

try:  # production path
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
except Exception:  # pragma: no cover
    AESGCM = None  # type: ignore
    InvalidTag = None  # type: ignore


GRAPH_KEY_SENTINEL_TEXT = "hello encrypted token 123@#E "
GRAPH_KEY_SENTINEL_NODE_ID = "system:graph_key_sentinel"
GRAPH_KEY_SENTINEL_KIND = "graph_key_sentinel"
GRAPH_KEY_SENTINEL_PAYLOAD = {"sentinel_text": GRAPH_KEY_SENTINEL_TEXT}
_GRAPH_KEY_SENTINEL_CACHE: dict[str, dict[str, str]] = {}


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def derive_key(app_key: str) -> bytes:
    if not app_key:
        raise ValueError("MODELKEYGUARD_GRAPH_KEY is required to decrypt graph payloads")
    return hashlib.sha256(app_key.encode("utf-8")).digest()


def _sentinel_cache_key(app_key: str) -> str:
    return hashlib.sha256(app_key.encode("utf-8")).hexdigest()


def build_graph_key_sentinel_storage_payload(app_key: str, *, aad: bytes = b"kgw-modelkeyguard-v2") -> dict[str, Any]:
    sealed = _seal_json_impl(GRAPH_KEY_SENTINEL_PAYLOAD, derive_key(app_key), aad)
    _GRAPH_KEY_SENTINEL_CACHE[_sentinel_cache_key(app_key)] = dict(sealed)
    return {
        "sentinel_text": GRAPH_KEY_SENTINEL_TEXT,
        "sealed_sentinel": dict(sealed),
    }


def seed_graph_key_sentinel_from_storage_payload(storage_payload: dict[str, Any], app_key: str) -> dict[str, str]:
    sealed = storage_payload.get("sealed_sentinel")
    if not isinstance(sealed, dict):
        raise ValueError("graph sentinel storage payload must include sealed_sentinel")
    try:
        opened = _open_json_impl(dict(sealed), derive_key(app_key))
    except Exception as exc:
        raise ValueError("sentinel payload did not decrypt to the expected text") from exc
    if opened.get("sentinel_text") != GRAPH_KEY_SENTINEL_TEXT:
        raise ValueError("sentinel payload did not decrypt to the expected text")
    _GRAPH_KEY_SENTINEL_CACHE[_sentinel_cache_key(app_key)] = dict(sealed)
    return dict(sealed)


def _bootstrap_graph_key_sentinel(app_key: str, *, aad: bytes = b"kgw-modelkeyguard-v2") -> dict[str, str]:
    storage_payload = build_graph_key_sentinel_storage_payload(app_key, aad=aad)
    sealed = dict(storage_payload["sealed_sentinel"])
    try:
        opened = _open_json_impl(dict(sealed), derive_key(app_key))
    except Exception as exc:
        raise ValueError("sentinel payload did not decrypt to the expected text") from exc
    if opened.get("sentinel_text") != GRAPH_KEY_SENTINEL_TEXT:
        raise ValueError("sentinel payload did not decrypt to the expected text")
    return sealed


def seal_json(payload: dict[str, Any], app_key: str, *, aad: bytes = b"kgw-modelkeyguard-v2") -> dict[str, str]:
    """Seal JSON payload with authenticated encryption.

    Uses AES-GCM when cryptography is installed. The output contains only
    ciphertext/tag/nonce and never stores plaintext graph payload fields.
    """
    ensure_graph_key_sentinel(app_key)
    return _seal_json_impl(payload, derive_key(app_key), aad)


def _seal_json_impl(payload: dict[str, Any], key: bytes, aad: bytes) -> dict[str, str]:
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
    return _open_json_impl(sealed, derive_key(app_key))


def ensure_graph_key_sentinel(app_key: str, *, aad: bytes = b"kgw-modelkeyguard-v2") -> dict[str, str]:
    cache_key = _sentinel_cache_key(app_key)
    sealed = _GRAPH_KEY_SENTINEL_CACHE.get(cache_key)
    if sealed is None:
        sealed = _bootstrap_graph_key_sentinel(app_key, aad=aad)
    try:
        opened = _open_json_impl(dict(sealed), derive_key(app_key))
    except Exception as exc:
        raise ValueError("sentinel payload did not decrypt to the expected text") from exc
    if opened.get("sentinel_text") != GRAPH_KEY_SENTINEL_TEXT:
        raise ValueError("sentinel payload did not decrypt to the expected text")
    _GRAPH_KEY_SENTINEL_CACHE[cache_key] = dict(sealed)
    return dict(sealed)


def _open_json_impl(sealed: dict[str, str], key: bytes) -> dict[str, Any]:
    nonce = _b64d(sealed["nonce"])
    aad = _b64d(sealed.get("aad", _b64e(b"kgw-modelkeyguard-v1")))
    ciphertext = _b64d(sealed["ciphertext"])
    if sealed.get("alg") == "AES-256-GCM" and AESGCM is not None:
        try:
            plain = AESGCM(key).decrypt(nonce, ciphertext, aad)
        except Exception as exc:
            if InvalidTag is not None and isinstance(exc, InvalidTag):
                raise ValueError("sealed graph payload authentication failed") from exc
            raise
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
