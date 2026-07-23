from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import wave
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import quote

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import BlobResourceContents, TextResourceContents
from pydantic import AnyUrl

from components.image_acquisition import (
    ImageAcquisitionResult,
    ImageCandidate,
    ImageSearchResult,
    ImageSource,
)
from components.render_inspection import InspectionCheck, RenderInspectionReport
from components.video_download import DownloadConfig, DownloadResult
from video_create_plugin.errors import PluginError
from video_create_plugin.knowledge import KnowledgeRecord, PublicationRequest
from video_create_plugin.mcp.application import PluginApplication
from video_create_plugin.mcp.server import build_server
from video_create_plugin.models import ArtifactRef

PROJECT_ROOT = Path(__file__).parents[1]


def test_in_memory_initialize_resources_and_tools(tmp_path: Path) -> None:
    async def scenario() -> None:
        media_root = tmp_path / "media"
        media_root.mkdir()
        server = build_server(
            output_root=tmp_path / "plugin-output",
            media_roots=(media_root,),
        )
        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as session:
            resources = await session.list_resources()
            resource_uris = {str(resource.uri) for resource in resources.resources}
            assert "huayang://catalog" in resource_uris
            assert "huayang://rules/main-agent" in resource_uris
            assert "huayang://skills/video-task-router" in resource_uris
            assert "huayang://schemas/reference-study" in resource_uris

            templates = await session.list_resource_templates()
            template_uris = {str(template.uriTemplate) for template in templates.resourceTemplates}
            assert "huayang://tasks/{task_id}/stage-envelope" in template_uris
            assert "huayang://stage-access/{access_handle}/artifacts/{artifact_id}" in template_uris
            assert (
                "huayang://stage-access/{access_handle}/evidence/"
                "{analysis_id}/{evidence_id}" in template_uris
            )

            catalog = await session.read_resource(AnyUrl("huayang://catalog"))
            catalog_content = cast(TextResourceContents, catalog.contents[0])
            catalog_payload = json.loads(catalog_content.text)
            assert any(item["uri"] == "huayang://rules/main-agent" for item in catalog_payload)

            rule = await session.read_resource(AnyUrl("huayang://rules/main-agent"))
            rule_content = cast(TextResourceContents, rule.contents[0])
            assert "主 Agent" in rule_content.text

            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            assert {
                "workflow_create_task",
                "workflow_get_stage_envelope",
                "reference_get_creation_context",
                "analysis_start",
                "video_download",
                "knowledge_search",
                "materials_search",
                "images_list_sources",
                "images_search",
                "images_acquire",
                "media_preprocess",
                "editor_create_project",
                "editor_import_asset",
                "editor_apply_commands",
                "editor_compile_spec",
                "editor_validate_execution_project",
                "editor_inspect_render",
            } <= tool_names
            tool_by_name = {tool.name: tool for tool in tools.tools}
            assert set(tool_by_name["editor_preflight_spec"].inputSchema["properties"]) == {
                "access_handle"
            }
            assert set(tool_by_name["editor_compile_spec"].inputSchema["properties"]) == {
                "access_handle"
            }
            assert set(
                tool_by_name["editor_validate_execution_project"].inputSchema["properties"]
            ) == {"access_handle", "project_id"}
            assert set(tool_by_name["editor_inspect_render"].inputSchema["properties"]) == {
                "access_handle",
                "render_job_id",
            }

    anyio.run(scenario)


def test_in_process_video_download_maps_stable_component_result(tmp_path: Path) -> None:
    calls: list[tuple[str, Path]] = []

    def fake_download(source: str, config: DownloadConfig) -> DownloadResult:
        calls.append((source, config.output_dir))
        config.output_dir.mkdir(parents=True, exist_ok=True)
        path = config.output_dir / "fixture.mp4"
        path.write_bytes(b"fixture-video")
        return DownloadResult(
            platform="Fixture",
            source_url="https://example.test/share/1",
            canonical_url="https://example.test/video/1",
            video_id="fixture-1",
            summary="fixture",
            timestamp="20260722_120000",
            timestamp_source="downloaded_at",
            title="Fixture Video",
            author="Fixture Author",
            duration_seconds=1.25,
            file_path=path,
        )

    app = PluginApplication(
        tmp_path / "plugin-output",
        project_root=PROJECT_ROOT,
        media_roots=(tmp_path,),
        video_downloader=fake_download,
    )

    async def scenario() -> None:
        server = build_server(application=app)
        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as session:
            created = await session.call_tool(
                "workflow_create_task",
                {"task_type": "reference_study"},
            )
            task_id = str(_tool_payload(created.structuredContent)["data"]["task_id"])
            envelope = await session.call_tool(
                "workflow_get_stage_envelope",
                {"task_id": task_id},
            )
            handle = str(_tool_payload(envelope.structuredContent)["data"]["stage_access_handle"])
            downloaded = await session.call_tool(
                "video_download",
                {
                    "access_handle": handle,
                    "source": "分享文本 https://example.test/share/1",
                },
            )
            payload = _tool_payload(downloaded.structuredContent)
            assert payload["ok"] is True
            assert payload["data"] == {
                "platform": "Fixture",
                "source_url": "https://example.test/share/1",
                "canonical_url": "https://example.test/video/1",
                "video_id": "fixture-1",
                "summary": "fixture",
                "timestamp": "20260722_120000",
                "timestamp_source": "downloaded_at",
                "title": "Fixture Video",
                "author": "Fixture Author",
                "duration_seconds": 1.25,
                "file_path": str(
                    tmp_path
                    / "plugin-output"
                    / "tasks"
                    / task_id
                    / "references"
                    / "downloads"
                    / "fixture.mp4"
                ),
            }
            assert calls == [
                (
                    "分享文本 https://example.test/share/1",
                    tmp_path / "plugin-output" / "tasks" / task_id / "references" / "downloads",
                )
            ]

    anyio.run(scenario)


