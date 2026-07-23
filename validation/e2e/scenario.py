from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ScenarioModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CanvasDefinition(ScenarioModel):
    width: int = Field(ge=320, le=1920)
    height: int = Field(ge=180, le=1080)
    fps: float = Field(gt=0, le=60)


class TransformDefinition(ScenarioModel):
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    opacity: float = Field(default=1, ge=0, le=1)


class SourceClipDefinition(ScenarioModel):
    asset_id: str = Field(pattern=r"^material_[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    source_start_us: int = Field(ge=0)
    extract_duration_us: int = Field(gt=0)
    content_summary: str = Field(min_length=1)
    selection_traits: list[str] = Field(min_length=1)


class PipDefinition(ScenarioModel):
    pip_id: str = Field(pattern=r"^pip_[A-Za-z0-9_-]+$")
    shot_index: int = Field(ge=0)
    asset_id: str = Field(pattern=r"^material_[A-Za-z0-9_-]+$")
    start_offset_us: int = Field(ge=0)
    duration_us: int = Field(gt=0)
    transform: TransformDefinition


class BgmDefinition(ScenarioModel):
    asset_id: Literal["material_bgm"] = "material_bgm"
    path: str = Field(min_length=1)
    provenance_path: str = Field(min_length=1)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mood_traits: list[str] = Field(min_length=1)


class KnowledgeQueryDefinition(ScenarioModel):
    text: str = Field(min_length=1)
    knowledge_types: list[str] = Field(min_length=1)
    limit: int = Field(default=8, ge=1, le=100)

    @field_validator("knowledge_types")
    @classmethod
    def normalize_types(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("知识类型不得重复")
        return values


class KnowledgeQueries(ScenarioModel):
    stage1: KnowledgeQueryDefinition
    stage2: KnowledgeQueryDefinition
    stage3: KnowledgeQueryDefinition


class ScenarioDefinition(ScenarioModel):
    schema_version: Literal["1.0"] = "1.0"
    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    profile: Literal["fast_cut_pip", "calm_layered"]
    title: str = Field(min_length=1)
    user_intent: str = Field(min_length=1)
    duration_us: int = Field(ge=6_000_000, le=9_000_000)
    canvas: CanvasDefinition
    main_shot_count: int = Field(ge=2, le=8)
    minimum_distinct_visual_assets: int = Field(ge=1)
    forbidden_source_sha256s: list[str] = Field(min_length=1)
    source_clips: list[SourceClipDefinition] = Field(min_length=1)
    main_asset_ids: list[str] = Field(min_length=1)
    pip_events: list[PipDefinition]
    bgm: BgmDefinition
    knowledge_queries: KnowledgeQueries

    @field_validator("forbidden_source_sha256s")
    @classmethod
    def hashes_are_unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in values
        ):
            raise ValueError("禁用来源哈希必须是唯一的小写 SHA-256")
        return values

    @model_validator(mode="after")
    def profile_constraints_are_consistent(self) -> Self:
        source_ids = [source.asset_id for source in self.source_clips]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("派生视觉素材 ID 不得重复")
        if len(self.main_asset_ids) != self.main_shot_count:
            raise ValueError("主素材清单必须与主镜头数量一致")
        if len(self.main_asset_ids) != len(set(self.main_asset_ids)):
            raise ValueError("真实验证的每个主镜头必须使用不同派生素材")
        if any(asset_id not in source_ids for asset_id in self.main_asset_ids):
            raise ValueError("主镜头引用了未声明的派生素材")
        if any(event.asset_id not in source_ids for event in self.pip_events):
            raise ValueError("画中画引用了未声明的派生素材")
        if any(event.shot_index >= self.main_shot_count for event in self.pip_events):
            raise ValueError("画中画镜头索引超出主镜头范围")
        if self.minimum_distinct_visual_assets > len(source_ids):
            raise ValueError("独立视觉素材门槛高于已声明素材数量")
        for event in self.pip_events:
            transform = event.transform
            if (
                transform.x + transform.width > self.canvas.width
                or transform.y + transform.height > self.canvas.height
            ):
                raise ValueError("画中画变换超出画布")
        if self.profile == "fast_cut_pip":
            if not 6 <= self.main_shot_count <= 8:
                raise ValueError("快切场景必须包含 6 到 8 个主镜头")
            if not 2 <= len(self.pip_events) <= 3:
                raise ValueError("快切场景必须包含 2 到 3 次镜内画中画")
            if self.minimum_distinct_visual_assets < 4:
                raise ValueError("快切场景至少规划四个独立视觉素材")
            if any(event.duration_us > 900_000 for event in self.pip_events):
                raise ValueError("快切场景的画中画必须短时停留")
        else:
            if not 2 <= self.main_shot_count <= 3:
                raise ValueError("舒缓场景必须包含 2 到 3 个长镜头")
            if len(self.pip_events) > 1:
                raise ValueError("舒缓场景至多规划一次画中画")
            if self.pip_events and self.pip_events[0].duration_us < 1_000_000:
                raise ValueError("舒缓场景的画中画必须保持长停留")
        return self


def load_scenario(path: Path | str) -> ScenarioDefinition:
    source = Path(path).resolve()
    payload = json.loads(source.read_text(encoding="utf-8"))
    return ScenarioDefinition.model_validate(payload)


def bundled_scenario_path(name: str) -> Path:
    path = Path(__file__).with_name("scenarios") / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"场景定义不存在：{name}")
    return path
