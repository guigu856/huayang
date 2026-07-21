"""本地视频剪辑器的 HTTP API 与静态界面入口。"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Generic, Literal, TypeVar

from fastapi import FastAPI, File, Form, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .commands import CommandBatch
from .errors import VideoEditorError
from .jobs import PersistentRenderQueue, RenderJob
from .media import MAX_MEDIA_BYTES, import_media, probe_media, resolve_media_path
from .models import Asset, Canvas, EditorProject
from .render import FFmpegRenderer
from .service import VideoEditorService

LOGGER = logging.getLogger(__name__)
DataT = TypeVar("DataT")


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SuccessResponse(_ApiModel, Generic[DataT]):
    ok: Literal[True] = True
    data: DataT


class HealthData(_ApiModel):
    status: Literal["ok"] = "ok"


class AssetImportData(_ApiModel):
    project: EditorProject
    asset: Asset


class ProjectCreateRequest(_ApiModel):
    name: str
    canvas: Canvas | None = None


class RenderCreateRequest(_ApiModel):
    expected_revision: int = Field(ge=0)


def create_app(
    root: Path | str = Path("output/editor/projects"),
    *,
    render_queue: PersistentRenderQueue | None = None,
) -> FastAPI:
    """创建使用指定数据根目录的本地剪辑器应用。"""
    data_root = Path(root)
    service = VideoEditorService(data_root)
    queue = render_queue or PersistentRenderQueue(
        data_root.parent / "render_jobs", FFmpegRenderer()
    )
    web_root = Path(__file__).with_name("web")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        queue.start()
        try:
            yield
        finally:
            queue.stop()

    app = FastAPI(title="本地视频剪辑器", version="1.0.0", lifespan=lifespan)
    app.state.editor_root = data_root
    app.state.editor_service = service
    app.state.render_queue = queue

    @app.exception_handler(VideoEditorError)
    async def handle_editor_error(
        _request: Request, error: VideoEditorError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=_status_for_editor_error(error.code),
            content=_error_payload(error.code, error.message, error.details),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        details = {
            "errors": [
                {
                    "location": [str(part) for part in item["loc"]],
                    "message": item["msg"],
                    "type": item["type"],
                }
                for item in error.errors()
            ]
        }
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_error_payload("invalid_request", "请求参数无效", details),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        _request: Request, error: StarletteHTTPException
    ) -> JSONResponse:
        message = "资源不存在" if error.status_code == 404 else "HTTP 请求失败"
        return JSONResponse(
            status_code=error.status_code,
            content=_error_payload(
                "http_not_found" if error.status_code == 404 else "http_error",
                message,
                {"status_code": error.status_code},
            ),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, error: Exception) -> JSONResponse:
        LOGGER.exception("视频剪辑器请求执行失败", exc_info=error)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_payload("internal_error", "服务内部错误", {}),
        )

    @app.get("/api/v1/health", response_model=SuccessResponse[HealthData])
    def health() -> dict[str, Any]:
        return _success({"status": "ok"})

    @app.get(
        "/api/v1/projects", response_model=SuccessResponse[list[EditorProject]]
    )
    def list_projects() -> dict[str, Any]:
        return _success([_dump(project) for project in service.list()])

    @app.post(
        "/api/v1/projects",
        status_code=status.HTTP_201_CREATED,
        response_model=SuccessResponse[EditorProject],
    )
    def create_project(request: ProjectCreateRequest) -> dict[str, Any]:
        return _success(_dump(service.create(request.name, request.canvas)))

    @app.get(
        "/api/v1/projects/{project_id}",
        response_model=SuccessResponse[EditorProject],
    )
    def get_project(project_id: str) -> dict[str, Any]:
        return _success(_dump(service.get(project_id)))

    @app.post(
        "/api/v1/projects/{project_id}/commands",
        response_model=SuccessResponse[EditorProject],
    )
    def apply_commands(project_id: str, batch: CommandBatch) -> dict[str, Any]:
        return _success(_dump(service.apply(project_id, batch)))

    @app.post(
        "/api/v1/projects/{project_id}/assets",
        status_code=status.HTTP_201_CREATED,
        response_model=SuccessResponse[AssetImportData],
    )
    def upload_asset(
        project_id: str,
        expected_revision: Annotated[int, Form(ge=0)],
        file: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        project = service.get(project_id)
        _assert_revision(project, expected_revision)
        project_dir = data_root / project_id
        asset_input = import_media(
            project_dir,
            file.filename or "素材",
            file.file,
            max_bytes=MAX_MEDIA_BYTES,
            probe=probe_media,
        )
        try:
            updated = service.apply(
                project_id,
                {
                    "expected_revision": expected_revision,
                    "commands": [
                        {
                            "type": "asset.add",
                            "asset": asset_input.model_dump(mode="python"),
                        }
                    ],
                },
            )
        except Exception:
            try:
                resolve_media_path(project_dir, asset_input.path).unlink(missing_ok=True)
            except VideoEditorError:
                pass
            raise

        asset = updated.assets[-1]
        return _success({"project": _dump(updated), "asset": _dump(asset)})

    @app.get("/api/v1/projects/{project_id}/assets/{asset_id}/content")
    def asset_content(project_id: str, asset_id: str) -> FileResponse:
        project = service.get(project_id)
        asset = next((item for item in project.assets if item.id == asset_id), None)
        if asset is None:
            raise VideoEditorError("asset_not_found", "素材不存在")
        path = resolve_media_path(data_root / project_id, asset.path)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type)

    @app.post(
        "/api/v1/projects/{project_id}/renders",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=SuccessResponse[RenderJob],
    )
    def create_render(
        project_id: str, request: RenderCreateRequest
    ) -> dict[str, Any]:
        project = service.get(project_id)
        _assert_revision(project, request.expected_revision)
        job = queue.submit(project, project_dir=data_root / project.id)
        return _success(_dump(job))

    @app.get(
        "/api/v1/render-jobs/{job_id}", response_model=SuccessResponse[RenderJob]
    )
    def get_render(job_id: str) -> dict[str, Any]:
        return _success(_dump(queue.get(job_id)))

    @app.delete(
        "/api/v1/render-jobs/{job_id}", response_model=SuccessResponse[RenderJob]
    )
    def cancel_render(job_id: str) -> dict[str, Any]:
        return _success(_dump(queue.cancel(job_id)))

    @app.get("/api/v1/render-jobs/{job_id}/output")
    def render_output(job_id: str) -> FileResponse:
        job = queue.get(job_id)
        if job.status != "succeeded":
            raise VideoEditorError(
                "render_output_not_ready",
                "渲染结果尚未就绪",
                details={"status": job.status},
            )
        path = Path(job.output_path)
        if not path.is_file():
            raise VideoEditorError("render_output_not_found", "渲染结果不存在")
        return FileResponse(path, media_type="video/mp4")

    app.mount("/static", StaticFiles(directory=web_root), name="editor-static")

    @app.get("/", include_in_schema=False)
    def editor_shell() -> FileResponse:
        return FileResponse(web_root / "index.html", media_type="text/html")

    return app


def _success(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _error_payload(
    code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": code, "message": message, "details": details or {}},
    }


def _status_for_editor_error(code: str) -> int:
    if code in {
        "project_not_found",
        "asset_not_found",
        "asset_content_not_found",
        "media_not_found",
        "track_not_found",
        "clip_not_found",
        "render_job_not_found",
        "render_output_not_found",
    }:
        return status.HTTP_404_NOT_FOUND
    if code in {"revision_conflict", "project_busy", "asset_in_use"}:
        return status.HTTP_409_CONFLICT
    if code == "render_output_not_ready":
        return status.HTTP_409_CONFLICT
    if code == "media_too_large":
        return status.HTTP_413_CONTENT_TOO_LARGE
    if code in {"invalid_asset_type", "unsupported_media"}:
        return status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    if code in {
        "invalid_input",
        "invalid_project_id",
        "split_point_invalid",
        "track_domain_mismatch",
        "track_index_invalid",
        "source_range_invalid",
        "invalid_filename",
        "invalid_media_stream",
        "invalid_media_metadata",
        "invalid_asset_path",
        "media_probe_failed",
        "media_probe_timeout",
        "invalid_render_job_id",
    }:
        return status.HTTP_422_UNPROCESSABLE_CONTENT
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def _dump(model: Any) -> Any:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model


def _assert_revision(project: EditorProject, expected_revision: int) -> None:
    if project.revision != expected_revision:
        raise VideoEditorError(
            "revision_conflict",
            "工程已被其他写入者更新",
            details={
                "expected_revision": expected_revision,
                "actual_revision": project.revision,
            },
        )
