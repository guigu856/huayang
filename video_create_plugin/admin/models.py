from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ResourceKind = Literal["rule", "skill"]
OutputScope = Literal["all", "creation", "learning"]


class AdminModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResourceSummary(AdminModel):
    resource_id: str
    kind: ResourceKind
    uri: str
    title: str
    description: str
    relative_path: str
    builtin: bool
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    modified_at: datetime


class ResourceDocument(ResourceSummary):
    content: str


class ResourceCreateRequest(AdminModel):
    resource_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=300)
    content: str = ""


class ResourceUpdateRequest(AdminModel):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str = Field(min_length=1)


class OutputEntry(AdminModel):
    output_id: str
    scope: Literal["creation", "learning", "system"]
    kind: Literal["video", "audio", "image", "json", "text", "document"]
    name: str
    relative_path: str
    size_bytes: int = Field(ge=0)
    modified_at: datetime
    previewable: bool
    content_url: str


class Overview(AdminModel):
    plugin_name: str
    resource_root: str
    output_root: str
    rule_count: int = Field(ge=0)
    skill_count: int = Field(ge=0)
    task_count: int = Field(ge=0)
    artifact_count: int = Field(ge=0)
    creation_output_count: int = Field(ge=0)
    learning_output_count: int = Field(ge=0)
