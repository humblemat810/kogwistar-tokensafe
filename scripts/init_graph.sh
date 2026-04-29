#!/usr/bin/env bash
set -euo pipefail

# Initialize or reinitialize graph state from config/gateway_policy.json.
# This seeds the graph/projection backend with the current policy contract.
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-.}"
export PYTHONDONTWRITEBYTECODE=1
export MODELKEYGUARD_GRAPH_KEY="${MODELKEYGUARD_GRAPH_KEY:-dev-only-change-this-32-bytes-minimum}"
export MODELKEYGUARD_POLICY_PATH="${MODELKEYGUARD_POLICY_PATH:-config/gateway_policy.json}"
export MODELKEYGUARD_GRAPH_PATH="${MODELKEYGUARD_GRAPH_PATH:-out/modelkeyguard_graph.jsonl}"
export MODELKEYGUARD_INIT_RESET_EXISTING="${MODELKEYGUARD_INIT_RESET_EXISTING:-1}"
if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="$PYTHON"
elif [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
else
  PYTHON_BIN="python3"
fi
"$PYTHON_BIN" -c "import os; from modelkeyguard.graph_tools import init_graph; init_graph(os.environ['MODELKEYGUARD_POLICY_PATH'], os.environ['MODELKEYGUARD_GRAPH_PATH'])"
