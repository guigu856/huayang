from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

from pydantic import ValidationError

from .errors import VideoEditorError
from .models import AssetCreate, MediaMetadata

MediaKind = Literal["video", "image", "audio"]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

_SAFE_SUFFIX = re.compile(r"^\.[a-z0-9]{1,10}$")
_DEFAULT_CHUNK_SIZE = 1024 * 1024
MAX_MEDIA_BYTES = 4 * 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class StoredMedia:
    name: str
    relative_path: str
    absolute_path: Path
    size: int


def store_media_stream(
    project_dir: Path | str,
    filename: str,
    source: BinaryIO,
    *,
    max_bytes: int | None = None,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
) -> StoredMedia:
    """将媒体流分块写入工程 assets 目录，成功后才暴露最终文件名。"""

    if chunk_size <= 0 or max_bytes is not None and max_bytes < 0:
        raise VideoEditorError("invalid_input", "媒体导入参数无效")
    name = _safe_name(filename)
    suffix = Path(name).suffix.lower()
    if _SAFE_SUFFIX.fullmatch(suffix) is None:
        suffix = ".bin"

    assets_dir = Path(project_dir) / "assets"
    final_path = assets_dir / f"{uuid.uuid4().hex}{suffix}"
    temporary_path: Path | None = None
    total = 0
    try:
        assets_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".upload-",
            suffix=f".tmp{suffix}",
            dir=assets_dir,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = source.read(chunk_size)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise VideoEditorError("invalid_media_stream", "媒体流必须返回字节")
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise VideoEditorError("media_too_large", "媒体文件超过允许大小")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, final_path)
        temporary_path = None
    except VideoEditorError:
        raise
    except (OSError, ValueError) as error:
        raise VideoEditorError("output_unavailable", f"媒体文件写入失败：{error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if temporary_path is not None or not final_path.is_file():
            final_path.unlink(missing_ok=True)

    return StoredMedia(
        name=name,
        relative_path=final_path.relative_to(Path(project_dir)).as_posix(),
        absolute_path=final_path,
        size=total,
    )


def import_media(
    project_dir: Path | str,
    filename: str,
    source: BinaryIO,
    *,
    max_bytes: int | None = None,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    probe: Callable[[Path], MediaMetadata] | None = None,
) -> AssetCreate:
    """导入并探测一份媒体，返回可交给 asset.add 的领域输入。"""

    stored = store_media_stream(
        project_dir,
        filename,
        source,
        max_bytes=max_bytes,
        chunk_size=chunk_size,
    )
    try:
        metadata = (probe or probe_media)(stored.absolute_path)
        return AssetCreate(
            kind=infer_media_kind(metadata),
            name=stored.name,
            path=stored.relative_path,
            metadata=metadata,
        )
    except VideoEditorError:
        stored.absolute_path.unlink(missing_ok=True)
        raise
    except (OSError, ValidationError, TypeError, ValueError) as error:
        stored.absolute_path.unlink(missing_ok=True)
        raise VideoEditorError("invalid_media_metadata", "媒体元数据无效") from error


def probe_media(
    path: Path | str,
    *,
    ffprobe_binary: str = "ffprobe",
    runner: CommandRunner | None = None,
) -> MediaMetadata:
    """通过 ffprobe 读取编辑器需要的视频、音频元数据。"""

    media_path = Path(path)
    if not media_path.is_file():
        raise VideoEditorError("media_not_found", "媒体文件不存在")
    argv = [
        ffprobe_binary,
        "-v",
        "error",
        "-show_entries",
        (
            "format=duration:"
            "stream=codec_type,codec_name,width,height,avg_frame_rate,"
            "r_frame_rate,sample_rate,channels,duration"
        ),
        "-of",
        "json",
        str(media_path),
    ]
    try:
        completed = (runner or subprocess.run)(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except FileNotFoundError as error:
        raise VideoEditorError("ffprobe_unavailable", "未找到 ffprobe") from error
    except subprocess.TimeoutExpired as error:
        raise VideoEditorError("media_probe_timeout", "媒体探测超时") from error
    except OSError as error:
        raise VideoEditorError("media_probe_failed", f"媒体探测失败：{error}") from error

    if completed.returncode != 0:
        raise VideoEditorError(
            "media_probe_failed",
            "ffprobe 未能读取媒体",
            details={"stderr": completed.stderr[-2000:]},
        )
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        if not isinstance(streams, list):
            raise TypeError("streams must be a list")
        video = next(
            (stream for stream in streams if stream.get("codec_type") == "video"),
            None,
        )
        audio = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"),
            None,
        )
        if video is None and audio is None:
            raise VideoEditorError("unsupported_media", "文件不包含视频、图片或音频流")
        format_data = payload.get("format", {})
        duration = _positive_float(format_data.get("duration"))
        if duration is None:
            duration = _positive_float(
                (video or audio or {}).get("duration")
            )
        return MediaMetadata(
            duration=duration,
            width=_positive_int(video.get("width")) if video else None,
            height=_positive_int(video.get("height")) if video else None,
            frame_rate=_frame_rate(video) if video else None,
            video_codec=_string(video.get("codec_name")) if video else None,
            audio_codec=_string(audio.get("codec_name")) if audio else None,
            sample_rate=_positive_int(audio.get("sample_rate")) if audio else None,
            channels=_positive_int(audio.get("channels")) if audio else None,
        )
    except VideoEditorError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as error:
        raise VideoEditorError("media_probe_failed", "ffprobe 返回结构无效") from error


def infer_media_kind(metadata: MediaMetadata) -> MediaKind:
    if metadata.width is not None:
        return "video" if metadata.duration is not None else "image"
    if metadata.duration is not None:
        return "audio"
    raise VideoEditorError("unsupported_media", "媒体元数据不足以判断素材类型")


def resolve_media_path(project_dir: Path | str, relative_path: str) -> Path:
    root = Path(project_dir).resolve()
    try:
        candidate = (root / relative_path).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError) as error:
        raise VideoEditorError("invalid_asset_path", "素材路径越出工程目录") from error
    if not candidate.is_file():
        raise VideoEditorError("media_not_found", "素材文件不存在")
    return candidate


def _safe_name(filename: str) -> str:
    if not isinstance(filename, str) or "\x00" in filename:
        raise VideoEditorError("invalid_filename", "媒体文件名无效")
    name = Path(filename.replace("\\", "/")).name.strip()
    if name in {"", ".", ".."}:
        raise VideoEditorError("invalid_filename", "媒体文件名无效")
    return name


def _frame_rate(stream: dict[str, Any]) -> float | None:
    for field in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(field)
        if not isinstance(raw, str):
            continue
        try:
            numerator, denominator = raw.split("/", maxsplit=1)
            value = float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            continue
        if value > 0:
            return value
    return None


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        resolved = float(value)
    except ValueError:
        return None
    return resolved if resolved > 0 else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        resolved = int(value)
    except ValueError:
        return None
    return resolved if resolved > 0 else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
