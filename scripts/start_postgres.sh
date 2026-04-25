#!/usr/bin/env bash
set -euo pipefail
docker compose up -d postgres
printf 'Postgres DSN: postgresql://modelguard:modelguard@localhost:5432/modelguard\n'
