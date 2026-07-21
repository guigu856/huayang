"""素材获取组件的 JSON 命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Never

from .base import SearchFilters
from .service import (
    MaterialAcquisitionConfig,
    MaterialAcquisitionError,
    MaterialAcquisitionService,
)


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        print(
            json.dumps(
                {"ok": False, "error": {"code": "invalid_arguments", "message": message}},
                ensure_ascii=False,
            )
        )
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description="搜索并获取带来源记录的视频素材")
    commands = parser.add_subparsers(dest="command", required=True)

    sources = commands.add_parser("sources", help="列出配置层可尝试的素材源")
    sources.add_argument(
        "--output-dir", type=Path, default=Path("output/materials"), help=argparse.SUPPRESS
    )

    search = commands.add_parser("search", help="搜索视频素材候选")
    search.add_argument("query", help="建议使用 2 到 4 个内容关键词")
    search.add_argument("--limit", type=int, default=6, help="返回候选总数，默认 6")
    search.add_argument(
        "--source", action="append", dest="sources", help="指定素材源，可重复"
    )
    search.add_argument("--min-duration", type=float, help="最小时长，秒")
    search.add_argument("--max-duration", type=float, help="最大时长，秒")
    search.add_argument(
        "--orientation", choices=("landscape", "portrait", "square"), help="画面方向"
    )
    search.add_argument("--min-width", type=int, help="最小宽度，像素")
    search.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/materials"),
        help="搜索记录和素材输出根目录，默认 output/materials",
    )

    acquire = commands.add_parser("acquire", help="获取 search 返回的候选")
    acquire.add_argument("candidate_ref", help="search 返回的 candidate_ref")
    acquire.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/materials"),
        help="必须与 search 使用的输出根目录一致",
    )
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    service = MaterialAcquisitionService(
        MaterialAcquisitionConfig(output_dir=args.output_dir)
    )
    if args.command == "sources":
        return service.sources()
    if args.command == "search":
        if (
            args.min_duration is not None
            and args.max_duration is not None
            and args.min_duration > args.max_duration
        ):
            raise MaterialAcquisitionError(
                "invalid_filters", "min-duration 不能大于 max-duration"
            )
        filters = SearchFilters(
            kind="video",
            per_page=max(1, args.limit),
            min_duration=args.min_duration,
            max_duration=args.max_duration,
            orientation=args.orientation,
            min_width=args.min_width,
        )
        print("正在搜索素材候选……", file=sys.stderr)
        search_result = await service.search(
            args.query,
            limit=args.limit,
            source_names=args.sources,
            filters=filters,
        )
        return search_result.to_dict()
    if args.command == "acquire":
        print("正在下载并验证素材……", file=sys.stderr)
        acquisition_result = await service.acquire(args.candidate_ref)
        return acquisition_result.to_dict()
    raise MaterialAcquisitionError("invalid_arguments", "未知素材获取命令")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        data = asyncio.run(_run(args))
    except MaterialAcquisitionError as error:
        print(
            json.dumps(
                {"ok": False, "error": {"code": error.code, "message": str(error)}},
                ensure_ascii=False,
            )
        )
        return 1
    except Exception as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {"code": "material_operation_failed", "message": str(error)},
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"ok": True, **data}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
