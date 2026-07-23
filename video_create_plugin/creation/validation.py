from __future__ import annotations

from video_create_plugin.editing import EditingSpecification

from .models import BgmPackage, PreparationPackage, PreparedMaterial


class PreparationScopeError(ValueError):
    """剪辑规格越过已冻结资源包边界。"""


def validate_preparation_scope(
    preparation: PreparationPackage,
    specification: EditingSpecification,
) -> None:
    approved_assets = {
        material.asset_id: _visual_identity(material) for material in preparation.materials
    }
    approved_assets[preparation.bgm.asset_id] = _audio_identity(preparation.bgm)

    for asset in specification.assets:
        expected = approved_assets.get(asset.asset_id)
        if expected is None:
            raise PreparationScopeError("剪辑规格引用了资源包之外的素材")
        if asset.model_dump(mode="python") != expected:
            raise PreparationScopeError("剪辑规格改写了已确认素材的身份或技术参数")

    bgm = preparation.bgm
    uses_bgm = any(action.action_type == "audio_media" for action in specification.actions)
    if uses_bgm and specification.duration_us > bgm.duration_us:
        raise PreparationScopeError("剪辑规格时长超出已确认 BGM")

    allowed_beats = {0, specification.duration_us, *bgm.beat_grid_us}
    if not set(specification.beat_grid_us).issubset(allowed_beats):
        raise PreparationScopeError("剪辑规格包含资源包未确认的 BGM 拍点")

    prepared_visuals = {material.asset_id: material for material in preparation.materials}
    for action in specification.actions:
        if action.action_type != "visual_media" or action.source is None:
            continue
        if action.asset_id is None or action.asset_id not in prepared_visuals:
            raise PreparationScopeError("视觉动作引用了资源包之外的素材")
        material = prepared_visuals[action.asset_id]
        if not any(
            source.start_us <= action.source.start_us and action.source.end_us <= source.end_us
            for source in material.usable_source_ranges
        ):
            raise PreparationScopeError("视觉动作源区间超出资源包确认的可用范围")


def _visual_identity(material: PreparedMaterial) -> dict[str, object]:
    return {
        "asset_id": material.asset_id,
        "kind": material.kind,
        "name": material.name,
        "path": material.path,
        "sha256": material.sha256,
        "duration_us": material.duration_us,
        "width": material.width,
        "height": material.height,
        "provenance_ref": material.provenance_ref,
    }


def _audio_identity(bgm: BgmPackage) -> dict[str, object]:
    return {
        "asset_id": bgm.asset_id,
        "kind": "audio",
        "name": bgm.name,
        "path": bgm.path,
        "sha256": bgm.sha256,
        "duration_us": bgm.duration_us,
        "width": None,
        "height": None,
        "provenance_ref": bgm.provenance_ref,
    }
