"""Pixabay 视频素材源适配器。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from components.material_acquisition.base import Candidate, SearchFilters, SourceError

_PIXABAY_LICENSE = "Pixabay Content License (free, no attribution required)"
_API_URL = "https://pixabay.com/api/videos/"


class PixabayVideoSource:
    """需要 ``PIXABAY_API_KEY`` 的 Pixabay 视频源。"""

    name = "pixabay_video"

    def __init__(self, *, api_key: str | None = None, timeout: float = 30.0) -> None:
        self._api_key = api_key or os.environ.get("PIXABAY_API_KEY")
        self._timeout = timeout

    def is_available(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        if not self._api_key:
            raise SourceError("pixabay 未配 API key（PIXABAY_API_KEY）")
        if (filters.kind or "video").lower() == "image":
            return []

        params: dict[str, Any] = {
            "key": self._api_key,
            "q": query,
            "per_page": max(3, min(filters.per_page, 200)),
            "page": max(1, filters.page),
            "safesearch": "true",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(_API_URL, params=params)
        except httpx.HTTPError as error:
            raise SourceError(f"pixabay 请求失败: {error}") from error
        if response.status_code >= 400:
            raise SourceError(f"pixabay 状态码 {response.status_code}")

        try:
            data = response.json()
        except ValueError as error:
            raise SourceError("pixabay 返回了无效 JSON") from error
        hits = data.get("hits", []) or []
        out: list[Candidate] = []
        for hit in hits:
            duration = float(hit.get("duration", 0) or 0)
            if filters.min_duration is not None and duration < filters.min_duration:
                continue
            if filters.max_duration is not None and duration > filters.max_duration:
                continue

            videos = hit.get("videos", {})
            rendition = _pick_rendition(videos, min_width=filters.min_width or 0)
            if rendition is None:
                continue
            if not _matches_orientation(
                filters.orientation, rendition["width"], rendition["height"]
            ):
                continue

            out.append(
                Candidate(
                    source=self.name,
                    source_id=str(hit.get("id")),
                    source_url=hit.get("pageURL", "") or "",
                    download_url=rendition["url"],
                    kind="video",
                    width=rendition["width"],
                    height=rendition["height"],
                    duration=duration,
                    creator=hit.get("user", "") or "",
                    license=_PIXABAY_LICENSE,
                    source_tags=hit.get("tags", "") or "",
                    thumbnail_url=videos.get("tiny", {}).get("thumbnail", "") or "",
                    extra={
                        "views": hit.get("views"),
                        "downloads": hit.get("downloads"),
                        "rendition_size": rendition.get("size"),
                    },
                )
            )
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
            raise SourceError(f"pixabay 下载失败: {error}") from error
        return out_path


def _matches_orientation(orientation: str | None, width: int, height: int) -> bool:
    if not orientation or width <= 0 or height <= 0:
        return True
    if orientation == "landscape":
        return width >= height
    if orientation == "portrait":
        return height > width
    if orientation == "square":
        return abs(width - height) <= max(width, height) * 0.1
    return True


def _pick_rendition(
    videos: dict[str, Any],
    *,
    min_width: int = 0,
) -> dict[str, Any] | None:
    for tier in ["large", "medium", "small", "tiny"]:
        rendition = videos.get(tier)
        if not rendition or not rendition.get("url"):
            continue
        width = int(rendition.get("width") or 0)
        if width >= min_width:
            return {
                "url": rendition["url"],
                "width": width,
                "height": int(rendition.get("height") or 0),
                "size": rendition.get("size"),
            }
    return None
