# ModelKeyGuard Schema and Kogwistar-Semantics Map

This document makes backend distinctions explicit:

- `MODELKEYGUARD_STORE=jsonl` is toy/tutorial mode.
- `MODELKEYGUARD_STORE=postgres` is the local serious backend implemented in this repo.
- `MODELKEYGUARD_STORE=kogwistar_postgres` is delegated serious mode using installed Kogwistar Postgres primitives.

## Integration truth table

| Capability | Current implementation in this repo | Import from Kogwistar? |
| --- | --- | --- |
| ACL decision graph | `kogwistar_acl_adapter.py` | Yes; required in `kogwistar_postgres` mode |
| Named projection primitive (`postgres`) | `PostgresGraphStateStore.get_named_projection/replace_named_projection/list_named_projections/clear_named_projection` | No (local implementation, Kogwistar-compatible semantics) |
| Named projection primitive (`kogwistar_postgres`) | `EnginePostgresMetaStore` via installed Kogwistar engine runtime | Yes |
| Graph entities (principal/user/application/token/key/quota) | Graph nodes + edges in `graph_nodes` / `graph_edges` | No direct import needed |

## Logical graph semantics

```mermaid
flowchart LR
    U["end_user node<br/>user:*"] -->|quota edges| UQ["quota_policy node<br/>lane=user"]
    A["application node<br/>app:*"] -->|HAS_PRINCIPAL| P["principal node<br/>agent:* / service:* / human:*"]
    P -->|MEMBER_OF_NAMESPACE| N["namespace node"]
    P -->|quota edges| PQ["quota_policy node<br/>lane=principal"]

    T["auth_token node"] -->|AUTHENTICATES_AS| P
    T -->|ON_BEHALF_OF| U
    T -->|ISSUED_FOR_APPLICATION| A

    K["model_key node"] -->|AVAILABLE_IN| N
    K -->|HAS_QUOTA_POLICY| KQ["quota_policy node<br/>lane=key"]
    P -->|ACL decision path| K
```

## Physical storage

### Local `postgres` backend

```mermaid
flowchart TB
    GR["graph_records<br/>append-first replay log"]
    GN["graph_nodes<br/>current node projection"]
    GE["graph_edges<br/>current edge projection"]
    GEV["graph_events<br/>append-only event log"]
    NP["named_projections<br/>rebuildable O(1) hot-read state"]

    GN --> GR
    GE --> GR
    GEV --> GR
    NP --> GR
```

Notes:

- There are no separate relational tables named `users`, `principals`, or `applications`.
- Those are graph node kinds in `graph_nodes` (`end_user`, `principal`, `application`) linked by typed edges.
- `named_projections` stores active serving views (quota counters, quota-policy latest revision views, usage-lane heads, history index windows).

Complete created-table list (from code scan of `CREATE TABLE` statements):

- `graph_records`
- `graph_nodes`
- `graph_edges`
- `graph_events`
- `named_projections`

### Delegated `kogwistar_postgres` backend

Delegated mode persists graph entities through installed Kogwistar engine primitives
(`GraphKnowledgeEngine` + pgvector backend), and uses Kogwistar meta-store named
projection primitives (`EnginePostgresMetaStore`) for projection reads/writes.

In delegated mode, ModelKeyGuard does not write to local `graph_*` tables.

For PK/index details and logical FK mapping, see:

- [`docs_postgres_schema.md`](docs_postgres_schema.md)

## Mermaid source file

The standalone Mermaid source is also available at:

- [`schema_semantics.mmd`](schema_semantics.mmd)
