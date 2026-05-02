# MonkeyGuard vs Other Solutions

This page compares **MonkeyGuard (Kogwistar ModelKeyGuard)** with common adjacent tools.

Use this as architecture guidance, not vendor criticism: most teams combine multiple layers.

## Quick comparison

| Capability | MonkeyGuard (ModelKeyGuard) | LLM Router only | API Gateway only | Secret Manager only | Policy Engine only | Usage/Billing Analytics only |
| --- | --- | --- | --- | --- | --- | --- |
| Never expose provider API keys to clients | Yes (gateway-held keys) | Sometimes (depends on router design) | Sometimes (depends on design) | No (storage only) | No | No |
| OpenAI-compatible model gateway path | Yes (`/v1`, provider-shaped routes) | Yes | Sometimes | No | No | No |
| Per-user + per-principal + per-key quota enforcement in request path | Yes | Rare | Rare | No | Partial (needs custom integration) | No (usually post-fact) |
| Graph-authoritative audit + usage history | Yes | Partial | Partial (request logs) | No | Partial | Partial |
| Rebuildable named projections for hot-path serving | Yes | No | No | No | No | No |
| Governance reviewer workflows (deterministic + LLM) | Yes | No | No | No | No | No |
| Built-in Keycloak/OIDC integration | Yes | Varies by router | Varies by product | No | No | No |

## Native provider ecosystem comparison (OpenAI, Claude, Azure, AWS, GCP)

This table is intentionally high-level. Native cloud features evolve quickly, so
treat this as architecture guidance rather than a feature-completeness claim.

| Capability | MonkeyGuard (ModelKeyGuard) | OpenAI native stack | Anthropic Claude native stack | Azure native stack | AWS native stack | GCP native stack |
| --- | --- | --- | --- | --- | --- | --- |
| Provider-neutral control plane across multiple providers | Yes | Limited (OpenAI-first) | Limited (Claude-first) | Limited (Azure-centric) | Limited (AWS-centric) | Limited (GCP-centric) |
| Keep client apps away from raw provider API keys | Yes (gateway-held keys) | Possible with your own middleware | Possible with your own middleware | Possible with managed identity + middleware | Possible with IAM + middleware | Possible with IAM + middleware |
| Request-path quotas by user + principal + key | Yes (single policy surface) | Partial, often app-side composition | Partial, often app-side composition | Partial, often cross-service composition | Partial, often cross-service composition | Partial, often cross-service composition |
| Built-in graph-authoritative audit + rebuildable projections | Yes | No (external data stack needed) | No (external data stack needed) | No (external data stack needed) | No (external data stack needed) | No (external data stack needed) |
| Governance reviewer workflows with loop/backoff/breaker semantics | Yes | No (custom implementation) | No (custom implementation) | No (custom implementation) | No (custom implementation) | No (custom implementation) |
| Best fit | Multi-provider governance and spend/risk controls | OpenAI-centric product velocity | Claude-centric product velocity | Azure enterprise integration | AWS enterprise integration | GCP enterprise integration |

## Where each solution fits best

- MonkeyGuard: model-key mediation + policy + quota + governance loops in one control plane.
- LLM router: model/provider routing, fallback, and latency/cost-aware dispatch.
- API gateway: routing, TLS, auth offload, rate limit primitives, edge networking.
- Secret manager: secure storage/rotation of long-lived secrets and certificates.
- Policy engine: reusable policy authoring/evaluation across many services.
- Usage analytics: dashboards, BI, and finance reconciliation.

## Recommended layered architecture

1. Keep MonkeyGuard in front of model providers for key mediation and quota decisions.
2. Add an LLM router if you need advanced multi-model routing/failover policy.
3. Keep your API gateway at the edge for ingress hardening and traffic control.
4. Keep secret manager as source of truth for long-lived secrets.
5. Keep analytics stack for warehouse/reporting and executive dashboards.

## MonkeyGuard + LLM router pattern

- Put MonkeyGuard on the control-plane boundary (identity, ACL, quotas, audit,
  governance reviewer workflows).
- Put the LLM router on execution-plane model choice (fallback, latency policy,
  cost-aware route selection).
- Keep provider API keys resolved only behind MonkeyGuard-controlled boundaries.
- Feed router decision metadata into MonkeyGuard usage/governance streams when
  end-to-end provenance is required.

## Tradeoff summary

- If your main risk is provider-key leakage and uncontrolled spend, MonkeyGuard is the fastest direct control point.
- If your main need is dynamic model/provider routing quality, add an LLM router.
- If your main need is only generic edge routing, an API gateway may be enough.
- If you need cross-domain enterprise policy beyond model traffic, combine MonkeyGuard with a policy engine.
- For serious production, prefer `postgres` / `kogwistar_postgres`; use `jsonl` for tutorials and local demos.
