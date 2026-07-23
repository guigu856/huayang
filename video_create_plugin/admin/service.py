from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import yaml

from video_create_plugin.artifacts import ArtifactStore
from video_create_plugin.context import ContextCatalog, ContextResource
from video_create_plugin.errors import PluginError
from video_create_plugin.models import ArtifactEnvelope
from video_create_plugin.repository import WorkflowRepository

from .models import (
    OutputEntry,
    OutputScope,
    Overview,
    ResourceCreateRequest,
    ResourceDocument,
    ResourceKind,
    ResourceSummary,
    ResourceUpdateRequest,
)

_RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_OUTPUT_SUFFIXES = {
    ".json": "json",
    ".jsonl": "json",
    ".md": "text",
    ".txt": "text",
    ".html": "text",
    ".csv": "text",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".gif": "image",
    ".mp4": "video",
    ".webm": "video",
    ".mov": "video",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".pdf": "document",
    ".docx": "document",
}
_LEARNING_MARKERS = {
    "analysis",
    "knowledge",
    "reference_learning",
    "reference_publication",
    "reference_reports",
}
_CREATION_MARKERS = {"creation", "editor", "render-inspection", "renders"}
_IGNORED_OUTPUT_DIRECTORIES = {"__pycache__", "objects"}


class AdminService:
    """管理 Huayang 上下文资源以及只读浏览工作流与文件产物。"""

    def __init__(self, resource_root: Path | str, output_root: Path | str) -> None:
        self.resource_root = Path(resource_root).expanduser().resolve()
        self.output_root = Path(output_root).expanduser().resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.context = ContextCatalog(self.resource_root)
        self._resource_write_lock = threading.RLock()
        self._workflow_source_lock = threading.RLock()
        self.workflow_sources = {
            "primary": WorkflowSource(
                source_id="primary",
                repository=WorkflowRepository(self.output_root / "workflow.sqlite3"),
                artifacts=ArtifactStore(self.output_root / "objects"),
            )
        }
        self._refresh_workflow_sources()

    def overview(self) -> Overview:
        self._refresh_workflow_sources()
        resources = self.list_resources()
        return Overview(
            plugin_name="huayang",
            resource_root=str(self.resource_root),
            output_root=str(self.output_root),
            rule_count=sum(item.kind == "rule" for item in resources),
            skill_count=sum(item.kind == "skill" for item in resources),
            task_count=sum(
                source.repository.count_tasks() for source in self.workflow_sources.values()
            ),
            artifact_count=sum(
                source.repository.count_artifacts() for source in self.workflow_sources.values()
            ),
            creation_output_count=self.count_outputs(scope="creation"),
            learning_output_count=self.count_outputs(scope="learning"),
        )

    def list_resources(self, kind: ResourceKind | None = None) -> list[ResourceSummary]:
        with self._resource_write_lock:
            resources = [
                self._resource_summary(item)
                for item in self.context.catalog()
                if item.kind in {"rule", "skill"} and (kind is None or item.kind == kind)
            ]
            return sorted(resources, key=lambda item: (item.kind, not item.builtin, item.title))

    def get_resource(self, kind: ResourceKind, resource_id: str) -> ResourceDocument:
        with self._resource_write_lock:
            item = self._read_resource(kind, resource_id)
            return ResourceDocument(
                **self._resource_summary(item).model_dump(mode="python"),
                content=item.content,
            )

    def create_resource(
        self,
        kind: ResourceKind,
        request: ResourceCreateRequest,
    ) -> ResourceDocument:
        with self._resource_write_lock:
            self._validate_resource_id(request.resource_id)
            if any(
                item.kind == kind and item.resource_id == request.resource_id
                for item in self.context.catalog()
            ):
                raise PluginError("resource_conflict", "同名上下文资源已经存在")
            path = self._new_resource_path(kind, request.resource_id)
            self._validate_new_resource_path(path)
            content = request.content.strip()
            if not content:
                content = _resource_template(kind, request)
            self._validate_content(kind, request.resource_id, content)
            self._write_new(path, content.rstrip() + "\n")
            return self.get_resource(kind, request.resource_id)

    def update_resource(
        self,
        kind: ResourceKind,
        resource_id: str,
        request: ResourceUpdateRequest,
    ) -> ResourceDocument:
        with self._resource_write_lock:
            current = self.get_resource(kind, resource_id)
            if current.sha256 != request.expected_sha256:
                raise PluginError(
                    "resource_revision_conflict",
                    "资源内容已经发生变化，请刷新后再保存",
                )
            self._validate_content(kind, resource_id, request.content)
            path = self.context.resource_path(kind, resource_id)
            self._write_replace(path, request.content.rstrip() + "\n")
            return self.get_resource(kind, resource_id)

    def delete_resource(
        self,
        kind: ResourceKind,
        resource_id: str,
        expected_sha256: str,
    ) -> None:
        with self._resource_write_lock:
            current = self.get_resource(kind, resource_id)
            if current.builtin:
                raise PluginError("resource_protected", "内置阶段资源带有删除保护")
            if current.sha256 != expected_sha256:
                raise PluginError(
                    "resource_revision_conflict",
                    "资源内容已经发生变化，请刷新后再删除",
                )
            path = self.context.resource_path(kind, resource_id)
            try:
                if kind == "skill":
                    skill_root = path.parent.resolve()
                    skills_root = (self.resource_root / "skills").resolve()
                    if skill_root.parent != skills_root:
                        raise PluginError("resource_write_failed", "Skill 资源目录无效")
                    shutil.rmtree(skill_root)
                else:
                    path.unlink()
            except PluginError:
                raise
            except OSError as error:
                raise PluginError("resource_write_failed", "上下文资源删除失败") from error

    def list_tasks(
        self,
        *,
        source_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        self._refresh_workflow_sources()
        sources = self._selected_sources(source_id)
        fetch_limit = _validated_page(limit, offset)
        tasks = [
            {"source_id": source.source_id, **task.model_dump(mode="json")}
            for source in sources
            for task in source.repository.list_tasks(limit=fetch_limit)
        ]
        ordered = sorted(tasks, key=lambda item: str(item["updated_at"]), reverse=True)
        return ordered[offset : offset + limit]

    def list_artifacts(
        self,
        *,
        source_id: str | None = None,
        task_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        self._refresh_workflow_sources()
        sources = self._selected_sources(source_id)
        fetch_limit = _validated_page(limit, offset)
        artifacts = [
            {"source_id": source.source_id, **artifact.model_dump(mode="json")}
            for source in sources
            for artifact in source.repository.list_artifacts(
                task_id=task_id,
                limit=fetch_limit,
            )
        ]
        ordered = sorted(artifacts, key=lambda item: str(item["created_at"]), reverse=True)
        return ordered[offset : offset + limit]

    def workflow_source_ids(self) -> list[str]:
        self._refresh_workflow_sources()
        return sorted(self.workflow_sources)

    def read_artifact(
        self,
        source_id: str,
        artifact_id: str,
    ) -> tuple[ArtifactEnvelope, bytes]:
        self._refresh_workflow_sources()
        source = self._source(source_id)
        artifact = source.repository.get_artifact(artifact_id)
        return artifact, source.artifacts.read_bytes(artifact.content_uri)

    def list_outputs(
        self,
        *,
        scope: OutputScope,
        limit: int,
        offset: int = 0,
    ) -> list[OutputEntry]:
        _validated_page(limit, offset)
        entries: list[OutputEntry] = []
        for path in self._iter_output_files():
            relative = path.relative_to(self.output_root)
            if not _is_visible_output(relative):
                continue
            output_scope = _classify_scope(relative)
            if scope != "all" and output_scope != scope:
                continue
            stat = path.stat()
            output_id = _encode_path(relative)
            kind = cast(
                Literal["video", "audio", "image", "json", "text", "document"],
                _OUTPUT_SUFFIXES[path.suffix.lower()],
            )
            entries.append(
                OutputEntry(
                    output_id=output_id,
                    scope=output_scope,
                    kind=kind,
                    name=path.name,
                    relative_path=relative.as_posix(),
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                    previewable=kind in {"video", "audio", "image", "json", "text"},
                    content_url=f"/api/v1/outputs/{output_id}/content",
                )
            )
        entries.sort(key=lambda item: (item.modified_at, item.relative_path), reverse=True)
        return entries[offset : offset + limit]

    def count_outputs(self, *, scope: OutputScope) -> int:
        return sum(
            scope == "all" or _classify_scope(path.relative_to(self.output_root)) == scope
            for path in self._iter_output_files()
            if _is_visible_output(path.relative_to(self.output_root))
        )

    def _iter_output_files(self) -> list[Path]:
        files: list[Path] = []
        for current_root, directory_names, file_names in os.walk(self.output_root):
            directory_names[:] = [
                name
                for name in directory_names
                if name not in _IGNORED_OUTPUT_DIRECTORIES
                and not name.startswith(".")
                and not name.startswith("package-audit")
                and not name.endswith(".lancedb")
            ]
            root = Path(current_root)
            files.extend(
                root / name
                for name in file_names
                if not name.startswith(".") and Path(name).suffix.lower() in _OUTPUT_SUFFIXES
            )
        return files

    def resolve_output(self, output_id: str) -> Path:
        relative = _decode_path(output_id)
        path = (self.output_root / relative).resolve()
        if (
            not path.is_relative_to(self.output_root)
            or not path.is_file()
            or path.suffix.lower() not in _OUTPUT_SUFFIXES
            or not _is_visible_output(path.relative_to(self.output_root))
        ):
            raise PluginError("output_not_found", "产物不存在")
        return path

    def _read_resource(self, kind: ResourceKind, resource_id: str) -> ContextResource:
        self._validate_resource_id(resource_id)
        return self.context.read(
            f"huayang://{'rules' if kind == 'rule' else 'skills'}/{resource_id}"
        )

    def _selected_sources(self, source_id: str | None) -> list[WorkflowSource]:
        if source_id is not None:
            return [self._source(source_id)]
        return list(self.workflow_sources.values())

    def _source(self, source_id: str) -> WorkflowSource:
        source = self.workflow_sources.get(source_id)
        if source is None:
            raise PluginError("workflow_source_not_found", "工作流来源不存在")
        return source

    def _resource_summary(self, item: ContextResource) -> ResourceSummary:
        if item.kind not in {"rule", "skill"}:
            raise PluginError("resource_kind_invalid", "后台仅管理 rule 与 skill")
        path = self.context.resource_path(item.kind, item.resource_id)
        stat = path.stat()
        return ResourceSummary(
            resource_id=item.resource_id,
            kind=cast(ResourceKind, item.kind),
            uri=item.uri,
            title=item.title,
            description=item.description,
            relative_path=item.relative_path,
            builtin=item.builtin,
            sha256=hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
        )

    def _new_resource_path(self, kind: ResourceKind, resource_id: str) -> Path:
        if kind == "rule":
            return self.resource_root / "rules" / "custom" / f"{resource_id}.md"
        return self.resource_root / "skills" / resource_id / "SKILL.md"

    def _validate_new_resource_path(self, path: Path) -> None:
        parent = path.parent.resolve()
        if not parent.is_relative_to(self.resource_root):
            raise PluginError("resource_write_failed", "上下文资源路径无效")

    def _refresh_workflow_sources(self) -> None:
        reference_root = self.output_root / "validation" / "reference_publication"
        reference_database = reference_root / "workflow.sqlite3"
        if not reference_database.is_file():
            return
        with self._workflow_source_lock:
            if "reference-validation" not in self.workflow_sources:
                self.workflow_sources["reference-validation"] = WorkflowSource(
                    source_id="reference-validation",
                    repository=WorkflowRepository(reference_database),
                    artifacts=ArtifactStore(reference_root / "objects"),
                )

    @staticmethod
    def _validate_resource_id(resource_id: str) -> None:
        if not _RESOURCE_ID.fullmatch(resource_id):
            raise PluginError("invalid_request", "资源 ID 仅支持小写字母、数字和连字符")

    @staticmethod
    def _validate_content(kind: ResourceKind, resource_id: str, content: str) -> None:
        if not content.strip():
            raise PluginError("invalid_request", "资源内容为空")
        if kind == "skill":
            lines = content.lstrip().splitlines()
            closing_index = next(
                (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
                None,
            )
            metadata: object = None
            if lines and lines[0].strip() == "---" and closing_index is not None:
                try:
                    metadata = yaml.safe_load("\n".join(lines[1:closing_index]))
                except yaml.YAMLError:
                    metadata = None
            if not isinstance(metadata, dict):
                metadata = {}
            name = metadata.get("name")
            description = metadata.get("description")
            if name != resource_id or not isinstance(description, str) or not description.strip():
                raise PluginError(
                    "invalid_skill_document",
                    "Skill 需要包含匹配资源 ID 的 name 与 description 前置信息",
                )

    @staticmethod
    def _write_new(path: Path, content: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8", newline="\n") as output:
                output.write(content)
        except FileExistsError as error:
            raise PluginError("resource_conflict", "同名上下文资源已经存在") from error
        except OSError as error:
            raise PluginError("resource_write_failed", "上下文资源写入失败") from error

    @staticmethod
    def _write_replace(path: Path, content: str) -> None:
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except OSError as error:
            raise PluginError("resource_write_failed", "上下文资源写入失败") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _resource_template(kind: ResourceKind, request: ResourceCreateRequest) -> str:
    if kind == "skill":
        return (
            "---\n"
            f"name: {request.resource_id}\n"
            f"description: {request.description}\n"
            "---\n\n"
            f"# {request.title}\n\n"
            f"{request.description}\n"
        )
    return f"# {request.title}\n\n{request.description}\n"


@dataclass(frozen=True, slots=True)
class WorkflowSource:
    source_id: str
    repository: WorkflowRepository
    artifacts: ArtifactStore


def _classify_scope(relative: Path) -> Literal["creation", "learning", "system"]:
    parts = {part.lower() for part in relative.parts}
    if parts & _LEARNING_MARKERS:
        return "learning"
    if parts & _CREATION_MARKERS:
        return "creation"
    return "system"


def _is_visible_output(relative: Path) -> bool:
    return not any(
        part.startswith(".")
        or part == "objects"
        or part.startswith("package-audit")
        or part.endswith(".lancedb")
        for part in (item.lower() for item in relative.parts)
    )


def _encode_path(path: Path) -> str:
    return base64.urlsafe_b64encode(path.as_posix().encode("utf-8")).decode("ascii").rstrip("=")


def _decode_path(value: str) -> Path:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise PluginError("output_not_found", "产物标识无效")
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding).decode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise PluginError("output_not_found", "产物标识无效") from error
    relative = Path(decoded)
    if not decoded or relative.is_absolute() or ".." in relative.parts or "\\" in decoded:
        raise PluginError("output_not_found", "产物标识无效")
    return relative


def _validated_page(limit: int, offset: int) -> int:
    if limit < 1 or limit > 1000:
        raise PluginError("invalid_request", "分页数量必须在 1 到 1000 之间")
    if offset < 0 or offset > 100_000:
        raise PluginError("invalid_request", "分页偏移必须在 0 到 100000 之间")
    return limit + offset
