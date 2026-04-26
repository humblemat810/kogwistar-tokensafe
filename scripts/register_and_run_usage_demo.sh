#!/usr/bin/env bash
set -euo pipefail
export MODELKEYGUARD_GRAPH_PATH=${MODELKEYGUARD_GRAPH_PATH:-out/registration_demo_graph.jsonl}
export MODELKEYGUARD_GRAPH_KEY=${MODELKEYGUARD_GRAPH_KEY:-dev-registration-demo-key-change-me}
export MODELKEYGUARD_DRY_RUN=${MODELKEYGUARD_DRY_RUN:-1}
export MODELKEYGUARD_HOST=${MODELKEYGUARD_HOST:-127.0.0.1}
export MODELKEYGUARD_PORT=${MODELKEYGUARD_PORT:-8789}
./scripts/register_usage_example.sh >/dev/null
python -m modelkeyguard gateway --host "$MODELKEYGUARD_HOST" --port "$MODELKEYGUARD_PORT" &
PID=$!
cleanup() { kill "$PID" >/dev/null 2>&1 || true; }
trap cleanup EXIT
for _ in $(seq 1 30); do
  if curl -fsS "http://$MODELKEYGUARD_HOST:$MODELKEYGUARD_PORT/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
export KGW_TOKEN=$(cat out/registration_demo_token.txt)
echo "Running OpenAI-compatible request through ModelKeyGuard..."
./scripts/test_chat.sh
echo ""
echo "Inspecting graph state after request..."
python -m modelkeyguard inspect-graph
