from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from components.video_editor.media import probe_media
from components.video_editor.models import (
    Asset,
    Canvas,
    Clip,
    EditorProject,
    MediaMetadata,
    Track,
    Transform,
)

from ..errors import PluginError
from .models import (
    ActionCapabilityCheck,
    ActionSpec,
    CapabilityAssessment,
    EditingSpecification,
    MaterialAsset,
    SpecTraceEntry,
    SpecTraceMap,
)

REGISTRY_VERSION = "video-editor-2.0-static-v1"
SUPPORTED_CAPABILITIES = frozenset(
    {
        "audio_source_trim",
        "hard_cut",
        "image_hold",
        "layer_overlay",
        "static_transform",
        "static_volume",
        "text_overlay",
        "video_source_trim",
    }
)


@dataclass(frozen=True, slots=True)
class CompileResult:
    project: EditorProject
    assessment: CapabilityAssessment
    trace_map: SpecTraceMap
    copied_asset_paths: tuple[Path, ...]


def canonical_spec_sha256(specification: EditingSpecification) -> str:
    payload = json.dumps(
        specification.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def preflight_spec(specification: EditingSpecification) -> CapabilityAssessment:
    checks: list[ActionCapabilityCheck] = []
    for action in specification.actions:
        required = sorted(
            set(action.required_capabilities) | _derived_capabilities(action, specification)
        )
        missing = sorted(set(required) - SUPPORTED_CAPABILITIES)
        checks.append(
            ActionCapabilityCheck(
                action_id=action.action_id,
                required=required,
                missing=missing,
            )
        )
    return CapabilityAssessment(
        registry_version=REGISTRY_VERSION,
        spec_sha256=canonical_spec_sha256(specification),
        supported=all(not check.missing for check in checks),
        action_checks=checks,
    )


class ExecutionCompiler:
    """把已验证 ActionSpec 逐项映射为 EditorProject 2.0。"""

    def __init__(
        self,
        *,
        probe: Callable[[Path], MediaMetadata] = probe_media,
    ) -> None:
        self._probe = probe

    def compile(
        self,
        specification: EditingSpecification,
        project_dir: Path | str,
    ) -> CompileResult:
        assessment = preflight_spec(specification)
        if not assessment.supported:
            raise PluginError(
                "capability_gap",
                "剪辑规格包含当前引擎能力之外的动作",
                details={
                    "actions": [
                        check.model_dump(mode="json")
                        for check in assessment.action_checks
                        if check.missing
                    ]
                },
            )
        root = Path(project_dir).resolve()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise PluginError("output_unavailable", "工程目录创建失败") from error
        editor_assets: list[Asset] = []
        asset_ids: dict[str, str] = {}
        copied: list[Path] = []
        for material in specification.assets:
            asset, copied_path = self._materialize_asset(material, root)
            editor_assets.append(asset)
            asset_ids[material.asset_id] = asset.id
            copied.append(copied_path)

        actions_by_layer: dict[int, list[ActionSpec]] = {}
        audio_actions: list[ActionSpec] = []
        for action in specification.actions:
            if action.action_type == "audio_media":
                audio_actions.append(action)
            else:
                actions_by_layer.setdefault(action.layer, []).append(action)

        tracks: list[Track] = []
        traces: list[SpecTraceEntry] = []
        for layer in sorted(actions_by_layer, reverse=True):
            track_id = f"track_visual_{layer}"
            clips: list[Clip] = []
            actions = sorted(
                actions_by_layer[layer],
                key=lambda item: (item.timeline.start_us, item.action_id),
            )
            for action in actions:
                clip = _compile_clip(action, asset_ids, specification)
                clips.append(clip)
                traces.append(
                    SpecTraceEntry(
                        action_id=action.action_id,
                        project_json_path=f"tracks[{len(tracks)}].clips[{len(clips) - 1}]",
                        track_id=track_id,
                        clip_id=clip.id,
                    )
                )
            tracks.append(
                Track(
                    id=track_id,
                    media_domain="visual",
                    name=f"视觉层 {layer}",
                    clips=clips,
                )
            )
        if audio_actions:
            track_id = "track_audio_bgm"
            clips = []
            for action in sorted(
                audio_actions,
                key=lambda item: (item.timeline.start_us, item.action_id),
            ):
                clip = _compile_clip(action, asset_ids, specification)
                clips.append(clip)
                traces.append(
                    SpecTraceEntry(
                        action_id=action.action_id,
                        project_json_path=f"tracks[{len(tracks)}].clips[{len(clips) - 1}]",
                        track_id=track_id,
                        clip_id=clip.id,
                    )
                )
            tracks.append(
                Track(
                    id=track_id,
                    media_domain="audio",
                    name="BGM",
                    clips=clips,
                )
            )

        spec_sha256 = assessment.spec_sha256
        project = EditorProject(
            id=f"project_{spec_sha256[:16]}",
            name=specification.title,
            canvas=Canvas(
                width=specification.canvas.width,
                height=specification.canvas.height,
                fps=specification.canvas.fps,
                background_color=specification.canvas.background_color,
            ),
            assets=editor_assets,
            tracks=tracks,
        )
        trace_map = SpecTraceMap(
            spec_sha256=spec_sha256,
            project_id=project.id,
            entries=sorted(traces, key=lambda item: item.action_id),
        )
        validate_execution_project(specification, project, trace_map)
        return CompileResult(
            project=project,
            assessment=assessment,
            trace_map=trace_map,
            copied_asset_paths=tuple(copied),
        )

    def _materialize_asset(self, material: MaterialAsset, root: Path) -> tuple[Asset, Path]:
        source = Path(material.path).expanduser().resolve()
        if not source.is_file():
            raise PluginError("artifact_not_found", "剪辑素材文件不存在")
        if _sha256(source) != material.sha256:
            raise PluginError("artifact_hash_mismatch", "剪辑素材哈希不匹配")
        suffix = source.suffix.lower() if source.suffix else ".bin"
        destination = root / "assets" / f"{material.sha256}{suffix}"
        _copy_content_addressed(source, destination)
        metadata = self._probe(destination)
        _verify_declared_metadata(material, metadata)
        editor_id = f"asset_{hashlib.sha256(material.asset_id.encode()).hexdigest()[:16]}"
        return (
            Asset(
                id=editor_id,
                kind=material.kind,
                name=material.name,
                path=destination.relative_to(root).as_posix(),
                metadata=metadata,
            ),
            destination,
        )


def validate_execution_project(
    specification: EditingSpecification,
    project: EditorProject,
    trace_map: SpecTraceMap,
) -> None:
    spec_sha256 = canonical_spec_sha256(specification)
    if trace_map.spec_sha256 != spec_sha256 or trace_map.project_id != project.id:
        raise PluginError("spec_trace_incomplete", "规格追溯表与工程身份不一致")
    expected = {action.action_id for action in specification.actions}
    entries = {entry.action_id: entry for entry in trace_map.entries}
    if set(entries) != expected:
        raise PluginError("spec_trace_incomplete", "规格动作没有全部映射到工程")
    track_by_id = {track.id: track for track in project.tracks}
    for entry in trace_map.entries:
        track = track_by_id.get(entry.track_id)
        if track is None or all(clip.id != entry.clip_id for clip in track.clips):
            raise PluginError("spec_trace_incomplete", "追溯表引用的工程片段不存在")


def _derived_capabilities(
    action: ActionSpec,
    specification: EditingSpecification,
) -> set[str]:
    if action.action_type == "text_overlay":
        return {"text_overlay", "static_transform"}
    asset = next(item for item in specification.assets if item.asset_id == action.asset_id)
    if action.action_type == "audio_media":
        return {"audio_source_trim", "static_volume"}
    capabilities = {"static_transform"}
    capabilities.add("image_hold" if asset.kind == "image" else "video_source_trim")
    capabilities.add("hard_cut" if action.layer == 0 else "layer_overlay")
    return capabilities


def _compile_clip(
    action: ActionSpec,
    asset_ids: dict[str, str],
    specification: EditingSpecification,
) -> Clip:
    transform = action.transform
    resolved_transform = Transform(
        x=transform.x if transform is not None else 0,
        y=transform.y if transform is not None else 0,
        width=(transform.width if transform is not None else specification.canvas.width),
        height=(transform.height if transform is not None else specification.canvas.height),
        rotation=transform.rotation if transform is not None else 0,
        opacity=transform.opacity if transform is not None else 1,
    )
    values: dict[str, object] = {
        "id": f"clip_{hashlib.sha256(action.action_id.encode()).hexdigest()[:16]}",
        "kind": "text" if action.action_type == "text_overlay" else "media",
        "timeline_start": action.timeline.start_us / 1_000_000,
        "duration": action.timeline.duration_us / 1_000_000,
        "source_in": (action.source.start_us / 1_000_000 if action.source else 0),
        "transform": resolved_transform,
        "volume": action.volume,
    }
    if action.action_type == "text_overlay":
        values["text"] = action.text
    else:
        assert action.asset_id is not None
        values["asset_id"] = asset_ids[action.asset_id]
    return Clip.model_validate(values)


def _verify_declared_metadata(material: MaterialAsset, metadata: MediaMetadata) -> None:
    actual_kind = (
        "audio"
        if metadata.width is None
        else ("video" if metadata.duration is not None else "image")
    )
    if actual_kind != material.kind:
        raise PluginError("action_spec_invalid", "素材声明类型与探测结果不一致")
    if material.duration_us is not None:
        if (
            metadata.duration is None
            or abs(metadata.duration * 1_000_000 - material.duration_us) > 100_000
        ):
            raise PluginError("action_spec_invalid", "素材声明时长与探测结果不一致")
    if material.width is not None and (
        metadata.width != material.width or metadata.height != material.height
    ):
        raise PluginError("action_spec_invalid", "素材声明尺寸与探测结果不一致")


def _copy_content_addressed(source: Path, destination: Path) -> None:
    if destination.is_file():
        if _sha256(destination) != _sha256(source):
            raise PluginError("artifact_hash_mismatch", "工程内同名素材哈希冲突")
        return
    temporary: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(descriptor)
        temporary = Path(name)
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
        temporary = None
    except OSError as error:
        raise PluginError("output_unavailable", "工程素材复制失败") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
