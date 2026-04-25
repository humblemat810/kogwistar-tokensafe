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
${PYTHON:-python3} -c "from modelkeyguard.gateway import serve; serve('127.0.0.1', 8789, 'config/gateway_policy.json')"
