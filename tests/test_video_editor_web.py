from __future__ import annotations

from copy import deepcopy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from math import cos, pi, sin
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TIMELINE_PAGE = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <link rel="stylesheet" href="/components/video_editor/web/styles.css" />
    <style>
      body { margin: 20px; }
      .timeline-content { width: 600px; height: 120px; overflow: auto; }
    </style>
  </head>
  <body>
    <div id="track-labels"></div>
    <div id="timeline-content" class="timeline-content">
      <div id="time-ruler" class="time-ruler"></div>
      <div id="track-list" class="track-list"></div>
      <div id="playhead" class="playhead"><span></span></div>
    </div>
    <script type="module">
      import { createTimeline } from "/components/video_editor/web/timeline.js";

      window.seekEvents = [];
      window.moveEvents = [];
      window.trimEvents = [];
      let timeline;
      timeline = createTimeline(
        {
          labels: document.querySelector("#track-labels"),
          content: document.querySelector("#timeline-content"),
          ruler: document.querySelector("#time-ruler"),
          tracks: document.querySelector("#track-list"),
          playhead: document.querySelector("#playhead"),
        },
        {
          onSeek(seconds) {
            window.seekEvents.push(seconds);
            timeline.updatePlayhead(seconds);
          },
          onMove(clip, timelineStart) {
            window.moveEvents.push({ clipId: clip.id, timelineStart });
          },
          onTrim(clip, changes) {
            window.trimEvents.push({ clipId: clip.id, changes });
          },
          onSelect() {},
          onAssetDrop() {},
          onTrackRename() {},
          onTrackMove() {},
          onTrackDelete() {},
        },
      );
      timeline.render(
        {
          assets: [{ id: "asset-1", kind: "video", name: "底层视频" }],
          tracks: [
            {
              id: "track-1",
              media_domain: "visual",
              name: "视觉 1",
              clips: [
                {
                  id: "clip-1",
                  kind: "media",
                  asset_id: "asset-1",
                  timeline_start: 0,
                  source_in: 0,
                  duration: 10,
                },
              ],
            },
          ],
        },
        80,
        null,
      );
      timeline.updatePlayhead(2);
      window.timeline = timeline;
      document.querySelector("#playhead").addEventListener("pointerdown", (event) => {
        window.activePointerId = event.pointerId;
      });
      window.timelineReady = true;
    </script>
  </body>
</html>
"""

PREVIEW_PAGE = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <link rel="stylesheet" href="/components/video_editor/web/styles.css" />
    <style>
      body { margin: 20px; }
      .preview-canvas { width: 640px; height: 360px; }
    </style>
  </head>
  <body>
    <div class="preview-canvas">
      <div id="preview-layers" class="preview-layers"></div>
    </div>
    <script type="module">
      import { createPreview } from "/components/video_editor/web/preview.js";

      const clip = {
        id: "clip-overlay",
        kind: "media",
        asset_id: "asset-overlay",
        timeline_start: 0,
        source_in: 0,
        duration: 10,
        transform: {
          x: 160,
          y: 90,
          width: 640,
          height: 360,
          rotation: 0,
          opacity: 1,
        },
        volume: 1,
      };
      const project = {
        id: "project-preview",
        canvas: { width: 1280, height: 720, fps: 30 },
        assets: [
          {
            id: "asset-overlay",
            kind: "image",
            name: "叠加视频",
            metadata: { width: 1280, height: 720 },
          },
        ],
        tracks: [
          {
            id: "track-overlay",
            media_domain: "visual",
            name: "视觉 2",
            clips: [clip],
          },
        ],
      };
      window.transformEvents = [];
      window.selectEvents = [];
      let selectedClipId = "clip-overlay";
      let preview;
      preview = createPreview(document.querySelector("#preview-layers"), {
        onSelect(clipId) {
          window.selectEvents.push(clipId);
          selectedClipId = clipId;
          preview.render(project, 0, false, selectedClipId);
        },
        onTransform(changedClip, transform) {
          window.transformEvents.push({ clipId: changedClip.id, transform: { ...transform } });
          Object.assign(changedClip.transform, transform);
          preview.render(project, 0, false, selectedClipId);
        },
      });
      preview.render(project, 0, false, selectedClipId);
      window.preview = preview;
      window.previewProject = project;
      window.previewReady = true;
    </script>
  </body>
</html>
"""


class TimelineRequestHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/preview":
            body = PREVIEW_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/":
            body = TIMELINE_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def timeline_page_url() -> str:
    handler = partial(TimelineRequestHandler, directory=str(PROJECT_ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_playhead_drag_seeks_continuously_without_moving_clip(timeline_page_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 240})
        try:
            page.goto(timeline_page_url)
            page.wait_for_function("window.timelineReady === true")
            _drag_playhead_and_assert_behavior(page)
        finally:
            browser.close()


def test_playhead_cleans_up_when_pointer_capture_is_lost(timeline_page_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 240})
        try:
            page.goto(timeline_page_url)
            page.wait_for_function("window.timelineReady === true")
            playhead_box = page.locator("#playhead").bounding_box()
            clip_box = page.locator('.timeline-clip[data-clip-id="clip-1"]').bounding_box()
            assert playhead_box is not None
            assert clip_box is not None

            pointer_x = playhead_box["x"] + playhead_box["width"] / 2
            pointer_y = clip_box["y"] + clip_box["height"] / 2
            page.mouse.move(pointer_x, pointer_y)
            page.mouse.down()
            assert page.locator("#playhead").evaluate(
                "element => element.classList.contains('is-dragging')"
            )
            page.mouse.move(pointer_x + 1, pointer_y)
            page.evaluate(
                "() => document.querySelector('#playhead')"
                ".releasePointerCapture(window.activePointerId)"
            )
            page.mouse.move(pointer_x + 20, pointer_y)
            page.wait_for_timeout(50)

            assert not page.locator("#playhead").evaluate(
                "element => element.classList.contains('is-dragging')"
            )
            page.mouse.up()
        finally:
            browser.close()


def test_playhead_drag_uses_horizontal_scroll_offset(timeline_page_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1000, "height": 240})
        try:
            page.goto(timeline_page_url)
            page.wait_for_function("window.timelineReady === true")
            page.evaluate(
                """() => {
                  const content = document.querySelector("#timeline-content");
                  content.scrollLeft = 160;
                  window.seekEvents.length = 0;
                  window.timeline.updatePlayhead(4);
                }"""
            )
            playhead_box = page.locator("#playhead").bounding_box()
            clip = page.locator('.timeline-clip[data-clip-id="clip-1"]')
            clip_box = clip.bounding_box()
            assert playhead_box is not None
            assert clip_box is not None

            pointer_x = playhead_box["x"] + playhead_box["width"] / 2
            pointer_y = clip_box["y"] + clip_box["height"] / 2
            clip_left_before = clip.evaluate("element => element.style.left")
            page.mouse.move(pointer_x, pointer_y)
            page.mouse.down()
            page.mouse.move(pointer_x + 80, pointer_y)
            seek_events = page.evaluate("window.seekEvents")
            clip_left_during_drag = clip.evaluate("element => element.style.left")
            page.mouse.up()

            assert seek_events[-1] == pytest.approx(5)
            assert clip_left_during_drag == clip_left_before
        finally:
            browser.close()


