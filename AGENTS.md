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

## Feature Slice Priority

1. Import-source guard (installed-only Kogwistar)
2. Backend wiring (`kogwistar_postgres`)
3. Embedding-space enforcement
4. CLI/GUI parity hardening
5. Tests + docs
