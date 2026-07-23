from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from validation.reference_publication import build_fixture_evidence_manifest
from video_create_plugin.editing import (
    EditingSpecification,
    SpecTraceEntry,
    SpecTraceMap,
)
from video_create_plugin.execution import ExecutionManifest
from video_create_plugin.knowledge import SearchResult
from video_create_plugin.mcp.application import PluginApplication
from video_create_plugin.models import (
    ArtifactEnvelope,
    FreezeRecord,
    ReferenceContextBinding,
    StageEnvelope,
    TaskType,
)
from video_create_plugin.reporting import (
    ReferenceReportManifest,
    StageKnowledgeProjection,
)
from video_create_plugin.retrieval import (
    CreationStage,
    RetrievalAudit,
)

from .files import canonical_json_bytes, sha256_file, write_bytes, write_json
from .media import ResolvedBgm, ResolvedVisualMaterial, ScenarioMediaResolver
from .planning import ScenarioPlanner
from .scenario import KnowledgeQueryDefinition, ScenarioDefinition

ModelT = TypeVar("ModelT", bound=BaseModel)


class ScenarioExecutionError(RuntimeError):
    def __init__(self, message: str, *, evidence_path: Path) -> None:
        super().__init__(message)
        self.evidence_path = evidence_path


@dataclass(frozen=True, slots=True)
class StageEvidence:
    artifact: ArtifactEnvelope
    freeze: FreezeRecord
    retrieval: RetrievalAudit
    search_result: SearchResult


@dataclass(frozen=True, slots=True)
class ReferenceFixture:
    slug: str
    source_media_root: Path | None = None


@dataclass(frozen=True, slots=True)
class ReferenceStageContext:
    binding: ReferenceContextBinding
    projection: StageKnowledgeProjection


@dataclass(frozen=True, slots=True)
class ScenarioRunResult:
    scenario_id: str
    task_id: str
    output_dir: Path
    specification_path: Path
    project_path: Path
    trace_map_path: Path
    render_path: Path
    execution_report_path: Path
    inspection_report_path: Path
    run_manifest_path: Path
    run_manifest_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "scenario_id": self.scenario_id,
            "task_id": self.task_id,
            "output_dir": str(self.output_dir),
            "specification_path": str(self.specification_path),
            "project_path": str(self.project_path),
            "trace_map_path": str(self.trace_map_path),
            "render_path": str(self.render_path),
            "execution_report_path": str(self.execution_report_path),
            "inspection_report_path": str(self.inspection_report_path),
            "run_manifest_path": str(self.run_manifest_path),
            "run_manifest_sha256": self.run_manifest_sha256,
        }


