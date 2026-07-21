"""Pond5 公有领域素材源的 HTTP、DataDome 与浏览器回归测试。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import components.material_acquisition.pond5_pd as pond5_module
from components.material_acquisition.base import SearchFilters, SourceError
from components.material_acquisition.pond5_pd import Pond5PDSource

from ._helpers import html_handler, patch_async_client

_HTML_RESPONSE = """\
<html><body>
<div class="search-results">
  <div class="item">
    <a href="https://www.pond5.com/video/clip-001">
      <h3>Historical Newsreel</h3>
      <img src="https://cdn.pond5.com/thumb-001.jpg" data-type="video" />
    </a>
  </div>
  <div class="item">
    <a href="https://www.pond5.com/image/photo-002">
      <h3>Vintage Photograph</h3>
      <img src="https://cdn.pond5.com/thumb-002.jpg" />
    </a>
  </div>
</div>
</body></html>
"""

_DATADOME_HTML = """\
<html><head><title>pond5.com</title></head>
<body>
<script src="https://ct.captcha-delivery.com/c.js"></script>
<div id="cmsg">Please wait...</div>
<iframe src="https://geo.captcha-delivery.com/captcha/"></iframe>
</body></html>
"""


def test_is_unavailable_without_browser_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("POND5_DATADOME_COOKIE", raising=False)
    monkeypatch.delenv("MATERIAL_ACQUISITION_BROWSER_CDP", raising=False)

    assert not Pond5PDSource().is_available()


def test_is_available_with_datadome_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POND5_DATADOME_COOKIE", "cookie")

    assert Pond5PDSource().is_available()


@pytest.mark.parametrize(
    ("kind", "expected_kinds"),
    [
        ("video", ["video"]),
        ("image", ["image"]),
        ("any", ["video", "image"]),
    ],
)
def test_search_media_kinds(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expected_kinds: list[str],
) -> None:
    source = Pond5PDSource()
    patch_async_client(monkeypatch, pond5_module, html_handler(_HTML_RESPONSE))

    results = asyncio.run(source.search("newsreel", SearchFilters(kind=kind)))

    assert [candidate.kind for candidate in results] == expected_kinds
    assert all(candidate.source == "pond5_pd" for candidate in results)
    assert all(candidate.license == "unknown" for candidate in results)


def test_search_error_on_http_and_playwright_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Pond5PDSource()
    patch_async_client(monkeypatch, pond5_module, html_handler("Server Error", status=500))

    async def fail_playwright(url: str, **kwargs: object) -> str:
        del url, kwargs
        raise SourceError("playwright 渲染失败: test")

    monkeypatch.setattr(pond5_module, "_playwright_fetch_with_cookie", fail_playwright)

    with pytest.raises(SourceError, match="pond5"):
        asyncio.run(source.search("test", SearchFilters(kind="video")))


def test_datadome_detection_httpx_and_playwright(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Pond5PDSource()
    patch_async_client(
        monkeypatch,
        pond5_module,
        html_handler(_DATADOME_HTML, status=403),
    )

    async def return_challenge(url: str, **kwargs: object) -> str:
        del url, kwargs
        return _DATADOME_HTML

    monkeypatch.setattr(pond5_module, "_playwright_fetch_with_cookie", return_challenge)

    with pytest.raises(SourceError, match="DataDome"):
        asyncio.run(source.search("newsreel", SearchFilters(kind="video")))


def test_datadome_cookie_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POND5_DATADOME_COOKIE", "fake-cookie-value-123")
    source = Pond5PDSource()
    captured_requests: list[httpx.Request] = []
    patch_async_client(
        monkeypatch,
        pond5_module,
        html_handler(_HTML_RESPONSE),
        captured_requests=captured_requests,
    )

    results = asyncio.run(source.search("newsreel", SearchFilters(kind="video")))

    assert "datadome=fake-cookie-value-123" in source._headers().get("Cookie", "")
    assert "datadome=fake-cookie-value-123" in captured_requests[0].headers.get(
        "Cookie", ""
    )
    assert len(results) == 1


def test_datadome_playwright_passes_component_cdp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MATERIAL_ACQUISITION_BROWSER_CDP",
        "http://localhost:9222",
    )
    source = Pond5PDSource()
    patch_async_client(monkeypatch, pond5_module, html_handler("Forbidden", status=403))
    captured_cdp: str | None = None

    async def capture_playwright(
        url: str,
        *,
        timeout: float = 30_000.0,
        cdp: str | None = None,
        datadome_cookie: str | None = None,
    ) -> str:
        del url, timeout, datadome_cookie
        nonlocal captured_cdp
        captured_cdp = cdp
        return _HTML_RESPONSE

    monkeypatch.setattr(pond5_module, "_playwright_fetch_with_cookie", capture_playwright)

    results = asyncio.run(source.search("newsreel", SearchFilters(kind="video")))

    assert captured_cdp == "http://localhost:9222"
    assert len(results) == 1


def test_datadome_playwright_succeeds_after_httpx_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Pond5PDSource()
    patch_async_client(monkeypatch, pond5_module, html_handler("Forbidden", status=403))

    async def playwright_ok(url: str, **kwargs: object) -> str:
        del url, kwargs
        return _HTML_RESPONSE

    monkeypatch.setattr(pond5_module, "_playwright_fetch_with_cookie", playwright_ok)

    results = asyncio.run(source.search("newsreel", SearchFilters(kind="video")))

    assert len(results) == 1
    assert results[0].kind == "video"
