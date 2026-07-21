"""Coverr 视频素材源适配器。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from components.material_acquisition.base import Candidate, SearchFilters, SourceError

_SEARCH_URL = "https://coverr.co/api/videos"
_LICENSE = "unknown"
_USER_AGENT = "video-create-material-acquisition/1.0"


class CoverrSource:
    """需要 ``COVERR_API_KEY`` 的 Coverr 视频源。"""

    name = "coverr"

    def __init__(self, *, api_key: str | None = None, timeout: float = 30.0) -> None:
        self._api_key = api_key or os.environ.get("COVERR_API_KEY")
        self._timeout = timeout

    def is_available(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        if (filters.kind or "video").lower() == "image":
            return []
        if not self._api_key:
            raise SourceError("coverr 未配 API key（COVERR_API_KEY）")

        headers = {"User-Agent": _USER_AGENT}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        params: dict[str, Any] = {
            "query": query,
            "page_size": max(1, min(filters.per_page, 25)),
            "page": max(1, filters.page) - 1,
            "urls": "true",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(_SEARCH_URL, headers=headers, params=params)
        except httpx.HTTPError as error:
            raise SourceError(f"coverr 请求失败: {error}") from error
        if response.status_code >= 400:
            raise SourceError(f"coverr 状态码 {response.status_code}")

        try:
            data = response.json()
        except ValueError as error:
            raise SourceError("coverr 返回了无效 JSON") from error
        hits = data.get("hits", []) or []
        out: list[Candidate] = []
        for video in hits:
            duration = float(video.get("duration", 0) or 0)
            if filters.min_duration is not None and duration < filters.min_duration:
                continue
            if filters.max_duration is not None and duration > filters.max_duration:
                continue

            urls = video.get("urls", {}) or {}
            download_url = urls.get("mp4_download", "") or ""
            if not download_url:
                continue

            width = int(video.get("max_width") or 1920)
            height = int(video.get("max_height") or 1080)
            if filters.min_width and width < filters.min_width:
                continue

            tags = video.get("tags", []) or []
            if isinstance(tags, list):
                tags = " ".join(tags)
            source_tags = " ".join(
                part
                for part in (
                    video.get("title", "") or "",
                    video.get("description", "") or "",
                    tags,
                )
                if part
            )
            slug = video.get("slug", "") or ""

            out.append(
                Candidate(
                    source=self.name,
                    source_id=str(video.get("id") or slug),
                    source_url=f"https://coverr.co/videos/{slug}" if slug else "",
                    download_url=download_url,
                    kind="video",
                    width=width,
                    height=height,
                    duration=duration,
                    license=_LICENSE,
                    source_tags=source_tags,
                    thumbnail_url=(
                        video.get("thumbnail", "") or video.get("poster", "") or ""
                    ),
                    extra={
                        "slug": slug,
                        "is_premium": video.get("is_premium"),
                        "fps": video.get("fps"),
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
            raise SourceError(f"coverr 下载失败: {error}") from error
        return out_path
