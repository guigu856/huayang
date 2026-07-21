from __future__ import annotations

import importlib
import json
import os
import queue
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import VideoEditorError
from .models import EditorProject

RenderStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
_TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}
_JOB_ID = re.compile(r"^render_[0-9a-f]{16}$")


class RenderJob(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(pattern=r"^render_[0-9a-f]{16}$")
    project_id: str
    revision: int = Field(ge=0)
    status: RenderStatus
    progress: float = Field(ge=0, le=1)
    message: str
    output_path: str
    error: dict[str, Any] | None = None


class ProjectRenderer(Protocol):
    def render(
        self,
        project: EditorProject,
        *,
        project_dir: Path,
        output_path: Path,
        cancel_event: threading.Event,
    ) -> Path: ...


class _WorkerFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        file: BinaryIO | None = None
        try:
            file = self.path.open("a+b")
            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b"\0")
                file.flush()
            file.seek(0)
        except OSError as error:
            if file is not None:
                file.close()
            raise VideoEditorError(
                "output_unavailable", f"渲染工作锁创建失败：{error}"
            ) from error

        assert file is not None
        try:
            _acquire_os_file_lock(file)
        except OSError as error:
            file.close()
            raise VideoEditorError(
                "render_worker_busy", "渲染工作线程已由其他进程占用"
            ) from error
        self._file = file

    def release(self) -> None:
        file = self._file
        self._file = None
        if file is not None:
            file.close()


