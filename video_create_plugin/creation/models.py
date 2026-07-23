from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from ..models import PluginModel, ReferenceContextBinding


class CreativeDirection(PluginModel):
    schema_version: Literal["1.0"] = "1.0"
    title: str = Field(min_length=1, max_length=200)
    user_intent: str = Field(min_length=1)
    video_type: str = Field(min_length=1)
    core_mechanism: str = Field(min_length=1)
    production_method: str = Field(min_length=1)
    visual_language: str = Field(min_length=1)
    rhythm_and_sound: str = Field(min_length=1)
    transition_principles: str = Field(min_length=1)
    asset_and_music_traits: str = Field(min_length=1)
    viewing_experience: str = Field(min_length=1)
    retrieval_ids: list[str] = Field(min_length=1)
    reference_context: ReferenceContextBinding | None = None


class SourceRange(PluginModel):
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)

    @model_validator(mode="after")
    def end_is_after_start(self) -> Self:
        if self.end_us <= self.start_us:
            raise ValueError("可用源区间终点必须晚于起点")
        return self


class PreparedMaterial(PluginModel):
    asset_id: str = Field(pattern=r"^material_[A-Za-z0-9_-]+$")
    kind: Literal["video", "image"]
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duration_us: int | None = Field(default=None, gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    content_summary: str = Field(min_length=1)
    selection_traits: list[str] = Field(min_length=1)
    usable_source_ranges: list[SourceRange] = Field(min_length=1)
    source_url: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    creator: str = Field(min_length=1)
    license_record: str = Field(min_length=1)
    provenance_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def source_ranges_fit_duration(self) -> Self:
        if self.duration_us is not None and any(
            item.end_us > self.duration_us for item in self.usable_source_ranges
        ):
            raise ValueError("素材可用区间超出源时长")
        return self


class BgmSection(PluginModel):
    section_id: str = Field(pattern=r"^section_[A-Za-z0-9_-]+$")
    start_us: int = Field(ge=0)
    end_us: int = Field(gt=0)
    role: str = Field(min_length=1)
    energy_phase: str = Field(min_length=1)

    @model_validator(mode="after")
    def end_is_after_start(self) -> Self:
        if self.end_us <= self.start_us:
            raise ValueError("音乐段落终点必须晚于起点")
        return self


class BgmPackage(PluginModel):
    asset_id: str = Field(pattern=r"^material_[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duration_us: int = Field(gt=0)
    source_url: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    creator: str = Field(min_length=1)
    license_record: str = Field(min_length=1)
    provenance_ref: str = Field(min_length=1)
    audio_analysis_ref: str = Field(min_length=1)
    mood_traits: list[str] = Field(min_length=1)
    tempo_candidates_bpm: list[float] = Field(min_length=1)
    beat_grid_us: list[int] = Field(min_length=2)
    sections: list[BgmSection] = Field(min_length=1)

    @model_validator(mode="after")
    def timeline_is_consistent(self) -> Self:
        if self.beat_grid_us != sorted(set(self.beat_grid_us)) or any(
            value < 0 or value > self.duration_us for value in self.beat_grid_us
        ):
            raise ValueError("BGM beat grid 必须有序且位于音频时长内")
        if any(section.end_us > self.duration_us for section in self.sections):
            raise ValueError("BGM 段落超出音频时长")
        return self


class PreparationPackage(PluginModel):
    schema_version: Literal["1.0"] = "1.0"
    materials: list[PreparedMaterial] = Field(min_length=1)
    bgm: BgmPackage
    provenance_refs: list[str] = Field(min_length=1)
    retrieval_ids: list[str] = Field(min_length=1)
    reference_context: ReferenceContextBinding | None = None

    @model_validator(mode="after")
    def identities_are_unique_and_complete(self) -> Self:
        identities = [material.asset_id for material in self.materials]
        if self.bgm.asset_id in identities or len(identities) != len(set(identities)):
            raise ValueError("资源包素材 ID 重复")
        expected = {
            self.bgm.provenance_ref,
            *(material.provenance_ref for material in self.materials),
        }
        if not expected.issubset(set(self.provenance_refs)):
            raise ValueError("资源包 provenance 引用不完整")
        return self