def test_stage_evidence_resource_uses_manifest_whitelist(tmp_path: Path) -> None:
    output_root = tmp_path / "plugin-output"
    app = PluginApplication(
        output_root,
        project_root=PROJECT_ROOT,
        media_roots=(tmp_path,),
    )
    task = app.workflow.create_task("reference_study")
    envelope = app.workflow.get_stage_envelope(task.task_id)
    analysis_id = "reference_fixture"
    job_dir = output_root / "tasks" / task.task_id / "analysis" / "jobs" / analysis_id
    evidence_path = job_dir / "visual" / "evidence.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text('{"measured": true}\n', encoding="utf-8")
    evidence_bytes = evidence_path.read_bytes()
    manifest_path = job_dir / "reference_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "evidence_bundle": {
                    "entries": [
                        {
                            "path": "visual/evidence.json",
                            "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
                            "size_bytes": len(evidence_bytes),
                        }
                    ]
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = SimpleNamespace(
        job_dir=job_dir,
        reference_manifest_path=manifest_path,
        reference_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )

    class StaticAnalysisService:
        def validate(self, requested_analysis_id: str) -> SimpleNamespace:
            assert requested_analysis_id == analysis_id
            return result

    app._analysis_services[task.task_id] = cast(Any, StaticAnalysisService())

    async def scenario() -> None:
        server = build_server(application=app)
        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as session:
            manifest = await session.read_resource(
                AnyUrl(
                    "huayang://stage-access/"
                    f"{envelope.stage_access_handle}/evidence/{analysis_id}/"
                    "reference_manifest.json"
                )
            )
            manifest_content = cast(BlobResourceContents, manifest.contents[0])
            assert base64.b64decode(manifest_content.blob) == manifest_path.read_bytes()

            encoded_path = quote("visual/evidence.json", safe="")
            evidence = await session.read_resource(
                AnyUrl(
                    "huayang://stage-access/"
                    f"{envelope.stage_access_handle}/evidence/{analysis_id}/{encoded_path}"
                )
            )
            evidence_content = cast(BlobResourceContents, evidence.contents[0])
            assert base64.b64decode(evidence_content.blob) == evidence_bytes

        with pytest.raises(PluginError) as traversal:
            app.read_stage_evidence(
                envelope.stage_access_handle,
                analysis_id,
                "../workflow.sqlite3",
            )
        assert traversal.value.code == "evidence_not_found"

    anyio.run(scenario)


def test_in_process_resource_preparation_image_and_preprocess_tools(
    tmp_path: Path,
) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    source_video = _generate_small_video(media_root / "source.mp4")
    output_root = tmp_path / "plugin-output"
    image_roots: list[Path] = []

    class FakeImageService:
        def __init__(self, root: Path) -> None:
            self.root = root

        def list_sources(self) -> tuple[ImageSource, ...]:
            return (
                ImageSource(
                    name="fixture_images",
                    display_name="Fixture Images",
                    api_url="https://example.test/api",
                    source_page_url="https://example.test/source",
                    access_mode="test_injected",
                ),
            )

        def search(self, query: str, *, limit: int = 6) -> ImageSearchResult:
            assert query == "城市夜景"
            assert limit == 1
            return ImageSearchResult(
                query=query,
                source="fixture_images",
                candidates=(
                    ImageCandidate(
                        candidate_ref="image_fixture_ref",
                        provider="fixture_images",
                        provider_asset_id="fixture-1",
                        title="Fixture City",
                        creator="Fixture Creator",
                        source_url="https://example.test/image/1",
                        license="CC BY 4.0",
                        license_url="https://creativecommons.org/licenses/by/4.0",
                        mime_type="image/png",
                        width=1,
                        height=1,
                    ),
                ),
            )

        def acquire(self, candidate_ref: str) -> ImageAcquisitionResult:
            assert candidate_ref == "image_fixture_ref"
            image_path = self.root / "images" / "fixture.png"
            provenance_path = self.root / "provenance" / "fixture.json"
            image_path.parent.mkdir(parents=True, exist_ok=True)
            provenance_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(b"fixture-image")
            provenance_path.write_text("{}\n", encoding="utf-8")
            return ImageAcquisitionResult(
                candidate_ref=candidate_ref,
                file_path=image_path,
                provenance_path=provenance_path,
                sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
                size_bytes=image_path.stat().st_size,
                provider="fixture_images",
                provider_asset_id="fixture-1",
                title="Fixture City",
                creator="Fixture Creator",
                source_url="https://example.test/image/1",
                license="CC BY 4.0",
                license_url="https://creativecommons.org/licenses/by/4.0",
                mime_type="image/png",
                width=1,
                height=1,
            )

    def image_factory(root: Path) -> Any:
        image_roots.append(root)
        return FakeImageService(root)

    app = PluginApplication(
        output_root,
        project_root=PROJECT_ROOT,
        media_roots=(media_root,),
        image_service_factory=cast(Any, image_factory),
    )
    task_id, access_handle = _seed_resource_preparation_stage(app)

    async def scenario() -> None:
        server = build_server(application=app)
        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as session:
            sources = await session.call_tool(
                "images_list_sources",
                {"access_handle": access_handle},
            )
            assert _tool_payload(sources.structuredContent)["data"][0]["name"] == "fixture_images"

            searched = await session.call_tool(
                "images_search",
                {
                    "access_handle": access_handle,
                    "query": "城市夜景",
                    "limit": 1,
                },
            )
            candidate_ref = _tool_payload(searched.structuredContent)["data"]["candidates"][0][
                "candidate_ref"
            ]
            acquired = await session.call_tool(
                "images_acquire",
                {
                    "access_handle": access_handle,
                    "candidate_ref": candidate_ref,
                },
            )
            acquired_data = _tool_payload(acquired.structuredContent)["data"]
            assert Path(acquired_data["file_path"]).is_relative_to(output_root / "tasks" / task_id)

            wrong_sha = await session.call_tool(
                "media_preprocess",
                {
                    "access_handle": access_handle,
                    "request": {
                        "operation": "frame_extract",
                        "input_path": str(source_video),
                        "input_sha256": "0" * 64,
                        "timestamp_seconds": 0.1,
                    },
                },
            )
            wrong_sha_payload = _tool_payload(wrong_sha.structuredContent)
            assert wrong_sha_payload["ok"] is False
            assert wrong_sha_payload["error"]["code"] == "input_sha256_mismatch"

            outside = tmp_path / "outside.mp4"
            outside.write_bytes(source_video.read_bytes())
            outside_result = await session.call_tool(
                "media_preprocess",
                {
                    "access_handle": access_handle,
                    "request": {
                        "operation": "frame_extract",
                        "input_path": str(outside),
                        "input_sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
                        "timestamp_seconds": 0.1,
                    },
                },
            )
            outside_payload = _tool_payload(outside_result.structuredContent)
            assert outside_payload["ok"] is False
            assert outside_payload["error"]["code"] == "media_path_not_allowed"

            processed = await session.call_tool(
                "media_preprocess",
                {
                    "access_handle": access_handle,
                    "request": {
                        "operation": "frame_extract",
                        "input_path": str(source_video),
                        "input_sha256": hashlib.sha256(source_video.read_bytes()).hexdigest(),
                        "timestamp_seconds": 0.1,
                    },
                },
            )
            processed_data = _tool_payload(processed.structuredContent)["data"]
            assert processed_data["operation"] == "frame_extract"
            assert processed_data["mime_type"] == "image/png"
            assert Path(processed_data["output_path"]).is_relative_to(
                output_root / "tasks" / task_id / "resources" / "preprocessed"
            )

    anyio.run(scenario)
    assert image_roots == [output_root / "tasks" / task_id / "resources" / "images"]


