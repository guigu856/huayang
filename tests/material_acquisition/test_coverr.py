"""Coverr 素材源零网络回归测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import components.material_acquisition.coverr as coverr_module
from components.material_acquisition.base import SearchFilters, SourceError
from components.material_acquisition.coverr import CoverrSource

from ._helpers import html_handler, json_handler, patch_async_client


def _coverr_response() -> dict[str, Any]:
    return {
        "hits": [
            {
                "id": "abc123",
                "slug": "city-night-abc123",
                "title": "City at Night",
                "description": "Beautiful city skyline at night",
                "duration": "15.000000",
                "max_width": 1920,
                "max_height": 1080,
                "tags": ["city", "night", "traffic"],
                "base_filename": "coverr-city-night-9359",
                "playback_id": "bRwCn1aXl02R7Fo3gfLtbYfuxu4sbq900P",
                "urls": {
                    "mp4_download": "https://cdn.coverr.co/videos/city-night-1080.mp4"
                },
                "poster": "https://cdn.coverr.co/videos/city/thumbnail?width=1920",
                "thumbnail": "https://cdn.coverr.co/videos/city/thumbnail?width=640",
                "is_premium": False,
                "fps": 25,
            }
        ]
    }


def test_is_unavailable_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COVERR_API_KEY", raising=False)

    assert not CoverrSource().is_available()


def test_is_available_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COVERR_API_KEY", "test-key")

    assert CoverrSource().is_available()


def test_search_videos(monkeypatch: pytest.MonkeyPatch) -> None:
    source = CoverrSource(api_key="test-key")
    captured_requests = []
    patch_async_client(
        monkeypatch,
        coverr_module,
        json_handler(_coverr_response()),
        captured_requests=captured_requests,
    )

    results = asyncio.run(source.search("city night", SearchFilters(kind="video")))

    assert len(results) == 1
    assert results[0].source == "coverr"
    assert results[0].kind == "video"
    assert results[0].duration == 15.0
    assert results[0].download_url == (
        "https://cdn.coverr.co/videos/city-night-1080.mp4"
    )
    assert results[0].width == 1920
    assert results[0].source_url == "https://coverr.co/videos/city-night-abc123"
    assert results[0].license == "unknown"
    assert captured_requests[0].url.params["urls"] == "true"


def test_search_image_kind_returns_empty() -> None:
    source = CoverrSource(api_key="test-key")

    assert asyncio.run(source.search("test", SearchFilters(kind="image"))) == []


def test_search_min_duration_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    source = CoverrSource(api_key="test-key")
    patch_async_client(monkeypatch, coverr_module, json_handler(_coverr_response()))

    results = asyncio.run(
        source.search("city", SearchFilters(kind="video", min_duration=20.0))
    )

    assert results == []


def test_search_invalid_json_is_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = CoverrSource(api_key="test-key")
    patch_async_client(monkeypatch, coverr_module, html_handler("not json"))

    with pytest.raises(SourceError, match="无效 JSON"):
        asyncio.run(source.search("city", SearchFilters(kind="video")))
