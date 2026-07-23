from __future__ import annotations

import hashlib
import json
from collections import Counter

from pydantic import Field, ValidationError

from .analysis import AnalysisEvidenceManifest
from .errors import PluginError
from .knowledge import KnowledgeStore, Publication, PublicationRequest
from .knowledge.models import CreationStage as KnowledgeCreationStage
from .models import ArtifactRef, PluginModel
from .reporting import ReferenceReportManifest
from .reporting.models import CreationStage as ReportCreationStage
from .workflow import WorkflowService

_REPORT_STAGE_BY_KNOWLEDGE_STAGE: dict[KnowledgeCreationStage, ReportCreationStage] = {
    "stage1": "creative_direction",
    "stage2": "resource_preparation",
    "stage3": "editing_specification",
}


class PublicationPreview(PluginModel):
    source_report_ref: ArtifactRef
    freeze_id: str
    record_count: int = Field(gt=0)
    collection_counts: dict[str, int]
    stage_counts: dict[str, int]
    knowledge_type_counts: dict[str, int]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class KnowledgePublicationService:
    """在工作流冻结门禁之后预览并发布知识单元。"""

    def __init__(self, workflow: WorkflowService, store: KnowledgeStore) -> None:
        self.workflow = workflow
        self.store = store

    def preview(
        self,
        *,
        access_handle: str,
        request: PublicationRequest,
    ) -> PublicationPreview:
        self._validate_request(
            access_handle=access_handle,
            required_tool="knowledge_preview_publication",
            request=request,
        )
        canonical = json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return PublicationPreview(
            source_report_ref=request.source_report_ref,
            freeze_id=request.freeze_id,
            record_count=len(request.records),
            collection_counts=dict(
                sorted(Counter(record.collection for record in request.records).items())
            ),
            stage_counts=dict(
                sorted(
                    Counter(
                        stage for record in request.records for stage in record.applicable_stages
                    ).items()
                )
            ),
            knowledge_type_counts=dict(
                sorted(Counter(record.knowledge_type for record in request.records).items())
            ),
            content_sha256=hashlib.sha256(canonical).hexdigest(),
        )

    def publish(
        self,
        *,
        access_handle: str,
        request: PublicationRequest,
    ) -> Publication:
        self._validate_request(
            access_handle=access_handle,
            required_tool="knowledge_publish",
            request=request,
        )
        return self.store.publish(request)

    def _validate_request(
        self,
        *,
        access_handle: str,
        required_tool: str,
        request: PublicationRequest,
    ) -> None:
        task, stage = self.workflow.authorize_stage_tool(access_handle, required_tool)
        if task.task_type != "reference_study" or stage.stage_type != "knowledge_publication":
            raise PluginError("knowledge_publication_rejected", "当前任务阶段不接受知识发布")
        if request.source_task_id != task.task_id:
            raise PluginError("knowledge_publication_rejected", "知识发布任务身份不匹配")
        if request.source_report_ref not in stage.input_artifact_refs:
            raise PluginError("artifact_not_approved", "来源报告不是当前阶段冻结输入")
        matching_freezes = [
            freeze
            for freeze in stage.input_freeze_refs
            if freeze.freeze_id == request.freeze_id
            and freeze.artifact_id == request.source_report_ref.artifact_id
            and freeze.artifact_revision == request.source_report_ref.revision
            and freeze.artifact_sha256 == request.source_report_ref.sha256
        ]
        if not matching_freezes:
            raise PluginError("artifact_not_approved", "来源报告缺少匹配的有效冻结记录")
        artifact = self.workflow.repository.get_artifact(request.source_report_ref.artifact_id)
        if artifact.status != "approved" or artifact.as_ref() != request.source_report_ref:
            raise PluginError("artifact_not_approved", "来源报告批准状态或哈希不一致")
        freeze = self.workflow.repository.get_freeze(request.freeze_id)
        if freeze.as_ref() != matching_freezes[0]:
            raise PluginError("artifact_not_approved", "来源报告冻结记录不一致")
        if artifact.artifact_type != "reference_report_manifest":
            raise PluginError(
                "knowledge_publication_rejected",
                "冻结产物不是参考视频报告清单",
            )

        report_bytes = self.workflow.artifact_store.read_bytes(artifact.content_uri)
        if hashlib.sha256(report_bytes).hexdigest() != artifact.content_sha256:
            raise PluginError("artifact_hash_mismatch", "来源报告内容哈希不一致")
        try:
            report = ReferenceReportManifest.model_validate_json(report_bytes)
        except ValidationError as error:
            raise PluginError(
                "knowledge_publication_rejected",
                "来源报告未通过结构与内容校验",
            ) from error

        if request.source_media_sha256 != report.source_sha256:
            raise PluginError(
                "knowledge_publication_rejected",
                "发布请求的来源媒体哈希与报告不一致",
            )

        report_evidence_refs = set(report.evidence_refs)
        evidence_bound = False
        for parent_ref in artifact.parent_artifact_refs:
            parent = self.workflow.repository.get_artifact(parent_ref.artifact_id)
            if (
                parent.as_ref() != parent_ref
                or parent.task_id != task.task_id
                or parent.artifact_type != "reference_analysis_manifest"
            ):
                continue
            self.workflow.artifact_store.verify(
                parent.content_uri,
                parent.content_sha256,
            )
            try:
                evidence_manifest = AnalysisEvidenceManifest.model_validate_json(
                    self.workflow.artifact_store.read_bytes(parent.content_uri)
                )
            except ValidationError as error:
                raise PluginError(
                    "knowledge_publication_rejected",
                    "报告父级分析证据清单结构无效",
                ) from error
            if (
                evidence_manifest.job_id == report.analysis_id
                and evidence_manifest.source.sha256 == report.source_sha256
                and report_evidence_refs.issubset(evidence_manifest.evidence_refs)
            ):
                evidence_bound = True
                break
        if not evidence_bound:
            raise PluginError(
                "knowledge_publication_rejected",
                "冻结报告缺少覆盖全部报告证据的哈希清单父级",
            )

        projections = {
            projection.stage: projection
            for projection in report.content.creation_context_projection.stage_projections
        }
        for record in request.records:
            unknown_evidence_refs = sorted(set(record.evidence_refs) - report_evidence_refs)
            if unknown_evidence_refs:
                raise PluginError(
                    "knowledge_publication_rejected",
                    "知识单元引用了报告之外的证据",
                    details={"evidence_refs": unknown_evidence_refs},
                )
            if record.collection != "creation_knowledge":
                continue
            for knowledge_stage in record.applicable_stages:
                projection = projections[_REPORT_STAGE_BY_KNOWLEDGE_STAGE[knowledge_stage]]
                recommendation_texts = {
                    recommendation.text for recommendation in projection.recommendations
                }
                if (
                    record.knowledge_type not in projection.knowledge_types
                    or record.content not in recommendation_texts
                ):
                    raise PluginError(
                        "knowledge_publication_rejected",
                        "共享创作知识不属于报告对应阶段的创作映射",
                        details={
                            "stage": knowledge_stage,
                            "knowledge_type": record.knowledge_type,
                        },
                    )
