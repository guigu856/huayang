from __future__ import annotations

import builtins
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from components.audio_analysis import (
    AudioAnalysisError,
    AudioAnalysisResult,
    AudioAnalysisService,
)
from components.video_analysis import (
    VideoAnalysisError,
    VideoAnalysisResult,
    VideoAnalysisService,
)

from ..errors import PluginError
from .models import (
    AnalysisFailure,
    AnalysisJob,
    ReferenceAnalysisResult,
    Status,
    StatusEvent,
)

SCHEMA_VERSION = "1.0"
ALGORITHM_VERSION = "reference-analysis-job-v1.0.0"
_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_REQUIRED_AUDIO_KINDS = {
    "media_probe",
    "audio_signals",
    "energy_curve",
    "spectral_flux",
    "transient_candidates",
    "silence_regions",
    "tempo_candidates",
    "beat_grid",
    "section_candidates",
}


class AnalysisJobError(PluginError):
    """参考分析应用服务的稳定错误。"""


class ReferenceAnalysisService:
    """组合视觉与音频证据组件，并维护可恢复的分析任务。"""

    def __init__(
        self,
        root: Path,
        *,
        video_service: VideoAnalysisService | None = None,
        audio_service: AudioAnalysisService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.video_service = video_service or VideoAnalysisService()
        self.audio_service = audio_service or AudioAnalysisService()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        try:
            self.root = Path(root).resolve()
            self.jobs_root = self.root / "jobs"
            self.jobs_root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise AnalysisJobError(
                "job_store_unavailable", f"分析任务目录初始化失败：{error}"
            ) from error
        self._interrupt_running_jobs()

    def start(self, source: Path) -> ReferenceAnalysisResult:
        """同步执行任务；持久化状态仍完整经过排队、运行和终态。"""

        source_path = self._resolve_source(source)
        source_sha256 = _sha256(source_path)
        config_payload = self._config_payload()
        config_sha256 = _canonical_sha256(config_payload)
        job_id = _job_id(source_sha256, config_sha256)

        with self._lock:
            existing = self._load_job_if_present(job_id)
            if existing is not None and existing.status is Status.SUCCEEDED:
                try:
                    return self.validate(job_id).as_reused()
                except AnalysisJobError as error:
                    raise AnalysisJobError(
                        "cached_analysis_invalid",
                        "已有分析任务的证据校验失败",
                        details={"job_id": job_id, "cause": error.as_dict()},
                    ) from error

            now = self._now()
            if existing is None:
                job = AnalysisJob(
                    job_id=job_id,
                    source_path=source_path,
                    source_sha256=source_sha256,
                    config_sha256=config_sha256,
                    status=Status.QUEUED,
                    created_at=now,
                    updated_at=now,
                    status_history=(StatusEvent(Status.QUEUED, now),),
                )
            else:
                job = self._transition(
                    existing,
                    Status.QUEUED,
                    reason="retry",
                    source_path=source_path,
                    result=None,
                    failure=None,
                )
            self._write_job(job)
            job = self._transition(job, Status.RUNNING)
            self._write_job(job)

            try:
                result = self._execute(job, config_payload)
            except (AnalysisJobError, VideoAnalysisError, AudioAnalysisError) as error:
                failure = self._normalize_failure(error)
                failed = self._transition(job, Status.FAILED, failure=failure)
                self._write_job(failed)
                if isinstance(error, AnalysisJobError):
                    raise
                raise AnalysisJobError(
                    failure.code,
                    failure.message,
                    details=failure.details,
                ) from error
            except Exception as error:
                failure = AnalysisFailure(
                    code="analysis_execution_failed",
                    message=f"参考分析执行失败：{error}",
                    details={"job_id": job_id},
                )
                self._write_job(self._transition(job, Status.FAILED, failure=failure))
                raise AnalysisJobError(
                    failure.code, failure.message, details=failure.details
                ) from error

            succeeded = self._transition(job, Status.SUCCEEDED, result=result)
            self._write_job(succeeded)
            return self.validate(job_id)

    def get(self, job_id: str) -> AnalysisJob:
        with self._lock:
            return self._load_job(job_id)

    def list(self) -> tuple[AnalysisJob, ...]:
        with self._lock:
            jobs: builtins.list[AnalysisJob] = []
            try:
                paths = sorted(self.jobs_root.glob("*/job.json"))
            except OSError as error:
                raise AnalysisJobError(
                    "job_store_unavailable", f"分析任务目录读取失败：{error}"
                ) from error
            for path in paths:
                jobs.append(self._read_job(path))
            return tuple(sorted(jobs, key=lambda job: (job.created_at, job.job_id)))

    def validate(self, job_id: str) -> ReferenceAnalysisResult:
        """验证汇总清单、文件哈希和媒体时间戳证据。"""

        with self._lock:
            job = self._load_job(job_id)
            if job.status is not Status.SUCCEEDED or job.result is None:
                raise AnalysisJobError(
                    "job_not_succeeded",
                    "分析任务尚未产出可验证结果",
                    details={"job_id": job_id, "status": job.status.value},
                )
            result = job.result
            manifest_path = self._contained_path(result.job_dir, result.reference_manifest_path)
            if not manifest_path.is_file():
                raise AnalysisJobError(
                    "reference_manifest_missing",
                    "参考分析汇总清单缺失",
                    details={"path": str(manifest_path)},
                )
            actual_manifest_sha = _sha256(manifest_path)
            if actual_manifest_sha != result.reference_manifest_sha256:
                raise AnalysisJobError(
                    "reference_manifest_hash_mismatch",
                    "参考分析汇总清单哈希不匹配",
                    details={"path": str(manifest_path)},
                )
            manifest = _read_json(manifest_path, "reference_manifest_invalid")
            self._validate_manifest_identity(job, manifest)
            bundle = _required_dict(manifest, "evidence_bundle")
            entries = _required_dict_list(bundle, "entries")
            bundle_sha = _bundle_sha256(entries)
            if bundle.get("sha256") != bundle_sha:
                raise AnalysisJobError(
                    "evidence_bundle_hash_mismatch",
                    "证据包汇总哈希不匹配",
                )
            if bundle_sha != result.evidence_bundle_sha256:
                raise AnalysisJobError(
                    "job_result_hash_mismatch",
                    "任务结果记录与证据包哈希不一致",
                )
            entries_by_path = self._validate_bundle_entries(result.job_dir, entries)

            visual = _required_dict(manifest, "visual_analysis")
            visual_manifest = self._component_manifest(
                result.job_dir,
                visual,
                entries_by_path,
                expected_source_sha=job.source_sha256,
            )
            self._validate_visual_timestamps(result.job_dir, visual, visual_manifest)
            has_audio = self._visual_probe_has_audio(result.job_dir, visual)
            if manifest.get("has_audio") is not has_audio or result.has_audio is not has_audio:
                raise AnalysisJobError(
                    "stream_inventory_mismatch",
                    "汇总清单中的音轨状态与真实媒体探测不一致",
                )

            audio = _required_dict(manifest, "audio_analysis")
            if has_audio:
                if audio.get("status") != "succeeded":
                    raise AnalysisJobError(
                        "audio_evidence_incomplete", "媒体包含音轨但缺少音频分析结果"
                    )
                audio_manifest = self._component_manifest(
                    result.job_dir,
                    audio,
                    entries_by_path,
                    expected_source_sha=job.source_sha256,
                )
                kinds = {
                    item.get("kind") for item in _required_dict_list(audio_manifest, "artifacts")
                }
                if not _REQUIRED_AUDIO_KINDS.issubset(kinds):
                    raise AnalysisJobError(
                        "audio_evidence_incomplete",
                        "音频分析证据种类不完整",
                        details={"missing": sorted(_REQUIRED_AUDIO_KINDS - kinds)},
                    )
                self._validate_audio_timestamps(result.job_dir, audio, audio_manifest)
            elif audio.get("status") != "not_present":
                raise AnalysisJobError("stream_inventory_mismatch", "无音轨媒体的音频分析状态无效")

            refinements = manifest.get("refinements", [])
            if not isinstance(refinements, list):
                raise AnalysisJobError("reference_manifest_invalid", "refinements 结构无效")
            for refinement in refinements:
                if not isinstance(refinement, dict):
                    raise AnalysisJobError("reference_manifest_invalid", "refinement 结构无效")
                self._validate_refinement(result.job_dir, refinement, entries_by_path)
            return result

    def refine_intervals(
        self,
        job_id: str,
        intervals: Sequence[tuple[int, int]],
        *,
        max_interval_us: int = 100_000,
    ) -> Path:
        """按真实视频帧 PTS 生成独立的密集抽帧证据。"""

        normalized = _normalize_intervals(intervals, max_interval_us)
        with self._lock:
            result = self.validate(job_id)
            job = self._load_job(job_id)
            frame_records = _read_jsonl(
                result.job_dir / "visual" / "frame_index.jsonl",
                "timestamp_evidence_invalid",
            )
            selections = self._select_dense_frames(frame_records, normalized, max_interval_us)
            refinement_key = {
                "algorithm_version": ALGORITHM_VERSION,
                "intervals": normalized,
                "max_interval_us": max_interval_us,
            }
            refinement_id = f"dense_{_canonical_sha256(refinement_key)[:20]}"
            output_dir = result.job_dir / "refinements" / refinement_id
            manifest_path = output_dir / "evidence_manifest.json"

            if not manifest_path.is_file():
                self._extract_dense_frames(
                    job.source_path,
                    output_dir,
                    selections,
                )
                payload = self._dense_manifest_payload(
                    job,
                    output_dir,
                    refinement_id,
                    normalized,
                    max_interval_us,
                    selections,
                )
                _write_json_atomic(manifest_path, payload)

            self._attach_refinement(job, manifest_path, refinement_id)
            self.validate(job_id)
            return manifest_path

    def _execute(self, job: AnalysisJob, config_payload: dict[str, Any]) -> ReferenceAnalysisResult:
        job_dir = self._job_dir(job.job_id)
        visual_result = self.video_service.analyze(job.source_path, job_dir / "visual")
        has_audio = self._probe_result_has_audio(visual_result)
        audio_result: AudioAnalysisResult | None
        try:
            audio_result = self.audio_service.analyze(job.source_path, job_dir / "audio")
        except AudioAnalysisError as error:
            if error.code != "audio_stream_not_found" or has_audio:
                raise
            audio_result = None
        if has_audio and audio_result is None:
            raise AnalysisJobError("audio_evidence_incomplete", "媒体包含音轨但音频分析结果缺失")

        entries = self._collect_component_entries(job_dir, "visual", visual_result)
        if audio_result is not None:
            entries.extend(self._collect_component_entries(job_dir, "audio", audio_result))
        entries.sort(key=lambda item: _required_string(item, "path"))
        bundle_sha = _bundle_sha256(entries)
        manifest_path = job_dir / "reference_manifest.json"
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "job_id": job.job_id,
            "source": {
                "path": str(job.source_path),
                "sha256": job.source_sha256,
                "size_bytes": job.source_path.stat().st_size,
            },
            "configuration": {
                "sha256": job.config_sha256,
                "value": config_payload,
            },
            "has_audio": has_audio,
            "visual_analysis": {
                "status": "succeeded",
                "algorithm_version": visual_result.algorithm_version,
                "manifest_path": _relative(job_dir, visual_result.evidence_manifest_path),
                "manifest_sha256": visual_result.manifest_sha256,
            },
            "audio_analysis": (
                {
                    "status": "succeeded",
                    "algorithm_version": audio_result.algorithm_version,
                    "audio_scope": audio_result.audio_scope,
                    "manifest_path": _relative(job_dir, audio_result.evidence_manifest_path),
                    "manifest_sha256": audio_result.manifest_sha256,
                }
                if audio_result is not None
                else {"status": "not_present"}
            ),
            "refinements": [],
            "evidence_bundle": {
                "hash_algorithm": "sha256-canonical-json-v1",
                "entries": entries,
                "sha256": bundle_sha,
            },
        }
        _write_json_atomic(manifest_path, manifest)
        return ReferenceAnalysisResult(
            job_id=job.job_id,
            job_dir=job_dir,
            source_sha256=job.source_sha256,
            config_sha256=job.config_sha256,
            reference_manifest_path=manifest_path,
            reference_manifest_sha256=_sha256(manifest_path),
            evidence_bundle_sha256=bundle_sha,
            visual_manifest_path=visual_result.evidence_manifest_path,
            audio_manifest_path=(
                audio_result.evidence_manifest_path if audio_result is not None else None
            ),
            has_audio=has_audio,
        )

    def _collect_component_entries(
        self,
        job_dir: Path,
        label: str,
        result: VideoAnalysisResult | AudioAnalysisResult,
    ) -> builtins.list[dict[str, Any]]:
        manifest = _read_json(result.evidence_manifest_path, "component_manifest_invalid")
        output_dir = result.output_dir
        entries: builtins.list[dict[str, Any]] = [
            _artifact_entry(
                job_dir,
                result.evidence_manifest_path,
                f"{label}_evidence_manifest",
                result.algorithm_version,
            )
        ]
        for item in _required_dict_list(manifest, "artifacts"):
            relative_path = _safe_relative(_required_string(item, "path"))
            path = self._contained_path(output_dir, output_dir / relative_path)
            self._verify_declared_artifact(path, item)
            entries.append(
                _artifact_entry(
                    job_dir,
                    path,
                    f"{label}:{_required_string(item, 'kind')}",
                    _required_string(item, "algorithm_version"),
                )
            )
        return entries

    def _component_manifest(
        self,
        job_dir: Path,
        descriptor: dict[str, Any],
        bundle_entries: dict[str, dict[str, Any]],
        *,
        expected_source_sha: str,
    ) -> dict[str, Any]:
        relative = _safe_relative(_required_string(descriptor, "manifest_path"))
        manifest_path = self._contained_path(job_dir, job_dir / relative)
        entry = bundle_entries.get(relative.as_posix())
        if entry is None or entry.get("sha256") != descriptor.get("manifest_sha256"):
            raise AnalysisJobError(
                "component_manifest_hash_mismatch",
                "组件证据清单未被证据包正确引用",
                details={"path": relative.as_posix()},
            )
        manifest = _read_json(manifest_path, "component_manifest_invalid")
        source = _required_dict(manifest, "source")
        if source.get("sha256") != expected_source_sha:
            raise AnalysisJobError("component_source_mismatch", "组件证据来源哈希不一致")
        base_dir = manifest_path.parent
        for artifact in _required_dict_list(manifest, "artifacts"):
            artifact_relative = _safe_relative(_required_string(artifact, "path"))
            artifact_path = self._contained_path(base_dir, base_dir / artifact_relative)
            bundle_relative = _relative(job_dir, artifact_path)
            bundled = bundle_entries.get(bundle_relative)
            if bundled is None or bundled.get("sha256") != artifact.get("sha256"):
                raise AnalysisJobError(
                    "component_artifact_unbundled",
                    "组件产物未被证据包正确引用",
                    details={"path": bundle_relative},
                )
        return manifest

    def _validate_bundle_entries(
        self, job_dir: Path, entries: builtins.list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        entries_by_path: dict[str, dict[str, Any]] = {}
        for entry in entries:
            relative = _safe_relative(_required_string(entry, "path"))
            key = relative.as_posix()
            if key in entries_by_path:
                raise AnalysisJobError("reference_manifest_invalid", "证据包包含重复路径")
            path = self._contained_path(job_dir, job_dir / relative)
            self._verify_declared_artifact(path, entry)
            entries_by_path[key] = entry
        return entries_by_path

    def _validate_manifest_identity(self, job: AnalysisJob, manifest: dict[str, Any]) -> None:
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise AnalysisJobError("reference_manifest_invalid", "参考分析汇总清单版本无效")
        if manifest.get("job_id") != job.job_id:
            raise AnalysisJobError("reference_manifest_invalid", "参考分析汇总清单任务标识无效")
        source = _required_dict(manifest, "source")
        configuration = _required_dict(manifest, "configuration")
        if source.get("sha256") != job.source_sha256:
            raise AnalysisJobError("reference_manifest_invalid", "参考分析来源哈希无效")
        if configuration.get("sha256") != job.config_sha256:
            raise AnalysisJobError("reference_manifest_invalid", "参考分析配置哈希无效")
        if _canonical_sha256(configuration.get("value")) != job.config_sha256:
            raise AnalysisJobError("reference_manifest_invalid", "参考分析配置内容与哈希不一致")

    def _validate_visual_timestamps(
        self,
        job_dir: Path,
        descriptor: dict[str, Any],
        manifest: dict[str, Any],
    ) -> None:
        manifest_path = job_dir / _safe_relative(_required_string(descriptor, "manifest_path"))
        frame_entry = _find_artifact(manifest, "frame_index")
        frames = _read_jsonl(
            manifest_path.parent / _safe_relative(_required_string(frame_entry, "path")),
            "timestamp_evidence_invalid",
        )
        if not frames:
            raise AnalysisJobError("timestamp_evidence_invalid", "视觉帧时间戳证据为空")
        previous: int | None = None
        for expected_index, frame in enumerate(frames):
            frame_index = _required_int(frame, "frame_index")
            pts = _required_int(frame, "pts")
            timestamp_us = _required_int(frame, "timestamp_us")
            time_base = _positive_fraction(_required_string(frame, "time_base"))
            if frame_index != expected_index:
                raise AnalysisJobError("timestamp_evidence_invalid", "视觉帧序号不连续")
            if timestamp_us != round(Fraction(pts) * time_base * 1_000_000):
                raise AnalysisJobError(
                    "timestamp_evidence_invalid", "视觉帧时间戳并非由真实 PTS 推导"
                )
            if previous is not None and timestamp_us < previous:
                raise AnalysisJobError("timestamp_evidence_invalid", "视觉帧时间戳不是单调递增")
            previous = timestamp_us

    def _validate_audio_timestamps(
        self,
        job_dir: Path,
        descriptor: dict[str, Any],
        manifest: dict[str, Any],
    ) -> None:
        manifest_path = job_dir / _safe_relative(_required_string(descriptor, "manifest_path"))
        base = manifest_path.parent
        media_path = base / _safe_relative(
            _required_string(_find_artifact(manifest, "media_probe"), "path")
        )
        media = _read_json(media_path, "timestamp_evidence_invalid")
        timeline = _required_dict(media, "timeline")
        decode = _required_dict(media, "decode")
        sample_rate = _required_int(decode, "sample_rate")
        if sample_rate <= 0:
            raise AnalysisJobError("timestamp_evidence_invalid", "音频采样率无效")
        start_timestamp_us = _required_int(timeline, "start_timestamp_us")
        start_pts = timeline.get("start_pts")
        if start_pts is not None:
            if isinstance(start_pts, bool) or not isinstance(start_pts, int):
                raise AnalysisJobError("timestamp_evidence_invalid", "音频 start_pts 无效")
            time_base = _positive_fraction(_required_string(timeline, "time_base"))
            if start_timestamp_us != round(Fraction(start_pts) * time_base * 1_000_000):
                raise AnalysisJobError("timestamp_evidence_invalid", "音频起点并非由真实 PTS 推导")

        def validate_sample_timestamp(
            record: dict[str, Any], sample_key: str, timestamp_key: str
        ) -> None:
            sample_index = _required_int(record, sample_key)
            timestamp_us = _required_int(record, timestamp_key)
            expected = start_timestamp_us + round(Fraction(sample_index, sample_rate) * 1_000_000)
            if timestamp_us != expected:
                raise AnalysisJobError(
                    "timestamp_evidence_invalid",
                    "音频时间戳并非由真实采样位置推导",
                    details={"sample_key": sample_key, "timestamp_key": timestamp_key},
                )

        for kind, collection_key in (
            ("energy_curve", "frames"),
            ("spectral_flux", "frames"),
            ("transient_candidates", "candidates"),
        ):
            artifact = _find_artifact(manifest, kind)
            payload = _read_json(
                base / _safe_relative(_required_string(artifact, "path")),
                "timestamp_evidence_invalid",
            )
            records = payload.get(collection_key)
            if not isinstance(records, list):
                raise AnalysisJobError("timestamp_evidence_invalid", f"{kind} 时间戳记录无效")
            for record in records:
                if not isinstance(record, dict):
                    raise AnalysisJobError("timestamp_evidence_invalid", f"{kind} 时间戳记录无效")
                validate_sample_timestamp(record, "sample_index", "timestamp_us")

        for kind, collection_key in (
            ("silence_regions", "regions"),
            ("beat_grid", "beats"),
        ):
            artifact = _find_artifact(manifest, kind)
            payload = _read_json(
                base / _safe_relative(_required_string(artifact, "path")),
                "timestamp_evidence_invalid",
            )
            records = payload.get(collection_key)
            if not isinstance(records, list):
                raise AnalysisJobError("timestamp_evidence_invalid", f"{kind} 时间戳记录无效")
            for record in records:
                if not isinstance(record, dict):
                    raise AnalysisJobError("timestamp_evidence_invalid", f"{kind} 时间戳记录无效")
                if kind == "silence_regions":
                    validate_sample_timestamp(record, "start_sample_index", "start_timestamp_us")
                    validate_sample_timestamp(record, "end_sample_index", "end_timestamp_us")
                else:
                    validate_sample_timestamp(record, "sample_index", "timestamp_us")

        section_artifact = _find_artifact(manifest, "section_candidates")
        sections = _read_json(
            base / _safe_relative(_required_string(section_artifact, "path")),
            "timestamp_evidence_invalid",
        )
        for boundary in _required_dict_list(sections, "boundaries"):
            validate_sample_timestamp(boundary, "sample_index", "timestamp_us")
        for section in _required_dict_list(sections, "sections"):
            validate_sample_timestamp(section, "start_sample_index", "start_timestamp_us")
            validate_sample_timestamp(section, "end_sample_index", "end_timestamp_us")

    def _visual_probe_has_audio(self, job_dir: Path, descriptor: dict[str, Any]) -> bool:
        manifest_path = job_dir / _safe_relative(_required_string(descriptor, "manifest_path"))
        manifest = _read_json(manifest_path, "component_manifest_invalid")
        probe_entry = _find_artifact(manifest, "media_probe")
        probe = _read_json(
            manifest_path.parent / _safe_relative(_required_string(probe_entry, "path")),
            "component_manifest_invalid",
        )
        streams = _required_dict(probe, "ffprobe").get("streams")
        if not isinstance(streams, list):
            raise AnalysisJobError("component_manifest_invalid", "媒体探测缺少 streams")
        return any(
            isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
        )

    def _probe_result_has_audio(self, result: VideoAnalysisResult) -> bool:
        probe = _read_json(result.media_probe_path, "component_manifest_invalid")
        streams = _required_dict(probe, "ffprobe").get("streams")
        if not isinstance(streams, list):
            raise AnalysisJobError("component_manifest_invalid", "媒体探测缺少 streams")
        return any(
            isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
        )

    def _select_dense_frames(
        self,
        frame_records: builtins.list[dict[str, Any]],
        intervals: tuple[tuple[int, int], ...],
        max_interval_us: int,
    ) -> builtins.list[tuple[tuple[int, int], builtins.list[dict[str, Any]]]]:
        if not frame_records:
            raise AnalysisJobError("timestamp_evidence_invalid", "视觉帧时间戳证据为空")
        selections: builtins.list[tuple[tuple[int, int], builtins.list[dict[str, Any]]]] = []
        for interval in intervals:
            start, end = interval
            selected = [
                frame
                for frame in frame_records
                if start <= _required_int(frame, "timestamp_us") <= end
            ]
            if not selected:
                raise AnalysisJobError(
                    "refinement_interval_out_of_range",
                    "抽帧区间内没有真实视频帧",
                    details={"start_timestamp_us": start, "end_timestamp_us": end},
                )
            timestamps = [_required_int(frame, "timestamp_us") for frame in selected]
            coverage_gaps = [timestamps[0] - start]
            coverage_gaps.extend(right - left for left, right in zip(timestamps, timestamps[1:]))
            coverage_gaps.append(end - timestamps[-1])
            observed = max(coverage_gaps)
            if observed > max_interval_us:
                raise AnalysisJobError(
                    "refinement_density_unavailable",
                    "真实视频帧间距超过请求的密集证据上限",
                    details={
                        "start_timestamp_us": start,
                        "end_timestamp_us": end,
                        "maximum_observed_gap_us": observed,
                        "requested_max_interval_us": max_interval_us,
                    },
                )
            selections.append((interval, selected))
        return selections

    def _extract_dense_frames(
        self,
        source: Path,
        output_dir: Path,
        selections: builtins.list[tuple[tuple[int, int], builtins.list[dict[str, Any]]]],
    ) -> None:
        unique_indexes = sorted(
            {_required_int(frame, "frame_index") for _, frames in selections for frame in frames}
        )
        if not unique_indexes:
            raise AnalysisJobError("refinement_failed", "密集抽帧没有选中视频帧")
        frame_dir = output_dir / "frames"
        try:
            frame_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise AnalysisJobError(
                "output_unavailable", f"密集抽帧目录创建失败：{error}"
            ) from error
        filter_value = "select=" + "+".join(
            f"between(n\\,{start}\\,{end})" for start, end in _contiguous_ranges(unique_indexes)
        )
        argv = [
            self.video_service.config.ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            filter_value,
            "-fps_mode",
            "vfr",
            "-q:v",
            "2",
            str(frame_dir / "%06d.jpg"),
        ]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.video_service.config.subprocess_timeout_seconds,
                check=False,
            )
        except FileNotFoundError as error:
            raise AnalysisJobError("dependency_missing", f"未找到 {argv[0]}") from error
        except subprocess.TimeoutExpired as error:
            raise AnalysisJobError("refinement_timeout", "密集抽帧执行超时") from error
        except OSError as error:
            raise AnalysisJobError("refinement_failed", f"密集抽帧执行失败：{error}") from error
        if completed.returncode != 0:
            raise AnalysisJobError(
                "refinement_failed",
                "FFmpeg 密集抽帧失败",
                details={
                    "returncode": completed.returncode,
                    "stderr": completed.stderr[-2_000:],
                },
            )
        images = sorted(frame_dir.glob("*.jpg"))
        if len(images) != len(unique_indexes):
            raise AnalysisJobError(
                "refinement_frame_count_mismatch",
                "密集抽帧图片数与真实帧索引数不一致",
                details={"expected": len(unique_indexes), "actual": len(images)},
            )

    def _dense_manifest_payload(
        self,
        job: AnalysisJob,
        output_dir: Path,
        refinement_id: str,
        intervals: tuple[tuple[int, int], ...],
        max_interval_us: int,
        selections: builtins.list[tuple[tuple[int, int], builtins.list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        indexes = sorted(
            {_required_int(frame, "frame_index") for _, frames in selections for frame in frames}
        )
        image_paths = sorted((output_dir / "frames").glob("*.jpg"))
        image_by_index = dict(zip(indexes, image_paths, strict=True))
        interval_payloads: builtins.list[dict[str, Any]] = []
        for interval_index, ((start, end), selected) in enumerate(selections, start=1):
            timestamps = [_required_int(frame, "timestamp_us") for frame in selected]
            gaps = [timestamps[0] - start]
            gaps.extend(right - left for left, right in zip(timestamps, timestamps[1:]))
            gaps.append(end - timestamps[-1])
            interval_payloads.append(
                {
                    "interval_id": f"interval_{interval_index:04d}",
                    "start_timestamp_us": start,
                    "end_timestamp_us": end,
                    "maximum_observed_gap_us": max(gaps),
                    "samples": [
                        {
                            "frame_index": _required_int(frame, "frame_index"),
                            "pts": _required_int(frame, "pts"),
                            "time_base": _required_string(frame, "time_base"),
                            "timestamp_us": _required_int(frame, "timestamp_us"),
                            "path": _relative(
                                output_dir,
                                image_by_index[_required_int(frame, "frame_index")],
                            ),
                            "sha256": _sha256(image_by_index[_required_int(frame, "frame_index")]),
                        }
                        for frame in selected
                    ],
                }
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": f"{ALGORITHM_VERSION}:dense-frame-extraction-v1",
            "refinement_id": refinement_id,
            "source": {"path": str(job.source_path), "sha256": job.source_sha256},
            "sampling": {
                "timestamp_source": "visual_frame_index_pts",
                "max_interval_us": max_interval_us,
                "intervals": interval_payloads,
            },
            "artifacts": [
                _artifact_entry(
                    output_dir,
                    path,
                    "dense_candidate_frame",
                    f"{ALGORITHM_VERSION}:dense-frame-extraction-v1",
                )
                for path in image_paths
            ],
        }

    def _attach_refinement(self, job: AnalysisJob, manifest_path: Path, refinement_id: str) -> None:
        if job.result is None:
            raise AnalysisJobError("job_not_succeeded", "分析任务缺少结果")
        result = job.result
        reference = _read_json(result.reference_manifest_path, "reference_manifest_invalid")
        refinements = reference.get("refinements")
        if not isinstance(refinements, list):
            raise AnalysisJobError("reference_manifest_invalid", "refinements 结构无效")
        descriptor = {
            "refinement_id": refinement_id,
            "manifest_path": _relative(result.job_dir, manifest_path),
            "manifest_sha256": _sha256(manifest_path),
        }
        refinements = [
            item
            for item in refinements
            if not isinstance(item, dict) or item.get("refinement_id") != refinement_id
        ]
        refinements.append(descriptor)
        refinements.sort(key=lambda item: str(item.get("refinement_id", "")))
        reference["refinements"] = refinements

        bundle = _required_dict(reference, "evidence_bundle")
        entries = _required_dict_list(bundle, "entries")
        prefix = f"refinements/{refinement_id}/"
        entries = [
            entry for entry in entries if not _required_string(entry, "path").startswith(prefix)
        ]
        refinement = _read_json(manifest_path, "refinement_manifest_invalid")
        algorithm = _required_string(refinement, "algorithm_version")
        entries.append(
            _artifact_entry(
                result.job_dir,
                manifest_path,
                "dense_refinement_manifest",
                algorithm,
            )
        )
        for artifact in _required_dict_list(refinement, "artifacts"):
            relative = _safe_relative(_required_string(artifact, "path"))
            path = self._contained_path(manifest_path.parent, manifest_path.parent / relative)
            entries.append(
                _artifact_entry(
                    result.job_dir,
                    path,
                    "refinement:dense_candidate_frame",
                    algorithm,
                )
            )
        entries.sort(key=lambda item: _required_string(item, "path"))
        bundle_sha = _bundle_sha256(entries)
        reference["evidence_bundle"] = {
            "hash_algorithm": "sha256-canonical-json-v1",
            "entries": entries,
            "sha256": bundle_sha,
        }
        _write_json_atomic(result.reference_manifest_path, reference)
        updated_result = replace(
            result,
            reference_manifest_sha256=_sha256(result.reference_manifest_path),
            evidence_bundle_sha256=bundle_sha,
            reused=False,
        )
        updated_job = replace(job, result=updated_result, updated_at=self._now())
        self._write_job(updated_job)

    def _validate_refinement(
        self,
        job_dir: Path,
        descriptor: dict[str, Any],
        entries: dict[str, dict[str, Any]],
    ) -> None:
        relative = _safe_relative(_required_string(descriptor, "manifest_path"))
        entry = entries.get(relative.as_posix())
        if entry is None or entry.get("sha256") != descriptor.get("manifest_sha256"):
            raise AnalysisJobError(
                "refinement_manifest_hash_mismatch",
                "密集抽帧清单未被证据包正确引用",
            )
        manifest_path = self._contained_path(job_dir, job_dir / relative)
        manifest = _read_json(manifest_path, "refinement_manifest_invalid")
        sampling = _required_dict(manifest, "sampling")
        maximum = _required_int(sampling, "max_interval_us")
        if maximum <= 0 or maximum > 100_000:
            raise AnalysisJobError("refinement_manifest_invalid", "密集抽帧间隔上限无效")
        intervals = _required_dict_list(sampling, "intervals")
        for interval in intervals:
            if _required_int(interval, "maximum_observed_gap_us") > maximum:
                raise AnalysisJobError("refinement_density_invalid", "密集抽帧证据间距超过清单上限")
            previous: int | None = None
            for sample in _required_dict_list(interval, "samples"):
                pts = _required_int(sample, "pts")
                timestamp_us = _required_int(sample, "timestamp_us")
                time_base = _positive_fraction(_required_string(sample, "time_base"))
                if timestamp_us != round(Fraction(pts) * time_base * 1_000_000):
                    raise AnalysisJobError(
                        "timestamp_evidence_invalid",
                        "密集抽帧时间戳并非由真实 PTS 推导",
                    )
                if previous is not None and timestamp_us - previous > maximum:
                    raise AnalysisJobError(
                        "refinement_density_invalid", "密集抽帧证据间距超过清单上限"
                    )
                previous = timestamp_us

    def _config_payload(self) -> dict[str, Any]:
        return {
            "algorithm_version": ALGORITHM_VERSION,
            "visual": asdict(self.video_service.config),
            "audio": asdict(self.audio_service.config),
        }

    def _transition(
        self,
        job: AnalysisJob,
        status: Status,
        *,
        reason: str | None = None,
        source_path: Path | None = None,
        result: ReferenceAnalysisResult | None = None,
        failure: AnalysisFailure | None = None,
    ) -> AnalysisJob:
        now = self._now()
        return replace(
            job,
            source_path=source_path or job.source_path,
            status=status,
            updated_at=now,
            status_history=job.status_history + (StatusEvent(status, now, reason),),
            result=result,
            failure=failure,
        )

    def _interrupt_running_jobs(self) -> None:
        with self._lock:
            for path in sorted(self.jobs_root.glob("*/job.json")):
                job = self._read_job(path)
                if job.status is Status.RUNNING:
                    interrupted = self._transition(
                        job, Status.INTERRUPTED, reason="service_restart"
                    )
                    self._write_job(interrupted)

    def _write_job(self, job: AnalysisJob) -> None:
        _write_json_atomic(self._job_path(job.job_id), job.to_dict())

    def _load_job_if_present(self, job_id: str) -> AnalysisJob | None:
        path = self._job_path(job_id)
        return self._read_job(path) if path.is_file() else None

    def _load_job(self, job_id: str) -> AnalysisJob:
        path = self._job_path(job_id)
        if not path.is_file():
            raise AnalysisJobError("job_not_found", "分析任务不存在", details={"job_id": job_id})
        return self._read_job(path)

    @staticmethod
    def _read_job(path: Path) -> AnalysisJob:
        payload = _read_json(path, "job_record_invalid")
        try:
            return AnalysisJob.from_dict(payload)
        except (ValueError, KeyError) as error:
            raise AnalysisJobError(
                "job_record_invalid",
                "分析任务记录结构无效",
                details={"path": str(path)},
            ) from error

    def _job_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"

    def _job_dir(self, job_id: str) -> Path:
        if not _JOB_ID_PATTERN.fullmatch(job_id):
            raise AnalysisJobError(
                "job_id_invalid", "分析任务标识格式无效", details={"job_id": job_id}
            )
        return self.jobs_root / job_id

    @staticmethod
    def _contained_path(root: Path, path: Path) -> Path:
        root_resolved = root.resolve()
        path_resolved = path.resolve()
        if path_resolved != root_resolved and root_resolved not in path_resolved.parents:
            raise AnalysisJobError(
                "artifact_path_invalid",
                "证据文件路径超出任务目录",
                details={"path": str(path)},
            )
        return path_resolved

    @staticmethod
    def _verify_declared_artifact(path: Path, declaration: dict[str, Any]) -> None:
        if not path.is_file():
            raise AnalysisJobError("artifact_missing", "证据文件缺失", details={"path": str(path)})
        expected_sha = _required_string(declaration, "sha256")
        if _sha256(path) != expected_sha:
            raise AnalysisJobError(
                "artifact_hash_mismatch",
                "证据文件哈希不匹配",
                details={"path": str(path)},
            )
        size = declaration.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size != path.stat().st_size:
            raise AnalysisJobError(
                "artifact_size_mismatch",
                "证据文件大小不匹配",
                details={"path": str(path)},
            )

    @staticmethod
    def _normalize_failure(
        error: AnalysisJobError | VideoAnalysisError | AudioAnalysisError,
    ) -> AnalysisFailure:
        prefix = "analysis"
        if isinstance(error, VideoAnalysisError):
            prefix = "video_analysis"
        elif isinstance(error, AudioAnalysisError):
            prefix = "audio_analysis"
        code = error.code if isinstance(error, AnalysisJobError) else f"{prefix}.{error.code}"
        return AnalysisFailure(code=code, message=error.message, details=error.details)

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _resolve_source(source: Path) -> Path:
        try:
            resolved = Path(source).resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise AnalysisJobError(
                "source_not_found",
                "参考视频文件不存在",
                details={"source": str(source)},
            ) from error
        if not resolved.is_file():
            raise AnalysisJobError(
                "source_not_found",
                "参考视频文件不存在",
                details={"source": str(source)},
            )
        return resolved


def _normalize_intervals(
    intervals: Sequence[tuple[int, int]], max_interval_us: int
) -> tuple[tuple[int, int], ...]:
    if max_interval_us <= 0 or max_interval_us > 100_000:
        raise AnalysisJobError(
            "refinement_interval_invalid",
            "密集抽帧最大间隔必须位于 1 到 100000 微秒",
        )
    normalized: set[tuple[int, int]] = set()
    for interval in intervals:
        if len(interval) != 2:
            raise AnalysisJobError("refinement_interval_invalid", "密集抽帧区间必须包含起止时间")
        start, end = interval
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
        ):
            raise AnalysisJobError("refinement_interval_invalid", "密集抽帧区间时间无效")
        normalized.add((start, end))
    if not normalized:
        raise AnalysisJobError("refinement_interval_invalid", "密集抽帧区间为空")
    return tuple(sorted(normalized))


def _contiguous_ranges(indexes: list[int]) -> Iterable[tuple[int, int]]:
    start = indexes[0]
    end = start
    for index in indexes[1:]:
        if index == end + 1:
            end = index
            continue
        yield start, end
        start = index
        end = index
    yield start, end


def _job_id(source_sha256: str, config_sha256: str) -> str:
    digest = hashlib.sha256(f"{source_sha256}:{config_sha256}".encode()).hexdigest()
    return f"reference_{digest[:24]}"


def _artifact_entry(root: Path, path: Path, kind: str, algorithm_version: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": _relative(root, path),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
        "algorithm_version": algorithm_version,
    }


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as error:
        raise AnalysisJobError("artifact_path_invalid", "证据文件路径超出任务目录") from error


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise AnalysisJobError(
            "artifact_path_invalid",
            "证据相对路径无效",
            details={"path": value},
        )
    return path


def _find_artifact(manifest: dict[str, Any], kind: str) -> dict[str, Any]:
    matches = [
        item for item in _required_dict_list(manifest, "artifacts") if item.get("kind") == kind
    ]
    if len(matches) != 1:
        raise AnalysisJobError(
            "component_manifest_invalid",
            "组件证据清单缺少唯一产物",
            details={"kind": kind},
        )
    return matches[0]


def _bundle_sha256(entries: list[dict[str, Any]]) -> str:
    return _canonical_sha256(entries)


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as error:
        raise AnalysisJobError("input_unavailable", f"文件读取失败：{path}：{error}") from error
    return digest.hexdigest()


def _read_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AnalysisJobError(code, "JSON 证据文件缺失", details={"path": str(path)}) from error
    except (OSError, json.JSONDecodeError) as error:
        raise AnalysisJobError(
            code, "JSON 证据文件读取失败", details={"path": str(path)}
        ) from error
    if not isinstance(value, dict):
        raise AnalysisJobError(code, "JSON 证据结构无效", details={"path": str(path)})
    return value


def _read_jsonl(path: Path, code: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        values = [json.loads(line) for line in lines if line]
    except FileNotFoundError as error:
        raise AnalysisJobError(code, "JSONL 证据文件缺失", details={"path": str(path)}) from error
    except (OSError, json.JSONDecodeError) as error:
        raise AnalysisJobError(
            code, "JSONL 证据文件读取失败", details={"path": str(path)}
        ) from error
    if not all(isinstance(value, dict) for value in values):
        raise AnalysisJobError(code, "JSONL 证据结构无效", details={"path": str(path)})
    return values


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as error:
        raise AnalysisJobError("job_store_unavailable", f"分析任务证据写入失败：{error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _required_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise AnalysisJobError("reference_manifest_invalid", f"{key} 结构无效")
    return value


def _required_dict_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AnalysisJobError("reference_manifest_invalid", f"{key} 列表结构无效")
    return value


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AnalysisJobError("reference_manifest_invalid", f"{key} 字段无效")
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnalysisJobError("timestamp_evidence_invalid", f"{key} 字段无效")
    return value


def _positive_fraction(value: str) -> Fraction:
    try:
        fraction = Fraction(value)
    except (ValueError, ZeroDivisionError) as error:
        raise AnalysisJobError("timestamp_evidence_invalid", "time_base 字段无效") from error
    if fraction <= 0:
        raise AnalysisJobError("timestamp_evidence_invalid", "time_base 字段无效")
    return fraction
