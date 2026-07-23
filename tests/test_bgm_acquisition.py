from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from components import bgm_acquisition as package
from components.bgm_acquisition import (
    BgmAcquisitionConfig,
    BgmAcquisitionError,
    BgmAcquisitionService,
)
from components.bgm_acquisition import service as service_module

CATALOG_REQUEST = "https://mixkit.co/free-stock-music/discover/energetic/"
CATALOG_FINAL = "https://mixkit.co/free-stock-music/mood/energetic/"
MODAL_URL = "https://mixkit.co/free-stock-music/download/738/?context=item+grid"
DOWNLOAD_URL = "https://assets.mixkit.co/music/738/738.mp3"
LICENSE_EVIDENCE_URL = "https://mixkit.co/license/modal/musicFree/"
TERMS_URL = "https://mixkit.co/terms/"


def _catalog_html(*, preview_url: str = DOWNLOAD_URL, title: str = "Pulse") -> bytes:
    return f"""
    <html><body><div class="item-grid__item">
      <div data-test-id="audio-player"
           data-audio-player-preview-url-value="{preview_url}"
           data-audio-player-item-id-value="738"></div>
      <h2 class="item-grid-card__title">{title}</h2>
      <p class="item-grid-music-preview__author">by Test Artist</p>
      <a class="meta-links__link" href="/tag/energetic/">Energetic</a>
      <a class="meta-links__link" href="/instrument/drums/">Drums</a>
      <div data-test-id="duration">0:02</div>
      <button
        data-download--button-modal-url-value="/free-stock-music/download/738/?context=item+grid"
      >Download</button>
    </div></body></html>
    """.encode()


class FakeHttpClient:
    def __init__(self, audio: bytes = b"audio") -> None:
        self.audio = audio
        self.calls: list[str] = []
        self.catalog = _catalog_html()

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
        responses = {
            CATALOG_REQUEST: service_module._HttpResponse(
                200, CATALOG_FINAL, {"content-type": "text/html; charset=utf-8"}, self.catalog
            ),
            CATALOG_FINAL: service_module._HttpResponse(
                200, CATALOG_FINAL, {"content-type": "text/html; charset=utf-8"}, self.catalog
            ),
            MODAL_URL: service_module._HttpResponse(
                200,
                MODAL_URL,
                {"content-type": "text/html"},
                f'<div data-download--modal-url-value="{DOWNLOAD_URL}"></div>'.encode(),
            ),
            LICENSE_EVIDENCE_URL: service_module._HttpResponse(
                200,
                LICENSE_EVIDENCE_URL,
                {"content-type": "text/html"},
                b"<h1>Stock Music Free License</h1>",
            ),
            TERMS_URL: service_module._HttpResponse(
                200,
                TERMS_URL,
                {"content-type": "text/html"},
                b"<h1>User Terms</h1>",
            ),
            DOWNLOAD_URL: service_module._HttpResponse(
                200, DOWNLOAD_URL, {"content-type": "audio/mpeg"}, self.audio
            ),
        }
        response = responses[url]
        if len(response.body) > max_bytes:
            return service_module._HttpResponse(
                response.status_code,
                response.final_url,
                response.headers,
                response.body[: max_bytes + 1],
            )
        return response


def _service(tmp_path: Path, http: FakeHttpClient) -> BgmAcquisitionService:
    return BgmAcquisitionService(
        BgmAcquisitionConfig(output_dir=tmp_path),
        http_client=http,
    )


def _search(tmp_path: Path, http: FakeHttpClient) -> tuple[BgmAcquisitionService, str]:
    service = _service(tmp_path, http)
    result = service.search("energetic", limit=1)
    return service, result.candidates[0].candidate_ref


def _make_mp3(tmp_path: Path) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is required for the media integration assertion")
    path = tmp_path / "tone.mp3"
    completed = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(path),
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    return path.read_bytes()


def test_list_sources_exposes_official_evidence_pages() -> None:
    source = BgmAcquisitionService().list_sources()[0]

    assert source.name == "mixkit"
    assert source.catalog_url == "https://mixkit.co/free-stock-music/"
    assert source.license_url == "https://mixkit.co/license/#musicFree"
    assert source.terms_url == TERMS_URL
    assert source.access_mode == "public_catalog_no_api_key_single_selection"


def test_search_returns_opaque_candidate_and_persists_private_urls(tmp_path: Path) -> None:
    result = _service(tmp_path, FakeHttpClient()).search("energetic", limit=1)

    assert result.source_page_url == CATALOG_FINAL
    assert len(result.candidates) == 1
    public = result.candidates[0].to_dict()
    assert public["title"] == "Pulse"
    assert public["creator"] == "Test Artist"
    assert public["tags"] == ["Energetic", "Drums"]
    assert public["candidate_ref"].startswith("bgm_")
    assert "preview_url" not in public
    assert "modal_url" not in public
    manifest = json.loads(next((tmp_path / "searches").glob("*.json")).read_text())
    private = next(iter(manifest["candidates"].values()))
    assert private["preview_url"] == DOWNLOAD_URL
    assert private["modal_url"] == MODAL_URL


