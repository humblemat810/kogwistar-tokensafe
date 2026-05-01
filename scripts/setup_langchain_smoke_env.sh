#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PATH="${KGW_LANGCHAIN_SMOKE_VENV:-$ROOT/.venv-langchain-smoke}"
PYTHON_BIN="${PYTHON:-python3}"

echo "[1/4] create venv: $VENV_PATH"
"$PYTHON_BIN" -m venv "$VENV_PATH"

echo "[2/4] upgrade pip"
"$VENV_PATH/bin/python" -m pip install --upgrade pip

echo "[3/4] install smoke requirements"
"$VENV_PATH/bin/python" -m pip install -r "$ROOT/scripts/requirements-langchain-smoke.txt"

echo "[4/4] verify imports"
"$VENV_PATH/bin/python" - <<'PY'
import importlib.util
mods = [
    "langchain_core",
    "langchain_openai",
    "langchain_ollama",
    "langchain_google_genai",
]
missing = [m for m in mods if importlib.util.find_spec(m) is None]
if missing:
    raise SystemExit(f"missing modules: {missing}")
print("ok: smoke environment ready")
PY

echo
echo "activate with:"
echo "  source \"$VENV_PATH/bin/activate\""
