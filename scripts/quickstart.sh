#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON:-python3}"
HOST="${MODELKEYGUARD_HOST:-127.0.0.1}"
PORT="${MODELKEYGUARD_PORT:-8789}"
BASE_URL="http://${HOST}:${PORT}"
LOG_DIR="${MODELKEYGUARD_QUICKSTART_LOG_DIR:-out/quickstart}"
GRAPH_PATH="${MODELKEYGUARD_GRAPH_PATH:-out/quickstart_graph.jsonl}"
AUDIT_PATH="${MODELKEYGUARD_AUDIT_PATH:-out/quickstart_audit.jsonl}"
REVIEW_OUT="${MODELKEYGUARD_REVIEW_OUT:-out/quickstart_review_results.jsonl}"
GRAPH_KEY="${MODELKEYGUARD_GRAPH_KEY:-dev-quickstart-modelkeyguard-graph-key-32b}"

mkdir -p "$LOG_DIR" out
export PYTHONPATH="${PYTHONPATH:-.}"
export PYTHONDONTWRITEBYTECODE=1
export MODELKEYGUARD_GRAPH_PATH="$GRAPH_PATH"
export MODELKEYGUARD_AUDIT_PATH="$AUDIT_PATH"
export MODELKEYGUARD_GRAPH_KEY="$GRAPH_KEY"
export MODELKEYGUARD_DRY_RUN="${MODELKEYGUARD_DRY_RUN:-1}"
export MODELKEYGUARD_AUTH_MODE="${MODELKEYGUARD_AUTH_MODE:-local}"
export MODELKEYGUARD_HOST="$HOST"
export MODELKEYGUARD_PORT="$PORT"

GATEWAY_PID=""
cleanup() {
  if [[ -n "${GATEWAY_PID:-}" ]] && kill -0 "$GATEWAY_PID" >/dev/null 2>&1; then
    echo "[cleanup] stopping gateway pid=$GATEWAY_PID"
    kill "$GATEWAY_PID" >/dev/null 2>&1 || true
    wait "$GATEWAY_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

step() { printf '\n\033[1;36m[%s/%s] %s\033[0m\n' "$1" "$2" "$3"; }
note() { printf '  - %s\n' "$1"; }
run() { printf '    $ %s\n' "$*"; "$@"; }

TOTAL=8
step 1 "$TOTAL" "Prepare isolated local quickstart state"
note "graph path: $GRAPH_PATH"
note "audit path: $AUDIT_PATH"
note "dry-run provider mode: $MODELKEYGUARD_DRY_RUN (no real OpenAI key needed)"
rm -f "$GRAPH_PATH" "$AUDIT_PATH" "$REVIEW_OUT"

step 2 "$TOTAL" "Initialize graph-native policy state"
run "$PYTHON_BIN" -c "from modelkeyguard.graph_tools import init_graph; init_graph('config/gateway_policy.json', '$GRAPH_PATH')"

step 3 "$TOTAL" "Start FastAPI ModelKeyGuard gateway in the background"
GATEWAY_LOG="$LOG_DIR/gateway.log"
("$PYTHON_BIN" -m modelkeyguard gateway --host "$HOST" --port "$PORT" >"$GATEWAY_LOG" 2>&1) &
GATEWAY_PID=$!
note "gateway pid: $GATEWAY_PID"
note "gateway log: $GATEWAY_LOG"

printf '  - waiting for /healthz'
for _ in $(seq 1 80); do
  if curl -fsS "$BASE_URL/healthz" >/dev/null 2>&1; then
    printf ' ready\n'
    break
  fi
  printf '.'
  sleep 0.25
done
if ! curl -fsS "$BASE_URL/healthz" >/dev/null 2>&1; then
  echo "\nGateway did not become ready. Last log lines:" >&2
  tail -80 "$GATEWAY_LOG" >&2 || true
  exit 1
fi
curl -fsS "$BASE_URL/healthz" | "$PYTHON_BIN" -m json.tool

step 4 "$TOTAL" "Run OpenAI-compatible client call using a Kogwistar-safe key"
export OPENAI_BASE_URL="$BASE_URL/v1"
export OPENAI_API_KEY="kgw_demo_doc_ingestor"
run "$PYTHON_BIN" scripts/langchain_user_openai_compatible.py

step 5 "$TOTAL" "Show quota/error semantics: principal limit, user limit, permission deny"
for token in kgw_demo_principal_busy kgw_demo_low_user kgw_demo_external; do
  printf '\n  - exercising token: %s\n' "$token"
  set +e
  KGW_TOKEN="$token" ./scripts/test_chat.sh
  status=$?
  set -e
  note "script exit code: $status (non-zero is expected for denied cases because curl uses -f)"
done

step 6 "$TOTAL" "Inspect graph state: access conversation, usage ledger, named projections"
run "$PYTHON_BIN" -c "from modelkeyguard.graph_tools import inspect_graph; inspect_graph('$GRAPH_PATH')"

step 7 "$TOTAL" "Run alert and LLM-style usage review over the audit log"
run "$PYTHON_BIN" -m modelkeyguard review-once --audit "$AUDIT_PATH" --policy config/gateway_policy.json --out "$REVIEW_OUT"
note "review results: $REVIEW_OUT"

step 8 "$TOTAL" "Summary and next commands"
cat <<TXT

Quickstart completed.

Important artifacts:
  - Gateway log:       $GATEWAY_LOG
  - Encrypted graph:   $GRAPH_PATH
  - Audit JSONL:       $AUDIT_PATH
  - Review JSONL:      $REVIEW_OUT

Try the normal foreground server next:
  export MODELKEYGUARD_GRAPH_PATH=$GRAPH_PATH
  export MODELKEYGUARD_AUDIT_PATH=$AUDIT_PATH
  export MODELKEYGUARD_GRAPH_KEY=$GRAPH_KEY
  ./scripts/start_gateway.sh

Then call it like OpenAI:
  export OPENAI_BASE_URL=$BASE_URL/v1
  export OPENAI_API_KEY=kgw_demo_doc_ingestor
  python scripts/langchain_user_openai_compatible.py
TXT
