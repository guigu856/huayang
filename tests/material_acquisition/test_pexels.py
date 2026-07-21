"""Pexels 素材源零网络回归测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import components.material_acquisition.pexels as pexels_module
from components.material_acquisition.base import SearchFilters, SourceError
from components.material_acquisition.pexels import (
    PexelsSource,
    _pick_video_rendition,
    _slug_tags_from_url,
)

from ._helpers import html_handler, json_handler, patch_async_client


def _video_response() -> dict[str, Any]:
    return {
        "videos": [
            {
                "id": 1234567,
                "url": "https://www.pexels.com/video/a-city-at-night-1234567/",
                "image": "https://images.pexels.com/1234567.jpeg",
                "duration": 15,
                "width": 1920,
                "height": 1080,
                "user": {"name": "Test User", "url": "https://www.pexels.com/@user"},
                "video_files": [
                    {
                        "link": "https://cdn.pexels.com/hd.mp4",
                        "width": 1920,
                        "height": 1080,
                        "quality": "hd",
                        "fps": 30,
                    },
                    {
                        "link": "https://cdn.pexels.com/sd.mp4",
                        "width": 640,
                        "height": 360,
                        "quality": "sd",
                        "fps": 30,
                    },
                ],
            },
            {
                "id": 8901234,
                "url": "https://www.pexels.com/video/short-clip-8901234/",
                "image": "https://images.pexels.com/8901234.jpeg",
                "duration": 3,
                "width": 1280,
                "height": 720,
                "user": {"name": "Another"},
                "video_files": [
                    {
                        "link": "https://cdn.pexels.com/720.mp4",
                        "width": 1280,
                        "height": 720,
                        "quality": "hd",
                        "fps": 25,
                    }
                ],
            },
        ]
    }


def test_is_available_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")

    assert PexelsSource().is_available()


def test_is_available_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)

    assert not PexelsSource().is_available()


def test_search_videos(monkeypatch: pytest.MonkeyPatch) -> None:
    source = PexelsSource(api_key="test-key")
    patch_async_client(monkeypatch, pexels_module, json_handler(_video_response()))

    results = asyncio.run(source.search("city night", SearchFilters(kind="video")))

    assert len(results) == 2
    assert results[0].source == "pexels"
    assert results[0].kind == "video"
    assert results[0].duration == 15.0
    assert results[0].download_url == "https://cdn.pexels.com/hd.mp4"
    assert results[0].width == 1920
    assert results[0].license


def test_search_no_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    source = PexelsSource(api_key=None)

    with pytest.raises(SourceError, match="PEXELS_API_KEY"):
        asyncio.run(source.search("test", SearchFilters()))


def test_search_min_duration_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    source = PexelsSource(api_key="test-key")
    patch_async_client(monkeypatch, pexels_module, json_handler(_video_response()))

    results = asyncio.run(
        source.search("city", SearchFilters(kind="video", min_duration=10.0))
    )

    assert [candidate.duration for candidate in results] == [15.0]


def test_pick_video_rendition_picks_largest() -> None:
    files = [
        {"link": "https://x/sd.mp4", "width": 640, "height": 360, "quality": "sd"},
        {
            "link": "https://x/hd.mp4",
            "width": 1920,
            "height": 1080,
            "quality": "hd",
        },
    ]

    rendition = _pick_video_rendition(files, min_width=0)

    assert rendition is not None
    assert rendition["width"] == 1920


def test_pick_video_rendition_respects_min_width() -> None:
    files = [
        {"link": "https://x/sd.mp4", "width": 640, "height": 360},
        {"link": "https://x/hd.mp4", "width": 1920, "height": 1080},
    ]

    rendition = _pick_video_rendition(files, min_width=1280)

    assert rendition is not None
    assert rendition["width"] == 1920


def test_pick_video_rendition_empty() -> None:
    assert _pick_video_rendition([], min_width=0) is None


def test_slug_tags_from_url() -> None:
    url = "https://www.pexels.com/video/a-city-at-night-1234567/"

    assert _slug_tags_from_url(url) == "a city at night"


def test_search_invalid_json_is_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
    source = PexelsSource(api_key="test-key")
    patch_async_client(monkeypatch, pexels_module, html_handler("not json"))

    with pytest.raises(SourceError, match="无效 JSON"):
        asyncio.run(source.search("city", SearchFilters(kind="video")))
