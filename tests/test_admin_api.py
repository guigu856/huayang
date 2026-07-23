from __future__ import annotations

import base64
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from video_create_plugin import ArtifactStore, WorkflowService
from video_create_plugin.admin.api import create_app
from video_create_plugin.repository import WorkflowRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def admin_client(tmp_path: Path) -> tuple[TestClient, Path, Path]:
    resource_root = tmp_path / "resources"
    for directory in ("rules", "skills", "schemas"):
        shutil.copytree(PROJECT_ROOT / directory, resource_root / directory)
    output_root = tmp_path / "output"
    return (
        TestClient(create_app(resource_root=resource_root, output_root=output_root)),
        resource_root,
        output_root,
    )


def _create_artifact(output_root: Path):
    store = ArtifactStore(output_root / "objects")
    service = WorkflowService(
        WorkflowRepository(output_root / "workflow.sqlite3"),
        store,
    )
    task = service.create_task("original_creation")
    envelope = service.get_stage_envelope(task.task_id)
    artifact = service.submit_artifact(
        access_handle=envelope.stage_access_handle,
        artifact_type="creative_direction",
        content=store.put_text('{"title": "测试总体方案"}'),
        schema_version="1.0",
        producer_kind="agent",
        producer_id="admin-api-test",
        model_id="test-model",
    )
    return task, artifact


def test_admin_shell_health_and_static_assets_are_available(
    admin_client: tuple[TestClient, Path, Path],
) -> None:
    client, _resource_root, _output_root = admin_client

    health = client.get("/api/v1/health")
    overview = client.get("/api/v1/overview")
    page = client.get("/")
    script = client.get("/static/app.js")

    assert health.status_code == 200
    assert health.json() == {"ok": True, "data": {"status": "ok", "plugin": "huayang"}}
    assert overview.status_code == 200
    assert overview.json()["data"]["plugin_name"] == "huayang"
    assert page.status_code == 200
    assert "Huayang · 视频创作管理" in page.text
    assert "学习产物" in page.text
    assert script.status_code == 200
    assert 'view: "overview"' in script.text


def test_resource_crud_api_preserves_revision_and_protection_errors(
    admin_client: tuple[TestClient, Path, Path],
) -> None:
    client, _resource_root, _output_root = admin_client

    created_response = client.post(
        "/api/v1/resources/rule",
        json={
            "resource_id": "api-reviewer",
            "title": "API 复核角色",
            "description": "通过后台接口维护",
            "content": "",
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()["data"]
    assert created["builtin"] is False

    stale = client.put(
        "/api/v1/resources/rule/api-reviewer",
        json={"expected_sha256": "0" * 64, "content": "# API 复核角色\n\n旧版本"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "resource_revision_conflict"

    updated_response = client.put(
        "/api/v1/resources/rule/api-reviewer",
        json={
            "expected_sha256": created["sha256"],
            "content": "# API 复核角色\n\n只复核当前阶段。",
        },
    )
    assert updated_response.status_code == 200
    updated = updated_response.json()["data"]
    assert updated["sha256"] != created["sha256"]

    builtin = client.get("/api/v1/resources/rule/main-agent").json()["data"]
    protected = client.delete(
        "/api/v1/resources/rule/main-agent",
        params={"expected_sha256": builtin["sha256"]},
    )
    assert protected.status_code == 409
    assert protected.json()["error"]["code"] == "resource_protected"

    deleted = client.delete(
        "/api/v1/resources/rule/api-reviewer",
        params={"expected_sha256": updated["sha256"]},
    )
    assert deleted.status_code == 204
    assert client.get("/api/v1/resources/rule/api-reviewer").status_code == 404


def test_output_api_lists_scoped_products_and_serves_content(
    admin_client: tuple[TestClient, Path, Path],
) -> None:
    client, _resource_root, output_root = admin_client
    creation = output_root / "renders" / "final.mp4"
    learning = output_root / "validation" / "reference_learning" / "report.md"
    creation.parent.mkdir(parents=True, exist_ok=True)
    creation.write_bytes(b"render-bytes")
    learning.parent.mkdir(parents=True, exist_ok=True)
    learning.write_text("# 学习报告", encoding="utf-8")

    creation_response = client.get("/api/v1/outputs", params={"scope": "creation"})
    learning_response = client.get("/api/v1/outputs", params={"scope": "learning"})
    paged_response = client.get(
        "/api/v1/outputs",
        params={"scope": "all", "limit": 1, "offset": 1},
    )

    assert creation_response.status_code == 200
    creation_entry = creation_response.json()["data"][0]
    assert creation_entry["relative_path"] == "renders/final.mp4"
    assert learning_response.json()["data"][0]["relative_path"] == (
        "validation/reference_learning/report.md"
    )
    assert len(paged_response.json()["data"]) == 1

    content = client.get(creation_entry["content_url"])
    assert content.status_code == 200
    assert content.content == b"render-bytes"
    assert content.headers["content-type"].startswith("video/mp4")


def test_output_api_serves_html_as_plain_text(
    admin_client: tuple[TestClient, Path, Path],
) -> None:
    client, _resource_root, output_root = admin_client
    report = output_root / "renders" / "report.html"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("<script>window.hacked = true</script>", encoding="utf-8")
    entry = client.get("/api/v1/outputs", params={"scope": "creation"}).json()["data"][0]

    response = client.get(entry["content_url"])

    assert response.status_code == 200
    assert response.text == "<script>window.hacked = true</script>"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "relative_path",
    [
        ".hidden/secret.json",
        "package-audit-target/plugin.json",
        "knowledge/knowledge.lancedb/version.json",
        "objects/aa/artifact.json",
    ],
)
def test_output_api_rejects_ids_for_hidden_output_directories(
    admin_client: tuple[TestClient, Path, Path],
    relative_path: str,
) -> None:
    client, _resource_root, output_root = admin_client
    hidden = output_root / Path(relative_path)
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text('{"secret": true}', encoding="utf-8")
    output_id = base64.urlsafe_b64encode(relative_path.encode("utf-8")).decode("ascii").rstrip("=")

    listed = client.get("/api/v1/outputs", params={"scope": "all", "limit": 1000})
    response = client.get(f"/api/v1/outputs/{output_id}/content")

    assert all(item["relative_path"] != relative_path for item in listed.json()["data"])
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "output_not_found"


def test_workflow_api_lists_and_reads_artifacts(
    admin_client: tuple[TestClient, Path, Path],
) -> None:
    client, _resource_root, output_root = admin_client
    task, artifact = _create_artifact(output_root)

    tasks = client.get("/api/v1/workflow/tasks", params={"source_id": "primary"})
    sources = client.get("/api/v1/workflow/sources")
    artifacts = client.get(
        "/api/v1/workflow/artifacts",
        params={"source_id": "primary", "task_id": task.task_id},
    )

    assert tasks.status_code == 200
    assert sources.json()["data"] == ["primary"]
    assert tasks.json()["data"][0]["task_id"] == task.task_id
    assert artifacts.status_code == 200
    assert artifacts.json()["data"][0]["artifact_id"] == artifact.artifact_id

    content = client.get(f"/api/v1/workflow/primary/artifacts/{artifact.artifact_id}/content")
    assert content.status_code == 200
    assert content.headers["content-type"].startswith("application/json")
    assert content.headers["etag"] == f'"{artifact.content_sha256}"'
    assert content.json() == {"title": "测试总体方案"}
