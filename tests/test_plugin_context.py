from pathlib import Path

import pytest

from video_create_plugin.context import ContextCatalog
from video_create_plugin.errors import PluginError


def test_bootstrap_loads_only_main_rule_and_router_skill() -> None:
    catalog = ContextCatalog()

    main_rule, router = catalog.bootstrap_bundle()

    assert main_rule.uri == "huayang://rules/main-agent"
    assert router.uri == "huayang://skills/video-task-router"
    assert "自然语言" in main_rule.content
    assert "任务类型" in router.content


def test_catalog_rejects_paths_and_unregistered_resource_ids(tmp_path: Path) -> None:
    catalog = ContextCatalog(tmp_path)

    with pytest.raises(PluginError) as captured:
        catalog.read("huayang://rules/../../AGENTS.md")

    assert captured.value.code == "context_resource_not_found"


def test_creation_stages_one_to_three_can_search_shared_knowledge() -> None:
    catalog = ContextCatalog()

    for stage in (
        "creative_direction",
        "resource_preparation",
        "editing_specification",
    ):
        bundle = catalog.stage_bundle("original_creation", stage)
        assert "knowledge_search" in bundle.tool_ids
        assert "reference_get_creation_context" not in bundle.tool_ids

        guided = catalog.stage_bundle("reference_guided_creation", stage)
        assert "knowledge_search" in guided.tool_ids
        assert "reference_get_creation_context" in guided.tool_ids

    execution = catalog.stage_bundle("original_creation", "execution")
    assert "knowledge_search" not in execution.tool_ids


def test_stage_bundle_is_limited_to_the_task_type() -> None:
    catalog = ContextCatalog()

    with pytest.raises(PluginError) as captured:
        catalog.stage_bundle("reference_study", "creative_direction")

    assert captured.value.code == "stage_not_allowed"


def test_registered_resources_exist_and_have_unique_uris() -> None:
    catalog = ContextCatalog()

    resources = catalog.catalog()

    assert len({resource.uri for resource in resources}) == len(resources)
    assert all(resource.content.strip() for resource in resources)
