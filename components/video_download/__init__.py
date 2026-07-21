"""视频下载组件的公共接口。"""

from .downloader import (
    DownloadConfig,
    DownloadResult,
    VideoDownloadError,
    download_video,
)

__all__ = [
    "DownloadConfig",
    "DownloadResult",
    "VideoDownloadError",
    "download_video",
]
