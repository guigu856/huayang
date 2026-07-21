"""面向 Agent 的素材搜索、候选获取与溯源接口。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError

from .archive_org import ArchiveOrgSource
from .base import Candidate, SearchFilters, SourceError, StockError
from .engine import StockEngine
from .pexels import PexelsSource
from .pixabay import PixabayVideoSource

_CANDIDATE_REF = re.compile(r"^candidate_([0-9a-f]{12})_([0-9a-f]{32})$")
_SCHEMA_VERSION = 1


class MaterialAcquisitionError(RuntimeError):
    """素材获取的稳定错误契约。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class MaterialAcquisitionConfig:
    """素材获取的本地输出配置。"""

    output_dir: Path = Path("output/materials")


@dataclass(frozen=True, slots=True)
class CandidateSummary:
    """可向 Agent 公开的候选信息，不包含下载地址。"""

    candidate_ref: str
    provider: str
    provider_asset_id: str
    source_url: str
    kind: str
    width: int
    height: int
    duration_seconds: float
    creator: str
    license: str
    tags: str
    thumbnail_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_ref": self.candidate_ref,
            "provider": self.provider,
            "provider_asset_id": self.provider_asset_id,
            "source_url": self.source_url,
            "kind": self.kind,
            "width": self.width,
            "height": self.height,
            "duration_seconds": self.duration_seconds,
            "creator": self.creator,
            "license": self.license,
            "tags": self.tags,
            "thumbnail_url": self.thumbnail_url,
        }


@dataclass(frozen=True, slots=True)
class MaterialSearchResult:
    """一次持久化搜索的公开结果。"""

    query: str
    candidates: tuple[CandidateSummary, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class MaterialAcquisitionResult:
    """一个已下载并验证的视频素材。"""

    candidate_ref: str
    file_path: Path
    provenance_path: Path
    sha256: str
    size_bytes: int
    provider: str
    provider_asset_id: str
    source_url: str
    creator: str
    license: str
    search_query: str
    duration_seconds: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_ref": self.candidate_ref,
            "file_path": str(self.file_path),
            "provenance_path": str(self.provenance_path),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "provider": self.provider,
            "provider_asset_id": self.provider_asset_id,
            "source_url": self.source_url,
            "creator": self.creator,
            "license": self.license,
            "search_query": self.search_query,
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
        }


@dataclass(frozen=True, slots=True)
class _VideoMetadata:
    duration_seconds: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None


