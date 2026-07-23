from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from video_create_plugin import PluginError
from video_create_plugin.analysis import (
    AnalysisEvidenceBundle,
    AnalysisEvidenceEntry,
    AnalysisEvidenceManifest,
    AnalysisSource,
)
from video_create_plugin.knowledge import (
    KnowledgeRecord,
    KnowledgeStore,
    PublicationRequest,
    Query,
)
from video_create_plugin.knowledge.models import CreationStage
from video_create_plugin.mcp.application import PluginApplication
from video_create_plugin.models import ArtifactRef
from video_create_plugin.reporting import ReferenceReportManifest

REFERENCE_FIXTURE_SLUGS = (
    "01_fastcut_pip",
    "08_cutout_recompose",
    "character_hype",
    "composition_collage",
)
REFERENCE_SOURCE_FILENAMES = {
    "01_fastcut_pip": "01一秒多切还带点画中画.mp4",
    "08_cutout_recompose": (
        "08从动漫片段中抠出人物，得到透明 PNG、PNG 序列或带 Alpha 的短视频，"
        "再与独立背景、字幕和特效重新合成.mp4"
    ),
    "character_hype": "角色高燃混剪.mp4",
    "composition_collage": "视觉 Composition 的动画拼贴混剪.mp4",
}
_RETRIEVAL_STAGE_ORDER: tuple[CreationStage, ...] = (
    "stage1",
    "stage2",
    "stage3",
)


@dataclass(frozen=True, slots=True)
class PublicationRunResult:
    manifest_path: Path
    manifest_sha256: str
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PublicationRow:
    publication_id: str
    publication_revision: int
    status: str
    supersedes_publication_id: str | None


def publish_reference_fixtures(
    *,
    revision: int,
    project_root: Path,
    plugin_output_root: Path,
    slugs: Sequence[str] = REFERENCE_FIXTURE_SLUGS,
    source_media_root: Path | None = None,
) -> PublicationRunResult:
    """通过冻结报告门禁发布 tracked 参考学习 fixture，并验证版本与召回。"""

    if revision < 1:
        raise ValueError("revision 必须为正整数")
    selected_slugs = tuple(slugs)
    if not selected_slugs or len(selected_slugs) != len(set(selected_slugs)):
        raise ValueError("fixture slug 列表必须非空且不得重复")
    if any(slug not in REFERENCE_FIXTURE_SLUGS for slug in selected_slugs):
        raise ValueError("fixture slug 不属于受控参考学习集合")

    project_root = project_root.resolve()
    plugin_output_root = plugin_output_root.resolve()
    fixture_root = project_root / "validation" / "reference_studies"
    publication_root = plugin_output_root / "validation" / "reference_publication"
    revision_root = publication_root / f"revision_{revision}"
    if revision_root.exists():
        raise PluginError(
            "reference_publication_run_exists",
            "目标 publication revision 运行目录已存在",
        )
    revision_root.mkdir(parents=True, exist_ok=False)

    application = PluginApplication(
        publication_root,
        project_root=project_root,
    )
    application._knowledge_store = KnowledgeStore(plugin_output_root / "knowledge")
    knowledge_store = application.knowledge_store
    runs: list[dict[str, Any]] = []

    for slug in selected_slugs:
        runs.append(
            _publish_fixture(
                slug=slug,
                revision=revision,
                fixture_root=fixture_root,
                revision_root=revision_root,
                project_root=project_root,
                application=application,
                knowledge_store=knowledge_store,
                source_media_root=source_media_root,
            )
        )

    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "validation_kind": "tracked_reference_fixture_publication",
        "project_root": str(project_root),
        "plugin_output_root": str(plugin_output_root),
        "knowledge_root": str(knowledge_store.root.resolve()),
        "publication_revision": revision,
        "fixture_count": len(runs),
        "runs": runs,
    }
    manifest_path = revision_root / "run_manifest.json"
    manifest_bytes = _json_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    (revision_root / "run_manifest.sha256").write_text(
        f"{manifest_sha256}  run_manifest.json\n",
        encoding="utf-8",
    )
    return PublicationRunResult(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        manifest=manifest,
    )


