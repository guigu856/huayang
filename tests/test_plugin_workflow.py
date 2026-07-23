from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from video_create_plugin import ArtifactStore, PluginError, WorkflowService
from video_create_plugin.repository import WorkflowRepository


def _service(
    tmp_path: Path,
    *,
    now: list[datetime] | None = None,
    ttl: timedelta = timedelta(minutes=15),
) -> tuple[WorkflowService, ArtifactStore]:
    store = ArtifactStore(tmp_path / "objects")
    repository = WorkflowRepository(tmp_path / "workflow.sqlite3")
    service = WorkflowService(
        repository,
        store,
        access_ttl=ttl,
        now=None if now is None else lambda: now[0],
    )
    return service, store


def _submit_component_artifact(
    service: WorkflowService,
    store: ArtifactStore,
    task_id: str,
    content: str,
    *,
    primary: bool,
):
    envelope = service.get_stage_envelope(task_id)
    return service.submit_artifact(
        access_handle=envelope.stage_access_handle,
        artifact_type="test_artifact",
        content=store.put_text(content),
        schema_version="1.0",
        producer_kind="component",
        producer_id="test-component",
        component_version="1.0.0",
        primary=primary,
    )


def _submit_primary_and_approve(
    service: WorkflowService,
    store: ArtifactStore,
    task_id: str,
    content: str,
):
    envelope = service.get_stage_envelope(task_id)
    artifact = service.submit_artifact(
        access_handle=envelope.stage_access_handle,
        artifact_type="test_artifact",
        content=store.put_text(content),
        schema_version="1.0",
        producer_kind="component",
        producer_id="test-component",
        component_version="1.0.0",
        parent_artifact_refs=envelope.input_artifacts,
        primary=True,
    )
    approval_envelope = service.get_stage_envelope(task_id)
    freeze = service.record_approval(
        access_handle=approval_envelope.stage_access_handle,
        user_confirmation_ref=f"approval:{artifact.artifact_id}",
        confirmation_assurance="audit_only",
    )
    return artifact, freeze


