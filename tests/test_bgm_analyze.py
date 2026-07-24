"""bgm_analyze 工具的核心逻辑测试。"""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np
import pytest

from components.bgm_analysis import BgmAnalysisService


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


class TestBgmAnalyzeReturnsStructuredResult:
    """bgm_analyze 对有效音频返回结构化分析摘要。"""

    def test_click_track_returns_top2_beat_grids_and_tempo(self, tmp_path: Path) -> None:
        source = _write_click_track(tmp_path / "clicks.wav", bpm=120.0, duration_seconds=4.0)
        service = BgmAnalysisService()

        result = service.analyze(source, tmp_path / "analysis")

        assert result["status"] == "succeeded"
        assert result["duration_us"] > 0
        assert result["transient_count"] > 0

        # tempo_candidates 包含 120 BPM 附近的候选
        tempos = result["tempo_candidates"]
        assert len(tempos) >= 1
        bpm_values = [t["bpm"] for t in tempos]
        assert any(abs(bpm - 120.0) < 5.0 for bpm in bpm_values), f"120 BPM not in {bpm_values}"

        # beat_grids 为 dict，key 是 tempo candidate id，value 是 timestamp_us 列表
        grids = result["beat_grids"]
        assert isinstance(grids, dict)
        assert len(grids) >= 1
        for key, beats in grids.items():
            assert isinstance(beats, list)
            assert all(isinstance(b, int) for b in beats)

        # energy_summary 是非空列表
        energy = result["energy_summary"]
        assert isinstance(energy, list)
        assert len(energy) >= 1
        assert "start_us" in energy[0]
        assert "end_us" in energy[0]
        assert "mean_normalized_energy" in energy[0]

        # analysis_dir 存在
        assert Path(result["analysis_dir"]).is_dir()


class TestBgmAnalyzeSilenceReturnsNoTempo:
    """静音文件返回 no_tempo_candidate 状态，不报错。"""

    def test_silence_returns_empty_grids_with_status(self, tmp_path: Path) -> None:
        source = _write_silence(tmp_path / "silence.wav", duration_seconds=2.0)
        service = BgmAnalysisService()

        result = service.analyze(source, tmp_path / "analysis")

        assert result["status"] == "no_tempo_candidate"
        assert result["beat_grids"] == {}
        assert result["tempo_candidates"] == []
        assert result["duration_us"] > 0
