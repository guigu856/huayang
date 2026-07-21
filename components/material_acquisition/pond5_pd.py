"""Pond5 Public Domain 视频和图片素材源适配器。"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from components.material_acquisition.base import Candidate, SearchFilters, SourceError

_log = logging.getLogger(__name__)

_PD_SEARCH_URL = "https://www.pond5.com/free"
_LICENSE = "unknown"
_USER_AGENT = "video-create-material-acquisition/1.0"
_DEFAULT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_DATADOME_MARKERS = (
    "captcha-delivery.com",
    "DataDome",
    "datadome",
    "CAPTCHA",
    "captcha",
    "challenge-platform",
    "cf-challenge",
    "#cmsg",
)
_DATADOME_MAX_HTML_LEN = 8000
_DATADOME_HELP = (
    "Pond5 被 DataDome 反爬拦截（需人工 CAPTCHA）。"
    "解决方法（任选其一）：\n"
    "  1. 设 POND5_DATADOME_COOKIE=<cookie值> ——"
    " 在浏览器访问 pond5.com 完成验证后提取 datadome cookie\n"
    "  2. 设 MATERIAL_ACQUISITION_BROWSER_CDP=http://localhost:9222 ——"
    " 复用已通过验证的 Chrome 实例\n"
    "未配置时此源将自动跳过，不影响其他素材源搜索。"
)


def _attr(element: object, name: str, default: str = "") -> str:
    value = element.get(name, default) if hasattr(element, "get") else default
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value) if value else default


def _is_challenge_page(html: str) -> bool:
    """检测 Playwright 页面是否为 Cloudflare 或 DataDome 挑战页。"""
    if len(html) >= 5000:
        return False
    markers = (
        "captcha-delivery.com",
        "cf-challenge",
        "#cmsg",
        "challenge-platform",
        "DataDome",
        "CAPTCHA",
    )
    return any(marker in html for marker in markers)


def _is_datadome_challenge(html: str) -> bool:
    """检测页面中短挑战页和长页面内注入的 DataDome 脚本。"""
    if not html:
        return False
    if len(html) < _DATADOME_MAX_HTML_LEN:
        return any(marker in html for marker in _DATADOME_MARKERS)
    return any(
        marker in html for marker in ("captcha-delivery.com", "DataDome", "datadome")
    )


async def _playwright_fetch_with_cookie(
    url: str,
    *,
    timeout: float = 30_000.0,
    cdp: str | None = None,
    datadome_cookie: str | None = None,
) -> str:
    """使用 Playwright 渲染页面，并按需复用 CDP 或注入 DataDome cookie。"""
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as playwright:
            if cdp:
                browser = await playwright.chromium.connect_over_cdp(cdp)
            else:
                browser = await playwright.chromium.launch(headless=True)

            page = None
            try:
                if cdp and browser.contexts:
                    context = browser.contexts[0]
                else:
                    context = await browser.new_context(
                        user_agent=_DEFAULT_HEADERS["User-Agent"]
                    )
                if datadome_cookie:
                    await context.add_cookies(
                        [
                            {
                                "name": "datadome",
                                "value": datadome_cookie,
                                "domain": ".pond5.com",
                                "path": "/",
                                "secure": True,
                                "httpOnly": True,
                                "sameSite": "Lax",
                            }
                        ]
                    )
                page = await context.new_page()

                await page.goto(url, wait_until="networkidle", timeout=timeout)
                html: str = await page.content()
                for _ in range(15):
                    if not _is_challenge_page(html):
                        break
                    await asyncio.sleep(1)
                    html = await page.content()

                try:
                    await page.wait_for_load_state("networkidle", timeout=5_000)
                    html = await page.content()
                except Exception:
                    pass
            finally:
                if page is not None:
                    await page.close()
                if not cdp:
                    await browser.close()
    except Exception as error:
        raise SourceError(f"playwright 渲染失败: {error}") from error
    return html


class Pond5PDSource:
    """通过 Cookie 或浏览器会话访问 Pond5 公有领域素材。"""

    name = "pond5_pd"

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._datadome_cookie = os.environ.get("POND5_DATADOME_COOKIE", "").strip()
        self._cdp = (
            os.environ.get("MATERIAL_ACQUISITION_BROWSER_CDP", "").strip() or None
        )

    def is_available(self) -> bool:
        return bool(self._datadome_cookie or self._cdp)

    def _headers(self) -> dict[str, str]:
        headers = dict(_DEFAULT_HEADERS)
        if self._datadome_cookie:
            headers["Cookie"] = f"datadome={self._datadome_cookie}"
        return headers

    async def _fetch_html(
        self, url: str, params: dict[str, Any] | None = None
    ) -> str:
        html = ""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=True
            ) as client:
                response = await client.get(url, params=params, headers=self._headers())
            if response.status_code < 400:
                html = response.text
            elif response.status_code == 403:
                _log.debug("pond5 httpx 403 (DataDome 拦截)")
        except httpx.HTTPError:
            pass

        if html and not _is_datadome_challenge(html):
            return html

        _log.info("pond5 httpx 失败或被拦截，尝试 playwright 渲染")
        try:
            html = await _playwright_fetch_with_cookie(
                url,
                timeout=30_000,
                cdp=self._cdp,
                datadome_cookie=self._datadome_cookie or None,
            )
        except Exception as error:
            raise SourceError(f"pond5 playwright 渲染失败: {error}") from error

        if _is_datadome_challenge(html):
            raise SourceError(_DATADOME_HELP)
        if not html:
            raise SourceError("pond5 未获取到页面内容")
        return html

    async def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        from bs4 import BeautifulSoup

        kind = (filters.kind or "video").lower()
        params: dict[str, Any] = {
            "q": query,
            "media": "video" if kind == "video" else "image" if kind == "image" else "all",
            "page": max(1, filters.page),
            "limit": max(1, min(filters.per_page, 30)),
        }
        search_url = str(httpx.URL(_PD_SEARCH_URL, params=params))
        html = await self._fetch_html(search_url)

        soup = BeautifulSoup(html, "html.parser")
        out: list[Candidate] = []
        cards = soup.select(
            ".item, .search-result, .media-item, article, [class*='MediaCard']"
        )
        for card in cards[: filters.per_page]:
            link_element = card.select_one("a[href]")
            if not link_element:
                continue
            href = _attr(link_element, "href")
            if not href:
                continue
            if not href.startswith("http"):
                href = f"https://www.pond5.com{href}"

            title_element = card.select_one("h3, h2, .title, [class*='title']")
            title = (
                title_element.get_text(strip=True)
                if title_element
                else _attr(link_element, "title") or link_element.get_text(strip=True)
            )
            image_element = card.select_one("img")
            thumbnail = (
                _attr(image_element, "src") or _attr(image_element, "data-src")
                if image_element
                else ""
            )

            is_video = "/video/" in href or "video" in _attr(image_element, "data-type")
            candidate_kind = "video" if is_video else "image"
            if kind == "video" and not is_video:
                continue
            if kind == "image" and is_video:
                continue
            clip_id = href.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or ""

            out.append(
                Candidate(
                    source=self.name,
                    source_id=f"pond5_{clip_id}",
                    source_url=href,
                    download_url=href,
                    kind=candidate_kind,
                    creator="Pond5 Public Domain",
                    license=_LICENSE,
                    source_tags=f"{title} {query}",
                    thumbnail_url=thumbnail,
                    extra={"detail_url": href},
                )
            )
        return out

    async def download(self, candidate: Candidate, out_path: Path) -> Path:
        from bs4 import BeautifulSoup

        if not candidate.download_url:
            raise SourceError(f"Candidate {candidate.clip_id} has no download_url")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        detail_url = str(candidate.extra.get("detail_url", candidate.download_url))

        extensions = (".mp4", ".mov", ".webm", ".jpg", ".png")
        if any(detail_url.lower().endswith(extension) for extension in extensions):
            return await _stream_download(detail_url, out_path, self._headers())

        soup = BeautifulSoup(await self._fetch_html(detail_url), "html.parser")
        download_url = ""
        for anchor in soup.select("a[href]"):
            href = _attr(anchor, "href")
            if any(href.lower().endswith(extension) for extension in extensions):
                download_url = href
                break
        if not download_url:
            video_element = soup.select_one("video source[src]")
            if video_element:
                download_url = _attr(video_element, "src")
        if not download_url:
            raise SourceError(f"pond5 未找到下载链接 (detail_url={detail_url})")
        return await _stream_download(download_url, out_path, self._headers())


async def _stream_download(
    url: str, out_path: Path, headers: dict[str, str] | None = None
) -> Path:
    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            async with client.stream(
                "GET", url, headers=headers or _DEFAULT_HEADERS
            ) as response:
                response.raise_for_status()
                with open(out_path, "wb") as output:
                    async for chunk in response.aiter_bytes(chunk_size=1 << 16):
                        output.write(chunk)
    except httpx.HTTPError as error:
        raise SourceError(f"pond5 下载失败: {error}") from error
    return out_path
