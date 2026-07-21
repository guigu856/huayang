"""Pixabay 视频素材源零网络回归测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import components.material_acquisition.pixabay as pixabay_module
from components.material_acquisition.base import SearchFilters, SourceError
from components.material_acquisition.pixabay import PixabayVideoSource, _pick_rendition

from ._helpers import html_handler, json_handler, patch_async_client


def _pixabay_response() -> dict[str, Any]:
    return {
        "hits": [
            {
                "id": 12345,
                "pageURL": "https://pixabay.com/videos/12345/",
                "duration": 12,
                "tags": "city night traffic",
                "user": "TestUser",
                "userImageURL": "https://cdn.pixabay.com/user.jpg",
                "views": 1000,
                "downloads": 500,
                "videos": {
                    "large": {
                        "url": "https://cdn.pixabay.com/large.mp4",
                        "width": 1920,
                        "height": 1080,
                        "size": 50_000_000,
                    },
                    "medium": {
                        "url": "https://cdn.pixabay.com/med.mp4",
                        "width": 1280,
                        "height": 720,
                        "size": 20_000_000,
                    },
                    "small": {
                        "url": "https://cdn.pixabay.com/small.mp4",
                        "width": 960,
                        "height": 540,
                        "size": 10_000_000,
                    },
                    "tiny": {
                        "url": "https://cdn.pixabay.com/tiny.mp4",
                        "width": 640,
                        "height": 360,
                        "size": 5_000_000,
                        "thumbnail": "https://cdn.pixabay.com/tiny_thumb.jpg",
                    },
                },
            }
        ]
    }


def test_is_available_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIXABAY_API_KEY", "test-key")

    assert PixabayVideoSource().is_available()


def test_is_available_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)

    assert not PixabayVideoSource().is_available()


def test_search_videos(monkeypatch: pytest.MonkeyPatch) -> None:
    source = PixabayVideoSource(api_key="test-key")
    patch_async_client(monkeypatch, pixabay_module, json_handler(_pixabay_response()))

    results = asyncio.run(source.search("city night", SearchFilters(kind="video")))

    assert len(results) == 1
    assert results[0].source == "pixabay_video"
    assert results[0].kind == "video"
    assert results[0].duration == 12.0
    assert results[0].download_url == "https://cdn.pixabay.com/large.mp4"
    assert results[0].width == 1920
    assert results[0].thumbnail_url == "https://cdn.pixabay.com/tiny_thumb.jpg"
    assert results[0].license


@pytest.mark.parametrize(
    ("orientation", "width", "height", "expected_count"),
    [
        ("landscape", 1920, 1080, 1),
        ("portrait", 1920, 1080, 0),
        ("portrait", 1080, 1920, 1),
        ("landscape", 1080, 1920, 0),
    ],
)
def test_search_filters_orientation_locally_without_video_type(
    monkeypatch: pytest.MonkeyPatch,
    orientation: str,
    width: int,
    height: int,
    expected_count: int,
) -> None:
    payload = _pixabay_response()
    payload["hits"][0]["videos"]["large"]["width"] = width
    payload["hits"][0]["videos"]["large"]["height"] = height
    captured_requests = []
    source = PixabayVideoSource(api_key="test-key")
    patch_async_client(
        monkeypatch,
        pixabay_module,
        json_handler(payload),
        captured_requests=captured_requests,
    )

    results = asyncio.run(
        source.search(
            "city night",
            SearchFilters(kind="video", orientation=orientation),
        )
    )

    assert len(results) == expected_count
    assert "video_type" not in captured_requests[0].url.params


def test_search_no_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    source = PixabayVideoSource(api_key=None)

    with pytest.raises(SourceError, match="PIXABAY_API_KEY"):
        asyncio.run(source.search("test", SearchFilters()))


def test_search_image_kind_returns_empty() -> None:
    source = PixabayVideoSource(api_key="test-key")

    assert asyncio.run(source.search("test", SearchFilters(kind="image"))) == []


def test_pick_rendition_prefers_large() -> None:
    videos = {
        "large": {"url": "https://x/large.mp4", "width": 1920, "height": 1080},
        "medium": {"url": "https://x/med.mp4", "width": 1280, "height": 720},
    }

    rendition = _pick_rendition(videos, min_width=0)

    assert rendition is not None
    assert rendition["width"] == 1920


def test_pick_rendition_respects_min_width() -> None:
    videos = {
        "large": {"url": "https://x/large.mp4", "width": 1920, "height": 1080},
        "tiny": {"url": "https://x/tiny.mp4", "width": 640, "height": 360},
    }

    rendition = _pick_rendition(videos, min_width=1280)

    assert rendition is not None
    assert rendition["width"] == 1920


def test_pick_rendition_falls_back_to_smaller() -> None:
    videos = {
        "small": {"url": "https://x/small.mp4", "width": 960, "height": 540},
        "tiny": {"url": "https://x/tiny.mp4", "width": 640, "height": 360},
    }

    rendition = _pick_rendition(videos, min_width=0)

    assert rendition is not None
    assert rendition["width"] == 960


def test_pick_rendition_no_match() -> None:
    videos = {"tiny": {"url": "https://x/tiny.mp4", "width": 640, "height": 360}}

    assert _pick_rendition(videos, min_width=1920) is None


def test_pick_rendition_empty() -> None:
    assert _pick_rendition({}, min_width=0) is None


def test_search_invalid_json_is_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = PixabayVideoSource(api_key="test-key")
    patch_async_client(monkeypatch, pixabay_module, html_handler("not json"))

    with pytest.raises(SourceError, match="无效 JSON"):
        asyncio.run(source.search("city", SearchFilters(kind="video")))
