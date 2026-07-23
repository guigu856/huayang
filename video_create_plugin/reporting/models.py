from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EpistemicStatus = Literal["fact", "inference", "opinion", "unverified"]
CreationStage = Literal[
    "creative_direction",
    "resource_preparation",
    "editing_specification",
]


def _normalized_evidence_refs(values: list[str]) -> list[str]:
    if any(not value or value != value.strip() for value in values):
        raise ValueError("证据引用必须是非空且无首尾空白的字符串")
    if len(values) != len(set(values)):
        raise ValueError("证据引用不得重复")
    return sorted(values)


class ReportingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Claim(ReportingModel):
    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    status: EpistemicStatus
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("claim_id", "text")
    @classmethod
    def text_is_trimmed(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("文本不得包含首尾空白")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_are_canonical(cls, values: list[str]) -> list[str]:
        return _normalized_evidence_refs(values)

    @model_validator(mode="after")
    def factual_claim_has_evidence(self) -> Self:
        if self.status == "fact" and not self.evidence_refs:
            raise ValueError("事实性断言必须关联证据")
        return self


class EvidenceBackedModel(ReportingModel):
    evidence_refs: list[str] = Field(min_length=1)

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_are_canonical(cls, values: list[str]) -> list[str]:
        return _normalized_evidence_refs(values)


class VideoOverview(EvidenceBackedModel):
    source_name: str = Field(min_length=1)
    duration_us: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_rate_numerator: int = Field(gt=0)
    frame_rate_denominator: int = Field(gt=0)
    summary: Claim
    content_goal: Claim
    editing_identity: list[Claim] = Field(min_length=1)


class TempoHypothesis(ReportingModel):
    bpm: float = Field(gt=0, le=400)
    confidence: float = Field(ge=0, le=1)
    status: EpistemicStatus
    basis: Claim
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_are_canonical(cls, values: list[str]) -> list[str]:
        return _normalized_evidence_refs(values)

    @model_validator(mode="after")
    def factual_tempo_has_evidence(self) -> Self:
        if self.status == "fact" and not self.evidence_refs:
            raise ValueError("事实性节奏结论必须关联证据")
        return self


class BgmSection(EvidenceBackedModel):
    section_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    start_timestamp_us: int = Field(ge=0)
    end_timestamp_us: int = Field(gt=0)
    energy_level: float = Field(ge=0, le=1)
    musical_function: Claim
    energy_direction: Claim
    editing_relation: Claim

    @model_validator(mode="after")
    def time_range_is_valid(self) -> Self:
        if self.end_timestamp_us <= self.start_timestamp_us:
            raise ValueError("BGM 段落结束时间必须晚于开始时间")
        return self


class BgmAnalysis(EvidenceBackedModel):
    audio_scope: Literal["mixed_program_audio", "isolated_bgm", "unknown"]
    duration_us: int = Field(gt=0)
    tempo_hypotheses: list[TempoHypothesis] = Field(default_factory=list)
    sections: list[BgmSection] = Field(min_length=1)
    rhythm_layers: list[Claim] = Field(min_length=1)
    sound_layer_changes: list[Claim] = Field(default_factory=list)
    energy_flow: list[Claim] = Field(min_length=1)
    editing_following: list[Claim] = Field(min_length=1)

    @model_validator(mode="after")
    def sections_follow_timeline(self) -> Self:
        previous_end = 0
        seen_ids: set[str] = set()
        for section in self.sections:
            if section.section_id in seen_ids:
                raise ValueError("BGM 段落 ID 不得重复")
            if section.start_timestamp_us != previous_end:
                raise ValueError("BGM 段落必须从零开始并连续覆盖完整音频")
            if section.end_timestamp_us > self.duration_us:
                raise ValueError("BGM 段落超出音频时长")
            seen_ids.add(section.section_id)
            previous_end = section.end_timestamp_us
        if previous_end != self.duration_us:
            raise ValueError("BGM 段落必须连续覆盖完整音频")
        return self


class VisualLayer(EvidenceBackedModel):
    layer_id: str = Field(min_length=1)
    layer_type: str = Field(min_length=1)
    z_index: int
    description: Claim


class EditAction(EvidenceBackedModel):
    action_id: str = Field(min_length=1)
    action_type: str = Field(min_length=1)
    start_timestamp_us: int = Field(ge=0)
    end_timestamp_us: int = Field(ge=0)
    target_layer_ids: list[str] = Field(min_length=1)
    description: Claim
    timing_relation_to_music: Claim

    @model_validator(mode="after")
    def time_range_is_valid(self) -> Self:
        if self.end_timestamp_us < self.start_timestamp_us:
            raise ValueError("剪辑动作结束时间不得早于开始时间")
        return self


class EffectObservation(EvidenceBackedModel):
    effect_id: str = Field(min_length=1)
    effect_type: str = Field(min_length=1)
    target_scope: Literal["layer", "composition", "transition"]
    target_layer_ids: list[str] = Field(default_factory=list)
    description: Claim

    @model_validator(mode="after")
    def layer_scope_has_target(self) -> Self:
        if self.target_scope == "layer" and not self.target_layer_ids:
            raise ValueError("图层级效果必须标明目标图层")
        return self


class ReferenceShotAnalysis(EvidenceBackedModel):
    shot_id: str = Field(min_length=1)
    start_timestamp_us: int = Field(ge=0)
    end_timestamp_us: int = Field(gt=0)
    summary: Claim
    layers: list[VisualLayer] = Field(min_length=1)
    actions: list[EditAction] = Field(default_factory=list)
    effects: list[EffectObservation] = Field(default_factory=list)
    sound_relation: Claim
    preceding_rhythm_role: Claim
    following_rhythm_role: Claim

    @model_validator(mode="after")
    def internal_references_are_valid(self) -> Self:
        if self.end_timestamp_us <= self.start_timestamp_us:
            raise ValueError("镜头结束时间必须晚于开始时间")

        layer_ids = [layer.layer_id for layer in self.layers]
        if len(layer_ids) != len(set(layer_ids)):
            raise ValueError("同一镜头内图层 ID 不得重复")
        known_layers = set(layer_ids)

        action_ids: set[str] = set()
        for action in self.actions:
            if action.action_id in action_ids:
                raise ValueError("同一镜头内动作 ID 不得重复")
            if not set(action.target_layer_ids).issubset(known_layers):
                raise ValueError("剪辑动作引用了未知图层")
            if (
                action.start_timestamp_us < self.start_timestamp_us
                or action.end_timestamp_us > self.end_timestamp_us
            ):
                raise ValueError("剪辑动作时间超出所属镜头")
            action_ids.add(action.action_id)

        effect_ids: set[str] = set()
        for effect in self.effects:
            if effect.effect_id in effect_ids:
                raise ValueError("同一镜头内效果 ID 不得重复")
            if not set(effect.target_layer_ids).issubset(known_layers):
                raise ValueError("效果引用了未知图层")
            effect_ids.add(effect.effect_id)
        return self


class RhythmUnit(EvidenceBackedModel):
    unit_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    start_timestamp_us: int = Field(ge=0)
    end_timestamp_us: int = Field(gt=0)
    shot_ids: list[str] = Field(min_length=1)
    music_pattern: Claim
    visual_sequence: list[Claim] = Field(min_length=1)
    build_up_role: Claim
    release_role: Claim
    transfer_rule: Claim

    @model_validator(mode="after")
    def time_range_and_shots_are_valid(self) -> Self:
        if self.end_timestamp_us <= self.start_timestamp_us:
            raise ValueError("节奏单元结束时间必须晚于开始时间")
        if len(self.shot_ids) != len(set(self.shot_ids)):
            raise ValueError("节奏单元内镜头引用不得重复")
        return self


class EditingGrammar(ReportingModel):
    rhythm_units: list[RhythmUnit] = Field(min_length=1)
    cutting_rules: list[Claim] = Field(default_factory=list)
    layering_rules: list[Claim] = Field(default_factory=list)
    motion_rules: list[Claim] = Field(default_factory=list)
    transition_rules: list[Claim] = Field(default_factory=list)
    density_rules: list[Claim] = Field(default_factory=list)
    reusable_patterns: list[Claim] = Field(min_length=1)


class ViewingExperience(ReportingModel):
    overall_target: Claim
    emotional_effects: list[Claim] = Field(min_length=1)
    pacing_and_breathing: list[Claim] = Field(min_length=1)
    richness_and_layering: list[Claim] = Field(default_factory=list)
    attention_guidance: list[Claim] = Field(default_factory=list)


class StageKnowledgeProjection(ReportingModel):
    stage: CreationStage
    knowledge_types: list[str] = Field(min_length=1)
    retrieval_tags: list[str] = Field(min_length=1)
    recommendations: list[Claim] = Field(min_length=1)

    @field_validator("knowledge_types", "retrieval_tags")
    @classmethod
    def values_are_unique_and_trimmed(cls, values: list[str]) -> list[str]:
        if any(not value or value != value.strip() for value in values):
            raise ValueError("检索字段必须是非空且无首尾空白的字符串")
        if len(values) != len(set(values)):
            raise ValueError("检索字段不得重复")
        return sorted(values)


class CreationContextProjection(ReportingModel):
    core_goal: Claim
    transferable_patterns: list[Claim] = Field(min_length=1)
    non_transferable_specifics: list[Claim] = Field(min_length=1)
    new_material_reconstruction_guidance: list[Claim] = Field(min_length=1)
    stage_projections: list[StageKnowledgeProjection] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def all_creation_stages_are_present(self) -> Self:
        stages = {projection.stage for projection in self.stage_projections}
        expected: set[CreationStage] = {
            "creative_direction",
            "resource_preparation",
            "editing_specification",
        }
        if stages != expected:
            raise ValueError("创作映射必须覆盖创意方案、资源筹备和剪辑规格三个阶段")
        return self


class ReferenceReportContent(ReportingModel):
    video_overview: VideoOverview
    bgm_analysis: BgmAnalysis
    shot_analyses: list[ReferenceShotAnalysis] = Field(min_length=1)
    editing_grammar: EditingGrammar
    viewing_experience: ViewingExperience
    creation_context_projection: CreationContextProjection

    @model_validator(mode="after")
    def timeline_and_cross_references_are_valid(self) -> Self:
        shot_ids: set[str] = set()
        previous_shot_end = 0
        for shot in self.shot_analyses:
            if shot.shot_id in shot_ids:
                raise ValueError("逐镜分析中的镜头 ID 不得重复")
            if shot.start_timestamp_us != previous_shot_end:
                raise ValueError("逐镜分析必须从零开始并连续覆盖完整视频")
            if shot.end_timestamp_us > self.video_overview.duration_us:
                raise ValueError("镜头时间超出视频时长")
            shot_ids.add(shot.shot_id)
            previous_shot_end = shot.end_timestamp_us
        if previous_shot_end != self.video_overview.duration_us:
            raise ValueError("逐镜分析必须连续覆盖完整视频")

        unit_ids: set[str] = set()
        previous_unit_end = 0
        for unit in self.editing_grammar.rhythm_units:
            if unit.unit_id in unit_ids:
                raise ValueError("节奏单元 ID 不得重复")
            if unit.start_timestamp_us < previous_unit_end:
                raise ValueError("节奏单元必须按时间排序且不得重叠")
            if unit.end_timestamp_us > self.video_overview.duration_us:
                raise ValueError("节奏单元时间超出视频时长")
            if not set(unit.shot_ids).issubset(shot_ids):
                raise ValueError("节奏单元引用了未知镜头")
            unit_ids.add(unit.unit_id)
            previous_unit_end = unit.end_timestamp_us
        return self


def canonical_json_bytes(model: BaseModel) -> bytes:
    payload = model.model_dump(mode="json", by_alias=True, exclude_none=False)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return serialized.encode("utf-8")


def collect_evidence_refs(value: Any) -> set[str]:
    references: set[str] = set()
    if isinstance(value, BaseModel):
        for field_name in type(value).model_fields:
            field_value = getattr(value, field_name)
            if field_name == "evidence_refs":
                references.update(field_value)
            else:
                references.update(collect_evidence_refs(field_value))
    elif isinstance(value, list | tuple):
        for item in value:
            references.update(collect_evidence_refs(item))
    elif isinstance(value, dict):
        for item in value.values():
            references.update(collect_evidence_refs(item))
    return references


class ReferenceReportManifest(ReportingModel):
    schema_version: str = Field(default="1.0.0", min_length=1)
    analysis_id: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs: list[str] = Field(min_length=1)
    content: ReferenceReportContent

    @field_validator("evidence_refs")
    @classmethod
    def evidence_refs_are_canonical(cls, values: list[str]) -> list[str]:
        return _normalized_evidence_refs(values)

    @model_validator(mode="after")
    def evidence_and_hash_match_content(self) -> Self:
        expected_evidence = sorted(collect_evidence_refs(self.content))
        if self.evidence_refs != expected_evidence:
            raise ValueError("清单证据引用必须与报告内容完全一致")
        expected_hash = hashlib.sha256(canonical_json_bytes(self.content)).hexdigest()
        if self.report_content_sha256 != expected_hash:
            raise ValueError("报告内容哈希与规范化内容不一致")
        return self

    @classmethod
    def build(
        cls,
        *,
        analysis_id: str,
        source_sha256: str,
        content: ReferenceReportContent,
        schema_version: str = "1.0.0",
    ) -> ReferenceReportManifest:
        return cls(
            schema_version=schema_version,
            analysis_id=analysis_id,
            source_sha256=source_sha256,
            report_content_sha256=hashlib.sha256(canonical_json_bytes(content)).hexdigest(),
            evidence_refs=sorted(collect_evidence_refs(content)),
            content=content,
        )
