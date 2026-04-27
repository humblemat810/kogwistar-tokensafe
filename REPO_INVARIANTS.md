# Repository Invariants

This document records the long-lived invariants agreed for this repository.

It separates **hard invariants** (must hold) from **operational conventions**
(how we run/review safely).

## Hard Invariants

1. Backend mode invariant
- `MODELKEYGUARD_STORE` is validated.
- Supported runtime backends are currently `jsonl` and `postgres`.
- Unknown/unsupported backend values must fail fast (no silent fallback).
- `jsonl` is toy/quickstart/testing mode only.
- Serious flows (real keys, production-shaped runs) must use `postgres`.

2. State authority invariant
- The graph/event stream is the authority.
- Hot reads are served through rebuildable named projections.
- No feature-specific “source of truth” tables are allowed to replace graph authority.

3. Append-only policy invariant
- Runtime policy changes are append-only.
- Quota upsert creates a new revision node; revoke is append-only (`revoked=true`), not hard delete.
- Latest active policy is derived from projection/rebuild logic.

4. Auth/governance flow invariant
- All serving routes use the same governance core:
  `authenticate safe token -> ACL/scope -> quota -> key selection -> provider forward/dry-run -> audit/history`.
- Denied/auth-failed calls are audit events but do not debit usage quotas.

5. Provider correctness invariant
- Provider-native routes enforce provider-specific key matching.
- Provider mismatch must return explicit 4xx (never silently switch providers).
- OpenAI-compatible `/v1/*` routes remain available as universal adapter paths.

6. Secret handling invariant
- Raw provider secrets are never returned by APIs/UI/audit/history responses.
- Graph payloads are sealed at rest with `MODELKEYGUARD_GRAPH_KEY`.
- Changing graph key without reset/reinit must not be silently tolerated.

7. Admin protection invariant
- All `/admin/*` routes require admin auth (session cookie or admin header secret).
- Admin session lifecycle endpoints are authoritative for browser login/logout.
- Security-event ingestion requires separate shared-secret auth.

8. Request/response history invariant
- History capture stores exact request/response body content for supported routes.
- Streaming captures include chunk sequence plus reconstructed output text.
- History data is encrypted/sealed at rest and filterable via projections.
- Provider auth headers/resolved secrets must not be persisted in history.

9. Repeatability/reset invariant
- From-scratch tutorials must be rerun-safe.
- Reset must clean backend state across modes (JSONL artifacts + Postgres graph state when configured/reachable).
- Init/rebuild paths must avoid stale encrypted-state crashes during intended clean reruns.

10. Kogwistar semantics invariant
- Core semantics stay graph-native: nodes, edges, events, and named projections.
- New features should map to these primitives instead of introducing incompatible side stores.

## Operational Conventions

1. Mode separation
- Keep demo/fake (`dry_run`) and real/provider-paid runs clearly separated.
- Do not mix mode assumptions in one runbook section.

2. Single-runbook execution
- For operator workflows, provide one canonical document per flow.
- Steps should be copy-pasteable, idempotent, and explicit about required env values.

3. Placeholder safety
- Documents must clearly mark placeholders and require replacement before execution.
- Avoid guidance that depends on hidden shell/session state.

4. Test pinning
- Invariants are pinned by regression tests; new behavior should include tests for:
  - backend-selection safety,
  - append-only semantics,
  - admin auth coverage,
  - rerun/reset idempotency.

5. Production guidance
- Real deployments should use Postgres-backed graph state and external secret management.
- JSONL remains acceptable only for local toy quickstart and debugging.

