"""Wikimedia Commons 图片搜索、获取、验证与溯源。"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from PIL import Image, UnidentifiedImageError

_SCHEMA_VERSION = 1
_API_URL = "https://commons.wikimedia.org/w/api.php"
_SOURCE_PAGE = "https://commons.wikimedia.org/wiki/Commons:API"
_CANDIDATE_REF = re.compile(r"^image_([0-9a-f]{12})_([0-9a-f]{32})$")
_HTML_TAG = re.compile(r"<[^>]+>")
_SUPPORTED_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_SEARCH_LIMIT_BYTES = 5 * 1024 * 1024


class ImageAcquisitionError(RuntimeError):
    """图片获取的稳定错误契约。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ImageAcquisitionConfig:
    """图片获取的本地输出与网络限制。"""

    output_dir: Path = Path("output/images")
    request_timeout_seconds: float = 30.0
    max_download_bytes: int = 32 * 1024 * 1024
    thumbnail_width: int = 1600
    user_agent: str = (
        "VideoCreatePlugin/0.1 "
        "(https://github.com/openai/codex; Wikimedia Commons image acquisition)"
    )


@dataclass(frozen=True, slots=True)
class ImageSource:
    """已接入的公开图片来源。"""

    name: str
    display_name: str
    api_url: str
    source_page_url: str
    access_mode: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ImageCandidate:
    """搜索阶段的公开候选，不包含下载地址。"""

    candidate_ref: str
    provider: str
    provider_asset_id: str
    title: str
    creator: str
    source_url: str
    license: str
    license_url: str
    mime_type: str
    width: int
    height: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ImageSearchResult:
    """一次持久化图片搜索的公开结果。"""

    query: str
    source: str
    candidates: tuple[ImageCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "source": self.source,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class ImageAcquisitionResult:
    """已获取、解码验证并记录溯源的图片。"""

    candidate_ref: str
    file_path: Path
    provenance_path: Path
    sha256: str
    size_bytes: int
    provider: str
    provider_asset_id: str
    title: str
    creator: str
    source_url: str
    license: str
    license_url: str
    mime_type: str
    width: int
    height: int

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["file_path"] = str(self.file_path)
        payload["provenance_path"] = str(self.provenance_path)
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
                return _HttpResponse(
                    status_code=int(response.status),
                    final_url=str(response.geturl()),
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read(max_bytes + 1),
                )
        except HTTPError as error:
            raise ImageAcquisitionError(
                "http_failed", f"图片来源请求返回 HTTP {error.code}：{url}"
            ) from error
        except (OSError, URLError) as error:
            raise ImageAcquisitionError(
                "http_failed", f"图片来源请求失败：{url}：{error}"
            ) from error


@dataclass(frozen=True, slots=True)
class _CandidateRecord:
    provider: str
    provider_asset_id: str
    title: str
    creator: str
    source_url: str
    download_url: str
    original_file_url: str
    license: str
    license_url: str
    mime_type: str
    width: int
    height: int


class ImageAcquisitionService:
    """公开图片 API 的两阶段 Agent 调用边界。"""

    def __init__(
        self,
        config: ImageAcquisitionConfig | None = None,
        *,
        http_client: _HttpClient | None = None,
    ) -> None:
        self.config = config or ImageAcquisitionConfig()
        _validate_config(self.config)
        self._http = http_client or _UrllibHttpClient()

    def list_sources(self) -> tuple[ImageSource, ...]:
        return (
            ImageSource(
                name="wikimedia_commons",
                display_name="Wikimedia Commons",
                api_url=_API_URL,
                source_page_url=_SOURCE_PAGE,
                access_mode="public_mediawiki_api_no_key",
            ),
        )

    def search(self, query: str, *, limit: int = 6) -> ImageSearchResult:
        """搜索有作者与授权元数据的可解码图片并返回不透明引用。"""
        resolved_query = query.strip()
        if not resolved_query:
            raise ImageAcquisitionError("invalid_query", "图片搜索词为空")
        if not 1 <= limit <= 20:
            raise ImageAcquisitionError("invalid_limit", "limit 必须位于 1 到 20")
        api_url = _build_search_url(
            resolved_query,
            limit=min(50, max(limit * 3, limit)),
            thumbnail_width=self.config.thumbnail_width,
        )
        response = self._request(api_url, max_bytes=_SEARCH_LIMIT_BYTES)
        if response.status_code != 200:
            raise ImageAcquisitionError(
                "search_failed", f"Wikimedia Commons API 返回 HTTP {response.status_code}"
            )
        if len(response.body) > _SEARCH_LIMIT_BYTES:
            raise ImageAcquisitionError("search_failed", "Wikimedia Commons API 响应过大")
        try:
            payload: Any = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ImageAcquisitionError("search_failed", "图片搜索响应不是有效 JSON") from error

        token = secrets.token_hex(6)
        records = _parse_search_payload(payload)
        selected = records[:limit]
        private_candidates: dict[str, dict[str, Any]] = {}
        public_candidates: list[ImageCandidate] = []
        for record in selected:
            candidate_ref = _make_candidate_ref(token, resolved_query, record)
            private_candidates[candidate_ref] = asdict(record)
            public_candidates.append(_public_candidate(candidate_ref, record))

        output_root = _prepare_output_root(self.config.output_dir)
        _write_json_atomic(
            output_root / "searches" / f"search_{token}.json",
            {
                "schema_version": _SCHEMA_VERSION,
                "search_token": token,
                "query": resolved_query,
                "created_at": datetime.now(UTC).isoformat(),
                "candidates": private_candidates,
            },
        )
        return ImageSearchResult(
            query=resolved_query,
            source="wikimedia_commons",
            candidates=tuple(public_candidates),
        )

    def acquire(self, candidate_ref: str) -> ImageAcquisitionResult:
        """通过搜索产生的引用取得单张图片并原子写入溯源记录。"""
        match = _CANDIDATE_REF.fullmatch(candidate_ref)
        if match is None:
            raise ImageAcquisitionError(
                "candidate_ref_invalid", "candidate_ref 格式无效，必须来自 search"
            )
        token = match.group(1)
        output_root = _prepare_output_root(self.config.output_dir)
        manifest = _read_search_manifest(output_root / "searches" / f"search_{token}.json")
        candidate_payload = manifest["candidates"].get(candidate_ref)
        if not isinstance(candidate_payload, dict):
            raise ImageAcquisitionError("candidate_ref_invalid", "candidate_ref 不属于对应搜索记录")
        try:
            record = _CandidateRecord(**candidate_payload)
        except (TypeError, ValueError) as error:
            raise ImageAcquisitionError("candidate_ref_invalid", "搜索记录中的候选无效") from error
        expected_ref = _make_candidate_ref(token, manifest["query"], record)
        if not secrets.compare_digest(candidate_ref, expected_ref):
            raise ImageAcquisitionError("candidate_ref_invalid", "候选记录与 candidate_ref 不匹配")
        _validate_download_url(record.download_url)

        response = self._request(
            record.download_url,
            max_bytes=self.config.max_download_bytes,
        )
        if response.status_code != 200:
            raise ImageAcquisitionError(
                "download_failed", f"图片下载返回 HTTP {response.status_code}"
            )
        if len(response.body) > self.config.max_download_bytes:
            raise ImageAcquisitionError("download_too_large", "图片超过最大下载字节数")
        _validate_download_url(response.final_url)
        mime_type, width, height = _decode_image(response.body)
        response_mime = _content_type(response.headers)
        if response_mime != mime_type or record.mime_type != mime_type:
            raise ImageAcquisitionError("invalid_media", "响应、候选与解码图片的 MIME 不一致")
        if (width, height) != (record.width, record.height):
            raise ImageAcquisitionError("invalid_media", "下载图片尺寸与搜索候选不一致")

        digest = hashlib.sha256(response.body).hexdigest()
        extension = _SUPPORTED_MIME[mime_type]
        file_path, provenance_path = _output_paths(
            output_root,
            asset_id=record.provider_asset_id,
            digest=digest,
            extension=extension,
        )
        provenance = {
            "schema_version": _SCHEMA_VERSION,
            "acquired_at": datetime.now(UTC).isoformat(),
            "candidate_ref": candidate_ref,
            "search_query": manifest["query"],
            "source": {
                "provider": record.provider,
                "provider_asset_id": record.provider_asset_id,
                "title": record.title,
                "creator": record.creator,
                "source_url": record.source_url,
                "original_file_url": record.original_file_url,
                "verified_download_url": response.final_url,
            },
            "rights_record": {
                "license": record.license,
                "license_url": record.license_url,
                "scope": "provider_metadata_record_not_publishability_determination",
            },
            "artifact": {
                "file_path": str(file_path),
                "sha256": digest,
                "size_bytes": len(response.body),
                "mime_type": mime_type,
                "width": width,
                "height": height,
            },
        }
        _commit_artifact(output_root, response.body, file_path, provenance, provenance_path)
        return ImageAcquisitionResult(
            candidate_ref=candidate_ref,
            file_path=file_path,
            provenance_path=provenance_path,
            sha256=digest,
            size_bytes=len(response.body),
            provider=record.provider,
            provider_asset_id=record.provider_asset_id,
            title=record.title,
            creator=record.creator,
            source_url=record.source_url,
            license=record.license,
            license_url=record.license_url,
            mime_type=mime_type,
            width=width,
            height=height,
        )

    def _request(self, url: str, *, max_bytes: int) -> _HttpResponse:
        return self._http.get(
            url,
            headers={"User-Agent": self.config.user_agent, "Accept": "application/json,image/*"},
            timeout_seconds=self.config.request_timeout_seconds,
            max_bytes=max_bytes,
        )


def _build_search_url(query: str, *, limit: int, thumbnail_width: int) -> str:
    params = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",
        "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|mime|size|extmetadata",
        "iiurlwidth": str(thumbnail_width),
        "iiextmetadatalanguage": "en",
        "uselang": "en",
    }
    return f"{_API_URL}?{urlencode(params)}"


