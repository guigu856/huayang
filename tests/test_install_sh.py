from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_minimal_repo(root: Path) -> None:
    (root / ".codex-plugin").mkdir(parents=True)
    (root / "rules").mkdir()
    (root / "skills").mkdir()
    (root / "schemas").mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "huayang"\n', encoding="utf-8")
    (root / ".mcp.json").write_text("{}", encoding="utf-8")
    (root / ".codex-plugin/plugin.json").write_text("{}", encoding="utf-8")
    (root / "rules/main-agent.md").write_text("# Main", encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="requires bash")
def test_unix_installer_uses_uv_configured_bin_and_runs_doctor(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    source = tmp_path / "source"
    fake_bin = tmp_path / "fake-bin"
    tool_bin = tmp_path / "custom-tool-bin"
    source.mkdir()
    fake_bin.mkdir()
    tool_bin.mkdir()
    _write_minimal_repo(source)

    uv_log = tmp_path / "uv.log"
    _write_executable(
        fake_bin / "uv",
        """#!/usr/bin/env bash
set -e
printf '%s\n' "$*" >> "$UV_LOG"
if [ "$1" = "--version" ]; then echo 'uv 0.10.0'; exit 0; fi
if [ "$1 $2 $3" = "tool dir --bin" ]; then echo "$UV_TOOL_BIN_DIR"; exit 0; fi
if [ "$1 $2" = "tool update-shell" ]; then exit 0; fi
if [ "$1" = "sync" ]; then exit 0; fi
if [ "$1 $2 $3 $4" = "run playwright install chromium" ]; then exit 0; fi
if [ "$1 $2" = "tool install" ]; then
  cat > "$UV_TOOL_BIN_DIR/huayang" <<'EOF'
#!/usr/bin/env bash
echo '{"ok":true,"checks":[]}'
EOF
  cat > "$UV_TOOL_BIN_DIR/huayang-mcp" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$UV_TOOL_BIN_DIR/huayang" "$UV_TOOL_BIN_DIR/huayang-mcp"
  exit 0
fi
exit 2
""",
    )
    for command in ("ffmpeg", "ffprobe"):
        _write_executable(fake_bin / command, "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(fake_bin / "pgrep", "#!/usr/bin/env bash\nexit 1\n")

    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HOME": str(tmp_path / "home"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "UV_TOOL_BIN_DIR": str(tool_bin),
            "UV_LOG": str(uv_log),
        }
    )
    result = subprocess.run(
        ["bash", str(root / "install.sh")],
        cwd=source,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (tool_bin / "huayang").is_file()
    assert "tool dir --bin" in uv_log.read_text(encoding="utf-8")
    assert "Installation complete." in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="requires bash")
def test_unix_installer_rejects_unexpected_current_origin(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    source = tmp_path / "source"
    fake_bin = tmp_path / "fake-bin"
    source.mkdir()
    fake_bin.mkdir()
    _write_minimal_repo(source)
    (source / ".git").mkdir()
    _write_executable(
        fake_bin / "git",
        """#!/usr/bin/env bash
if [ "$3 $4" = "remote get-url" ]; then
  echo https://github.com/example/other.git
  exit 0
fi
exit 0
""",
    )
    _write_executable(fake_bin / "pgrep", "#!/usr/bin/env bash\nexit 1\n")

    environment = dict(os.environ)
    environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    result = subprocess.run(
        ["bash", str(root / "install.sh")],
        cwd=source,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode != 0
    assert "unexpected origin" in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="requires bash")
def test_unix_installer_stops_when_managed_repo_pull_fails(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    fake_bin = tmp_path / "fake-bin"
    managed = tmp_path / "data/huayang/repo"
    work = tmp_path / "work"
    fake_bin.mkdir()
    managed.mkdir(parents=True)
    work.mkdir()
    _write_minimal_repo(managed)
    (managed / ".git").mkdir()
    _write_executable(
        fake_bin / "git",
        """#!/usr/bin/env bash
if [ "$3 $4" = "remote get-url" ]; then echo https://github.com/guigu856/huayang.git; exit 0; fi
if [ "$3" = "status" ]; then exit 0; fi
if [ "$3" = "pull" ]; then exit 42; fi
exit 0
""",
    )
    _write_executable(fake_bin / "pgrep", "#!/usr/bin/env bash\nexit 1\n")

    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "HOME": str(tmp_path / "home"),
        }
    )
    result = subprocess.run(
        ["bash", str(root / "install.sh")],
        cwd=work,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 42
    assert "Updating existing checkout" in result.stdout
