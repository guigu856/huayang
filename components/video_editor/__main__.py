"""本地视频剪辑器的 Agent 友好命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Never

from .api import create_app
from .errors import VideoEditorError
from .jobs import PersistentRenderQueue, RenderJob
from .media import MAX_MEDIA_BYTES, import_media, resolve_media_path
from .models import EditorProject
from .render import FFmpegRenderer
from .service import VideoEditorService


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        _write_error("invalid_arguments", message)
        raise SystemExit(2)


class _CliError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description="本地视频剪辑器")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ.get("VIDEO_EDITOR_ROOT", "output/editor/projects")),
        help="工程数据目录，默认 output/editor/projects",
    )
    commands = parser.add_subparsers(dest="operation", required=True)

    serve = commands.add_parser("serve", help="启动网页剪辑器")
    serve.add_argument("--host", default="127.0.0.1", help="监听地址")
    serve.add_argument("--port", type=int, default=8765, help="监听端口")

    project = commands.add_parser("project", help="管理剪辑工程")
    project_commands = project.add_subparsers(dest="project_operation", required=True)

    create = project_commands.add_parser("create", help="创建工程")
    create.add_argument("--name", required=True, help="工程名称")
    create.add_argument("--canvas-json", help="画布 JSON 对象")

    project_commands.add_parser("list", help="列出工程")
    show = project_commands.add_parser("show", help="读取工程")
    show.add_argument("project_id", help="工程 ID")

    command = commands.add_parser("command", help="执行声明式剪辑命令")
    command_actions = command.add_subparsers(dest="command_operation", required=True)
    apply = command_actions.add_parser("apply", help="原子执行命令批次")
    apply.add_argument("project_id", help="工程 ID")
    batch_source = apply.add_mutually_exclusive_group()
    batch_source.add_argument("--file", type=Path, help="UTF-8 JSON 批次文件")
    batch_source.add_argument("--json", dest="json_text", help="JSON 批次字符串")

    asset = commands.add_parser("asset", help="管理工程素材")
    asset_actions = asset.add_subparsers(dest="asset_operation", required=True)
    import_asset = asset_actions.add_parser("import", help="复制并登记本地媒体")
    import_asset.add_argument("project_id", help="工程 ID")
    import_asset.add_argument("source", type=Path, help="本地媒体路径")
    import_asset.add_argument(
        "--expected-revision",
        required=True,
        type=int,
        help="预期工程 revision",
    )

    render = commands.add_parser("render", help="提交渲染任务并等待完成")
    render.add_argument("project_id", help="工程 ID")
    render.add_argument(
        "--expected-revision",
        required=True,
        type=int,
        help="预期工程 revision",
    )
    render.add_argument("--output", type=Path, help="MP4 输出路径")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        if args.operation == "serve":
            return _serve(args)
        service = VideoEditorService(args.root)
        result = _execute(service, args)
    except VideoEditorError as error:
        _write_error(error.code, error.message, error.details)
        return 1
    except _CliError as error:
        _write_error(error.code, error.message)
        return 1

    print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    print(
        f"网页剪辑器：http://{args.host}:{args.port}；数据目录：{args.root}",
        file=sys.stderr,
    )
    uvicorn.run(create_app(args.root), host=args.host, port=args.port)
    return 0


def _execute(service: VideoEditorService, args: argparse.Namespace) -> Any:
    if args.operation == "project" and args.project_operation == "create":
        canvas = _parse_json(args.canvas_json, source="--canvas-json") if args.canvas_json else None
        project = service.create(args.name, canvas)
        print(f"已创建工程 {project.id}", file=sys.stderr)
        return project.model_dump(mode="json")
    if args.operation == "project" and args.project_operation == "list":
        projects = service.list()
        print(f"共 {len(projects)} 个工程", file=sys.stderr)
        return [project.model_dump(mode="json") for project in projects]
    if args.operation == "project" and args.project_operation == "show":
        project = service.get(args.project_id)
        print(f"已读取工程 {project.id}", file=sys.stderr)
        return project.model_dump(mode="json")
    if args.operation == "command" and args.command_operation == "apply":
        batch = _read_batch(args)
        project = service.apply(args.project_id, batch)
        print(
            f"已更新工程 {project.id} 至 revision {project.revision}",
            file=sys.stderr,
        )
        return project.model_dump(mode="json")
    if args.operation == "asset" and args.asset_operation == "import":
        return _import_asset(service, args)
    if args.operation == "render":
        return _render_project(service, args)
    raise _CliError("invalid_arguments", "命令结构无效")


def _import_asset(service: VideoEditorService, args: argparse.Namespace) -> Any:
    project = service.get(args.project_id)
    _assert_revision(project, args.expected_revision)
    project_dir = Path(args.root) / project.id
    print(f"正在导入素材：{args.source}", file=sys.stderr)
    try:
        with args.source.open("rb") as source:
            asset_input = import_media(
                project_dir,
                args.source.name,
                source,
                max_bytes=MAX_MEDIA_BYTES,
            )
    except OSError as error:
        raise _CliError("input_unavailable", f"素材文件读取失败：{error}") from error

    try:
        updated = service.apply(
            project.id,
            {
                "expected_revision": args.expected_revision,
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
    print(f"已导入素材 {asset.id}", file=sys.stderr)
    return {
        "project": updated.model_dump(mode="json"),
        "asset": asset.model_dump(mode="json"),
    }


def _render_project(service: VideoEditorService, args: argparse.Namespace) -> Any:
    project = service.get(args.project_id)
    _assert_revision(project, args.expected_revision)
    root = Path(args.root)
    queue = PersistentRenderQueue(root.parent / "render_jobs", FFmpegRenderer())
    queue.start()
    try:
        job = queue.submit(
            project,
            project_dir=root / project.id,
            output_path=args.output,
        )
        print(f"渲染任务 {job.id} 已排队", file=sys.stderr)
        last_progress: tuple[str, float, str] | None = None
        while True:
            try:
                completed = queue.wait(job.id, timeout=0.5)
                break
            except TimeoutError:
                current = queue.get(job.id)
                progress = (current.status, current.progress, current.message)
                if progress != last_progress:
                    print(
                        f"{current.message} {current.progress:.0%}",
                        file=sys.stderr,
                    )
                    last_progress = progress
    finally:
        queue.stop()

    if completed.status != "succeeded":
        _raise_render_error(completed)
    print(f"渲染完成：{completed.output_path}", file=sys.stderr)
    return completed.model_dump(mode="json")


def _raise_render_error(job: RenderJob) -> Never:
    if job.error is None:
        raise VideoEditorError("render_cancelled", job.message)
    code = job.error.get("code")
    message = job.error.get("message")
    details = job.error.get("details")
    raise VideoEditorError(
        code if isinstance(code, str) else "render_failed",
        message if isinstance(message, str) else "渲染失败",
        details=details if isinstance(details, dict) else None,
    )


def _read_batch(args: argparse.Namespace) -> Mapping[str, Any]:
    if args.file is not None:
        try:
            text = args.file.read_text(encoding="utf-8")
        except OSError as error:
            raise _CliError("input_unavailable", f"命令文件读取失败：{error}") from error
        source = str(args.file)
    elif args.json_text is not None:
        text = args.json_text
        source = "--json"
    else:
        text = sys.stdin.read()
        source = "stdin"
    payload = _parse_json(text, source=source)
    if not isinstance(payload, dict):
        raise _CliError("invalid_json", "命令批次必须是 JSON 对象")
    return payload


def _parse_json(text: str, *, source: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise _CliError(
            "invalid_json",
            f"{source} 不是有效 JSON：第 {error.lineno} 行第 {error.colno} 列",
        ) from error


def _assert_revision(project: EditorProject, expected_revision: int) -> None:
    if expected_revision < 0:
        raise _CliError("invalid_arguments", "expected revision 必须大于等于 0")
    if project.revision != expected_revision:
        raise VideoEditorError(
            "revision_conflict",
            "工程已被其他写入者更新",
            details={
                "expected_revision": expected_revision,
                "actual_revision": project.revision,
            },
        )


def _write_error(
    code: str, message: str, details: dict[str, Any] | None = None
) -> None:
    print(
        json.dumps(
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                    "details": details or {},
                },
            },
            ensure_ascii=False,
        )
    )


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="strict")


if __name__ == "__main__":
    raise SystemExit(main())