def _validate_config(config: ImageAcquisitionConfig) -> None:
    if config.request_timeout_seconds <= 0:
        raise ImageAcquisitionError("invalid_config", "request_timeout_seconds 必须大于 0")
    if config.max_download_bytes <= 0:
        raise ImageAcquisitionError("invalid_config", "max_download_bytes 必须大于 0")
    if not 320 <= config.thumbnail_width <= 4096:
        raise ImageAcquisitionError("invalid_config", "thumbnail_width 必须位于 320 到 4096")
    if not config.user_agent.strip():
        raise ImageAcquisitionError("invalid_config", "user_agent 为空")


def _parse_search_payload(payload: Any) -> list[_CandidateRecord]:
    if not isinstance(payload, dict):
        raise ImageAcquisitionError("search_failed", "图片搜索响应结构无效")
    query = payload.get("query")
    if not isinstance(query, dict):
        return []
    pages = query.get("pages")
    if not isinstance(pages, list):
        return []
    records: list[_CandidateRecord] = []
    for page in pages:
        record = _parse_page(page)
        if record is not None:
            records.append(record)
    return records


def _parse_page(page: Any) -> _CandidateRecord | None:
    if not isinstance(page, dict):
        return None
    image_info = page.get("imageinfo")
    if not isinstance(image_info, list) or not image_info or not isinstance(image_info[0], dict):
        return None
    info = image_info[0]
    metadata = info.get("extmetadata")
    if not isinstance(metadata, dict):
        return None
    mime_type = str(info.get("thumbmime") or info.get("mime") or "").lower()
    if mime_type not in _SUPPORTED_MIME:
        return None
    creator = _plain_text(_metadata_value(metadata, "Artist"))
    license_name = _plain_text(_metadata_value(metadata, "LicenseShortName"))
    license_url = _metadata_value(metadata, "LicenseUrl").strip()
    source_url = str(info.get("descriptionurl") or "").strip()
    original_file_url = str(info.get("url") or "").strip()
    download_url = str(info.get("thumburl") or original_file_url).strip()
    width = _positive_int(info.get("thumbwidth") or info.get("width"))
    height = _positive_int(info.get("thumbheight") or info.get("height"))
    page_id = _positive_int(page.get("pageid"))
    title = _plain_text(_metadata_value(metadata, "ObjectName"))
    if not title:
        title = str(page.get("title") or "").removeprefix("File:").strip()
    required = (
        creator,
        license_name,
        license_url,
        source_url,
        original_file_url,
        download_url,
        title,
    )
    if not all(required) or min(width, height, page_id) <= 0:
        return None
    if urlparse(license_url).scheme not in {"http", "https"}:
        return None
    try:
        _validate_download_url(download_url)
    except ImageAcquisitionError:
        return None
    return _CandidateRecord(
        provider="wikimedia_commons",
        provider_asset_id=str(page_id),
        title=title,
        creator=creator,
        source_url=source_url,
        download_url=download_url,
        original_file_url=original_file_url,
        license=license_name,
        license_url=license_url,
        mime_type=mime_type,
        width=width,
        height=height,
    )


