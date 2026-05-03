#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out_root="${1:-$repo_root/out/pypi-dist}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "missing required command: $1" >&2
    exit 1
  fi
}

require_cmd python3
require_cmd rsync

python3 -m pip show build >/dev/null 2>&1 || python3 -m pip install build >/dev/null
python3 -m pip show twine >/dev/null 2>&1 || python3 -m pip install twine >/dev/null

build_one() {
  local project_name="$1"
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' RETURN

  mkdir -p "$tmp_dir/repo"
  rsync -a --delete \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude '.venv-langchain-smoke' \
    --exclude 'out' \
    --exclude 'build' \
    --exclude 'dist' \
    "$repo_root/" "$tmp_dir/repo/"

  PROJECT_NAME="$project_name" python3 - <<'PY' "$tmp_dir/repo/pyproject.toml"
import os
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
target = os.environ["PROJECT_NAME"]
patched, n = re.subn(
    r'(?m)^name = ".*"$',
    f'name = "{target}"',
    text,
    count=1,
)
if n != 1:
    raise SystemExit("failed to patch [project].name in pyproject.toml")
patched, m = re.subn(
    r'(?m)^readme = ".*"$',
    'readme = "README_PYPI.md"',
    patched,
    count=1,
)
if m != 1:
    raise SystemExit("failed to patch [project].readme in pyproject.toml")
path.write_text(patched, encoding="utf-8")
PY

  local out_dir="$out_root/$project_name"
  rm -rf "$out_dir"
  mkdir -p "$out_dir"
  (
    cd "$tmp_dir/repo"
    python3 -m build --sdist --wheel --outdir "$out_dir"
    python3 -m twine check "$out_dir"/*
  )
  echo "built distributions for $project_name in $out_dir"
}

mkdir -p "$out_root"
build_one "kogwistar-modelkeyguard"
build_one "monkeyguard"

echo "all done: $out_root"
