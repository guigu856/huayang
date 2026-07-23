"""从真实视频帧与时间戳生成可复核的视觉分析证据。"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import IO, Any

ALGORITHM_VERSION = "video-analysis-visual-v1.1.0"
SCHEMA_VERSION = "1.0"


class VideoAnalysisError(RuntimeError):
    """视频分析组件对外暴露的稳定错误。"""

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
class VideoAnalysisConfig:
    """确定性视觉分析使用的本机工具与算法参数。"""

    ffprobe_binary: str = "ffprobe"
    ffmpeg_binary: str = "ffmpeg"
    analysis_width: int = 96
    evidence_width: int = 480
    histogram_bins: int = 16
    minimum_boundary_score: float = 0.18
    boundary_mad_multiplier: float = 6.0
    flash_white_ratio: float = 0.85
    flash_brightness_delta: float = 0.25
    flash_max_duration_us: int = 300_000
    fast_cut_max_gap_us: int = 250_000
    fast_cut_min_candidates: int = 3
    fast_evidence_max_interval_us: int = 100_000
    subprocess_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not self.ffprobe_binary or not self.ffmpeg_binary:
            raise ValueError("ffprobe_binary 与 ffmpeg_binary 不能为空")
        if self.analysis_width <= 0 or self.evidence_width <= 0:
            raise ValueError("analysis_width 与 evidence_width 必须大于 0")
        if not 2 <= self.histogram_bins <= 256:
            raise ValueError("histogram_bins 必须位于 2 到 256")
        if not 0 < self.minimum_boundary_score <= 1:
            raise ValueError("minimum_boundary_score 必须位于 0 到 1")
        if self.boundary_mad_multiplier < 0:
            raise ValueError("boundary_mad_multiplier 必须大于等于 0")
        if not 0 < self.flash_white_ratio <= 1:
            raise ValueError("flash_white_ratio 必须位于 0 到 1")
        if not 0 < self.flash_brightness_delta <= 1:
            raise ValueError("flash_brightness_delta 必须位于 0 到 1")
        if self.flash_max_duration_us <= 0 or self.fast_cut_max_gap_us <= 0:
            raise ValueError("时间窗口必须大于 0")
        if self.fast_cut_min_candidates < 2:
            raise ValueError("fast_cut_min_candidates 必须大于等于 2")
        if not 0 < self.fast_evidence_max_interval_us <= 100_000:
            raise ValueError("快切证据最大间隔必须位于 1 到 100000 微秒")
        if self.subprocess_timeout_seconds <= 0:
            raise ValueError("subprocess_timeout_seconds 必须大于 0")


@dataclass(frozen=True, slots=True)
class VideoAnalysisResult:
    """一次成功视频分析的结构化产物索引。"""

    source_path: Path
    output_dir: Path
    evidence_manifest_path: Path
    media_probe_path: Path
    frame_index_path: Path
    visual_signals_path: Path
    boundary_candidates_path: Path
    contact_sheet_path: Path
    candidate_frame_paths: tuple[Path, ...]
    source_sha256: str
    manifest_sha256: str
    algorithm_version: str
    frame_count: int
    candidate_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": str(self.source_path),
            "output_dir": str(self.output_dir),
            "evidence_manifest_path": str(self.evidence_manifest_path),
            "media_probe_path": str(self.media_probe_path),
            "frame_index_path": str(self.frame_index_path),
            "visual_signals_path": str(self.visual_signals_path),
            "boundary_candidates_path": str(self.boundary_candidates_path),
            "contact_sheet_path": str(self.contact_sheet_path),
            "candidate_frame_paths": [str(path) for path in self.candidate_frame_paths],
            "source_sha256": self.source_sha256,
            "manifest_sha256": self.manifest_sha256,
            "algorithm_version": self.algorithm_version,
            "frame_count": self.frame_count,
            "candidate_count": self.candidate_count,
        }


@dataclass(frozen=True, slots=True)
class _ProbedFrame:
    frame_index: int
    pts: int
    timestamp_us: int
    time_base: str
    pts_source: str
    key_frame: bool
    pict_type: str | None


@dataclass(frozen=True, slots=True)
class _FrameRecord:
    probe: _ProbedFrame
    brightness: float
    difference: float
    histogram_difference: float
    scene_score: float
    white_ratio: float
    histogram: dict[str, list[float]]

    def index_dict(self, source_sha256: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "source_sha256": source_sha256,
            "frame_index": self.probe.frame_index,
            "pts": self.probe.pts,
            "pts_source": self.probe.pts_source,
            "time_base": self.probe.time_base,
            "timestamp_us": self.probe.timestamp_us,
            "key_frame": self.probe.key_frame,
            "pict_type": self.probe.pict_type,
            "brightness": self.brightness,
            "difference": self.difference,
            "histogram_difference": self.histogram_difference,
            "scene_score": self.scene_score,
            "white_ratio": self.white_ratio,
            "histogram": self.histogram,
        }

    def signal_dict(self) -> dict[str, Any]:
        return {
            "frame_index": self.probe.frame_index,
            "timestamp_us": self.probe.timestamp_us,
            "brightness": self.brightness,
            "difference": self.difference,
            "histogram_difference": self.histogram_difference,
            "scene_score": self.scene_score,
            "white_ratio": self.white_ratio,
            "histogram": self.histogram,
        }


@dataclass(frozen=True, slots=True)
class _Boundary:
    kind: str
    frame_index: int
    timestamp_us: int
    pts: int
    time_base: str
    score: float
    evidence_indexes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _FastCutRegion:
    start_timestamp_us: int
    end_timestamp_us: int
    boundary_indexes: tuple[int, ...]
    evidence_indexes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _EvidenceImage:
    frame_index: int
    timestamp_us: int
    path: Path
    relative_path: str
    sha256: str
    size_bytes: int

    def reference_dict(self) -> dict[str, Any]:
        return {
            "frame_index": self.frame_index,
            "timestamp_us": self.timestamp_us,
            "path": self.relative_path,
            "sha256": self.sha256,
            "algorithm_version": ALGORITHM_VERSION,
        }


class VideoAnalysisService:
    """执行媒体探测、逐帧视觉信号计算和候选证据落盘。"""

    def __init__(self, config: VideoAnalysisConfig | None = None) -> None:
        self.config = config or VideoAnalysisConfig()

    def analyze(self, source: Path, output_dir: Path) -> VideoAnalysisResult:
        source_path = self._resolve_source(source)
        output_root = self._prepare_output(output_dir)
        source_sha256 = _sha256(source_path)

        media_payload, video_stream = self._probe_media(source_path)
        time_base = self._parse_time_base(video_stream)
        probed_frames = self._probe_frames(source_path, time_base)
        source_width = _required_positive_int(video_stream, "width")
        source_height = _required_positive_int(video_stream, "height")
        decoded_width, decoded_height = self._decoded_dimensions(source_width, source_height)
        evidence_width = min(source_width, self.config.evidence_width)
        evidence_height = max(1, round(source_height * evidence_width / source_width))
        frames = self._decode_frames(
            source_path,
            probed_frames,
            decoded_width,
            decoded_height,
        )
        threshold, threshold_data = self._adaptive_threshold(frames)
        boundaries = self._detect_boundaries(frames, threshold)
        fast_regions = self._detect_fast_cut_regions(frames, boundaries)

        evidence_indexes = {
            frame_index for boundary in boundaries for frame_index in boundary.evidence_indexes
        }
        evidence_indexes.update(
            frame_index for region in fast_regions for frame_index in region.evidence_indexes
        )
        images = self._extract_evidence_images(
            source_path,
            output_root,
            frames,
            evidence_indexes,
            evidence_width,
            evidence_height,
        )
        contact_sheet_path = self._extract_contact_sheet(
            source_path,
            output_root,
            frames,
        )

        media_probe_path = output_root / "media_probe.json"
        frame_index_path = output_root / "frame_index.jsonl"
        visual_signals_path = output_root / "visual_signals.json"
        boundary_candidates_path = output_root / "boundary_candidates.json"
        evidence_manifest_path = output_root / "evidence_manifest.json"

        _write_json_atomic(
            media_probe_path,
            {
                "schema_version": SCHEMA_VERSION,
                "algorithm_version": ALGORITHM_VERSION,
                "source_sha256": source_sha256,
                "source_path": str(source_path),
                "selected_video_stream": video_stream,
                "ffprobe": media_payload,
                "frame_probe": {
                    "frame_count": len(probed_frames),
                    "pts_source_counts": _count_pts_sources(probed_frames),
                },
                "decode": {
                    "pixel_format": "rgb24",
                    "width": decoded_width,
                    "height": decoded_height,
                    "streaming": True,
                },
            },
        )
        _write_jsonl_atomic(
            frame_index_path,
            [frame.index_dict(source_sha256) for frame in frames],
        )
        _write_json_atomic(
            visual_signals_path,
            {
                "schema_version": SCHEMA_VERSION,
                "algorithm_version": ALGORITHM_VERSION,
                "source_sha256": source_sha256,
                "histogram_bins": self.config.histogram_bins,
                "decoded_width": decoded_width,
                "decoded_height": decoded_height,
                "frames": [frame.signal_dict() for frame in frames],
            },
        )
        image_by_index = {image.frame_index: image for image in images}
        _write_json_atomic(
            boundary_candidates_path,
            {
                "schema_version": SCHEMA_VERSION,
                "algorithm_version": ALGORITHM_VERSION,
                "source_sha256": source_sha256,
                "candidate_semantics": (
                    "候选只表示全帧变化峰值；硬切、镜内动画、局部图层变化和全帧效果"
                    "必须结合证据帧复核后再分类"
                ),
                "threshold": threshold_data,
                "candidates": [
                    self._boundary_dict(index, boundary, image_by_index)
                    for index, boundary in enumerate(boundaries, start=1)
                ],
                "dense_change_regions": [
                    self._fast_region_dict(index, region, boundaries, image_by_index)
                    for index, region in enumerate(fast_regions, start=1)
                ],
            },
        )

        artifacts = [
            _artifact_entry(output_root, media_probe_path, "media_probe"),
            _artifact_entry(output_root, frame_index_path, "frame_index"),
            _artifact_entry(output_root, visual_signals_path, "visual_signals"),
            _artifact_entry(output_root, boundary_candidates_path, "boundary_candidates"),
            _artifact_entry(output_root, contact_sheet_path, "contact_sheet"),
            *[
                {
                    "kind": "candidate_frame",
                    "path": image.relative_path,
                    "sha256": image.sha256,
                    "size_bytes": image.size_bytes,
                    "algorithm_version": ALGORITHM_VERSION,
                }
                for image in images
            ],
        ]
        _write_json_atomic(
            evidence_manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "algorithm_version": ALGORITHM_VERSION,
                "source": {
                    "path": str(source_path),
                    "sha256": source_sha256,
                    "size_bytes": source_path.stat().st_size,
                },
                "analysis_config": self._config_dict(),
                "artifacts": artifacts,
            },
        )

        return VideoAnalysisResult(
            source_path=source_path,
            output_dir=output_root,
            evidence_manifest_path=evidence_manifest_path,
            media_probe_path=media_probe_path,
            frame_index_path=frame_index_path,
            visual_signals_path=visual_signals_path,
            boundary_candidates_path=boundary_candidates_path,
            contact_sheet_path=contact_sheet_path,
            candidate_frame_paths=tuple(image.path for image in images),
            source_sha256=source_sha256,
            manifest_sha256=_sha256(evidence_manifest_path),
            algorithm_version=ALGORITHM_VERSION,
            frame_count=len(frames),
            candidate_count=len(boundaries),
        )

    @staticmethod
    def _resolve_source(source: Path) -> Path:
        try:
            resolved = Path(source).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise VideoAnalysisError(
                "source_not_found",
                "视频文件不存在",
                details={"source": str(source)},
            ) from error
        if not resolved.is_file():
            raise VideoAnalysisError(
                "source_not_found",
                "视频文件不存在",
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
            raise VideoAnalysisError(
                "output_unavailable", f"视频分析输出目录写入失败：{error}"
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
                    "stream=index,codec_type,codec_name,width,height,pix_fmt,"
                    "r_frame_rate,avg_frame_rate,time_base,start_pts,start_time,"
                    "duration_ts,duration,nb_frames"
                ),
                "-of",
                "json",
                str(source),
            ],
            failure_code="media_probe_failed",
            failure_message="ffprobe 未能读取视频媒体信息",
        )
        streams = payload.get("streams")
        if not isinstance(streams, list):
            raise VideoAnalysisError("invalid_media", "ffprobe 未返回媒体流")
        stream = next(
            (
                item
                for item in streams
                if isinstance(item, dict) and item.get("codec_type") == "video"
            ),
            None,
        )
        if not isinstance(stream, dict):
            raise VideoAnalysisError("invalid_media", "输入文件不包含视频流")
        return payload, stream

    def _probe_frames(self, source: Path, time_base: Fraction) -> list[_ProbedFrame]:
        payload = self._run_json(
            [
                self.config.ffprobe_binary,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=pts,best_effort_timestamp,key_frame,pict_type",
                "-of",
                "json",
                str(source),
            ],
            failure_code="frame_probe_failed",
            failure_message="ffprobe 未能读取视频帧时间戳",
        )
        raw_frames = payload.get("frames")
        if not isinstance(raw_frames, list) or not raw_frames:
            raise VideoAnalysisError("invalid_media", "视频没有可分析帧")

        frames: list[_ProbedFrame] = []
        time_base_text = str(time_base)
        for frame_index, value in enumerate(raw_frames):
            if not isinstance(value, dict):
                raise VideoAnalysisError("frame_probe_invalid", "视频帧信息结构无效")
            pts_value = _optional_int(value.get("pts"))
            pts_source = "pts"
            if pts_value is None:
                pts_value = _optional_int(value.get("best_effort_timestamp"))
                pts_source = "best_effort_timestamp"
            if pts_value is None:
                raise VideoAnalysisError(
                    "frame_pts_missing",
                    "ffprobe 帧缺少 PTS",
                    details={"frame_index": frame_index},
                )
            timestamp_us = round(Fraction(pts_value) * time_base * 1_000_000)
            if frames and timestamp_us < frames[-1].timestamp_us:
                raise VideoAnalysisError("frame_timestamps_invalid", "ffprobe 帧时间戳不是单调递增")
            frames.append(
                _ProbedFrame(
                    frame_index=frame_index,
                    pts=pts_value,
                    timestamp_us=timestamp_us,
                    time_base=time_base_text,
                    pts_source=pts_source,
                    key_frame=_optional_int(value.get("key_frame")) == 1,
                    pict_type=(
                        value.get("pict_type") if isinstance(value.get("pict_type"), str) else None
                    ),
                )
            )
        return frames

    @staticmethod
    def _parse_time_base(stream: dict[str, Any]) -> Fraction:
        value = stream.get("time_base")
        if not isinstance(value, str):
            raise VideoAnalysisError("invalid_media", "视频流缺少 time_base")
        try:
            time_base = Fraction(value)
        except (ValueError, ZeroDivisionError) as error:
            raise VideoAnalysisError("invalid_media", "视频流 time_base 无效") from error
        if time_base <= 0:
            raise VideoAnalysisError("invalid_media", "视频流 time_base 无效")
        return time_base

    def _decoded_dimensions(self, width: int, height: int) -> tuple[int, int]:
        decoded_width = min(width, self.config.analysis_width)
        decoded_height = max(1, round(height * decoded_width / width))
        return decoded_width, decoded_height

    def _decode_frames(
        self,
        source: Path,
        probed_frames: list[_ProbedFrame],
        width: int,
        height: int,
    ) -> list[_FrameRecord]:
        argv = [
            self.config.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            f"scale={width}:{height}:flags=area",
            "-pix_fmt",
            "rgb24",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
        try:
            process = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise VideoAnalysisError(
                "dependency_missing", f"未找到 {self.config.ffmpeg_binary}"
            ) from error
        except OSError as error:
            raise VideoAnalysisError("frame_decode_failed", f"FFmpeg 启动失败：{error}") from error
        if process.stdout is None or process.stderr is None:
            process.kill()
            raise VideoAnalysisError("frame_decode_failed", "FFmpeg 管道创建失败")

        frame_size = width * height * 3
        records: list[_FrameRecord] = []
        previous_raw: bytes | None = None
        previous_histogram: dict[str, list[float]] | None = None
        try:
            for probed_frame in probed_frames:
                raw = _read_exact(process.stdout, frame_size)
                if len(raw) != frame_size:
                    _remaining, stderr = process.communicate()
                    raise VideoAnalysisError(
                        "frame_count_mismatch",
                        "FFmpeg 解码帧数少于 ffprobe 帧数",
                        details={
                            "probed_frames": len(probed_frames),
                            "decoded_frames": len(records),
                            "stderr": stderr.decode("utf-8", errors="replace")[-2000:],
                        },
                    )
                histogram = _rgb_histogram(raw, self.config.histogram_bins)
                brightness = _brightness(raw)
                difference = (
                    0.0 if previous_raw is None else _mean_byte_difference(raw, previous_raw)
                )
                histogram_difference = (
                    0.0
                    if previous_histogram is None
                    else _histogram_difference(histogram, previous_histogram)
                )
                scene_score = difference * 0.7 + histogram_difference * 0.3
                records.append(
                    _FrameRecord(
                        probe=probed_frame,
                        brightness=_round_signal(brightness),
                        difference=_round_signal(difference),
                        histogram_difference=_round_signal(histogram_difference),
                        scene_score=_round_signal(scene_score),
                        white_ratio=_round_signal(_white_ratio(raw)),
                        histogram=histogram,
                    )
                )
                previous_raw = raw
                previous_histogram = histogram

            extra_stdout, stderr = process.communicate()
            if process.returncode != 0:
                raise VideoAnalysisError(
                    "frame_decode_failed",
                    "FFmpeg 逐帧解码失败",
                    details={
                        "returncode": process.returncode,
                        "stderr": stderr.decode("utf-8", errors="replace")[-2000:],
                    },
                )
            if extra_stdout:
                extra_frames = len(extra_stdout) // frame_size
                raise VideoAnalysisError(
                    "frame_count_mismatch",
                    "FFmpeg 解码帧数多于 ffprobe 帧数",
                    details={
                        "probed_frames": len(probed_frames),
                        "extra_frames": extra_frames,
                    },
                )
        except Exception:
            if process.poll() is None:
                process.kill()
                process.communicate()
            raise
        return records

    def _adaptive_threshold(self, frames: list[_FrameRecord]) -> tuple[float, dict[str, Any]]:
        scores = sorted(frame.scene_score for frame in frames[1:])
        if not scores:
            threshold = self.config.minimum_boundary_score
            baseline_median = 0.0
            baseline_mad = 0.0
            upper_quartile = 0.0
        else:
            lower_half = scores[: max(1, (len(scores) + 1) // 2)]
            baseline_median = float(statistics.median(lower_half))
            baseline_mad = float(
                statistics.median(abs(score - baseline_median) for score in lower_half)
            )
            upper_quartile = scores[round((len(scores) - 1) * 0.75)]
            raw_threshold = baseline_median + self.config.boundary_mad_multiplier * baseline_mad
            threshold = min(
                max(self.config.minimum_boundary_score, raw_threshold),
                max(self.config.minimum_boundary_score, upper_quartile),
            )
        threshold = _round_signal(threshold)
        return threshold, {
            "method": "lower_half_median_mad_with_upper_quartile_cap",
            "value": threshold,
            "minimum": self.config.minimum_boundary_score,
            "baseline_median": _round_signal(baseline_median),
            "baseline_mad": _round_signal(baseline_mad),
            "upper_quartile": _round_signal(upper_quartile),
            "mad_multiplier": self.config.boundary_mad_multiplier,
        }

    def _detect_boundaries(self, frames: list[_FrameRecord], threshold: float) -> list[_Boundary]:
        flashes, claimed_transitions = self._detect_flashes(frames, threshold)
        boundaries = list(flashes)
        for frame in frames[1:]:
            if frame.probe.frame_index in claimed_transitions:
                continue
            if frame.scene_score + 1e-12 < threshold:
                continue
            frame_index = frame.probe.frame_index
            boundaries.append(
                _Boundary(
                    kind="scene_change_candidate",
                    frame_index=frame_index,
                    timestamp_us=frame.probe.timestamp_us,
                    pts=frame.probe.pts,
                    time_base=frame.probe.time_base,
                    score=frame.scene_score,
                    evidence_indexes=_neighbor_indexes(frame_index, len(frames)),
                )
            )
        return sorted(boundaries, key=lambda item: (item.timestamp_us, item.kind))

    def _detect_flashes(
        self, frames: list[_FrameRecord], threshold: float
    ) -> tuple[list[_Boundary], set[int]]:
        flashes: list[_Boundary] = []
        claimed: set[int] = set()
        index = 0
        while index < len(frames):
            if frames[index].white_ratio < self.config.flash_white_ratio:
                index += 1
                continue
            start = index
            while (
                index + 1 < len(frames)
                and frames[index + 1].white_ratio >= self.config.flash_white_ratio
            ):
                index += 1
            end = index
            before = start - 1
            after = end + 1
            if before >= 0 and after < len(frames):
                duration_us = frames[after].probe.timestamp_us - frames[start].probe.timestamp_us
                contrast = frames[start].brightness - max(
                    frames[before].brightness, frames[after].brightness
                )
                if (
                    duration_us <= self.config.flash_max_duration_us
                    and contrast >= self.config.flash_brightness_delta
                    and frames[start].scene_score + 1e-12 >= threshold
                    and frames[after].scene_score + 1e-12 >= threshold
                ):
                    center = (start + end) // 2
                    evidence = tuple(sorted({before, start, center, end, after}))
                    flashes.append(
                        _Boundary(
                            kind="flash_candidate",
                            frame_index=center,
                            timestamp_us=frames[center].probe.timestamp_us,
                            pts=frames[center].probe.pts,
                            time_base=frames[center].probe.time_base,
                            score=max(
                                frames[start].scene_score,
                                frames[after].scene_score,
                            ),
                            evidence_indexes=evidence,
                        )
                    )
                    claimed.update({start, after})
            index += 1
        return flashes, claimed

    def _detect_fast_cut_regions(
        self,
        frames: list[_FrameRecord],
        boundaries: list[_Boundary],
    ) -> list[_FastCutRegion]:
        if len(boundaries) < self.config.fast_cut_min_candidates:
            return []
        runs: list[list[int]] = []
        current = [0]
        for boundary_index in range(1, len(boundaries)):
            gap = (
                boundaries[boundary_index].timestamp_us
                - boundaries[boundary_index - 1].timestamp_us
            )
            if gap <= self.config.fast_cut_max_gap_us:
                current.append(boundary_index)
            else:
                runs.append(current)
                current = [boundary_index]
        runs.append(current)

        regions: list[_FastCutRegion] = []
        for run in runs:
            if len(run) < self.config.fast_cut_min_candidates:
                continue
            start_index = max(0, boundaries[run[0]].frame_index - 1)
            end_index = min(len(frames) - 1, boundaries[run[-1]].frame_index + 1)
            evidence_indexes = self._dense_evidence_indexes(frames, start_index, end_index)
            if evidence_indexes is None:
                continue
            regions.append(
                _FastCutRegion(
                    start_timestamp_us=frames[start_index].probe.timestamp_us,
                    end_timestamp_us=frames[end_index].probe.timestamp_us,
                    boundary_indexes=tuple(run),
                    evidence_indexes=evidence_indexes,
                )
            )
        return regions

    def _dense_evidence_indexes(
        self,
        frames: list[_FrameRecord],
        start_index: int,
        end_index: int,
    ) -> tuple[int, ...] | None:
        max_interval = self.config.fast_evidence_max_interval_us
        if any(
            frames[index + 1].probe.timestamp_us - frames[index].probe.timestamp_us > max_interval
            for index in range(start_index, end_index)
        ):
            return None
        selected = [start_index]
        current = start_index
        while current < end_index:
            next_index = current + 1
            while (
                next_index + 1 <= end_index
                and frames[next_index + 1].probe.timestamp_us - frames[current].probe.timestamp_us
                <= max_interval
            ):
                next_index += 1
            selected.append(next_index)
            current = next_index
        return tuple(selected)

    def _extract_evidence_images(
        self,
        source: Path,
        output_root: Path,
        frames: list[_FrameRecord],
        evidence_indexes: set[int],
        width: int,
        height: int,
    ) -> list[_EvidenceImage]:
        if not evidence_indexes:
            return []
        image_dir = output_root / "candidate_frames"
        try:
            image_dir.mkdir(parents=True, exist_ok=True)
            for stale in image_dir.glob("frame_*.jpg"):
                stale.unlink()
            for stale in image_dir.glob(".batch_*.jpg"):
                stale.unlink()
        except OSError as error:
            raise VideoAnalysisError(
                "output_unavailable", f"候选帧目录写入失败：{error}"
            ) from error

        images: list[_EvidenceImage] = []
        selected = sorted(evidence_indexes)
        for chunk_index, offset in enumerate(range(0, len(selected), 40)):
            chunk = selected[offset : offset + 40]
            expression = "+".join(f"eq(n\\,{frame_index})" for frame_index in chunk)
            temporary_pattern = image_dir / f".batch_{chunk_index:04d}_%06d.jpg"
            argv = [
                self.config.ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-vf",
                f"select='{expression}',scale={width}:{height}:flags=lanczos",
                "-fps_mode",
                "passthrough",
                "-q:v",
                "2",
                "-start_number",
                "0",
                str(temporary_pattern),
            ]
            try:
                completed = subprocess.run(
                    argv,
                    capture_output=True,
                    timeout=self.config.subprocess_timeout_seconds,
                    check=False,
                )
            except FileNotFoundError as error:
                raise VideoAnalysisError(
                    "dependency_missing", f"未找到 {self.config.ffmpeg_binary}"
                ) from error
            except subprocess.TimeoutExpired as error:
                self._delete_batch_files(image_dir, chunk_index)
                raise VideoAnalysisError("candidate_frame_timeout", "候选帧提取超时") from error
            except OSError as error:
                self._delete_batch_files(image_dir, chunk_index)
                raise VideoAnalysisError(
                    "candidate_frame_failed", f"候选帧提取失败：{error}"
                ) from error
            batch_files = sorted(image_dir.glob(f".batch_{chunk_index:04d}_*.jpg"))
            if completed.returncode != 0 or len(batch_files) != len(chunk):
                self._delete_batch_files(image_dir, chunk_index)
                raise VideoAnalysisError(
                    "candidate_frame_failed",
                    "FFmpeg 批量候选帧提取失败",
                    details={
                        "expected_frames": len(chunk),
                        "actual_frames": len(batch_files),
                        "stderr": completed.stderr.decode("utf-8", errors="replace")[-2000:],
                    },
                )
            for frame_index, temporary in zip(chunk, batch_files, strict=True):
                frame = frames[frame_index]
                timestamp_us = max(0, frame.probe.timestamp_us)
                filename = f"frame_{frame_index:06d}_{timestamp_us:012d}.jpg"
                destination = image_dir / filename
                try:
                    os.replace(temporary, destination)
                except OSError as error:
                    self._delete_batch_files(image_dir, chunk_index)
                    raise VideoAnalysisError(
                        "output_unavailable", f"候选帧发布失败：{error}"
                    ) from error
                images.append(
                    _EvidenceImage(
                        frame_index=frame_index,
                        timestamp_us=frame.probe.timestamp_us,
                        path=destination,
                        relative_path=destination.relative_to(output_root).as_posix(),
                        sha256=_sha256(destination),
                        size_bytes=destination.stat().st_size,
                    )
                )
        return images

    def _extract_contact_sheet(
        self,
        source: Path,
        output_root: Path,
        frames: list[_FrameRecord],
    ) -> Path:
        if not frames:
            raise VideoAnalysisError("frame_decode_failed", "视频没有可用于联系表的帧")
        duration_us = max(
            1,
            frames[-1].probe.timestamp_us
            - frames[0].probe.timestamp_us
            + _median_frame_interval_us(frames),
        )
        destination = output_root / "contact_sheet.jpg"
        temporary = output_root / ".contact_sheet.tmp.jpg"
        sample_fps = 12_000_000 / duration_us
        argv = [
            self.config.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-vf",
            f"fps={sample_fps:.12g},scale=320:-2:flags=lanczos,tile=4x3",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(temporary),
        ]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                timeout=self.config.subprocess_timeout_seconds,
                check=False,
            )
        except FileNotFoundError as error:
            raise VideoAnalysisError(
                "dependency_missing", f"未找到 {self.config.ffmpeg_binary}"
            ) from error
        except (OSError, subprocess.TimeoutExpired) as error:
            temporary.unlink(missing_ok=True)
            raise VideoAnalysisError("candidate_frame_failed", "联系表生成失败") from error
        if completed.returncode != 0 or not temporary.is_file():
            temporary.unlink(missing_ok=True)
            raise VideoAnalysisError(
                "candidate_frame_failed",
                "联系表生成失败",
                details={"stderr": completed.stderr.decode("utf-8", errors="replace")[-2000:]},
            )
        try:
            os.replace(temporary, destination)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise VideoAnalysisError("output_unavailable", "联系表发布失败") from error
        return destination

    @staticmethod
    def _delete_batch_files(image_dir: Path, chunk_index: int) -> None:
        for path in image_dir.glob(f".batch_{chunk_index:04d}_*.jpg"):
            path.unlink(missing_ok=True)

    @staticmethod
    def _boundary_dict(
        index: int,
        boundary: _Boundary,
        image_by_index: dict[int, _EvidenceImage],
    ) -> dict[str, Any]:
        return {
            "candidate_id": f"boundary_{index:04d}",
            "kind": boundary.kind,
            "status": "candidate",
            "possible_explanations": (
                ["white_flash", "full_frame_effect"]
                if boundary.kind == "flash_candidate"
                else ["hard_cut", "large_motion", "layer_change", "full_frame_effect"]
            ),
            "frame_index": boundary.frame_index,
            "timestamp_us": boundary.timestamp_us,
            "pts": boundary.pts,
            "time_base": boundary.time_base,
            "score": boundary.score,
            "evidence_frames": [
                image_by_index[frame_index].reference_dict()
                for frame_index in boundary.evidence_indexes
            ],
        }

    @staticmethod
    def _fast_region_dict(
        index: int,
        region: _FastCutRegion,
        boundaries: list[_Boundary],
        image_by_index: dict[int, _EvidenceImage],
    ) -> dict[str, Any]:
        return {
            "region_id": f"dense_change_{index:04d}",
            "classification": "dense_change_candidate",
            "candidate_semantics": "连续变化峰值需要复核后才能判断是否为快切镜头组",
            "start_timestamp_us": region.start_timestamp_us,
            "end_timestamp_us": region.end_timestamp_us,
            "boundary_frame_indexes": [
                boundaries[boundary_index].frame_index for boundary_index in region.boundary_indexes
            ],
            "maximum_evidence_interval_us": 100_000,
            "evidence_samples": [
                image_by_index[frame_index].reference_dict()
                for frame_index in region.evidence_indexes
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
            raise VideoAnalysisError("dependency_missing", f"未找到 {argv[0]}") from error
        except subprocess.TimeoutExpired as error:
            raise VideoAnalysisError(
                f"{failure_code}_timeout", f"{failure_message}：执行超时"
            ) from error
        except OSError as error:
            raise VideoAnalysisError(failure_code, f"{failure_message}：{error}") from error
        if completed.returncode != 0:
            raise VideoAnalysisError(
                failure_code,
                failure_message,
                details={
                    "returncode": completed.returncode,
                    "stderr": completed.stderr[-2000:],
                },
            )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise VideoAnalysisError(failure_code, f"{failure_message}：返回 JSON 无效") from error
        if not isinstance(value, dict):
            raise VideoAnalysisError(failure_code, f"{failure_message}：返回结构无效")
        return value

    def _config_dict(self) -> dict[str, Any]:
        return {
            "analysis_width": self.config.analysis_width,
            "evidence_width": self.config.evidence_width,
            "histogram_bins": self.config.histogram_bins,
            "minimum_boundary_score": self.config.minimum_boundary_score,
            "boundary_mad_multiplier": self.config.boundary_mad_multiplier,
            "flash_white_ratio": self.config.flash_white_ratio,
            "flash_brightness_delta": self.config.flash_brightness_delta,
            "flash_max_duration_us": self.config.flash_max_duration_us,
            "fast_cut_max_gap_us": self.config.fast_cut_max_gap_us,
            "fast_cut_min_candidates": self.config.fast_cut_min_candidates,
            "fast_evidence_max_interval_us": (self.config.fast_evidence_max_interval_us),
        }


def _read_exact(stream: IO[bytes], size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _rgb_histogram(raw: bytes, bins: int) -> dict[str, list[float]]:
    pixel_count = len(raw) // 3
    histograms: dict[str, list[float]] = {}
    for name, channel in (("r", raw[0::3]), ("g", raw[1::3]), ("b", raw[2::3])):
        counts = [0] * bins
        for value in channel:
            counts[min(bins - 1, value * bins // 256)] += 1
        histograms[name] = [_round_signal(count / pixel_count) for count in counts]
    return histograms


def _brightness(raw: bytes) -> float:
    pixel_count = len(raw) // 3
    red_mean = sum(raw[0::3]) / pixel_count
    green_mean = sum(raw[1::3]) / pixel_count
    blue_mean = sum(raw[2::3]) / pixel_count
    return (0.299 * red_mean + 0.587 * green_mean + 0.114 * blue_mean) / 255


def _white_ratio(raw: bytes) -> float:
    red = raw[0::3]
    green = raw[1::3]
    blue = raw[2::3]
    white_pixels = sum(
        1
        for red_value, green_value, blue_value in zip(red, green, blue, strict=True)
        if red_value >= 240 and green_value >= 240 and blue_value >= 240
    )
    return white_pixels / len(red)


def _mean_byte_difference(current: bytes, previous: bytes) -> float:
    return sum(
        abs(current_value - previous_value)
        for current_value, previous_value in zip(current, previous, strict=True)
    ) / (len(current) * 255)


def _histogram_difference(
    current: dict[str, list[float]], previous: dict[str, list[float]]
) -> float:
    distance = sum(
        abs(current_value - previous_value)
        for channel in ("r", "g", "b")
        for current_value, previous_value in zip(current[channel], previous[channel], strict=True)
    )
    return distance / 6


def _neighbor_indexes(frame_index: int, frame_count: int) -> tuple[int, ...]:
    return tuple(
        index
        for index in (frame_index - 1, frame_index, frame_index + 1)
        if 0 <= index < frame_count
    )


def _median_frame_interval_us(frames: list[_FrameRecord]) -> int:
    intervals = [
        current.probe.timestamp_us - previous.probe.timestamp_us
        for previous, current in zip(frames, frames[1:], strict=False)
        if current.probe.timestamp_us > previous.probe.timestamp_us
    ]
    return round(statistics.median(intervals)) if intervals else 1


def _required_positive_int(value: dict[str, Any], field: str) -> int:
    resolved = _optional_int(value.get(field))
    if resolved is None or resolved <= 0:
        raise VideoAnalysisError("invalid_media", f"视频流缺少有效 {field}")
    return resolved


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _round_signal(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 6)


def _count_pts_sources(frames: list[_ProbedFrame]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for frame in frames:
        counts[frame.pts_source] = counts.get(frame.pts_source, 0) + 1
    return counts


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
        raise VideoAnalysisError("input_unavailable", f"文件读取失败：{path}：{error}") from error
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    _write_text_atomic(path, text)


def _write_jsonl_atomic(path: Path, records: list[dict[str, Any]]) -> None:
    text = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in records
    )
    _write_text_atomic(path, text)


def _write_text_atomic(path: Path, text: str) -> None:
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
        raise VideoAnalysisError("output_unavailable", f"视频分析证据写入失败：{error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
