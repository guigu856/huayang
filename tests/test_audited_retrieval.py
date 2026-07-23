from __future__ import annotations

from pathlib import Path

import pytest

from video_create_plugin import ArtifactStore, WorkflowService
from video_create_plugin.context import ContextCatalog
from video_create_plugin.errors import PluginError
from video_create_plugin.knowledge import KnowledgeRecord, KnowledgeStore, PublicationRequest
from video_create_plugin.models import ArtifactRef
from video_create_plugin.repository import WorkflowRepository
from video_create_plugin.retrieval import AuditedKnowledgeService


def _publish_stage_knowledge(store: KnowledgeStore) -> None:
    report_ref = ArtifactRef(
        artifact_id="artifact_0123456789abcdef",
        revision=1,
        sha256="a" * 64,
    )
    records = []
    for stage, knowledge_type, content in (
        ("stage1", "core_mechanism", "约一秒主镜头硬切之间插入短时画中画"),
        ("stage2", "asset_selection_traits", "选择运动方向一致且主体清晰的不同素材"),
        ("stage3", "pip_layer_pattern", "画中画在主镜头内部进入并在下一次硬切前退出"),
    ):
        records.append(
            KnowledgeRecord.model_validate(
                {
                    "collection": "creation_knowledge",
                    "source_task_id": "task_reference_source",
                    "source_report_ref": report_ref,
                    "source_artifact_refs": [report_ref],
                    "analysis_version": "v1",
                    "applicable_stages": [stage],
                    "knowledge_type": knowledge_type,
                    "visibility": "creation_shared",
                    "transferability": "reusable_mechanism",
                    "content": content,
                    "evidence_refs": [f"evidence://{stage}"],
                    "fact_status": "inference",
                    "confidence": 0.9,
                    "granularity": "rhythm_unit",
                }
            )
        )
    store.publish(
        PublicationRequest(
            source_task_id="task_reference_source",
            source_report_ref=report_ref,
            source_media_sha256="b" * 64,
            publication_revision=1,
            freeze_id="freeze_reference",
            records=records,
        )
    )


def _approve_current_stage(
    workflow: WorkflowService,
    store: ArtifactStore,
    task_id: str,
) -> None:
    envelope = workflow.get_stage_envelope(task_id)
    content = store.put_text("阶段产物")
    workflow.submit_artifact(
        access_handle=envelope.stage_access_handle,
        artifact_type=f"{envelope.stage}_artifact",
        content=content,
        schema_version="1.0",
        producer_kind="agent",
        producer_id="test-agent",
        model_id="test-model",
        primary=True,
    )
    approval_envelope = workflow.get_stage_envelope(envelope.task_id)
    workflow.record_approval(
        access_handle=approval_envelope.stage_access_handle,
        user_confirmation_ref=f"message:{envelope.stage}",
        confirmation_assurance="audit_only",
    )


def test_original_creation_stage_one_two_three_have_real_shared_retrievals(
    tmp_path: Path,
) -> None:
    knowledge = KnowledgeStore(tmp_path / "knowledge")
    _publish_stage_knowledge(knowledge)
    artifact_store = ArtifactStore(tmp_path / "objects")
    workflow = WorkflowService(
        WorkflowRepository(tmp_path / "workflow.sqlite3"),
        artifact_store,
        policy_resolver=ContextCatalog().policy,
    )
    task = workflow.create_task("original_creation")
    audited = AuditedKnowledgeService(
        knowledge,
        workflow,
        tmp_path / "retrieval.sqlite3",
    )
    requests = (
        ("stage1", "一秒硬切和画中画的总体机制", ["core_mechanism"]),
        ("stage2", "适合一秒硬切的不同素材", ["asset_selection_traits"]),
        ("stage3", "画中画进入退出与硬切的组合", ["pip_layer_pattern"]),
    )
    for index, (stage, text, types) in enumerate(requests):
        envelope = workflow.get_stage_envelope(task.task_id)
        audit, result = audited.search(
            access_handle=envelope.stage_access_handle,
            text=text,
            knowledge_types=types,
        )
        assert audit.stage == stage
        assert result.shared_creation_knowledge
        assert audited.validate_stage_retrievals(
            task_id=task.task_id,
            stage_run_id=envelope.stage_run_id,
            stage=stage,
            retrieval_ids=[audit.retrieval_id],
        ) == [audit]
        if index < 2:
            _approve_current_stage(workflow, artifact_store, task.task_id)


def test_reopened_stage_rejects_retrieval_from_previous_stage_run(
    tmp_path: Path,
) -> None:
    knowledge = KnowledgeStore(tmp_path / "knowledge")
    _publish_stage_knowledge(knowledge)
    artifact_store = ArtifactStore(tmp_path / "objects")
    workflow = WorkflowService(
        WorkflowRepository(tmp_path / "workflow.sqlite3"),
        artifact_store,
        policy_resolver=ContextCatalog().policy,
    )
    task = workflow.create_task("original_creation")
    audited = AuditedKnowledgeService(
        knowledge,
        workflow,
        tmp_path / "retrieval.sqlite3",
    )
    first = workflow.get_stage_envelope(task.task_id)
    audit, _ = audited.search(
        access_handle=first.stage_access_handle,
        text="一秒硬切和画中画的总体机制",
        knowledge_types=["core_mechanism"],
    )
    _approve_current_stage(workflow, artifact_store, task.task_id)
    stage_two = workflow.get_stage_envelope(task.task_id)
    workflow.reopen_stage(
        access_handle=stage_two.stage_access_handle,
        stage_type="creative_direction",
    )
    reopened = workflow.get_stage_envelope(task.task_id)

    with pytest.raises(PluginError) as error:
        audited.validate_stage_retrievals(
            task_id=task.task_id,
            stage_run_id=reopened.stage_run_id,
            stage="stage1",
            retrieval_ids=[audit.retrieval_id],
        )

    assert error.value.code == "knowledge_filter_required"