def test_search_validates_query_and_limit(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeHttpClient())

    with pytest.raises(BgmAcquisitionError, match="搜索词") as empty:
        service.search("  ")
    assert empty.value.code == "invalid_query"
    with pytest.raises(BgmAcquisitionError) as unsupported:
        service.search("高燃")
    assert unsupported.value.code == "query_not_supported"
    with pytest.raises(BgmAcquisitionError) as invalid_limit:
        service.search("energetic", limit=21)
    assert invalid_limit.value.code == "invalid_limit"


def test_acquire_rejects_arbitrary_reference_without_http(tmp_path: Path) -> None:
    http = FakeHttpClient()
    service = _service(tmp_path, http)

    with pytest.raises(BgmAcquisitionError) as captured:
        service.acquire("https://assets.mixkit.co/music/738/738.mp3")

    assert captured.value.code == "candidate_ref_invalid"
    assert http.calls == []


def test_acquire_rejects_tampered_private_manifest(tmp_path: Path) -> None:
    http = FakeHttpClient()
    service, candidate_ref = _search(tmp_path, http)
    manifest_path = next((tmp_path / "searches").glob("*.json"))
    manifest = json.loads(manifest_path.read_text())
    manifest["candidates"][candidate_ref]["preview_url"] = (
        "https://assets.mixkit.co/music/999/999.mp3"
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    calls_before = list(http.calls)

    with pytest.raises(BgmAcquisitionError) as captured:
        service.acquire(candidate_ref)

    assert captured.value.code in {"candidate_ref_invalid", "download_url_invalid"}
    assert http.calls == calls_before


def test_acquire_revalidates_current_source_page(tmp_path: Path) -> None:
    http = FakeHttpClient()
    service, candidate_ref = _search(tmp_path, http)
    http.catalog = _catalog_html(title="Changed Title")

    with pytest.raises(BgmAcquisitionError) as captured:
        service.acquire(candidate_ref)

    assert captured.value.code == "source_evidence_changed"
    assert DOWNLOAD_URL not in http.calls


def test_acquire_rejects_download_url_for_different_asset(tmp_path: Path) -> None:
    http = FakeHttpClient()
    service, candidate_ref = _search(tmp_path, http)
    http.catalog = _catalog_html(preview_url="https://assets.mixkit.co/music/999/999.mp3")

    with pytest.raises(BgmAcquisitionError) as captured:
        service.acquire(candidate_ref)

    assert captured.value.code == "source_evidence_changed"


def test_acquire_validates_audio_and_keeps_original_separate_from_clip(
    tmp_path: Path,
) -> None:
    http = FakeHttpClient(_make_mp3(tmp_path))
    service, candidate_ref = _search(tmp_path / "output", http)

    result = service.acquire(
        candidate_ref,
        clip_start_seconds=0.25,
        clip_duration_seconds=1.0,
    )

    assert result.original_path.is_file()
    assert result.original_path.parent == (tmp_path / "output" / "originals").resolve()
    assert result.derived_path is not None and result.derived_path.is_file()
    assert result.derived_path.parent == (tmp_path / "output" / "derivatives").resolve()
    assert result.selected_path == result.derived_path
    assert result.original_sha256 == hashlib.sha256(result.original_path.read_bytes()).hexdigest()
    assert result.derived_sha256 == hashlib.sha256(result.derived_path.read_bytes()).hexdigest()
    provenance = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert provenance["source_evidence"]["verified_url"] == CATALOG_FINAL
    assert provenance["rights_record"]["provider_label"] == ("Mixkit Stock Music Free License")
    assert provenance["rights_record"]["scope"] == (
        "provider_page_record_not_publishability_determination"
    )
    assert provenance["derivative"]["relation"] == "clip_of_original"
    assert provenance["derivative"]["parent_sha256"] == result.original_sha256
    assert provenance["original"]["file_path"] != provenance["derivative"]["file_path"]


def test_acquire_rejects_clip_outside_original(tmp_path: Path) -> None:
    http = FakeHttpClient(_make_mp3(tmp_path))
    service, candidate_ref = _search(tmp_path / "output", http)

    with pytest.raises(BgmAcquisitionError) as captured:
        service.acquire(candidate_ref, clip_start_seconds=1.8, clip_duration_seconds=1.0)

    assert captured.value.code == "clip_out_of_range"
    assert list((tmp_path / "output" / "originals").glob("*.mp3")) == []


def test_package_exports_only_public_agent_contract() -> None:
    assert set(package.__all__) == {
        "BgmAcquisitionConfig",
        "BgmAcquisitionError",
        "BgmAcquisitionResult",
        "BgmAcquisitionService",
        "BgmCandidate",
        "BgmSearchResult",
        "BgmSource",
    }
    assert not hasattr(package, "_HttpResponse")
