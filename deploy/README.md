# Deployment Target Configuration

This directory contains copy-and-edit templates for split production targets.
Plain Docker Compose is a single-host tool; it cannot place one service on
machine A, another on machine B, and another on machine C by itself.

Use these files as the deployment contract for your runner, orchestrator, or
host-level service manager:

| Target | Template | Purpose |
| --- | --- | --- |
| Gateway on machine A | `gateway.env.example` | Environment contract for the token-safe gateway container. |
| Postgres on machine B | `postgres.env.example` | Minimum pgvector PostgreSQL variables and network expectation. |
| Keycloak/OIDC on machine C | `keycloak.env.example` | Minimum OIDC client/realm contract expected by the gateway. |
| Gateway-only Compose | `docker-compose.gateway-only.yml` | Example Compose file for machine A when Postgres and Keycloak are remote. |

## Machine A: Gateway

Copy `gateway.env.example` into your deployment system, replace every
`<...>` placeholder, mount the referenced secret files, and run the gateway
image.

Example local gateway-only shape:

```bash
docker compose -f deploy/docker-compose.gateway-only.yml --env-file deploy/gateway.env.example config
```

For a real deployment, do not use the example file directly with placeholders.
Render the same variables from CI, Docker secrets, Kubernetes secrets, Vault,
or your platform's secret manager.

## Machine B: Postgres

Run pgvector PostgreSQL where machine A can reach it. The gateway only needs a
DSN:

```text
MODELKEYGUARD_POSTGRES_DSN=postgresql://modelguard:<password>@postgres-b.example:5432/modelguard
```

Use persistent storage, backups, TLS/network policy, and a password managed by
your secret manager.

## Machine C: Keycloak Or OIDC

Run Keycloak or another OIDC provider where machine A can reach it. The gateway
expects:

```text
KEYCLOAK_URL=https://keycloak-c.example
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
