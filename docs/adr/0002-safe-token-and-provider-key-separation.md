# 0002: Safe token and provider key separation

Status: Accepted

## Context

The system has two different credential layers that are easy to confuse:

- a safe token issued by `/admin/policy/tokens`
- a provider key registered by `/admin/keys`

They solve different problems. A safe token is a client credential for a
principal. A provider key is the server-side credential and model bundle that
the gateway uses to reach an upstream model provider.

## Decision

Keep the two credential types separate:

- safe token identifies who is calling and which user/principal quota applies
- provider key identifies which upstream model route exists and which secret is
  sealed for server-side use

The runtime request joins them only when the client sends a model name. The
gateway picks the provider key whose `models` list contains that model name,
then applies ACL and quota checks before forwarding.

## Consequences

- Clients never receive the raw provider secret.
- A safe token does not directly name a provider key.
- A provider key does not create a client credential.
- Model-level quota can be applied to the key lane, while user/principal/token
  quotas stay orthogonal.
- Documentation and tooling can explain "who is calling" and "what model route
  exists" separately, which reduces operator confusion.

