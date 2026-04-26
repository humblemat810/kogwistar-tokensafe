from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, Request


HandleAdapterRoute = Callable[..., Awaitable[Any]]


def create_router(handle_adapter_route: HandleAdapterRoute) -> APIRouter:
    router = APIRouter(tags=["provider-ollama"])

    @router.post("/api/chat")
    async def ollama_chat(request: Request, authorization: str | None = Header(default=None)):
        return await handle_adapter_route(request, provider="ollama", authorization=authorization)

    return router
