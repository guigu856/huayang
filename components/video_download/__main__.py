"""视频下载组件的命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from .downloader import DownloadConfig, VideoDownloadError, download_video


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
    parser = _JsonArgumentParser(description="下载视频链接或平台分享文本")
    parser.add_argument("source", help="视频 URL 或包含 URL 的平台分享文本")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/download"),
        help="视频输出目录，默认 output/download",
    )
    parser.add_argument("--cookies", type=Path, help="Netscape 格式 Cookie 文件")
    parser.add_argument("--proxy", help="HTTP(S) 代理地址")
    parser.add_argument("--browser-cdp", help="可复用 Chrome 的 CDP 地址")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cookies_value = args.cookies or os.environ.get("VIDEO_DOWNLOADER_COOKIES_PATH")
    config = DownloadConfig(
        output_dir=args.output_dir,
        cookies_path=Path(cookies_value) if cookies_value else None,
        proxy=args.proxy or os.environ.get("VIDEO_DOWNLOADER_PROXY"),
        browser_cdp=args.browser_cdp or os.environ.get("VIDEO_DOWNLOADER_BROWSER_CDP"),
    )
    try:
        result = download_video(
            args.source,
            config,
            on_progress=lambda text: print(text, file=sys.stderr),
        )
    except VideoDownloadError as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"code": exc.code, "message": str(exc)}},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"ok": True, **result.to_dict()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
