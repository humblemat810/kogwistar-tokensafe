# 0008: Graph key resolution and sealed payload contract

Status: Accepted

## Context

ModelKeyGuard seals graph payloads at rest. That key is used by multiple runtime
paths:

- local graph state
- Postgres-backed graph state
- installed-Kogwistar Postgres graph state
- gateway startup rehydration

The code previously let those paths drift apart. Some constructors consulted
`MODELKEYGUARD_GRAPH_KEY`, while others accepted `_FILE` config in settings but
did not consistently use the resolved value. That made a deployment vulnerable
to “works in one path, fails after restart in another path” behavior.

## Decision

Use one canonical graph-key resolution path:

- explicit `app_key` wins when provided by code
- otherwise `MODELKEYGUARD_GRAPH_KEY_FILE` is resolved through the existing
  settings reader
- otherwise `MODELKEYGUARD_GRAPH_KEY` is used
- the dev fallback key is allowed only when
  `MODELKEYGUARD_ALLOW_DEV_GRAPH_KEY=1` is set explicitly

Rules:

- graph stores must not read the graph-key environment variables directly
- gateway startup must pass the resolved graph key into the graph store
- serious backends must fail with `graph_key_required` before touching persisted
  state when neither `MODELKEYGUARD_GRAPH_KEY_FILE` nor
  `MODELKEYGUARD_GRAPH_KEY` is configured
- sealed payload failures must fail fast and explain that the persisted data was
  sealed with a different key
- production deployments must keep the key in secret-managed configuration, not
  in scattered constructor-specific env lookups

## Consequences

- the `_FILE` and non-`_FILE` graph-key forms behave consistently
- restart behavior becomes deterministic instead of constructor-specific
- the same persisted graph state can be reopened by every serious backend path
  that shares the same key
- a mismatch is reported as a sealed-payload error, not as a hidden fallback to
  another storage mode
- accidental use of the dev fallback key is loud and opt-in, so local direct
  commands cannot silently decrypt production-like Postgres state with the wrong
  key
