from .analytics import AnalyticsError, KeycloakServiceAccount, UsageAnalyticsClient
from .core import AccessDecision, AuditEvent, ModelKey, ModelKeyGuard, Principal, Request


def __getattr__(name: str):
    """Load governance/LLM sidecars only when their public symbol is used."""
    if name == "UsageAnalysisAgent":
        from .usage_agent import UsageAnalysisAgent

        return UsageAnalysisAgent
    raise AttributeError(name)

__all__ = [
    "AccessDecision",
    "AuditEvent",
    "AnalyticsError",
    "KeycloakServiceAccount",
    "ModelKey",
    "ModelKeyGuard",
    "Principal",
    "Request",
    "UsageAnalysisAgent",
    "UsageAnalyticsClient",
]
