"""素材获取引擎的多源搜索、下载与路径约束回归测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from components.material_acquisition.base import (
    Candidate,
    SearchFilters,
    SourceError,
    StockError,
)
from components.material_acquisition.engine import StockEngine, _guess_ext


def _candidate(source: str = "ok_source", source_id: str = "1") -> Candidate:
    return Candidate(
        source=source,
        source_id=source_id,
        source_url=f"https://example.test/items/{source_id}",
        download_url=f"https://cdn.example.test/{source_id}.mp4",
        kind="video",
        duration=10.0,
    )


class OkSource:
    def __init__(
        self,
        name: str = "ok_source",
        candidates: list[Candidate] | None = None,
    ) -> None:
        self.name = name
        self.download_calls = 0
        self._candidates = candidates if candidates is not None else [_candidate(name)]

    def is_available(self) -> bool:
        return True

    async def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        del query, filters
        return self._candidates

    async def download(self, candidate: Candidate, out_path: Path) -> Path:
        del candidate
        self.download_calls += 1
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake video")
        return out_path


class FailSource:
    name = "fail_source"

    def is_available(self) -> bool:
        return True

    async def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        del query, filters
        raise SourceError("boom")

    async def download(self, candidate: Candidate, out_path: Path) -> Path:
        del candidate, out_path
        raise SourceError("download boom")


class DownloadFailSource(OkSource):
    async def download(self, candidate: Candidate, out_path: Path) -> Path:
        del candidate, out_path
        self.download_calls += 1
        raise SourceError("download boom")


class UnavailableSource:
    name = "unavailable"

    def is_available(self) -> bool:
        return False

    async def search(self, query: str, filters: SearchFilters) -> list[Candidate]:
        del query, filters
        raise AssertionError("不可用素材源不应被搜索")

    async def download(self, candidate: Candidate, out_path: Path) -> Path:
        del candidate, out_path
        raise AssertionError("不可用素材源不应被下载")


def test_search_aggregates_multiple_sources() -> None:
    engine = StockEngine(sources=[OkSource("first"), OkSource("second")])

    results = asyncio.run(engine.search("test", limit_per_source=3))

    assert [candidate.source for candidate in results] == ["first", "second"]


def test_search_limit_is_applied_per_source() -> None:
    first = OkSource("first", [_candidate("first", str(index)) for index in range(3)])
    second = OkSource("second", [_candidate("second", str(index)) for index in range(3)])
    engine = StockEngine(sources=[first, second])

    results = asyncio.run(engine.search("test", limit_per_source=2))

    assert len(results) == 4
    assert [item.source for item in results] == ["first", "first", "second", "second"]


def test_search_failover_to_next_source() -> None:
    engine = StockEngine(sources=[FailSource(), OkSource()])

    results = asyncio.run(engine.search("test", limit_per_source=3))

    assert len(results) == 1
    assert results[0].source == "ok_source"


def test_search_all_fail_raises_stock_error() -> None:
    engine = StockEngine(sources=[FailSource(), FailSource()])

    with pytest.raises(StockError, match="fail_source"):
        asyncio.run(engine.search("test", limit_per_source=3))


def test_search_skips_unavailable_sources() -> None:
    engine = StockEngine(sources=[UnavailableSource(), OkSource()])

    results = asyncio.run(engine.search("test", limit_per_source=3))

    assert len(results) == 1
    assert results[0].source == "ok_source"


def test_search_no_available_sources_raises() -> None:
    engine = StockEngine(sources=[UnavailableSource()])

    with pytest.raises(StockError, match="无可用素材源"):
        asyncio.run(engine.search("test", limit_per_source=3))


def test_search_with_specific_source_names() -> None:
    engine = StockEngine(sources=[OkSource(), FailSource()])

    results = asyncio.run(
        engine.search("test", source_names=["ok_source"], limit_per_source=3)
    )

    assert len(results) == 1
    assert results[0].source == "ok_source"


def test_search_unknown_specific_source_raises() -> None:
    engine = StockEngine(sources=[OkSource()])

    with pytest.raises(StockError, match="无可用素材源"):
        asyncio.run(
            engine.search("test", source_names=["missing"], limit_per_source=3)
        )


def test_available_sources() -> None:
    engine = StockEngine(sources=[OkSource(), UnavailableSource()])

    assert engine.available_sources() == ["ok_source"]


def test_download_writes_file(tmp_path: Path) -> None:
    engine = StockEngine(sources=[OkSource()])

    path = asyncio.run(engine.download(_candidate(), tmp_path))

    assert path.exists()
    assert path.read_bytes() == b"fake video"
    assert path.suffix == ".mp4"


def test_download_rejects_unregistered_candidate_source(tmp_path: Path) -> None:
    source = OkSource()
    engine = StockEngine(sources=[source])

    with pytest.raises(SourceError, match="未知素材源"):
        asyncio.run(engine.download(_candidate("unknown"), tmp_path))

    assert source.download_calls == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("source", "source_id"),
    [
        ("../outside", "1"),
        ("ok_source", "sub/escape"),
        ("ok_source", r"sub\escape"),
        ("ok_source", "C:escape"),
        ("ok_source", ".."),
        ("ok_source", "bad|name"),
    ],
)
def test_download_rejects_unsafe_candidate_before_writing(
    tmp_path: Path,
    source: str,
    source_id: str,
) -> None:
    candidate = _candidate(source, source_id)
    fake_source = OkSource()
    engine = StockEngine(sources=[fake_source])

    with pytest.raises(SourceError):
        asyncio.run(engine.download(candidate, tmp_path / "downloads"))

    assert fake_source.download_calls == 0
    assert not (tmp_path / "downloads").exists()


def test_search_and_download(tmp_path: Path) -> None:
    engine = StockEngine(sources=[OkSource()])

    assets = asyncio.run(
        engine.search_and_download(
            "test",
            tmp_path,
            limit=1,
            extract_thumbnail=False,
        )
    )

    assert len(assets) == 1
    assert Path(assets[0].path).exists()
    assert assets[0].candidate.source == "ok_source"


def test_search_and_download_limit_is_total_across_sources(tmp_path: Path) -> None:
    first = OkSource("first", [_candidate("first", str(index)) for index in range(3)])
    second = OkSource("second", [_candidate("second", str(index)) for index in range(3)])
    engine = StockEngine(sources=[first, second])

    assets = asyncio.run(
        engine.search_and_download(
            "test",
            tmp_path,
            limit=4,
            extract_thumbnail=False,
        )
    )

    assert len(assets) == 4
    assert [asset.candidate.source for asset in assets] == [
        "first",
        "first",
        "first",
        "second",
    ]


def test_search_and_download_skips_failed_downloads(tmp_path: Path) -> None:
    source = DownloadFailSource()
    engine = StockEngine(sources=[source])

    assets = asyncio.run(
        engine.search_and_download(
            "test",
            tmp_path,
            limit=1,
            extract_thumbnail=False,
        )
    )

    assert assets == []
    assert source.download_calls == 1


def test_search_and_download_zero_limit() -> None:
    engine = StockEngine(sources=[FailSource()])

    assert asyncio.run(engine.search_and_download("test", Path("unused"), limit=0)) == []


@pytest.mark.parametrize(
    ("download_url", "kind", "expected"),
    [
        ("https://example.test/video.mp4", "video", ".mp4"),
        ("https://example.test/photo.jpeg", "image", ".jpg"),
        ("https://example.test/noext", "video", ".mp4"),
    ],
)
def test_guess_ext(download_url: str, kind: str, expected: str) -> None:
    candidate = _candidate()
    candidate.download_url = download_url
    candidate.kind = kind

    assert _guess_ext(candidate) == expected
