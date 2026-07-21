"""把视频链接或平台分享文本下载为本地视频文件。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]
TimestampSource = Literal["published_at", "downloaded_at"]

_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff，。；、」』）】]+")
_DOUYIN_DETAIL_PATH = "/aweme/v1/web/aweme/detail/"
_DOUYIN_VIDEO_ID = re.compile(r"/video/(\d+)")
_DOUYIN_WAIT_SECONDS = 30
_DOUYIN_ATTEMPTS = 3
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".ts"}
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
_BILIBILI_HEADERS = {"User-Agent": _UA, "Referer": "https://www.bilibili.com/"}
_DOUYIN_HEADERS = {"User-Agent": _UA, "Referer": "https://www.douyin.com/"}


class VideoDownloadError(RuntimeError):
    """视频下载失败，并携带供 Agent 判断的稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DownloadConfig:
    """下载目录与平台访问配置。"""

    output_dir: Path = Path("output/download")
    cookies_path: Path | None = None
    proxy: str | None = None
    browser_cdp: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """一次成功下载的结构化结果。"""

    platform: str
    source_url: str
    canonical_url: str
    video_id: str
    summary: str
    timestamp: str
    timestamp_source: TimestampSource
    title: str
    author: str | None
    duration_seconds: float | None
    file_path: Path

    def to_dict(self) -> dict[str, str | float | None]:
        return {
            "platform": self.platform,
            "source_url": self.source_url,
            "canonical_url": self.canonical_url,
            "video_id": self.video_id,
            "summary": self.summary,
            "timestamp": self.timestamp,
            "timestamp_source": self.timestamp_source,
            "title": self.title,
            "author": self.author,
            "duration_seconds": self.duration_seconds,
            "file_path": str(self.file_path),
        }


@dataclass(frozen=True, slots=True)
class _DownloadedVideo:
    temporary_path: Path
    platform: str
    canonical_url: str
    video_id: str
    title: str
    author: str | None
    duration_seconds: float | None
    published_at: datetime | None


def extract_url(text: str) -> str | None:
    """从纯链接或平台分享文本中抽取首个 HTTP(S) URL。"""
    match = _URL_RE.search(text)
    return match.group(0).rstrip(").,;'\"") if match else None


def download_video(
    source: str,
    config: DownloadConfig | None = None,
    *,
    on_progress: ProgressCallback | None = None,
) -> DownloadResult:
    """下载一个视频，并用平台摘要与时间戳生成最终文件名。"""
    active_config = config or DownloadConfig()
    url = extract_url(source)
    if url is None:
        raise VideoDownloadError("url_not_found", "输入中没有可下载的 HTTP(S) 视频链接")

    output_dir = active_config.output_dir.resolve()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VideoDownloadError(
            "output_unavailable", f"无法创建视频输出目录：{output_dir}：{exc}"
        ) from exc
    report = on_progress or (lambda _: None)
    report(f"识别到视频链接：{url}")
    source_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    temporary_key = f".video-download-{source_hash}-{uuid.uuid4().hex[:8]}"

    try:
        if _is_douyin_url(url):
            report("抖音链接：启动浏览器获取视频详情")
            downloaded = asyncio.run(
                _download_douyin(url, output_dir, temporary_key, active_config, report)
            )
        else:
            report("使用 yt-dlp 获取视频信息并下载")
            downloaded = _download_ytdlp(
                url, output_dir, temporary_key, active_config, report
            )
        return _finalize_download(url, downloaded, report)
    except VideoDownloadError:
        _remove_temporary_files(output_dir, temporary_key)
        raise
    except Exception as exc:
        _remove_temporary_files(output_dir, temporary_key)
        raise VideoDownloadError("download_failed", f"视频下载失败：{exc}") from exc


