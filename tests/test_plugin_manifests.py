from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_host_manifests_share_one_plugin_identity_and_content_root() -> None:
    codex = _json(ROOT / ".codex-plugin/plugin.json")
    claude = _json(ROOT / ".claude-plugin/plugin.json")

    assert codex["name"] == claude["name"] == "huayang"
    assert codex["version"] == claude["version"]
    assert codex["skills"] == claude["skills"] == "./skills/"
    assert codex["mcpServers"] == "./.mcp.json"
    assert (ROOT / "skills").is_dir()
    assert (ROOT / "rules/main-agent.md").is_file()
    skill_files = list((ROOT / "skills").glob("*/SKILL.md"))
    assert skill_files
    assert all(path.is_file() for path in skill_files)


def test_mcp_manifest_targets_the_packaged_console_entrypoint() -> None:
    codex = _json(ROOT / ".codex-plugin/plugin.json")
    mcp = _json(ROOT / ".mcp.json")
    servers = mcp["mcpServers"]
    assert isinstance(servers, dict)
    server = servers["huayang"]
    assert isinstance(server, dict)
    assert server == {
        "command": "huayang-mcp",
        "args": [],
    }

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == "huayang"
    assert project["project"]["version"] == codex["version"]
    scripts = project["project"]["scripts"]
    assert scripts["huayang"] == "video_create_plugin.admin.__main__:main"
    assert scripts["huayang-mcp"] == "video_create_plugin.mcp.server:main"
