from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from .errors import VideoEditorError
from .media import probe_media, resolve_media_path
from .models import EditorProject, MediaMetadata


class RenderProcess(Protocol):
    returncode: int

    def communicate(self, timeout: float | None = None) -> tuple[str, str]: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[..., RenderProcess]


@dataclass(frozen=True, slots=True)
class RenderPlan:
    argv: tuple[str, ...]
    filtergraph: str
    output_path: Path
    duration: float
    support_files: tuple[RenderSupportFile, ...]


@dataclass(frozen=True, slots=True)
class RenderSupportFile:
    path: Path
    content: str


def compile_render_plan(
    project: EditorProject,
    project_dir: Path | str,
    output_path: Path | str,
    *,
    ffmpeg_binary: str = "ffmpeg",
    font_path: Path | str | None = None,
) -> RenderPlan:
    """把工程快照纯编译为 FFmpeg 参数和 filtergraph。"""

    duration = _project_duration(project)
    resolved_font = _resolve_font(font_path)
    assets = {asset.id: asset for asset in project.assets}
    referenced_ids = {
        clip.asset_id
        for track in project.tracks
        for clip in track.clips
        if clip.asset_id is not None
    }
    ordered_assets = [asset for asset in project.assets if asset.id in referenced_ids]
    input_indexes = {asset.id: index + 1 for index, asset in enumerate(ordered_assets)}

    visual_uses = Counter(
        clip.asset_id
        for track in project.tracks
        if track.media_domain == "visual"
        for clip in track.clips
        if clip.kind == "media" and clip.asset_id is not None
    )
    audio_uses = Counter(
        clip.asset_id
        for track in project.tracks
        for clip in track.clips
        if clip.kind == "media" and clip.asset_id is not None
        and (
            track.media_domain == "audio"
            or (
                track.media_domain == "visual"
                and assets[clip.asset_id].metadata.audio_codec is not None
            )
        )
    )

    argv = [
        ffmpeg_binary,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        (
            f"color=c=0x{project.canvas.background_color[1:]}:"
            f"s={project.canvas.width}x{project.canvas.height}:"
            f"r={_number(project.canvas.fps)}:d={_number(duration)}"
        ),
    ]
    for asset in ordered_assets:
        path = resolve_media_path(project_dir, asset.path)
        if asset.kind == "image":
            argv.extend(["-loop", "1"])
        argv.extend(["-i", str(path)])

    graph = ["[0:v]setpts=PTS-STARTPTS,format=rgba[vbase0]"]
    support_files: list[RenderSupportFile] = []
    visual_sources: dict[str, list[str]] = {}
    audio_sources: dict[str, list[str]] = {}
    for asset in ordered_assets:
        input_index = input_indexes[asset.id]
        visual_count = visual_uses[asset.id]
        if visual_count == 1:
            visual_sources[asset.id] = [f"{input_index}:v"]
        elif visual_count > 1:
            labels = [f"vsrc{input_index}_{index}" for index in range(visual_count)]
            graph.append(
                f"[{input_index}:v]split={visual_count}"
                + "".join(f"[{label}]" for label in labels)
            )
            visual_sources[asset.id] = labels
        audio_count = audio_uses[asset.id]
        if audio_count == 1:
            audio_sources[asset.id] = [f"{input_index}:a"]
        elif audio_count > 1:
            labels = [f"asrc{input_index}_{index}" for index in range(audio_count)]
            graph.append(
                f"[{input_index}:a]asplit={audio_count}"
                + "".join(f"[{label}]" for label in labels)
            )
            audio_sources[asset.id] = labels

    visual_source_offsets: Counter[str] = Counter()
    audio_source_offsets: Counter[str] = Counter()
    current_video = "vbase0"
    visual_index = 0
    text_index = 0
    for track in reversed(project.tracks):
        if track.media_domain != "visual":
            continue
        for clip in track.clips:
            if clip.kind == "media":
                if clip.asset_id is None:
                    continue
                asset = assets[clip.asset_id]
                source_offset = visual_source_offsets[asset.id]
                source_label = visual_sources[asset.id][source_offset]
                visual_source_offsets[asset.id] += 1
                source_end = clip.source_in + clip.duration
                branch = [
                    f"trim=start={_number(clip.source_in)}:end={_number(source_end)}",
                    (
                        "setpts=PTS-STARTPTS+"
                        f"{_number(clip.timeline_start)}/TB"
                    ),
                    (
                        f"scale={_number(clip.transform.width)}:"
                        f"{_number(clip.transform.height)}"
                    ),
                ]
                if clip.transform.rotation != 0:
                    branch.append(
                        "rotate="
                        f"{_number(clip.transform.rotation)}*PI/180:"
                        "c=none:ow=rotw(iw):oh=roth(ih)"
                    )
                branch.append("format=rgba")
                if clip.transform.opacity != 1:
                    branch.append(
                        f"colorchannelmixer=aa={_number(clip.transform.opacity)}"
                    )
                clip_label = f"vclip{visual_index}"
                graph.append(f"[{source_label}]{','.join(branch)}[{clip_label}]")
                next_video = f"vbase{visual_index + 1}"
                graph.append(
                    f"[{current_video}][{clip_label}]overlay="
                    f"x={_number(clip.transform.x)}:y={_number(clip.transform.y)}:"
                    "eof_action=pass:shortest=0:repeatlast=0"
                    f"[{next_video}]"
                )
                current_video = next_video
                visual_index += 1
            else:
                if clip.text is None:
                    continue
                next_video = f"vtext{text_index}"
                text_label = f"tclip{text_index}"
                text_path = Path(f"{Path(output_path)}.text-{text_index}.txt")
                support_files.append(RenderSupportFile(text_path, clip.text))
                options = [
                    "expansion=none",
                    f"textfile='{_escape_filter_value(text_path.as_posix())}'",
                    "reload=0",
                    "x=(w-text_w)/2",
                    "y=(h-text_h)/2",
                    "fontsize=48",
                    f"fontcolor=white@{_number(clip.transform.opacity)}",
                ]
                if resolved_font is not None:
                    options.insert(
                        0,
                        f"fontfile='{_escape_filter_value(resolved_font.as_posix())}'",
                    )
                text_branch = [
                    (
                        "color=c=black@0.0:"
                        f"s={_pixel_dimension(clip.transform.width)}x"
                        f"{_pixel_dimension(clip.transform.height)}:"
                        f"r={_number(project.canvas.fps)}:d={_number(clip.duration)}"
                    ),
                    "format=rgba",
                    f"drawtext={':'.join(options)}",
                ]
                if clip.transform.rotation != 0:
                    text_branch.append(
                        "rotate="
                        f"{_number(clip.transform.rotation)}*PI/180:"
                        "c=none:ow=rotw(iw):oh=roth(ih)"
                    )
                text_branch.append(
                    "setpts=PTS-STARTPTS+"
                    f"{_number(clip.timeline_start)}/TB"
                )
                graph.append(f"{','.join(text_branch)}[{text_label}]")
                graph.append(
                    f"[{current_video}][{text_label}]overlay="
                    f"x={_number(clip.transform.x)}+"
                    f"({_number(clip.transform.width)}-overlay_w)/2:"
                    f"y={_number(clip.transform.y)}+"
                    f"({_number(clip.transform.height)}-overlay_h)/2:"
                    "eof_action=pass:shortest=0:repeatlast=0"
                    f"[{next_video}]"
                )
                current_video = next_video
                text_index += 1
    graph.append(f"[{current_video}]format=yuv420p[vout]")

    audio_labels: list[str] = []
    for track in project.tracks:
        for clip in track.clips:
            if clip.kind != "media" or clip.asset_id is None:
                continue
            asset = assets[clip.asset_id]
            if track.media_domain == "audio" or (
                track.media_domain == "visual"
                and asset.metadata.audio_codec is not None
            ):
                label = f"aclip{len(audio_labels)}"
                source_offset = audio_source_offsets[asset.id]
                source_label = audio_sources[asset.id][source_offset]
                audio_source_offsets[asset.id] += 1
                source_end = clip.source_in + clip.duration
                graph.append(
                    f"[{source_label}]"
                    f"atrim=start={_number(clip.source_in)}:end={_number(source_end)},"
                    "asetpts=PTS-STARTPTS+"
                    f"{_number(clip.timeline_start)}/TB,"
                    f"volume={_number(clip.volume)},"
                    "aresample=48000,"
                    "aformat=sample_fmts=fltp:channel_layouts=stereo"
                    f"[{label}]"
                )
                audio_labels.append(label)

    if audio_labels:
        graph.append(
            "anullsrc=r=48000:cl=stereo,"
            f"atrim=duration={_number(duration)},"
            "asetpts=PTS-STARTPTS[asilence]"
        )
        mix_labels = ["asilence", *audio_labels]
        weights = " ".join("1" for _ in mix_labels)
        graph.append(
            "".join(f"[{label}]" for label in mix_labels)
            + f"amix=inputs={len(mix_labels)}:duration=longest:"
            "dropout_transition=0:normalize=0:"
            f"weights='{weights}'[aout]"
        )

    filtergraph = ";".join(graph)
    argv.extend(
        [
            "-filter_complex",
            filtergraph,
            "-map",
            "[vout]",
        ]
    )
    if audio_labels:
        argv.extend(["-map", "[aout]", "-c:a", "aac", "-ar", "48000"])
    else:
        argv.append("-an")
    argv.extend(
        [
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            _number(project.canvas.fps),
            "-movflags",
            "+faststart",
            "-t",
            _number(duration),
            "-progress",
            "pipe:1",
            "-nostats",
            str(Path(output_path)),
        ]
    )
    return RenderPlan(
        argv=tuple(argv),
        filtergraph=filtergraph,
        output_path=Path(output_path),
        duration=duration,
        support_files=tuple(support_files),
    )


