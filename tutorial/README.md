# Tutorial Folder

- `../scripts/reset_local_e2e_state.sh`: recommended preflight reset script for repeatable from-scratch runs (stops local gateway/containers, removes local JSONL artifacts, attempts Postgres graph-table truncation when configured, and prints 5432/8789 port holders).
- `slow_quickstart_cli_gui_parity.md`: step-by-step retry-safe tutorial that maps CLI and GUI flows.
- `e2e_single_azure_gui_key_and_principal.md`: single deterministic flow for registering an app principal, creating Azure key via admin GUI, and calling Azure endpoint with that principal token.
- `e2e_azure_real_key_usage_and_billing.md`: real paid Azure flow from policy file + admin GUI key creation to client call and usage/billing verification (including provider token breakdown extraction from history detail).
- `final_dev_guard_azure_real_setup.md`: final-dev guard setup for real Azure token use with PostgreSQL-backed state, CLI/GUI admin parity, and real smoke tests.
- `e2e_langchain_from_scratch_two_cases.md`: consolidated end-to-end adoption tutorial with two tracks: (1) fake key + local token + JSONL dry-run, and (2) real key + real token + PostgreSQL real-upstream flow.
- `price_change_billing_integrity.md`: validates pre-change vs post-change price accounting remains additive and non-retroactive.
- `container_hardened_nonadmin_key_safety.md`: hardened container deployment pattern with private admin surface and public non-admin model-serving ingress, designed to prevent non-admin key recovery.
