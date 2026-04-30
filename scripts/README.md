# Script Index

This directory has a mix of runnable demos, deployment helpers, and smoke tests.
The most useful bash entrypoints are below.

## Secret and stack setup

| Script | What it does |
| --- | --- |
| `bootstrap_secrets.sh` | Creates local secret files under `./secrets` if they do not already exist. Local mode is demo-friendly and may create a placeholder provider key. `--production` generates strong graph/admin/Keycloak secrets, does not create placeholder provider keys, and never overwrites existing files. |
| `bootstrap_keycloak_admin_role.sh` | Grants the `model.admin` realm role to the bundled `modelguard-admin` service account so the Keycloak admin bearer-token path can actually authorize `/admin/*` requests. Safe to rerun. |
| `production_compose.sh` | Production-style single-host Compose runner. It bootstraps production secrets, preflights build-context exclusions and required secret files, then runs Compose detached with the hardened override. Use `stop`/`start` to pause and resume containers without teardown, `down` to remove containers/networks, and `fresh-up` for a clean local rehearsal after stale data. By default the Keycloak import is empty of end-user demo accounts; set `MODELKEYGUARD_KEYCLOAK_REALM_IMPORT_FILE=./keycloak/modelguard-realm.beginner.json` if you want beginner seed data. Provider keys are registered later through `/admin/keys`. |
| `render_deployment_env.sh` | Renders split-target `gateway.env`, `postgres.env`, `keycloak.env`, and `gateway-compose.env` from one filled `deployment-targets.env` file. |
| `gateway_from_deployment_targets.sh` | Gateway-only split-target runner. It reads one source of truth, renders component env files, and runs `deploy/docker-compose.gateway-only.yml`. |
| `deploy_remote_stack.sh` | SSH-based deploy wrapper for a same-machine lower-privilege user or another host. It builds the gateway image locally, loads it on the remote Docker host, stages secrets into remote tmpfs for the lifetime of the run, and then runs the local production or gateway-only workflow remotely in a separate remote checkout root (`~/token-safe-deploy` by default). The compose path prints a one-time Keycloak bootstrap admin pair during deploy; copy it when the run finishes. |
| `deployment_smoke.sh` | Shared smoke that checks browser OIDC redirect, CLI token auth, and the usage-analysis agent against a running deployment. |
| `start_postgres.sh` | Starts the pgvector-backed Postgres container or compose service used by the repo. It also prints the DSN and tells you what to export next. |
| `start_keycloak.sh` | Starts only the Keycloak container from the compose stack and waits for the realm discovery endpoint to answer. |
| `start_stack.sh` | Starts the local Postgres + Keycloak compose services together. It is the quick “local infrastructure” launcher. |
| `init_graph.sh` | Initializes the gateway graph state from `config/gateway_policy.json` using the current `MODELKEYGUARD_GRAPH_KEY` and backend settings. |
| `start_gateway.sh` | Starts the FastAPI gateway process with the current environment and policy file. |
| `reset_local_e2e_state.sh` | Stops local services, clears local volumes and out/ artifacts, and resets local graph/audit state for a fresh run. |

## Local quickstarts and smoke tests

| Script | What it does |
| --- | --- |
| `quickstart.sh` | Runs the local toy/demo quickstart end to end. |
| `one_minute_e2e_demo.sh` | A fast local end-to-end demo run. |
| `kogwistar_postgres_no_jsonl_smoke.py` | Verifies the installed-Kogwistar Postgres path and the “no JSONL graph artifact” behavior. |
| `oidc_protect_everything_smoke.sh` | Runs the OIDC-only deployment smoke with Keycloak tokens protecting model and admin routes. |
| `test_chat.sh` | Sends a single OpenAI-compatible chat request using the configured demo token. |
| `inspect_graph.sh` | Dumps graph state so you can see what the backend persisted. |

## Real-provider and tutorial helpers

| Script | What it does |
| --- | --- |
| `get_agent_token.sh` | Fetches a Keycloak access token for a bundled client. If the compose gateway is running, it mints through that container so the token issuer matches the gateway’s Keycloak view. |
| `langchain_user_openai_compatible.py` | Acts like a LangChain/OpenAI client against the gateway. |
| `external_langchain_smoke.py` | Separate smoke path for external-style LangChain requests. |
| `smoke_azure_real_completion.sh` | Checks a real Azure-style completion path. |
| `smoke_langchain_azure_structured_real.py` | Real structured-output smoke for Azure-style usage. |
| `usage_analysis_agent.py` | Minimal reusable Python usage-analysis agent that mints a Keycloak service-account token and reads `/admin/usage.json` for user, principal, or key analytics. |
| `usage_analysis_agent_smoke.sh` | Fresh-up smoke harness for the usage-analysis agent. Run it after `production_compose.sh fresh-up` to prove the reusable agent talks to the live gateway and Keycloak. |
| `register_usage_example.sh` | Demonstrates registering usage/principal/token state. |
| `register_and_run_usage_demo.sh` | Registers sample state and runs a demo request. |
| `alert_review_once.sh` | Runs one alert/review pass over existing graph/audit data. |
| `review_once.sh` | Runs a single review pass. |
| `demo_alert_review.sh` | Demo wrapper for alert review. |
| `demo_quota_cases.sh` | Demo wrapper for quota behavior cases. |

## Host and operational helpers

| Script | What it does |
| --- | --- |
| `host_admin_login_watcher.py` | Watches host security events and forwards them into the admin security intake. |
| `bundle_for_chatgpt.sh` | Bundles a working tree snapshot for offline ChatGPT-style review or handoff. |
| `setup_langchain_smoke_env.sh` | Prepares environment variables for LangChain smoke tests. |

If you are tracing one script from another, this is the order most local flows use:

```text
bootstrap_secrets.sh
bootstrap_keycloak_admin_role.sh
start_postgres.sh or start_stack.sh
init_graph.sh
start_gateway.sh
test_chat.sh or langchain_user_openai_compatible.py
```

For OIDC-only deployment, the runner is:

```text
oidc_protect_everything_smoke.sh
```

For a single-host production-style Compose run, use:

```text
production_compose.sh up
production_compose.sh fresh-up
```

For split-target deployment configuration, use the templates in `deploy/`:

```text
deploy/gateway.env.example
deploy/postgres.env.example
deploy/keycloak.env.example
deploy/docker-compose.gateway-only.yml
```

For SSH-based deployment to another user or another machine, use:

```text
scripts/deploy_remote_stack.sh
scripts/deployment_smoke.sh
```
