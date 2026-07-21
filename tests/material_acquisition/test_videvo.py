"""Videvo 素材源零网络回归测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import components.material_acquisition.videvo as videvo_module
from components.material_acquisition.base import SearchFilters, SourceError
from components.material_acquisition.videvo import VidevoSource

from ._helpers import json_handler, patch_async_client


def _videvo_response() -> dict[str, Any]:
    return {
        "data": [
            {
                "id": "vid_001",
                "title": "Aerial City Footage",
                "duration": 20,
                "width": 1920,
                "height": 1080,
                "download_url": "https://cdn.videvo.net/v001.mp4",
                "thumbnail_url": "https://cdn.videvo.net/v001_thumb.jpg",
                "page_url": "https://www.videvo.net/video/v001/",
                "tags": ["aerial", "city", "drone"],
                "license_type": "creative commons 3.0",
                "author": "TestCreator",
                "resolution": "1080p",
            }
        ]
    }


def test_is_available_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEVO_API_KEY", "test-key")

    assert VidevoSource().is_available()


def test_is_available_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEVO_API_KEY", raising=False)

    assert not VidevoSource().is_available()


def test_search_videos(monkeypatch: pytest.MonkeyPatch) -> None:
    source = VidevoSource(api_key="test-key")
    patch_async_client(monkeypatch, videvo_module, json_handler(_videvo_response()))

    results = asyncio.run(source.search("aerial city", SearchFilters(kind="video")))

    assert len(results) == 1
    assert results[0].source == "videvo"
    assert results[0].kind == "video"
    assert results[0].duration == 20.0
    assert results[0].download_url == "https://cdn.videvo.net/v001.mp4"
    assert results[0].width == 1920
    assert "CC BY 3.0" in results[0].license


def test_search_no_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEVO_API_KEY", raising=False)
    source = VidevoSource(api_key=None)

    with pytest.raises(SourceError, match="VIDEVO_API_KEY"):
        asyncio.run(source.search("test", SearchFilters()))


def test_search_image_kind_returns_empty() -> None:
    source = VidevoSource(api_key="test-key")

    assert asyncio.run(source.search("test", SearchFilters(kind="image"))) == []


def test_search_min_duration_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    source = VidevoSource(api_key="test-key")
    patch_async_client(monkeypatch, videvo_module, json_handler(_videvo_response()))

    results = asyncio.run(
        source.search("aerial", SearchFilters(kind="video", min_duration=30.0))
    )

    assert results == []


@pytest.mark.parametrize("missing_field", ["download_url", "license_type", "page_url"])
def test_search_rejects_incomplete_download_or_rights_record(
    monkeypatch: pytest.MonkeyPatch,
    missing_field: str,
) -> None:
    response = _videvo_response()
    response["data"][0].pop(missing_field)
    response["data"][0]["preview_url"] = "https://cdn.videvo.net/preview.mp4"
    source = VidevoSource(api_key="test-key")
    patch_async_client(monkeypatch, videvo_module, json_handler(response))

    results = asyncio.run(source.search("aerial city", SearchFilters(kind="video")))

    assert results == []
