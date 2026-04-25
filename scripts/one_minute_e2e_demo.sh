#!/usr/bin/env bash
set -euo pipefail
export MODELKEYGUARD_DRY_RUN=${MODELKEYGUARD_DRY_RUN:-1}
export MODELKEYGUARD_AUTH_MODE=${MODELKEYGUARD_AUTH_MODE:-local}
export MODELKEYGUARD_GRAPH_KEY=${MODELKEYGUARD_GRAPH_KEY:-dev-modelkeyguard-change-me-32bytes!!}
export OPENAI_BASE_URL=${OPENAI_BASE_URL:-http://127.0.0.1:8789/v1}
export OPENAI_API_KEY=${OPENAI_API_KEY:-kgw_demo_doc_ingestor}
python scripts/langchain_user_openai_compatible.py
