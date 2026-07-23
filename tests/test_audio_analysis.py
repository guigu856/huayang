from __future__ import annotations

import hashlib
import json
import math
import subprocess
import wave
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from components.audio_analysis import AudioAnalysisError, AudioAnalysisService


def _write_click_track(
    path: Path,
    *,
    bpm: float = 120.0,
    duration_seconds: float = 4.0,
    sample_rate: int = 44_100,
) -> Path:
    sample_count = round(duration_seconds * sample_rate)
    samples = np.zeros(sample_count, dtype=np.float64)
    click_length = round(0.015 * sample_rate)
    click_time = np.arange(click_length, dtype=np.float64) / sample_rate
    click = np.sin(2 * math.pi * 1_500 * click_time) * np.linspace(1.0, 0.0, click_length)
    period = round(60 * sample_rate / bpm)
    for start in range(0, sample_count, period):
        end = min(sample_count, start + click_length)
        samples[start:end] += click[: end - start]
    pcm = np.clip(samples * 30_000, -32_768, 32_767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())
    return path


def _write_silence(
    path: Path,
    *,
    duration_seconds: float = 2.0,
    sample_rate: int = 44_100,
) -> Path:
    pcm = np.zeros(round(duration_seconds * sample_rate), dtype="<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm.tobytes())
    return path


def _run_ffmpeg(argv: list[str]) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *argv],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_click_track_produces_candidate_tempo_grid_and_hashed_evidence(
    tmp_path: Path,
) -> None:
    source = _write_click_track(tmp_path / "clicks.wav")

    result = AudioAnalysisService().analyze(source, tmp_path / "analysis")

    probe = _read_json(result.media_probe_path)
    assert probe["audio_scope"] == "mixed_program_audio"
    assert probe["stream_selection"]["criterion"] == "codec_type=audio"
    assert probe["decode"]["sample_format"] == "f32le"
    assert probe["decode"]["channels"] == 1
    tempo = _read_json(result.tempo_candidates_path)
    assert tempo["candidate_semantics"]
    assert all(item["status"] == "candidate" for item in tempo["candidates"])
    assert any(115 <= item["bpm"] <= 125 for item in tempo["candidates"])
    beat_grid = _read_json(result.beat_grid_path)
    assert beat_grid["status"] == "derived_from_tempo_candidate"
    assert len(beat_grid["beats"]) >= 6

    manifest = _read_json(result.evidence_manifest_path)
    assert manifest["audio_scope"] == "mixed_program_audio"
    assert result.manifest_sha256 == _sha256(result.evidence_manifest_path)
    for artifact in manifest["artifacts"]:
        artifact_path = result.output_dir / artifact["path"]
        assert artifact_path.is_file()
        assert artifact["sha256"] == _sha256(artifact_path)
        assert artifact["algorithm_version"] == result.algorithm_version


def test_silence_has_a_region_and_no_tempo_candidate(tmp_path: Path) -> None:
    source = _write_silence(tmp_path / "silence.wav")

    result = AudioAnalysisService().analyze(source, tmp_path / "analysis")

    silence = _read_json(result.silence_regions_path)
    assert len(silence["regions"]) == 1
    assert silence["regions"][0]["start_sample_index"] == 0
    assert silence["regions"][0]["duration_us"] >= 1_900_000
    assert _read_json(result.tempo_candidates_path)["candidates"] == []
    assert _read_json(result.beat_grid_path)["beats"] == []
    sections = _read_json(result.section_candidates_path)
    assert len(sections["sections"]) == 1
    assert sections["sections"][0]["status"] == "candidate"


def test_video_without_audio_raises_a_stable_error(tmp_path: Path) -> None:
    source = tmp_path / "silent-video.mp4"
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=10:d=0.5",
            "-an",
            "-c:v",
            "mpeg4",
            str(source),
        ]
    )

    with pytest.raises(AudioAnalysisError) as captured:
        AudioAnalysisService().analyze(source, tmp_path / "analysis")

    assert captured.value.code == "audio_stream_not_found"


def test_sample_timestamps_are_derived_from_stream_time_base(tmp_path: Path) -> None:
    wav_path = _write_click_track(tmp_path / "source.wav", duration_seconds=1.0)
    source = tmp_path / "offset.mkv"
    _run_ffmpeg(
        [
            "-itsoffset",
            "0.375",
            "-i",
            str(wav_path),
            "-map",
            "0:a:0",
            "-c:a",
            "pcm_s16le",
            "-copyts",
            str(source),
        ]
    )

    result = AudioAnalysisService().analyze(source, tmp_path / "analysis")

    probe = _read_json(result.media_probe_path)
    timeline = probe["timeline"]
    assert timeline["start_source"] == "start_pts"
    numerator_text, denominator_text = timeline["time_base"].split("/", maxsplit=1)
    expected_start = round(
        Fraction(timeline["start_pts"])
        * Fraction(int(numerator_text), int(denominator_text))
        * 1_000_000
    )
    assert timeline["start_timestamp_us"] == expected_start
    frames = _read_json(result.energy_curve_path)["frames"]
    for frame in frames[:20]:
        expected_timestamp = expected_start + round(
            Fraction(frame["sample_index"], result.sample_rate) * 1_000_000
        )
        assert frame["timestamp_us"] == expected_timestamp


def test_audio_stream_is_selected_by_codec_type_not_stream_position(
    tmp_path: Path,
) -> None:
    wav_path = _write_click_track(tmp_path / "source.wav", duration_seconds=1.0)
    source = tmp_path / "video-first.mp4"
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x64:r=10:d=1",
            "-i",
            str(wav_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ]
    )

    result = AudioAnalysisService().analyze(source, tmp_path / "analysis")

    probe = _read_json(result.media_probe_path)
    assert probe["ffprobe"]["streams"][0]["codec_type"] == "video"
    assert probe["selected_audio_stream"]["codec_type"] == "audio"
    assert probe["stream_selection"]["selected_stream_index"] == 1
