from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ValidationError

from video_create_plugin.errors import PluginError
from video_create_plugin.models import ConfirmationAssurance, TaskType

from .application import PluginApplication, default_application, validation_error

_T = TypeVar("_T")
ToolResponse = dict[str, Any]


def build_server(
    *,
    application: PluginApplication | None = None,
    output_root: Path | str | None = None,
    media_roots: tuple[Path | str, ...] = (),
) -> FastMCP:
    if application is not None and output_root is not None:
        raise ValueError("application 与 output_root 只能设置一个")
    if application is None:
        if output_root is None:
            application = default_application()
        else:
            application = PluginApplication(
                output_root,
                project_root=Path(__file__).parents[2],
                media_roots=media_roots,
            )
    app = application
    server = FastMCP(
        "huayang",
        instructions=(
            "先读取 huayang://catalog，再创建任务并获取当前阶段 envelope；"
            "阶段工具只使用 envelope 返回的 stage_access_handle。"
        ),
        log_level="WARNING",
    )
    _register_resources(server, app)
    _register_tools(server, app)
    return server


def _register_resources(server: FastMCP, app: PluginApplication) -> None:
    @server.resource(
        "huayang://catalog",
        name="huayang-catalog",
        title="视频创作上下文目录",
        description="当前可用的 rules、skills 与 schemas 目录",
        mime_type="application/json",
    )
    def catalog_resource() -> str:
        return json.dumps(app.catalog(), ensure_ascii=False, indent=2)

    for resource in app.context.catalog():
        reader = _fixed_resource_reader(app, resource.uri)
        server.resource(
            resource.uri,
            name=f"huayang-{resource.kind}-{resource.resource_id}",
            title=resource.title,
            description=resource.description,
            mime_type=("application/schema+json" if resource.kind == "schema" else "text/markdown"),
        )(reader)

    @server.resource(
        "huayang://tasks/{task_id}/stage-envelope",
        name="huayang-task-stage",
        title="任务当前阶段 Envelope",
        description="读取任务当前阶段及短期阶段访问句柄",
        mime_type="application/json",
    )
    def task_stage_resource(task_id: str) -> str:
        return json.dumps(
            app.get_stage_envelope(task_id),
            ensure_ascii=False,
            indent=2,
        )

    @server.resource(
        "huayang://stage-access/{access_handle}/artifacts/{artifact_id}",
        name="huayang-stage-artifact",
        title="阶段 Artifact",
        description="使用当前阶段访问句柄读取阶段输入或输出 Artifact",
        mime_type="application/octet-stream",
    )
    def stage_artifact_resource(access_handle: str, artifact_id: str) -> bytes:
        return app.read_stage_artifact(access_handle, artifact_id)

    @server.resource(
        "huayang://stage-access/{access_handle}/evidence/{analysis_id}/{evidence_id}",
        name="huayang-stage-evidence",
        title="阶段分析证据",
        description="使用阶段访问句柄和证据清单中的相对路径读取分析证据",
        mime_type="application/octet-stream",
    )
    def stage_evidence_resource(
        access_handle: str,
        analysis_id: str,
        evidence_id: str,
    ) -> bytes:
        return app.read_stage_evidence(access_handle, analysis_id, evidence_id)


