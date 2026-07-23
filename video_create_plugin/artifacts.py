from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import PluginError


@dataclass(frozen=True, slots=True)
class ArtifactObject:
    uri: str
    sha256: str
    size: int
    path: Path


class ArtifactStore:
    """以 SHA-256 寻址并原子发布 Artifact 内容。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def put_bytes(self, content: bytes) -> ArtifactObject:
        digest = hashlib.sha256(content).hexdigest()
        path = self._path_for_hash(digest)
        if not path.exists():
            self._write_atomic(path, content)
        return ArtifactObject(
            uri=self.uri_for_hash(digest),
            sha256=digest,
            size=len(content),
            path=path,
        )

    def put_text(self, content: str) -> ArtifactObject:
        return self.put_bytes(content.encode("utf-8"))

    def put_file(self, source: Path | str) -> ArtifactObject:
        source_path = Path(source)
        if not source_path.is_file():
            raise PluginError("artifact_source_not_found", "Artifact 源文件不存在")
        try:
            content = source_path.read_bytes()
        except OSError as error:
            raise PluginError("artifact_read_failed", "Artifact 源文件读取失败") from error
        return self.put_bytes(content)

    def read_bytes(self, uri: str) -> bytes:
        digest = self.hash_from_uri(uri)
        path = self._path_for_hash(digest)
        try:
            content = path.read_bytes()
        except FileNotFoundError as error:
            raise PluginError("artifact_object_not_found", "Artifact 对象不存在") from error
        except OSError as error:
            raise PluginError("artifact_read_failed", "Artifact 对象读取失败") from error
        if hashlib.sha256(content).hexdigest() != digest:
            raise PluginError("artifact_hash_mismatch", "Artifact 对象哈希不一致")
        return content

    def verify(self, uri: str, expected_sha256: str) -> None:
        digest = self.hash_from_uri(uri)
        if digest != expected_sha256:
            raise PluginError("artifact_hash_mismatch", "Artifact URI 与记录哈希不一致")
        self.read_bytes(uri)

    @staticmethod
    def uri_for_hash(digest: str) -> str:
        return f"video-create-object://sha256/{digest}"

    @staticmethod
    def hash_from_uri(uri: str) -> str:
        prefix = "video-create-object://sha256/"
        digest = uri.removeprefix(prefix)
        if not uri.startswith(prefix) or len(digest) != 64:
            raise PluginError("artifact_uri_invalid", "Artifact URI 格式无效")
        try:
            int(digest, 16)
        except ValueError as error:
            raise PluginError("artifact_uri_invalid", "Artifact URI 格式无效") from error
        return digest

    def _path_for_hash(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    @staticmethod
    def _write_atomic(path: Path, content: bytes) -> None:
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}-",
                suffix=".tmp",
                dir=path.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.replace(temporary_path, path)
            except FileExistsError:
                temporary_path.unlink(missing_ok=True)
        except OSError as error:
            raise PluginError("artifact_write_failed", "Artifact 对象写入失败") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