def test_workflow_stage_access_and_artifact_resource(tmp_path: Path) -> None:
    async def scenario() -> None:
        media_root = tmp_path / "media"
        media_root.mkdir()
        server = build_server(
            output_root=tmp_path / "plugin-output",
            media_roots=(media_root,),
        )
        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as session:
            created = await session.call_tool(
                "workflow_create_task",
                {"task_type": "reference_study"},
            )
            created_payload = _tool_payload(created.structuredContent)
            task_id = str(created_payload["data"]["task_id"])

            stage_resource = await session.read_resource(
                AnyUrl(f"huayang://tasks/{task_id}/stage-envelope")
            )
            stage_content = cast(TextResourceContents, stage_resource.contents[0])
            stage_payload = json.loads(stage_content.text)
            handle = str(stage_payload["stage_access_handle"])
            assert stage_payload["stage"] == "reference_study"
            assert "analysis_start" in stage_payload["allowed_tools"]

            forbidden = await session.call_tool(
                "materials_list_sources",
                {"access_handle": handle},
            )
            forbidden_payload = _tool_payload(forbidden.structuredContent)
            assert forbidden_payload == {
                "ok": False,
                "error": {
                    "code": "stage_not_allowed",
                    "message": "工具不属于当前阶段",
                    "details": {},
                },
            }

            forbidden_editor = await session.call_tool(
                "editor_create_project",
                {"access_handle": handle, "name": "越阶段工程"},
            )
            forbidden_editor_payload = _tool_payload(forbidden_editor.structuredContent)
            assert forbidden_editor_payload["ok"] is False
            assert forbidden_editor_payload["error"]["code"] == "stage_not_allowed"

            for tool_name, arguments in (
                ("images_list_sources", {"access_handle": handle}),
                (
                    "media_preprocess",
                    {"access_handle": handle, "request": {}},
                ),
            ):
                forbidden_resource = await session.call_tool(tool_name, arguments)
                forbidden_resource_payload = _tool_payload(forbidden_resource.structuredContent)
                assert forbidden_resource_payload["ok"] is False
                assert forbidden_resource_payload["error"]["code"] == ("stage_not_allowed")

            submitted = await session.call_tool(
                "workflow_submit_artifact",
                {
                    "access_handle": handle,
                    "artifact_type": "test-evidence",
                    "content": "deterministic artifact",
                    "schema_version": "1.0",
                    "producer_kind": "component",
                    "producer_id": "mcp-test",
                    "component_version": "1.0",
                },
            )
            submitted_payload = _tool_payload(submitted.structuredContent)
            artifact_id = str(submitted_payload["data"]["artifact_id"])

            new_envelope = await session.call_tool(
                "workflow_get_stage_envelope",
                {"task_id": task_id},
            )
            envelope_payload = _tool_payload(new_envelope.structuredContent)
            new_handle = str(envelope_payload["data"]["stage_access_handle"])
            artifact = await session.read_resource(
                AnyUrl(f"huayang://stage-access/{new_handle}/artifacts/{artifact_id}")
            )
            artifact_content = cast(BlobResourceContents, artifact.contents[0])
            assert base64.b64decode(artifact_content.blob) == b"deterministic artifact"

            stale_handle = await session.call_tool(
                "media_probe",
                {"access_handle": handle, "source_path": str(media_root / "x.mp4")},
            )
            stale_payload = _tool_payload(stale_handle.structuredContent)
            assert stale_payload["ok"] is False
            assert stale_payload["error"]["code"] == "stage_access_invalid"

            invalid_handle = await session.call_tool(
                "media_probe",
                {"access_handle": "x" * 40, "source_path": str(media_root / "x.mp4")},
            )
            invalid_payload = _tool_payload(invalid_handle.structuredContent)
            assert invalid_payload["ok"] is False
            assert invalid_payload["error"]["code"] == "stage_access_invalid"

    anyio.run(scenario)


def test_explicit_media_input_boundary(tmp_path: Path) -> None:
    async def scenario() -> None:
        media_root = tmp_path / "declared-media"
        media_root.mkdir()
        inside = media_root / "inside.mp4"
        inside.write_bytes(b"declared-media")
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"not-media")
        server = build_server(
            output_root=tmp_path / "plugin-output",
            media_roots=(media_root,),
        )
        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as session:
            created = await session.call_tool(
                "workflow_create_task",
                {"task_type": "reference_study"},
            )
            task_id = str(_tool_payload(created.structuredContent)["data"]["task_id"])
            envelope = await session.call_tool(
                "workflow_get_stage_envelope",
                {"task_id": task_id},
            )
            handle = str(_tool_payload(envelope.structuredContent)["data"]["stage_access_handle"])
            accepted = await session.call_tool(
                "reference_resolve_source",
                {"access_handle": handle, "source": str(inside)},
            )
            accepted_payload = _tool_payload(accepted.structuredContent)
            assert accepted_payload["ok"] is True
            assert accepted_payload["data"]["source_kind"] == "local_media"
            result = await session.call_tool(
                "reference_resolve_source",
                {"access_handle": handle, "source": str(outside)},
            )
            payload = _tool_payload(result.structuredContent)
            assert payload["ok"] is False
            assert payload["error"]["code"] == "media_path_not_allowed"

    anyio.run(scenario)


