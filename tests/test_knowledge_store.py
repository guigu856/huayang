from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from video_create_plugin.knowledge import (
    EMBEDDING_DIMENSION,
    EMBEDDING_VERSION,
    ChineseCharNgramEmbedding,
    KnowledgeRecord,
    KnowledgeStore,
    PublicationRequest,
    Query,
)
from video_create_plugin.models import ArtifactRef


def _report_ref(
    *,
    revision: int = 1,
    fill: str = "a",
    artifact_id: str = "artifact_0123456789abcdef",
) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id,
        revision=revision,
        sha256=fill * 64,
    )


def _record(
    *,
    report_ref: ArtifactRef,
    collection: str = "creation_knowledge",
    task_id: str = "task_reference_a",
    stage: str = "stage1",
    knowledge_type: str = "video_type",
    content: str = "画中画随重拍进入并形成约一秒的剪辑句子",
    evidence_ref: str = "evidence://shot/1",
) -> KnowledgeRecord:
    evidence_only = collection == "reference_evidence"
    return KnowledgeRecord.model_validate(
        {
            "collection": collection,
            "source_task_id": task_id,
            "source_report_ref": report_ref,
            "source_artifact_refs": [report_ref],
            "analysis_version": "reference-analysis-v1",
            "applicable_stages": [stage],
            "knowledge_type": knowledge_type,
            "visibility": "evidence_only" if evidence_only else "creation_shared",
            "transferability": ("reference_specific" if evidence_only else "reusable_mechanism"),
            "content": content,
            "evidence_refs": [evidence_ref],
            "fact_status": "observed",
            "confidence": 0.9,
            "granularity": "rhythm_unit",
        }
    )


def _request(
    *,
    report_ref: ArtifactRef,
    records: list[KnowledgeRecord],
    task_id: str = "task_reference_a",
    publication_revision: int = 1,
) -> PublicationRequest:
    return PublicationRequest(
        source_task_id=task_id,
        source_report_ref=report_ref,
        source_media_sha256="f" * 64,
        publication_revision=publication_revision,
        freeze_id=f"freeze_{publication_revision}",
        records=records,
    )


def _store(tmp_path: Path) -> KnowledgeStore:
    return KnowledgeStore(
        tmp_path / "knowledge",
        now=lambda: datetime(2026, 7, 22, tzinfo=UTC),
    )


def test_chinese_char_ngram_embedding_is_versioned_and_deterministic() -> None:
    embedding = ChineseCharNgramEmbedding()

    first = embedding.embed("画中画 节奏剪辑")
    second = embedding.embed("画中画　节奏剪辑")

    assert first == second
    assert len(first) == EMBEDDING_DIMENSION
    assert embedding.version == EMBEDDING_VERSION
    assert sum(value * value for value in first) == pytest.approx(1.0)


def test_search_hard_filters_stage_type_source_and_collection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    report_ref = _report_ref()
    records = [
        _record(report_ref=report_ref),
        _record(
            report_ref=report_ref,
            stage="stage2",
            knowledge_type="bgm_mood",
            content="高能电子音乐在推进段逐步抬升",
            evidence_ref="evidence://audio/1",
        ),
        _record(
            report_ref=report_ref,
            stage="stage1",
            knowledge_type="viewing_experience",
            content="快切与停顿交替产生呼吸感",
            evidence_ref="evidence://shot/2",
        ),
        _record(
            report_ref=report_ref,
            collection="reference_evidence",
            content="原片第一个画中画位于右上区域",
            evidence_ref="evidence://frame/25",
        ),
    ]
    store.publish(_request(report_ref=report_ref, records=records))

    query = Query(
        text="重拍画中画快切",
        stage="stage1",
        knowledge_types=["video_type"],
        source_task_id="task_reference_a",
        current_task_id="task_reference_a",
    )
    result = store.search(query)

    assert [hit.content for hit in result.shared_creation_knowledge] == [records[0].content]
    assert [hit.content for hit in result.current_task_reference_evidence] == [records[3].content]
    assert result.shared_creation_knowledge[0].collection == "creation_knowledge"
    assert result.current_task_reference_evidence[0].collection == "reference_evidence"
    assert result.current_task_reference_evidence[0].match_reasons[0] == (
        "priority=current_task_reference_evidence"
    )
    assert result.shared_creation_knowledge[0].source_report_ref == report_ref
    assert result.shared_creation_knowledge[0].embedding_version == EMBEDDING_VERSION

    wrong_stage = store.search(
        query.model_copy(update={"stage": "stage3"})
    ).shared_creation_knowledge
    wrong_source = store.search(
        query.model_copy(update={"source_task_id": "task_other"})
    ).shared_creation_knowledge
    assert wrong_stage == []
    assert wrong_source == []


