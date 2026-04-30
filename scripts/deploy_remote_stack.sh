#!/usr/bin/env bash
set -euo pipefail

# Build the gateway image locally, load it onto a remote SSH target, and start
# the deployed workflow there.
# This supports:
# - another unprivileged user on the same machine via ssh localhost
# - a single remote machine running the full compose stack
# - a gateway-only split-target host that points at remote Postgres/Keycloak
# Secrets are staged into a remote tmpfs-backed runtime directory so the remote
# target can run the same scripts without leaving static secret files behind.
# Use `down` to stop the stack and clean up the runtime secret staging.

usage() {
  cat <<'TXT'
Usage:
  ./scripts/deploy_remote_stack.sh up
  ./scripts/deploy_remote_stack.sh fresh-up
  ./scripts/deploy_remote_stack.sh start
  ./scripts/deploy_remote_stack.sh stop
  ./scripts/deploy_remote_stack.sh down
  ./scripts/deploy_remote_stack.sh logs
  ./scripts/deploy_remote_stack.sh smoke
  ./scripts/deploy_remote_stack.sh config

Options:
  --ssh TARGET          SSH target such as user@host. Default: localhost
  --remote-root PATH    Remote checkout root. Default: ~/token-safe
  --shape MODE          compose | gateway-only. Default: compose
  --targets-file PATH   Source deployment-targets.env for gateway-only mode.
                        Default: deploy/deployment-targets.env
  --keep-remote         Leave the remote checkout in place on down.

Notes:
  - compose mode runs ./scripts/production_compose.sh on the remote target.
  - gateway-only mode runs ./scripts/gateway_from_deployment_targets.sh on the
    remote target after syncing the repo and rendered deploy inputs.
  - smoke runs ./scripts/deployment_smoke.sh on the remote target.
TXT
}

command_name=""
ssh_target="${MODELKEYGUARD_DEPLOY_TARGET:-localhost}"
remote_root="${MODELKEYGUARD_DEPLOY_ROOT:-~/token-safe}"
shape="${MODELKEYGUARD_DEPLOY_SHAPE:-compose}"
targets_file="${MODELKEYGUARD_DEPLOYMENT_TARGETS_FILE:-deploy/deployment-targets.env}"
keep_remote=0
local_image="${MODELKEYGUARD_IMAGE:-token-safe-gateway:latest}"
keycloak_bootstrap_admin_username="${MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME:-}"
keycloak_bootstrap_admin_password="${MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD:-}"

if [[ $# -eq 0 ]]; then
  usage >&2
  exit 2
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    up|fresh-up|start|stop|down|logs|smoke|config)
      command_name="$1"
      shift
      ;;
    --ssh)
      ssh_target="${2:-}"
      [[ -n "$ssh_target" ]] || { echo "--ssh requires a target" >&2; exit 2; }
      shift 2
      ;;
    --remote-root)
      remote_root="${2:-}"
      [[ -n "$remote_root" ]] || { echo "--remote-root requires a path" >&2; exit 2; }
      shift 2
      ;;
    --shape)
      shape="${2:-}"
      [[ -n "$shape" ]] || { echo "--shape requires a value" >&2; exit 2; }
      shift 2
      ;;
    --targets-file)
      targets_file="${2:-}"
      [[ -n "$targets_file" ]] || { echo "--targets-file requires a path" >&2; exit 2; }
      shift 2
      ;;
    --keep-remote)
      keep_remote=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$command_name" ]]; then
  usage >&2
  exit 2
fi

if ! command -v ssh >/dev/null 2>&1; then
  echo "ssh is required for remote deployment" >&2
  exit 1
fi
if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required for remote deployment" >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

random_secret() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
}

random_username() {
  python3 - <<'PY'
import secrets
import string
alphabet = string.ascii_lowercase + string.digits
print("remote-admin-" + "".join(secrets.choice(alphabet) for _ in range(10)))
PY
}

if [[ -z "$keycloak_bootstrap_admin_username" ]]; then
  keycloak_bootstrap_admin_username="$(random_username)"
fi
if [[ -z "$keycloak_bootstrap_admin_password" ]]; then
  keycloak_bootstrap_admin_password="$(random_secret)"
fi

