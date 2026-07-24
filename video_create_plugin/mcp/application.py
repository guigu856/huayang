from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import unquote

from pydantic import ValidationError

from components.bgm_acquisition import BgmAcquisitionConfig, BgmAcquisitionService
from components.bgm_analysis import BgmAnalysisService
from components.image_acquisition import (
    ImageAcquisitionConfig,
    ImageAcquisitionService,
)
from components.material_acquisition import (
    MaterialAcquisitionConfig,
    MaterialAcquisitionService,
    SearchFilters,
)
from components.media_preprocessing import (
    MediaPreprocessingConfig,
    MediaPreprocessingError,
    MediaPreprocessingService,
    MediaPreprocessRequest,
)
from components.render_inspection import (
    OverlayExpectation,
    RenderExpectation,
    RenderInspectionReport,
    RenderInspectionService,
)
from components.video_download import (
    DownloadConfig,
    DownloadResult,
    download_video,
)
from components.video_editor import VideoEditorService
from components.video_editor.errors import VideoEditorError
from components.video_editor.jobs import PersistentRenderQueue, RenderJob
from components.video_editor.media import (
    MAX_MEDIA_BYTES,
    import_media,
    probe_media,
    resolve_media_path,
)
from components.video_editor.models import EditorProject
from components.video_editor.render import FFmpegRenderer
from components.video_editor.storage import ProjectStorage
from video_create_plugin.analysis import (
    AnalysisEvidenceManifest,
    ReferenceAnalysisResult,
    ReferenceAnalysisService,
)
from video_create_plugin.artifacts import ArtifactStore
from video_create_plugin.context import ContextCatalog
from video_create_plugin.creation import (
    PreparationPackage,
    PreparationScopeError,
    validate_preparation_scope,
)
from video_create_plugin.editing import (
    EditingSpecification,
    ExecutionCompiler,
    SpecTraceMap,
    canonical_spec_sha256,
    preflight_spec,
    validate_execution_project,
)
from video_create_plugin.errors import PluginError
from video_create_plugin.execution import (
    ExecutionManifest,
    RenderInspectionBinding,
    RenderInspectionBindingPayload,
)
from video_create_plugin.models import (
    ArtifactEnvelope,
    ArtifactRef,
    ConfirmationAssurance,
    FreezeRef,
    ReferenceContextBinding,
    StageRun,
    TaskRun,
    TaskType,
)
from video_create_plugin.publication import KnowledgePublicationService
from video_create_plugin.reporting import (
    ReferenceReportGenerator,
    ReferenceReportManifest,
    canonical_json_bytes,
)
from video_create_plugin.repository import WorkflowRepository
from video_create_plugin.retrieval import AuditedKnowledgeService
from video_create_plugin.workflow import WorkflowService

_PROJECT_ID = re.compile(r"^project_[0-9a-f]{16}$")
_RENDER_JOB_ID = re.compile(r"^render_[0-9a-f]{16}$")
_TASK_ID = re.compile(r"^task_[0-9a-f]{16}$")
CreationProjectionStage = Literal[
    "creative_direction",
    "resource_preparation",
    "editing_specification",
]


