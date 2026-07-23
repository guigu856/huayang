from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models import ReferenceContextBinding


class EditingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TimeRange(EditingModel):
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)

    @model_validator(mode="after")
    def end_is_after_start(self) -> Self:
        if self.end_us <= self.start_us:
            raise ValueError("时间区间终点必须晚于起点")
        return self

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us


class CanvasSpec(EditingModel):
    width: int = Field(ge=1, le=7680)
    height: int = Field(ge=1, le=7680)
    fps: float = Field(gt=0, le=240)
    background_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")


class MaterialAsset(EditingModel):
    asset_id: str = Field(pattern=r"^material_[A-Za-z0-9_-]+$")
    kind: Literal["video", "image", "audio"]
    name: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duration_us: int | None = Field(default=None, gt=0)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    provenance_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def metadata_matches_kind(self) -> Self:
        if self.kind in {"video", "audio"} and self.duration_us is None:
            raise ValueError("视频和音频素材必须提供时长")
        if self.kind in {"video", "image"} and (self.width is None or self.height is None):
            raise ValueError("视觉素材必须提供尺寸")
        return self


class StaticTransform(EditingModel):
    x: float = 0
    y: float = 0
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: float = 0
    opacity: float = Field(default=1, ge=0, le=1)


class ActionSpec(EditingModel):
    action_id: str = Field(pattern=r"^action_[A-Za-z0-9_-]+$")
    shot_id: str = Field(pattern=r"^shot_[A-Za-z0-9_-]+$")
    action_type: Literal["visual_media", "audio_media", "text_overlay"]
    timeline: TimeRange
    asset_id: str | None = Field(default=None, pattern=r"^material_[A-Za-z0-9_-]+$")
    source: TimeRange | None = None
    text: str | None = Field(default=None, min_length=1)
    layer: int = Field(default=0, ge=0, le=100)
    transform: StaticTransform | None = None
    volume: float = Field(default=1, ge=0, le=4)
    required_capabilities: list[str] = Field(min_length=1)
    human_description: str = Field(min_length=1)
    audio_event_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def fields_match_action_type(self) -> Self:
        if self.action_type == "text_overlay":
            if self.text is None or self.asset_id is not None or self.source is not None:
                raise ValueError("文本动作必须且只能提供 text")
            if self.transform is None:
                raise ValueError("文本动作必须提供静态变换")
            return self
        if self.asset_id is None or self.text is not None:
            raise ValueError("媒体动作必须且只能引用素材")
        if self.action_type == "visual_media" and self.transform is None:
            raise ValueError("视觉动作必须提供静态变换")
        if self.source is not None and self.source.duration_us != self.timeline.duration_us:
            raise ValueError("当前引擎不支持变速，源区间和时间线时长必须一致")
        return self


class ShotSpec(EditingModel):
    shot_id: str = Field(pattern=r"^shot_[A-Za-z0-9_-]+$")
    timeline: TimeRange
    main_action_id: str = Field(pattern=r"^action_[A-Za-z0-9_-]+$")
    action_ids: list[str] = Field(min_length=1)
    human_description: str = Field(min_length=1)
    audio_event_refs: list[str] = Field(default_factory=list)
    transition_to_next: Literal["hard_cut", "end"]


class EditingSpecification(EditingModel):
    schema_version: Literal["1.0"] = "1.0"
    spec_id: str = Field(pattern=r"^spec_[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=200)
    canvas: CanvasSpec
    duration_us: int = Field(gt=0)
    assets: list[MaterialAsset] = Field(min_length=1)
    shots: list[ShotSpec] = Field(min_length=1)
    actions: list[ActionSpec] = Field(min_length=1)
    beat_grid_us: list[int] = Field(default_factory=list)
    retrieval_ids: list[str] = Field(min_length=1)
    reference_context: ReferenceContextBinding | None = None

    @model_validator(mode="after")
    def validate_graph_and_timeline(self) -> Self:
        _require_unique([asset.asset_id for asset in self.assets], "素材 ID")
        _require_unique([shot.shot_id for shot in self.shots], "镜头 ID")
        _require_unique([action.action_id for action in self.actions], "动作 ID")
        asset_by_id = {asset.asset_id: asset for asset in self.assets}
        action_by_id = {action.action_id: action for action in self.actions}
        expected_start = 0
        referenced_actions: list[str] = []
        for index, shot in enumerate(self.shots):
            if shot.timeline.start_us != expected_start:
                raise ValueError("主镜头时间线必须从零开始且无空洞、无重叠")
            expected_start = shot.timeline.end_us
            if shot.transition_to_next != ("end" if index == len(self.shots) - 1 else "hard_cut"):
                raise ValueError("当前镜头边界只接受硬切，末镜头必须标记 end")
            if shot.main_action_id not in shot.action_ids:
                raise ValueError("主动作必须列入镜头 action_ids")
            referenced_actions.extend(shot.action_ids)
            for action_id in shot.action_ids:
                action = action_by_id.get(action_id)
                if action is None or action.shot_id != shot.shot_id:
                    raise ValueError("镜头动作引用缺失或归属不一致")
            main = action_by_id[shot.main_action_id]
            if (
                main.action_type != "visual_media"
                or main.layer != 0
                or main.timeline != shot.timeline
            ):
                raise ValueError("每个镜头必须有覆盖完整镜头区间的零层主画面")
            for action_id in shot.action_ids:
                action = action_by_id[action_id]
                if action.action_type != "audio_media" and (
                    action.timeline.start_us < shot.timeline.start_us
                    or action.timeline.end_us > shot.timeline.end_us
                ):
                    raise ValueError("视觉或文字动作必须位于所属镜头区间内")
        if expected_start != self.duration_us:
            raise ValueError("主镜头时间线必须覆盖声明的完整时长")
        if sorted(referenced_actions) != sorted(action_by_id):
            raise ValueError("每个动作必须被且只被一个镜头引用")
        if len(referenced_actions) != len(set(referenced_actions)):
            raise ValueError("动作被多个镜头重复引用")
        for action in self.actions:
            if action.asset_id is None:
                continue
            asset = asset_by_id.get(action.asset_id)
            if asset is None:
                raise ValueError("动作引用的素材不存在")
            if action.action_type == "audio_media" and asset.kind != "audio":
                raise ValueError("音频动作必须引用音频素材")
            if action.action_type == "visual_media" and asset.kind not in {"video", "image"}:
                raise ValueError("视觉动作必须引用视觉素材")
            if asset.kind == "image" and action.source is not None:
                raise ValueError("图片动作没有源时间区间")
            if asset.kind != "image" and action.source is None:
                raise ValueError("视频和音频动作必须提供源时间区间")
            if (
                action.source is not None
                and asset.duration_us is not None
                and action.source.end_us > asset.duration_us
            ):
                raise ValueError("动作源区间超出素材时长")
        if self.beat_grid_us != sorted(set(self.beat_grid_us)) or any(
            value < 0 or value > self.duration_us for value in self.beat_grid_us
        ):
            raise ValueError("beat grid 必须有序、唯一且位于工程时长内")
        return self


class ActionCapabilityCheck(EditingModel):
    action_id: str
    required: list[str]
    missing: list[str]


class CapabilityAssessment(EditingModel):
    registry_version: str
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    supported: bool
    action_checks: list[ActionCapabilityCheck]


class SpecTraceEntry(EditingModel):
    action_id: str
    project_json_path: str
    track_id: str
    clip_id: str


class SpecTraceMap(EditingModel):
    schema_version: Literal["1.0"] = "1.0"
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: str
    entries: list[SpecTraceEntry]


def _require_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} 重复")
