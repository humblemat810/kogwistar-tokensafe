#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

BASE_URL="${BASE_URL:-http://127.0.0.1:8789}"
MODEL="${MODEL:-gemma4:e2b}"
OLLAMA_KEY_ID="${OLLAMA_KEY_ID:-}"
PRINCIPAL_ID="${PRINCIPAL_ID:-agent:doc-ingestor}"
USER_ID="${USER_ID:-user:alice}"
APPLICATION_ID="${APPLICATION_ID:-app:doc-ingestor}"
CONNECT_TIMEOUT_SECONDS="${CONNECT_TIMEOUT_SECONDS:-3}"
MAX_TIME_SECONDS="${MAX_TIME_SECONDS:-20}"
CLIENT_TIMEOUT_SECONDS="${CLIENT_TIMEOUT_SECONDS:-180}"
PYTHON_BIN="${PYTHON_BIN:-${REPO_ROOT}/.venv/bin/python}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="python3"
fi

if [[ -z "${MODELKEYGUARD_ADMIN_API_SECRET:-}" ]]; then
  echo "MODELKEYGUARD_ADMIN_API_SECRET is required." >&2
  echo "For the local compose deployment, try:" >&2
  echo "  export MODELKEYGUARD_ADMIN_API_SECRET=\"\$(cat ./secrets/modelkeyguard_admin_api_secret)\"" >&2
  exit 1
fi

echo "Minting admin token..."
export ADMIN_TOKEN="$(
  "${REPO_ROOT}/scripts/get_agent_token.sh" modelguard-admin admin-agent-secret
)"
if [[ -z "${ADMIN_TOKEN}" ]]; then
  echo "failed to mint ADMIN_TOKEN; check scripts/get_agent_token.sh and your Keycloak/admin auth setup" >&2
  exit 1
fi

echo "Reading registered provider keys from ${BASE_URL}/admin/keys.json..."
key_lookup_json="$(
  curl -fsS --connect-timeout "${CONNECT_TIMEOUT_SECONDS}" --max-time "${MAX_TIME_SECONDS}" \
    -H "x-modelkeyguard-admin-secret: ${MODELKEYGUARD_ADMIN_API_SECRET}" \
    "${BASE_URL}/admin/keys.json"
)"

read -r OLLAMA_KEY_ID OLLAMA_MODEL OLLAMA_UPSTREAM_URL <<<"$(
  printf '%s' "${key_lookup_json}" | MODEL="${MODEL}" SELECTED_KEY_ID="${OLLAMA_KEY_ID}" "${PYTHON_BIN}" -c '
import json, os, sys

model = os.environ["MODEL"]
selected = os.environ.get("SELECTED_KEY_ID", "").strip()
data = json.load(sys.stdin)["data"]
ollama = [item for item in data if item.get("provider") == "ollama" and item.get("status", "active") == "active"]

if selected:
    match = next((item for item in ollama if item.get("key_id") == selected), None)
    if not match:
        raise SystemExit(f"no ollama key found with key_id={selected}")
    models = [m for m in match.get("models", []) if m]
    if not models:
        raise SystemExit(f"ollama key {selected} has no models")
    print(selected, models[0], match.get("upstream_url", ""))
    raise SystemExit(0)

matches = [item for item in ollama if model in item.get("models", [])]
if not matches:
    raise SystemExit(f"no matching ollama key found for model {model}")
if len(matches) > 1:
    raise SystemExit(
        "ambiguous ollama model selection: "
        + ",".join(item["key_id"] for item in matches)
        + " all advertise model "
        + model
        + ". Revoke the old key or register the forwarded key with a unique model name."
    )
print(matches[0]["key_id"], model, matches[0].get("upstream_url", ""))
'
)"

echo "Using Ollama key: ${OLLAMA_KEY_ID}"
echo "Using Ollama model: ${OLLAMA_MODEL}"
echo "Using Ollama upstream: ${OLLAMA_UPSTREAM_URL:-<default>}"

if [[ -n "${OLLAMA_UPSTREAM_URL}" ]]; then
  OLLAMA_TAGS_URL="$(
    OLLAMA_UPSTREAM_URL="${OLLAMA_UPSTREAM_URL}" "${PYTHON_BIN}" -c '
import os
from urllib.parse import urlsplit, urlunsplit

url = os.environ["OLLAMA_UPSTREAM_URL"]
parts = urlsplit(url)
print(urlunsplit((parts.scheme, parts.netloc, "/api/tags", "", "")))
'
  )"
  echo "Checking Ollama tags endpoint from this machine: ${OLLAMA_TAGS_URL}"
  curl -fsS --connect-timeout "${CONNECT_TIMEOUT_SECONDS}" --max-time "${CLIENT_TIMEOUT_SECONDS}" \
    "${OLLAMA_TAGS_URL}" >/dev/null
fi

echo "Minting safe token for ${PRINCIPAL_ID} on behalf of ${USER_ID}..."
SAFE_TOKEN="$(
  curl -fsS --connect-timeout "${CONNECT_TIMEOUT_SECONDS}" --max-time "${MAX_TIME_SECONDS}" \
    -X POST "${BASE_URL}/admin/policy/tokens" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H 'content-type: application/json' \
    -d "$(printf '{"principal_id":"%s","namespace":"tenant:kogwistar","on_behalf_of_user_id":"%s","application_id":"%s","scopes":["model.invoke"]}' \
      "$PRINCIPAL_ID" "$USER_ID" "$APPLICATION_ID")" \
  | "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin)["safe_token"])'
)"

echo "Calling ModelKeyGuard through ${BASE_URL}/v1/chat/completions..."
OPENAI_BASE_URL="${BASE_URL}/v1" \
OPENAI_API_KEY="${SAFE_TOKEN}" \
OPENAI_MODEL="${OLLAMA_MODEL}" \
MODELKEYGUARD_KEY_ID="${OLLAMA_KEY_ID}" \
MODELKEYGUARD_CLIENT_TIMEOUT_SECONDS="${CLIENT_TIMEOUT_SECONDS}" \
"${PYTHON_BIN}" "${REPO_ROOT}/scripts/langchain_user_openai_compatible.py"