def _metadata_value(metadata: dict[str, Any], key: str) -> str:
    item = metadata.get(key)
    if not isinstance(item, dict):
        return ""
    value = item.get("value")
    return value if isinstance(value, str) else ""


def _plain_text(value: str) -> str:
    return " ".join(html.unescape(_HTML_TAG.sub(" ", value)).split())


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _make_candidate_ref(token: str, query: str, record: _CandidateRecord) -> str:
    payload = {"query": query, "record": asdict(record)}
    digest = hashlib.sha256(
        token.encode("ascii") + _canonical_json(payload).encode("utf-8")
    ).hexdigest()[:32]
    return f"image_{token}_{digest}"


def _public_candidate(candidate_ref: str, record: _CandidateRecord) -> ImageCandidate:
    return ImageCandidate(
        candidate_ref=candidate_ref,
        provider=record.provider,
        provider_asset_id=record.provider_asset_id,
        title=record.title,
        creator=record.creator,
        source_url=record.source_url,
        license=record.license,
        license_url=record.license_url,
        mime_type=record.mime_type,
        width=record.width,
        height=record.height,
    )


def _read_search_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ImageAcquisitionError("candidate_ref_expired", "candidate_ref 对应的搜索记录不存在")
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImageAcquisitionError("candidate_ref_invalid", "搜索记录不可读取") from error
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _SCHEMA_VERSION
        or not isinstance(payload.get("query"), str)
        or not isinstance(payload.get("candidates"), dict)
    ):
        raise ImageAcquisitionError("candidate_ref_invalid", "搜索记录结构无效")
    return payload


