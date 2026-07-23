from pathlib import Path

import pytest

from video_create_plugin import ArtifactStore, PluginError


def test_artifact_store_addresses_content_by_hash(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "objects")

    first = store.put_text("同一内容")
    second = store.put_bytes("同一内容".encode())

    assert first.sha256 == second.sha256
    assert first.uri == f"video-create-object://sha256/{first.sha256}"
    assert first.path == second.path
    assert first.path.is_file()
    assert store.read_bytes(first.uri).decode() == "同一内容"


def test_artifact_store_detects_content_tampering(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "objects")
    artifact = store.put_text("原内容")
    artifact.path.write_text("被修改", encoding="utf-8")

    with pytest.raises(PluginError) as captured:
        store.read_bytes(artifact.uri)

    assert captured.value.code == "artifact_hash_mismatch"


def test_artifact_store_rejects_unregistered_uri(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "objects")

    with pytest.raises(PluginError) as captured:
        store.read_bytes("file:///tmp/report.json")

    assert captured.value.code == "artifact_uri_invalid"
