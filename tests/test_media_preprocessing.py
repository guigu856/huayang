from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from components import media_preprocessing as package
from components.media_preprocessing import (
    MediaOperation,
    MediaPreprocessingConfig,
    MediaPreprocessingError,
    MediaPreprocessingService,
    MediaPreprocessRequest,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def source_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("FFmpeg and ffprobe are required")
    root = tmp_path_factory.mktemp("media-preprocessing")
    source = root / "source.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=30:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    return source


def _service(tmp_path: Path) -> MediaPreprocessingService:
    return MediaPreprocessingService(MediaPreprocessingConfig(output_dir=tmp_path))


def test_request_rejects_missing_irrelevant_or_implicit_parameters(source_video: Path) -> None:
    common = {"input_path": source_video, "input_sha256": _sha256(source_video)}

    with pytest.raises(ValidationError, match="timestamp_seconds"):
        MediaPreprocessRequest(operation=MediaOperation.FRAME_EXTRACT, **common)
    with pytest.raises(ValidationError, match="无关参数"):
        MediaPreprocessRequest(
            operation=MediaOperation.FRAME_EXTRACT,
            timestamp_seconds=0.5,
            duration_seconds=1.0,
            **common,
        )
    with pytest.raises(ValidationError, match="偶数"):
        MediaPreprocessRequest(
            operation=MediaOperation.SCALE_PAD,
            width=101,
            height=100,
            pad_color="black",
            **common,
        )


def test_execute_checks_parent_sha_before_ffmpeg(source_video: Path, tmp_path: Path) -> None:
    request = MediaPreprocessRequest(
        operation=MediaOperation.FRAME_EXTRACT,
        input_path=source_video,
        input_sha256="0" * 64,
        timestamp_seconds=0.5,
    )

    with pytest.raises(MediaPreprocessingError) as captured:
        _service(tmp_path).execute(request)

    assert captured.value.code == "input_sha256_mismatch"
    assert not (tmp_path / "derivatives").exists()


def test_frame_extract_produces_png_with_parent_provenance(
    source_video: Path,
    tmp_path: Path,
) -> None:
    parent_sha = _sha256(source_video)
    request = MediaPreprocessRequest(
        operation=MediaOperation.FRAME_EXTRACT,
        input_path=source_video,
        input_sha256=parent_sha,
        timestamp_seconds=0.5,
    )

    result = _service(tmp_path).execute(request)

    assert result.output_path.suffix == ".png"
    assert result.output_sha256 == _sha256(result.output_path)
    assert result.parent_sha256 == parent_sha
    assert (result.width, result.height, result.mime_type) == (160, 90, "image/png")
    provenance = json.loads(result.provenance_path.read_text("utf-8"))
    assert provenance["operation"] == "frame_extract"
    assert provenance["parameters"] == {"timestamp_seconds": 0.5}
    assert provenance["parent"]["sha256"] == parent_sha
    assert provenance["derivative"]["sha256"] == result.output_sha256


@pytest.mark.parametrize(
    ("operation", "extension", "mime_type"),
    [
        (MediaOperation.VIDEO_TRIM, ".mp4", "video/mp4"),
        (MediaOperation.AUDIO_TRIM, ".m4a", "audio/mp4"),
    ],
)
def test_trim_operations_preserve_declared_range(
    operation: MediaOperation,
    extension: str,
    mime_type: str,
    source_video: Path,
    tmp_path: Path,
) -> None:
    request = MediaPreprocessRequest(
        operation=operation,
        input_path=source_video,
        input_sha256=_sha256(source_video),
        start_seconds=0.25,
        duration_seconds=0.75,
    )

    result = _service(tmp_path / operation.value).execute(request)

    assert result.output_path.suffix == extension
    assert result.mime_type == mime_type
    assert result.duration_seconds == pytest.approx(0.75, abs=0.2)
    assert result.applied_parameters == {"start_seconds": 0.25, "duration_seconds": 0.75}
    if operation is MediaOperation.AUDIO_TRIM:
        assert result.video_codec is None
        assert result.audio_codec == "aac"
    else:
        assert result.video_codec == "h264"


def test_scale_pad_outputs_exact_declared_canvas(source_video: Path, tmp_path: Path) -> None:
    request = MediaPreprocessRequest(
        operation=MediaOperation.SCALE_PAD,
        input_path=source_video,
        input_sha256=_sha256(source_video),
        width=200,
        height=200,
        pad_color="black",
    )

    result = _service(tmp_path).execute(request)

    assert (result.width, result.height) == (200, 200)
    assert result.applied_parameters == {"width": 200, "height": 200, "pad_color": "black"}
    assert result.video_codec == "h264"


def test_trim_range_outside_parent_is_rejected(source_video: Path, tmp_path: Path) -> None:
    request = MediaPreprocessRequest(
        operation=MediaOperation.VIDEO_TRIM,
        input_path=source_video,
        input_sha256=_sha256(source_video),
        start_seconds=1.5,
        duration_seconds=1.0,
    )

    with pytest.raises(MediaPreprocessingError) as captured:
        _service(tmp_path).execute(request)

    assert captured.value.code == "time_range_out_of_bounds"
    assert not (tmp_path / "derivatives").exists()


def test_package_exports_only_public_contract() -> None:
    assert set(package.__all__) == {
        "MediaOperation",
        "MediaPreprocessingConfig",
        "MediaPreprocessingError",
        "MediaPreprocessingResult",
        "MediaPreprocessingService",
        "MediaPreprocessRequest",
    }
