#!/usr/bin/env bash
set -euo pipefail
mkdir -p secrets
[ -f secrets/modelkeyguard_graph_key ] || python3 - <<'PY'
import secrets, pathlib
pathlib.Path('secrets/modelkeyguard_graph_key').write_text(secrets.token_urlsafe(48))
PY
[ -f secrets/keycloak_client_secret ] || printf 'gateway-secret\n' > secrets/keycloak_client_secret
[ -f secrets/openai_provider_key ] || printf 'dry-run-placeholder-provider-key\n' > secrets/openai_provider_key
chmod 600 secrets/*
echo "secrets bootstrapped under ./secrets"