def _publish_fixture(
    *,
    slug: str,
    revision: int,
    fixture_root: Path,
    revision_root: Path,
    project_root: Path,
    application: PluginApplication,
    knowledge_store: KnowledgeStore,
    source_media_root: Path | None,
) -> dict[str, Any]:
    fixture_dir = fixture_root / slug
    report_path = fixture_dir / "report_manifest.json"
    records_path = fixture_dir / "knowledge_records.json"
    report_bytes = report_path.read_bytes()
    report = ReferenceReportManifest.model_validate_json(report_bytes)
    previous_active = _single_active_publication(
        knowledge_store,
        report.source_sha256,
    )

    task = application.create_task("reference_study", None)
    task_id = cast(str, task["task_id"])
    study_envelope = application.get_stage_envelope(task_id)
    evidence_manifest, source_verified = build_fixture_evidence_manifest(
        slug=slug,
        report=report,
        project_root=project_root,
        source_media_root=source_media_root,
    )
    analysis_envelope = application.workflow.submit_artifact(
        access_handle=cast(str, study_envelope["stage_access_handle"]),
        artifact_type="reference_analysis_manifest",
        content=application.artifacts.put_bytes(
            _json_bytes(evidence_manifest.model_dump(mode="json"))
        ),
        schema_version=evidence_manifest.schema_version,
        producer_kind="component",
        producer_id="tracked-reference-evidence-indexer",
        primary=False,
        evidence_refs=sorted(evidence_manifest.evidence_refs),
        component_version="tracked-reference-evidence-v1",
    )
    analysis_artifact = analysis_envelope.model_dump(mode="json")
    report_envelope = application.get_stage_envelope(task_id)
    report_artifact = application.submit_artifact(
        access_handle=cast(str, report_envelope["stage_access_handle"]),
        artifact_type="reference_report_manifest",
        content=report_bytes.decode("utf-8"),
        schema_version=report.schema_version,
        producer_kind="agent",
        producer_id="reference-semantics-validation",
        primary=True,
        parent_artifact_refs=[_artifact_ref_dict(analysis_artifact)],
        model_id=f"semantic-review-fixture-v{revision}",
        evidence_refs=[report_path.relative_to(fixture_root.parent.parent).as_posix()],
        rule_version=None,
        skill_versions=None,
        component_version=None,
    )
    approval_envelope = application.get_stage_envelope(task_id)
    freeze = application.record_approval(
        access_handle=cast(str, approval_envelope["stage_access_handle"]),
        user_confirmation_ref=(f"validation://reference-study/{slug}/revision-{revision}/approved"),
        confirmation_assurance="audit_only",
        host_approval_receipt=None,
    )
    publication_envelope = application.get_stage_envelope(task_id)
    records = _load_records(
        records_path=records_path,
        source_task_id=task_id,
        source_report_ref=_artifact_ref_dict(report_artifact),
    )
    request = PublicationRequest(
        source_task_id=task_id,
        source_report_ref=ArtifactRef.model_validate(_artifact_ref_dict(report_artifact)),
        source_media_sha256=report.source_sha256,
        publication_revision=revision,
        freeze_id=cast(str, freeze["freeze_id"]),
        records=records,
    )
    publication_handle = cast(str, publication_envelope["stage_access_handle"])
    preview = application.knowledge_preview(
        publication_handle,
        request.model_dump(mode="json"),
    )
    publication = application.knowledge_publish(
        publication_handle,
        request.model_dump(mode="json"),
    )
    publication_bytes = _json_bytes(publication)
    publication_artifact = application.submit_artifact(
        access_handle=publication_handle,
        artifact_type="knowledge_publication",
        content=publication_bytes.decode("utf-8"),
        schema_version="1.0",
        producer_kind="component",
        producer_id="knowledge-publication-service",
        component_version="knowledge-store-v1",
        parent_artifact_refs=[_artifact_ref_dict(report_artifact)],
        evidence_refs=[f"publication://{publication['publication_id']}"],
        primary=True,
        rule_version=None,
        skill_versions=None,
        model_id=None,
    )

    state_validation = _validate_publication_state(
        knowledge_store=knowledge_store,
        publication_id=cast(str, publication["publication_id"]),
        source_media_sha256=report.source_sha256,
        previous_active=previous_active,
    )
    retrieval_validation = _validate_stage_retrieval(
        knowledge_store=knowledge_store,
        publication_id=cast(str, publication["publication_id"]),
        source_task_id=task_id,
        records=records,
    )
    task_after = application.get_task(task_id)["task"]
    if task_after["status"] != "completed":
        raise PluginError(
            "reference_publication_validation_failed",
            "知识发布工作流没有完成",
        )

    fixture_output = revision_root / slug
    fixture_output.mkdir(parents=True, exist_ok=False)
    (fixture_output / "publication.json").write_bytes(publication_bytes)
    return {
        "slug": slug,
        "task_id": task_id,
        "task_status": task_after["status"],
        "report_fixture": report_path.relative_to(fixture_root.parent.parent).as_posix(),
        "report_file_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "report_content_sha256": report.report_content_sha256,
        "source_media_verified": source_verified,
        "analysis_evidence_manifest": evidence_manifest.model_dump(mode="json"),
        "analysis_artifact": analysis_artifact,
        "report_artifact": report_artifact,
        "freeze": freeze,
        "preview": preview,
        "publication": publication,
        "publication_artifact": publication_artifact,
        "state_validation": state_validation,
        "retrieval_validation": retrieval_validation,
    }


