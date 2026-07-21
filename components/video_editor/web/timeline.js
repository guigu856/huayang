import { formatTimecode, projectDuration } from "./state.js";

const MIN_CLIP_DURATION = 0.08;

export function createTimeline(elements, callbacks) {
  const { labels, content, ruler, tracks, playhead } = elements;
  let activeProject = null;
  let pixelsPerSecond = 80;

  function seekAtClientX(clientX) {
    if (!activeProject) return;
    const bounds = content.getBoundingClientRect();
    const timelineX = clientX - bounds.left + content.scrollLeft;
    callbacks.onSeek(Math.max(0, timelineX / pixelsPerSecond));
  }

  function beginPlayheadDrag(event) {
    if (event.button !== 0 || !activeProject) return;
    event.preventDefault();
    event.stopPropagation();
    callbacks.onScrubStart?.();
    playhead.classList.add("is-dragging");
    playhead.setPointerCapture(event.pointerId);
    seekAtClientX(event.clientX);

    const pointerId = event.pointerId;
    let cleanedUp = false;
    const move = (pointerEvent) => {
      if (pointerEvent.pointerId === pointerId) seekAtClientX(pointerEvent.clientX);
    };
    const cleanup = () => {
      if (cleanedUp) return;
      cleanedUp = true;
      playhead.removeEventListener("pointermove", move);
      playhead.removeEventListener("pointerup", end);
      playhead.removeEventListener("pointercancel", cancel);
      playhead.removeEventListener("lostpointercapture", lostCapture);
      playhead.classList.remove("is-dragging");
    };
    const end = (pointerEvent) => {
      if (pointerEvent.pointerId !== pointerId) return;
      seekAtClientX(pointerEvent.clientX);
      cleanup();
    };
    const cancel = (pointerEvent) => {
      if (pointerEvent.pointerId === pointerId) cleanup();
    };
    const lostCapture = (pointerEvent) => {
      if (pointerEvent.pointerId === pointerId) cleanup();
    };

    playhead.addEventListener("pointermove", move);
    playhead.addEventListener("pointerup", end);
    playhead.addEventListener("pointercancel", cancel);
    playhead.addEventListener("lostpointercapture", lostCapture);
  }

  playhead.addEventListener("pointerdown", beginPlayheadDrag);

  function trackColor(mediaDomain) {
    return mediaDomain === "audio" ? "var(--audio)" : "var(--video)";
  }

  function renderRuler(width, seconds) {
    ruler.replaceChildren();
    ruler.style.width = `${width}px`;
    const interval = pixelsPerSecond >= 120 ? 1 : pixelsPerSecond >= 60 ? 2 : 5;
    for (let second = 0; second <= seconds; second += interval) {
      const mark = document.createElement("div");
      mark.className = "ruler-mark";
      mark.style.left = `${second * pixelsPerSecond}px`;
      const label = document.createElement("span");
      label.textContent = formatTimecode(second).slice(0, 5);
      mark.append(label);
      ruler.append(mark);
    }
  }

  function makeLabel(track, index, trackCount) {
    const label = document.createElement("div");
    label.className = "track-label";
    const dot = document.createElement("span");
    dot.className = "track-label-dot";
    dot.style.background = trackColor(track.media_domain);
    const name = document.createElement("input");
    name.className = "track-name";
    name.value = track.name;
    name.title = "重命名轨道";
    name.setAttribute("aria-label", `${track.name}名称`);
    name.addEventListener("click", (event) => event.stopPropagation());
    name.addEventListener("change", () => {
      const resolved = name.value.trim();
      if (!resolved) {
        name.value = track.name;
        return;
      }
      callbacks.onTrackRename(track, resolved);
    });
    const controls = document.createElement("div");
    controls.className = "track-controls";
    const actions = [
      ["↑", "轨道上移", index > 0, () => callbacks.onTrackMove(track, index - 1)],
      ["↓", "轨道下移", index < trackCount - 1, () => callbacks.onTrackMove(track, index + 1)],
      ["×", "删除轨道", true, () => callbacks.onTrackDelete(track)],
    ];
    for (const [text, title, enabled, action] of actions) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "track-control";
      button.textContent = text;
      button.title = title;
      button.setAttribute("aria-label", `${track.name}${title}`);
      button.disabled = !enabled;
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        action();
      });
      controls.append(button);
    }
    label.append(dot, name, controls);
    return label;
  }

  function beginDrag(event, clipElement, clip, mode) {
    if (event.button !== 0) return;
    event.preventDefault();
    const originX = event.clientX;
    const original = {
      start: clip.timeline_start,
      duration: clip.duration,
      sourceIn: clip.source_in,
    };
    clipElement.setPointerCapture(event.pointerId);

    const move = (pointerEvent) => {
      const delta = (pointerEvent.clientX - originX) / pixelsPerSecond;
      if (mode === "move") {
        clipElement.style.left = `${Math.max(0, original.start + delta) * pixelsPerSecond}px`;
      } else if (mode === "right") {
        const duration = Math.max(MIN_CLIP_DURATION, original.duration + delta);
        clipElement.style.width = `${duration * pixelsPerSecond}px`;
      } else {
        const start = Math.max(0, Math.min(original.start + delta, original.start + original.duration - MIN_CLIP_DURATION));
        const consumed = start - original.start;
        clipElement.style.left = `${start * pixelsPerSecond}px`;
        clipElement.style.width = `${(original.duration - consumed) * pixelsPerSecond}px`;
      }
    };

    const end = (pointerEvent) => {
      const deltaPixels = pointerEvent.clientX - originX;
      const delta = deltaPixels / pixelsPerSecond;
      clipElement.removeEventListener("pointermove", move);
      clipElement.removeEventListener("pointerup", end);
      clipElement.removeEventListener("pointercancel", end);
      if (Math.abs(deltaPixels) < 2) return;
      if (mode === "move") {
        callbacks.onMove(clip, Math.max(0, original.start + delta));
      } else if (mode === "right") {
        callbacks.onTrim(clip, {
          duration: Math.max(MIN_CLIP_DURATION, original.duration + delta),
          source_in: original.sourceIn,
          timeline_start: original.start,
        });
      } else {
        const start = Math.max(0, Math.min(original.start + delta, original.start + original.duration - MIN_CLIP_DURATION));
        const consumed = start - original.start;
        callbacks.onTrim(clip, {
          timeline_start: start,
          source_in: original.sourceIn + consumed,
          duration: original.duration - consumed,
        });
      }
    };

    clipElement.addEventListener("pointermove", move);
    clipElement.addEventListener("pointerup", end);
    clipElement.addEventListener("pointercancel", end);
  }

  function makeClip(project, track, clip, isSelected) {
    const element = document.createElement("div");
    element.className = `timeline-clip${isSelected ? " is-selected" : ""}`;
    element.dataset.kind = clip.kind;
    element.dataset.domain = track.media_domain;
    element.dataset.clipId = clip.id;
    element.style.left = `${clip.timeline_start * pixelsPerSecond}px`;
    element.style.width = `${Math.max(clip.duration * pixelsPerSecond, 18)}px`;
    const asset = project.assets.find((item) => item.id === clip.asset_id);
    const clipName = clip.text ?? asset?.name ?? "片段";
    element.title = `${clipName} · ${clip.duration.toFixed(2)} 秒`;

    const left = document.createElement("span");
    left.className = "clip-handle";
    left.addEventListener("pointerdown", (event) => beginDrag(event, element, clip, "left"));
    const label = document.createElement("span");
    label.className = "clip-label";
    label.textContent = clipName;
    label.addEventListener("pointerdown", (event) => beginDrag(event, element, clip, "move"));
    const right = document.createElement("span");
    right.className = "clip-handle";
    right.addEventListener("pointerdown", (event) => beginDrag(event, element, clip, "right"));
    element.addEventListener("click", (event) => {
      event.stopPropagation();
      callbacks.onSelect(clip.id);
    });
    element.append(left, label, right);
    return element;
  }

  function makeTrackRow(track, width, selectedClipId) {
    const row = document.createElement("div");
    row.className = "track-row";
    row.dataset.trackId = track.id;
    row.dataset.domain = track.media_domain;
    row.style.width = `${width}px`;
    row.addEventListener("click", (event) => {
      const bounds = row.getBoundingClientRect();
      callbacks.onSeek((event.clientX - bounds.left) / pixelsPerSecond);
    });
    row.addEventListener("dragover", (event) => {
      event.preventDefault();
      row.classList.add("is-drop-target");
    });
    row.addEventListener("dragleave", () => row.classList.remove("is-drop-target"));
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      row.classList.remove("is-drop-target");
      const assetId = event.dataTransfer?.getData("application/x-video-create-asset");
      if (!assetId) return;
      const bounds = row.getBoundingClientRect();
      callbacks.onAssetDrop(track, assetId, Math.max(0, (event.clientX - bounds.left) / pixelsPerSecond));
    });
    for (const clip of track.clips) {
      row.append(makeClip(activeProject, track, clip, clip.id === selectedClipId));
    }
    return row;
  }

  function render(project, nextPixelsPerSecond, selectedClipId) {
    activeProject = project;
    pixelsPerSecond = nextPixelsPerSecond;
    labels.replaceChildren();
    tracks.replaceChildren();
    if (!project) return;
    const duration = Math.max(projectDuration(project), 15);
    const viewportWidth = Math.max(content.clientWidth - 8, 600);
    const width = Math.max(duration * pixelsPerSecond + 160, viewportWidth);
    renderRuler(width, Math.ceil(width / pixelsPerSecond));
    project.tracks.forEach((track, index) => {
      labels.append(makeLabel(track, index, project.tracks.length));
      tracks.append(makeTrackRow(track, width, selectedClipId));
    });
    updatePlayhead(0);
  }

  function updatePlayhead(seconds) {
    if (!activeProject) return;
    playhead.style.left = `${seconds * pixelsPerSecond}px`;
  }

  return { render, updatePlayhead };
}
