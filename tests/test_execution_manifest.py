from __future__ import annotations

from video_create_plugin.execution import ExecutionManifest


def test_execution_manifest_requires_a_passed_inspection() -> None:
    manifest = ExecutionManifest(
        render_job_id="render_0123456789abcdef",
        spec_sha256="a" * 64,
        capability_registry_version="registry-v1",
        project_id="project_0123456789abcdef",
        project_path="project.json",
        project_sha256="b" * 64,
        trace_map_path="trace.json",
        trace_map_sha256="c" * 64,
        render_path="render.mp4",
        render_sha256="d" * 64,
        inspection_path="inspection.json",
        inspection_sha256="e" * 64,
        inspection_binding_path="inspection_binding.json",
        inspection_binding_sha256="f" * 64,
        inspection_passed=True,
    )
    assert manifest.inspection_passed
