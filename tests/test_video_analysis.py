from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from components.video_analysis import VideoAnalysisError, VideoAnalysisService


def _make_color_video(
    path: Path,
    colors: list[str],
    *,
    segment_duration: float,
    fps: int = 20,
) -> Path:
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for color in colors:
        argv.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=96x64:r={fps}:d={segment_duration}",
            ]
        )
    inputs = "".join(f"[{index}:v]" for index in range(len(colors)))
    argv.extend(
        [
            "-filter_complex",
            f"{inputs}concat=n={len(colors)}:v=1:a=0,format=yuv420p[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-g",
            str(fps * 10),
            "-bf",
            "0",
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
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        assert isinstance(value, dict)
        records.append(value)
    return records


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_analyze_detects_a_synthetic_hard_cut(tmp_path: Path) -> None:
    source = _make_color_video(
        tmp_path / "hard-cut.mp4",
        ["red", "blue"],
        segment_duration=0.5,
    )

    result = VideoAnalysisService().analyze(source, tmp_path / "analysis")

    boundaries = _read_json(result.boundary_candidates_path)
    cuts = [item for item in boundaries["candidates"] if item["kind"] == "scene_change_candidate"]
    assert any(400_000 <= item["timestamp_us"] <= 600_000 for item in cuts)
    assert result.candidate_frame_paths
    assert all(path.is_file() and path.suffix == ".jpg" for path in result.candidate_frame_paths)


def test_analyze_classifies_a_single_white_flash(tmp_path: Path) -> None:
    source = _make_color_video(
        tmp_path / "flash.mp4",
        ["black", "white", "black"],
        segment_duration=0.2,
    )

    result = VideoAnalysisService().analyze(source, tmp_path / "analysis")

    boundaries = _read_json(result.boundary_candidates_path)
    flashes = [item for item in boundaries["candidates"] if item["kind"] == "flash_candidate"]
    assert len(flashes) == 1
    assert flashes[0]["evidence_frames"]


def test_analyze_rejects_a_missing_source(tmp_path: Path) -> None:
    with pytest.raises(VideoAnalysisError) as captured:
        VideoAnalysisService().analyze(tmp_path / "missing.mp4", tmp_path / "analysis")

    assert captured.value.code == "source_not_found"
    assert captured.value.details["source"].endswith("missing.mp4")


def test_frame_index_uses_ffprobe_pts_and_fast_evidence_is_dense(tmp_path: Path) -> None:
    source = _make_color_video(
        tmp_path / "fast-cuts.mp4",
        ["red", "blue", "green", "white", "black", "yellow", "purple", "cyan"],
        segment_duration=0.1,
        fps=20,
    )

    result = VideoAnalysisService().analyze(source, tmp_path / "analysis")

    media_probe = _read_json(result.media_probe_path)
    time_base_text = media_probe["selected_video_stream"]["time_base"]
    numerator_text, denominator_text = time_base_text.split("/", maxsplit=1)
    numerator = int(numerator_text)
    denominator = int(denominator_text)
    frames = _read_jsonl(result.frame_index_path)
    assert frames
    for expected_index, frame in enumerate(frames):
        assert frame["frame_index"] == expected_index
        assert frame["time_base"] == time_base_text
        expected_us = round(frame["pts"] * numerator * 1_000_000 / denominator)
        assert frame["timestamp_us"] == expected_us
        assert "brightness" in frame
        assert "difference" in frame
        assert "histogram" in frame

    boundaries = _read_json(result.boundary_candidates_path)
    assert boundaries["dense_change_regions"]
    for region in boundaries["dense_change_regions"]:
        timestamps = [sample["timestamp_us"] for sample in region["evidence_samples"]]
        assert len(timestamps) >= 2
        assert all(right - left <= 100_000 for left, right in zip(timestamps, timestamps[1:]))
        assert region["classification"] == "dense_change_candidate"


def test_manifest_lists_and_hashes_every_generated_evidence(tmp_path: Path) -> None:
    source = _make_color_video(
        tmp_path / "manifest.mp4",
        ["red", "blue"],
        segment_duration=0.2,
    )

    result = VideoAnalysisService().analyze(source, tmp_path / "analysis")

    manifest = _read_json(result.evidence_manifest_path)
    entries = manifest["artifacts"]
    paths = {entry["path"] for entry in entries}
    assert {
        "media_probe.json",
        "frame_index.jsonl",
        "visual_signals.json",
        "boundary_candidates.json",
        "contact_sheet.jpg",
    }.issubset(paths)
    assert result.contact_sheet_path.is_file()
    assert result.manifest_sha256 == _sha256(result.evidence_manifest_path)
    for entry in entries:
        artifact_path = result.output_dir / entry["path"]
        assert artifact_path.is_file()
        assert entry["sha256"] == _sha256(artifact_path)
        assert entry["algorithm_version"] == result.algorithm_version
