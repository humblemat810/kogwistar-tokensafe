#!/usr/bin/env bash
set -euo pipefail
export MODELKEYGUARD_GRAPH_KEY=${MODELKEYGUARD_GRAPH_KEY:-dev-modelkeyguard-change-me-32bytes!!}
export MODELKEYGUARD_DRY_RUN=${MODELKEYGUARD_DRY_RUN:-1}
export MODELKEYGUARD_HOST=${MODELKEYGUARD_HOST:-127.0.0.1}
export MODELKEYGUARD_PORT=${MODELKEYGUARD_PORT:-8789}
python -m modelkeyguard gateway --host "$MODELKEYGUARD_HOST" --port "$MODELKEYGUARD_PORT"
