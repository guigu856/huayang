from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

import pytest

from video_create_plugin.knowledge import (
    KnowledgeRecord,
    KnowledgeStore,
    PublicationRequest,
    Query,
)
from video_create_plugin.reporting import (
    ReferenceReportManifest,
    ReferenceReportValidator,
    collect_evidence_refs,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "validation/reference_studies"
LEARNING_ROOT = ROOT / "output/plugin/validation/reference_learning"
REPORT_ROOT = ROOT / "output/plugin/validation/reference_reports"

EXPECTED = {
    "01_fastcut_pip": {
        "sha256": "41e33f413403767c2a4217bf0ee0bad47a84769ae5e8868644549a073d2367ab",
        "duration_us": 30_233_333,
        "shot_count": 50,
        "candidate_count": 53,
    },
    "08_cutout_recompose": {
        "sha256": "0aa959b445fd2358374067494a905b13acda24c67b384eea0940a2c6515122ab",
        "duration_us": 19_200_000,
        "shot_count": 3,
        "candidate_count": 1,
    },
    "character_hype": {
        "sha256": "2fa096d1eaabf1b503b52cd091229c9c92b316d4b743b4e245587cb7fc39e4ed",
        "duration_us": 22_833_333,
        "shot_count": 46,
        "candidate_count": 87,
    },
    "composition_collage": {
        "sha256": "db1895c4c5dc7ec317a7ae00e3ea110ef4ecc77dd4b07584e3bfc7fde0aa00c9",
        "duration_us": 55_978_333,
        "shot_count": 38,
        "candidate_count": 117,
    },
}

SHARED_BANNED_TERMS = {
    "01_fastcut_pip": {"鸟类", "公共交通", "街头", "城市", "交通镜头"},
    "08_cutout_recompose": {"三段"},
    "character_hype": {"红紫"},
    "composition_collage": {"标牌", "设备界面", "故障标题"},
}


def _report(slug: str) -> ReferenceReportManifest:
    path = FIXTURE_ROOT / slug / "report_manifest.json"
    return ReferenceReportValidator().validate(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("slug", EXPECTED)
def test_reference_fixture_is_evidence_closed_and_covers_real_timeline(
    slug: str,
) -> None:
    expected = EXPECTED[slug]
    report = _report(slug)

    assert report.source_sha256 == expected["sha256"]
    assert report.content.video_overview.duration_us == expected["duration_us"]
    assert len(report.content.shot_analyses) == expected["shot_count"]
    assert report.content.shot_analyses[0].start_timestamp_us == 0
    assert report.content.shot_analyses[-1].end_timestamp_us == expected["duration_us"]
    assert all(
        left.end_timestamp_us == right.start_timestamp_us
        for left, right in zip(
            report.content.shot_analyses,
            report.content.shot_analyses[1:],
        )
    )

    evidence_refs = collect_evidence_refs(report.content)
    assert any(ref.endswith("/visual/contact_sheet.jpg") for ref in evidence_refs)
    assert any("/visual/candidate_frames/" in ref for ref in evidence_refs)
    for evidence_ref in evidence_refs:
        assert (ROOT / evidence_ref).is_file(), evidence_ref

    assert report.content.bgm_analysis.audio_scope == "mixed_program_audio"
    assert all(
        hypothesis.status == "inference"
        for hypothesis in report.content.bgm_analysis.tempo_hypotheses
    )
    assert report.content.bgm_analysis.sections[0].start_timestamp_us == 0
    assert (
        report.content.bgm_analysis.sections[-1].end_timestamp_us
        == report.content.bgm_analysis.duration_us
    )


@pytest.mark.parametrize("slug", EXPECTED)
def test_automatic_candidates_are_not_treated_as_confirmed_shots(slug: str) -> None:
    expected = EXPECTED[slug]
    visual_manifest = json.loads(
        (LEARNING_ROOT / slug / "visual/evidence_manifest.json").read_text(encoding="utf-8")
    )
    boundaries = json.loads(
        (LEARNING_ROOT / slug / "visual/boundary_candidates.json").read_text(encoding="utf-8")
    )

    assert visual_manifest["algorithm_version"] == "video-analysis-visual-v1.1.0"
    assert boundaries["candidate_semantics"].startswith("候选只表示全帧变化峰值")
    assert all(candidate["status"] == "candidate" for candidate in boundaries["candidates"])
    assert len(boundaries["candidates"]) == expected["candidate_count"]
    assert len(_report(slug).content.shot_analyses) != len(boundaries["candidates"])


@pytest.mark.parametrize("slug", EXPECTED)
def test_knowledge_templates_separate_shared_mechanisms_from_source_evidence(
    slug: str,
) -> None:
    path = FIXTURE_ROOT / slug / "knowledge_records.json"
    records = [
        KnowledgeRecord.model_validate(item)
        for item in json.loads(path.read_text(encoding="utf-8"))
    ]
    shared = [record for record in records if record.collection == "creation_knowledge"]
    source_evidence = [record for record in records if record.collection == "reference_evidence"]

    assert {stage for record in shared for stage in record.applicable_stages} == {
        "stage1",
        "stage2",
        "stage3",
    }
    assert {record.knowledge_type for record in shared} == {
        "asset_selection",
        "bgm_structure",
        "layering_rule",
        "rhythm_unit",
        "video_type",
        "viewing_experience",
    }
    assert all(record.visibility == "creation_shared" for record in shared)
    assert all(record.transferability == "reusable_mechanism" for record in shared)
    assert {stage for record in source_evidence for stage in record.applicable_stages} == {
        "stage1",
        "stage2",
        "stage3",
    }
    assert all(record.visibility == "evidence_only" for record in source_evidence)
    assert all(record.transferability == "reference_specific" for record in source_evidence)

    report_path = FIXTURE_ROOT / slug / "report_manifest.json"
    report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
    for record in records:
        assert record.source_report_ref.sha256 == report_sha256
        for evidence_ref in record.evidence_refs:
            assert (ROOT / evidence_ref).is_file(), evidence_ref


@pytest.mark.parametrize("slug", EXPECTED)
def test_shared_projection_omits_reference_specific_content(slug: str) -> None:
    report = _report(slug)
    projection_text = json.dumps(
        report.content.creation_context_projection.model_dump(mode="json"),
        ensure_ascii=False,
    )
    records = [
        KnowledgeRecord.model_validate(item)
        for item in json.loads(
            (FIXTURE_ROOT / slug / "knowledge_records.json").read_text(encoding="utf-8")
        )
    ]
    shared_text = "\n".join(
        record.content for record in records if record.collection == "creation_knowledge"
    )

    for term in SHARED_BANNED_TERMS[slug]:
        assert term not in projection_text
        assert term not in shared_text


def test_bgm_shared_knowledge_distinguishes_four_reference_structures() -> None:
    structures: set[str] = set()
    for slug in EXPECTED:
        records = [
            KnowledgeRecord.model_validate(item)
            for item in json.loads(
                (FIXTURE_ROOT / slug / "knowledge_records.json").read_text(encoding="utf-8")
            )
        ]
        structures.update(
            record.content
            for record in records
            if record.collection == "creation_knowledge"
            and record.knowledge_type == "bgm_structure"
        )

    assert len(structures) == len(EXPECTED)


@pytest.mark.parametrize("slug", EXPECTED)
def test_knowledge_templates_publish_and_retrieve_by_all_three_stages(
    slug: str,
    tmp_path: Path,
) -> None:
    report = _report(slug)
    records = [
        KnowledgeRecord.model_validate(item)
        for item in json.loads(
            (FIXTURE_ROOT / slug / "knowledge_records.json").read_text(encoding="utf-8")
        )
    ]
    report_ref = records[0].source_report_ref
    task_id = f"reference_study_{slug}"
    store = KnowledgeStore(tmp_path / slug)
    publication = store.publish(
        PublicationRequest(
            source_task_id=task_id,
            source_report_ref=report_ref,
            source_media_sha256=report.source_sha256,
            publication_revision=1,
            freeze_id=f"fixture_freeze_{slug}",
            records=records,
        )
    )

    assert publication.collection_counts == {
        "creation_knowledge": 6,
        "reference_evidence": 3,
    }
    stage_queries: dict[Literal["stage1", "stage2", "stage3"], str] = {
        "stage1": "video_type",
        "stage2": "asset_selection",
        "stage3": "rhythm_unit",
    }
    for stage, knowledge_type in stage_queries.items():
        hits = store.search_shared(
            Query(
                text="节奏 图层 素材 观看体验",
                stage=stage,
                knowledge_types=[knowledge_type],
                source_task_id=task_id,
            )
        )
        assert hits
        assert all(hit.visibility == "creation_shared" for hit in hits)
        assert all(hit.transferability == "reusable_mechanism" for hit in hits)

    direct_evidence = store.search_current_task_evidence(
        Query(
            text="原片人工语义镜头时间线",
            stage="stage3",
            knowledge_types=["reference_shot_timeline"],
            current_task_id=task_id,
        )
    )
    assert direct_evidence
    assert all(hit.collection == "reference_evidence" for hit in direct_evidence)


@pytest.mark.parametrize("slug", EXPECTED)
def test_generated_json_and_markdown_match_validated_fixture(slug: str) -> None:
    generated_json = REPORT_ROOT / slug / "reference_report.json"
    generated_markdown = REPORT_ROOT / slug / "reference_report.md"
    generated_report = ReferenceReportManifest.model_validate_json(
        generated_json.read_text(encoding="utf-8")
    )

    assert generated_report == _report(slug)
    markdown = generated_markdown.read_text(encoding="utf-8")
    assert "## 三、参考视频逐镜效果规划表" in markdown
    assert "## 六、重新创作映射" in markdown
