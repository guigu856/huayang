"""Archive.org 视频素材源适配器。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import httpx

from components.material_acquisition.base import Candidate, SearchFilters, SourceError

_SEARCH_URL = "https://archive.org/advancedsearch.php"
_METADATA_URL = "https://archive.org/metadata/{identifier}"
_HTTP_HEADERS = {"User-Agent": "video-create-material-acquisition/1.0"}
_VIDEO_FORMAT_PRIORITY = ["H.264", "h.264", "MPEG4", "mpeg4", "Ogg Theora"]
_MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024
_MIN_DOWNLOAD_BYTES = 1024
_DEFAULT_COLLECTIONS = ["prelinger", "movies", "opensource_movies"]

_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "it",
        "this",
        "that",
        "these",
        "those",
        "as",
        "be",
        "are",
        "was",
        "were",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "can",
        "shall",
        "not",
        "no",
        "yes",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
    }
)
_SOURCE_HINT_TOKENS = frozenset(
    {
        "prelinger",
        "archive",
        "footage",
        "stock",
        "video",
        "film",
        "clip",
        "movie",
        "public",
        "domain",
        "free",
    }
)


class ArchiveOrgSource:
    """无需 API 密钥并保留条目授权字段的 Archive.org 视频源。"""

    name = "archive_org"

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def is_available(self) -> bool:
        return True

    async def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        if (filters.kind or "video").lower() not in ("video", "any"):
            return []

        for _label, solr_query in self._build_queries(query):
            params: list[tuple[str, str | int | float | bool | None]] = [
                ("q", solr_query),
                ("fl[]", "identifier"),
                ("fl[]", "title"),
                ("fl[]", "description"),
                ("fl[]", "creator"),
                ("fl[]", "date"),
                ("fl[]", "subject"),
                ("fl[]", "licenseurl"),
                ("fl[]", "collection"),
                ("rows", str(max(1, min(filters.per_page, 50)))),
                ("page", str(max(1, filters.page))),
                ("output", "json"),
            ]

            try:
                async with httpx.AsyncClient(
                    timeout=self._timeout, headers=_HTTP_HEADERS
                ) as client:
                    response = await client.get(_SEARCH_URL, params=params)
            except httpx.HTTPError:
                continue
            if response.status_code >= 400:
                continue
            try:
                data = response.json()
            except ValueError:
                continue

            docs = (data.get("response") or {}).get("docs", []) or []
            if not docs:
                continue
            out: list[Candidate] = []
            for doc in docs:
                candidate = await self._hydrate_candidate(doc, filters)
                if candidate is not None:
                    out.append(candidate)
            if out:
                return out
        return []

    async def download(self, candidate: Candidate, out_path: Path) -> Path:
        if not candidate.download_url:
            raise SourceError(f"Candidate {candidate.clip_id} has no download_url")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for attempt in range(3):
            if attempt > 0:
                await asyncio.sleep(2.0)
            try:
                async with httpx.AsyncClient(
                    timeout=300.0,
                    follow_redirects=True,
                    headers=_HTTP_HEADERS,
                ) as client:
                    async with client.stream("GET", candidate.download_url) as response:
                        response.raise_for_status()
                        with open(out_path, "wb") as output:
                            async for chunk in response.aiter_bytes(chunk_size=1 << 16):
                                output.write(chunk)
                if out_path.exists() and out_path.stat().st_size >= _MIN_DOWNLOAD_BYTES:
                    return out_path
                size = out_path.stat().st_size if out_path.exists() else 0
                if out_path.exists():
                    out_path.unlink()
                last_error = SourceError(
                    f"archive_org 下载文件过小 ({size} bytes), 可能是错误页面"
                )
            except httpx.HTTPError as error:
                last_error = error
                if out_path.exists():
                    out_path.unlink()

        raise SourceError(f"archive_org 下载失败 (3 次尝试): {last_error}") from last_error

    def _build_queries(self, user_query: str) -> list[tuple[str, str]]:
        collection_query = " OR ".join(
            f"collection:{collection}" for collection in _DEFAULT_COLLECTIONS
        )
        user_query = user_query.strip()
        if not user_query:
            return [("default", f"mediatype:movies AND ({collection_query})")]

        tokens = [
            token
            for token in re.split(r"\s+", user_query)
            if len(token) >= 3
            and token.lower() not in _STOP_WORDS
            and token.lower() not in _SOURCE_HINT_TOKENS
        ]
        if not tokens:
            return [
                (
                    "quoted_fallback",
                    f'mediatype:movies AND ({collection_query}) AND ("{user_query}")',
                )
            ]

        queries: list[tuple[str, str]] = [
            (
                "phrase_prox_10",
                "mediatype:movies "
                f'AND ({collection_query}) AND ("{" ".join(tokens)}"~10)',
            )
        ]

        non_year_tokens = [token for token in tokens if not _looks_like_year(token)]
        if len(non_year_tokens) >= 2:
            distinctive = sorted(non_year_tokens, key=lambda token: -len(token))[:2]
            queries.append(
                (
                    "distinctive_and",
                    f"mediatype:movies AND ({collection_query}) "
                    f"AND ({' AND '.join(distinctive)})",
                )
            )
        elif len(non_year_tokens) == 1:
            queries.append(
                (
                    "single_term",
                    f"mediatype:movies AND ({collection_query}) "
                    f"AND ({non_year_tokens[0]})",
                )
            )

        top_tokens = sorted(tokens, key=lambda token: -len(token))[:3]
        queries.append(
            (
                "distinctive_or",
                f"mediatype:movies AND ({collection_query}) "
                f"AND ({' OR '.join(top_tokens)})",
            )
        )
        return queries

    async def _hydrate_candidate(
        self, doc: dict[str, Any], filters: SearchFilters
    ) -> Candidate | None:
        identifier = doc.get("identifier")
        if not identifier:
            return None

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, headers=_HTTP_HEADERS
            ) as client:
                response = await client.get(_METADATA_URL.format(identifier=identifier))
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        try:
            metadata = response.json()
        except ValueError:
            return None

        video_file = _pick_video_file(metadata.get("files", []) or [])
        if video_file is None:
            return None
        download_url = f"https://archive.org/download/{identifier}/{video_file['name']}"

        duration = (
            _safe_float(metadata.get("runtime"))
            or _safe_float(video_file.get("length"))
            or 0.0
        )
        if filters.min_duration is not None and duration < filters.min_duration:
            return None
        if filters.max_duration is not None and duration > filters.max_duration:
            return None

        creator = doc.get("creator", "") or ""
        if isinstance(creator, list):
            creator = ", ".join(str(item) for item in creator)
        subjects = doc.get("subject", "")
        if isinstance(subjects, list):
            subjects = " ".join(str(item) for item in subjects)

        return Candidate(
            source=self.name,
            source_id=str(identifier),
            source_url=f"https://archive.org/details/{identifier}",
            download_url=download_url,
            kind="video",
            duration=duration,
            creator=str(creator),
            license=str(doc.get("licenseurl", "") or "unknown"),
            source_tags=str(subjects),
            thumbnail_url=f"https://archive.org/services/img/{identifier}",
            extra={
                "title": doc.get("title", "") or identifier,
                "collection": doc.get("collection", ""),
                "date": doc.get("date", ""),
                "format": video_file.get("format", ""),
                "size": video_file.get("size", ""),
            },
        )


def _looks_like_year(token: str) -> bool:
    bare = token.rstrip("sS")
    return bare.isdigit() and len(bare) == 4


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick_video_file(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not files:
        return None

    by_format: dict[str, list[dict[str, Any]]] = {}
    for file in files:
        file_format = (file.get("format") or "").strip()
        file_name = (file.get("name") or "").lower()
        if file_format not in _VIDEO_FORMAT_PRIORITY:
            continue
        if any(tag in file_name for tag in ("thumb", "preview", ".gif", "_meta")):
            continue
        by_format.setdefault(file_format, []).append(file)

    for file_format in _VIDEO_FORMAT_PRIORITY:
        bucket = by_format.get(file_format)
        if not bucket:
            continue
        affordable = [
            file
            for file in bucket
            if 0 < _safe_int(file.get("size")) <= _MAX_FILE_SIZE_BYTES
        ]
        if not affordable:
            continue
        affordable.sort(key=lambda file: _safe_int(file.get("size")), reverse=True)
        return affordable[0]
    return None
