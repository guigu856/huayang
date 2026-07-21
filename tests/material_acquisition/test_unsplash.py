"""Unsplash 素材源零网络回归测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import components.material_acquisition.unsplash as unsplash_module
from components.material_acquisition.base import SearchFilters, SourceError
from components.material_acquisition.unsplash import (
    UnsplashSource,
    _build_download_url,
    _matches_orientation,
    _orientation_for_unsplash,
)

from ._helpers import json_handler, patch_async_client


def _unsplash_response() -> dict[str, Any]:
    return {
        "results": [
            {
                "id": "abc123",
                "width": 4000,
                "height": 3000,
                "description": "A beautiful city skyline",
                "alt_description": "city skyline at night",
                "slug": "city-skyline",
                "user": {"name": "Test Photographer", "username": "testuser"},
                "links": {"html": "https://unsplash.com/photos/abc123"},
                "urls": {
                    "raw": "https://images.unsplash.com/photo-123?ixid=test",
                    "regular": "https://images.unsplash.com/photo-123?w=1080",
                    "small": "https://images.unsplash.com/photo-123?w=400",
                    "thumb": "https://images.unsplash.com/photo-123?w=200",
                },
                "likes": 500,
            }
        ]
    }


def test_is_available_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNSPLASH_ACCESS_KEY", "test-key")

    assert UnsplashSource().is_available()


def test_is_available_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNSPLASH_ACCESS_KEY", raising=False)

    assert not UnsplashSource().is_available()


def test_search_images(monkeypatch: pytest.MonkeyPatch) -> None:
    source = UnsplashSource(api_key="test-key")
    patch_async_client(monkeypatch, unsplash_module, json_handler(_unsplash_response()))

    results = asyncio.run(source.search("city skyline", SearchFilters(kind="image")))

    assert len(results) == 1
    assert results[0].source == "unsplash"
    assert results[0].kind == "image"
    assert results[0].creator == "Test Photographer"
    assert "w=1920" in results[0].download_url
    assert "download tracking required" in results[0].license


def test_search_no_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UNSPLASH_ACCESS_KEY", raising=False)
    source = UnsplashSource(api_key=None)

    with pytest.raises(SourceError, match="UNSPLASH_ACCESS_KEY"):
        asyncio.run(source.search("test", SearchFilters(kind="image")))


def test_search_video_kind_returns_empty() -> None:
    source = UnsplashSource(api_key="test-key")

    assert asyncio.run(source.search("test", SearchFilters(kind="video"))) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("landscape", "landscape"),
        ("portrait", "portrait"),
        ("square", "squarish"),
        (None, None),
    ],
)
def test_orientation_for_unsplash(value: str | None, expected: str | None) -> None:
    assert _orientation_for_unsplash(value) == expected


def test_matches_orientation_landscape() -> None:
    assert _matches_orientation("landscape", 1920, 1080) is True
    assert _matches_orientation("landscape", 720, 1280) is False


def test_matches_orientation_none() -> None:
    assert _matches_orientation(None, 1920, 1080) is True


def test_build_download_url() -> None:
    result = _build_download_url(
        "https://images.unsplash.com/photo-123?ixid=test",
        target_width=1920,
    )

    assert "w=1920" in result
    assert "fit=max" in result


def test_build_download_url_empty() -> None:
    assert _build_download_url("") == ""
