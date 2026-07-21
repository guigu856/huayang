from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from components import material_acquisition as material_package
from components.material_acquisition import (
    MaterialAcquisitionConfig,
    MaterialAcquisitionError,
    MaterialAcquisitionService,
    SearchFilters,
)
from components.material_acquisition import __main__ as cli_module
from components.material_acquisition import service as service_module
from components.material_acquisition.base import Candidate, SourceError
from components.material_acquisition.engine import StockEngine


class FakeSource:
    def __init__(self, name: str, candidates: list[Candidate]) -> None:
        self.name = name
        self.candidates = candidates
        self.download_calls = 0

    def is_available(self) -> bool:
        return True

    async def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        return self.candidates

    async def download(self, candidate: Candidate, out_path: Path) -> Path:
        self.download_calls += 1
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes((candidate.source_id.encode("utf-8") + b"-video") * 256)
        return out_path


def _candidate(
    source: str,
    source_id: str,
    *,
    license_text: str = "CC0 1.0",
) -> Candidate:
    return Candidate(
        source=source,
        source_id=source_id,
        source_url=f"https://source.example/{source_id}",
        download_url=f"https://cdn.example/{source_id}.mp4",
        kind="video",
        width=1920,
        height=1080,
        duration=10.0,
        creator="Creator",
        license=license_text,
        source_tags="city night",
        thumbnail_url=f"https://cdn.example/{source_id}.jpg",
    )


def _service(tmp_path: Path, *sources: FakeSource) -> MaterialAcquisitionService:
    return MaterialAcquisitionService(
        MaterialAcquisitionConfig(output_dir=tmp_path),
        engine=StockEngine(sources=list(sources)),
    )


def test_search_limit_is_total_across_sources(tmp_path: Path) -> None:
    first = FakeSource("first", [_candidate("first", str(index)) for index in range(3)])
    second = FakeSource(
        "second", [_candidate("second", str(index)) for index in range(3)]
    )

    result = asyncio.run(_service(tmp_path, first, second).search("city night", limit=3))

    assert len(result.candidates) == 3


def test_search_hides_download_urls_from_agent_result(tmp_path: Path) -> None:
    source = FakeSource("stock", [_candidate("stock", "asset-1")])

    result = asyncio.run(_service(tmp_path, source).search("city night", limit=1))

    public_result = result.to_dict()
    public_candidate = public_result["candidates"][0]
    assert "search_ref" not in public_result
    assert "candidate_ref" in public_candidate
    assert "download_url" not in public_candidate
    search_path = next((tmp_path / "searches").glob("search_*.json"))
    private_manifest = json.loads(search_path.read_text(encoding="utf-8"))
    private_candidate = next(iter(private_manifest["candidates"].values()))
    assert private_candidate["download_url"] == "https://cdn.example/asset-1.mp4"


def test_search_rejects_ambiguous_archive_license(tmp_path: Path) -> None:
    source = FakeSource(
        "archive_org",
        [
            _candidate("archive_org", "ambiguous", license_text="Public Domain"),
            _candidate(
                "archive_org",
                "explicit",
                license_text="http://creativecommons.org/licenses/publicdomain/",
            ),
        ],
    )

    result = asyncio.run(_service(tmp_path, source).search("archive", limit=1))

    assert [candidate.provider_asset_id for candidate in result.candidates] == ["explicit"]


def test_acquire_downloads_validates_and_writes_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeSource("stock", [_candidate("stock", "asset-1")])
    service = _service(tmp_path, source)
    search = asyncio.run(service.search("city night", limit=1))
    candidate_ref = search.candidates[0].candidate_ref
    metadata = service_module._VideoMetadata(10.5, 1920, 1080, "h264", "aac")
    monkeypatch.setattr(service_module, "_probe_video", lambda path: metadata)

    result = asyncio.run(service.acquire(candidate_ref))

    assert result.file_path.is_file()
    assert result.file_path.parent == (tmp_path / "downloads").resolve()
    assert result.sha256 == hashlib.sha256(result.file_path.read_bytes()).hexdigest()
    provenance = json.loads(result.provenance_path.read_text(encoding="utf-8"))
    assert provenance["provider"] == "stock"
    assert provenance["provider_asset_id"] == "asset-1"
    assert provenance["candidate_ref"] == candidate_ref
    assert provenance["search_query"] == "city night"
    assert provenance["media"]["video_codec"] == "h264"