resolve_remote_root() {
  local remote="$1"
  local path="$2"
  if [[ "$path" == "~"* ]]; then
    local remote_home
    remote_home="$(ssh "$remote" 'printf %s "$HOME"')"
    if [[ "$path" == "~" ]]; then
      echo "$remote_home"
    elif [[ "$path" == "~/"* ]]; then
      echo "${remote_home}${path:1}"
    else
      echo "$path"
    fi
  else
    echo "$path"
  fi
}

remote_root_expanded="$(resolve_remote_root "$ssh_target" "$remote_root")"
runtime_state_file="${remote_root_expanded}/out/.runtime-secret-dir"

sync_repo() {
  local remote="${1}"
  local repo="${2}"
  rsync -a --delete \
    --exclude '.git/' \
    --exclude '.venv/' \
    --exclude 'data/' \
    --exclude 'out/' \
    --exclude 'secrets/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude 'kogwistar_reference_only/' \
    "$repo"/ "$remote":"$remote_root_expanded"/
}

remote_exec() {
  local remote="${1}"
  shift
  ssh "$remote" "cd '$remote_root_expanded' && $*"
}

remote_env_export() {
  local source_file="$1"
  local remote="$2"
  if [[ -f "$source_file" ]]; then
    local remote_rel="${source_file#${repo_root}/}"
    remote_dir="$(dirname "$remote_rel")"
    ssh "$remote" "mkdir -p '$remote_root_expanded/$remote_dir'"
    rsync -a "$source_file" "$remote":"$remote_root_expanded/$remote_rel"
  fi
}

stage_runtime_secrets() {
  local remote="$1"
  ssh "$remote" "mkdir -p '$remote_root_expanded' '$remote_root_expanded/out'"
  runtime_secret_dir="$(ssh "$remote" "if [[ -f '$runtime_state_file' ]]; then cat '$runtime_state_file'; fi" | tr -d '\r\n')"
  if [[ -n "$runtime_secret_dir" ]] && ssh "$remote" "test -d '$runtime_secret_dir'"; then
    if [[ -d "${repo_root}/secrets" ]]; then
      rsync -a --delete "${repo_root}/secrets/" "$remote":"$runtime_secret_dir/"
    fi
  else
    runtime_secret_dir="$(ssh "$remote" "mktemp -d /dev/shm/token-safe-secrets.XXXXXX")"
    if [[ -d "${repo_root}/secrets" ]]; then
      ssh "$remote" "mkdir -p '$runtime_secret_dir'"
      rsync -a --delete "${repo_root}/secrets/" "$remote":"$runtime_secret_dir/"
    fi
    ssh "$remote" "printf '%s\n' '$runtime_secret_dir' > '$runtime_state_file'"
  fi
  ssh "$remote" "rm -rf '$remote_root_expanded/secrets' && ln -s '$runtime_secret_dir' '$remote_root_expanded/secrets' && printf '%s\n' '$runtime_secret_dir' > '$runtime_state_file'"
}

build_local_image() {
  docker build -t "$local_image" "$repo_root"
}

load_remote_image() {
  docker save "$local_image" | ssh "$ssh_target" "docker load"
}

cleanup_runtime_secrets() {
  local remote="$1"
  local dir
  dir="$(ssh "$remote" "if [[ -f '$runtime_state_file' ]]; then cat '$runtime_state_file'; fi" | tr -d '\r\n')"
  if [[ -z "$dir" ]]; then
    return 0
  fi
  ssh "$remote" "rm -rf '$remote_root_expanded/secrets' '$runtime_state_file' '$dir'" >/dev/null 2>&1 || true
}

if [[ "$shape" != "compose" && "$shape" != "gateway-only" ]]; then
  echo "--shape must be either compose or gateway-only" >&2
  exit 2
fi

build_local_image
load_remote_image

sync_repo "$ssh_target" "$repo_root"
stage_runtime_secrets "$ssh_target"

if [[ "$shape" == "gateway-only" ]]; then
  remote_env_export "${repo_root}/${targets_file}" "$ssh_target"
fi