def _finalize_download(
    source_url: str,
    downloaded: _DownloadedVideo,
    report: ProgressCallback,
) -> DownloadResult:
    downloaded_at = datetime.now(UTC)
    naming_time = downloaded.published_at or downloaded_at
    timestamp_source: TimestampSource = (
        "published_at" if downloaded.published_at else "downloaded_at"
    )
    timestamp = naming_time.strftime("%Y%m%d_%H%M%S")
    summary = _safe_summary(downloaded.title, downloaded.platform, downloaded.video_id)
    suffix = downloaded.temporary_path.suffix.lower()
    if suffix not in _VIDEO_SUFFIXES:
        suffix = ".mp4"
    filename = f"{timestamp}_{summary}_{_safe_identifier(downloaded.video_id)}{suffix}"
    destination = _reserve_destination(downloaded.temporary_path.parent / filename)
    try:
        downloaded.temporary_path.replace(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    destination = destination.resolve()
    report(f"视频已保存：{destination.name}（{destination.stat().st_size // 1024} KB）")
    return DownloadResult(
        platform=downloaded.platform,
        source_url=source_url,
        canonical_url=downloaded.canonical_url,
        video_id=downloaded.video_id,
        summary=summary,
        timestamp=timestamp,
        timestamp_source=timestamp_source,
        title=downloaded.title,
        author=downloaded.author,
        duration_seconds=downloaded.duration_seconds,
        file_path=destination,
    )


def _download_ytdlp(
    url: str,
    output_dir: Path,
    temporary_key: str,
    config: DownloadConfig,
    report: ProgressCallback,
) -> _DownloadedVideo:
    try:
        import yt_dlp
    except ImportError as exc:
        raise VideoDownloadError(
            "dependency_missing", "缺少 yt-dlp，请先安装项目依赖"
        ) from exc

    last_reported = -10.0

    def progress_hook(data: dict[str, Any]) -> None:
        nonlocal last_reported
        if data.get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        done = data.get("downloaded_bytes")
        if not total or done is None:
            return
        percent = done / total * 100
        if percent - last_reported >= 10:
            last_reported = percent
            report(f"下载中：{percent:.0f}%")

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "outtmpl": str(output_dir / f"{temporary_key}.%(ext)s"),
        "format": "mp4/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "progress_hooks": [progress_hook],
    }
    if _is_bilibili_url(url):
        options["http_headers"] = dict(_BILIBILI_HEADERS)
    if config.cookies_path is not None:
        options["cookiefile"] = str(config.cookies_path)
    if config.proxy:
        options["proxy"] = config.proxy

    try:
        with yt_dlp.YoutubeDL(cast("Any", options)) as ydl:
            info = ydl.extract_info(url, download=True)
            if not isinstance(info, dict):
                raise VideoDownloadError("metadata_missing", "yt-dlp 未返回视频信息")
    except VideoDownloadError:
        raise
    except Exception as exc:
        message = str(exc)
        code = "cookie_required" if "cookies" in message.lower() else "download_failed"
        raise VideoDownloadError(code, f"yt-dlp 下载失败：{message}") from exc

    temporary_path = _find_downloaded_file(output_dir, temporary_key)
    _validate_video_file(temporary_path)
    video_id = str(info.get("id") or hashlib.sha1(url.encode("utf-8")).hexdigest()[:12])
    platform = str(info.get("extractor_key") or info.get("extractor") or _host(url))
    title = str(info.get("title") or info.get("description") or video_id).strip()
    author_value = info.get("uploader") or info.get("channel") or info.get("creator")
    duration = info.get("duration")
    return _DownloadedVideo(
        temporary_path=temporary_path,
        platform=platform,
        canonical_url=str(info.get("webpage_url") or info.get("original_url") or url),
        video_id=video_id,
        title=title,
        author=str(author_value).strip() if author_value else None,
        duration_seconds=float(duration) if isinstance(duration, int | float) else None,
        published_at=_published_datetime(cast("dict[str, Any]", info)),
    )


