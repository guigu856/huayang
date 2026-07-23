from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from validation.reference_publication import publish_reference_fixtures

ROOT = Path(__file__).resolve().parents[1]
SLUG = "01_fastcut_pip"


def test_runner_supersedes_previous_and_recalls_all_creation_stages(
    tmp_path: Path,
) -> None:
    plugin_output_root = tmp_path / "plugin"
    first = publish_reference_fixtures(
        revision=1,
        project_root=ROOT,
        plugin_output_root=plugin_output_root,
        slugs=(SLUG,),
    )
    second = publish_reference_fixtures(
        revision=2,
        project_root=ROOT,
        plugin_output_root=plugin_output_root,
        slugs=(SLUG,),
    )

    assert first.manifest_path.is_file()
    assert second.manifest_path.is_file()
    assert hashlib.sha256(second.manifest_path.read_bytes()).hexdigest() == (second.manifest_sha256)
    assert (second.manifest_path.parent / "run_manifest.sha256").read_text(encoding="utf-8") == (
        f"{second.manifest_sha256}  run_manifest.json\n"
    )

    run = second.manifest["runs"][0]
    current_publication_id = run["publication"]["publication_id"]
    previous_publication_id = first.manifest["runs"][0]["publication"]["publication_id"]
    assert run["task_status"] == "completed"
    assert run["freeze"]["confirmation_assurance"] == "audit_only"
    assert run["freeze"]["host_approval_receipt"] is None
    assert run["source_media_verified"] is False
    assert run["report_artifact"]["parent_artifact_refs"] == [
        {
            "artifact_id": run["analysis_artifact"]["artifact_id"],
            "revision": run["analysis_artifact"]["revision"],
            "sha256": run["analysis_artifact"]["content_sha256"],
        }
    ]
    assert set(run["analysis_evidence_manifest"]["evidence_bundle"]["entries"][0]) == {
        "kind",
        "path",
        "sha256",
        "size_bytes",
        "algorithm_version",
    }
    assert run["state_validation"] == {
        "single_active": True,
        "active_publication_id": current_publication_id,
        "previous_active_publication_id": previous_publication_id,
        "previous_active_status_after_publish": "superseded",
        "supersedes_publication_id": previous_publication_id,
    }
    assert {item["stage"] for item in run["retrieval_validation"]} == {"stage1", "stage2", "stage3"}
    assert all(
        item["hit_publication_ids"] == [current_publication_id]
        for item in run["retrieval_validation"]
    )
    assert run["publication_artifact"]["parent_artifact_refs"] == [
        {
            "artifact_id": run["report_artifact"]["artifact_id"],
            "revision": run["report_artifact"]["revision"],
            "sha256": run["report_artifact"]["content_sha256"],
        }
    ]

    manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert manifest == second.manifest
    with sqlite3.connect(plugin_output_root / "knowledge" / "publications.sqlite3") as connection:
        rows = connection.execute(
            "SELECT publication_id, publication_revision, status "
            "FROM publications ORDER BY publication_revision"
        ).fetchall()
    assert rows == [
        (previous_publication_id, 1, "superseded"),
        (current_publication_id, 2, "active"),
    ]
