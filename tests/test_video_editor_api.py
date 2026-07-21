from __future__ import annotations

import threading
from pathlib import Path

from fastapi.testclient import TestClient

from components.video_editor.api import create_app
from components.video_editor.errors import VideoEditorError
from components.video_editor.jobs import PersistentRenderQueue
from components.video_editor.models import EditorProject, MediaMetadata


def test_project_create_and_command_round_trip(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "editor"))

    created = client.post("/api/v1/projects", json={"name": "测试工程"})
    assert created.status_code == 201
    project = created.json()["data"]

    updated = client.post(
        f"/api/v1/projects/{project['id']}/commands",
        json={
            "expected_revision": 0,
            "commands": [
                {
                    "type": "track.add",
                    "name": "主视频",
                    "media_domain": "visual",
                }
            ],
        },
    )

    assert updated.status_code == 200
    assert updated.json()["data"]["revision"] == 1
    loaded = client.get(f"/api/v1/projects/{project['id']}")
    assert loaded.json()["data"]["tracks"][0]["name"] == "主视频"


def test_openapi_exposes_the_discriminated_command_batch_contract(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "editor"))

    schema = client.get("/openapi.json").json()
    request_schema = schema["paths"][
        "/api/v1/projects/{project_id}/commands"
    ]["post"]["requestBody"]["content"]["application/json"]["schema"]
    batch_name = request_schema["$ref"].rsplit("/", maxsplit=1)[-1]
    batch_schema = schema["components"]["schemas"][batch_name]
    command_items = batch_schema["properties"]["commands"]["items"]

    assert set(batch_schema["required"]) == {"expected_revision", "commands"}
    assert command_items["discriminator"]["propertyName"] == "type"
    assert set(command_items["discriminator"]["mapping"]) == {
        "project.update",
        "asset.add",
        "asset.delete",
        "track.add",
        "track.update",
        "track.move",
        "track.delete",
        "clip.add",
        "clip.update",
        "clip.delete",
        "clip.split",
    }


def test_openapi_exposes_concrete_success_response_models(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "editor"))
    schema = client.get("/openapi.json").json()
    components = schema["components"]["schemas"]

    def response_model(path: str, method: str = "get", status_code: str = "200"):
        response_schema = schema["paths"][path][method]["responses"][status_code][
            "content"
        ]["application/json"]["schema"]
        model_name = response_schema["$ref"].rsplit("/", maxsplit=1)[-1]
        return components[model_name]

    project_paths = [
        ("/api/v1/projects", "post", "201"),
        ("/api/v1/projects/{project_id}", "get", "200"),
        ("/api/v1/projects/{project_id}/commands", "post", "200"),
    ]
    for path, method, status_code in project_paths:
        model = response_model(path, method, status_code)
        assert model["properties"]["data"]["$ref"].endswith("/EditorProject")

    project_list = response_model("/api/v1/projects")
    assert project_list["properties"]["data"]["items"]["$ref"].endswith(
        "/EditorProject"
    )

    asset_import = response_model(
        "/api/v1/projects/{project_id}/assets", "post", "201"
    )
    asset_data_name = asset_import["properties"]["data"]["$ref"].rsplit(
        "/", maxsplit=1
    )[-1]
    asset_data = components[asset_data_name]
    assert asset_data["properties"]["project"]["$ref"].endswith("/EditorProject")
    assert asset_data["properties"]["asset"]["$ref"].endswith("/Asset")

    render_paths = [
        ("/api/v1/projects/{project_id}/renders", "post", "202"),
        ("/api/v1/render-jobs/{job_id}", "get", "200"),
        ("/api/v1/render-jobs/{job_id}", "delete", "200"),
    ]
    for path, method, status_code in render_paths:
        model = response_model(path, method, status_code)
        assert model["properties"]["data"]["$ref"].endswith("/RenderJob")


