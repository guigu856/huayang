from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from components.audio_analysis import AudioAnalysisResult
from components.render_inspection import InspectionCheck, RenderInspectionReport
from components.video_editor.models import MediaMetadata
from validation.e2e import (
    CreationScenarioRunner,
    ReferenceFixture,
    ScenarioPlanner,
    bundled_scenario_path,
    choose_beat_aligned_boundaries,
    load_scenario,
)
from validation.e2e.media import (
    ResolvedBgm,
    ResolvedVisualMaterial,
    ScenarioMediaResolver,
)
from video_create_plugin.execution import ExecutionManifest
from video_create_plugin.knowledge import (
    Hit,
    KnowledgeRecord,
    PublicationRequest,
    SearchResult,
)
from video_create_plugin.mcp.application import PluginApplication
from video_create_plugin.models import ArtifactRef


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _knowledge(knowledge_type: str) -> SearchResult:
    reference = ArtifactRef(
        artifact_id="artifact_0123456789abcdef",
        revision=1,
        sha256="a" * 64,
    )
    return SearchResult(
        current_task_reference_evidence=[],
        shared_creation_knowledge=[
            Hit(
                knowledge_id=f"knowledge_{_digest(knowledge_type)[:16]}",
                publication_id="publication_0123456789abcdef",
                collection="creation_knowledge",
                content="共享知识说明镜头密度、图层停留和音乐节拍之间的可迁移关系",
                score=0.9,
                match_reasons=["stage=stage3"],
                source_task_id="task_reference",
                source_report_ref=reference,
                source_artifact_refs=[reference],
                evidence_refs=["evidence://unit/1"],
                applicable_stages=["stage1", "stage2", "stage3"],
                knowledge_type=knowledge_type,
                visibility="creation_shared",
                transferability="reusable_mechanism",
                fact_status="inference",
                confidence=0.9,
                embedding_version="zh-char-ngram-hash-v1-d384-n1-3",
            )
        ],
    )


def _visuals(tmp_path: Path, scenario_name: str) -> list[ResolvedVisualMaterial]:
    scenario = load_scenario(bundled_scenario_path(scenario_name))
    materials = []
    for index, definition in enumerate(scenario.source_clips):
        path = tmp_path / f"{definition.asset_id}.mp4"
        provenance = tmp_path / f"{definition.asset_id}.json"
        path.write_bytes(definition.asset_id.encode())
        provenance.write_text("{}", encoding="utf-8")
        materials.append(
            ResolvedVisualMaterial(
                asset_id=definition.asset_id,
                name=definition.name,
                path=path,
                sha256=_digest(definition.asset_id),
                duration_us=definition.extract_duration_us,
                width=scenario.canvas.width,
                height=scenario.canvas.height,
                content_summary=definition.content_summary,
                selection_traits=tuple(definition.selection_traits),
                source_path=tmp_path / f"source_{index % 3}.mp4",
                source_sha256=_digest(f"source-{index % 3}"),
                source_start_us=definition.source_start_us,
                provenance_path=provenance,
            )
        )
    return materials


