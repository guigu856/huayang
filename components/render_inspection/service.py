from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self, cast

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

_ALGORITHM_VERSION = "render-inspection-v1"


class RenderInspectionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InspectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OverlayExpectation(InspectionModel):
    overlay_id: str = Field(min_length=1)
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)

    @model_validator(mode="after")
    def end_is_after_start(self) -> Self:
        if self.end_us <= self.start_us:
            raise ValueError("画中画结束时间必须晚于开始时间")
        return self


class RenderExpectation(InspectionModel):
    duration_us: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(gt=0)
    shot_boundaries_us: list[int] = Field(default_factory=list)
    beat_grid_us: list[int] = Field(default_factory=list)
    overlays: list[OverlayExpectation] = Field(default_factory=list)
    expected_audio: bool = True
    asset_sha256s: list[str] = Field(default_factory=list)
    minimum_distinct_assets: int = Field(default=1, ge=1)
    action_count: int = Field(ge=0)
    traced_action_count: int = Field(ge=0)


class InspectionCheck(InspectionModel):
    code: str
    passed: bool
    message: str
    measured: dict[str, float | int | str | bool] = Field(default_factory=dict)


class RenderInspectionReport(InspectionModel):
    schema_version: Literal["1.0"] = "1.0"
    algorithm_version: str
    source_path: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed: bool
    checks: list[InspectionCheck]
    video_metrics: dict[str, float | int | str | bool]
    audio_metrics: dict[str, float | int | str | bool]
    overlay_metrics: list[dict[str, float | int | str | bool]]


@dataclass(frozen=True, slots=True)
class RenderInspectionConfig:
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    sample_width: int = 96
    sample_height: int = 54
    duration_tolerance_us: int = 70_000
    freeze_difference_threshold: float = 0.1
    maximum_freeze_run_ms: float = 600
    maximum_black_ratio: float = 0.03
    boundary_difference_threshold: float = 5.0
    beat_alignment_tolerance_us: int = 80_000
    overlay_difference_threshold: float = 4.0
    minimum_audio_rms_dbfs: float = -42
    maximum_audio_clipping_ratio: float = 0.01


@dataclass(frozen=True, slots=True)
class RenderInspectionResult:
    report: RenderInspectionReport
    report_path: Path
    contact_sheet_path: Path


