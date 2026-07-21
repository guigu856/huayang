"""多源素材搜索、下载和缩略图抽取门面。"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from components.material_acquisition.archive_org import ArchiveOrgSource
from components.material_acquisition.base import (
    Candidate,
    SearchFilters,
    SourceError,
    StockError,
    StockSource,
)
from components.material_acquisition.coverr import CoverrSource
from components.material_acquisition.mixkit import MixkitSource
from components.material_acquisition.pexels import PexelsSource
from components.material_acquisition.pixabay import PixabayVideoSource
from components.material_acquisition.pond5_pd import Pond5PDSource
from components.material_acquisition.unsplash import UnsplashSource
from components.material_acquisition.videvo import VidevoSource


class DownloadedAsset(BaseModel):
    """搜索并下载后的素材。"""

    path: str
    thumbnail_path: str = ""
    candidate: Candidate


class StockEngine:
    """管理多个素材源并提供统一搜索和下载接口。"""

    def __init__(self, *, sources: Sequence[StockSource] | None = None) -> None:
        self._sources: list[StockSource] = (
            list(sources) if sources is not None else _default_sources()
        )

    @property
    def sources(self) -> list[StockSource]:
        return self._sources

    def available_sources(self) -> list[str]:
        """返回已配置且可调用的素材源名称。"""
        return [source.name for source in self._sources if source.is_available()]

    async def search(
        self,
        query: str,
        filters: SearchFilters | None = None,
        *,
        source_names: list[str] | None = None,
        limit_per_source: int = 3,
    ) -> list[Candidate]:
        """依次搜索可用素材源，并聚合每个源的有限候选。"""
        search_filters = filters or SearchFilters()
        sources = self._select_sources(source_names)
        if not sources:
            raise StockError("无可用素材源")

        errors: list[str] = []
        results: list[Candidate] = []
        for source in sources:
            per_page = max(limit_per_source * 2, search_filters.per_page)
            adjusted_filters = search_filters.model_copy(update={"per_page": per_page})
            try:
                candidates = await source.search(query, adjusted_filters)
            except SourceError as error:
                errors.append(f"{source.name}: {error}")
                continue
            results.extend(candidates[:limit_per_source])

        if not results and errors:
            raise StockError(
                f"全部素材源搜索失败 (query={query!r}): " + " | ".join(errors)
            )
        return results

    async def download(self, candidate: Candidate, out_dir: Path) -> Path:
        """通过候选所属的已注册素材源下载文件。"""
        source = self._find_source_for(candidate)
        if source is None:
            raise SourceError(f"未知素材源: {candidate.source}")

        extension = _guess_ext(candidate)
        out_path = _candidate_download_path(candidate, out_dir, extension)
        out_dir.mkdir(parents=True, exist_ok=True)
        await source.download(candidate, out_path)
        return out_path

    async def search_and_download(
        self,
        query: str,
        out_dir: Path,
        filters: SearchFilters | None = None,
        *,
        limit: int = 3,
        extract_thumbnail: bool = True,
        source_names: list[str] | None = None,
    ) -> list[DownloadedAsset]:
        """搜索最多 ``limit`` 个候选并下载，可选抽取视频缩略图。"""
        if limit <= 0:
            return []

        candidates = await self.search(
            query,
            filters,
            source_names=source_names,
            limit_per_source=limit,
        )
        candidates = candidates[:limit]

        out_dir.mkdir(parents=True, exist_ok=True)
        thumbnails_dir = out_dir / "thumbnails"
        assets: list[DownloadedAsset] = []
        for candidate in candidates:
            try:
                path = await self.download(candidate, out_dir)
            except SourceError:
                continue

            thumbnail_path = ""
            if extract_thumbnail and candidate.kind == "video":
                thumbnail_path = await _extract_thumbnail(path, thumbnails_dir)
            assets.append(
                DownloadedAsset(
                    path=str(path),
                    thumbnail_path=thumbnail_path,
                    candidate=candidate,
                )
            )
        return assets

    def _select_sources(self, source_names: list[str] | None) -> list[StockSource]:
        if source_names is None:
            return [source for source in self._sources if source.is_available()]
        source_by_name = {source.name: source for source in self._sources}
        return [
            source_by_name[name]
            for name in source_names
            if name in source_by_name and source_by_name[name].is_available()
        ]

    def _find_source_for(self, candidate: Candidate) -> StockSource | None:
        for source in self._sources:
            if source.name == candidate.source:
                return source
        return None


def _default_sources() -> list[StockSource]:
    return [
        PexelsSource(),
        PixabayVideoSource(),
        CoverrSource(),
        UnsplashSource(),
        MixkitSource(),
        VidevoSource(),
        Pond5PDSource(),
        ArchiveOrgSource(),
    ]


def _guess_ext(candidate: Candidate) -> str:
    url = candidate.download_url.lower()
    for extension in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"):
        if extension in url:
            return extension
    for extension in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if extension in url:
            return ".jpg" if extension == ".jpeg" else extension
    return ".mp4" if candidate.kind == "video" else ".jpg"


def _candidate_download_path(
    candidate: Candidate,
    out_dir: Path,
    extension: str,
) -> Path:
    """构造受输出根目录约束的安全候选文件路径。"""
    for field, value in (("source", candidate.source), ("source_id", candidate.source_id)):
        if (
            not value
            or value in {".", ".."}
            or len(value) > 200
            or any(ord(character) < 32 for character in value)
            or any(character in '<>:"/\\|?*' for character in value)
        ):
            raise SourceError(f"candidate {field} is not a safe filename component")

    root = out_dir.resolve()
    target = (root / f"{candidate.clip_id}{extension}").resolve()
    if target.parent != root:
        raise SourceError("candidate download path escapes the output directory")
    return target


async def _extract_thumbnail(video_path: Path, thumbnails_dir: Path) -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return ""

    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_path = thumbnails_dir / f"{video_path.stem}.jpg"
    probe = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await probe.communicate()
    midpoint = 1.0
    if stdout:
        try:
            data = json.loads(stdout)
            midpoint = float(data.get("format", {}).get("duration", 2)) / 2
        except (ValueError, KeyError, TypeError):
            pass

    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-ss",
        f"{midpoint:.2f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(thumbnail_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.communicate()
    if thumbnail_path.exists() and thumbnail_path.stat().st_size > 0:
        return str(thumbnail_path)
    return ""
