from __future__ import annotations

import argparse
import os

from .analytics import KeycloakServiceAccount
from .usage_agent import UsageAnalysisAgent


def get_agent_token(client_id: str, client_secret: str, *, keycloak_url: str | None = None, realm: str | None = None) -> str:
    svc = KeycloakServiceAccount(
        keycloak_url=keycloak_url or os.getenv("KEYCLOAK_URL", "http://localhost:8080"),
        realm=realm or os.getenv("KEYCLOAK_REALM", "modelguard"),
        client_id=client_id,
        client_secret=client_secret,
        timeout_seconds=int(os.getenv("MODELKEYGUARD_KEYCLOAK_TOKEN_TIMEOUT_SECONDS", "10")),
    )
    return svc.mint_access_token()


def get_agent_token_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="modelkeyguard token",
        description="Mint a Keycloak client_credentials token for a service account.",
    )
    parser.add_argument("--client-id", default=os.getenv("MODELKEYGUARD_OIDC_USAGE_CLIENT_ID", "modelguard-usage-agent"))
    parser.add_argument("--client-secret", default=os.getenv("MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET", ""))
    parser.add_argument("--keycloak-url", default=os.getenv("KEYCLOAK_URL", "http://localhost:8080"))
    parser.add_argument("--realm", default=os.getenv("KEYCLOAK_REALM", "modelguard"))
    args = parser.parse_args(argv)
    if not args.client_secret:
        raise SystemExit("missing client secret: pass --client-secret or set MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET")
    print(get_agent_token(args.client_id, args.client_secret, keycloak_url=args.keycloak_url, realm=args.realm))
    return 0


def usage_analysis_agent_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch usage analytics for a user, principal, or key using the reusable scaffold."
    )
    parser.add_argument("--base-url", default=os.getenv("MODELKEYGUARD_GATEWAY_PUBLIC_URL", "http://127.0.0.1:8789"))
    parser.add_argument("--time-range", default=os.getenv("MODELKEYGUARD_ANALYTICS_TIME_RANGE", "24h"))
    parser.add_argument("--bucket", default=os.getenv("MODELKEYGUARD_ANALYTICS_BUCKET", "hour"))
    parser.add_argument("--bearer-token", default=os.getenv("MODELKEYGUARD_BEARER_TOKEN", "").strip() or None)
    parser.add_argument("--keycloak-url", default=os.getenv("KEYCLOAK_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--realm", default=os.getenv("KEYCLOAK_REALM", "modelguard"))
    parser.add_argument("--client-id", default=os.getenv("MODELKEYGUARD_OIDC_USAGE_CLIENT_ID", "modelguard-usage-agent"))
    parser.add_argument("--client-secret", default=os.getenv("MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET", ""))
    parser.add_argument("--user", action="append", default=[], help="Subject ID in the user lane, e.g. user:alice")
    parser.add_argument("--principal", action="append", default=[], help="Subject ID in the principal lane, e.g. agent:doc-ingestor")
    parser.add_argument("--key", action="append", default=[], help="Subject ID in the key lane, e.g. key:openai:prod")
    args = parser.parse_args(argv)

    agent = _build_usage_agent(args)
    if args.user:
        agent.user_subjects = list(args.user)
    if args.principal:
        agent.principal_subjects = list(args.principal)
    if args.key:
        agent.key_subjects = list(args.key)
    print(agent.render())
    return 0


def _build_usage_agent(args: argparse.Namespace) -> UsageAnalysisAgent:
    os.environ["MODELKEYGUARD_GATEWAY_PUBLIC_URL"] = args.base_url
    os.environ["MODELKEYGUARD_ANALYTICS_TIME_RANGE"] = args.time_range
    os.environ["MODELKEYGUARD_ANALYTICS_BUCKET"] = args.bucket
    os.environ["KEYCLOAK_URL"] = args.keycloak_url
    os.environ["KEYCLOAK_REALM"] = args.realm
    os.environ["MODELKEYGUARD_OIDC_USAGE_CLIENT_ID"] = args.client_id
    os.environ["MODELKEYGUARD_OIDC_USAGE_CLIENT_SECRET"] = args.client_secret
    if args.bearer_token:
        os.environ["MODELKEYGUARD_BEARER_TOKEN"] = args.bearer_token
    agent = UsageAnalysisAgent.from_env()
    if any((args.user, args.principal, args.key)):
        agent.user_subjects = list(args.user)
        agent.principal_subjects = list(args.principal)
        agent.key_subjects = list(args.key)
    return agent
