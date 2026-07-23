from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextResourceContents
from pydantic import AnyUrl

ROOT = Path(__file__).resolve().parents[1]


def test_project_plugin_mcp_configuration_starts_console(tmp_path: Path) -> None:
    config = _mcp_server_config(ROOT / ".mcp.json")
    environment = dict(os.environ)
    environment["HUAYANG_OUTPUT_ROOT"] = str(tmp_path / "output")

    anyio.run(
        _assert_plugin_console,
        config["command"],
        config["args"],
        ROOT,
        environment,
    )


def test_wheel_installs_complete_plugin_bundle_and_console(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    wheel_dir = tmp_path / "wheel"
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    wheel = next(wheel_dir.glob("*.whl"))

    expected_bundle_files = _source_bundle_files()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        wheel_bundle_files = {
            name.split("/share/huayang/", maxsplit=1)[1]
            for name in names
            if "/share/huayang/" in name
        }
        assert wheel_bundle_files == expected_bundle_files
        assert "video_create_plugin/admin/web/index.html" in names
        entry_points_name = next(name for name in names if name.endswith("/entry_points.txt"))
        entry_points = archive.read(entry_points_name).decode("utf-8")
        assert "huayang = video_create_plugin.admin.__main__:main" in entry_points
        assert "huayang-mcp = video_create_plugin.mcp.server:main" in entry_points

    target = tmp_path / "installed"
    subprocess.run(
        [uv, "pip", "install", "--target", str(target), "--no-deps", str(wheel)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    bundle_root = target / "share" / "huayang"
    installed_bundle_files = {
        path.relative_to(bundle_root).as_posix()
        for path in bundle_root.rglob("*")
        if path.is_file()
    }
    assert installed_bundle_files == expected_bundle_files

    launcher = target / "bin" / ("huayang-mcp.exe" if sys.platform == "win32" else "huayang-mcp")
    assert launcher.is_file()
    admin_launcher = target / "bin" / ("huayang.exe" if sys.platform == "win32" else "huayang")
    assert admin_launcher.is_file()
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(target), environment.get("PYTHONPATH", "")) if part
    )
    environment["HUAYANG_OUTPUT_ROOT"] = str(tmp_path / "output")

    anyio.run(
        _assert_plugin_console,
        str(launcher),
        [],
        bundle_root,
        environment,
    )


async def _assert_plugin_console(
    command: str,
    args: list[str],
    cwd: Path,
    environment: dict[str, str],
) -> None:
    parameters = StdioServerParameters(
        command=command,
        args=args,
        cwd=cwd,
        env=environment,
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(
            read,
            write,
            read_timeout_seconds=timedelta(seconds=30),
        ) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "huayang"
            resource = await session.read_resource(AnyUrl("huayang://rules/main-agent"))
            content = cast(TextResourceContents, resource.contents[0])
            assert "自然语言" in content.text
            tools = await session.list_tools()
            assert any(tool.name == "workflow_create_task" for tool in tools.tools)


def _mcp_server_config(path: Path) -> dict[str, Any]:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    servers = payload.get("mcpServers")
    assert isinstance(servers, dict)
    config = servers.get("huayang")
    assert isinstance(config, dict)
    command = config.get("command")
    args = config.get("args")
    assert isinstance(command, str)
    assert isinstance(args, list) and all(isinstance(item, str) for item in args)
    return {"command": command, "args": args}


def _source_bundle_files() -> set[str]:
    files = {
        ".mcp.json",
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
    }
    for directory in ("rules", "skills", "schemas"):
        files.update(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / directory).rglob("*")
            if path.is_file()
        )
    return files