class PluginApplication:
    """MCP handlers use this application boundary instead of owning domain logic."""

    def __init__(
        self,
        output_root: Path | str,
        *,
        project_root: Path | str,
        context_root: Path | str | None = None,
        media_roots: Sequence[Path | str] = (),
        video_downloader: Callable[[str, DownloadConfig], DownloadResult] | None = None,
        image_service_factory: Callable[[Path], ImageAcquisitionService] | None = None,
        preprocessing_service_factory: (Callable[[Path], MediaPreprocessingService] | None) = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.media_roots = tuple(
            dict.fromkeys(
                [
                    self.output_root,
                    *(Path(root).expanduser().resolve() for root in media_roots),
                ]
            )
        )

        self.context = ContextCatalog(context_root)
        self.video_downloader = video_downloader or download_video
        self._image_service_factory = image_service_factory or (
            lambda output: ImageAcquisitionService(ImageAcquisitionConfig(output_dir=output))
        )
        self._preprocessing_service_factory = preprocessing_service_factory or (
            lambda output: MediaPreprocessingService(MediaPreprocessingConfig(output_dir=output))
        )
        self.artifacts = ArtifactStore(self.output_root / "objects")
        self.repository = WorkflowRepository(self.output_root / "workflow.sqlite3")
        self.workflow = WorkflowService(
            self.repository,
            self.artifacts,
            policy_resolver=self.context.policy,
        )
        self.report_generator = ReferenceReportGenerator()
        self.materials = MaterialAcquisitionService(
            MaterialAcquisitionConfig(output_dir=self.output_root / "materials")
        )
        self.bgm = BgmAcquisitionService(BgmAcquisitionConfig(output_dir=self.output_root / "bgm"))
        self.compiler = ExecutionCompiler()
        self.project_storage = ProjectStorage(self.output_root / "editor" / "projects")
        self.editor = VideoEditorService(storage=self.project_storage)
        self.render_queue = PersistentRenderQueue(
            self.output_root / "editor" / "render-jobs",
            FFmpegRenderer(),
        )
        self.render_inspection = RenderInspectionService()
        self._render_worker_started = False
        self._render_worker_lock = threading.Lock()
        self._knowledge_store: Any | None = None
        self._audited_knowledge: AuditedKnowledgeService | None = None
        self._publication_service: KnowledgePublicationService | None = None
        self._analysis_services: dict[str, ReferenceAnalysisService] = {}
        self._image_services: dict[str, ImageAcquisitionService] = {}
        self._preprocessing_services: dict[str, MediaPreprocessingService] = {}

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "resource_id": item.resource_id,
                "kind": item.kind,
                "uri": item.uri,
                "title": item.title,
                "description": item.description,
                "relative_path": item.relative_path,
                "builtin": item.builtin,
            }
            for item in self.context.catalog()
        ]

    def context_read(self, uri: str) -> dict[str, Any]:
        item = self.context.read(uri)
        return {
            "resource_id": item.resource_id,
            "kind": item.kind,
            "uri": item.uri,
            "title": item.title,
            "description": item.description,
            "content": item.content,
        }

    def stage_bundle(self, task_type: TaskType, stage: str) -> dict[str, Any]:
        bundle = self.context.stage_bundle(task_type, stage)
        return {
            "task_type": bundle.task_type,
            "stage": bundle.stage,
            "rule_ids": list(bundle.rule_ids),
            "skill_ids": list(bundle.skill_ids),
            "schema_ids": list(bundle.schema_ids),
            "tool_ids": list(bundle.tool_ids),
            "confirmation_required": bundle.confirmation_required,
        }

    def create_task(
        self,
        task_type: TaskType,
        reference_analysis_ids: list[str] | None,
    ) -> dict[str, Any]:
        task = self.workflow.create_task(
            task_type,
            reference_analysis_ids=reference_analysis_ids,
        )
        return task.model_dump(mode="json")

    def get_task(self, task_id: str) -> dict[str, Any]:
        task, stage = self.workflow.get_task(task_id)
        return {
            "task": task.model_dump(mode="json"),
            "current_stage": stage.model_dump(mode="json"),
            "stages": [
                item.model_dump(mode="json") for item in self.repository.list_stages(task.task_id)
            ],
        }

    def get_stage_envelope(self, task_id: str) -> dict[str, Any]:
        return self.workflow.get_stage_envelope(task_id).model_dump(mode="json")

    def reference_creation_context(self, access_handle: str) -> dict[str, Any]:
        task, stage = self.authorize(access_handle, "reference_get_creation_context")
        projection_stage = _creation_projection_stage(stage.stage_type)
        binding, projection = self._expected_reference_context(
            task,
            stage,
            projection_stage,
        )
        return {
            "binding": binding.model_dump(mode="json"),
            "projection": projection,
        }

    def submit_artifact(
        self,
        *,
        access_handle: str,
        artifact_type: str,
        content: str,
        schema_version: str,
        producer_kind: Literal["agent", "component"],
        producer_id: str,
        primary: bool,
        parent_artifact_refs: list[dict[str, Any]] | None,
        evidence_refs: list[str] | None,
        rule_version: str | None,
        skill_versions: list[str] | None,
        model_id: str | None,
        component_version: str | None,
    ) -> dict[str, Any]:
        if artifact_type == "reference_analysis_manifest":
            raise PluginError(
                "artifact_type_reserved",
                "分析证据清单只由分析组件登记",
            )
        if primary:
            self._validate_primary_stage_content(access_handle, artifact_type, content)
        object_ref = self.artifacts.put_text(content)
        parents = [ArtifactRef.model_validate(item) for item in (parent_artifact_refs or [])]
        artifact = self.workflow.submit_artifact(
            access_handle=access_handle,
            artifact_type=artifact_type,
            content=object_ref,
            schema_version=schema_version,
            producer_kind=producer_kind,
            producer_id=producer_id,
            primary=primary,
            parent_artifact_refs=parents,
            evidence_refs=evidence_refs,
            rule_version=rule_version,
            skill_versions=skill_versions,
            model_id=model_id,
            component_version=component_version,
        )
        return artifact.model_dump(mode="json")

    def record_approval(
        self,
        *,
        access_handle: str,
        user_confirmation_ref: str,
        confirmation_assurance: ConfirmationAssurance,
        host_approval_receipt: str | None,
    ) -> dict[str, Any]:
        freeze = self.workflow.record_approval(
            access_handle=access_handle,
            user_confirmation_ref=user_confirmation_ref,
            confirmation_assurance=confirmation_assurance,
            host_approval_receipt=host_approval_receipt,
        )
        return freeze.model_dump(mode="json")

    def reopen_stage(
        self,
        *,
        access_handle: str,
        stage_type: str,
    ) -> dict[str, Any]:
        stage = self.workflow.reopen_stage(
            access_handle=access_handle,
            stage_type=stage_type,
        )
        return stage.model_dump(mode="json")

    def resolve_reference(self, access_handle: str, source: str) -> dict[str, Any]:
        task, _ = self.authorize(access_handle, "reference_resolve_source")
        if "http://" in source or "https://" in source:
            output = self._task_root(task.task_id) / "references" / "downloads"
            result = self.video_downloader(source, DownloadConfig(output_dir=output))
            return {"source_kind": "download", **result.to_dict()}
        path = self.media_input(source)
        return {
            "source_kind": "local_media",
            "file_path": str(path),
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
        }

    def video_download(self, access_handle: str, source: str) -> dict[str, Any]:
        task, _ = self.authorize(access_handle, "video_download")
        output = self._task_root(task.task_id) / "references" / "downloads"
        result = self.video_downloader(source, DownloadConfig(output_dir=output))
        return dict(result.to_dict())

    def probe_media(self, access_handle: str, source_path: str) -> dict[str, Any]:
        self.authorize(access_handle, "media_probe")
        metadata = probe_media(self.media_input(source_path))
        return metadata.model_dump(mode="json")

    def analysis_start(
        self,
        access_handle: str,
        source_path: str,
        modalities: list[Literal["video", "audio"]],
    ) -> dict[str, Any]:
        task, _ = self.authorize(access_handle, "analysis_start")
        if sorted(set(modalities)) != ["audio", "video"]:
            raise PluginError("analysis_request_invalid", "参考学习必须同时生成画面与音频证据")
        source = self.media_input(source_path)
        result = self._analysis_service(task.task_id).start(source)
        analysis_artifact = self._register_analysis_manifest(
            access_handle,
            task.task_id,
            result,
        )
        return {
            "task_id": task.task_id,
            "status": "succeeded",
            "modalities": ["video", "audio"],
            "result": result.to_dict(),
            "analysis_artifact": analysis_artifact,
        }

    def analysis_get_job(self, access_handle: str, job_id: str) -> dict[str, Any]:
        task, _ = self.authorize(access_handle, "analysis_get_job")
        return self._analysis_service(task.task_id).get(job_id).to_dict()

    def analysis_refine_intervals(
        self,
        access_handle: str,
        job_id: str,
        intervals: list[dict[str, Any]],
    ) -> dict[str, Any]:
        task, _ = self.authorize(access_handle, "analysis_refine_intervals")
        normalized: list[tuple[int, int]] = []
        for index, interval in enumerate(intervals):
            start = interval.get("start_us")
            end = interval.get("end_us")
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
                or start < 0
                or end <= start
            ):
                raise PluginError(
                    "analysis_interval_invalid",
                    "分析区间必须使用递增的非负微秒时间",
                    details={"index": index},
                )
            normalized.append((start, end))
        path = self._analysis_service(task.task_id).refine_intervals(
            job_id,
            normalized,
            max_interval_us=100_000,
        )
        result = self._analysis_service(task.task_id).validate(job_id)
        analysis_artifact = self._register_analysis_manifest(
            access_handle,
            task.task_id,
            result,
        )
        return {
            "job_id": job_id,
            "intervals": [{"start_us": start, "end_us": end} for start, end in normalized],
            "artifact_path": str(path),
            "sha256": _sha256(path),
            "analysis_artifact": analysis_artifact,
        }

    def analysis_validate_artifact(
        self,
        access_handle: str,
        artifact_path: str,
    ) -> dict[str, Any]:
        task, _ = self.authorize(access_handle, "analysis_validate_artifact")
        path = self.task_output(task.task_id, artifact_path)
        if not path.is_file():
            raise PluginError("artifact_not_found", "分析产物不存在")
        result: dict[str, Any] = {
            "artifact_path": str(path),
            "relative_path": path.relative_to(self.output_root).as_posix(),
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
        }
        if path.suffix.lower() in {".json", ".jsonl"}:
            self._validate_json_artifact(path)
            result["json_valid"] = True
        return result

    def report_generate(
        self,
        access_handle: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        self.authorize(access_handle, "report_generate")
        generated = self.report_generator.generate(
            cast(Any, manifest),
            self.artifacts,
        )
        return {
            "manifest": generated.manifest.model_dump(mode="json"),
            "json_artifact": _artifact_object_dict(generated.json_artifact),
            "markdown_artifact": _artifact_object_dict(generated.markdown_artifact),
        }

    def knowledge_preview(
        self,
        access_handle: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        from video_create_plugin.knowledge import PublicationRequest

        self.authorize(access_handle, "knowledge_preview_publication")
        publication = PublicationRequest.model_validate(request)
        return self.publication_service.preview(
            access_handle=access_handle,
            request=publication,
        ).model_dump(mode="json")

    def knowledge_publish(
        self,
        access_handle: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        from video_create_plugin.knowledge import PublicationRequest

        self.authorize(access_handle, "knowledge_publish")
        publication_request = PublicationRequest.model_validate(request)
        published = self.publication_service.publish(
            access_handle=access_handle,
            request=publication_request,
        ).model_dump(mode="json")
        return published

    def knowledge_search(
        self,
        access_handle: str,
        query: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {"text", "knowledge_types", "limit"}
        unknown = set(query) - allowed
        if unknown:
            raise PluginError(
                "knowledge_filter_required",
                "知识检索阶段与共享门禁由服务端确定",
                details={"unknown_fields": sorted(unknown)},
            )
        audit, result = self.audited_knowledge.search(
            access_handle=access_handle,
            text=str(query.get("text", "")),
            knowledge_types=cast(list[str], query.get("knowledge_types", [])),
            limit=int(query.get("limit", 5)),
        )
        return {
            "retrieval": audit.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
        }

    def materials_sources(self, access_handle: str, tool_id: str) -> dict[str, Any]:
        self.authorize(access_handle, tool_id)
        return self.materials.sources()

    async def materials_search(
        self,
        access_handle: str,
        tool_id: str,
        query: str,
        *,
        limit: int,
        source_names: list[str] | None,
        filters: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self.authorize(access_handle, tool_id)
        parsed_filters = SearchFilters.model_validate(filters) if filters else None
        result = await self.materials.search(
            query,
            limit=limit,
            source_names=source_names,
            filters=parsed_filters,
        )
        return result.to_dict()

    async def materials_acquire(
        self,
        access_handle: str,
        tool_id: str,
        candidate_ref: str,
    ) -> dict[str, Any]:
        self.authorize(access_handle, tool_id)
        return (await self.materials.acquire(candidate_ref)).to_dict()

    def images_sources(self, access_handle: str) -> list[dict[str, Any]]:
        task, _ = self.authorize(access_handle, "images_list_sources")
        return [source.to_dict() for source in self._image_service(task.task_id).list_sources()]

    def images_search(
        self,
        access_handle: str,
        query: str,
        *,
        limit: int,
    ) -> dict[str, Any]:
        task, _ = self.authorize(access_handle, "images_search")
        return self._image_service(task.task_id).search(query, limit=limit).to_dict()

    def images_acquire(
        self,
        access_handle: str,
        candidate_ref: str,
    ) -> dict[str, Any]:
        task, _ = self.authorize(access_handle, "images_acquire")
        return self._image_service(task.task_id).acquire(candidate_ref).to_dict()

    def media_preprocess(
        self,
        access_handle: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        task, _ = self.authorize(access_handle, "media_preprocess")
        parsed = MediaPreprocessRequest.model_validate(request)
        source = self.media_input(parsed.input_path)
        if not secrets.compare_digest(parsed.input_sha256, _sha256(source)):
            raise MediaPreprocessingError(
                "input_sha256_mismatch",
                "输入媒体 SHA-256 不匹配",
            )
        normalized = parsed.model_copy(update={"input_path": source})
        return self._preprocessing_service(task.task_id).execute(normalized).to_dict()

    def bgm_sources(self, access_handle: str) -> list[dict[str, Any]]:
        self.authorize(access_handle, "bgm_list_sources")
        return [source.to_dict() for source in self.bgm.list_sources()]

    def bgm_search(
        self,
        access_handle: str,
        query: str,
        *,
        limit: int,
    ) -> dict[str, Any]:
        self.authorize(access_handle, "bgm_search")
        return self.bgm.search(query, limit=limit).to_dict()

    def bgm_acquire(
        self,
        access_handle: str,
        candidate_ref: str,
        *,
        clip_start_seconds: float,
        clip_duration_seconds: float | None,
    ) -> dict[str, Any]:
        self.authorize(access_handle, "bgm_acquire")
        return self.bgm.acquire(
            candidate_ref,
            clip_start_seconds=clip_start_seconds,
            clip_duration_seconds=clip_duration_seconds,
        ).to_dict()

    def bgm_analyze(self, access_handle: str, source_path: str) -> dict[str, Any]:
        task, _ = self.authorize(access_handle, "bgm_analyze")
        source = self.media_input(source_path)
        analysis_dir = self._task_root(task.task_id) / "bgm_analysis" / source.stem
        return BgmAnalysisService().analyze(source, analysis_dir)

    def editor_preflight(
        self,
        access_handle: str,
    ) -> dict[str, Any]:
        _, _, _, specification = self._frozen_editing_specification(
            access_handle,
            "editor_preflight_spec",
        )
        return preflight_spec(specification).model_dump(mode="json")

    def editor_create_project(
        self,
        access_handle: str,
        name: str,
        canvas: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self.authorize(access_handle, "editor_create_project")
        project = self.editor.create(name, canvas)
        return {
            "project": project.model_dump(mode="json"),
            "project_dir": str(self.project_storage.root / project.id),
        }

    def editor_import_asset(
        self,
        access_handle: str,
        project_id: str,
        expected_revision: int,
        source_path: str,
    ) -> dict[str, Any]:
        self.authorize(access_handle, "editor_import_asset")
        source = self.media_input(source_path)
        project = self.editor.get(project_id)
        if expected_revision < 0:
            raise VideoEditorError("invalid_input", "工程版本必须是非负整数")
        if project.revision != expected_revision:
            raise VideoEditorError(
                "revision_conflict",
                "工程已被其他写入者更新",
                details={
                    "expected_revision": expected_revision,
                    "actual_revision": project.revision,
                },
            )
        project_dir = self.project_storage.root / project_id
        with source.open("rb") as stream:
            asset_input = import_media(
                project_dir,
                source.name,
                stream,
                max_bytes=MAX_MEDIA_BYTES,
                probe=probe_media,
            )
        try:
            updated = self.editor.apply(
                project_id,
                {
                    "expected_revision": expected_revision,
                    "commands": [
                        {
                            "type": "asset.add",
                            "asset": asset_input.model_dump(mode="python"),
                        }
                    ],
                },
            )
        except Exception:
            try:
                resolve_media_path(project_dir, asset_input.path).unlink(missing_ok=True)
            except VideoEditorError:
                pass
            raise
        return {
            "project": updated.model_dump(mode="json"),
            "asset": updated.assets[-1].model_dump(mode="json"),
        }

    def editor_apply_commands(
        self,
        access_handle: str,
        project_id: str,
        batch: dict[str, Any],
    ) -> dict[str, Any]:
        self.authorize(access_handle, "editor_apply_commands")
        project = self.editor.apply(project_id, batch)
        return {"project": project.model_dump(mode="json")}

    def editor_compile(
        self,
        access_handle: str,
    ) -> dict[str, Any]:
        task, artifact_ref, freeze_ref, specification = self._frozen_editing_specification(
            access_handle,
            "editor_compile_spec",
        )
        project_id = f"project_{canonical_spec_sha256(specification)[:16]}"
        project_root = self.project_storage.root / project_id
        project_json = project_root / "project.json"
        if project_json.exists():
            project, trace, manifest = self._compiled_snapshot(
                task=task,
                artifact_ref=artifact_ref,
                freeze_ref=freeze_ref,
                specification=specification,
                project_id=project_id,
                require_task_binding=False,
            )
            binding = _compile_binding(task, artifact_ref, freeze_ref)
            bindings = cast(list[dict[str, Any]], manifest["bindings"])
            if binding not in bindings:
                bindings.append(binding)
                bindings.sort(key=lambda item: str(item["task_id"]))
                _write_json(project_root / "compile_manifest.json", manifest)
            return {
                "project": project.model_dump(mode="json"),
                "assessment": manifest["assessment"],
                "trace_map": trace.model_dump(mode="json"),
                "project_dir": str(project_root),
                "editing_artifact_ref": artifact_ref.model_dump(mode="json"),
                "reused": True,
            }
        result = self.compiler.compile(specification, project_root)
        _write_json(project_json, result.project.model_dump(mode="json"))
        _write_json(project_root / "trace_map.json", result.trace_map.model_dump(mode="json"))
        _write_json(
            project_root / "compile_manifest.json",
            {
                "spec_sha256": result.assessment.spec_sha256,
                "project_id": result.project.id,
                "project_sha256": _sha256(project_json),
                "trace_map_sha256": _sha256(project_root / "trace_map.json"),
                "assessment": result.assessment.model_dump(mode="json"),
                "bindings": [_compile_binding(task, artifact_ref, freeze_ref)],
            },
        )
        return {
            "project": result.project.model_dump(mode="json"),
            "assessment": result.assessment.model_dump(mode="json"),
            "trace_map": result.trace_map.model_dump(mode="json"),
            "project_dir": str(project_root),
            "copied_asset_paths": [str(path) for path in result.copied_asset_paths],
            "editing_artifact_ref": artifact_ref.model_dump(mode="json"),
            "reused": False,
        }

    def editor_validate_execution_project(
        self,
        access_handle: str,
        project_id: str,
    ) -> dict[str, Any]:
        task, artifact_ref, freeze_ref, specification = self._frozen_editing_specification(
            access_handle,
            "editor_validate_execution_project",
        )
        project, trace_map, _ = self._compiled_snapshot(
            task=task,
            artifact_ref=artifact_ref,
            freeze_ref=freeze_ref,
            specification=specification,
            project_id=project_id,
        )
        return {
            "valid": True,
            "project_id": project.id,
            "project_revision": project.revision,
            "spec_sha256": trace_map.spec_sha256,
            "action_count": len(specification.actions),
            "mapped_action_count": len(trace_map.entries),
        }

    def editor_submit_render(
        self,
        access_handle: str,
        project_id: str,
    ) -> dict[str, Any]:
        task, artifact_ref, freeze_ref, specification = self._frozen_editing_specification(
            access_handle,
            "editor_submit_render",
        )
        project, trace_map, _ = self._compiled_snapshot(
            task=task,
            artifact_ref=artifact_ref,
            freeze_ref=freeze_ref,
            specification=specification,
            project_id=project_id,
        )
        project_dir = self.project_storage.root / project_id
        self._start_render_worker()
        job = self.render_queue.submit(
            project,
            project_dir=project_dir,
            output_path=project_dir / "renders" / f"{uuid.uuid4().hex[:16]}.mp4",
        )
        job_dir = self.render_queue.root / job.id
        trace_path = job_dir / "trace_map.json"
        _write_json(trace_path, trace_map.model_dump(mode="json"))
        _write_json(
            job_dir / "binding.json",
            {
                **_compile_binding(task, artifact_ref, freeze_ref),
                "spec_sha256": canonical_spec_sha256(specification),
                "project_id": project.id,
                "project_revision": project.revision,
                "project_snapshot_sha256": _sha256(job_dir / "project.json"),
                "trace_map_sha256": _sha256(trace_path),
            },
        )
        return job.model_dump(mode="json")

    def editor_get_render(
        self,
        access_handle: str,
        render_job_id: str,
    ) -> dict[str, Any]:
        job, _, _, _, _, _, _ = self._bound_render_job(
            access_handle,
            render_job_id,
            "editor_get_render",
        )
        return job.model_dump(mode="json")

    def editor_inspect_render(
        self,
        access_handle: str,
        render_job_id: str,
    ) -> dict[str, Any]:
        (
            job,
            task,
            artifact_ref,
            freeze_ref,
            specification,
            trace_map,
            source,
        ) = self._bound_render_job(
            access_handle,
            render_job_id,
            "editor_inspect_render",
        )
        if job.status != "succeeded":
            raise PluginError("render_not_ready", "渲染任务尚未成功完成")
        if not source.is_file():
            raise PluginError("render_output_not_found", "渲染成片不存在")
        expectation = _render_expectation(specification, trace_map)
        inspection_id = uuid.uuid4().hex[:16]
        inspection_root = self._task_root(task.task_id) / "render-inspection" / inspection_id
        result = self.render_inspection.inspect(
            source,
            expectation,
            inspection_root,
        )
        report_path = self.task_output(task.task_id, result.report_path)
        contact_sheet_path = self.task_output(task.task_id, result.contact_sheet_path)
        if (
            report_path != (inspection_root / "render_inspection.json").resolve()
            or contact_sheet_path != (inspection_root / "contact_sheet.jpg").resolve()
        ):
            raise PluginError("inspection_result_invalid", "渲染检查产物路径与本次检查不一致")
        report = self._read_render_inspection_report(report_path)
        if report != result.report:
            raise PluginError("inspection_result_invalid", "渲染检查返回值与落盘报告不一致")
        self._validate_render_inspection_report(report, source, expectation)
        if not contact_sheet_path.is_file():
            raise PluginError("inspection_result_invalid", "渲染检查联系表不存在")

        binding_path = self.render_queue.root / render_job_id / "inspection_binding.json"
        binding = self._create_inspection_binding(
            task=task,
            artifact_ref=artifact_ref,
            freeze_ref=freeze_ref,
            job=job,
            specification=specification,
            expectation=expectation,
            report=report,
            report_path=report_path,
            contact_sheet_path=contact_sheet_path,
            source=source,
        )
        _write_json(binding_path, binding.model_dump(mode="json"))
        return {
            "report": report.model_dump(mode="json"),
            "report_path": str(report_path),
            "contact_sheet_path": str(contact_sheet_path),
            "inspection_binding_path": str(binding_path.resolve()),
            "inspection_binding_sha256": _sha256(binding_path),
        }

    def read_stage_artifact(self, access_handle: str, artifact_id: str) -> bytes:
        return self.workflow.read_artifact(
            access_handle=access_handle,
            artifact_id=artifact_id,
        )

    def read_stage_evidence(
        self,
        access_handle: str,
        analysis_id: str,
        evidence_id: str,
    ) -> bytes:
        task, _ = self.authorize(access_handle, "analysis_get_job")
        result = self._analysis_service(task.task_id).validate(analysis_id)
        manifest = _read_json(result.reference_manifest_path, "evidence_not_found")
        bundle = manifest.get("evidence_bundle")
        entries = bundle.get("entries") if isinstance(bundle, dict) else None
        if not isinstance(entries, list):
            raise PluginError("evidence_not_found", "分析证据清单结构无效")

        requested = unquote(evidence_id)
        relative = Path(requested)
        if not requested or "\\" in requested or relative.is_absolute() or ".." in relative.parts:
            raise PluginError("evidence_not_found", "分析证据未登记")

        expected_sha256: str | None = None
        expected_size: int | None = None
        if requested == "reference_manifest.json":
            expected_sha256 = result.reference_manifest_sha256
            expected_size = result.reference_manifest_path.stat().st_size
        else:
            matches = [
                entry
                for entry in entries
                if isinstance(entry, dict) and entry.get("path") == requested
            ]
            if len(matches) == 1:
                sha256 = matches[0].get("sha256")
                size_bytes = matches[0].get("size_bytes")
                if isinstance(sha256, str) and isinstance(size_bytes, int):
                    expected_sha256 = sha256
                    expected_size = size_bytes
        if expected_sha256 is None or expected_size is None:
            raise PluginError("evidence_not_found", "分析证据未登记")

        job_dir = result.job_dir.resolve()
        evidence_path = (job_dir / relative).resolve()
        if not evidence_path.is_relative_to(job_dir) or not evidence_path.is_file():
            raise PluginError("evidence_not_found", "分析证据文件不存在")
        if (
            evidence_path.stat().st_size != expected_size
            or _sha256(evidence_path) != expected_sha256
        ):
            raise PluginError("artifact_hash_mismatch", "分析证据文件哈希不匹配")
        try:
            return evidence_path.read_bytes()
        except OSError as error:
            raise PluginError("evidence_not_found", "分析证据文件读取失败") from error

    def authorize(self, access_handle: str, tool_id: str) -> tuple[Any, Any]:
        return self.workflow.authorize_stage_tool(access_handle, tool_id)

    @property
    def knowledge_store(self) -> Any:
        if self._knowledge_store is None:
            from video_create_plugin.knowledge import KnowledgeStore

            self._knowledge_store = KnowledgeStore(self.output_root / "knowledge")
        return self._knowledge_store

    @property
    def audited_knowledge(self) -> AuditedKnowledgeService:
        if self._audited_knowledge is None:
            self._audited_knowledge = AuditedKnowledgeService(
                self.knowledge_store,
                self.workflow,
                self.output_root / "retrieval.sqlite3",
            )
        return self._audited_knowledge

    @property
    def publication_service(self) -> KnowledgePublicationService:
        if self._publication_service is None:
            self._publication_service = KnowledgePublicationService(
                self.workflow,
                self.knowledge_store,
            )
        return self._publication_service

    def _analysis_service(self, task_id: str) -> ReferenceAnalysisService:
        service = self._analysis_services.get(task_id)
        if service is None:
            service = ReferenceAnalysisService(self._task_root(task_id) / "analysis")
            self._analysis_services[task_id] = service
        return service

    def _image_service(self, task_id: str) -> ImageAcquisitionService:
        service = self._image_services.get(task_id)
        if service is None:
            output = self._task_root(task_id) / "resources" / "images"
            service = self._image_service_factory(output)
            self._image_services[task_id] = service
        return service

    def _preprocessing_service(self, task_id: str) -> MediaPreprocessingService:
        service = self._preprocessing_services.get(task_id)
        if service is None:
            output = self._task_root(task_id) / "resources" / "preprocessed"
            service = self._preprocessing_service_factory(output)
            self._preprocessing_services[task_id] = service
        return service

    def media_input(self, value: Path | str) -> Path:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise PluginError("media_not_found", "媒体输入不存在")
        if not any(path.is_relative_to(root) for root in self.media_roots):
            raise PluginError(
                "media_path_not_allowed",
                "媒体输入不在 MCP 启动时声明的输入边界内",
            )
        return path

    def task_output(
        self,
        task_id: str,
        value: Path | str,
        *,
        allow_editor: bool = False,
    ) -> Path:
        path = Path(value).expanduser().resolve()
        roots = [self._task_root(task_id)]
        if allow_editor:
            roots.append((self.output_root / "editor").resolve())
        if not any(path.is_relative_to(root) for root in roots):
            raise PluginError("output_path_not_allowed", "产物路径不属于当前任务输出边界")
        return path

    def _task_root(self, task_id: str) -> Path:
        return (self.output_root / "tasks" / task_id).resolve()

    def _editing_specification(self, value: dict[str, Any]) -> EditingSpecification:
        parsed = EditingSpecification.model_validate(value)
        for asset in parsed.assets:
            self.media_input(asset.path)
        return parsed

    def _frozen_editing_specification(
        self,
        access_handle: str,
        tool_id: str,
    ) -> tuple[TaskRun, ArtifactRef, FreezeRef, EditingSpecification]:
        task, stage = self.authorize(access_handle, tool_id)
        if stage.stage_type != "execution":
            raise PluginError("stage_not_allowed", "冻结剪辑规格只供执行阶段使用")
        if len(stage.input_artifact_refs) != len(stage.input_freeze_refs):
            raise PluginError("dependency_closure_mismatch", "阶段输入冻结闭包不完整")

        matches: list[tuple[ArtifactRef, FreezeRef, Any]] = []
        for artifact_ref, freeze_ref in zip(
            stage.input_artifact_refs,
            stage.input_freeze_refs,
            strict=True,
        ):
            artifact = self.repository.get_artifact(artifact_ref.artifact_id)
            if artifact.artifact_type == "editing_specification":
                matches.append((artifact_ref, freeze_ref, artifact))
        if len(matches) != 1:
            raise PluginError(
                "frozen_spec_not_unique",
                "执行阶段必须且只能引用一份冻结剪辑规格",
            )

        artifact_ref, freeze_ref, artifact = matches[0]
        if (
            artifact.task_id != task.task_id
            or artifact.as_ref() != artifact_ref
            or artifact.status != "approved"
            or freeze_ref.artifact_id != artifact_ref.artifact_id
            or freeze_ref.artifact_revision != artifact_ref.revision
            or freeze_ref.artifact_sha256 != artifact_ref.sha256
        ):
            raise PluginError("dependency_closure_mismatch", "冻结剪辑规格引用无效")
        freeze = self.repository.get_freeze(freeze_ref.freeze_id)
        if freeze.as_ref() != freeze_ref:
            raise PluginError("dependency_closure_mismatch", "冻结剪辑规格记录无效")
        self.artifacts.verify(artifact.content_uri, artifact.content_sha256)
        try:
            value: object = json.loads(self.artifacts.read_bytes(artifact.content_uri))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise PluginError("frozen_spec_invalid", "冻结剪辑规格不是有效 JSON") from error
        if not isinstance(value, dict):
            raise PluginError("frozen_spec_invalid", "冻结剪辑规格必须是 JSON object")
        try:
            specification = self._editing_specification(value)
        except ValidationError as error:
            raise validation_error(error) from error
        preparation = self._frozen_preparation_package(task.task_id, stage)
        try:
            validate_preparation_scope(preparation, specification)
        except PreparationScopeError as error:
            raise PluginError("preparation_scope_mismatch", str(error)) from error
        return task, artifact_ref, freeze_ref, specification

    def _frozen_preparation_package(
        self,
        task_id: str,
        stage: StageRun,
    ) -> PreparationPackage:
        matches: list[tuple[ArtifactRef, FreezeRef, Any]] = []
        for artifact_ref, freeze_ref in zip(
            stage.input_artifact_refs,
            stage.input_freeze_refs,
            strict=True,
        ):
            artifact = self.repository.get_artifact(artifact_ref.artifact_id)
            if artifact.artifact_type == "preparation_package":
                matches.append((artifact_ref, freeze_ref, artifact))
        if len(matches) != 1:
            raise PluginError(
                "frozen_preparation_not_unique",
                "阶段必须且只能引用一份冻结资源包",
            )
        artifact_ref, freeze_ref, artifact = matches[0]
        freeze = self.repository.get_freeze(freeze_ref.freeze_id)
        if (
            artifact.task_id != task_id
            or artifact.as_ref() != artifact_ref
            or artifact.status != "approved"
            or freeze.as_ref() != freeze_ref
            or freeze_ref.artifact_id != artifact_ref.artifact_id
            or freeze_ref.artifact_revision != artifact_ref.revision
            or freeze_ref.artifact_sha256 != artifact_ref.sha256
        ):
            raise PluginError("dependency_closure_mismatch", "冻结资源包引用无效")
        self.artifacts.verify(artifact.content_uri, artifact.content_sha256)
        try:
            return PreparationPackage.model_validate_json(
                self.artifacts.read_bytes(artifact.content_uri)
            )
        except ValidationError as error:
            raise PluginError("frozen_preparation_invalid", "冻结资源包结构无效") from error

    def _validate_reference_context(
        self,
        *,
        task: TaskRun,
        stage: StageRun,
        binding: ReferenceContextBinding | None,
        projection_stage: CreationProjectionStage,
    ) -> None:
        if task.task_type == "original_creation":
            if binding is not None:
                raise PluginError(
                    "reference_context_forbidden",
                    "原创任务产物不得声明本次参考报告绑定",
                )
            return
        if task.task_type != "reference_guided_creation":
            raise PluginError("stage_not_allowed", "当前任务不属于视频创作阶段")
        if binding is None:
            raise PluginError(
                "reference_context_required",
                "参考引导创作产物缺少本次冻结报告绑定",
            )
        expected, _ = self._expected_reference_context(
            task,
            stage,
            projection_stage,
        )
        if binding != expected:
            raise PluginError(
                "reference_context_mismatch",
                "创作产物绑定的参考报告、冻结版本或阶段投影不一致",
            )

    def _expected_reference_context(
        self,
        task: TaskRun,
        stage: StageRun,
        projection_stage: CreationProjectionStage,
    ) -> tuple[ReferenceContextBinding, dict[str, Any]]:
        if task.task_type != "reference_guided_creation":
            raise PluginError(
                "stage_not_allowed",
                "参考报告阶段上下文只属于参考引导创作任务",
            )
        report_inputs: list[tuple[ArtifactRef, ArtifactEnvelope]] = []
        for reference in stage.input_artifact_refs:
            artifact = self.repository.get_artifact(reference.artifact_id)
            if artifact.artifact_type == "reference_report_manifest":
                report_inputs.append((reference, artifact))
        if len(report_inputs) != 1:
            raise PluginError(
                "reference_context_mismatch",
                "当前创作阶段缺少唯一冻结参考报告",
            )
        report_ref, report_artifact = report_inputs[0]
        if (
            report_artifact.task_id != task.task_id
            or report_artifact.status != "approved"
            or report_artifact.as_ref() != report_ref
        ):
            raise PluginError(
                "reference_context_mismatch",
                "当前参考报告状态或版本无效",
            )
        matching_freezes = [
            reference
            for reference in stage.input_freeze_refs
            if reference.artifact_id == report_ref.artifact_id
            and reference.artifact_revision == report_ref.revision
            and reference.artifact_sha256 == report_ref.sha256
        ]
        if len(matching_freezes) != 1:
            raise PluginError(
                "reference_context_mismatch",
                "当前参考报告缺少唯一冻结版本",
            )
        freeze_ref = matching_freezes[0]
        freeze = self.repository.get_freeze(freeze_ref.freeze_id)
        if freeze.task_id != task.task_id or freeze.as_ref() != freeze_ref:
            raise PluginError(
                "reference_context_mismatch",
                "当前参考报告冻结记录无效",
            )

        self.artifacts.verify(
            report_artifact.content_uri,
            report_artifact.content_sha256,
        )
        try:
            report = ReferenceReportManifest.model_validate_json(
                self.artifacts.read_bytes(report_artifact.content_uri)
            )
        except ValidationError as error:
            raise PluginError(
                "reference_context_mismatch",
                "当前冻结参考报告结构无效",
            ) from error
        projections = [
            projection
            for projection in report.content.creation_context_projection.stage_projections
            if projection.stage == projection_stage
        ]
        if len(projections) != 1:
            raise PluginError(
                "reference_context_mismatch",
                "当前冻结参考报告缺少唯一阶段投影",
            )
        projection = projections[0]
        projection_sha256 = hashlib.sha256(canonical_json_bytes(projection)).hexdigest()
        return (
            ReferenceContextBinding(
                source_report_ref=report_ref,
                source_report_freeze_ref=freeze_ref,
                report_content_sha256=report.report_content_sha256,
                projection_stage=projection_stage,
                stage_projection_sha256=projection_sha256,
            ),
            projection.model_dump(mode="json"),
        )

    def _register_analysis_manifest(
        self,
        access_handle: str,
        task_id: str,
        result: ReferenceAnalysisResult,
    ) -> dict[str, Any]:
        task, stage = self.authorize(access_handle, "workflow_submit_artifact")
        if task.task_id != task_id or stage.stage_type != "reference_study":
            raise PluginError("stage_not_allowed", "分析证据清单只属于参考学习阶段")
        try:
            manifest = AnalysisEvidenceManifest.model_validate_json(
                result.reference_manifest_path.read_bytes()
            )
        except (OSError, ValidationError) as error:
            raise PluginError("analysis_artifact_invalid", "分析证据清单结构无效") from error
        if (
            manifest.job_id != result.job_id
            or manifest.source.sha256 != result.source_sha256
            or manifest.evidence_bundle.sha256 != result.evidence_bundle_sha256
            or _sha256(result.reference_manifest_path) != result.reference_manifest_sha256
        ):
            raise PluginError("analysis_artifact_invalid", "分析证据清单身份或哈希不一致")

        existing_analysis_refs: list[ArtifactRef] = []
        for reference in stage.output_artifact_refs:
            artifact = self.repository.get_artifact(reference.artifact_id)
            if artifact.artifact_type != "reference_analysis_manifest":
                continue
            if artifact.content_sha256 == result.reference_manifest_sha256:
                return artifact.model_dump(mode="json")
            existing_analysis_refs.append(reference)

        artifact = self.workflow.submit_artifact(
            access_handle=access_handle,
            artifact_type="reference_analysis_manifest",
            content=self.artifacts.put_file(result.reference_manifest_path),
            schema_version=manifest.schema_version,
            producer_kind="component",
            producer_id="reference-analysis-service",
            component_version="reference-analysis-v1",
            parent_artifact_refs=existing_analysis_refs,
            evidence_refs=sorted(manifest.evidence_refs),
        )
        return artifact.model_dump(mode="json")

    def _validate_reference_report_evidence(
        self,
        report: ReferenceReportManifest,
        stage: StageRun,
    ) -> None:
        report_refs = set(report.evidence_refs)
        for reference in stage.output_artifact_refs:
            artifact = self.repository.get_artifact(reference.artifact_id)
            if artifact.artifact_type != "reference_analysis_manifest":
                continue
            self.artifacts.verify(artifact.content_uri, artifact.content_sha256)
            try:
                manifest = AnalysisEvidenceManifest.model_validate_json(
                    self.artifacts.read_bytes(artifact.content_uri)
                )
            except ValidationError as error:
                raise PluginError(
                    "analysis_artifact_invalid",
                    "阶段分析证据清单结构无效",
                ) from error
            if (
                manifest.job_id == report.analysis_id
                and manifest.source.sha256 == report.source_sha256
                and report_refs.issubset(manifest.evidence_refs)
            ):
                return
        raise PluginError(
            "report_evidence_mismatch",
            "参考报告没有匹配的哈希证据清单",
        )

    def _compiled_snapshot(
        self,
        *,
        task: TaskRun,
        artifact_ref: ArtifactRef,
        freeze_ref: FreezeRef,
        specification: EditingSpecification,
        project_id: str,
        require_task_binding: bool = True,
    ) -> tuple[EditorProject, SpecTraceMap, dict[str, Any]]:
        if _PROJECT_ID.fullmatch(project_id) is None:
            raise PluginError("invalid_project_id", "工程 ID 格式无效")
        spec_sha256 = canonical_spec_sha256(specification)
        if project_id != f"project_{spec_sha256[:16]}":
            raise PluginError("compiled_project_not_bound", "工程不属于冻结剪辑规格")
        project_root = self.project_storage.root / project_id
        project_path = project_root / "project.json"
        trace_path = project_root / "trace_map.json"
        manifest_path = project_root / "compile_manifest.json"
        manifest = _read_json(manifest_path, "compiled_project_not_found")
        project = self.project_storage.get(project_id)
        try:
            trace_map = SpecTraceMap.model_validate(
                _read_json(trace_path, "compiled_project_not_found")
            )
        except ValidationError as error:
            raise PluginError("compiled_project_corrupt", "规格追溯表结构无效") from error
        if (
            manifest.get("spec_sha256") != spec_sha256
            or manifest.get("project_id") != project.id
            or manifest.get("project_sha256") != _sha256(project_path)
            or manifest.get("trace_map_sha256") != _sha256(trace_path)
        ):
            raise PluginError("compiled_project_corrupt", "编译工程快照或追溯表已被改写")
        bindings = _compile_bindings(manifest.get("bindings"))
        manifest["bindings"] = bindings
        if (
            require_task_binding
            and _compile_binding(
                task,
                artifact_ref,
                freeze_ref,
            )
            not in bindings
        ):
            raise PluginError("compiled_project_not_bound", "工程未绑定当前任务的冻结剪辑规格")
        validate_execution_project(specification, project, trace_map)
        return project, trace_map, manifest

    def _bound_render_job(
        self,
        access_handle: str,
        render_job_id: str,
        tool_id: str,
    ) -> tuple[
        RenderJob,
        TaskRun,
        ArtifactRef,
        FreezeRef,
        EditingSpecification,
        SpecTraceMap,
        Path,
    ]:
        if _RENDER_JOB_ID.fullmatch(render_job_id) is None:
            raise PluginError("invalid_render_job_id", "渲染任务 ID 格式无效")
        task, artifact_ref, freeze_ref, specification = self._frozen_editing_specification(
            access_handle, tool_id
        )
        job = self.render_queue.get(render_job_id)
        job_dir = self.render_queue.root / render_job_id
        binding = _read_json(job_dir / "binding.json", "render_binding_not_found")
        trace_path = job_dir / "trace_map.json"
        project_snapshot_path = job_dir / "project.json"
        expected_binding = {
            **_compile_binding(task, artifact_ref, freeze_ref),
            "spec_sha256": canonical_spec_sha256(specification),
            "project_id": job.project_id,
            "project_revision": job.revision,
            "project_snapshot_sha256": _sha256(project_snapshot_path),
            "trace_map_sha256": _sha256(trace_path),
        }
        if binding != expected_binding:
            raise PluginError("render_not_bound", "渲染任务未绑定当前任务的冻结剪辑规格")
        try:
            project = EditorProject.model_validate(
                _read_json(project_snapshot_path, "render_binding_not_found")
            )
            trace_map = SpecTraceMap.model_validate(
                _read_json(trace_path, "render_binding_not_found")
            )
        except ValidationError as error:
            raise PluginError("render_binding_corrupt", "渲染快照或追溯表结构无效") from error
        if project.id != job.project_id or project.revision != job.revision:
            raise PluginError("render_binding_corrupt", "渲染工程快照身份无效")
        validate_execution_project(specification, project, trace_map)
        compiled_project, compiled_trace_map, _ = self._compiled_snapshot(
            task=task,
            artifact_ref=artifact_ref,
            freeze_ref=freeze_ref,
            specification=specification,
            project_id=job.project_id,
        )
        if project != compiled_project or trace_map != compiled_trace_map:
            raise PluginError("render_not_bound", "渲染快照与冻结规格编译快照不一致")
        request = _read_json(job_dir / "request.json", "render_binding_not_found")
        project_dir = request.get("project_dir")
        output_path = request.get("output_path")
        if (
            not isinstance(project_dir, str)
            or Path(project_dir).resolve() != (self.project_storage.root / job.project_id).resolve()
            or not isinstance(output_path, str)
            or Path(output_path).resolve() != Path(job.output_path).resolve()
        ):
            raise PluginError("render_binding_corrupt", "渲染任务请求与任务记录不一致")
        source = self.task_output(task.task_id, job.output_path, allow_editor=True)
        return (
            job,
            task,
            artifact_ref,
            freeze_ref,
            specification,
            trace_map,
            source,
        )

    def _create_inspection_binding(
        self,
        *,
        task: TaskRun,
        artifact_ref: ArtifactRef,
        freeze_ref: FreezeRef,
        job: RenderJob,
        specification: EditingSpecification,
        expectation: RenderExpectation,
        report: RenderInspectionReport,
        report_path: Path,
        contact_sheet_path: Path,
        source: Path,
    ) -> RenderInspectionBinding:
        payload = self._inspection_binding_payload(
            task=task,
            artifact_ref=artifact_ref,
            freeze_ref=freeze_ref,
            job=job,
            specification=specification,
            expectation=expectation,
            report=report,
            report_path=report_path,
            contact_sheet_path=contact_sheet_path,
            source=source,
        )
        return RenderInspectionBinding(
            payload=payload,
            hmac_sha256=self._inspection_binding_hmac(payload),
        )

    def _inspection_binding_payload(
        self,
        *,
        task: TaskRun,
        artifact_ref: ArtifactRef,
        freeze_ref: FreezeRef,
        job: RenderJob,
        specification: EditingSpecification,
        expectation: RenderExpectation,
        report: RenderInspectionReport,
        report_path: Path,
        contact_sheet_path: Path,
        source: Path,
    ) -> RenderInspectionBindingPayload:
        assessment = preflight_spec(specification)
        project_root = (self.project_storage.root / job.project_id).resolve()
        job_root = (self.render_queue.root / job.id).resolve()
        compiled_project_path = project_root / "project.json"
        trace_map_path = project_root / "trace_map.json"
        render_project_snapshot_path = job_root / "project.json"
        render_trace_map_path = job_root / "trace_map.json"
        return RenderInspectionBindingPayload(
            task_id=task.task_id,
            render_job_id=job.id,
            editing_artifact_ref=artifact_ref,
            editing_freeze_ref=freeze_ref,
            spec_sha256=canonical_spec_sha256(specification),
            capability_registry_version=assessment.registry_version,
            project_id=job.project_id,
            project_revision=job.revision,
            compiled_project_path=str(compiled_project_path),
            compiled_project_sha256=_sha256(compiled_project_path),
            render_project_snapshot_path=str(render_project_snapshot_path),
            render_project_snapshot_sha256=_sha256(render_project_snapshot_path),
            trace_map_path=str(trace_map_path),
            trace_map_sha256=_sha256(trace_map_path),
            render_trace_map_path=str(render_trace_map_path),
            render_trace_map_sha256=_sha256(render_trace_map_path),
            render_path=str(source.resolve()),
            render_sha256=_sha256(source),
            expectation_sha256=_canonical_sha256(expectation.model_dump(mode="json")),
            inspection_path=str(report_path.resolve()),
            inspection_sha256=_sha256(report_path),
            inspection_passed=report.passed,
            contact_sheet_path=str(contact_sheet_path.resolve()),
            contact_sheet_sha256=_sha256(contact_sheet_path),
        )

    def _inspection_binding_hmac(self, payload: RenderInspectionBindingPayload) -> str:
        return hmac.new(
            self._inspection_binding_key(),
            _canonical_json_bytes(payload.model_dump(mode="json")),
            hashlib.sha256,
        ).hexdigest()

    def _inspection_binding_key(self) -> bytes:
        path = self.output_root / ".inspection-binding.key"
        try:
            key = path.read_bytes() if path.exists() else b""
            if not key:
                generated = secrets.token_bytes(32)
                try:
                    descriptor = os.open(
                        path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except FileExistsError:
                    key = path.read_bytes()
                else:
                    with os.fdopen(descriptor, "wb") as file:
                        file.write(generated)
                        file.flush()
                        os.fsync(file.fileno())
                    key = generated
        except OSError as error:
            raise PluginError(
                "inspection_binding_key_unavailable",
                "渲染检查绑定密钥读写失败",
            ) from error
        if len(key) != 32:
            raise PluginError("inspection_binding_key_invalid", "渲染检查绑定密钥结构无效")
        return key

    def _validate_execution_manifest(
        self,
        access_handle: str,
        execution: ExecutionManifest,
    ) -> None:
        (
            job,
            task,
            artifact_ref,
            freeze_ref,
            specification,
            trace_map,
            source,
        ) = self._bound_render_job(
            access_handle,
            execution.render_job_id,
            "workflow_submit_artifact",
        )
        if job.status != "succeeded":
            raise PluginError("render_not_ready", "执行清单引用的渲染任务尚未成功完成")
        if not source.is_file():
            raise PluginError("render_output_not_found", "执行清单引用的成片不存在")

        assessment = preflight_spec(specification)
        project_path = (self.project_storage.root / job.project_id / "project.json").resolve()
        trace_map_path = (self.project_storage.root / job.project_id / "trace_map.json").resolve()
        binding_path = (self.render_queue.root / job.id / "inspection_binding.json").resolve()
        manifest_paths = {
            "project": self.task_output(task.task_id, execution.project_path, allow_editor=True),
            "trace_map": self.task_output(
                task.task_id,
                execution.trace_map_path,
                allow_editor=True,
            ),
            "render": self.task_output(task.task_id, execution.render_path, allow_editor=True),
            "inspection": self.task_output(
                task.task_id,
                execution.inspection_path,
                allow_editor=True,
            ),
            "inspection_binding": self.task_output(
                task.task_id,
                execution.inspection_binding_path,
                allow_editor=True,
            ),
        }
        if (
            execution.spec_sha256 != canonical_spec_sha256(specification)
            or execution.capability_registry_version != assessment.registry_version
            or execution.project_id != job.project_id
            or manifest_paths["project"] != project_path
            or manifest_paths["trace_map"] != trace_map_path
            or manifest_paths["render"] != source.resolve()
            or manifest_paths["inspection_binding"] != binding_path
        ):
            raise PluginError(
                "execution_binding_mismatch",
                "执行清单与当前冻结规格、编译工程或渲染任务不一致",
            )
        for path, expected_sha256 in (
            (project_path, execution.project_sha256),
            (trace_map_path, execution.trace_map_sha256),
            (source, execution.render_sha256),
            (manifest_paths["inspection"], execution.inspection_sha256),
            (binding_path, execution.inspection_binding_sha256),
        ):
            if not path.is_file() or _sha256(path) != expected_sha256:
                raise PluginError(
                    "artifact_hash_mismatch",
                    "执行清单引用的绑定产物缺失或哈希不匹配",
                )

        binding = self._read_inspection_binding(binding_path)
        expected_hmac = self._inspection_binding_hmac(binding.payload)
        if not hmac.compare_digest(binding.hmac_sha256, expected_hmac):
            raise PluginError("inspection_binding_invalid", "渲染检查绑定认证失败")
        report_path = manifest_paths["inspection"]
        if report_path != Path(binding.payload.inspection_path).resolve():
            raise PluginError("execution_binding_mismatch", "执行清单引用的检查报告未绑定渲染任务")
        contact_sheet_path = self.task_output(
            task.task_id,
            binding.payload.contact_sheet_path,
        )
        report = self._read_render_inspection_report(report_path)
        expectation = _render_expectation(specification, trace_map)
        self._validate_render_inspection_report(report, source, expectation)
        if not report.passed:
            raise PluginError("render_inspection_failed", "执行清单引用的渲染检查未通过")
        expected_payload = self._inspection_binding_payload(
            task=task,
            artifact_ref=artifact_ref,
            freeze_ref=freeze_ref,
            job=job,
            specification=specification,
            expectation=expectation,
            report=report,
            report_path=report_path,
            contact_sheet_path=contact_sheet_path,
            source=source,
        )
        if binding.payload != expected_payload:
            raise PluginError("inspection_binding_invalid", "渲染检查绑定与当前执行证据不一致")
        if (
            execution.project_sha256 != expected_payload.compiled_project_sha256
            or execution.trace_map_sha256 != expected_payload.trace_map_sha256
            or execution.render_sha256 != expected_payload.render_sha256
            or execution.inspection_sha256 != expected_payload.inspection_sha256
            or execution.inspection_passed != expected_payload.inspection_passed
        ):
            raise PluginError("execution_binding_mismatch", "执行清单字段与渲染检查绑定不一致")

    @staticmethod
    def _read_render_inspection_report(path: Path) -> RenderInspectionReport:
        try:
            return RenderInspectionReport.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as error:
            raise PluginError("inspection_report_invalid", "渲染检查报告结构无效") from error

    @staticmethod
    def _validate_render_inspection_report(
        report: RenderInspectionReport,
        source: Path,
        expectation: RenderExpectation,
    ) -> None:
        required_checks = {
            "duration",
            "canvas",
            "fps",
            "decode",
            "black_frames",
            "freeze_run",
            "audio_presence",
            "asset_diversity",
            "trace_coverage",
            "hard_cut_boundaries",
            "beat_alignment",
            "overlay_events",
        }
        if expectation.expected_audio:
            required_checks.update({"audio_level", "audio_clipping"})
        check_codes = [check.code for check in report.checks]
        if (
            Path(report.source_path).resolve() != source.resolve()
            or report.source_sha256 != _sha256(source)
            or len(check_codes) != len(set(check_codes))
            or set(check_codes) != required_checks
            or report.passed != all(check.passed for check in report.checks)
        ):
            raise PluginError(
                "inspection_report_mismatch",
                "渲染检查报告与成片或验收项目不一致",
            )

    @staticmethod
    def _read_inspection_binding(path: Path) -> RenderInspectionBinding:
        try:
            return RenderInspectionBinding.model_validate_json(path.read_bytes())
        except (OSError, ValidationError) as error:
            raise PluginError("inspection_binding_invalid", "渲染检查绑定结构无效") from error

    def _validate_primary_stage_content(
        self,
        access_handle: str,
        artifact_type: str,
        content: str,
    ) -> None:
        task, stage = self.authorize(access_handle, "workflow_submit_artifact")
        expected_artifact_types = {
            "reference_study": "reference_report_manifest",
            "knowledge_publication": "knowledge_publication",
            "creative_direction": "creative_direction",
            "resource_preparation": "preparation_package",
            "editing_specification": "editing_specification",
            "execution": "execution_manifest",
        }
        if artifact_type != expected_artifact_types[stage.stage_type]:
            raise PluginError("artifact_type_invalid", "阶段主产物类型与阶段契约不一致")
        if stage.stage_type == "creative_direction":
            if not content.strip():
                raise PluginError("artifact_schema_invalid", "阶段主产物不能为空")
            return
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as error:
            raise PluginError("artifact_schema_invalid", "阶段主产物必须是有效 JSON") from error
        if not isinstance(payload, dict):
            raise PluginError("artifact_schema_invalid", "阶段主产物必须是 JSON object")
        retrievals: tuple[str, list[str]] | None = None
        try:
            if stage.stage_type == "reference_study":
                report = ReferenceReportManifest.model_validate(payload)
                self._validate_reference_report_evidence(report, stage)
            elif stage.stage_type == "resource_preparation":
                preparation = PreparationPackage.model_validate(payload)
                self._validate_reference_context(
                    task=task,
                    stage=stage,
                    binding=preparation.reference_context,
                    projection_stage="resource_preparation",
                )
                resource_identities = [
                    (material.path, material.sha256) for material in preparation.materials
                ]
                resource_identities.append((preparation.bgm.path, preparation.bgm.sha256))
                for resource_path, resource_sha256 in resource_identities:
                    path = self.media_input(resource_path)
                    if _sha256(path) != resource_sha256:
                        raise PluginError(
                            "artifact_hash_mismatch",
                            "资源包引用的媒体哈希不匹配",
                        )
                retrievals = ("stage2", preparation.retrieval_ids)
            elif stage.stage_type == "editing_specification":
                specification = EditingSpecification.model_validate(payload)
                self._validate_reference_context(
                    task=task,
                    stage=stage,
                    binding=specification.reference_context,
                    projection_stage="editing_specification",
                )
                preparation = self._frozen_preparation_package(task.task_id, stage)
                try:
                    validate_preparation_scope(preparation, specification)
                except PreparationScopeError as error:
                    raise PluginError("preparation_scope_mismatch", str(error)) from error
                retrievals = ("stage3", specification.retrieval_ids)
            elif stage.stage_type == "execution":
                execution = ExecutionManifest.model_validate(payload)
                self._validate_execution_manifest(access_handle, execution)
            elif stage.stage_type == "knowledge_publication":
                from video_create_plugin.knowledge import Publication

                publication = Publication.model_validate_json(content)
                self._validate_knowledge_publication_binding(publication, task, stage)
        except ValidationError as error:
            raise validation_error(error) from error
        if retrievals is not None:
            creation_stage, retrieval_ids = retrievals
            self.audited_knowledge.validate_stage_retrievals(
                task_id=task.task_id,
                stage_run_id=stage.stage_run_id,
                stage=cast(Any, creation_stage),
                retrieval_ids=retrieval_ids,
                require_shared_hit=True,
            )

    def _validate_knowledge_publication_binding(
        self,
        publication: Any,
        task: TaskRun,
        stage: StageRun,
    ) -> None:
        try:
            stored = self.knowledge_store.get_publication(publication.publication_id)
        except PluginError as error:
            if error.code == "knowledge_publication_not_found":
                raise PluginError(
                    "knowledge_publication_not_published",
                    "知识发布主产物尚未通过 knowledge_publish 写入知识库",
                ) from error
            raise
        if stored != publication:
            raise PluginError(
                "knowledge_publication_store_mismatch",
                "知识发布主产物与知识库记录不一致",
            )
        active = self.knowledge_store.get_unique_active_publication(publication.source_media_sha256)
        if publication.status != "active" or active != publication:
            raise PluginError(
                "knowledge_publication_not_active",
                "知识发布主产物不是来源媒体的当前 active 版本",
            )
        if publication.source_task_id != task.task_id:
            raise PluginError(
                "knowledge_publication_task_mismatch",
                "知识发布主产物与当前任务不一致",
            )

        report_inputs = []
        for reference in stage.input_artifact_refs:
            artifact = self.repository.get_artifact(reference.artifact_id)
            if artifact.artifact_type == "reference_report_manifest":
                report_inputs.append(reference)
        if report_inputs != [publication.source_report_ref]:
            raise PluginError(
                "knowledge_publication_report_mismatch",
                "知识发布主产物与当前冻结报告不一致",
            )
        matching_freezes = [
            reference
            for reference in stage.input_freeze_refs
            if reference.freeze_id == publication.freeze_id
            and reference.artifact_id == publication.source_report_ref.artifact_id
            and reference.artifact_revision == publication.source_report_ref.revision
            and reference.artifact_sha256 == publication.source_report_ref.sha256
        ]
        if len(matching_freezes) != 1:
            raise PluginError(
                "knowledge_publication_freeze_mismatch",
                "知识发布主产物与当前报告冻结版本不一致",
            )

        report_artifact = self.repository.get_artifact(publication.source_report_ref.artifact_id)
        if (
            report_artifact.task_id != task.task_id
            or report_artifact.status != "approved"
            or report_artifact.as_ref() != publication.source_report_ref
        ):
            raise PluginError(
                "knowledge_publication_report_mismatch",
                "知识发布主产物引用的报告状态或版本无效",
            )
        self.artifacts.verify(
            report_artifact.content_uri,
            report_artifact.content_sha256,
        )
        try:
            report = ReferenceReportManifest.model_validate_json(
                self.artifacts.read_bytes(report_artifact.content_uri)
            )
        except ValidationError as error:
            raise PluginError(
                "knowledge_publication_report_mismatch",
                "知识发布主产物引用的报告结构无效",
            ) from error
        if report.source_sha256 != publication.source_media_sha256:
            raise PluginError(
                "knowledge_publication_report_mismatch",
                "知识发布主产物的来源媒体与当前冻结报告不一致",
            )

    def _start_render_worker(self) -> None:
        with self._render_worker_lock:
            if not self._render_worker_started:
                self.render_queue.start()
                self._render_worker_started = True

    @staticmethod
    def _validate_json_artifact(path: Path) -> None:
        try:
            if path.suffix.lower() == ".jsonl":
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        json.loads(line)
            else:
                json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PluginError("analysis_artifact_invalid", "分析 JSON 产物结构无效") from error


def default_application() -> PluginApplication:
    source_root = Path(__file__).resolve().parents[2]
    configured_project = os.environ.get("HUAYANG_PROJECT_ROOT")
    project_root = (
        Path(configured_project).expanduser().resolve() if configured_project else source_root
    )
    configured_output = os.environ.get("HUAYANG_OUTPUT_ROOT")
    default_output = (
        project_root / "output" / "plugin"
        if (project_root / "rules" / "main-agent.md").is_file()
        else Path.home() / ".huayang" / "output"
    )
    output_root = (
        Path(configured_output).expanduser().resolve() if configured_output else default_output
    )
    roots = [
        Path(value)
        for value in os.environ.get("HUAYANG_MEDIA_ROOTS", "").split(os.pathsep)
        if value.strip()
    ]
    # 项目 output 目录默认作为媒体输入边界，覆盖素材下载和 BGM 产物
    project_output = (project_root / "output").resolve()
    if project_output.is_dir() and project_output not in roots:
        roots.append(project_output)
    return PluginApplication(
        output_root,
        project_root=project_root,
        media_roots=roots,
    )


def validation_error(error: ValidationError) -> PluginError:
    return PluginError(
        "invalid_request",
        "请求结构校验失败",
        details={
            "errors": error.errors(include_input=False, include_url=False),
        },
    )


def _artifact_object_dict(value: Any) -> dict[str, Any]:
    return {
        "uri": value.uri,
        "sha256": value.sha256,
        "size": value.size,
        "path": str(value.path),
    }


def _creation_projection_stage(
    stage_type: str,
) -> CreationProjectionStage:
    if stage_type not in {
        "creative_direction",
        "resource_preparation",
        "editing_specification",
    }:
        raise PluginError("stage_not_allowed", "当前阶段没有参考报告创作投影")
    return cast(CreationProjectionStage, stage_type)


def _compile_binding(
    task: TaskRun,
    artifact_ref: ArtifactRef,
    freeze_ref: FreezeRef,
) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "editing_artifact_ref": artifact_ref.model_dump(mode="json"),
        "editing_freeze_ref": freeze_ref.model_dump(mode="json"),
    }


def _compile_bindings(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PluginError("compiled_project_corrupt", "编译工程绑定记录无效")
    bindings: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "task_id",
            "editing_artifact_ref",
            "editing_freeze_ref",
        }:
            raise PluginError("compiled_project_corrupt", "编译工程绑定记录无效")
        task_id = item["task_id"]
        if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
            raise PluginError("compiled_project_corrupt", "编译工程绑定记录无效")
        try:
            artifact_ref = ArtifactRef.model_validate(item["editing_artifact_ref"])
            freeze_ref = FreezeRef.model_validate(item["editing_freeze_ref"])
        except ValidationError as error:
            raise PluginError("compiled_project_corrupt", "编译工程绑定记录无效") from error
        canonical = {
            "task_id": task_id,
            "editing_artifact_ref": artifact_ref.model_dump(mode="json"),
            "editing_freeze_ref": freeze_ref.model_dump(mode="json"),
        }
        if canonical != item:
            raise PluginError("compiled_project_corrupt", "编译工程绑定记录无效")
        bindings.append(canonical)
    return bindings


def _render_expectation(
    specification: EditingSpecification,
    trace_map: SpecTraceMap,
) -> RenderExpectation:
    overlays = [
        OverlayExpectation(
            overlay_id=action.action_id,
            start_us=action.timeline.start_us,
            end_us=action.timeline.end_us,
            x=action.transform.x,
            y=action.transform.y,
            width=action.transform.width,
            height=action.transform.height,
        )
        for action in specification.actions
        if action.action_type == "visual_media"
        and action.layer > 0
        and action.transform is not None
    ]
    visual_asset_ids = {
        action.asset_id
        for action in specification.actions
        if action.action_type == "visual_media" and action.asset_id is not None
    }
    visual_sha256s = [
        asset.sha256 for asset in specification.assets if asset.asset_id in visual_asset_ids
    ]
    return RenderExpectation(
        duration_us=specification.duration_us,
        width=specification.canvas.width,
        height=specification.canvas.height,
        fps=specification.canvas.fps,
        shot_boundaries_us=[shot.timeline.end_us for shot in specification.shots[:-1]],
        beat_grid_us=specification.beat_grid_us,
        overlays=overlays,
        expected_audio=any(action.action_type == "audio_media" for action in specification.actions),
        asset_sha256s=visual_sha256s,
        minimum_distinct_assets=max(1, len(set(visual_sha256s))),
        action_count=len(specification.actions),
        traced_action_count=len(trace_map.entries),
    )


def _read_json(path: Path, missing_code: str) -> dict[str, Any]:
    if not path.is_file():
        raise PluginError(missing_code, "JSON 产物不存在")
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PluginError("artifact_corrupt", "JSON 产物结构无效") from error
    if not isinstance(value, Mapping):
        raise PluginError("artifact_corrupt", "JSON 产物必须为对象")
    return {str(key): item for key, item in value.items()}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, path)
    except OSError as error:
        raise PluginError("output_unavailable", "JSON 产物写入失败") from error
    finally:
        temporary.unlink(missing_ok=True)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as error:
        raise PluginError("artifact_read_failed", "文件哈希读取失败") from error
    return digest.hexdigest()
