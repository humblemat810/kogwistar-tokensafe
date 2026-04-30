#!/usr/bin/env bash
set -euo pipefail

# Build the gateway image locally, load it onto a remote SSH target, and start
# the deployed workflow there.
# This supports:
# - another unprivileged user on the same machine via ssh localhost
# - a single remote machine running the full compose stack
# - a gateway-only split-target host that points at remote Postgres/Keycloak
# Secrets are staged into a remote tmpfs-backed runtime directory for the
# lifetime of the run.
# Cleanup still removes `rm -rf '$remote_root_expanded/secrets'` and
# `rm -rf '$remote_root_expanded/secrets' '$runtime_state_file' '$dir'`.
# The compose path can also propagate MODELKEYGUARD_KEYCLOAK_REALM_IMPORT_FILE.
# Options:
#   --ssh TARGET
#   --remote-root PATH
#   --shape MODE          compose | gateway-only
#   --targets-file PATH
#   --keep-remote
# Keycloak bootstrap admin for this deployment is printed by the installed
# Python command when `up` or `fresh-up` runs in compose mode.

PYTHON_BIN="${PYTHON:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x ".venv/bin/python" ]]; then
    PYTHON_BIN=".venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

exec "${PYTHON_BIN}" -m modelkeyguard.remote_deploy "$@"
