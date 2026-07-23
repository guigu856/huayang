from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def test_wheel_contains_web_assets_and_serves_homepage_after_extraction(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).parents[1]
    source_root = tmp_path / "source"
    source_root.mkdir()
    shutil.copy2(project_root / "pyproject.toml", source_root / "pyproject.toml")
    shutil.copytree(
        project_root / "components",
        source_root / "components",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(project_root / ".mcp.json", source_root / ".mcp.json")
    for directory in (".claude-plugin", ".codex-plugin", "rules", "schemas", "skills"):
        shutil.copytree(project_root / directory, source_root / directory)
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()

    built = subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--out-dir",
            str(wheel_dir),
        ],
        cwd=source_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert built.returncode == 0, built.stderr
    wheel = next(wheel_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert "components/video_editor/web/index.html" in names
        assert "components/video_editor/web/app.js" in names
        site_packages = tmp_path / "site-packages"
        archive.extractall(site_packages)

    served = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(site_packages)!r}); "
                "from fastapi.testclient import TestClient; "
                "from components.video_editor.api import create_app; "
                f"response=TestClient(create_app(Path({str(tmp_path / 'data')!r}))).get('/'); "
                "assert response.status_code == 200; "
                "assert '本地视频剪辑器' in response.text"
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert served.returncode == 0, served.stderr
