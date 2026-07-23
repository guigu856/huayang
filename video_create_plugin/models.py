from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TaskType = Literal[
    "reference_study",
    "original_creation",
    "reference_guided_creation",
]
TaskStatus = Literal["active", "awaiting_user", "completed"]
StageStatus = Literal[
    "not_started",
    "running",
    "awaiting_confirmation",
    "approved",
    "stale",
    "completed",
]
ArtifactStatus = Literal["draft", "submitted", "approved", "superseded"]
ConfirmationAssurance = Literal["audit_only", "host_verified"]


class PluginModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ArtifactRef(PluginModel):
    artifact_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FreezeRef(PluginModel):
    freeze_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    artifact_revision: int = Field(ge=1)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReferenceContextBinding(PluginModel):
    source_report_ref: ArtifactRef
    source_report_freeze_ref: FreezeRef
    report_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_stage: Literal[
        "creative_direction",
        "resource_preparation",
        "editing_specification",
    ]
    stage_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TaskRun(PluginModel):
    task_id: str = Field(pattern=r"^task_[0-9a-f]{16}$")
    task_type: TaskType
    status: TaskStatus = "active"
    current_stage: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    reference_analysis_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class StageRun(PluginModel):
    stage_run_id: str = Field(pattern=r"^stage_[0-9a-f]{16}$")
    task_id: str = Field(pattern=r"^task_[0-9a-f]{16}$")
    stage_type: str = Field(min_length=1)
    status: StageStatus = "running"
    input_artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    input_freeze_refs: list[FreezeRef] = Field(default_factory=list)
    output_artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    primary_output_artifact_ref: ArtifactRef | None = None
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def primary_output_is_part_of_outputs(self) -> Self:
        if (
            self.primary_output_artifact_ref is not None
            and self.primary_output_artifact_ref not in self.output_artifact_refs
        ):
            raise ValueError("主产物必须属于阶段输出")
        return self


class ArtifactEnvelope(PluginModel):
    artifact_id: str = Field(pattern=r"^artifact_[0-9a-f]{16}$")
    artifact_type: str = Field(min_length=1)
    task_id: str = Field(pattern=r"^task_[0-9a-f]{16}$")
    stage_run_id: str = Field(pattern=r"^stage_[0-9a-f]{16}$")
    revision: int = Field(default=1, ge=1)
    status: ArtifactStatus = "submitted"
    content_uri: str = Field(pattern=r"^video-create-object://sha256/[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: str = Field(min_length=1)
    producer_kind: Literal["agent", "component"]
    producer_id: str = Field(min_length=1)
    rule_version: str | None = None
    skill_versions: list[str] = Field(default_factory=list)
    model_id: str | None = None
    component_version: str | None = None
    parent_artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: datetime

    @model_validator(mode="after")
    def producer_metadata_matches_kind(self) -> Self:
        if self.producer_kind == "agent" and self.model_id is None:
            raise ValueError("Agent 产物必须记录 model_id")
        if self.producer_kind == "component" and self.component_version is None:
            raise ValueError("组件产物必须记录 component_version")
        return self

    def as_ref(self) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=self.artifact_id,
            revision=self.revision,
            sha256=self.content_sha256,
        )


class FreezeRecord(PluginModel):
    freeze_id: str = Field(pattern=r"^freeze_[0-9a-f]{16}$")
    task_id: str = Field(pattern=r"^task_[0-9a-f]{16}$")
    stage_run_id: str = Field(pattern=r"^stage_[0-9a-f]{16}$")
    artifact_id: str = Field(pattern=r"^artifact_[0-9a-f]{16}$")
    artifact_revision: int = Field(ge=1)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_freeze_refs: list[FreezeRef] = Field(default_factory=list)
    dependency_closure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    user_confirmation_ref: str = Field(min_length=1)
    confirmation_assurance: ConfirmationAssurance
    host_approval_receipt: str | None = None
    expected_stage_revision: int = Field(ge=1)
    frozen_at: datetime

    @model_validator(mode="after")
    def host_receipt_matches_assurance(self) -> Self:
        if self.confirmation_assurance == "host_verified" and self.host_approval_receipt is None:
            raise ValueError("宿主校验确认必须携带回执")
        return self

    def as_ref(self) -> FreezeRef:
        return FreezeRef(
            freeze_id=self.freeze_id,
            artifact_id=self.artifact_id,
            artifact_revision=self.artifact_revision,
            artifact_sha256=self.artifact_sha256,
        )


class StagePolicy(PluginModel):
    role_resource: str = Field(pattern=r"^huayang://rules/")
    skill_resources: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    output_contract: str = Field(pattern=r"^huayang://schemas/")
    confirmation_required: bool = False

    @field_validator("skill_resources")
    @classmethod
    def skill_resource_uris_are_valid(cls, values: list[str]) -> list[str]:
        if any(not value.startswith("huayang://skills/") for value in values):
            raise ValueError("skill resource URI 无效")
        return values


class StageEnvelope(PluginModel):
    task_id: str
    task_type: TaskType
    task_revision: int = Field(ge=1)
    stage_run_id: str
    stage: str
    stage_revision: int = Field(ge=1)
    stage_access_handle: str = Field(min_length=32)
    expires_at: datetime
    role_resource: str
    skill_resources: list[str]
    allowed_resources: list[str]
    allowed_tools: list[str]
    input_artifacts: list[ArtifactRef]
    input_freezes: list[FreezeRef]
    retrieval_scope: dict[str, str | list[str]]
    output_contract: str
    confirmation_required: bool