class PersistentRenderQueue:
    """以磁盘任务记录和单个工作线程执行渲染。"""

    def __init__(self, root: Path | str, renderer: ProjectRenderer) -> None:
        self.root = Path(root)
        self.renderer = renderer
        self._pending: queue.Queue[str] = queue.Queue()
        self._pending_ids: set[str] = set()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._worker_lock = _WorkerFileLock(self.root / ".worker.lock")
        self._current_id: str | None = None
        self._current_cancel: threading.Event | None = None

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._ensure_root()
            self._worker_lock.acquire()
            try:
                self._stop_event.clear()
                queued_ids: list[str] = []
                paths = sorted(
                    self.root.glob("render_*/job.json"),
                    key=lambda path: (path.stat().st_mtime_ns, path.parent.name),
                )
                for path in paths:
                    job = self._read_job_path(path)
                    if job.status == "running":
                        job = job.model_copy(
                            update={
                                "status": "failed",
                                "message": "渲染中断",
                                "error": {
                                    "code": "render_interrupted",
                                    "message": "服务重启导致渲染中断",
                                },
                            }
                        )
                        self._write_job(job)
                    elif job.status == "queued":
                        if job.id not in self._pending_ids:
                            queued_ids.append(job.id)
                            self._pending_ids.add(job.id)
                self._worker = threading.Thread(
                    target=self._run,
                    name="video-editor-render-worker",
                    daemon=True,
                )
                self._worker.start()
                for job_id in queued_ids:
                    self._pending.put(job_id)
            except Exception:
                self._worker = None
                self._worker_lock.release()
                raise

    def stop(
        self,
        *,
        wait: bool = True,
        timeout: float | None = None,
    ) -> None:
        with self._lock:
            worker = self._worker
            if worker is None:
                return
            self._stop_event.set()
            if self._current_cancel is not None:
                self._current_cancel.set()
        if wait:
            worker.join(timeout=timeout)
        with self._lock:
            if not worker.is_alive():
                self._worker = None

    def submit(
        self,
        project: EditorProject,
        *,
        project_dir: Path | str,
        output_path: Path | str | None = None,
    ) -> RenderJob:
        with self._lock:
            self._ensure_root()
            job_id = self._new_id()
            resolved_project_dir = Path(project_dir).resolve()
            resolved_output = (
                Path(output_path).resolve()
                if output_path is not None
                else resolved_project_dir / "renders" / f"{job_id}.mp4"
            )
            job_dir = self.root / job_id
            try:
                job_dir.mkdir(parents=False, exist_ok=False)
            except OSError as error:
                raise VideoEditorError(
                    "output_unavailable", f"渲染任务目录创建失败：{error}"
                ) from error
            job = RenderJob(
                id=job_id,
                project_id=project.id,
                revision=project.revision,
                status="queued",
                progress=0,
                message="等待渲染",
                output_path=str(resolved_output),
            )
            try:
                _write_json_atomic(
                    job_dir / "project.json", project.model_dump(mode="json")
                )
                _write_json_atomic(
                    job_dir / "request.json",
                    {
                        "project_dir": str(resolved_project_dir),
                        "output_path": str(resolved_output),
                    },
                )
                self._write_job(job)
            except Exception:
                for path in job_dir.iterdir():
                    path.unlink(missing_ok=True)
                job_dir.rmdir()
                raise
            self._pending_ids.add(job.id)
            self._pending.put(job.id)
            self._condition.notify_all()
            return job

    def get(self, job_id: str) -> RenderJob:
        with self._lock:
            self._validate_job_id(job_id)
            path = self.root / job_id / "job.json"
            if not path.is_file():
                raise VideoEditorError("render_job_not_found", "渲染任务不存在")
            return self._read_job_path(path)

    def list(self, *, project_id: str | None = None) -> list[RenderJob]:
        with self._lock:
            if not self.root.exists():
                return []
            jobs = [
                self._read_job_path(path)
                for path in sorted(self.root.glob("render_*/job.json"))
            ]
            if project_id is not None:
                jobs = [job for job in jobs if job.project_id == project_id]
            return jobs

    def cancel(self, job_id: str) -> RenderJob:
        with self._lock:
            job = self.get(job_id)
            if job.status in _TERMINAL_STATUSES:
                return job
            if job.status == "queued":
                updated = job.model_copy(
                    update={"status": "cancelled", "message": "已取消"}
                )
            else:
                updated = job.model_copy(update={"message": "正在取消"})
                if self._current_id == job_id and self._current_cancel is not None:
                    self._current_cancel.set()
            self._write_job(updated)
            self._condition.notify_all()
            return updated

    def wait(self, job_id: str, *, timeout: float | None = None) -> RenderJob:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                job = self.get(job_id)
                if job.status in _TERMINAL_STATUSES:
                    return job
                remaining = (
                    None if deadline is None else deadline - time.monotonic()
                )
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(f"等待渲染任务超时：{job_id}")
                self._condition.wait(timeout=remaining)

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    job_id = self._pending.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    with self._lock:
                        if self._stop_event.is_set():
                            self._pending_ids.discard(job_id)
                            return
                        self._pending_ids.discard(job_id)
                    self._execute(job_id)
                finally:
                    self._pending.task_done()
        finally:
            with self._lock:
                self._current_id = None
                self._current_cancel = None
                self._worker_lock.release()
                self._worker = None
                self._condition.notify_all()

    def _execute(self, job_id: str) -> None:
        with self._lock:
            job = self.get(job_id)
            if job.status != "queued":
                return
            cancel_event = threading.Event()
            self._current_id = job_id
            self._current_cancel = cancel_event
            running = job.model_copy(
                update={"status": "running", "message": "渲染中", "error": None}
            )
            self._write_job(running)
            self._condition.notify_all()

        try:
            project, project_dir, output_path = self._read_request(job_id)
            self.renderer.render(
                project,
                project_dir=project_dir,
                output_path=output_path,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                completed = running.model_copy(
                    update={"status": "cancelled", "message": "已取消"}
                )
            else:
                completed = running.model_copy(
                    update={
                        "status": "succeeded",
                        "progress": 1,
                        "message": "渲染完成",
                    }
                )
        except VideoEditorError as error:
            if error.code == "render_cancelled" or cancel_event.is_set():
                completed = running.model_copy(
                    update={"status": "cancelled", "message": "已取消"}
                )
            else:
                completed = running.model_copy(
                    update={
                        "status": "failed",
                        "message": "渲染失败",
                        "error": {
                            "code": error.code,
                            "message": error.message,
                            "details": error.details,
                        },
                    }
                )
        except Exception as error:
            completed = running.model_copy(
                update={
                    "status": "failed",
                    "message": "渲染失败",
                    "error": {
                        "code": "render_failed",
                        "message": str(error) or error.__class__.__name__,
                    },
                }
            )

        with self._lock:
            self._write_job(completed)
            self._current_id = None
            self._current_cancel = None
            self._condition.notify_all()

    def _read_request(self, job_id: str) -> tuple[EditorProject, Path, Path]:
        job_dir = self.root / job_id
        try:
            project = EditorProject.model_validate_json(
                (job_dir / "project.json").read_text(encoding="utf-8")
            )
            request = json.loads(
                (job_dir / "request.json").read_text(encoding="utf-8")
            )
            return (
                project,
                Path(request["project_dir"]),
                Path(request["output_path"]),
            )
        except (OSError, KeyError, TypeError, json.JSONDecodeError, ValidationError) as error:
            raise VideoEditorError(
                "render_job_corrupt", "渲染任务快照结构无效"
            ) from error

    def _write_job(self, job: RenderJob) -> None:
        _write_json_atomic(
            self.root / job.id / "job.json", job.model_dump(mode="json")
        )

    @staticmethod
    def _read_job_path(path: Path) -> RenderJob:
        try:
            return RenderJob.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise VideoEditorError(
                "render_job_corrupt", "渲染任务记录结构无效"
            ) from error

    def _new_id(self) -> str:
        for _ in range(3):
            job_id = f"render_{uuid.uuid4().hex[:16]}"
            if not (self.root / job_id).exists():
                return job_id
        raise VideoEditorError("id_generation_failed", "渲染任务 ID 生成失败")

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise VideoEditorError("invalid_render_job_id", "渲染任务 ID 格式无效")

    def _ensure_root(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise VideoEditorError(
                "output_unavailable", f"渲染任务目录写入失败：{error}"
            ) from error


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.stem}-",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    except OSError as error:
        raise VideoEditorError(
            "output_unavailable", f"渲染任务写入失败：{error}"
        ) from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _acquire_os_file_lock(file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl = importlib.import_module("fcntl")
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
