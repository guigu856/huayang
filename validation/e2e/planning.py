from __future__ import annotations

from components.render_inspection import OverlayExpectation, RenderExpectation
from video_create_plugin.creation import (
    BgmPackage,
    BgmSection,
    PreparationPackage,
    PreparedMaterial,
    SourceRange,
)
from video_create_plugin.editing import (
    ActionSpec,
    CanvasSpec,
    EditingSpecification,
    MaterialAsset,
    ShotSpec,
    StaticTransform,
    TimeRange,
)
from video_create_plugin.knowledge import SearchResult
from video_create_plugin.reporting import StageKnowledgeProjection

from .media import ResolvedBgm, ResolvedVisualMaterial
from .scenario import PipDefinition, ScenarioDefinition


class ScenarioPlanningError(RuntimeError):
    pass


def choose_beat_aligned_boundaries(
    beat_grid_us: list[int] | tuple[int, ...],
    duration_us: int,
    shot_count: int,
) -> list[int]:
    candidates = sorted(set(beat for beat in beat_grid_us if 0 < beat < duration_us))
    required = shot_count - 1
    if len(candidates) < required:
        raise ScenarioPlanningError("真实 BGM 节拍候选不足以形成声明的主镜头数量")
    selected: list[int] = []
    remaining = candidates
    for index in range(1, shot_count):
        target = round(duration_us * index / shot_count)
        available = [
            value
            for position, value in enumerate(remaining)
            if len(remaining) - position >= shot_count - index
        ]
        if selected:
            available = [value for value in available if value > selected[-1]]
        if not available:
            raise ScenarioPlanningError("节拍候选未形成严格递增的镜头边界")
        chosen = min(available, key=lambda value: (abs(value - target), value))
        selected.append(chosen)
        remaining = [value for value in remaining if value > chosen]
    return selected