def _bgm(tmp_path: Path) -> ResolvedBgm:
    audio = tmp_path / "bgm.mp3"
    provenance = tmp_path / "bgm.json"
    audio.write_bytes(b"bgm")
    provenance.write_text("{}", encoding="utf-8")
    analysis_dir = tmp_path / "audio_analysis"
    analysis_dir.mkdir()
    evidence = analysis_dir / "evidence_manifest.json"
    evidence.write_text("{}", encoding="utf-8")
    analysis = AudioAnalysisResult(
        source_path=audio,
        output_dir=analysis_dir,
        evidence_manifest_path=evidence,
        media_probe_path=analysis_dir / "media_probe.json",
        audio_signals_path=analysis_dir / "audio_signals.json",
        energy_curve_path=analysis_dir / "energy_curve.json",
        spectral_flux_path=analysis_dir / "spectral_flux.json",
        transient_candidates_path=analysis_dir / "transient_candidates.json",
        silence_regions_path=analysis_dir / "silence_regions.json",
        tempo_candidates_path=analysis_dir / "tempo_candidates.json",
        beat_grid_path=analysis_dir / "beat_grid.json",
        section_candidates_path=analysis_dir / "section_candidates.json",
        source_sha256=_digest("bgm"),
        manifest_sha256=_digest("analysis"),
        algorithm_version="audio-analysis-dsp-v1.0.0",
        audio_scope="mixed_program_audio",
        sample_rate=22_050,
        sample_count=132_300,
        duration_us=6_000_000,
        transient_count=12,
    )
    return ResolvedBgm(
        asset_id="material_bgm",
        name="test bgm",
        path=audio,
        sha256=_digest("bgm"),
        duration_us=6_025_000,
        provenance_path=provenance,
        provider="mixkit",
        creator="test creator",
        source_url="https://mixkit.co/free-stock-music/",
        license_record="Mixkit Stock Music Free License",
        mood_traits=("pulse",),
        tempo_candidates_bpm=(120.0,),
        beat_grid_us=tuple(range(500_000, 6_000_000, 500_000)),
        sections=((0, 3_000_000, 0.25), (3_000_000, 6_000_000, 0.55)),
        analysis=analysis,
    )


class _FixtureMediaResolver(ScenarioMediaResolver):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.metadata_by_sha256: dict[str, MediaMetadata] = {}

    def resolve_visuals(
        self,
        scenario: Any,
        output_dir: Path,
    ) -> list[ResolvedVisualMaterial]:
        visual_root = self.root / "visuals"
        visual_root.mkdir(parents=True, exist_ok=True)
        materials = _visuals(visual_root, str(scenario.profile))
        for material in materials:
            self.metadata_by_sha256[material.sha256] = MediaMetadata(
                duration=material.duration_us / 1_000_000,
                width=material.width,
                height=material.height,
                frame_rate=30.0,
                video_codec="h264",
            )
        return materials

    def resolve_bgm(self, scenario: Any, output_dir: Path) -> ResolvedBgm:
        bgm_root = self.root / "bgm"
        bgm_root.mkdir(parents=True, exist_ok=True)
        bgm = _bgm(bgm_root)
        self.metadata_by_sha256[bgm.sha256] = MediaMetadata(
            duration=bgm.duration_us / 1_000_000,
            audio_codec="mp3",
            sample_rate=44_100,
            channels=2,
        )
        return bgm

    def probe(self, path: Path) -> MediaMetadata:
        return self.metadata_by_sha256[path.stem]


def _publish_stage_knowledge(application: PluginApplication) -> None:
    report_ref = ArtifactRef(
        artifact_id="artifact_0123456789abcdef",
        revision=1,
        sha256="a" * 64,
    )
    definitions = (
        ("stage1", "video_type", "高密度混剪以连续视觉推进形成类型特征"),
        ("stage1", "viewing_experience", "镜头密度与短暂停顿共同控制观看呼吸"),
        ("stage2", "asset_selection", "选择主体清晰且运动方向互补的视觉素材"),
        ("stage2", "bgm_structure", "音乐段落能量和节拍网格共同约束素材筹备"),
        ("stage3", "rhythm_unit", "蓄力、连续切换、叠层和停留组成节奏单元"),
        ("stage3", "layering_rule", "辅助图层跟随音色进入并在主镜切换后延迟退出"),
    )
    records = [
        KnowledgeRecord.model_validate(
            {
                "collection": "creation_knowledge",
                "source_task_id": "task_reference_fixture",
                "source_report_ref": report_ref,
                "source_artifact_refs": [report_ref],
                "analysis_version": "reference-analysis-test",
                "applicable_stages": [stage],
                "knowledge_type": knowledge_type,
                "visibility": "creation_shared",
                "transferability": "reusable_mechanism",
                "content": content,
                "evidence_refs": [f"evidence://{stage}/{knowledge_type}"],
                "fact_status": "inference",
                "confidence": 0.9,
                "granularity": "rhythm_unit",
            }
        )
        for stage, knowledge_type, content in definitions
    ]
    application.knowledge_store.publish(
        PublicationRequest(
            source_task_id="task_reference_fixture",
            source_report_ref=report_ref,
            source_media_sha256="f" * 64,
            publication_revision=1,
            freeze_id="freeze_fixture",
            records=records,
        )
    )