def test_new_revision_supersedes_old_and_publish_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_ref = _report_ref(revision=1, fill="a")
    first_request = _request(
        report_ref=first_ref,
        records=[
            _record(
                report_ref=first_ref,
                content="旧版本认为所有镜头严格跟随基础拍点",
            ),
        ],
        publication_revision=1,
    )
    first = store.publish(first_request)
    assert store.publish(first_request) == first

    second_ref = _report_ref(
        revision=1,
        fill="b",
        artifact_id="artifact_fedcba9876543210",
    )
    second_request = _request(
        report_ref=second_ref,
        records=[
            _record(
                report_ref=second_ref,
                content="新版本区分精确卡点、提前蓄力和拍后释放",
            ),
        ],
        publication_revision=2,
    )
    second = store.publish(second_request)

    assert second.supersedes_publication_id == first.publication_id
    assert store.get_publication(first.publication_id).status == "superseded"
    assert store.get_publication(second.publication_id).status == "active"
    assert store.publish(second_request) == second

    hits = store.search_shared(
        Query(
            text="卡点蓄力释放",
            stage="stage1",
            knowledge_types=["video_type"],
        )
    )
    assert [hit.content for hit in hits] == [second_request.records[0].content]


def test_current_task_evidence_is_separate_and_does_not_leak_between_tasks(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    report_ref = _report_ref()
    store.publish(
        _request(
            report_ref=report_ref,
            records=[
                _record(report_ref=report_ref),
                _record(
                    report_ref=report_ref,
                    collection="reference_evidence",
                    content="本次参考片具体画面证据",
                ),
            ],
        )
    )

    same_task = store.search(
        Query(
            text="画中画具体证据",
            stage="stage1",
            knowledge_types=["video_type"],
            current_task_id="task_reference_a",
        )
    )
    other_task = store.search(
        Query(
            text="画中画具体证据",
            stage="stage1",
            knowledge_types=["video_type"],
            current_task_id="task_other",
        )
    )

    assert len(same_task.current_task_reference_evidence) == 1
    assert len(same_task.shared_creation_knowledge) == 1
    assert other_task.current_task_reference_evidence == []
    assert len(other_task.shared_creation_knowledge) == 1


def test_equal_scores_use_stable_knowledge_id_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    report_ref = _report_ref()
    content = "镜头组使用完全相同的画中画节奏机制"
    records = [
        _record(
            report_ref=report_ref,
            content=content,
            evidence_ref="evidence://shot/b",
        ),
        _record(
            report_ref=report_ref,
            content=content,
            evidence_ref="evidence://shot/a",
        ),
    ]
    publication = store.publish(_request(report_ref=report_ref, records=records))
    reordered = store.publish(_request(report_ref=report_ref, records=list(reversed(records))))
    query = Query(
        text=content,
        stage="stage1",
        knowledge_types=["video_type"],
        limit=2,
    )

    first = store.search_shared(query)
    second = store.search_shared(query)

    assert [hit.knowledge_id for hit in first] == sorted(hit.knowledge_id for hit in first)
    assert [hit.knowledge_id for hit in first] == [hit.knowledge_id for hit in second]
    assert first[0].score == first[1].score
    assert reordered == publication


def test_collection_policies_are_enforced_before_publish() -> None:
    report_ref = _report_ref()
    payload = _record(report_ref=report_ref).model_dump()
    payload["visibility"] = "evidence_only"

    with pytest.raises(ValidationError):
        KnowledgeRecord.model_validate(payload)
