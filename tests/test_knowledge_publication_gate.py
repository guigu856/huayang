from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from video_create_plugin import ArtifactStore, WorkflowService
from video_create_plugin.analysis import (
    AnalysisEvidenceBundle,
    AnalysisEvidenceEntry,
    AnalysisEvidenceManifest,
    AnalysisSource,
)
from video_create_plugin.context import ContextCatalog
from video_create_plugin.errors import PluginError
from video_create_plugin.knowledge import KnowledgeRecord, KnowledgeStore, PublicationRequest
from video_create_plugin.models import ArtifactEnvelope, ArtifactRef, FreezeRecord
from video_create_plugin.publication import KnowledgePublicationService
from video_create_plugin.reporting import ReferenceReportManifest
from video_create_plugin.repository import WorkflowRepository

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "validation/reference_studies/01_fastcut_pip"


def _freeze_report(
    tmp_path: Path,
    report_bytes: bytes,
) -> tuple[
    WorkflowService,
    KnowledgePublicationService,
    str,
    ArtifactEnvelope,
    FreezeRecord,
]:
    objects = ArtifactStore(tmp_path / "objects")
    workflow = WorkflowService(
        WorkflowRepository(tmp_path / "workflow.sqlite3"),
        objects,
        policy_resolver=ContextCatalog().policy,
    )
    task = workflow.create_task("reference_study")
    envelope = workflow.get_stage_envelope(task.task_id)
    parent_artifact_refs: list[ArtifactRef] = []
    try:
        report = ReferenceReportManifest.model_validate_json(report_bytes)
    except ValidationError:
        pass
    else:
        entries = []
        for evidence_ref in report.evidence_refs:
            evidence_path = ROOT / evidence_ref
            entries.append(
                AnalysisEvidenceEntry(
                    kind="fixture_evidence",
                    path=evidence_ref,
                    sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                    size_bytes=evidence_path.stat().st_size,
                    algorithm_version="test-evidence-index-v1",
                )
            )
        entries.sort(key=lambda entry: entry.path)
        encoded_entries = json.dumps(
            [entry.model_dump(mode="json") for entry in entries],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        evidence_manifest = AnalysisEvidenceManifest(
            schema_version="1.0",
            job_id=report.analysis_id,
            source=AnalysisSource(
                path="fixture-source.mp4",
                sha256=report.source_sha256,
                size_bytes=1,
            ),
            evidence_bundle=AnalysisEvidenceBundle(
                entries=entries,
                sha256=hashlib.sha256(encoded_entries).hexdigest(),
            ),
        )
        analysis_artifact = workflow.submit_artifact(
            access_handle=envelope.stage_access_handle,
            artifact_type="reference_analysis_manifest",
            content=objects.put_text(
                json.dumps(
                    evidence_manifest.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ),
            schema_version="1.0",
            producer_kind="component",
            producer_id="test-evidence-indexer",
            component_version="test-evidence-index-v1",
        )
        parent_artifact_refs = [analysis_artifact.as_ref()]
        envelope = workflow.get_stage_envelope(task.task_id)
    artifact = workflow.submit_artifact(
        access_handle=envelope.stage_access_handle,
        artifact_type="reference_report_manifest",
        content=objects.put_bytes(report_bytes),
        schema_version="1.0",
        producer_kind="agent",
        producer_id="reference-agent",
        model_id="test-model",
        parent_artifact_refs=parent_artifact_refs,
        primary=True,
    )
    approval_envelope = workflow.get_stage_envelope(task.task_id)
    freeze = workflow.record_approval(
        access_handle=approval_envelope.stage_access_handle,
        user_confirmation_ref="message:approved",
        confirmation_assurance="audit_only",
    )
    publication_envelope = workflow.get_stage_envelope(task.task_id)
    service = KnowledgePublicationService(
        workflow,
        KnowledgeStore(tmp_path / "knowledge"),
    )
    return (
        workflow,
        service,
        publication_envelope.stage_access_handle,
        artifact,
        freeze,
    )


def _fixture_request(
    *,
    task_id: str,
    artifact: ArtifactEnvelope,
    freeze: FreezeRecord,
) -> PublicationRequest:
    report = ReferenceReportManifest.model_validate_json(
        (FIXTURE_DIR / "report_manifest.json").read_bytes()
    )
    source_report_ref = artifact.as_ref()
    records = []
    for payload in json.loads((FIXTURE_DIR / "knowledge_records.json").read_text(encoding="utf-8")):
        payload["source_task_id"] = task_id
        payload["source_report_ref"] = source_report_ref.model_dump(mode="json")
        payload["source_artifact_refs"] = [source_report_ref.model_dump(mode="json")]
        records.append(KnowledgeRecord.model_validate(payload))
    return PublicationRequest(
        source_task_id=task_id,
        source_report_ref=source_report_ref,
        source_media_sha256=report.source_sha256,
        publication_revision=1,
        freeze_id=freeze.freeze_id,
        records=records,
    )


def test_valid_fixture_projection_can_publish_shared_knowledge(tmp_path: Path) -> None:
    report_bytes = (FIXTURE_DIR / "report_manifest.json").read_bytes()
    workflow, service, access_handle, artifact, freeze = _freeze_report(
        tmp_path,
        report_bytes,
    )
    request = _fixture_request(
        task_id=workflow.repository.get_artifact(artifact.artifact_id).task_id,
        artifact=artifact,
        freeze=freeze,
    )

    preview = service.preview(access_handle=access_handle, request=request)
    publication = service.publish(access_handle=access_handle, request=request)

    assert preview.record_count == 9
    assert preview.stage_counts == {"stage1": 3, "stage2": 3, "stage3": 3}
    assert publication.status == "active"


def test_injected_shared_knowledge_is_rejected(tmp_path: Path) -> None:
    report_bytes = (FIXTURE_DIR / "report_manifest.json").read_bytes()
    workflow, service, access_handle, artifact, freeze = _freeze_report(
        tmp_path,
        report_bytes,
    )
    request = _fixture_request(
        task_id=workflow.repository.get_artifact(artifact.artifact_id).task_id,
        artifact=artifact,
        freeze=freeze,
    )
    injected = request.records[0].model_copy(
        update={"content": "这条共享知识没有出现在冻结报告的阶段创作映射中。"}
    )
    tampered_request = request.model_copy(update={"records": [injected, *request.records[1:]]})

    with pytest.raises(PluginError, match="共享创作知识不属于") as error:
        service.preview(access_handle=access_handle, request=tampered_request)

    assert error.value.code == "knowledge_publication_rejected"


def test_record_evidence_must_be_declared_by_frozen_report(tmp_path: Path) -> None:
    report_bytes = (FIXTURE_DIR / "report_manifest.json").read_bytes()
    workflow, service, access_handle, artifact, freeze = _freeze_report(
        tmp_path,
        report_bytes,
    )
    request = _fixture_request(
        task_id=workflow.repository.get_artifact(artifact.artifact_id).task_id,
        artifact=artifact,
        freeze=freeze,
    )
    payload = request.records[0].model_dump(mode="json")
    payload["evidence_refs"].append("evidence://injected/outside-report")
    injected = KnowledgeRecord.model_validate(payload)
    tampered_request = request.model_copy(update={"records": [injected, *request.records[1:]]})

    with pytest.raises(PluginError, match="报告之外的证据") as error:
        service.publish(access_handle=access_handle, request=tampered_request)

    assert error.value.code == "knowledge_publication_rejected"
    assert error.value.details == {"evidence_refs": ["evidence://injected/outside-report"]}


def test_invalid_frozen_report_is_rejected_before_publication(tmp_path: Path) -> None:
    workflow, service, access_handle, artifact, freeze = _freeze_report(
        tmp_path,
        b'{"schema_version":"1.0.0","unexpected":true}',
    )
    report_ref = artifact.as_ref()
    record = KnowledgeRecord.model_validate(
        {
            "collection": "creation_knowledge",
            "source_task_id": workflow.repository.get_artifact(artifact.artifact_id).task_id,
            "source_report_ref": report_ref,
            "source_artifact_refs": [report_ref],
            "analysis_version": "reference-analysis-v1",
            "applicable_stages": ["stage1"],
            "knowledge_type": "video_type",
            "visibility": "creation_shared",
            "transferability": "reusable_mechanism",
            "content": "未受报告约束的正文",
            "evidence_refs": ["evidence://unknown"],
            "fact_status": "inference",
            "confidence": 0.9,
            "granularity": "global",
        }
    )
    request = PublicationRequest(
        source_task_id=record.source_task_id,
        source_report_ref=report_ref,
        source_media_sha256="b" * 64,
        publication_revision=1,
        freeze_id=freeze.freeze_id,
        records=[record],
    )

    with pytest.raises(PluginError, match="来源报告未通过") as error:
        service.preview(access_handle=access_handle, request=request)

    assert error.value.code == "knowledge_publication_rejected"
