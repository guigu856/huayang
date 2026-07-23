from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from components import image_acquisition as package
from components.image_acquisition import (
    ImageAcquisitionConfig,
    ImageAcquisitionError,
    ImageAcquisitionService,
)
from components.image_acquisition import service as service_module

DOWNLOAD_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a1/Test.jpg/1600px-Test.jpg"
SOURCE_URL = "https://commons.wikimedia.org/wiki/File:Test.jpg"
ORIGINAL_URL = "https://upload.wikimedia.org/wikipedia/commons/a/a1/Test.jpg"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0"


def _png_bytes(width: int = 64, height: int = 32) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), (24, 48, 96)).save(output, format="PNG")
    return output.getvalue()


def _api_payload(*, include_rights: bool = True) -> bytes:
    metadata = {
        "ObjectName": {"value": "Test image"},
        "Artist": {"value": '<a href="//commons.wikimedia.org/wiki/User:Tester">Tester</a>'},
    }
    if include_rights:
        metadata.update(
            {
                "LicenseShortName": {"value": "CC BY 4.0"},
                "LicenseUrl": {"value": LICENSE_URL},
            }
        )
    return json.dumps(
        {
            "query": {
                "pages": [
                    {
                        "pageid": 123,
                        "title": "File:Test.jpg",
                        "imageinfo": [
                            {
                                "url": ORIGINAL_URL,
                                "descriptionurl": SOURCE_URL,
                                "thumburl": DOWNLOAD_URL,
                                "mime": "image/png",
                                "thumbmime": "image/png",
                                "width": 640,
                                "height": 320,
                                "thumbwidth": 64,
                                "thumbheight": 32,
                                "extmetadata": metadata,
                            }
                        ],
                    }
                ]
            }
        }
    ).encode()


class FakeHttpClient:
    def __init__(
        self,
        *,
        api_body: bytes | None = None,
        image_body: bytes | None = None,
        image_content_type: str = "image/png",
    ) -> None:
        self.api_body = api_body or _api_payload()
        self.image_body = image_body or _png_bytes()
        self.image_content_type = image_content_type
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_bytes: int,
    ) -> service_module._HttpResponse:
        self.calls.append(url)
        assert headers["User-Agent"]
        assert timeout_seconds > 0
        if url.startswith("https://commons.wikimedia.org/w/api.php?"):
            body = self.api_body
            return service_module._HttpResponse(
                200,
                url,
                {"content-type": "application/json"},
                body[: max_bytes + 1],
            )
        assert url == DOWNLOAD_URL
        return service_module._HttpResponse(
            200,
            DOWNLOAD_URL,
            {"content-type": self.image_content_type},
            self.image_body[: max_bytes + 1],
        )


def _service(tmp_path: Path, http: FakeHttpClient) -> ImageAcquisitionService:
    return ImageAcquisitionService(
        ImageAcquisitionConfig(output_dir=tmp_path, thumbnail_width=1600),
        http_client=http,
    )


def _search(tmp_path: Path, http: FakeHttpClient) -> tuple[ImageAcquisitionService, str]:
    service = _service(tmp_path, http)
    result = service.search("city night", limit=1)
    return service, result.candidates[0].candidate_ref


def test_source_is_official_public_commons_api() -> None:
    source = ImageAcquisitionService().list_sources()[0]

    assert source.name == "wikimedia_commons"
    assert source.api_url == "https://commons.wikimedia.org/w/api.php"
    assert source.access_mode == "public_mediawiki_api_no_key"


def test_invalid_network_config_is_rejected_before_request(tmp_path: Path) -> None:
    with pytest.raises(ImageAcquisitionError) as captured:
        ImageAcquisitionService(
            ImageAcquisitionConfig(output_dir=tmp_path, max_download_bytes=0),
            http_client=FakeHttpClient(),
        )

    assert captured.value.code == "invalid_config"


def test_search_returns_opaque_reference_and_persists_private_download_url(
    tmp_path: Path,
) -> None:
    result = _service(tmp_path, FakeHttpClient()).search("city night", limit=1)

    assert len(result.candidates) == 1
    candidate = result.candidates[0].to_dict()
    assert candidate["candidate_ref"].startswith("image_")
    assert candidate["creator"] == "Tester"
    assert candidate["license"] == "CC BY 4.0"
    assert "download_url" not in candidate
    assert "thumbnail_url" not in candidate
    manifest = json.loads(next((tmp_path / "searches").glob("*.json")).read_text("utf-8"))
    private = next(iter(manifest["candidates"].values()))
    assert private["download_url"] == DOWNLOAD_URL


def test_search_excludes_candidate_without_license_evidence(tmp_path: Path) -> None:
    result = _service(
        tmp_path,
        FakeHttpClient(api_body=_api_payload(include_rights=False)),
    ).search("city night", limit=1)

    assert result.candidates == ()


def test_acquire_rejects_arbitrary_or_tampered_reference(tmp_path: Path) -> None:
    http = FakeHttpClient()
    service, candidate_ref = _search(tmp_path, http)
    calls_after_search = list(http.calls)

    with pytest.raises(ImageAcquisitionError) as arbitrary:
        service.acquire("https://upload.wikimedia.org/example.png")
    assert arbitrary.value.code == "candidate_ref_invalid"
    assert http.calls == calls_after_search

    manifest_path = next((tmp_path / "searches").glob("*.json"))
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["candidates"][candidate_ref]["creator"] = "Changed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ImageAcquisitionError) as tampered:
        service.acquire(candidate_ref)
    assert tampered.value.code == "candidate_ref_invalid"
    assert http.calls == calls_after_search


def test_acquire_validates_image_and_writes_atomic_provenance(tmp_path: Path) -> None:
    body = _png_bytes()
    http = FakeHttpClient(image_body=body)
    service, candidate_ref = _search(tmp_path, http)

    result = service.acquire(candidate_ref)

    assert result.file_path.is_file()
    assert result.provenance_path.is_file()
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert (result.width, result.height, result.mime_type) == (64, 32, "image/png")
    provenance = json.loads(result.provenance_path.read_text("utf-8"))
    assert provenance["source"]["creator"] == "Tester"
    assert provenance["rights_record"]["license"] == "CC BY 4.0"
    assert provenance["rights_record"]["license_url"] == LICENSE_URL
    assert provenance["artifact"]["sha256"] == result.sha256
    assert not list(tmp_path.glob(".image-*"))


def test_acquire_rejects_mime_or_dimensions_that_differ_from_candidate(tmp_path: Path) -> None:
    mime_http = FakeHttpClient(image_content_type="image/jpeg")
    mime_service, mime_ref = _search(tmp_path / "mime", mime_http)
    with pytest.raises(ImageAcquisitionError) as mime_error:
        mime_service.acquire(mime_ref)
    assert mime_error.value.code == "invalid_media"

    size_http = FakeHttpClient(image_body=_png_bytes(32, 32))
    size_service, size_ref = _search(tmp_path / "size", size_http)
    with pytest.raises(ImageAcquisitionError) as size_error:
        size_service.acquire(size_ref)
    assert size_error.value.code == "invalid_media"


def test_package_exports_only_public_contract() -> None:
    assert set(package.__all__) == {
        "ImageAcquisitionConfig",
        "ImageAcquisitionError",
        "ImageAcquisitionResult",
        "ImageAcquisitionService",
        "ImageCandidate",
        "ImageSearchResult",
        "ImageSource",
    }
    assert not hasattr(package, "_HttpResponse")
