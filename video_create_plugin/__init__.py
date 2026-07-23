"""剪辑创作 Plugin 的公共接口。"""

from .artifacts import ArtifactObject, ArtifactStore
from .errors import PluginError
from .models import (
    ArtifactEnvelope,
    ArtifactRef,
    FreezeRecord,
    FreezeRef,
    ReferenceContextBinding,
    StageEnvelope,
    StagePolicy,
    StageRun,
    TaskRun,
)
from .workflow import WorkflowService

__all__ = [
    "ArtifactEnvelope",
    "ArtifactObject",
    "ArtifactRef",
    "ArtifactStore",
    "FreezeRecord",
    "FreezeRef",
    "PluginError",
    "ReferenceContextBinding",
    "StageEnvelope",
    "StagePolicy",
    "StageRun",
    "TaskRun",
    "WorkflowService",
]
