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

compose_cmd=()
if docker compose version >/dev/null 2>&1; then
  compose_cmd=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose_cmd=(docker-compose)
fi

if [[ ${#compose_cmd[@]} -gt 0 ]] && "${compose_cmd[@]}" ps -q gateway >/dev/null 2>&1; then
  gateway_container="$("${compose_cmd[@]}" ps -q gateway 2>/dev/null | head -n1)"
  if [[ -n "${gateway_container}" ]]; then
    if "${compose_cmd[@]}" exec -T gateway python3 - "$CLIENT_ID" "$CLIENT_SECRET" <<'PY'
import json
import sys
import urllib.parse
import urllib.request

client_id = sys.argv[1]
client_secret = sys.argv[2]
payload = urllib.parse.urlencode({
    "grant_type": "client_credentials",
    "client_id": client_id,
    "client_secret": client_secret,
}).encode("utf-8")
url = "http://keycloak:8080/realms/modelguard/protocol/openid-connect/token"
with urllib.request.urlopen(
    urllib.request.Request(url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"),
    timeout=10,
) as resp:
    print(json.load(resp)["access_token"])
PY
    then
      exit 0
    fi
    echo "failed to mint token through the running compose gateway container" >&2
    exit 1
  fi
fi

if command -v docker >/dev/null 2>&1; then
  gateway_container="$(
    docker ps \
      --filter "label=com.docker.compose.service=gateway" \
      --format '{{.ID}}' \
      | head -n1
  )"
  if [[ -z "${gateway_container}" ]]; then
    gateway_container="$(
      docker ps \
        --filter "name=gateway" \
        --format '{{.ID}}' \
        | head -n1
    )"
  fi
  if [[ -n "${gateway_container}" ]]; then
    if docker exec -i "${gateway_container}" python3 - "$CLIENT_ID" "$CLIENT_SECRET" <<'PY'
import json
import os
import sys
import urllib.parse
import urllib.request

client_id = sys.argv[1]
client_secret = sys.argv[2]
keycloak_url = os.getenv("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")
realm = os.getenv("KEYCLOAK_REALM", "modelguard")
payload = urllib.parse.urlencode({
    "grant_type": "client_credentials",
    "client_id": client_id,
    "client_secret": client_secret,
}).encode("utf-8")
url = f"{keycloak_url}/realms/{realm}/protocol/openid-connect/token"
with urllib.request.urlopen(
    urllib.request.Request(url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"),
    timeout=10,
) as resp:
    print(json.load(resp)["access_token"])
PY
    then
      exit 0
    fi
    echo "failed to mint token through discovered gateway container ${gateway_container}" >&2
    exit 1
  fi
fi

curl -fsS -X POST "${KEYCLOAK_URL:-http://localhost:8080}/realms/modelguard/protocol/openid-connect/token" \
  -H 'content-type: application/x-www-form-urlencoded' \
  -d grant_type=client_credentials \
  -d client_id="$CLIENT_ID" \
  -d client_secret="$CLIENT_SECRET" | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin)["access_token"])'
