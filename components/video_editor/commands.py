from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from .models import AssetCreate, Canvas, ClipCreate, EditorModel, Transform


class ProjectUpdate(EditorModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    canvas: Canvas | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        resolved = value.strip()
        if not resolved:
            raise ValueError("工程名称不能为空")
        return resolved

    @model_validator(mode="after")
    def contains_a_change(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, field) is None for field in self.model_fields_set
        ):
            raise ValueError("project.update 必须提供有效变更")
        return self


class ClipUpdate(EditorModel):
    timeline_start: float | None = Field(default=None, ge=0)
    duration: float | None = Field(default=None, gt=0)
    source_in: float | None = Field(default=None, ge=0)
    asset_id: str | None = Field(default=None, min_length=1)
    text: str | None = Field(default=None, min_length=1)
    transform: Transform | None = None
    volume: float | None = Field(default=None, ge=0)

    @field_validator("asset_id", "text")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        resolved = value.strip()
        if not resolved:
            raise ValueError("字段不能为空")
        return resolved

    @model_validator(mode="after")
    def contains_a_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("clip.update 必须提供变更")
        nullable_sources = {"asset_id", "text"}
        if any(
            getattr(self, field) is None
            for field in self.model_fields_set - nullable_sources
        ):
            raise ValueError("片段变更字段不能为 null")
        if self.asset_id is not None and self.text is not None:
            raise ValueError("asset_id 与 text 必须且只能提供一个")
        if {"asset_id", "text"}.issubset(self.model_fields_set):
            if self.asset_id is None and self.text is None:
                raise ValueError("片段来源不能为空")
        return self


class ProjectUpdateCommand(EditorModel):
    type: Literal["project.update"] = "project.update"
    changes: ProjectUpdate


class AssetAddCommand(EditorModel):
    type: Literal["asset.add"] = "asset.add"
    asset: AssetCreate


class AssetDeleteCommand(EditorModel):
    type: Literal["asset.delete"] = "asset.delete"
    asset_id: str = Field(min_length=1)


class TrackAddCommand(EditorModel):
    type: Literal["track.add"] = "track.add"
    media_domain: Literal["visual", "audio"]
    name: str = Field(min_length=1, max_length=200)
    index: int | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        resolved = value.strip()
        if not resolved:
            raise ValueError("轨道名称不能为空")
        return resolved


class TrackUpdate(EditorModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        resolved = value.strip()
        if not resolved:
            raise ValueError("轨道名称不能为空")
        return resolved


class TrackUpdateCommand(EditorModel):
    type: Literal["track.update"] = "track.update"
    track_id: str = Field(min_length=1)
    changes: TrackUpdate


class TrackMoveCommand(EditorModel):
    type: Literal["track.move"] = "track.move"
    track_id: str = Field(min_length=1)
    to_index: int = Field(ge=0)


class TrackDeleteCommand(EditorModel):
    type: Literal["track.delete"] = "track.delete"
    track_id: str = Field(min_length=1)


class ClipAddCommand(EditorModel):
    type: Literal["clip.add"] = "clip.add"
    track_id: str = Field(min_length=1)
    clip: ClipCreate


class ClipUpdateCommand(EditorModel):
    type: Literal["clip.update"] = "clip.update"
    track_id: str = Field(min_length=1)
    clip_id: str = Field(min_length=1)
    changes: ClipUpdate


class ClipDeleteCommand(EditorModel):
    type: Literal["clip.delete"] = "clip.delete"
    track_id: str = Field(min_length=1)
    clip_id: str = Field(min_length=1)


class ClipSplitCommand(EditorModel):
    type: Literal["clip.split"] = "clip.split"
    track_id: str = Field(min_length=1)
    clip_id: str = Field(min_length=1)
    at: float = Field(ge=0)


EditorCommand = Annotated[
    ProjectUpdateCommand
    | AssetAddCommand
    | AssetDeleteCommand
    | TrackAddCommand
    | TrackUpdateCommand
    | TrackMoveCommand
    | TrackDeleteCommand
    | ClipAddCommand
    | ClipUpdateCommand
    | ClipDeleteCommand
    | ClipSplitCommand,
    Field(discriminator="type"),
]


class CommandBatch(EditorModel):
    expected_revision: int = Field(ge=0)
    commands: list[EditorCommand] = Field(min_length=1)