async def _download_douyin(
    url: str,
    output_dir: Path,
    temporary_key: str,
    config: DownloadConfig,
    report: ProgressCallback,
) -> _DownloadedVideo:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise VideoDownloadError(
            "dependency_missing",
            "抖音下载需要 Playwright，请先安装项目依赖和 Chromium",
        ) from exc

    detail: dict[str, Any] = {}
    try:
        async with async_playwright() as playwright:
            if config.browser_cdp:
                browser = await playwright.chromium.connect_over_cdp(config.browser_cdp)
            else:
                browser = await playwright.chromium.launch(
                    headless=True,
                    proxy={"server": config.proxy} if config.proxy else None,
                )
            page = None
            context = None
            try:
                if config.browser_cdp and browser.contexts:
                    page = await browser.contexts[0].new_page()
                else:
                    context = await browser.new_context(
                        user_agent=_UA,
                        viewport={"width": 1280, "height": 720},
                    )
                    page = await context.new_page()

                async def capture_detail(response: Any) -> None:
                    if _DOUYIN_DETAIL_PATH not in response.url or detail:
                        return
                    try:
                        payload = await response.json()
                    except Exception:
                        return
                    aweme = payload.get("aweme_detail")
                    if isinstance(aweme, dict):
                        detail.update(aweme)

                page.on("response", capture_detail)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                except Exception as exc:
                    if "Timeout" not in str(exc):
                        raise
                    logger.warning("抖音页面打开超时，继续等待详情响应：%s", exc)
                report("已打开抖音视频页，等待视频详情")

                for attempt in range(_DOUYIN_ATTEMPTS):
                    for _ in range(_DOUYIN_WAIT_SECONDS):
                        if detail:
                            break
                        await asyncio.sleep(1)
                    if detail:
                        match = _DOUYIN_VIDEO_ID.search(page.url)
                        if match and str(detail.get("aweme_id")) == match.group(1):
                            break
                        detail.clear()
                    if attempt < _DOUYIN_ATTEMPTS - 1:
                        report(
                            "未等到目标视频详情，刷新重试"
                            f"（{attempt + 1}/{_DOUYIN_ATTEMPTS - 1}）"
                        )
                        try:
                            await page.reload(wait_until="domcontentloaded", timeout=30_000)
                        except Exception as exc:
                            if "Timeout" not in str(exc):
                                raise
                            logger.warning("抖音页面刷新超时，继续等待详情响应：%s", exc)
            finally:
                if page is not None:
                    await page.close()
                if config.browser_cdp and context is not None:
                    await context.close()
                if not config.browser_cdp:
                    await browser.close()
    except VideoDownloadError:
        raise
    except Exception as exc:
        raise VideoDownloadError("douyin_browser_failed", f"抖音浏览器解析失败：{exc}") from exc

    video_id = str(detail.get("aweme_id") or "")
    video = detail.get("video") or {}
    media_urls: list[str] = []
    for key in ("play_addr", "play_addr_h265"):
        for media_url in ((video.get(key) or {}).get("url_list")) or []:
            if media_url and media_url not in media_urls:
                media_urls.append(str(media_url))
    if not video_id or not media_urls:
        raise VideoDownloadError(
            "douyin_access_blocked",
            "未取得目标抖音视频详情；请通过浏览器 CDP 复用可访问抖音的 Chrome",
        )

    report("已取得抖音视频直链，开始下载")
    temporary_path = output_dir / f"{temporary_key}.mp4"
    await _download_douyin_media(media_urls, temporary_path, config.proxy, report)
    description = str(detail.get("desc") or video_id).strip()
    author = detail.get("author") or {}
    create_time = detail.get("create_time")
    duration_ms = video.get("duration")
    return _DownloadedVideo(
        temporary_path=temporary_path,
        platform="Douyin",
        canonical_url=f"https://www.douyin.com/video/{video_id}",
        video_id=video_id,
        title=description,
        author=str(author.get("nickname")).strip() if author.get("nickname") else None,
        duration_seconds=(
            round(float(duration_ms) / 1000, 3)
            if isinstance(duration_ms, int | float)
            else None
        ),
        published_at=(
            datetime.fromtimestamp(create_time, tz=UTC)
            if isinstance(create_time, int | float)
            else None
        ),
    )


async def _download_douyin_media(
    media_urls: list[str],
    temporary_path: Path,
    proxy: str | None,
    report: ProgressCallback,
) -> None:
    last_error: VideoDownloadError | None = None
    for index, media_url in enumerate(media_urls, start=1):
        temporary_path.unlink(missing_ok=True)
        try:
            await _stream_download(media_url, temporary_path, proxy, report)
            await asyncio.to_thread(_validate_video_file, temporary_path)
            return
        except VideoDownloadError as exc:
            if exc.code == "dependency_missing":
                raise
            last_error = exc
            temporary_path.unlink(missing_ok=True)
            if index < len(media_urls):
                report(f"当前媒体地址不可用，尝试备用地址（{index + 1}/{len(media_urls)}）")
    message = str(last_error) if last_error else "平台未返回可用媒体地址"
    raise VideoDownloadError(
        "douyin_download_failed", f"所有抖音媒体地址均下载失败：{message}"
    )


