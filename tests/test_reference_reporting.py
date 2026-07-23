from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from video_create_plugin.artifacts import ArtifactStore
from video_create_plugin.reporting import (
    BgmAnalysis,
    BgmSection,
    Claim,
    CreationContextProjection,
    EditAction,
    EditingGrammar,
    EffectObservation,
    ReferenceReportContent,
    ReferenceReportGenerator,
    ReferenceReportManifest,
    ReferenceReportValidationError,
    ReferenceReportValidator,
    ReferenceShotAnalysis,
    RhythmUnit,
    StageKnowledgeProjection,
    TempoHypothesis,
    VideoOverview,
    ViewingExperience,
    VisualLayer,
    canonical_json_bytes,
)

SOURCE_SHA256 = "a" * 64
FRAME_EVIDENCE = "evidence://frame/000015"
AUDIO_EVIDENCE = "evidence://audio/energy/000000-002000"
PROBE_EVIDENCE = "evidence://probe/source-streams"


def _claim(
    claim_id: str,
    text: str,
    *,
    status: str = "inference",
    evidence_refs: list[str] | None = None,
) -> Claim:
    return Claim.model_validate(
        {
            "claim_id": claim_id,
            "text": text,
            "status": status,
            "evidence_refs": evidence_refs or [],
        }
    )


