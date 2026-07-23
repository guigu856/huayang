from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_create_plugin.codex_install import (
    build_codex_marketplace,
    default_marketplace_root,
)


def _write_source(root: Path) -> None:
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "huayang", "version": "0.1.0"}),
        encoding="utf-8",
    )
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "huayang": {"command": "huayang-mcp", "args": []},
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "rules/common").mkdir(parents=True)
    (root / "rules/main-agent.md").write_text("main rule", encoding="utf-8")
    (root / "rules/common/shared.md").write_text("shared rule", encoding="utf-8")
    (root / "skills/router").mkdir(parents=True)
    (root / "skills/router/SKILL.md").write_text("router skill", encoding="utf-8")
    (root / "schemas").mkdir(parents=True)
    (root / "schemas/task.json").write_text('{"type":"object"}', encoding="utf-8")


def _relative_files(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def test_builds_minimal_marketplace_from_explicit_allowlist(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "marketplace"
    _write_source(source)

    (source / "output").mkdir()
    (source / "output/render.mp4").write_bytes(b"render")
    (source / ".venv").mkdir()
    (source / ".venv/python.exe").write_bytes(b"python")
    (source / "video_create_plugin").mkdir()
    (source / "video_create_plugin/server.py").write_text("source code", encoding="utf-8")
    (source / "README.md").write_text("docs", encoding="utf-8")

    result = build_codex_marketplace(source, destination)

    assert result == destination.resolve()
    assert _relative_files(result) == {
        ".agents/plugins/marketplace.json",
        "plugins/huayang/.codex-plugin/plugin.json",
        "plugins/huayang/.mcp.json",
        "plugins/huayang/rules/common/shared.md",
        "plugins/huayang/rules/main-agent.md",
        "plugins/huayang/schemas/task.json",
        "plugins/huayang/skills/router/SKILL.md",
    }

    plugin_root = result / "plugins/huayang"
    for relative_path in (
        Path(".codex-plugin/plugin.json"),
        Path(".mcp.json"),
        Path("rules/main-agent.md"),
        Path("rules/common/shared.md"),
        Path("schemas/task.json"),
        Path("skills/router/SKILL.md"),
    ):
        assert (plugin_root / relative_path).read_bytes() == (source / relative_path).read_bytes()

    marketplace = json.loads(
        (result / ".agents/plugins/marketplace.json").read_text(encoding="utf-8")
    )
    assert marketplace == {
        "name": "huayang-local",
        "interface": {"displayName": "Huayang Local"},
        "plugins": [
            {
                "name": "huayang",
                "source": {
                    "source": "local",
                    "path": "./plugins/huayang",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Creativity",
            }
        ],
    }


def test_rebuild_replaces_only_a_matching_owned_marketplace(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "marketplace"
    _write_source(source)
    build_codex_marketplace(source, destination)
    stale = destination / "plugins/huayang/rules/stale.md"
    stale.write_text("stale", encoding="utf-8")
    (source / "rules/main-agent.md").write_text("updated", encoding="utf-8")

    build_codex_marketplace(source, destination)

    assert not stale.exists()
    assert (destination / "plugins/huayang/rules/main-agent.md").read_text(
        encoding="utf-8"
    ) == "updated"


def test_refuses_to_replace_an_unmanaged_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "existing"
    _write_source(source)
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="not an owned Huayang marketplace"):
        build_codex_marketplace(source, destination)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_rejects_overlapping_source_and_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source)

    with pytest.raises(ValueError, match="must not overlap"):
        build_codex_marketplace(source, source / "generated-marketplace")

    assert (source / ".codex-plugin/plugin.json").is_file()


def test_requires_the_requested_plugin_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source)
    (source / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "another-plugin"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest name must match"):
        build_codex_marketplace(source, tmp_path / "marketplace")


def test_default_marketplace_root_uses_local_app_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert default_marketplace_root() == (tmp_path / "huayang/marketplace").resolve()
