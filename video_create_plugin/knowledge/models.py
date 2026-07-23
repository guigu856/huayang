from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from video_create_plugin.models import ArtifactRef, PluginModel

KnowledgeCollection = Literal["creation_knowledge", "reference_evidence"]
CreationStage = Literal["stage1", "stage2", "stage3"]
KnowledgeVisibility = Literal["creation_shared", "task_private", "evidence_only"]
KnowledgeTransferability = Literal[
    "reusable_mechanism",
    "reference_specific",
    "uncertain",
]
PublicationStatus = Literal["active", "superseded"]

EMBEDDING_VERSION = "zh-char-ngram-hash-v1-d384-n1-3"
EMBEDDING_DIMENSION = 384
CHUNKER_VERSION = "knowledge-unit-v1"

_STAGE_ORDER = {"stage1": 1, "stage2": 2, "stage3": 3}


class KnowledgeRecord(PluginModel):
    """一条可发布的知识或参考证据单元。"""

    knowledge_id: str | None = None
    publication_id: str | None = None
    collection: KnowledgeCollection
    source_task_id: str = Field(min_length=1)
    source_report_ref: ArtifactRef
    source_artifact_refs: list[ArtifactRef] = Field(min_length=1)
    analysis_version: str = Field(min_length=1)
    applicable_stages: list[CreationStage] = Field(min_length=1)
    knowledge_type: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    visibility: KnowledgeVisibility
    transferability: KnowledgeTransferability
    content: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    fact_status: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    source_time_range_us: tuple[int, int] | None = None
    video_type_tags: list[str] = Field(default_factory=list)
    technique_tags: list[str] = Field(default_factory=list)
    music_layer_tags: list[str] = Field(default_factory=list)
    energy_phase: str | None = None
    granularity: Literal["global", "section", "rhythm_unit", "shot"]
    chunker_version: str = CHUNKER_VERSION
    embedding_version: str = EMBEDDING_VERSION
    embedding_dimension: int = EMBEDDING_DIMENSION

    @field_validator("source_artifact_refs")
    @classmethod
    def normalize_artifact_refs(cls, values: list[ArtifactRef]) -> list[ArtifactRef]:
        by_identity = {(value.artifact_id, value.revision, value.sha256): value for value in values}
        return [by_identity[key] for key in sorted(by_identity)]

    @field_validator(
        "evidence_refs",
        "video_type_tags",
        "technique_tags",
        "music_layer_tags",
    )
    @classmethod
    def normalize_string_lists(cls, values: list[str]) -> list[str]:
        return sorted(set(values))

    @field_validator("applicable_stages")
    @classmethod
    def normalize_stages(cls, values: list[CreationStage]) -> list[CreationStage]:
        return sorted(set(values), key=_STAGE_ORDER.__getitem__)

    @field_validator("content")
    @classmethod
    def content_is_not_whitespace(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("知识正文不得为空")
        return normalized

    @model_validator(mode="after")
    def collection_policy_is_consistent(self) -> Self:
        if self.collection == "creation_knowledge" and (
            self.visibility != "creation_shared" or self.transferability != "reusable_mechanism"
        ):
            raise ValueError("共享创作知识必须是可迁移机制")
        if self.collection == "reference_evidence" and (
            self.visibility != "evidence_only" or self.transferability == "reusable_mechanism"
        ):
            raise ValueError("参考证据必须保持证据专属属性")
        if self.source_time_range_us is not None:
            start, end = self.source_time_range_us
            if start < 0 or end <= start:
                raise ValueError("证据时间范围无效")
        if self.embedding_version != EMBEDDING_VERSION:
            raise ValueError("知识单元嵌入版本不匹配")
        if self.embedding_dimension != EMBEDDING_DIMENSION:
            raise ValueError("知识单元向量维度不匹配")
        return self


class PublicationRequest(PluginModel):
    source_task_id: str = Field(min_length=1)
    source_report_ref: ArtifactRef
    source_media_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_revision: int = Field(ge=1)
    freeze_id: str = Field(min_length=1)
    records: list[KnowledgeRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def record_sources_match_publication(self) -> Self:
        seen: set[str] = set()
        for record in self.records:
            if record.knowledge_id is not None or record.publication_id is not None:
                raise ValueError("待发布知识不得预置发布标识")
            if record.source_task_id != self.source_task_id:
                raise ValueError("知识单元与发布任务不一致")
            if record.source_report_ref != self.source_report_ref:
                raise ValueError("知识单元与发布报告不一致")
            digest_key = record.model_dump_json(exclude={"knowledge_id", "publication_id"})
            if digest_key in seen:
                raise ValueError("同一发布中存在重复知识单元")
            seen.add(digest_key)
        return self


class Publication(PluginModel):
    publication_id: str = Field(pattern=r"^publication_[0-9a-f]{16}$")
    source_task_id: str = Field(min_length=1)
    source_report_ref: ArtifactRef
    source_media_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication_revision: int = Field(ge=1)
    status: PublicationStatus
    supersedes_publication_id: str | None = None
    freeze_id: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_counts: dict[KnowledgeCollection, int]
    embedding_version: str = EMBEDDING_VERSION
    embedding_dimension: int = EMBEDDING_DIMENSION
    created_at: datetime


class Query(PluginModel):
    text: str = Field(min_length=1)
    stage: CreationStage
    knowledge_types: list[str] = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=100)
    current_task_id: str | None = None
    source_task_id: str | None = None
    active: Literal[True] = True
    visibility: Literal["creation_shared"] = "creation_shared"
    transferability: Literal["reusable_mechanism"] = "reusable_mechanism"

    @field_validator("knowledge_types")
    @classmethod
    def normalize_knowledge_types(cls, values: list[str]) -> list[str]:
        normalized = sorted(set(values))
        if any(not value or not value.replace("_", "a").isalnum() for value in normalized):
            raise ValueError("knowledge_type 过滤值无效")
        return normalized

    @field_validator("text")
    @classmethod
    def query_is_not_whitespace(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("查询文本不得为空")
        return normalized


class Hit(PluginModel):
    knowledge_id: str
    publication_id: str
    collection: KnowledgeCollection
    content: str
    score: float = Field(ge=-1.0, le=1.0)
    match_reasons: list[str]
    source_task_id: str
    source_report_ref: ArtifactRef
    source_artifact_refs: list[ArtifactRef]
    evidence_refs: list[str]
    applicable_stages: list[CreationStage]
    knowledge_type: str
    visibility: KnowledgeVisibility
    transferability: KnowledgeTransferability
    fact_status: str
    confidence: float = Field(ge=0.0, le=1.0)
    embedding_version: str


class SearchResult(PluginModel):
    """本次证据与共享知识保持独立，字段顺序表达读取优先级。"""

    current_task_reference_evidence: list[Hit]
    shared_creation_knowledge: list[Hit]


KnowledgePublication = Publication
KnowledgeQuery = Query
KnowledgeHit = Hit
