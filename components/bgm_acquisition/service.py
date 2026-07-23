"""Mixkit BGM 的搜索、选择、取得与溯源。"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

_SCHEMA_VERSION = 1
_CANDIDATE_REF = re.compile(r"^bgm_([0-9a-f]{12})_([0-9a-f]{32})$")
_MIXKIT_ORIGIN = "https://mixkit.co"
_CATALOG_URL = f"{_MIXKIT_ORIGIN}/free-stock-music/"
_LICENSE_URL = f"{_MIXKIT_ORIGIN}/license/#musicFree"
_LICENSE_EVIDENCE_URL = f"{_MIXKIT_ORIGIN}/license/modal/musicFree/"
_TERMS_URL = f"{_MIXKIT_ORIGIN}/terms/"
_HTML_LIMIT_BYTES = 5 * 1024 * 1024


class BgmAcquisitionError(RuntimeError):
    """BGM 获取的稳定错误契约。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BgmAcquisitionConfig:
    """BGM 获取的本地输出与网络限制。"""

    output_dir: Path = Path("output/bgm")
    request_timeout_seconds: float = 30.0
    max_download_bytes: int = 64 * 1024 * 1024
    user_agent: str = "VideoCreatePlugin/0.1 (single-item BGM acquisition)"


@dataclass(frozen=True, slots=True)
class BgmSource:
    """一个已接入的 BGM 来源及其公开依据页面。"""

    name: str
    display_name: str
    catalog_url: str
    license_url: str
    terms_url: str
    access_mode: str
    license_record: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BgmCandidate:
    """搜索阶段可公开的候选，不包含下载地址。"""

    candidate_ref: str
    provider: str
    provider_asset_id: str
    title: str
    creator: str
    duration_seconds: float
    source_url: str
    tags: tuple[str, ...]
    license_record: str
    license_url: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tags"] = list(self.tags)
        return payload


