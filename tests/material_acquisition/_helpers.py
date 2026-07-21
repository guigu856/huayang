"""素材源测试共用的零网络 HTTP 客户端。"""

from __future__ import annotations

from collections.abc import Callable
from types import ModuleType
from typing import Any

import httpx
import pytest

ResponseHandler = Callable[[httpx.Request], httpx.Response]


def json_handler(payload: dict[str, Any], *, status: int = 200) -> ResponseHandler:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, request=request)

    return handle


def html_handler(html: str, *, status: int = 200) -> ResponseHandler:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            text=html,
            headers={"content-type": "text/html"},
            request=request,
        )

    return handle


def patch_async_client(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    handler: ResponseHandler,
    *,
    captured_requests: list[httpx.Request] | None = None,
) -> None:
    class MockAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def __aenter__(self) -> MockAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            del args

        async def get(self, url: str, **kwargs: object) -> httpx.Response:
            params = kwargs.get("params")
            headers = kwargs.get("headers")
            request = httpx.Request(
                "GET",
                url,
                params=params if isinstance(params, dict) else None,
                headers=headers if isinstance(headers, dict) else None,
            )
            if captured_requests is not None:
                captured_requests.append(request)
            return handler(request)

    monkeypatch.setattr(module.httpx, "AsyncClient", MockAsyncClient)
