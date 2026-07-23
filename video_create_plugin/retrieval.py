from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

from pydantic import Field

from .errors import PluginError
from .knowledge import KnowledgeStore, Query, SearchResult
from .models import PluginModel
from .workflow import WorkflowService

CreationStage = Literal["stage1", "stage2", "stage3"]

_WORKFLOW_TO_CREATION_STAGE: dict[str, CreationStage] = {
    "creative_direction": "stage1",
    "resource_preparation": "stage2",
    "editing_specification": "stage3",
}


class RetrievalAudit(PluginModel):
    retrieval_id: str = Field(pattern=r"^retrieval_[0-9a-f]{16}$")
    task_id: str = Field(pattern=r"^task_[0-9a-f]{16}$")
    stage_run_id: str = Field(pattern=r"^stage_[0-9a-f]{16}$")
    stage: CreationStage
    query: Query
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    current_task_hit_ids: list[str]
    shared_hit_ids: list[str]
    publication_ids: list[str]
    embedding_versions: list[str]
    created_at: datetime


class AuditedKnowledgeService:
    """执行阶段受限知识检索，并持久化可证明的调用记录。"""

    def __init__(
        self,
        store: KnowledgeStore,
        workflow: WorkflowService,
        database_path: Path | str,
    ) -> None:
        self.store = store
        self.workflow = workflow
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def search(
        self,
        *,
        access_handle: str,
        text: str,
        knowledge_types: list[str],
        limit: int = 5,
    ) -> tuple[RetrievalAudit, SearchResult]:
        task, stage_run = self.workflow.authorize_stage_tool(access_handle, "knowledge_search")
        creation_stage = _WORKFLOW_TO_CREATION_STAGE.get(stage_run.stage_type)
        if creation_stage is None:
            raise PluginError("stage_not_allowed", "当前阶段不属于创作知识检索阶段")
        query = Query(
            text=text,
            stage=creation_stage,
            knowledge_types=knowledge_types,
            limit=limit,
            current_task_id=task.task_id,
        )
        result = self.store.search(query)
        result_bytes = _canonical_json(result.model_dump(mode="json"))
        all_hits = [
            *result.current_task_reference_evidence,
            *result.shared_creation_knowledge,
        ]
        audit = RetrievalAudit(
            retrieval_id=f"retrieval_{uuid.uuid4().hex[:16]}",
            task_id=task.task_id,
            stage_run_id=stage_run.stage_run_id,
            stage=creation_stage,
            query=query,
            result_sha256=hashlib.sha256(result_bytes).hexdigest(),
            current_task_hit_ids=[
                hit.knowledge_id for hit in result.current_task_reference_evidence
            ],
            shared_hit_ids=[hit.knowledge_id for hit in result.shared_creation_knowledge],
            publication_ids=sorted({hit.publication_id for hit in all_hits}),
            embedding_versions=sorted({hit.embedding_version for hit in all_hits}),
            created_at=datetime.now(UTC),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO retrieval_audits (
                    retrieval_id, task_id, stage_run_id, stage, query_json,
                    result_json, audit_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit.retrieval_id,
                    audit.task_id,
                    audit.stage_run_id,
                    audit.stage,
                    query.model_dump_json(),
                    result.model_dump_json(),
                    audit.model_dump_json(),
                    audit.created_at.isoformat(),
                ),
            )
        return audit, result

    def get(self, retrieval_id: str) -> tuple[RetrievalAudit, SearchResult]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT audit_json, result_json FROM retrieval_audits WHERE retrieval_id = ?",
                (retrieval_id,),
            ).fetchone()
        if row is None:
            raise PluginError("artifact_not_found", "知识检索审计记录不存在")
        return (
            RetrievalAudit.model_validate_json(row["audit_json"]),
            SearchResult.model_validate_json(row["result_json"]),
        )

    def validate_stage_retrievals(
        self,
        *,
        task_id: str,
        stage_run_id: str,
        stage: CreationStage,
        retrieval_ids: list[str],
        require_shared_hit: bool = True,
    ) -> list[RetrievalAudit]:
        if not retrieval_ids:
            raise PluginError("knowledge_filter_required", "阶段产物缺少知识检索记录")
        audits = [self.get(retrieval_id)[0] for retrieval_id in retrieval_ids]
        if any(
            audit.task_id != task_id or audit.stage_run_id != stage_run_id or audit.stage != stage
            for audit in audits
        ):
            raise PluginError("knowledge_filter_required", "知识检索记录不属于当前任务阶段")
        if require_shared_hit and not any(audit.shared_hit_ids for audit in audits):
            logger.warning(
                "阶段检索未命中已发布共享知识，允许继续创作 (task=%s, stage=%s)",
                task_id,
                stage,
            )
        return audits

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS retrieval_audits (
                    retrieval_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    stage_run_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    query_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    audit_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_retrieval_task_stage "
                "ON retrieval_audits(task_id, stage)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
