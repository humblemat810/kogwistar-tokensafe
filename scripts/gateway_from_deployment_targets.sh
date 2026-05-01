#!/usr/bin/env bash
set -euo pipefail

# Gateway-only split-target runner.
# It takes one source of truth, renders the gateway env automatically, and runs
# the gateway-only Compose file. Operators should edit only the targets file or
# export the same variables in their shell/CI job.

usage() {
  cat <<'TXT'
Usage:
  ./scripts/gateway_from_deployment_targets.sh config
  ./scripts/gateway_from_deployment_targets.sh up
  ./scripts/gateway_from_deployment_targets.sh build
  ./scripts/gateway_from_deployment_targets.sh render

Options:
  --env-file PATH   Source deployment targets from PATH.
                    Default: deploy/deployment-targets.env
  --from-env        Source deployment targets from the current environment.
  --out-dir PATH    Rendered env output directory.
                    Default: out/deployment_targets_rendered

Single source of truth:
  Edit only deploy/deployment-targets.env, or export the same variables in CI.
  This script renders component env files and propagates them into Compose.
TXT
}

source_mode="file"
targets_file="${MODELKEYGUARD_DEPLOYMENT_TARGETS_FILE:-deploy/deployment-targets.env}"
out_dir="${MODELKEYGUARD_DEPLOYMENT_RENDER_DIR:-out/deployment_targets_rendered}"
command_name=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    config|up|build|render)
      command_name="$1"
      shift
      ;;
    --env-file)
      targets_file="${2:-}"
      if [[ -z "$targets_file" ]]; then
        echo "--env-file requires a path" >&2
        exit 2
      fi
      source_mode="file"
      shift 2
      ;;
    --from-env)
      source_mode="env"
      shift
      ;;
    --out-dir)
      out_dir="${2:-}"
      if [[ -z "$out_dir" ]]; then
        echo "--out-dir requires a path" >&2
        exit 2
      fi
      shift 2
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

if docker compose version >/dev/null 2>&1; then
  compose_cmd=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose_cmd=(docker-compose)
else
  echo "Neither 'docker compose' nor 'docker-compose' is available." >&2
  exit 1
fi

compose_project_name="${MODELKEYGUARD_COMPOSE_PROJECT_NAME:-$(basename "$(pwd)")}"

if [[ "$source_mode" == "env" ]]; then
  ./scripts/render_deployment_env.sh --from-env "$out_dir"
else
  if [[ ! -f "$targets_file" ]]; then
    cat >&2 <<TXT
deployment targets file not found: ${targets_file}
Create it from the single source-of-truth template:
  cp deploy/deployment-targets.env.example ${targets_file}
  edit ${targets_file}
TXT
    exit 1
  fi
  ./scripts/render_deployment_env.sh --env-file "$targets_file" "$out_dir"
fi

compose_args=(
  -p "$compose_project_name"
  -f deploy/docker-compose.gateway-only.yml
  --env-file "${out_dir}/gateway.env"
  --env-file "${out_dir}/gateway-compose.env"
)

case "$command_name" in
  render)
    ;;
  config)
    "${compose_cmd[@]}" "${compose_args[@]}" config
    ;;
  build)
    "${compose_cmd[@]}" "${compose_args[@]}" build gateway
    ;;
  up)
    "${compose_cmd[@]}" "${compose_args[@]}" up -d --force-recreate gateway
    ;;
esac
