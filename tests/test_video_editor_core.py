import json
import os
import time
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from components.video_editor import (
    AssetAddCommand,
    AssetCreate,
    AssetDeleteCommand,
    Canvas,
    ClipAddCommand,
    ClipCreate,
    ClipDeleteCommand,
    ClipSplitCommand,
    ClipUpdate,
    ClipUpdateCommand,
    CommandBatch,
    EditorCommand,
    ProjectUpdate,
    ProjectUpdateCommand,
    TrackAddCommand,
    TrackDeleteCommand,
    TrackMoveCommand,
    TrackUpdate,
    TrackUpdateCommand,
    Transform,
    VideoEditorError,
    VideoEditorService,
)


def test_stale_project_lock_is_reclaimed(tmp_path: Path) -> None:
    service = VideoEditorService(tmp_path)
    project = service.create("锁恢复")
    lock_path = tmp_path / project.id / ".project.lock"
    lock_path.write_text("999999", encoding="utf-8")
    stale = time.time() - 120
    os.utime(lock_path, (stale, stale))

    updated = service.apply(
        project.id,
        {
            "expected_revision": 0,
            "commands": [
                {"type": "track.add", "media_domain": "visual", "name": "主视频"}
            ],
        },
    )

    assert updated.revision == 1
    assert not lock_path.exists()


def test_fresh_project_lock_blocks_a_concurrent_writer(tmp_path: Path) -> None:
    service = VideoEditorService(tmp_path)
    project = service.create("并发写入")
    lock_path = tmp_path / project.id / ".project.lock"
    lock_path.write_text("writer", encoding="utf-8")

    with pytest.raises(VideoEditorError) as captured:
        service.apply(
            project.id,
            {
                "expected_revision": 0,
                "commands": [
                    {
                        "type": "track.add",
                        "media_domain": "visual",
                        "name": "主视频",
                    }
                ],
            },
        )

    assert captured.value.code == "project_busy"
    assert service.get(project.id).revision == 0


def _service(tmp_path: Path) -> VideoEditorService:
    return VideoEditorService(root=tmp_path / "output" / "editor" / "projects")


def _apply(
    service: VideoEditorService,
    project_id: str,
    revision: int,
    *commands: EditorCommand,
):
    return service.apply(
        project_id,
        CommandBatch(expected_revision=revision, commands=list(commands)),
    )


def test_create_persists_the_versioned_declarative_project(tmp_path: Path) -> None:
    service = _service(tmp_path)

    project = service.create("产品演示", Canvas(width=1080, height=1920, fps=30))

    assert project.schema_version == "2.0"
    assert project.revision == 0
    assert project.canvas.width == 1080
    assert project.assets == []
    assert project.tracks == []
    project_path = (
        tmp_path / "output" / "editor" / "projects" / project.id / "project.json"
    )
    assert project_path.is_file()
    assert json.loads(project_path.read_text(encoding="utf-8"))["id"] == project.id
    assert service.get(project.id) == project
    assert service.list() == [project]


def test_all_command_kinds_are_parsed_by_the_discriminated_union() -> None:
    adapter = TypeAdapter(EditorCommand)
    payloads = [
        {"type": "project.update", "changes": {"name": "新名称"}},
        {
            "type": "asset.add",
            "asset": {
                "kind": "video",
                "name": "片段",
                "path": "assets/demo.mp4",
                "metadata": {"duration": 8, "width": 1920, "height": 1080},
            },
        },
        {"type": "asset.delete", "asset_id": "asset_1"},
        {"type": "track.add", "media_domain": "visual", "name": "主轨"},
        {
            "type": "track.update",
            "track_id": "track_1",
            "changes": {"name": "新轨道"},
        },
        {"type": "track.move", "track_id": "track_1", "to_index": 0},
        {"type": "track.delete", "track_id": "track_1"},
        {
            "type": "clip.add",
            "track_id": "track_1",
            "clip": {
                "kind": "media",
                "timeline_start": 0,
                "duration": 2,
                "source_in": 0,
                "asset_id": "asset_1",
            },
        },
        {
            "type": "clip.update",
            "track_id": "track_1",
            "clip_id": "clip_1",
            "changes": {"timeline_start": 1},
        },
        {
            "type": "clip.delete",
            "track_id": "track_1",
            "clip_id": "clip_1",
        },
        {
            "type": "clip.split",
            "track_id": "track_1",
            "clip_id": "clip_1",
            "at": 1,
        },
    ]

    assert [adapter.validate_python(payload).type for payload in payloads] == [
        payload["type"] for payload in payloads
    ]


