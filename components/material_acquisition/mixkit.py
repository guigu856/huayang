"""Mixkit 视频素材源适配器。"""

from __future__ import annotations

from pathlib import Path

import httpx

from components.material_acquisition.base import Candidate, SearchFilters, SourceError

_LICENSE = "unknown"
_USER_AGENT = "video-create-material-acquisition/1.0"


def _attr(element: object, name: str, default: str = "") -> str:
    value = element.get(name, default) if hasattr(element, "get") else default
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value) if value else default


class MixkitSource:
    """通过公开搜索页面提供 Mixkit 视频素材。"""

    name = "mixkit"

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def is_available(self) -> bool:
        try:
            import bs4  # noqa: F401

            return True
        except ImportError:
            return False

    async def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        if (filters.kind or "video").lower() == "image":
            return []

        from bs4 import BeautifulSoup

        slug = query.lower().replace(" ", "-")
        search_url = f"https://mixkit.co/free-stock-video/{slug}/"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    search_url, headers={"User-Agent": _USER_AGENT}
                )
        except httpx.HTTPError as error:
            raise SourceError(f"mixkit 请求失败: {error}") from error
        if response.status_code >= 400:
            raise SourceError(f"mixkit 状态码 {response.status_code}")

        soup = BeautifulSoup(response.text, "html.parser")
        out: list[Candidate] = []
        cards = soup.select(
            ".item-grid-card, .item-grid__item, .video-item, article, [class*='VideoCard']"
        )
        for card in cards[: filters.per_page]:
            link_element = card.select_one("a[href]")
            if not link_element:
                continue
            href = _attr(link_element, "href")
            if not href:
                continue
            if not href.startswith("http"):
                href = f"https://mixkit.co{href}"
            if "/free-stock-video/" not in href and "/video/" not in href:
                continue

            title_element = card.select_one("h3, h2, .title, [class*='title']")
            title = (
                title_element.get_text(strip=True)
                if title_element
                else link_element.get_text(strip=True)
            )
            image_element = card.select_one("img")
            thumbnail = (
                _attr(image_element, "src") or _attr(image_element, "data-src")
                if image_element
                else ""
            )
            video_element = card.select_one("video source[src], video[src]")
            preview_url = _attr(video_element, "src") if video_element else ""
            clip_id = href.rstrip("/").rsplit("/", 1)[-1]

            out.append(
                Candidate(
                    source=self.name,
                    source_id=f"mixkit_{clip_id}",
                    source_url=href,
                    download_url=href,
                    kind="video",
                    creator="Mixkit",
                    license=_LICENSE,
                    source_tags=f"{title} {query}",
                    thumbnail_url=thumbnail,
                    extra={"detail_url": href, "preview_url": preview_url},
                )
            )
        return out

    async def download(self, candidate: Candidate, out_path: Path) -> Path:
        from bs4 import BeautifulSoup

        if not candidate.download_url:
            raise SourceError(f"Candidate {candidate.clip_id} has no download_url")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        detail_url = str(candidate.extra.get("detail_url", candidate.download_url))
        if any(detail_url.lower().endswith(ext) for ext in (".mp4", ".mov", ".webm")):
            return await _stream_download(detail_url, out_path)

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    detail_url, headers={"User-Agent": _USER_AGENT}
                )
        except httpx.HTTPError as error:
            raise SourceError(f"mixkit 详情页请求失败: {error}") from error
        if response.status_code >= 400:
            raise SourceError(f"mixkit 详情页状态码 {response.status_code}")

        soup = BeautifulSoup(response.text, "html.parser")
        download_url = ""
        for anchor in soup.select("a[href]"):
            href = _attr(anchor, "href")
            if any(href.lower().endswith(ext) for ext in (".mp4", ".mov", ".webm")):
                download_url = href
                break
        if not download_url:
            video_element = soup.select_one("video source[src]")
            if video_element:
                download_url = _attr(video_element, "src")
        if not download_url:
            raise SourceError(f"mixkit 未找到下载链接 (detail_url={detail_url})")
        return await _stream_download(download_url, out_path)


async def _stream_download(url: str, out_path: Path) -> Path:
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            async with client.stream(
                "GET", url, headers={"User-Agent": _USER_AGENT}
            ) as response:
                response.raise_for_status()
                with open(out_path, "wb") as output:
                    async for chunk in response.aiter_bytes(chunk_size=1 << 16):
                        output.write(chunk)
    except httpx.HTTPError as error:
        raise SourceError(f"mixkit 下载失败: {error}") from error
    return out_path
