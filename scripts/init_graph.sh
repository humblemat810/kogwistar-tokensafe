#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-.}"
export PYTHONDONTWRITEBYTECODE=1
export MODELKEYGUARD_GRAPH_KEY="${MODELKEYGUARD_GRAPH_KEY:-dev-only-change-this-32-bytes-minimum}"
export MODELKEYGUARD_POLICY_PATH="${MODELKEYGUARD_POLICY_PATH:-config/gateway_policy.json}"
export MODELKEYGUARD_GRAPH_PATH="${MODELKEYGUARD_GRAPH_PATH:-out/modelkeyguard_graph.jsonl}"
export MODELKEYGUARD_INIT_RESET_EXISTING="${MODELKEYGUARD_INIT_RESET_EXISTING:-1}"
${PYTHON:-python3} -c "import os; from modelkeyguard.graph_tools import init_graph; init_graph(os.environ['MODELKEYGUARD_POLICY_PATH'], os.environ['MODELKEYGUARD_GRAPH_PATH'])"
