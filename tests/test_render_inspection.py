from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from components.render_inspection import (
    OverlayExpectation,
    RenderExpectation,
    RenderInspectionService,
)


def test_inspection_decodes_and_checks_real_render(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    output = tmp_path / "render.mp4"
    graph = (
        "testsrc2=s=320x180:r=30:d=1[first];"
        "testsrc=s=320x180:r=30:d=1[second];"
        "[first][second]concat=n=2:v=1:a=0[main];"
        "testsrc2=s=100x60:r=30:d=0.5[pip];"
        "[main][pip]overlay=x=200:y=100:enable='between(t,0.25,0.75)'[v]"
    )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-map",
            "0:a",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(output),
        ],
        check=True,
    )
    expectation = RenderExpectation(
        duration_us=2_000_000,
        width=320,
        height=180,
        fps=30,
        shot_boundaries_us=[1_000_000],
        beat_grid_us=[0, 500_000, 1_000_000, 1_500_000, 2_000_000],
        overlays=[
            OverlayExpectation(
                overlay_id="pip_1",
                start_us=250_000,
                end_us=750_000,
                x=200,
                y=100,
                width=100,
                height=60,
            )
        ],
        asset_sha256s=["a" * 64, "b" * 64, "c" * 64],
        minimum_distinct_assets=3,
        action_count=4,
        traced_action_count=4,
    )
    result = RenderInspectionService().inspect(output, expectation, tmp_path / "inspection")
    assert result.report.passed
    assert result.report_path.is_file()
    assert result.contact_sheet_path.is_file()
    assert result.report.video_metrics["decoded_frame_count"] == 60


def test_inspection_reports_planning_mismatch_without_hiding_it(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    output = tmp_path / "still.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=gray:s=160x90:r=30:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ],
        check=True,
    )
    result = RenderInspectionService().inspect(
        output,
        RenderExpectation(
            duration_us=1_000_000,
            width=160,
            height=90,
            fps=30,
            shot_boundaries_us=[500_000],
            beat_grid_us=[0, 500_000, 1_000_000],
            expected_audio=False,
            asset_sha256s=["a" * 64],
            minimum_distinct_assets=2,
            action_count=2,
            traced_action_count=1,
        ),
        tmp_path / "inspection",
    )
    assert not result.report.passed
    failed = {check.code for check in result.report.checks if not check.passed}
    assert {"freeze_run", "asset_diversity", "trace_coverage", "hard_cut_boundaries"} <= failed
