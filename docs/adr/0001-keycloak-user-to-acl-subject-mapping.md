# 0001: Keycloak user-to-ACL subject mapping

Status: Accepted

## Context

Keycloak is the authentication source. ModelKeyGuard's ACL and quota model use
separate graph subjects for different runtime roles:

- `user:*` for end users
- `principal:*` for agents, service accounts, and machine identities

The repo also supports safe tokens that can carry `on_behalf_of_user_id`, which
links a machine principal to a human user for quota and usage accounting.

## Decision

When creating or importing a Keycloak identity, choose the ACL subject type
based on what that identity represents:

- human login account -> ACL `user`
- service account, daemon, or AI agent -> ACL `principal`
- agent acting for a human -> `principal` plus `on_behalf_of_user_id=user:*`

Keycloak roles such as `model.admin` and `model.usage.read` remain separate from
subject type. They answer what the identity may do, not whether it is modeled as
a user or principal.

## Consequences

- Human identities can be quotaed as `user:*` regardless of which client or GUI
  they use.
- Machine identities can be quotaed as `principal:*` without pretending they
  are human users.
- A single human can be represented twice when needed: once as the end user and
  once as the agent principal acting on their behalf.
- Browser admin login, CLI admin tokens, and usage-analysis service accounts all
  keep a consistent ACL story because they share the same underlying subject
  model.

