from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import lancedb  # type: ignore[import-untyped]
import pyarrow as pa  # type: ignore[import-untyped]

from video_create_plugin.errors import PluginError
from video_create_plugin.models import ArtifactRef

from .embedding import ChineseCharNgramEmbedding
from .models import (
    EMBEDDING_DIMENSION,
    EMBEDDING_VERSION,
    CreationStage,
    Hit,
    KnowledgeCollection,
    KnowledgeRecord,
    Publication,
    PublicationRequest,
    Query,
    SearchResult,
)

_COLLECTIONS: tuple[KnowledgeCollection, ...] = (
    "creation_knowledge",
    "reference_evidence",
)


class KnowledgeStore:
    """以 SQLite 记录发布版本，以 LanceDB 提供隔离的本地向量检索。"""

    def __init__(
        self,
        root: Path | str,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.sqlite_path = self.root / "publications.sqlite3"
        self.lance_path = self.root / "knowledge.lancedb"
        self.embedding = ChineseCharNgramEmbedding()
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._lance = lancedb.connect(str(self.lance_path))
        self._initialize_sqlite()
        self._initialize_lance_tables()

    def publish(self, request: PublicationRequest) -> Publication:
        content_sha256 = self._publication_digest(request)
        publication_id = f"publication_{content_sha256[:16]}"
        created_at = self._now()
        if created_at.tzinfo is None:
            raise PluginError("knowledge_clock_invalid", "知识库时钟必须携带时区")

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM publications WHERE content_sha256 = ?",
                (content_sha256,),
            ).fetchone()
            if existing is not None:
                return self._publication_from_row(existing)

            revision_conflict = connection.execute(
                """
                SELECT publication_id FROM publications
                WHERE source_media_sha256 = ? AND publication_revision = ?
                """,
                (
                    request.source_media_sha256,
                    request.publication_revision,
                ),
            ).fetchone()
            if revision_conflict is not None:
                raise PluginError(
                    "knowledge_publication_revision_conflict",
                    "同一报告 revision 已发布不同内容",
                )

            previous_row = connection.execute(
                """
                SELECT * FROM publications
                WHERE source_media_sha256 = ? AND status = 'active'
                ORDER BY publication_revision DESC LIMIT 1
                """,
                (request.source_media_sha256,),
            ).fetchone()
            if previous_row is not None and request.publication_revision <= int(
                previous_row["publication_revision"]
            ):
                raise PluginError(
                    "knowledge_publication_revision_invalid",
                    "新发布 revision 必须递增",
                )

            previous_id = None if previous_row is None else str(previous_row["publication_id"])
            records = self._prepare_records(request, publication_id)
            publication = Publication(
                publication_id=publication_id,
                source_task_id=request.source_task_id,
                source_report_ref=request.source_report_ref,
                source_media_sha256=request.source_media_sha256,
                publication_revision=request.publication_revision,
                status="active",
                supersedes_publication_id=previous_id,
                freeze_id=request.freeze_id,
                content_sha256=content_sha256,
                collection_counts={
                    collection: sum(record.collection == collection for record in records)
                    for collection in _COLLECTIONS
                },
                created_at=created_at,
            )

            changed_previous = False
            added_new = False
            try:
                self._delete_lance_publication(publication_id)
                if previous_id is not None:
                    self._set_lance_publication_status(previous_id, "superseded")
                    changed_previous = True
                self._add_records(records)
                added_new = True
                if previous_id is not None:
                    connection.execute(
                        "UPDATE publications SET status = 'superseded' WHERE publication_id = ?",
                        (previous_id,),
                    )
                self._insert_publication(connection, publication)
                connection.commit()
            except Exception as error:
                connection.rollback()
                if added_new:
                    self._delete_lance_publication(publication_id)
                if changed_previous and previous_id is not None:
                    self._set_lance_publication_status(previous_id, "active")
                if isinstance(error, PluginError):
                    raise
                raise PluginError(
                    "knowledge_publication_failed",
                    "知识发布写入失败",
                ) from error
            return publication

    def get_publication(self, publication_id: str) -> Publication:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
        if row is None:
            raise PluginError("knowledge_publication_not_found", "知识发布不存在")
        return self._publication_from_row(row)

    def get_unique_active_publication(self, source_media_sha256: str) -> Publication:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM publications
                WHERE source_media_sha256 = ? AND status = 'active'
                ORDER BY publication_revision DESC, publication_id
                """,
                (source_media_sha256,),
            ).fetchall()
        if len(rows) != 1:
            raise PluginError(
                "knowledge_active_publication_invalid",
                "来源媒体必须且只能存在一个 active 知识发布版本",
                details={"active_count": len(rows)},
            )
        return self._publication_from_row(rows[0])

    def search(self, query: Query) -> SearchResult:
        direct_hits: list[Hit] = []
        if query.current_task_id is not None:
            direct_hits = self._search_reference_evidence(query)
        return SearchResult(
            current_task_reference_evidence=direct_hits,
            shared_creation_knowledge=self._search_creation_knowledge(query),
        )

    def search_shared(self, query: Query) -> list[Hit]:
        return self._search_creation_knowledge(query)

    def search_current_task_evidence(self, query: Query) -> list[Hit]:
        if query.current_task_id is None:
            return []
        return self._search_reference_evidence(query)

    def _search_creation_knowledge(self, query: Query) -> list[Hit]:
        filters = [
            "active = true",
            "publication_status = 'active'",
            f"{query.stage} = true",
            self._knowledge_type_filter(query.knowledge_types),
            "visibility = 'creation_shared'",
            "transferability = 'reusable_mechanism'",
        ]
        if query.source_task_id is not None:
            filters.append(f"source_task_id = {self._sql_literal(query.source_task_id)}")
        return self._vector_search(
            "creation_knowledge",
            query,
            " AND ".join(filters),
            direct_evidence=False,
        )

    def _search_reference_evidence(self, query: Query) -> list[Hit]:
        if query.current_task_id is None:
            return []
        filters = [
            "active = true",
            "publication_status = 'active'",
            f"{query.stage} = true",
            self._knowledge_type_filter(query.knowledge_types),
            "visibility = 'evidence_only'",
            "(transferability = 'reference_specific' OR transferability = 'uncertain')",
            f"source_task_id = {self._sql_literal(query.current_task_id)}",
        ]
        return self._vector_search(
            "reference_evidence",
            query,
            " AND ".join(filters),
            direct_evidence=True,
        )

    def _vector_search(
        self,
        collection: KnowledgeCollection,
        query: Query,
        where: str,
        *,
        direct_evidence: bool,
    ) -> list[Hit]:
        table = self._lance.open_table(collection)
        eligible_count = int(table.count_rows(where))
        if eligible_count == 0:
            return []
        try:
            query_vector = self.embedding.embed(query.text)
        except ValueError as error:
            raise PluginError("knowledge_query_invalid", "查询文本缺少可索引字符") from error
        rows = cast(
            list[dict[str, Any]],
            table.search(query_vector)
            .distance_type("cosine")
            .where(where, prefilter=True)
            .limit(eligible_count)
            .to_list(),
        )
        hits = [
            self._hit_from_row(
                row,
                query_vector,
                direct_evidence=direct_evidence,
            )
            for row in rows
        ]
        hits.sort(key=lambda hit: (-hit.score, hit.knowledge_id))
        return hits[: query.limit]

    def _hit_from_row(
        self,
        row: Mapping[str, Any],
        query_vector: Sequence[float],
        *,
        direct_evidence: bool,
    ) -> Hit:
        stored_vector = cast(Sequence[float], row["vector"])
        score = round(
            sum(left * float(right) for left, right in zip(query_vector, stored_vector)),
            12,
        )
        reasons = [
            f"stage={self._first_enabled_stage(row)}",
            f"knowledge_type={row['knowledge_type']}",
            f"visibility={row['visibility']}",
            f"transferability={row['transferability']}",
            f"embedding_similarity={score:.6f}",
        ]
        if direct_evidence:
            reasons.insert(0, "priority=current_task_reference_evidence")
        return Hit(
            knowledge_id=str(row["knowledge_id"]),
            publication_id=str(row["publication_id"]),
            collection=cast(KnowledgeCollection, row["collection"]),
            content=str(row["content"]),
            score=max(-1.0, min(1.0, score)),
            match_reasons=reasons,
            source_task_id=str(row["source_task_id"]),
            source_report_ref=ArtifactRef.model_validate_json(str(row["source_report_ref_json"])),
            source_artifact_refs=[
                ArtifactRef.model_validate(item)
                for item in json.loads(str(row["source_artifact_refs_json"]))
            ],
            evidence_refs=list(json.loads(str(row["evidence_refs_json"]))),
            applicable_stages=cast(
                list[CreationStage],
                list(json.loads(str(row["applicable_stages_json"]))),
            ),
            knowledge_type=str(row["knowledge_type"]),
            visibility=cast(Any, row["visibility"]),
            transferability=cast(Any, row["transferability"]),
            fact_status=str(row["fact_status"]),
            confidence=float(row["confidence"]),
            embedding_version=str(row["embedding_version"]),
        )

    def _prepare_records(
        self,
        request: PublicationRequest,
        publication_id: str,
    ) -> list[KnowledgeRecord]:
        records: list[KnowledgeRecord] = []
        for record in request.records:
            canonical = record.model_dump(
                mode="json",
                exclude={"knowledge_id", "publication_id"},
            )
            digest = hashlib.sha256(
                json.dumps(
                    canonical,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            records.append(
                record.model_copy(
                    update={
                        "knowledge_id": f"knowledge_{digest[:16]}",
                        "publication_id": publication_id,
                    }
                )
            )
        return records

    def _add_records(self, records: Sequence[KnowledgeRecord]) -> None:
        for collection in _COLLECTIONS:
            rows = [
                self._record_to_lance_row(record)
                for record in records
                if record.collection == collection
            ]
            if rows:
                self._lance.open_table(collection).add(rows)

    def _record_to_lance_row(self, record: KnowledgeRecord) -> dict[str, object]:
        assert record.knowledge_id is not None
        assert record.publication_id is not None
        try:
            vector = self.embedding.embed(record.content)
        except ValueError as error:
            raise PluginError(
                "knowledge_publication_rejected",
                "知识正文缺少可索引字符",
            ) from error
        stages = set(record.applicable_stages)
        return {
            "knowledge_id": record.knowledge_id,
            "publication_id": record.publication_id,
            "publication_status": "active",
            "active": True,
            "collection": record.collection,
            "source_task_id": record.source_task_id,
            "source_report_ref_json": record.source_report_ref.model_dump_json(),
            "source_artifact_refs_json": json.dumps(
                [item.model_dump(mode="json") for item in record.source_artifact_refs],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "analysis_version": record.analysis_version,
            "applicable_stages_json": json.dumps(record.applicable_stages),
            "stage1": "stage1" in stages,
            "stage2": "stage2" in stages,
            "stage3": "stage3" in stages,
            "knowledge_type": record.knowledge_type,
            "visibility": record.visibility,
            "transferability": record.transferability,
            "content": record.content,
            "evidence_refs_json": json.dumps(
                record.evidence_refs,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "fact_status": record.fact_status,
            "confidence": record.confidence,
            "source_time_range_json": json.dumps(record.source_time_range_us),
            "video_type_tags_json": json.dumps(record.video_type_tags, ensure_ascii=False),
            "technique_tags_json": json.dumps(record.technique_tags, ensure_ascii=False),
            "music_layer_tags_json": json.dumps(record.music_layer_tags, ensure_ascii=False),
            "energy_phase": record.energy_phase or "",
            "granularity": record.granularity,
            "chunker_version": record.chunker_version,
            "embedding_version": record.embedding_version,
            "embedding_dimension": record.embedding_dimension,
            "vector": vector,
        }

    def _initialize_sqlite(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS index_metadata (
                    collection TEXT PRIMARY KEY,
                    embedding_version TEXT NOT NULL,
                    embedding_dimension INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publications (
                    publication_id TEXT PRIMARY KEY,
                    source_task_id TEXT NOT NULL,
                    source_artifact_id TEXT NOT NULL,
                    source_artifact_revision INTEGER NOT NULL,
                    source_artifact_sha256 TEXT NOT NULL,
                    source_media_sha256 TEXT NOT NULL,
                    publication_revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    supersedes_publication_id TEXT,
                    freeze_id TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL UNIQUE,
                    collection_counts_json TEXT NOT NULL,
                    embedding_version TEXT NOT NULL,
                    embedding_dimension INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_media_sha256, publication_revision)
                );
                """
            )
            for collection in _COLLECTIONS:
                row = connection.execute(
                    "SELECT * FROM index_metadata WHERE collection = ?",
                    (collection,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO index_metadata VALUES (?, ?, ?)",
                        (collection, EMBEDDING_VERSION, EMBEDDING_DIMENSION),
                    )
                elif (
                    row["embedding_version"] != EMBEDDING_VERSION
                    or int(row["embedding_dimension"]) != EMBEDDING_DIMENSION
                ):
                    raise PluginError(
                        "knowledge_index_version_mismatch",
                        "知识索引向量版本不匹配",
                    )

    def _initialize_lance_tables(self) -> None:
        schema = pa.schema(
            [
                pa.field("knowledge_id", pa.string(), nullable=False),
                pa.field("publication_id", pa.string(), nullable=False),
                pa.field("publication_status", pa.string(), nullable=False),
                pa.field("active", pa.bool_(), nullable=False),
                pa.field("collection", pa.string(), nullable=False),
                pa.field("source_task_id", pa.string(), nullable=False),
                pa.field("source_report_ref_json", pa.string(), nullable=False),
                pa.field("source_artifact_refs_json", pa.string(), nullable=False),
                pa.field("analysis_version", pa.string(), nullable=False),
                pa.field("applicable_stages_json", pa.string(), nullable=False),
                pa.field("stage1", pa.bool_(), nullable=False),
                pa.field("stage2", pa.bool_(), nullable=False),
                pa.field("stage3", pa.bool_(), nullable=False),
                pa.field("knowledge_type", pa.string(), nullable=False),
                pa.field("visibility", pa.string(), nullable=False),
                pa.field("transferability", pa.string(), nullable=False),
                pa.field("content", pa.string(), nullable=False),
                pa.field("evidence_refs_json", pa.string(), nullable=False),
                pa.field("fact_status", pa.string(), nullable=False),
                pa.field("confidence", pa.float32(), nullable=False),
                pa.field("source_time_range_json", pa.string(), nullable=False),
                pa.field("video_type_tags_json", pa.string(), nullable=False),
                pa.field("technique_tags_json", pa.string(), nullable=False),
                pa.field("music_layer_tags_json", pa.string(), nullable=False),
                pa.field("energy_phase", pa.string(), nullable=False),
                pa.field("granularity", pa.string(), nullable=False),
                pa.field("chunker_version", pa.string(), nullable=False),
                pa.field("embedding_version", pa.string(), nullable=False),
                pa.field("embedding_dimension", pa.int32(), nullable=False),
                pa.field(
                    "vector",
                    pa.list_(pa.float32(), EMBEDDING_DIMENSION),
                    nullable=False,
                ),
            ]
        )
        for collection in _COLLECTIONS:
            self._lance.create_table(
                collection,
                schema=schema,
                exist_ok=True,
            )

    def _insert_publication(
        self,
        connection: sqlite3.Connection,
        publication: Publication,
    ) -> None:
        connection.execute(
            """
            INSERT INTO publications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                publication.publication_id,
                publication.source_task_id,
                publication.source_report_ref.artifact_id,
                publication.source_report_ref.revision,
                publication.source_report_ref.sha256,
                publication.source_media_sha256,
                publication.publication_revision,
                publication.status,
                publication.supersedes_publication_id,
                publication.freeze_id,
                publication.content_sha256,
                json.dumps(publication.collection_counts, sort_keys=True),
                publication.embedding_version,
                publication.embedding_dimension,
                publication.created_at.isoformat(),
            ),
        )

    @staticmethod
    def _publication_from_row(row: sqlite3.Row) -> Publication:
        return Publication(
            publication_id=str(row["publication_id"]),
            source_task_id=str(row["source_task_id"]),
            source_report_ref=ArtifactRef(
                artifact_id=str(row["source_artifact_id"]),
                revision=int(row["source_artifact_revision"]),
                sha256=str(row["source_artifact_sha256"]),
            ),
            source_media_sha256=str(row["source_media_sha256"]),
            publication_revision=int(row["publication_revision"]),
            status=cast(Any, row["status"]),
            supersedes_publication_id=(
                None
                if row["supersedes_publication_id"] is None
                else str(row["supersedes_publication_id"])
            ),
            freeze_id=str(row["freeze_id"]),
            content_sha256=str(row["content_sha256"]),
            collection_counts=json.loads(str(row["collection_counts_json"])),
            embedding_version=str(row["embedding_version"]),
            embedding_dimension=int(row["embedding_dimension"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def _set_lance_publication_status(
        self,
        publication_id: str,
        status: str,
    ) -> None:
        active = status == "active"
        where = f"publication_id = {self._sql_literal(publication_id)}"
        for collection in _COLLECTIONS:
            self._lance.open_table(collection).update(
                where=where,
                values={"publication_status": status, "active": active},
            )

    def _delete_lance_publication(self, publication_id: str) -> None:
        where = f"publication_id = {self._sql_literal(publication_id)}"
        for collection in _COLLECTIONS:
            self._lance.open_table(collection).delete(where)

    @staticmethod
    def _publication_digest(request: PublicationRequest) -> str:
        canonical = request.model_dump(mode="json", exclude={"records"})
        canonical_records = [
            record.model_dump(
                mode="json",
                exclude={"knowledge_id", "publication_id"},
            )
            for record in request.records
        ]
        canonical["records"] = sorted(
            canonical_records,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        canonical["embedding_version"] = EMBEDDING_VERSION
        canonical["embedding_dimension"] = EMBEDDING_DIMENSION
        return hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _knowledge_type_filter(values: Sequence[str]) -> str:
        clauses = [f"knowledge_type = {KnowledgeStore._sql_literal(value)}" for value in values]
        return f"({' OR '.join(clauses)})"

    @staticmethod
    def _sql_literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _first_enabled_stage(row: Mapping[str, Any]) -> str:
        return next(stage for stage in ("stage1", "stage2", "stage3") if row[stage])

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.sqlite_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()
