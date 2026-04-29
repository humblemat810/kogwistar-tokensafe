#!/usr/bin/env bash
set -euo pipefail

# Fetch a Keycloak access token for one of the bundled demo clients.
# Used by local OIDC and Keycloak-authenticated smoke tests.
CLIENT_ID="${1:-langchain-agent}"
CLIENT_SECRET="${2:-agent-secret}"
PYTHON_BIN="${PYTHON:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x ".venv/bin/python" ]]; then
    PYTHON_BIN=".venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi
curl -fsS -X POST 'http://localhost:8080/realms/modelguard/protocol/openid-connect/token' \
  -H 'content-type: application/x-www-form-urlencoded' \
  -d grant_type=client_credentials \
  -d client_id="$CLIENT_ID" \
  -d client_secret="$CLIENT_SECRET" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
