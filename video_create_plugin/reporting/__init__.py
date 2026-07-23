"""参考视频结构化报告、校验与确定性产物生成。"""

from .generator import GeneratedReferenceReport, ReferenceReportGenerator
from .models import (
    BgmAnalysis,
    BgmSection,
    Claim,
    CreationContextProjection,
    EditAction,
    EditingGrammar,
    EffectObservation,
    ReferenceReportContent,
    ReferenceReportManifest,
    ReferenceShotAnalysis,
    RhythmUnit,
    StageKnowledgeProjection,
    TempoHypothesis,
    VideoOverview,
    ViewingExperience,
    VisualLayer,
    canonical_json_bytes,
    collect_evidence_refs,
)
from .validation import ReferenceReportValidationError, ReferenceReportValidator

__all__ = [
    "BgmAnalysis",
    "BgmSection",
    "Claim",
    "CreationContextProjection",
    "EditingGrammar",
    "EditAction",
    "EffectObservation",
    "GeneratedReferenceReport",
    "ReferenceReportContent",
    "ReferenceReportGenerator",
    "ReferenceReportManifest",
    "ReferenceReportValidationError",
    "ReferenceReportValidator",
    "ReferenceShotAnalysis",
    "RhythmUnit",
    "StageKnowledgeProjection",
    "TempoHypothesis",
    "VideoOverview",
    "ViewingExperience",
    "VisualLayer",
    "canonical_json_bytes",
    "collect_evidence_refs",
]