def _passed_inspection(source: Path, expectation: Any, output_dir: Path) -> Any:
    output_dir.mkdir(parents=True, exist_ok=True)
    required_codes = [
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
    ]
    if expectation.expected_audio:
        required_codes.extend(["audio_level", "audio_clipping"])
    report = RenderInspectionReport(
        algorithm_version="render-inspection-fixture",
        source_path=str(source.resolve()),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        passed=True,
        checks=[InspectionCheck(code=code, passed=True, message="通过") for code in required_codes],
        video_metrics={},
        audio_metrics={},
        overlay_metrics=[],
    )
    report_path = output_dir / "render_inspection.json"
    contact_sheet_path = output_dir / "contact_sheet.jpg"
    report_path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    contact_sheet_path.write_bytes(b"contact-sheet")
    return SimpleNamespace(
        report=report,
        report_path=report_path,
        contact_sheet_path=contact_sheet_path,
    )


def test_bundled_scenarios_are_strict_and_opposite() -> None:
    fast = load_scenario(bundled_scenario_path("fast_cut_pip"))
    calm = load_scenario(bundled_scenario_path("calm_layered"))

    assert fast.main_shot_count == 6
    assert len(fast.pip_events) == 3
    assert calm.main_shot_count == 3
    assert len(calm.pip_events) == 1
    assert calm.pip_events[0].duration_us >= 1_000_000
    all_sources = [*fast.source_clips, *calm.source_clips]
    assert all("01一秒多切" not in item.source_path for item in all_sources)
    assert fast.forbidden_source_sha256s == calm.forbidden_source_sha256s
    for scenario in (fast, calm):
        assert scenario.knowledge_queries.stage1.knowledge_types == [
            "video_type",
            "viewing_experience",
        ]
        assert scenario.knowledge_queries.stage2.knowledge_types == [
            "asset_selection",
            "bgm_structure",
        ]
        assert scenario.knowledge_queries.stage3.knowledge_types == [
            "rhythm_unit",
            "layering_rule",
        ]
        assert scenario.knowledge_queries.stage1.limit == 1
        assert scenario.knowledge_queries.stage2.limit == 1
        assert scenario.knowledge_queries.stage3.limit == 1


def test_stage_one_direction_stays_semantic_and_has_no_execution_details() -> None:
    planner = ScenarioPlanner()
    for scenario_name in ("fast_cut_pip", "calm_layered"):
        scenario = load_scenario(bundled_scenario_path(scenario_name))
        direction = planner.build_creative_direction(
            scenario,
            _knowledge("video_type"),
        )
        semantic_plan = re.sub(r"## 用户意图\n.*?(?=\n## )", "", direction, flags=re.DOTALL)

        assert not re.search(r"[A-Za-z]:[\\/]", semantic_plan)
        assert not re.search(r"\d", semantic_plan)
        assert not re.search(r"SHA|右下|左下|右上|左上|时间码|坐标|三支|四个|一秒", semantic_plan)
        assert "派生不同区间" not in semantic_plan


def test_boundary_planner_uses_only_real_beat_candidates() -> None:
    beats = list(range(500_000, 6_000_000, 500_000))

    fast = choose_beat_aligned_boundaries(beats, 6_000_000, 6)
    calm = choose_beat_aligned_boundaries(beats, 6_000_000, 3)

    assert fast == [1_000_000, 2_000_000, 3_000_000, 4_000_000, 5_000_000]
    assert calm == [2_000_000, 4_000_000]
    assert set(fast + calm) <= set(beats)


