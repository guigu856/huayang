from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from .artifacts import ArtifactObject, ArtifactStore
from .errors import PluginError
from .models import (
    ArtifactEnvelope,
    ArtifactRef,
    ConfirmationAssurance,
    FreezeRecord,
    FreezeRef,
    StageEnvelope,
    StagePolicy,
    StageRun,
    StageStatus,
    TaskRun,
    TaskStatus,
    TaskType,
)
from .repository import WorkflowRepository

StagePolicyResolver = Callable[[TaskType, str], StagePolicy]

_TASK_STAGES: dict[TaskType, tuple[str, ...]] = {
    "reference_study": ("reference_study", "knowledge_publication"),
    "original_creation": (
        "creative_direction",
        "resource_preparation",
        "editing_specification",
        "execution",
    ),
    "reference_guided_creation": (
        "reference_study",
        "creative_direction",
        "resource_preparation",
        "editing_specification",
        "execution",
    ),
}


class WorkflowService:
    """执行任务阶段、访问句柄、产物提交与冻结状态转换。"""

    def __init__(
        self,
        repository: WorkflowRepository,
        artifact_store: ArtifactStore,
        *,
        policy_resolver: StagePolicyResolver | None = None,
        access_ttl: timedelta = timedelta(minutes=15),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.artifact_store = artifact_store
        self.policy_resolver = policy_resolver or _default_policy
        self.access_ttl = access_ttl
        self._now = now or (lambda: datetime.now(UTC))

    def create_task(
        self,
        task_type: TaskType,
        *,
        reference_analysis_ids: list[str] | None = None,
    ) -> TaskRun:
        now = self._now()
        first_stage = _TASK_STAGES[task_type][0]
        task = TaskRun(
            task_id=_new_id("task"),
            task_type=task_type,
            current_stage=first_stage,
            reference_analysis_ids=reference_analysis_ids or [],
            created_at=now,
            updated_at=now,
        )
        stage = StageRun(
            stage_run_id=_new_id("stage"),
            task_id=task.task_id,
            stage_type=first_stage,
        )
        self.repository.create_task(task, stage)
        return task

    def get_task(self, task_id: str) -> tuple[TaskRun, StageRun]:
        task = self.repository.get_task(task_id)
        return task, self.repository.get_current_stage(task)

    def authorize_stage_tool(
        self,
        access_handle: str,
        required_tool: str,
    ) -> tuple[TaskRun, StageRun]:
        """校验短期阶段句柄，并返回它绑定的任务和活动阶段。"""

        return self._validate_access(access_handle, required_tool=required_tool)

    def get_stage_envelope(self, task_id: str) -> StageEnvelope:
        task, stage = self.get_task(task_id)
        if stage.status == "stale":
            raise PluginError("stage_stale", "阶段已经失效")
        if task.status == "completed" or stage.status == "completed":
            raise PluginError("stage_not_allowed", "任务没有活动阶段")
        self._verify_input_freezes(stage)
        policy = self.policy_resolver(task.task_type, stage.stage_type)
        raw_handle = secrets.token_urlsafe(32)
        expires_at = self._now() + self.access_ttl
        self.repository.save_access_grant(
            handle_hash=_hash_handle(raw_handle),
            task_id=task.task_id,
            stage_run_id=stage.stage_run_id,
            task_revision=task.revision,
            stage_revision=stage.revision,
            allowed_tools=policy.allowed_tools,
            expires_at=expires_at.isoformat(),
        )
        allowed_resources = [
            f"huayang://stage-access/{raw_handle}/artifacts/{reference.artifact_id}"
            for reference in stage.input_artifact_refs
        ]
        return StageEnvelope(
            task_id=task.task_id,
            task_type=task.task_type,
            task_revision=task.revision,
            stage_run_id=stage.stage_run_id,
            stage=stage.stage_type,
            stage_revision=stage.revision,
            stage_access_handle=raw_handle,
            expires_at=expires_at,
            role_resource=policy.role_resource,
            skill_resources=policy.skill_resources,
            allowed_resources=allowed_resources,
            allowed_tools=policy.allowed_tools,
            input_artifacts=stage.input_artifact_refs,
            input_freezes=stage.input_freeze_refs,
            retrieval_scope=_retrieval_scope(task, stage),
            output_contract=policy.output_contract,
            confirmation_required=policy.confirmation_required,
        )

    def submit_artifact(
        self,
        *,
        access_handle: str,
        artifact_type: str,
        content: ArtifactObject,
        schema_version: str,
        producer_kind: Literal["agent", "component"],
        producer_id: str,
        primary: bool = False,
        parent_artifact_refs: list[ArtifactRef] | None = None,
        evidence_refs: list[str] | None = None,
        rule_version: str | None = None,
        skill_versions: list[str] | None = None,
        model_id: str | None = None,
        component_version: str | None = None,
    ) -> ArtifactEnvelope:
        task, stage = self._validate_access(access_handle, required_tool="workflow_submit_artifact")
        if stage.status != "running":
            raise PluginError("stage_not_allowed", "阶段主产物提交后只接受确认或重开")
        self.artifact_store.verify(content.uri, content.sha256)
        parents = parent_artifact_refs or []
        parent_keys = {(parent.artifact_id, parent.revision, parent.sha256) for parent in parents}
        if len(parent_keys) != len(parents):
            raise PluginError("dependency_closure_mismatch", "Artifact 父级引用存在重复")
        allowed_parent_keys = {
            (reference.artifact_id, reference.revision, reference.sha256)
            for reference in [
                *stage.input_artifact_refs,
                *stage.output_artifact_refs,
            ]
        }
        if not parent_keys.issubset(allowed_parent_keys):
            raise PluginError("stage_not_allowed", "Artifact 父级不属于当前阶段上下文")
        current_output_keys = {
            (reference.artifact_id, reference.revision, reference.sha256)
            for reference in stage.output_artifact_refs
        }
        if primary and not current_output_keys.issubset(parent_keys):
            raise PluginError("dependency_closure_mismatch", "阶段主产物缺少阶段子产物依赖")
        for parent in parents:
            self._verify_artifact_ref(parent, expected_task_id=task.task_id)
        now = self._now()
        artifact = ArtifactEnvelope.model_validate(
            {
                "artifact_id": _new_id("artifact"),
                "artifact_type": artifact_type,
                "task_id": task.task_id,
                "stage_run_id": stage.stage_run_id,
                "content_uri": content.uri,
                "content_sha256": content.sha256,
                "schema_version": schema_version,
                "producer_kind": producer_kind,
                "producer_id": producer_id,
                "rule_version": rule_version,
                "skill_versions": skill_versions or [],
                "model_id": model_id,
                "component_version": component_version,
                "parent_artifact_refs": parents,
                "evidence_refs": evidence_refs or [],
                "created_at": now,
            }
        )
        output_refs = [*stage.output_artifact_refs, artifact.as_ref()]
        policy = self.policy_resolver(task.task_type, stage.stage_type)
        status: StageStatus = stage.status
        primary_ref = stage.primary_output_artifact_ref
        task_status: TaskStatus = task.status
        if primary:
            primary_ref = artifact.as_ref()
            if policy.confirmation_required:
                status = "awaiting_confirmation"
                task_status = "awaiting_user"
            elif self._is_last_stage(task.task_type, stage.stage_type):
                status = "completed"
                task_status = "completed"
        updated_stage = stage.model_copy(
            update={
                "output_artifact_refs": output_refs,
                "primary_output_artifact_ref": primary_ref,
                "status": status,
                "revision": stage.revision + 1,
            }
        )
        updated_task = task.model_copy(
            update={
                "status": task_status,
                "revision": task.revision + 1,
                "updated_at": now,
            }
        )
        self.repository.submit_artifact(
            artifact=artifact,
            stage=updated_stage,
            task=updated_task,
        )
        return artifact

    def record_approval(
        self,
        *,
        access_handle: str,
        user_confirmation_ref: str,
        confirmation_assurance: ConfirmationAssurance,
        host_approval_receipt: str | None = None,
    ) -> FreezeRecord:
        task, stage = self._validate_access(access_handle, required_tool="workflow_record_approval")
        if stage.status != "awaiting_confirmation":
            raise PluginError("stage_not_allowed", "当前阶段不等待确认")
        if confirmation_assurance == "host_verified" and host_approval_receipt is None:
            raise PluginError("approval_receipt_invalid", "宿主校验确认缺少回执")
        primary_ref = stage.primary_output_artifact_ref
        if primary_ref is None:
            raise PluginError("artifact_not_found", "阶段主产物不存在")
        artifact = self._verify_artifact_ref(primary_ref, expected_task_id=task.task_id)
        primary_parent_keys = {
            (parent.artifact_id, parent.revision, parent.sha256)
            for parent in artifact.parent_artifact_refs
        }
        subordinate_output_keys = {
            (output.artifact_id, output.revision, output.sha256)
            for output in stage.output_artifact_refs
            if output != primary_ref
        }
        if not subordinate_output_keys.issubset(primary_parent_keys):
            raise PluginError(
                "dependency_closure_mismatch",
                "阶段主产物依赖闭包没有覆盖全部阶段子产物",
            )
        closure_hash = self._dependency_closure_hash(artifact, stage.input_freeze_refs)
        now = self._now()
        freeze = FreezeRecord(
            freeze_id=_new_id("freeze"),
            task_id=task.task_id,
            stage_run_id=stage.stage_run_id,
            artifact_id=artifact.artifact_id,
            artifact_revision=artifact.revision,
            artifact_sha256=artifact.content_sha256,
            input_freeze_refs=stage.input_freeze_refs,
            dependency_closure_sha256=closure_hash,
            user_confirmation_ref=user_confirmation_ref,
            confirmation_assurance=confirmation_assurance,
            host_approval_receipt=host_approval_receipt,
            expected_stage_revision=stage.revision,
            frozen_at=now,
        )
        approved_artifact = artifact.model_copy(update={"status": "approved"})
        approved_stage = stage.model_copy(
            update={"status": "approved", "revision": stage.revision + 1}
        )
        next_stage_type = self._next_stage(task.task_type, stage.stage_type)
        next_stage: StageRun | None = None
        if next_stage_type is None:
            updated_task = task.model_copy(
                update={
                    "status": "completed",
                    "revision": task.revision + 1,
                    "updated_at": now,
                }
            )
        else:
            updated_task = task.model_copy(
                update={
                    "status": "active",
                    "current_stage": next_stage_type,
                    "revision": task.revision + 1,
                    "updated_at": now,
                }
            )
            next_stage = StageRun(
                stage_run_id=_new_id("stage"),
                task_id=task.task_id,
                stage_type=next_stage_type,
                input_artifact_refs=[
                    *stage.input_artifact_refs,
                    approved_artifact.as_ref(),
                ],
                input_freeze_refs=[
                    *stage.input_freeze_refs,
                    freeze.as_ref(),
                ],
            )
        self.repository.approve_stage(
            artifact=approved_artifact,
            freeze=freeze,
            stage=approved_stage,
            task=updated_task,
            next_stage=next_stage,
        )
        return freeze

    def reopen_stage(self, *, access_handle: str, stage_type: str) -> StageRun:
        task, _ = self._validate_access(access_handle, required_tool="workflow_reopen_stage")
        stage_order = _TASK_STAGES[task.task_type]
        if stage_type not in stage_order:
            raise PluginError("stage_not_allowed", "目标阶段不属于当前任务")
        target_index = stage_order.index(stage_type)
        stages = self.repository.list_stages(task.task_id)
        stale: list[StageRun] = []
        for stage in stages:
            if stage_order.index(stage.stage_type) >= target_index and stage.status != "stale":
                stale.append(
                    stage.model_copy(update={"status": "stale", "revision": stage.revision + 1})
                )
        input_artifacts: list[ArtifactRef] = []
        input_freezes: list[FreezeRef] = []
        if target_index > 0:
            previous_type = stage_order[target_index - 1]
            previous = next(
                (
                    stage
                    for stage in reversed(stages)
                    if stage.stage_type == previous_type and stage.status == "approved"
                ),
                None,
            )
            if previous is None or previous.primary_output_artifact_ref is None:
                raise PluginError("stage_not_allowed", "目标阶段缺少已冻结上游")
            freeze = self.repository.get_freeze_for_artifact(
                previous.primary_output_artifact_ref.artifact_id
            )
            input_artifacts = [
                *previous.input_artifact_refs,
                previous.primary_output_artifact_ref,
            ]
            input_freezes = [
                *previous.input_freeze_refs,
                freeze.as_ref(),
            ]
        new_stage = StageRun(
            stage_run_id=_new_id("stage"),
            task_id=task.task_id,
            stage_type=stage_type,
            input_artifact_refs=input_artifacts,
            input_freeze_refs=input_freezes,
        )
        updated_task = task.model_copy(
            update={
                "status": "active",
                "current_stage": stage_type,
                "revision": task.revision + 1,
                "updated_at": self._now(),
            }
        )
        self.repository.reopen_stage(
            task=updated_task,
            stale_stages=stale,
            new_stage=new_stage,
        )
        return new_stage

    def read_artifact(self, *, access_handle: str, artifact_id: str) -> bytes:
        task, stage = self._validate_access(access_handle)
        allowed_ids = {reference.artifact_id for reference in stage.input_artifact_refs}
        allowed_ids.update(reference.artifact_id for reference in stage.output_artifact_refs)
        if artifact_id not in allowed_ids:
            raise PluginError("stage_not_allowed", "Artifact 不属于当前阶段上下文")
        artifact = self.repository.get_artifact(artifact_id)
        if artifact.task_id != task.task_id:
            raise PluginError("stage_not_allowed", "Artifact 不属于当前任务")
        return self.artifact_store.read_bytes(artifact.content_uri)

    def _validate_access(
        self,
        access_handle: str,
        *,
        required_tool: str | None = None,
    ) -> tuple[TaskRun, StageRun]:
        grant = self.repository.get_access_grant(_hash_handle(access_handle))
        expires_at = datetime.fromisoformat(str(grant["expires_at"]))
        if expires_at <= self._now():
            raise PluginError("stage_access_expired", "阶段访问句柄已过期")
        task = self.repository.get_task(str(grant["task_id"]))
        stage = self.repository.get_stage(str(grant["stage_run_id"]))
        if stage.status == "stale":
            raise PluginError("stage_stale", "阶段已经失效")
        if task.revision != grant["task_revision"]:
            raise PluginError("task_revision_conflict", "任务版本冲突")
        if stage.revision != grant["stage_revision"]:
            raise PluginError("stage_revision_conflict", "阶段版本冲突")
        if task.current_stage != stage.stage_type:
            raise PluginError("stage_not_allowed", "阶段不是任务当前阶段")
        if required_tool is not None and required_tool not in grant["allowed_tools"]:
            raise PluginError("stage_not_allowed", "工具不属于当前阶段")
        self._verify_input_freezes(stage)
        return task, stage

    def _verify_input_freezes(self, stage: StageRun) -> None:
        if len(stage.input_artifact_refs) != len(stage.input_freeze_refs):
            raise PluginError("dependency_closure_mismatch", "阶段输入冻结闭包不完整")
        for artifact_ref, freeze_ref in zip(
            stage.input_artifact_refs, stage.input_freeze_refs, strict=True
        ):
            if artifact_ref.artifact_id != freeze_ref.artifact_id:
                raise PluginError("dependency_closure_mismatch", "阶段输入冻结引用不匹配")
            artifact = self._verify_artifact_ref(artifact_ref)
            freeze = self.repository.get_freeze(freeze_ref.freeze_id)
            if freeze.as_ref() != freeze_ref or artifact.status != "approved":
                raise PluginError("dependency_closure_mismatch", "阶段输入冻结记录无效")
            expected_closure = self._dependency_closure_hash(artifact, freeze.input_freeze_refs)
            if expected_closure != freeze.dependency_closure_sha256:
                raise PluginError("dependency_closure_mismatch", "阶段输入依赖闭包失效")

    def _verify_artifact_ref(
        self,
        reference: ArtifactRef,
        *,
        expected_task_id: str | None = None,
    ) -> ArtifactEnvelope:
        artifact = self.repository.get_artifact(reference.artifact_id)
        if artifact.as_ref() != reference:
            raise PluginError("artifact_hash_mismatch", "Artifact 引用与记录不一致")
        if expected_task_id is not None and artifact.task_id != expected_task_id:
            raise PluginError("stage_not_allowed", "Artifact 不属于当前任务")
        self.artifact_store.verify(artifact.content_uri, artifact.content_sha256)
        return artifact

    def _dependency_closure_hash(
        self,
        artifact: ArtifactEnvelope,
        input_freezes: list[FreezeRef],
    ) -> str:
        artifacts: dict[str, ArtifactRef] = {}

        def visit(current: ArtifactEnvelope) -> None:
            reference = current.as_ref()
            if reference.artifact_id in artifacts:
                return
            artifacts[reference.artifact_id] = reference
            for parent_ref in current.parent_artifact_refs:
                visit(self._verify_artifact_ref(parent_ref, expected_task_id=current.task_id))

        visit(artifact)
        freezes: list[dict[str, str | int]] = []
        for freeze_ref in input_freezes:
            freeze = self.repository.get_freeze(freeze_ref.freeze_id)
            if freeze.as_ref() != freeze_ref:
                raise PluginError("dependency_closure_mismatch", "上游冻结引用不一致")
            freezes.append(
                {
                    **freeze_ref.model_dump(mode="json"),
                    "dependency_closure_sha256": freeze.dependency_closure_sha256,
                }
            )
        payload = {
            "artifacts": [
                reference.model_dump(mode="json")
                for reference in sorted(artifacts.values(), key=lambda value: value.artifact_id)
            ],
            "input_freezes": sorted(freezes, key=lambda value: str(value["freeze_id"])),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _next_stage(task_type: TaskType, stage_type: str) -> str | None:
        stages = _TASK_STAGES[task_type]
        index = stages.index(stage_type)
        return None if index == len(stages) - 1 else stages[index + 1]

    @staticmethod
    def _is_last_stage(task_type: TaskType, stage_type: str) -> bool:
        return _TASK_STAGES[task_type][-1] == stage_type


def _default_policy(task_type: TaskType, stage_type: str) -> StagePolicy:
    confirmation_required = stage_type in {
        "reference_study",
        "creative_direction",
        "resource_preparation",
        "editing_specification",
    }
    tools = [
        "workflow_submit_artifact",
        "workflow_reopen_stage",
    ]
    if confirmation_required:
        tools.append("workflow_record_approval")
    stage_number = {
        "creative_direction": "stage1",
        "resource_preparation": "stage2",
        "editing_specification": "stage3",
    }.get(stage_type)
    if stage_number is not None:
        tools.append("knowledge_search")
    return StagePolicy(
        role_resource=f"huayang://rules/{stage_type}",
        skill_resources=[f"huayang://skills/{stage_type}"],
        allowed_tools=tools,
        output_contract=f"huayang://schemas/{stage_type}",
        confirmation_required=confirmation_required,
    )


def _retrieval_scope(task: TaskRun, stage: StageRun) -> dict[str, str | list[str]]:
    stage_number = {
        "creative_direction": "stage1",
        "resource_preparation": "stage2",
        "editing_specification": "stage3",
    }.get(stage.stage_type, stage.stage_type)
    return {
        "stage": stage_number,
        "task_private_analysis_ids": task.reference_analysis_ids,
        "publication_status": "active",
        "visibility": "creation_shared",
        "transferability": "reusable_mechanism",
    }


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _hash_handle(handle: str) -> str:
    return hashlib.sha256(handle.encode("utf-8")).hexdigest()