def test_upper_visual_tracks_add_videos_at_full_canvas_size(timeline_page_url: str) -> None:
    full_canvas_transform = {
        "x": 0,
        "y": 0,
        "width": 1280,
        "height": 720,
        "rotation": 0,
        "opacity": 1,
    }
    project = {
        "id": "project-overlay",
        "name": "叠加视频测试",
        "revision": 1,
        "canvas": {"width": 1280, "height": 720, "fps": 30},
        "assets": [
            {
                "id": f"asset-{number}",
                "kind": "video",
                "name": f"视频{number}",
                "metadata": {
                    "width": 1920,
                    "height": 1080,
                    "duration": 10,
                },
            }
            for number in (1, 2, 3)
        ],
        "tracks": [
            {
                "id": "track-3",
                "media_domain": "visual",
                "name": "视觉 3",
                "clips": [],
            },
            {
                "id": "track-2",
                "media_domain": "visual",
                "name": "视觉 2",
                "clips": [],
            },
            {
                "id": "track-1",
                "media_domain": "visual",
                "name": "视觉 1",
                "clips": [
                    {
                        "id": "clip-1",
                        "kind": "media",
                        "asset_id": "asset-1",
                        "timeline_start": 0,
                        "source_in": 0,
                        "duration": 10,
                        "transform": deepcopy(full_canvas_transform),
                        "volume": 1,
                    }
                ],
            },
        ],
    }
    submitted_clips: list[dict[str, object]] = []

    def serve_static(route: Route) -> None:
        filename = Path(urlparse(route.request.url).path).name
        source = PROJECT_ROOT / "components" / "video_editor" / "web" / filename
        content_type = "text/css" if source.suffix == ".css" else "text/javascript"
        route.fulfill(path=source, content_type=content_type)

    def mock_api(route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if path.endswith("/content"):
            route.fulfill(status=204, body="")
            return
        if path == "/api/v1/projects" and request.method == "GET":
            route.fulfill(json={"ok": True, "data": [{"id": project["id"]}]})
            return
        if path == f"/api/v1/projects/{project['id']}" and request.method == "GET":
            route.fulfill(json={"ok": True, "data": deepcopy(project)})
            return
        if path.endswith("/commands") and request.method == "POST":
            payload = request.post_data_json
            for command in payload["commands"]:
                if command["type"] != "clip.add":
                    continue
                clip = deepcopy(command["clip"])
                clip["id"] = f"clip-{len(submitted_clips) + 2}"
                submitted_clips.append(deepcopy(clip))
                track = next(
                    item for item in project["tracks"] if item["id"] == command["track_id"]
                )
                track["clips"].append(clip)
            project["revision"] += 1
            route.fulfill(json={"ok": True, "data": deepcopy(project)})
            return
        route.fulfill(status=404, json={"ok": False, "error": {"message": "not found"}})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.route("**/static/**", serve_static)
        page.route("**/api/v1/**", mock_api)
        try:
            page.goto(f"{timeline_page_url}components/video_editor/web/index.html")
            page.locator(".asset-card").first.wait_for(state="visible")

            page.evaluate(
                """([assetName, trackId]) => {
                  const source = [...document.querySelectorAll('.asset-card')]
                    .find((element) => element.textContent.includes(assetName));
                  const target = document.querySelector(`[data-track-id="${trackId}"]`);
                  const dataTransfer = new DataTransfer();
                  source.dispatchEvent(new DragEvent('dragstart', {
                    bubbles: true,
                    dataTransfer,
                  }));
                  const bounds = target.getBoundingClientRect();
                  target.dispatchEvent(new DragEvent('drop', {
                    bubbles: true,
                    clientX: bounds.left + 2,
                    clientY: bounds.top + 20,
                    dataTransfer,
                  }));
                }""",
                ["视频2", "track-2"],
            )
            page.locator("#revision-label").wait_for(state="visible")
            page.wait_for_function(
                "document.querySelector('#revision-label').textContent === '版本 2'"
            )
            page.evaluate(
                """([assetName, trackId]) => {
                  const source = [...document.querySelectorAll('.asset-card')]
                    .find((element) => element.textContent.includes(assetName));
                  const target = document.querySelector(`[data-track-id="${trackId}"]`);
                  const dataTransfer = new DataTransfer();
                  source.dispatchEvent(new DragEvent('dragstart', {
                    bubbles: true,
                    dataTransfer,
                  }));
                  const bounds = target.getBoundingClientRect();
                  target.dispatchEvent(new DragEvent('drop', {
                    bubbles: true,
                    clientX: bounds.left + 2,
                    clientY: bounds.top + 20,
                    dataTransfer,
                  }));
                }""",
                ["视频3", "track-3"],
            )
            page.wait_for_function(
                "document.querySelector('#revision-label').textContent === '版本 3'"
            )
        finally:
            browser.close()

    observed_transforms = [clip["transform"] for clip in submitted_clips]
    assert observed_transforms == [full_canvas_transform, full_canvas_transform], submitted_clips


def test_selected_preview_layer_drag_submits_canvas_coordinates(timeline_page_url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 900, "height": 500})
        try:
            page.goto(f"{timeline_page_url}preview")
            page.wait_for_function("window.previewReady === true")
            selection = page.locator('.preview-selection[data-clip-id="clip-overlay"]')
            selection.wait_for(state="visible", timeout=1000)
            bounds = selection.bounding_box()
            assert bounds is not None

            start_x = bounds["x"] + bounds["width"] / 2
            start_y = bounds["y"] + bounds["height"] / 2
            page.mouse.move(start_x, start_y)
            page.mouse.down()
            page.mouse.move(start_x + 50, start_y + 25)
            page.mouse.up()

            transform = page.evaluate("window.transformEvents.at(-1)?.transform")
            assert transform is not None
            assert transform["x"] == pytest.approx(260)
            assert transform["y"] == pytest.approx(140)
            assert transform["width"] == pytest.approx(640)
            assert transform["height"] == pytest.approx(360)
        finally:
            browser.close()