class MaterialAcquisitionService:
    """将素材源内核限制为 Agent 可安全调用的两阶段流程。"""

    def __init__(
        self,
        config: MaterialAcquisitionConfig | None = None,
        *,
        engine: StockEngine | None = None,
    ) -> None:
        self.config = config or MaterialAcquisitionConfig()
        self.engine = engine or StockEngine(
            sources=[PexelsSource(), PixabayVideoSource(), ArchiveOrgSource()]
        )

    def sources(self) -> dict[str, Any]:
        """返回配置层可尝试的来源及固定工作流说明。"""
        return {
            "available_sources": self.engine.available_sources(),
            "availability_scope": "configured_not_live_health_check",
            "supported_kinds": ["video"],
            "unsupported_kinds": {
                "image": "Agent 获取接口当前仅开放视频素材。",
                "audio": "当前没有带来源与授权记录的音频素材源。",
            },
            "workflow": ["search", "acquire"],
            "rights_gate": {
                "required_fields": [
                    "provider",
                    "provider_asset_id",
                    "source_url",
                    "license",
                ],
                "note": "license 是素材源返回或适配器声明的记录，不构成法律意见。",
            },
        }

    async def search(
        self,
        query: str,
        *,
        limit: int = 6,
        source_names: list[str] | None = None,
        filters: SearchFilters | None = None,
    ) -> MaterialSearchResult:
        """搜索视频候选，持久化私有下载信息并只公开候选引用。"""
        resolved_query = query.strip()
        if not resolved_query:
            raise MaterialAcquisitionError("invalid_query", "素材搜索词不能为空")
        if not 1 <= limit <= 20:
            raise MaterialAcquisitionError("invalid_limit", "limit 必须位于 1 到 20")

        available_sources = self.engine.available_sources()
        selected_sources = source_names or available_sources
        if not selected_sources:
            raise MaterialAcquisitionError(
                "material_source_unavailable", "没有已配置、可尝试的素材源"
            )
        unavailable_sources = [
            source for source in selected_sources if source not in available_sources
        ]
        if unavailable_sources:
            raise MaterialAcquisitionError(
                "material_source_unavailable",
                f"素材源未注册或未配置：{', '.join(unavailable_sources)}",
            )

        resolved_filters = filters or SearchFilters(kind="video", per_page=limit)
        if resolved_filters.kind != "video":
            raise MaterialAcquisitionError(
                "unsupported_kind", "Agent 素材获取接口当前仅支持视频"
            )
        resolved_filters = resolved_filters.model_copy(
            update={"kind": "video", "per_page": max(limit, resolved_filters.per_page)}
        )
        pool_per_source = max(5, min(limit, 8))
        try:
            candidates = await self.engine.search(
                resolved_query,
                resolved_filters,
                source_names=selected_sources,
                limit_per_source=pool_per_source,
            )
        except StockError as error:
            raise MaterialAcquisitionError("material_search_failed", str(error)) from error

        token = secrets.token_hex(6)
        public_candidates: list[CandidateSummary] = []
        private_candidates: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            if candidate.kind != "video":
                continue
            try:
                _validate_candidate_rights(candidate)
            except MaterialAcquisitionError:
                continue
            candidate_ref = _make_candidate_ref(token, resolved_query, candidate)
            private_candidates[candidate_ref] = candidate.model_dump(mode="json")
            public_candidates.append(_candidate_summary(candidate_ref, candidate))
            if len(public_candidates) == limit:
                break

        output_root = _prepare_output_root(self.config.output_dir)
        search_path = (output_root / "searches" / f"search_{token}.json").resolve()
        _write_json_atomic(
            search_path,
            {
                "schema_version": _SCHEMA_VERSION,
                "search_token": token,
                "query": resolved_query,
                "created_at": datetime.now(UTC).isoformat(),
                "candidates": private_candidates,
            },
        )
        return MaterialSearchResult(
            query=resolved_query,
            candidates=tuple(public_candidates),
        )

    async def acquire(self, candidate_ref: str) -> MaterialAcquisitionResult:
        """通过搜索返回的候选引用下载、验证视频并记录溯源信息。"""
        match = _CANDIDATE_REF.fullmatch(candidate_ref)
        if match is None:
            raise MaterialAcquisitionError(
                "candidate_ref_invalid", "candidate_ref 格式无效，必须来自 search"
            )
        token = match.group(1)
        output_root = _prepare_output_root(self.config.output_dir)
        manifest_path = output_root / "searches" / f"search_{token}.json"
        manifest = _read_manifest(manifest_path)
        candidate_data = manifest["candidates"].get(candidate_ref)
        if candidate_data is None:
            raise MaterialAcquisitionError(
                "candidate_ref_invalid", "candidate_ref 不属于对应的搜索记录"
            )
        try:
            candidate = Candidate.model_validate(candidate_data)
        except ValidationError as error:
            raise MaterialAcquisitionError(
                "search_record_invalid", "搜索记录中的候选结构无效"
            ) from error
        query = str(manifest["query"])
        if _make_candidate_ref(token, query, candidate) != candidate_ref:
            raise MaterialAcquisitionError(
                "candidate_ref_invalid", "候选身份与 candidate_ref 不一致"
            )
        _validate_candidate_rights(candidate)

        downloads_dir = (output_root / "downloads").resolve()
        downloads_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(
                prefix=".material-acquisition-", dir=output_root
            ) as temporary_dir:
                try:
                    temporary_path = await self.engine.download(
                        candidate, Path(temporary_dir)
                    )
                except SourceError as error:
                    raise MaterialAcquisitionError(
                        "material_download_failed", str(error)
                    ) from error
                resolved_temporary = temporary_path.resolve()
                try:
                    resolved_temporary.relative_to(Path(temporary_dir).resolve())
                except ValueError as error:
                    raise MaterialAcquisitionError(
                        "download_path_invalid", "素材源将文件写到了临时目录之外"
                    ) from error
                metadata = _probe_video(resolved_temporary)
                digest = _sha256(resolved_temporary)
                size_bytes = resolved_temporary.stat().st_size
                destination = _reserve_destination(
                    downloads_dir / resolved_temporary.name
                )
                try:
                    os.replace(resolved_temporary, destination)
                except OSError:
                    destination.unlink(missing_ok=True)
                    raise
        except MaterialAcquisitionError:
            raise
        except OSError as error:
            raise MaterialAcquisitionError(
                "output_unavailable", f"无法写入素材输出目录：{error}"
            ) from error

        destination = destination.resolve()
        provenance_path = (output_root / "provenance" / f"{destination.stem}.json").resolve()
        acquired_at = datetime.now(UTC).isoformat()
        provenance = {
            "schema_version": _SCHEMA_VERSION,
            "asset_id": destination.stem,
            "candidate_ref": candidate_ref,
            "search_token": token,
            "file_path": str(destination),
            "sha256": digest,
            "size_bytes": size_bytes,
            "provider": candidate.source,
            "provider_asset_id": candidate.source_id,
            "source_url": candidate.source_url,
            "creator": candidate.creator,
            "license": candidate.license,
            "search_query": query,
            "acquired_at": acquired_at,
            "media": {
                "duration_seconds": metadata.duration_seconds,
                "width": metadata.width,
                "height": metadata.height,
                "video_codec": metadata.video_codec,
                "audio_codec": metadata.audio_codec,
            },
        }
        try:
            _write_json_atomic(provenance_path, provenance)
        except MaterialAcquisitionError:
            destination.unlink(missing_ok=True)
            raise

        return MaterialAcquisitionResult(
            candidate_ref=candidate_ref,
            file_path=destination,
            provenance_path=provenance_path,
            sha256=digest,
            size_bytes=size_bytes,
            provider=candidate.source,
            provider_asset_id=candidate.source_id,
            source_url=candidate.source_url,
            creator=candidate.creator,
            license=candidate.license,
            search_query=query,
            duration_seconds=metadata.duration_seconds,
            width=metadata.width,
            height=metadata.height,
            video_codec=metadata.video_codec,
            audio_codec=metadata.audio_codec,
        )


