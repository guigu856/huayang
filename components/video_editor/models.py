from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EditorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Canvas(EditorModel):
    width: int = Field(default=1920, ge=1, le=7680)
    height: int = Field(default=1080, ge=1, le=7680)
    fps: float = Field(default=30, gt=0, le=240)
    background_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")


class MediaMetadata(EditorModel):
    duration: float | None = Field(default=None, gt=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    frame_rate: float | None = Field(default=None, gt=0)
    video_codec: str | None = None
    audio_codec: str | None = None
    sample_rate: int | None = Field(default=None, ge=1)
    channels: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def dimensions_are_a_pair(self) -> Self:
        if (self.width is None) != (self.height is None):
            raise ValueError("width 与 height 必须同时提供")
        return self


class AssetCreate(EditorModel):
    kind: Literal["video", "image", "audio"]
    name: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1)
    metadata: MediaMetadata

    @field_validator("name", "path")
    @classmethod
    def strip_non_empty(cls, value: str) -> str:
        resolved = value.strip()
        if not resolved:
            raise ValueError("字段不能为空")
        return resolved

    @model_validator(mode="after")
    def required_metadata_is_present(self) -> Self:
        if self.kind == "video":
            if self.metadata.duration is None:
                raise ValueError("视频素材必须提供 duration")
            if self.metadata.width is None:
                raise ValueError("视频素材必须提供 width 与 height")
        elif self.kind == "image":
            if self.metadata.width is None:
                raise ValueError("图片素材必须提供 width 与 height")
        elif self.metadata.duration is None:
            raise ValueError("音频素材必须提供 duration")
        return self


class Asset(AssetCreate):
    id: str = Field(min_length=1)


class Transform(EditorModel):
    x: float = 0
    y: float = 0
    width: float = Field(default=1920, gt=0)
    height: float = Field(default=1080, gt=0)
    rotation: float = 0
    opacity: float = Field(default=1, ge=0, le=1)


class ClipCreate(EditorModel):
    kind: Literal["media", "text"]
    timeline_start: float = Field(ge=0)
    duration: float = Field(gt=0)
    source_in: float = Field(default=0, ge=0)
    asset_id: str | None = Field(default=None, min_length=1)
    text: str | None = Field(default=None, min_length=1)
    transform: Transform = Field(default_factory=Transform)
    volume: float = Field(default=1, ge=0)

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
    def source_matches_kind(self) -> Self:
        if self.kind == "media":
            if self.asset_id is None or self.text is not None:
                raise ValueError("媒体片段必须且只能提供 asset_id")
        elif self.text is None or self.asset_id is not None:
            raise ValueError("文本片段必须且只能提供 text")
        return self


class Clip(ClipCreate):
    id: str = Field(min_length=1)


class Track(EditorModel):
    id: str = Field(min_length=1)
    media_domain: Literal["visual", "audio"]
    name: str = Field(min_length=1, max_length=200)
    clips: list[Clip] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        resolved = value.strip()
        if not resolved:
            raise ValueError("轨道名称不能为空")
        return resolved


class EditorProject(EditorModel):
    schema_version: Literal["2.0"] = "2.0"
    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    revision: int = Field(default=0, ge=0)
    canvas: Canvas = Field(default_factory=Canvas)
    assets: list[Asset] = Field(default_factory=list)
    tracks: list[Track] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        resolved = value.strip()
        if not resolved:
            raise ValueError("工程名称不能为空")
        return resolved

    @model_validator(mode="after")
    def identifiers_are_unique(self) -> Self:
        asset_ids = [asset.id for asset in self.assets]
        track_ids = [track.id for track in self.tracks]
        clip_ids = [clip.id for track in self.tracks for clip in track.clips]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("素材 ID 重复")
        if len(track_ids) != len(set(track_ids)):
            raise ValueError("轨道 ID 重复")
        if len(clip_ids) != len(set(clip_ids)):
            raise ValueError("片段 ID 重复")
        return self

    @model_validator(mode="after")
    def clip_references_are_consistent(self) -> Self:
        assets = {asset.id: asset for asset in self.assets}
        accepted_kinds = {"visual": {"video", "image"}, "audio": {"audio"}}
        for track in self.tracks:
            for clip in track.clips:
                if clip.kind == "text":
                    if track.media_domain != "visual":
                        raise ValueError("音频轨道包含文本片段")
                    continue
                if clip.asset_id is None:
                    raise ValueError("媒体片段缺少素材引用")
                asset = assets.get(clip.asset_id)
                if asset is None:
                    raise ValueError("片段引用的素材不存在")
                if asset.kind not in accepted_kinds[track.media_domain]:
                    raise ValueError("素材类型与轨道处理域不匹配")
                if (
                    asset.metadata.duration is not None
                    and clip.source_in + clip.duration
                    > asset.metadata.duration + 1e-9
                ):
                    raise ValueError("片段源区间超出素材时长")
        return self