def _content() -> ReferenceReportContent:
    overview = VideoOverview(
        source_name="参考视频.mp4",
        duration_us=2_000_000,
        width=1280,
        height=720,
        frame_rate_numerator=30,
        frame_rate_denominator=1,
        summary=_claim(
            "overview-summary",
            "主画面与画中画共同形成一轮视觉推进。",
            evidence_refs=[FRAME_EVIDENCE],
        ),
        content_goal=_claim(
            "overview-goal",
            "通过层次变化维持注意力。",
            status="opinion",
        ),
        editing_identity=[
            _claim(
                "overview-identity",
                "主画面保持时，辅助层承担局部加速。",
                evidence_refs=[FRAME_EVIDENCE],
            )
        ],
        evidence_refs=[PROBE_EVIDENCE],
    )
    bgm = BgmAnalysis(
        audio_scope="mixed_program_audio",
        duration_us=2_000_000,
        tempo_hypotheses=[
            TempoHypothesis(
                bpm=120.0,
                confidence=0.82,
                status="inference",
                basis=_claim(
                    "tempo-basis",
                    "相邻瞬态的主要间距接近半秒。",
                    evidence_refs=[AUDIO_EVIDENCE],
                ),
                evidence_refs=[AUDIO_EVIDENCE],
            )
        ],
        sections=[
            BgmSection(
                section_id="bgm-section-01",
                label="推进",
                start_timestamp_us=0,
                end_timestamp_us=2_000_000,
                energy_level=0.74,
                musical_function=_claim(
                    "bgm-function",
                    "连续瞬态维持推进。",
                    evidence_refs=[AUDIO_EVIDENCE],
                ),
                energy_direction=_claim(
                    "bgm-energy",
                    "能量先平稳后抬升。",
                    evidence_refs=[AUDIO_EVIDENCE],
                ),
                editing_relation=_claim(
                    "bgm-editing",
                    "视觉动作跟随重瞬态组，而非每个基础拍。",
                    evidence_refs=[AUDIO_EVIDENCE, FRAME_EVIDENCE],
                ),
                evidence_refs=[AUDIO_EVIDENCE],
            )
        ],
        rhythm_layers=[
            _claim(
                "bgm-rhythm-layer",
                "低频重音提供主要切换锚点。",
                evidence_refs=[AUDIO_EVIDENCE],
            )
        ],
        sound_layer_changes=[
            _claim(
                "bgm-sound-change",
                "高频瞬态出现时辅助画面进入。",
                evidence_refs=[AUDIO_EVIDENCE, FRAME_EVIDENCE],
            )
        ],
        energy_flow=[
            _claim(
                "bgm-energy-flow",
                "段末能量高于段首。",
                evidence_refs=[AUDIO_EVIDENCE],
            )
        ],
        editing_following=[
            _claim(
                "bgm-following",
                "主切换主要响应低频重音。",
                evidence_refs=[AUDIO_EVIDENCE, FRAME_EVIDENCE],
            )
        ],
        evidence_refs=[AUDIO_EVIDENCE, PROBE_EVIDENCE],
    )
    layer = VisualLayer(
        layer_id="main",
        layer_type="main",
        z_index=0,
        description=_claim(
            "layer-main",
            "主体占据全画面。",
            status="fact",
            evidence_refs=[FRAME_EVIDENCE],
        ),
        evidence_refs=[FRAME_EVIDENCE],
    )
    action = EditAction(
        action_id="action-01",
        action_type="scale",
        start_timestamp_us=200_000,
        end_timestamp_us=450_000,
        target_layer_ids=["main"],
        description=_claim(
            "action-description",
            "主体短促放大后回稳。",
            status="fact",
            evidence_refs=[FRAME_EVIDENCE],
        ),
        timing_relation_to_music=_claim(
            "action-music-relation",
            "动作峰值接近低频重音。",
            evidence_refs=[AUDIO_EVIDENCE, FRAME_EVIDENCE],
        ),
        evidence_refs=[FRAME_EVIDENCE],
    )
    effect = EffectObservation(
        effect_id="effect-01",
        effect_type="motion_blur",
        target_scope="layer",
        target_layer_ids=["main"],
        description=_claim(
            "effect-description",
            "运动模糊仅作用于主体层。",
            status="fact",
            evidence_refs=[FRAME_EVIDENCE],
        ),
        evidence_refs=[FRAME_EVIDENCE],
    )
    shot = ReferenceShotAnalysis(
        shot_id="shot-01",
        start_timestamp_us=0,
        end_timestamp_us=2_000_000,
        summary=_claim(
            "shot-summary",
            "主体层在重音前完成蓄力。",
            evidence_refs=[AUDIO_EVIDENCE, FRAME_EVIDENCE],
        ),
        layers=[layer],
        actions=[action],
        effects=[effect],
        sound_relation=_claim(
            "shot-sound",
            "缩放响应段内首个强瞬态。",
            evidence_refs=[AUDIO_EVIDENCE, FRAME_EVIDENCE],
        ),
        preceding_rhythm_role=_claim(
            "shot-before",
            "承接上一段的稳定画面。",
            evidence_refs=[FRAME_EVIDENCE],
        ),
        following_rhythm_role=_claim(
            "shot-after",
            "为后续辅助层进入保留视觉空间。",
            evidence_refs=[FRAME_EVIDENCE],
        ),
        evidence_refs=[AUDIO_EVIDENCE, FRAME_EVIDENCE],
    )
    rhythm_unit = RhythmUnit(
        unit_id="unit-01",
        name="蓄力后释放",
        start_timestamp_us=0,
        end_timestamp_us=1_000_000,
        shot_ids=["shot-01"],
        music_pattern=_claim(
            "unit-music",
            "弱起后出现强瞬态。",
            evidence_refs=[AUDIO_EVIDENCE],
        ),
        visual_sequence=[
            _claim(
                "unit-visual",
                "稳定主体后使用短促缩放。",
                evidence_refs=[FRAME_EVIDENCE],
            )
        ],
        build_up_role=_claim(
            "unit-build-up",
            "稳定画面承担预备。",
            evidence_refs=[FRAME_EVIDENCE],
        ),
        release_role=_claim(
            "unit-release",
            "缩放峰值承担释放。",
            evidence_refs=[FRAME_EVIDENCE],
        ),
        transfer_rule=_claim(
            "unit-transfer",
            "新素材可保留先稳后冲的相对节奏。",
            status="opinion",
        ),
        evidence_refs=[AUDIO_EVIDENCE, FRAME_EVIDENCE],
    )
    grammar = EditingGrammar(
        rhythm_units=[rhythm_unit],
        cutting_rules=[
            _claim(
                "grammar-cut",
                "切换优先使用强瞬态作为锚点。",
                evidence_refs=[AUDIO_EVIDENCE, FRAME_EVIDENCE],
            )
        ],
        layering_rules=[
            _claim(
                "grammar-layer",
                "辅助层用于提升局部密度。",
                evidence_refs=[FRAME_EVIDENCE],
            )
        ],
        motion_rules=[],
        transition_rules=[],
        density_rules=[],
        reusable_patterns=[
            _claim(
                "grammar-reusable",
                "稳定画面、短促动作和释放停留可组成一个节奏句。",
                status="opinion",
            )
        ],
    )
    experience = ViewingExperience(
        overall_target=_claim(
            "experience-target",
            "形成紧凑但可辨认的视觉推进。",
            status="opinion",
        ),
        emotional_effects=[_claim("experience-emotion", "产生轻微冲击感。", status="opinion")],
        pacing_and_breathing=[
            _claim(
                "experience-breathing",
                "短动作后保留稳定停留。",
                evidence_refs=[FRAME_EVIDENCE],
            )
        ],
        richness_and_layering=[],
        attention_guidance=[],
    )
    stage_projections = [
        StageKnowledgeProjection(
            stage="creative_direction",
            knowledge_types=["viewing_experience"],
            retrieval_tags=["紧凑推进"],
            recommendations=[
                _claim(
                    "projection-direction",
                    "总体方案保留紧凑推进与释放停留。",
                    status="opinion",
                )
            ],
        ),
        StageKnowledgeProjection(
            stage="resource_preparation",
            knowledge_types=["material_role"],
            retrieval_tags=["主体清晰"],
            recommendations=[
                _claim(
                    "projection-resource",
                    "选择轮廓清楚且运动方向可衔接的新素材。",
                    status="opinion",
                )
            ],
        ),
        StageKnowledgeProjection(
            stage="editing_specification",
            knowledge_types=["editing_grammar"],
            retrieval_tags=["先稳后冲"],
            recommendations=[
                _claim(
                    "projection-spec",
                    "以相对节奏关系生成新的动作时间。",
                    status="opinion",
                )
            ],
        ),
    ]
    projection = CreationContextProjection(
        core_goal=_claim(
            "projection-core",
            "理解节奏组织后使用新素材重构体验。",
            status="opinion",
        ),
        transferable_patterns=[
            _claim(
                "projection-transferable",
                "先稳后冲的节奏关系可迁移。",
                status="opinion",
            )
        ],
        non_transferable_specifics=[
            _claim(
                "projection-specific",
                "原视频人物、素材和绝对时间点属于原视频特有信息。",
                status="opinion",
            )
        ],
        new_material_reconstruction_guidance=[
            _claim(
                "projection-reconstruct",
                "使用不同素材重建相似密度，而非照搬绝对参数。",
                status="opinion",
            )
        ],
        stage_projections=stage_projections,
    )
    return ReferenceReportContent(
        video_overview=overview,
        bgm_analysis=bgm,
        shot_analyses=[shot],
        editing_grammar=grammar,
        viewing_experience=experience,
        creation_context_projection=projection,
    )


