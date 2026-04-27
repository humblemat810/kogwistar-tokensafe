from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, Request


HandleAdapterRoute = Callable[..., Awaitable[Any]]


def create_router(handle_adapter_route: HandleAdapterRoute) -> APIRouter:
    router = APIRouter(tags=["provider-azure"])

    @router.post("/openai/deployments/{deployment}/chat/completions")
    async def azure_chat_completions(
        deployment: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        return await handle_adapter_route(
            request,
            provider="azure_openai",
            authorization=authorization,
            deployment=deployment,
            operation="chat_completions",
        )

    @router.post("/openai/responses")
    async def azure_responses(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        return await handle_adapter_route(
            request,
            provider="azure_openai",
            authorization=authorization,
            operation="responses",
        )

    return router
