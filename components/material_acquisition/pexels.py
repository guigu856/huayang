"""Pexels 视频和图片素材源适配器。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx

from components.material_acquisition.base import Candidate, SearchFilters, SourceError

_PEXELS_LICENSE = "Pexels License (free, no attribution required)"
_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
_IMAGE_SEARCH_URL = "https://api.pexels.com/v1/search"


class PexelsSource:
    """需要 ``PEXELS_API_KEY`` 的 Pexels 视频和图片源。"""

    name = "pexels"

    def __init__(self, *, api_key: str | None = None, timeout: float = 30.0) -> None:
        self._api_key = api_key or os.environ.get("PEXELS_API_KEY")
        self._timeout = timeout

    def is_available(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        if not self._api_key:
            raise SourceError("pexels 未配 API key（PEXELS_API_KEY）")

        kind = (filters.kind or "video").lower()
        out: list[Candidate] = []
        if kind in ("video", "any"):
            out.extend(await self._search_videos(query, filters))
        if kind in ("image", "any"):
            out.extend(await self._search_images(query, filters))
        return out

    async def download(self, candidate: Candidate, out_path: Path) -> Path:
        if not candidate.download_url:
            raise SourceError(f"Candidate {candidate.clip_id} has no download_url")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                async with client.stream("GET", candidate.download_url) as response:
                    response.raise_for_status()
                    with open(out_path, "wb") as output:
                        async for chunk in response.aiter_bytes(chunk_size=1 << 16):
                            output.write(chunk)
        except httpx.HTTPError as error:
            raise SourceError(f"pexels 下载失败: {error}") from error
        return out_path

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._api_key or ""}

    async def _search_videos(
        self, query: str, filters: SearchFilters
    ) -> list[Candidate]:
        params: dict[str, Any] = {
            "query": query,
            "per_page": max(1, min(filters.per_page, 80)),
            "page": max(1, filters.page),
        }
        if filters.orientation:
            params["orientation"] = filters.orientation

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    _VIDEO_SEARCH_URL, headers=self._headers(), params=params
                )
        except httpx.HTTPError as error:
            raise SourceError(f"pexels 视频搜索请求失败: {error}") from error
        if response.status_code >= 400:
            raise SourceError(f"pexels 视频搜索状态码 {response.status_code}")

        try:
            data = response.json()
        except ValueError as error:
            raise SourceError("pexels 视频搜索返回了无效 JSON") from error
        videos = data.get("videos", []) or []
        out: list[Candidate] = []
        for video in videos:
            duration = float(video.get("duration", 0) or 0)
            if filters.min_duration is not None and duration < filters.min_duration:
                continue
            if filters.max_duration is not None and duration > filters.max_duration:
                continue

            rendition = _pick_video_rendition(
                video.get("video_files", []) or [],
                min_width=filters.min_width or 0,
            )
            if rendition is None:
                continue

            user = video.get("user") or {}
            out.append(
                Candidate(
                    source=self.name,
                    source_id=str(video.get("id")),
                    source_url=video.get("url", "") or "",
                    download_url=rendition.get("link", "") or "",
                    kind="video",
                    width=int(rendition.get("width") or video.get("width") or 0),
                    height=int(rendition.get("height") or video.get("height") or 0),
                    duration=duration,
                    creator=user.get("name", "") or "",
                    license=_PEXELS_LICENSE,
                    source_tags=_slug_tags_from_url(video.get("url", "") or ""),
                    thumbnail_url=video.get("image", "") or "",
                    extra={
                        "fps": rendition.get("fps"),
                        "rendition_quality": rendition.get("quality"),
                    },
                )
            )
        return out

    async def _search_images(
        self, query: str, filters: SearchFilters
    ) -> list[Candidate]:
        params: dict[str, Any] = {
            "query": query,
            "per_page": max(1, min(filters.per_page, 80)),
            "page": max(1, filters.page),
        }
        if filters.orientation:
            params["orientation"] = filters.orientation

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    _IMAGE_SEARCH_URL, headers=self._headers(), params=params
                )
        except httpx.HTTPError as error:
            raise SourceError(f"pexels 图片搜索请求失败: {error}") from error
        if response.status_code >= 400:
            raise SourceError(f"pexels 图片搜索状态码 {response.status_code}")

        try:
            data = response.json()
        except ValueError as error:
            raise SourceError("pexels 图片搜索返回了无效 JSON") from error
        photos = data.get("photos", []) or []
        out: list[Candidate] = []
        for photo in photos:
            width = int(photo.get("width", 0) or 0)
            height = int(photo.get("height", 0) or 0)
            if filters.min_width is not None and width < filters.min_width:
                continue

            sources = photo.get("src") or {}
            download_url = sources.get("large2x") or sources.get("original") or ""
            if not download_url:
                continue

            out.append(
                Candidate(
                    source=self.name,
                    source_id=str(photo.get("id")),
                    source_url=photo.get("url", "") or "",
                    download_url=download_url,
                    kind="image",
                    width=width,
                    height=height,
                    creator=photo.get("photographer", "") or "",
                    license=_PEXELS_LICENSE,
                    source_tags=(photo.get("alt") or "").strip(),
                    thumbnail_url=sources.get("medium", "") or "",
                    extra={"avg_color": photo.get("avg_color")},
                )
            )
        return out


def _pick_video_rendition(
    files: list[dict[str, Any]],
    *,
    min_width: int = 0,
    max_width: int = 1920,
) -> dict[str, Any] | None:
    candidates = [
        file
        for file in files
        if file.get("link") and int(file.get("width") or 0) >= min_width
    ]
    if not candidates:
        return None
    affordable = [
        file for file in candidates if int(file.get("width") or 0) <= max_width
    ]
    pool = affordable if affordable else candidates
    pool.sort(key=lambda file: int(file.get("width") or 0), reverse=True)
    return pool[0]


def _slug_tags_from_url(url: str) -> str:
    parts = url.rstrip("/").split("/")
    if not parts:
        return ""
    slug = re.sub(r"-\d+$", "", parts[-1])
    return slug.replace("-", " ")
