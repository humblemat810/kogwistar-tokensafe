# 0007: Fresh-up and up bind-mount parity

Status: Accepted

## Context

The compose deployment has two lifecycle paths:

- `up` and `start`, which resume or recreate the active deployment
- `fresh-up`, which is intended to create a new clean rehearsal root

The repository originally let these paths drift apart. In practice that caused
confusion about whether `fresh-up` should:

- wipe the existing bind-mounted Postgres directory
- create a new bind-mounted rehearsal directory
- preserve the active fresh root across later `down` and `up` calls
- fall back to the default `./data/postgres` bind mount after a fresh run

That ambiguity mattered because the gateway stores authoritative graph state in
Postgres, and stale bind mounts can preserve sealed data across a restart even
when the container itself is recreated.

## Decision

Treat `fresh-up` as an explicit active-root switch, not as a search for the
newest directory and not as an implicit delete of the previous one.

Rules:

- `up` and `start` must reuse the currently active bind mounts.
- `fresh-up` must create a new timestamped rehearsal root for Postgres and
  Keycloak.
- after `fresh-up`, later `down` and `up` must keep using that same fresh root
  until the operator intentionally clears it.
- `fresh-up` must record the active root explicitly instead of discovering it by
  scanning disk.
- the local production runner and the remote deploy wrapper must share the same
  lifecycle semantics.
- `down` is not a data wipe; it only removes containers and networks and
  preserves mapped volumes.
- a separate, explicit cleanup action is required if an operator wants to delete
  stale rehearsal roots from disk.

## Consequences

- `fresh-up` now has a clear operational meaning: create a new active rehearsal
  root.
- normal restart flows remain persistent by default.
- the operator can resume a fresh rehearsal with `stop`/`start` without losing
  the active bind mounts.
- the repo can pin the lifecycle contract with smoke tests instead of relying on
  implicit compose behavior.
- stale bind mounts are less likely to masquerade as a clean reset.
