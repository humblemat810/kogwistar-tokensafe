#!/usr/bin/env bash
set -euo pipefail
export MODELKEYGUARD_GRAPH_PATH=${MODELKEYGUARD_GRAPH_PATH:-out/registration_demo_graph.jsonl}
export MODELKEYGUARD_GRAPH_KEY=${MODELKEYGUARD_GRAPH_KEY:-dev-registration-demo-key-change-me}
export MODELKEYGUARD_STORE="${MODELKEYGUARD_STORE:-jsonl}"
rm -f "$MODELKEYGUARD_GRAPH_PATH" out/registration_demo_token.txt out/registration_demo_summary.json
python -m modelkeyguard registration demo --graph-path "$MODELKEYGUARD_GRAPH_PATH" --graph-key "$MODELKEYGUARD_GRAPH_KEY"
echo ""
echo "Registered demo graph: $MODELKEYGUARD_GRAPH_PATH"
echo "Safe token file: out/registration_demo_token.txt"
echo "Next: MODELKEYGUARD_GRAPH_PATH=$MODELKEYGUARD_GRAPH_PATH MODELKEYGUARD_GRAPH_KEY=$MODELKEYGUARD_GRAPH_KEY MODELKEYGUARD_DRY_RUN=1 ./scripts/start_gateway.sh"