def test_process_stdio_initialize_and_call(tmp_path: Path) -> None:
    async def scenario() -> None:
        output_root = tmp_path / "stdio-output"
        media_root = tmp_path / "stdio-media"
        media_root.mkdir()
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "video_create_plugin.mcp.server"],
            cwd=PROJECT_ROOT,
            env={
                "HUAYANG_OUTPUT_ROOT": str(output_root),
                "HUAYANG_MEDIA_ROOTS": str(media_root),
            },
        )
        stderr_path = tmp_path / "stdio-stderr.log"
        with stderr_path.open("w", encoding="utf-8") as stderr:
            async with stdio_client(parameters, errlog=stderr) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=30),
                ) as session:
                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "huayang"
                    resources = await session.list_resources()
                    assert any(
                        str(resource.uri) == "huayang://catalog" for resource in resources.resources
                    )
                    catalog = await session.read_resource(AnyUrl("huayang://catalog"))
                    catalog_content = cast(TextResourceContents, catalog.contents[0])
                    assert "huayang://rules/main-agent" in catalog_content.text
                    tools = await session.list_tools()
                    assert any(tool.name == "workflow_create_task" for tool in tools.tools)
                    created = await session.call_tool(
                        "workflow_create_task",
                        {"task_type": "original_creation"},
                    )
                    payload = _tool_payload(created.structuredContent)
                    assert payload["ok"] is True
                    assert payload["data"]["current_stage"] == "creative_direction"

    anyio.run(scenario)


def test_process_stdio_video_download_uses_component_contract(tmp_path: Path) -> None:
    output_root = tmp_path / "stdio-download-output"
    media_root = tmp_path / "stdio-download-media"
    media_root.mkdir()
    launcher = """
from components.video_download import DownloadResult
import video_create_plugin.mcp.application as application

def fake_download(source, config):
    config.output_dir.mkdir(parents=True, exist_ok=True)
    path = config.output_dir / "stdio-fixture.mp4"
    path.write_bytes(b"stdio-fixture-video")
    return DownloadResult(
        platform="Fixture",
        source_url="https://example.test/share/stdio",
        canonical_url="https://example.test/video/stdio",
        video_id="stdio-fixture",
        summary="stdio-fixture",
        timestamp="20260722_130000",
        timestamp_source="downloaded_at",
        title="Stdio Fixture",
        author=None,
        duration_seconds=2.5,
        file_path=path,
    )

application.download_video = fake_download
from video_create_plugin.mcp.server import main
main()
"""

    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", launcher],
            cwd=PROJECT_ROOT,
            env={
                "HUAYANG_OUTPUT_ROOT": str(output_root),
                "HUAYANG_MEDIA_ROOTS": str(media_root),
            },
        )
        stderr_path = tmp_path / "stdio-download-stderr.log"
        with stderr_path.open("w", encoding="utf-8") as stderr:
            async with stdio_client(parameters, errlog=stderr) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=30),
                ) as session:
                    await session.initialize()
                    created = await session.call_tool(
                        "workflow_create_task",
                        {"task_type": "reference_study"},
                    )
                    task_id = str(_tool_payload(created.structuredContent)["data"]["task_id"])
                    envelope = await session.call_tool(
                        "workflow_get_stage_envelope",
                        {"task_id": task_id},
                    )
                    handle = str(
                        _tool_payload(envelope.structuredContent)["data"]["stage_access_handle"]
                    )
                    downloaded = await session.call_tool(
                        "video_download",
                        {
                            "access_handle": handle,
                            "source": "https://example.test/share/stdio",
                        },
                    )
                    payload = _tool_payload(downloaded.structuredContent)
                    assert payload["ok"] is True
                    assert payload["data"]["video_id"] == "stdio-fixture"
                    assert Path(payload["data"]["file_path"]).read_bytes() == b"stdio-fixture-video"

    anyio.run(scenario)


