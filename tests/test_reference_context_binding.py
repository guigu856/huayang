from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from video_create_plugin.errors import PluginError
from video_create_plugin.knowledge import (
    KnowledgeRecord,
    PublicationRequest,
)
from video_create_plugin.mcp.application import PluginApplication
from video_create_plugin.models import ArtifactRef

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "validation/reference_studies/01_fastcut_pip/report_manifest.json"


def test_reference_guided_creation_binds_each_stage_to_frozen_report_projection(
    tmp_path: Path,
) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    image_path = media_root / "one.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+"
            "A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    bgm_path = media_root / "bgm.wav"
    bgm_path.write_bytes(b"test-bgm")
    app = PluginApplication(
        tmp_path / "plugin-output",
        project_root=ROOT,
        media_roots=(media_root,),
    )
    _publish_shared_record(app)

    task = app.workflow.create_task("reference_guided_creation")
    study = app.workflow.get_stage_envelope(task.task_id)
    report_artifact = app.workflow.submit_artifact(
        access_handle=study.stage_access_handle,
        artifact_type="reference_report_manifest",
        content=app.artifacts.put_file(REPORT_PATH),
        schema_version="1.0",
        producer_kind="agent",
        producer_id="test-reference-agent",
        model_id="test-model",
        primary=True,
    )
    approval = app.workflow.get_stage_envelope(task.task_id)
    app.workflow.record_approval(
        access_handle=approval.stage_access_handle,
        user_confirmation_ref="test://reference-approved",
        confirmation_assurance="audit_only",
    )

    creative = app.workflow.get_stage_envelope(task.task_id)
    creative_context = app.reference_creation_context(creative.stage_access_handle)
    creative_retrieval = _search(app, creative.stage_access_handle)
    direction = _direction(creative_retrieval, creative_context["binding"])

    without_binding = {**direction, "reference_context": None}
    with pytest.raises(PluginError) as missing:
        _submit_primary(app, creative.stage_access_handle, "creative_direction", without_binding)
    assert missing.value.code == "reference_context_required"

    wrong_binding = {
        **creative_context["binding"],
        "stage_projection_sha256": "0" * 64,
    }
    with pytest.raises(PluginError) as mismatch:
        _submit_primary(
            app,
            creative.stage_access_handle,
            "creative_direction",
            _direction(creative_retrieval, wrong_binding),
        )
    assert mismatch.value.code == "reference_context_mismatch"

    _submit_primary(app, creative.stage_access_handle, "creative_direction", direction)
    _approve(app, task.task_id, "creative")

    resource = app.workflow.get_stage_envelope(task.task_id)
    resource_context = app.reference_creation_context(resource.stage_access_handle)
    assert resource_context["binding"]["source_report_ref"] == (
        report_artifact.as_ref().model_dump(mode="json")
    )
    resource_retrieval = _search(app, resource.stage_access_handle)
    preparation = _preparation(
        image_path,
        bgm_path,
        resource_retrieval,
        resource_context["binding"],
    )
    _submit_primary(
        app,
        resource.stage_access_handle,
        "preparation_package",
        preparation,
    )
    _approve(app, task.task_id, "resources")

    editing = app.workflow.get_stage_envelope(task.task_id)
    editing_context = app.reference_creation_context(editing.stage_access_handle)
    editing_retrieval = _search(app, editing.stage_access_handle)
    specification = _specification(
        image_path,
        editing_retrieval,
        editing_context["binding"],
    )
    artifact = _submit_primary(
        app,
        editing.stage_access_handle,
        "editing_specification",
        specification,
    )
    assert artifact["artifact_type"] == "editing_specification"

    original = app.workflow.create_task("original_creation")
    original_stage = app.workflow.get_stage_envelope(original.task_id)
    original_retrieval = _search(app, original_stage.stage_access_handle)
    with pytest.raises(PluginError) as forbidden:
        _submit_primary(
            app,
            original_stage.stage_access_handle,
            "creative_direction",
            _direction(original_retrieval, creative_context["binding"]),
        )
    assert forbidden.value.code == "reference_context_forbidden"


def _publish_shared_record(app: PluginApplication) -> None:
    source_ref = ArtifactRef(
        artifact_id="artifact_0123456789abcdef",
        revision=1,
        sha256="a" * 64,
    )
    record = KnowledgeRecord.model_validate(
        {
            "collection": "creation_knowledge",
            "source_task_id": "task_reference_source",
            "source_report_ref": source_ref,
            "source_artifact_refs": [source_ref],
            "analysis_version": "v1",
            "applicable_stages": ["stage1", "stage2", "stage3"],
            "knowledge_type": "reference_mechanism",
            "visibility": "creation_shared",
            "transferability": "reusable_mechanism",
            "content": "按音乐层级组织镜头密度与辅助层进入退出。",
            "evidence_refs": ["evidence://reference/1"],
            "fact_status": "inference",
            "confidence": 0.9,
            "granularity": "global",
        }
    )
    app.knowledge_store.publish(
        PublicationRequest(
            source_task_id="task_reference_source",
            source_report_ref=source_ref,
            source_media_sha256="b" * 64,
            publication_revision=1,
            freeze_id="freeze_reference",
            records=[record],
        )
    )


