from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .errors import VideoEditorError
from .models import EditorProject

_PROJECT_ID = re.compile(r"^project_[0-9a-f]{16}$")
_STALE_LOCK_SECONDS = 60


class ProjectStorage:
    """以单工程单 JSON 文件保存声明式剪辑状态。"""

    def __init__(self, root: Path | str = Path("output/editor/projects")) -> None:
        self.root = Path(root)

    def create(self, project: EditorProject) -> None:
        self._validate_project_id(project.id)
        project_dir = self.root / project.id
        try:
            project_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise VideoEditorError("project_exists", "工程 ID 已存在") from error
        except OSError as error:
            raise VideoEditorError("output_unavailable", f"工程目录写入失败：{error}") from error

        try:
            _write_json_atomic(
                project_dir / "project.json", project.model_dump(mode="json")
            )
        except Exception:
            try:
                project_dir.rmdir()
            except OSError:
                pass
            raise

    def list(self) -> list[EditorProject]:
        if not self.root.exists():
            return []
        try:
            paths = sorted(self.root.glob("project_*/project.json"))
        except OSError as error:
            raise VideoEditorError("output_unavailable", f"工程目录读取失败：{error}") from error
        return [self._read(path) for path in paths]

    def get(self, project_id: str) -> EditorProject:
        self._validate_project_id(project_id)
        path = self.root / project_id / "project.json"
        if not path.is_file():
            raise VideoEditorError("project_not_found", "工程不存在")
        return self._read(path)

    def save(self, project: EditorProject, *, expected_revision: int) -> None:
        self._validate_project_id(project.id)
        project_dir = self.root / project.id
        path = project_dir / "project.json"
        if not path.is_file():
            raise VideoEditorError("project_not_found", "工程不存在")

        with self._write_lock(project_dir):
            current = self._read(path)
            if current.revision != expected_revision:
                raise VideoEditorError(
                    "revision_conflict",
                    "工程已被其他写入者更新",
                    details={
                        "expected_revision": expected_revision,
                        "actual_revision": current.revision,
                    },
                )
            if project.revision != expected_revision + 1:
                raise VideoEditorError(
                    "invalid_revision_transition", "工程版本必须恰好递增 1"
                )
            _write_json_atomic(path, project.model_dump(mode="json"))

    def _read(self, path: Path) -> EditorProject:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return EditorProject.model_validate(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
            raise VideoEditorError("project_corrupt", "工程文件结构无效") from error

    @contextmanager
    def _write_lock(self, project_dir: Path) -> Iterator[None]:
        lock_path = project_dir / ".project.lock"
        descriptor = self._acquire_lock(lock_path)

        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as lock_file:
                lock_file.write(str(os.getpid()))
                lock_file.flush()
                os.fsync(lock_file.fileno())
            yield
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _acquire_lock(lock_path: Path) -> int:
        for attempt in range(2):
            try:
                return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError as error:
                if attempt == 0 and ProjectStorage._lock_is_stale(lock_path):
                    try:
                        lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as unlink_error:
                        raise VideoEditorError(
                            "project_busy", "工程正在被其他写入者更新"
                        ) from unlink_error
                    continue
                raise VideoEditorError(
                    "project_busy", "工程正在被其他写入者更新"
                ) from error
            except OSError as error:
                raise VideoEditorError(
                    "output_unavailable", f"工程锁创建失败：{error}"
                ) from error
        raise VideoEditorError("project_busy", "工程正在被其他写入者更新")

    @staticmethod
    def _lock_is_stale(lock_path: Path) -> bool:
        try:
            return time.time() - lock_path.stat().st_mtime >= _STALE_LOCK_SECONDS
        except OSError:
            return False

    @staticmethod
    def _validate_project_id(project_id: str) -> None:
        if not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None:
            raise VideoEditorError("invalid_project_id", "工程 ID 格式无效")


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    except OSError as error:
        raise VideoEditorError("output_unavailable", f"工程文件写入失败：{error}") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