def test_selected_preview_corner_resizes_with_locked_aspect_ratio(
    timeline_page_url: str,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 900, "height": 500})
        try:
            page.goto(f"{timeline_page_url}preview")
            page.wait_for_function("window.previewReady === true")
            handle = page.locator(
                '.preview-resize-handle[data-handle="se"]'
            )
            handle.wait_for(state="visible", timeout=1000)
            bounds = handle.bounding_box()
            assert bounds is not None

            start_x = bounds["x"] + bounds["width"] / 2
            start_y = bounds["y"] + bounds["height"] / 2
            page.mouse.move(start_x, start_y)
            page.mouse.down()
            page.mouse.move(start_x + 64, start_y + 36)
            page.mouse.up()

            transform = page.evaluate("window.transformEvents.at(-1)?.transform")
            assert transform is not None
            assert transform["x"] == pytest.approx(160)
            assert transform["y"] == pytest.approx(90)
            assert transform["width"] == pytest.approx(768)
            assert transform["height"] == pytest.approx(432)
            assert transform["width"] / transform["height"] == pytest.approx(16 / 9)
        finally:
            browser.close()


def test_rotated_preview_corner_keeps_the_opposite_corner_fixed(
    timeline_page_url: str,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 900, "height": 500})
        try:
            page.goto(f"{timeline_page_url}preview")
            page.wait_for_function("window.previewReady === true")
            page.evaluate(
                """() => {
                  const clip = window.previewProject.tracks[0].clips[0];
                  clip.transform.rotation = 30;
                  window.transformEvents.length = 0;
                  window.preview.render(window.previewProject, 0, false, clip.id);
                }"""
            )
            handle = page.locator('.preview-resize-handle[data-handle="se"]')
            bounds = handle.bounding_box()
            assert bounds is not None

            start_x = bounds["x"] + bounds["width"] / 2
            start_y = bounds["y"] + bounds["height"] / 2
            angle = 30 * pi / 180
            delta_x = 64 * cos(angle) - 36 * sin(angle)
            delta_y = 64 * sin(angle) + 36 * cos(angle)
            page.mouse.move(start_x, start_y)
            page.mouse.down()
            page.mouse.move(start_x + delta_x, start_y + delta_y)
            page.mouse.up()

            transform = page.evaluate("window.transformEvents.at(-1)?.transform")
            assert transform is not None
            original = {
                "x": 160,
                "y": 90,
                "width": 640,
                "height": 360,
                "rotation": 30,
            }
            assert transform["width"] == pytest.approx(768)
            assert transform["height"] == pytest.approx(432)
            assert _rotated_corner(transform, -1, -1) == pytest.approx(
                _rotated_corner(original, -1, -1), abs=1e-3
            )
        finally:
            browser.close()


def _rotated_corner(
    transform: dict[str, float], horizontal: int, vertical: int
) -> tuple[float, float]:
    angle = transform["rotation"] * pi / 180
    local_x = horizontal * transform["width"] / 2
    local_y = vertical * transform["height"] / 2
    center_x = transform["x"] + transform["width"] / 2
    center_y = transform["y"] + transform["height"] / 2
    return (
        center_x + local_x * cos(angle) - local_y * sin(angle),
        center_y + local_x * sin(angle) + local_y * cos(angle),
    )


def _drag_playhead_and_assert_behavior(page: Page) -> None:
    playhead_box = page.locator("#playhead").bounding_box()
    clip = page.locator('[data-clip-id="clip-1"]')
    clip_box = clip.bounding_box()
    assert playhead_box is not None
    assert clip_box is not None

    pointer_x = playhead_box["x"] + playhead_box["width"] / 2
    pointer_y = clip_box["y"] + clip_box["height"] / 2
    clip_left_before = clip.evaluate("element => element.style.left")
    assert page.evaluate(
        "([x, y]) => document.elementsFromPoint(x, y)"
        ".some(element => element.dataset.clipId === 'clip-1')",
        [pointer_x, pointer_y],
    )

    page.mouse.move(pointer_x, pointer_y)
    page.mouse.down()
    seek_counts = []
    for delta_x in (40, 80, 120):
        page.mouse.move(pointer_x + delta_x, pointer_y)
        seek_counts.append(page.evaluate("window.seekEvents.length"))

    observed_before_pointerup = {
        "seek_counts": seek_counts,
        "seek_events": page.evaluate("window.seekEvents"),
        "clip_left_before": clip_left_before,
        "clip_left_during_drag": clip.evaluate("element => element.style.left"),
        "move_events": page.evaluate("window.moveEvents"),
        "trim_events": page.evaluate("window.trimEvents"),
    }
    page.mouse.up()

    assert all(
        current > previous
        for previous, current in zip([0, *seek_counts[:-1]], seek_counts, strict=True)
    ), observed_before_pointerup
    assert observed_before_pointerup["seek_events"][-3:] == pytest.approx([2.5, 3.0, 3.5])
    assert observed_before_pointerup["clip_left_during_drag"] == clip_left_before
    assert observed_before_pointerup["move_events"] == []
    assert observed_before_pointerup["trim_events"] == []
