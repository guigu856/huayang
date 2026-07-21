"""Videvo 视频素材源适配器。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from components.material_acquisition.base import Candidate, SearchFilters, SourceError

_API_URL = "https://api.videvo.net/v1/search"
_LICENSE_ATTR = "Videvo Attribution License (free, attribution required)"
_LICENSE_CC = "Creative Commons 3.0 (CC BY 3.0, attribution required)"


class VidevoSource:
    """需要 ``VIDEVO_API_KEY`` 的 Videvo 视频源。"""

    name = "videvo"

    def __init__(self, *, api_key: str | None = None, timeout: float = 30.0) -> None:
        self._api_key = api_key or os.environ.get("VIDEVO_API_KEY")
        self._timeout = timeout

    def is_available(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        if not self._api_key:
            raise SourceError("videvo 未配 API key（VIDEVO_API_KEY）")
        if (filters.kind or "video").lower() == "image":
            return []

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        params: dict[str, Any] = {
            "query": query,
            "page": max(1, filters.page),
            "per_page": max(1, min(filters.per_page, 50)),
            "license_type": "free",
        }
        if filters.orientation:
            params["orientation"] = filters.orientation

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(_API_URL, headers=headers, params=params)
        except httpx.HTTPError as error:
            raise SourceError(f"videvo 请求失败: {error}") from error
        if response.status_code >= 400:
            raise SourceError(f"videvo 状态码 {response.status_code}")

        try:
            data = response.json()
        except ValueError as error:
            raise SourceError("videvo 返回了无效 JSON") from error
        hits = data.get("data", []) or data.get("results", []) or data.get("clips", []) or []
        out: list[Candidate] = []
        for video in hits:
            duration = float(video.get("duration", 0) or 0)
            if filters.min_duration is not None and duration < filters.min_duration:
                continue
            if filters.max_duration is not None and duration > filters.max_duration:
                continue

            download_url = (
                video.get("download_url", "")
                or video.get("url_hd", "")
                or video.get("url_sd", "")
                or ""
            )
            if not download_url:
                continue

            width = int(video.get("width") or 0)
            height = int(video.get("height") or 0)
            if filters.min_width and width and width < filters.min_width:
                continue

            tags = video.get("tags", "") or video.get("keywords", "") or ""
            if isinstance(tags, list):
                tags = " ".join(tags)
            license_type = (video.get("license_type", "") or "").lower()
            if "creative commons" in license_type or "cc" in license_type:
                license_name = _LICENSE_CC
            elif "attribution" in license_type or "videvo" in license_type:
                license_name = _LICENSE_ATTR
            else:
                continue
            clip_id = str(video.get("id", "") or "")
            source_url = video.get("page_url", "") or video.get("url", "") or ""
            if not clip_id or not source_url:
                continue

            out.append(
                Candidate(
                    source=self.name,
                    source_id=clip_id,
                    source_url=source_url,
                    download_url=download_url,
                    kind="video",
                    width=width,
                    height=height,
                    duration=duration,
                    creator=video.get("author", "") or video.get("user", "") or "",
                    license=license_name,
                    source_tags=f"{video.get('title', '') or ''} {tags}".strip(),
                    thumbnail_url=(
                        video.get("thumbnail_url", "") or video.get("preview", "") or ""
                    ),
                    extra={
                        "resolution": video.get("resolution", ""),
                        "license_type": license_type,
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
            raise SourceError(f"videvo 下载失败: {error}") from error
        return out_path
