from __future__ import annotations

import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from video_create_plugin import ArtifactStore, PluginError, WorkflowService
from video_create_plugin.admin.models import (
    ResourceCreateRequest,
    ResourceUpdateRequest,
)
from video_create_plugin.admin.service import AdminService
from video_create_plugin.repository import WorkflowRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def resource_root(tmp_path: Path) -> Path:
    root = tmp_path / "resources"
    for directory in ("rules", "skills", "schemas"):
        shutil.copytree(PROJECT_ROOT / directory, root / directory)
    return root


def _workflow_service(root: Path) -> tuple[WorkflowService, ArtifactStore]:
    store = ArtifactStore(root / "objects")
    return WorkflowService(WorkflowRepository(root / "workflow.sqlite3"), store), store


def _submit_text_artifact(
    root: Path,
    *,
    task_type: str,
    artifact_type: str,
    content: str,
):
    service, store = _workflow_service(root)
    task = service.create_task(task_type)  # type: ignore[arg-type]
    envelope = service.get_stage_envelope(task.task_id)
    artifact = service.submit_artifact(
        access_handle=envelope.stage_access_handle,
        artifact_type=artifact_type,
        content=store.put_text(content),
        schema_version="1.0",
        producer_kind="component",
        producer_id="admin-test",
        component_version="1.0.0",
    )
    return task, artifact