def test_fast_scenario_builds_six_distinct_main_shots_and_three_pips(
    tmp_path: Path,
) -> None:
    scenario = load_scenario(bundled_scenario_path("fast_cut_pip"))
    specification = ScenarioPlanner().build_editing_specification(
        scenario,
        _visuals(tmp_path, "fast_cut_pip"),
        _bgm(tmp_path),
        "retrieval_fast",
        _knowledge("editing_sentence"),
    )

    assert len(specification.shots) == 6
    assert all(700_000 <= shot.timeline.duration_us <= 1_300_000 for shot in specification.shots)
    overlays = [action for action in specification.actions if action.layer > 0]
    assert len(overlays) == 3
    actions_by_id = {action.action_id: action for action in specification.actions}
    main_asset_ids = [actions_by_id[shot.main_action_id].asset_id for shot in specification.shots]
    assert len(set(main_asset_ids)) == 6
    assert all(
        shot.timeline.end_us in specification.beat_grid_us for shot in specification.shots[:-1]
    )


def test_calm_scenario_builds_three_long_shots_and_one_long_overlay(
    tmp_path: Path,
) -> None:
    scenario = load_scenario(bundled_scenario_path("calm_layered"))
    specification = ScenarioPlanner().build_editing_specification(
        scenario,
        _visuals(tmp_path, "calm_layered"),
        _bgm(tmp_path),
        "retrieval_calm",
        _knowledge("density_pattern"),
    )

    assert len(specification.shots) == 3
    assert all(shot.timeline.duration_us >= 1_500_000 for shot in specification.shots)
    overlays = [action for action in specification.actions if action.layer > 0]
    assert len(overlays) == 1
    assert overlays[0].timeline.duration_us == scenario.pip_events[0].duration_us
    expectation = ScenarioPlanner().render_expectation(scenario, specification)
    assert expectation.minimum_distinct_assets == 4
    assert expectation.shot_boundaries_us == [2_000_000, 4_000_000]


