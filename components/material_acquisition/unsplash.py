"""Unsplash 图片素材源适配器。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from components.material_acquisition.base import Candidate, SearchFilters, SourceError

_SEARCH_URL = "https://api.unsplash.com/search/photos"
_LICENSE = "Unsplash API terms (attribution and download tracking required)"
_USER_AGENT = "video-create-material-acquisition/1.0"


class UnsplashSource:
    """需要 ``UNSPLASH_ACCESS_KEY`` 的 Unsplash 图片源。"""

    name = "unsplash"

    def __init__(self, *, api_key: str | None = None, timeout: float = 30.0) -> None:
        self._api_key = api_key or os.environ.get("UNSPLASH_ACCESS_KEY")
        self._timeout = timeout

    def is_available(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        if not self._api_key:
            raise SourceError("unsplash 未配 API key（UNSPLASH_ACCESS_KEY）")
        if (filters.kind or "video").lower() == "video":
            return []

        params: dict[str, Any] = {
            "query": query,
            "page": max(1, filters.page),
            "per_page": max(1, min(filters.per_page, 30)),
            "content_filter": "high",
        }
        orientation = _orientation_for_unsplash(filters.orientation)
        if orientation:
            params["orientation"] = orientation

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    _SEARCH_URL,
                    params=params,
                    headers=self._headers(),
                )
        except httpx.HTTPError as error:
            raise SourceError(f"unsplash 请求失败: {error}") from error
        if response.status_code >= 400:
            raise SourceError(f"unsplash 状态码 {response.status_code}")

        try:
            data = response.json()
        except ValueError as error:
            raise SourceError("unsplash 返回了无效 JSON") from error
        out: list[Candidate] = []
        for photo in data.get("results", []) or []:
            candidate = _photo_to_candidate(photo, filters)
            if candidate is not None:
                out.append(candidate)
        return out

    async def download(self, candidate: Candidate, out_path: Path) -> Path:
        if not candidate.download_url:
            raise SourceError(f"Candidate {candidate.clip_id} has no download_url")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as client:
                async with client.stream(
                    "GET",
                    candidate.download_url,
                    headers={"User-Agent": _USER_AGENT},
                ) as response:
                    response.raise_for_status()
                    with open(out_path, "wb") as output:
                        async for chunk in response.aiter_bytes(chunk_size=1 << 16):
                            output.write(chunk)
        except httpx.HTTPError as error:
            raise SourceError(f"unsplash 下载失败: {error}") from error
        return out_path

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise SourceError("UNSPLASH_ACCESS_KEY not set")
        return {
            "Authorization": f"Client-ID {self._api_key}",
            "Accept-Version": "v1",
            "User-Agent": _USER_AGENT,
        }


def _orientation_for_unsplash(orientation: str | None) -> str | None:
    if not orientation:
        return None
    return {
        "landscape": "landscape",
        "portrait": "portrait",
        "square": "squarish",
    }.get(orientation)


def _matches_orientation(orientation: str | None, width: int, height: int) -> bool:
    if not orientation or width == 0 or height == 0:
        return True
    if orientation == "landscape":
        return width >= height
    if orientation == "portrait":
        return height > width
    if orientation == "square":
        return abs(width - height) <= max(width, height) * 0.1
    return True


def _build_download_url(raw_url: str, *, target_width: int = 1920) -> str:
    if not raw_url:
        return ""
    parsed = urlparse(raw_url)
    query = dict(parse_qsl(parsed.query))
    query["w"] = str(target_width)
    query["fit"] = "max"
    return urlunparse(parsed._replace(query=urlencode(query)))


def _photo_to_candidate(
    photo: dict[str, Any], filters: SearchFilters
) -> Candidate | None:
    width = int(photo.get("width") or 0)
    height = int(photo.get("height") or 0)
    if filters.min_width is not None and width and width < filters.min_width:
        return None
    if not _matches_orientation(filters.orientation, width, height):
        return None

    user = photo.get("user") or {}
    links = photo.get("links") or {}
    urls = photo.get("urls") or {}
    raw_url = urls.get("raw") or urls.get("regular") or ""
    if not raw_url:
        return None

    source_tags = " ".join(
        part.strip()
        for part in (
            photo.get("description") or "",
            photo.get("alt_description") or "",
            photo.get("slug") or "",
        )
        if part
    ).strip()[:500]

    return Candidate(
        source=UnsplashSource.name,
        source_id=str(photo.get("id") or ""),
        source_url=links.get("html", "") or "",
        download_url=_build_download_url(
            raw_url, target_width=max(filters.min_width or 0, 1920)
        ),
        kind="image",
        width=width,
        height=height,
        creator=user.get("name", "") or "",
        license=_LICENSE,
        source_tags=source_tags,
        thumbnail_url=urls.get("small") or urls.get("thumb") or "",
        extra={
            "user_username": user.get("username", ""),
            "likes": photo.get("likes"),
        },
    )
