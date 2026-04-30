# 0003: Graph entities and named projections

Status: Accepted

## Context

ModelKeyGuard uses a graph-native governance model. The system needs to represent
users, principals, applications, safe tokens, provider keys, quota policies, and
usage history in a way that can be rebuilt from authoritative events.

Different runtime backends may store the graph differently, but the semantics
must stay the same:

- nodes and edges are the authoritative model
- events are append-only
- hot reads come from rebuildable named projections

## Decision

Represent governance and usage data as graph entities rather than as separate
feature-specific tables.

Canonical graph subjects include:

- `user:*` for end users
- `principal:*` for agents and service accounts
- `application:*` for client applications
- `auth_token:*` for issued safe tokens
- `key:*` for provider keys
- `quota:*` for quota policy records

Named projections are the serving layer for hot reads such as quota counters,
latest active policy, and usage summaries. They are rebuildable from the graph
record/event stream and are not the source of truth.

## Consequences

- Governance state can be replayed and reconstructed.
- Serving paths can stay fast without becoming the authority themselves.
- The same semantic model works across JSONL, Postgres, and delegated
  Kogwistar-backed modes.
- New features should be added by extending the graph model first, then by
  adding projections when read performance requires it.

