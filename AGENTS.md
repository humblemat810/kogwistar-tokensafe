# AGENTS

## Hard Runtime Rules

1. Vendored Kogwistar clone is **reference-only**.
- Local clone path: `./kogwistar_reference_only`
- Never import runtime modules from this tree.

2. Runtime Kogwistar source must be pip-installed only.
- Import target must resolve from installed package locations (for example site-packages), not repository-local paths.
- Enforce with `MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY=1` (default).

3. Backend seriousness levels
- Serious backends: `postgres`, `kogwistar_postgres`
- Toy backend: `jsonl` (tutorial/local demo only)
- No silent fallback from serious modes to toy mode.

4. Authority and projection model
- Graph nodes/edges/events remain authoritative.
- Serving reads must use rebuildable named projections for hot paths.

5. Admin channel parity
- CLI/JSON API and GUI/form actions must call the same command service logic.
- Validation/error mapping/audit semantics must stay aligned across channels.
- Registration commands must not silently diverge between direct-store and
  gateway-admin paths in serious backends. If a local gateway is reachable,
  the CLI should prefer it; if a direct local store and the gateway disagree,
  treat that as a bug and pin it with tests.

6. Compose lifecycle semantics
- `./scripts/production_compose.sh down` and the remote wrapper `down` stop/remove
  containers and networks, but they do not imply a data wipe of mapped volumes.
- Use `fresh-up` for an explicit clean rehearsal root or a dedicated reset script
  when you want to clear persistent data.
- Before making compose changes that require `up`/`down` churn, take a state
  snapshot first with `./scripts/compose_state_clone.sh backup`.
- After changing any Dockerfile, docker compose file, or compose-related shell
  script, run `.venv/bin/pytest -q tests/test_oidc_deployment_script.py -q`
  and `bash -n` on the touched shell script(s) before handing off.

## Feature Slice Priority

1. Import-source guard (installed-only Kogwistar)
2. Backend wiring (`kogwistar_postgres`)
3. Embedding-space enforcement
4. CLI/GUI parity hardening
5. Tests + docs