@dataclass(frozen=True, slots=True)
class BgmSearchResult:
    """一次两阶段 BGM 搜索的公开结果。"""

    query: str
    source: str
    source_page_url: str
    candidates: tuple[BgmCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "source": self.source,
            "source_page_url": self.source_page_url,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class BgmAcquisitionResult:
    """取得、验证并记录溯源的 BGM 文件。"""

    candidate_ref: str
    original_path: Path
    derived_path: Path | None
    selected_path: Path
    provenance_path: Path
    original_sha256: str
    derived_sha256: str | None
    provider: str
    provider_asset_id: str
    title: str
    creator: str
    source_url: str
    license_record: str
    license_url: str
    terms_url: str
    original_duration_seconds: float
    selected_duration_seconds: float
    audio_codec: str
    sample_rate_hz: int
    channels: int

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("original_path", "selected_path", "provenance_path"):
            payload[key] = str(payload[key])
        if self.derived_path is not None:
            payload["derived_path"] = str(self.derived_path)
        return payload


@dataclass(frozen=True, slots=True)
class _HttpResponse:
    status_code: int
    final_url: str
    headers: Mapping[str, str]
    body: bytes


class _HttpClient(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> _HttpResponse: ...


class _UrllibHttpClient:
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> _HttpResponse:
        request = Request(url, headers=dict(headers))
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read(max_bytes + 1)
                return _HttpResponse(
                    status_code=int(response.status),
                    final_url=str(response.geturl()),
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=body,
                )
        except HTTPError as error:
            raise BgmAcquisitionError(
                "http_failed", f"BGM 来源请求返回 HTTP {error.code}：{url}"
            ) from error
        except (OSError, URLError) as error:
            raise BgmAcquisitionError("http_failed", f"BGM 来源请求失败：{url}：{error}") from error


@dataclass(frozen=True, slots=True)
class _CatalogTrack:
    provider_asset_id: str
    title: str
    creator: str
    duration_seconds: float
    tags: tuple[str, ...]
    preview_url: str
    modal_url: str


@dataclass(frozen=True, slots=True)
class _CandidateRecord:
    provider: str
    provider_asset_id: str
    title: str
    creator: str
    duration_seconds: float
    source_url: str
    tags: tuple[str, ...]
    preview_url: str
    modal_url: str
    license_record: str
    license_url: str
    terms_url: str


@dataclass(frozen=True, slots=True)
class _AudioMetadata:
    duration_seconds: float
    audio_codec: str
    sample_rate_hz: int
    channels: int
    format_name: str


class _CatalogParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.tracks: list[_CatalogTrack] = []
        self._active: dict[str, Any] | None = None
        self._capture: str | None = None
        self._capture_tag: str | None = None
        self._capture_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if tag == "div" and attributes.get("data-test-id") == "audio-player":
            self._finish_track()
            self._active = {
                "provider_asset_id": attributes.get("data-audio-player-item-id-value", ""),
                "preview_url": attributes.get("data-audio-player-preview-url-value", ""),
                "title": "",
                "creator": "",
                "duration": "",
                "tags": [],
                "modal_url": "",
            }
            return
        if self._active is None:
            return
        if tag == "h2" and "item-grid-card__title" in classes:
            self._start_capture("title", tag)
        elif tag == "p" and "item-grid-music-preview__author" in classes:
            self._start_capture("creator", tag)
        elif tag == "div" and attributes.get("data-test-id") == "duration":
            self._start_capture("duration", tag)
        elif tag == "a" and "meta-links__link" in classes:
            self._start_capture("tag", tag)
        elif tag == "button" and attributes.get("data-download--button-modal-url-value"):
            self._active["modal_url"] = urljoin(
                self.base_url,
                attributes["data-download--button-modal-url-value"],
            )

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture is None or tag != self._capture_tag or self._active is None:
            return
        value = " ".join("".join(self._capture_text).split())
        if self._capture == "tag":
            if value:
                self._active["tags"].append(value)
        else:
            self._active[self._capture] = value
        self._capture = None
        self._capture_tag = None
        self._capture_text = []

    def close(self) -> None:
        super().close()
        self._finish_track()

    def _start_capture(self, field: str, tag: str) -> None:
        self._capture = field
        self._capture_tag = tag
        self._capture_text = []

    def _finish_track(self) -> None:
        active = self._active
        self._active = None
        self._capture = None
        self._capture_tag = None
        self._capture_text = []
        if active is None:
            return
        try:
            duration_seconds = _parse_duration(str(active["duration"]))
            creator = re.sub(r"^by\s+", "", str(active["creator"]), flags=re.I).strip()
            track = _CatalogTrack(
                provider_asset_id=str(active["provider_asset_id"]),
                title=str(active["title"]).strip(),
                creator=creator,
                duration_seconds=duration_seconds,
                tags=tuple(dict.fromkeys(str(tag).strip() for tag in active["tags"] if tag)),
                preview_url=str(active["preview_url"]),
                modal_url=str(active["modal_url"]),
            )
            _validate_catalog_track(track)
        except (BgmAcquisitionError, KeyError, TypeError, ValueError):
            return
        self.tracks.append(track)


class BgmAcquisitionService:
    """将 Mixkit 限制为一次搜索、一次选择的两阶段获取流程。"""

    def __init__(
        self,
        config: BgmAcquisitionConfig | None = None,
        *,
        http_client: _HttpClient | None = None,
    ) -> None:
        self.config = config or BgmAcquisitionConfig()
        self._http = http_client or _UrllibHttpClient()

    def list_sources(self) -> tuple[BgmSource, ...]:
        """列出配置来源；该结果不是实时可用性检查。"""
        return (
            BgmSource(
                name="mixkit",
                display_name="Mixkit Free Stock Music",
                catalog_url=_CATALOG_URL,
                license_url=_LICENSE_URL,
                terms_url=_TERMS_URL,
                access_mode="public_catalog_no_api_key_single_selection",
                license_record="Mixkit Stock Music Free License",
            ),
        )

    def search(self, query: str, *, limit: int = 6) -> BgmSearchResult:
        """搜索公开目录并返回不含下载地址的候选引用。"""
        resolved_query = " ".join(query.split())
        if not resolved_query:
            raise BgmAcquisitionError("invalid_query", "BGM 搜索词为空")
        if not 1 <= limit <= 20:
            raise BgmAcquisitionError("invalid_limit", "limit 应位于 1 到 20")
        slug = _query_slug(resolved_query)
        if not slug:
            raise BgmAcquisitionError(
                "query_not_supported", "Mixkit 公开搜索入口需要含拉丁字母或数字的搜索词"
            )

        requested_url = urljoin(_CATALOG_URL, f"discover/{quote(slug)}/")
        response = self._get_html(requested_url)
        source_page_url = _validate_source_page_url(response.final_url)
        tracks = _parse_catalog(response.body, source_page_url)
        token = secrets.token_hex(6)
        private_candidates: dict[str, dict[str, Any]] = {}
        public_candidates: list[BgmCandidate] = []
        for track in tracks[:limit]:
            record = _CandidateRecord(
                provider="mixkit",
                provider_asset_id=track.provider_asset_id,
                title=track.title,
                creator=track.creator,
                duration_seconds=track.duration_seconds,
                source_url=source_page_url,
                tags=track.tags,
                preview_url=track.preview_url,
                modal_url=track.modal_url,
                license_record="Mixkit Stock Music Free License",
                license_url=_LICENSE_URL,
                terms_url=_TERMS_URL,
            )
            _validate_candidate_record(record)
            candidate_ref = _make_candidate_ref(token, resolved_query, record)
            private_candidates[candidate_ref] = _record_to_dict(record)
            public_candidates.append(_public_candidate(candidate_ref, record))

        output_root = _prepare_output_root(self.config.output_dir)
        search_path = output_root / "searches" / f"search_{token}.json"
        _write_json_atomic(
            search_path,
            {
                "schema_version": _SCHEMA_VERSION,
                "search_token": token,
                "query": resolved_query,
                "requested_url": requested_url,
                "source_page_url": source_page_url,
                "source_page_sha256": hashlib.sha256(response.body).hexdigest(),
                "retrieved_at": datetime.now(UTC).isoformat(),
                "candidates": private_candidates,
            },
        )
        return BgmSearchResult(
            query=resolved_query,
            source="mixkit",
            source_page_url=source_page_url,
            candidates=tuple(public_candidates),
        )

    def acquire(
        self,
        candidate_ref: str,
        *,
        clip_start_seconds: float = 0.0,
        clip_duration_seconds: float | None = None,
    ) -> BgmAcquisitionResult:
        """取得所选候选；可另行生成短片段，原始文件保持独立。"""
        token = _candidate_token(candidate_ref)
        output_root = _prepare_output_root(self.config.output_dir)
        manifest = _read_manifest(output_root / "searches" / f"search_{token}.json")
        record_payload = manifest["candidates"].get(candidate_ref)
        if not isinstance(record_payload, dict):
            raise BgmAcquisitionError("candidate_ref_invalid", "candidate_ref 不属于对应搜索记录")
        record = _record_from_dict(record_payload)
        query = str(manifest["query"])
        if _make_candidate_ref(token, query, record) != candidate_ref:
            raise BgmAcquisitionError("candidate_ref_invalid", "候选记录与 candidate_ref 不一致")
        _validate_clip_request(clip_start_seconds, clip_duration_seconds)

        source_response = self._get_html(record.source_url)
        current_source_url = _validate_source_page_url(source_response.final_url)
        current_tracks = _parse_catalog(source_response.body, current_source_url)
        current_track = next(
            (
                track
                for track in current_tracks
                if track.provider_asset_id == record.provider_asset_id
            ),
            None,
        )
        if current_track is None or not _track_matches_record(current_track, record):
            raise BgmAcquisitionError(
                "source_evidence_changed", "来源页面不再包含与搜索记录一致的候选"
            )

        modal_response = self._get_html(record.modal_url)
        _validate_exact_evidence_url(modal_response.final_url, record.modal_url)
        download_url = _parse_download_url(modal_response.body)
        _validate_download_url(download_url, record.provider_asset_id)
        if download_url != record.preview_url:
            raise BgmAcquisitionError(
                "download_evidence_changed", "来源页面与下载确认页的音频地址不一致"
            )

        license_response = self._get_html(_LICENSE_EVIDENCE_URL)
        _validate_exact_evidence_url(
            license_response.final_url,
            _LICENSE_EVIDENCE_URL,
        )
        license_text = _visible_text(license_response.body)
        if "Stock Music Free License" not in license_text:
            raise BgmAcquisitionError("rights_evidence_changed", "授权页面未包含预期的音乐授权标识")
        terms_response = self._get_html(_TERMS_URL)
        _validate_exact_evidence_url(terms_response.final_url, _TERMS_URL)

        audio_response = self._get(
            download_url,
            max_bytes=self.config.max_download_bytes,
            accept="audio/mpeg,audio/*;q=0.9,application/octet-stream;q=0.5",
        )
        _validate_download_url(audio_response.final_url, record.provider_asset_id)
        if not audio_response.body:
            raise BgmAcquisitionError("invalid_media", "下载结果为空")
        original_sha256 = hashlib.sha256(audio_response.body).hexdigest()
        original_path = (
            output_root
            / "originals"
            / f"mixkit_{record.provider_asset_id}_{original_sha256[:12]}.mp3"
        ).resolve()
        original_preexisted = original_path.exists()
        original_metadata = _write_and_probe_audio(original_path, audio_response.body)
        if not _durations_close(
            original_metadata.duration_seconds, record.duration_seconds, tolerance_ratio=0.06
        ):
            if not original_preexisted:
                original_path.unlink(missing_ok=True)
            raise BgmAcquisitionError("media_metadata_mismatch", "下载音频时长与来源页面记录不一致")

        derived_path: Path | None = None
        derived_preexisted = False
        derived_sha256: str | None = None
        selected_metadata = original_metadata
        if clip_duration_seconds is not None:
            if clip_start_seconds + clip_duration_seconds > (
                original_metadata.duration_seconds + 0.05
            ):
                if not original_preexisted:
                    original_path.unlink(missing_ok=True)
                raise BgmAcquisitionError("clip_out_of_range", "短片段范围超出原始音频")
            clip_start_ms = round(clip_start_seconds * 1000)
            clip_duration_ms = round(clip_duration_seconds * 1000)
            derived_path = (
                output_root
                / "derivatives"
                / (
                    f"mixkit_{record.provider_asset_id}_{original_sha256[:12]}"
                    f"_clip_{clip_start_ms}_{clip_duration_ms}.mp3"
                )
            ).resolve()
            derived_preexisted = derived_path.exists()
            try:
                selected_metadata = _derive_clip(
                    original_path,
                    derived_path,
                    start_seconds=clip_start_seconds,
                    duration_seconds=clip_duration_seconds,
                )
            except BgmAcquisitionError:
                if not original_preexisted:
                    original_path.unlink(missing_ok=True)
                raise
            if not _durations_close(
                selected_metadata.duration_seconds,
                clip_duration_seconds,
                tolerance_ratio=0.08,
            ):
                if not original_preexisted:
                    original_path.unlink(missing_ok=True)
                if not derived_preexisted:
                    derived_path.unlink(missing_ok=True)
                raise BgmAcquisitionError("derived_media_invalid", "短片段实际时长与请求不一致")
            derived_sha256 = _sha256(derived_path)

        selected_path = derived_path or original_path
        acquired_at = datetime.now(UTC).isoformat()
        provenance_path = (output_root / "provenance" / f"{selected_path.stem}.json").resolve()
        provenance = {
            "schema_version": _SCHEMA_VERSION,
            "candidate_ref": candidate_ref,
            "search_token": token,
            "search_query": query,
            "provider": record.provider,
            "provider_asset_id": record.provider_asset_id,
            "title": record.title,
            "creator": record.creator,
            "tags": list(record.tags),
            "source_url": record.source_url,
            "acquired_at": acquired_at,
            "source_evidence": {
                "verified_url": current_source_url,
                "sha256": hashlib.sha256(source_response.body).hexdigest(),
                "retrieved_at": acquired_at,
            },
            "rights_record": {
                "provider_label": record.license_record,
                "license_url": record.license_url,
                "license_evidence_url": _LICENSE_EVIDENCE_URL,
                "license_evidence_sha256": hashlib.sha256(license_response.body).hexdigest(),
                "terms_url": record.terms_url,
                "terms_evidence_sha256": hashlib.sha256(terms_response.body).hexdigest(),
                "scope": "provider_page_record_not_publishability_determination",
            },
            "original": {
                "file_path": str(original_path),
                "sha256": original_sha256,
                "size_bytes": original_path.stat().st_size,
                "download_url": download_url,
                "media": asdict(original_metadata),
            },
            "derivative": None,
        }
        if derived_path is not None:
            provenance["derivative"] = {
                "file_path": str(derived_path),
                "sha256": derived_sha256,
                "size_bytes": derived_path.stat().st_size,
                "relation": "clip_of_original",
                "parent_sha256": original_sha256,
                "clip_start_seconds": clip_start_seconds,
                "clip_duration_seconds": clip_duration_seconds,
                "media": asdict(selected_metadata),
            }
        try:
            _write_json_atomic(provenance_path, provenance)
        except BgmAcquisitionError:
            if not original_preexisted:
                original_path.unlink(missing_ok=True)
            if derived_path is not None and not derived_preexisted:
                derived_path.unlink(missing_ok=True)
            raise

        return BgmAcquisitionResult(
            candidate_ref=candidate_ref,
            original_path=original_path,
            derived_path=derived_path,
            selected_path=selected_path,
            provenance_path=provenance_path,
            original_sha256=original_sha256,
            derived_sha256=derived_sha256,
            provider=record.provider,
            provider_asset_id=record.provider_asset_id,
            title=record.title,
            creator=record.creator,
            source_url=record.source_url,
            license_record=record.license_record,
            license_url=record.license_url,
            terms_url=record.terms_url,
            original_duration_seconds=original_metadata.duration_seconds,
            selected_duration_seconds=selected_metadata.duration_seconds,
            audio_codec=selected_metadata.audio_codec,
            sample_rate_hz=selected_metadata.sample_rate_hz,
            channels=selected_metadata.channels,
        )

    def _get_html(self, url: str) -> _HttpResponse:
        response = self._get(
            url,
            max_bytes=_HTML_LIMIT_BYTES,
            accept="text/html,application/xhtml+xml;q=0.9",
        )
        content_type = response.headers.get("content-type", "").lower()
        if content_type and "html" not in content_type:
            raise BgmAcquisitionError("invalid_source_response", f"BGM 来源未返回 HTML：{url}")
        return response

    def _get(self, url: str, *, max_bytes: int, accept: str) -> _HttpResponse:
        if max_bytes <= 0:
            raise BgmAcquisitionError("invalid_config", "max_download_bytes 应为正数")
        try:
            response = self._http.get(
                url,
                headers={"User-Agent": self.config.user_agent, "Accept": accept},
                timeout_seconds=self.config.request_timeout_seconds,
                max_bytes=max_bytes,
            )
        except BgmAcquisitionError:
            raise
        except Exception as error:
            raise BgmAcquisitionError("http_failed", f"BGM 来源请求失败：{url}") from error
        if not 200 <= response.status_code < 300:
            raise BgmAcquisitionError(
                "http_failed", f"BGM 来源请求返回 HTTP {response.status_code}：{url}"
            )
        if len(response.body) > max_bytes:
            raise BgmAcquisitionError("response_too_large", f"BGM 来源响应超过限制：{url}")
        return response


def _public_candidate(candidate_ref: str, record: _CandidateRecord) -> BgmCandidate:
    return BgmCandidate(
        candidate_ref=candidate_ref,
        provider=record.provider,
        provider_asset_id=record.provider_asset_id,
        title=record.title,
        creator=record.creator,
        duration_seconds=record.duration_seconds,
        source_url=record.source_url,
        tags=record.tags,
        license_record=record.license_record,
        license_url=record.license_url,
    )


def _query_slug(query: str) -> str:
    normalized = unicodedata.normalize("NFKD", query.lower())
    ascii_text = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    slug = re.sub(r"[^a-z0-9\s-]", "", ascii_text)
    slug = re.sub(r"[\s-]+", "-", slug).strip("-")
    return slug


def _parse_catalog(body: bytes, source_url: str) -> tuple[_CatalogTrack, ...]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BgmAcquisitionError("invalid_source_response", "BGM 来源页面不是 UTF-8") from error
    parser = _CatalogParser(source_url)
    parser.feed(text)
    parser.close()
    return tuple(parser.tracks)


def _parse_duration(value: str) -> float:
    parts = value.strip().split(":")
    if len(parts) not in {2, 3} or any(not part.isdigit() for part in parts):
        raise ValueError("invalid duration")
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    if seconds <= 0:
        raise ValueError("invalid duration")
    return float(seconds)


def _validate_catalog_track(track: _CatalogTrack) -> None:
    if not track.provider_asset_id.isdigit() or not track.title or not track.creator:
        raise BgmAcquisitionError("invalid_source_response", "目录候选字段缺失")
    _validate_download_url(track.preview_url, track.provider_asset_id)
    modal = urlparse(track.modal_url)
    expected_path = f"/free-stock-music/download/{track.provider_asset_id}/"
    if modal.scheme != "https" or modal.hostname != "mixkit.co" or modal.path != expected_path:
        raise BgmAcquisitionError("invalid_source_response", "目录候选下载确认页无效")


def _validate_candidate_record(record: _CandidateRecord) -> None:
    if record.provider != "mixkit" or record.license_record != "Mixkit Stock Music Free License":
        raise BgmAcquisitionError("search_record_invalid", "候选来源或授权记录无效")
    _validate_source_page_url(record.source_url)
    _validate_download_url(record.preview_url, record.provider_asset_id)
    _validate_catalog_track(
        _CatalogTrack(
            provider_asset_id=record.provider_asset_id,
            title=record.title,
            creator=record.creator,
            duration_seconds=record.duration_seconds,
            tags=record.tags,
            preview_url=record.preview_url,
            modal_url=record.modal_url,
        )
    )
    if record.license_url != _LICENSE_URL or record.terms_url != _TERMS_URL:
        raise BgmAcquisitionError("search_record_invalid", "候选依据页面无效")


def _validate_source_page_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "mixkit.co"
        or parsed.port is not None
        or not parsed.path.startswith("/free-stock-music/")
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise BgmAcquisitionError("invalid_source_url", "BGM 来源页面地址无效")
    return parsed._replace(fragment="").geturl()


def _validate_download_url(url: str, provider_asset_id: str) -> None:
    parsed = urlparse(url)
    expected_path = f"/music/{provider_asset_id}/{provider_asset_id}.mp3"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "assets.mixkit.co"
        or parsed.port is not None
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise BgmAcquisitionError("download_url_invalid", "Mixkit 音频下载地址无效")


def _validate_exact_evidence_url(actual_url: str, expected_url: str) -> None:
    actual = urlparse(actual_url)._replace(fragment="").geturl()
    expected = urlparse(expected_url)._replace(fragment="").geturl()
    if actual != expected:
        raise BgmAcquisitionError("evidence_redirect_invalid", "依据页面跳转到了非预期地址")


def _parse_download_url(body: bytes) -> str:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BgmAcquisitionError("invalid_source_response", "下载确认页不是 UTF-8") from error
    match = re.search(r'data-download--modal-url-value="([^"]+)"', text)
    if match is None:
        raise BgmAcquisitionError("download_evidence_changed", "下载确认页缺少音频地址")
    return html.unescape(match.group(1))


def _visible_text(body: bytes) -> str:
    try:
        source = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BgmAcquisitionError("invalid_source_response", "依据页面不是 UTF-8") from error
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", source)).split())


def _track_matches_record(track: _CatalogTrack, record: _CandidateRecord) -> bool:
    return (
        track.title == record.title
        and track.creator == record.creator
        and track.duration_seconds == record.duration_seconds
        and track.tags == record.tags
        and track.preview_url == record.preview_url
        and track.modal_url == record.modal_url
    )


def _record_to_dict(record: _CandidateRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["tags"] = list(record.tags)
    return payload


def _record_from_dict(payload: dict[str, Any]) -> _CandidateRecord:
    expected = {
        "provider",
        "provider_asset_id",
        "title",
        "creator",
        "duration_seconds",
        "source_url",
        "tags",
        "preview_url",
        "modal_url",
        "license_record",
        "license_url",
        "terms_url",
    }
    if set(payload) != expected or not isinstance(payload.get("tags"), list):
        raise BgmAcquisitionError("search_record_invalid", "候选搜索记录结构无效")
    try:
        record = _CandidateRecord(
            provider=str(payload["provider"]),
            provider_asset_id=str(payload["provider_asset_id"]),
            title=str(payload["title"]),
            creator=str(payload["creator"]),
            duration_seconds=float(payload["duration_seconds"]),
            source_url=str(payload["source_url"]),
            tags=tuple(str(tag) for tag in payload["tags"]),
            preview_url=str(payload["preview_url"]),
            modal_url=str(payload["modal_url"]),
            license_record=str(payload["license_record"]),
            license_url=str(payload["license_url"]),
            terms_url=str(payload["terms_url"]),
        )
    except (TypeError, ValueError) as error:
        raise BgmAcquisitionError("search_record_invalid", "候选搜索记录字段无效") from error
    _validate_candidate_record(record)
    return record


def _make_candidate_ref(token: str, query: str, record: _CandidateRecord) -> str:
    serialized = json.dumps(
        _record_to_dict(record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(f"{token}\0{query}\0{serialized}".encode()).hexdigest()[:32]
    return f"bgm_{token}_{digest}"


def _candidate_token(candidate_ref: str) -> str:
    match = _CANDIDATE_REF.fullmatch(candidate_ref)
    if match is None:
        raise BgmAcquisitionError("candidate_ref_invalid", "candidate_ref 格式无效，应来自 search")
    return match.group(1)


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise BgmAcquisitionError(
            "candidate_ref_expired", "candidate_ref 对应的搜索记录不存在"
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise BgmAcquisitionError("search_record_invalid", "搜索记录读取失败") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _SCHEMA_VERSION
        or not isinstance(payload.get("query"), str)
        or not isinstance(payload.get("candidates"), dict)
    ):
        raise BgmAcquisitionError("search_record_invalid", "搜索记录结构无效")
    return payload


def _prepare_output_root(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise OSError("目标不是目录")
    except OSError as error:
        raise BgmAcquisitionError("output_unavailable", f"BGM 输出目录不可用：{error}") from error
    return path.resolve()


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
        raise BgmAcquisitionError("output_unavailable", f"BGM 记录写入失败：{error}") from error


def _write_and_probe_audio(destination: Path, body: bytes) -> _AudioMetadata:
    if destination.exists():
        if _sha256(destination) != hashlib.sha256(body).hexdigest():
            raise BgmAcquisitionError("name_conflict", "BGM 原始文件名发生哈希冲突")
        return _probe_audio(destination)
    temporary_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp.mp3", dir=destination.parent
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        temporary_path.write_bytes(body)
        metadata = _probe_audio(temporary_path)
        os.replace(temporary_path, destination)
        return metadata
    except BgmAcquisitionError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise BgmAcquisitionError("output_unavailable", f"BGM 音频写入失败：{error}") from error


def _probe_audio(path: Path) -> _AudioMetadata:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise BgmAcquisitionError("dependency_missing", "缺少 ffprobe")
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
        raise BgmAcquisitionError(
            "invalid_media", f"ffprobe 未识别下载音频：{completed.stderr.strip()}"
        )
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        audio_stream = next(stream for stream in streams if stream.get("codec_type") == "audio")
        format_data = payload.get("format", {})
        duration = float(format_data.get("duration") or audio_stream.get("duration") or 0)
        codec = str(audio_stream.get("codec_name") or "")
        sample_rate = int(audio_stream.get("sample_rate") or 0)
        channels = int(audio_stream.get("channels") or 0)
        format_name = str(format_data.get("format_name") or "")
    except (AttributeError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as error:
        raise BgmAcquisitionError("invalid_media", "下载结果缺少可解析的音频流") from error
    if path.stat().st_size <= 0 or duration <= 0 or not codec or sample_rate <= 0 or channels <= 0:
        raise BgmAcquisitionError("invalid_media", "下载结果不是有效音频")
    return _AudioMetadata(duration, codec, sample_rate, channels, format_name)


def _derive_clip(
    source: Path,
    destination: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
) -> _AudioMetadata:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise BgmAcquisitionError("dependency_missing", "缺少 ffmpeg")
    temporary_path: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp.mp3", dir=destination.parent
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        completed = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-y",
                "-ss",
                f"{start_seconds:.6f}",
                "-i",
                str(source),
                "-t",
                f"{duration_seconds:.6f}",
                "-map",
                "0:a:0",
                "-vn",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(temporary_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise BgmAcquisitionError(
                "derived_media_failed", f"ffmpeg 裁取 BGM 失败：{completed.stderr.strip()}"
            )
        metadata = _probe_audio(temporary_path)
        os.replace(temporary_path, destination)
        return metadata
    except BgmAcquisitionError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise BgmAcquisitionError("output_unavailable", f"BGM 短片段写入失败：{error}") from error


def _validate_clip_request(start_seconds: float, duration_seconds: float | None) -> None:
    if not isinstance(start_seconds, (int, float)) or start_seconds < 0:
        raise BgmAcquisitionError("invalid_clip", "clip_start_seconds 应为非负数")
    if duration_seconds is None:
        if start_seconds != 0:
            raise BgmAcquisitionError("invalid_clip", "仅设置 clip_start_seconds 不会形成短片段")
        return
    if not isinstance(duration_seconds, (int, float)) or duration_seconds <= 0:
        raise BgmAcquisitionError("invalid_clip", "clip_duration_seconds 应为正数")


def _durations_close(actual: float, expected: float, *, tolerance_ratio: float) -> bool:
    tolerance = max(0.25, expected * tolerance_ratio)
    return abs(actual - expected) <= tolerance


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
