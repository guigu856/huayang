"""素材搜索内核的协议、数据模型与错误类型。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class StockError(Exception):
    """所有素材源均无法完成搜索。"""


class SourceError(Exception):
    """单个素材源失败，引擎可继续尝试其他源。"""


class SearchFilters(BaseModel):
    """素材搜索过滤参数。"""

    kind: str = "video"
    per_page: int = 10
    page: int = 1
    min_duration: float | None = None
    max_duration: float | None = None
    orientation: str | None = None
    min_width: int | None = None


class Candidate(BaseModel):
    """所有素材源共用的归一化搜索结果。"""

    source: str
    source_id: str
    source_url: str
    download_url: str
    kind: str
    width: int = 0
    height: int = 0
    duration: float = 0.0
    creator: str = ""
    license: str = ""
    source_tags: str = ""
    thumbnail_url: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def clip_id(self) -> str:
        """返回跨素材源唯一的标识。"""
        return f"{self.source}_{self.source_id}"


@runtime_checkable
class StockSource(Protocol):
    """素材源协议。"""

    name: str

    def is_available(self) -> bool:
        """返回素材源是否已配置且可调用。"""
        ...

    async def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        """搜索并返回按相关性排序的候选素材。"""
        ...

    async def download(self, candidate: Candidate, out_path: Path) -> Path:
        """下载候选素材并返回本地路径。"""
        ...
