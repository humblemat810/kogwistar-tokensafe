# Deployment Target Configuration

This directory contains copy-and-edit templates for split production targets.
Plain Docker Compose is a single-host tool; it cannot place one service on
machine A, another on machine B, and another on machine C by itself.

Use one source of truth only: `deployment-targets.env`.

```bash
cp deploy/deployment-targets.env.example deploy/deployment-targets.env
# edit deploy/deployment-targets.env
./scripts/gateway_from_deployment_targets.sh config
```

The runner renders and propagates component env files automatically under:

```text
out/deployment_targets_rendered/gateway.env
out/deployment_targets_rendered/postgres.env
out/deployment_targets_rendered/keycloak.env
out/deployment_targets_rendered/gateway-compose.env
```

You can also source targets from CI/exported environment variables:

```bash
export MODELKEYGUARD_POSTGRES_HOST='postgres.internal'
export MODELKEYGUARD_KEYCLOAK_PUBLIC_URL='https://keycloak.example'
# set the remaining variables from deployment-targets.env.example
./scripts/gateway_from_deployment_targets.sh --from-env config
```

The generated files are build artifacts, not operator-edited config.

| Target | Template | Purpose |
| --- | --- | --- |
| Shared target values | `deployment-targets.env.example` | One place to decide machine A/B/C hostnames, ports, realm, and client id. |
| Gateway on machine A | generated `gateway.env` | Environment contract for the token-safe gateway container. |
| Postgres on machine B | generated `postgres.env` | Minimum pgvector PostgreSQL variables and network expectation. |
| Keycloak/OIDC on machine C | generated `keycloak.env` | Minimum OIDC client/realm contract expected by the gateway. |
| Gateway-only Compose | `docker-compose.gateway-only.yml` | Example Compose file for machine A when Postgres and Keycloak are remote. |

The target values must agree across files:

| Target value | Define it in | Reuse it in |
| --- | --- | --- |
| Postgres host/IP | `postgres.env.example` `MODELKEYGUARD_POSTGRES_HOST` | `gateway.env.example` `MODELKEYGUARD_POSTGRES_DSN` |
| Keycloak public URL | `keycloak.env.example` `MODELKEYGUARD_KEYCLOAK_PUBLIC_URL` | `gateway.env.example` `KEYCLOAK_URL` |
| Keycloak realm | `keycloak.env.example` `MODELKEYGUARD_KEYCLOAK_REALM` | `gateway.env.example` `KEYCLOAK_REALM` |
| Introspection client id | `keycloak.env.example` `MODELKEYGUARD_KEYCLOAK_INTROSPECTION_CLIENT_ID` | `gateway.env.example` `KEYCLOAK_INTROSPECTION_CLIENT_ID` |

Start by filling your copied `deployment-targets.env`. Then run
`scripts/gateway_from_deployment_targets.sh config` or `up`. Do not leave the
placeholder hostnames in place; they are intentionally invalid.

## Machine A: Gateway

Copy `gateway.env.example` into your deployment system, replace every
`<...>` placeholder, mount the referenced secret files, and run the gateway
image.

Example gateway-only config check after rendering:

```bash
docker compose \
  -f deploy/docker-compose.gateway-only.yml \
  --env-file deploy/rendered/gateway.env \
  --env-file deploy/rendered/gateway-compose.env \
  config
```

For a real deployment, do not use the example file directly with placeholders.
Render the same variables from CI, Docker secrets, Kubernetes secrets, Vault,
or your platform's secret manager.

## Machine B: Postgres

Run pgvector PostgreSQL where machine A can reach it. The gateway only needs a
DSN:

```text
MODELKEYGUARD_POSTGRES_HOST=<postgres-machine-b-dns-or-ip>
MODELKEYGUARD_POSTGRES_DSN=postgresql://modelguard:<postgres-password>@<postgres-machine-b-dns-or-ip>:5432/modelguard
```

Use persistent storage, backups, TLS/network policy, and a password managed by
your secret manager.

## Machine C: Keycloak Or OIDC

Run Keycloak or another OIDC provider where machine A can reach it. The gateway
expects:

```text
MODELKEYGUARD_KEYCLOAK_PUBLIC_URL=https://<keycloak-machine-c-dns>
KEYCLOAK_URL=https://<keycloak-machine-c-dns>
KEYCLOAK_REALM=modelguard
KEYCLOAK_INTROSPECTION_CLIENT_ID=modelguard-gateway
KEYCLOAK_INTROSPECTION_CLIENT_SECRET_FILE=/run/secrets/keycloak_client_secret
```

The admin client/token must carry the configured role:

```text
MODELKEYGUARD_ADMIN_REQUIRED_ROLE=model.admin
```

OIDC protects the HTTP surface. Host, database, registry, and secret-manager
administrators are still infrastructure trust boundaries.
