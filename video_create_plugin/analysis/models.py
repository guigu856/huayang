from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any


class Status(StrEnum):
    """参考分析任务的持久化状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class StatusEvent:
    status: Status
    timestamp: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status.value,
            "timestamp": self.timestamp,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> StatusEvent:
        reason = payload.get("reason")
        return cls(
            status=Status(_required_string(payload, "status")),
            timestamp=_required_string(payload, "timestamp"),
            reason=reason if isinstance(reason, str) else None,
        )


@dataclass(frozen=True, slots=True)
class AnalysisFailure:
    code: str
    message: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AnalysisFailure:
        details = payload.get("details")
        return cls(
            code=_required_string(payload, "code"),
            message=_required_string(payload, "message"),
            details=details if isinstance(details, dict) else {},
        )


@dataclass(frozen=True, slots=True)
class ReferenceAnalysisResult:
    """成功任务的参考证据入口。"""

    job_id: str
    job_dir: Path
    source_sha256: str
    config_sha256: str
    reference_manifest_path: Path
    reference_manifest_sha256: str
    evidence_bundle_sha256: str
    visual_manifest_path: Path
    audio_manifest_path: Path | None
    has_audio: bool
    reused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_dir": str(self.job_dir),
            "source_sha256": self.source_sha256,
            "config_sha256": self.config_sha256,
            "reference_manifest_path": str(self.reference_manifest_path),
            "reference_manifest_sha256": self.reference_manifest_sha256,
            "evidence_bundle_sha256": self.evidence_bundle_sha256,
            "visual_manifest_path": str(self.visual_manifest_path),
            "audio_manifest_path": (
                str(self.audio_manifest_path) if self.audio_manifest_path is not None else None
            ),
            "has_audio": self.has_audio,
            "reused": self.reused,
        }

    def persisted_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload.pop("reused")
        return payload

    def as_reused(self) -> ReferenceAnalysisResult:
        return replace(self, reused=True)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ReferenceAnalysisResult:
        audio_path = payload.get("audio_manifest_path")
        has_audio = payload.get("has_audio")
        reused = payload.get("reused", False)
        if not isinstance(has_audio, bool) or not isinstance(reused, bool):
            raise ValueError("分析结果布尔字段无效")
        return cls(
            job_id=_required_string(payload, "job_id"),
            job_dir=Path(_required_string(payload, "job_dir")),
            source_sha256=_required_string(payload, "source_sha256"),
            config_sha256=_required_string(payload, "config_sha256"),
            reference_manifest_path=Path(_required_string(payload, "reference_manifest_path")),
            reference_manifest_sha256=_required_string(payload, "reference_manifest_sha256"),
            evidence_bundle_sha256=_required_string(payload, "evidence_bundle_sha256"),
            visual_manifest_path=Path(_required_string(payload, "visual_manifest_path")),
            audio_manifest_path=(Path(audio_path) if isinstance(audio_path, str) else None),
            has_audio=has_audio,
            reused=reused,
        )


@dataclass(frozen=True, slots=True)
class AnalysisJob:
    """磁盘中的参考分析任务快照。"""

    job_id: str
    source_path: Path
    source_sha256: str
    config_sha256: str
    status: Status
    created_at: str
    updated_at: str
    status_history: tuple[StatusEvent, ...]
    result: ReferenceAnalysisResult | None = None
    failure: AnalysisFailure | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "job_id": self.job_id,
            "source_path": str(self.source_path),
            "source_sha256": self.source_sha256,
            "config_sha256": self.config_sha256,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status_history": [event.to_dict() for event in self.status_history],
            "result": self.result.persisted_dict() if self.result else None,
            "failure": self.failure.to_dict() if self.failure else None,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AnalysisJob:
        raw_history = payload.get("status_history")
        if not isinstance(raw_history, list):
            raise ValueError("status_history 无效")
        history = tuple(
            StatusEvent.from_dict(item) for item in raw_history if isinstance(item, dict)
        )
        if len(history) != len(raw_history) or not history:
            raise ValueError("status_history 无效")
        result_payload = payload.get("result")
        failure_payload = payload.get("failure")
        result = (
            ReferenceAnalysisResult.from_dict(result_payload)
            if isinstance(result_payload, dict)
            else None
        )
        failure = (
            AnalysisFailure.from_dict(failure_payload)
            if isinstance(failure_payload, dict)
            else None
        )
        job = cls(
            job_id=_required_string(payload, "job_id"),
            source_path=Path(_required_string(payload, "source_path")),
            source_sha256=_required_string(payload, "source_sha256"),
            config_sha256=_required_string(payload, "config_sha256"),
            status=Status(_required_string(payload, "status")),
            created_at=_required_string(payload, "created_at"),
            updated_at=_required_string(payload, "updated_at"),
            status_history=history,
            result=result,
            failure=failure,
        )
        if history[-1].status is not job.status:
            raise ValueError("当前状态与状态历史不一致")
        if job.status is Status.SUCCEEDED and job.result is None:
            raise ValueError("成功任务缺少结果")
        return job


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} 无效")
    return value
