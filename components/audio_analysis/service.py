"""从真实音频采样生成可复核的节奏与结构候选证据。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

ALGORITHM_VERSION = "audio-analysis-dsp-v1.0.0"
SCHEMA_VERSION = "1.0"
AUDIO_SCOPE = "mixed_program_audio"


class AudioAnalysisError(RuntimeError):
    """音频分析组件对外暴露的稳定错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class AudioAnalysisConfig:
    """确定性音频分析使用的本机工具与算法参数。"""

    ffprobe_binary: str = "ffprobe"
    ffmpeg_binary: str = "ffmpeg"
    sample_rate: int = 22_050
    frame_size: int = 1_024
    hop_size: int = 256
    silence_threshold_dbfs: float = -50.0
    minimum_silence_duration_ms: int = 250
    transient_flux_floor: float = 0.02
    transient_mad_multiplier: float = 3.5
    transient_minimum_separation_ms: int = 80
    minimum_tempo_bpm: float = 60.0
    maximum_tempo_bpm: float = 200.0
    maximum_tempo_candidates: int = 5
    minimum_tempo_transients: int = 3
    minimum_section_duration_ms: int = 1_000
    section_mad_multiplier: float = 2.5
    subprocess_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not self.ffprobe_binary or not self.ffmpeg_binary:
            raise ValueError("ffprobe_binary 与 ffmpeg_binary 不能为空")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate 必须大于 0")
        if self.frame_size < 16 or self.hop_size <= 0:
            raise ValueError("frame_size 与 hop_size 必须为有效正整数")
        if self.hop_size > self.frame_size:
            raise ValueError("hop_size 必须小于等于 frame_size")
        if self.minimum_silence_duration_ms <= 0:
            raise ValueError("minimum_silence_duration_ms 必须大于 0")
        if self.transient_flux_floor < 0 or self.transient_mad_multiplier < 0:
            raise ValueError("瞬态阈值参数必须大于等于 0")
        if self.transient_minimum_separation_ms <= 0:
            raise ValueError("transient_minimum_separation_ms 必须大于 0")
        if not 0 < self.minimum_tempo_bpm < self.maximum_tempo_bpm:
            raise ValueError("tempo BPM 范围无效")
        if self.maximum_tempo_candidates <= 0:
            raise ValueError("maximum_tempo_candidates 必须大于 0")
        if self.minimum_tempo_transients < 2:
            raise ValueError("minimum_tempo_transients 必须大于等于 2")
        if self.minimum_section_duration_ms <= 0:
            raise ValueError("minimum_section_duration_ms 必须大于 0")
        if self.section_mad_multiplier < 0:
            raise ValueError("section_mad_multiplier 必须大于等于 0")
        if self.subprocess_timeout_seconds <= 0:
            raise ValueError("subprocess_timeout_seconds 必须大于 0")


@dataclass(frozen=True, slots=True)
class AudioAnalysisResult:
    """一次成功音频分析的结构化产物索引。"""

    source_path: Path
    output_dir: Path
    evidence_manifest_path: Path
    media_probe_path: Path
    audio_signals_path: Path
    energy_curve_path: Path
    spectral_flux_path: Path
    transient_candidates_path: Path
    silence_regions_path: Path
    tempo_candidates_path: Path
    beat_grid_path: Path
    section_candidates_path: Path
    source_sha256: str
    manifest_sha256: str
    algorithm_version: str
    audio_scope: str
    sample_rate: int
    sample_count: int
    duration_us: int
    transient_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": str(self.source_path),
            "output_dir": str(self.output_dir),
            "evidence_manifest_path": str(self.evidence_manifest_path),
            "media_probe_path": str(self.media_probe_path),
            "audio_signals_path": str(self.audio_signals_path),
            "energy_curve_path": str(self.energy_curve_path),
            "spectral_flux_path": str(self.spectral_flux_path),
            "transient_candidates_path": str(self.transient_candidates_path),
            "silence_regions_path": str(self.silence_regions_path),
            "tempo_candidates_path": str(self.tempo_candidates_path),
            "beat_grid_path": str(self.beat_grid_path),
            "section_candidates_path": str(self.section_candidates_path),
            "source_sha256": self.source_sha256,
            "manifest_sha256": self.manifest_sha256,
            "algorithm_version": self.algorithm_version,
            "audio_scope": self.audio_scope,
            "sample_rate": self.sample_rate,
            "sample_count": self.sample_count,
            "duration_us": self.duration_us,
            "transient_count": self.transient_count,
        }


@dataclass(frozen=True, slots=True)
class _Timeline:
    time_base: Fraction
    time_base_text: str
    start_pts: int | None
    start_timestamp_us: int
    start_source: str

    def timestamp_us(self, sample_index: int, sample_rate: int) -> int:
        return self.start_timestamp_us + round(Fraction(sample_index, sample_rate) * 1_000_000)


