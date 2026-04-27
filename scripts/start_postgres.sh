#!/usr/bin/env bash
set -euo pipefail

if ! docker info >/dev/null 2>&1; then
  cat >&2 <<'TXT'
Unable to connect to the Docker daemon.
If you recently ran `usermod -aG docker $USER`, start a new login shell (or run `newgrp docker`) before retrying.
TXT
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  compose_cmd=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose_cmd=(docker-compose)
else
  echo "Neither 'docker compose' nor 'docker-compose' is available." >&2
  exit 1
fi

"${compose_cmd[@]}" up -d postgres

container_id="$("${compose_cmd[@]}" ps -q postgres)"
if [[ -n "${container_id}" ]]; then
  for _ in $(seq 1 30); do
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}")"
    if [[ "${health}" == "healthy" || "${health}" == "running" ]]; then
      break
    fi
    sleep 1
  done
fi

printf 'Postgres DSN: postgresql://modelguard:modelguard@localhost:5432/modelguard\n'
