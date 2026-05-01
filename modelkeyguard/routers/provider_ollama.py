from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, Request


HandleAdapterRoute = Callable[..., Awaitable[Any]]


def create_router(handle_adapter_route: HandleAdapterRoute) -> APIRouter:
    router = APIRouter(tags=["provider-ollama"])

    @router.post("/api/chat")
    async def ollama_chat(
        request: Request,
        authorization: str | None = Header(default=None),
        x_modelkeyguard_key_id: str | None = Header(default=None, alias="x-modelkeyguard-key-id"),
    ):
        return await handle_adapter_route(
            request,
            provider="ollama",
            authorization=authorization,
            x_modelkeyguard_key_id=x_modelkeyguard_key_id,
        )

    return router