class FFmpegRenderer:
    def __init__(
        self,
        *,
        ffmpeg_binary: str = "ffmpeg",
        ffprobe_binary: str = "ffprobe",
        font_path: Path | str | None = None,
        process_factory: ProcessFactory | None = None,
        probe: Callable[[Path], MediaMetadata] | None = None,
        render_timeout_seconds: float = 3600,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if render_timeout_seconds <= 0:
            raise ValueError("render_timeout_seconds 必须大于 0")
        self.ffmpeg_binary = ffmpeg_binary
        self.ffprobe_binary = ffprobe_binary
        self.font_path = Path(font_path) if font_path is not None else None
        self._process_factory: ProcessFactory = process_factory or cast(
            ProcessFactory, subprocess.Popen
        )
        self._probe = probe
        self.render_timeout_seconds = float(render_timeout_seconds)
        self._monotonic = monotonic or time.monotonic

    def render(
        self,
        project: EditorProject,
        *,
        project_dir: Path | str,
        output_path: Path | str,
        cancel_event: threading.Event | None = None,
    ) -> Path:
        final_path = Path(output_path)
        temporary_path = self._temporary_output(final_path)
        plan: RenderPlan | None = None
        try:
            plan = compile_render_plan(
                project,
                project_dir,
                temporary_path,
                ffmpeg_binary=self.ffmpeg_binary,
                font_path=self.font_path,
            )
            self._write_support_files(plan.support_files)
            process = self._start_process(plan)
            _stdout, stderr = self._wait(process, cancel_event)
            if process.returncode != 0:
                raise VideoEditorError(
                    "render_failed",
                    "FFmpeg 渲染失败",
                    details={
                        "returncode": process.returncode,
                        "stderr": (stderr or "")[-4000:],
                    },
                )
            self._verify_output(temporary_path)
            try:
                os.replace(temporary_path, final_path)
            except OSError as error:
                raise VideoEditorError(
                    "output_unavailable", f"渲染结果发布失败：{error}"
                ) from error
            return final_path
        finally:
            temporary_path.unlink(missing_ok=True)
            if plan is not None:
                for support_file in plan.support_files:
                    support_file.path.unlink(missing_ok=True)

    @staticmethod
    def _temporary_output(final_path: Path) -> Path:
        try:
            final_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=f".{final_path.stem}-",
                suffix=f".tmp{final_path.suffix or '.mp4'}",
                dir=final_path.parent,
            )
            os.close(descriptor)
            return Path(name)
        except OSError as error:
            raise VideoEditorError(
                "output_unavailable", f"渲染目录写入失败：{error}"
            ) from error

    def _start_process(self, plan: RenderPlan) -> RenderProcess:
        try:
            return self._process_factory(
                list(plan.argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as error:
            raise VideoEditorError("ffmpeg_unavailable", "未找到 FFmpeg") from error
        except OSError as error:
            raise VideoEditorError("render_failed", f"FFmpeg 启动失败：{error}") from error

    @staticmethod
    def _write_support_files(files: tuple[RenderSupportFile, ...]) -> None:
        try:
            for support_file in files:
                support_file.path.write_text(
                    support_file.content,
                    encoding="utf-8",
                    newline="\n",
                )
        except OSError as error:
            raise VideoEditorError(
                "output_unavailable", f"渲染辅助文件写入失败：{error}"
            ) from error

    def _wait(
        self,
        process: RenderProcess,
        cancel_event: threading.Event | None,
    ) -> tuple[str, str]:
        deadline = self._monotonic() + self.render_timeout_seconds
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self._stop_process(process)
                raise VideoEditorError("render_cancelled", "渲染任务已取消")
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                self._stop_process(process)
                raise VideoEditorError(
                    "render_timeout",
                    "FFmpeg 渲染超时",
                    details={"timeout_seconds": self.render_timeout_seconds},
                )
            try:
                return process.communicate(timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                continue

    @staticmethod
    def _stop_process(process: RenderProcess) -> None:
        process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()

    def _verify_output(self, path: Path) -> None:
        try:
            metadata = (
                self._probe(path)
                if self._probe is not None
                else probe_media(path, ffprobe_binary=self.ffprobe_binary)
            )
        except VideoEditorError as error:
            raise VideoEditorError(
                "render_output_invalid",
                "渲染结果校验失败",
                details={"probe_error": error.code},
            ) from error
        if metadata.duration is None or metadata.width is None:
            raise VideoEditorError("render_output_invalid", "渲染结果缺少有效视频流")


def _project_duration(project: EditorProject) -> float:
    duration = max(
        (
            clip.timeline_start + clip.duration
            for track in project.tracks
            for clip in track.clips
        ),
        default=0,
    )
    if duration <= 0:
        raise VideoEditorError("render_empty", "工程没有可渲染片段")
    return duration


def _resolve_font(font_path: Path | str | None) -> Path | None:
    if font_path is None:
        configured = os.environ.get("VIDEO_EDITOR_FONT_PATH")
        if configured:
            font_path = configured
        else:
            candidates = (
                Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc",
                Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/simhei.ttf",
                Path("/System/Library/Fonts/PingFang.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-VF.otf.ttc"),
                Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            )
            return next(
                (candidate.resolve() for candidate in candidates if candidate.is_file()),
                None,
            )
    resolved = Path(font_path).resolve()
    if not resolved.is_file():
        raise VideoEditorError("font_not_found", "字幕字体文件不存在")
    return resolved


def _number(value: int | float) -> str:
    return format(value, ".12g")


def _pixel_dimension(value: float) -> int:
    return max(1, int(value + 0.5))


def _escape_filter_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in ("'", ":", ",", "[", "]", ";"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped
