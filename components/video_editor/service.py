from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .commands import (
    AssetAddCommand,
    AssetDeleteCommand,
    ClipAddCommand,
    ClipDeleteCommand,
    ClipSplitCommand,
    ClipUpdateCommand,
    CommandBatch,
    EditorCommand,
    ProjectUpdateCommand,
    TrackAddCommand,
    TrackDeleteCommand,
    TrackMoveCommand,
    TrackUpdateCommand,
)
from .errors import VideoEditorError
from .models import Asset, Canvas, Clip, EditorProject, Track
from .storage import ProjectStorage


class VideoEditorService:
    """执行声明式剪辑命令并以乐观版本控制持久化工程。"""

    def __init__(
        self,
        root: Path | str = Path("output/editor/projects"),
        *,
        storage: ProjectStorage | None = None,
    ) -> None:
        self.storage = storage or ProjectStorage(root)

    def create(
        self,
        name: str,
        canvas: Canvas | Mapping[str, Any] | None = None,
    ) -> EditorProject:
        try:
            resolved_canvas = (
                Canvas() if canvas is None else Canvas.model_validate(canvas)
            )
            project = EditorProject(
                id=self._new_id("project"),
                name=name,
                canvas=resolved_canvas,
            )
        except (ValidationError, TypeError) as error:
            raise VideoEditorError("invalid_input", "创建工程参数无效") from error

        for _ in range(3):
            try:
                self.storage.create(project)
                return project
            except VideoEditorError as error:
                if error.code != "project_exists":
                    raise
                project = project.model_copy(update={"id": self._new_id("project")})
        raise VideoEditorError("id_generation_failed", "工程 ID 生成失败")

    def list(self) -> list[EditorProject]:
        return self.storage.list()

    def get(self, project_id: str) -> EditorProject:
        return self.storage.get(project_id)

    def apply(
        self,
        project_id: str,
        batch: CommandBatch | Mapping[str, Any],
    ) -> EditorProject:
        try:
            resolved_batch = CommandBatch.model_validate(batch)
        except (ValidationError, TypeError) as error:
            raise VideoEditorError("invalid_input", "命令批次结构无效") from error

        current = self.storage.get(project_id)
        if current.revision != resolved_batch.expected_revision:
            raise VideoEditorError(
                "revision_conflict",
                "工程已被其他写入者更新",
                details={
                    "expected_revision": resolved_batch.expected_revision,
                    "actual_revision": current.revision,
                },
            )

        working = current.model_copy(deep=True)
        for command in resolved_batch.commands:
            working = self._apply_command(working, command)
        try:
            updated = EditorProject.model_validate(
                {
                    **working.model_dump(mode="python"),
                    "revision": current.revision + 1,
                }
            )
        except ValidationError as error:
            raise VideoEditorError("invalid_input", "命令执行结果无效") from error
        self.storage.save(updated, expected_revision=resolved_batch.expected_revision)
        return updated

    def _apply_command(
        self, project: EditorProject, command: EditorCommand
    ) -> EditorProject:
        if isinstance(command, ProjectUpdateCommand):
            updates = {
                field: getattr(command.changes, field)
                for field in command.changes.model_fields_set
            }
            return project.model_copy(update=updates)
        if isinstance(command, AssetAddCommand):
            asset = Asset(
                id=self._new_id("asset", self._all_ids(project)),
                **command.asset.model_dump(mode="python"),
            )
            return project.model_copy(update={"assets": [*project.assets, asset]})
        if isinstance(command, AssetDeleteCommand):
            return self._delete_asset(project, command.asset_id)
        if isinstance(command, TrackAddCommand):
            track = Track(
                id=self._new_id("track", self._all_ids(project)),
                media_domain=command.media_domain,
                name=command.name,
            )
            tracks = list(project.tracks)
            index = len(tracks) if command.index is None else command.index
            if index > len(tracks):
                raise VideoEditorError("track_index_invalid", "轨道位置超出范围")
            tracks.insert(index, track)
            return project.model_copy(update={"tracks": tracks})
        if isinstance(command, TrackUpdateCommand):
            track_index, track = self._find_track(project, command.track_id)
            return self._replace_track(
                project,
                track_index,
                track.model_copy(update={"name": command.changes.name}),
            )
        if isinstance(command, TrackMoveCommand):
            return self._move_track(project, command.track_id, command.to_index)
        if isinstance(command, TrackDeleteCommand):
            return self._delete_track(project, command.track_id)
        if isinstance(command, ClipAddCommand):
            track_index, track = self._find_track(project, command.track_id)
            clip = Clip(
                id=self._new_id("clip", self._all_ids(project)),
                **command.clip.model_dump(mode="python"),
            )
            self._validate_clip(project, track, clip)
            return self._replace_track(
                project,
                track_index,
                track.model_copy(update={"clips": [*track.clips, clip]}),
            )
        if isinstance(command, ClipUpdateCommand):
            return self._update_clip(project, command)
        if isinstance(command, ClipDeleteCommand):
            return self._delete_clip(project, command.track_id, command.clip_id)
        if isinstance(command, ClipSplitCommand):
            return self._split_clip(project, command)
        raise VideoEditorError("invalid_input", "未知命令类型")

    def _delete_asset(self, project: EditorProject, asset_id: str) -> EditorProject:
        if all(asset.id != asset_id for asset in project.assets):
            raise VideoEditorError("asset_not_found", "素材不存在")
        if any(
            clip.asset_id == asset_id
            for track in project.tracks
            for clip in track.clips
        ):
            raise VideoEditorError("asset_in_use", "素材仍被片段引用")
        return project.model_copy(
            update={
                "assets": [asset for asset in project.assets if asset.id != asset_id]
            }
        )

    def _delete_track(self, project: EditorProject, track_id: str) -> EditorProject:
        if all(track.id != track_id for track in project.tracks):
            raise VideoEditorError("track_not_found", "轨道不存在")
        return project.model_copy(
            update={"tracks": [track for track in project.tracks if track.id != track_id]}
        )

    def _move_track(
        self, project: EditorProject, track_id: str, to_index: int
    ) -> EditorProject:
        track_index, track = self._find_track(project, track_id)
        tracks = list(project.tracks)
        if to_index >= len(tracks):
            raise VideoEditorError("track_index_invalid", "轨道位置超出范围")
        tracks.pop(track_index)
        tracks.insert(to_index, track)
        return project.model_copy(update={"tracks": tracks})

    def _update_clip(
        self, project: EditorProject, command: ClipUpdateCommand
    ) -> EditorProject:
        track_index, track = self._find_track(project, command.track_id)
        clip_index, clip = self._find_clip(track, command.clip_id)
        updates = {
            field: getattr(command.changes, field)
            for field in command.changes.model_fields_set
        }
        try:
            updated_clip = Clip.model_validate(
                {**clip.model_dump(mode="python"), **updates}
            )
        except ValidationError as error:
            raise VideoEditorError("invalid_input", "片段变更无效") from error
        self._validate_clip(project, track, updated_clip)
        clips = list(track.clips)
        clips[clip_index] = updated_clip
        return self._replace_track(
            project, track_index, track.model_copy(update={"clips": clips})
        )

    def _delete_clip(
        self, project: EditorProject, track_id: str, clip_id: str
    ) -> EditorProject:
        track_index, track = self._find_track(project, track_id)
        clip_index, _ = self._find_clip(track, clip_id)
        clips = list(track.clips)
        clips.pop(clip_index)
        return self._replace_track(
            project, track_index, track.model_copy(update={"clips": clips})
        )

    def _split_clip(
        self, project: EditorProject, command: ClipSplitCommand
    ) -> EditorProject:
        track_index, track = self._find_track(project, command.track_id)
        clip_index, clip = self._find_clip(track, command.clip_id)
        clip_end = clip.timeline_start + clip.duration
        if not clip.timeline_start < command.at < clip_end:
            raise VideoEditorError("split_point_invalid", "切分点必须位于片段内部")

        left_duration = command.at - clip.timeline_start
        right_duration = clip_end - command.at
        left = Clip.model_validate(
            {**clip.model_dump(mode="python"), "duration": left_duration}
        )
        right_source_in = (
            clip.source_in + left_duration
            if clip.asset_id is not None
            else clip.source_in
        )
        right = Clip.model_validate(
            {
                **clip.model_dump(mode="python"),
                "id": self._new_id("clip", self._all_ids(project)),
                "timeline_start": command.at,
                "duration": right_duration,
                "source_in": right_source_in,
            }
        )
        self._validate_clip(project, track, left)
        self._validate_clip(project, track, right)
        clips = list(track.clips)
        clips[clip_index : clip_index + 1] = [left, right]
        return self._replace_track(
            project, track_index, track.model_copy(update={"clips": clips})
        )

    @staticmethod
    def _find_track(project: EditorProject, track_id: str) -> tuple[int, Track]:
        for index, track in enumerate(project.tracks):
            if track.id == track_id:
                return index, track
        raise VideoEditorError("track_not_found", "轨道不存在")

    @staticmethod
    def _find_clip(track: Track, clip_id: str) -> tuple[int, Clip]:
        for index, clip in enumerate(track.clips):
            if clip.id == clip_id:
                return index, clip
        raise VideoEditorError("clip_not_found", "片段不存在")

    @staticmethod
    def _replace_track(
        project: EditorProject, track_index: int, track: Track
    ) -> EditorProject:
        tracks = list(project.tracks)
        tracks[track_index] = track
        return project.model_copy(update={"tracks": tracks})

    @staticmethod
    def _validate_clip(project: EditorProject, track: Track, clip: Clip) -> None:
        if clip.kind == "text":
            if track.media_domain != "visual":
                raise VideoEditorError(
                    "track_domain_mismatch", "音频轨道只接受媒体片段"
                )
            return
        if clip.asset_id is None:
            raise VideoEditorError("invalid_input", "媒体片段缺少素材引用")

        asset = next(
            (asset for asset in project.assets if asset.id == clip.asset_id), None
        )
        if asset is None:
            raise VideoEditorError("asset_not_found", "片段引用的素材不存在")
        accepted_kinds = {"visual": {"video", "image"}, "audio": {"audio"}}
        if asset.kind not in accepted_kinds[track.media_domain]:
            raise VideoEditorError(
                "track_domain_mismatch", "素材类型与轨道处理域不匹配"
            )
        media_duration = asset.metadata.duration
        if (
            media_duration is not None
            and clip.source_in + clip.duration > media_duration + 1e-9
        ):
            raise VideoEditorError(
                "source_range_invalid", "片段源区间超出素材时长"
            )

    @staticmethod
    def _all_ids(project: EditorProject) -> set[str]:
        return {
            project.id,
            *(asset.id for asset in project.assets),
            *(track.id for track in project.tracks),
            *(clip.id for track in project.tracks for clip in track.clips),
        }

    @staticmethod
    def _new_id(kind: str, existing: set[str] | None = None) -> str:
        occupied = existing or set()
        for _ in range(3):
            candidate = f"{kind}_{uuid.uuid4().hex[:16]}"
            if candidate not in occupied:
                return candidate
        raise VideoEditorError("id_generation_failed", f"{kind} ID 生成失败")
