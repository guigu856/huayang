from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from video_create_plugin.errors import PluginError
from video_create_plugin.mcp.application import PluginApplication

PROJECT_ROOT = Path(__file__).parents[1]


def test_editing_stage_rejects_asset_outside_frozen_preparation(tmp_path: Path) -> None:
    app, preparation, specification, media_root = _image_case(tmp_path)
    external_path = media_root / "external.png"
    external_path.write_bytes(b"external-image")
    external_asset = {
        **specification["assets"][0],
        "asset_id": "material_external",
        "name": "包外图片",
        "path": str(external_path),
        "sha256": _sha256(external_path),
        "provenance_ref": "test://visual/external",
    }
    specification["assets"] = [external_asset]
    specification["actions"][0]["asset_id"] = external_asset["asset_id"]

    access_handle = _seed_editing_stage(app, preparation)

    with pytest.raises(PluginError) as caught:
        _submit_specification(app, access_handle, specification)

    assert caught.value.code == "preparation_scope_mismatch"
    assert "资源包之外" in caught.value.message


@pytest.mark.parametrize(
    "mutation",
    ["path", "sha256", "provenance_ref", "width", "height", "duration_us"],
)
def test_editing_stage_rejects_modified_approved_asset_identity(
    tmp_path: Path,
    mutation: str,
) -> None:
    app, preparation, specification, media_root = _image_case(tmp_path)
    asset = specification["assets"][0]
    if mutation == "path":
        replacement_path = media_root / "same-content-different-path.png"
        replacement_path.write_bytes(Path(asset["path"]).read_bytes())
        asset["path"] = str(replacement_path)
    elif mutation == "sha256":
        asset["sha256"] = "0" * 64
    elif mutation == "provenance_ref":
        asset["provenance_ref"] = "test://visual/forged-provenance"
    elif mutation in {"width", "height"}:
        asset[mutation] = 2
    else:
        asset["duration_us"] = 2_000_000

    access_handle = _seed_editing_stage(app, preparation)

    with pytest.raises(PluginError) as caught:
        _submit_specification(app, access_handle, specification)

    assert caught.value.code == "preparation_scope_mismatch"
    assert "身份或技术参数" in caught.value.message


def test_editing_stage_rejects_video_source_outside_usable_ranges(tmp_path: Path) -> None:
    app, preparation, specification = _video_case(tmp_path)
    access_handle = _seed_editing_stage(app, preparation)

    with pytest.raises(PluginError) as caught:
        _submit_specification(app, access_handle, specification)

    assert caught.value.code == "preparation_scope_mismatch"
    assert "可用范围" in caught.value.message


def test_execution_preflight_rechecks_frozen_spec_against_preparation(
    tmp_path: Path,
) -> None:
    app, preparation, specification, _ = _image_case(tmp_path)
    specification["assets"][0]["provenance_ref"] = "test://visual/forged-provenance"
    access_handle = _seed_execution_stage(app, preparation, specification)

    with pytest.raises(PluginError) as caught:
        app.editor_preflight(access_handle)

    assert caught.value.code == "preparation_scope_mismatch"
    assert "身份或技术参数" in caught.value.message


def _image_case(
    tmp_path: Path,
) -> tuple[PluginApplication, dict[str, Any], dict[str, Any], Path]:
    media_root = tmp_path / "media"
    media_root.mkdir()
    image_path = media_root / "approved.png"
    image_path.write_bytes(b"approved-image")
    app = PluginApplication(
        tmp_path / "plugin-output",
        project_root=PROJECT_ROOT,
        media_roots=(media_root,),
    )
    specification = _specification(
        asset={
            "asset_id": "material_approved",
            "kind": "image",
            "name": "已确认图片",
            "path": str(image_path),
            "sha256": _sha256(image_path),
            "width": 1,
            "height": 1,
            "provenance_ref": "test://visual/approved",
        },
        source=None,
    )
    preparation = _preparation(
        media_root,
        specification["assets"][0],
        usable_source_ranges=[{"start_us": 0, "end_us": 1_000_000}],
    )
    return app, preparation, specification, media_root


def _video_case(
    tmp_path: Path,
) -> tuple[PluginApplication, dict[str, Any], dict[str, Any]]:
    media_root = tmp_path / "media"
    media_root.mkdir()
    video_path = media_root / "approved.mp4"
    video_path.write_bytes(b"approved-video")
    app = PluginApplication(
        tmp_path / "plugin-output",
        project_root=PROJECT_ROOT,
        media_roots=(media_root,),
    )
    asset = {
        "asset_id": "material_approved",
        "kind": "video",
        "name": "已确认视频",
        "path": str(video_path),
        "sha256": _sha256(video_path),
        "duration_us": 3_000_000,
        "width": 1920,
        "height": 1080,
        "provenance_ref": "test://visual/approved",
    }
    specification = _specification(
        asset=asset,
        source={"start_us": 1_000_000, "end_us": 2_000_000},
    )
    preparation = _preparation(
        media_root,
        asset,
        usable_source_ranges=[{"start_us": 0, "end_us": 1_000_000}],
    )
    return app, preparation, specification