async def _stream_download(
    media_url: str,
    destination: Path,
    proxy: str | None,
    report: ProgressCallback,
) -> None:
    last_reported = -10.0
    downloaded = 0
    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            headers=_DOUYIN_HEADERS,
            proxy=proxy,
        ) as client:
            async with client.stream("GET", media_url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type and not (
                    content_type.startswith("video/")
                    or content_type == "application/octet-stream"
                ):
                    raise VideoDownloadError(
                        "invalid_video", f"媒体地址返回了非视频内容：{content_type}"
                    )
                total_text = response.headers.get("content-length")
                total = int(total_text) if total_text and total_text.isdigit() else None
                with destination.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        file.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            percent = downloaded / total * 100
                            if percent - last_reported >= 10:
                                last_reported = percent
                                report(f"下载中：{percent:.0f}%")
    except VideoDownloadError:
        raise
    except Exception as exc:
        raise VideoDownloadError("douyin_download_failed", f"抖音视频流下载失败：{exc}") from exc
    if not destination.is_file() or destination.stat().st_size <= 1000:
        raise VideoDownloadError("invalid_video", "抖音视频下载结果不存在或文件过小")


def _find_downloaded_file(output_dir: Path, temporary_key: str) -> Path:
    candidates = [
        path
        for path in output_dir.glob(f"{temporary_key}.*")
        if path.is_file() and path.suffix.lower() in _VIDEO_SUFFIXES and path.stat().st_size > 1000
    ]
    if not candidates:
        raise VideoDownloadError("invalid_video", "下载完成后没有找到有效视频文件")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _validate_video_file(path: Path) -> None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise VideoDownloadError(
            "dependency_missing", "缺少 ffprobe，请安装 FFmpeg 并加入 PATH"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VideoDownloadError("invalid_video", "ffprobe 验证视频超时") from exc
    try:
        payload = json.loads(result.stdout) if result.stdout else {}
    except json.JSONDecodeError as exc:
        raise VideoDownloadError("invalid_video", "ffprobe 返回了无效结果") from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    has_video = isinstance(streams, list) and any(
        isinstance(stream, dict) and stream.get("codec_type") == "video" for stream in streams
    )
    if result.returncode != 0 or not has_video:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "没有视频流"
        raise VideoDownloadError("invalid_video", f"下载文件不是有效视频：{detail}")


def _published_datetime(info: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "release_timestamp"):
        value = info.get(key)
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value, tz=UTC)
    upload_date = info.get("upload_date") or info.get("release_date")
    if isinstance(upload_date, str) and re.fullmatch(r"\d{8}", upload_date):
        return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=UTC)
    return None


def _safe_summary(title: str, platform: str, video_id: str) -> str:
    text = re.sub(r"https?://\S+", "", title)
    text = _INVALID_FILENAME_CHARS.sub("_", text)
    text = re.sub(r"[\s_]+", "_", text).strip(" ._")
    return (text or platform or video_id or "video")[:64].rstrip(" ._")


def _safe_identifier(video_id: str) -> str:
    value = _INVALID_FILENAME_CHARS.sub("_", video_id).strip(" ._")
    return value[:48] or "unknown"


def _reserve_destination(path: Path) -> Path:
    candidates = [
        path,
        *(path.with_name(f"{path.stem}_{index}{path.suffix}") for index in range(2, 10_000)),
    ]
    for candidate in candidates:
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(descriptor)
        return candidate
    raise VideoDownloadError("name_conflict", f"无法为下载文件分配名称：{path.name}")


def _remove_temporary_files(output_dir: Path, temporary_key: str) -> None:
    for path in output_dir.glob(f"{temporary_key}.*"):
        if path.is_file():
            path.unlink(missing_ok=True)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "video").lower()


def _is_douyin_url(url: str) -> bool:
    host = _host(url)
    return host == "douyin.com" or host.endswith(".douyin.com") or host.endswith(".iesdouyin.com")


def _is_bilibili_url(url: str) -> bool:
    host = _host(url)
    return host == "b23.tv" or host == "bilibili.com" or host.endswith(".bilibili.com")
