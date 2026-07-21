from __future__ import annotations

import json
import queue as queue_module
import threading
from pathlib import Path

import pytest

from components.video_editor.errors import VideoEditorError
from components.video_editor.jobs import PersistentRenderQueue
from components.video_editor.models import Clip, EditorProject, Track


def _project(project_id: str = "project_0123456789abcdef") -> EditorProject:
    return EditorProject(
        id=project_id,
        name="队列测试",
        revision=3,
        tracks=[
            Track(
                id="track_text",
                media_domain="visual",
                name="字幕",
                clips=[
                    Clip(
                        id="clip_text",
                        kind="text",
                        timeline_start=0,
                        duration=1,
                        text="测试",
                    )
                ],
            )
        ],
    )


def test_queue_persists_public_shape_and_runs_only_one_job_at_a_time(
    tmp_path: Path,
) -> None:
    release = threading.Event()
    started = threading.Event()

    class BlockingRenderer:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.calls = 0
            self.lock = threading.Lock()

        def render(
            self,
            project: EditorProject,
            *,
            project_dir: Path,
            output_path: Path,
            cancel_event: threading.Event,
        ) -> Path:
            with self.lock:
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                started.set()
            assert release.wait(3)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(project.id.encode())
            with self.lock:
                self.active -= 1
            return output_path

    renderer = BlockingRenderer()
    queue = PersistentRenderQueue(tmp_path / "jobs", renderer)
    project_dir = tmp_path / "projects" / _project().id
    first = queue.submit(_project(), project_dir=project_dir)
    second = queue.submit(_project(), project_dir=project_dir)

    persisted = json.loads(
        (tmp_path / "jobs" / first.id / "job.json").read_text(encoding="utf-8")
    )
    assert set(persisted) == {
        "id",
        "project_id",
        "revision",
        "status",
        "progress",
        "message",
        "output_path",
        "error",
    }

    queue.start()
    assert started.wait(2)
    assert queue.get(second.id).status == "queued"
    release.set()

    assert queue.wait(first.id, timeout=3).status == "succeeded"
    assert queue.wait(second.id, timeout=3).status == "succeeded"
    assert renderer.max_active == 1
    assert renderer.calls == 2
    queue.stop()


def test_start_marks_persisted_running_job_failed_and_restores_queued_job(
    tmp_path: Path,
) -> None:
    class ImmediateRenderer:
        def __init__(self) -> None:
            self.calls = 0

        def render(
            self,
            project: EditorProject,
            *,
            project_dir: Path,
            output_path: Path,
            cancel_event: threading.Event,
        ) -> Path:
            self.calls += 1
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"rendered")
            return output_path

    root = tmp_path / "jobs"
    project_dir = tmp_path / "projects" / _project().id
    initial = PersistentRenderQueue(root, ImmediateRenderer())
    interrupted = initial.submit(_project(), project_dir=project_dir)
    queued = initial.submit(_project(), project_dir=project_dir)
    job_path = root / interrupted.id / "job.json"
    payload = json.loads(job_path.read_text(encoding="utf-8"))
    payload.update(status="running", progress=0.4, message="渲染中")
    job_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    renderer = ImmediateRenderer()
    recovered = PersistentRenderQueue(root, renderer)
    recovered.start()

    failed = recovered.get(interrupted.id)
    assert failed.status == "failed"
    assert failed.progress == 0.4
    assert failed.error == {
        "code": "render_interrupted",
        "message": "服务重启导致渲染中断",
    }
    assert recovered.wait(queued.id, timeout=3).status == "succeeded"
    assert renderer.calls == 1
    recovered.stop()


def test_cancel_queued_job_prevents_renderer_execution(tmp_path: Path) -> None:
    class UnexpectedRenderer:
        def render(self, *_args: object, **_kwargs: object) -> Path:
            raise AssertionError("cancelled queued job must not execute")

    queue = PersistentRenderQueue(tmp_path / "jobs", UnexpectedRenderer())
    job = queue.submit(
        _project(),
        project_dir=tmp_path / "projects" / _project().id,
    )

    cancelled = queue.cancel(job.id)
    queue.start()

    assert cancelled.status == "cancelled"
    assert queue.wait(job.id, timeout=1).status == "cancelled"
    queue.stop()


