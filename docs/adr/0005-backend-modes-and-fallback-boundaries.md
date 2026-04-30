# 0005: Backend modes and fallback boundaries

Status: Accepted

## Context

ModelKeyGuard supports multiple runtime storage modes with different levels of
seriousness:

- `jsonl` for toy/tutorial/local-demo use
- `postgres` for local serious runs implemented in this repo
- `kogwistar_postgres` for delegated serious runs using installed Kogwistar
  Postgres primitives

The repo also carries a vendored Kogwistar clone for reference only, but runtime
must import the installed package, not the repo-local clone.

## Decision

Treat backend mode as an explicit deployment choice, not as a convenience hint.

- `jsonl` is allowed only for toy/demo/tutorial workflows
- `postgres` is the default serious local backend when the repo owns the graph
  state
- `kogwistar_postgres` is the delegated serious backend when installed Kogwistar
  manages the runtime graph primitives
- unsupported backend values must fail fast
- serious modes must not silently fall back to `jsonl`

Additional import rule:

- if `MODELKEYGUARD_KOGWISTAR_ENFORCE_INSTALLED_ONLY=1` is set, runtime must
  resolve Kogwistar from installed package locations only
- the reference clone under `./kogwistar_reference_only` remains semantic
  reference material, not a runtime import source

## Consequences

- toy flows remain easy to run without pretending to be production
- serious runs keep their state authority and hot-read semantics explicit
- deployed behavior does not change behind the operator's back when a serious
  backend is unavailable
- the delegated Kogwistar path stays aligned with the installed package
  boundary rather than a repo-local clone

