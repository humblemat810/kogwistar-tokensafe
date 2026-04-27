# PostgreSQL Backend Schema (Current)

This is the **current implemented schema** in `PostgresGraphStateStore._ensure_schema`.

Code reference: [modelkeyguard/postgres_state.py](/home/azureuser/token-safe/modelkeyguard/postgres_state.py#L45)

## ERD (PK/FK-oriented)

Mermaid source:

- [postgres_schema_erd.mmd](/home/azureuser/token-safe/postgres_schema_erd.mmd)

Key point:

- Primary keys are present.
- **No SQL `FOREIGN KEY` constraints are currently declared**.
- Relationships are enforced by application semantics and replay logic (graph-native style), not relational FK enforcement.

## Tables and keys

1. `graph_records`
- PK: `record_seq`
- Purpose: append-first replay log for node/edge/event/projection writes.

2. `graph_nodes`
- PK: `id`
- Purpose: current node projection.

3. `graph_edges`
- PK: `id`
- Purpose: current edge projection.
- Logical refs: `source`, `target` reference `graph_nodes.id` (not SQL FK).

4. `graph_events`
- PK: `id`
- Purpose: append-only event log.
- Logical ref: `subject` often references graph entity ids (not SQL FK).

5. `named_projections`
- Composite PK: (`namespace`, `key`)
- Purpose: rebuildable O(1) serving state (quota windows, policy projections, usage lane heads, history windows, etc.).

## Indexes (current)

- `idx_graph_edges_source_kind` on `graph_edges(source, kind)`
- `idx_graph_edges_target_kind` on `graph_edges(target, kind)`
- `idx_named_projections_namespace` on `named_projections(namespace, updated_at_ms)`

## Why no SQL FKs today

The store is modeled as a graph authority with append/replay semantics. IDs in edges/events/projections are intentionally flexible across heterogeneous record kinds, so strict SQL FK constraints were not added in the current implementation.
