# 0004: Policy rules and enforcement order

Status: Accepted

## Context

The gateway enforces several kinds of policy at once:

- identity and auth token validity
- ACL / namespace / model-route authorization
- quota and spend limits
- approval thresholds
- admin-only governance actions

These rules must be deterministic and stable across CLI, GUI, and API paths.

## Decision

Use one shared enforcement order for every serving path:

1. authenticate the caller
2. resolve the subject graph identity
3. apply ACL / namespace / scope checks
4. resolve the requested model to a provider key
5. apply quota checks for token, user, principal, and key lanes
6. apply approval-threshold checks where configured
7. forward to the provider only if all checks pass
8. append audit and usage events for the final decision

Additional rules:

- policy denies are hard failures; there is no silent fallback to a weaker mode
- `token`, `user`, `principal`, and `key` quotas are independent lanes
- `token` caps the issued client credential itself
- `user` caps the end user behind the request
- `principal` caps the service or agent identity making the request
- `key` caps the provider route and the model bundle attached to it
- `approval_threshold_usd` is a soft gate distinct from quota exhaustion
- admin routes share the same command-service logic across CLI, GUI, and API

## Consequences

- Operators can reason about enforcement in one order instead of special-casing
  each client type.
- Browser login, safe-token clients, and usage-analysis service accounts follow
  the same governance path.
- Policy changes remain append-only and auditable.
- Documentation can describe behavior from the same shared rule set that the
  code enforces.