def test_process_stdio_resource_preparation_tools(tmp_path: Path) -> None:
    output_root = tmp_path / "stdio-resource-output"
    media_root = tmp_path / "stdio-resource-media"
    media_root.mkdir()
    source_video = _generate_small_video(media_root / "source.mp4")
    seed_app = PluginApplication(
        output_root,
        project_root=PROJECT_ROOT,
        media_roots=(media_root,),
    )
    task_id, access_handle = _seed_resource_preparation_stage(seed_app)
    launcher = """
import hashlib
from components.image_acquisition import (
    ImageAcquisitionResult,
    ImageCandidate,
    ImageSearchResult,
    ImageSource,
)
import video_create_plugin.mcp.application as application

class FakeImageService:
    def __init__(self, config):
        self.root = config.output_dir

    def list_sources(self):
        return (ImageSource(
            name="fixture_images",
            display_name="Fixture Images",
            api_url="https://example.test/api",
            source_page_url="https://example.test/source",
            access_mode="test_injected",
        ),)

    def search(self, query, *, limit=6):
        return ImageSearchResult(
            query=query,
            source="fixture_images",
            candidates=(ImageCandidate(
                candidate_ref="image_stdio_ref",
                provider="fixture_images",
                provider_asset_id="stdio-1",
                title="Stdio Image",
                creator="Fixture Creator",
                source_url="https://example.test/image/stdio",
                license="CC BY 4.0",
                license_url="https://creativecommons.org/licenses/by/4.0",
                mime_type="image/png",
                width=1,
                height=1,
            ),),
        )

    def acquire(self, candidate_ref):
        image_path = self.root / "images" / "stdio.png"
        provenance_path = self.root / "provenance" / "stdio.json"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"stdio-image")
        provenance_path.write_text("{}\\n", encoding="utf-8")
        return ImageAcquisitionResult(
            candidate_ref=candidate_ref,
            file_path=image_path,
            provenance_path=provenance_path,
            sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
            size_bytes=image_path.stat().st_size,
            provider="fixture_images",
            provider_asset_id="stdio-1",
            title="Stdio Image",
            creator="Fixture Creator",
            source_url="https://example.test/image/stdio",
            license="CC BY 4.0",
            license_url="https://creativecommons.org/licenses/by/4.0",
            mime_type="image/png",
            width=1,
            height=1,
        )

application.ImageAcquisitionService = FakeImageService
from video_create_plugin.mcp.server import main
main()
"""

    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", launcher],
            cwd=PROJECT_ROOT,
            env={
                "HUAYANG_OUTPUT_ROOT": str(output_root),
                "HUAYANG_MEDIA_ROOTS": str(media_root),
            },
        )
        stderr_path = tmp_path / "stdio-resource-stderr.log"
        with stderr_path.open("w", encoding="utf-8") as stderr:
            async with stdio_client(parameters, errlog=stderr) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=30),
                ) as session:
                    await session.initialize()
                    sources = await session.call_tool(
                        "images_list_sources",
                        {"access_handle": access_handle},
                    )
                    assert _tool_payload(sources.structuredContent)["data"][0]["name"] == (
                        "fixture_images"
                    )
                    searched = await session.call_tool(
                        "images_search",
                        {
                            "access_handle": access_handle,
                            "query": "城市夜景",
                            "limit": 1,
                        },
                    )
                    candidate_ref = _tool_payload(searched.structuredContent)["data"]["candidates"][
                        0
                    ]["candidate_ref"]
                    acquired = await session.call_tool(
                        "images_acquire",
                        {
                            "access_handle": access_handle,
                            "candidate_ref": candidate_ref,
                        },
                    )
                    acquired_path = Path(
                        _tool_payload(acquired.structuredContent)["data"]["file_path"]
                    )
                    assert acquired_path.is_relative_to(output_root / "tasks" / task_id)

                    processed = await session.call_tool(
                        "media_preprocess",
                        {
                            "access_handle": access_handle,
                            "request": {
                                "operation": "frame_extract",
                                "input_path": str(source_video),
                                "input_sha256": hashlib.sha256(
                                    source_video.read_bytes()
                                ).hexdigest(),
                                "timestamp_seconds": 0.1,
                            },
                        },
                    )
                    processed_data = _tool_payload(processed.structuredContent)["data"]
                    assert processed_data["operation"] == "frame_extract"
                    assert Path(processed_data["output_path"]).is_relative_to(
                        output_root / "tasks" / task_id / "resources" / "preprocessed"
                    )

    anyio.run(scenario)


def test_process_stdio_editor_public_api_lifecycle(tmp_path: Path) -> None:
    output_root = tmp_path / "stdio-editor-output"
    media_root = tmp_path / "stdio-editor-media"
    media_root.mkdir()
    image_path = media_root / "one.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+"
            "A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    specification = _image_specification(image_path)
    access_handle = _seed_execution_stage(output_root, media_root, specification)

    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "video_create_plugin.mcp.server"],
            cwd=PROJECT_ROOT,
            env={
                "HUAYANG_OUTPUT_ROOT": str(output_root),
                "HUAYANG_MEDIA_ROOTS": str(media_root),
            },
        )
        stderr_path = tmp_path / "stdio-editor-stderr.log"
        with stderr_path.open("w", encoding="utf-8") as stderr:
            async with stdio_client(parameters, errlog=stderr) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=30),
                ) as session:
                    await session.initialize()
                    created = await session.call_tool(
                        "editor_create_project",
                        {
                            "access_handle": access_handle,
                            "name": "MCP 编辑工程",
                            "canvas": {"width": 640, "height": 360, "fps": 25},
                        },
                    )
                    created_data = _tool_payload(created.structuredContent)["data"]
                    project_id = str(created_data["project"]["id"])
                    assert created_data["project"]["revision"] == 0

                    imported = await session.call_tool(
                        "editor_import_asset",
                        {
                            "access_handle": access_handle,
                            "project_id": project_id,
                            "expected_revision": 0,
                            "source_path": str(image_path),
                        },
                    )
                    imported_data = _tool_payload(imported.structuredContent)["data"]
                    assert imported_data["asset"]["kind"] == "image"
                    assert imported_data["project"]["revision"] == 1

                    batch = {
                        "expected_revision": 1,
                        "commands": [
                            {
                                "type": "track.add",
                                "media_domain": "visual",
                                "name": "主画面",
                            }
                        ],
                    }
                    applied = await session.call_tool(
                        "editor_apply_commands",
                        {
                            "access_handle": access_handle,
                            "project_id": project_id,
                            "batch": batch,
                        },
                    )
                    applied_data = _tool_payload(applied.structuredContent)["data"]
                    assert applied_data["project"]["revision"] == 2
                    assert applied_data["project"]["tracks"][0]["name"] == "主画面"

                    conflicted = await session.call_tool(
                        "editor_apply_commands",
                        {
                            "access_handle": access_handle,
                            "project_id": project_id,
                            "batch": batch,
                        },
                    )
                    conflict_payload = _tool_payload(conflicted.structuredContent)
                    assert conflict_payload["ok"] is False
                    assert conflict_payload["error"]["code"] == "revision_conflict"

                    compiled = await session.call_tool(
                        "editor_compile_spec",
                        {
                            "access_handle": access_handle,
                        },
                    )
                    compiled_data = _tool_payload(compiled.structuredContent)["data"]
                    validated = await session.call_tool(
                        "editor_validate_execution_project",
                        {
                            "access_handle": access_handle,
                            "project_id": compiled_data["project"]["id"],
                        },
                    )
                    validation_data = _tool_payload(validated.structuredContent)["data"]
                    assert validation_data == {
                        "valid": True,
                        "project_id": compiled_data["project"]["id"],
                        "project_revision": 0,
                        "spec_sha256": compiled_data["trace_map"]["spec_sha256"],
                        "action_count": 1,
                        "mapped_action_count": 1,
                    }

    anyio.run(scenario)


