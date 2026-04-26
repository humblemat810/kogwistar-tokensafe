from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Header, Request


HandleAdapterRoute = Callable[..., Awaitable[Any]]


def create_router(handle_adapter_route: HandleAdapterRoute) -> APIRouter:
    router = APIRouter(tags=["provider-gemini"])

    @router.post("/v1beta/models/{model}:generateContent")
    async def gemini_generate_content(
        model: str,
        request: Request,
        authorization: str | None = Header(default=None),
        x_goog_api_key: str | None = Header(default=None, alias="x-goog-api-key"),
    ):
        return await handle_adapter_route(
            request,
            provider="gemini",
            authorization=authorization,
            route_model=model,
            x_goog_api_key=x_goog_api_key,
        )

    @router.post("/v1beta/models/{model}:streamGenerateContent")
    async def gemini_stream_generate_content(
        model: str,
        request: Request,
        authorization: str | None = Header(default=None),
        x_goog_api_key: str | None = Header(default=None, alias="x-goog-api-key"),
    ):
        return await handle_adapter_route(
            request,
            provider="gemini",
            authorization=authorization,
            route_model=model,
            x_goog_api_key=x_goog_api_key,
            stream=True,
        )

    return router