def test_task_issues_stage_bound_access_and_accepts_multiple_outputs(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    task = service.create_task("reference_study")

    first_envelope = service.get_stage_envelope(task.task_id)
    assert first_envelope.stage == "reference_study"
    assert "workflow_submit_artifact" in first_envelope.allowed_tools
    assert first_envelope.input_artifacts == []

    child = service.submit_artifact(
        access_handle=first_envelope.stage_access_handle,
        artifact_type="visual_analysis",
        content=store.put_text("视觉证据"),
        schema_version="1.0",
        producer_kind="component",
        producer_id="video-analysis",
        component_version="1.0.0",
    )
    second_envelope = service.get_stage_envelope(task.task_id)
    manifest = service.submit_artifact(
        access_handle=second_envelope.stage_access_handle,
        artifact_type="reference_report_manifest",
        content=store.put_text("汇总报告"),
        schema_version="1.0",
        producer_kind="agent",
        producer_id="reference-report-agent",
        model_id="test-model",
        primary=True,
        parent_artifact_refs=[child.as_ref()],
    )

    current_task, stage = service.get_task(task.task_id)
    assert current_task.status == "awaiting_user"
    assert stage.status == "awaiting_confirmation"
    assert stage.output_artifact_refs == [child.as_ref(), manifest.as_ref()]
    assert stage.primary_output_artifact_ref == manifest.as_ref()


def test_stage_handle_rejects_reuse_expiry_and_unknown_tools(tmp_path: Path) -> None:
    now = [datetime(2026, 7, 22, tzinfo=UTC)]
    service, store = _service(tmp_path, now=now, ttl=timedelta(seconds=5))
    task = service.create_task("original_creation")
    envelope = service.get_stage_envelope(task.task_id)

    service.submit_artifact(
        access_handle=envelope.stage_access_handle,
        artifact_type="creative_direction_notes",
        content=store.put_text("方向"),
        schema_version="1.0",
        producer_kind="agent",
        producer_id="creative-direction-agent",
        model_id="test-model",
    )

    with pytest.raises(PluginError) as reused:
        service.submit_artifact(
            access_handle=envelope.stage_access_handle,
            artifact_type="creative_direction_notes",
            content=store.put_text("重复"),
            schema_version="1.0",
            producer_kind="agent",
            producer_id="creative-direction-agent",
            model_id="test-model",
        )
    assert reused.value.code == "stage_access_invalid"

    fresh = service.get_stage_envelope(task.task_id)
    with pytest.raises(PluginError) as disallowed:
        service._validate_access(fresh.stage_access_handle, required_tool="analysis_start")
    assert disallowed.value.code == "stage_not_allowed"

    now[0] += timedelta(seconds=6)
    with pytest.raises(PluginError) as expired:
        service.read_artifact(
            access_handle=fresh.stage_access_handle,
            artifact_id="artifact_0000000000000000",
        )
    assert expired.value.code == "stage_access_expired"


def test_approval_freezes_dependency_closure_and_advances_stage(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    task = service.create_task("original_creation")
    child = _submit_component_artifact(service, store, task.task_id, "方向依据", primary=False)
    envelope = service.get_stage_envelope(task.task_id)
    main = service.submit_artifact(
        access_handle=envelope.stage_access_handle,
        artifact_type="creative_direction",
        content=store.put_text("视频总体方案"),
        schema_version="1.0",
        producer_kind="agent",
        producer_id="creative-direction-agent",
        model_id="test-model",
        parent_artifact_refs=[child.as_ref()],
        primary=True,
    )
    approval_envelope = service.get_stage_envelope(task.task_id)

    freeze = service.record_approval(
        access_handle=approval_envelope.stage_access_handle,
        user_confirmation_ref="codex-message-123",
        confirmation_assurance="audit_only",
    )

    updated_task, stage = service.get_task(task.task_id)
    assert freeze.artifact_id == main.artifact_id
    assert freeze.input_freeze_refs == []
    assert updated_task.current_stage == "resource_preparation"
    assert updated_task.status == "active"
    assert stage.input_artifact_refs == [main.as_ref()]
    assert stage.input_freeze_refs == [freeze.as_ref()]
    assert service.repository.get_artifact(main.artifact_id).status == "approved"


def test_primary_artifact_requires_every_current_stage_subordinate_output(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    task = service.create_task("reference_study")
    child = _submit_component_artifact(service, store, task.task_id, "分析清单", primary=False)
    envelope = service.get_stage_envelope(task.task_id)

    with pytest.raises(PluginError) as missing_dependency:
        service.submit_artifact(
            access_handle=envelope.stage_access_handle,
            artifact_type="reference_report_manifest",
            content=store.put_text("汇总报告"),
            schema_version="1.0",
            producer_kind="agent",
            producer_id="reference-report-agent",
            model_id="test-model",
            primary=True,
        )

    assert missing_dependency.value.code == "dependency_closure_mismatch"
    accepted_envelope = service.get_stage_envelope(task.task_id)
    report = service.submit_artifact(
        access_handle=accepted_envelope.stage_access_handle,
        artifact_type="reference_report_manifest",
        content=store.put_text("汇总报告"),
        schema_version="1.0",
        producer_kind="agent",
        producer_id="reference-report-agent",
        model_id="test-model",
        parent_artifact_refs=[child.as_ref()],
        primary=True,
    )
    assert report.parent_artifact_refs == [child.as_ref()]


def test_stage_rejects_new_outputs_after_primary_submission(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    task = service.create_task("original_creation")
    _submit_component_artifact(service, store, task.task_id, "总体方案", primary=True)
    awaiting_confirmation = service.get_stage_envelope(task.task_id)

    with pytest.raises(PluginError) as late_output:
        service.submit_artifact(
            access_handle=awaiting_confirmation.stage_access_handle,
            artifact_type="late_child",
            content=store.put_text("迟到的子产物"),
            schema_version="1.0",
            producer_kind="component",
            producer_id="test-component",
            component_version="1.0.0",
        )

    assert late_output.value.code == "stage_not_allowed"


def test_creation_stages_keep_all_frozen_ancestor_inputs(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    task = service.create_task("original_creation")

    creative, creative_freeze = _submit_primary_and_approve(
        service, store, task.task_id, "总体方案"
    )
    resource, resource_freeze = _submit_primary_and_approve(
        service, store, task.task_id, "素材与 BGM"
    )
    editing, editing_freeze = _submit_primary_and_approve(service, store, task.task_id, "剪辑规格")

    _, execution = service.get_task(task.task_id)
    assert execution.stage_type == "execution"
    assert execution.input_artifact_refs == [
        creative.as_ref(),
        resource.as_ref(),
        editing.as_ref(),
    ]
    assert execution.input_freeze_refs == [
        creative_freeze.as_ref(),
        resource_freeze.as_ref(),
        editing_freeze.as_ref(),
    ]


def test_reference_guided_creation_keeps_report_available_in_every_creation_stage(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    task = service.create_task("reference_guided_creation")
    report_path = (
        Path(__file__).parents[1]
        / "validation"
        / "reference_studies"
        / "01_fastcut_pip"
        / "report_manifest.json"
    )
    report_bytes = report_path.read_bytes()
    reference_envelope = service.get_stage_envelope(task.task_id)
    report = service.submit_artifact(
        access_handle=reference_envelope.stage_access_handle,
        artifact_type="reference_report_manifest",
        content=store.put_file(report_path),
        schema_version="1.0",
        producer_kind="agent",
        producer_id="reference-report-agent",
        model_id="test-model",
        primary=True,
    )
    approval_envelope = service.get_stage_envelope(task.task_id)
    service.record_approval(
        access_handle=approval_envelope.stage_access_handle,
        user_confirmation_ref="approval:reference-report",
        confirmation_assurance="audit_only",
    )

    for stage_name in (
        "creative_direction",
        "resource_preparation",
        "editing_specification",
    ):
        envelope = service.get_stage_envelope(task.task_id)
        assert envelope.stage == stage_name
        assert report.as_ref() in envelope.input_artifacts
        assert (
            service.read_artifact(
                access_handle=envelope.stage_access_handle,
                artifact_id=report.artifact_id,
            )
            == report_bytes
        )
        _submit_primary_and_approve(service, store, task.task_id, stage_name)

    execution_envelope = service.get_stage_envelope(task.task_id)
    assert execution_envelope.stage == "execution"
    assert report.as_ref() in execution_envelope.input_artifacts
    assert (
        service.read_artifact(
            access_handle=execution_envelope.stage_access_handle,
            artifact_id=report.artifact_id,
        )
        == report_bytes
    )


def test_approval_rejects_tampered_parent_and_missing_host_receipt(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    task = service.create_task("original_creation")
    child = _submit_component_artifact(service, store, task.task_id, "原始依据", primary=False)
    envelope = service.get_stage_envelope(task.task_id)
    service.submit_artifact(
        access_handle=envelope.stage_access_handle,
        artifact_type="creative_direction",
        content=store.put_text("总体方案"),
        schema_version="1.0",
        producer_kind="agent",
        producer_id="creative-direction-agent",
        model_id="test-model",
        parent_artifact_refs=[child.as_ref()],
        primary=True,
    )
    approval_envelope = service.get_stage_envelope(task.task_id)

    with pytest.raises(PluginError) as receipt_error:
        service.record_approval(
            access_handle=approval_envelope.stage_access_handle,
            user_confirmation_ref="message-1",
            confirmation_assurance="host_verified",
        )
    assert receipt_error.value.code == "approval_receipt_invalid"

    child_artifact = service.repository.get_artifact(child.artifact_id)
    child_path = store._path_for_hash(child_artifact.content_sha256)
    child_path.write_text("篡改", encoding="utf-8")
    with pytest.raises(PluginError) as hash_error:
        service.record_approval(
            access_handle=approval_envelope.stage_access_handle,
            user_confirmation_ref="message-1",
            confirmation_assurance="audit_only",
        )
    assert hash_error.value.code == "artifact_hash_mismatch"


def test_stage_tool_rechecks_frozen_inputs_after_handle_is_issued(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    task = service.create_task("original_creation")
    creative, _ = _submit_primary_and_approve(service, store, task.task_id, "总体方案")
    resource_envelope = service.get_stage_envelope(task.task_id)
    creative_artifact = service.repository.get_artifact(creative.artifact_id)
    store._path_for_hash(creative_artifact.content_sha256).write_text(
        "冻结后篡改",
        encoding="utf-8",
    )

    with pytest.raises(PluginError) as tampered:
        service.authorize_stage_tool(
            resource_envelope.stage_access_handle,
            "workflow_submit_artifact",
        )

    assert tampered.value.code == "artifact_hash_mismatch"


def test_reopen_marks_downstream_stale_and_invalidates_access(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    task = service.create_task("original_creation")
    _submit_component_artifact(service, store, task.task_id, "总体方案", primary=True)
    approval_envelope = service.get_stage_envelope(task.task_id)
    service.record_approval(
        access_handle=approval_envelope.stage_access_handle,
        user_confirmation_ref="message-1",
        confirmation_assurance="audit_only",
    )
    resource_envelope = service.get_stage_envelope(task.task_id)

    reopened = service.reopen_stage(
        access_handle=resource_envelope.stage_access_handle,
        stage_type="creative_direction",
    )

    task_after, current = service.get_task(task.task_id)
    assert reopened.stage_run_id == current.stage_run_id
    assert task_after.current_stage == "creative_direction"
    old_stages = [
        stage for stage in service.repository.list_stages(task.task_id) if stage != current
    ]
    assert {stage.status for stage in old_stages} == {"stale"}
    with pytest.raises(PluginError) as invalidated:
        service.read_artifact(
            access_handle=resource_envelope.stage_access_handle,
            artifact_id="artifact_0000000000000000",
        )
    assert invalidated.value.code == "stage_access_invalid"


def test_reopen_intermediate_stage_restores_full_frozen_ancestor_closure(
    tmp_path: Path,
) -> None:
    service, store = _service(tmp_path)
    task = service.create_task("original_creation")
    creative, creative_freeze = _submit_primary_and_approve(
        service, store, task.task_id, "总体方案"
    )
    resource, resource_freeze = _submit_primary_and_approve(
        service, store, task.task_id, "素材与 BGM"
    )
    _submit_primary_and_approve(service, store, task.task_id, "剪辑规格")
    execution_envelope = service.get_stage_envelope(task.task_id)

    reopened = service.reopen_stage(
        access_handle=execution_envelope.stage_access_handle,
        stage_type="editing_specification",
    )

    assert reopened.input_artifact_refs == [creative.as_ref(), resource.as_ref()]
    assert reopened.input_freeze_refs == [
        creative_freeze.as_ref(),
        resource_freeze.as_ref(),
    ]
    reopened_envelope = service.get_stage_envelope(task.task_id)
    assert [
        service.read_artifact(
            access_handle=reopened_envelope.stage_access_handle,
            artifact_id=reference.artifact_id,
        )
        for reference in reopened.input_artifact_refs
    ] == ["总体方案".encode(), "素材与 BGM".encode()]