def test_repeated_acquire_does_not_overwrite_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = FakeSource("stock", [_candidate("stock", "same")])
    service = _service(tmp_path, source)
    search = asyncio.run(service.search("city night", limit=1))
    candidate_ref = search.candidates[0].candidate_ref
    monkeypatch.setattr(
        service_module,
        "_probe_video",
        lambda path: service_module._VideoMetadata(1.0, 640, 360, "h264", None),
    )

    first = asyncio.run(service.acquire(candidate_ref))
    second = asyncio.run(service.acquire(candidate_ref))

    assert first.file_path.name == "stock_same.mp4"
    assert second.file_path.name == "stock_same_2.mp4"
    assert first.file_path.is_file()
    assert second.file_path.is_file()


def test_acquire_rejects_arbitrary_url_without_downloading(tmp_path: Path) -> None:
    source = FakeSource("stock", [_candidate("stock", "asset-1")])
    service = _service(tmp_path, source)

    with pytest.raises(MaterialAcquisitionError) as captured:
        asyncio.run(service.acquire("https://cdn.example/arbitrary.mp4"))

    assert captured.value.code == "candidate_ref_invalid"
    assert source.download_calls == 0


def test_acquire_rejects_modified_search_manifest(tmp_path: Path) -> None:
    source = FakeSource("stock", [_candidate("stock", "asset-1")])
    service = _service(tmp_path, source)
    search = asyncio.run(service.search("city night", limit=1))
    candidate_ref = search.candidates[0].candidate_ref
    search_path = next((tmp_path / "searches").glob("search_*.json"))
    manifest = json.loads(search_path.read_text(encoding="utf-8"))
    manifest["candidates"][candidate_ref]["download_url"] = (
        "https://attacker.invalid/payload.mp4"
    )
    manifest["candidates"][candidate_ref]["license"] = "FORGED"
    search_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(MaterialAcquisitionError) as captured:
        asyncio.run(service.acquire(candidate_ref))

    assert captured.value.code == "candidate_ref_invalid"
    assert source.download_calls == 0


def test_engine_rejects_unregistered_candidate_source(tmp_path: Path) -> None:
    source = FakeSource("registered", [])
    engine = StockEngine(sources=[source])

    with pytest.raises(SourceError):
        asyncio.run(engine.download(_candidate("unknown", "asset"), tmp_path))

    assert source.download_calls == 0
    assert list(tmp_path.iterdir()) == []


def test_engine_rejects_unsafe_candidate_filename(tmp_path: Path) -> None:
    unsafe = _candidate("stock", "../outside")
    source = FakeSource("stock", [unsafe])
    engine = StockEngine(sources=[source])

    with pytest.raises(SourceError):
        asyncio.run(engine.download(unsafe, tmp_path))

    assert source.download_calls == 0
    assert not (tmp_path.parent / "outside.mp4").exists()


def test_probe_requires_video_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_path = tmp_path / "not-video.mp4"
    media_path.write_bytes(b"audio")
    monkeypatch.setattr(service_module.shutil, "which", lambda name: "ffprobe")
    monkeypatch.setattr(
        service_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"streams":[{"codec_type":"audio","codec_name":"aac"}]}',
            stderr="",
        ),
    )

    with pytest.raises(MaterialAcquisitionError) as captured:
        service_module._probe_video(media_path)

    assert captured.value.code == "invalid_media"


def test_cli_argument_error_is_json(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as captured_exit:
        cli_module.main([])

    captured = capsys.readouterr()
    assert captured_exit.value.code == 2
    assert captured.err == ""
    assert '"code": "invalid_arguments"' in captured.out


def test_sources_describes_configuration_not_health(tmp_path: Path) -> None:
    source = FakeSource("stock", [])

    result = _service(tmp_path, source).sources()

    assert result["available_sources"] == ["stock"]
    assert result["availability_scope"] == "configured_not_live_health_check"
    assert result["workflow"] == ["search", "acquire"]


def test_package_root_exports_only_agent_boundary() -> None:
    assert "MaterialAcquisitionService" in material_package.__all__
    assert "StockEngine" not in material_package.__all__
    assert "Candidate" not in material_package.__all__


def test_search_rejects_unavailable_explicit_source(tmp_path: Path) -> None:
    source = FakeSource("stock", [])

    with pytest.raises(MaterialAcquisitionError) as captured:
        asyncio.run(
            _service(tmp_path, source).search(
                "city night", source_names=["missing"], limit=1
            )
        )

    assert captured.value.code == "material_source_unavailable"
