from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from video_create_plugin.analysis import AnalysisEvidenceManifest
from video_create_plugin.errors import PluginError
from video_create_plugin.knowledge import KnowledgeRecord, Publication, PublicationRequest
from video_create_plugin.mcp.application import PluginApplication
from video_create_plugin.reporting import ReferenceReportManifest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "validation/reference_studies/01_fastcut_pip"


def _evidence_manifest(report: ReferenceReportManifest) -> AnalysisEvidenceManifest:
    entries = [
        {
            "kind": "fixture",
            "path": evidence_ref,
            "sha256": hashlib.sha256(evidence_ref.encode("utf-8")).hexdigest(),
            "size_bytes": len(evidence_ref.encode("utf-8")),
            "algorithm_version": "fixture-v1",
        }
        for evidence_ref in sorted(report.evidence_refs)
    ]
    bundle_sha256 = hashlib.sha256(
        json.dumps(
            entries,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return AnalysisEvidenceManifest.model_validate(
        {
            "schema_version": "1.0.0",
            "job_id": report.analysis_id,
            "source": {
                "path": "fixture.mp4",
                "sha256": report.source_sha256,
                "size_bytes": 1,
            },
            "evidence_bundle": {
                "entries": entries,
                "sha256": bundle_sha256,
            },
        }
    )


def _seed_publication_stage(
    tmp_path: Path,
) -> tuple[PluginApplication, str, PublicationRequest, dict[str, object]]:
    app = PluginApplication(
        tmp_path / "plugin-output",
        project_root=ROOT,
        media_roots=(tmp_path,),
    )
    report = ReferenceReportManifest.model_validate_json(
        (FIXTURE_DIR / "report_manifest.json").read_bytes()
    )
    task = app.workflow.create_task("reference_study")
    envelope = app.workflow.get_stage_envelope(task.task_id)
    evidence_manifest = _evidence_manifest(report)
    evidence_artifact = app.workflow.submit_artifact(
        access_handle=envelope.stage_access_handle,
        artifact_type="reference_analysis_manifest",
        content=app.artifacts.put_text(evidence_manifest.model_dump_json()),
        schema_version="1.0.0",
        producer_kind="component",
        producer_id="fixture-analysis",
        component_version="fixture-v1",
        evidence_refs=sorted(report.evidence_refs),
    )
    envelope = app.workflow.get_stage_envelope(task.task_id)
    report_artifact = app.workflow.submit_artifact(
        access_handle=envelope.stage_access_handle,
        artifact_type="reference_report_manifest",
        content=app.artifacts.put_bytes((FIXTURE_DIR / "report_manifest.json").read_bytes()),
        schema_version=report.schema_version,
        producer_kind="agent",
        producer_id="fixture-reference-agent",
        model_id="fixture-model",
        primary=True,
        parent_artifact_refs=[evidence_artifact.as_ref()],
        evidence_refs=sorted(report.evidence_refs),
    )
    app.workflow.record_approval(
        access_handle=app.workflow.get_stage_envelope(task.task_id).stage_access_handle,
        user_confirmation_ref="message:approved-report",
        confirmation_assurance="audit_only",
    )
    publication_handle = app.workflow.get_stage_envelope(task.task_id).stage_access_handle
    report_ref = report_artifact.as_ref()
    records = []
    for payload in json.loads((FIXTURE_DIR / "knowledge_records.json").read_text(encoding="utf-8")):
        payload["source_task_id"] = task.task_id
        payload["source_report_ref"] = report_ref.model_dump(mode="json")
        payload["source_artifact_refs"] = [report_ref.model_dump(mode="json")]
        records.append(KnowledgeRecord.model_validate(payload))
    freeze = app.repository.get_freeze_for_artifact(
        report_ref.artifact_id,
    )
    request = PublicationRequest(
        source_task_id=task.task_id,
        source_report_ref=report_ref,
        source_media_sha256=report.source_sha256,
        publication_revision=1,
        freeze_id=freeze.freeze_id,
        records=records,
    )
    return (
        app,
        publication_handle,
        request,
        report_ref.model_dump(mode="json"),
    )


def _submit_publication_primary(
    app: PluginApplication,
    access_handle: str,
    publication: dict[str, object],
    report_ref: dict[str, object],
) -> dict[str, object]:
    return cast(
        dict[str, object],
        app.submit_artifact(
            access_handle=access_handle,
            artifact_type="knowledge_publication",
            content=json.dumps(publication, ensure_ascii=False),
            schema_version="1.0.0",
            producer_kind="component",
            producer_id="knowledge-store",
            primary=True,
            parent_artifact_refs=[report_ref],
            evidence_refs=None,
            rule_version=None,
            skill_versions=None,
            model_id=None,
            component_version="knowledge-store-v1",
        ),
    )


def test_primary_requires_real_current_unique_active_publication(tmp_path: Path) -> None:
    app, access_handle, request, report_ref = _seed_publication_stage(tmp_path)
    first = app.knowledge_publish(
        access_handle,
        request.model_dump(mode="json"),
    )

    fabricated = {**first, "publication_id": "publication_ffffffffffffffff"}
    Publication.model_validate_json(json.dumps(fabricated))
    with pytest.raises(PluginError) as missing:
        _submit_publication_primary(app, access_handle, fabricated, report_ref)
    assert missing.value.code == "knowledge_publication_not_published"

    latest = app.knowledge_publish(
        access_handle,
        request.model_copy(update={"publication_revision": 2}).model_dump(mode="json"),
    )
    superseded = app.knowledge_store.get_publication(str(first["publication_id"])).model_dump(
        mode="json"
    )
    with pytest.raises(PluginError) as inactive:
        _submit_publication_primary(app, access_handle, superseded, report_ref)
    assert inactive.value.code == "knowledge_publication_not_active"

    with sqlite3.connect(app.knowledge_store.sqlite_path) as connection:
        connection.execute(
            "UPDATE publications SET status = 'active' WHERE publication_id = ?",
            (first["publication_id"],),
        )
    with pytest.raises(PluginError) as ambiguous:
        _submit_publication_primary(app, access_handle, latest, report_ref)
    assert ambiguous.value.code == "knowledge_active_publication_invalid"
    assert ambiguous.value.details == {"active_count": 2}

    with sqlite3.connect(app.knowledge_store.sqlite_path) as connection:
        connection.execute(
            "UPDATE publications SET status = 'superseded' WHERE publication_id = ?",
            (first["publication_id"],),
        )
    primary = _submit_publication_primary(app, access_handle, latest, report_ref)
    assert primary["artifact_type"] == "knowledge_publication"
    task, stage = app.workflow.get_task(request.source_task_id)
    assert stage.primary_output_artifact_ref is not None
    assert stage.primary_output_artifact_ref.artifact_id == primary["artifact_id"]
    assert task.status == "completed"
