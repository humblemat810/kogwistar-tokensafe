#!/usr/bin/env bash
set -euo pipefail
export MODELKEYGUARD_GRAPH_KEY=${MODELKEYGUARD_GRAPH_KEY:-dev-modelkeyguard-change-me-32bytes!!}
export MODELKEYGUARD_DRY_RUN=${MODELKEYGUARD_DRY_RUN:-1}
export MODELKEYGUARD_HOST=${MODELKEYGUARD_HOST:-127.0.0.1}
export MODELKEYGUARD_PORT=${MODELKEYGUARD_PORT:-8789}
export MODELKEYGUARD_POLICY_PATH=${MODELKEYGUARD_POLICY_PATH:-config/gateway_policy.json}

if ${PYTHON:-python3} - "${MODELKEYGUARD_HOST}" "${MODELKEYGUARD_PORT}" <<'PY'
import json
import sys
import urllib.error
import urllib.request

host, port = sys.argv[1], sys.argv[2]
url = f"http://{host}:{port}/healthz"
try:
    with urllib.request.urlopen(url, timeout=1.0) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
ok = bool(payload.get("ok")) and payload.get("service") == "modelkeyguard-gateway"
raise SystemExit(0 if ok else 1)
PY
then
  echo "ModelKeyGuard gateway is already running on ${MODELKEYGUARD_HOST}:${MODELKEYGUARD_PORT}; skipping restart."
  exit 0
fi

python -m modelkeyguard gateway --host "$MODELKEYGUARD_HOST" --port "$MODELKEYGUARD_PORT" --policy "$MODELKEYGUARD_POLICY_PATH"