def test_execution_tools_use_frozen_spec_and_bound_render_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "plugin-output"
    media_root = tmp_path / "media"
    media_root.mkdir()
    image_path = media_root / "one.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+"
            "A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    specification = _image_specification(image_path)
    app = PluginApplication(
        output_root,
        project_root=PROJECT_ROOT,
        media_roots=(media_root,),
    )
    access_handle = _seed_execution_stage_in_app(app, specification)

    preflight = app.editor_preflight(access_handle)
    assert preflight["supported"] is True
    compiled = app.editor_compile(access_handle)
    project_id = str(compiled["project"]["id"])
    assert compiled["editing_artifact_ref"]["artifact_id"].startswith("artifact_")
    assert app.editor_validate_execution_project(access_handle, project_id) == {
        "valid": True,
        "project_id": project_id,
        "project_revision": 0,
        "spec_sha256": compiled["trace_map"]["spec_sha256"],
        "action_count": 1,
        "mapped_action_count": 1,
    }

    monkeypatch.setattr(app, "_start_render_worker", lambda: None)
    submitted = app.editor_submit_render(access_handle, project_id)
    render_job_id = str(submitted["id"])
    job = app.render_queue.get(render_job_id)
    output_path = Path(job.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"bound-render")
    completed = job.model_copy(
        update={
            "status": "succeeded",
            "progress": 1.0,
            "message": "渲染完成",
        }
    )
    (app.render_queue.root / render_job_id / "job.json").write_text(
        json.dumps(completed.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}

    def fake_inspect(source: Path, expectation: Any, output_dir: Path) -> Any:
        captured["source"] = source
        captured["expectation"] = expectation
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "render_inspection.json"
        contact_sheet_path = output_dir / "contact_sheet.jpg"
        report = _passed_inspection_report(source)
        report_path.write_text(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False),
            encoding="utf-8",
        )
        contact_sheet_path.write_bytes(b"sheet")
        return SimpleNamespace(
            report=report,
            report_path=report_path,
            contact_sheet_path=contact_sheet_path,
        )

    monkeypatch.setattr(app.render_inspection, "inspect", fake_inspect)
    inspected = app.editor_inspect_render(access_handle, render_job_id)
    expectation = captured["expectation"]
    assert captured["source"] == output_path.resolve()
    assert expectation.duration_us == specification["duration_us"]
    assert expectation.width == specification["canvas"]["width"]
    assert expectation.height == specification["canvas"]["height"]
    assert expectation.expected_audio is False
    assert expectation.action_count == 1
    assert expectation.traced_action_count == 1
    assert inspected["report"]["passed"] is True
    assert Path(inspected["inspection_binding_path"]).is_file()

    project_path = app.project_storage.root / project_id / "project.json"
    trace_map_path = app.project_storage.root / project_id / "trace_map.json"
    report_path = Path(str(inspected["report_path"]))
    binding_path = Path(str(inspected["inspection_binding_path"]))
    execution_manifest = {
        "schema_version": "1.0",
        "render_job_id": render_job_id,
        "spec_sha256": compiled["trace_map"]["spec_sha256"],
        "capability_registry_version": preflight["registry_version"],
        "project_id": project_id,
        "project_path": str(project_path),
        "project_sha256": hashlib.sha256(project_path.read_bytes()).hexdigest(),
        "trace_map_path": str(trace_map_path),
        "trace_map_sha256": hashlib.sha256(trace_map_path.read_bytes()).hexdigest(),
        "render_path": str(output_path),
        "render_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "inspection_path": str(report_path),
        "inspection_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "inspection_binding_path": str(binding_path),
        "inspection_binding_sha256": hashlib.sha256(binding_path.read_bytes()).hexdigest(),
        "inspection_passed": True,
    }
    task, stage = app.authorize(access_handle, "workflow_submit_artifact")
    arbitrary_project = app._task_root(task.task_id) / "arbitrary-project.json"
    arbitrary_project.parent.mkdir(parents=True, exist_ok=True)
    arbitrary_project.write_bytes(project_path.read_bytes())
    arbitrary_manifest = {
        **execution_manifest,
        "project_path": str(arbitrary_project),
        "project_sha256": hashlib.sha256(arbitrary_project.read_bytes()).hexdigest(),
    }
    with pytest.raises(PluginError) as arbitrary_error:
        app.submit_artifact(
            access_handle=access_handle,
            artifact_type="execution_manifest",
            content=json.dumps(arbitrary_manifest, ensure_ascii=False),
            schema_version="1.0",
            producer_kind="component",
            producer_id="mcp-execution-test",
            primary=True,
            parent_artifact_refs=[
                reference.model_dump(mode="json") for reference in stage.input_artifact_refs
            ],
            evidence_refs=[report_path.as_uri()],
            rule_version=None,
            skill_versions=None,
            model_id=None,
            component_version="test-v1",
        )
    assert arbitrary_error.value.code == "execution_binding_mismatch"

    authentic_binding = binding_path.read_bytes()
    forged_binding = json.loads(authentic_binding)
    forged_binding["hmac_sha256"] = "0" * 64
    binding_path.write_text(json.dumps(forged_binding), encoding="utf-8")
    forged_manifest = {
        **execution_manifest,
        "inspection_binding_sha256": hashlib.sha256(binding_path.read_bytes()).hexdigest(),
    }
    try:
        with pytest.raises(PluginError) as forged_error:
            app.submit_artifact(
                access_handle=access_handle,
                artifact_type="execution_manifest",
                content=json.dumps(forged_manifest, ensure_ascii=False),
                schema_version="1.0",
                producer_kind="component",
                producer_id="mcp-execution-test",
                primary=True,
                parent_artifact_refs=[
                    reference.model_dump(mode="json") for reference in stage.input_artifact_refs
                ],
                evidence_refs=[report_path.as_uri()],
                rule_version=None,
                skill_versions=None,
                model_id=None,
                component_version="test-v1",
            )
        assert forged_error.value.code == "inspection_binding_invalid"
    finally:
        binding_path.write_bytes(authentic_binding)

    restarted = PluginApplication(
        output_root,
        project_root=PROJECT_ROOT,
        media_roots=(media_root,),
    )
    artifact = restarted.submit_artifact(
        access_handle=access_handle,
        artifact_type="execution_manifest",
        content=json.dumps(execution_manifest, ensure_ascii=False),
        schema_version="1.0",
        producer_kind="component",
        producer_id="mcp-execution-test",
        primary=True,
        parent_artifact_refs=[
            reference.model_dump(mode="json") for reference in stage.input_artifact_refs
        ],
        evidence_refs=[report_path.as_uri()],
        rule_version=None,
        skill_versions=None,
        model_id=None,
        component_version="test-v1",
    )
    assert artifact["task_id"] == task.task_id
    assert artifact["artifact_type"] == "execution_manifest"