def test_revision_conflict_uses_stable_error_shape(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "editor"))
    project = client.post("/api/v1/projects", json={"name": "冲突测试"}).json()["data"]

    response = client.post(
        f"/api/v1/projects/{project['id']}/commands",
        json={
            "expected_revision": 9,
            "commands": [
                {"type": "track.add", "media_domain": "visual", "name": "主视频"}
            ],
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "ok": False,
        "error": {
            "code": "revision_conflict",
            "message": "工程已被其他写入者更新",
            "details": {"actual_revision": 0, "expected_revision": 9},
        },
    }


def test_upload_registers_asset_and_serves_project_scoped_media(
    tmp_path: Path, monkeypatch
) -> None:
    client = TestClient(create_app(tmp_path / "editor"))
    project = client.post("/api/v1/projects", json={"name": "素材测试"}).json()["data"]

    monkeypatch.setattr(
        "components.video_editor.api.probe_media",
        lambda _path: MediaMetadata(
            duration=2.5,
            width=640,
            height=360,
            frame_rate=25,
            video_codec="h264",
            audio_codec="aac",
            sample_rate=48_000,
            channels=2,
        ),
    )
    response = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        data={"expected_revision": "0"},
        files={"file": ("sample.mp4", b"video-bytes", "video/mp4")},
    )

    assert response.status_code == 201
    data = response.json()["data"]
    asset = data["asset"]
    assert data["project"]["revision"] == 1
    assert asset["kind"] == "video"

    media = client.get(
        f"/api/v1/projects/{project['id']}/assets/{asset['id']}/content"
    )
    assert media.status_code == 200
    assert media.content == b"video-bytes"


def test_upload_rejects_media_above_the_fixed_size_limit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("components.video_editor.api.MAX_MEDIA_BYTES", 4, raising=False)
    client = TestClient(create_app(tmp_path / "editor"))
    project = client.post("/api/v1/projects", json={"name": "素材上限"}).json()["data"]

    response = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        data={"expected_revision": "0"},
        files={"file": ("too-large.mp4", b"12345", "video/mp4")},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "media_too_large"
    assert list((tmp_path / "editor" / project["id"] / "assets").glob("*")) == []


def test_editor_shell_and_health_are_available(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "editor"))

    health = client.get("/api/v1/health")
    page = client.get("/")

    assert health.status_code == 200
    assert health.json()["data"]["status"] == "ok"
    assert page.status_code == 200
    assert "本地视频剪辑器" in page.text


def test_missing_project_uses_stable_404_error(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "editor"))

    response = client.get("/api/v1/projects/project_0000000000000000")

    assert response.status_code == 404
    assert response.json() == {
        "ok": False,
        "error": {
            "code": "project_not_found",
            "message": "工程不存在",
            "details": {},
        },
    }


def test_asset_content_supports_byte_ranges(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_app(tmp_path / "editor"))
    project = client.post("/api/v1/projects", json={"name": "范围请求"}).json()["data"]
    monkeypatch.setattr(
        "components.video_editor.api.probe_media",
        lambda _path: MediaMetadata(
            duration=2.5,
            width=640,
            height=360,
            frame_rate=25,
            video_codec="h264",
            audio_codec="aac",
            sample_rate=48_000,
            channels=2,
        ),
    )
    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        data={"expected_revision": "0"},
        files={"file": ("sample.mp4", b"0123456789", "video/mp4")},
    ).json()["data"]["asset"]

    response = client.get(
        f"/api/v1/projects/{project['id']}/assets/{uploaded['id']}/content",
        headers={"Range": "bytes=2-5"},
    )

    assert response.status_code == 206
    assert response.headers["accept-ranges"] == "bytes"
    assert response.content == b"2345"


def test_render_job_lifecycle_and_output_range(tmp_path: Path) -> None:
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
            output_path.write_bytes(b"rendered-video")
            return output_path

    root = tmp_path / "editor"
    queue = PersistentRenderQueue(tmp_path / "jobs", ImmediateRenderer())
    with TestClient(create_app(root, render_queue=queue)) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "渲染工程"}
        ).json()["data"]

        submitted = client.post(
            f"/api/v1/projects/{project['id']}/renders",
            json={"expected_revision": 0},
        )
        assert submitted.status_code == 202
        job_id = submitted.json()["data"]["id"]
        assert queue.wait(job_id, timeout=2).status == "succeeded"

        loaded = client.get(f"/api/v1/render-jobs/{job_id}")
        output = client.get(
            f"/api/v1/render-jobs/{job_id}/output",
            headers={"Range": "bytes=0-7"},
        )

    assert loaded.status_code == 200
    assert loaded.json()["data"]["status"] == "succeeded"
    assert output.status_code == 206
    assert output.content == b"rendered"


def test_render_job_can_be_cancelled(tmp_path: Path) -> None:
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

    root = tmp_path / "editor"
    queue = PersistentRenderQueue(tmp_path / "jobs", CancellableRenderer())
    with TestClient(create_app(root, render_queue=queue)) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "取消渲染"}
        ).json()["data"]
        created = client.post(
            f"/api/v1/projects/{project['id']}/renders",
            json={"expected_revision": 0},
        ).json()["data"]
        assert started.wait(2)

        response = client.delete(f"/api/v1/render-jobs/{created['id']}")
        completed = queue.wait(created["id"], timeout=3)

        assert response.status_code == 200
        assert completed.status == "cancelled"
