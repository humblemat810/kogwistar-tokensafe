#!/usr/bin/env bash
set -euo pipefail

# Start the pgvector Postgres backend used by serious graph/projection modes.
# Reuses an existing container if present, otherwise launches a fresh one.

if ! docker info >/dev/null 2>&1; then
  cat >&2 <<'TXT'
Unable to connect to the Docker daemon.
If you recently ran `usermod -aG docker $USER`, start a new login shell (or run `newgrp docker`) before retrying.
TXT
  exit 1
fi

TOKENSAFE_HOME="${MODELKEYGUARD_HOME:-${TOKENSAFE_HOME:-$HOME/.tokensafe}}"
export MODELKEYGUARD_POSTGRES_DATA_DIR="${MODELKEYGUARD_POSTGRES_DATA_DIR:-${TOKENSAFE_HOME%/}/postgres}"
mkdir -p "${MODELKEYGUARD_POSTGRES_DATA_DIR}"

if docker compose version >/dev/null 2>&1; then
  compose_cmd=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose_cmd=(docker-compose)
else
  compose_cmd=()
fi

if [[ "${#compose_cmd[@]}" -gt 0 ]]; then
  set +e
  compose_out="$("${compose_cmd[@]}" up -d postgres 2>&1)"
  compose_rc=$?
  set -e
  if [[ "${compose_rc}" -ne 0 ]]; then
    printf '%s\n' "${compose_out}" >&2
    if grep -qi "port is already allocated" <<<"${compose_out}"; then
      cat >&2 <<'TXT'
Port 5432 is already in use on this host.
Either stop the conflicting service/container or use a different port:
  export MODELKEYGUARD_POSTGRES_PORT=5433
  export MODELKEYGUARD_POSTGRES_DSN='postgresql://modelguard:modelguard@localhost:5433/modelguard'
Then rerun ./scripts/start_postgres.sh
TXT
    fi
    exit "${compose_rc}"
  fi
  container_id="$("${compose_cmd[@]}" ps -q postgres)"
else
  container_name="${MODELKEYGUARD_POSTGRES_CONTAINER:-modelkeyguard-postgres}"
  image="${MODELKEYGUARD_POSTGRES_IMAGE:-pgvector/pgvector:pg16}"
  host_port="${MODELKEYGUARD_POSTGRES_PORT:-5432}"
  db="${MODELKEYGUARD_POSTGRES_DB:-modelguard}"
  user="${MODELKEYGUARD_POSTGRES_USER:-modelguard}"
  password="${MODELKEYGUARD_POSTGRES_PASSWORD:-modelguard}"
  data_dir="${MODELKEYGUARD_POSTGRES_DATA_DIR}"
  mkdir -p "${data_dir}"
  if docker ps -a --format '{{.Names}}' | grep -Fxq "${container_name}"; then
    docker start "${container_name}" >/dev/null
  else
    set +e
    run_out="$(docker run -d \
      --name "${container_name}" \
      -e "POSTGRES_DB=${db}" \
      -e "POSTGRES_USER=${user}" \
      -e "POSTGRES_PASSWORD=${password}" \
      -p "${host_port}:5432" \
      -v "${data_dir}:/var/lib/postgresql/data" \
      "${image}" 2>&1)"
    run_rc=$?
    set -e
    if [[ "${run_rc}" -ne 0 ]]; then
      printf '%s\n' "${run_out}" >&2
      if grep -qi "port is already allocated" <<<"${run_out}"; then
        cat >&2 <<'TXT'
Port 5432 is already in use on this host.
Either stop the conflicting service/container or use a different port:
  export MODELKEYGUARD_POSTGRES_PORT=5433
  export MODELKEYGUARD_POSTGRES_DSN='postgresql://modelguard:modelguard@localhost:5433/modelguard'
Then rerun ./scripts/start_postgres.sh
TXT
      fi
      exit "${run_rc}"
    fi
  fi
  container_id="$(docker ps -q -f "name=^${container_name}$")"
fi

if [[ -n "${container_id}" ]]; then
  for _ in $(seq 1 45); do
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")"
    if [[ "${health}" == "healthy" || "${health}" == "running" ]]; then
      break
    fi
    sleep 1
  done
fi

printf 'Postgres data dir: %s\n' "${MODELKEYGUARD_POSTGRES_DATA_DIR}"
printf 'Postgres DSN: postgresql://modelguard:modelguard@localhost:5432/modelguard\n'