def _validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "upload.wikimedia.org":
        raise ImageAcquisitionError("download_url_invalid", "下载地址不属于 Wikimedia 上传域名")


def _decode_image(body: bytes) -> tuple[str, int, int]:
    try:
        with Image.open(BytesIO(body)) as image:
            image.load()
            mime_type = Image.MIME.get(image.format or "", "").lower()
            width, height = image.size
    except (OSError, UnidentifiedImageError) as error:
        raise ImageAcquisitionError("invalid_media", "下载内容不是可解码图片") from error
    if mime_type not in _SUPPORTED_MIME or width <= 0 or height <= 0:
        raise ImageAcquisitionError("invalid_media", "图片格式或尺寸不受支持")
    return mime_type, width, height


def _content_type(headers: Mapping[str, str]) -> str:
    for key, value in headers.items():
        if key.lower() == "content-type":
            return value.split(";", 1)[0].strip().lower()
    return ""


def _prepare_output_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    try:
        for name in ("searches", "downloads", "provenance"):
            (root / name).mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ImageAcquisitionError("output_unavailable", f"图片输出目录不可写：{root}") from error
    return root


def _output_paths(
    output_root: Path,
    *,
    asset_id: str,
    digest: str,
    extension: str,
) -> tuple[Path, Path]:
    safe_id = re.sub(r"[^0-9A-Za-z_-]+", "_", asset_id).strip("_") or "asset"
    base = f"wikimedia_{safe_id}_{digest[:12]}"
    index = 1
    while True:
        suffix = "" if index == 1 else f"_{index}"
        file_path = (output_root / "downloads" / f"{base}{suffix}{extension}").resolve()
        provenance_path = (output_root / "provenance" / f"{base}{suffix}.json").resolve()
        if not file_path.exists() and not provenance_path.exists():
            return file_path, provenance_path
        index += 1


def _commit_artifact(
    output_root: Path,
    body: bytes,
    file_path: Path,
    provenance: dict[str, Any],
    provenance_path: Path,
) -> None:
    try:
        with tempfile.TemporaryDirectory(prefix=".image-", dir=output_root) as temp_name:
            temp_dir = Path(temp_name)
            temp_image = temp_dir / file_path.name
            temp_provenance = temp_dir / provenance_path.name
            temp_image.write_bytes(body)
            temp_provenance.write_text(
                json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temp_image, file_path)
            try:
                os.replace(temp_provenance, provenance_path)
            except OSError:
                file_path.unlink(missing_ok=True)
                raise
    except OSError as error:
        raise ImageAcquisitionError("output_unavailable", "图片或溯源记录写入失败") from error


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, path)
    except OSError as error:
        temp_path.unlink(missing_ok=True)
        raise ImageAcquisitionError("output_unavailable", "图片搜索记录写入失败") from error


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