def test_cancel_running_job_sets_event_and_persists_cancelled_status(
    tmp_path: Path,
) -> None:
    started = threading.Event()

    class CancellableRenderer:
        def render(
            self,
            project: EditorProject,
            *,
            project_dir: Path,
            output_path: Path,
            cancel_event: threading.Event,
        ) -> Path:
            started.set()
            assert cancel_event.wait(3)
            raise VideoEditorError("render_cancelled", "渲染任务已取消")

    queue = PersistentRenderQueue(tmp_path / "jobs", CancellableRenderer())
    job = queue.submit(
        _project(),
        project_dir=tmp_path / "projects" / _project().id,
    )
    queue.start()
    assert started.wait(2)

    cancelling = queue.cancel(job.id)
    completed = queue.wait(job.id, timeout=3)

    assert cancelling.message == "正在取消"
    assert completed.status == "cancelled"
    assert completed.progress == 0
    assert json.loads(
        (tmp_path / "jobs" / job.id / "job.json").read_text(encoding="utf-8")
    )["status"] == "cancelled"
    queue.stop()


def test_stop_cancels_running_job_and_leaves_queued_jobs_for_restart(
    tmp_path: Path,
) -> None:
    started = threading.Event()

    class CancellableRenderer:
        def __init__(self) -> None:
            self.calls = 0

        def render(
            self,
            project: EditorProject,
            *,
            project_dir: Path,
            output_path: Path,
            cancel_event: threading.Event,
        ) -> Path:
            self.calls += 1
            if self.calls > 1:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"resumed")
                return output_path
            started.set()
            assert cancel_event.wait(3)
            raise VideoEditorError("render_cancelled", "渲染任务已取消")

    renderer = CancellableRenderer()
    queue = PersistentRenderQueue(tmp_path / "jobs", renderer)
    first = queue.submit(_project(), project_dir=tmp_path / "projects" / _project().id)
    second = queue.submit(_project(), project_dir=tmp_path / "projects" / _project().id)
    queue.start()
    assert started.wait(2)

    queue.stop()

    assert queue.get(first.id).status == "cancelled"
    assert queue.get(second.id).status == "queued"
    assert renderer.calls == 1

    queue.start()
    assert queue.wait(second.id, timeout=3).status == "succeeded"
    queue.stop()


def test_wait_times_out_and_unknown_job_has_stable_error(tmp_path: Path) -> None:
    started = threading.Event()

    class BlockingRenderer:
        def render(
            self,
            project: EditorProject,
            *,
            project_dir: Path,
            output_path: Path,
            cancel_event: threading.Event,
        ) -> Path:
            started.set()
            assert cancel_event.wait(3)
            raise VideoEditorError("render_cancelled", "渲染任务已取消")

    queue = PersistentRenderQueue(tmp_path / "jobs", BlockingRenderer())
    job = queue.submit(
        _project(),
        project_dir=tmp_path / "projects" / _project().id,
    )
    queue.start()
    assert started.wait(2)

    with pytest.raises(TimeoutError):
        queue.wait(job.id, timeout=0.01)
    with pytest.raises(VideoEditorError) as captured:
        queue.get("render_ffffffffffffffff")

    assert captured.value.code == "render_job_not_found"
    queue.cancel(job.id)
    assert queue.wait(job.id, timeout=3).status == "cancelled"
    queue.stop()


def test_only_one_queue_can_own_a_persistent_root(tmp_path: Path) -> None:
    class ImmediateRenderer:
        def render(
            self,
            project: EditorProject,
            *,
            project_dir: Path,
            output_path: Path,
            cancel_event: threading.Event,
        ) -> Path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(project.id.encode())
            return output_path

    root = tmp_path / "jobs"
    first = PersistentRenderQueue(root, ImmediateRenderer())
    second = PersistentRenderQueue(root, ImmediateRenderer())

    first.start()
    with pytest.raises(VideoEditorError) as captured:
        second.start()

    assert captured.value.code == "render_worker_busy"
    first.stop()

    second.start()
    second.stop()


def test_stop_after_worker_waits_for_job_leaves_job_queued(tmp_path: Path) -> None:
    waiting = threading.Event()
    release = threading.Event()
    rendered = threading.Event()

    class GatedPendingQueue(queue_module.Queue[str]):
        def get(self, block: bool = True, timeout: float | None = None) -> str:
            waiting.set()
            assert release.wait(2)
            return super().get(block=block, timeout=timeout)

    class UnexpectedRenderer:
        def render(self, *_args: object, **_kwargs: object) -> Path:
            rendered.set()
            raise AssertionError("stopped worker executed a queued job")

    queue = PersistentRenderQueue(tmp_path / "jobs", UnexpectedRenderer())
    queue._pending = GatedPendingQueue()
    job = queue.submit(
        _project(),
        project_dir=tmp_path / "projects" / _project().id,
    )
    queue.start()
    assert waiting.wait(2)

    queue.stop(wait=False)
    release.set()
    queue.stop(timeout=2)

    assert rendered.is_set() is False
    assert queue.get(job.id).status == "queued"
