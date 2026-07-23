from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from components.video_editor import MediaMetadata
from video_create_plugin.editing import (
    ActionSpec,
    CanvasSpec,
    EditingSpecification,
    ExecutionCompiler,
    MaterialAsset,
    ShotSpec,
    StaticTransform,
    TimeRange,
    preflight_spec,
)
from video_create_plugin.errors import PluginError


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _spec(tmp_path: Path, *, extra_capability: str | None = None) -> EditingSpecification:
    video_a = tmp_path / "a.mp4"
    video_b = tmp_path / "b.mp4"
    audio = tmp_path / "bgm.mp3"
    video_a.write_bytes(b"video-a")
    video_b.write_bytes(b"video-b")
    audio.write_bytes(b"audio")
    assets = [
        MaterialAsset(
            asset_id="material_a",
            kind="video",
            name="A",
            path=str(video_a),
            sha256=_digest(video_a),
            duration_us=3_000_000,
            width=1280,
            height=720,
            provenance_ref="provenance:a",
        ),
        MaterialAsset(
            asset_id="material_b",
            kind="video",
            name="B",
            path=str(video_b),
            sha256=_digest(video_b),
            duration_us=3_000_000,
            width=1280,
            height=720,
            provenance_ref="provenance:b",
        ),
        MaterialAsset(
            asset_id="material_bgm",
            kind="audio",
            name="BGM",
            path=str(audio),
            sha256=_digest(audio),
            duration_us=2_000_000,
            provenance_ref="provenance:bgm",
        ),
    ]
    full = StaticTransform(x=0, y=0, width=1280, height=720)
    pip = StaticTransform(x=880, y=440, width=360, height=240)
    capability = ["video_source_trim"]
    if extra_capability:
        capability.append(extra_capability)
    actions = [
        ActionSpec(
            action_id="action_main_1",
            shot_id="shot_1",
            action_type="visual_media",
            timeline=TimeRange(start_us=0, end_us=1_000_000),
            asset_id="material_a",
            source=TimeRange(start_us=0, end_us=1_000_000),
            transform=full,
            volume=0,
            required_capabilities=capability,
            human_description="A 主画面",
        ),
        ActionSpec(
            action_id="action_pip_1",
            shot_id="shot_1",
            action_type="visual_media",
            timeline=TimeRange(start_us=250_000, end_us=750_000),
            asset_id="material_b",
            source=TimeRange(start_us=0, end_us=500_000),
            layer=10,
            transform=pip,
            volume=0,
            required_capabilities=["layer_overlay"],
            human_description="短暂画中画",
        ),
        ActionSpec(
            action_id="action_main_2",
            shot_id="shot_2",
            action_type="visual_media",
            timeline=TimeRange(start_us=1_000_000, end_us=2_000_000),
            asset_id="material_b",
            source=TimeRange(start_us=1_000_000, end_us=2_000_000),
            transform=full,
            volume=0,
            required_capabilities=["hard_cut"],
            human_description="B 主画面",
        ),
        ActionSpec(
            action_id="action_bgm",
            shot_id="shot_1",
            action_type="audio_media",
            timeline=TimeRange(start_us=0, end_us=2_000_000),
            asset_id="material_bgm",
            source=TimeRange(start_us=0, end_us=2_000_000),
            required_capabilities=["static_volume"],
            human_description="全片 BGM",
        ),
    ]
    return EditingSpecification(
        spec_id="spec_demo",
        title="一秒快切测试",
        canvas=CanvasSpec(width=1280, height=720, fps=30),
        duration_us=2_000_000,
        assets=assets,
        shots=[
            ShotSpec(
                shot_id="shot_1",
                timeline=TimeRange(start_us=0, end_us=1_000_000),
                main_action_id="action_main_1",
                action_ids=["action_main_1", "action_pip_1", "action_bgm"],
                human_description="首镜头叠加画中画",
                transition_to_next="hard_cut",
            ),
            ShotSpec(
                shot_id="shot_2",
                timeline=TimeRange(start_us=1_000_000, end_us=2_000_000),
                main_action_id="action_main_2",
                action_ids=["action_main_2"],
                human_description="第二镜头",
                transition_to_next="end",
            ),
        ],
        actions=actions,
        beat_grid_us=[0, 500_000, 1_000_000, 1_500_000, 2_000_000],
        retrieval_ids=["retrieval_demo"],
    )


def test_spec_rejects_timeline_gap(tmp_path: Path) -> None:
    payload = _spec(tmp_path).model_dump(mode="python")
    payload["shots"][1]["timeline"]["start_us"] = 1_100_000
    with pytest.raises(ValidationError, match="无空洞"):
        EditingSpecification.model_validate(payload)


def test_preflight_reports_explicit_capability_gap(tmp_path: Path) -> None:
    spec = _spec(tmp_path, extra_capability="keyframe_transform")
    assessment = preflight_spec(spec)
    assert not assessment.supported
    assert assessment.action_checks[0].missing == ["keyframe_transform"]


def test_compiler_maps_every_action_and_preserves_layer_order(tmp_path: Path) -> None:
    spec = _spec(tmp_path)

    def probe(path: Path) -> MediaMetadata:
        if path.suffix == ".mp3":
            return MediaMetadata(duration=2, audio_codec="mp3", sample_rate=48_000, channels=2)
        return MediaMetadata(
            duration=3,
            width=1280,
            height=720,
            frame_rate=30,
            video_codec="h264",
            audio_codec="aac",
        )

    result = ExecutionCompiler(probe=probe).compile(spec, tmp_path / "project")
    assert result.project.tracks[0].id == "track_visual_10"
    assert result.project.tracks[1].id == "track_visual_0"
    assert result.project.tracks[2].id == "track_audio_bgm"
    assert {entry.action_id for entry in result.trace_map.entries} == {
        action.action_id for action in spec.actions
    }
    assert len(result.copied_asset_paths) == 3
    assert all(path.is_file() for path in result.copied_asset_paths)


def test_compiler_stops_before_project_when_capability_is_missing(tmp_path: Path) -> None:
    with pytest.raises(PluginError) as captured:
        ExecutionCompiler().compile(
            _spec(tmp_path, extra_capability="animated_mask"),
            tmp_path / "project",
        )
    assert captured.value.code == "capability_gap"


def test_compiler_verifies_material_hash(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    Path(spec.assets[0].path).write_bytes(b"changed")
    with pytest.raises(PluginError) as captured:
        ExecutionCompiler().compile(spec, tmp_path / "project")
    assert captured.value.code == "artifact_hash_mismatch"