def _manifest() -> ReferenceReportManifest:
    return ReferenceReportManifest.build(
        analysis_id="analysis-example",
        source_sha256=SOURCE_SHA256,
        content=_content(),
    )


def test_factual_claim_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="事实性断言必须关联证据"):
        Claim(
            claim_id="fact-without-evidence",
            text="画面中存在两个图层。",
            status="fact",
            evidence_refs=[],
        )

    unverified = Claim(
        claim_id="unverified",
        text="这一变化可能来自歌词。",
        status="unverified",
        evidence_refs=[],
    )
    assert unverified.status == "unverified"


def test_shot_rejects_unknown_layer_and_out_of_range_action() -> None:
    content = _content()
    shot = content.shot_analyses[0]
    invalid_action = shot.actions[0].model_copy(update={"target_layer_ids": ["unknown"]})

    with pytest.raises(ValidationError, match="剪辑动作引用了未知图层"):
        ReferenceShotAnalysis(
            **shot.model_dump(exclude={"actions"}),
            actions=[invalid_action],
        )

    late_action = shot.actions[0].model_copy(update={"end_timestamp_us": 2_100_000})
    with pytest.raises(ValidationError, match="剪辑动作时间超出所属镜头"):
        ReferenceShotAnalysis(
            **shot.model_dump(exclude={"actions"}),
            actions=[late_action],
        )


def test_report_rejects_unknown_shot_in_rhythm_unit() -> None:
    content = _content()
    grammar = content.editing_grammar.model_copy(
        update={
            "rhythm_units": [
                content.editing_grammar.rhythm_units[0].model_copy(
                    update={"shot_ids": ["shot-missing"]}
                )
            ]
        }
    )

    with pytest.raises(ValidationError, match="节奏单元引用了未知镜头"):
        ReferenceReportContent(
            **content.model_dump(exclude={"editing_grammar"}),
            editing_grammar=grammar,
        )


def test_manifest_hash_and_serialization_are_deterministic() -> None:
    first = _manifest()
    second = _manifest()
    first_json = canonical_json_bytes(first)

    assert (
        first.report_content_sha256
        == hashlib.sha256(canonical_json_bytes(first.content)).hexdigest()
    )
    assert first_json == canonical_json_bytes(second)
    assert first.evidence_refs == sorted(first.evidence_refs)
    assert ReferenceReportManifest.model_validate_json(first_json) == first


def test_validator_rejects_tampered_hash_and_evidence_index() -> None:
    validator = ReferenceReportValidator()
    payload = _manifest().model_dump(mode="python")
    payload["report_content_sha256"] = "0" * 64
    with pytest.raises(ReferenceReportValidationError) as hash_error:
        validator.validate(payload)
    assert hash_error.value.code == "reference_report_invalid"
    assert hash_error.value.details["error_count"] == 1

    payload = _manifest().model_dump(mode="python")
    payload["evidence_refs"] = [PROBE_EVIDENCE]
    with pytest.raises(ReferenceReportValidationError):
        validator.validate(payload)


def test_generator_publishes_deterministic_json_and_chinese_markdown(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    store = ArtifactStore(tmp_path / "objects")
    generator = ReferenceReportGenerator()

    first = generator.generate(manifest, store)
    second = generator.generate(manifest, store)
    markdown = store.read_bytes(first.markdown_artifact.uri).decode("utf-8")

    assert first.json_artifact.sha256 == second.json_artifact.sha256
    assert first.markdown_artifact.sha256 == second.markdown_artifact.sha256
    assert (
        first.json_artifact.sha256 == hashlib.sha256(generator.serialize_json(manifest)).hexdigest()
    )
    assert first.manifest.report_content_sha256 == manifest.report_content_sha256
    assert "# 参考视频分析报告" in markdown
    assert "## 三、参考视频逐镜效果规划表" in markdown
    assert "00:00.000–00:01.000" in markdown
    assert "图层" in markdown
    assert "声音关系" in markdown
    assert "[已证实事实]" in markdown
    assert "## 六、重新创作映射" in markdown
