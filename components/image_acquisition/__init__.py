"""图片获取组件的公共 Agent API。"""

from .service import (
    ImageAcquisitionConfig,
    ImageAcquisitionError,
    ImageAcquisitionResult,
    ImageAcquisitionService,
    ImageCandidate,
    ImageSearchResult,
    ImageSource,
)

__all__ = [
    "ImageAcquisitionConfig",
    "ImageAcquisitionError",
    "ImageAcquisitionResult",
    "ImageAcquisitionService",
    "ImageCandidate",
    "ImageSearchResult",
    "ImageSource",
]