def _search(app: PluginApplication, access_handle: str) -> str:
    result = app.knowledge_search(
        access_handle,
        {
            "text": "镜头密度与辅助层",
            "knowledge_types": ["reference_mechanism"],
        },
    )
    assert result["result"]["shared_creation_knowledge"]
    return str(result["retrieval"]["retrieval_id"])


def _direction(retrieval_id: str, binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "title": "参考节奏重构",
        "user_intent": "使用新素材重构参考节奏体验",
        "video_type": "节奏型短视频",
        "core_mechanism": "按音乐层级组织镜头与辅助层",
        "production_method": "先定能量结构，再组织画面主次",
        "visual_language": "主画面清晰，辅助层短暂进入",
        "rhythm_and_sound": "重拍切换，新音色触发辅助层",
        "transition_principles": "主画面硬切，镜内叠层连续",
        "asset_and_music_traits": "素材运动协调，音乐瞬态清晰",
        "viewing_experience": "紧凑、丰富且仍可辨认",
        "retrieval_ids": [retrieval_id],
        "reference_context": binding,
    }


def _preparation(
    image_path: Path,
    bgm_path: Path,
    retrieval_id: str,
    binding: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "materials": [
            {
                "asset_id": "material_image",
                "kind": "image",
                "name": "单像素图片",
                "path": str(image_path),
                "sha256": _sha256(image_path),
                "width": 1,
                "height": 1,
                "content_summary": "测试视觉素材",
                "selection_traits": ["主体清晰"],
                "usable_source_ranges": [{"start_us": 0, "end_us": 1_000_000}],
                "source_url": "test://visual/source",
                "provider": "test",
                "creator": "test",
                "license_record": "test-license",
                "provenance_ref": "test://visual/provenance",
            }
        ],
        "bgm": {
            "asset_id": "material_bgm",
            "name": "测试 BGM",
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
        "provenance_refs": [
            "test://visual/provenance",
            "test://bgm/provenance",
        ],
        "retrieval_ids": [retrieval_id],
        "reference_context": binding,
    }


def _specification(
    image_path: Path,
    retrieval_id: str,
    binding: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "spec_id": "spec_reference_binding",
        "title": "参考绑定规格",
        "canvas": {"width": 1, "height": 1, "fps": 25},
        "duration_us": 1_000_000,
        "assets": [
            {
                "asset_id": "material_image",
                "kind": "image",
                "name": "单像素图片",
                "path": str(image_path),
                "sha256": _sha256(image_path),
                "width": 1,
                "height": 1,
                "provenance_ref": "test://visual/provenance",
            }
        ],
        "shots": [
            {
                "shot_id": "shot_1",
                "timeline": {"start_us": 0, "end_us": 1_000_000},
                "main_action_id": "action_main",
                "action_ids": ["action_main"],
                "human_description": "图片覆盖完整时间线",
                "transition_to_next": "end",
            }
        ],
        "actions": [
            {
                "action_id": "action_main",
                "shot_id": "shot_1",
                "action_type": "visual_media",
                "timeline": {"start_us": 0, "end_us": 1_000_000},
                "asset_id": "material_image",
                "transform": {"x": 0, "y": 0, "width": 1, "height": 1},
                "required_capabilities": ["image_hold"],
                "human_description": "单图主画面",
            }
        ],
        "beat_grid_us": [0, 1_000_000],
        "retrieval_ids": [retrieval_id],
        "reference_context": binding,
    }


def _submit_primary(
    app: PluginApplication,
    access_handle: str,
    artifact_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return app.submit_artifact(
        access_handle=access_handle,
        artifact_type=artifact_type,
        content=json.dumps(payload, ensure_ascii=False),
        schema_version="1.0",
        producer_kind="agent",
        producer_id="test-creation-agent",
        primary=True,
        parent_artifact_refs=None,
        evidence_refs=None,
        rule_version="test-rule-v1",
        skill_versions=[],
        model_id="test-model",
        component_version=None,
    )


def _approve(app: PluginApplication, task_id: str, suffix: str) -> None:
    envelope = app.workflow.get_stage_envelope(task_id)
    app.workflow.record_approval(
        access_handle=envelope.stage_access_handle,
        user_confirmation_ref=f"test://approved/{suffix}",
        confirmation_assurance="audit_only",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
