from __future__ import annotations

from typing import Literal

from pydantic import Field

from .models import ArtifactRef, FreezeRef, PluginModel


class RenderInspectionBindingPayload(PluginModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(pattern=r"^task_[0-9a-f]{16}$")
    render_job_id: str = Field(pattern=r"^render_[0-9a-f]{16}$")
    editing_artifact_ref: ArtifactRef
    editing_freeze_ref: FreezeRef
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_registry_version: str = Field(min_length=1)
    project_id: str = Field(pattern=r"^project_[0-9a-f]{16}$")
    project_revision: int = Field(ge=0)
    compiled_project_path: str = Field(min_length=1)
    compiled_project_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_project_snapshot_path: str = Field(min_length=1)
    render_project_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace_map_path: str = Field(min_length=1)
    trace_map_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_trace_map_path: str = Field(min_length=1)
    render_trace_map_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_path: str = Field(min_length=1)
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expectation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspection_path: str = Field(min_length=1)
    inspection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspection_passed: bool
    contact_sheet_path: str = Field(min_length=1)
    contact_sheet_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RenderInspectionBinding(PluginModel):
    payload: RenderInspectionBindingPayload
    hmac_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutionManifest(PluginModel):
    schema_version: Literal["1.0"] = "1.0"
    render_job_id: str = Field(pattern=r"^render_[0-9a-f]{16}$")
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_registry_version: str = Field(min_length=1)
    project_id: str = Field(pattern=r"^project_[0-9a-f]{16}$")
    project_path: str = Field(min_length=1)
    project_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace_map_path: str = Field(min_length=1)
    trace_map_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_path: str = Field(min_length=1)
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspection_path: str = Field(min_length=1)
    inspection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspection_binding_path: str = Field(min_length=1)
    inspection_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspection_passed: Literal[True]