@pytest.mark.parametrize(
    ("scenario_name", "reference_fixture_slug"),
    [
        ("fast_cut_pip", None),
        ("calm_layered", None),
        ("fast_cut_pip", "01_fastcut_pip"),
    ],
)
def test_creation_runner_uses_application_boundary_and_submits_bound_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario_name: str,
    reference_fixture_slug: str | None,
) -> None:
    plugin_output = tmp_path / "plugin-output"
    run_root = tmp_path / "run"
    application = PluginApplication(
        plugin_output,
        project_root=Path(__file__).parents[1],
        media_roots=(tmp_path,),
    )
    _publish_stage_knowledge(application)

    search_types: list[list[str]] = []
    submitted_types: list[str] = []
    call_order: list[str] = []
    original_search = application.knowledge_search
    original_reference_context = application.reference_creation_context
    original_submit_artifact = application.submit_artifact
    original_submit_render = application.editor_submit_render

    def tracked_search(access_handle: str, query: dict[str, Any]) -> dict[str, Any]:
        search_types.append(list(query["knowledge_types"]))
        call_order.append(f"search:stage{len(search_types)}")
        return original_search(access_handle, query)

    def tracked_reference_context(access_handle: str) -> dict[str, Any]:
        response = original_reference_context(access_handle)
        call_order.append(f"context:{response['binding']['projection_stage']}")
        return response

    def tracked_submit_artifact(**kwargs: Any) -> dict[str, Any]:
        submitted_types.append(str(kwargs["artifact_type"]))
        return original_submit_artifact(**kwargs)

    def completed_submit_render(access_handle: str, project_id: str) -> dict[str, Any]:
        submitted = original_submit_render(access_handle, project_id)
        render_job_id = str(submitted["id"])
        job = application.render_queue.get(render_job_id)
        output_path = Path(job.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"application-bound-render")
        completed = job.model_copy(
            update={
                "status": "succeeded",
                "progress": 1.0,
                "message": "渲染完成",
            }
        )
        (application.render_queue.root / render_job_id / "job.json").write_text(
            json.dumps(completed.model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8",
        )
        return submitted

    monkeypatch.setattr(application, "knowledge_search", tracked_search)
    monkeypatch.setattr(
        application,
        "reference_creation_context",
        tracked_reference_context,
    )
    monkeypatch.setattr(application, "submit_artifact", tracked_submit_artifact)
    monkeypatch.setattr(application, "_start_render_worker", lambda: None)
    monkeypatch.setattr(application, "editor_submit_render", completed_submit_render)
    monkeypatch.setattr(application.render_inspection, "inspect", _passed_inspection)

    scenario = load_scenario(bundled_scenario_path(scenario_name))
    media_resolver = _FixtureMediaResolver(tmp_path / "fixture-media")
    monkeypatch.setattr(application.compiler, "_probe", media_resolver.probe)
    result = CreationScenarioRunner(
        project_root=Path(__file__).parents[1],
        plugin_output_root=plugin_output,
        media_resolver=media_resolver,
        application=application,
    ).run(
        scenario,
        run_root,
        reference_fixture=(
            ReferenceFixture(reference_fixture_slug) if reference_fixture_slug is not None else None
        ),
    )

    assert search_types == [
        scenario.knowledge_queries.stage1.knowledge_types,
        scenario.knowledge_queries.stage2.knowledge_types,
        scenario.knowledge_queries.stage3.knowledge_types,
    ]
    assert call_order == (
        [
            "context:creative_direction",
            "search:stage1",
            "context:resource_preparation",
            "search:stage2",
            "context:editing_specification",
            "search:stage3",
        ]
        if reference_fixture_slug is not None
        else ["search:stage1", "search:stage2", "search:stage3"]
    )
    expected_types = [
        "creative_direction",
        "preparation_package",
        "editing_specification",
        "execution_manifest",
    ]
    if reference_fixture_slug is not None:
        expected_types.insert(0, "reference_report_manifest")
    assert submitted_types == expected_types
    assert application.get_task(result.task_id)["task"]["status"] == "completed"

    execution = ExecutionManifest.model_validate_json(
        (run_root / "execution_manifest.json").read_bytes()
    )
    assert (
        Path(execution.project_path).parent
        == application.project_storage.root / execution.project_id
    )
    assert Path(execution.render_path).parent.parent == (
        application.project_storage.root / execution.project_id
    )
    assert Path(execution.inspection_binding_path).is_file()
    assert result.project_path == (run_root / "project" / "project.json").resolve()
    assert result.trace_map_path == (run_root / "spec_trace_map.json").resolve()
    assert result.render_path == (run_root / "render.mp4").resolve()

    manifest = json.loads(result.run_manifest_path.read_text(encoding="utf-8"))
    assert manifest["task_type"] == (
        "reference_guided_creation" if reference_fixture_slug is not None else "original_creation"
    )
    if reference_fixture_slug is not None:
        assert manifest["reference_study"]["fixture_slug"] == reference_fixture_slug
        reference_report = json.loads(
            (
                Path(__file__).parents[1]
                / "validation"
                / "reference_studies"
                / reference_fixture_slug
                / "report_manifest.json"
            ).read_text(encoding="utf-8")
        )
        projections = {
            projection["stage"]: projection
            for projection in reference_report["content"]["creation_context_projection"][
                "stage_projections"
            ]
        }
        direction_text = (run_root / "stage1_creative_direction.md").read_text(encoding="utf-8")
        assert projections["creative_direction"]["recommendations"][0]["text"] in direction_text
        for path, stage in (
            ("stage2_preparation_package.json", "resource_preparation"),
            ("stage3_editing_specification.json", "editing_specification"),
        ):
            payload = json.loads((run_root / path).read_text(encoding="utf-8"))
            assert payload["reference_context"]["projection_stage"] == stage
            assert projections[stage]["recommendations"][0]["text"] in json.dumps(
                payload,
                ensure_ascii=False,
            )
    assert manifest["inspection_passed"] is True
    assert manifest["execution_artifact"]["artifact_type"] == "execution_manifest"
    assert len(manifest["retrieval_ids"]) == 3
    listed_paths = {entry["path"] for entry in manifest["files"]}
    assert {
        "scenario.json",
        "stage1_creative_direction.md",
        "stage2_preparation_package.json",
        "stage3_editing_specification.json",
        "preflight.json",
        "project/project.json",
        "spec_trace_map.json",
        "render.mp4",
        "render_expectation.json",
        "inspection/render_inspection.json",
        "inspection/contact_sheet.jpg",
        "inspection/inspection_binding.json",
        "execution_report.json",
        "execution_manifest.json",
    } <= listed_paths
