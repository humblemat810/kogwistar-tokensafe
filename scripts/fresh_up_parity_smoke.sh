#!/usr/bin/env bash
set -euo pipefail

# Smoke-test the lifecycle contract between `up` and `fresh-up`.
# It verifies that:
# - `fresh-up` moves Postgres to a fresh bind mount path
# - `up` and `down` keep using the active fresh bind mount path once one exists
# - repeated `fresh-up` calls create a new bind mount path

usage() {
  cat <<'TXT'
Usage:
  ./scripts/fresh_up_parity_smoke.sh

This smoke test expects the local SSH target to point at the deployment host,
usually localhost in a single-machine rehearsal.
TXT
}

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
  "")
    ;;
  *)
    echo "unknown argument: ${1}" >&2
    usage >&2
    exit 2
    ;;
esac

if ! command -v ssh >/dev/null 2>&1; then
  echo "ssh is required for this smoke test" >&2
  exit 1
fi

ssh_target="${MODELKEYGUARD_DEPLOY_TARGET:-localhost}"

postgres_mount_source() {
  ssh "$ssh_target" "docker inspect token-safe-deploy-postgres-1 --format '{{range .Mounts}}{{if eq .Destination \"/var/lib/postgresql/data\"}}{{.Source}}{{end}}{{end}}'"
}

assert_fresh_mount() {
  case "$1" in
    */out/production_compose_fresh/*/postgres)
      ;;
    *)
      echo "expected fresh postgres bind mount under .../out/production_compose_fresh/.../postgres, got: $1" >&2
      exit 1
      ;;
  esac
}

echo "[1/5] fresh-up"
./scripts/deploy_remote_stack.sh fresh-up --ssh "$ssh_target" --shape compose
first_fresh_mount="$(postgres_mount_source)"
assert_fresh_mount "$first_fresh_mount"

echo "[2/5] up"
./scripts/deploy_remote_stack.sh up --ssh "$ssh_target" --shape compose
up_mount="$(postgres_mount_source)"
if [[ "$up_mount" != "$first_fresh_mount" ]]; then
  echo "expected up to keep the active fresh mount path, got: $up_mount (wanted $first_fresh_mount)" >&2
  exit 1
fi

echo "[3/5] fresh-up again"
./scripts/deploy_remote_stack.sh fresh-up --ssh "$ssh_target" --shape compose
second_fresh_mount="$(postgres_mount_source)"
assert_fresh_mount "$second_fresh_mount"

if [[ "$first_fresh_mount" == "$second_fresh_mount" ]]; then
  echo "fresh-up reused the same Postgres bind mount path twice: $first_fresh_mount" >&2
  exit 1
fi

echo "[4/5] down"
./scripts/deploy_remote_stack.sh down --ssh "$ssh_target" --shape compose

echo "[5/5] up after down"
./scripts/deploy_remote_stack.sh up --ssh "$ssh_target" --shape compose
final_up_mount="$(postgres_mount_source)"
if [[ "$final_up_mount" != "$second_fresh_mount" ]]; then
  echo "expected up after down to keep the most recent fresh mount path, got: $final_up_mount (wanted $second_fresh_mount)" >&2
  exit 1
fi

echo "fresh-up vs up smoke passed"