@dataclass(frozen=True, slots=True)
class _EnergyFrame:
    frame_index: int
    sample_index: int
    end_sample_index: int
    timestamp_us: int
    rms: float
    normalized_rms: float
    dbfs: float
    peak: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "sample_index": self.sample_index,
            "end_sample_index": self.end_sample_index,
            "timestamp_us": self.timestamp_us,
            "rms": self.rms,
            "normalized_rms": self.normalized_rms,
            "dbfs": self.dbfs,
            "peak": self.peak,
        }


@dataclass(frozen=True, slots=True)
class _FluxFrame:
    frame_index: int
    sample_index: int
    timestamp_us: int
    spectral_flux: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "sample_index": self.sample_index,
            "timestamp_us": self.timestamp_us,
            "spectral_flux": self.spectral_flux,
        }


@dataclass(frozen=True, slots=True)
class _Transient:
    frame_index: int
    sample_index: int
    timestamp_us: int
    spectral_flux: float
    rms: float
    score: float


@dataclass(frozen=True, slots=True)
class _SilenceRegion:
    start_sample_index: int
    end_sample_index: int
    start_timestamp_us: int
    end_timestamp_us: int
    minimum_dbfs: float
    mean_dbfs: float


@dataclass(frozen=True, slots=True)
class _TempoDraft:
    bpm: float
    score: float
    autocorrelation_score: float
    interval_support: float
    lag_frames: int
    period_samples: int


