from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .codex_install import build_codex_marketplace
from .context import ContextCatalog

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]
ExecutableFinder = Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def install_codex_plugin(
    *,
    build_only: bool = False,
    source_root: Path | None = None,
    destination_root: Path | None = None,
    command_runner: CommandRunner | None = None,
    executable_finder: ExecutableFinder | None = None,
) -> Path:
    """Build the local Codex marketplace and optionally register the plugin."""

    source = ContextCatalog().root if source_root is None else source_root.expanduser().resolve()
    marketplace = build_codex_marketplace(source, destination_root)
    if build_only:
        return marketplace

    find_executable = shutil.which if executable_finder is None else executable_finder
    codex = find_executable("codex")
    if codex is None:
        raise RuntimeError(
            "未找到 codex 命令。Marketplace 已构建，可安装 Codex 后重试："
            f"huayang plugin install codex；路径：{marketplace}"
        )

    runner = _run_command if command_runner is None else command_runner
    _run_codex_idempotent(
        runner,
        [codex, "plugin", "marketplace", "add", str(marketplace)],
        action="注册 Huayang Marketplace",
    )
    _run_codex_idempotent(
        runner,
        [codex, "plugin", "add", "huayang@huayang-local"],
        action="安装 Huayang Plugin",
    )
    return marketplace


def run_doctor(*, launch_browser: bool = True) -> list[DoctorCheck]:
    """Verify the installed runtime, packaged resources, MCP construction and browser."""

    checks = [
        DoctorCheck(
            "python",
            sys.version_info >= (3, 11),
            f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    ]

    resource_root = ContextCatalog().root
    required_resources = (
        resource_root / ".mcp.json",
        resource_root / ".codex-plugin/plugin.json",
        resource_root / "rules/main-agent.md",
        resource_root / "skills",
        resource_root / "schemas",
    )
    missing = [str(path) for path in required_resources if not path.exists()]
    checks.append(
        DoctorCheck(
            "plugin_resources",
            not missing,
            str(resource_root) if not missing else "缺少：" + "、".join(missing),
        )
    )

    for executable in ("ffmpeg", "ffprobe"):
        resolved = shutil.which(executable)
        checks.append(
            DoctorCheck(
                executable,
                resolved is not None,
                resolved or f"PATH 中未找到 {executable}",
            )
        )

    try:
        from .mcp.server import build_server

        with tempfile.TemporaryDirectory(prefix="huayang-doctor-") as temporary_root:
            build_server(output_root=Path(temporary_root))
        checks.append(DoctorCheck("mcp_server", True, "MCP Server 构建成功"))
    except Exception as error:  # noqa: BLE001 - doctor must report every startup failure.
        checks.append(DoctorCheck("mcp_server", False, str(error)))

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable_path = Path(playwright.chromium.executable_path)
            if not executable_path.is_file():
                raise RuntimeError(f"Chromium 未安装：{executable_path}")
            if launch_browser:
                browser = playwright.chromium.launch(headless=True)
                browser.close()
        detail = "Chromium 已安装并成功启动" if launch_browser else str(executable_path)
        checks.append(DoctorCheck("chromium", True, detail))
    except Exception as error:  # noqa: BLE001 - report missing browser and shared libraries.
        checks.append(DoctorCheck("chromium", False, str(error)))

    return checks


def doctor_payload(checks: list[DoctorCheck]) -> dict[str, Any]:
    return {
        "ok": all(check.ok for check in checks),
        "checks": [check.to_dict() for check in checks],
    }


def print_doctor(checks: list[DoctorCheck], *, json_output: bool) -> None:
    payload = doctor_payload(checks)
    if json_output:
        print(json.dumps(payload, ensure_ascii=False))
        return
    for check in checks:
        state = "PASS" if check.ok else "FAIL"
        print(f"[{state}] {check.name}: {check.detail}")


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )


def _run_codex_idempotent(
    runner: CommandRunner,
    command: list[str],
    *,
    action: str,
) -> None:
    result = runner(command)
    if result.returncode == 0:
        return
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    normalized = output.lower()
    existing_markers = (
        "already exists",
        "already added",
        "already installed",
        "already registered",
    )
    if any(marker in normalized for marker in existing_markers):
        return
    raise RuntimeError(f"{action}失败：{output or f'退出码 {result.returncode}'}")