def test_clip_requires_exactly_one_of_asset_or_text() -> None:
    with pytest.raises(ValidationError):
        ClipCreate(kind="media", timeline_start=0, duration=2, source_in=0)

    with pytest.raises(ValidationError):
        ClipCreate(
            kind="media",
            timeline_start=0,
            duration=2,
            source_in=0,
            asset_id="asset_1",
            text="重复来源",
        )


def test_batch_builds_assets_tracks_and_clips_with_service_generated_ids(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    project = service.create("剪辑工程")

    project = _apply(
        service,
        project.id,
        project.revision,
        AssetAddCommand(
            asset=AssetCreate(
                kind="video",
                name="开场",
                path="assets/opening.mp4",
                metadata={
                    "duration": 12,
                    "width": 1920,
                    "height": 1080,
                    "video_codec": "h264",
                    "audio_codec": "aac",
                },
            )
        ),
        TrackAddCommand(media_domain="visual", name="主视频"),
    )
    asset = project.assets[0]
    track = project.tracks[0]
    assert asset.id.startswith("asset_")
    assert track.id.startswith("track_")
    assert project.revision == 1

    project = _apply(
        service,
        project.id,
        project.revision,
        ClipAddCommand(
            track_id=track.id,
            clip=ClipCreate(
                kind="media",
                timeline_start=1,
                duration=5,
                source_in=2,
                asset_id=asset.id,
                transform=Transform(
                    x=40,
                    y=20,
                    width=1280,
                    height=720,
                    rotation=2,
                    opacity=0.8,
                ),
                volume=0.7,
            ),
        ),
    )

    clip = project.tracks[0].clips[0]
    assert clip.id.startswith("clip_")
    assert clip.asset_id == asset.id
    assert clip.transform.width == 1280
    assert clip.volume == 0.7
    assert project.revision == 2


def test_text_clip_is_accepted_only_on_a_visual_track(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create("字幕")
    project = _apply(
        service,
        project.id,
        0,
        TrackAddCommand(media_domain="audio", name="音频轨"),
        TrackAddCommand(media_domain="visual", name="视觉轨"),
    )

    with pytest.raises(VideoEditorError) as captured:
        _apply(
            service,
            project.id,
            project.revision,
            ClipAddCommand(
                track_id=project.tracks[0].id,
                clip=ClipCreate(
                    kind="text",
                    timeline_start=0,
                    duration=2,
                    source_in=0,
                    text="错误轨道",
                ),
            ),
        )
    assert captured.value.code == "track_domain_mismatch"

    updated = _apply(
        service,
        project.id,
        project.revision,
        ClipAddCommand(
            track_id=project.tracks[1].id,
            clip=ClipCreate(
                kind="text",
                timeline_start=0,
                duration=2,
                source_in=0,
                text="正确字幕",
            ),
        ),
    )
    assert updated.tracks[1].clips[0].text == "正确字幕"


def test_source_range_cannot_exceed_media_duration(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create("范围")
    project = _apply(
        service,
        project.id,
        0,
        AssetAddCommand(
            asset=AssetCreate(
                kind="audio",
                name="配乐",
                path="assets/music.mp3",
                metadata={"duration": 3, "audio_codec": "mp3"},
            )
        ),
        TrackAddCommand(media_domain="audio", name="音乐"),
    )

    with pytest.raises(VideoEditorError) as captured:
        _apply(
            service,
            project.id,
            project.revision,
            ClipAddCommand(
                track_id=project.tracks[0].id,
                clip=ClipCreate(
                    kind="media",
                    timeline_start=0,
                    duration=2,
                    source_in=2,
                    asset_id=project.assets[0].id,
                ),
            ),
        )

    assert captured.value.code == "source_range_invalid"
    assert service.get(project.id).revision == project.revision


def test_split_update_and_delete_mutate_a_clip_as_one_revision_per_batch(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    project = service.create("命令")
    project = _apply(
        service,
        project.id,
        0,
        AssetAddCommand(
            asset=AssetCreate(
                kind="video",
                name="素材",
                path="assets/video.mp4",
                metadata={"duration": 10, "width": 1920, "height": 1080},
            )
        ),
        TrackAddCommand(media_domain="visual", name="主轨"),
    )
    project = _apply(
        service,
        project.id,
        1,
        ClipAddCommand(
            track_id=project.tracks[0].id,
            clip=ClipCreate(
                kind="media",
                timeline_start=2,
                duration=6,
                source_in=1,
                asset_id=project.assets[0].id,
            ),
        ),
    )
    clip_id = project.tracks[0].clips[0].id

    project = _apply(
        service,
        project.id,
        2,
        ClipSplitCommand(track_id=project.tracks[0].id, clip_id=clip_id, at=5),
    )
    left, right = project.tracks[0].clips
    assert (left.timeline_start, left.duration, left.source_in) == (2, 3, 1)
    assert (right.timeline_start, right.duration, right.source_in) == (5, 3, 4)

    project = _apply(
        service,
        project.id,
        3,
        ClipUpdateCommand(
            track_id=project.tracks[0].id,
            clip_id=right.id,
            changes=ClipUpdate(timeline_start=6, volume=0.5),
        ),
        ClipDeleteCommand(track_id=project.tracks[0].id, clip_id=left.id),
    )
    assert len(project.tracks[0].clips) == 1
    assert project.tracks[0].clips[0].timeline_start == 6
    assert project.revision == 4


def test_asset_in_use_is_not_deleted(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create("引用")
    project = _apply(
        service,
        project.id,
        0,
        AssetAddCommand(
            asset=AssetCreate(
                kind="image",
                name="封面",
                path="assets/cover.png",
                metadata={"width": 1080, "height": 1920},
            )
        ),
        TrackAddCommand(media_domain="visual", name="叠加"),
    )
    project = _apply(
        service,
        project.id,
        1,
        ClipAddCommand(
            track_id=project.tracks[0].id,
            clip=ClipCreate(
                kind="media",
                timeline_start=0,
                duration=2,
                source_in=0,
                asset_id=project.assets[0].id,
            ),
        ),
    )

    with pytest.raises(VideoEditorError) as captured:
        _apply(
            service,
            project.id,
            project.revision,
            AssetDeleteCommand(asset_id=project.assets[0].id),
        )

    assert captured.value.code == "asset_in_use"


def test_expected_revision_prevents_stale_writer_from_overwriting_project(
    tmp_path: Path,
) -> None:
    first = _service(tmp_path)
    second = _service(tmp_path)
    original = first.create("初始")

    current = first.apply(
        original.id,
        {
            "expected_revision": 0,
            "commands": [
                {"type": "project.update", "changes": {"name": "第一位写入者"}}
            ],
        },
    )

    with pytest.raises(VideoEditorError) as captured:
        second.apply(
            original.id,
            CommandBatch(
                expected_revision=0,
                commands=[
                    ProjectUpdateCommand(changes=ProjectUpdate(name="过期写入者"))
                ],
            ),
        )

    assert captured.value.code == "revision_conflict"
    assert first.get(original.id).name == "第一位写入者"
    assert first.get(original.id).revision == current.revision


def test_track_delete_and_project_update_are_persisted(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create("旧名称")
    project = _apply(
        service,
        project.id,
        0,
        TrackAddCommand(media_domain="visual", name="临时轨道"),
    )

    updated = _apply(
        service,
        project.id,
        1,
        ProjectUpdateCommand(
            changes=ProjectUpdate(
                name="新名称", canvas=Canvas(width=720, height=1280, fps=24)
            )
        ),
        TrackDeleteCommand(track_id=project.tracks[0].id),
    )

    assert updated.name == "新名称"
    assert updated.canvas.width == 720
    assert updated.tracks == []
    assert service.get(project.id) == updated


def test_tracks_can_be_inserted_renamed_and_reordered(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create("自由轨道")
    project = _apply(
        service,
        project.id,
        0,
        TrackAddCommand(media_domain="audio", name="音频 1"),
        TrackAddCommand(media_domain="visual", name="视觉 1", index=0),
        TrackAddCommand(media_domain="visual", name="视觉 2", index=0),
    )

    assert [track.name for track in project.tracks] == ["视觉 2", "视觉 1", "音频 1"]
    audio_id = project.tracks[2].id
    project = _apply(
        service,
        project.id,
        1,
        TrackUpdateCommand(
            track_id=audio_id,
            changes=TrackUpdate(name="环境声"),
        ),
        TrackMoveCommand(track_id=audio_id, to_index=1),
    )

    assert [track.name for track in project.tracks] == ["视觉 2", "环境声", "视觉 1"]
    assert project.revision == 2


def test_track_position_outside_project_is_rejected_atomically(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create("轨道位置")

    with pytest.raises(VideoEditorError) as captured:
        _apply(
            service,
            project.id,
            0,
            TrackAddCommand(media_domain="visual", name="越界", index=1),
        )

    assert captured.value.code == "track_index_invalid"
    assert service.get(project.id).revision == 0


def test_external_input_and_project_identifier_are_validated_at_service_boundary(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    project = service.create("边界")

    with pytest.raises(VideoEditorError) as invalid_batch:
        service.apply(
            project.id,
            {"expected_revision": "not-an-int", "commands": []},
        )
    assert invalid_batch.value.code == "invalid_input"

    with pytest.raises(VideoEditorError) as invalid_id:
        service.get("../outside")
    assert invalid_id.value.code == "invalid_project_id"


def test_get_rejects_a_persisted_project_with_a_dangling_asset_reference(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    project = service.create("损坏工程")
    project_path = (
        tmp_path / "output" / "editor" / "projects" / project.id / "project.json"
    )
    payload = json.loads(project_path.read_text(encoding="utf-8"))
    payload["tracks"] = [
        {
            "id": "track_manual",
            "media_domain": "visual",
            "name": "损坏轨道",
            "clips": [
                {
                    "id": "clip_manual",
                    "kind": "media",
                    "timeline_start": 0,
                    "duration": 1,
                    "source_in": 0,
                    "asset_id": "asset_missing",
                    "text": None,
                    "transform": {
                        "x": 0,
                        "y": 0,
                        "width": 1920,
                        "height": 1080,
                        "rotation": 0,
                        "opacity": 1,
                    },
                    "volume": 1,
                }
            ],
        }
    ]
    project_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(VideoEditorError) as captured:
        service.get(project.id)

    assert captured.value.code == "project_corrupt"


def test_get_reports_non_utf8_project_as_corrupt(tmp_path: Path) -> None:
    service = _service(tmp_path)
    project = service.create("编码损坏")
    project_path = (
        tmp_path / "output" / "editor" / "projects" / project.id / "project.json"
    )
    project_path.write_bytes(b"\xff")

    with pytest.raises(VideoEditorError) as captured:
        service.get(project.id)

    assert captured.value.code == "project_corrupt"
