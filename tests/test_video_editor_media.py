from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from components.video_editor.errors import VideoEditorError
from components.video_editor.media import (
    import_media,
    probe_media,
    resolve_media_path,
)
from components.video_editor.models import MediaMetadata


class RecordingStream(io.BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


def test_import_media_streams_to_a_generated_project_scoped_path(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project_0123456789abcdef"
    source = RecordingStream(b"0123456789")

    asset = import_media(
        project_dir,
        "../unsafe/片段.mp4",
        source,
        chunk_size=4,
        probe=lambda _path: MediaMetadata(
            duration=2.5,
            width=640,
            height=360,
            frame_rate=25,
            video_codec="h264",
            audio_codec="aac",
            sample_rate=48_000,
            channels=2,
        ),
    )

    assert asset.kind == "video"
    assert asset.name == "片段.mp4"
    assert asset.path.startswith("assets/")
    assert asset.path.endswith(".mp4")
    assert resolve_media_path(project_dir, asset.path).read_bytes() == b"0123456789"
    assert source.read_sizes == [4, 4, 4, 4]


def test_import_media_removes_partial_file_when_size_limit_is_exceeded(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project_0123456789abcdef"

    with pytest.raises(VideoEditorError) as captured:
        import_media(
            project_dir,
            "large.mp4",
            io.BytesIO(b"123456"),
            chunk_size=2,
            max_bytes=5,
            probe=lambda _path: MediaMetadata(
                duration=1,
                width=10,
                height=10,
            ),
        )

    assert captured.value.code == "media_too_large"
    assert list((project_dir / "assets").glob("*")) == []


def test_resolve_media_path_rejects_paths_outside_the_project(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_0123456789abcdef"

    with pytest.raises(VideoEditorError) as captured:
        resolve_media_path(project_dir, "../outside.mp4")

    assert captured.value.code == "invalid_asset_path"


def test_probe_media_parses_video_audio_and_rational_frame_rate(
    tmp_path: Path,
) -> None:
    media_path = tmp_path / "sample.mp4"
    media_path.write_bytes(b"sample")
    observed: list[str] = []

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.extend(argv)
        payload = {
            "format": {"duration": "3.25"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30000/1001",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "sample_rate": "48000",
                    "channels": 2,
                },
            ],
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    metadata = probe_media(media_path, runner=runner)

    assert metadata.duration == 3.25
    assert metadata.width == 1920
    assert metadata.height == 1080
    assert metadata.frame_rate == pytest.approx(29.97002997)
    assert metadata.video_codec == "h264"
    assert metadata.audio_codec == "aac"
    assert metadata.sample_rate == 48_000
    assert metadata.channels == 2
    assert observed[0] == "ffprobe"
    assert observed[-1] == str(media_path)


def test_probe_media_rejects_payload_without_audio_or_video_stream(
    tmp_path: Path,
) -> None:
    media_path = tmp_path / "unsupported.bin"
    media_path.write_bytes(b"sample")

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        payload = {"format": {}, "streams": [{"codec_type": "subtitle"}]}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    with pytest.raises(VideoEditorError) as captured:
        probe_media(media_path, runner=runner)

    assert captured.value.code == "unsupported_media"
