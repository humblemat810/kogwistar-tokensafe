# 0009: Explicit model key selection and ambiguity failure

Status: Accepted

## Context

The gateway can expose multiple model keys that all match the same model string
or provider family. That is useful when one model is served by different
hardware or different upstream endpoints.

The earlier behavior was to quietly pick a matching key. That was unsafe when
more than one key could satisfy the same request, because the operator could not
tell which upstream was actually used.

## Decision

Model-key selection must be explicit when the request identifies a key, and
ambiguous matches must fail instead of being guessed.

Rules:

- if the request includes `modelkeyguard_key_id` or `modelkeyguard.key_id`,
  use that exact key when it exists and is allowed
- if a request relies on model/provider matching and more than one key matches,
  return a `model_key_ambiguous` error
- do not silently choose an arbitrary match
- strip the ModelKeyGuard-only key override before forwarding the upstream
  request

## Consequences

- operators can serve the same model from multiple hardware endpoints safely
- requests become predictable and auditable
- the gateway returns a clear error instead of a hidden policy decision when the
  request is underspecified
- tests can pin the ambiguous case and the explicit override case separately
