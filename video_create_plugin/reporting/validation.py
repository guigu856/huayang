from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from video_create_plugin.errors import PluginError

from .models import ReferenceReportManifest


class ReferenceReportValidationError(PluginError):
    def __init__(self, message: str, *, error_count: int) -> None:
        super().__init__(
            "reference_report_invalid",
            message,
            details={"error_count": error_count},
        )


class ReferenceReportValidator:
    """统一执行字段、时间轴、证据闭包和内容哈希校验。"""

    def validate(
        self,
        value: ReferenceReportManifest | Mapping[str, Any],
    ) -> ReferenceReportManifest:
        payload: object
        if isinstance(value, ReferenceReportManifest):
            payload = value.model_dump(mode="python")
        else:
            payload = dict(value)
        try:
            return ReferenceReportManifest.model_validate(payload)
        except ValidationError as error:
            raise ReferenceReportValidationError(
                "参考视频报告未通过结构与证据校验",
                error_count=error.error_count(),
            ) from error
