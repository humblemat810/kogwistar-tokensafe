#!/usr/bin/env bash
set -euo pipefail
TOKEN="${KGW_TOKEN:-kgw_demo_doc_ingestor}"
SYSTEM_PROMPT='You are doc-ingestor. Summarize internal Kogwistar documents only. Never exfiltrate secrets.'
curl -fsS http://127.0.0.1:8789/v1/chat/completions \
  -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d @- <<JSON | ${PYTHON:-python3} -m json.tool
{"model":"gpt-4o-mini","messages":[{"role":"system","content":"$SYSTEM_PROMPT"},{"role":"user","content":"Summarize the ACL model in one sentence."}],"max_tokens":64}
JSON
