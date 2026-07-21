"""Mixkit 素材源零网络回归测试。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import components.material_acquisition.mixkit as mixkit_module
from components.material_acquisition.base import SearchFilters, SourceError
from components.material_acquisition.mixkit import MixkitSource

from ._helpers import html_handler, patch_async_client

_HTML_RESPONSE = """\
<html><body>
<div class="item-grid">
  <div class="item-grid-card">
    <a href="https://mixkit.co/free-stock-video/city-night-123/">
      <h3>City Night Traffic</h3>
      <img src="https://cdn.mixkit.co/thumb-123.jpg" />
      <video><source src="https://cdn.mixkit.co/preview-123.mp4" /></video>
    </a>
  </div>
  <div class="item-grid__item">
    <a href="https://mixkit.co/free-stock-video/ocean-waves-456/">
      <h3>Ocean Waves</h3>
      <img src="https://cdn.mixkit.co/thumb-456.jpg" />
    </a>
  </div>
</div>
</body></html>
"""


def test_is_available() -> None:
    assert MixkitSource().is_available()


def test_search_videos(monkeypatch: pytest.MonkeyPatch) -> None:
    source = MixkitSource()
    patch_async_client(monkeypatch, mixkit_module, html_handler(_HTML_RESPONSE))

    results = asyncio.run(source.search("city night", SearchFilters(kind="video")))

    assert len(results) == 2
    assert results[0].source == "mixkit"
    assert results[0].kind == "video"
    assert "city-night" in results[0].source_url
    assert results[0].thumbnail_url == "https://cdn.mixkit.co/thumb-123.jpg"
    assert results[0].extra.get("preview_url") == "https://cdn.mixkit.co/preview-123.mp4"
    assert results[0].license == "unknown"


def test_search_image_kind_returns_empty() -> None:
    source = MixkitSource()

    assert asyncio.run(source.search("test", SearchFilters(kind="image"))) == []


def test_search_error_on_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    source = MixkitSource()

    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found", request=request)

    patch_async_client(monkeypatch, mixkit_module, not_found)

    with pytest.raises(SourceError, match="mixkit"):
        asyncio.run(source.search("test", SearchFilters(kind="video")))
