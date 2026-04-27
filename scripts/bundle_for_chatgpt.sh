#!/usr/bin/env bash
set -euo pipefail

# Bundle repository code into a single zip for analysis upload.
# Excludes git history and virtual environments. Keeps .env.example files.
#
# Usage:
#   scripts/bundle_for_chatgpt.sh [repo_dir] [output_zip]
# Example:
#   scripts/bundle_for_chatgpt.sh /home/azureuser/token-safe /home/azureuser/token-safe-chatgpt.zip

ROOT="$(cd "${1:-.}" && pwd)"
REPO_NAME="$(basename "$ROOT")"
OUT_INPUT="${2:-$PWD/${REPO_NAME}-chatgpt.zip}"
OUT_DIR="$(cd "$(dirname "$OUT_INPUT")" && pwd)"
OUT="$OUT_DIR/$(basename "$OUT_INPUT")"

TMP_DIR="$(mktemp -d)"
STAGE_DIR="$TMP_DIR/$REPO_NAME"
mkdir -p "$STAGE_DIR"

RSYNC_EXCLUDES=(
  --exclude='.git/'
  --exclude='.venv/'
  --exclude='.venv-*/'
  --exclude='venv/'
  --exclude='env/'
  --exclude='ENV/'
  --exclude='*/.venv/'
  --exclude='*/.venv-*/'
  --exclude='*/venv/'
  --exclude='*/env/'
  --exclude='*/ENV/'
  --exclude='__pycache__/'
  --exclude='*.pyc'
  --exclude='.pytest_cache/'
  --exclude='.mypy_cache/'
  --exclude='.ruff_cache/'
  --exclude='.tox/'
  --exclude='.nox/'
  --exclude='node_modules/'
  --exclude='data/'
  --exclude='out/'
  --exclude='.env*'
  --exclude='*.env'
  --exclude='*.env.*'
  --exclude='*-chatgpt.zip'
)

# If output zip is under the repo root, exclude that exact path too.
if [[ "$OUT" == "$ROOT/"* ]]; then
  OUT_REL="${OUT#$ROOT/}"
  RSYNC_EXCLUDES+=(--exclude="$OUT_REL")
fi

rsync -a "$ROOT"/ "$STAGE_DIR"/ "${RSYNC_EXCLUDES[@]}"

# Keep env examples even though .env* patterns are excluded above.
# Prune unreadable/runtime dirs to avoid noisy permission errors (for example
# docker-owned Postgres bind mounts).
while IFS= read -r -d '' src; do
  rel="${src#$ROOT/}"
  mkdir -p "$STAGE_DIR/$(dirname "$rel")"
  cp "$src" "$STAGE_DIR/$rel"
done < <(
  find "$ROOT" \
    \( -path "$ROOT/.git" -o -path "$ROOT/.venv" -o -path "$ROOT/.venv-langchain-smoke" -o -path "$ROOT/data" -o -path "$ROOT/out" -o -path "$ROOT/node_modules" \) -prune \
    -o -type f \( -name '.env.example' -o -name '*.env.example' \) -print0 \
    2>/dev/null
)

(
  cd "$TMP_DIR"
  zip -qr "$OUT" "$REPO_NAME"
)

rm -rf "$TMP_DIR"
echo "Created: $OUT"
