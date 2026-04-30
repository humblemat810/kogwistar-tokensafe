# 0006: Documentation layers and audience split

Status: Accepted

## Context

This repository has several documentation entry points:

- `README.md`
- `docs_quickstart_and_tutorial.md`
- `docs_production.md`
- `docs_usage_registration.md`
- `tutorial/*`
- `docs/adr/*`

Without a stable split, the same workflow can end up described in multiple
places with different levels of detail or different assumptions about the
operator's goal.

## Decision

Use the docs layers with distinct jobs:

- `README.md` is the top-level orientation and capability map
- `docs_quickstart_and_tutorial.md` is the low-friction local start path
- `docs_production.md` is the canonical operator runbook for real deployments
- `docs_usage_registration.md` is the focused user/principal/token walkthrough
- `tutorial/*` are guided learning or scenario-specific walkthroughs
- `docs/adr/*` are the durable semantic decisions and invariants

Rules:

- do not make README the only source of an operational workflow
- do not repeat long semantic explanations in every tutorial
- do keep the production runbook and tutorial examples aligned with the ADRs
- when a behavior is long-lived, document it in an ADR first, then summarize it
  in the runbook/tutorials as needed

## Consequences

- users can find the right level of detail faster
- operational steps stay in one canonical runbook
- tutorials can stay friendly without becoming the source of truth
- ADRs hold the durable semantics, which reduces drift across docs