def build_fixture_evidence_manifest(
    *,
    slug: str,
    report: ReferenceReportManifest,
    project_root: Path,
    source_media_root: Path | None,
) -> tuple[AnalysisEvidenceManifest, bool]:
    entries: list[AnalysisEvidenceEntry] = []
    for evidence_ref in report.evidence_refs:
        evidence_path = (project_root / evidence_ref).resolve()
        if not evidence_path.is_relative_to(project_root) or not evidence_path.is_file():
            raise PluginError(
                "reference_evidence_missing",
                "tracked 参考报告引用的证据文件不存在",
                details={"evidence_ref": evidence_ref},
            )
        entries.append(
            AnalysisEvidenceEntry(
                kind=_evidence_kind(evidence_path),
                path=evidence_ref,
                sha256=_sha256(evidence_path),
                size_bytes=evidence_path.stat().st_size,
                algorithm_version="tracked-reference-evidence-index-v1",
            )
        )
    entries.sort(key=lambda entry: entry.path)
    entry_payload = [entry.model_dump(mode="json") for entry in entries]
    bundle_sha256 = hashlib.sha256(
        json.dumps(
            entry_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    source_path, source_size, source_verified = _source_identity(
        slug=slug,
        report=report,
        project_root=project_root,
        source_media_root=source_media_root,
    )
    return (
        AnalysisEvidenceManifest(
            schema_version="1.0",
            job_id=report.analysis_id,
            source=AnalysisSource(
                path=str(source_path),
                sha256=report.source_sha256,
                size_bytes=source_size,
            ),
            evidence_bundle=AnalysisEvidenceBundle(
                entries=entries,
                sha256=bundle_sha256,
            ),
        ),
        source_verified,
    )


def _source_identity(
    *,
    slug: str,
    report: ReferenceReportManifest,
    project_root: Path,
    source_media_root: Path | None,
) -> tuple[Path | str, int, bool]:
    if source_media_root is not None:
        source_path = (source_media_root.resolve() / REFERENCE_SOURCE_FILENAMES[slug]).resolve()
        if not source_path.is_file():
            raise PluginError(
                "reference_source_missing",
                "tracked 参考视频源文件不存在",
                details={"source_path": str(source_path)},
            )
        if _sha256(source_path) != report.source_sha256:
            raise PluginError(
                "reference_source_hash_mismatch",
                "tracked 参考视频源文件哈希与报告不一致",
                details={"source_path": str(source_path)},
            )
        return source_path, source_path.stat().st_size, True

    probe_refs = [ref for ref in report.evidence_refs if ref.endswith("/visual/media_probe.json")]
    if len(probe_refs) != 1:
        raise PluginError(
            "reference_source_identity_missing",
            "tracked 参考报告缺少唯一画面媒体探测证据",
        )
    probe = json.loads((project_root / probe_refs[0]).read_text(encoding="utf-8"))
    source_path = probe.get("source_path")
    source_sha256 = probe.get("source_sha256")
    ffprobe = probe.get("ffprobe")
    format_payload = ffprobe.get("format") if isinstance(ffprobe, dict) else None
    size_text = format_payload.get("size") if isinstance(format_payload, dict) else None
    if (
        not isinstance(source_path, str)
        or source_sha256 != report.source_sha256
        or not isinstance(size_text, str)
        or not size_text.isdigit()
        or int(size_text) < 1
    ):
        raise PluginError(
            "reference_source_identity_invalid",
            "tracked 参考视频媒体探测身份与报告不一致",
        )
    return source_path, int(size_text), False


def _artifact_ref_dict(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact["artifact_id"],
        "revision": artifact["revision"],
        "sha256": artifact["content_sha256"],
    }


def _evidence_kind(path: Path) -> str:
    if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
        return "analysis_image"
    if path.suffix.lower() in {".json", ".jsonl"}:
        return "analysis_json"
    return "analysis_evidence"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_records(
    *,
    records_path: Path,
    source_task_id: str,
    source_report_ref: dict[str, Any],
) -> list[KnowledgeRecord]:
    payloads = cast(
        list[dict[str, Any]],
        json.loads(records_path.read_text(encoding="utf-8")),
    )
    records: list[KnowledgeRecord] = []
    for payload in payloads:
        payload["source_task_id"] = source_task_id
        payload["source_report_ref"] = source_report_ref
        payload["source_artifact_refs"] = [source_report_ref]
        records.append(KnowledgeRecord.model_validate(payload))
    return records


def _single_active_publication(
    knowledge_store: KnowledgeStore,
    source_media_sha256: str,
) -> _PublicationRow | None:
    active = [
        row
        for row in _publication_rows(knowledge_store, source_media_sha256)
        if row.status == "active"
    ]
    if len(active) > 1:
        raise PluginError(
            "reference_publication_validation_failed",
            "同一源媒体存在多个 active publication",
        )
    return None if not active else active[0]


def _validate_publication_state(
    *,
    knowledge_store: KnowledgeStore,
    publication_id: str,
    source_media_sha256: str,
    previous_active: _PublicationRow | None,
) -> dict[str, Any]:
    rows = _publication_rows(knowledge_store, source_media_sha256)
    active = [row for row in rows if row.status == "active"]
    if len(active) != 1 or active[0].publication_id != publication_id:
        raise PluginError(
            "reference_publication_validation_failed",
            "新 publication 不是同源唯一 active 版本",
        )
    current = knowledge_store.get_publication(publication_id)
    previous_status: str | None = None
    if previous_active is None:
        if current.supersedes_publication_id is not None:
            raise PluginError(
                "reference_publication_validation_failed",
                "首个 publication 出现了意外替代关系",
            )
    else:
        previous = knowledge_store.get_publication(previous_active.publication_id)
        previous_status = previous.status
        if (
            previous.status != "superseded"
            or current.supersedes_publication_id != previous.publication_id
        ):
            raise PluginError(
                "reference_publication_validation_failed",
                "上一 active publication 没有被新版本替代",
            )
    return {
        "single_active": True,
        "active_publication_id": active[0].publication_id,
        "previous_active_publication_id": (
            None if previous_active is None else previous_active.publication_id
        ),
        "previous_active_status_after_publish": previous_status,
        "supersedes_publication_id": current.supersedes_publication_id,
    }


def _validate_stage_retrieval(
    *,
    knowledge_store: KnowledgeStore,
    publication_id: str,
    source_task_id: str,
    records: list[KnowledgeRecord],
) -> list[dict[str, Any]]:
    validations: list[dict[str, Any]] = []
    for stage in _RETRIEVAL_STAGE_ORDER:
        record = next(
            item
            for item in records
            if item.collection == "creation_knowledge" and stage in item.applicable_stages
        )
        hits = knowledge_store.search_shared(
            Query(
                text=record.content,
                stage=stage,
                knowledge_types=[record.knowledge_type],
                source_task_id=source_task_id,
                limit=5,
            )
        )
        if not hits or any(hit.publication_id != publication_id for hit in hits):
            raise PluginError(
                "reference_publication_validation_failed",
                f"{stage} 没有命中新 publication",
            )
        validations.append(
            {
                "stage": stage,
                "knowledge_type": record.knowledge_type,
                "hit_knowledge_ids": [hit.knowledge_id for hit in hits],
                "hit_publication_ids": sorted({hit.publication_id for hit in hits}),
            }
        )
    return validations


def _publication_rows(
    knowledge_store: KnowledgeStore,
    source_media_sha256: str,
) -> list[_PublicationRow]:
    with sqlite3.connect(knowledge_store.sqlite_path) as connection:
        rows = connection.execute(
            """
            SELECT publication_id, publication_revision, status,
                   supersedes_publication_id
            FROM publications
            WHERE source_media_sha256 = ?
            ORDER BY publication_revision
            """,
            (source_media_sha256,),
        ).fetchall()
    return [
        _PublicationRow(
            publication_id=str(row[0]),
            publication_revision=int(row[1]),
            status=str(row[2]),
            supersedes_publication_id=(None if row[3] is None else str(row[3])),
        )
        for row in rows
    ]


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _positive_revision(value: str) -> int:
    revision = int(value)
    if revision < 1:
        raise argparse.ArgumentTypeError("revision 必须为正整数")
    return revision


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="发布 tracked 参考视频学习知识")
    parser.add_argument("--revision", required=True, type=_positive_revision)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--plugin-output-root", required=True, type=Path)
    parser.add_argument(
        "--source-media-root",
        type=Path,
        help="可选；提供时逐个校验四个参考视频源文件的 SHA-256",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = publish_reference_fixtures(
        revision=args.revision,
        project_root=args.project_root,
        plugin_output_root=args.plugin_output_root,
        source_media_root=args.source_media_root,
    )
    print(
        json.dumps(
            {
                "manifest_path": str(result.manifest_path),
                "manifest_sha256": result.manifest_sha256,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
