from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

DEFAULT_MARKETPLACE_NAME = "huayang-local"
DEFAULT_PLUGIN_NAME = "huayang"

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PLUGIN_FILES = (Path(".codex-plugin/plugin.json"), Path(".mcp.json"))
_PLUGIN_DIRECTORIES = (Path("rules"), Path("skills"), Path("schemas"))


def default_marketplace_root() -> Path:
    """Return the dedicated local marketplace path used by Huayang."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data).expanduser() / "huayang" / "marketplace").resolve()
    return (Path.home() / ".huayang" / "marketplace").resolve()


def build_codex_marketplace(
    source_root: Path,
    destination_root: Path | None = None,
    *,
    marketplace_name: str = DEFAULT_MARKETPLACE_NAME,
    plugin_name: str = DEFAULT_PLUGIN_NAME,
) -> Path:
    """Build a minimal local Codex marketplace and return its absolute path.

    Only the Codex manifest, MCP manifest, rules, skills, and schemas are copied.
    An existing destination is replaced only when its marketplace manifest proves
    that it was previously generated for the same marketplace and plugin.
    """

    _validate_identifier("marketplace_name", marketplace_name)
    _validate_identifier("plugin_name", plugin_name)

    source = source_root.expanduser().resolve()
    destination = (
        default_marketplace_root()
        if destination_root is None
        else destination_root.expanduser().resolve()
    )

    _validate_source(source, plugin_name)
    _validate_separate_trees(source, destination)
    _validate_destination(destination, marketplace_name, plugin_name)

    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-staging-", dir=parent)).resolve()

    try:
        _populate_marketplace(
            source,
            staging,
            marketplace_name=marketplace_name,
            plugin_name=plugin_name,
        )
        if destination.exists():
            _remove_owned_tree(destination, parent)
        staging.replace(destination)
    finally:
        if staging.exists():
            _remove_staging_tree(staging, parent, destination.name)

    return destination


def _validate_identifier(label: str, value: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must use lowercase letters, numbers, '.', '_' or '-'")


def _validate_source(source: Path, plugin_name: str) -> None:
    if not source.is_dir():
        raise FileNotFoundError(f"plugin source root does not exist: {source}")

    for relative_path in _PLUGIN_FILES:
        path = source / relative_path
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(f"required plugin file is missing: {relative_path}")

    for relative_path in _PLUGIN_DIRECTORIES:
        path = source / relative_path
        if path.is_symlink() or not path.is_dir():
            raise FileNotFoundError(f"required plugin directory is missing: {relative_path}")

    plugin_manifest_path = source / ".codex-plugin/plugin.json"
    plugin_manifest: object = json.loads(plugin_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(plugin_manifest, dict) or plugin_manifest.get("name") != plugin_name:
        raise ValueError(f"plugin manifest name must match requested plugin name: {plugin_name}")


def _validate_separate_trees(source: Path, destination: Path) -> None:
    if (
        source == destination
        or source.is_relative_to(destination)
        or destination.is_relative_to(source)
    ):
        raise ValueError("source and destination trees must not overlap")

    if destination == Path(destination.anchor):
        raise ValueError("destination must not be a filesystem root")


def _validate_destination(
    destination: Path,
    marketplace_name: str,
    plugin_name: str,
) -> None:
    if destination.is_symlink():
        raise ValueError(f"destination must not be a symbolic link: {destination}")
    if not destination.exists():
        return
    if not destination.is_dir():
        raise ValueError(f"destination must be a directory: {destination}")
    if not _is_owned_marketplace(destination, marketplace_name, plugin_name):
        raise ValueError(f"destination is not an owned Huayang marketplace: {destination}")


def _is_owned_marketplace(root: Path, marketplace_name: str, plugin_name: str) -> bool:
    manifest_path = root / ".agents/plugins/marketplace.json"
    try:
        manifest: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False

    if not isinstance(manifest, dict) or manifest.get("name") != marketplace_name:
        return False
    plugins = manifest.get("plugins")
    if not isinstance(plugins, list) or len(plugins) != 1:
        return False
    plugin = plugins[0]
    if not isinstance(plugin, dict) or plugin.get("name") != plugin_name:
        return False
    return plugin.get("source") == {
        "source": "local",
        "path": f"./plugins/{plugin_name}",
    }


def _populate_marketplace(
    source: Path,
    destination: Path,
    *,
    marketplace_name: str,
    plugin_name: str,
) -> None:
    plugin_root = destination / "plugins" / plugin_name
    for relative_path in _PLUGIN_FILES:
        _copy_file(source / relative_path, plugin_root / relative_path)
    for relative_path in _PLUGIN_DIRECTORIES:
        _copy_tree(source / relative_path, plugin_root / relative_path)

    marketplace_manifest = {
        "name": marketplace_name,
        "interface": {
            "displayName": f"{plugin_name.replace('-', ' ').title()} Local",
        },
        "plugins": [
            {
                "name": plugin_name,
                "source": {
                    "source": "local",
                    "path": f"./plugins/{plugin_name}",
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Creativity",
            }
        ],
    }
    manifest_path = destination / ".agents/plugins/marketplace.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(marketplace_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"plugin bundle entries must be regular files: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for entry in sorted(source.rglob("*")):
        if entry.is_symlink():
            raise ValueError(f"plugin bundle entries must not be symbolic links: {entry}")
        relative_path = entry.relative_to(source)
        target = destination / relative_path
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif entry.is_file():
            _copy_file(entry, target)
        else:
            raise ValueError(f"plugin bundle entry must be a file or directory: {entry}")


def _remove_owned_tree(path: Path, expected_parent: Path) -> None:
    resolved = path.resolve()
    if resolved.parent != expected_parent or resolved == Path(resolved.anchor):
        raise ValueError(f"refusing to replace unsafe destination: {resolved}")
    shutil.rmtree(resolved)


def _remove_staging_tree(path: Path, expected_parent: Path, destination_name: str) -> None:
    resolved = path.resolve()
    prefix = f".{destination_name}-staging-"
    if resolved.parent != expected_parent or not resolved.name.startswith(prefix):
        raise ValueError(f"refusing to remove unsafe staging directory: {resolved}")
    shutil.rmtree(resolved)
