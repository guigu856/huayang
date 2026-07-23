from __future__ import annotations

import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from video_create_plugin.analysis import (
    AnalysisJobError,
    ReferenceAnalysisService,
    Status,
)


def _make_video(path: Path, *, with_audio: bool = True, fps: int = 20) -> Path:
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=s=96x64:r={fps}:d=1",
    ]
    if with_audio:
        argv.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=44100:duration=1",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:a",
                "aac",
                "-shortest",
            ]
        )
    else:
        argv.extend(["-map", "0:v:0", "-an"])
    argv.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return path


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert all(isinstance(value, dict) for value in values)
    return values


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def test_start_persists_dual_analysis_lifecycle_and_reuses_by_content(
    tmp_path: Path,
) -> None:
    source = _make_video(tmp_path / "reference.mp4")
    service = ReferenceAnalysisService(tmp_path / "store")

    result = service.start(source)

    assert result.has_audio is True
    assert result.reused is False
    assert result.reference_manifest_path.is_file()
    assert result.audio_manifest_path is not None
    job = service.get(result.job_id)
    assert job.status is Status.SUCCEEDED
    assert [event.status for event in job.status_history] == [
        Status.QUEUED,
        Status.RUNNING,
        Status.SUCCEEDED,
    ]
    assert service.list() == (job,)
    assert (result.job_dir / "job.json").is_file()

    manifest = _read_json(result.reference_manifest_path)
    bundle = manifest["evidence_bundle"]
    assert result.reference_manifest_sha256 == _sha256(result.reference_manifest_path)
    assert result.evidence_bundle_sha256 == _canonical_sha256(bundle["entries"])
    assert bundle["sha256"] == result.evidence_bundle_sha256
    assert {entry["kind"] for entry in bundle["entries"]} >= {
        "visual_evidence_manifest",
        "audio_evidence_manifest",
        "visual:frame_index",
        "audio:beat_grid",
    }

    frames = _read_jsonl(result.job_dir / "visual" / "frame_index.jsonl")
    assert frames
    for frame in frames:
        numerator, denominator = frame["time_base"].split("/", maxsplit=1)
        expected = round(
            Fraction(frame["pts"]) * Fraction(int(numerator), int(denominator)) * 1_000_000
        )
        assert frame["timestamp_us"] == expected

    repeated = service.start(source)
    assert repeated.job_id == result.job_id
    assert repeated.reused is True
    assert service.get(result.job_id).status_history == job.status_history


def test_dense_refinement_uses_real_pts_and_at_most_point_one_second_gaps(
    tmp_path: Path,
) -> None:
    source = _make_video(tmp_path / "reference.mp4", fps=20)
    service = ReferenceAnalysisService(tmp_path / "store")
    result = service.start(source)

    refinement_path = service.refine_intervals(
        result.job_id,
        [(100_000, 600_000)],
        max_interval_us=100_000,
    )

    refinement = _read_json(refinement_path)
    assert refinement["sampling"]["timestamp_source"] == "visual_frame_index_pts"
    interval = refinement["sampling"]["intervals"][0]
    assert interval["maximum_observed_gap_us"] <= 100_000
    timestamps = [sample["timestamp_us"] for sample in interval["samples"]]
    assert all(right - left <= 100_000 for left, right in zip(timestamps, timestamps[1:]))
    for sample in interval["samples"]:
        numerator, denominator = sample["time_base"].split("/", maxsplit=1)
        assert sample["timestamp_us"] == round(
            Fraction(sample["pts"]) * Fraction(int(numerator), int(denominator)) * 1_000_000
        )
        image = refinement_path.parent / sample["path"]
        assert image.is_file()
        assert sample["sha256"] == _sha256(image)
    validated = service.validate(result.job_id)
    assert validated.evidence_bundle_sha256 != result.evidence_bundle_sha256
    repeated_path = service.refine_intervals(
        result.job_id,
        [(100_000, 600_000)],
        max_interval_us=100_000,
    )
    assert repeated_path == refinement_path
    assert service.validate(result.job_id).evidence_bundle_sha256 == (
        validated.evidence_bundle_sha256
    )


def test_invalid_media_records_a_failed_terminal_state(tmp_path: Path) -> None:
    source = tmp_path / "invalid.mp4"
    source.write_bytes(b"not a media file")
    service = ReferenceAnalysisService(tmp_path / "store")

    with pytest.raises(AnalysisJobError) as captured:
        service.start(source)

    assert captured.value.code.startswith("video_analysis.")
    jobs = service.list()
    assert len(jobs) == 1
    assert jobs[0].status is Status.FAILED
    assert [event.status for event in jobs[0].status_history] == [
        Status.QUEUED,
        Status.RUNNING,
        Status.FAILED,
    ]
    assert jobs[0].failure is not None
    assert jobs[0].failure.code == captured.value.code


def test_restart_marks_a_persisted_running_job_interrupted(tmp_path: Path) -> None:
    source = _make_video(tmp_path / "reference.mp4", with_audio=False)
    service = ReferenceAnalysisService(tmp_path / "store")
    result = service.start(source)
    job_path = result.job_dir / "job.json"
    job_payload = _read_json(job_path)
    job_payload["status"] = "running"
    job_payload["status_history"].append(
        {"status": "running", "timestamp": "2026-01-01T00:00:00+00:00"}
    )
    job_payload["result"] = None
    job_path.write_text(
        json.dumps(job_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    restarted = ReferenceAnalysisService(tmp_path / "store")

    recovered = restarted.get(result.job_id)
    assert recovered.status is Status.INTERRUPTED
    assert recovered.status_history[-1].reason == "service_restart"
    assert _read_json(job_path)["status"] == "interrupted"


def test_no_audio_is_explicit_and_validation_detects_tampering(tmp_path: Path) -> None:
    source = _make_video(tmp_path / "silent.mp4", with_audio=False)
    service = ReferenceAnalysisService(tmp_path / "store")
    result = service.start(source)

    assert result.has_audio is False
    assert result.audio_manifest_path is None
    manifest = _read_json(result.reference_manifest_path)
    assert manifest["audio_analysis"] == {"status": "not_present"}
    service.validate(result.job_id)

    target_entry = next(
        entry
        for entry in manifest["evidence_bundle"]["entries"]
        if entry["kind"] == "visual:frame_index"
    )
    target = result.job_dir / target_entry["path"]
    target.write_text(target.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(AnalysisJobError) as captured:
        service.validate(result.job_id)
    assert captured.value.code == "artifact_hash_mismatch"
