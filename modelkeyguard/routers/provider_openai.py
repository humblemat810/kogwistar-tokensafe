from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, Request


HandleAdapterRoute = Callable[..., Awaitable[Any]]


def create_router(handle_adapter_route: HandleAdapterRoute) -> APIRouter:
    router = APIRouter(tags=["provider-openai"])

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request, authorization: str | None = Header(default=None)):
        return await handle_adapter_route(request, provider="openai", authorization=authorization)

    @router.post("/v1/responses")
    async def responses(request: Request, authorization: str | None = Header(default=None)):
        return await handle_adapter_route(request, provider="openai", authorization=authorization)

    return router
