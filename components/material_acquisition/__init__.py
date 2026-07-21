"""素材获取组件的公共接口。"""

from .base import SearchFilters
from .service import (
    CandidateSummary,
    MaterialAcquisitionConfig,
    MaterialAcquisitionError,
    MaterialAcquisitionResult,
    MaterialAcquisitionService,
    MaterialSearchResult,
)

__all__ = [
    "CandidateSummary",
    "MaterialAcquisitionConfig",
    "MaterialAcquisitionError",
    "MaterialAcquisitionResult",
    "MaterialAcquisitionService",
    "MaterialSearchResult",
    "SearchFilters",
]
