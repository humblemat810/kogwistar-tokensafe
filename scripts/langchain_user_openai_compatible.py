#!/usr/bin/env python3
"""One-minute client demo: LangChain/OpenAI-style app calls ModelKeyGuard.

This intentionally sends an OpenAI-compatible /v1/chat/completions request with
Authorization: Bearer <Kogwistar token>. The gateway replaces that with the real
provider secret only after graph ACL/quota checks pass.

If the running gateway is still in dry-run mode, the request will succeed but
return a synthetic ModelKeyGuard completion instead of a real provider reply.
Set MODELKEYGUARD_DRY_RUN=0 and register a real provider secret to exercise the
real upstream path.
"""
from __future__ import annotations
import json
import os
import urllib.request
import socket

base_url = os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:8789/v1").rstrip("/")
api_key = os.getenv("OPENAI_API_KEY", os.getenv("KGW_TOKEN", "kgw_demo_doc_ingestor"))
model = os.getenv("OPENAI_MODEL", "gpt-5.3-mini")
timeout_seconds = float(os.getenv("MODELKEYGUARD_CLIENT_TIMEOUT_SECONDS", "20"))
key_id = os.getenv("MODELKEYGUARD_KEY_ID", "").strip()

payload = {
    "model": model,
    "messages": [
        {"role": "system", "content": "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."},
        {"role": "user", "content": "Say one sentence proving the request passed through ModelKeyGuard."},
    ],
    "max_tokens": 64,
}
if key_id:
    payload["modelkeyguard"] = {"key_id": key_id}
req = urllib.request.Request(
    f"{base_url}/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        print(resp.status)
        print(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    print(e.code)
    print(e.read().decode("utf-8"))
except (TimeoutError, socket.timeout) as e:
    print("timeout")
    print(f"request timed out after {timeout_seconds:g}s: {e}")