def _register_tools(server: FastMCP, app: PluginApplication) -> None:
    @server.tool(name="context_catalog", structured_output=True)
    def context_catalog() -> ToolResponse:
        return _invoke(app.catalog)

    @server.tool(name="context_read", structured_output=True)
    def context_read(uri: str) -> ToolResponse:
        return _invoke(lambda: app.context_read(uri))

    @server.tool(name="context_stage_bundle", structured_output=True)
    def context_stage_bundle(task_type: TaskType, stage: str) -> ToolResponse:
        return _invoke(lambda: app.stage_bundle(task_type, stage))

    @server.tool(name="workflow_create_task", structured_output=True)
    def workflow_create_task(
        task_type: TaskType,
        reference_analysis_ids: list[str] | None = None,
    ) -> ToolResponse:
        return _invoke(lambda: app.create_task(task_type, reference_analysis_ids))

    @server.tool(name="workflow_get_task", structured_output=True)
    def workflow_get_task(task_id: str) -> ToolResponse:
        return _invoke(lambda: app.get_task(task_id))

    @server.tool(name="workflow_get_stage_envelope", structured_output=True)
    def workflow_get_stage_envelope(task_id: str) -> ToolResponse:
        return _invoke(lambda: app.get_stage_envelope(task_id))

    @server.tool(name="reference_get_creation_context", structured_output=True)
    def reference_get_creation_context(access_handle: str) -> ToolResponse:
        return _invoke(lambda: app.reference_creation_context(access_handle))

    @server.tool(name="workflow_submit_artifact", structured_output=True)
    def workflow_submit_artifact(
        access_handle: str,
        artifact_type: str,
        content: str,
        schema_version: str,
        producer_kind: Literal["agent", "component"],
        producer_id: str,
        primary: bool = False,
        parent_artifact_refs: list[dict[str, Any]] | None = None,
        evidence_refs: list[str] | None = None,
        rule_version: str | None = None,
        skill_versions: list[str] | None = None,
        model_id: str | None = None,
        component_version: str | None = None,
    ) -> ToolResponse:
        return _invoke(
            lambda: app.submit_artifact(
                access_handle=access_handle,
                artifact_type=artifact_type,
                content=content,
                schema_version=schema_version,
                producer_kind=producer_kind,
                producer_id=producer_id,
                primary=primary,
                parent_artifact_refs=parent_artifact_refs,
                evidence_refs=evidence_refs,
                rule_version=rule_version,
                skill_versions=skill_versions,
                model_id=model_id,
                component_version=component_version,
            )
        )

    @server.tool(name="workflow_record_approval", structured_output=True)
    def workflow_record_approval(
        access_handle: str,
        user_confirmation_ref: str,
        confirmation_assurance: ConfirmationAssurance,
        host_approval_receipt: str | None = None,
    ) -> ToolResponse:
        return _invoke(
            lambda: app.record_approval(
                access_handle=access_handle,
                user_confirmation_ref=user_confirmation_ref,
                confirmation_assurance=confirmation_assurance,
                host_approval_receipt=host_approval_receipt,
            )
        )

    @server.tool(name="workflow_reopen_stage", structured_output=True)
    def workflow_reopen_stage(access_handle: str, stage_type: str) -> ToolResponse:
        return _invoke(
            lambda: app.reopen_stage(
                access_handle=access_handle,
                stage_type=stage_type,
            )
        )

    @server.tool(name="reference_resolve_source", structured_output=True)
    def reference_resolve_source(access_handle: str, source: str) -> ToolResponse:
        return _invoke(lambda: app.resolve_reference(access_handle, source))

    @server.tool(name="video_download", structured_output=True)
    def video_download(access_handle: str, source: str) -> ToolResponse:
        return _invoke(lambda: app.video_download(access_handle, source))

    @server.tool(name="media_probe", structured_output=True)
    def media_probe(access_handle: str, source_path: str) -> ToolResponse:
        return _invoke(lambda: app.probe_media(access_handle, source_path))

    @server.tool(name="analysis_start", structured_output=True)
    def analysis_start(
        access_handle: str,
        source_path: str,
        modalities: list[Literal["video", "audio"]] | None = None,
    ) -> ToolResponse:
        return _invoke(
            lambda: app.analysis_start(
                access_handle,
                source_path,
                modalities or ["video", "audio"],
            )
        )

    @server.tool(name="analysis_get_job", structured_output=True)
    def analysis_get_job(access_handle: str, job_id: str) -> ToolResponse:
        return _invoke(lambda: app.analysis_get_job(access_handle, job_id))

    @server.tool(name="analysis_refine_intervals", structured_output=True)
    def analysis_refine_intervals(
        access_handle: str,
        job_id: str,
        intervals: list[dict[str, Any]],
    ) -> ToolResponse:
        return _invoke(
            lambda: app.analysis_refine_intervals(
                access_handle,
                job_id,
                intervals,
            )
        )

    @server.tool(name="analysis_validate_artifact", structured_output=True)
    def analysis_validate_artifact(
        access_handle: str,
        artifact_path: str,
    ) -> ToolResponse:
        return _invoke(lambda: app.analysis_validate_artifact(access_handle, artifact_path))

    @server.tool(name="report_generate", structured_output=True)
    def report_generate(
        access_handle: str,
        manifest: dict[str, Any],
    ) -> ToolResponse:
        return _invoke(lambda: app.report_generate(access_handle, manifest))

    @server.tool(name="knowledge_preview_publication", structured_output=True)
    def knowledge_preview_publication(
        access_handle: str,
        request: dict[str, Any],
    ) -> ToolResponse:
        return _invoke(lambda: app.knowledge_preview(access_handle, request))

    @server.tool(name="knowledge_publish", structured_output=True)
    def knowledge_publish(
        access_handle: str,
        request: dict[str, Any],
    ) -> ToolResponse:
        return _invoke(lambda: app.knowledge_publish(access_handle, request))

    @server.tool(name="knowledge_search", structured_output=True)
    def knowledge_search(
        access_handle: str,
        query: dict[str, Any],
    ) -> ToolResponse:
        return _invoke(lambda: app.knowledge_search(access_handle, query))

    @server.tool(name="materials_list_sources", structured_output=True)
    def materials_list_sources(access_handle: str) -> ToolResponse:
        return _invoke(lambda: app.materials_sources(access_handle, "materials_list_sources"))

    @server.tool(name="materials_search", structured_output=True)
    async def materials_search(
        access_handle: str,
        query: str,
        limit: int = 6,
        source_names: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> ToolResponse:
        return await _invoke_async(
            lambda: app.materials_search(
                access_handle,
                "materials_search",
                query,
                limit=limit,
                source_names=source_names,
                filters=filters,
            )
        )

    @server.tool(name="materials_acquire", structured_output=True)
    async def materials_acquire(
        access_handle: str,
        candidate_ref: str,
    ) -> ToolResponse:
        return await _invoke_async(
            lambda: app.materials_acquire(
                access_handle,
                "materials_acquire",
                candidate_ref,
            )
        )

    @server.tool(name="images_list_sources", structured_output=True)
    def images_list_sources(access_handle: str) -> ToolResponse:
        return _invoke(lambda: app.images_sources(access_handle))

    @server.tool(name="images_search", structured_output=True)
    def images_search(
        access_handle: str,
        query: str,
        limit: int = 6,
    ) -> ToolResponse:
        return _invoke(lambda: app.images_search(access_handle, query, limit=limit))

    @server.tool(name="images_acquire", structured_output=True)
    def images_acquire(
        access_handle: str,
        candidate_ref: str,
    ) -> ToolResponse:
        return _invoke(lambda: app.images_acquire(access_handle, candidate_ref))

    @server.tool(name="media_preprocess", structured_output=True)
    def media_preprocess(
        access_handle: str,
        request: dict[str, Any],
    ) -> ToolResponse:
        return _invoke(lambda: app.media_preprocess(access_handle, request))

    @server.tool(name="bgm_list_sources", structured_output=True)
    def bgm_list_sources(access_handle: str) -> ToolResponse:
        return _invoke(lambda: app.bgm_sources(access_handle))

    @server.tool(name="bgm_search", structured_output=True)
    def bgm_search(
        access_handle: str,
        query: str,
        limit: int = 6,
    ) -> ToolResponse:
        return _invoke(lambda: app.bgm_search(access_handle, query, limit=limit))

    @server.tool(name="bgm_acquire", structured_output=True)
    def bgm_acquire(
        access_handle: str,
        candidate_ref: str,
        clip_start_seconds: float = 0.0,
        clip_duration_seconds: float | None = None,
    ) -> ToolResponse:
        return _invoke(
            lambda: app.bgm_acquire(
                access_handle,
                candidate_ref,
                clip_start_seconds=clip_start_seconds,
                clip_duration_seconds=clip_duration_seconds,
            )
        )

    @server.tool(name="bgm_analyze", structured_output=True)
    def bgm_analyze(access_handle: str, source_path: str) -> ToolResponse:
        return _invoke(lambda: app.bgm_analyze(access_handle, source_path))

    @server.tool(name="editor_preflight_spec", structured_output=True)
    def editor_preflight_spec(access_handle: str) -> ToolResponse:
        return _invoke(lambda: app.editor_preflight(access_handle))

    @server.tool(name="editor_create_project", structured_output=True)
    def editor_create_project(
        access_handle: str,
        name: str,
        canvas: dict[str, Any] | None = None,
    ) -> ToolResponse:
        return _invoke(lambda: app.editor_create_project(access_handle, name, canvas))

    @server.tool(name="editor_import_asset", structured_output=True)
    def editor_import_asset(
        access_handle: str,
        project_id: str,
        expected_revision: int,
        source_path: str,
    ) -> ToolResponse:
        return _invoke(
            lambda: app.editor_import_asset(
                access_handle,
                project_id,
                expected_revision,
                source_path,
            )
        )

    @server.tool(name="editor_apply_commands", structured_output=True)
    def editor_apply_commands(
        access_handle: str,
        project_id: str,
        batch: dict[str, Any],
    ) -> ToolResponse:
        return _invoke(lambda: app.editor_apply_commands(access_handle, project_id, batch))

    @server.tool(name="editor_compile_spec", structured_output=True)
    def editor_compile_spec(access_handle: str) -> ToolResponse:
        return _invoke(lambda: app.editor_compile(access_handle))

    @server.tool(name="editor_validate_execution_project", structured_output=True)
    def editor_validate_execution_project(
        access_handle: str,
        project_id: str,
    ) -> ToolResponse:
        return _invoke(
            lambda: app.editor_validate_execution_project(
                access_handle,
                project_id,
            )
        )

    @server.tool(name="editor_submit_render", structured_output=True)
    def editor_submit_render(access_handle: str, project_id: str) -> ToolResponse:
        return _invoke(lambda: app.editor_submit_render(access_handle, project_id))

    @server.tool(name="editor_get_render", structured_output=True)
    def editor_get_render(access_handle: str, render_job_id: str) -> ToolResponse:
        return _invoke(lambda: app.editor_get_render(access_handle, render_job_id))

    @server.tool(name="editor_inspect_render", structured_output=True)
    def editor_inspect_render(
        access_handle: str,
        render_job_id: str,
    ) -> ToolResponse:
        return _invoke(
            lambda: app.editor_inspect_render(
                access_handle,
                render_job_id,
            )
        )


def _fixed_resource_reader(
    app: PluginApplication,
    uri: str,
) -> Callable[[], str]:
    def read() -> str:
        return str(app.context_read(uri)["content"])

    read.__name__ = "read_" + uri.rsplit("/", maxsplit=1)[-1].replace("-", "_")
    return read


def _invoke(function: Callable[[], _T]) -> ToolResponse:
    try:
        return {"ok": True, "data": _jsonable(function())}
    except Exception as error:
        return {"ok": False, "error": _error_payload(error)}


async def _invoke_async(function: Callable[[], Awaitable[_T]]) -> ToolResponse:
    try:
        return {"ok": True, "data": _jsonable(await function())}
    except Exception as error:
        return {"ok": False, "error": _error_payload(error)}


def _error_payload(error: Exception) -> dict[str, Any]:
    if isinstance(error, ValidationError):
        error = validation_error(error)
    if isinstance(error, PluginError):
        return error.as_dict()
    code = getattr(error, "code", None)
    if isinstance(code, str):
        message = getattr(error, "message", None)
        details = getattr(error, "details", None)
        return {
            "code": code,
            "message": message if isinstance(message, str) else str(error),
            "details": _jsonable(details) if isinstance(details, dict) else {},
        }
    return {
        "code": "internal_error",
        "message": "MCP 工具执行出现未分类异常",
        "details": {},
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _jsonable(cast(Callable[[], Any], to_dict)())
    return value


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
