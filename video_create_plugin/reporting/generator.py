from __future__ import annotations

from dataclasses import dataclass

from video_create_plugin.artifacts import ArtifactObject, ArtifactStore

from .models import (
    Claim,
    ReferenceReportManifest,
    canonical_json_bytes,
    collect_evidence_refs,
)
from .validation import ReferenceReportValidator

_STATUS_LABELS = {
    "fact": "已证实事实",
    "inference": "合理推测",
    "opinion": "观点",
    "unverified": "尚未证实",
}


@dataclass(frozen=True, slots=True)
class GeneratedReferenceReport:
    manifest: ReferenceReportManifest
    json_artifact: ArtifactObject
    markdown_artifact: ArtifactObject


class ReferenceReportGenerator:
    def __init__(self, validator: ReferenceReportValidator | None = None) -> None:
        self._validator = validator or ReferenceReportValidator()

    def serialize_json(self, manifest: ReferenceReportManifest) -> bytes:
        validated = self._validator.validate(manifest)
        return canonical_json_bytes(validated)

    def render_markdown(self, manifest: ReferenceReportManifest) -> str:
        report = self._validator.validate(manifest)
        content = report.content
        overview = content.video_overview
        lines = [
            "# 参考视频分析报告",
            "",
            f"- 分析 ID：`{_escape(report.analysis_id)}`",
            f"- 源视频 SHA-256：`{report.source_sha256}`",
            f"- 报告内容 SHA-256：`{report.report_content_sha256}`",
            "",
            "## 一、视频总概括",
            "",
            "| 项目 | 内容 |",
            "|---|---|",
            f"| 来源 | {_escape(overview.source_name)} |",
            f"| 时长 | {_format_timestamp(overview.duration_us)} |",
            f"| 画幅 | {overview.width} × {overview.height} |",
            f"| 帧率 | {overview.frame_rate_numerator}/{overview.frame_rate_denominator} fps |",
            f"| 总结 | {_format_claim(overview.summary)} |",
            f"| 内容目标 | {_format_claim(overview.content_goal)} |",
            "",
            "### 剪辑识别特征",
            "",
            *_claim_bullets(overview.editing_identity),
            "",
            "## 二、BGM 分析数据",
            "",
            f"- 音频范围：`{content.bgm_analysis.audio_scope}`",
            f"- 音频时长：{_format_timestamp(content.bgm_analysis.duration_us)}",
            "",
            "### 速度候选",
            "",
        ]
        if content.bgm_analysis.tempo_hypotheses:
            for tempo in content.bgm_analysis.tempo_hypotheses:
                evidence = "、".join(tempo.evidence_refs) or "未关联"
                lines.append(
                    f"- {tempo.bpm:g} BPM（置信度 {tempo.confidence:.2f}，"
                    f"{_STATUS_LABELS[tempo.status]}；依据：{_format_claim(tempo.basis)}；"
                    f"证据：{_escape(evidence)}）"
                )
        else:
            lines.append("- 尚无速度候选。")

        lines.extend(
            [
                "",
                "### 音乐段落",
                "",
                "| 段落 | 时间 | 能量 | 音乐作用 | 能量走向 | 与剪辑关系 | 证据 |",
                "|---|---:|---:|---|---|---|---|",
            ]
        )
        for section in content.bgm_analysis.sections:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape(section.label),
                        _format_range(section.start_timestamp_us, section.end_timestamp_us),
                        f"{section.energy_level:.2f}",
                        _format_claim(section.musical_function),
                        _format_claim(section.energy_direction),
                        _format_claim(section.editing_relation),
                        _escape("、".join(section.evidence_refs)),
                    ]
                )
                + " |"
            )

        lines.extend(
            [
                "",
                "### 多层音乐理解",
                "",
                "#### 节奏层",
                *_claim_bullets(content.bgm_analysis.rhythm_layers),
                "",
                "#### 声音层变化",
                *_claim_bullets(content.bgm_analysis.sound_layer_changes),
                "",
                "#### 能量流动",
                *_claim_bullets(content.bgm_analysis.energy_flow),
                "",
                "#### 剪辑跟随层",
                *_claim_bullets(content.bgm_analysis.editing_following),
                "",
                "## 三、参考视频逐镜效果规划表",
                "",
                "| 镜头 | 时间戳 | 画面概括 | 图层 | 动作 | 效果 | 声音关系 | "
                "前后节奏作用 | 证据 |",
                "|---|---:|---|---|---|---|---|---|---|",
            ]
        )
        for shot in content.shot_analyses:
            layers = "<br>".join(
                f"{_escape(layer.layer_id)}／{_escape(layer.layer_type)}／z={layer.z_index}："
                f"{_format_claim(layer.description)}"
                for layer in shot.layers
            )
            actions = (
                "<br>".join(
                    f"{_escape(action.action_type)} "
                    f"({_format_range(action.start_timestamp_us, action.end_timestamp_us)})："
                    f"{_format_claim(action.description)}；音乐："
                    f"{_format_claim(action.timing_relation_to_music)}"
                    for action in shot.actions
                )
                or "—"
            )
            effects = (
                "<br>".join(
                    f"{_escape(effect.effect_type)}／{effect.target_scope}："
                    f"{_format_claim(effect.description)}"
                    for effect in shot.effects
                )
                or "—"
            )
            rhythm_role = (
                f"前：{_format_claim(shot.preceding_rhythm_role)}<br>"
                f"后：{_format_claim(shot.following_rhythm_role)}"
            )
            shot_evidence = sorted(collect_evidence_refs(shot))
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape(shot.shot_id),
                        _format_range(shot.start_timestamp_us, shot.end_timestamp_us),
                        _format_claim(shot.summary),
                        layers,
                        actions,
                        effects,
                        _format_claim(shot.sound_relation),
                        rhythm_role,
                        _escape("、".join(shot_evidence)),
                    ]
                )
                + " |"
            )

        grammar = content.editing_grammar
        lines.extend(
            [
                "",
                "## 四、剪辑语法与节奏单元",
                "",
                "| 节奏单元 | 时间 | 镜头 | 音乐模式 | 视觉序列 | 蓄力作用 | "
                "释放作用 | 可迁移规则 | 证据 |",
                "|---|---:|---|---|---|---|---|---|---|",
            ]
        )
        for unit in grammar.rhythm_units:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape(unit.name),
                        _format_range(unit.start_timestamp_us, unit.end_timestamp_us),
                        _escape("、".join(unit.shot_ids)),
                        _format_claim(unit.music_pattern),
                        "<br>".join(_format_claim(claim) for claim in unit.visual_sequence),
                        _format_claim(unit.build_up_role),
                        _format_claim(unit.release_role),
                        _format_claim(unit.transfer_rule),
                        _escape("、".join(sorted(collect_evidence_refs(unit)))),
                    ]
                )
                + " |"
            )

        for title, claims in (
            ("切换规律", grammar.cutting_rules),
            ("叠层规律", grammar.layering_rules),
            ("运动规律", grammar.motion_rules),
            ("转场规律", grammar.transition_rules),
            ("密度规律", grammar.density_rules),
            ("可复用编排模式", grammar.reusable_patterns),
        ):
            lines.extend(["", f"### {title}", "", *_claim_bullets(claims)])

        experience = content.viewing_experience
        lines.extend(
            [
                "",
                "## 五、观看体验与情绪目标",
                "",
                f"- 总体目标：{_format_claim(experience.overall_target)}",
                "",
                "### 情绪效果",
                *_claim_bullets(experience.emotional_effects),
                "",
                "### 节奏与呼吸",
                *_claim_bullets(experience.pacing_and_breathing),
                "",
                "### 丰富度与层次",
                *_claim_bullets(experience.richness_and_layering),
                "",
                "### 注意力引导",
                *_claim_bullets(experience.attention_guidance),
                "",
                "## 六、重新创作映射",
                "",
                f"- 核心目标：{_format_claim(content.creation_context_projection.core_goal)}",
                "",
                "### 可迁移规律",
                *_claim_bullets(content.creation_context_projection.transferable_patterns),
                "",
                "### 原视频特有信息",
                *_claim_bullets(content.creation_context_projection.non_transferable_specifics),
                "",
                "### 新素材重构指引",
                *_claim_bullets(
                    content.creation_context_projection.new_material_reconstruction_guidance
                ),
                "",
                "### 创作阶段知识映射",
                "",
                "| 阶段 | 知识类型 | 检索标签 | 建议 |",
                "|---|---|---|---|",
            ]
        )
        for projection in content.creation_context_projection.stage_projections:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _stage_label(projection.stage),
                        _escape("、".join(projection.knowledge_types)),
                        _escape("、".join(projection.retrieval_tags)),
                        "<br>".join(_format_claim(claim) for claim in projection.recommendations),
                    ]
                )
                + " |"
            )
        lines.extend(
            [
                "",
                "## 七、证据索引",
                "",
                *[f"- `{_escape(reference)}`" for reference in report.evidence_refs],
                "",
            ]
        )
        return "\n".join(lines)

    def generate(
        self,
        manifest: ReferenceReportManifest,
        artifact_store: ArtifactStore,
    ) -> GeneratedReferenceReport:
        validated = self._validator.validate(manifest)
        json_artifact = artifact_store.put_bytes(canonical_json_bytes(validated))
        markdown_artifact = artifact_store.put_text(self.render_markdown(validated))
        return GeneratedReferenceReport(
            manifest=validated,
            json_artifact=json_artifact,
            markdown_artifact=markdown_artifact,
        )


def _format_claim(claim: Claim) -> str:
    evidence = "、".join(claim.evidence_refs) or "未关联"
    return f"[{_STATUS_LABELS[claim.status]}] {_escape(claim.text)}（证据：{_escape(evidence)}）"


def _claim_bullets(claims: list[Claim]) -> list[str]:
    if not claims:
        return ["- 暂无条目。"]
    return [f"- {_format_claim(claim)}" for claim in claims]


def _format_timestamp(timestamp_us: int) -> str:
    total_milliseconds = timestamp_us // 1_000
    minutes, remainder = divmod(total_milliseconds, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def _format_range(start_timestamp_us: int, end_timestamp_us: int) -> str:
    return f"{_format_timestamp(start_timestamp_us)}–{_format_timestamp(end_timestamp_us)}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _stage_label(stage: str) -> str:
    return {
        "creative_direction": "创意方案",
        "resource_preparation": "资源筹备",
        "editing_specification": "剪辑规格",
    }[stage]
