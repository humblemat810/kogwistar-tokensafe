#!/usr/bin/env bash
set -euo pipefail
CLIENT_ID="${1:-langchain-agent}"
CLIENT_SECRET="${2:-agent-secret}"
curl -fsS -X POST 'http://localhost:8080/realms/modelguard/protocol/openid-connect/token' \
  -H 'content-type: application/x-www-form-urlencoded' \
  -d grant_type=client_credentials \
  -d client_id="$CLIENT_ID" \
  -d client_secret="$CLIENT_SECRET" | python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
