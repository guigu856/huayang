from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from components.video_editor.errors import VideoEditorError
from components.video_editor.jobs import PersistentRenderQueue
from components.video_editor.models import (
    Asset,
    Canvas,
    Clip,
    EditorProject,
    MediaMetadata,
    Track,
    Transform,
)
from components.video_editor.render import FFmpegRenderer, compile_render_plan


def _system_cjk_font() -> Path | None:
    return next(
        (
            path
            for path in (
                Path("C:/Windows/Fonts/msyh.ttc"),
                Path("/System/Library/Fonts/PingFang.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            )
            if path.is_file()
        ),
        None,
    )


def _project(project_dir: Path) -> EditorProject:
    assets_dir = project_dir / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "main.mp4").write_bytes(b"video")
    (assets_dir / "logo.png").write_bytes(b"image")
    (assets_dir / "music.wav").write_bytes(b"audio")
    return EditorProject(
        id="project_0123456789abcdef",
        name="渲染测试",
        canvas=Canvas(width=1280, height=720, fps=25),
        assets=[
            Asset(
                id="asset_video",
                kind="video",
                name="主视频",
                path="assets/main.mp4",
                metadata=MediaMetadata(
                    duration=10,
                    width=1920,
                    height=1080,
                    video_codec="h264",
                    audio_codec="aac",
                ),
            ),
            Asset(
                id="asset_image",
                kind="image",
                name="角标",
                path="assets/logo.png",
                metadata=MediaMetadata(width=200, height=100),
            ),
            Asset(
                id="asset_audio",
                kind="audio",
                name="配乐",
                path="assets/music.wav",
                metadata=MediaMetadata(duration=8, audio_codec="pcm_s16le"),
            ),
        ],
        tracks=[
            Track(
                id="track_text",
                media_domain="visual",
                name="字幕",
                clips=[
                    Clip(
                        id="clip_text",
                        kind="text",
                        timeline_start=0,
                        duration=1.5,
                        source_in=0,
                        text="你好：FFmpeg",
                        transform=Transform(x=100, y=600, opacity=0.9),
                    )
                ],
            ),
            Track(
                id="track_overlay",
                media_domain="visual",
                name="叠加",
                clips=[
                    Clip(
                        id="clip_overlay",
                        kind="media",
                        timeline_start=0.5,
                        duration=1,
                        source_in=0,
                        asset_id="asset_image",
                        transform=Transform(
                            x=20,
                            y=30,
                            width=200,
                            height=100,
                            opacity=0.7,
                        ),
                    )
                ],
            ),
            Track(
                id="track_video",
                media_domain="visual",
                name="主轨",
                clips=[
                    Clip(
                        id="clip_video",
                        kind="media",
                        timeline_start=1,
                        duration=2,
                        source_in=3,
                        asset_id="asset_video",
                        transform=Transform(width=1280, height=720),
                        volume=0.8,
                    )
                ],
            ),
            Track(
                id="track_audio",
                media_domain="audio",
                name="配乐",
                clips=[
                    Clip(
                        id="clip_audio",
                        kind="media",
                        timeline_start=2,
                        duration=2,
                        source_in=1,
                        asset_id="asset_audio",
                        volume=0.5,
                    )
                ],
            ),
        ],
    )


def test_compile_render_plan_uses_timestamp_safe_filters_and_explicit_mix(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project_0123456789abcdef"
    project = _project(project_dir)
    output_path = project_dir / "renders" / "result.mp4"

    plan = compile_render_plan(project, project_dir, output_path)

    assert "trim=start=3:end=5,setpts=PTS-STARTPTS+1/TB" in plan.filtergraph
    assert "trim=start=0:end=1,setpts=PTS-STARTPTS+0.5/TB" in plan.filtergraph
    assert "eof_action=pass:shortest=0:repeatlast=0" in plan.filtergraph
    assert "atrim=start=1:end=3,asetpts=PTS-STARTPTS+2/TB" in plan.filtergraph
    assert "amix=inputs=" in plan.filtergraph
    assert "duration=longest:dropout_transition=0:normalize=0" in plan.filtergraph
    assert "drawtext=" in plan.filtergraph
    assert "expansion=none" in plan.filtergraph
    assert "textfile=" in plan.filtergraph
    assert "x=(w-text_w)/2" in plan.filtergraph
    assert "y=(h-text_h)/2" in plan.filtergraph
    assert "overlay=x=100+(1920-overlay_w)/2:y=600+(1080-overlay_h)/2" in plan.filtergraph
    assert plan.support_files[0].content == "你好：FFmpeg"
    assert plan.argv[0] == "ffmpeg"
    assert "-filter_complex" in plan.argv
    assert plan.argv[-1] == str(output_path)
    assert plan.duration == 4


def test_compile_text_rotation_uses_centered_transparent_layer(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project_0123456789abcdef"
    project = _project(project_dir)
    text_clip = project.tracks[0].clips[0]
    text_clip.timeline_start = 0.25
    text_clip.duration = 1.25
    text_clip.transform = Transform(
        x=100,
        y=200,
        width=320,
        height=120,
        rotation=30,
        opacity=0.75,
    )

    plan = compile_render_plan(project, project_dir, project_dir / "result.mp4")

    assert "color=c=black@0.0:s=320x120:r=25:d=1.25,format=rgba" in plan.filtergraph
    assert "fontcolor=white@0.75" in plan.filtergraph
    assert "rotate=30*PI/180:c=none:ow=rotw(iw):oh=roth(ih)" in plan.filtergraph
    assert "setpts=PTS-STARTPTS+0.25/TB[tclip0]" in plan.filtergraph
    assert (
        "[vbase2][tclip0]overlay="
        "x=100+(320-overlay_w)/2:y=200+(120-overlay_h)/2:"
        "eof_action=pass:shortest=0:repeatlast=0[vtext0]"
    ) in plan.filtergraph


def test_compile_visual_layers_follow_top_to_bottom_ui_order_with_interleaved_audio(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project_0123456789abcdef"
    project = _project(project_dir)
    audio_track = project.tracks.pop()
    project.tracks.insert(1, audio_track)
    overlay_track = next(track for track in project.tracks if track.id == "track_overlay")
    overlay_track.clips.append(
        Clip(
            id="clip_overlay_second",
            kind="media",
            timeline_start=2,
            duration=1,
            asset_id="asset_image",
            transform=Transform(x=300, y=40, width=200, height=100),
        )
    )

    plan = compile_render_plan(project, project_dir, project_dir / "result.mp4")

    main_position = plan.filtergraph.index("trim=start=3:end=5")
    first_overlay_position = plan.filtergraph.index("overlay=x=20:y=30")
    second_overlay_position = plan.filtergraph.index("overlay=x=300:y=40")
    text_position = plan.filtergraph.index("drawtext=")
    assert main_position < first_overlay_position < second_overlay_position < text_position


def test_compile_render_plan_rejects_asset_path_outside_project(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project_0123456789abcdef"
    project = _project(project_dir)
    project.assets[0].path = "../outside.mp4"

    with pytest.raises(VideoEditorError) as captured:
        compile_render_plan(project, project_dir, project_dir / "output.mp4")

    assert captured.value.code == "invalid_asset_path"


def test_compile_splits_reused_asset_streams_before_filtering(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_0123456789abcdef"
    project = _project(project_dir)
    project.tracks[2].clips.append(
        Clip(
            id="clip_video_reused",
            kind="media",
            timeline_start=4,
            duration=2,
            source_in=5,
            asset_id="asset_video",
            transform=Transform(width=1280, height=720),
        )
    )

    plan = compile_render_plan(project, project_dir, project_dir / "result.mp4")

    assert "[1:v]split=2[vsrc1_0][vsrc1_1]" in plan.filtergraph
    assert "[1:a]asplit=2[asrc1_0][asrc1_1]" in plan.filtergraph
    assert "[vsrc1_0]trim=start=3:end=5" in plan.filtergraph
    assert "[vsrc1_1]trim=start=5:end=7" in plan.filtergraph
    assert "[asrc1_0]atrim=start=3:end=5" in plan.filtergraph
    assert "[asrc1_1]atrim=start=5:end=7" in plan.filtergraph


def test_compile_uses_configured_subtitle_font(tmp_path: Path, monkeypatch) -> None:
    project_dir = tmp_path / "project_0123456789abcdef"
    project = _project(project_dir)
    font = tmp_path / "font.ttc"
    font.write_bytes(b"font")
    monkeypatch.setenv("VIDEO_EDITOR_FONT_PATH", str(font))

    plan = compile_render_plan(project, project_dir, project_dir / "result.mp4")

    assert "fontfile=" in plan.filtergraph
    assert "font.ttc" in plan.filtergraph


def test_renderer_verifies_temporary_output_before_atomic_replace(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project_0123456789abcdef"
    project = _project(project_dir)
    output_path = project_dir / "renders" / "result.mp4"
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"old-output")
    observed_temporary_paths: list[Path] = []

    class SuccessfulProcess:
        returncode = 0

        def __init__(self, argv: list[str]) -> None:
            self.output_path = Path(argv[-1])
            observed_temporary_paths.append(self.output_path)
            self.output_path.write_bytes(b"new-output")

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "", ""

        def terminate(self) -> None:
            raise AssertionError("successful render must not be terminated")

        def kill(self) -> None:
            raise AssertionError("successful render must not be killed")

    def popen(argv: list[str], **_kwargs: object) -> SuccessfulProcess:
        return SuccessfulProcess(argv)

    renderer = FFmpegRenderer(
        process_factory=popen,
        probe=lambda path: MediaMetadata(duration=4, width=1280, height=720),
    )

    result = renderer.render(
        project,
        project_dir=project_dir,
        output_path=output_path,
    )

    assert result == output_path
    assert output_path.read_bytes() == b"new-output"
    assert observed_temporary_paths[0] != output_path
    assert not observed_temporary_paths[0].exists()


def test_renderer_keeps_previous_output_when_verification_fails(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project_0123456789abcdef"
    project = _project(project_dir)
    output_path = project_dir / "renders" / "result.mp4"
    output_path.parent.mkdir(parents=True)
    output_path.write_bytes(b"old-output")
    temporary_paths: list[Path] = []

    class SuccessfulProcess:
        returncode = 0

        def __init__(self, argv: list[str]) -> None:
            temporary = Path(argv[-1])
            temporary.write_bytes(b"broken-output")
            temporary_paths.append(temporary)

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "", ""

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

    def popen(argv: list[str], **_kwargs: object) -> SuccessfulProcess:
        return SuccessfulProcess(argv)

    def reject_output(_path: Path) -> MediaMetadata:
        raise VideoEditorError("media_probe_failed", "输出损坏")

    renderer = FFmpegRenderer(process_factory=popen, probe=reject_output)

    with pytest.raises(VideoEditorError) as captured:
        renderer.render(project, project_dir=project_dir, output_path=output_path)

    assert captured.value.code == "render_output_invalid"
    assert output_path.read_bytes() == b"old-output"
    assert not temporary_paths[0].exists()


def test_renderer_reports_ffmpeg_failure_without_publishing_output(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "project_0123456789abcdef"
    project = _project(project_dir)
    output_path = project_dir / "renders" / "result.mp4"

    class FailedProcess:
        returncode = 1

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "", "encoder failed"

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

    def popen(_argv: list[str], **_kwargs: object) -> FailedProcess:
        return FailedProcess()

    renderer = FFmpegRenderer(process_factory=popen)

    with pytest.raises(VideoEditorError) as captured:
        renderer.render(project, project_dir=project_dir, output_path=output_path)

    assert captured.value.code == "render_failed"
    assert captured.value.details["stderr"] == "encoder failed"
    assert not output_path.exists()


def test_render_timeout_kills_process_and_queue_continues(tmp_path: Path) -> None:
    project_dir = tmp_path / "project_0123456789abcdef"
    project = _project(project_dir)
    process_calls = 0
    timed_out_process: HangingProcess | None = None

    class HangingProcess:
        returncode = 0

        def __init__(self) -> None:
            self.terminated = False
            self.killed = False

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if self.killed:
                return "", ""
            raise subprocess.TimeoutExpired("ffmpeg", timeout or 0)

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    class SuccessfulProcess:
        returncode = 0

        def __init__(self, argv: list[str]) -> None:
            Path(argv[-1]).write_bytes(b"rendered")

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "", ""

        def terminate(self) -> None:
            raise AssertionError("successful render must not be terminated")

        def kill(self) -> None:
            raise AssertionError("successful render must not be killed")

    def popen(argv: list[str], **_kwargs: object) -> HangingProcess | SuccessfulProcess:
        nonlocal process_calls, timed_out_process
        process_calls += 1
        if process_calls == 1:
            timed_out_process = HangingProcess()
            return timed_out_process
        return SuccessfulProcess(argv)

    times = iter([0.0, 1.0, 10.0, 10.0])
    renderer = FFmpegRenderer(
        process_factory=popen,
        probe=lambda _path: MediaMetadata(duration=4, width=1280, height=720),
        render_timeout_seconds=1,
        monotonic=lambda: next(times),
    )
    queue = PersistentRenderQueue(tmp_path / "jobs", renderer)
    first = queue.submit(project, project_dir=project_dir)
    second = queue.submit(project, project_dir=project_dir)
    queue.start()

    failed = queue.wait(first.id, timeout=2)
    succeeded = queue.wait(second.id, timeout=2)
    queue.stop()

    assert failed.status == "failed"
    assert failed.error == {
        "code": "render_timeout",
        "message": "FFmpeg 渲染超时",
        "details": {"timeout_seconds": 1.0},
    }
    assert succeeded.status == "succeeded"
    assert timed_out_process is not None
    assert timed_out_process.terminated is True
    assert timed_out_process.killed is True


def test_real_ffmpeg_renders_utf8_textfile_with_filter_metacharacters(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    font = _system_cjk_font()
    if ffmpeg is None or ffprobe is None or font is None:
        pytest.skip("需要本机 FFmpeg、ffprobe 和可用字体")

    project = EditorProject(
        id="project_0123456789abcdef",
        name="真实字幕渲染",
        canvas=Canvas(width=320, height=180, fps=12),
        tracks=[
            Track(
                id="track_text",
                media_domain="visual",
                name="字幕",
                clips=[
                    Clip(
                        id="clip_text",
                        kind="text",
                        timeline_start=0,
                        duration=0.5,
                        text="中文:他说'好', [OK]; 100%",
                        transform=Transform(x=10, y=80),
                    )
                ],
            )
        ],
    )
    project_dir = tmp_path / project.id
    output_path = project_dir / "renders" / "result.mp4"

    result = FFmpegRenderer(
        ffmpeg_binary=ffmpeg,
        ffprobe_binary=ffprobe,
    ).render(project, project_dir=project_dir, output_path=output_path)

    assert result == output_path
    assert output_path.stat().st_size > 0
    assert list(output_path.parent.glob("*.text-*.txt")) == []


def test_real_ffmpeg_rotates_text_around_transform_center(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    font = _system_cjk_font()
    if ffmpeg is None or ffprobe is None or font is None:
        pytest.skip("需要本机 FFmpeg、ffprobe 和可用字体")

    project = EditorProject(
        id="project_0123456789abcdef",
        name="真实旋转字幕渲染",
        canvas=Canvas(width=320, height=180, fps=12),
        tracks=[
            Track(
                id="track_text",
                media_domain="visual",
                name="字幕",
                clips=[
                    Clip(
                        id="clip_text",
                        kind="text",
                        timeline_start=0,
                        duration=0.5,
                        text="中文",
                        transform=Transform(
                            x=100,
                            y=50,
                            width=120,
                            height=80,
                            rotation=90,
                        ),
                    )
                ],
            )
        ],
    )
    project_dir = tmp_path / project.id
    output_path = project_dir / "renders" / "rotated.mp4"

    FFmpegRenderer(
        ffmpeg_binary=ffmpeg,
        ffprobe_binary=ffprobe,
        font_path=font,
    ).render(project, project_dir=project_dir, output_path=output_path)

    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert probe.stdout.strip() == "320,180"

    frame = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            "0.2",
            "-i",
            str(output_path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert len(frame) == 320 * 180 * 3
    bright_pixels = [
        (index % 320, index // 320)
        for index in range(320 * 180)
        if max(frame[index * 3 : index * 3 + 3]) >= 96
    ]
    assert bright_pixels
    xs = [point[0] for point in bright_pixels]
    ys = [point[1] for point in bright_pixels]
    width = max(xs) - min(xs) + 1
    height = max(ys) - min(ys) + 1
    assert height > width
    assert abs((min(xs) + max(xs)) / 2 - 160) <= 4
    assert abs((min(ys) + max(ys)) / 2 - 90) <= 4
