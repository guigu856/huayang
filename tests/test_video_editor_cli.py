from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from components.video_editor.__main__ import main
from components.video_editor.models import AssetCreate, EditorProject, MediaMetadata


def _stdout_json(capsys) -> dict[str, object]:
    return json.loads(capsys.readouterr().out)


def test_cli_stdout_is_utf8_when_windows_utf8_mode_is_disabled(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "0"
    environment.pop("PYTHONIOENCODING", None)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "components.video_editor",
            "--root",
            str(tmp_path / "editor"),
            "project",
            "create",
            "--name",
            "中文工程",
        ],
        capture_output=True,
        check=False,
        env=environment,
    )

    payload = json.loads(completed.stdout.decode("utf-8"))
    assert completed.returncode == 0
    assert payload["data"]["name"] == "中文工程"


def test_cli_stdin_is_utf8_when_windows_utf8_mode_is_disabled(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "0"
    environment.pop("PYTHONIOENCODING", None)
    root = tmp_path / "editor"
    base_command = [
        sys.executable,
        "-m",
        "components.video_editor",
        "--root",
        str(root),
    ]
    created = subprocess.run(
        [*base_command, "project", "create", "--name", "stdin"],
        capture_output=True,
        check=False,
        env=environment,
    )
    project = json.loads(created.stdout.decode("utf-8"))["data"]
    batch = json.dumps(
        {
            "expected_revision": 0,
            "commands": [
                    {
                        "type": "track.add",
                        "media_domain": "audio",
                        "name": "配乐轨道",
                    }
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")

    completed = subprocess.run(
        [*base_command, "command", "apply", project["id"]],
        input=batch,
        capture_output=True,
        check=False,
        env=environment,
    )

    payload = json.loads(completed.stdout.decode("utf-8"))
    assert completed.returncode == 0
    assert payload["data"]["tracks"][0]["name"] == "配乐轨道"


def test_cli_project_and_command_round_trip(tmp_path: Path, capsys) -> None:
    root = tmp_path / "editor"
    assert main(["--root", str(root), "project", "create", "--name", "命令行工程"]) == 0
    created = _stdout_json(capsys)["data"]
    assert isinstance(created, dict)

    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps(
            {
                "expected_revision": 0,
                "commands": [
                        {
                            "type": "track.add",
                            "media_domain": "visual",
                            "name": "主视频",
                        }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                "--root",
                str(root),
                "command",
                "apply",
                str(created["id"]),
                "--file",
                str(batch_path),
            ]
        )
        == 0
    )
    updated = _stdout_json(capsys)["data"]
    assert isinstance(updated, dict)
    assert updated["revision"] == 1

    assert (
        main(["--root", str(root), "project", "show", str(created["id"])])
        == 0
    )
    shown = _stdout_json(capsys)["data"]
    assert isinstance(shown, dict)
    assert shown["tracks"][0]["name"] == "主视频"

    assert main(["--root", str(root), "project", "list"]) == 0
    projects = _stdout_json(capsys)["data"]
    assert isinstance(projects, list)
    assert [project["id"] for project in projects] == [created["id"]]


def test_cli_reads_command_batch_from_stdin(tmp_path: Path, capsys, monkeypatch) -> None:
    root = tmp_path / "editor"
    main(["--root", str(root), "project", "create", "--name", "标准输入"])
    project = _stdout_json(capsys)["data"]
    assert isinstance(project, dict)
    monkeypatch.setattr(
        "sys.stdin",
        type(
            "Input",
            (),
            {
                "read": staticmethod(
                    lambda: json.dumps(
                        {
                            "expected_revision": 0,
                                "commands": [
                                    {
                                        "type": "track.add",
                                        "media_domain": "audio",
                                        "name": "配乐",
                                }
                            ],
                        }
                    )
                )
            },
        )(),
    )

    exit_code = main(
        ["--root", str(root), "command", "apply", str(project["id"])]
    )

    payload = _stdout_json(capsys)
    assert exit_code == 0
    assert payload["data"]["tracks"][0]["media_domain"] == "audio"  # type: ignore[index]


def test_cli_domain_error_is_one_json_object(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "--root",
            str(tmp_path / "editor"),
            "project",
            "show",
            "project_0000000000000000",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 1
    assert payload == {
        "ok": False,
        "error": {
            "code": "project_not_found",
            "message": "工程不存在",
            "details": {},
        },
    }


def test_cli_imports_asset_and_returns_updated_project(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = tmp_path / "editor"
    main(["--root", str(root), "project", "create", "--name", "素材工程"])
    project = _stdout_json(capsys)["data"]
    assert isinstance(project, dict)
    source = tmp_path / "素材.mp4"
    source.write_bytes(b"media")

    def fake_import(
        project_dir: Path, filename: str, stream, *, max_bytes: int
    ) -> AssetCreate:
        assert max_bytes > 0
        target = project_dir / "assets" / "stored.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(stream.read())
        return AssetCreate(
            kind="video",
            name=filename,
            path="assets/stored.mp4",
            metadata=MediaMetadata(duration=1, width=320, height=180),
        )

    monkeypatch.setattr("components.video_editor.__main__.import_media", fake_import)

    exit_code = main(
        [
            "--root",
            str(root),
            "asset",
            "import",
            str(project["id"]),
            str(source),
            "--expected-revision",
            "0",
        ]
    )

    payload = _stdout_json(capsys)
    assert exit_code == 0
    assert payload["data"]["asset"]["name"] == "素材.mp4"  # type: ignore[index]
    assert payload["data"]["project"]["revision"] == 1  # type: ignore[index]


def test_cli_import_rejects_media_above_the_fixed_size_limit(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    root = tmp_path / "editor"
    main(["--root", str(root), "project", "create", "--name", "素材上限"])
    project = _stdout_json(capsys)["data"]
    assert isinstance(project, dict)
    source = tmp_path / "too-large.mp4"
    source.write_bytes(b"12345")
    monkeypatch.setattr(
        "components.video_editor.__main__.MAX_MEDIA_BYTES", 4, raising=False
    )

    exit_code = main(
        [
            "--root",
            str(root),
            "asset",
            "import",
            str(project["id"]),
            str(source),
            "--expected-revision",
            "0",
        ]
    )

    payload = _stdout_json(capsys)
    assert exit_code == 1
    assert payload["error"]["code"] == "media_too_large"  # type: ignore[index]
    assert list((root / str(project["id"]) / "assets").glob("*")) == []


def test_cli_render_waits_for_persisted_job(tmp_path: Path, capsys, monkeypatch) -> None:
    class ImmediateRenderer:
        def render(
            self,
            project: EditorProject,
            *,
            project_dir: Path,
            output_path: Path,
            cancel_event: threading.Event,
        ) -> Path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"rendered")
            return output_path

    root = tmp_path / "editor"
    main(["--root", str(root), "project", "create", "--name", "渲染工程"])
    project = _stdout_json(capsys)["data"]
    assert isinstance(project, dict)
    monkeypatch.setattr(
        "components.video_editor.__main__.FFmpegRenderer", ImmediateRenderer
    )

    exit_code = main(
        [
            "--root",
            str(root),
            "render",
            str(project["id"]),
            "--expected-revision",
            "0",
        ]
    )

    payload = _stdout_json(capsys)
    assert exit_code == 0
    assert payload["data"]["status"] == "succeeded"  # type: ignore[index]
    assert Path(payload["data"]["output_path"]).read_bytes() == b"rendered"  # type: ignore[index]
