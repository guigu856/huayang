"""素材获取基础模型回归测试。"""

from components.material_acquisition.base import Candidate, SearchFilters


def _candidate(**overrides: object) -> Candidate:
    values: dict[str, object] = {
        "source": "test",
        "source_id": "1",
        "source_url": "https://example.test/items/1",
        "download_url": "https://cdn.example.test/1.mp4",
        "kind": "video",
    }
    values.update(overrides)
    return Candidate.model_validate(values)


def test_search_filters_defaults() -> None:
    filters = SearchFilters()

    assert filters.kind == "video"
    assert filters.per_page == 10
    assert filters.page == 1
    assert filters.min_duration is None
    assert filters.max_duration is None
    assert filters.orientation is None
    assert filters.min_width is None


def test_search_filters_custom() -> None:
    filters = SearchFilters(
        kind="image",
        per_page=20,
        page=2,
        min_duration=5.0,
        max_duration=60.0,
        orientation="landscape",
        min_width=1280,
    )

    assert filters.kind == "image"
    assert filters.per_page == 20
    assert filters.page == 2
    assert filters.min_duration == 5.0
    assert filters.max_duration == 60.0
    assert filters.orientation == "landscape"
    assert filters.min_width == 1280


def test_candidate_defaults() -> None:
    candidate = _candidate()

    assert candidate.width == 0
    assert candidate.height == 0
    assert candidate.duration == 0.0
    assert candidate.creator == ""
    assert candidate.license == ""
    assert candidate.extra == {}


def test_candidate_clip_id() -> None:
    candidate = _candidate(source="pexels", source_id="123")

    assert candidate.clip_id == "pexels_123"


def test_candidate_extra() -> None:
    candidate = _candidate(extra={"fps": 30, "quality": "hd"})

    assert candidate.extra["fps"] == 30
    assert candidate.extra["quality"] == "hd"