def test_execution_rejects_replacement_project_and_tampered_frozen_spec(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "plugin-output"
    media_root = tmp_path / "media"
    media_root.mkdir()
    image_path = media_root / "one.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+"
            "A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    app = PluginApplication(
        output_root,
        project_root=PROJECT_ROOT,
        media_roots=(media_root,),
    )
    first_specification = _image_specification(image_path)
    first_handle = _seed_execution_stage_in_app(app, first_specification)
    second_specification = {
        **_image_specification(image_path),
        "spec_id": "spec_replacement",
        "title": "替换规格",
    }
    second_handle = _seed_execution_stage_in_app(app, second_specification)
    replacement = app.editor_compile(second_handle)

    with pytest.raises(PluginError) as replacement_error:
        app.editor_validate_execution_project(
            first_handle,
            str(replacement["project"]["id"]),
        )
    assert replacement_error.value.code == "compiled_project_not_bound"

    _, stage = app.authorize(first_handle, "editor_preflight_spec")
    editing_ref = next(
        reference
        for reference in stage.input_artifact_refs
        if app.repository.get_artifact(reference.artifact_id).artifact_type
        == "editing_specification"
    )
    object_path = app.artifacts.root / editing_ref.sha256[:2] / editing_ref.sha256
    object_path.write_text(json.dumps(second_specification), encoding="utf-8")
    with pytest.raises(PluginError) as tamper_error:
        app.editor_preflight(first_handle)
    assert tamper_error.value.code == "artifact_hash_mismatch"


def test_mcp_knowledge_search_returns_auditable_retrieval_for_stage_artifact(
    tmp_path: Path,
) -> None:
    app = PluginApplication(
        tmp_path / "plugin-output",
        project_root=PROJECT_ROOT,
        media_roots=(tmp_path,),
    )
    report_ref = ArtifactRef(
        artifact_id="artifact_0123456789abcdef",
        revision=1,
        sha256="a" * 64,
    )
    record = KnowledgeRecord.model_validate(
        {
            "collection": "creation_knowledge",
            "source_task_id": "task_reference_source",
            "source_report_ref": report_ref,
            "source_artifact_refs": [report_ref],
            "analysis_version": "v1",
            "applicable_stages": ["stage1"],
            "knowledge_type": "core_mechanism",
            "visibility": "creation_shared",
            "transferability": "reusable_mechanism",
            "content": "约一秒硬切之间用短时画中画形成局部密度峰值",
            "evidence_refs": ["evidence://unit/1"],
            "fact_status": "inference",
            "confidence": 0.9,
            "granularity": "rhythm_unit",
        }
    )
    app.knowledge_store.publish(
        PublicationRequest(
            source_task_id="task_reference_source",
            source_report_ref=report_ref,
            source_media_sha256="b" * 64,
            publication_revision=1,
            freeze_id="freeze_reference",
            records=[record],
        )
    )

    async def scenario() -> None:
        server = build_server(application=app)
        async with create_connected_server_and_client_session(
            server,
            raise_exceptions=True,
        ) as session:
            created = await session.call_tool(
                "workflow_create_task",
                {"task_type": "original_creation"},
            )
            task_id = str(_tool_payload(created.structuredContent)["data"]["task_id"])
            envelope = await session.call_tool(
                "workflow_get_stage_envelope",
                {"task_id": task_id},
            )
            handle = str(_tool_payload(envelope.structuredContent)["data"]["stage_access_handle"])
            searched = await session.call_tool(
                "knowledge_search",
                {
                    "access_handle": handle,
                    "query": {
                        "text": "一秒硬切与画中画",
                        "knowledge_types": ["core_mechanism"],
                    },
                },
            )
            search_data = _tool_payload(searched.structuredContent)["data"]
            assert search_data["retrieval"]["retrieval_id"]
            assert search_data["retrieval"]["stage"] == "stage1"
            assert search_data["result"]["shared_creation_knowledge"]

            direction = (
                "# 节奏城市\n\n"
                "## 用户意图\n制作一秒硬切并带画中画的短视频\n\n"
                "## 视频类型与核心机制\n节奏型短视频。主镜头按稳定节奏硬切，辅助层形成局部密度峰值\n\n"
                "## 整体制作方法\n先组织音乐能量，再配置画面主次\n\n"
                "## 视觉语言与画面组织\n主体清晰、画中画短暂进入\n\n"
                "## 节奏与声音\n重拍切换，新音色触发辅助层\n\n"
                "## 转场与镜头连接\n主画面硬切，镜内叠层连续\n\n"
                "## 素材与音乐性质\n运动方向协调、瞬态清晰\n\n"
                "## 预期观看体验\n紧凑、丰富、仍可辨认\n"
            )
            submitted = await session.call_tool(
                "workflow_submit_artifact",
                {
                    "access_handle": handle,
                    "artifact_type": "creative_direction",
                    "content": direction,
                    "schema_version": "1.0",
                    "producer_kind": "agent",
                    "producer_id": "mcp-test-agent",
                    "model_id": "test-model",
                    "primary": True,
                },
            )
            assert _tool_payload(submitted.structuredContent)["ok"] is True

    anyio.run(scenario)


def _tool_payload(value: dict[str, Any] | None) -> dict[str, Any]:
    assert value is not None
    return value


