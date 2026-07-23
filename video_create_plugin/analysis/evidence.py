from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AnalysisSource(EvidenceModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)


class AnalysisEvidenceEntry(EvidenceModel):
    kind: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    algorithm_version: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def path_is_safe_relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if "\\" in value or path.is_absolute() or ".." in path.parts:
            raise ValueError("证据路径必须是安全的 POSIX 相对路径")
        return value


class AnalysisEvidenceBundle(EvidenceModel):
    hash_algorithm: str = "sha256-canonical-json-v1"
    entries: list[AnalysisEvidenceEntry] = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def entries_and_hash_are_canonical(self) -> Self:
        paths = [entry.path for entry in self.entries]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("证据清单路径必须有序且唯一")
        payload = [entry.model_dump(mode="json") for entry in self.entries]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != self.sha256:
            raise ValueError("证据清单汇总哈希不匹配")
        return self


class AnalysisEvidenceManifest(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    schema_version: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    source: AnalysisSource
    evidence_bundle: AnalysisEvidenceBundle

    @property
    def evidence_refs(self) -> set[str]:
        return {entry.path for entry in self.evidence_bundle.entries}