def _candidate_summary(candidate_ref: str, candidate: Candidate) -> CandidateSummary:
    return CandidateSummary(
        candidate_ref=candidate_ref,
        provider=candidate.source,
        provider_asset_id=candidate.source_id,
        source_url=candidate.source_url,
        kind=candidate.kind,
        width=candidate.width,
        height=candidate.height,
        duration_seconds=candidate.duration,
        creator=candidate.creator,
        license=candidate.license,
        tags=candidate.source_tags,
        thumbnail_url=candidate.thumbnail_url,
    )


def _make_candidate_ref(token: str, query: str, candidate: Candidate) -> str:
    candidate_json = json.dumps(
        candidate.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    identity = f"{token}\0{query}\0{candidate_json}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"candidate_{token}_{digest}"


def _validate_candidate_rights(candidate: Candidate) -> None:
    source_url = urlparse(candidate.source_url)
    if (
        not candidate.source
        or not candidate.source_id
        or source_url.scheme not in {"http", "https"}
        or not source_url.netloc
    ):
        raise MaterialAcquisitionError(
            "material_provenance_incomplete",
            "候选缺少稳定的素材源、素材 ID 或来源页面",
        )
    normalized_license = candidate.license.strip().lower()
    ambiguous_archive_default = (
        candidate.source == "archive_org" and normalized_license == "public domain"
    )
    if (
        not normalized_license
        or normalized_license in {"unknown", "unspecified"}
        or ambiguous_archive_default
    ):
        raise MaterialAcquisitionError(
            "material_rights_evidence_missing", "候选没有明确的授权记录"
        )


def _prepare_output_root(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise OSError("目标不是目录")
    except OSError as error:
        raise MaterialAcquisitionError(
            "output_unavailable", f"无法使用素材输出目录：{error}"
        ) from error
    return path.resolve()


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise MaterialAcquisitionError(
            "candidate_ref_expired", "candidate_ref 对应的搜索记录不存在"
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise MaterialAcquisitionError(
            "search_record_invalid", "无法读取 candidate_ref 对应的搜索记录"
        ) from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _SCHEMA_VERSION
        or not isinstance(payload.get("query"), str)
        or not isinstance(payload.get("candidates"), dict)
    ):
        raise MaterialAcquisitionError(
            "search_record_invalid", "candidate_ref 对应的搜索记录结构无效"
        )
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary_path, path)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise MaterialAcquisitionError(
            "output_unavailable", f"无法写入素材记录：{error}"
        ) from error


def _reserve_destination(path: Path) -> Path:
    candidates = [
        path,
        *(path.with_name(f"{path.stem}_{index}{path.suffix}") for index in range(2, 10_000)),
    ]
    for candidate in candidates:
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(descriptor)
        return candidate
    raise MaterialAcquisitionError(
        "name_conflict", f"无法为素材文件分配名称：{path.name}"
    )


def _probe_video(path: Path) -> _VideoMetadata:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise MaterialAcquisitionError("dependency_missing", "缺少 ffprobe")
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise MaterialAcquisitionError(
            "invalid_media", f"ffprobe 无法读取下载素材：{completed.stderr.strip()}"
        )
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        audio = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"), None
        )
        duration = float(payload.get("format", {}).get("duration") or video.get("duration") or 0)
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        video_codec = str(video.get("codec_name") or "unknown")
        audio_codec = str(audio.get("codec_name") or "unknown") if audio else None
    except (AttributeError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as error:
        raise MaterialAcquisitionError(
            "invalid_media", "下载结果不包含可解析的视频流"
        ) from error
    if path.stat().st_size == 0 or width <= 0 or height <= 0:
        raise MaterialAcquisitionError(
            "invalid_media", "下载结果不是有效的视频素材"
        )
    return _VideoMetadata(duration, width, height, video_codec, audio_codec)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
