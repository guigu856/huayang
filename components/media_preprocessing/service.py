"""声明式媒体预处理与父子溯源。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, model_validator

_SCHEMA_VERSION = 1
_PAD_COLOR = re.compile(r"^(?:black|white|0x[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?)$")


class MediaPreprocessingError(RuntimeError):
    """媒体预处理的稳定错误契约。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MediaOperation(StrEnum):
    """当前剪辑工作流使用的确定性媒体操作。"""

    VIDEO_TRIM = "video_trim"
    AUDIO_TRIM = "audio_trim"
    FRAME_EXTRACT = "frame_extract"
    SCALE_PAD = "scale_pad"


class MediaPreprocessRequest(BaseModel):
    """单次预处理的严格声明式输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: MediaOperation
    input_path: Path
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_seconds: float | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, gt=0)
    timestamp_seconds: float | None = Field(default=None, ge=0)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    pad_color: str | None = None

    @model_validator(mode="after")
    def validate_operation_parameters(self) -> Self:
        trim_values = (self.start_seconds, self.duration_seconds)
        dimension_values = (self.width, self.height)
        if self.operation in {MediaOperation.VIDEO_TRIM, MediaOperation.AUDIO_TRIM}:
            if None in trim_values:
                raise ValueError("trim 操作必须声明 start_seconds 和 duration_seconds")
            if (
                self.timestamp_seconds is not None
                or any(value is not None for value in dimension_values)
                or self.pad_color is not None
            ):
                raise ValueError("trim 操作包含无关参数")
        elif self.operation is MediaOperation.FRAME_EXTRACT:
            if self.timestamp_seconds is None:
                raise ValueError("frame_extract 必须声明 timestamp_seconds")
            if (
                any(value is not None for value in trim_values)
                or any(value is not None for value in dimension_values)
                or self.pad_color is not None
            ):
                raise ValueError("frame_extract 包含无关参数")
        elif self.operation is MediaOperation.SCALE_PAD:
            if None in dimension_values or self.pad_color is None:
                raise ValueError("scale_pad 必须声明 width、height 和 pad_color")
            if (
                any(value is not None for value in trim_values)
                or self.timestamp_seconds is not None
            ):
                raise ValueError("scale_pad 包含无关参数")
            assert self.width is not None and self.height is not None
            if self.width % 2 or self.height % 2:
                raise ValueError("scale_pad 的 width 和 height 必须是偶数")
            if _PAD_COLOR.fullmatch(self.pad_color) is None:
                raise ValueError("pad_color 仅支持 black、white 或 0xRRGGBB[AA]")
        return self

    def operation_parameters(self) -> dict[str, Any]:
        names = {
            MediaOperation.VIDEO_TRIM: ("start_seconds", "duration_seconds"),
            MediaOperation.AUDIO_TRIM: ("start_seconds", "duration_seconds"),
            MediaOperation.FRAME_EXTRACT: ("timestamp_seconds",),
            MediaOperation.SCALE_PAD: ("width", "height", "pad_color"),
        }[self.operation]
        return {name: getattr(self, name) for name in names}


@dataclass(frozen=True, slots=True)
class MediaPreprocessingConfig:
    """预处理输出与可执行文件配置。"""

    output_dir: Path = Path("output/preprocessed")
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"


@dataclass(frozen=True, slots=True)
class MediaPreprocessingResult:
    """一个已验证的派生媒体及父子关系。"""

    operation: MediaOperation
    output_path: Path
    provenance_path: Path
    output_sha256: str
    parent_sha256: str
    size_bytes: int
    mime_type: str
    duration_seconds: float | None
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None
    applied_parameters: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["operation"] = self.operation.value
        payload["output_path"] = str(self.output_path)
        payload["provenance_path"] = str(self.provenance_path)
        return payload


@dataclass(frozen=True, slots=True)
class _MediaProbe:
    duration_seconds: float | None
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None
    format_name: str | None


class MediaPreprocessingService:
    """验证输入哈希后执行单个声明式 FFmpeg 操作。"""

    def __init__(self, config: MediaPreprocessingConfig | None = None) -> None:
        self.config = config or MediaPreprocessingConfig()

    def execute(self, request: MediaPreprocessRequest) -> MediaPreprocessingResult:
        input_path = request.input_path.expanduser().resolve()
        if not input_path.is_file():
            raise MediaPreprocessingError("input_missing", f"输入媒体不存在：{input_path}")
        actual_input_sha = _sha256_file(input_path)
        if not secrets.compare_digest(request.input_sha256, actual_input_sha):
            raise MediaPreprocessingError("input_sha256_mismatch", "输入媒体 SHA-256 不匹配")

        ffmpeg = _resolve_binary(self.config.ffmpeg_binary, "ffmpeg")
        ffprobe = _resolve_binary(self.config.ffprobe_binary, "ffprobe")
        source_probe = _probe_media(ffprobe, input_path)
        _validate_source(request, source_probe)
        output_root = _prepare_output_root(self.config.output_dir)
        extension, mime_type = _output_contract(request.operation)

        with tempfile.TemporaryDirectory(prefix=".preprocess-", dir=output_root) as temp_name:
            temp_output = Path(temp_name) / f"derived{extension}"
            command = _build_ffmpeg_command(ffmpeg, input_path, temp_output, request)
            _run_ffmpeg(command)
            derived_probe = _validate_derived(
                ffprobe,
                temp_output,
                request=request,
                source_probe=source_probe,
                mime_type=mime_type,
            )
            output_sha = _sha256_file(temp_output)
            output_path, provenance_path = _output_paths(
                output_root,
                input_stem=input_path.stem,
                operation=request.operation,
                output_sha=output_sha,
                extension=extension,
            )
            provenance = {
                "schema_version": _SCHEMA_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "operation": request.operation.value,
                "parameters": request.operation_parameters(),
                "parent": {
                    "file_path": str(input_path),
                    "sha256": actual_input_sha,
                },
                "derivative": {
                    "relation": "derived_from_parent",
                    "file_path": str(output_path),
                    "sha256": output_sha,
                    "size_bytes": temp_output.stat().st_size,
                    "mime_type": mime_type,
                    "duration_seconds": derived_probe.duration_seconds,
                    "width": derived_probe.width,
                    "height": derived_probe.height,
                    "video_codec": derived_probe.video_codec,
                    "audio_codec": derived_probe.audio_codec,
                },
            }
            size_bytes = temp_output.stat().st_size
            _commit_derivative(temp_output, output_path, provenance, provenance_path)

        return MediaPreprocessingResult(
            operation=request.operation,
            output_path=output_path,
            provenance_path=provenance_path,
            output_sha256=output_sha,
            parent_sha256=actual_input_sha,
            size_bytes=size_bytes,
            mime_type=mime_type,
            duration_seconds=derived_probe.duration_seconds,
            width=derived_probe.width,
            height=derived_probe.height,
            video_codec=derived_probe.video_codec,
            audio_codec=derived_probe.audio_codec,
            applied_parameters=request.operation_parameters(),
        )


def _validate_source(request: MediaPreprocessRequest, probe: _MediaProbe) -> None:
    requires_video = request.operation in {
        MediaOperation.VIDEO_TRIM,
        MediaOperation.FRAME_EXTRACT,
        MediaOperation.SCALE_PAD,
    }
    if requires_video and probe.video_codec is None:
        raise MediaPreprocessingError("required_stream_missing", "操作要求输入包含视频流")
    if request.operation is MediaOperation.AUDIO_TRIM and probe.audio_codec is None:
        raise MediaPreprocessingError("required_stream_missing", "audio_trim 要求输入包含音频流")
    if request.operation in {MediaOperation.VIDEO_TRIM, MediaOperation.AUDIO_TRIM}:
        assert request.start_seconds is not None
        assert request.duration_seconds is not None
        _validate_time_range(
            probe.duration_seconds,
            start=request.start_seconds,
            duration=request.duration_seconds,
        )
    elif request.operation is MediaOperation.FRAME_EXTRACT:
        assert request.timestamp_seconds is not None
        _validate_timestamp(probe.duration_seconds, request.timestamp_seconds)


def _validate_time_range(
    input_duration: float | None,
    *,
    start: float,
    duration: float,
) -> None:
    if input_duration is None:
        raise MediaPreprocessingError("invalid_media", "输入媒体缺少可验证时长")
    if start + duration > input_duration + 0.001:
        raise MediaPreprocessingError("time_range_out_of_bounds", "声明的裁剪范围超出输入时长")


def _validate_timestamp(duration: float | None, timestamp: float) -> None:
    if duration is None:
        raise MediaPreprocessingError("invalid_media", "输入媒体缺少可验证时长")
    if timestamp >= duration:
        raise MediaPreprocessingError("timestamp_out_of_bounds", "声明的抽帧时间不在输入范围内")


def _build_ffmpeg_command(
    ffmpeg: str,
    input_path: Path,
    output_path: Path,
    request: MediaPreprocessRequest,
) -> list[str]:
    prefix = [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(input_path)]
    if request.operation is MediaOperation.VIDEO_TRIM:
        assert request.start_seconds is not None and request.duration_seconds is not None
        return prefix + [
            "-ss",
            _decimal(request.start_seconds),
            "-t",
            _decimal(request.duration_seconds),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    if request.operation is MediaOperation.AUDIO_TRIM:
        assert request.start_seconds is not None and request.duration_seconds is not None
        return prefix + [
            "-ss",
            _decimal(request.start_seconds),
            "-t",
            _decimal(request.duration_seconds),
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_path),
        ]
    if request.operation is MediaOperation.FRAME_EXTRACT:
        assert request.timestamp_seconds is not None
        return prefix + [
            "-ss",
            _decimal(request.timestamp_seconds),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-c:v",
            "png",
            str(output_path),
        ]
    assert request.width is not None
    assert request.height is not None
    assert request.pad_color is not None
    video_filter = (
        f"scale=w={request.width}:h={request.height}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={request.width}:{request.height}:(ow-iw)/2:(oh-ih)/2:color={request.pad_color}"
    )
    return prefix + [
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        video_filter,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def _run_ffmpeg(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, check=False)
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise MediaPreprocessingError("ffmpeg_failed", f"FFmpeg 预处理失败：{message}")


def _validate_derived(
    ffprobe: str,
    path: Path,
    *,
    request: MediaPreprocessRequest,
    source_probe: _MediaProbe,
    mime_type: str,
) -> _MediaProbe:
    if request.operation is MediaOperation.FRAME_EXTRACT:
        try:
            with Image.open(path) as image:
                image.load()
                if image.format != "PNG" or min(image.size) <= 0:
                    raise MediaPreprocessingError("invalid_output", "抽帧输出不是有效 PNG")
                width, height = image.size
        except (OSError, UnidentifiedImageError) as error:
            raise MediaPreprocessingError("invalid_output", "抽帧输出解码失败") from error
        if (width, height) != (source_probe.width, source_probe.height):
            raise MediaPreprocessingError("invalid_output", "抽帧输出尺寸与输入视频不一致")
        return _MediaProbe(
            duration_seconds=None,
            width=width,
            height=height,
            video_codec=None,
            audio_codec=None,
            format_name=None,
        )

    probe = _probe_media(ffprobe, path)
    if request.operation is MediaOperation.AUDIO_TRIM:
        if (
            probe.audio_codec != "aac"
            or probe.video_codec is not None
            or not _format_contains(probe, "m4a")
        ):
            raise MediaPreprocessingError("invalid_output", "audio_trim 输出流结构无效")
    else:
        if probe.video_codec != "h264" or not _format_contains(probe, "mp4"):
            raise MediaPreprocessingError("invalid_output", "视频派生结果缺少视频流")
        expected_audio = "aac" if source_probe.audio_codec is not None else None
        if probe.audio_codec != expected_audio:
            raise MediaPreprocessingError("invalid_output", "视频派生结果的音频流结构无效")
    if request.operation is MediaOperation.VIDEO_TRIM:
        if (probe.width, probe.height) != (source_probe.width, source_probe.height):
            raise MediaPreprocessingError("invalid_output", "video_trim 改变了画面尺寸")
    if request.operation is MediaOperation.SCALE_PAD:
        if (probe.width, probe.height) != (request.width, request.height):
            raise MediaPreprocessingError("invalid_output", "scale_pad 输出尺寸不等于声明尺寸")
        if probe.duration_seconds is None or source_probe.duration_seconds is None:
            raise MediaPreprocessingError("invalid_output", "scale_pad 输出缺少可验证时长")
        if abs(probe.duration_seconds - source_probe.duration_seconds) > 0.2:
            raise MediaPreprocessingError("invalid_output", "scale_pad 改变了媒体时长")
    if request.operation in {MediaOperation.VIDEO_TRIM, MediaOperation.AUDIO_TRIM}:
        assert request.duration_seconds is not None
        if (
            probe.duration_seconds is None
            or abs(probe.duration_seconds - request.duration_seconds) > 0.2
        ):
            raise MediaPreprocessingError("invalid_output", "裁剪输出时长偏离声明时长")
    if mime_type not in {"video/mp4", "audio/mp4"}:
        raise MediaPreprocessingError("invalid_output", "派生媒体 MIME 契约无效")
    return probe


def _format_contains(probe: _MediaProbe, name: str) -> bool:
    if probe.format_name is None:
        return False
    return name in probe.format_name.split(",")


def _probe_media(ffprobe: str, path: Path) -> _MediaProbe:
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise MediaPreprocessingError("invalid_media", f"ffprobe 验证失败：{message}")
    try:
        payload: Any = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MediaPreprocessingError("invalid_media", "ffprobe 返回无效 JSON") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        raise MediaPreprocessingError("invalid_media", "ffprobe 响应结构无效")
    streams = payload["streams"]
    video = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ),
        None,
    )
    audio = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "audio"
        ),
        None,
    )
    format_info = payload.get("format")
    duration = (
        _optional_float(format_info.get("duration")) if isinstance(format_info, dict) else None
    )
    if duration is None:
        duration = _optional_float(video.get("duration")) if isinstance(video, dict) else None
    if duration is None:
        duration = _optional_float(audio.get("duration")) if isinstance(audio, dict) else None
    return _MediaProbe(
        duration_seconds=duration,
        width=_optional_int(video.get("width")) if isinstance(video, dict) else None,
        height=_optional_int(video.get("height")) if isinstance(video, dict) else None,
        video_codec=_optional_text(video.get("codec_name")) if isinstance(video, dict) else None,
        audio_codec=_optional_text(audio.get("codec_name")) if isinstance(audio, dict) else None,
        format_name=(
            _optional_text(format_info.get("format_name"))
            if isinstance(format_info, dict)
            else None
        ),
    )


def _optional_float(value: Any) -> float | None:
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        return None
    return resolved if resolved >= 0 else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return None
    return resolved if resolved > 0 else None


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _resolve_binary(binary: str, label: str) -> str:
    resolved = shutil.which(binary)
    if resolved is None:
        raise MediaPreprocessingError("dependency_missing", f"PATH 中缺少 {label}")
    return resolved


def _output_contract(operation: MediaOperation) -> tuple[str, str]:
    return {
        MediaOperation.VIDEO_TRIM: (".mp4", "video/mp4"),
        MediaOperation.AUDIO_TRIM: (".m4a", "audio/mp4"),
        MediaOperation.FRAME_EXTRACT: (".png", "image/png"),
        MediaOperation.SCALE_PAD: (".mp4", "video/mp4"),
    }[operation]


def _prepare_output_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    try:
        (root / "derivatives").mkdir(parents=True, exist_ok=True)
        (root / "provenance").mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise MediaPreprocessingError(
            "output_unavailable", f"预处理输出目录不可写：{root}"
        ) from error
    return root


def _output_paths(
    output_root: Path,
    *,
    input_stem: str,
    operation: MediaOperation,
    output_sha: str,
    extension: str,
) -> tuple[Path, Path]:
    safe_stem = re.sub(r"[^0-9A-Za-z_-]+", "_", input_stem).strip("_") or "media"
    base = f"{safe_stem}_{operation.value}_{output_sha[:12]}"
    index = 1
    while True:
        suffix = "" if index == 1 else f"_{index}"
        output_path = (output_root / "derivatives" / f"{base}{suffix}{extension}").resolve()
        provenance_path = (output_root / "provenance" / f"{base}{suffix}.json").resolve()
        if not output_path.exists() and not provenance_path.exists():
            return output_path, provenance_path
        index += 1


def _commit_derivative(
    temp_output: Path,
    output_path: Path,
    provenance: dict[str, Any],
    provenance_path: Path,
) -> None:
    temp_provenance = temp_output.with_name(f".{provenance_path.name}.tmp")
    try:
        temp_provenance.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_output, output_path)
        try:
            os.replace(temp_provenance, provenance_path)
        except OSError:
            output_path.unlink(missing_ok=True)
            raise
    except OSError as error:
        temp_provenance.unlink(missing_ok=True)
        raise MediaPreprocessingError("output_unavailable", "派生媒体或溯源记录写入失败") from error


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise MediaPreprocessingError("input_unreadable", f"媒体文件不可读取：{path}") from error
    return digest.hexdigest()


def _decimal(value: float) -> str:
    return format(value, ".9g")
