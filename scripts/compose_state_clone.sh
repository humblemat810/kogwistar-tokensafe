#!/usr/bin/env bash
set -euo pipefail

# Two-operation snapshot helper for the production compose stack.
# It backs up the mutable deployment state that lives on disk:
# - secrets/
# - data/
# - the active fresh-up root recorded in out/.runtime-compose-data-dir
#
# It does not clone Docker image layers or running container RAM state.
# For this repo, that on-disk state is the part you need to preserve/recover.

usage() {
  cat <<'TXT'
Usage:
  ./scripts/compose_state_clone.sh backup [backup-dir]
  ./scripts/compose_state_clone.sh restore <backup-dir>

What it does:
  - backup: stops the local production compose stack, snapshots the current
    mutable state into a directory, and prints that directory path.
  - restore: stops the local production compose stack, restores the snapshot
    back into the repo, then starts the stack again.

Notes:
  - This snapshots bind-mounted state, not Docker image layers.
  - The backup directory is safe to move or archive with tar afterwards.
TXT
}

command_name="${1:-}"
backup_dir="${2:-}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_state_file="${MODELKEYGUARD_RUNTIME_COMPOSE_DATA_FILE:-${repo_root}/out/.runtime-compose-data-dir}"

copy_tree() {
  local src="$1"
  local dest="$2"
  if [[ ! -e "$src" ]]; then
    return 0
  fi
  mkdir -p "$(dirname "$dest")"
  rm -rf "$dest"
  cp -a "$src" "$dest"
}

stop_stack() {
  if [[ -x "${repo_root}/scripts/production_compose.sh" ]]; then
    "${repo_root}/scripts/production_compose.sh" stop >/dev/null 2>&1 || true
  fi
}

active_root_from_runtime() {
  if [[ ! -f "$runtime_state_file" ]]; then
    return 0
  fi
  tr -d '\r\n' < "$runtime_state_file"
}

if [[ -z "$command_name" || "$command_name" == "-h" || "$command_name" == "--help" ]]; then
  usage
  [[ -n "$command_name" ]] || exit 0
  exit 0
fi

case "$command_name" in
  backup|restore)
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

cd "$repo_root"

case "$command_name" in
  backup)
    if [[ -z "$backup_dir" ]]; then
      backup_dir="${repo_root}/out/compose_state_backups/$(date +%Y%m%d%H%M%S)-$$"
    fi

    stop_stack
    mkdir -p "$backup_dir"

    active_root=""
    if [[ -f "$runtime_state_file" ]]; then
      active_root="$(active_root_from_runtime)"
    fi
    active_root="${active_root#./}"

    copy_tree "${repo_root}/secrets" "${backup_dir}/secrets"
    copy_tree "${repo_root}/data" "${backup_dir}/data"
    copy_tree "$runtime_state_file" "${backup_dir}/out/.runtime-compose-data-dir"
    if [[ -n "$active_root" ]]; then
      copy_tree "${repo_root}/${active_root}" "${backup_dir}/${active_root}"
    fi

    cat >"${backup_dir}/manifest.txt" <<EOF
repo_root=${repo_root}
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
active_root=${active_root}
runtime_state_file=${runtime_state_file}
EOF

    echo "$backup_dir"
    ;;
  restore)
    if [[ -z "$backup_dir" ]]; then
      echo "restore requires a backup-dir" >&2
      usage >&2
      exit 2
    fi
    if [[ ! -d "$backup_dir" ]]; then
      echo "backup-dir not found: $backup_dir" >&2
      exit 1
    fi

    stop_stack

    active_root=""
    if [[ -f "${backup_dir}/out/.runtime-compose-data-dir" ]]; then
      active_root="$(tr -d '\r\n' < "${backup_dir}/out/.runtime-compose-data-dir")"
    fi
    active_root="${active_root#./}"

    copy_tree "${backup_dir}/secrets" "${repo_root}/secrets"
    copy_tree "${backup_dir}/data" "${repo_root}/data"
    copy_tree "${backup_dir}/out/.runtime-compose-data-dir" "${repo_root}/out/.runtime-compose-data-dir"
    if [[ -n "$active_root" ]]; then
      copy_tree "${backup_dir}/${active_root}" "${repo_root}/${active_root}"
    fi

    if [[ -x "${repo_root}/scripts/production_compose.sh" ]]; then
      "${repo_root}/scripts/production_compose.sh" start
    fi
    ;;
esac
