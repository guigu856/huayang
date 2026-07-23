from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from components.audio_analysis import AudioAnalysisResult, AudioAnalysisService
from components.video_editor.media import probe_media

from .files import sha256_file, write_json
from .scenario import ScenarioDefinition, SourceClipDefinition


class ScenarioMediaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedVisualMaterial:
    asset_id: str
    name: str
    path: Path
    sha256: str
    duration_us: int
    width: int
    height: int
    content_summary: str
    selection_traits: tuple[str, ...]
    source_path: Path
    source_sha256: str
    source_start_us: int
    provenance_path: Path


@dataclass(frozen=True, slots=True)
class ResolvedBgm:
    asset_id: str
    name: str
    path: Path
    sha256: str
    duration_us: int
    provenance_path: Path
    provider: str
    creator: str
    source_url: str
    license_record: str
    mood_traits: tuple[str, ...]
    tempo_candidates_bpm: tuple[float, ...]
    beat_grid_us: tuple[int, ...]
    sections: tuple[tuple[int, int, float], ...]
    analysis: AudioAnalysisResult


class ScenarioMediaResolver:
    def __init__(
        self,
        project_root: Path,
        *,
        ffmpeg_binary: str = "ffmpeg",
        audio_analysis: AudioAnalysisService | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.ffmpeg_binary = ffmpeg_binary
        self.audio_analysis = audio_analysis or AudioAnalysisService()

    def resolve_visuals(
        self,
        scenario: ScenarioDefinition,
        output_dir: Path,
    ) -> list[ResolvedVisualMaterial]:
        materials = [
            self._extract_visual(scenario, source, output_dir) for source in scenario.source_clips
        ]
        derived_hashes = {material.sha256 for material in materials}
        if len(derived_hashes) < scenario.minimum_distinct_visual_assets:
            raise ScenarioMediaError("派生视觉素材的独立 SHA-256 数量未达到场景门槛")
        if len({material.source_sha256 for material in materials}) < 3:
            raise ScenarioMediaError("真实验证场景必须覆盖三支不同的用户视频来源")
        return materials

    def resolve_bgm(
        self,
        scenario: ScenarioDefinition,
        output_dir: Path,
    ) -> ResolvedBgm:
        source = self._resolve_project_path(scenario.bgm.path)
        provenance_path = self._resolve_project_path(scenario.bgm.provenance_path)
        if not source.is_file() or not provenance_path.is_file():
            raise ScenarioMediaError("场景指定的 Mixkit BGM 或溯源记录不存在")
        digest = sha256_file(source)
        if digest != scenario.bgm.expected_sha256:
            raise ScenarioMediaError("场景 BGM 的 SHA-256 与固定定义不一致")
        metadata = probe_media(source)
        if metadata.duration is None or metadata.width is not None:
            raise ScenarioMediaError("场景 BGM 的媒体类型或时长探测结果无效")
        analysis = self.audio_analysis.analyze(source, output_dir / "audio_analysis")
        tempo_payload = _read_json_object(analysis.tempo_candidates_path)
        beat_payload = _read_json_object(analysis.beat_grid_path)
        section_payload = _read_json_object(analysis.section_candidates_path)
        tempo_candidates = tuple(
            float(candidate["bpm"])
            for candidate in _object_list(tempo_payload, "candidates")
            if isinstance(candidate.get("bpm"), (float, int))
        )
        beats = tuple(
            sorted(
                {
                    int(beat["timestamp_us"])
                    for beat in _object_list(beat_payload, "beats")
                    if isinstance(beat.get("timestamp_us"), int)
                    and 0 <= int(beat["timestamp_us"]) <= scenario.duration_us
                }
            )
        )
        if not tempo_candidates or len(beats) < scenario.main_shot_count - 1:
            raise ScenarioMediaError("真实 BGM 分析未产生足够的速度候选和节拍候选")
        raw_sections = _object_list(section_payload, "sections")
        sections: list[tuple[int, int, float]] = []
        for index, section in enumerate(raw_sections):
            start = int(section.get("start_timestamp_us", 0))
            end = int(section.get("end_timestamp_us", scenario.duration_us))
            if index == 0:
                start = 0
            if index == len(raw_sections) - 1:
                end = scenario.duration_us
            start = max(0, min(start, scenario.duration_us - 1))
            end = max(start + 1, min(end, scenario.duration_us))
            sections.append((start, end, float(section.get("mean_normalized_energy", 0.5))))
        if not sections:
            sections = [(0, scenario.duration_us, 0.5)]
        provenance = _read_json_object(provenance_path)
        return ResolvedBgm(
            asset_id=scenario.bgm.asset_id,
            name=str(provenance.get("title") or source.stem),
            path=source,
            sha256=digest,
            duration_us=round(metadata.duration * 1_000_000),
            provenance_path=provenance_path,
            provider=str(provenance.get("provider") or "mixkit"),
            creator=str(provenance.get("creator") or "Mixkit contributor"),
            source_url=str(provenance.get("source_url") or "https://mixkit.co/free-stock-music/"),
            license_record=str(
                cast(dict[str, Any], provenance.get("rights_record") or {}).get(
                    "provider_label", "Mixkit Stock Music Free License"
                )
            ),
            mood_traits=tuple(scenario.bgm.mood_traits),
            tempo_candidates_bpm=tempo_candidates,
            beat_grid_us=beats,
            sections=tuple(sections),
            analysis=analysis,
        )

    def _extract_visual(
        self,
        scenario: ScenarioDefinition,
        definition: SourceClipDefinition,
        output_dir: Path,
    ) -> ResolvedVisualMaterial:
        source = Path(definition.source_path).expanduser().resolve()
        if not source.is_file():
            raise ScenarioMediaError(f"用户视频来源不存在：{source}")
        source_sha256 = sha256_file(source)
        if source_sha256 in scenario.forbidden_source_sha256s:
            raise ScenarioMediaError("场景素材命中了禁用参考视频 SHA-256")
        destination = output_dir / "visuals" / f"{definition.asset_id}.mp4"
        filtergraph = (
            f"scale={scenario.canvas.width}:{scenario.canvas.height}:"
            "force_original_aspect_ratio=increase,"
            f"crop={scenario.canvas.width}:{scenario.canvas.height},"
            f"fps={scenario.canvas.fps:.12g},setsar=1,format=yuv420p"
        )
        argv = [
            self.ffmpeg_binary,
            "-v",
            "error",
            "-y",
            "-ss",
            f"{definition.source_start_us / 1_000_000:.6f}",
            "-i",
            str(source),
            "-t",
            f"{definition.extract_duration_us / 1_000_000:.6f}",
            "-map",
            "0:v:0",
            "-map_metadata",
            "-1",
            "-an",
            "-vf",
            filtergraph,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-threads",
            "1",
            "-movflags",
            "+faststart",
            str(destination),
        ]
        destination.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        if completed.returncode != 0 or not destination.is_file():
            raise ScenarioMediaError(f"视觉素材派生失败：{completed.stderr[-2000:]}")
        metadata = probe_media(destination)
        if (
            metadata.duration is None
            or metadata.width != scenario.canvas.width
            or metadata.height != scenario.canvas.height
        ):
            raise ScenarioMediaError("派生视觉素材的媒体元数据与场景定义不一致")
        duration_us = round(metadata.duration * 1_000_000)
        if duration_us + 70_000 < definition.extract_duration_us:
            raise ScenarioMediaError("派生视觉素材的实际时长短于声明区间")
        provenance_path = output_dir / "provenance" / f"{definition.asset_id}.json"
        digest = sha256_file(destination)
        write_json(
            provenance_path,
            {
                "schema_version": "1.0",
                "scenario_id": scenario.scenario_id,
                "relation": "time_range_and_canvas_derivative",
                "source": {
                    "path": str(source),
                    "sha256": source_sha256,
                    "start_us": definition.source_start_us,
                    "duration_us": definition.extract_duration_us,
                },
                "derivative": {
                    "path": str(destination.resolve()),
                    "sha256": digest,
                    "duration_us": duration_us,
                    "width": metadata.width,
                    "height": metadata.height,
                    "fps": metadata.frame_rate,
                },
                "transform": {
                    "filtergraph": filtergraph,
                    "video_codec": "libx264",
                    "audio_removed": True,
                    "preset": "medium",
                    "crf": 18,
                    "threads": 1,
                    "source_metadata_removed": True,
                },
            },
        )
        return ResolvedVisualMaterial(
            asset_id=definition.asset_id,
            name=definition.name,
            path=destination.resolve(),
            sha256=digest,
            duration_us=duration_us,
            width=metadata.width,
            height=metadata.height,
            content_summary=definition.content_summary,
            selection_traits=tuple(definition.selection_traits),
            source_path=source,
            source_sha256=source_sha256,
            source_start_us=definition.source_start_us,
            provenance_path=provenance_path.resolve(),
        )

    def _resolve_project_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return (path if path.is_absolute() else self.project_root / path).resolve()


def _read_json_object(path: Path) -> dict[str, Any]:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ScenarioMediaError(f"JSON 产物不是 object：{path}")
    return cast(dict[str, Any], payload)


def _object_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [cast(dict[str, Any], item) for item in value if isinstance(item, dict)]