class AudioAnalysisService:
    """执行音频流探测、PCM 解码和确定性 DSP 证据落盘。"""

    def __init__(self, config: AudioAnalysisConfig | None = None) -> None:
        self.config = config or AudioAnalysisConfig()

    def analyze(self, source: Path, output_dir: Path) -> AudioAnalysisResult:
        source_path = self._resolve_source(source)
        output_root = self._prepare_output(output_dir)
        source_sha256 = _sha256(source_path)
        media_payload, audio_stream = self._probe_media(source_path)
        stream_index = _required_stream_index(audio_stream)
        timeline = _audio_timeline(audio_stream)
        pcm_bytes, samples = self._decode_audio(source_path, stream_index)

        energies, fluxes = self._analyze_frames(samples, timeline)
        transients, transient_threshold = self._detect_transients(energies, fluxes)
        silences = self._detect_silence(energies, len(samples), timeline)
        tempos = self._tempo_candidates(fluxes, transients)
        beat_grid = self._beat_grid(fluxes, tempos, len(samples), timeline)
        sections = self._section_candidates(energies, fluxes, silences, len(samples), timeline)

        duration_us = round(Fraction(len(samples), self.config.sample_rate) * 1_000_000)
        media_probe_path = output_root / "media_probe.json"
        audio_signals_path = output_root / "audio_signals.json"
        energy_curve_path = output_root / "energy_curve.json"
        spectral_flux_path = output_root / "spectral_flux.json"
        transient_candidates_path = output_root / "transient_candidates.json"
        silence_regions_path = output_root / "silence_regions.json"
        tempo_candidates_path = output_root / "tempo_candidates.json"
        beat_grid_path = output_root / "beat_grid.json"
        section_candidates_path = output_root / "section_candidates.json"
        evidence_manifest_path = output_root / "evidence_manifest.json"

        common = {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "source_sha256": source_sha256,
            "audio_scope": AUDIO_SCOPE,
        }
        _write_json_atomic(
            media_probe_path,
            {
                **common,
                "source_path": str(source_path),
                "stream_selection": {
                    "criterion": "codec_type=audio",
                    "selected_stream_index": stream_index,
                },
                "selected_audio_stream": audio_stream,
                "timeline": {
                    "time_base": timeline.time_base_text,
                    "start_pts": timeline.start_pts,
                    "start_timestamp_us": timeline.start_timestamp_us,
                    "start_source": timeline.start_source,
                },
                "decode": {
                    "sample_format": "f32le",
                    "channels": 1,
                    "sample_rate": self.config.sample_rate,
                    "sample_count": len(samples),
                    "duration_us": duration_us,
                    "pcm_sha256": hashlib.sha256(pcm_bytes).hexdigest(),
                },
                "ffprobe": media_payload,
            },
        )
        _write_json_atomic(
            energy_curve_path,
            {
                **common,
                "sample_rate": self.config.sample_rate,
                "frame_size": self.config.frame_size,
                "hop_size": self.config.hop_size,
                "frames": [frame.to_dict() for frame in energies],
            },
        )
        _write_json_atomic(
            spectral_flux_path,
            {
                **common,
                "sample_rate": self.config.sample_rate,
                "frame_size": self.config.frame_size,
                "hop_size": self.config.hop_size,
                "frames": [frame.to_dict() for frame in fluxes],
            },
        )
        _write_json_atomic(
            transient_candidates_path,
            {
                **common,
                "classification": "transient_candidates",
                "candidate_semantics": "候选瞬态，需结合听觉与画面证据复核",
                "threshold": transient_threshold,
                "candidates": [
                    {
                        "candidate_id": f"transient_{index:05d}",
                        "status": "candidate",
                        "frame_index": transient.frame_index,
                        "sample_index": transient.sample_index,
                        "timestamp_us": transient.timestamp_us,
                        "spectral_flux": transient.spectral_flux,
                        "rms": transient.rms,
                        "score": transient.score,
                    }
                    for index, transient in enumerate(transients, start=1)
                ],
            },
        )
        _write_json_atomic(
            silence_regions_path,
            {
                **common,
                "threshold_dbfs": self.config.silence_threshold_dbfs,
                "minimum_duration_ms": self.config.minimum_silence_duration_ms,
                "regions": [
                    {
                        "region_id": f"silence_{index:04d}",
                        "start_sample_index": region.start_sample_index,
                        "end_sample_index": region.end_sample_index,
                        "start_timestamp_us": region.start_timestamp_us,
                        "end_timestamp_us": region.end_timestamp_us,
                        "duration_us": (region.end_timestamp_us - region.start_timestamp_us),
                        "minimum_dbfs": region.minimum_dbfs,
                        "mean_dbfs": region.mean_dbfs,
                    }
                    for index, region in enumerate(silences, start=1)
                ],
            },
        )
        tempo_payload = self._tempo_payload(common, tempos)
        _write_json_atomic(tempo_candidates_path, tempo_payload)
        _write_json_atomic(beat_grid_path, {**common, **beat_grid})
        _write_json_atomic(section_candidates_path, {**common, **sections})
        _write_json_atomic(
            audio_signals_path,
            {
                **common,
                "sample_rate": self.config.sample_rate,
                "sample_count": len(samples),
                "duration_us": duration_us,
                "timeline_start_timestamp_us": timeline.start_timestamp_us,
                "statistics": {
                    "peak": _round_signal(float(np.max(np.abs(samples)))),
                    "overall_rms": _round_signal(
                        float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
                    ),
                    "energy_frame_count": len(energies),
                    "transient_candidate_count": len(transients),
                    "silence_region_count": len(silences),
                    "tempo_candidate_count": len(tempos),
                    "section_candidate_count": len(sections["sections"]),
                },
                "artifacts": {
                    "energy_curve": energy_curve_path.name,
                    "spectral_flux": spectral_flux_path.name,
                    "transient_candidates": transient_candidates_path.name,
                    "silence_regions": silence_regions_path.name,
                    "tempo_candidates": tempo_candidates_path.name,
                    "beat_grid": beat_grid_path.name,
                    "section_candidates": section_candidates_path.name,
                },
            },
        )

        artifact_paths = [
            (media_probe_path, "media_probe"),
            (audio_signals_path, "audio_signals"),
            (energy_curve_path, "energy_curve"),
            (spectral_flux_path, "spectral_flux"),
            (transient_candidates_path, "transient_candidates"),
            (silence_regions_path, "silence_regions"),
            (tempo_candidates_path, "tempo_candidates"),
            (beat_grid_path, "beat_grid"),
            (section_candidates_path, "section_candidates"),
        ]
        _write_json_atomic(
            evidence_manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "algorithm_version": ALGORITHM_VERSION,
                "audio_scope": AUDIO_SCOPE,
                "source": {
                    "path": str(source_path),
                    "sha256": source_sha256,
                    "size_bytes": source_path.stat().st_size,
                },
                "selected_audio_stream_index": stream_index,
                "analysis_config": self._config_dict(),
                "artifacts": [
                    _artifact_entry(output_root, path, kind) for path, kind in artifact_paths
                ],
            },
        )

        return AudioAnalysisResult(
            source_path=source_path,
            output_dir=output_root,
            evidence_manifest_path=evidence_manifest_path,
            media_probe_path=media_probe_path,
            audio_signals_path=audio_signals_path,
            energy_curve_path=energy_curve_path,
            spectral_flux_path=spectral_flux_path,
            transient_candidates_path=transient_candidates_path,
            silence_regions_path=silence_regions_path,
            tempo_candidates_path=tempo_candidates_path,
            beat_grid_path=beat_grid_path,
            section_candidates_path=section_candidates_path,
            source_sha256=source_sha256,
            manifest_sha256=_sha256(evidence_manifest_path),
            algorithm_version=ALGORITHM_VERSION,
            audio_scope=AUDIO_SCOPE,
            sample_rate=self.config.sample_rate,
            sample_count=len(samples),
            duration_us=duration_us,
            transient_count=len(transients),
        )

    @staticmethod
    def _resolve_source(source: Path) -> Path:
        try:
            resolved = Path(source).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise AudioAnalysisError(
                "source_not_found",
                "音频来源文件不存在",
                details={"source": str(source)},
            ) from error
        if not resolved.is_file():
            raise AudioAnalysisError(
                "source_not_found",
                "音频来源文件不存在",
                details={"source": str(source)},
            )
        return resolved

    @staticmethod
    def _prepare_output(output_dir: Path) -> Path:
        try:
            path = Path(output_dir)
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise OSError("输出路径不是目录")
            return path.resolve()
        except OSError as error:
            raise AudioAnalysisError(
                "output_unavailable", f"音频分析输出目录写入失败：{error}"
            ) from error

    def _probe_media(self, source: Path) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = self._run_json(
            [
                self.config.ffprobe_binary,
                "-v",
                "error",
                "-show_entries",
                (
                    "format=format_name,duration,size,bit_rate,start_time:"
                    "stream=index,codec_type,codec_name,profile,sample_fmt,"
                    "sample_rate,channels,channel_layout,time_base,start_pts,"
                    "start_time,duration_ts,duration,bit_rate"
                ),
                "-of",
                "json",
                str(source),
            ],
            failure_code="media_probe_failed",
            failure_message="ffprobe 未能读取音频媒体信息",
        )
        streams = payload.get("streams")
        if not isinstance(streams, list):
            raise AudioAnalysisError("invalid_media", "ffprobe 未返回媒体流")
        stream = next(
            (
                item
                for item in streams
                if isinstance(item, dict) and item.get("codec_type") == "audio"
            ),
            None,
        )
        if not isinstance(stream, dict):
            raise AudioAnalysisError("audio_stream_not_found", "输入文件不包含音频流")
        return payload, stream

    def _decode_audio(
        self, source: Path, stream_index: int
    ) -> tuple[bytes, npt.NDArray[np.float32]]:
        argv = [
            self.config.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(source),
            "-map",
            f"0:{stream_index}",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            str(self.config.sample_rate),
            "-c:a",
            "pcm_f32le",
            "-f",
            "f32le",
            "pipe:1",
        ]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.config.subprocess_timeout_seconds,
                check=False,
            )
        except FileNotFoundError as error:
            raise AudioAnalysisError(
                "dependency_missing", f"未找到 {self.config.ffmpeg_binary}"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise AudioAnalysisError("audio_decode_timeout", "FFmpeg 音频解码超时") from error
        except OSError as error:
            raise AudioAnalysisError(
                "audio_decode_failed", f"FFmpeg 音频解码失败：{error}"
            ) from error
        if completed.returncode != 0:
            raise AudioAnalysisError(
                "audio_decode_failed",
                "FFmpeg 音频解码失败",
                details={
                    "returncode": completed.returncode,
                    "stderr": completed.stderr.decode("utf-8", errors="replace")[-2_000:],
                },
            )
        if not completed.stdout or len(completed.stdout) % 4:
            raise AudioAnalysisError("audio_decode_empty", "音频流没有可分析采样")
        samples = np.frombuffer(completed.stdout, dtype="<f4").astype(np.float32, copy=True)
        if samples.size == 0 or not bool(np.all(np.isfinite(samples))):
            raise AudioAnalysisError("audio_decode_invalid", "解码音频采样无效")
        return completed.stdout, samples

    def _analyze_frames(
        self,
        samples: npt.NDArray[np.float32],
        timeline: _Timeline,
    ) -> tuple[list[_EnergyFrame], list[_FluxFrame]]:
        raw_energies: list[tuple[int, int, float, float, float]] = []
        flux_values: list[float] = []
        window = np.hanning(self.config.frame_size).astype(np.float64)
        previous_spectrum = np.zeros(self.config.frame_size // 2 + 1, dtype=np.float64)
        for start in range(0, len(samples), self.config.hop_size):
            end = min(len(samples), start + self.config.frame_size)
            frame = samples[start:end].astype(np.float64, copy=False)
            rms = float(np.sqrt(np.mean(np.square(frame))))
            peak = float(np.max(np.abs(frame)))
            dbfs = 20.0 * math.log10(max(rms, 1e-12))
            raw_energies.append((start, end, rms, dbfs, peak))

            padded = np.zeros(self.config.frame_size, dtype=np.float64)
            padded[: len(frame)] = frame
            spectrum = np.abs(np.fft.rfft(padded * window))
            magnitude_sum = float(np.sum(spectrum))
            if magnitude_sum > 1e-12:
                spectrum /= magnitude_sum
            positive_difference = np.maximum(spectrum - previous_spectrum, 0.0)
            flux_values.append(float(np.sum(positive_difference)))
            previous_spectrum = spectrum

        maximum_rms = max((value[2] for value in raw_energies), default=0.0)
        energies: list[_EnergyFrame] = []
        fluxes: list[_FluxFrame] = []
        for frame_index, ((start, end, rms, dbfs, peak), flux) in enumerate(
            zip(raw_energies, flux_values, strict=True)
        ):
            timestamp_us = timeline.timestamp_us(start, self.config.sample_rate)
            energies.append(
                _EnergyFrame(
                    frame_index=frame_index,
                    sample_index=start,
                    end_sample_index=end,
                    timestamp_us=timestamp_us,
                    rms=_round_signal(rms),
                    normalized_rms=_round_signal(rms / maximum_rms if maximum_rms > 1e-12 else 0.0),
                    dbfs=round(dbfs, 6),
                    peak=_round_signal(peak),
                )
            )
            fluxes.append(
                _FluxFrame(
                    frame_index=frame_index,
                    sample_index=start,
                    timestamp_us=timestamp_us,
                    spectral_flux=_round_signal(flux),
                )
            )
        return energies, fluxes

    def _detect_transients(
        self,
        energies: list[_EnergyFrame],
        fluxes: list[_FluxFrame],
    ) -> tuple[list[_Transient], dict[str, float]]:
        flux_values = [frame.spectral_flux for frame in fluxes]
        median = statistics.median(flux_values)
        mad = statistics.median(abs(value - median) for value in flux_values)
        threshold = max(
            self.config.transient_flux_floor,
            median + self.config.transient_mad_multiplier * mad,
        )
        maximum_flux = max(flux_values, default=0.0)
        raw: list[_Transient] = []
        for index, flux in enumerate(flux_values):
            previous = flux_values[index - 1] if index else -1.0
            following = flux_values[index + 1] if index + 1 < len(flux_values) else -1.0
            if flux + 1e-12 < threshold or flux < previous or flux < following:
                continue
            score = flux / maximum_flux if maximum_flux > 1e-12 else 0.0
            raw.append(
                _Transient(
                    frame_index=index,
                    sample_index=fluxes[index].sample_index,
                    timestamp_us=fluxes[index].timestamp_us,
                    spectral_flux=flux,
                    rms=energies[index].rms,
                    score=_round_signal(score),
                )
            )

        separation_samples = round(
            self.config.transient_minimum_separation_ms * self.config.sample_rate / 1_000
        )
        selected: list[_Transient] = []
        for candidate in raw:
            if selected and candidate.sample_index - selected[-1].sample_index < separation_samples:
                if candidate.score > selected[-1].score:
                    selected[-1] = candidate
                continue
            selected.append(candidate)
        return selected, {
            "median": round(median, 6),
            "median_absolute_deviation": round(mad, 6),
            "mad_multiplier": self.config.transient_mad_multiplier,
            "minimum_flux": self.config.transient_flux_floor,
            "effective_flux": round(threshold, 6),
        }

    def _detect_silence(
        self,
        energies: list[_EnergyFrame],
        sample_count: int,
        timeline: _Timeline,
    ) -> list[_SilenceRegion]:
        minimum_samples = round(
            self.config.minimum_silence_duration_ms * self.config.sample_rate / 1_000
        )
        regions: list[_SilenceRegion] = []
        index = 0
        while index < len(energies):
            if energies[index].dbfs > self.config.silence_threshold_dbfs:
                index += 1
                continue
            start_index = index
            while (
                index + 1 < len(energies)
                and energies[index + 1].dbfs <= self.config.silence_threshold_dbfs
            ):
                index += 1
            end_index = index
            start_sample = energies[start_index].sample_index
            end_sample = min(sample_count, energies[end_index].end_sample_index)
            if end_sample - start_sample >= minimum_samples:
                dbfs_values = [energies[item].dbfs for item in range(start_index, end_index + 1)]
                regions.append(
                    _SilenceRegion(
                        start_sample_index=start_sample,
                        end_sample_index=end_sample,
                        start_timestamp_us=timeline.timestamp_us(
                            start_sample, self.config.sample_rate
                        ),
                        end_timestamp_us=timeline.timestamp_us(end_sample, self.config.sample_rate),
                        minimum_dbfs=round(min(dbfs_values), 6),
                        mean_dbfs=round(statistics.fmean(dbfs_values), 6),
                    )
                )
            index += 1
        return regions

    def _tempo_candidates(
        self,
        fluxes: list[_FluxFrame],
        transients: list[_Transient],
    ) -> list[_TempoDraft]:
        if len(transients) < self.config.minimum_tempo_transients:
            return []
        envelope = np.asarray([frame.spectral_flux for frame in fluxes], dtype=np.float64)
        median = float(np.median(envelope))
        envelope = np.maximum(envelope - median, 0.0)
        if float(np.max(envelope)) <= 1e-12:
            return []

        minimum_lag = max(
            1,
            math.floor(
                60
                * self.config.sample_rate
                / (self.config.maximum_tempo_bpm * self.config.hop_size)
            ),
        )
        maximum_lag = min(
            len(envelope) - 1,
            math.ceil(
                60
                * self.config.sample_rate
                / (self.config.minimum_tempo_bpm * self.config.hop_size)
            ),
        )
        if maximum_lag < minimum_lag:
            return []

        correlations: dict[int, float] = {}
        for lag in range(minimum_lag, maximum_lag + 1):
            left = envelope[:-lag]
            right = envelope[lag:]
            denominator = math.sqrt(float(np.dot(left, left)) * float(np.dot(right, right)))
            correlations[lag] = (
                float(np.dot(left, right)) / denominator if denominator > 1e-12 else 0.0
            )
        peak_correlation = max(correlations.values(), default=0.0)
        if peak_correlation <= 1e-12:
            return []

        interval_support = self._transient_interval_support(transients)
        drafts: list[_TempoDraft] = []
        for lag, correlation in correlations.items():
            previous = correlations.get(lag - 1, -1.0)
            following = correlations.get(lag + 1, -1.0)
            if correlation < previous or correlation < following:
                continue
            bpm = 60 * self.config.sample_rate / (lag * self.config.hop_size)
            support = max(
                (
                    score
                    for candidate_bpm, score in interval_support.items()
                    if abs(candidate_bpm - bpm) <= 2.0
                ),
                default=0.0,
            )
            autocorrelation_score = correlation / peak_correlation
            combined = 0.7 * autocorrelation_score + 0.3 * support
            drafts.append(
                _TempoDraft(
                    bpm=round(bpm, 3),
                    score=_round_signal(combined),
                    autocorrelation_score=_round_signal(autocorrelation_score),
                    interval_support=_round_signal(support),
                    lag_frames=lag,
                    period_samples=lag * self.config.hop_size,
                )
            )

        for bpm, support in interval_support.items():
            if any(abs(draft.bpm - bpm) <= 2.0 for draft in drafts):
                continue
            lag = max(
                1,
                round(60 * self.config.sample_rate / (bpm * self.config.hop_size)),
            )
            drafts.append(
                _TempoDraft(
                    bpm=round(bpm, 3),
                    score=_round_signal(0.3 * support),
                    autocorrelation_score=0.0,
                    interval_support=_round_signal(support),
                    lag_frames=lag,
                    period_samples=lag * self.config.hop_size,
                )
            )

        drafts.sort(key=lambda item: (-item.score, item.bpm))
        selected: list[_TempoDraft] = []
        for draft in drafts:
            if any(abs(existing.bpm - draft.bpm) <= 2.0 for existing in selected):
                continue
            selected.append(draft)
            if len(selected) == self.config.maximum_tempo_candidates:
                break
        return selected

    def _transient_interval_support(self, transients: list[_Transient]) -> dict[float, float]:
        counts: dict[float, int] = {}
        for left, right in zip(transients, transients[1:]):
            interval = right.sample_index - left.sample_index
            if interval <= 0:
                continue
            bpm = 60 * self.config.sample_rate / interval
            while bpm < self.config.minimum_tempo_bpm:
                bpm *= 2
            while bpm > self.config.maximum_tempo_bpm:
                bpm /= 2
            bucket = round(bpm * 2) / 2
            counts[bucket] = counts.get(bucket, 0) + 1
        maximum = max(counts.values(), default=0)
        if maximum == 0:
            return {}
        return {bpm: count / maximum for bpm, count in counts.items()}

    def _beat_grid(
        self,
        fluxes: list[_FluxFrame],
        tempos: list[_TempoDraft],
        sample_count: int,
        timeline: _Timeline,
    ) -> dict[str, Any]:
        if not tempos:
            return {
                "status": "no_tempo_candidate",
                "candidate_semantics": "节拍网格仅由 tempo 候选推导",
                "tempo_candidate_id": None,
                "beats": [],
            }
        tempo = tempos[0]
        envelope = [frame.spectral_flux for frame in fluxes]
        lag = tempo.lag_frames
        phase = max(
            range(min(lag, len(envelope))),
            key=lambda value: (sum(envelope[value::lag]), -value),
        )
        first_sample = phase * self.config.hop_size
        beats: list[dict[str, Any]] = []
        beat_index = 0
        sample_index = first_sample
        while sample_index < sample_count:
            beats.append(
                {
                    "beat_index": beat_index,
                    "status": "provisional_candidate_grid",
                    "sample_index": sample_index,
                    "timestamp_us": timeline.timestamp_us(sample_index, self.config.sample_rate),
                    "period_samples": tempo.period_samples,
                    "tempo_candidate_id": "tempo_001",
                }
            )
            beat_index += 1
            sample_index += tempo.period_samples
        return {
            "status": "derived_from_tempo_candidate",
            "candidate_semantics": "临时节拍网格，需结合听觉与段落证据复核",
            "tempo_candidate_id": "tempo_001",
            "bpm": tempo.bpm,
            "phase_sample_index": first_sample,
            "beats": beats,
        }

    def _section_candidates(
        self,
        energies: list[_EnergyFrame],
        fluxes: list[_FluxFrame],
        silences: list[_SilenceRegion],
        sample_count: int,
        timeline: _Timeline,
    ) -> dict[str, Any]:
        minimum_samples = round(
            self.config.minimum_section_duration_ms * self.config.sample_rate / 1_000
        )
        bucket_frames = max(1, round(minimum_samples / self.config.hop_size))
        buckets: list[tuple[int, float, float]] = []
        for start in range(0, len(energies), bucket_frames):
            end = min(len(energies), start + bucket_frames)
            sample_index = energies[start].sample_index
            mean_energy = statistics.fmean(frame.normalized_rms for frame in energies[start:end])
            mean_flux = statistics.fmean(frame.spectral_flux for frame in fluxes[start:end])
            buckets.append((sample_index, mean_energy, mean_flux))

        change_scores = [
            abs(right[1] - left[1]) + 0.5 * abs(right[2] - left[2])
            for left, right in zip(buckets, buckets[1:])
        ]
        if change_scores:
            change_median = statistics.median(change_scores)
            change_mad = statistics.median(abs(value - change_median) for value in change_scores)
            change_threshold = change_median + self.config.section_mad_multiplier * change_mad
        else:
            change_median = 0.0
            change_mad = 0.0
            change_threshold = math.inf

        boundaries: dict[int, tuple[str, float]] = {
            0: ("program_start", 1.0),
            sample_count: ("program_end", 1.0),
        }
        last_boundary = 0
        for bucket_index, score in enumerate(change_scores, start=1):
            sample_index = buckets[bucket_index][0]
            if (
                score + 1e-12 >= change_threshold
                and score > 0
                and sample_index - last_boundary >= minimum_samples
                and sample_count - sample_index >= minimum_samples
            ):
                boundaries[sample_index] = ("energy_or_flux_change", _round_signal(score))
                last_boundary = sample_index
        for silence in silences:
            if silence.end_sample_index - silence.start_sample_index < minimum_samples:
                continue
            for sample_index, reason in (
                (silence.start_sample_index, "long_silence_start"),
                (silence.end_sample_index, "long_silence_end"),
            ):
                if minimum_samples <= sample_index <= sample_count - minimum_samples:
                    boundaries.setdefault(sample_index, (reason, 1.0))

        ordered = sorted(boundaries)
        boundary_payload = [
            {
                "candidate_id": f"section_boundary_{index:04d}",
                "status": "candidate",
                "sample_index": sample_index,
                "timestamp_us": timeline.timestamp_us(sample_index, self.config.sample_rate),
                "reason": boundaries[sample_index][0],
                "score": boundaries[sample_index][1],
            }
            for index, sample_index in enumerate(ordered, start=1)
        ]
        sections: list[dict[str, Any]] = []
        for index, (start_sample, end_sample) in enumerate(zip(ordered, ordered[1:]), start=1):
            contained = [
                frame for frame in energies if start_sample <= frame.sample_index < end_sample
            ]
            sections.append(
                {
                    "section_id": f"section_{index:04d}",
                    "status": "candidate",
                    "start_sample_index": start_sample,
                    "end_sample_index": end_sample,
                    "start_timestamp_us": timeline.timestamp_us(
                        start_sample, self.config.sample_rate
                    ),
                    "end_timestamp_us": timeline.timestamp_us(end_sample, self.config.sample_rate),
                    "duration_us": round(
                        Fraction(end_sample - start_sample, self.config.sample_rate) * 1_000_000
                    ),
                    "mean_rms": round(statistics.fmean(frame.rms for frame in contained), 6),
                    "mean_normalized_energy": round(
                        statistics.fmean(frame.normalized_rms for frame in contained),
                        6,
                    ),
                }
            )
        return {
            "classification": "section_candidates",
            "candidate_semantics": "段落边界与区间均为候选，需结合音乐语义复核",
            "threshold": {
                "median_change": round(change_median, 6),
                "median_absolute_deviation": round(change_mad, 6),
                "mad_multiplier": self.config.section_mad_multiplier,
                "effective_change": (
                    None if math.isinf(change_threshold) else round(change_threshold, 6)
                ),
            },
            "boundaries": boundary_payload,
            "sections": sections,
        }

    @staticmethod
    def _tempo_payload(common: dict[str, Any], tempos: list[_TempoDraft]) -> dict[str, Any]:
        return {
            **common,
            "classification": "tempo_candidates",
            "candidate_semantics": "BPM 为算法候选，不代表已确认音乐速度",
            "candidates": [
                {
                    "candidate_id": f"tempo_{index:03d}",
                    "status": "candidate",
                    "bpm": tempo.bpm,
                    "score": tempo.score,
                    "method": "spectral_flux_autocorrelation_with_transient_intervals",
                    "autocorrelation_score": tempo.autocorrelation_score,
                    "transient_interval_support": tempo.interval_support,
                    "lag_frames": tempo.lag_frames,
                    "period_samples": tempo.period_samples,
                }
                for index, tempo in enumerate(tempos, start=1)
            ],
        }

    def _run_json(
        self,
        argv: list[str],
        *,
        failure_code: str,
        failure_message: str,
    ) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.subprocess_timeout_seconds,
                check=False,
            )
        except FileNotFoundError as error:
            raise AudioAnalysisError("dependency_missing", f"未找到 {argv[0]}") from error
        except subprocess.TimeoutExpired as error:
            raise AudioAnalysisError(
                f"{failure_code}_timeout", f"{failure_message}：执行超时"
            ) from error
        except OSError as error:
            raise AudioAnalysisError(failure_code, f"{failure_message}：{error}") from error
        if completed.returncode != 0:
            raise AudioAnalysisError(
                failure_code,
                failure_message,
                details={
                    "returncode": completed.returncode,
                    "stderr": completed.stderr[-2_000:],
                },
            )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise AudioAnalysisError(failure_code, f"{failure_message}：返回 JSON 无效") from error
        if not isinstance(value, dict):
            raise AudioAnalysisError(failure_code, f"{failure_message}：返回结构无效")
        return value

    def _config_dict(self) -> dict[str, Any]:
        return {
            "sample_rate": self.config.sample_rate,
            "frame_size": self.config.frame_size,
            "hop_size": self.config.hop_size,
            "silence_threshold_dbfs": self.config.silence_threshold_dbfs,
            "minimum_silence_duration_ms": self.config.minimum_silence_duration_ms,
            "transient_flux_floor": self.config.transient_flux_floor,
            "transient_mad_multiplier": self.config.transient_mad_multiplier,
            "transient_minimum_separation_ms": (self.config.transient_minimum_separation_ms),
            "minimum_tempo_bpm": self.config.minimum_tempo_bpm,
            "maximum_tempo_bpm": self.config.maximum_tempo_bpm,
            "maximum_tempo_candidates": self.config.maximum_tempo_candidates,
            "minimum_tempo_transients": self.config.minimum_tempo_transients,
            "minimum_section_duration_ms": self.config.minimum_section_duration_ms,
            "section_mad_multiplier": self.config.section_mad_multiplier,
        }


def _required_stream_index(stream: dict[str, Any]) -> int:
    value = stream.get("index")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AudioAnalysisError("invalid_media", "音频流缺少有效 index")
    try:
        index = int(value)
    except ValueError as error:
        raise AudioAnalysisError("invalid_media", "音频流 index 无效") from error
    if index < 0:
        raise AudioAnalysisError("invalid_media", "音频流 index 无效")
    return index


def _audio_timeline(stream: dict[str, Any]) -> _Timeline:
    time_base_value = stream.get("time_base")
    if not isinstance(time_base_value, str):
        raise AudioAnalysisError("invalid_media", "音频流缺少 time_base")
    try:
        time_base = Fraction(time_base_value)
    except (ValueError, ZeroDivisionError) as error:
        raise AudioAnalysisError("invalid_media", "音频流 time_base 无效") from error
    if time_base <= 0:
        raise AudioAnalysisError("invalid_media", "音频流 time_base 无效")

    start_pts = _optional_int(stream.get("start_pts"))
    if start_pts is not None:
        return _Timeline(
            time_base=time_base,
            time_base_text=str(time_base),
            start_pts=start_pts,
            start_timestamp_us=round(Fraction(start_pts) * time_base * 1_000_000),
            start_source="start_pts",
        )
    start_time = stream.get("start_time")
    if isinstance(start_time, (str, int, float)) and not isinstance(start_time, bool):
        try:
            start_timestamp_us = round(Fraction(str(start_time)) * 1_000_000)
        except (ValueError, ZeroDivisionError) as error:
            raise AudioAnalysisError("invalid_media", "音频流 start_time 无效") from error
        return _Timeline(
            time_base=time_base,
            time_base_text=str(time_base),
            start_pts=None,
            start_timestamp_us=start_timestamp_us,
            start_source="start_time",
        )
    return _Timeline(
        time_base=time_base,
        time_base_text=str(time_base),
        start_pts=None,
        start_timestamp_us=0,
        start_source="implicit_zero",
    )


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _round_signal(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 6)


def _artifact_entry(output_root: Path, path: Path, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": path.relative_to(output_root).as_posix(),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
        "algorithm_version": ALGORITHM_VERSION,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as error:
        raise AudioAnalysisError("input_unavailable", f"文件读取失败：{path}：{error}") from error
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as error:
        raise AudioAnalysisError("output_unavailable", f"音频分析证据写入失败：{error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
