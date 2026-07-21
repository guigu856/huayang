"""Archive.org 素材源的级联搜索与文件选择回归测试。"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

import components.material_acquisition.archive_org as archive_module
from components.material_acquisition.archive_org import (
    ArchiveOrgSource,
    _looks_like_year,
    _pick_video_file,
    _safe_int,
)
from components.material_acquisition.base import SearchFilters

from ._helpers import patch_async_client


def _archive_search_response(*, include_license: bool = True) -> dict[str, Any]:
    document: dict[str, Any] = {
        "identifier": "DuckandC1951",
        "title": "Duck and Cover (1951)",
        "creator": "Archer Productions",
        "date": "1951",
        "subject": "Civil defense; Nuclear war",
        "collection": "prelinger",
    }
    if include_license:
        document["licenseurl"] = "https://creativecommons.org/publicdomain/mark/1.0/"
    return {"response": {"docs": [document]}}


def _archive_metadata_response() -> dict[str, Any]:
    return {
        "runtime": "10:00",
        "files": [
            {
                "name": "DuckandC1951_512kb.mp4",
                "format": "MPEG4",
                "size": "50000000",
            },
            {
                "name": "DuckandC1951_h264.mp4",
                "format": "H.264",
                "size": "100000000",
            },
            {"name": "DuckandC1951_thumbs.jpg", "format": "JPEG", "size": "5000"},
            {
                "name": "DuckandC1951_preview.gif",
                "format": "Animated GIF",
                "size": "100000",
            },
        ],
    }


def _archive_handler(*, include_license: bool = True):
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "advancedsearch" in url:
            payload = _archive_search_response(include_license=include_license)
            return httpx.Response(200, json=payload, request=request)
        if "metadata" in url:
            return httpx.Response(200, json=_archive_metadata_response(), request=request)
        return httpx.Response(404, text="not found", request=request)

    return handle


def test_is_available_always_true() -> None:
    assert ArchiveOrgSource().is_available()


def test_search_returns_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    source = ArchiveOrgSource()
    patch_async_client(monkeypatch, archive_module, _archive_handler())

    results = asyncio.run(
        source.search("duck and cover", SearchFilters(kind="video"))
    )

    assert len(results) == 1
    assert results[0].source == "archive_org"
    assert results[0].kind == "video"
    assert results[0].source_id == "DuckandC1951"
    assert "h264" in results[0].download_url.lower()
    assert results[0].source_url == "https://archive.org/details/DuckandC1951"
    assert results[0].license == "https://creativecommons.org/publicdomain/mark/1.0/"


def test_search_missing_license_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    source = ArchiveOrgSource()
    patch_async_client(
        monkeypatch,
        archive_module,
        _archive_handler(include_license=False),
    )

    results = asyncio.run(source.search("duck", SearchFilters(kind="video")))

    assert len(results) == 1
    assert results[0].license == "unknown"


def test_search_empty_query_returns_default() -> None:
    queries = ArchiveOrgSource()._build_queries("")

    assert len(queries) == 1
    assert "default" in queries[0][0]


def test_search_image_kind_returns_empty() -> None:
    source = ArchiveOrgSource()

    assert asyncio.run(source.search("test", SearchFilters(kind="image"))) == []


def test_build_queries_phrase_proximity() -> None:
    queries = ArchiveOrgSource()._build_queries("duck and cover drill")
    labels = [query[0] for query in queries]
    phrase_query = next(query[1] for query in queries if query[0] == "phrase_prox_10")

    assert "phrase_prox_10" in labels
    assert '"duck cover drill"~10' in phrase_query


def test_build_queries_distinctive_and() -> None:
    queries = ArchiveOrgSource()._build_queries("suburban optimism 1955")
    labels = [query[0] for query in queries]
    and_query = next(query[1] for query in queries if query[0] == "distinctive_and")

    assert "distinctive_and" in labels
    assert "1955" not in and_query


def test_build_queries_distinctive_or_fallback() -> None:
    labels = [
        query[0]
        for query in ArchiveOrgSource()._build_queries("suburban optimism 1955")
    ]

    assert "distinctive_or" in labels


def test_build_queries_strips_source_hints() -> None:
    queries = ArchiveOrgSource()._build_queries("prelinger archive footage")

    assert len(queries) == 1
    assert "quoted_fallback" in queries[0][0]


def test_build_queries_single_non_year_token() -> None:
    labels = [
        query[0] for query in ArchiveOrgSource()._build_queries("1950s industrial")
    ]

    assert "single_term" in labels


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1950", True),
        ("1950s", True),
        ("2026", True),
        ("hello", False),
        ("50", False),
        ("19500", False),
    ],
)
def test_looks_like_year(value: str, expected: bool) -> None:
    assert _looks_like_year(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("123", 123), (456, 456), (None, 0), ("abc", 0)],
)
def test_safe_int(value: object, expected: int) -> None:
    assert _safe_int(value) == expected


def test_pick_video_file_prefers_h264() -> None:
    files = [
        {"name": "video_mpeg4.mp4", "format": "MPEG4", "size": "50000000"},
        {"name": "video_h264.mp4", "format": "H.264", "size": "80000000"},
    ]

    result = _pick_video_file(files)

    assert result is not None
    assert result["format"] == "H.264"


def test_pick_video_file_skips_thumbnails() -> None:
    files = [
        {"name": "video_thumb.jpg", "format": "H.264", "size": "5000"},
        {"name": "video_preview.gif", "format": "H.264", "size": "10000"},
        {"name": "video_real.mp4", "format": "H.264", "size": "50000000"},
    ]

    result = _pick_video_file(files)

    assert result is not None
    assert result["name"] == "video_real.mp4"


def test_pick_video_file_size_limit() -> None:
    files = [
        {"name": "huge.mp4", "format": "H.264", "size": str(600 * 1024 * 1024)},
        {"name": "reasonable.mp4", "format": "MPEG4", "size": "40000000"},
    ]

    result = _pick_video_file(files)

    assert result is not None
    assert result["format"] == "MPEG4"


def test_pick_video_file_empty() -> None:
    assert _pick_video_file([]) is None


def test_pick_video_file_no_video_formats() -> None:
    files = [{"name": "audio.mp3", "format": "MP3", "size": "5000000"}]

    assert _pick_video_file(files) is None