def _specification(
    *,
    asset: dict[str, Any],
    source: dict[str, int] | None,
) -> dict[str, Any]:
    action: dict[str, Any] = {
        "action_id": "action_main",
        "shot_id": "shot_main",
        "action_type": "visual_media",
        "timeline": {"start_us": 0, "end_us": 1_000_000},
        "asset_id": asset["asset_id"],
        "transform": {"x": 0, "y": 0, "width": 1, "height": 1},
        "required_capabilities": ["image_hold" if source is None else "video_clip"],
        "human_description": "完整覆盖时间线的主画面",
    }
    if source is not None:
        action["source"] = source
    return {
        "schema_version": "1.0",
        "spec_id": "spec_preparation_scope",
        "title": "资源包边界测试规格",
        "canvas": {"width": 1, "height": 1, "fps": 25},
        "duration_us": 1_000_000,
        "assets": [copy.deepcopy(asset)],
        "shots": [
            {
                "shot_id": "shot_main",
                "timeline": {"start_us": 0, "end_us": 1_000_000},
                "main_action_id": "action_main",
                "action_ids": ["action_main"],
                "human_description": "单镜头测试",
                "transition_to_next": "end",
            }
        ],
        "actions": [action],
        "beat_grid_us": [0, 1_000_000],
        "retrieval_ids": ["retrieval_stage3_test"],
    }


def _preparation(
    media_root: Path,
    asset: dict[str, Any],
    *,
    usable_source_ranges: list[dict[str, int]],
) -> dict[str, Any]:
    bgm_path = media_root / "approved-bgm.wav"
    bgm_path.write_bytes(b"approved-bgm")
    material = {
        **copy.deepcopy(asset),
        "content_summary": "测试素材",
        "selection_traits": ["已确认"],
        "usable_source_ranges": usable_source_ranges,
        "source_url": "test://visual/source",
        "provider": "test",
        "creator": "test",
        "license_record": "test-license",
    }
    return {
        "schema_version": "1.0",
        "materials": [material],
        "bgm": {
            "asset_id": "material_bgm",
            "name": "已确认 BGM",
            "path": str(bgm_path),
            "sha256": _sha256(bgm_path),
            "duration_us": 1_000_000,
            "source_url": "test://bgm/source",
            "provider": "test",
            "creator": "test",
            "license_record": "test-license",
            "provenance_ref": "test://bgm/provenance",
            "audio_analysis_ref": "test://bgm/analysis",
            "mood_traits": ["稳定"],
            "tempo_candidates_bpm": [120.0],
            "beat_grid_us": [0, 1_000_000],
            "sections": [
                {
                    "section_id": "section_full",
                    "start_us": 0,
                    "end_us": 1_000_000,
                    "role": "完整段落",
                    "energy_phase": "稳定",
                }
            ],
        },
        "provenance_refs": [asset["provenance_ref"], "test://bgm/provenance"],
        "retrieval_ids": ["retrieval_stage2_test"],
    }


def _seed_editing_stage(
    app: PluginApplication,
    preparation: dict[str, Any],
) -> str:
    task = app.workflow.create_task("original_creation")
    _submit_and_approve(
        app,
        task.task_id,
        "creative_direction",
        {"stage": "creative_direction"},
    )
    _submit_and_approve(app, task.task_id, "preparation_package", preparation)
    envelope = app.workflow.get_stage_envelope(task.task_id)
    assert envelope.stage == "editing_specification"
    return envelope.stage_access_handle


def _seed_execution_stage(
    app: PluginApplication,
    preparation: dict[str, Any],
    specification: dict[str, Any],
) -> str:
    task = app.workflow.create_task("original_creation")
    for artifact_type, payload in (
        ("creative_direction", {"stage": "creative_direction"}),
        ("preparation_package", preparation),
        ("editing_specification", specification),
    ):
        _submit_and_approve(app, task.task_id, artifact_type, payload)
    envelope = app.workflow.get_stage_envelope(task.task_id)
    assert envelope.stage == "execution"
    return envelope.stage_access_handle


def _submit_and_approve(
    app: PluginApplication,
    task_id: str,
    artifact_type: str,
    payload: dict[str, Any],
) -> None:
    envelope = app.workflow.get_stage_envelope(task_id)
    artifact = app.artifacts.put_text(json.dumps(payload, ensure_ascii=False))
    app.workflow.submit_artifact(
        access_handle=envelope.stage_access_handle,
        artifact_type=artifact_type,
        content=artifact,
        schema_version="1.0",
        producer_kind="component",
        producer_id="preparation-scope-test",
        primary=True,
        component_version="test-v1",
    )
    approval_envelope = app.workflow.get_stage_envelope(task_id)
    app.workflow.record_approval(
        access_handle=approval_envelope.stage_access_handle,
        user_confirmation_ref=f"test://approval/{artifact_type}",
        confirmation_assurance="audit_only",
    )


def _submit_specification(
    app: PluginApplication,
    access_handle: str,
    specification: dict[str, Any],
) -> dict[str, Any]:
    return app.submit_artifact(
        access_handle=access_handle,
        artifact_type="editing_specification",
        content=json.dumps(specification, ensure_ascii=False),
        schema_version="1.0",
        producer_kind="agent",
        producer_id="editing-agent-test",
        primary=True,
        parent_artifact_refs=None,
        evidence_refs=None,
        rule_version="test-rule-v1",
        skill_versions=[],
        model_id="test-model",
        component_version=None,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