class ScenarioPlanner:
    def build_creative_direction(
        self,
        scenario: ScenarioDefinition,
        knowledge: SearchResult,
        reference_projection: StageKnowledgeProjection | None = None,
    ) -> str:
        basis = _planning_basis(knowledge, reference_projection)
        if scenario.profile == "fast_cut_pip":
            sections = {
                "title": "脉冲驱动短切与局部叠层重创作",
                "video_type": "高密度节拍驱动动画混剪",
                "core_mechanism": f"稳定脉冲驱动短镜头组，局部辅助层提高节奏单元密度。规划依据：{basis}",
                "production_method": "选用与参考主题不同且视觉角色明确的新素材，按可迁移节奏机制重新组织",
                "visual_language": "全屏主层保持主体可读，局部辅助层只在完整节奏单元中进入与退出",
                "rhythm_and_sound": "主画面响应音乐主脉冲，辅助层响应次级节奏或音色变化",
                "transition_principles": "主画面变化保持直接清晰，辅助层不跨越语义段落",
                "asset_and_music_traits": "主体清晰、构图差异明确的新素材，音乐具有稳定脉冲与能量推进",
                "viewing_experience": "连续推进中保持画面可辨识，并以局部叠层形成视觉刺激",
            }
        else:
            sections = {
                "title": "低密度长镜与克制叠层重创作",
                "video_type": "低密度舒缓叠层混剪",
                "core_mechanism": f"少量长镜头形成呼吸，克制的持续辅助层建立空间深度。规划依据：{basis}",
                "production_method": "使用可持续观看、内部运动连贯的新素材，以低切换密度重新组织",
                "visual_language": "画面以长时间全屏主体为主，辅助层缓慢保持而非频繁闪现",
                "rhythm_and_sound": "画面段落变化响应音乐结构，并按多拍跨度形成长句",
                "transition_principles": "只在长段落边界直接切换，避免高频转场堆叠",
                "asset_and_music_traits": "选择主体稳定、构图可持续展开的新素材，音乐气质平静且段落清楚",
                "viewing_experience": "观看节奏留有呼吸，层次变化集中而克制",
            }
        return (
            f"# {sections['title']}\n\n"
            f"## 用户意图\n{scenario.user_intent}\n\n"
            f"## 视频类型与核心机制\n{sections['video_type']}。{sections['core_mechanism']}\n\n"
            f"## 整体制作方法\n{sections['production_method']}\n\n"
            f"## 视觉语言与画面组织\n{sections['visual_language']}\n\n"
            f"## 节奏与声音\n{sections['rhythm_and_sound']}\n\n"
            f"## 转场与镜头连接\n{sections['transition_principles']}\n\n"
            f"## 素材与音乐性质\n{sections['asset_and_music_traits']}\n\n"
            f"## 预期观看体验\n{sections['viewing_experience']}\n"
        )

    def build_preparation_package(
        self,
        scenario: ScenarioDefinition,
        visuals: list[ResolvedVisualMaterial],
        bgm: ResolvedBgm,
        retrieval_id: str,
        knowledge: SearchResult,
        reference_projection: StageKnowledgeProjection | None = None,
    ) -> PreparationPackage:
        basis = _planning_basis(knowledge, reference_projection)
        prepared = [
            PreparedMaterial(
                asset_id=material.asset_id,
                kind="video",
                name=material.name,
                path=str(material.path),
                sha256=material.sha256,
                duration_us=material.duration_us,
                width=material.width,
                height=material.height,
                content_summary=material.content_summary,
                selection_traits=[*material.selection_traits, f"规划依据：{basis}"],
                usable_source_ranges=[SourceRange(start_us=0, end_us=material.duration_us)],
                source_url=material.source_path.as_uri(),
                provider="user_supplied_video_derivative",
                creator="user supplied source",
                license_record="local validation derivative with retained provenance",
                provenance_ref=material.provenance_path.as_uri(),
            )
            for material in visuals
        ]
        sections = [
            BgmSection(
                section_id=f"section_{index:02d}",
                start_us=start,
                end_us=end,
                role="低能量铺垫" if energy < 0.35 else "能量推进",
                energy_phase=f"normalized_energy={energy:.3f}",
            )
            for index, (start, end, energy) in enumerate(bgm.sections, start=1)
        ]
        bgm_package = BgmPackage(
            asset_id=bgm.asset_id,
            name=bgm.name,
            path=str(bgm.path),
            sha256=bgm.sha256,
            duration_us=bgm.duration_us,
            source_url=bgm.source_url,
            provider=bgm.provider,
            creator=bgm.creator,
            license_record=bgm.license_record,
            provenance_ref=bgm.provenance_path.as_uri(),
            audio_analysis_ref=bgm.analysis.evidence_manifest_path.as_uri(),
            mood_traits=list(bgm.mood_traits),
            tempo_candidates_bpm=list(bgm.tempo_candidates_bpm),
            beat_grid_us=list(bgm.beat_grid_us),
            sections=sections,
        )
        return PreparationPackage(
            materials=prepared,
            bgm=bgm_package,
            provenance_refs=[
                *(material.provenance_path.as_uri() for material in visuals),
                bgm.provenance_path.as_uri(),
            ],
            retrieval_ids=[retrieval_id],
        )

    def build_editing_specification(
        self,
        scenario: ScenarioDefinition,
        visuals: list[ResolvedVisualMaterial],
        bgm: ResolvedBgm,
        retrieval_id: str,
        knowledge: SearchResult,
        reference_projection: StageKnowledgeProjection | None = None,
    ) -> EditingSpecification:
        boundaries = choose_beat_aligned_boundaries(
            bgm.beat_grid_us,
            scenario.duration_us,
            scenario.main_shot_count,
        )
        timeline_points = [0, *boundaries, scenario.duration_us]
        visual_by_id = {material.asset_id: material for material in visuals}
        basis = _planning_basis(knowledge, reference_projection)
        assets = [
            MaterialAsset(
                asset_id=material.asset_id,
                kind="video",
                name=material.name,
                path=str(material.path),
                sha256=material.sha256,
                duration_us=material.duration_us,
                width=material.width,
                height=material.height,
                provenance_ref=material.provenance_path.as_uri(),
            )
            for material in visuals
        ]
        assets.append(
            MaterialAsset(
                asset_id=bgm.asset_id,
                kind="audio",
                name=bgm.name,
                path=str(bgm.path),
                sha256=bgm.sha256,
                duration_us=bgm.duration_us,
                provenance_ref=bgm.provenance_path.as_uri(),
            )
        )
        actions: list[ActionSpec] = []
        shots: list[ShotSpec] = []
        full_transform = StaticTransform(
            x=0,
            y=0,
            width=float(scenario.canvas.width),
            height=float(scenario.canvas.height),
        )
        pips_by_shot: dict[int, list[PipDefinition]] = {
            index: [] for index in range(scenario.main_shot_count)
        }
        for event in scenario.pip_events:
            pips_by_shot[event.shot_index].append(event)
        for index, asset_id in enumerate(scenario.main_asset_ids):
            shot_id = f"shot_{index + 1:02d}"
            start = timeline_points[index]
            end = timeline_points[index + 1]
            material = visual_by_id[asset_id]
            if material.duration_us < end - start:
                raise ScenarioPlanningError("派生主素材时长短于节拍对齐后的镜头时长")
            main_action_id = f"action_main_{index + 1:02d}"
            action_ids = [main_action_id]
            actions.append(
                ActionSpec(
                    action_id=main_action_id,
                    shot_id=shot_id,
                    action_type="visual_media",
                    timeline=TimeRange(start_us=start, end_us=end),
                    asset_id=asset_id,
                    source=TimeRange(start_us=0, end_us=end - start),
                    layer=0,
                    transform=full_transform,
                    volume=0,
                    required_capabilities=[
                        "video_source_trim",
                        "hard_cut",
                        "static_transform",
                    ],
                    human_description=f"{material.content_summary}；规划依据：{basis}",
                    audio_event_refs=[f"audio://bgm/beat/{start}"],
                )
            )
            for pip_index, event in enumerate(pips_by_shot[index], start=1):
                pip_start = start + event.start_offset_us
                pip_end = pip_start + event.duration_us
                pip_material = visual_by_id[event.asset_id]
                if pip_end > end or pip_material.duration_us < event.duration_us:
                    raise ScenarioPlanningError("画中画时间范围超出所属镜头或派生素材")
                action_id = f"action_{event.pip_id}_{pip_index:02d}"
                action_ids.append(action_id)
                actions.append(
                    ActionSpec(
                        action_id=action_id,
                        shot_id=shot_id,
                        action_type="visual_media",
                        timeline=TimeRange(start_us=pip_start, end_us=pip_end),
                        asset_id=event.asset_id,
                        source=TimeRange(start_us=0, end_us=event.duration_us),
                        layer=10,
                        transform=StaticTransform(
                            x=event.transform.x,
                            y=event.transform.y,
                            width=event.transform.width,
                            height=event.transform.height,
                            opacity=event.transform.opacity,
                        ),
                        volume=0,
                        required_capabilities=[
                            "video_source_trim",
                            "layer_overlay",
                            "static_transform",
                        ],
                        human_description=f"{event.pip_id} 镜内辅助层进入并在硬切前退出",
                        audio_event_refs=[f"audio://bgm/pulse-near/{pip_start}"],
                    )
                )
            shots.append(
                ShotSpec(
                    shot_id=shot_id,
                    timeline=TimeRange(start_us=start, end_us=end),
                    main_action_id=main_action_id,
                    action_ids=action_ids,
                    human_description=(
                        "约一秒节拍主镜头"
                        if scenario.profile == "fast_cut_pip"
                        else "多拍跨度长镜头"
                    ),
                    audio_event_refs=[f"audio://bgm/beat/{start}"],
                    transition_to_next=(
                        "end" if index == scenario.main_shot_count - 1 else "hard_cut"
                    ),
                )
            )
        bgm_action = ActionSpec(
            action_id="action_bgm_01",
            shot_id=shots[0].shot_id,
            action_type="audio_media",
            timeline=TimeRange(start_us=0, end_us=scenario.duration_us),
            asset_id=bgm.asset_id,
            source=TimeRange(start_us=0, end_us=scenario.duration_us),
            layer=0,
            volume=0.82,
            required_capabilities=["audio_source_trim", "static_volume"],
            human_description="具有真实 DSP 节拍证据的全片 Mixkit BGM",
            audio_event_refs=[f"audio://analysis/{bgm.analysis.manifest_sha256}"],
        )
        actions.append(bgm_action)
        shots[0] = shots[0].model_copy(
            update={"action_ids": [*shots[0].action_ids, bgm_action.action_id]}
        )
        specification = EditingSpecification(
            spec_id=f"spec_{scenario.scenario_id}",
            title=scenario.title,
            canvas=CanvasSpec(
                width=scenario.canvas.width,
                height=scenario.canvas.height,
                fps=scenario.canvas.fps,
            ),
            duration_us=scenario.duration_us,
            assets=assets,
            shots=shots,
            actions=actions,
            beat_grid_us=sorted({0, *bgm.beat_grid_us, scenario.duration_us}),
            retrieval_ids=[retrieval_id],
        )
        validate_scenario_specification(scenario, specification)
        return specification

    def render_expectation(
        self,
        scenario: ScenarioDefinition,
        specification: EditingSpecification,
    ) -> RenderExpectation:
        action_by_id = {action.action_id: action for action in specification.actions}
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
        visual_hashes = [
            asset.sha256
            for asset in specification.assets
            if asset.asset_id
            in {
                action.asset_id
                for action in action_by_id.values()
                if action.action_type == "visual_media"
            }
        ]
        return RenderExpectation(
            duration_us=scenario.duration_us,
            width=scenario.canvas.width,
            height=scenario.canvas.height,
            fps=scenario.canvas.fps,
            shot_boundaries_us=[shot.timeline.end_us for shot in specification.shots[:-1]],
            beat_grid_us=specification.beat_grid_us,
            overlays=overlays,
            expected_audio=True,
            asset_sha256s=visual_hashes,
            minimum_distinct_assets=scenario.minimum_distinct_visual_assets,
            action_count=len(specification.actions),
            traced_action_count=0,
        )