class RenderInspectionService:
    """解码成片并检查技术完整性、规划覆盖和可观测剪辑事件。"""

    def __init__(self, config: RenderInspectionConfig | None = None) -> None:
        self.config = config or RenderInspectionConfig()

    def inspect(
        self,
        source: Path | str,
        expectation: RenderExpectation,
        output_dir: Path | str,
    ) -> RenderInspectionResult:
        path = Path(source).resolve()
        if not path.is_file():
            raise RenderInspectionError("media_not_found", "成片文件不存在")
        output = Path(output_dir).resolve()
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RenderInspectionError("output_unavailable", "检查目录创建失败") from error
        probe = self._probe(path)
        video = next(
            (stream for stream in probe["streams"] if stream.get("codec_type") == "video"),
            None,
        )
        audio = next(
            (stream for stream in probe["streams"] if stream.get("codec_type") == "audio"),
            None,
        )
        if video is None:
            raise RenderInspectionError("render_output_invalid", "成片缺少视频流")
        actual_fps = _rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        duration_us = _duration_us(probe, video)
        frames = self._decode_video(path)
        frame_differences = _frame_differences(frames)
        video_metrics = _video_metrics(
            frames,
            frame_differences,
            actual_fps,
            duration_us,
            video,
            self.config,
        )
        audio_metrics = self._audio_metrics(path, audio is not None)
        overlay_metrics = _overlay_metrics(
            frames,
            expectation,
            actual_fps,
            self.config,
        )
        checks = self._checks(
            expectation,
            video,
            audio is not None,
            actual_fps,
            duration_us,
            frames,
            frame_differences,
            video_metrics,
            audio_metrics,
            overlay_metrics,
        )
        contact_sheet = output / "contact_sheet.jpg"
        self._contact_sheet(path, expectation.duration_us, contact_sheet)
        report = RenderInspectionReport(
            algorithm_version=_ALGORITHM_VERSION,
            source_path=str(path),
            source_sha256=_sha256(path),
            passed=all(check.passed for check in checks),
            checks=checks,
            video_metrics=video_metrics,
            audio_metrics=audio_metrics,
            overlay_metrics=overlay_metrics,
        )
        report_path = output / "render_inspection.json"
        _write_json_atomic(report_path, report.model_dump(mode="json"))
        return RenderInspectionResult(report, report_path, contact_sheet)

    def _probe(self, path: Path) -> dict[str, Any]:
        completed = _run_text(
            [
                self.config.ffprobe_binary,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            "ffprobe_unavailable",
        )
        if completed.returncode != 0:
            raise RenderInspectionError("render_output_invalid", "ffprobe 未能读取成片")
        try:
            parsed: object = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RenderInspectionError("render_output_invalid", "ffprobe 返回结构无效") from error
        if not isinstance(parsed, dict):
            raise RenderInspectionError("render_output_invalid", "ffprobe 返回结构无效")
        payload = cast(dict[str, Any], parsed)
        if not isinstance(payload.get("streams"), list):
            raise RenderInspectionError("render_output_invalid", "ffprobe 缺少流清单")
        return payload

    def _decode_video(self, path: Path) -> np.ndarray[Any, np.dtype[np.uint8]]:
        completed = _run_binary(
            [
                self.config.ffmpeg_binary,
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-vf",
                f"scale={self.config.sample_width}:{self.config.sample_height}:flags=area",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            "ffmpeg_unavailable",
        )
        if completed.returncode != 0:
            raise RenderInspectionError("render_output_invalid", "成片逐帧解码失败")
        frame_bytes = self.config.sample_width * self.config.sample_height * 3
        if not completed.stdout or len(completed.stdout) % frame_bytes:
            raise RenderInspectionError("render_output_invalid", "成片帧数据不完整")
        array = np.frombuffer(completed.stdout, dtype=np.uint8)
        return array.reshape((-1, self.config.sample_height, self.config.sample_width, 3))

    def _audio_metrics(
        self,
        path: Path,
        has_audio: bool,
    ) -> dict[str, float | int | str | bool]:
        if not has_audio:
            return {"present": False, "sample_count": 0}
        completed = _run_binary(
            [
                self.config.ffmpeg_binary,
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-f",
                "f32le",
                "pipe:1",
            ],
            "ffmpeg_unavailable",
        )
        if completed.returncode != 0 or len(completed.stdout) % 4:
            raise RenderInspectionError("render_output_invalid", "成片音频解码失败")
        samples = np.frombuffer(completed.stdout, dtype="<f4")
        if samples.size == 0:
            return {"present": True, "sample_count": 0, "rms_dbfs": -120.0, "clipping_ratio": 0.0}
        rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
        rms_dbfs = 20 * math.log10(max(rms, 1e-6))
        clipping = float(np.mean(np.abs(samples) >= 0.999))
        return {
            "present": True,
            "sample_count": int(samples.size),
            "rms_dbfs": round(rms_dbfs, 4),
            "peak_dbfs": round(20 * math.log10(max(float(np.max(np.abs(samples))), 1e-6)), 4),
            "clipping_ratio": round(clipping, 8),
        }

    def _contact_sheet(self, path: Path, duration_us: int, destination: Path) -> None:
        sample_fps = 12_000_000 / duration_us
        completed = _run_text(
            [
                self.config.ffmpeg_binary,
                "-v",
                "error",
                "-y",
                "-i",
                str(path),
                "-vf",
                f"fps={sample_fps:.12g},scale=320:-2:flags=lanczos,tile=4x3",
                "-frames:v",
                "1",
                str(destination),
            ],
            "ffmpeg_unavailable",
        )
        if completed.returncode != 0 or not destination.is_file():
            raise RenderInspectionError("render_inspection_failed", "联系表生成失败")

    def _checks(
        self,
        expectation: RenderExpectation,
        video: dict[str, Any],
        has_audio: bool,
        actual_fps: float,
        duration_us: int,
        frames: np.ndarray[Any, np.dtype[np.uint8]],
        differences: np.ndarray[Any, np.dtype[np.float64]],
        video_metrics: dict[str, float | int | str | bool],
        audio_metrics: dict[str, float | int | str | bool],
        overlay_metrics: list[dict[str, float | int | str | bool]],
    ) -> list[InspectionCheck]:
        checks = [
            _check(
                "duration",
                abs(duration_us - expectation.duration_us) <= self.config.duration_tolerance_us,
                "成片时长与规格一致",
                actual_us=duration_us,
                expected_us=expectation.duration_us,
            ),
            _check(
                "canvas",
                int(video.get("width") or 0) == expectation.width
                and int(video.get("height") or 0) == expectation.height,
                "成片画布与规格一致",
                actual_width=int(video.get("width") or 0),
                actual_height=int(video.get("height") or 0),
            ),
            _check(
                "fps",
                abs(actual_fps - expectation.fps) <= 0.05,
                "成片帧率与规格一致",
                actual_fps=round(actual_fps, 6),
                expected_fps=expectation.fps,
            ),
            _check(
                "decode",
                len(frames)
                >= max(1, round(expectation.duration_us / 1_000_000 * expectation.fps) - 2),
                "完整解码帧数达到规格预期",
                decoded_frames=len(frames),
            ),
            _check(
                "black_frames",
                float(video_metrics["black_frame_ratio"]) <= self.config.maximum_black_ratio,
                "黑帧占比在阈值内",
                black_frame_ratio=float(video_metrics["black_frame_ratio"]),
            ),
            _check(
                "freeze_run",
                float(video_metrics["maximum_freeze_run_ms"]) <= self.config.maximum_freeze_run_ms,
                "连续近重复帧时长在阈值内",
                maximum_freeze_run_ms=float(video_metrics["maximum_freeze_run_ms"]),
            ),
            _check(
                "audio_presence",
                has_audio == expectation.expected_audio,
                "音频流存在性与规格一致",
                actual=has_audio,
                expected=expectation.expected_audio,
            ),
            _check(
                "asset_diversity",
                len(set(expectation.asset_sha256s)) >= expectation.minimum_distinct_assets,
                "规划使用的独立素材数量达标",
                distinct_assets=len(set(expectation.asset_sha256s)),
                minimum=expectation.minimum_distinct_assets,
            ),
            _check(
                "trace_coverage",
                expectation.action_count == expectation.traced_action_count,
                "每项规格动作都具有工程追溯映射",
                action_count=expectation.action_count,
                traced_action_count=expectation.traced_action_count,
            ),
        ]
        boundary_strengths = _boundary_strengths(
            differences, expectation.shot_boundaries_us, actual_fps
        )
        checks.append(
            _check(
                "hard_cut_boundaries",
                all(
                    value >= self.config.boundary_difference_threshold
                    for value in boundary_strengths
                ),
                "规划硬切在成片中形成可观测画面变化",
                boundary_count=len(boundary_strengths),
                minimum_strength=round(min(boundary_strengths), 4) if boundary_strengths else 0.0,
            )
        )
        beat_error = _maximum_beat_error(expectation.shot_boundaries_us, expectation.beat_grid_us)
        checks.append(
            _check(
                "beat_alignment",
                beat_error <= self.config.beat_alignment_tolerance_us,
                "镜头边界与规划节拍的偏差在阈值内",
                maximum_error_us=beat_error,
            )
        )
        checks.append(
            _check(
                "overlay_events",
                all(bool(item["passed"]) for item in overlay_metrics),
                "规划画中画的进入和退出在目标区域可观测",
                overlay_count=len(overlay_metrics),
                observed=sum(bool(item["passed"]) for item in overlay_metrics),
            )
        )
        if expectation.expected_audio:
            checks.extend(
                [
                    _check(
                        "audio_level",
                        float(audio_metrics.get("rms_dbfs", -120))
                        >= self.config.minimum_audio_rms_dbfs,
                        "音频有效响度高于静音阈值",
                        rms_dbfs=float(audio_metrics.get("rms_dbfs", -120)),
                    ),
                    _check(
                        "audio_clipping",
                        float(audio_metrics.get("clipping_ratio", 1))
                        <= self.config.maximum_audio_clipping_ratio,
                        "音频削波占比在阈值内",
                        clipping_ratio=float(audio_metrics.get("clipping_ratio", 1)),
                    ),
                ]
            )
        return checks


def _video_metrics(
    frames: np.ndarray[Any, np.dtype[np.uint8]],
    differences: np.ndarray[Any, np.dtype[np.float64]],
    fps: float,
    duration_us: int,
    video: dict[str, Any],
    config: RenderInspectionConfig,
) -> dict[str, float | int | str | bool]:
    brightness = frames.astype(np.float32).mean(axis=(1, 2, 3))
    frozen = differences < config.freeze_difference_threshold
    longest = _longest_true_run(frozen)
    return {
        "decoded_frame_count": int(len(frames)),
        "duration_us": duration_us,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 6),
        "black_frame_ratio": round(float(np.mean(brightness < 8)), 8),
        "near_duplicate_ratio": round(float(np.mean(frozen)) if frozen.size else 0.0, 8),
        "maximum_freeze_run_ms": round(longest / fps * 1000 if fps else 0.0, 4),
        "mean_frame_difference": round(float(np.mean(differences)) if differences.size else 0.0, 4),
    }


def _overlay_metrics(
    frames: np.ndarray[Any, np.dtype[np.uint8]],
    expectation: RenderExpectation,
    fps: float,
    config: RenderInspectionConfig,
) -> list[dict[str, float | int | str | bool]]:
    results: list[dict[str, float | int | str | bool]] = []
    for overlay in expectation.overlays:
        x0 = max(
            0,
            min(
                config.sample_width - 1, round(overlay.x / expectation.width * config.sample_width)
            ),
        )
        y0 = max(
            0,
            min(
                config.sample_height - 1,
                round(overlay.y / expectation.height * config.sample_height),
            ),
        )
        x1 = max(
            x0 + 1,
            min(
                config.sample_width,
                round((overlay.x + overlay.width) / expectation.width * config.sample_width),
            ),
        )
        y1 = max(
            y0 + 1,
            min(
                config.sample_height,
                round((overlay.y + overlay.height) / expectation.height * config.sample_height),
            ),
        )
        entry = _event_region_difference(frames, overlay.start_us, fps, x0, y0, x1, y1)
        exit_value = _event_region_difference(frames, overlay.end_us, fps, x0, y0, x1, y1)
        passed = max(entry, exit_value) >= config.overlay_difference_threshold
        results.append(
            {
                "overlay_id": overlay.overlay_id,
                "entry_difference": round(entry, 4),
                "exit_difference": round(exit_value, 4),
                "passed": passed,
            }
        )
    return results


def _event_region_difference(
    frames: np.ndarray[Any, np.dtype[np.uint8]],
    timestamp_us: int,
    fps: float,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
) -> float:
    index = round(timestamp_us / 1_000_000 * fps)
    before = max(0, min(len(frames) - 1, index - 1))
    after = max(0, min(len(frames) - 1, index + 1))
    left = frames[before, y0:y1, x0:x1].astype(np.float32)
    right = frames[after, y0:y1, x0:x1].astype(np.float32)
    return float(np.mean(np.abs(right - left)))


def _frame_differences(
    frames: np.ndarray[Any, np.dtype[np.uint8]],
) -> np.ndarray[Any, np.dtype[np.float64]]:
    if len(frames) < 2:
        return np.empty(0, dtype=np.float64)
    values = np.abs(np.diff(frames.astype(np.float32), axis=0)).mean(axis=(1, 2, 3))
    return cast(np.ndarray[Any, np.dtype[np.float64]], values.astype(np.float64))


def _boundary_strengths(
    differences: np.ndarray[Any, np.dtype[np.float64]],
    boundaries_us: list[int],
    fps: float,
) -> list[float]:
    values: list[float] = []
    for boundary in boundaries_us:
        index = round(boundary / 1_000_000 * fps) - 1
        if 0 <= index < len(differences):
            values.append(float(differences[index]))
    return values


def _maximum_beat_error(boundaries: list[int], beats: list[int]) -> int:
    if not boundaries:
        return 0
    if not beats:
        return 2**31 - 1
    return max(min(abs(boundary - beat) for beat in beats) for boundary in boundaries)


def _longest_true_run(values: np.ndarray[Any, np.dtype[np.bool_]]) -> int:
    longest = 0
    current = 0
    for value in values:
        current = current + 1 if bool(value) else 0
        longest = max(longest, current)
    return longest


def _duration_us(payload: dict[str, Any], video: dict[str, Any]) -> int:
    format_data = payload.get("format")
    format_duration: object = format_data.get("duration") if isinstance(format_data, dict) else None
    raw: object = format_duration or video.get("duration")
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise RenderInspectionError("render_output_invalid", "成片缺少有效时长")
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise RenderInspectionError("render_output_invalid", "成片缺少有效时长") from error
    return round(value * 1_000_000)


def _rate(raw: object) -> float:
    try:
        numerator, denominator = str(raw).split("/", maxsplit=1)
        value = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError) as error:
        raise RenderInspectionError("render_output_invalid", "成片缺少有效帧率") from error
    if value <= 0:
        raise RenderInspectionError("render_output_invalid", "成片帧率无效")
    return value


def _check(
    code: str, passed: bool, message: str, **measured: float | int | str | bool
) -> InspectionCheck:
    return InspectionCheck(code=code, passed=passed, message=message, measured=measured)


def _run_text(argv: list[str], missing_code: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except FileNotFoundError as error:
        raise RenderInspectionError(missing_code, "媒体工具不存在") from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RenderInspectionError("render_inspection_failed", "媒体工具执行失败") from error


def _run_binary(argv: list[str], missing_code: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(argv, capture_output=True, timeout=120, check=False)
    except FileNotFoundError as error:
        raise RenderInspectionError(missing_code, "媒体工具不存在") from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RenderInspectionError("render_inspection_failed", "媒体工具执行失败") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as error:
        raise RenderInspectionError("output_unavailable", "检查报告写入失败") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
