from __future__ import annotations

import json
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from video_create_plugin.context import ContextCatalog
from video_create_plugin.errors import PluginError

from .models import (
    OutputScope,
    ResourceCreateRequest,
    ResourceKind,
    ResourceUpdateRequest,
)
from .service import AdminService

LOGGER = logging.getLogger(__name__)


def create_app(
    *,
    resource_root: Path | str | None = None,
    output_root: Path | str | None = None,
) -> FastAPI:
    resolved_resource_root = ContextCatalog(resource_root).root
    resolved_output_root = (
        Path(output_root).expanduser().resolve()
        if output_root is not None
        else _default_output_root(resolved_resource_root)
    )
    service = AdminService(resolved_resource_root, resolved_output_root)
    web_root = Path(__file__).with_name("web")
    app = FastAPI(title="Huayang 后台管理", version="1.0.0")
    app.state.admin_service = service

    @app.exception_handler(PluginError)
    async def handle_plugin_error(_request: Request, error: PluginError) -> JSONResponse:
        return JSONResponse(
            status_code=_status_for_error(error.code),
            content={"ok": False, "error": error.as_dict()},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "ok": False,
                "error": {
                    "code": "invalid_request",
                    "message": "请求结构校验失败",
                    "details": {
                        "errors": [
                            {
                                "location": [str(part) for part in item["loc"]],
                                "message": item["msg"],
                                "type": item["type"],
                            }
                            for item in error.errors()
                        ]
                    },
                },
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        _request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={
                "ok": False,
                "error": {
                    "code": "http_not_found" if error.status_code == 404 else "http_error",
                    "message": "资源不存在" if error.status_code == 404 else "HTTP 请求失败",
                    "details": {"status_code": error.status_code},
                },
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, error: Exception) -> JSONResponse:
        LOGGER.exception("Huayang 后台请求执行失败", exc_info=error)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": "服务内部错误",
                    "details": {},
                },
            },
        )

    @app.get("/api/v1/health")
    def health() -> dict[str, Any]:
        return _success({"status": "ok", "plugin": "huayang"})

    @app.get("/api/v1/overview")
    def overview() -> dict[str, Any]:
        return _success(service.overview().model_dump(mode="json"))

    @app.get("/api/v1/resources")
    def list_resources(kind: ResourceKind | None = None) -> dict[str, Any]:
        return _success([item.model_dump(mode="json") for item in service.list_resources(kind)])

    @app.get("/api/v1/resources/{kind}/{resource_id}")
    def get_resource(kind: ResourceKind, resource_id: str) -> dict[str, Any]:
        return _success(service.get_resource(kind, resource_id).model_dump(mode="json"))

    @app.post(
        "/api/v1/resources/{kind}",
        status_code=status.HTTP_201_CREATED,
    )
    def create_resource(
        kind: ResourceKind,
        request: ResourceCreateRequest,
    ) -> dict[str, Any]:
        return _success(service.create_resource(kind, request).model_dump(mode="json"))

    @app.put("/api/v1/resources/{kind}/{resource_id}")
    def update_resource(
        kind: ResourceKind,
        resource_id: str,
        request: ResourceUpdateRequest,
    ) -> dict[str, Any]:
        return _success(service.update_resource(kind, resource_id, request).model_dump(mode="json"))

    @app.delete(
        "/api/v1/resources/{kind}/{resource_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_resource(
        kind: ResourceKind,
        resource_id: str,
        expected_sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    ) -> Response:
        service.delete_resource(kind, resource_id, expected_sha256)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/v1/workflow/tasks")
    def list_tasks(
        source_id: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0, le=100_000),
    ) -> dict[str, Any]:
        return _success(service.list_tasks(source_id=source_id, limit=limit, offset=offset))

    @app.get("/api/v1/workflow/sources")
    def workflow_sources() -> dict[str, Any]:
        return _success(service.workflow_source_ids())

    @app.get("/api/v1/workflow/artifacts")
    def list_artifacts(
        source_id: str | None = None,
        task_id: str | None = None,
        limit: int = Query(default=500, ge=1, le=1000),
        offset: int = Query(default=0, ge=0, le=100_000),
    ) -> dict[str, Any]:
        return _success(
            service.list_artifacts(
                source_id=source_id,
                task_id=task_id,
                limit=limit,
                offset=offset,
            )
        )

    @app.get("/api/v1/workflow/{source_id}/artifacts/{artifact_id}/content")
    def artifact_content(source_id: str, artifact_id: str) -> Response:
        artifact, content = service.read_artifact(source_id, artifact_id)
        media_type = _artifact_media_type(artifact.artifact_type, content)
        return Response(
            content=content,
            media_type=media_type,
            headers={"ETag": f'"{artifact.content_sha256}"'},
        )

    @app.get("/api/v1/outputs")
    def list_outputs(
        scope: OutputScope = "all",
        limit: int = Query(default=300, ge=1, le=1000),
        offset: int = Query(default=0, ge=0, le=100_000),
    ) -> dict[str, Any]:
        return _success(
            [
                item.model_dump(mode="json")
                for item in service.list_outputs(scope=scope, limit=limit, offset=offset)
            ]
        )

    @app.get("/api/v1/outputs/{output_id}/content")
    def output_content(output_id: str) -> FileResponse:
        path = service.resolve_output(output_id)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() in {".html", ".txt"}:
            media_type = "text/plain"
        elif path.suffix.lower() == ".md":
            media_type = "text/markdown"
        return FileResponse(
            path,
            media_type=media_type,
            filename=None,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    app.mount("/static", StaticFiles(directory=web_root), name="huayang-static")

    @app.get("/", include_in_schema=False)
    def admin_shell() -> FileResponse:
        return FileResponse(web_root / "index.html", media_type="text/html")

    return app


def _default_output_root(resource_root: Path) -> Path:
    configured = os.environ.get("HUAYANG_OUTPUT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if (resource_root / "rules" / "main-agent.md").is_file() and (
        resource_root / "pyproject.toml"
    ).is_file():
        return resource_root / "output" / "plugin"
    return Path.home() / ".huayang" / "output"


def _success(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _status_for_error(code: str) -> int:
    if code.endswith("_not_found") or code in {
        "artifact_not_found",
        "context_resource_not_found",
        "output_not_found",
    }:
        return status.HTTP_404_NOT_FOUND
    if code in {
        "resource_conflict",
        "resource_revision_conflict",
        "resource_protected",
    }:
        return status.HTTP_409_CONFLICT
    if code in {"resource_write_failed", "workflow_store_unavailable"}:
        return status.HTTP_500_INTERNAL_SERVER_ERROR
    return status.HTTP_400_BAD_REQUEST


def _artifact_media_type(artifact_type: str, content: bytes) -> str:
    if artifact_type.endswith("manifest") or artifact_type in {
        "creative_direction",
        "preparation_package",
        "editing_specification",
        "knowledge_publication",
    }:
        try:
            json.loads(content)
        except (UnicodeError, json.JSONDecodeError):
            pass
        else:
            return "application/json"
    try:
        content.decode("utf-8")
    except UnicodeError:
        return "application/octet-stream"
    return "text/plain"
