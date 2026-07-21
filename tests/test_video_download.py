from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from components.video_download import (
    DownloadConfig,
    VideoDownloadError,
    download_video,
)
from components.video_download import __main__ as cli_module
from components.video_download import downloader as downloader_module

SHARE_TEXT = (
    "4.12 JVL:/ 10/07 :9pm g@b.nd 【520特辑】有时间吗？一起出去走走！ "
    "# 了不起的混剪团 # he战队 # 混剪产粮团 # 抖音精选 # 520 "
    "灵感来源：@BinMax  https://v.douyin.com/bsOsbJOKVwE/ "
    "复制此链接，打开Dou音搜索，直接观看视频i"
)


def test_extract_url_from_douyin_share_text() -> None:
    assert downloader_module.extract_url(SHARE_TEXT) == "https://v.douyin.com/bsOsbJOKVwE/"


def test_missing_url_has_stable_error_code(tmp_path: Path) -> None:
    with pytest.raises(VideoDownloadError) as captured:
        download_video("没有链接", DownloadConfig(output_dir=tmp_path))
    assert captured.value.code == "url_not_found"


def test_unavailable_output_has_stable_error_code(tmp_path: Path) -> None:
    output_file = tmp_path / "not-a-directory"
    output_file.write_text("occupied", encoding="utf-8")

    with pytest.raises(VideoDownloadError) as captured:
        download_video(
            "https://example.com/video",
            DownloadConfig(output_dir=output_file),
        )

    assert captured.value.code == "output_unavailable"


def test_date_only_metadata_uses_utc_midnight() -> None:
    published_at = downloader_module._published_datetime({"upload_date": "20260520"})

    assert published_at is not None
    assert published_at.strftime("%Y%m%d_%H%M%S") == "20260520_000000"


def test_ffprobe_requires_a_video_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        downloader_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"streams": [{"codec_type": "audio"}]}',
            stderr="",
        ),
    )

    with pytest.raises(VideoDownloadError) as captured:
        downloader_module._validate_video_file(tmp_path / "not-video.mp4")

    assert captured.value.code == "invalid_video"


def test_missing_ffprobe_does_not_retry_media_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested_urls: list[str] = []

    async def fake_stream_download(
        media_url: str,
        destination: Path,
        proxy: str | None,
        report: object,
    ) -> None:
        requested_urls.append(media_url)
        destination.write_bytes(b"video" * 1000)

    def missing_ffprobe(path: Path) -> None:
        raise VideoDownloadError("dependency_missing", "缺少 ffprobe")

    monkeypatch.setattr(downloader_module, "_stream_download", fake_stream_download)
    monkeypatch.setattr(downloader_module, "_validate_video_file", missing_ffprobe)

    with pytest.raises(VideoDownloadError) as captured:
        asyncio.run(
            downloader_module._download_douyin_media(
                ["https://cdn.example/one", "https://cdn.example/two"],
                tmp_path / "video.mp4",
                None,
                lambda text: None,
            )
        )

    assert captured.value.code == "dependency_missing"
    assert requested_urls == ["https://cdn.example/one"]


def test_cli_failure_writes_one_json_object_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli_module.main(["没有链接"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == ""
    assert '"code": "url_not_found"' in captured.out


def test_cli_argument_error_is_json(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as captured_exit:
        cli_module.main([])

    captured = capsys.readouterr()
    assert captured_exit.value.code == 2
    assert captured.err == ""
    assert '"code": "invalid_arguments"' in captured.out


def test_download_result_uses_metadata_summary_and_publish_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_download(
        url: str,
        output_dir: Path,
        temporary_key: str,
        config: DownloadConfig,
        report: object,
    ) -> downloader_module._DownloadedVideo:
        temporary_path = output_dir / f"{temporary_key}.mp4"
        temporary_path.write_bytes(b"video" * 1000)
        return downloader_module._DownloadedVideo(
            temporary_path=temporary_path,
            platform="Douyin",
            canonical_url="https://www.douyin.com/video/7641818963147816667",
            video_id="7641818963147816667",
            title='【520特辑】有时间吗？一起出去走走！/ 特别篇',
            author="作者",
            duration_seconds=12.5,
            published_at=datetime(2026, 5, 20, 13, 14, 15, tzinfo=UTC),
        )

    monkeypatch.setattr(downloader_module, "_download_douyin", fake_download)
    monkeypatch.setattr(downloader_module, "_validate_video_file", lambda path: None)
    result = download_video(SHARE_TEXT, DownloadConfig(output_dir=tmp_path))

    assert result.timestamp == "20260520_131415"
    assert result.timestamp_source == "published_at"
    assert result.summary == "【520特辑】有时间吗？一起出去走走！_特别篇"
    assert result.file_path.parent == tmp_path.resolve()
    assert result.file_path.name == (
        "20260520_131415_【520特辑】有时间吗？一起出去走走！_特别篇_"
        "7641818963147816667.mp4"
    )
    assert result.file_path.stat().st_size == 5000


def test_same_metadata_does_not_overwrite_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def fake_download(
        url: str,
        output_dir: Path,
        temporary_key: str,
        config: DownloadConfig,
        report: object,
    ) -> downloader_module._DownloadedVideo:
        nonlocal calls
        calls += 1
        temporary_path = output_dir / f"{temporary_key}.mp4"
        temporary_path.write_bytes(str(calls).encode() * 2048)
        return downloader_module._DownloadedVideo(
            temporary_path=temporary_path,
            platform="Douyin",
            canonical_url="https://www.douyin.com/video/1",
            video_id="1",
            title="同名视频",
            author=None,
            duration_seconds=None,
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    monkeypatch.setattr(downloader_module, "_download_douyin", fake_download)
    monkeypatch.setattr(downloader_module, "_validate_video_file", lambda path: None)
    first = download_video(SHARE_TEXT, DownloadConfig(output_dir=tmp_path))
    second = download_video(SHARE_TEXT, DownloadConfig(output_dir=tmp_path))

    assert first.file_path.name == "20260101_000000_同名视频_1.mp4"
    assert second.file_path.name == "20260101_000000_同名视频_1_2.mp4"
    assert first.file_path.read_bytes().startswith(b"1")
    assert second.file_path.read_bytes().startswith(b"2")
