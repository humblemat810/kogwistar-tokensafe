# Tutorial Index

Start here when you know what you want to prove.

For the single production workflow that covers config, deploy, register, and
use, follow [`../docs_production.md`](../docs_production.md). The entries below
are narrower demos and sub-flows.

```text
First 5 minutes, no real key
  -> slow_quickstart_cli_gui_parity.md

First real admin setup from Keycloak console
  -> keycloak_admin_first_setup.md

Real Kogwistar backend is wired correctly
  -> kogwistar_managed_postgres_setup.md

Real Azure key + real safe token + Kogwistar Postgres
  -> e2e_azure_real_key_usage_and_billing.md

Same real call, but retry cheaply after the first paid call
  -> e2e_azure_real_key_usage_and_billing.md
  -> enable MODELKEYGUARD_LLM_CALL_CACHE=joblib

One deterministic GUI-first Azure flow
  -> e2e_single_azure_gui_key_and_principal.md

Final dev/prod-shaped guard setup
  -> final_dev_guard_azure_real_setup.md

Keycloak/OIDC protects model and admin endpoints
  -> keycloak_oidc_protect_everything.md

Keycloak usage-analysis agent via reusable Python client
  -> usage_analysis_agent.md

Keycloak usage-analysis agent smoke after fresh-up
  -> ../scripts/usage_analysis_agent_smoke.sh

Container hardening and private admin/public serving split
  -> container_hardened_nonadmin_key_safety.md
```

## Pick A Path

| Goal | Real provider key? | Backend | Cache? | Go here |
| --- | --- | --- | --- | --- |
| Full production deploy, register app/key, and use it | Yes | `kogwistar_postgres` | Optional | [../docs_production.md](../docs_production.md) |
| Set up the first Keycloak admin user, ModelKeyGuard user, principal, quotas, and reviewer account | No | `kogwistar_postgres` | No | [keycloak_admin_first_setup.md](keycloak_admin_first_setup.md) |
| Learn the app safely | No | `jsonl` toy mode | No | [slow_quickstart_cli_gui_parity.md](slow_quickstart_cli_gui_parity.md) |
| Verify installed Kogwistar + pgvector + no JSONL graph artifact | No | `kogwistar_postgres` | No | [kogwistar_managed_postgres_setup.md](kogwistar_managed_postgres_setup.md) |
| Real Azure call through Kogwistar-managed Postgres | Yes | `kogwistar_postgres` | No | [e2e_azure_real_key_usage_and_billing.md](e2e_azure_real_key_usage_and_billing.md) |
| Real Azure call once, then replay identical upstream response | Yes | `kogwistar_postgres` | `joblib` | [e2e_azure_real_key_usage_and_billing.md](e2e_azure_real_key_usage_and_billing.md) |
| Register one principal/key in GUI, then call it | Yes or fake | `kogwistar_postgres` in real mode | Optional | [e2e_single_azure_gui_key_and_principal.md](e2e_single_azure_gui_key_and_principal.md) |
| Check billing survives price changes | Yes | same as prior real flow | Optional | [price_change_billing_integrity.md](price_change_billing_integrity.md) |
| Require Keycloak/OIDC for model and admin endpoints | No | `kogwistar_postgres` | No | [keycloak_oidc_protect_everything.md](keycloak_oidc_protect_everything.md) |
| Build a simple usage-analysis agent with Keycloak service-account auth | No | `kogwistar_postgres` | No | [usage_analysis_agent.md](usage_analysis_agent.md) |
| Smoke the usage-analysis agent against a fresh-up compose stack | No | `kogwistar_postgres` | No | [../scripts/usage_analysis_agent_smoke.sh](../scripts/usage_analysis_agent_smoke.sh) |
| Harden container deployment | Yes | `kogwistar_postgres` | No | [container_hardened_nonadmin_key_safety.md](container_hardened_nonadmin_key_safety.md) |

## Kogwistar Real Call Modes

Use the same real-token tutorial for both modes:

[e2e_azure_real_key_usage_and_billing.md](e2e_azure_real_key_usage_and_billing.md)

Without cache:

```bash
export MODELKEYGUARD_STORE='kogwistar_postgres'
export MODELKEYGUARD_DRY_RUN=0
unset MODELKEYGUARD_LLM_CALL_CACHE
```

With joblib cache:

```bash
export MODELKEYGUARD_STORE='kogwistar_postgres'
export MODELKEYGUARD_DRY_RUN=0
export MODELKEYGUARD_LLM_CALL_CACHE='joblib'
export MODELKEYGUARD_LLM_CALL_CACHE_DIR="$PWD/out/llm_call_cache"
```

Clear cached upstream responses whenever you want the next run to call the real
provider again:

```bash
rm -rf "${MODELKEYGUARD_LLM_CALL_CACHE_DIR:-$PWD/out/llm_call_cache}"
```

The full reset script also clears this cache:

```bash
./scripts/reset_local_e2e_state.sh
```

## Reset First

For repeatable from-scratch tutorial runs:

[../scripts/reset_local_e2e_state.sh](../scripts/reset_local_e2e_state.sh)

It stops local gateway/container state, removes local JSONL tutorial artifacts,
removes `out/llm_call_cache`, attempts Postgres graph-table truncation when
configured, and prints `5432`/`8789` port holders.