case "$command_name" in
  up)
    if [[ "$shape" == "compose" ]]; then
      remote_exec "$ssh_target" "export MODELKEYGUARD_IMAGE='$local_image' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME='$keycloak_bootstrap_admin_username' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD='$keycloak_bootstrap_admin_password'; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml up -d --no-build"
    else
      remote_exec "$ssh_target" "export MODELKEYGUARD_IMAGE='$local_image'; ./scripts/gateway_from_deployment_targets.sh up --env-file '${targets_file}'"
    fi
    ;;
  fresh-up)
    if [[ "$shape" == "compose" ]]; then
      remote_exec "$ssh_target" "rm -rf data/postgres data/keycloak; mkdir -p data/postgres data/keycloak; export MODELKEYGUARD_IMAGE='$local_image' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME='$keycloak_bootstrap_admin_username' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD='$keycloak_bootstrap_admin_password'; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml down --remove-orphans -v >/dev/null 2>&1 || true; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml up -d --no-build"
    else
      remote_exec "$ssh_target" "rm -rf out/production_compose_fresh; export MODELKEYGUARD_IMAGE='$local_image'; ./scripts/gateway_from_deployment_targets.sh up --env-file '${targets_file}'"
    fi
    ;;
  start)
    if [[ "$shape" == "compose" ]]; then
      remote_exec "$ssh_target" "export MODELKEYGUARD_IMAGE='$local_image' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME='$keycloak_bootstrap_admin_username' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD='$keycloak_bootstrap_admin_password'; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml start"
    else
      remote_exec "$ssh_target" "export MODELKEYGUARD_IMAGE='$local_image'; ./scripts/gateway_from_deployment_targets.sh up --env-file '${targets_file}'"
    fi
    ;;
  stop)
    if [[ "$shape" == "compose" ]]; then
      remote_exec "$ssh_target" "export MODELKEYGUARD_IMAGE='$local_image' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME='$keycloak_bootstrap_admin_username' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD='$keycloak_bootstrap_admin_password'; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml stop"
    else
      remote_exec "$ssh_target" "docker compose -f deploy/docker-compose.gateway-only.yml --env-file out/deployment_targets_rendered/gateway.env --env-file out/deployment_targets_rendered/gateway-compose.env stop"
    fi
    ;;
  down)
    if [[ "$shape" == "compose" ]]; then
      remote_exec "$ssh_target" "export MODELKEYGUARD_IMAGE='$local_image' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME='$keycloak_bootstrap_admin_username' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD='$keycloak_bootstrap_admin_password'; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml down --remove-orphans"
    else
      remote_exec "$ssh_target" "docker compose -f deploy/docker-compose.gateway-only.yml --env-file out/deployment_targets_rendered/gateway.env --env-file out/deployment_targets_rendered/gateway-compose.env down --remove-orphans"
    fi
    cleanup_runtime_secrets
    if [[ "$keep_remote" -eq 0 ]]; then
      remote_exec "$ssh_target" "rm -rf out/deployment_targets_rendered"
    fi
    ;;
  logs)
    if [[ "$shape" == "compose" ]]; then
      remote_exec "$ssh_target" "export MODELKEYGUARD_IMAGE='$local_image' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME='$keycloak_bootstrap_admin_username' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD='$keycloak_bootstrap_admin_password'; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml logs -f"
    else
      remote_exec "$ssh_target" "docker compose -f deploy/docker-compose.gateway-only.yml --env-file out/deployment_targets_rendered/gateway.env --env-file out/deployment_targets_rendered/gateway-compose.env logs -f"
    fi
    ;;
  config)
    if [[ "$shape" == "compose" ]]; then
      remote_exec "$ssh_target" "export MODELKEYGUARD_IMAGE='$local_image' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_USERNAME='$keycloak_bootstrap_admin_username' MODELKEYGUARD_KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD='$keycloak_bootstrap_admin_password'; docker compose -f docker-compose.yml -f docker-compose.container-secure.yml config"
    else
      remote_exec "$ssh_target" "export MODELKEYGUARD_IMAGE='$local_image'; ./scripts/gateway_from_deployment_targets.sh config --env-file '${targets_file}'"
    fi
    ;;
  smoke)
    if [[ "$shape" == "compose" ]]; then
      remote_exec "$ssh_target" "./scripts/deployment_smoke.sh"
    else
      remote_exec "$ssh_target" "export MODELKEYGUARD_IMAGE='$local_image'; set -a; . '${targets_file}'; set +a; ./scripts/deployment_smoke.sh"
    fi
    ;;
esac

if [[ "$shape" == "compose" && ( "$command_name" == "up" || "$command_name" == "fresh-up" ) ]]; then
  cat <<TXT
Keycloak bootstrap admin for this deployment:
  username: ${keycloak_bootstrap_admin_username}
  password: ${keycloak_bootstrap_admin_password}
Keep this pair on the devops side if you need the Keycloak admin console.
TXT
fi
