#!/usr/bin/env python3
"""One-minute client demo: LangChain/OpenAI-style app calls ModelKeyGuard.

This intentionally sends an OpenAI-compatible /v1/chat/completions request with
Authorization: Bearer <Kogwistar token>. The gateway replaces that with the real
provider secret only after graph ACL/quota checks pass.
"""
from __future__ import annotations
import json
import os
import urllib.request

base_url = os.getenv("OPENAI_BASE_URL", "http://127.0.0.1:8789/v1").rstrip("/")
api_key = os.getenv("OPENAI_API_KEY", os.getenv("KGW_TOKEN", "kgw_demo_doc_ingestor"))
model = os.getenv("OPENAI_MODEL", "gpt-5.3-mini")

payload = {
    "model": model,
    "messages": [
        {"role": "system", "content": "You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets."},
        {"role": "user", "content": "Say one sentence proving the request passed through ModelKeyGuard."},
    ],
    "max_tokens": 64,
}
req = urllib.request.Request(
    f"{base_url}/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={"authorization": f"Bearer {api_key}", "content-type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.status)
        print(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    print(e.code)
    print(e.read().decode("utf-8"))