def validate_scenario_specification(
    scenario: ScenarioDefinition,
    specification: EditingSpecification,
) -> None:
    if len(specification.shots) != scenario.main_shot_count:
        raise ScenarioPlanningError("剪辑规格主镜头数量偏离固定场景")
    boundaries = [shot.timeline.end_us for shot in specification.shots[:-1]]
    if any(boundary not in specification.beat_grid_us for boundary in boundaries):
        raise ScenarioPlanningError("剪辑规格出现未对齐真实 BGM beat grid 的主切点")
    overlay_actions = [
        action
        for action in specification.actions
        if action.action_type == "visual_media" and action.layer > 0
    ]
    if len(overlay_actions) != len(scenario.pip_events):
        raise ScenarioPlanningError("剪辑规格画中画事件数量偏离固定场景")
    durations = [shot.timeline.duration_us for shot in specification.shots]
    if scenario.profile == "fast_cut_pip" and any(
        duration < 700_000 or duration > 1_300_000 for duration in durations
    ):
        raise ScenarioPlanningError("快切场景主镜头没有保持约一秒时长")
    if scenario.profile == "calm_layered" and any(duration < 1_500_000 for duration in durations):
        raise ScenarioPlanningError("舒缓场景主镜头没有保持长镜头密度")
    visual_assets = {
        asset.asset_id: asset for asset in specification.assets if asset.kind in {"video", "image"}
    }
    used_visual_ids = {
        action.asset_id
        for action in specification.actions
        if action.action_type == "visual_media" and action.asset_id is not None
    }
    hashes = {visual_assets[asset_id].sha256 for asset_id in used_visual_ids}
    if len(hashes) < scenario.minimum_distinct_visual_assets:
        raise ScenarioPlanningError("剪辑规格使用的独立视觉 SHA 数量低于固定门槛")


def _knowledge_summary(result: SearchResult) -> str:
    hits = result.shared_creation_knowledge
    if not hits:
        raise ScenarioPlanningError("阶段没有读到已发布共享知识")
    return hits[0].content.strip().replace("\n", " ")[:180]


def _planning_basis(
    result: SearchResult,
    reference_projection: StageKnowledgeProjection | None,
) -> str:
    shared = _knowledge_summary(result)
    if reference_projection is None:
        return f"共享知识：{shared}"
    texts = [item.text.strip().replace("\n", " ") for item in reference_projection.recommendations]
    if not texts:
        raise ScenarioPlanningError("本次参考报告阶段投影没有可用建议")
    current = "；".join(texts)[:240]
    return f"本次参考投影：{current}；共享知识：{shared}"
