#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-.}"
export PYTHONDONTWRITEBYTECODE=1
export KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
export KEYCLOAK_REALM="${KEYCLOAK_REALM:-modelguard}"
export KEYCLOAK_INTROSPECTION_CLIENT_ID="${KEYCLOAK_INTROSPECTION_CLIENT_ID:-modelguard-gateway}"
export KEYCLOAK_INTROSPECTION_CLIENT_SECRET="${KEYCLOAK_INTROSPECTION_CLIENT_SECRET:-gateway-secret}"
export MODELKEYGUARD_DRY_RUN="${MODELKEYGUARD_DRY_RUN:-1}"
export MODELKEYGUARD_GRAPH_KEY="${MODELKEYGUARD_GRAPH_KEY:-dev-only-change-this-32-bytes-minimum}"
exec ${PYTHON:-python3} -m modelkeyguard gateway --host "${MODELKEYGUARD_HOST:-127.0.0.1}" --port "${MODELKEYGUARD_PORT:-8789}" --policy "${MODELKEYGUARD_POLICY:-config/gateway_policy.json}"