def test_rule_and_skill_crud_are_discovered_by_the_live_catalog(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    service = AdminService(resource_root, tmp_path / "output")

    rule = service.create_resource(
        "rule",
        ResourceCreateRequest(
            resource_id="custom-reviewer",
            title="自定义复核角色",
            description="复核阶段产物",
        ),
    )
    assert rule.uri == "huayang://rules/custom-reviewer"
    assert rule.relative_path == "rules/custom/custom-reviewer.md"
    assert rule.builtin is False
    assert "# 自定义复核角色" in rule.content

    updated_rule = service.update_resource(
        "rule",
        rule.resource_id,
        ResourceUpdateRequest(
            expected_sha256=rule.sha256,
            content="# 自定义复核角色\n\n只复核当前阶段的冻结产物。",
        ),
    )
    assert updated_rule.sha256 != rule.sha256
    assert "冻结产物" in updated_rule.content

    skill = service.create_resource(
        "skill",
        ResourceCreateRequest(
            resource_id="custom-review",
            title="自定义复核",
            description="按检查表复核产物",
        ),
    )
    assert skill.uri == "huayang://skills/custom-review"
    assert skill.relative_path == "skills/custom-review/SKILL.md"
    assert "name: custom-review" in skill.content
    assert any(
        item.resource_id == "custom-review" and item.kind == "skill"
        for item in service.list_resources("skill")
    )

    service.delete_resource("rule", rule.resource_id, updated_rule.sha256)
    service.delete_resource("skill", skill.resource_id, skill.sha256)

    with pytest.raises(PluginError) as deleted_rule:
        service.get_resource("rule", rule.resource_id)
    assert deleted_rule.value.code == "context_resource_not_found"
    assert not (resource_root / "skills" / skill.resource_id).exists()


def test_resource_revision_conflicts_and_builtin_delete_protection(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    service = AdminService(resource_root, tmp_path / "output")
    builtin = service.get_resource("rule", "main-agent")

    with pytest.raises(PluginError) as protected:
        service.delete_resource("rule", builtin.resource_id, builtin.sha256)
    assert protected.value.code == "resource_protected"

    with pytest.raises(PluginError) as stale_update:
        service.update_resource(
            "rule",
            builtin.resource_id,
            ResourceUpdateRequest(
                expected_sha256="0" * 64,
                content="# 主 Agent 规则\n\n更新内容",
            ),
        )
    assert stale_update.value.code == "resource_revision_conflict"

    custom = service.create_resource(
        "rule",
        ResourceCreateRequest(
            resource_id="revision-test",
            title="版本测试",
            description="验证乐观并发控制",
        ),
    )
    (resource_root / custom.relative_path).write_text("# 外部更新\n", encoding="utf-8")

    with pytest.raises(PluginError) as stale_delete:
        service.delete_resource("rule", custom.resource_id, custom.sha256)
    assert stale_delete.value.code == "resource_revision_conflict"


def test_concurrent_updates_with_the_same_sha_accept_exactly_one_writer(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    service = AdminService(resource_root, tmp_path / "output")
    resource = service.create_resource(
        "rule",
        ResourceCreateRequest(
            resource_id="concurrent-review",
            title="并发复核",
            description="验证同版本写入",
        ),
    )
    start = Barrier(3)

    def update(content: str) -> tuple[str, str]:
        start.wait()
        try:
            updated = service.update_resource(
                "rule",
                resource.resource_id,
                ResourceUpdateRequest(
                    expected_sha256=resource.sha256,
                    content=content,
                ),
            )
        except PluginError as error:
            return "error", error.code
        return "updated", updated.content

    candidates = {
        "# 并发复核\n\n写入者一",
        "# 并发复核\n\n写入者二",
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(update, content) for content in candidates]
        start.wait()
        results = [future.result(timeout=5) for future in futures]

    assert sorted(status for status, _value in results) == ["error", "updated"]
    assert [value for status, value in results if status == "error"] == [
        "resource_revision_conflict"
    ]
    winner = next(value for status, value in results if status == "updated")
    assert winner.rstrip() in candidates
    assert service.get_resource("rule", resource.resource_id).content == winner


def test_create_resource_rejects_a_parent_directory_outside_the_resource_root(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    custom_root = resource_root / "rules" / "custom"
    custom_root.rmdir()
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(custom_root), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        custom_root.symlink_to(outside, target_is_directory=True)

    try:
        service = AdminService(resource_root, tmp_path / "output")
        with pytest.raises(PluginError) as invalid_path:
            service.create_resource(
                "rule",
                ResourceCreateRequest(
                    resource_id="escaped-rule",
                    title="越界规则",
                    description="路径应留在资源根目录",
                ),
            )

        assert invalid_path.value.code == "resource_write_failed"
        assert not (outside / "escaped-rule.md").exists()
    finally:
        if os.name == "nt":
            os.rmdir(custom_root)
        else:
            custom_root.unlink()


def test_deleting_a_custom_skill_removes_its_support_files(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    service = AdminService(resource_root, tmp_path / "output")
    skill = service.create_resource(
        "skill",
        ResourceCreateRequest(
            resource_id="scripted-review",
            title="带脚本的复核",
            description="调用本地确定性脚本",
        ),
    )
    skill_root = resource_root / "skills" / skill.resource_id
    helper = skill_root / "scripts" / "check.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("print('checked')\n", encoding="utf-8")

    service.delete_resource("skill", skill.resource_id, skill.sha256)

    assert not skill_root.exists()


@pytest.mark.parametrize(
    ("resource_id", "content"),
    [
        (
            "missing-close",
            "---\nname: missing-close\ndescription: 缺少结束标记\n# 正文",
        ),
        (
            "empty-description",
            "---\nname: empty-description\ndescription:\n---\n\n# 空说明",
        ),
        (
            "quoted-empty-description",
            '---\nname: quoted-empty-description\ndescription: ""\n---\n\n# 空说明',
        ),
        (
            "invalid-yaml",
            "---\nname: invalid-yaml\ndescription: 合法说明\nbad: [\n---\n\n# 无效 YAML",
        ),
    ],
)
def test_skill_frontmatter_requires_a_closing_marker_and_nonempty_description(
    resource_root: Path,
    tmp_path: Path,
    resource_id: str,
    content: str,
) -> None:
    service = AdminService(resource_root, tmp_path / "output")

    with pytest.raises(PluginError) as invalid:
        service.create_resource(
            "skill",
            ResourceCreateRequest(
                resource_id=resource_id,
                title="无效 Skill",
                description="请求元数据有效，但正文前置信息无效",
                content=content,
            ),
        )

    assert invalid.value.code == "invalid_skill_document"
    assert not (resource_root / "skills" / resource_id).exists()


def test_output_listing_classifies_creation_learning_and_system_files(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    creation = output_root / "editor" / "project-1" / "result.mp4"
    learning = output_root / "validation" / "reference_learning" / "report.json"
    system = output_root / "logs" / "summary.txt"
    object_file = output_root / "objects" / "aa" / "hidden.json"
    unsupported = output_root / "editor" / "project-1" / "cache.bin"
    for path, content in (
        (creation, b"video"),
        (learning, b'{"result": "learned"}'),
        (system, b"log"),
        (object_file, b"hidden"),
        (unsupported, b"cache"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    service = AdminService(resource_root, output_root)
    all_outputs = service.list_outputs(scope="all", limit=100)

    assert {(item.relative_path, item.scope) for item in all_outputs} == {
        ("editor/project-1/result.mp4", "creation"),
        ("validation/reference_learning/report.json", "learning"),
        ("logs/summary.txt", "system"),
    }
    creation_entry = service.list_outputs(scope="creation", limit=100)[0]
    assert creation_entry.kind == "video"
    assert service.resolve_output(creation_entry.output_id).read_bytes() == b"video"
    assert [item.relative_path for item in service.list_outputs(scope="learning", limit=100)] == [
        "validation/reference_learning/report.json"
    ]
    first_page = service.list_outputs(scope="all", limit=2, offset=0)
    second_page = service.list_outputs(scope="all", limit=2, offset=2)
    assert {item.output_id for item in first_page + second_page} == {
        item.output_id for item in all_outputs
    }
    assert service.count_outputs(scope="creation") == 1
    assert service.count_outputs(scope="learning") == 1


def test_workflow_queries_include_primary_and_reference_validation_sources(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    primary_task, primary_artifact = _submit_text_artifact(
        output_root,
        task_type="original_creation",
        artifact_type="creative_notes",
        content="创作产物",
    )
    reference_root = output_root / "validation" / "reference_publication"
    reference_task, reference_artifact = _submit_text_artifact(
        reference_root,
        task_type="reference_study",
        artifact_type="visual_analysis",
        content="学习产物",
    )

    service = AdminService(resource_root, output_root)
    tasks = service.list_tasks(limit=20)
    artifacts = service.list_artifacts(limit=20)

    assert {(item["source_id"], item["task_id"]) for item in tasks} == {
        ("primary", primary_task.task_id),
        ("reference-validation", reference_task.task_id),
    }
    assert {(item["source_id"], item["artifact_id"]) for item in artifacts} == {
        ("primary", primary_artifact.artifact_id),
        ("reference-validation", reference_artifact.artifact_id),
    }
    assert (
        service.list_tasks(source_id="reference-validation", limit=20)[0]["task_id"]
        == reference_task.task_id
    )
    artifact, content = service.read_artifact(
        "reference-validation",
        reference_artifact.artifact_id,
    )
    assert artifact.content_sha256 == reference_artifact.content_sha256
    assert content.decode("utf-8") == "学习产物"

    overview = service.overview()
    assert overview.task_count == 2
    assert overview.artifact_count == 2
    assert {
        service.list_artifacts(limit=1, offset=0)[0]["artifact_id"],
        service.list_artifacts(limit=1, offset=1)[0]["artifact_id"],
    } == {primary_artifact.artifact_id, reference_artifact.artifact_id}
    assert service.workflow_source_ids() == ["primary", "reference-validation"]


def test_reference_validation_source_is_discovered_after_admin_startup(
    resource_root: Path,
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    service = AdminService(resource_root, output_root)
    assert "reference-validation" not in service.workflow_sources

    reference_root = output_root / "validation" / "reference_publication"
    task, artifact = _submit_text_artifact(
        reference_root,
        task_type="reference_study",
        artifact_type="reference_report_manifest",
        content="后台启动后生成的学习报告",
    )

    tasks = service.list_tasks(source_id="reference-validation", limit=20)
    artifacts = service.list_artifacts(source_id="reference-validation", limit=20)

    assert tasks[0]["task_id"] == task.task_id
    assert artifacts[0]["artifact_id"] == artifact.artifact_id
    assert "reference-validation" in service.workflow_sources