def _passed_inspection_report(source: Path) -> RenderInspectionReport:
    checks = [
        InspectionCheck(code=code, passed=True, message="通过")
        for code in (
            "duration",
            "canvas",
            "fps",
            "decode",
            "black_frames",
            "freeze_run",
            "audio_presence",
            "asset_diversity",
            "trace_coverage",
            "hard_cut_boundaries",
            "beat_alignment",
            "overlay_events",
        )
    ]
    return RenderInspectionReport(
        algorithm_version="render-inspection-test",
        source_path=str(source.resolve()),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        passed=True,
        checks=checks,
        video_metrics={},
        audio_metrics={},
        overlay_metrics=[],
    )


def _seed_execution_stage(
    output_root: Path,
    media_root: Path,
    specification: dict[str, Any],
) -> str:
    app = PluginApplication(
        output_root,
        project_root=PROJECT_ROOT,
        media_roots=(media_root,),
    )
    return _seed_execution_stage_in_app(app, specification)


def _seed_execution_stage_in_app(
    app: PluginApplication,
    specification: dict[str, Any],
) -> str:
    task = app.workflow.create_task("original_creation")
    for stage_name in (
        "creative_direction",
        "resource_preparation",
        "editing_specification",
    ):
        envelope = app.workflow.get_stage_envelope(task.task_id)
        assert envelope.stage == stage_name
        if stage_name == "resource_preparation":
            content = _preparation_for_specification(specification)
            artifact_type = "preparation_package"
        elif stage_name == "editing_specification":
            content = specification
            artifact_type = "editing_specification"
        else:
            content = {"stage": stage_name}
            artifact_type = "creative_direction"
        artifact = app.artifacts.put_text(json.dumps(content))
        app.workflow.submit_artifact(
            access_handle=envelope.stage_access_handle,
            artifact_type=artifact_type,
            content=artifact,
            schema_version="test",
            producer_kind="component",
            producer_id="mcp-test-seed",
            primary=True,
            component_version="test",
        )
        approval_envelope = app.workflow.get_stage_envelope(task.task_id)
        app.workflow.record_approval(
            access_handle=approval_envelope.stage_access_handle,
            user_confirmation_ref=f"test://approval/{stage_name}",
            confirmation_assurance="host_verified",
            host_approval_receipt=f"test-receipt-{stage_name}",
        )
    execution_envelope = app.workflow.get_stage_envelope(task.task_id)
    assert execution_envelope.stage == "execution"
    return execution_envelope.stage_access_handle


def _preparation_for_specification(specification: dict[str, Any]) -> dict[str, Any]:
    first_path = Path(str(specification["assets"][0]["path"]))
    bgm_path = first_path.with_name("test-bgm.wav")
    with wave.open(str(bgm_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\x00\x00" * 8_000)

    materials = []
    for asset in specification["assets"]:
        if asset["kind"] == "audio":
            continue
        usable_end = int(asset.get("duration_us") or specification["duration_us"])
        materials.append(
            {
                **asset,
                "content_summary": "测试视觉素材",
                "selection_traits": ["测试"],
                "usable_source_ranges": [{"start_us": 0, "end_us": usable_end}],
                "source_url": "test://visual/source",
                "provider": "test",
                "creator": "test",
                "license_record": "test-license",
            }
        )
    return {
        "schema_version": "1.0",
        "materials": materials,
        "bgm": {
            "asset_id": "material_test_bgm",
            "name": "测试 BGM",
            "path": str(bgm_path),
            "sha256": hashlib.sha256(bgm_path.read_bytes()).hexdigest(),
            "duration_us": 1_000_000,
            "source_url": "test://bgm/source",
            "provider": "test",
            "creator": "test",
            "license_record": "test-license",
            "provenance_ref": "test://bgm/provenance",
            "audio_analysis_ref": "test://bgm/analysis",
            "mood_traits": ["测试"],
            "tempo_candidates_bpm": [120.0],
            "beat_grid_us": specification["beat_grid_us"],
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
            *(asset["provenance_ref"] for asset in materials),
            "test://bgm/provenance",
        ],
        "retrieval_ids": ["retrieval_stage2_test"],
    }


def _seed_resource_preparation_stage(app: PluginApplication) -> tuple[str, str]:
    task = app.workflow.create_task("original_creation")
    envelope = app.workflow.get_stage_envelope(task.task_id)
    artifact = app.artifacts.put_text('{"stage":"creative_direction"}')
    app.workflow.submit_artifact(
        access_handle=envelope.stage_access_handle,
        artifact_type="creative_direction",
        content=artifact,
        schema_version="test",
        producer_kind="component",
        producer_id="mcp-test-seed",
        primary=True,
        component_version="test",
    )
    approval_envelope = app.workflow.get_stage_envelope(task.task_id)
    app.workflow.record_approval(
        access_handle=approval_envelope.stage_access_handle,
        user_confirmation_ref="test://approval/creative-direction",
        confirmation_assurance="host_verified",
        host_approval_receipt="test-receipt-creative-direction",
    )
    resource_envelope = app.workflow.get_stage_envelope(task.task_id)
    assert resource_envelope.stage == "resource_preparation"
    return task.task_id, resource_envelope.stage_access_handle


def _generate_small_video(path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is required")
    completed = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=32x32:rate=10:duration=0.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    return path


def _image_specification(image_path: Path) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "spec_id": "spec_mcp_stdio",
        "title": "MCP 规格工程",
        "canvas": {"width": 1, "height": 1, "fps": 25},
        "duration_us": 1_000_000,
        "assets": [
            {
                "asset_id": "material_image",
                "kind": "image",
                "name": "单像素图片",
                "path": str(image_path),
                "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                "width": 1,
                "height": 1,
                "provenance_ref": "test://media/one-pixel",
            }
        ],
        "shots": [
            {
                "shot_id": "shot_1",
                "timeline": {"start_us": 0, "end_us": 1_000_000},
                "main_action_id": "action_main",
                "action_ids": ["action_main"],
                "human_description": "图片完整覆盖时间线",
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
        "retrieval_ids": ["retrieval_mcp_stdio"],
    }
