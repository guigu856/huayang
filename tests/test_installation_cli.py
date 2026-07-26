from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from video_create_plugin import installation
from video_create_plugin.admin import __main__ as admin_main


def _write_plugin_source(root: Path) -> None:
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "huayang", "version": "0.1.0"}), encoding="utf-8"
    )
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"huayang": {"command": "huayang-mcp", "args": []}}}),
        encoding="utf-8",
    )
    (root / "rules").mkdir()
    (root / "rules/main-agent.md").write_text("# Main", encoding="utf-8")
    (root / "skills/router").mkdir(parents=True)
    (root / "skills/router/SKILL.md").write_text("# Router", encoding="utf-8")
    (root / "schemas").mkdir()
    (root / "schemas/task.json").write_text('{"type":"object"}', encoding="utf-8")


def test_install_codex_plugin_uses_explicit_source_and_registers(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "marketplace"
    _write_plugin_source(source)
    commands: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    result = installation.install_codex_plugin(
        source_root=source,
        destination_root=destination,
        command_runner=runner,
        executable_finder=lambda name: "/usr/bin/codex" if name == "codex" else None,
    )

    assert result == destination.resolve()
    assert commands == [
        ["/usr/bin/codex", "plugin", "marketplace", "add", str(destination.resolve())],
        ["/usr/bin/codex", "plugin", "add", "huayang@huayang-local"],
    ]


def test_install_codex_plugin_build_only_does_not_require_codex(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "marketplace"
    _write_plugin_source(source)

    result = installation.install_codex_plugin(
        build_only=True,
        source_root=source,
        destination_root=destination,
        executable_finder=lambda name: None,
    )

    assert result == destination.resolve()
    assert (result / ".agents/plugins/marketplace.json").is_file()


def test_install_codex_plugin_accepts_existing_registration(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_plugin_source(source)

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="already installed", stderr="")

    installation.install_codex_plugin(
        source_root=source,
        destination_root=tmp_path / "marketplace",
        command_runner=runner,
        executable_finder=lambda name: "codex",
    )


def test_doctor_payload_fails_when_any_check_fails() -> None:
    payload = installation.doctor_payload(
        [
            installation.DoctorCheck("ok", True, "ready"),
            installation.DoctorCheck("bad", False, "missing"),
        ]
    )

    assert payload["ok"] is False
    assert payload["checks"][1] == {"name": "bad", "ok": False, "detail": "missing"}


def test_huayang_without_arguments_keeps_admin_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def serve() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(admin_main, "_serve_admin", serve)

    admin_main.main([])

    assert called is True


def test_huayang_plugin_install_routes_to_installer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "marketplace"
    monkeypatch.setattr(admin_main, "install_codex_plugin", lambda **kwargs: destination)

    admin_main.main(["plugin", "install", "codex", "--build-only"])

    assert "已构建" in capsys.readouterr().out


def test_huayang_doctor_exits_nonzero_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        admin_main,
        "run_doctor",
        lambda **kwargs: [installation.DoctorCheck("ffmpeg", False, "missing")],
    )

    with pytest.raises(SystemExit, match="1"):
        admin_main.main(["doctor", "--json", "--skip-browser"])