class CreationScenarioRunner:
    """组合公共工作流、知识、编译、渲染和检查 API 的真实创作验证。"""

    def __init__(
        self,
        *,
        project_root: Path | str,
        plugin_output_root: Path | str,
        planner: ScenarioPlanner | None = None,
        media_resolver: ScenarioMediaResolver | None = None,
        application: PluginApplication | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.plugin_output_root = Path(plugin_output_root).resolve()
        self.planner = planner or ScenarioPlanner()
        self.media_resolver = media_resolver or ScenarioMediaResolver(self.project_root)
        self.application = application

    def run(
        self,
        scenario: ScenarioDefinition,
        output_dir: Path | str,
        *,
        reference_fixture: ReferenceFixture | None = None,
    ) -> ScenarioRunResult:
        run_root = Path(output_dir).resolve()
        run_root.mkdir(parents=True, exist_ok=True)
        scenario_path = run_root / "scenario.json"
        write_json(scenario_path, scenario.model_dump(mode="json"))

        application = self.application or PluginApplication(
            self.plugin_output_root,
            project_root=self.project_root,
            media_roots=(run_root, self.project_root),
        )
        task_type: TaskType = (
            "reference_guided_creation" if reference_fixture is not None else "original_creation"
        )
        task = application.create_task(task_type, None)
        task_id = str(task["task_id"])
        reference_study = (
            self._run_reference_study(
                application=application,
                task_id=task_id,
                fixture=reference_fixture,
                run_root=run_root,
                scenario_id=scenario.scenario_id,
            )
            if reference_fixture is not None
            else None
        )

        stage1 = self._run_confirmed_stage(
            application=application,
            task_id=task_id,
            expected_stage="creative_direction",
            creation_stage="stage1",
            query=scenario.knowledge_queries.stage1,
            output_path=run_root / "stage1_creative_direction.md",
            artifact_type="creative_direction",
            build=lambda retrieval_id, result, projection: self.planner.build_creative_direction(
                scenario,
                result,
                projection,
            ),
            scenario_id=scenario.scenario_id,
            reference_guided=reference_fixture is not None,
        )

        stage2_envelope = self._stage_envelope(application, task_id)
        stage2_reference = self._load_reference_context(
            application,
            stage2_envelope,
            enabled=reference_fixture is not None,
        )
        stage2_audit, stage2_result = self._search_stage(
            application,
            stage2_envelope,
            scenario.knowledge_queries.stage2,
            "stage2",
        )
        visual_materials = self.media_resolver.resolve_visuals(
            scenario, run_root / "derived_materials"
        )
        bgm = self.media_resolver.resolve_bgm(scenario, run_root / "bgm")
        preparation = self.planner.build_preparation_package(
            scenario,
            visual_materials,
            bgm,
            stage2_audit.retrieval_id,
            stage2_result,
            stage2_reference.projection if stage2_reference is not None else None,
        )
        preparation = self._attach_reference_context(
            preparation,
            stage2_reference,
        )
        stage2 = self._submit_and_confirm(
            application=application,
            envelope=stage2_envelope,
            output_path=run_root / "stage2_preparation_package.json",
            content=preparation,
            artifact_type="preparation_package",
            scenario_id=scenario.scenario_id,
            evidence_refs=[f"retrieval://{stage2_audit.retrieval_id}"],
        )
        stage2_evidence = StageEvidence(
            artifact=stage2[0],
            freeze=stage2[1],
            retrieval=stage2_audit,
            search_result=stage2_result,
        )

        stage3_envelope = self._stage_envelope(application, task_id)
        stage3_reference = self._load_reference_context(
            application,
            stage3_envelope,
            enabled=reference_fixture is not None,
        )
        stage3_audit, stage3_result = self._search_stage(
            application,
            stage3_envelope,
            scenario.knowledge_queries.stage3,
            "stage3",
        )
        specification = self.planner.build_editing_specification(
            scenario,
            visual_materials,
            bgm,
            stage3_audit.retrieval_id,
            stage3_result,
            stage3_reference.projection if stage3_reference is not None else None,
        )
        specification = self._attach_reference_context(
            specification,
            stage3_reference,
        )
        stage3 = self._submit_and_confirm(
            application=application,
            envelope=stage3_envelope,
            output_path=run_root / "stage3_editing_specification.json",
            content=specification,
            artifact_type="editing_specification",
            scenario_id=scenario.scenario_id,
            evidence_refs=[f"retrieval://{stage3_audit.retrieval_id}"],
        )
        stage3_evidence = StageEvidence(
            artifact=stage3[0],
            freeze=stage3[1],
            retrieval=stage3_audit,
            search_result=stage3_result,
        )

        execution_envelope = self._stage_envelope(application, task_id)
        if execution_envelope.stage != "execution":
            raise ScenarioExecutionError(
                "阶段三冻结后没有进入 execution",
                evidence_path=run_root / "stage3_editing_specification.json",
            )
        assessment = application.editor_preflight(execution_envelope.stage_access_handle)
        preflight_path = run_root / "preflight.json"
        write_json(preflight_path, assessment)
        if not bool(assessment["supported"]):
            raise ScenarioExecutionError("能力预检存在缺口", evidence_path=preflight_path)

        compiled = application.editor_compile(execution_envelope.stage_access_handle)
        trace_map = SpecTraceMap.model_validate(compiled["trace_map"])
        project_id = str(compiled["project"]["id"])
        validation = application.editor_validate_execution_project(
            execution_envelope.stage_access_handle,
            project_id,
        )
        if not bool(validation["valid"]):
            validation_path = run_root / "execution_project_validation.json"
            write_json(validation_path, validation)
            raise ScenarioExecutionError(
                "编译工程与冻结剪辑规格不一致",
                evidence_path=validation_path,
            )
        project_dir = Path(str(compiled["project_dir"])).resolve()
        authoritative_project_path = project_dir / "project.json"
        authoritative_trace_path = project_dir / "trace_map.json"

        submitted_render = application.editor_submit_render(
            execution_envelope.stage_access_handle,
            project_id,
        )
        render_job_id = str(submitted_render["id"])
        render_job = self._wait_for_render(
            application,
            execution_envelope.stage_access_handle,
            render_job_id,
            run_root / "render_job.json",
        )
        authoritative_render_path = Path(str(render_job["output_path"])).resolve()

        inspected = application.editor_inspect_render(
            execution_envelope.stage_access_handle,
            render_job_id,
        )
        expectation = self.planner.render_expectation(scenario, specification).model_copy(
            update={"traced_action_count": len(trace_map.entries)}
        )
        expectation_path = run_root / "render_expectation.json"
        write_json(expectation_path, expectation.model_dump(mode="json"))
        authoritative_inspection_path = Path(str(inspected["report_path"])).resolve()
        authoritative_contact_sheet_path = Path(str(inspected["contact_sheet_path"])).resolve()
        authoritative_binding_path = Path(str(inspected["inspection_binding_path"])).resolve()
        inspection_report = dict(inspected["report"])

        project_path = self._snapshot_file(
            authoritative_project_path,
            run_root / "project" / "project.json",
        )
        self._snapshot_tree(project_dir / "assets", run_root / "project" / "assets")
        trace_path = self._snapshot_file(
            authoritative_trace_path,
            run_root / "spec_trace_map.json",
        )
        render_path = self._snapshot_file(
            authoritative_render_path,
            run_root / "render.mp4",
        )
        inspection_report_path = self._snapshot_file(
            authoritative_inspection_path,
            run_root / "inspection" / "render_inspection.json",
        )
        self._snapshot_file(
            authoritative_contact_sheet_path,
            run_root / "inspection" / "contact_sheet.jpg",
        )
        self._snapshot_file(
            authoritative_binding_path,
            run_root / "inspection" / "inspection_binding.json",
        )
        execution_report_path = run_root / "execution_report.json"
        write_json(
            execution_report_path,
            self._execution_report(
                scenario,
                specification,
                visual_materials,
                bgm,
                trace_map.entries,
                inspection_report,
                [stage1, stage2_evidence, stage3_evidence],
            ),
        )

        execution_artifact: ArtifactEnvelope | None = None
        if bool(inspection_report["passed"]):
            execution_manifest = ExecutionManifest(
                render_job_id=render_job_id,
                spec_sha256=trace_map.spec_sha256,
                capability_registry_version=str(assessment["registry_version"]),
                project_id=project_id,
                project_path=str(authoritative_project_path),
                project_sha256=sha256_file(authoritative_project_path),
                trace_map_path=str(authoritative_trace_path),
                trace_map_sha256=sha256_file(authoritative_trace_path),
                render_path=str(authoritative_render_path),
                render_sha256=sha256_file(authoritative_render_path),
                inspection_path=str(authoritative_inspection_path),
                inspection_sha256=sha256_file(authoritative_inspection_path),
                inspection_binding_path=str(authoritative_binding_path),
                inspection_binding_sha256=sha256_file(authoritative_binding_path),
                inspection_passed=True,
            )
            execution_manifest_path = run_root / "execution_manifest.json"
            write_json(
                execution_manifest_path,
                execution_manifest.model_dump(mode="json"),
            )
            execution_artifact = ArtifactEnvelope.model_validate_json(
                json.dumps(
                    application.submit_artifact(
                        access_handle=execution_envelope.stage_access_handle,
                        artifact_type="execution_manifest",
                        content=execution_manifest_path.read_text(encoding="utf-8"),
                        schema_version="1.0",
                        producer_kind="component",
                        producer_id="creation-e2e-runner",
                        primary=True,
                        parent_artifact_refs=[
                            item.model_dump(mode="json")
                            for item in execution_envelope.input_artifacts
                        ],
                        evidence_refs=[
                            authoritative_inspection_path.as_uri(),
                            authoritative_binding_path.as_uri(),
                        ],
                        rule_version=None,
                        skill_versions=None,
                        model_id=None,
                        component_version="creation-e2e-v2",
                    ),
                    ensure_ascii=False,
                )
            )

        application_outputs = {
            "project_path": str(authoritative_project_path),
            "project_sha256": sha256_file(authoritative_project_path),
            "trace_map_path": str(authoritative_trace_path),
            "trace_map_sha256": sha256_file(authoritative_trace_path),
            "render_job_id": render_job_id,
            "render_path": str(authoritative_render_path),
            "render_sha256": sha256_file(authoritative_render_path),
            "inspection_path": str(authoritative_inspection_path),
            "inspection_sha256": sha256_file(authoritative_inspection_path),
            "inspection_binding_path": str(authoritative_binding_path),
            "inspection_binding_sha256": sha256_file(authoritative_binding_path),
        }
        manifest_path, manifest_sha256 = self._write_run_manifest(
            run_root=run_root,
            scenario=scenario,
            task_id=task_id,
            stages=[stage1, stage2_evidence, stage3_evidence],
            execution_artifact=execution_artifact,
            inspection_passed=bool(inspection_report["passed"]),
            application_outputs=application_outputs,
            task_type=task_type,
            reference_study=reference_study,
        )
        if not bool(inspection_report["passed"]):
            raise ScenarioExecutionError(
                "成片检查存在未通过项目",
                evidence_path=inspection_report_path,
            )
        return ScenarioRunResult(
            scenario_id=scenario.scenario_id,
            task_id=task_id,
            output_dir=run_root,
            specification_path=run_root / "stage3_editing_specification.json",
            project_path=project_path,
            trace_map_path=trace_path,
            render_path=render_path,
            execution_report_path=execution_report_path,
            inspection_report_path=inspection_report_path,
            run_manifest_path=manifest_path,
            run_manifest_sha256=manifest_sha256,
        )

    def _run_reference_study(
        self,
        *,
        application: PluginApplication,
        task_id: str,
        fixture: ReferenceFixture,
        run_root: Path,
        scenario_id: str,
    ) -> dict[str, Any]:
        fixture_root = self.project_root / "validation" / "reference_studies"
        report_path = fixture_root / fixture.slug / "report_manifest.json"
        if not report_path.is_file():
            raise ScenarioExecutionError(
                "参考学习 fixture 报告不存在",
                evidence_path=report_path,
            )
        report = ReferenceReportManifest.model_validate_json(report_path.read_bytes())
        evidence_manifest, source_verified = build_fixture_evidence_manifest(
            slug=fixture.slug,
            report=report,
            project_root=self.project_root,
            source_media_root=fixture.source_media_root,
        )
        reference_root = run_root / "reference_study"
        analysis_manifest_path = reference_root / "analysis_evidence_manifest.json"
        report_snapshot_path = reference_root / "reference_report_manifest.json"
        write_json(
            analysis_manifest_path,
            evidence_manifest.model_dump(mode="json"),
        )
        self._snapshot_file(report_path, report_snapshot_path)

        study = self._stage_envelope(application, task_id)
        if study.stage != "reference_study":
            raise ScenarioExecutionError(
                "参考驱动任务没有从参考学习阶段开始",
                evidence_path=analysis_manifest_path,
            )
        analysis_artifact = application.workflow.submit_artifact(
            access_handle=study.stage_access_handle,
            artifact_type="reference_analysis_manifest",
            content=application.artifacts.put_file(analysis_manifest_path),
            schema_version=evidence_manifest.schema_version,
            producer_kind="component",
            producer_id="creation-e2e-reference-indexer",
            component_version="creation-e2e-reference-v1",
            evidence_refs=sorted(evidence_manifest.evidence_refs),
        )
        report_stage = self._stage_envelope(application, task_id)
        report_artifact = ArtifactEnvelope.model_validate_json(
            json.dumps(
                application.submit_artifact(
                    access_handle=report_stage.stage_access_handle,
                    artifact_type="reference_report_manifest",
                    content=report_path.read_text(encoding="utf-8"),
                    schema_version=report.schema_version,
                    producer_kind="agent",
                    producer_id="creation-e2e-reference-agent",
                    primary=True,
                    parent_artifact_refs=[analysis_artifact.as_ref().model_dump(mode="json")],
                    evidence_refs=report.evidence_refs,
                    rule_version="reference-study-e2e-v1",
                    skill_versions=[],
                    model_id="tracked-reference-semantic-v1",
                    component_version=None,
                ),
                ensure_ascii=False,
            )
        )
        approval = self._stage_envelope(application, task_id)
        freeze = FreezeRecord.model_validate_json(
            json.dumps(
                application.record_approval(
                    access_handle=approval.stage_access_handle,
                    user_confirmation_ref=(f"validation://{scenario_id}/reference-study/confirmed"),
                    confirmation_assurance="host_verified",
                    host_approval_receipt=(f"validation://{scenario_id}/reference-study/receipt"),
                ),
                ensure_ascii=False,
            )
        )
        approved_report = application.repository.get_artifact(report_artifact.artifact_id)
        return {
            "fixture_slug": fixture.slug,
            "source_media_verified": source_verified,
            "analysis_artifact": analysis_artifact.model_dump(mode="json"),
            "report_artifact": approved_report.model_dump(mode="json"),
            "freeze": freeze.model_dump(mode="json"),
        }

    @staticmethod
    def _load_reference_context(
        application: PluginApplication,
        envelope: StageEnvelope,
        *,
        enabled: bool,
    ) -> ReferenceStageContext | None:
        if not enabled:
            return None
        response = application.reference_creation_context(envelope.stage_access_handle)
        return ReferenceStageContext(
            binding=ReferenceContextBinding.model_validate(response["binding"]),
            projection=StageKnowledgeProjection.model_validate(response["projection"]),
        )

    @staticmethod
    def _attach_reference_context(
        content: ModelT,
        reference_context: ReferenceStageContext | None,
    ) -> ModelT:
        if reference_context is None:
            return content
        return content.model_copy(update={"reference_context": reference_context.binding})

    def _run_confirmed_stage(
        self,
        *,
        application: PluginApplication,
        task_id: str,
        expected_stage: str,
        creation_stage: CreationStage,
        query: KnowledgeQueryDefinition,
        output_path: Path,
        artifact_type: str,
        build: Callable[
            [str, SearchResult, StageKnowledgeProjection | None],
            str,
        ],
        scenario_id: str,
        reference_guided: bool,
    ) -> StageEvidence:
        envelope = self._stage_envelope(application, task_id)
        if envelope.stage != expected_stage:
            raise ScenarioExecutionError(
                f"当前阶段不是 {expected_stage}", evidence_path=output_path.parent
            )
        reference_context = self._load_reference_context(
            application,
            envelope,
            enabled=reference_guided,
        )
        audit, result = self._search_stage(
            application,
            envelope,
            query,
            creation_stage,
        )
        content = build(
            audit.retrieval_id,
            result,
            (reference_context.projection if reference_context is not None else None),
        )
        artifact, freeze = self._submit_and_confirm(
            application=application,
            envelope=envelope,
            output_path=output_path,
            content=content,
            artifact_type=artifact_type,
            scenario_id=scenario_id,
            evidence_refs=[f"retrieval://{audit.retrieval_id}"],
        )
        return StageEvidence(artifact, freeze, audit, result)

    @staticmethod
    def _search_stage(
        application: PluginApplication,
        envelope: StageEnvelope,
        query: KnowledgeQueryDefinition,
        creation_stage: CreationStage,
    ) -> tuple[RetrievalAudit, SearchResult]:
        response = application.knowledge_search(
            envelope.stage_access_handle,
            {
                "text": query.text,
                "knowledge_types": query.knowledge_types,
                "limit": query.limit,
            },
        )
        audit = RetrievalAudit.model_validate_json(
            json.dumps(response["retrieval"], ensure_ascii=False)
        )
        result = SearchResult.model_validate(response["result"])
        if audit.stage != creation_stage or not result.shared_creation_knowledge:
            raise ScenarioExecutionError(
                "当前阶段没有检索到可用共享知识",
                evidence_path=application.output_root / "retrieval.sqlite3",
            )
        return audit, result

    @staticmethod
    def _submit_and_confirm(
        *,
        application: PluginApplication,
        envelope: StageEnvelope,
        output_path: Path,
        content: BaseModel | str,
        artifact_type: str,
        scenario_id: str,
        evidence_refs: list[str],
    ) -> tuple[ArtifactEnvelope, FreezeRecord]:
        if isinstance(content, str):
            output_path.write_text(content, encoding="utf-8")
        else:
            write_json(output_path, content.model_dump(mode="json"))
        artifact = ArtifactEnvelope.model_validate_json(
            json.dumps(
                application.submit_artifact(
                    access_handle=envelope.stage_access_handle,
                    artifact_type=artifact_type,
                    content=output_path.read_text(encoding="utf-8"),
                    schema_version="1.0",
                    producer_kind="component",
                    producer_id="creation-e2e-runner",
                    primary=True,
                    parent_artifact_refs=[
                        item.model_dump(mode="json") for item in envelope.input_artifacts
                    ],
                    evidence_refs=evidence_refs,
                    rule_version=None,
                    skill_versions=None,
                    model_id=None,
                    component_version="creation-e2e-v2",
                ),
                ensure_ascii=False,
            )
        )
        approval = StageEnvelope.model_validate_json(
            json.dumps(application.get_stage_envelope(envelope.task_id), ensure_ascii=False)
        )
        freeze = FreezeRecord.model_validate_json(
            json.dumps(
                application.record_approval(
                    access_handle=approval.stage_access_handle,
                    user_confirmation_ref=f"validation://{scenario_id}/{envelope.stage}/confirmed",
                    confirmation_assurance="host_verified",
                    host_approval_receipt=f"validation://{scenario_id}/{envelope.stage}/receipt",
                ),
                ensure_ascii=False,
            )
        )
        approved_artifact = artifact.model_copy(update={"status": "approved"})
        return approved_artifact, freeze

    @staticmethod
    def _stage_envelope(
        application: PluginApplication,
        task_id: str,
    ) -> StageEnvelope:
        return StageEnvelope.model_validate_json(
            json.dumps(application.get_stage_envelope(task_id), ensure_ascii=False)
        )

    @staticmethod
    def _wait_for_render(
        application: PluginApplication,
        access_handle: str,
        render_job_id: str,
        evidence_path: Path,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 600
        while True:
            job = application.editor_get_render(access_handle, render_job_id)
            write_json(evidence_path, job)
            status = str(job["status"])
            if status == "succeeded":
                return job
            if status in {"failed", "cancelled"}:
                raise ScenarioExecutionError(
                    f"渲染任务结束于 {status}",
                    evidence_path=evidence_path,
                )
            if time.monotonic() >= deadline:
                raise ScenarioExecutionError("渲染任务等待超时", evidence_path=evidence_path)
            time.sleep(0.2)

    @staticmethod
    def _snapshot_file(source: Path, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination.resolve()

    @staticmethod
    def _snapshot_tree(source: Path, destination: Path) -> None:
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)

    @staticmethod
    def _execution_report(
        scenario: ScenarioDefinition,
        specification: EditingSpecification,
        visuals: list[ResolvedVisualMaterial],
        bgm: ResolvedBgm,
        trace_entries: list[SpecTraceEntry],
        inspection: dict[str, Any],
        stages: list[StageEvidence],
    ) -> dict[str, Any]:
        boundaries = [shot.timeline.end_us for shot in specification.shots[:-1]]
        beat_grid = specification.beat_grid_us
        beat_errors = {
            str(boundary): min(abs(boundary - int(beat)) for beat in beat_grid)
            for boundary in boundaries
        }
        return {
            "schema_version": "1.0",
            "scenario_id": scenario.scenario_id,
            "profile": scenario.profile,
            "user_intent": scenario.user_intent,
            "passed": bool(inspection["passed"]),
            "retrievals": [stage.retrieval.model_dump(mode="json") for stage in stages],
            "asset_audit": {
                "visual_asset_count": len(visuals),
                "distinct_visual_sha256_count": len({item.sha256 for item in visuals}),
                "source_video_sha256_count": len({item.source_sha256 for item in visuals}),
                "visual_sha256s": sorted(item.sha256 for item in visuals),
                "forbidden_reference_sha256s": scenario.forbidden_source_sha256s,
                "bgm_sha256": bgm.sha256,
                "bgm_analysis_manifest_sha256": bgm.analysis.manifest_sha256,
            },
            "planning_audit": {
                "shot_count": len(specification.shots),
                "hard_cut_boundaries_us": boundaries,
                "beat_error_us_by_boundary": beat_errors,
                "maximum_beat_error_us": max(beat_errors.values(), default=0),
                "pip_event_count": len(scenario.pip_events),
                "action_count": len(specification.actions),
                "traced_action_count": len(trace_entries),
                "all_actions_traced": len(specification.actions) == len(trace_entries),
            },
            "render_inspection": inspection,
        }

    @staticmethod
    def _write_run_manifest(
        *,
        run_root: Path,
        scenario: ScenarioDefinition,
        task_id: str,
        stages: list[StageEvidence],
        execution_artifact: ArtifactEnvelope | None,
        inspection_passed: bool,
        application_outputs: dict[str, str],
        task_type: TaskType,
        reference_study: dict[str, Any] | None,
    ) -> tuple[Path, str]:
        manifest_path = run_root / "run_manifest.json"
        digest_path = run_root / "run_manifest.sha256"
        files = []
        for path in sorted(item for item in run_root.rglob("*") if item.is_file()):
            if path in {manifest_path, digest_path} or path.name.endswith(("-shm", "-wal")):
                continue
            try:
                digest = sha256_file(path)
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            files.append(
                {
                    "path": path.relative_to(run_root).as_posix(),
                    "sha256": digest,
                    "size_bytes": size,
                }
            )
        manifest = {
            "schema_version": "1.0",
            "scenario_id": scenario.scenario_id,
            "scenario_definition_sha256": hashlib.sha256(
                canonical_json_bytes(scenario.model_dump(mode="json"))
            ).hexdigest(),
            "task_id": task_id,
            "task_type": task_type,
            "reference_study": reference_study,
            "inspection_passed": inspection_passed,
            "stage_artifacts": [stage.artifact.model_dump(mode="json") for stage in stages],
            "stage_freezes": [stage.freeze.model_dump(mode="json") for stage in stages],
            "retrieval_ids": [stage.retrieval.retrieval_id for stage in stages],
            "execution_artifact": (
                execution_artifact.model_dump(mode="json")
                if execution_artifact is not None
                else None
            ),
            "application_outputs": application_outputs,
            "created_at": datetime.now(UTC).isoformat(),
            "files": files,
        }
        write_json(manifest_path, manifest)
        digest = sha256_file(manifest_path)
        write_bytes(digest_path, f"{digest}  run_manifest.json\n".encode("ascii"))
        return manifest_path, digest


def default_run_directory(
    plugin_output_root: Path,
    scenario_id: str,
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return plugin_output_root / "validation" / "creation" / scenario_id / timestamp


def dump_result(result: ScenarioRunResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
