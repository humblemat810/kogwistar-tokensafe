#!/usr/bin/env python3
from __future__ import annotations

# This CLI is intentionally thin. If you want to write a real analysis agent,
# start in `modelkeyguard/usage_agent.py` and keep this file as the wrapper.
# The reusable scaffold is modelkeyguard.usage_agent.UsageAnalysisAgent.
# CLI options include --base-url, --time-range, --bucket, --bearer-token,
# --keycloak-url, --realm, --client-id, --client-secret, --user, --principal,
# and --key.
from modelkeyguard.tools import usage_analysis_agent_main as main


if __name__ == "__main__":
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
