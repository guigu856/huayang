from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from video_create_plugin.analysis import (
    AnalysisEvidenceBundle,
    AnalysisEvidenceEntry,
    AnalysisEvidenceManifest,
    AnalysisSource,
)
from video_create_plugin.errors import PluginError
from video_create_plugin.mcp.application import PluginApplication
from video_create_plugin.reporting import ReferenceReportManifest

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "validation/reference_studies/01_fastcut_pip/report_manifest.json"


def test_evidence_manifest_rejects_unsafe_path_and_tampered_bundle_hash() -> None:
    with pytest.raises(ValidationError, match="安全的 POSIX 相对路径"):
        AnalysisEvidenceEntry(
            kind="frame",
            path="../outside.jpg",
            sha256="a" * 64,
            size_bytes=1,
            algorithm_version="test-v1",
        )

    entry = AnalysisEvidenceEntry(
        kind="frame",
        path="visual/frame.jpg",
        sha256="a" * 64,
        size_bytes=1,
        algorithm_version="test-v1",
    )
    with pytest.raises(ValidationError, match="汇总哈希不匹配"):
        AnalysisEvidenceBundle(entries=[entry], sha256="0" * 64)


def test_generic_artifact_submission_cannot_forge_analysis_manifest(
    tmp_path: Path,
) -> None:
    app = PluginApplication(tmp_path / "output", project_root=ROOT)
    task = app.create_task("reference_study", None)
    envelope = app.get_stage_envelope(str(task["task_id"]))

    with pytest.raises(PluginError) as error:
        app.submit_artifact(
            access_handle=str(envelope["stage_access_handle"]),
            artifact_type="reference_analysis_manifest",
            content="{}",
            schema_version="1.0",
            producer_kind="component",
            producer_id="forged-component",
            primary=False,
            parent_artifact_refs=None,
            evidence_refs=None,
            rule_version=None,
            skill_versions=None,
            model_id=None,
            component_version="forged-v1",
        )

    assert error.value.code == "artifact_type_reserved"


def test_report_requires_one_hash_manifest_covering_every_evidence_ref(
    tmp_path: Path,
) -> None:
    app = PluginApplication(tmp_path / "output", project_root=ROOT)
    task = app.workflow.create_task("reference_study")
    report = ReferenceReportManifest.model_validate_json(REPORT_PATH.read_bytes())
    entry = AnalysisEvidenceEntry(
        kind="analysis_json",
        path=report.evidence_refs[0],
        sha256="a" * 64,
        size_bytes=1,
        algorithm_version="test-v1",
    )
    entries_payload = [entry.model_dump(mode="json")]
    bundle_sha256 = hashlib.sha256(
        json.dumps(
            entries_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    manifest = AnalysisEvidenceManifest(
        schema_version="1.0",
        job_id=report.analysis_id,
        source=AnalysisSource(
            path="fixture.mp4",
            sha256=report.source_sha256,
            size_bytes=1,
        ),
        evidence_bundle=AnalysisEvidenceBundle(
            entries=[entry],
            sha256=bundle_sha256,
        ),
    )
    study = app.workflow.get_stage_envelope(task.task_id)
    analysis = app.workflow.submit_artifact(
        access_handle=study.stage_access_handle,
        artifact_type="reference_analysis_manifest",
        content=app.artifacts.put_text(manifest.model_dump_json()),
        schema_version="1.0",
        producer_kind="component",
        producer_id="test-analysis",
        component_version="test-v1",
    )
    report_stage = app.workflow.get_stage_envelope(task.task_id)

    with pytest.raises(PluginError) as error:
        app.submit_artifact(
            access_handle=report_stage.stage_access_handle,
            artifact_type="reference_report_manifest",
            content=REPORT_PATH.read_text(encoding="utf-8"),
            schema_version="1.0",
            producer_kind="agent",
            producer_id="test-report-agent",
            primary=True,
            parent_artifact_refs=[analysis.as_ref().model_dump(mode="json")],
            evidence_refs=report.evidence_refs,
            rule_version="test-v1",
            skill_versions=[],
            model_id="test-model",
            component_version=None,
        )

    assert error.value.code == "report_evidence_mismatch"
