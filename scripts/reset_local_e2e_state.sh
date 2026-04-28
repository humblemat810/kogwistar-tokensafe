#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TOKENSAFE_HOME="${MODELKEYGUARD_HOME:-${TOKENSAFE_HOME:-$HOME/.tokensafe}}"
DEFAULT_PG_DATA_DIR="${MODELKEYGUARD_POSTGRES_DATA_DIR:-${TOKENSAFE_HOME%/}/postgres}"
DEFAULT_KEYCLOAK_DATA_DIR="${MODELKEYGUARD_KEYCLOAK_DATA_DIR:-${TOKENSAFE_HOME%/}/keycloak}"
RESET_DATA_DIR="${MODELKEYGUARD_RESET_DATA_DIR:-$DEFAULT_PG_DATA_DIR}"
RESET_KEYCLOAK_DATA_DIR="${MODELKEYGUARD_RESET_KEYCLOAK_DATA_DIR:-$DEFAULT_KEYCLOAK_DATA_DIR}"
RESET_OUT_DIR="${MODELKEYGUARD_RESET_OUT_DIR:-$ROOT_DIR/out}"
RESET_LLM_CALL_CACHE_DIR="${MODELKEYGUARD_LLM_CALL_CACHE_DIR:-${RESET_OUT_DIR%/}/llm_call_cache}"

echo "[reset] stopping local gateway processes (if any)"
pkill -f "python -m modelkeyguard gateway" >/dev/null 2>&1 || true
pkill -f "uvicorn.*modelkeyguard" >/dev/null 2>&1 || true

echo "[reset] stopping compose stack and removing volumes (if compose is available)"
if docker compose version >/dev/null 2>&1; then
  docker compose down -v >/dev/null 2>&1 || true
elif command -v docker-compose >/dev/null 2>&1; then
  docker-compose down -v >/dev/null 2>&1 || true
fi

echo "[reset] removing fallback standalone postgres container (if any)"
docker rm -f modelkeyguard-postgres >/dev/null 2>&1 || true

echo "[reset] removing local postgres data directory used by this repo (if writable): ${RESET_DATA_DIR}"
rm -rf "${RESET_DATA_DIR}" >/dev/null 2>&1 || true
if [[ -e "${RESET_DATA_DIR}" ]]; then
  echo "[reset] warning: could not remove ${RESET_DATA_DIR}; stop containers and check ownership/permissions" >&2
  echo "[reset] warning: for a truly fresh local Postgres directory, run:" >&2
  echo "  sudo rm -rf '${RESET_DATA_DIR}'" >&2
fi
echo "[reset] removing local keycloak data directory used by this repo (if writable): ${RESET_KEYCLOAK_DATA_DIR}"
rm -rf "${RESET_KEYCLOAK_DATA_DIR}" >/dev/null 2>&1 || true
if [[ -e "${RESET_KEYCLOAK_DATA_DIR}" ]]; then
  echo "[reset] warning: could not remove ${RESET_KEYCLOAK_DATA_DIR}; stop containers and check ownership/permissions" >&2
  echo "[reset] warning: for a truly fresh local Keycloak directory, run:" >&2
  echo "  sudo rm -rf '${RESET_KEYCLOAK_DATA_DIR}'" >&2
fi
echo "[reset] removing local LLM call cache directory (if present): ${RESET_LLM_CALL_CACHE_DIR}"
rm -rf "${RESET_LLM_CALL_CACHE_DIR}" >/dev/null 2>&1 || true
if [[ -e "${RESET_LLM_CALL_CACHE_DIR}" ]]; then
  echo "[reset] warning: could not remove ${RESET_LLM_CALL_CACHE_DIR}; check ownership/permissions" >&2
fi

echo "[reset] removing configured local graph/audit artifacts for any backend"
${PYTHON:-python3} - <<'PY' || true
from __future__ import annotations

import os
from pathlib import Path


def _remove_file(path_str: str) -> None:
    p = Path(path_str)
    if not p.exists():
        return
    try:
        p.unlink()
    except IsADirectoryError:
        return
    except Exception:
        return


for env_name, default in (
    ("MODELKEYGUARD_GRAPH_PATH", "out/modelkeyguard_graph.jsonl"),
    ("MODELKEYGUARD_AUDIT_PATH", "out/modelkeyguard_audit.jsonl"),
    ("MODELKEYGUARD_REVIEW_OUT", "out/review_results.jsonl"),
):
    _remove_file(os.getenv(env_name, default))

store = os.getenv("MODELKEYGUARD_STORE", "jsonl").strip().lower()
dsn = os.getenv("MODELKEYGUARD_POSTGRES_DSN", "").strip()
if store == "postgres" or dsn:
    try:
        from modelkeyguard.graph_tools import _reset_postgres_graph_state
        _reset_postgres_graph_state(dsn or None)
        print("[reset] postgres graph tables truncated (when reachable)")
    except Exception:
        # Keep reset script resilient; compose down -v and file cleanup already ran.
        print("[reset] postgres truncate skipped (not reachable or dependency unavailable)")
PY

echo "[reset] removing local tutorial artifacts (if present) from ${RESET_OUT_DIR}"
if [[ -d "${RESET_OUT_DIR}" ]]; then
  rm -f \
    "${RESET_OUT_DIR}/modelkeyguard_graph.jsonl" \
    "${RESET_OUT_DIR}/modelkeyguard_audit.jsonl" \
    "${RESET_OUT_DIR}/review_results.jsonl" \
    "${RESET_OUT_DIR}/single_e2e_graph.jsonl" \
    "${RESET_OUT_DIR}/single_e2e_audit.jsonl" \
    "${RESET_OUT_DIR}/single_e2e_policy.json" \
    "${RESET_OUT_DIR}/azure_real_billing_graph.jsonl" \
    "${RESET_OUT_DIR}/azure_real_billing_audit.jsonl" \
    "${RESET_OUT_DIR}/azure_real_billing_policy.json" \
    "${RESET_OUT_DIR}/azure_real_history_detail.json" \
    "${RESET_OUT_DIR}/case1_graph.jsonl" \
    "${RESET_OUT_DIR}/case1_audit.jsonl" \
    "${RESET_OUT_DIR}/case1_review.jsonl" \
    "${RESET_OUT_DIR}/case1_review_checkpoint.json" \
    "${RESET_OUT_DIR}/case2_graph.jsonl" \
    "${RESET_OUT_DIR}/case2_audit.jsonl" \
    "${RESET_OUT_DIR}/quickstart_review_results.jsonl" \
    "${RESET_OUT_DIR}/review_checkpoint.json" \
    "${RESET_OUT_DIR}/finaldev_graph.jsonl" \
    "${RESET_OUT_DIR}/finaldev_audit.jsonl" \
    "${RESET_OUT_DIR}/finaldev_review.jsonl" \
    "${RESET_OUT_DIR}/quickstart_graph.jsonl" \
    "${RESET_OUT_DIR}/quickstart_audit.jsonl" >/dev/null 2>&1 || true
fi

echo "[reset] current listeners on 5432/8789 (if any):"
if command -v rg >/dev/null 2>&1; then
  ss -ltnp | rg ':5432|:8789' || echo "  none"
else
  ss -ltnp | grep -E ':5432|:8789' || echo "  none"
fi

echo "[reset] done"
echo "If 5432 is still occupied by a host postgres service, stop it manually:"
echo "  sudo systemctl stop postgresql"
