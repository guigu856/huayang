from __future__ import annotations

from typing import Any


class VideoEditorError(Exception):
    """视频编辑领域对外暴露的稳定错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

